"""Budget engine: CRUD, spend evaluation, threshold alerts and notification fan-out.

The budget engine is the part of TokenLens that turns *observability* into *action*. It
answers three questions on every pass:

1. **How much has this scope spent in the current calendar window?** Windows are
   calendar aligned in the display timezone (``settings.display_tz``) via
   :func:`tokenlens.timeutil.period_window`, so a "daily" budget for a Shanghai team
   resets at 00:00 +08:00 rather than at 00:00 UTC.
2. **Which thresholds has that spend crossed?** Thresholds are *ratios* of the limit
   (``0.5`` = 50 %), not absolute dollars, so raising a limit does not require rewriting
   every alert rule.
3. **Who needs to be told, and how often?** An alert is emitted at most once per
   ``(budget_id, window_start, threshold)``; later passes over the same window *update*
   that row and bump its ``trigger_count`` (the "告警触发次数" metric the web UI shows)
   instead of inserting duplicates. Notifications are dispatched only when the breach is
   *new* (a threshold is crossed for the first time in this window, or a higher threshold
   is crossed), which is what keeps a 30 s evaluation loop from spamming Slack.

Data model (see ``docs/API_CONTRACT.md`` §13):

* ``budgets``      — the rule (scope, period, limit, thresholds, delivery config)
* ``alert_events`` — one row per ``(budget, window, threshold)``; ``trigger_count``
  counts repeat breaches, ``UNIQUE(budget_id, window_start, threshold)`` is the
  idempotency key.
* ``budget_state`` — one row per ``(budget, window)``: cumulative trigger count, highest
  threshold breached, last notification timestamp, overspend.

Money is kept at 6 decimals everywhere (``round(x, 6)``) to match the rest of the
platform and to keep float sums reproducible in tests.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import notify, pricing, timeutil
from .config import Settings, get_settings
from .db import Database, build_filters, dump_json, load_json
from .schemas import (
    AlertRecord,
    AlertSummary,
    BudgetIn,
    BudgetOut,
    BudgetPatch,
    BudgetStatus,
    EvaluateResponse,
    Window,
)

logger = logging.getLogger(__name__)

#: Severity buckets, from calmest to loudest.
SEVERITIES: tuple[str, ...] = ("warning", "critical", "exceeded")

#: ``status`` values reported on :class:`~tokenlens.schemas.BudgetStatus`.
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_CRITICAL = "critical"
STATUS_EXCEEDED = "exceeded"

#: pct >= 1.0 is an overspend, pct >= 0.8 is critical, anything triggered is a warning.
CRITICAL_RATIO = 0.8
EXCEEDED_RATIO = 1.0

#: Guards against absurd projections when a window has just opened.
MIN_PROJECTION_ELAPSED_MS = 60_000

#: Float comparison slack for threshold crossings (0.8 * 100 is 80.00000000000001).
EPS = 1e-9


# --------------------------------------------------------------------------------------
# Severity helpers
# --------------------------------------------------------------------------------------


def severity_for_threshold(threshold: float) -> str:
    """Map a threshold ratio to a severity, per contract §7.

    ``t < 0.8`` → ``warning``, ``0.8 <= t < 1.0`` → ``critical``, ``t >= 1.0`` → ``exceeded``.
    """
    value = float(threshold)
    if value >= EXCEEDED_RATIO:
        return "exceeded"
    if value >= CRITICAL_RATIO:
        return "critical"
    return "warning"


def status_for(pct: float, triggered: Sequence[float]) -> str:
    """Aggregate status for a budget evaluation (``ok|warning|critical|exceeded``)."""
    if pct + EPS >= EXCEEDED_RATIO:
        return STATUS_EXCEEDED
    if any(float(t) + EPS >= CRITICAL_RATIO for t in triggered):
        return STATUS_CRITICAL
    if triggered:
        return STATUS_WARNING
    return STATUS_OK


def _round_money(value: float) -> float:
    return round(float(value), 6)


# --------------------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------------------


def row_to_budget(row: Any) -> BudgetOut:
    """Convert a ``budgets`` row into the API model."""
    thresholds = load_json(row["thresholds"], default=[0.5, 0.8, 1.0])
    if not isinstance(thresholds, list):
        thresholds = [0.5, 0.8, 1.0]
    return BudgetOut(
        id=int(row["id"]),
        name=row["name"],
        scope=row["scope"],
        scope_key=row["scope_key"],
        period=row["period"],
        limit_usd=_round_money(row["limit_usd"]),
        thresholds=[float(t) for t in thresholds],
        webhook_url=row["webhook_url"],
        desktop=bool(row["desktop"]),
        enabled=bool(row["enabled"]),
        created_at=timeutil.ms_to_dt(int(row["created_at"])) if row["created_at"] else None,
        updated_at=timeutil.ms_to_dt(int(row["updated_at"])) if row["updated_at"] else None,
    )


def list_budgets(db: Database, *, enabled_only: bool = False) -> list[BudgetOut]:
    """All budgets, newest first (``GET /api/v1/budgets``)."""
    sql = "SELECT * FROM budgets"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id ASC"
    return [row_to_budget(row) for row in db.fetch_all(sql)]


def get_budget(db: Database, budget_id: int) -> BudgetOut | None:
    row = db.fetch_one("SELECT * FROM budgets WHERE id = ?", (int(budget_id),))
    return row_to_budget(row) if row is not None else None


def create_budget(db: Database, payload: BudgetIn, *, now_ms: int | None = None) -> BudgetOut:
    """Insert a budget and return it (``POST /api/v1/budgets``)."""
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    scope_key = "*" if payload.scope == "global" else payload.scope_key
    budget_id = db.execute(
        """
        INSERT INTO budgets (name, scope, scope_key, period, limit_usd, thresholds,
                             webhook_url, desktop, enabled, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            payload.name.strip(),
            payload.scope,
            scope_key,
            payload.period,
            _round_money(payload.limit_usd),
            dump_json([float(t) for t in payload.thresholds]),
            payload.webhook_url,
            1 if payload.desktop else 0,
            1 if payload.enabled else 0,
            now,
            now,
        ),
    )
    created = get_budget(db, budget_id)
    assert created is not None  # noqa: S101 - the insert above guarantees the row
    return created


def update_budget(
    db: Database,
    budget_id: int,
    patch: BudgetPatch,
    *,
    now_ms: int | None = None,
) -> BudgetOut | None:
    """Apply a partial update (``PATCH /api/v1/budgets/{id}``); ``None`` when missing."""
    current = get_budget(db, budget_id)
    if current is None:
        return None
    merged = current.model_dump()
    changes = patch.model_dump(exclude_unset=True, exclude_none=True)

    for key, value in changes.items():
        if key == "thresholds" and value is not None:
            merged["thresholds"] = [float(t) for t in value]
        else:
            merged[key] = value

    # Re-validate the merged document through the strict input model so PATCH can never
    # produce a budget that POST would have rejected.
    validated = BudgetIn(
        name=merged["name"],
        scope=merged["scope"],
        scope_key=merged.get("scope_key") or "*",
        period=merged["period"],
        limit_usd=float(merged["limit_usd"]),
        thresholds=[float(t) for t in merged["thresholds"]],
        webhook_url=merged.get("webhook_url"),
        desktop=bool(merged["desktop"]),
        enabled=bool(merged["enabled"]),
    )
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    db.execute(
        """
        UPDATE budgets
           SET name = ?, scope = ?, scope_key = ?, period = ?, limit_usd = ?,
               thresholds = ?, webhook_url = ?, desktop = ?, enabled = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            validated.name.strip(),
            validated.scope,
            "*" if validated.scope == "global" else validated.scope_key,
            validated.period,
            _round_money(validated.limit_usd),
            dump_json([float(t) for t in validated.thresholds]),
            validated.webhook_url,
            1 if validated.desktop else 0,
            1 if validated.enabled else 0,
            now,
            int(budget_id),
        ),
    )
    return get_budget(db, budget_id)


def delete_budget(db: Database, budget_id: int) -> bool:
    """Delete a budget (``DELETE /api/v1/budgets/{id}``). Alert history is kept."""
    if get_budget(db, budget_id) is None:
        return False
    db.execute("DELETE FROM budgets WHERE id = ?", (int(budget_id),))
    db.execute("DELETE FROM budget_state WHERE budget_id = ?", (int(budget_id),))
    return True


# --------------------------------------------------------------------------------------
# Spend computation
# --------------------------------------------------------------------------------------


def _scope_filter(scope: str, scope_key: str) -> dict[str, str]:
    """Translate a ``(scope, scope_key)`` pair into a ``build_filters`` keyword."""
    if scope == "project":
        return {"project": scope_key}
    if scope == "agent":
        return {"agent": scope_key}
    if scope == "model":
        # Snapshots store the canonical model name (see tokenlens.store), so a budget
        # created with "claude-sonnet-4-5-20250929" must still match its rows.
        return {"model": pricing.normalize_model_name(scope_key)}
    return {}


def scope_spend(db: Database, scope: str, scope_key: str, start_ms: int, end_ms: int) -> float:
    """USD spent by ``scope`` inside ``[start_ms, end_ms)``.

    Uses the raw ``usage_snapshots`` table (the source of truth) rather than the hourly
    rollup so a partially rolled-up window can never under-report a breach.
    """
    where, params = build_filters(
        start_ms=int(start_ms), end_ms=int(end_ms), **_scope_filter(scope, scope_key)
    )
    value = db.scalar(
        f"SELECT COALESCE(SUM(s.cost_usd), 0.0) FROM usage_snapshots s{where}",
        params,
        default=0.0,
    )
    return _round_money(float(value or 0.0))


def scope_snapshot_count(db: Database, scope: str, scope_key: str, start_ms: int, end_ms: int) -> int:
    """Number of snapshots contributing to :func:`scope_spend` (used by tests/diagnostics)."""
    where, params = build_filters(
        start_ms=int(start_ms), end_ms=int(end_ms), **_scope_filter(scope, scope_key)
    )
    return int(
        db.scalar(f"SELECT COUNT(*) FROM usage_snapshots s{where}", params, default=0) or 0
    )


def resolve_window(
    period: str,
    *,
    settings: Settings | None = None,
    at_ms: int | None = None,
) -> tuple[int, int]:
    """Current calendar window for ``period`` in the configured display timezone."""
    settings = settings or get_settings()
    return timeutil.period_window(period, at_ms, settings.display_tz)


def project_spend(spent_usd: float, window_start: int, window_end: int, now_ms: int) -> float:
    """Linear projection of ``spent_usd`` to the end of the window.

    ``spent / elapsed * total``. Elapsed time is floored at one minute so that a pass
    running a few milliseconds after midnight does not project a multi-million dollar
    day from three cents of spend.
    """
    total = max(1, int(window_end) - int(window_start))
    elapsed = max(MIN_PROJECTION_ELAPSED_MS, int(now_ms) - int(window_start))
    elapsed = min(elapsed, total)
    return _round_money(float(spent_usd) * total / elapsed)


# --------------------------------------------------------------------------------------
# State helpers
# --------------------------------------------------------------------------------------


def _state_row(db: Database, budget_id: int, window_start: int) -> Any:
    return db.fetch_one(
        "SELECT * FROM budget_state WHERE budget_id = ? AND window_start = ?",
        (int(budget_id), int(window_start)),
    )


def _state_max_threshold(db: Database, budget_id: int, window_start: int) -> float:
    """Highest threshold already recorded for this window (0.0 when untouched)."""
    row = _state_row(db, budget_id, window_start)
    return float(row["max_threshold"] or 0.0) if row is not None else 0.0


def _state_trigger_count(db: Database, budget_id: int, window_start: int) -> int:
    """Evaluation passes that have breached this window so far."""
    row = _state_row(db, budget_id, window_start)
    return int(row["trigger_count"] or 0) if row is not None else 0


def build_status(
    db: Database,
    budget: BudgetOut,
    *,
    settings: Settings | None = None,
    now_ms: int | None = None,
) -> BudgetStatus:
    """Evaluate one budget for the current window without persisting anything."""
    settings = settings or get_settings()
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    window_start, window_end = resolve_window(budget.period, settings=settings, at_ms=now)

    spent = scope_spend(db, budget.scope, budget.scope_key, window_start, window_end)
    limit = float(budget.limit_usd) or 0.0
    pct = _round_money(spent / limit) if limit > 0 else 0.0
    triggered = [float(t) for t in budget.thresholds if pct + EPS >= float(t)]
    overspend = _round_money(max(0.0, spent - limit))
    projected = project_spend(spent, window_start, window_end, now)
    projected_pct = _round_money(projected / limit) if limit > 0 else 0.0

    state = _state_row(db, budget.id, window_start)
    trigger_count = int(state["trigger_count"]) if state is not None else 0
    last_alert_at = (
        timeutil.ms_to_dt(int(state["last_notified_at"]))
        if state is not None and state["last_notified_at"]
        else None
    )
    if state is not None and state["overspend_usd"] is not None:
        overspend = _round_money(max(overspend, float(state["overspend_usd"])))

    return BudgetStatus(
        budget=budget,
        window_start=timeutil.ms_to_dt(window_start),
        window_end=timeutil.ms_to_dt(window_end),
        spent_usd=spent,
        limit_usd=_round_money(limit),
        pct=pct,
        remaining_usd=_round_money(max(0.0, limit - spent)),
        projected_usd=projected,
        projected_pct=projected_pct,
        status=status_for(pct, triggered),
        triggered_thresholds=triggered,
        trigger_count=trigger_count,
        overspend_usd=overspend,
        last_alert_at=last_alert_at,
    )


def list_budget_statuses(
    db: Database,
    *,
    settings: Settings | None = None,
    now_ms: int | None = None,
) -> list[BudgetStatus]:
    """Evaluate every budget (enabled and disabled) — ``GET /api/v1/budgets``."""
    settings = settings or get_settings()
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    return [build_status(db, budget, settings=settings, now_ms=now) for budget in list_budgets(db)]


# --------------------------------------------------------------------------------------
# Alert rows
# --------------------------------------------------------------------------------------


def alert_message(budget: BudgetOut, threshold: float, spent: float, pct: float) -> str:
    """Human readable alert body (Chinese, matching the product's UI language)."""
    overspend = max(0.0, spent - float(budget.limit_usd))
    head = (
        f"预算「{budget.name}」已用 ${spent:.2f} / ${float(budget.limit_usd):.2f}"
        f"（{pct * 100:.1f}%），触发 {threshold * 100:.0f}% 阈值。"
    )
    if overspend > 0:
        return head + f" 已超支 ${overspend:.2f}。"
    return head


def row_to_alert(row: Any) -> AlertRecord:
    """Convert an ``alert_events`` row into the API model."""
    channels = load_json(row["channels"], default=[])
    if not isinstance(channels, list):
        channels = []
    return AlertRecord(
        id=int(row["id"]),
        budget_id=int(row["budget_id"]) if row["budget_id"] is not None else None,
        budget_name=row["budget_name"] or "",
        scope=row["scope"] or "global",
        scope_key=row["scope_key"] or "*",
        period=row["period"] or "daily",
        window_start=timeutil.ms_to_dt(int(row["window_start"] or 0)),
        window_end=timeutil.ms_to_dt(int(row["window_end"] or 0)),
        threshold=float(row["threshold"] or 0.0),
        severity=row["severity"] or "warning",
        spent_usd=_round_money(row["spent_usd"] or 0.0),
        limit_usd=_round_money(row["limit_usd"] or 0.0),
        overspend_usd=_round_money(row["overspend_usd"] or 0.0),
        trigger_count=int(row["trigger_count"] or 1),
        message=row["message"] or "",
        channels=[str(c) for c in channels],
        created_at=timeutil.ms_to_dt(int(row["created_at"])) if row["created_at"] else None,
    )


def get_alert(db: Database, alert_id: int) -> AlertRecord | None:
    row = db.fetch_one("SELECT * FROM alert_events WHERE id = ?", (int(alert_id),))
    return row_to_alert(row) if row is not None else None


# --------------------------------------------------------------------------------------
# Evaluation pass
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingAlert:
    """A threshold breach produced by one evaluation pass, before/after persistence."""

    threshold: float
    severity: str
    alert_id: int
    trigger_count: int
    created_at: int
    channels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EvaluationResult:
    """Outcome of :func:`evaluate_budgets`, serialised as ``EvaluateResponse``."""

    evaluated: int = 0
    triggered: list[AlertRecord] = field(default_factory=list)
    evaluated_at: int = 0
    dry_run: bool = False
    notifications: list[dict[str, Any]] = field(default_factory=list)

    def as_response(self) -> EvaluateResponse:
        return EvaluateResponse(
            evaluated=self.evaluated,
            triggered=list(self.triggered),
            evaluated_at=timeutil.ms_to_dt(self.evaluated_at),
            dry_run=self.dry_run,
        )


def _renotify_enabled() -> bool:
    """Opt-in re-notification on every repeat breach (``TOKENLENS_ALERT_RENOTIFY=1``).

    Off by default: a 30 second evaluation loop over a breached daily budget would
    otherwise deliver ~2 880 webhooks a day for a single incident.
    """
    raw = os.environ.get("TOKENLENS_ALERT_RENOTIFY")
    if raw is None or raw == "":
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def evaluate_budgets(
    db: Database,
    *,
    settings: Settings | None = None,
    now_ms: int | None = None,
    dry_run: bool = False,
    transport: Any = None,
) -> EvaluationResult:
    """Run one evaluation pass over every enabled budget in the current window.

    ``dry_run=True`` computes exactly the same numbers but persists nothing and sends
    nothing — used by the web UI's "preview" button and by ``tokenlens detect --evaluate``.
    """
    settings = settings or get_settings()
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    result = EvaluationResult(evaluated_at=now, dry_run=bool(dry_run))

    budgets = list_budgets(db, enabled_only=True)
    for budget in budgets:
        result.evaluated += 1
        window_start, window_end = resolve_window(budget.period, settings=settings, at_ms=now)
        spent = scope_spend(db, budget.scope, budget.scope_key, window_start, window_end)
        limit = float(budget.limit_usd) or 0.0
        pct = _round_money(spent / limit) if limit > 0 else 0.0
        overspend = _round_money(max(0.0, spent - limit))
        breached = [float(t) for t in budget.thresholds if pct + EPS >= float(t)]
        if not breached:
            continue

        max_threshold = max(breached)
        message = alert_message(budget, max_threshold, spent, pct)

        if dry_run:
            for threshold in breached:
                result.triggered.append(
                    AlertRecord(
                        id=0,
                        budget_id=budget.id,
                        budget_name=budget.name,
                        scope=budget.scope,
                        scope_key=budget.scope_key,
                        period=budget.period,
                        window_start=timeutil.ms_to_dt(window_start),
                        window_end=timeutil.ms_to_dt(window_end),
                        threshold=threshold,
                        severity=severity_for_threshold(threshold),
                        spent_usd=spent,
                        limit_usd=_round_money(limit),
                        overspend_usd=overspend,
                        trigger_count=0,
                        message=alert_message(budget, threshold, spent, pct),
                        channels=[],
                        created_at=timeutil.ms_to_dt(now),
                    )
                )
            continue

        # Read the pre-pass state *before* persisting: "is this a new breach?" must be
        # answered from the value that existed before this evaluation bumped anything.
        previous_max = _state_max_threshold(db, budget.id, window_start)
        previous_trigger_count = _state_trigger_count(db, budget.id, window_start)

        pending = _persist_breaches(
            db,
            budget=budget,
            window_start=window_start,
            window_end=window_end,
            spent=spent,
            limit=limit,
            pct=pct,
            overspend=overspend,
            breached=breached,
            now=now,
            message=message,
        )

        # A threshold is "new" when it exceeds everything previously recorded for this
        # window. ``_renotify_enabled`` opts into re-notifying on every repeat breach.
        notify_now = _renotify_enabled() or max_threshold > previous_max + EPS
        _ = previous_trigger_count  # retained for diagnostics / future backoff policy

        records = [
            AlertRecord(
                id=item.alert_id,
                budget_id=budget.id,
                budget_name=budget.name,
                scope=budget.scope,
                scope_key=budget.scope_key,
                period=budget.period,
                window_start=timeutil.ms_to_dt(window_start),
                window_end=timeutil.ms_to_dt(window_end),
                threshold=item.threshold,
                severity=item.severity,
                spent_usd=spent,
                limit_usd=_round_money(limit),
                overspend_usd=overspend,
                trigger_count=item.trigger_count,
                message=alert_message(budget, item.threshold, spent, pct),
                channels=[],
                created_at=timeutil.ms_to_dt(item.created_at),
            )
            for item in pending
        ]

        if notify_now:
            channels = notify.channels_for(
                webhook_url=budget.webhook_url,
                desktop=bool(budget.desktop),
                log=True,
            )
            outcomes: list[dict[str, Any]] = []
            for record in records:
                notification = notify.Notification(
                    channel="budget",
                    title=f"TokenLens 预算告警 · {budget.name}",
                    body=record.message,
                    severity=record.severity,
                    payload=_webhook_payload(
                        record, window_start, window_end, budget.webhook_url
                    ),
                )
                channel_results = notify.dispatch(
                    settings, channels, notification, transport=transport
                )
                outcomes.append(
                    {
                        "budget_id": budget.id,
                        "threshold": record.threshold,
                        "channels": channel_results,
                    }
                )
                record.channels = [entry["channel"] for entry in channel_results]
            result.notifications.extend(outcomes)

            delivered = sorted({c for entry in outcomes for c in _ok_channels(entry["channels"])})
            with db.cursor(write=True) as cur:
                cur.execute(
                    "UPDATE alert_events SET channels = ? WHERE budget_id = ? AND window_start = ?",
                    (dump_json(delivered or channels), int(budget.id), int(window_start)),
                )
                cur.execute(
                    "UPDATE budget_state SET last_notified_at = ?, updated_at = ? "
                    "WHERE budget_id = ? AND window_start = ?",
                    (now, now, int(budget.id), int(window_start)),
                )
            for record in records:
                record.channels = delivered or channels

        result.triggered.extend(records)

    return result


def _ok_channels(entries: Iterable[dict[str, Any]]) -> list[str]:
    return [str(entry["channel"]) for entry in entries if entry.get("ok")]


def _webhook_payload(
    record: AlertRecord,
    window_start: int,
    window_end: int,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """JSON document POSTed to ``webhook_url`` (Slack/Feishu/Discord compatible enough).

    ``webhook_url`` is echoed inside the payload because :func:`tokenlens.notify.dispatch`
    is channel-oriented and therefore needs the target to be part of the notification.
    """
    return {
        "event": "budget.threshold",
        "budget_id": record.budget_id,
        "budget_name": record.budget_name,
        "scope": record.scope,
        "scope_key": record.scope_key,
        "period": record.period,
        "window_start": timeutil.iso(window_start),
        "window_end": timeutil.iso(window_end),
        "threshold": record.threshold,
        "severity": record.severity,
        "spent_usd": record.spent_usd,
        "limit_usd": record.limit_usd,
        "overspend_usd": record.overspend_usd,
        "pct": round(record.spent_usd / record.limit_usd, 6) if record.limit_usd else 0.0,
        "trigger_count": record.trigger_count,
        "message": record.message,
        "webhook_url": webhook_url or "",
    }


def _persist_breaches(
    db: Database,
    *,
    budget: BudgetOut,
    window_start: int,
    window_end: int,
    spent: float,
    limit: float,
    pct: float,
    overspend: float,
    breached: Sequence[float],
    now: int,
    message: str,
) -> list[_PendingAlert]:
    """Upsert one ``alert_events`` row per breached threshold and bump ``budget_state``.

    The ``UNIQUE(budget_id, window_start, threshold)`` constraint is the idempotency key:
    a repeat pass over the same window/threshold increments ``trigger_count`` and
    refreshes the money columns instead of inserting a duplicate alert.
    """
    pending: list[_PendingAlert] = []
    with db.cursor(write=True) as cur:
        for threshold in breached:
            existing = cur.execute(
                "SELECT id, trigger_count, created_at FROM alert_events "
                "WHERE budget_id = ? AND window_start = ? AND threshold = ?",
                (int(budget.id), int(window_start), float(threshold)),
            ).fetchone()
            severity = severity_for_threshold(threshold)
            threshold_message = alert_message(budget, threshold, spent, pct)
            if existing is not None:
                alert_id = int(existing["id"])
                trigger_count = int(existing["trigger_count"]) + 1
                created_at = int(existing["created_at"] or now)
                cur.execute(
                    """
                    UPDATE alert_events
                       SET trigger_count = ?, severity = ?, spent_usd = ?, limit_usd = ?,
                           overspend_usd = ?, message = ?, window_end = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        trigger_count,
                        severity,
                        spent,
                        _round_money(limit),
                        overspend,
                        threshold_message,
                        int(window_end),
                        now,
                        alert_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO alert_events (
                        budget_id, budget_name, scope, scope_key, period, window_start,
                        window_end, threshold, severity, spent_usd, limit_usd,
                        overspend_usd, trigger_count, message, channels, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(budget.id),
                        budget.name,
                        budget.scope,
                        budget.scope_key,
                        budget.period,
                        int(window_start),
                        int(window_end),
                        float(threshold),
                        severity,
                        spent,
                        _round_money(limit),
                        overspend,
                        1,  # trigger_count: first breach of this (budget, window, threshold)
                        threshold_message,
                        dump_json([]),
                        now,
                        now,
                    ),
                )
                alert_id = int(cur.lastrowid or 0)
                trigger_count = 1
                created_at = now
            pending.append(
                _PendingAlert(
                    threshold=float(threshold),
                    severity=severity,
                    alert_id=alert_id,
                    trigger_count=trigger_count,
                    created_at=created_at,
                )
            )

        cur.execute(
            """
            INSERT INTO budget_state (
                budget_id, window_start, trigger_count, max_threshold,
                last_notified_at, overspend_usd, updated_at
            ) VALUES (?,?,?,?,NULL,?,?)
            ON CONFLICT(budget_id, window_start) DO UPDATE SET
                trigger_count = budget_state.trigger_count + 1,
                max_threshold = MAX(budget_state.max_threshold, excluded.max_threshold),
                overspend_usd = excluded.overspend_usd,
                updated_at = excluded.updated_at
            """,
            (
                int(budget.id),
                int(window_start),
                1,
                float(max(breached)),
                overspend,
                now,
            ),
        )
    return pending


# --------------------------------------------------------------------------------------
# Alert history queries
# --------------------------------------------------------------------------------------


def _alert_where(
    *,
    severity: str | None = None,
    budget_id: int | None = None,
    since_ms: int | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if severity:
        clauses.append("severity = ?")
        params.append(str(severity))
    if budget_id is not None:
        clauses.append("budget_id = ?")
        params.append(int(budget_id))
    if since_ms is not None:
        clauses.append("created_at >= ?")
        params.append(int(since_ms))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_alerts(
    db: Database,
    *,
    limit: int = 50,
    offset: int = 0,
    severity: str | None = None,
    budget_id: int | None = None,
    since_ms: int | None = None,
) -> tuple[int, list[AlertRecord]]:
    """Paginated alert history, newest first (``GET /api/v1/alerts``)."""
    where, params = _alert_where(severity=severity, budget_id=budget_id, since_ms=since_ms)
    total = int(db.scalar(f"SELECT COUNT(*) FROM alert_events{where}", params, default=0) or 0)
    rows = db.fetch_all(
        f"SELECT * FROM alert_events{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)],
    )
    return total, [row_to_alert(row) for row in rows]


def alert_summary(db: Database, *, now_ms: int | None = None, since_ms: int | None = None) -> AlertSummary:
    """Aggregate alert statistics (``GET /api/v1/alerts/summary``).

    ``total_overspend_usd`` is de-duplicated per ``(budget_id, window_start)`` because the
    same overspend figure is mirrored onto every threshold row of a window.
    """
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    where, params = _alert_where(since_ms=since_ms)

    total_alerts = int(
        db.scalar(f"SELECT COUNT(*) FROM alert_events{where}", params, default=0) or 0
    )
    total_triggers = int(
        db.scalar(f"SELECT COALESCE(SUM(trigger_count), 0) FROM alert_events{where}", params, default=0)
        or 0
    )

    by_severity = {name: 0 for name in SEVERITIES}
    for row in db.fetch_all(
        f"SELECT severity, COUNT(*) AS n FROM alert_events{where} GROUP BY severity", params
    ):
        by_severity[str(row["severity"] or "warning")] = int(row["n"])

    overspend_rows = db.fetch_all(
        f"""
        SELECT budget_id, budget_name,
               COUNT(*)              AS alerts,
               SUM(trigger_count)    AS triggers,
               COALESCE(SUM(overspend_usd), 0.0) AS overspend_usd
          FROM alert_events{where}
         GROUP BY budget_id, budget_name
         ORDER BY overspend_usd DESC, alerts DESC
        """,
        params,
    )
    by_budget: list[dict[str, Any]] = []
    total_overspend = 0.0
    for row in overspend_rows:
        # One overspend figure per window, not per threshold row.
        dedup_where = where + (" AND " if where else " WHERE ")
        dedup_where += "budget_id IS ?"
        per_window = db.fetch_all(
            f"SELECT MAX(overspend_usd) AS overspend FROM alert_events{dedup_where} "
            "GROUP BY window_start",
            [*params, row["budget_id"]],
        )
        overspend = _round_money(sum(float(item["overspend"] or 0.0) for item in per_window))
        total_overspend += overspend
        by_budget.append(
            {
                "budget_id": row["budget_id"],
                "budget_name": row["budget_name"] or "",
                "alerts": int(row["alerts"]),
                "triggers": int(row["triggers"] or 0),
                "overspend_usd": overspend,
            }
        )

    first_created = db.scalar(f"SELECT MIN(created_at) FROM alert_events{where}", params, default=None)
    last_triggered = db.scalar(f"SELECT MAX(updated_at) FROM alert_events{where}", params, default=None)
    window = Window(
        **{
            "from": timeutil.ms_to_dt(int(first_created)) if first_created else timeutil.ms_to_dt(now),
            "to": timeutil.ms_to_dt(now),
        }
    )
    return AlertSummary(
        total_alerts=total_alerts,
        total_triggers=total_triggers,
        by_severity=by_severity,
        by_budget=by_budget,
        total_overspend_usd=_round_money(total_overspend),
        window=window,
        last_triggered_at=timeutil.ms_to_dt(int(last_triggered)) if last_triggered else None,
    )
