"""File scanner — the "dumb" entry point of the ingest layer.

Single job: look in ``data/raw/`` and return the candidate ``.xlsm`` workbook
paths. Nothing else. It does NOT parse filenames, infer periods, or open
workbooks — those belong to ``period_parser.py`` and ``excel_loader.py``.
Keeping this boundary clean is deliberate: the scanner only answers "which real
workbook files are present?", deterministically.
"""

from __future__ import annotations

from pathlib import Path

from src import config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Real workbook extension we accept. Compared case-insensitively.
_WORKBOOK_SUFFIX = ".xlsm"

# Excel writes a "~$"-prefixed owner/lock file next to any workbook that is
# currently open on Windows. These are not data files — skip them.
_LOCK_PREFIX = "~$"


def _is_candidate(path: Path) -> bool:
    """True if ``path`` is a real workbook we should hand downstream."""
    if not path.is_file():
        return False
    name = path.name
    if name.startswith(_LOCK_PREFIX):  # Excel lock/temp file
        return False
    if name.startswith("."):  # hidden file
        return False
    return path.suffix.lower() == _WORKBOOK_SUFFIX


def scan_raw_files(directory: Path = config.DATA_RAW) -> list[Path]:
    """Return the candidate ``.xlsm`` workbook paths in ``directory``, sorted.

    - Filters to real ``.xlsm`` files, excluding Excel lock files (``~$``) and
      hidden files (leading dot).
    - Returns a deterministically sorted list (same folder state → same order).
    - An empty folder is "nothing to do": logs and returns ``[]``, not an error.

    Raises:
        FileNotFoundError: if ``directory`` does not exist or is not a directory
            (signals that setup / ``ensure_directories()`` was not run).
    """
    if not directory.exists():
        raise FileNotFoundError(
            f"Raw data directory does not exist: {directory}. "
            "Run ensure_directories() (setup) before scanning."
        )
    if not directory.is_dir():
        raise FileNotFoundError(
            f"Expected a directory but found a non-directory path: {directory}."
        )

    candidates = sorted(p for p in directory.iterdir() if _is_candidate(p))

    if not candidates:
        logger.info("No .xlsm workbooks found in %s — nothing to do.", directory)
    else:
        logger.info("Found %d candidate workbook(s) in %s.", len(candidates), directory)

    return candidates
