"""Owed hand-offs of finished results (lead_relay): derivation from the bus, the atomic publication,
crash and race behaviour.

Design: docs/DESIGN-lead-loop-default.md, sections 5, 6, 11 and 15 (the tests of slice 1).
"""
from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from _lead_loop_barrier import barrier_code, run_released_together
from agenttalk import lead_relay as relay
from agenttalk import signing, threads
from agenttalk import store as store_mod
from agenttalk.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path, agents=("lead", "reviewer", "builder", "other")) -> Store:
    s = Store(tmp_path)
    s.init(list(agents))
    return s


def _request(s: Store, rid: str, kind: str = "review-request", **meta) -> str:
    return s.send(sender="lead", recipient="reviewer", kind=kind, body="please", meta=dict(meta, request_id=rid)).id


def _result(s: Store, rid: str, kind: str = "review-result", status: str = "approved") -> str:
    meta = {"request_id": rid, "status": status}
    return s.send(sender="reviewer", recipient="lead", kind=kind, body="done", meta=meta).id


def _declare(s: Store, seat: str, rid: str, key: str = "waiting_on") -> str:
    return s.send(sender=seat, recipient="lead", body="I wait", meta={key: rid}).id


def _derive(s: Store, lead: str = "lead") -> list[relay.Obligation]:
    return relay.derive(s.valid_messages(), lead, retired=s.retired_agents())


def _states(s: Store) -> dict[tuple[str, str], str]:
    return {(o.seat, o.request_id): o.state for o in _derive(s)}


# ------------------------------------------------------------------------------------------------ derivation
def test_a_declared_waiter_is_owed_once_the_result_has_arrived(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _declare(s, "builder", "R1")
    assert _states(s) == {}                                   # the result has not arrived: nothing is owed yet
    result = _result(s, "R1")
    (ob,) = _derive(s)
    assert (ob.seat, ob.request_id, ob.result_id, ob.state) == ("builder", "R1", result, relay.OWED)


def test_relay_to_on_the_leads_own_request_declares_the_waiters(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder,other")
    _result(s, "R1")
    assert _states(s) == {("builder", "R1"): relay.OWED, ("other", "R1"): relay.OWED}


def test_an_undeclared_waiter_is_invisible(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _result(s, "R1")
    assert _derive(s) == []
    assert relay.notice_for(s, "lead") == ""


def test_a_late_declaration_after_the_result_creates_an_owed_obligation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _result(s, "R1")
    decl = _declare(s, "builder", "R1")
    (ob,) = _derive(s)
    assert ob.state == relay.OWED and ob.declaration_id == decl


def test_waiting_off_cancels_and_a_later_declaration_revives(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _result(s, "R1")
    _declare(s, "builder", "R1")
    _declare(s, "builder", "R1", key="waiting_off")
    assert _states(s) == {("builder", "R1"): relay.CANCELLED}
    _declare(s, "builder", "R1")
    assert _states(s) == {("builder", "R1"): relay.OWED}


def test_a_retired_seat_closes_the_obligation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _result(s, "R1")
    _declare(s, "builder", "R1")
    s.retire_agent("builder")
    assert _states(s) == {("builder", "R1"): relay.CLOSED_RECIPIENT_RETIRED}


@pytest.mark.parametrize("opener,response,meta,is_result", [
    ("task", "task-response", {"status": "accepted"}, False),
    ("task", "task-response", {"status": "done"}, True),
    ("task", "task-response", {"status": "declined"}, True),
    ("task", "task-response", {"verdict": "FIX"}, True),
    ("review-request", "review-result", {"status": "needs-info"}, False),
    ("review-request", "review-result", {"status": "approved"}, True),
    ("proposal", "proposal-response", {"status": "accepted"}, True),
    ("proposal", "proposal-response", {"status": "countered"}, True),
    ("question", "message", {}, True),
])
def test_what_counts_as_a_result_follows_the_response_kind(tmp_path: Path, opener, response, meta, is_result) -> None:
    s = _store(tmp_path)
    s.send(sender="lead", recipient="reviewer", kind=opener, body="x", meta={"request_id": "R1"})
    s.send(sender="reviewer", recipient="lead", kind=response, body="y", meta=dict(meta, request_id="R1"))
    _declare(s, "builder", "R1")
    assert (_states(s) == {("builder", "R1"): relay.OWED}) is is_result


def test_a_declaration_for_a_request_that_is_not_in_live_history_is_unresolved(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _declare(s, "builder", "GONE")
    (ob,) = _derive(s)
    assert (ob.state, ob.result_id) == (relay.UNRESOLVED, None)
    assert "no longer in live history" in "\n".join(relay.notice_lines([ob], now_epoch=time.time()))


def test_another_seats_request_is_not_this_leads_obligation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.send(sender="other", recipient="reviewer", kind="review-request", body="x", meta={"request_id": "X1"})
    _declare(s, "builder", "X1")
    assert _derive(s) == []


def test_a_closing_message_from_the_lead_closes_only_its_recipient(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder,other")
    result = _result(s, "R1")
    msg, created = relay.publish_handoff(s, "lead", "builder", result_id=result, body="merge it")
    assert created
    assert _states(s) == {("builder", "R1"): relay.CLOSED_FORWARDED, ("other", "R1"): relay.OWED}
    relay.publish_handoff(s, "lead", "other", result_id=result, outcome=relay.OUTCOME_NOT_NEEDED, reason="not affected")
    assert _states(s) == {("builder", "R1"): relay.CLOSED_FORWARDED, ("other", "R1"): relay.CLOSED_NOT_NEEDED}
    assert msg.meta["relay_of"] == result


def test_a_closing_message_with_a_wrong_declared_sender_is_not_counted(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    s.send(sender="other", recipient="builder", body="I decide", meta={"relay_of": result, "relay_nonce": "x" * 32})
    assert _states(s) == {("builder", "R1"): relay.OWED}


def test_signing_on_rejects_an_unsigned_closing_message_and_signing_off_counts_the_declared_sender(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key, no_key = tmp_path / "k.key", tmp_path / "absent.key"
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(key))
    s = _store(tmp_path)
    signing.init_key(s.project_id())
    assert s.signing_enforced()
    _request(s, "R1", relay_to="builder")                     # request, result: signed
    result = _result(s, "R1")
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(no_key))        # signing OFF: one unsigned closing message
    assert not s.signing_enforced()
    relay.publish_handoff(s, "lead", "builder", result_id=result, body="decision")
    assert _states(s) == {("builder", "R1"): relay.CLOSED_FORWARDED}          # signing off: policy counts it
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(key))
    assert s.signing_enforced()
    assert _states(s) == {("builder", "R1"): relay.OWED}                       # signing on: the gate rejects it


# ----------------------------------------------------------------------------------------------- publication
def test_sequential_rerun_returns_the_first_message_and_sends_nothing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    first, c1 = relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    second, c2 = relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    assert c1 and not c2 and first.id == second.id
    assert len([m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]) == 1


def test_a_different_payload_under_the_same_identity_is_refused(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    with pytest.raises(ValueError, match="different payload"):
        relay.publish_handoff(s, "lead", "builder", result_id=result, body="stop")
    with pytest.raises(ValueError, match="different payload"):
        relay.publish_handoff(s, "lead", "builder", result_id=result, outcome=relay.OUTCOME_NOT_NEEDED, reason="no")


def test_refusals_are_typed_and_raised_inside_the_lock(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rid = _request(s, "R1", relay_to="builder")
    with pytest.raises(relay.RelayRefused, match="not a finished result"):
        relay.publish_handoff(s, "lead", "builder", result_id=rid, body="x")             # the opener, not a result
    result = _result(s, "R1")
    with pytest.raises(relay.RelayRefused, match="not on the roster"):
        relay.publish_handoff(s, "lead", "ghost", result_id=result, body="x")
    with pytest.raises(relay.RelayRefused, match="itself"):
        relay.publish_handoff(s, "lead", "lead", result_id=result, body="x")
    with pytest.raises(relay.RelayRefused, match="reason"):
        relay.publish_handoff(s, "lead", "builder", result_id=result, outcome=relay.OUTCOME_NOT_NEEDED)
    with pytest.raises(relay.RelayRefused, match="still in live history"):
        relay.publish_handoff(s, "lead", "builder", request_id="R1", outcome=relay.OUTCOME_UNRESOLVED)
    assert not [m for m in s.valid_messages() if (m.meta or {}).get("relay_of")]


def test_unresolved_is_closed_by_exactly_one_note(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _declare(s, "builder", "GONE")
    note, created = relay.publish_handoff(s, "lead", "builder", request_id="GONE", outcome=relay.OUTCOME_UNRESOLVED)
    again, created2 = relay.publish_handoff(s, "lead", "builder", request_id="GONE", outcome=relay.OUTCOME_UNRESOLVED)
    assert created and not created2 and note.id == again.id
    assert note.meta["relay_outcome"] == "unresolved" and "no longer in live history" in note.body
    assert _states(s) == {("builder", "GONE"): relay.CLOSED_UNRESOLVED}


def _publish_script(root: Path, barrier_dir: Path, result: str, tag: str) -> str:
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from pathlib import Path
        from agenttalk import lead_relay as relay
        from agenttalk.store import Store
        s = Store(Path({str(root)!r}))
    """) + barrier_code(barrier_dir, tag) + textwrap.dedent("""
        m, created = relay.publish_handoff(s, "lead", "builder", result_id=%r, body="go")
        print("CREATED" if created else "EXISTING", m.id)
    """ % result)


def test_two_processes_released_together_publish_exactly_one_message(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    barrier = tmp_path.parent / (tmp_path.name + "-barrier")
    outs = run_released_together(barrier, [_publish_script(tmp_path, barrier, result, str(i)) for i in range(2)])
    assert [o[0] for o in outs] == [0, 0], outs
    assert sorted(o[1].split()[0] for o in outs) == ["CREATED", "EXISTING"], outs
    assert len([m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]) == 1


def test_crash_between_the_send_and_the_intent_update_is_repaired_by_the_rerun(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    real = store_mod._atomic_write_text
    calls = {"n": 0}

    def flaky(path, text, *a, **k):
        if "operation-intents" in str(path):
            calls["n"] += 1
            if calls["n"] == 2:                                       # the intent update after the send
                raise OSError("simulated crash after send")
        return real(path, text, *a, **k)

    monkeypatch.setattr(store_mod, "_atomic_write_text", flaky)
    with pytest.raises(OSError, match="simulated"):
        relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    monkeypatch.setattr(store_mod, "_atomic_write_text", real)
    sent = [m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]
    assert len(sent) == 1                                              # the message is on the bus
    msg, created = relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    assert not created and msg.id == sent[0].id
    assert len([m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]) == 1


def test_crash_before_the_send_leaves_no_message_and_the_rerun_sends_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    real_send = Store.send

    def boom(self, *a, **k):
        raise RuntimeError("simulated crash before the send")

    monkeypatch.setattr(Store, "send", boom)
    with pytest.raises(RuntimeError):
        relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    monkeypatch.setattr(Store, "send", real_send)
    assert not [m for m in s.valid_messages() if (m.meta or {}).get("relay_of")]
    _, created = relay.publish_handoff(s, "lead", "builder", result_id=result, body="go")
    assert created
    assert len([m for m in s.valid_messages() if (m.meta or {}).get("relay_of") == result]) == 1


# --------------------------------------------------------------------------------------------- reminders, notice
def test_reminder_buckets_grow_at_1_2_4_8_times_the_deadline_then_daily() -> None:
    d = relay.RELAY_DEADLINE_SECONDS
    ages = (0, d - 1, d, 2 * d, 4 * d, 8 * d, 8 * d + 86400, 8 * d + 3 * 86400)
    assert [relay.reminder_bucket(a, d) for a in ages] == [0, 0, 1, 2, 3, 4, 5, 7]


def test_notice_is_empty_when_nothing_is_open_and_bounded_when_much_is(tmp_path: Path) -> None:
    obs = [relay.Obligation("lead", "s%d" % i, "R%d" % i, "M%d" % i, relay.OWED, "2026-10-17T10:00:00Z", "D%d" % i)
           for i in range(14)]
    assert relay.notice_lines([], now_epoch=0) == []
    lines = relay.notice_lines(obs, now_epoch=time.time())
    assert lines[0].startswith("== OWED RELAY") and any("and 4 more" in x for x in lines)
    closed = [relay.Obligation("lead", "s", "R", "M", relay.CLOSED_FORWARDED, "2026-10-17T10:00:00Z", "D")]
    assert relay.notice_lines(closed, now_epoch=0) == []


def test_threads_terminal_responses_does_not_depend_on_the_readers_cursor(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    result = _result(s, "R1")
    found = threads.terminal_responses(s.valid_messages(), "lead")
    assert found["R1"][1].id == result
    assert threads.terminal_responses(s.valid_messages(), "reviewer") == {}


# --------------------------------------------------- fix round 1: re-ask, and the full closing tuple (P2-1, P2-5)
@pytest.mark.parametrize("opener, response, meta", [
    ("review-request", "review-result", {"status": "approved"}),
    ("task", "task-response", {"status": "done"}),
    ("proposal", "proposal-response", {"status": "accepted"}),
])
def test_a_reask_after_a_terminal_result_drops_the_old_result(tmp_path: Path, opener: str, response: str,
                                                              meta: dict) -> None:
    s = _store(tmp_path)
    _request(s, "R1", kind=opener, relay_to="builder")
    old = s.send(sender="reviewer", recipient="lead", kind=response, body="done", meta=dict(meta, request_id="R1")).id
    assert [t.id for _, t in threads.terminal_responses(s.valid_messages(), "lead").values()] == [old]
    assert _states(s) == {("builder", "R1"): relay.OWED}
    _request(s, "R1", kind=opener, relay_to="builder")                       # the re-ask: same id, same direction
    assert threads.terminal_responses(s.valid_messages(), "lead") == {}
    assert _states(s) == {}                                                   # nothing owed while the new round is open
    with pytest.raises(relay.RelayRefused, match="not a finished result"):
        relay.publish_handoff(s, "lead", "builder", result_id=old, body="act on the old result")
    assert not [m for m in s.valid_messages() if (m.meta or {}).get("relay_of")]
    new = s.send(sender="reviewer", recipient="lead", kind=response, body="again", meta=dict(meta, request_id="R1")).id
    assert [t.id for _, t in threads.terminal_responses(s.valid_messages(), "lead").values()] == [new]


def test_the_reask_rule_is_the_one_derive_threads_uses(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1")
    _result(s, "R1")
    _request(s, "R1")
    (thread,) = [t for t in threads.derive_threads(s.valid_messages(), agent="lead", cursor="") if t.request_id == "R1"]
    assert thread.state != "closed" and "R1" not in threads.terminal_responses(s.valid_messages(), "lead")


def _closing_meta(result: str, **over) -> dict:
    meta = {"relay_of": result, "relay_nonce": relay.handoff_nonce("lead", result, "builder", relay.OUTCOME_FORWARDED)}
    meta.update(over)
    return {k: v for k, v in meta.items() if v is not None}


@pytest.mark.parametrize("label", ["no nonce", "wrong nonce", "nonce of another seat", "unknown outcome",
                                   "not-needed without a reason"])
def test_a_partial_closing_marker_closes_nothing(tmp_path: Path, label: str) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    body = "a note"
    if label == "no nonce":
        meta = _closing_meta(result, relay_nonce=None)
    elif label == "wrong nonce":
        meta = _closing_meta(result, relay_nonce="0" * 32)
    elif label == "nonce of another seat":
        meta = _closing_meta(result, relay_nonce=relay.handoff_nonce("lead", result, "other", relay.OUTCOME_FORWARDED))
    elif label == "unknown outcome":
        meta = _closing_meta(result, relay_outcome="banana")
    else:
        body = " "
        meta = _closing_meta(result, relay_outcome="not-needed",
                             relay_nonce=relay.handoff_nonce("lead", result, "builder", relay.OUTCOME_NOT_NEEDED))
    s.send(sender="lead", recipient="builder", body=body, meta=meta)
    assert _states(s) == {("builder", "R1"): relay.OWED}, label
    assert [o.state for o in relay.derive_for_seat(s.valid_messages(), "builder")] == [relay.OWED]


def test_the_full_closing_tuple_closes_and_an_absent_outcome_means_forwarded(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _request(s, "R1", relay_to="builder")
    result = _result(s, "R1")
    s.send(sender="lead", recipient="builder", body="merge it", meta=_closing_meta(result))
    assert _states(s) == {("builder", "R1"): relay.CLOSED_FORWARDED}


def test_an_unresolved_closing_needs_the_full_tuple_too(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _declare(s, "builder", "GONE")
    s.send(sender="lead", recipient="builder", body="unresolved",
           meta={"relay_of": "GONE", "relay_outcome": "unresolved"})
    assert _states(s) == {("builder", "GONE"): relay.UNRESOLVED}
    assert [o.state for o in relay.derive_for_seat(s.valid_messages(), "builder")] == [relay.UNRESOLVED]
    relay.publish_handoff(s, "lead", "builder", request_id="GONE", outcome=relay.OUTCOME_UNRESOLVED)
    assert _states(s) == {("builder", "GONE"): relay.CLOSED_UNRESOLVED}
