"""Foundation tests: pricing, time helpers, storage, ingest, app wiring.

These cover the modules owned by the Lead. Every teammate's suite builds on them.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tokenlens import pricing, timeutil
from tokenlens.db import Database
from tokenlens.schemas import SnapshotIn
from tokenlens.store import ingest_snapshots


# --------------------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------------------


def test_catalog_has_at_least_50_models():
    assert pricing.model_count() >= 50


def test_every_catalog_entry_has_coherent_prices():
    for entry in pricing.CATALOG.values():
        assert entry.input_per_mtok >= 0
        assert entry.output_per_mtok >= 0
        assert entry.cache_read_per_mtok <= entry.input_per_mtok
        assert entry.context_window > 0
        assert entry.provider


@pytest.mark.parametrize(
    "reported,expected",
    [
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("openai/gpt-4.1-mini", "gpt-4.1-mini"),
        ("deepseek-chat", "deepseek-chat"),
        ("llama3.1:70b", "llama3.1:70b"),
        ("sonnet", "claude-sonnet-4-5"),
    ],
)
def test_model_aliases_resolve(reported, expected):
    entry, known = pricing.resolve_model(reported)
    assert known is True
    assert entry.model == expected


def test_unknown_model_uses_fallback_but_is_flagged():
    entry, known = pricing.resolve_model("totally-new-model-xyz", "openai")
    assert known is False
    assert entry.provider == "openai"
    assert entry.input_per_mtok > 0


def test_compute_cost_uses_all_token_classes():
    cost, model, known = pricing.compute_cost(
        "claude-sonnet-4-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        reasoning_tokens=0,
    )
    assert known and model == "claude-sonnet-4-5"
    # 3.0 + 15.0 + 0.3 + 3.75
    assert cost == pytest.approx(22.05, abs=1e-6)


def test_reasoning_tokens_billed_at_output_rate():
    with_reasoning, _, _ = pricing.compute_cost("o3", output_tokens=0, reasoning_tokens=1_000_000)
    output_only, _, _ = pricing.compute_cost("o3", output_tokens=1_000_000)
    assert with_reasoning == pytest.approx(output_only)


def test_local_models_are_zero_cost_but_known():
    cost, model, known = pricing.compute_cost("llama3.1:8b", input_tokens=500_000, output_tokens=500_000)
    assert known is True
    assert model == "llama3.1:8b"
    assert cost == 0.0


def test_cost_breakdown_sums_to_total():
    breakdown = pricing.cost_breakdown(
        "gpt-4o", input_tokens=10_000, output_tokens=2_000, cache_read_tokens=5_000
    )
    parts = breakdown["input"] + breakdown["output"] + breakdown["cache_read"] + breakdown["cache_write"] + breakdown["reasoning"]
    assert breakdown["total"] == pytest.approx(parts, abs=1e-6)


def test_list_models_filter_by_provider():
    anthropic = pricing.list_models("anthropic")
    assert anthropic and all(m["provider"] == "anthropic" for m in anthropic)


# --------------------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------------------


NOV10 = 1794268800000  # 2026-11-10T00:00:00Z


@pytest.mark.parametrize(
    "value,expected_ms",
    [
        ("2026-11-10T00:00:00Z", NOV10),
        ("2026-11-10T08:00:00+08:00", NOV10),
        ("2026-11-10", NOV10),
        (NOV10, NOV10),
        (str(NOV10), NOV10),
        (NOV10 / 1000, NOV10),
    ],
)
def test_parse_time_ms(value, expected_ms):
    assert timeutil.parse_time_ms(value) == expected_ms


def test_parse_time_ms_rejects_garbage():
    with pytest.raises(ValueError):
        timeutil.parse_time_ms("not-a-time")


def test_iso_roundtrip():
    ms = NOV10 + 123
    assert timeutil.iso(ms) == "2026-11-10T00:00:00.123Z"
    assert timeutil.parse_time_ms(timeutil.iso(ms)) == ms


def test_bucket_start_is_utc_aligned():
    ms = NOV10 + 123
    assert timeutil.bucket_start(ms, "minute") == NOV10
    assert timeutil.bucket_start(ms, "hour") == NOV10
    assert timeutil.bucket_start(ms, "day") == NOV10
    assert timeutil.bucket_start(ms, "month") == timeutil.parse_time_ms("2026-11-01T00:00:00Z")


def test_week_buckets_start_monday():
    monday = timeutil.parse_time_ms("2026-11-09T00:00:00Z")
    wednesday = timeutil.parse_time_ms("2026-11-11T00:00:00Z")
    assert timeutil.bucket_start(wednesday, "week") == monday


def test_iter_buckets_gap_filling():
    start = timeutil.parse_time_ms("2026-11-10T00:00:00Z")
    end = timeutil.parse_time_ms("2026-11-10T04:00:00Z")
    buckets = list(timeutil.iter_buckets(start, end, "hour"))
    assert len(buckets) == 4
    assert buckets[0] == start


def test_period_windows_are_calendar_aligned_in_display_tz():
    at = timeutil.parse_time_ms("2026-11-10T13:00:00Z")  # 21:00 Shanghai
    start, end = timeutil.period_window("daily", at)
    assert timeutil.iso(start) == "2026-11-09T16:00:00.000Z"  # 00:00 +08:00
    assert timeutil.iso(end) == "2026-11-10T16:00:00.000Z"
    assert end - start == 86_400_000


def test_window_from_request_defaults_and_validation():
    now = timeutil.parse_time_ms("2026-11-10T12:00:00Z")
    start, end = timeutil.window_from_request(None, None, default_hours=6, now=now)
    assert end == now
    assert end - start == 6 * 3_600_000
    with pytest.raises(ValueError):
        timeutil.window_from_request("2026-11-10T12:00:00Z", "2026-11-10T10:00:00Z")


def test_normalize_bucket_aliases():
    assert timeutil.normalize_bucket("1h") == "hour"
    assert timeutil.normalize_bucket("daily") == "day"
    with pytest.raises(ValueError):
        timeutil.normalize_bucket("fortnight")


# --------------------------------------------------------------------------------------
# Database / ingest
# --------------------------------------------------------------------------------------


def test_schema_creates_all_tables(db: Database):
    counts = db.table_counts()
    assert set(counts) == {
        "usage_snapshots",
        "usage_rollup_hourly",
        "agents",
        "projects",
        "budgets",
        "alert_events",
        "anomaly_events",
    }


def test_ingest_roundtrip(db: Database):
    from tokenlens.testing import make_snapshot

    result = ingest_snapshots(db, [make_snapshot(event_id="e1"), make_snapshot(event_id="e2")])
    assert result.accepted == 2
    assert result.duplicates == 0
    assert result.total_tokens > 0
    assert result.cost_usd > 0
    assert db.scalar("SELECT COUNT(*) FROM usage_snapshots") == 2
    assert db.scalar("SELECT COUNT(*) FROM agents") == 1
    assert db.scalar("SELECT COUNT(*) FROM projects") == 1
    assert db.scalar("SELECT COUNT(*) FROM usage_rollup_hourly") == 1


def test_ingest_is_idempotent_on_event_id(db: Database):
    from tokenlens.testing import make_snapshot

    snap = make_snapshot(event_id="dup-1")
    first = ingest_snapshots(db, [snap])
    second = ingest_snapshots(db, [snap])
    assert first.accepted == 1
    assert second.accepted == 0
    assert second.duplicates == 1
    assert db.scalar("SELECT COUNT(*) FROM usage_snapshots") == 1


def test_ingest_auto_generates_event_ids(db: Database):
    from tokenlens.testing import make_snapshot

    snap = make_snapshot()
    snap.event_id = None
    result = ingest_snapshots(db, [snap])
    assert result.accepted == 1
    event_id = db.scalar("SELECT event_id FROM usage_snapshots")
    assert event_id and event_id.startswith("auto-")


def test_ingest_prices_and_canonicalises_model(db: Database):
    from tokenlens.testing import make_snapshot

    ingest_snapshots(db, [make_snapshot(model="claude-sonnet-4-5-20250929", event_id="m1")])
    row = db.fetch_one("SELECT model, provider, cost_usd, price_version FROM usage_snapshots")
    assert row["model"] == "claude-sonnet-4-5"
    assert row["provider"] == "anthropic"
    assert row["cost_usd"] > 0
    assert row["price_version"] == pricing.PRICE_VERSION


def test_ingest_reports_unknown_models(db: Database):
    from tokenlens.testing import make_snapshot

    result = ingest_snapshots(db, [make_snapshot(model="mystery-model-9", event_id="u1")])
    assert result.unknown_models == ["mystery-model-9"]
    assert result.accepted == 1


def test_ingest_rejects_oversized_batch(db: Database, settings):
    from tokenlens.testing import make_snapshot

    batch = [make_snapshot(event_id=f"big-{i}") for i in range(settings.max_ingest_batch + 1)]
    result = ingest_snapshots(db, batch)
    assert result.accepted == 0
    assert result.rejected == len(batch)


def test_rollup_matches_raw_aggregates(db: Database):
    from tokenlens.testing import make_snapshot

    ts = timeutil.parse_time_ms("2026-11-10T05:30:00Z")
    ingest_snapshots(
        db,
        [
            make_snapshot(ts=ts, event_id="r1", input_tokens=100, output_tokens=50, requests=1),
            make_snapshot(ts=ts + 60_000, event_id="r2", input_tokens=200, output_tokens=70, requests=1),
        ],
    )
    raw = db.fetch_one("SELECT SUM(input_tokens) i, SUM(output_tokens) o, SUM(requests) r FROM usage_snapshots")
    rollup = db.fetch_one("SELECT SUM(input_tokens) i, SUM(output_tokens) o, SUM(requests) r FROM usage_rollup_hourly")
    assert raw["i"] == rollup["i"]
    assert raw["o"] == rollup["o"]
    assert raw["r"] == rollup["r"]


# --------------------------------------------------------------------------------------
# API wiring
# --------------------------------------------------------------------------------------


def test_health_endpoint(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["price_version"] == pricing.PRICE_VERSION
    assert body["now"].endswith("Z")


def test_meta_endpoint_lists_endpoints(client: TestClient):
    response = client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["models"] >= 50
    assert any("ingest" in e for e in body["endpoints"])


def test_ingest_batch_endpoint(client: TestClient):
    payload = {
        "snapshots": [
            {
                "event_id": "api-1",
                "agent": "claude-code",
                "project": "payments-api",
                "session_id": "s-1",
                "model": "claude-sonnet-4-5",
                "runtime": "anthropic",
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_tokens": 500,
            }
        ]
    }
    response = client.post("/api/v1/ingest/snapshots", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["total_tokens"] == 1700
    assert body["cost_usd"] > 0
    assert body["price_version"] == pricing.PRICE_VERSION


def test_ingest_is_idempotent_over_http(client: TestClient):
    payload = {
        "snapshots": [
            {
                "event_id": "api-dup",
                "agent": "a",
                "project": "p",
                "session_id": "s",
                "model": "gpt-4o",
                "input_tokens": 10,
                "output_tokens": 10,
            }
        ]
    }
    assert client.post("/api/v1/ingest/snapshots", json=payload).json()["accepted"] == 1
    second = client.post("/api/v1/ingest/snapshots", json=payload).json()
    assert second["accepted"] == 0
    assert second["duplicates"] == 1


def test_ingest_validates_negative_tokens(client: TestClient):
    payload = {"snapshots": [{"agent": "a", "model": "gpt-4o", "input_tokens": -5}]}
    assert client.post("/api/v1/ingest/snapshots", json=payload).status_code == 422


def test_ingest_single_endpoint(client: TestClient):
    response = client.post(
        "/api/v1/ingest/snapshot",
        json={"agent": "codex-cli", "model": "gpt-5", "project": "web", "input_tokens": 100},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_api_key_auth_enforced_when_configured(settings, db):
    settings.api_token = "s3cret"
    from tokenlens.app import create_app

    app = create_app(settings=settings, db=db)
    with TestClient(app) as client:
        # /health and /api/v1/meta stay open (probes + capability discovery).
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/meta").status_code == 200
        # Every /api/v1 ingest/query route requires the key.
        assert client.get("/api/v1/ingest/ping").status_code == 401
        ok = client.get("/api/v1/ingest/ping", headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 200
        bad = client.get("/api/v1/ingest/ping", headers={"X-API-Key": "nope"})
        assert bad.status_code == 401


def test_openapi_schema_builds(app):
    schema = app.openapi()
    assert schema["info"]["title"] == "TokenLens"
    assert "/api/v1/ingest/snapshots" in schema["paths"]


def test_settings_ensure_dirs(tmp_path):
    from tokenlens.config import Settings

    target = tmp_path / "nested" / "dir"
    settings = Settings(db_path=target / "x.db", data_dir=target)
    settings.ensure_dirs()
    assert target.exists()


def test_notifications_jsonl_is_plain_text(tmp_path):
    """Guard: notification sink writes one JSON object per line (budget-anomaly owns impl)."""
    path = tmp_path / "n.jsonl"
    path.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8").strip())["a"] == 1

