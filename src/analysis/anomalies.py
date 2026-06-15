"""Deterministic, rules-based anomaly detection.

Analysis layer — file 2 of 3. This module reads the upstream *facts* — the
per-SKU metrics + thresholds (``sku_metrics.py``) and the cross-period
deltas/bridges/structural-movers (``comparisons.py``) — and emits a structured
list of flagged conditions for the report layer to cite.

Every rule here is explicit Python with a documented threshold (§0): there is no
prompt and no model call. The flags are *evidence*; the external Claude skill
later interprets them, it does not generate them. Two flags on the same SKU is
fine — they are different framings of the same SKU and are grouped per-SKU on
the way out.

**Reuse, don't recompute.** The segment thresholds, the structural-mover
divergence, and the PauseAds condition already exist upstream and are consumed
here as-is. Re-deriving any of them would create a second source of truth that
could drift from the transform/comparison layers.

**Materiality gate.** ``max($100, 1% of current-period profit)`` — a *computed*
value, not a hardcoded 100 (for April 2026, profit $7,595.09 → 1% = $75.95 →
gate = $100). Trend / profit-movement rules (A–E, partly I) must clear the gate.
The one deliberate exception is rule G (data integrity), flagged regardless of
dollar size: a tiny per-unit cost-setup error (e.g. freight > price, a $22.22
loss) recurs every month and signals a systemic mistake, not a demand signal.

Scope note: this file does NOT parse the data-quality sheets (unmapped ads,
canceled shipping, orders-without-payout) — that is the next file. Anomalies
here are derived only from metrics and comparisons.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from src.analysis.comparisons import SKU, ComparisonResult
from src.ingest.period_parser import Period
from src.transform.normalize_tiktok import GROSS_COLUMN, KEY_COLUMN
from src.transform.sku_metrics import SkuMetricsResult
from src.utils.logger import get_logger

logger = get_logger(__name__)

CHANNEL = "channel"

# Materiality gate parameters (the gate itself is computed, never hardcoded).
MIN_GATE = 100.0
GATE_PROFIT_FRACTION = 0.01

# Rule E3: a SKU taking this share or more of the channel's total SKU ad spend
# *while declining* is an ad-concentration risk. April's FG-3BLAH-4P2 absorbed
# ~half of SKU ad spend while down on both lenses.
AD_SHARE_THRESHOLD = 0.20

# Rule H: "single-digit" units → a small-sample caution. Not an anomaly on its
# own; only attached when the SKU already carries another flag.
LOW_VOLUME_UNITS = 10

# Rule G: per-SKU logistics / fulfillment cost lines whose *magnitude* exceeding
# the SKU's gross sale is implausible — it points at a per-unit cost-table setup
# error (freight / customs / shipping miskeyed) that recurs monthly. COGS and
# the percentage-based fees (referral / affiliate / ad) are deliberately
# EXCLUDED: COGS > gross is a pricing/margin question (rule F), and fee lines
# scale with revenue by design, so a large one is not a setup error.
G_COST_COMPONENTS = (
    "Total Ocean Freight Cost",
    "Total Customs",
    "Total Tiktok Shipping cost",
    "Total Order ShippingEasy Cost",
    "Total ShippingEasy Supply Cost",
    "Total Returned Shipping Cost",
)


# ─────────────────────────────────────────────────────────────────────────────
# Enums (small, documented — not free strings)
# ─────────────────────────────────────────────────────────────────────────────
class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(str, Enum):
    TREND = "trend"            # period-over-period profit/gross movement
    AD = "ad"                  # ad efficiency / spend concentration
    MARGIN = "margin"          # margin state or cost/fee erosion
    DATA_INTEGRITY = "data_integrity"  # routes to operations, not merchandising
    CAUTION = "caution"        # interpret-with-care (e.g. tiny sample)


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    DIVERGING = "diverging"    # the two lenses disagree


# ─────────────────────────────────────────────────────────────────────────────
# Output containers
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AnomalyFlag:
    """One flagged condition, carrying enough raw numbers to cite as evidence.

    ``scope`` is a Marketplace SKU id or the literal ``"channel"``. ``evidence``
    holds the supporting numbers (and a few labels, e.g. which lens / which cost
    component) so the report can quote them without re-querying upstream.
    """

    rule_id: str
    scope: str
    category: Category
    severity: Severity
    reason: str
    evidence: dict[str, object]
    direction: Direction | None = None


@dataclass(frozen=True)
class AnomalyReport:
    current_period: Period
    flags: list[AnomalyFlag]
    by_sku: dict[str, list[AnomalyFlag]] = field(default_factory=dict)
    by_category: dict[Category, list[AnomalyFlag]] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Gate + small helpers
# ─────────────────────────────────────────────────────────────────────────────
def materiality_gate(current_period_profit: float) -> float:
    """``max($100, 1% of |current-period profit|)`` — computed, never hardcoded."""
    return max(MIN_GATE, GATE_PROFIT_FRACTION * abs(float(current_period_profit)))


def _num(value: object) -> float | None:
    """Coerce a cell to float; None/NaN/NA → None (a genuinely missing lens)."""
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
def detect_anomalies(
    metrics_result: SkuMetricsResult,
    comparison: ComparisonResult,
    current_period_profit: float,
    *,
    cost_detail: pd.DataFrame | None = None,
    trailing: pd.DataFrame | None = None,
) -> AnomalyReport:
    """Run every anomaly rule and return a grouped ``AnomalyReport``.

    Inputs (reuse upstream facts; do not recompute them):
      * ``metrics_result`` — per-SKU metrics + the segment thresholds.
      * ``comparison`` — dual deltas, revenue/cost bridges, structural movers.
      * ``current_period_profit`` — drives the materiality gate.
      * ``cost_detail`` (optional) — per-SKU raw cost-line frame for the current
        period (``KEY_COLUMN`` + the ``G_COST_COMPONENTS`` columns + gross),
        used ONLY by rule G. Absent → rule G skips cleanly (mirrors rule I).
      * ``trailing`` (optional) — per-SKU trailing baselines for rule I
        (``KEY_COLUMN``/``marketplace_sku`` + ``trailing_margin_pct`` +
        ``trailing_ad_pct``). None → rule I no-ops with a logged note; we do not
        fabricate a baseline.

    Rules degrade gracefully when a lens is missing: anything needing the absent
    lens is skipped rather than guessed.
    """
    period = metrics_result.period
    gate = materiality_gate(current_period_profit)
    metrics = metrics_result.metrics
    thresholds = metrics_result.thresholds
    deltas = comparison.sku_deltas

    has_mom = comparison.revenue_bridge_mom is not None or comparison.cost_bridge_mom is not None
    has_yoy = comparison.revenue_bridge_yoy is not None or comparison.cost_bridge_yoy is not None

    flags: list[AnomalyFlag] = []

    if metrics.empty and deltas.empty:
        logger.warning("No metrics or deltas for %s — empty anomaly report.", period)
        return _assemble(period, flags)

    # Trend rules read the delta union (covers lapsed SKUs absent from metrics).
    _rules_trend(flags, deltas, gate, has_mom, has_yoy)                     # A, C
    _rule_b_divergence(flags, comparison.structural_movers, gate)           # B
    _rule_d_revenue_flat_profit_down(flags, deltas, comparison, gate, has_mom, has_yoy)  # D
    _rule_e_ad_efficiency(flags, metrics, deltas, comparison, gate, has_mom, has_yoy)    # E
    _rule_f_high_gross_low_margin(flags, metrics, thresholds)              # F
    _rule_g_cost_setup_error(flags, cost_detail, period)                  # G
    _rule_i_vs_trailing(flags, metrics, trailing, gate)                   # I
    _rule_h_low_volume(flags, metrics, deltas)                            # H (attach-only)

    report = _assemble(period, flags)
    _log_summary(period, gate, report)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Rule A (confirmed decline) + Rule C (quiet YoY bleeder)
# ─────────────────────────────────────────────────────────────────────────────
def _rules_trend(
    flags: list[AnomalyFlag],
    deltas: pd.DataFrame,
    gate: float,
    has_mom: bool,
    has_yoy: bool,
) -> None:
    """A and C both need both lenses; skip cleanly when either is missing."""
    if not (has_mom and has_yoy) or deltas.empty:
        return

    for _, row in deltas.iterrows():
        sku = str(row[SKU])
        pm = _num(row.get("profit_delta_mom"))
        py = _num(row.get("profit_delta_yoy"))
        if pm is None or py is None:
            continue
        ev = {"profit_delta_mom": pm, "profit_delta_yoy": py, "gate": gate}

        # A — both lenses materially down: a real slide, not a soft month.
        if pm <= -gate and py <= -gate:
            flags.append(AnomalyFlag(
                rule_id="A", scope=sku, category=Category.TREND, severity=Severity.HIGH,
                direction=Direction.DOWN,
                reason=(f"Confirmed decline: profit down both MoM ({pm:,.2f}) and "
                        f"YoY ({py:,.2f}), clearing the ${gate:,.2f} gate."),
                evidence=ev,
            ))
        # C — MoM flat/positive but materially down YoY: a month-only view misses it.
        elif pm > -gate and py <= -gate:
            flags.append(AnomalyFlag(
                rule_id="C", scope=sku, category=Category.TREND, severity=Severity.HIGH,
                direction=Direction.DOWN,
                reason=(f"Quiet YoY bleeder: roughly flat/up MoM ({pm:,.2f}) but "
                        f"materially down YoY ({py:,.2f}); a month-only read misses it."),
                evidence=ev,
            ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule B (seasonal / volatility divergence) — reuses structural movers
# ─────────────────────────────────────────────────────────────────────────────
def _rule_b_divergence(flags: list[AnomalyFlag], movers: pd.DataFrame, gate: float) -> None:
    """Reuse comparisons' structural-movers view verbatim (same gate, same divergence)."""
    if movers is None or movers.empty:
        return
    for _, row in movers.iterrows():
        sku = str(row[SKU])
        pm = _num(row.get("profit_delta_mom"))
        py = _num(row.get("profit_delta_yoy"))
        div = _num(row.get("divergence"))
        flags.append(AnomalyFlag(
            rule_id="B", scope=sku, category=Category.TREND, severity=Severity.MEDIUM,
            direction=Direction.DIVERGING,
            reason=(f"Lens divergence: MoM ({pm:,.2f}) and YoY ({py:,.2f}) disagree "
                    f"in direction (divergence {div:,.2f}); don't trust a single-lens read."),
            evidence={"profit_delta_mom": pm, "profit_delta_yoy": py, "divergence": div},
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule D (profit down while revenue flat/up) — cost/fee erosion, not demand
# ─────────────────────────────────────────────────────────────────────────────
def _rule_d_revenue_flat_profit_down(
    flags: list[AnomalyFlag],
    deltas: pd.DataFrame,
    comparison: ComparisonResult,
    gate: float,
    has_mom: bool,
    has_yoy: bool,
) -> None:
    lenses = []
    if has_mom:
        lenses.append(("mom", comparison.revenue_bridge_mom, comparison.cost_bridge_mom))
    if has_yoy:
        lenses.append(("yoy", comparison.revenue_bridge_yoy, comparison.cost_bridge_yoy))

    # Channel scope: total gross flat/up while total profit materially down.
    for lens, rev, cost in lenses:
        if rev is None or cost is None:
            continue
        if cost.profit_change <= -gate and rev.total_change >= -gate:
            flags.append(AnomalyFlag(
                rule_id="D", scope=CHANNEL, category=Category.MARGIN, severity=Severity.MEDIUM,
                direction=Direction.DOWN,
                reason=(f"Channel profit down {cost.profit_change:,.2f} ({lens.upper()}) "
                        f"while gross flat/up ({rev.total_change:+,.2f}) — cost/fee erosion, "
                        "not demand."),
                evidence={"lens": lens, "profit_change": cost.profit_change,
                          "gross_change": rev.total_change, "gate": gate},
            ))

    # SKU scope: per lens, profit down clearing the gate while gross flat/up.
    if deltas.empty:
        return
    for _, row in deltas.iterrows():
        sku = str(row[SKU])
        for lens, present in (("mom", has_mom), ("yoy", has_yoy)):
            if not present:
                continue
            pdelta = _num(row.get(f"profit_delta_{lens}"))
            gdelta = _num(row.get(f"gross_delta_{lens}"))
            if pdelta is None or gdelta is None:
                continue
            if pdelta <= -gate and gdelta >= -gate:
                flags.append(AnomalyFlag(
                    rule_id="D", scope=sku, category=Category.MARGIN, severity=Severity.MEDIUM,
                    direction=Direction.DOWN,
                    reason=(f"Profit down {pdelta:,.2f} ({lens.upper()}) while gross "
                            f"flat/up ({gdelta:+,.2f}) — margin erosion, not lost demand."),
                    evidence={"lens": lens, "profit_delta": pdelta, "gross_delta": gdelta,
                              "gate": gate},
                ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule E (ad-efficiency risk)
# ─────────────────────────────────────────────────────────────────────────────
def _materially_down(row: pd.Series, gate: float, has_mom: bool, has_yoy: bool) -> bool:
    """True if profit is materially down on every *present* lens (rule-A shape)."""
    downs = []
    for lens, present in (("mom", has_mom), ("yoy", has_yoy)):
        if not present:
            continue
        d = _num(row.get(f"profit_delta_{lens}"))
        if d is None:
            return False
        downs.append(d <= -gate)
    return bool(downs) and all(downs)


def _rule_e_ad_efficiency(
    flags: list[AnomalyFlag],
    metrics: pd.DataFrame,
    deltas: pd.DataFrame,
    comparison: ComparisonResult,
    gate: float,
    has_mom: bool,
    has_yoy: bool,
) -> None:
    # E1 — channel: ad spend up (ad cost more negative) while profit down.
    for lens, cost in (("mom", comparison.cost_bridge_mom), ("yoy", comparison.cost_bridge_yoy)):
        if cost is None:
            continue
        ad_delta = cost.line_deltas.get("AD Cost")
        if ad_delta is None:
            continue
        if ad_delta <= -gate and cost.profit_change <= -gate:  # spend up & profit down
            flags.append(AnomalyFlag(
                rule_id="E", scope=CHANNEL, category=Category.AD, severity=Severity.MEDIUM,
                direction=Direction.DOWN,
                reason=(f"Channel ad spend up {(-ad_delta):,.2f} ({lens.upper()}) while "
                        f"profit down {cost.profit_change:,.2f} — ad efficiency eroding."),
                evidence={"lens": lens, "ad_cost_delta": ad_delta,
                          "profit_change": cost.profit_change, "gate": gate},
            ))

    if metrics.empty:
        return
    d_by_sku = deltas.set_index(SKU) if not deltas.empty else None
    total_ad = float(metrics["ad_spend"].sum())

    for _, m in metrics.iterrows():
        sku = str(m[KEY_COLUMN])

        # E2 — PauseAds: profit-before-ads > 0 but profit <= 0 (reuse the segment).
        if m.get("segment") == "PauseAds":
            flags.append(AnomalyFlag(
                rule_id="E", scope=sku, category=Category.AD, severity=Severity.HIGH,
                direction=Direction.DOWN,
                reason=(f"PauseAds: profitable before ads ({m['profit_before_ads']:,.2f}) "
                        f"but net profit {m['profit']:,.2f} after ${m['ad_spend']:,.2f} ad "
                        "spend — the ads are erasing the margin."),
                evidence={"profit_before_ads": float(m["profit_before_ads"]),
                          "profit": float(m["profit"]), "ad_spend": float(m["ad_spend"])},
            ))

        # E3 — ad concentration: a large share of channel ad spend on a declining SKU.
        if total_ad > 0:
            share = float(m["ad_spend"]) / total_ad
            drow = d_by_sku.loc[sku] if (d_by_sku is not None and sku in d_by_sku.index) else None
            declining = drow is not None and _materially_down(drow, gate, has_mom, has_yoy)
            if share >= AD_SHARE_THRESHOLD and declining:
                flags.append(AnomalyFlag(
                    rule_id="E", scope=sku, category=Category.AD, severity=Severity.HIGH,
                    direction=Direction.DOWN,
                    reason=(f"Ad concentration: {share:.0%} of channel SKU ad spend "
                            f"(${m['ad_spend']:,.2f}) on a SKU declining on every lens."),
                    evidence={"ad_spend": float(m["ad_spend"]), "ad_share": share,
                              "total_ad_spend": total_ad, "gate": gate},
                ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule F (high revenue, low/negative margin) — reuse the Fix-segment thresholds
# ─────────────────────────────────────────────────────────────────────────────
def _rule_f_high_gross_low_margin(flags: list[AnomalyFlag], metrics: pd.DataFrame, thresholds) -> None:
    if metrics.empty:
        return
    for _, m in metrics.iterrows():
        gross = float(m["gross"])
        margin = _num(m.get("profit_margin_pct"))
        if margin is None:  # undefined margin (gross 0) never matches a margin rule
            continue
        if gross >= thresholds.gross_p75 and margin < thresholds.margin_p25:
            severity = Severity.HIGH if (margin < 0 or float(m["profit"]) <= 0) else Severity.MEDIUM
            flags.append(AnomalyFlag(
                rule_id="F", scope=str(m[KEY_COLUMN]), category=Category.MARGIN, severity=severity,
                reason=(f"High revenue (${gross:,.2f} ≥ p75 ${thresholds.gross_p75:,.2f}) but "
                        f"low margin ({margin:.2f}% < p25 {thresholds.margin_p25:.2f}%) — Fix."),
                evidence={"gross": gross, "margin_pct": margin, "profit": float(m["profit"]),
                          "gross_p75": thresholds.gross_p75, "margin_p25": thresholds.margin_p25},
            ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule G (data-integrity / cost-setup error) — size-independent
# ─────────────────────────────────────────────────────────────────────────────
def _rule_g_cost_setup_error(
    flags: list[AnomalyFlag], cost_detail: pd.DataFrame | None, period: Period
) -> None:
    if cost_detail is None:
        logger.info("Rule G skipped for %s — no per-SKU cost detail supplied.", period)
        return
    if "period" in cost_detail.columns:
        cost_detail = cost_detail[cost_detail["period"] == period]
    components = [c for c in G_COST_COMPONENTS if c in cost_detail.columns]
    if not components:
        logger.info("Rule G skipped for %s — no recognized cost components present.", period)
        return

    for _, row in cost_detail.iterrows():
        gross = _num(row.get(GROSS_COLUMN))
        if gross is None or gross <= 0:
            continue
        sku = str(row[KEY_COLUMN])
        for comp in components:
            cost = _num(row.get(comp))
            if cost is None:
                continue
            if abs(cost) > gross:  # a single cost line exceeds the whole sale
                flags.append(AnomalyFlag(
                    rule_id="G", scope=sku, category=Category.DATA_INTEGRITY,
                    severity=Severity.MEDIUM,
                    reason=(f"Cost-setup error: {comp} ({cost:,.2f}) exceeds gross sale "
                            f"(${gross:,.2f}) — likely a per-unit cost miskeyed. Flagged "
                            "regardless of dollar size (recurs monthly; route to operations)."),
                    evidence={"component": comp, "cost": cost, "gross": gross,
                              "ratio_to_gross": abs(cost) / gross},
                ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule I (vs trailing history) — guarded; no-ops without history
# ─────────────────────────────────────────────────────────────────────────────
def _rule_i_vs_trailing(
    flags: list[AnomalyFlag], metrics: pd.DataFrame, trailing: pd.DataFrame | None, gate: float
) -> None:
    if trailing is None:
        logger.info("Rule I skipped — no trailing history available (need ≥1 prior period).")
        return
    if metrics.empty or trailing.empty:
        return

    key = KEY_COLUMN if KEY_COLUMN in trailing.columns else SKU
    t = trailing.set_index(key)
    for _, m in metrics.iterrows():
        sku = str(m[KEY_COLUMN])
        if sku not in t.index:
            continue
        gross = float(m["gross"])
        if gross < gate:  # don't flag tiny SKUs on a rate comparison
            continue
        trow = t.loc[sku]

        tmargin = _num(trow.get("trailing_margin_pct"))
        margin = _num(m.get("profit_margin_pct"))
        if tmargin is not None and margin is not None and margin < tmargin:
            flags.append(AnomalyFlag(
                rule_id="I", scope=sku, category=Category.MARGIN, severity=Severity.MEDIUM,
                direction=Direction.DOWN,
                reason=(f"Margin {margin:.2f}% below trailing average {tmargin:.2f}% — "
                        "deteriorating versus its own recent history."),
                evidence={"margin_pct": margin, "trailing_margin_pct": tmargin},
            ))

        t_ad_pct = _num(trow.get("trailing_ad_pct"))
        ad_pct = (float(m["ad_spend"]) / gross * 100.0) if gross > 0 else None
        if t_ad_pct is not None and ad_pct is not None and ad_pct > t_ad_pct:
            flags.append(AnomalyFlag(
                rule_id="I", scope=sku, category=Category.AD, severity=Severity.MEDIUM,
                direction=Direction.UP,
                reason=(f"Ad spend {ad_pct:.2f}% of revenue above trailing average "
                        f"{t_ad_pct:.2f}% — ad intensity creeping up."),
                evidence={"ad_pct": ad_pct, "trailing_ad_pct": t_ad_pct},
            ))


# ─────────────────────────────────────────────────────────────────────────────
# Rule H (low-volume caution) — attach-only, never standalone noise
# ─────────────────────────────────────────────────────────────────────────────
def _rule_h_low_volume(flags: list[AnomalyFlag], metrics: pd.DataFrame, deltas: pd.DataFrame) -> None:
    flagged_skus = {f.scope for f in flags if f.scope != CHANNEL}
    if not flagged_skus:
        return

    units_by_sku: dict[str, float] = {}
    if not metrics.empty:
        for _, m in metrics.iterrows():
            units_by_sku[str(m[KEY_COLUMN])] = _num(m.get("units")) or 0.0
    if not deltas.empty:
        for _, row in deltas.iterrows():
            units_by_sku.setdefault(str(row[SKU]), _num(row.get("current_units")) or 0.0)

    cautions: list[AnomalyFlag] = []
    for sku in flagged_skus:
        units = units_by_sku.get(sku)
        if units is not None and 0 < units < LOW_VOLUME_UNITS:
            cautions.append(AnomalyFlag(
                rule_id="H", scope=sku, category=Category.CAUTION, severity=Severity.LOW,
                reason=(f"Low volume ({units:.0f} units): treat this SKU's other flags as a "
                        "small-sample signal, not a trend."),
                evidence={"units": units, "threshold": LOW_VOLUME_UNITS},
            ))
    flags.extend(cautions)


# ─────────────────────────────────────────────────────────────────────────────
# Assembly + logging
# ─────────────────────────────────────────────────────────────────────────────
def _assemble(period: Period, flags: list[AnomalyFlag]) -> AnomalyReport:
    by_sku: dict[str, list[AnomalyFlag]] = defaultdict(list)
    by_category: dict[Category, list[AnomalyFlag]] = defaultdict(list)
    for f in flags:
        by_sku[f.scope].append(f)
        by_category[f.category].append(f)
    return AnomalyReport(
        current_period=period,
        flags=flags,
        by_sku=dict(by_sku),
        by_category=dict(by_category),
    )


def _log_summary(period: Period, gate: float, report: AnomalyReport) -> None:
    by_cat = {c.value: len(report.by_category.get(c, [])) for c in Category}
    by_sev: dict[str, int] = defaultdict(int)
    for f in report.flags:
        by_sev[f.severity.value] += 1
    logger.info(
        "Anomalies %s: %d flag(s) over gate $%.2f | by category=%s | by severity=%s",
        period, len(report.flags), gate, by_cat, dict(by_sev),
    )
