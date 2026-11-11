"""Pure rendering for the TokenLens HUD.

Everything in this module is a *pure function* of its inputs: no sockets, no clocks, no
terminal state. That is what lets the test suite assert on exact bar widths, KPI
formatting and offline banners without spinning up a server or a pty.

Two renderers share one view model (:class:`HudSnapshot`):

``render_plain``
    Deterministic, colourless text. Used for non-TTY invocations (CI, pipes, cron),
    ``--no-color``, and as the fallback when ``rich`` cannot detect a terminal.
``render_rich``
    A ``rich`` renderable tree (panels, grids, coloured bars) used by the live loop.

Layout of the HUD, top to bottom:

1. header — title, server, dimension, timestamp, OFFLINE banner if applicable;
2. KPI strip — today / week / month cost, burn rate, cache hit rate, active counts;
3. budget bars — ``█``/``░`` against each budget's own limit, colour coded by status;
4. dimension bars — ``█``/``░`` scaled to the largest row, sorted by cost descending;
5. footer — key bindings.

Bar maths
---------
Bars are scaled **relative to the maximum row** so the largest row always fills the bar;
the numeric columns next to the bar carry the absolute truth. A non-zero value always
renders at least one filled cell, otherwise a tiny-but-real cost would look like zero,
which is precisely the mistake a cost tool must not make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tokenlens import timeutil

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

FILLED = "█"
EMPTY = "░"
SPARK_CHARS = "▁▂▃▄▅▆▇█"

DIMENSIONS: tuple[str, ...] = ("project", "agent", "model", "budget")
DIMENSION_LABELS = {
    "project": "PROJECT",
    "agent": "AGENT",
    "model": "MODEL",
    "budget": "BUDGET",
}
#: Live-view key bindings (``q`` is handled by the loop, not a dimension).
KEY_BINDINGS: dict[str, str] = {
    "p": "project",
    "a": "agent",
    "m": "model",
    "b": "budget",
}
DIMENSION_ORDER: tuple[str, ...] = ("project", "agent", "model", "budget")

STATUS_STYLES = {
    "ok": "green",
    "warning": "yellow",
    "critical": "red",
    "exceeded": "bold red",
}
SEVERITY_STYLES = {
    "low": "cyan",
    "medium": "yellow",
    "high": "red",
    "critical": "bold red",
    "warning": "yellow",
    "exceeded": "bold red",
}

DEFAULT_BAR_WIDTH = 28
DEFAULT_WIDTH = 100


# --------------------------------------------------------------------------------------
# Number formatting
# --------------------------------------------------------------------------------------


def fmt_cost(value: float | int | None) -> str:
    """Format USD with a precision that stays readable from micro-spend to budgets."""
    amount = float(value or 0.0)
    magnitude = abs(amount)
    if magnitude >= 1000:
        return f"${amount:,.2f}"
    if magnitude >= 1:
        return f"${amount:.2f}"
    if magnitude >= 0.01:
        return f"${amount:.3f}"
    return f"${amount:.4f}"


def fmt_tokens(value: float | int | None) -> str:
    """Compact token count: ``999`` / ``12.3k`` / ``4.56M`` / ``1.20B``."""
    amount = float(value or 0.0)
    magnitude = abs(amount)
    if magnitude >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f}B"
    if magnitude >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{amount / 1_000:.1f}k"
    return f"{int(amount)}"


def fmt_int(value: float | int | None) -> str:
    """Thousands-separated integer (``1,234,567``)."""
    return f"{int(value or 0):,}"


def fmt_pct(fraction: float | None, *, digits: int = 1) -> str:
    """Render a 0..1 fraction as a percentage string (``62.5%``)."""
    return f"{float(fraction or 0.0) * 100:.{digits}f}%"


def fmt_rate(tokens_per_min: float | int | None) -> str:
    """Burn rate in tokens/minute, compacted (``12.3k/min``)."""
    return f"{fmt_tokens(tokens_per_min)}/min"


def fmt_usd_rate(usd_per_hour: float | int | None) -> str:
    """Hourly spend rate, e.g. ``$7.38/h``."""
    return f"{fmt_cost(usd_per_hour)}/h"


def fmt_duration_ms(ms: float | int | None) -> str:
    """Compact duration, delegating to the shared helper so CLI and server agree."""
    return timeutil.humanize_duration(int(ms or 0))


def fmt_timestamp(ms: int | float | None, *, with_seconds: bool = True) -> str:
    """UTC timestamp as ``YYYY-MM-DD HH:MM:SS`` (never raises)."""
    if not ms:
        return "-"
    dt = timeutil.ms_to_dt(ms)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def fmt_clock(ms: int | float | None) -> str:
    """Wall clock ``HH:MM:SS`` in UTC — the HUD footer/updated marker."""
    if not ms:
        return "--:--:--"
    return timeutil.ms_to_dt(ms).strftime("%H:%M:%S")


# --------------------------------------------------------------------------------------
# Bars
# --------------------------------------------------------------------------------------


def bar_cells(value: float, maximum: float, width: int) -> tuple[int, int]:
    """Return ``(filled, empty)`` cell counts for a bar.

    ``maximum`` is the scale reference (the largest row). Non-positive or non-finite
    input degrades to an empty bar rather than raising: a monitoring UI must render
    *something* even when the data is nonsense.
    """
    width = max(0, int(width))
    if width == 0:
        return 0, 0
    try:
        value = float(value)
        maximum = float(maximum)
    except (TypeError, ValueError):
        return 0, width
    if maximum <= 0 or value <= 0 or value != value:  # NaN check
        return 0, width
    fraction = value / maximum
    if fraction != fraction or fraction == float("inf"):
        return width, 0
    fraction = min(1.0, max(0.0, fraction))
    filled = int(fraction * width + 0.5)
    if filled == 0:
        filled = 1  # never hide a non-zero value
    return min(width, filled), width - min(width, filled)


def make_bar(
    value: float,
    maximum: float,
    width: int = DEFAULT_BAR_WIDTH,
    *,
    filled_char: str = FILLED,
    empty_char: str = EMPTY,
) -> str:
    """Render a horizontal ``█``/``░`` bar scaled to ``maximum``."""
    filled, empty = bar_cells(value, maximum, width)
    return filled_char * filled + empty_char * empty


def budget_bar(pct: float, width: int = DEFAULT_BAR_WIDTH) -> str:
    """Render a budget utilisation bar where ``pct`` is a 0..1+ fraction of the limit.

    Values above 100% clamp to a full bar — the numeric ``pct`` label carries the
    overshoot, the bar only communicates "at/over limit".
    """
    return make_bar(min(float(pct or 0.0), 1.0), 1.0, width)


def sparkline(values: Sequence[float], *, width: int | None = None) -> str:
    """Unicode sparkline (``▁▂▃▄▅▆▇█``) for the trends command."""
    numbers = [float(v or 0.0) for v in values]
    if not numbers:
        return ""
    if width is not None and width > 0 and len(numbers) > width:
        # Downsample by averaging buckets so the shape survives.
        step = len(numbers) / width
        numbers = [
            sum(numbers[int(i * step) : max(int(i * step) + 1, int((i + 1) * step))])
            / max(1, len(numbers[int(i * step) : max(int(i * step) + 1, int((i + 1) * step))]))
            for i in range(width)
        ]
    top = max(numbers)
    bottom = min(numbers)
    span = top - bottom
    if span <= 0:
        return SPARK_CHARS[-1] * len(numbers) if top > 0 else SPARK_CHARS[0] * len(numbers)
    out = []
    for value in numbers:
        index = int((value - bottom) / span * (len(SPARK_CHARS) - 1) + 0.5)
        out.append(SPARK_CHARS[min(len(SPARK_CHARS) - 1, max(0, index))])
    return "".join(out)


# --------------------------------------------------------------------------------------
# View model
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class KpiSet:
    """The KPI strip at the top of the HUD."""

    today_cost_usd: float = 0.0
    week_cost_usd: float = 0.0
    month_cost_usd: float = 0.0
    today_tokens: int = 0
    burn_rate_tokens_per_min: float = 0.0
    burn_rate_usd_per_hour: float = 0.0
    cache_hit_rate: float = 0.0
    active_agents: int = 0
    active_projects: int = 0
    total_sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "today_cost_usd": round(self.today_cost_usd, 6),
            "week_cost_usd": round(self.week_cost_usd, 6),
            "month_cost_usd": round(self.month_cost_usd, 6),
            "today_tokens": int(self.today_tokens),
            "burn_rate_tokens_per_min": round(self.burn_rate_tokens_per_min, 3),
            "burn_rate_usd_per_hour": round(self.burn_rate_usd_per_hour, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 6),
            "active_agents": int(self.active_agents),
            "active_projects": int(self.active_projects),
            "total_sessions": int(self.total_sessions),
        }


@dataclass(slots=True)
class Bar:
    """One horizontal bar row of the current dimension."""

    label: str
    value: float
    bar: str
    detail: str = ""
    share_pct: float = 0.0
    tokens: int = 0
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": round(self.value, 6),
            "share_pct": round(self.share_pct, 4),
            "tokens": int(self.tokens),
            "detail": self.detail,
            "status": self.status,
            "bar": self.bar,
        }


@dataclass(slots=True)
class BudgetBar:
    """One budget utilisation row (``spent / limit`` against its own scale)."""

    name: str
    scope: str = "global"
    period: str = "daily"
    spent_usd: float = 0.0
    limit_usd: float = 0.0
    pct: float = 0.0
    projected_pct: float = 0.0
    status: str = "ok"
    bar: str = ""

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def overspend_usd(self) -> float:
        return max(0.0, self.spent_usd - self.limit_usd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "period": self.period,
            "spent_usd": round(self.spent_usd, 6),
            "limit_usd": round(self.limit_usd, 6),
            "pct": round(self.pct, 6),
            "projected_pct": round(self.projected_pct, 6),
            "status": self.status,
            "remaining_usd": round(self.remaining_usd, 6),
            "overspend_usd": round(self.overspend_usd, 6),
            "bar": self.bar,
        }


@dataclass(slots=True)
class HudSnapshot:
    """Everything one HUD frame needs, independent of how it was fetched."""

    server: str = ""
    dimension: str = "project"
    project: str | None = None
    generated_ms: int = 0
    kpis: KpiSet = field(default_factory=KpiSet)
    bars: list[Bar] = field(default_factory=list)
    budgets: list[BudgetBar] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    offline: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    # -- derived helpers ------------------------------------------------------------
    @property
    def dimension_label(self) -> str:
        return DIMENSION_LABELS.get(self.dimension, self.dimension.upper())

    @property
    def generated_at(self) -> str:
        return timeutil.iso(self.generated_ms) or ""

    def to_dict(self) -> dict[str, Any]:
        """JSON view model for ``--json``."""
        return {
            "server": self.server,
            "dimension": self.dimension,
            "project": self.project,
            "generated_at": self.generated_at,
            "offline": self.offline,
            "error": self.error,
            "warnings": list(self.warnings),
            "kpis": self.kpis.to_dict(),
            "bars": [bar.to_dict() for bar in self.bars],
            "budgets": [budget.to_dict() for budget in self.budgets],
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens": int(self.total_tokens),
        }


# --------------------------------------------------------------------------------------
# Payload -> view model adapters
# --------------------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def kpis_from_dashboard(overview: dict[str, Any]) -> KpiSet:
    """Build the KPI strip from ``GET /api/v1/dashboard/overview``."""
    kpis = overview.get("kpis") or {}
    if not isinstance(kpis, dict):
        return KpiSet()
    return KpiSet(
        today_cost_usd=_as_float(kpis.get("today_cost_usd")),
        week_cost_usd=_as_float(kpis.get("week_cost_usd")),
        month_cost_usd=_as_float(kpis.get("month_cost_usd")),
        today_tokens=_as_int(kpis.get("today_tokens")),
        burn_rate_tokens_per_min=_as_float(kpis.get("burn_rate_tokens_per_min")),
        burn_rate_usd_per_hour=_as_float(kpis.get("burn_rate_usd_per_hour")),
        cache_hit_rate=_as_float(kpis.get("cache_hit_rate")),
        active_agents=_as_int(kpis.get("active_agents")),
        active_projects=_as_int(kpis.get("active_projects")),
        total_sessions=_as_int(kpis.get("total_sessions")),
    )


def kpis_from_timeseries(points: Sequence[dict[str, Any]], now_ms: int) -> KpiSet:
    """Derive the KPI strip from daily ``TimePoint``s (fallback when no dashboard API).

    ``today``/``week``/``month`` are calendar aligned in UTC here — the authoritative
    numbers come from the dashboard endpoint, which aligns to the display timezone. The
    fallback only needs to be *close enough to be useful* while the aggregation endpoint
    is unavailable, and it must never raise.
    """
    today_start = timeutil.bucket_start(now_ms, "day")
    week_start = timeutil.bucket_start(now_ms, "week")
    month_start = timeutil.bucket_start(now_ms, "month")

    kpis = KpiSet()
    cache_read = 0
    cache_input = 0
    for point in points or []:
        totals = (point or {}).get("totals") or {}
        if not isinstance(totals, dict):
            continue
        bucket_ms = _as_int((point or {}).get("bucket_start_ms"))
        if bucket_ms == 0:
            bucket_ms = timeutil.parse_time_ms((point or {}).get("bucket_start"), default=0) or 0
        cost = _as_float(totals.get("cost_usd"))
        tokens = _as_int(totals.get("total_tokens"))
        cache_read += _as_int(totals.get("cache_read_tokens"))
        cache_input += _as_int(totals.get("input_tokens"))
        kpis.month_cost_usd += cost
        if bucket_ms >= week_start:
            kpis.week_cost_usd += cost
        if bucket_ms >= today_start:
            kpis.today_cost_usd += cost
            kpis.today_tokens += tokens
        kpis.month_cost_usd = kpis.month_cost_usd if bucket_ms >= month_start else kpis.month_cost_usd
    denominator = cache_read + cache_input
    kpis.cache_hit_rate = (cache_read / denominator) if denominator else 0.0
    return kpis


def kpis_from_live(live: dict[str, Any], kpis: KpiSet | None = None) -> KpiSet:
    """Overlay the trailing-window ``/usage/live`` numbers onto a KPI set."""
    result = kpis or KpiSet()
    if not isinstance(live, dict):
        return result
    result.burn_rate_tokens_per_min = _as_float(live.get("tokens_per_min"))
    result.burn_rate_usd_per_hour = _as_float(live.get("cost_per_hour"))
    result.active_agents = _as_int(live.get("active_agents"))
    result.active_projects = _as_int(live.get("active_projects"))
    result.total_sessions = _as_int(live.get("active_sessions"))
    return result


def bars_from_breakdown_rows(
    rows: Iterable[dict[str, Any]],
    *,
    dimension: str = "project",
    bar_width: int = DEFAULT_BAR_WIDTH,
    limit: int = 12,
) -> tuple[list[Bar], float, int]:
    """Turn ``BreakdownRow`` payloads into bar rows.

    Returns ``(bars, total_cost, total_tokens)``. Rows are sorted by cost descending and
    the bar scale is the largest row so the top talker always fills the bar.
    """
    prepared: list[tuple[str, float, int, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        totals = row.get("totals") or {}
        if not isinstance(totals, dict):
            totals = {}
        label = str(row.get("label") or row.get("key") or "?")
        cost = _as_float(totals.get("cost_usd", row.get("cost_usd")))
        tokens = _as_int(totals.get("total_tokens", row.get("total_tokens")))
        share = _as_float(row.get("share_pct"))
        prepared.append((label, cost, tokens, share))
    prepared.sort(key=lambda item: (-item[1], -item[2], item[0]))
    if limit and limit > 0:
        prepared = prepared[:limit]
    maximum = max((item[1] for item in prepared), default=0.0)
    if maximum <= 0:
        maximum = max((float(item[2]) for item in prepared), default=0.0)
        scale_tokens = True
    else:
        scale_tokens = False

    bars: list[Bar] = []
    total_cost = 0.0
    total_tokens = 0
    for label, cost, tokens, share in prepared:
        total_cost += cost
        total_tokens += tokens
        value = float(tokens) if scale_tokens else cost
        detail = f"{fmt_tokens(tokens)} tok  {fmt_pct(share / 100.0)}" if share else f"{fmt_tokens(tokens)} tok"
        bars.append(
            Bar(
                label=label,
                value=cost,
                bar=make_bar(value, maximum, bar_width),
                detail=detail,
                share_pct=share,
                tokens=tokens,
            )
        )
    return bars, total_cost, total_tokens


def budget_bars_from_statuses(
    statuses: Iterable[dict[str, Any]],
    *,
    bar_width: int = DEFAULT_BAR_WIDTH,
) -> list[BudgetBar]:
    """Turn ``BudgetStatus`` payloads into budget bar rows (never raises on gaps)."""
    out: list[BudgetBar] = []
    for status in statuses or []:
        if not isinstance(status, dict):
            continue
        budget = status.get("budget") or {}
        if not isinstance(budget, dict):
            budget = {}
        name = str(budget.get("name") or status.get("budget_name") or f"budget-{status.get('id', '?')}")
        spent = _as_float(status.get("spent_usd"))
        limit = _as_float(status.get("limit_usd", budget.get("limit_usd")))
        pct = _as_float(status.get("pct"))
        # ``spent/limit`` is authoritative when the limit is known; ``pct`` is the
        # server's own fraction and is trusted when the limit is missing.
        if limit > 0:
            pct = spent / limit
        projected_pct = _as_float(status.get("projected_pct"), pct)
        out.append(
            BudgetBar(
                name=name,
                scope=str(budget.get("scope") or status.get("scope") or "global"),
                period=str(budget.get("period") or status.get("period") or "daily"),
                spent_usd=spent,
                limit_usd=limit,
                pct=pct,
                projected_pct=projected_pct,
                status=str(status.get("status") or "ok"),
                bar=budget_bar(pct, bar_width),
            )
        )
    out.sort(key=lambda item: (-item.pct, item.name))
    return out


def bars_from_budget_bars(budgets: Sequence[BudgetBar]) -> list[Bar]:
    """Reuse budget rows as the main bar list when the dimension is ``budget``."""
    return [
        Bar(
            label=budget.name,
            value=budget.spent_usd,
            bar=budget.bar,
            detail=f"{fmt_cost(budget.spent_usd)} / {fmt_cost(budget.limit_usd)}  {fmt_pct(budget.pct)}  {budget.status}",
            share_pct=budget.pct * 100.0,
            status=budget.status,
        )
        for budget in budgets
    ]


# --------------------------------------------------------------------------------------
# Plain text renderer
# --------------------------------------------------------------------------------------


#: ASCII fallbacks for consoles whose code page cannot encode the block glyphs
#: (a Windows ``cmd.exe`` with the legacy GBK/CP437 code page, for instance).
ASCII_GLYPHS = {
    FILLED: "#",
    EMPTY: "-",
    "─": "-",
    "▁": ".",
    "▂": ":",
    "▃": "-",
    "▄": "=",
    "▅": "+",
    "▆": "*",
    "▇": "%",
    "█": "#",
    "▲": "^",
    "▼": "v",
    "→": "->",
    "·": "-",
    "…": "...",
    "✓": "ok",
    "✗": "x",
}


def can_encode(text: str, stream: Any = None) -> bool:
    """True when ``stream`` can encode ``text`` (False when unknown/absent)."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def adapt_for_stream(text: str, stream: Any = None) -> str:
    """Down-convert block glyphs to ASCII when the output stream cannot encode them.

    A monitoring tool that crashes with ``UnicodeEncodeError`` on a legacy Windows code
    page is worse than one that draws slightly uglier bars, so every CLI write goes
    through this helper.
    """
    if stream is None or can_encode(text, stream):
        return text
    for fancy, plain in ASCII_GLYPHS.items():
        text = text.replace(fancy, plain)
    # Anything still unencodable (user-supplied labels) becomes a replacement char.
    encoding = getattr(stream, "encoding", None)
    if encoding:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    return text


def write_text(stream: Any, text: str) -> None:
    """Write ``text`` to ``stream``, degrading glyphs when the code page demands it."""
    stream.write(adapt_for_stream(text, stream))


def _rule(width: int, char: str = "─") -> str:
    return char * max(10, width)


def plain_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    justify: Sequence[str] | None = None,
    indent: str = "  ",
    gap: str = "  ",
) -> str:
    """Render a column aligned plain-text table (no ``rich``, no colour)."""
    headers = [str(h) for h in headers]
    body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    if not headers:
        return ""
    width = len(headers)
    just = list(justify or ["left"] * width)
    while len(just) < width:
        just.append("left")
    widths = [len(headers[i]) for i in range(width)]
    for row in body:
        for i in range(min(width, len(row))):
            widths[i] = max(widths[i], len(row[i]))

    def _line(cells: Sequence[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            if i >= width:
                break
            parts.append(cell.rjust(widths[i]) if just[i] == "right" else cell.ljust(widths[i]))
        return indent + gap.join(parts).rstrip()

    lines = [_line(headers)]
    lines.append(indent + gap.join("─" * widths[i] for i in range(width)))
    for row in body:
        padded = list(row) + [""] * (width - len(row))
        lines.append(_line(padded))
    return "\n".join(lines)


def render_plain(snapshot: HudSnapshot, *, width: int = DEFAULT_WIDTH) -> str:
    """Render one HUD frame as deterministic plain text."""
    width = max(60, int(width or DEFAULT_WIDTH))
    bar_width = max(12, min(40, width - 52))
    lines: list[str] = []

    header = f"TokenLens HUD  {snapshot.server}  {fmt_clock(snapshot.generated_ms)} UTC"
    if snapshot.project:
        header += f"  project={snapshot.project}"
    header += f"  [{snapshot.dimension_label}]"
    lines.append(header)

    if snapshot.offline:
        lines.append(f"OFFLINE  {snapshot.error or 'server unreachable'}  (retrying…)")
        return "\n".join(lines)

    lines.append(_rule(width))

    kpis = snapshot.kpis
    lines.append(
        f"  today {fmt_cost(kpis.today_cost_usd)}   "
        f"week {fmt_cost(kpis.week_cost_usd)}   "
        f"month {fmt_cost(kpis.month_cost_usd)}"
    )
    lines.append(
        f"  burn {fmt_rate(kpis.burn_rate_tokens_per_min)} ({fmt_usd_rate(kpis.burn_rate_usd_per_hour)})"
        f"   cache hit {fmt_pct(kpis.cache_hit_rate)}"
        f"   tokens(today) {fmt_tokens(kpis.today_tokens)}"
    )
    lines.append(
        f"  agents {kpis.active_agents}   projects {kpis.active_projects}"
        f"   sessions {kpis.total_sessions}"
    )

    if snapshot.warnings:
        lines.append("  " + "  ".join(f"! {warning}" for warning in snapshot.warnings))

    if snapshot.dimension != "budget":
        lines.append(_rule(width))
        lines.append("  BUDGETS")
        if not snapshot.budgets:
            lines.append("  (no budgets configured)")
        for budget in snapshot.budgets:
            lines.append(
                f"  {budget.name[:18]:<18} {fmt_cost(budget.spent_usd):>9} / {fmt_cost(budget.limit_usd):<9}"
                f" {fmt_pct(budget.pct):>7}  {budget.status:<8} {budget.bar}"
            )

    lines.append(_rule(width))
    lines.append(f"  BY {snapshot.dimension_label}   (cost · share · tokens)")
    if not snapshot.bars:
        lines.append("  (no usage recorded in this window)")
    for bar in snapshot.bars:
        lines.append(
            f"  {bar.label[:18]:<18} {fmt_cost(bar.value):>9}  {fmt_pct(bar.share_pct / 100.0):>6}"
            f"  {bar.bar}  {bar.detail}"
        )

    lines.append(_rule(width))
    total_line = (
        f"  total {fmt_cost(snapshot.total_cost_usd)}"
        f"   tokens {fmt_tokens(snapshot.total_tokens)}"
        f"   rows {len(snapshot.bars)}"
    )
    lines.append(total_line)
    if snapshot.budgets and snapshot.dimension != "budget":
        exceeded = [b for b in snapshot.budgets if b.status in ("critical", "exceeded")]
        if exceeded:
            lines.append(
                "  budget warning: " + ", ".join(f"{b.name} {fmt_pct(b.pct)}" for b in exceeded)
            )
    lines.append("  keys: p project · a agent · m model · b budget · q quit")
    return "\n".join(lines)


def render_offline_plain(server: str, error: str, *, width: int = DEFAULT_WIDTH) -> str:
    """Standalone OFFLINE frame (used when the very first fetch fails)."""
    width = max(40, int(width or DEFAULT_WIDTH))
    return "\n".join(
        [
            f"TokenLens HUD  {server}",
            _rule(width),
            f"OFFLINE  {error}",
            "  hint: start the server with  python run_server.py --port 8787",
        ]
    )


# --------------------------------------------------------------------------------------
# Rich renderer
# --------------------------------------------------------------------------------------


def _kpi_grid(kpis: KpiSet) -> Table:
    """A 5-column KPI strip (label above value, ai-token-hud style)."""
    grid = Table.grid(padding=(0, 2), expand=True)
    for _ in range(5):
        grid.add_column(justify="center")
    grid.add_row(
        Text("TODAY", style="dim"),
        Text("WEEK", style="dim"),
        Text("BURN", style="dim"),
        Text("CACHE HIT", style="dim"),
        Text("ACTIVE", style="dim"),
    )
    burn_style = "bold cyan"
    if kpis.burn_rate_tokens_per_min > 0:
        burn_style = "bold yellow"
    grid.add_row(
        Text(fmt_cost(kpis.today_cost_usd), style="bold green"),
        Text(fmt_cost(kpis.week_cost_usd), style="bold green"),
        Text(fmt_rate(kpis.burn_rate_tokens_per_min), style=burn_style),
        Text(fmt_pct(kpis.cache_hit_rate), style="bold magenta"),
        Text(
            f"{kpis.active_agents}a / {kpis.active_projects}p / {kpis.total_sessions}s",
            style="bold blue",
        ),
    )
    grid.add_row(
        Text(f"{fmt_tokens(kpis.today_tokens)} tok today", style="dim"),
        Text(f"month {fmt_cost(kpis.month_cost_usd)}", style="dim"),
        Text(fmt_usd_rate(kpis.burn_rate_usd_per_hour), style="dim"),
        Text("cache_read / prompt", style="dim"),
        Text("agents/projects/sessions", style="dim"),
    )
    return grid


def _bars_table(bars: Sequence[Bar], title: str, *, bar_width: int = DEFAULT_BAR_WIDTH) -> Table:
    table = Table(
        title=Text(title, style="bold white"),
        title_justify="left",
        box=None,
        pad_edge=False,
        expand=True,
        show_header=False,
    )
    table.add_column("label", no_wrap=True, width=20)
    table.add_column("cost", justify="right", no_wrap=True, width=10)
    table.add_column("share", justify="right", no_wrap=True, width=7)
    table.add_column("bar", no_wrap=True, width=bar_width)
    table.add_column("detail", no_wrap=False, ratio=1)
    for bar in bars:
        style = "cyan"
        if bar.status:
            style = STATUS_STYLES.get(bar.status, "cyan")
        table.add_row(
            Text(bar.label, style="white"),
            Text(fmt_cost(bar.value), style="green"),
            Text(fmt_pct(bar.share_pct / 100.0), style="dim"),
            Text(bar.bar, style=style),
            Text(bar.detail, style="dim"),
        )
    return table


def _budgets_table(budgets: Sequence[BudgetBar], *, bar_width: int = DEFAULT_BAR_WIDTH) -> Table:
    table = Table(
        title=Text("BUDGETS", style="bold white"),
        title_justify="left",
        box=None,
        pad_edge=False,
        expand=True,
        show_header=False,
    )
    table.add_column("name", no_wrap=True, width=20)
    table.add_column("spent", justify="right", no_wrap=True, width=10)
    table.add_column("limit", justify="right", no_wrap=True, width=10)
    table.add_column("pct", justify="right", no_wrap=True, width=7)
    table.add_column("bar", no_wrap=True, width=bar_width)
    table.add_column("status", no_wrap=True, ratio=1)
    for budget in budgets:
        style = STATUS_STYLES.get(budget.status, "green")
        table.add_row(
            Text(budget.name, style="white"),
            Text(fmt_cost(budget.spent_usd), style="green"),
            Text(fmt_cost(budget.limit_usd), style="dim"),
            Text(fmt_pct(budget.pct), style=style),
            Text(budget.bar, style=style),
            Text(
                f"{budget.status}  projected {fmt_pct(budget.projected_pct)}  "
                f"{budget.scope}/{budget.period}",
                style=style,
            ),
        )
    return table


def render_rich(snapshot: HudSnapshot) -> RenderableType:
    """Build the ``rich`` renderable tree for one HUD frame."""
    parts: list[RenderableType] = []

    header_text = Text()
    header_text.append("TokenLens ", style="bold cyan")
    header_text.append("HUD", style="bold white")
    header_text.append(f"   {snapshot.server}", style="dim")
    header_text.append(f"   {fmt_clock(snapshot.generated_ms)} UTC", style="dim")
    if snapshot.project:
        header_text.append(f"   project={snapshot.project}", style="bold yellow")
    header_text.append(f"   dimension={snapshot.dimension_label}", style="bold magenta")

    if snapshot.offline:
        parts.append(
            Panel(
                Text(f"OFFLINE — {snapshot.error or 'server unreachable'}", style="bold red"),
                title=header_text,
                border_style="red",
                subtitle=Text("retrying…", style="dim red"),
            )
        )
        parts.append(
            Text("  hint: start the server with  python run_server.py --port 8787", style="dim")
        )
        return Group(*parts)

    parts.append(Panel(header_text, border_style="cyan", padding=(0, 1)))
    parts.append(_kpi_grid(snapshot.kpis))

    if snapshot.warnings:
        for warning in snapshot.warnings:
            parts.append(Text(f"! {warning}", style="yellow"))

    if snapshot.dimension != "budget":
        if snapshot.budgets:
            parts.append(_budgets_table(snapshot.budgets))
        else:
            parts.append(Text("BUDGETS", style="bold white"))
            parts.append(Text("  (no budgets configured)", style="dim"))

    if snapshot.bars:
        parts.append(_bars_table(snapshot.bars, f"BY {snapshot.dimension_label}"))
    else:
        parts.append(Text(f"BY {snapshot.dimension_label}", style="bold white"))
        parts.append(Text("  (no usage recorded in this window)", style="dim"))

    footer = Text()
    footer.append(f"total {fmt_cost(snapshot.total_cost_usd)}", style="bold green")
    footer.append(f"   tokens {fmt_tokens(snapshot.total_tokens)}", style="dim")
    footer.append(f"   rows {len(snapshot.bars)}", style="dim")
    footer.append("   keys: ", style="dim")
    for key, dimension in KEY_BINDINGS.items():
        footer.append(f"[{key}]", style="bold cyan")
        footer.append(f" {dimension}  ", style="dim")
    footer.append("[q]", style="bold red")
    footer.append(" quit", style="dim")
    parts.append(Text("─" * 60, style="dim"))
    parts.append(footer)
    return Group(*parts)


# --------------------------------------------------------------------------------------
# Reusable tables for the non-live subcommands
# --------------------------------------------------------------------------------------


def rich_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    title: str | None = None,
    justify: Sequence[str] | None = None,
    styles: Sequence[str] | None = None,
) -> Table:
    """Generic ``rich`` table used by ``summary``/``trends``/``budgets``/... commands."""
    table = Table(
        title=Text(title, style="bold white") if title else None,
        title_justify="left",
        box=None,
        header_style="bold dim",
        pad_edge=False,
        expand=False,
    )
    just = list(justify or [])
    for index, header in enumerate(headers):
        table.add_column(
            str(header),
            justify=(just[index] if index < len(just) else "left"),
            no_wrap=False,
        )
    for row in rows:
        rendered = []
        for index, cell in enumerate(row):
            style = styles[index] if styles and index < len(styles) else None
            rendered.append(Text("" if cell is None else str(cell), style=style))
        table.add_row(*rendered)
    return table


def format_bytes(value: float | int | None) -> str:
    """Human readable byte size (``1.2 MB``) for anomaly evidence columns."""
    amount = float(value or 0.0)
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if abs(amount) >= scale:
            return f"{amount / scale:.1f} {unit}"
    return f"{int(amount)} B"


def format_datetime(value: Any) -> str:
    """Render an API datetime (ISO string or epoch ms) as ``YYYY-MM-DD HH:MM:SS``."""
    if value is None or value == "":
        return "-"
    if isinstance(value, (int, float)):
        return fmt_timestamp(int(value))
    text = str(value)
    try:
        ms = timeutil.parse_time_ms(text)
    except ValueError:
        return text
    if ms is None:
        return text
    return fmt_timestamp(ms)


def relative_age(value: Any, *, now_ms: int | None = None) -> str:
    """``12s ago`` / ``3m ago`` for alert and anomaly tables."""
    if value is None or value == "":
        return "-"
    try:
        ms = timeutil.parse_time_ms(value)
    except ValueError:
        return str(value)
    if not ms:
        return "-"
    now = timeutil.now_ms() if now_ms is None else int(now_ms)
    delta = max(0, now - int(ms))
    return f"{fmt_duration_ms(delta)} ago"


def utc_day_start(now_ms: int | None = None) -> int:
    """UTC midnight of the current day — the default ``--from`` for the CLI."""
    return timeutil.bucket_start(now_ms if now_ms is not None else timeutil.now_ms(), "day")


def utc_now_iso() -> str:
    """Current time as an API ready ISO-8601 ``Z`` string."""
    return timeutil.iso(timeutil.now_ms()) or datetime.now(timezone.utc).isoformat()
