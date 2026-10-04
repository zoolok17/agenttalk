"""Team-folder canary probe: where does this process put temporary and cache files?

Standard library only and no network, because it runs inside every child the canary
starts, including the gateway-backed child and the dev gate's pytest, whose
environments carry almost nothing. It records three kinds of fact:

* configured: what the environment variables say, and destinations the tools resolve
  for themselves (pip's cache folder, where Python would put a compiled file);
* observed: a file this process really wrote, and where it landed;
* unknown: anything it could not ask or did not write (left as None).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess  # nosec B404 - runs only this interpreter's own pip, shell disabled
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

#: The variables that decide where temporary, cache and per-user files go.
NAMES = (
    "TEMP",
    "TMP",
    "TMPDIR",
    "PIP_CACHE_DIR",
    "PIP_NO_CACHE_DIR",
    "npm_config_cache",
    "PYTHONPYCACHEPREFIX",
    "PYTHONDONTWRITEBYTECODE",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "UV_CACHE_DIR",
    "PRE_COMMIT_HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "AGENTTALK_ROOT",
    "AGENTTALK_SCRATCH",
    "AGENTTALK_TURN_EVENTS_DIR",
)


def _pip_cache_dir() -> tuple[str | None, int]:
    """pip's resolved cache folder (no download, no cache write) and its count of warnings.

    The folder comes only from a successful run's standard output, as exactly one
    absolute path; anything on standard error is a diagnostic, never the answer.
    """
    try:
        done = subprocess.run(  # nosec B603 - fixed argv: this interpreter's pip
            [sys.executable, "-m", "pip", "cache", "dir"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None, 0
    diagnostics = len([line for line in done.stderr.splitlines() if line.strip()])
    if done.returncode != 0:
        return ("pip-cache-disabled" if "cache is disabled" in done.stderr else None), diagnostics
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not os.path.isabs(lines[0]):
        return None, diagnostics
    return lines[0], diagnostics


def observe(label: str, work_dir: str, write: bool) -> dict[str, Any]:
    """One process's answers. ``write`` also writes (and removes) one temporary file."""
    out: dict[str, Any] = {
        "label": label,
        "python": sys.version.split()[0],
        "env": {name: os.environ.get(name) for name in NAMES},
        "python_tempdir": tempfile.gettempdir(),
        "pycache_prefix": sys.pycache_prefix,
        "dont_write_bytecode": bool(sys.dont_write_bytecode),
        "temp_file_written": None,
    }
    if write:
        handle, path = tempfile.mkstemp(prefix="team-canary-")
        os.close(handle)
        out["temp_file_written"] = path
        os.remove(path)
    # Import a fresh module and see where (and whether) Python writes its compiled copy.
    source_dir = Path(work_dir) / ("module-" + uuid.uuid4().hex[:8])
    source_dir.mkdir(parents=True)
    name = "team_canary_" + uuid.uuid4().hex[:8]
    source = source_dir / (name + ".py")
    source.write_text("VALUE = 1\n", encoding="ascii")
    sys.path.insert(0, str(source_dir))
    try:
        importlib.import_module(name)
    finally:
        sys.path.remove(str(source_dir))
    expected = importlib.util.cache_from_source(str(source))
    out["pycache_resolved"] = expected
    out["pycache_written"] = expected if os.path.exists(expected) else None
    # Where a compiled file for installed code (the standard library here) would go.
    out["pycache_resolved_for_installed_code"] = importlib.util.cache_from_source(json.__file__)
    out["pip_cache_dir_resolved"], out["pip_stderr_lines"] = _pip_cache_dir()
    return out
