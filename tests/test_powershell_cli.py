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

    def popen(argv, **kwargs):
        events.append("launch")
        launched.append((argv, kwargs))
        return Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", popen)

    assert cli.main([
        "--root", str(tmp_path), "start", "--no-browser", "--port", "0",
    ]) == 0
    assert events.index("artifacts") < events.index("bind")
    assert events.index("selection") < events.index("bind")
    assert launched[0][0][:5] == [
        PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
    ]
    assert launched[0][0][5].endswith("supervisor.ps1")


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
