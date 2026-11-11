"""CSV export endpoint.

Design notes:

* **UTF-8 with BOM** (``utf-8-sig``). Excel on Windows mis-detects plain UTF-8 CSV and
  renders Chinese project names as mojibake; the BOM is the only reliable fix and every
  other consumer (pandas, LibreOffice) tolerates it.
* **Streamed via ``io.StringIO`` + ``csv.writer``**, not string concatenation, so values
  containing commas, quotes or newlines (alert messages, tool output snippets) are escaped
  correctly instead of corrupting the file.
* The header row is always English ``snake_case`` — the file is machine readable first,
  and the dashboard/CLI render the human labels.
* ``limit`` defaults to 5000 and is capped at 100000 so a careless client cannot ask the
  server to materialise the entire ledger in memory.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from .. import query, timeutil
from ..db import Database, get_db

router = APIRouter(prefix="/api/v1/export", tags=["export"])

#: Hard cap on exported rows (contract §6).
MAX_EXPORT_ROWS = 100_000


def _display_tz(request: Request) -> str:
    """Display timezone for the ``label`` column of a timeseries export."""
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "display_tz", None) or query.DEFAULT_TZ


def _csv_bytes(header: list[str], rows: list[list[Any]]) -> bytes:
    """Serialise a header + rows matrix to BOM-prefixed UTF-8 CSV bytes."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _filename(dataset: str) -> str:
    """``tokenlens-<dataset>-<utcstamp>.csv`` (no colons: invalid on Windows)."""
    stamp = timeutil.ms_to_dt(timeutil.now_ms()).strftime("%Y%m%dT%H%M%SZ")
    return f"tokenlens-{dataset}-{stamp}.csv"


@router.get("/csv", summary="Export a dataset as UTF-8 (BOM) CSV")
def export_csv(
    request: Request,
    db: Database = Depends(get_db),
    dataset: str = Query(
        default="timeseries",
        description="timeseries|breakdown|sessions|snapshots|alerts|anomalies|budgets",
    ),
    bucket: str = Query(default="hour", description="timeseries bucket size"),
    dimension: str = Query(default="agent", description="breakdown dimension"),
    limit: int = Query(default=5000, ge=1, le=MAX_EXPORT_ROWS),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    default_hours: float = Query(default=24.0, gt=0, le=24 * 366),
) -> Response:
    """Export one dataset as a downloadable CSV attachment.

    ``dataset=snapshots`` exports the raw ledger rows; the other datasets export the same
    aggregates the JSON API returns, so a spreadsheet and the dashboard can never disagree.
    Invalid ``dataset``/``bucket``/``dimension`` or an inverted window is an HTTP 400.
    """
    canonical_dataset = query.normalize_dataset(dataset)
    filters = query.normalize_filters(
        agent=agent, project=project, model=model, runtime=runtime, session_id=session_id
    )

    start_ms: int | None = None
    end_ms: int | None = None
    if canonical_dataset in ("timeseries", "breakdown", "snapshots"):
        start_ms, end_ms = query.resolve_window(from_, to, default_hours=default_hours)
    elif from_ is not None or to is not None:
        # History datasets accept an optional window to narrow the export.
        start_ms, end_ms = query.resolve_window(from_, to, default_hours=default_hours)

    header, rows = query.csv_rows(
        db,
        canonical_dataset,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=limit,
        bucket=bucket,
        dimension=dimension,
        tz_name=_display_tz(request),
        **filters,
    )

    return Response(
        content=_csv_bytes(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(canonical_dataset)}"',
            "X-Row-Count": str(len(rows)),
            "Cache-Control": "no-store",
        },
    )
