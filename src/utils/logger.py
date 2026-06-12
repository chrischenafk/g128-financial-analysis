"""Logging factory for the pipeline.

``get_logger(name)`` returns a configured logger that writes to BOTH the console
and a timestamped file in ``config.LOGS``. Use this everywhere instead of
``print`` so every run leaves an auditable trail.

This module is the single allowed exception to "only paths.py creates
directories": logging must work before (and independently of) any explicit
``ensure_directories()`` call, so it ensures ``config.LOGS`` exists itself.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src import config

# Consistent format: timestamp | LEVEL | module name | message
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# One log file per process run. Computed once at import so every logger created
# during this run writes to the same file. (Wall-clock here is for the log
# filename only — it is NOT in the metric path, so determinism is unaffected.)
_RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE = config.LOGS / f"run_{_RUN_TIMESTAMP}.log"


def _resolve_level() -> int:
    """Map config.LOG_LEVEL (e.g. 'INFO') to a logging constant, default INFO."""
    return getattr(logging, config.LOG_LEVEL.strip().upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that logs to console + a timestamped file.

    Idempotent: calling repeatedly with the same name does not stack handlers.
    """
    logger = logging.getLogger(name)

    # Already configured by a previous call — return as-is (no duplicate handlers).
    if logger.handlers:
        return logger

    logger.setLevel(_resolve_level())

    # The one allowed dir-creation exception (see module docstring).
    config.LOGS.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Don't bubble up to the root logger (avoids duplicate lines if the root
    # ever gets its own handlers).
    logger.propagate = False

    return logger
