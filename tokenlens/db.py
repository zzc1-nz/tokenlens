"""SQLite storage layer.

Design notes (these are the interesting engineering decisions of the project):

* **WAL + NORMAL synchronous** — the ingest path is write heavy while the dashboard polls
  read heavy; WAL lets readers and the writer proceed concurrently instead of serialising
  on a global lock.
* **Connection per unit of work** — SQLite objects are not thread safe and FastAPI runs
  sync endpoints in a thread pool. Opening a connection is cheap (microseconds) and this
  avoids ``check_same_thread=False`` foot-guns entirely.
* **Raw snapshots are the source of truth**, aggregates are always computed by SQL. The
  hourly rollup table exists as a *cache* that the scheduler maintains; every query has a
  raw-table fallback so a fresh database or a partially rolled-up window is still correct.
* **Idempotent ingest** — ``usage_snapshots.event_id`` is ``UNIQUE``; a replayed batch is
  counted as duplicate instead of inflating spend.
* **Money is REAL (float64) rounded to 6 decimals** — with per-token prices around
  1e-7 USD, 6 decimals is 1/100 of a cent; SQLite has no DECIMAL type and SUM() over
  REAL is accurate enough at this magnitude (verified in tests).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import get_settings

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS usage_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           TEXT UNIQUE,
    ts                 INTEGER NOT NULL,
    received_at        INTEGER NOT NULL,
    agent              TEXT NOT NULL,
    agent_version      TEXT,
    runtime            TEXT NOT NULL,
    project            TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    model              TEXT NOT NULL,
    provider           TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    requests           INTEGER NOT NULL DEFAULT 1,
    retries            INTEGER NOT NULL DEFAULT 0,
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER,
    context_window     INTEGER,
    tool_result_bytes  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    price_version      TEXT,
    tags               TEXT,
    meta               TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON usage_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_agent_ts ON usage_snapshots(agent, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_project_ts ON usage_snapshots(project, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_model_ts ON usage_snapshots(model, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_session_ts ON usage_snapshots(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_runtime_ts ON usage_snapshots(runtime, ts);

CREATE TABLE IF NOT EXISTS usage_rollup_hourly (
    bucket_ms          INTEGER NOT NULL,
    agent              TEXT NOT NULL,
    project            TEXT NOT NULL,
    model              TEXT NOT NULL,
    runtime            TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    requests           INTEGER NOT NULL DEFAULT 0,
    retries            INTEGER NOT NULL DEFAULT 0,
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_ms, agent, project, model)
);
CREATE INDEX IF NOT EXISTS idx_rollup_bucket ON usage_rollup_hourly(bucket_ms);

CREATE TABLE IF NOT EXISTS agents (
    id             TEXT PRIMARY KEY,
    display_name   TEXT,
    runtime        TEXT,
    first_seen     INTEGER,
    last_seen      INTEGER,
    snapshot_count INTEGER NOT NULL DEFAULT 0,
    meta           TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    first_seen     INTEGER,
    last_seen      INTEGER,
    snapshot_count INTEGER NOT NULL DEFAULT 0,
    meta           TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    scope        TEXT NOT NULL,
    scope_key    TEXT NOT NULL DEFAULT '*',
    period       TEXT NOT NULL,
    limit_usd    REAL NOT NULL,
    thresholds   TEXT NOT NULL,
    webhook_url  TEXT,
    desktop      INTEGER NOT NULL DEFAULT 1,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id      INTEGER,
    budget_name    TEXT,
    scope          TEXT,
    scope_key      TEXT,
    period         TEXT,
    window_start   INTEGER,
    window_end     INTEGER,
    threshold      REAL,
    severity       TEXT,
    spent_usd      REAL,
    limit_usd      REAL,
    overspend_usd  REAL,
    trigger_count  INTEGER NOT NULL DEFAULT 1,
    message        TEXT,
    channels       TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    UNIQUE(budget_id, window_start, threshold)
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alert_events(created_at DESC);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at   INTEGER NOT NULL,
    agent         TEXT,
    project       TEXT,
    model         TEXT,
    window_start  INTEGER,
    window_end    INTEGER,
    metric        TEXT,
    observed      REAL,
    baseline      REAL,
    score         REAL,
    ratio         REAL,
    severity      TEXT,
    reason        TEXT,
    reason_label  TEXT,
    reason_detail TEXT,
    evidence      TEXT,
    tokens_burn   REAL,
    cost_usd      REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    fingerprint   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_anomalies_detected ON anomaly_events(detected_at DESC);

CREATE TABLE IF NOT EXISTS budget_state (
    budget_id        INTEGER NOT NULL,
    window_start     INTEGER NOT NULL,
    trigger_count    INTEGER NOT NULL DEFAULT 0,
    max_threshold    REAL NOT NULL DEFAULT 0,
    last_notified_at INTEGER,
    overspend_usd    REAL NOT NULL DEFAULT 0,
    updated_at       INTEGER,
    PRIMARY KEY (budget_id, window_start)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    """Thin, thread-safe-by-construction wrapper around a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialised = False

    # -- connection management -----------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Open a tuned connection. Callers own the lifetime (use :meth:`cursor`)."""
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA cache_size=-64000")  # 64 MB page cache
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def cursor(self, *, write: bool = False) -> Iterator[sqlite3.Cursor]:
        """Yield a cursor inside an explicit transaction for writes, autocommit for reads."""
        conn = self.connect()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            yield cur
            if write:
                conn.execute("COMMIT")
        except BaseException:
            if write:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - rollback best effort
                    pass
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the schema once per process (idempotent)."""
        with self._schema_lock:
            if self._initialised:
                return
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
            finally:
                conn.close()
            self._initialised = True

    # -- convenience helpers -------------------------------------------------------
    def fetch_all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def fetch_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = 0) -> Any:
        row = self.fetch_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        """Run a single write statement, returning ``lastrowid``."""
        with self.cursor(write=True) as cur:
            cur.execute(sql, params)
            return cur.lastrowid or 0

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self.cursor(write=True) as cur:
            cur.executemany(sql, rows)
            return cur.rowcount

    def table_counts(self) -> dict[str, int]:
        """Row counts per table (health endpoint / diagnostics)."""
        tables = [
            "usage_snapshots",
            "usage_rollup_hourly",
            "agents",
            "projects",
            "budgets",
            "alert_events",
            "anomaly_events",
        ]
        return {t: int(self.scalar(f"SELECT COUNT(*) FROM {t}")) for t in tables}

    def vacuum(self) -> None:
        conn = self.connect()
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    def reset(self) -> None:
        """Drop every row (tests / demo resets)."""
        conn = self.connect()
        try:
            for table in (
                "usage_snapshots",
                "usage_rollup_hourly",
                "agents",
                "projects",
                "budgets",
                "alert_events",
                "anomaly_events",
                "budget_state",
            ):
                conn.execute(f"DELETE FROM {table}")
        finally:
            conn.close()


# --------------------------------------------------------------------------------------
# Process wide database handle + FastAPI dependency
# --------------------------------------------------------------------------------------

_db_lock = threading.Lock()
_db: Database | None = None


def get_database(path: str | Path | None = None) -> Database:
    """Return the process-wide :class:`Database`, creating and initialising it lazily."""
    global _db
    with _db_lock:
        if _db is None or (path is not None and Path(path) != _db.path):
            _db = Database(path or get_settings().db_path)
            _db.initialize()
        return _db


def set_database(db: Database | None) -> None:
    """Swap the process-wide database (used by tests)."""
    global _db
    with _db_lock:
        _db = db


def get_db(request: Request) -> Database:
    """FastAPI dependency: the database bound to the current app (``app.state.db``).

    Every request resolves the handle from ``app.state`` so a test (or a second app in
    the same process) can never accidentally write into the production database.
    """
    app = getattr(request, "app", None)
    db = getattr(getattr(app, "state", None), "db", None) if app is not None else None
    if isinstance(db, Database):
        return db
    return get_database()


try:  # pragma: no cover - import shim so the annotation resolves for FastAPI
    from fastapi import Request  # noqa: F401
except Exception:  # pragma: no cover - FastAPI is a hard dependency in practice
    Request = None  # type: ignore[assignment]


# --------------------------------------------------------------------------------------
# Small shared helpers used by several routers
# --------------------------------------------------------------------------------------


def dump_json(value: Any) -> str | None:
    """Serialise a JSON column; ``None`` stays ``NULL``."""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return json.dumps(str(value))


def load_json(value: str | None, default: Any = None) -> Any:
    """Deserialise a JSON column, tolerating legacy/garbage values."""
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return default


def build_filters(
    *,
    agent: str | None = None,
    project: str | None = None,
    model: str | None = None,
    runtime: str | None = None,
    session_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    alias: str = "s",
) -> tuple[str, list[Any]]:
    """Build a parameterised ``WHERE`` clause over ``usage_snapshots``.

    This is the single place where filter SQL is assembled, so the query, trends, export
    and dashboard routers cannot drift apart in behaviour (or in injection safety).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if start_ms is not None:
        clauses.append(f"{alias}.ts >= ?")
        params.append(int(start_ms))
    if end_ms is not None:
        clauses.append(f"{alias}.ts < ?")
        params.append(int(end_ms))
    for column, value in (
        ("agent", agent),
        ("project", project),
        ("model", model),
        ("runtime", runtime),
        ("session_id", session_id),
    ):
        if value:
            clauses.append(f"{alias}.{column} = ?")
            params.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    """``sqlite3.Row`` -> plain dict (JSON friendly)."""
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}
