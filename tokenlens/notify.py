"""Notification sinks for budget alerts and (future) anomaly alerts.

An alert is only useful if it actually reaches a human, so the budget engine fans one
:class:`Notification` out to every configured channel:

* ``webhook``  — JSON ``POST`` to a Slack/Discord/Feishu/Lark style receiver. Retried
  ``settings.webhook_retries`` times with a short exponential backoff and a hard
  ``settings.webhook_timeout_seconds`` timeout.
* ``desktop``  — appends one JSON line to ``settings.notify_file`` (``data/notifications.jsonl``)
  and, on Windows, makes a best-effort balloon-tip attempt through PowerShell.
* ``log``      — writes the notification to the application log (used by the CLI).

Design rule: **every sink is total**. It returns ``(ok, detail)`` and never raises. An
alerting path that can take down the evaluation loop is worse than no alerting at all, so
all I/O is wrapped and failures are logged instead of propagated.

The desktop sink is deliberately split in two: the JSONL append is the durable, inspectable
record (it is what the tests and the web UI read), while the toast is a fire-and-forget
nicety that runs on a daemon thread so a hung ``powershell.exe`` can never delay a budget
evaluation pass. Set ``TOKENLENS_TOAST=0`` to disable it (it is also skipped off Windows).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import timeutil
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - httpx is a hard dependency in practice
    import httpx
except Exception:  # pragma: no cover - defensive
    httpx = None  # type: ignore[assignment]


#: Channel names understood by :func:`dispatch`.
CHANNELS: tuple[str, ...] = ("webhook", "desktop", "log")

#: Injectable transport so tests (and air-gapped deployments) never touch the network.
#: Assign an ``httpx.MockTransport`` here to intercept every webhook delivery.
WEBHOOK_TRANSPORT: Any = None

#: Guard so concurrent ingest threads appending to the same JSONL file cannot interleave.
_JSONL_LOCK = threading.Lock()

#: Maximum number of characters copied into the PowerShell toast command line.
_TOAST_TEXT_LIMIT = 180


@dataclass(slots=True)
class Notification:
    """One alert rendered for delivery.

    ``payload`` is the machine readable body: for budget alerts it is exactly the JSON
    document POSTed to ``webhook_url`` (which it also carries under the ``webhook_url``
    key, because :func:`dispatch` has no other way to know the target).
    """

    channel: str
    title: str
    body: str
    severity: str = "info"
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Flat JSON-friendly representation (used by the JSONL sink and the log sink)."""
        return {
            "channel": self.channel,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "payload": self.payload,
        }


# --------------------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------------------


def send_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
    transport: Any = None,
) -> tuple[bool, str]:
    """POST ``payload`` to ``url`` with retries.

    Returns ``(ok, detail)`` where ``detail`` is a short human readable outcome such as
    ``"HTTP 200"`` or ``"ConnectError: ..."``. Never raises.
    """
    settings = settings or get_settings()
    if not url or not str(url).startswith(("http://", "https://")):
        return False, "invalid webhook url (must be http/https)"
    if httpx is None:  # pragma: no cover - httpx is installed in every supported env
        return False, "httpx is not available"

    attempts = max(1, int(getattr(settings, "webhook_retries", 2) or 0) + 1)
    timeout = float(getattr(settings, "webhook_timeout_seconds", 3.0) or 3.0)
    transport = transport if transport is not None else WEBHOOK_TRANSPORT
    last_detail = "not attempted"

    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
                response = client.post(str(url), json=payload)
            if 200 <= int(response.status_code) < 300:
                return True, f"HTTP {response.status_code}"
            last_detail = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - a webhook must never raise
            last_detail = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < attempts:
            time.sleep(min(0.5, 0.05 * (2**attempt)))

    logger.warning(
        "webhook delivery failed after %s attempt(s): %s (%s)", attempts, last_detail, url
    )
    return False, last_detail


# --------------------------------------------------------------------------------------
# Desktop / local sink
# --------------------------------------------------------------------------------------


def _notification_file(settings: Settings) -> Path:
    path = getattr(settings, "notify_file", None)
    if path is None:  # pragma: no cover - Settings always provides one
        path = getattr(settings, "data_dir", Path("data")) / "notifications.jsonl"
    return Path(path)


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append exactly one JSON object (plus newline) to ``path``; creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _JSONL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _toast_enabled() -> bool:
    """Windows toast is best-effort and on by default there; ``TOKENLENS_TOAST=0`` disables."""
    if os.name != "nt":
        return False
    raw = os.environ.get("TOKENLENS_TOAST")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _ps_literal(text: str) -> str:
    """Render ``text`` as a single quoted PowerShell string literal."""
    cleaned = "".join(ch for ch in str(text) if ch >= " " or ch == "\t")
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    cleaned = cleaned[:_TOAST_TEXT_LIMIT]
    return "'" + cleaned.replace("'", "''") + "'"


def _toast_script(title: str, body: str) -> str:
    """PowerShell source for a balloon tip (uses WinForms so it works on stock Windows)."""
    return (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(4000, {_ps_literal(title)}, {_ps_literal(body)}, "
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Milliseconds 4200;"
        "$n.Dispose()"
    )


def _spawn_toast(title: str, body: str) -> None:
    """Launch the toast on a daemon thread. Fully guarded: failures only log."""
    def _run() -> None:
        try:
            creationflags = 0
            if os.name == "nt":  # keep the console quiet
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    _toast_script(title, body),
                ],
                timeout=8,
                capture_output=True,
                creationflags=creationflags,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - toast is cosmetic
            logger.debug("desktop toast failed: %s", exc)

    try:
        threading.Thread(target=_run, name="tokenlens-toast", daemon=True).start()
    except Exception as exc:  # pragma: no cover - thread creation failure
        logger.debug("could not start toast thread: %s", exc)


def send_desktop(n: Notification, *, settings: Settings | None = None) -> tuple[bool, str]:
    """Durable JSONL append + best-effort Windows toast.

    Returns ``(ok, detail)``; ``ok`` reflects the durable sink (the toast is cosmetic).
    """
    settings = settings or get_settings()
    path = _notification_file(settings)
    entry = {
        "ts": timeutil.iso(timeutil.now_ms()),
        "channel": "desktop",
        "kind": n.channel,
        "title": n.title,
        "body": n.body,
        "severity": n.severity,
        "payload": n.payload,
    }
    try:
        append_jsonl(path, entry)
    except Exception as exc:  # noqa: BLE001 - notification must never raise
        logger.warning("could not append notification to %s: %s", path, exc)
        return False, f"write failed: {type(exc).__name__}: {exc}"

    if _toast_enabled():
        _spawn_toast(n.title, n.body)
    return True, f"appended to {path}"


# --------------------------------------------------------------------------------------
# Fan-out
# --------------------------------------------------------------------------------------


def _normalise_channels(channels: Sequence[str] | str | None) -> list[str]:
    if channels is None:
        return []
    if isinstance(channels, str):
        raw: Iterable[str] = channels.split(",")
    else:
        raw = channels
    seen: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if name and name not in seen:
            seen.append(name)
    return seen


def dispatch(
    settings: Settings,
    channels: Sequence[str] | str | None,
    notification: Notification,
    *,
    transport: Any = None,
) -> list[dict[str, Any]]:
    """Deliver ``notification`` over every channel and report per-channel outcomes.

    The returned list has one ``{"channel", "ok", "detail"}`` entry per *attempted*
    channel (unknown channel names produce ``ok=False`` instead of an exception), which
    the budget engine stores on the alert row for auditability.
    """
    settings = settings or get_settings()
    results: list[dict[str, Any]] = []

    for channel in _normalise_channels(channels):
        if channel == "webhook":
            url = notification.payload.get("webhook_url") or ""
            ok, detail = send_webhook(url, notification.payload, settings=settings, transport=transport)
        elif channel == "desktop":
            ok, detail = send_desktop(notification, settings=settings)
        elif channel == "log":
            try:
                logger.warning("[%s] %s — %s", notification.severity, notification.title, notification.body)
                ok, detail = True, "logged"
            except Exception as exc:  # pragma: no cover - logging should not fail
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        else:
            ok, detail = False, f"unknown channel: {channel}"
        results.append({"channel": channel, "ok": bool(ok), "detail": detail})

    return results


def channels_for(*, webhook_url: str | None = None, desktop: bool = False, log: bool = False) -> list[str]:
    """Build the channel list for a budget from its delivery configuration."""
    channels: list[str] = []
    if webhook_url:
        channels.append("webhook")
    if desktop:
        channels.append("desktop")
    if log:
        channels.append("log")
    return channels
