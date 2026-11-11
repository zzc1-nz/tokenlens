"""Single-call dashboard aggregation (contract §9).

The web dashboard polls one endpoint every three seconds; assembling six panels from six
round trips would be both slower and racy (each panel would show a slightly different
"now"). ``GET /api/v1/dashboard/overview`` therefore returns everything the first screen
needs in one consistent snapshot:

* ``kpis``        — today/week/month cost, live burn rate, cache hit rate, active counts
* ``breakdowns``  — by agent / project / model / runtime (cost share)
* ``timeseries``  — hourly (last 24 h) and daily (last ``days``) points
* ``budgets``     — every budget with its live status
* ``alerts``      — recent alerts + summary
* ``anomalies``   — recent anomalies + summary

**Degradation is a feature, not an afterthought**: an empty database returns zeros and
empty lists with HTTP 200, because the web UI's first render happens before any data has
been ingested. Every section is also individually guarded — if one panel's SQL fails, the
others still render, and the failure is logged.

The aggregation helpers live in :mod:`tokenlens.query` (owned by the query-api teammate).
They are imported lazily at call time and used when present; otherwise the local SQL
fallbacks below are used. Both paths produce identical JSON because both are built on
``Totals.from_mapping`` / ``BreakdownRow`` / ``TimePoint``. The fallback exists so the
dashboard never hard-depends on another module's import order (and so this file is
self-contained if the query layer is refactored).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from fastapi import APIRouter, Depends, Query, Request

from .. import anomaly as anomaly_module
from .. import budget as budget_module
from .. import pricing, timeutil
from ..config import Settings, get_settings
from ..db import Database, build_filters, get_db
from ..schemas import (
    BreakdownRow,
    DashboardKpis,
    DashboardOverview,
    TimePoint,
    Totals,
)

logger = logging.getLogger(__name__)


def _query_helpers() -> Any:
    """The shared query layer, or ``None`` when it is unavailable.

    Resolved per call rather than at import time so that a partially implemented
    ``tokenlens.query`` can never break the dashboard at import.
    """
    try:
        from .. import query as query_module  # local import: optional dependency

        return query_module
    except Exception:  # noqa: BLE001 - the local fallback is the safety net
        logger.debug("tokenlens.query unavailable; using dashboard-local SQL", exc_info=True)
        return None

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

#: Dimensions exposed in ``breakdowns``, keyed exactly as contract §9 specifies
#: (``by_agent`` / ``by_project`` / ``by_model`` / ``by_runtime``).
BREAKDOWN_KEYS: tuple[str, ...] = ("by_agent", "by_project", "by_model", "by_runtime")

#: Bare dimension names, derived once so the two representations cannot drift.
BREAKDOWN_DIMENSIONS: tuple[str, ...] = tuple(
    key.removeprefix("by_") for key in BREAKDOWN_KEYS
)

#: How many rows each breakdown panel returns (the UI shows a scrollable top-N table).
BREAKDOWN_LIMIT = 12

#: Hourly series length (a day of history fits the sparkline comfortably).
HOURLY_HOURS = 24

#: ``recent`` list sizes.
RECENT_ALERTS = 10
RECENT_ANOMALIES = 10

#: Rollup only trustworthy for windows that ended at least this long ago.
ROLLUP_LAG_MS = 3_600_000


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


# --------------------------------------------------------------------------------------
# Local SQL fallbacks (used when tokenlens.query is unavailable)
# --------------------------------------------------------------------------------------

_AGGREGATE_COLUMNS = """
       COALESCE(SUM(s.input_tokens), 0)        AS input_tokens,
       COALESCE(SUM(s.output_tokens), 0)       AS output_tokens,
       COALESCE(SUM(s.cache_read_tokens), 0)   AS cache_read_tokens,
       COALESCE(SUM(s.cache_write_tokens), 0)  AS cache_write_tokens,
       COALESCE(SUM(s.reasoning_tokens), 0)    AS reasoning_tokens,
       COALESCE(SUM(s.requests), 0)            AS requests,
       COALESCE(SUM(s.retries), 0)             AS retries,
       COALESCE(SUM(s.tool_calls), 0)          AS tool_calls,
       COALESCE(SUM(s.cost_usd), 0)            AS cost_usd,
       COUNT(DISTINCT s.session_id)            AS sessions,
       COUNT(DISTINCT s.agent)                 AS agents,
       COUNT(DISTINCT s.project)               AS projects
"""


def _totals_for_window(
    db: Database,
    start_ms: int,
    end_ms: int,
    **filters: Any,
) -> Totals:
    """Aggregate one window into :class:`~tokenlens.schemas.Totals`.

    Prefers :func:`tokenlens.query.totals_for_window` so the dashboard and the usage API
    can never disagree about a number; falls back to equivalent local SQL.
    """
    query_module = _query_helpers()
    if query_module is not None:
        try:
            return query_module.totals_for_window(db, int(start_ms), int(end_ms), **filters)
        except Exception:  # noqa: BLE001 - fall through to the local implementation
            logger.debug("query.totals_for_window failed; using local SQL", exc_info=True)
    where, params = build_filters(start_ms=int(start_ms), end_ms=int(end_ms), **filters)
    row = db.fetch_one(f"SELECT {_AGGREGATE_COLUMNS} FROM usage_snapshots s{where}", params)
    if row is None:
        return Totals.zero()
    return Totals.from_mapping(dict(row))


def _breakdown_rows(
    db: Database,
    dimension: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = BREAKDOWN_LIMIT,
) -> list[BreakdownRow]:
    """Top-N rows of one dimension by cost, with ``share_pct`` of the window's cost."""
    if dimension not in BREAKDOWN_DIMENSIONS:
        raise ValueError(f"unsupported dimension: {dimension!r}")
    query_module = _query_helpers()
    if query_module is not None:
        try:
            return query_module.breakdown(
                db, dimension, int(start_ms), int(end_ms), limit=int(limit)
            )
        except Exception:  # noqa: BLE001 - fall through to the local implementation
            logger.debug("query.breakdown failed; using local SQL", exc_info=True)
    where, params = build_filters(start_ms=int(start_ms), end_ms=int(end_ms))
    rows = db.fetch_all(
        f"""
        SELECT s.{dimension} AS key, {_AGGREGATE_COLUMNS}
          FROM usage_snapshots s{where}
         GROUP BY s.{dimension}
         ORDER BY cost_usd DESC, key ASC
         LIMIT ?
        """,
        [*params, int(limit)],
    )
    total_row = db.fetch_one(
        f"SELECT COALESCE(SUM(s.cost_usd), 0) AS cost FROM usage_snapshots s{where}", params
    )
    total_cost = float(total_row["cost"] or 0.0) if total_row is not None else 0.0

    out: list[BreakdownRow] = []
    for row in rows:
        mapping = dict(row)
        totals = Totals.from_mapping(mapping)
        share = round(totals.cost_usd / total_cost * 100.0, 4) if total_cost > 0 else 0.0
        key = str(mapping.get("key") or "unknown")
        out.append(BreakdownRow(key=key, label=key, totals=totals, share_pct=share))
    return out


def _timeseries(
    db: Database,
    bucket: str,
    start_ms: int,
    end_ms: int,
    *,
    tz_name: str | None = None,
) -> list[TimePoint]:
    """Gap-filled ascending time series (zero buckets are emitted, not skipped)."""
    query_module = _query_helpers()
    if query_module is not None:
        try:
            return query_module.timeseries(
                db, bucket, int(start_ms), int(end_ms), tz_name=tz_name or get_settings().display_tz
            )
        except Exception:  # noqa: BLE001 - fall through to the local implementation
            logger.debug("query.timeseries failed; using local SQL", exc_info=True)
    where, params = build_filters(start_ms=int(start_ms), end_ms=int(end_ms))
    bucket_ms = timeutil.BUCKET_MS[bucket]
    rows = db.fetch_all(
        f"""
        SELECT (s.ts / ?) * ? AS bucket_ms, {_AGGREGATE_COLUMNS}
          FROM usage_snapshots s{where}
         GROUP BY bucket_ms
         ORDER BY bucket_ms ASC
        """,
        [bucket_ms, bucket_ms, *params],
    )
    by_bucket = {int(row["bucket_ms"]): Totals.from_mapping(dict(row)) for row in rows}
    points: list[TimePoint] = []
    for bucket_start in timeutil.iter_buckets(int(start_ms), int(end_ms), bucket):
        totals = by_bucket.get(bucket_start, Totals.zero())
        points.append(
            TimePoint(
                bucket_start=timeutil.ms_to_dt(bucket_start),
                bucket_end=timeutil.ms_to_dt(timeutil.bucket_end(bucket_start, bucket)),
                label=timeutil.label_for(bucket_start, bucket, tz_name or get_settings().display_tz),
                totals=totals,
            )
        )
    return points


def _live_metrics(db: Database, window_minutes: float = 5.0) -> dict[str, float]:
    """Trailing burn rate from raw snapshots (tokens/min and USD/hour)."""
    query_module = _query_helpers()
    if query_module is not None:
        try:
            live = query_module.live_metrics(db, window_minutes)
            return {
                "tokens_per_min": float(live.get("tokens_per_min") or 0.0),
                "cost_per_hour": float(live.get("cost_per_hour") or 0.0),
            }
        except Exception:  # noqa: BLE001 - fall through to the local implementation
            logger.debug("query.live_metrics failed; using local SQL", exc_info=True)
    now = timeutil.now_ms()
    start = now - int(window_minutes * 60_000)
    where, params = build_filters(start_ms=start, end_ms=now)
    row = db.fetch_one(
        f"""
        SELECT COALESCE(SUM(s.input_tokens + s.output_tokens + s.cache_read_tokens
                           + s.cache_write_tokens + s.reasoning_tokens), 0) AS tokens,
               COALESCE(SUM(s.cost_usd), 0) AS cost
          FROM usage_snapshots s{where}
        """,
        params,
    )
    tokens = float(row["tokens"] or 0.0) if row is not None else 0.0
    cost = float(row["cost"] or 0.0) if row is not None else 0.0
    return {
        "tokens_per_min": round(tokens / max(1e-9, float(window_minutes)), 3),
        "cost_per_hour": round(cost * (60.0 / max(1e-9, float(window_minutes))), 6),
    }


# --------------------------------------------------------------------------------------
# Section builders (each individually guarded by the caller)
# --------------------------------------------------------------------------------------


def _kpis(db: Database, settings: Settings, now: int) -> DashboardKpis:
    """KPI cards for the overview header."""
    today_start, today_end = timeutil.period_window("daily", now, settings.display_tz)
    week_start, week_end = timeutil.period_window("weekly", now, settings.display_tz)
    month_start, month_end = timeutil.period_window("monthly", now, settings.display_tz)

    today = _totals_for_window(db, today_start, today_end)
    week = _totals_for_window(db, week_start, week_end)
    month = _totals_for_window(db, month_start, month_end)
    live = _live_metrics(db, 5.0)
    trailing = _totals_for_window(db, now - 3_600_000, now)

    return DashboardKpis(
        today_cost_usd=today.cost_usd,
        today_tokens=today.total_tokens,
        week_cost_usd=week.cost_usd,
        month_cost_usd=month.cost_usd,
        burn_rate_tokens_per_min=live["tokens_per_min"],
        burn_rate_usd_per_hour=live["cost_per_hour"],
        cache_hit_rate=trailing.cache_hit_rate,
        active_agents=trailing.agents,
        active_projects=trailing.projects,
        total_sessions=int(db.scalar("SELECT COUNT(DISTINCT session_id) FROM usage_snapshots", default=0) or 0),
    )


def _breakdowns(db: Database, now: int, days: int) -> dict[str, list[BreakdownRow]]:
    """All four breakdown panels, keyed ``by_<dimension>`` per contract §9."""
    start = now - days * 86_400_000
    return {
        key: _breakdown_rows(db, key.removeprefix("by_"), start, now)
        for key in BREAKDOWN_KEYS
    }


def _timeseries_section(db: Database, now: int, days: int) -> dict[str, list[TimePoint]]:
    """Hourly (last 24 buckets) and daily (last ``days`` buckets) series.

    The window ends at the **end of the bucket containing ``now``**, not at ``now``
    itself. ``_timeseries`` takes an exclusive ``end_ms``, so passing ``now`` would drop
    the current bucket whenever the clock happens to sit exactly on a boundary — giving
    23 hourly points at 06:00:00 and 24 at 06:00:01. Anchoring to the bucket end keeps the
    point count fixed and always includes the in-progress bucket, which is what a live
    sparkline should show.
    """
    hourly_end = timeutil.bucket_end(timeutil.bucket_start(now, "hour"), "hour")
    hourly_start = hourly_end - HOURLY_HOURS * 3_600_000
    daily_end = timeutil.bucket_end(timeutil.bucket_start(now, "day"), "day")
    daily_start = daily_end - max(1, days) * 86_400_000
    return {
        "hourly": _timeseries(db, "hour", hourly_start, hourly_end),
        "daily": _timeseries(db, "day", daily_start, daily_end),
    }


def _budgets_section(db: Database, settings: Settings, now: int) -> list[Any]:
    return budget_module.list_budget_statuses(db, settings=settings, now_ms=now)


def _alerts_section(db: Database, now: int) -> dict[str, Any]:
    _, recent = budget_module.list_alerts(db, limit=RECENT_ALERTS)
    summary = budget_module.alert_summary(db, now_ms=now)
    return {"recent": recent, "summary": summary}


def _anomalies_section(db: Database) -> dict[str, Any]:
    _, recent = anomaly_module.list_anomalies(db, limit=RECENT_ANOMALIES)
    summary = anomaly_module.anomaly_summary(db)
    return {"recent": recent, "summary": summary}


# --------------------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------------------


@router.get("/overview", response_model=DashboardOverview, summary="Everything the dashboard needs")
def overview(
    request: Request,
    days: int = Query(default=7, ge=1, le=90, description="Trailing window for breakdowns and the daily series"),
    db: Database = Depends(get_db),
) -> DashboardOverview:
    """One consistent snapshot of the whole platform, safe to poll every few seconds.

    Each section degrades independently: an empty database yields zeros and empty lists,
    and a failing section is logged and replaced by its empty value instead of returning
    a 500 for the entire dashboard.
    """
    settings = _settings(request)
    now = timeutil.now_ms()

    try:
        kpis = _kpis(db, settings, now)
    except Exception:  # noqa: BLE001 - a broken panel must not blank the dashboard
        logger.exception("dashboard: KPI section failed")
        kpis = DashboardKpis()

    try:
        breakdowns = _breakdowns(db, now, days)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: breakdown section failed")
        breakdowns = {key: [] for key in BREAKDOWN_KEYS}

    try:
        timeseries = _timeseries_section(db, now, days)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: timeseries section failed")
        timeseries = {"hourly": [], "daily": []}

    try:
        budgets = _budgets_section(db, settings, now)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: budget section failed")
        budgets = []

    try:
        alerts = _alerts_section(db, now)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: alert section failed")
        alerts = {"recent": [], "summary": budget_module.AlertSummary()}

    try:
        anomalies = _anomalies_section(db)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: anomaly section failed")
        anomalies = {"recent": [], "summary": anomaly_module.AnomalySummary()}

    return DashboardOverview(
        generated_at=timeutil.ms_to_dt(now),
        kpis=kpis,
        breakdowns=breakdowns,
        timeseries=timeseries,
        budgets=budgets,
        alerts=alerts,
        anomalies=anomalies,
    )


@router.get("/health", summary="Dashboard data availability probe")
def dashboard_health(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Cheap probe the web UI uses to decide between "loading" and "no data yet"."""
    counts = db.table_counts()
    return {
        "ok": True,
        "snapshots": counts.get("usage_snapshots", 0),
        "budgets": counts.get("budgets", 0),
        "alerts": counts.get("alert_events", 0),
        "anomalies": counts.get("anomaly_events", 0),
        "price_version": pricing.PRICE_VERSION,
        "now": timeutil.iso(timeutil.now_ms()),
    }
