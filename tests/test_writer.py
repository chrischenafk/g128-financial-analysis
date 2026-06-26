"""Tests for src/package/writer.py.

Synthetic inputs only — no real company files. The writer is a pure
translator/serializer, so the tests build minimal upstream structures by hand
and assert the *contract*: file presence, field names, name translations
(segment labels, DQ codes/severity), NaN→empty handling, materiality
classification, structured (non-stringified) evidence, and the
required-file-raises-first guarantee.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd
import pytest

from src import config
from src.analysis.anomalies import (
    AnomalyFlag, AnomalyReport, Category, Direction, Severity,
)
from src.analysis.comparisons import ComparisonResult, CostBridge, RevenueBridge
from src.analysis.data_quality import DataQualityReport, DataQualityWarning, DQSeverity
from src.ingest.period_parser import Period
from src.package.writer import PackageInputs, write_package
from src.transform.sku_metrics import SegmentThresholds, SkuMetricsResult

APR_2026 = Period(2026, 4)
MAR_2026 = Period(2026, 3)
APR_2025 = Period(2025, 4)
GEN_AT = "2026-06-10T14:00:00Z"

# A complete-enough current channel summary (Summary line items, raw signs).
SUMMARY_CUR = {
    "Total Gross Sale": 32033.09, "Total Profit": 7595.09, "Total Sold Units": 2133,
    "Total Sold Orders": 1996, "Total AD Cost": -1861.55, "Total Affiliate commission": -1983.19,
    "Total Tiktok Shipping cost": -10336.77, "Total Cost of Goods Sold": -4008.23,
    "Total Refund": -704.91,
}
SUMMARY_MOM = {"Total Gross Sale": 66710.72, "Total Profit": 12490.01, "Total Sold Units": 4524}
SUMMARY_YOY = {"Total Gross Sale": 17133.93, "Total Profit": 5060.37, "Total Sold Units": 1458}


# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────
def _metrics() -> SkuMetricsResult:
    df = pd.DataFrame([
        {"period": APR_2026, "Marketplace SKU": "FG-A", "Product Name": "Alpha", "Theme": "Heritage",
         "gross": 6160.89, "profit": 1130.00, "units": 200, "ad_spend": 300.0,
         "profit_margin_pct": 18.34, "profit_before_ads": 1430.0, "breakeven_roas": 3.40,
         "segment": "TestMore"},
        {"period": APR_2026, "Marketplace SKU": "FG-B", "Product Name": "Beta", "Theme": "Classic",
         "gross": 100.0, "profit": -30.0, "units": 4, "ad_spend": 150.0,
         "profit_margin_pct": -30.0, "profit_before_ads": 120.0, "breakeven_roas": float("nan"),
         "segment": "PauseAds"},
    ])
    return SkuMetricsResult(APR_2026, df, SegmentThresholds(100, 300, 1000, 5, 15, 30))


def _deltas() -> pd.DataFrame:
    cols = ["marketplace_sku", "current_units", "current_gross", "current_profit",
            "profit_delta_mom", "gross_delta_mom", "units_delta_mom", "status_mom",
            "profit_delta_yoy", "gross_delta_yoy", "units_delta_yoy", "status_yoy"]
    rows = [
        # FG-A: big MoM drop (material), small YoY rise.
        {"marketplace_sku": "FG-A", "current_units": 200, "current_gross": 6160.89,
         "current_profit": 1130.0, "profit_delta_mom": -561.75, "gross_delta_mom": -600.0,
         "units_delta_mom": -50, "status_mom": "continuing", "profit_delta_yoy": 50.0,
         "gross_delta_yoy": 100.0, "units_delta_yoy": 20, "status_yoy": "continuing"},
        # FG-B: tiny MoM move (noise).
        {"marketplace_sku": "FG-B", "current_units": 4, "current_gross": 100.0,
         "current_profit": -30.0, "profit_delta_mom": -10.0, "gross_delta_mom": -5.0,
         "units_delta_mom": -1, "status_mom": "continuing", "profit_delta_yoy": -8.0,
         "gross_delta_yoy": -4.0, "units_delta_yoy": -1, "status_yoy": "continuing"},
        # FG-LAP: lapsed on MoM (baseline only) → new/lapsed handling.
        {"marketplace_sku": "FG-LAP", "current_units": None, "current_gross": None,
         "current_profit": None, "profit_delta_mom": -400.0, "gross_delta_mom": -500.0,
         "units_delta_mom": -30, "status_mom": "lapsed", "profit_delta_yoy": None,
         "gross_delta_yoy": None, "units_delta_yoy": None, "status_yoy": "absent"},
    ]
    return pd.DataFrame(rows, columns=cols)


def _comparison(*, has_mom=True, has_yoy=True) -> ComparisonResult:
    rev_mom = RevenueBridge(MAR_2026, APR_2026, -34677.63, 0, 0, 0, 0, 0.0, True) if has_mom else None
    rev_yoy = RevenueBridge(APR_2025, APR_2026, 14899.16, 0, 0, 0, 0, 0.0, True) if has_yoy else None
    cost_mom = CostBridge(MAR_2026, APR_2026, profit_change=-4894.92,
                          line_deltas={"Gross Sale": -34677.63, "Refund": 120.0},
                          sum_of_line_deltas=-4894.74, residual=-0.18, reconciles=True,
                          threshold=1.0) if has_mom else None
    cost_yoy = CostBridge(APR_2025, APR_2026, profit_change=2534.72,
                          line_deltas={"Gross Sale": 14899.16, "Refund": -50.0},
                          sum_of_line_deltas=2235.02, residual=299.70, reconciles=False,
                          threshold=1.0) if has_yoy else None
    return ComparisonResult(
        current_period=APR_2026, sku_deltas=_deltas(),
        revenue_bridge_mom=rev_mom, revenue_bridge_yoy=rev_yoy,
        cost_bridge_mom=cost_mom, cost_bridge_yoy=cost_yoy,
        structural_movers=pd.DataFrame(),
    )


def _anomalies() -> AnomalyReport:
    flags = [
        AnomalyFlag("A", "FG-A", Category.TREND, Severity.HIGH,
                    "both down", {"profit_delta_mom": -561.75, "profit_delta_yoy": -807.18},
                    Direction.DOWN),
        AnomalyFlag("F", "FG-B", Category.MARGIN, Severity.HIGH,
                    "high gross low margin", {"gross": 100.0, "margin_pct": -30.0}),
        AnomalyFlag("E", "channel", Category.AD, Severity.MEDIUM,
                    "channel ad up", {"lens": "mom", "ad_cost_delta": -200.0}, Direction.DOWN),
    ]
    by_sku: dict = {}
    by_cat: dict = {}
    for f in flags:
        by_sku.setdefault(f.scope, []).append(f)
        by_cat.setdefault(f.category, []).append(f)
    return AnomalyReport(APR_2026, flags, by_sku, by_cat)


def _data_quality() -> DataQualityReport:
    warns = [
        DataQualityWarning("orders_without_payout", APR_2026, DQSeverity.CAUTION,
                           4854.60, 346, "346 April orders unsettled at export."),
        DataQualityWarning("unmapped_ads", APR_2026, DQSeverity.INFO, 0.63, None,
                           "Unmapped ad spend $0.63."),
        DataQualityWarning("yoy_bridge_residual", None, DQSeverity.CAUTION, 299.70, None,
                           "YoY cost bridge residual $299.70."),
    ]
    by_code: dict = {}
    for w in warns:
        by_code.setdefault(w.code, []).append(w)
    return DataQualityReport(APR_2026, warns, by_code)


def _inputs(**overrides) -> PackageInputs:
    base = dict(
        current_period=APR_2026, mom_baseline=MAR_2026, yoy_baseline=APR_2025,
        generated_at=GEN_AT, summary_current=SUMMARY_CUR, summary_mom_baseline=SUMMARY_MOM,
        summary_yoy_baseline=SUMMARY_YOY, comparison=_comparison(), sku_metrics=_metrics(),
        materiality_gate=100.0, anomaly_report=_anomalies(), data_quality_report=_data_quality(),
        sku_historical_trends=None, report_context_path=None,
    )
    base.update(overrides)
    return PackageInputs(**base)


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _read_csv_rows(p: Path) -> list[dict]:
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ─────────────────────────────────────────────────────────────────────────────
# Full run
# ─────────────────────────────────────────────────────────────────────────────
def test_full_run_writes_expected_files(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    assert pkg.name == "TikTok_2026-04"
    names = {p.name for p in pkg.iterdir()}
    # 8 files for a both-lens run with no history (historical_trends omitted).
    assert names == {
        "run_metadata.json", "channel_metrics.json", "sku_metrics_current.csv",
        "sku_comparisons_mom.csv", "sku_comparisons_yoy.csv",
        "anomaly_flags.json", "data_quality_warnings.json", "report_context.md",
    }


def test_run_metadata_shape_and_labels(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    meta = _read_json(pkg / "run_metadata.json")
    assert meta["package_schema_version"] == config.PACKAGE_SCHEMA_VERSION == "1.0.0"
    assert meta["marketplace"] == "TikTok Shop"
    assert meta["current_period"] == {"label": "April 2026", "start": "2026-04-01", "end": "2026-04-30"}
    assert meta["mom_baseline"]["label"] == "March 2026"
    assert meta["yoy_baseline"] == {"label": "April 2025", "start": "2025-04-01", "end": "2025-04-30"}
    assert meta["generated_at"] == GEN_AT
    assert meta["currency"] == "USD"
    assert meta["run_type"] == "MoM+YoY"  # both files present


def test_channel_metrics_required_fields(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    cm = _read_json(pkg / "channel_metrics.json")
    cur = cm["current"]
    assert cur["gross"] == 32033.09
    assert cur["profit"] == 7595.09
    assert cur["units"] == 2133 and cur["orders"] == 1996
    assert cur["ad_cost"] == 1861.55          # positive magnitude
    assert cur["refund"] == 704.91
    assert cur["profit_margin_pct"] == 23.71  # 7595.09/32033.09*100
    assert cur["refund_rate_pct"] == 2.20
    # MoM/YoY blocks present with baseline sub-objects + bridges.
    assert cm["mom"]["baseline"]["gross"] == 66710.72
    assert "bridge_mom" in cm and "bridge_yoy" in cm
    # bridge_mom carries pct_of_profit_delta; bridge_yoy does not.
    assert "pct_of_profit_delta" in cm["bridge_mom"][0]
    assert "pct_of_profit_delta" not in cm["bridge_yoy"][0]
    # internal label → full contract Summary name.
    assert cm["bridge_mom"][0]["line"] == "Total Gross Sale"


def test_sku_metrics_csv_columns_translations_and_nan(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    rows = _read_csv_rows(pkg / "sku_metrics_current.csv")
    assert list(rows[0].keys()) == [
        "sku", "name", "theme", "units", "gross", "profit", "profit_margin_pct",
        "ad_cost", "profit_before_ads", "break_even_roas", "segment"]
    by_sku = {r["sku"]: r for r in rows}
    # segment enum → contract labels.
    assert by_sku["FG-A"]["segment"] == "Test More"
    assert by_sku["FG-B"]["segment"] == "Pause Ads"
    # NaN break_even_roas → empty string (not "nan"/"None").
    assert by_sku["FG-B"]["break_even_roas"] == ""
    assert by_sku["FG-A"]["break_even_roas"] == "3.4"


# ─────────────────────────────────────────────────────────────────────────────
# Comparisons
# ─────────────────────────────────────────────────────────────────────────────
def test_comparison_materiality_and_new_lapsed(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    rows = {r["sku"]: r for r in _read_csv_rows(pkg / "sku_comparisons_mom.csv")}
    # FG-A: |−561.75| ≥ 100 → material; baseline = current − delta.
    assert rows["FG-A"]["materiality"] == "material"
    assert float(rows["FG-A"]["profit_baseline"]) == pytest.approx(1130.0 - (-561.75))
    # FG-B: |−10| < 100 → noise.
    assert rows["FG-B"]["materiality"] == "noise"
    # FG-LAP: lapsed (current absent) → profit_current 0, baseline = +400.
    assert float(rows["FG-LAP"]["profit_current"]) == pytest.approx(0.0)
    assert float(rows["FG-LAP"]["profit_baseline"]) == pytest.approx(400.0)


# ─────────────────────────────────────────────────────────────────────────────
# Anomalies
# ─────────────────────────────────────────────────────────────────────────────
def test_anomaly_kind_mapping_and_structured_evidence(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    flags = _read_json(pkg / "anomaly_flags.json")
    by_sku = {f["sku"]: f for f in flags}
    assert by_sku["FG-A"]["kind"] == "both_lenses_down"
    assert by_sku["FG-A"]["pipeline_rule_id"] == "A"
    assert by_sku["FG-A"]["severity"] == "high"
    assert by_sku["FG-A"]["lenses"] == ["mom", "yoy"]
    # evidence is a JSON object, not a stringified blob.
    assert isinstance(by_sku["FG-A"]["evidence"], dict)
    assert by_sku["FG-B"]["kind"] == "high_gross_low_margin"
    # channel-scope flag: sku "channel", theme null, lens from the 'lens' evidence tag.
    chan = by_sku["channel"]
    assert chan["theme"] is None
    assert chan["kind"] == "ad_efficiency"
    assert chan["lenses"] == []  # channel-scope → no lenses


# ─────────────────────────────────────────────────────────────────────────────
# Data quality
# ─────────────────────────────────────────────────────────────────────────────
def test_dq_code_and_severity_translation(tmp_path: Path) -> None:
    pkg = write_package(_inputs(), tmp_path)
    warns = {w["code"]: w for w in _read_json(pkg / "data_quality_warnings.json")}
    # internal code → contract code; "caution" → "warn".
    assert "unsettled_payouts" in warns
    assert warns["unsettled_payouts"]["severity"] == "warn"
    assert warns["unsettled_payouts"]["affects"] == "current-period margin (optimistic)"
    assert warns["unmapped_ads"]["severity"] == "info"
    assert warns["yoy_bridge_residual"]["affects"] == "YoY cost bridge reconciliation"
    # every warning surfaced.
    assert len(warns) == 3


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────
def test_historical_trends_written_when_present(tmp_path: Path) -> None:
    trends = pd.DataFrame({
        "sku": ["FG-A", "FG-A"], "theme": ["Heritage", "Heritage"],
        "period_label": ["February 2026", "March 2026"],
        "period_end": ["2026-02-28", "2026-03-31"],
        "units": [10, 12], "gross": [100.0, 120.0], "profit": [20.0, 24.0],
        "profit_margin_pct": [20.0, 20.0],
    })
    pkg = write_package(_inputs(sku_historical_trends=trends), tmp_path)
    out = pkg / "sku_historical_trends.csv"
    assert out.exists()
    rows = _read_csv_rows(out)
    assert list(rows[0].keys()) == [
        "sku", "theme", "period_label", "period_end", "units", "gross", "profit", "profit_margin_pct"]


# ─────────────────────────────────────────────────────────────────────────────
# MoM-only run
# ─────────────────────────────────────────────────────────────────────────────
def test_mom_only_run_omits_yoy(tmp_path: Path) -> None:
    inp = _inputs(yoy_baseline=None, summary_yoy_baseline=None,
                  comparison=_comparison(has_yoy=False))
    pkg = write_package(inp, tmp_path)
    names = {p.name for p in pkg.iterdir()}
    assert "sku_comparisons_yoy.csv" not in names
    assert "sku_comparisons_mom.csv" in names
    meta = _read_json(pkg / "run_metadata.json")
    assert "yoy_baseline" not in meta      # omitted, not null
    assert "mom_baseline" in meta
    cm = _read_json(pkg / "channel_metrics.json")
    assert "yoy" not in cm and "bridge_yoy" not in cm
    assert "mom" in cm
    # run_type marks this as a single-lens run so the skill omits the YoY section.
    assert meta["run_type"] == "MoM"


# ─────────────────────────────────────────────────────────────────────────────
# Required-file-raises-first
# ─────────────────────────────────────────────────────────────────────────────
def test_channel_metrics_missing_raises_before_writing(tmp_path: Path) -> None:
    # Current summary lacks Total Profit → channel_metrics cannot be produced.
    bad_summary = {"Total Gross Sale": 32033.09}
    inp = _inputs(summary_current=bad_summary)
    with pytest.raises(ValueError, match="channel_metrics is REQUIRED"):
        write_package(inp, tmp_path)
    # Nothing was written — not even the package directory.
    assert not (tmp_path / "TikTok_2026-04").exists()
    assert list(tmp_path.iterdir()) == []
