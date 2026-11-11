# TokenLens evidence

Generated at: 2026-11-11T11:15:14.500Z

## 1. Ingest throughput

| metric | value |
|---|---|
| target tokens | 1,000,000 |
| ingested tokens | 1,031,162 |
| snapshots accepted | 80 |
| batches | 2 |
| elapsed | 0.027 s |
| client throughput | 2,952.3 snapshots/s · 38,054,330.5 tokens/s |
| server ingest time | 12.725 ms total · 6,286.8 snapshots/s (price+persist+rollup only) |

### Sustained ingest benchmark (fixed 10 x 100-snapshot batches)

| metric | value |
|---|---|
| snapshots | 1,000 |
| tokens | 12,597,662 |
| server-side rate | 8,775.7 snapshots/s · 110,553,325.6 tokens/s |
| batch latency | p50 16.59 ms · p95 20.39 ms · max 20.39 ms |
| simulated cost | $1.9955 |
| catalog | 64 models @ 2025-11-01 |

Idempotency replay: {"first": {"accepted": 50, "duplicates": 0}, "replay": {"accepted": 0, "duplicates": 50}, "no_double_count": true}

## 2. Budget alerts

| budget | scope | limit | spent | % | trigger_count | overspend | status |
|---|---|---|---|---|---|---|---|
| 全局日预算 | global:* | $2.3325 | $3.3822 | 145.0% | 2 | $1.0497 | exceeded |
| payments-api 日预算 | project:payments-api | $0.7754 | $0.6979 | 90.0% | 2 | $0.0000 | critical |
| claude-code 日预算 | agent:claude-code | $1.0619 | $0.5840 | 55.0% | 2 | $0.0000 | warning |

Total alerts: **6**, total triggers: **12**, total overspend: **$1.0497**

## 3. Anomaly detection

Incident snapshots injected: 120, baseline snapshots: 540, detected events: 3

| agent | reason | severity | observed tok/min | baseline tok/min | score | ratio |
|---|---|---|---|---|---|---|
| deepseek-agent | 工具返回数据未压缩 (`uncompressed_tool_output`) | critical | 38,611 | 2,600 | 180.06 | 14.85x |
| codex-cli | 上下文膨胀 (`context_bloat`) | critical | 44,532 | 2,050 | 212.41 | 21.72x |
| claude-code | 重试风暴 (`retry_storm`) | critical | 31,346 | 2,780 | 142.83 | 11.28x |

## 4. Dashboard KPIs

| kpi | value |
|---|---|
| today cost | $18.1505 |
| week cost | $18.1505 |
| month cost | $18.1505 |
| today tokens | 13,616,983 |
| burn rate | 2,211,821 tokens/min |
| cache hit rate | 11.7% |
| active agents | 5 |
| active projects | 5 |

## 5. Exports

* `alerts` → `docs\evidence\alerts.csv` (1,829 bytes)
* `anomalies` → `docs\evidence\anomalies.csv` (1,306 bytes)
* `timeseries` → `docs\evidence\timeseries.csv` (3,235 bytes)
* `breakdown` → `docs\evidence\breakdown.csv` (962 bytes)
