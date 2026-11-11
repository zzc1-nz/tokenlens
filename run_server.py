#!/usr/bin/env python
"""Development entry point: ``python run_server.py [--port 8787] [--reload]``.

Equivalent to ``uvicorn tokenlens.app:app`` but adds the local ``.pylibs`` directory to
``sys.path`` first, so the project runs on a machine where the dependencies were
installed into the repo instead of site-packages.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PYLIBS = REPO_ROOT / ".pylibs"
for candidate in (PYLIBS, REPO_ROOT):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TokenLens API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--db", default=None, help="override the SQLite path")
    args = parser.parse_args()

    if args.db:
        import os

        os.environ["TOKENLENS_DB"] = args.db

    import uvicorn

    uvicorn.run(
        "tokenlens.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
