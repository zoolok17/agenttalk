"""Codex supervisor launch admission and evidence from the restricted process.

Host ACL repair is necessary but is not proof. Only the sandbox command that
successfully publishes a bus message writes the launch-specific receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from ._atomic import write_text
from .store import validate_agent_name

ACL_TIMEOUT = 10.0
PROBE_SECONDS = 60.0
PROOF_TIMEOUT = 120.0
PROBE_ENV = "AGENTTALK_CODEX_BUS_PROBE"


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


def is_restricted_process() -> bool:
    """On Windows reject receipts written by the ordinary host wrapper token."""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.IsTokenRestricted.argtypes = [wintypes.HANDLE]
    advapi.IsTokenRestricted.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise PreflightError("codex_bus_proof_failed: cannot inspect restricted token")
    try:
        return bool(advapi.IsTokenRestricted(token))
    finally:
        kernel.CloseHandle(token)


def _nonce(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise PreflightError("codex_bus_proof_failed: invalid launch nonce")
    return value


def _receipt_path(store, agent: str) -> Path:
    validate_agent_name(agent)
    digest = hashlib.sha256(agent.encode()).hexdigest()[:32]
    return store.state_dir / f"codex-bus-proof-{digest}.json"


def write_bus_proof(store, agent: str, nonce: str) -> None:
    _nonce(nonce)
    path = _receipt_path(store, agent)
    if not is_restricted_process():
        raise PreflightError("codex_bus_proof_failed: command must run inside the restricted seat")
    target = store.operator_facing() or store.sole_lead()
    if not target:
        raise PreflightError("codex_bus_proof_failed: no lead or operator-facing recipient")
    message = store.send(sender=agent, recipient=target, kind="note",
                         subject="Codex restricted bus-write proof",
                         body=f"{agent}: restricted Codex process published its launch proof.",
                         meta={"codex_bus_proof": nonce})
    write_text(path, json.dumps({"agent": agent, "nonce": nonce,
                                "message_id": message.id}))


def notify_failure(store, agent: str, failure: str) -> None:
    """Mandatory lead escalation; independent of optional supervisor notes."""
    if failure not in {"CODEX_ACL_PREFLIGHT_FAILED", "CODEX_FIRST_BUS_WRITE_MISSING"}:
        raise PreflightError("unknown Codex launch failure")
    target = store.operator_facing() or store.sole_lead()
    if not target:
        raise PreflightError(f"{failure}: cannot escalate: no lead/operator-facing recipient")
    store.send(sender=agent, recipient=target, kind="question",
               subject=failure,
               body=f"Supervisor: {agent}: {failure}. Automatic relaunch is held. "
               "Repair the guard ACLs/runtime, stop the supervisor and clear this agent's "
               "codex_launch_check in supervisor state before retrying.",
               meta={"request_id": "esc-" + uuid.uuid4().hex[:12],
                     "needs_operator": "true", "codex_launch_failure": failure})


def read_bus_proof(store, agent: str) -> dict | None:
    try:
        with _receipt_path(store, agent).open("rb") as stream:
            payload = stream.read(4097)
        if len(payload) > 4096:
            return None
        receipt = json.loads(payload)
        if (isinstance(receipt, dict) and receipt.get("agent") == agent
                and isinstance(receipt.get("message_id"), str) and receipt["message_id"]):
            _nonce(receipt.get("nonce"))
            return receipt
    except (OSError, ValueError):
        pass
    return None


def complete_probe(store, agent: str, nonce: str) -> None:
    """Host completion acknowledges an existing restricted receipt, never creates one."""
    receipt = read_bus_proof(store, agent)
    if receipt is None or receipt.get("nonce") != nonce:
        raise PreflightError("codex_bus_proof_failed: no matching restricted receipt")
    receipt["ready_epoch"] = time.time()
    write_text(_receipt_path(store, agent), json.dumps(receipt))


def probe_prompt(store, agent: str, nonce: str, python: str) -> str:
    _nonce(nonce)
    validate_agent_name(agent)
    # This is a one-command startup check, not an inbound work turn.
    args = [python, "-m", "agenttalk", "--root", str(store.root), "supervise",
            "--codex-bus-proof", nonce, "--for", agent]
    if os.name == "nt":
        command = "& " + " ".join("'" + a.replace("'", "''") + "'" for a in args)
    else:
        import shlex
        command = shlex.join(args)
    return ("Supervisor startup check. Run exactly this one command using your normal "
            "sandboxed shell tool, with no escalation, then end the turn. Do not write "
            "a reply draft or run any other commands. A permission refusal is a result; "
            "do not retry it or change permissions.\n" + command)
