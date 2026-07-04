from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk import intents
from agenttalk.store import PROC_ALIVE, PROC_DEAD, Store, _new_id


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "dev", "reviewer"])
    s.set_role("lead", "lead")
    return s


def _write_raw_intent(store: Store, record: dict) -> None:
    store.intents_active_dir.mkdir(parents=True, exist_ok=True)
    (store.intents_active_dir / f"{record['intent_id']}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8")


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


@pytest.mark.parametrize("bus_kind", ["release", "review-result"])
def test_frozen_plan_revalidation_rejects_forged_bus_kind_and_control_meta(
    tmp_path: Path, bus_kind: str,
) -> None:
    s = _store(tmp_path)
    rec = {
        "schema_version": 1,
        "intent_id": "wi-forged",
        "kind": "send",
        "payload": {"target": "dev", "body": "hello"},
        "created_at": "2026-07-04T00:00:00.000000Z",
        "state": Store.INTENT_QUEUED,
        "attempts": 0,
        "deliveries": [],
        "plan": {
            "actor": "lead",
            "deliveries": [{
                "recipient": "dev",
                "bus_kind": bus_kind,
                "subject": "",
                "body": "hello",
                "stable_meta": {"release_authority": "yes"},
            }],
        },
    }
    _write_raw_intent(s, rec)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent("wi-forged")
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == "plan_revalidation_failed"
    assert stored["error"].startswith("bus_kind_drift:")
    assert s.messages_for("dev") == []


def test_frozen_plan_revalidation_ignores_delivery_state_as_authority(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    rec = {
        "schema_version": 1,
        "intent_id": "wi-forged-delivered",
        "kind": "send",
        "payload": {"target": "dev", "body": "hello"},
        "created_at": "2026-07-04T00:00:00.000000Z",
        "state": Store.INTENT_QUEUED,
        "attempts": 0,
        "deliveries": [{
            "delivery_index": 0,
            "state": "delivered",
            "message_id": "20260704-000000-000000-AbCd",
        }],
        "plan": {
            "actor": "lead",
            "deliveries": [{
                "recipient": "dev",
                "bus_kind": "release",
                "subject": "",
                "body": "hello",
                "stable_meta": {"release_authority": "yes"},
            }],
        },
    }
    _write_raw_intent(s, rec)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent("wi-forged-delivered")
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == "plan_revalidation_failed"
    assert stored["error"].startswith("bus_kind_drift:")
    assert s.messages_for("dev") == []


def test_frozen_plan_revalidation_rejects_forged_broadcast_audience(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    s.set_role("reviewer", "reviewer")
    rec = {
        "schema_version": 1,
        "intent_id": "wi-broadcast-forged",
        "kind": "broadcast",
        "payload": {
            "audience": {"kind": "role", "value": "reviewer"},
            "body": "please review",
        },
        "created_at": "2026-07-04T00:00:00.000000Z",
        "state": Store.INTENT_QUEUED,
        "attempts": 0,
        "deliveries": [],
        "plan": {
            "actor": "lead",
            "deliveries": [{
                "recipient": "dev",
                "bus_kind": "message",
                "subject": "",
                "body": "please review",
                "stable_meta": {
                    "broadcast_id": "b-forged",
                    "request_id": "b-forged",
                    "audience_kind": "role",
                    "audience_resolved": "dev",
                    "batch_total": "1",
                },
            }],
        },
    }
    _write_raw_intent(s, rec)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent("wi-broadcast-forged")
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == "plan_revalidation_failed"
    assert stored["error"].startswith("recipient_drift:")
    assert s.messages_for("dev") == []
    assert s.messages_for("reviewer") == []


def test_frozen_plan_revalidation_rejects_forged_reply_recipient(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    s.send(sender="dev", recipient="lead", kind="question",
           body="anchor", meta={"request_id": "q-anchor"})
    rec = {
        "schema_version": 1,
        "intent_id": "wi-reply-forged",
        "kind": "reply",
        "payload": {"to_request": "q-anchor", "body": "answer"},
        "created_at": "2026-07-04T00:00:00.000000Z",
        "state": Store.INTENT_QUEUED,
        "attempts": 0,
        "deliveries": [],
        "plan": {
            "actor": "lead",
            "deliveries": [{
                "recipient": "reviewer",
                "bus_kind": "message",
                "subject": "",
                "body": "answer",
                "stable_meta": {"request_id": "q-anchor"},
            }],
        },
    }
    _write_raw_intent(s, rec)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent("wi-reply-forged")
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == "plan_revalidation_failed"
    assert stored["error"].startswith("recipient_drift:")
    assert s.messages_for("reviewer") == []


def test_frozen_plan_revalidation_requires_opener_epoch_key(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = {
        "schema_version": 1,
        "intent_id": "wi-question-no-epoch",
        "kind": "send",
        "payload": {"target": "dev", "body": "hello", "message_kind": "question"},
        "created_at": "2026-07-04T00:00:00.000000Z",
        "state": Store.INTENT_QUEUED,
        "attempts": 0,
        "deliveries": [],
        "plan": {
            "actor": "lead",
            "deliveries": [{
                "recipient": "dev",
                "bus_kind": "question",
                "subject": "",
                "body": "hello",
                "stable_meta": {"request_id": "q-forged"},
            }],
        },
    }
    _write_raw_intent(s, rec)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent("wi-question-no-epoch")
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == "plan_revalidation_failed"
    assert stored["error"].startswith("stable_meta_shape:")
    assert s.messages_for("dev") == []


def test_valid_frozen_plan_preserves_minted_ids(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.write_intent(
        "send",
        {"target": "dev", "body": "hello", "message_kind": "question"},
    )
    actor = intents.resolve_web_actor(s)
    assert actor == "lead"
    plan = intents.build_plan(s, actor, rec)
    request_id = plan["deliveries"][0]["stable_meta"]["request_id"]
    s.update_intent(rec["intent_id"], lambda r: r.update({"plan": plan}))

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["applied"] == 1
    stored = s.read_intent(rec["intent_id"])
    assert stored["plan"]["deliveries"][0]["stable_meta"]["request_id"] == request_id
    assert s.messages_for("dev")[0].meta["request_id"] == request_id


@pytest.mark.parametrize(("kind", "payload", "code"), [
    ("send", {"target": "ghost", "body": "hello"}, "target_not_in_roster"),
    ("reply", {"to_request": "q-missing", "body": "answer"}, "reply_anchor_not_found"),
    ("broadcast", {"audience": {"kind": "role", "value": "ghost"}, "body": "hello"}, "empty_audience"),
])
def test_drain_denial_codes_are_pinned(
    tmp_path: Path, kind: str, payload: dict, code: str,
) -> None:
    s = _store(tmp_path)
    rec = s.write_intent(kind, payload)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent(rec["intent_id"])
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == code
    assert s.messages_for("dev") == []
    assert s.messages_for("reviewer") == []


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


def test_claimed_intent_reclaims_live_reused_pid_only_with_start_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    new = "2026-07-04T00:00:01.000000Z"
    s = _store(tmp_path / "known-different")
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    assert s.claim_intent(
        rec["intent_id"], pid=111, pid_start=old, now_epoch=1.0) is not None
    monkeypatch.setattr("agenttalk.store._process_liveness", lambda pid: PROC_ALIVE)
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: new)

    reclaimed = s.claim_intent(
        rec["intent_id"], pid=222, claim_stale_after=0.0, now_epoch=1000.0)

    assert reclaimed is not None
    assert reclaimed["claim"]["pid"] == 222

    s2 = _store(tmp_path / "unknown-start")
    rec2 = s2.write_intent("send", {"target": "dev", "body": "hello"})
    assert s2.claim_intent(
        rec2["intent_id"], pid=111, pid_start=old, now_epoch=1.0) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: None)

    assert s2.claim_intent(
        rec2["intent_id"], pid=222, claim_stale_after=0.0,
        now_epoch=1000.0) is None


def test_claimed_intent_same_pid_unknown_start_blocks_refresh_and_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    new = "2026-07-04T00:00:01.000000Z"
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    assert s.claim_intent(
        rec["intent_id"], pid=111, pid_start=old, now_epoch=1.0) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: None)

    summary = intents.drain_intents(
        s, pid=111, pid_start=new, max_per_tick=1, now_epoch=1000.0)

    assert summary["claimed"] == 0
    assert summary["skipped"] == 1
    assert s.messages_for("dev") == []


def test_claimed_intent_same_pid_matching_start_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    assert s.claim_intent(
        rec["intent_id"], pid=111, pid_start=old, now_epoch=1.0) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: None)

    summary = intents.drain_intents(
        s, pid=111, pid_start=old, max_per_tick=1, now_epoch=1000.0)

    assert summary["applied"] == 1
    messages = s.messages_for("dev")
    assert len(messages) == 1
    assert messages[0].sender == "lead"


def test_claimed_intent_same_pid_confident_reuse_reclaims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    new = "2026-07-04T00:00:01.000000Z"
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    assert s.claim_intent(
        rec["intent_id"], pid=111, pid_start=old, now_epoch=1.0) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: new)

    summary = intents.drain_intents(
        s, pid=111, pid_start=new, max_per_tick=1, now_epoch=1000.0)

    assert summary["applied"] == 1
    assert len(s.messages_for("dev")) == 1


def test_live_claimed_intent_blocks_second_drainer_and_sends_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "2026-07-04T00:00:00.000000Z"
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    assert s.claim_intent(
        rec["intent_id"], pid=111, pid_start=start, now_epoch=1.0) is not None
    monkeypatch.setattr("agenttalk.store._process_liveness", lambda pid: PROC_ALIVE)

    skipped = intents.drain_intents(s, pid=222, max_per_tick=1, now_epoch=1000.0)
    applied = intents.drain_intents(
        s, pid=111, pid_start=start, max_per_tick=1, now_epoch=1001.0)
    again = intents.drain_intents(s, pid=222, max_per_tick=1, now_epoch=1002.0)

    assert skipped["skipped"] == 1
    assert applied["applied"] == 1
    assert again["examined"] == 0
    assert len(s.messages_for("dev")) == 1


def test_supervisor_instance_reclaims_only_known_reused_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    new = "2026-07-04T00:00:01.000000Z"
    s = _store(tmp_path / "known-different")
    first = s.claim_supervisor_instance(pid=111, pid_start=old)
    assert first is not None
    monkeypatch.setattr("agenttalk.store._process_liveness", lambda pid: PROC_ALIVE)
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: new)

    second = s.claim_supervisor_instance(pid=222, pid_start=new)

    assert second is not None
    assert second["pid"] == 222

    s2 = _store(tmp_path / "unknown-start")
    assert s2.claim_supervisor_instance(pid=111, pid_start=old) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: None)

    assert s2.claim_supervisor_instance(pid=222, pid_start=new) is None


def test_supervisor_instance_same_pid_unknown_start_blocks_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    new = "2026-07-04T00:00:01.000000Z"
    s = _store(tmp_path)
    assert s.claim_supervisor_instance(pid=111, pid_start=old) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: None)

    assert s.claim_supervisor_instance(pid=111, pid_start=new) is None


def test_supervisor_instance_same_pid_matching_start_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    s = _store(tmp_path)
    first = s.claim_supervisor_instance(pid=111, pid_start=old)
    assert first is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: None)

    second = s.claim_supervisor_instance(pid=111, pid_start=old)

    assert second is not None
    assert second["pid"] == 111
    assert second["pid_start"] == old
    assert second["token"] != first["token"]


def test_supervisor_instance_same_pid_confident_reuse_reclaims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "2026-07-04T00:00:00.000000Z"
    new = "2026-07-04T00:00:01.000000Z"
    s = _store(tmp_path)
    assert s.claim_supervisor_instance(pid=111, pid_start=old) is not None
    monkeypatch.setattr("agenttalk.store._process_start_token", lambda pid: new)

    second = s.claim_supervisor_instance(pid=111, pid_start=new)

    assert second is not None
    assert second["pid"] == 111
    assert second["pid_start"] == new


def test_supervisor_instance_release_requires_matching_token_pid_and_start(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.claim_supervisor_instance(pid=123, pid_start="start-a")
    assert rec is not None

    assert not s.release_supervisor_instance(token=rec["token"], pid=999, pid_start="start-a")
    assert not s.release_supervisor_instance(token=rec["token"], pid=123, pid_start="other")
    assert s.read_supervisor_instance() is not None
    assert s.release_supervisor_instance(token=rec["token"], pid=123, pid_start="start-a")
    assert s.read_supervisor_instance() is None


def test_drain_quarantines_invalid_active_intent_before_claim(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.intents_active_dir.mkdir(parents=True, exist_ok=True)
    bad = s.intents_active_dir / "wi-bad.json"
    bad.write_text("{not-json", encoding="utf-8")

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["quarantined_invalid"] == 1
    assert not bad.exists()
    sink = s.control_audit_dir / "intents-invalid"
    payloads = [p for p in sink.glob("*.json") if not p.name.endswith(".meta.json")]
    assert len(payloads) == 1
    assert payloads[0].read_text(encoding="utf-8") == "{not-json"
    meta = json.loads((sink / f"{payloads[0].name}.meta.json").read_text(encoding="utf-8"))
    assert meta["original_name"] == "wi-bad.json"
    assert meta["reason"] == "invalid_active_intent"
    assert "parse_error" in meta
    s.reset()
    assert payloads[0].exists()
    assert (sink / f"{payloads[0].name}.meta.json").exists()


def test_drain_kill_switch_active_short_circuits_without_mutation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    s.intents_active_dir.mkdir(parents=True, exist_ok=True)
    bad = s.intents_active_dir / "wi-bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    (s.dir / "supervisor.kill").write_text("stop", encoding="utf-8")

    summary = intents.drain_intents(s, pid=123, max_per_tick=10)

    assert summary["disabled"] is True
    assert summary["disabled_reason"] == "kill_switch"
    assert summary["examined"] == 0
    assert summary["claimed"] == 0
    assert summary["applied"] == 0
    assert summary["quarantined_invalid"] == 0
    assert summary["rotation"] == {"rotated": 0, "audit_dropped": 0, "quarantined_invalid": 0}
    assert s.read_intent(rec["intent_id"])["state"] == Store.INTENT_QUEUED
    assert bad.exists()
    assert s.messages_for("dev") == []
    assert not (s.control_audit_dir / "intents-invalid").exists()


def test_drain_kill_switch_unreadable_fails_closed_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    s.intents_active_dir.mkdir(parents=True, exist_ok=True)
    bad = s.intents_active_dir / "wi-bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(s, "supervisor_kill_switch", lambda: None)

    summary = intents.drain_intents(s, pid=123, max_per_tick=10)

    assert summary["disabled"] is True
    assert summary["disabled_reason"] == "kill_switch_unreadable"
    assert summary["examined"] == 0
    assert summary["claimed"] == 0
    assert summary["applied"] == 0
    assert summary["quarantined_invalid"] == 0
    assert s.read_intent(rec["intent_id"])["state"] == Store.INTENT_QUEUED
    assert bad.exists()
    assert s.messages_for("dev") == []
    assert not (s.control_audit_dir / "intents-invalid").exists()


def test_rotate_quarantines_terminal_intent_with_unparseable_timestamp(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    rec = s.write_intent("send", {"target": "dev", "body": "hello"})
    s.update_intent(rec["intent_id"], lambda r: r.update({
        "state": Store.INTENT_DENIED,
        "code": "invalid_payload",
        "terminal_at": "not-a-date",
    }))

    result = s.rotate_intents(now_epoch=1000.0, terminal_linger_seconds=0.0)

    assert result["quarantined_invalid"] == 1
    assert s.read_intent(rec["intent_id"]) is None
    sink = s.control_audit_dir / "intents-invalid"
    payloads = [p for p in sink.glob("*.json") if not p.name.endswith(".meta.json")]
    assert len(payloads) == 1
    meta = json.loads((sink / f"{payloads[0].name}.meta.json").read_text(encoding="utf-8"))
    assert meta["reason"] == "invalid_intent_timestamp"


def test_rotate_intents_byte_eviction_sorts_by_mtime_then_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path)
    audit = s.control_audit_dir / "intents"
    audit.mkdir(parents=True, exist_ok=True)
    # Names sort opposite to mtimes: filename order would drop wi-a-new first.
    files = {
        "wi-a-new.json": (300.0, "n" * 20),
        "wi-b-old.json": (100.0, "o" * 20),
        "wi-c-mid.json": (200.0, "m" * 20),
    }
    for name, (mtime, body) in files.items():
        p = audit / name
        p.write_text(body, encoding="utf-8")
        os.utime(p, (mtime, mtime))
    monkeypatch.setattr(Store, "INTENT_AUDIT_MAX_BYTES", 45)
    monkeypatch.setattr(Store, "INTENT_AUDIT_MAX_AGE_SECONDS", 10_000.0)

    result = s.rotate_intents(now_epoch=350.0, terminal_linger_seconds=0.0)

    assert result["audit_dropped"] == 1
    assert not (audit / "wi-b-old.json").exists()
    assert (audit / "wi-a-new.json").exists()
    assert (audit / "wi-c-mid.json").exists()
