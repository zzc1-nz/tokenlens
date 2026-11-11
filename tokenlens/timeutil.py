"""Time helpers.

Two representations are used across TokenLens:

* **epoch milliseconds (UTC)** inside the database and the ingest path — cheap to sort,
  index and aggregate;
* **ISO-8601 UTC strings with a ``Z`` suffix** on the API boundary — human readable and
  timezone unambiguous.

All bucketing is done in UTC so that a bucket never shifts with the server timezone.
Labels shown in the UI are rendered in :data:`tokenlens.config.Settings.display_tz`.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

UTC = timezone.utc

_ISO_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"(?:[T ](?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2})(?:\.(?P<f>\d{1,9}))?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?)?$"
)


def now_ms() -> int:
    """Current wall clock time in epoch milliseconds (UTC)."""
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def ms_to_dt(ms: int | float) -> datetime:
    """Epoch milliseconds -> timezone aware UTC datetime."""
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Timezone aware datetime -> epoch milliseconds (naive datetimes are assumed UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def iso(ms: int | float | None) -> str | None:
    """Epoch milliseconds -> ``2026-11-10T13:04:05.123Z``."""
    if ms is None:
        return None
    dt = ms_to_dt(ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_seconds(ms: int | float | None) -> str | None:
    """Epoch milliseconds -> ``2026-11-10T13:04:05Z`` (second resolution)."""
    if ms is None:
        return None
    return ms_to_dt(ms).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time_ms(value: str | int | float | datetime | None, *, default: int | None = None) -> int | None:
    """Parse an ISO-8601 string, a datetime or an epoch-ms number into epoch ms.

    Accepts ``2026-11-10``, ``2026-11-10T13:04``, ``...Z``, ``...+08:00`` and bare
    numbers (treated as epoch milliseconds; seconds are auto-detected below 1e11).
    Returns ``default`` for ``None``/empty input and raises ``ValueError`` on garbage.
    """
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number if number >= 1e11 else number * 1000)

    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return int(number if number >= 1e11 else number * 1000)

    match = _ISO_RE.match(text)
    if not match:
        raise ValueError(f"invalid timestamp: {value!r}")
    parts = match.groupdict()
    fraction = parts.get("f") or ""
    micro = int((fraction + "000000")[:6]) if fraction else 0
    tz_raw = parts.get("tz")
    if tz_raw in (None, ""):
        tzinfo = UTC
    elif tz_raw.upper() == "Z":
        tzinfo = UTC
    else:
        sign = 1 if tz_raw[0] == "+" else -1
        digits = tz_raw[1:].replace(":", "")
        tzinfo = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0)))
    dt = datetime(
        year=int(parts["y"]),
        month=int(parts["m"]),
        day=int(parts["d"]),
        hour=int(parts.get("H") or 0),
        minute=int(parts.get("M") or 0),
        second=int(parts.get("S") or 0),
        microsecond=micro,
        tzinfo=tzinfo,
    )
    return dt_to_ms(dt.astimezone(UTC))


def display_tz(tz_name: str = "Asia/Shanghai"):
    """Best-effort timezone lookup that never fails (falls back to a fixed +08:00)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - platform without tzdata
        return timezone(timedelta(hours=8))


def label_for(ms: int, bucket: str = "hour", tz_name: str = "Asia/Shanghai") -> str:
    """Human label for a bucket start, rendered in the display timezone."""
    dt = ms_to_dt(ms).astimezone(display_tz(tz_name))
    if bucket == "minute":
        return dt.strftime("%Y-%m-%d %H:%M")
    if bucket == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    if bucket == "day":
        return dt.strftime("%Y-%m-%d")
    if bucket == "week":
        return f"{dt.strftime('%Y-%m-%d')} W{dt.isocalendar().week:02d}"
    if bucket == "month":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d %H:%M")


BUCKET_MS = {
    "minute": 60_000,
    "hour": 3_600_000,
    "day": 86_400_000,
    "week": 7 * 86_400_000,
    "month": 30 * 86_400_000,  # nominal; use bucket_start(..., "month") for calendar months
}

BUCKET_ALIASES = {
    "1m": "minute",
    "minute": "minute",
    "minutes": "minute",
    "5m": "minute",
    "h": "hour",
    "1h": "hour",
    "hour": "hour",
    "hourly": "hour",
    "d": "day",
    "1d": "day",
    "day": "day",
    "daily": "day",
    "w": "week",
    "1w": "week",
    "week": "week",
    "weekly": "week",
    "mo": "month",
    "month": "month",
    "monthly": "month",
}


def normalize_bucket(bucket: str | None, default: str = "hour") -> str:
    """Map aliases (``1h``, ``daily``, ...) to a canonical bucket name."""
    if not bucket:
        return default
    key = str(bucket).strip().lower()
    if key not in BUCKET_ALIASES:
        raise ValueError(f"unsupported bucket: {bucket!r}")
    return BUCKET_ALIASES[key]


def bucket_start(ms: int, bucket: str = "hour") -> int:
    """Floor an epoch-ms timestamp to the start of its bucket (UTC).

    Calendar aware for ``day``/``week`` (ISO weeks start Monday) and ``month``.
    """
    bucket = normalize_bucket(bucket)
    if bucket == "minute":
        return (ms // 60_000) * 60_000
    if bucket == "hour":
        return (ms // 3_600_000) * 3_600_000
    dt = ms_to_dt(ms)
    if bucket == "day":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif bucket == "week":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=dt.weekday())
    elif bucket == "month":
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # pragma: no cover - normalize_bucket already validated
        raise ValueError(f"unsupported bucket: {bucket!r}")
    return dt_to_ms(start)


def bucket_end(start_ms: int, bucket: str = "hour") -> int:
    """Exclusive end of the bucket that starts at ``start_ms``."""
    bucket = normalize_bucket(bucket)
    if bucket == "minute":
        return start_ms + 60_000
    if bucket == "hour":
        return start_ms + 3_600_000
    if bucket == "week":
        return start_ms + 7 * 86_400_000
    dt = ms_to_dt(start_ms)
    if bucket == "day":
        return dt_to_ms(dt + timedelta(days=1))
    if bucket == "month":
        year, month = dt.year, dt.month
        month += 1
        if month == 13:
            year, month = year + 1, 1
        days = calendar.monthrange(year, month)[1]
        return dt_to_ms(dt.replace(year=year, month=month, day=min(dt.day, days)))
    raise ValueError(f"unsupported bucket: {bucket!r}")  # pragma: no cover


def iter_buckets(start_ms: int, end_ms: int, bucket: str = "hour") -> Iterator[int]:
    """Yield every bucket start in ``[start_ms, end_ms)`` — used for gap filling."""
    bucket = normalize_bucket(bucket)
    cursor = bucket_start(start_ms, bucket)
    guard = 0
    while cursor < end_ms:
        yield cursor
        nxt = bucket_end(cursor, bucket)
        if nxt <= cursor:  # pragma: no cover - defensive
            raise ValueError("bucket_end must advance")
        cursor = nxt
        guard += 1
        if guard > 200_000:  # pragma: no cover - defensive
            raise ValueError("too many buckets")


def period_window(period: str, at_ms: int | None = None, tz_name: str = "Asia/Shanghai") -> tuple[int, int]:
    """Return ``(start_ms, end_ms)`` for the *current* ``daily|weekly|monthly`` window.

    Windows are calendar aligned in the display timezone (a "day" for a Shanghai user
    starts at 00:00 +08:00, not 00:00 UTC) and converted back to UTC epoch ms.
    """
    key = str(period).strip().lower()
    if key in {"day", "daily"}:
        key = "daily"
    elif key in {"week", "weekly"}:
        key = "weekly"
    elif key in {"month", "monthly"}:
        key = "monthly"
    else:
        raise ValueError(f"unsupported period: {period!r}")

    at_ms = now_ms() if at_ms is None else int(at_ms)
    local = ms_to_dt(at_ms).astimezone(display_tz(tz_name))
    if key == "daily":
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
    elif key == "weekly":
        start_local = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = start_local + timedelta(days=7)
    else:
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year, month = start_local.year, start_local.month
        month += 1
        if month == 13:
            year, month = year + 1, 1
        end_local = start_local.replace(year=year, month=month)
    return dt_to_ms(start_local), dt_to_ms(end_local)


def previous_window(period: str, start_ms: int, end_ms: int, tz_name: str = "Asia/Shanghai") -> tuple[int, int]:
    """The window immediately before ``[start_ms, end_ms)`` of the same period type."""
    key = str(period).strip().lower()
    if key in {"day", "daily"}:
        return period_window("daily", start_ms - 1, tz_name)
    if key in {"week", "weekly"}:
        return period_window("weekly", start_ms - 1, tz_name)
    return period_window("monthly", start_ms - 1, tz_name)


def window_from_request(
    from_value: object,
    to_value: object,
    *,
    default_hours: float = 24.0,
    now: int | None = None,
) -> tuple[int, int]:
    """Resolve ``from``/``to`` query parameters into a concrete ``(start, end)`` window.

    ``to`` defaults to now; ``from`` defaults to ``to - default_hours``. Raises
    ``ValueError`` when ``from >= to``.
    """
    now = now_ms() if now is None else int(now)
    end = parse_time_ms(to_value, default=now)
    start = parse_time_ms(from_value, default=None)
    if start is None:
        start = end - int(default_hours * 3_600_000)
    if start >= end:
        raise ValueError("'from' must be earlier than 'to'")
    return int(start), int(end)


def humanize_duration(ms: int) -> str:
    """Compact duration rendering used by the CLI and alert messages."""
    seconds = max(0, int(ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def summarize_ranges(ranges: Iterable[tuple[int, int]]) -> str:
    """Debug helper: ``[(0,10),(10,20)]`` -> ``"0-20"``."""
    items = sorted(ranges)
    if not items:
        return ""
    merged: list[list[int]] = [list(items[0])]
    for start, end in items[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return ",".join(f"{a}-{b}" for a, b in merged)
