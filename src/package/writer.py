"""Package writer — the contract-lock file (pipeline → external skill).

Package layer (the only file). Everything this module emits is the **versioned
contract** the external report-synthesis skill consumes; the authoritative field
names and structures are in the skill's ``references/package-schema.md`` (schema
version ``config.PACKAGE_SCHEMA_VERSION`` = "1.0.0").

This module does **zero business computation**. It translates, shapes, and
serializes what the analysis layers already produced — it never recomputes a
delta, re-derives a threshold or segment, re-classifies an anomaly, or queries
the history store. The only arithmetic it performs is *presentational*: rounding,
and the contract's channel ratios / percentage-change fields derived directly
from the channel totals the analysis already provided. If a value isn't in the
inputs, it is **absent** from the package — never invented or back-filled.

Internal-vs-contract names: where pipeline names differ from the contract, this
writer is the **single translation point** (e.g. ``Marketplace SKU`` → ``sku``,
the ``PauseAds`` segment → ``"Pause Ads"``, the ``orders_without_payout`` DQ code
→ the contract's ``unsettled_payouts``). Internal names never leak into the
package.

Files emitted into ``output/analysis_packages/TikTok_{YYYY-MM}/`` (the directory
name is the package identifier). ``channel_metrics.json`` is REQUIRED — if the
current channel gross/profit can't be produced, the writer raises *before*
creating the directory or writing anything. All other files are optional and are
omitted (with a logged reason) when their lens/source is absent.
"""

from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src import config
from src.analysis.anomalies import CHANNEL, AnomalyReport
from src.analysis.comparisons import COST_BRIDGE_LINES, ComparisonResult
from src.analysis.data_quality import DataQualityReport
from src.ingest.period_parser import Period
from src.transform.normalize_tiktok import KEY_COLUMN
from src.transform.sku_metrics import SkuMetricsResult
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PIPELINE_VERSION = "1.0.0"
_PACKAGE_DIR_PREFIX = "TikTok"  # the package identifier prefix (marketplace = "TikTok Shop")
_NO_CONTEXT_STUB = "No additional operator context provided for this period.\n"

# ── Channel current-block: contract field → (Summary line item, is_cost_magnitude)
# Cost lines are stored signed-negative upstream; the contract wants positive
# magnitudes for cost-like fields, so we take abs() for those.
_CHANNEL_LINES: dict[str, tuple[str, bool]] = {
    "gross": ("Total Gross Sale", False),
    "profit": ("Total Profit", False),
    "ad_cost": ("Total AD Cost", True),
    "affiliate": ("Total Affiliate commission", True),
    "shipping": ("Total Tiktok Shipping cost", True),
    "cogs": ("Total Cost of Goods Sold", True),
    "refund": ("Total Refund", True),
}
_UNITS_LINE = "Total Sold Units"
_ORDERS_LINE = "Total Sold Orders"

# ── Segment enum → contract label (others pass through unchanged) ────────────
_SEGMENT_LABELS = {"TestMore": "Test More", "PauseAds": "Pause Ads"}

# ── Anomaly rule_id → contract "kind" vocabulary ─────────────────────────────
_KIND_BY_RULE = {
    "A": "both_lenses_down",
    "B": "lens_divergence",
    "C": "quiet_yoy_decline",
    "D": "profit_margin_drop",
    "E": "ad_efficiency",
    "F": "high_gross_low_margin",
    "G": "cost_setup_error",
    "H": "low_volume_caution",
    "I": "below_historical_avg",
}

# ── DQ internal code → contract code (unmapped codes pass through) ───────────
_DQ_CODE_MAP = {
    "orders_without_payout": "unsettled_payouts",
    "yoy_unallocated_credit": "unallocated_credit",
}
# ── DQ severity (internal → contract) ────────────────────────────────────────
_DQ_SEVERITY_MAP = {"caution": "warn", "info": "info"}
# ── Contract code → "affects" phrase ─────────────────────────────────────────
_DQ_AFFECTS = {
    "unsettled_payouts": "current-period margin (optimistic)",
    "unsettled_referral_fee": "current-period margin (optimistic)",
    "ad_cost_mapping_gap": "SKU-level ad attribution",
    "unmapped_ads": "SKU-level ad attribution",
    "canceled_shipping": "current-period shipping cost",
    "unmapped_payout": "payout completeness",
    "unallocated_credit": "YoY baseline reconciliation",
    "yoy_bridge_residual": "YoY cost bridge reconciliation",
    "missing_history": "trend analysis",
}

# ── Historical-trends contract columns (long format), in order ───────────────
_TREND_COLUMNS = ["sku", "theme", "period_label", "period_end",
                  "units", "gross", "profit", "profit_margin_pct"]
_TREND_OPTIONAL = ["trailing_3m_profit", "trailing_6m_avg_units", "trend_direction"]


# ─────────────────────────────────────────────────────────────────────────────
# Inputs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PackageInputs:
    """Everything the analysis layers produced, collected for serialization.

    All values arrive pre-computed. Channel totals are the per-period Summary
    line-item dicts (``{line item: value}``); the writer reads them and produces
    only the contract's presentational ratios / percentage-change fields.
    Baselines / lenses are ``None`` when that lens was not part of the run.
    """

    current_period: Period
    mom_baseline: Period | None
    yoy_baseline: Period | None
    generated_at: str  # ISO-8601; supplied by the caller (writer stays deterministic)

    # Channel P&L (Summary line items) per period.
    summary_current: dict[str, float | None]
    summary_mom_baseline: dict[str, float | None] | None
    summary_yoy_baseline: dict[str, float | None] | None

    # Cross-period facts and per-SKU current metrics.
    comparison: ComparisonResult
    sku_metrics: SkuMetricsResult

    # Materiality gate (computed in anomalies.py — passed in, not recomputed).
    materiality_gate: float

    # Evidence sets.
    anomaly_report: AnomalyReport
    data_quality_report: DataQualityReport

    # Pre-queried trailing history (long format) — None/empty → file omitted.
    sku_historical_trends: pd.DataFrame | None = None

    # Optional operator context file to copy into report_context.md.
    report_context_path: Path | None = None

    # Original workbook filename(s) for this run (e.g. the .xlsm names), joined for
    # display on the report cover's "Source:" line. None → field omitted from
    # run_metadata.json and the skill falls back to whatever it can infer.
    source_file: str | None = None

    marketplace: str = config.MARKETPLACE
    currency: str = config.CURRENCY
    pipeline_version: str = DEFAULT_PIPELINE_VERSION


# ─────────────────────────────────────────────────────────────────────────────
# Small presentational helpers
# ─────────────────────────────────────────────────────────────────────────────
def _r(value: float | None, ndigits: int = 2) -> float | None:
    """Round, mapping None/NaN → None (so it serializes as JSON null / empty CSV)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits)


def _val(summary: dict[str, float | None] | None, key: str) -> float | None:
    if not summary:
        return None
    v = summary.get(key)
    return None if v is None else float(v)


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    """``numerator / denominator * 100`` rounded; None if denominator is 0/missing."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _r(numerator / denominator * 100)


def _month_label(period: Period) -> str:
    return f"{calendar.month_name[period.month]} {period.year}"


def _period_block(period: Period) -> dict[str, str]:
    last_day = calendar.monthrange(period.year, period.month)[1]
    return {
        "label": _month_label(period),
        "start": f"{period.year:04d}-{period.month:02d}-01",
        "end": f"{period.year:04d}-{period.month:02d}-{last_day:02d}",
    }


def _json_default(obj: object) -> object:
    """Serialize Period and numpy scalars that slipped through; else fail loud."""
    if isinstance(obj, Period):
        return str(obj)
    if hasattr(obj, "item"):  # numpy scalar
        return obj.item()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _has_mom(cmp_: ComparisonResult) -> bool:
    return cmp_.revenue_bridge_mom is not None or cmp_.cost_bridge_mom is not None


def _has_yoy(cmp_: ComparisonResult) -> bool:
    return cmp_.revenue_bridge_yoy is not None or cmp_.cost_bridge_yoy is not None


# ─────────────────────────────────────────────────────────────────────────────
# Builders (pure dict/DataFrame construction — no I/O)
# ─────────────────────────────────────────────────────────────────────────────
def _build_channel_metrics(inp: PackageInputs) -> dict:
    """Build channel_metrics. Raises if the required current gross/profit absent."""
    cur = inp.summary_current or {}

    current: dict[str, float | int] = {}
    for field_name, (line, is_cost) in _CHANNEL_LINES.items():
        raw = _val(cur, line)
        if raw is None:
            continue
        current[field_name] = _r(abs(raw) if is_cost else raw)

    if "gross" not in current or "profit" not in current:
        raise ValueError(
            "channel_metrics is REQUIRED but current-period gross/profit could "
            f"not be produced from the Summary dict (found keys: {sorted(cur)}). "
            "Refusing to write a package without it."
        )

    gross = current["gross"]
    units = _val(cur, _UNITS_LINE)
    orders = _val(cur, _ORDERS_LINE)
    if units is not None:
        current["units"] = int(round(units))
    if orders is not None:  # "orders (if available, else omit)"
        current["orders"] = int(round(orders))

    current["profit_margin_pct"] = _pct(current["profit"], gross)
    current["ad_pct_of_gross"] = _pct(current.get("ad_cost"), gross)
    current["affiliate_pct_of_gross"] = _pct(current.get("affiliate"), gross)
    current["refund_rate_pct"] = _pct(current.get("refund"), gross)
    # Drop any ratio that came back None (denominator/numerator missing).
    current = {k: v for k, v in current.items() if v is not None}

    metrics: dict[str, object] = {"current": current}

    mom = _lens_block(inp.summary_current, inp.summary_mom_baseline)
    if _has_mom(inp.comparison) and mom is not None:
        metrics["mom"] = mom
    yoy = _lens_block(inp.summary_current, inp.summary_yoy_baseline)
    if _has_yoy(inp.comparison) and yoy is not None:
        metrics["yoy"] = yoy

    bridge_mom = _bridge_lines(inp.comparison.cost_bridge_mom, with_pct=True)
    if bridge_mom is not None:
        metrics["bridge_mom"] = bridge_mom
    bridge_yoy = _bridge_lines(inp.comparison.cost_bridge_yoy, with_pct=False)
    if bridge_yoy is not None:
        metrics["bridge_yoy"] = bridge_yoy

    return metrics


def _lens_block(
    cur: dict[str, float | None], base: dict[str, float | None] | None
) -> dict | None:
    """The mom/yoy block: percentage changes + a baseline P&L sub-object."""
    if base is None:
        return None
    cg, cp = _val(cur, "Total Gross Sale"), _val(cur, "Total Profit")
    cu = _val(cur, _UNITS_LINE)
    bg, bp = _val(base, "Total Gross Sale"), _val(base, "Total Profit")
    bu = _val(base, _UNITS_LINE)
    if bg is None or bp is None:
        return None

    cur_margin = _pct(cp, cg)
    base_margin = _pct(bp, bg)
    margin_pts = (_r(cur_margin - base_margin)
                  if (cur_margin is not None and base_margin is not None) else None)

    baseline_obj = {"gross": _r(bg), "profit": _r(bp)}
    if bu is not None:
        baseline_obj["units"] = int(round(bu))
    if base_margin is not None:
        baseline_obj["profit_margin_pct"] = base_margin

    block = {
        "gross_pct": _pct((cg - bg) if (cg is not None and bg is not None) else None, bg),
        "profit_pct": _pct((cp - bp), bp),
        "units_pct": _pct((cu - bu) if (cu is not None and bu is not None) else None, bu),
        "margin_pts": margin_pts,
        "baseline": baseline_obj,
    }
    return {k: v for k, v in block.items() if v is not None}


def _bridge_lines(cost_bridge, *, with_pct: bool) -> list[dict] | None:
    """Cost-bridge line deltas → contract bridge list. None if the lens is absent."""
    if cost_bridge is None:
        return None
    profit_change = cost_bridge.profit_change
    lines: list[dict] = []
    for label, delta in cost_bridge.line_deltas.items():
        # Translate the internal label to the full Summary line name (the contract).
        contract_line = COST_BRIDGE_LINES.get(label, label)
        entry: dict[str, object] = {"line": contract_line, "delta": _r(delta)}
        if with_pct:
            entry["pct_of_profit_delta"] = (_pct(delta, profit_change)
                                            if profit_change else None)
            if entry["pct_of_profit_delta"] is None:
                entry.pop("pct_of_profit_delta")
        lines.append(entry)
    return lines


def _build_run_metadata(inp: PackageInputs) -> dict:
    meta: dict[str, object] = {
        "marketplace": inp.marketplace,
        "package_schema_version": config.PACKAGE_SCHEMA_VERSION,
        "current_period": _period_block(inp.current_period),
    }
    # Baselines are OMITTED (not null) when the lens wasn't present.
    if inp.mom_baseline is not None and _has_mom(inp.comparison):
        meta["mom_baseline"] = _period_block(inp.mom_baseline)
    if inp.yoy_baseline is not None and _has_yoy(inp.comparison):
        meta["yoy_baseline"] = _period_block(inp.yoy_baseline)
    meta["pipeline_version"] = inp.pipeline_version
    meta["generated_at"] = inp.generated_at
    meta["currency"] = inp.currency
    # Original workbook name(s) for the cover's "Source:" line. Omitted (not null)
    # when unknown, so the skill never renders an empty/internal-artifact source.
    if inp.source_file:
        meta["source_file"] = inp.source_file
    return meta


def _build_sku_metrics_current(inp: PackageInputs) -> pd.DataFrame:
    m = inp.sku_metrics.metrics
    rows = []
    for _, r in m.iterrows():
        roas = _r(r.get("breakeven_roas"))
        rows.append({
            "sku": str(r[KEY_COLUMN]),
            "name": r.get("Product Name"),
            "theme": r.get("Theme"),
            "units": int(r["units"]) if pd.notna(r.get("units")) else None,
            "gross": _r(r["gross"]),
            "profit": _r(r["profit"]),
            "profit_margin_pct": _r(r.get("profit_margin_pct")),
            "ad_cost": _r(r.get("ad_spend")),  # already a positive magnitude
            "profit_before_ads": _r(r.get("profit_before_ads")),
            "break_even_roas": roas,  # None → empty in CSV (na_rep="")
            "segment": _SEGMENT_LABELS.get(r.get("segment"), r.get("segment")),
        })
    cols = ["sku", "name", "theme", "units", "gross", "profit", "profit_margin_pct",
            "ad_cost", "profit_before_ads", "break_even_roas", "segment"]
    return pd.DataFrame(rows, columns=cols)


def _build_comparison_csv(inp: PackageInputs, lens: str) -> pd.DataFrame | None:
    """One row per SKU active in either period for the given lens, or None if absent."""
    present = _has_mom(inp.comparison) if lens == "mom" else _has_yoy(inp.comparison)
    if not present:
        return None

    deltas = inp.comparison.sku_deltas
    theme_by_sku = _theme_lookup(inp.sku_metrics)
    gate = inp.materiality_gate
    pcol, ucol, scol = f"profit_delta_{lens}", f"units_delta_{lens}", f"status_{lens}"

    rows = []
    for _, r in deltas.iterrows():
        sku = str(r["marketplace_sku"])
        profit_delta = _f(r.get(pcol))
        if profit_delta is None:
            continue  # this SKU has no delta on this lens
        profit_current = _f(r.get("current_profit")) or 0.0
        profit_baseline = profit_current - profit_delta
        rows.append({
            "sku": sku,
            "theme": theme_by_sku.get(sku),
            "profit_current": _r(profit_current),
            "profit_baseline": _r(profit_baseline),
            "profit_delta": _r(profit_delta),
            "profit_delta_pct": (_pct(profit_delta, profit_baseline)
                                 if profit_baseline != 0 else None),
            "units_delta": _int_or_none(r.get(ucol)),
            "materiality": "material" if abs(profit_delta) >= gate else "noise",
        })
    cols = ["sku", "theme", "profit_current", "profit_baseline", "profit_delta",
            "profit_delta_pct", "units_delta", "materiality"]
    return pd.DataFrame(rows, columns=cols)


def _build_anomaly_flags(inp: PackageInputs) -> list[dict]:
    theme_by_sku = _theme_lookup(inp.sku_metrics)
    has_context = _context_present(inp.report_context_path)
    out = []
    for flag in inp.anomaly_report.flags:
        is_channel = flag.scope == CHANNEL
        out.append({
            "sku": "channel" if is_channel else flag.scope,
            "theme": None if is_channel else theme_by_sku.get(flag.scope),
            "kind": _KIND_BY_RULE.get(flag.rule_id, flag.rule_id),
            "pipeline_rule_id": flag.rule_id,
            "severity": flag.severity.value,  # already "high"/"medium"/"low"
            "evidence": flag.evidence,        # structured object, not stringified
            "lenses": _flag_lenses(flag, is_channel),
            "suggested_context": ("see report_context.md" if has_context
                                  else "no logged event"),
        })
    return out


def _build_dq_warnings(inp: PackageInputs) -> list[dict]:
    out = []
    for w in inp.data_quality_report.warnings:
        code = _DQ_CODE_MAP.get(w.code, w.code)
        out.append({
            "code": code,
            "severity": _DQ_SEVERITY_MAP.get(w.severity.value, w.severity.value),
            "message": w.description,
            "affects": _DQ_AFFECTS.get(code, ""),
        })
    return out


def _build_historical_trends(inp: PackageInputs) -> pd.DataFrame | None:
    df = inp.sku_historical_trends
    if df is None or df.empty:
        return None
    cols = [c for c in _TREND_COLUMNS if c in df.columns]
    cols += [c for c in _TREND_OPTIONAL if c in df.columns]
    return df[cols].copy() if cols else df.copy()


# ── tiny coercion helpers ────────────────────────────────────────────────────
def _f(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    f = _f(value)
    return None if f is None else int(round(f))


def _theme_lookup(sku_metrics: SkuMetricsResult) -> dict[str, object]:
    m = sku_metrics.metrics
    if "Theme" not in m.columns:
        return {}
    return {str(s): t for s, t in zip(m[KEY_COLUMN], m["Theme"])}


def _flag_lenses(flag, is_channel: bool) -> list[str]:
    """Derive lenses from the flag's evidence (mom/yoy delta keys or a 'lens' tag)."""
    if is_channel:
        return []
    ev = flag.evidence or {}
    if ev.get("lens") in ("mom", "yoy"):
        return [ev["lens"]]
    lenses = []
    for lens in ("mom", "yoy"):
        if any(k.endswith(f"_{lens}") and v is not None for k, v in ev.items()):
            lenses.append(lens)
    return lenses


def _context_present(path: Path | None) -> bool:
    return path is not None and path.exists() and path.read_text(encoding="utf-8").strip() != ""


# ─────────────────────────────────────────────────────────────────────────────
# I/O helpers
# ─────────────────────────────────────────────────────────────────────────────
def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, default=_json_default) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d bytes).", path.name, path.stat().st_size)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, na_rep="")  # NaN/None → empty cell, never "nan"/"None"
    logger.info("Wrote %s (%d rows, %d bytes).", path.name, len(df), path.stat().st_size)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    logger.info("Wrote %s (%d bytes).", path.name, path.stat().st_size)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def write_package(inputs: PackageInputs, output_dir: Path) -> Path:
    """Serialize ``inputs`` into the versioned package under ``output_dir``.

    ``output_dir`` is the base packages directory; the package itself goes in
    ``output_dir/TikTok_{YYYY-MM}/`` (the directory name is the identifier).
    Returns that package directory path.

    ``channel_metrics.json`` is built and validated FIRST: if the required
    current gross/profit can't be produced, this raises before creating the
    directory or writing any file.
    """
    # Build (and validate) the REQUIRED file before any filesystem mutation.
    channel_metrics = _build_channel_metrics(inputs)

    p = inputs.current_period
    package_dir = output_dir / f"{_PACKAGE_DIR_PREFIX}_{p.year:04d}-{p.month:02d}"
    package_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Writing package %s (schema %s).", package_dir.name, config.PACKAGE_SCHEMA_VERSION)

    # 1 + 2: metadata and the required channel metrics.
    _write_json(package_dir / "run_metadata.json", _build_run_metadata(inputs))
    _write_json(package_dir / "channel_metrics.json", channel_metrics)

    # 3: current SKU metrics.
    _write_csv(package_dir / "sku_metrics_current.csv", _build_sku_metrics_current(inputs))

    # 4 + 5: per-lens comparisons (omit the file entirely when the lens is absent).
    for lens in ("mom", "yoy"):
        df = _build_comparison_csv(inputs, lens)
        if df is None:
            logger.info("Omitting sku_comparisons_%s.csv — %s lens absent.", lens, lens.upper())
            continue
        _write_csv(package_dir / f"sku_comparisons_{lens}.csv", df)

    # 6: historical trends (omit if the store returned nothing).
    trends = _build_historical_trends(inputs)
    if trends is None:
        logger.info("Omitting sku_historical_trends.csv — no history available.")
    else:
        n_periods = trends["period_end"].nunique() if "period_end" in trends.columns else 0
        if n_periods < 2:
            logger.info("Thin history (%d period(s)) — emitting trends with a "
                        "missing_history note for the skill.", n_periods)
        _write_csv(package_dir / "sku_historical_trends.csv", trends)

    # 7 + 8: evidence sets (always emitted; the skill surfaces every item).
    _write_json(package_dir / "anomaly_flags.json", _build_anomaly_flags(inputs))
    _write_json(package_dir / "data_quality_warnings.json", _build_dq_warnings(inputs))

    # 9: operator context — copy if supplied, else a stub.
    if _context_present(inputs.report_context_path):
        _write_text(package_dir / "report_context.md",
                    inputs.report_context_path.read_text(encoding="utf-8"))
    else:
        _write_text(package_dir / "report_context.md", _NO_CONTEXT_STUB)

    logger.info("Package %s complete.", package_dir.name)
    return package_dir
