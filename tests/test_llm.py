"""Tests for the LLM layer (prompt_builder + claude_client).

No real API calls and no real keys: the Anthropic client is mocked and the key
is monkeypatched. The package is synthetic stub files in a tmp directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import config
from src.llm import claude_client, prompt_builder


def _make_package(tmp_path: Path, *, with_history: bool = True) -> Path:
    pkg = tmp_path / "TikTok_2026-04"
    pkg.mkdir()
    (pkg / "run_metadata.json").write_text(
        json.dumps({"current_period": {"label": "April 2026", "start": "2026-04-01",
                                       "end": "2026-04-30"}}), encoding="utf-8")
    (pkg / "channel_metrics.json").write_text('{"current": {"gross": 32033.09}}', encoding="utf-8")
    (pkg / "sku_metrics_current.csv").write_text("sku,profit\nFG-A,100\n", encoding="utf-8")
    (pkg / "sku_comparisons_mom.csv").write_text("sku,profit_delta\nFG-A,-50\n", encoding="utf-8")
    (pkg / "sku_comparisons_yoy.csv").write_text("sku,profit_delta\nFG-A,20\n", encoding="utf-8")
    (pkg / "anomaly_flags.json").write_text('[{"sku": "FG-A", "kind": "both_lenses_down"}]',
                                            encoding="utf-8")
    (pkg / "data_quality_warnings.json").write_text('[{"code": "unsettled_payouts"}]',
                                                    encoding="utf-8")
    (pkg / "report_context.md").write_text("No additional operator context.\n", encoding="utf-8")
    if with_history:
        (pkg / "sku_historical_trends.csv").write_text(
            "sku,period_label,profit\nFG-A,March 2026,90\n", encoding="utf-8")
    return pkg


# ─────────────────────────────────────────────────────────────────────────────
# prompt_builder
# ─────────────────────────────────────────────────────────────────────────────
def test_build_messages_structure_and_labels(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path)
    messages = prompt_builder.build_messages(pkg)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert "financial reporting assistant for G128" in messages[0]["content"]

    user = messages[1]["content"]
    for filename in ("run_metadata.json", "channel_metrics.json", "sku_metrics_current.csv",
                     "sku_comparisons_mom.csv", "sku_comparisons_yoy.csv",
                     "sku_historical_trends.csv", "anomaly_flags.json",
                     "data_quality_warnings.json", "report_context.md"):
        assert f"--- {filename} ---" in user
    # Closing instruction present; actual file contents transcribed verbatim.
    assert "write the full business report" in user
    assert "32033.09" in user


def test_build_messages_is_deterministic(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path)
    assert prompt_builder.build_messages(pkg) == prompt_builder.build_messages(pkg)


def test_build_messages_skips_missing_optional_file(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path, with_history=False)  # no sku_historical_trends.csv
    user = prompt_builder.build_messages(pkg)[1]["content"]
    assert "--- sku_historical_trends.csv ---" not in user
    assert "--- channel_metrics.json ---" in user  # the rest still included, no crash


# ─────────────────────────────────────────────────────────────────────────────
# claude_client
# ─────────────────────────────────────────────────────────────────────────────
def test_generate_report_missing_key_raises_before_network(tmp_path: Path, monkeypatch) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    # If a client were constructed, this would explode — proving no network attempt.
    monkeypatch.setattr(claude_client.anthropic, "Anthropic",
                        MagicMock(side_effect=AssertionError("client must not be built")))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        claude_client.generate_report(pkg, tmp_path / "out.md")


def test_generate_report_calls_sdk_and_writes_output(tmp_path: Path, monkeypatch) -> None:
    pkg = _make_package(tmp_path)
    out = tmp_path / "report.md"
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setattr(config, "CLAUDE_MODEL", None)  # → default model

    fake_response = SimpleNamespace(content=[SimpleNamespace(text="# Report\n\nBody.")])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    anthropic_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(claude_client.anthropic, "Anthropic", anthropic_ctor)

    result = claude_client.generate_report(pkg, out)

    assert result == out
    assert out.read_text(encoding="utf-8") == "# Report\n\nBody."
    anthropic_ctor.assert_called_once_with(api_key="sk-test-123")
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["model"] == config.CLAUDE_MODEL_DEFAULT
    assert kwargs["max_tokens"] == config.CLAUDE_MAX_TOKENS
    # system split out of the messages array; only the user turn passed as messages.
    assert "financial reporting assistant" in kwargs["system"]
    assert [m["role"] for m in kwargs["messages"]] == ["user"]


def test_generate_report_derives_output_path_from_metadata(tmp_path: Path, monkeypatch) -> None:
    pkg = _make_package(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setattr(config, "OUTPUT_REPORTS", tmp_path / "reports")

    fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    monkeypatch.setattr(claude_client.anthropic, "Anthropic", MagicMock(return_value=fake_client))

    result = claude_client.generate_report(pkg)  # output_path=None → derived
    assert result == tmp_path / "reports" / "TikTok_Performance_Report_2026-04.md"
    assert result.exists()


def test_generate_report_reraises_api_error(tmp_path: Path, monkeypatch) -> None:
    import anthropic

    pkg = _make_package(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test-123")
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIError(
        "boom", request=MagicMock(), body=None)
    monkeypatch.setattr(claude_client.anthropic, "Anthropic", MagicMock(return_value=fake_client))

    with pytest.raises(anthropic.APIError):
        claude_client.generate_report(pkg, tmp_path / "out.md")
    assert not (tmp_path / "out.md").exists()  # nothing written on failure


def test_generate_report_stub_writes_placeholder(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path)
    out = tmp_path / "stub.md"
    result = claude_client.generate_report_stub(pkg, out)
    assert result == out
    assert "Stub report" in out.read_text(encoding="utf-8")
