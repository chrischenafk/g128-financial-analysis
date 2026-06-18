"""Tests for src/report/builder.py.

The vendored scripts are NOT run — ``subprocess.run`` is mocked with a fake that
creates the output files load_package.py / charts.py would create, so we test the
orchestration: working-dir layout, fatal vs best-effort failure handling, and
collecting only the chart PNGs that actually exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config
from src.report import builder


def _make_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "TikTok_2026-04"
    pkg.mkdir()
    (pkg / "run_metadata.json").write_text(
        json.dumps({"current_period": {"label": "April 2026", "start": "2026-04-01",
                                       "end": "2026-04-30"}}), encoding="utf-8")
    (pkg / "channel_metrics.json").write_text("{}", encoding="utf-8")
    return pkg


def _fake_run(*, load_rc=0, load_err="", charts_rc=0, charts_pngs=()):
    """A subprocess.run replacement that materializes the scripts' output files."""
    def run(cmd, capture_output=True, text=True):
        script = Path(cmd[1]).name
        if script == "load_package.py":
            out = Path(cmd[cmd.index("-o") + 1])
            if load_rc == 0:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=load_rc, stdout="OK package loaded.", stderr=load_err)
        if script == "charts.py":
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            if charts_rc == 0:
                outdir.mkdir(parents=True, exist_ok=True)
                for name in charts_pngs:
                    (outdir / name).write_bytes(b"\x89PNG\r\n")
            return SimpleNamespace(returncode=charts_rc, stdout="",
                                   stderr="charts boom" if charts_rc else "")
        raise AssertionError(f"unexpected command: {cmd}")
    return run


@pytest.fixture()
def reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_REPORTS", tmp_path / "reports")
    return tmp_path / "reports"


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_returns_package_json_and_charts(tmp_path, monkeypatch, reports_dir) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(builder.subprocess, "run",
                        _fake_run(charts_pngs=("bridge_mom.png", "bridge_yoy.png")))

    ri = builder.prepare_report_inputs(pkg)

    workdir = reports_dir / ".build" / "2026-04"
    assert ri.workdir == workdir
    assert ri.package_json == workdir / "package.json"
    assert ri.package_json.exists()
    assert ri.charts == [workdir / "charts" / "bridge_mom.png",
                         workdir / "charts" / "bridge_yoy.png"]


# ─────────────────────────────────────────────────────────────────────────────
# load_package.py failure is fatal
# ─────────────────────────────────────────────────────────────────────────────
def test_load_package_failure_raises_with_script_and_stderr(tmp_path, monkeypatch, reports_dir) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(builder.subprocess, "run",
                        _fake_run(load_rc=2, load_err="schema_version MISSING"))
    with pytest.raises(RuntimeError, match="load_package.py.*schema_version MISSING"):
        builder.prepare_report_inputs(pkg)


# ─────────────────────────────────────────────────────────────────────────────
# charts.py failure is non-fatal
# ─────────────────────────────────────────────────────────────────────────────
def test_charts_failure_returns_empty_charts_no_raise(tmp_path, monkeypatch, reports_dir) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(builder.subprocess, "run", _fake_run(charts_rc=1))

    ri = builder.prepare_report_inputs(pkg)
    assert ri.package_json.exists()      # load_package still succeeded
    assert ri.charts == []               # charts failed → empty, no raise


# ─────────────────────────────────────────────────────────────────────────────
# Only some charts generated
# ─────────────────────────────────────────────────────────────────────────────
def test_only_existing_charts_are_collected(tmp_path, monkeypatch, reports_dir) -> None:
    # charts.py exits 0 but only writes bridge_mom.png (no history → no trend.png).
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(builder.subprocess, "run", _fake_run(charts_pngs=("bridge_mom.png",)))

    ri = builder.prepare_report_inputs(pkg)
    workdir = reports_dir / ".build" / "2026-04"
    assert ri.charts == [workdir / "charts" / "bridge_mom.png"]  # trend/yoy absent → excluded
