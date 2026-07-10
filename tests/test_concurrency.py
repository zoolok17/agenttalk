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
import time
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


def test_config_lock_interoperates_with_legacy_o_excl_process(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    ready_path = tmp_path / "legacy.ready"
    release_path = tmp_path / "legacy.release"
    holder_code = """
import json, os, pathlib, sys, time
lock, ready, release = map(pathlib.Path, sys.argv[1:])
fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.write(fd, json.dumps({'pid': os.getpid(), 'protocol': 'legacy'}).encode())
os.close(fd)
ready.write_text('ready', encoding='utf-8')
while not release.exists():
    time.sleep(0.005)
lock.unlink()
"""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_code,
            str(lock_path),
            str(ready_path),
            str(release_path),
        ]
    )
    deadline = time.monotonic() + 5.0
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ready_path.exists(), "legacy holder did not acquire the lock"
    try:
        with pytest.raises(TimeoutError, match="config lock"):
            with store._config_lock(timeout=0.05, poll=0.005):
                pass
    finally:
        release_path.write_text("release", encoding="utf-8")
        assert holder.wait(timeout=5.0) == 0

    legacy_probe = """
import os, pathlib, sys
lock = pathlib.Path(sys.argv[1])
try:
    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    raise SystemExit(17)
os.close(fd)
lock.unlink()
"""
    with store._config_lock(timeout=0.2, poll=0.005):
        blocked = subprocess.run(
            [sys.executable, "-c", legacy_probe, str(lock_path)],
            check=False,
        )
        assert blocked.returncode == 17
    acquired = subprocess.run(
        [sys.executable, "-c", legacy_probe, str(lock_path)],
        check=False,
    )
    assert acquired.returncode == 0


def test_config_lock_does_not_reap_live_legacy_zero_byte_creator(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    ready_path = tmp_path / "legacy-zero.ready"
    release_path = tmp_path / "legacy-zero.release"
    holder_code = """
import json, os, pathlib, sys, time
lock, ready, release = map(pathlib.Path, sys.argv[1:])
fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
ready.write_text('ready', encoding='utf-8')
while not release.exists():
    time.sleep(0.005)
os.write(fd, json.dumps({'pid': os.getpid(), 'protocol': 'legacy'}).encode())
os.fsync(fd)
os.close(fd)
try:
    lock.unlink()
except FileNotFoundError:
    pass
"""
    holder = subprocess.Popen([
        sys.executable,
        "-c",
        holder_code,
        str(lock_path),
        str(ready_path),
        str(release_path),
    ])
    deadline = time.monotonic() + 5.0
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ready_path.exists(), "legacy creator did not publish its zero-byte marker"
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="config lock"):
            with store._config_lock(timeout=0.15, poll=0.005):
                pass
        assert time.monotonic() - started >= 0.1
        assert lock_path.exists()
        assert lock_path.stat().st_size == 0
    finally:
        release_path.write_text("release", encoding="utf-8")
        assert holder.wait(timeout=5.0) == 0


def test_current_config_lock_publishes_complete_marker_without_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    linked_sizes: list[int] = []
    real_link = store_mod.os.link

    def observed_link(source, destination, *, follow_symlinks=True) -> None:
        if Path(destination) == lock_path:
            assert not lock_path.exists()
            linked_sizes.append(Path(source).stat().st_size)
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(store_mod.os, "link", observed_link)
    with store._config_lock(timeout=0.2, poll=0.005):
        record = json.loads(lock_path.read_text(encoding="utf-8"))
        assert record["protocol"] == "o_excl_v2"
        assert record["pid"] == os.getpid()

    assert linked_sizes and all(size > 0 for size in linked_sizes)
    assert list(store.dir.glob(".config.lock.*.prepare")) == []


def test_config_lock_recovers_prepare_link_left_by_post_publish_crash(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    prepared = store.dir / f".config.lock.{'a' * 32}.prepare"
    crash_code = """
import json, os, pathlib, sys
lock, prepared = map(pathlib.Path, sys.argv[1:])
record = json.dumps({
    'pid': os.getpid(),
    'protocol': 'o_excl_v2',
    'generation': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'at': '2026-07-10T00:00:00Z',
    'root': str(lock.parent.parent),
})
fd = os.open(str(prepared), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.write(fd, record.encode('utf-8'))
os.fsync(fd)
os.close(fd)
os.link(prepared, lock)
os._exit(91)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", crash_code, str(lock_path), str(prepared)],
        check=False,
    )
    assert crashed.returncode == 91
    assert os.path.samefile(lock_path, prepared)
    assert os.lstat(lock_path).st_nlink == 2

    with store._config_lock(timeout=0.5, poll=0.005):
        pass

    assert not lock_path.exists()
    assert not prepared.exists()


def test_config_lock_safely_migrates_persistent_os_lock_marker(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    legacy_record = json.dumps({
        "pid": os.getpid(),
        "generation": "persistent-v1",
        "at": "2026-01-01T00:00:00Z",
    }).encode("utf-8")
    lock_path.write_bytes(legacy_record.ljust(store_mod._LOCK_METADATA_BYTES, b" "))

    with store._config_lock(timeout=0.2, poll=0.005):
        pass

    assert not lock_path.exists()


def test_config_lock_does_not_steal_truncated_legacy_live_owner(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    lock_path.write_bytes(
        b'{"pid": ' + str(os.getpid()).encode("ascii") + b', "root": "'
        + b"x" * (store_mod._LOCK_METADATA_BYTES + 100)
    )

    with pytest.raises(TimeoutError, match="config lock"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass


def test_config_lock_waits_for_active_persistent_marker_before_migration(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    ready_path = tmp_path / "persistent.ready"
    release_path = tmp_path / "persistent.release"
    holder_code = """
import json, os, pathlib, sys, time
lock, ready, release = map(pathlib.Path, sys.argv[1:])
fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
record = json.dumps({'pid': os.getpid(), 'generation': 'persistent-v1'}).encode()
os.write(fd, record.ljust(4096, b' '))
os.fsync(fd)
os.lseek(fd, 0, os.SEEK_SET)
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
ready.write_text('ready', encoding='utf-8')
while not release.exists():
    time.sleep(0.005)
if os.name == 'nt':
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    holder = subprocess.Popen([
        sys.executable,
        "-c",
        holder_code,
        str(lock_path),
        str(ready_path),
        str(release_path),
    ])
    deadline = time.monotonic() + 5.0
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ready_path.exists(), "persistent holder did not acquire the OS lock"
    try:
        with pytest.raises(TimeoutError, match="config lock"):
            with store._config_lock(timeout=0.05, poll=0.005):
                pass
    finally:
        release_path.write_text("release", encoding="utf-8")
        assert holder.wait(timeout=5.0) == 0

    with store._config_lock(timeout=0.2, poll=0.005):
        pass
    assert not lock_path.exists()


def test_config_lock_rejects_symlink_without_overwriting_target(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    target = tmp_path / "lock-target.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    lock_path = store.dir / "config.lock"
    try:
        lock_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(OSError, match="lock path|symlink|reparse"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass
    assert target.read_text(encoding="utf-8") == "do not overwrite"


def test_config_lock_rejects_hardlink_without_overwriting_target(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    target = tmp_path / "lock-target.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    lock_path = store.dir / "config.lock"
    os.link(target, lock_path)

    with pytest.raises(OSError, match="lock path|hardlink|link count"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass
    assert target.read_text(encoding="utf-8") == "do not overwrite"


def test_config_lock_rejects_hardlink_with_nonmatching_prepare_decoy(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    target = tmp_path / "lock-target.txt"
    marker = json.dumps({
        "pid": 2147483647,
        "protocol": "o_excl_v2",
        "generation": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    })
    target.write_text(marker, encoding="utf-8")
    lock_path = store.dir / "config.lock"
    os.link(target, lock_path)
    decoy_target = tmp_path / "prepare-decoy-target.txt"
    decoy_target.write_text(marker, encoding="utf-8")
    decoy = store.dir / f".config.lock.{'b' * 32}.prepare"
    os.link(decoy_target, decoy)
    assert os.lstat(lock_path).st_nlink == 2
    assert os.lstat(decoy).st_nlink == 2

    with pytest.raises(OSError, match="hardlink count"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass

    assert target.read_text(encoding="utf-8") == marker
    assert decoy.read_text(encoding="utf-8") == marker
    assert os.path.samefile(target, lock_path)
    assert os.path.samefile(decoy_target, decoy)
    assert not os.path.samefile(target, decoy)


def test_config_lock_rejects_matching_prepare_with_extra_hardlink(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    generation = "cccccccccccccccccccccccccccccccc"
    target = tmp_path / "lock-target.txt"
    marker = json.dumps({
        "pid": 2147483647,
        "protocol": "o_excl_v2",
        "generation": generation,
    })
    target.write_text(marker, encoding="utf-8")
    prepared = store.dir / f".config.lock.{generation}.prepare"
    lock_path = store.dir / "config.lock"
    os.link(target, prepared)
    os.link(target, lock_path)
    assert os.lstat(lock_path).st_nlink == 3

    with pytest.raises(OSError, match="hardlink count"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass

    assert target.read_text(encoding="utf-8") == marker
    assert os.path.samefile(target, prepared)
    assert os.path.samefile(target, lock_path)


def test_config_lock_rejects_hardlinked_generation_guard_without_overwrite(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    target = tmp_path / "guard-target.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    guard_path = store.dir / ".config.lock.generation"
    os.link(target, guard_path)

    with pytest.raises(OSError, match="hardlink count"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass
    assert target.read_text(encoding="utf-8") == "do not overwrite"


def test_config_lock_rejects_reparse_path_without_overwriting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    lock_path.write_text("do not overwrite", encoding="utf-8")
    target_identity = os.lstat(lock_path)
    real_reparse = store_mod._is_reparse_point

    def identify_target(info: os.stat_result) -> bool:
        return store_mod._same_file(info, target_identity) or real_reparse(info)

    monkeypatch.setattr(store_mod, "_is_reparse_point", identify_target)
    with pytest.raises(OSError, match="reparse point"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass
    assert lock_path.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.skipif(os.name == "nt", reason="FIFO lock-path probe is POSIX-only")
def test_config_lock_rejects_non_regular_path(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    os.mkfifo(lock_path)

    with pytest.raises(OSError, match="not a regular file"):
        with store._config_lock(timeout=0.05, poll=0.005):
            pass


@pytest.mark.parametrize("raw", [b"", b"{}"])
def test_config_lock_recovers_ownerless_or_zero_byte_marker(
    tmp_path: Path, raw: bytes,
) -> None:
    """A crashed creator cannot wedge the store before owner metadata lands."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    lockf = s.dir / "config.lock"
    lockf.write_bytes(raw)
    stale_mtime = time.time() - store_mod._LOCK_OWNERLESS_STALE_SECONDS - 1.0
    os.utime(lockf, (stale_mtime, stale_mtime))

    with s._config_lock(timeout=0.5, poll=0.005):
        pass

    assert not lockf.exists()


def test_config_lock_does_not_reap_fresh_ownerless_marker(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    lock_path = store.dir / "config.lock"
    lock_path.write_bytes(b"")

    with pytest.raises(TimeoutError, match="config lock"):
        with store._config_lock(timeout=0.1, poll=0.005):
            pass

    assert lock_path.exists()
    assert lock_path.stat().st_size == 0


def test_config_lock_recovery_never_path_replaces_stale_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale metadata is reusable without a read-then-replace ABA window."""
    s = Store(tmp_path)
    s.init(["a", "b"])
    lockf = s.dir / "config.lock"
    lockf.write_text(json.dumps({"pid": 4242, "at": "old"}), encoding="utf-8")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda _pid: store_mod.PROC_DEAD)

    real_replace = store_mod.os.replace

    def forbid_path_overwrite(source, destination) -> None:
        if Path(destination) == lockf:
            raise AssertionError("lock recovery must not overwrite the lock pathname")
        real_replace(source, destination)

    monkeypatch.setattr(store_mod.os, "replace", forbid_path_overwrite)
    with s._config_lock(timeout=0.2, poll=0.005):
        pass


def test_conditional_unlink_preserves_replacement_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "config.lock"
    displaced_path = tmp_path / "displaced.lock"
    replacement_path = tmp_path / "replacement.lock"
    lock_path.write_text("expected", encoding="utf-8")
    replacement_path.write_text("replacement", encoding="utf-8")
    expected = os.lstat(lock_path)
    real_lstat = store_mod.os.lstat
    real_replace = store_mod.os.replace
    raced = False

    def racing_lstat(path) -> os.stat_result:
        nonlocal raced
        info = real_lstat(path)
        if Path(path) == lock_path and not raced:
            raced = True
            real_replace(lock_path, displaced_path)
            real_replace(replacement_path, lock_path)
        return info

    monkeypatch.setattr(store_mod.os, "lstat", racing_lstat)
    assert not store_mod._unlink_if_same_file(lock_path, expected)
    assert lock_path.read_text(encoding="utf-8") == "replacement"
    assert displaced_path.read_text(encoding="utf-8") == "expected"


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
    assert not (s.dir / "config.lock").exists()


def test_retirement_commits_before_paused_sender_final_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Store(tmp_path)
    s.init(["a", "b"])
    prepared = threading.Event()
    resume = threading.Event()
    errors: list[BaseException] = []
    lock_users: list[str] = []
    real_write = store_mod._write_text_exclusive
    real_retirement_lock = s._retirement_lock

    def paused_write(path: Path, text: str) -> os.stat_result:
        identity = real_write(path, text)
        if path.name.endswith(".pending"):
            prepared.set()
            if not resume.wait(5.0):
                raise TimeoutError("test did not resume sender")
        return identity

    @contextlib.contextmanager
    def observed_retirement_lock(*args, **kwargs):
        with real_retirement_lock(*args, **kwargs):
            lock_users.append(threading.current_thread().name)
            yield

    monkeypatch.setattr(store_mod, "_write_text_exclusive", paused_write)
    monkeypatch.setattr(s, "_retirement_lock", observed_retirement_lock)

    def send() -> None:
        try:
            s.send(sender="a", recipient="b", body="must not land")
        except BaseException as e:  # noqa: BLE001 - asserted in the parent thread
            errors.append(e)

    sender = threading.Thread(target=send, name="sender")
    sender.start()
    try:
        assert prepared.wait(2.0)
        s.retire_agent("b")
        assert lock_users == ["MainThread"]
        assert "b" in s.retired_agents()
    finally:
        resume.set()
        sender.join(timeout=2.0)

    assert not sender.is_alive()
    assert lock_users == ["MainThread", "sender"]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "retired" in str(errors[0])
    assert list(s.messages_dir.glob("*.json")) == []
    assert list(s.messages_dir.glob("*.pending")) == []


def test_concurrent_sends_prepare_durable_payloads_outside_config_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only final roster revalidation + namespace publish may serialize.

    The barrier is the accepted budget contract: both durable write+fsync
    preparations must overlap. Moving preparation back under config.lock makes
    the first sender time out at the barrier while the second waits on the lock.
    """
    store = Store(tmp_path)
    store.init(["a", "b"])
    barrier = threading.Barrier(2)
    prepared: list[Path] = []
    errors: list[BaseException] = []
    real_write = store_mod._write_text_exclusive

    def observed_prepare(path: Path, text: str) -> os.stat_result:
        if path.name.endswith(".pending"):
            prepared.append(path)
            barrier.wait(timeout=2.0)
        return real_write(path, text)

    monkeypatch.setattr(store_mod, "_write_text_exclusive", observed_prepare)

    def send(body: str) -> None:
        try:
            store.send(sender="a", recipient="b", body=body)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=send, args=(body,)) for body in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(prepared) == 2
    assert sorted(message.body for message in store.messages_for("b")) == ["one", "two"]


def test_send_publish_failure_cleans_prepared_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["a", "b"])
    real_replace = store_mod.os.replace

    def fail_message_publish(source, destination) -> None:
        if (
            Path(source).name.endswith(".pending")
            and Path(destination).suffix == ".json"
        ):
            raise OSError("injected message publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(store_mod.os, "replace", fail_message_publish)
    with pytest.raises(OSError, match="publish failure"):
        store.send(sender="a", recipient="b", body="must not land")

    assert list(store.messages_dir.iterdir()) == []


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
    # Compatible O_EXCL ownership clears the marker on release.
    assert not (s.dir / "config.lock").exists()
    with s._config_lock(timeout=0.2):
        pass
