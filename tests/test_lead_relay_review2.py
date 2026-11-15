"""PR #528 connector review, 11 P2 findings: one regression test (at least) per finding.

Each test fails without its fix (mutation-checked in the build log of the fix round). The findings are named in the
test names; the design's follow-up section lists nothing as bounded.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agenttalk import lead_loop_cadence as cad
from agenttalk import lead_relay as relay
from agenttalk import printed_commands as pc
from agenttalk import threads
from agenttalk.store import Store


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "lead2", "reviewer", "builder", "other"])
    s.set_role("lead", "lead")
    return s


def _request(s: Store, rid: str, kind: str = "review-request", **meta) -> str:
    return s.send(sender="lead", recipient="reviewer", kind=kind, body="please", meta=dict(meta, request_id=rid)).id


def _result(s: Store, rid: str) -> str:
    return s.send(sender="reviewer", recipient="lead", kind="review-result", body="done",
                  meta={"request_id": rid, "status": "approved"}).id


def _declare(s: Store, seat: str, rid: str, to: str = "lead") -> str:
    return s.send(sender=seat, recipient=to, body="I wait", meta={"waiting_on": rid}).id


def _states(s: Store, lead: str = "lead") -> dict:
    return {(o.seat, o.request_id): o.state for o in relay.derive(s.valid_messages(), lead)}


# 1 ---------------------------------------------------------------- closings must postdate the declaration
def test_a_hand_off_made_before_the_declaration_does_not_answer_the_later_declaration(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    assert _states(s) == {("builder", "R1"): relay.CLOSED_FORWARDED}
    _declare(s, "builder", "R1")                       # the seat now declares: it was not covered by the old hand-off
    assert _states(s) == {("builder", "R1"): relay.OWED}


def test_a_cancel_and_a_new_declaration_after_a_hand_off_is_owed_again(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    result = _result(s, "R1")
    _declare(s, "builder", "R1")
    relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    s.send(sender="builder", recipient="lead", body="never mind", meta={"waiting_off": "R1"})
    _declare(s, "builder", "R1")
    assert _states(s) == {("builder", "R1"): relay.OWED}


def test_an_unresolved_note_older_than_the_declaration_does_not_close_it(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _declare(s, "builder", "GONE")
    relay.publish_handoff(s, "lead", "builder", request_id="GONE", outcome=relay.OUTCOME_UNRESOLVED)
    s.send(sender="builder", recipient="lead", body="cancel", meta={"waiting_off": "GONE"})
    _declare(s, "builder", "GONE")
    assert _states(s) == {("builder", "GONE"): relay.UNRESOLVED}


# 2 ---------------------------------------------------------------------------- a rescinded request has no result
def test_a_rescinded_request_gives_no_terminal_result_and_nothing_is_owed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    s.send(sender="lead", recipient="reviewer", kind="rescind", body="cancelled", meta={"request_id": "R1"})
    _result(s, "R1")                                    # the responder answers after the cancel
    assert threads.terminal_responses(s.valid_messages(), "lead") == {}
    assert _states(s) == {}
    with pytest.raises(relay.RelayRefused, match="not a finished result"):
        relay.publish_handoff(s, "lead", "builder", result_id=s.valid_messages()[-1].id, body="x")


def test_a_request_rescinded_after_its_result_also_has_no_result(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    _result(s, "R1")
    assert _states(s) == {("builder", "R1"): relay.OWED}
    s.send(sender="lead", recipient="reviewer", kind="rescind", body="cancelled", meta={"request_id": "R1"})
    (thread,) = [t for t in threads.derive_threads(s.valid_messages(), agent="lead", cursor="") if t.request_id == "R1"]
    assert thread.state == "closed-superseded"          # the thread view and the derivation now agree
    assert _states(s) == {}


# 5 ----------------------------------------------------- a broadcast is complete only when every member answered
def _broadcast(s: Store, rid: str, members: list[str]) -> None:
    for member in members:
        s.send(sender="lead", recipient=member, kind="question", body="who can?",
               meta={"request_id": rid, "broadcast_id": "B-" + rid, "audience": "all"})


def _answer(s: Store, member: str, rid: str) -> str:
    return s.send(sender=member, recipient="lead", kind="message", body="I can", meta={"request_id": rid}).id


def test_a_partly_answered_broadcast_gives_no_result_until_every_member_replied(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _broadcast(s, "Q1", ["reviewer", "other"])
    _declare(s, "builder", "Q1")
    _answer(s, "reviewer", "Q1")
    assert threads.terminal_responses(s.valid_messages(), "lead") == {}
    assert _states(s) == {}                              # the thread view says open-outbound; nothing is owed
    last = _answer(s, "other", "Q1")
    ((opener, terminal),) = threads.terminal_responses(s.valid_messages(), "lead").values()
    assert terminal.id == last and opener.kind == "question"
    assert _states(s) == {("builder", "Q1"): relay.OWED}


def test_a_retired_broadcast_member_does_not_hold_the_result_back(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _broadcast(s, "Q1", ["reviewer", "other"])
    _declare(s, "builder", "Q1")
    _answer(s, "reviewer", "Q1")
    assert _states(s) == {}                              # "other" has not answered: still incomplete
    s.retire_agent("other", reason="gone")
    derived = relay.derive(s.valid_messages(), "lead", retired=s.retired_agents())
    assert [o.state for o in derived] == [relay.OWED]


# 3 ------------------------------------------------------- an exact rerun after compaction returns the publication
def test_an_exact_rerun_after_the_request_was_compacted_returns_the_existing_publication(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    first, created = relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    assert created
    archived = s.archive_messages_below(first.id)       # opener and result leave live history, the closing stays
    assert archived and result not in {m.id for m in s.valid_messages()}
    again, created_again = relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    assert not created_again and again.id == first.id
    assert len([m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]) == 1


def test_a_different_payload_after_compaction_is_still_refused(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    first, _ = relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    s.archive_messages_below(first.id)
    with pytest.raises(ValueError):
        relay.publish_handoff(s, "lead", "builder", result_id=result, body="another decision")


# 4 -------------------------------------------------------------------- the seat view reads retained closings
def test_the_seat_view_shows_a_retained_forwarded_closing_as_closed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    result = _result(s, "R1")
    decl = _declare(s, "builder", "R1")
    closing, _ = relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    s.archive_messages_below(decl)                      # request and result are gone; declaration and closing stay
    assert result not in {m.id for m in s.valid_messages()}
    assert [o.state for o in relay.derive(s.valid_messages(), "lead")] == [relay.CLOSED_FORWARDED]
    assert [o.state for o in relay.derive_for_seat(s.valid_messages(), "builder")] == [relay.CLOSED_FORWARDED]
    assert closing.id in {m.id for m in s.valid_messages()}


def test_the_seat_view_shows_a_retained_not_needed_closing_as_closed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    result = _result(s, "R1")
    decl = _declare(s, "builder", "R1")
    relay.publish_handoff(s, "lead", "builder", result_id=result, outcome=relay.OUTCOME_NOT_NEEDED, reason="no")
    s.archive_messages_below(decl)
    assert [o.state for o in relay.derive_for_seat(s.valid_messages(), "builder")] == [relay.CLOSED_NOT_NEEDED]


# 6 ------------------------------------------------------------------ reminder dedup restarts on a new declaration
def _snap(declaration: str, age: float) -> dict:
    return {"relay": [{"seat": "builder", "request_id": "R1", "result_id": "M1", "declaration_id": declaration,
                       "state": "owed", "age_seconds": age}]}


def test_a_redeclared_waiter_gets_its_first_reminder_again(tmp_path: Path) -> None:
    d = relay.RELAY_DEADLINE_SECONDS
    cstate = Store(tmp_path).read_lead_loop_cadence("lead") if False else {"relay_reminded": {}}
    (old,) = cad.cadence_actionable(_snap("D1", 8 * d + 1), cstate, now_epoch=0.0, reminder_after_seconds=1800)
    assert old["bucket"] == 4
    cstate = cad.apply_tick_success(cstate, now_epoch=0.0, reminded_keys=[], escalation_keys=[],
                                    relay_reminded={old["key"]: old["bucket"]})
    assert cad.cadence_actionable(_snap("D1", 8 * d + 5), cstate, now_epoch=0.0, reminder_after_seconds=1800) == []
    (new,) = cad.cadence_actionable(_snap("D2", d + 1), cstate, now_epoch=0.0, reminder_after_seconds=1800)
    assert new["bucket"] == 1 and new["key"] != old["key"]


def test_the_snapshot_names_the_declaration_that_created_each_obligation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _result(s, "R1")
    first = _declare(s, "builder", "R1")
    s.send(sender="builder", recipient="lead", body="off", meta={"waiting_off": "R1"})
    second = _declare(s, "builder", "R1")
    (item,) = cad.build_cadence_snapshot(s, "lead", now_epoch=time.time())["relay"]
    assert item["declaration_id"] == second != first


# 7 ------------------------------------------------------------------------- relay_to must name roster members
def test_relay_to_with_a_mistyped_seat_is_refused_at_send(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="relay_to names 'buildr'"):
        _request(s, "R1", relay_to="builder,buildr")
    with pytest.raises(ValueError, match="relay_to names 'lead'"):
        _request(s, "R2", relay_to="lead")
    assert not [m for m in s.valid_messages() if m.kind == "review-request"]
    _request(s, "R3", relay_to="builder, other")         # members, spaces tolerated
    assert [m.meta["relay_to"] for m in s.valid_messages()] == ["builder, other"]


# 8 --------------------------------------------------------------------- non-finite bucket values are sanitised
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf"), "x", None, -3, [1]])
def test_a_corrupt_bucket_value_never_fails_a_sweep(tmp_path: Path, bad) -> None:
    d = relay.RELAY_DEADLINE_SECONDS
    key = "relay:R1:M1:builder:D1"
    cstate = {"relay_reminded": {key: bad}}
    (item,) = cad.cadence_actionable(_snap("D1", d + 1), cstate, now_epoch=0.0, reminder_after_seconds=1800)
    assert item["bucket"] == 1                            # the bad entry reads as "never reminded"
    cad.apply_tick_success(cstate, now_epoch=0.0, reminded_keys=[], escalation_keys=[],
                           relay_reminded={key: item["bucket"]})


def test_a_corrupt_cadence_file_is_cleaned_at_the_read_boundary(tmp_path: Path) -> None:
    s = _store(tmp_path)
    path = s.lead_loop_cadence_path("lead")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"relay_reminded": {"a": NaN, "b": Infinity, "c": "7", "d": 2, "e": "zz"}}', encoding="utf-8")
    assert s.read_lead_loop_cadence("lead")["relay_reminded"] == {"c": 7, "d": 2}


# 9 ---------------------------------------------------------- relay obligations rotate before the bound applies
def test_more_open_hand_offs_than_the_bound_all_get_their_reminder_over_successive_sweeps(tmp_path: Path) -> None:
    s = _store(tmp_path)
    total = cad.RELAY_ITEMS_PER_SWEEP + 5
    for i in range(total):
        rid = "R%03d" % i
        _request(s, rid, relay_to="builder")
        _result(s, rid)
    now = time.time() + 2 * relay.RELAY_DEADLINE_SECONDS
    snap = cad.build_cadence_snapshot(s, "lead", now_epoch=now)
    assert len(snap["relay"]) == total                    # the snapshot is not cut at the per-sweep bound
    cstate = {"relay_reminded": {}}
    seen: set[str] = set()
    sweeps = 0
    while True:
        items = [i for i in cad.cadence_actionable(snap, cstate, now_epoch=now, reminder_after_seconds=1800)
                 if i["type"] == "relay_overdue"]
        if not items:
            break
        assert len(items) <= cad.RELAY_ITEMS_PER_SWEEP
        seen |= {i["request_id"] for i in items}
        cstate = cad.apply_tick_success(cstate, now_epoch=now, reminded_keys=[], escalation_keys=[],
                                        relay_reminded={i["key"]: i["bucket"] for i in items})
        sweeps += 1
        assert sweeps < 10
    assert len(seen) == total and sweeps == 2             # the five newest were not starved by the twenty oldest


# 11 ------------------------------------------------------------ declarations are scoped to the addressed lead
def test_a_declaration_addressed_to_one_lead_is_not_another_leads_obligation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _declare(s, "builder", "GONE", to="lead")
    assert _states(s, "lead") == {("builder", "GONE"): relay.UNRESOLVED}
    assert _states(s, "lead2") == {}


def test_a_cancel_addressed_to_another_lead_does_not_cancel(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _declare(s, "builder", "GONE", to="lead")
    s.send(sender="builder", recipient="lead2", body="off", meta={"waiting_off": "GONE"})
    assert _states(s, "lead") == {("builder", "GONE"): relay.UNRESOLVED}
    s.send(sender="builder", recipient="lead", body="off", meta={"waiting_off": "GONE"})
    assert _states(s, "lead") == {("builder", "GONE"): relay.CANCELLED}


# 10 (quoting) ---------------------------------------------------- untrusted request ids in command text
def test_a_request_id_with_shell_characters_gets_no_ready_command(tmp_path: Path) -> None:
    s = _store(tmp_path)
    evil = "X; echo pwned #"
    _declare(s, "builder", evil)
    text = relay.notice_for(s, "lead")
    assert "OWED RELAY" in text
    # #727: the command keeps its shape with a placeholder (the odd id is not in it); the id is shown escaped
    assert "relay result --request-id <r>" in text and "relay result --request-id X" not in text
    assert "X; echo pwned" not in text and pc.shown(evil) in text
    _declare(s, "other", "SAFE-1")
    text = relay.notice_for(s, "lead")
    assert "agenttalk relay result --request-id SAFE-1 --to other --unresolved" in text
