"""Cost anomaly detection: robust z-score on per-agent token burn + root-cause classifier.

Why this algorithm
------------------
Token spend is heavy tailed and *seasonal*: a coding agent burns 300 tokens/min while you
think and 30 000 tokens/min while it rewrites a file. A mean/standard-deviation z-score
would either be dominated by those spikes or need weeks of history to stabilise. The
robust (median / MAD) statistic below has a 50 % breakdown point, so a window full of
incidents still produces a sane baseline:

1. **Series.** For every agent, bucket ``usage_snapshots`` into one-minute buckets over
   ``[now - lookback, now)``; the metric is ``total_tokens / minute``. Empty minutes are
   real zeros, not gaps — an agent that was idle for ten minutes has a baseline of zero
   and any burst is genuinely anomalous.
2. **Baseline.** ``median`` of the same per-minute series over the *history* window
   ``[now - 6 * lookback, now - lookback)``. When there is no history at all (fresh
   install, brand new agent) the detector falls back to the first 20 minutes of the
   observation window, so it can still fire on the very first incident.
3. **Robust sigma.** ``1.4826 * median(|x - median|)`` — the MAD, scaled to be a
   consistent estimator of the standard deviation for normal data — with a floor of
   ``ANOMALY_SIGMA_FLOOR`` (200 tokens/min) so a perfectly flat baseline cannot produce a
   division-by-zero "infinite" score.
4. **Trigger.** ``observed`` is the 95th percentile of the observation window (one bad
   request must not raise the alert, a sustained burst must). Flag only when all three
   guards agree: ``score >= 4`` **and** ``observed >= 3 x baseline`` **and**
   ``observed >= 2000 tokens/min``. The ratio guard suppresses tiny-but-statistically-loud
   blips; the absolute guard suppresses noise from idle agents.
5. **Classification.** Once something is anomalous, *what* happened is decided from the
   raw snapshot evidence of the offending window (retry ratio, context window, tool result
   bytes, cache hit rate collapse, request growth). The first matching rule wins, in the
   order the contract lists them.
6. **Dedupe.** ``sha1(agent|reason|window_start // 300000)`` — one event per agent, per
   reason, per 5-minute bucket, enforced by ``anomaly_events.fingerprint UNIQUE`` and
   ``INSERT OR IGNORE``. A 30 s scheduler loop therefore cannot flood the table with the
   same incident.

Everything the contract declares configurable is read from :class:`~tokenlens.config.Settings`
(``anomaly_lookback_minutes``, ``anomaly_z_threshold``, ``anomaly_ratio_threshold``,
``anomaly_min_tokens_per_min``), plus the classification cut-offs below, which are module
constants exposed so an operator can tune them without forking the code.
"""

from __future__ import annotations

import hashlib
import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import timeutil
from .config import Settings, get_settings
from .db import Database, build_filters, dump_json, load_json
from .schemas import AnomalyRecord, AnomalySummary, DetectResponse, Window

logger = logging.getLogger(__name__)

#: The metric this detector scores. Kept as a column value so a future detector can add
#: ``cost_per_min`` without a schema migration.
METRIC = "tokens_per_min"

#: Chinese labels for the UI (the product's interface language is Chinese).
REASON_LABELS: dict[str, str] = {
    "retry_storm": "重试风暴",
    "context_bloat": "上下文膨胀",
    "uncompressed_tool_output": "工具返回数据未压缩",
    "cache_miss_storm": "缓存命中崩塌",
    "traffic_spike": "流量尖峰",
    "unknown": "未知",
}

#: Classification order matters: the first matching rule wins (contract §8.4).
REASON_ORDER: tuple[str, ...] = (
    "retry_storm",
    "context_bloat",
    "uncompressed_tool_output",
    "cache_miss_storm",
    "traffic_spike",
    "unknown",
)

#: Anomaly statuses.
STATUSES: tuple[str, ...] = ("open", "acknowledged", "resolved")

# ---- algorithm constants (contract §8) ------------------------------------------------

#: Floor for the robust sigma, in tokens/min. Prevents infinite scores on flat baselines.
ANOMALY_SIGMA_FLOOR = 200.0

#: Minimum observation history used by the no-history fallback (minutes).
FALLBACK_BASELINE_MINUTES = 20

#: Minimum number of **active** (non-empty) history minutes before the history branch is
#: trusted. Without this, an agent that only started reporting recently is scored against
#: a gap-filled history window that is almost entirely zeros: the median collapses to 0,
#: the "observed >= 3x baseline" guard becomes trivially true, and every fresh install
#: gets a burst of false ``critical`` alerts on its first detection pass.
MIN_HISTORY_MINUTES = 10

#: Upper clamp for the persisted ``ratio`` (and for the severity comparison when the
#: baseline is tiny). ``ratio`` must stay finite: JSON has no infinity, and persisting
#: ``0.0`` for an infinite spike reads as "observed is zero" — the exact opposite.
MAX_RATIO = 999.0

#: Minutes per baseline history window multiplier: history = 6 x lookback.
HISTORY_MULTIPLIER = 6

#: Fingerprint bucket: 5 minutes.
FINGERPRINT_BUCKET_MS = 300_000

#: Percentile used for ``observed`` (p95 of the observation window).
OBSERVED_PERCENTILE = 0.95

# ---- classification thresholds (contract §8.4) ---------------------------------------

#: retry_storm: retries / requests >= 15 % ...
RETRY_RATIO_THRESHOLD = 0.15
#: ... or mean retries per request >= 2.
RETRY_PER_REQUEST_THRESHOLD = 2.0
#: context_bloat: mean context_window >= 120k tokens ...
CONTEXT_WINDOW_THRESHOLD = 120_000
#: ... and, when the agent has history, at least this much larger than its own baseline.
#: Rationale (deviation from the literal contract, reported to the Lead): modern coding
#: agents legitimately run at a 200k-1M context, so the absolute 120k rule alone labels
#: *every* incident as context_bloat for those agents. Requiring growth relative to the
#: agent's own baseline keeps the rule honest while still firing on a 3x-5x context jump.
CONTEXT_WINDOW_GROWTH_RATIO = 1.5
#: ... or avg input tokens per request >= 3x the agent's 24h baseline.
CONTEXT_INPUT_RATIO_THRESHOLD = 3.0
#: uncompressed_tool_output: tool_result_bytes / requests >= 200 KB.
TOOL_RESULT_BYTES_PER_REQUEST = 200_000
#: cache_miss_storm: cache hit rate dropped by >= 40 % relative to the agent baseline.
CACHE_HIT_DROP_THRESHOLD = 0.40
#: traffic_spike: request volume grew >= 2.5x while per-request tokens stayed flat.
TRAFFIC_GROWTH_THRESHOLD = 2.5
#: "stayed flat" tolerance for per-request tokens (+/- 25 %).
TRAFFIC_FLAT_TOLERANCE = 0.25

#: Severity cut-offs (contract §8.5).
SEVERITY_CRITICAL_SCORE = 10.0
SEVERITY_CRITICAL_RATIO = 8.0
SEVERITY_HIGH_SCORE = 7.0
SEVERITY_MEDIUM_SCORE = 5.0


# --------------------------------------------------------------------------------------
# Small statistics helpers (pure, unit tested)
# --------------------------------------------------------------------------------------


def median(values: Sequence[float]) -> float:
    """Median of ``values`` (0.0 for an empty sequence)."""
    if not values:
        return 0.0
    return float(statistics.median(values))


def robust_sigma(values: Sequence[float], *, floor: float = ANOMALY_SIGMA_FLOOR) -> float:
    """MAD-based robust standard deviation: ``1.4826 * median(|x - median(x)|)``.

    The 1.4826 factor makes the MAD a consistent estimator of sigma for normally
    distributed data. A floor is applied so a constant series yields a usable denominator.
    """
    if not values:
        return float(floor)
    centre = median(values)
    deviations = [abs(float(value) - centre) for value in values]
    sigma = 1.4826 * median(deviations)
    return max(float(floor), float(sigma))


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (``q`` in ``[0, 1]``); 0.0 for an empty sequence."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(float(q), 0.0), 1.0) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def robust_score(observed: float, baseline: float, sigma: float) -> float:
    """``(observed - baseline) / sigma``, clamped at 0 (we only care about increases)."""
    denominator = max(1e-9, float(sigma))
    return max(0.0, (float(observed) - float(baseline)) / denominator)


def severity_for(score: float, ratio: float) -> str:
    """Severity from score/ratio per contract §8.5."""
    if score >= SEVERITY_CRITICAL_SCORE or ratio >= SEVERITY_CRITICAL_RATIO:
        return "critical"
    if score >= SEVERITY_HIGH_SCORE:
        return "high"
    if score >= SEVERITY_MEDIUM_SCORE:
        return "medium"
    return "low"


def fingerprint_for(agent: str, reason: str, window_start: int) -> str:
    """``sha1(agent|reason|window_start // 300000)`` — the dedupe key."""
    bucket = int(window_start) // FINGERPRINT_BUCKET_MS
    return hashlib.sha1(f"{agent}|{reason}|{bucket}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Per-minute series
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class MinuteBucket:
    """One minute of one agent's usage, aggregated from raw snapshots."""

    minute_ms: int
    total_tokens: int = 0
    cost_usd: float = 0.0
    requests: int = 0
    retries: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    snapshots: int = 0
    tool_result_bytes: int = 0
    context_window_sum: int = 0
    context_window_count: int = 0

    @property
    def tpm(self) -> float:
        """Tokens per minute for this bucket."""
        return float(self.total_tokens)


def minute_series(
    db: Database,
    agent: str,
    start_ms: int,
    end_ms: int,
    *,
    project: str | None = None,
) -> dict[int, MinuteBucket]:
    """Aggregate one agent's snapshots into per-minute buckets inside ``[start_ms, end_ms)``.

    Gap filling happens in :func:`series_values`: an absent minute is a zero minute.
    """
    where, params = build_filters(
        agent=agent, project=project, start_ms=int(start_ms), end_ms=int(end_ms)
    )
    rows = db.fetch_all(
        f"""
        SELECT (s.ts / 60000) * 60000                        AS minute_ms,
               SUM(s.input_tokens + s.output_tokens + s.cache_read_tokens
                   + s.cache_write_tokens + s.reasoning_tokens) AS total_tokens,
               SUM(s.cost_usd)                               AS cost_usd,
               SUM(s.requests)                               AS requests,
               SUM(s.retries)                                AS retries,
               SUM(s.tool_calls)                             AS tool_calls,
               SUM(s.input_tokens)                           AS input_tokens,
               SUM(s.output_tokens)                          AS output_tokens,
               SUM(s.cache_read_tokens)                      AS cache_read_tokens,
               SUM(s.cache_write_tokens)                     AS cache_write_tokens,
               SUM(s.reasoning_tokens)                       AS reasoning_tokens,
               SUM(s.tool_result_bytes)                      AS tool_result_bytes,
               COUNT(*)                                      AS snapshots,
               SUM(CASE WHEN s.context_window IS NOT NULL THEN s.context_window ELSE 0 END)
                                                             AS context_window_sum,
               SUM(CASE WHEN s.context_window IS NOT NULL THEN 1 ELSE 0 END)
                                                             AS context_window_count
          FROM usage_snapshots s{where}
         GROUP BY minute_ms
         ORDER BY minute_ms ASC
        """,
        params,
    )
    series: dict[int, MinuteBucket] = {}
    for row in rows:
        series[int(row["minute_ms"])] = MinuteBucket(
            minute_ms=int(row["minute_ms"]),
            total_tokens=int(row["total_tokens"] or 0),
            cost_usd=float(row["cost_usd"] or 0.0),
            requests=int(row["requests"] or 0),
            retries=int(row["retries"] or 0),
            tool_calls=int(row["tool_calls"] or 0),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            cache_write_tokens=int(row["cache_write_tokens"] or 0),
            reasoning_tokens=int(row["reasoning_tokens"] or 0),
            tool_result_bytes=int(row["tool_result_bytes"] or 0),
            snapshots=int(row["snapshots"] or 0),
            context_window_sum=int(row["context_window_sum"] or 0),
            context_window_count=int(row["context_window_count"] or 0),
        )
    return series


def series_values(
    series: dict[int, MinuteBucket],
    start_ms: int,
    end_ms: int,
    *,
    gap_fill: bool = True,
) -> list[float]:
    """Token-per-minute values for every whole minute in ``[start_ms, end_ms)``.

    ``gap_fill=True`` (the default) emits a zero for minutes with no data, which is right
    for *plotting* — an idle minute really did burn zero tokens. Pass ``gap_fill=False``
    for *statistics*: a minute the agent never reported is "no measurement", not a
    measured zero, and including it drags the median down toward zero.
    """
    values: list[float] = []
    cursor = int(start_ms) // 60_000 * 60_000
    end = int(end_ms)
    while cursor < end:
        bucket = series.get(cursor)
        if bucket is not None:
            values.append(bucket.tpm)
        elif gap_fill:
            values.append(0.0)
        cursor += 60_000
    return values


def observed_buckets(
    series: dict[int, MinuteBucket],
    start_ms: int,
    end_ms: int,
    *,
    percentile_q: float = OBSERVED_PERCENTILE,
) -> list[MinuteBucket]:
    """Buckets in the observation window whose tpm is at or above its p95.

    These are the "offending minutes" whose raw snapshots drive root-cause
    classification: classifying from the whole window would dilute a 3-minute retry storm
    with 57 calm minutes.
    """
    buckets = [
        bucket
        for minute, bucket in sorted(series.items())
        if int(start_ms) <= minute < int(end_ms)
    ]
    if not buckets:
        return []
    threshold = percentile([bucket.tpm for bucket in buckets], percentile_q)
    selected = [bucket for bucket in buckets if bucket.tpm >= threshold]
    return selected or buckets


# --------------------------------------------------------------------------------------
# Evidence + classification
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Evidence:
    """Raw snapshot aggregates for one agent inside a time range.

    Rates are expressed **per minute** so the observation window (a handful of offending
    minutes) and the baseline window (hours of history) are directly comparable.
    """

    snapshots: int = 0
    requests: int = 0
    retries: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    tool_result_bytes: int = 0
    context_window_sum: int = 0
    context_window_count: int = 0
    cost_usd: float = 0.0
    #: Number of whole minutes the evidence spans (>= 1), used for rate normalisation.
    minutes: int = 1

    @classmethod
    def from_buckets(cls, buckets: Iterable[MinuteBucket]) -> "Evidence":
        evidence = cls()
        minute_starts: list[int] = []
        for bucket in buckets:
            minute_starts.append(bucket.minute_ms)
            evidence.snapshots += bucket.snapshots
            evidence.requests += bucket.requests
            evidence.retries += bucket.retries
            evidence.tool_calls += bucket.tool_calls
            evidence.total_tokens += bucket.total_tokens
            evidence.input_tokens += bucket.input_tokens
            evidence.output_tokens += bucket.output_tokens
            evidence.cache_read_tokens += bucket.cache_read_tokens
            evidence.cache_write_tokens += bucket.cache_write_tokens
            evidence.reasoning_tokens += bucket.reasoning_tokens
            evidence.tool_result_bytes += bucket.tool_result_bytes
            evidence.context_window_sum += bucket.context_window_sum
            evidence.context_window_count += bucket.context_window_count
            evidence.cost_usd += bucket.cost_usd
        # Span from first to last minute inclusive, so a sparse series is not divided by
        # the number of non-empty minutes (that would inflate every rate).
        if minute_starts:
            evidence.minutes = max(1, (max(minute_starts) - min(minute_starts)) // 60_000 + 1)
        return evidence

    @property
    def retry_ratio(self) -> float:
        """``retries / requests`` (0 when no requests were made)."""
        return float(self.retries) / float(self.requests) if self.requests else 0.0

    @property
    def retries_per_request(self) -> float:
        """Mean retries per request (same as the ratio, kept explicit for readability)."""
        return self.retry_ratio

    @property
    def mean_context_window(self) -> float:
        """Mean ``context_window`` over snapshots that reported one."""
        if not self.context_window_count:
            return 0.0
        return float(self.context_window_sum) / float(self.context_window_count)

    @property
    def tokens_per_request(self) -> float:
        """Average tokens billed per request."""
        return float(self.total_tokens) / float(self.requests) if self.requests else 0.0

    @property
    def prompt_tokens_per_request(self) -> float:
        """Average *prompt* tokens per request (cached + uncached + cache writes).

        This is the honest measure of "how big is the context". Using only uncached
        ``input_tokens`` would misread a cache-miss storm (same prompt, no cache hits) as
        context growth, because uncached input rises while the prompt size does not.
        """
        prompt = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return float(prompt) / float(self.requests) if self.requests else 0.0

    @property
    def input_tokens_per_request(self) -> float:
        """Average *uncached* prompt tokens per request."""
        return float(self.input_tokens) / float(self.requests) if self.requests else 0.0

    @property
    def tool_bytes_per_request(self) -> float:
        """``tool_result_bytes / requests`` — the uncompressed-tool-output signal."""
        return float(self.tool_result_bytes) / float(self.requests) if self.requests else 0.0

    @property
    def cache_hit_rate(self) -> float:
        """``cache_read / (cache_read + input)`` — same definition as ``Totals``."""
        denominator = self.cache_read_tokens + self.input_tokens
        return float(self.cache_read_tokens) / float(denominator) if denominator else 0.0

    @property
    def requests_per_snapshot(self) -> float:
        """Request amplification: how many upstream calls one snapshot represents."""
        return float(self.requests) / float(self.snapshots) if self.snapshots else 0.0

    @property
    def requests_per_minute(self) -> float:
        """Request rate, normalised over the evidence span."""
        return float(self.requests) / float(max(1, self.minutes))

    @property
    def tool_bytes_per_minute(self) -> float:
        """Tool result byte rate, normalised over the evidence span."""
        return float(self.tool_result_bytes) / float(max(1, self.minutes))

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "requests": self.requests,
            "retries": self.retries,
            "tool_calls": self.tool_calls,
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "tool_result_bytes": self.tool_result_bytes,
            "mean_context_window": round(self.mean_context_window, 1),
            "retry_ratio": round(self.retry_ratio, 4),
            "tool_result_bytes_per_request": round(self.tool_bytes_per_request, 1),
            "tokens_per_request": round(self.tokens_per_request, 1),
            "prompt_tokens_per_request": round(self.prompt_tokens_per_request, 1),
            "input_tokens_per_request": round(self.input_tokens_per_request, 1),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass(slots=True)
class Classification:
    """Root cause of one anomaly plus the numbers that justified it."""

    reason: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, REASON_LABELS["unknown"])


def classify_root_cause(
    window: Evidence,
    baseline: Evidence,
    *,
    context_window_threshold: float = CONTEXT_WINDOW_THRESHOLD,
    context_window_growth_ratio: float = CONTEXT_WINDOW_GROWTH_RATIO,
    retry_ratio_threshold: float = RETRY_RATIO_THRESHOLD,
    retry_per_request_threshold: float = RETRY_PER_REQUEST_THRESHOLD,
    tool_bytes_per_request_threshold: float = TOOL_RESULT_BYTES_PER_REQUEST,
    cache_drop_threshold: float = CACHE_HIT_DROP_THRESHOLD,
    traffic_growth_threshold: float = TRAFFIC_GROWTH_THRESHOLD,
    flat_tolerance: float = TRAFFIC_FLAT_TOLERANCE,
) -> Classification:
    """Classify the offending window against the agent's historical baseline.

    Rules are evaluated in the contract's order and the first match wins; ``unknown`` is
    the fallback so a persisted anomaly always has a reason.

    Each rule has a cheap ``has_*`` predicate so the classification order stays readable
    and individually testable.
    """
    evidence = {
        "window": window.as_dict(),
        "baseline": baseline.as_dict(),
    }

    # 1. retry_storm — the agent is hammering a failing endpoint.
    if has_retry_storm(window, retry_ratio_threshold, retry_per_request_threshold):
        detail = (
            f"重试率 {window.retry_ratio * 100:.1f}%"
            f"（{window.retries}/{window.requests} 次请求），"
            f"阈值 {retry_ratio_threshold * 100:.0f}%：上游错误触发自动重试风暴。"
        )
        return Classification("retry_storm", detail, evidence)

    # 2a. context_bloat, structural signal — the reported context window itself grew.
    #     This is the unambiguous form of the root cause, so it is evaluated before the
    #     token-ratio heuristics below.
    if has_context_window_bloat(window, baseline, context_window_threshold, context_window_growth_ratio):
        baseline_context = baseline.mean_context_window
        if baseline_context > 0:
            detail = (
                f"平均上下文窗口从 {baseline_context:,.0f} 增至 "
                f"{window.mean_context_window:,.0f} tokens"
                f"（{window.mean_context_window / baseline_context:.1f} 倍），"
                f"阈值 {context_window_growth_ratio:.1f} 倍：会话历史/文件内容持续累积。"
            )
        else:
            detail = (
                f"平均上下文窗口 {window.mean_context_window:,.0f} tokens，"
                f"阈值 {context_window_threshold:,.0f}：会话历史/文件内容持续累积。"
            )
        return Classification("context_bloat", detail, evidence)

    # 3. uncompressed_tool_output — raw tool results pushed straight into the prompt.
    #     Deliberately evaluated *before* the prompt-token-ratio form of context_bloat:
    #     a 2 MB tool payload inflates input tokens too, and the byte evidence is the
    #     specific, actionable explanation. (Documented deviation from the literal rule
    #     order in contract §8.4 — reported to the Lead.)
    if has_uncompressed_tool_output(window, baseline, tool_bytes_per_request_threshold):
        detail = (
            f"每次请求工具返回 {window.tool_bytes_per_request:,.0f} 字节"
            f"（基线 {baseline.tool_bytes_per_request:,.0f}），"
            f"阈值 {tool_bytes_per_request_threshold:,.0f}：工具返回数据未压缩即进入上下文。"
        )
        return Classification("uncompressed_tool_output", detail, evidence)

    # 2b. context_bloat, behavioural signal — prompts per request ballooned even though
    #     the reported context window did not (unbounded history, whole-file prompts).
    baseline_prompt_per_request = baseline.prompt_tokens_per_request
    prompt_ratio = (
        window.prompt_tokens_per_request / baseline_prompt_per_request
        if baseline_prompt_per_request > 0
        else 0.0
    )
    if prompt_ratio >= CONTEXT_INPUT_RATIO_THRESHOLD:
        detail = (
            f"每请求 prompt tokens 为基线的 {prompt_ratio:.1f} 倍"
            f"（{window.prompt_tokens_per_request:,.0f} vs "
            f"{baseline_prompt_per_request:,.0f}），阈值 {CONTEXT_INPUT_RATIO_THRESHOLD:.0f} 倍："
            "上下文膨胀。"
        )
        return Classification("context_bloat", detail, evidence)

    # 4. cache_miss_storm — prompt caching stopped working.
    if has_cache_miss_storm(window, baseline, cache_drop_threshold):
        baseline_cache = baseline.cache_hit_rate
        drop = (baseline_cache - window.cache_hit_rate) / baseline_cache
        detail = (
            f"缓存命中率从 {baseline_cache * 100:.1f}% 跌至 "
            f"{window.cache_hit_rate * 100:.1f}%（下降 {drop * 100:.1f}%），"
            f"阈值 {cache_drop_threshold * 100:.0f}%：缓存命中崩塌。"
        )
        return Classification("cache_miss_storm", detail, evidence)

    # 5. traffic_spike — more requests, same size per request.
    if has_traffic_spike(window, baseline, traffic_growth_threshold, flat_tolerance):
        baseline_rpm = baseline.requests_per_minute
        growth = window.requests_per_minute / baseline_rpm if baseline_rpm > 0 else 0.0
        detail = (
            f"请求速率增长 {growth:.1f} 倍而单请求 tokens 基本持平"
            f"（{window.tokens_per_request:,.0f} vs {baseline.tokens_per_request:,.0f}）：流量尖峰。"
        )
        return Classification("traffic_spike", detail, evidence)

    detail = "指标显著偏离基线，但未匹配到已知根因特征（重试/上下文/工具返回/缓存/流量）。"
    return Classification("unknown", detail, evidence)


# ---- individual root-cause predicates (each is directly unit tested) -----------------


def has_retry_storm(
    window: Evidence,
    retry_ratio_threshold: float = RETRY_RATIO_THRESHOLD,
    retry_per_request_threshold: float = RETRY_PER_REQUEST_THRESHOLD,
) -> bool:
    """``retries / requests >= 15 %`` or mean retries per request ``>= 2``."""
    if not window.requests:
        return False
    if window.retries <= 0:
        return False
    return (
        window.retry_ratio >= retry_ratio_threshold
        or window.retries_per_request >= retry_per_request_threshold
    )


def has_context_window_bloat(
    window: Evidence,
    baseline: Evidence,
    context_window_threshold: float = CONTEXT_WINDOW_THRESHOLD,
    context_window_growth_ratio: float = CONTEXT_WINDOW_GROWTH_RATIO,
) -> bool:
    """The reported ``context_window`` is both large and materially larger than baseline.

    Both halves are required whenever history exists: modern coding agents legitimately
    run at 200k-1M context, so the absolute 120k rule alone would label *every* incident
    as context bloat for those agents.
    """
    window_context = window.mean_context_window
    if window_context < context_window_threshold:
        return False
    baseline_context = baseline.mean_context_window
    if baseline_context <= 0:
        return True  # no history to compare against: trust the absolute threshold
    return window_context >= baseline_context * context_window_growth_ratio


def has_context_bloat(
    window: Evidence,
    baseline: Evidence,
    context_window_threshold: float = CONTEXT_WINDOW_THRESHOLD,
    context_window_growth_ratio: float = CONTEXT_WINDOW_GROWTH_RATIO,
    prompt_ratio: float = 0.0,
    prompt_ratio_threshold: float = CONTEXT_INPUT_RATIO_THRESHOLD,
) -> bool:
    """Either structural (context window grew) or behavioural (prompt tokens ballooned).

    ``prompt_ratio`` is the window's per-request *prompt* tokens (cached + uncached) over
    the baseline's — measuring uncached tokens alone would misread a cache miss as bloat.
    """
    if has_context_window_bloat(
        window, baseline, context_window_threshold, context_window_growth_ratio
    ):
        return True
    return prompt_ratio >= prompt_ratio_threshold


def has_uncompressed_tool_output(
    window: Evidence,
    baseline: Evidence,
    tool_bytes_per_request_threshold: float = TOOL_RESULT_BYTES_PER_REQUEST,
) -> bool:
    """``tool_result_bytes / requests >= 200 KB`` **and** tool bytes grew vs baseline.

    The growth guard is what separates this root cause from generic context bloat: a huge
    tool payload shows up as a jump in bytes, not merely as a large absolute number.
    """
    if not window.requests:
        return False
    if window.tool_bytes_per_request < tool_bytes_per_request_threshold:
        return False
    if baseline.requests and baseline.tool_bytes_per_request > 0:
        return window.tool_bytes_per_request >= 2.0 * baseline.tool_bytes_per_request
    return True


def has_cache_miss_storm(
    window: Evidence,
    baseline: Evidence,
    cache_drop_threshold: float = CACHE_HIT_DROP_THRESHOLD,
) -> bool:
    """Cache hit rate dropped by at least 40 % *relative to the baseline*."""
    baseline_rate = baseline.cache_hit_rate
    if baseline_rate <= 0:
        return False
    drop = (baseline_rate - window.cache_hit_rate) / baseline_rate
    return drop >= cache_drop_threshold


def has_traffic_spike(
    window: Evidence,
    baseline: Evidence,
    traffic_growth_threshold: float = TRAFFIC_GROWTH_THRESHOLD,
    flat_tolerance: float = TRAFFIC_FLAT_TOLERANCE,
) -> bool:
    """Request volume up ``>= 2.5x`` while per-request tokens stayed flat.

    Requests are compared as a **rate per minute**, not as a raw count: the observation
    window is a handful of minutes while the baseline is hours, so raw counts would make
    every incident look like a traffic collapse.
    """
    baseline_rpm = baseline.requests_per_minute
    if baseline_rpm <= 0:
        return False
    growth = window.requests_per_minute / baseline_rpm
    if growth < traffic_growth_threshold:
        return False
    window_tpr = window.tokens_per_request
    baseline_tpr = baseline.tokens_per_request
    if baseline_tpr > 0:
        return abs(window_tpr - baseline_tpr) <= flat_tolerance * baseline_tpr
    return window_tpr == 0


# --------------------------------------------------------------------------------------
# Detection pass
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AgentDetection:
    """Intermediate result for one agent (kept for tests and diagnostics)."""

    agent: str
    baseline: float
    sigma: float
    observed: float
    score: float
    ratio: float
    severity: str
    reason: str
    reason_label: str
    reason_detail: str
    evidence: dict[str, Any]
    tokens_burn: float
    cost_usd: float
    window_start: int
    window_end: int
    project: str = ""
    model: str = ""
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "baseline": round(self.baseline, 3),
            "sigma": round(self.sigma, 3),
            "observed": round(self.observed, 3),
            "score": round(self.score, 3),
            "ratio": round(self.ratio, 3),
            "severity": self.severity,
            "reason": self.reason,
            "reason_label": self.reason_label,
            "tokens_burn": round(self.tokens_burn, 3),
            "cost_usd": round(self.cost_usd, 6),
        }


def _top_dimension(db: Database, agent: str, start_ms: int, end_ms: int, column: str) -> str:
    """Most costly ``project``/``model`` for an agent in a window (evidence only)."""
    if column not in {"project", "model"}:
        raise ValueError(f"unsupported dimension: {column}")
    where, params = build_filters(agent=agent, start_ms=int(start_ms), end_ms=int(end_ms))
    row = db.fetch_one(
        f"SELECT s.{column} AS value, SUM(s.cost_usd) AS cost FROM usage_snapshots s{where} "
        f"GROUP BY s.{column} ORDER BY cost DESC, value ASC LIMIT 1",
        params,
    )
    return str(row["value"]) if row is not None and row["value"] else ""


def detect_agent(
    db: Database,
    agent: str,
    *,
    settings: Settings | None = None,
    now_ms: int | None = None,
    lookback_minutes: float | None = None,
    z_threshold: float | None = None,
    ratio_threshold: float | None = None,
    min_tokens_per_min: float | None = None,
) -> AgentDetection | None:
    """Run the robust z-score test for one agent; ``None`` when it is not anomalous."""
    settings = settings or get_settings()
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    lookback = float(
        lookback_minutes
        if lookback_minutes is not None
        else getattr(settings, "anomaly_lookback_minutes", 60) or 60
    )
    z_cut = float(z_threshold if z_threshold is not None else getattr(settings, "anomaly_z_threshold", 4.0))
    ratio_cut = float(
        ratio_threshold
        if ratio_threshold is not None
        else getattr(settings, "anomaly_ratio_threshold", 3.0)
    )
    min_tpm = float(
        min_tokens_per_min
        if min_tokens_per_min is not None
        else getattr(settings, "anomaly_min_tokens_per_min", 2000.0)
    )

    lookback_ms = int(lookback * 60_000)
    obs_start = now - lookback_ms
    obs_end = now
    history_start = now - HISTORY_MULTIPLIER * lookback_ms
    history_end = obs_start

    series = minute_series(db, agent, min(history_start, obs_start), obs_end)
    observed_values = series_values(series, obs_start, obs_end)
    if not observed_values:
        return None

    # A gap-filled window is right for plotting but wrong for statistics: minutes the
    # agent never reported are "no measurement", not a measured zero. Feeding those zeros
    # into the median collapses the baseline to 0, which silently disables the
    # ``observed >= ratio * baseline`` guard and makes every fresh install fire a burst of
    # false critical alerts. So: statistics over *active* minutes only, and only trust the
    # history branch when there are enough of them to be a real baseline.
    history_buckets = [
        bucket
        for minute, bucket in series.items()
        if history_start <= minute < history_end and bucket.total_tokens > 0
    ]
    observed_buckets_all = [
        bucket for minute, bucket in series.items() if obs_start <= minute < obs_end
    ]

    if len(history_buckets) >= MIN_HISTORY_MINUTES:
        history_values = [bucket.tpm for bucket in history_buckets]
        baseline = median(history_values)
        sigma = robust_sigma(history_values)
        baseline_source = "history"
    else:
        # Not enough history (fresh install / new agent / thin data). Fall back to the
        # calmest part of the observation window so a first incident can still be caught.
        # Only *active* minutes are used: gap-filled zeros would collapse the baseline to
        # zero and silently disable the ratio guard (the alert-fatigue failure mode).
        active_observed = [bucket for bucket in observed_buckets_all if bucket.total_tokens > 0]
        warmup = [bucket.tpm for bucket in active_observed[:FALLBACK_BASELINE_MINUTES]]
        baseline = median(warmup)
        sigma = robust_sigma(warmup)
        baseline_source = "warmup"
        history_buckets = []

    # A zero baseline makes the ratio guard vacuous (`observed >= 3 * 0` is always true),
    # so an unmeasurable baseline is reported as "not anomalous" rather than guessed at.
    # An agent whose entire visible history *is* the burst cannot be distinguished from
    # one that simply started working at that rate.
    if baseline <= 0:
        logger.debug(
            "agent %s has no measurable baseline (observed sample=%s); skipping",
            agent,
            observed_values[:3],
        )
        return None

    observed = percentile(observed_values, OBSERVED_PERCENTILE)
    score = robust_score(observed, baseline, sigma)
    raw_ratio = observed / baseline
    # Clamp to a finite value: JSON has no infinity and persisting 0.0 would read as
    # "observed is zero" — the exact opposite of an unbounded spike.
    ratio = min(MAX_RATIO, raw_ratio)

    if not (score >= z_cut and observed >= ratio_cut * baseline and observed >= min_tpm):
        return None

    offending = observed_buckets(series, obs_start, obs_end)
    window_evidence = Evidence.from_buckets(offending)
    baseline_evidence = Evidence.from_buckets(history_buckets)
    if baseline_evidence.snapshots == 0:
        # Warmup fallback: compare against the non-offending part of the observation window.
        offending_minutes = {bucket.minute_ms for bucket in offending}
        calm = [
            bucket
            for minute, bucket in series.items()
            if obs_start <= minute < obs_end and minute not in offending_minutes
        ]
        baseline_evidence = Evidence.from_buckets(calm)

    classification = classify_root_cause(window_evidence, baseline_evidence)
    severity = severity_for(score, ratio)

    window_start = offending[0].minute_ms if offending else timeutil.bucket_start(now, "minute")
    window_end = (offending[-1].minute_ms + 60_000) if offending else now
    fingerprint = fingerprint_for(agent, classification.reason, window_start)

    evidence = {
        "baseline_source": baseline_source,
        "baseline_median": round(baseline, 3),
        "sigma": round(sigma, 3),
        "sigma_floor": ANOMALY_SIGMA_FLOOR,
        "observed_p95": round(observed, 3),
        "score": round(score, 3),
        "ratio": round(ratio, 3),
        "z_threshold": z_cut,
        "ratio_threshold": ratio_cut,
        "min_tokens_per_min": min_tpm,
        "lookback_minutes": lookback,
        "observed_minutes": len(observed_values),
        "history_minutes": len(history_buckets),
        "history_min_minutes": MIN_HISTORY_MINUTES,
        "offending_minutes": len(offending),
        "offending_minute_starts": [timeutil.iso(b.minute_ms) for b in offending[:20]],
        "total_tokens": window_evidence.total_tokens,
        "cost_usd": round(window_evidence.cost_usd, 6),
        "classification": classification.evidence,
        "reason_detail": classification.detail,
    }

    return AgentDetection(
        agent=agent,
        baseline=baseline,
        sigma=sigma,
        observed=observed,
        score=score,
        ratio=ratio,
        severity=severity,
        reason=classification.reason,
        reason_label=classification.label,
        reason_detail=classification.detail,
        evidence=evidence,
        tokens_burn=float(window_evidence.total_tokens),
        cost_usd=round(window_evidence.cost_usd, 6),
        window_start=window_start,
        window_end=window_end,
        project=_top_dimension(db, agent, window_start, window_end, "project"),
        model=_top_dimension(db, agent, window_start, window_end, "model"),
        fingerprint=fingerprint,
    )


def list_agents_in_window(db: Database, start_ms: int, end_ms: int) -> list[str]:
    """Distinct agents that reported usage inside ``[start_ms, end_ms)``."""
    rows = db.fetch_all(
        "SELECT DISTINCT agent FROM usage_snapshots WHERE ts >= ? AND ts < ? ORDER BY agent",
        (int(start_ms), int(end_ms)),
    )
    return [str(row["agent"]) for row in rows if row["agent"]]


def _insert_anomaly(db: Database, detection: AgentDetection, detected_at: int) -> int:
    """``INSERT OR IGNORE`` one event; returns the new id (0 when deduped)."""
    with db.cursor(write=True) as cur:
        cur.execute(
            """
            INSERT OR IGNORE INTO anomaly_events (
                detected_at, agent, project, model, window_start, window_end, metric,
                observed, baseline, score, ratio, severity, reason, reason_label,
                reason_detail, evidence, tokens_burn, cost_usd, status, fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(detected_at),
                detection.agent,
                detection.project,
                detection.model,
                int(detection.window_start),
                int(detection.window_end),
                METRIC,
                round(detection.observed, 3),
                round(detection.baseline, 3),
                round(detection.score, 3),
                round(min(MAX_RATIO, detection.ratio), 3),
                detection.severity,
                detection.reason,
                detection.reason_label,
                detection.reason_detail,
                dump_json(detection.evidence),
                round(detection.tokens_burn, 3),
                round(detection.cost_usd, 6),
                "open",
                detection.fingerprint,
            ),
        )
        return int(cur.lastrowid or 0) if cur.rowcount else 0


def row_to_anomaly(row: Any) -> AnomalyRecord:
    """Convert an ``anomaly_events`` row into the API model."""
    evidence = load_json(row["evidence"], default={})
    if not isinstance(evidence, dict):
        evidence = {}
    return AnomalyRecord(
        id=int(row["id"]),
        detected_at=timeutil.ms_to_dt(int(row["detected_at"] or 0)),
        agent=row["agent"] or "",
        project=row["project"] or "",
        model=row["model"] or "",
        window_start=timeutil.ms_to_dt(int(row["window_start"] or 0)),
        window_end=timeutil.ms_to_dt(int(row["window_end"] or 0)),
        metric=row["metric"] or METRIC,
        observed=float(row["observed"] or 0.0),
        baseline=float(row["baseline"] or 0.0),
        score=float(row["score"] or 0.0),
        ratio=float(row["ratio"] or 0.0),
        severity=row["severity"] or "low",
        reason=row["reason"] or "unknown",
        reason_label=row["reason_label"] or REASON_LABELS["unknown"],
        reason_detail=row["reason_detail"] or "",
        evidence=evidence,
        tokens_burn=float(row["tokens_burn"] or 0.0),
        cost_usd=float(row["cost_usd"] or 0.0),
        status=row["status"] or "open",
        fingerprint=row["fingerprint"] or "",
    )


def get_anomaly(db: Database, anomaly_id: int) -> AnomalyRecord | None:
    row = db.fetch_one("SELECT * FROM anomaly_events WHERE id = ?", (int(anomaly_id),))
    return row_to_anomaly(row) if row is not None else None


def set_anomaly_status(db: Database, anomaly_id: int, status: str) -> AnomalyRecord | None:
    """PATCH an anomaly's triage status; ``None`` when the id does not exist."""
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    if get_anomaly(db, anomaly_id) is None:
        return None
    db.execute("UPDATE anomaly_events SET status = ? WHERE id = ?", (status, int(anomaly_id)))
    return get_anomaly(db, anomaly_id)


def detect(
    db: Database,
    *,
    settings: Settings | None = None,
    lookback_minutes: float | None = None,
    now_ms: int | None = None,
    persist: bool = True,
) -> DetectResponse:
    """Run one detection pass over every agent active in the trailing window.

    ``persist=False`` is the dry-run mode used by tests and by ``tokenlens detect --dry-run``.
    """
    settings = settings or get_settings()
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    lookback = float(
        lookback_minutes
        if lookback_minutes is not None
        else getattr(settings, "anomaly_lookback_minutes", 60) or 60
    )
    start_ms = now - int(lookback * 60_000)
    agents = list_agents_in_window(db, start_ms, now)

    detected: list[AnomalyRecord] = []
    for agent in agents:
        detection = detect_agent(
            db, agent, settings=settings, now_ms=now, lookback_minutes=lookback
        )
        if detection is None:
            continue
        if not persist:
            detected.append(
                AnomalyRecord(
                    id=0,
                    detected_at=timeutil.ms_to_dt(now),
                    agent=detection.agent,
                    project=detection.project,
                    model=detection.model,
                    window_start=timeutil.ms_to_dt(detection.window_start),
                    window_end=timeutil.ms_to_dt(detection.window_end),
                    metric=METRIC,
                    observed=round(detection.observed, 3),
                    baseline=round(detection.baseline, 3),
                    score=round(detection.score, 3),
                    ratio=round(min(MAX_RATIO, detection.ratio), 3),
                    severity=detection.severity,
                    reason=detection.reason,
                    reason_label=detection.reason_label,
                    reason_detail=detection.reason_detail,
                    evidence=detection.evidence,
                    tokens_burn=round(detection.tokens_burn, 3),
                    cost_usd=round(detection.cost_usd, 6),
                    status="open",
                    fingerprint=detection.fingerprint,
                )
            )
            continue

        anomaly_id = _insert_anomaly(db, detection, now)
        if not anomaly_id:
            logger.debug(
                "anomaly deduped (fingerprint %s) for agent %s", detection.fingerprint, agent
            )
            continue
        record = get_anomaly(db, anomaly_id)
        if record is not None:
            detected.append(record)

    return DetectResponse(
        scanned_agents=len(agents),
        detected=detected,
        detected_at=timeutil.ms_to_dt(now),
        lookback_minutes=lookback,
    )


# --------------------------------------------------------------------------------------
# History queries
# --------------------------------------------------------------------------------------


def _anomaly_where(
    *,
    status: str | None = None,
    severity: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    since_ms: int | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("status", status),
        ("severity", severity),
        ("agent", agent),
        ("project", project),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    if since_ms is not None:
        clauses.append("detected_at >= ?")
        params.append(int(since_ms))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_anomalies(
    db: Database,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    severity: str | None = None,
    agent: str | None = None,
    project: str | None = None,
) -> tuple[int, list[AnomalyRecord]]:
    """Paginated anomaly history, newest first (``GET /api/v1/anomalies``)."""
    where, params = _anomaly_where(
        status=status, severity=severity, agent=agent, project=project
    )
    total = int(db.scalar(f"SELECT COUNT(*) FROM anomaly_events{where}", params, default=0) or 0)
    rows = db.fetch_all(
        f"SELECT * FROM anomaly_events{where} ORDER BY detected_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)],
    )
    return total, [row_to_anomaly(row) for row in rows]


def anomaly_summary(
    db: Database,
    *,
    now_ms: int | None = None,
    since_ms: int | None = None,
) -> AnomalySummary:
    """Aggregate anomaly statistics (``GET /api/v1/anomalies/summary``).

    ``estimated_waste_usd`` is the summed cost of events still in ``open`` or
    ``acknowledged`` state — resolved incidents are excluded because their spend was
    already understood and written off.
    """
    now = int(now_ms if now_ms is not None else timeutil.now_ms())
    where, params = _anomaly_where(since_ms=since_ms)

    total_events = int(
        db.scalar(f"SELECT COUNT(*) FROM anomaly_events{where}", params, default=0) or 0
    )
    open_events = int(
        db.scalar(
            f"SELECT COUNT(*) FROM anomaly_events{where + (' AND ' if where else ' WHERE ')}"
            "status IN ('open','acknowledged')",
            params,
            default=0,
        )
        or 0
    )

    by_reason = {reason: 0 for reason in REASON_ORDER}
    for row in db.fetch_all(
        f"SELECT reason, COUNT(*) AS n FROM anomaly_events{where} GROUP BY reason", params
    ):
        by_reason[str(row["reason"] or "unknown")] = int(row["n"])

    by_severity = {name: 0 for name in ("low", "medium", "high", "critical")}
    for row in db.fetch_all(
        f"SELECT severity, COUNT(*) AS n FROM anomaly_events{where} GROUP BY severity", params
    ):
        by_severity[str(row["severity"] or "low")] = int(row["n"])

    waste = db.scalar(
        f"SELECT COALESCE(SUM(cost_usd), 0.0) FROM anomaly_events{where + (' AND ' if where else ' WHERE ')}"
        "status IN ('open','acknowledged')",
        params,
        default=0.0,
    )
    last_detected = db.scalar(
        f"SELECT MAX(detected_at) FROM anomaly_events{where}", params, default=None
    )
    first_detected = db.scalar(
        f"SELECT MIN(detected_at) FROM anomaly_events{where}", params, default=None
    )

    return AnomalySummary(
        total_events=total_events,
        open_events=open_events,
        by_reason=by_reason,
        by_severity=by_severity,
        window=Window(
            **{
                "from": timeutil.ms_to_dt(int(first_detected)) if first_detected else timeutil.ms_to_dt(now),
                "to": timeutil.ms_to_dt(now),
            }
        ),
        last_detected_at=timeutil.ms_to_dt(int(last_detected)) if last_detected else None,
        estimated_waste_usd=round(float(waste or 0.0), 6),
    )


def detect_enabled(settings: Settings | None = None) -> bool:
    """Whether the scheduler should run detection passes."""
    settings = settings or get_settings()
    return bool(getattr(settings, "anomaly_enabled", True))
