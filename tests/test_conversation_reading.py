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
    assert "conversation=160000 tokens in this conversation (16% of 1000000, read 2026-11-02T10:00:00Z)" in line
    payload["context"]["conversation"]["window"] = None
    payload["context"]["conversation"]["percent"] = None
    assert "(window unknown, read 2026-11-02T10:00:00Z)" in checkpoint.render_resume_context(payload)
    payload["context"].pop("conversation")
    assert "in this conversation" not in checkpoint.render_resume_context(payload)


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
