"""Ingest pipeline: validate -> price -> persist -> update catalogs.

The write path is the throughput-critical part of the system (the simulator pushes
millions of tokens through it), so it is implemented as a single transaction with
``executemany``:

1. every snapshot is priced by :mod:`tokenlens.pricing` (pure function, no I/O);
2. one ``INSERT OR IGNORE`` batch writes the rows — the ``event_id`` UNIQUE constraint
   gives us idempotency for free, and ``changes()`` tells us how many actually landed;
3. the ``agents``/``projects`` catalogs are upserted in the same transaction;
4. the hourly rollup is updated incrementally so dashboard queries stay cheap.

Everything is synchronous and CPU-bound apart from SQLite itself, which is why the API
layer can run it inside FastAPI's thread pool without blocking the event loop.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import pricing, timeutil
from .config import Settings, get_settings
from .db import Database, dump_json
from .schemas import IngestError, SnapshotIn

INSERT_SQL = """
INSERT OR IGNORE INTO usage_snapshots (
    event_id, ts, received_at, agent, agent_version, runtime, project, session_id,
    model, provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    reasoning_tokens, requests, retries, tool_calls, latency_ms, context_window,
    tool_result_bytes, cost_usd, price_version, tags, meta
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

UPSERT_AGENT_SQL = """
INSERT INTO agents (id, display_name, runtime, first_seen, last_seen, snapshot_count, meta)
VALUES (?,?,?,?,?,1,NULL)
ON CONFLICT(id) DO UPDATE SET
    last_seen = MAX(agents.last_seen, excluded.last_seen),
    first_seen = MIN(COALESCE(agents.first_seen, excluded.first_seen), excluded.first_seen),
    snapshot_count = agents.snapshot_count + 1,
    runtime = COALESCE(agents.runtime, excluded.runtime)
"""

UPSERT_PROJECT_SQL = """
INSERT INTO projects (id, name, first_seen, last_seen, snapshot_count, meta)
VALUES (?,?,?,?,1,NULL)
ON CONFLICT(id) DO UPDATE SET
    last_seen = MAX(projects.last_seen, excluded.last_seen),
    first_seen = MIN(COALESCE(projects.first_seen, excluded.first_seen), excluded.first_seen),
    snapshot_count = projects.snapshot_count + 1
"""

ROLLUP_SQL = """
INSERT INTO usage_rollup_hourly (
    bucket_ms, agent, project, model, runtime, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, reasoning_tokens, requests, retries,
    tool_calls, cost_usd
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(bucket_ms, agent, project, model) DO UPDATE SET
    input_tokens = usage_rollup_hourly.input_tokens + excluded.input_tokens,
    output_tokens = usage_rollup_hourly.output_tokens + excluded.output_tokens,
    cache_read_tokens = usage_rollup_hourly.cache_read_tokens + excluded.cache_read_tokens,
    cache_write_tokens = usage_rollup_hourly.cache_write_tokens + excluded.cache_write_tokens,
    reasoning_tokens = usage_rollup_hourly.reasoning_tokens + excluded.reasoning_tokens,
    requests = usage_rollup_hourly.requests + excluded.requests,
    retries = usage_rollup_hourly.retries + excluded.retries,
    tool_calls = usage_rollup_hourly.tool_calls + excluded.tool_calls,
    cost_usd = usage_rollup_hourly.cost_usd + excluded.cost_usd
"""


@dataclass(slots=True)
class IngestResult:
    """Outcome of one ingest batch (serialised as :class:`tokenlens.schemas.IngestResponse`)."""

    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[IngestError] = field(default_factory=list)
    total_tokens: int = 0
    cost_usd: float = 0.0
    ingest_ms: float = 0.0
    price_version: str = pricing.PRICE_VERSION
    unknown_models: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": [e.model_dump() for e in self.errors],
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "ingest_ms": round(self.ingest_ms, 3),
            "price_version": self.price_version,
            "unknown_models": self.unknown_models,
        }


def ensure_event_id(snapshot: SnapshotIn) -> str:
    """Derive a stable id for snapshots that do not carry one.

    Clients *should* send ``event_id`` (that is what makes retries idempotent). When they
    do not, we synthesise one from the identity of the usage record so that a byte
    identical replay within the same millisecond is still de-duplicated.
    """
    if snapshot.event_id:
        return snapshot.event_id
    seed = "|".join(
        str(part)
        for part in (
            snapshot.ts,
            snapshot.agent,
            snapshot.project,
            snapshot.session_id,
            snapshot.model,
            snapshot.input_tokens,
            snapshot.output_tokens,
            snapshot.cache_read_tokens,
            snapshot.cache_write_tokens,
            snapshot.reasoning_tokens,
            snapshot.requests,
            snapshot.retries,
            snapshot.tool_calls,
        )
    )
    # Not a cryptographic hash requirement — just a compact, collision-resistant key.
    import hashlib

    return "auto-" + hashlib.blake2b(seed.encode("utf-8"), digest_size=12).hexdigest()


def _prepare_rows(
    snapshots: Sequence[SnapshotIn],
    received_at: int,
    result: IngestResult,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], dict[tuple[int, str, str, str], list[Any]]]:
    """Price snapshots and build insert payloads (pure CPU, no I/O)."""
    rows: list[tuple[Any, ...]] = []
    unknown: set[str] = set()
    rollup: dict[tuple[int, str, str, str], list[Any]] = {}

    for index, snap in enumerate(snapshots):
        ts = int(snap.ts) if snap.ts is not None else received_at
        cost, canonical_model, known = pricing.compute_cost(
            snap.model,
            input_tokens=snap.input_tokens,
            output_tokens=snap.output_tokens,
            cache_read_tokens=snap.cache_read_tokens,
            cache_write_tokens=snap.cache_write_tokens,
            reasoning_tokens=snap.reasoning_tokens,
            runtime=snap.runtime,
        )
        if not known:
            unknown.add(snap.model)
        provider = pricing.infer_provider(canonical_model, snap.runtime)
        event_id = ensure_event_id(snap)
        rows.append(
            (
                event_id,
                ts,
                received_at,
                snap.agent,
                snap.agent_version,
                snap.runtime,
                snap.project,
                snap.session_id,
                canonical_model,
                provider,
                snap.input_tokens,
                snap.output_tokens,
                snap.cache_read_tokens,
                snap.cache_write_tokens,
                snap.reasoning_tokens,
                snap.requests,
                snap.retries,
                snap.tool_calls,
                snap.latency_ms,
                snap.context_window,
                snap.tool_result_bytes,
                cost,
                pricing.PRICE_VERSION,
                dump_json(snap.tags),
                dump_json(snap.meta),
            )
        )
        result.total_tokens += snap.total_tokens
        result.cost_usd += cost
        key = (timeutil.bucket_start(ts, "hour"), snap.agent, snap.project, canonical_model)
        bucket = rollup.setdefault(key, [0, 0, 0, 0, 0, 0, 0, 0, 0.0])
        bucket[0] += snap.input_tokens
        bucket[1] += snap.output_tokens
        bucket[2] += snap.cache_read_tokens
        bucket[3] += snap.cache_write_tokens
        bucket[4] += snap.reasoning_tokens
        bucket[5] += snap.requests
        bucket[6] += snap.retries
        bucket[7] += snap.tool_calls
        bucket[8] += cost
        _ = index  # kept for a future per-index error report

    result.unknown_models = sorted(unknown)[:50]
    return rows, [], rollup


def ingest_snapshots(
    db: Database,
    snapshots: Sequence[SnapshotIn],
    *,
    settings: Settings | None = None,
    received_at: int | None = None,
) -> IngestResult:
    """Persist a batch of snapshots atomically and return per-batch statistics."""
    settings = settings or get_settings()
    started = time.perf_counter()
    received_at = int(received_at if received_at is not None else timeutil.now_ms())
    result = IngestResult()

    if not snapshots:
        result.ingest_ms = (time.perf_counter() - started) * 1000.0
        return result
    if len(snapshots) > settings.max_ingest_batch:
        result.rejected = len(snapshots)
        result.errors = [
            IngestError(index=0, error=f"batch of {len(snapshots)} exceeds max {settings.max_ingest_batch}")
        ]
        result.ingest_ms = (time.perf_counter() - started) * 1000.0
        return result

    rows, _, rollup = _prepare_rows(snapshots, received_at, result)

    with db.cursor(write=True) as cur:
        cur.executemany(INSERT_SQL, rows)
        accepted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        result.duplicates = len(rows) - accepted
        result.accepted = accepted

        if accepted:
            # Catalog upserts: one row per distinct agent/project in this batch.
            agents: dict[str, tuple[Any, ...]] = {}
            projects: dict[str, tuple[Any, ...]] = {}
            for snap in snapshots:
                ts = int(snap.ts) if snap.ts is not None else received_at
                agents[snap.agent] = (snap.agent, snap.agent, snap.runtime, ts, ts)
                projects[snap.project] = (snap.project, snap.project, ts, ts)
            cur.executemany(UPSERT_AGENT_SQL, list(agents.values()))
            cur.executemany(UPSERT_PROJECT_SQL, list(projects.values()))
            cur.executemany(
                ROLLUP_SQL,
                [
                    (
                        bucket_ms,
                        agent,
                        project,
                        model,
                        "",
                        values[0],
                        values[1],
                        values[2],
                        values[3],
                        values[4],
                        values[5],
                        values[6],
                        values[7],
                        round(values[8], 6),
                    )
                    for (bucket_ms, agent, project, model), values in rollup.items()
                ],
            )

    result.ingest_ms = (time.perf_counter() - started) * 1000.0
    result.cost_usd = round(result.cost_usd, 6)
    return result


def ingest_one(db: Database, snapshot: SnapshotIn, *, settings: Settings | None = None) -> IngestResult:
    """Convenience wrapper for the single-snapshot endpoint."""
    return ingest_snapshots(db, [snapshot], settings=settings)


def new_event_id() -> str:
    """Generate a client-side event id (used by the CLI simulator and tests)."""
    return uuid.uuid4().hex
