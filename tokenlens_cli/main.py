"""``tokenlens`` console entry point — the argparse surface.

The parser is built by :func:`build_parser` so tests can assert on the command surface
without touching ``sys.argv``. :func:`main` wires the parser to
:data:`tokenlens_cli.commands.COMMANDS` and translates its exit code into a process exit
code.

Subcommands (see ``docs/API_CONTRACT.md`` §10)::

    tokenlens hud      [--server URL] [--project NAME] [--interval 1.0] [--no-color]
    tokenlens live     [--server URL] [--window 5]
    tokenlens summary  [--server URL] [--from ...] [--to ...] [--agent ...]
    tokenlens trends   [--server URL] [--period day|week] [--limit 30]
    tokenlens breakdown --dimension agent|project|model|session
    tokenlens budgets / alerts / anomalies
    tokenlens export   --dataset timeseries|breakdown|sessions --out FILE
    tokenlens detect   [--lookback 60] [--evaluate-budgets]
    tokenlens simulate ...      (delegates to tools/simulate.py)
    tokenlens version

Every subcommand accepts ``--server`` (default ``http://127.0.0.1:8787``) and the read
commands accept ``--json``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from . import __version__
from .api import DEFAULT_SERVER, DEFAULT_TIMEOUT
from .commands import COMMANDS

PROG = "tokenlens"

EPILOG = """\
examples:
  tokenlens hud                          # live view, 1s refresh
  tokenlens hud --once --no-color        # one snapshot, CI friendly
  tokenlens summary --from 2026-11-10 --agent claude-code
  tokenlens breakdown --dimension model --json
  tokenlens export --dataset timeseries --out usage.csv
  tokenlens simulate --mode incident --duration 30 --report
  python tools/simulate.py --dry-run --mode incident --target-tokens 100000

environment:
  TOKENLENS_API_TOKEN   sent as X-API-Key when the server enables auth
"""

#: Dimensions offered by ``breakdown`` (``budget`` is a HUD-only view).
BREAKDOWN_DIMENSIONS = ("agent", "project", "model", "runtime", "session")
EXPORT_DATASETS = ("timeseries", "breakdown", "sessions", "snapshots", "alerts", "anomalies", "budgets")
TREND_PERIODS = ("day", "week", "month")
SIM_MODES = ("burst", "steady", "incident", "mixed")


def _add_common(parser: argparse.ArgumentParser, *, json_flag: bool = True) -> None:
    """Add ``--server``/``--timeout``/``--api-key``/``--json``/``--no-color``."""
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"TokenLens server base URL (default {DEFAULT_SERVER})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="X-API-Key value when the server enables auth (or set TOKENLENS_API_TOKEN)",
    )
    if json_flag:
        parser.add_argument("--json", action="store_true", help="emit raw JSON instead of tables")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colour/ANSI styling (also implied when stdout is not a TTY)",
    )


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", default=None, help="filter by agent id")
    parser.add_argument("--project", default=None, help="filter by project")
    parser.add_argument("--model", default=None, help="filter by model")


def build_parser() -> argparse.ArgumentParser:
    """Construct the full ``tokenlens`` argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="TokenLens HUD — multi-model / multi-agent LLM token cost observability",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
        help="print the client version and exit",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ---- hud ---------------------------------------------------------------------
    hud = sub.add_parser(
        "hud",
        help="live ai-token-hud style view (one snapshot when piped)",
        description=(
            "Live view: KPI header, budget bars and horizontal cost bars for the current "
            "dimension. Keys: p project, a agent, m model, b budget, q quit. When stdout "
            "is not a TTY (or --once/--json is passed) exactly one snapshot is printed."
        ),
    )
    _add_common(hud)
    hud.add_argument("--project", default=None, help="pin the view to one project")
    hud.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="refresh interval in seconds (default 1.0, clamped to 0.2..60)",
    )
    hud.add_argument(
        "--dimension",
        choices=("project", "agent", "model", "budget"),
        default="project",
        help="initial bar dimension (default project)",
    )
    hud.add_argument(
        "--once",
        action="store_true",
        help="render exactly one snapshot and exit (implied for non-TTY output)",
    )
    hud.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="stop after N frames (mostly for testing the loop)",
    )

    # ---- live --------------------------------------------------------------------
    live = sub.add_parser("live", help="trailing-window burn rate and top talkers")
    _add_common(live)
    live.add_argument("--window", type=float, default=5.0, help="trailing window minutes (default 5)")

    # ---- summary -----------------------------------------------------------------
    summary = sub.add_parser("summary", help="aggregate Totals for a filtered window")
    _add_common(summary)
    _add_filters(summary)
    summary.add_argument("--from", dest="from_value", default=None, help="window start (ISO or epoch ms)")
    summary.add_argument("--to", dest="to_value", default=None, help="window end (ISO or epoch ms)")

    # ---- trends ------------------------------------------------------------------
    trends = sub.add_parser("trends", help="cost trend buckets with period-over-period delta")
    _add_common(trends)
    _add_filters(trends)
    trends.add_argument("--period", choices=TREND_PERIODS, default="day", help="bucket period (default day)")
    trends.add_argument("--limit", type=int, default=30, help="number of buckets (default 30)")

    # ---- breakdown ---------------------------------------------------------------
    breakdown = sub.add_parser("breakdown", help="cost share bars for one dimension")
    _add_common(breakdown)
    _add_filters(breakdown)
    breakdown.add_argument(
        "--dimension",
        choices=BREAKDOWN_DIMENSIONS,
        default="project",
        help="dimension to break down by (default project)",
    )
    breakdown.add_argument("--limit", type=int, default=20, help="max rows (default 20)")

    # ---- budgets / alerts / anomalies -------------------------------------------
    budgets = sub.add_parser("budgets", help="budget utilisation and projected overspend")
    _add_common(budgets)

    alerts = sub.add_parser("alerts", help="recent budget alerts with trigger counts")
    _add_common(alerts)
    alerts.add_argument("--limit", type=int, default=20, help="max rows (default 20)")
    alerts.add_argument(
        "--severity",
        choices=("warning", "critical", "exceeded"),
        default=None,
        help="filter by severity",
    )

    anomalies = sub.add_parser("anomalies", help="detected anomalies with root cause evidence")
    _add_common(anomalies)
    anomalies.add_argument("--limit", type=int, default=20, help="max rows (default 20)")
    anomalies.add_argument(
        "--status",
        choices=("open", "acknowledged", "resolved"),
        default=None,
        help="filter by status",
    )

    # ---- export ------------------------------------------------------------------
    export = sub.add_parser("export", help="download a CSV dataset")
    _add_common(export)
    export.add_argument("--dataset", choices=EXPORT_DATASETS, default="timeseries", help="dataset to export")
    export.add_argument("--out", default=None, help="output file (stdout when omitted)")
    export.add_argument("--limit", type=int, default=None, help="max rows")
    export.add_argument("--from", dest="from_value", default=None, help="window start (ISO or epoch ms)")
    export.add_argument("--to", dest="to_value", default=None, help="window end (ISO or epoch ms)")
    export.add_argument("--bucket", default=None, help="bucket for dataset=timeseries")
    export.add_argument("--dimension", default=None, help="dimension for dataset=breakdown")

    # ---- detect ------------------------------------------------------------------
    detect = sub.add_parser("detect", help="run one anomaly detection pass")
    _add_common(detect)
    detect.add_argument("--lookback", type=int, default=60, help="lookback minutes (default 60)")
    detect.add_argument(
        "--evaluate-budgets",
        action="store_true",
        help="also run one budget evaluation pass",
    )
    detect.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate budgets without sending notifications",
    )

    # ---- simulate ----------------------------------------------------------------
    simulate = sub.add_parser(
        "simulate",
        help="load generator (delegates to tools/simulate.py)",
        description=(
            "Async load generator: POSTs batches to /api/v1/ingest/snapshots. Modes: "
            "burst, steady, incident, mixed. See `tokenlens simulate --help`."
        ),
    )
    simulate.add_argument("--server", default=DEFAULT_SERVER, help="server base URL")
    simulate.add_argument("--mode", choices=SIM_MODES, default="steady", help="workload shape")
    simulate.add_argument("--duration", type=float, default=10.0, help="seconds to run (default 10)")
    simulate.add_argument("--agents", type=int, default=5, help="number of distinct agents")
    simulate.add_argument("--projects", type=int, default=4, help="number of distinct projects")
    simulate.add_argument("--sessions", type=int, default=20, help="number of concurrent sessions")
    simulate.add_argument("--target-tokens", type=int, default=0, help="stop once this many tokens were generated")
    simulate.add_argument("--concurrency", type=int, default=4, help="concurrent in-flight batches")
    simulate.add_argument("--seed", type=int, default=42, help="RNG seed (deterministic output)")
    simulate.add_argument("--batch-size", type=int, default=100, help="snapshots per request (max 500)")
    simulate.add_argument("--dry-run", action="store_true", help="generate and validate without HTTP")
    simulate.add_argument("--report", action="store_true", help="print throughput report")
    simulate.add_argument("--json", action="store_true", help="emit the report as JSON")
    simulate.add_argument("--api-key", default=None, help="X-API-Key when the server enables auth")
    simulate.add_argument("--timeout", type=float, default=10.0, help="per-request timeout seconds")

    # ---- version -----------------------------------------------------------------
    version = sub.add_parser("version", help="print client/server version information")
    _add_common(version)
    version.add_argument(
        "--offline",
        action="store_true",
        help="do not probe the server (print the client version only)",
    )

    return parser


def main(argv: Sequence[str] | None = None, *, out: Any = None) -> int:
    """Run the CLI. Returns the process exit code (never raises for user errors)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse already restricts choices
        parser.error(f"unknown command: {args.command}")
        return 2
    try:
        return int(handler(args, out=out))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130
    except BrokenPipeError:  # pragma: no cover - `| head` style pipelines
        return 0
    except ValueError as exc:
        sys.stderr.write(f"tokenlens {args.command}: {exc}\n")
        return 3


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
