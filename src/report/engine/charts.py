#!/usr/bin/env python3
"""
charts.py (package mode) — render charts from the normalized package.json.
  bridge_mom / bridge_yoy : profit waterfall from channel.bridge_* (pipeline-provided deltas)
  trend                   : historical profit line for the top-N tracked SKUs
Usage: python3 charts.py package.json --outdir charts --which bridge_mom,bridge_yoy,trend
"""
import json, argparse, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

INK = "#1a1a1a"; GOOD = "#1f7a1f"; BAD = "#c62828"; BRAND = "#d81617"; GRID = "#e2e2e2"
PALETTE = ["#d81617", "#1f7a1f", "#1a1a1a", "#e08a00", "#5566aa", "#9a4ca0", "#2a9d8f"]


def short(line):
    return (line or "").replace("Total ", "").replace(" Cost", "").replace("Order ShippingEasy", "Ship") \
        .replace("Affiliate commission", "Affiliate").replace("Cost of Goods Sold", "COGS") \
        .replace(" Sale", "").replace("Gross", "Gross")[:18]


def money(x, _):
    return f"-${abs(x)/1000:.0f}k" if x < 0 else f"${x/1000:.0f}k"


def bridge_waterfall(start_label, end_label, start_val, end_val, bridge_rows, path):
    fig, ax = plt.subplots(figsize=(8.6, 4.3), dpi=130)
    gross_row = next((r for r in bridge_rows if "Gross" in (r.get("line") or "")), None)
    items = [(short(r["line"]), r["delta"]) for r in bridge_rows
             if r.get("line") and "Gross" not in r["line"] and "Profit" not in r["line"]]
    seq = ([("Gross", gross_row["delta"])] if gross_row else []) + items
    cum = start_val
    xs = [0]; bottoms = [0]; heights = [start_val]; colors = [INK]; labels = [start_label]
    i = 1
    for name, delta in seq:
        labels.append(name)
        bottoms.append(cum if delta >= 0 else cum + delta)
        heights.append(abs(delta)); colors.append(GOOD if delta >= 0 else BAD)
        cum += delta; xs.append(i); i += 1
    labels.append(end_label); bottoms.append(0); heights.append(end_val); colors.append(INK); xs.append(i)
    ax.bar(xs, heights, bottom=bottoms, color=colors, width=0.62, edgecolor="white", linewidth=0.6)
    span = max(start_val, end_val, cum)
    for x, b, h, (nm, dv) in zip(xs[1:-1], bottoms[1:-1], heights[1:-1], seq):
        ax.text(x, b + h + span * 0.012, f"{dv:+,.0f}", ha="center", va="bottom",
                fontsize=7.0, color=GOOD if dv >= 0 else BAD)
    for x, v in ((0, start_val), (xs[-1], end_val)):
        ax.text(x, v + span * 0.012, f"${v:,.0f}", ha="center", va="bottom",
                fontsize=8, fontweight="bold", color=INK)
    ax.set_xticks(xs); ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=7.4)
    ax.set_ylim(0, span * 1.16)
    ax.yaxis.set_major_formatter(FuncFormatter(money))
    ax.set_title(f"What moved profit: {start_label} \u2192 {end_label}", fontsize=10.5, color=INK, pad=8)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    ax.grid(axis="y", color=GRID, lw=0.6); ax.set_axisbelow(True)
    plt.tight_layout(); plt.savefig(path, bbox_inches="tight"); plt.close()


def trend_lines(hist_by_sku, path, topn=5, metric="profit"):
    series = sorted(hist_by_sku.items(), key=lambda kv: -(kv[1][-1].get(metric) or 0))[:topn]
    fig, ax = plt.subplots(figsize=(8.6, 4.2), dpi=130)
    for i, (sku, rows) in enumerate(series):
        xs = [r.get("period_label") for r in rows]
        ys = [r.get(metric) for r in rows]
        theme = rows[-1].get("theme") or sku
        ax.plot(xs, ys, marker="o", ms=4, lw=1.8, color=PALETTE[i % len(PALETTE)], label=f"{theme} ({sku})")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x/1000:.1f}k"))
    ax.set_title(f"Trailing {metric} by SKU", fontsize=10.5, color=INK, pad=8)
    ax.legend(fontsize=7.2, frameon=False, loc="upper left")
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    ax.grid(axis="y", color=GRID, lw=0.6); ax.set_axisbelow(True)
    plt.xticks(fontsize=8)
    plt.tight_layout(); plt.savefig(path, bbox_inches="tight"); plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package"); ap.add_argument("--outdir", default="charts")
    ap.add_argument("--which", default="bridge_mom,trend")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    p = json.load(open(a.package))
    ch = p["channel"]; which = a.which.split(","); cur = ch.get("current", {})
    meta = p.get("meta", {})
    if "bridge_mom" in which and ch.get("bridge_mom") and ch.get("mom", {}).get("baseline"):
        base = ch["mom"]["baseline"]
        bridge_waterfall(meta.get("mom_baseline", {}).get("label", "Prev month"),
                         meta.get("current_period", {}).get("label", "Current"),
                         base.get("profit"), cur.get("profit"), ch["bridge_mom"],
                         os.path.join(a.outdir, "bridge_mom.png"))
        print("wrote bridge_mom.png")
    if "bridge_yoy" in which and ch.get("bridge_yoy") and ch.get("yoy", {}).get("baseline"):
        base = ch["yoy"]["baseline"]
        bridge_waterfall(meta.get("yoy_baseline", {}).get("label", "Last year"),
                         meta.get("current_period", {}).get("label", "Current"),
                         base.get("profit"), cur.get("profit"), ch["bridge_yoy"],
                         os.path.join(a.outdir, "bridge_yoy.png"))
        print("wrote bridge_yoy.png")
    if "trend" in which and p.get("historical", {}).get("by_sku"):
        trend_lines(p["historical"]["by_sku"], os.path.join(a.outdir, "trend.png"))
        print("wrote trend.png")


if __name__ == "__main__":
    main()
