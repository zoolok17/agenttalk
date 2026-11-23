import os
import shlex
import subprocess
import sys
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
    ):
        self.interrupt, self.interrupt_in_wait = interrupt, interrupt_in_wait
        # An ordinary error (not an interruption) raised after the timeout, by the reap, the wait or the kill.
        self.reap_error, self.wait_error, self.kill_error = reap_error, wait_error, kill_error
        self.reap_times_out = reap_times_out or interrupt_in_wait or wait_error is not None
        self.fail = fail or {}
        self.communicates = self.killed = self.waited = 0
        self.cleanup = False

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
            raise subprocess.TimeoutExpired("git", timeout)
        if self.communicates == 2:
            if self.interrupt:
                self.cleanup = True
                raise _FIRST
            if self.reap_error is not None:
                raise self.reap_error
            if self.reap_times_out:
                raise subprocess.TimeoutExpired("git", timeout)
            return "", ""
        self._maybe_fail("communicate")
        return "", ""

    def wait(self, timeout=None):
        self.waited += 1
        if self.interrupt_in_wait and self.waited == 1:
            self.cleanup = True
            raise _FIRST
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
    "launcher",
    [
        _Launcher(reap_error=OSError("simulated pipe failure")),
        _Launcher(wait_error=OSError("simulated wait failure")),
        _Launcher(kill_error=RuntimeError("simulated kill failure")),
    ],
    ids=["reap-oserror", "wait-oserror", "kill-error"],
)
def test_an_ordinary_error_after_the_timeout_becomes_the_warned_timeout_error(tmp_path, monkeypatch, launcher):
    """Once the write has timed out, a failing kill, reap or wait is still the timeout: the error warns that Git or a
    program it started may still be running and keeps the original TimeoutExpired as its cause (the lane assignment's
    timeout recovery is keyed on that), and the launcher is still killed and reaped on a best-effort basis."""
    with pytest.raises(cli.GitWriteError) as error:
        _timeout(tmp_path, monkeypatch, launcher)
    assert _WARNING in str(error.value) and "and was killed" not in str(error.value)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert launcher.communicates >= 2


def test_an_ordinary_error_after_the_timeout_on_another_platform_is_still_the_timeout_error(tmp_path, monkeypatch):
    launcher = _Launcher(reap_error=OSError("simulated pipe failure"))
    with pytest.raises(cli.GitWriteError) as error:
        _timeout(tmp_path, monkeypatch, launcher, windows=False)
    assert "timed out" in str(error.value) and isinstance(error.value.__cause__, subprocess.TimeoutExpired)


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
        states = [kernel.WaitForSingleObject(handle, 0) for handle in handles]
        # The launcher is stopped, the programs Git started are not: a child is still running, and the error says so.
        assert 0x102 in states, "a child must still be alive: the programs Git started are not stopped"
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
