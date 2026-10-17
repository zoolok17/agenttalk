"""A real restart of the lead's wrapper around the commit of a finished result (lead loop, slice 1).

Design: docs/DESIGN-lead-loop-default.md, sections 9 and 11 (restart before and after commit). Two wrapper instances
run one after the other on the same store, the way a supervisor relaunches an interrupted controller:

* the first is interrupted BEFORE the commit (the cursor is not moved): the result is delivered again to the second,
  the model's hand-off is published exactly once however often it is attempted, and the thread is seen afterwards;
* the first is interrupted AFTER the commit: the second does not replay the result, and the hand-off, which is derived
  from the bus and not from the consumed message, is still owed and can be closed exactly once.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import lead_relay as relay
from agenttalk import threads
from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session


class Crash(Exception):
    """The process dies here (a kill, a power loss): nothing after this point runs."""


def _scenario(tmp_path: Path) -> tuple[Store, str]:
    s = Store(tmp_path)
    s.init(["lead", "reviewer", "builder"])
    s.set_role("lead", "lead")
    # relay_to on the lead's own request declares the waiter, so the finished result is the ONLY inbound message
    s.send(sender="lead", recipient="reviewer", kind="review-request", body="please",
           meta={"request_id": "R1", "relay_to": "builder"})
    result = s.send(sender="reviewer", recipient="lead", kind="review-result", body="GO",
                    meta={"request_id": "R1", "status": "approved"}).id
    return s, result


def _ok_lines() -> list[str]:
    return [json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})]


class Model:
    """The fake model process of one wrapper instance: counts the deliveries of the result and, when told to,
    does what the lead skill tells the lead to do: hand the decision over."""

    def __init__(self, s: Store, result: str, *, hand_over: bool) -> None:
        self.s, self.result, self.hand_over = s, result, hand_over
        self.deliveries = 0
        self.publications: list[bool] = []

    def __call__(self, argv, stdin):
        if self.result in (stdin or ""):
            self.deliveries += 1
            if self.hand_over:
                _, created = relay.publish_handoff(self.s, "lead", "builder", result_id=self.result, body="merge it")
                self.publications.append(created)
        return _ok_lines()


def _instance(s: Store, model: Model, **hooks) -> int:
    """One wrapper instance: a fresh drive, a fresh session, the real run_loop; it handles one turn at most."""
    state = session.SessionState(cli="claude", claude_session_id="sess-%d" % id(model), turns=0,
                                 resume_available=False)
    drive = run.make_drive(s, "lead", "claude", state, ["claude"], spawn=model, clock=lambda: 0.0, render=False)
    ticks = {"t": 0.0}

    def sleep(delay: float) -> None:
        ticks["t"] += max(delay, 1.0)

    return loop.run_loop(s, "lead", drive, clock=lambda: ticks["t"], sleep=sleep, max_turns=1, max_polls=60,
                         **hooks)


def _publications(s: Store, result: str) -> list:
    return [m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]


def _states(s: Store) -> list[str]:
    return [o.state for o in relay.derive(s.valid_messages(), "lead")]


def _thread_state(s: Store) -> str:
    (thread,) = [t for t in threads.derive_threads(s.valid_messages(), agent="lead", cursor=s.cursor("lead"))
                 if t.request_id == "R1"]
    return thread.state


def test_interrupted_before_the_commit_the_result_comes_again_and_the_handoff_is_published_once(tmp_path: Path) -> None:
    s, result = _scenario(tmp_path)
    assert _states(s) == [relay.OWED] and _thread_state(s) == "reply-waiting"
    first = Model(s, result, hand_over=True)

    def die_before_commit() -> None:
        raise Crash("killed at the commit boundary")

    with pytest.raises(Crash):
        _instance(s, first, pre_commit=die_before_commit)
    assert first.deliveries == 1 and first.publications == [True]            # the turn did its work ...
    assert s.cursor("lead") < result                                          # ... but the result is NOT consumed
    assert _thread_state(s) == "reply-waiting"                                # and the thread is still unseen
    assert _states(s) == [relay.CLOSED_FORWARDED]                             # the hand-off is already closed
    second = Model(s, result, hand_over=True)                                 # the relaunched wrapper
    assert _instance(s, second) == 1
    assert second.deliveries == 1                                             # normal redelivery before the commit
    assert second.publications == [False]                                     # the same hand-off: nothing new is sent
    assert len(_publications(s, result)) == 1                                 # exactly one message on the bus
    assert s.cursor("lead") >= result and _thread_state(s) == "closed"
    assert _states(s) == [relay.CLOSED_FORWARDED]


def test_interrupted_before_the_commit_without_a_handoff_the_second_instance_sends_it_once(tmp_path: Path) -> None:
    s, result = _scenario(tmp_path)
    first = Model(s, result, hand_over=False)

    def die_before_commit() -> None:
        raise Crash("killed at the commit boundary")

    with pytest.raises(Crash):
        _instance(s, first, pre_commit=die_before_commit)
    assert first.deliveries == 1 and not _publications(s, result) and _states(s) == [relay.OWED]
    second = Model(s, result, hand_over=True)
    assert _instance(s, second) == 1
    assert second.deliveries == 1 and second.publications == [True] and len(_publications(s, result)) == 1
    assert _states(s) == [relay.CLOSED_FORWARDED] and s.cursor("lead") >= result


def test_interrupted_after_the_commit_the_result_is_not_replayed_and_the_handoff_is_still_owed(tmp_path: Path) -> None:
    s, result = _scenario(tmp_path)
    first = Model(s, result, hand_over=False)                                 # the turn did not hand the decision over

    def die_after_commit() -> None:
        if s.cursor("lead") >= result:                                        # idle stamps before it are harmless
            raise Crash("killed right after the cursor moved")

    with pytest.raises(Crash):
        _instance(s, first, on_runtime_idle=die_after_commit)
    assert first.deliveries == 1 and s.cursor("lead") >= result               # the result is consumed
    assert _thread_state(s) == "closed"                                       # and the thread is seen
    assert _states(s) == [relay.OWED]                                         # but the hand-off is derived, still owed
    second = Model(s, result, hand_over=False)                                # the relaunched wrapper
    assert _instance(s, second) == 0                                          # nothing to deliver: no replay
    assert second.deliveries == 0 and first.deliveries == 1
    assert _states(s) == [relay.OWED] and "OWED RELAY" in relay.notice_for(s, "lead")
    msg, created = relay.publish_handoff(s, "lead", "builder", result_id=result, body="merge it")
    assert created and len(_publications(s, result)) == 1 and _states(s) == [relay.CLOSED_FORWARDED]
    _, again = relay.publish_handoff(s, "lead", "builder", result_id=result, body="merge it")
    assert not again and len(_publications(s, result)) == 1


def test_interrupted_after_the_commit_when_the_first_attempt_already_forwarded_the_closed_state_stays(
        tmp_path: Path) -> None:
    s, result = _scenario(tmp_path)
    first = Model(s, result, hand_over=True)

    def die_after_commit() -> None:
        if s.cursor("lead") >= result:                                        # idle stamps before it are harmless
            raise Crash("killed right after the cursor moved")

    with pytest.raises(Crash):
        _instance(s, first, on_runtime_idle=die_after_commit)
    assert first.publications == [True] and _states(s) == [relay.CLOSED_FORWARDED]
    second = Model(s, result, hand_over=True)
    assert _instance(s, second) == 0 and second.deliveries == 0
    assert _states(s) == [relay.CLOSED_FORWARDED] and len(_publications(s, result)) == 1
