"""``.agenttalk/comprehension/`` layout constants (design: "Local storage model")."""

from __future__ import annotations

from pathlib import Path

from ..store import DIRNAME as _AGENTTALK_DIRNAME

COMPREHENSION_DIRNAME = "comprehension"
INDEX_FILENAME = "index.json"
LOCK_FILENAME = "scan.lock"
RUNS_DIRNAME = "runs"
STAGING_DIRNAME = ".staging"

#: The comprehension dir's path RELATIVE TO THE PROJECT ROOT, POSIX-spelled
#: (design: "every plane output is written under `.agenttalk/`") — for
#: callers that need to name it to an external tool (e.g. privacy.py's git
#: subprocess calls), not for filesystem access (use comprehension_dir()
#: below for that, given an already-resolved ``.agenttalk/`` Path).
RELATIVE_COMPREHENSION_DIR = f"{_AGENTTALK_DIRNAME}/{COMPREHENSION_DIRNAME}"


def comprehension_dir(agenttalk_dir: Path) -> Path:
    """``agenttalk_dir`` is the project's ``.agenttalk/`` directory."""
    return agenttalk_dir / COMPREHENSION_DIRNAME


def project_root_from_comprehension_dir(comprehension_dir: Path) -> Path:
    """The inverse of ``comprehension_dir(agenttalk_dir)`` composed with
    the ``root / ".agenttalk"`` convention: climbs two levels
    (``comprehension`` then ``.agenttalk``) to recover the project root a
    ``comprehension_dir`` implies. Used to verify a privacy proof's bound
    root matches the root an operation is about to act on (reviewer-1
    cold-read finding 1 on PR-A, rq-6cc5560b62f6) — if ``comprehension_dir``
    was never actually shaped this way, the derived "root" simply won't
    match any real proof, which is the correct (safe) failure mode.
    """
    return comprehension_dir.parent.parent


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
