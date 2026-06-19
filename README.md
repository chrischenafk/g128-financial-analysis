# G128 Financial Analysis
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

The pipeline is feature-complete: all five layers (ingest → transform → analysis → package → LLM),
plus a local report pre-processing layer, are implemented and covered by 128 mocked tests that run
in CI on every push and pull request.

---

## How it works

1. The operator drops the period's standardized TikTok workbook(s) into `data/raw/`.
2. The pipeline scans `data/raw/`, parses reporting periods from filenames, and cross-checks them
   against the period headers inside each workbook.
3. It identifies the latest current period and pairs the relevant month-over-month (MoM) and
   year-over-year (YoY) files for it.
4. It loads, validates, cleans, and normalizes the workbook data.
5. It computes channel-level and SKU-level metrics, MoM/YoY comparisons, historical trend context,
   anomaly flags, and data-quality warnings — all deterministically.
6. It writes a structured **analysis package** to `output/analysis_packages/TikTok_{YYYY-MM}/`.
7. It runs the skill's own pre-processing locally (`src/report/`): `load_package.py` normalizes the
   package to a single `package.json`, and `charts.py` renders the bridge/trend PNGs. The package is
   then slimmed for upload.
8. It calls the **external Claude skill** (via the Claude Platform Skills API) with that package as
   the source of truth. The skill writes and renders the branded report inside its own
   code-execution container and returns a `.docx`.
9. The downloaded `.docx` is saved to `output/reports/` as `G128_TikTok_PM_Report_{YYYY-MM}.docx`,
   and the locally-rendered chart PNGs are injected into it (the container can't mount them itself).
10. A run record is written to `data/processed/run_manifest.json` (audit trail + skip-existing guard),
    and `output/reports/` is never cleaned — it becomes a running archive of every period's report.

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
  PROJECT_CONTEXT.md
  requirements.txt
  conftest.py             # adds repo root to sys.path so `from src import ...` resolves in tests
  .env.example
  .gitignore
  .github/
    workflows/ci.yml      # runs the pytest suite on every push and pull request

  data/
    raw/            # operator drops period workbooks + report_context.md (gitignored; real data)
    processed/      # run_manifest.json + history.sqlite (gitignored)

  output/
    reports/            # final .docx reports, one per period, never cleaned (gitignored)
    analysis_packages/  # versioned packages handed to the skill (gitignored)

  logs/                 # run logs (gitignored)

  src/
    main.py             # 12-step orchestration; CLI --target-period / --force / --context
    config.py           # all paths + constants (schema version, model, skill id, token budgets)

    ingest/
      file_scanner.py     # scan data/raw/, identify candidate workbooks
      period_parser.py    # parse periods from filenames; cross-check workbook headers
      excel_loader.py     # load the Summary + Profit Margin + data-quality sheets

    transform/
      normalize_tiktok.py # clean/normalize sheets into tidy DataFrames
      sku_metrics.py      # SKU-level metric computation
      historical_index.py # local SQLite trailing-history store (SQLAlchemy)

    analysis/
      comparisons.py      # MoM / YoY / trailing comparisons + revenue bridges
      anomalies.py        # deterministic rules-based anomaly flags + materiality gate
      data_quality.py     # the four data-quality sheets → data_quality_warnings

    package/
      writer.py           # serialize computed results to the versioned package contract

    report/               # run the skill's pre-processing locally, before the skill call
      builder.py          # run load_package + charts, slim the package, inject charts into the .docx
      engine/
        load_package.py   # vendored from the skill — normalize the package into one package.json
        charts.py         # vendored from the skill — render bridge/trend PNGs (matplotlib)

    llm/
      claude_client.py    # external skill call via Skills API: upload, pause_turn loop, download .docx
      prompt_builder.py   # assemble the skill request from the package

    utils/
      logger.py
      paths.py

  tests/                  # 128 tests across 13 files, fully mocked (no network, no real data)
    test_file_scanner.py      test_period_parser.py     test_excel_loader.py
    test_normalize_tiktok.py  test_sku_metrics.py       test_historical_index.py
    test_comparisons.py       test_anomalies.py         test_data_quality.py
    test_writer.py            test_report_builder.py    test_llm.py
    test_main.py
```

The modular boundaries (ingest / transform / analysis / package / report / llm) are preserved end to
end — no monolithic script. `src/report/` was added during implementation to run the skill's
deterministic pre-processing (`load_package.py`, `charts.py`) locally, since the skill's file mounting
doesn't work over the API; those two scripts are vendored verbatim from the external skill and never
edited here.

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

## Usage

```bash
# process the latest available period found in data/raw/
python src/main.py

# process a specific period
python src/main.py --target-period 2026-04

# regenerate a report that already exists
python src/main.py --target-period 2026-04 --force

# supply an operator context file (defaults to data/raw/report_context.md)
python src/main.py --context path/to/context.md
```

If a report already exists for the target period (manifest status `complete`), the run skips it
unless `--force` is given. If a filename is malformed or a period is ambiguous, that file is skipped
with a warning; if nothing parses, the run stops with a clear error rather than guessing. If only one
of the MoM/YoY files is present, the run proceeds with a warning that the missing lens reduces
context. The MoM-vs-YoY anchor match (current-period gross/profit must agree to the penny across both
files) is a hard stop — a mismatch aborts the run rather than producing a meaningless comparison.

---

## Claude integration

The external `pm-analysis-code-supplement` skill is reached through the Claude Platform **Skills API**
(`src/llm/claude_client.py`). The integration uploads the pre-processed `package.json` and chart PNGs,
invokes the skill by `skill_id`, drives the `pause_turn` continuation loop while the skill's
code-execution container runs its scripts, and downloads the branded `.docx`. A stub
(`generate_report_stub`) is kept for dry runs and tests so the rest of the pipeline can run end-to-end
without an API key.

- API key, model, and skill settings come from environment variables / `.env`
  (`ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `SKILL_ID`, `SKILL_VERSION`). The real call fails fast with a
  clear error if `ANTHROPIC_API_KEY` or `SKILL_ID` is missing. **Keys are never hardcoded and never
  committed.**
- The skill itself lives **outside this repository** and is deployed separately. This repo treats the
  skill's package schema (version `1.0.0`) as a downstream contract to satisfy, not as code to edit.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in values; never commit .env
```

Requires Python 3.11+ (the project targets 3.12; CI runs on 3.12).

Run the tests:

```bash
pytest tests/ -v --tb=short
```

The suite is fully mocked — no API key, no network, and no real workbooks are needed. CI
(`.github/workflows/ci.yml`) runs exactly this on every push and pull request.

---

## Data handling and privacy

This repository processes **real company financial data**. That data must never enter version control.

- `data/raw/`, `data/processed/`, `output/`, and `logs/` are all gitignored.
- Real `.xlsm` / `.xlsx` / `.csv` / report / `.env` files are never committed.
- If sample data is needed for tests or examples, it must be sanitized/synthetic and kept in a
  separate, clearly-labeled location — never copied from real company files.

---

## Status

Phase 2, feature-complete. All six layers are implemented and wired end to end in `src/main.py`:
ingest (file scanning, period parsing, MoM/YoY pairing, anchor-match assertion) → transform
(normalize, SKU metrics, history store) → analysis (comparisons, anomalies, data quality) → package
(versioned contract writer) → report (local pre-processing + chart rendering) → LLM (the real Skills
API call). The April 2026 regression targets are reproduced to the penny, and 128 mocked tests run in
CI on every push and pull request.

See `AGENTS.md` for the engineering guardrails any contributor or agent must follow, and
`PROJECT_CONTEXT.md` for the detailed layer-by-layer architecture.
