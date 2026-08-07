from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import supervisor as sup
from agenttalk import supervisor_lifecycle as lifecycle
from agenttalk import web
from agenttalk.store import Store


PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"
WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="PowerShell host is Windows-only",
)


class _Server:
    server_address = ("127.0.0.1", 43210)

    def __init__(self, events: list[str]):
        self.events = events

    def serve_forever(self) -> None:
        self.events.append("serve")
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.events.append("close")


def _store(tmp_path: Path, *, artifacts: bool = True) -> Store:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    if artifacts:
        sup.init(store)
    return store


def _host() -> dict:
    return {
        "path": PWSH,
        "selection_revision": 1,
        "selection_fingerprint": "a" * 64,
        "_warning": None,
    }


def _spawned_identity(pid: int) -> lifecycle.SpawnedProcessIdentity:
    return lifecycle.SpawnedProcessIdentity(
        pid=pid,
        pid_start="2026-07-15T12:00:00.0000000Z",
        start_filetime=134_285_904_000_000_000,
    )


@WINDOWS_ONLY
def test_start_host_failure_occurs_before_server_bind(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    monkeypatch.setattr(
        web,
        "make_server",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("server bound too early")),
    )

    assert cli.main([
        "--root", str(tmp_path), "start", "--no-browser", "--port", "0",
    ]) == 3
    assert "select-pwsh" in capsys.readouterr().err


def test_select_lock_failure_is_a_deterministic_cli_refusal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    monkeypatch.setattr(
        lifecycle,
        "select_powershell_host",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TimeoutError("PowerShell selection lock is busy")
        ),
    )

    assert cli.main([
        "--root", str(tmp_path), "supervise", "--select-pwsh",
    ]) == 3
    assert "selection lock is busy" in capsys.readouterr().err


@WINDOWS_ONLY
def test_start_validates_before_bind_and_launches_absolute_selected_host(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    real_validate = sup.validate_artifact_bundle

    def validate(*args, **kwargs):
        events.append("artifacts")
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(sup, "validate_artifact_bundle", validate)
    monkeypatch.setattr(
        lifecycle,
        "read_selected_host",
        lambda store: events.append("selection") or _host(),
    )

    @contextlib.contextmanager
    def selected(store):
        events.append("launch-selection")
        yield _host()

    monkeypatch.setattr(lifecycle, "selected_host_for_spawn", selected)
    monkeypatch.setattr(
        web,
        "make_server",
        lambda *args, **kwargs: events.append("bind") or _Server(events),
    )

    launched = []

    class Proc:
        pid = 321

        @staticmethod
        def poll():
            return None

    def popen(argv, **kwargs):
        events.append("launch")
        launched.append((argv, kwargs))
        claim = Store(tmp_path).claim_supervisor_instance(
            pid=Proc.pid,
            pid_start="2026-07-15T12:00:00Z",
        )
        assert claim is not None
        return Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(
        lifecycle,
        "observe_spawned_supervisor",
        lambda process: _spawned_identity(process.pid),
    )

    assert cli.main([
        "--root", str(tmp_path), "start", "--no-browser", "--port", "0",
    ]) == 0
    assert events.index("artifacts") < events.index("bind")
    assert events.index("selection") < events.index("bind")
    assert launched[0][0][:5] == [
        PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
    ]
    assert launched[0][0][5].endswith("supervisor.ps1")
    output = capsys.readouterr().err
    assert "supervisor started pid=321" in output
    assert "Ctrl-C stops the Team Console only" in output
    assert "--stop-instance --acknowledge-stop-supervisor" in output
    assert "Stop-Process" not in output
    assert "--repair-instance-marker" in output
    assert f"--root '{tmp_path.resolve()}' supervise --stop-instance" in output
    assert str(tmp_path.resolve() / ".agenttalk" / "supervisor.kill") in output


@WINDOWS_ONLY
def test_start_refuses_dead_marker_before_server_bind_or_spawn(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    monkeypatch.setattr(lifecycle, "read_selected_host", lambda _store: _host())
    monkeypatch.setattr(
        lifecycle,
        "assert_supervisor_start_precondition",
        lambda _store: (_ for _ in ()).throw(
            lifecycle.SupervisorLifecycleError(
                "marker holder is dead; run `agenttalk supervise "
                "--repair-instance-marker --quarantine "
                "--acknowledge-no-live-supervisor`"
            )
        ),
    )
    monkeypatch.setattr(
        web,
        "make_server",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("server bound despite failed marker precondition")
        ),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("spawned despite failed marker precondition")
        ),
    )

    assert cli.main(
        ["--root", str(tmp_path), "start", "--no-browser", "--port", "0"]
    ) == 3
    assert "repair-instance-marker" in capsys.readouterr().err


@WINDOWS_ONLY
def test_start_precondition_precedes_explicit_host_selection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "assert_supervisor_start_precondition",
        lambda _store: events.append("precondition") or (_ for _ in ()).throw(
            lifecycle.SupervisorLifecycleError("dead marker; repair-instance-marker")
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "select_powershell_host",
        lambda *args, **kwargs: events.append("selection") or pytest.fail(
            "selection must not auto-quarantine before the start precondition"
        ),
    )

    assert cli.main([
        "--root", str(tmp_path), "start", "--pwsh", PWSH,
        "--no-browser", "--port", "0",
    ]) == 3
    assert events == ["precondition"]
    assert "repair-instance-marker" in capsys.readouterr().err


@WINDOWS_ONLY
def test_start_does_not_report_unclaimed_spawn_pid(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(lifecycle, "read_selected_host", lambda _store: _host())

    @contextlib.contextmanager
    def selected(_store):
        yield _host()

    monkeypatch.setattr(lifecycle, "selected_host_for_spawn", selected)
    monkeypatch.setattr(
        web,
        "make_server",
        lambda *args, **kwargs: events.append("bind") or _Server(events),
    )

    class UnclaimedProcess:
        pid = 987654

        @staticmethod
        def poll():
            return 3

        @staticmethod
        def wait(timeout=None):
            return 3

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: UnclaimedProcess())
    monkeypatch.setattr(
        lifecycle,
        "observe_spawned_supervisor",
        lambda process: _spawned_identity(process.pid),
    )

    assert cli.main(
        ["--root", str(tmp_path), "start", "--no-browser", "--port", "0"]
    ) == 3
    output = capsys.readouterr().err
    assert "supervisor started pid=" not in output
    assert "claim did not succeed" in output
    assert events == ["bind", "close"]


@WINDOWS_ONLY
def test_start_terminates_an_alive_child_that_never_claimed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(lifecycle, "read_selected_host", lambda _store: _host())

    @contextlib.contextmanager
    def selected(_store):
        yield _host()

    monkeypatch.setattr(lifecycle, "selected_host_for_spawn", selected)
    monkeypatch.setattr(web, "make_server", lambda *args, **kwargs: _Server(events))

    class AliveUnclaimedProcess:
        pid = 987654
        terminated = False

        @classmethod
        def poll(cls):
            return 3 if cls.terminated else None

        @classmethod
        def terminate(cls) -> None:
            cls.terminated = True

        @classmethod
        def wait(cls, timeout=None):
            assert timeout == 5
            return 3

    monkeypatch.setattr(
        lifecycle,
        "wait_for_supervisor_claim",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            lifecycle.SupervisorLifecycleError("alive child never claimed")
        ),
    )
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *args, **kwargs: AliveUnclaimedProcess()
    )
    monkeypatch.setattr(
        lifecycle,
        "observe_spawned_supervisor",
        lambda process: _spawned_identity(process.pid),
    )

    assert cli.main(
        ["--root", str(tmp_path), "start", "--no-browser", "--port", "0"]
    ) == 3
    assert AliveUnclaimedProcess.terminated is True
    output = capsys.readouterr().err
    assert "supervisor started pid=" not in output
    assert "claim did not succeed" in output
    assert events == ["close"]


@WINDOWS_ONLY
def test_start_contains_child_when_identity_capture_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(lifecycle, "read_selected_host", lambda _store: _host())

    @contextlib.contextmanager
    def selected(_store):
        yield _host()

    monkeypatch.setattr(lifecycle, "selected_host_for_spawn", selected)
    monkeypatch.setattr(web, "make_server", lambda *args, **kwargs: _Server(events))

    class SpawnedProcess:
        pid = 987654
        terminated = False

        @classmethod
        def poll(cls):
            return 3 if cls.terminated else None

        @classmethod
        def terminate(cls) -> None:
            cls.terminated = True

        @classmethod
        def wait(cls, timeout=None):
            assert timeout == 5
            return 3

    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *args, **kwargs: SpawnedProcess()
    )
    monkeypatch.setattr(
        lifecycle,
        "_open_process_observation",
        lambda _pid: (_ for _ in ()).throw(
            lifecycle.psh.PowerShellHostError("image identity unavailable")
        ),
    )

    assert cli.main(
        ["--root", str(tmp_path), "start", "--no-browser", "--port", "0"]
    ) == 3
    assert SpawnedProcess.terminated is True
    output = capsys.readouterr().err
    assert "supervisor started pid=" not in output
    assert "image identity unavailable" in output
    assert events == ["close"]


def test_repair_command_quarantines_confirmed_dead_valid_marker(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    store = _store(tmp_path)
    assert store.claim_supervisor_instance(
        pid=987654,
        pid_start="2026-07-15T12:00:00Z",
    ) is not None
    monkeypatch.setattr(lifecycle, "_owner_identity_gone", lambda *_args: True)

    assert cli.main([
        "--root", str(tmp_path), "supervise", "--repair-instance-marker",
        "--quarantine", "--acknowledge-no-live-supervisor",
    ]) == 0
    assert "quarantined:" in capsys.readouterr().out
    assert not store.supervisor_instance_path().exists()


def test_stop_instance_requires_acknowledgement_and_reports_exact_pid(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _store(tmp_path)
    calls: list[Path] = []

    def stop(store: Store) -> dict:
        calls.append(store.root)
        return {"pid": 321}

    monkeypatch.setattr(lifecycle, "stop_supervisor_instance", stop)

    argv = ["--root", str(tmp_path), "supervise", "--stop-instance"]
    assert cli.main(argv) == 2
    assert calls == []
    assert "requires --acknowledge-stop-supervisor" in capsys.readouterr().err

    assert cli.main(argv + ["--acknowledge-stop-supervisor"]) == 0
    assert calls == [tmp_path]
    output = capsys.readouterr().out
    assert "stopped exact supervisor pid=321" in output
    assert "--repair-instance-marker" in output
    assert f"--root '{tmp_path.resolve()}' supervise --repair-instance-marker" in output


def test_start_no_supervisor_needs_no_powershell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "read_selected_host",
        lambda store: (_ for _ in ()).throw(AssertionError("PowerShell must not be used")),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no launch expected")),
    )
    monkeypatch.setattr(web, "make_server", lambda *args, **kwargs: _Server(events))

    assert cli.main([
        "--root", str(tmp_path), "start", "--no-supervisor", "--no-browser", "--port", "0",
    ]) == 0
    assert events == ["serve", "close"]


def test_non_windows_start_never_launches_generated_powershell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _store(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(cli, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no launch expected")),
    )
    monkeypatch.setattr(web, "make_server", lambda *args, **kwargs: _Server(events))

    assert cli.main([
        "--root", str(tmp_path), "start", "--no-browser", "--port", "0",
    ]) == 0
    assert events == ["serve", "close"]


def test_supervise_init_reports_partially_scaffolded_artifacts(
    tmp_path: Path,
    capsys,
) -> None:
    store = _store(tmp_path)
    (store.dir / "deadman.ps1").unlink()

    assert cli.main(["--root", str(tmp_path), "supervise", "--init"]) == 0

    output = capsys.readouterr().out
    assert "partially scaffolded" in output
    assert "--refresh-scripts" in output
    assert "all files already exist" not in output
