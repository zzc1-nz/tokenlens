"""``tokenlens_cli`` — the desktop-style HUD client for the TokenLens platform.

The package is deliberately split into small, individually testable layers:

``api``
    A thin ``httpx`` client with timeouts and *friendly* errors (the HUD must be able to
    tell "server is down" from "endpoint does not exist yet").
``render``
    Pure rendering: number formatting, ``█``/``░`` bar maths, the KPI/bars layout model and
    both renderers (plain text for CI, ``rich`` renderables for the live view).
``hud``
    The ai-token-hud style live loop: non-blocking key reader, refresh timer, OFFLINE
    banner with automatic retry.
``commands``
    One function per subcommand; each returns a process exit code and never raises for
    expected failures.
``main``
    The argparse surface behind the ``tokenlens`` console script.

Everything below the ``hud`` loop is free of terminal side effects, which is what makes
the test suite able to assert on rendered text instead of screen scraping.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Kept in sync with the server package (``tokenlens.__version__``) by ``version`` tests.
__version__ = "0.1.0"
