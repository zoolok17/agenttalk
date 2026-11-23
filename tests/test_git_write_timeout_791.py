import ast
import inspect
import os
import shlex
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agenttalk import cli

try:  # Windows only; the test that patches it must still run on Linux and macOS
    import _winapi
except ImportError:  # pragma: no cover - not Windows
    _winapi = None


def _capture_snapshot_tree(root_pid, parents, observe, close):
    """The test's own, independent walk of the tree (the product has its own): strictly later than the parent."""
    root = observe(root_pid)
    assert root is not None
    accepted = {root_pid: root}
    pending = {pid: parent for pid, parent in parents.items() if pid != root_pid}
    while True:
        progressed = False
        for pid, parent in list(pending.items()):
            if parent not in accepted:
                continue
            del pending[pid]
            child = observe(pid)
            if child is None:
                continue
            if child[1] <= accepted[parent][1]:
                close(child[0])
                continue
            accepted[pid] = child
            progressed = True
        if not progressed:
            return accepted


# --- the launch observer: no program may be started while a timed-out Git command is dealt with ---

_LAUNCH_EVENTS = {
    "subprocess.Popen",
    "_winapi.CreateProcess",
    "os.system",
    "os.spawn",
    "os.exec",
    "os.startfile",
    "os.posix_spawn",
}
_LAUNCH_FUNCTIONS = {"CreateProcessW", "CreateProcessA", "ShellExecuteW", "ShellExecuteExW", "WinExec"}
_armed = {"thread": None, "seen": []}


def _launch_observer(event, args):
    if _armed["thread"] != threading.get_ident():
        return
    named = event == "ctypes.dlsym" and any(str(a) in _LAUNCH_FUNCTIONS for a in args)
    if event in _LAUNCH_EVENTS or named:
        _armed["seen"].append(event)
        raise RuntimeError(f"a program was launched in the timeout path: {event}")


sys.addaudithook(_launch_observer)  # audit hooks cannot be removed: this one only acts while armed


def arm_launch_observer():
    """Arm the launch observer for this thread; returns the list that collects what it sees."""
    _armed["seen"] = []
    _armed["thread"] = threading.get_ident()
    return _armed["seen"]


def disarm_launch_observer():
    _armed["thread"] = None


def test_the_launch_observer_sees_every_way_a_program_can_start():
    seen = arm_launch_observer()
    try:
        with pytest.raises(RuntimeError):
            subprocess.run([sys.executable, "-c", "pass"], check=False)
        with pytest.raises(RuntimeError):
            os.system("exit 0")  # noqa: S605  # nosec B605
    finally:
        disarm_launch_observer()
    assert seen == ["subprocess.Popen", "os.system"]


class _NtOS:
    name = "nt"

    def __getattr__(self, key):
        return getattr(os, key)


_WARNING = "may still be running"
_FIRST = KeyboardInterrupt("first")
_REAP_TIMEOUT = subprocess.TimeoutExpired("git", 5)


class _Launcher:
    """A Git launcher whose first wait times out.

    ``interrupt``: the reap in the timeout path (the second communicate) raises ``_FIRST``.
    ``interrupt_in_wait``: the wait() that follows a reap that timed out raises ``_FIRST`` instead.
    ``fail``: step name -> the error that step raises once the cleanup after an interruption has begun.
    """

    pid = 100

    def __init__(
        self,
        *,
        interrupt=False,
        reap_times_out=False,
        interrupt_in_wait=False,
        fail=None,
        reap_error=None,
        wait_error=None,
        kill_error=None,
        fallback=None,
    ):
        self.interrupt, self.interrupt_in_wait = interrupt, interrupt_in_wait
        # An ordinary error (not an interruption) raised after the timeout, by the reap, the wait or the kill.
        self.reap_error, self.wait_error, self.kill_error = reap_error, wait_error, kill_error
        self.reap_times_out = reap_times_out or interrupt_in_wait or wait_error is not None
        self.fail = fail or {}
        self.communicates = self.killed = self.waited = 0
        self.first_timeout = None
        self.cleanup = False
        # (step, error): after the reap failed with an ordinary error, the fallback's first attempt is interrupted
        # at that step ("communicate" or "wait"); the cleanup that follows then works normally.
        self.fallback, self.phase = fallback, "normal"

    def _maybe_fail(self, step):
        error = self.fail.get(step) if self.cleanup else None
        if error is not None:
            raise error

    def poll(self):
        self._maybe_fail("poll")
        return None  # a killed launcher can still answer "running" for a moment, so the cleanup must act

    def kill(self):
        self.killed += 1
        if self.kill_error is not None and self.killed == 1:
            raise self.kill_error
        self._maybe_fail("kill")

    def communicate(self, timeout=None):
        self.communicates += 1
        if self.communicates == 1:
            self.first_timeout = subprocess.TimeoutExpired("git", timeout)
            raise self.first_timeout
        if self.communicates == 2:
            if self.interrupt:
                self.cleanup = True
                raise _FIRST
            if self.reap_error is not None:
                if self.fallback is not None:
                    self.phase = "fallback"
                raise self.reap_error
            if self.reap_times_out:
                raise subprocess.TimeoutExpired("git", timeout)
            return "", ""
        if self.phase == "fallback":
            step, error = self.fallback
            if step == "communicate":
                self.phase = "cleanup"
                raise error
            raise OSError("simulated reap failure")  # the fallback's reap fails, so its wait() runs
        self._maybe_fail("communicate")
        return "", ""

    def wait(self, timeout=None):
        self.waited += 1
        if self.interrupt_in_wait and self.waited == 1:
            self.cleanup = True
            raise _FIRST
        if self.phase == "fallback" and self.fallback[0] == "wait":
            self.phase = "cleanup"
            raise self.fallback[1]
        if self.wait_error is not None and self.waited == 1:
            raise self.wait_error
        self._maybe_fail("wait")
        return 0


def _timeout(tmp_path, monkeypatch, launcher, *, windows=True):
    """Run ``_git_write`` through the timeout path with a fake launcher, on Windows rules unless told otherwise."""
    if windows:
        monkeypatch.setattr(cli, "os", _NtOS())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: launcher)
    return cli._git_write(
        tmp_path, ["worktree", "add", "-b", "lane/safe", "--", str(tmp_path / "wt"), "a" * 40], timeout=0.01
    )


def _timeout_error(tmp_path, monkeypatch, launcher):
    with pytest.raises(cli.GitWriteError) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    return str(error.value)


def test_a_windows_timeout_kills_the_launcher_always_warns_and_launches_nothing(tmp_path, monkeypatch):
    """Children are not stopped, so the error never claims a kill of the whole command and always warns."""
    if _winapi is not None:
        monkeypatch.setattr(_winapi, "CreateProcess", lambda *a, **k: pytest.fail("CreateProcess was called"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("no program may be launched"))
    launcher = _Launcher()
    seen = arm_launch_observer()
    try:
        message = _timeout_error(tmp_path, monkeypatch, launcher)
    finally:
        disarm_launch_observer()
    assert seen == []
    assert _WARNING in message and "and was killed" not in message
    assert launcher.killed == 1 and launcher.communicates == 2


def test_a_windows_timeout_with_a_failing_kill_still_raises_the_warned_error(tmp_path, monkeypatch):
    launcher = _Launcher()

    def failing_kill():
        launcher.killed += 1
        raise OSError("simulated kill failure")

    launcher.kill = failing_kill
    assert _WARNING in _timeout_error(tmp_path, monkeypatch, launcher)
    assert launcher.killed == 1


def test_a_windows_reap_that_times_out_keeps_the_warning_and_waits(tmp_path, monkeypatch):
    launcher = _Launcher(reap_times_out=True)
    message = _timeout_error(tmp_path, monkeypatch, launcher)
    assert "could not be reaped" in message and _WARNING in message
    assert launcher.killed == 1 and launcher.waited == 1


@pytest.mark.parametrize(
    "launcher, kills, reaps, waits",
    [
        # the timeout path's kill and reap, then the fallback's own kill and a second reap that works
        (_Launcher(reap_error=OSError("simulated pipe failure")), 2, 3, 0),
        # the reap timed out, so wait() ran and failed; the fallback kills and reaps again
        (_Launcher(wait_error=OSError("simulated wait failure")), 2, 3, 1),
        # the first kill failed with a non-OSError; the fallback kills again and its reap is the second communicate
        (_Launcher(kill_error=RuntimeError("simulated kill failure")), 2, 2, 0),
    ],
    ids=["reap-oserror", "wait-oserror", "kill-error"],
)
def test_an_ordinary_error_after_the_timeout_becomes_the_warned_timeout_error(
    tmp_path, monkeypatch, launcher, kills, reaps, waits
):
    """Once the write has timed out, a failing kill, reap or wait is still the timeout: the error warns that Git or a
    program it started may still be running and keeps the original TimeoutExpired as its cause (the lane assignment's
    timeout recovery is keyed on that), and the launcher is still killed and reaped on a best-effort basis."""
    with pytest.raises(cli.GitWriteError) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    assert _WARNING in str(error.value) and "and was killed" not in str(error.value)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert (launcher.killed, launcher.communicates, launcher.waited) == (kills, reaps, waits)  # the fallback ran


def test_the_stand_in_launcher_recovers_after_a_single_reap_error():
    """The stand-in itself: without a fallback it fails once and then reaps normally."""
    launcher = _Launcher(reap_error=OSError("one pipe failure"))
    with pytest.raises(subprocess.TimeoutExpired):
        launcher.communicate(timeout=0.01)
    with pytest.raises(OSError):
        launcher.communicate(timeout=5)
    assert launcher.communicate(timeout=5) == ("", "")
    assert launcher.wait(5) == 0


@pytest.mark.parametrize(
    "interruption", [KeyboardInterrupt("fallback"), SystemExit("fallback")], ids=["ctrl-c", "exit"]
)
@pytest.mark.parametrize("step", ["communicate", "wait"])
def test_an_interruption_in_the_fallback_after_an_ordinary_error_wins_over_the_timeout_error(
    tmp_path, monkeypatch, step, interruption
):
    """No interruption is in flight yet when the ordinary error is handled, so a Ctrl-C (or an exit request) that
    arrives while the fallback reaps or waits stops the waiting, gets the bounded best-effort launcher cleanup, and is
    what is raised: not the timeout error."""
    launcher = _Launcher(reap_error=OSError("simulated pipe failure"), fallback=(step, interruption))
    with pytest.raises(BaseException) as raised:
        _timeout(tmp_path, monkeypatch, launcher)
    assert raised.value is interruption and not isinstance(raised.value, cli.GitWriteError)
    # the timeout path's kill, the fallback's kill, and the cleanup's kill; the cleanup reaps the launcher again
    assert launcher.killed == 3 and launcher.communicates == 4
    assert launcher.waited == (1 if step == "wait" else 0)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt("gap"), SystemExit("gap")], ids=["ctrl-c", "exit"])
@pytest.mark.parametrize("reap_fails", [False, True], ids=["reap-works", "reap-fails-so-wait-runs"])
def test_an_interruption_between_two_cleanup_steps_never_replaces_the_first_or_skips_a_step(
    tmp_path, monkeypatch, interruption, reap_fails
):
    """A second Ctrl-C can land after one wrapped step returned and before the next starts, outside any step's own
    guard. While an interruption is being handled that must not escape, and the remaining steps still run."""
    real = cli._best_effort

    def with_a_gap(step):
        real(step)
        raise interruption  # raised in the code between two steps

    monkeypatch.setattr(cli, "_best_effort", with_a_gap)
    launcher = _Launcher(interrupt=True, fail={"communicate": _REAP_TIMEOUT} if reap_fails else None)
    with pytest.raises(KeyboardInterrupt) as raised:
        _timeout(tmp_path, monkeypatch, launcher)
    assert raised.value is _FIRST
    assert launcher.killed == 2 and launcher.communicates == 3  # the kill and the reap still ran
    assert launcher.waited == (1 if reap_fails else 0)


class _CleanupLauncher:
    """A launcher that only counts what the cleanup does to it."""

    killed = reaped = 0

    def poll(self):
        return None

    def kill(self):
        self.killed += 1

    def communicate(self, timeout=None):
        self.reaped += 1
        return "", ""

    def wait(self, timeout=None):
        return 0


def _lines_of_the_guarded_region():
    """(line, n): the n-th time the normal path reaches a statement inside the try of the swallow-all loop (a
    `return` is left out: Python does not run its last instruction inside the try)."""
    lines, start = inspect.getsourcelines(cli._run_swallowing)
    function = ast.parse(textwrap.dedent("".join(lines))).body[0]
    guarded = next(node for node in ast.walk(function) if isinstance(node, ast.Try)).body
    wanted = {
        node.lineno + start - 1
        for statement in guarded
        for node in ast.walk(statement)
        if isinstance(node, ast.stmt) and not isinstance(node, ast.Return)  # a `return` is not covered by the try
    }
    seen, code = {}, cli._run_swallowing.__code__

    def watch(frame, event, arg):
        if frame.f_code is code:
            return watch_lines
        return None

    def watch_lines(frame, event, arg):
        if event == "line":
            seen[frame.f_lineno] = seen.get(frame.f_lineno, 0) + 1
        return watch_lines

    prior = sys.gettrace()
    sys.settrace(watch)
    try:
        cli._kill_and_reap(_CleanupLauncher())
    finally:
        sys.settrace(prior)
    return [(line, n) for line in sorted(wanted & set(seen)) for n in range(1, seen[line] + 1)]


_GUARDED_LINES = _lines_of_the_guarded_region()


def test_the_guarded_region_of_the_cleanup_loop_has_lines_to_probe():
    assert len(_GUARDED_LINES) >= 4, _GUARDED_LINES  # the loop control, the step runner, and the return


@pytest.mark.parametrize("line, n", _GUARDED_LINES)
def test_a_second_exception_at_any_guarded_line_of_the_cleanup_loop_keeps_the_first_and_skips_no_step(line, n):
    """The loop control sits inside the guarded region: an exception that code raises on any of its lines, on any
    pass through it (the loop condition, right after a step was chosen, at the call), never escapes to replace the
    interruption being handled, and no step is skipped."""
    proc = _CleanupLauncher()
    code, fired, hits = cli._run_swallowing.__code__, [], []

    def tracer(frame, event, arg):
        return line_tracer if frame.f_code is code else None

    def line_tracer(frame, event, arg):
        if event == "line" and frame.f_lineno == line:
            hits.append(line)
            if len(hits) == n:
                fired.append(line)
                raise SystemExit("second")
        return line_tracer

    prior = sys.gettrace()
    try:
        with pytest.raises(KeyboardInterrupt, match="first"):
            try:
                raise KeyboardInterrupt("first")
            except KeyboardInterrupt:
                sys.settrace(tracer)
                cli._kill_and_reap(proc)
                raise
    finally:
        sys.settrace(prior)
    assert fired == [line]
    assert proc.killed == 1 and proc.reaped == 1


def test_the_previous_sigint_handler_is_restored_after_a_partial_install_failure(monkeypatch):
    """The previous handler is read before anything is installed, and put back in a finally even when the install
    itself failed after it had changed the handler."""
    import signal

    previous = object()
    state = {"handler": previous, "calls": 0}

    def install(signum, handler):
        state["calls"] += 1
        old = state["handler"]
        state["handler"] = handler
        if state["calls"] == 1:
            raise OSError("simulated failure after the handler was installed")
        return old

    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(signal, "getsignal", lambda signum: state["handler"])
    with cli._sigint_deferred():
        pass
    assert state["handler"] is previous


@pytest.mark.parametrize("partial", [False, True], ids=["install-fails", "install-fails-after-taking-effect"])
@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt("install"), SystemExit("install"), OSError("install"), ValueError("install")],
    ids=["ctrl-c", "exit", "oserror", "valueerror"],
)
def test_an_install_that_raises_or_is_interrupted_goes_on_to_the_cleanup_without_deferral(
    tmp_path, monkeypatch, failure, partial
):
    """If installing the temporary handler raises, or is itself interrupted, the cleanup still runs (without the
    deferral), the original interruption is what is raised, and the previous handler is back."""
    import signal

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers can only be changed from the main thread")
    before = signal.getsignal(signal.SIGINT)
    real, calls = signal.signal, []

    def install(signum, handler):
        calls.append(handler)
        if len(calls) == 1:
            if partial:
                real(signum, handler)  # the handler took effect, and then the call still failed
            raise failure
        return real(signum, handler)

    monkeypatch.setattr(signal, "signal", install)
    launcher = _Launcher(interrupt=True)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _timeout(tmp_path, monkeypatch, launcher)
    finally:
        monkeypatch.setattr(signal, "signal", real)
        restored = signal.getsignal(signal.SIGINT)
        real(signal.SIGINT, before)
    assert raised.value is _FIRST
    assert launcher.killed == 2 and launcher.communicates == 3  # the cleanup ran, undeferred
    assert restored is before


@pytest.mark.parametrize("reap_fails", [False, True], ids=["reap-works", "reap-fails-so-wait-runs"])
@pytest.mark.parametrize("line, n", _GUARDED_LINES)
def test_the_cleanup_counts_hold_at_every_guarded_line_through_the_whole_timeout_path(
    tmp_path, monkeypatch, line, n, reap_fails
):
    """The same injection as above, but through the whole timeout path and its stand-in launcher: wherever a second
    exception lands in the loop, the kill (2 with the one of the timeout path), the reap (3) and the wait (1 when the
    reap fails, else 0) are all done; a step may repeat, never be skipped, so none comes out lower."""
    launcher = _Launcher(interrupt=True, fail={"communicate": _REAP_TIMEOUT} if reap_fails else None)
    code, hits, fired = cli._run_swallowing.__code__, [], []

    def tracer(frame, event, arg):
        return line_tracer if frame.f_code is code else None

    def line_tracer(frame, event, arg):
        if event == "line" and frame.f_lineno == line:
            hits.append(line)
            if len(hits) == n:
                fired.append(line)
                raise SystemExit("second")
        return line_tracer

    prior = sys.gettrace()
    try:
        sys.settrace(tracer)
        with pytest.raises(KeyboardInterrupt) as raised:
            _timeout(tmp_path, monkeypatch, launcher)
    finally:
        sys.settrace(prior)
    assert raised.value is _FIRST and fired == [line]
    assert (launcher.killed, launcher.communicates, launcher.waited) == (2, 3, 1 if reap_fails else 0)


def test_a_failed_install_that_changed_nothing_is_not_followed_by_a_restore(monkeypatch):
    """Off the main thread the install is refused outright: nothing was changed, so nothing is put back."""
    import signal

    calls = []

    def refuse(signum, handler):
        calls.append(handler)
        raise ValueError("signal only works in main thread of the main interpreter")

    monkeypatch.setattr(signal, "signal", refuse)
    with cli._sigint_deferred():
        pass
    assert len(calls) == 1


def test_a_real_ctrl_c_during_the_interruption_cleanup_is_deferred_and_the_handler_restored(tmp_path, monkeypatch):
    """A SIGINT delivered while the cleanup runs does not interrupt it (a temporary handler takes it), and the
    previous handler is back afterwards."""
    import signal

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers can only be changed from the main thread")
    before = signal.getsignal(signal.SIGINT)
    launcher = _Launcher(interrupt=True)
    seen = {}

    def kill_with_a_ctrl_c():
        launcher.killed += 1
        if launcher.cleanup and "handler" not in seen:  # the cleanup's own kill
            seen["handler"] = signal.getsignal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
            seen["finished"] = True  # the kill was not interrupted by the Ctrl-C

    launcher.kill = kill_with_a_ctrl_c
    with pytest.raises(KeyboardInterrupt) as raised:
        _timeout(tmp_path, monkeypatch, launcher)
    assert raised.value is _FIRST
    assert seen.get("finished") and seen["handler"] is not before
    assert signal.getsignal(signal.SIGINT) is before
    assert launcher.communicates == 3  # the reap after the Ctrl-C still ran


def test_an_ordinary_error_after_the_timeout_on_another_platform_is_still_the_timeout_error(tmp_path, monkeypatch):
    launcher = _Launcher(reap_error=OSError("simulated pipe failure"))
    with pytest.raises(cli.GitWriteError) as error:
        _timeout(tmp_path, monkeypatch, launcher, windows=False)
    assert "timed out" in str(error.value) and isinstance(error.value.__cause__, subprocess.TimeoutExpired)


def test_a_reap_that_times_out_keeps_the_original_write_timeout_as_the_cause(tmp_path, monkeypatch):
    """The cause is the timeout of the write itself, not the later timeout of the reap, which stays as context."""
    launcher = _Launcher(reap_times_out=True)
    with pytest.raises(cli.GitWriteError) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    assert error.value.__cause__ is launcher.first_timeout
    assert isinstance(error.value.__context__, subprocess.TimeoutExpired)
    assert error.value.__context__ is not launcher.first_timeout
    assert "could not be reaped" in str(error.value) and _WARNING in str(error.value)


def test_an_interruption_during_the_reap_kills_and_reaps_the_launcher_and_is_raised_again(tmp_path, monkeypatch):
    launcher = _Launcher(interrupt=True)
    with pytest.raises(KeyboardInterrupt) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    assert error.value is _FIRST
    assert launcher.killed == 2 and launcher.communicates == 3  # the timeout path's kill, the cleanup's, the reap


def test_an_interruption_in_the_last_step_of_the_timeout_handling_is_cleaned_up_and_raised_again(tmp_path, monkeypatch):
    """The whole timeout handling sits inside the interruption boundary, its last step (the wait) included."""
    launcher = _Launcher(interrupt_in_wait=True)
    with pytest.raises(KeyboardInterrupt) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    assert error.value is _FIRST
    assert launcher.killed == 2 and launcher.communicates == 3  # the cleanup kills and reaps the launcher again


@pytest.mark.parametrize(
    "fail",
    [
        {"poll": OSError("simulated poll failure")},
        {"kill": OSError("simulated kill failure")},
        {"communicate": OSError("simulated reap failure")},
        {"communicate": _REAP_TIMEOUT},
        {"communicate": _REAP_TIMEOUT, "wait": OSError("simulated wait failure")},
    ],
    ids=["poll", "kill", "communicate", "communicate-timeout", "wait"],
)
def test_each_launcher_cleanup_step_is_best_effort_and_the_interruption_is_raised(tmp_path, monkeypatch, fail):
    """A failing poll never skips the kill, a failing kill never skips the reap, a failed or timed-out reap is
    followed by wait(); each step stands alone and the original interruption is what is raised."""
    launcher = _Launcher(interrupt=True, fail=fail)
    with pytest.raises(KeyboardInterrupt) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    assert error.value is _FIRST
    assert launcher.killed == 2, "a failing poll must not skip the kill"
    assert launcher.communicates == 3, "a failing kill must not skip the reap"
    if "communicate" in fail:
        assert launcher.waited == 1, "a failed or timed-out reap is followed by wait()"


@pytest.mark.parametrize("second", [KeyboardInterrupt("second"), SystemExit("second")], ids=["ctrl-c", "exit"])
@pytest.mark.parametrize("step", ["poll", "kill", "communicate", "wait"])
def test_a_second_interruption_while_one_is_being_handled_never_replaces_the_first(tmp_path, monkeypatch, step, second):
    """The original interruption is kept through the whole best-effort cleanup, and the steps after the one that was
    interrupted still run."""
    fail = {step: second}
    if step == "wait":
        fail["communicate"] = _REAP_TIMEOUT  # the reap fails, so wait() runs, and the second interruption lands there
    launcher = _Launcher(interrupt=True, fail=fail)
    with pytest.raises(KeyboardInterrupt) as raised:
        _timeout(tmp_path, monkeypatch, launcher)
    assert raised.value is _FIRST
    assert launcher.killed == 2
    if step in ("poll", "kill"):
        assert launcher.communicates == 3, "the reap still ran after the interrupted step"
    if step == "communicate":
        assert launcher.waited == 1, "the wait still ran after the interrupted reap"


def test_posix_timeout_keeps_direct_kill_and_original_message(tmp_path, monkeypatch):
    class PosixOS:
        name = "posix"

        def __getattr__(self, key):
            return getattr(os, key)

    class Git:
        killed = False
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            return "", ""

        def kill(self):
            self.killed = True

    proc = Git()
    monkeypatch.setattr(cli, "os", PosixOS())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: pytest.fail("POSIX must not call taskkill"))
    with pytest.raises(cli.GitWriteError) as error:
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--", str(tmp_path / "wt"), "a" * 40], timeout=0.01
        )
    assert proc.killed
    assert "and was killed" in str(error.value)
    assert "may still be running" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows process tree")
def test_a_git_filter_keeps_running_after_a_windows_timeout_and_the_error_warns(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    from agenttalk.supervisor_lifecycle import _PROCESSENTRY32W
    from test_lanes import _commit, _git, _repo

    root, _ = _repo(tmp_path)
    _commit(root, "file.txt", "content\n")
    base = _commit(root, ".gitattributes", "file.txt filter=slow791\n")
    ready, release = tmp_path / "ready", tmp_path / "release"
    script = tmp_path / "filter.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "data = sys.stdin.buffer.read()\n"
        "ready = pathlib.Path(os.environ['FILTER_READY_791'])\n"
        "ready.with_suffix('.pid').write_text(str(os.getpid()))\n"
        "ready.touch()\n"
        "deadline = time.monotonic() + 45\n"
        "while not pathlib.Path(os.environ['FILTER_RELEASE_791']).exists():\n"
        "    if time.monotonic() > deadline: raise SystemExit(2)\n"
        "    time.sleep(.02)\n"
        "sys.stdout.buffer.write(data)\n",
        encoding="utf-8",
    )
    command = shlex.quote(Path(sys.executable).as_posix()) + " " + shlex.quote(script.as_posix())
    _git(root, "config", "filter.slow791.smudge", command)
    _git(root, "config", "filter.slow791.required", "true")
    monkeypatch.setenv("FILTER_READY_791", str(ready))
    monkeypatch.setenv("FILTER_RELEASE_791", str(release))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handles = []
    held = {}  # "launcher" and "filter": the handles of those two processes
    real_popen = subprocess.Popen
    process = None
    fired = []
    launched = []

    def capture_tree(proc):
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline, "filter did not start"
            time.sleep(0.02)
        filter_pid = int(ready.with_suffix(".pid").read_text())
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        assert snapshot != wintypes.HANDLE(-1).value
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            parents = {}
            ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)

        def observe(pid):
            handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
            if not handle:
                return None
            creation, exit_time, user, system = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(system), ctypes.byref(user)
            ):
                kernel.CloseHandle(handle)
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return handle, ticks

        assert proc.poll() is None, "launcher exited before process capture"
        found = _capture_snapshot_tree(proc.pid, parents, observe, kernel.CloseHandle)
        handles.extend(handle for handle, _ in found.values())
        held["launcher"], held["filter"] = found[proc.pid][0], found[filter_pid][0]
        assert filter_pid in found and len(found) >= 3, "expected launcher, Git and filter descendants"

    def popen(cmd, *args, **kwargs):
        nonlocal process
        proc = real_popen(cmd, *args, **kwargs)
        if isinstance(cmd, list) and cmd[0] == "git" and "worktree" in cmd:
            process = proc
            communicate = proc.communicate

            def synced(timeout=None):
                if not handles:
                    capture_tree(proc)
                try:
                    return communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    if not fired:
                        fired.append(int((time.time() + 11644473600) * 10_000_000))  # as Windows file time
                        launched.append(arm_launch_observer())  # from here on, only the product runs
                    raise

            proc.communicate = synced
        return proc

    try:
        with monkeypatch.context() as patch:
            patch.setattr(cli.subprocess, "Popen", popen)
            try:
                with pytest.raises(cli.GitWriteError) as error:
                    cli._git_write(
                        root, ["worktree", "add", "-b", "lane/tree791", "--", str(tmp_path / "wt"), base], timeout=0.1
                    )
            finally:
                disarm_launch_observer()
        assert handles, "the filter tree must have been captured"
        assert fired, "the timeout must have fired"
        assert launched == [[]], "no program may be launched in the timeout path"
        # The launcher is stopped (checked on its own handle); the programs Git started are not: the content filter's
        # own process, held by its own handle, is still running, and the error says so.
        assert kernel.WaitForSingleObject(held["launcher"], 5000) == 0, "the launcher must have been stopped"
        assert kernel.WaitForSingleObject(held["filter"], 0) == 0x102, "the content filter must still be running"
        assert "may still be running" in str(error.value) and "and was killed" not in str(error.value)
        # Whatever the audit hook cannot see: no process created after the timeout fired is a child of ours.
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ours = []
            ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if int(entry.th32ParentProcessID) == os.getpid():
                    ours.append(int(entry.th32ProcessID))
                ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        for pid in ours:
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                continue
            try:
                creation, exit_time, user, system = (wintypes.FILETIME() for _ in range(4))
                if kernel.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(system),
                    ctypes.byref(user),
                ):
                    created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                    assert created < fired[0], f"process {pid} was started after the timeout fired"
            finally:
                kernel.CloseHandle(handle)
    finally:
        release.touch()
        for handle in handles:
            kernel.WaitForSingleObject(handle, 15000)
            kernel.CloseHandle(handle)
        if process is not None:
            process.communicate(timeout=15)
