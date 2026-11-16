"""Keep the deadline tests honest without starting a model process."""

# What these contract tests are coupled to. If any of it is renamed, they fail on
# purpose, and the fix is to move this file along with the code, not to loosen it:
#   - in agenttalk.wrapper.run: _read_ready_pipe_chunk, _iter_output_lines,
#     _ProcStream._iter_until_watchdog_or_eof, _ExitGrace, _WATCHDOG_STREAM_POLL_SECONDS,
#     and the names `time`, `threading` and `os` looked up on the module;
#   - in test_wrapper_exit_grace: _Attempt (positional fields: worker, box, elapsed,
#     started, finished), _attempt, _run_plain, _drive, _pids, _alive, _end_all,
#     _finished_messages, _OutputWaitBudget.assert_bounded and the output_waits argument;
#   - in test_wrapper_output_grace: _Clock;
#   - the failure text "CONTROL waited ..." / "output wait ..." that the delayed cases
#     expect (matched as "CONTROL|output wait").

import inspect
import os
from types import SimpleNamespace

import pytest

import test_wrapper_exit_grace as integration
from test_wrapper_output_grace import _Clock

run = integration.run


def test_fake_clock_advances_by_the_requested_wait():
    clock = _Clock()
    clock.sleep(0.05)
    clock.sleep(1.25)
    assert clock.now == pytest.approx(1.30)
    assert clock.waits == [0.05, 1.25]


class _NoWaitCondition:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def wait(self, timeout=None):
        pass

    def notify_all(self):
        pass


@pytest.mark.parametrize("how", ["plain", "plain-stream", "watchdog"])
@pytest.mark.parametrize("mode", [integration.CONTROL, "survivor-default-inherit"])
@pytest.mark.parametrize("delayed", [False, True])
def test_integration_checks_reject_requested_linger_without_timing_startup(tmp_path, monkeypatch, how, mode, delayed):
    # Drive the real integration assertion with a completed, process-free attempt.
    # The stand-in reader uses the real EOF/grace code; only IO and waiting are fake.
    local_time = SimpleNamespace(monotonic=lambda: 0.0, time=lambda: 0.0, sleep=lambda seconds: None)
    monkeypatch.setattr(integration, "time", local_time)
    monkeypatch.setattr(run, "time", local_time)
    monkeypatch.setattr(run, "threading", SimpleNamespace(Condition=_NoWaitCondition))
    delay = (3.0 if mode == integration.CONTROL else 5.0) if delayed else 0.0
    chunks = [None, b""] if mode != integration.CONTROL else [b""]
    monkeypatch.setattr(run, "_read_ready_pipe_chunk", lambda pipe: chunks.pop(0))
    # The old tests have no recorder. Keeping this conditional lets their missing
    # assertion fail as a passing delayed attempt, rather than an import error.
    budget_type = getattr(integration, "_OutputWaitBudget", None)
    budget = budget_type(monkeypatch) if budget_type else None
    monkeypatch.setattr(run, "_WATCHDOG_STREAM_POLL_SECONDS", delay)

    def completed(*args, **kwargs):
        run.time.sleep(100.0)  # simulated startup, before either EOF or eligibility
        proc = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 123), poll=lambda: 0)
        if how == "watchdog":
            stream = object.__new__(run._ProcStream)
            stream._proc = proc
            stream.turn_finished = True
            stream._watchdog_stream_condition = run.threading.Condition()
            stream._watchdog_stream_interrupted = False
            stream._watchdog_stream_done = False
            assert list(stream._iter_until_watchdog_or_eof()) == []
        else:
            assert list(run._iter_output_lines(proc, lambda: True)) == []
        if mode == integration.CONTROL:
            run.time.sleep(delay)
        return 0 if how == "plain" else SimpleNamespace(ok=True)

    worker = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)

    def attempt(call):
        return integration._Attempt(worker, {"result": call(), "done_at": delay}, delay, 0.0, True)

    monkeypatch.setattr(integration, "_attempt", attempt)
    monkeypatch.setattr(integration, "_run_plain", completed)
    monkeypatch.setattr(integration, "_drive", completed)
    monkeypatch.setattr(integration, "_pids", lambda pidfile: [7])
    monkeypatch.setattr(integration, "_alive", lambda pid: True)
    monkeypatch.setattr(integration, "_end_all", lambda pidfile: None)
    monkeypatch.setattr(integration, "_finished_messages", lambda seen: (True, ["done"]))
    test = (
        integration.test_plain_wrap_stops_waiting_after_the_grace_when_a_survivor_holds_the_pipe
        if how == "plain"
        else integration.test_loop_turn_stops_waiting_after_the_grace_when_a_survivor_holds_the_pipe
    )
    arguments = {"model": tmp_path / "not-started.py", "tmp_path": tmp_path, "mode": mode}
    if how != "plain":
        arguments["watchdog"] = None if how == "plain-stream" else integration.TurnWatchdogConfig(enabled=True)
    if "output_waits" in inspect.signature(test).parameters:
        arguments["output_waits"] = budget
    if delayed:
        with pytest.raises(AssertionError, match="CONTROL|output wait"):
            test(**arguments)
    else:
        test(**arguments)


# --- the pipe probe: it must say "nothing yet" at once, never wait or block -----------------------------------


@pytest.fixture
def pipe_pair():
    read_fd, write_fd = os.pipe()
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass  # a test closed it already


def _probe(read_fd):
    return run._read_ready_pipe_chunk(SimpleNamespace(fileno=lambda: read_fd))


def _forbid_waiting(monkeypatch):
    """No timer: a probe that sleeps or waits is a probe that stalls, so any such call fails the test at once.

    This covers time.sleep and Condition/Event waits. A stall inside a system call, such as a poll with a
    timeout, is not seen here; the unconditional read is what the first test catches.
    """

    def sleeping(seconds):
        raise AssertionError(f"the pipe probe slept for {seconds} s")

    def waiting(self, timeout=None):
        raise AssertionError(f"the pipe probe waited (timeout {timeout})")

    monkeypatch.setattr("time.sleep", sleeping)
    monkeypatch.setattr("threading.Condition.wait", waiting)


def test_probe_returns_none_at_once_on_an_open_pipe_with_no_data(monkeypatch, pipe_pair):
    read_fd, _write_fd = pipe_pair
    _forbid_waiting(monkeypatch)

    def blocking_read(fd, size):
        raise AssertionError("the pipe probe read an empty pipe; the real read would block")

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", blocking_read)
        assert _probe(read_fd) is None
        assert _probe(read_fd) is None  # asking again changes nothing


def test_probe_returns_the_waiting_bytes_and_then_nothing_again(monkeypatch, pipe_pair):
    read_fd, write_fd = pipe_pair
    _forbid_waiting(monkeypatch)
    os.write(write_fd, b"hello")
    assert _probe(read_fd) == b"hello"
    with monkeypatch.context() as patch:
        patch.setattr(os, "read", lambda fd, size: pytest.fail("read an empty pipe"))
        assert _probe(read_fd) is None


def test_probe_returns_empty_bytes_once_the_writer_has_closed(monkeypatch, pipe_pair):
    read_fd, write_fd = pipe_pair
    _forbid_waiting(monkeypatch)
    os.write(write_fd, b"last")
    os.close(write_fd)
    assert _probe(read_fd) == b"last"
    assert _probe(read_fd) == b""


# --- the long-running cases must really call the wait check -------------------------------------------------


class _RecordingBudget:
    def __init__(self):
        self.modes = []

    def assert_bounded(self, mode):
        self.modes.append(mode)


_KEEPS_WRITING = "test_a_survivor_that_keeps_writing_does_not_keep_wrap_waiting"
_LAST_LINES = "test_the_last_lines_before_the_turn_end_are_kept"


@pytest.mark.parametrize(
    "name, mode, how",
    [
        (_KEEPS_WRITING, "writing", "plain"),
        (_KEEPS_WRITING, "writing", "loop"),
        (_KEEPS_WRITING, "writing", "loop-watchdog"),
        (_LAST_LINES, "burst-then-writing", None),
    ],
)
def test_the_writing_and_last_lines_cases_check_the_output_wait(tmp_path, monkeypatch, name, mode, how):
    # Delete the assert_bounded call from either case and this test fails. Without it,
    # those cases would pass even if the wrapper waited far longer than the grace.
    worker = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)
    monkeypatch.setattr(
        integration,
        "_attempt",
        lambda call: integration._Attempt(worker, {"result": call(), "done_at": 0.0}, 0.0, 0.0, True),
    )
    monkeypatch.setattr(integration, "_run_plain", lambda *args, **kwargs: 0)
    monkeypatch.setattr(integration, "_drive", lambda *args, **kwargs: SimpleNamespace(ok=True))
    monkeypatch.setattr(integration, "_pids", lambda pidfile: [7])
    monkeypatch.setattr(integration, "_alive", lambda pid: True)
    monkeypatch.setattr(integration, "_end_all", lambda pidfile: None)
    monkeypatch.setattr(integration, "_finished_messages", lambda seen: (True, [f"line {i}" for i in range(3000)]))
    budget = _RecordingBudget()
    test = getattr(integration, name)
    arguments = {"model": tmp_path / "not-started.py", "tmp_path": tmp_path, "output_waits": budget}
    if how:
        arguments["how"] = how
    test(**arguments)
    assert budget.modes == [mode]
