"""The turn journal's hooks in the wrapper: dispatch events, the switch, start
failure, non-interference, privacy and the OFF proof.

Everything runs with fake spawners and fixture stores: no model CLI, no network.
The message-ending hooks (every way the loop consumes a message) are in
tests/test_turn_events_consume.py and the status labels in
tests/test_turn_events_status.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess  # noqa: S404 - a real child process, to prove the launch mark
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agenttalk import cli
from agenttalk import turn_events as te
from agenttalk import wrapper_runtime as wr_module
from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper_logs import WrapperLifecycleLog

SENT_AT = "2026-10-03T00:00:00.000Z"
USAGE_LINE = {
    "input_tokens": 100,
    "output_tokens": 7,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 20,
}


@pytest.fixture(autouse=True)
def _journal_root_in_tmp(tmp_path, monkeypatch):
    """No test here may write into the real per-user folder."""
    monkeypatch.setenv(te.ENV_TURN_EVENTS_DIR, str(tmp_path / "journal-root"))
    monkeypatch.delenv(te.ENV_TURN_EVENTS, raising=False)


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    return store


def _record(message_id: str = "m-1", **over: Any) -> dict:
    record = {
        "id": message_id,
        "ts": SENT_AT,
        "from": "alpha",
        "to": "beta",
        "kind": "question",
        "subject": "s",
        "body": "do the work",
        "correlation_id": "q-1",
        "request_id": "q-1",
        "broadcast_id": None,
        "meta": {"request_id": "q-1"},
    }
    record.update(over)
    return record


def _claude_turn(usage: dict | None = USAGE_LINE, *, error: bool = False) -> list[str]:
    result: dict[str, Any] = {
        "type": "result",
        "is_error": error,
        "result": "boom" if error else "done",
        "num_turns": 1,
    }
    if usage is not None:
        result["usage"] = usage
    lines = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"role": "assistant"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        result,
    ]
    return [json.dumps(line) for line in lines]


def _codex_turn() -> list[str]:
    return [
        json.dumps(line)
        for line in (
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed"},
        )
    ]


class _Sink:
    """A stand-in for the journal that keeps what it was given."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, dict(fields)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def of(self, kind: str) -> list[dict]:
        return [fields for k, fields in self.events if k == kind]


def _valid(kind: str, fields: dict) -> None:
    """Every field set the hooks produce is a valid record of the closed schema."""
    record = {
        "v": 1,
        "kind": kind,
        "event_id": "00000000-0000-4000-8000-000000000000",
        "stream": "beta.g1",
        "at": SENT_AT,
        "agent": "beta",
        "dropped_total": 0,
        "seq": 1,
        **fields,
    }
    te.validate_event(record)


def _drive(tmp_path, sink, spawn, *, cli_name="claude", state=None, **kw):
    store = _store(tmp_path)
    state = state or (
        session.SessionState(cli=cli_name, claude_session_id="sid-1")
        if cli_name == "claude"
        else session.SessionState(cli=cli_name)
    )
    drive = run.make_drive(
        store,
        "beta",
        cli_name,
        state,
        [cli_name],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
        turn_events=sink,
        **kw,
    )
    return store, drive


# --- the five exits of one dispatch (E1-E5) ------------------------------------


def test_a_successful_claude_turn_is_journaled_with_its_usage(tmp_path):
    sink = _Sink()
    _store_, drive = _drive(tmp_path, sink, lambda argv, stdin: _claude_turn())
    assert drive(_record()).ok is True
    assert sink.kinds() == ["dispatch_started", "dispatch_ended"]
    started, ended = sink.of("dispatch_started")[0], sink.of("dispatch_ended")[0]
    assert started["message_id"] == "m-1" and started["turn_id"].startswith("turn-")
    assert (started["cli"], started["cli_session"], started["message_at"]) == ("claude", "fresh", SENT_AT)
    assert ended["turn_id"] == started["turn_id"] and ended["message_id"] == "m-1"
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (True, "success", "normal")
    assert "failure_class" not in ended
    assert ended["usage"] == {
        "input_tokens": 100,
        "output_tokens": 7,
        "cache_read_tokens": 30,
        "cache_write_tokens": 20,
    }
    assert isinstance(ended["duration_ms"], int) and ended["duration_ms"] >= 0
    _valid("dispatch_started", started)
    _valid("dispatch_ended", ended)


def test_a_resumed_turn_says_resume(tmp_path):
    sink = _Sink()
    state = session.SessionState(cli="claude", claude_session_id="sess-1", turns=1)
    _s, drive = _drive(tmp_path, sink, lambda argv, stdin: _claude_turn(), state=state)
    assert drive(_record()).ok is True
    assert sink.of("dispatch_started")[0]["cli_session"] == "resume"


def test_missing_usage_is_unknown_never_zero(tmp_path):
    sink = _Sink()
    _s, drive = _drive(tmp_path, sink, lambda argv, stdin: _claude_turn(usage=None))
    drive(_record())
    assert sink.of("dispatch_ended")[0]["usage"] is None
    sink = _Sink()
    partial = {"input_tokens": 5, "output_tokens": -1, "cache_read_input_tokens": "x"}
    _s, drive = _drive(tmp_path / "again", sink, lambda argv, stdin: _claude_turn(usage=partial))
    drive(_record())
    assert sink.of("dispatch_ended")[0]["usage"] == {
        "input_tokens": 5,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }


def test_a_codex_turn_has_no_usage_in_this_version(tmp_path):
    sink = _Sink()
    _s, drive = _drive(tmp_path, sink, lambda argv, stdin: _codex_turn(), cli_name="codex")
    assert drive(_record()).ok is True
    started, ended = sink.of("dispatch_started")[0], sink.of("dispatch_ended")[0]
    assert started["cli"] == "codex" and ended["usage"] is None and ended["outcome"] == "success"


def test_a_failed_turn_is_launched_and_failed_with_a_closed_class(tmp_path):
    sink = _Sink()
    _s, drive = _drive(tmp_path, sink, lambda argv, stdin: _claude_turn(error=True))
    assert drive(_record()).ok is False
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (True, "failed", "normal")
    assert ended["failure_class"] in te.FAILURE_CLASSES
    _valid("dispatch_ended", ended)


def test_e1_a_refused_preflight_is_a_dispatch_that_never_launched(tmp_path):
    sink = _Sink()
    spawned: list[int] = []
    _s, drive = _drive(
        tmp_path,
        sink,
        lambda argv, stdin: spawned.append(1) or _claude_turn(),
        agenttalk_preflight=lambda: "agenttalk is not runnable here",
    )
    assert drive(_record()).ok is False
    ended = sink.of("dispatch_ended")[0]
    assert spawned == []
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (False, "not_launched", "normal")
    assert ended["failure_class"] == "config_blocked" and ended["usage"] is None
    _valid("dispatch_ended", ended)


@pytest.mark.parametrize(("transient", "expected"), [(True, "gateway_held"), (False, "config_blocked")])
def test_e2_a_held_or_capped_gateway_never_launches(tmp_path, transient, expected):
    sink = _Sink()

    def spawn(argv, stdin):
        raise run._GatewayChildCapUnavailable("held", transient=transient)

    _s, drive = _drive(tmp_path, sink, spawn)
    drive(_record())
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (False, "not_launched", "normal")
    assert ended["failure_class"] == expected


def test_e3_an_os_error_before_the_process_exists_is_not_launched(tmp_path):
    sink = _Sink()

    def spawn(argv, stdin):
        raise FileNotFoundError("no such program")

    _s, drive = _drive(tmp_path, sink, spawn)
    drive(_record())
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (False, "not_launched", "spawn_error")
    assert "no such program" not in json.dumps(sink.events)


def test_e3_an_os_error_while_reading_the_stream_is_a_launched_failure(tmp_path):
    sink = _Sink()

    def spawn(argv, stdin):
        def lines():
            yield _claude_turn()[0]
            raise OSError("pipe broke")

        return lines()

    _s, drive = _drive(tmp_path, sink, spawn)
    drive(_record())
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (True, "failed", "spawn_error")


def test_e5_an_other_exception_is_re_raised_untouched_and_still_journaled(tmp_path):
    sink = _Sink()
    boom = RuntimeError("SENTINEL-EXC-RAISE")

    def spawn(argv, stdin):
        raise boom

    _s, drive = _drive(tmp_path, sink, spawn)
    with pytest.raises(RuntimeError) as caught:
        drive(_record())
    assert caught.value is boom
    assert sink.kinds() == ["dispatch_started", "dispatch_ended"]
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (False, "not_launched", "exception")
    assert ended["failure_class"] == "other"
    assert "SENTINEL-EXC-RAISE" not in json.dumps(sink.events)


def test_e5_an_exception_after_the_launch_counts_as_a_launched_failure(tmp_path):
    sink = _Sink()
    boom = ValueError("later")

    def spawn(argv, stdin):
        def lines():
            yield _claude_turn()[0]
            raise boom

        return lines()

    _s, drive = _drive(tmp_path, sink, spawn)
    with pytest.raises(ValueError) as caught:
        drive(_record())
    assert caught.value is boom
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (True, "failed", "exception")


def test_a_journal_that_raises_never_changes_the_result_or_the_exception(tmp_path):
    class Raising:
        def emit(self, kind, **fields):
            raise RuntimeError("journal broke")

    _s, drive = _drive(tmp_path, Raising(), lambda argv, stdin: _claude_turn())
    assert drive(_record()).ok is True
    boom = KeyError("original")

    def spawn(argv, stdin):
        raise boom

    _s, drive = _drive(tmp_path / "x", Raising(), spawn)
    with pytest.raises(KeyError) as caught:
        drive(_record())
    assert caught.value is boom


# --- D2: a launch counts the moment the process exists ---------------------------


def _real_stream(**kw):
    marks: list[int] = []
    return marks, lambda: marks.append(1), kw


def test_d2_the_launch_is_marked_even_when_the_spawn_callback_raises():
    marks, mark, _ = _real_stream()
    boom = RuntimeError("spawn callback broke")

    def on_spawn(pid, start):
        raise boom

    with pytest.raises(RuntimeError) as caught:
        run._ProcStream([sys.executable, "-c", "pass"], None, on_spawn=on_spawn, on_launch=mark)
    assert caught.value is boom and marks == [1]


def test_d2_the_launch_is_marked_even_when_the_stdin_write_raises():
    marks, mark, _ = _real_stream()
    with pytest.raises(TypeError):
        run._ProcStream([sys.executable, "-c", "pass"], 12345, on_launch=mark)  # a prompt must be text
    assert marks == [1]


def test_d2_a_launch_mark_that_raises_cannot_disturb_the_launch():
    def bad():
        raise RuntimeError("mark broke")

    stream = run._ProcStream([sys.executable, "-c", "print('ok')"], None, on_launch=bad)
    assert "".join(stream).strip() == "ok"


def test_d2_a_real_spawner_failure_after_launch_is_a_launched_dispatch(tmp_path):
    class FakeRuntime:
        def starting(self, **kw):
            return {}

        def active(self, pid, start):
            raise RuntimeError("runtime record write failed")

        def terminal(self, outcome):
            return {}

        def progress(self):
            return {}

        def idle(self):
            return {}

        def launcher_exited(self, *a, **k):
            return {}

    store = _store(tmp_path)
    sink = _Sink()
    drive = run.make_drive(
        store,
        "beta",
        "claude",
        session.SessionState(cli="claude", claude_session_id="sid-1"),
        [sys.executable, "-c", "pass"],
        render=False,
        clock=lambda: 0.0,
        runtime_writer=FakeRuntime(),
        turn_events=sink,
    )
    with pytest.raises(RuntimeError, match="runtime record write failed"):
        drive(_record())
    ended = sink.of("dispatch_ended")[0]
    assert (ended["launched"], ended["exit"]) == (True, "exception")


# --- identity ------------------------------------------------------------------------


def test_a_hold_between_two_dispatches_still_gives_two_distinct_dispatches(tmp_path):
    store = _store(tmp_path)
    store.send(sender="alpha", recipient="beta", body="work", meta={"request_id": "q-1"})
    sink = _Sink()
    spawns = {"n": 0}

    def spawn(argv, stdin):
        spawns["n"] += 1
        if spawns["n"] == 1:
            raise run._GatewayChildCapUnavailable("held", transient=True)
        return _claude_turn()

    drive = run.make_drive(
        store,
        "beta",
        "claude",
        session.SessionState(cli="claude", claude_session_id="sid-1"),
        ["claude"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
        turn_events=sink,
    )
    disposed: list[tuple[str, str]] = []
    turns = loop.run_loop(
        store,
        "beta",
        drive,
        clock=lambda: 0.0,
        sleep=lambda _d: None,
        max_polls=6,
        max_turns=1,
        on_message_disposed=lambda rec, disposition, facts: disposed.append((rec["id"], disposition)),
    )
    assert turns == 1 and len(disposed) == 1 and disposed[0][1] == "completed"
    started = sink.of("dispatch_started")
    ended = sink.of("dispatch_ended")
    assert len(started) == 2 and len(ended) == 2
    assert started[0]["turn_id"] != started[1]["turn_id"]
    assert [e["outcome"] for e in ended] == ["not_launched", "success"]
    assert ended[0]["failure_class"] == "gateway_held"
    assert started[0]["message_id"] == started[1]["message_id"] == disposed[0][0]


# --- the switch, the wiring and the start bound --------------------------------------


def _wrap(tmp_path, monkeypatch, *, turn_events, fake_spawn=None, run_loop=None, lifecycle=None, lead_loop=False):
    store = _store(tmp_path)
    real_make = run.make_drive
    if fake_spawn is not None:
        monkeypatch.setattr(
            run,
            "make_drive",
            lambda *a, **kw: real_make(*a, spawn=fake_spawn, **{**kw, "runtime_writer": None}),
        )
    if run_loop is not None:
        monkeypatch.setattr(loop, "run_loop", run_loop)
    rc = cli._wrap_loop_mode(
        store,
        "beta",
        cli="claude",
        base_argv=["claude"],
        sender="beta",
        min_interval=0.0,
        render=False,
        turn_events=turn_events,
        lifecycle_log=lifecycle,
        lead_loop=lead_loop,
    )
    return store, rc


def _journal_dir(store: Store) -> Path:
    return te.default_turn_events_root(store.root) / "beta"


def _read(directory: Path) -> list[dict]:
    out = []
    for _g, _n, path in te.list_segments(directory):
        out.extend(te.read_segment(path).records)
    return out


def test_the_switch_is_the_flag_or_the_environment_and_off_by_default():
    assert te.turn_events_requested(False, {}) is False
    assert te.turn_events_requested(True, {}) is True
    assert te.turn_events_requested(False, {te.ENV_TURN_EVENTS: "1"}) is True
    for other in ("0", "", "true", "yes"):
        assert te.turn_events_requested(False, {te.ENV_TURN_EVENTS: other}) is False


def test_the_wrap_command_accepts_the_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["wrap", "--loop", "--turn-events", "--", "claude"])
    assert args.turn_events is True
    assert not parser.parse_args(["wrap", "--loop", "--", "claude"]).turn_events


def test_the_wired_wrapper_journals_a_turn_end_to_end(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run_loop(store, agent, drive, **kw):
        assert drive(_record()).ok is True
        kw["on_message_disposed"](
            _record(),
            "completed",
            {
                "consumed": True,
                "landed": None,
                "compliance_success": None,
                "dead_lettered": False,
                "terminal_failure": None,
            },
        )
        seen["keys"] = sorted(kw)
        return 1

    store, rc = _wrap(
        tmp_path, monkeypatch, turn_events=True, fake_spawn=lambda a, s: _claude_turn(), run_loop=fake_run_loop
    )
    assert rc == 0
    records = _read(_journal_dir(store))
    kinds = [r["kind"] for r in records]
    assert kinds == ["stream_started", "dispatch_started", "dispatch_ended", "message_disposed", "stream_closed"]
    assert records[-2]["disposition"] == "completed" and records[-2]["message_at"] == SENT_AT
    assert records[0]["unmanaged"] == []
    assert records[-1]["last_seq"] == 3 and records[-1]["dropped_total"] == 0
    assert "on_message_disposed" in seen["keys"]


def test_a_lead_loop_stream_says_cadence_turns_are_unmanaged(tmp_path, monkeypatch):
    store = _store(tmp_path)
    journal = cli._build_turn_journal(store, "beta", lead_loop=True)
    assert journal is not None and journal._unmanaged == ["cadence"]  # noqa: SLF001


def test_one_shot_never_creates_a_journal(tmp_path, monkeypatch):
    store = _store(tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(loop, "run_loop", lambda s, a, d, **kw: captured.update(kw) or 1)
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: captured.update(drive_kw=kw) or (lambda rec: True))
    cli._wrap_loop_mode(
        store,
        "beta",
        cli="claude",
        base_argv=["claude"],
        sender="beta",
        min_interval=0.0,
        render=False,
        one_shot_request_id="rq-1",
        turn_events=True,
    )
    assert captured["drive_kw"]["turn_events"] is None
    assert captured["on_message_disposed"] is None
    assert not te.default_turn_events_root(store.root).exists()


def test_d3d_a_failed_start_runs_the_first_message_on_time_and_says_start_failed(tmp_path, monkeypatch):
    def fail(self, path, data):
        raise OSError("SENTINEL-EXC-REGISTER")

    monkeypatch.setattr(te.JournalFiles, "append_line", fail)
    handled: list[float] = []
    lifecycle_stream = io.StringIO()
    lifecycle = WrapperLifecycleLog("beta", stream=lifecycle_stream, enabled=True)

    def fake_run_loop(store, agent, drive, **kw):
        begin = time.monotonic()
        assert drive(_record()).ok is True
        handled.append(time.monotonic() - begin)
        return 1

    begin = time.monotonic()
    store, rc = _wrap(
        tmp_path,
        monkeypatch,
        turn_events=True,
        fake_spawn=lambda a, s: _claude_turn(),
        run_loop=fake_run_loop,
        lifecycle=lifecycle,
    )
    assert rc == 0 and handled and time.monotonic() - begin < te.TURN_EVENTS_START_SECONDS + 5
    assert te.list_segments(_journal_dir(store)) == []
    status = next(Path(_journal_dir(store)).glob("status-*.json"))
    data = json.loads(status.read_text(encoding="ascii"))
    assert data["state"] == "off" and data["off_reason"] == "start_failed"
    # nothing about the journal goes through the wrapper's own logging
    assert "turn_journal" not in lifecycle_stream.getvalue()
    assert "SENTINEL-EXC-REGISTER" not in lifecycle_stream.getvalue() + status.read_text(encoding="ascii")


def test_the_journal_is_closed_even_when_the_loop_raises(tmp_path, monkeypatch):
    def fake_run_loop(store, agent, drive, **kw):
        raise RuntimeError("loop crashed")

    store = _store(tmp_path)
    monkeypatch.setattr(loop, "run_loop", fake_run_loop)
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: lambda rec: True)
    with pytest.raises(RuntimeError, match="loop crashed"):
        cli._wrap_loop_mode(
            store,
            "beta",
            cli="claude",
            base_argv=["claude"],
            sender="beta",
            min_interval=0.0,
            render=False,
            turn_events=True,
        )
    records = _read(_journal_dir(store))
    assert records[-1]["kind"] == "stream_closed"


# --- OFF is the code that ran before ----------------------------------------------------


def test_off_creates_no_journal_file_and_no_thread(tmp_path, monkeypatch):
    before = {t.name for t in threading.enumerate()}
    seen: dict[str, Any] = {}
    store = _store(tmp_path)
    monkeypatch.setattr(loop, "run_loop", lambda s, a, d, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: seen.update(drive=kw) or (lambda rec: True))
    cli._wrap_loop_mode(
        store, "beta", cli="claude", base_argv=["claude"], sender="beta", min_interval=0.0, render=False
    )
    assert seen["on_message_disposed"] is None and seen["drive"]["turn_events"] is None
    assert not te.default_turn_events_root(store.root).exists()
    assert {t.name for t in threading.enumerate()} - before == set()
    assert not any("turn-events" in str(p) for p in (tmp_path).rglob("*"))


def _scenario(tmp_path, *, observe: bool, sink=None):
    """One fixed loop scenario: a success, then a poison failure that dead-letters.

    Returns what the loop observably did (not ids or times)."""
    store = _store(tmp_path)
    store.send(sender="alpha", recipient="beta", body="one")
    store.send(sender="alpha", recipient="beta", body="two")
    sleeps: list[float] = []
    stamps: list[int] = []
    calls: list[str] = []

    def spawn(argv, stdin):
        calls.append("spawn")
        return _claude_turn() if len(calls) == 1 else _claude_turn(error=True)

    drive = run.make_drive(
        store,
        "beta",
        "claude",
        session.SessionState(cli="claude", claude_session_id="sid-1"),
        ["claude"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
        heartbeat=lambda: stamps.append(1),
        turn_events=sink if observe else None,
    )
    disposed: list[str] = []
    kwargs: dict[str, Any] = {}
    if observe:
        kwargs["on_message_disposed"] = lambda rec, disposition, facts: disposed.append(disposition)
    turns = loop.run_loop(
        store,
        "beta",
        drive,
        clock=lambda: 0.0,
        sleep=sleeps.append,
        max_polls=40,
        k_poison=1,
        heartbeat=lambda: stamps.append(1),
        **kwargs,
    )
    dead = (
        sorted(p.name for p in (store.state_dir.parent / "dead-letter").rglob("*") if p.is_file())
        if (store.state_dir.parent / "dead-letter").exists()
        else []
    )
    return {
        "turns": turns,
        "cursor_is_last": store.cursor("beta") == max(m.id for m in store.all_messages() if m.recipient == "beta"),
        "spawns": len(calls),
        "sleeps": sleeps,
        "stamps": len(stamps),
        "dead_letter_files": len(dead),
        "disposed": disposed,
    }


def test_the_same_loop_scenario_behaves_identically_with_the_journal_on_or_off(tmp_path):
    off = _scenario(tmp_path / "off", observe=False)
    sink = _Sink()
    on = _scenario(tmp_path / "on", observe=True, sink=sink)
    assert on.pop("disposed") == ["completed", "dead_lettered"] and off.pop("disposed") == []
    assert on == off
    assert off["turns"] == 1 and off["spawns"] >= 2 and off["dead_letter_files"] >= 1
    assert sink.kinds().count("dispatch_started") == sink.kinds().count("dispatch_ended") == off["spawns"]


def test_a_writer_that_never_returns_leaves_the_loop_exactly_as_it_was(tmp_path):
    class Blocking(te.JournalFiles):
        def __init__(self):
            self.entered = threading.Event()
            self.unblock = threading.Event()

        def write(self, handle, data):
            if b'"seq"' in data:
                self.entered.set()
                self.unblock.wait(20)
            super().write(handle, data)

    off = _scenario(tmp_path / "off", observe=False)
    files = Blocking()
    sink = te.TurnEventSink(tmp_path / "journal", "beta", files=files, close_seconds=0.3, queue_max=4)
    assert sink.start() is True
    begin = time.monotonic()
    on = _scenario(tmp_path / "on", observe=True, sink=sink)
    assert time.monotonic() - begin < 10
    assert files.entered.wait(5)  # the writer really was stuck while the loop ran
    off.pop("disposed")
    on.pop("disposed")
    assert on == off
    start = time.monotonic()
    sink.close()
    assert time.monotonic() - start < 2.0
    files.unblock.set()


# --- privacy through the real hooks -----------------------------------------------------


def test_a_poisoned_run_leaves_no_prompt_path_environment_or_exception_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTALK_TEST_SECRET", "SENTINEL-ENV-VALUE")
    root = tmp_path / "SENTINEL-PATH-DIR"
    sink = te.TurnEventSink(root, "beta")
    assert sink.start() is True
    calls = {"n": 0}

    def spawn(argv, stdin):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("SENTINEL-EXC-OS /SENTINEL/path")
        if calls["n"] == 2:
            return _claude_turn()
        raise RuntimeError("SENTINEL-EXC-OTHER")

    store = _store(tmp_path / "project")
    drive = run.make_drive(
        store,
        "beta",
        "claude",
        session.SessionState(cli="claude", claude_session_id="sid-1"),
        ["claude", "SENTINEL-ARGV"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
        turn_events=sink,
    )
    poisoned = _record(body="SENTINEL-PROMPT-TEXT", subject="SENTINEL-SUBJECT")
    drive(poisoned)
    drive(poisoned)
    with pytest.raises(RuntimeError):
        drive(poisoned)
    sink.emit(
        "message_disposed",
        message_id="m-1",
        disposition="completed",
        message_at=SENT_AT,
        consumed=True,
        landed=None,
        compliance_success=None,
        dead_lettered=False,
        terminal_failure=None,
    )
    sink.close()
    blob = b""
    for base, _dirs, names in os.walk(root):
        for name in names:
            blob += (Path(base) / name).read_bytes()
    assert blob
    for secret in (
        b"SENTINEL-ENV-VALUE",
        b"SENTINEL-PROMPT-TEXT",
        b"SENTINEL-SUBJECT",
        b"SENTINEL-EXC-OS",
        b"SENTINEL-EXC-OTHER",
        b"SENTINEL-ARGV",
        b"SENTINEL-PATH-DIR",
        b"SENTINEL/path",
    ):
        assert secret not in blob
    kinds = [json.loads(line)["kind"] for line in blob.splitlines() if line.startswith(b"{") and b'"kind"' in line]
    assert kinds.count("dispatch_started") == 3 and kinds.count("dispatch_ended") == 3


def test_a_child_process_is_the_only_thing_the_launch_tests_start():
    # A guard for this file itself: the launch tests above start the python
    # interpreter only, never a model CLI.
    out = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True, check=True)  # noqa: S603
    assert out.stdout.strip() == "1"


# --- the journal never logs through the wrapper's own logging (fix round 1, F2) ---------------


def test_a_blocked_lifecycle_log_cannot_delay_a_failed_start_into_the_loop(tmp_path, monkeypatch):
    entered, unblock, handled = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []

    class Stream(io.StringIO):
        def write(self, text):
            if '"event":"turn_journal"' in text:
                entered.set()
                unblock.wait(5)
            return super().write(text)

    class Sink:
        off_reason = "start_failed"

        def start(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(cli, "_build_turn_journal", lambda *a, **kw: Sink())
    lifecycle = WrapperLifecycleLog("beta", stream=Stream(), enabled=True)

    def work():
        try:
            _wrap(tmp_path, monkeypatch, turn_events=True, lifecycle=lifecycle,
                  run_loop=lambda *a, **kw: handled.set() or 0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert handled.wait(3), errors
        assert not entered.is_set(), "a failed journal start wrote to the wrapper's log"
    finally:
        unblock.set()
        thread.join(5)
    assert not errors


def test_a_journal_fault_never_touches_the_wrapper_log_streams(tmp_path, monkeypatch):
    touched = threading.Event()

    class Stream(io.StringIO):
        def write(self, text):
            touched.set()
            return super().write(text)

    stream = Stream()
    lifecycle = WrapperLifecycleLog("beta", stream=stream, enabled=True)
    store = _store(tmp_path / "project")
    sink = cli._build_turn_journal(store, "beta", lead_loop=False)
    sink._fault("write_failed")  # noqa: SLF001 - what a failing write calls
    sink._fault("disk_full")  # noqa: SLF001
    assert not touched.is_set() and stream.getvalue() == ""
    # and an ordinary lifecycle log is not held up by anything the journal did
    done = threading.Event()

    def ordinary():
        lifecycle.child_exited(123, None, 0)
        done.set()

    threading.Thread(target=ordinary).start()
    assert done.wait(2)


def test_the_wrapper_logging_module_is_unchanged_by_the_journal():
    source = Path(cli.__file__).with_name("wrapper_logs.py").read_text(encoding="utf-8")
    assert "turn_journal" not in source


def test_a_failed_start_is_carried_by_the_wrappers_own_health_record(tmp_path, monkeypatch):
    class Sink:
        off_reason = "start_failed"

        def start(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(cli, "_build_turn_journal", lambda *a, **kw: Sink())
    seen: dict[str, Any] = {}

    def fake_run_loop(store, agent, drive, **kw):
        # the write the wrapper already makes on its normal path
        kw["on_runtime_idle"]()
        kw["on_health_idle"]()
        seen["warnings"] = store.read_health_raw(agent)["warnings"]
        return 0

    lifecycle_stream = io.StringIO()
    store = _store(tmp_path)
    te.default_turn_events_root(store.root).mkdir(parents=True)  # the journal folder exists, no status file
    monkeypatch.setattr(loop, "run_loop", fake_run_loop)
    rc = cli._wrap_loop_mode(
        store,
        "beta",
        cli="claude",
        base_argv=["claude"],
        sender="beta",
        min_interval=0.0,
        render=False,
        turn_events=True,
        lifecycle_log=WrapperLifecycleLog("beta", stream=lifecycle_stream, enabled=True),
        lead_loop=False,
    )
    me = os.getpid()
    owner = te.start_failure_warning("start_failed", me, wr_module.process_start_token(me))
    assert rc == 0 and seen["warnings"] == [owner]  # the word names this wrapper as its owner
    assert not list(_journal_dir(store).glob("status-*.json"))
    assert cli._turn_journal_label(store, "beta", None) == "off (start_failed)"
    assert "turn_journal" not in lifecycle_stream.getvalue()
