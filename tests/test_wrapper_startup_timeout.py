"""#714: the wrapper's start-up checks must let a healthy but slow start through (no saved compiled files, so
Python recompiles agenttalk), and must still refuse a check that hangs, in bounded time and in plain words."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import agenttalk.store as store_mod
from agenttalk import cli, lead_loop_cadence as cad
from agenttalk.store import PROC_ALIVE, Store
from agenttalk.wrapper import run, session
from agenttalk.wrapper.health import WrapperHealthWriter

SLOW_START = 8.0  # longer than the old 5-second limit: a cold start on a slow or busy machine


def _start_taking(seconds: float, healthy_output: str, *, raises: bool = False):
    """A runner standing in for a start that needs ``seconds``: under a shorter limit it times out, either as the real
    runner did before #714 (no exit code, the timeout's text) or by raising, as it does now; otherwise it succeeds.
    Records each limit it got."""
    limits: list[float] = []

    def runner(argv, cwd, env, timeout):
        limits.append(timeout)
        if timeout < seconds:
            if raises:
                raise subprocess.TimeoutExpired(argv, timeout)
            return None, f"TimeoutExpired: Command {argv!r} timed out after {timeout} seconds"
        return 0, healthy_output

    return runner, limits


def _hanging_agenttalk(tmp_path: Path) -> Path:
    """A folder whose `agenttalk` package never finishes importing, put first on PYTHONPATH."""
    package = tmp_path / "hang" / "agenttalk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    return package.parent


def _agenttalk_starting_a_lingering_process(tmp_path: Path) -> tuple[Path, Path]:
    """An `agenttalk` package that starts a long-lived process sharing its output, then hangs. The process writes its
    id to the returned file, so the test can stop exactly that process."""
    package = tmp_path / "linger" / "agenttalk"
    package.mkdir(parents=True)
    pid_file = tmp_path / "lingering.pid"
    (package / "__init__.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import os, sys, time; "
        "open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)', "
        f"{str(pid_file)!r}], stdout=sys.stdout, stderr=sys.stderr)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return package.parent, pid_file


def _stop_lingering(pid_file: Path) -> None:
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
        except (OSError, ValueError):
            pass  # already gone


def _plain_timeout_wording(text: str, seconds: str) -> None:
    assert f"agenttalk took longer than {seconds} seconds to start" in text, text
    assert "cold start without saved compiled Python files" in text, text
    assert "PYTHONDONTWRITEBYTECODE" in text and "once by hand" in text, text
    assert "TimeoutExpired" in text, text  # the word that callers already look for stays


def test_a_slow_but_healthy_launch_check_is_not_refused(tmp_path) -> None:
    runner, limits = _start_taking(SLOW_START, "agenttalk 0.test\n")
    env = {"AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path)}

    result = run.preflight_launch_runtime([sys.executable], "claude", tmp_path, env, runner=runner)

    assert result.blocked is None, result.blocked
    assert limits == [run.STARTUP_CHECK_TIMEOUT]


def test_a_slow_but_healthy_import_check_is_not_refused(tmp_path) -> None:
    # Raising: a time-out reported as text is let through by this check, so only a raised one tests the limit.
    runner, limits = _start_taking(SLOW_START, json.dumps({"file": None}) + "\n", raises=True)

    assert run.preflight_agenttalk_runtime(workspace_root=tmp_path, runner=runner) is None
    assert limits == [run.STARTUP_CHECK_TIMEOUT]


def test_the_default_limit_covers_a_cold_start_with_margin_and_stays_bounded() -> None:
    # Measured on Windows (#714): a cold `-m agenttalk --version` took up to 3.7 s here and 5.1 s on a contributor's
    # machine. The limit must clear the slowest at least five times over and still end a check that hangs.
    assert 5 * 5.1 <= run.STARTUP_CHECK_TIMEOUT <= 60


def test_a_launch_check_that_hangs_is_refused_in_bounded_time_with_the_reason(tmp_path) -> None:
    env = {**os.environ, "AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path),
           "PYTHONPATH": str(_hanging_agenttalk(tmp_path)), "PYTHONDONTWRITEBYTECODE": "1"}

    started = time.monotonic()
    result = run.preflight_launch_runtime([sys.executable], "claude", tmp_path, env, timeout=2.0)

    assert time.monotonic() - started < 30
    assert result.blocked is not None
    _plain_timeout_wording(result.blocked, "2")


def test_an_import_check_that_hangs_is_refused_in_bounded_time_with_the_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTTALK_PY", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(_hanging_agenttalk(tmp_path)))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    started = time.monotonic()
    blocked = run.preflight_agenttalk_runtime(workspace_root=tmp_path, timeout=2.0)

    assert time.monotonic() - started < 30
    assert blocked is not None  # before #714 a timed-out import check let the seat through
    _plain_timeout_wording(blocked, "2")


@pytest.mark.parametrize("check", ["launch", "import"])
def test_a_runner_that_times_out_is_refused_with_the_limit_it_was_given(tmp_path, check) -> None:
    def runner(argv, cwd, env, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    if check == "launch":
        env = {"AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path)}
        blocked = run.preflight_launch_runtime([sys.executable], "claude", tmp_path, env, runner=runner).blocked
    else:
        blocked = run.preflight_agenttalk_runtime(workspace_root=tmp_path, runner=runner)

    assert blocked is not None
    _plain_timeout_wording(blocked, f"{run.STARTUP_CHECK_TIMEOUT:g}")


def test_a_missing_agenttalk_is_still_refused(tmp_path) -> None:
    def runner(argv, cwd, env, timeout):
        return 1, "No module named agenttalk\n"

    env = {"AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path)}
    result = run.preflight_launch_runtime([sys.executable], "claude", tmp_path, env, runner=runner)
    assert result.blocked is not None and "No module named agenttalk" in result.blocked
    assert "took longer" not in result.blocked


@pytest.mark.parametrize("check", ["launch", "import"])
def test_a_check_ends_on_time_when_a_process_it_started_keeps_the_output_open(tmp_path, monkeypatch, check) -> None:
    # On Windows, waiting for a check's output used to last until every process holding it had closed it, so a
    # process the check started could hold the check open far past its limit (outside read of #717, finding 2).
    package_root, pid_file = _agenttalk_starting_a_lingering_process(tmp_path)
    monkeypatch.setenv("AGENTTALK_PY", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(package_root))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    try:
        started = time.monotonic()
        if check == "launch":
            env = {**os.environ, "AGENTTALK_ROOT": str(tmp_path)}
            blocked = run.preflight_launch_runtime([sys.executable], "claude", tmp_path, env, timeout=2.0).blocked
        else:
            blocked = run.preflight_agenttalk_runtime(workspace_root=tmp_path, timeout=2.0)
        elapsed = time.monotonic() - started
    finally:
        _stop_lingering(pid_file)
    assert elapsed < 12, f"the check took {elapsed:.1f} s against a 2 s limit"
    assert blocked is not None
    _plain_timeout_wording(blocked, "2")


def _timed_out(argv, cwd, env, timeout):
    raise subprocess.TimeoutExpired(argv, timeout)


def test_a_cadence_turn_refused_by_a_check_that_hangs_says_why(tmp_path, capsys) -> None:
    # A lead-loop controller's cadence turn only returns False: the reason must still reach the wrapper's log and its
    # health, as it does on the message path (outside read of #717, finding 1).
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    spawned: list = []
    drive = run.make_cadence_drive(
        store, "beta", "codex", session.SessionState(cli="codex"), ["codex"],
        spawn=lambda argv, stdin: spawned.append(argv) or [],
        agenttalk_preflight=lambda: run.preflight_agenttalk_runtime(workspace_root=tmp_path, runner=_timed_out),
        clock=lambda: 0.0, render=False,
        health_writer=WrapperHealthWriter(store, "beta", "codex", mode="wrapper-loop", min_interval=0.0),
    )

    assert drive({"agent": "beta"}, [{"type": "dead_letter"}]) is False
    assert spawned == []  # the model is still never started
    err = capsys.readouterr().err
    assert "cadence turn config-blocked" in err, err
    _plain_timeout_wording(err, f"{run.STARTUP_CHECK_TIMEOUT:g}")
    assert (store.read_health_raw("beta") or {}).get("reason_code") == "config_blocked"


def test_the_controller_notice_for_a_failing_cadence_names_the_hung_check(tmp_path, monkeypatch) -> None:
    # The controller's notice after repeated failures used to say only "snapshot/sweep failure".
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    store.set_managed_lead_loop("beta")
    store.set_operator_facing("alpha")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    monkeypatch.setattr(cad, "cadence_due", lambda *a, **k: True)
    monkeypatch.setattr(cad, "build_cadence_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(cad, "cadence_actionable", lambda *a, **k: [{"type": "dead_letter", "key": "dl:x"}])
    monkeypatch.setattr(cad, "snapshot_for_prompt", lambda snap: {"agent": "beta"})
    real_check = run.preflight_agenttalk_runtime
    monkeypatch.setattr(run, "preflight_agenttalk_runtime",
                        lambda **kw: real_check(workspace_root=tmp_path, runner=_timed_out))
    from agenttalk.wrapper import loop as wloop

    def capture_run_loop(store, agent, drive, **kw):
        for _ in range(store_mod.LEAD_LOOP_CADENCE_HEALTH_THRESHOLD):
            result = kw["cadence"]()
            assert result.ran is True and result.ok is False
        return 0

    monkeypatch.setattr(wloop, "run_loop", capture_run_loop)
    cli._wrap_loop_mode(store, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                        sender="beta", min_interval=5.0, render=False, lead_loop=True)
    notices = [m for m in store.valid_messages() if (m.meta or {}).get("cadence_health") == "true"]
    assert len(notices) == 1
    _plain_timeout_wording(notices[0].body, f"{run.STARTUP_CHECK_TIMEOUT:g}")


def test_a_failing_launch_check_still_reports_what_it_printed(tmp_path) -> None:
    # The checks now read their output from temporary files: what the check printed, on both streams, still reaches
    # the refusal.
    package = tmp_path / "noisy" / "agenttalk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\nprint('printed on stdout')\nprint('printed on stderr', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8")
    env = {**os.environ, "AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path),
           "PYTHONPATH": str(package.parent), "PYTHONDONTWRITEBYTECODE": "1"}

    blocked = run.preflight_launch_runtime([sys.executable], "claude", tmp_path, env).blocked

    assert blocked is not None
    assert "printed on stdout" in blocked and "printed on stderr" in blocked, blocked
    assert blocked.index("printed on stdout") < blocked.index("printed on stderr")


def test_an_import_check_still_reads_where_agenttalk_came_from(tmp_path, monkeypatch) -> None:
    # The import check finds the module path in the check's output: an agenttalk from outside the workspace is
    # still refused.
    package = tmp_path / "elsewhere" / "agenttalk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENTTALK_PY", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    blocked = run.preflight_agenttalk_runtime(workspace_root=workspace)

    assert blocked is not None and "out-of-workspace source checkout" in blocked, blocked


class _RefusingTemporaryFiles:
    """Stands in for the tempfile module: the ``failing``-th TemporaryFile() call raises, the others make real files."""

    def __init__(self, failing: int) -> None:
        self.failing, self.calls, self.made = failing, 0, []

    def TemporaryFile(self, *args, **kwargs):  # noqa: N802 - the name the runner calls
        self.calls += 1
        if self.calls == self.failing:
            raise OSError("no temporary folder to write to")
        made = tempfile.TemporaryFile(*args, **kwargs)
        self.made.append(made)
        return made


@pytest.mark.parametrize("failing", [1, 2], ids=["first-file", "second-file"])
def test_a_check_without_temporary_files_still_runs_and_reports(tmp_path, monkeypatch, failing) -> None:
    # Where no temporary file can be made the check falls back to pipes: it must still run the command and report
    # what it returned and printed (final delta read of #717).
    files = _RefusingTemporaryFiles(failing)
    monkeypatch.setattr(run, "tempfile", files)
    code = "import sys; print('printed on stdout'); print('printed on stderr', file=sys.stderr); sys.exit(3)"

    rc, out = run._run_startup_check([sys.executable, "-c", code], str(tmp_path), dict(os.environ), 30.0)

    assert files.calls == failing  # the fallback was taken
    assert rc == 3
    assert out.index("printed on stdout") < out.index("printed on stderr"), out
    assert all(made.closed for made in files.made)  # a file made before the failing one is closed


@pytest.mark.parametrize("failing", [1, 2], ids=["first-file", "second-file"])
def test_a_check_without_temporary_files_still_times_out(tmp_path, monkeypatch, failing) -> None:
    monkeypatch.setattr(run, "tempfile", _RefusingTemporaryFiles(failing))

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run._run_startup_check([sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path),
                               dict(os.environ), 1.0)
    assert time.monotonic() - started < 30
