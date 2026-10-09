"""Hand-run probes cannot change the store a seat inherited by mistake.

The incident of 2026-10-08: a reviewer meant to try commands in a throwaway store,
but its shell still held the wrapped seat's AGENTTALK_ROOT, so ``init``, ``roster
set-operator-facing`` and ``supervise --bootstrap-check`` all acted on the live store.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agenttalk
from agenttalk import cli
from agenttalk.store import Store

SRC = Path(agenttalk.__file__).resolve().parents[1]   # the agenttalk under test, also in subprocesses


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A live-looking store, and a shell that inherited it the way a wrapped seat does.
    Like the reviewer's shell it has no store fence: the suite's fence is lifted for these
    tests, whose stores all stay inside their temporary folder."""
    root = tmp_path / "live"
    Store(root).init(["reviewer", "lead-seat", "worker-seat"])
    monkeypatch.delenv("AGENTTALK_STORE_FENCE")
    monkeypatch.setenv("AGENTTALK_ROOT", str(root))
    monkeypatch.setenv("AGENTTALK_SELF", "reviewer")
    monkeypatch.setenv("AGENTTALK_WRAPPER_GENERATION", "1")
    probe = tmp_path / "probe"
    probe.mkdir()
    monkeypatch.chdir(probe)
    return root


def test_the_incident_commands_refuse_or_name_the_inherited_store_before_writing(
    live: Path, capsys: pytest.CaptureFixture,
) -> None:
    before = _snapshot(live)

    assert cli.main(["init", "--agents", "lead,worker"]) == 2
    init_err = capsys.readouterr().err
    assert str(live) in init_err and "AGENTTALK_ROOT" in init_err
    assert "agenttalk scratch store" in init_err
    assert _snapshot(live) == before                                       # init wrote nothing
    config = (live / ".agenttalk" / "config.json").read_bytes()

    assert cli.main(["roster", "set-operator-facing", "lead"]) == 2      # not on that roster
    roster_err = capsys.readouterr().err
    assert f"changing the store at {live} (from the inherited AGENTTALK_ROOT)" in roster_err

    cli.main(["supervise", "--bootstrap-check"])                           # reads only
    capsys.readouterr()
    assert (live / ".agenttalk" / "config.json").read_bytes() == config


def test_init_still_uses_an_inherited_root_when_named_or_new(
    live: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    assert cli.main(["--root", str(live), "init", "--agents", "reviewer"]) == 0   # named on purpose
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("AGENTTALK_ROOT", str(fresh))                  # a new project's root
    assert cli.main(["init", "--agents", "lead,worker"]) == 0
    assert Store(fresh).initialized()
    capsys.readouterr()


def test_a_roster_change_names_where_its_store_came_from_only_when_inherited(
    live: Path, capsys: pytest.CaptureFixture,
) -> None:
    assert cli.main(["roster", "set-role", "worker-seat", "reviewer"]) == 0
    assert "from the inherited AGENTTALK_ROOT" in capsys.readouterr().err
    assert cli.main(["--root", str(live), "roster", "set-role", "worker-seat", "builder"]) == 0
    assert "inherited" not in capsys.readouterr().err


def test_a_seats_own_bus_commands_stay_silent_on_an_inherited_store(
    live: Path, capsys: pytest.CaptureFixture,
) -> None:
    for argv in (
        ["send", "--from", "reviewer", "--to", "lead-seat", "-m", "hello"],
        ["threads", "--for", "lead-seat"],
        ["recv", "--for", "lead-seat"],
        ["roster"],
        ["roster", "add", "reviewer"],
        ["knowledge", "search", "anything"],
        ["whoami"],
    ):
        cli.main(argv)
        assert "inherited AGENTTALK_ROOT" not in capsys.readouterr().err, argv


def test_scratch_store_makes_a_throwaway_store_and_says_how_to_use_it(
    live: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    assert cli.main(["scratch", "store", "--agents", "lead,worker"]) == 0
    out = capsys.readouterr().out
    path = Path(next(line for line in out.splitlines() if "ready at" in line).split("ready at ", 1)[1].split(" (")[0])
    assert path.resolve() != live.resolve() and Store(path).load_config()["agents"] == ["lead", "worker"]
    assert "AGENTTALK_STORE_FENCE" in out and "unset AGENTTALK_SELF" in out and "Remove-Item Env:AGENTTALK_SELF" in out

    for shell in ("bash", "powershell"):
        assert cli.main(["scratch", "store", "--shell", shell]) == 0
        line = capsys.readouterr().out.strip()
        assert "\n" not in line and "AGENTTALK_ROOT" in line and "AGENTTALK_STORE_FENCE" in line


def _run_printed(shell: str, line: str, *commands: str) -> subprocess.CompletedProcess:
    python = sys.executable
    if shell == "bash":
        script = line + "\n" + "\n".join(f"{python!r} -B -m agenttalk {c}" for c in commands)
        argv = ["bash", "-c", script]
    else:
        script = line + "\n" + "\n".join(f"& '{python}' -B -m agenttalk {c}; $LASTEXITCODE" for c in commands)
        argv = [shutil.which("pwsh") or shutil.which("powershell") or "pwsh", "-NoProfile", "-Command", script]
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300)


def _shells() -> list[str]:
    shells = []
    if sys.platform != "win32" and shutil.which("bash"):
        shells.append("bash")              # Windows' bash may be WSL, which cannot run this Python
    if shutil.which("pwsh") or (sys.platform == "win32" and shutil.which("powershell")):
        shells.append("powershell")
    return shells


@pytest.mark.subprocess
@pytest.mark.parametrize("shell", _shells())
def test_the_printed_line_moves_the_incident_commands_to_the_throwaway_store(
    shell: str, live: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Run the printed line unchanged, then the reviewer's three commands: they act on the
    throwaway store, the seat's identity is gone, and the live store cannot be opened even
    by name."""
    before = _snapshot(live)
    assert cli.main(["scratch", "store", "--agents", "lead,worker", "--shell", shell]) == 0
    line = capsys.readouterr().out.strip()
    run = _run_printed(
        shell, line,
        "init --agents lead,worker",
        "roster set-operator-facing lead",
        "supervise --bootstrap-check",
        f"--root '{live}' roster",
    )
    out = run.stdout + run.stderr
    assert "from the inherited AGENTTALK_ROOT" not in out, out
    assert "AGENTTALK_STORE_FENCE" in out, out            # the live store, even named, is refused
    shown = _run_printed(shell, line, "roster --json")
    roster = json.loads(shown.stdout[shown.stdout.index("{"):shown.stdout.rindex("}") + 1])
    assert roster["agents"] == ["lead", "worker"] and not roster.get("self"), shown.stdout + shown.stderr
    assert roster["operator_facing"] == "lead", roster     # the change went to the throwaway store
    assert _snapshot(live) == before
