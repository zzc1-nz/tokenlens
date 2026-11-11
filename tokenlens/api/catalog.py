"""Catalog endpoints: model price table, agents, projects and sessions.

Two different kinds of catalog live here:

* ``/catalog/models`` is the **static price catalog** (:mod:`tokenlens.pricing`) — what the
  platform *can* price, versioned with ``PRICE_VERSION`` so a cost number can always be
  traced back to the price list that produced it.
* ``/catalog/agents|projects|sessions`` are **observed entities** derived from the raw
  snapshots (lifetime spend, first/last seen, snapshot counts). They are computed from
  ``usage_snapshots`` rather than the ``agents``/``projects`` catalog tables so a rebuilt
  or stale catalog table can never make the listing disagree with the ledger.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from .. import pricing, query
from ..db import Database, get_db
from ..schemas import CatalogListResponse, CatalogModelsResponse, ModelInfo

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


@router.get(
    "/models",
    response_model=CatalogModelsResponse,
    summary="Price catalog",
)
def catalog_models(
    provider: str | None = Query(default=None, description="filter by provider id"),
) -> CatalogModelsResponse:
    """Return every known model with its per-million-token prices.

    ``known`` distinguishes real catalog entries from the documented fallback pricing used
    for models released after this build.
    """
    entries = pricing.list_models(provider)
    models = [ModelInfo(**entry) for entry in entries]
    return CatalogModelsResponse(
        price_version=pricing.PRICE_VERSION, count=len(models), models=models
    )


@router.get(
    "/agents",
    response_model=CatalogListResponse,
    summary="Observed agents",
)
def catalog_agents(
    db: Database = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> CatalogListResponse:
    """Agents seen in the ledger, sorted by lifetime cost descending."""
    data: dict[str, Any] = query.catalog_agents(db, limit=limit, offset=offset)
    return CatalogListResponse(total=data["total"], items=data["items"])


@router.get(
    "/projects",
    response_model=CatalogListResponse,
    summary="Observed projects",
)
def catalog_projects(
    db: Database = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> CatalogListResponse:
    """Projects seen in the ledger, sorted by lifetime cost descending."""
    data: dict[str, Any] = query.catalog_projects(db, limit=limit, offset=offset)
    return CatalogListResponse(total=data["total"], items=data["items"])


@router.get(
    "/sessions",
    response_model=CatalogListResponse,
    summary="Observed sessions",
)
def catalog_sessions(
    db: Database = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    project: str | None = Query(default=None),
    agent: str | None = Query(default=None),
) -> CatalogListResponse:
    """Sessions seen in the ledger, newest activity first, with the models they touched."""
    data: dict[str, Any] = query.catalog_sessions(
        db, limit=limit, offset=offset, project=project, agent=agent
    )
    return CatalogListResponse(total=data["total"], items=data["items"])
