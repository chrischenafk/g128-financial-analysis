"""Single source of truth for paths and settings.

Everything in the pipeline imports its paths and constants from here — no magic
strings scattered across modules.

This module ONLY DECLARES. It does not create directories or write anything to
the filesystem on import. Runtime directory creation lives in
``src/utils/paths.py`` (and the one logging exception in ``src/utils/logger.py``).
The single filesystem touch here is ``load_dotenv()``, which *reads* an optional
``.env`` file if present — it never writes.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Project root — derived from this file's location, so paths resolve correctly
# no matter what the current working directory is.
#   this file:  <PROJECT_ROOT>/src/config.py
#   .parent  -> <PROJECT_ROOT>/src
#   .parent  -> <PROJECT_ROOT>
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# ── Input data ───────────────────────────────────────────────────────────────
DATA_RAW: Path = PROJECT_ROOT / "data" / "raw"              # operator drops workbooks here
DATA_PROCESSED: Path = PROJECT_ROOT / "data" / "processed"  # run manifest + history store

# ── Generated output ─────────────────────────────────────────────────────────
OUTPUT_REPORTS: Path = PROJECT_ROOT / "output" / "reports"            # final reports (never cleaned)
OUTPUT_PACKAGES: Path = PROJECT_ROOT / "output" / "analysis_packages" # versioned packages for the skill

# ── Logs ─────────────────────────────────────────────────────────────────────
LOGS: Path = PROJECT_ROOT / "logs"

# ── Specific files inside the above folders ──────────────────────────────────
HISTORY_DB: Path = DATA_PROCESSED / "history.sqlite"   # local trailing-history store (Level 2)
MANIFEST: Path = DATA_PROCESSED / "run_manifest.json"  # auditable record of each run

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
# Package contract version. Locked at "1.0.0" with the external skill (the
# package layer / src/package/writer.py emits exactly this contract). Changing
# the package shape is a coordinated, versioned act — never done unilaterally;
# a shape change must bump this and be agreed with the skill author.
PACKAGE_SCHEMA_VERSION: str = "1.0.0"

MARKETPLACE: str = "TikTok Shop"  # current scope — single marketplace
CURRENCY: str = "USD"

# ── LLM layer settings ───────────────────────────────────────────────────────
# Model + token budget for the external skill call. The model is overridable via
# the CLAUDE_MODEL env var (below); this is the fallback when it is unset. Kept
# here (not hardcoded in the llm layer) so the call is configured in one place.
CLAUDE_MODEL_DEFAULT: str = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS: int = 8192

# Token budget for the report-skill call specifically. Producing the full
# branded .docx (report.json → verify → build_doc inside the skill's container)
# from a large package.json needs a high output ceiling, so it gets its own
# limit while CLAUDE_MAX_TOKENS stays for any other use.
REPORT_MAX_TOKENS: int = 20000

# ─────────────────────────────────────────────────────────────────────────────
# Environment-backed settings (read-only load; values come from .env / the
# environment). Secrets are never hardcoded and never logged.
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env")

ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL: str | None = os.getenv("CLAUDE_MODEL")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Skills API: the pm-analysis-code-supplement skill that owns all report logic.
# SKILL_ID empty-by-default so the llm layer's missing-setting check fires
# cleanly; pin SKILL_VERSION to a specific version in production (not "latest").
SKILL_ID: str = os.getenv("SKILL_ID", "")
SKILL_VERSION: str = os.getenv("SKILL_VERSION", "latest")