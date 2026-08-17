from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agenttalk.store import Store


_AUTHORITY = "test-lifecycle-v1"
_ARTIFACT_BYTES = 4096
_METADATA_BYTES = _ARTIFACT_BYTES - 1
_HOLDER_SCRIPT = r"""
import sys
import time
from pathlib import Path

from agenttalk.lifecycle_lock import CrossProcessLifecycleLock

lock_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])
with CrossProcessLifecycleLock(
    lock_path,
    authority="test-lifecycle-v1",
    timeout_seconds=2.0,
).hold("test-holder"):
    ready_path.write_text("ready\n", encoding="ascii")
    while not release_path.exists():
        time.sleep(0.01)
"""


def _lock_api():
    from agenttalk.lifecycle_lock import (
        CrossProcessLifecycleLock,
        LifecycleLockContended,
        LifecycleLockUnknown,
    )

    return CrossProcessLifecycleLock, LifecycleLockContended, LifecycleLockUnknown


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source if not inherited else source + os.pathsep + inherited
    return env


def _wait_for_path(path: Path, process: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"holder exited {process.returncode} before readiness: "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(0.01)
    raise AssertionError(f"holder did not publish {path} within {timeout:g}s")


def _spawn_holder(lock_path: Path, ready: Path, release: Path) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - exact local interpreter and test script
        [sys.executable, "-c", _HOLDER_SCRIPT, str(lock_path), str(ready), str(release)],
        env=_subprocess_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _wait_clean(process: subprocess.Popen) -> tuple[str, str]:
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"holder did not exit after release: stdout={stdout!r}, stderr={stderr!r}"
        ) from None


def _kill_and_reap(process: subprocess.Popen) -> tuple[str, str]:
    if process.poll() is None:
        process.kill()
    return process.communicate(timeout=5)


def _wait_for_posix_zombie(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(  # noqa: S603,S607 - read-only exact-pid test oracle
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("Z"):
            return
        time.sleep(0.01)
    raise AssertionError(f"pid {pid} did not become an unreaped zombie")


def _read_record(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        raw = os.read(fd, _METADATA_BYTES)
    finally:
        os.close(fd)
    assert len(raw) == _METADATA_BYTES
    payload, separator, padding = raw.partition(b"\0")
    assert separator == b"\0"
    assert not padding.strip(b"\0")
    return json.loads(payload)


def _write_record(path: Path, value: object) -> bytes:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    assert len(payload) < _METADATA_BYTES
    raw = payload + b"\0" * (_ARTIFACT_BYTES - len(payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _exact_process_identity(pid: int) -> tuple[str, str]:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        assert handle
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            assert kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            ticks = (int(creation.dwHighDateTime) << 32) | int(
                creation.dwLowDateTime
            )
            assert ticks > 0
            return "win32-filetime-v1", str(ticks)
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform == "darwin":
        class ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        info = ProcBSDInfo()
        assert libproc.proc_pidinfo(
            pid,
            3,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ) == ctypes.sizeof(info)
        assert info.pbi_pid == pid
        assert info.pbi_start_tvsec > 0
        assert 0 <= info.pbi_start_tvusec <= 999_999
        return (
            "darwin-proc-bsdinfo-v1",
            f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
        )
    stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat_text[stat_text.rfind(")") + 2 :].split()
    start_ticks = fields[19]
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="utf-8"
    ).strip()
    assert start_ticks.isdigit()
    return "linux-proc-start-v1", f"{boot_id}:{start_ticks}"


def _different_exact_identity(identity: tuple[str, str]) -> dict[str, str]:
    scheme, value = identity
    prefix, numeric = value.rsplit(":", 1) if ":" in value else ("", value)
    changed = "2" if numeric == "1" else "1"
    return {"scheme": scheme, "value": f"{prefix}:{changed}" if prefix else changed}


def _identity_record(pid: int) -> dict[str, str]:
    scheme, value = _exact_process_identity(pid)
    return {"scheme": scheme, "value": value}


@pytest.mark.subprocess
def test_lifecycle_lock_reports_real_process_holder_on_contention(tmp_path) -> None:
    CrossProcessLifecycleLock, LifecycleLockContended, _ = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    ready = tmp_path / "holder.ready"
    release = tmp_path / "holder.release"
    holder = _spawn_holder(lock_path, ready, release)
    try:
        _wait_for_path(ready, holder)
        held_record = _read_record(lock_path)
        assert held_record["process_identity"] == _identity_record(holder.pid)

        with pytest.raises(LifecycleLockContended) as exc_info:
            with CrossProcessLifecycleLock(
                lock_path,
                authority=_AUTHORITY,
                timeout_seconds=0.2,
                poll_seconds=0.01,
            ).hold("contender"):
                raise AssertionError("a live holder must exclude the contender")

        refusal = exc_info.value
        assert refusal.reason_code == "lifecycle_lock_contended"
        assert refusal.holder_pid == holder.pid
        assert refusal.holder_operation == "test-holder"
        assert refusal.holder_identity == held_record["process_identity"]
        assert refusal.holder_since.endswith("Z")
        assert str(holder.pid) in str(refusal)
        assert "test-holder" in str(refusal)
        assert "retry" in str(refusal).casefold()
        assert _read_record(lock_path) == held_record
    finally:
        release.write_text("release\n", encoding="ascii")
        stdout, stderr = _wait_clean(holder)
    assert holder.returncode == 0, (stdout, stderr)
    assert _read_record(lock_path) == {
        "authority": _AUTHORITY,
        "schema_version": 1,
        "state": "released",
    }


@pytest.mark.subprocess
def test_lifecycle_lock_contention_names_holder_after_real_process_handoff(
    tmp_path,
    monkeypatch,
) -> None:
    from agenttalk import lifecycle_lock as lock_module

    CrossProcessLifecycleLock, LifecycleLockContended, _ = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    first_ready = tmp_path / "first.ready"
    first_release = tmp_path / "first.release"
    first = _spawn_holder(lock_path, first_ready, first_release)
    second: subprocess.Popen | None = None
    second_release = tmp_path / "second.release"
    try:
        _wait_for_path(first_ready, first)
        real_try_os_lock = lock_module._try_os_lock
        calls = 0

        def handoff_before_final_attempt(fd: int) -> bool:
            nonlocal calls, second
            calls += 1
            if calls == 2:
                first_release.write_text("release\n", encoding="ascii")
                stdout, stderr = _wait_clean(first)
                assert first.returncode == 0, (stdout, stderr)
                second_ready = tmp_path / "second.ready"
                second = _spawn_holder(lock_path, second_ready, second_release)
                _wait_for_path(second_ready, second)
            return real_try_os_lock(fd)

        monkeypatch.setattr(lock_module, "_try_os_lock", handoff_before_final_attempt)

        with pytest.raises(LifecycleLockContended) as exc_info:
            with CrossProcessLifecycleLock(
                lock_path,
                authority=_AUTHORITY,
                timeout_seconds=0,
            ).hold("handoff-contender"):
                raise AssertionError("the second real holder must retain the lock")

        assert second is not None
        assert calls == 2
        assert exc_info.value.holder_pid == second.pid
        assert exc_info.value.holder_operation == "test-holder"
    finally:
        first_release.write_text("release\n", encoding="ascii")
        if first.poll() is None:
            _kill_and_reap(first)
        if second is not None:
            second_release.write_text("release\n", encoding="ascii")
            stdout, stderr = _wait_clean(second)
            assert second.returncode == 0, (stdout, stderr)


@pytest.mark.subprocess
def test_lifecycle_lock_takes_over_after_exact_holder_dies(tmp_path) -> None:
    CrossProcessLifecycleLock, _, _ = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    ready = tmp_path / "holder.ready"
    release = tmp_path / "holder.release"
    holder = _spawn_holder(lock_path, ready, release)
    try:
        _wait_for_path(ready, holder)
        crashed_owner = _read_record(lock_path)
        assert crashed_owner["process_identity"] == _identity_record(holder.pid)
        holder.kill()
        holder.communicate(timeout=5)
        inode_before = os.stat(lock_path)

        with CrossProcessLifecycleLock(
            lock_path,
            authority=_AUTHORITY,
            timeout_seconds=1.0,
        ).hold("takeover"):
            replacement = _read_record(lock_path)
            assert replacement["pid"] == os.getpid()
            assert replacement["process_identity"] == _identity_record(os.getpid())
            assert replacement["generation"] != crashed_owner["generation"]

        inode_after = os.stat(lock_path)
        assert (inode_after.st_dev, inode_after.st_ino) == (
            inode_before.st_dev,
            inode_before.st_ino,
        )
        assert _read_record(lock_path)["state"] == "released"
    finally:
        _kill_and_reap(holder)


@pytest.mark.skipif(os.name != "posix", reason="POSIX zombie ownership semantics")
@pytest.mark.subprocess
def test_lifecycle_lock_takes_over_from_unreaped_zombie_holder(tmp_path) -> None:
    CrossProcessLifecycleLock, _, _ = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    ready = tmp_path / "holder.ready"
    release = tmp_path / "holder.release"
    holder = _spawn_holder(lock_path, ready, release)
    try:
        _wait_for_path(ready, holder)
        crashed_owner = _read_record(lock_path)
        holder.kill()
        _wait_for_posix_zombie(holder.pid)

        with CrossProcessLifecycleLock(
            lock_path,
            authority=_AUTHORITY,
            timeout_seconds=1.0,
        ).hold("zombie-takeover"):
            replacement = _read_record(lock_path)
            assert replacement["generation"] != crashed_owner["generation"]
            assert replacement["pid"] == os.getpid()
    finally:
        _kill_and_reap(holder)


@pytest.mark.subprocess
def test_lifecycle_lock_pid_reuse_imposter_allows_takeover_without_kill(
    tmp_path,
) -> None:
    CrossProcessLifecycleLock, _, _ = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    imposter = subprocess.Popen(  # noqa: S603 - exact local inert child
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        current_identity = _exact_process_identity(imposter.pid)
        _write_record(
            lock_path,
            {
                "authority": _AUTHORITY,
                "schema_version": 1,
                "state": "held",
                "generation": "0" * 32,
                "pid": imposter.pid,
                "process_identity": _different_exact_identity(current_identity),
                "operation": "dead-holder",
                "acquired_at": "2026-08-17T13:00:00.000000Z",
            },
        )

        with CrossProcessLifecycleLock(
            lock_path,
            authority=_AUTHORITY,
            timeout_seconds=1.0,
        ).hold("pid-reuse-takeover"):
            assert imposter.poll() is None
            assert _read_record(lock_path)["pid"] == os.getpid()

        assert imposter.poll() is None
    finally:
        _kill_and_reap(imposter)


def test_lifecycle_lock_refuses_free_kernel_lock_with_exact_live_owner(
    tmp_path,
) -> None:
    CrossProcessLifecycleLock, _, LifecycleLockUnknown = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    contradictory = {
        "authority": _AUTHORITY,
        "schema_version": 1,
        "state": "held",
        "generation": "0" * 32,
        "pid": os.getpid(),
        "process_identity": _identity_record(os.getpid()),
        "operation": "impossible-free-lock",
        "acquired_at": "2026-08-17T13:00:00.000000Z",
    }
    original = _write_record(lock_path, contradictory)

    with pytest.raises(
        LifecycleLockUnknown,
        match="kernel lock was free while its exact recorded holder was alive",
    ):
        with CrossProcessLifecycleLock(
            lock_path,
            authority=_AUTHORITY,
            timeout_seconds=0.1,
        ).hold("must-not-steal"):
            raise AssertionError("exact live ownership must never be stolen")

    assert lock_path.read_bytes() == original


def test_lifecycle_lock_body_exception_durably_releases_and_allows_reacquire(
    tmp_path,
) -> None:
    CrossProcessLifecycleLock, _, _ = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    lock = CrossProcessLifecycleLock(
        lock_path,
        authority=_AUTHORITY,
        timeout_seconds=0.1,
    )

    with pytest.raises(RuntimeError, match="injected body failure"):
        with lock.hold("failing-body"):
            raise RuntimeError("injected body failure")

    assert _read_record(lock_path) == {
        "authority": _AUTHORITY,
        "schema_version": 1,
        "state": "released",
    }
    with lock.hold("after-body-failure"):
        assert _read_record(lock_path)["operation"] == "after-body-failure"


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        b'{"schema_version":1,"state":"held","pid":"not-an-int"}\n',
        b'{"authority":"test-lifecycle-v1","schema_version":true,"state":"released"}\n',
        b'{"authority":"test-lifecycle-v1","schema_version":1.0,"state":"released"}\n',
        b'{"authority":"test-lifecycle-v1","schema_version":1,'
        b'"schema_version":1,"state":"released"}\n',
        (
            json.dumps(
                {
                    "authority": _AUTHORITY,
                    "schema_version": 1,
                    "state": "held",
                    "generation": "0" * 32,
                    "pid": 1,
                    "process_identity": {
                        "scheme": "win32-filetime-v1",
                        "value": "",
                    },
                    "operation": "corrupt",
                    "acquired_at": "2026-08-17T13:00:00.000000Z",
                }
            )
            + "\n"
        ).encode("utf-8"),
    ],
)
def test_lifecycle_lock_refuses_corrupt_artifact_as_unknown(
    tmp_path,
    corrupt_payload,
) -> None:
    CrossProcessLifecycleLock, _, LifecycleLockUnknown = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    assert len(corrupt_payload) < _METADATA_BYTES
    corrupt = corrupt_payload + b"\0" * (_ARTIFACT_BYTES - len(corrupt_payload))
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(corrupt)

    with pytest.raises(LifecycleLockUnknown) as exc_info:
        with CrossProcessLifecycleLock(
            lock_path,
            authority=_AUTHORITY,
            timeout_seconds=0.1,
        ).hold("corruption-control"):
            raise AssertionError("corrupt authority must never grant the lock")

    assert exc_info.value.reason_code == "lifecycle_lock_unknown"
    message = str(exc_info.value).casefold()
    assert "corrupt" in message
    assert "confirm no lifecycle operation is running" in message
    assert str(lock_path).casefold() in message
    assert "remove" in message
    assert lock_path.read_bytes() == corrupt
    if b'"process_identity"' in corrupt_payload:
        assert "lock holder process identity is corrupt" in exc_info.value.detail


def test_lifecycle_lock_refuses_hardlinked_artifact_without_touching_target(
    tmp_path,
) -> None:
    CrossProcessLifecycleLock, _, LifecycleLockUnknown = _lock_api()
    lock_path = tmp_path / "gateway" / "lifecycle.lock"
    victim = tmp_path / "victim.bin"
    original = b"v" * _ARTIFACT_BYTES
    victim.write_bytes(original)
    lock_path.parent.mkdir(parents=True)
    os.link(victim, lock_path)

    with pytest.raises(LifecycleLockUnknown, match="single-link regular file"):
        with CrossProcessLifecycleLock(
            lock_path,
            authority=_AUTHORITY,
            timeout_seconds=0.1,
        ).hold("hardlink-control"):
            raise AssertionError("a hardlinked authority must not be opened for mutation")

    assert victim.read_bytes() == original
    assert lock_path.read_bytes() == original
    assert os.stat(victim).st_nlink == 2


@pytest.mark.subprocess
def test_gateway_lifecycle_lock_never_contends_with_store_config_lock(tmp_path) -> None:
    CrossProcessLifecycleLock, _, _ = _lock_api()
    root = tmp_path / "project"
    Store(root).init(["lead"])
    lock_path = root / ".agenttalk" / "gateway" / "lifecycle.lock"
    ready = tmp_path / "holder.ready"
    release = tmp_path / "holder.release"
    holder = _spawn_holder(lock_path, ready, release)
    try:
        _wait_for_path(ready, holder)
        with Store(root).config_lock(timeout=0.5):
            pass
    finally:
        release.write_text("release\n", encoding="ascii")
        stdout, stderr = _wait_clean(holder)
    assert holder.returncode == 0, (stdout, stderr)

    reverse_ready = tmp_path / "reverse.ready"
    reverse_release = tmp_path / "reverse.release"
    with Store(root).config_lock(timeout=0.5):
        reverse = _spawn_holder(lock_path, reverse_ready, reverse_release)
        try:
            _wait_for_path(reverse_ready, reverse)
        finally:
            reverse_release.write_text("release\n", encoding="ascii")
            stdout, stderr = _wait_clean(reverse)
    assert reverse.returncode == 0, (stdout, stderr)
