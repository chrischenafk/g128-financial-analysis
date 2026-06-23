"""Pipeline entry point — wires all five layers together.

This is glue, not logic. ``main.py`` coordinates the layers in order, passing
each one's output to the next, and handles CLI args + errors. It contains ZERO
business logic: no metric computation, no data cleaning, no schema knowledge.
Every real decision is delegated to the layer that owns it. If something here is
doing more than calling a layer function and handling its result, that logic is
in the wrong place.

Flow (do not reorder): setup → scan/parse → identify target & pair MoM/YoY →
load → normalize (+ VD1 anchor match) → SKU metrics → comparisons → anomalies →
data quality → history index → write package → (stubbed) Claude report → update
manifest.

CLI:
    python src/main.py
    python src/main.py --target-period 2026-04
    python src/main.py --target-period 2026-04 --force
    python src/main.py --context path/to/context.md

The Claude call (step 11) is a local stub until the LLM layer (``src/llm/``) is
built; it writes a placeholder report so the end-to-end wiring is exercised.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.analysis.anomalies import detect_anomalies, materiality_gate
from src.analysis.comparisons import compare, period_data_from_normalized
from src.analysis.data_quality import build_data_quality_report
from src.ingest.excel_loader import load_workbook
from src.ingest.file_scanner import scan_raw_files
from src.ingest.period_parser import Period, parse_and_validate
from src.llm.claude_client import generate_report
from src.package.writer import PackageInputs, write_package
from src.report.builder import _inject_charts, prepare_report_inputs
from src.transform.historical_index import init_db, record_period
from src.transform.normalize_tiktok import (
    GROSS_COLUMN,
    PROFIT_COLUMN,
    normalize_workbook,
)
from src.transform.sku_metrics import compute_sku_metrics
from src.utils import paths
from src.utils.logger import get_logger

logger = get_logger(__name__)

ANCHOR_EPS = 0.01  # VD1: current-period gross/profit must agree across files to the penny.
_PROFIT_LINE = "Total Profit"
_GROSS_LINE = "Total Gross Sale"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="g128-pipeline",
        description="Generate a TikTok Shop analysis package + report for one period.",
    )
    parser.add_argument(
        "--target-period",
        metavar="YYYY-MM",
        help="Process this current period (e.g. 2026-04). Default: the latest available.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if a report already exists for the target period.",
    )
    parser.add_argument(
        "--context",
        metavar="PATH",
        type=Path,
        help="Operator context file to use instead of data/raw/report_context.md.",
    )
    return parser.parse_args(argv)


def _parse_target_period(text: str) -> Period:
    """Parse a ``YYYY-MM`` string into a Period (raises ValueError on bad input)."""
    parts = text.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"--target-period must be YYYY-MM, got {text!r}.")
    return Period(int(parts[0]), int(parts[1]))


def _period_str(period: Period) -> str:
    return f"{period.year:04d}-{period.month:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# Manifest I/O (atomic write)
# ─────────────────────────────────────────────────────────────────────────────
def _load_manifest() -> dict:
    if config.MANIFEST.exists():
        try:
            return json.loads(config.MANIFEST.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read manifest %s (%s) — treating as empty.",
                           config.MANIFEST, exc)
    return {}


def _write_manifest_atomic(manifest: dict) -> None:
    """Write the manifest via a temp file + rename so a crash can't corrupt it."""
    tmp = config.MANIFEST.parent / (config.MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, config.MANIFEST)  # atomic on the same filesystem
    logger.info("Manifest updated: %s", config.MANIFEST)


def _rel(path: Path) -> str:
    """Path relative to the project root when possible (for a tidy manifest)."""
    try:
        return str(path.resolve().relative_to(config.PROJECT_ROOT))
    except ValueError:
        return str(path)


# ─────────────────────────────────────────────────────────────────────────────
# VD1 anchor match (lives here because it needs BOTH normalized outputs)
# ─────────────────────────────────────────────────────────────────────────────
def assert_anchor_match(mom_normalized, yoy_normalized, current_period: Period) -> None:
    """Hard-stop if the current period's gross/profit differ across the two files.

    A mismatch means the MoM and YoY files do not describe the same month, so any
    comparison would be meaningless. This is deliberately NOT caught downstream.
    """
    if mom_normalized is None or yoy_normalized is None:
        return  # single-lens run — nothing to cross-check
    mg, mp = _current_totals(mom_normalized, current_period)
    yg, yp = _current_totals(yoy_normalized, current_period)
    if abs(mg - yg) > ANCHOR_EPS or abs(mp - yp) > ANCHOR_EPS:
        raise ValueError(
            f"Anchor mismatch for {current_period}: the MoM file reports "
            f"(gross={mg:.2f}, profit={mp:.2f}) but the YoY file reports "
            f"(gross={yg:.2f}, profit={yp:.2f}). The two lenses must describe the "
            "same month; refusing to produce a meaningless report."
        )
    logger.info("VD1 anchor OK for %s: gross=%.2f profit=%.2f agree across both files.",
                current_period, mg, mp)


def _current_totals(normalized, period: Period) -> tuple[float, float]:
    """(gross, profit) for the current period from a normalized workbook's catalog."""
    df = normalized.sku_level
    sub = df[df["period"] == period]
    return float(sub[GROSS_COLUMN].sum()), float(sub[PROFIT_COLUMN].sum())


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
def _run(args: argparse.Namespace) -> int:
    # ── Step 0: setup ────────────────────────────────────────────────────────
    paths.ensure_directories()
    manifest = _load_manifest()

    # ── Step 1: scan + parse ─────────────────────────────────────────────────
    candidates = scan_raw_files()
    if not candidates:
        logger.info("No workbooks in %s — nothing to do.", config.DATA_RAW)
        return 0

    valid = []
    for path in candidates:
        try:
            valid.append(parse_and_validate(path))
        except Exception as exc:  # one bad filename shouldn't sink the run
            logger.warning("Skipping %s — failed period parse/validation: %s", path.name, exc)
    if not valid:
        logger.error("No workbooks passed period parsing/validation — aborting.")
        return 1

    # ── Step 2: identify target period + pair MoM / YoY ──────────────────────
    if args.target_period:
        try:
            target = _parse_target_period(args.target_period)
        except ValueError as exc:
            logger.error("%s", exc)
            return 1
    else:
        target = max(fp.current for fp in valid)
    logger.info("Target current period: %s", target)

    matched = [fp for fp in valid if fp.current == target]
    if not matched:
        logger.error("No workbook has current period %s. Available: %s.",
                     target, sorted({str(fp.current) for fp in valid}))
        return 1

    mom_fp = next((fp for fp in matched if fp.comparison_type == "MoM"), None)
    yoy_fp = next((fp for fp in matched if fp.comparison_type == "YoY"), None)
    if mom_fp is None and yoy_fp is None:
        logger.error("Neither a MoM nor a YoY file found for %s — cannot compare.", target)
        return 1
    if mom_fp is None:
        logger.warning("No MoM file for %s — proceeding YoY-only.", target)
    if yoy_fp is None:
        logger.warning("No YoY file for %s — proceeding MoM-only.", target)

    # Skip-existing-report guard.
    period_key = _period_str(target)
    if not args.force and manifest.get(period_key, {}).get("status") == "complete":
        logger.info("Report for %s already exists (manifest status=complete) and --force "
                    "not set — skipping. Use --force to regenerate.", period_key)
        return 0

    # ── Step 3: load workbooks ───────────────────────────────────────────────
    mom_loaded = load_workbook(mom_fp.path, mom_fp) if mom_fp else None
    yoy_loaded = load_workbook(yoy_fp.path, yoy_fp) if yoy_fp else None

    # ── Step 4: normalize + VD1 anchor match ─────────────────────────────────
    mom_norm = normalize_workbook(mom_loaded) if mom_loaded else None
    yoy_norm = normalize_workbook(yoy_loaded) if yoy_loaded else None
    assert_anchor_match(mom_norm, yoy_norm, target)  # hard stop on mismatch (not caught)

    current_norm = mom_norm if mom_norm is not None else yoy_norm

    # ── Step 5: SKU metrics ──────────────────────────────────────────────────
    current_metrics = compute_sku_metrics(current_norm)[target]

    # ── Step 6: comparisons ──────────────────────────────────────────────────
    current_pd = period_data_from_normalized(current_norm, target)
    mom_baseline_pd = period_data_from_normalized(mom_norm, mom_fp.comparison) if mom_fp else None
    yoy_baseline_pd = period_data_from_normalized(yoy_norm, yoy_fp.comparison) if yoy_fp else None
    current_mom_view = period_data_from_normalized(mom_norm, target) if mom_fp else None
    current_yoy_view = period_data_from_normalized(yoy_norm, target) if yoy_fp else None
    comparison = compare(
        current_pd, mom_baseline_pd, yoy_baseline_pd,
        current_mom_view=current_mom_view, current_yoy_view=current_yoy_view,
    )

    # ── Step 7: anomalies ────────────────────────────────────────────────────
    current_summary = current_norm.summary.get(target, {})
    current_period_profit = current_summary.get(_PROFIT_LINE)
    if current_period_profit is None:
        current_period_profit = float(current_metrics.metrics["profit"].sum())
    anomaly_report = detect_anomalies(
        current_metrics, comparison, current_period_profit,
        cost_detail=current_norm.sku_level, trailing=None,
    )

    # ── Step 8: data quality ─────────────────────────────────────────────────
    summary_by_period: dict = {}
    for norm in (yoy_norm, mom_norm):  # mom last → current period from MoM file wins
        if norm is not None:
            summary_by_period.update(norm.summary)
    dq_sources = [ld for ld in (mom_loaded, yoy_loaded) if ld is not None]
    dq_report = build_data_quality_report(
        sources=dq_sources, summary_by_period=summary_by_period,
        comparison=comparison, current_period=target,
    )

    # ── Step 9: history index (failure must NOT block the report) ────────────
    try:
        source_file = (mom_fp or yoy_fp).path.name
        engine = init_db()
        record_period(target, config.MARKETPLACE, source_file,
                      current_metrics.metrics, current_summary, engine=engine)
    except Exception as exc:
        logger.warning("History store update failed (%s) — continuing without it; "
                       "the report is unaffected.", exc)

    # ── Step 10: write package ───────────────────────────────────────────────
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    context_path = _resolve_context(args.context)
    gate = materiality_gate(current_period_profit)
    inputs = PackageInputs(
        current_period=target,
        mom_baseline=mom_fp.comparison if mom_fp else None,
        yoy_baseline=yoy_fp.comparison if yoy_fp else None,
        generated_at=generated_at,
        summary_current=current_summary,
        summary_mom_baseline=(mom_norm.summary.get(mom_fp.comparison) if mom_fp else None),
        summary_yoy_baseline=(yoy_norm.summary.get(yoy_fp.comparison) if yoy_fp else None),
        comparison=comparison,
        sku_metrics=current_metrics,
        materiality_gate=gate,
        anomaly_report=anomaly_report,
        data_quality_report=dq_report,
        sku_historical_trends=None,
        report_context_path=context_path,
        # Original workbook filename(s) for the cover's "Source:" line — joined with
        # " + " when both an MoM and a YoY workbook fed the run. path.name drops the
        # (gitignored) data/raw/ directory, leaving just the meaningful filename.
        source_file=" + ".join(
            fp.path.name for fp in (mom_fp, yoy_fp) if fp is not None
        ) or None,
    )
    package_dir = write_package(inputs, config.OUTPUT_PACKAGES)  # raises if channel_metrics missing

    # ── Step 10b: run load_package + charts locally (graceful fallback) ───────
    # Hands the skill a processed package.json + chart PNGs. If this fails, fall
    # back to the raw-file upload path — lower quality, but the run still produces
    # a report rather than crashing.
    try:
        report_inputs = prepare_report_inputs(package_dir)
    except Exception as exc:
        logger.warning("Local report pre-processing failed (%s) — falling back to raw "
                       "package upload; report quality may be lower.", exc)
        report_inputs = None

    # ── Step 11: Claude report (external skill) ──────────────────────────────
    report_path = generate_report(package_dir, report_inputs=report_inputs)

    # ── Step 11b: inject the real chart PNGs into the downloaded .docx ─────────
    # The skill embeds placeholders (it can't mount the chart files); swap in the
    # locally-rendered charts. Best-effort — a failure must not lose the report.
    if report_inputs is not None and report_inputs.charts:
        try:
            _inject_charts(report_path, report_inputs.charts)
        except Exception as exc:
            logger.warning("Chart injection failed (%s) — keeping the report without "
                           "injected charts.", exc)

    # ── Step 12: update manifest (atomic) ────────────────────────────────────
    manifest[period_key] = {
        "status": "complete",
        "package_dir": _rel(package_dir),
        "report_path": _rel(report_path),
        "generated_at": generated_at,
        "mom_file": mom_fp.path.name if mom_fp else None,
        "yoy_file": yoy_fp.path.name if yoy_fp else None,
        "schema_version": config.PACKAGE_SCHEMA_VERSION,
    }
    _write_manifest_atomic(manifest)

    logger.info("Pipeline complete for %s.", period_key)
    return 0


def _resolve_context(override: Path | None) -> Path | None:
    """--context override → data/raw/report_context.md → None (writer writes a stub)."""
    if override is not None:
        if override.exists():
            logger.info("Using operator context from --context: %s", override)
            return override
        logger.warning("--context %s does not exist — falling back.", override)
    default = config.DATA_RAW / "report_context.md"
    if default.exists():
        logger.info("Using operator context from %s.", default)
        return default
    logger.info("No operator context file found — the package will carry a stub.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """Parse args, run the pipeline, and translate any failure into a clean exit code."""
    args = _parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:
        # Top-level safety net: log the failure (with traceback to the log), never
        # dump a raw traceback to the operator's stdout. Anchor-match and the
        # channel_metrics required check reach here and stop the run by design.
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
