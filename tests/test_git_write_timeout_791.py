import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli


@pytest.mark.skipif(os.name != "nt", reason="Windows tree stop")
@pytest.mark.parametrize(
    "outcome", ["success", "failed", "exited", "missing", "timeout", "denied", "kill-error", "system-lookup"]
)
def test_timeout_tree_stop_is_absolute_and_unconfirmed_stop_warns(tmp_path, monkeypatch, outcome):
    calls = []

    class Git:
        pid = 12345

        def __init__(self):
            self.count = 0
            self.killed = False

        def poll(self):
            return 0 if outcome == "exited" else None

        def communicate(self, timeout=None):
            self.count += 1
            if self.count == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            return "", ""

        def kill(self):
            self.killed = True
            if outcome == "kill-error":
                raise OSError("simulated kill failure")

    proc = Git()
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: proc)
    if outcome == "system-lookup":
        import ctypes

        monkeypatch.setattr(
            ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(GetSystemDirectoryW=lambda *args: 0)
        )

    def taskkill(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if outcome == "missing":
            raise FileNotFoundError("simulated missing taskkill")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return SimpleNamespace(
            returncode=0 if outcome == "success" else 1,
            stderr="ERROR: Access denied." if outcome == "denied" else "",
        )

    monkeypatch.setattr(cli.subprocess, "run", taskkill)
    with pytest.raises(cli.GitWriteError) as error:
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--", str(tmp_path / "wt"), "a" * 40], timeout=0.01
        )
    if outcome in {"exited", "system-lookup"}:
        assert not calls
    else:
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert Path(cmd[0]).is_absolute() and Path(cmd[0]).name.lower() == "taskkill.exe"
        assert cmd[1:] == ["/PID", "12345", "/T", "/F"]
        assert kwargs["timeout"] > 0 and not kwargs.get("shell", False)
    assert proc.killed == (outcome != "success")
    assert ("may still be running" in str(error.value)) == (outcome != "success")


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
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handles = []
    real_popen = subprocess.Popen
    real_run = subprocess.run
    tree_results = []
    process = None

    def capture_tree(proc):
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline, "filter did not start"
            time.sleep(.02)
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
        found = {proc.pid}
        while True:
            expanded = found | {pid for pid, parent in parents.items() if parent in found}
            if expanded == found:
                break
            found = expanded
        assert filter_pid in found and len(found) >= 3, "expected launcher, Git and filter descendants"
        for pid in found:
            handle = kernel.OpenProcess(0x100000, False, pid)
            assert handle, f"cannot open test process {pid}: {ctypes.get_last_error()}"
            handles.append(handle)

    def popen(cmd, *args, **kwargs):
        nonlocal process
        proc = real_popen(cmd, *args, **kwargs)
        if isinstance(cmd, list) and cmd[0] == "git" and "worktree" in cmd:
            process = proc
            communicate = proc.communicate

            def synced(timeout=None):
                if not handles:
                    capture_tree(proc)
                return communicate(timeout=timeout)

            proc.communicate = synced
        return proc

    try:
        with monkeypatch.context() as patch:
            patch.setattr(cli.subprocess, "Popen", popen)
            if not tree_stop:
                patch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
            else:

                def capture_taskkill(*args, **kwargs):
                    kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    result = real_run(*args, **kwargs)
                    tree_results.append((result.returncode, result.stdout, result.stderr))
                    return result

                patch.setattr(cli.subprocess, "run", capture_taskkill)
            with pytest.raises(cli.GitWriteError) as error:
                cli._git_write(
                    root, ["worktree", "add", "-b", "lane/tree791", "--", str(tmp_path / "wt"), base], timeout=0.1
                )
        assert handles, "the filter tree must have been captured"
        states = [kernel.WaitForSingleObject(handle, 5000 if tree_stop else 0) for handle in handles]
        if tree_stop:
            if not os.environ.get("CI") and tree_results and tree_results[0][0] == 1 and tree_results[0][2].strip() in {
                "ERROR: Access denied", "ERROR: Access denied."
            }:
                pytest.skip("taskkill denied: Access denied (sandboxed environment); this test must run outside it")
            assert tree_results and tree_results[0][0] == 0, f"taskkill failed: {tree_results}"
            assert all(state == 0 for state in states), f"a captured Git process is still alive: {tree_results}"
            assert "may still be running" not in str(error.value)
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
