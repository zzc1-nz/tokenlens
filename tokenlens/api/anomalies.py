"""Anomaly detection and history endpoints (contract §8).

``POST /anomalies/detect`` runs the robust z-score detector and persists new events;
everything else reads the ``anomaly_events`` table. Route order matters: ``/detect`` and
``/summary`` are registered before ``/{anomaly_id}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import anomaly as anomaly_module
from ..config import Settings, get_settings
from ..db import Database, get_db
from ..schemas import (
    AnomalyListResponse,
    AnomalyPatch,
    AnomalyRecord,
    AnomalySummary,
    DetectResponse,
)

router = APIRouter(prefix="/api/v1/anomalies", tags=["anomalies"])

MAX_LIMIT = 1000


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


@router.post("/detect", response_model=DetectResponse, summary="Run one anomaly detection pass")
def detect(
    request: Request,
    lookback_minutes: float = Query(default=60.0, gt=0, le=1440, description="Observation window"),
    persist: bool = Query(default=True, description="Persist new events (false = dry run)"),
    db: Database = Depends(get_db),
) -> DetectResponse:
    """Score every agent active in the trailing window and classify any root cause.

    Detection is deterministic for a given database state: the same data always produces
    the same score, severity and reason.
    """
    settings = _settings(request)
    return anomaly_module.detect(
        db, settings=settings, lookback_minutes=lookback_minutes, persist=persist
    )


@router.get("", response_model=AnomalyListResponse, summary="List anomalies")
def list_anomalies(
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status", description="open|acknowledged|resolved"),
    severity: str | None = Query(default=None, description="low|medium|high|critical"),
    agent: str | None = Query(default=None),
    project: str | None = Query(default=None),
    db: Database = Depends(get_db),
) -> AnomalyListResponse:
    """Newest first, optionally filtered by triage status / severity / agent / project."""
    if status_filter and status_filter not in anomaly_module.STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {list(anomaly_module.STATUSES)}",
        )
    total, items = anomaly_module.list_anomalies(
        db,
        limit=limit,
        offset=offset,
        status=status_filter,
        severity=severity,
        agent=agent,
        project=project,
    )
    return AnomalyListResponse(total=total, items=items)


@router.get("/summary", response_model=AnomalySummary, summary="Anomaly statistics")
def anomaly_summary(db: Database = Depends(get_db)) -> AnomalySummary:
    """Counts by reason/severity plus the estimated waste of unresolved incidents."""
    return anomaly_module.anomaly_summary(db)


@router.get("/{anomaly_id}", response_model=AnomalyRecord, summary="Get one anomaly")
def get_anomaly(anomaly_id: int, db: Database = Depends(get_db)) -> AnomalyRecord:
    """404 when the anomaly does not exist."""
    record = anomaly_module.get_anomaly(db, anomaly_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"anomaly {anomaly_id} not found"
        )
    return record


@router.patch("/{anomaly_id}", response_model=AnomalyRecord, summary="Update triage status")
def patch_anomaly(
    anomaly_id: int,
    payload: AnomalyPatch,
    db: Database = Depends(get_db),
) -> AnomalyRecord:
    """Move an anomaly between ``open`` → ``acknowledged`` → ``resolved``."""
    try:
        record = anomaly_module.set_anomaly_status(db, anomaly_id, payload.status)
    except ValueError as exc:  # pragma: no cover - the schema Literal already validates
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"anomaly {anomaly_id} not found"
        )
    return record
