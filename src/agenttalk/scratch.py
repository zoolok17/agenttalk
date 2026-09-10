"""Per-seat scratch root resolution (#148).

Every piece of temporary work a seat creates (pytest base-temps, review
worktrees, service data directories, reply-draft probes) should live under
one predictable root instead of sprawling into the repository root, the OS
temp dir, or `.worktrees/`. This module resolves that root; `janitor.py`
cleans it (and the legacy locations) up.

Config (optional, `.agenttalk/config.json`, key `"scratch"`):

    {"scratch": "D:/custom/scratch/root"}

or the long form (any key may be omitted; unset keys take the default):

    {"scratch": {"root": "D:/custom/scratch/root", "keep_days": 3}}

No config, or no `"scratch"` key, or a corrupt config file: silently use
the default (a sibling `atk-scratch` directory next to the project root).
This resolution never raises for a missing/malformed config - scratch
hygiene must work in every project, initialized or not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_KEEP_DAYS = 3

# A resolved agent/task path segment is used to build a filesystem path -
# reject anything that could escape the scratch root (path separators,
# `..`, empty) rather than silently sanitizing it into something else.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _read_scratch_config(project_root: Path) -> dict:
    """Best-effort read of the `"scratch"` config key. Never raises."""
    config_path = project_root / ".agenttalk" / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    scratch = raw.get("scratch")
    if isinstance(scratch, str) and scratch:
        return {"root": scratch}
    if isinstance(scratch, dict):
        return scratch
    return {}


def default_scratch_root(project_root: Path) -> Path:
    """The sibling `atk-scratch` directory next to the project root."""
    return project_root.parent / "atk-scratch"


def resolve_scratch_root(project_root: Path) -> Path:
    """Resolve the scratch root for a project: configured `"scratch"."root"`,
    else the default sibling directory. Does not create it."""
    project_root = Path(project_root).resolve()
    cfg = _read_scratch_config(project_root)
    root = cfg.get("root")
    if isinstance(root, str) and root.strip():
        return Path(root).resolve()
    return default_scratch_root(project_root)


def resolve_keep_days(project_root: Path) -> int:
    cfg = _read_scratch_config(project_root)
    keep_days = cfg.get("keep_days")
    if isinstance(keep_days, int) and keep_days >= 0:
        return keep_days
    return DEFAULT_KEEP_DAYS


def validate_segment(name: str, *, label: str) -> str:
    if not _SAFE_SEGMENT.match(name):
        raise ValueError(
            f"{label} {name!r} is not a safe scratch path segment "
            f"(letters/digits/-._ only, must not start with one of -._)"
        )
    return name


def agent_scratch_dir(project_root: Path, agent: str, *, create: bool = True) -> Path:
    """`<scratch_root>/<agent>/`."""
    validate_segment(agent, label="agent name")
    path = resolve_scratch_root(project_root) / agent
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def task_scratch_dir(
    project_root: Path, agent: str, task: str | None, *, create: bool = True
) -> Path:
    """`<scratch_root>/<agent>/<task>` (task defaults to `"default"`)."""
    task = task if task else "default"
    validate_segment(task, label="task id")
    path = agent_scratch_dir(project_root, agent, create=create) / task
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
