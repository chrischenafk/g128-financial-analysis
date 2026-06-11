# g128-financial-analysis

Automated marketplace profit-margin analysis for G128.

> **Current scope: TikTok Shop only.** The repository is named generically because the
> intent is to expand to other marketplaces later, but **everything implemented today targets
> the TikTok Shop pipeline**. Anything in this README describing "marketplaces" in the plural is
> forward-looking, not a description of what currently runs. Do not assume non-TikTok support
> exists until it is actually built.

---

## What this is

A local Python pipeline that turns each period's TikTok Shop profit-and-loss workbook(s) into a
polished, decision-oriented business report — with deterministic code doing the math and an
external Claude skill doing the writing.

The guiding split:

- **Code produces trusted facts.** Ingestion, validation, cleaning, metrics, comparisons,
  trend context, and anomaly detection all happen in deterministic Python.
- **Claude produces trusted communication.** An external skill takes the structured, already-computed
  analysis package and writes the executive narrative, interpretation, and recommendations.

Claude never recomputes the numbers. The pipeline is the source of truth; the skill interprets it.

This is the Phase 2 successor to a manual Phase 1 workflow, where a Claude skill read a raw Excel
file directly and wrote the whole report itself. Phase 2 moves all calculation into versioned,
testable code and reduces Claude to the synthesis layer.

---

## How it works (intended flow)

1. The operator drops the period's standardized TikTok workbook(s) into `data/raw/`.
2. The pipeline scans `data/raw/`, parses reporting periods from filenames, and cross-checks them
   against the period headers inside each workbook.
3. It identifies the latest current period and pairs the relevant month-over-month (MoM) and
   year-over-year (YoY) files for it.
4. It loads, validates, cleans, and normalizes the workbook data.
5. It computes channel-level and SKU-level metrics, MoM/YoY comparisons, historical trend context,
   anomaly flags, and data-quality warnings — all deterministically.
6. It writes a structured **analysis package** to `output/analysis_packages/`.
7. It calls the **external Claude skill** (via the Claude Platform API) with that package as the
   source of truth.
8. Claude returns the final report, which is saved to `output/reports/` with the target period in
   the filename.
9. `output/reports/` is never cleaned; it becomes a running archive of every period's report.

The target business UX: the operator adds files to `data/raw/`, and a finished report appears in
`output/reports/`.

---

## The analysis package (pipeline → skill contract)

The single most important artifact in this repo is the **analysis package** — the structured output
the pipeline produces and the external skill consumes. It is the contract between the two systems.

Core principle, **contract-first with deliberate amendments**:

- The package schema is **versioned**. The pipeline stamps a `package_schema_version`.
- The external skill knows exactly what fields to expect for each version.
- The package **may grow**, but only through a deliberate, documented amendment: schema doc updated,
  version bumped, and the skill's loader/field-map updated in lockstep.
- The pipeline **never emits a field the skill doesn't know about**, and the skill **never guesses**
  at a field the pipeline didn't send. No silent drift on either side.

The authoritative schema lives with the external skill (`pm-analysis-code-supplement`), not in this
repo. This repo's job is to **satisfy** that contract. See `AGENTS.md` for the rules an agent must
follow when the contract needs to change.

---

## Source of truth

- The **package is the truth.** The report contains only what the package supports.
- If a raw Excel value conflicts with the package, the **package value wins**, and the discrepancy is
  surfaced as a data-quality caveat — never silently substituted.
- If the package is missing a metric, that is stated as a caveat; it is not back-filled from raw.

---

## Repository structure

```
g128-financial-analysis/
  README.md
  AGENTS.md
  requirements.txt
  .env.example
  .gitignore

  data/
    raw/            # operator drops period workbooks here (gitignored; real financial data)
    processed/      # run manifest / local history cache (gitignored)

  output/
    reports/            # final reports, one per period, never cleaned (gitignored)
    analysis_packages/  # structured packages handed to the skill (gitignored)

  logs/                 # run logs (gitignored)

  src/
    main.py
    config.py

    ingest/
      file_scanner.py     # scan data/raw/, identify candidate workbooks
      period_parser.py    # parse periods from filenames; cross-check workbook headers
      excel_loader.py     # load the Summary + Profit Margin + data-quality sheets

    transform/
      normalize_tiktok.py # clean/normalize sheets into tidy DataFrames
      sku_metrics.py      # SKU-level metric computation
      historical_index.py # local trailing-history context (Level 2)

    analysis/
      comparisons.py      # MoM / YoY / trailing comparisons
      anomalies.py        # deterministic rules-based anomaly flags
      recommendations.py  # evidence-tagged recommendation candidates

    package/
      writer.py           # serialize computed results to the versioned package contract

    llm/
      claude_client.py    # external Claude skill call (stubbed first, real API later)
      prompt_builder.py   # assemble the skill request from the package

    reports/
      saver.py            # write/return the final report into output/reports/

    utils/
      logger.py
      paths.py

  tests/
    test_period_parser.py
    test_metrics.py
    test_comparisons.py
```

This layout is a starting point and may be refined during implementation, but the modular boundaries
(ingest / transform / analysis / package / llm) should be preserved. Avoid one monolithic script.

---

## Input file shape (TikTok Shop)

Each standardized workbook (`.xlsm`) contains these sheets:

- **`TikTok Summary`** — channel-level P&L. Periods are columns (e.g. `04/01/2026 - 04/30/2026`);
  line items are rows. Costs are signed **negative**; `Total Profit` is the sum of all money lines.
- **`TikTok Profit Margin`** — SKU-level detail. One row per SKU **per period**, so each SKU appears
  twice in a two-period file. Keyed on **`Marketplace SKU`** + the `Date Range` (period) string.
  The sheet holds the **full catalog** (thousands of SKUs); "active" SKUs are those with non-zero
  units or gross and are derived by filtering, not by counting rows.
- **`TikTok Unmapped Ads`**, **`TikTok Canceled Shipping`**, **`TikTok Unmapped Payout`**,
  **`PM_TikTok_outOrdersWithoutPayou`** — data-quality / completeness sheets that feed
  data-quality warnings.

A monthly batch typically arrives as two files sharing the **same current period**:

- a **MoM** file (current month vs prior month), and
- a **YoY** file (current month vs same month last year).

The current period must be **identical** across both files; the pipeline asserts this anchor match
before merging the two into one combined report.

> **Period detection:** the reporting period is parsed from the filename to select files, then
> cross-checked against the period header inside the workbook's Summary sheet. The two must agree.
> File modified dates are never used to determine the period. The MoM-vs-YoY distinction is inferred
> from the gap between the two periods in a file (≈1 month = MoM, ≈12 months = YoY), not from a label
> in the filename.

---

## Usage (planned)

```bash
# process the latest available period found in data/raw/
python src/main.py

# process a specific period
python src/main.py --target-period 2026-04

# regenerate a report that already exists
python src/main.py --target-period 2026-04 --force
```

If a report already exists for the target period, the run skips it unless `--force` is given.
If a filename is malformed or a period is ambiguous, the run stops with a clear error rather than
guessing. If only one of the MoM/YoY files is present, the run proceeds with a warning that the
missing lens reduces context.

---

## Claude integration

The external skill is reached through the Claude Platform API. The integration is modular and isolated
in `src/llm/`. For MVP development the skill call can be **stubbed** so the rest of the pipeline runs
end-to-end without an API key; the real API call is wired in afterward.

- API keys and model settings come from environment variables / `.env`. **Keys are never hardcoded
  and never committed.**
- The skill itself lives **outside this repository** and is deployed separately. This repo treats the
  skill's package schema as a downstream contract to satisfy, not as code to edit.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in values; never commit .env
```

Requires Python 3.11+.

---

## Data handling and privacy

This repository processes **real company financial data**. That data must never enter version control.

- `data/raw/`, `data/processed/`, `output/`, and `logs/` are all gitignored.
- Real `.xlsm` / `.xlsx` / `.csv` / report / `.env` files are never committed.
- If sample data is needed for tests or examples, it must be sanitized/synthetic and kept in a
  separate, clearly-labeled location — never copied from real company files.

---

## Status

Phase 2, early. The external skill and its package contract already exist. The immediate build focus
is the ingestion layer (file scanning, period parsing, MoM/YoY pairing, anchor-match assertion),
followed by the deterministic metrics layer, then the package writer, then the Claude integration.

See `AGENTS.md` for the engineering guardrails any contributor or agent must follow.
