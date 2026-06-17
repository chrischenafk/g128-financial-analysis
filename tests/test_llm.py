"""Tests for the LLM layer (Skills API client).

The Anthropic client is mocked entirely — no real network, no real skill, no real
key. We assert the orchestration: credentials fail-fast, the package is uploaded,
the skill is invoked, the pause_turn loop continues with the container id, the
.docx is downloaded, and the uploaded zip is cleaned up.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import config
from src.llm import claude_client


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / builders
# ─────────────────────────────────────────────────────────────────────────────
def _make_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "TikTok_2026-04"
    pkg.mkdir()
    (pkg / "run_metadata.json").write_text(
        json.dumps({"current_period": {"label": "April 2026", "start": "2026-04-01",
                                       "end": "2026-04-30"}}), encoding="utf-8")
    (pkg / "channel_metrics.json").write_text('{"current": {"gross": 32033.09}}', encoding="utf-8")
    (pkg / "sku_metrics_current.csv").write_text("sku,profit\nFG-A,100\n", encoding="utf-8")
    return pkg


def _response(stop_reason: str, file_ids: tuple[str, ...] = (), container_id: str = "cont_1"):
    """A fake messages response with optional bash-code-execution output files."""
    content: list = []
    if file_ids:
        inner = SimpleNamespace(
            type="bash_code_execution_result",
            content=[SimpleNamespace(file_id=fid) for fid in file_ids],
        )
        content.append(SimpleNamespace(type="bash_code_execution_tool_result", content=inner))
    else:
        content.append(SimpleNamespace(type="text"))
    return SimpleNamespace(stop_reason=stop_reason, content=content,
                           container=SimpleNamespace(id=container_id))


def _client(*, create_returns=None, filenames=None) -> MagicMock:
    """A mocked anthropic client; download.write_to_file actually writes a file."""
    client = MagicMock()
    client.beta.files.upload.return_value = SimpleNamespace(id="file_up_1")

    if create_returns is not None:
        client.beta.messages.create.side_effect = create_returns

    # retrieve_metadata: file_id → filename (default one .docx).
    fmap = filenames or {"file_doc": "G128_TikTok_PM_Report_2026-04.docx"}
    client.beta.files.retrieve_metadata.side_effect = (
        lambda file_id: SimpleNamespace(filename=fmap[file_id]))

    dl = MagicMock()
    dl.write_to_file.side_effect = lambda p: Path(p).write_bytes(b"PK\x03\x04docx")
    client.beta.files.download.return_value = dl
    return client


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setattr(config, "SKILL_ID", "skill_01abc")
    monkeypatch.setattr(config, "SKILL_VERSION", "latest")
    monkeypatch.setattr(config, "CLAUDE_MODEL", None)  # → default model


def _patch_client(monkeypatch, client: MagicMock) -> MagicMock:
    ctor = MagicMock(return_value=client)
    monkeypatch.setattr(claude_client.anthropic, "Anthropic", ctor)
    return ctor


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_uploads_invokes_downloads(tmp_path, monkeypatch, creds) -> None:
    pkg = _make_package(tmp_path)
    client = _client(create_returns=[_response("end_turn", file_ids=("file_doc",))])
    _patch_client(monkeypatch, client)

    result = claude_client.generate_report(pkg, output_dir=tmp_path / "reports")

    assert result == tmp_path / "reports" / "G128_TikTok_PM_Report_2026-04.docx"
    assert result.exists()

    # Uploaded a real zip containing the package files (relative paths preserved).
    upload_kwargs = client.beta.files.upload.call_args.kwargs
    name, blob, mime = upload_kwargs["file"]
    assert name == "package.zip" and mime == "application/zip"
    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
        assert set(zf.namelist()) == {"run_metadata.json", "channel_metrics.json",
                                      "sku_metrics_current.csv"}

    # Initial call carried the skill + the uploaded file_id in the trigger message.
    create_kwargs = client.beta.messages.create.call_args_list[0].kwargs
    assert create_kwargs["model"] == config.CLAUDE_MODEL_DEFAULT
    assert create_kwargs["max_tokens"] == config.REPORT_MAX_TOKENS
    assert create_kwargs["container"]["skills"][0]["skill_id"] == "skill_01abc"
    assert "<file_id>file_up_1</file_id>" in create_kwargs["messages"][0]["content"]
    # Downloaded the docx and cleaned up the upload.
    client.beta.files.download.assert_called_once_with(file_id="file_doc")
    client.beta.files.delete.assert_called_once_with(file_id="file_up_1")


# ─────────────────────────────────────────────────────────────────────────────
# pause_turn loop
# ─────────────────────────────────────────────────────────────────────────────
def test_pause_turn_loop_continues_with_container_id(tmp_path, monkeypatch, creds) -> None:
    pkg = _make_package(tmp_path)
    client = _client(create_returns=[
        _response("pause_turn"),
        _response("pause_turn"),
        _response("end_turn", file_ids=("file_doc",)),
    ])
    _patch_client(monkeypatch, client)

    result = claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    assert result.exists()

    calls = client.beta.messages.create.call_args_list
    assert len(calls) == 3
    # Initial call: container has skills, no id. Continuations: carry container id.
    assert "id" not in calls[0].kwargs["container"]
    assert calls[1].kwargs["container"]["id"] == "cont_1"
    assert calls[2].kwargs["container"]["id"] == "cont_1"
    assert calls[2].kwargs["container"]["skills"][0]["skill_id"] == "skill_01abc"


def test_pause_turn_exhausted_raises(tmp_path, monkeypatch, creds) -> None:
    pkg = _make_package(tmp_path)
    # Always pause_turn → never completes.
    client = _client()
    client.beta.messages.create.return_value = _response("pause_turn")
    _patch_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match="did not complete after 15 continuations"):
        claude_client.generate_report(pkg, output_dir=tmp_path / "reports")


# ─────────────────────────────────────────────────────────────────────────────
# Credential fail-fast (before any network call)
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_skill_id_raises_before_call(tmp_path, monkeypatch) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setattr(config, "SKILL_ID", "")
    ctor = _patch_client(monkeypatch, _client())
    with pytest.raises(RuntimeError, match="SKILL_ID"):
        claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    ctor.assert_not_called()  # no client constructed → no network


def test_missing_api_key_raises_before_call(tmp_path, monkeypatch) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "SKILL_ID", "skill_01abc")
    ctor = _patch_client(monkeypatch, _client())
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    ctor.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# No .docx in the skill output
# ─────────────────────────────────────────────────────────────────────────────
def test_no_docx_raises_with_returned_filenames(tmp_path, monkeypatch, creds) -> None:
    pkg = _make_package(tmp_path)
    client = _client(
        create_returns=[_response("end_turn", file_ids=("file_png",))],
        filenames={"file_png": "bridge_mom.png"},
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match="bridge_mom.png"):
        claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    # Upload still cleaned up even though the run failed.
    client.beta.files.delete.assert_called_once_with(file_id="file_up_1")


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup robustness
# ─────────────────────────────────────────────────────────────────────────────
def test_cleanup_failure_does_not_raise(tmp_path, monkeypatch, creds) -> None:
    pkg = _make_package(tmp_path)
    client = _client(create_returns=[_response("end_turn", file_ids=("file_doc",))])
    client.beta.files.delete.side_effect = RuntimeError("delete failed")
    _patch_client(monkeypatch, client)

    # Delete blows up, but the report still succeeds.
    result = claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    assert result.exists()
    client.beta.files.delete.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Stub (dry run)
# ─────────────────────────────────────────────────────────────────────────────
def test_generate_report_stub_writes_placeholder(tmp_path) -> None:
    pkg = _make_package(tmp_path)
    result = claude_client.generate_report_stub(pkg, output_dir=tmp_path / "reports")
    assert result == tmp_path / "reports" / "G128_TikTok_PM_Report_2026-04.docx"
    assert "STUB report for 2026-04" in result.read_text(encoding="utf-8")
