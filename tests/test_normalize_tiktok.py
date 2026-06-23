"""Tests for src/transform/normalize_tiktok.py.

Synthetic in-memory frames — no real company data. The normalizer's contract:
collapse duplicate (SKU, period) rows by SUMMING the additive columns (None→0,
no row dropped), normalize the Date Range string to a Period, keep the full
catalog (active filtering is a non-destructive helper, not a deletion), and
reshape the Summary grid into period-keyed line items.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.ingest.period_parser import Period
from src.transform.normalize_tiktok import (
    GROSS_COLUMN,
    KEY_COLUMN,
    PERIOD_RAW_COLUMN,
    PROFIT_COLUMN,
    UNITS_COLUMN,
    active_skus,
    normalize_profit_margin,
    normalize_summary,
)

APR = "04/01/2026 - 04/30/2026"
MAR = "03/01/2026 - 03/31/2026"

# Minimal Profit-Margin columns: the required set plus one identifier and one
# descriptive field, enough to exercise grouping/summing/first.
_COLS = [
    PERIOD_RAW_COLUMN,
    KEY_COLUMN,
    "TikTok SKU ID",
    "Product Name",
    PROFIT_COLUMN,
    GROSS_COLUMN,
    UNITS_COLUMN,
]


def _pm(rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLS)


def test_duplicate_key_rows_are_summed_not_dropped() -> None:
    # One Marketplace SKU, one period, two rows: a populated row and a dormant
    # zero/None row (the real two-rows-per-SKU wrinkle). Must collapse to ONE row
    # whose numeric values equal the sum, with None treated as 0.
    pm = _pm(
        [
            [APR, "SKU-A", 111.0, None, None, 0.0, 0],      # dormant: None profit, 0 gross/units
            [APR, "SKU-A", 222.0, "Widget A", 2.56, 10.99, 1],  # active
        ]
    )
    out = normalize_profit_margin(pm)

    assert len(out) == 1
    row = out.iloc[0]
    assert row[KEY_COLUMN] == "SKU-A"
    assert row[PROFIT_COLUMN] == pytest.approx(2.56)   # None + 2.56 = 2.56
    assert row[GROSS_COLUMN] == pytest.approx(10.99)
    assert row[UNITS_COLUMN] == 1
    # "first" carries one valid identifier — the populated row's name survives is
    # not guaranteed by order, but a value is present and it's one of the two ids.
    assert row["TikTok SKU ID"] in (111.0, 222.0)


def test_blank_numeric_cells_coerced_to_zero_no_nan() -> None:
    pm = _pm(
        [
            [APR, "SKU-A", 1.0, "A", None, None, None],
            [APR, "SKU-B", 2.0, "B", 5.0, 50.0, 5],
        ]
    )
    out = normalize_profit_margin(pm)
    # No NaN anywhere in the additive numeric columns.
    for col in (PROFIT_COLUMN, GROSS_COLUMN, UNITS_COLUMN):
        assert out[col].isna().sum() == 0
    sku_a = out[out[KEY_COLUMN] == "SKU-A"].iloc[0]
    assert sku_a[PROFIT_COLUMN] == 0.0
    assert sku_a[GROSS_COLUMN] == 0.0
    assert sku_a[UNITS_COLUMN] == 0


def test_date_range_normalized_to_period() -> None:
    pm = _pm([[APR, "SKU-A", 1.0, "A", 2.0, 10.0, 1]])
    out = normalize_profit_margin(pm)
    period = out.iloc[0]["period"]
    assert period == Period(2026, 4)
    assert str(period) == "2026-04"


def test_active_filter_is_non_destructive() -> None:
    # Two SKUs: one with units, one with zero units. Base table keeps both;
    # active_skus() returns only the units>0 one.
    pm = _pm(
        [
            [APR, "SKU-ACTIVE", 1.0, "A", 2.0, 10.0, 3],
            [APR, "SKU-ZERO", 2.0, "Z", 0.0, 0.0, 0],
        ]
    )
    out = normalize_profit_margin(pm)
    assert len(out) == 2  # base table retains the zero-unit SKU
    assert set(out[KEY_COLUMN]) == {"SKU-ACTIVE", "SKU-ZERO"}

    active = active_skus(out)
    assert list(active[KEY_COLUMN]) == ["SKU-ACTIVE"]
    # Filter did not mutate the base table.
    assert len(out) == 2


def test_end_to_end_two_period_sums() -> None:
    # A handful of SKUs across two periods, with one duplicate-key collapse in
    # April. Hand-computed per-period sums must match.
    pm = _pm(
        [
            # April
            [APR, "SKU-A", 1.0, "A", 2.00, 10.00, 1],
            [APR, "SKU-A", 1.0, "A", 3.00, 20.00, 2],   # dup → sums with the above
            [APR, "SKU-B", 2.0, "B", 5.00, 50.00, 5],
            [APR, "SKU-C", 3.0, "C", 0.00, 0.00, 0],    # zero-activity, still retained
            # March
            [MAR, "SKU-A", 1.0, "A", 4.00, 40.00, 4],
            [MAR, "SKU-B", 2.0, "B", 6.00, 60.00, 6],
        ]
    )
    out = normalize_profit_margin(pm)

    # April: SKU-A collapses (profit 5.00, gross 30.00, units 3), B, C → 3 rows.
    apr = out[out["period"] == Period(2026, 4)]
    assert len(apr) == 3
    assert apr[PROFIT_COLUMN].sum() == pytest.approx(10.00)   # 5 + 5 + 0
    assert apr[GROSS_COLUMN].sum() == pytest.approx(80.00)    # 30 + 50 + 0
    assert apr[UNITS_COLUMN].sum() == 8                       # 3 + 5 + 0
    sku_a_apr = apr[apr[KEY_COLUMN] == "SKU-A"].iloc[0]
    assert sku_a_apr[GROSS_COLUMN] == pytest.approx(30.00)
    assert sku_a_apr[UNITS_COLUMN] == 3

    # March: two rows, simple sums.
    mar = out[out["period"] == Period(2026, 3)]
    assert len(mar) == 2
    assert mar[PROFIT_COLUMN].sum() == pytest.approx(10.00)
    assert mar[GROSS_COLUMN].sum() == pytest.approx(100.00)
    assert mar[UNITS_COLUMN].sum() == 10

    # Active view per period (units > 0): April A,B (C excluded); March A,B.
    assert len(active_skus(apr)) == 2
    assert len(active_skus(mar)) == 2


def test_missing_required_column_raises() -> None:
    bad = pd.DataFrame({PERIOD_RAW_COLUMN: [APR], KEY_COLUMN: ["SKU-A"]})  # no money cols
    with pytest.raises(ValueError, match="missing required column"):
        normalize_profit_margin(bad)


def test_unparseable_date_range_raises() -> None:
    pm = _pm([["not a date range", "SKU-A", 1.0, "A", 2.0, 10.0, 1]])
    with pytest.raises(ValueError, match="Unparseable"):
        normalize_profit_margin(pm)


# ─────────────────────────────────────────────────────────────────────────────
# Summary grid reshape
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_reshaped_to_period_keyed_line_items() -> None:
    # Raw grid: row 0 header with two period columns, then line-item rows.
    raw = pd.DataFrame(
        [
            ["Summaries", MAR, APR, "Note"],
            ["Total Gross Sale", 66710.72, 32033.09, None],
            ["Total Refund", -1840.06, -704.91, None],
            ["Total Profit", 12490.01, 7595.09, None],
            [None, None, None, None],  # blank row ignored
        ]
    )
    summary = normalize_summary(raw)

    assert set(summary.keys()) == {Period(2026, 3), Period(2026, 4)}
    apr = summary[Period(2026, 4)]
    assert apr["Total Gross Sale"] == pytest.approx(32033.09)
    assert apr["Total Profit"] == pytest.approx(7595.09)
    # Sign preserved (cost stays negative).
    assert apr["Total Refund"] == pytest.approx(-704.91)
    # The earlier period is independently captured.
    assert summary[Period(2026, 3)]["Total Gross Sale"] == pytest.approx(66710.72)


def test_summary_without_period_header_raises() -> None:
    raw = pd.DataFrame([["Summaries", "nope", "still nope"], ["Total Gross Sale", 1, 2]])
    with pytest.raises(ValueError, match="period header"):
        normalize_summary(raw)


def test_summary_note_column_captured_under_note_keys() -> None:
    # Header carries a "Note" column; rows with note text get a "{line}__note" key.
    raw = pd.DataFrame(
        [
            ["Summaries", MAR, APR, "Note"],
            ["Total AD Cost", -2000.0, -2210.70, "Delayed partially"],
            ["Total Referral Fee", -500.0, -291.28, "$4,854.60 unsettled"],
            ["Total Profit", 12490.01, 7595.09, None],   # blank note → no __note key
        ]
    )
    summary = normalize_summary(raw)
    apr = summary[Period(2026, 4)]

    # Notes captured for rows that have text, numeric values left untouched.
    assert apr["Total AD Cost__note"] == "Delayed partially"
    assert apr["Total Referral Fee__note"] == "$4,854.60 unsettled"
    assert apr["Total AD Cost"] == pytest.approx(-2210.70)
    # A blank/NaN note adds no __note key (no None value sneaks in).
    assert "Total Profit__note" not in apr
    # The single Note column applies to every period column.
    assert summary[Period(2026, 3)]["Total AD Cost__note"] == "Delayed partially"


def test_summary_without_note_column_has_no_note_keys() -> None:
    # No "Note" header → existing behavior is unchanged, no __note keys, no error.
    raw = pd.DataFrame(
        [
            ["Summaries", MAR, APR],
            ["Total Gross Sale", 66710.72, 32033.09],
            ["Total Profit", 12490.01, 7595.09],
        ]
    )
    summary = normalize_summary(raw)
    apr = summary[Period(2026, 4)]
    assert apr["Total Gross Sale"] == pytest.approx(32033.09)
    assert not any(k.endswith("__note") for k in apr)
