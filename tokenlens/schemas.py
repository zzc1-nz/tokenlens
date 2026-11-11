"""Pydantic v2 models shared by the ingest, query, budget, anomaly and dashboard APIs.

Naming conventions used throughout:

* ``*In``      — request bodies accepted from clients
* ``*Out``     — response objects serialised to JSON
* ``Totals``   — the canonical token/cost aggregate; every aggregation endpoint reuses it

All datetimes are serialised as ISO-8601 UTC strings ending in ``Z``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from . import timeutil

# --------------------------------------------------------------------------------------
# Serialisation base
# --------------------------------------------------------------------------------------


class ApiModel(BaseModel):
    """Base model that renders ``datetime`` fields as ``...Z`` UTC strings."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @field_serializer("*", when_used="json")
    def _serialize_datetimes(self, value: Any) -> Any:  # pragma: no cover - passthrough
        return value


def _to_utc_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timeutil.UTC)
    return value.astimezone(timeutil.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc(dt: datetime | None) -> datetime | None:
    """Normalise a datetime to UTC (naive values are assumed to already be UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timeutil.UTC)
    return dt.astimezone(timeutil.UTC)


class TimeSeriesBase(ApiModel):
    """Model whose datetime fields serialise as ``Z`` suffixed UTC strings."""

    @model_validator(mode="after")
    def _normalise_datetimes(self):
        for name in self.__class__.model_fields:
            value = getattr(self, name, None)
            if isinstance(value, datetime):
                object.__setattr__(self, name, _utc(value))
        return self

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: D102
        data = super().model_dump(*args, **kwargs)
        return data

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:  # noqa: D102
        return super().model_dump_json(*args, **kwargs)


class UtcModel(ApiModel):
    """Any model that contains datetimes inherits from this to get ``Z`` serialisation."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # noqa: D102
        kwargs.setdefault("mode", "python")
        return _convert_datetimes(super().model_dump(**kwargs))

    def model_dump_json(self, **kwargs: Any) -> str:  # noqa: D102
        import json

        return json.dumps(self.model_dump(), default=str)


def _convert_datetimes(value: Any) -> Any:
    """Recursively convert datetimes in a dumped structure into ``Z`` ISO strings."""
    if isinstance(value, datetime):
        return _to_utc_z(value)
    if isinstance(value, dict):
        return {k: _convert_datetimes(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert_datetimes(v) for v in value]
    return value


# --------------------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------------------

RUNTIMES = (
    "openai",
    "anthropic",
    "claude",
    "ollama",
    "local",
    "deepseek",
    "google",
    "gemini",
    "xai",
    "mistral",
    "cursor",
    "custom",
)


class SnapshotIn(UtcModel):
    """One token usage report produced by an agent runtime.

    A snapshot is a *delta*: the tokens consumed since the previous report for the same
    session. The ingest path is idempotent on ``event_id`` so a retried HTTP request
    never double counts.
    """

    event_id: str | None = Field(default=None, max_length=128)
    ts: Any = Field(default=None, description="ISO-8601 or epoch ms; defaults to server now")
    agent: str = Field(min_length=1, max_length=128)
    agent_version: str | None = Field(default=None, max_length=64)
    runtime: str = Field(default="custom", max_length=32)
    project: str = Field(default="default", max_length=200)
    session_id: str = Field(default="unknown", max_length=200)
    model: str = Field(default="unknown", max_length=200)
    input_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    output_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    cache_read_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    cache_write_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    reasoning_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    requests: int = Field(default=1, ge=0, le=1_000_000)
    retries: int = Field(default=0, ge=0, le=1_000_000)
    tool_calls: int = Field(default=0, ge=0, le=1_000_000)
    latency_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    context_window: int | None = Field(default=None, ge=0, le=100_000_000)
    tool_result_bytes: int = Field(default=0, ge=0, le=10_000_000_000)
    tags: list[str] | dict[str, Any] | None = None
    meta: dict[str, Any] | None = None

    @field_validator("ts", mode="before")
    @classmethod
    def _validate_ts(cls, value: Any) -> Any:
        if value is None:
            return None
        # Parse eagerly so a bad timestamp is a 422 rather than a 500.
        return timeutil.parse_time_ms(value)

    @field_validator("runtime")
    @classmethod
    def _normalise_runtime(cls, value: str) -> str:
        return (value or "custom").strip().lower() or "custom"

    @field_validator("agent", "project", "session_id", "model")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = (value or "").strip()
        return cleaned or "unknown"

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.reasoning_tokens
        )


class IngestRequest(UtcModel):
    """Batch envelope. ``snapshots`` accepts 1..``max_ingest_batch`` items."""

    snapshots: list[SnapshotIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch(self):
        if not self.snapshots:
            raise ValueError("snapshots must not be empty")
        return self


class IngestError(UtcModel):
    index: int
    event_id: str | None = None
    error: str


class IngestResponse(UtcModel):
    accepted: int
    duplicates: int
    rejected: int
    errors: list[IngestError] = Field(default_factory=list)
    total_tokens: int
    cost_usd: float
    ingest_ms: float
    price_version: str
    unknown_models: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------------------


class Totals(UtcModel):
    """The canonical aggregate. Every aggregation endpoint returns this shape."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    billable_tokens: int = 0
    requests: int = 0
    retries: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    sessions: int = 0
    agents: int = 0
    projects: int = 0
    cache_hit_rate: float = 0.0
    avg_cost_per_request: float = 0.0
    avg_tokens_per_request: float = 0.0

    @classmethod
    def zero(cls) -> "Totals":
        return cls()

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Totals":
        """Build totals from a raw SQL aggregate row (keys may be missing)."""
        get = lambda k: row.get(k) or 0  # noqa: E731
        input_tokens = int(get("input_tokens"))
        output_tokens = int(get("output_tokens"))
        cache_read = int(get("cache_read_tokens"))
        cache_write = int(get("cache_write_tokens"))
        reasoning = int(get("reasoning_tokens"))
        requests = int(get("requests"))
        total_tokens = int(
            get("total_tokens")
            or (input_tokens + output_tokens + cache_read + cache_write + reasoning)
        )
        cost = round(float(get("cost_usd")), 6)
        prompt_denominator = cache_read + input_tokens
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
            total_tokens=total_tokens,
            billable_tokens=int(
                get("billable_tokens") or (input_tokens + output_tokens + reasoning)
            ),
            requests=requests,
            retries=int(get("retries")),
            tool_calls=int(get("tool_calls")),
            cost_usd=cost,
            sessions=int(get("sessions")),
            agents=int(get("agents")),
            projects=int(get("projects")),
            cache_hit_rate=round(cache_read / prompt_denominator, 6) if prompt_denominator else 0.0,
            avg_cost_per_request=round(cost / requests, 6) if requests else 0.0,
            avg_tokens_per_request=round(total_tokens / requests, 2) if requests else 0.0,
        )

    @classmethod
    def combine(cls, items: list["Totals"]) -> "Totals":
        """Sum a list of totals, recomputing derived ratios."""
        if not items:
            return cls.zero()
        summed = {
            "input_tokens": sum(i.input_tokens for i in items),
            "output_tokens": sum(i.output_tokens for i in items),
            "cache_read_tokens": sum(i.cache_read_tokens for i in items),
            "cache_write_tokens": sum(i.cache_write_tokens for i in items),
            "reasoning_tokens": sum(i.reasoning_tokens for i in items),
            "total_tokens": sum(i.total_tokens for i in items),
            "billable_tokens": sum(i.billable_tokens for i in items),
            "requests": sum(i.requests for i in items),
            "retries": sum(i.retries for i in items),
            "tool_calls": sum(i.tool_calls for i in items),
            "cost_usd": round(sum(i.cost_usd for i in items), 6),
            "sessions": sum(i.sessions for i in items),
            "agents": sum(i.agents for i in items),
            "projects": sum(i.projects for i in items),
        }
        return cls.from_mapping(summed)

    def add(self, other: "Totals") -> "Totals":
        return Totals.combine([self, other])


class Window(UtcModel):
    start: datetime = Field(alias="from")
    end: datetime = Field(alias="to")

    model_config = ConfigDict(populate_by_name=True, extra="ignore", serialize_by_alias=True)


class TimePoint(UtcModel):
    bucket_start: datetime
    bucket_end: datetime
    label: str
    totals: Totals


class BreakdownRow(UtcModel):
    key: str
    label: str
    totals: Totals
    share_pct: float = 0.0


class UsageSummary(UtcModel):
    window: Window
    totals: Totals
    filters: dict[str, Any] = Field(default_factory=dict)


class BreakdownResponse(UtcModel):
    dimension: str
    window: Window
    rows: list[BreakdownRow] = Field(default_factory=list)
    total: Totals = Field(default_factory=Totals.zero)


class TimeSeriesResponse(UtcModel):
    bucket: str
    window: Window
    points: list[TimePoint] = Field(default_factory=list)


class TrendComparison(UtcModel):
    previous_cost_usd: float = 0.0
    current_cost_usd: float = 0.0
    delta_usd: float = 0.0
    delta_pct: float = 0.0
    previous_tokens: int = 0
    current_tokens: int = 0
    token_delta_pct: float = 0.0


class TrendsResponse(UtcModel):
    period: str
    window: Window
    points: list[TimePoint] = Field(default_factory=list)
    comparison: TrendComparison = Field(default_factory=TrendComparison)
    filters: dict[str, Any] = Field(default_factory=dict)


class LiveTopEntry(UtcModel):
    project: str | None = None
    agent: str | None = None
    cost_usd: float = 0.0
    tokens_per_min: float = 0.0


class LiveResponse(UtcModel):
    window_minutes: float
    tokens_per_min: float
    cost_per_hour: float
    active_agents: int
    active_projects: int
    active_sessions: int
    top_projects: list[LiveTopEntry] = Field(default_factory=list)
    top_agents: list[LiveTopEntry] = Field(default_factory=list)
    generated_at: datetime | None = None


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------


class ModelInfo(UtcModel):
    model: str
    provider: str
    display_name: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0
    context_window: int = 200_000
    known: bool = True


class CatalogModelsResponse(UtcModel):
    price_version: str
    count: int
    models: list[ModelInfo]


class AgentInfo(UtcModel):
    id: str
    display_name: str
    runtime: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    snapshot_count: int = 0
    cost_usd: float = 0.0
    total_tokens: int = 0


class ProjectInfo(UtcModel):
    id: str
    display_name: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    snapshot_count: int = 0
    cost_usd: float = 0.0
    total_tokens: int = 0


class SessionInfo(UtcModel):
    session_id: str
    project: str
    agent: str
    started_at: datetime | None = None
    last_seen: datetime | None = None
    snapshots: int = 0
    cost_usd: float = 0.0
    total_tokens: int = 0
    models: list[str] = Field(default_factory=list)


class CatalogListResponse(UtcModel):
    total: int
    items: list[dict[str, Any]]


# --------------------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------------------

BudgetScope = Literal["global", "project", "agent", "model"]
BudgetPeriod = Literal["daily", "weekly", "monthly"]


class BudgetIn(UtcModel):
    name: str = Field(min_length=1, max_length=120)
    scope: BudgetScope = "global"
    scope_key: str = Field(default="*", max_length=200)
    period: BudgetPeriod = "daily"
    limit_usd: float = Field(gt=0, le=10_000_000)
    thresholds: list[float] = Field(default_factory=lambda: [0.5, 0.8, 1.0])
    webhook_url: str | None = Field(default=None, max_length=2048)
    desktop: bool = True
    enabled: bool = True

    @field_validator("thresholds")
    @classmethod
    def _validate_thresholds(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("thresholds must not be empty")
        if len(value) > 6:
            raise ValueError("at most 6 thresholds are allowed")
        cleaned = sorted({round(float(v), 6) for v in value})
        if any(v <= 0 for v in cleaned):
            raise ValueError("thresholds must be > 0")
        if any(v > 5 for v in cleaned):
            raise ValueError("thresholds must be <= 5 (500%)")
        return cleaned

    @model_validator(mode="after")
    def _validate_scope(self):
        if self.scope == "global":
            self.scope_key = "*"
        elif not self.scope_key or self.scope_key == "*":
            raise ValueError("scope_key is required for non-global budgets")
        if self.webhook_url and not str(self.webhook_url).startswith(("http://", "https://")):
            raise ValueError("webhook_url must be an http(s) URL")
        return self


class BudgetPatch(UtcModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    scope: BudgetScope | None = None
    scope_key: str | None = Field(default=None, max_length=200)
    period: BudgetPeriod | None = None
    limit_usd: float | None = Field(default=None, gt=0, le=10_000_000)
    thresholds: list[float] | None = None
    webhook_url: str | None = Field(default=None, max_length=2048)
    desktop: bool | None = None
    enabled: bool | None = None

    @field_validator("thresholds")
    @classmethod
    def _validate_thresholds(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        return BudgetIn._validate_thresholds(value)


class BudgetOut(UtcModel):
    id: int
    name: str
    scope: str
    scope_key: str
    period: str
    limit_usd: float
    thresholds: list[float]
    webhook_url: str | None = None
    desktop: bool
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BudgetStatus(UtcModel):
    budget: BudgetOut
    window_start: datetime
    window_end: datetime
    spent_usd: float = 0.0
    limit_usd: float = 0.0
    pct: float = 0.0
    remaining_usd: float = 0.0
    projected_usd: float = 0.0
    projected_pct: float = 0.0
    status: str = "ok"
    triggered_thresholds: list[float] = Field(default_factory=list)
    trigger_count: int = 0
    overspend_usd: float = 0.0
    last_alert_at: datetime | None = None


class BudgetListResponse(UtcModel):
    items: list[BudgetStatus] = Field(default_factory=list)


class AlertRecord(UtcModel):
    id: int
    budget_id: int | None = None
    budget_name: str = ""
    scope: str = "global"
    scope_key: str = "*"
    period: str = "daily"
    window_start: datetime
    window_end: datetime
    threshold: float = 1.0
    severity: str = "warning"
    spent_usd: float = 0.0
    limit_usd: float = 0.0
    overspend_usd: float = 0.0
    trigger_count: int = 1
    message: str = ""
    channels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class AlertListResponse(UtcModel):
    total: int = 0
    items: list[AlertRecord] = Field(default_factory=list)


class AlertSummary(UtcModel):
    total_alerts: int = 0
    total_triggers: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_budget: list[dict[str, Any]] = Field(default_factory=list)
    total_overspend_usd: float = 0.0
    window: Window | None = None
    last_triggered_at: datetime | None = None


class EvaluateResponse(UtcModel):
    evaluated: int = 0
    triggered: list[AlertRecord] = Field(default_factory=list)
    evaluated_at: datetime | None = None
    dry_run: bool = False


# --------------------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------------------


class AnomalyRecord(UtcModel):
    id: int
    detected_at: datetime
    agent: str = ""
    project: str = ""
    model: str = ""
    window_start: datetime
    window_end: datetime
    metric: str = "tokens_per_min"
    observed: float = 0.0
    baseline: float = 0.0
    score: float = 0.0
    ratio: float = 0.0
    severity: str = "low"
    reason: str = "unknown"
    reason_label: str = "未知"
    reason_detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    tokens_burn: float = 0.0
    cost_usd: float = 0.0
    status: str = "open"
    fingerprint: str = ""


class AnomalyListResponse(UtcModel):
    total: int = 0
    items: list[AnomalyRecord] = Field(default_factory=list)


class AnomalySummary(UtcModel):
    total_events: int = 0
    open_events: int = 0
    by_reason: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    window: Window | None = None
    last_detected_at: datetime | None = None
    estimated_waste_usd: float = 0.0


class DetectResponse(UtcModel):
    scanned_agents: int = 0
    detected: list[AnomalyRecord] = Field(default_factory=list)
    detected_at: datetime | None = None
    lookback_minutes: float = 60.0


class AnomalyPatch(UtcModel):
    status: Literal["open", "acknowledged", "resolved"]


# --------------------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------------------


class DashboardKpis(UtcModel):
    today_cost_usd: float = 0.0
    today_tokens: int = 0
    week_cost_usd: float = 0.0
    month_cost_usd: float = 0.0
    burn_rate_tokens_per_min: float = 0.0
    burn_rate_usd_per_hour: float = 0.0
    cache_hit_rate: float = 0.0
    active_agents: int = 0
    active_projects: int = 0
    total_sessions: int = 0


class DashboardOverview(UtcModel):
    generated_at: datetime | None = None
    kpis: DashboardKpis = Field(default_factory=DashboardKpis)
    breakdowns: dict[str, list[BreakdownRow]] = Field(default_factory=dict)
    timeseries: dict[str, list[TimePoint]] = Field(default_factory=dict)
    budgets: list[BudgetStatus] = Field(default_factory=list)
    alerts: dict[str, Any] = Field(default_factory=dict)
    anomalies: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Health / meta
# --------------------------------------------------------------------------------------


class HealthResponse(UtcModel):
    status: str = "ok"
    version: str
    db: str
    snapshots: int = 0
    price_version: str
    uptime_seconds: float = 0.0
    now: datetime | None = None


class MetaResponse(UtcModel):
    name: str = "TokenLens"
    version: str
    price_version: str
    models: int
    endpoints: list[str] = Field(default_factory=list)
    runtimes: list[str] = Field(default_factory=lambda: list(RUNTIMES))
