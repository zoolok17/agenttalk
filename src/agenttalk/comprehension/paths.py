"""``.agenttalk/comprehension/`` layout constants (design: "Local storage model")."""

from __future__ import annotations

from pathlib import Path

COMPREHENSION_DIRNAME = "comprehension"
INDEX_FILENAME = "index.json"
LOCK_FILENAME = "scan.lock"
RUNS_DIRNAME = "runs"
STAGING_DIRNAME = ".staging"


def comprehension_dir(agenttalk_dir: Path) -> Path:
    """``agenttalk_dir`` is the project's ``.agenttalk/`` directory."""
    return agenttalk_dir / COMPREHENSION_DIRNAME


def index_path(comprehension_dir: Path) -> Path:
    return comprehension_dir / INDEX_FILENAME


def lock_path(comprehension_dir: Path) -> Path:
    return comprehension_dir / LOCK_FILENAME


def runs_dir(comprehension_dir: Path) -> Path:
    return comprehension_dir / RUNS_DIRNAME


def run_dir(comprehension_dir: Path, scan_id: str) -> Path:
    return runs_dir(comprehension_dir) / scan_id


def staging_dir(comprehension_dir: Path) -> Path:
    return comprehension_dir / STAGING_DIRNAME


def staging_run_dir(comprehension_dir: Path, scan_id: str, nonce: str) -> Path:
    return staging_dir(comprehension_dir) / f"{scan_id}-{nonce}"
