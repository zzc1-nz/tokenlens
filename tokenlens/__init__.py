"""TokenLens — multi-model / multi-agent LLM token cost observability platform.

The package is organised as a layered observability pipeline::

    ingest (agent runtimes) -> pricing -> raw store -> rollups -> query API
                                                        \\-> budget engine
                                                        \\-> anomaly engine
                                                        \\-> HUD client / web dashboard

Public entry points:

* :func:`tokenlens.app.create_app` — build the FastAPI application.
* :data:`tokenlens.app.app` — module level ASGI app for ``uvicorn tokenlens.app:app``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
