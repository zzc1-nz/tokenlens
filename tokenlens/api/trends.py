"""Trend endpoints: period-over-period spend trajectory.

``/api/v1/trends`` answers the question a cost dashboard exists for: *"is my spend going
up, and by how much compared to the previous period?"*

It returns the last ``limit`` buckets of the requested period (inclusive of the bucket
that contains "now"), gap-filled, plus a comparison block against the immediately
preceding ``limit`` buckets. Buckets are calendar aligned in UTC and labelled in the
server's display timezone, so "today" for a Shanghai user starts at 00:00 +08:00.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .. import query, timeutil
from ..db import Database, get_db
from ..schemas import TimePoint, Totals, TrendComparison, TrendsResponse, Window

router = APIRouter(prefix="/api/v1/trends", tags=["trends"])


def _display_tz(request: Request) -> str:
    """Display timezone for bucket labels, from ``app.state.settings``."""
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "display_tz", None) or query.DEFAULT_TZ


def _pct(current: float, previous: float) -> float:
    """Percentage change, defined as ``0`` when there is no baseline to compare against.

    Reporting ``+inf%`` (or ``+100%`` against zero) would be noise: the UI shows "no
    baseline yet" instead. Negative baselines cannot occur for cost/tokens.
    """
    if previous <= 0:
        return 0.0
    return round((current - previous) / previous * 100.0, 6)


def _comparison(points: list[TimePoint], previous: Totals) -> TrendComparison:
    """Build the comparison block from the current points and the previous window totals."""
    current = Totals.combine([point.totals for point in points])
    return TrendComparison(
        previous_cost_usd=round(previous.cost_usd, 6),
        current_cost_usd=round(current.cost_usd, 6),
        delta_usd=round(current.cost_usd - previous.cost_usd, 6),
        delta_pct=_pct(current.cost_usd, previous.cost_usd),
        previous_tokens=previous.total_tokens,
        current_tokens=current.total_tokens,
        token_delta_pct=_pct(float(current.total_tokens), float(previous.total_tokens)),
    )


@router.get("", response_model=TrendsResponse, summary="Spend trend with period comparison")
@router.get("/", response_model=TrendsResponse, include_in_schema=False)
def trends(
    request: Request,
    db: Database = Depends(get_db),
    period: str = Query(default="day", description="day|week|month"),
    limit: int = Query(default=30, ge=1, le=1000, description="number of buckets"),
    agent: str | None = Query(default=None),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
) -> TrendsResponse:
    """Return the last ``limit`` buckets of ``period`` and a comparison vs the previous run.

    The window ends at the bucket currently in progress (inclusive), which is what a
    "last 7 days" chart is expected to show. The comparison window is the equally long run
    of buckets immediately before it, so the two ranges never overlap.
    """
    bucket = query.normalize_period(period)
    filters = query.normalize_filters(
        agent=agent, project=project, model=model, runtime=runtime, session_id=session_id
    )

    start, end = query.last_n_buckets(bucket, limit)
    points = query.timeseries(
        db, bucket, start, end, tz_name=_display_tz(request), **filters
    )

    prev_start, prev_end = query.window_before(bucket, start, limit)
    previous = query.totals_for_window(db, prev_start, prev_end, **filters)

    return TrendsResponse(
        period=bucket,
        window=Window(**{"from": timeutil.ms_to_dt(start), "to": timeutil.ms_to_dt(end)}),
        points=points,
        comparison=_comparison(points, previous),
        filters=filters,
    )
