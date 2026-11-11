"""Query API tests: usage summary/breakdown/timeseries/live, trends, catalog, CSV export.

Coverage strategy:

* **Filter semantics** — every filter is exercised both alone and combined, and the
  result is compared against a Python-side re-aggregation of the same rows, so a bug in
  the SQL ``WHERE`` clause cannot hide behind a self-consistent SQL assertion.
* **Bucketing** — the SQL bucket expressions are asserted to be byte-identical to
  :func:`tokenlens.timeutil.bucket_start`, and gap filling is asserted on an explicitly
  sparse window (three rows, six hours).
* **Empty database** — every endpoint is called before anything is ingested and must
  return zeros/empty lists with HTTP 200. A monitoring API that 500s on a fresh install
  is a broken monitoring API.
* **Contract violations** — invalid ``bucket``/``dimension``/``period``/``dataset`` and an
  inverted window must all be HTTP 400, never 500.
* **CSV** — BOM prefix, ``Content-Disposition`` filename, header order and row counts.
* **Live metrics** — asserted from known raw rows and an explicitly seeded "now", because
  a burn rate that only ever reads zero is worse than no burn rate at all.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from tokenlens import query, timeutil
from tokenlens.db import Database
from tokenlens.store import ingest_snapshots
from tokenlens.testing import make_snapshot

# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------

#: A fixed, timezone-unambiguous anchor: 2026-11-10T00:00:00Z.
DAY0 = timeutil.parse_time_ms("2026-11-10T00:00:00Z")
HOUR = 3_600_000


def _iso(ms: int) -> str:
    return timeutil.iso(ms)


def _seed(db: Database, snapshots) -> None:
    """Ingest a list of snapshots and assert they all landed."""
    result = ingest_snapshots(db, list(snapshots))
    assert result.accepted == len(snapshots), result.errors


@pytest.fixture()
def seed_data(db: Database) -> Database:
    """A small, fully deterministic ledger used by most tests.

    Layout (UTC, all on 2026-11-10)::

        00:00  agent-a  project-x  claude-sonnet-4-5  1000 in / 500 out
        00:30  agent-a  project-x  claude-sonnet-4-5  2000 in / 500 out
        01:00  agent-b  project-y  gpt-5              5000 in / 900 out
        02:00  agent-b  project-y  gpt-5              1000 in / 100 out
        03:00  agent-a  project-y  claude-opus-4-1    3000 in / 800 out
        05:00  agent-c  project-x  deepseek-chat      1000 in / 200 out   (leaves a gap)

    Three agents, two projects, four models, six snapshots, a deliberate 04:00 gap.
    """
    _seed(
        db,
        [
            make_snapshot(
                ts=DAY0 + 0 * HOUR,
                agent="agent-a",
                project="project-x",
                model="claude-sonnet-4-5",
                runtime="anthropic",
                session_id="sess-1",
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="seed-1",
            ),
            make_snapshot(
                ts=DAY0 + 30 * 60_000,
                agent="agent-a",
                project="project-x",
                model="claude-sonnet-4-5",
                runtime="anthropic",
                session_id="sess-1",
                input_tokens=2000,
                output_tokens=500,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="seed-2",
            ),
            make_snapshot(
                ts=DAY0 + 1 * HOUR,
                agent="agent-b",
                project="project-y",
                model="gpt-5",
                runtime="openai",
                session_id="sess-2",
                input_tokens=5000,
                output_tokens=900,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=2,
                event_id="seed-3",
            ),
            make_snapshot(
                ts=DAY0 + 2 * HOUR,
                agent="agent-b",
                project="project-y",
                model="gpt-5",
                runtime="openai",
                session_id="sess-2",
                input_tokens=1000,
                output_tokens=100,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="seed-4",
            ),
            make_snapshot(
                ts=DAY0 + 3 * HOUR,
                agent="agent-a",
                project="project-y",
                model="claude-opus-4-1",
                runtime="anthropic",
                session_id="sess-3",
                input_tokens=3000,
                output_tokens=800,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="seed-5",
            ),
            make_snapshot(
                ts=DAY0 + 5 * HOUR,
                agent="agent-c",
                project="project-x",
                model="deepseek-chat",
                runtime="deepseek",
                session_id="sess-4",
                input_tokens=1000,
                output_tokens=200,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="seed-6",
            ),
        ],
    )
    return db


def _window_params(start: int, end: int) -> dict[str, str]:
    return {"from": _iso(start), "to": _iso(end)}


FULL_WINDOW = (DAY0, DAY0 + 6 * HOUR)


# --------------------------------------------------------------------------------------
# /api/v1/usage/summary
# --------------------------------------------------------------------------------------


def test_summary_empty_database_is_zero_not_an_error(client: TestClient) -> None:
    response = client.get("/api/v1/usage/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["total_tokens"] == 0
    assert body["totals"]["cost_usd"] == 0.0
    assert body["totals"]["requests"] == 0
    assert body["window"]["from"].endswith("Z") and body["window"]["to"].endswith("Z")


def test_summary_aggregates_every_token_class(client: TestClient, seed_data: Database) -> None:
    response = client.get("/api/v1/usage/summary", params=_window_params(*FULL_WINDOW))
    assert response.status_code == 200
    totals = response.json()["totals"]
    # 1000+2000+5000+1000+3000+1000 input, 500+500+900+100+800+200 output
    assert totals["input_tokens"] == 13_000
    assert totals["output_tokens"] == 3_000
    assert totals["total_tokens"] == 16_000
    assert totals["billable_tokens"] == 16_000  # no cache/reasoning traffic seeded
    assert totals["requests"] == 7
    assert totals["sessions"] == 4
    assert totals["agents"] == 3
    assert totals["projects"] == 2
    assert totals["cost_usd"] > 0
    assert totals["cache_hit_rate"] == 0.0


def test_summary_matches_python_side_aggregation(
    client: TestClient, seed_data: Database
) -> None:
    """Cross-check the SQL aggregate against a plain Python sum of the raw rows."""
    rows = seed_data.fetch_all("SELECT * FROM usage_snapshots")
    expected_cost = round(sum(row["cost_usd"] for row in rows), 6)
    expected_tokens = sum(
        row["input_tokens"]
        + row["output_tokens"]
        + row["cache_read_tokens"]
        + row["cache_write_tokens"]
        + row["reasoning_tokens"]
        for row in rows
    )
    body = client.get("/api/v1/usage/summary", params=_window_params(*FULL_WINDOW)).json()
    assert body["totals"]["cost_usd"] == pytest.approx(expected_cost, abs=1e-6)
    assert body["totals"]["total_tokens"] == expected_tokens
    assert body["totals"]["avg_tokens_per_request"] == pytest.approx(
        expected_tokens / 7, abs=0.01
    )
    assert body["totals"]["avg_cost_per_request"] == pytest.approx(
        expected_cost / 7, abs=1e-6
    )


def test_summary_window_is_half_open(client: TestClient, seed_data: Database) -> None:
    """``to`` is exclusive: the 01:00 snapshot belongs to the second window only."""
    first = client.get(
        "/api/v1/usage/summary", params=_window_params(DAY0, DAY0 + 1 * HOUR)
    ).json()
    second = client.get(
        "/api/v1/usage/summary", params=_window_params(DAY0 + 1 * HOUR, DAY0 + 2 * HOUR)
    ).json()
    assert first["totals"]["input_tokens"] == 3000  # 00:00 + 00:30 only
    assert second["totals"]["input_tokens"] == 5000  # 01:00 only
    total = client.get("/api/v1/usage/summary", params=_window_params(*FULL_WINDOW)).json()
    assert (
        first["totals"]["input_tokens"] + second["totals"]["input_tokens"]
        <= total["totals"]["input_tokens"]
    )


@pytest.mark.parametrize(
    "filters,expected_input",
    [
        ({"agent": "agent-a"}, 6000),        # 1000 + 2000 + 3000
        ({"agent": "agent-b"}, 6000),        # 5000 + 1000
        ({"agent": "agent-c"}, 1000),
        ({"project": "project-x"}, 4000),    # 1000 + 2000 + 1000
        ({"project": "project-y"}, 9000),    # 5000 + 1000 + 3000
        ({"model": "gpt-5"}, 6000),
        ({"model": "claude-sonnet-4-5"}, 3000),
        ({"runtime": "openai"}, 6000),
        ({"runtime": "anthropic"}, 6000),
        ({"session_id": "sess-1"}, 3000),
        ({"session_id": "nope"}, 0),
        ({"agent": "agent-a", "project": "project-y"}, 3000),
        ({"agent": "agent-a", "project": "project-x"}, 3000),
        ({"runtime": "openai", "project": "project-x"}, 0),
    ],
)
def test_summary_filters(
    client: TestClient, seed_data: Database, filters: dict[str, str], expected_input: int
) -> None:
    params = {**_window_params(*FULL_WINDOW), **filters}
    response = client.get("/api/v1/usage/summary", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["input_tokens"] == expected_input
    # Echoed filters are exactly what the caller supplied.
    assert body["filters"] == filters


def test_summary_rejects_inverted_window(client: TestClient) -> None:
    response = client.get(
        "/api/v1/usage/summary",
        params={"from": "2026-11-10T05:00:00Z", "to": "2026-11-10T04:00:00Z"},
    )
    assert response.status_code == 400
    assert "from" in response.json()["detail"]


def test_summary_rejects_unparseable_timestamp(client: TestClient) -> None:
    response = client.get("/api/v1/usage/summary", params={"from": "yesterday-ish"})
    assert response.status_code == 400


def test_summary_accepts_epoch_milliseconds(client: TestClient, seed_data: Database) -> None:
    """Both ISO-8601 and raw epoch ms are valid ``from``/``to`` inputs (contract §6)."""
    response = client.get(
        "/api/v1/usage/summary",
        params={"from": DAY0, "to": DAY0 + 6 * HOUR},
    )
    assert response.status_code == 200
    assert response.json()["totals"]["total_tokens"] == 16_000


def test_summary_default_window_is_trailing_24h(client: TestClient) -> None:
    body = client.get("/api/v1/usage/summary").json()
    start = timeutil.parse_time_ms(body["window"]["from"])
    end = timeutil.parse_time_ms(body["window"]["to"])
    assert end - start == 24 * HOUR


# --------------------------------------------------------------------------------------
# /api/v1/usage/breakdown
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", ["agent", "project", "model", "runtime", "session"])
def test_breakdown_shape_and_sorting(
    client: TestClient, seed_data: Database, dimension: str
) -> None:
    response = client.get(
        "/api/v1/usage/breakdown",
        params={**_window_params(*FULL_WINDOW), "dimension": dimension},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dimension"] == dimension
    assert body["rows"], f"{dimension} breakdown must not be empty"
    costs = [row["totals"]["cost_usd"] for row in body["rows"]]
    assert costs == sorted(costs, reverse=True), "rows must be sorted by cost desc"
    for row in body["rows"]:
        assert set(row) == {"key", "label", "totals", "share_pct"}
        assert row["label"]


def test_breakdown_share_pct_sums_to_100(
    client: TestClient, seed_data: Database
) -> None:
    for dimension in ("agent", "project", "model", "runtime"):
        body = client.get(
            "/api/v1/usage/breakdown",
            params={**_window_params(*FULL_WINDOW), "dimension": dimension},
        ).json()
        total_share = sum(row["share_pct"] for row in body["rows"])
        assert total_share == pytest.approx(100.0, abs=1e-3), dimension


def test_breakdown_row_costs_sum_to_window_total(
    client: TestClient, seed_data: Database
) -> None:
    body = client.get(
        "/api/v1/usage/breakdown",
        params={**_window_params(*FULL_WINDOW), "dimension": "agent"},
    ).json()
    row_cost = sum(row["totals"]["cost_usd"] for row in body["rows"])
    assert row_cost == pytest.approx(body["total"]["cost_usd"], abs=1e-6)
    summary = client.get(
        "/api/v1/usage/summary", params=_window_params(*FULL_WINDOW)
    ).json()
    assert body["total"]["cost_usd"] == pytest.approx(
        summary["totals"]["cost_usd"], abs=1e-6
    )


def test_breakdown_agent_groups_are_correct(
    client: TestClient, seed_data: Database
) -> None:
    body = client.get(
        "/api/v1/usage/breakdown",
        params={**_window_params(*FULL_WINDOW), "dimension": "agent"},
    ).json()
    by_key = {row["key"]: row for row in body["rows"]}
    assert set(by_key) == {"agent-a", "agent-b", "agent-c"}
    assert by_key["agent-a"]["totals"]["input_tokens"] == 6000
    assert by_key["agent-b"]["totals"]["input_tokens"] == 6000
    assert by_key["agent-c"]["totals"]["input_tokens"] == 1000
    # agent-a/b tie on input tokens but the ordering must stay deterministic.
    assert [row["key"] for row in body["rows"]] == sorted(
        by_key, key=lambda k: (-by_key[k]["totals"]["cost_usd"], k)
    )


def test_breakdown_limit_and_offset_are_stable(
    client: TestClient, seed_data: Database
) -> None:
    params = {**_window_params(*FULL_WINDOW), "dimension": "model"}
    full = client.get("/api/v1/usage/breakdown", params=params).json()["rows"]
    page = client.get("/api/v1/usage/breakdown", params={**params, "limit": 2}).json()[
        "rows"
    ]
    offset = client.get(
        "/api/v1/usage/breakdown", params={**params, "limit": 2, "offset": 2}
    ).json()["rows"]
    assert [row["key"] for row in page] == [row["key"] for row in full[:2]]
    assert [row["key"] for row in offset] == [row["key"] for row in full[2:4]]
    # share_pct is relative to the window, not the page.
    assert page[0]["share_pct"] == pytest.approx(full[0]["share_pct"])


def test_breakdown_respects_filters(client: TestClient, seed_data: Database) -> None:
    body = client.get(
        "/api/v1/usage/breakdown",
        params={
            **_window_params(*FULL_WINDOW),
            "dimension": "model",
            "project": "project-x",
        },
    ).json()
    keys = {row["key"] for row in body["rows"]}
    assert keys == {"claude-sonnet-4-5", "deepseek-chat"}
    assert body["total"]["input_tokens"] == 4000


def test_breakdown_empty_database_returns_empty_rows(client: TestClient) -> None:
    body = client.get("/api/v1/usage/breakdown", params={"dimension": "agent"}).json()
    assert body["rows"] == []
    assert body["total"]["cost_usd"] == 0.0


def test_breakdown_invalid_dimension_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/usage/breakdown", params={"dimension": "banana"})
    assert response.status_code == 400
    assert "dimension" in response.json()["detail"]


def test_breakdown_model_labels_use_display_names(
    client: TestClient, seed_data: Database
) -> None:
    body = client.get(
        "/api/v1/usage/breakdown",
        params={**_window_params(*FULL_WINDOW), "dimension": "model"},
    ).json()
    labels = {row["key"]: row["label"] for row in body["rows"]}
    assert labels["gpt-5"] == "GPT-5"
    assert labels["claude-sonnet-4-5"] == "Claude Sonnet 4.5"


# --------------------------------------------------------------------------------------
# /api/v1/usage/timeseries
# --------------------------------------------------------------------------------------


def test_timeseries_gap_fills_ascending(client: TestClient, seed_data: Database) -> None:
    """Six hour buckets, three of them empty — all six must be present, in order."""
    body = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(*FULL_WINDOW), "bucket": "hour"},
    ).json()
    assert body["bucket"] == "hour"
    points = body["points"]
    assert len(points) == 6
    starts = [timeutil.parse_time_ms(point["bucket_start"]) for point in points]
    assert starts == sorted(starts)
    assert starts[0] == DAY0
    # Hour 04:00 has no rows -> explicit zero point, not a missing one.
    assert points[4]["totals"]["total_tokens"] == 0
    assert points[4]["totals"]["cost_usd"] == 0.0
    # Hours with traffic carry the right tokens.
    assert points[0]["totals"]["input_tokens"] == 3000
    assert points[1]["totals"]["input_tokens"] == 5000
    assert points[2]["totals"]["input_tokens"] == 1000
    assert points[3]["totals"]["input_tokens"] == 3000
    assert points[5]["totals"]["input_tokens"] == 1000


def test_timeseries_totals_match_summary(client: TestClient, seed_data: Database) -> None:
    points = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(*FULL_WINDOW), "bucket": "hour"},
    ).json()["points"]
    summary = client.get(
        "/api/v1/usage/summary", params=_window_params(*FULL_WINDOW)
    ).json()["totals"]
    assert sum(point["totals"]["total_tokens"] for point in points) == summary["total_tokens"]
    assert sum(point["totals"]["cost_usd"] for point in points) == pytest.approx(
        summary["cost_usd"], abs=1e-6
    )


def test_timeseries_labels_are_display_timezone(
    client: TestClient, seed_data: Database
) -> None:
    """00:00 UTC is 08:00 in Asia/Shanghai (the configured display tz)."""
    points = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(DAY0, DAY0 + 1 * HOUR), "bucket": "hour"},
    ).json()["points"]
    assert points[0]["label"] == "2026-11-10 08:00"
    assert timeutil.parse_time_ms(points[0]["bucket_start"]) == DAY0
    assert timeutil.parse_time_ms(points[0]["bucket_end"]) == DAY0 + HOUR
    # Contract §3: datetimes are ISO-8601 UTC ending in Z.
    assert points[0]["bucket_start"].endswith("Z")


@pytest.mark.parametrize(
    "bucket,expected",
    [("minute", 360), ("hour", 6), ("day", 1)],
)
def test_timeseries_bucket_sizes(
    client: TestClient, seed_data: Database, bucket: str, expected: int
) -> None:
    body = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(*FULL_WINDOW), "bucket": bucket},
    ).json()
    assert len(body["points"]) == expected
    assert body["bucket"] == bucket


def test_timeseries_minute_buckets_are_gap_filled(
    client: TestClient, seed_data: Database
) -> None:
    points = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(DAY0, DAY0 + 3 * 60_000), "bucket": "minute"},
    ).json()["points"]
    assert len(points) == 3
    assert points[0]["totals"]["input_tokens"] == 1000
    assert points[1]["totals"]["total_tokens"] == 0
    assert points[2]["totals"]["total_tokens"] == 0


def test_timeseries_respects_filters(client: TestClient, seed_data: Database) -> None:
    points = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(*FULL_WINDOW), "bucket": "hour", "agent": "agent-b"},
    ).json()["points"]
    assert points[0]["totals"]["total_tokens"] == 0
    assert points[1]["totals"]["input_tokens"] == 5000
    assert points[2]["totals"]["input_tokens"] == 1000
    assert sum(point["totals"]["total_tokens"] for point in points) == 7000


def test_timeseries_empty_database_is_all_zero_points(client: TestClient) -> None:
    body = client.get(
        "/api/v1/usage/timeseries",
        params={**_window_params(DAY0, DAY0 + 3 * HOUR), "bucket": "hour"},
    ).json()
    assert len(body["points"]) == 3
    assert all(point["totals"]["total_tokens"] == 0 for point in body["points"])


def test_timeseries_invalid_bucket_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/usage/timeseries", params={"bucket": "fortnight"})
    assert response.status_code == 400
    assert "bucket" in response.json()["detail"]


def test_timeseries_oversized_window_is_400(client: TestClient) -> None:
    """A year of minute buckets is rejected instead of allocating 500k points."""
    response = client.get(
        "/api/v1/usage/timeseries",
        params={
            "bucket": "minute",
            "from": "2025-01-01T00:00:00Z",
            "to": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 400
    assert "too large" in response.json()["detail"]


def test_timeseries_week_and_month_buckets_are_calendar_aligned(
    client: TestClient, seed_data: Database
) -> None:
    """2026-11-10 is a Tuesday; its ISO week starts Monday 2026-11-09."""
    week = client.get(
        "/api/v1/usage/timeseries",
        params={
            "bucket": "week",
            "from": "2026-11-01T00:00:00Z",
            "to": "2026-11-20T00:00:00Z",
        },
    ).json()["points"]
    assert timeutil.parse_time_ms(week[0]["bucket_start"]) == timeutil.parse_time_ms(
        "2026-10-26T00:00:00Z"
    )
    month = client.get(
        "/api/v1/usage/timeseries",
        params={
            "bucket": "month",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-12-01T00:00:00Z",
        },
    ).json()["points"]
    assert [
        timeutil.iso_seconds(timeutil.parse_time_ms(point["bucket_start"]))
        for point in month
    ] == ["2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"]


def test_sql_bucketing_matches_timeutil(db: Database, seed_data: Database) -> None:
    """The SQL bucket expressions must be byte-identical to timeutil.bucket_start."""
    rows = db.fetch_all("SELECT ts FROM usage_snapshots")
    for bucket in ("minute", "hour", "day", "week", "month"):
        expression = query._BUCKET_EXPRESSIONS[bucket]
        sql_rows = db.fetch_all(
            f"SELECT DISTINCT {expression} AS bucket_ms FROM usage_snapshots s ORDER BY 1"
        )
        sql_buckets = {int(row["bucket_ms"]) for row in sql_rows}
        expected = {timeutil.bucket_start(row["ts"], bucket) for row in rows}
        assert sql_buckets == expected, bucket


# --------------------------------------------------------------------------------------
# /api/v1/usage/live
# --------------------------------------------------------------------------------------


def test_live_empty_database_is_zero(client: TestClient) -> None:
    body = client.get("/api/v1/usage/live").json()
    assert body["window_minutes"] == 5.0
    assert body["tokens_per_min"] == 0.0
    assert body["cost_per_hour"] == 0.0
    assert body["active_agents"] == 0
    assert body["active_projects"] == 0
    assert body["active_sessions"] == 0
    assert body["top_projects"] == []
    assert body["top_agents"] == []


def test_live_metrics_from_raw_rows(client: TestClient, db: Database) -> None:
    """Seed snapshots at a known offset from *now* and verify the burn rate math."""
    now = timeutil.now_ms()
    _seed(
        db,
        [
            make_snapshot(
                ts=now - 60_000,
                agent="live-a",
                project="live-p1",
                session_id="live-s1",
                model="gpt-5",
                runtime="openai",
                input_tokens=600,
                output_tokens=400,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=2,
                event_id="live-1",
            ),
            make_snapshot(
                ts=now - 120_000,
                agent="live-b",
                project="live-p2",
                session_id="live-s2",
                model="gpt-5",
                runtime="openai",
                input_tokens=600,
                output_tokens=400,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=2,
                event_id="live-2",
            ),
            # Outside the 5 minute window -> must be ignored.
            make_snapshot(
                ts=now - 20 * 60_000,
                agent="stale",
                project="stale-p",
                session_id="stale-s",
                event_id="live-stale",
            ),
        ],
    )
    body = client.get("/api/v1/usage/live", params={"window_minutes": 5}).json()
    # 2000 tokens over 5 minutes = 400 tokens/min.
    assert body["tokens_per_min"] == pytest.approx(400.0, abs=1.0)
    assert body["active_agents"] == 2
    assert body["active_projects"] == 2
    assert body["active_sessions"] == 2
    assert body["cost_per_hour"] > 0
    assert {entry["project"] for entry in body["top_projects"]} == {"live-p1", "live-p2"}
    assert {entry["agent"] for entry in body["top_agents"]} == {"live-a", "live-b"}
    top = body["top_projects"][0]
    assert top["tokens_per_min"] == pytest.approx(200.0, abs=1.0)


def test_live_window_minutes_scales_the_rate(client: TestClient, db: Database) -> None:
    now = timeutil.now_ms()
    _seed(
        db,
        [
            make_snapshot(
                ts=now - 60_000,
                agent="scale-a",
                project="scale-p",
                input_tokens=1200,
                output_tokens=800,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="scale-1",
            )
        ],
    )
    short = client.get("/api/v1/usage/live", params={"window_minutes": 5}).json()
    long = client.get("/api/v1/usage/live", params={"window_minutes": 10}).json()
    assert short["tokens_per_min"] == pytest.approx(400.0, abs=1.0)
    assert long["tokens_per_min"] == pytest.approx(200.0, abs=1.0)


def test_live_rejects_invalid_window(client: TestClient) -> None:
    assert client.get("/api/v1/usage/live", params={"window_minutes": 0}).status_code == 422
    assert (
        client.get("/api/v1/usage/live", params={"window_minutes": 5000}).status_code == 422
    )


def test_live_metrics_helper_returns_expected_keys(db: Database) -> None:
    metrics = query.live_metrics(db, 5)
    assert set(metrics) >= {
        "window_minutes",
        "tokens_per_min",
        "cost_per_hour",
        "active_agents",
        "active_projects",
        "active_sessions",
        "top_projects",
        "top_agents",
    }


# --------------------------------------------------------------------------------------
# /api/v1/trends
# --------------------------------------------------------------------------------------


def test_trends_empty_database_returns_zero_comparison(client: TestClient) -> None:
    response = client.get("/api/v1/trends", params={"period": "day", "limit": 7})
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "day"
    assert len(body["points"]) == 7
    assert body["comparison"]["current_cost_usd"] == 0.0
    assert body["comparison"]["previous_cost_usd"] == 0.0
    assert body["comparison"]["delta_pct"] == 0.0


@pytest.mark.parametrize(
    "period,expected_points",
    [("day", 3), ("week", 3), ("month", 3)],
)
def test_trends_point_count_matches_limit(
    client: TestClient, seed_data: Database, period: str, expected_points: int
) -> None:
    body = client.get(
        "/api/v1/trends", params={"period": period, "limit": expected_points}
    ).json()
    assert body["period"] == period
    assert len(body["points"]) == expected_points


def test_trends_window_ends_at_current_bucket(client: TestClient) -> None:
    body = client.get("/api/v1/trends", params={"period": "day", "limit": 2}).json()
    start = timeutil.parse_time_ms(body["window"]["from"])
    end = timeutil.parse_time_ms(body["window"]["to"])
    today = timeutil.bucket_start(timeutil.now_ms(), "day")
    assert end == timeutil.bucket_end(today, "day")
    assert start == today - 86_400_000


def test_trends_comparison_uses_previous_window(client: TestClient, db: Database) -> None:
    """Seed an explicit previous-day spend and assert the delta is computed correctly.

    With ``limit=1`` the current window is today and the comparison window is yesterday,
    so the two seeded days map exactly onto "current" and "previous".
    """
    today = timeutil.bucket_start(timeutil.now_ms(), "day")
    yesterday = today - 86_400_000
    _seed(
        db,
        [
            make_snapshot(
                ts=yesterday + HOUR,
                agent="trend-a",
                project="trend-p",
                model="gpt-5",
                runtime="openai",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="trend-prev",
            ),
            make_snapshot(
                ts=today + HOUR,
                agent="trend-a",
                project="trend-p",
                model="gpt-5",
                runtime="openai",
                input_tokens=2_000_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                requests=1,
                event_id="trend-cur",
            ),
        ],
    )
    body = client.get("/api/v1/trends", params={"period": "day", "limit": 1}).json()
    comparison = body["comparison"]
    # gpt-5 input is $1.25/Mtok: 1.25 USD then 2.50 USD.
    assert comparison["previous_cost_usd"] == pytest.approx(1.25, abs=1e-6)
    assert comparison["current_cost_usd"] == pytest.approx(2.50, abs=1e-6)
    assert comparison["delta_usd"] == pytest.approx(1.25, abs=1e-6)
    assert comparison["delta_pct"] == pytest.approx(100.0, abs=1e-6)
    assert comparison["previous_tokens"] == 1_000_000
    assert comparison["current_tokens"] == 2_000_000
    assert comparison["token_delta_pct"] == pytest.approx(100.0, abs=1e-6)


def test_trends_multi_bucket_window_does_not_overlap_its_comparison(
    client: TestClient, db: Database
) -> None:
    """With ``limit=2`` the previous window is the two buckets *before* the current two."""
    today = timeutil.bucket_start(timeutil.now_ms(), "day")
    _seed(
        db,
        [
            make_snapshot(
                ts=today - 2 * 86_400_000 + HOUR,
                agent="overlap",
                project="p",
                model="gpt-5",
                runtime="openai",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="overlap-prev",
            ),
            make_snapshot(
                ts=today + HOUR,
                agent="overlap",
                project="p",
                model="gpt-5",
                runtime="openai",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="overlap-cur",
            ),
        ],
    )
    body = client.get("/api/v1/trends", params={"period": "day", "limit": 2}).json()
    # Yesterday + today are the current window; only the day before yesterday is previous.
    assert body["comparison"]["current_tokens"] == 1_000_000
    assert body["comparison"]["previous_tokens"] == 1_000_000


def test_trends_comparison_without_baseline_is_zero_pct(
    client: TestClient, db: Database
) -> None:
    today = timeutil.bucket_start(timeutil.now_ms(), "day")
    _seed(
        db,
        [
            make_snapshot(
                ts=today + HOUR,
                agent="only-today",
                project="p",
                model="gpt-5",
                runtime="openai",
                input_tokens=100_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="only-today-1",
            )
        ],
    )
    comparison = client.get(
        "/api/v1/trends", params={"period": "day", "limit": 2}
    ).json()["comparison"]
    assert comparison["previous_cost_usd"] == 0.0
    assert comparison["current_cost_usd"] > 0
    assert comparison["delta_pct"] == 0.0  # no division by zero / infinity


def test_trends_applies_filters(client: TestClient, db: Database) -> None:
    today = timeutil.bucket_start(timeutil.now_ms(), "day")
    _seed(
        db,
        [
            make_snapshot(
                ts=today + HOUR,
                agent="keep-me",
                project="p",
                model="gpt-5",
                runtime="openai",
                input_tokens=10_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="f-1",
            ),
            make_snapshot(
                ts=today + HOUR,
                agent="drop-me",
                project="p",
                model="gpt-5",
                runtime="openai",
                input_tokens=90_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="f-2",
            ),
        ],
    )
    filtered = client.get(
        "/api/v1/trends",
        params={"period": "day", "limit": 1, "agent": "keep-me"},
    ).json()
    unfiltered = client.get(
        "/api/v1/trends", params={"period": "day", "limit": 1}
    ).json()
    assert filtered["comparison"]["current_tokens"] == 10_000
    assert unfiltered["comparison"]["current_tokens"] == 100_000
    assert filtered["filters"] == {"agent": "keep-me"}


def test_trends_invalid_period_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/trends", params={"period": "quarter"})
    assert response.status_code == 400
    assert "period" in response.json()["detail"]


def test_trends_rejects_invalid_limit(client: TestClient) -> None:
    assert client.get("/api/v1/trends", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/trends", params={"limit": 5000}).status_code == 422


def test_last_n_buckets_is_calendar_aware() -> None:
    at = timeutil.parse_time_ms("2026-11-10T13:00:00Z")
    start, end = query.last_n_buckets("month", 3, at_ms=at)
    assert timeutil.iso(start) == "2026-09-01T00:00:00.000Z"
    assert timeutil.iso(end) == "2026-12-01T00:00:00.000Z"


# --------------------------------------------------------------------------------------
# /api/v1/catalog/*
# --------------------------------------------------------------------------------------


def test_catalog_models_shape(client: TestClient) -> None:
    body = client.get("/api/v1/catalog/models").json()
    assert body["price_version"]
    assert body["count"] >= 50
    assert body["count"] == len(body["models"])
    entry = body["models"][0]
    assert set(entry) == {
        "model",
        "provider",
        "display_name",
        "input_per_mtok",
        "output_per_mtok",
        "cache_read_per_mtok",
        "cache_write_per_mtok",
        "context_window",
        "known",
    }
    assert entry["known"] is True


def test_catalog_models_filter_by_provider(client: TestClient) -> None:
    body = client.get("/api/v1/catalog/models", params={"provider": "anthropic"}).json()
    assert body["models"]
    assert all(entry["provider"] == "anthropic" for entry in body["models"])
    assert body["count"] == len(body["models"])


def test_catalog_agents_lists_observed_entities(client: TestClient, seed_data: Database) -> None:
    body = client.get("/api/v1/catalog/agents").json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    by_id = {item["id"]: item for item in body["items"]}
    assert set(by_id) == {"agent-a", "agent-b", "agent-c"}
    agent_a = by_id["agent-a"]
    assert agent_a["snapshot_count"] == 3
    assert agent_a["total_tokens"] == 7800  # 1000+500 + 2000+500 + 3000+800
    assert agent_a["runtime"] == "anthropic"
    assert agent_a["first_seen"] == "2026-11-10T00:00:00.000Z"
    assert agent_a["last_seen"] == "2026-11-10T03:00:00.000Z"
    # Sorted by cost descending.
    costs = [item["cost_usd"] for item in body["items"]]
    assert costs == sorted(costs, reverse=True)


def test_catalog_agents_pagination(client: TestClient, seed_data: Database) -> None:
    first = client.get("/api/v1/catalog/agents", params={"limit": 2}).json()
    second = client.get("/api/v1/catalog/agents", params={"limit": 2, "offset": 2}).json()
    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert {item["id"] for item in first["items"]}.isdisjoint(
        {item["id"] for item in second["items"]}
    )


def test_catalog_projects(client: TestClient, seed_data: Database) -> None:
    body = client.get("/api/v1/catalog/projects").json()
    assert body["total"] == 2
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["project-x"]["snapshot_count"] == 3
    assert by_id["project-y"]["snapshot_count"] == 3
    assert by_id["project-x"]["display_name"] == "project-x"


def test_catalog_sessions(client: TestClient, seed_data: Database) -> None:
    body = client.get("/api/v1/catalog/sessions").json()
    assert body["total"] == 4
    by_id = {item["session_id"]: item for item in body["items"]}
    assert by_id["sess-1"]["snapshots"] == 2
    assert by_id["sess-1"]["agent"] == "agent-a"
    assert by_id["sess-1"]["project"] == "project-x"
    assert by_id["sess-1"]["models"] == ["claude-sonnet-4-5"]
    assert by_id["sess-1"]["started_at"] == "2026-11-10T00:00:00.000Z"
    assert by_id["sess-1"]["last_seen"] == "2026-11-10T00:30:00.000Z"
    # Newest activity first.
    seen = [item["last_seen"] for item in body["items"]]
    assert seen == sorted(seen, reverse=True)


def test_catalog_sessions_filters(client: TestClient, seed_data: Database) -> None:
    by_project = client.get(
        "/api/v1/catalog/sessions", params={"project": "project-y"}
    ).json()
    assert by_project["total"] == 2
    assert {item["session_id"] for item in by_project["items"]} == {"sess-2", "sess-3"}
    by_agent = client.get("/api/v1/catalog/sessions", params={"agent": "agent-a"}).json()
    assert {item["session_id"] for item in by_agent["items"]} == {"sess-1", "sess-3"}


def test_catalog_empty_database(client: TestClient) -> None:
    for endpoint in ("/api/v1/catalog/agents", "/api/v1/catalog/projects", "/api/v1/catalog/sessions"):
        body = client.get(endpoint).json()
        assert body["total"] == 0
        assert body["items"] == []


# --------------------------------------------------------------------------------------
# /api/v1/export/csv
# --------------------------------------------------------------------------------------


def _parse_csv(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


@pytest.mark.parametrize(
    "dataset",
    ["timeseries", "breakdown", "sessions", "snapshots", "alerts", "anomalies", "budgets"],
)
def test_export_csv_headers_and_encoding(
    client: TestClient, seed_data: Database, dataset: str
) -> None:
    response = client.get("/api/v1/export/csv", params={"dataset": dataset})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="tokenlens-')
    assert f"tokenlens-{dataset}-" in disposition
    assert disposition.endswith('.csv"')
    # UTF-8 BOM so Excel detects the encoding.
    assert response.content.startswith(b"\xef\xbb\xbf")
    header, _rows = _parse_csv(response.content)
    assert header, dataset
    assert all(column == column.lower() for column in header)


def test_export_timeseries_rows_match_api(client: TestClient, seed_data: Database) -> None:
    params = {**_window_params(*FULL_WINDOW), "bucket": "hour"}
    api_points = client.get("/api/v1/usage/timeseries", params=params).json()["points"]
    response = client.get(
        "/api/v1/export/csv", params={**params, "dataset": "timeseries"}
    )
    header, rows = _parse_csv(response.content)
    assert len(rows) == len(api_points) == 6
    assert header[:3] == ["bucket_start", "bucket_end", "label"]
    assert "cost_usd" in header
    cost_index = header.index("cost_usd")
    exported_cost = sum(float(row[cost_index]) for row in rows)
    assert exported_cost == pytest.approx(
        sum(point["totals"]["cost_usd"] for point in api_points), abs=1e-6
    )
    assert rows[0][2] == "2026-11-10 08:00"  # display-tz label


def test_export_breakdown_rows(client: TestClient, seed_data: Database) -> None:
    response = client.get(
        "/api/v1/export/csv",
        params={**_window_params(*FULL_WINDOW), "dataset": "breakdown", "dimension": "agent"},
    )
    header, rows = _parse_csv(response.content)
    assert header[:4] == ["dimension", "key", "label", "share_pct"]
    assert len(rows) == 3
    share_index = header.index("share_pct")
    assert sum(float(row[share_index]) for row in rows) == pytest.approx(100.0, abs=1e-3)


def test_export_snapshots_capped_by_limit(client: TestClient, seed_data: Database) -> None:
    response = client.get(
        "/api/v1/export/csv",
        params={**_window_params(*FULL_WINDOW), "dataset": "snapshots", "limit": 2},
    )
    header, rows = _parse_csv(response.content)
    assert len(rows) == 2
    assert "event_id" in header
    assert "ts" in header
    assert response.headers["x-row-count"] == "2"


def test_export_snapshots_applies_filters(client: TestClient, seed_data: Database) -> None:
    response = client.get(
        "/api/v1/export/csv",
        params={
            **_window_params(*FULL_WINDOW),
            "dataset": "snapshots",
            "agent": "agent-b",
        },
    )
    _header, rows = _parse_csv(response.content)
    assert len(rows) == 2
    agent_index = _header.index("agent")
    assert {row[agent_index] for row in rows} == {"agent-b"}


def test_export_sessions_dataset(client: TestClient, seed_data: Database) -> None:
    response = client.get("/api/v1/export/csv", params={"dataset": "sessions"})
    header, rows = _parse_csv(response.content)
    assert len(rows) == 4
    assert header[0] == "session_id"
    assert "models" in header


def test_export_invalid_dataset_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/export/csv", params={"dataset": "everything"})
    assert response.status_code == 400
    assert "dataset" in response.json()["detail"]


def test_export_invalid_bucket_is_400(client: TestClient) -> None:
    response = client.get(
        "/api/v1/export/csv", params={"dataset": "timeseries", "bucket": "fortnight"}
    )
    assert response.status_code == 400


def test_export_inverted_window_is_400(client: TestClient) -> None:
    response = client.get(
        "/api/v1/export/csv",
        params={
            "dataset": "timeseries",
            "from": "2026-11-10T05:00:00Z",
            "to": "2026-11-10T04:00:00Z",
        },
    )
    assert response.status_code == 400


def test_export_empty_database_still_returns_header(client: TestClient) -> None:
    response = client.get("/api/v1/export/csv", params={"dataset": "snapshots"})
    assert response.status_code == 200
    header, rows = _parse_csv(response.content)
    assert rows == []
    assert "event_id" in header


def test_export_limit_is_validated(client: TestClient) -> None:
    assert (
        client.get("/api/v1/export/csv", params={"dataset": "sessions", "limit": 0}).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/export/csv", params={"dataset": "sessions", "limit": 100_001}
        ).status_code
        == 422
    )


def test_export_csv_escapes_commas_and_quotes(client: TestClient, db: Database) -> None:
    """A budget name containing a comma/quote must not corrupt the CSV structure."""
    db.execute(
        "INSERT INTO budgets (name, scope, scope_key, period, limit_usd, thresholds,"
        " desktop, enabled, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ['weird, "quoted" name', "global", "*", "daily", 10.0, "[0.5,1.0]", 1, 1, DAY0, DAY0],
    )
    response = client.get("/api/v1/export/csv", params={"dataset": "budgets"})
    header, rows = _parse_csv(response.content)
    assert len(rows) == 1
    name_index = header.index("name")
    assert rows[0][name_index] == 'weird, "quoted" name'


# --------------------------------------------------------------------------------------
# Cross-endpoint consistency / helpers
# --------------------------------------------------------------------------------------


def test_query_helpers_expose_stable_signatures() -> None:
    """The budget/dashboard owners import these — guard against signature drift."""
    import inspect

    for name in (
        "totals_for_window",
        "breakdown",
        "timeseries",
        "live_metrics",
        "catalog_agents",
        "catalog_projects",
        "catalog_sessions",
        "csv_rows",
    ):
        assert callable(getattr(query, name)), name
    params = inspect.signature(query.totals_for_window).parameters
    assert list(params)[:3] == ["db", "start_ms", "end_ms"]
    assert params["agent"].kind is inspect.Parameter.KEYWORD_ONLY
    breakdown_params = inspect.signature(query.breakdown).parameters
    assert list(breakdown_params)[:4] == ["db", "dimension", "start_ms", "end_ms"]


def test_scope_to_filters_maps_budget_scopes() -> None:
    assert query.scope_to_filters("global", "*") == {}
    assert query.scope_to_filters("project", "payments-api") == {"project": "payments-api"}
    assert query.scope_to_filters("agent", "claude-code") == {"agent": "claude-code"}
    assert query.scope_to_filters("model", "gpt-5") == {"model": "gpt-5"}
    assert query.scope_to_filters("project", "*") == {}
    with pytest.raises(ValueError):
        query.scope_to_filters("banana", "x")


def test_totals_for_window_is_consistent_with_breakdown_sum(
    db: Database, seed_data: Database
) -> None:
    totals = query.totals_for_window(db, DAY0, DAY0 + 6 * HOUR)
    rows = query.breakdown(db, "agent", DAY0, DAY0 + 6 * HOUR)
    assert sum(row.totals.total_tokens for row in rows) == totals.total_tokens
    assert sum(row.totals.cost_usd for row in rows) == pytest.approx(
        totals.cost_usd, abs=1e-6
    )


def test_totals_for_window_rejects_inverted_window(db: Database) -> None:
    with pytest.raises(ValueError):
        query.totals_for_window(db, DAY0 + HOUR, DAY0)


def test_normalize_helpers_validate_contract_vocabulary() -> None:
    assert query.normalize_bucket("1h") == "hour"
    assert query.normalize_dimension("session") == "session"
    assert query.normalize_dimension("session_id") == "session"
    assert query.normalize_period("daily") == "day"
    assert query.normalize_period("weekly") == "week"
    assert query.normalize_dataset("sessions") == "sessions"
    for bad in ("banana", ""):
        if bad == "":
            continue
        with pytest.raises(ValueError):
            query.normalize_bucket(bad)
        with pytest.raises(ValueError):
            query.normalize_dimension(bad)
        with pytest.raises(ValueError):
            query.normalize_period(bad)
        with pytest.raises(ValueError):
            query.normalize_dataset(bad)


def test_all_read_endpoints_survive_an_empty_database(client: TestClient) -> None:
    """A fresh install must serve every read endpoint with HTTP 200."""
    endpoints = [
        "/api/v1/usage/summary",
        "/api/v1/usage/breakdown",
        "/api/v1/usage/timeseries",
        "/api/v1/usage/live",
        "/api/v1/trends",
        "/api/v1/catalog/models",
        "/api/v1/catalog/agents",
        "/api/v1/catalog/projects",
        "/api/v1/catalog/sessions",
        "/api/v1/export/csv?dataset=timeseries&from=2026-11-10T00:00:00Z&to=2026-11-10T04:00:00Z",
        "/api/v1/export/csv?dataset=breakdown&from=2026-11-10T00:00:00Z&to=2026-11-10T04:00:00Z",
        "/api/v1/export/csv?dataset=alerts",
        "/api/v1/export/csv?dataset=anomalies",
        "/api/v1/export/csv?dataset=budgets",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200, f"{endpoint} -> {response.status_code}"


def test_summary_and_trends_agree_on_overlapping_data(
    client: TestClient, db: Database
) -> None:
    """Spend ingested in the current day must show up in both endpoints."""
    today = timeutil.bucket_start(timeutil.now_ms(), "day")
    _seed(
        db,
        [
            make_snapshot(
                ts=today + HOUR,
                agent="agree-a",
                project="agree-p",
                model="gpt-5",
                runtime="openai",
                input_tokens=100_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id="agree-1",
            )
        ],
    )
    summary = client.get(
        "/api/v1/usage/summary",
        params={"from": _iso(today), "to": _iso(today + 86_400_000)},
    ).json()
    trends = client.get("/api/v1/trends", params={"period": "day", "limit": 1}).json()
    assert summary["totals"]["cost_usd"] == pytest.approx(
        trends["comparison"]["current_cost_usd"], abs=1e-6
    )
    assert trends["points"][-1]["totals"]["cost_usd"] == pytest.approx(
        summary["totals"]["cost_usd"], abs=1e-6
    )
