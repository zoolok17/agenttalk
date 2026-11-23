import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agenttalk import cli


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


class _NtOS:
    name = "nt"

    def __getattr__(self, key):
        return getattr(os, key)


class _TimedOutGit:
    """A Git launcher whose first wait times out; ``reap`` says what the second wait does."""

    pid = 100
    _handle = 1

    def __init__(self, *, reap="ok", exited=False):
        self.count, self.killed, self.waited, self.reap, self.exited = 0, 0, 0, reap, exited

    def poll(self):
        return 0 if self.exited or (self.killed and self.reap == "ok") else None

    def communicate(self, timeout=None):
        self.count += 1
        if self.count == 1:
            raise subprocess.TimeoutExpired("git", timeout)
        if self.count == 2 and self.reap == "timeout":
            raise subprocess.TimeoutExpired("git", timeout)
        if self.count == 2 and self.reap == "interrupt":
            raise KeyboardInterrupt
        return "", ""

    def wait(self, timeout=None):
        self.waited += 1
        return 0

    def kill(self):
        self.killed += 1
        if self.reap == "kill-error":
            raise OSError("simulated kill failure")


def _windows_timeout(tmp_path, monkeypatch, world, git):
    """Run ``_git_write`` through the timeout path as if on Windows, with a fake native layer."""
    from agenttalk import windows_tree_stop as wts

    monkeypatch.setattr(cli, "os", _NtOS())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: git)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("no program may be launched"))
    monkeypatch.setattr(wts, "_default_native", lambda: world)
    return cli._git_write(
        tmp_path, ["worktree", "add", "-b", "lane/safe", "--", str(tmp_path / "wt"), "a" * 40], timeout=0.01
    )


_WARNING = "may still be running"


def _timeout_error(tmp_path, monkeypatch, world, git):
    with pytest.raises(cli.GitWriteError) as error:
        _windows_timeout(tmp_path, monkeypatch, world, git)
    return str(error.value)


def test_confirmed_tree_stop_adds_no_warning_and_does_not_kill_again(tmp_path, monkeypatch):
    from test_windows_tree_stop_791 import World, no_launch

    world = World((101, 100, 1100), (102, 101, 1200))
    git = _TimedOutGit()
    with no_launch() as seen:
        message = _timeout_error(tmp_path, monkeypatch, world, git)
    assert seen == []
    assert _WARNING not in message and "and was killed" in message
    assert git.killed == 0
    assert not any(world.alive(pid) for pid in (100, 101, 102))


@pytest.mark.parametrize("fault", ["snapshot", "open101", "times101", "terminate101", "times100"])
def test_unconfirmed_tree_stop_warns_and_kills_the_launcher(tmp_path, monkeypatch, fault):
    from test_windows_tree_stop_791 import World

    world = World((101, 100, 1100))
    world.fail.add(fault)
    git = _TimedOutGit()
    message = _timeout_error(tmp_path, monkeypatch, world, git)
    assert _WARNING in message and "and was killed" not in message
    assert git.killed == 1


def test_unconfirmed_stop_with_a_failing_kill_still_raises_the_warned_error(tmp_path, monkeypatch):
    from test_windows_tree_stop_791 import World

    world = World((101, 100, 1100))
    world.fail.add("terminate101")
    message = _timeout_error(tmp_path, monkeypatch, world, _TimedOutGit(reap="kill-error"))
    assert _WARNING in message


def test_a_launcher_that_already_ended_is_walked_and_a_confirmed_stop_adds_no_warning(tmp_path, monkeypatch):
    from test_windows_tree_stop_791 import World

    world = World((101, 100, 1100))
    world.procs[100].alive = False
    world.procs[100].listed_when_dead = True
    git = _TimedOutGit(exited=True)
    message = _timeout_error(tmp_path, monkeypatch, world, git)
    assert not world.alive(101)
    assert _WARNING not in message and git.killed == 0


def test_a_reap_that_times_out_after_a_confirmed_stop_keeps_the_warning(tmp_path, monkeypatch):
    from test_windows_tree_stop_791 import World

    world = World((101, 100, 1100))
    git = _TimedOutGit(reap="timeout")
    message = _timeout_error(tmp_path, monkeypatch, world, git)
    assert "could not be reaped" in message and _WARNING in message
    assert git.killed == 0 and git.waited == 1


def test_an_interruption_during_the_stop_kills_and_reaps_the_launcher_and_is_raised_again(tmp_path, monkeypatch):
    from test_windows_tree_stop_791 import World

    world = World((101, 100, 1100))

    def interrupt(w, p, ms):
        raise KeyboardInterrupt

    world.on_wait = interrupt
    git = _TimedOutGit()
    with pytest.raises(KeyboardInterrupt):
        _windows_timeout(tmp_path, monkeypatch, world, git)
    assert git.killed == 1 and git.count == 2  # killed, then reaped
    world.assert_handles_closed()


def test_an_interruption_during_the_reap_kills_and_reaps_the_launcher_and_is_raised_again(tmp_path, monkeypatch):
    from test_windows_tree_stop_791 import World

    world = World((101, 100, 1100))
    git = _TimedOutGit(reap="interrupt")
    with pytest.raises(KeyboardInterrupt):
        _windows_timeout(tmp_path, monkeypatch, world, git)
    assert git.killed == 1 and git.count == 3
    world.assert_handles_closed()


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
@pytest.mark.parametrize("tree_stop", [True, False])
def test_filter_process_tree_stopped_or_warned_before_return(tmp_path, monkeypatch, tree_stop):
    import ctypes
    from ctypes import wintypes

    from agenttalk import windows_tree_stop
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
    from test_windows_tree_stop_791 import arm_launch_observer, disarm_launch_observer

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
            if not tree_stop:
                # the stop is not confirmed: only the launcher is killed, so a child must survive
                patch.setattr(windows_tree_stop, "stop_process_tree", lambda proc: False)
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
        states = [kernel.WaitForSingleObject(handle, 5000 if tree_stop else 0) for handle in handles]
        if tree_stop:
            assert all(state == 0 for state in states), f"a captured Git process is still alive: {states}"
            assert "may still be running" not in str(error.value)
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
        else:
            assert 0x102 in states, "a child must still be alive to exercise the fallback"
            assert "may still be running" in str(error.value)
    finally:
        release.touch()
        for handle in handles:
            kernel.WaitForSingleObject(handle, 15000)
            kernel.CloseHandle(handle)
        if process is not None:
            process.communicate(timeout=15)
