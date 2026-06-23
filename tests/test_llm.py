"""Tests for the LLM layer (Skills API client).

The Anthropic client is mocked entirely — no real network, no real skill, no real
key. We assert the orchestration: credentials fail-fast, the package is uploaded,
the skill is invoked, the pause_turn loop continues with the container id, the
.docx is downloaded, and the uploaded zip is cleaned up.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import config
from src.llm import claude_client

# The package files _upload_package_files will include (.json / .csv / .md).
PACKAGE_FILES = {"run_metadata.json", "channel_metrics.json", "sku_metrics_current.csv"}


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


def _stream_cm(response):
    """A context-manager mock matching ``with client.beta.messages.stream(...) as s``.

    ``s.get_final_message()`` returns ``response`` (the client now streams each
    call instead of using ``messages.create``).
    """
    cm = MagicMock()
    cm.__enter__.return_value.get_final_message.return_value = response
    cm.__exit__.return_value = False
    return cm


def _client(*, create_returns=None, filenames=None) -> MagicMock:
    """A mocked anthropic client; download.write_to_file actually writes a file."""
    client = MagicMock()
    # Each upload returns a distinct id derived from the filename it was given.
    client.beta.files.upload.side_effect = (
        lambda file, extra_headers=None: SimpleNamespace(id=f"up_{file[0]}"))

    # Each stream(...) call yields a CM resolving to the next queued response.
    if create_returns is not None:
        client.beta.messages.stream.side_effect = [_stream_cm(r) for r in create_returns]

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

    # Each package file uploaded individually (not a single zip).
    uploaded_names = [c.kwargs["file"][0] for c in client.beta.files.upload.call_args_list]
    assert set(uploaded_names) == PACKAGE_FILES
    assert client.beta.files.upload.call_count == len(PACKAGE_FILES)

    # Initial call carried the skill + one document block per file + a trigger text.
    create_kwargs = client.beta.messages.stream.call_args_list[0].kwargs
    assert create_kwargs["model"] == config.CLAUDE_MODEL_DEFAULT
    assert create_kwargs["max_tokens"] == config.REPORT_MAX_TOKENS
    assert create_kwargs["container"]["skills"][0]["skill_id"] == "skill_01abc"
    content = create_kwargs["messages"][0]["content"]
    doc_blocks = [b for b in content if b["type"] == "document"]
    assert len(doc_blocks) == len(PACKAGE_FILES)
    assert all(b["source"] == {"type": "file", "file_id": f"up_{b['title']}"} for b in doc_blocks)
    assert content[-1]["type"] == "text"  # trigger text comes last
    # Downloaded the docx and cleaned up every upload.
    client.beta.files.download.assert_called_once_with(file_id="file_doc")
    assert client.beta.files.delete.call_count == len(PACKAGE_FILES)


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

    calls = client.beta.messages.stream.call_args_list
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
    client.beta.messages.stream.side_effect = lambda **kw: _stream_cm(_response("pause_turn"))
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
    # Uploads still cleaned up even though the run failed.
    assert client.beta.files.delete.call_count == len(PACKAGE_FILES)


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
    assert client.beta.files.delete.call_count == len(PACKAGE_FILES)  # all attempted


# ─────────────────────────────────────────────────────────────────────────────
# Transient-error retry
# ─────────────────────────────────────────────────────────────────────────────
class _FakeOverloaded(claude_client.anthropic.APIStatusError):
    """An ``overloaded_error`` as it surfaces mid-stream: HTTP 200, error in body."""

    def __init__(self) -> None:  # deliberately skip the SDK's response/body machinery
        self.status_code = 200
        self.body = {"type": "error",
                     "error": {"type": "overloaded_error", "message": "Overloaded"}}

    def __str__(self) -> str:
        return "Overloaded"


class _FakeBadRequest(claude_client.anthropic.BadRequestError):
    """A non-transient 400 — must NOT be retried."""

    def __init__(self) -> None:
        self.status_code = 400
        self.body = {"error": {"type": "invalid_request_error"}}

    def __str__(self) -> str:
        return "bad skill request"


@pytest.fixture()
def _no_sleep(monkeypatch):
    """Make backoff instant so retry tests don't actually wait."""
    monkeypatch.setattr(claude_client.time, "sleep", lambda _s: None)


def test_overloaded_error_retries_then_succeeds(tmp_path, monkeypatch, creds, _no_sleep) -> None:
    pkg = _make_package(tmp_path)
    client = _client()
    # First stream call raises overloaded mid-stream; the retry succeeds.
    client.beta.messages.stream.side_effect = [
        _FakeOverloaded(),
        _stream_cm(_response("end_turn", file_ids=("file_doc",))),
    ]
    _patch_client(monkeypatch, client)

    result = claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    assert result.exists()
    assert client.beta.messages.stream.call_count == 2  # one failure + one success


def test_overloaded_error_exhausts_retries_then_raises(tmp_path, monkeypatch, creds, _no_sleep) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 2)  # 1 initial + 2 retries = 3 tries
    client = _client()
    client.beta.messages.stream.side_effect = [_FakeOverloaded() for _ in range(3)]
    _patch_client(monkeypatch, client)

    with pytest.raises(claude_client.anthropic.APIStatusError):
        claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    assert client.beta.messages.stream.call_count == 3
    # Uploaded files are still cleaned up despite the failure.
    assert client.beta.files.delete.call_count == len(PACKAGE_FILES)


def test_bad_request_is_not_retried(tmp_path, monkeypatch, creds, _no_sleep) -> None:
    pkg = _make_package(tmp_path)
    client = _client()
    client.beta.messages.stream.side_effect = [_FakeBadRequest(), _FakeBadRequest()]
    _patch_client(monkeypatch, client)

    with pytest.raises(claude_client.anthropic.BadRequestError):
        claude_client.generate_report(pkg, output_dir=tmp_path / "reports")
    assert client.beta.messages.stream.call_count == 1  # no retry on a 400


def test_is_retryable_classification() -> None:
    assert claude_client._is_retryable(_FakeOverloaded()) is True
    assert claude_client._is_retryable(_FakeBadRequest()) is False
    assert claude_client._error_type(_FakeOverloaded()) == "overloaded_error"


def test_retry_delay_bounded_and_grows(monkeypatch) -> None:
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY", 2.0)
    monkeypatch.setattr(config, "LLM_RETRY_MAX_DELAY", 60.0)
    # Equal jitter: each delay sits in [ceiling/2, ceiling]; never exceeds the cap.
    for attempt in range(8):
        d = claude_client._retry_delay(attempt)
        ceiling = min(60.0, 2.0 * (2 ** attempt))
        assert ceiling / 2 <= d <= ceiling


# ─────────────────────────────────────────────────────────────────────────────
# Stub (dry run)
# ─────────────────────────────────────────────────────────────────────────────
def test_generate_report_stub_writes_placeholder(tmp_path) -> None:
    pkg = _make_package(tmp_path)
    result = claude_client.generate_report_stub(pkg, output_dir=tmp_path / "reports")
    assert result == tmp_path / "reports" / "G128_TikTok_PM_Report_2026-04.docx"
    assert "STUB report for 2026-04" in result.read_text(encoding="utf-8")
