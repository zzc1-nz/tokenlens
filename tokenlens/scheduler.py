"""Background evaluation loop.

The API endpoints are *pull*: a dashboard or CLI asks for state. But budget breaches and
anomalies have to be noticed when they happen, even if nobody is looking — that is this
module's job. It is a single asyncio task that wakes every
``settings.budget_interval_seconds`` and runs, in order:

1. a **budget evaluation pass** (:func:`tokenlens.budget.evaluate_budgets`) which fires
   threshold alerts and dispatches webhook/desktop notifications;
2. an **anomaly detection pass** (:func:`tokenlens.anomaly.detect`) when
   ``settings.anomaly_enabled`` is set.

Three properties matter more than throughput here:

* **Never fatal.** Every pass is individually wrapped: a broken webhook, a malformed
  budget row or a transient ``SQLITE_BUSY`` must not kill the loop and must not prevent
  the anomaly pass from running.
* **Never blocking the event loop.** Both passes are synchronous SQLite work, so they run
  in a worker thread via ``asyncio.to_thread``; the HTTP server keeps serving requests
  while a large evaluation runs.
* **Drift free.** The loop sleeps on a monotonic deadline rather than ``sleep(interval)``
  after each pass, so a pass that takes 4 s of a 30 s period still runs every 30 s.

``start(app)`` returns the task (or ``None`` when disabled) and is idempotent; ``stop(app)``
cancels it and swallows ``CancelledError``. That is exactly the shape
``tokenlens.app._lifespan`` expects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import anomaly as anomaly_module
from . import budget as budget_module
from .config import Settings, get_settings
from .db import Database

logger = logging.getLogger(__name__)

#: ``app.state`` attribute holding the running task.
TASK_ATTR = "scheduler_task"

#: ``app.state`` attribute holding the last pass statistics (exposed for diagnostics).
STATE_ATTR = "scheduler_state"


def _resolve(app: Any) -> tuple[Settings, Database]:
    """Pull the settings and database handles off ``app.state`` (with sane fallbacks)."""
    state = getattr(app, "state", None)
    settings = getattr(state, "settings", None) or get_settings()
    db = getattr(state, "db", None)
    if db is None:  # pragma: no cover - only a mis-wired app hits this
        from .db import get_database

        db = get_database(settings.db_path)
    return settings, db


def run_once(app: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    """Execute one evaluation cycle synchronously (used by the loop and by tests).

    Returns a statistics dict; a failing pass is recorded as an error string rather than
    raised, so the caller can log and continue.
    """
    settings, db = _resolve(app)
    stats: dict[str, Any] = {"started_at": time.time(), "budget": None, "anomaly": None, "errors": []}

    try:
        result = budget_module.evaluate_budgets(db, settings=settings, now_ms=now_ms)
        stats["budget"] = {
            "evaluated": result.evaluated,
            "triggered": len(result.triggered),
            "dry_run": result.dry_run,
        }
    except Exception as exc:  # noqa: BLE001 - the loop must survive anything
        logger.exception("budget evaluation pass failed")
        stats["errors"].append(f"budget: {type(exc).__name__}: {exc}")

    if anomaly_module.detect_enabled(settings):
        try:
            detection = anomaly_module.detect(db, settings=settings, now_ms=now_ms)
            stats["anomaly"] = {
                "scanned_agents": detection.scanned_agents,
                "detected": len(detection.detected),
            }
        except Exception as exc:  # noqa: BLE001 - same contract as above
            logger.exception("anomaly detection pass failed")
            stats["errors"].append(f"anomaly: {type(exc).__name__}: {exc}")

    stats["duration_ms"] = round((time.time() - float(stats["started_at"])) * 1000.0, 3)
    return stats


async def _loop(app: Any, settings: Settings) -> None:
    """The scheduler coroutine: run a cycle, then sleep to the next deadline."""
    interval = max(1.0, float(getattr(settings, "budget_interval_seconds", 30) or 30))
    logger.info("TokenLens scheduler started (interval=%.1fs)", interval)
    loop = asyncio.get_running_loop()
    next_deadline = loop.time() + interval

    while True:
        try:
            stats = await asyncio.to_thread(run_once, app)
            setattr(app.state, STATE_ATTR, stats)
            if stats["errors"]:
                logger.warning("scheduler pass completed with errors: %s", stats["errors"])
            else:
                logger.debug("scheduler pass: %s", stats)
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown path
            logger.info("TokenLens scheduler cancelled")
            raise
        except Exception:  # noqa: BLE001 - defensive: to_thread itself failed
            logger.exception("scheduler pass crashed")

        # Monotonic deadline: the period does not drift when a pass is slow.
        now = loop.time()
        while next_deadline <= now:
            next_deadline += interval
        try:
            await asyncio.sleep(max(0.0, next_deadline - now))
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown path
            logger.info("TokenLens scheduler stopped")
            raise


def start(app: Any) -> asyncio.Task[None] | None:
    """Start the background task and return it (``None`` when the scheduler is disabled).

    Idempotent: calling it twice returns the already running task instead of spawning a
    second loop. Must be called from within a running event loop (``app`` lifespan).
    """
    settings, _ = _resolve(app)
    if not getattr(settings, "enable_scheduler", True):
        logger.info("TokenLens scheduler disabled by settings")
        return None

    existing = getattr(app.state, TASK_ATTR, None)
    if isinstance(existing, asyncio.Task) and not existing.done():
        logger.debug("TokenLens scheduler already running")
        return existing

    task = asyncio.get_event_loop().create_task(_loop(app, settings), name="tokenlens-scheduler")
    setattr(app.state, TASK_ATTR, task)
    return task


async def stop(app: Any, *, timeout: float = 5.0) -> None:
    """Cancel the background task and wait for it to settle.

    ``CancelledError`` is swallowed (that is the *expected* outcome of a cancel) and a
    stubborn task is abandoned after ``timeout`` seconds so shutdown can never hang.
    """
    task = getattr(getattr(app, "state", None), TASK_ATTR, None)
    if task is None:
        return
    setattr(app.state, TASK_ATTR, None)
    if not isinstance(task, asyncio.Task) or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:  # noqa: BLE001 - shutdown is best effort
        logger.debug("scheduler task raised during shutdown", exc_info=True)
