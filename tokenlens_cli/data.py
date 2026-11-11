"""Snapshot assembly: API payloads -> :class:`tokenlens_cli.render.HudSnapshot`.

The HUD wants one *coherent* frame: KPI strip, budget bars and the bars for the current
dimension. The server exposes those across several endpoints, so this module owns the
fetch strategy and the graceful degradation rules:

1. ``GET /api/v1/dashboard/overview`` is the fast path — one round trip, everything the
   HUD shows. It is the contract's aggregation endpoint (§9).
2. If that endpoint is not available yet (404/501 while the platform is being built) the
   builder falls back to ``/usage/breakdown`` + ``/usage/live`` + ``/budgets`` and
   synthesises the KPI strip from the breakdown totals.
3. Individual optional endpoints (budgets, live) may fail without failing the frame; the
   failure becomes a warning line so the user can see *what* is degraded instead of
   staring at stale numbers.

Every function here takes an injected :class:`~tokenlens_cli.api.TokenLensClient`, so the
whole layer is exercised in tests through ``httpx.MockTransport`` — no sockets.
"""

from __future__ import annotations

from typing import Any

from tokenlens import timeutil

from . import render
from .api import ApiError, TokenLensClient

#: ``dimension`` -> dashboard breakdown key (dashboard uses ``by_<dimension>``).
DASHBOARD_KEYS = {
    "project": "by_project",
    "agent": "by_agent",
    "model": "by_model",
    "budget": "by_budget",
}


def _rows(value: Any) -> list[dict[str, Any]]:
    """Coerce an API list-ish field into ``list[dict]`` without raising."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def fetch_breakdown_rows(
    client: TokenLensClient,
    dimension: str,
    *,
    limit: int = 12,
    project: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch breakdown rows for one dimension; returns ``(rows, warnings)``."""
    warnings: list[str] = []
    try:
        payload = client.breakdown(dimension, limit=limit, project=project)
    except ApiError as exc:
        if exc.missing:
            warnings.append(f"breakdown/{dimension} not implemented on this server")
        elif exc.offline:
            raise
        else:
            warnings.append(f"breakdown/{dimension}: {exc}")
        return [], warnings
    return _rows(payload.get("rows")), warnings


def fetch_budgets(client: TokenLensClient) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch budget statuses; a missing/failing budgets endpoint degrades to ``[]``."""
    try:
        payload = client.budgets()
    except ApiError as exc:
        if exc.offline:
            raise
        return [], [f"budgets unavailable ({exc.friendly(client.server)})"]
    return _rows(payload.get("items")), []


def fetch_dashboard(client: TokenLensClient, *, days: int = 7) -> dict[str, Any] | None:
    """Fetch the aggregation endpoint, returning ``None`` when it is not available."""
    try:
        return client.dashboard(days=days)
    except ApiError as exc:
        if exc.offline:
            raise
        return None


def build_snapshot(
    client: TokenLensClient,
    *,
    dimension: str = "project",
    project: str | None = None,
    bar_width: int = render.DEFAULT_BAR_WIDTH,
    now_ms: int | None = None,
    prefer_dashboard: bool = True,
) -> render.HudSnapshot:
    """Build one HUD frame from the server.

    Raises :class:`~tokenlens_cli.api.ApiError` only when the server is unreachable —
    every other failure is downgraded into ``snapshot.warnings`` so the HUD keeps
    rendering the parts it *can* render.
    """
    dimension = dimension if dimension in render.DIMENSIONS else "project"
    generated = timeutil.now_ms() if now_ms is None else int(now_ms)
    snapshot = render.HudSnapshot(
        server=client.server,
        dimension=dimension,
        project=project,
        generated_ms=generated,
    )

    overview = fetch_dashboard(client) if prefer_dashboard else None
    warnings: list[str] = []

    if overview is not None:
        snapshot.kpis = render.kpis_from_dashboard(overview)
        breakdowns = overview.get("breakdowns") or {}
        if isinstance(breakdowns, dict) and dimension != "budget":
            rows = _rows(breakdowns.get(DASHBOARD_KEYS.get(dimension, f"by_{dimension}")))
            if project:
                rows = [row for row in rows if str(row.get("key") or row.get("label")) == project]
            bars, total_cost, total_tokens = render.bars_from_breakdown_rows(
                rows, dimension=dimension, bar_width=bar_width
            )
            snapshot.bars = bars
            snapshot.total_cost_usd = total_cost
            snapshot.total_tokens = total_tokens
        budgets = _rows(overview.get("budgets"))
        snapshot.budgets = render.budget_bars_from_statuses(budgets, bar_width=bar_width)
    else:
        if prefer_dashboard:
            warnings.append("dashboard/overview not implemented — using fallback queries")
        # -- fallback: explicit queries ------------------------------------------------
        if dimension == "budget":
            budgets, budget_warnings = fetch_budgets(client)
            warnings.extend(budget_warnings)
            snapshot.budgets = render.budget_bars_from_statuses(budgets, bar_width=bar_width)
        else:
            rows, row_warnings = fetch_breakdown_rows(
                client, dimension, limit=12, project=project
            )
            warnings.extend(row_warnings)
            bars, total_cost, total_tokens = render.bars_from_breakdown_rows(
                rows, dimension=dimension, bar_width=bar_width
            )
            snapshot.bars = bars
            snapshot.total_cost_usd = total_cost
            snapshot.total_tokens = total_tokens
            budgets, budget_warnings = fetch_budgets(client)
            warnings.extend(budget_warnings)
            snapshot.budgets = render.budget_bars_from_statuses(budgets, bar_width=bar_width)

        # Synthesise the KPI strip from the trailing live window + today's breakdown.
        kpis = render.KpiSet()
        try:
            kpis = render.kpis_from_live(client.live(window_minutes=5), kpis)
        except ApiError as exc:
            if exc.offline:
                raise
            warnings.append(f"usage/live unavailable ({exc.friendly(client.server)})")
        if snapshot.bars:
            kpis.today_cost_usd = snapshot.total_cost_usd
            kpis.today_tokens = snapshot.total_tokens
        snapshot.kpis = kpis

    if dimension == "budget" and not snapshot.bars:
        snapshot.bars = render.bars_from_budget_bars(snapshot.budgets)
        snapshot.total_cost_usd = sum(budget.spent_usd for budget in snapshot.budgets)
        snapshot.total_tokens = 0

    snapshot.warnings = warnings
    return snapshot


def offline_snapshot(
    server: str,
    error: str,
    *,
    dimension: str = "project",
    project: str | None = None,
    now_ms: int | None = None,
) -> render.HudSnapshot:
    """Build the frame shown when the server cannot be reached."""
    return render.HudSnapshot(
        server=server,
        dimension=dimension if dimension in render.DIMENSIONS else "project",
        project=project,
        generated_ms=timeutil.now_ms() if now_ms is None else int(now_ms),
        offline=True,
        error=error,
    )


def next_dimension(current: str) -> str:
    """Cycle through the HUD dimensions in key-binding order."""
    order = render.DIMENSION_ORDER
    try:
        index = order.index(current)
    except ValueError:
        return order[0]
    return order[(index + 1) % len(order)]


def dimension_for_key(key: str) -> str | None:
    """Map a pressed key to a dimension (``p``->project …), or ``None``."""
    return render.KEY_BINDINGS.get((key or "").lower())
