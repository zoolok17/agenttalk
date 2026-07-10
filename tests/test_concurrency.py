"""Concurrency tests: concurrent send() must never lose or clobber a message.

The bus is a multi-agent file store with no lock server. send() relies on
unique monotonic ids — within a process via the _id_lock + _last_id_dt, and
ACROSS processes via the microsecond timestamp plus a 4-char random suffix.
The suite previously pinned only within-process monotonicity (a single
thread emitting 2000 ids); these pin that genuinely concurrent writers don't
overwrite each other's <id>.json or drop a delivery (review M4b).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agenttalk import store as store_mod
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

def test_config_lock_times_out_while_another_holder_is_active(tmp_path: Path) -> None:
    """Ownership comes from the held OS lock, not advisory marker contents."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def hold() -> None:
        try:
            with s._config_lock(timeout=1.0, poll=0.01):
                entered.set()
                if not release.wait(5.0):
                    raise TimeoutError("test did not release lock holder")
        except Exception as e:  # noqa: BLE001 - asserted in the parent thread
            errors.append(e)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(2.0)
    try:
        with pytest.raises(TimeoutError, match="config lock"):
            with s._config_lock(timeout=0.05, poll=0.005):
                pass
    finally:
        release.set()
        holder.join(timeout=2.0)
    assert not holder.is_alive()
    assert errors == []


@pytest.mark.parametrize("raw", [b"", b"{}"])
def test_config_lock_recovers_ownerless_or_zero_byte_marker(
    tmp_path: Path, raw: bytes,
) -> None:
    """A crashed creator cannot wedge the store before owner metadata lands."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    lockf = s.dir / "config.lock"
    lockf.write_bytes(raw)

    with s._config_lock(timeout=0.2, poll=0.005):
        pass

    data = json.loads(lockf.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()


def test_config_lock_recovery_never_path_replaces_stale_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale metadata is reusable without a read-then-replace ABA window."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    lockf = s.dir / "config.lock"
    lockf.write_text(json.dumps({"pid": 4242, "at": "old"}), encoding="utf-8")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda _pid: store_mod.PROC_DEAD)

    def forbid_replace(*_args, **_kwargs) -> None:
        raise AssertionError("lock recovery must not replace a pathname generation")

    monkeypatch.setattr(store_mod.os, "replace", forbid_replace)
    with s._config_lock(timeout=0.2, poll=0.005):
        pass


def test_config_lock_release_failure_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Store(tmp_path)
    s.init(["a", "b"])

    def fail_release(_fd: int) -> None:
        raise OSError("injected unlock failure")

    monkeypatch.setattr(store_mod, "_release_file_lock", fail_release)
    with pytest.raises(OSError, match="release.*config lock"):
        with s._config_lock(timeout=0.2):
            pass


def test_send_revalidates_recipient_after_concurrent_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Store(tmp_path)
    s.init(["a", "b"])
    prepared = threading.Event()
    resume = threading.Event()
    errors: list[Exception] = []
    real_new_id = store_mod._new_id

    def paused_new_id() -> str:
        prepared.set()
        if not resume.wait(5.0):
            raise TimeoutError("test did not resume sender")
        return real_new_id()

    monkeypatch.setattr(store_mod, "_new_id", paused_new_id)

    def send() -> None:
        try:
            s.send(sender="a", recipient="b", body="must not land")
        except Exception as e:  # noqa: BLE001 - asserted in the parent thread
            errors.append(e)

    sender = threading.Thread(target=send)
    sender.start()
    assert prepared.wait(2.0)
    s.retire_agent("b")
    resume.set()
    sender.join(timeout=2.0)

    assert not sender.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "retired" in str(errors[0])
    assert list(s.messages_dir.glob("*.json")) == []


def test_wait_marker_clear_is_serialized_against_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Store(tmp_path)
    s.init(["a", "b"])
    s.write_waiting("a", {"agent": "a", "wait_token": "old", "pid": 1})
    clear_read = threading.Event()
    resume_clear = threading.Event()
    writer_attempted = threading.Event()
    clear_result: list[bool] = []
    errors: list[Exception] = []
    real_read = s.read_waiting
    real_waiting_lock = s._waiting_lock

    def paused_read(agent: str) -> dict | None:
        marker = real_read(agent)
        if threading.current_thread().name == "clear-old":
            clear_read.set()
            if not resume_clear.wait(5.0):
                raise TimeoutError("test did not resume marker clear")
        return marker

    @contextlib.contextmanager
    def observed_waiting_lock(agent: str):
        if threading.current_thread().name == "write-new":
            writer_attempted.set()
        with real_waiting_lock(agent):
            yield

    monkeypatch.setattr(s, "read_waiting", paused_read)
    monkeypatch.setattr(s, "_waiting_lock", observed_waiting_lock)

    def clear_old() -> None:
        try:
            clear_result.append(s.clear_waiting_if_token("a", "old"))
        except Exception as e:  # noqa: BLE001 - asserted in the parent thread
            errors.append(e)

    def write_new() -> None:
        try:
            s.write_waiting("a", {"agent": "a", "wait_token": "new", "pid": 2})
        except Exception as e:  # noqa: BLE001 - asserted in the parent thread
            errors.append(e)

    clearer = threading.Thread(target=clear_old, name="clear-old")
    writer = threading.Thread(target=write_new, name="write-new")
    clearer.start()
    assert clear_read.wait(2.0)
    writer.start()
    assert writer_attempted.wait(2.0)
    resume_clear.set()
    clearer.join(timeout=2.0)
    writer.join(timeout=2.0)

    assert not clearer.is_alive() and not writer.is_alive()
    assert errors == []
    assert clear_result == [True]
    assert s.read_waiting("a")["wait_token"] == "new"


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
    # The marker persists; OS ownership, not pathname existence, is the lock.
    assert (s.dir / "config.lock").is_file()
    with s._config_lock(timeout=0.2):
        pass
