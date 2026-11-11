#!/usr/bin/env python
"""End-to-end evidence capture for the README / portfolio.

This is the "prove it works" script: it boots the real ASGI app in-process against a
throw-away SQLite file, drives it exclusively through the public HTTP API (exactly like a
real deployment), and writes the artefacts referenced by the README into
``docs/evidence/``:

    ingest_report.json          throughput of the million-token load
    budget_alert_record.json    fired alerts with trigger counts + overspend
    anomaly_cases.json          detected anomalies with root-cause classification
    dashboard_snapshot.json     the aggregated overview payload
    summary.md                  human readable digest of the above
    alerts.csv / anomalies.csv  exported through the CSV endpoint

Usage::

    python tools/capture_evidence.py --target-tokens 1000000 --out docs/evidence
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for candidate in (REPO_ROOT / ".pylibs", REPO_ROOT):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tokenlens import timeutil  # noqa: E402
from tokenlens.app import create_app  # noqa: E402
from tokenlens.config import Settings  # noqa: E402
from tokenlens.db import Database  # noqa: E402
from tokenlens.pricing import PRICE_VERSION, compute_cost, model_count  # noqa: E402
from tokenlens.testing import AGENTS, MODEL_MIX, PROJECTS, generate_workload, make_snapshot  # noqa: E402

AGENT_POOL = [a for a, _ in AGENTS]


def _client(settings: Settings) -> TestClient:
    database = Database(settings.db_path)
    database.initialize()
    app = create_app(settings=settings, db=database)
    return TestClient(app)


def _post_batch(client: TestClient, snapshots: list) -> dict:
    payload = {"snapshots": [s.model_dump(mode="json") for s in snapshots]}
    response = client.post("/api/v1/ingest/snapshots", json=payload)
    response.raise_for_status()
    return response.json()


def phase_throughput(client: TestClient, seed: int, batches: int = 10, per_batch: int = 100) -> dict:
    """Dedicated sustained-ingest benchmark.

    The load phase above stops as soon as the token target is hit, so it may only span a
    couple of round trips. This phase pushes a fixed number of fixed-size batches and
    reports the *server-side* ``ingest_ms`` (pricing + transaction + rollup), which is the
    number that actually characterises the write path.
    """
    rng = random.Random(seed)
    server_ms = 0.0
    accepted = 0
    tokens = 0
    latencies: list[float] = []
    started = time.perf_counter()
    for index in range(batches):
        batch = generate_workload(
            count=per_batch, span_minutes=30.0, seed=rng.randrange(1_000_000)
        )
        wall_start = time.perf_counter()
        body = _post_batch(client, batch)
        latencies.append((time.perf_counter() - wall_start) * 1000.0)
        server_ms += float(body.get("ingest_ms") or 0.0)
        accepted += body["accepted"]
        tokens += body["total_tokens"]
        _ = index
    elapsed = time.perf_counter() - started
    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        index = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
        return round(latencies[index], 2)

    return {
        "batches": batches,
        "snapshots_per_batch": per_batch,
        "snapshots": accepted,
        "tokens": tokens,
        "wall_seconds": round(elapsed, 3),
        "server_ingest_ms_total": round(server_ms, 3),
        "server_snapshots_per_second": round(accepted / (server_ms / 1000.0), 1) if server_ms else 0.0,
        "server_tokens_per_second": round(tokens / (server_ms / 1000.0), 1) if server_ms else 0.0,
        "per_batch_latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": pct(1.0)},
    }


def phase_ingest(
    client: TestClient,
    target_tokens: int,
    seed: int,
    batch_size: int,
    max_batches: int = 12,
) -> dict:
    """Push synthetic traffic until ``target_tokens`` have been ingested.

    The target is split into ``max_batches`` chunks so the measurement covers *sustained*
    traffic (many HTTP round trips, repeated transactions) instead of a single
    serialisation burst. The per-snapshot token estimate comes from the generated mix
    (~12.5k tokens: a coding agent's prompt is dominated by cache reads).
    """
    rng = random.Random(seed)
    started = time.perf_counter()
    accepted = duplicates = rejected = 0
    tokens = 0
    cost = 0.0
    batches = 0
    snapshots_sent = 0
    errors: list[str] = []
    server_ingest_ms = 0.0
    now = timeutil.now_ms()
    tokens_per_snapshot = 12_500
    chunk = max(1, target_tokens // max(1, max_batches))

    while tokens < target_tokens and batches < max_batches:
        remaining = max(1, target_tokens - tokens)
        want = min(chunk, remaining)
        # At least 40 snapshots per round trip: fewer would measure HTTP overhead, not
        # the ingest pipeline, and would understate achievable throughput.
        count = max(40, min(batch_size, round(want / tokens_per_snapshot) or 1))
        batch = generate_workload(
            count=count,
            end_ms=now,
            span_minutes=45.0,
            seed=rng.randrange(1_000_000),
        )
        body = _post_batch(client, batch)
        batches += 1
        snapshots_sent += len(batch)
        accepted += body["accepted"]
        duplicates += body["duplicates"]
        rejected += body["rejected"]
        tokens += body["total_tokens"]
        cost += body["cost_usd"]
        server_ingest_ms += float(body.get("ingest_ms") or 0.0)
        if body.get("errors"):
            errors.extend(str(e) for e in body["errors"][:3])
    elapsed = time.perf_counter() - started

    return {
        "target_tokens": target_tokens,
        "ingested_tokens": tokens,
        "batches": batches,
        "snapshots_sent": snapshots_sent,
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "elapsed_seconds": round(elapsed, 3),
        "snapshots_per_second": round(accepted / elapsed, 1),
        "tokens_per_second": round(tokens / elapsed, 1),
        # Client-observed throughput is bounded by synthetic-data generation on this
        # machine; server ingest_ms isolates the price+persist+rollup work itself.
        "server_ingest_ms_total": round(server_ingest_ms, 3),
        "server_snapshots_per_second": round(
            accepted / (server_ingest_ms / 1000.0), 1
        ) if server_ingest_ms > 0 else 0.0,
        "total_cost_usd": round(cost, 4),
        "price_version": PRICE_VERSION,
        "models_in_catalog": model_count(),
        "errors": errors[:5],
    }


def phase_idempotency(client: TestClient, seed: int) -> dict:
    """Prove replaying a batch does not double count (or double charge)."""
    batch = generate_workload(count=50, span_minutes=10.0, seed=seed)
    first = _post_batch(client, batch)
    second = _post_batch(client, batch)
    return {
        "first": {"accepted": first["accepted"], "duplicates": first["duplicates"]},
        "replay": {"accepted": second["accepted"], "duplicates": second["duplicates"]},
        "no_double_count": second["accepted"] == 0 and second["duplicates"] == 50,
    }


def phase_budgets(client: TestClient) -> dict:
    """Create three budgets and evaluate them twice to demonstrate trigger counting.

    Limits are derived from the *measured* spend so the demo deterministically shows one
    budget in each state (``exceeded`` with a real overspend, ``critical``, ``warning``)
    instead of depending on how much traffic the previous phase happened to generate.
    """
    daily = client.get("/api/v1/usage/summary", params={"from": "1970-01-01", "to": "2999-01-01"}).json()
    del daily  # window check only; real spend is read per scope below
    now = timeutil.now_ms()
    day_start, day_end = timeutil.period_window("daily", now)

    def spend(**filters) -> float:
        params = {"from": timeutil.iso(day_start), "to": timeutil.iso(day_end), **filters}
        response = client.get("/api/v1/usage/summary", params=params)
        response.raise_for_status()
        return float(response.json()["totals"]["cost_usd"])

    total_spend = spend()
    project_spend = spend(project="payments-api")
    agent_spend = spend(agent="claude-code")

    created = []
    for spec in (
        {"name": "全局日预算", "scope": "global", "scope_key": "*", "period": "daily",
         "limit_usd": max(0.5, round(total_spend / 1.45, 4)), "thresholds": [0.5, 0.8, 1.0]},
        {"name": "payments-api 日预算", "scope": "project", "scope_key": "payments-api",
         "period": "daily", "limit_usd": max(0.2, round(project_spend / 0.9, 4)),
         "thresholds": [0.5, 0.8, 1.0]},
        {"name": "claude-code 日预算", "scope": "agent", "scope_key": "claude-code",
         "period": "daily", "limit_usd": max(0.2, round(agent_spend / 0.55, 4)),
         "thresholds": [0.5, 0.8, 1.0]},
    ):
        response = client.post("/api/v1/budgets", json=spec)
        response.raise_for_status()
        created.append(response.json())

    evaluations = []
    for _ in range(2):
        response = client.post("/api/v1/budgets/evaluate")
        response.raise_for_status()
        evaluations.append(response.json())

    alerts = client.get("/api/v1/alerts", params={"limit": 100}).json()
    summary = client.get("/api/v1/alerts/summary").json()
    statuses = client.get("/api/v1/budgets").json()
    return {
        "budgets": created,
        "evaluations": [
            {"evaluated": e["evaluated"], "triggered": len(e["triggered"])} for e in evaluations
        ],
        "statuses": statuses.get("items", []),
        "alerts": alerts.get("items", []),
        "summary": summary,
    }


def phase_anomalies(client: TestClient) -> dict:
    """Inject the three incident signatures next to a calm baseline, then detect."""
    now = timeutil.now_ms()
    baseline = []
    # 3 hours of calm traffic at a low, steady rate for *every* agent that will later
    # receive an incident. The detector deliberately refuses to classify an agent with no
    # measurable baseline (alert-fatigue guard), so a fair demo must establish one.
    for minute in range(180):
        ts = now - minute * 60_000
        baseline.extend(
            [
                make_snapshot(
                    ts=ts,
                    agent="claude-code",
                    project="payments-api",
                    model="claude-sonnet-4-5",
                    session_id="calm-1",
                    input_tokens=600,
                    output_tokens=180,
                    cache_read_tokens=1800,
                    requests=1,
                    tool_result_bytes=4000,
                    event_id=f"calm-a-{minute}",
                ),
                make_snapshot(
                    ts=ts,
                    agent="codex-cli",
                    project="web-dashboard",
                    model="gpt-5-mini",
                    session_id="calm-2",
                    runtime="openai",
                    input_tokens=500,
                    output_tokens=150,
                    cache_read_tokens=1200,
                    requests=1,
                    context_window=200_000,
                    tool_result_bytes=3000,
                    event_id=f"calm-b-{minute}",
                ),
                make_snapshot(
                    ts=ts,
                    agent="deepseek-agent",
                    project="ml-pipeline",
                    model="deepseek-chat",
                    session_id="calm-3",
                    runtime="deepseek",
                    input_tokens=700,
                    output_tokens=200,
                    cache_read_tokens=1500,
                    requests=1,
                    tool_result_bytes=5000,
                    event_id=f"calm-c-{minute}",
                ),
            ]
        )
    _post_batch(client, baseline)

    incidents = []
    # 1) retry storm: many requests, most of them retried, on claude-code
    for i in range(40):
        incidents.append(
            make_snapshot(
                ts=now - i * 1000,
                agent="claude-code",
                project="payments-api",
                model="claude-sonnet-4-5",
                session_id="storm-1",
                input_tokens=9000,
                output_tokens=2400,
                cache_read_tokens=1500,
                requests=12,
                retries=9,
                event_id=f"storm-{i}",
            )
        )
    # 2) context bloat: giant context windows and huge prompts on codex-cli
    for i in range(40):
        incidents.append(
            make_snapshot(
                ts=now - i * 1200,
                agent="codex-cli",
                project="web-dashboard",
                model="gpt-5",
                session_id="bloat-1",
                runtime="openai",
                input_tokens=180_000,
                output_tokens=3000,
                cache_read_tokens=4000,
                context_window=1_000_000,
                requests=2,
                event_id=f"bloat-{i}",
            )
        )
    # 3) uncompressed tool output: multi-hundred-KB tool results on deepseek-agent
    for i in range(40):
        incidents.append(
            make_snapshot(
                ts=now - i * 1500,
                agent="deepseek-agent",
                project="ml-pipeline",
                model="deepseek-chat",
                session_id="tools-1",
                runtime="deepseek",
                input_tokens=60_000,
                output_tokens=1800,
                cache_read_tokens=2000,
                tool_result_bytes=2_400_000,
                requests=1,
                event_id=f"tools-{i}",
            )
        )
    _post_batch(client, incidents)

    detect = client.post("/api/v1/anomalies/detect", params={"lookback_minutes": 60})
    detect.raise_for_status()
    listed = client.get("/api/v1/anomalies", params={"limit": 50}).json()
    summary = client.get("/api/v1/anomalies/summary").json()
    return {
        "detect": detect.json(),
        "anomalies": listed.get("items", []),
        "summary": summary,
        "incident_snapshots": len(incidents),
        "baseline_snapshots": len(baseline),
    }


def phase_dashboard(client: TestClient) -> dict:
    response = client.get("/api/v1/dashboard/overview", params={"days": 7})
    response.raise_for_status()
    return response.json()


def phase_exports(client: TestClient, out_dir: Path) -> dict:
    exports = {}
    for dataset, name in (("alerts", "alerts.csv"), ("anomalies", "anomalies.csv"),
                          ("timeseries", "timeseries.csv"), ("breakdown", "breakdown.csv")):
        response = client.get("/api/v1/export/csv", params={"dataset": dataset})
        response.raise_for_status()
        target = out_dir / name
        target.write_bytes(response.content)
        try:
            shown_path = str(target.resolve().relative_to(REPO_ROOT))
        except ValueError:  # pragma: no cover - out dir outside the repo
            shown_path = str(target)
        exports[dataset] = {
            "path": shown_path,
            "bytes": len(response.content),
            "content_type": response.headers.get("content-type"),
            "disposition": response.headers.get("content-disposition"),
        }
    return exports


def _fmt_usd(value: float | None) -> str:
    return f"${value:,.4f}" if isinstance(value, (int, float)) else "n/a"


def write_summary(out_dir: Path, payload: dict) -> None:
    ingest = payload["ingest"]
    budgets = payload["budgets"]
    anomalies = payload["anomalies"]
    kpis = payload["dashboard"].get("kpis", {})

    lines = [
        "# TokenLens evidence",
        "",
        f"Generated at: {payload['generated_at']}",
        "",
        "## 1. Ingest throughput",
        "",
        "| metric | value |",
        "|---|---|",
        f"| target tokens | {ingest['target_tokens']:,} |",
        f"| ingested tokens | {ingest['ingested_tokens']:,} |",
        f"| snapshots accepted | {ingest['accepted']:,} |",
        f"| batches | {ingest['batches']:,} |",
        f"| elapsed | {ingest['elapsed_seconds']} s |",
        f"| client throughput | {ingest['snapshots_per_second']:,} snapshots/s · {ingest['tokens_per_second']:,} tokens/s |",
        f"| server ingest time | {ingest.get('server_ingest_ms_total', 0)} ms total · "
        f"{ingest.get('server_snapshots_per_second', 0):,} snapshots/s (price+persist+rollup only) |",
        "",
        "### Sustained ingest benchmark (fixed 10 x 100-snapshot batches)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| snapshots | {payload['throughput']['snapshots']:,} |",
        f"| tokens | {payload['throughput']['tokens']:,} |",
        f"| server-side rate | {payload['throughput']['server_snapshots_per_second']:,} snapshots/s · "
        f"{payload['throughput']['server_tokens_per_second']:,} tokens/s |",
        f"| batch latency | p50 {payload['throughput']['per_batch_latency_ms']['p50']} ms · "
        f"p95 {payload['throughput']['per_batch_latency_ms']['p95']} ms · "
        f"max {payload['throughput']['per_batch_latency_ms']['max']} ms |",
        f"| simulated cost | {_fmt_usd(ingest['total_cost_usd'])} |",
        f"| catalog | {ingest['models_in_catalog']} models @ {ingest['price_version']} |",
        "",
        f"Idempotency replay: {json.dumps(payload['idempotency'], ensure_ascii=False)}",
        "",
        "## 2. Budget alerts",
        "",
        "| budget | scope | limit | spent | % | trigger_count | overspend | status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for status in budgets["statuses"]:
        budget = status["budget"]
        lines.append(
            f"| {budget['name']} | {budget['scope']}:{budget['scope_key']} | "
            f"{_fmt_usd(status['limit_usd'])} | {_fmt_usd(status['spent_usd'])} | "
            f"{status['pct'] * 100:.1f}% | {status['trigger_count']} | "
            f"{_fmt_usd(status['overspend_usd'])} | {status['status']} |"
        )
    summary = budgets["summary"]
    lines += [
        "",
        f"Total alerts: **{summary.get('total_alerts', 0)}**, total triggers: "
        f"**{summary.get('total_triggers', 0)}**, total overspend: "
        f"**{_fmt_usd(summary.get('total_overspend_usd', 0))}**",
        "",
        "## 3. Anomaly detection",
        "",
        f"Incident snapshots injected: {anomalies['incident_snapshots']}, "
        f"baseline snapshots: {anomalies['baseline_snapshots']}, "
        f"detected events: {len(anomalies['anomalies'])}",
        "",
        "| agent | reason | severity | observed tok/min | baseline tok/min | score | ratio |",
        "|---|---|---|---|---|---|---|",
    ]
    for event in anomalies["anomalies"]:
        lines.append(
            f"| {event['agent']} | {event['reason_label']} (`{event['reason']}`) | "
            f"{event['severity']} | {event['observed']:,.0f} | {event['baseline']:,.0f} | "
            f"{event['score']:.2f} | {event['ratio']:.2f}x |"
        )
    lines += [
        "",
        "## 4. Dashboard KPIs",
        "",
        "| kpi | value |",
        "|---|---|",
        f"| today cost | {_fmt_usd(kpis.get('today_cost_usd'))} |",
        f"| week cost | {_fmt_usd(kpis.get('week_cost_usd'))} |",
        f"| month cost | {_fmt_usd(kpis.get('month_cost_usd'))} |",
        f"| today tokens | {kpis.get('today_tokens', 0):,} |",
        f"| burn rate | {kpis.get('burn_rate_tokens_per_min', 0):,.0f} tokens/min |",
        f"| cache hit rate | {kpis.get('cache_hit_rate', 0) * 100:.1f}% |",
        f"| active agents | {kpis.get('active_agents', 0)} |",
        f"| active projects | {kpis.get('active_projects', 0)} |",
        "",
        "## 5. Exports",
        "",
    ]
    for dataset, info in payload["exports"].items():
        lines.append(f"* `{dataset}` → `{info['path']}` ({info['bytes']:,} bytes)")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    # Windows consoles default to GBK; force UTF-8 so the Chinese notification previews
    # and the evidence tables print legibly instead of mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - redirected stream
            pass

    parser = argparse.ArgumentParser(description="Capture TokenLens evidence artefacts")
    parser.add_argument("--target-tokens", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20261110)
    parser.add_argument("--out", default=str(REPO_ROOT / "docs" / "evidence"))
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "evidence.db"))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    if db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()

    settings = Settings(
        db_path=db_path,
        data_dir=db_path.parent,
        api_token=None,
        enable_scheduler=False,
        notify_file=db_path.parent / "notifications.jsonl",
    )
    settings.ensure_dirs()

    payload: dict = {"generated_at": timeutil.iso(timeutil.now_ms())}
    with _client(settings) as client:
        health = client.get("/health").json()
        payload["health"] = health
        print(f"[1/5] ingest: pushing {args.target_tokens:,} tokens ...")
        payload["ingest"] = phase_ingest(client, args.target_tokens, args.seed, args.batch_size)
        print(f"      {payload['ingest']['snapshots_per_second']:,} snapshots/s, "
              f"{payload['ingest']['tokens_per_second']:,} tokens/s")

        print("[3/6] idempotency replay ...")
        payload["idempotency"] = phase_idempotency(client, args.seed + 1)

        print("[4/6] budget engine ...")
        payload["budgets"] = phase_budgets(client)

        print("[5/6] anomaly injection + detection ...")
        payload["anomalies"] = phase_anomalies(client)

        print("[6/6] dashboard + CSV exports ...")
        payload["dashboard"] = phase_dashboard(client)
        payload["exports"] = phase_exports(client, out_dir)

        # The benchmark runs last so the demo workload above is not polluted by it.
        print("[7/7] sustained ingest benchmark ...")
        payload["throughput"] = phase_throughput(client, args.seed + 7)
        print(f"      server: {payload['throughput']['server_snapshots_per_second']:,} snapshots/s, "
              f"p50 {payload['throughput']['per_batch_latency_ms']['p50']} ms/batch")

    (out_dir / "ingest_report.json").write_text(
        json.dumps(payload["ingest"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "budget_alert_record.json").write_text(
        json.dumps(payload["budgets"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "anomaly_cases.json").write_text(
        json.dumps(payload["anomalies"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "dashboard_snapshot.json").write_text(
        json.dumps(payload["dashboard"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "evidence.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_summary(out_dir, payload)

    print(f"\nEvidence written to {out_dir}")
    print(f"  alerts={len(payload['budgets']['alerts'])} "
          f"anomalies={len(payload['anomalies']['anomalies'])} "
          f"today_cost={payload['dashboard'].get('kpis', {}).get('today_cost_usd')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
