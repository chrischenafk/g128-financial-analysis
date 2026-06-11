# Project Context — g128-financial-analysis (paste this into Cursor first)

You are helping build a financial-reporting data pipeline. This document is your full project
context. **Read `README.md` and `AGENTS.md` in this repo before doing anything** — `AGENTS.md` is the
authoritative source for engineering boundaries and overrides anything here if they ever conflict.
This document adds the detailed architecture and the working agreement for how we'll build.

We are building this **file by file**. Do not scaffold the whole project at once. Each prompt I send
will target one script. Produce that one script, explain your choices, and stop. I validate each piece
before we move on. If you find yourself wanting to write three modules to make one work, stop and tell
me — don't run ahead.

---

## 1. What we're building

An automated pipeline that turns each period's TikTok Shop profit-and-loss workbook(s) into a polished
business report. The defining principle:

**Code produces trusted facts. Claude produces trusted communication.**

- All numbers — totals, deltas, margins, comparisons, anomaly thresholds — are computed in
  deterministic Python *in this repo*.
- An **external** Claude skill (deployed separately on the Claude Platform, reached via API) only
  *interprets and writes* the report. It never computes a metric. This repo never relies on it to.

This is Phase 2. Phase 1 was a manual workflow where a Claude skill read raw Excel and wrote the whole
report itself. Phase 2 moves all calculation into versioned, testable code and reduces Claude to the
synthesis layer.

> **Scope:** TikTok Shop only, despite the generic repo name. Do not build scaffolding for other
> marketplaces unless explicitly asked. The structure should *allow* later generalization, not
> implement it now.

---

## 2. Environment

- **Dev machine:** macOS (this Cursor session). **Eventual deploy target:** Windows, and ultimately
  the company codebase via SQL Server Management Studio / GitHub. Write OS-portable code: use
  `pathlib`, never hardcode separators, don't assume `python3` vs `python`.
- **Python:** target **3.12** (stable). Do not rely on 3.13/3.14 pre-release features even if a newer
  interpreter is installed locally.
- **npm** is available (needed later for the doc-builder side, which lives in the external skill, not
  here — but keep it in mind).
- Private GitHub repo. **Real financial data and secrets never get committed.** `.env` for secrets.

---

## 3. The contract: the analysis package

The pipeline's output is a structured **analysis package** — the contract between this repo and the
external skill. This is the most important artifact in the system.

**Contract-first with deliberate amendments:**

- The package carries a `package_schema_version`.
- The schema is **not** redesigned per run. The external skill expects an exact shape for each version.
- The schema *may* evolve, but only through a deliberate, coordinated amendment: schema doc updated,
  version bumped, external skill's loader/field-map updated in lockstep. **This is coordinated with me,
  the human — never done unilaterally**, because the skill is external.
- The pipeline never emits a field the skill doesn't know about. The skill never guesses at a field the
  pipeline didn't send.

The authoritative schema lives with the external skill, not in this repo. This repo's job is to
**satisfy** that contract. We have not finalized the schema yet — it will emerge as we build the
metrics layer — but once we lock it, it's locked.

**Source of truth:** the package is the truth. If raw Excel ever conflicts with the package, the
package wins and the conflict is surfaced as a caveat — never silently swapped. Missing metric → stated
as a caveat, never back-filled.

---

## 4. Architecture — layer by layer

Five layers, each with a narrow, independently testable job. Keeping them distinct is the whole
engineering bet. The LLM never sees raw Excel; the package is the only thing crossing into Claude.

```
data/raw/        [INGEST]          [TRANSFORM]        [ANALYSIS]         [PACKAGE]          [LLM]
.xlsm files  →  scan + parse  →  normalize into  →  compute metrics  →  write versioned  →  Claude skill
                + pair files      DataFrames          + comparisons       package            → report
                + load sheets     + validate          + anomaly flags
                                  + deduplicate       + data quality
```

### Layer 1 — Ingest (`src/ingest/`)
- **`file_scanner.py`** — scan `data/raw/`, find `.xlsm` files matching the naming pattern, return
  candidates. Nothing else.
- **`period_parser.py`** — from a filename, extract the two period strings and parse to structured
  dates; **also read the workbook's Summary header and cross-check** — the two must agree. Error
  loudly on mismatch or ambiguity. Infer MoM vs YoY from the date gap (≈1 month = MoM, ≈12 months =
  YoY), not from any filename label.
- **`excel_loader.py`** — given a validated path + periods, load the relevant sheets into raw
  DataFrames faithfully. **No cleaning here.**

Output of ingest: raw DataFrames + confirmed period metadata. Nothing computed yet.

### Layer 2 — Transform (`src/transform/`)
- **`normalize_tiktok.py`** — raw DataFrames → clean, typed, consistently-named DataFrames. Handles the
  active-SKU filter, sign conventions, the two-rows-per-SKU-per-period structure, real-data edge cases.
  **The regression targets (§6) must be reproducible from this layer's output alone.**
- **`sku_metrics.py`** — normalized data → per-SKU metrics: gross, profit, margin, units, ad spend,
  profit-before-ads, break-even ROAS, segment classification.
- **`historical_index.py`** — read/write the local SQLite history store for trailing context. Stub for
  MVP, real for Level 2.

### Layer 3 — Analysis (`src/analysis/`)
- **`comparisons.py`** — MoM/YoY deltas, revenue decomposition (volume/price/new-SKU bridge),
  structural movers (SKUs whose MoM and YoY lenses disagree).
- **`anomalies.py`** — deterministic, rules-based flags. Every rule is explicit Python, never a prompt.
  Emits the `anomaly_flags` structure the skill expects.
- **`data_quality.py`** — read the four data-quality sheets → `data_quality_warnings`. Unsettled
  payouts, unmapped ads, canceled shipping, unallocated credits.

### Layer 4 — Package (`src/package/`)
- **`writer.py`** — take everything analysis produced and serialize it to the exact contract. The only
  place that knows the schema version. **Does ZERO computation — only shapes and writes.** Every field
  name here is the contract; changing one means bumping the version and updating the external skill.

### Layer 5 — LLM (`src/llm/`)
- **`claude_client.py`** — call the external skill via API. **Stubbed first** (writes a placeholder
  report), real later. API key from `.env`.
- **`prompt_builder.py`** — assemble the request from the package.

### Support
- **`src/config.py`** — single home for all paths and settings. Everything imports from here; no magic
  strings scattered around. Includes `PACKAGE_SCHEMA_VERSION`, `MARKETPLACE`, and all the `Path`s.
- **`src/utils/`** — `logger.py` (logging, not print), `paths.py`.
- **`src/main.py`** — entry point that *coordinates* the layers and does no work itself. Reads CLI args
  (`--target-period`, `--force`), then calls each layer in order, failing loudly with a layer-specific
  message if any step breaks.

---

## 5. Known facts about the real data (established by direct inspection — encode, don't re-guess)

- **Two sheets carry the signal:** `TikTok Summary` (channel P&L, **periods are columns** like
  `04/01/2026 - 04/30/2026`, line items are rows) and `TikTok Profit Margin` (SKU level, **one row per
  SKU per period** — each SKU appears twice in a two-period file). Plus four data-quality sheets:
  `TikTok Unmapped Ads`, `TikTok Canceled Shipping`, `TikTok Unmapped Payout`,
  `PM_TikTok_outOrdersWithoutPayou`.
- **Dedup / join key = `Marketplace SKU` + period** (the `Date Range` string). NOT TikTok SKU ID.
- **Costs are signed negative.** `Total Profit` = sum of all money lines.
  `profit_margin_pct = Total Profit / Total Gross Sale`.
- **The Profit Margin sheet holds the FULL catalog** (~7,300 rows/period, mostly zero-activity).
  "Active" SKUs = non-zero units or gross, derived by **filtering**, not by counting rows
  (~213 active in April 2026).
- **A monthly batch = two files sharing the same current period** — a MoM file (current vs prior month)
  and a YoY file (current vs same month last year). The current period must be **identical** across
  both; **assert this anchor match before merging** (current-period gross/profit gap must be $0.00).
- **Real filename convention:** `Tiktok_SKULevel_Profit_2026_03_vs_2026_04.xlsm` — lowercase `t`,
  underscores, no MoM/YoY token. The parser must tolerate this exact style.
- **Summary has many line items** (Gross, Units, Orders, Refund, Tiktok Shipping, Referral Fee,
  Affiliate commission, Refund admin fee, Affiliate Shop Ads commission, Co-funded promo fee, Campaign
  fee, AD Cost, Order ShippingEasy, ShippingEasy Supply, Returned Shipping, Other Expense, COGS, Ocean
  Freight, Customs, Profit). Carry the full set for bridges; don't truncate.
- **Two known wrinkles to resolve, not paper over:** (a) at least one SKU has a true duplicate row
  within a period (max 2 rows per period+SKU) — decide sum-vs-dedup deterministically; (b) active-SKU
  count was 213 vs the Phase-1 report's 211 — a small extra filter we need to reconcile. Flag both when
  you reach them; don't silently absorb them.

---

## 6. Regression targets (April 2026 — the metrics layer must reproduce these)

The uploaded Phase-1 report is the baseline. A correct pipeline reproduces, to the penny / stated
tolerance, **before Claude is ever involved**:

- Total Gross Sale: **$32,033.09**
- Total Profit: **$7,595.09**
- Profit Margin: **23.71%**
- Total Sold Units: **2,133**

The April SKU-sum ties to the Summary tab exactly for the current period. The April-2025 YoY baseline
is known to tie only **within $32.99** (an unallocated marketplace-level credit) — that gap is
**expected and disclosed**, not a bug to force to zero. If the pipeline doesn't hit the figures above,
the pipeline is wrong — fix the code, never adjust the report to match.

---

## 7. History store design (SQLite now, SQL Server later)

MVP re-scans `data/raw/`. Level 2 adds a local **SQLite** history index for trailing 3/6-month context.
Because this eventually migrates to **SQL Server**, write **plain, portable SQL** from day one — no
SQLite-only types, no JSON columns. Use SQLAlchemy so the eventual swap is just a connection string.
Starter shape (two tables): a `run_history` (one row per ingested period/file) and a
`sku_period_metrics` (one row per SKU per period — the historical series). Keep types translatable
(`DATE`, `DECIMAL(12,2)`, `TEXT`/`VARCHAR`, `INTEGER`).

---

## 8. Build order (do not jump ahead)

1. **Skeleton + ingest** — folder structure, `config.py`, `paths.py`, `file_scanner.py`,
   `period_parser.py` with the anchor-match assertion. Prove it can find, pair, and validate the two
   sample files. **No metrics yet.** ← we start here
2. **Transform** — normalize, then SKU metrics. Reproduce the regression targets.
3. **Analysis** — comparisons, anomalies, data quality.
4. **Package** — serialize to the (now-locked) versioned contract; verify it reproduces the targets.
5. **LLM** — stubbed skill call first, then the real Claude Platform API.
6. **History (Level 2)** — SQLite trailing context. Don't block MVP on it.

Claude integration is **last** and stays stubbed until the package is trusted.

---

## 9. Non-negotiables (the short list — full version in AGENTS.md)

- **Never edit the external skill from this repo.** It lives elsewhere; this repo only satisfies its
  contract.
- **Never commit real data or secrets.** Check gitignore before any commit.
- **Never change the package schema unilaterally** — it's a versioned, coordinated contract.
- **Never weaken a validation / reconciliation / anchor check to make a run pass.** A failing check is
  information.
- **Never substitute a raw value for a package value, or invent/back-fill a number.** Missing → caveat.
- **Determinism:** same inputs → same package. No randomness, no network, no wall-clock in the metric
  path. The only external call is the isolated, stubbable Claude step.
- **Fail loud, fail early.** Malformed filename, ambiguous period, mismatched anchor, missing required
  sheet → clear error and stop. Never guess past bad input.
- **Tests for every fact-producing module.** Metrics tests assert the §6 regression targets.

---

## 10. How to work with me

- One script per prompt. Build it, explain the key choices, stop.
- If a design choice is genuinely ambiguous, or a task would require crossing a non-negotiable, **pause
  and ask** — don't brute-force. A paused question is cheap; a confidently-wrong number in a leadership
  report is not.
- When the real data doesn't match §5, tell me — don't smooth it over.
- Keep modules small and single-purpose. No monolithic script.

Acknowledge you've read this and `AGENTS.md`, then wait for my first build prompt (the skeleton +
ingest layer). Don't start writing code in response to this document.
