"""Call the external report-synthesis skill and save its report.

LLM layer — file 2 of 2. This module makes the Anthropic API call and writes the
result. It owns NO package knowledge (``prompt_builder.py`` assembles the
request) and NO business logic — the external skill does all interpretation and
report writing. All call configuration (model, token budget, key) comes from
``config.py`` / ``.env``; nothing is hardcoded here except the SDK call shape.

The package is the source of truth: this layer hands the assembled package to
the skill and persists whatever the skill returns, verbatim.

Key handling is fail-fast: a missing/empty ``ANTHROPIC_API_KEY`` raises a clear
``RuntimeError`` before any network call, rather than letting the SDK surface a
cryptic auth error. API errors are logged (with a status code when available)
and re-raised so ``main.py``'s top-level handler exits cleanly — never swallowed.
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic

from src import config
from src.llm import prompt_builder
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _resolve_model() -> str:
    """The configured model, or the documented default when CLAUDE_MODEL is unset."""
    return config.CLAUDE_MODEL or config.CLAUDE_MODEL_DEFAULT


def _default_output_path(package_dir: Path) -> Path:
    """Derive the report path from the package's run_metadata.json current period."""
    meta_path = package_dir / "run_metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ym = str(meta["current_period"]["start"])[:7]  # "2026-04-01" → "2026-04"
    return config.OUTPUT_REPORTS / f"TikTok_Performance_Report_{ym}.md"


def _split_system(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Split the system-role message out of the array (the SDK wants it separate)."""
    system = next((m["content"] for m in messages if m.get("role") == "system"), None)
    conversation = [m for m in messages if m.get("role") != "system"]
    return system, conversation


def generate_report(package_dir: Path, output_path: Path | None = None) -> Path:
    """Call the skill with the assembled package and write the report to disk.

    Returns the path written. Raises ``RuntimeError`` if the API key is unset and
    re-raises any ``anthropic.APIError`` after logging it.
    """
    if output_path is None:
        output_path = _default_output_path(package_dir)

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example) "
            "before generating a real report, or call generate_report_stub for a "
            "placeholder."
        )

    messages = prompt_builder.build_messages(package_dir)
    system, conversation = _split_system(messages)
    model = _resolve_model()
    approx_input = sum(len(str(m.get("content", ""))) for m in messages) // 4

    logger.info(
        "Calling skill: model=%s, ~%d input tokens, %d message(s) → %s",
        model, approx_input, len(conversation), output_path.name,
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=system,
            messages=conversation,
        )
    except anthropic.APIError as exc:
        status = getattr(exc, "status_code", None)
        logger.error("Anthropic API call failed (status=%s): %s", status, exc)
        raise  # let main.py's top-level handler report and exit cleanly

    report_text = response.content[0].text
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text, encoding="utf-8")
    logger.info("Report written: %s (%d bytes).", output_path, output_path.stat().st_size)
    return output_path


def generate_report_stub(package_dir: Path, output_path: Path | None = None) -> Path:
    """Write a placeholder report (no API call). For use when no key is configured.

    Not called automatically — ``generate_report`` always attempts the real call.
    Kept here so an operator/test can explicitly fall back to a placeholder.
    """
    if output_path is None:
        output_path = _default_output_path(package_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"# TikTok Performance Report — {package_dir.name}\n\n"
        f"_Stub report. The analysis package is at `{package_dir}`._\n\n"
        "The Claude report-synthesis skill was not called (no API key configured "
        "or stub explicitly requested); this placeholder confirms the pipeline ran "
        "end to end.\n",
        encoding="utf-8",
    )
    logger.info("STUB: wrote placeholder report %s (no API call).", output_path.name)
    return output_path
