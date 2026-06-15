"""Transform layer: clean, type, dedupe-by-summing, and reshape raw sheets.

Boundary: this layer turns the ingest layer's faithful-but-raw DataFrames into
tidy, trustworthy per-period structures. It cleans and reshapes; it does NOT
compute derived metrics (margins, deltas, ranks, segments) — that begins in
``sku_metrics.py`` and the analysis layer.
"""
