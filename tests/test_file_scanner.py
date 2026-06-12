"""Tests for src/ingest/file_scanner.py.

Uses pytest's tmp_path fixture exclusively — no dependence on real data. The
scanner's contract: return only real .xlsm files, sorted, excluding Excel lock
files / hidden files / other extensions; tolerate an empty folder; raise on a
missing directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingest.file_scanner import scan_raw_files


def _touch(directory: Path, name: str) -> Path:
    """Create an empty file in directory and return its path."""
    path = directory / name
    path.write_bytes(b"")  # contents are irrelevant — the scanner never opens it
    return path


def test_returns_only_valid_xlsm_sorted(tmp_path: Path) -> None:
    # Two valid workbooks (created out of alphabetical order to prove sorting),
    # plus a lock file, a .txt, and a hidden file that must all be excluded.
    valid_b = _touch(tmp_path, "Tiktok_SKULevel_Profit_2026_03_vs_2026_04.xlsm")
    valid_a = _touch(tmp_path, "Tiktok_SKULevel_Profit_2025_04_vs_2026_04.xlsm")
    _touch(tmp_path, "~$Tiktok_SKULevel_Profit_2026_03_vs_2026_04.xlsm")  # Excel lock
    _touch(tmp_path, "notes.txt")  # wrong extension
    _touch(tmp_path, ".hidden.xlsm")  # hidden file

    result = scan_raw_files(tmp_path)

    # Only the two real workbooks come back.
    assert result == [valid_a, valid_b]
    # Explicitly sorted (valid_a sorts before valid_b: 2025 < 2026).
    assert result == sorted(result)
    # None of the excluded files leaked through.
    names = {p.name for p in result}
    assert not any(n.startswith("~$") for n in names)
    assert not any(n.startswith(".") for n in names)
    assert all(p.suffix == ".xlsm" for p in result)


def test_case_insensitive_extension(tmp_path: Path) -> None:
    # A .XLSM (uppercase) is still a real workbook.
    upper = _touch(tmp_path, "Report_2026_04.XLSM")
    assert scan_raw_files(tmp_path) == [upper]


def test_empty_directory_returns_empty_list(tmp_path: Path) -> None:
    # An existing-but-empty raw folder is "nothing to do", not an error.
    assert scan_raw_files(tmp_path) == []


def test_directory_with_no_matching_files_returns_empty(tmp_path: Path) -> None:
    _touch(tmp_path, "readme.md")
    _touch(tmp_path, "~$open.xlsm")
    assert scan_raw_files(tmp_path) == []


def test_missing_directory_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        scan_raw_files(missing)


def test_path_to_file_not_directory_raises(tmp_path: Path) -> None:
    # If the path exists but is a file, that's a misconfiguration → raise.
    a_file = _touch(tmp_path, "some.xlsm")
    with pytest.raises(FileNotFoundError):
        scan_raw_files(a_file)
