"""#714: the wrapper's start-up checks must let a healthy but slow start through (no saved compiled files, so
Python recompiles agenttalk), and must still refuse a check that hangs, in bounded time and in plain words."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agenttalk.wrapper import run

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
