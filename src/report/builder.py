"""Run the skill's deterministic preprocessing locally, before the skill call.

The external skill's file mounting doesn't work over the API, so it otherwise
reconstructs the package from conversation text (losing the segmentation table,
charts, and full verification). To fix that, this builder runs the skill's own
``load_package.py`` and ``charts.py`` (vendored verbatim under ``engine/``) as
subprocesses and hands the resulting ``package.json`` + chart PNGs to the skill —
exactly the input it expects.

This is glue around the vendored scripts: it does no business computation and
never edits their logic. ``load_package.py`` failing is fatal (no package → no
report); ``charts.py`` failing is non-fatal (the report is still written, just
without charts). Deterministic, no network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src import config
from src.utils.logger import get_logger

logger = get_logger(__name__)

_ENGINE = Path(__file__).resolve().parent / "engine"
LOAD_PACKAGE = _ENGINE / "load_package.py"
CHARTS = _ENGINE / "charts.py"

# Chart kinds requested from charts.py; each maps to "<kind>.png" in the outdir.
# charts.py emits only those whose data exists (e.g. trend needs history), so we
# collect from disk rather than assuming all three.
CHART_KINDS = ("bridge_mom", "bridge_yoy", "trend")


@dataclass
class ReportInputs:
    """Locally pre-processed inputs handed to the skill."""

    package_json: Path        # output of load_package.py
    charts: list[Path]        # PNGs that were actually generated (may be empty)
    workdir: Path             # the working directory these live in


def _read_period(package_dir: Path) -> str:
    """'YYYY-MM' period label from the package's run_metadata.json."""
    meta = json.loads((package_dir / "run_metadata.json").read_text(encoding="utf-8"))
    return str(meta["current_period"]["start"])[:7]  # "2026-04-01" → "2026-04"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout/stderr as text."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _slim_package(package_json: Path, workdir: Path) -> Path:
    """Strip bulk raw arrays from package.json, keeping only what the skill
    needs for report writing. Returns path to the slimmed file.

    Removed (skill uses ranked{} subsets instead):
      - comparisons.mom / comparisons.yoy  — full 384-row arrays
      - sku_current                         — full 211-row array
      - known_dq_codes                      — static dict baked into the skill
      - supported_schema_versions           — housekeeping only

    Kept:
      - meta, channel (with bridges), anomalies, pipeline_warnings,
        loader_flags, context_md, ranked, historical, present, schema_version
    """
    pkg = json.loads(package_json.read_text(encoding="utf-8"))

    pkg.pop("sku_current", None)
    pkg.pop("known_dq_codes", None)
    pkg.pop("supported_schema_versions", None)
    # Keep ranked{} (pre-sorted top-N subsets) but drop the full comparison arrays
    # since ranked already contains the material movers the skill cites
    pkg.pop("comparisons", None)

    slim_path = workdir / "package_slim.json"
    slim_path.write_text(json.dumps(pkg), encoding="utf-8")  # no indent — saves ~20% vs indent=1

    size_before = package_json.stat().st_size
    size_after = slim_path.stat().st_size
    logger.info(
        "Slimmed package.json: %d KB → %d KB (removed raw SKU/comparison arrays).",
        size_before // 1024, size_after // 1024,
    )
    return slim_path


def prepare_report_inputs(package_dir: Path) -> ReportInputs:
    """Run load_package.py (fatal) then charts.py (best-effort) for ``package_dir``.

    Returns the ``package.json`` path plus the chart PNGs that were actually
    produced. Raises ``RuntimeError`` (naming the script + its stderr) if
    ``load_package.py`` fails; a ``charts.py`` failure is logged and yields an
    empty charts list rather than aborting.
    """
    period = _read_period(package_dir)
    workdir = config.OUTPUT_REPORTS / ".build" / period
    charts_dir = workdir / "charts"
    workdir.mkdir(parents=True, exist_ok=True)
    package_json = workdir / "package.json"

    # Step 1 — load_package.py (fatal on failure: no package → no report).
    result = _run([sys.executable, str(LOAD_PACKAGE), str(package_dir), "-o", str(package_json)])
    if result.returncode != 0:
        raise RuntimeError(
            f"load_package.py failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    if result.stdout.strip():
        logger.info("load_package.py: %s", result.stdout.strip())

    # Step 1b — slim package.json for upload (drop bulky raw arrays). Charts still
    # read the full package.json below; only the uploaded copy is slimmed.
    slim_json = _slim_package(package_json, workdir)

    # Step 2 — charts.py (best-effort: a failure must not block the report).
    charts: list[Path] = []
    chart_result = _run([
        sys.executable, str(CHARTS), str(package_json),
        "--outdir", str(charts_dir), "--which", ",".join(CHART_KINDS),
    ])
    if chart_result.returncode != 0:
        logger.warning(
            "charts.py failed (exit %d): %s — continuing without charts.",
            chart_result.returncode, chart_result.stderr.strip() or "<no stderr>",
        )
    else:
        # Collect only the PNGs that actually exist (e.g. trend is skipped with no history).
        charts = [charts_dir / f"{kind}.png" for kind in CHART_KINDS
                  if (charts_dir / f"{kind}.png").exists()]
        logger.info("charts.py produced %d chart(s): %s", len(charts), [p.name for p in charts])

    # Hand the skill the slimmed package.json (charts were rendered from the full one).
    return ReportInputs(package_json=slim_json, charts=charts, workdir=workdir)
