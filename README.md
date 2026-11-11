# TokenLens

**多模型 / 多 Agent 的 LLM Token 成本可观测性平台（Observability Infrastructure）**

> 从「消耗 token」到「理解 token 去向」的完整闭环：
> **采集 → 定价 → 存储 → 聚合 → 告警 → 归因 → 趋势 → 导出**

[![tests](https://img.shields.io/badge/tests-552%20passed-brightgreen)](#测试)
[![models](https://img.shields.io/badge/pricing%20catalog-64%20models-blue)](#模型单价表)
[![python](https://img.shields.io/badge/python-3.11%2B-3776AB)](#快速开始)
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](#)

---

## 这是什么

[`ai-token-hud`](https://github.com/WuDuKangBaZi/ai-token-hud) 是一个优秀的**单机桌面悬浮窗**：实时显示 Claude Code / Codex 的 token 流速、今日费用、缓存命中率，内置 50+ 模型单价，支持每日预算告警。

但它有一个天然边界——**它面向单个开发者、单台机器、单个窗口**。当一个人同时跑 4 个 Agent、跨 5 个项目时，「我现在烧了多少钱」就不再是可回答的问题了。

TokenLens 保留 HUD 的交互模型（实时横条、秒级刷新、维度切换），把账本搬到**服务端**：

| 能力 | 单机 HUD | **TokenLens** |
|---|---|---|
| 实时 token 流速 / 今日费用 / 缓存命中率 | ✅ | ✅ |
| 内置模型单价表 | 50+ | **64 个模型 / 7 家 provider**，4 档价格 + 价格版本 |
| **项目维度**累计 token 与预估账单 | ❌ | ✅ 按 project / agent / model / session / runtime 五维聚合 |
| **多 Agent 运行时**统一上报 | ❌ | ✅ OpenAI / Anthropic / Ollama / DeepSeek / Gemini / xAI / Mistral |
| **日/周/月预算 + 阈值告警** | 仅每日 | ✅ 三级阈值 × 四作用域 × 三周期 |
| **告警触发次数 / 超支金额** | ❌ | ✅ 幂等告警行，`trigger_count` 累积 |
| **成本异常检测 + 根因归因** | ❌ | ✅ 鲁棒 z-score + 六类根因分类 |
| **历史趋势 + CSV 导出** | ❌ | ✅ 日/周/月趋势、同比环比、7 种数据集导出 |
| 数据持久化 / 多人共享 | ❌ | ✅ SQLite(WAL) + REST API |

**这不是一个「token 计数器」，而是一套 token 可观测性基础设施。** 核心论点：*每一个 token 都能追溯到具体的 agent、项目、会话、模型和一个价格版本。*

---

## 架构

```
┌───────────────────────────── Agent 运行时（多客户端）────────────────────────────┐
│  Claude Code hook      Codex wrapper     OpenAI proxy    Ollama sidecar        │
│  (JSONL tail)          (usage parser)    (ASGI middleware) (token counter)     │
└───────────┬──────────────────┬────────────────┬─────────────────┬───────────────┘
            └──────────────────┴────────┬───────┴─────────────────┘
                                        │  POST /api/v1/ingest/snapshots
                                        │  （批量 ≤5000，event_id 幂等）
                              ┌─────────▼───────────┐
                              │    TokenLens API    │  FastAPI + Uvicorn
                              │  ┌───────────────┐  │
                              │  │ ingest        │──┼──► 定价目录（64 模型）
                              │  ├───────────────┤  │
                              │  │ query / 聚合   │──┼──► SQLite（WAL）
                              │  ├───────────────┤  │      ├─ usage_snapshots   原始
                              │  │ budget engine │──┼──►   ├─ usage_rollup_hourly 缓存
                              │  ├───────────────┤  │      ├─ budgets / alert_events
                              │  │ anomaly       │──┼──►   └─ anomaly_events
                              │  └───────────────┘  │
                              └────┬────────┬───────┘
                    ┌──────────────┘        └───────────────┐
           ┌────────▼────────┐                     ┌────────▼──────────┐
           │ HUD CLI (rich)  │                     │ Web Dashboard     │
           │ 实时横条 / 项目切换│                     │ 手写 SVG 图表      │
           └─────────────────┘                     └───────────────────┘
                    │
           ┌────────▼──────────┐  Webhook / 桌面通知
           │ 告警通道           │──► Slack · 飞书 · Teams · 系统通知
           └───────────────────┘
```

设计细节见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**，接口契约见 **[docs/API_CONTRACT.md](docs/API_CONTRACT.md)**。

---

## 五个子系统

### 1. 采集：快照是「增量」，不是累计计数器

```json
POST /api/v1/ingest/snapshots
{"snapshots": [{
  "event_id": "01JC8…", "ts": "2026-11-10T13:04:05.123Z",
  "agent": "claude-code", "project": "payments-api", "session_id": "sess-9f2",
  "model": "claude-sonnet-4-5", "runtime": "anthropic",
  "input_tokens": 1200, "output_tokens": 400,
  "cache_read_tokens": 3000, "cache_write_tokens": 200,
  "requests": 1, "retries": 0, "tool_calls": 1,
  "context_window": 200000, "tool_result_bytes": 8421
}]}
```

关键决策：

- **批量而非逐条** — 一个编码 Agent 每分钟产生数百条小增量，每条一个 HTTP 往返会让「监控」本身成为瓶颈。单事务写入最多 5000 条快照。
- **`event_id` 幂等** — `usage_snapshots.event_id` 有 `UNIQUE` 约束 + `INSERT OR IGNORE`。超时重传的批次计入 `duplicates`，**永不重复计费**。客户端不传时用 `blake2b` 派生确定性 id，字节级重放同样去重。
- **入库即定价并打版本戳** — `cost_usd` 在写入时按 `PRICE_VERSION` 算好并落库。历史重算是一次刻意的迁移，而不是 provider 改价导致的静默漂移。
- **五类 token 永不合并** — `input / output / cache_read / cache_write / reasoning` 分开存储。缓存读取只花 input 的 10%，是真实支出的最大杠杆，必须始终可见。

### 2. 定价：64 个模型 × 4 档价格

Anthropic / OpenAI / DeepSeek / Google / xAI / Mistral / 本地 Ollama，每个模型 4 档单价（input、output、cache_read、cache_write）+ 上下文窗口。

解析是宽容的：带日期快照 `claude-sonnet-4-5-20250929`、provider 前缀 `openai/gpt-4.1-mini`、简写 `sonnet` 都能归一到规范条目。

**未知模型不拒绝** —— 用文档化的兜底价定价，并在响应的 `unknown_models` 里回传。新模型发布绝不应该打断采集。本地模型按 `$0` 编目并标注 `local-gpu-amortised`：它们不表现为隐形支出，而表现为**零成本但被记账**的用量。

### 3. 预算告警：幂等的「触发次数」

预算 = `(scope, scope_key, period)` 三元组，`global/project/agent/model` × `daily/weekly/monthly` 共 12 种组合。窗口按**用户时区的自然日历**对齐（上海用户的「一天」从 00:00 +08:00 开始，不是 00:00 UTC）。

阈值评估在结构上就是幂等的：

```sql
UNIQUE(budget_id, window_start, threshold)
```

首次突破插入告警行；同一窗口同一阈值的后续评估**更新**该行（`trigger_count += 1`、刷新 `spent_usd` / `overspend_usd`）。这给出两个「只告警一次」的实现拿不到的指标：

- **被重复突破了多少次**（`trigger_count`）
- **超出了多少钱**（`overspend_usd = max(0, spent − limit)`）

通知只在 `trigger_count` 真正增长时下发——指标精确，但不刷屏。

### 4. 异常检测：鲁棒 z-score + 根因归因

Token 支出是重尾、突发的，均值/σ 的 z-score 在这里失效——一次重试风暴就足以把均值抬到掩盖自己。

```
baseline = median(每分钟 token 数，窗口 [now − 6L, now − L))
σ_robust = 1.4826 × median(|x − baseline|)        （下限 200 tok/min）
score    = (p95(观测窗口) − baseline) / σ_robust
触发     ⇔ score ≥ 4  ∧  observed ≥ 3×baseline  ∧  observed ≥ 2000 tok/min
```

三段式条件让它真正可用：z-score 捕捉**统计**异常，倍数条件避免把极小的绝对波动当成异常，绝对下限避免安静 agent 从 3 tok/min 涨到 12 tok/min 就惊动任何人。

检出后再从原始证据**归因根因**——「支出飙升」不可行动，但「重试风暴」可以：

| 根因 | 证据规则 | 运维该做什么 |
|---|---|---|
| `retry_storm` 重试风暴 | `retries/requests ≥ 15%` 或 ≥2 次重试/请求 | 退避、查 provider 429/5xx、修幂等性 |
| `context_bloat` 上下文膨胀 | 平均上下文窗口 ≥120k **且** ≥1.5× 自身基线 | 压缩历史、调高摘要阈值 |
| `uncompressed_tool_output` 工具返回未压缩 | `tool_result_bytes/请求 ≥ 200KB` | 截断工具返回、摘要化文件读取 |
| `cache_miss_storm` 缓存命中崩塌 | 缓存命中率相对基线下降 ≥40% | prompt 前缀变了、缓存 TTL 太短 |
| `traffic_spike` 流量尖峰 | 请求量 ≥2.5× 且单请求 token 持平 | 正常负载——调预算，不是改代码 |
| `unknown` 未知 | 以上都不匹配 | 人工排查窗口内的原始快照 |

去重指纹 `sha1(agent | reason | window_start/5min)`：持续事故每 5 分钟产出一个事件，而不是每次检测轮询都产出一个。

### 5. 查询与呈现

一个共享 SQL 构造器（`build_filters`）拼装所有 `WHERE`，所以 query / trends / export / dashboard 四个模块**不可能**在行为或注入安全上分叉。所有聚合返回同一个 `Totals` 形状。

时间序列**补零**：没有流量的桶输出零值而不是被跳过——静默跳过安静时段的图表，在撒谎。

---

## 实测证据

全部由 `python tools/capture_evidence.py` 通过**公开 HTTP API**（与真实部署完全一致）生成，原始产物在 **[docs/evidence/](docs/evidence/)**。

### 1. 灌入百万级 token（测试监控系统本身）

| 指标 | 数值 |
|---|---|
| 目标 token | 1,000,000 |
| 实际灌入 | 1,031,162 |
| 快照数 | 80（2 批） |
| 客户端吞吐 | **2,952.3 快照/s · 38.1M token/s** |
| 服务端 ingest 耗时 | 12.7 ms（仅定价+事务+汇总） |
| 持续基准（10×100 快照定批） | **8,775.7 快照/s**，p50 16.6 ms/批 |
| 模拟账单 | $1.9955 @ 价格版本 2025-11-01 |
| 定价目录 | 64 个模型 |

> 这不是真实的 API 费用，而是**可控的、零成本的 token 消耗**——它压测的是数据处理吞吐量本身。

**幂等性验证**：同一批次重放 → `accepted=0, duplicates=50`，金额零增长。

### 2. 告警触发记录（含触发次数与超支金额）

| 预算 | 作用域 | 限额 | 已用 | 占比 | **触发次数** | **超支** | 状态 |
|---|---|---|---|---|---|---|---|
| 全局日预算 | global:* | $2.3325 | $3.3822 | **145.0%** | **2** | **$1.0497** | `exceeded` |
| payments-api 日预算 | project:payments-api | $0.7754 | $0.6979 | 90.0% | **2** | $0.0000 | `critical` |
| claude-code 日预算 | agent:claude-code | $1.0619 | $0.5840 | 55.0% | **2** | $0.0000 | `warning` |

**总计：6 条告警 / 12 次触发 / 超支 $1.0497。**

注意 `trigger_count = 2`：第二次评估复用了同一告警行并累加，**没有产生重复告警**，也**没有重复通知**。这正是「告警触发次数」这个指标能存在的前提。原始记录：[`docs/evidence/alerts.csv`](docs/evidence/alerts.csv)、[`budget_alert_record.json`](docs/evidence/budget_alert_record.json)

### 3. 异常检测案例（三个真实注入的事故）

在 3 小时平静基线（540 条快照）之上注入三类事故（120 条快照），一次检测全部命中并正确归因：

| Agent | 归因 | 严重度 | 观测 tok/min | 基线 tok/min | score | 倍数 | 根因证据 |
|---|---|---|---|---|---|---|---|
| `claude-code` | **重试风暴** | critical | 31,346 | 2,780 | 142.83 | **11.28×** | 重试率 74.1%（361/487 次请求），阈值 15% |
| `codex-cli` | **上下文膨胀** | critical | 44,532 | 2,050 | 212.41 | **21.72×** | 上下文窗口 200,000 → 891,667（4.5×），阈值 1.5× |
| `deepseek-agent` | **工具返回未压缩** | critical | 38,611 | 2,600 | 180.06 | **14.85×** | 每次请求工具返回 2,004,822 字节（基线 5,000），阈值 200,000 |

原始记录：[`docs/evidence/anomalies.csv`](docs/evidence/anomalies.csv)、[`anomaly_cases.json`](docs/evidence/anomaly_cases.json)

### 4. Dashboard KPI 快照

今日成本 **$18.15** · 今日 token **13.62M** · burn rate **2.21M tok/min** · 缓存命中率 **11.7%** · 5 个活跃 Agent / 5 个项目

---

## 快速开始

```bash
# 1. 安装
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt

# 2. 起服务（默认 127.0.0.1:8787）
python run_server.py
#   或：uvicorn tokenlens.app:app --port 8787

# 3. 打开 Web 控制台
start http://127.0.0.1:8787

# 4. 实时 HUD（ai-token-hud 风格的终端横条）
tokenlens hud
```

### 灌入模拟流量（测试平台本身）

```bash
# 稳态：10 秒，5 agent / 4 project / 20 session
python tools/simulate.py --mode steady --duration 10 --report

# 事故模式：注入三类异常签名，供检测器发现
python tools/simulate.py --mode incident --duration 20 --report

# 目标驱动：灌满 100 万 token 后停止
python tools/simulate.py --mode mixed --target-tokens 1000000 --report

# 离线演练（不发 HTTP，可进 CI）
python tools/simulate.py --dry-run --mode incident --target-tokens 100000
```

### HUD 客户端

```bash
tokenlens hud                     # 实时视图，1s 刷新，p/a/m/b 切换维度
tokenlens hud --once --no-color   # 单帧输出，CI 友好
tokenlens summary --from 2026-11-10 --agent claude-code
tokenlens breakdown --dimension model
tokenlens budgets                 # 预算利用率与预计超支
tokenlens anomalies               # 异常事件 + 根因证据
tokenlens export --dataset timeseries --out usage.csv
tokenlens detect --lookback 60    # 手动触发一次检测
```

HUD 实际输出：

```
TokenLens HUD  http://127.0.0.1:8787  14:46:47 UTC  [PROJECT]
────────────────────────────────────────────────────────────────────────────
  today $42.23   week $42.23   month $42.23
  burn 0/min ($0.0000/h)   cache hit 33.0%   tokens(today) 26.21M
  agents 5   projects 5   sessions 11
────────────────────────────────────────────────────────────────────────────
  BUDGETS
  全局日预算              $42.23 / $2.33    1810.4%  exceeded ████████████████
  payments-api 日预算     $8.45 / $0.775    1090.2%  exceeded ████████████████
────────────────────────────────────────────────────────────────────────────
  BY PROJECT   (cost · share · tokens)
  web-dashboard         $17.34   41.1%  ████████████████████████████  11.10M tok
  payments-api           $8.45   20.0%  ██████████████░░░░░░░░░░░░░░   3.83M tok
  ml-pipeline            $5.73   13.6%  █████████░░░░░░░░░░░░░░░░░░░   5.55M tok
────────────────────────────────────────────────────────────────────────────
  keys: p project · a agent · m model · b budget · q quit
```

---

## API

完整契约见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)。服务启动后可在 `/docs` 查看自动生成的 OpenAPI 文档。

| 分组 | 端点 |
|---|---|
| 采集 | `POST /api/v1/ingest/snapshots` · `/snapshot` · `GET /ingest/ping` |
| 用量 | `GET /api/v1/usage/summary` · `/breakdown` · `/timeseries` · `/live` |
| 趋势 | `GET /api/v1/trends?period=day\|week\|month` |
| 目录 | `GET /api/v1/catalog/models` · `/agents` · `/projects` · `/sessions` |
| 导出 | `GET /api/v1/export/csv?dataset=timeseries\|breakdown\|sessions\|snapshots\|alerts\|anomalies\|budgets` |
| 预算 | `GET/POST /api/v1/budgets` · `PATCH/DELETE /budgets/{id}` · `POST /budgets/evaluate` |
| 告警 | `GET /api/v1/alerts` · `/alerts/summary` · `/alerts/{id}` |
| 异常 | `POST /api/v1/anomalies/detect` · `GET /anomalies` · `/anomalies/summary` · `/anomalies/{id}` · `PATCH` |
| 聚合 | `GET /api/v1/dashboard/overview?days=7` |
| 健康 | `GET /health` · `GET /api/v1/meta` |

**鉴权**：设置 `TOKENLENS_API_TOKEN` 后，所有 `/api/v1/**` 需带 `X-API-Key`（`/health` 与 `/api/v1/meta` 保持开放，便于探活）。

---

## 测试

```
$ python -m pytest tests -q
552 passed
```

| 套件 | 测试数 | 覆盖 |
|---|---|---|
| `test_foundation.py` | 47 | 定价、时间工具、存储、幂等 ingest、app 装配 |
| `test_query_api.py` | 98 | 聚合、分桶、补零、筛选组合、CSV、空库、400 路径 |
| `test_budget_anomaly.py` | 187 | 预算 CRUD、阈值触发、幂等 trigger_count、webhook、异常检测与归因 |
| `test_cli_simulator.py` | 184 | CLI 参数面、渲染、离线降级、模拟器确定性与事故签名 |
| `test_dashboard_api.py` | 25 | 静态资源、契约漂移守卫（JS 里的每个 fetch 都必须存在于 OpenAPI） |
| `test_integration.py` | 11 | 端到端：灌数据 → KPI → 预算触发 → 异常检出 → CSV 导出 → 鉴权 |

**集成测试抓到了三个真实后端缺陷**（这正是 E2E 套件存在的意义）：

1. `budget.py` 的 INSERT 有 17 个占位符但只绑定了 16 个值 → 任何预算**首次**突破阈值都会 500（只做重复评估永远发现不了）。
2. `anomaly.py` 把补零的分钟喂进 median/MAD，得到 `baseline = 0.0`，使 `observed ≥ 3×baseline` 守卫失效 → 普通 agent 收到虚假 critical 告警。
3. `dashboard.py` 输出 `breakdowns.{agent,…}`，而契约要求 `by_agent`。

### 跨天稳定性：一个「昨天通过、今天失败」的教训

上线次日重跑测试，`test_budget_anomaly.py` 有 8 个用例失败——而昨天全绿。这类失败最容易被当成环境问题糊过去，实际是两个**真实的确定性缺陷**：

1. **测试时钟与数据时钟不一致。** 用例把快照写死在 `2026-11-10T06:00Z`，但 HTTP 端点从 `timeutil.now_ms()` 取「当前窗口」。昨天真实时钟恰好还落在同一个「日」窗口里，所以通过；一旦跨天，落库的支出就不在窗口内了，读数为 0。修复：`conftest.py` 加一个 **autouse 的 `frozen_clock` fixture** 冻结 `timeutil.now_ms()`，让「现在」与 fixture 数据用同一个锚点。该模块的 docstring 原本声称「不依赖墙钟」，但只有单元测试真的传了 `now_ms`，走 HTTP 的用例并没有——现在这个声明是真的了。
2. **`dashboard.py` 的分桶端点把当前桶丢了。** `_timeseries` 的 `end_ms` 是开区间，而代码直接传了 `now`。当 `now` 恰好落在整点（`06:00:00.000`）时，当前小时桶被排除，返回 23 个点；`06:00:00.001` 时返回 24 个。修复：窗口右端对齐到**当前桶的结束时刻**，保证点数恒定且总是包含进行中的桶——这也才是实时 sparkline 应该显示的东西。

验证方式：把系统日期分别伪装成 `2026-11-11` / `2026-12-31` / `2027-03-15` 各跑一遍，结果稳定 `223 passed`。**测试不该在跨年夜里变红。**

---

## 工程决策

**为什么 SQLite 而不是 PostgreSQL？** 单写入者、读多写少的场景下，WAL 模式让 dashboard 的 3 秒轮询与 ingest 写入并发执行，而不必在全局锁上串行。Schema 是可移植的——换 PG 只需替换 `db.py` 并保留 `build_filters` 的契约。

**为什么原始快照是真源，而 rollup 是缓存？** 聚合永远由 SQL 从原始表算出，`usage_rollup_hourly` 只是同一事务里维护的加速缓存。部分汇总的窗口会静默少报——所以每条查询都有原始表兜底。

**为什么按连接而不是按进程持有 SQLite 句柄？** SQLite 对象非线程安全，而 FastAPI 把同步端点跑在线程池里。每单元工作开一个连接只要微秒级，并彻底避开 `check_same_thread=False` 的坑。

**金额用 REAL(float64) 并保留 6 位小数。** 每 token 单价约 1e-7 USD，6 位小数是 1/100 美分。SQLite 没有 DECIMAL 类型，在这个量级上 SUM() 的精度足够（有测试验证）。

**为什么异常检测不用均值？** 见上文——重尾分布下，均值会被异常本身污染到掩盖异常。median/MAD 的崩溃点是 50%，一个坏点无法动摇它。

---

## 故障行为

| 故障 | 行为 |
|---|---|
| 服务不可用 | HUD 显示 `OFFLINE` 横幅并保留上一帧；模拟器重试批次 |
| 批次重复 | `INSERT OR IGNORE` → 计入 `duplicates`，金额零增长 |
| 未知模型 | 兜底定价 + `unknown_models` 回传，ingest 仍成功 |
| Webhook 不可达 | 3s 超时、2 次重试，失败记入告警的 `channels`，评估流程永不阻塞 |
| 调度器崩溃 | 每轮捕获并记录，API 保持存活；`POST /budgets/evaluate` 始终可手动调用 |
| 时钟偏移 | 窗口由服务端按 UTC epoch ms 计算；客户端时间戳只影响分桶 |

---

## 目录结构

```
tokenlens/
├── tokenlens/                  # 服务端
│   ├── app.py                  # FastAPI 装配 + 生命周期
│   ├── config.py               # 环境变量配置
│   ├── timeutil.py             # 时间/分桶/日历窗口
│   ├── pricing.py              # 64 模型 × 4 档单价 + 别名归一
│   ├── schemas.py              # 全部 Pydantic 契约模型
│   ├── db.py                   # SQLite(WAL) schema + 共享 SQL 构造器
│   ├── store.py                # ingest 管线（定价→事务→汇总）
│   ├── query.py                # 聚合层（summary/breakdown/timeseries/live）
│   ├── budget.py               # 预算引擎 + 幂等告警
│   ├── anomaly.py              # 鲁棒 z-score + 根因归因
│   ├── notify.py               # Webhook / 桌面通知通道
│   ├── scheduler.py            # 后台评估循环
│   ├── testing.py              # 测试与模拟器共用的数据工厂
│   ├── api/                    # 10 个路由模块
│   └── web/                    # 零构建 Web 控制台（手写 SVG）
├── tokenlens_cli/              # HUD 客户端（rich）
│   ├── hud.py                  # 实时横条 + 维度切换
│   ├── render.py               # 纯渲染函数（可单测）
│   ├── commands.py             # 12 个子命令
│   └── api.py                  # HTTP 客户端
├── tools/
│   ├── simulate.py             # 百万级 token 负载生成器
│   └── capture_evidence.py     # 证据采集（本 README 的数据来源）
├── tests/                      # 552 个测试
└── docs/
    ├── ARCHITECTURE.md         # 架构与设计决策
    ├── API_CONTRACT.md         # 接口契约
    └── evidence/               # 实测产物（JSON/CSV/Markdown）
```

---

## 扩展点

- **换 PostgreSQL**：schema 可移植，替换 `db.py` 为 async SQLAlchemy 引擎，保留 `build_filters` 契约。
- **接新运行时**：把名字加进 `schemas.RUNTIMES`，模型加进 `pricing.CATALOG`，写个 shim 上报即可，服务端零改动。
- **新告警通道**：在 `notify.py` 实现一个 sink 并从 `dispatch` 返回。
- **数据保留**：长期趋势只需 `usage_rollup_hourly`，原始快照可定期裁剪而不丢历史图表。

---

## 与 ai-token-hud 的关系

TokenLens 明确站在 `ai-token-hud` 的肩膀上，并把它从**单机工具**推进到**团队基础设施**：

| | ai-token-hud | TokenLens |
|---|---|---|
| 形态 | 桌面悬浮窗 | 服务端 + 多客户端（CLI HUD / Web） |
| 视角 | 当前机器 | 多 agent × 多项目 × 多会话 |
| 单价表 | 内置 50+ | 版本化目录 64 模型，4 档价格 |
| 预算 | 每日 | 日/周/月 × 四作用域 × 三级阈值 |
| 告警度量 | 触发即止 | 触发次数 + 超支金额（幂等累积） |
| 异常 | ❌ | 鲁棒 z-score + 六类根因 |
| 数据 | 本地视图 | 可查询、可导出、可追溯 |

---

MIT License · 用 `python tools/capture_evidence.py` 可一键复现本 README 中的所有数字。
