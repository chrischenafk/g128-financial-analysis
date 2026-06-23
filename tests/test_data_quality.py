"""Tests for src/analysis/data_quality.py.

Synthetic data only — no real company files. The sheets are hand-built tiny
DataFrames with the verified column names and the file-relative ``Period``
integer (1 = baseline, 2 = current). FilePeriods are constructed so we can prove
the integer maps to the file's *own* periods (April-2025/2026 for the YoY file),
not an absolute "prior month".
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from src.analysis.comparisons import ComparisonResult, CostBridge, RevenueBridge
from src.analysis.data_quality import (
    CODE_AD_COST_MAPPING_GAP,
    CODE_CANCELED_SHIPPING,
    CODE_ORDERS_WITHOUT_PAYOUT,
    CODE_UNMAPPED_ADS,
    CODE_UNMAPPED_PAYOUT,
    CODE_UNSETTLED_REFERRAL_FEE,
    CODE_YOY_BRIDGE_RESIDUAL,
    CODE_YOY_UNALLOCATED_CREDIT,
    KEY_AD_COST_MAPPED,
    KEY_UNALLOCATED_CREDIT,
    KEY_UNSETTLED_REFERRAL_FEE,
    DQSeverity,
    _extract_note_figures,
    build_data_quality_report,
)
from src.ingest.excel_loader import LoadedWorkbook
from src.ingest.period_parser import FilePeriods, Period

APR_2026 = Period(2026, 4)
MAR_2026 = Period(2026, 3)
APR_2025 = Period(2025, 4)

MOM_PERIODS = FilePeriods(Path("mom.xlsm"), current=APR_2026, comparison=MAR_2026, comparison_type="MoM")
YOY_PERIODS = FilePeriods(Path("yoy.xlsm"), current=APR_2026, comparison=APR_2025, comparison_type="YoY")


# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────
def _loaded(
    periods: FilePeriods,
    *,
    unmapped_ads=None,
    canceled_shipping=None,
    unmapped_payout=None,
    orders_without_payout=None,
) -> LoadedWorkbook:
    return LoadedWorkbook(
        path=periods.path,
        summary=pd.DataFrame(),
        profit_margin=pd.DataFrame(),
        unmapped_ads=unmapped_ads,
        canceled_shipping=canceled_shipping,
        unmapped_payout=unmapped_payout,
        orders_without_payout=orders_without_payout,
        periods=periods,
    )


def _comparison(*, cost_yoy: CostBridge | None = None, has_yoy_rev: bool = True) -> ComparisonResult:
    rev_yoy = (RevenueBridge(APR_2025, APR_2026, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True)
               if has_yoy_rev else None)
    return ComparisonResult(
        current_period=APR_2026,
        sku_deltas=pd.DataFrame(),
        revenue_bridge_mom=None,
        revenue_bridge_yoy=rev_yoy,
        cost_bridge_mom=None,
        cost_bridge_yoy=cost_yoy,
        structural_movers=pd.DataFrame(),
    )


def _by_code(report, code):
    return report.by_code.get(code, [])


# ─────────────────────────────────────────────────────────────────────────────
# File-relative period mapping (the critical correctness property)
# ─────────────────────────────────────────────────────────────────────────────
def test_period_integer_is_file_relative_not_absolute() -> None:
    # YoY file: Period 1 → April-2025, Period 2 → April-2026 (NOT prior month).
    ads = pd.DataFrame({"Period": [1, 2], "Cost": [2.40, 0.63]})
    report = build_data_quality_report(
        [_loaded(YOY_PERIODS, unmapped_ads=ads)],
        summary_by_period={}, comparison=_comparison(), current_period=APR_2026,
    )
    ws = {w.period: w for w in _by_code(report, CODE_UNMAPPED_ADS)}
    assert set(ws) == {APR_2025, APR_2026}
    assert ws[APR_2025].amount == pytest.approx(2.40)   # period 1 → baseline
    assert ws[APR_2026].amount == pytest.approx(0.63)   # period 2 → current


# ─────────────────────────────────────────────────────────────────────────────
# Per-period sums & counts
# ─────────────────────────────────────────────────────────────────────────────
def test_unmapped_ads_and_canceled_shipping_sums() -> None:
    ads = pd.DataFrame({"Period": [1, 1, 2], "Cost": [1.40, 1.00, 0.63]})
    ship = pd.DataFrame({"Period": [1, 2, 2], "ShippingEasy Cost": [56.21, 5.00, 5.68]})
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, unmapped_ads=ads, canceled_shipping=ship)],
        summary_by_period={}, comparison=_comparison(has_yoy_rev=False),
        current_period=APR_2026,
    )
    ads_w = {w.period: w.amount for w in _by_code(report, CODE_UNMAPPED_ADS)}
    assert ads_w[MAR_2026] == pytest.approx(2.40)
    assert ads_w[APR_2026] == pytest.approx(0.63)
    ship_w = {w.period: w.amount for w in _by_code(report, CODE_CANCELED_SHIPPING)}
    assert ship_w[MAR_2026] == pytest.approx(56.21)
    assert ship_w[APR_2026] == pytest.approx(10.68)


def test_orders_without_payout_sum_and_count() -> None:
    owp = pd.DataFrame({
        "Period": [1] * 2 + [2] * 3,
        "Total gross sale": [100.0, 28.91, 1000.0, 2000.0, 1854.60],
    })  # P1 = 128.91 / 2 orders, P2 = 4854.60 / 3 orders
    summary = {APR_2026: {"Total Gross Sale": 32033.09}, MAR_2026: {"Total Gross Sale": 60000.0}}
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, orders_without_payout=owp)],
        summary_by_period=summary, comparison=_comparison(has_yoy_rev=False),
        current_period=APR_2026,
    )
    ws = {w.period: w for w in _by_code(report, CODE_ORDERS_WITHOUT_PAYOUT)}
    assert ws[MAR_2026].amount == pytest.approx(128.91)
    assert ws[MAR_2026].count == 2
    assert ws[APR_2026].amount == pytest.approx(4854.60)
    assert ws[APR_2026].count == 3


# ─────────────────────────────────────────────────────────────────────────────
# Materiality escalation for orders-without-payout
# ─────────────────────────────────────────────────────────────────────────────
def test_orders_without_payout_large_share_escalates() -> None:
    # 4,854.60 / 32,033.09 ≈ 15% → CAUTION (margin caveat).
    owp = pd.DataFrame({"Period": [2, 2], "Total gross sale": [4000.0, 854.60]})
    summary = {APR_2026: {"Total Gross Sale": 32033.09}}
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, orders_without_payout=owp)],
        summary_by_period=summary, comparison=_comparison(has_yoy_rev=False),
        current_period=APR_2026,
    )
    w = _by_code(report, CODE_ORDERS_WITHOUT_PAYOUT)[0]
    assert w.severity is DQSeverity.CAUTION
    assert w.evidence["share_of_gross"] == pytest.approx(4854.60 / 32033.09)


def test_orders_without_payout_small_share_informational() -> None:
    # 128.91 / 60,000 ≈ 0.2% → INFO only.
    owp = pd.DataFrame({"Period": [2], "Total gross sale": [128.91]})
    summary = {APR_2026: {"Total Gross Sale": 60000.0}}
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, orders_without_payout=owp)],
        summary_by_period=summary, comparison=_comparison(has_yoy_rev=False),
        current_period=APR_2026,
    )
    assert _by_code(report, CODE_ORDERS_WITHOUT_PAYOUT)[0].severity is DQSeverity.INFO


# ─────────────────────────────────────────────────────────────────────────────
# Empty / missing sheets
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_unmapped_payout_is_zero_not_crash() -> None:
    empty = pd.DataFrame(columns=["Period", "Order ID", "Seller SKU"])  # header only
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, unmapped_payout=empty)],
        summary_by_period={}, comparison=_comparison(has_yoy_rev=False),
        current_period=APR_2026,
    )
    ws = _by_code(report, CODE_UNMAPPED_PAYOUT)
    assert len(ws) == 1
    assert ws[0].count == 0
    assert ws[0].evidence.get("empty_sheet") is True


def test_missing_optional_sheet_omits_code_with_warning(caplog) -> None:
    # The pipeline logger sets propagate=False, so attach caplog's handler to it
    # directly to observe the warning.
    dq_logger = logging.getLogger("src.analysis.data_quality")
    dq_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="src.analysis.data_quality"):
            report = build_data_quality_report(
                [_loaded(MOM_PERIODS)],  # all four sheets None
                summary_by_period={}, comparison=_comparison(has_yoy_rev=False),
                current_period=APR_2026,
            )
    finally:
        dq_logger.removeHandler(caplog.handler)
    # No code emitted for an absent sheet…
    assert _by_code(report, CODE_UNMAPPED_ADS) == []
    assert _by_code(report, CODE_CANCELED_SHIPPING) == []
    # …and the omission was logged.
    assert any("absent" in r.message.lower() or "omitting" in r.message.lower()
               for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Channel caveats
# ─────────────────────────────────────────────────────────────────────────────
def test_ad_cost_mapping_gap_from_summary_notes() -> None:
    summary = {APR_2026: {"Total AD Cost": -2210.70, "AD Cost Mapped": -1861.55}}
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS)], summary_by_period=summary,
        comparison=_comparison(has_yoy_rev=False), current_period=APR_2026,
    )
    w = _by_code(report, CODE_AD_COST_MAPPING_GAP)[0]
    assert w.amount == pytest.approx(349.15)
    assert w.period == APR_2026
    assert w.severity is DQSeverity.CAUTION


def test_unsettled_referral_fee_ties_to_orders_without_payout() -> None:
    owp = pd.DataFrame({"Period": [2], "Total gross sale": [4854.60]})
    summary = {APR_2026: {"Total Gross Sale": 32033.09, "Unsettled Referral Fee": -291.28}}
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, orders_without_payout=owp)], summary_by_period=summary,
        comparison=_comparison(has_yoy_rev=False), current_period=APR_2026,
    )
    w = _by_code(report, CODE_UNSETTLED_REFERRAL_FEE)[0]
    assert w.amount == pytest.approx(-291.28)
    assert w.evidence["orders_without_payout_gross"] == pytest.approx(4854.60)


def test_yoy_unallocated_credit_surfaced_without_adjustment() -> None:
    # Credit sits in the YoY *baseline* (April-2025) summary.
    summary = {APR_2025: {"Unallocated Credit": -32.99}}
    report = build_data_quality_report(
        [_loaded(YOY_PERIODS)], summary_by_period=summary,
        comparison=_comparison(), current_period=APR_2026,
    )
    w = _by_code(report, CODE_YOY_UNALLOCATED_CREDIT)[0]
    assert w.period == APR_2025
    assert w.amount == pytest.approx(-32.99)
    assert w.severity is DQSeverity.INFO


def test_yoy_bridge_residual_disclosed_when_not_reconciling() -> None:
    cost_yoy = CostBridge(APR_2025, APR_2026, profit_change=10000.0, line_deltas={},
                          sum_of_line_deltas=9700.30, residual=299.70, reconciles=False,
                          threshold=1.00)
    report = build_data_quality_report(
        [_loaded(YOY_PERIODS)], summary_by_period={},
        comparison=_comparison(cost_yoy=cost_yoy), current_period=APR_2026,
    )
    w = _by_code(report, CODE_YOY_BRIDGE_RESIDUAL)[0]
    assert w.amount == pytest.approx(299.70)
    assert w.period is None  # cross-period caveat
    assert w.severity is DQSeverity.CAUTION


def test_reconciling_bridge_emits_no_residual_warning() -> None:
    cost_yoy = CostBridge(APR_2025, APR_2026, profit_change=10000.0, line_deltas={},
                          sum_of_line_deltas=9999.82, residual=0.18, reconciles=True,
                          threshold=1.00)
    report = build_data_quality_report(
        [_loaded(YOY_PERIODS)], summary_by_period={},
        comparison=_comparison(cost_yoy=cost_yoy), current_period=APR_2026,
    )
    assert _by_code(report, CODE_YOY_BRIDGE_RESIDUAL) == []


# ─────────────────────────────────────────────────────────────────────────────
# Two-file dedup + single-lens robustness
# ─────────────────────────────────────────────────────────────────────────────
def test_shared_current_period_deduped_across_two_files() -> None:
    mom_ads = pd.DataFrame({"Period": [1, 2], "Cost": [2.40, 0.63]})      # Mar, Apr2026
    yoy_ads = pd.DataFrame({"Period": [1, 2], "Cost": [5.00, 0.63]})      # Apr2025, Apr2026
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, unmapped_ads=mom_ads), _loaded(YOY_PERIODS, unmapped_ads=yoy_ads)],
        summary_by_period={}, comparison=_comparison(), current_period=APR_2026,
    )
    periods = [w.period for w in _by_code(report, CODE_UNMAPPED_ADS)]
    # April-2026 appears once (first file wins), plus Mar-2026 and Apr-2025.
    assert periods.count(APR_2026) == 1
    assert set(periods) == {MAR_2026, APR_2026, APR_2025}


def test_mom_only_run_no_yoy_caveats_no_crash() -> None:
    ads = pd.DataFrame({"Period": [1, 2], "Cost": [2.40, 0.63]})
    report = build_data_quality_report(
        [_loaded(MOM_PERIODS, unmapped_ads=ads)], summary_by_period={},
        comparison=_comparison(has_yoy_rev=False), current_period=APR_2026,
    )
    assert _by_code(report, CODE_YOY_UNALLOCATED_CREDIT) == []
    assert _by_code(report, CODE_YOY_BRIDGE_RESIDUAL) == []
    assert len(_by_code(report, CODE_UNMAPPED_ADS)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Summary-note figure extraction
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_note_figures_ad_cost_mapped() -> None:
    # "Delayed partially" → the line's own magnitude becomes the mapped figure.
    summary = {"Total AD Cost": -1861.55, "Total AD Cost__note": "Delayed partially"}
    out = _extract_note_figures(summary)
    assert out[KEY_AD_COST_MAPPED] == pytest.approx(1861.55)


def test_extract_note_figures_unsettled_referral_fee() -> None:
    summary = {"Total Referral Fee__note": "$4,854.60 unsettled"}
    out = _extract_note_figures(summary)
    assert out[KEY_UNSETTLED_REFERRAL_FEE] == pytest.approx(4854.60)


def test_extract_note_figures_unallocated_credit() -> None:
    summary = {"Total Other Expense__note": "+$32.99 unallocated credit"}
    out = _extract_note_figures(summary)
    assert out[KEY_UNALLOCATED_CREDIT] == pytest.approx(32.99)


def test_extract_note_figures_empty_and_blank_notes_extract_nothing() -> None:
    assert _extract_note_figures({}) == {}
    assert _extract_note_figures({"Total Profit": 100.0}) == {}      # no __note keys
    assert _extract_note_figures({"X__note": None, "Y__note": ""}) == {}  # blank notes
    # A dollar amount with no "unsettled"/credit context matches nothing.
    assert _extract_note_figures({"Z__note": "see $4,854.60 elsewhere"}) == {}


def test_channel_caveats_surface_all_three_from_notes_end_to_end() -> None:
    # The whole point: notes alone (no pre-populated numeric keys) drive all three
    # caveats. The credit note sits in the YoY *baseline* (April-2025) dict.
    summary = {
        APR_2026: {
            "Total Gross Sale": 32033.09,
            "Total AD Cost": -2210.70,
            "Total AD Cost__note": "Delayed partially",
            "Total Referral Fee__note": "$4,854.60 unsettled",
        },
        APR_2025: {"Total Other Expense__note": "+$32.99 unallocated credit"},
    }
    report = build_data_quality_report(
        [_loaded(YOY_PERIODS)], summary_by_period=summary,
        comparison=_comparison(), current_period=APR_2026,
    )
    assert _by_code(report, CODE_AD_COST_MAPPING_GAP)
    assert _by_code(report, CODE_UNSETTLED_REFERRAL_FEE)
    credit = _by_code(report, CODE_YOY_UNALLOCATED_CREDIT)
    assert credit and credit[0].period == APR_2025
    assert credit[0].amount == pytest.approx(32.99)
