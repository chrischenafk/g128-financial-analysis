"""Compute per-SKU derived metrics and a segment classification.

Transform layer — file 2 of 3. Input is the ``NormalizedWorkbook`` from
``normalize_tiktok.py`` (one row per ``Marketplace SKU`` per period, additive
totals summed, raw signs preserved). This module computes single-period,
single-file derived metrics — there are **no MoM/YoY deltas here** (those belong
to ``comparisons.py``).

Everything operates on the **active set** (``Total Sold Units > 0``, via the
normalize layer's ``active_skus()`` helper — 211 SKUs for April 2026). Ranks and
the distributional segment thresholds are over active SKUs only; the full
catalog stays available upstream for loss-maker / data-quality views later.

Derived-metric conventions (verified against the real April 2026 data):

  * ``profit_margin_pct``   = Total Profit / Total Gross Sale * 100
  * ``ad_spend``            = -Total AD Cost   (raw is negative; reported as a
                              positive magnitude)
  * ``profit_before_ads``   = Total Profit - Total AD Cost   (ad cost is
                              negative, so this adds it back)
  * ``pread_contribution_margin`` = profit_before_ads / Total Gross Sale
  * ``breakeven_roas``      = 1 / pread_contribution_margin, but **undefined
                              (NaN)** when ad_spend == 0 (no ads → break-even is
                              N/A), gross == 0, or pre-ad contribution ≤ 0 (a SKU
                              losing money before ads has no meaningful
                              break-even). Never a nonsense or negative ROAS.

Ranks (over the active set, ``rank 1 = highest``): ``rank_by_gross``,
``rank_by_profit``, ``rank_by_margin``. Ties use pandas ``method="min"`` (tied
SKUs share the lowest rank in the tie, e.g. two firsts → 1, 1, 3).

Segment classification — exactly one of Scale / TestMore / Fix / PauseAds /
Deprioritize / Steady, using distributional thresholds over the active set:

  * Thresholds are the 25th/50th/75th percentiles of ``gross`` and of
    ``profit_margin_pct``, computed with ``pandas.Series.quantile`` using its
    default ``interpolation="linear"`` (NumPy linear). Margin percentiles are
    over SKUs with a defined margin (gross > 0).
  * Rules, **first match wins** (precedence is load-bearing — Phase-1 order):
      1. PauseAds     — profit_before_ads > 0 and profit <= 0
      2. Scale        — gross >= gross_p75 and margin >= margin_p50 and profit > 0
      3. TestMore     — margin >= margin_p75 and gross < gross_p50
      4. Fix          — gross >= gross_p75 and margin < margin_p25
      5. Deprioritize — gross <= gross_p25 and profit <= 0
      6. Steady       — everything else

The Phase-2 deterministic definition above is now authoritative. Phase-1's April
counts (Scale 24, TestMore 31, Fix 17, PauseAds 0, Deprioritize 7, Steady 132)
are an **approximate sanity check only** — the old LLM skill applied undocumented
tie-handling at the percentile edges. We do NOT reverse-engineer those tie-breaks
or tune thresholds to hit exact counts (AGENTS.md §9); a clean implementation
lands within a few SKUs on Scale/Deprioritize/Steady and reproduces TestMore /
Fix / PauseAds, summing to the active total.
"""

from __future__ import annotations

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

# Raw column carrying ad cost (signed negative). Named here, not in normalize.
AD_COST_COLUMN = "Total AD Cost"

# Descriptive fields carried through onto the metrics table (raw names kept —
# the package layer owns the final contract naming).
_DESCRIPTIVE_CARRY = ("Product Name", "Theme", "Category")

# Segment labels, in rule-precedence order (first match wins). "Steady" is the
# default fall-through and is not a matched condition.
SEGMENT_LABELS = ("PauseAds", "Scale", "TestMore", "Fix", "Deprioritize", "Steady")


# ─────────────────────────────────────────────────────────────────────────────
# Output containers
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SegmentThresholds:
    """The distributional cut points used for segmentation (one set per period).

    Percentiles of the active set, ``interpolation="linear"``. Carried out of
    this layer so the report/package can show the boundaries that produced each
    SKU's segment.
    """

    gross_p25: float
    gross_p50: float
    gross_p75: float
    margin_p25: float
    margin_p50: float
    margin_p75: float


@dataclass(frozen=True)
class SkuMetricsResult:
    """Per-period SKU metrics plus the thresholds that drove segmentation."""

    period: Period
    metrics: pd.DataFrame
    thresholds: SegmentThresholds


# ─────────────────────────────────────────────────────────────────────────────
# Single-period computation
# ─────────────────────────────────────────────────────────────────────────────
def compute_period_metrics(active: pd.DataFrame, period: Period) -> SkuMetricsResult:
    """Compute metrics, ranks, thresholds, and segments for ONE period's active set.

    ``active`` must already be the active subset (``Total Sold Units > 0``) for a
    single period. Raw signs on the inputs are preserved; derived fields use the
    documented conventions in the module docstring.
    """
    df = active.reset_index(drop=True)

    gross = df[GROSS_COLUMN].astype(float)
    profit = df[PROFIT_COLUMN].astype(float)
    units = df[UNITS_COLUMN]
    ad_cost = df[AD_COST_COLUMN].astype(float)  # signed negative

    ad_spend = -ad_cost + 0.0  # +0.0 normalizes the IEEE -0.0 from negating 0.0
    profit_before_ads = profit - ad_cost  # ad_cost negative → adds it back

    # Margin & contribution: undefined where gross == 0 (NaN, never a fabricated 0).
    gross_nonzero = gross != 0
    profit_margin_pct = (profit / gross * 100).where(gross_nonzero, np.nan)
    pread_cm = (profit_before_ads / gross).where(gross_nonzero, np.nan)

    # Break-even ROAS only where it is meaningful: real ad spend, positive gross,
    # and positive pre-ad contribution. Otherwise NaN (N/A), not a number.
    breakeven_defined = (ad_spend > 0) & gross_nonzero & (pread_cm > 0)
    breakeven_roas = (1.0 / pread_cm).where(breakeven_defined, np.nan)

    # Ranks over the active set (1 = highest); ties share the lowest rank (min).
    # Nullable Int64 so an undefined-margin rank can be <NA> without coercion.
    rank_by_gross = gross.rank(ascending=False, method="min").astype("Int64")
    rank_by_profit = profit.rank(ascending=False, method="min").astype("Int64")
    rank_by_margin = profit_margin_pct.rank(ascending=False, method="min").astype("Int64")

    metrics = pd.DataFrame(
        {
            "period": df["period"],
            KEY_COLUMN: df[KEY_COLUMN],
            **{col: df[col] for col in _DESCRIPTIVE_CARRY if col in df.columns},
            "gross": gross,
            "profit": profit,
            "units": units,
            "ad_spend": ad_spend,
            "profit_margin_pct": profit_margin_pct,
            "profit_before_ads": profit_before_ads,
            "pread_contribution_margin": pread_cm,
            "breakeven_roas": breakeven_roas,
            "rank_by_gross": rank_by_gross,
            "rank_by_profit": rank_by_profit,
            "rank_by_margin": rank_by_margin,
        }
    )

    thresholds = _compute_thresholds(gross, profit_margin_pct)
    metrics["segment"] = _classify(metrics, profit_before_ads, thresholds)

    _log_period(period, metrics, thresholds)
    return SkuMetricsResult(period=period, metrics=metrics, thresholds=thresholds)


def _compute_thresholds(gross: pd.Series, margin_pct: pd.Series) -> SegmentThresholds:
    """Percentile cut points over the active set (linear interpolation)."""
    gq = gross.quantile([0.25, 0.50, 0.75])
    mq = margin_pct.quantile([0.25, 0.50, 0.75])  # skips NaN margins by default
    return SegmentThresholds(
        gross_p25=float(gq.loc[0.25]),
        gross_p50=float(gq.loc[0.50]),
        gross_p75=float(gq.loc[0.75]),
        margin_p25=float(mq.loc[0.25]),
        margin_p50=float(mq.loc[0.50]),
        margin_p75=float(mq.loc[0.75]),
    )


def _classify(
    metrics: pd.DataFrame,
    profit_before_ads: pd.Series,
    t: SegmentThresholds,
) -> np.ndarray:
    """Assign exactly one segment per row, first-match-wins over the rule order.

    NaN margins compare False in every margin rule, so such a SKU falls through
    to a profit-based rule or to Steady — never silently into a margin segment.
    """
    gross = metrics["gross"]
    profit = metrics["profit"]
    margin = metrics["profit_margin_pct"]

    conditions = [
        (profit_before_ads > 0) & (profit <= 0),                       # PauseAds
        (gross >= t.gross_p75) & (margin >= t.margin_p50) & (profit > 0),  # Scale
        (margin >= t.margin_p75) & (gross < t.gross_p50),              # TestMore
        (gross >= t.gross_p75) & (margin < t.margin_p25),              # Fix
        (gross <= t.gross_p25) & (profit <= 0),                        # Deprioritize
    ]
    choices = ["PauseAds", "Scale", "TestMore", "Fix", "Deprioritize"]
    return np.select(conditions, choices, default="Steady")


def _log_period(period: Period, metrics: pd.DataFrame, t: SegmentThresholds) -> None:
    counts = metrics["segment"].value_counts().to_dict()
    ordered = {label: int(counts.get(label, 0)) for label in SEGMENT_LABELS}
    logger.info(
        "SKU metrics %s: %d active SKUs | segments=%s",
        period,
        len(metrics),
        ordered,
    )
    logger.info(
        "Thresholds %s: gross p25/p50/p75=%.2f/%.2f/%.2f | "
        "margin%% p25/p50/p75=%.2f/%.2f/%.2f",
        period,
        t.gross_p25,
        t.gross_p50,
        t.gross_p75,
        t.margin_p25,
        t.margin_p50,
        t.margin_p75,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — both periods, computed independently
# ─────────────────────────────────────────────────────────────────────────────
def compute_sku_metrics(normalized: NormalizedWorkbook) -> dict[Period, SkuMetricsResult]:
    """Compute per-period SKU metrics for every period in the workbook.

    Each period is computed independently (no cross-period comparison here). The
    active set is taken per period via ``active_skus``. A period with no active
    SKUs yields an empty result with NaN thresholds and a warning (graceful
    degradation, AGENTS.md §8).
    """
    active = active_skus(normalized)
    results: dict[Period, SkuMetricsResult] = {}

    for period in normalized.periods:
        period_active = active[active["period"] == period]
        if period_active.empty:
            logger.warning("No active SKUs for %s — empty metrics result.", period)
            nan_t = SegmentThresholds(*([float("nan")] * 6))
            results[period] = SkuMetricsResult(period, period_active.copy(), nan_t)
            continue
        results[period] = compute_period_metrics(period_active, period)

    return results
