"""Runtime configuration, environment driven.

Every knob is overridable through the environment so that tests, the simulator and a
real deployment can share one code path::

    TOKENLENS_DB=data/tokenlens.db
    TOKENLENS_API_TOKEN=secret          # enables X-API-Key auth on /api/v1/**
    TOKENLENS_BUDGET_INTERVAL=30        # background evaluation period (seconds)
    TOKENLENS_WEBHOOK_TIMEOUT=3
    TOKENLENS_NOTIFY_FILE=data/notifications.jsonl
    TOKENLENS_TZ=Asia/Shanghai          # display timezone for bucket labels
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass(slots=True)
class Settings:
    """Resolved application settings."""

    db_path: Path = field(default_factory=lambda: Path(_env_str("TOKENLENS_DB", str(DEFAULT_DATA_DIR / "tokenlens.db"))))
    data_dir: Path = field(default_factory=lambda: Path(_env_str("TOKENLENS_DATA_DIR", str(DEFAULT_DATA_DIR))))
    api_token: str | None = field(default_factory=lambda: os.environ.get("TOKENLENS_API_TOKEN") or None)
    budget_interval_seconds: int = field(default_factory=lambda: _env_int("TOKENLENS_BUDGET_INTERVAL", 30))
    webhook_timeout_seconds: float = field(default_factory=lambda: _env_float("TOKENLENS_WEBHOOK_TIMEOUT", 3.0))
    webhook_retries: int = field(default_factory=lambda: _env_int("TOKENLENS_WEBHOOK_RETRIES", 2))
    notify_file: Path = field(
        default_factory=lambda: Path(_env_str("TOKENLENS_NOTIFY_FILE", str(DEFAULT_DATA_DIR / "notifications.jsonl")))
    )
    display_tz: str = field(default_factory=lambda: _env_str("TOKENLENS_TZ", "Asia/Shanghai"))
    enable_scheduler: bool = field(default_factory=lambda: _env_bool("TOKENLENS_SCHEDULER", True))
    anomaly_lookback_minutes: int = field(default_factory=lambda: _env_int("TOKENLENS_ANOMALY_LOOKBACK", 60))
    anomaly_enabled: bool = field(default_factory=lambda: _env_bool("TOKENLENS_ANOMALY_ENABLED", True))
    max_ingest_batch: int = field(default_factory=lambda: _env_int("TOKENLENS_MAX_BATCH", 5000))
    cors_origins: str = field(default_factory=lambda: _env_str("TOKENLENS_CORS", "*"))
    # Threshold defaults for the anomaly detector (kept configurable for tuning demos).
    anomaly_z_threshold: float = field(default_factory=lambda: _env_float("TOKENLENS_ANOMALY_Z", 4.0))
    anomaly_ratio_threshold: float = field(default_factory=lambda: _env_float("TOKENLENS_ANOMALY_RATIO", 3.0))
    anomaly_min_tokens_per_min: float = field(
        default_factory=lambda: _env_float("TOKENLENS_ANOMALY_MIN_TPM", 2000.0)
    )

    def ensure_dirs(self) -> None:
        """Create the data directory that backs the SQLite file."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process wide settings singleton (tests may clear the cache)."""
    settings = Settings()
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    """Drop the cached settings object (used by tests that patch the environment)."""
    get_settings.cache_clear()
