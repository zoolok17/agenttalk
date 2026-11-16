"""Keep the deadline tests honest without starting a model process."""

import inspect
from types import SimpleNamespace

import pytest

import test_wrapper_exit_grace as integration
from test_wrapper_output_grace import _Clock


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
    run = integration.run
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
