"""Bounded Windows guard ACL repair before supervised Codex relaunches."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess  # nosec B404
import time
import uuid

from .store import _validate_lock_file_stat

ACL_TIMEOUT = 10.0


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
                        if entry.name.endswith(".generation"):
                            raise OSError(f"linked store entry: {path}")
                        continue  # Codex-home junctions are not guard traversal roots.
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.name.endswith(".generation"):
                        guards.append(path)
        for guard in guards:
            # DirEntry.stat can omit link counts on Windows. Use a fresh stat
            # and the store's lock safety rules before privileged ACL changes.
            _validate_lock_file_stat(guard, guard.lstat())
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


def notify_failure(store, agent: str, failure: str) -> None:
    """Mandatory escalation, independent of optional supervisor notifications."""
    if failure != "CODEX_ACL_PREFLIGHT_FAILED":
        raise PreflightError("unknown Codex access failure")
    target = store.operator_facing() or store.sole_lead()
    if not target:
        raise PreflightError(f"{failure}: cannot escalate: no lead/operator-facing recipient")
    if target == agent:
        # Lead-chat displays questions from the lead to the human principal;
        # self-addressed questions are excluded from the operator view.
        try:
            target = store.operator_identity(lead=agent)
        except ValueError as exc:
            raise PreflightError(f"{failure}: cannot escalate: {exc}") from exc
    store.send(sender=agent, recipient=target, kind="question", subject=failure,
               body=f"Supervisor: {agent}: {failure}. Automatic relaunch is held. "
               "Stop the supervisor and wrapper, repair guard permissions, then clear "
               "this agent's codex_access_hold before restarting.",
               meta={"request_id": "esc-" + uuid.uuid4().hex[:12],
                     "needs_operator": "true", "codex_launch_failure": failure})
