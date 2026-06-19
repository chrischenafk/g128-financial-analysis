"""Call the pm-analysis-code-supplement skill directly via the Skills API.

LLM layer. The skill — deployed on the Claude Platform — owns the entire report
workflow (load_package → charts → report.json → verify → build_doc) and runs
those scripts inside its own code-execution container. This module's whole job is
to (1) upload the analysis package files, (2) invoke the skill by ``skill_id``,
(3) drive the ``pause_turn`` continuation loop while the container works, and
(4) download the branded ``.docx`` it produces.

Because the skill does all interpretation and rendering, this pipeline no longer
assembles a prompt or vendors the report scripts — there is no system prompt
here, only a concise trigger message pointing at the uploaded package. The
package remains the source of truth; the skill's own ``verify.py`` keeps every
figure in the doc traceable to it.

Fail-fast: a missing ``ANTHROPIC_API_KEY`` or ``SKILL_ID`` raises before any
network call. API errors are logged (skill-specific failures called out) and
re-raised so ``main.py``'s top-level handler exits cleanly — never swallowed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic

from src import config
from src.utils.logger import get_logger

if TYPE_CHECKING:  # for the type hint only — avoids a runtime llm→report dependency
    from src.report.builder import ReportInputs

logger = get_logger(__name__)

# Beta features the Skills API call requires (code execution + skills + files).
BETAS = ["code-execution-2025-08-25", "skills-2025-10-02", "files-api-2025-04-14", "prompt-caching-2024-07-31"]
FILES_BETA_HEADER = {"anthropic-beta": "files-api-2025-04-14"}
CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}

# Doc generation pauses the turn repeatedly while the container runs scripts;
# bound the continuation loop so a stuck skill can't spin forever.
MAX_CONTINUATIONS = 15

_TRIGGER_MESSAGE = (
    "The attached files are the structured analysis package produced by the "
    "G128 TikTok Shop Python pipeline (schema version {schema}). "
    "Run the full report workflow as specified in SKILL.md "
    "(load_package → charts → report.json → verify → build_doc), "
    "and produce the branded G128_TikTok_PM_Report_.docx. "
    "The package is the source of truth — do not recompute any metric."
)

# Used when load_package.py + charts.py have already run locally (the preferred
# path): the skill receives the processed package.json and starts at Step 4.
_TRIGGER_MESSAGE_PROCESSED = (
    "The attached package.json is the pre-processed analysis package produced by load_package.py "
    "(schema version {schema}). Run the report workflow from Step 4 onward as specified in SKILL.md "
    "(report.json → verify → build_doc) — load_package.py has already been run locally.\n"
    "Note: comparisons.mom/yoy and sku_current raw arrays have been removed to reduce size — "
    "use ranked.mom_winners, ranked.mom_losers, ranked.yoy_winners, ranked.yoy_losers, "
    "ranked.top_profit_current, and ranked.structural_movers for all SKU analysis.\n"
    "The exec_summary.verdict field must be a single SHORT sentence (max 20 words) — "
    "the punchy one-line read on the month. Save the detail for exec_summary.paragraphs.\n"
    "WRITING STYLE — apply to every section of the report:\n"
    "Lead with the business insight, not the data. Every paragraph should open with a plain-English "
    "sentence a non-financial reader can act on, then support it with the specific numbers. "
    "Never open a paragraph with a SKU ID, a raw figure, or a pipeline rule citation. "
    "Example of WRONG style (data-first): "
    "'FG-3BLAH-4P2 had ad_spend of $907.28 (ad_share 0.487) against profit of $306 at 7.73% margin.' "
    "Example of RIGHT style (insight-first): "
    "'Black American Heritage is spending nearly half the ad budget for a 7.7% return — "
    "the clearest cost problem in the catalog this month. At $907 in ads against $306 in profit, "
    "the math only works if those ads are buying future volume, and the YoY decline suggests they are not.' "
    "SKU IDs belong in parentheses after the product name, never leading a sentence. "
    "Pipeline rule citations (rule A, rule B, etc.) belong in the appendix or in parenthetical notes, "
    "never in the body of a section. Evidence dict field names (ad_share, profit_delta_yoy, etc.) "
    "must never appear in the report prose — translate them into plain English. "
    "Keep sections concise. If a point is made, move on. Avoid restating the same figure "
    "in multiple consecutive sentences. Target 2–4 sentences per paragraph, not 6–8.\n"
    "EXECUTIVE SUMMARY — specific rules:\n"
    "The exec_summary.paragraphs must read as if written for a business owner with 2 minutes. "
    "No SKU IDs, no percentages, no pipeline rule references in the first paragraph. "
    "Just the plain English story: what happened this month, why, and the one thing to watch. "
    "Bring in specific numbers only in the second and third paragraphs, where they support "
    "a point already made in plain language. "
    "The scorecard tiles carry the headline numbers — the paragraphs explain what they mean, "
    "not what they are. Do not restate the scorecard figures in the opening sentence. "
    "Example of WRONG exec summary opening: "
    "'April delivered $7,595 in profit at a 23.7% margin — 5 points better than March but "
    "5.8 points below April 2025.' "
    "Example of RIGHT exec summary opening: "
    "'April was a normal seasonal step-down from March — smaller volume, but the business "
    "is running more efficiently and is substantially larger than it was a year ago. "
    "The one number to watch is margin: we are buying growth with ad and affiliate spend "
    "that did not exist a year ago, and the strategic question is whether that spend is "
    "building the business or just renting volume.' "
    "The verdict sentence (exec_summary.verdict) should be the one-line read on the month "
    "in plain language — max 15 words, no numbers, no SKU IDs.\n"
    "Section headings in the sections[] array MUST include their section number prefix "
    "(e.g. '2. Current Period Snapshot', '3. MoM Performance', '4. YoY / Seasonality Context', etc.). "
    "The ONE exception: do NOT write a '11. Recommended Action Plan' section entry — "
    "build_doc.js renders the action_plan array as its own titled block automatically, "
    "so writing it in sections[] creates an empty duplicate. Number all other sections normally.\n"
    "key_insights headlines MUST start with the plain English product name, never the SKU ID. "
    "Format: '[Product name] — [one punchy observation under 10 words]'. "
    "The SKU ID goes in parentheses after the name if needed, never at the start. "
    "CORRECT: 'Black American Heritage flag absorbs half the ad budget at thin margin'. "
    "WRONG: 'FG-3BLAH-4P2 absorbs 49% of ad budget — both lenses negative'. "
    "WRONG: 'RISK FG-3BLAH-4P2 absorbs...' — never start with a tag or SKU ID.\n"
    "Target report length: 8-10 pages equivalent. "
    "Do not exceed 5,500 words total across all sections combined. "
    "If a section is complete, move on — do not pad or restate.\n"
    "{chart_note}\n"
    "The package is the source of truth — do not recompute any metric."
)
_CHART_NOTE_PRESENT = (
    "The attached chart image(s) are the pre-generated bridge/trend PNGs — "
    "reference them in the report sections by their filename."
)
_CHART_NOTE_ABSENT = "No charts were generated for this package."


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_model() -> str:
    """Configured model, or the documented default when CLAUDE_MODEL is unset."""
    return config.CLAUDE_MODEL or config.CLAUDE_MODEL_DEFAULT


def _require_credentials() -> None:
    """Raise (before any network call) if a required setting is missing."""
    missing = [name for name, value in
               (("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY), ("SKILL_ID", config.SKILL_ID))
               if not value]
    if missing:
        raise RuntimeError(
            f"Missing required setting(s): {', '.join(missing)}. Set them in .env "
            "(see .env.example) before calling the report skill."
        )


def _read_period(package_dir: Path) -> str:
    """The 'YYYY-MM' period label from the package's run_metadata.json."""
    meta = json.loads((package_dir / "run_metadata.json").read_text(encoding="utf-8"))
    return str(meta["current_period"]["start"])[:7]  # "2026-04-01" → "2026-04"


def _upload_package_files(client, source: Path) -> list[tuple[str, str]]:
    """Upload package file(s) as plaintext. Returns list of (filename, file_id).

    ``source`` may be a directory (uploads each .json/.csv/.md file — the raw
    fallback path) or a single file (uploads just that one — e.g. the processed
    ``package.json``).
    """
    if source.is_file():
        targets = [source]
    else:
        INCLUDE_EXTENSIONS = {".json", ".csv", ".md"}  # ordered: skill sees JSON first
        targets = [p for p in sorted(source.iterdir())
                   if p.is_file() and p.suffix.lower() in INCLUDE_EXTENSIONS]
    uploaded: list[tuple[str, str]] = []
    for path in targets:
        content = path.read_bytes()
        result = client.beta.files.upload(
            file=(path.name, content, "text/plain"),  # API accepts plaintext; our files are text
            extra_headers=FILES_BETA_HEADER,
        )
        uploaded.append((path.name, result.id))
        logger.info("Uploaded %s → file_id=%s (%d bytes).", path.name, result.id, len(content))
    if not uploaded:
        raise RuntimeError(f"No uploadable files found: {source}")
    return uploaded


def _upload_charts(client, charts: list[Path]) -> list[tuple[str, str]]:
    """Upload chart PNGs (consumed as image inputs). Returns list of (filename, file_id)."""
    uploaded: list[tuple[str, str]] = []
    for png in charts:
        content = png.read_bytes()
        result = client.beta.files.upload(
            file=(png.name, content, "image/png"),
            extra_headers=FILES_BETA_HEADER,
        )
        uploaded.append((png.name, result.id))
        logger.info("Uploaded chart %s → file_id=%s (%d bytes).", png.name, result.id, len(content))
    return uploaded


def _skill_spec() -> dict:
    return {"type": "custom", "skill_id": config.SKILL_ID, "version": config.SKILL_VERSION}


def _skill_create(client, *, messages: list[dict], container: dict):
    """One Skills API call using streaming to support long-running operations.

    Doc generation can exceed the 10-minute non-streaming ceiling, so each call is
    made with ``messages.stream`` and resolved via ``get_final_message`` — the
    pause_turn loop in ``_drive_to_completion`` is unchanged (streaming is
    per-call). Skill-aware error handling is preserved.
    """
    try:
        with client.beta.messages.stream(
            model=_resolve_model(),
            max_tokens=config.REPORT_MAX_TOKENS,
            betas=BETAS,
            container=container,
            tools=[CODE_EXECUTION_TOOL],
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        return response
    except anthropic.BadRequestError as exc:
        if "skill" in str(exc).lower():
            logger.error("Skill call rejected — check SKILL_ID/SKILL_VERSION (%s): %s",
                         config.SKILL_ID, exc)
        else:
            logger.error("Bad request to the messages API: %s", exc)
        raise
    except anthropic.APIError as exc:
        logger.error("Anthropic API error (status=%s): %s",
                     getattr(exc, "status_code", None), exc)
        raise


def _drive_to_completion(client, response, messages: list[dict]):
    """Follow ``pause_turn`` continuations until the skill finishes (or give up)."""
    for i in range(MAX_CONTINUATIONS):
        if response.stop_reason != "pause_turn":
            return response
        logger.debug("pause_turn continuation %d/%d (container=%s)",
                     i + 1, MAX_CONTINUATIONS, response.container.id)
        messages.append({"role": "assistant", "content": response.content})
        response = _skill_create(
            client, messages=messages,
            container={"id": response.container.id, "skills": [_skill_spec()]},
        )
    raise RuntimeError(
        f"Skill did not complete after {MAX_CONTINUATIONS} continuations — still pause_turn."
    )


def _extract_file_ids(response) -> list[str]:
    """File IDs produced by the container's bash code-execution results."""
    file_ids: list[str] = []
    for item in response.content:
        if getattr(item, "type", None) == "bash_code_execution_tool_result":
            content_item = item.content
            if getattr(content_item, "type", None) == "bash_code_execution_result":
                for f in content_item.content:
                    file_ids.append(f.file_id)
    return file_ids


def _download_docx(client, response, output_path: Path) -> None:
    """Find the .docx among the skill's output files and download it."""
    named = [(fid, client.beta.files.retrieve_metadata(file_id=fid).filename)
             for fid in _extract_file_ids(response)]
    docx = [(fid, name) for fid, name in named if name.lower().endswith(".docx")]
    if not docx:
        returned = [name for _, name in named]
        logger.warning("Skill produced no .docx. Files returned: %s", returned or "none")
        raise RuntimeError(
            f"Skill completed but returned no .docx. Files returned: {returned or 'none'}."
        )
    file_id, filename = docx[0]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    client.beta.files.download(file_id=file_id).write_to_file(str(output_path))
    logger.info("Report downloaded: %s (%d bytes, source skill file %r).",
                output_path, output_path.stat().st_size, filename)


def _cleanup_uploads(client, uploads: list[tuple[str, str]]) -> None:
    """Best-effort delete of all uploaded files; failures must not block the run."""
    for filename, file_id in uploads:
        try:
            client.beta.files.delete(file_id=file_id)
            logger.debug("Deleted uploaded file %s (%s).", filename, file_id)
        except Exception as exc:
            logger.warning(
                "Could not delete uploaded file %s (%s): %s — leaving it; run unaffected.",
                filename, file_id, exc,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def generate_report(
    package_dir: Path,
    report_inputs: "ReportInputs | None" = None,
    output_dir: Path = config.OUTPUT_REPORTS,
) -> Path:
    """Run the report skill and return the downloaded ``.docx`` path.

    Preferred path (``report_inputs`` provided): upload the locally pre-processed
    ``package.json`` as a document block plus each chart PNG as an image block, and
    tell the skill to start from Step 4 (load_package already ran locally).
    Fallback path (``report_inputs`` is ``None``): upload the raw package files and
    run the full workflow — preserved for tests and dry runs.

    Invokes the skill by ``skill_id``, follows the ``pause_turn`` loop, downloads
    the branded ``.docx`` to ``output_dir/G128_TikTok_PM_Report_{YYYY-MM}.docx``,
    and cleans up all uploads. Raises ``RuntimeError`` on missing credentials or
    when the skill returns no ``.docx``; re-raises Anthropic API errors.
    """
    _require_credentials()  # fail fast, before any network call
    period = _read_period(package_dir)
    output_path = output_dir / f"G128_TikTok_PM_Report_{period}.docx"

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Step 1 — upload inputs and build the user message content blocks.
    if report_inputs is not None:
        doc_uploads = _upload_package_files(client, report_inputs.package_json)   # single package.json
        chart_uploads = _upload_charts(client, report_inputs.charts)              # image blocks
        uploads = doc_uploads + chart_uploads
        logger.info("Uploaded package.json + %d chart(s) for %s.", len(chart_uploads), period)
        content_blocks: list[dict] = [
            {"type": "document", "source": {"type": "file", "file_id": fid}, "title": name}
            for name, fid in doc_uploads
        ]
        for name, fid in chart_uploads:
            # Label each image so the skill can name it (charts/<file>) in report.json
            # → build_doc.js then resolves the section's chart path.
            content_blocks.append({"type": "text", "text": f"Chart: {name}"})
            content_blocks.append({"type": "image", "source": {"type": "file", "file_id": fid}})
        chart_note = _CHART_NOTE_PRESENT if report_inputs.charts else _CHART_NOTE_ABSENT
        content_blocks.append({
            "type": "text",
            "text": _TRIGGER_MESSAGE_PROCESSED.format(
                schema=config.PACKAGE_SCHEMA_VERSION, chart_note=chart_note),
        })
    else:
        uploads = _upload_package_files(client, package_dir)  # raw files (backward compatible)
        logger.info("Uploaded %d package file(s) for %s.", len(uploads), period)
        content_blocks = [
            {"type": "document", "source": {"type": "file", "file_id": fid}, "title": name}
            for name, fid in uploads
        ]
        content_blocks.append({
            "type": "text",
            "text": _TRIGGER_MESSAGE.format(schema=config.PACKAGE_SCHEMA_VERSION),
        })

    try:
        # Step 2 — invoke the skill.
        messages: list[dict] = [{"role": "user", "content": content_blocks}]
        logger.info("Invoking skill %s (version %s), model=%s.",
                    config.SKILL_ID, config.SKILL_VERSION, _resolve_model())
        response = _skill_create(
            client, messages=messages, container={"skills": [_skill_spec()]}
        )

        # Step 3 — drive the pause_turn continuation loop
        response = _drive_to_completion(client, response, messages)

        # Temporary debug — remove after diagnosis
        for i, item in enumerate(response.content):
            logger.debug("response.content[%d]: type=%s", i, item.type)
            if hasattr(item, "text"):
                logger.debug("  text=%s", item.text[:300])
            if getattr(item, "type", None) == "bash_code_execution_tool_result":
                logger.debug("  tool_result=%s", str(item)[:400])

        # Step 4 — extract file IDs and download the .docx
        _download_docx(client, response, output_path)

    finally:
        # Step 5 — clean up all uploaded files regardless of outcome
        _cleanup_uploads(client, uploads)

    return output_path


def generate_report_stub(package_dir: Path, output_dir: Path = config.OUTPUT_REPORTS) -> Path:
    """Write a placeholder .docx-named report (no API call) for dry runs/tests.

    Not on the default path — ``main.py`` calls ``generate_report``. Kept so a run
    can be exercised end-to-end without a skill/API key.
    """
    period = _read_period(package_dir)
    output_path = output_dir / f"G128_TikTok_PM_Report_{period}.docx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"STUB report for {period}. The analysis package is at {package_dir}. "
        "The report skill was not called (stub path).\n",
        encoding="utf-8",
    )
    logger.info("STUB: wrote placeholder %s (no skill call).", output_path.name)
    return output_path
