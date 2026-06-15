"""Tests for src/analysis/comparisons.py.

Synthetic hand-built PeriodData — no real company files. Every numeric target
here is computed by hand in the test so the assertions pin the *method*, not a
golden output. Covers the store's contract for this layer:

  * the anchor assertion (the current period must match across both files),
  * the revenue bridge buckets summing exactly to the gross change,
  * the cost bridge disclosing — not zeroing — an injected imbalance,
  * dual MoM/YoY deltas with new / lapsed / continuing status,
  * MoM-only / YoY-only runs (absent lens is NA, no structural movers, no crash),
  * structural movers: direction-disagreement clears the gate and ranks first.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.comparisons import (
    COST_BRIDGE_LINES,
    PeriodData,
    compare,
    cost_bridge,
    revenue_bridge,
)
from src.ingest.period_parser import Period

APR_2026 = Period(2026, 4)
MAR_2026 = Period(2026, 3)
APR_2025 = Period(2025, 4)


def _sku(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """Build an active-SKU frame from (sku, units, gross, profit) tuples."""
    return pd.DataFrame(rows, columns=["marketplace_sku", "units", "gross", "profit"])


def _period(period: Period, rows, summary: dict | None = None) -> PeriodData:
    return PeriodData(period=period, sku=_sku(rows), summary=summary or {})


# ─────────────────────────────────────────────────────────────────────────────
# Anchor
# ─────────────────────────────────────────────────────────────────────────────
def test_anchor_mismatch_raises() -> None:
    # Canonical current period.
    current = _period(APR_2026, [("FG-A", 10, 1000.0, 200.0)])
    # The MoM file disagrees about the same month's gross by more than epsilon.
    mom_view = _period(APR_2026, [("FG-A", 10, 1000.50, 200.0)])
    mom_baseline = _period(MAR_2026, [("FG-A", 8, 800.0, 150.0)])

    with pytest.raises(ValueError, match="Anchor mismatch"):
        compare(current, mom_baseline=mom_baseline, current_mom_view=mom_view)


def test_anchor_within_epsilon_passes() -> None:
    # A sub-cent difference is float noise, not a real mismatch.
    current = _period(APR_2026, [("FG-A", 10, 1000.0, 200.0)])
    mom_view = _period(APR_2026, [("FG-A", 10, 1000.005, 200.0)])
    mom_baseline = _period(MAR_2026, [("FG-A", 8, 800.0, 150.0)])

    result = compare(current, mom_baseline=mom_baseline, current_mom_view=mom_view)
    assert result.current_period == APR_2026


# ─────────────────────────────────────────────────────────────────────────────
# Revenue bridge
# ─────────────────────────────────────────────────────────────────────────────
def test_revenue_bridge_buckets_and_residual() -> None:
    # Hand-built case with one continuing, one new, one lapsed SKU.
    #   CONT: u1=10 g1=100 (p1=10) → u2=15 g2=180 (p2=12)
    #         volume = (15-10)*10 =  50
    #         price  = (12-10)*15 =  30
    #   NEW (current only): g2 = 40  → +40
    #   LAP (baseline only): g1 = 25 → -25
    #   total gross change = (180+40) - (100+25) = 95
    #   50 + 30 + 40 - 25 = 95  → residual 0
    baseline = _period(MAR_2026, [("CONT", 10, 100.0, 5.0), ("LAP", 4, 25.0, 2.0)])
    current = _period(APR_2026, [("CONT", 15, 180.0, 9.0), ("NEW", 3, 40.0, 4.0)])

    bridge = revenue_bridge(baseline, current)
    assert bridge.volume_effect == pytest.approx(50.0)
    assert bridge.price_effect == pytest.approx(30.0)
    assert bridge.new_skus_effect == pytest.approx(40.0)
    assert bridge.lapsed_skus_effect == pytest.approx(-25.0)
    assert bridge.total_change == pytest.approx(95.0)
    assert bridge.residual == pytest.approx(0.0, abs=1e-9)
    assert bridge.reconciles is True
    # The four buckets reconstruct the total exactly.
    reconstructed = (
        bridge.volume_effect
        + bridge.price_effect
        + bridge.new_skus_effect
        + bridge.lapsed_skus_effect
    )
    assert reconstructed == pytest.approx(bridge.total_change)


# ─────────────────────────────────────────────────────────────────────────────
# Cost bridge
# ─────────────────────────────────────────────────────────────────────────────
def test_cost_bridge_balanced_within_epsilon() -> None:
    # Build a baseline and current where the money lines move by known amounts and
    # the profit delta equals the sum of those moves exactly.
    keys = list(COST_BRIDGE_LINES.values())
    base_lines = {k: 0.0 for k in keys}
    cur_lines = dict(base_lines)
    cur_lines["Total Gross Sale"] = 500.0       # +500
    cur_lines["Total Cost of Goods Sold"] = -120.0  # -120 (cost, negative)
    cur_lines["Total AD Cost"] = -30.0          # -30
    # Sum of deltas = 500 - 120 - 30 = 350 → profit moves by exactly 350.
    baseline = PeriodData(MAR_2026, _sku([("FG-A", 1, 1.0, 1.0)]),
                          {"Total Profit": 0.0, **base_lines})
    current = PeriodData(APR_2026, _sku([("FG-A", 1, 1.0, 1.0)]),
                         {"Total Profit": 350.0, **cur_lines})

    bridge = cost_bridge(baseline, current)
    assert bridge.profit_change == pytest.approx(350.0)
    assert bridge.sum_of_line_deltas == pytest.approx(350.0)
    assert bridge.residual == pytest.approx(0.0, abs=1e-9)
    assert bridge.reconciles is True


def test_cost_bridge_injected_imbalance_is_disclosed_not_zeroed() -> None:
    # Same line moves sum to 350, but Total Profit moved by 650 — a 300 imbalance
    # (mirrors the real ~$299.70 YoY data-quality artifact). The residual must be
    # reported as-is and reconciles=False; nothing nudged to zero.
    keys = list(COST_BRIDGE_LINES.values())
    base_lines = {k: 0.0 for k in keys}
    cur_lines = dict(base_lines)
    cur_lines["Total Gross Sale"] = 500.0
    cur_lines["Total Cost of Goods Sold"] = -120.0
    cur_lines["Total AD Cost"] = -30.0
    baseline = PeriodData(MAR_2026, _sku([("FG-A", 1, 1.0, 1.0)]),
                          {"Total Profit": 0.0, **base_lines})
    current = PeriodData(APR_2026, _sku([("FG-A", 1, 1.0, 1.0)]),
                         {"Total Profit": 650.0, **cur_lines})

    bridge = cost_bridge(baseline, current)
    assert bridge.profit_change == pytest.approx(650.0)
    assert bridge.sum_of_line_deltas == pytest.approx(350.0)
    assert bridge.residual == pytest.approx(300.0)  # reported, not zeroed
    assert bridge.reconciles is False


def test_cost_bridge_threshold_is_configurable() -> None:
    keys = list(COST_BRIDGE_LINES.values())
    base_lines = {k: 0.0 for k in keys}
    cur_lines = dict(base_lines)
    cur_lines["Total Gross Sale"] = 100.0
    baseline = PeriodData(MAR_2026, _sku([("FG-A", 1, 1.0, 1.0)]),
                          {"Total Profit": 0.0, **base_lines})
    current = PeriodData(APR_2026, _sku([("FG-A", 1, 1.0, 1.0)]),
                         {"Total Profit": 100.5, **cur_lines})  # residual 0.50

    # Default $1.00 threshold → reconciles. A tight $0.10 threshold → does not.
    assert cost_bridge(baseline, current).reconciles is True
    assert cost_bridge(baseline, current, threshold=0.10).reconciles is False


# ─────────────────────────────────────────────────────────────────────────────
# Dual deltas
# ─────────────────────────────────────────────────────────────────────────────
def test_dual_deltas_continuing_new_lapsed() -> None:
    # CONT is in all three; NEWMOM only in current (new vs both baselines);
    # LAPMOM only in the MoM baseline (lapsed on the MoM lens).
    current = _period(APR_2026, [("CONT", 10, 1000.0, 200.0), ("NEWMOM", 5, 500.0, 100.0)])
    mom = _period(MAR_2026, [("CONT", 8, 800.0, 150.0), ("LAPMOM", 4, 400.0, 80.0)])
    yoy = _period(APR_2025, [("CONT", 6, 600.0, 120.0)])

    result = compare(current, mom_baseline=mom, yoy_baseline=yoy)
    deltas = result.sku_deltas.set_index("marketplace_sku")

    # CONT: continuing on both lenses, deltas current - baseline.
    assert deltas.loc["CONT", "profit_delta_mom"] == pytest.approx(50.0)   # 200-150
    assert deltas.loc["CONT", "profit_delta_yoy"] == pytest.approx(80.0)   # 200-120
    assert deltas.loc["CONT", "units_delta_mom"] == pytest.approx(2.0)
    assert deltas.loc["CONT", "status_mom"] == "continuing"
    assert deltas.loc["CONT", "status_yoy"] == "continuing"

    # NEWMOM: current only → new on both lenses, delta = full current value.
    assert deltas.loc["NEWMOM", "profit_delta_mom"] == pytest.approx(100.0)
    assert deltas.loc["NEWMOM", "status_mom"] == "new"
    assert deltas.loc["NEWMOM", "status_yoy"] == "new"

    # LAPMOM: in MoM baseline only → lapsed on MoM, absent on YoY.
    assert deltas.loc["LAPMOM", "profit_delta_mom"] == pytest.approx(-80.0)
    assert deltas.loc["LAPMOM", "status_mom"] == "lapsed"
    assert deltas.loc["LAPMOM", "status_yoy"] == "absent"


# ─────────────────────────────────────────────────────────────────────────────
# Single-lens runs
# ─────────────────────────────────────────────────────────────────────────────
def test_mom_only_run_yoy_lens_absent_and_no_movers() -> None:
    current = _period(APR_2026, [("FG-A", 10, 1000.0, 200.0)])
    mom = _period(MAR_2026, [("FG-A", 8, 800.0, 150.0)])

    result = compare(current, mom_baseline=mom)
    assert result.revenue_bridge_mom is not None
    assert result.revenue_bridge_yoy is None
    assert result.cost_bridge_yoy is None

    deltas = result.sku_deltas.set_index("marketplace_sku")
    assert deltas.loc["FG-A", "profit_delta_mom"] == pytest.approx(50.0)
    # YoY lens absent → NA delta, None status.
    assert pd.isna(deltas.loc["FG-A", "profit_delta_yoy"])
    assert deltas.loc["FG-A", "status_yoy"] is None

    # Structural movers needs both lenses → empty, no crash.
    assert result.structural_movers.empty


def test_yoy_only_run_mom_lens_absent_and_no_movers() -> None:
    current = _period(APR_2026, [("FG-A", 10, 1000.0, 200.0)])
    yoy = _period(APR_2025, [("FG-A", 6, 600.0, 120.0)])

    result = compare(current, yoy_baseline=yoy)
    assert result.revenue_bridge_yoy is not None
    assert result.revenue_bridge_mom is None
    assert result.cost_bridge_mom is None

    deltas = result.sku_deltas.set_index("marketplace_sku")
    assert pd.isna(deltas.loc["FG-A", "profit_delta_mom"])
    assert deltas.loc["FG-A", "status_mom"] is None
    assert result.structural_movers.empty


def test_compare_requires_at_least_one_baseline() -> None:
    current = _period(APR_2026, [("FG-A", 10, 1000.0, 200.0)])
    with pytest.raises(ValueError, match="at least one baseline"):
        compare(current)


# ─────────────────────────────────────────────────────────────────────────────
# Structural movers
# ─────────────────────────────────────────────────────────────────────────────
def test_structural_movers_disagreement_ranks_above_agreement() -> None:
    # DIVERGE: down on MoM (-500), up on YoY (+400) → directions disagree, clears
    #   the $100 gate, divergence = |400 - (-500)| = 900.
    # AGREE: down on both lenses (-300 MoM, -200 YoY) → same direction, excluded
    #   from movers even though it's material.
    # SMALL: disagrees in direction but both moves are under $100 → excluded.
    current = _period(
        APR_2026,
        [("DIVERGE", 10, 1000.0, 100.0), ("AGREE", 10, 1000.0, 100.0), ("SMALL", 10, 1000.0, 100.0)],
    )
    # MoM baseline: DIVERGE profit 600 (cur 100 → -500); AGREE 400 (→ -300); SMALL 150 (→ -50).
    mom = _period(
        MAR_2026,
        [("DIVERGE", 9, 900.0, 600.0), ("AGREE", 9, 900.0, 400.0), ("SMALL", 9, 900.0, 150.0)],
    )
    # YoY baseline: DIVERGE -300 (cur 100 → +400); AGREE 300 (→ -200); SMALL 50 (→ +50).
    yoy = _period(
        APR_2025,
        [("DIVERGE", 5, 500.0, -300.0), ("AGREE", 5, 500.0, 300.0), ("SMALL", 5, 500.0, 50.0)],
    )

    result = compare(current, mom_baseline=mom, yoy_baseline=yoy)
    movers = result.structural_movers

    # Only DIVERGE qualifies (disagrees in direction AND material).
    assert list(movers["marketplace_sku"]) == ["DIVERGE"]
    assert movers.iloc[0]["divergence"] == pytest.approx(900.0)
    assert movers.iloc[0]["profit_delta_mom"] == pytest.approx(-500.0)
    assert movers.iloc[0]["profit_delta_yoy"] == pytest.approx(400.0)


def test_structural_movers_ordered_by_divergence_desc() -> None:
    # Two divergent SKUs; the bigger-divergence one ranks first.
    current = _period(APR_2026, [("BIG", 10, 1000.0, 100.0), ("LITTLE", 10, 1000.0, 100.0)])
    # BIG: MoM -800, YoY +700 → divergence 1500.
    # LITTLE: MoM -200, YoY +150 → divergence 350.
    mom = _period(MAR_2026, [("BIG", 9, 900.0, 900.0), ("LITTLE", 9, 900.0, 300.0)])
    yoy = _period(APR_2025, [("BIG", 5, 500.0, -600.0), ("LITTLE", 5, 500.0, -50.0)])

    movers = compare(current, mom_baseline=mom, yoy_baseline=yoy).structural_movers
    assert list(movers["marketplace_sku"]) == ["BIG", "LITTLE"]
    assert list(movers["divergence"]) == pytest.approx([1500.0, 350.0])
