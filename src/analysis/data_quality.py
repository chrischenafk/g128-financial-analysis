"""Completeness & reconciliation diagnostics — how much to trust the period.

Analysis layer — file 3 of 3 (completes the layer). This module parses the four
TikTok completeness/data-quality sheets and surfaces the channel-level
reconciliation caveats, producing a structured set of **data-quality warnings**
for the report's "Data Quality Caveats" section.

These are *completeness diagnostics*, not business anomalies: they tell the
reader how complete and settled the period's numbers are, not what to do
commercially. Business anomalies live in ``anomalies.py``; the two are kept
strictly separate.

The file-relative Period integer (CRITICAL):
    All four sheets carry an integer ``Period`` column with values 1 and 2 — NOT
    the date-range string, and the integer is **file-relative**: ``1`` = the
    file's baseline period, ``2`` = the file's current period. In the MoM file
    that is March / April-2026; in the YoY file it is April-2025 / April-2026.
    We map the integer to the real ``Period`` using each source's own
    ``FilePeriods`` (1 → ``comparison``, 2 → ``current``). Assuming "1 = prior
    month" in the absolute sense would mis-attach YoY-file diagnostics to the
    wrong period.

Channel caveats come from the Summary sheet's *note* figures (mapped ad cost,
unsettled referral fee, an unallocated marketplace credit) plus the YoY
cost-bridge residual from ``comparisons.py``. We **disclose, never force**: the
~$299.70 YoY residual and the $32.99 credit are surfaced as visible caveats; SKU
figures are never adjusted to absorb them.

Faithful & defensive parsing: a present-but-empty sheet → zero (not a crash); a
missing optional sheet → logged warning + the code is omitted (graceful
degradation, AGENTS.md §8); None cells → 0 before summing. Nothing here
recomputes a business metric or re-derives an anomaly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

import pandas as pd

from src.analysis.comparisons import ComparisonResult
from src.ingest.excel_loader import LoadedWorkbook
from src.ingest.period_parser import FilePeriods, Period
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Sheet column names (verified against the real sheets) ────────────────────
PERIOD_COL = "Period"
UNMAPPED_ADS_COST_COL = "Cost"
CANCELED_SHIPPING_COST_COL = "ShippingEasy Cost"
ORDERS_WITHOUT_PAYOUT_GROSS_COL = "Total gross sale"

# ── Warning codes (the report surfaces every one it receives) ────────────────
CODE_UNMAPPED_ADS = "unmapped_ads"
CODE_CANCELED_SHIPPING = "canceled_shipping"
CODE_ORDERS_WITHOUT_PAYOUT = "orders_without_payout"
CODE_UNMAPPED_PAYOUT = "unmapped_payout"
CODE_AD_COST_MAPPING_GAP = "ad_cost_mapping_gap"
CODE_UNSETTLED_REFERRAL_FEE = "unsettled_referral_fee"
CODE_YOY_UNALLOCATED_CREDIT = "yoy_unallocated_credit"
CODE_YOY_BRIDGE_RESIDUAL = "yoy_bridge_residual"

# ── Summary line-item / note keys read from the summary dict ─────────────────
# The first two are standard Summary line items (always present). The remaining
# three are *note-derived* figures: ``normalize_summary`` captures only the
# Summary's period-value columns, not its free-text "Note" column, so these keys
# are populated upstream (a Summary-note parser / main.py) when available. Absent
# → the corresponding caveat degrades gracefully (logged + omitted).
KEY_TOTAL_AD_COST = "Total AD Cost"
KEY_TOTAL_GROSS = "Total Gross Sale"
KEY_AD_COST_MAPPED = "AD Cost Mapped"            # note: portion mapped to items
KEY_UNSETTLED_REFERRAL_FEE = "Unsettled Referral Fee"  # note: est. fee on unsettled sales
KEY_UNALLOCATED_CREDIT = "Unallocated Credit"    # note: marketplace credit not on any SKU

# ── Thresholds ───────────────────────────────────────────────────────────────
# Orders-without-payout escalates from informational to a margin caveat when its
# gross is this share or more of the period's gross (April's $4,854.60 ≈ 15% of
# $32,033.09 made the reported margin slightly optimistic).
ORDERS_WITHOUT_PAYOUT_MATERIAL_SHARE = 0.10


# ─────────────────────────────────────────────────────────────────────────────
# Output containers
# ─────────────────────────────────────────────────────────────────────────────
class DQSeverity(str, Enum):
    INFO = "info"          # disclosed for completeness; doesn't bias the numbers
    CAUTION = "caution"    # material enough to caveat the reported figures


@dataclass(frozen=True)
class DataQualityWarning:
    """One completeness/reconciliation caveat with its supporting numbers.

    ``period`` is ``None`` for cross-period caveats (e.g. the YoY bridge
    residual). ``amount`` / ``count`` are ``None`` where not applicable.
    """

    code: str
    period: Period | None
    severity: DQSeverity
    amount: float | None
    count: int | None
    description: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DataQualityReport:
    current_period: Period
    warnings: list[DataQualityWarning]
    by_code: dict[str, list[DataQualityWarning]] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _map_period(period_int: int, fp: FilePeriods) -> Period | None:
    """File-relative integer → real Period (1 = baseline, 2 = current)."""
    if period_int == 1:
        return fp.comparison
    if period_int == 2:
        return fp.current
    logger.warning("Unexpected Period integer %r (expected 1 or 2) — skipping.", period_int)
    return None


def _per_period_totals(df: pd.DataFrame, value_col: str | None) -> list[tuple[int, float, int]] | None:
    """``[(period_int, summed_value, row_count)]`` sorted by period; None if no Period col.

    ``None`` value cells become 0 before summing. If ``value_col`` is absent the
    sum is 0 (count still reflects the rows).
    """
    if PERIOD_COL not in df.columns:
        logger.warning("Sheet has no %r column — cannot attribute rows to a period.", PERIOD_COL)
        return None
    work = df.copy()
    work["_pint"] = pd.to_numeric(work[PERIOD_COL], errors="coerce")
    work = work[work["_pint"].notna()]
    if value_col and value_col in work.columns:
        work["_val"] = pd.to_numeric(work[value_col], errors="coerce").fillna(0.0)
    else:
        work["_val"] = 0.0
    out: list[tuple[int, float, int]] = []
    for pint, grp in work.groupby(work["_pint"].astype(int), sort=True):
        out.append((int(pint), float(grp["_val"].sum()), int(len(grp))))
    return out


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def build_data_quality_report(
    sources: Sequence[LoadedWorkbook],
    summary_by_period: dict[Period, dict[str, float | None]],
    comparison: ComparisonResult,
    current_period: Period,
) -> DataQualityReport:
    """Parse the data-quality sheets and surface the channel caveats.

    ``sources`` are the loaded workbooks (each carries its own ``FilePeriods`` via
    ``LoadedWorkbook.periods`` for the integer→Period mapping). One source (MoM-
    or YoY-only) or both are valid. ``summary_by_period`` is the merged Summary
    line items across files (channel caveats read documented keys from it).
    ``comparison`` supplies the bridge reconcile/residual flags.

    A diagnostic for the shared current period appears in both files; the first
    occurrence per ``(code, period)`` wins (they describe the same month).
    """
    warnings: list[DataQualityWarning] = []
    seen: set[tuple[str, Period | None]] = set()

    for src in sources:
        _process_source(src, summary_by_period, warnings, seen)

    _channel_caveats(summary_by_period, comparison, current_period, warnings, seen)

    report = _assemble(current_period, warnings)
    _log_summary(report)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Per-file sheet parsing
# ─────────────────────────────────────────────────────────────────────────────
def _process_source(
    src: LoadedWorkbook,
    summary_by_period: dict[Period, dict[str, float | None]],
    warnings: list[DataQualityWarning],
    seen: set[tuple[str, Period | None]],
) -> None:
    fp = src.periods
    if fp is None:
        logger.warning(
            "Source %s has no FilePeriods — cannot map file-relative periods; skipping its sheets.",
            getattr(src, "path", "<unknown>"),
        )
        return

    _sheet_simple_sum(
        src.unmapped_ads, fp, CODE_UNMAPPED_ADS, UNMAPPED_ADS_COST_COL, warnings, seen,
        lambda p, amt, cnt: (
            DQSeverity.INFO,
            f"Unmapped ad spend for {p}: ${amt:,.2f} of ad cost could not be mapped to a "
            "SKU/AllList (SKU-level unmapped figure; distinct from the channel AD-cost gap).",
        ),
    )
    _sheet_simple_sum(
        src.canceled_shipping, fp, CODE_CANCELED_SHIPPING, CANCELED_SHIPPING_COST_COL, warnings, seen,
        lambda p, amt, cnt: (
            DQSeverity.INFO,
            f"Canceled-order shipping leakage for {p}: ${amt:,.2f} of ShippingEasy cost paid on "
            "orders later canceled.",
        ),
    )
    _orders_without_payout(src.orders_without_payout, fp, summary_by_period, warnings, seen)
    _unmapped_payout(src.unmapped_payout, fp, warnings, seen, current=fp.current)


def _emit(
    warnings: list[DataQualityWarning],
    seen: set[tuple[str, Period | None]],
    warning: DataQualityWarning,
) -> None:
    """Append a warning unless the same (code, period) was already recorded."""
    key = (warning.code, warning.period)
    if key in seen:
        logger.debug("Duplicate %s for %s across files — keeping first.", warning.code, warning.period)
        return
    seen.add(key)
    warnings.append(warning)
    logger.info(
        "DQ %s @ %s: amount=%s count=%s severity=%s",
        warning.code, warning.period,
        f"{warning.amount:,.2f}" if warning.amount is not None else "—",
        warning.count if warning.count is not None else "—",
        warning.severity.value,
    )


def _sheet_simple_sum(
    df: pd.DataFrame | None,
    fp: FilePeriods,
    code: str,
    value_col: str,
    warnings: list[DataQualityWarning],
    seen: set[tuple[str, Period | None]],
    describe,
) -> None:
    """Sum one column per period for a sheet; missing → omit code, empty → zero."""
    if df is None:
        logger.warning("Optional sheet for %r absent — omitting that code.", code)
        return
    totals = _per_period_totals(df, value_col)
    if totals is None:
        return
    if not totals:  # present but empty → a clean zero at the current period
        sev, desc = describe(fp.current, 0.0, 0)
        _emit(warnings, seen, DataQualityWarning(code, fp.current, sev, 0.0, 0, desc,
                                                 {"empty_sheet": True}))
        return
    for pint, amount, count in totals:
        period = _map_period(pint, fp)
        if period is None:
            continue
        sev, desc = describe(period, amount, count)
        _emit(warnings, seen, DataQualityWarning(code, period, sev, amount, count, desc,
                                                 {"period_int": pint}))


def _orders_without_payout(
    df: pd.DataFrame | None,
    fp: FilePeriods,
    summary_by_period: dict[Period, dict[str, float | None]],
    warnings: list[DataQualityWarning],
    seen: set[tuple[str, Period | None]],
) -> None:
    """Sum gross + count orders per period; escalate when the share of gross is material."""
    if df is None:
        logger.warning("Optional sheet for %r absent — omitting that code.", CODE_ORDERS_WITHOUT_PAYOUT)
        return
    totals = _per_period_totals(df, ORDERS_WITHOUT_PAYOUT_GROSS_COL)
    if totals is None:
        return
    if not totals:
        _emit(warnings, seen, DataQualityWarning(
            CODE_ORDERS_WITHOUT_PAYOUT, fp.current, DQSeverity.INFO, 0.0, 0,
            f"No orders awaiting payout for {fp.current}.", {"empty_sheet": True}))
        return

    for pint, amount, count in totals:
        period = _map_period(pint, fp)
        if period is None:
            continue
        period_gross = _num((summary_by_period.get(period) or {}).get(KEY_TOTAL_GROSS))
        share = (amount / period_gross) if (period_gross and period_gross > 0) else None
        material = share is not None and share >= ORDERS_WITHOUT_PAYOUT_MATERIAL_SHARE
        severity = DQSeverity.CAUTION if material else DQSeverity.INFO
        share_txt = f" (~{share:.0%} of period gross)" if share is not None else ""
        if material:
            desc = (f"Orders shipped but not yet settled for {period}: ${amount:,.2f} across "
                    f"{count} orders{share_txt}. This is the normal end-of-period settlement "
                    "cutoff and reconciles next cycle, but at this share the reported margin is "
                    "slightly optimistic — caveat it.")
        else:
            desc = (f"Orders shipped but not yet settled for {period}: ${amount:,.2f} across "
                    f"{count} orders{share_txt}. Normal settlement cutoff; reconciles next cycle.")
        _emit(warnings, seen, DataQualityWarning(
            CODE_ORDERS_WITHOUT_PAYOUT, period, severity, amount, count, desc,
            {"period_int": pint, "share_of_gross": share, "period_gross": period_gross}))


def _unmapped_payout(
    df: pd.DataFrame | None,
    fp: FilePeriods,
    warnings: list[DataQualityWarning],
    seen: set[tuple[str, Period | None]],
    current: Period,
) -> None:
    """Count unmapped payout lines per period; header-only sheet → a clean zero."""
    if df is None:
        logger.warning("Optional sheet for %r absent — omitting that code.", CODE_UNMAPPED_PAYOUT)
        return
    totals = _per_period_totals(df, None)  # 21-col sheet: count lines (no canonical amount)
    if totals is None:
        return
    if not totals:  # the verified sample is header-only
        _emit(warnings, seen, DataQualityWarning(
            CODE_UNMAPPED_PAYOUT, current, DQSeverity.INFO, None, 0,
            f"No unmapped payout lines for {current} (sheet present but empty).",
            {"empty_sheet": True}))
        return
    for pint, _amount, count in totals:
        period = _map_period(pint, fp)
        if period is None:
            continue
        _emit(warnings, seen, DataQualityWarning(
            CODE_UNMAPPED_PAYOUT, period, DQSeverity.INFO, None, count,
            f"Unmapped payout lines for {period}: {count} payout line(s) could not be mapped.",
            {"period_int": pint}))


# ─────────────────────────────────────────────────────────────────────────────
# Channel-level caveats (Summary notes + bridge residual)
# ─────────────────────────────────────────────────────────────────────────────
def _channel_caveats(
    summary_by_period: dict[Period, dict[str, float | None]],
    comparison: ComparisonResult,
    current_period: Period,
    warnings: list[DataQualityWarning],
    seen: set[tuple[str, Period | None]],
) -> None:
    cur = summary_by_period.get(current_period) or {}

    # AD-cost mapping gap: SKU-level ad analysis covers only the mapped portion.
    total_ad = _num(cur.get(KEY_TOTAL_AD_COST))
    mapped_ad = _num(cur.get(KEY_AD_COST_MAPPED))
    if total_ad is not None and mapped_ad is not None:
        gap = abs(total_ad) - abs(mapped_ad)
        _emit(warnings, seen, DataQualityWarning(
            CODE_AD_COST_MAPPING_GAP, current_period, DQSeverity.CAUTION, gap, None,
            f"Channel ad-cost mapping gap for {current_period}: ${abs(total_ad):,.2f} total ad "
            f"cost vs ${abs(mapped_ad):,.2f} mapped to specific items — ${gap:,.2f} unmapped. "
            "SKU-level ad analysis covers only the mapped portion.",
            {"total_ad_cost": abs(total_ad), "mapped_ad_cost": abs(mapped_ad)}))
    else:
        logger.info("Channel ad-cost mapping gap skipped — note figures not present in summary.")

    # Unsettled referral fee: ties to orders-without-payout; margin biased high.
    unsettled_fee = _num(cur.get(KEY_UNSETTLED_REFERRAL_FEE))
    if unsettled_fee is not None:
        owp = next((w for w in warnings
                    if w.code == CODE_ORDERS_WITHOUT_PAYOUT and w.period == current_period), None)
        tie = f" tied to ${owp.amount:,.2f} of unsettled sales" if (owp and owp.amount) else ""
        _emit(warnings, seen, DataQualityWarning(
            CODE_UNSETTLED_REFERRAL_FEE, current_period, DQSeverity.CAUTION, unsettled_fee, None,
            f"Unsettled referral fee for {current_period}: est. {unsettled_fee:,.2f} of referral "
            f"fee on unsettled sales{tie}. Reported margin is biased slightly high until settlement.",
            {"unsettled_referral_fee": unsettled_fee,
             "orders_without_payout_gross": owp.amount if owp else None}))
    else:
        logger.info("Unsettled referral-fee caveat skipped — note figure not present in summary.")

    # YoY unallocated credit: surfaced, never used to adjust SKU figures.
    yoy_baseline = _yoy_baseline_period(comparison)
    if yoy_baseline is not None:
        credit = _num((summary_by_period.get(yoy_baseline) or {}).get(KEY_UNALLOCATED_CREDIT))
        if credit is not None:
            _emit(warnings, seen, DataQualityWarning(
                CODE_YOY_UNALLOCATED_CREDIT, yoy_baseline, DQSeverity.INFO, credit, None,
                f"Unallocated marketplace credit in {yoy_baseline}: ${abs(credit):,.2f} (in Total "
                "Other Expense) is not allocated to any SKU, so YoY-file SKU sums tie to the "
                "Summary within this amount. Disclosed; SKU figures are not adjusted.",
                {"unallocated_credit": credit}))
        else:
            logger.info("YoY unallocated-credit caveat skipped — note figure not present in summary.")

    # YoY cost-bridge residual: disclose the non-reconciling residual as a caveat.
    cb = comparison.cost_bridge_yoy
    if cb is not None and not cb.reconciles:
        _emit(warnings, seen, DataQualityWarning(
            CODE_YOY_BRIDGE_RESIDUAL, None, DQSeverity.CAUTION, cb.residual, None,
            f"YoY cost bridge did not reconcile: residual ${cb.residual:,.2f} exceeds the "
            f"${cb.threshold:,.2f} tolerance (a Summary-level data-quality artifact). Disclosed, "
            "not adjusted — the SKU-level profit decomposition remains as reported.",
            {"residual": cb.residual, "threshold": cb.threshold,
             "profit_change": cb.profit_change}))


def _yoy_baseline_period(comparison: ComparisonResult) -> Period | None:
    """The YoY baseline period from whichever YoY bridge is present, else None."""
    for bridge in (comparison.revenue_bridge_yoy, comparison.cost_bridge_yoy):
        if bridge is not None:
            return bridge.baseline_period
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Assembly + logging
# ─────────────────────────────────────────────────────────────────────────────
def _assemble(current_period: Period, warnings: list[DataQualityWarning]) -> DataQualityReport:
    by_code: dict[str, list[DataQualityWarning]] = defaultdict(list)
    for w in warnings:
        by_code[w.code].append(w)
    return DataQualityReport(current_period=current_period, warnings=warnings, by_code=dict(by_code))


def _log_summary(report: DataQualityReport) -> None:
    counts = {code: len(ws) for code, ws in report.by_code.items()}
    logger.info(
        "Data quality %s: %d warning(s) across %d code(s) %s.",
        report.current_period, len(report.warnings), len(report.by_code), counts,
    )
