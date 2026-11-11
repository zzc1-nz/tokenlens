"""Alert history endpoints (contract §7).

Alerts are written by the budget engine (``POST /api/v1/budgets/evaluate`` and the
background scheduler); this module only reads them, so it is safe to poll from the web
dashboard every few seconds.

Route order matters: ``/summary`` is registered before ``/{alert_id}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import budget as budget_module
from .. import timeutil
from ..db import Database, get_db
from ..schemas import AlertListResponse, AlertRecord, AlertSummary

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

MAX_LIMIT = 1000


def _parse_since(since: str | None) -> int | None:
    """``since`` accepts ISO-8601 or epoch ms; garbage becomes a 422."""
    if since in (None, ""):
        return None
    try:
        return timeutil.parse_time_ms(since)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid 'since' timestamp: {since!r}",
        ) from exc


@router.get("", response_model=AlertListResponse, summary="List alert history")
def list_alerts(
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    severity: str | None = Query(default=None, description="warning|critical|exceeded"),
    budget_id: int | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO-8601 or epoch ms"),
    db: Database = Depends(get_db),
) -> AlertListResponse:
    """Newest first. ``since`` filters on ``created_at`` (when the breach was first seen)."""
    if severity and severity not in budget_module.SEVERITIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"severity must be one of {list(budget_module.SEVERITIES)}",
        )
    total, items = budget_module.list_alerts(
        db,
        limit=limit,
        offset=offset,
        severity=severity,
        budget_id=budget_id,
        since_ms=_parse_since(since),
    )
    return AlertListResponse(total=total, items=items)


@router.get("/summary", response_model=AlertSummary, summary="Alert statistics")
def alert_summary(
    since: str | None = Query(default=None, description="ISO-8601 or epoch ms"),
    db: Database = Depends(get_db),
) -> AlertSummary:
    """Counts by severity and budget, plus de-duplicated total overspend."""
    return budget_module.alert_summary(
        db, now_ms=timeutil.now_ms(), since_ms=_parse_since(since)
    )


@router.get("/{alert_id}", response_model=AlertRecord, summary="Get one alert")
def get_alert(alert_id: int, db: Database = Depends(get_db)) -> AlertRecord:
    """404 when the alert does not exist."""
    record = budget_module.get_alert(db, alert_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"alert {alert_id} not found"
        )
    return record
