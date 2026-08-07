from __future__ import annotations

import contextlib
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import powershell_host as psh
from agenttalk import store as store_module
from agenttalk import supervisor as sup
from agenttalk import supervisor_lifecycle as lifecycle
from agenttalk.store import Store, _process_start_token


PWSH = (
    r"C:\Program Files\PowerShell\7\pwsh.exe"
    if sys.platform == "win32"
    else "/opt/microsoft/powershell/7/pwsh.exe"
)
IDENTITY_SCHEME = "win32-file-id-v1" if sys.platform == "win32" else "stat-v1"
WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="PowerShell host is Windows-only",
)
EXACT_START_TICKS = 134_285_904_001_234_560


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    return store


def _identity(path: str = PWSH, file_id: str = "01") -> psh.NativeFileIdentity:
    return psh.NativeFileIdentity(
        IDENTITY_SCHEME, path, "aabbccdd", file_id, 123, 456,
    )


def _probe(path: str = PWSH, file_id: str = "01") -> psh.ProbeResult:
    return psh.ProbeResult(
        path,
        "explicit",
        "Core",
        psh.PowerShellVersion(7, 6, 3),
        _identity(path, file_id),
    )


def _selected(
    store: Store,
    *,
    path: str = PWSH,
    file_id: str = "01",
    revision: int = 1,
) -> dict:
    identity = _identity(path, file_id)
    return {
        "path": path,
        "source": "explicit",
        "edition": "Core",
        "selection_revision": revision,
        "selection_fingerprint": file_id * 64,
        "_identity": identity,
        "_version": psh.PowerShellVersion(7, 6, 3),
        "project_id": store.project_id(),
    }


def _observation(
    pid: int,
    *,
    parent: int,
    path: str,
    ticks: int,
    identity: psh.NativeFileIdentity | None = None,
    start_locator: str = "2026-07-15T12:00:00.123456Z",
) -> lifecycle.ProcessObservation:
    return lifecycle.ProcessObservation(
        pid=pid,
        parent_pid=parent,
        path=path,
        creation_token=start_locator,
        creation_ticks=ticks,
        identity=identity or _identity(path),
        handle=0,
    )


def _write_instance_marker(
    store: Store,
    *,
    pid: int,
    pid_start: str | None,
    pid_start_filetime: str | None = None,
) -> Path:
    payload = {
        "root": str(store.root),
        "pid": pid,
        "pid_start": pid_start,
        "token": "a" * 32,
        "started_at": "2026-07-15T12:00:00Z",
    }
    if pid_start_filetime is not None:
        payload["pid_start_filetime"] = pid_start_filetime
    marker = store.supervisor_instance_path()
    marker.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return marker


def _spawned_identity(
    pid: int = 101,
    *,
    pid_start: str = "2026-07-15T12:00:00.1234567Z",
    ticks: int = 134_285_904_001_234_567,
) -> lifecycle.SpawnedProcessIdentity:
    return lifecycle.SpawnedProcessIdentity(
        pid=pid,
        pid_start=pid_start,
        start_filetime=ticks,
    )


def test_dotnet_seven_digit_creation_locator_matches_kernel_token() -> None:
    assert lifecycle.start_tokens_match(
        "2026-07-15T13:05:52.062092Z",
        "2026-07-15T15:05:52.0620917+02:00",
    )


def test_operator_commands_literal_quote_the_resolved_root(tmp_path: Path) -> None:
    store = Store(tmp_path / "operator's $root")
    escaped_root = str(store.root).replace("'", "''")
    escaped_kill = str(store.dir / "supervisor.kill").replace("'", "''")

    assert lifecycle.stop_instance_command(store).startswith(
        f"agenttalk --root '{escaped_root}' supervise --stop-instance"
    )
    assert lifecycle.instance_marker_repair_command(store).startswith(
        f"agenttalk --root '{escaped_root}' supervise --repair-instance-marker"
    )
    assert lifecycle.create_kill_switch_command(store).endswith(
        f"-LiteralPath '{escaped_kill}'"
    )


def test_start_marker_dead_holder_requires_explicit_repair_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
    )
    before = marker.read_bytes()
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: True)
    monkeypatch.setattr(
        lifecycle,
        "_process_parent_map",
        lambda: pytest.fail("dead holder must fail before parent inspection"),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="dead or reused.*repair-instance-marker",
    ) as exc_info:
        lifecycle.assert_supervisor_start_precondition(store)

    assert f"--root '{tmp_path.resolve()}' supervise" in str(exc_info.value)
    assert marker.read_bytes() == before


def test_start_marker_parentless_live_holder_requires_stop_then_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    holder = _observation(101, parent=202, path=PWSH, ticks=200)
    marker = _write_instance_marker(
        store,
        pid=holder.pid,
        pid_start=holder.creation_token,
    )
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: False)
    # The holder is present, but its recorded current parent is not: this is
    # the explicit orphan shape produced when `agenttalk start` exits first.
    monkeypatch.setattr(lifecycle, "_process_parent_map", lambda: {holder.pid: 202})
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: holder,
    )
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda _obs: None)

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="parentless.*stop-instance.*repair-instance-marker",
    ) as exc_info:
        lifecycle.assert_supervisor_start_precondition(store)

    assert "Stop-Process" not in str(exc_info.value)
    assert f"--root '{tmp_path.resolve()}' supervise --stop-instance" in str(
        exc_info.value
    )
    assert str(store.dir / "supervisor.kill") in str(exc_info.value)
    assert marker.exists()


def test_start_marker_unknown_observation_remains_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
    )
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: False)
    monkeypatch.setattr(
        lifecycle,
        "_process_parent_map",
        lambda: (_ for _ in ()).throw(
            lifecycle.SupervisorLifecycleError("process snapshot denied")
        ),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="live or unqueryable.*HELD",
    ):
        lifecycle.assert_supervisor_start_precondition(store)

    assert marker.exists()


def test_start_marker_without_exact_start_identity_remains_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(store, pid=101, pid_start=None)
    holder = _observation(101, parent=202, path=PWSH, ticks=200)
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: False)
    monkeypatch.setattr(lifecycle, "_process_parent_map", lambda: {101: 202, 202: 1})
    monkeypatch.setattr(
        lifecycle, "_open_process_observation", lambda pid, parents=None: holder
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError) as exc_info:
        lifecycle.assert_supervisor_start_precondition(store)

    assert "live or unqueryable" in str(exc_info.value)
    assert "HELD" in str(exc_info.value)
    assert "Stop-Process" not in str(exc_info.value)
    assert "stop-instance" not in str(exc_info.value)
    assert marker.exists()


def test_start_marker_with_live_older_parent_is_not_called_parentless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    holder = _observation(101, parent=202, path=PWSH, ticks=200)
    parent = _observation(202, parent=1, path=r"C:\Python\python.exe", ticks=100)
    _write_instance_marker(store, pid=holder.pid, pid_start=holder.creation_token)
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: False)
    monkeypatch.setattr(lifecycle, "_process_parent_map", lambda: {101: 202, 202: 1})
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: holder if pid == holder.pid else parent,
    )
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda _obs: None)

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="another live supervisor instance",
    ) as exc_info:
        lifecycle.assert_supervisor_start_precondition(store)

    assert "parentless" not in str(exc_info.value)


def test_repair_marker_quarantines_confirmed_dead_valid_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
    )
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: True)

    quarantined = lifecycle.repair_instance_marker(store)

    assert quarantined is not None and quarantined.exists()
    assert not marker.exists()


def test_repair_marker_refuses_live_or_unqueryable_valid_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
    )
    before = marker.read_bytes()
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: False)

    with pytest.raises(ValueError, match="live or unqueryable"):
        lifecycle.repair_instance_marker(store)

    assert marker.read_bytes() == before


@WINDOWS_ONLY
def test_repair_marker_refuses_inconsistent_exact_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00.0000000Z",
        pid_start_filetime="134285904000000001",
    )
    before = marker.read_bytes()
    monkeypatch.setattr(
        store_module,
        "_windows_owner_identity_gone_exact",
        lambda *_args: pytest.fail(
            "an inconsistent marker must not gain teardown authority"
        ),
    )

    with pytest.raises(ValueError, match="live or unqueryable"):
        lifecycle.repair_instance_marker(store)

    assert marker.read_bytes() == before


def test_claim_refuses_inconsistent_exact_identity_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="must identify the same process start"):
        store.claim_supervisor_instance(
            pid=101,
            pid_start="2026-07-15T12:00:00.0000000Z",
            pid_start_filetime="134285904000000001",
        )

    assert not store.supervisor_instance_path().exists()


def test_wait_for_supervisor_claim_rejects_alive_unclaimed_process(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    class Process:
        pid = 101

        @staticmethod
        def poll() -> None:
            return None

    ticks = iter((0.0, 2.0))
    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="remained alive but did not claim",
    ):
        lifecycle.wait_for_supervisor_claim(
            store,
            Process(),
            identity=_spawned_identity(),
            timeout_seconds=1.0,
            clock=lambda: next(ticks),
            pause=lambda _seconds: pytest.fail("deadline should already be reached"),
        )


def test_wait_for_supervisor_claim_rejects_a_different_marker_owner(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.claim_supervisor_instance(pid=202, pid_start=None) is not None

    class Process:
        pid = 101

        @staticmethod
        def poll() -> None:
            return None

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="another supervisor owns.*pid=202",
    ):
        lifecycle.wait_for_supervisor_claim(
            store, Process(), identity=_spawned_identity()
        )


def test_wait_for_supervisor_claim_rejects_exited_same_pid(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.claim_supervisor_instance(pid=101, pid_start=None) is not None

    class Process:
        pid = 101

        @staticmethod
        def poll() -> int:
            return 3

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="pid=101 exited with code 3",
    ):
        lifecycle.wait_for_supervisor_claim(
            store, Process(), identity=_spawned_identity()
        )


def test_wait_for_supervisor_claim_rejects_stale_same_pid_marker(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T11:59:59.1234567Z",
    )

    class Process:
        pid = 101

        @staticmethod
        def poll() -> None:
            return None

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="spawned pid=.*different start identity",
    ):
        lifecycle.wait_for_supervisor_claim(
            store, Process(), identity=_spawned_identity()
        )


def test_wait_for_supervisor_claim_rejects_contradictory_exact_marker(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00.0000001Z",
        pid_start_filetime="134285904000000000",
    )

    class Process:
        pid = 101

        @staticmethod
        def poll() -> None:
            return None

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="different start identity",
    ):
        lifecycle.wait_for_supervisor_claim(
            store,
            Process(),
            identity=_spawned_identity(
                pid_start="2026-07-15T12:00:00Z",
                ticks=134_285_904_000_000_000,
            ),
        )


def test_wait_for_supervisor_claim_rechecks_liveness_after_matching_marker(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.claim_supervisor_instance(
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
        pid_start_filetime="134285904000000000",
    )

    class Process:
        pid = 101
        polls = iter((None, 3))

        @classmethod
        def poll(cls):
            return next(cls.polls)

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="exited with code 3 after claiming",
    ):
        lifecycle.wait_for_supervisor_claim(
            store,
            Process(),
            identity=_spawned_identity(
                pid_start="2026-07-15T12:00:00Z",
                ticks=134_285_904_000_000_000,
            ),
        )


def test_failed_start_cleanup_preserves_a_late_verified_claim(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    marker = store.claim_supervisor_instance(
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
        pid_start_filetime="134285904000000000",
    )
    assert marker is not None

    class Process:
        pid = 101
        terminated = False

        @staticmethod
        def poll() -> None:
            return None

        @classmethod
        def terminate(cls) -> None:
            cls.terminated = True

    assert lifecycle.stop_unverified_supervisor(
        store,
        Process(),
        identity=_spawned_identity(
            pid_start="2026-07-15T12:00:00Z",
            ticks=134_285_904_000_000_000,
        ),
    ) == marker
    assert Process.terminated is False


def test_failed_start_cleanup_rechecks_liveness_after_late_claim(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.claim_supervisor_instance(
        pid=101,
        pid_start="2026-07-15T12:00:00Z",
        pid_start_filetime="134285904000000000",
    )

    class Process:
        pid = 101
        polls = iter((None, 3))

        @classmethod
        def poll(cls):
            return next(cls.polls)

    assert lifecycle.stop_unverified_supervisor(
        store,
        Process(),
        identity=_spawned_identity(
            pid_start="2026-07-15T12:00:00Z",
            ticks=134_285_904_000_000_000,
        ),
    ) is None


def test_failed_start_cleanup_rejects_stale_same_pid_claim(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T11:59:59.1234567Z",
    )

    class Process:
        pid = 101
        terminated = False

        @staticmethod
        def poll() -> None:
            return None

        @classmethod
        def terminate(cls) -> None:
            cls.terminated = True

        @staticmethod
        def wait(timeout=None) -> int:
            assert timeout == 5
            return 1

    assert lifecycle.stop_unverified_supervisor(
        store, Process(), identity=_spawned_identity()
    ) is None
    assert Process.terminated is True


@WINDOWS_ONLY
def test_process_parent_map_rejects_partial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Kernel32:
        @staticmethod
        def CreateToolhelp32Snapshot(_flags, _pid):
            return 123

        @staticmethod
        def Process32FirstW(_snapshot, entry_ptr):
            entry_ptr._obj.th32ProcessID = 101
            entry_ptr._obj.th32ParentProcessID = 202
            return True

        @staticmethod
        def Process32NextW(_snapshot, _entry_ptr):
            ctypes.set_last_error(5)
            return False

        @staticmethod
        def CloseHandle(_snapshot):
            return True

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: Kernel32())

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="snapshot ended before completion.*winerror 5",
    ):
        lifecycle._process_parent_map()


@WINDOWS_ONLY
def test_stop_instance_requires_kill_switch_without_mutating_marker(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    marker = _write_instance_marker(
        store,
        pid=101,
        pid_start="2026-07-15T12:00:00.0000000Z",
        pid_start_filetime="134285904000000000",
    )
    before = marker.read_bytes()

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="supervisor.kill is absent",
    ):
        lifecycle.stop_supervisor_instance(store)

    assert marker.read_bytes() == before


def test_legacy_roundtrip_start_token_retains_exact_filetime() -> None:
    assert lifecycle._start_token_filetime(
        "2026-07-15T14:00:00.1234567+02:00"
    ) == 134_285_904_001_234_567


@WINDOWS_ONLY
def test_stop_instance_verifies_creation_time_on_the_terminated_handle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        identity = lifecycle.observe_spawned_supervisor(process)
        _write_instance_marker(
            store,
            pid=identity.pid,
            pid_start=lifecycle._ticks_to_token(identity.start_filetime + 1),
            pid_start_filetime=str(identity.start_filetime + 1),
        )
        (store.dir / "supervisor.kill").write_text("", encoding="utf-8")

        with pytest.raises(
            lifecycle.SupervisorLifecycleError,
            match="start identity",
        ):
            lifecycle.stop_supervisor_instance(store)
        assert process.poll() is None

        _write_instance_marker(
            store,
            pid=identity.pid,
            pid_start=identity.pid_start,
            pid_start_filetime=str(identity.start_filetime),
        )

        stopped = lifecycle.stop_supervisor_instance(store)

        assert stopped["pid"] == identity.pid
        assert process.wait(timeout=5) != 0
        assert store.supervisor_instance_path().exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@WINDOWS_ONLY
def test_validate_ancestry_accepts_direct_and_cmd_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current_pid = os.getpid()
    direct = _observation(current_pid, parent=100, path=r"C:\Python\python.exe", ticks=300)
    monkeypatch.setattr(lifecycle, "_process_parent_map", lambda: {current_pid: 100})
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid, parents=None: direct)
    assert lifecycle._validate_ancestry(host) == (direct,)

    cmd = _observation(200, parent=100, path=r"C:\Windows\System32\cmd.exe", ticks=200)
    current = _observation(current_pid, parent=200, path=r"C:\Python\python.exe", ticks=300)
    monkeypatch.setattr(
        lifecycle, "_process_parent_map", lambda: {current_pid: 200, 200: 100},
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: current if pid == current_pid else cmd,
    )
    assert lifecycle._validate_ancestry(host) == (current, cmd)


def test_validate_ancestry_refuses_unrelated_manual_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current_pid = os.getpid()
    current = _observation(current_pid, parent=200, path=r"C:\Python\python.exe", ticks=300)
    unrelated = _observation(200, parent=999, path=r"C:\Tools\runner.exe", ticks=200)
    monkeypatch.setattr(
        lifecycle, "_process_parent_map", lambda: {current_pid: 200, 200: 999},
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: current if pid == current_pid else unrelated,
    )
    with pytest.raises(lifecycle.SupervisorLifecycleError, match="unsupported"):
        lifecycle._validate_ancestry(host)


def test_claim_rechecks_selection_before_marker_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _selected(store)
    second = _selected(store, path=r"D:\PowerShell\pwsh.exe", file_id="02", revision=2)
    reads = iter((first, second))
    host = _observation(100, parent=1, path=PWSH, ticks=EXACT_START_TICKS)
    current = _observation(os.getpid(), parent=100, path=r"C:\Python\python.exe", ticks=200)
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: next(reads))
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_validate_ancestry", lambda observed: (current,))
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)
    monkeypatch.setattr(psh, "native_file_identity", lambda path: host.identity)

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="selection changed"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        )
    assert not store.supervisor_instance_path().exists()


def test_claim_revalidates_image_identity_after_config_lock_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store, file_id="01")
    host = _observation(100, parent=1, path=PWSH, ticks=EXACT_START_TICKS)
    current = _observation(
        os.getpid(), parent=100, path=r"C:\Python\python.exe", ticks=200,
    )
    disk_identity = {"value": _identity(PWSH, "01")}

    @contextlib.contextmanager
    def replace_on_entry():
        disk_identity["value"] = _identity(PWSH, "02")
        yield

    monkeypatch.setattr(store, "_config_lock", replace_on_entry)
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_validate_ancestry", lambda observed: (current,))
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)
    monkeypatch.setattr(
        psh, "native_file_identity", lambda path: disk_identity["value"],
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="image identity changed"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        )
    assert not store.supervisor_instance_path().exists()


def test_claim_refuses_reused_pid_locator_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(
        lifecycle,
        "_validate_ancestry",
        lambda observed: pytest.fail("a reused locator must fail before ancestry"),
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="reused or ambiguous"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start="different-process-start",
            validate_artifacts=lambda: None,
        )
    assert not store.supervisor_instance_path().exists()


def test_claim_refuses_observed_image_identity_replacement_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store, file_id="01")
    host = _observation(
        100,
        parent=1,
        path=PWSH,
        ticks=EXACT_START_TICKS,
        identity=_identity(PWSH, "02"),
    )
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(
        lifecycle,
        "_validate_ancestry",
        lambda observed: pytest.fail("an image mismatch must fail before ancestry"),
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="does not match"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        )
    assert not store.supervisor_instance_path().exists()


def test_claim_refuses_process_exit_during_final_activity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(100, parent=1, path=PWSH, ticks=EXACT_START_TICKS)
    current = _observation(
        os.getpid(), parent=100, path=r"C:\Python\python.exe", ticks=200,
    )
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_validate_ancestry", lambda observed: (current,))

    def require_active(observed: lifecycle.ProcessObservation) -> None:
        if observed is host:
            raise lifecycle.SupervisorLifecycleError("process exited during validation")

    monkeypatch.setattr(lifecycle, "_require_process_active", require_active)

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="exited during validation"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        )
    assert not store.supervisor_instance_path().exists()


def test_claim_refuses_unrelated_pid_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(100, parent=1, path=PWSH, ticks=EXACT_START_TICKS)
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(
        lifecycle,
        "_validate_ancestry",
        lambda observed: (_ for _ in ()).throw(
            lifecycle.SupervisorLifecycleError("manual --claim-instance is unsupported")
        ),
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="unsupported"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        )
    assert not store.supervisor_instance_path().exists()


def test_generated_claim_reports_corrupt_marker_recovery_before_write(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    marker = store.supervisor_instance_path()
    marker.write_text("{broken", encoding="utf-8")

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="repair-instance-marker"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=os.getpid(),
            pid_start="locator",
            validate_artifacts=lambda: None,
        )
    assert marker.read_text(encoding="utf-8") == "{broken"


def test_generated_claim_lock_order_is_lifecycle_selection_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    events: list[str] = []

    def lock(name: str):
        @contextlib.contextmanager
        def held():
            events.append("enter:" + name)
            try:
                yield
            finally:
                events.append("exit:" + name)

        return held()

    monkeypatch.setattr(store, "_supervisor_lifecycle_lock", lambda: lock("lifecycle"))
    monkeypatch.setattr(store, "_powershell_selection_lock", lambda: lock("selection"))
    monkeypatch.setattr(store, "_config_lock", lambda: lock("config"))
    selected = _selected(store)
    host = _observation(100, parent=1, path=PWSH, ticks=EXACT_START_TICKS)
    current = _observation(os.getpid(), parent=100, path=r"C:\Python\python.exe", ticks=200)
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_validate_ancestry", lambda observed: (current,))
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)
    monkeypatch.setattr(psh, "native_file_identity", lambda path: host.identity)

    claim = lifecycle.claim_powershell_supervisor(
        store,
        pid=host.pid,
        pid_start=host.creation_token,
        validate_artifacts=lambda: events.append("artifacts"),
    )
    assert claim is not None
    assert claim["pid_start_filetime"] == str(host.creation_ticks)
    assert events == [
        "enter:lifecycle", "artifacts", "enter:selection", "enter:config",
        "exit:config", "exit:selection", "exit:lifecycle",
    ]


def test_generated_claim_preserves_exact_local_offset_start_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(
        100,
        parent=1,
        path=PWSH,
        ticks=134_285_904_001_234_567,
        start_locator="2026-07-15T12:00:00.1234567Z",
    )
    current = _observation(
        os.getpid(), parent=100, path=r"C:\Python\python.exe", ticks=200
    )
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_validate_ancestry", lambda observed: (current,))
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)
    monkeypatch.setattr(psh, "native_file_identity", lambda path: host.identity)

    caller_start = "2026-07-15T14:00:00.1234567+02:00"
    claim = lifecycle.claim_powershell_supervisor(
        store,
        pid=host.pid,
        pid_start=caller_start,
        validate_artifacts=lambda: None,
    )

    assert claim is not None
    assert claim["pid_start"] == caller_start
    assert claim["pid_start_filetime"] == str(host.creation_ticks)


def test_generated_claim_rejects_merely_close_start_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(
        100,
        parent=1,
        path=PWSH,
        ticks=134_285_904_001_234_567,
        start_locator="2026-07-15T12:00:00.1234567Z",
    )
    monkeypatch.setattr(lifecycle, "_read_valid_selection_locked", lambda store: selected)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(
        lifecycle,
        "_validate_ancestry",
        lambda observed: pytest.fail("an inexact locator must fail before ancestry"),
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="reused or ambiguous"):
        lifecycle.claim_powershell_supervisor(
            store,
            pid=host.pid,
            pid_start="2026-07-15T12:00:00.1230000Z",
            validate_artifacts=lambda: None,
        )

    assert not store.supervisor_instance_path().exists()


@WINDOWS_ONLY
def test_lifecycle_barrier_blocks_claim_select_and_refresh_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")
    before = {
        relative: (store.dir / Path(relative)).read_bytes()
        for relative in sup.ARTIFACT_RELATIVE_PATHS
    }
    original_lock = store._supervisor_lifecycle_lock
    outer = original_lock(timeout=1.0, poll=0.01)
    monkeypatch.setattr(
        store,
        "_supervisor_lifecycle_lock",
        lambda: original_lock(timeout=0.05, poll=0.005),
    )
    resolution = psh.Resolution(
        _probe(), (psh.CandidateAttempt(PWSH, "explicit", True, "accepted"),),
    )
    monkeypatch.setattr(psh, "resolve_candidate", lambda **kwargs: resolution)
    monkeypatch.setattr(psh, "native_file_identity", lambda path: _identity())
    start = _process_start_token(os.getpid())
    assert start is not None

    with outer:
        with pytest.raises(OSError, match="supervisor lifecycle lock"):
            lifecycle.select_powershell_host(store, explicit_path=PWSH)
        with pytest.raises(OSError, match="supervisor lifecycle lock"):
            sup.refresh_artifacts(store, python_exe=r"C:\PythonB\python.exe")
        with pytest.raises(OSError, match="supervisor lifecycle lock"):
            store.claim_supervisor_instance(pid=os.getpid(), pid_start=start)

    assert not lifecycle.selection_path(store).exists()
    assert not store.supervisor_instance_path().exists()
    assert before == {
        relative: (store.dir / Path(relative)).read_bytes()
        for relative in sup.ARTIFACT_RELATIVE_PATHS
    }
    assert not list(store.dir.rglob("*.tmp"))


def test_generic_python_claim_remains_host_agnostic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _process_start_token(os.getpid())
    claim = store.claim_supervisor_instance(pid=os.getpid(), pid_start=start)
    assert claim is not None
    assert claim["pid_start"] == start
    assert not lifecycle.selection_path(store).exists()
    assert store.release_supervisor_instance(
        token=claim["token"], pid=os.getpid(), pid_start=start,
    )


def test_generic_python_claim_accepts_unknown_process_start(tmp_path: Path) -> None:
    store = _store(tmp_path)
    claim = store.claim_supervisor_instance(pid=os.getpid(), pid_start=None)
    assert claim is not None
    assert claim["pid_start"] is None
    assert store.release_supervisor_instance(
        token=claim["token"], pid=os.getpid(), pid_start=None,
    )


def test_expired_selection_reprobes_exact_host_without_renewing_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    result = _probe()
    record = psh.make_selection_record(
        result,
        project_id=store.project_id(),
        now=1000.0,
    )
    lifecycle._atomic_write_selection(lifecycle.selection_path(store), record)
    before = lifecycle.selection_path(store).read_bytes()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(psh, "native_file_identity", lambda path: result.identity)

    def reprobe(path, *, source):
        calls.append((path, source))
        return result

    monkeypatch.setattr(psh, "probe_candidate", reprobe)
    selected = lifecycle.read_selected_host(
        store,
        now=1000.0 + psh.SELECTION_TTL_SECONDS + 1,
    )
    assert selected["_expired"] is True
    assert calls == [(PWSH, "explicit")]
    assert lifecycle.selection_path(store).read_bytes() == before


def test_atomic_selection_failure_preserves_old_bytes_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    path = lifecycle.selection_path(store)
    path.write_bytes(b"old-selection\n")

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(lifecycle.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        lifecycle._atomic_write_selection(path, {"schema": "test"})
    assert path.read_bytes() == b"old-selection\n"
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


@WINDOWS_ONLY
def test_explicit_selection_repairs_invalid_record_under_writer_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    path = lifecycle.selection_path(store)
    path.write_text("{broken", encoding="utf-8")
    resolution = psh.Resolution(
        _probe(), (psh.CandidateAttempt(PWSH, "explicit", True, "accepted"),),
    )
    monkeypatch.setattr(psh, "resolve_candidate", lambda **kwargs: resolution)
    monkeypatch.setattr(psh, "native_file_identity", lambda candidate: _identity())

    record, _attempts = lifecycle.select_powershell_host(store, explicit_path=PWSH)

    assert record["path"] == PWSH
    assert record["selection_revision"] == 1
    assert lifecycle.read_selected_host(store)["path"] == PWSH


@WINDOWS_ONLY
def test_task_install_commit_refuses_concurrent_selection_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    initial = psh.make_selection_record(
        _probe(),
        project_id=store.project_id(),
        now=1000.0,
    )
    lifecycle._atomic_write_selection(lifecycle.selection_path(store), initial)
    before = lifecycle.selection_path(store).read_bytes()
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_probe_observed_current_host", lambda observed: _probe())
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="changed during"):
        lifecycle.commit_task_install(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            task_name="agenttalk-custom",
            expected_revision=initial["selection_revision"] + 1,
            expected_fingerprint=initial["selection_fingerprint"],
            validate_artifacts=lambda: None,
        )
    assert json.loads(lifecycle.selection_path(store).read_text(encoding="utf-8"))[
        "task_name"
    ] is None
    assert lifecycle.selection_path(store).read_bytes() == before


@WINDOWS_ONLY
def test_task_install_prepare_refuses_different_existing_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    initial = psh.make_selection_record(
        _probe(),
        project_id=store.project_id(),
        task_name="old-task",
    )
    lifecycle._atomic_write_selection(lifecycle.selection_path(store), initial)
    before = lifecycle.selection_path(store).read_bytes()
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_probe_observed_current_host", lambda observed: _probe())
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="different Scheduled Task binding",
    ):
        lifecycle.prepare_task_install(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            task_name="new-task",
            validate_artifacts=lambda: None,
        )

    assert lifecycle.selection_path(store).read_bytes() == before


@WINDOWS_ONLY
def test_task_uninstall_clears_binding_before_new_name_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    initial = psh.make_selection_record(
        _probe(),
        project_id=store.project_id(),
        task_name="old-task",
    )
    lifecycle._atomic_write_selection(lifecycle.selection_path(store), initial)
    monkeypatch.setattr(psh, "native_file_identity", lambda path: _identity())

    cleared = lifecycle.clear_task_binding(store, task_name="old-task")

    assert cleared["task_name"] is None
    assert cleared["selection_revision"] == initial["selection_revision"] + 1
    assert cleared["selection_fingerprint"] != initial["selection_fingerprint"]

    host = _observation(100, parent=1, path=PWSH, ticks=100)
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_probe_observed_current_host", lambda observed: _probe())
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)

    prepared = lifecycle.prepare_task_install(
        store,
        pid=host.pid,
        pid_start=host.creation_token,
        task_name="new-task",
        validate_artifacts=lambda: None,
    )
    assert prepared["task_name"] == "new-task"
