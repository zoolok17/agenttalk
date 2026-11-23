"""#907: a changing Git command on Windows runs in a Windows job of its own, so a time-out stops every program Git
started. These tests run `cli._git_write` and its time-out paths against a fake of the native layer (see
`_git_job_fake.py`), so they run on every platform and nothing here calls Windows. The real Windows and real Git tests
are in `test_git_job_windows.py`.
"""

import subprocess

import pytest
from _git_job_fake import (
    JOB,
    LAUNCHER_PID,
    FakeClock,
    FakeWorld,
    PosixOS,
    raising,
    run_write,
)
from test_git_write_timeout_791 import arm_launch_observer, disarm_launch_observer

from agenttalk import cli, git_job

STOPPED = "Git and the programs in its Windows job were stopped"
WARNING = cli._GIT_STILL_RUNNING
TIMEOUT_THEN_REAP = (("timeout",), ("return", "", ""))


def make(monkeypatch, script=TIMEOUT_THEN_REAP, *, clock=False):
    world = FakeWorld(clock=FakeClock() if clock else None)
    return world.install(monkeypatch, script)


def names(world):
    return [entry[0] for entry in world.log]


def the_timeout_handling(world):
    """The log from the write's own wait on: the time-out handling, without the start."""
    log = names(world)
    return log[log.index("communicate") :]


def timeout_error(tmp_path):
    with pytest.raises(cli.GitWriteError) as error:
        run_write(tmp_path)
    return error.value


# --- the nine histories: an unrelated process is never stopped ------------------------------------------------------
# (id, parent id, creation time) as on the issue. The launcher is 100; 101 is a real child of Git: a job member. Every
# other process is unrelated and is not in the job, so it must stay alive. The product is never allowed to read ids,
# parents or times (the fake raises if it asks), so the history can only matter through the job's own member list.

HISTORIES = {
    "prelaunch_step_inside_window": ([(200, 100, 1015), (201, 200, 1020)], [], 1000, 1030, 30, 1030),
    "reuse_at_genuine_descendant": ([(200, 101, 1015), (201, 200, 1020)], [(101, 100, 1010)], 1000, 1030, 30, 1030),
    "cancelled_steps_during_command": (
        [(200, 101, 1002), (201, 200, 1003)],
        [(101, 100, 1001)],
        1000,
        1030,
        30,
        1030,
    ),
    "below_tolerance_step_during_command": ([(200, 101, 1001.15)], [(101, 100, 1001.05)], 1000, 1029.75, 30, 1029.75),
    "step_after_stop_sample": ([(200, 101, 1002)], [(101, 100, 1001.1)], 1000, 1030, 30, 1003),
    "control_future_timestamp": ([(200, 100, 2000), (201, 200, 2100)], [], 1000, 1030, 30, 1030),
    "control_visible_step_rejected": ([(200, 100, 1015)], [], 1000, 1020, 30, 1020),
    "control_equal_times_stay_rejected": ([(200, 100, 1000)], [], 1000, 1030, 30, 1030),
    "control_forward_only_old_orphan_stays_rejected": ([(200, 100, 990)], [], 1000, 1030, 30, 1040),
}


class NoWallClock:
    """`git_job.time` with only the monotonic clock and sleep: any wall-clock read fails the test."""

    def __init__(self):
        self.reads = []

    def __getattr__(self, name):
        if name in ("monotonic", "sleep"):
            import time

            return getattr(time, name)
        self.reads.append(name)
        raise AssertionError(f"the job code read the clock: time.{name}")


@pytest.mark.parametrize("name", list(HISTORIES))
def test_no_unrelated_process_is_stopped_in_history(monkeypatch, tmp_path, name):
    unrelated, members, _launch, _stop_sample, elapsed, _verify = HISTORIES[name]  # the times are only history
    world = make(monkeypatch, clock=True)
    wall = NoWallClock()
    monkeypatch.setattr(git_job, "time", wall)
    for pid, parent, created in unrelated:
        world.add_process(pid, parent, created)
    for pid, parent, created in members:
        world.add_process(pid, parent, created, member=True)
    once = {"done": False}

    def the_command_runs_for_a_while():  # only the write's own wait: it ends in the time-out, then the cleanup starts
        if not once["done"]:
            once["done"] = True
            world.clock.now += elapsed

    world.hooks["communicate"] = [the_command_runs_for_a_while]

    timeout_error(tmp_path)

    assert all(world.alive(pid) for pid, _p, _c in unrelated), "an unrelated process was stopped"
    assert not world.alive(LAUNCHER_PID)
    assert world.forbidden == [] and wall.reads == []  # no id, no parent, no creation time, no wall clock was read
    assert world.violations == []


@pytest.mark.parametrize("name", [n for n, row in HISTORIES.items() if row[1]])
def test_the_real_children_in_a_history_are_stopped_with_the_job(monkeypatch, tmp_path, name):
    """The same histories, looked at from the other side: Git's real children are members, so they end."""
    unrelated, members, _launch, _stop_sample, _elapsed, _verify = HISTORIES[name]
    world = make(monkeypatch)
    for pid, parent, created in unrelated:
        world.add_process(pid, parent, created)
    for pid, parent, created in members:
        world.add_process(pid, parent, created, member=True)
    assert STOPPED in str(timeout_error(tmp_path))
    assert all(not world.alive(pid) for pid, _p, _c in members), "a member of the job was not stopped"
    assert all(world.alive(pid) for pid, _p, _c in unrelated)
    assert world.closed == {JOB}


@pytest.mark.parametrize(
    "case",
    ["git", "hook_grandchild", "member_whose_parent_exited"],
)
def test_job_members_are_stopped(monkeypatch, tmp_path, case):
    world = make(monkeypatch)
    # a hook's child and grandchild, and a member whose parent has already exited (so it is nobody's descendant now)
    world.add_process(300, 100, 1001, member=True)
    world.add_process(301, 300, 1002, member=True)
    world.add_process(302, 9999, 1003, member=True)
    world.add_process(400, 100, 1004)  # an outsider that is alive during the whole time-out
    error = timeout_error(tmp_path)
    assert STOPPED in str(error)
    watched = {"git": LAUNCHER_PID, "hook_grandchild": 301, "member_whose_parent_exited": 302}[case]
    assert not world.alive(watched)
    assert world.alive(400)
    assert world.forbidden == []


# --- a normal end: the job is closed and never stopped --------------------------------------------------------------


def test_a_normal_write_closes_the_job_and_never_stops_it(monkeypatch, tmp_path):
    world = make(monkeypatch, (("return", "out", "err", 0),))
    assert run_write(tmp_path) == (0, "out", "err")
    assert names(world) == ["create", "popen", "assign", "resume", "communicate", "close"]
    assert world.terminated_jobs == 0 and world.closed == {JOB} and world.violations == []


def test_a_nonzero_git_exit_closes_without_stopping(monkeypatch, tmp_path):
    world = make(monkeypatch, (("return", "", "fatal: nope", 128),))
    assert run_write(tmp_path) == (128, "", "fatal: nope")
    assert "terminate" not in names(world) and "kill" not in names(world)
    assert world.closed == {JOB} and world.violations == []


def test_an_ordinary_communicate_error_closes_without_stopping(monkeypatch, tmp_path):
    error = RuntimeError("simulated pipe failure")
    world = make(monkeypatch, (("raise", error),))
    with pytest.raises(RuntimeError) as raised:
        run_write(tmp_path)
    assert raised.value is error
    assert "terminate" not in names(world) and "query" not in names(world)
    assert "kill" in names(world)  # the launcher is still killed, as before
    assert world.closed == {JOB} and world.violations == []


def test_an_interruption_before_any_timeout_closes_without_stopping(monkeypatch, tmp_path):
    first = KeyboardInterrupt("first")
    world = make(monkeypatch, (("raise", first),))
    with pytest.raises(KeyboardInterrupt) as raised:
        run_write(tmp_path)
    assert raised.value is first
    assert "terminate" not in names(world)
    assert world.closed == {JOB} and world.violations == []


# --- a start that fails, seen from the write ----------------------------------------------------------------------


def test_a_missing_resume_symbol_is_a_start_failure_of_the_write(monkeypatch, tmp_path):
    world = make(monkeypatch)
    world.native_missing = git_job.JobStartError("this Windows has no NtResumeProcess")
    with pytest.raises(cli.GitWriteError, match="mutating git failed to start: .*NtResumeProcess"):
        run_write(tmp_path)
    assert world.popen_calls == []


def test_a_popen_failure_closes_the_job_and_is_a_start_failure_of_the_write(monkeypatch, tmp_path):
    world = make(monkeypatch)

    def failing_popen(cmd, **kwargs):
        world.log.append(("popen",))
        raise FileNotFoundError("no git")

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    with pytest.raises(cli.GitWriteError, match="mutating git failed to start: no git"):
        run_write(tmp_path)
    assert world.closed == {JOB} and "assign" not in names(world)


@pytest.mark.parametrize("status", [1, -1073741790])
def test_a_refused_resume_is_a_start_failure_and_the_paused_launcher_is_stopped(monkeypatch, tmp_path, status):
    world = make(monkeypatch)
    world.resume_status = status
    with pytest.raises(cli.GitWriteError, match="mutating git failed to start: could not resume it"):
        run_write(tmp_path)
    assert "kill" in names(world) and "communicate" in names(world)  # the launcher never ran: it is reaped
    assert "terminate" not in names(world)
    assert world.closed == {JOB} and world.violations == []


# --- the close, in every ending -----------------------------------------------------------------------------------
# pending: what the write is about to report when the close happens. when: the close fails before the native close
# succeeded (the handle is still owned) or after it succeeded (the handle is given up). kind: an ordinary failure, a
# first interruption (nothing was in flight), or a second one (an interruption was already being handled).

PENDING = [
    "normal",
    "timeout",
    "ordinary_error",
    "fallback_error",
    "interrupt_after_timeout",
    "interrupt_before_timeout",
]
SCRIPTS = {
    "normal": (("return", "", "", 0),),
    "timeout": TIMEOUT_THEN_REAP,
    "ordinary_error": (("raise", RuntimeError("simulated pipe failure")),),
    "fallback_error": (("timeout",), ("raise", RuntimeError("simulated pipe failure"))),
}
INTERRUPTING = {"interrupt_after_timeout", "interrupt_before_timeout"}
BASE_STOPS = {
    "normal": 0,
    "timeout": 1,
    "ordinary_error": 0,
    "fallback_error": 2,  # the time-out's stop and the fallback's own
    "interrupt_after_timeout": 2,  # the time-out's stop and the cleanup's own
    "interrupt_before_timeout": 0,
}
CLOSE_CASES = (
    [(p, "before", "ordinary") for p in PENDING]
    + [(p, w, "first") for p in PENDING if p not in INTERRUPTING for w in ("before", "after")]
    + [(p, w, "second") for p in INTERRUPTING for w in ("before", "after")]
)


@pytest.mark.parametrize("pending, when, kind", CLOSE_CASES, ids=["-".join(case) for case in CLOSE_CASES])
def test_the_close(monkeypatch, tmp_path, pending, when, kind):
    pending_interruption = KeyboardInterrupt("pending")
    new_interruption = KeyboardInterrupt("new")
    script = SCRIPTS.get(pending)
    if pending == "interrupt_after_timeout":
        script = (("timeout",), ("raise", pending_interruption))
    elif pending == "interrupt_before_timeout":
        script = (("raise", pending_interruption),)
    world = make(monkeypatch, script)

    # An ordinary failure of the close (an error, never an interruption), or an interruption: before the native close
    # succeeded (raised by the seam just before the call: the handle is still owned) or after it succeeded.
    if kind == "ordinary":
        world.close_result = OSError("simulated close failure")
    elif when == "before":
        monkeypatch.setattr(git_job, "_BEFORE_CLOSE", [raising(new_interruption)])
    else:
        world.close_interrupt = ("after", new_interruption)

    if kind == "ordinary" and pending == "normal":
        assert run_write(tmp_path) == (0, "", "")  # the failed close does not change the result
    elif kind == "ordinary" and pending in ("timeout", "fallback_error"):
        error = timeout_error(tmp_path)
        assert isinstance(error.__cause__, subprocess.TimeoutExpired)  # the pending timeout outcome is kept
    else:
        if kind == "ordinary" and pending == "ordinary_error":
            expected = script[0][1]
        elif kind == "first":
            expected = new_interruption  # nothing was in flight: the new interruption is the one raised
        else:
            expected = pending_interruption  # the first interruption stays the one raised
        with pytest.raises(BaseException) as raised:
            run_write(tmp_path)
        assert raised.value is expected
    stops = names(world).count("terminate")
    further_stop = kind == "first" and when == "before" and pending in ("timeout", "fallback_error")
    assert stops == BASE_STOPS[pending] + (1 if further_stop else 0)
    native_closes = names(world).count("close")
    if kind == "second" and when == "before":
        assert native_closes == 0
    else:
        assert native_closes == 1  # tried once; after a success or a given-up handle nothing tries again
    closed = kind == "first" or (kind == "second" and when == "after")
    assert (world.closed == {JOB}) is closed
    assert world.violations == []  # nothing was ever called through a handle that was already closed


# --- the time-out, job enabled ------------------------------------------------------------------------------------


def test_normal_timeout_stops_the_job_then_kills_reaps_and_counts(monkeypatch, tmp_path):
    world = make(monkeypatch, (("timeout",), ("return", "", "git said no")))
    error = timeout_error(tmp_path)
    assert the_timeout_handling(world) == ["communicate", "terminate", "kill", "communicate", "query", "close"]
    assert world.log[-1] == ("close", JOB)  # the job is closed last, after the count
    assert str(error) == (
        f"mutating git timed out after 0.01s; {STOPPED}: git said no; "
        "a partial checkout or a Git lock may still need recovery"
    )
    assert world.violations == [] and world.forbidden == []


def test_reap_timeout_after_a_job_stop(monkeypatch, tmp_path):
    world = make(monkeypatch, (("timeout",), ("timeout",)))
    error = timeout_error(tmp_path)
    assert the_timeout_handling(world) == ["communicate", "terminate", "kill", "communicate", "wait", "query", "close"]
    assert "could not be reaped" in str(error) and str(error).endswith(f"; {STOPPED}")
    assert WARNING not in str(error)
    assert isinstance(error.__context__, subprocess.TimeoutExpired)


def test_ordinary_error_fallback_stops_the_job_first(monkeypatch, tmp_path):
    """The reap fails with an ordinary error after the time-out: the job is stopped again at the start of the
    fallback, the launcher is cleaned up, the live count is read while the handle is still owned, and the job is closed
    last. (The order is the point: a count after the close would be a read through a closed handle.)"""
    world = make(monkeypatch, (("timeout",), ("raise", RuntimeError("simulated pipe failure"))))
    error = timeout_error(tmp_path)
    assert the_timeout_handling(world) == [
        "communicate",
        "terminate",
        "kill",
        "communicate",
        "terminate",
        "poll",
        "query",
        "close",
    ]
    assert names(world).count("close") == 1 and world.violations == []
    assert "could not be reaped" in str(error) and "simulated pipe failure" in str(error)
    assert str(error).endswith(f"; {STOPPED}")
    assert isinstance(error.__cause__, subprocess.TimeoutExpired)


def test_ordinary_error_fallback_counts_before_it_closes(monkeypatch, tmp_path):
    """Each live count is asked while the handle is open: the log shows no query after the close."""
    world = make(monkeypatch, (("timeout",), ("raise", RuntimeError("simulated pipe failure"))))
    timeout_error(tmp_path)
    log = names(world)
    assert log.index("query") < log.index("close") and "query" not in log[log.index("close") :]


def test_interruption_fallback_stops_the_job(monkeypatch, tmp_path):
    first = KeyboardInterrupt("first")
    world = make(monkeypatch, (("timeout",), ("raise", first)))
    with pytest.raises(KeyboardInterrupt) as raised:
        run_write(tmp_path)
    assert raised.value is first
    assert the_timeout_handling(world) == [
        "communicate",
        "terminate",
        "kill",
        "communicate",
        "terminate",
        "poll",
        "close",
    ]
    assert world.closed == {JOB} and world.violations == []


@pytest.mark.parametrize("where", ["stop", "reap", "count"])
def test_first_interruption_wins_and_a_second_is_absorbed(monkeypatch, tmp_path, where):
    first, second = KeyboardInterrupt("first"), KeyboardInterrupt("second")
    if where == "stop":
        world = make(monkeypatch)
        world.hooks["terminate"] = [raising(first, second)]  # the stop, then the cleanup's own stop
    elif where == "reap":
        world = make(monkeypatch, (("timeout",), ("raise", first), ("raise", second)))
        world.hooks["kill"] = [raising(OSError("the first kill fails"))]  # so the cleanup has to reap again
    else:
        world = make(monkeypatch)
        world.hooks["query"] = [raising(first)]
        world.hooks["terminate"] = [raising(None, second)]  # the time-out's stop works, the cleanup's own is cut
    with pytest.raises(KeyboardInterrupt) as raised:
        run_write(tmp_path)
    assert raised.value is first
    assert world.closed == {JOB} and world.violations == []
    assert names(world)[-1] == "close"  # the cleanup still ran to its last step


def test_an_exhausted_deadline_skips_waits_and_the_count_and_keeps_the_warning(monkeypatch, tmp_path):
    world = make(monkeypatch, (("timeout",), ("timeout",)), clock=True)
    world.hooks["terminate"] = [lambda: setattr(world.clock, "now", world.clock.now + 31)]  # a very slow stop
    error = timeout_error(tmp_path)
    assert "wait" not in names(world) and "query" not in names(world)  # no time left for either
    assert "could not be reaped" in str(error) and WARNING in str(error) and STOPPED not in str(error)
    assert world.closed == {JOB} and names(world)[-1] == "close"
    assert [s[1] for s in world.stamps if s[0] == "communicate"][-1] == 0.0  # the reap got no time at all


def test_no_wait_runs_past_the_deadline(monkeypatch, tmp_path):
    world = make(monkeypatch, (("timeout",), ("timeout",)), clock=True)
    world.wait_times_out = True
    start = {}

    def slow_stop():
        start.setdefault("at", world.clock.now)
        world.clock.now += 10  # the stop itself uses up 10 of the 30 seconds

    def use_the_time():
        timeout = world.stamps[-1][1]
        world.clock.now += timeout  # every wait lasts as long as it was allowed to

    world.hooks["terminate"] = [slow_stop]
    world.hooks["communicate"] = [use_the_time]
    world.hooks["wait"] = [use_the_time]
    timeout_error(tmp_path)
    waits = [s for s in world.stamps if s[0] in ("communicate", "wait")][1:]  # the write's own communicate is first
    deadline_end = start["at"] + 30  # the cleanup deadline began at the first stop
    assert waits, "the cleanup waited at all"
    for name, timeout, at in waits:
        assert at + timeout <= deadline_end + 1e-9, f"{name} was allowed to run past the cleanup deadline"
    assert ("wait", 15.0) in [(s[0], round(s[1], 6)) for s in world.stamps]  # 25 s cut to what was left


@pytest.mark.parametrize("how", ["query_error", "query_none"])
def test_a_failed_query_is_not_zero(monkeypatch, tmp_path, how):
    world = make(monkeypatch)
    if how == "query_error":
        world.query_error = OSError("simulated query failure")
    else:
        world.counts = [None]
    error = timeout_error(tmp_path)
    assert WARNING in str(error) and STOPPED not in str(error)


def test_a_failed_stop_with_zero_count_still_warns(monkeypatch, tmp_path):
    world = make(monkeypatch)
    world.terminate_ok = False  # the stop failed, yet killing the launcher leaves a count of 0
    error = timeout_error(tmp_path)
    assert "kill" in names(world)  # a failed job stop still attempts the launcher kill
    assert WARNING in str(error) and STOPPED not in str(error)


def test_a_count_above_zero_keeps_the_warning(monkeypatch, tmp_path):
    world = make(monkeypatch, clock=True)
    world.counts = [2]  # still two members through the whole window
    error = timeout_error(tmp_path)
    assert WARNING in str(error) and STOPPED not in str(error)
    queries = [e for e in world.log if e[0] == "query"]
    assert len(queries) > 1 and world.clock.now - 1000 <= 2.0 + 0.05  # polled, and stopped at the 2 second window


def test_a_count_that_goes_from_above_zero_to_zero_within_the_deadline_gives_the_success_wording(monkeypatch, tmp_path):
    world = make(monkeypatch, clock=True)
    world.counts = [2, 1, 0]
    error = timeout_error(tmp_path)
    assert STOPPED in str(error) and WARNING not in str(error)
    assert names(world).count("query") == 3


def test_success_wording_keeps_output_and_recovery_advice(monkeypatch, tmp_path):
    make(monkeypatch, (("timeout",), ("return", "some output", "")))
    message = str(timeout_error(tmp_path))
    assert "some output" in message
    assert message.endswith("a partial checkout or a Git lock may still need recovery")


CAUSE_SCRIPTS = {
    "reaped": (("timeout",), ("return", "", "")),
    "reap_timed_out": (("timeout",), ("timeout",)),
    "reap_failed": (("timeout",), ("raise", RuntimeError("simulated pipe failure"))),
}


@pytest.mark.parametrize("path", list(CAUSE_SCRIPTS))
def test_the_cause_is_still_the_writes_timeout(monkeypatch, tmp_path, path):
    """On every path the cause is the write's own TimeoutExpired (the lane assignment's recovery is keyed on it)."""
    world = make(monkeypatch, CAUSE_SCRIPTS[path])
    error = timeout_error(tmp_path)
    assert isinstance(error.__cause__, subprocess.TimeoutExpired)
    assert error.__cause__ is world.proc.raised[0]  # the write's own, not the reap's
    if path == "reap_timed_out":
        assert error.__context__ is world.proc.raised[1]


def test_the_no_job_and_posix_timings_are_unchanged(monkeypatch, tmp_path):
    # Windows, no job (creation refused): the #903 steps and times (5 s reap, 25 s wait), the warning, no job calls.
    world = make(monkeypatch, (("timeout",), ("timeout",)))
    world.create_refused = True
    error = timeout_error(tmp_path)
    assert names(world) == ["create", "popen", "communicate", "kill", "communicate", "wait"]
    assert [e[1] for e in world.log if e[0] in ("communicate", "wait")] == [0.01, 5, 25]
    assert "could not be reaped" in str(error) and WARNING in str(error) and STOPPED not in str(error)
    # not Windows: a plain launch, "and was killed", no job calls and no pause flag
    world = make(monkeypatch)
    monkeypatch.setattr(cli, "os", PosixOS())
    error = timeout_error(tmp_path)
    assert names(world) == ["popen", "communicate", "kill", "communicate"]
    assert world.log[0] == ("popen", 0) and "and was killed" in str(error) and WARNING not in str(error)


def test_the_timeout_path_launches_nothing(monkeypatch, tmp_path):
    """No program is started anywhere in the job-enabled time-out handling (#903's launch observer)."""
    world = make(monkeypatch)
    seen = None
    original = world.popen
    armed = {}

    def watched_popen(script):
        inner = original(script)

        def launch(cmd, **kwargs):
            proc = inner(cmd, **kwargs)
            armed["seen"] = arm_launch_observer()  # armed only after Git itself was started
            return proc

        return launch

    monkeypatch.setattr(subprocess, "Popen", watched_popen(TIMEOUT_THEN_REAP))
    try:
        timeout_error(tmp_path)
        seen = list(armed["seen"])
    finally:
        disarm_launch_observer()
    assert seen == []
    assert len(world.popen_calls) == 1  # the launcher itself, once


# --- fallback rows ------------------------------------------------------------------------------------------------


def _failed_stop(world):
    world.terminate_ok = False


def _failed_resume(world):
    world.resume_status = 1


def _refused_creation(world):
    world.create_refused = True


def _refused_assignment(world):
    world.assign_ok = False


FALLBACK_ROWS = {
    "failed_stop_still_kills": (_failed_stop, "kill", WARNING),
    "refused_creation_keeps_the_warning": (_refused_creation, "communicate", WARNING),
    "refused_assignment_keeps_the_warning": (_refused_assignment, "resume", WARNING),
}


@pytest.mark.parametrize("row", list(FALLBACK_ROWS))
def test_fallback_rows(monkeypatch, tmp_path, row):
    change, must_have, warning = FALLBACK_ROWS[row]
    world = make(monkeypatch)
    change(world)
    error = timeout_error(tmp_path)
    assert must_have in names(world)
    assert warning in str(error) and STOPPED not in str(error)
    if row == "refused_assignment_keeps_the_warning":
        assert "close" in names(world) and "terminate" not in names(world)  # the job was closed at the refusal
        assert names(world).index("close") < names(world).index("resume")
    if row == "refused_creation_keeps_the_warning":
        assert "assign" not in names(world) and "terminate" not in names(world)


def test_a_failed_resume_runs_the_bounded_launcher_cleanup(monkeypatch, tmp_path):
    world = make(monkeypatch)
    _failed_resume(world)
    with pytest.raises(cli.GitWriteError, match="failed to start"):
        run_write(tmp_path)
    log = names(world)
    assert log[log.index("resume") :][:2] == ["resume", "poll"] and "kill" in log and log[-1] == "close"
