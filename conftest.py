"""Pytest bootstrap.

A conftest.py at the repository root causes pytest to add this directory to
sys.path, so ``from src import ...`` resolves when tests are collected from any
working directory. This keeps the absolute-import style robust without requiring
a packaging/editable install during development.
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)