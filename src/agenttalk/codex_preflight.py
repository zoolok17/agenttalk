"""Codex supervisor launch admission and evidence from the restricted process.

Windows guard repair and durable holds for real-turn bus access denials.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess  # nosec B404
import time
import uuid

from ._atomic import write_text
from .store import validate_agent_name

ACL_TIMEOUT = 10.0
BUS_PERMISSION_DENIED = "codex_bus_permission_denied"


class PreflightError(ValueError):
    """An unattended Codex launch must remain held for operator repair."""


def reset_guard_acls(directory: Path, *, windows=None, run=None) -> None:
    """Re-inherit existing guards without replacing a live lock's inode.

    An unresettable guard is a hard failure: moving an open guard would split its
    lock identity. Never follow reparse points, and bound both traversal and tools.
    """
    if not (os.name == "nt" if windows is None else windows):
        return
    run = run or subprocess.run
    deadline = time.monotonic() + ACL_TIMEOUT
    count = 0
    try:
        if (directory.is_symlink() or getattr(directory.lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError("linked store directory")
        stack = [directory]
        guards = []
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    count += 1
                    if count > 100000 or time.monotonic() >= deadline:
                        raise OSError("guard traversal exceeded admission budget")
                    path = Path(entry.path)
                    if (entry.is_symlink() or getattr(entry.stat(follow_symlinks=False),
                                                     "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        raise OSError(f"linked store entry: {path}")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.name.endswith(".generation"):
                        guards.append(path)
        for guard in guards:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("ACL reset exceeded admission budget")
            # Use the OS installation, never a repository/PATH-supplied icacls.
            executable = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe")
            result = run([executable, str(guard), "/reset", "/L"],
                         capture_output=True, timeout=remaining, check=False)
            if result.returncode:
                raise OSError(f"icacls exit {result.returncode}: {guard}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError(f"codex_acl_preflight_failed: {exc}") from exc


def is_store_permission_denial(output: str, store_dir: Path) -> bool:
    """Require a denial and this store's absolute child path on the same line."""
    def normalized(text):
        return re.sub(r"\\+", "/", text).casefold() if os.name == "nt" else text

    prefix = normalized(str(store_dir.resolve())).rstrip("/") + "/"
    denial = re.compile(r"permission denied|access is denied|\bEACCES\b|\[Errno 13\]|\[WinError 5\]", re.I)
    return any(denial.search(line) and prefix in normalized(line) for line in output.splitlines())


def permission_hold_path(store, agent: str) -> Path:
    validate_agent_name(agent)
    digest = hashlib.sha256(agent.encode()).hexdigest()[:32]
    return store.state_dir / f"codex-bus-permission-{digest}.json"


def write_permission_hold(store, agent: str) -> None:
    # The host records a child sandbox's denial without reopening that guard.
    write_text(permission_hold_path(store, agent), json.dumps({"agent": agent, "failure": BUS_PERMISSION_DENIED}))


def has_permission_hold(store, agent: str) -> bool:
    try:
        permission_hold_path(store, agent).stat()
    except FileNotFoundError:
        return False
    # Presence is a hold, including malformed content. Other IO failures propagate.
    return True


def notify_failure(store, agent: str, failure: str) -> None:
    """Mandatory escalation, independent of optional supervisor notifications."""
    if failure not in {"CODEX_ACL_PREFLIGHT_FAILED", "CODEX_BUS_PERMISSION_DENIED"}:
        raise PreflightError("unknown Codex access failure")
    target = store.operator_facing() or store.sole_lead()
    if not target:
        raise PreflightError(f"{failure}: cannot escalate: no lead/operator-facing recipient")
    store.send(sender=agent, recipient=target, kind="question", subject=failure,
               body=f"Supervisor: {agent}: {failure}. Automatic relaunch is held. "
               "Stop the supervisor and wrapper, repair guard permissions, then clear "
               "this agent's permission marker and codex_access_hold before restarting.",
               meta={"request_id": "esc-" + uuid.uuid4().hex[:12],
                     "needs_operator": "true", "codex_launch_failure": failure})
