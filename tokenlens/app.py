"""FastAPI application factory and wiring.

``create_app`` builds a fully wired application:

* settings + database on ``app.state`` (so tests can inject their own),
* optional API key auth for ``/api/v1/**``,
* every router, static dashboard mount,
* background scheduler (budget + anomaly passes) started on startup.

The module level ``app`` object exists for ``uvicorn tokenlens.app:app``.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, pricing
from .config import Settings, get_settings
from .db import Database, get_database

WEB_DIR = Path(__file__).resolve().parent / "web"

_STARTED_AT = time.time()


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Enforce ``X-API-Key`` when the deployment configured a token.

    Disabled by default (``TOKENLENS_API_TOKEN`` unset) so that local development and the
    simulator work without ceremony; a deployed collector sets the token and every
    ``/api/v1`` route inherits this dependency.
    """
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    if not settings.api_token:
        return
    if x_api_key != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop the background evaluation scheduler with the ASGI lifecycle."""
    settings: Settings = app.state.settings
    scheduler = None
    if settings.enable_scheduler:
        try:
            from . import scheduler as scheduler_module

            scheduler = scheduler_module.start(app)
        except Exception as exc:  # pragma: no cover - scheduler must never break boot
            app.state.scheduler_error = str(exc)
    try:
        yield
    finally:
        if scheduler is not None:
            try:
                from . import scheduler as scheduler_module

                await scheduler_module.stop(app)
            except Exception:  # pragma: no cover - shutdown best effort
                pass


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    """Build the TokenLens ASGI application.

    Parameters
    ----------
    settings:
        Override the environment derived settings (tests pass an isolated data dir).
    db:
        Override the database handle; defaults to the settings' SQLite path.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    database = db or Database(settings.db_path)
    database.initialize()

    app = FastAPI(
        title="TokenLens",
        version=__version__,
        description=(
            "Multi-model / multi-agent LLM token cost observability platform: ingest, "
            "aggregate, budget, detect anomalies."
        ),
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.started_at = _STARTED_AT

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- routers -----------------------------------------------------------------
    from .api import (
        alerts,
        anomalies,
        budgets,
        catalog,
        dashboard,
        export,
        health,
        ingest,
        trends,
        usage,
    )

    guarded = [Depends(require_api_key)]
    app.include_router(health.router)
    app.include_router(ingest.router, dependencies=guarded)
    app.include_router(usage.router, dependencies=guarded)
    app.include_router(trends.router, dependencies=guarded)
    app.include_router(export.router, dependencies=guarded)
    app.include_router(catalog.router, dependencies=guarded)
    app.include_router(budgets.router, dependencies=guarded)
    app.include_router(alerts.router, dependencies=guarded)
    app.include_router(anomalies.router, dependencies=guarded)
    app.include_router(dashboard.router, dependencies=guarded)

    # ---- web dashboard (static, no build step) -----------------------------------
    if WEB_DIR.exists() and (WEB_DIR / "index.html").exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def dashboard_index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Turn contract violations (bad bucket, bad window) into 400s, not 500s."""
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "price_version": pricing.PRICE_VERSION}

    return app


app = create_app()
