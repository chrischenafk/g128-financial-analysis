# report.json schema & the 12-section structure

`build_doc.js` consumes `report.json`. It renders: a cover, an Executive Summary (verdict + scorecard
tiles + short paragraphs), an optional Key Insights block, an ordered list of `sections` (each with
paragraphs, an optional chart, an optional table, and trailing paragraphs), a structured Action Plan,
and an Appendix (assumptions, reconciliation/notes, context sources, verification log).

The 12 report sections map onto these primitives: Executive Summary and Action Plan and Appendix have
dedicated rendering; the other nine are entries in the `sections` array.

## Top-level shape
```json
{
  "meta": {"title","period_label","comparison_line","prepared","source_file","top_caveats":[...]},
  "exec_summary": {"verdict","paragraphs":[...],"scorecard":[{"label","value","change","dir"}]},
  "exec_summary_numbers": { /* every page-1 number, for VR1 tracing — package values only */ },
  "key_insights": [ /* OPTIONAL: 3-6 {kind:win|risk|anomaly|watch, headline, detail, linked_action} */ ],
  "sections": [ {"heading","paras":[...],"chart":"path","chart_caption","table_ref":"key","paras_after":[...]} ],
  "tables": { "<key>": {"columns":[...],"widths":[...],"bold_first":true,"rows":[[...]]} },
  "action_plan": [ {"priority","title","what_we_saw","do_this":[...],"owner_hint","expected_impact"} ],
  "assumptions_log": [ {"assumption","where_used","basis","risk_if_wrong"} ],
  "appendix": {"methodology_paras":[...],"reconciliation_notes":[...],"context_sources":[...]}
}
```

## The 12 sections (order)
1. **Executive Summary** — plain-English, big-picture, stands alone. Verdict sentence + scorecard tiles
   + 2–4 short paragraphs. Lead with momentum (MoM) and how the month went; bring YoY in as the trend
   check; name the one thing to watch. No P&L jargon. (Built from `exec_summary`.)
2. **Current Period Snapshot** — `channel.current` as a compact table (gross, profit, margin, units,
   ad %, affiliate %, refund rate). One or two sentences of plain reading.
3. **MoM Performance** — `channel.mom`; the MoM bridge chart + bridge table if `bridge_mom` exists.
   What drove the change in plain terms.
4. **YoY / Seasonality Context** — `channel.yoy`; the YoY bridge if present. State whether the MoM move
   is seasonal noise or a real trend. Pull seasonality from `report_context.md` + the calendar (labeled).
5. **Historical Trend Analysis** — the `trend` chart + a short read of the trailing series (rising /
   flat / declining; trust pipeline `trend_direction`/trailing fields if present). If history is thin or
   illustrative, say so and keep this section short.
6. **SKU Winners** — `ranked.mom_winners` / `yoy_winners` and `structural_movers` that are up. A table of
   the few that matter, each with its delta and why it's worth noting. Not an exhaustive list.
7. **SKU Losers / Watchlist** — `ranked.*_losers`, loss-makers, and structural decliners. Separate a soft
   month from a multi-period decline using the MoM-vs-YoY contrast. Flag the watchlist explicitly.
8. **Advertising Efficiency** — SKUs with ad spend from `sku_metrics_current` (ad_cost,
   profit_before_ads, break_even_roas). State the ROAS limitation if the package notes it; never invent
   a true ROAS. Call out concentration of spend against weak SKUs.
9. **Margin / Cost / Fee Issues** — where margin compressed and which cost/fee lines drove it (from the
   bridges and current cost ratios). Connect to the YoY margin trend.
10. **Data Quality Caveats** — EVERY `data_quality_warnings` item, loader flag, and raw conflict, phrased
    plainly with its practical effect ("April margin is optimistic because…"). This section must exist
    even if short. VR3 checks coverage.
11. **Recommended Action Plan** — ≤8 items, [HIGH]/[MEDIUM]/[LOW], each: what we saw (cite the figure),
    do-this (concrete numbered steps), owner hint, expected impact (grounded in a real number).
12. **Appendix / Notes** — methodology (one paragraph: package-driven, pipeline version), source-of-truth
    note, assumptions log, the verification-pass log (verbatim), context sources.

## Writing rules
- **The package is the only source of numbers.** If it is not in `package.json`, it does not go in the
  report. VR1 enforces this against `tables` and `exec_summary_numbers`; derived estimates (e.g. "a 5-pt
  margin lift ≈ $X/mo") live in PROSE / `expected_impact`, with the derivation stated inline.
- **Concise and decision-first.** Prioritize the few findings that change a decision; omit the rest.
  Short paragraphs; tables for data, sentences for meaning. Target 8–12 pages.
- **Materiality and volume gates.** Use the pipeline's `materiality`; don't conclude from tiny SKUs.
- **Caveat everything uncertain.** Unsettled payouts, unmapped ads, canceled shipping, missing history,
  low volume → say how each could distort the read. Never overclaim.
- **Context labeled, one step deep.** `report_context.md` and the seasonal calendar are context, never
  asserted causation.
- **Scorecard dir follows MoM** (momentum leads); YoY rides along in the change text.
