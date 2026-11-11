"""Web dashboard tests: static serving, tab coverage, and the contract-drift guard.

The dashboard is deliberately dependency free (no build step, no CDN), which makes it easy
to serve but easy to *drift*: a renamed endpoint silently breaks the UI. The guard test at
the bottom of this file extracts every ``/api/v1/...`` literal the JavaScript talks to and
asserts it exists in the live OpenAPI schema — a rename on the backend now fails the suite
instead of producing an empty panel at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB_DIR = Path(__file__).resolve().parent.parent / "tokenlens" / "web"

#: The six tabs required by docs/API_CONTRACT.md §11, in display order.
TAB_LABELS = ["总览", "维度", "趋势", "预算与告警", "异常检测", "模拟器"]

TAB_KEYS = ["overview", "dimensions", "trends", "budgets", "anomalies", "simulator"]

#: ``"/api/v1/usage/breakdown"`` style literals (single or double quoted).
API_LITERAL_RE = re.compile(r"""["'](/api/v1/[A-Za-z0-9_\-/{}]+)["']""")

#: A template literal containing an API path would hide from the guard above.
API_TEMPLATE_RE = re.compile(r"`[^`]*?/api/v1/[^`]*?`")


def _web_text(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _normalise(path: str) -> str:
    """Collapse path parameters so ``{id}`` and ``{budget_id}`` compare equal."""
    segments = []
    for segment in path.strip().strip("/").split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            segments.append("{}")
        else:
            segments.append(segment)
    return "/" + "/".join(segments)


def _referenced_api_paths() -> list[str]:
    """Every ``/api/v1`` literal referenced by the dashboard JavaScript."""
    found: list[str] = []
    for name in ("app.js", "charts.js"):
        text = _web_text(name)
        for match in API_LITERAL_RE.finditer(text):
            path = match.group(1)
            # Strip a query string if somebody inlines one.
            path = path.split("?", 1)[0]
            if path not in found:
                found.append(path)
    return found


# --------------------------------------------------------------------------------------
# Files & static serving
# --------------------------------------------------------------------------------------


def test_web_directory_contains_the_static_assets():
    assert WEB_DIR.is_dir(), "tokenlens/web must exist so app.py can mount it"
    for name in ("index.html", "styles.css", "app.js", "charts.js", "favicon.svg"):
        assert (WEB_DIR / name).is_file(), f"missing dashboard asset: {name}"


def test_index_is_served_at_root(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "<!DOCTYPE html>" in body
    assert "TokenLens" in body
    # The page must load its own assets through the mounted /static path.
    assert "/static/styles.css" in body
    assert "/static/app.js" in body
    assert "/static/charts.js" in body


def test_index_contains_all_six_tab_labels(client: TestClient):
    body = client.get("/").text
    for label in TAB_LABELS:
        assert label in body, f"tab label missing from index.html: {label}"


def test_index_declares_one_panel_per_tab(client: TestClient):
    body = client.get("/").text
    for key in TAB_KEYS:
        assert f'id="panel-{key}"' in body, f"missing panel container for tab: {key}"
        assert f'data-tab="{key}"' in body, f"missing tab button for: {key}"


def test_static_app_js_is_served_with_javascript_content_type(client: TestClient):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "TokenLensApp" in response.text


def test_static_charts_js_is_served_with_javascript_content_type(client: TestClient):
    response = client.get("/static/charts.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "TLCharts" in response.text


def test_static_styles_css_is_served_with_css_content_type(client: TestClient):
    response = client.get("/static/styles.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert response.text.strip(), "stylesheet must not be empty"


def test_favicon_is_served_as_svg(client: TestClient):
    response = client.get("/static/favicon.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]
    assert response.text.lstrip().startswith("<svg")


def test_static_mount_does_not_leak_the_repository(client: TestClient):
    """A path traversal attempt must not expose files outside ``tokenlens/web``."""
    response = client.get("/static/../app.py")
    assert response.status_code in (404, 400)


# --------------------------------------------------------------------------------------
# Offline safety & UX requirements
# --------------------------------------------------------------------------------------


def test_dashboard_is_offline_safe():
    """No CDN, no web fonts, no remote assets — the page must work on an air-gapped box.

    The rule is about *resource loads*, not about text: loopback URLs appear in the
    simulator panel (``--server http://127.0.0.1:8787``) and ``hooks.example.com`` is a
    form placeholder — a human types those, the browser never fetches them.
    """
    html = _web_text("index.html")
    for attribute, value in re.findall(r"""\b(src|href)\s*=\s*["']([^"']+)["']""", html):
        # Same-origin absolute paths (/static/..., /docs) are fine; anything with a
        # scheme or protocol-relative prefix would be a remote dependency.
        assert value.startswith(("/", "#", "data:")), (
            f"index.html loads a non-local resource via {attribute}={value!r}"
        )
        assert not value.startswith("//"), f"protocol-relative URL is remote: {value!r}"

    for name in ("app.js", "charts.js", "styles.css"):
        text = _web_text(name)
        # A script must never fetch()/import a cross-origin URL.
        for pattern in (r"""fetch\(\s*["']https?://""", r"""import\s*\(?\s*["']https?://""", r"@import\s+url"):
            assert not re.search(pattern, text), f"{name} loads a remote resource: {pattern}"
        for needle in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg", "jsdelivr", "//cdnjs"):
            assert needle not in text, f"{name} must not reference {needle!r}"

    assert not re.search(r"@import\s+(?:url\()?['\"]?https?:", _web_text("styles.css")), (
        "styles.css must not import a remote stylesheet"
    )


def test_dashboard_assets_are_all_same_origin():
    """Every ``<script>``/``<link>`` must resolve under /static (served by FastAPI)."""
    html = _web_text("index.html")
    sources = re.findall(r"""<(?:script|link)[^>]+(?:src|href)="([^"]+)\"""", html)
    assert sources, "index.html should load local assets"
    for source in sources:
        assert source.startswith("/static/"), f"asset must be served locally: {source}"


def test_app_js_polls_the_overview_every_three_seconds():
    app_js = _web_text("app.js")
    assert "/api/v1/dashboard/overview" in app_js
    assert "POLL_MS = 3000" in app_js
    assert "setInterval" in app_js


def test_app_js_formats_numbers_with_intl():
    app_js = _web_text("app.js")
    assert "Intl.NumberFormat" in app_js
    # Money/token helpers must exist so every panel formats consistently.
    assert "function money(" in app_js
    assert "function intText(" in app_js


def test_app_js_renders_placeholders_for_missing_fields():
    """Defensive rendering: no bare ``.field`` chains on possibly-undefined payloads."""
    app_js = _web_text("app.js")
    assert "function obj(" in app_js and "function arr(" in app_js
    # A thrown exception in a poll tick would freeze the whole console.
    assert "catch" in app_js


def test_charts_js_implements_sparkline_and_line_area():
    charts = _web_text("charts.js")
    assert "function sparkline(" in charts
    assert "function lineArea(" in charts
    assert "createElementNS" in charts, "charts must be hand-written SVG, not canvas"
    assert "no chart library" in charts.lower() or "no CDN" in charts or "No chart library" in charts


def test_anomaly_reason_labels_are_localised():
    """§8 reason codes must render as the documented Chinese labels."""
    app_js = _web_text("app.js")
    for reason, label in (
        ("retry_storm", "重试风暴"),
        ("context_bloat", "上下文膨胀"),
        ("uncompressed_tool_output", "工具返回数据未压缩"),
        ("cache_miss_storm", "缓存命中崩塌"),
        ("traffic_spike", "流量尖峰"),
        ("unknown", "未知"),
    ):
        assert reason in app_js
        assert label in app_js


def test_simulator_tab_documents_the_load_generator():
    body = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "tools/simulate.py" in body
    assert "--mode incident" in body
    assert "api/v1/anomalies/detect" in body


# --------------------------------------------------------------------------------------
# Contract drift guard
# --------------------------------------------------------------------------------------


def test_js_does_not_hide_api_paths_in_template_literals():
    for name in ("app.js", "charts.js"):
        hidden = API_TEMPLATE_RE.findall(_web_text(name))
        assert not hidden, f"{name} builds API URLs inside template literals: {hidden}"


def test_every_api_path_used_by_the_dashboard_exists_in_openapi(app):
    """The drift guard: dashboard literals must be real, documented routes."""
    documented = {_normalise(path) for path in app.openapi()["paths"]}
    referenced = _referenced_api_paths()

    assert referenced, "the dashboard should reference the API at all"
    missing = sorted(
        path for path in referenced if _normalise(path) not in documented
    )
    assert not missing, (
        "dashboard references endpoints that do not exist in the OpenAPI schema "
        f"(contract drift): {missing}"
    )


def test_dashboard_covers_every_contract_surface(app):
    """Sanity check that the UI actually wires up all six feature areas."""
    referenced = {_normalise(path) for path in _referenced_api_paths()}
    required = {
        "/api/v1/dashboard/overview",
        "/api/v1/usage/breakdown",
        "/api/v1/trends",
        "/api/v1/export/csv",
        "/api/v1/budgets",
        "/api/v1/budgets/evaluate",
        "/api/v1/alerts",
        "/api/v1/alerts/summary",
        "/api/v1/anomalies",
        "/api/v1/anomalies/summary",
        "/api/v1/anomalies/detect",
    }
    assert required <= referenced, f"dashboard does not use: {sorted(required - referenced)}"


# --------------------------------------------------------------------------------------
# Empty database behaviour
# --------------------------------------------------------------------------------------


def test_empty_database_still_serves_the_dashboard(client: TestClient):
    """A brand new install must render (static assets + a zeroed overview)."""
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200

    response = client.get("/api/v1/dashboard/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    for key in ("kpis", "breakdowns", "timeseries", "budgets", "alerts", "anomalies"):
        assert key in body, f"overview is missing the {key!r} section"
    assert body["kpis"]["today_cost_usd"] == 0
    assert body["kpis"]["week_cost_usd"] == 0
    assert body["kpis"]["month_cost_usd"] == 0
    assert body["budgets"] == []
    assert body["breakdowns"]["by_agent"] == []


def test_empty_overview_has_zeroed_kpi_shape(client: TestClient):
    kpis = client.get("/api/v1/dashboard/overview").json()["kpis"]
    for field in (
        "today_cost_usd",
        "today_tokens",
        "week_cost_usd",
        "month_cost_usd",
        "burn_rate_tokens_per_min",
        "burn_rate_usd_per_hour",
        "cache_hit_rate",
        "active_agents",
        "active_projects",
        "total_sessions",
    ):
        assert field in kpis, f"KPI card field missing from the contract: {field}"
        assert kpis[field] == 0


@pytest.mark.parametrize("asset", ["app.js", "charts.js", "styles.css"])
def test_static_assets_are_utf8_and_non_trivial(asset: str):
    raw = (WEB_DIR / asset).read_bytes()
    assert len(raw) > 1500, f"{asset} looks like a stub"
    raw.decode("utf-8")  # must be valid UTF-8 (Chinese labels live here)
