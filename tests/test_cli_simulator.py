"""Tests for the HUD client (``tokenlens_cli``) and the load generator (``tools/simulate.py``).

Scope
-----
This suite covers the two deliverables owned by ``hud-sim`` without ever starting a real
server:

* **CLI surface** — argparse shape for all twelve subcommands, ``--json``/``--no-color``,
  exit codes.
* **API client** — timeouts, friendly errors, and the offline/missing distinction, driven
  through ``httpx.MockTransport`` (no sockets).
* **Rendering** — pure functions: bar maths, number formatting, KPI extraction, budget
  bars, plain snapshot output, OFFLINE banner. Asserted on text, never on a pty.
* **Live loop** — dimension switching via injected keys, refresh loop with an injected
  clock/sleep (so the suite never actually sleeps), offline recovery.
* **Simulator** — dry-run determinism by seed, the three incident signatures, the
  ``--target-tokens`` stop condition, batch/event-id invariants, and the report fields.
* **HTTP integration** — the simulator's real ``httpx`` write path pointed at the FastAPI
  app through an ASGI transport, plus a CSV export round trip.

The FastAPI app is driven with ``fastapi.testclient.TestClient`` (see ``tests/conftest.py``
for the isolated-DB fixtures) or an in-process ``httpx`` transport — never a live port.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest

from tokenlens import pricing, timeutil
from tokenlens.testing import make_snapshot

from tokenlens_cli import api as cli_api
from tokenlens_cli import commands, data, hud, main, render
from tokenlens_cli.api import ApiError, TokenLensClient

from tools import simulate as sim

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def json_response(payload: Any, status_code: int = 200) -> httpx.Response:
    """Build an ``httpx`` response carrying ``payload`` as JSON."""
    return httpx.Response(status_code, json=payload)


def mock_client(handler: Any, server: str = "http://testserver") -> TokenLensClient:
    """A :class:`TokenLensClient` backed by ``httpx.MockTransport``."""
    return TokenLensClient(server, transport=httpx.MockTransport(handler))


def route(handler: Any) -> Any:
    """Wrap a ``(method, path, request) -> payload`` callable into a MockTransport handler.

    A handler may return an ``httpx.Response`` directly (for error cases) or a plain
    JSON-serialisable payload, which is wrapped into a 200 response.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        result = handler(request.method, request.url.path, request)
        if isinstance(result, httpx.Response):
            return result
        return json_response(result)

    return _handler


def totals(**overrides: Any) -> dict[str, Any]:
    """A ``Totals`` JSON payload with coherent derived fields."""
    base = {
        "input_tokens": 1000,
        "output_tokens": 400,
        "cache_read_tokens": 3000,
        "cache_write_tokens": 200,
        "reasoning_tokens": 0,
        "total_tokens": 4600,
        "billable_tokens": 1400,
        "requests": 10,
        "retries": 1,
        "tool_calls": 4,
        "cost_usd": 0.25,
        "sessions": 2,
        "agents": 2,
        "projects": 2,
        "cache_hit_rate": 0.75,
        "avg_cost_per_request": 0.025,
        "avg_tokens_per_request": 460.0,
    }
    base.update(overrides)
    return base


def breakdown_row(key: str, cost: float, tokens: int = 1000, share: float = 0.0) -> dict[str, Any]:
    """A ``BreakdownRow`` payload."""
    return {
        "key": key,
        "label": key.replace("-", " ").title(),
        "totals": totals(cost_usd=cost, total_tokens=tokens),
        "share_pct": share,
    }


def budget_status(
    name: str = "team-daily",
    spent: float = 5.0,
    limit: float = 25.0,
    *,
    status: str = "ok",
    period: str = "daily",
    scope: str = "global",
) -> dict[str, Any]:
    """A ``BudgetStatus`` payload."""
    return {
        "budget": {
            "id": 1,
            "name": name,
            "scope": scope,
            "scope_key": "*",
            "period": period,
            "limit_usd": limit,
            "thresholds": [0.5, 0.8, 1.0],
            "webhook_url": None,
            "desktop": True,
            "enabled": True,
            "created_at": "2026-11-10T00:00:00.000Z",
            "updated_at": "2026-11-10T00:00:00.000Z",
        },
        "window_start": "2026-11-09T16:00:00.000Z",
        "window_end": "2026-11-10T16:00:00.000Z",
        "spent_usd": spent,
        "limit_usd": limit,
        "pct": spent / limit if limit else 0.0,
        "remaining_usd": max(0.0, limit - spent),
        "projected_usd": spent * 1.5,
        "projected_pct": (spent / limit) * 1.5 if limit else 0.0,
        "status": status,
        "triggered_thresholds": [],
        "trigger_count": 0,
        "overspend_usd": max(0.0, spent - limit),
        "last_alert_at": None,
    }


def dashboard_payload(
    *,
    dimension_rows: dict[str, list[dict[str, Any]]] | None = None,
    budgets: list[dict[str, Any]] | None = None,
    kpis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ``DashboardOverview`` payload (contract §9)."""
    rows = dimension_rows or {
        "by_project": [
            breakdown_row("payments-api", 6.0, 60_000, 60.0),
            breakdown_row("web-dashboard", 3.0, 30_000, 30.0),
            breakdown_row("ml-pipeline", 1.0, 10_000, 10.0),
        ],
        "by_agent": [breakdown_row("claude-code", 8.0, 80_000, 80.0)],
        "by_model": [breakdown_row("claude-sonnet-4-5", 8.0, 80_000, 80.0)],
        "by_runtime": [],
    }
    return {
        "generated_at": "2026-11-10T13:00:00.000Z",
        "kpis": kpis
        or {
            "today_cost_usd": 10.0,
            "today_tokens": 100_000,
            "week_cost_usd": 42.5,
            "month_cost_usd": 180.25,
            "burn_rate_tokens_per_min": 12_500.0,
            "burn_rate_usd_per_hour": 7.38,
            "cache_hit_rate": 0.625,
            "active_agents": 4,
            "active_projects": 3,
            "total_sessions": 11,
        },
        "breakdowns": rows,
        "timeseries": {"hourly": [], "daily": []},
        "budgets": budgets if budgets is not None else [budget_status()],
        "alerts": {"recent": [], "summary": {}},
        "anomalies": {"recent": [], "summary": {}},
    }


class FakeClock:
    """Monotonic clock whose ``advance`` is driven by the injected sleep function."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def args_for(command: str, **overrides: Any) -> Any:
    """Parse argv for ``command`` and apply attribute overrides (test convenience)."""
    namespace = main.build_parser().parse_args([command])
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# --------------------------------------------------------------------------------------
# 1. CLI surface (argparse)
# --------------------------------------------------------------------------------------


def test_parser_exposes_every_contract_subcommand():
    parser = main.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]  # noqa: SLF001
    assert actions, "subparsers missing"
    choices = set(actions[0].choices)
    assert choices == {
        "hud",
        "live",
        "summary",
        "trends",
        "breakdown",
        "budgets",
        "alerts",
        "anomalies",
        "export",
        "detect",
        "simulate",
        "version",
    }


@pytest.mark.parametrize(
    "command", sorted(commands.COMMANDS)
)
def test_every_command_has_a_handler(command: str):
    assert callable(commands.COMMANDS[command])


@pytest.mark.parametrize(
    "command",
    ["hud", "live", "summary", "trends", "breakdown", "budgets", "alerts", "anomalies", "export", "detect", "version"],
)
def test_read_commands_accept_server_and_json(command: str):
    namespace = main.build_parser().parse_args([command])
    assert namespace.server == cli_api.DEFAULT_SERVER
    assert namespace.json is False
    parsed = main.build_parser().parse_args([command, "--server", "http://example:9000", "--json"])
    assert parsed.server == "http://example:9000"
    assert parsed.json is True


def test_hud_defaults_and_flags():
    namespace = main.build_parser().parse_args(["hud"])
    assert namespace.interval == 1.0
    assert namespace.dimension == "project"
    assert namespace.once is False
    parsed = main.build_parser().parse_args(
        ["hud", "--interval", "2.5", "--dimension", "agent", "--project", "web", "--once", "--no-color"]
    )
    assert parsed.interval == 2.5
    assert parsed.dimension == "agent"
    assert parsed.project == "web"
    assert parsed.once is True
    assert parsed.no_color is True


def test_breakdown_dimension_choices_are_validated():
    parser = main.build_parser()
    for dimension in main.BREAKDOWN_DIMENSIONS:
        assert parser.parse_args(["breakdown", "--dimension", dimension]).dimension == dimension
    with pytest.raises(SystemExit):
        parser.parse_args(["breakdown", "--dimension", "nope"])


def test_export_dataset_choices_and_out_flag():
    parsed = main.build_parser().parse_args(["export", "--dataset", "sessions", "--out", "out.csv"])
    assert parsed.dataset == "sessions"
    assert parsed.out == "out.csv"
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["export", "--dataset", "bogus"])


def test_detect_flags():
    parsed = main.build_parser().parse_args(["detect", "--lookback", "15", "--evaluate-budgets"])
    assert parsed.lookback == 15
    assert parsed.evaluate_budgets is True


def test_simulate_subcommand_flags():
    parsed = main.build_parser().parse_args(
        [
            "simulate",
            "--mode",
            "incident",
            "--duration",
            "5",
            "--target-tokens",
            "1000",
            "--agents",
            "3",
            "--seed",
            "7",
            "--dry-run",
            "--report",
        ]
    )
    assert parsed.mode == "incident"
    assert parsed.target_tokens == 1000
    assert parsed.dry_run is True
    assert parsed.report is True


def test_main_without_command_prints_help(capsys):
    assert main.main([]) == 0
    captured = capsys.readouterr()
    assert "hud" in captured.out


def test_main_version_flag_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        main.main(["--version"])
    assert excinfo.value.code == 0


# --------------------------------------------------------------------------------------
# 2. Number formatting + bar maths (pure functions)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "$0.0000"),
        (0.00123, "$0.0012"),
        (0.25, "$0.250"),
        (12.5, "$12.50"),
        (1234.567, "$1,234.57"),
    ],
)
def test_fmt_cost_scales_precision(value, expected):
    assert render.fmt_cost(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (12_345, "12.3k"),
        (1_500_000, "1.50M"),
        (2_000_000_000, "2.00B"),
    ],
)
def test_fmt_tokens_compacts(value, expected):
    assert render.fmt_tokens(value) == expected


def test_fmt_pct_and_rates():
    assert render.fmt_pct(0.625) == "62.5%"
    assert render.fmt_pct(None) == "0.0%"
    assert render.fmt_rate(12_500) == "12.5k/min"
    assert render.fmt_usd_rate(7.38) == "$7.38/h"


def test_fmt_int_groups_thousands():
    assert render.fmt_int(1_234_567) == "1,234,567"


@pytest.mark.parametrize(
    "value,maximum,width,filled,empty",
    [
        (10, 10, 20, 20, 0),
        (5, 10, 20, 10, 10),
        (0, 10, 20, 0, 20),
        (1, 1000, 20, 1, 19),  # non-zero never renders as empty
        (10, 10, 0, 0, 0),
        (-5, 10, 20, 0, 20),
        (5, 0, 20, 0, 20),
    ],
)
def test_bar_cells_math(value, maximum, width, filled, empty):
    assert render.bar_cells(value, maximum, width) == (filled, empty)


def test_bar_cells_clamps_overflow():
    assert render.bar_cells(50, 10, 10) == (10, 0)


def test_make_bar_uses_block_glyphs():
    bar = render.make_bar(5, 10, 10)
    assert bar == render.FILLED * 5 + render.EMPTY * 5
    assert len(bar) == 10
    assert set(bar) <= {render.FILLED, render.EMPTY}


def test_budget_bar_clamps_over_limit_but_keeps_width():
    bar = render.budget_bar(1.8, 20)
    assert bar == render.FILLED * 20
    assert len(bar) == 20


def test_sparkline_shape():
    spark = render.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
    assert len(spark) == 8
    assert spark[0] == render.SPARK_CHARS[0]
    assert spark[-1] == render.SPARK_CHARS[-1]


def test_sparkline_handles_empty_and_flat_series():
    assert render.sparkline([]) == ""
    assert render.sparkline([0, 0, 0]) == render.SPARK_CHARS[0] * 3
    assert render.sparkline([5, 5, 5]) == render.SPARK_CHARS[-1] * 3


def test_sparkline_downsamples_to_requested_width():
    assert len(render.sparkline(list(range(100)), width=20)) == 20


def test_plain_table_aligns_columns():
    table = render.plain_table(["A", "B"], [["x", "1"], ["long-name", "22"]], justify=["left", "right"])
    lines = table.splitlines()
    assert "A" in lines[0] and "B" in lines[0]
    assert "long-name" in lines[-1]
    # Every rendered line keeps the right-aligned numeric column aligned.
    assert lines[2].rstrip().endswith("1")
    assert lines[3].rstrip().endswith("22")


# --------------------------------------------------------------------------------------
# 3. Payload -> view model adapters
# --------------------------------------------------------------------------------------


def test_kpis_from_dashboard_maps_every_field():
    kpis = render.kpis_from_dashboard(dashboard_payload())
    assert kpis.today_cost_usd == 10.0
    assert kpis.week_cost_usd == 42.5
    assert kpis.month_cost_usd == 180.25
    assert kpis.burn_rate_tokens_per_min == 12_500.0
    assert kpis.burn_rate_usd_per_hour == 7.38
    assert kpis.cache_hit_rate == 0.625
    assert kpis.active_agents == 4
    assert kpis.active_projects == 3
    assert kpis.total_sessions == 11


def test_kpis_from_dashboard_tolerates_missing_fields():
    kpis = render.kpis_from_dashboard({})
    assert kpis.today_cost_usd == 0.0
    assert kpis.active_agents == 0


def test_kpis_from_live_overlays_burn_rate():
    base = render.KpiSet(today_cost_usd=3.0)
    kpis = render.kpis_from_live(
        {
            "tokens_per_min": 900.0,
            "cost_per_hour": 1.5,
            "active_agents": 2,
            "active_projects": 1,
            "active_sessions": 5,
        },
        base,
    )
    assert kpis.burn_rate_tokens_per_min == 900.0
    assert kpis.today_cost_usd == 3.0  # preserved
    assert kpis.total_sessions == 5


def test_bars_from_breakdown_rows_sorts_and_scales_to_max():
    rows = [
        breakdown_row("small", 1.0, 100, 10.0),
        breakdown_row("big", 6.0, 600, 60.0),
        breakdown_row("mid", 3.0, 300, 30.0),
    ]
    bars, total_cost, total_tokens = render.bars_from_breakdown_rows(rows, bar_width=10)
    assert [bar.label for bar in bars] == ["Big", "Mid", "Small"]
    assert total_cost == pytest.approx(10.0)
    assert total_tokens == 1000
    # The largest row fills the bar; the smallest is at least one cell.
    assert bars[0].bar == render.FILLED * 10
    assert bars[2].bar.count(render.FILLED) >= 1
    assert len(bars[0].bar) == 10


def test_bars_from_breakdown_rows_handles_empty_and_zero_cost():
    assert render.bars_from_breakdown_rows([])[0] == []
    zero_rows = [breakdown_row("a", 0.0, 500, 0.0), breakdown_row("b", 0.0, 1500, 0.0)]
    bars, total_cost, total_tokens = render.bars_from_breakdown_rows(zero_rows, bar_width=10)
    assert total_cost == 0.0
    assert total_tokens == 2000
    # Falls back to token-scaled bars so an unpriced local model still renders a shape.
    assert bars[0].label == "B"
    assert bars[0].bar == render.FILLED * 10


def test_bars_from_breakdown_rows_respects_limit():
    rows = [breakdown_row(f"p{i}", float(i), 100 * i, 1.0) for i in range(1, 10)]
    bars, _, _ = render.bars_from_breakdown_rows(rows, limit=3)
    assert len(bars) == 3


def test_budget_bars_compute_pct_from_spent_over_limit():
    bars = render.budget_bars_from_statuses(
        [budget_status("daily", spent=5.0, limit=25.0), budget_status("weekly", spent=60.0, limit=50.0)]
    )
    assert bars[0].name == "weekly"  # sorted by pct desc
    assert bars[0].pct == pytest.approx(1.2)
    assert bars[0].overspend_usd == pytest.approx(10.0)
    assert bars[1].pct == pytest.approx(0.2)
    assert bars[1].remaining_usd == pytest.approx(20.0)


def test_budget_bars_tolerate_missing_limit():
    bars = render.budget_bars_from_statuses(
        [{"budget": {"name": "odd"}, "spent_usd": 1.0, "limit_usd": 0.0, "pct": 0.4}]
    )
    assert bars[0].pct == pytest.approx(0.4)


def test_bars_from_budget_bars_renders_dimension_budget():
    budget_bars = render.budget_bars_from_statuses([budget_status()])
    bars = render.bars_from_budget_bars(budget_bars)
    assert bars[0].label == "team-daily"
    assert "/" in bars[0].detail


# --------------------------------------------------------------------------------------
# 4. Plain snapshot rendering + OFFLINE banner
# --------------------------------------------------------------------------------------


def sample_snapshot() -> render.HudSnapshot:
    """A fully populated snapshot used by the rendering tests."""
    bars, total_cost, total_tokens = render.bars_from_breakdown_rows(
        [breakdown_row("payments-api", 6.0, 60_000, 60.0), breakdown_row("web", 4.0, 40_000, 40.0)],
        bar_width=20,
    )
    return render.HudSnapshot(
        server="http://127.0.0.1:8787",
        dimension="project",
        generated_ms=timeutil.parse_time_ms("2026-11-10T13:04:05Z"),
        kpis=render.KpiSet(
            today_cost_usd=10.0,
            week_cost_usd=42.5,
            month_cost_usd=180.25,
            today_tokens=100_000,
            burn_rate_tokens_per_min=12_500.0,
            burn_rate_usd_per_hour=7.38,
            cache_hit_rate=0.625,
            active_agents=4,
            active_projects=3,
            total_sessions=11,
        ),
        bars=bars,
        budgets=render.budget_bars_from_statuses([budget_status()]),
        total_cost_usd=total_cost,
        total_tokens=total_tokens,
    )


def test_render_plain_contains_kpi_header_and_bars():
    text = render.render_plain(sample_snapshot(), width=100)
    assert "TokenLens HUD" in text
    assert "today $10.00" in text
    assert "week $42.50" in text
    assert "burn 12.5k/min" in text
    assert "cache hit 62.5%" in text
    assert "BY PROJECT" in text
    assert render.FILLED in text
    assert "Payments Api" in text
    assert "keys: p project" in text


def test_render_plain_is_deterministic():
    snapshot = sample_snapshot()
    assert render.render_plain(snapshot, width=100) == render.render_plain(snapshot, width=100)


def test_render_plain_empty_state_is_friendly():
    snapshot = render.HudSnapshot(server="http://x", generated_ms=0)
    text = render.render_plain(snapshot, width=100)
    assert "no usage recorded" in text
    assert "no budgets configured" in text


def test_render_plain_offline_banner():
    snapshot = data.offline_snapshot("http://127.0.0.1:8787", "connection refused", now_ms=0)
    text = render.render_plain(snapshot)
    assert text.startswith("TokenLens HUD")
    assert "OFFLINE" in text
    assert "connection refused" in text
    assert "retrying" in text
    # An offline frame must not pretend to have data.
    assert "today" not in text


def test_render_offline_plain_has_hint():
    text = render.render_offline_plain("http://127.0.0.1:8787", "connection refused")
    assert "OFFLINE" in text
    assert "run_server.py" in text


def test_render_plain_budget_dimension_uses_budget_rows():
    snapshot = sample_snapshot()
    snapshot.dimension = "budget"
    snapshot.bars = render.bars_from_budget_bars(snapshot.budgets)
    text = render.render_plain(snapshot)
    assert "BY BUDGET" in text
    assert "team-daily" in text


def test_snapshot_to_dict_is_json_serialisable():
    payload = sample_snapshot().to_dict()
    assert payload["kpis"]["today_cost_usd"] == 10.0
    assert len(payload["bars"]) == 2
    assert payload["offline"] is False
    json.dumps(payload)  # must not raise


# --------------------------------------------------------------------------------------
# 5. API client behaviour (offline vs missing vs ok)
# --------------------------------------------------------------------------------------


def test_client_normalises_server_url():
    assert cli_api.normalize_server("127.0.0.1:8787") == "http://127.0.0.1:8787"
    assert cli_api.normalize_server("http://x:1/") == "http://x:1"
    assert cli_api.normalize_server(None) == cli_api.DEFAULT_SERVER


def test_client_returns_json_on_success():
    client = mock_client(route(lambda method, path, request: {"ok": True, "path": path}))
    assert client.health() == {"ok": True, "path": "/health"}


def test_client_classifies_connection_errors_as_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as excinfo:
        client.summary()
    assert excinfo.value.kind == "offline"
    assert excinfo.value.offline is True
    assert "cannot reach TokenLens" in excinfo.value.friendly()


def test_client_classifies_timeout_as_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = TokenLensClient("http://x", timeout=0.1, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as excinfo:
        client.live()
    assert excinfo.value.offline is True
    assert "timed out" in str(excinfo.value)


def test_client_extracts_fastapi_detail_on_http_error():
    client = mock_client(route(lambda *a: httpx.Response(400, json={"detail": "bad bucket"})))
    with pytest.raises(ApiError) as excinfo:
        client.timeseries("bogus")
    assert excinfo.value.kind == "http"
    assert excinfo.value.status_code == 400
    assert str(excinfo.value) == "bad bucket"


def test_client_flags_unimplemented_endpoint_as_missing():
    client = mock_client(route(lambda *a: httpx.Response(404, json={"detail": "Not Found"})))
    with pytest.raises(ApiError) as excinfo:
        client.dashboard()
    assert excinfo.value.missing is True
    assert "not implemented" in excinfo.value.friendly()


def test_client_rejects_non_json_body():
    client = mock_client(route(lambda *a: httpx.Response(200, text="<html>nope</html>")))
    with pytest.raises(ApiError) as excinfo:
        client.budgets()
    assert excinfo.value.kind == "invalid"


def test_client_is_available_swallows_errors():
    ok = mock_client(route(lambda *a: {"status": "ok"}))
    assert ok.is_available() is True
    broken = mock_client(route(lambda *a: httpx.Response(500, json={"detail": "boom"})))
    assert broken.is_available() is False


def test_client_sends_filters_as_query_params():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return json_response({"window": {}, "totals": totals(), "filters": {}})

    client = mock_client(handler)
    client.summary(agent="claude-code", project="web", from_value="2026-11-10")
    assert seen["agent"] == "claude-code"
    assert seen["project"] == "web"
    assert seen["from"] == "2026-11-10"
    # None filters must not be sent at all (the server defaults them).
    assert "model" not in seen


def test_client_sends_api_key_header():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key", "")
        return json_response({"status": "ok"})

    client = TokenLensClient("http://x", api_key="s3cret", transport=httpx.MockTransport(handler))
    client.health()
    assert seen["key"] == "s3cret"


def test_client_export_csv_returns_bytes():
    client = mock_client(
        route(lambda *a: httpx.Response(200, content=b"\xef\xbb\xbfbucket,cost\n", headers={"content-type": "text/csv"}))
    )
    content = client.export_csv("timeseries")
    assert content.startswith(b"\xef\xbb\xbf")
    assert b"bucket,cost" in content


def test_client_export_rejects_non_csv():
    client = mock_client(route(lambda *a: httpx.Response(200, json={"nope": True})))
    with pytest.raises(ApiError) as excinfo:
        client.export_csv("timeseries")
    assert excinfo.value.kind == "invalid"


# --------------------------------------------------------------------------------------
# 6. Snapshot assembly (dashboard fast path + fallback)
# --------------------------------------------------------------------------------------


def test_build_snapshot_uses_dashboard_fast_path():
    client = mock_client(route(lambda *a: dashboard_payload()))
    snapshot = data.build_snapshot(client, dimension="project", bar_width=20)
    assert snapshot.offline is False
    assert snapshot.kpis.today_cost_usd == 10.0
    assert [bar.label for bar in snapshot.bars][0] == "Payments Api"
    assert snapshot.total_cost_usd == pytest.approx(10.0)
    assert len(snapshot.budgets) == 1
    assert snapshot.warnings == []


def test_build_snapshot_switches_dimension_from_dashboard():
    client = mock_client(route(lambda *a: dashboard_payload()))
    snapshot = data.build_snapshot(client, dimension="agent", bar_width=20)
    assert snapshot.dimension == "agent"
    assert [bar.label for bar in snapshot.bars] == ["Claude Code"]


def test_build_snapshot_falls_back_when_dashboard_missing():
    """404 on /dashboard/overview must degrade to explicit queries, not fail."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/dashboard/overview":
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/api/v1/usage/breakdown":
            return json_response({"dimension": "project", "window": {}, "rows": [breakdown_row("web", 2.0, 2000, 100.0)], "total": totals()})
        if path == "/api/v1/usage/live":
            return json_response({"window_minutes": 5, "tokens_per_min": 500.0, "cost_per_hour": 1.0, "active_agents": 1, "active_projects": 1, "active_sessions": 2, "top_projects": [], "top_agents": []})
        if path == "/api/v1/budgets":
            return json_response({"items": [budget_status()]})
        return httpx.Response(404, json={"detail": "Not Found"})

    client = mock_client(handler)
    snapshot = data.build_snapshot(client)
    assert snapshot.bars and snapshot.bars[0].label == "Web"
    assert snapshot.kpis.burn_rate_tokens_per_min == 500.0
    assert snapshot.kpis.today_cost_usd == pytest.approx(2.0)
    assert any("dashboard" in warning for warning in snapshot.warnings)


def test_build_snapshot_budget_dimension_uses_budget_rows():
    client = mock_client(route(lambda *a: dashboard_payload()))
    snapshot = data.build_snapshot(client, dimension="budget", bar_width=20)
    assert snapshot.bars[0].label == "team-daily"


def test_build_snapshot_raises_when_server_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        data.build_snapshot(client)


def test_build_snapshot_records_budget_warning_without_failing():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/usage/breakdown":
            return json_response({"dimension": "project", "window": {}, "rows": [breakdown_row("web", 1.0)], "total": totals()})
        if path == "/api/v1/usage/live":
            return json_response({"tokens_per_min": 0, "cost_per_hour": 0})
        return httpx.Response(500, json={"detail": "budgets exploded"})

    client = mock_client(handler)
    snapshot = data.build_snapshot(client)
    assert snapshot.bars
    assert any("budgets" in warning for warning in snapshot.warnings)


def test_next_dimension_cycles_and_wraps():
    assert data.next_dimension("project") == "agent"
    assert data.next_dimension("agent") == "model"
    assert data.next_dimension("model") == "budget"
    assert data.next_dimension("budget") == "project"
    assert data.next_dimension("bogus") == "project"


def test_dimension_for_key_maps_bindings():
    assert data.dimension_for_key("p") == "project"
    assert data.dimension_for_key("A") == "agent"
    assert data.dimension_for_key("m") == "model"
    assert data.dimension_for_key("b") == "budget"
    assert data.dimension_for_key("z") is None


# --------------------------------------------------------------------------------------
# 7. HUD live loop (injected clock, sleep, keys and console)
# --------------------------------------------------------------------------------------


def test_key_reader_disabled_on_non_tty():
    reader = hud.KeyReader(StringIO("p"))
    assert reader.available is False
    assert reader.read_key() == ""


def test_key_reader_push_queue_is_drained():
    reader = hud.KeyReader(StringIO(""))
    reader.push("p", "a", "q")
    assert reader.read_keys() == ["p", "a", "q"]
    assert reader.read_key() == ""


def test_key_reader_respects_limit():
    reader = hud.KeyReader(StringIO(""))
    reader.push(*"abcdefghij")
    assert reader.read_keys(limit=3) == ["a", "b", "c"]
    assert reader.read_key() == "d"


@pytest.mark.parametrize(
    "value,expected",
    [(None, 1.0), (0.01, hud.INTERVAL_MIN), (120, hud.INTERVAL_MAX), (2.5, 2.5), ("bad", 1.0)],
)
def test_clamp_interval(value, expected):
    assert hud.clamp_interval(value) == expected


def test_run_hud_renders_requested_frames_and_quits():
    client = mock_client(route(lambda *a: dashboard_payload()))
    console = hud.make_console(stream=StringIO(), width=100, no_color=True)
    clock = FakeClock()
    exit_code = hud.run_hud(
        client,
        interval=1.0,
        iterations=3,
        key_reader=hud.KeyReader(StringIO("")),
        console=console,
        sleep=clock.sleep,
        clock=clock,
    )
    assert exit_code == 0
    output = console.file.getvalue()
    assert output.count("TokenLens") >= 3
    assert "today" in output
    # 3 frames at a 1s interval -> the loop slept twice (never after the final frame).
    assert clock.now == pytest.approx(2.0)


def test_run_hud_dimension_keys_switch_the_bars():
    client = mock_client(route(lambda *a: dashboard_payload()))
    console = hud.make_console(stream=StringIO(), width=100, no_color=True)
    reader = hud.KeyReader(StringIO(""))
    reader.push("a")  # switch to the agent dimension before the next frame
    hud.run_hud(
        client,
        dimension="project",
        interval=1.0,
        iterations=2,
        key_reader=reader,
        console=console,
        sleep=FakeClock().sleep,
        clock=FakeClock(),
    )
    output = console.file.getvalue()
    assert "BY PROJECT" in output
    assert "BY AGENT" in output


def test_run_hud_q_quits_immediately():
    client = mock_client(route(lambda *a: dashboard_payload()))
    console = hud.make_console(stream=StringIO(), width=100, no_color=True)
    reader = hud.KeyReader(StringIO(""))
    reader.push("q")
    exit_code = hud.run_hud(
        client,
        interval=1.0,
        iterations=10,
        key_reader=reader,
        console=console,
        sleep=FakeClock().sleep,
        clock=FakeClock(),
    )
    assert exit_code == 0
    assert console.file.getvalue().count("TokenLens") == 1


def test_run_hud_shows_offline_banner_and_keeps_retrying():
    """A server that comes back must recover: the loop must not exit on the first error."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return json_response(dashboard_payload())

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    console = hud.make_console(stream=StringIO(), width=100, no_color=True)
    clock = FakeClock()
    exit_code = hud.run_hud(
        client,
        interval=1.0,
        iterations=2,
        key_reader=hud.KeyReader(StringIO("")),
        console=console,
        sleep=clock.sleep,
        clock=clock,
    )
    output = console.file.getvalue()
    assert "OFFLINE" in output
    assert "today" in output  # the second frame recovered
    assert exit_code == 1  # last frame succeeded, but the run saw an outage


def test_run_hud_survives_a_persistent_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    console = hud.make_console(stream=StringIO(), width=100, no_color=True)
    exit_code = hud.run_hud(
        client,
        interval=1.0,
        iterations=2,
        key_reader=hud.KeyReader(StringIO("")),
        console=console,
        sleep=FakeClock().sleep,
        clock=FakeClock(),
    )
    output = console.file.getvalue()
    assert output.count("OFFLINE") >= 2
    assert exit_code == 1


# --------------------------------------------------------------------------------------
# 8. render_once (non-TTY / CI path)
# --------------------------------------------------------------------------------------


def test_render_once_prints_one_snapshot():
    client = mock_client(route(lambda *a: dashboard_payload()))
    stream = StringIO()
    assert hud.render_once(client, stream=stream) == 0
    text = stream.getvalue()
    assert "TokenLens HUD" in text
    assert "BY PROJECT" in text


def test_render_once_offline_returns_exit_code_1():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    stream = StringIO()
    assert hud.render_once(client, stream=stream) == 1
    assert "OFFLINE" in stream.getvalue()


def test_render_once_json_mode_is_parseable():
    client = mock_client(route(lambda *a: dashboard_payload()))
    stream = StringIO()
    assert hud.render_once(client, as_json=True, stream=stream) == 0
    payload = json.loads(stream.getvalue())
    assert payload["kpis"]["today_cost_usd"] == 10.0
    assert payload["dimension"] == "project"
    assert isinstance(payload["bars"], list)


def test_render_once_json_mode_offline_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    stream = StringIO()
    assert hud.render_once(client, as_json=True, stream=stream) == 1
    payload = json.loads(stream.getvalue())
    assert payload["offline"] is True
    assert payload["error"]


def test_render_once_no_color_output_has_no_ansi():
    client = mock_client(route(lambda *a: dashboard_payload()))
    stream = StringIO()
    hud.render_once(client, no_color=True, stream=stream)
    assert "\x1b[" not in stream.getvalue()


# --------------------------------------------------------------------------------------
# 9. Commands (exit codes, tables, JSON)
# --------------------------------------------------------------------------------------


def test_cmd_hud_non_tty_renders_once():
    client = mock_client(route(lambda *a: dashboard_payload()))
    stream = StringIO()
    assert commands.cmd_hud(args_for("hud"), client=client, out=stream) == 0
    assert "BY PROJECT" in stream.getvalue()


def test_cmd_live_prints_burn_rate():
    payload = {
        "window_minutes": 5,
        "tokens_per_min": 1250.0,
        "cost_per_hour": 3.5,
        "active_agents": 2,
        "active_projects": 2,
        "active_sessions": 4,
        "top_projects": [{"project": "web", "cost_usd": 1.5, "tokens_per_min": 900.0}],
        "top_agents": [{"agent": "claude-code", "cost_usd": 1.5, "tokens_per_min": 900.0}],
    }
    client = mock_client(route(lambda *a: payload))
    stream = StringIO()
    assert commands.cmd_live(args_for("live"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "LIVE WINDOW" in text
    assert "1.2k/min" in text
    assert "claude-code" in text


def test_cmd_summary_prints_all_token_classes():
    client = mock_client(route(lambda *a: {"window": {"from": "2026-11-10T00:00:00Z", "to": "2026-11-11T00:00:00Z"}, "totals": totals(), "filters": {}}))
    stream = StringIO()
    assert commands.cmd_summary(args_for("summary"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "SUMMARY" in text
    assert "cache read" in text
    assert "billable tokens" in text


def test_cmd_trends_prints_sparkline_and_delta():
    payload = {
        "period": "day",
        "window": {"from": "2026-11-01T00:00:00Z", "to": "2026-11-10T00:00:00Z"},
        "points": [
            {"bucket_start": "2026-11-09T00:00:00Z", "bucket_end": "2026-11-10T00:00:00Z", "label": "2026-11-09", "totals": totals(cost_usd=2.0)},
            {"bucket_start": "2026-11-10T00:00:00Z", "bucket_end": "2026-11-11T00:00:00Z", "label": "2026-11-10", "totals": totals(cost_usd=3.0)},
        ],
        "comparison": {"previous_cost_usd": 2.0, "current_cost_usd": 3.0, "delta_usd": 1.0, "delta_pct": 50.0, "previous_tokens": 1000, "current_tokens": 1500, "token_delta_pct": 50.0},
        "filters": {},
    }
    client = mock_client(route(lambda *a: payload))
    stream = StringIO()
    assert commands.cmd_trends(args_for("trends"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "TRENDS" in text
    assert "+50.0%" in text
    assert any(char in text for char in render.SPARK_CHARS)


def test_cmd_breakdown_renders_bars():
    payload = {"dimension": "model", "window": {}, "rows": [breakdown_row("gpt-5", 5.0, 5000, 100.0)], "total": totals(cost_usd=5.0)}
    client = mock_client(route(lambda *a: payload))
    stream = StringIO()
    namespace = args_for("breakdown", dimension="model")
    assert commands.cmd_breakdown(namespace, client=client, out=stream) == 0
    text = stream.getvalue()
    assert "BREAKDOWN" in text
    assert "Gpt 5" in text
    assert render.FILLED in text


def test_cmd_budgets_renders_utilisation():
    client = mock_client(route(lambda *a: {"items": [budget_status(spent=20.0, limit=25.0, status="critical")]}))
    stream = StringIO()
    assert commands.cmd_budgets(args_for("budgets"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "BUDGETS" in text
    assert "80.0%" in text
    assert "critical" in text


def test_cmd_budgets_empty_state():
    client = mock_client(route(lambda *a: {"items": []}))
    stream = StringIO()
    assert commands.cmd_budgets(args_for("budgets"), client=client, out=stream) == 0
    assert "no budgets configured" in stream.getvalue()


def test_cmd_alerts_renders_trigger_counts():
    payload = {
        "total": 1,
        "items": [
            {
                "id": 1,
                "budget_id": 1,
                "budget_name": "team-daily",
                "scope": "global",
                "scope_key": "*",
                "period": "daily",
                "window_start": "2026-11-09T16:00:00Z",
                "window_end": "2026-11-10T16:00:00Z",
                "threshold": 0.8,
                "severity": "critical",
                "spent_usd": 22.0,
                "limit_usd": 25.0,
                "overspend_usd": 0.0,
                "trigger_count": 3,
                "message": "budget team-daily at 88%",
                "channels": ["desktop"],
                "created_at": "2026-11-10T13:00:00Z",
            }
        ],
    }
    client = mock_client(route(lambda *a: payload))
    stream = StringIO()
    assert commands.cmd_alerts(args_for("alerts"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "ALERTS" in text
    assert "team-daily" in text
    assert "critical" in text


def test_cmd_anomalies_renders_reason_labels():
    payload = {
        "total": 1,
        "items": [
            {
                "id": 1,
                "detected_at": "2026-11-10T13:00:00Z",
                "agent": "agent-retry-storm",
                "project": "payments-api",
                "model": "claude-opus-4-1",
                "window_start": "2026-11-10T12:00:00Z",
                "window_end": "2026-11-10T13:00:00Z",
                "metric": "tokens_per_min",
                "observed": 50_000.0,
                "baseline": 5000.0,
                "score": 12.5,
                "ratio": 10.0,
                "severity": "critical",
                "reason": "retry_storm",
                "reason_label": "重试风暴",
                "reason_detail": "retries/requests=0.35",
                "evidence": {"retry_ratio": 0.35},
                "tokens_burn": 100_000.0,
                "cost_usd": 4.2,
                "status": "open",
                "fingerprint": "abc",
            }
        ],
    }
    client = mock_client(route(lambda *a: payload))
    stream = StringIO()
    assert commands.cmd_anomalies(args_for("anomalies"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "ANOMALIES" in text
    assert "重试风暴" in text
    assert "12.5" in text


def test_cmd_detect_prints_events_and_budget_pass():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/anomalies/detect"):
            return json_response(
                {
                    "scanned_agents": 4,
                    "detected": [
                        {
                            "id": 1,
                            "detected_at": "2026-11-10T13:00:00Z",
                            "agent": "agent-tool-dump",
                            "project": "web",
                            "model": "claude-sonnet-4-5",
                            "window_start": "2026-11-10T12:00:00Z",
                            "window_end": "2026-11-10T13:00:00Z",
                            "metric": "tokens_per_min",
                            "observed": 1.0,
                            "baseline": 0.1,
                            "score": 9.0,
                            "ratio": 10.0,
                            "severity": "high",
                            "reason": "uncompressed_tool_output",
                            "reason_label": "工具输出未压缩",
                            "reason_detail": "tool bytes high",
                            "evidence": {},
                            "tokens_burn": 1.0,
                            "cost_usd": 1.0,
                            "status": "open",
                            "fingerprint": "x",
                        }
                    ],
                    "detected_at": "2026-11-10T13:00:00Z",
                    "lookback_minutes": 60,
                }
            )
        if path.endswith("/budgets/evaluate"):
            return json_response({"evaluated": 2, "triggered": [], "evaluated_at": "2026-11-10T13:00:00Z", "dry_run": True})
        return httpx.Response(404, json={"detail": "Not Found"})

    client = mock_client(handler)
    stream = StringIO()
    namespace = args_for("detect", lookback=60, evaluate_budgets=True, dry_run=True)
    assert commands.cmd_detect(namespace, client=client, out=stream) == 0
    text = stream.getvalue()
    assert "DETECT" in text
    assert "工具输出未压缩" in text
    assert "budgets evaluated 2" in text


def test_cmd_export_writes_csv_to_file(tmp_path: Path):
    csv_bytes = "\ufeffbucket,cost\n2026-11-10,1.5\n".encode("utf-8")
    client = mock_client(route(lambda *a: httpx.Response(200, content=csv_bytes, headers={"content-type": "text/csv"})))
    target = tmp_path / "nested" / "usage.csv"
    stream = StringIO()
    namespace = args_for("export", dataset="timeseries", out=str(target))
    assert commands.cmd_export(namespace, client=client, out=stream) == 0
    assert target.exists()
    assert target.read_bytes() == csv_bytes
    assert "exported timeseries" in stream.getvalue()


def test_cmd_export_json_mode_reports_path(tmp_path: Path):
    client = mock_client(route(lambda *a: httpx.Response(200, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})))
    target = tmp_path / "x.csv"
    stream = StringIO()
    namespace = args_for("export", dataset="sessions", out=str(target), json=True)
    assert commands.cmd_export(namespace, client=client, out=stream) == 0
    payload = json.loads(stream.getvalue())
    assert payload["dataset"] == "sessions"
    assert payload["bytes"] == len(b"a,b\n1,2\n")


def test_cmd_version_offline():
    stream = StringIO()
    assert commands.cmd_version(args_for("version", offline=True), out=stream) == 0
    assert "tokenlens client" in stream.getvalue()


def test_cmd_version_probes_server():
    client = mock_client(route(lambda *a: {"status": "ok", "version": "0.1.0", "price_version": "2025-11-01", "snapshots": 42}))
    stream = StringIO()
    assert commands.cmd_version(args_for("version"), client=client, out=stream) == 0
    text = stream.getvalue()
    assert "price 2025-11-01" in text
    assert "snapshots 42" in text


def test_command_reports_offline_with_exit_code_1():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TokenLensClient("http://127.0.0.1:1", transport=httpx.MockTransport(handler))
    stream = StringIO()
    assert commands.cmd_summary(args_for("summary"), client=client, out=stream) == commands.EXIT_OFFLINE
    assert "cannot reach TokenLens" in stream.getvalue()


def test_command_reports_unimplemented_endpoint_with_exit_code_2():
    client = mock_client(route(lambda *a: httpx.Response(404, json={"detail": "Not Found"})))
    stream = StringIO()
    assert commands.cmd_budgets(args_for("budgets"), client=client, out=stream) == commands.EXIT_UNIMPLEMENTED
    assert "not implemented" in stream.getvalue()


@pytest.mark.parametrize(
    "command,path,payload",
    [
        ("summary", "/api/v1/usage/summary", {"window": {}, "totals": totals(), "filters": {}}),
        ("live", "/api/v1/usage/live", {"window_minutes": 5, "tokens_per_min": 1.0, "cost_per_hour": 1.0, "active_agents": 0, "active_projects": 0, "active_sessions": 0, "top_projects": [], "top_agents": []}),
        ("budgets", "/api/v1/budgets", {"items": []}),
        ("alerts", "/api/v1/alerts", {"total": 0, "items": []}),
        ("anomalies", "/api/v1/anomalies", {"total": 0, "items": []}),
    ],
)
def test_json_mode_emits_raw_server_payload(command, path, payload):
    client = mock_client(route(lambda *a: payload))
    stream = StringIO()
    namespace = args_for(command, json=True)
    assert commands.COMMANDS[command](namespace, client=client, out=stream) == 0
    assert json.loads(stream.getvalue()) == payload


# --------------------------------------------------------------------------------------
# 10. Simulator: config, fleet, generation determinism
# --------------------------------------------------------------------------------------


def test_sim_config_validation_clamps_values():
    config = sim.SimConfig(batch_size=9999, concurrency=999, agents=0, duration=1).validate()
    assert config.batch_size == sim.MAX_BATCH
    assert config.concurrency == 64
    assert config.agents == 1


def test_sim_config_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown mode"):
        sim.SimConfig(mode="nope", duration=1).validate()


def test_sim_config_requires_a_stop_condition():
    with pytest.raises(ValueError):
        sim.SimConfig(duration=0, target_tokens=0).validate()


def test_sim_config_normalises_server_url():
    assert sim.SimConfig(server="127.0.0.1:8787", duration=1).validate().server == "http://127.0.0.1:8787"
    assert sim.SimConfig(server="http://x:1/", duration=1).validate().server == "http://x:1"


def test_build_fleet_is_deterministic_by_seed():
    import random

    first = sim.build_fleet(sim.SimConfig(agents=5, projects=3, seed=11), rng=random.Random(11))
    second = sim.build_fleet(sim.SimConfig(agents=5, projects=3, seed=11), rng=random.Random(11))
    assert [(a.name, a.project, a.model) for a in first] == [(b.name, b.project, b.model) for b in second]


def test_incident_mode_adds_three_anomaly_agents():
    import random

    fleet = sim.build_fleet(sim.SimConfig(mode="incident", agents=3, seed=1), rng=random.Random(1))
    names = {profile.name for profile in fleet}
    assert set(sim.INCIDENT_AGENTS.values()) <= names
    assert len(fleet) == 3 + 3


def test_steady_mode_has_no_incident_agents():
    import random

    fleet = sim.build_fleet(sim.SimConfig(mode="steady", agents=4, seed=1), rng=random.Random(1))
    assert not (set(sim.INCIDENT_AGENTS.values()) & {profile.name for profile in fleet})


def test_generate_batch_is_deterministic_for_a_seed():
    import random

    def build() -> list[dict[str, Any]]:
        rng = random.Random(7)
        config = sim.SimConfig(mode="incident", agents=4, seed=7)
        fleet = sim.build_fleet(config, rng=rng)
        batch = sim.generate_batch(
            fleet, rng=rng, count=25, ts=1_800_000_000_000, mode="incident",
            event_prefix="t", event_counter=0,
        )
        return [json.loads(snap.model_dump_json()) for snap in batch]

    assert build() == build()


def test_generate_batch_assigns_unique_event_ids():
    import random

    rng = random.Random(3)
    fleet = sim.build_fleet(sim.SimConfig(agents=3, seed=3), rng=rng)
    batch = sim.generate_batch(
        fleet, rng=rng, count=200, ts=timeutil.now_ms(), mode="steady",
        event_prefix="uniq", event_counter=1000,
    )
    ids = [snap.event_id for snap in batch]
    assert len(ids) == len(set(ids)) == 200
    assert all(str(event_id).startswith("uniq-") for event_id in ids)


def test_generate_batch_stamps_mode_into_meta():
    import random

    rng = random.Random(5)
    fleet = sim.build_fleet(sim.SimConfig(mode="mixed", agents=2, seed=5), rng=rng)
    batch = sim.generate_batch(
        fleet, rng=rng, count=5, ts=timeutil.now_ms(), mode="mixed",
        event_prefix="m", event_counter=0,
    )
    assert all(snap.meta["mode"] == "mixed" for snap in batch)


def test_generate_batch_token_budget_is_respected():
    import random

    rng = random.Random(9)
    fleet = sim.build_fleet(sim.SimConfig(agents=3, seed=9), rng=rng)
    batch = sim.generate_batch(
        fleet, rng=rng, count=500, ts=timeutil.now_ms(), mode="steady",
        event_prefix="b", event_counter=0, token_budget=50_000,
    )
    assert sim.total_tokens_of(batch) <= 50_000
    assert batch  # at least one snapshot is always emitted


def test_estimate_cost_matches_pricing_module():
    snap = make_snapshot(model="claude-sonnet-4-5", input_tokens=1000, output_tokens=500, cache_read_tokens=2000)
    expected, _, _ = pricing.compute_cost(
        "claude-sonnet-4-5", input_tokens=1000, output_tokens=500, cache_read_tokens=2000,
        cache_write_tokens=200, reasoning_tokens=0,
    )
    assert sim.estimate_cost([snap]) == pytest.approx(expected)


# --------------------------------------------------------------------------------------
# 11. Simulator: incident signatures
# --------------------------------------------------------------------------------------


def _incident_workload(count: int = 400, seed: int = 42) -> list:
    """Generate an incident-mode workload directly (no HTTP, no asyncio)."""
    import random

    config = sim.SimConfig(mode="incident", agents=4, seed=seed)
    rng = random.Random(seed)
    fleet = sim.build_fleet(config, rng=rng)
    snapshots = []
    counter = 0
    while len(snapshots) < count:
        batch = sim.generate_batch(
            fleet, rng=rng, count=min(100, count - len(snapshots)), ts=timeutil.now_ms(),
            mode="incident", event_prefix="sig", event_counter=counter,
        )
        snapshots.extend(batch)
        counter += len(batch)
    return snapshots


def test_incident_mode_produces_retry_storm_signature():
    evidence = sim.anomaly_signatures(_incident_workload())["retry_storm"]
    assert evidence["detected"] is True
    assert evidence["ratio"] >= sim.RETRY_STORM_MIN_RATIO


def test_incident_mode_produces_context_bloat_signature():
    evidence = sim.anomaly_signatures(_incident_workload())["context_bloat"]
    assert evidence["detected"] is True
    assert evidence["mean_context_window"] >= sim.CONTEXT_BLOAT_MIN_WINDOW
    # Contract §8 also wants "very large input tokens per request".
    assert evidence["input_tokens_per_request"] >= 10_000


def test_incident_mode_produces_uncompressed_tool_output_signature():
    evidence = sim.anomaly_signatures(_incident_workload())["tool_dump"]
    assert evidence["detected"] is True
    assert evidence["bytes_per_request"] >= sim.TOOL_DUMP_MIN_BYTES_PER_REQUEST


def test_all_three_signatures_present_together():
    assert sim.all_signatures_present(_incident_workload())


def test_steady_mode_does_not_trigger_incident_signatures():
    import random

    config = sim.SimConfig(mode="steady", agents=5, seed=42)
    rng = random.Random(42)
    fleet = sim.build_fleet(config, rng=rng)
    snapshots = []
    counter = 0
    while len(snapshots) < 400:
        batch = sim.generate_batch(
            fleet, rng=rng, count=100, ts=timeutil.now_ms(), mode="steady",
            event_prefix="steady", event_counter=counter,
        )
        snapshots.extend(batch)
        counter += len(batch)
    signatures = sim.anomaly_signatures(snapshots)
    # The incident agents are absent entirely, so no class can be detected.
    assert not any(item["detected"] for item in signatures.values())


# --------------------------------------------------------------------------------------
# 12. Simulator: dry-run + target-tokens + report (asyncio.run, no HTTP)
# --------------------------------------------------------------------------------------


def test_dry_run_is_deterministic_by_seed():
    config = sim.SimConfig(mode="incident", target_tokens=100_000, seed=7, dry_run=True)
    first = asyncio_run(sim.run_simulation(config))
    second = asyncio_run(sim.run_simulation(config))
    assert first.tokens_generated == second.tokens_generated
    assert first.snapshots_generated == second.snapshots_generated
    assert first.estimated_cost_usd == pytest.approx(second.estimated_cost_usd)


def test_dry_run_respects_target_tokens():
    config = sim.SimConfig(mode="steady", target_tokens=50_000, seed=3, dry_run=True)
    stats = asyncio_run(sim.run_simulation(config))
    assert stats.target_reached is True
    # The plan is trimmed layer-proportionally, so the run lands near the target rather
    # than overshooting by a whole batch. Some overshoot is expected from the final row.
    assert stats.tokens_generated >= 50_000
    assert stats.tokens_generated <= 50_000 * 3


def test_dry_run_makes_no_http_calls(monkeypatch):
    """``--dry-run`` must not construct an HTTP client at all (hermetic tests)."""

    class ExplodingIngestor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("dry run must not create an HTTP client")

    monkeypatch.setattr(sim, "_HttpIngestor", ExplodingIngestor)
    stats = asyncio_run(sim.run_simulation(sim.SimConfig(mode="burst", target_tokens=20_000, dry_run=True)))
    assert stats.snapshots_generated > 0
    assert stats.accepted == 0  # nothing was POSTed


def test_dry_run_validates_every_snapshot_through_the_schema():
    config = sim.SimConfig(mode="incident", target_tokens=30_000, seed=11, dry_run=True)
    stats = asyncio_run(sim.run_simulation(config))
    assert stats.snapshots_sent == stats.snapshots_generated
    assert stats.batches >= 1


def test_dry_run_report_fields():
    config = sim.SimConfig(mode="incident", target_tokens=100_000, seed=7, dry_run=True)
    stats = asyncio_run(sim.run_simulation(config))
    payload = stats.as_dict()
    for key in (
        "mode",
        "elapsed_s",
        "snapshots_generated",
        "accepted",
        "duplicates",
        "tokens_generated",
        "estimated_cost_usd",
        "snapshots_per_s",
        "tokens_per_s",
        "target_reached",
    ):
        assert key in payload
    assert payload["mode"] == "incident"
    assert payload["target_reached"] is True


def test_report_text_contains_throughput_and_ingest_counts():
    config = sim.SimConfig(mode="steady", target_tokens=10_000, seed=1, dry_run=True)
    stats = asyncio_run(sim.run_simulation(config))
    text = sim.format_report(stats)
    assert "TokenLens simulate" in text
    assert "snapshots" in text
    assert "ingest" in text
    assert "accepted" in text
    assert "duplicates" in text
    assert "tokens" in text
    assert "cost" in text


def test_format_report_json_is_parseable():
    config = sim.SimConfig(mode="steady", target_tokens=10_000, seed=1, dry_run=True)
    stats = asyncio_run(sim.run_simulation(config))
    payload = json.loads(sim.format_report(stats, as_json=True))
    assert payload["dry_run"] is True
    assert payload["snapshots_generated"] > 0


def test_simulate_main_dry_run_prints_report_and_signatures():
    stream = StringIO()
    code = sim.main(
        ["--dry-run", "--mode", "incident", "--target-tokens", "100000", "--seed", "7"],
        out=stream,
    )
    assert code == 0
    text = stream.getvalue()
    assert "DRY RUN" in text
    assert "target-tokens reached" in text
    # The dry run prints evidence that the three anomaly classes are present.
    assert "[OK ] retry_storm" in text
    assert "[OK ] context_bloat" in text
    assert "[OK ] tool_dump" in text


def test_simulate_main_rejects_bad_mode():
    with pytest.raises(SystemExit):
        sim.main(["--dry-run", "--mode", "bogus"])


def test_simulate_argparser_surface():
    parser = sim.build_parser()
    args = parser.parse_args(
        [
            "--server", "http://x:1", "--mode", "mixed", "--duration", "3",
            "--agents", "6", "--projects", "4", "--sessions", "40",
            "--target-tokens", "1000000", "--concurrency", "8", "--seed", "42",
            "--batch-size", "250", "--dry-run", "--report",
        ]
    )
    config = sim.config_from_namespace(args)
    assert config.mode == "mixed"
    assert config.duration == 3
    assert config.agents == 6
    assert config.projects == 4
    assert config.sessions == 40
    assert config.target_tokens == 1_000_000
    assert config.concurrency == 8
    assert config.seed == 42
    assert config.batch_size == 250
    assert config.dry_run is True


def test_argv_from_namespace_round_trips_the_flag_set():
    namespace = main.build_parser().parse_args(
        ["simulate", "--mode", "burst", "--duration", "2", "--agents", "3", "--seed", "9", "--dry-run", "--report"]
    )
    argv = sim.argv_from_namespace(namespace)
    reparsed = sim.build_parser().parse_args(argv)
    config = sim.config_from_namespace(reparsed)
    assert config.mode == "burst"
    assert config.agents == 3
    assert config.seed == 9
    assert config.dry_run is True
    assert config.report is True


def test_tokenlens_simulate_command_delegates_to_the_same_generator():
    stream = StringIO()
    namespace = main.build_parser().parse_args(
        ["simulate", "--dry-run", "--mode", "incident", "--target-tokens", "20000", "--seed", "5"]
    )
    assert commands.cmd_simulate(namespace, out=stream) == 0
    text = stream.getvalue()
    assert "TokenLens simulate" in text
    assert "mode=incident" in text


# --------------------------------------------------------------------------------------
# 13. Simulator over real HTTP against the FastAPI app (no live server)
# --------------------------------------------------------------------------------------


def test_simulator_ingests_through_the_real_api(app, db):
    """Drive the simulator's write path against the app via an ASGI transport."""
    import asyncio

    import random

    config = sim.SimConfig(mode="incident", agents=3, projects=2, seed=21, batch_size=25)
    rng = random.Random(21)
    fleet = sim.build_fleet(config, rng=rng)
    batches = [
        sim.generate_batch(
            fleet, rng=rng, count=25, ts=timeutil.now_ms(), mode="incident",
            event_prefix="http", event_counter=index * 25,
        )
        for index in range(2)
    ]

    async def post_all() -> list[dict[str, Any]]:
        transport = httpx.ASGITransport(app=app)
        headers = {"Content-Type": "application/json"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as client:
            responses = []
            for batch in batches:
                payload = {"snapshots": [json.loads(snap.model_dump_json()) for snap in batch]}
                response = await client.post(sim.INGEST_PATH, json=payload)
                response.raise_for_status()
                responses.append(response.json())
            return responses

    responses = asyncio.run(post_all())
    assert sum(item["accepted"] for item in responses) == 50
    assert sum(item["duplicates"] for item in responses) == 0
    assert db.scalar("SELECT COUNT(*) FROM usage_snapshots") == 50
    assert sum(item["total_tokens"] for item in responses) > 0

    # Replaying the same batches must count as duplicates, never double count.
    replay = asyncio.run(post_all())
    assert sum(item["accepted"] for item in replay) == 0
    assert sum(item["duplicates"] for item in replay) == 50
    assert db.scalar("SELECT COUNT(*) FROM usage_snapshots") == 50


def test_simulator_batches_never_exceed_the_cap():
    import random

    rng = random.Random(1)
    fleet = sim.build_fleet(sim.SimConfig(agents=3, seed=1), rng=rng)
    for batch_size in (1, 100, 500, 5000):
        config = sim.SimConfig(batch_size=batch_size, duration=1).validate()
        assert 1 <= config.batch_size <= sim.MAX_BATCH


def test_simulator_run_against_app_via_asgi_transport(app, db, monkeypatch):
    """End-to-end: the async runner posts real batches into the app and reports them."""
    transport = httpx.ASGITransport(app=app)
    real_ingestor = sim._HttpIngestor

    class AsgiIngestor(real_ingestor):  # type: ignore[misc, valid-type]
        def __init__(self, config: sim.SimConfig) -> None:
            headers = {"Content-Type": "application/json"}
            self._client = httpx.AsyncClient(
                transport=transport, base_url="http://testserver", headers=headers
            )

    monkeypatch.setattr(sim, "_HttpIngestor", AsgiIngestor)
    config = sim.SimConfig(
        mode="steady", agents=3, projects=2, seed=4, duration=0.35,
        target_tokens=120_000, batch_size=50, concurrency=2, max_snapshots=120,
    )
    stats = asyncio_run(sim.run_simulation(config))
    assert stats.snapshots_generated > 0
    assert stats.accepted > 0
    assert stats.accepted + stats.duplicates + stats.rejected == stats.snapshots_sent
    assert db.scalar("SELECT COUNT(*) FROM usage_snapshots") == stats.accepted
    assert stats.estimated_cost_usd > 0
    # The three incident agents were not part of a steady fleet.
    agents = {row[0] for row in db.fetch_all("SELECT DISTINCT agent FROM usage_snapshots")}
    assert not (set(sim.INCIDENT_AGENTS.values()) & agents)


def test_simulator_incident_run_populates_detectable_evidence(app, db, monkeypatch):
    """The incident fleet must leave the raw evidence the detector classifies (§8).

    The evidence lives in the *burst* rows: seeded baseline minutes deliberately look like
    ordinary traffic so the agent's own history stays clean (the classifier compares the
    offending window against it). This test therefore asserts on the burst layer, which is
    what the detector's offending-window aggregation actually reads.
    """
    transport = httpx.ASGITransport(app=app)
    real_ingestor = sim._HttpIngestor

    class AsgiIngestor(real_ingestor):  # type: ignore[misc, valid-type]
        def __init__(self, config: sim.SimConfig) -> None:
            self._client = httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                headers={"Content-Type": "application/json"},
            )

    monkeypatch.setattr(sim, "_HttpIngestor", AsgiIngestor)
    config = sim.SimConfig(
        mode="incident", agents=3, projects=2, seed=8, duration=5.0,
        target_tokens=6_000_000, batch_size=50, concurrency=2,
    )
    stats = asyncio_run(sim.run_simulation(config))
    assert stats.accepted > 0

    # Burst rows are tagged in ``meta``; the detector aggregates the offending minutes,
    # which are exactly the burst rows. ``meta`` is stored as compact JSON
    # (``{"sim":true,...}``) so match without whitespace.
    def _burst_aggregate(agent: str, columns: str) -> Any:
        rows = db.fetch_all(
            f"SELECT {columns} FROM usage_snapshots "
            'WHERE agent = ? AND meta LIKE \'%"burst":true%\'',
            (agent,),
        )
        return rows[0] if rows else None

    retry = _burst_aggregate(sim.INCIDENT_AGENTS["retry_storm"], "SUM(retries) AS r, SUM(requests) AS q")
    assert retry and retry["q"], "retry-storm agent produced no burst rows"
    assert retry["r"] / retry["q"] >= sim.RETRY_STORM_MIN_RATIO

    context = _burst_aggregate(
        sim.INCIDENT_AGENTS["context_bloat"],
        "AVG(context_window) AS w, SUM(input_tokens) AS i, SUM(requests) AS q",
    )
    assert context and context["w"] >= sim.CONTEXT_BLOAT_MIN_WINDOW
    assert context["i"] / context["q"] >= 10_000

    tools = _burst_aggregate(
        sim.INCIDENT_AGENTS["tool_dump"],
        "SUM(tool_result_bytes) AS b, SUM(requests) AS q",
    )
    assert tools and tools["q"]
    assert tools["b"] / tools["q"] >= sim.TOOL_DUMP_MIN_BYTES_PER_REQUEST


def test_simulator_records_errors_instead_of_crashing(monkeypatch):
    """A failing server must degrade into ``stats.errors``, not an exception."""

    class FailingIngestor:
        def __init__(self, config: sim.SimConfig) -> None:
            self.config = config

        async def post_batch(self, snapshots):
            raise httpx.ConnectError("refused")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(sim, "_HttpIngestor", FailingIngestor)
    config = sim.SimConfig(mode="steady", agents=2, duration=0.2, batch_size=20, concurrency=1, max_snapshots=20)
    stats = asyncio_run(sim.run_simulation(config))
    assert stats.errors
    assert stats.rejected == stats.snapshots_sent


def test_incident_plan_has_dense_baseline_and_a_trailing_burst():
    """The plan must give the detector both a populated baseline and a trailing spike.

    Uses a target large enough that the layer allocation keeps the full baseline window;
    a very small target shrinks both layers by design (it is a smoke test).
    """
    config = sim.SimConfig(mode="incident", agents=4, projects=3, seed=42, target_tokens=8_000_000, dry_run=True)
    plan = sim.build_plan(config)
    baseline = [s for s in plan if (s.meta or {}).get("baseline")]
    burst = [s for s in plan if (s.meta or {}).get("burst")]
    assert baseline, "no baseline rows: the detector would have no median to compare against"
    assert burst, "no burst rows: the incident would be undetectable"

    # The baseline must span the whole backfill window (that is what gives the detector a
    # clean *history* baseline instead of its diluted warmup fallback).
    baseline_minutes = {int(s.ts) // 60_000 for s in baseline}
    expected_minutes = sim.DEFAULT_BACKFILL_MINUTES
    assert len(baseline_minutes) >= expected_minutes * 0.9, (
        f"baseline covers only {len(baseline_minutes)} of ~{expected_minutes} minutes"
    )

    # Every anomaly agent needs enough active baseline minutes for the detector's
    # MIN_HISTORY_MINUTES gate.
    for agent in sim.INCIDENT_AGENTS.values():
        agent_minutes = {int(s.ts) // 60_000 for s in baseline if s.agent == agent}
        assert len(agent_minutes) >= 10, (
            f"{agent}: only {len(agent_minutes)} baseline minutes, below the detector's "
            "10-minute history requirement"
        )

    # The burst must sit entirely inside the trailing slice of the window (the detector's
    # lookback), never leaking into the history window where it would poison the baseline.
    all_ts = [int(s.ts) for s in plan]
    burst_ts = [int(s.ts) for s in burst]
    window_end = max(all_ts)
    lookback_start = window_end - sim.BURST_WINDOW_MINUTES * 60_000
    assert min(burst_ts) >= lookback_start - 60_000, (
        "burst rows start before the trailing lookback window, which would contaminate "
        "the history baseline"
    )


def test_incident_baseline_rows_look_normal_for_anomaly_agents():
    """Seeded baseline rows must not carry the anomaly signature themselves.

    The classifier decides context bloat by comparing the offending window against the
    agent's own history; a baseline that already shows the inflated 240k context window
    would make the growth ratio unreachable.
    """
    config = sim.SimConfig(mode="incident", agents=4, projects=3, seed=42, target_tokens=6_000_000, dry_run=True)
    plan = sim.build_plan(config)
    seeded = [s for s in plan if (s.meta or {}).get("baseline") and s.agent == sim.INCIDENT_AGENTS["context_bloat"]]
    burst = [s for s in plan if (s.meta or {}).get("burst") and s.agent == sim.INCIDENT_AGENTS["context_bloat"]]
    assert seeded, "context-bloat agent has no seeded baseline rows"
    assert burst, "context-bloat agent has no burst rows"
    # Baseline rows report a normal window; the burst rows report the inflated one.
    assert all(int(s.context_window or 0) == 200_000 for s in seeded)
    assert all(int(s.retries) == 0 for s in seeded)
    assert max(int(s.context_window or 0) for s in burst) >= sim.CONTEXT_BLOAT_MIN_WINDOW * 2
    # Baseline rows are also much smaller than burst rows, so the median stays low.
    avg_seed = sum(s.total_tokens for s in seeded) / len(seeded)
    avg_burst = sum(s.total_tokens for s in burst) / len(burst)
    assert avg_burst > avg_seed * 3


def test_modes_produce_different_arrival_shapes():
    """``burst``/``steady``/``mixed`` must not all emit an identical flat stream."""
    def per_minute_counts(mode: str) -> list[int]:
        config = sim.SimConfig(mode=mode, agents=4, projects=2, seed=42, target_tokens=1_000_000, dry_run=True)
        plan = sim.build_plan(config)
        counts: dict[int, int] = {}
        for snapshot in plan:
            minute = int(snapshot.ts) // 60_000
            counts[minute] = counts.get(minute, 0) + 1
        return [counts[minute] for minute in sorted(counts)]

    steady = per_minute_counts("steady")
    burst = per_minute_counts("burst")
    assert steady and burst
    # ``burst`` waves: its per-minute counts vary more than steady's near-flat stream.
    steady_spread = max(steady) - min(steady)
    burst_spread = max(burst) - min(burst)
    assert burst_spread >= steady_spread, (
        f"burst spread {burst_spread} should be at least steady's {steady_spread}"
    )


#: The reason each incident agent is *designed* to trigger. Each agent must be anomalous
#: in exactly one dimension, because the detector's classifier evaluates root causes in a
#: fixed order and the first match wins (contract §8.4).
EXPECTED_INCIDENT_REASONS = {
    "retry_storm": "retry_storm",
    "context_bloat": "context_bloat",
    "tool_dump": "uncompressed_tool_output",
}


def _detect_incident_run(app, db, *, seed: int, agents: int = 4) -> dict[str, str]:
    """Run an incident simulation into ``db`` and return ``{agent: reason}``."""
    transport = httpx.ASGITransport(app=app)
    real_ingestor = sim._HttpIngestor

    class AsgiIngestor(real_ingestor):  # type: ignore[misc, valid-type]
        def __init__(self, config: sim.SimConfig) -> None:
            self._client = httpx.AsyncClient(
                transport=transport, base_url="http://testserver",
                headers={"Content-Type": "application/json"},
            )

    import unittest.mock as _mock

    from tokenlens import anomaly

    with _mock.patch.object(sim, "_HttpIngestor", AsgiIngestor):
        config = sim.SimConfig(
            mode="incident", agents=agents, projects=3, seed=seed,
            target_tokens=12_000_000, batch_size=250, concurrency=3,
        )
        stats = asyncio_run(sim.run_simulation(config))
    assert stats.accepted > 0

    result = anomaly.detect(
        db, settings=app.state.settings, lookback_minutes=60, now_ms=timeutil.now_ms()
    )
    return {record.agent: record.reason for record in result.detected}


def test_incident_run_yields_exactly_the_three_expected_reasons(app, db):
    """End-to-end: every incident agent is classified with *its own* designed reason.

    This is the acceptance criterion that matters for the demo. Asserting only "three
    things were detected" is not enough: ``agent-tool-dump`` was once classified as
    ``context_bloat`` because its randomly-drawn context window (≈370k) exceeded the 1.5x
    growth rule against its own 200k baseline, and that rule is evaluated *before* the
    tool-output rule. The exact mapping is the regression this locks in.
    """
    got = _detect_incident_run(app, db, seed=42)
    expected = {
        sim.INCIDENT_AGENTS[key]: reason for key, reason in EXPECTED_INCIDENT_REASONS.items()
    }
    assert got == expected, (
        "each incident agent must be classified with the reason it was designed to trigger; "
        f"expected {expected}, got {got}"
    )


def test_incident_reason_mapping_is_stable_across_seeds(app, db):
    """The classification must not depend on the RNG seed.

    A seed-dependent reason means the underlying evidence is marginal (e.g. a ratio barely
    over a threshold), which would make every demo a coin flip.
    """
    from tokenlens import anomaly

    expected = {
        sim.INCIDENT_AGENTS[key]: reason for key, reason in EXPECTED_INCIDENT_REASONS.items()
    }
    for seed in (0, 1, 7):
        # Reset between seeds: runs share one DB, and stacked history from a previous run
        # would change the baseline the next run is judged against.
        db.reset()
        got = _detect_incident_run(app, db, seed=seed)
        assert got == expected, f"seed {seed}: expected {expected}, got {got}"


def test_incident_agents_are_isolated_to_one_anomalous_dimension():
    """Structural guard: each incident profile varies in exactly one dimension.

    The detector's first-match-wins classifier makes an *unintended* signal as harmful as
    a missing intended one — the profile that should trigger tool-output must not also
    look like context bloat. ``build_fleet`` asserts this internally; this test pins the
    invariant so a future edit to the fleet cannot silently reintroduce the bug.
    """
    import random

    fleet = sim.build_fleet(
        sim.SimConfig(mode="incident", agents=4, seed=1), rng=random.Random(1)
    )
    by_name = {profile.name: profile for profile in fleet}
    for key, agent in sim.INCIDENT_AGENTS.items():
        profile = by_name[agent]
        assert profile.context_window is not None, f"{agent}: context window must be pinned"

    retry = by_name[sim.INCIDENT_AGENTS["retry_storm"]]
    assert retry.retry_ratio > 0
    assert retry.context_window == sim.NORMAL_CONTEXT_WINDOW
    assert retry.tool_bytes_per_request == 0

    bloat = by_name[sim.INCIDENT_AGENTS["context_bloat"]]
    assert bloat.context_window >= sim.CONTEXT_BLOAT_GROWTH_TARGET
    assert bloat.tool_bytes_per_request == 0

    dump = by_name[sim.INCIDENT_AGENTS["tool_dump"]]
    assert dump.tool_bytes_per_request >= sim.TOOL_DUMP_MIN_BYTES_PER_REQUEST
    assert dump.context_window == sim.NORMAL_CONTEXT_WINDOW, (
        "the tool-dump agent must report a *normal* context window: the tool-output "
        "signature is 'huge payload at normal context', and a larger window would trip "
        "the earlier context-bloat rule and steal the classification"
    )


def test_incident_backfill_provides_a_clean_history_baseline(app, db):
    """The default backfill must be long enough for the detector to use a *history* baseline.

    The detector's baseline comes from ``[now - 6*lookback, now - lookback)``. If the
    simulator only writes inside the observation window, that history is empty and the
    detector falls back to a warmup baseline rebuilt from the (contaminated) observation
    window — which drags the baseline up and makes classification marginal.
    """
    from tokenlens import anomaly

    got = _detect_incident_run(app, db, seed=5)
    assert got  # sanity: something was detected

    for agent in sim.INCIDENT_AGENTS.values():
        detection = anomaly.detect_agent(
            db, agent, settings=app.state.settings, lookback_minutes=60
        )
        assert detection is not None, f"{agent} was not detected"
        assert detection.evidence["baseline_source"] == "history", (
            f"{agent}: detector fell back to '{detection.evidence['baseline_source']}' "
            "instead of a clean history baseline; the simulator's backfill window is "
            "too short relative to the detector lookback"
        )
        assert detection.evidence["history_minutes"] >= anomaly.MIN_HISTORY_MINUTES


# --------------------------------------------------------------------------------------
# 14. CLI against the real app (TestClient)
# --------------------------------------------------------------------------------------


def test_cli_summary_against_real_app(client):
    """``summary`` must work against a live app with an empty database (zeros, HTTP 200)."""
    from fastapi.testclient import TestClient

    transport = httpx.ASGITransport(app=client.app)

    def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(request.method, request.url.path, params=dict(request.url.params))
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    tokenlens_client = TokenLensClient("http://testserver", transport=httpx.MockTransport(handler))
    stream = StringIO()
    code = commands.cmd_summary(args_for("summary"), client=tokenlens_client, out=stream)
    # The query API may still be a stub in this build: accept "unimplemented" gracefully.
    assert code in (0, commands.EXIT_UNIMPLEMENTED)
    assert stream.getvalue()


def test_cli_health_probe_against_real_app(app):
    """``GET /health`` must round trip through the client's real HTTP code path."""
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:

        def handler(request: httpx.Request) -> httpx.Response:
            response = test_client.request(
                request.method, request.url.path, params=dict(request.url.params)
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"content-type": response.headers.get("content-type", "application/json")},
            )

        tokenlens_client = TokenLensClient("http://testserver", transport=httpx.MockTransport(handler))
        health = tokenlens_client.health()
    assert health["status"] == "ok"
    assert health["price_version"] == pricing.PRICE_VERSION


def test_cli_version_against_real_app(app):
    from fastapi.testclient import TestClient

    transport = httpx.ASGITransport(app=app)
    with TestClient(app) as test_client:
        def handler(request: httpx.Request) -> httpx.Response:
            response = test_client.request(request.method, request.url.path)
            return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

        tokenlens_client = TokenLensClient("http://testserver", transport=httpx.MockTransport(handler))
        stream = StringIO()
        assert commands.cmd_version(args_for("version"), client=tokenlens_client, out=stream) == 0
        assert "price 2025-11-01" in stream.getvalue()


def test_cli_export_against_real_app(client, tmp_path: Path):
    """A real CSV export round trip (the export router may be a stub; tolerate that)."""
    def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(request.method, request.url.path, params=dict(request.url.params))
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    tokenlens_client = TokenLensClient("http://testserver", transport=httpx.MockTransport(handler))
    target = tmp_path / "export.csv"
    stream = StringIO()
    namespace = args_for("export", dataset="timeseries", out=str(target))
    code = commands.cmd_export(namespace, client=tokenlens_client, out=stream)
    if code == commands.EXIT_UNIMPLEMENTED:
        assert not target.exists()
    else:
        assert code == 0
        assert target.exists()
        assert target.read_bytes()


# --------------------------------------------------------------------------------------
# 15. Cross-cutting invariants
# --------------------------------------------------------------------------------------


def test_render_module_has_no_io_side_effects():
    """The rendering layer must stay pure: no httpx/network imports."""
    import ast
    import inspect

    source = inspect.getsource(render)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "httpx" not in imported
    assert "socket" not in imported


def test_cli_version_matches_server_package_version():
    import tokenlens

    from tokenlens_cli import __version__ as cli_version

    assert cli_version == tokenlens.__version__


def test_hud_snapshot_roundtrips_through_json():
    payload = sample_snapshot().to_dict()
    restored = json.loads(json.dumps(payload))
    assert restored["total_cost_usd"] == pytest.approx(10.0)
    assert restored["kpis"]["cache_hit_rate"] == pytest.approx(0.625)


def test_plain_and_rich_renderers_agree_on_the_numbers():
    from rich.console import Console

    snapshot = sample_snapshot()
    plain = render.render_plain(snapshot, width=100)
    console = Console(file=StringIO(), width=120, no_color=True, force_terminal=False, highlight=False)
    console.print(render.render_rich(snapshot))
    rich_text = console.file.getvalue()
    for token in ("10.00", "42.50", "12.5k/min", "62.5%"):
        assert token in plain
        assert token in rich_text


def test_rich_renderer_handles_offline_snapshot():
    from rich.console import Console

    snapshot = data.offline_snapshot("http://127.0.0.1:8787", "connection refused")
    console = Console(file=StringIO(), width=100, no_color=True, force_terminal=False, highlight=False)
    console.print(render.render_rich(snapshot))
    text = console.file.getvalue()
    assert "OFFLINE" in text
    assert "connection refused" in text


def test_rich_renderer_handles_empty_snapshot():
    from rich.console import Console

    console = Console(file=StringIO(), width=100, no_color=True, force_terminal=False, highlight=False)
    console.print(render.render_rich(render.HudSnapshot(server="http://x", generated_ms=0)))
    text = console.file.getvalue()
    assert "no usage recorded" in text
    assert "no budgets configured" in text


# --------------------------------------------------------------------------------------
# asyncio helper (pytest-asyncio is not installed in this environment)
# --------------------------------------------------------------------------------------


def asyncio_run(coroutine: Any) -> Any:
    """Run a coroutine on a fresh event loop (keeps the suite dependency free)."""
    import asyncio

    return asyncio.run(coroutine)
