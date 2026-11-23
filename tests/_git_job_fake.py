"""A fake of `git_job`'s native layer, with a small world of processes and a log of every call, for the #907 tests.

The tests run the product's own logic (`git_job.start_in_job`, `GitJob`, and `cli._git_write` with its time-out paths)
against this fake, on every platform: nothing here calls Windows. `NtOS` makes `cli` believe it runs on Windows.

The world:
- `world.log` is the ordered list of every native call and every call on the launcher (`("terminate", job)`,
  `("kill",)`, `("communicate", timeout)`, `("close", handle)` ...), so tests can check the order of the steps.
- Any call through a handle that was already closed successfully is recorded in `world.violations` (an exception
  would be swallowed by the product's own best-effort handlers, so the test reads the list).
- `world.processes` holds the processes of a history (id, parent id, creation time) and `world.members` the ids that
  are in the job. Only a stop of the job ends members; the fake layer raises `ForbiddenCall` if the product asks for
  anything that would walk process ids or times (`snapshot`, `open_process`, `process_times`).
"""

import subprocess

JOB = 7001  # the job handle the fake hands out
LAUNCHER_HANDLE = 5005  # the handle Popen "holds" for the launcher
LAUNCHER_PID = 100


class ForbiddenCall(AssertionError):
    """The product asked for a process list, an open by id, or a creation time (section 9 of the design)."""


class NtOS:
    """`cli.os` that says it is Windows; every other attribute is the real `os`'s."""

    name = "nt"

    def __getattr__(self, key):
        import os

        return getattr(os, key)


class PosixOS(NtOS):
    """`cli.os` that says it is not Windows."""

    name = "posix"


def raising(*errors):
    """A hook that raises the given errors one per call, in order (None: that call does nothing), and does nothing
    after that."""
    remaining = list(errors)

    def hook(*_ignored):  # the close seam passes the job
        if remaining:
            error = remaining.pop(0)
            if error is not None:
                raise error

    return hook


class FakeClock:
    """A monotonic clock that only moves when the test or `sleep` moves it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProc:
    """The launcher `subprocess.Popen` would return. `script` is what its `communicate` calls do, in order."""

    pid = LAUNCHER_PID
    returncode = None

    def __init__(self, world, script):
        self.world = world
        self._handle = LAUNCHER_HANDLE
        self.script = list(script)
        self.dead = False
        self.raised = []  # the TimeoutExpired objects its communicate and wait raised, in order

    def _note(self, *entry):
        self.world.log.append(entry)

    def poll(self):
        self._note("poll")
        if self.returncode is not None:
            return self.returncode  # it finished on its own
        return 1 if self.dead else None

    def kill(self):
        self._note("kill")
        self.world.fire("kill")
        self.dead = True
        self.world.end(LAUNCHER_PID)

    def communicate(self, timeout=None):
        self._note("communicate", timeout)
        self.world.stamp("communicate", timeout)
        self.world.fire("communicate")
        step = self.script.pop(0) if self.script else ("return", "", "")
        if step[0] == "return":
            self.returncode = step[3] if len(step) > 3 else 0
            return step[1], step[2]
        if step[0] == "timeout":
            self.raised.append(subprocess.TimeoutExpired("git", timeout))
            raise self.raised[-1]
        raise step[1]

    def wait(self, timeout=None):
        self._note("wait", timeout)
        self.world.stamp("wait", timeout)
        self.world.fire("wait")
        if self.world.wait_times_out:
            self.raised.append(subprocess.TimeoutExpired("git", timeout))
            raise self.raised[-1]
        return 0


class FakeWorld:
    """The fake native layer (create_job, assign, resume, terminate, active_processes, close) and its world."""

    def __init__(self, *, clock=None):
        self.log = []
        self.violations = []
        self.stamps = []  # (call, timeout, clock) of every wait on the launcher, when there is a fake clock
        self.forbidden = []  # a walk over ids or times the product asked for (the product's handlers swallow errors)
        self.clock = clock
        self.closed = set()
        self.members = set()
        self.processes = {}  # id -> (parent id, creation time): a history; never read by the product
        self.alive_ids = set()
        self.terminated_jobs = 0
        # behaviour
        self.create_refused = False
        self.create_error = None
        self.assign_ok = True
        self.assign_error = None
        self.resume_status = 0
        self.resume_error = None
        self.terminate_ok = True
        self.terminate_error = None
        self.counts = None  # a list of answers for active_processes (None entries: the query failed); None: real count
        self.query_error = None
        self.close_result = True  # False or an exception to raise (an ordinary failure)
        self.close_interrupt = None  # ("before" | "after", exception): raised before the close, or after it succeeded
        self.wait_times_out = False
        self.native_missing = None  # an OSError raised when the layer is loaded
        self.hooks = {}  # name -> list of callables run at the start of that call
        self.after_hooks = {}  # name -> list of callables run after the operation, before the call returns
        self.proc = None
        self.popen_calls = []
        self.assigned = False

    # --- the world -------------------------------------------------------------------------------------------------
    def stamp(self, name, timeout):
        """Remember what the launcher was asked to wait for, and what the clock said at that moment."""
        if self.clock is not None:
            self.stamps.append((name, timeout, self.clock.now))

    def fire(self, name):
        for hook in list(self.hooks.get(name, ())):
            hook()

    def fire_after(self, name):
        for hook in list(self.after_hooks.get(name, ())):
            hook()

    def end(self, pid):
        self.alive_ids.discard(pid)

    def alive(self, pid):
        return pid in self.alive_ids

    def add_process(self, pid, parent, created, *, member=False):
        self.processes[pid] = (parent, created)
        self.alive_ids.add(pid)
        if member:
            self.members.add(pid)

    def _check(self, name, handle):
        if handle in self.closed:
            self.violations.append((name, handle))
        elif handle != JOB:
            self.violations.append((name, handle, "not our job handle"))

    # --- what the product may call ---------------------------------------------------------------------------------
    def create_job(self):
        self.log.append(("create",))
        self.fire("create")
        if self.create_error is not None:
            raise self.create_error
        return None if self.create_refused else JOB

    def assign(self, job, process):
        self.log.append(("assign", job, process))
        self._check("assign", job)
        self.fire("assign")
        if self.assign_error is not None:
            raise self.assign_error
        if self.assign_ok:
            self.assigned = True
            self.members.add(LAUNCHER_PID)
        self.fire_after("assign")
        return self.assign_ok

    def resume(self, process):
        self.log.append(("resume", process))
        self.fire("resume")
        if self.resume_error is not None:
            raise self.resume_error
        self.fire_after("resume")
        return self.resume_status

    def terminate(self, job, exit_code):
        self.log.append(("terminate", job))
        self._check("terminate", job)
        self.fire("terminate")
        if self.terminate_error is not None:
            raise self.terminate_error
        if self.terminate_ok:
            self.terminated_jobs += 1
            for pid in list(self.members):
                self.end(pid)
        self.fire_after("terminate")
        return self.terminate_ok

    def active_processes(self, job):
        self.log.append(("query", job))
        self._check("query", job)
        self.fire("query")
        if self.query_error is not None:
            raise self.query_error
        if self.counts is not None:
            answer = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
            return answer
        return len([pid for pid in self.members if pid in self.alive_ids])

    def close(self, handle):
        self.log.append(("close", handle))
        self._check("close", handle)
        self.fire("close")
        if self.close_interrupt is not None and self.close_interrupt[0] == "before":
            raise self.close_interrupt[1]
        if isinstance(self.close_result, BaseException):
            raise self.close_result
        if self.close_result:
            self.closed.add(handle)
        if self.close_interrupt is not None and self.close_interrupt[0] == "after" and self.close_result:
            raise self.close_interrupt[1]
        return bool(self.close_result)

    # --- what the product must never call (section 9) --------------------------------------------------------------
    def snapshot(self):
        self.forbidden.append("snapshot")
        raise ForbiddenCall("snapshot")

    def open_process(self, pid):
        self.forbidden.append("open_process")
        raise ForbiddenCall("open_process")

    def process_times(self, handle):
        self.forbidden.append("process_times")
        raise ForbiddenCall("process_times")

    # --- wiring ----------------------------------------------------------------------------------------------------
    def popen(self, script):
        """A `subprocess.Popen` stand-in that returns the launcher and records what it was called with."""

        def fake_popen(cmd, **kwargs):
            self.popen_calls.append((cmd, kwargs))
            self.log.append(("popen", kwargs.get("creationflags", 0)))
            self.proc = FakeProc(self, script)
            self.alive_ids.add(LAUNCHER_PID)
            return self.proc

        return fake_popen

    def install(self, monkeypatch, script=(("timeout",),)):
        """Make `cli` and `git_job` use this world: Windows rules, this native layer, this launcher, this clock."""
        from agenttalk import cli, git_job

        def load():
            if self.native_missing is not None:
                raise self.native_missing
            return self

        monkeypatch.setattr(cli, "os", NtOS())
        monkeypatch.setattr(git_job, "_load_native", load)
        monkeypatch.setattr(subprocess, "Popen", self.popen(script))
        if self.clock is not None:
            monkeypatch.setattr(git_job, "_monotonic", self.clock.monotonic)
            monkeypatch.setattr(git_job, "_sleep", self.clock.sleep)
        return self


def write_args(tmp_path):
    """The argv of one allowlisted mutating Git command, for `cli._git_write`."""
    return ["worktree", "add", "-b", "lane/safe", "--", str(tmp_path / "wt"), "a" * 40]


def run_write(tmp_path, *, timeout=0.01):
    from agenttalk import cli

    return cli._git_write(tmp_path, write_args(tmp_path), timeout=timeout)


def no_job(monkeypatch):
    """The fake layer set to "no job" (job creation refused): the #903 path, for the tests of that path."""
    from agenttalk import git_job

    world = FakeWorld()
    world.create_refused = True
    monkeypatch.setattr(git_job, "_load_native", lambda: world)
    return world
