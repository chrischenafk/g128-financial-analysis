#!/usr/bin/env python3
"""
load_package.py — read the pipeline's structured analysis package from one directory, validate it,
and normalize it into a single package.json that the report writer + verifier consume.

This script does NOT recompute business metrics. It trusts the pipeline. Its only "analysis" is:
  - presence/shape validation (which files exist, required fields present)
  - light internal-consistency checks that compare numbers the pipeline ALREADY computed against each
    other (e.g. does channel_metrics.mom.gross_pct agree with current vs baseline gross?). A mismatch
    is recorded as a data-quality flag — never silently corrected.
  - ranking helpers that ORDER the pipeline's own per-SKU deltas (no delta is computed here).

Usage: python3 load_package.py <package_dir> -o package.json
"""
import argparse, csv, json, os, sys

# The package-schema version this loader is written against (see references/package-schema.md).
SUPPORTED_SCHEMA_VERSIONS = {"1.0.0"}

# Data-quality codes the skill knows how to phrase well. Unknown codes still surface, but get a
# generic treatment and a loader flag so the writer knows to phrase them carefully. The phrasing_hint
# is guidance for the report-writer, not final copy.
KNOWN_DQ_CODES = {
    "unsettled_payouts": "Orders not yet settled at export; current-period margin reads optimistic.",
    "unmapped_ads": "Ad spend on SKUs with no sales row; lives in the channel total only.",
    "canceled_shipping": "Shipping booked on canceled orders; minor cost leakage.",
    "unallocated_credit": "Marketplace-level credit not allocated to any SKU; affects reconciliation tie.",
    "missing_history": "Trailing series incomplete/illustrative; read trend direction, not levels.",
    "low_volume": "Too few units to draw a conclusion; treat as noise unless corroborated.",
    "raw_vs_package_conflict": "Raw Excel disagrees with the package; package value kept, gap flagged.",
    # --- pipeline v1.0.0 additions ---
    "ad_cost_mapping_gap": "Some ad cost could not be mapped to a SKU; SKU-level ad efficiency is "
                            "understated by that amount while the channel ad total stays correct.",
    "yoy_bridge_residual": "The YoY profit bridge does not tie to the YoY profit change to the penny; "
                            "the residual is disclosed, not forced to zero.",
}


def read_json(path):
    with open(path) as f:
        return json.load(f)


def read_table(path):
    """Read .json (list of dicts) or .csv into a list of dicts. Numbers coerced where possible."""
    if path.endswith(".json"):
        data = read_json(path)
        return data if isinstance(data, list) else data.get("rows", [])
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out = {}
            for k, v in r.items():
                if v is None or v == "":
                    out[k] = None
                else:
                    try:
                        out[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
                    except (ValueError, AttributeError):
                        out[k] = v
            rows.append(out)
    return rows


def find(pkg_dir, stem):
    """Return the first existing path matching <stem>.json or <stem>.csv."""
    for ext in (".json", ".csv"):
        p = os.path.join(pkg_dir, stem + ext)
        if os.path.exists(p):
            return p
    return None


def near(a, b, tol):
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package_dir")
    ap.add_argument("-o", "--out", default="package.json")
    a = ap.parse_args()
    d = a.package_dir
    if not os.path.isdir(d):
        sys.exit(f"Not a directory: {d}")

    flags = []  # data-quality flags raised by the loader itself (separate from pipeline warnings)

    def flag(code, sev, msg, affects=""):
        flags.append({"code": code, "severity": sev, "message": msg, "affects": affects})

    # ---- required: channel_metrics ----
    cm_path = find(d, "channel_metrics")
    if not cm_path:
        sys.exit("REQUIRED file channel_metrics.json|csv not found. Cannot build a report without it.")
    channel = read_json(cm_path) if cm_path.endswith(".json") else None
    if channel is None:
        sys.exit("channel_metrics must be JSON (it is nested, not tabular).")

    # ---- optional files ----
    meta = read_json(find(d, "run_metadata")) if find(d, "run_metadata") else {}
    sku_current = read_table(find(d, "sku_metrics_current")) if find(d, "sku_metrics_current") else []
    cmp_mom = read_table(find(d, "sku_comparisons_mom")) if find(d, "sku_comparisons_mom") else []
    cmp_yoy = read_table(find(d, "sku_comparisons_yoy")) if find(d, "sku_comparisons_yoy") else []
    hist = read_table(find(d, "sku_historical_trends")) if find(d, "sku_historical_trends") else []
    anomalies = read_json(find(d, "anomaly_flags")) if find(d, "anomaly_flags") else []
    pipeline_warnings = read_json(find(d, "data_quality_warnings")) if find(d, "data_quality_warnings") else []
    ctx_path = find(d, "report_context") or os.path.join(d, "report_context.md")
    context_md = open(ctx_path).read() if os.path.exists(ctx_path) else ""

    present = {"run_metadata": bool(meta), "sku_metrics_current": bool(sku_current),
               "sku_comparisons_mom": bool(cmp_mom), "sku_comparisons_yoy": bool(cmp_yoy),
               "sku_historical_trends": bool(hist), "anomaly_flags": bool(anomalies),
               "data_quality_warnings": bool(pipeline_warnings), "report_context": bool(context_md)}
    for name, ok in present.items():
        if not ok:
            flag("missing_component", "info", f"Optional package component '{name}' not provided; "
                 f"the corresponding report section will be shortened or omitted.", affects=name)

    # ---- package_schema_version (explicit; the verifier checks it) ----
    schema_version = meta.get("package_schema_version")
    if not schema_version:
        flag("schema_version_missing", "warn",
             "run_metadata has no package_schema_version. The loader is written against "
             f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}; an unversioned package may drift from the contract.",
             affects="contract")
    elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        flag("schema_version_unsupported", "warn",
             f"package_schema_version={schema_version} is outside what this loader supports "
             f"({sorted(SUPPORTED_SCHEMA_VERSIONS)}). Fields may not map; review before trusting the report.",
             affects="contract")

    # ---- anomalies: accept structured-object OR string evidence (amendment #1) ----
    # Structured evidence is preferred and is preserved verbatim so the writer can read evidence.<field>
    # and the verifier can trace the numbers inside it. A plain string is still accepted (legacy).
    for an in anomalies:
        ev = an.get("evidence")
        if isinstance(ev, dict):
            an["evidence_kind"] = "structured"
        elif isinstance(ev, str):
            an["evidence_kind"] = "text"
            flag("anomaly_evidence_text", "info",
                 f"Anomaly {an.get('sku','?')} carries free-text evidence; structured evidence is "
                 f"preferred so individual numbers stay auditable.", affects="anomaly evidence")
        else:
            an["evidence_kind"] = "none"

    # ---- annotate pipeline warnings with known/phrasing (amendment #2) ----
    for w in pipeline_warnings:
        code = w.get("code")
        w["known"] = code in KNOWN_DQ_CODES
        if w["known"]:
            w.setdefault("phrasing_hint", KNOWN_DQ_CODES[code])
        else:
            flag("unknown_dq_code", "info",
                 f"data_quality_warnings code '{code}' is not in the skill's known-codes list; it will "
                 f"be surfaced generically. Add it to KNOWN_DQ_CODES to phrase it well.", affects="caveats")

    # ---- light internal-consistency checks (compare pipeline's OWN numbers; never overwrite) ----
    cur = channel.get("current", {})
    for lens in ("mom", "yoy"):
        block = channel.get(lens)
        if not block or "baseline" not in block:
            continue
        base = block["baseline"]
        for metric, pct_key in (("gross", "gross_pct"), ("profit", "profit_pct")):
            if metric in cur and metric in base and base.get(metric) and pct_key in block:
                implied = (cur[metric] - base[metric]) / abs(base[metric]) * 100
                if not near(implied, block[pct_key], 0.1):
                    flag("internal_inconsistency", "warn",
                         f"channel_metrics.{lens}.{pct_key}={block[pct_key]} disagrees with "
                         f"current vs baseline {metric} (implies {implied:.2f}%). Pipeline value kept; "
                         f"flagged for review.", affects=f"{lens} {metric} delta")
        # margin points consistency
        if "profit_margin_pct" in cur and "profit_margin_pct" in base and "margin_pts" in block:
            implied = cur["profit_margin_pct"] - base["profit_margin_pct"]
            if not near(implied, block["margin_pts"], 0.1):
                flag("internal_inconsistency", "warn",
                     f"channel_metrics.{lens}.margin_pts={block['margin_pts']} disagrees with current "
                     f"minus baseline margin (implies {implied:.2f} pts). Pipeline value kept.",
                     affects=f"{lens} margin")

    # ---- ranking helpers: ORDER the pipeline's own deltas (no delta computed here) ----
    def material(rows):
        out = []
        for r in rows:
            m = r.get("materiality")
            keep = True if m is None else (m is True or str(m).lower() in ("material", "true", "1"))
            if keep:
                out.append(r)
        return out

    def topn(rows, key, n, reverse):
        vals = [r for r in rows if isinstance(r.get(key), (int, float))]
        return sorted(vals, key=lambda r: r[key], reverse=reverse)[:n]

    mom_mat = material(cmp_mom)
    yoy_mat = material(cmp_yoy)
    ranked = {
        "mom_winners": topn(mom_mat, "profit_delta", 8, True),
        "mom_losers": topn(mom_mat, "profit_delta", 8, False),
        "yoy_winners": topn(yoy_mat, "profit_delta", 8, True),
        "yoy_losers": topn(yoy_mat, "profit_delta", 8, False),
        "top_profit_current": topn(sku_current, "profit", 10, True),
        "loss_makers_current": [r for r in sku_current if isinstance(r.get("profit"), (int, float)) and r["profit"] < 0],
    }

    # structural movers: SKUs whose MoM and YoY profit-delta directions disagree
    mom_by = {r.get("sku"): r for r in cmp_mom}
    yoy_by = {r.get("sku"): r for r in cmp_yoy}
    structural = []
    for sku, m in mom_by.items():
        y = yoy_by.get(sku)
        if not y:
            continue
        md, yd = m.get("profit_delta"), y.get("profit_delta")
        if isinstance(md, (int, float)) and isinstance(yd, (int, float)) and (md < 0) != (yd < 0):
            structural.append({"sku": sku, "theme": m.get("theme") or y.get("theme"),
                               "mom_delta": md, "yoy_delta": yd})
    structural.sort(key=lambda r: abs(r["yoy_delta"]), reverse=True)
    ranked["structural_movers"] = structural[:8]

    # historical trend grouping (by SKU, ordered by period_end)
    hist_by = {}
    for r in hist:
        hist_by.setdefault(r.get("sku"), []).append(r)
    for sku in hist_by:
        hist_by[sku].sort(key=lambda r: str(r.get("period_end") or r.get("period_label") or ""))

    out = {
        "schema_version": schema_version,
        "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
        "known_dq_codes": KNOWN_DQ_CODES,
        "meta": meta,
        "channel": channel,
        "sku_current": sku_current,
        "comparisons": {"mom": cmp_mom, "yoy": cmp_yoy},
        "historical": {"by_sku": hist_by, "n_periods": max((len(v) for v in hist_by.values()), default=0)},
        "anomalies": anomalies,
        "pipeline_warnings": pipeline_warnings,
        "loader_flags": flags,
        "context_md": context_md,
        "ranked": ranked,
        "present": present,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    nflag = len(flags)
    print(f"OK package loaded. schema_version={schema_version or 'MISSING'} | "
          f"components present: {sum(present.values())}/8 | "
          f"SKUs current={len(sku_current)} mom_cmp={len(cmp_mom)} yoy_cmp={len(cmp_yoy)} | "
          f"anomalies={len(anomalies)} (structured={sum(1 for a in anomalies if a.get('evidence_kind')=='structured')}) "
          f"pipeline_warnings={len(pipeline_warnings)} loader_flags={nflag}")
    for fl in flags:
        if fl["severity"] in ("warn", "error"):
            print(f"  [{fl['severity'].upper()}] {fl['code']}: {fl['message'][:90]}")


if __name__ == "__main__":
    main()
