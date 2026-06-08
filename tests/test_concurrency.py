"""Concurrency tests: concurrent send() must never lose or clobber a message.

The bus is a multi-agent file store with no lock server. send() relies on
unique monotonic ids — within a process via the _id_lock + _last_id_dt, and
ACROSS processes via the microsecond timestamp plus a 4-char random suffix.
The suite previously pinned only within-process monotonicity (a single
thread emitting 2000 ids); these pin that genuinely concurrent writers don't
overwrite each other's <id>.json or drop a delivery (review M4b).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from agenttalk.store import Store


def test_concurrent_threads_send_no_loss(tmp_path: Path) -> None:
    """N threads each sending in a tight loop: every send lands on its own
    file and is delivered exactly once (exercises the within-process lock)."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    n_threads, per_thread = 8, 25
    errors: list[Exception] = []

    def worker(w: int) -> None:
        try:
            for i in range(per_thread):
                s.send(sender="alpha", recipient="beta", body=f"w{w}-m{i}")
        except Exception as e:  # noqa: BLE001 — re-raised via assertion below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    total = n_threads * per_thread
    files = list(s.messages_dir.glob("*.json"))
    assert len(files) == total  # no file silently clobbered
    bodies = sorted(m.body for m in s.messages_for("beta"))
    expected = sorted(f"w{w}-m{i}"
                      for w in range(n_threads) for i in range(per_thread))
    assert bodies == expected  # every send delivered exactly once


def test_concurrent_processes_send_no_loss(tmp_path: Path) -> None:
    """The real cross-process case: separate processes have independent
    _last_id_dt, so id uniqueness rests on the timestamp + random suffix.
    Spawn K concurrent `agenttalk send` processes against one store and
    assert none overwrote another's file or was lost."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    k = 10
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "agenttalk", "--root", str(tmp_path),
             "send", "--from", "alpha", "--to", "beta", "-m", f"p-{i}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for i in range(k)
    ]
    rcs = [p.wait(timeout=60) for p in procs]
    assert all(rc == 0 for rc in rcs), rcs
    files = list(s.messages_dir.glob("*.json"))
    assert len(files) == k  # no <id>.json collision lost a message
    bodies = sorted(m.body for m in s.messages_for("beta"))
    assert bodies == sorted(f"p-{i}" for i in range(k))
