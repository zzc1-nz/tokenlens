"""One function per ``tokenlens`` subcommand.

Every command follows the same contract:

* signature ``cmd_xxx(args, *, client=None, out=None) -> int`` where ``args`` is the
  argparse namespace, ``client`` an optional pre-built
  :class:`~tokenlens_cli.api.TokenLensClient` (tests inject one backed by
  ``httpx.MockTransport``) and ``out`` an optional text stream;
* returns a process exit code: ``0`` success, ``1`` unreachable server, ``2`` the
  endpoint exists in the contract but not in this build, ``3`` bad usage;
* never raises for expected failures — it prints a friendly one-liner instead.

``--json`` is supported everywhere it makes sense (all read commands + ``detect`` and
``export``), and the JSON payload is always the *server's* shape (never a re-invented
one) so the CLI can be scripted against the API contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from tokenlens import timeutil

from . import api, data, hud, render
from .api import ApiError, TokenLensClient

EXIT_OK = 0
EXIT_OFFLINE = 1
EXIT_UNIMPLEMENTED = 2
EXIT_USAGE = 3


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def build_client(args: Any, *, transport: Any = None) -> TokenLensClient:
    """Build a client from the parsed ``--server``/``--api-key``/``--timeout`` options."""
    return TokenLensClient(
        getattr(args, "server", None),
        timeout=float(getattr(args, "timeout", api.DEFAULT_TIMEOUT) or api.DEFAULT_TIMEOUT),
        api_key=getattr(args, "api_key", None),
        transport=transport,
    )


def _out(stream: TextIO | None) -> TextIO:
    return stream if stream is not None else sys.stdout


def emit_json(payload: Any, out: TextIO | None = None) -> None:
    """Print a JSON payload deterministically (indented, UTF-8, no ASCII escaping)."""
    render.write_text(_out(out), json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def _print(text: str = "", out: TextIO | None = None) -> None:
    render.write_text(_out(out), text + "\n")


def _fail(exc: ApiError, *, command: str, server: str, out: TextIO | None = None) -> int:
    """Print a friendly error and map the failure to an exit code."""
    message = exc.friendly(server)
    _print(f"tokenlens {command}: {message}", out)
    if exc.missing:
        return EXIT_UNIMPLEMENTED
    if exc.offline:
        return EXIT_OFFLINE
    return EXIT_OFFLINE


def _guard(
    command: str,
    args: Any,
    client: TokenLensClient | None,
    out: TextIO | None,
    body: Any,
) -> int:
    """Run ``body(client, out)`` translating API errors into exit codes."""
    owns = client is None
    active = client or build_client(args)
    try:
        return int(body(active, _out(out)))
    except ApiError as exc:
        return _fail(exc, command=command, server=active.server, out=out)
    finally:
        if owns:
            active.close()


def _table(headers: list[str], rows: list[list[Any]], *, justify: list[str] | None = None) -> str:
    """Plain table (used for ``--no-color``/piped output)."""
    return render.plain_table(headers, rows, justify=justify)


def _use_rich(args: Any) -> bool:
    """True when the command should render through ``rich`` (i.e. a real terminal)."""
    if getattr(args, "json", False) or getattr(args, "no_color", False):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _rich_print(args: Any, renderable: Any, out: TextIO | None = None) -> None:
    """Print a ``rich`` renderable, degrading to ASCII on legacy code pages."""
    console = hud.make_console(no_color=bool(getattr(args, "no_color", False)), stream=_out(out))
    console.print(renderable)


# --------------------------------------------------------------------------------------
# hud / live
# --------------------------------------------------------------------------------------


def cmd_hud(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens hud`` — the live ai-token-hud style view (or one snapshot when piped)."""
    dimension = getattr(args, "dimension", "project") or "project"
    project = getattr(args, "project", None)
    interval = hud.clamp_interval(getattr(args, "interval", 1.0))

    owns = client is None
    active = client or build_client(args)
    try:
        interactive = _interactive(args)
        if not interactive:
            return hud.render_once(
                active,
                dimension=dimension,
                project=project,
                as_json=bool(getattr(args, "json", False)),
                no_color=bool(getattr(args, "no_color", False)),
                stream=_out(out),
            )
        console = hud.make_console(no_color=bool(getattr(args, "no_color", False)), stream=_out(out))
        return hud.run_hud(
            active,
            dimension=dimension,
            project=project,
            interval=interval,
            no_color=bool(getattr(args, "no_color", False)),
            iterations=getattr(args, "iterations", None),
            console=console,
        )
    finally:
        if owns:
            active.close()


def _interactive(args: Any) -> bool:
    """Decide between the live loop and the one-shot snapshot.

    ``--once`` forces the CI path; ``--no-color`` also forces it because colourless output
    is meant to be redirected. Otherwise the loop only runs when stdout is a TTY.
    """
    if getattr(args, "once", False) or getattr(args, "json", False):
        return False
    if getattr(args, "no_color", False):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def cmd_live(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens live`` — trailing-window burn rate and top talkers."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        payload = active.live(window_minutes=float(getattr(args, "window", 5) or 5))
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        tokens_per_min = float(payload.get("tokens_per_min") or 0.0)
        cost_per_hour = float(payload.get("cost_per_hour") or 0.0)
        _print(f"LIVE WINDOW  {payload.get('window_minutes', '?')} min", stream)
        _print(
            f"  burn {render.fmt_rate(tokens_per_min)}   {render.fmt_usd_rate(cost_per_hour)}"
            f"   agents {payload.get('active_agents', 0)}"
            f"   projects {payload.get('active_projects', 0)}"
            f"   sessions {payload.get('active_sessions', 0)}",
            stream,
        )
        rows = []
        for entry in payload.get("top_projects") or []:
            if isinstance(entry, dict):
                rows.append(
                    [
                        entry.get("project") or "-",
                        render.fmt_cost(entry.get("cost_usd")),
                        render.fmt_rate(entry.get("tokens_per_min")),
                    ]
                )
        if rows:
            _print("", stream)
            _print(_table(["PROJECT", "COST", "TOKENS/MIN"], rows, justify=["left", "right", "right"]), stream)
        agent_rows = []
        for entry in payload.get("top_agents") or []:
            if isinstance(entry, dict):
                agent_rows.append(
                    [
                        entry.get("agent") or "-",
                        render.fmt_cost(entry.get("cost_usd")),
                        render.fmt_rate(entry.get("tokens_per_min")),
                    ]
                )
        if agent_rows:
            _print("", stream)
            _print(_table(["AGENT", "COST", "TOKENS/MIN"], agent_rows, justify=["left", "right", "right"]), stream)
        return EXIT_OK

    return _guard("live", args, client, out, body)


# --------------------------------------------------------------------------------------
# summary / trends / breakdown
# --------------------------------------------------------------------------------------


def cmd_summary(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens summary`` — one ``Totals`` block for a filtered window."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        payload = active.summary(
            agent=getattr(args, "agent", None),
            project=getattr(args, "project", None),
            model=getattr(args, "model", None),
            from_value=getattr(args, "from_value", None),
            to_value=getattr(args, "to_value", None),
        )
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        totals = payload.get("totals") or {}
        window = payload.get("window") or {}
        _print(
            f"SUMMARY  {render.format_datetime(window.get('from'))} → "
            f"{render.format_datetime(window.get('to'))}",
            stream,
        )
        rows = [
            ["cost", render.fmt_cost(totals.get("cost_usd"))],
            ["total tokens", render.fmt_int(totals.get("total_tokens"))],
            ["  input", render.fmt_int(totals.get("input_tokens"))],
            ["  output", render.fmt_int(totals.get("output_tokens"))],
            ["  cache read", render.fmt_int(totals.get("cache_read_tokens"))],
            ["  cache write", render.fmt_int(totals.get("cache_write_tokens"))],
            ["  reasoning", render.fmt_int(totals.get("reasoning_tokens"))],
            ["billable tokens", render.fmt_int(totals.get("billable_tokens"))],
            ["requests", render.fmt_int(totals.get("requests"))],
            ["retries", render.fmt_int(totals.get("retries"))],
            ["tool calls", render.fmt_int(totals.get("tool_calls"))],
            ["cache hit rate", render.fmt_pct(totals.get("cache_hit_rate"))],
            ["avg cost/request", render.fmt_cost(totals.get("avg_cost_per_request"))],
            ["avg tokens/request", render.fmt_int(totals.get("avg_tokens_per_request"))],
            ["sessions / agents / projects", (
                f"{totals.get('sessions', 0)} / {totals.get('agents', 0)} / {totals.get('projects', 0)}"
            )],
        ]
        _print(_table(["METRIC", "VALUE"], rows, justify=["left", "right"]), stream)
        return EXIT_OK

    return _guard("summary", args, client, out, body)


def cmd_trends(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens trends`` — bucket series, sparkline and period-over-period delta."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        payload = active.trends(
            period=str(getattr(args, "period", "day") or "day"),
            limit=int(getattr(args, "limit", 30) or 30),
            agent=getattr(args, "agent", None),
            project=getattr(args, "project", None),
            model=getattr(args, "model", None),
        )
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        points = [p for p in payload.get("points") or [] if isinstance(p, dict)]
        comparison = payload.get("comparison") or {}
        _print(f"TRENDS  period={payload.get('period', '?')}  buckets={len(points)}", stream)
        delta_pct = float(comparison.get("delta_pct") or 0.0)
        arrow = "▲" if delta_pct > 0 else ("▼" if delta_pct < 0 else "=")
        _print(
            f"  current {render.fmt_cost(comparison.get('current_cost_usd'))}"
            f"   previous {render.fmt_cost(comparison.get('previous_cost_usd'))}"
            f"   {arrow} {delta_pct:+.1f}%"
            f"   tokens {render.fmt_int(comparison.get('current_tokens'))}"
            f" ({float(comparison.get('token_delta_pct') or 0.0):+.1f}%)",
            stream,
        )
        if not points:
            _print("  (no buckets in this window)", stream)
            return EXIT_OK
        costs = [float((p.get("totals") or {}).get("cost_usd") or 0.0) for p in points]
        spark = render.sparkline(costs, width=min(len(costs), 60))
        _print(f"  cost {spark}", stream)
        rows = []
        for point in points[-int(getattr(args, "limit", 30) or 30) :]:
            totals = point.get("totals") or {}
            rows.append(
                [
                    str(point.get("label") or render.format_datetime(point.get("bucket_start"))),
                    render.fmt_cost(totals.get("cost_usd")),
                    render.fmt_int(totals.get("total_tokens")),
                    render.fmt_int(totals.get("requests")),
                ]
            )
        _print("", stream)
        _print(
            _table(["BUCKET", "COST", "TOKENS", "REQUESTS"], rows, justify=["left", "right", "right", "right"]),
            stream,
        )
        return EXIT_OK

    return _guard("trends", args, client, out, body)


def cmd_breakdown(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens breakdown`` — cost share bars for one dimension."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        dimension = str(getattr(args, "dimension", "project") or "project")
        payload = active.breakdown(
            dimension,
            limit=int(getattr(args, "limit", 20) or 20),
            agent=getattr(args, "agent", None),
            project=getattr(args, "project", None),
            model=getattr(args, "model", None),
        )
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        rows = payload.get("rows") or []
        total = payload.get("total") or {}
        _print(
            f"BREAKDOWN  dimension={payload.get('dimension', dimension)}"
            f"   total {render.fmt_cost(total.get('cost_usd'))}"
            f"   tokens {render.fmt_tokens(total.get('total_tokens'))}",
            stream,
        )
        bars, total_cost, total_tokens = render.bars_from_breakdown_rows(
            rows,
            dimension=dimension,
            bar_width=max(12, min(40, (int(getattr(args, "width", render.DEFAULT_WIDTH) or 100)) - 52)),
            limit=int(getattr(args, "limit", 20) or 20),
        )
        if not bars:
            _print("  (no usage recorded in this window)", stream)
            return EXIT_OK
        table_rows = [
            [
                bar.label,
                render.fmt_cost(bar.value),
                render.fmt_pct(bar.share_pct / 100.0),
                render.fmt_tokens(bar.tokens),
                bar.bar,
            ]
            for bar in bars
        ]
        _print(
            _table(
                [dimension.upper(), "COST", "SHARE", "TOKENS", ""],
                table_rows,
                justify=["left", "right", "right", "right", "left"],
            ),
            stream,
        )
        _print(
            f"  total {render.fmt_cost(total_cost)}  tokens {render.fmt_tokens(total_tokens)}"
            f"  rows {len(bars)}",
            stream,
        )
        return EXIT_OK

    return _guard("breakdown", args, client, out, body)


# --------------------------------------------------------------------------------------
# budgets / alerts / anomalies
# --------------------------------------------------------------------------------------


def cmd_budgets(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens budgets`` — budget utilisation with projected overspend."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        payload = active.budgets()
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        statuses = [s for s in payload.get("items") or [] if isinstance(s, dict)]
        _print(f"BUDGETS  {len(statuses)} configured", stream)
        if not statuses:
            _print("  (no budgets configured — POST /api/v1/budgets to create one)", stream)
            return EXIT_OK
        bars = render.budget_bars_from_statuses(statuses)
        rows = [
            [
                bar.name,
                f"{bar.scope}/{bar.period}",
                render.fmt_cost(bar.spent_usd),
                render.fmt_cost(bar.limit_usd),
                render.fmt_pct(bar.pct),
                render.fmt_pct(bar.projected_pct),
                bar.status,
                bar.bar,
            ]
            for bar in bars
        ]
        _print(
            _table(
                ["BUDGET", "SCOPE", "SPENT", "LIMIT", "PCT", "PROJ", "STATUS", ""],
                rows,
                justify=["left", "left", "right", "right", "right", "right", "left", "left"],
            ),
            stream,
        )
        overspent = sum(bar.overspend_usd for bar in bars)
        if overspent > 0:
            _print(f"  overspend {render.fmt_cost(overspent)}", stream)
        return EXIT_OK

    return _guard("budgets", args, client, out, body)


def cmd_alerts(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens alerts`` — recent budget alerts with trigger counts."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        payload = active.alerts(
            limit=int(getattr(args, "limit", 20) or 20),
            severity=getattr(args, "severity", None),
        )
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        items = [i for i in payload.get("items") or [] if isinstance(i, dict)]
        _print(f"ALERTS  total {payload.get('total', len(items))}", stream)
        if not items:
            _print("  (no alerts recorded)", stream)
            return EXIT_OK
        rows = []
        for alert in items:
            rows.append(
                [
                    render.relative_age(alert.get("created_at")),
                    str(alert.get("severity") or "-"),
                    str(alert.get("budget_name") or "-"),
                    f"{alert.get('scope', '-')}/{alert.get('scope_key', '*')}",
                    render.fmt_pct(alert.get("threshold")),
                    render.fmt_cost(alert.get("spent_usd")),
                    render.fmt_cost(alert.get("limit_usd")),
                    str(alert.get("trigger_count", 1)),
                ]
            )
        _print(
            _table(
                ["WHEN", "SEVERITY", "BUDGET", "SCOPE", "THRESH", "SPENT", "LIMIT", "TRIGGERS"],
                rows,
                justify=["left", "left", "left", "left", "right", "right", "right", "right"],
            ),
            stream,
        )
        return EXIT_OK

    return _guard("alerts", args, client, out, body)


def cmd_anomalies(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens anomalies`` — detected anomalies with root cause and evidence."""

    def body(active: TokenLensClient, stream: TextIO) -> int:
        payload = active.anomalies(
            limit=int(getattr(args, "limit", 20) or 20),
            status=getattr(args, "status", None),
        )
        if getattr(args, "json", False):
            emit_json(payload, stream)
            return EXIT_OK
        items = [i for i in payload.get("items") or [] if isinstance(i, dict)]
        _print(f"ANOMALIES  total {payload.get('total', len(items))}", stream)
        if not items:
            _print("  (no anomalies detected — run `tokenlens detect` after loading data)", stream)
            return EXIT_OK
        rows = []
        for item in items:
            rows.append(
                [
                    render.relative_age(item.get("detected_at")),
                    str(item.get("agent") or "-"),
                    str(item.get("project") or "-"),
                    str(item.get("reason_label") or item.get("reason") or "-"),
                    f"{float(item.get('score') or 0.0):.1f}",
                    f"{float(item.get('ratio') or 0.0):.1f}x",
                    str(item.get("severity") or "-"),
                    str(item.get("status") or "-"),
                    render.fmt_cost(item.get("cost_usd")),
                ]
            )
        _print(
            _table(
                ["WHEN", "AGENT", "PROJECT", "REASON", "SCORE", "RATIO", "SEVERITY", "STATUS", "COST"],
                rows,
                justify=["left", "left", "left", "left", "right", "right", "left", "left", "right"],
            ),
            stream,
        )
        return EXIT_OK

    return _guard("anomalies", args, client, out, body)


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------


def cmd_export(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens export`` — download a CSV dataset to ``--out`` (or stdout)."""
    dataset = str(getattr(args, "dataset", "timeseries") or "timeseries")
    target = getattr(args, "out", None)

    def body(active: TokenLensClient, stream: TextIO) -> int:
        content = active.export_csv(
            dataset,
            limit=getattr(args, "limit", None),
            from_value=getattr(args, "from_value", None),
            to_value=getattr(args, "to_value", None),
            bucket=getattr(args, "bucket", None),
            dimension=getattr(args, "dimension", None),
        )
        if target:
            path = Path(str(target))
            if path.parent and str(path.parent) not in ("", "."):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if getattr(args, "json", False):
                emit_json(
                    {"dataset": dataset, "path": str(path), "bytes": len(content)}, stream
                )
            else:
                lines = content.count(b"\n")
                _print(
                    f"exported {dataset} → {path} "
                    f"({render.format_bytes(len(content))}, {lines} lines)",
                    stream,
                )
            return EXIT_OK
        # No --out: write raw CSV to stdout so it can be piped (`tokenlens export | ...`).
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(content)
            buffer.flush()
        else:  # pragma: no cover - StringIO in tests
            stream.write(content.decode("utf-8-sig", errors="replace"))
        return EXIT_OK

    return _guard("export", args, client, out, body)


# --------------------------------------------------------------------------------------
# detect
# --------------------------------------------------------------------------------------


def cmd_detect(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens detect`` — run one anomaly detection pass (and optionally budgets)."""
    lookback = int(getattr(args, "lookback", 60) or 60)
    also_budgets = bool(getattr(args, "evaluate_budgets", False))

    def body(active: TokenLensClient, stream: TextIO) -> int:
        detected = active.detect_anomalies(lookback_minutes=lookback)
        budget_result: dict[str, Any] | None = None
        if also_budgets:
            try:
                budget_result = active.evaluate_budgets(dry_run=bool(getattr(args, "dry_run", False)))
            except ApiError as exc:
                if exc.offline:
                    raise
                detected.setdefault("warnings", []).append(f"budget evaluation failed: {exc}")
        if getattr(args, "json", False):
            payload: dict[str, Any] = dict(detected)
            if budget_result is not None:
                payload["budgets"] = budget_result
            emit_json(payload, stream)
            return EXIT_OK
        events = [e for e in detected.get("detected") or [] if isinstance(e, dict)]
        _print(
            f"DETECT  lookback={detected.get('lookback_minutes', lookback)}min"
            f"  agents_scanned={detected.get('scanned_agents', 0)}"
            f"  detected={len(events)}",
            stream,
        )
        for event in events:
            _print(
                f"  ! {event.get('agent', '-')} / {event.get('project', '-')}"
                f"  {event.get('reason_label') or event.get('reason')}"
                f"  score {float(event.get('score') or 0.0):.1f}"
                f"  ratio {float(event.get('ratio') or 0.0):.1f}x"
                f"  {event.get('severity')}"
                f"  {render.fmt_cost(event.get('cost_usd'))}",
                stream,
            )
        if not events:
            _print("  (nothing anomalous in the lookback window)", stream)
        if budget_result is not None:
            triggered = budget_result.get("triggered") or []
            _print(
                f"  budgets evaluated {budget_result.get('evaluated', 0)}"
                f"  triggered {len(triggered)}"
                f"  dry_run {budget_result.get('dry_run', False)}",
                stream,
            )
        for warning in detected.get("warnings") or []:
            _print(f"  warning: {warning}", stream)
        return EXIT_OK

    return _guard("detect", args, client, out, body)


# --------------------------------------------------------------------------------------
# simulate / version
# --------------------------------------------------------------------------------------


def cmd_simulate(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens simulate`` — delegate to ``tools/simulate.py``.

    The simulator lives outside the package on purpose (it is an operator tool, not part
    of the client library), so this command loads it by path and forwards the flags. The
    same code is runnable as ``python tools/simulate.py ...``.
    """
    from tools import simulate as simulate_module  # local import keeps CLI start-up fast

    argv = simulate_module.argv_from_namespace(args)
    return int(simulate_module.main(argv, out=_out(out)))


def cmd_version(args: Any, *, client: TokenLensClient | None = None, out: TextIO | None = None) -> int:
    """``tokenlens version`` — client version, optionally probing the server."""
    from tokenlens import __version__ as server_version

    from . import __version__ as client_version

    payload: dict[str, Any] = {
        "client": client_version,
        "package": server_version,
        "server": None,
        "reachable": False,
    }
    if not getattr(args, "offline", False):
        owns = client is None
        active = client or build_client(args)
        try:
            health = active.health()
            payload.update(
                {
                    "server": active.server,
                    "reachable": True,
                    "server_version": health.get("version"),
                    "price_version": health.get("price_version"),
                    "snapshots": health.get("snapshots"),
                }
            )
        except ApiError as exc:
            payload["server"] = active.server
            payload["error"] = exc.friendly(active.server)
        finally:
            if owns:
                active.close()
    if getattr(args, "json", False):
        emit_json(payload, out)
        return EXIT_OK if payload["reachable"] or getattr(args, "offline", False) else EXIT_OFFLINE
    stream = _out(out)
    _print(f"tokenlens client {payload['client']}  (package {payload['package']})", stream)
    if payload.get("reachable"):
        _print(
            f"server {payload['server']}  version {payload.get('server_version')}"
            f"  price {payload.get('price_version')}"
            f"  snapshots {payload.get('snapshots')}",
            stream,
        )
        return EXIT_OK
    if getattr(args, "offline", False):
        _print("server: not probed (--offline)", stream)
        return EXIT_OK
    _print(f"server {payload['server']}: {payload.get('error', 'unreachable')}", stream)
    return EXIT_OFFLINE


#: Registry consumed by ``main.py`` and the tests (command name -> callable).
COMMANDS = {
    "hud": cmd_hud,
    "live": cmd_live,
    "summary": cmd_summary,
    "trends": cmd_trends,
    "breakdown": cmd_breakdown,
    "budgets": cmd_budgets,
    "alerts": cmd_alerts,
    "anomalies": cmd_anomalies,
    "export": cmd_export,
    "detect": cmd_detect,
    "simulate": cmd_simulate,
    "version": cmd_version,
}

__all__ = ["COMMANDS", "build_client"] + [f"cmd_{name}" for name in COMMANDS]
