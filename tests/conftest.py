"""Shared pytest fixtures.

Every test runs against an isolated SQLite file in ``tmp_path`` with the background
scheduler disabled, so tests are deterministic and never touch ``data/tokenlens.db``.

The suite also **freezes the application clock** (see :func:`frozen_clock`). This is not
cosmetic: the HTTP layer resolves budget windows and anomaly lookbacks from
``timeutil.now_ms()``, so a test that seeds snapshots at a fixed timestamp and then calls
``POST /api/v1/budgets/evaluate`` is only correct if "now" is also fixed. Without the
freeze such a test silently passes whenever the wall clock happens to fall inside the
same calendar window as the fixture data, and fails the moment the date rolls over.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PYLIBS = REPO_ROOT / ".pylibs"
if PYLIBS.exists() and str(PYLIBS) not in sys.path:
    sys.path.insert(0, str(PYLIBS))

from tokenlens import pricing, timeutil  # noqa: E402
from tokenlens.app import create_app  # noqa: E402
from tokenlens.config import Settings  # noqa: E402
from tokenlens.db import Database  # noqa: E402
from tokenlens.schemas import SnapshotIn  # noqa: E402
from tokenlens.store import ingest_snapshots  # noqa: E402

#: The single clock anchor every fixture-derived timestamp is relative to.
#: 2026-11-10T06:00:00Z == 14:00 Asia/Shanghai: mid-day, mid-week, mid-month, so no test
#: window straddles a calendar boundary regardless of which timezone the window is in.
FROZEN_NOW = timeutil.parse_time_ms("2026-11-10T06:00:00Z")


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> int:
    """Pin ``timeutil.now_ms()`` for every test.

    Autouse on purpose: any test that exercises a time-derived window (budgets, trends,
    live burn rate, anomaly lookback) must see the same "now" that the fixtures seeded
    around, otherwise the result depends on the day the suite is run.
    """
    monkeypatch.setattr(timeutil, "now_ms", lambda: FROZEN_NOW)
    return FROZEN_NOW


@pytest.fixture()
def tmp_path(request) -> Iterator[Path]:
    """Workspace-local ``tmp_path`` replacement.

    The sandbox blocks writes inside directories created by ``tempfile.mkdtemp``, which
    is exactly what pytest's built-in ``tmp_path`` uses. We allocate a plain directory
    under ``<repo>/.pytest-tmp`` instead and clean it up afterwards.
    """
    base = REPO_ROOT / ".pytest-tmp"
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{request.node.name[:40].replace('/', '_')}-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    yield target
    import shutil

    shutil.rmtree(target, ignore_errors=True)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Isolated settings: temp DB, scheduler off, notifications into tmp dir."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        db_path=data_dir / "test.db",
        data_dir=data_dir,
        api_token=None,
        budget_interval_seconds=3600,
        notify_file=data_dir / "notifications.jsonl",
        display_tz="Asia/Shanghai",
        enable_scheduler=False,
    )


@pytest.fixture()
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings.db_path)
    database.initialize()
    yield database
    database.reset()


@pytest.fixture()
def app(settings: Settings, db: Database):
    """FastAPI app bound to the isolated DB (scheduler disabled)."""
    application = create_app(settings=settings, db=db)
    return application


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client



