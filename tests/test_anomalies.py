"""Tests for src/analysis/anomalies.py.

Synthetic data only — no real company files. Each test builds the minimal
upstream structures (a SkuMetricsResult and a ComparisonResult) by hand so the
assertions pin a *rule and its threshold*, not a golden run. The gate is driven
to a known value via ``current_period_profit`` so "clears / below the gate" is
unambiguous.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.anomalies import (
    AD_SHARE_THRESHOLD,
    Category,
    Direction,
    Severity,
    detect_anomalies,
    materiality_gate,
)
from src.analysis.comparisons import ComparisonResult, RevenueBridge, CostBridge
from src.ingest.period_parser import Period
from src.transform.sku_metrics import SegmentThresholds, SkuMetricsResult

APR_2026 = Period(2026, 4)
MAR_2026 = Period(2026, 3)
APR_2025 = Period(2025, 4)

# A profit that makes the gate exactly $100 (1% = $1 < $100 floor).
GATE_100_PROFIT = 7595.09


# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────
def _metrics(rows: list[dict], thresholds: SegmentThresholds | None = None) -> SkuMetricsResult:
    base = {
        "Marketplace SKU": "SKU", "Theme": "T", "gross": 100.0, "profit": 20.0,
        "units": 50, "ad_spend": 5.0, "profit_margin_pct": 20.0,
        "profit_before_ads": 25.0, "pread_contribution_margin": 0.25,
        "breakeven_roas": 4.0, "segment": "Steady",
    }
    df = pd.DataFrame([{**base, **r} for r in rows])
    df.insert(0, "period", APR_2026)
    if thresholds is None:
        thresholds = SegmentThresholds(100.0, 300.0, 1000.0, 5.0, 15.0, 30.0)
    return SkuMetricsResult(period=APR_2026, metrics=df, thresholds=thresholds)


def _deltas(rows: list[dict]) -> pd.DataFrame:
    cols = ["marketplace_sku", "current_units", "current_gross", "current_profit",
            "profit_delta_mom", "gross_delta_mom", "units_delta_mom", "status_mom",
            "profit_delta_yoy", "gross_delta_yoy", "units_delta_yoy", "status_yoy"]
    base = {c: pd.NA for c in cols}
    base.update({"current_units": 50, "current_gross": 100.0, "current_profit": 20.0})
    return pd.DataFrame([{**base, **r} for r in rows], columns=cols)


def _movers(rows: list[dict]) -> pd.DataFrame:
    cols = ["marketplace_sku", "profit_delta_mom", "profit_delta_yoy", "divergence"]
    return pd.DataFrame(rows, columns=cols)


def _comparison(
    deltas: pd.DataFrame,
    *,
    movers: pd.DataFrame | None = None,
    has_mom: bool = True,
    has_yoy: bool = True,
    cost_mom: CostBridge | None = None,
    cost_yoy: CostBridge | None = None,
    rev_mom: RevenueBridge | None = None,
    rev_yoy: RevenueBridge | None = None,
) -> ComparisonResult:
    def _rev(baseline):
        return RevenueBridge(baseline, APR_2026, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True)

    if has_mom and rev_mom is None:
        rev_mom = _rev(MAR_2026)
    if has_yoy and rev_yoy is None:
        rev_yoy = _rev(APR_2025)
    return ComparisonResult(
        current_period=APR_2026,
        sku_deltas=deltas,
        revenue_bridge_mom=rev_mom if has_mom else None,
        revenue_bridge_yoy=rev_yoy if has_yoy else None,
        cost_bridge_mom=cost_mom if has_mom else None,
        cost_bridge_yoy=cost_yoy if has_yoy else None,
        structural_movers=movers if movers is not None else _movers([]),
    )


def _flags(report, rule_id: str, scope: str | None = None):
    return [f for f in report.flags
            if f.rule_id == rule_id and (scope is None or f.scope == scope)]


# ─────────────────────────────────────────────────────────────────────────────
# Gate
# ─────────────────────────────────────────────────────────────────────────────
def test_gate_is_computed_not_hardcoded() -> None:
    assert materiality_gate(7595.09) == pytest.approx(100.0)   # floor wins
    assert materiality_gate(50000.0) == pytest.approx(500.0)   # 1% wins
    assert materiality_gate(0.0) == pytest.approx(100.0)


# ─────────────────────────────────────────────────────────────────────────────
# Rule A — confirmed decline + gate behaviour
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_a_both_lenses_down_clears_gate_high() -> None:
    deltas = _deltas([{"marketplace_sku": "FG-A", "profit_delta_mom": -561.75,
                       "gross_delta_mom": -600.0, "profit_delta_yoy": -807.18,
                       "gross_delta_yoy": -900.0}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-A"}]),
                              _comparison(deltas), GATE_100_PROFIT)
    a = _flags(report, "A", "FG-A")
    assert len(a) == 1
    assert a[0].severity is Severity.HIGH
    assert a[0].direction is Direction.DOWN
    assert a[0].category is Category.TREND


def test_rule_a_below_gate_not_flagged() -> None:
    # Same shape, but both moves are under the $100 gate → no A flag.
    deltas = _deltas([{"marketplace_sku": "FG-A", "profit_delta_mom": -40.0,
                       "gross_delta_mom": -50.0, "profit_delta_yoy": -60.0,
                       "gross_delta_yoy": -70.0}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-A"}]),
                              _comparison(deltas), GATE_100_PROFIT)
    assert _flags(report, "A") == []


# ─────────────────────────────────────────────────────────────────────────────
# Rule B — divergence (reuses structural movers) and Rule C — quiet YoY bleeder
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_b_divergence_from_structural_movers() -> None:
    deltas = _deltas([{"marketplace_sku": "FG-1HT", "profit_delta_mom": -2245.79,
                       "gross_delta_mom": -3000.0, "profit_delta_yoy": 1670.01,
                       "gross_delta_yoy": 2000.0}])
    movers = _movers([{"marketplace_sku": "FG-1HT", "profit_delta_mom": -2245.79,
                       "profit_delta_yoy": 1670.01, "divergence": 3915.80}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-1HT"}]),
                              _comparison(deltas, movers=movers), GATE_100_PROFIT)
    b = _flags(report, "B", "FG-1HT")
    assert len(b) == 1
    assert b[0].direction is Direction.DIVERGING
    assert b[0].severity is Severity.MEDIUM


def test_rule_c_quiet_yoy_bleeder_direction_down() -> None:
    # Flat/positive MoM, materially down YoY (FG-1MX shape).
    deltas = _deltas([{"marketplace_sku": "FG-1MX", "profit_delta_mom": 222.08,
                       "gross_delta_mom": 300.0, "profit_delta_yoy": -1065.95,
                       "gross_delta_yoy": -1200.0}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-1MX"}]),
                              _comparison(deltas), GATE_100_PROFIT)
    c = _flags(report, "C", "FG-1MX")
    assert len(c) == 1
    assert c[0].direction is Direction.DOWN
    # And it is NOT mislabelled as a confirmed (both-down) decline.
    assert _flags(report, "A", "FG-1MX") == []


# ─────────────────────────────────────────────────────────────────────────────
# Rule D — profit down while revenue flat/up
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_d_profit_down_revenue_flat_sku_scope() -> None:
    deltas = _deltas([{"marketplace_sku": "FG-D", "profit_delta_mom": -300.0,
                       "gross_delta_mom": 50.0, "profit_delta_yoy": -250.0,
                       "gross_delta_yoy": 20.0}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-D"}]),
                              _comparison(deltas), GATE_100_PROFIT)
    d = _flags(report, "D", "FG-D")
    assert len(d) >= 1
    assert all(f.category is Category.MARGIN for f in d)


# ─────────────────────────────────────────────────────────────────────────────
# Rule E — ad-efficiency (PauseAds reuse)
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_e_pauseads_profit_before_ads_positive_after_nonpositive() -> None:
    metrics = _metrics([{"Marketplace SKU": "FG-E", "segment": "PauseAds",
                         "profit_before_ads": 120.0, "profit": -30.0, "ad_spend": 150.0}])
    report = detect_anomalies(metrics, _comparison(_deltas([{"marketplace_sku": "FG-E"}])),
                              GATE_100_PROFIT)
    e = _flags(report, "E", "FG-E")
    assert len(e) >= 1
    assert any(f.category is Category.AD and f.severity is Severity.HIGH for f in e)


def test_rule_e_ad_concentration_on_declining_sku() -> None:
    # FG-BIG takes >20% of channel ad spend and is down on both lenses.
    metrics = _metrics([
        {"Marketplace SKU": "FG-BIG", "ad_spend": 500.0},
        {"Marketplace SKU": "FG-SMALL", "ad_spend": 100.0},
    ])
    deltas = _deltas([
        {"marketplace_sku": "FG-BIG", "profit_delta_mom": -400.0, "gross_delta_mom": -500.0,
         "profit_delta_yoy": -300.0, "gross_delta_yoy": -350.0},
        {"marketplace_sku": "FG-SMALL", "profit_delta_mom": 10.0, "gross_delta_mom": 5.0,
         "profit_delta_yoy": 8.0, "gross_delta_yoy": 4.0},
    ])
    report = detect_anomalies(metrics, _comparison(deltas), GATE_100_PROFIT)
    conc = [f for f in _flags(report, "E", "FG-BIG") if "concentration" in f.reason.lower()]
    assert len(conc) == 1
    assert conc[0].evidence["ad_share"] >= AD_SHARE_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Rule F — high gross, low margin (reuse passed-in thresholds)
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_f_high_gross_low_margin_uses_thresholds() -> None:
    thresholds = SegmentThresholds(100.0, 300.0, 1000.0, 10.0, 20.0, 30.0)
    metrics = _metrics([{"Marketplace SKU": "FG-3WTP", "gross": 6160.89, "profit": 1130.0,
                         "profit_margin_pct": 18.34}], thresholds=thresholds)  # 18.34% < p25 10? no
    # 18.34% is ABOVE margin_p25 10.0 → must NOT flag (proves threshold reuse).
    report = detect_anomalies(metrics, _comparison(_deltas([{"marketplace_sku": "FG-3WTP"}])),
                              GATE_100_PROFIT)
    assert _flags(report, "F") == []

    # Now drop margin below p25 → flags.
    thresholds2 = SegmentThresholds(100.0, 300.0, 1000.0, 20.0, 25.0, 30.0)
    metrics2 = _metrics([{"Marketplace SKU": "FG-3WTP", "gross": 6160.89, "profit": 1130.0,
                          "profit_margin_pct": 18.34}], thresholds=thresholds2)  # 18.34 < 20
    report2 = detect_anomalies(metrics2, _comparison(_deltas([{"marketplace_sku": "FG-3WTP"}])),
                               GATE_100_PROFIT)
    f = _flags(report2, "F", "FG-3WTP")
    assert len(f) == 1
    assert f[0].category is Category.MARGIN


# ─────────────────────────────────────────────────────────────────────────────
# Rule G — cost > gross, size-independent (fires below the $100 gate)
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_g_cost_exceeds_gross_below_gate_still_flagged() -> None:
    # MC-2PR shape: $14.99 gross, -$31.50 ocean freight, ~$22 loss (< gate).
    cost_detail = pd.DataFrame([{
        "Marketplace SKU": "MC-2PR", "period": APR_2026, "Total Gross Sale": 14.99,
        "Total Ocean Freight Cost": -31.50, "Total Customs": -1.0,
        "Total Tiktok Shipping cost": -2.0, "Total Order ShippingEasy Cost": 0.0,
        "Total ShippingEasy Supply Cost": 0.0, "Total Returned Shipping Cost": 0.0,
    }])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "MC-2PR", "units": 1}]),
                              _comparison(_deltas([{"marketplace_sku": "MC-2PR"}])),
                              GATE_100_PROFIT, cost_detail=cost_detail)
    g = _flags(report, "G", "MC-2PR")
    assert len(g) == 1
    assert g[0].category is Category.DATA_INTEGRITY
    assert g[0].evidence["component"] == "Total Ocean Freight Cost"
    assert g[0].evidence["ratio_to_gross"] > 2.0


def test_rule_g_skipped_without_cost_detail() -> None:
    report = detect_anomalies(_metrics([{"Marketplace SKU": "MC-2PR"}]),
                              _comparison(_deltas([{"marketplace_sku": "MC-2PR"}])),
                              GATE_100_PROFIT)  # cost_detail=None
    assert _flags(report, "G") == []


# ─────────────────────────────────────────────────────────────────────────────
# Rule H — low-volume caution is attached, not standalone
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_h_attaches_only_when_other_flags_exist() -> None:
    # Low-volume SKU that ALSO has a confirmed decline → caution attached.
    deltas = _deltas([{"marketplace_sku": "FG-TINY", "profit_delta_mom": -200.0,
                       "gross_delta_mom": -250.0, "profit_delta_yoy": -300.0,
                       "gross_delta_yoy": -350.0}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-TINY", "units": 3}]),
                              _comparison(deltas), GATE_100_PROFIT)
    assert _flags(report, "A", "FG-TINY")          # the triggering flag
    h = _flags(report, "H", "FG-TINY")
    assert len(h) == 1
    assert h[0].category is Category.CAUTION

    # Low-volume SKU with NO other flag → no standalone caution noise.
    quiet = _deltas([{"marketplace_sku": "FG-QUIET", "profit_delta_mom": 5.0,
                      "gross_delta_mom": 5.0, "profit_delta_yoy": 5.0, "gross_delta_yoy": 5.0}])
    report2 = detect_anomalies(_metrics([{"Marketplace SKU": "FG-QUIET", "units": 2}]),
                               _comparison(quiet), GATE_100_PROFIT)
    assert _flags(report2, "H") == []


# ─────────────────────────────────────────────────────────────────────────────
# Rule I — history-guarded
# ─────────────────────────────────────────────────────────────────────────────
def test_rule_i_no_history_no_crash_no_fabrication() -> None:
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-A"}]),
                              _comparison(_deltas([{"marketplace_sku": "FG-A"}])),
                              GATE_100_PROFIT, trailing=None)
    assert _flags(report, "I") == []


def test_rule_i_below_trailing_margin_fires() -> None:
    metrics = _metrics([{"Marketplace SKU": "FG-A", "gross": 5000.0, "profit": 250.0,
                         "profit_margin_pct": 5.0, "ad_spend": 600.0}])
    trailing = pd.DataFrame([{"Marketplace SKU": "FG-A", "trailing_margin_pct": 18.0,
                              "trailing_ad_pct": 6.0}])  # margin 5 < 18; ad 12% > 6%
    report = detect_anomalies(metrics, _comparison(_deltas([{"marketplace_sku": "FG-A"}])),
                              GATE_100_PROFIT, trailing=trailing)
    i_flags = _flags(report, "I", "FG-A")
    assert any(f.category is Category.MARGIN and f.direction is Direction.DOWN for f in i_flags)
    assert any(f.category is Category.AD and f.direction is Direction.UP for f in i_flags)


# ─────────────────────────────────────────────────────────────────────────────
# Single-lens inputs — rules needing the absent lens skip cleanly
# ─────────────────────────────────────────────────────────────────────────────
def test_mom_only_skips_both_lens_rules_no_crash() -> None:
    deltas = _deltas([{"marketplace_sku": "FG-A", "profit_delta_mom": -500.0,
                       "gross_delta_mom": -600.0, "status_mom": "continuing"}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-A"}]),
                              _comparison(deltas, has_yoy=False), GATE_100_PROFIT)
    # A and C need both lenses → none. B has no movers → none.
    assert _flags(report, "A") == []
    assert _flags(report, "C") == []
    assert _flags(report, "B") == []
    # D on the MoM lens still works (profit down, gross down → not D; here gross down
    # so no D either). The run simply must not crash and must produce a report.
    assert report.current_period == APR_2026


def test_yoy_only_skips_both_lens_rules_no_crash() -> None:
    deltas = _deltas([{"marketplace_sku": "FG-A", "profit_delta_yoy": -500.0,
                       "gross_delta_yoy": -600.0, "status_yoy": "continuing"}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-A"}]),
                              _comparison(deltas, has_mom=False), GATE_100_PROFIT)
    assert _flags(report, "A") == []
    assert _flags(report, "C") == []
    assert report.current_period == APR_2026


# ─────────────────────────────────────────────────────────────────────────────
# Grouping
# ─────────────────────────────────────────────────────────────────────────────
def test_report_grouping_by_sku_and_category() -> None:
    deltas = _deltas([{"marketplace_sku": "FG-A", "profit_delta_mom": -561.75,
                       "gross_delta_mom": -600.0, "profit_delta_yoy": -807.18,
                       "gross_delta_yoy": -900.0}])
    report = detect_anomalies(_metrics([{"Marketplace SKU": "FG-A", "units": 4}]),
                              _comparison(deltas), GATE_100_PROFIT)
    assert "FG-A" in report.by_sku
    assert Category.TREND in report.by_category
    # by_sku partitions the flat flag list.
    assert sum(len(v) for v in report.by_sku.values()) == len(report.flags)
