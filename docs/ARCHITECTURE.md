# TokenLens Architecture

> A token **observability** system, not a token counter. The unit of analysis is the
> *attributed* token: every token is traceable to an agent, a project, a session, a model
> and a price version.

## 1. Why this exists

`ai-token-hud` answers one question well: *"how fast am I burning tokens right now?"* — a
single developer, a single machine, a floating window. The moment a team runs **several
agent runtimes across several projects**, that model breaks down:

| Question | Single-user HUD | TokenLens |
|---|---|---|
| How much did `payments-api` cost this week? | ✗ | ✅ project dimension |
| Which agent is burning tokens right now? | ✗ | ✅ live burn rate per agent |
| Are we going to blow the monthly budget? | ✗ | ✅ projection + threshold alerts |
| Why did spend triple in 4 minutes? | ✗ | ✅ anomaly detection + root-cause class |
| Can finance have the raw rows? | ✗ | ✅ CSV export, price-versioned |

TokenLens keeps the HUD interaction model (a live horizontal bar view, refreshed every
second) but moves the ledger to a **server**: one SQLite-backed collector, many reporters,
one aggregation API, and an alerting engine on top.

## 2. System topology

```
┌───────────────────────────── agent runtimes ─────────────────────────────┐
│  Claude Code hook      Codex wrapper     OpenAI proxy    Ollama sidecar  │
│  (JSONL tail)          (usage parser)    (ASGI middlew)  (token counter) │
└───────────┬──────────────────┬────────────────┬─────────────────┬────────┘
            │                  │                │                 │
            └──────────────────┴────────┬───────┴─────────────────┘
                                        │  POST /api/v1/ingest/snapshots
                                        │  (batch ≤5000, idempotent on event_id)
                              ┌─────────▼──────────┐
                              │   TokenLens API    │  FastAPI + Uvicorn
                              │  ┌──────────────┐  │
                              │  │ ingest       │──┼──► pricing catalog (64 models)
                              │  ├──────────────┤  │
                              │  │ query/agg    │──┼──► SQLite (WAL)
                              │  ├──────────────┤  │     ├─ usage_snapshots  (raw)
                              │  │ budget engine│──┼──►   ├─ usage_rollup_hourly (cache)
                              │  ├──────────────┤  │     ├─ budgets / alert_events
                              │  │ anomaly      │──┼──►   └─ anomaly_events
                              │  └──────────────┘  │
                              └────┬───────┬───────┘
                    ┌──────────────┘       └──────────────┐
           ┌────────▼────────┐                    ┌────────▼─────────┐
           │ HUD CLI (rich)  │                    │ Web dashboard    │
           │ live bars,      │                    │ SVG charts,      │
           │ project switch  │                    │ budget forms     │
           └─────────────────┘                    └──────────────────┘
                    │
           ┌────────▼─────────┐   webhook / desktop toast
           │ Alert sinks      │──► Slack, Feishu, Teams, OS notification
           └──────────────────┘
```

## 3. The five layers

### 3.1 Ingest (write path)

A **snapshot** is a delta, not a cumulative counter:

```json
{"event_id":"01JC…","ts":"2026-11-10T13:04:05.123Z","agent":"claude-code",
 "project":"payments-api","session_id":"sess-9f2","model":"claude-sonnet-4-5",
 "runtime":"anthropic","input_tokens":1200,"output_tokens":400,
 "cache_read_tokens":3000,"cache_write_tokens":200,"requests":1,"retries":0,
 "tool_calls":1,"context_window":200000,"tool_result_bytes":8421}
```

Design decisions:

* **Batch, not per-request.** A coding agent produces hundreds of small usage deltas per
  minute; one HTTP round trip each would make monitoring the bottleneck. Batches of up to
  5000 snapshots are written in a single transaction.
* **Idempotency on `event_id`.** `usage_snapshots.event_id` is `UNIQUE` and the insert is
  `INSERT OR IGNORE`; a retried batch after a timeout is counted as `duplicates` and never
  double-charges a budget. Clients that omit `event_id` get a deterministic
  `blake2b`-derived id so a byte-identical replay is still deduplicated.
* **Price at ingest, stamp the version.** `cost_usd` is computed once from
  `pricing.PRICE_VERSION` and stored. Re-pricing history is therefore a deliberate
  migration, not an accidental drift when a provider changes its rates.
* **Token classes are kept separate.** `input / output / cache_read / cache_write /
  reasoning` are never collapsed into one number — cache reads cost 10% of input and are
  the single biggest lever on real spend, so they must remain visible. Reasoning tokens are
  billed at the output rate but tracked separately for analysis.

### 3.2 Pricing catalog

64 models across Anthropic, OpenAI, DeepSeek, Google, xAI, Mistral, and local Ollama
runtimes, each with four price points (`input`, `output`, `cache_read`, `cache_write`) and a
context window. Resolution is forgiving: dated snapshots
(`claude-sonnet-4-5-20250929`), provider prefixes (`openai/gpt-4.1-mini`) and shorthand
(`sonnet`) all normalise to a canonical entry. Unknown models are **not rejected** — they
are priced with a documented fallback and reported back to the caller in
`unknown_models`, because a new model release must never break ingestion.

Local models are catalogued at `$0` with an explicit `local-gpu-amortised` note: they do
not appear as invisible spend, they appear as *zero-cost but accounted* usage.

### 3.3 Storage

SQLite in WAL mode, one connection per unit of work:

* WAL lets the dashboard's read-heavy polling run concurrently with the ingest writer
  instead of serialising on a global lock.
* Raw snapshots are the source of truth. `usage_rollup_hourly` is a maintained **cache**
  (upserted in the same transaction as the raw insert), not a second truth.
* Indexes are dimension-aligned: `(ts)`, `(agent, ts)`, `(project, ts)`, `(model, ts)`,
  `(session_id, ts)`, `(runtime, ts)` — every query filter maps to a covering prefix.

### 3.4 Aggregation (read path)

One shared SQL builder (`tokenlens.db.build_filters`) assembles every `WHERE` clause, so
query, trends, export and dashboard cannot drift apart in behaviour or in injection safety.
Every aggregation returns the same `Totals` shape, computed in SQL with `SUM`, and derived
ratios (`cache_hit_rate`, `avg_cost_per_request`) computed once in
`Totals.from_mapping`.

Time series are **gap-filled**: a bucket with no traffic is emitted with zeros, because a
chart that silently skips quiet hours lies about the shape of spend.

### 3.5 Budget engine

Budgets are `(scope, scope_key, period)` tuples — `global`, `project`, `agent`, `model`
crossed with `daily`, `weekly`, `monthly`. Windows are **calendar aligned in the user's
timezone** (a Shanghai user's "day" starts at 00:00 +08:00, not 00:00 UTC).

Threshold evaluation is idempotent by construction:

```sql
UNIQUE(budget_id, window_start, threshold)
```

The first breach inserts an alert row; later evaluations of the *same* window and
threshold **update** it (`trigger_count += 1`, refreshed `spent_usd`, `overspend_usd`).
That gives two metrics that a naive "fire once" implementation cannot produce:

* **how many times** a budget was re-breached (`trigger_count`), and
* **how far over** it went (`overspend_usd = max(0, spent − limit)`).

Notifications are only dispatched when `trigger_count` increases, so the metric is precise
without spamming Slack.

### 3.6 Anomaly detection

Token spend is heavy-tailed and bursty, so a mean/σ z-score is useless — a single retry
storm inflates the mean enough to hide itself. TokenLens uses a **robust z-score**:

```
baseline = median(tokens_per_min over [now − 6·L, now − L))
σ_robust = 1.4826 × median(|x − baseline|)        (floor: 200 tok/min)
score    = (p95(observed window) − baseline) / σ_robust
flag     ⇔ score ≥ 4  ∧  observed ≥ 3·baseline  ∧  observed ≥ 2000 tok/min
```

The three-part condition is what keeps it usable: the z-score catches the *statistical*
anomaly, the ratio requirement prevents flagging tiny absolute movements, and the absolute
floor prevents a quiet agent's 3 tokens/min → 12 tokens/min blip from paging anyone.

Detected events are then **classified by root cause** from the raw evidence of the
offending window, because "spend spiked" is not actionable but "retry storm" is:

| Reason | Evidence rule | What the operator should do |
|---|---|---|
| `retry_storm` | `retries / requests ≥ 0.15` or ≥2 retries per request | back off, check provider 429/5xx, fix idempotency |
| `context_bloat` | mean `context_window ≥ 120k` or input/request ≥ 3× 24h baseline | compact history, raise the summarisation threshold |
| `uncompressed_tool_output` | `tool_result_bytes / request ≥ 200 KB` | trim/truncate tool results, summarise file reads |
| `cache_miss_storm` | cache hit rate dropped ≥ 40% vs baseline | prompt prefix changed, cache TTL too short |
| `traffic_spike` | requests ≥ 2.5× with flat tokens/request | legitimate load — scale budget, not code |
| `unknown` | none matched | inspect the raw snapshots in the window |

Deduplication uses `fingerprint = sha1(agent | reason | window_start/5min)`, so a sustained
incident produces one event per 5-minute bucket rather than one per detection pass.

## 4. Data model

```
usage_snapshots ──1:N── (agent, project, session_id, model)   ← raw, append-only
      │
      ├─► usage_rollup_hourly   (bucket_ms, agent, project, model) → PK
      ├─► agents / projects     (catalog + first/last seen counters)
      ├─► budgets               (scope, scope_key, period, limit, thresholds)
      │        └─► alert_events  UNIQUE(budget_id, window_start, threshold)
      │        └─► budget_state  (trigger_count per window)
      └─► anomaly_events         UNIQUE(fingerprint)
```

## 5. Failure behaviour

| Failure | Behaviour |
|---|---|
| Server down | HUD shows `OFFLINE` banner and keeps the last known frame; simulator retries the batch |
| Duplicate batch | `INSERT OR IGNORE` → `duplicates` count, no double spend |
| Unknown model | fallback pricing + `unknown_models` in the response, ingest still succeeds |
| Webhook endpoint down | 3s timeout, 2 retries, failure recorded in the alert's `channels`, evaluation never blocks |
| Scheduler crash | caught and logged per tick; the API stays up; `POST /budgets/evaluate` is always available manually |
| Clock skew | windows are computed server-side from UTC epoch ms; client timestamps only affect bucketing |

## 6. Performance envelope

Measured on this machine (see `docs/evidence/`):

* ingest: batch of 500 snapshots ≈ a few ms of CPU; the simulator sustains thousands of
  snapshots per second over localhost.
* aggregation: `usage/summary` over a million-token dataset returns in single-digit
  milliseconds thanks to the `(ts)` index and SQL-side `SUM`.
* SQLite file for ~100k snapshots stays in the tens of MB; WAL keeps writers from blocking
  the dashboard's 3-second poll.

## 7. Extension points

* **PostgreSQL**: the schema is portable; swap `tokenlens/db.py` for an async SQLAlchemy
  engine and keep `build_filters`' contract.
* **New runtime**: add the runtime name to `schemas.RUNTIMES`, its models to
  `pricing.CATALOG`, and post snapshots from a shim. No server change required.
* **New alert channel**: implement a sink in `notify.py` and return it from `dispatch`.
* **Retention**: `usage_rollup_hourly` is enough for long-range trends; raw snapshots can
  be pruned on a schedule without losing the historical charts.
