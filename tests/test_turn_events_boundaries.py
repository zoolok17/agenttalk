"""Turn journal: the three boundaries found in the cold read of the journal patch.

1. A reader that stops early resumes at the first record it has not received.
2. A message time that is not a real time never reaches a file (it becomes null).
3. Building and starting the journal never holds the wrapper past the start deadline,
   even while finding the default folder is blocked.

The first test of each group is the reviewer's own reproduction, kept as it was
written; the third one is adapted to the fix (finding the folder now happens when
the journal starts, so the test builds AND starts it)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli
from agenttalk import turn_events as te
from agenttalk.store import Message

PRIVATE = "C:/private/SYNTHETIC-SECRET.txt"


@pytest.fixture(autouse=True)
def _journal_root_in_tmp(tmp_path, monkeypatch):
    """No test here may write into the real per-user folder."""
    monkeypatch.setenv(te.ENV_TURN_EVENTS_DIR, str(tmp_path / "journal-root"))
    monkeypatch.delenv(te.ENV_TURN_EVENTS, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def make_journal(root):
    sink = te.TurnEventSink(root, "beta", start_seconds=5, close_seconds=5)
    assert sink.start()
    return sink


def _kinds(directory, cursor=None):
    return [item[2]["kind"] for item in te.iter_records(directory, cursor)]


# --- 1. the reader's cursor follows delivery ---------------------------------


def test_partial_reader_must_not_skip_unyielded_records(tmp_path):
    sink = make_journal(tmp_path / "journal")
    sink.emit("dispatch_started", message_id="m1", turn_id="t1", cli="claude",
              cli_session="fresh", message_at=None)
    sink.close()
    directory = tmp_path / "journal" / "beta"
    cursor = {}
    iterator = te.iter_records(directory, cursor)
    assert next(iterator)[2]["kind"] == "stream_started"
    iterator.close()
    remaining = [item[2]["kind"] for item in te.iter_records(directory, cursor)]
    assert remaining == ["dispatch_started", "stream_closed"], remaining


def test_reading_one_record_at_a_time_misses_nothing_and_repeats_nothing(tmp_path):
    sink = make_journal(tmp_path / "journal")
    for n in range(5):
        sink.emit("dispatch_started", message_id="m%d" % n, turn_id="t%d" % n, cli="claude",
                  cli_session="fresh", message_at=None)
    sink.close()
    directory = tmp_path / "journal" / "beta"
    everything = _kinds(directory)
    cursor: dict = {}
    seen = []
    while True:
        iterator = te.iter_records(directory, cursor)
        item = next(iterator, None)
        iterator.close()
        if item is None:
            break
        seen.append(item[2]["kind"])
    assert seen == everything
    assert everything[0] == "stream_started" and everything[-1] == "stream_closed"
    assert _kinds(directory, cursor) == []


def test_a_full_read_leaves_the_cursor_after_the_last_complete_line(tmp_path):
    sink = make_journal(tmp_path / "journal")
    sink.close()
    directory = tmp_path / "journal" / "beta"
    cursor: dict = {}
    _kinds(directory, cursor)
    [(generation, number, path)] = te.list_segments(directory)
    assert cursor[(generation, number)] == path.stat().st_size


# --- 2. only a real time is kept as message_at --------------------------------


def test_bus_accepted_timestamp_cannot_copy_private_path_to_journal(tmp_path, monkeypatch):
    from test_turn_events_hooks import _claude_turn, _record, _store

    from agenttalk.wrapper import run, session

    marker = PRIVATE
    record = _record("20261003-000000-000000-Ab12")
    record["ts"] = marker
    message = Message.from_raw(record)
    message.validate(["alpha", "beta"])
    store = _store(tmp_path / "project")
    sink = make_journal(tmp_path / "journal")
    drive = run.make_drive(
        store, "beta", "claude", session.SessionState(cli="claude", claude_session_id="sid-1"),
        ["claude"], spawn=lambda argv, stdin: _claude_turn(), clock=lambda: 0.0,
        render=False, turn_events=sink,
    )
    try:
        assert drive(record).ok
    finally:
        sink.close()
    records = [item[2] for item in te.iter_records(tmp_path / "journal" / "beta")]
    starts = [r for r in records if r["kind"] == "dispatch_started"]
    assert starts and starts[0]["message_at"] != marker, starts
    assert starts[0]["message_at"] is None


@pytest.mark.parametrize("value", [
    PRIVATE,
    "beta",
    "",
    "2026-10-03T00:00:00",            # a time without a zone is not one
    "2026-10-03T00:00:00.000Z" + "0" * 20,  # longer than any real time
    "2026-10-03T00:00:00.000\u00a0Z",  # not ASCII
])
def test_a_message_time_that_is_not_a_real_time_is_written_as_null(tmp_path, value):
    sink = make_journal(tmp_path / "journal")
    sink.emit("dispatch_started", message_id="m1", turn_id="t1", cli="claude",
              cli_session="fresh", message_at=value)
    sink.emit("message_disposed", message_id="m1", disposition="completed", message_at=value,
              consumed=True, landed=None, compliance_success=None, dead_lettered=False,
              terminal_failure=False)
    sink.close()
    records = [item[2] for item in te.iter_records(tmp_path / "journal" / "beta")]
    by_kind = {r["kind"]: r for r in records}
    assert by_kind["dispatch_started"]["message_at"] is None
    assert by_kind["message_disposed"]["message_at"] is None
    raw = b"".join(path.read_bytes() for _g, _n, path in te.list_segments(tmp_path / "journal" / "beta"))
    assert b"SYNTHETIC-SECRET" not in raw


def test_a_real_message_time_is_kept_exactly(tmp_path):
    sent = "2026-10-03T04:53:20.686688Z"
    sink = make_journal(tmp_path / "journal")
    sink.emit("dispatch_started", message_id="m1", turn_id="t1", cli="claude",
              cli_session="fresh", message_at=sent)
    sink.close()
    records = [item[2] for item in te.iter_records(tmp_path / "journal" / "beta")]
    [start] = [r for r in records if r["kind"] == "dispatch_started"]
    assert start["message_at"] == sent


def test_the_reader_counts_a_record_with_a_non_time_message_at_as_damage(tmp_path):
    sink = make_journal(tmp_path / "journal")
    sink.emit("dispatch_started", message_id="m1", turn_id="t1", cli="claude",
              cli_session="fresh", message_at="2026-10-03T00:00:00.000Z")
    sink.close()
    [(_g, _n, path)] = te.list_segments(tmp_path / "journal" / "beta")
    good = path.read_bytes()
    path.write_bytes(good.replace(b'"2026-10-03T00:00:00.000Z"', b'"beta-not-a-time"'))
    read = te.read_segment(path)
    assert read.damaged == 1
    assert [r["kind"] for r in read.records] == ["stream_started", "stream_closed"]


# --- 3. building and starting stay within the start deadline -------------------


def _blocked_resolve(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_resolve = Path.resolve
    threads = []

    def blocked_resolve(path, *args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", blocked_resolve)
    return entered, release, threads


def test_optional_journal_build_cannot_block_before_start_deadline(tmp_path, monkeypatch):
    monkeypatch.delenv(te.ENV_TURN_EVENTS_DIR, raising=False)
    entered, release, threads = _blocked_resolve(monkeypatch)
    finished = threading.Event()
    outcome = {}

    def build_and_start():
        try:
            sink = cli._build_turn_journal(SimpleNamespace(root=tmp_path / "project"), "beta", lead_loop=False)
            outcome["built_without_resolving"] = not entered.is_set()
            outcome["started"] = sink.start()
            outcome["sink"] = sink
            outcome["ident"] = threading.get_ident()
        finally:
            finished.set()

    caller = threading.Thread(target=build_and_start, daemon=True)
    began = time.monotonic()
    caller.start()
    try:
        assert entered.wait(1)
        assert finished.wait(te.TURN_EVENTS_START_SECONDS + 0.2)
    finally:
        release.set()
        caller.join(5)
    assert time.monotonic() - began < te.TURN_EVENTS_START_SECONDS + 1.0
    assert outcome["built_without_resolving"] is True
    assert outcome["started"] is False
    assert outcome["sink"].off_reason == te.OFF_START_TIMEOUT
    assert outcome["ident"] not in threads  # the folder was looked for on the writer thread only
    outcome["sink"].close(timeout=5)


def test_building_the_journal_does_no_file_system_lookup(tmp_path, monkeypatch):
    monkeypatch.delenv(te.ENV_TURN_EVENTS_DIR, raising=False)
    entered, release, _threads = _blocked_resolve(monkeypatch)
    try:
        sink = cli._build_turn_journal(SimpleNamespace(root=tmp_path / "project"), "beta", lead_loop=False)
        assert sink is not None
        assert not entered.is_set()
    finally:
        release.set()


def test_the_journal_still_uses_the_default_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(te.ENV_TURN_EVENTS_DIR, raising=False)
    project = tmp_path / "project"
    project.mkdir()
    sink = cli._build_turn_journal(SimpleNamespace(root=project), "beta", lead_loop=False)
    try:
        assert sink.start()
        assert sink.directory == str(te.default_turn_events_root(project))
        assert Path(sink.status_path).parent == te.default_turn_events_root(project) / "beta"
    finally:
        sink.close()


def test_a_folder_function_that_fails_switches_the_journal_off_quietly(tmp_path):
    def broken():
        raise OSError("no disk")

    sink = te.TurnEventSink(broken, "beta", start_seconds=5, close_seconds=5)
    assert sink.start() is False
    assert sink.off_reason == te.OFF_START_FAILED
    assert sink.status_path == ""
    sink.emit("dispatch_started", message_id="m1", turn_id="t1", cli="claude",
              cli_session="fresh", message_at=None)
    sink.close()
