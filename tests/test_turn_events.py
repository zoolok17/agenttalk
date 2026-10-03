"""The turn journal writer (src/agenttalk/turn_events.py).

The tests read back what was actually written, with the module's own reader,
and drive the writer thread through a files seam that can block, fail or count
each operation. Nothing here starts a real model process or touches a network.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from agenttalk import turn_events as te
from agenttalk.turn_events import JournalFiles, TurnEventSink

USAGE = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50, "cache_write_tokens": 20}
MESSAGE_AT = "2026-10-03T00:00:00.000Z"


class Hooked(JournalFiles):
    """A files seam whose operations can be watched, blocked or made to fail."""

    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}
        self.calls: list[str] = []

    def _hook(self, op: str, *args: Any) -> None:
        self.calls.append(op)
        hook = self.hooks.get(op)
        if hook is not None:
            hook(*args)

    def makedirs(self, path):
        self._hook("makedirs", path)
        super().makedirs(path)

    def open_append(self, path):
        self._hook("open_append", path)
        return super().open_append(path)

    def write(self, handle, data):
        self._hook("write", handle, data)
        super().write(handle, data)

    def sync(self, handle):
        self._hook("sync", handle)
        super().sync(handle)

    def append_line(self, path, data):
        self._hook("append_line", path, data)
        super().append_line(path, data)

    def write_atomic(self, path, data):
        self._hook("write_atomic", path, data)
        super().write_atomic(path, data)

    def remove(self, path):
        self._hook("remove", path)
        super().remove(path)


def is_event(data: bytes) -> bool:
    return b'"seq"' in data


def make(tmp_path: Path, **kw: Any) -> TurnEventSink:
    kw.setdefault("agent_version", "0.0.0")
    kw.setdefault("start_seconds", 5.0)
    kw.setdefault("close_seconds", 5.0)
    return TurnEventSink(tmp_path / "journal", "alpha", **kw)


def agent_dir(tmp_path: Path) -> Path:
    return tmp_path / "journal" / "alpha"


def started(sink, mid="m1", turn="t1"):
    sink.emit(
        "dispatch_started", message_id=mid, turn_id=turn, cli="claude", cli_session="fresh", message_at=MESSAGE_AT
    )


def ended(sink, mid="m1", turn="t1", **over):
    fields = {
        "message_id": mid,
        "turn_id": turn,
        "launched": True,
        "outcome": "success",
        "exit": "normal",
        "duration_ms": 5,
        "usage": USAGE,
    }
    fields.update(over)
    sink.emit("dispatch_ended", **fields)


def disposed(sink, mid="m1", disposition="completed", **over):
    fields = {
        "message_id": mid,
        "disposition": disposition,
        "message_at": MESSAGE_AT,
        "consumed": True,
        "landed": True,
        "compliance_success": True,
        "dead_lettered": False,
        "terminal_failure": False,
    }
    fields.update(over)
    sink.emit("message_disposed", **fields)


def read_everything(directory: Path) -> list[dict]:
    """Every record of every segment, in file order."""
    out = []
    for _generation, _number, path in te.list_segments(directory):
        out.extend(te.read_segment(path).records)
    return out


def events_only(records: list[dict]) -> list[dict]:
    return [r for r in records if "seq" in r]


def status_of(sink: TurnEventSink) -> dict:
    return json.loads(Path(sink.status_path).read_text(encoding="ascii"))


def all_bytes(directory: Path) -> bytes:
    blob = b""
    for root, _dirs, names in os.walk(directory):
        for name in names:
            blob += (Path(root) / name).read_bytes()
    return blob


def run_clean(tmp_path: Path, **kw: Any) -> TurnEventSink:
    sink = make(tmp_path, **kw)
    assert sink.start() is True
    return sink


# --- putting the writer and the bounds in a fixed order, however slow the machine is ----------


class _AckCountedFrom(threading.Event):
    """The writer's acknowledgement, whose wait in start() begins only once `ready` is set: the
    start bound then passes with the writer provably inside the blocked step. Getting there has
    its own generous limit; `bound_began` is when the real wait began, so a test that times
    the caller measures only the bound, never the writer's way to its start position."""

    def __init__(self, ready: threading.Event) -> None:
        super().__init__()
        self._ready = ready
        self.bound_began: float | None = None

    def wait(self, timeout: float | None = None) -> bool:
        assert self._ready.wait(10)
        self.bound_began = time.monotonic()
        return super().wait(timeout)


def start_bound_counted_from(sink: TurnEventSink, ready: threading.Event) -> _AckCountedFrom:
    ack = _AckCountedFrom(ready)
    sink._ack = ack  # noqa: SLF001
    return ack


class _JoinedFrom:
    """Stands in for the writer thread inside close(): close's wait begins only once `ready` is
    set and then lasts a moment, so close times out with the writer provably inside the
    blocked step."""

    def __init__(self, thread: threading.Thread, ready: threading.Event) -> None:
        self._thread, self._ready = thread, ready

    def join(self, timeout: float | None = None) -> None:
        assert self._ready.wait(10)
        self._thread.join(0.05)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def until_close_deadline_passed(sink: TurnEventSink) -> None:
    """close() can return a moment before its own deadline by the journal's clock (Windows on
    Python 3.12 and earlier reads a 15.6 ms clock; the wait has its own timer). Until the
    deadline passes the startup status note is still allowed, so wait it out on that clock."""
    give_up = time.monotonic() + 10
    while te.time.monotonic() < sink._deadline:  # noqa: SLF001
        assert time.monotonic() < give_up
        time.sleep(0.005)


def valid_event(kind: str = "message_disposed", **over: Any) -> dict:
    base = {
        "v": 1,
        "kind": kind,
        "event_id": str(uuid.uuid4()),
        "stream": "alpha.g1",
        "at": "2026-10-03T00:00:00.000Z",
        "agent": "alpha",
        "dropped_total": 0,
    }
    extra: dict[str, Any] = {
        "dispatch_started": {
            "seq": 1,
            "message_id": "m1",
            "turn_id": "t1",
            "cli": "claude",
            "cli_session": "resume",
            "message_at": MESSAGE_AT,
        },
        "dispatch_ended": {
            "seq": 2,
            "message_id": "m1",
            "turn_id": "t1",
            "launched": True,
            "outcome": "failed",
            "exit": "spawn_error",
            "duration_ms": 3,
            "usage": None,
            "failure_class": "other",
        },
        "message_disposed": {
            "seq": 3,
            "message_id": "m1",
            "disposition": "outcome_unknown",
            "message_at": None,
            "consumed": True,
            "landed": None,
            "compliance_success": False,
            "dead_lettered": None,
            "terminal_failure": None,
        },
        "stream_started": {
            "agent_version": "x",
            "previous_stream": None,
            "mode": "loop",
            "unmanaged": ["cadence"],
            "segment": 1,
            "after_seq": 0,
        },
        "stream_closed": {"last_seq": 3},
    }[kind]
    base.update(extra)
    base.update(over)
    return base


# --- the closed schema -------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["dispatch_started", "dispatch_ended", "message_disposed", "stream_started", "stream_closed"]
)
def test_every_kind_round_trips_through_one_line(kind):
    event = valid_event(kind)
    assert te.parse_line(te.encode_line(event)) == event


def test_an_unknown_schema_version_is_refused_not_guessed_at():
    with pytest.raises(te.UnsupportedSchemaVersion):
        te.validate_event(valid_event(v=2))
    with pytest.raises(te.UnsupportedSchemaVersion):
        te.validate_event(valid_event(v="1"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(extra_key=1),
        lambda e: e.pop("message_id"),
        lambda e: e.update(seq=0),
        lambda e: e.update(seq=True),
        lambda e: e.update(seq="3"),
        lambda e: e.update(dropped_total=-1),
        lambda e: e.update(disposition="consumed_undriven"),
        lambda e: e.update(disposition="cancelled"),
        lambda e: e.update(consumed=1),
        lambda e: e.update(landed="true"),
        lambda e: e.update(message_at=5),
        lambda e: e.update(stream="beta.g1"),
        lambda e: e.update(event_id="nope"),
        lambda e: e.update(at="yesterday"),
        lambda e: e.update(kind="turn_ended"),
        lambda e: e.pop("terminal_failure"),
    ],
)
def test_wrong_keys_types_and_words_are_refused(mutate):
    event = valid_event("message_disposed")
    mutate(event)
    with pytest.raises(te.TurnEventError):
        te.validate_event(event)


def test_stream_records_carry_no_seq_and_the_closing_record_says_how_far_it_went():
    for kind in ("stream_started", "stream_closed"):
        te.validate_event(valid_event(kind))
        with pytest.raises(te.TurnEventError):
            te.validate_event(valid_event(kind, seq=1))
    with pytest.raises(te.TurnEventError):
        bad = valid_event("stream_closed")
        del bad["last_seq"]
        te.validate_event(bad)


def test_usage_is_tokens_only_and_unknown_is_null_never_zero():
    ended_event = valid_event("dispatch_ended", outcome="success", exit="normal")
    ended_event.pop("failure_class")
    for usage in (None, USAGE, {**USAGE, "input_tokens": None, "cache_write_tokens": None}):
        te.validate_event({**ended_event, "usage": usage})
    for bad in (
        {"input_tokens": 1},
        {**USAGE, "model": "x"},
        {**USAGE, "output_tokens": -1},
        {**USAGE, "output_tokens": 1.5},
    ):
        with pytest.raises(te.TurnEventError):
            te.validate_event({**ended_event, "usage": bad})


def test_the_failure_class_is_a_closed_word_and_the_launch_flag_must_fit_the_outcome():
    event = valid_event("dispatch_ended")
    with pytest.raises(te.TurnEventError):
        te.validate_event({**event, "failure_class": "boom: secret text"})
    with pytest.raises(te.TurnEventError):
        te.validate_event({**event, "outcome": "not_launched", "launched": True})
    with pytest.raises(te.TurnEventError):
        te.validate_event({**event, "outcome": "success", "launched": False})
    te.validate_event({**event, "outcome": "not_launched", "launched": False})


def test_a_line_over_four_kilobytes_is_refused():
    event = valid_event("dispatch_started", message_id="m" * 128)
    te.encode_line(event)
    with pytest.raises(te.TurnEventError):
        te.encode_line({**event, "x": "y" * 5000})


# --- writing and reading back --------------------------------------------------


def test_a_clean_run_writes_header_events_and_a_closing_record(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    ended(sink)
    disposed(sink)
    sink.close()
    records = read_everything(agent_dir(tmp_path))
    assert [r["kind"] for r in records] == [
        "stream_started",
        "dispatch_started",
        "dispatch_ended",
        "message_disposed",
        "stream_closed",
    ]
    header, *middle, closing = records
    assert (header["segment"], header["after_seq"], header["previous_stream"], header["v"]) == (1, 0, None, 1)
    assert "seq" not in header and "seq" not in closing
    assert [r["seq"] for r in middle] == [1, 2, 3]
    assert closing["last_seq"] == 3 and closing["dropped_total"] == 0
    assert all(r["stream"] == sink.stream and r["agent"] == "alpha" for r in records)
    assert sink.state == "closed" and sink.off_reason is None


def test_every_event_is_exactly_one_write(tmp_path):
    files = Hooked()
    writes: list[bytes] = []
    files.hooks["write"] = lambda handle, data: writes.append(data)
    sink = run_clean(tmp_path, files=files)
    for index in range(5):
        started(sink, turn="t%d" % index)
    sink.close()
    lines = [w for w in writes if is_event(w)]
    assert len(lines) == 5 and all(w.endswith(b"\n") and w.count(b"\n") == 1 for w in lines)


def test_a_disposition_is_forced_to_disk_right_after_it_is_written(tmp_path):
    files = Hooked()
    order: list[str] = []
    files.hooks["write"] = lambda _h, data: order.append(
        "write:" + ("disposed" if b"message_disposed" in data else "other")
    )
    files.hooks["sync"] = lambda _h: order.append("sync")
    sink = run_clean(tmp_path, files=files, sync_seconds=3600)
    started(sink)
    disposed(sink)
    sink.close()
    at = order.index("write:disposed")
    assert order[at + 1] == "sync"


def test_the_streams_record_chains_a_second_stream_in_the_same_folder(tmp_path):
    first = run_clean(tmp_path)
    started(first)
    first.close()
    second = run_clean(tmp_path)
    started(second, mid="m2")
    second.close()
    streams = te.read_streams(agent_dir(tmp_path))
    assert [s["stream"] for s in streams] == [first.stream, second.stream]
    assert streams[0]["previous_stream"] is None and streams[1]["previous_stream"] == first.stream
    headers = [r for r in read_everything(agent_dir(tmp_path)) if r["kind"] == "stream_started"]
    assert {h["stream"]: h["previous_stream"] for h in headers} == {first.stream: None, second.stream: first.stream}
    # each stream numbers its own events from 1
    for stream in (first.stream, second.stream):
        seqs = [r["seq"] for r in events_only(read_everything(agent_dir(tmp_path))) if r["stream"] == stream]
        assert seqs == [1]


def test_the_streams_record_survives_a_deleted_segment(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    sink.close()
    for _generation, _number, path in te.list_segments(agent_dir(tmp_path)):
        path.unlink()
    assert te.list_segments(agent_dir(tmp_path)) == []
    assert [s["stream"] for s in te.read_streams(agent_dir(tmp_path))] == [sink.stream]


def test_the_reader_continues_from_a_cursor_and_leaves_a_torn_line_for_later(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    sink.close()
    directory = agent_dir(tmp_path)
    cursor: dict = {}
    first = list(te.iter_records(directory, cursor))
    assert [r[2]["kind"] for r in first] == ["stream_started", "dispatch_started", "stream_closed"]
    assert list(te.iter_records(directory, cursor)) == []
    path = te.list_segments(directory)[0][2]
    line = te.encode_line(valid_event("dispatch_started", stream=sink.stream, seq=9))
    with open(path, "ab") as handle:
        handle.write(line[:20])
    assert list(te.iter_records(directory, cursor)) == []
    assert te.read_segment(path, cursor[(sink.generation, 1)]).torn is True
    with open(path, "ab") as handle:
        handle.write(line[20:])
    assert [r[2]["seq"] for r in te.iter_records(directory, cursor)] == [9]


def test_a_torn_last_line_is_ignored_and_an_unknown_version_in_a_segment_raises(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    sink.close()
    path = te.list_segments(agent_dir(tmp_path))[0][2]
    with open(path, "ab") as handle:
        handle.write(b'{"v":1,"kind":"dispatch_sta')
    read = te.read_segment(path)
    assert read.torn is True and read.damaged == 0 and len(read.records) == 3
    with open(path, "ab") as handle:
        handle.write(b'\n{"v":2,"kind":"x"}\n')
    with pytest.raises(te.UnsupportedSchemaVersion):
        te.read_segment(path)


def test_a_damaged_complete_line_is_counted_not_fatal(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    sink.close()
    path = te.list_segments(agent_dir(tmp_path))[0][2]
    with open(path, "ab") as handle:
        handle.write(b"not json\n")
    read = te.read_segment(path)
    assert read.damaged == 1 and len(read.records) == 3


# --- rotation, backlog and cap (D4) ----------------------------------------------


def blocked_first_event(files: Hooked) -> tuple[threading.Event, threading.Event]:
    """Make the writer wait at its first event write, so a backlog can build up."""
    entered, unblock = threading.Event(), threading.Event()
    state = {"done": False}

    def hook(_handle, data):
        if is_event(data) and not state["done"]:
            state["done"] = True
            entered.set()
            assert unblock.wait(10)

    files.hooks["write"] = hook
    return entered, unblock


def test_rotation_with_a_backlog_keeps_numbers_continuous_and_headers_unnumbered(tmp_path):
    files = Hooked()
    entered, unblock = blocked_first_event(files)
    sink = run_clean(tmp_path, files=files, segment_bytes=600)
    started(sink, turn="t0")
    assert entered.wait(5)
    for index in range(1, 12):
        started(sink, turn="t%d" % index)  # all queued behind the blocked write
    unblock.set()
    sink.close()
    records = read_everything(agent_dir(tmp_path))
    headers = [r for r in records if r["kind"] == "stream_started"]
    events = events_only(records)
    assert [e["seq"] for e in events] == list(range(1, 13))
    assert len(headers) >= 3
    assert [h["segment"] for h in headers] == list(range(1, len(headers) + 1))
    assert all("seq" not in h for h in headers)
    # each header's after_seq is the highest event number written before it
    segments = te.list_segments(agent_dir(tmp_path))
    written = 0
    for (_g, _n, path), header in zip(segments, headers, strict=True):
        assert header["after_seq"] == written
        seen = [r["seq"] for r in te.read_segment(path).records if "seq" in r]
        written = seen[-1] if seen else written
    assert records[-1]["kind"] == "stream_closed" and records[-1]["last_seq"] == 12


def test_a_failed_write_starts_a_new_segment_and_loses_exactly_that_event(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    seen = {"events": 0}

    def hook(_handle, data):
        if not is_event(data):
            return
        seen["events"] += 1
        if seen["events"] == 1:
            entered.set()
            assert unblock.wait(10)
        if seen["events"] == 4:
            raise OSError("SENTINEL-EXC-DISK")

    files.hooks["write"] = hook
    sink = run_clean(tmp_path, files=files)
    started(sink, turn="t0")
    assert entered.wait(5)
    for index in range(1, 10):
        started(sink, turn="t%d" % index)  # a queued backlog
    unblock.set()
    sink.close()
    records = read_everything(agent_dir(tmp_path))
    events = events_only(records)
    assert [e["seq"] for e in events] == [1, 2, 3, 5, 6, 7, 8, 9, 10]
    headers = [r for r in records if r["kind"] == "stream_started"]
    assert [h["segment"] for h in headers] == [1, 2]
    assert headers[1]["after_seq"] == 3  # the failed number 4 was never written
    assert [e["dropped_total"] for e in events] == [0, 0, 0, 1, 1, 1, 1, 1, 1]
    assert records[-1]["kind"] == "stream_closed" and records[-1]["dropped_total"] == 1
    status = status_of(sink)
    assert status["counts"]["write_failures"] == 1 and status["last_fault"] == "write_failed"
    assert status["faults"]["write_failed"] == 1
    assert b"SENTINEL-EXC-DISK" not in all_bytes(agent_dir(tmp_path))


def test_drops_from_a_full_queue_leave_a_visible_counted_gap(tmp_path):
    files = Hooked()
    entered, unblock = blocked_first_event(files)
    sink = run_clean(tmp_path, files=files, queue_max=4)
    started(sink, turn="t0")
    assert entered.wait(5)
    started_at = time.monotonic()
    for index in range(1, 60):
        started(sink, turn="t%d" % index)
    assert time.monotonic() - started_at < 2.0  # emit never waited for the blocked writer
    unblock.set()
    deadline = time.monotonic() + 5
    while not sink._queue.empty() and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.01)
    time.sleep(0.05)
    for index in range(60, 63):  # events AFTER the gap: the gap is in the middle
        started(sink, turn="t%d" % index)
    sink.close()
    records = read_everything(agent_dir(tmp_path))
    events = events_only(records)
    written = [e["seq"] for e in events]
    assert written[0] == 1 and written[-3:] == [61, 62, 63] and len(written) < 63
    for event in events:  # dropped_total is exactly the missing numbers below it
        assert event["dropped_total"] == event["seq"] - 1 - sum(1 for s in written if s < event["seq"])
    assert events[-1]["dropped_total"] > 0
    closing = records[-1]
    assert closing["kind"] == "stream_closed" and closing["last_seq"] == 63
    assert closing["dropped_total"] == 63 - len(written)
    assert status_of(sink)["counts"]["queue_full"] == 63 - len(written)


def test_drops_at_the_very_end_are_exposed_by_the_closing_record(tmp_path):
    files = Hooked()
    entered, unblock = blocked_first_event(files)
    sink = run_clean(tmp_path, files=files, queue_max=4)
    started(sink, turn="t0")
    assert entered.wait(5)
    for index in range(1, 30):
        started(sink, turn="t%d" % index)
    unblock.set()
    sink.close()
    records = read_everything(agent_dir(tmp_path))
    written = [e["seq"] for e in events_only(records)]
    assert written == list(range(1, len(written) + 1)) and len(written) < 30  # no gap before the tail
    assert records[-1]["last_seq"] == 30 and records[-1]["dropped_total"] == 30 - len(written)


def test_an_invalid_event_is_dropped_counted_and_never_raised(tmp_path):
    sink = run_clean(tmp_path)
    started(sink, turn="t0")
    sink.emit(
        "dispatch_started", message_id="m1", turn_id="t1", cli="claude", cli_session="fresh", prompt="SENTINEL-PROMPT"
    )
    sink.emit("nonsense_kind")
    started(sink, turn="t3")
    sink.close()
    events = events_only(read_everything(agent_dir(tmp_path)))
    assert [e["seq"] for e in events] == [1, 4]
    assert events[1]["dropped_total"] == 2
    assert status_of(sink)["counts"]["invalid"] == 2
    assert b"SENTINEL-PROMPT" not in all_bytes(agent_dir(tmp_path))


def test_the_cap_removes_the_oldest_segments_never_the_open_one(tmp_path):
    sink = run_clean(tmp_path, segment_bytes=400, max_bytes=1500)
    for index in range(40):
        started(sink, turn="t%d" % index)
    sink.close()
    segments = te.list_segments(agent_dir(tmp_path))
    total = sum(path.stat().st_size for _g, _n, path in segments)
    numbers = [n for _g, n, _p in segments]
    assert numbers[-1] > numbers[0] > 1  # the early ones are gone, the last is kept
    assert total <= 1500 + 400
    first_left = events_only(te.read_segment(segments[0][2]).records)
    assert first_left and first_left[0]["seq"] > 1  # a visible gap at the start
    assert te.read_streams(agent_dir(tmp_path))[0]["stream"] == sink.stream
    assert status_of(sink)["counts"]["segments_removed"] >= 1


def test_a_cap_smaller_than_one_segment_still_keeps_the_open_segment(tmp_path):
    sink = run_clean(tmp_path, segment_bytes=400, max_bytes=50)
    for index in range(20):
        started(sink, turn="t%d" % index)
    sink.close()
    segments = te.list_segments(agent_dir(tmp_path))
    assert len(segments) == 1
    records = te.read_segment(segments[0][2]).records
    assert records[0]["kind"] == "stream_started" and records[-1]["kind"] == "stream_closed"


def test_a_cap_removal_that_fails_is_counted_and_does_not_stop_the_journal(tmp_path):
    files = Hooked()

    def fail(_path):
        raise PermissionError("SENTINEL-EXC-LOCK")

    files.hooks["remove"] = fail
    sink = run_clean(tmp_path, files=files, segment_bytes=400, max_bytes=800)
    for index in range(30):
        started(sink, turn="t%d" % index)
    sink.close()
    assert len(events_only(read_everything(agent_dir(tmp_path)))) == 30
    assert status_of(sink)["faults"]["remove_failed"] >= 1
    assert b"SENTINEL-EXC-LOCK" not in all_bytes(agent_dir(tmp_path))


# --- never making the turn wait ---------------------------------------------------


def test_a_writer_that_never_returns_cannot_slow_emit_or_hold_up_close(tmp_path):
    files = Hooked()
    entered, unblock = blocked_first_event(files)
    sink = run_clean(tmp_path, files=files, queue_max=8, close_seconds=0.4)
    started(sink, turn="t0")
    assert entered.wait(5)
    begin = time.monotonic()
    for _ in range(1, 5000):
        started(sink, turn="x")
    assert time.monotonic() - begin < 2.0
    begin = time.monotonic()
    sink.close()
    assert time.monotonic() - begin < 2.0  # one deadline, no matter what is stuck
    assert sink.state == "off" and sink.off_reason == te.OFF_CLOSE_TIMEOUT
    unblock.set()
    time.sleep(0.2)
    records = read_everything(agent_dir(tmp_path))
    assert not any(r["kind"] == "stream_closed" for r in records)  # an overrun writes nothing more
    started(sink)  # still a no-op, still no raise
    sink.close()


def test_a_writer_stuck_in_a_status_update_cannot_slow_emit(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()

    calls = {"n": 0}

    def hook(_path, _data):
        calls["n"] += 1
        if calls["n"] >= 2:  # the first status record belongs to registering the stream
            entered.set()
            assert unblock.wait(10)

    files.hooks["write_atomic"] = hook
    sink = run_clean(tmp_path, files=files, queue_max=4, close_seconds=0.3, status_seconds=0.05)
    assert entered.wait(5)
    begin = time.monotonic()
    for index in range(2000):
        started(sink, turn="t%d" % index)
    assert time.monotonic() - begin < 2.0
    begin = time.monotonic()
    sink.close()
    assert time.monotonic() - begin < 2.0
    unblock.set()


def test_a_writer_stuck_in_rotation_cannot_slow_emit(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    state = {"opens": 0}

    def hook(_path):
        state["opens"] += 1
        if state["opens"] == 2:  # the second segment
            entered.set()
            assert unblock.wait(10)

    files.hooks["open_append"] = hook
    sink = run_clean(tmp_path, files=files, segment_bytes=300, queue_max=4, close_seconds=0.3)
    for index in range(10):
        started(sink, turn="t%d" % index)
    assert entered.wait(5)
    begin = time.monotonic()
    for index in range(2000):
        started(sink, turn="u%d" % index)
    assert time.monotonic() - begin < 2.0
    begin = time.monotonic()
    sink.close()
    assert time.monotonic() - begin < 2.0
    unblock.set()


def test_no_input_or_output_happens_in_the_caller_while_emitting(tmp_path):
    files = Hooked()
    sink = run_clean(tmp_path, files=files)
    caller = threading.get_ident()
    seen: list[int] = []
    files.hooks["write"] = lambda *_a: seen.append(threading.get_ident())
    for index in range(20):
        started(sink, turn="t%d" % index)
    sink.close()
    assert seen and caller not in seen


def test_a_thread_that_cannot_start_switches_the_journal_off_quietly(tmp_path, monkeypatch):
    def refuse(self):
        raise RuntimeError("SENTINEL-EXC-THREAD")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    sink = make(tmp_path)
    assert sink.start() is False
    assert sink.state == "off" and sink.off_reason == te.OFF_START_FAILED
    started(sink)
    sink.close()
    assert not (tmp_path / "journal").exists()


def test_emit_before_start_and_after_close_does_nothing_and_takes_no_number(tmp_path):
    sink = make(tmp_path)
    started(sink)  # not started yet
    assert sink.start() is True
    started(sink, turn="t1")
    sink.close()
    started(sink, turn="t2")  # closed
    seqs = [e["seq"] for e in events_only(read_everything(agent_dir(tmp_path)))]
    assert seqs == [1]


def test_emit_never_raises_whatever_it_is_given(tmp_path):
    sink = run_clean(tmp_path)
    sink.emit(None)  # type: ignore[arg-type]
    sink.emit("dispatch_started", **{"x": object()})
    sink.emit("dispatch_started", message_id=object())
    sink.close()


# --- registration and the start bound (D3, D3b, D3d) ----------------------------------


def test_a_registration_held_past_the_bound_latches_off_and_writes_no_event(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()

    def hook(_path, _data):
        entered.set()
        assert unblock.wait(10)

    files.hooks["append_line"] = hook  # the streams record: first step of registration
    sink = make(tmp_path, files=files, start_seconds=0.2)
    ack = start_bound_counted_from(sink, entered)
    assert sink.start() is False
    # once the writer is in place, the caller is let go within its bound (timed from then on)
    assert ack.bound_began is not None and time.monotonic() - ack.bound_began < 2.0
    assert entered.is_set()
    assert sink.state == "off" and sink.off_reason == te.OFF_START_TIMEOUT
    unblock.set()  # the blocked write may finish, but nothing new starts
    sink._thread.join(5)  # noqa: SLF001
    started(sink)
    sink.close()
    assert te.list_segments(agent_dir(tmp_path)) == []  # no header, no event
    assert sink.state == "off"  # a late success never turns it back on
    status = status_of(sink)
    assert status["state"] == "off" and status["off_reason"] == "start_timeout"


def test_a_forced_registration_failure_says_start_failed(tmp_path):
    files = Hooked()

    def fail(_path, _data):
        raise OSError(errno.EIO, "SENTINEL-EXC-REG")

    files.hooks["append_line"] = fail
    sink = make(tmp_path, files=files)
    assert sink.start() is False
    assert sink.off_reason == te.OFF_START_FAILED
    started(sink)
    sink.close()
    assert te.list_segments(agent_dir(tmp_path)) == []
    status = status_of(sink)
    assert status["off_reason"] == "start_failed" and status["faults"]["registration_failed"] == 1
    assert b"SENTINEL-EXC-REG" not in all_bytes(agent_dir(tmp_path))


def test_an_unwritable_folder_switches_the_journal_off_without_raising(tmp_path):
    files = Hooked()

    def fail(_path):
        raise PermissionError("SENTINEL-EXC-DIR")

    files.hooks["makedirs"] = fail
    sink = make(tmp_path, files=files)
    assert sink.start() is False and sink.off_reason == te.OFF_START_FAILED
    sink.close()


def test_a_bad_agent_name_never_reaches_the_disk(tmp_path):
    sink = TurnEventSink(tmp_path / "journal", "../escape")
    assert sink.start() is False and sink.off_reason == te.OFF_START_FAILED
    assert not (tmp_path / "journal").exists()


def test_start_is_idempotent(tmp_path):
    sink = make(tmp_path)
    assert sink.start() is True and sink.start() is True
    sink.close()
    assert sink.start() is False


def test_a_writer_that_dies_unexpectedly_switches_off_with_a_closed_reason(tmp_path, monkeypatch):
    sink = make(tmp_path)

    def boom():
        raise RuntimeError("SENTINEL-EXC-LOOP")

    monkeypatch.setattr(sink, "_loop", boom)
    sink.start()  # the writer may already have died when this returns
    sink._thread.join(5)  # noqa: SLF001
    assert sink.off_reason == te.OFF_WRITER_ERROR
    started(sink)
    sink.close()
    assert b"SENTINEL-EXC-LOOP" not in all_bytes(agent_dir(tmp_path))


# --- faults are counted and visible --------------------------------------------------------


def test_a_full_disk_is_counted_by_name_and_the_journal_recovers(tmp_path):
    files = Hooked()
    state = {"events": 0}

    def hook(_handle, data):
        if is_event(data):
            state["events"] += 1
            if state["events"] in (2, 3):
                raise OSError(errno.ENOSPC, "SENTINEL-EXC-FULL")

    files.hooks["write"] = hook
    sink = run_clean(tmp_path, files=files)
    for index in range(6):
        started(sink, turn="t%d" % index)
    sink.close()
    seqs = [e["seq"] for e in events_only(read_everything(agent_dir(tmp_path)))]
    assert seqs == [1, 4, 5, 6]
    status = status_of(sink)
    assert status["faults"]["disk_full"] == 2 and status["counts"]["dropped"] == 2
    assert status["last_fault"] == "disk_full"
    assert b"SENTINEL-EXC-FULL" not in all_bytes(agent_dir(tmp_path))


def test_a_writer_that_raises_something_unexpected_loses_only_that_event(tmp_path):
    files = Hooked()
    state = {"events": 0}

    def hook(_handle, data):
        if is_event(data):
            state["events"] += 1
            if state["events"] == 2:
                raise ValueError("SENTINEL-EXC-ODD")

    files.hooks["write"] = hook
    sink = run_clean(tmp_path, files=files)
    for index in range(4):
        started(sink, turn="t%d" % index)
    sink.close()
    assert [e["seq"] for e in events_only(read_everything(agent_dir(tmp_path)))] == [1, 3, 4]
    assert status_of(sink)["faults"]["write_failed"] == 1


def test_a_failing_status_write_is_counted_and_harmless(tmp_path):
    files = Hooked()
    state = {"n": 0}

    def hook(_path, _data):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("SENTINEL-EXC-STATUS")

    files.hooks["write_atomic"] = hook
    sink = run_clean(tmp_path, files=files)
    started(sink)
    sink.close()
    assert len(events_only(read_everything(agent_dir(tmp_path)))) == 1
    assert status_of(sink)["faults"]["status_failed"] == 1


def test_the_writer_has_no_log_hook_and_a_fault_only_reaches_its_status_record(tmp_path, capsys):
    with pytest.raises(TypeError):
        te.TurnEventSink(tmp_path, "alpha", log=lambda word, counts: None)  # type: ignore[call-arg]
    files = Hooked()

    def fail(_handle, data):
        if is_event(data):
            raise OSError("SENTINEL-EXC-LOG")

    files.hooks["write"] = fail
    sink = run_clean(tmp_path, files=files)
    started(sink)
    sink.close()
    shown = capsys.readouterr()
    assert shown.out == "" and shown.err == ""
    assert status_of(sink)["faults"]["write_failed"] >= 1
    assert "SENTINEL" not in repr(status_of(sink))


# --- the status record -----------------------------------------------------------------------


def test_the_status_record_describes_the_stream(tmp_path):
    sink = run_clean(tmp_path, unmanaged=("cadence",))
    started(sink)
    disposed(sink)
    sink.close()
    status = status_of(sink)
    assert status["schema_version"] == 1 and status["stream"] == sink.stream and status["agent"] == "alpha"
    assert status["pid"] == os.getpid() and status["unmanaged"] == ["cadence"] and status["mode"] == "loop"
    assert status["state"] == "closed" and status["off_reason"] is None
    assert status["counts"]["emitted"] == 2 and status["counts"]["written"] == 2
    assert te.parse_time(status["updated_at"]) is not None and te.parse_time(status["started_at"]) is not None
    assert not any(name.endswith(".tmp") for name in os.listdir(agent_dir(tmp_path)))


def test_old_status_files_are_pruned_to_a_few(tmp_path):
    for _ in range(8):
        sink = run_clean(tmp_path)
        sink.close()
        time.sleep(0.01)
    statuses = [n for n in os.listdir(agent_dir(tmp_path)) if n.startswith("status-")]
    assert len(statuses) <= 5 + 1


# --- privacy -------------------------------------------------------------------------------


def test_a_poisoned_run_leaves_no_trace_of_paths_environment_or_exception_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTALK_TEST_SECRET", "SENTINEL-ENV-VALUE")
    files = Hooked()
    state = {"events": 0}

    def hook(_handle, data):
        if is_event(data):
            state["events"] += 1
            if state["events"] == 2:
                raise OSError("SENTINEL-EXC-TEXT in SENTINEL-PATH-DIR")

    files.hooks["write"] = hook
    root = tmp_path / "SENTINEL-PATH-DIR"
    sink = TurnEventSink(root, "alpha", files=files, start_seconds=5, close_seconds=5)
    assert sink.start()
    for index in range(4):
        started(sink, turn="t%d" % index)
    sink.emit(
        "dispatch_started",
        message_id="m1",
        turn_id="t9",
        cli="claude",
        cli_session="fresh",
        message_at=None,
        path="C:/SENTINEL-FILE",
    )
    disposed(sink, mid="m1")
    sink.close()
    blob = all_bytes(root)
    for secret in (
        b"SENTINEL-ENV-VALUE",
        b"SENTINEL-EXC-TEXT",
        b"SENTINEL-PATH-DIR",
        b"SENTINEL-FILE",
        str(tmp_path).encode(),
    ):
        assert secret not in blob
    assert os.getcwd().encode() not in blob


def test_event_fields_are_closed_words_numbers_flags_ids_and_times_only(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    ended(sink)
    disposed(sink)
    sink.close()
    for record in read_everything(agent_dir(tmp_path)):
        for key, value in record.items():
            assert isinstance(value, (str, int, bool, type(None), list, dict)), key
            if isinstance(value, str) and key not in ("at", "message_at", "started_at"):
                assert len(value) <= 128 and value.isascii(), key


# --- where the files live -----------------------------------------------------------------------


def test_the_default_folder_sits_beside_the_wrapper_logs_and_reuses_their_project_name(tmp_path):
    from agenttalk import wrapper_logs

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    logs = wrapper_logs.default_wrapper_log_root(checkout, platform="posix", environ=env)
    root = te.default_turn_events_root(checkout, platform="posix", environ=env)
    assert root.name == logs.name and len(root.name) == 64
    assert root.parent == logs.parent.parent / "turn-events"
    assert checkout.resolve() not in root.resolve().parents
    assert str(checkout) not in root.name


def test_the_override_replaces_the_folder_but_only_when_it_is_absolute(tmp_path):
    target = tmp_path / "elsewhere"
    env = {te.ENV_TURN_EVENTS_DIR: str(target), "XDG_STATE_HOME": str(tmp_path / "state")}
    assert te.default_turn_events_root(tmp_path, platform="posix", environ=env) == target
    relative = dict(env, **{te.ENV_TURN_EVENTS_DIR: "relative/dir"})
    assert te.default_turn_events_root(tmp_path, platform="posix", environ=relative).parent.name == "turn-events"
    assert te.ENV_TURN_EVENTS_DIR == "AGENTTALK_TURN_EVENTS_DIR"


def test_the_published_constants_have_the_agreed_defaults():
    assert te.TURN_EVENTS_QUEUE_MAX == 1024
    assert te.TURN_EVENTS_START_SECONDS == 2.0 and te.TURN_EVENTS_CLOSE_SECONDS == 2.0
    assert te.TURN_EVENTS_MAX_BYTES == 64 * 1024 * 1024 and te.TURN_EVENTS_SEGMENT_BYTES == 1024 * 1024
    assert copy.deepcopy(te.SCHEMA_VERSION) == 1


# --- fix round 1: after a deadline, the writer's own fields and records ---------------


def test_f1_after_the_close_deadline_a_blocked_segment_open_starts_no_new_write(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    state = {"opens": 0, "late_writes": [], "cancelled": False}

    def opening(_path):
        state["opens"] += 1
        if state["opens"] == 2:  # the rotation's new segment
            entered.set()
            assert unblock.wait(10)

    def writing(_handle, data):
        if state["cancelled"]:
            state["late_writes"].append(json.loads(data)["kind"])

    files.hooks["open_append"] = opening
    files.hooks["write"] = writing
    sink = make(tmp_path, files=files, segment_bytes=300, close_seconds=0.05)
    try:
        assert sink.start()
        started(sink, turn="a")
        started(sink, turn="b")
        assert entered.wait(5)
        sink.close()
        assert sink.off_reason == te.OFF_CLOSE_TIMEOUT
        state["cancelled"] = True
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
    assert state["late_writes"] == []
    kinds = [r["kind"] for r in read_everything(agent_dir(tmp_path))]
    assert "stream_closed" not in kinds and kinds.count("stream_started") == 1


def test_f1_after_a_failed_write_a_blocked_open_at_close_writes_no_closing_record(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    state = {"events": 0, "opens": 0}

    def writing(_handle, data):
        if b'"seq"' in data:
            state["events"] += 1
            if state["events"] == 1:
                raise OSError("first event fails")

    def opening(_path):
        state["opens"] += 1
        if state["opens"] == 2:  # the segment the closing record would need
            entered.set()
            assert unblock.wait(10)

    files.hooks["write"] = writing
    files.hooks["open_append"] = opening
    # A deadline long enough that the writer reaches the open first; close's own wait begins
    # once the writer is held there, so close times out with the open provably in progress.
    sink = make(tmp_path, files=files, close_seconds=5.0)
    assert sink.start()
    writer = sink._thread  # noqa: SLF001
    sink._thread = _JoinedFrom(writer, entered)  # noqa: SLF001
    try:
        started(sink)
        sink.close()
        assert entered.is_set() and sink.off_reason == te.OFF_CLOSE_TIMEOUT
    finally:
        unblock.set()
        writer.join(5)
    kinds = [r["kind"] for r in read_everything(agent_dir(tmp_path))]
    assert kinds == ["stream_started"]  # no second header, no closing record


def test_f1_after_the_start_bound_a_blocked_first_open_writes_no_header_only_the_timeout_status(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()

    def opening(_path):
        entered.set()
        assert unblock.wait(10)

    files.hooks["open_append"] = opening
    sink = make(tmp_path, files=files, start_seconds=0.05)
    start_bound_counted_from(sink, entered)
    try:
        assert not sink.start()
        assert entered.is_set()
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
        sink.close()
    assert read_everything(agent_dir(tmp_path)) == []
    status = status_of(sink)
    assert status["state"] == "off" and status["off_reason"] == "start_timeout"
    for _generation, _number, path in te.list_segments(agent_dir(tmp_path)):
        assert path.read_bytes() == b""  # the open that was already running created it; nothing was written


def test_f1_no_removal_starts_after_a_cancellation(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    removed: list[str] = []
    state = {"stat": 0}

    def stat_hook(*_a):
        pass

    def remove(path):
        removed.append(path)

    original_stat = files.stat

    def slow_stat(path):
        state["stat"] += 1
        if state["stat"] == 3:  # while the cap is looking at its second segment
            entered.set()
            assert unblock.wait(10)
        return original_stat(path)

    files.stat = slow_stat
    files.hooks["remove"] = remove
    sink = make(tmp_path, files=files, segment_bytes=300, max_bytes=1, close_seconds=0.05)
    try:
        assert sink.start()
        for index in range(6):
            started(sink, turn="t%d" % index)
        assert entered.wait(5)
        sink.close()
        before = len(removed)
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
    assert len(removed) == before


def test_f2_a_torn_registry_with_no_earlier_record_does_not_swallow_the_next_registration(tmp_path):
    directory = agent_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "streams.jsonl").write_bytes(b'{"stream":"alpha.old')
    sink = make(tmp_path)
    assert sink.start()
    started(sink)
    sink.close()
    records, damaged = te.read_streams_checked(directory)
    assert [r["stream"] for r in records] == [sink.stream] and damaged == 1
    assert [r["stream"] for r in te.read_streams(directory)] == [sink.stream]


def test_f2_a_torn_registry_after_valid_records_keeps_them_and_the_new_one(tmp_path):
    first = run_clean(tmp_path)
    first.close()
    directory = agent_dir(tmp_path)
    with open(directory / "streams.jsonl", "ab") as handle:
        handle.write(b'{"stream":"alpha.torn')
    second = make(tmp_path)
    assert second.start()
    second.close()
    records, damaged = te.read_streams_checked(directory)
    assert [r["stream"] for r in records] == [first.stream, second.stream] and damaged == 1
    assert records[1]["previous_stream"] == first.stream


def test_f2_registration_is_acknowledged_only_if_the_record_reads_back(tmp_path):
    class Lossy(te.JournalFiles):
        def append_line(self, path, data):
            pass  # claims success and writes nothing

    sink = make(tmp_path, files=Lossy())
    assert sink.start() is False and sink.off_reason == te.OFF_START_FAILED
    sink.close()


def test_f3_the_caller_cannot_set_the_writers_fields(tmp_path):
    sink = run_clean(tmp_path)
    for forged in (
        {"seq": 500},
        {"dropped_total": 12},
        {"stream": "alpha.forged"},
        {"agent": "other"},
        {"event_id": "x"},
        {"v": 2},
        {"at": "2000-01-01T00:00:00.000Z"},
    ):
        sink.emit(
            "dispatch_started",
            message_id="m1",
            turn_id="t1",
            cli="claude",
            cli_session="fresh",
            message_at=None,
            **forged,
        )
    sink.emit("dispatch_started", message_id="m2", turn_id="t2", cli="claude", cli_session="fresh", message_at=None)
    sink.close()
    events = events_only(read_everything(agent_dir(tmp_path)))
    assert [(e["message_id"], e["seq"]) for e in events] == [("m2", 8)]
    assert events[0]["stream"] == sink.stream and events[0]["dropped_total"] == 7
    status = status_of(sink)
    assert status["counts"]["invalid"] == 7 and status["counts"]["written"] == 1


def test_f3_a_stream_record_kind_cannot_be_emitted(tmp_path):
    sink = run_clean(tmp_path)
    sink.emit("stream_closed", last_seq=1)
    sink.emit("stream_started", segment=1)
    sink.close()
    kinds = [r["kind"] for r in read_everything(agent_dir(tmp_path))]
    assert kinds == ["stream_started", "stream_closed"]  # only the writer's own records


def test_f4_the_event_is_copied_at_hand_off(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    calls = {"n": 0}

    def status(_path, _data):
        calls["n"] += 1
        if calls["n"] >= 2:
            entered.set()
            assert unblock.wait(10)

    files.hooks["write_atomic"] = status
    sink = make(tmp_path, files=files, status_seconds=0.05)
    try:
        assert sink.start()
        assert entered.wait(5)  # the writer is busy elsewhere while the event waits in the queue
        usage = dict(USAGE)
        ended(sink, usage=usage)
        usage["input_tokens"] = 99999
        usage["extra"] = "later"
    finally:
        unblock.set()
        sink.close()
    event = events_only(read_everything(agent_dir(tmp_path)))[0]
    assert event["usage"] == USAGE


def test_f4_a_mutable_or_odd_value_is_refused_not_kept_by_reference(tmp_path):
    sink = run_clean(tmp_path)
    sink.emit("dispatch_started", message_id=["m1"], turn_id="t1", cli="claude", cli_session="fresh", message_at=None)
    ended(sink, usage={**USAGE, "input_tokens": [1]})
    ended(sink, usage={"input_tokens": 1})
    sink.close()
    assert events_only(read_everything(agent_dir(tmp_path))) == []
    assert status_of(sink)["counts"]["invalid"] == 3


@pytest.mark.parametrize(
    "kw",
    [
        {"mode": "SENTINEL-PRIVATE-MODE"},
        {"unmanaged": ("SENTINEL-PRIVATE-UNMANAGED",)},
        {"agent_version": "SENTINEL/private/path"},
        {"agent_version": "x" * 41},
        {"agent_version": "café"},
    ],
)
def test_f5_an_invalid_startup_value_is_never_written_anywhere(tmp_path, kw):
    sink = make(tmp_path, **kw)
    assert sink.start() is False and sink.off_reason == te.OFF_START_FAILED
    started(sink)
    sink.close()
    blob = all_bytes(tmp_path / "journal") if (tmp_path / "journal").exists() else b""
    assert b"SENTINEL" not in blob and "café".encode() not in blob


def test_f5_a_valid_startup_value_still_starts(tmp_path):
    sink = make(tmp_path, agent_version="0.96.0+dev.1", unmanaged=("cadence",))
    assert sink.start() is True
    sink.close()
    header = read_everything(agent_dir(tmp_path))[0]
    assert header["agent_version"] == "0.96.0+dev.1" and header["unmanaged"] == ["cadence"]


@pytest.mark.parametrize("bad_kind", [[], {"a": 1}, 7, None, ["dispatch_started"]])
def test_f6_a_kind_that_is_not_text_is_a_closed_error(bad_kind):
    with pytest.raises(te.TurnEventError):
        te.validate_event(valid_event("dispatch_started") | {"kind": bad_kind})


def test_f6_a_wrongly_typed_kind_is_a_damaged_line_and_later_records_are_read(tmp_path):
    sink = run_clean(tmp_path)
    started(sink)
    sink.close()
    path = te.list_segments(agent_dir(tmp_path))[0][2]
    good = path.read_bytes()
    bad = [te.encode_line(valid_event("dispatch_started") | {"kind": k}) for k in ([], {"a": 1})]
    later = te.encode_line(valid_event("dispatch_started", stream=sink.stream, seq=9))
    path.write_bytes(good + bad[0] + bad[1] + later)
    read = te.read_segment(path)
    assert read.damaged == 2 and read.records[-1]["seq"] == 9


def test_f6_a_deeply_nested_line_is_damage_not_a_crash(tmp_path):
    with pytest.raises(te.TurnEventError):
        te.parse_line(b"[" * 1500 + b"0" + b"]" * 1500)
    sink = run_clean(tmp_path)
    started(sink)
    sink.close()
    path = te.list_segments(agent_dir(tmp_path))[0][2]
    path.write_bytes(path.read_bytes() + b"[" * 1500 + b"0" + b"]" * 1500 + b"\n")
    assert te.read_segment(path).damaged == 1


def _sync_fault_status(tmp_path, *, fail_on, **kw):
    files = Hooked()
    state = {"n": 0}

    def syncing(_handle):
        state["n"] += 1
        if fail_on(state["n"]):
            raise OSError("SENTINEL-EXC-SYNC")

    files.hooks["sync"] = syncing
    sink = make(tmp_path, files=files, **kw)
    assert sink.start()
    return sink, state


def test_f7_a_failed_sync_during_rotation_is_a_counted_fault_and_stays_unsynced(tmp_path):
    # sync calls: 1 = segment 1's header, 2 = the rotation of segment 1
    sink, _state = _sync_fault_status(tmp_path, fail_on=lambda n: n == 2, segment_bytes=300, sync_seconds=1e12)
    started(sink, turn="first")
    started(sink, turn="second")
    started(sink, turn="third")
    sink.close()
    status = status_of(sink)
    assert status["faults"].get("write_failed") == 1 and status["counts"]["sync_failures"] == 1
    assert status["counts"]["write_failures"] == 0 and status["counts"]["dropped"] == 0
    assert len(events_only(read_everything(agent_dir(tmp_path)))) == 3  # nothing was lost, only unproven durable
    assert b"SENTINEL-EXC-SYNC" not in all_bytes(agent_dir(tmp_path))


def test_f8_a_failed_rotation_sync_is_never_retried_and_its_fault_stays_visible(tmp_path):
    """The failed segment is followed BY NAME: no later sync touches it, and its fault stays
    in the status record; its events are still readable and are not counted as lost."""
    files = Hooked()
    synced: list[str] = []
    state = {"failed": None}

    def sync(handle):
        name = os.path.basename(handle.name)
        synced.append(name)
        if state["failed"] is None and len(synced) == 2:  # call 1: segment 1's header; 2: its rotation
            state["failed"] = name
            raise OSError("SENTINEL-EXC-SYNC")

    files.hooks["sync"] = sync
    sink = make(tmp_path, files=files, segment_bytes=300, sync_seconds=0.0, status_seconds=0.05)
    assert sink.start()
    for turn in ("first", "second", "third"):
        started(sink, turn=turn)
    time.sleep(0.5)  # many periodic passes
    sink.close()
    failed = state["failed"]
    assert failed is not None and failed.endswith("-1.jsonl")
    assert synced.count(failed) == 2  # its header sync and the one that failed; nothing after
    status = status_of(sink)
    assert status["faults"]["write_failed"] == 1 and status["counts"]["sync_failures"] == 1
    assert status["last_fault"] == "write_failed" and status["counts"]["dropped"] == 0
    assert len(events_only(read_everything(agent_dir(tmp_path)))) == 3


# --- fix round 2: nothing new starts once cancellation is latched, enforced at the seam ---------------


def _latched(tmp_path, **kw):
    files = Hooked()
    sink = make(tmp_path, files=files, **kw)
    assert sink.start()
    files.calls.clear()
    sink._cancel.set()  # noqa: SLF001 - what a latch does
    return sink, files


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("makedirs", ("x",)),
        ("open_append", ("x",)),
        ("write", (object(), b"x")),
        ("sync", (object(),)),
        ("append_line", ("x", b"x")),
        ("read_bytes", ("x",)),
        ("write_atomic", ("x", b"x")),
        ("listdir", ("x",)),
        ("stat", ("x",)),
        ("remove", ("x",)),
    ],
)
def test_f1_every_io_operation_refuses_to_start_once_cancellation_is_latched(tmp_path, op, args):
    # Path arguments point into tmp_path, so a broken gate can only ever
    # touch this test's own folder, never the working directory.
    args = tuple(str(tmp_path / a) if a == "x" else a for a in args)
    sink, files = _latched(tmp_path)
    try:
        with pytest.raises(te._Cancelled):  # noqa: SLF001
            getattr(sink._files, op)(*args)  # noqa: SLF001
        assert op not in files.calls  # the seam was never reached
    finally:
        sink.close()


def test_f1_letting_go_of_a_handle_is_never_refused(tmp_path):
    sink, files = _latched(tmp_path)
    closed = []
    files.close = lambda handle: closed.append(handle)
    sink._files.close("handle")  # noqa: SLF001
    assert closed == ["handle"]
    sink.close()


class _AckThenHold(threading.Event):
    """The writer's acknowledgement that, once given, holds the writer until `release` is set:
    start() returns, and the writer's next step (its first status write) has not begun."""

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release

    def set(self) -> None:
        super().set()
        assert self._release.wait(10)


@pytest.mark.parametrize("first_status", ["written before the latch", "not yet begun at the latch"])
def test_f1_a_cancellation_is_not_a_fault_and_not_a_writer_error(tmp_path, first_status):
    # start() returns at the writer's acknowledgement; its first status write follows on the
    # writer's own thread. Both orders against the latch are fixed here, not left to the machine.
    sink = make(tmp_path)
    release = threading.Event()
    if first_status == "not yet begun at the latch":
        sink._ack = _AckThenHold(release)  # noqa: SLF001
    assert sink.start()
    if first_status == "written before the latch":
        give_up = time.monotonic() + 10
        while not Path(sink.status_path).exists():
            assert time.monotonic() < give_up
            time.sleep(0.005)
    sink._cancel.set()  # noqa: SLF001 - what a latch does
    release.set()
    started(sink)
    sink.close()
    sink._thread.join(5)  # noqa: SLF001
    assert sink._faults == {} and sink._last_fault is None  # noqa: SLF001
    assert sink.off_reason != te.OFF_WRITER_ERROR
    if first_status == "written before the latch":
        status = status_of(sink)
        assert status["faults"] == {} and status["off_reason"] != "writer_error"
    else:
        # no new status write begins after a cancellation, so there is no status record at all
        assert not Path(sink.status_path).exists()


def test_f1_a_cancellation_that_lands_inside_a_step_is_not_a_fault_either(tmp_path):
    # Latched while a rotation's open is in progress: the step after it is refused (the
    # writer's cancellation path), and that is still neither a fault nor a writer error.
    files = Hooked()
    entered, release = threading.Event(), threading.Event()
    opens = {"n": 0}

    def opening(_path):
        opens["n"] += 1
        if opens["n"] == 2:  # the rotation's new segment
            entered.set()
            assert release.wait(10)

    files.hooks["open_append"] = opening
    sink = make(tmp_path, files=files, segment_bytes=300)
    assert sink.start()
    started(sink, turn="a")
    started(sink, turn="b")
    assert entered.wait(10)
    sink._cancel.set()  # noqa: SLF001 - what a latch does
    release.set()
    sink._thread.join(5)  # noqa: SLF001
    sink.close()
    assert sink._faults == {} and sink._last_fault is None  # noqa: SLF001
    assert sink.off_reason != te.OFF_WRITER_ERROR


def test_f1_a_registry_read_that_returns_after_the_start_timeout_starts_no_append(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    original = files.read_bytes
    reads = {"n": 0}

    def read(path):
        reads["n"] += 1
        if reads["n"] == 2:  # the torn-tail inspection after the previous-stream lookup
            entered.set()
            assert unblock.wait(5)
        return original(path)

    files.read_bytes = read
    sink = make(tmp_path, files=files, start_seconds=0.2)
    start_bound_counted_from(sink, entered)
    try:
        assert sink.start() is False
        assert entered.is_set()
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
        sink.close()
    assert "append_line" not in files.calls


def test_f1_a_disposition_write_that_returns_after_close_timed_out_starts_no_sync(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    late: list[str] = []
    cancelled = threading.Event()

    def write(handle, data):
        if json.loads(data).get("kind") == "message_disposed":
            entered.set()
            assert unblock.wait(5)

    def sync(handle):
        if cancelled.is_set():
            late.append(handle.name)

    files.hooks.update(write=write, sync=sync)
    sink = make(tmp_path, files=files, close_seconds=0.05)
    try:
        assert sink.start()
        disposed(sink)
        assert entered.wait(5)
        sink.close()
        assert sink.off_reason == "close_timeout"
        cancelled.set()
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
    assert late == []


def test_f1_a_start_timeout_still_gets_its_status_record_while_close_has_not_timed_out(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()

    def opening(path):
        entered.set()
        assert unblock.wait(5)

    files.hooks["open_append"] = opening
    sink = make(tmp_path, files=files, start_seconds=0.05)
    start_bound_counted_from(sink, entered)
    try:
        assert not sink.start() and entered.is_set()
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
    status = status_of(sink)
    assert status["state"] == "off" and status["off_reason"] == "start_timeout"
    sink.close()


def test_f1_close_overrides_the_start_timeout_exception_once_its_deadline_passed(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    late: list[Any] = []
    deadline_passed = threading.Event()

    def opening(path):
        entered.set()
        assert unblock.wait(5)

    def status(path, data):
        if deadline_passed.is_set():
            late.append(json.loads(data))

    files.hooks.update(open_append=opening, write_atomic=status)
    sink = make(tmp_path, files=files, start_seconds=0.05, close_seconds=0.05)
    start_bound_counted_from(sink, entered)
    try:
        assert not sink.start() and entered.is_set()
        sink.close()
        until_close_deadline_passed(sink)  # the writer is still held in the open
        deadline_passed.set()
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
    assert late == []
    assert not Path(sink.status_path).exists()


def test_f1_a_held_status_write_does_not_begin_later_cap_or_status_work(tmp_path):
    files = Hooked()
    entered, unblock = threading.Event(), threading.Event()
    statuses: list[str] = []
    removals: list[str] = []

    def status(path, data):
        statuses.append(path)
        if len(statuses) == 1:
            entered.set()
            assert unblock.wait(5)

    files.hooks.update(write_atomic=status, remove=lambda p: removals.append(p))
    sink = make(tmp_path, files=files, close_seconds=0.05)
    directory = Path(sink.status_path).parent
    directory.mkdir(parents=True)
    for i in range(6):
        (directory / ("status-old-%d.json" % i)).write_text("{}")
    try:
        assert sink.start() and entered.wait(5)
        sink.close()
    finally:
        unblock.set()
        sink._thread.join(5)  # noqa: SLF001
    assert len(statuses) == 1 and removals == []


def test_f7_an_event_write_failure_and_a_periodic_sync_failure_are_counted_apart(tmp_path):
    sink, _state = _sync_fault_status(tmp_path, fail_on=lambda n: n == 2, sync_seconds=0.0, status_seconds=0.05)
    started(sink, turn="a")
    time.sleep(0.4)  # the periodic sync (call 2) fails
    started(sink, turn="b")
    sink.close()
    status = status_of(sink)
    assert status["counts"]["sync_failures"] == 1 and status["faults"].get("write_failed") == 1
