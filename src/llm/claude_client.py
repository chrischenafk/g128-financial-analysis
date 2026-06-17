"""Call the pm-analysis-code-supplement skill directly via the Skills API.

LLM layer. The skill — deployed on the Claude Platform — owns the entire report
workflow (load_package → charts → report.json → verify → build_doc) and runs
those scripts inside its own code-execution container. This module's whole job is
to (1) zip + upload the analysis package, (2) invoke the skill by ``skill_id``,
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

import io
import json
import zipfile
from pathlib import Path

import anthropic

from src import config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Beta features the Skills API call requires (code execution + skills + files).
BETAS = ["code-execution-2025-08-25", "skills-2025-10-02", "files-api-2025-04-14"]
FILES_BETA_HEADER = {"anthropic-beta": "files-api-2025-04-14"}
CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}

# Doc generation pauses the turn repeatedly while the container runs scripts;
# bound the continuation loop so a stuck skill can't spin forever.
MAX_CONTINUATIONS = 15

_TRIGGER_MESSAGE = (
    "The attached zip contains the structured analysis package produced by the "
    "G128 TikTok Shop Python pipeline (schema version {schema}). "
    "Unzip it, run the full report workflow as specified in SKILL.md "
    "(load_package → charts → report.json → verify → build_doc), "
    "and produce the branded G128_TikTok_PM_Report_.docx. "
    "The package is the source of truth — do not recompute any metric."
)


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


def _zip_package(package_dir: Path) -> bytes:
    """Zip the whole package directory in memory, preserving relative paths."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(package_dir.rglob("*")):  # sorted → deterministic archive
            if path.is_file():
                zf.write(path, path.relative_to(package_dir).as_posix())
    return buf.getvalue()


def _skill_spec() -> dict:
    return {"type": "custom", "skill_id": config.SKILL_ID, "version": config.SKILL_VERSION}


def _skill_create(client: "anthropic.Anthropic", *, messages: list[dict], container: dict):
    """One Skills API ``messages.create`` call, with skill-aware error handling."""
    try:
        return client.beta.messages.create(
            model=_resolve_model(),
            max_tokens=config.REPORT_MAX_TOKENS,
            betas=BETAS,
            container=container,
            tools=[CODE_EXECUTION_TOOL],
            messages=messages,
        )
    except anthropic.BadRequestError as exc:
        if "skill" in str(exc).lower():
            logger.error("Skill call rejected — check SKILL_ID/SKILL_VERSION (%s): %s",
                         config.SKILL_ID, exc)
        else:
            logger.error("Bad request to the messages API: %s", exc)
        raise
    except anthropic.APIError as exc:
        logger.error("Anthropic API error (status=%s): %s", getattr(exc, "status_code", None), exc)
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


def _cleanup_upload(client, file_id: str) -> None:
    """Best-effort delete of the uploaded zip; a failure must not block the run."""
    try:
        client.beta.files.delete(file_id=file_id)
        logger.info("Deleted uploaded package zip %s from the Files API.", file_id)
    except Exception as exc:  # cleanup is optional — never raise from here
        logger.warning("Could not delete uploaded zip %s (%s) — leaving it; run unaffected.",
                       file_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def generate_report(package_dir: Path, output_dir: Path = config.OUTPUT_REPORTS) -> Path:
    """Run the report skill on ``package_dir`` and return the downloaded ``.docx`` path.

    Zips + uploads the package, invokes the skill by ``skill_id``, follows the
    ``pause_turn`` loop while the container builds the report, downloads the
    branded ``.docx`` to ``output_dir/G128_TikTok_PM_Report_{YYYY-MM}.docx``, and
    cleans up the uploaded zip. Raises ``RuntimeError`` on missing credentials or
    when the skill returns no ``.docx``; re-raises Anthropic API errors.
    """
    _require_credentials()  # fail fast, before any network call
    period = _read_period(package_dir)
    output_path = output_dir / f"G128_TikTok_PM_Report_{period}.docx"

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Step 1 — zip + upload the package.
    zip_bytes = _zip_package(package_dir)
    uploaded = client.beta.files.upload(
        file=("package.zip", zip_bytes, "application/zip"),
        extra_headers=FILES_BETA_HEADER,
    )
    logger.info("Uploaded package zip: file_id=%s (%d bytes) for %s.",
                uploaded.id, len(zip_bytes), period)

    try:
        # Step 2 — initial skill call.
        messages: list[dict] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "file",
                            "file_id": uploaded.id,
                        },
                    },
                    {
                        "type": "text",
                        "text": _TRIGGER_MESSAGE.format(schema=config.PACKAGE_SCHEMA_VERSION),
                    },
                ],
            }
        ]
        logger.info("Invoking skill %s (version %s), model=%s.",
                    config.SKILL_ID, config.SKILL_VERSION, _resolve_model())
        response = _skill_create(client, messages=messages, container={"skills": [_skill_spec()]})

        # Step 3 — drive the pause_turn continuation loop.
        response = _drive_to_completion(client, response, messages)

        # Temporary debug — remove after diagnosis
        import json
        for i, item in enumerate(response.content):
            logger.debug(f"response.content[{i}]: type={item.type}")
            if hasattr(item, 'text'):
                logger.debug(f"  text={item.text[:500]}")
            if item.type == "bash_code_execution_tool_result":
                logger.debug(f"  tool_result={str(item)[:500]}")

        # Step 4 — extract file IDs and download the .docx.
        _download_docx(client, response, output_path)
    finally:
        # Step 5 — clean up the uploaded zip regardless of outcome.
        _cleanup_upload(client, uploaded.id)

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
