"""Analysis layer: compare periods, flag anomalies, summarize data quality.

Boundary: this layer consumes the transform layer's clean per-period structures
and computes cross-period facts (deltas, bridges, structural movers, anomaly
flags, data-quality warnings). It still computes only trusted facts — no
narrative, no package serialization. The package layer shapes; the LLM writes.
"""
