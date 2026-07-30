from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import sys
import sysconfig
from pathlib import Path

import pytest

from agenttalk import powershell_host as psh
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
) -> lifecycle.ProcessObservation:
    return lifecycle.ProcessObservation(
        pid=pid,
        parent_pid=parent,
        path=path,
        creation_token="2026-07-15T12:00:00.123456Z",
        creation_ticks=ticks,
        identity=identity or _identity(path),
        handle=0,
    )


def _patch_observed_chain(
    monkeypatch: pytest.MonkeyPatch,
    host: lifecycle.ProcessObservation,
    *,
    intermediate: tuple[tuple[str, psh.NativeFileIdentity, int], ...],
    current_path: str,
    current_identity: psh.NativeFileIdentity,
    current_ticks: int = 500,
) -> tuple[
    lifecycle.ProcessObservation,
    tuple[lifecycle.ProcessObservation, ...],
]:
    current_pid = os.getpid()
    observations: dict[int, lifecycle.ProcessObservation] = {}
    parent_pid = host.pid
    ordered: list[lifecycle.ProcessObservation] = []
    for index, (path, identity, ticks) in enumerate(intermediate):
        pid = 200 + index
        item = _observation(
            pid,
            parent=parent_pid,
            path=path,
            ticks=ticks,
            identity=identity,
        )
        observations[pid] = item
        ordered.append(item)
        parent_pid = pid
    current = _observation(
        current_pid,
        parent=parent_pid,
        path=current_path,
        ticks=current_ticks,
        identity=current_identity,
    )
    observations[current_pid] = current
    monkeypatch.setattr(
        lifecycle,
        "_process_parent_map",
        lambda: {
            pid: observation.parent_pid
            for pid, observation in observations.items()
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: observations[pid],
    )
    return current, tuple(ordered)


def test_dotnet_seven_digit_creation_locator_matches_kernel_token() -> None:
    assert lifecycle.start_tokens_match(
        "2026-07-15T13:05:52.062092Z",
        "2026-07-15T15:05:52.0620917+02:00",
    )


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("name", "intermediate_paths"),
    [
        ("direct-pwsh", ()),
        ("cmd-hop", (r"C:\Windows\System32\cmd.exe",)),
        ("venv-launcher", (r"C:\venv\Scripts\python.exe",)),
        (
            "generated-bin-shim",
            (
                r"C:\Windows\System32\cmd.exe",
                r"C:\venv\Scripts\python.exe",
            ),
        ),
        (
            "wheel-console-script",
            (
                r"C:\venv\Scripts\agenttalk.exe",
                r"C:\venv\Scripts\python.exe",
            ),
        ),
        (
            "cmd-wheel-console-script",
            (
                r"C:\Windows\System32\cmd.exe",
                r"C:\venv\Scripts\agenttalk.exe",
                r"C:\venv\Scripts\python.exe",
            ),
        ),
    ],
)
def test_validate_ancestry_accepts_identified_generated_launch_classes(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    intermediate_paths: tuple[str, ...],
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current_pid = os.getpid()
    base_python = r"C:\Python\python.exe"
    identities = {
        path: _identity(path, f"{index + 1:02x}")
        for index, path in enumerate(
            (
                r"C:\Windows\System32\cmd.exe",
                r"C:\venv\Scripts\agenttalk.exe",
                r"C:\venv\Scripts\python.exe",
                base_python,
            )
        )
    }
    observations: dict[int, lifecycle.ProcessObservation] = {}
    parent_pid = host.pid
    for index, path in enumerate(intermediate_paths):
        pid = 200 + index
        observations[pid] = _observation(
            pid,
            parent=parent_pid,
            path=path,
            ticks=200 + index,
            identity=identities[path],
        )
        parent_pid = pid
    current = _observation(
        current_pid,
        parent=parent_pid,
        path=base_python,
        ticks=300,
        identity=identities[base_python],
    )
    observations[current_pid] = current
    parent_map = {
        pid: observation.parent_pid
        for pid, observation in observations.items()
    }
    monkeypatch.setattr(lifecycle, "_process_parent_map", lambda: parent_map)
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: observations[pid],
    )
    uses_venv = r"C:\venv\Scripts\python.exe" in intermediate_paths
    monkeypatch.setattr(
        sys,
        "executable",
        r"C:\venv\Scripts\python.exe" if uses_venv else base_python,
    )
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\venv" if uses_venv else r"C:\Python")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            (
                r"C:\venv\Scripts\agenttalk"
                if r"C:\venv\Scripts\agenttalk.exe" in intermediate_paths
                else r"C:\venv\Lib\site-packages\agenttalk\__main__.py"
            )
        ],
    )
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: (
            (r"C:\venv\Scripts" if uses_venv else r"C:\Python\Scripts")
            if name == "scripts"
            else None
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "_system_cmd_path",
        lambda: r"C:\Windows\System32\cmd.exe",
        raising=False,
    )
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: identities[str(path)],
    )

    expected = (current,) + tuple(
        observations[200 + index]
        for index in reversed(range(len(intermediate_paths)))
    )
    assert lifecycle._validate_ancestry(host) == expected, name


@WINDOWS_ONLY
@pytest.mark.parametrize("with_cmd", [False, True])
def test_validate_ancestry_accepts_base_install_console_script(
    monkeypatch: pytest.MonkeyPatch,
    with_cmd: bool,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current_pid = os.getpid()
    cmd_path = r"C:\Windows\System32\cmd.exe"
    console_path = r"C:\Python\Scripts\agenttalk.exe"
    base_python = r"C:\Python\python.exe"
    cmd = _observation(
        200,
        parent=host.pid,
        path=cmd_path,
        ticks=200,
        identity=_identity(cmd_path, "0f"),
    )
    console = _observation(
        201,
        parent=cmd.pid if with_cmd else host.pid,
        path=console_path,
        ticks=210,
        identity=_identity(console_path, "10"),
    )
    current = _observation(
        current_pid,
        parent=console.pid,
        path=base_python,
        ticks=300,
        identity=_identity(base_python, "11"),
    )
    observations = {current_pid: current, console.pid: console}
    if with_cmd:
        observations[cmd.pid] = cmd
    monkeypatch.setattr(
        lifecycle,
        "_process_parent_map",
        lambda: {pid: item.parent_pid for pid, item in observations.items()},
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: observations[pid],
    )
    monkeypatch.setattr(sys, "executable", base_python)
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\Python")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\agenttalk"])
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: r"C:\Python\Scripts" if name == "scripts" else None,
    )
    monkeypatch.setattr(lifecycle, "_system_cmd_path", lambda: cmd_path)
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: (
            cmd.identity
            if str(path) == cmd_path
            else console.identity
            if str(path) == console_path
            else current.identity
        ),
    )

    expected = (current, console, cmd) if with_cmd else (current, console)
    assert lifecycle._validate_ancestry(host) == expected


@WINDOWS_ONLY
def test_validate_ancestry_refuses_venv_launcher_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    launcher_path = r"C:\venv\Scripts\python.exe"
    base_python = r"C:\Python\python.exe"
    launcher_identity = _identity(launcher_path, "20")
    base_identity = _identity(base_python, "21")
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=((launcher_path, launcher_identity, 200),),
        current_path=base_python,
        current_identity=base_identity,
    )
    monkeypatch.setattr(sys, "executable", launcher_path)
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\venv")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: r"C:\venv\Scripts" if name == "scripts" else None,
    )
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: (
            _identity(launcher_path, "different")
            if str(path) == launcher_path
            else base_identity
        ),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="virtual-environment Python launcher image identity",
    ):
        lifecycle._validate_ancestry(host)


@WINDOWS_ONLY
def test_validate_ancestry_refuses_missing_venv_redirector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    launcher_path = r"C:\venv\Scripts\python.exe"
    base_python = r"C:\Python\python.exe"
    base_identity = _identity(base_python, "30")
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=(),
        current_path=base_python,
        current_identity=base_identity,
    )
    monkeypatch.setattr(sys, "executable", launcher_path)
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\venv")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: _identity(str(path), "31"),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="running Python image identity",
    ):
        lifecycle._validate_ancestry(host)


@WINDOWS_ONLY
def test_validate_ancestry_refuses_unattributed_console_and_python_launchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    base_python = r"C:\Python\python.exe"
    base_identity = _identity(base_python, "32")
    console_path = r"C:\Python\Scripts\agenttalk.exe"
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=((console_path, _identity(console_path, "33"), 200),),
        current_path=base_python,
        current_identity=base_identity,
    )
    monkeypatch.setattr(sys, "executable", base_python)
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\Python")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(sys, "argv", [r"C:\Tools\not-agenttalk.py"])
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: r"C:\Python\Scripts" if name == "scripts" else None,
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="not the active agenttalk entry point",
    ):
        lifecycle._validate_ancestry(host)

    arbitrary_python = r"C:\Tools\python.exe"
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=(
            (arbitrary_python, _identity(arbitrary_python, "34"), 200),
        ),
        current_path=base_python,
        current_identity=base_identity,
    )
    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="not the active virtual-environment launcher",
    ):
        lifecycle._validate_ancestry(host)


@WINDOWS_ONLY
def test_validate_ancestry_refuses_replaced_console_script_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    console_path = r"C:\Python\Scripts\agenttalk.exe"
    base_python = r"C:\Python\python.exe"
    console_identity = _identity(console_path, "35")
    base_identity = _identity(base_python, "36")
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=((console_path, console_identity, 200),),
        current_path=base_python,
        current_identity=base_identity,
    )
    monkeypatch.setattr(sys, "executable", base_python)
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\Python")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\agenttalk"])
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: r"C:\Python\Scripts" if name == "scripts" else None,
    )
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: (
            _identity(console_path, "replaced")
            if str(path) == console_path
            else base_identity
        ),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="agenttalk console-script ancestor image identity",
    ):
        lifecycle._validate_ancestry(host)


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("name", "intermediate_paths", "match"),
    [
        (
            "reordered",
            (
                r"C:\venv\Scripts\python.exe",
                r"C:\Windows\System32\cmd.exe",
            ),
            "launcher order",
        ),
        (
            "duplicate",
            (
                r"C:\Windows\System32\cmd.exe",
                r"C:\Windows\System32\cmd.exe",
            ),
            "launcher order",
        ),
        (
            "over-depth",
            (
                r"C:\Windows\System32\cmd.exe",
                r"C:\venv\Scripts\agenttalk.exe",
                r"C:\venv\Scripts\python.exe",
                r"C:\Windows\System32\cmd.exe",
            ),
            "too many",
        ),
    ],
)
def test_validate_ancestry_refuses_unbounded_or_reordered_launchers(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    intermediate_paths: tuple[str, ...],
    match: str,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    base_python = r"C:\Python\python.exe"
    identities = {
        path: _identity(path, f"{index + 40:02x}")
        for index, path in enumerate(set(intermediate_paths) | {base_python})
    }
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=tuple(
            (path, identities[path], 200 + index)
            for index, path in enumerate(intermediate_paths)
        ),
        current_path=base_python,
        current_identity=identities[base_python],
    )
    uses_venv = r"C:\venv\Scripts\python.exe" in intermediate_paths
    monkeypatch.setattr(
        sys,
        "executable",
        r"C:\venv\Scripts\python.exe" if uses_venv else base_python,
    )
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\venv" if uses_venv else r"C:\Python")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            (
                r"C:\venv\Scripts\agenttalk"
                if r"C:\venv\Scripts\agenttalk.exe" in intermediate_paths
                else r"C:\agenttalk\__main__.py"
            )
        ],
    )
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name: r"C:\venv\Scripts" if name == "scripts" else None,
    )
    monkeypatch.setattr(
        lifecycle,
        "_system_cmd_path",
        lambda: r"C:\Windows\System32\cmd.exe",
    )
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: identities[str(path)],
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match=match):
        lifecycle._validate_ancestry(host)


@WINDOWS_ONLY
def test_validate_ancestry_refuses_copied_cmd_and_start_inversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    copied_cmd = r"C:\Tools\cmd.exe"
    base_python = r"C:\Python\python.exe"
    copied_identity = _identity(copied_cmd, "50")
    base_identity = _identity(base_python, "51")
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=((copied_cmd, copied_identity, 90),),
        current_path=base_python,
        current_identity=base_identity,
    )
    monkeypatch.setattr(sys, "executable", base_python)
    monkeypatch.setattr(sys, "_base_executable", base_python)
    monkeypatch.setattr(sys, "prefix", r"C:\Python")
    monkeypatch.setattr(sys, "base_prefix", r"C:\Python")
    monkeypatch.setattr(
        lifecycle,
        "_system_cmd_path",
        lambda: r"C:\Windows\System32\cmd.exe",
    )
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: (
            _identity(r"C:\Windows\System32\cmd.exe", "52")
            if str(path) == r"C:\Windows\System32\cmd.exe"
            else base_identity
        ),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="command-interpreter ancestor image identity",
    ):
        lifecycle._validate_ancestry(host)

    system_cmd = _identity(r"C:\Windows\System32\cmd.exe", "52")
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=((r"C:\Windows\System32\cmd.exe", system_cmd, 90),),
        current_path=base_python,
        current_identity=base_identity,
    )
    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="start times are inconsistent",
    ):
        lifecycle._validate_ancestry(host)


@WINDOWS_ONLY
def test_validate_ancestry_refuses_launcher_identity_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    cmd_path = r"C:\Windows\System32\cmd.exe"
    base_python = r"C:\Python\python.exe"
    _patch_observed_chain(
        monkeypatch,
        host,
        intermediate=((cmd_path, _identity(cmd_path, "60"), 200),),
        current_path=base_python,
        current_identity=_identity(base_python, "61"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_system_cmd_path",
        lambda: cmd_path,
    )
    monkeypatch.setattr(
        psh,
        "native_file_identity",
        lambda path: (_ for _ in ()).throw(
            psh.PowerShellHostError("access denied")
        ),
    )

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="cannot identify command-interpreter ancestor",
    ):
        lifecycle._validate_ancestry(host)


def test_validate_ancestry_refuses_unrelated_manual_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current_pid = os.getpid()
    current = _observation(current_pid, parent=200, path=r"C:\Python\python.exe", ticks=300)
    unrelated = _observation(
        200,
        parent=host.pid,
        path=r"C:\Tools\runner.exe",
        ticks=200,
    )
    monkeypatch.setattr(
        lifecycle,
        "_process_parent_map",
        lambda: {current_pid: 200, 200: host.pid},
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: current if pid == current_pid else unrelated,
    )
    with pytest.raises(lifecycle.SupervisorLifecycleError, match="unidentified"):
        lifecycle._validate_ancestry(host)


def test_claim_rechecks_selection_before_marker_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _selected(store)
    second = _selected(store, path=r"D:\PowerShell\pwsh.exe", file_id="02", revision=2)
    reads = iter((first, second))
    host = _observation(100, parent=1, path=PWSH, ticks=100)
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
    host = _observation(100, parent=1, path=PWSH, ticks=100)
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
        ticks=100,
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
    host = _observation(100, parent=1, path=PWSH, ticks=100)
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
    host = _observation(100, parent=1, path=PWSH, ticks=100)
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


def test_observer_refuses_unrelated_powershell_pid_without_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current_pid = os.getpid()
    current = _observation(
        current_pid,
        parent=200,
        path=r"C:\Python\python.exe",
        ticks=300,
    )
    unrelated = _observation(
        200,
        parent=host.pid,
        path=r"C:\Tools\runner.exe",
        ticks=200,
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_valid_selection_locked",
        lambda store: selected,
    )
    monkeypatch.setattr(
        lifecycle,
        "_process_parent_map",
        lambda: {current_pid: 200, 200: host.pid},
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda pid, parents=None: (
            host
            if pid == host.pid
            else current
            if pid == current_pid
            else unrelated
        ),
    )

    with pytest.raises(lifecycle.SupervisorLifecycleError, match="unidentified"):
        with lifecycle.checked_powershell_supervisor_observer(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        ):
            pytest.fail("unrelated caller reached the observation write")
    assert not store.supervisor_instance_path().exists()


def test_observer_refuses_exited_identified_launcher_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    selected = _selected(store)
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    launcher = _observation(
        200,
        parent=host.pid,
        path=r"C:\venv\Scripts\python.exe",
        ticks=200,
    )
    current = _observation(
        os.getpid(),
        parent=launcher.pid,
        path=r"C:\Python\python.exe",
        ticks=300,
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_valid_selection_locked",
        lambda store: selected,
    )
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(
        lifecycle,
        "_validate_ancestry",
        lambda observed: (current, launcher),
    )

    def require_active(observed: lifecycle.ProcessObservation) -> None:
        if observed is launcher:
            raise lifecycle.SupervisorLifecycleError(
                "process 200 exited during validation"
            )

    monkeypatch.setattr(lifecycle, "_require_process_active", require_active)

    with pytest.raises(
        lifecycle.SupervisorLifecycleError,
        match="exited during validation",
    ):
        with lifecycle.checked_powershell_supervisor_observer(
            store,
            pid=host.pid,
            pid_start=host.creation_token,
            validate_artifacts=lambda: None,
        ):
            pytest.fail("an exited launcher reached the observation write")
    assert not store.supervisor_instance_path().exists()


def test_observer_holds_lifecycle_selection_config_through_write(
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
    host = _observation(100, parent=1, path=PWSH, ticks=100)
    current = _observation(
        os.getpid(),
        parent=100,
        path=r"C:\Python\python.exe",
        ticks=200,
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_valid_selection_locked",
        lambda store: selected,
    )
    monkeypatch.setattr(lifecycle, "_open_process_observation", lambda pid: host)
    monkeypatch.setattr(lifecycle, "_validate_ancestry", lambda observed: (current,))
    monkeypatch.setattr(lifecycle, "_require_process_active", lambda observed: None)
    monkeypatch.setattr(psh, "native_file_identity", lambda path: host.identity)

    with lifecycle.checked_powershell_supervisor_observer(
        store,
        pid=host.pid,
        pid_start=host.creation_token,
        validate_artifacts=lambda: events.append("artifacts"),
    ):
        events.append("write")

    assert events == [
        "enter:lifecycle",
        "artifacts",
        "enter:selection",
        "enter:config",
        "write",
        "exit:config",
        "exit:selection",
        "exit:lifecycle",
    ]
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
    host = _observation(100, parent=1, path=PWSH, ticks=100)
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
    assert events == [
        "enter:lifecycle", "artifacts", "enter:selection", "enter:config",
        "exit:config", "exit:selection", "exit:lifecycle",
    ]


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

    competing_calls = (
        lambda: lifecycle.select_powershell_host(store, explicit_path=PWSH),
        lambda: sup.refresh_artifacts(
            store, python_exe=r"C:\PythonB\python.exe",
        ),
        lambda: store.claim_supervisor_instance(
            pid=os.getpid(), pid_start=start,
        ),
    )
    with outer, concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
    ) as executor:
        for call in competing_calls:
            with pytest.raises(OSError, match="supervisor lifecycle lock"):
                executor.submit(call).result(timeout=1.0)

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
