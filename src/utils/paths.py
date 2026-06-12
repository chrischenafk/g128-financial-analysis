"""Runtime filesystem helpers.

Acts on the paths that ``config.py`` declares. ``config`` only *declares* paths;
this module is where they get *created* on disk at the start of a run.

Kept intentionally lean. Period-specific path builders (report/package file
naming) are NOT here yet — they arrive with the package and report layers that
actually need them.
"""

from __future__ import annotations

from src import config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# The runtime folders a run needs. (config.LOGS is also ensured by logger.py,
# but listing it here keeps a single explicit "these must exist" declaration.)
_RUNTIME_DIRECTORIES = (
    config.DATA_RAW,
    config.DATA_PROCESSED,
    config.OUTPUT_REPORTS,
    config.OUTPUT_PACKAGES,
    config.LOGS,
)


def ensure_directories() -> None:
    """Create every runtime directory if missing. Idempotent and safe to repeat."""
    for directory in _RUNTIME_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured directory exists: %s", directory)