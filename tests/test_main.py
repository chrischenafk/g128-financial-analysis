"""Tests for src/main.py — the orchestration glue.

The layer functions are mocked; main.py owns no business logic, so these tests
assert *wiring*: the steps run in order, the manifest is written atomically, the
skip-existing/`--force` guards work, single-lens runs don't crash, the anchor
mismatch is a hard stop, and a history-store failure is survivable.

``normalize_workbook`` is patched to return real (tiny) NormalizedWorkbook
objects so main's own glue — the VD1 anchor check, the summary merge, the
current-period profit lookup — executes for real rather than against a Mock.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src import config, main as main_mod
from src.ingest.period_parser import FilePeriods, Period
from src.transform.normalize_tiktok import NormalizedWorkbook

TARGET = Period(2026, 4)
MAR = Period(2026, 3)
APR25 = Period(2025, 4)

MOM_PATH = Path("Tiktok 2026.03 vs 2026.04.xlsm")
YOY_PATH = Path("Tiktok 2025.04 vs 2026.04.xlsm")
MOM_FP = FilePeriods(MOM_PATH, current=TARGET, comparison=MAR, comparison_type="MoM")
YOY_FP = FilePeriods(YOY_PATH, current=TARGET, comparison=APR25, comparison_type="YoY")


def _normalized(current_gross: float, current_profit: float, baseline: Period) -> NormalizedWorkbook:
    """A minimal but real NormalizedWorkbook with a 2-period catalog + summary."""
    sku_level = pd.DataFrame({
        "period": [TARGET, baseline],
        "Marketplace SKU": ["FG-A", "FG-A"],
        "Total Gross Sale": [current_gross, 500.0],
        "Total Profit": [current_profit, 100.0],
        "Total Sold Units": [10, 8],
    })
    summary = {
        TARGET: {"Total Gross Sale": current_gross, "Total Profit": current_profit,
                 "Total Sold Units": 10},
        baseline: {"Total Gross Sale": 500.0, "Total Profit": 100.0, "Total Sold Units": 8},
    }
    return NormalizedWorkbook(source_path=Path("x.xlsm"), periods=(baseline, TARGET),
                              sku_level=sku_level, summary=summary)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Patch every layer call on src.main; return a namespace of the mocks."""
    monkeypatch.setattr(config, "MANIFEST", tmp_path / "run_manifest.json")
    monkeypatch.setattr(main_mod.paths, "ensure_directories", MagicMock())

    mgr = MagicMock()  # records global call order across all attached mocks

    def attach(name, mock):
        monkeypatch.setattr(main_mod, name, mock)
        mgr.attach_mock(mock, name)
        return mock

    # Ingest
    attach("scan_raw_files", MagicMock(return_value=[MOM_PATH, YOY_PATH]))
    attach("parse_and_validate",
           MagicMock(side_effect=lambda p: {MOM_PATH: MOM_FP, YOY_PATH: YOY_FP}[p]))
    attach("load_workbook", MagicMock(side_effect=lambda path, fp: SimpleNamespace(path=path, periods=fp)))

    # Transform — real NormalizedWorkbooks so the glue runs for real.
    attach("normalize_workbook",
           MagicMock(side_effect=[_normalized(1000.0, 200.0, MAR), _normalized(1000.0, 200.0, APR25)]))
    attach("compute_sku_metrics", MagicMock(return_value={TARGET: MagicMock(name="metrics")}))
    attach("init_db", MagicMock(return_value=MagicMock(name="engine")))
    attach("record_period", MagicMock())

    # Analysis
    attach("period_data_from_normalized", MagicMock(return_value=MagicMock(name="pd")))
    attach("compare", MagicMock(return_value=MagicMock(name="comparison")))
    attach("detect_anomalies", MagicMock(return_value=MagicMock(name="anomalies")))
    attach("build_data_quality_report", MagicMock(return_value=MagicMock(name="dq")))

    # Package + report
    attach("write_package", MagicMock(return_value=tmp_path / "pkg" / "TikTok_2026-04"))
    attach("prepare_report_inputs", MagicMock(return_value=SimpleNamespace(
        package_json=tmp_path / "pkg" / "package.json", charts=[], workdir=tmp_path / "pkg")))
    attach("generate_report", MagicMock(return_value=tmp_path / "report.docx"))

    return SimpleNamespace(mgr=mgr, tmp=tmp_path)


def _names_in_order(mgr) -> list[str]:
    return [c[0] for c in mgr.mock_calls if c[0] and "." not in c[0]]


def _is_subsequence(sub, seq) -> bool:
    it = iter(seq)
    return all(item in it for item in sub)


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_both_lenses_runs_in_order(wired) -> None:
    rc = main_mod.main([])
    assert rc == 0

    order = _names_in_order(wired.mgr)
    expected = ["scan_raw_files", "load_workbook", "normalize_workbook", "compute_sku_metrics",
                "compare", "detect_anomalies", "build_data_quality_report", "record_period",
                "write_package", "prepare_report_inputs", "generate_report"]
    assert _is_subsequence(expected, order), order

    # Manifest written with a complete entry, and no .tmp left behind.
    manifest = json.loads(config.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["2026-04"]["status"] == "complete"
    assert manifest["2026-04"]["schema_version"] == "1.0.0"
    assert manifest["2026-04"]["mom_file"] == MOM_PATH.name
    assert manifest["2026-04"]["yoy_file"] == YOY_PATH.name
    assert not (config.MANIFEST.parent / (config.MANIFEST.name + ".tmp")).exists()


def test_nothing_to_do_exits_zero(wired) -> None:
    wired.mgr  # keep fixture
    main_mod.scan_raw_files.return_value = []
    assert main_mod.main([]) == 0
    main_mod.load_workbook.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Skip-existing-report guard
# ─────────────────────────────────────────────────────────────────────────────
def test_skip_existing_report_without_force(wired) -> None:
    config.MANIFEST.write_text(json.dumps({"2026-04": {"status": "complete"}}), encoding="utf-8")
    assert main_mod.main([]) == 0
    # No processing past pairing.
    main_mod.load_workbook.assert_not_called()
    main_mod.write_package.assert_not_called()


def test_force_overrides_skip(wired) -> None:
    config.MANIFEST.write_text(json.dumps({"2026-04": {"status": "complete"}}), encoding="utf-8")
    assert main_mod.main(["--force"]) == 0
    main_mod.write_package.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Target selection / missing files
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_target_period_errors_before_loading(wired) -> None:
    rc = main_mod.main(["--target-period", "2025-12"])  # no file has this current period
    assert rc == 1
    main_mod.load_workbook.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# MoM-only run
# ─────────────────────────────────────────────────────────────────────────────
def test_mom_only_run_no_crash(wired, monkeypatch) -> None:
    # Only the MoM file is present and valid.
    main_mod.scan_raw_files.return_value = [MOM_PATH]
    main_mod.parse_and_validate.side_effect = lambda p: {MOM_PATH: MOM_FP}[p]
    main_mod.normalize_workbook.side_effect = [_normalized(1000.0, 200.0, MAR)]

    # Capture INFO from main's logger (propagate=False → caplog can't see it).
    info_msgs: list[str] = []
    monkeypatch.setattr(
        main_mod.logger, "info",
        lambda msg, *args, **kwargs: info_msgs.append(msg % args if args else msg),
    )

    assert main_mod.main([]) == 0
    # VD1 anchor match is skipped cleanly on a single-file run (logged, not silent).
    assert any("VD1 skipped" in m for m in info_msgs), info_msgs
    # YoY baseline absent in the package inputs / manifest; source is the MoM file
    # alone (no " + " join).
    main_mod.write_package.assert_called_once()
    inputs = main_mod.write_package.call_args.args[0]
    assert inputs.yoy_baseline is None
    assert inputs.mom_baseline == MAR
    assert inputs.source_file == MOM_PATH.name
    manifest = json.loads(config.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["2026-04"]["yoy_file"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Anchor mismatch — hard stop
# ─────────────────────────────────────────────────────────────────────────────
def test_anchor_mismatch_hard_stops_no_package(wired) -> None:
    # The two files disagree on the current period's profit → VD1 must abort.
    main_mod.normalize_workbook.side_effect = [
        _normalized(1000.0, 200.0, MAR), _normalized(1000.0, 999.0, APR25)]
    rc = main_mod.main([])
    assert rc == 1
    main_mod.write_package.assert_not_called()
    assert not config.MANIFEST.exists()  # nothing recorded


# ─────────────────────────────────────────────────────────────────────────────
# History-store failure is survivable
# ─────────────────────────────────────────────────────────────────────────────
def test_history_failure_continues_to_report(wired) -> None:
    main_mod.record_period.side_effect = RuntimeError("db locked")
    rc = main_mod.main([])
    assert rc == 0  # the report is still produced
    main_mod.write_package.assert_called_once()
    main_mod.generate_report.assert_called_once()
    manifest = json.loads(config.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["2026-04"]["status"] == "complete"
