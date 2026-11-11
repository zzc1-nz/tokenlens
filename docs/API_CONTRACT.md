# TokenLens API Contract (v1)

This document is the **single source of truth** for the API surface and the internal
module boundaries. When an implementation detail is ambiguous here, prefer the behaviour
described in this file and record the deviation in the relevant module docstring rather
than silently diverging.

## 0. Development environment

- Python **3.11+** (developed and tested on 3.14).
- Dependencies: FastAPI, Starlette, Pydantic v2, Uvicorn, httpx (runtime); pytest (tests).
  Install with `pip install -r requirements.txt` or `pip install -e .[dev]`.
- The package has no build step and no Node.js dependency: the web dashboard is plain
  HTML/CSS/JS served straight from `tokenlens/web/`.

```bash
python -m pytest tests -q                 # full suite
python run_server.py                      # http://127.0.0.1:8787
python -m uvicorn tokenlens.app:app --port 8787
```

Tests use the `tmp_path` fixture for isolated databases and never touch `data/`.


## 1. Product framing

TokenLens is an **observability infrastructure** for LLM token spend:

```
Agent runtimes (Claude Code, Codex, OpenAI API, Ollama, DeepSeek)
        │  POST /api/v1/ingest/snapshots   (batch, idempotent)
        ▼
   ingest → price → store (raw) → rollup (hourly) → SQLite (WAL)
        ▼
   query API  →  usage / trends / breakdown / export
   budget engine  →  daily/weekly/monthly thresholds → webhook + desktop notify
   anomaly engine →  burn-rate z-score → classified root cause
        ▼
   HUD client (rich CLI, live bars, project switching) + web dashboard
```

## 2. Module map

Each layer owns one module plus its router(s); routers only parse/validate and delegate,
so no HTTP concern leaks into the domain logic.

| Layer | Modules |
|---|---|
| **Foundation** | `tokenlens/config.py`, `timeutil.py`, `pricing.py`, `schemas.py`, `db.py`, `store.py`, `app.py`, `api/ingest.py`, `api/health.py` |
| **Query / aggregation** | `tokenlens/query.py`, `api/usage.py`, `api/trends.py`, `api/export.py`, `api/catalog.py` |
| **Budgets & anomalies** | `tokenlens/budget.py`, `notify.py`, `anomaly.py`, `scheduler.py`, `api/budgets.py`, `api/alerts.py`, `api/anomalies.py`, `api/dashboard.py` |
| **HUD client & tooling** | `tokenlens_cli/`, `tools/simulate.py`, `tools/capture_evidence.py` |
| **Web dashboard** | `tokenlens/web/` |

Foundation modules every other layer builds on:

- `tokenlens.db` — `Database`, `get_db` dependency, schema, `build_filters()` SQL builder
- `tokenlens.schemas` — every Pydantic request/response model
- `tokenlens.pricing` — price catalog, `resolve_model()`, `compute_cost()`
- `tokenlens.timeutil` — `now_ms()`, `parse_time_ms()`, `period_window()`, `bucket_start()`
- `tokenlens.config` — `get_settings()` (env-driven)
- `tokenlens.store` — `ingest_snapshots()`, `IngestResult`
- `tokenlens.app` — `create_app()`, `app` (wires every router in §12)

## 3. Conventions

- All timestamps in API JSON are **ISO-8601 UTC with `Z`** (e.g. `2026-11-10T13:04:05.123Z`).
- All internal timestamps are **integer epoch milliseconds (UTC)**.
- Money is `float` USD, rounded to **6** decimals. Tokens are `int`.
- Token semantics:
  - `input_tokens` — *uncached* prompt tokens (full price)
  - `cache_read_tokens` — prompt tokens served from cache (cache-read price)
  - `cache_write_tokens` — prompt tokens written to the cache (cache-write price)
  - `output_tokens` — completion tokens (output price)
  - `reasoning_tokens` — billed at the **output** price; kept separately for analysis
  - `total_tokens = input + cache_read + cache_write + output + reasoning`
  - `billable_tokens = input + output + reasoning` (excludes cache traffic)
- Errors: `{"detail": "..."}` with 4xx/5xx (FastAPI default). Validation → 422.
- Auth: when env `TOKENLENS_API_TOKEN` is set, every `/api/v1/**` request must send
  `X-API-Key: <token>` (except `/health`). The dependency lives in `app.py`; routers
  inherit it and need no per-route code.
- Pagination: `limit` (default 50, max 1000) + `offset` (default 0).


## 4. Shared response models (`tokenlens.schemas`)

```python
class Totals(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    total_tokens: int
    billable_tokens: int
    requests: int
    retries: int
    tool_calls: int
    cost_usd: float
    sessions: int
    agents: int
    projects: int
    cache_hit_rate: float          # cache_read / (cache_read + input)  ∈ [0,1]
    avg_cost_per_request: float
    avg_tokens_per_request: float

class TimePoint(BaseModel):
    bucket_start: datetime          # UTC
    bucket_end: datetime
    label: str                      # e.g. "2026-11-10 14:00" (server local tz, UTC+8)
    totals: Totals

class BreakdownRow(BaseModel):
    key: str                        # dimension value
    label: str                      # human friendly
    totals: Totals
    share_pct: float                # share of cost in the queried window

class BudgetStatus(BaseModel):
    budget: BudgetOut
    window_start: datetime
    window_end: datetime
    spent_usd: float
    limit_usd: float
    pct: float
    remaining_usd: float
    projected_usd: float            # linear projection to end of window
    projected_pct: float
    status: str                     # "ok" | "warning" | "critical" | "exceeded"
    triggered_thresholds: list[float]
    trigger_count: int
    overspend_usd: float
    last_alert_at: datetime | None

class AlertRecord(BaseModel):
    id: int
    budget_id: int | None
    budget_name: str
    scope: str                      # global|project|agent|model
    scope_key: str
    period: str                     # daily|weekly|monthly
    window_start: datetime
    window_end: datetime
    threshold: float
    severity: str                   # warning|critical|exceeded
    spent_usd: float
    limit_usd: float
    overspend_usd: float
    trigger_count: int
    message: str
    channels: list[str]
    created_at: datetime

class AnomalyRecord(BaseModel):
    id: int
    detected_at: datetime
    agent: str
    project: str
    model: str
    window_start: datetime
    window_end: datetime
    metric: str                     # tokens_per_min
    observed: float
    baseline: float
    score: float                    # robust z-score
    ratio: float                    # observed / baseline
    severity: str                   # low|medium|high|critical
    reason: str                     # retry_storm|context_bloat|uncompressed_tool_output|traffic_spike|cache_miss_storm|unknown
    reason_label: str               # Chinese label, e.g. "重试风暴"
    reason_detail: str
    evidence: dict
    tokens_burn: float
    cost_usd: float
    status: str                     # open|acknowledged|resolved
    fingerprint: str
```

`tokenlens.schemas` also exposes `Totals.zero()` and
`Totals.from_row(row)` helpers, plus `tokens_to_totals(rows)` aggregation used by the
query layer — reuse them, do not re-derive the formulas.

## 5. Ingest (write path)

### `POST /api/v1/ingest/snapshots`
Body (batch, 1..5000 items):
```json
{"snapshots": [SnapshotIn, ...]}
```
`SnapshotIn` fields: `event_id?`, `ts?` (ISO or epoch ms, default now),
`agent`, `agent_version?`, `runtime?` (`openai|anthropic|ollama|deepseek|google|xai|local`),
`project`, `session_id`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, `requests`, `retries`, `tool_calls`,
`latency_ms?`, `context_window?`, `tool_result_bytes?`, `tags?`, `meta?`.

Response `IngestResponse`:
```json
{"accepted": 1200, "duplicates": 0, "rejected": 0, "errors": [],
 "total_tokens": 8123456, "cost_usd": 12.345678, "ingest_ms": 42.7,
 "price_version": "2025-11-01", "unknown_models": ["foo"]}
```
Idempotency: `event_id` unique — replayed batches count as `duplicates`, never double count.

### `POST /api/v1/ingest/snapshot` — single snapshot (same schema), returns `IngestResponse`.

## 6. Query API (read path)

All filters are optional and combine with AND:
`agent`, `project`, `model`, `runtime`, `session_id`, `from`, `to` (ISO or epoch ms).

### `GET /api/v1/usage/summary` → `{"window": {"from","to"}, "totals": Totals, "filters": {...}}`
### `GET /api/v1/usage/breakdown?dimension=agent|project|model|runtime|session&limit=50`
→ `{"dimension": "...", "window": {...}, "rows": [BreakdownRow, ...], "total": Totals}`
Sorted by `cost_usd` desc. `share_pct` = row cost / total cost * 100 (0 when total = 0).
### `GET /api/v1/usage/timeseries?bucket=minute|hour|day|week|month&from=&to=`
→ `{"bucket": "...", "window": {...}, "points": [TimePoint, ...]}` (gap-filled, ascending).
### `GET /api/v1/usage/live?window_minutes=5`
→ `{"window_minutes": 5, "tokens_per_min": float, "cost_per_hour": float,
     "active_agents": int, "active_projects": int, "active_sessions": int,
     "top_projects": [{"project","cost_usd","tokens_per_min"}], "top_agents": [...]}`
Computed from raw snapshots inside the trailing window.
### `GET /api/v1/trends?period=day|week|month&limit=30&...filters`
→ `{"period","from","to","points":[TimePoint],"comparison":{"previous_cost_usd","current_cost_usd",
   "delta_usd","delta_pct","previous_tokens","current_tokens","token_delta_pct"},"filters":{}}`
`limit` = number of buckets ending at the current bucket (inclusive).
### `GET /api/v1/catalog/models` → `{"price_version","models":[{model, provider, display_name, input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok, context_window, known}]}`
### `GET /api/v1/catalog/agents?limit=&offset=` → `{"total", "items":[{id, display_name, runtime, first_seen, last_seen, snapshot_count, cost_usd, total_tokens}]}`
### `GET /api/v1/catalog/projects?...` → same shape
### `GET /api/v1/catalog/sessions?limit=&offset=&project=&agent=` → `{"total","items":[{session_id, project, agent, started_at, last_seen, snapshots, cost_usd, total_tokens, models:[...]}]}`

### CSV export
`GET /api/v1/export/csv?dataset=timeseries|breakdown|sessions|snapshots|alerts|anomalies|budgets&...filters&limit=`
- Response: `text/csv; charset=utf-8`, header `Content-Disposition: attachment; filename="tokenlens-<dataset>-<utcstamp>.csv"`.
- `dataset=snapshots` exports raw rows (capped by `limit`, default 5000, max 100000).
- UTF-8 **with BOM** so Excel opens Chinese headers correctly. Headers in English snake_case.

## 7. Budget engine

### `GET /api/v1/budgets` → `{"items": [BudgetStatus, ...]}` (all enabled+disabled budgets, evaluated)
### `POST /api/v1/budgets` body `BudgetIn`:
```json
{"name":"team-daily","scope":"global","scope_key":"*","period":"daily",
 "limit_usd":25.0,"thresholds":[0.5,0.8,1.0],"webhook_url":null,
 "desktop":true,"enabled":true}
```
`scope` ∈ `global|project|agent|model`; for non-global scopes `scope_key` is required.
Thresholds must be ascending, `> 0`, `<= 5`, max 6 entries. Default `[0.5,0.8,1.0]`.
`desktop` toggles local desktop notification (implemented as a notify sink that appends
to `data/notifications.jsonl` and, on Windows, tries PowerShell toast — never raises).
`webhook_url` is POSTed a JSON payload (§7.4) with 3s timeout, 2 retries, never blocks the API.

### `PATCH /api/v1/budgets/{id}` (partial), `DELETE /api/v1/budgets/{id}` → 204
### `GET /api/v1/budgets/{id}` → BudgetStatus
### `POST /api/v1/budgets/evaluate?dry_run=false`
Runs one evaluation pass for all enabled budgets in the **current** window.
→ `{"evaluated": 6, "triggered": [AlertRecord, ...], "evaluated_at": "...", "dry_run": false}`
Rules:
- spend = cost_usd of snapshots in the current period window, filtered by scope.
- For each threshold `t` with `pct >= t`: emit an alert **at most once per
  (budget_id, window_start, threshold)** — the `alert_events` row is then **updated**
  (`trigger_count += 1`, `spent_usd`/`overspend_usd` refreshed) on later passes so the
  "告警触发次数" metric reflects repeated breaches without spamming notifications.
- `severity`: `t < 0.8` → `warning`, `0.8 <= t < 1.0` → `critical`, `t >= 1.0` → `exceeded`.
- `overspend_usd = max(0, spent - limit)` (recorded on the exceeded row and mirrored to
  every row of that window).
- Notifications are sent only when `trigger_count` increases **and** not `dry_run`.
### `GET /api/v1/alerts?limit=50&offset=0&severity=&budget_id=&since=`
→ `{"total", "items":[AlertRecord]}` newest first.
### `GET /api/v1/alerts/summary` → 
`{"total_alerts": int, "total_triggers": int, "by_severity": {"warning":n,...},
  "by_budget": [{"budget_id","budget_name","alerts","triggers","overspend_usd"}],
  "total_overspend_usd": float, "window": {"from","to"}, "last_triggered_at": "..."}`

### `tokenlens/notify.py`
```python
@dataclass
class Notification: channel: str; title: str; body: str; severity: str; payload: dict
def send_webhook(url: str, payload: dict) -> tuple[bool, str]   # (ok, detail)
def send_desktop(n: Notification) -> tuple[bool, str]           # append jsonl + toast attempt
def dispatch(settings, channels: list[str], n: Notification) -> list[dict]
```
### `tokenlens/scheduler.py`
```python
def start(app) -> None      # asyncio task: every settings.budget_interval_seconds
def stop(app) -> None       # cancel task, swallow CancelledError
```
It calls the budget evaluation + anomaly detection passes; failures are logged, never fatal.

## 8. Anomaly engine

### `POST /api/v1/anomalies/detect?lookback_minutes=60`
Runs detection over the trailing window and persists new events.
→ `{"scanned_agents": n, "detected": [AnomalyRecord, ...], "detected_at": "...", "lookback_minutes": 60}`
### `GET /api/v1/anomalies?limit=50&offset=0&status=&severity=&agent=&project=`
→ `{"total","items":[AnomalyRecord]}`
### `GET /api/v1/anomalies/summary` →
`{"total_events": int, "open_events": int, "by_reason": {"retry_storm":n,...},
  "by_severity": {...}, "window":{"from","to"}, "last_detected_at": "...",
  "estimated_waste_usd": float}`
### `GET /api/v1/anomalies/{id}` → AnomalyRecord (404 if missing)
### `PATCH /api/v1/anomalies/{id}` body `{"status":"open|acknowledged|resolved"}` → AnomalyRecord

Detection algorithm (documented in the module docstring, must be deterministic):
1. Build per-agent per-minute token series from `usage_snapshots` in
   `[now - lookback, now]`; metric = `total_tokens / minute`.
2. Baseline = median of the per-minute series over `[now - lookback*6, now - lookback)`
   (fall back to the same window's first 20 minutes when there is no history), with
   MAD-based robust sigma (`1.4826 * median(|x - median|)`), floor of 200 tokens/min.
3. `score = (observed_p95 - baseline) / sigma`; flag when `score >= 4.0` **and**
   `observed >= 3 * baseline` **and** `observed >= 2000 tokens/min`.
4. Classify root cause using the raw snapshot evidence of the offending window:
   - `retry_storm` — `retries / requests >= 0.15` **or** mean retries per request >= 2
   - `context_bloat` — mean `context_window` >= 120k **or** avg input tokens per request
     >= 3x the agent's 24h baseline
   - `uncompressed_tool_output` — `tool_result_bytes / requests >= 200_000`
   - `cache_miss_storm` — `cache_hit_rate` dropped >= 40% vs agent baseline
   - `traffic_spike` — snapshot count / request count grew >= 2.5x while per-request
     tokens stayed flat
   - `unknown` — none of the above
5. `severity`: `score>=10` or `ratio>=8` → `critical`; `score>=7` → `high`; `score>=5` → `medium`; else `low`.
6. `fingerprint = sha1(f"{agent}|{reason}|{window_start//300000}")` → dedupe: one event
   per agent per reason per 5-minute bucket (`INSERT OR IGNORE`).
7. `estimated_waste_usd` in summary = sum of `cost_usd` of `open`+`acknowledged` events.

## 9. Dashboard aggregation

### `GET /api/v1/dashboard/overview?days=7`
```json
{"generated_at":"...","kpis":{"today_cost_usd":0,"today_tokens":0,"week_cost_usd":0,
 "month_cost_usd":0,"burn_rate_tokens_per_min":0,"burn_rate_usd_per_hour":0,
 "cache_hit_rate":0,"active_agents":0,"active_projects":0,"total_sessions":0},
 "breakdowns":{"by_agent":[BreakdownRow],"by_project":[...],"by_model":[...],"by_runtime":[...]},
 "timeseries":{"hourly":[TimePoint],"daily":[TimePoint]},
 "budgets":[BudgetStatus],"alerts":{"recent":[AlertRecord],"summary":{...}},
 "anomalies":{"recent":[AnomalyRecord],"summary":{...}}}
```
Must degrade gracefully (empty lists / zeros) when the database is empty. Reuse the
`tokenlens.query` helpers (`totals_for_window`, `breakdown`, `timeseries`, `live_metrics`)
so the dashboard and the query endpoints can never disagree.

## 10. HUD client

Python package `tokenlens_cli` (console script name `tokenlens`).

```
tokenlens hud      [--server URL] [--project NAME] [--interval 1.0] [--no-color]
tokenlens live     [--server URL] [--window 5]
tokenlens summary  [--server URL] [--from ...] [--to ...] [--agent ...]
tokenlens trends   [--server URL] [--period day|week] [--limit 30]
tokenlens breakdown --dimension agent|project|model|session
tokenlens budgets / alerts / anomalies
tokenlens export --dataset timeseries|breakdown|sessions --out FILE
tokenlens detect [--lookback 60] [--evaluate-budgets]
tokenlens simulate --help        (delegates to tools/simulate.py)
tokenlens version
```
`hud` = the ai-token-hud-style live view:
- top: today cost, week cost, burn rate (tokens/min), cache hit rate, budget bars
- middle: horizontal bars (`█` + `░`) for the **current dimension**
- keys: `p` cycle project → `a` agent → `m` model → `b` budget → `q` quit
  (non-blocking key read via `msvcrt.getwch` on Windows / `termios` fallback)
- refresh every `--interval` seconds; must survive server being down (show `OFFLINE` banner)
- `--no-color` and non-TTY mode must print one plain snapshot and exit (CI friendly)

`tools/simulate.py` — load generator for testing the platform itself:
```
python tools/simulate.py --server URL --mode burst|steady|incident|mixed \
  --duration 60 --agents 6 --projects 4 --sessions 40 --target-tokens 1000000 \
  --concurrency 8 --seed 42 [--inject anomalies] [--report]
```
- Writes through the public ingest API (HTTP), batching up to 500 snapshots per request.
- Realistic model mix and cache ratios; `--target-tokens` stops when reached.
- `--mode incident` injects the three anomaly classes so the detector has something to find:
  retry storm (high retries), context bloat (huge `context_window`), uncompressed tool
  output (huge `tool_result_bytes`).
- `--report` prints ingest throughput (snapshots/s, tokens/s), accepted/duplicate counts.
- Must also support `--dry-run` (generate without HTTP) so it is unit-testable.

## 11. Web dashboard

`tokenlens/web/` — static, **no build step, no CDN** (offline-safe):
- `index.html`, `styles.css`, `app.js` (+ optional `charts.js`)
- Served by FastAPI: `GET /` → `index.html`, `GET /static/*` → files.
- Dark "observability console" look; single page with tabs:
  Overview (KPI cards, live burn sparkline, budget bars) · Projects/Agents/Models
  (sortable breakdown tables) · Trends (SVG line/area chart, period switch,
  model/agent filter, CSV export button) · Budgets & Alerts (create budget form,
  alert table with trigger counts + overspend) · Anomalies (cards with reason labels
  and evidence) · Simulator hint panel.
- Charts: hand-written SVG (no libs). Poll `/api/v1/dashboard/overview` every 3s.
- Chinese UI labels; numbers formatted with `Intl.NumberFormat`.

## 12. App wiring

`tokenlens/app.py` exposes `create_app(settings=None) -> FastAPI` and module-level `app`.
It includes exactly these routers (already stubbed, keep the prefix):

| module | prefix | tags |
|---|---|---|
| `tokenlens.api.ingest` | `/api/v1/ingest` | `ingest` |
| `tokenlens.api.usage` | `/api/v1/usage` | `usage` |
| `tokenlens.api.trends` | `/api/v1/trends` | `trends` |
| `tokenlens.api.export` | `/api/v1/export` | `export` |
| `tokenlens.api.catalog` | `/api/v1/catalog` | `catalog` |
| `tokenlens.api.budgets` | `/api/v1/budgets` | `budgets` |
| `tokenlens.api.alerts` | `/api/v1/alerts` | `alerts` |
| `tokenlens.api.anomalies` | `/api/v1/anomalies` | `anomalies` |
| `tokenlens.api.dashboard` | `/api/v1/dashboard` | `dashboard` |
| `tokenlens.api.health` | `""` (`/health`, `/api/v1/meta`) | `health` |

## 13. Database schema (`tokenlens/db.py`)

```sql
usage_snapshots(id INTEGER PK AUTOINCREMENT, event_id TEXT UNIQUE, ts INTEGER NOT NULL,
  received_at INTEGER NOT NULL, agent TEXT NOT NULL, agent_version TEXT,
  runtime TEXT NOT NULL, project TEXT NOT NULL, session_id TEXT NOT NULL,
  model TEXT NOT NULL, provider TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 1, retries INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER, context_window INTEGER,
  tool_result_bytes INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  price_version TEXT, tags TEXT, meta TEXT)

usage_rollup_hourly(bucket_ms INTEGER NOT NULL, agent TEXT, project TEXT, model TEXT,
  runtime TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, reasoning_tokens INTEGER, requests INTEGER, retries INTEGER,
  tool_calls INTEGER, cost_usd REAL, PRIMARY KEY(bucket_ms, agent, project, model))

agents(id TEXT PK, display_name TEXT, runtime TEXT, first_seen INTEGER, last_seen INTEGER,
  snapshot_count INTEGER DEFAULT 0, meta TEXT)
projects(id TEXT PK, name TEXT, first_seen INTEGER, last_seen INTEGER,
  snapshot_count INTEGER DEFAULT 0, meta TEXT)

budgets(id INTEGER PK AUTOINCREMENT, name TEXT NOT NULL, scope TEXT NOT NULL,
  scope_key TEXT NOT NULL DEFAULT '*', period TEXT NOT NULL, limit_usd REAL NOT NULL,
  thresholds TEXT NOT NULL, webhook_url TEXT, desktop INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)

alert_events(id INTEGER PK AUTOINCREMENT, budget_id INTEGER, budget_name TEXT,
  scope TEXT, scope_key TEXT, period TEXT, window_start INTEGER, window_end INTEGER,
  threshold REAL, severity TEXT, spent_usd REAL, limit_usd REAL, overspend_usd REAL,
  trigger_count INTEGER NOT NULL DEFAULT 1, message TEXT, channels TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE(budget_id, window_start, threshold))

anomaly_events(id INTEGER PK AUTOINCREMENT, detected_at INTEGER NOT NULL, agent TEXT,
  project TEXT, model TEXT, window_start INTEGER, window_end INTEGER, metric TEXT,
  observed REAL, baseline REAL, score REAL, ratio REAL, severity TEXT, reason TEXT,
  reason_label TEXT, reason_detail TEXT, evidence TEXT, tokens_burn REAL, cost_usd REAL,
  status TEXT NOT NULL DEFAULT 'open', fingerprint TEXT UNIQUE)

budget_state(budget_id INTEGER, window_start INTEGER, trigger_count INTEGER DEFAULT 0,
  max_threshold REAL DEFAULT 0, last_notified_at INTEGER, overspend_usd REAL DEFAULT 0,
  updated_at INTEGER, PRIMARY KEY(budget_id, window_start))
```

Indexes exist on `usage_snapshots(ts)`, `(agent, ts)`, `(project, ts)`, `(model, ts)`,
`(session_id, ts)`, `(runtime, ts)` and `usage_rollup_hourly(bucket_ms)`.

## 14. Definition of done

1. `python -m pytest tests -q` passes (the suite is self-contained: no network, no wall-clock dependence).
2. Every endpoint returns the documented JSON shape, asserted in tests.
3. Empty-database behaviour is correct (zeros / empty lists, HTTP 200).
4. Module docstrings explain the *why* behind non-obvious decisions; type hints throughout.
5. Tests are hermetic: isolated database per test, frozen clock, no live server required.
