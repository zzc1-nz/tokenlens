"""Health and meta endpoints (unauthenticated)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from .. import __version__, pricing
from ..db import Database, get_db
from ..schemas import HealthResponse, MetaResponse, RUNTIMES
from ..timeutil import iso, now_ms

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
def health(request: Request, db: Database = Depends(get_db)) -> HealthResponse:
    started = getattr(request.app.state, "started_at", time.time())
    settings = getattr(request.app.state, "settings", None)
    return HealthResponse(
        status="ok",
        version=__version__,
        db=str(getattr(settings, "db_path", "unknown")),
        snapshots=int(db.scalar("SELECT COUNT(*) FROM usage_snapshots")),
        price_version=pricing.PRICE_VERSION,
        uptime_seconds=round(time.time() - started, 3),
        now=iso(now_ms()),
    )


@router.get("/api/v1/meta", response_model=MetaResponse, summary="Capabilities and catalog size")
def meta() -> MetaResponse:
    endpoints = [
        "POST /api/v1/ingest/snapshots",
        "POST /api/v1/ingest/snapshot",
        "GET  /api/v1/usage/summary",
        "GET  /api/v1/usage/breakdown",
        "GET  /api/v1/usage/timeseries",
        "GET  /api/v1/usage/live",
        "GET  /api/v1/trends",
        "GET  /api/v1/catalog/models",
        "GET  /api/v1/catalog/agents",
        "GET  /api/v1/catalog/projects",
        "GET  /api/v1/catalog/sessions",
        "GET  /api/v1/export/csv",
        "GET  /api/v1/budgets",
        "POST /api/v1/budgets",
        "POST /api/v1/budgets/evaluate",
        "GET  /api/v1/alerts",
        "GET  /api/v1/alerts/summary",
        "GET  /api/v1/anomalies",
        "POST /api/v1/anomalies/detect",
        "GET  /api/v1/anomalies/summary",
        "GET  /api/v1/dashboard/overview",
    ]
    return MetaResponse(
        name="TokenLens",
        version=__version__,
        price_version=pricing.PRICE_VERSION,
        models=pricing.model_count(),
        endpoints=endpoints,
        runtimes=list(RUNTIMES),
    )
