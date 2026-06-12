# AGENTS.md

Operating rules for any AI agent (or human) working in **g128-financial-analysis**.

Read this fully before making changes. This project's entire value is **trustworthy financial
reporting**. The guardrails below are what make the output trustworthy. They are not stylistic
preferences — violating them can silently produce wrong numbers in a report leadership acts on.

> **Scope reminder:** this repo currently targets **TikTok Shop only**, despite the generic name.
> Do not add scaffolding for other marketplaces unless explicitly asked.

---

## 0. The prime directive

**Code produces trusted facts. Claude produces trusted communication.**

- All numbers — totals, deltas, margins, comparisons, anomaly thresholds — are computed in
  deterministic Python in this repo.
- The external Claude skill only *interprets and writes*. It must never be handed the job of
  computing a metric, and this repo must never rely on it to do so.
- If you find yourself about to let the LLM calculate, rank, or reconcile something, stop. That logic
  belongs in `src/` with a test.

---

## 1. Hard boundaries (do not cross without explicit human approval)

These require a human's explicit go-ahead **in the current task**. Do not infer permission.

1. **Never edit the external skill from this repo.** The `pm-analysis-code-supplement` skill lives
   outside this repository and is deployed separately on the Claude Platform. This repo *satisfies*
   its package contract; it does not contain or modify the skill. Do not create a copy of it here and
   "fix" it.
2. **Never commit real data or secrets.** No `.xlsm`/`.xlsx`/`.csv` financial files, no generated
   reports, no analysis packages built from real data, no `.env`, no API keys. If you created one
   while testing, confirm it is gitignored before any commit.
3. **Never change the package schema unilaterally.** The package is a versioned contract (see §3).
   Changing it is a deliberate, coordinated act, not a casual edit.
4. **Never weaken or delete a validation, reconciliation, or anchor-match check** to make a run pass.
   A failing check is information. Investigate the cause; do not silence the alarm.
5. **Never substitute a raw Excel value for a package value** to "fix" a discrepancy. The package is
   the source of truth (see §4).
6. **Never invent, estimate, or back-fill a number** that the data does not support. A missing number
   is a caveat, not a gap to paper over.

If a task seems to require crossing one of these, **pause and ask the human first.** Explain what you
want to do and why. Do not brute-force around the boundary.

---

## 2. Contract-first with deliberate amendments

The pipeline's output — the **analysis package** — is the contract between this repo and the external
skill. The schema is allowed to evolve, but only through a controlled process:

- The package carries a `package_schema_version`.
- The schema is **not** redesigned per run. Once a version is set, the pipeline emits exactly that
  shape and the skill expects exactly that shape.
- **To change the contract:** update the schema definition, bump the version, and update the skill's
  loader/field-map **in lockstep** — coordinated with the human, since the skill is external. A
  pipeline change that adds or renames a field without the matching skill update is a broken contract.
- The pipeline must **never emit a field the skill doesn't know about.** The skill must **never guess**
  at a field the pipeline didn't send. No silent drift in either direction.

When in doubt about whether a change is "just an internal refactor" or "a contract change": if it
alters what lands in the package (field names, types, units, sign conventions, presence/absence),
it is a **contract change** and follows the process above.

---

## 3. Source-of-truth rules (enforced end to end)

1. The **package is the truth.** Anything in the final report must trace to a value in the package.
2. **Raw conflicts with package → keep the package value**, and surface the conflict as a
   `raw_vs_package_conflict` data-quality caveat. Never silently swap in the raw number.
3. **Package missing a metric → say so** as a caveat. Do not back-fill from raw.
4. Raw Excel, when passed alongside, is **secondary context for spot-checking only** — never the
   primary source.

---

## 4. Data-handling rules

- Real financial workbooks live only in `data/raw/` (gitignored). Treat their contents as confidential.
- `data/raw/`, `data/processed/`, `output/`, and `logs/` are gitignored and must stay that way.
- The raw folder is **persistent** — it is a growing historical archive. **Never move, rename, or
  delete files in `data/raw/`** as a side effect of a run. History there powers trend analysis.
- `output/reports/` is **never cleaned**. Each period's report accumulates. Do not add logic that
  prunes it.
- Determine the reporting period from **filenames cross-checked against in-workbook period headers** —
  **never** from file modified dates.
- For tests/examples, use only **sanitized or synthetic** data, kept clearly separate from real files.

---

## 5. Known facts about the data (don't re-derive these wrong)

These were established by direct inspection of the real sample workbooks. Encode them; don't guess.

- **Two sheets carry the signal:** `TikTok Summary` (channel P&L, periods as columns) and
  `TikTok Profit Margin` (SKU level, one row per SKU per period).
- **Dedup / join key is `Marketplace SKU` + period** (the `Date Range` string). Not TikTok SKU ID.
- **Each SKU appears once per period.** A two-period file lists each SKU twice. Account for that when
  aggregating — do not double-count, and do not assume one row per SKU.
- **The Profit Margin sheet holds the full catalog** (thousands of rows, mostly zero-activity).
  "Active" SKUs are those with non-zero units or gross, derived by filtering — *not* a row count.
- **Costs are signed negative.** `Total Profit` is the sum of all money lines.
  `profit_margin_pct = Total Profit / Total Gross Sale`.
- **A monthly batch is two files sharing the same current period** (a MoM file and a YoY file). The
  current period must be **identical** across both; assert this anchor match before merging. The
  Phase-1 reference checks exactly this (gross/profit gap must be $0.00).
- **MoM vs YoY is inferred from the period gap** (≈1 month vs ≈12 months), not a filename label.
- **Real filename convention.** The actual sample workbooks in `data/raw/` are named
  `Tiktok SKU-Level Profit 2026.03 vs 2026.04.xlsm` — lowercase `t`, a **dot** between year and
  month, **spaces** around `vs`, no MoM/YoY token. An underscore variant
  (`Tiktok_SKULevel_Profit_2026_03_vs_2026_04.xlsm`) also occurs. The parser tolerates **both**
  separator styles (it keys only on the `YYYY<sep>MM vs YYYY<sep>MM` number structure), not an
  idealized name. It does **not** read MoM/YoY from the name — that comes from the period gap.
- **The Summary has more line items than any abbreviated schema example** (Ocean Freight, Customs,
  ShippingEasy Supply, Returned Shipping, multiple fee/commission lines). Carry the full line set for
  bridges; don't truncate to a sample.

---

## 6. Regression targets (the metrics layer must reproduce these)

The Phase-1 report for **April 2026** is the regression baseline. A correct pipeline reproduces, to
the penny / stated tolerance:

- Total Gross Sale: **$32,033.09**
- Total Profit: **$7,595.09**
- Profit Margin: **23.71%**
- Total Sold Units: **2,133**

The April SKU-sum ties to the Summary tab exactly for the current period. The April-2025 YoY baseline
is known to tie only **within $32.99** due to an unallocated marketplace-level credit — that gap is
**expected and disclosed**, not a bug to "fix" by forcing it to zero.

If the pipeline does not reproduce the current-period figures above before Claude is ever involved,
**the pipeline is wrong** — stop and fix it there. Do not adjust the report to match; adjust the code
to match reality.

---

## 7. Build order (respect the sequence)

Do not jump ahead to later layers while earlier ones are unverified.

1. **Ingest** — file scanning, period parsing, MoM/YoY pairing, **anchor-match assertion**. (current)
2. **Transform** — normalize sheets, SKU-level metrics.
3. **Analysis** — MoM/YoY comparisons, anomaly flags, data-quality warnings.
4. **Package** — serialize to the versioned contract; verify it reproduces the regression targets.
5. **LLM** — stubbed skill call first (no key needed), then the real Claude Platform API.
6. **History (Level 2)** — local trailing-history cache for 3/6-month context. Don't block MVP on it.

Claude integration is **last**, and stays stubbed until the package is trusted.

---

## 8. Engineering conventions

- **Python 3.11+.** Keep modules small and single-purpose; preserve the
  ingest/transform/analysis/package/llm boundaries. No monolithic script (a throwaway prototype aside).
- **Determinism.** Same inputs → same package, every time. No randomness, no network calls, no
  wall-clock dependence in the metric path. The only external call in the whole pipeline is the
  (isolated, stubbable) Claude step in `src/llm/`.
- **Fail loud, fail early.** Malformed filename, ambiguous period, mismatched anchor, missing required
  sheet → raise a clear error and stop. Never silently guess your way past bad input.
- **Tests for every fact-producing module.** Period parsing, metrics, and comparisons each get tests;
  the metrics tests assert the §6 regression targets.
- **Secrets via environment / `.env`.** Never hardcoded, never logged, never committed.
- **Logging, not print.** Each run leaves a log and updates the manifest/cache in `data/processed/`
  so it's auditable: which files, which period, which report, what warnings.
- **Graceful degradation.** Missing optional input (e.g. one of MoM/YoY, or a data-quality sheet)
  shortens or omits the affected section and adds a caveat — it does not crash the run.

---

## 9. When to stop and ask

Pause and ask the human — don't brute-force — when:

- A change would touch the package schema / contract (§2).
- A validation, reconciliation, or anchor check is failing and the cause isn't obviously benign.
- The data doesn't match the known facts in §5 (e.g. a new sheet layout, a SKU appearing 3× in a
  period, a sign-convention surprise).
- You'd need to cross any §1 hard boundary to finish the task.
- The right design choice is genuinely ambiguous and picking wrong would be expensive to unwind.

A paused question is cheap. A confidently-wrong number in a leadership report is not.
