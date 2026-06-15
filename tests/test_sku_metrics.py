"""Tests for src/transform/sku_metrics.py.

Synthetic in-memory active-set frames — no real company data. Covers the derived
formulas (exact values), the break-even ROAS edge cases (undefined → NaN, never a
number), rank tie-handling (method="min"), and the segment classifier (each rule
fires, every SKU gets exactly one label, counts sum to the active total).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.ingest.period_parser import Period
from src.transform.sku_metrics import (
    AD_COST_COLUMN,
    SEGMENT_LABELS,
    compute_period_metrics,
)

APR = Period(2026, 4)

# Active-set columns the metrics layer reads (raw signs: ad cost negative).
_COLS = [
    "period",
    "Marketplace SKU",
    "Product Name",
    "Theme",
    "Category",
    "Total Gross Sale",
    "Total Profit",
    "Total Sold Units",
    AD_COST_COLUMN,
]


def _active(rows: list[dict]) -> pd.DataFrame:
    """Build an active-set frame; each row dict overrides sensible defaults."""
    base = {
        "period": APR,
        "Product Name": "p",
        "Theme": "t",
        "Category": "c",
        "Total Gross Sale": 100.0,
        "Total Profit": 20.0,
        "Total Sold Units": 5,
        AD_COST_COLUMN: -10.0,
    }
    return pd.DataFrame([{**base, **r} for r in rows], columns=_COLS)


def test_core_formulas_exact() -> None:
    # gross 200, profit 40, ad cost -50 → margin 20%, ad_spend 50,
    # profit_before_ads 90, pread_cm 0.45, breakeven 1/0.45 = 2.2222...
    df = _active(
        [{"Marketplace SKU": "A", "Total Gross Sale": 200.0, "Total Profit": 40.0,
          AD_COST_COLUMN: -50.0}]
    )
    res = compute_period_metrics(df, APR)
    row = res.metrics.iloc[0]
    assert row["profit_margin_pct"] == pytest.approx(20.0)
    assert row["ad_spend"] == pytest.approx(50.0)
    assert row["profit_before_ads"] == pytest.approx(90.0)
    assert row["pread_contribution_margin"] == pytest.approx(0.45)
    assert row["breakeven_roas"] == pytest.approx(1 / 0.45)


def test_zero_ad_spend_breakeven_is_nan() -> None:
    df = _active(
        [{"Marketplace SKU": "A", "Total Gross Sale": 100.0, "Total Profit": 30.0,
          AD_COST_COLUMN: 0.0}]
    )
    row = compute_period_metrics(df, APR).metrics.iloc[0]
    assert pd.isna(row["breakeven_roas"])          # N/A, not a number
    assert row["profit_before_ads"] == pytest.approx(30.0)  # == profit
    assert row["ad_spend"] == 0.0                   # not -0.0


def test_negative_preadd_margin_breakeven_is_nan() -> None:
    # Loses money even before ads: profit_before_ads < 0 → no meaningful ROAS.
    df = _active(
        [{"Marketplace SKU": "A", "Total Gross Sale": 100.0, "Total Profit": -80.0,
          AD_COST_COLUMN: -10.0}]  # profit_before_ads = -70 → pread_cm < 0
    )
    row = compute_period_metrics(df, APR).metrics.iloc[0]
    assert row["profit_before_ads"] == pytest.approx(-70.0)
    assert pd.isna(row["breakeven_roas"])


def test_ranks_top_is_one_and_ties_use_min() -> None:
    df = _active(
        [
            {"Marketplace SKU": "TOP", "Total Gross Sale": 300.0, "Total Profit": 90.0},
            {"Marketplace SKU": "TIE1", "Total Gross Sale": 100.0, "Total Profit": 30.0},
            {"Marketplace SKU": "TIE2", "Total Gross Sale": 100.0, "Total Profit": 30.0},
        ]
    )
    m = compute_period_metrics(df, APR).metrics.set_index("Marketplace SKU")
    assert m.loc["TOP", "rank_by_gross"] == 1
    # Two SKUs tie at gross 100 → both rank 2 (min), none ranked 3.
    assert m.loc["TIE1", "rank_by_gross"] == 2
    assert m.loc["TIE2", "rank_by_gross"] == 2


def test_segmentation_each_rule_fires_and_partitions() -> None:
    # Build a distribution where every rule has a clear firing case. With these
    # 8 SKUs the gross/margin percentiles place each target as intended.
    # Hand-tuned so the linear-interpolation thresholds over these 8 SKUs are
    # gross p25/p50/p75 = 155/215/600 and margin% p25/p50/p75 = -3.25/15/21.25,
    # which makes each rule's target the unambiguous match.
    rows = [
        # gross 1000 (≥p75), margin 40% (≥p50), profit>0 → Scale
        {"Marketplace SKU": "SCALE", "Total Gross Sale": 1000.0, "Total Profit": 400.0, AD_COST_COLUMN: -10.0},
        # profit_before_ads = -5+50 = 45 > 0 but profit ≤ 0 → PauseAds (wins on precedence)
        {"Marketplace SKU": "PAUSE", "Total Gross Sale": 500.0, "Total Profit": -5.0, AD_COST_COLUMN: -50.0},
        # margin 90% (≥p75), gross 20 (<p50) → TestMore
        {"Marketplace SKU": "TEST", "Total Gross Sale": 20.0, "Total Profit": 18.0, AD_COST_COLUMN: -1.0},
        # gross 900 (≥p75), margin -10% (<p25) → Fix
        {"Marketplace SKU": "FIX", "Total Gross Sale": 900.0, "Total Profit": -90.0, AD_COST_COLUMN: -1.0},
        # gross 10 (≤p25), profit ≤ 0 → Deprioritize
        {"Marketplace SKU": "DEPRI", "Total Gross Sale": 10.0, "Total Profit": -2.0, AD_COST_COLUMN: -1.0},
        # mid gross, margin 15% (=p50, <p75) → Steady
        {"Marketplace SKU": "STEADY1", "Total Gross Sale": 200.0, "Total Profit": 30.0, AD_COST_COLUMN: -5.0},
        {"Marketplace SKU": "STEADY2", "Total Gross Sale": 210.0, "Total Profit": 31.5, AD_COST_COLUMN: -5.0},
        {"Marketplace SKU": "STEADY3", "Total Gross Sale": 220.0, "Total Profit": 33.0, AD_COST_COLUMN: -5.0},
    ]
    m = compute_period_metrics(_active(rows), APR).metrics.set_index("Marketplace SKU")

    assert m.loc["SCALE", "segment"] == "Scale"
    assert m.loc["PAUSE", "segment"] == "PauseAds"
    assert m.loc["TEST", "segment"] == "TestMore"
    assert m.loc["FIX", "segment"] == "Fix"
    assert m.loc["DEPRI", "segment"] == "Deprioritize"
    assert m.loc["STEADY1", "segment"] == "Steady"

    # Exactly one label each, all from the known set, counts sum to the active total.
    assert m["segment"].isin(SEGMENT_LABELS).all()
    assert m["segment"].notna().all()
    assert m["segment"].value_counts().sum() == len(m)


def test_pauseads_precedence_over_scale() -> None:
    # A SKU that satisfies Scale's gross/margin but has profit <= 0 and
    # profit_before_ads > 0 must be PauseAds (rule 1 wins over rule 2).
    rows = [
        {"Marketplace SKU": "X", "Total Gross Sale": 1000.0, "Total Profit": -1.0, AD_COST_COLUMN: -200.0},
        {"Marketplace SKU": "L", "Total Gross Sale": 100.0, "Total Profit": 10.0, AD_COST_COLUMN: -1.0},
    ]
    m = compute_period_metrics(_active(rows), APR).metrics.set_index("Marketplace SKU")
    assert m.loc["X", "segment"] == "PauseAds"


def test_thresholds_returned() -> None:
    df = _active(
        [
            {"Marketplace SKU": "A", "Total Gross Sale": 100.0, "Total Profit": 10.0},
            {"Marketplace SKU": "B", "Total Gross Sale": 200.0, "Total Profit": 40.0},
            {"Marketplace SKU": "C", "Total Gross Sale": 300.0, "Total Profit": 90.0},
        ]
    )
    t = compute_period_metrics(df, APR).thresholds
    # Linear-interpolation median of [100,200,300] gross is 200.
    assert t.gross_p50 == pytest.approx(200.0)
    assert t.gross_p25 == pytest.approx(150.0)
    assert t.gross_p75 == pytest.approx(250.0)
