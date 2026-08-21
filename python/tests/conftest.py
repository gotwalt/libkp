"""Shared test fixtures: locate the repo-root spec data and load JSON."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# tests/ -> python/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO_ROOT / "spec"
VECTORS_DIR = SPEC_DIR / "vectors"
CAPTURES_DIR = SPEC_DIR / "captures"

# Allow `python -m pytest` from python/ without an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_json(path: Path) -> dict:
    """Read one JSON document."""
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def vector(name: str) -> dict:
    """Load ``spec/vectors/<name>.json``."""
    return load_json(VECTORS_DIR / f"{name}.json")


@pytest.fixture(scope="session")
def vectors_dir() -> Path:
    return VECTORS_DIR


@pytest.fixture(scope="session")
def captures_dir() -> Path:
    return CAPTURES_DIR
