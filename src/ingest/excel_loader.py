"""Excel loader — the final ingest file, and the last point the data is untouched.

Single job: given a VALIDATED workbook path (period_parser.py already confirmed
the filename agrees with the in-workbook headers), load the relevant sheets into
raw pandas DataFrames that still look exactly like the workbook, and hand them
off in a named container.

This module is deliberately "dumb". It does NOT clean, type-coerce, sign-flip,
fill NaNs, filter to active SKUs, dedupe the known duplicate row, merge the two
periods, or compute any total/margin. All of that is the transform layer's job.
The full catalog (~7,300 rows/period) and the two-rows-per-SKU-per-period
structure are loaded exactly as present — wrinkles and all.

What it DOES decide is the one structural question pandas can't guess: where the
header row is. For the Profit Margin sheet the real workbooks carry title/junk
rows above the column headers, so the header row is *discovered* by locating the
row that contains the known anchor columns ("Marketplace SKU" + "Date Range") —
the same discover-don't-assume philosophy as the period parser. The Summary
sheet (periods-as-columns, line-items-as-rows) has no single clean header row, so
it is loaded as a faithful raw grid (``header=None``) and left for transform to
interpret.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.ingest.period_parser import FilePeriods
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Sheet names — all TikTok-specific (current scope is TikTok Shop only).
# ─────────────────────────────────────────────────────────────────────────────
SUMMARY_SHEET = "TikTok Summary"
PROFIT_MARGIN_SHEET = "TikTok Profit Margin"

# Required: the workbook is unusable without these two.
REQUIRED_SHEETS = (SUMMARY_SHEET, PROFIT_MARGIN_SHEET)

# Optional data-quality sheets. A workbook missing one must not crash the run
# (graceful degradation, AGENTS.md §8) — we log a warning and store None.
UNMAPPED_ADS_SHEET = "TikTok Unmapped Ads"
CANCELED_SHIPPING_SHEET = "TikTok Canceled Shipping"
UNMAPPED_PAYOUT_SHEET = "TikTok Unmapped Payout"
ORDERS_WITHOUT_PAYOUT_SHEET = "PM_TikTok_outOrdersWithoutPayou"

# Anchor columns that identify the Profit Margin header row wherever it sits.
_PROFIT_MARGIN_ANCHORS = ("Marketplace SKU", "Date Range")

# How many leading rows to scan when hunting for a header row. The real header
# sits within the first handful of rows; this caps the search cheaply.
_HEADER_SCAN_DEPTH = 25


# ─────────────────────────────────────────────────────────────────────────────
# Output container
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LoadedWorkbook:
    """Raw, faithfully-loaded sheets from one workbook.

    The two core sheets (``summary``, ``profit_margin``) are always present — if
    either were missing, ``load_workbook`` would have raised. The four
    data-quality fields are ``None`` when the corresponding sheet is absent
    (sentinel for "sheet not in workbook", distinct from "present but empty").

    Named fields (not a loose tuple) keep the transform layer readable.
    """

    path: Path
    summary: pd.DataFrame
    profit_margin: pd.DataFrame
    unmapped_ads: pd.DataFrame | None
    canceled_shipping: pd.DataFrame | None
    unmapped_payout: pd.DataFrame | None
    orders_without_payout: pd.DataFrame | None
    periods: FilePeriods | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Header discovery
# ─────────────────────────────────────────────────────────────────────────────
def _find_header_row(raw: pd.DataFrame, anchors: tuple[str, ...]) -> int:
    """Return the index of the first row whose cells contain all ``anchors``.

    ``raw`` is the sheet read with ``header=None`` (purely positional), so each
    row is a horizontal slice of cell values. We compare on stripped strings so
    incidental whitespace in the workbook doesn't defeat the match.

    Raises:
        ValueError: if no row within the scan depth contains every anchor.
    """
    wanted = {a.strip() for a in anchors}
    scan_limit = min(len(raw), _HEADER_SCAN_DEPTH)
    for row_idx in range(scan_limit):
        cells = {
            str(v).strip()
            for v in raw.iloc[row_idx].tolist()
            if v is not None and not (isinstance(v, float) and pd.isna(v))
        }
        if wanted.issubset(cells):
            return row_idx
    raise ValueError(
        f"Could not locate the header row: no row in the first {scan_limit} "
        f"rows contains all required columns {anchors}."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def load_workbook(path: Path, periods: FilePeriods | None = None) -> LoadedWorkbook:
    """Load the relevant sheets from ``path`` into raw DataFrames.

    ``periods`` (from period_parser.parse_and_validate) is carried through onto
    the result for downstream convenience — this loader does not re-validate the
    filename or interpret the periods.

    A single ``pd.ExcelFile`` handle is opened (openpyxl engine) and always
    closed via the context manager — Windows holds a lock on an open ``.xlsm``,
    so a leaked handle would block later steps.

    Raises:
        ValueError: if a required sheet (Summary or Profit Margin) is absent, or
            the Profit Margin header row cannot be discovered.
    """
    with pd.ExcelFile(path, engine="openpyxl") as xls:
        available = set(xls.sheet_names)

        missing_required = [s for s in REQUIRED_SHEETS if s not in available]
        if missing_required:
            raise ValueError(
                f"Workbook {path.name!r} is missing required sheet(s) "
                f"{missing_required}. Present sheets: {xls.sheet_names}."
            )

        # ── Summary: faithful raw grid (no header assumed) ───────────────────
        summary = xls.parse(SUMMARY_SHEET, header=None)
        logger.info(
            "Loaded %r: %d rows x %d cols (raw grid).",
            SUMMARY_SHEET,
            summary.shape[0],
            summary.shape[1],
        )

        # ── Profit Margin: discover the header row, then load with it ────────
        pm_raw = xls.parse(PROFIT_MARGIN_SHEET, header=None)
        try:
            header_idx = _find_header_row(pm_raw, _PROFIT_MARGIN_ANCHORS)
        except ValueError as exc:
            raise ValueError(f"In sheet {PROFIT_MARGIN_SHEET!r}: {exc}") from exc
        profit_margin = xls.parse(PROFIT_MARGIN_SHEET, header=header_idx)
        logger.info(
            "Loaded %r: %d rows x %d cols (header discovered at row %d).",
            PROFIT_MARGIN_SHEET,
            profit_margin.shape[0],
            profit_margin.shape[1],
            header_idx,
        )

        # ── Optional data-quality sheets: load if present, else None ─────────
        def _load_optional(sheet_name: str) -> pd.DataFrame | None:
            if sheet_name not in available:
                logger.warning(
                    "Optional sheet %r absent from %s — storing None.",
                    sheet_name,
                    path.name,
                )
                return None
            df = xls.parse(sheet_name)
            logger.info(
                "Loaded %r: %d rows x %d cols.",
                sheet_name,
                df.shape[0],
                df.shape[1],
            )
            return df

        unmapped_ads = _load_optional(UNMAPPED_ADS_SHEET)
        canceled_shipping = _load_optional(CANCELED_SHIPPING_SHEET)
        unmapped_payout = _load_optional(UNMAPPED_PAYOUT_SHEET)
        orders_without_payout = _load_optional(ORDERS_WITHOUT_PAYOUT_SHEET)

    return LoadedWorkbook(
        path=path,
        summary=summary,
        profit_margin=profit_margin,
        unmapped_ads=unmapped_ads,
        canceled_shipping=canceled_shipping,
        unmapped_payout=unmapped_payout,
        orders_without_payout=orders_without_payout,
        periods=periods,
    )
