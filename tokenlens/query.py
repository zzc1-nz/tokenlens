"""Read-side SQL aggregation layer.

Every read endpoint (usage, trends, catalog, CSV export, dashboard) funnels through this
module, so the aggregation semantics only exist once:

* **Filters are always assembled by** :func:`tokenlens.db.build_filters` — one place, one
  parameterisation style, no SQL injection surface and no drift between routers.
* **Raw snapshots are the source of truth.** ``usage_rollup_hourly`` is a cache maintained
  by the ingest path; a partially rolled-up window would silently under-report, so the
  query layer always aggregates ``usage_snapshots`` directly and stays correct on a fresh
  database. (SQLite scans ~1M indexed rows in tens of milliseconds, which is far cheaper
  than an inconsistent dashboard.)
* **Time bucketing is done in SQL with integer arithmetic** (minute/hour/day/week) and
  ``strftime(..., 'start of month')`` for calendar months. The expressions below are
  byte-for-byte equivalent to :func:`tokenlens.timeutil.bucket_start`, which is asserted in
  ``tests/test_query_api.py`` so the two implementations can never disagree.
* **Gap filling happens in Python** via :func:`tokenlens.timeutil.iter_buckets`: an empty
  hour must render as a zero point, not as a missing one, otherwise a chart lies about
  continuity.

Money is rounded to 6 decimals (see the module docstring of :mod:`tokenlens.db`), tokens
stay integers. Every function tolerates an empty database and returns zeros / empty lists
instead of raising.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import pricing, timeutil
from .db import Database, build_filters, row_to_dict
from .schemas import AgentInfo, BreakdownRow, ProjectInfo, SessionInfo, TimePoint, Totals

__all__ = [
    "BUCKETS",
    "CSV_DATASETS",
    "DEFAULT_TZ",
    "DIMENSIONS",
    "TOTALS_SELECT",
    "breakdown",
    "catalog_agents",
    "catalog_projects",
    "catalog_sessions",
    "csv_rows",
    "count_buckets",
    "last_n_buckets",
    "live_metrics",
    "normalize_bucket",
    "normalize_dataset",
    "normalize_dimension",
    "normalize_filters",
    "normalize_period",
    "resolve_window",
    "scope_to_filters",
    "timeseries",
    "totals_for_window",
    "window_before",
]

#: Display timezone used for bucket labels when the caller does not pass one.
DEFAULT_TZ = "Asia/Shanghai"

#: Canonical bucket names accepted by :func:`timeseries`.
BUCKETS: tuple[str, ...] = ("minute", "hour", "day", "week", "month")

#: Breakdown dimensions accepted by :func:`breakdown`.
DIMENSIONS: tuple[str, ...] = ("agent", "project", "model", "runtime", "session")

#: Datasets supported by the CSV export endpoint.
CSV_DATASETS: tuple[str, ...] = (
    "timeseries",
    "breakdown",
    "sessions",
    "snapshots",
    "alerts",
    "anomalies",
    "budgets",
)

#: Upper bound on the number of points a single time series may materialise. A year of
#: minute buckets is half a million points — far more than any chart can use, and enough
#: to make a careless client allocate hundreds of MB. Rejected with HTTP 400 instead.
MAX_POINTS = 20_000

#: Hard cap on rows produced by :func:`csv_rows` (mirrors the export endpoint contract).
MAX_EXPORT_ROWS = 100_000

#: Snapshot columns accepted as filters (``session`` maps to the ``session_id`` column).
_FILTER_KEYS: tuple[str, ...] = ("agent", "project", "model", "runtime", "session_id")

#: Breakdown dimension -> physical column on ``usage_snapshots``.
_DIMENSION_COLUMNS: dict[str, str] = {
    "agent": "agent",
    "project": "project",
    "model": "model",
    "runtime": "runtime",
    "session": "session_id",
}

#: Aggregate projection shared by every totals-producing query.
#:
#: Kept as one constant so the totals formula (including ``billable_tokens`` and the
#: distinct entity counts) is defined exactly once. Reusable by other modules that need
#: the same aggregate over a custom ``WHERE``/``GROUP BY``.
TOTALS_SELECT = """
    SUM(s.input_tokens)                                        AS input_tokens,
    SUM(s.output_tokens)                                       AS output_tokens,
    SUM(s.cache_read_tokens)                                   AS cache_read_tokens,
    SUM(s.cache_write_tokens)                                  AS cache_write_tokens,
    SUM(s.reasoning_tokens)                                    AS reasoning_tokens,
    (SUM(s.input_tokens) + SUM(s.output_tokens) + SUM(s.cache_read_tokens)
     + SUM(s.cache_write_tokens) + SUM(s.reasoning_tokens))    AS total_tokens,
    (SUM(s.input_tokens) + SUM(s.output_tokens) + SUM(s.reasoning_tokens))
                                                               AS billable_tokens,
    SUM(s.requests)                                            AS requests,
    SUM(s.retries)                                             AS retries,
    SUM(s.tool_calls)                                          AS tool_calls,
    SUM(s.cost_usd)                                            AS cost_usd,
    COUNT(DISTINCT s.session_id)                               AS sessions,
    COUNT(DISTINCT s.agent)                                    AS agents,
    COUNT(DISTINCT s.project)                                  AS projects
"""

#: SQL expression that floors ``s.ts`` to a bucket start (mirrors timeutil.bucket_start).
_BUCKET_EXPRESSIONS: dict[str, str] = {
    "minute": "CAST(s.ts / 60000 AS INTEGER) * 60000",
    "hour": "CAST(s.ts / 3600000 AS INTEGER) * 3600000",
    "day": "CAST(s.ts / 86400000 AS INTEGER) * 86400000",
    # 1970-01-01 was a Thursday, so shifting by 3 days makes Monday day 0 of the week.
    "week": (
        "(CAST(s.ts / 86400000 AS INTEGER)"
        " - ((CAST(s.ts / 86400000 AS INTEGER) + 3) % 7)) * 86400000"
    ),
    "month": "CAST(strftime('%s', s.ts / 1000, 'unixepoch', 'start of month') AS INTEGER) * 1000",
}

#: Totals fields in their canonical order (CSV headers reuse this).
TOTALS_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "billable_tokens",
    "requests",
    "retries",
    "tool_calls",
    "cost_usd",
    "sessions",
    "agents",
    "projects",
    "cache_hit_rate",
    "avg_cost_per_request",
    "avg_tokens_per_request",
)


# --------------------------------------------------------------------------------------
# Validation / normalisation helpers
# --------------------------------------------------------------------------------------


def normalize_bucket(bucket: str | None, default: str = "hour") -> str:
    """Return a canonical bucket name, raising ``ValueError`` for unsupported input."""
    return timeutil.normalize_bucket(bucket, default)


def normalize_dimension(dimension: str | None, default: str = "agent") -> str:
    """Validate a breakdown dimension (``agent|project|model|runtime|session``)."""
    key = str(dimension or default).strip().lower()
    aliases = {"session_id": "session", "sessions": "session"}
    key = aliases.get(key, key)
    if key not in DIMENSIONS:
        raise ValueError(
            f"unsupported dimension: {dimension!r} (expected one of {', '.join(DIMENSIONS)})"
        )
    return key


def normalize_period(period: str | None, default: str = "day") -> str:
    """Validate a trend period (``day|week|month``, plus daily/weekly/monthly aliases)."""
    key = str(period or default).strip().lower()
    aliases = {
        "daily": "day",
        "weekly": "week",
        "monthly": "month",
        "d": "day",
        "w": "week",
        "mo": "month",
    }
    key = aliases.get(key, key)
    if key not in ("day", "week", "month"):
        raise ValueError(f"unsupported period: {period!r} (expected day, week or month)")
    return key


def normalize_dataset(dataset: str | None, default: str = "timeseries") -> str:
    """Validate a CSV export dataset name."""
    key = str(dataset or default).strip().lower()
    if key not in CSV_DATASETS:
        raise ValueError(
            f"unsupported dataset: {dataset!r} (expected one of {', '.join(CSV_DATASETS)})"
        )
    return key


def normalize_filters(**filters: Any) -> dict[str, str]:
    """Keep only non-empty snapshot filters, stripped of whitespace.

    Unknown keys are ignored so a router can hand over its whole query dict. The returned
    mapping is exactly what the response ``filters`` object echoes back to the client.
    """
    clean: dict[str, str] = {}
    for key in _FILTER_KEYS:
        value = filters.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            clean[key] = text
    return clean


def scope_to_filters(scope: str, scope_key: str | None = None) -> dict[str, str]:
    """Translate a budget/anomaly scope (``global|project|agent|model``) into filters.

    Shared with the budget engine so "spend for this budget scope" and "spend for this
    dashboard filter" can never diverge.
    """
    key = str(scope or "global").strip().lower()
    if key == "global":
        return {}
    if key not in ("project", "agent", "model"):
        raise ValueError(f"unsupported scope: {scope!r}")
    value = (scope_key or "").strip()
    if not value or value == "*":
        return {}
    return {key: value}


def _validate_window(start_ms: int | None, end_ms: int | None) -> tuple[int, int]:
    """Coerce and validate a half-open ``[start, end)`` window in epoch ms."""
    if start_ms is None or end_ms is None:
        raise ValueError("both 'from' and 'to' are required for this query")
    start, end = int(start_ms), int(end_ms)
    if start >= end:
        raise ValueError("'from' must be earlier than 'to'")
    return start, end


def resolve_window(
    from_value: object,
    to_value: object,
    *,
    default_hours: float = 24.0,
) -> tuple[int, int]:
    """Resolve the ``from``/``to`` query parameters into a concrete window.

    ``to`` defaults to now and ``from`` defaults to ``to - default_hours``. Raises
    ``ValueError`` (which the app maps to HTTP 400) when the window is inverted or a
    timestamp cannot be parsed.
    """
    return timeutil.window_from_request(from_value, to_value, default_hours=default_hours)


def _where(
    filters: Mapping[str, str], start_ms: int, end_ms: int, alias: str = "s"
) -> tuple[str, list[Any]]:
    """Build the shared ``WHERE`` clause for a window + filters."""
    return build_filters(start_ms=start_ms, end_ms=end_ms, alias=alias, **dict(filters))


def _totals(row: Any) -> Totals:
    """``sqlite3.Row`` (or mapping) -> :class:`Totals`."""
    return Totals.from_mapping(row_to_dict(row) if not isinstance(row, Mapping) else row)


def _label_for(dimension: str, key: str) -> str:
    """Human friendly label for a breakdown key (models get their catalog name)."""
    if dimension == "model":
        entry, _known = pricing.resolve_model(key)
        return entry.display_name or key
    return key


def _share_pct(cost_usd: float, total_cost_usd: float) -> float:
    """Row share of the window cost, ``0`` when the window is free/empty."""
    if total_cost_usd <= 0:
        return 0.0
    return round(cost_usd / total_cost_usd * 100.0, 6)


# --------------------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------------------


def totals_for_window(
    db: Database,
    start_ms: int,
    end_ms: int,
    *,
    agent: str | None = None,
    project: str | None = None,
    model: str | None = None,
    runtime: str | None = None,
    session_id: str | None = None,
) -> Totals:
    """Aggregate every token/cost metric of ``[start_ms, end_ms)`` under the filters.

    This is the single totals implementation used by ``/usage/summary``, the breakdown
    share denominator, trends comparison, budgets (via ``scope_to_filters``) and the
    dashboard KPIs. Returns :meth:`Totals.zero` for an empty window.
    """
    start, end = _validate_window(start_ms, end_ms)
    filters = normalize_filters(
        agent=agent, project=project, model=model, runtime=runtime, session_id=session_id
    )
    where, params = _where(filters, start, end)
    row = db.fetch_one(f"SELECT {TOTALS_SELECT} FROM usage_snapshots s{where}", params)
    return _totals(row)


# --------------------------------------------------------------------------------------
# Breakdown
# --------------------------------------------------------------------------------------


def breakdown(
    db: Database,
    dimension: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 50,
    offset: int = 0,
    **filters: Any,
) -> list[BreakdownRow]:
    """Group the window by ``dimension`` and return rows sorted by cost descending.

    ``share_pct`` is relative to the **whole window** total (not to the truncated page), so
    a limited page still reports honest percentages. Ties break on the key for stable
    pagination.
    """
    dimension = normalize_dimension(dimension)
    start, end = _validate_window(start_ms, end_ms)
    clean = normalize_filters(**filters)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    column = _DIMENSION_COLUMNS[dimension]
    where, params = _where(clean, start, end)
    sql = (
        f"SELECT s.{column} AS key, {TOTALS_SELECT}"
        f" FROM usage_snapshots s{where}"
        f" GROUP BY s.{column}"
        f" ORDER BY cost_usd DESC, key ASC"
        f" LIMIT ? OFFSET ?"
    )
    rows = db.fetch_all(sql, [*params, limit, offset])

    total = totals_for_window(db, start, end, **clean)
    result: list[BreakdownRow] = []
    for row in rows:
        item = row_to_dict(row)
        key = str(item.get("key") or "unknown")
        totals = Totals.from_mapping(item)
        result.append(
            BreakdownRow(
                key=key,
                label=_label_for(dimension, key),
                totals=totals,
                share_pct=_share_pct(totals.cost_usd, total.cost_usd),
            )
        )
    return result


# --------------------------------------------------------------------------------------
# Time series
# --------------------------------------------------------------------------------------


def count_buckets(start_ms: int, end_ms: int, bucket: str) -> int:
    """Number of buckets :func:`timeseries` would materialise for a window.

    O(1) for the fixed-width buckets (minute/hour/day/week) so the guard on every request
    costs nothing; calendar months genuinely vary in length and are counted by walking.
    """
    bucket = normalize_bucket(bucket)
    start, end = _validate_window(start_ms, end_ms)
    fixed_width = timeutil.BUCKET_MS.get(bucket)
    if bucket != "month" and fixed_width:
        first = timeutil.bucket_start(start, bucket)
        return 0 if first >= end else (end - first + fixed_width - 1) // fixed_width
    cursor = timeutil.bucket_start(start, bucket)
    total = 0
    while cursor < end:
        total += 1
        cursor = timeutil.bucket_end(cursor, bucket)
    return total


def timeseries(
    db: Database,
    bucket: str,
    start_ms: int,
    end_ms: int,
    *,
    tz_name: str = DEFAULT_TZ,
    max_points: int = MAX_POINTS,
    **filters: Any,
) -> list[TimePoint]:
    """Bucket the window by ``bucket`` and gap-fill every bucket in ``[start, end)``.

    Points are ascending and always contiguous: a bucket with no traffic is present with
    :meth:`Totals.zero`, which is what a chart needs to draw a truthful baseline. A window
    that would materialise more than ``max_points`` buckets is rejected with ``ValueError``
    (HTTP 400) rather than silently allocating an unbounded response.
    """
    bucket = normalize_bucket(bucket)
    start, end = _validate_window(start_ms, end_ms)
    clean = normalize_filters(**filters)

    if count_buckets(start, end, bucket) > max_points:
        raise ValueError(
            f"window too large for bucket={bucket!r}: at most {max_points} points per request"
        )

    expression = _BUCKET_EXPRESSIONS[bucket]
    where, params = _where(clean, start, end)
    sql = (
        f"SELECT {expression} AS bucket_ms, {TOTALS_SELECT}"
        f" FROM usage_snapshots s{where}"
        f" GROUP BY bucket_ms ORDER BY bucket_ms ASC"
    )
    rows = db.fetch_all(sql, params)
    by_bucket = {int(row_to_dict(row)["bucket_ms"]): _totals(row) for row in rows}

    points: list[TimePoint] = []
    for bucket_ms in timeutil.iter_buckets(start, end, bucket):
        points.append(
            TimePoint(
                bucket_start=timeutil.ms_to_dt(bucket_ms),
                bucket_end=timeutil.ms_to_dt(timeutil.bucket_end(bucket_ms, bucket)),
                label=timeutil.label_for(bucket_ms, bucket, tz_name),
                totals=by_bucket.get(bucket_ms, Totals.zero()),
            )
        )
    return points


def last_n_buckets(
    bucket: str, count: int, *, at_ms: int | None = None
) -> tuple[int, int]:
    """Window covering the last ``count`` buckets, ending at the *current* bucket.

    ``count`` buckets inclusive of the bucket that contains ``at_ms`` (default: now), i.e.
    the window a ``/trends?limit=N`` chart renders. Calendar aware: walking back N months
    lands on the first day of the Nth previous month, not on "now minus 30*N days".
    """
    bucket = normalize_bucket(bucket)
    count = int(count)
    if count <= 0:
        raise ValueError("limit must be >= 1")
    if count > 5000:  # pragma: no cover - defensive guard against accidental scans
        raise ValueError("limit must be <= 5000")
    current = timeutil.bucket_start(at_ms if at_ms is not None else timeutil.now_ms(), bucket)
    start = current
    for _ in range(count - 1):
        start = timeutil.bucket_start(start - 1, bucket)
    return start, timeutil.bucket_end(current, bucket)


def window_before(bucket: str, start_ms: int, count: int = 1) -> tuple[int, int]:
    """The ``count`` buckets immediately preceding the window that starts at ``start_ms``.

    Used by ``/trends`` to build a calendar-correct comparison window.
    """
    bucket = normalize_bucket(bucket)
    count = max(1, int(count))
    cursor = int(start_ms)
    for _ in range(count):
        cursor = timeutil.bucket_start(cursor - 1, bucket)
    return cursor, int(start_ms)


# --------------------------------------------------------------------------------------
# Live / burn-rate metrics
# --------------------------------------------------------------------------------------


def _top_entities(
    db: Database,
    column: str,
    start_ms: int,
    end_ms: int,
    window_minutes: float,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Top ``column`` values (project/agent) by cost inside a trailing window."""
    sql = (
        f"SELECT s.{column} AS key, SUM(s.cost_usd) AS cost_usd,"
        f" (SUM(s.input_tokens) + SUM(s.output_tokens) + SUM(s.cache_read_tokens)"
        f"  + SUM(s.cache_write_tokens) + SUM(s.reasoning_tokens)) AS total_tokens"
        f" FROM usage_snapshots s WHERE s.ts >= ? AND s.ts < ?"
        f" GROUP BY s.{column}"
        f" ORDER BY cost_usd DESC, total_tokens DESC, key ASC LIMIT ?"
    )
    rows = db.fetch_all(sql, [int(start_ms), int(end_ms), int(limit)])
    entries: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row)
        entries.append(
            {
                column: str(item.get("key") or "unknown"),
                "cost_usd": round(float(item.get("cost_usd") or 0.0), 6),
                "tokens_per_min": round(
                    float(item.get("total_tokens") or 0) / window_minutes, 2
                ),
            }
        )
    return entries


def live_metrics(db: Database, window_minutes: float = 5.0) -> dict[str, Any]:
    """Burn-rate snapshot computed from the raw snapshots of the trailing window.

    Returns a JSON-ready dict matching :class:`tokenlens.schemas.LiveResponse`:
    tokens/minute, USD/hour, the number of active agents/projects/sessions and the top
    spenders. Deliberately *not* derived from hourly rollups — a five minute window is
    smaller than one rollup bucket.
    """
    try:
        minutes = float(window_minutes)
    except (TypeError, ValueError) as exc:  # pragma: no cover - router validates first
        raise ValueError(f"invalid window_minutes: {window_minutes!r}") from exc
    if minutes <= 0:
        raise ValueError("window_minutes must be > 0")
    if minutes > 24 * 60:
        raise ValueError("window_minutes must be <= 1440")

    now = timeutil.now_ms()
    start = now - int(minutes * 60_000)
    row = db.fetch_one(
        f"SELECT {TOTALS_SELECT} FROM usage_snapshots s WHERE s.ts >= ? AND s.ts < ?",
        [start, now],
    )
    totals = _totals(row)

    return {
        "window_minutes": round(minutes, 3),
        "tokens_per_min": round(totals.total_tokens / minutes, 2),
        "cost_per_hour": round(totals.cost_usd * 60.0 / minutes, 6),
        "active_agents": totals.agents,
        "active_projects": totals.projects,
        "active_sessions": totals.sessions,
        "top_projects": _top_entities(db, "project", start, now, minutes),
        "top_agents": _top_entities(db, "agent", start, now, minutes),
        "generated_at": timeutil.iso(now),
    }


# --------------------------------------------------------------------------------------
# Catalog listings
# --------------------------------------------------------------------------------------


def _catalog_total(db: Database, sql: str, params: Sequence[Any]) -> int:
    return int(db.scalar(sql, params, default=0))


def catalog_agents(db: Database, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Agent catalog: identity plus lifetime spend, sorted by cost descending.

    Derived from ``usage_snapshots`` (the source of truth) and enriched from the
    ``agents`` catalog table, so the listing is correct even if the catalog table was
    rebuilt or is stale.
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    sql = f"""
        SELECT s.agent AS id,
               COALESCE(a.display_name, s.agent) AS display_name,
               COALESCE(a.runtime, MAX(s.runtime)) AS runtime,
               MIN(s.ts) AS first_seen,
               MAX(s.ts) AS last_seen,
               COUNT(*) AS snapshot_count,
               SUM(s.cost_usd) AS cost_usd,
               (SUM(s.input_tokens) + SUM(s.output_tokens) + SUM(s.cache_read_tokens)
                + SUM(s.cache_write_tokens) + SUM(s.reasoning_tokens)) AS total_tokens
        FROM usage_snapshots s
        LEFT JOIN agents a ON a.id = s.agent
        GROUP BY s.agent
        ORDER BY cost_usd DESC, id ASC
        LIMIT ? OFFSET ?
    """
    rows = db.fetch_all(sql, [limit, offset])
    items = [
        AgentInfo(
            id=str(item.get("id") or "unknown"),
            display_name=str(item.get("display_name") or item.get("id") or "unknown"),
            runtime=item.get("runtime"),
            first_seen=timeutil.ms_to_dt(int(item["first_seen"])) if item.get("first_seen") else None,
            last_seen=timeutil.ms_to_dt(int(item["last_seen"])) if item.get("last_seen") else None,
            snapshot_count=int(item.get("snapshot_count") or 0),
            cost_usd=round(float(item.get("cost_usd") or 0.0), 6),
            total_tokens=int(item.get("total_tokens") or 0),
        ).model_dump()
        for item in (row_to_dict(row) for row in rows)
    ]
    total = _catalog_total(db, "SELECT COUNT(DISTINCT agent) FROM usage_snapshots", [])
    return {"total": total, "items": items}


def catalog_projects(db: Database, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Project catalog: identity plus lifetime spend, sorted by cost descending."""
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    sql = """
        SELECT s.project AS id,
               COALESCE(p.name, s.project) AS display_name,
               MIN(s.ts) AS first_seen,
               MAX(s.ts) AS last_seen,
               COUNT(*) AS snapshot_count,
               SUM(s.cost_usd) AS cost_usd,
               (SUM(s.input_tokens) + SUM(s.output_tokens) + SUM(s.cache_read_tokens)
                + SUM(s.cache_write_tokens) + SUM(s.reasoning_tokens)) AS total_tokens
        FROM usage_snapshots s
        LEFT JOIN projects p ON p.id = s.project
        GROUP BY s.project
        ORDER BY cost_usd DESC, id ASC
        LIMIT ? OFFSET ?
    """
    rows = db.fetch_all(sql, [limit, offset])
    items = [
        ProjectInfo(
            id=str(item.get("id") or "unknown"),
            display_name=str(item.get("display_name") or item.get("id") or "unknown"),
            first_seen=timeutil.ms_to_dt(int(item["first_seen"])) if item.get("first_seen") else None,
            last_seen=timeutil.ms_to_dt(int(item["last_seen"])) if item.get("last_seen") else None,
            snapshot_count=int(item.get("snapshot_count") or 0),
            cost_usd=round(float(item.get("cost_usd") or 0.0), 6),
            total_tokens=int(item.get("total_tokens") or 0),
        ).model_dump()
        for item in (row_to_dict(row) for row in rows)
    ]
    total = _catalog_total(db, "SELECT COUNT(DISTINCT project) FROM usage_snapshots", [])
    return {"total": total, "items": items}


def catalog_sessions(
    db: Database,
    limit: int = 50,
    offset: int = 0,
    *,
    project: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Session catalog, newest activity first, with the models each session touched.

    ``project``/``agent`` filter the sessions themselves (a session belongs to the
    project/agent of its most recent snapshot).
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    clean = normalize_filters(project=project, agent=agent)
    where, params = build_filters(agent=clean.get("agent"), project=clean.get("project"), alias="s")
    sql = f"""
        SELECT s.session_id AS session_id,
               (SELECT x.project FROM usage_snapshots x
                 WHERE x.session_id = s.session_id
                 ORDER BY x.ts DESC, x.id DESC LIMIT 1) AS project,
               (SELECT x.agent FROM usage_snapshots x
                 WHERE x.session_id = s.session_id
                 ORDER BY x.ts DESC, x.id DESC LIMIT 1) AS agent,
               MIN(s.ts) AS started_at,
               MAX(s.ts) AS last_seen,
               COUNT(*) AS snapshots,
               SUM(s.cost_usd) AS cost_usd,
               (SUM(s.input_tokens) + SUM(s.output_tokens) + SUM(s.cache_read_tokens)
                + SUM(s.cache_write_tokens) + SUM(s.reasoning_tokens)) AS total_tokens,
               GROUP_CONCAT(DISTINCT s.model) AS models
        FROM usage_snapshots s{where}
        GROUP BY s.session_id
        ORDER BY last_seen DESC, session_id ASC
        LIMIT ? OFFSET ?
    """
    rows = db.fetch_all(sql, [*params, limit, offset])
    items: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row)
        models = sorted({m for m in str(item.get("models") or "").split(",") if m})
        items.append(
            SessionInfo(
                session_id=str(item.get("session_id") or "unknown"),
                project=str(item.get("project") or "unknown"),
                agent=str(item.get("agent") or "unknown"),
                started_at=timeutil.ms_to_dt(int(item["started_at"])) if item.get("started_at") else None,
                last_seen=timeutil.ms_to_dt(int(item["last_seen"])) if item.get("last_seen") else None,
                snapshots=int(item.get("snapshots") or 0),
                cost_usd=round(float(item.get("cost_usd") or 0.0), 6),
                total_tokens=int(item.get("total_tokens") or 0),
                models=models,
            ).model_dump()
        )
    total = _catalog_total(
        db, f"SELECT COUNT(DISTINCT s.session_id) FROM usage_snapshots s{where}", params
    )
    return {"total": total, "items": items}


# --------------------------------------------------------------------------------------
# CSV export rows
# --------------------------------------------------------------------------------------

#: Raw snapshot columns exported by ``dataset=snapshots`` (stable order, no JSON blobs).
_SNAPSHOT_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "event_id",
    "ts",
    "received_at",
    "agent",
    "agent_version",
    "runtime",
    "project",
    "session_id",
    "model",
    "provider",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "requests",
    "retries",
    "tool_calls",
    "latency_ms",
    "context_window",
    "tool_result_bytes",
    "cost_usd",
    "price_version",
    "tags",
    "meta",
)


def _totals_csv_values(totals: Totals) -> list[Any]:
    """Flatten a :class:`Totals` into the canonical column order."""
    return [getattr(totals, name) for name in TOTALS_FIELDS]


def csv_rows(
    db: Database,
    dataset: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 5000,
    bucket: str = "hour",
    dimension: str = "agent",
    tz_name: str = DEFAULT_TZ,
    **filters: Any,
) -> tuple[list[str], list[list[Any]]]:
    """Build ``(header, rows)`` for the CSV export endpoint.

    Snapshot-derived datasets require a window; the operational history datasets
    (alerts/anomalies/budgets) only apply a window when one is given, so "export all
    alerts" keeps working. Values are JSON friendly (ISO-8601 ``Z`` timestamps, numbers,
    strings) — the router only has to serialise the CSV.
    """
    dataset = normalize_dataset(dataset)
    clean = normalize_filters(**filters)
    limit = max(1, int(limit))

    if dataset in ("timeseries", "breakdown", "snapshots"):
        start, end = _validate_window(start_ms, end_ms)
    else:
        # Catalog/history datasets are lifetime scoped; a window is optional and only
        # narrows the history datasets (alerts/anomalies).
        if start_ms is not None and end_ms is not None:
            start, end = _validate_window(start_ms, end_ms)
        else:
            start, end = 0, 0

    if dataset == "timeseries":
        points = timeseries(
            db, bucket, start, end, tz_name=tz_name, max_points=MAX_EXPORT_ROWS, **clean
        )
        header = ["bucket_start", "bucket_end", "label", *TOTALS_FIELDS]
        rows = [
            [timeutil.iso(timeutil.dt_to_ms(point.bucket_start)),
             timeutil.iso(timeutil.dt_to_ms(point.bucket_end)),
             point.label,
             *_totals_csv_values(point.totals)]
            for point in points
        ]
        return header, rows

    if dataset == "breakdown":
        dim = normalize_dimension(dimension)
        items = breakdown(db, dim, start, end, limit=limit, **clean)
        header = ["dimension", "key", "label", "share_pct", *TOTALS_FIELDS]
        rows = [
            [dim, row.key, row.label, row.share_pct, *_totals_csv_values(row.totals)]
            for row in items
        ]
        return header, rows

    if dataset == "sessions":
        data = catalog_sessions(
            db, limit=limit, offset=0, project=clean.get("project"), agent=clean.get("agent")
        )
        header = [
            "session_id",
            "project",
            "agent",
            "started_at",
            "last_seen",
            "snapshots",
            "cost_usd",
            "total_tokens",
            "models",
        ]
        rows = [
            [
                item["session_id"],
                item["project"],
                item["agent"],
                item["started_at"],
                item["last_seen"],
                item["snapshots"],
                item["cost_usd"],
                item["total_tokens"],
                ";".join(item["models"]),
            ]
            for item in data["items"]
        ]
        return header, rows

    if dataset == "snapshots":
        where, params = _where(clean, start, end)
        columns = ", ".join(f"s.{column} AS {column}" for column in _SNAPSHOT_CSV_COLUMNS)
        sql = (
            f"SELECT {columns} FROM usage_snapshots s{where}"
            f" ORDER BY s.ts ASC, s.id ASC LIMIT ?"
        )
        rows = []
        for row in db.fetch_all(sql, [*params, limit]):
            item = row_to_dict(row)
            for column in ("ts", "received_at"):
                if item.get(column) is not None:
                    item[column] = timeutil.iso(int(item[column]))
            rows.append([item.get(column) for column in _SNAPSHOT_CSV_COLUMNS])
        return list(_SNAPSHOT_CSV_COLUMNS), rows

    if dataset == "alerts":
        clauses: list[str] = []
        params: list[Any] = []
        if start and end:
            clauses.append("created_at >= ?")
            params.append(start)
            clauses.append("created_at < ?")
            params.append(end)
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        header = [
            "id",
            "budget_id",
            "budget_name",
            "scope",
            "scope_key",
            "period",
            "window_start",
            "window_end",
            "threshold",
            "severity",
            "spent_usd",
            "limit_usd",
            "overspend_usd",
            "trigger_count",
            "message",
            "channels",
            "created_at",
        ]
        sql = (
            f"SELECT * FROM alert_events{where_sql}"
            f" ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        rows = []
        for row in db.fetch_all(sql, [*params, limit]):
            item = row_to_dict(row)
            for column in ("window_start", "window_end", "created_at"):
                if item.get(column) is not None:
                    item[column] = timeutil.iso(int(item[column]))
            rows.append([item.get(column) for column in header])
        return header, rows

    if dataset == "anomalies":
        clauses = []
        params = []
        if start and end:
            clauses.append("detected_at >= ?")
            params.append(start)
            clauses.append("detected_at < ?")
            params.append(end)
        for column in ("agent", "project", "model"):
            if clean.get(column):
                clauses.append(f"{column} = ?")
                params.append(clean[column])
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        header = [
            "id",
            "detected_at",
            "agent",
            "project",
            "model",
            "window_start",
            "window_end",
            "metric",
            "observed",
            "baseline",
            "score",
            "ratio",
            "severity",
            "reason",
            "reason_label",
            "reason_detail",
            "status",
            "cost_usd",
            "fingerprint",
        ]
        sql = (
            f"SELECT * FROM anomaly_events{where_sql}"
            f" ORDER BY detected_at DESC, id DESC LIMIT ?"
        )
        rows = []
        for row in db.fetch_all(sql, [*params, limit]):
            item = row_to_dict(row)
            for column in ("detected_at", "window_start", "window_end"):
                if item.get(column) is not None:
                    item[column] = timeutil.iso(int(item[column]))
            rows.append([item.get(column) for column in header])
        return header, rows

    # dataset == "budgets"
    header = [
        "id",
        "name",
        "scope",
        "scope_key",
        "period",
        "limit_usd",
        "thresholds",
        "webhook_url",
        "desktop",
        "enabled",
        "created_at",
        "updated_at",
    ]
    sql = "SELECT * FROM budgets ORDER BY id ASC LIMIT ?"
    rows = []
    for row in db.fetch_all(sql, [limit]):
        item = row_to_dict(row)
        for column in ("created_at", "updated_at"):
            if item.get(column) is not None:
                item[column] = timeutil.iso(int(item[column]))
        rows.append([item.get(column) for column in header])
    return header, rows


# --------------------------------------------------------------------------------------
# Convenience re-exports for the dashboard / budget engine
# --------------------------------------------------------------------------------------


def window_totals(
    db: Database, period: str, *, at_ms: int | None = None, **filters: Any
) -> Totals:
    """Totals for the *current* calendar window of a budget period (daily/weekly/monthly).

    Thin wrapper over :func:`timeutil.period_window` + :func:`totals_for_window` so the
    budget engine and the dashboard KPI cards share one definition of "today".
    """
    key = str(period or "daily").strip().lower()
    if key in ("day", "daily"):
        period_key = "daily"
    elif key in ("week", "weekly"):
        period_key = "weekly"
    elif key in ("month", "monthly"):
        period_key = "monthly"
    else:
        raise ValueError(f"unsupported period: {period!r}")
    start, end = timeutil.period_window(period_key, at_ms)
    return totals_for_window(db, start, end, **normalize_filters(**filters))


def entity_counts(db: Database, start_ms: int, end_ms: int) -> dict[str, int]:
    """Distinct active agents/projects/sessions inside a window (cheap KPI helper)."""
    start, end = _validate_window(start_ms, end_ms)
    row = db.fetch_one(
        "SELECT COUNT(DISTINCT s.agent) AS agents,"
        " COUNT(DISTINCT s.project) AS projects,"
        " COUNT(DISTINCT s.session_id) AS sessions"
        " FROM usage_snapshots s WHERE s.ts >= ? AND s.ts < ?",
        [start, end],
    )
    data = row_to_dict(row) if row is not None else {}
    return {key: int(data.get(key) or 0) for key in ("agents", "projects", "sessions")}


def iter_totals_fields() -> Iterable[str]:
    """Iterator over the canonical Totals column order (used by the CSV writer)."""
    return iter(TOTALS_FIELDS)
