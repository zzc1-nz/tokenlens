"""Thin HTTP client for the TokenLens query API.

Design notes
------------
The HUD is a *monitoring* tool, so it must be extremely clear about why it cannot show
data. Instead of letting ``httpx`` exceptions leak into the render loop, every call
raises :class:`ApiError` carrying a machine readable ``kind``:

``offline``
    The server is unreachable (connection refused, DNS failure, timeout). The HUD turns
    this into the ``OFFLINE`` banner and retries on the next tick.
``http``
    The server answered with a 4xx/5xx. ``status_code`` is preserved so callers can treat
    ``404``/``501`` ("endpoint not implemented yet" while the platform is being built)
    as "no data" rather than a hard failure.
``invalid``
    The server answered 200 with a body that is not the JSON object we expected. This is
    a contract violation and is surfaced loudly.

The client is synchronous on purpose: the HUD loop is a single-threaded
refresh/render/read-key cycle and a background thread would only add locking.
:class:`TokenLensClient` accepts an ``httpx`` transport so tests can drive it through
``httpx.MockTransport`` without opening a socket.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import httpx

DEFAULT_SERVER = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 5.0

#: Paths the CLI uses. Centralised so the test suite can assert the exact surface.
PATH_HEALTH = "/health"
PATH_META = "/api/v1/meta"
PATH_SUMMARY = "/api/v1/usage/summary"
PATH_BREAKDOWN = "/api/v1/usage/breakdown"
PATH_TIMESERIES = "/api/v1/usage/timeseries"
PATH_LIVE = "/api/v1/usage/live"
PATH_TRENDS = "/api/v1/trends"
PATH_CATALOG_MODELS = "/api/v1/catalog/models"
PATH_CATALOG_AGENTS = "/api/v1/catalog/agents"
PATH_CATALOG_PROJECTS = "/api/v1/catalog/projects"
PATH_CATALOG_SESSIONS = "/api/v1/catalog/sessions"
PATH_EXPORT_CSV = "/api/v1/export/csv"
PATH_BUDGETS = "/api/v1/budgets"
PATH_BUDGET_EVALUATE = "/api/v1/budgets/evaluate"
PATH_ALERTS = "/api/v1/alerts"
PATH_ALERTS_SUMMARY = "/api/v1/alerts/summary"
PATH_ANOMALIES = "/api/v1/anomalies"
PATH_ANOMALIES_SUMMARY = "/api/v1/anomalies/summary"
PATH_ANOMALIES_DETECT = "/api/v1/anomalies/detect"
PATH_DASHBOARD = "/api/v1/dashboard/overview"


class ApiError(RuntimeError):
    """A failed API call, classified so the UI can react appropriately."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "http",
        status_code: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.url = url

    @property
    def offline(self) -> bool:
        """True when the failure means "cannot reach the server at all"."""
        return self.kind == "offline"

    @property
    def missing(self) -> bool:
        """True when the endpoint exists in the contract but not in this build."""
        return self.status_code in (404, 405, 501)

    def friendly(self, server: str | None = None) -> str:
        """One-line explanation suitable for a CLI error message."""
        target = server or self.url or "the server"
        if self.kind == "offline":
            return f"cannot reach TokenLens at {target} — is the server running? ({self})"
        if self.missing:
            return f"{self.url or 'endpoint'} is not implemented on this server (HTTP {self.status_code})"
        if self.kind == "invalid":
            return f"unexpected response from {self.url or target}: {self}"
        return f"HTTP {self.status_code} from {self.url or target}: {self}"


def normalize_server(server: str | None) -> str:
    """Normalise a ``--server`` value into a base URL without a trailing slash."""
    raw = (server or DEFAULT_SERVER).strip() or DEFAULT_SERVER
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


def _detail_from_body(body: bytes) -> str:
    """Extract FastAPI's ``{"detail": "..."}`` (or raw text) from an error body."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return "no response body"
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:300]
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)[:300]
    return text[:300]


class TokenLensClient:
    """Minimal synchronous client for the read side of the TokenLens API.

    Parameters
    ----------
    server:
        Base URL, e.g. ``http://127.0.0.1:8787``. A bare ``host:port`` is accepted and
        gets an ``http://`` prefix.
    timeout:
        Per-request timeout in seconds. The HUD uses a short one so a hung server does
        not freeze the refresh loop.
    api_key:
        Sent as ``X-API-Key`` when the deployment enabled auth.
    transport:
        Optional ``httpx`` transport, injected by tests (``httpx.MockTransport``).
    """

    def __init__(
        self,
        server: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.server = normalize_server(server)
        self.timeout = float(timeout)
        headers = {"Accept": "application/json", "User-Agent": "tokenlens-cli/0.1"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.server,
            timeout=httpx.Timeout(self.timeout),
            headers=headers,
            transport=transport,
            follow_redirects=True,
        )

    # -- lifecycle ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying connection pool (no-op for an injected client)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TokenLensClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- transport ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Issue one request, converting every failure mode into :class:`ApiError`."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.server}{path}"
        try:
            response = self._client.request(method, path, params=clean, json=json_body)
        except httpx.TimeoutException as exc:
            raise ApiError(f"timed out after {self.timeout:g}s", kind="offline", url=url) from exc
        except httpx.HTTPError as exc:
            raise ApiError(str(exc) or exc.__class__.__name__, kind="offline", url=url) from exc
        if response.status_code >= 400:
            raise ApiError(
                _detail_from_body(response.content),
                kind="http",
                status_code=response.status_code,
                url=url,
            )
        return response

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        expect: type = dict,
    ) -> Any:
        response = self._request("GET", path, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError("response was not valid JSON", kind="invalid", url=path) from exc
        if not isinstance(payload, expect):
            raise ApiError(
                f"expected a JSON {expect.__name__}, got {type(payload).__name__}",
                kind="invalid",
                url=path,
            )
        return payload

    # -- health / meta --------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """``GET /health`` — cheap liveness probe (unauthenticated)."""
        return self._get_json(PATH_HEALTH)

    def meta(self) -> dict[str, Any]:
        """``GET /api/v1/meta`` — version, price catalog size, endpoint list."""
        return self._get_json(PATH_META)

    def is_available(self) -> bool:
        """Probe the server without raising; used by ``--dry-run`` style checks."""
        try:
            self.health()
        except ApiError:
            return False
        return True

    # -- usage ----------------------------------------------------------------------
    def summary(
        self,
        *,
        agent: str | None = None,
        project: str | None = None,
        model: str | None = None,
        runtime: str | None = None,
        session_id: str | None = None,
        from_value: str | int | None = None,
        to_value: str | int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/v1/usage/summary`` — one ``Totals`` for a filtered window."""
        return self._get_json(
            PATH_SUMMARY,
            params={
                "agent": agent,
                "project": project,
                "model": model,
                "runtime": runtime,
                "session_id": session_id,
                "from": from_value,
                "to": to_value,
            },
        )

    def breakdown(
        self,
        dimension: str = "project",
        *,
        limit: int = 20,
        agent: str | None = None,
        project: str | None = None,
        model: str | None = None,
        from_value: str | int | None = None,
        to_value: str | int | None = None,
    ) -> dict[str, Any]:
        """``GET /api/v1/usage/breakdown`` — cost rows for one dimension."""
        return self._get_json(
            PATH_BREAKDOWN,
            params={
                "dimension": dimension,
                "limit": limit,
                "agent": agent,
                "project": project,
                "model": model,
                "from": from_value,
                "to": to_value,
            },
        )

    def timeseries(
        self,
        bucket: str = "hour",
        *,
        from_value: str | int | None = None,
        to_value: str | int | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """``GET /api/v1/usage/timeseries`` — gap-filled points, ascending."""
        params = {"bucket": bucket, "from": from_value, "to": to_value}
        params.update({k: v for k, v in filters.items() if v is not None})
        return self._get_json(PATH_TIMESERIES, params=params)

    def live(self, *, window_minutes: float = 5.0) -> dict[str, Any]:
        """``GET /api/v1/usage/live`` — trailing-window burn rate + top talkers."""
        return self._get_json(PATH_LIVE, params={"window_minutes": window_minutes})

    def trends(self, *, period: str = "day", limit: int = 30, **filters: Any) -> dict[str, Any]:
        """``GET /api/v1/trends`` — bucket series plus period-over-period comparison."""
        params: dict[str, Any] = {"period": period, "limit": limit}
        params.update({k: v for k, v in filters.items() if v is not None})
        return self._get_json(PATH_TRENDS, params=params)

    # -- catalog --------------------------------------------------------------------
    def catalog_models(self) -> dict[str, Any]:
        """``GET /api/v1/catalog/models`` — the versioned price catalog."""
        return self._get_json(PATH_CATALOG_MODELS)

    def catalog_agents(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """``GET /api/v1/catalog/agents`` — known agents with rollup totals."""
        return self._get_json(PATH_CATALOG_AGENTS, params={"limit": limit, "offset": offset})

    def catalog_projects(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """``GET /api/v1/catalog/projects`` — known projects with rollup totals."""
        return self._get_json(PATH_CATALOG_PROJECTS, params={"limit": limit, "offset": offset})

    def catalog_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        project: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/v1/catalog/sessions`` — sessions with per-session cost."""
        return self._get_json(
            PATH_CATALOG_SESSIONS,
            params={"limit": limit, "offset": offset, "project": project, "agent": agent},
        )

    # -- budgets / alerts / anomalies -----------------------------------------------
    def budgets(self) -> dict[str, Any]:
        """``GET /api/v1/budgets`` — every budget, evaluated for its current window."""
        return self._get_json(PATH_BUDGETS)

    def evaluate_budgets(self, *, dry_run: bool = False) -> dict[str, Any]:
        """``POST /api/v1/budgets/evaluate`` — run one evaluation pass."""
        return self._request(
            "POST", PATH_BUDGET_EVALUATE, params={"dry_run": str(dry_run).lower()}
        ).json()

    def alerts(self, *, limit: int = 20, offset: int = 0, severity: str | None = None) -> dict[str, Any]:
        """``GET /api/v1/alerts`` — newest alert rows first."""
        return self._get_json(
            PATH_ALERTS, params={"limit": limit, "offset": offset, "severity": severity}
        )

    def alerts_summary(self) -> dict[str, Any]:
        """``GET /api/v1/alerts/summary`` — aggregate trigger counts and overspend."""
        return self._get_json(PATH_ALERTS_SUMMARY)

    def anomalies(self, *, limit: int = 20, offset: int = 0, status: str | None = None) -> dict[str, Any]:
        """``GET /api/v1/anomalies`` — detected anomalies, newest first."""
        return self._get_json(
            PATH_ANOMALIES, params={"limit": limit, "offset": offset, "status": status}
        )

    def anomalies_summary(self) -> dict[str, Any]:
        """``GET /api/v1/anomalies/summary`` — reason histogram and estimated waste."""
        return self._get_json(PATH_ANOMALIES_SUMMARY)

    def detect_anomalies(self, *, lookback_minutes: int = 60) -> dict[str, Any]:
        """``POST /api/v1/anomalies/detect`` — run one detection pass."""
        return self._request(
            "POST", PATH_ANOMALIES_DETECT, params={"lookback_minutes": lookback_minutes}
        ).json()

    def dashboard(self, *, days: int = 7) -> dict[str, Any]:
        """``GET /api/v1/dashboard/overview`` — the single-call aggregation the HUD wants."""
        return self._get_json(PATH_DASHBOARD, params={"days": days})

    # -- export ---------------------------------------------------------------------
    def export_csv(self, dataset: str = "timeseries", **params: Any) -> bytes:
        """``GET /api/v1/export/csv`` — raw CSV bytes (UTF-8 with BOM from the server)."""
        query: dict[str, Any] = {"dataset": dataset}
        query.update({k: v for k, v in params.items() if v is not None})
        response = self._request("GET", PATH_EXPORT_CSV, params=query)
        content_type = response.headers.get("content-type", "")
        if "csv" not in content_type and "text" not in content_type:
            raise ApiError(
                f"expected CSV, got content-type {content_type or 'unknown'}",
                kind="invalid",
                url=PATH_EXPORT_CSV,
            )
        return response.content

    # -- ingest (used by ``tokenlens simulate``'s self-check) ------------------------
    def ingest_ping(self) -> dict[str, Any]:
        """``GET /api/v1/ingest/ping`` — writability probe used by the simulator."""
        return self._get_json("/api/v1/ingest/ping")


def any_missing(errors: Iterable[ApiError]) -> bool:
    """True when every error is a "not implemented in this build" 404/501."""
    errors = list(errors)
    return bool(errors) and all(error.missing for error in errors)
