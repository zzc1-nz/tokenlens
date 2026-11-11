"""Budget CRUD and evaluation endpoints (contract §7).

Every endpoint is a thin HTTP shell: validation lives in ``tokenlens.schemas``, the money
math lives in :mod:`tokenlens.budget`, and this module only translates between the two.
That split is what makes the evaluation loop (``tokenlens.scheduler``) able to reuse the
exact same code path as ``POST /api/v1/budgets/evaluate``.

Route order matters: ``/evaluate`` is registered *before* ``/{budget_id}`` so FastAPI does
not try to parse ``"evaluate"`` as an integer path parameter.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from .. import budget as budget_module
from ..config import Settings, get_settings
from ..db import Database, get_db
from ..schemas import (
    BudgetIn,
    BudgetListResponse,
    BudgetPatch,
    BudgetStatus,
    EvaluateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/budgets", tags=["budgets"])


def _settings(request: Request) -> Settings:
    """Settings bound to the running app (tests inject their own)."""
    return getattr(request.app.state, "settings", None) or get_settings()


def _status_or_404(db: Database, budget_id: int, settings: Settings) -> BudgetStatus:
    budget = budget_module.get_budget(db, budget_id)
    if budget is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"budget {budget_id} not found")
    return budget_module.build_status(db, budget, settings=settings)


@router.get("", response_model=BudgetListResponse, summary="List budgets with live status")
def list_budgets(request: Request, db: Database = Depends(get_db)) -> BudgetListResponse:
    """Every budget (enabled or not), evaluated against the current window."""
    settings = _settings(request)
    return BudgetListResponse(items=budget_module.list_budget_statuses(db, settings=settings))


@router.post(
    "",
    response_model=BudgetStatus,
    status_code=status.HTTP_201_CREATED,
    summary="Create a budget",
)
def create_budget(
    payload: BudgetIn,
    request: Request,
    db: Database = Depends(get_db),
) -> BudgetStatus:
    """Create a budget and return it with its freshly evaluated status.

    ``201 Created`` with a :class:`~tokenlens.schemas.BudgetStatus` body: a budget is
    always presented together with its live spend, exactly like the list and detail
    endpoints, so a client never has to follow up with a second request.
    """
    settings = _settings(request)
    try:
        budget = budget_module.create_budget(db, payload)
    except ValueError as exc:  # pragma: no cover - schema validation catches most cases
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return budget_module.build_status(db, budget, settings=settings)


@router.post("/evaluate", response_model=EvaluateResponse, summary="Run one evaluation pass")
def evaluate_budgets(
    request: Request,
    dry_run: bool = Query(default=False, description="Compute alerts without persisting or notifying"),
    db: Database = Depends(get_db),
) -> EvaluateResponse:
    """Evaluate all enabled budgets in the current window.

    Repeat breaches of the same ``(budget, window, threshold)`` increment ``trigger_count``
    on the existing alert row; notifications are only dispatched when a breach is new.
    ``dry_run=true`` returns the alerts that *would* fire and touches nothing.
    """
    settings = _settings(request)
    result = budget_module.evaluate_budgets(db, settings=settings, dry_run=dry_run)
    return result.as_response()


@router.get("/{budget_id}", response_model=BudgetStatus, summary="Get one budget with live status")
def get_budget(budget_id: int, request: Request, db: Database = Depends(get_db)) -> BudgetStatus:
    """404 when the budget does not exist."""
    return _status_or_404(db, budget_id, _settings(request))


@router.patch("/{budget_id}", response_model=BudgetStatus, summary="Partially update a budget")
def patch_budget(
    budget_id: int,
    payload: BudgetPatch,
    request: Request,
    db: Database = Depends(get_db),
) -> BudgetStatus:
    """Apply a partial update; the merged document is re-validated as a full ``BudgetIn``."""
    settings = _settings(request)
    if budget_module.get_budget(db, budget_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"budget {budget_id} not found")
    try:
        updated = budget_module.update_budget(db, budget_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if updated is None:  # pragma: no cover - guarded by the existence check above
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"budget {budget_id} not found")
    return budget_module.build_status(db, updated, settings=settings)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a budget")
def delete_budget(budget_id: int, db: Database = Depends(get_db)) -> Response:
    """Delete the budget (alert history is retained for auditing)."""
    if not budget_module.delete_budget(db, budget_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"budget {budget_id} not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
