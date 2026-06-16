"""Assemble the Anthropic request from an analysis package.

LLM layer — file 1 of 2. This module knows the package directory layout;
``claude_client.py`` does not. Its single job is to read the package files and
build the deterministic ``messages`` array the external skill consumes. It does
NOT call the API, choose a model, or know about keys/token limits.

Boundary: the package is the **source of truth**. This builder only transcribes
the package files verbatim into a labeled prompt — it never recomputes, reshapes,
or interprets a metric. All interpretation and report writing happen inside the
external skill on the Claude Platform.

Determinism: the same package directory always produces byte-identical messages.
Files are read in a fixed order and missing optional files are simply skipped
(logged), so the assembly is reproducible across runs.
"""

from __future__ import annotations

from pathlib import Path

from src.utils.logger import get_logger

logger = get_logger(__name__)

# The skill's role. Content (not configuration) — safe to live here.
SYSTEM_PROMPT = (
    "You are a financial reporting assistant for G128, a TikTok Shop seller. You "
    "will be provided with a structured analysis package produced by a "
    "deterministic Python pipeline. The package is the source of truth — do not "
    "recompute any metric. Your job is to write a concise, decision-oriented "
    "business report from this package following the 12-section structure."
)

# Closing instruction appended after the package contents.
CLOSING_INSTRUCTION = (
    "Using the package above, write the full business report. Every number you "
    "cite must come from the package. Surface every data_quality_warnings item in "
    "the Data Quality Caveats section. Each recommendation must cite a specific "
    "figure."
)

# Package files to include, in a FIXED order (determinism). Every file is
# optional from this builder's perspective: a missing one is skipped + logged,
# mirroring the skill's own graceful degradation on absent optional files.
PACKAGE_FILES: tuple[str, ...] = (
    "run_metadata.json",
    "channel_metrics.json",
    "sku_metrics_current.csv",
    "sku_comparisons_mom.csv",
    "sku_comparisons_yoy.csv",
    "sku_historical_trends.csv",
    "anomaly_flags.json",
    "data_quality_warnings.json",
    "report_context.md",
)


def _labeled_block(filename: str, contents: str) -> str:
    """Wrap one file's contents in a labeled fence the skill can split on."""
    return f"--- {filename} ---\n{contents}\n"


def build_messages(package_dir: Path) -> list[dict]:
    """Build the Anthropic ``messages`` array for one analysis package.

    Returns a two-message conversation: a ``system`` role with the skill's
    mandate and a ``user`` role carrying every present package file (each in a
    labeled block) followed by the closing instruction. Missing files are skipped
    with a WARNING — never a crash.
    """
    blocks: list[str] = []
    included: list[str] = []
    for filename in PACKAGE_FILES:
        path = package_dir / filename
        if not path.exists():
            logger.warning("Package file %r absent from %s — skipping.", filename, package_dir.name)
            continue
        blocks.append(_labeled_block(filename, path.read_text(encoding="utf-8")))
        included.append(filename)

    user_content = (
        "Here is the analysis package. Each file is wrapped in a "
        "'--- <filename> ---' block.\n\n"
        + "\n".join(blocks)
        + "\n"
        + CLOSING_INSTRUCTION
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    approx_tokens = len(SYSTEM_PROMPT + user_content) // 4  # rough: ~4 chars/token
    logger.info(
        "Built messages from %s: %d/%d package file(s) included %s (~%d tokens).",
        package_dir.name, len(included), len(PACKAGE_FILES), included, approx_tokens,
    )
    return messages
