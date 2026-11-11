"""Usage query endpoints: summary, breakdown, timeseries and live burn rate.

These are the read path of the observability pipeline. All four endpoints share the same
filter vocabulary (``agent``/``project``/``model``/``runtime``/``session_id`` plus the
``from``/``to`` window) and delegate the SQL to :mod:`tokenlens.query`, so a number shown
in ``/usage/summary`` is by construction consistent with the breakdown, the time series and
the CSV export.

Contract violations are signalled with ``ValueError``; the application registers a
``ValueError -> 400`` handler (see :func:`tokenlens.app.create_app`), which keeps the
routers free of repetitive ``HTTPException`` plumbing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .. import query, timeutil
from ..db import Database, get_db
from ..schemas import (
    BreakdownResponse,
    LiveResponse,
    TimeSeriesResponse,
    UsageSummary,
    Window,
)

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


def _display_tz(request: Request) -> str:
    """Display timezone for bucket labels, from ``app.state.settings``."""
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "display_tz", None) or query.DEFAULT_TZ


def _window(start: int, end: int) -> Window:
    """Build the response ``window`` object from an epoch-ms pair."""
    return Window(**{"from": timeutil.ms_to_dt(start), "to": timeutil.ms_to_dt(end)})


def _filters(**kwargs: Any) -> dict[str, str]:
    """Echo only the filters the caller actually supplied."""
    return query.normalize_filters(**kwargs)


# --------------------------------------------------------------------------------------
# GET /api/v1/usage/summary
# --------------------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=UsageSummary,
    summary="Aggregate token/cost totals for a window",
)
def usage_summary(
    request: Request,
    db: Database = Depends(get_db),
    from_: str | None = Query(default=None, alias="from", description="ISO-8601 or epoch ms"),
    to: str | None = Query(default=None, description="ISO-8601 or epoch ms; defaults to now"),
    agent: str | None = Query(default=None),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    default_hours: float = Query(default=24.0, gt=0, le=24 * 366),
) -> UsageSummary:
    """Return the canonical :class:`~tokenlens.schemas.Totals` for the requested window.

    All filters combine with ``AND``. An empty database returns zeroed totals with HTTP
    200 — a monitoring API must never fail because nothing happened yet.
    """
    start, end = query.resolve_window(from_, to, default_hours=default_hours)
    filters = _filters(
        agent=agent, project=project, model=model, runtime=runtime, session_id=session_id
    )
    totals = query.totals_for_window(db, start, end, **filters)
    return UsageSummary(window=_window(start, end), totals=totals, filters=filters)


# --------------------------------------------------------------------------------------
# GET /api/v1/usage/breakdown
# --------------------------------------------------------------------------------------


@router.get(
    "/breakdown",
    response_model=BreakdownResponse,
    summary="Group usage by a dimension",
)
def usage_breakdown(
    request: Request,
    db: Database = Depends(get_db),
    dimension: str = Query(default="agent", description="agent|project|model|runtime|session"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    default_hours: float = Query(default=24.0, gt=0, le=24 * 366),
) -> BreakdownResponse:
    """Break the window down by ``dimension``, sorted by cost descending.

    ``share_pct`` is relative to the window total (not to the returned page) so a limited
    result still reports honest percentages.
    """
    start, end = query.resolve_window(from_, to, default_hours=default_hours)
    filters = _filters(
        agent=agent, project=project, model=model, runtime=runtime, session_id=session_id
    )
    rows = query.breakdown(db, dimension, start, end, limit=limit, offset=offset, **filters)
    total = query.totals_for_window(db, start, end, **filters)
    return BreakdownResponse(
        dimension=query.normalize_dimension(dimension),
        window=_window(start, end),
        rows=rows,
        total=total,
    )


# --------------------------------------------------------------------------------------
# GET /api/v1/usage/timeseries
# --------------------------------------------------------------------------------------


@router.get(
    "/timeseries",
    response_model=TimeSeriesResponse,
    summary="Gap-filled time series for a bucket size",
)
def usage_timeseries(
    request: Request,
    db: Database = Depends(get_db),
    bucket: str = Query(default="hour", description="minute|hour|day|week|month"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    default_hours: float = Query(default=24.0, gt=0, le=24 * 366),
) -> TimeSeriesResponse:
    """Return ascending, gap-filled points covering the whole window.

    Buckets without traffic are present with zeroed totals so charts render a continuous
    baseline instead of interpolating over a gap.
    """
    canonical_bucket = query.normalize_bucket(bucket)
    start, end = query.resolve_window(from_, to, default_hours=default_hours)
    filters = _filters(
        agent=agent, project=project, model=model, runtime=runtime, session_id=session_id
    )
    points = query.timeseries(
        db, canonical_bucket, start, end, tz_name=_display_tz(request), **filters
    )
    return TimeSeriesResponse(
        bucket=canonical_bucket, window=_window(start, end), points=points
    )


# --------------------------------------------------------------------------------------
# GET /api/v1/usage/live
# --------------------------------------------------------------------------------------


@router.get(
    "/live",
    response_model=LiveResponse,
    summary="Burn rate over the trailing window (HUD polling endpoint)",
)
def usage_live(
    request: Request,
    db: Database = Depends(get_db),
    window_minutes: float = Query(default=5.0, gt=0, le=24 * 60),
) -> LiveResponse:
    """Tokens/minute, USD/hour, active entities and top spenders of the trailing window.

    Computed from raw snapshots because the window is usually smaller than the smallest
    rollup bucket — a rollup based number would be either zero or stale.
    """
    metrics = query.live_metrics(db, window_minutes)
    return LiveResponse(**metrics)
