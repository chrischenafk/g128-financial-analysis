"""Normalize the raw TikTok sheets into clean, typed, per-period structures.

Transform layer — file 1 of 3. Input is the ingest layer's ``LoadedWorkbook``
(raw, faithful DataFrames). Output is a ``NormalizedWorkbook`` carrying:

  * ``sku_level`` — a tidy DataFrame with **exactly one row per
    (``Marketplace SKU``, period)**, the additive money/unit columns summed and
    typed, identifiers/descriptive fields preserved.
  * ``summary`` — a period-keyed dict of ``line item -> value`` for the channel
    P&L (the Summary tab), reshaped out of its periods-as-columns raw grid.

This is the layer where the §6 regression targets first become reproducible
(verified against the real workbooks: April 2026 sums to 211 active SKUs /
2,133 units / $32,033.09 gross / $7,595.09 profit from ``sku_level`` alone).

What this module DOES:
  * Collapse the two-rows-per-SKU-per-period structure by **summing** the
    additive columns, grouped on ``Marketplace SKU`` + period. The raw sheet has
    duplicate keys (26 ``(SKU, period)`` groups across the two periods of a real
    file — typically one active row and one dormant zero/None row). Summing is
    safe and lossless: the dormant row contributes zeros, so no real value is
    dropped. We never pick one row and discard the other.
  * Coerce additive numeric cells to numbers with blank/None → 0 **before**
    summing (some dormant rows carry a None profit).
  * Normalize the ``Date Range`` string ("04/01/2026 - 04/30/2026") into the
    ``Period`` type from ``period_parser`` ("2026-04"), so periods join cleanly
    across layers.

What this module deliberately does NOT do (later layers' jobs):
  * No derived metrics — no margin %, no per-unit recompute, no MoM/YoY deltas,
    no ranks/segments/bridges. The one borderline case (summing duplicate rows)
    is normalization, not metric computation, so it lives here.
  * No active-SKU filtering of the base table. "Active" is a downstream *view*,
    not a row deletion: the base table keeps the full catalog, and
    ``active_skus()`` is provided as a non-destructive filter helper.
  * No sign flipping — costs stay negative exactly as loaded.
  * No second-workbook pairing and no reading of the data-quality sheets.

Column handling rests on one real-data fact (confirmed by direct inspection):
the per-unit / rate columns are **intensive** — summing two rows' per-unit
prices is meaningless (10.99 + 10.99 ≠ a price). Only the **extensive**
``Total*`` / fee / count columns are additive. So columns are partitioned by
name into: the key, the period, identifiers (first), descriptive (first), the
24 additive columns (summed), and the per-unit/rate columns (excluded here —
the metrics layer recomputes per-unit values from the summed totals, never by
summing a price). The four lists below partition all 43 columns exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.ingest.excel_loader import LoadedWorkbook
from src.ingest.period_parser import Period
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Column roles in the Profit Margin sheet (keyed by NAME, not position — the
# loader keys columns by discovered header, so names are the stable contract).
# ─────────────────────────────────────────────────────────────────────────────
PERIOD_RAW_COLUMN = "Date Range"        # "04/01/2026 - 04/30/2026"
KEY_COLUMN = "Marketplace SKU"          # the business / dedup / join key
UNITS_COLUMN = "Total Sold Units"       # drives the "active" view
GROSS_COLUMN = "Total Gross Sale"
PROFIT_COLUMN = "Total Profit"

# Identifiers: carried through, NOT summed. (TikTok SKU ID / Product ID are
# numeric but are IDs — summing them would be nonsense. Within a duplicate group
# the two rows may hold two different TikTok listing IDs for the same business
# SKU; "first" keeps one valid id. Finale ID is constant within the group.)
IDENTIFIER_COLUMNS = ("TikTok SKU ID", "TikTok Product ID", "Finale ID")

# Descriptive: constant within a (SKU, period) group; "first" is exact.
DESCRIPTIVE_COLUMNS = ("Product Name", "Category", "Theme", "Package Type")

# Additive (extensive) money / unit / count columns — the only columns we sum.
# These are the SKU-level analogs of the 24 Summary line items. Costs are signed
# negative and stay that way; ``Total Profit`` is itself a summable total.
ADDITIVE_COLUMNS = (
    "Total Profit",
    "Total Gross Sale",
    "Total Sold Units",
    "Total Sold Orders",
    "Total Other Income",
    "Total Refund",
    "Total Returned Units",
    "Total Returned Orders",
    "Total Discount",
    "Total Tiktok Shipping cost",
    "Total Referral Fee",
    "Total Affiliate commission",
    "Refund administration fee",
    "Affiliate Shop Ads commission",
    "Co-funded promotion service fee",
    "Campaign service fee",
    "Total AD Cost",
    "Total Order ShippingEasy Cost",
    "Total ShippingEasy Supply Cost",
    "Total Returned Shipping Cost",
    "Total Other Expense",
    "Total Cost of Goods Sold",
    "Total Ocean Freight Cost",
    "Total Customs",
)

# Intensive per-unit / rate columns — documented for transparency. These are
# deliberately EXCLUDED from sku_level (summing them is wrong); the metrics layer
# recomputes any per-unit value it needs from the summed totals above.
PER_UNIT_EXCLUDED_COLUMNS = (
    "Selling Price per Unit",
    "Fixed Cost per Unit - Estimated",
    "Cost of Goods Sold Per Unit",
    "Ocean Freight Cost Per Unit",
    "Customs Per Unit",
    "ShippingEasy Per Unit",
    "Potential Maximum Profit per Unit - Estimated",
    "Net Profit per Unit",
    "Profit % per Unit",
    "Return Rate",
)

# Columns that must be present or the sheet is unusable (fail loud, AGENTS.md §8).
_REQUIRED_PM_COLUMNS = (PERIOD_RAW_COLUMN, KEY_COLUMN, GROSS_COLUMN, PROFIT_COLUMN, UNITS_COLUMN)

# Period header inside a date-range string: START date defines the period.
_DATE_RANGE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2})/(\d{2})/(\d{4})")


# ─────────────────────────────────────────────────────────────────────────────
# Output container
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NormalizedWorkbook:
    """Clean, per-period structures normalized from one workbook.

    ``sku_level`` has one row per (``Marketplace SKU``, ``period``); its
    ``period`` column holds ``Period`` objects (str form "2026-04"). ``summary``
    maps each ``Period`` to a ``{line item: value}`` dict. ``periods`` is the
    sorted tuple of periods present.
    """

    source_path: Path
    periods: tuple[Period, ...]
    sku_level: pd.DataFrame
    summary: dict[Period, dict[str, float | None]]


# ─────────────────────────────────────────────────────────────────────────────
# Period helpers
# ─────────────────────────────────────────────────────────────────────────────
def _try_period_from_date_range(value: object) -> Period | None:
    """Parse a ``Period`` from a "MM/DD/YYYY - MM/DD/YYYY" string, else None.

    Non-strings and non-matching strings (e.g. "Note") return None — used both
    to detect the period columns in the Summary header and to map SKU rows.
    """
    if not isinstance(value, str):
        return None
    match = _DATE_RANGE_RE.search(value)
    if match is None:
        return None
    start_month = int(match.group(1))
    start_year = int(match.group(3))
    return Period(start_year, start_month)


def _period_for_row(value: object) -> Period:
    """Map a Profit Margin ``Date Range`` cell to a ``Period``, or fail loud.

    Every Profit Margin row carries a period; an unparseable value is a real
    structural surprise, not something to silently drop.
    """
    period = _try_period_from_date_range(value)
    if period is None:
        raise ValueError(
            f"Unparseable {PERIOD_RAW_COLUMN!r} value in the Profit Margin "
            f"sheet: {value!r}. Expected a 'MM/DD/YYYY - MM/DD/YYYY' range."
        )
    return period


# ─────────────────────────────────────────────────────────────────────────────
# Profit Margin → tidy SKU level
# ─────────────────────────────────────────────────────────────────────────────
def normalize_profit_margin(profit_margin: pd.DataFrame) -> pd.DataFrame:
    """Collapse the raw Profit Margin sheet to one row per (SKU, period).

    Additive columns are coerced to numeric (blank/None → 0) and summed within
    each (``Marketplace SKU``, period) group; identifiers/descriptive fields take
    the first value in the group; per-unit/rate columns are dropped. The full
    catalog is retained — no active-SKU filtering here.

    Raises:
        ValueError: if a required column is missing, or a ``Date Range`` cell
            cannot be parsed.
    """
    missing = [c for c in _REQUIRED_PM_COLUMNS if c not in profit_margin.columns]
    if missing:
        raise ValueError(
            f"Profit Margin sheet is missing required column(s) {missing}. "
            f"Present columns: {list(profit_margin.columns)}."
        )

    work = profit_margin.copy()

    # Tripwire: any column we don't recognize (so a new sheet revision can't
    # silently slip an unhandled column past us). AGENTS.md §9.
    classified = {
        PERIOD_RAW_COLUMN,
        KEY_COLUMN,
        *IDENTIFIER_COLUMNS,
        *DESCRIPTIVE_COLUMNS,
        *ADDITIVE_COLUMNS,
        *PER_UNIT_EXCLUDED_COLUMNS,
    }
    unexpected = [c for c in work.columns if c not in classified]
    if unexpected:
        logger.warning(
            "Profit Margin has %d unclassified column(s) — neither summed nor "
            "carried: %s.",
            len(unexpected),
            unexpected,
        )

    # Drop rows without a business key (cannot be aggregated to a SKU). Real data
    # has none; log loudly if that ever changes rather than absorbing it.
    null_key = work[KEY_COLUMN].isna()
    if null_key.any():
        logger.warning(
            "Dropping %d Profit Margin row(s) with a null %r — no business key.",
            int(null_key.sum()),
            KEY_COLUMN,
        )
        work = work[~null_key]

    # Normalize the period (fail loud on a bad Date Range).
    work["period"] = work[PERIOD_RAW_COLUMN].map(_period_for_row)

    # Coerce additive columns to numeric with NaN→0 BEFORE summing.
    present_additive = [c for c in ADDITIVE_COLUMNS if c in work.columns]
    for col in present_additive:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    present_carry = [c for c in (*IDENTIFIER_COLUMNS, *DESCRIPTIVE_COLUMNS) if c in work.columns]

    rows_in = len(work)
    group_sizes = work.groupby(["period", KEY_COLUMN], sort=False).size()
    collapsed = int((group_sizes > 1).sum())

    agg_map: dict[str, str] = {c: "sum" for c in present_additive}
    agg_map.update({c: "first" for c in present_carry})
    grouped = (
        work.groupby(["period", KEY_COLUMN], sort=False)
        .agg(agg_map)
        .reset_index()
    )

    # Deterministic order: by period, then SKU; columns in a stable, readable order.
    grouped = grouped.sort_values(["period", KEY_COLUMN]).reset_index(drop=True)
    ordered_cols = ["period", KEY_COLUMN, *present_carry, *present_additive]
    grouped = grouped[ordered_cols]

    logger.info(
        "Normalized Profit Margin: %d raw rows → %d unique (SKU, period) rows "
        "(%d duplicate-key group(s) collapsed by summing).",
        rows_in,
        len(grouped),
        collapsed,
    )
    return grouped


# ─────────────────────────────────────────────────────────────────────────────
# Summary grid → tidy period-keyed line items
# ─────────────────────────────────────────────────────────────────────────────
def normalize_summary(summary_raw: pd.DataFrame) -> dict[Period, dict[str, float | None]]:
    """Reshape the raw Summary grid into ``{Period: {line item: value}}``.

    Row 0 is the header (``['Summaries', <period1>, <period2>, 'Note', ...]``);
    the period columns are discovered by matching the date-range pattern in that
    header (discover-don't-assume, not a hardcoded column index). Each later row
    is a line item (col 0 = name), with that period's value in the period column.
    Values keep their raw sign (costs negative); blanks become None (a channel
    line is genuinely missing, not zero).
    """
    if summary_raw.empty:
        raise ValueError("Summary sheet is empty — cannot locate period headers.")

    header = summary_raw.iloc[0].tolist()
    period_cols: dict[int, Period] = {}
    for col_idx, cell in enumerate(header):
        period = _try_period_from_date_range(cell)
        if period is not None:
            period_cols[col_idx] = period

    if not period_cols:
        raise ValueError(
            "Could not find any 'MM/DD/YYYY - MM/DD/YYYY' period header in the "
            f"Summary sheet's first row: {header}."
        )

    # Locate the optional free-text "Note" column (discover-don't-assume, same as
    # the period columns). Its per-line strings carry deferred data-quality figures
    # (mapped ad cost, unsettled referral fee, an unallocated marketplace credit)
    # that channel caveats parse downstream. We store each row's note under a
    # "{line item}__note" key: the suffix can never collide with a numeric line
    # item, so the float-coercion below never touches it and existing numeric-key
    # consumers (writer, metrics) ignore it. Optional — older workbooks omit it.
    note_col: int | None = None
    for col_idx, cell in enumerate(header):
        if isinstance(cell, str) and cell.strip().lower() == "note":
            note_col = col_idx
            break

    result: dict[Period, dict[str, float | None]] = {p: {} for p in period_cols.values()}
    note_count = 0
    for row_idx in range(1, len(summary_raw)):
        name = summary_raw.iat[row_idx, 0]
        if not isinstance(name, str) or not name.strip():
            continue  # blank / non-label row
        line_item = name.strip()
        for col_idx, period in period_cols.items():
            raw_value = summary_raw.iat[row_idx, col_idx]
            number = pd.to_numeric(raw_value, errors="coerce")
            result[period][line_item] = None if pd.isna(number) else float(number)

        # The Note column is a single shared column; attach its text to every
        # period for this line item. Blank / NaN notes are skipped silently (no
        # None __note key) — a note is either present text or absent.
        if note_col is not None:
            note = summary_raw.iat[row_idx, note_col]
            if isinstance(note, str) and note.strip():
                note_str = note.strip()
                # DEBUG so the first real run reveals the exact strings present —
                # the downstream patterns are calibrated against these.
                logger.debug("Summary note [%s]: %r", line_item, note_str)
                for period in period_cols.values():
                    result[period][f"{line_item}__note"] = note_str
                note_count += 1

    if note_col is not None:
        logger.debug("Note column found at col %d — %d note string(s) extracted.",
                     note_col, note_count)
    else:
        logger.debug("No 'Note' column in the Summary header — skipping note parsing (optional).")

    logger.info(
        "Normalized Summary: %d period(s) × %s line items.",
        len(result),
        # Count only the numeric line items, not the __note companions.
        {str(p): sum(1 for k in items if not k.endswith("__note"))
         for p, items in result.items()},
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Active-SKU view (non-destructive helper — NOT applied to the base table)
# ─────────────────────────────────────────────────────────────────────────────
def active_skus(source: NormalizedWorkbook | pd.DataFrame) -> pd.DataFrame:
    """Return only the rows with ``Total Sold Units > 0`` (a filtered copy).

    "Active" is a view, not a deletion — the base ``sku_level`` table is left
    intact (full catalog). For April 2026 this yields 211 rows, matching the
    Phase-1 baseline.
    """
    df = source.sku_level if isinstance(source, NormalizedWorkbook) else source
    return df[df[UNITS_COLUMN] > 0].copy()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def normalize_workbook(loaded: LoadedWorkbook) -> NormalizedWorkbook:
    """Normalize one ``LoadedWorkbook`` into a ``NormalizedWorkbook``.

    Logs a per-period self-check (rows, active count, and summed gross / profit /
    units) so the §6 April-2026 targets can be eyeballed early. Hard assertions
    against the Summary tab belong to the package/verify layer, not here.
    """
    sku_level = normalize_profit_margin(loaded.profit_margin)
    summary = normalize_summary(loaded.summary)

    periods = tuple(sorted(sku_level["period"].unique()))

    for period in periods:
        sub = sku_level[sku_level["period"] == period]
        active = int((sub[UNITS_COLUMN] > 0).sum())
        logger.info(
            "Self-check %s: %d SKUs, %d active | units=%d gross=%.2f profit=%.2f",
            period,
            len(sub),
            active,
            int(sub[UNITS_COLUMN].sum()),
            float(sub[GROSS_COLUMN].sum()),
            float(sub[PROFIT_COLUMN].sum()),
        )

    return NormalizedWorkbook(
        source_path=loaded.path,
        periods=periods,
        sku_level=sku_level,
        summary=summary,
    )
