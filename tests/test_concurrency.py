"""Concurrency tests: concurrent send() must never lose or clobber a message.

The bus is a multi-agent file store with no lock server. send() relies on
unique monotonic ids — within a process via the _id_lock + _last_id_dt, and
ACROSS processes via the microsecond timestamp plus a 4-char random suffix.
The suite previously pinned only within-process monotonicity (a single
thread emitting 2000 ids); these pin that genuinely concurrent writers don't
overwrite each other's <id>.json or drop a delivery (review M4b).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

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


# ----- M2: config.json read-modify-write must be serialized -------------

def test_config_lock_times_out_when_held_by_live_pid(tmp_path: Path) -> None:
    """A lock held by a LIVE pid is never stolen — acquisition waits and
    then raises a clear timeout (no silent override of a live writer)."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    lockf = s.dir / "config.lock"
    lockf.write_text(json.dumps({"pid": os.getpid(), "at": "x",
                                 "root": str(s.root)}), encoding="utf-8")
    try:
        with pytest.raises(TimeoutError, match="config lock"):
            with s._config_lock(timeout=0.3):
                pass
    finally:
        lockf.unlink()  # release the held lock we planted


def test_config_lock_breaks_stale_dead_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock whose recorded pid is provably dead is broken atomically and
    acquisition proceeds (a crashed writer must not wedge roster admin)."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    lockf = s.dir / "config.lock"
    lockf.write_text(json.dumps({"pid": 4242, "at": "x", "root": str(s.root)}),
                     encoding="utf-8")
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: False)
    with s._config_lock(timeout=2.0):
        pass
    assert not lockf.exists()  # broken + released


def test_concurrent_add_agent_no_lost_update(tmp_path: Path) -> None:
    """Two threads adding different agents concurrently must both survive —
    without the lock the later load->mutate->write clobbers the earlier add
    (review M2). The barrier maximizes the overlap window."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def add(n: str) -> None:
        try:
            barrier.wait()
            s.add_agent(n)
        except Exception as e:  # noqa: BLE001 — surfaced via assertion
            errors.append(e)

    t1 = threading.Thread(target=add, args=("c",))
    t2 = threading.Thread(target=add, args=("d",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, errors
    agents = s.load_config()["agents"]
    assert "c" in agents and "d" in agents  # neither add lost
    assert not (s.dir / "config.lock").exists()  # lock released
