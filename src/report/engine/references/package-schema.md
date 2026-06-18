# Analysis package schema (the pipeline → skill contract)

This skill is the **report-synthesis layer**. The Python pipeline produces a structured analysis
package; this skill turns it into a polished business report. The package is the **source of truth** —
the skill does not recompute metrics from raw Excel. (Raw Excel, if present, is secondary context for
spot-checking only; see `crosscheck_raw.py`.)

`load_package.py` reads the files below from one directory, validates them, and normalizes them into a
single `package.json` the report writer and verifier consume. **Field names below are the contract.**
If your pipeline already emits different names, either rename in the pipeline or tell the skill author
to adjust the loader's field map — do not let the skill silently guess.

Every file is optional except `channel_metrics.json`. Missing optional files degrade gracefully: the
corresponding report section is shortened or omitted, and a note is added to Data Quality Caveats.

---

## 1. run_metadata.json
```json
{
  "marketplace": "TikTok Shop",
  "package_schema_version": "1.0.0",
  "current_period": {"label": "April 2026", "start": "2026-04-01", "end": "2026-04-30"},
  "mom_baseline":  {"label": "March 2026", "start": "2026-03-01", "end": "2026-03-31"},
  "yoy_baseline":  {"label": "April 2025", "start": "2025-04-01", "end": "2025-04-30"},
  "pipeline_version": "2.0.1",
  "generated_at": "2026-06-10T14:00:00Z",
  "currency": "USD"
}
```
Drives the cover, period labels, and column headers. If absent, labels fall back to whatever the
metric files carry.

## 2. channel_metrics.json  (REQUIRED)
Channel-level P&L for the current period plus pre-computed MoM / YoY deltas. The pipeline has already
done the math; the skill never re-derives it.
```json
{
  "current": {"gross": 32033.09, "profit": 7595.09, "units": 2133, "orders": 1996,
              "profit_margin_pct": 23.71, "ad_cost": 1861.55, "affiliate": 1983.19,
              "shipping": 10336.77, "cogs": 4008.23, "refund": 704.91,
              "ad_pct_of_gross": 5.81, "affiliate_pct_of_gross": 6.19, "refund_rate_pct": 2.20},
  "mom": {"gross_pct": -51.98, "profit_pct": -39.19, "units_pct": -52.85, "margin_pts": 4.99,
          "baseline": {"gross": 66710.72, "profit": 12490.01, "units": 4524, "profit_margin_pct": 18.72}},
  "yoy": {"gross_pct": 86.96, "profit_pct": 50.09, "units_pct": 46.30, "margin_pts": -5.82,
          "baseline": {"gross": 17133.93, "profit": 5060.37, "units": 1458, "profit_margin_pct": 29.53}},
  "bridge_mom": [{"line": "Total Gross Sale", "delta": -34677.63, "pct_of_profit_delta": 708.44}, ...],
  "bridge_yoy": [{"line": "Total Gross Sale", "delta": 14899.16}, ...]
}
```
`bridge_*` are optional; if present they drive the waterfall charts and the "why profit moved" tables.

## 3. sku_metrics_current.csv|json
One row per active SKU for the current period. Columns:
`sku, name, theme, units, gross, profit, profit_margin_pct, ad_cost, profit_before_ads,
break_even_roas, segment` (segment ∈ Scale / Test More / Fix / Pause Ads / Deprioritize / Steady).

## 4. sku_comparisons_mom.csv|json  and  5. sku_comparisons_yoy.csv|json
One row per SKU with the pre-computed change vs that baseline. Columns:
`sku, theme, profit_current, profit_baseline, profit_delta, profit_delta_pct, units_delta,
materiality` (materiality = bool or "material"/"noise"; the pipeline decides what's noise).
The skill ranks **from these**; it does not compute deltas itself.

## 6. sku_historical_trends.csv|json
Trailing multi-period series per SKU (the pipeline maintains the history DB). Long format:
`sku, theme, period_label, period_end, units, gross, profit, profit_margin_pct` — one row per
SKU-period. The skill reads the series for the trend section/chart and trusts the pipeline's
trailing averages if it provides them (optional extra columns `trailing_3m_profit`,
`trailing_6m_avg_units`, `trend_direction` ∈ rising/flat/declining).

## 7. anomaly_flags.json
The pipeline owns anomaly DETECTION. The skill only narrates what's flagged.
```json
[{"sku": "FG-3BLAH-4P2", "theme": "Black American Heritage", "kind": "margin_drop|ad_spike|both_lenses_down|...",
  "severity": "high|medium|low",
  "evidence": {"margin_before": 20.6, "margin_after": 7.73, "ad_spend_delta": 533},
  "lenses": ["mom", "yoy"], "suggested_context": "no logged event"}]
```
`evidence` may be a structured object (preferred, as shown) or a plain string; the loader and skill accept both.

## 8. data_quality_warnings.json
The pipeline owns validation. The skill must surface every item here in Data Quality Caveats.
```json
[{"code": "unsettled_payouts", "severity": "info|warn|error",
  "message": "346 April orders ($4,854.60 gross) unsettled at export; est. fee impact -$291.28",
  "affects": "current-period margin (optimistic)"}]
```
Known codes the skill phrases well: `unsettled_payouts`, `unmapped_ads`, `canceled_shipping`,
`unallocated_credit`, `ad_cost_mapping_gap`, `yoy_bridge_residual`, `missing_history`,
`low_volume`, `raw_vs_package_conflict`.

## 9. report_context.md
Free-text business context the operator wants reflected (planned promos, stock-outs, launches,
seasonality notes). The skill may quote/paraphrase it as **labeled context**, never as asserted cause.

## 10. Raw/cleaned Excel (optional, secondary)
If provided, `crosscheck_raw.py` spot-checks a few headline channel totals against
`channel_metrics.json`. Disagreements are reported as a `raw_vs_package_conflict` data-quality caveat —
the package value is kept; the raw value is never silently substituted.

---

## Source-of-truth rule (enforced)
1. Package present → package is truth. Report only what the package supports.
2. Raw conflicts with package → keep the package number, add a `raw_vs_package_conflict` caveat.
3. Package missing a metric → say so in Data Quality Caveats; do not back-fill from raw silently.
4. Only-raw, no package → fall back to legacy mode (see SKILL.md Fallback); state that mode at the top.
