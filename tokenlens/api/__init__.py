"""HTTP API layer. One module per bounded context; every module owns one router."""

from __future__ import annotations

__all__ = [
    "ingest",
    "usage",
    "trends",
    "export",
    "catalog",
    "budgets",
    "alerts",
    "anomalies",
    "dashboard",
    "health",
]
