"""Cross-period comparisons — where the two workbooks finally meet.

Analysis layer — file 1 of 3. Given the current period's facts and one or two
comparison baselines (MoM = prior month, YoY = same month last year), this
computes:

  * per-SKU dual deltas (profit/gross/units, MoM and YoY),
  * the channel-level **revenue bridge** (volume / price / new / lapsed),
  * the channel-level **cost bridge** (profit change decomposed across money
    lines), and
  * the **structural-mover** view (SKUs whose MoM and YoY lenses disagree).

This is the most numerically sensitive file in the pipeline; its methods are
verified to reproduce the real April-2026 bridges to a $0.00 revenue residual
and the disclosed cost residuals ($0.18 MoM, ~$299.70 YoY).

The anchor (AGENTS.md §5): the current period (April 2026) appears in BOTH files
and must be identical across them. Before computing anything, ``compare`` asserts
the current-period totals (gross / profit / units) agree across the supplied
sources within a tiny epsilon; a mismatch raises, because it would mean the two
lenses are not describing the same month. The MoM file supplies the March
baseline; the YoY file supplies the April-2025 baseline.

Disclose-don't-force (AGENTS.md §4): every bridge carries its own ``residual``
and a ``reconciles`` flag. Nothing is nudged to zero — the known ~$299.70 YoY
cost-bridge residual (an April-2025-file data-quality artifact) is reported and
flagged, not engineered away.

Scope: no segmentation (transform did it), no anomaly flags (next file), no
data-quality sheet parsing (next file), no package serialization. Raw signs are
preserved throughout (costs negative).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.ingest.period_parser import Period
from src.transform.normalize_tiktok import (
    GROSS_COLUMN,
    KEY_COLUMN,
    PROFIT_COLUMN,
    UNITS_COLUMN,
    NormalizedWorkbook,
    active_skus,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Standardized per-SKU column name used inside this layer.
SKU = "marketplace_sku"

# Anchor / revenue-bridge tolerances. The revenue bridge reconciles exactly by
# construction (algebra below), so a tight epsilon catches only float noise.
ANCHOR_EPS = 0.01
REVENUE_RESIDUAL_EPS = 0.01

# Cost-bridge reconcile threshold: |residual| above this sets reconciles=False so
# the data-quality layer can surface it. $1.00 by default (configurable).
COST_BRIDGE_RESIDUAL_THRESHOLD = 1.00

# Profit total = sum of these money lines (PROJECT_CONTEXT §5). Counts (units /
# orders) and Total Profit itself are excluded. Label → Summary line-item key.
COST_BRIDGE_LINES: "OrderedDict[str, str]" = OrderedDict(
    [
        ("Gross Sale", "Total Gross Sale"),
        ("Refund", "Total Refund"),
        ("Tiktok Shipping", "Total Tiktok Shipping cost"),
        ("Referral Fee", "Total Referral Fee"),
        ("Affiliate commission", "Total Affiliate commission"),
        ("Refund admin fee", "Refund administration fee"),
        ("Affiliate Shop Ads commission", "Affiliate Shop Ads commission"),
        ("Co-funded promo fee", "Co-funded promotion service fee"),
        ("Campaign fee", "Campaign service fee"),
        ("AD Cost", "Total AD Cost"),
        ("Order ShippingEasy", "Total Order ShippingEasy Cost"),
        ("ShippingEasy Supply", "Total ShippingEasy Supply Cost"),
        ("Returned Shipping", "Total Returned Shipping Cost"),
        ("Other Expense", "Total Other Expense"),
        ("COGS", "Total Cost of Goods Sold"),
        ("Ocean Freight", "Total Ocean Freight Cost"),
        ("Customs", "Total Customs"),
    ]
)
_PROFIT_LINE = "Total Profit"


# ─────────────────────────────────────────────────────────────────────────────
# Inputs / outputs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PeriodData:
    """One period's comparison inputs: the active SKU rows + the channel summary.

    ``sku`` is the active set (units > 0) with columns ``marketplace_sku``,
    ``units``, ``gross``, ``profit``. ``summary`` maps Summary line item → value.
    """

    period: Period
    sku: pd.DataFrame
    summary: dict[str, float | None]


@dataclass(frozen=True)
class RevenueBridge:
    """Gross-change decomposition for one baseline → current pair."""

    baseline_period: Period
    current_period: Period
    total_change: float
    volume_effect: float
    price_effect: float
    new_skus_effect: float
    lapsed_skus_effect: float
    residual: float
    reconciles: bool


@dataclass(frozen=True)
class CostBridge:
    """Profit-change decomposition across the channel money lines."""

    baseline_period: Period
    current_period: Period
    profit_change: float
    line_deltas: dict[str, float]
    sum_of_line_deltas: float
    residual: float
    reconciles: bool
    threshold: float


@dataclass(frozen=True)
class ComparisonResult:
    current_period: Period
    sku_deltas: pd.DataFrame
    revenue_bridge_mom: RevenueBridge | None
    revenue_bridge_yoy: RevenueBridge | None
    cost_bridge_mom: CostBridge | None
    cost_bridge_yoy: CostBridge | None
    structural_movers: pd.DataFrame


# ─────────────────────────────────────────────────────────────────────────────
# Convenience constructors (used by main.py; tests can build PeriodData directly)
# ─────────────────────────────────────────────────────────────────────────────
def period_data_from_normalized(normalized: NormalizedWorkbook, period: Period) -> PeriodData:
    """Build a ``PeriodData`` (active SKUs + summary) for one period of a file."""
    active = active_skus(normalized)
    sub = active[active["period"] == period]
    sku = pd.DataFrame(
        {
            SKU: sub[KEY_COLUMN].astype(str).to_numpy(),
            "units": sub[UNITS_COLUMN].to_numpy(),
            "gross": sub[GROSS_COLUMN].to_numpy(),
            "profit": sub[PROFIT_COLUMN].to_numpy(),
        }
    )
    return PeriodData(period=period, sku=sku, summary=normalized.summary.get(period, {}))


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _totals(sku: pd.DataFrame) -> tuple[float, float, float]:
    """(gross, profit, units) totals for an active SKU frame."""
    return (
        float(sku["gross"].sum()),
        float(sku["profit"].sum()),
        float(sku["units"].sum()),
    )


def _val(summary: dict[str, float | None], key: str) -> float:
    """Summary value as float; missing or None → 0.0."""
    value = summary.get(key)
    return 0.0 if value is None else float(value)


# ─────────────────────────────────────────────────────────────────────────────
# Anchor
# ─────────────────────────────────────────────────────────────────────────────
def _assert_anchor(current: PeriodData, views: list[tuple[str, PeriodData]]) -> None:
    """Assert each source's current-period totals match ``current`` within epsilon."""
    cg, cp, cu = _totals(current.sku)
    for label, view in views:
        g, p, u = _totals(view.sku)
        gaps = {"gross": g - cg, "profit": p - cp, "units": u - cu}
        if any(abs(gap) > ANCHOR_EPS for gap in gaps.values()):
            raise ValueError(
                f"Anchor mismatch: the current period as seen in {label!r} does "
                f"not match the canonical current period. Gaps {gaps}. The two "
                "lenses must describe the same month before comparison."
            )
    logger.info(
        "Anchor OK: current period %s agrees across %d source(s) "
        "(gross=%.2f profit=%.2f units=%.0f).",
        current.period,
        len(views),
        cg,
        cp,
        cu,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Revenue bridge
# ─────────────────────────────────────────────────────────────────────────────
def revenue_bridge(baseline: PeriodData, current: PeriodData) -> RevenueBridge:
    """Decompose the gross change baseline → current into volume/price/new/lapsed.

    For continuing SKUs (active in both): ``volume = (u2-u1)*p1`` and
    ``price = (p2-p1)*u2`` where ``p1 = g1/u1``, ``p2 = g2/u2``. New SKUs add
    ``+g2``; lapsed SKUs subtract ``-g1``. By construction these sum exactly to
    the total gross change (residual ~ float noise).
    """
    b = baseline.sku.set_index(SKU)[["units", "gross"]]
    c = current.sku.set_index(SKU)[["units", "gross"]]

    cont = b.join(c, lsuffix="_1", rsuffix="_2", how="inner")
    # Active means units > 0 in both, so the per-unit divisions are safe; guard
    # anyway against any zero-unit row that slipped through.
    safe = (cont["units_1"] > 0) & (cont["units_2"] > 0)
    cont = cont[safe]
    p1 = cont["gross_1"] / cont["units_1"]
    p2 = cont["gross_2"] / cont["units_2"]
    volume_effect = float(((cont["units_2"] - cont["units_1"]) * p1).sum())
    price_effect = float(((p2 - p1) * cont["units_2"]).sum())

    new_idx = c.index.difference(b.index)
    lapsed_idx = b.index.difference(c.index)
    new_effect = float(c.loc[new_idx, "gross"].sum())
    lapsed_effect = -float(b.loc[lapsed_idx, "gross"].sum())

    total_change = float(c["gross"].sum()) - float(b["gross"].sum())
    residual = total_change - (volume_effect + price_effect + new_effect + lapsed_effect)
    reconciles = abs(residual) <= REVENUE_RESIDUAL_EPS

    logger.info(
        "Revenue bridge %s→%s: total=%.2f | volume=%.2f price=%.2f new=%.2f "
        "lapsed=%.2f | residual=%.2f reconciles=%s",
        baseline.period,
        current.period,
        total_change,
        volume_effect,
        price_effect,
        new_effect,
        lapsed_effect,
        residual,
        reconciles,
    )
    return RevenueBridge(
        baseline_period=baseline.period,
        current_period=current.period,
        total_change=total_change,
        volume_effect=volume_effect,
        price_effect=price_effect,
        new_skus_effect=new_effect,
        lapsed_skus_effect=lapsed_effect,
        residual=residual,
        reconciles=reconciles,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cost bridge
# ─────────────────────────────────────────────────────────────────────────────
def cost_bridge(
    baseline: PeriodData,
    current: PeriodData,
    threshold: float = COST_BRIDGE_RESIDUAL_THRESHOLD,
) -> CostBridge:
    """Decompose the profit change across the channel money lines.

    ``profit_change`` is the Total Profit delta; ``line_deltas`` are the per-line
    current-minus-baseline deltas. Their sum should ≈ the profit change. The
    ``residual`` (profit_change − sum_of_line_deltas) is reported as-is and
    ``reconciles`` is set from ``|residual| <= threshold`` — never forced to zero.
    """
    profit_change = _val(current.summary, _PROFIT_LINE) - _val(baseline.summary, _PROFIT_LINE)
    line_deltas = {
        label: _val(current.summary, key) - _val(baseline.summary, key)
        for label, key in COST_BRIDGE_LINES.items()
    }
    sum_deltas = float(sum(line_deltas.values()))
    residual = profit_change - sum_deltas
    reconciles = abs(residual) <= threshold

    logger.info(
        "Cost bridge %s→%s: profit_change=%.2f sum_of_line_deltas=%.2f "
        "residual=%.2f reconciles=%s (threshold=%.2f)",
        baseline.period,
        current.period,
        profit_change,
        sum_deltas,
        residual,
        reconciles,
        threshold,
    )
    if not reconciles:
        logger.warning(
            "Cost bridge %s→%s residual %.2f exceeds threshold %.2f — disclosed "
            "as a data-quality caveat, not adjusted.",
            baseline.period,
            current.period,
            residual,
            threshold,
        )
    return CostBridge(
        baseline_period=baseline.period,
        current_period=current.period,
        profit_change=profit_change,
        line_deltas=line_deltas,
        sum_of_line_deltas=sum_deltas,
        residual=residual,
        reconciles=reconciles,
        threshold=threshold,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-SKU dual deltas
# ─────────────────────────────────────────────────────────────────────────────
def _lens_deltas(out: pd.DataFrame, current: PeriodData, baseline: PeriodData | None, lens: str) -> None:
    """Add ``*_delta_<lens>`` and ``status_<lens>`` columns to ``out`` in place."""
    p, g, u, s = (f"profit_delta_{lens}", f"gross_delta_{lens}",
                  f"units_delta_{lens}", f"status_{lens}")
    if baseline is None:
        out[p] = pd.NA
        out[g] = pd.NA
        out[u] = pd.NA
        out[s] = None
        return

    cur = current.sku.set_index(SKU)[["units", "gross", "profit"]].reindex(out.index)
    base = baseline.sku.set_index(SKU)[["units", "gross", "profit"]].reindex(out.index)
    cur0, base0 = cur.fillna(0.0), base.fillna(0.0)

    out[p] = cur0["profit"] - base0["profit"]
    out[g] = cur0["gross"] - base0["gross"]
    out[u] = cur0["units"] - base0["units"]

    in_cur = out.index.isin(current.sku[SKU])
    in_base = out.index.isin(baseline.sku[SKU])
    out[s] = np.select(
        [in_cur & in_base, in_cur & ~in_base, ~in_cur & in_base],
        ["continuing", "new", "lapsed"],
        default="absent",
    )


def _sku_deltas(
    current: PeriodData,
    mom_baseline: PeriodData | None,
    yoy_baseline: PeriodData | None,
) -> pd.DataFrame:
    all_skus = pd.Index(current.sku[SKU])
    for b in (mom_baseline, yoy_baseline):
        if b is not None:
            all_skus = all_skus.union(pd.Index(b.sku[SKU]))

    cur = current.sku.set_index(SKU)[["units", "gross", "profit"]].reindex(all_skus)
    out = pd.DataFrame(index=all_skus)
    out.index.name = SKU
    out["current_units"] = cur["units"]
    out["current_gross"] = cur["gross"]
    out["current_profit"] = cur["profit"]

    _lens_deltas(out, current, mom_baseline, "mom")
    _lens_deltas(out, current, yoy_baseline, "yoy")

    return out.reset_index()


# ─────────────────────────────────────────────────────────────────────────────
# Structural movers
# ─────────────────────────────────────────────────────────────────────────────
def _structural_movers(sku_deltas: pd.DataFrame, gate: float) -> pd.DataFrame:
    """SKUs whose MoM and YoY profit deltas disagree in direction and are material.

    Materiality: ``max(|mom|, |yoy|) >= gate``. Disagreement: opposite signs.
    Ranked by ``divergence = |yoy - mom|`` descending.
    """
    columns = [SKU, "profit_delta_mom", "profit_delta_yoy", "divergence"]
    both = sku_deltas["profit_delta_mom"].notna() & sku_deltas["profit_delta_yoy"].notna()
    if not both.any():
        return pd.DataFrame(columns=columns)

    d = sku_deltas[both].copy()
    mom = d["profit_delta_mom"].astype(float)
    yoy = d["profit_delta_yoy"].astype(float)
    disagree = ((mom > 0) & (yoy < 0)) | ((mom < 0) & (yoy > 0))
    material = (mom.abs() >= gate) | (yoy.abs() >= gate)
    movers = d[disagree & material].copy()
    movers["divergence"] = (yoy - mom).abs()[disagree & material]
    movers = movers.sort_values("divergence", ascending=False).reset_index(drop=True)
    return movers[columns]


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def compare(
    current: PeriodData,
    mom_baseline: PeriodData | None = None,
    yoy_baseline: PeriodData | None = None,
    *,
    current_mom_view: PeriodData | None = None,
    current_yoy_view: PeriodData | None = None,
    residual_threshold: float = COST_BRIDGE_RESIDUAL_THRESHOLD,
) -> ComparisonResult:
    """Compute the full comparison for one current period against present baselines.

    ``current`` is the canonical current-period data (received once).
    ``current_mom_view`` / ``current_yoy_view`` are the current period as seen in
    each file; when supplied they are anchor-checked against ``current``. At least
    one baseline must be provided. MoM-only / YoY-only / both are all valid;
    absent-lens deltas are NA and structural movers needs both lenses.
    """
    if mom_baseline is None and yoy_baseline is None:
        raise ValueError("compare requires at least one baseline (MoM and/or YoY).")

    views = []
    if current_mom_view is not None:
        views.append(("MoM file", current_mom_view))
    if current_yoy_view is not None:
        views.append(("YoY file", current_yoy_view))
    if views:
        _assert_anchor(current, views)

    revenue_mom = revenue_bridge(mom_baseline, current) if mom_baseline else None
    revenue_yoy = revenue_bridge(yoy_baseline, current) if yoy_baseline else None
    cost_mom = cost_bridge(mom_baseline, current, residual_threshold) if mom_baseline else None
    cost_yoy = cost_bridge(yoy_baseline, current, residual_threshold) if yoy_baseline else None
    if yoy_baseline is None:
        # No YoY file in this run — the YoY revenue/cost bridges are absent by
        # design (left as None), not an error to surface downstream.
        logger.info("YoY comparisons skipped — no YoY file provided.")

    sku_deltas = _sku_deltas(current, mom_baseline, yoy_baseline)

    # Materiality gate: max($100, 1% of current period profit).
    period_profit = _val(current.summary, _PROFIT_LINE) or float(current.sku["profit"].sum())
    gate = max(100.0, 0.01 * abs(period_profit))

    if mom_baseline is not None and yoy_baseline is not None:
        movers = _structural_movers(sku_deltas, gate)
        logger.info("Structural movers: %d SKU(s) over gate $%.2f.", len(movers), gate)
    else:
        movers = pd.DataFrame(columns=[SKU, "profit_delta_mom", "profit_delta_yoy", "divergence"])
        logger.info("Structural movers skipped — needs both MoM and YoY lenses.")

    return ComparisonResult(
        current_period=current.period,
        sku_deltas=sku_deltas,
        revenue_bridge_mom=revenue_mom,
        revenue_bridge_yoy=revenue_yoy,
        cost_bridge_mom=cost_mom,
        cost_bridge_yoy=cost_yoy,
        structural_movers=movers,
    )
