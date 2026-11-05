"""W1 of the self-compaction pilot: the size of ONE conversation, read from Claude's stream.

The completion rule (docs/design/self-compaction-pilot-w0.md, probe (a)), the reset paths and the
views that show the reading (status, the web state, the checkpoint line)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import checkpoint, cli, web
from agenttalk import conversation_size as cs
from agenttalk.store import Store
from agenttalk.wrapper import conversation_reading as cr
from agenttalk.wrapper import session as sess

MODEL = "claude-haiku-5-5"


# --- stream fixtures (shapes taken from the W0 probe) -------------------------------------------

def _start(mid, inp=4, read=1000, write=200, model=MODEL):
    return {"type": "stream_event", "event": {"type": "message_start", "message": {
        "id": mid, "model": model, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write, "output_tokens": 3}}}}


def _delta(out):
    return {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": out}}}


def _stop():
    return {"type": "stream_event", "event": {"type": "message_stop"}}


def _assistant(mid, inp=4, read=1000, write=200):
    return {"type": "assistant", "message": {"id": mid, "usage": {
        "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write}}}


def _call(mid, inp=4, read=1000, write=200, out=50):
    return [_start(mid, inp, read, write), _delta(out), _stop()]


def _result(calls, window=1_000_000, model=MODEL, num_turns=None):
    """A result event whose usage is the sum of ``calls`` = [(inp, read, write, out), ...]."""
    return {"type": "result", "is_error": False,
            "num_turns": len(calls) if num_turns is None else num_turns,
            "usage": {"input_tokens": sum(c[0] for c in calls),
                      "cache_read_input_tokens": sum(c[1] for c in calls),
                      "cache_creation_input_tokens": sum(c[2] for c in calls),
                      "output_tokens": sum(c[3] for c in calls)},
            "modelUsage": {model: {"contextWindow": window}} if window else {}}


def _fold(*events):
    f = cr.DispatchFold()
    for e in events:
        f.note(e)
    return f


TWO_CALLS = [(4, 1000, 200, 50), (6, 1400, 100, 60)]


def _two_calls_stream(window=1_000_000):
    return [*_call("m1", 4, 1000, 200, 50), _assistant("m1"),
            *_call("m2", 6, 1400, 100, 60), _assistant("m2", 6, 1400, 100), _assistant("m2", 6, 1400, 100),
            _result(TWO_CALLS, window=window)]


# --- the completion rule ------------------------------------------------------------------------

def test_reading_is_the_last_complete_calls_input_side():
    r = _fold(*_two_calls_stream()).reading()
    assert r == {"tokens": 6 + 1400 + 100, "window": 1_000_000}


def test_repeated_assistant_events_do_not_add_calls():
    """W0 fixture: three assistant events for two calls. Counting them would give 3 calls (WF1)."""
    f = _fold(*_two_calls_stream())
    assert len(f.calls) == 2
    assert f.reading() is not None  # the two calls match num_turns == 2


def test_several_deltas_keep_the_last_output_once():
    events = [_start("m1"), _delta(10), _delta(20), _delta(50), _stop(), _result([(4, 1000, 200, 50)])]
    assert _fold(*events).reading() == {"tokens": 1204, "window": 1_000_000}


def test_missing_window_keeps_the_tokens_and_guesses_nothing():
    assert _fold(*_two_calls_stream(window=None)).reading() == {"tokens": 1506, "window": None}
    other_model = _two_calls_stream()
    other_model[-1]["modelUsage"] = {"some-other-model": {"contextWindow": 200_000}}
    assert _fold(*other_model).reading() == {"tokens": 1506, "window": None}


@pytest.mark.parametrize("events", [
    # F-12: a complete call, then a final call that never finished
    [*_call("m1"), _start("m2"), _delta(5), _result([(4, 1000, 200, 50)], num_turns=1)],
    [*_call("m1"), _start("m2"), _result([(4, 1000, 200, 50)], num_turns=2)],
    # a call with no message_start, or no message_stop, or no delta
    [_delta(5), _stop(), _result([], num_turns=0)],
    [_start("m1"), _delta(50), _result([(4, 1000, 200, 50)])],
    [_start("m1"), _stop(), _result([(4, 1000, 200, 50)])],
    # no result event at all: nothing to cross-check
    [*_call("m1")],
    # complete calls that disagree with result.usage, or with num_turns
    [*_call("m1"), _result([(4, 1000, 200, 51)])],
    [*_call("m1"), _result([(4, 1000, 200, 50)], num_turns=2)],
    # an assistant event that disagrees with its call's start
    [*_call("m1"), _assistant("m1", inp=99), _result([(4, 1000, 200, 50)])],
    # a start whose input fields are missing: never read as zero
    [{"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1", "usage": {}}}},
     _delta(5), _stop(), _result([(0, 0, 0, 5)])],
], ids=["f12-unfinished-final", "f12-no-delta", "orphan-delta", "no-stop", "no-delta", "no-result",
        "usage-sum", "num-turns", "assistant-conflict", "missing-input-fields"])
def test_incomplete_or_conflicting_dispatch_gives_no_reading(events):
    assert _fold(*events).reading() is None


def test_a_subagents_calls_are_not_the_conversation():
    sub = _call("s1", 9, 9, 9, 9)
    for e in sub:
        e["parent_tool_use_id"] = "toolu_1"
    f = _fold(*_call("m1"), *sub, _result([(4, 1000, 200, 50)]))
    assert f.reading() == {"tokens": 1204, "window": 1_000_000}


def test_compaction_inside_a_dispatch_discards_calls_before_it():
    boundary = {"type": "system", "subtype": "compact_boundary"}
    done = [(4, 1000, 200, 50), (6, 300, 0, 60)]
    f = _fold(*_call("m1", 4, 1000, 200, 50), boundary, *_call("m2", 6, 300, 0, 60), _result(done))
    assert f.compacted and f.reading() == {"tokens": 306, "window": 1_000_000}
    only_before = _fold(*_call("m1"), boundary, _result([(4, 1000, 200, 50)]))
    assert only_before.compacted and only_before.reading() is None


# --- the session record: every transition resets it ---------------------------------------------

def _state(session_id="s-1") -> sess.SessionState:
    return sess.SessionState(cli="claude", claude_session_id=session_id, conversation_id=session_id)


def _finish(state, events=None, *, now="2026-11-02T10:00:00Z", finished=True):
    sess.record_dispatch(state, _fold(*(events or _two_calls_stream())), finished=finished, now=now)


def test_a_finished_turn_records_reading_time_window_and_baseline():
    st = _state()
    _finish(st)
    assert (st.conversation_context_tokens, st.conversation_context_window) == (1506, 1_000_000)
    assert st.conversation_context_at == "2026-11-02T10:00:00Z"
    assert st.conversation_baseline_tokens == 1506 and st.conversation_turns == 1
    _finish(st, [*_call("m3", 7, 5000, 0, 5), _result([(7, 5000, 0, 5)])], now="2026-11-02T11:00:00Z")
    assert st.conversation_context_tokens == 5007 and st.conversation_baseline_tokens == 1506
    assert st.conversation_turns == 2


@pytest.mark.parametrize("transition", [
    lambda st: sess.reset_claude_session(st, "fresh_requested"),                    # fresh start
    lambda st: sess.reset_claude_session(st, "resume_unavailable after 2 session failures"),
    lambda st: sess.reset_claude_session(st, "fresh_session_turn_failed"),          # a new session id
    lambda st: sess.reconcile_runtime_fingerprint(
        _with_fp(st), "other-model|high"),                                          # model change
], ids=["fresh-start", "self-heal", "failed-first-turn", "runtime-config-changed"])
def test_every_transition_resets_the_reading(transition):
    st = _state()
    _finish(st)
    old_id = st.claude_session_id
    transition(st)
    assert st.claude_session_id != old_id and st.conversation_id == st.claude_session_id
    assert st.conversation_turns == 0
    assert st.conversation_context_tokens is None and st.conversation_context_at == ""
    assert st.conversation_context_window is None and st.conversation_baseline_tokens is None


def _with_fp(st):
    st.runtime_fingerprint = "model|low"
    return st


def test_codex_reset_clears_the_reading_too():
    st = sess.SessionState(cli="codex", conversation_context_tokens=5, conversation_turns=3)
    sess.mark_resume_unavailable(st, "fresh_requested")
    assert st.conversation_context_tokens is None and st.conversation_turns == 0


def test_baseline_only_for_a_conversation_seen_from_its_start():
    legacy = sess.SessionState(cli="claude", claude_session_id="s-1", turns=375)  # saved before W1
    _finish(legacy)
    assert legacy.conversation_id == "s-1" and legacy.conversation_context_tokens == 1506
    assert legacy.conversation_baseline_tokens is None
    st = _state()
    sess.reset_claude_session(st, "fresh_requested")
    _finish(st)
    assert st.conversation_baseline_tokens == 1506  # a conversation the wrapper saw start


def test_failed_turn_records_nothing_but_a_compaction_clears_the_old_reading():
    st = _state()
    _finish(st)
    boundary = {"type": "system", "subtype": "compact_boundary"}
    _finish(st, [*_call("m9"), {"type": "rate_limit_event"}], finished=False, now="2026-11-02T12:00:00Z")
    assert st.conversation_context_tokens == 1506 and st.conversation_context_at == "2026-11-02T10:00:00Z"
    assert st.conversation_turns == 1
    _finish(st, [*_call("m9"), boundary], finished=False, now="2026-11-02T12:30:00Z")
    assert st.conversation_context_tokens is None and st.conversation_context_at == ""
    assert st.conversation_baseline_tokens == 1506  # what the conversation started at is still true


def test_compaction_in_a_finished_turn_replaces_the_reading_with_the_new_size():
    st = _state()
    _finish(st)
    boundary = {"type": "system", "subtype": "compact_boundary"}
    ev = [*_call("m1", 4, 1000, 200, 50), boundary, *_call("m2", 6, 30, 0, 60),
          _result([(4, 1000, 200, 50), (6, 30, 0, 60)])]
    _finish(st, ev, now="2026-11-02T13:00:00Z")
    assert st.conversation_context_tokens == 36


def test_a_dispatch_with_no_reading_keeps_the_old_reading_and_its_own_time():
    """F-12 and fresh rate-limit activity: the old reading keeps its time, nothing refreshes it."""
    st = _state()
    _finish(st)
    f12 = [*_call("m1"), _start("m2"), _delta(5), _result([(4, 1000, 200, 50)], num_turns=1)]
    _finish(st, [{"type": "rate_limit_event"}, *f12], now="2026-11-02T14:00:00Z")
    assert st.conversation_context_tokens == 1506
    assert st.conversation_context_at == "2026-11-02T10:00:00Z"
    assert st.conversation_turns == 2  # the turn still finished


def test_the_record_survives_a_restart_and_a_new_record_starts_its_own_conversation(tmp_path):
    store = Store(tmp_path)
    store.init(["claude-a"])
    first = sess.load_session(store, "claude-a", "claude")
    assert first.conversation_id == first.claude_session_id != ""
    _finish(first)
    sess.save_session(store, "claude-a", first)
    again = sess.load_session(store, "claude-a", "claude")  # resume: same conversation, same reading
    assert again.conversation_context_tokens == 1506 and again.conversation_id == again.claude_session_id


# --- the views ----------------------------------------------------------------------------------

def _store_with(tmp_path: Path, **record) -> Store:
    s = Store(tmp_path)
    s.init(["claude-a"])
    (s.state_dir / "claude-a.wrapper-session.json").write_text(json.dumps(record), encoding="utf-8")
    return s


def _record(**over):
    base = {"turns": 375, "claude_session_id": "s-1", "conversation_id": "s-1",
            "conversation_context_tokens": 160_000, "conversation_context_at": "2026-11-02T10:00:00Z",
            "conversation_context_window": 1_000_000}
    base.update(over)
    return base


def test_runtime_projection_gives_the_reading_only_for_the_current_conversation(tmp_path):
    rt = _store_with(tmp_path, **_record()).read_wrapper_runtime("claude-a")
    assert rt["conversation"] == {"tokens": 160_000, "at": "2026-11-02T10:00:00Z", "window": 1_000_000}
    assert "session_id" not in json.dumps(rt)
    # an old reading after a session change counts as no reading
    changed = _store_with(tmp_path / "x", **_record(claude_session_id="s-2")).read_wrapper_runtime("claude-a")
    assert "conversation" not in changed
    legacy = _store_with(tmp_path / "y", turns=5).read_wrapper_runtime("claude-a")
    assert "conversation" not in legacy


def test_size_says_in_this_conversation_and_gives_percent_only_with_a_window(tmp_path):
    rt = _store_with(tmp_path, **_record()).read_wrapper_runtime("claude-a")
    size = cs.conversation_size("claude", None, 375, None, reading=cs.reading_view(rt))
    assert size["basis"] == "context_tokens" and size["scope"] == "conversation"
    assert size["value"] == 160_000 and size["warn"] is True
    assert size["percent"] == 16 and size["window"] == 1_000_000 and size["at"] == "2026-11-02T10:00:00Z"
    text = cs.format_size(size)
    assert "160,000 tokens in this conversation (16% of 1,000,000, read 2026-11-02T10:00:00Z)" in text
    assert "turns in total" not in text
    nowin = _store_with(tmp_path / "n", **_record(conversation_context_window=None)).read_wrapper_runtime("claude-a")
    s2 = cs.conversation_size("claude", None, 375, None, reading=cs.reading_view(nowin))
    assert s2["value"] == 160_000 and s2["percent"] is None and "%" not in cs.format_size(s2)


def test_without_a_reading_the_old_figures_apply(tmp_path):
    size = cs.conversation_size("claude", None, 375, None, reading=None)
    assert size["basis"] == "turns_total"


def test_status_and_web_use_the_session_reading(tmp_path, monkeypatch):
    store = _store_with(tmp_path, **_record())
    row = next(a for a in cli._gather_status(store)["agents"] if a["name"] == "claude-a")
    assert row["conversation_size"]["scope"] == "conversation"
    assert row["conversation_size"]["value"] == 160_000
    entry = web._agent_entries(store, store.load_config(), [], None)[0]
    assert entry["conversation_size"]["scope"] == "conversation"
    assert entry["conversation_size"]["percent"] == 16


def test_checkpoint_shows_the_same_reading(tmp_path):
    store = _store_with(tmp_path, **_record())
    payload = checkpoint.build_checkpoint(store, "claude-a")
    assert payload["context"]["conversation"] == {
        "tokens": 160_000, "percent": 16, "window": 1_000_000, "at": "2026-11-02T10:00:00Z"}
    line = checkpoint.render_resume_context(payload)
    assert ("saved conversation reading (as at the save, may be out of date): "
            "160000 tokens (16% of 1000000, read 2026-11-02T10:00:00Z)") in line
    payload["context"]["conversation"]["window"] = None
    payload["context"]["conversation"]["percent"] = None
    assert "160000 tokens (window unknown, read 2026-11-02T10:00:00Z)" in checkpoint.render_resume_context(payload)
    payload["context"].pop("conversation")
    assert "conversation reading" not in checkpoint.render_resume_context(payload)


# --- through the real drive ---------------------------------------------------------------------

def test_a_real_drive_records_the_reading_from_its_own_stream(tmp_path):
    from agenttalk.wrapper import loop, run
    from test_usage_park_loop import AGENT, T0, Clock, Spawner, make_store

    store = make_store(tmp_path)
    state = sess.SessionState(cli="claude", claude_session_id="sid-1", conversation_id="sid-1")
    drive = run.make_drive(store, AGENT, "claude", state, ["claude"], spawn=Spawner(_two_calls_stream()),
                           clock=lambda: 0.0, render=False, heartbeat=lambda: None)
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso,
                  max_polls=3, wrapper_generation="g1")
    assert state.turns == 1 and state.conversation_turns == 1
    assert state.conversation_context_tokens == 1506 and state.conversation_context_window == 1_000_000
    assert state.conversation_baseline_tokens == 1506


def test_a_cadence_turn_records_the_reading_too(tmp_path):
    from agenttalk.wrapper import run

    store = Store(tmp_path)
    store.init(["beta"])
    state = sess.SessionState(cli="claude", claude_session_id="sid-1", conversation_id="sid-1")
    cadence = run.make_cadence_drive(
        store, "beta", "claude", state, ["claude"], clock=lambda: 0.0, render=False,
        spawn=lambda argv, _stdin: iter(json.dumps(e) for e in _two_calls_stream()))
    cadence({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}])
    assert state.conversation_context_tokens == 1506 and state.conversation_turns == 1


# --- fix round: the five findings of the read of a3fe6794 ----------------------------------------

BOUNDARY = {"type": "system", "subtype": "compact_boundary"}
ONE = [(4, 1000, 200, 50)]


def test_f3_a_call_that_started_before_the_compaction_never_supplies_the_reading_after_it():
    ev = [_start("m1"), BOUNDARY, _delta(50), _stop(), _result(ONE)]
    f = _fold(*ev)
    assert f.compacted and f.reading() is None


def test_f4_a_call_needs_an_identity_and_a_start_id_is_not_reused():
    no_id = _start("m1")
    del no_id["event"]["message"]["id"]
    assert _fold(no_id, _delta(50), _stop(), _result(ONE)).reading() is None
    twice = [_start("m1", 4, 1000, 200), _delta(50), _stop(), _start("m1", 6, 1400, 100), _delta(60), _stop(),
             _result(TWO_CALLS)]
    assert _fold(*twice).reading() is None
    # repeated assistant events for one existing call stay supported
    assert _fold(*_two_calls_stream()).reading() is not None


def test_f5_a_dispatch_has_one_result_and_a_contradicting_repeat_gives_no_reading():
    wrong = _result(ONE)
    wrong["usage"] = dict(wrong["usage"], output_tokens=99)
    assert _fold(*_call("m1"), wrong, _result(ONE)).reading() is None
    assert _fold(*_call("m1"), _result(ONE), _result(ONE)).reading() is None  # exactly one result is defined


class _LazyStream:
    """Like a child process: each line is produced only when the wrapper asks for it."""
    returncode = None
    watchdog_result = None

    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


def _hold_the_stream_after_the_boundary(seen, state, store, agent):
    """A stream that, once the wrapper has consumed the compaction line, looks at the live
    session object and at what a reloaded wrapper would read; then finishes the dispatch."""
    def gen():
        for e in [*_call("m1", 4, 1000, 200, 50), BOUNDARY]:
            yield json.dumps(e)
        seen["live"] = state.conversation_context_tokens
        seen["saved"] = sess.load_session(store, agent, "claude").conversation_context_tokens
        for e in [*_call("m2", 6, 30, 0, 60), _result([(4, 1000, 200, 50), (6, 30, 0, 60)])]:
            yield json.dumps(e)
    return gen()


def _old_reading_state(store, agent):
    st = sess.SessionState(cli="claude", claude_session_id="sid-1", conversation_id="sid-1")
    sess.record_dispatch(st, _fold(*_two_calls_stream()), finished=True, now="2026-11-02T10:00:00Z")
    sess.save_session(store, agent, st)
    return st


def test_f2_the_old_reading_is_cleared_and_saved_the_moment_the_compaction_is_seen_message_path(tmp_path):
    from agenttalk.wrapper import loop, run
    from test_usage_park_loop import AGENT, T0, Clock, make_store

    store = make_store(tmp_path)
    state, seen = _old_reading_state(store, AGENT), {}
    assert sess.load_session(store, AGENT, "claude").conversation_context_tokens == 1506
    drive = run.make_drive(
        store, AGENT, "claude", state, ["claude"], clock=lambda: 0.0, render=False, heartbeat=lambda: None,
        persist=lambda st: sess.save_session(store, AGENT, st),
        spawn=lambda argv, stdin: _LazyStream(_hold_the_stream_after_the_boundary(seen, state, store, AGENT)))
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso,
                  max_polls=3, wrapper_generation="g1")
    assert seen == {"live": None, "saved": None}
    assert state.conversation_context_tokens == 36  # the finished turn then records the new size


def test_f2_the_old_reading_is_cleared_and_saved_the_moment_the_compaction_is_seen_cadence_path(tmp_path):
    from agenttalk.wrapper import run

    store = Store(tmp_path)
    store.init(["beta"])
    state, seen = _old_reading_state(store, "beta"), {}
    cadence = run.make_cadence_drive(
        store, "beta", "claude", state, ["claude"], clock=lambda: 0.0, render=False,
        persist=lambda st: sess.save_session(store, "beta", st),
        spawn=lambda argv, _stdin: _hold_the_stream_after_the_boundary(seen, state, store, "beta"))
    cadence({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}])
    assert seen == {"live": None, "saved": None}


# Finding 1, through the real hook commands (save before compaction, reload after it)

def _hook_run(root, *argv):
    from test_checkpoint import _run
    return _run(root, *argv)


def _checkpointed_seat(tmp_path, monkeypatch, hook_session):
    from test_checkpoint import FIXTURE, _BinaryStdin
    store = Store(tmp_path)
    store.init(["alpha"])
    fixture_session = json.loads(FIXTURE.read_text(encoding="utf-8"))["session_id"]
    state = sess.SessionState(
        cli="claude", claude_session_id=fixture_session, conversation_id=fixture_session, turns=375,
        conversation_context_tokens=160_000, conversation_context_at="2026-11-02T10:00:00Z",
        conversation_context_window=1_000_000)
    sess.save_session(store, "alpha", state)  # the real save: load_session must accept it
    monkeypatch.setattr(checkpoint, "collect_git_state", lambda _r: {"head": "abc", "branch": "b", "dirty_files": 0})
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["session_id"] = hook_session or fixture_session
    monkeypatch.setattr("sys.stdin", _BinaryStdin(json.dumps(payload).encode()))
    assert _hook_run(tmp_path, "save", "--hook", "--for", "alpha") == 0
    return store, fixture_session


def _resume_text(tmp_path, capsys):
    capsys.readouterr()
    assert _hook_run(tmp_path, "resume", "--hook", "--for", "alpha") == 0
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("after", ["compaction", "fresh-start"])
def test_f1_a_reloaded_checkpoint_never_calls_its_saved_size_the_current_one(tmp_path, monkeypatch, capsys, after):
    store, sid = _checkpointed_seat(tmp_path, monkeypatch, None)
    st = sess.load_session(store, "alpha", "claude")
    assert (st.claude_session_id, st.conversation_id) == (sid, sid)  # the saved session, not a fresh one
    assert st.conversation_context_tokens == 160_000 and st.conversation_context_at == "2026-11-02T10:00:00Z"
    if after == "compaction":
        sess.record_dispatch(st, _fold(*_call("m1"), BOUNDARY, _result(ONE)), finished=False)
    else:
        sess.reset_claude_session(st, "fresh_requested")
    sess.save_session(store, "alpha", st)
    assert store.read_wrapper_runtime("alpha").get("conversation") is None  # the live reading is gone
    text = _resume_text(tmp_path, capsys)
    assert "in this conversation" not in text and "current" not in text
    assert "saved conversation reading" in text and "read 2026-11-02T10:00:00Z" in text


def test_f1_a_hook_naming_another_session_gets_no_wrapper_reading(tmp_path, monkeypatch, capsys):
    _checkpointed_seat(tmp_path, monkeypatch, "another-session-id")
    saved = json.loads((tmp_path / ".agenttalk" / "checkpoints" / "alpha.json").read_text(encoding="utf-8"))
    assert "conversation" not in saved["context"]
    assert "saved conversation reading" not in _resume_text(tmp_path, capsys)


# --- #605: a failed save at the compaction must not leave the old size looking current ----------

OLD_AT = "2026-11-02T10:00:00Z"


def _compaction_with_broken_storage(tmp_path, *, session_save_fails, health_write_fails):
    """Drive a dispatch that compacts, with chosen storage failing at that moment; sample every
    public view and a restart while the turn is still running, then let storage heal."""
    from agenttalk.wrapper import loop, run
    from agenttalk.wrapper.health import WrapperHealthWriter
    from test_usage_park_loop import AGENT, T0, Clock, make_store

    store = make_store(tmp_path)
    state, seen, broken = _old_reading_state(store, AGENT), {}, {"session": False, "health": False}
    real_write_health = store.write_health

    def write_health(agent, snap):
        if broken["health"]:
            raise OSError("health refused")
        real_write_health(agent, snap)

    store.write_health = write_health
    health = WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=0.0)

    def persist(st):
        if broken["session"]:
            raise OSError("session refused")
        sess.save_session(store, AGENT, st)

    def gen():
        yield from (json.dumps(e) for e in _call("m1", 4, 1000, 200, 50))
        broken["session"], broken["health"] = session_save_fails, health_write_fails
        yield json.dumps(BOUNDARY)
        seen["runtime"] = store.read_wrapper_runtime(AGENT)
        seen["status"] = next(a for a in cli._gather_status(store)["agents"] if a["name"] == AGENT)
        seen["web"] = next(e for e in web._agent_entries(store, store.load_config(), [], None) if e["name"] == AGENT)
        seen["checkpoint"] = checkpoint.build_checkpoint(store, AGENT)["context"]
        seen["restart"] = sess.load_session(store, AGENT, "claude").conversation_context_tokens
        broken["session"] = broken["health"] = False
        yield from (json.dumps(e) for e in [*_call("m2", 6, 30, 0, 60),
                                            _result([(4, 1000, 200, 50), (6, 30, 0, 60)])])

    drive = run.make_drive(store, AGENT, "claude", state, ["claude"], clock=lambda: 0.0, render=False,
                           heartbeat=lambda: None, persist=persist, health_writer=health,
                           spawn=lambda argv, stdin: _LazyStream(gen()))
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso,
                  max_polls=3, wrapper_generation="g1")
    seen["after"] = store.read_wrapper_runtime(AGENT)
    seen["after_status"] = next(a for a in cli._gather_status(store)["agents"] if a["name"] == AGENT)
    return seen


def _assert_a_good_save_wins(seen):
    after = seen["after"]
    assert after["conversation"]["tokens"] == 36 and "current" not in after["conversation"]
    assert "size_reading_unsaved" not in after and "size_reading_unsaved" not in seen["after_status"]
    assert seen["after_status"]["conversation_size"]["scope"] == "conversation"


def test_605_session_save_fails_health_works_every_view_says_saved_not_current(tmp_path):
    seen = _compaction_with_broken_storage(tmp_path, session_save_fails=True, health_write_fails=False)
    assert seen["runtime"]["size_reading_unsaved"] is True
    assert seen["runtime"]["conversation"]["current"] is False
    for size in (seen["status"]["conversation_size"], seen["web"]["conversation_size"]):
        assert size["scope"] == "saved" and size["at"] == OLD_AT and size["warn"] is False
        assert "saved reading, may be out of date" in cs.format_size(size)
    assert seen["status"]["size_reading_unsaved"] is True and seen["web"]["size_reading_unsaved"] is True
    assert seen["checkpoint"]["conversation"]["current"] is False
    _assert_a_good_save_wins(seen)


def test_605_when_storage_refuses_the_session_and_the_health_write_the_old_reading_keeps_its_time(tmp_path):
    """The accepted residual: nothing can say the saved reading is obsolete, so every view and a
    restart show it with its read time, until the next good save."""
    seen = _compaction_with_broken_storage(tmp_path, session_save_fails=True, health_write_fails=True)
    assert "size_reading_unsaved" not in seen["runtime"] and "current" not in seen["runtime"]["conversation"]
    assert seen["status"]["conversation_size"]["at"] == OLD_AT
    assert seen["web"]["conversation_size"]["at"] == OLD_AT and seen["restart"] == 1506
    _assert_a_good_save_wins(seen)


def test_605_when_only_the_health_write_fails_the_clear_is_saved_and_nothing_is_flagged(tmp_path):
    seen = _compaction_with_broken_storage(tmp_path, session_save_fails=False, health_write_fails=True)
    assert "conversation" not in seen["runtime"] and "size_reading_unsaved" not in seen["runtime"]
    assert seen["restart"] is None and "conversation" not in seen["checkpoint"]
    _assert_a_good_save_wins(seen)


def test_605_a_warning_left_on_disk_never_hides_a_later_valid_reading_for_long(tmp_path):
    """The health file is rewritten by the wrapper's own state, so a clear that could not be written
    is repeated by the next health write; no leftover file can outlive it."""
    from agenttalk.wrapper.health import WrapperHealthWriter

    store = _store_with(tmp_path, **_record())
    health = WrapperHealthWriter(store, "claude-a", "claude", mode="wrapper", min_interval=0.0)
    health.idle()
    health.size_unsaved()
    assert store.read_wrapper_runtime("claude-a")["conversation"]["current"] is False
    real = store.write_health
    store.write_health = lambda a, snap: (_ for _ in ()).throw(OSError("refused"))
    health.size_unsaved(False)  # a good session save, but the health write is refused
    store.write_health = real
    health.idle()  # the next ordinary health write repeats the truth
    rt = store.read_wrapper_runtime("claude-a")
    assert "size_reading_unsaved" not in rt and "current" not in rt["conversation"]


def test_605_a_healthy_seat_adds_no_key(tmp_path):
    store = _store_with(tmp_path, **_record())
    assert "size_reading_unsaved" not in store.read_wrapper_runtime("claude-a")
    row = next(a for a in cli._gather_status(store)["agents"] if a["name"] == "claude-a")
    assert "size_reading_unsaved" not in row and row["conversation_size"]["scope"] == "conversation"


def test_605_a_save_that_fails_once_is_retried_and_nothing_is_flagged(tmp_path):
    from agenttalk.wrapper import run
    from agenttalk.wrapper.health import WrapperHealthWriter

    store = Store(tmp_path)
    store.init(["beta"])
    state, calls = _old_reading_state(store, "beta"), []
    health = WrapperHealthWriter(store, "beta", "claude", mode="wrapper", min_interval=0.0)
    health.idle()

    def persist(st):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("busy")
        sess.save_session(store, "beta", st)

    run._compaction_seen(state, persist, health)()
    rt = store.read_wrapper_runtime("beta")
    assert len(calls) == 2 and "size_reading_unsaved" not in rt and "conversation" not in rt


def test_605_a_saved_reading_never_raises_the_long_conversation_warning():
    big = {"tokens": 900_000, "window": None, "at": OLD_AT, "percent": None}
    assert cs.conversation_size("claude", None, 5, None, reading=big)["warn"] is True
    saved = cs.conversation_size("claude", None, 5, None, reading=dict(big, current=False))
    assert saved["scope"] == "saved" and saved["warn"] is False


# --- #605 review: a health file with a malformed `warnings` value must not break any read ---------

_TOKEN = "size_reading_unsaved"
_BAD_WARNINGS = [1, "x size_reading_unsaved", {_TOKEN: 1}, None, [5, {"a": 1}], True, 1.5]


def _views(store, agent="claude-a"):
    """Every public view the #605 flag reaches; each must be readable whatever the health file holds."""
    row = next(a for a in cli._gather_status(store)["agents"] if a["name"] == agent)
    entry = next(e for e in web._agent_entries(store, store.load_config(), [], None) if e["name"] == agent)
    return {"runtime": store.read_wrapper_runtime(agent), "status": row, "web": entry,
            "checkpoint": checkpoint.build_checkpoint(store, agent)["context"]}


@pytest.mark.parametrize("warnings", _BAD_WARNINGS, ids=repr)
def test_605_a_malformed_health_warnings_value_means_no_flag_and_no_error(tmp_path, warnings):
    store = _store_with(tmp_path, **_record())
    store.health_path("claude-a").write_text(json.dumps({"warnings": warnings}), encoding="utf-8")
    for name, view in _views(store).items():
        assert _TOKEN not in json.dumps(view), name
    assert "current" not in _views(store)["runtime"]["conversation"]


@pytest.mark.parametrize("warnings", [[_TOKEN], ["other", _TOKEN], [_TOKEN, 5]], ids=repr)
def test_605_a_list_holding_the_exact_token_still_shows_the_flag_in_every_view(tmp_path, warnings):
    store = _store_with(tmp_path, **_record())
    store.health_path("claude-a").write_text(json.dumps({"warnings": warnings}), encoding="utf-8")
    views = _views(store)
    for name in ("runtime", "status", "web"):
        assert views[name][_TOKEN] is True, name
    assert views["runtime"]["conversation"]["current"] is False
    assert views["checkpoint"]["conversation"]["current"] is False


# --- #625: an older warning is told apart from a newer valid reading, and survives a restart ------

def _writer(store):
    from agenttalk.wrapper.health import WrapperHealthWriter
    return WrapperHealthWriter(store, "claude-a", "claude", mode="wrapper", min_interval=0.0)


def _measure_again(store, tokens=42_000, *, seconds=60):
    """A new measurement saved after the warning: the session file gets a later read time."""
    from datetime import datetime, timedelta, timezone
    path = store.state_dir / "claude-a.wrapper-session.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    later = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rec.update(conversation_context_tokens=tokens, conversation_context_at=later)
    path.write_text(json.dumps(rec), encoding="utf-8")


def _refuse_health(store):
    store.write_health = lambda a, snap: (_ for _ in ()).throw(OSError("refused"))


def _views_loose(store):
    """The projections, without the checkpoint (it does not take a malformed record as a reading)."""
    v = _views(store)
    return {k: v[k] for k in ("runtime", "status", "web")}


def _says_stale(store, agent="claude-a"):
    v = _views(store, agent)
    return [v["runtime"].get(_TOKEN), v["status"].get(_TOKEN), v["web"].get(_TOKEN),
            v["runtime"]["conversation"].get("current"), v["checkpoint"]["conversation"].get("current")]


def test_625_a_restart_keeps_a_recorded_warning_while_the_reading_has_not_changed(tmp_path):
    store = _store_with(tmp_path, **_record())
    first = _writer(store)
    first.idle()
    first.size_unsaved()
    assert _says_stale(store) == [True, True, True, False, False]
    _writer(store).idle()  # the restart: a new writer publishes its first state
    assert _says_stale(store) == [True, True, True, False, False]


def test_625_a_restart_does_not_carry_the_warning_over_a_reading_measured_after_it(tmp_path):
    store = _store_with(tmp_path, **_record())
    first = _writer(store)
    first.idle()
    first.size_unsaved()
    _measure_again(store)
    _writer(store).idle()
    assert _says_stale(store) == [None, None, None, None, None]


def test_625_a_restart_with_no_reading_carries_no_warning(tmp_path):
    store = _store_with(tmp_path, **_record())
    first = _writer(store)
    first.idle()
    first.size_unsaved()
    path = store.state_dir / "claude-a.wrapper-session.json"  # the clear is saved at last
    path.write_text(json.dumps(_record(conversation_context_tokens=None, conversation_context_at="")), encoding="utf-8")
    _writer(store).idle()
    assert _TOKEN not in json.dumps(store.read_wrapper_runtime("claude-a"))


def test_625_a_good_save_is_not_shown_stale_while_the_health_write_that_clears_the_warning_fails(tmp_path):
    store = _store_with(tmp_path, **_record())
    writer = _writer(store)
    writer.idle()
    writer.size_unsaved()
    _refuse_health(store)  # every write that would clear the warning fails
    _measure_again(store)
    writer.size_unsaved(False)  # the good save ends the warning, but the write is refused
    assert _says_stale(store) == [None, None, None, None, None]
    assert _views(store)["runtime"]["conversation"]["tokens"] == 42_000


@pytest.mark.parametrize("stamp", ["not a time", "2026-11-02T10:00:00"])
def test_625_a_read_time_that_cannot_be_compared_keeps_the_warning(tmp_path, stamp):
    store = _store_with(tmp_path, **_record(conversation_context_at=stamp))
    store.health_path("claude-a").write_text(
        json.dumps({"warnings": [_TOKEN], "updated_at": "2026-11-03T10:00:00.000000Z"}), encoding="utf-8")
    assert store.read_wrapper_runtime("claude-a")[_TOKEN] is True
    store.health_path("claude-a").write_text(json.dumps({"warnings": [_TOKEN], "updated_at": 5}), encoding="utf-8")
    assert store.read_wrapper_runtime("claude-a")[_TOKEN] is True


# --- #625 fix round: the warning is tied to the reading it concerns, not to a clock ------------

def test_625_a_clock_that_ran_backwards_does_not_hide_a_warning_on_the_old_reading(tmp_path):
    """The old reading carries a read time later than the warning's write time (the clock moved back
    between them). Only a different reading may end the warning, not the order of two times."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    store = _store_with(tmp_path, **_record(conversation_context_at=future))
    writer = _writer(store)
    writer.idle()
    writer.size_unsaved()
    assert _says_stale(store) == [True, True, True, False, False]


def test_625_a_refreshed_warning_record_does_not_make_a_newer_reading_stale(tmp_path):
    """The warning-bearing health record is rewritten after the replacement reading was saved (any
    progress write does that); the replacement must still show as current when later writes fail."""
    store = _store_with(tmp_path, **_record())
    writer = _writer(store)
    writer.idle()
    writer.size_unsaved()
    _measure_again(store, seconds=0)  # saved at once, before the record is refreshed
    writer.idle()  # the refresh: still carries the warning, and is newer than the reading
    _refuse_health(store)
    writer.size_unsaved(False)
    assert _says_stale(store) == [None, None, None, None, None]
    assert _views(store)["runtime"]["conversation"]["tokens"] == 42_000


def test_625_a_restart_that_cannot_read_the_health_record_does_not_erase_the_warning(tmp_path, monkeypatch):
    from pathlib import Path
    store = _store_with(tmp_path, **_record())
    first = _writer(store)
    first.idle()
    first.size_unsaved()
    real, health_file = Path.read_text, store.health_path("claude-a")
    broken = {"on": True}

    def read_text(self, *a, **k):
        if broken["on"] and self == health_file:
            raise OSError("unreadable")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    second = _writer(store)  # the restart
    second.idle()  # the first successful write, while the old record still cannot be read
    broken["on"] = False
    assert _says_stale(store) == [True, True, True, False, False]
    second.idle()  # readable again: the record it wrote says the same, so nothing is lost
    assert _says_stale(store) == [True, True, True, False, False]


def test_625_a_warning_naming_another_reading_than_the_stored_one_is_dropped_and_a_bad_name_is_ignored(tmp_path):
    store = _store_with(tmp_path, **_record())

    def see(ref):
        snap = {"warnings": [_TOKEN], "updated_at": "2026-11-03T10:00:00.000000Z"}
        if ref is not ...:
            snap["size_reading_ref"] = ref
        store.health_path("claude-a").write_text(json.dumps(snap), encoding="utf-8")
        return store.read_wrapper_runtime("claude-a").get(_TOKEN)

    from agenttalk import health
    assert see(health.reading_ref(160_000, OLD_AT)) is True  # names the stored reading
    assert see(health.reading_ref(1, OLD_AT)) is None  # names another one
    assert see(...) is True  # an older record without a name still stands
    for bad in (5, "x", None, ["a"], {"a": 1}):
        assert see(bad) is True  # unreadable name: the warning stands, nothing raises


# --- #625 fix round 2: the progress writer keeps the name; a malformed read time never raises -----

def test_625_a_progress_note_keeps_the_warning_and_its_name_together(tmp_path):
    """The real progress writer (`health.stamp_progress`, as `agenttalk progress` calls it) rewrites
    the record of a working agent; a newer reading saved afterwards must still show as current."""
    from agenttalk import health
    store = _store_with(tmp_path, **_record())
    writer = _writer(store)
    writer.turn_start({"id": "m1", "request_id": "r1"})  # a working record, as during a turn
    writer.size_unsaved()
    before = store.read_health_raw("claude-a")
    assert health.SIZE_UNSAVED_WARNING in before["warnings"] and before["size_reading_ref"]
    _measure_again(store, seconds=0)  # the replacement is saved, then a progress note is posted
    store.write_health("claude-a", health.stamp_progress(store.read_health_raw("claude-a")))
    after = store.read_health_raw("claude-a")
    assert after["warnings"] == before["warnings"] and after["size_reading_ref"] == before["size_reading_ref"]
    _refuse_health(store)  # nothing can clear the warning now
    assert _says_stale(store) == [None, None, None, None, None]
    assert _views(store)["runtime"]["conversation"]["tokens"] == 42_000


def test_625_a_progress_note_drops_a_name_that_has_no_warning(tmp_path):
    from agenttalk import health
    snap = {"schema_version": 1, "agent": "claude-a", "state": "working_turn", "warnings": [],
            "size_reading_ref": "0123456789ab"}
    assert "size_reading_ref" not in health.stamp_progress(snap)


@pytest.mark.parametrize("at", ["\ud800", "x\udfffy", "\ud83d"], ids=repr)
def test_625_a_read_time_that_cannot_be_encoded_never_makes_a_view_raise(tmp_path, at):
    """A hand-edited session record with a lone surrogate in the read time, beside a warning that
    names some reading (the escaped form is what a JSON file holds)."""
    store = _store_with(tmp_path, **_record(conversation_context_at=at))
    store.health_path("claude-a").write_text(
        json.dumps({"warnings": [_TOKEN], "size_reading_ref": "0123456789ab"}), encoding="utf-8")
    for name, view in _views_loose(store).items():
        assert name and isinstance(view, dict)


# --- #625 fix round 3: the record is rewritten when the reading the warning names changes --------

def test_625_a_second_failed_save_after_a_restart_publishes_the_new_name(tmp_path):
    store = _store_with(tmp_path, **_record())
    first = _writer(store)
    first.idle()
    first.size_unsaved()  # warning on the reading R1
    real = store.write_health
    _refuse_health(store)
    _measure_again(store)  # R2 is saved; the health write that clears R1's warning fails
    first.size_unsaved(False)
    store.write_health = real
    second = _writer(store)  # restart: inherits the warning that still names R1
    second.idle()
    assert _says_stale(store) == [None, None, None, None, None]  # R2 is not what the record names
    second.size_unsaved()  # another compaction whose save fails: R2 is now the obsolete reading
    assert _says_stale(store) == [True, True, True, False, False]


# --- #625 fix round 4: the rule. The warning ends only when a save stores a reading different from
# the one it names, or no reading. A save that rewrites the same reading (resume bookkeeping) keeps it.

STALE = [True, True, True, False, False]
CURRENT = [None, None, None, None, None]


def _restarted_with_a_warning(tmp_path):
    """The old reading R1 is on disk, its warning is recorded, then the wrapper restarts."""
    from agenttalk.wrapper.health import WrapperHealthWriter
    from test_usage_park_loop import AGENT, make_store

    store = make_store(tmp_path)
    _old_reading_state(store, AGENT)
    before = WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=0.0)
    before.idle()
    before.size_unsaved()
    assert _says_stale(store, AGENT) == STALE
    state = sess.load_session(store, AGENT, "claude")  # the restart: R1 is back in memory
    assert state.conversation_context_tokens == 1506
    state.turns = max(state.turns, 3)  # a seat with history: its next turn resumes the conversation
    return store, state, WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=0.0)


def test_625_a_resume_bookkeeping_save_of_the_same_reading_keeps_the_warning_message_path(tmp_path):
    from agenttalk.wrapper import loop, run
    from test_usage_park_loop import AGENT, T0, Clock

    store, state, writer = _restarted_with_a_warning(tmp_path)
    seen = {}

    def spawn(argv, stdin):
        if "views" not in seen:  # the first child: resume_attempt_start has been saved just before
            seen["resume"] = "--resume" in argv
            seen["views"] = _says_stale(store, AGENT)
        return _LazyStream([])

    drive = run.make_drive(store, AGENT, "claude", state, ["claude"], clock=lambda: 0.0, render=False,
                           heartbeat=lambda: None, health_writer=writer,
                           persist=lambda st: sess.save_session(store, AGENT, st), spawn=spawn)
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso,
                  max_polls=3, wrapper_generation="g1")
    assert seen["resume"] is True
    assert seen["views"] == STALE


def test_625_a_resume_bookkeeping_save_of_the_same_reading_keeps_the_warning_cadence_path(tmp_path):
    from agenttalk.wrapper import run
    from test_usage_park_loop import AGENT

    store, state, writer = _restarted_with_a_warning(tmp_path)
    writer.idle()  # a running wrapper has published its first state before any turn
    seen = {}

    def spawn(argv, _stdin):
        seen.setdefault("resume", "--resume" in argv)
        seen.setdefault("views", _says_stale(store, AGENT))
        return []

    cadence = run.make_cadence_drive(store, AGENT, "claude", state, ["claude"], clock=lambda: 0.0, render=False,
                                     health_writer=writer, persist=lambda st: sess.save_session(store, AGENT, st),
                                     spawn=spawn)
    cadence({"agent": AGENT}, [{"type": "dead_letter", "key": "dl:1"}])
    assert seen["resume"] is True
    assert seen["views"] == STALE


def _save_through_the_drive_wrapper(store, state, writer, agent):
    from agenttalk.wrapper import run
    writer.idle()  # the restarted writer has published its first state (it inherits the warning)
    run._persist_ending_unsaved(lambda st: sess.save_session(store, agent, st), writer)(state)


def test_625_a_save_of_a_different_reading_ends_the_warning(tmp_path):
    from test_usage_park_loop import AGENT
    store, state, writer = _restarted_with_a_warning(tmp_path)
    state.conversation_context_tokens, state.conversation_context_at = 4242, "2026-11-04T10:00:00Z"
    _save_through_the_drive_wrapper(store, state, writer, AGENT)
    assert _TOKEN not in store.read_health_raw(AGENT)["warnings"]
    assert _says_stale(store, AGENT) == CURRENT


def test_625_a_save_that_stores_no_reading_ends_the_warning(tmp_path):
    from test_usage_park_loop import AGENT
    store, state, writer = _restarted_with_a_warning(tmp_path)
    sess.clear_reading(state)
    _save_through_the_drive_wrapper(store, state, writer, AGENT)
    assert _TOKEN not in store.read_health_raw(AGENT)["warnings"]
    assert _TOKEN not in json.dumps(store.read_wrapper_runtime(AGENT))


def test_625_a_warning_with_no_name_ends_at_any_good_save(tmp_path):
    from test_usage_park_loop import AGENT
    store, state, writer = _restarted_with_a_warning(tmp_path)
    raw = store.read_health_raw(AGENT)
    del raw["size_reading_ref"]  # a record from an older build
    store.health_path(AGENT).write_text(json.dumps(raw), encoding="utf-8")
    _save_through_the_drive_wrapper(store, state, writer, AGENT)
    assert _TOKEN not in store.read_health_raw(AGENT)["warnings"]


def test_625_a_same_reading_save_before_the_restarted_writer_has_written_keeps_the_warning(tmp_path):
    """The first thing the new writer sees is a save: it must judge it against the warning the
    restart carried over, not against an empty memory."""
    from agenttalk.wrapper import run
    from test_usage_park_loop import AGENT
    store, state, writer = _restarted_with_a_warning(tmp_path)
    run._persist_ending_unsaved(lambda st: sess.save_session(store, AGENT, st), writer)(state)
    writer.idle()  # its first write
    assert _says_stale(store, AGENT) == STALE


# --- #656: the session-start save and the idle fresh-start save settle the warning too ------------

_NAMES = {"older build, no name": ..., "damaged name": "not-a-name", "damaged name, wrong type": 5, "named": "named"}


def _warned(tmp_path, name, *, reading=True):
    """A seat whose health record carries the warning (named, unnamed or damaged) and whose
    session record holds the reading R1 or none."""
    from agenttalk import health
    store = Store(tmp_path)
    store.init(["claude-a"])
    state = sess.SessionState(cli="claude", claude_session_id="s-1", conversation_id="s-1", turns=375)
    if reading:
        state.conversation_context_tokens, state.conversation_context_at = 160_000, OLD_AT
        state.conversation_context_window = 1_000_000
    sess.save_session(store, "claude-a", state)  # the real save: load_session must accept it
    snap = {"warnings": [_TOKEN], "state": "idle_waiting", "agent": "claude-a"}
    if name == "named":
        snap["size_reading_ref"] = health.reading_ref(160_000, OLD_AT)
    elif name is not ...:
        snap["size_reading_ref"] = name
    store.health_path("claude-a").write_text(json.dumps(snap), encoding="utf-8")
    return store


def _run_the_wrapper_start(store, monkeypatch, during):
    """The real `_wrap_loop_mode` start (load, save, health writer), with the loop replaced by `during`,
    which gets the keyword arguments the loop would be given (idle hooks included)."""
    from agenttalk.wrapper import loop, run
    monkeypatch.setattr(loop, "run_loop", lambda st, agent, drive, **kw: during(kw) or 0)
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: (lambda rec: True))
    assert cli._wrap_loop_mode(store, "claude-a", cli="claude", base_argv=["claude"], sender="claude-a",
                               min_interval=0.0, render=False) == 0


@pytest.mark.parametrize("name", list(_NAMES.values()), ids=list(_NAMES))
def test_656_the_session_start_save_ends_a_warning_when_no_reading_is_stored(tmp_path, monkeypatch, name):
    store = _warned(tmp_path, name, reading=False)
    _run_the_wrapper_start(store, monkeypatch, lambda kw: kw["on_health_idle"]())  # the loop's first idle write
    assert _TOKEN not in store.read_health_raw("claude-a")["warnings"]
    assert _TOKEN not in json.dumps(store.read_wrapper_runtime("claude-a"))


@pytest.mark.parametrize("name", list(_NAMES.values()), ids=list(_NAMES))
def test_656_the_idle_fresh_start_save_ends_the_warning_with_the_reading(tmp_path, monkeypatch, name):
    store = _warned(tmp_path, name)
    assert store.write_fresh_request("claude-a", {"agent": "claude-a", "request_id": "fr-aaaaaaaaaaaa",
                                                  "requested_by": "claude-a", "reason": "x",
                                                  "at": "2026-11-05T10:00:00Z", "at_epoch": 1.0})

    def during(kw):
        kw["on_health_idle"]()
        kw["on_idle_poll"]()  # the idle fresh start: saves a record without the reading
        kw["on_health_idle"]()  # later idle writes must not bring the warning back

    _run_the_wrapper_start(store, monkeypatch, during)
    assert "conversation" not in store.read_wrapper_runtime("claude-a")
    assert _TOKEN not in store.read_health_raw("claude-a")["warnings"]
    for name_, view in _views_loose(store).items():
        assert _TOKEN not in json.dumps(view), name_


def test_656_the_session_start_save_of_the_same_reading_keeps_a_named_warning(tmp_path, monkeypatch):
    """The rule, at the start: the warning names R1 and the start stores R1 again, so it stays."""
    store = _warned(tmp_path, "named")
    _run_the_wrapper_start(store, monkeypatch, lambda kw: kw["on_health_idle"]())
    assert _says_stale(store) == STALE

