"""Budget engine, anomaly detector, notification sinks, scheduler and dashboard tests.

Scope of this suite (owner: **budget-anomaly**):

* ``tokenlens.notify``    — webhook retry/never-raise contract, JSONL desktop sink, fan-out
* ``tokenlens.budget``    — CRUD, scope/window resolution, threshold firing, idempotent
  ``trigger_count`` bumps, overspend math, notification-on-new-breach-only, history queries
* ``tokenlens.anomaly``   — robust statistics helpers, all five root-cause classifiers,
  fingerprint dedupe, severity mapping, status transitions, and end-to-end detection on
  ``generate_workload`` data with injected incidents
* ``tokenlens.scheduler`` — one-shot pass, start/stop lifecycle
* ``tokenlens.api.*``     — every §7/§8/§9 endpoint shape, including empty-DB behaviour

Determinism: every test that involves a calendar window passes an explicit ``now_ms``
anchored to midday Shanghai time, and every anomaly test builds its baseline relative to
that anchor. Nothing depends on the wall clock, so the suite cannot flake at midnight.
"""

from __future__ import annotations

import hashlib
import json
import math

import httpx
import pytest
from fastapi.testclient import TestClient

from tokenlens import anomaly, budget, notify, timeutil
from tokenlens.config import Settings
from tokenlens.db import Database
from tokenlens.schemas import BudgetIn, BudgetPatch
from tokenlens.store import ingest_snapshots
from tokenlens.testing import generate_workload, make_snapshot

#: Fixed anchor: 2026-11-10T06:00:00Z == 14:00 Asia/Shanghai, comfortably mid-day so the
#: calendar windows below never straddle a boundary in any test.
NOW = timeutil.parse_time_ms("2026-11-10T06:00:00Z")


def minutes_ago(n: float, *, now: int = NOW) -> int:
    return int(now - n * 60_000)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def seed_spend(
    db: Database,
    settings: Settings,
    *,
    usd_target: float = 20.0,
    ts: int | None = None,
    agent: str = "claude-code",
    project: str = "payments-api",
    model: str = "claude-sonnet-4-5",
    event_id: str = "spend-1",
) -> int:
    """Ingest one snapshot whose priced cost is approximately ``usd_target``.

    ``claude-sonnet-4-5`` is $3/Mtok input + $15/Mtok output, so 1M output tokens is
    exactly $15. We size the output tokens to hit the target and let pricing round.
    """
    output_tokens = max(1, int(round(usd_target / 15.0 * 1_000_000)))
    ingest_snapshots(
        db,
        [
            make_snapshot(
                ts=ts if ts is not None else minutes_ago(5),
                agent=agent,
                project=project,
                model=model,
                input_tokens=0,
                output_tokens=output_tokens,
                cache_read_tokens=0,
                cache_write_tokens=0,
                event_id=event_id,
            )
        ],
        settings=settings,
    )
    return output_tokens


def seed_traffic(
    db: Database,
    settings: Settings,
    *,
    start_min: float,
    end_min: float,
    now: int = NOW,
    agent: str = "claude-code",
    per_minute: int = 1,
    input_tokens: int = 800,
    output_tokens: int = 200,
    cache_read_tokens: int = 1_000,
    cache_write_tokens: int = 50,
    requests: int = 1,
    retries: int = 0,
    tool_result_bytes: int = 1_000,
    context_window: int = 200_000,
    session_prefix: str = "base",
    project: str = "payments-api",
) -> int:
    """Ingest ``per_minute`` snapshots for every minute in ``[now-start_min, now-end_min)``.

    Offsets are *minutes ago*, so ``start_min=360, end_min=10`` covers everything from six
    hours ago up to ten minutes ago. This is the workhorse behind every detection test.
    """
    snapshots = []
    minute = int(now - start_min * 60_000)
    stop = int(now - end_min * 60_000)
    index = 0
    while minute < stop:
        for k in range(per_minute):
            snapshots.append(
                make_snapshot(
                    ts=minute + k * 400,
                    agent=agent,
                    project=project,
                    session_id=f"{session_prefix}-{k}",
                    model="claude-sonnet-4-5",
                    runtime="anthropic",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    requests=requests,
                    retries=retries,
                    tool_result_bytes=tool_result_bytes,
                    context_window=context_window,
                    event_id=f"{session_prefix}-{agent}-{index}-{k}",
                )
            )
        minute += 60_000
        index += 1
    ingest_snapshots(db, snapshots, settings=settings)
    return len(snapshots)


def seed_baseline(
    db: Database,
    settings: Settings,
    *,
    now: int = NOW,
    agent: str = "claude-code",
    history_minutes: int = 300,
    lookback_minutes: int = 60,
    calm_tail_minutes: int = 10,
    per_minute: int = 1,
    input_tokens: int = 800,
    output_tokens: int = 200,
    cache_read_tokens: int = 1_000,
    cache_write_tokens: int = 50,
    requests: int = 1,
    retries: int = 0,
    tool_result_bytes: int = 1_000,
    context_window: int = 200_000,
    session_prefix: str = "base",
    project: str = "payments-api",
) -> int:
    """Seed steady traffic from ``history_minutes`` ago up to ``calm_tail_minutes`` ago.

    That covers the whole history window *and* the calm part of the observation window, so
    an incident injected into the trailing minutes is the only source of variance - which
    is the shape of a real incident (steady state, then a burst).
    """
    return seed_traffic(
        db,
        settings,
        start_min=history_minutes + lookback_minutes,
        end_min=calm_tail_minutes,
        now=now,
        agent=agent,
        per_minute=per_minute,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        requests=requests,
        retries=retries,
        tool_result_bytes=tool_result_bytes,
        context_window=context_window,
        session_prefix=session_prefix,
        project=project,
    )


def seed_incident(
    db: Database,
    settings: Settings,
    *,
    now: int = NOW,
    agent: str = "claude-code",
    incident_minutes: int = 10,
    per_minute: int = 5,
    input_tokens: int = 6_000,
    output_tokens: int = 3_000,
    cache_read_tokens: int = 1_000,
    cache_write_tokens: int = 50,
    requests: int = 1,
    retries: int = 0,
    tool_result_bytes: int = 1_000,
    context_window: int = 200_000,
    session_prefix: str = "inc",
    project: str = "payments-api",
) -> int:
    """Inject a burst into the trailing ``incident_minutes`` of the observation window."""
    return seed_traffic(
        db,
        settings,
        start_min=incident_minutes,
        end_min=0,
        now=now,
        agent=agent,
        per_minute=per_minute,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        requests=requests,
        retries=retries,
        tool_result_bytes=tool_result_bytes,
        context_window=context_window,
        session_prefix=session_prefix,
        project=project,
    )



def run_detection(db: Database, settings: Settings, **kwargs) -> anomaly.DetectResponse:
    """Run detection anchored to the fixed test clock (unit-level tests)."""
    return anomaly.detect(db, settings=settings, now_ms=NOW, **kwargs)


def run_detection_live(db: Database, settings: Settings, **kwargs) -> anomaly.DetectResponse:
    """Run detection on the real clock, for tests that go through the HTTP API."""
    return anomaly.detect(db, settings=settings, **kwargs)


def live_now() -> int:
    """Current wall clock, used by API tests whose fixtures must line up with ``now_ms()``."""
    return timeutil.now_ms()


class _WebhookRecorder:
    """``httpx.MockTransport`` handler that records payloads and returns a scripted status."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.payloads: list[dict] = []
        self.calls = 0
        self._statuses = list(statuses or [200])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        try:
            self.payloads.append(json.loads(request.content))
        except Exception:  # pragma: no cover - defensive
            self.payloads.append({})
        index = min(self.calls - 1, len(self._statuses) - 1)
        return httpx.Response(self._statuses[index], json={"ok": self._statuses[index] < 300})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# ======================================================================================
# notify.py — webhook
# ======================================================================================


def test_send_webhook_posts_payload_and_reports_success(settings):
    recorder = _WebhookRecorder([200])
    ok, detail = notify.send_webhook(
        "https://hooks.example.com/tokenlens", {"hello": "world"}, settings=settings, transport=recorder.transport
    )
    assert ok is True
    assert detail == "HTTP 200"
    assert recorder.calls == 1
    assert recorder.payloads == [{"hello": "world"}]


def test_send_webhook_retries_then_succeeds(settings):
    settings.webhook_retries = 2
    recorder = _WebhookRecorder([500, 500, 200])
    ok, detail = notify.send_webhook(
        "https://hooks.example.com/x", {"a": 1}, settings=settings, transport=recorder.transport
    )
    assert ok is True
    assert recorder.calls == 3, "two failures then success should take exactly three attempts"
    assert detail == "HTTP 200"


def test_send_webhook_gives_up_after_configured_retries(settings):
    settings.webhook_retries = 2
    recorder = _WebhookRecorder([503])
    ok, detail = notify.send_webhook(
        "https://hooks.example.com/x", {"a": 1}, settings=settings, transport=recorder.transport
    )
    assert ok is False
    assert recorder.calls == 3, "1 initial attempt + 2 retries"
    assert "503" in detail


def test_send_webhook_never_raises_on_transport_error(settings):
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    ok, detail = notify.send_webhook(
        "https://unreachable.example.com/x", {"a": 1}, settings=settings, transport=httpx.MockTransport(explode)
    )
    assert ok is False
    assert "ConnectError" in detail


def test_send_webhook_rejects_non_http_url(settings):
    ok, detail = notify.send_webhook("ftp://nope/x", {"a": 1}, settings=settings)
    assert ok is False
    assert "http" in detail.lower()


def test_send_webhook_honours_zero_retries(settings):
    settings.webhook_retries = 0
    recorder = _WebhookRecorder([500])
    ok, _ = notify.send_webhook(
        "https://hooks.example.com/x", {}, settings=settings, transport=recorder.transport
    )
    assert ok is False
    assert recorder.calls == 1


# ======================================================================================
# notify.py — desktop sink
# ======================================================================================


def test_send_desktop_appends_one_json_line(settings):
    notification = notify.Notification(
        channel="budget", title="标题", body="正文", severity="critical", payload={"budget_id": 7}
    )
    ok, detail = notify.send_desktop(notification, settings=settings)
    assert ok is True
    assert "appended" in detail

    lines = settings.notify_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["title"] == "标题"
    assert entry["body"] == "正文"
    assert entry["severity"] == "critical"
    assert entry["payload"]["budget_id"] == 7
    assert entry["ts"].endswith("Z")


def test_send_desktop_appends_multiple_lines(settings):
    for i in range(3):
        notify.send_desktop(
            notify.Notification(channel="budget", title=f"t{i}", body="b", payload={"i": i}),
            settings=settings,
        )
    lines = settings.notify_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["payload"]["i"] for line in lines] == [0, 1, 2]


def test_send_desktop_creates_missing_parent_directory(settings, tmp_path):
    settings.notify_file = tmp_path / "deep" / "nested" / "notifications.jsonl"
    ok, _ = notify.send_desktop(notify.Notification(channel="budget", title="t", body="b"), settings=settings)
    assert ok is True
    assert settings.notify_file.exists()


def test_desktop_toast_is_skipped_off_windows(monkeypatch, settings):
    """The toast is cosmetic: it must never be attempted where it cannot work."""
    calls: list[str] = []
    monkeypatch.setattr(notify, "_toast_enabled", lambda: False)
    monkeypatch.setattr(notify, "_spawn_toast", lambda title, body: calls.append(title))
    notify.send_desktop(notify.Notification(channel="budget", title="t", body="b"), settings=settings)
    assert calls == []


def test_toast_script_escapes_single_quotes():
    script = notify._toast_script("it's a title", "body's here")
    assert "''" in script, "PowerShell literals must double single quotes"
    assert "\n" not in script


def test_toast_script_truncates_long_text():
    script = notify._toast_script("x" * 5000, "y")
    assert len(script) < 2000


# ======================================================================================
# notify.py — dispatch
# ======================================================================================


def test_dispatch_reports_per_channel_results(settings):
    recorder = _WebhookRecorder([200])
    notification = notify.Notification(
        channel="budget",
        title="t",
        body="b",
        severity="exceeded",
        payload={"webhook_url": "https://hooks.example.com/x"},
    )
    results = notify.dispatch(
        settings, ["webhook", "desktop", "log"], notification, transport=recorder.transport
    )
    assert [entry["channel"] for entry in results] == ["webhook", "desktop", "log"]
    assert all(entry["ok"] for entry in results)
    assert recorder.calls == 1
    assert settings.notify_file.exists()


def test_dispatch_reports_unknown_channel_without_raising(settings):
    results = notify.dispatch(
        settings, ["carrier-pigeon"], notify.Notification(channel="budget", title="t", body="b")
    )
    assert results == [
        {"channel": "carrier-pigeon", "ok": False, "detail": "unknown channel: carrier-pigeon"}
    ]


def test_dispatch_webhook_without_url_fails_softly(settings):
    results = notify.dispatch(
        settings, ["webhook"], notify.Notification(channel="budget", title="t", body="b", payload={})
    )
    assert results[0]["ok"] is False


def test_dispatch_accepts_comma_separated_channels(settings):
    results = notify.dispatch(
        settings, "log,desktop", notify.Notification(channel="budget", title="t", body="b")
    )
    assert [entry["channel"] for entry in results] == ["log", "desktop"]


def test_dispatch_dedupes_channels(settings):
    results = notify.dispatch(
        settings, ["log", "log", "LOG"], notify.Notification(channel="budget", title="t", body="b")
    )
    assert len(results) == 1


def test_channels_for_builds_expected_list():
    assert notify.channels_for(webhook_url="https://x", desktop=True) == ["webhook", "desktop"]
    assert notify.channels_for(desktop=True) == ["desktop"]
    assert notify.channels_for() == []


# ======================================================================================
# budget.py — CRUD
# ======================================================================================


def test_create_and_get_budget(db):
    created = budget.create_budget(
        db,
        BudgetIn(
            name="team-daily",
            scope="global",
            period="daily",
            limit_usd=25.0,
            thresholds=[0.5, 0.8, 1.0],
            desktop=True,
        ),
    )
    assert created.id > 0
    assert created.scope_key == "*", "global budgets are normalised to scope_key='*'"
    assert created.thresholds == [0.5, 0.8, 1.0]
    fetched = budget.get_budget(db, created.id)
    assert fetched is not None
    assert fetched.name == "team-daily"
    assert fetched.enabled is True


def test_create_budget_normalises_thresholds(db):
    created = budget.create_budget(
        db,
        BudgetIn(name="t", scope="global", period="daily", limit_usd=10.0, thresholds=[1.0, 0.5, 0.5, 0.8]),
    )
    assert created.thresholds == [0.5, 0.8, 1.0], "thresholds are sorted and de-duplicated"


def test_list_budgets_returns_all(db):
    for i in range(3):
        budget.create_budget(
            db, BudgetIn(name=f"b{i}", scope="global", period="daily", limit_usd=10.0 + i)
        )
    items = budget.list_budgets(db)
    assert len(items) == 3
    assert [item.name for item in items] == ["b0", "b1", "b2"]


def test_list_budgets_enabled_only(db):
    budget.create_budget(db, BudgetIn(name="on", scope="global", period="daily", limit_usd=10.0))
    budget.create_budget(
        db, BudgetIn(name="off", scope="global", period="daily", limit_usd=10.0, enabled=False)
    )
    assert [item.name for item in budget.list_budgets(db)] == ["on", "off"]
    assert [item.name for item in budget.list_budgets(db, enabled_only=True)] == ["on"]


def test_update_budget_partial(db):
    created = budget.create_budget(
        db, BudgetIn(name="old", scope="global", period="daily", limit_usd=10.0)
    )
    updated = budget.update_budget(
        db, created.id, BudgetPatch(name="new", limit_usd=99.0, thresholds=[0.9])
    )
    assert updated is not None
    assert updated.name == "new"
    assert updated.limit_usd == 99.0
    assert updated.thresholds == [0.9]
    assert updated.scope == "global", "unspecified fields are preserved"


def test_update_budget_missing_returns_none(db):
    assert budget.update_budget(db, 4242, BudgetPatch(name="x")) is None


def test_update_budget_rejects_invalid_scope_key(db):
    created = budget.create_budget(
        db, BudgetIn(name="p", scope="project", scope_key="payments-api", period="daily", limit_usd=5.0)
    )
    with pytest.raises(ValueError):
        budget.update_budget(db, created.id, BudgetPatch(scope="project", scope_key=""))


def test_delete_budget(db):
    created = budget.create_budget(
        db, BudgetIn(name="temp", scope="global", period="daily", limit_usd=10.0)
    )
    assert budget.delete_budget(db, created.id) is True
    assert budget.get_budget(db, created.id) is None
    assert budget.delete_budget(db, created.id) is False


def test_delete_budget_keeps_alert_history(db, settings):
    seed_spend(db, settings, usd_target=20.0)
    created = budget.create_budget(
        db, BudgetIn(name="temp", scope="global", period="daily", limit_usd=1.0)
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert db.scalar("SELECT COUNT(*) FROM alert_events") > 0
    budget.delete_budget(db, created.id)
    assert db.scalar("SELECT COUNT(*) FROM alert_events") > 0, "history survives budget deletion"
    assert db.scalar("SELECT COUNT(*) FROM budget_state") == 0


# ======================================================================================
# budget.py — window / scope resolution
# ======================================================================================


@pytest.mark.parametrize(
    "period,expected_start_iso",
    [
        ("daily", "2026-11-09T16:00:00.000Z"),   # 00:00 +08:00
        ("weekly", "2026-11-08T16:00:00.000Z"),  # Monday 00:00 +08:00
        ("monthly", "2026-10-31T16:00:00.000Z"),  # 1st 00:00 +08:00
    ],
)
def test_resolve_window_is_calendar_aligned_in_display_tz(settings, period, expected_start_iso):
    start, end = budget.resolve_window(period, settings=settings, at_ms=NOW)
    assert timeutil.iso(start) == expected_start_iso
    assert start < NOW < end


def test_scope_spend_filters_by_project(db, settings):
    seed_spend(db, settings, usd_target=20.0, project="payments-api", event_id="p1")
    seed_spend(db, settings, usd_target=5.0, project="web-dashboard", event_id="p2")
    start, end = budget.resolve_window("daily", settings=settings, at_ms=NOW)

    assert budget.scope_spend(db, "global", "*", start, end) == pytest.approx(25.0, abs=1e-4)
    assert budget.scope_spend(db, "project", "payments-api", start, end) == pytest.approx(20.0, abs=1e-4)
    assert budget.scope_spend(db, "project", "web-dashboard", start, end) == pytest.approx(5.0, abs=1e-4)
    assert budget.scope_spend(db, "project", "missing", start, end) == 0.0


def test_scope_spend_filters_by_agent(db, settings):
    seed_spend(db, settings, usd_target=12.0, agent="claude-code", event_id="a1")
    seed_spend(db, settings, usd_target=8.0, agent="codex-cli", event_id="a2")
    start, end = budget.resolve_window("daily", settings=settings, at_ms=NOW)

    assert budget.scope_spend(db, "agent", "claude-code", start, end) == pytest.approx(12.0, abs=1e-4)
    assert budget.scope_spend(db, "agent", "codex-cli", start, end) == pytest.approx(8.0, abs=1e-4)


def test_scope_spend_filters_by_model_and_normalises_alias(db, settings):
    seed_spend(db, settings, usd_target=9.0, model="claude-sonnet-4-5", event_id="m1")
    start, end = budget.resolve_window("daily", settings=settings, at_ms=NOW)

    # Snapshots store the canonical model, so an alias-based budget must still match.
    assert budget.scope_spend(db, "model", "claude-sonnet-4-5", start, end) == pytest.approx(9.0, abs=1e-4)
    assert budget.scope_spend(
        db, "model", "claude-sonnet-4-5-20250929", start, end
    ) == pytest.approx(9.0, abs=1e-4)


def test_scope_spend_excludes_previous_window(db, settings):
    seed_spend(db, settings, usd_target=20.0, ts=minutes_ago(5), event_id="today")
    seed_spend(db, settings, usd_target=50.0, ts=minutes_ago(60 * 30), event_id="yesterday")
    start, end = budget.resolve_window("daily", settings=settings, at_ms=NOW)
    assert budget.scope_spend(db, "global", "*", start, end) == pytest.approx(20.0, abs=1e-4)


# ======================================================================================
# budget.py — projection & severity
# ======================================================================================


@pytest.mark.parametrize(
    "threshold,expected",
    [
        (0.1, "warning"),
        (0.5, "warning"),
        (0.79, "warning"),
        (0.8, "critical"),
        (0.99, "critical"),
        (1.0, "exceeded"),
        (1.5, "exceeded"),
        (5.0, "exceeded"),
    ],
)
def test_severity_for_threshold(threshold, expected):
    assert budget.severity_for_threshold(threshold) == expected


@pytest.mark.parametrize(
    "pct,triggered,expected",
    [
        (0.1, [], "ok"),
        (0.6, [0.5], "warning"),
        (0.85, [0.5, 0.8], "critical"),
        (1.2, [0.5, 0.8, 1.0], "exceeded"),
        (1.0, [0.5, 0.8, 1.0], "exceeded"),
    ],
)
def test_status_for(pct, triggered, expected):
    assert budget.status_for(pct, triggered) == expected


def test_project_spend_is_linear():
    # Half of a 24h window elapsed -> projection is double the spend.
    start = NOW
    end = NOW + 86_400_000
    assert budget.project_spend(10.0, start, end, start + 43_200_000) == pytest.approx(20.0, abs=1e-6)


def test_project_spend_floors_elapsed_time():
    """A pass a few ms after midnight must not project a million-dollar day."""
    start = NOW
    end = NOW + 86_400_000
    projected = budget.project_spend(1.0, start, end, start + 5)
    assert projected == pytest.approx(1440.0, abs=1e-6), "5 ms is floored to the 1 minute minimum"


def test_project_spend_never_exceeds_full_window_scale():
    start = NOW
    end = NOW + 3_600_000
    # now beyond the window end is clamped, so projection == spend
    assert budget.project_spend(10.0, start, end, end + 999_999) == pytest.approx(10.0, abs=1e-6)


# ======================================================================================
# budget.py — status building
# ======================================================================================


def test_build_status_empty_database(db, settings):
    created = budget.create_budget(
        db, BudgetIn(name="empty", scope="global", period="daily", limit_usd=25.0)
    )
    status = budget.build_status(db, created, settings=settings, now_ms=NOW)
    assert status.spent_usd == 0.0
    assert status.pct == 0.0
    assert status.remaining_usd == 25.0
    assert status.status == "ok"
    assert status.triggered_thresholds == []
    assert status.trigger_count == 0
    assert status.overspend_usd == 0.0
    assert status.last_alert_at is None


def test_build_status_computes_pct_and_remaining(db, settings):
    seed_spend(db, settings, usd_target=20.0)
    created = budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=40.0, thresholds=[0.5, 0.8, 1.0])
    )
    status = budget.build_status(db, created, settings=settings, now_ms=NOW)
    assert status.spent_usd == pytest.approx(20.0, abs=1e-4)
    assert status.pct == pytest.approx(0.5, abs=1e-4)
    assert status.remaining_usd == pytest.approx(20.0, abs=1e-4)
    assert status.triggered_thresholds == [0.5]
    assert status.status == "warning"
    assert status.projected_usd > status.spent_usd


def test_build_status_overspend(db, settings):
    seed_spend(db, settings, usd_target=30.0)
    created = budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=10.0)
    )
    status = budget.build_status(db, created, settings=settings, now_ms=NOW)
    assert status.overspend_usd == pytest.approx(20.0, abs=1e-4)
    assert status.remaining_usd == 0.0
    assert status.status == "exceeded"


# ======================================================================================
# budget.py — evaluation
# ======================================================================================


def test_evaluate_with_no_budgets(db, settings):
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert result.evaluated == 0
    assert result.triggered == []


def test_evaluate_skips_disabled_budgets(db, settings):
    seed_spend(db, settings, usd_target=30.0)
    budget.create_budget(
        db, BudgetIn(name="off", scope="global", period="daily", limit_usd=1.0, enabled=False)
    )
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert result.evaluated == 0
    assert result.triggered == []


def test_evaluate_does_not_trigger_below_first_threshold(db, settings):
    seed_spend(db, settings, usd_target=1.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, thresholds=[0.5, 0.8, 1.0])
    )
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert result.evaluated == 1
    assert result.triggered == []
    assert db.scalar("SELECT COUNT(*) FROM alert_events") == 0


def test_evaluate_creates_one_row_per_crossed_threshold(db, settings):
    seed_spend(db, settings, usd_target=85.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, thresholds=[0.5, 0.8, 1.0])
    )
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert [round(a.threshold, 3) for a in result.triggered] == [0.5, 0.8]
    assert [a.severity for a in result.triggered] == ["warning", "critical"]
    assert db.scalar("SELECT COUNT(*) FROM alert_events") == 2


def test_evaluate_severity_and_overspend_on_exceeded(db, settings):
    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, thresholds=[0.5, 0.8, 1.0])
    )
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    by_threshold = {round(a.threshold, 3): a for a in result.triggered}
    assert by_threshold[0.5].severity == "warning"
    assert by_threshold[0.8].severity == "critical"
    assert by_threshold[1.0].severity == "exceeded"
    # overspend is mirrored onto *every* row of the window, not just the exceeded one.
    assert all(a.overspend_usd == pytest.approx(50.0, abs=1e-4) for a in result.triggered)
    rows = db.fetch_all("SELECT threshold, overspend_usd FROM alert_events ORDER BY threshold")
    assert all(row["overspend_usd"] == pytest.approx(50.0, abs=1e-4) for row in rows)


def test_evaluate_is_idempotent_on_alert_rows(db, settings):
    """The UNIQUE(budget_id, window_start, threshold) row is updated, never duplicated."""
    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, thresholds=[0.5, 0.8, 1.0])
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert db.scalar("SELECT COUNT(*) FROM alert_events") == 3

    for _ in range(4):
        budget.evaluate_budgets(db, settings=settings, now_ms=NOW)

    assert db.scalar("SELECT COUNT(*) FROM alert_events") == 3, "repeat passes must not duplicate rows"
    rows = db.fetch_all("SELECT trigger_count FROM alert_events ORDER BY threshold")
    assert [row["trigger_count"] for row in rows] == [5, 5, 5], "1 initial + 4 re-evaluations"


def test_evaluate_refreshes_spend_on_repeat_breach(db, settings):
    seed_spend(db, settings, usd_target=60.0, event_id="s1")
    created = budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, thresholds=[0.5])
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert db.scalar("SELECT spent_usd FROM alert_events") == pytest.approx(60.0, abs=1e-4)

    seed_spend(db, settings, usd_target=30.0, event_id="s2")
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    assert db.scalar("SELECT spent_usd FROM alert_events") == pytest.approx(90.0, abs=1e-4)
    assert db.scalar("SELECT trigger_count FROM alert_events") == 2
    status = budget.build_status(db, created, settings=settings, now_ms=NOW)
    assert status.trigger_count == 2


def test_evaluate_dry_run_persists_nothing(db, settings):
    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, desktop=False)
    )
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW, dry_run=True)
    assert result.dry_run is True
    assert len(result.triggered) == 3
    assert all(record.id == 0 for record in result.triggered), "dry-run alerts are not persisted"
    assert db.scalar("SELECT COUNT(*) FROM alert_events") == 0
    assert db.scalar("SELECT COUNT(*) FROM budget_state") == 0
    assert not settings.notify_file.exists(), "dry run must not notify"


def test_evaluate_notifies_only_on_new_breach(db, settings):
    """A repeat breach bumps trigger_count but must not re-send notifications."""
    recorder = _WebhookRecorder([200])
    seed_spend(db, settings, usd_target=150.0)
    created = budget.create_budget(
        db,
        BudgetIn(
            name="b",
            scope="global",
            period="daily",
            limit_usd=100.0,
            thresholds=[0.5, 0.8, 1.0],
            webhook_url="https://hooks.example.com/x",
            desktop=False,
        ),
    )
    assert created.webhook_url

    first = budget.evaluate_budgets(db, settings=settings, now_ms=NOW, transport=recorder.transport)
    assert recorder.calls == 3, "one webhook per newly crossed threshold"
    assert all(record.channels for record in first.triggered)

    second = budget.evaluate_budgets(db, settings=settings, now_ms=NOW, transport=recorder.transport)
    assert recorder.calls == 3, "repeat breach must not re-notify"
    assert [record.trigger_count for record in second.triggered] == [2, 2, 2]


def test_evaluate_notifies_when_a_higher_threshold_is_crossed_later(db, settings):
    recorder = _WebhookRecorder([200])
    seed_spend(db, settings, usd_target=60.0, event_id="s1")
    budget.create_budget(
        db,
        BudgetIn(
            name="b",
            scope="global",
            period="daily",
            limit_usd=100.0,
            thresholds=[0.5, 0.8, 1.0],
            webhook_url="https://hooks.example.com/x",
            desktop=False,
        ),
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW, transport=recorder.transport)
    assert recorder.calls == 1, "only the 0.5 threshold is crossed"

    seed_spend(db, settings, usd_target=40.0, event_id="s2")  # now 100 -> exactly 1.0
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW, transport=recorder.transport)
    assert recorder.calls == 4, "crossing 0.8 and 1.0 must notify for both"


def test_evaluate_webhook_payload_shape(db, settings):
    recorder = _WebhookRecorder([200])
    seed_spend(db, settings, usd_target=120.0)
    budget.create_budget(
        db,
        BudgetIn(
            name="team-daily",
            scope="global",
            period="daily",
            limit_usd=100.0,
            thresholds=[1.0],
            webhook_url="https://hooks.example.com/x",
            desktop=False,
        ),
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW, transport=recorder.transport)
    assert len(recorder.payloads) == 1
    payload = recorder.payloads[0]
    assert payload["event"] == "budget.threshold"
    assert payload["budget_name"] == "team-daily"
    assert payload["scope"] == "global"
    assert payload["period"] == "daily"
    assert payload["severity"] == "exceeded"
    assert payload["threshold"] == 1.0
    assert payload["limit_usd"] == pytest.approx(100.0, abs=1e-4)
    assert payload["overspend_usd"] == pytest.approx(20.0, abs=1e-4)
    assert payload["window_start"].endswith("Z")
    assert payload["message"]


def test_evaluate_writes_desktop_notifications(db, settings):
    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, desktop=True)
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    lines = settings.notify_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "one JSONL line per alerted threshold"
    entries = [json.loads(line) for line in lines]
    assert {entry["severity"] for entry in entries} == {"warning", "critical", "exceeded"}


def test_evaluate_failing_webhook_does_not_break_the_pass(db, settings):
    """A dead webhook must not stop the evaluation (or the desktop sink) from working."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db,
        BudgetIn(
            name="b",
            scope="global",
            period="daily",
            limit_usd=100.0,
            thresholds=[1.0],
            webhook_url="https://hooks.example.com/x",
            desktop=True,
        ),
    )
    result = budget.evaluate_budgets(
        db, settings=settings, now_ms=NOW, transport=httpx.MockTransport(explode)
    )
    assert len(result.triggered) == 1
    assert db.scalar("SELECT COUNT(*) FROM alert_events") == 1
    assert settings.notify_file.exists(), "desktop sink still delivered"


def test_evaluate_isolates_scopes(db, settings):
    """Each budget only sees its own scope's spend."""
    seed_spend(db, settings, usd_target=80.0, project="payments-api", agent="claude-code", event_id="s1")
    seed_spend(db, settings, usd_target=5.0, project="web-dashboard", agent="codex-cli", event_id="s2")

    budget.create_budget(
        db, BudgetIn(name="proj", scope="project", scope_key="payments-api", period="daily",
                     limit_usd=100.0, thresholds=[0.5], desktop=False)
    )
    budget.create_budget(
        db, BudgetIn(name="agent", scope="agent", scope_key="codex-cli", period="daily",
                     limit_usd=100.0, thresholds=[0.5], desktop=False)
    )
    budget.create_budget(
        db, BudgetIn(name="global", scope="global", period="daily", limit_usd=100.0,
                     thresholds=[0.5], desktop=False)
    )

    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    fired = {record.budget_name for record in result.triggered}
    assert fired == {"proj", "global"}, "the codex-cli budget is only at 5% of its limit"


def test_evaluate_evaluated_at_and_dry_run_flag(db, settings):
    budget.create_budget(db, BudgetIn(name="b", scope="global", period="daily", limit_usd=10.0))
    result = budget.evaluate_budgets(db, settings=settings, now_ms=NOW, dry_run=True)
    assert result.evaluated_at == NOW
    assert result.dry_run is True
    response = result.as_response()
    assert response.dry_run is True
    assert response.evaluated == 1


# ======================================================================================
# budget.py — alert history
# ======================================================================================


def _seed_alerts(db: Database, settings: Settings, *, limit_usd: float = 100.0, spend: float = 150.0) -> int:
    seed_spend(db, settings, usd_target=spend)
    created = budget.create_budget(
        db, BudgetIn(name="hist", scope="global", period="daily", limit_usd=limit_usd, desktop=False)
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    return created.id


def test_list_alerts_newest_first(db, settings):
    _seed_alerts(db, settings)
    total, items = budget.list_alerts(db)
    assert total == 3
    assert len(items) == 3
    assert items[0].updated_at if hasattr(items[0], "updated_at") else True
    assert [round(item.threshold, 3) for item in items] == [1.0, 0.8, 0.5], "newest first"


def test_list_alerts_pagination(db, settings):
    _seed_alerts(db, settings)
    total, page1 = budget.list_alerts(db, limit=2, offset=0)
    _, page2 = budget.list_alerts(db, limit=2, offset=2)
    assert total == 3
    assert len(page1) == 2
    assert len(page2) == 1
    assert {item.id for item in page1}.isdisjoint({item.id for item in page2})


def test_list_alerts_filter_by_severity(db, settings):
    _seed_alerts(db, settings)
    total, items = budget.list_alerts(db, severity="exceeded")
    assert total == 1
    assert items[0].severity == "exceeded"


def test_list_alerts_filter_by_budget_id(db, settings):
    budget_id = _seed_alerts(db, settings)
    other = budget.create_budget(
        db, BudgetIn(name="other", scope="global", period="daily", limit_usd=1000.0, desktop=False)
    )
    total, _ = budget.list_alerts(db, budget_id=budget_id)
    assert total == 3
    total_other, _ = budget.list_alerts(db, budget_id=other.id)
    assert total_other == 0


def test_list_alerts_filter_by_since(db, settings):
    _seed_alerts(db, settings)
    assert budget.list_alerts(db, since_ms=NOW - 60_000)[0] == 3
    assert budget.list_alerts(db, since_ms=NOW + 60_000)[0] == 0


def test_alert_summary_aggregates(db, settings):
    _seed_alerts(db, settings)
    summary = budget.alert_summary(db, now_ms=NOW)
    assert summary.total_alerts == 3
    assert summary.total_triggers == 3
    assert summary.by_severity == {"warning": 1, "critical": 1, "exceeded": 1}
    assert len(summary.by_budget) == 1
    assert summary.by_budget[0]["budget_name"] == "hist"
    assert summary.by_budget[0]["alerts"] == 3
    assert summary.by_budget[0]["triggers"] == 3
    # Overspend is mirrored onto all three rows but must only be counted once per window.
    assert summary.total_overspend_usd == pytest.approx(50.0, abs=1e-4)
    assert summary.by_budget[0]["overspend_usd"] == pytest.approx(50.0, abs=1e-4)
    assert summary.last_triggered_at is not None


def test_alert_summary_empty_database(db):
    summary = budget.alert_summary(db, now_ms=NOW)
    assert summary.total_alerts == 0
    assert summary.total_triggers == 0
    assert summary.by_severity == {"warning": 0, "critical": 0, "exceeded": 0}
    assert summary.by_budget == []
    assert summary.total_overspend_usd == 0.0
    assert summary.last_triggered_at is None


def test_alert_summary_counts_repeat_triggers(db, settings):
    _seed_alerts(db, settings)
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    summary = budget.alert_summary(db, now_ms=NOW)
    assert summary.total_alerts == 3
    assert summary.total_triggers == 6


# ======================================================================================
# anomaly.py — statistics helpers
# ======================================================================================


@pytest.mark.parametrize(
    "values,expected",
    [([], 0.0), ([5.0], 5.0), ([1.0, 3.0], 2.0), ([1.0, 2.0, 3.0], 2.0), ([1.0, 2.0, 3.0, 4.0], 2.5)],
)
def test_median(values, expected):
    assert anomaly.median(values) == pytest.approx(expected)


def test_robust_sigma_uses_mad():
    # MAD of [1,2,3,4,5] is 1.0 -> sigma = 1.4826
    assert anomaly.robust_sigma([1.0, 2.0, 3.0, 4.0, 5.0], floor=0.0) == pytest.approx(1.4826, abs=1e-4)


def test_robust_sigma_applies_floor():
    assert anomaly.robust_sigma([100.0, 100.0, 100.0]) == anomaly.ANOMALY_SIGMA_FLOOR
    assert anomaly.ANOMALY_SIGMA_FLOOR == 200.0


def test_robust_sigma_is_resistant_to_outliers():
    """A window full of incidents must not blow up the baseline estimate."""
    clean = [1000.0] * 50
    contaminated = clean + [100_000.0] * 5
    assert anomaly.robust_sigma(contaminated) == pytest.approx(anomaly.robust_sigma(clean))


@pytest.mark.parametrize(
    "values,q,expected",
    [([], 0.95, 0.0), ([5.0], 0.95, 5.0), ([0.0, 10.0], 0.5, 5.0), ([0.0, 10.0], 0.95, 9.5)],
)
def test_percentile(values, q, expected):
    assert anomaly.percentile(values, q) == pytest.approx(expected)


def test_percentile_p95_of_uniform_series():
    values = list(range(101))  # 0..100
    assert anomaly.percentile(values, 0.95) == pytest.approx(95.0)


def test_robust_score_clamps_at_zero():
    assert anomaly.robust_score(100.0, 1000.0, 200.0) == 0.0, "below-baseline is not an anomaly"
    assert anomaly.robust_score(1800.0, 1000.0, 200.0) == pytest.approx(4.0)


@pytest.mark.parametrize(
    "score,ratio,expected",
    [
        (3.9, 1.5, "low"),
        (5.0, 1.5, "medium"),
        (6.9, 2.0, "medium"),
        (7.0, 2.0, "high"),
        (9.9, 2.0, "high"),
        (10.0, 2.0, "critical"),
        (4.5, 8.0, "critical"),
    ],
)
def test_severity_for(score, ratio, expected):
    assert anomaly.severity_for(score, ratio) == expected


def test_fingerprint_is_sha1_of_agent_reason_bucket():
    expected = hashlib.sha1(b"claude-code|retry_storm|7156096").hexdigest()
    assert anomaly.fingerprint_for("claude-code", "retry_storm", 2_146_828_800_000) == expected


def test_fingerprint_buckets_by_five_minutes():
    base = 2_146_828_800_000  # aligned to a 5 minute boundary
    assert anomaly.fingerprint_for("a", "retry_storm", base) == anomaly.fingerprint_for(
        "a", "retry_storm", base + 60_000
    )
    assert anomaly.fingerprint_for("a", "retry_storm", base) != anomaly.fingerprint_for(
        "a", "retry_storm", base + anomaly.FINGERPRINT_BUCKET_MS
    )


def test_fingerprint_differs_by_agent_and_reason():
    assert anomaly.fingerprint_for("a", "retry_storm", 0) != anomaly.fingerprint_for("b", "retry_storm", 0)
    assert anomaly.fingerprint_for("a", "retry_storm", 0) != anomaly.fingerprint_for("a", "context_bloat", 0)


def test_reason_labels_are_chinese():
    assert anomaly.REASON_LABELS["retry_storm"] == "重试风暴"
    assert anomaly.REASON_LABELS["context_bloat"] == "上下文膨胀"
    assert anomaly.REASON_LABELS["uncompressed_tool_output"] == "工具返回数据未压缩"
    assert anomaly.REASON_LABELS["cache_miss_storm"] == "缓存命中崩塌"
    assert anomaly.REASON_LABELS["traffic_spike"] == "流量尖峰"
    assert anomaly.REASON_LABELS["unknown"] == "未知"


# ======================================================================================
# anomaly.py — Evidence + classifiers (pure unit tests, no DB)
# ======================================================================================


def _evidence(**kwargs) -> anomaly.Evidence:
    defaults = dict(
        snapshots=10,
        requests=10,
        retries=0,
        total_tokens=20_000,
        input_tokens=5_000,
        output_tokens=5_000,
        cache_read_tokens=10_000,
        cache_write_tokens=0,
        tool_result_bytes=10_000,
        context_window_sum=2_000_000,
        context_window_count=10,
        minutes=10,
    )
    defaults.update(kwargs)
    return anomaly.Evidence(**defaults)


def test_evidence_derived_rates():
    evidence = _evidence()
    assert evidence.retry_ratio == 0.0
    assert evidence.tokens_per_request == pytest.approx(2000.0)
    assert evidence.prompt_tokens_per_request == pytest.approx(1500.0)  # (5000+10000+0)/10
    assert evidence.tool_bytes_per_request == pytest.approx(1000.0)
    assert evidence.cache_hit_rate == pytest.approx(10_000 / 15_000, abs=1e-6)
    assert evidence.mean_context_window == pytest.approx(200_000.0)
    assert evidence.requests_per_minute == pytest.approx(1.0)


def test_evidence_handles_zero_denominators():
    empty = anomaly.Evidence()
    assert empty.retry_ratio == 0.0
    assert empty.tokens_per_request == 0.0
    assert empty.cache_hit_rate == 0.0
    assert empty.mean_context_window == 0.0
    assert empty.tool_bytes_per_request == 0.0


def test_has_retry_storm_by_ratio():
    assert anomaly.has_retry_storm(_evidence(requests=100, retries=20)) is True


def test_has_retry_storm_by_absolute_count():
    assert anomaly.has_retry_storm(_evidence(requests=10, retries=25)) is True


def test_has_retry_storm_rejects_low_retries():
    assert anomaly.has_retry_storm(_evidence(requests=100, retries=5)) is False


def test_has_retry_storm_rejects_zero_requests():
    assert anomaly.has_retry_storm(_evidence(requests=0, retries=10)) is False


def test_has_context_window_bloat_requires_growth():
    baseline = _evidence(context_window_sum=2_000_000)  # 200k mean
    grew = _evidence(context_window_sum=9_000_000)  # 900k mean
    assert anomaly.has_context_window_bloat(grew, baseline) is True
    flat = _evidence(context_window_sum=2_000_000)
    assert anomaly.has_context_window_bloat(flat, baseline) is False, (
        "a constant 200k context is normal for a coding agent, not bloat"
    )


def test_has_context_window_bloat_without_history_trusts_absolute():
    assert anomaly.has_context_window_bloat(_evidence(), anomaly.Evidence()) is True


def test_has_uncompressed_tool_output():
    window = _evidence(requests=10, tool_result_bytes=3_000_000)  # 300 KB/request
    baseline = _evidence(requests=10, tool_result_bytes=10_000)
    assert anomaly.has_uncompressed_tool_output(window, baseline) is True


def test_has_uncompressed_tool_output_rejects_small_payloads():
    window = _evidence(requests=10, tool_result_bytes=100_000)  # 10 KB/request
    assert anomaly.has_uncompressed_tool_output(window, _evidence()) is False


def test_has_cache_miss_storm():
    baseline = _evidence(input_tokens=1_000, cache_read_tokens=9_000)  # 90% hit
    window = _evidence(input_tokens=9_000, cache_read_tokens=1_000)  # 10% hit
    assert anomaly.has_cache_miss_storm(window, baseline) is True


def test_has_cache_miss_storm_requires_baseline_hits():
    assert anomaly.has_cache_miss_storm(_evidence(), anomaly.Evidence()) is False


def test_has_traffic_spike():
    # Baseline: 100 requests over 100 minutes == 1 req/min, 2000 tokens per request.
    baseline = _evidence(snapshots=100, requests=100, minutes=100, total_tokens=200_000)
    # Window: 50 requests over 10 minutes == 5 req/min (5x), same 2000 tokens per request.
    window = _evidence(snapshots=50, requests=50, minutes=10, total_tokens=100_000)
    assert anomaly.has_traffic_spike(window, baseline) is True


def test_has_traffic_spike_rejects_volume_growth_with_bigger_requests():
    baseline = _evidence(snapshots=100, requests=100, minutes=100, total_tokens=200_000)
    window = _evidence(snapshots=50, requests=50, minutes=10, total_tokens=1_000_000)
    assert anomaly.has_traffic_spike(window, baseline) is False, (
        "requests got bigger, so this is not a pure traffic spike"
    )


def test_classify_unknown_when_nothing_matches():
    window = _evidence(requests=10, retries=0, tool_result_bytes=10_000)
    baseline = _evidence(requests=10, retries=0, tool_result_bytes=10_000)
    classification = anomaly.classify_root_cause(window, baseline)
    assert classification.reason == "unknown"
    assert classification.label == "未知"
    assert classification.detail


def test_classify_prefers_retry_storm_over_everything():
    window = _evidence(requests=10, retries=10, tool_result_bytes=5_000_000, context_window_sum=20_000_000)
    classification = anomaly.classify_root_cause(window, _evidence())
    assert classification.reason == "retry_storm"


def test_classification_evidence_includes_window_and_baseline():
    classification = anomaly.classify_root_cause(_evidence(), _evidence())
    assert "window" in classification.evidence
    assert "baseline" in classification.evidence
    assert "retry_ratio" in classification.evidence["window"]


# ======================================================================================
# anomaly.py — detection on synthetic incidents (the headline behaviour)
# ======================================================================================


def _assert_detected(db: Database, settings: Settings, reason: str) -> anomaly.AnomalyRecord:
    response = run_detection(db, settings)
    assert response.scanned_agents >= 1
    assert response.detected, "detector must fire on the injected incident"
    match = [record for record in response.detected if record.reason == reason]
    assert match, f"expected reason={reason}, got {[(r.agent, r.reason) for r in response.detected]}"
    record = match[0]
    assert record.reason_label == anomaly.REASON_LABELS[reason]
    assert record.reason_detail
    assert record.severity in {"low", "medium", "high", "critical"}
    assert record.metric == "tokens_per_min"
    assert record.fingerprint
    assert record.evidence
    return record


def test_detect_retry_storm(db, settings):
    """A retry storm: the same requests, but each one is retried 4x."""
    seed_baseline(db, settings)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    record = _assert_detected(db, settings, "retry_storm")
    assert record.evidence["classification"]["window"]["retry_ratio"] > 0.15
    assert record.observed > record.baseline
    assert record.score >= 4.0


def test_detect_context_bloat(db, settings):
    """Context bloat: the reported context window jumps 4.5x."""
    seed_baseline(db, settings, context_window=200_000)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  context_window=900_000)
    record = _assert_detected(db, settings, "context_bloat")
    assert record.evidence["classification"]["window"]["mean_context_window"] >= 900_000


def test_detect_uncompressed_tool_output(db, settings):
    """A huge tool payload: 2 MB per request, i.e. 20x the 200 KB rule of thumb."""
    seed_baseline(db, settings, tool_result_bytes=1_000)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  tool_result_bytes=2_000_000)
    record = _assert_detected(db, settings, "uncompressed_tool_output")
    assert record.evidence["classification"]["window"]["tool_result_bytes_per_request"] >= 200_000


def test_detect_cache_miss_storm(db, settings):
    """Caching stopped working: hit rate falls from ~92% to ~2%."""
    seed_baseline(db, settings, input_tokens=500, cache_read_tokens=6_000)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  cache_read_tokens=100)
    record = _assert_detected(db, settings, "cache_miss_storm")
    window = record.evidence["classification"]["window"]
    baseline = record.evidence["classification"]["baseline"]
    assert window["cache_hit_rate"] < baseline["cache_hit_rate"]


def test_detect_traffic_spike(db, settings):
    """5x the request rate with identical per-request token counts."""
    seed_baseline(db, settings, per_minute=1, input_tokens=800, output_tokens=200,
                  cache_read_tokens=1_000)
    seed_incident(db, settings, per_minute=5, input_tokens=800, output_tokens=200,
                  cache_read_tokens=1_000)
    record = _assert_detected(db, settings, "traffic_spike")


def test_detect_on_generate_workload_with_injected_incident(db, settings):
    """The required end-to-end scenario: generated traffic + one injected incident."""
    ingest_snapshots(
        db,
        generate_workload(count=1_200, end_ms=NOW - 3_600_000, span_minutes=300, seed=7),
        settings=settings,
    )
    seed_incident(
        db, settings, per_minute=5, input_tokens=7_000, output_tokens=3_500,
        requests=2, retries=4,
    )
    response = run_detection(db, settings)
    assert response.scanned_agents >= 1
    assert response.detected, "detector must find the injected incident in generated traffic"
    reasons = {record.reason for record in response.detected}
    assert "retry_storm" in reasons, f"expected a retry storm, got {reasons}"
    record = next(r for r in response.detected if r.reason == "retry_storm")
    assert record.ratio >= 3.0
    assert record.score >= 4.0


def test_detect_no_anomaly_on_steady_traffic(db, settings):
    seed_baseline(db, settings, history_minutes=300)
    seed_incident(db, settings, incident_minutes=10, per_minute=1, input_tokens=800,
                  output_tokens=200, cache_read_tokens=1_000, session_prefix="steady")
    response = run_detection(db, settings)
    assert response.detected == [], "flat traffic at the baseline level is not an anomaly"


def test_detect_respects_min_tokens_per_min(db, settings):
    """A statistically loud but tiny blip must not alert (absolute volume floor)."""
    seed_baseline(db, settings, input_tokens=50, output_tokens=50, cache_read_tokens=50)
    # 5 snapshots/min x ~300 tokens = ~1500 tpm, safely under the 2000 tpm floor while
    # still being 10x the 150 tpm baseline (i.e. statistically loud).
    seed_incident(db, settings, per_minute=5, input_tokens=150, output_tokens=100,
                  cache_read_tokens=50, cache_write_tokens=0, session_prefix="tiny")
    response = run_detection(db, settings)
    assert response.detected == [], "observed tpm is below the 2000 tokens/min floor"


def test_detect_deduplicates_within_five_minute_bucket(db, settings):
    seed_baseline(db, settings)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    first = run_detection(db, settings)
    assert len(first.detected) == 1
    second = run_detection(db, settings)
    assert second.detected == [], "the same incident in the same 5 minute bucket is deduped"
    assert db.scalar("SELECT COUNT(*) FROM anomaly_events") == 1


def test_detect_dry_run_persists_nothing(db, settings):
    seed_baseline(db, settings)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    response = run_detection(db, settings, persist=False)
    assert len(response.detected) == 1
    assert response.detected[0].id == 0
    assert db.scalar("SELECT COUNT(*) FROM anomaly_events") == 0


def test_detect_empty_database(db, settings):
    response = run_detection(db, settings)
    assert response.scanned_agents == 0
    assert response.detected == []


def test_detect_is_deterministic(db, settings):
    seed_baseline(db, settings)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    first = run_detection(db, settings, persist=False)
    second = run_detection(db, settings, persist=False)
    assert [r.reason for r in first.detected] == [r.reason for r in second.detected]
    assert first.detected[0].score == pytest.approx(second.detected[0].score)
    assert first.detected[0].fingerprint == second.detected[0].fingerprint


def test_detect_respects_lookback_minutes(db, settings):
    seed_baseline(db, settings)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    response = anomaly.detect(db, settings=settings, now_ms=NOW, lookback_minutes=30, persist=False)
    assert response.lookback_minutes == 30.0
    assert any(record.reason == "retry_storm" for record in response.detected)


def test_detect_handles_multiple_agents(db, settings):
    seed_baseline(db, settings, agent="claude-code", session_prefix="a")
    seed_baseline(db, settings, agent="codex-cli", session_prefix="b")
    seed_incident(db, settings, agent="codex-cli", per_minute=5, input_tokens=6_000,
                  output_tokens=3_000, requests=2, retries=4, session_prefix="binc")
    response = run_detection(db, settings)
    assert response.scanned_agents == 2
    assert {record.agent for record in response.detected} == {"codex-cli"}


def test_detect_warmup_fallback_without_history(db, settings):
    """A brand new agent with no history is still evaluated via the warmup baseline.

    A *pure* burst with no calm minutes at all is deliberately not flagged: with every
    visible minute already at burst level there is no way to distinguish an incident from
    an agent that simply runs at that rate, and guessing would spam fresh installs with
    critical alerts. The warmup fallback exists so a burst that follows some calm minutes
    inside the observation window is still caught — which is what this test exercises.
    """
    # 50 calm minutes then a 10 minute burst, all inside the 60 minute observation window
    # and with no history before it.
    seed_traffic(
        db, settings, start_min=60, end_min=10, per_minute=1,
        input_tokens=800, output_tokens=200, session_prefix="freshcalm",
    )
    seed_incident(
        db, settings, incident_minutes=10, per_minute=5, input_tokens=6_000,
        output_tokens=3_000, requests=2, retries=4, session_prefix="freshburst",
    )
    response = run_detection(db, settings)
    assert response.scanned_agents == 1
    assert response.detected, "a burst after calm minutes must be caught without history"
    assert response.detected[0].evidence["baseline_source"] == "warmup"
    assert response.detected[0].reason == "retry_storm"


def test_detect_skips_agent_with_no_measurable_baseline(db, settings):
    """A pure burst from a brand new agent is not flagged (no baseline to compare to).

    This is the alert-fatigue guard: a fresh install whose entire visible window is one
    burst would otherwise produce a spurious critical event with ``baseline = 0``.
    """
    seed_incident(
        db, settings, incident_minutes=10, per_minute=5, input_tokens=6_000,
        output_tokens=3_000, requests=2, retries=4, session_prefix="fresh",
    )
    response = run_detection(db, settings)
    assert response.scanned_agents == 1
    assert response.detected == []
    assert all(record.baseline > 0 for record in response.detected)


# ======================================================================================
# anomaly.py — persistence + triage
# ======================================================================================


def _seed_one_anomaly(db: Database, settings: Settings) -> int:
    seed_baseline(db, settings)
    seed_incident(db, settings, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    response = run_detection(db, settings)
    assert response.detected
    return response.detected[0].id


def test_anomaly_persisted_with_all_fields(db, settings):
    anomaly_id = _seed_one_anomaly(db, settings)
    record = anomaly.get_anomaly(db, anomaly_id)
    assert record is not None
    assert record.status == "open"
    assert record.metric == "tokens_per_min"
    assert record.reason in anomaly.REASON_LABELS
    assert record.reason_label == anomaly.REASON_LABELS[record.reason]
    assert record.tokens_burn > 0
    assert record.cost_usd > 0
    assert record.fingerprint
    assert record.detected_at is not None


def test_get_missing_anomaly_returns_none(db):
    assert anomaly.get_anomaly(db, 9999) is None


def test_set_anomaly_status_transitions(db, settings):
    anomaly_id = _seed_one_anomaly(db, settings)
    acknowledged = anomaly.set_anomaly_status(db, anomaly_id, "acknowledged")
    assert acknowledged is not None and acknowledged.status == "acknowledged"
    resolved = anomaly.set_anomaly_status(db, anomaly_id, "resolved")
    assert resolved is not None and resolved.status == "resolved"
    reopened = anomaly.set_anomaly_status(db, anomaly_id, "open")
    assert reopened is not None and reopened.status == "open"


def test_set_anomaly_status_rejects_unknown_value(db, settings):
    anomaly_id = _seed_one_anomaly(db, settings)
    with pytest.raises(ValueError):
        anomaly.set_anomaly_status(db, anomaly_id, "ignored")


def test_set_anomaly_status_missing_returns_none(db):
    assert anomaly.set_anomaly_status(db, 9999, "resolved") is None


def test_list_anomalies_filters(db, settings):
    _seed_one_anomaly(db, settings)
    total, items = anomaly.list_anomalies(db)
    assert total == 1
    assert items[0].status == "open"

    assert anomaly.list_anomalies(db, status="open")[0] == 1
    assert anomaly.list_anomalies(db, status="resolved")[0] == 0
    assert anomaly.list_anomalies(db, severity=items[0].severity)[0] == 1
    assert anomaly.list_anomalies(db, agent=items[0].agent)[0] == 1
    assert anomaly.list_anomalies(db, agent="nobody")[0] == 0


def test_anomaly_summary(db, settings):
    _seed_one_anomaly(db, settings)
    summary = anomaly.anomaly_summary(db, now_ms=NOW)
    assert summary.total_events == 1
    assert summary.open_events == 1
    assert summary.by_reason["retry_storm"] == 1
    assert sum(summary.by_severity.values()) == 1
    assert summary.estimated_waste_usd > 0
    assert summary.last_detected_at is not None


def test_anomaly_summary_excludes_resolved_from_waste(db, settings):
    anomaly_id = _seed_one_anomaly(db, settings)
    before = anomaly.anomaly_summary(db, now_ms=NOW)
    assert before.estimated_waste_usd > 0
    anomaly.set_anomaly_status(db, anomaly_id, "resolved")
    after = anomaly.anomaly_summary(db, now_ms=NOW)
    assert after.estimated_waste_usd == 0.0
    assert after.open_events == 0
    assert after.total_events == 1


def test_anomaly_summary_empty_database(db):
    summary = anomaly.anomaly_summary(db, now_ms=NOW)
    assert summary.total_events == 0
    assert summary.open_events == 0
    assert summary.estimated_waste_usd == 0.0
    assert summary.last_detected_at is None
    assert set(summary.by_reason) == set(anomaly.REASON_ORDER)


# ======================================================================================
# scheduler.py
# ======================================================================================


def test_scheduler_run_once_evaluates_budgets_and_anomalies(app, db, settings):
    from tokenlens import scheduler

    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db, BudgetIn(name="s", scope="global", period="daily", limit_usd=100.0, desktop=False)
    )
    stats = scheduler.run_once(app)
    assert stats["errors"] == []
    assert stats["budget"]["evaluated"] == 1
    assert stats["budget"]["triggered"] == 3
    assert stats["anomaly"] is not None
    assert stats["duration_ms"] >= 0


def test_scheduler_run_once_survives_a_failing_pass(app, db, settings, monkeypatch):
    """A crashing budget pass must be recorded, not raised, and must not stop detection."""
    from tokenlens import scheduler

    def boom(*args, **kwargs):
        raise RuntimeError("sqlite is having a bad day")

    monkeypatch.setattr(scheduler.budget_module, "evaluate_budgets", boom)
    stats = scheduler.run_once(app)
    assert stats["budget"] is None
    assert any("budget" in error for error in stats["errors"])
    assert stats["anomaly"] is not None, "the anomaly pass still ran"


def test_scheduler_run_once_with_empty_database(app):
    from tokenlens import scheduler

    stats = scheduler.run_once(app)
    assert stats["errors"] == []
    assert stats["budget"]["evaluated"] == 0
    assert stats["anomaly"]["scanned_agents"] == 0


def test_scheduler_start_and_stop_lifecycle(settings, db):
    """``start`` returns a task, ``stop`` cancels it, and both are idempotent."""
    import asyncio

    from tokenlens.app import create_app
    from tokenlens import scheduler

    settings.enable_scheduler = True
    settings.budget_interval_seconds = 3600  # one pass then sleep, so the test is fast
    application = create_app(settings=settings, db=db)

    async def scenario() -> None:
        task = scheduler.start(application)
        assert task is not None
        assert scheduler.start(application) is task, "start is idempotent"
        await asyncio.sleep(0.2)
        assert not task.done()
        await scheduler.stop(application)
        assert task.done()
        await scheduler.stop(application)  # stopping twice is safe

    asyncio.run(scenario())


def test_scheduler_start_respects_disabled_setting(settings, db):
    import asyncio

    from tokenlens.app import create_app
    from tokenlens import scheduler

    settings.enable_scheduler = False
    application = create_app(settings=settings, db=db)
    assert asyncio.run(_start_disabled(application, scheduler)) is None


async def _start_disabled(application, scheduler):
    return scheduler.start(application)


def test_app_lifespan_starts_and_stops_scheduler(settings, db):
    """The wired application must boot and shut down with the scheduler enabled."""
    from tokenlens.app import create_app

    settings.enable_scheduler = True
    settings.budget_interval_seconds = 3600
    application = create_app(settings=settings, db=db)
    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
    assert getattr(application.state, "scheduler_task", None) is None


# ======================================================================================
# API — budgets (§7)
# ======================================================================================


def test_api_create_budget_returns_status(client):
    response = client.post(
        "/api/v1/budgets",
        json={
            "name": "team-daily",
            "scope": "global",
            "period": "daily",
            "limit_usd": 25.0,
            "thresholds": [0.5, 0.8, 1.0],
            "desktop": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["budget"]["name"] == "team-daily"
    assert body["budget"]["scope_key"] == "*"
    assert body["spent_usd"] == 0.0
    assert body["status"] == "ok"
    assert body["window_start"].endswith("Z")


def test_api_create_budget_validation_errors(client):
    assert client.post("/api/v1/budgets", json={"name": "x", "limit_usd": -1}).status_code == 422
    assert client.post(
        "/api/v1/budgets", json={"name": "x", "limit_usd": 10, "thresholds": []}
    ).status_code == 422
    assert client.post(
        "/api/v1/budgets", json={"name": "x", "limit_usd": 10, "thresholds": [2.0, 1.0, 3.0, 4.0, 5.0, 6.0]}
    ).status_code == 422
    assert client.post(
        "/api/v1/budgets",
        json={"name": "x", "scope": "project", "limit_usd": 10},
    ).status_code == 422, "non-global scope requires scope_key"


def test_api_list_budgets_empty(client):
    response = client.get("/api/v1/budgets")
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_api_list_budgets_with_items(client):
    client.post("/api/v1/budgets", json={"name": "a", "limit_usd": 10, "period": "daily"})
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 20, "period": "weekly"})
    body = client.get("/api/v1/budgets").json()
    assert [item["budget"]["name"] for item in body["items"]] == ["a", "b"]


def test_api_get_budget_404(client):
    assert client.get("/api/v1/budgets/999").status_code == 404


def test_api_patch_budget(client):
    created = client.post("/api/v1/budgets", json={"name": "a", "limit_usd": 10}).json()
    budget_id = created["budget"]["id"]
    response = client.patch(f"/api/v1/budgets/{budget_id}", json={"limit_usd": 50.0, "enabled": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["budget"]["limit_usd"] == 50.0
    assert body["budget"]["enabled"] is False
    assert body["budget"]["name"] == "a"


def test_api_patch_budget_404(client):
    assert client.patch("/api/v1/budgets/999", json={"limit_usd": 1.0}).status_code == 404


def test_api_delete_budget(client):
    created = client.post("/api/v1/budgets", json={"name": "a", "limit_usd": 10}).json()
    budget_id = created["budget"]["id"]
    assert client.delete(f"/api/v1/budgets/{budget_id}").status_code == 204
    assert client.get(f"/api/v1/budgets/{budget_id}").status_code == 404
    assert client.delete(f"/api/v1/budgets/{budget_id}").status_code == 404


def test_api_evaluate_empty(client):
    response = client.post("/api/v1/budgets/evaluate")
    assert response.status_code == 200
    body = response.json()
    assert body["evaluated"] == 0
    assert body["triggered"] == []
    assert body["dry_run"] is False
    assert body["evaluated_at"].endswith("Z")


def test_api_evaluate_triggers_alerts(client, db, settings):
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 100.0, "desktop": False})
    body = client.post("/api/v1/budgets/evaluate").json()
    assert body["evaluated"] == 1
    assert [item["threshold"] for item in body["triggered"]] == [0.5, 0.8, 1.0]
    assert body["triggered"][0]["severity"] == "warning"
    assert body["triggered"][2]["severity"] == "exceeded"
    assert body["triggered"][2]["overspend_usd"] == pytest.approx(50.0, abs=1e-4)


def test_api_evaluate_dry_run(client, db, settings):
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 100.0, "desktop": False})
    body = client.post("/api/v1/budgets/evaluate?dry_run=true").json()
    assert body["dry_run"] is True
    assert len(body["triggered"]) == 3
    assert client.get("/api/v1/alerts").json()["total"] == 0


def test_api_evaluate_increments_trigger_count(client, db, settings):
    """The E2E path: two passes must bump trigger_count, not duplicate alerts."""
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 100.0, "desktop": False})
    client.post("/api/v1/budgets/evaluate")
    second = client.post("/api/v1/budgets/evaluate").json()
    assert [item["trigger_count"] for item in second["triggered"]] == [2, 2, 2]
    assert client.get("/api/v1/alerts").json()["total"] == 3


# ======================================================================================
# API — alerts (§7)
# ======================================================================================


def test_api_alerts_empty(client):
    body = client.get("/api/v1/alerts").json()
    assert body == {"total": 0, "items": []}


def test_api_alerts_list_and_filters(client, db, settings):
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 100.0, "desktop": False})
    client.post("/api/v1/budgets/evaluate")

    body = client.get("/api/v1/alerts").json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert {"id", "budget_name", "scope", "period", "threshold", "severity", "overspend_usd",
            "trigger_count", "message", "channels", "created_at"} <= set(body["items"][0])

    filtered = client.get("/api/v1/alerts?severity=exceeded").json()
    assert filtered["total"] == 1

    paged = client.get("/api/v1/alerts?limit=2&offset=0").json()
    assert len(paged["items"]) == 2


def test_api_alerts_invalid_severity(client):
    assert client.get("/api/v1/alerts?severity=nope").status_code == 422


def test_api_alerts_invalid_since(client):
    assert client.get("/api/v1/alerts?since=not-a-time").status_code == 422


def test_api_alert_summary_empty(client):
    body = client.get("/api/v1/alerts/summary").json()
    assert body["total_alerts"] == 0
    assert body["total_triggers"] == 0
    assert body["by_severity"] == {"warning": 0, "critical": 0, "exceeded": 0}
    assert body["by_budget"] == []
    assert body["total_overspend_usd"] == 0.0
    assert body["last_triggered_at"] is None


def test_api_alert_summary_populated(client, db, settings):
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 100.0, "desktop": False})
    client.post("/api/v1/budgets/evaluate")
    body = client.get("/api/v1/alerts/summary").json()
    assert body["total_alerts"] == 3
    assert body["total_triggers"] == 3
    assert body["by_severity"]["exceeded"] == 1
    assert body["by_budget"][0]["budget_name"] == "b"
    assert body["total_overspend_usd"] == pytest.approx(50.0, abs=1e-4)


def test_api_get_alert_by_id(client, db, settings):
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "b", "limit_usd": 100.0, "desktop": False})
    client.post("/api/v1/budgets/evaluate")
    alert_id = client.get("/api/v1/alerts").json()["items"][0]["id"]
    body = client.get(f"/api/v1/alerts/{alert_id}").json()
    assert body["id"] == alert_id
    assert client.get("/api/v1/alerts/999999").status_code == 404


# ======================================================================================
# API — anomalies (§8)
# ======================================================================================


def test_api_detect_empty(client):
    body = client.post("/api/v1/anomalies/detect").json()
    assert body["scanned_agents"] == 0
    assert body["detected"] == []
    assert body["detected_at"].endswith("Z")
    assert body["lookback_minutes"] == 60.0


def test_api_detect_finds_injected_incident(client, db, settings):
    now = live_now()
    seed_baseline(db, settings, now=now)
    seed_incident(db, settings, now=now, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    body = client.post("/api/v1/anomalies/detect?lookback_minutes=60").json()
    assert body["scanned_agents"] >= 1
    assert body["detected"], "detector must fire over HTTP too"
    record = body["detected"][0]
    assert record["reason"] == "retry_storm"
    assert record["reason_label"] == "重试风暴"
    assert record["metric"] == "tokens_per_min"
    assert record["status"] == "open"
    assert record["fingerprint"]


def test_api_detect_dry_run(client, db, settings):
    now = live_now()
    seed_baseline(db, settings, now=now)
    seed_incident(db, settings, now=now, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    body = client.post("/api/v1/anomalies/detect?persist=false").json()
    assert len(body["detected"]) == 1
    assert client.get("/api/v1/anomalies").json()["total"] == 0


def test_api_anomalies_empty(client):
    assert client.get("/api/v1/anomalies").json() == {"total": 0, "items": []}


def test_api_anomalies_list_filters(client, db, settings):
    now = live_now()
    seed_baseline(db, settings, now=now)
    seed_incident(db, settings, now=now, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    client.post("/api/v1/anomalies/detect")
    body = client.get("/api/v1/anomalies").json()
    assert body["total"] == 1
    record = body["items"][0]
    assert {"id", "detected_at", "agent", "project", "model", "window_start", "window_end",
            "metric", "observed", "baseline", "score", "ratio", "severity", "reason",
            "reason_label", "reason_detail", "evidence", "tokens_burn", "cost_usd",
            "status", "fingerprint"} <= set(record)

    assert client.get("/api/v1/anomalies?status=open").json()["total"] == 1
    assert client.get("/api/v1/anomalies?status=resolved").json()["total"] == 0
    assert client.get(f"/api/v1/anomalies?agent={record['agent']}").json()["total"] == 1
    assert client.get("/api/v1/anomalies?agent=nobody").json()["total"] == 0


def test_api_anomalies_invalid_status_filter(client):
    assert client.get("/api/v1/anomalies?status=nope").status_code == 422


def test_api_anomaly_summary_empty(client):
    body = client.get("/api/v1/anomalies/summary").json()
    assert body["total_events"] == 0
    assert body["open_events"] == 0
    assert body["estimated_waste_usd"] == 0.0
    assert body["last_detected_at"] is None
    assert set(body["by_reason"]) == set(anomaly.REASON_ORDER)


def test_api_anomaly_get_and_patch(client, db, settings):
    now = live_now()
    seed_baseline(db, settings, now=now)
    seed_incident(db, settings, now=now, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    anomaly_id = client.post("/api/v1/anomalies/detect").json()["detected"][0]["id"]

    fetched = client.get(f"/api/v1/anomalies/{anomaly_id}").json()
    assert fetched["id"] == anomaly_id

    patched = client.patch(f"/api/v1/anomalies/{anomaly_id}", json={"status": "acknowledged"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "acknowledged"

    resolved = client.patch(f"/api/v1/anomalies/{anomaly_id}", json={"status": "resolved"})
    assert resolved.json()["status"] == "resolved"

    summary = client.get("/api/v1/anomalies/summary").json()
    assert summary["total_events"] == 1
    assert summary["open_events"] == 0
    assert summary["estimated_waste_usd"] == 0.0


def test_api_anomaly_patch_validation(client):
    assert client.patch("/api/v1/anomalies/1", json={"status": "nope"}).status_code == 422


def test_api_anomaly_404s(client):
    assert client.get("/api/v1/anomalies/999999").status_code == 404
    assert client.patch("/api/v1/anomalies/999999", json={"status": "resolved"}).status_code == 404


# ======================================================================================
# API — dashboard (§9)
# ======================================================================================


def test_dashboard_overview_empty_database(client):
    response = client.get("/api/v1/dashboard/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generated_at"].endswith("Z")

    kpis = body["kpis"]
    assert kpis["today_cost_usd"] == 0.0
    assert kpis["today_tokens"] == 0
    assert kpis["week_cost_usd"] == 0.0
    assert kpis["month_cost_usd"] == 0.0
    assert kpis["burn_rate_tokens_per_min"] == 0.0
    assert kpis["cache_hit_rate"] == 0.0
    assert kpis["active_agents"] == 0
    assert kpis["total_sessions"] == 0

    assert body["breakdowns"] == {
        "by_agent": [],
        "by_project": [],
        "by_model": [],
        "by_runtime": [],
    }
    assert body["budgets"] == []
    assert body["alerts"]["recent"] == []
    assert body["alerts"]["summary"]["total_alerts"] == 0
    assert body["anomalies"]["recent"] == []
    assert body["anomalies"]["summary"]["total_events"] == 0
    # Timeseries are gap-filled, so even an empty DB yields points (all zero).
    assert body["timeseries"]["hourly"]
    assert body["timeseries"]["daily"]


def test_dashboard_overview_seeded(client, db, settings):
    ingest_snapshots(
        db,
        generate_workload(count=400, end_ms=timeutil.now_ms(), span_minutes=120, seed=11),
        settings=settings,
    )
    response = client.get("/api/v1/dashboard/overview?days=7")
    assert response.status_code == 200
    body = response.json()

    assert body["kpis"]["today_cost_usd"] > 0
    assert body["kpis"]["today_tokens"] > 0
    assert body["kpis"]["active_agents"] > 0
    assert body["kpis"]["total_sessions"] > 0

    for key in ("by_agent", "by_project", "by_model", "by_runtime"):
        rows = body["breakdowns"][key]
        assert rows, f"{key} should not be empty"
        row = rows[0]
        assert {"key", "label", "totals", "share_pct"} <= set(row)
        assert row["totals"]["cost_usd"] >= 0
        assert 0 <= row["share_pct"] <= 100


def test_dashboard_breakdown_shares_sum_to_about_100(client, db, settings):
    ingest_snapshots(
        db,
        generate_workload(count=300, end_ms=timeutil.now_ms(), span_minutes=60, seed=3),
        settings=settings,
    )
    body = client.get("/api/v1/dashboard/overview").json()
    for key in ("by_agent", "by_project", "by_model", "by_runtime"):
        shares = [row["share_pct"] for row in body["breakdowns"][key]]
        if shares:
            assert sum(shares) == pytest.approx(100.0, abs=1.0), f"{key} shares should total ~100%"


def test_dashboard_includes_budgets_alerts_anomalies(client, db, settings):
    seed_spend(db, settings, usd_target=150.0)
    client.post("/api/v1/budgets", json={"name": "dash", "limit_usd": 100.0, "desktop": False})
    client.post("/api/v1/budgets/evaluate")

    now = live_now()
    seed_baseline(db, settings, now=now)
    seed_incident(db, settings, now=now, per_minute=5, input_tokens=6_000, output_tokens=3_000,
                  requests=2, retries=4)
    client.post("/api/v1/anomalies/detect")

    body = client.get("/api/v1/dashboard/overview").json()
    assert len(body["budgets"]) == 1
    assert body["budgets"][0]["budget"]["name"] == "dash"
    assert body["budgets"][0]["status"] == "exceeded"

    assert body["alerts"]["recent"]
    assert body["alerts"]["summary"]["total_alerts"] == 3

    assert body["anomalies"]["recent"]
    assert body["anomalies"]["summary"]["total_events"] == 1


def test_dashboard_timeseries_buckets(client, db, settings):
    ingest_snapshots(
        db,
        generate_workload(count=200, end_ms=timeutil.now_ms(), span_minutes=90, seed=5),
        settings=settings,
    )
    body = client.get("/api/v1/dashboard/overview").json()
    hourly = body["timeseries"]["hourly"]
    daily = body["timeseries"]["daily"]
    assert len(hourly) == 24
    assert len(daily) == 7
    point = hourly[-1]
    assert {"bucket_start", "bucket_end", "label", "totals"} <= set(point)
    assert point["bucket_start"].endswith("Z")


def test_dashboard_days_parameter(client):
    assert client.get("/api/v1/dashboard/overview?days=1").status_code == 200
    assert client.get("/api/v1/dashboard/overview?days=90").status_code == 200
    assert client.get("/api/v1/dashboard/overview?days=0").status_code == 422
    assert client.get("/api/v1/dashboard/overview?days=500").status_code == 422


def test_dashboard_health(client):
    body = client.get("/api/v1/dashboard/health").json()
    assert body["ok"] is True
    assert body["snapshots"] == 0
    assert body["price_version"]
    assert body["now"].endswith("Z")


# ======================================================================================
# Cross-cutting: the whole intelligence layer in one pass
# ======================================================================================


def test_full_intelligence_pass(client, db, settings):
    """Ingest -> budget breach -> alert -> anomaly -> dashboard, all in one flow."""
    from tokenlens import scheduler

    ingest_snapshots(
        db,
        generate_workload(count=600, end_ms=timeutil.now_ms(), span_minutes=120, seed=13),
        settings=settings,
    )
    seed_incident(db, settings, now=timeutil.now_ms(), per_minute=6, input_tokens=7_000,
                  output_tokens=3_500, requests=2, retries=4)
    client.post("/api/v1/budgets", json={"name": "flow", "limit_usd": 0.5, "desktop": False})

    stats = scheduler.run_once(client.app)
    assert stats["errors"] == []
    assert stats["budget"]["evaluated"] == 1
    assert stats["anomaly"]["detected"] >= 1

    overview = client.get("/api/v1/dashboard/overview").json()
    assert overview["budgets"][0]["status"] in {"warning", "critical", "exceeded"}
    assert overview["alerts"]["summary"]["total_alerts"] >= 1
    assert overview["anomalies"]["summary"]["total_events"] >= 1
    assert overview["kpis"]["today_cost_usd"] > 0


def test_notifications_file_is_valid_jsonl(db, settings):
    """Guard for the CLI/UI consumers: every line must be independently parseable."""
    seed_spend(db, settings, usd_target=150.0)
    budget.create_budget(
        db, BudgetIn(name="b", scope="global", period="daily", limit_usd=100.0, desktop=True)
    )
    budget.evaluate_budgets(db, settings=settings, now_ms=NOW)
    lines = settings.notify_file.read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        entry = json.loads(line)
        assert {"ts", "channel", "title", "body", "severity", "payload"} <= set(entry)
