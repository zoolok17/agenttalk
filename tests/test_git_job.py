"""#907: `agenttalk.git_job` on a fake native layer: who owns what from the job's creation to the hand-over, the handle
rule, the count, and the cleanup deadline. Nothing here calls Windows, so it runs on every platform."""

import inspect
import subprocess
import time

import pytest
from _git_job_fake import JOB, LAUNCHER_HANDLE, FakeClock, FakeWorld, raising

from agenttalk import cli, git_job

CMD = ["git", "-C", "repo", "worktree", "add"]
KW = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}


def started(world, monkeypatch, **options):
    world.install(monkeypatch, script=[])
    return git_job.start_in_job(CMD, **KW, **options)


def names(world):
    return [entry[0] for entry in world.log]


# --- step 0 and step 1 ------------------------------------------------------------------------------------------


def test_missing_resume_symbol_is_a_start_failure_before_any_launch(monkeypatch):
    world = FakeWorld()
    world.native_missing = git_job.JobStartError("this Windows has no NtResumeProcess to let a paused Git run")
    world.install(monkeypatch)
    with pytest.raises(git_job.JobStartError, match="no NtResumeProcess") as raised:
        git_job.start_in_job(CMD, **KW)
    assert isinstance(raised.value, OSError)  # `_git_write` turns an OSError into "mutating git failed to start"
    assert world.log == [] and world.popen_calls == []  # nothing was created, nothing was started


@pytest.mark.parametrize("how", ["refused", "error"])
def test_a_job_that_cannot_be_made_runs_git_as_before_with_no_job(monkeypatch, how):
    world = FakeWorld()
    if how == "refused":
        world.create_refused = True
    else:
        world.create_error = OSError("no job here")
    proc, job = started(world, monkeypatch)
    assert job is None and proc is world.proc
    assert world.popen_calls[0][1].get("creationflags", 0) == 0  # not started paused: exactly as without a job
    assert names(world) == ["create", "popen"]


def test_an_interruption_while_the_job_is_made_is_raised_and_nothing_is_cleaned(monkeypatch):
    world = FakeWorld()
    world.install(monkeypatch)
    first = KeyboardInterrupt("first")
    world.hooks["create"] = [lambda: (_ for _ in ()).throw(first)]
    with pytest.raises(KeyboardInterrupt) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is first and world.popen_calls == [] and world.violations == []


# --- step 2: the paused launch ------------------------------------------------------------------------------------


def test_the_launcher_is_started_paused_and_resumed_only_after_the_assignment(monkeypatch):
    world = FakeWorld()
    proc, job = started(world, monkeypatch)
    assert job is not None and job.handle == JOB
    assert world.popen_calls[0][1]["creationflags"] & 0x4  # CREATE_SUSPENDED
    # no child can start outside the job: the assignment comes before the resume, and both use Popen's own handle
    assert world.log == [("create",), ("popen", 4), ("assign", JOB, LAUNCHER_HANDLE), ("resume", LAUNCHER_HANDLE)]


def test_other_creation_flags_are_kept_when_the_launcher_is_started_paused(monkeypatch):
    world = FakeWorld()
    world.install(monkeypatch, script=[])
    git_job.start_in_job(CMD, creationflags=0x08000000, **KW)  # CREATE_NO_WINDOW stays
    assert world.popen_calls[0][1]["creationflags"] == 0x08000000 | 0x4


@pytest.mark.parametrize("error", [OSError("no git"), KeyboardInterrupt("popen"), SystemExit("popen")])
def test_popen_failure_after_the_job_exists_closes_the_job(monkeypatch, error):
    world = FakeWorld()
    world.install(monkeypatch)

    def failing_popen(cmd, **kwargs):
        world.log.append(("popen", kwargs.get("creationflags", 0)))
        raise error

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    with pytest.raises(type(error)) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is error  # the original error, whatever else happened
    assert names(world) == ["create", "popen", "close"] and world.closed == {JOB} and world.violations == []


def test_a_second_interruption_while_the_job_is_closed_after_a_failed_popen_is_absorbed(monkeypatch):
    world = FakeWorld()
    world.install(monkeypatch)
    first = OSError("no git")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (_ for _ in ()).throw(first))
    world.close_interrupt = ("before", KeyboardInterrupt("second"))
    with pytest.raises(OSError) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is first


# --- step 3: the assignment -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["refused", "error"])
def test_a_refused_assignment_closes_the_job_resumes_the_launcher_and_goes_on_with_no_job(monkeypatch, how):
    world = FakeWorld()
    if how == "refused":
        world.assign_ok = False
    else:
        world.assign_error = OSError("assign broke")
    proc, job = started(world, monkeypatch)
    assert job is None and proc is world.proc
    assert names(world) == ["create", "popen", "assign", "close", "resume"]  # resumed even though no job is used
    assert world.closed == {JOB} and not proc.dead and world.violations == []


@pytest.mark.parametrize("when", ["before", "after"])
def test_failure_or_interrupt_just_before_and_after_assignment(monkeypatch, when):
    world = FakeWorld()
    first = KeyboardInterrupt(f"{when} the assignment")
    raiser = raising(first)
    (world.hooks if when == "before" else world.after_hooks)["assign"] = [raiser]
    world.install(monkeypatch, script=[])
    with pytest.raises(KeyboardInterrupt) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is first
    assert world.proc.dead and world.closed == {JOB}  # the launcher is stopped, the job closed, nothing ran
    assert "resume" not in names(world) and world.violations == []


# --- step 4: the resume ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, text",
    [(1, "0x00000001"), (-1073741790, "0xc0000022"), (-2147483648, "0x80000000")],
    ids=["positive", "access-denied", "sign-bit"],
)
def test_nonzero_status_is_a_resume_failure(monkeypatch, status, text):
    """An NTSTATUS, read as a signed 32-bit value: only exactly 0 is success."""
    world = FakeWorld()
    world.resume_status = status
    world.install(monkeypatch, script=[])
    with pytest.raises(git_job.JobStartError, match=r"could not resume it \(status ") as raised:
        git_job.start_in_job(CMD, **KW)
    assert text in str(raised.value).lower()
    assert world.proc.dead and world.closed == {JOB}  # Git never ran: the launcher is stopped, the job closed
    assert names(world).count("terminate") == 1  # the job is stopped too: harmless, nothing ran in it


def test_a_failing_resume_call_is_a_start_failure_too(monkeypatch):
    world = FakeWorld()
    world.resume_error = OSError("NtResumeProcess broke")
    world.install(monkeypatch, script=[])
    with pytest.raises(git_job.JobStartError, match="could not resume it .NtResumeProcess broke"):
        git_job.start_in_job(CMD, **KW)
    assert world.proc.dead and world.closed == {JOB}


@pytest.mark.parametrize("when", ["before", "after"])
def test_failure_or_interrupt_just_before_and_after_resume(monkeypatch, when):
    world = FakeWorld()
    first = KeyboardInterrupt(f"{when} the resume")
    raiser = raising(first)
    (world.hooks if when == "before" else world.after_hooks)["resume"] = [raiser]
    world.install(monkeypatch, script=[])
    with pytest.raises(KeyboardInterrupt) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is first
    assert world.proc.dead and world.closed == {JOB} and world.violations == []


def test_interrupt_at_the_hand_over(monkeypatch):
    """Right after the resume returned, before `start_in_job` returns: Git already runs in the job, so its cleanup
    stops the job (not only the launcher) and closes it."""
    world = FakeWorld()
    first = SystemExit("at the hand-over")
    world.after_hooks["resume"] = [lambda: (_ for _ in ()).throw(first)]
    world.install(monkeypatch, script=[])
    with pytest.raises(SystemExit) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is first and world.proc.dead and world.closed == {JOB}
    assert world.terminated_jobs == 1 and names(world).index("terminate") < names(world).index("close")


def test_a_second_interruption_during_the_setup_cleanup_is_absorbed_and_the_first_is_raised(monkeypatch):
    world = FakeWorld()
    first = KeyboardInterrupt("first")
    world.hooks["resume"] = [lambda: (_ for _ in ()).throw(first)]
    world.close_interrupt = ("before", KeyboardInterrupt("second"))
    world.install(monkeypatch, script=[])
    with pytest.raises(KeyboardInterrupt) as raised:
        git_job.start_in_job(CMD, **KW)
    assert raised.value is first and world.proc.dead


def test_the_setup_cleanup_given_by_the_caller_is_the_one_used(monkeypatch):
    world = FakeWorld()
    world.resume_status = 5
    world.install(monkeypatch, script=[])
    seen = []
    with pytest.raises(git_job.JobStartError):
        git_job.start_in_job(CMD, reap=lambda proc, job: seen.append((proc, job)), **KW)
    assert len(seen) == 1 and seen[0][0] is world.proc and seen[0][1] is not None


# --- the handle rule ------------------------------------------------------------------------------------------------


def make_job(monkeypatch, **setup):
    world = FakeWorld()
    for key, value in setup.items():
        setattr(world, key, value)
    proc, job = started(world, monkeypatch)
    return world, job


def test_after_a_successful_close_every_call_is_a_no_op_and_nothing_goes_through_the_handle(monkeypatch):
    world, job = make_job(monkeypatch)
    before = len(world.log)
    assert job.close() is True
    assert (job.stop(), job.live_count(), job.close()) == (False, None, True)
    assert job.count_after_stop(git_job.Deadline()) is None
    assert world.log[before:] == [("close", JOB)]  # one native close; the later calls never reached the native layer
    assert world.violations == [] and job.handle is None


def test_a_close_that_failed_leaves_the_handle_owned_so_a_later_step_can_stop_and_close_it(monkeypatch):
    world, job = make_job(monkeypatch)
    world.close_result = False
    assert job.close() is False and job.handle == JOB
    world.close_result = OSError("broken")
    assert job.close() is False and job.handle == JOB  # an exception is an ordinary failure too
    assert job.stop() is True  # still owned: it can still be stopped
    world.close_result = True
    assert job.close() is True and job.handle is None and world.violations == []


@pytest.mark.parametrize("where", ["before", "after"])
def test_an_interruption_inside_the_native_close_gives_the_handle_up(monkeypatch, where):
    """It may have come after the close succeeded, so the handle is never used again (a leak at worst)."""
    world, job = make_job(monkeypatch)
    first = KeyboardInterrupt("in the close")
    world.close_interrupt = (where, first)
    with pytest.raises(KeyboardInterrupt) as raised:
        job.close()
    assert raised.value is first and job.handle is None
    assert (job.stop(), job.live_count(), job.close()) == (False, None, True) and world.violations == []


def test_an_interruption_before_the_native_close_is_called_leaves_the_handle_owned(monkeypatch):
    world, job = make_job(monkeypatch)
    first = KeyboardInterrupt("before the close call")
    monkeypatch.setattr(git_job, "_BEFORE_CLOSE", [lambda j: (_ for _ in ()).throw(first)])
    with pytest.raises(KeyboardInterrupt):
        job.close()
    assert job.handle == JOB and "close" not in names(world)  # the native close was never called: still owned
    monkeypatch.setattr(git_job, "_BEFORE_CLOSE", [])
    assert job.stop() is True and job.close() is True


def test_the_job_never_uses_a_null_or_the_hosts_job_handle(monkeypatch):
    world, job = make_job(monkeypatch)
    job.stop()
    job.live_count()
    job.close()
    handles = {entry[1] for entry in world.log if entry[0] in ("assign", "terminate", "query", "close")}
    assert handles == {JOB} and 0 not in handles and world.violations == []


# --- stop and the count ---------------------------------------------------------------------------------------------


def test_stop_is_repeatable_and_a_failed_stop_is_false_and_not_a_success(monkeypatch):
    world, job = make_job(monkeypatch)
    world.terminate_ok = False
    assert job.stop() is False and job.stop_succeeded is False
    world.terminate_ok = True
    assert job.stop() is True and job.stop() is True and job.stop_succeeded is True


def test_an_ordinary_error_from_the_stop_is_a_failed_stop_not_an_exception(monkeypatch):
    world, job = make_job(monkeypatch)
    world.terminate_error = OSError("TerminateJobObject broke")
    assert job.stop() is False and job.stop_succeeded is False


def test_a_failed_query_is_none_never_zero(monkeypatch):
    world, job = make_job(monkeypatch)
    world.counts = [None]
    assert job.live_count() is None
    world.counts = None
    world.query_error = OSError("QueryInformationJobObject broke")
    assert job.live_count() is None
    assert job.count_after_stop(git_job.Deadline()) is None


def test_the_count_is_polled_until_it_reaches_zero_within_the_window(monkeypatch):
    clock = FakeClock()
    world = FakeWorld(clock=clock)
    world.install(monkeypatch, script=[])
    _proc, job = git_job.start_in_job(CMD, **KW)
    world.counts = [3, 2, 1, 0]
    assert job.count_after_stop(git_job.Deadline()) == 0
    assert names(world).count("query") == 4


def test_a_count_that_stays_above_zero_stops_polling_at_the_window_and_returns_it(monkeypatch):
    clock = FakeClock()
    world = FakeWorld(clock=clock)
    world.install(monkeypatch, script=[])
    _proc, job = git_job.start_in_job(CMD, **KW)
    world.counts = [2]
    started_at = clock.now
    assert job.count_after_stop(git_job.Deadline()) == 2
    assert clock.now - started_at <= git_job.COUNT_SECONDS + 0.05  # polled for at most 2 s, then gave up


def test_no_count_is_read_when_no_time_is_left(monkeypatch):
    clock = FakeClock()
    world = FakeWorld(clock=clock)
    world.install(monkeypatch, script=[])
    _proc, job = git_job.start_in_job(CMD, **KW)
    deadline = git_job.Deadline()
    clock.now += git_job.CLEANUP_SECONDS + 1  # the deadline has passed
    before = names(world).count("query")
    assert job.count_after_stop(deadline) is None
    assert names(world).count("query") == before  # not even one query


def test_the_deadline_cuts_a_limit_to_what_is_left_and_never_below_zero(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(git_job, "_monotonic", clock.monotonic)
    deadline = git_job.Deadline()
    assert deadline.cut(5) == 5 and deadline.cut(1000) == git_job.CLEANUP_SECONDS
    clock.now += 28
    assert deadline.cut(5) == pytest.approx(2) and deadline.left() == pytest.approx(2)
    clock.now += 10
    assert deadline.cut(5) == 0 and deadline.left() < 0


# --- no walk over process ids, times or the clock of the day --------------------------------------------------------


class _OnlyTheMonotonicClock:
    """`git_job.time` for a test: the monotonic clock and sleep (waiting limits) are allowed, the time of day is not."""

    forbidden = []

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(0)

    def __getattr__(self, name):
        self.forbidden.append(name)
        raise AssertionError(f"git_job asked the clock for {name}")


def test_starting_a_job_and_stopping_it_never_asks_for_a_process_list_an_id_or_a_time(monkeypatch):
    world = FakeWorld()
    guard = _OnlyTheMonotonicClock()
    guard.forbidden.clear()
    proc, job = started(world, monkeypatch)
    monkeypatch.setattr(git_job, "time", guard)
    job.stop()
    job.count_after_stop(git_job.Deadline())
    job.close()
    assert world.forbidden == [] and guard.forbidden == []


# --- a standing guard for direct system calls (section 9) --------------------------------------------------------

# Every way a program could list processes, open one by its id, or read when one started. The fake layer above fails the
# test when the product asks it for one; this reads the code itself, so a direct call through `ctypes` (which never goes
# through the fake) is caught too.
_WALKING_CALLS = (
    "CreateToolhelp32Snapshot",
    "Process32First",
    "Process32Next",
    "OpenProcess",
    "GetProcessTimes",
    "NtQueryInformationProcess",
    "EnumProcesses",
)
_JOB_PATH_FUNCTIONS = (
    "_git_write",
    "_raise_git_write_timeout",
    "_kill_and_reap",
    "_job_confirmed",
    "_close_job",
    "_reap_after_failed_start",
)


def test_the_job_code_never_names_a_call_that_walks_processes():
    sources = {"git_job.py": inspect.getsource(git_job)}
    sources.update({f"cli.{name}": inspect.getsource(getattr(cli, name)) for name in _JOB_PATH_FUNCTIONS})
    assert all(len(text) > 100 for text in sources.values()), "a source was not read: the guard would pass for nothing"
    found = sorted((where, call) for where, text in sources.items() for call in _WALKING_CALLS if call in text)
    assert found == [], f"a call that walks processes, ids or start times is named: {found}"
