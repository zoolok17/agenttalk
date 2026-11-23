"""#907 on a real Windows with real Git: a time-out stops the programs Git started, a normal end leaves them alone.

Every test here is Windows only and carries the marker ``real_git_job`` (the other tests of the suite run without a
job). The tests look at processes by id, through handles they hold themselves, because a test is allowed to do what the
product never does. Where a test shows that the old code (the launcher only is stopped) fails it, that is said.
"""

import ctypes
import os
import shlex
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest
from test_git_job_capability import _BasicLimits, _ExtendedLimits

from agenttalk import cli, git_job

pytestmark = [
    pytest.mark.real_git_job,
    pytest.mark.skipif(os.name != "nt", reason="Windows job objects"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH"),
]

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
QUERY_LIMITED = 0x1000
STILL_RUNNING = 0x102
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x800


def kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k.IsProcessInJob.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    return k


class Held:
    """Handles to processes a test holds (opened by id, which only a test does); all ended and closed at the end."""

    def __init__(self):
        self.k = kernel()
        self.handles = []

    def open(self, pid):
        handle = self.k.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE | QUERY_LIMITED, False, pid)
        assert handle, f"could not open process {pid} ({ctypes.get_last_error()})"
        self.handles.append(handle)
        return handle

    def alive(self, handle, wait_ms=0):
        return self.k.WaitForSingleObject(handle, wait_ms) == STILL_RUNNING

    def in_job(self, handle, job_handle):
        inside = wintypes.BOOL(0)
        assert self.k.IsProcessInJob(handle, job_handle, ctypes.byref(inside)), ctypes.get_last_error()
        return bool(inside.value)

    def end_all(self):
        for handle in self.handles:
            if self.alive(handle):
                self.k.TerminateProcess(handle, 1)
            self.k.CloseHandle(handle)
        self.handles.clear()


@pytest.fixture
def held():
    holder = Held()
    yield holder
    holder.end_all()


def wait_for(path, seconds, what):
    end = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > end:
            pytest.fail(f"not ready: {what}")
        time.sleep(0.02)


HOLD = (
    "import os, pathlib, sys, time\n"
    "data = sys.stdin.buffer.read() if sys.argv[1:] == ['filter'] else b''\n"
    "ready = pathlib.Path(os.environ['JOB907_READY'])\n"
    "ready.with_suffix('.pid').write_text(str(os.getpid()))\n"
    "ready.touch()\n"
    "time.sleep(120)\n"
    "pathlib.Path(os.environ['JOB907_DONE']).write_text('finished')\n"
    "sys.stdout.buffer.write(data)\n"
)


def sh(*parts):
    return " ".join(shlex.quote(Path(p).as_posix() if isinstance(p, Path) else p) for p in parts)


def run_git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8")


def make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q")
    run_git(root, "config", "user.email", "t@t")
    run_git(root, "config", "user.name", "t")
    (root / "file.txt").write_text("content\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "base")
    return root


def head(root):
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def worktree_args(tmp_path, root):
    return ["worktree", "add", "-b", "lane/tree907", "--", str(tmp_path / "wt"), head(root)]


def timed_write_with_ready_program(tmp_path, monkeypatch, root, held):
    """Run the write with a 0.1 s time-out while the program Git started is known to be alive.

    The wrapped `communicate` first waits (at most 60 s) for the program's ready file and opens a handle on its id, and
    only then calls the real, timed `communicate`, so the readiness limit always runs out before the write's time-out
    starts, and a program that never becomes ready fails the test as "not ready", never as a pass. Returns the error,
    the held handle, the job's live count read just before the product's own close, and the marker's path."""
    ready, done = tmp_path / "ready", tmp_path / "done"
    monkeypatch.setenv("JOB907_READY", str(ready))
    monkeypatch.setenv("JOB907_DONE", str(done))
    seen = {}
    counts = []
    real_popen = subprocess.Popen

    def popen(cmd, *args, **kwargs):
        proc = real_popen(cmd, *args, **kwargs)
        if isinstance(cmd, list) and cmd[:1] == ["git"] and "worktree" in cmd:
            communicate = proc.communicate

            def synced(timeout=None):
                if "handle" not in seen:
                    wait_for(ready, 60, "the program Git started did not write its ready file")
                    seen["handle"] = held.open(int(ready.with_suffix(".pid").read_text()))
                    assert held.alive(seen["handle"]), "the program must be alive before the time-out starts"
                return communicate(timeout=timeout)

            proc.communicate = synced
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(git_job, "_BEFORE_CLOSE", [lambda job: counts.append(job._native.active_processes(job.handle))])
    with pytest.raises(cli.GitWriteError) as error:
        cli._git_write(root, worktree_args(tmp_path, root), timeout=0.1)
    return error.value, seen.get("handle"), counts, done


def test_a_ready_hook_is_stopped_with_git_on_a_timeout(tmp_path, monkeypatch, held):
    """RED on the old code (only the launcher was stopped): the hook outlives the time-out there."""
    root = make_repo(tmp_path)
    program = tmp_path / "hook.py"
    program.write_text(HOLD, encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text(
        "#!/bin/sh\nexec " + sh(Path(sys.executable), program) + "\n", encoding="utf-8"
    )
    run_git(root, "config", "core.hooksPath", hooks.as_posix())

    error, handle, counts, done = timed_write_with_ready_program(tmp_path, monkeypatch, root, held)

    assert handle is not None, "not ready: the hook never ran"
    assert not held.alive(handle, 5000), "the hook was still running 5 s after the time-out"
    assert counts == [0], f"the job's live count before the close must be 0, was {counts}"
    time.sleep(5)  # an explicit window: the marker is only meaningful together with the signalled handle
    assert not done.exists()
    assert git_job_stopped_text() in str(error) and "may still be running" not in str(error)


def test_a_git_filter_is_stopped_with_git_on_a_windows_timeout(tmp_path, monkeypatch, held):
    """RED on the old code: #903's test of the same name pattern showed the filter still running."""
    root = make_repo(tmp_path)
    (root / ".gitattributes").write_text("file.txt filter=slow907\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "attributes")
    program = tmp_path / "filter.py"
    program.write_text(HOLD, encoding="utf-8")
    run_git(root, "config", "filter.slow907.smudge", sh(Path(sys.executable), program, "filter"))
    run_git(root, "config", "filter.slow907.required", "true")

    error, handle, counts, done = timed_write_with_ready_program(tmp_path, monkeypatch, root, held)

    assert handle is not None, "not ready: the filter never ran"
    assert not held.alive(handle, 5000), "the filter was still running 5 s after the time-out"
    assert counts == [0], f"the job's live count before the close must be 0, was {counts}"
    time.sleep(5)
    assert not done.exists()
    assert git_job_stopped_text() in str(error) and "may still be running" not in str(error)


def git_job_stopped_text():
    return cli._GIT_JOB_STOPPED


def test_a_normal_write_leaves_the_monitor_it_started_running(tmp_path, monkeypatch, held):
    """A normal end never stops the job: a file-system monitor Git started keeps running."""
    root = make_repo(tmp_path)
    run_git(root, "config", "core.fsmonitor", "true")
    survivors = []

    def read_members(job):
        # What is still in the job when the write ends normally, read through OUR job's handle.
        k = kernel()
        size = 8 + 8 * 64  # JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORDs, then the ids
        buffer = ctypes.create_string_buffer(size)
        if not k.QueryInformationJobObject(job.handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST, buffer, size, None):
            return
        listed = ctypes.c_uint32.from_buffer(buffer, 4).value
        for index in range(min(listed, 64)):
            pid = ctypes.c_size_t.from_buffer(buffer, 8 + 8 * index).value
            try:
                survivors.append(held.open(pid))
            except AssertionError:
                pass  # it ended meanwhile

    monkeypatch.setattr(git_job, "_BEFORE_CLOSE", [read_members])
    try:
        code, _out, err = cli._git_write(root, worktree_args(tmp_path, root), timeout=60)
        assert code == 0, err
        if not survivors:
            pytest.skip("this Git started no monitor (or any other program) that outlives the write")
        time.sleep(2)  # the close has happened by now: nothing may have stopped them
        assert any(held.alive(handle) for handle in survivors), "a program Git started was stopped by a normal end"
    finally:
        subprocess.run(
            ["git", "-C", str(tmp_path / "wt"), "fsmonitor--daemon", "stop"], capture_output=True, check=False
        )
        subprocess.run(["git", "-C", str(root), "fsmonitor--daemon", "stop"], capture_output=True, check=False)


# --- facts about our job ---------------------------------------------------------------------------------------------


def start_python_in_a_job(tmp_path, code, *argv):
    log = open(tmp_path / "child.err", "wb")  # noqa: SIM115  # closed by the caller's cleanup below
    proc, job = git_job.start_in_job(
        [sys.executable, "-c", code, *argv], stdout=subprocess.DEVNULL, stderr=log, stdin=subprocess.DEVNULL
    )
    assert job is not None, "the host refused the job (see the capability check)"
    return proc, job, log


def finish(proc, job, log):
    job.stop()
    proc.wait(timeout=30)
    job.close()
    log.close()


def test_a_direct_breakaway_request_does_not_take_a_child_out_of_our_job(tmp_path, held):
    """Windows either refuses the request (access denied, error 5) or, on a host whose own outer job allows breakaway,
    lets it through; in both cases the child must not escape our job: a granted one is still a member and ends with
    our job's stop."""
    answer = tmp_path / "answer.txt"
    code = (
        "import subprocess, sys, time\n"
        "try:\n"
        "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        f"creationflags={CREATE_BREAKAWAY_FROM_JOB})\n"
        f"    text = 'allowed ' + str(p.pid)\n"
        "except OSError as e:\n"
        "    text = 'refused ' + str(getattr(e, 'winerror', None))\n"
        f"open({str(answer)!r}, 'w').write(text)\n"
        "time.sleep(120)\n"
    )
    proc, job, log = start_python_in_a_job(tmp_path, code)
    try:
        wait_for(answer, 60, "the child gave no answer (see child.err)")
        time.sleep(0.2)
        verdict, _, detail = answer.read_text().partition(" ")
        if verdict == "refused":
            assert detail == "5", answer.read_text()  # access denied
        else:
            grandchild = held.open(int(detail))
            assert held.in_job(grandchild, job.handle), "a child that asked to break away left our job"
            assert job.stop()
            assert not held.alive(grandchild, 5000), "the child was not ended by our job's stop"
    finally:
        finish(proc, job, log)


def test_a_permissive_inner_job_cannot_take_a_child_out_of_our_job(tmp_path, held):
    ready = tmp_path / "child.pid"
    code = (
        "import ctypes, subprocess, sys, time\n"
        "from ctypes import wintypes\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
        "import test_git_job_capability as m\n"
        "k, _ = m._libraries()\n"
        "k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]\n"
        "k.SetInformationJobObject.restype = wintypes.BOOL\n"
        "inner = k.CreateJobObjectW(None, None)\n"
        "info = m._ExtendedLimits()\n"
        f"info.BasicLimitInformation.LimitFlags = {JOB_OBJECT_LIMIT_BREAKAWAY_OK}\n"
        f"assert k.SetInformationJobObject(inner, {JOB_OBJECT_EXTENDED_LIMIT_INFORMATION}, "
        "ctypes.byref(info), ctypes.sizeof(info))\n"
        "assert k.AssignProcessToJobObject(inner, k.GetCurrentProcess()), ctypes.get_last_error()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        f"creationflags={CREATE_BREAKAWAY_FROM_JOB})\n"
        f"open({str(ready)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    proc, job, log = start_python_in_a_job(tmp_path, code)
    try:
        wait_for(ready, 60, "the inner job's child did not start (see child.err)")
        grandchild = held.open(int(ready.read_text()))
        assert held.in_job(grandchild, job.handle), "a child that broke away from an inner job left our job"
        assert job.stop()
        assert not held.alive(grandchild, 5000), "the child was not ended by our job's stop"
    finally:
        finish(proc, job, log)


def test_our_job_has_no_limits(tmp_path):
    proc, job, log = start_python_in_a_job(tmp_path, "import time; time.sleep(60)")
    try:
        k = kernel()
        limits, returned = _ExtendedLimits(), wintypes.DWORD(0)
        assert k.QueryInformationJobObject(
            job.handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            ctypes.byref(returned),
        )
        assert limits.BasicLimitInformation.LimitFlags == 0  # no kill-on-close, no breakaway, nothing
        assert isinstance(limits.BasicLimitInformation, _BasicLimits)
    finally:
        finish(proc, job, log)


def test_git_is_in_our_job_while_pytest_may_be_in_another(tmp_path, held):
    """The membership asked about is OUR job's own handle: never the host's job (pytest may be inside another one)."""
    ready = tmp_path / "hold"
    program = tmp_path / "hold.py"
    program.write_text(HOLD, encoding="utf-8")
    env = dict(os.environ, JOB907_READY=str(ready), JOB907_DONE=str(tmp_path / "done"))
    command = sh(Path(sys.executable), program)
    proc, job = git_job.start_in_job(
        ["git", "-c", f"alias.hold=!{command}", "hold"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    try:
        assert job is not None
        wait_for(ready, 60, "the alias program did not start")
        assert held.in_job(int(proc._handle), job.handle), "git is not in our job"
        assert held.in_job(held.open(int(ready.with_suffix(".pid").read_text())), job.handle), "its program is not"
    finally:
        if job is not None:
            job.stop()
        proc.wait(timeout=30)
        if job is not None:
            job.close()


def test_a_short_git_command_runs_in_a_job_and_the_job_is_closed_after_it(tmp_path):
    """The plain case: starting a short Git command in a job works and ends with the job closed."""
    proc, job = git_job.start_in_job(
        ["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL
    )
    assert job is not None
    out, _err = proc.communicate(timeout=60)
    assert out.startswith("git version") and job.close() and job.handle is None
