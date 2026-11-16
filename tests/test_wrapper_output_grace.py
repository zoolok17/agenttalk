"""Prove output deadlines without real processes or a shared clock patch."""

from types import SimpleNamespace

import pytest

from agenttalk.wrapper import run


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.observations = 0
        self.waits = []

    def observe(self):
        self.observations += 1
        assert self.observations <= 128, "script exhausted: reader did not stop"

    def monotonic(self):
        self.observe()
        return self.now

    def sleep(self, seconds):
        self.observe()
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    # The deadline reads run.time directly, not the clock passed to make_drive.
    # Replace that module-local reference only; the boundary assertions pin >=.
    monkeypatch.setattr(run, "time", clock)
    return clock


@pytest.mark.parametrize("first", ["exit", "finish", "neither"])
def test_grace_starts_only_when_both_facts_are_observed(clock, first):
    facts = SimpleNamespace(exited=first == "exit", finished=first == "finish")
    proc = SimpleNamespace(poll=lambda: 0 if facts.exited else None)
    grace = run._ExitGrace(proc, lambda: facts.finished)
    assert not grace.over()
    clock.now = 100.0
    assert not grace.over()
    facts.exited = facts.finished = True
    assert not grace.over(), "earlier time must not count toward the fresh grace"
    clock.now = 101.999999
    assert not grace.over()
    clock.now = 102.0
    assert grace.over()


def test_grace_has_a_fixed_two_second_inclusive_boundary(clock):
    assert run.OUTPUT_GRACE_AFTER_EXIT_SECONDS == 2.0
    grace = run._ExitGrace(SimpleNamespace(poll=lambda: 0), lambda: True)
    assert not grace.over()
    clock.now = 1.999999
    assert not grace.over()
    clock.now = 2.0
    assert grace.over()
    clock.now = 10.0
    assert grace.over()


class _Condition:
    def __init__(self, clock):
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def wait(self, timeout):
        self.clock.sleep(timeout)

    def notify_all(self):
        self.clock.observe()


@pytest.fixture(params=["plain", "watchdog"])
def reader(request, monkeypatch, clock):
    def consume(read, *, eligible=True):
        pipe = SimpleNamespace(fileno=lambda: 123)

        def poll():
            clock.observe()
            return 0 if eligible else None

        proc = SimpleNamespace(stdout=pipe, poll=poll)

        def read_pipe(actual_pipe):
            clock.observe()
            assert actual_pipe is pipe
            return read()

        monkeypatch.setattr(run, "_read_ready_pipe_chunk", read_pipe)
        if request.param == "plain":
            return list(run._iter_output_lines(proc, lambda: eligible))
        stream = object.__new__(run._ProcStream)
        stream._proc = proc
        stream.turn_finished = eligible
        stream._watchdog_stream_condition = _Condition(clock)
        stream._watchdog_stream_interrupted = False
        stream._watchdog_stream_done = False
        lines = list(stream._iter_until_watchdog_or_eof())
        assert stream._watchdog_stream_done
        return lines

    return consume


@pytest.mark.parametrize("eligible", [True, False])
def test_eof_finishes_without_waiting_for_a_deadline(clock, reader, eligible):
    chunks = [b"ready\n", b""]
    reads = []

    def read():
        reads.append(clock.now)
        assert chunks, "extra read after EOF"
        return chunks.pop(0)

    assert reader(read, eligible=eligible) == ["ready\n"]
    assert reads == [0.0, 0.0]
    assert not chunks
    assert clock.waits == []
    assert clock.now == 0.0


def test_open_idle_pipe_stops_at_the_deadline_without_eof(clock, reader):
    reads = []

    def read():
        assert clock.now < 2.0, "read past the deadline"
        reads.append(clock.now)
        return None

    assert reader(read) == []
    assert run._WATCHDOG_STREAM_POLL_SECONDS == 0.05
    assert reads == pytest.approx([i * 0.05 for i in range(40)])
    assert 2.0 <= clock.now <= 2.05
    assert clock.waits == [0.05] * 40


def test_continuous_output_cannot_extend_the_deadline(clock, reader):
    reads = []

    def read():
        assert clock.now < 2.0, "read past the fixed deadline"
        reads.append(clock.now)
        clock.now += 0.25
        return f"line {len(reads)}\n".encode()

    assert reader(read) == [f"line {i}\n" for i in range(1, 9)]
    assert reads == [i * 0.25 for i in range(8)]
    assert clock.now == 2.0
    assert clock.waits == []
