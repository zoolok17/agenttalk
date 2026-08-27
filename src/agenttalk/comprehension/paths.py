"""``.agenttalk/comprehension/`` layout constants (design: "Local storage model")."""

from __future__ import annotations

from pathlib import Path

from ..store import DIRNAME as _AGENTTALK_DIRNAME
from .errors import InvalidComprehensionDir

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
    cold-read finding 1 on PR-A, rq-6cc5560b62f6).

    Raises :class:`InvalidComprehensionDir` unless the resolved path's last
    two segments are EXACTLY ``.agenttalk/comprehension`` (reviewer-1
    cold-read finding 1, round 2: blindly climbing "two parents up" let
    ``acquire_scan_lock(root / "unignored" / "store", ...)`` derive
    ``root`` right back out and pass the root-binding check while writing
    ``scan.lock`` OUTSIDE ``.agenttalk`` entirely — reproduced with
    ``under_agenttalk=False``). This shape check runs BEFORE the
    root-binding comparison, so a malformed path can never reach it.
    """
    resolved = Path(comprehension_dir).resolve()
    if resolved.name != COMPREHENSION_DIRNAME or resolved.parent.name != _AGENTTALK_DIRNAME:
        raise InvalidComprehensionDir(
            f"{comprehension_dir} is not a "
            f"<project root>/{_AGENTTALK_DIRNAME}/{COMPREHENSION_DIRNAME} path - refusing "
            "to derive a project root, and therefore a privacy disposition, from an "
            "arbitrary directory")
    return resolved.parent.parent


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
