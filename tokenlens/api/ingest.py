"""Ingest endpoints — the write path of the observability pipeline.

Agent runtimes (Claude Code hooks, Codex wrappers, an OpenAI proxy, an Ollama sidecar)
POST usage deltas here. The endpoint is batch oriented because a coding agent produces
hundreds of small snapshots per minute and one HTTP round trip per snapshot would make
the monitoring itself a bottleneck.

Idempotency: each snapshot may carry a client generated ``event_id``; replaying a batch
after a timeout counts the rows as ``duplicates`` instead of double charging the budget.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import timeutil
from ..db import Database, get_db
from ..schemas import IngestRequest, IngestResponse, SnapshotIn
from ..store import ingest_snapshots

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


def _settings(request: Request):
    return getattr(request.app.state, "settings", None)


@router.post(
    "/snapshots",
    response_model=IngestResponse,
    summary="Ingest a batch of token usage snapshots",
)
def ingest_batch(
    payload: IngestRequest,
    request: Request,
    db: Database = Depends(get_db),
) -> IngestResponse:
    """Persist 1..N snapshots in one transaction and return ingest statistics."""
    settings = _settings(request)
    result = ingest_snapshots(db, payload.snapshots, settings=settings)
    if result.rejected and result.accepted == 0:
        raise HTTPException(status_code=413, detail=result.errors[0].error if result.errors else "batch too large")
    return IngestResponse(**result.as_dict())


@router.post(
    "/snapshot",
    response_model=IngestResponse,
    summary="Ingest a single snapshot",
)
def ingest_single(
    snapshot: SnapshotIn,
    request: Request,
    db: Database = Depends(get_db),
) -> IngestResponse:
    """Convenience endpoint for agents that emit one snapshot at a time."""
    settings = _settings(request)
    result = ingest_snapshots(db, [snapshot], settings=settings)
    return IngestResponse(**result.as_dict())


@router.get("/ping", summary="Ingest health probe used by the simulator")
def ingest_ping(request: Request, db: Database = Depends(get_db)) -> dict:
    """Cheap probe: confirms the DB is writable and reports server clock skew info."""
    return {
        "ok": True,
        "server_time": timeutil.iso(timeutil.now_ms()),
        "snapshots": int(db.scalar("SELECT COUNT(*) FROM usage_snapshots")),
        "price_version": __import__("tokenlens.pricing", fromlist=["PRICE_VERSION"]).PRICE_VERSION,
        "checked_at_ms": int(time.time() * 1000),
    }
