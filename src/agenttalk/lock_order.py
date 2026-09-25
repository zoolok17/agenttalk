"""Store lock ranks. See docs/LOCK-ORDER.md for the complete acquisition inventory."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading


_local = threading.local()


def rank(root: Path, path: Path) -> int:
    # Classify the requested path without following links. The ownership layer
    # must still reject a replaced/reparse parent with its normal IO error.
    try:
        relative = path.absolute().relative_to(root.absolute()).as_posix().lower()
    except ValueError:
        return 210
    exact = {
        "assurance/coverage.lock": 20,
        "assurance/coverage-handoff.lock": 30,
        "supervisor-lifecycle.lock": 40,
        "powershell-host.lock": 50,
        "supervisor.instance.lock": 60,
        "locks/lane-reset.lock": 70,
        "state/operation-publication.lock": 100,
        ".acceptance-write.lock": 110,
        "config.lock": 130,
        "lane-deliveries/.worktree-integrity-secret.lock": 140,
        "retirement": 150,
        "message-publication": 160,
        "state/owed-action/ledger.lock": 170,
        "state/owed-action/proof-health.lock": 180,
    }
    if relative in exact:
        return exact[relative]
    if relative.startswith("locks/lane-") and relative.endswith(".transaction.lock"):
        return 80
    if relative.startswith("state/lane-") and relative.endswith(".cleanup.lock"):
        return 90
    if relative.startswith("closes/"):
        return 120
    if relative.endswith(".lead-loop-lease.lock"):
        return 190
    if relative.endswith(".waiting.lock"):
        return 200
    # Awaiting edges and other independent leaf locks must never call back
    # into a store transaction. New non-leaf families need an explicit rank.
    return 210


@contextmanager
def hold(root: Path, path: Path):
    key = os.getpid(), os.path.normcase(str(root.resolve()))
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    stack = held.setdefault(key, [])
    current = rank(root, path)
    if stack and current < max(item[0] for item in stack):
        label = path.name.replace("-", " ")
        raise TimeoutError(f"lock order inversion: {label} rank {current} after {stack[-1][1]}")
    entry = current, path.name
    stack.append(entry)
    try:
        yield
    finally:
        stack.remove(entry)
        if not stack:
            held.pop(key, None)
