from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk import intents
from agenttalk.store import PROC_ALIVE, PROC_DEAD, Store, _new_id


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "dev", "reviewer"])
    s.set_role("lead", "lead")
    return s


def test_write_intent_rejects_reserved_payload_without_file(tmp_path: Path) -> None:
    s = _store(tmp_path)

    with pytest.raises(ValueError, match="reserved/control"):
        s.write_intent("send", {"target": "dev", "body": "x", "meta": {"request_id": "q-1"}})

    assert s.list_intents() == []


def test_resolve_web_actor_prefers_operator_facing_then_sole_lead(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert intents.resolve_web_actor(s) == "lead"

    s.set_operator_facing("dev")
    assert intents.resolve_web_actor(s) == "dev"

    cfg = s.load_config()
    cfg.pop("operator_facing", None)
    cfg["roles"] = {"lead": "lead", "dev": "lead"}
    s._write_config(cfg)
    assert intents.resolve_web_actor(s) is None
    assert intents.web_actor_denial(s)[0] == "multiple_leads_configured"


def test_drain_send_uses_derived_actor_and_applies(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello", "message_kind": "question"})

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["applied"] == 1
    sent = s.messages_for("dev")
    assert len(sent) == 1
    assert sent[0].sender == "lead"
    assert sent[0].meta["web_intent_id"] == rec["intent_id"]
    assert sent[0].meta["executor_marker"] == intents.EXECUTOR_MARKER
    assert "epoch_at_send" in sent[0].meta
    assert s.read_intent(rec["intent_id"])["state"] == Store.INTENT_APPLIED


def test_disallowed_bus_kinds_are_unrepresentable(tmp_path: Path) -> None:
    s = _store(tmp_path)

    with pytest.raises(ValueError, match="not allowed"):
        s.write_intent("send", {"target": "dev", "body": "x", "message_kind": "release"})
    with pytest.raises(ValueError, match="not allowed"):
        s.write_intent("reply", {"to_request": "q-1", "body": "x", "reply_kind": "review-result"})
    with pytest.raises(ValueError, match="unknown intent kind"):
        s.write_intent("escalate", {"target": "dev", "body": "x"})

    assert s.list_intents() == []


def test_broadcast_plan_is_frozen_in_intent_file(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_role("reviewer", "reviewer")
    rec = s.write_intent(
        "broadcast",
        {"audience": {"kind": "role", "value": "reviewer"}, "body": "please review"},
    )

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["applied"] == 1
    stored = s.read_intent(rec["intent_id"])
    assert stored["state"] == Store.INTENT_APPLIED
    plan = stored["plan"]
    assert plan["actor"] == "lead"
    assert [d["recipient"] for d in plan["deliveries"]] == ["reviewer"]
    assert plan["deliveries"][0]["stable_meta"]["audience_kind"] == "role"
    assert plan["deliveries"][0]["stable_meta"]["audience_resolved"] == "reviewer"


def test_crash_after_send_before_record_is_reconciled_without_duplicate(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    actor = intents.resolve_web_actor(s)
    assert actor == "lead"
    plan = intents.build_plan(s, actor, rec)
    delivery = plan["deliveries"][0]
    iid = rec["intent_id"]

    s.update_intent(iid, lambda r: r.update({"plan": plan}))
    floor = _new_id()
    fp = intents.delivery_fingerprint(
        intent_id=iid, delivery_index=0, actor=actor,
        recipient=delivery["recipient"], bus_kind=delivery["bus_kind"],
        subject=delivery["subject"], body=delivery["body"],
        stable_meta=delivery["stable_meta"])
    s.update_intent(iid, lambda r: r.update({"deliveries": [{
        "delivery_index": 0, "state": "attempting", "attempt_floor": floor,
        "fingerprint": fp, "recipient": delivery["recipient"],
        "bus_kind": delivery["bus_kind"],
    }]}))
    meta = dict(delivery["stable_meta"])
    meta.update({
        "web_intent_id": iid,
        "web_intent_delivery_index": "0",
        "web_intent_fingerprint": fp,
        "web_intent_attempt_floor": floor,
        "executor_marker": intents.EXECUTOR_MARKER,
    })
    sent = s.send(sender=actor, recipient=delivery["recipient"], body=delivery["body"],
                  kind=delivery["bus_kind"], subject=delivery["subject"], meta=meta)

    summary = intents.drain_intents(s, pid=124, max_per_tick=1)

    assert summary["applied"] == 1
    assert [m.id for m in s.messages_for("dev")] == [sent.id]
    delivery_state = s.read_intent(iid)["deliveries"][0]
    assert delivery_state["state"] == "delivered"
    assert delivery_state["message_id"] == sent.id


def test_recovery_requires_matching_stable_meta(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.write_intent(
        "send",
        {"target": "dev", "body": "hello", "message_kind": "question"},
    )
    actor = intents.resolve_web_actor(s)
    assert actor == "lead"
    plan = intents.build_plan(s, actor, rec)
    delivery = plan["deliveries"][0]
    iid = rec["intent_id"]

    s.update_intent(iid, lambda r: r.update({"plan": plan}))
    floor = _new_id()
    fp = intents.delivery_fingerprint(
        intent_id=iid, delivery_index=0, actor=actor,
        recipient=delivery["recipient"], bus_kind=delivery["bus_kind"],
        subject=delivery["subject"], body=delivery["body"],
        stable_meta=delivery["stable_meta"])
    s.update_intent(iid, lambda r: r.update({"deliveries": [{
        "delivery_index": 0, "state": "attempting", "attempt_floor": floor,
        "fingerprint": fp, "recipient": delivery["recipient"],
        "bus_kind": delivery["bus_kind"],
    }]}))
    meta = dict(delivery["stable_meta"])
    meta["request_id"] = "q-forged"
    meta.update({
        "web_intent_id": iid,
        "web_intent_delivery_index": "0",
        "web_intent_fingerprint": fp,
        "web_intent_attempt_floor": floor,
        "executor_marker": intents.EXECUTOR_MARKER,
    })
    forged = s.send(sender=actor, recipient=delivery["recipient"], body=delivery["body"],
                    kind=delivery["bus_kind"], subject=delivery["subject"], meta=meta)

    summary = intents.drain_intents(s, pid=124, max_per_tick=1)

    assert summary["applied"] == 1
    messages = s.messages_for("dev")
    assert len(messages) == 2
    delivery_state = s.read_intent(iid)["deliveries"][0]
    assert delivery_state["state"] == "delivered"
    assert delivery_state["message_id"] != forged.id


def test_claimed_intent_reclaims_only_confirmed_dead_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    assert s.claim_intent(rec["intent_id"], pid=111, now_epoch=1.0) is not None

    monkeypatch.setattr("agenttalk.store._process_liveness", lambda pid: PROC_ALIVE)
    assert s.claim_intent(rec["intent_id"], pid=222, claim_stale_after=0.0, now_epoch=1000.0) is None

    monkeypatch.setattr("agenttalk.store._process_liveness", lambda pid: PROC_DEAD)
    reclaimed = s.claim_intent(rec["intent_id"], pid=222, claim_stale_after=0.0, now_epoch=1001.0)
    assert reclaimed is not None
    assert reclaimed["claim"]["pid"] == 222


def test_supervisor_instance_release_requires_matching_token_pid_and_start(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.claim_supervisor_instance(pid=123, pid_start="start-a")
    assert rec is not None

    assert not s.release_supervisor_instance(token=rec["token"], pid=999, pid_start="start-a")
    assert not s.release_supervisor_instance(token=rec["token"], pid=123, pid_start="other")
    assert s.read_supervisor_instance() is not None
    assert s.release_supervisor_instance(token=rec["token"], pid=123, pid_start="start-a")
    assert s.read_supervisor_instance() is None
