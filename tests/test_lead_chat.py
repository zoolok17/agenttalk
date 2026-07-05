from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import cli, doctor, health as hm, intents, supervisor as sup, web
from agenttalk.store import Store


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "dev", "reviewer"])
    s.set_role("lead", "lead")
    s.set_role("dev", "developer")
    s.set_role("reviewer", "reviewer")
    s.set_group("reviewers", ["reviewer"])
    return s


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _available(store: Store, agent: str = "lead") -> None:
    ts = _now_iso()
    store.write_heartbeat(agent)
    store.write_health(agent, hm.build_snapshot(
        agent=agent,
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_IDLE_WAITING,
        updated_at=ts,
        since=ts,
        last_progress_at=ts,
        reason_code=hm.STATE_IDLE_WAITING,
    ))


def _stale_heartbeat(store: Store, agent: str = "lead") -> None:
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    (store.state_dir / f"{agent}.heartbeat").write_text(
        old.isoformat().replace("+00:00", "Z"), encoding="utf-8")


def _lead_question(
    store: Store, rid: str = "esc-choice", *, sender: str = "lead"
) -> None:
    store.send(
        sender=sender,
        recipient="operator",
        kind="question",
        subject="operator input needed",
        body="body visible in lead chat only",
        meta={
            "request_id": rid,
            "needs_operator": "true",
            "attention": {
                "decision": "Choose release path",
                "recommendation": "ship the narrow fix",
                "priority": "urgent",
                "options": ["ship", "hold"],
            },
        },
    )


def _session(base: str) -> str:
    with urllib.request.urlopen(f"{base}/api/session", timeout=5) as resp:  # noqa: S310
        assert resp.status == 200
        return json.loads(resp.read())["csrf_token"]


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
        assert resp.status == 200
        return json.loads(resp.read())


def _post_lead_chat(base: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        f"{base}/api/lead-chat",
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": token,
            "Origin": base,
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        assert resp.status == 202
        return json.loads(resp.read())


def test_operator_identity_has_no_fallback_and_stable_lc_request_id(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)

    assert s.operator_identity(lead="lead") == "operator"
    rid = s.lead_chat_request_id(operator="operator", lead="lead")
    assert rid.startswith("lc-")
    assert rid == s.lead_chat_request_id(operator="operator", lead="lead")

    cfg = s.load_config()
    cfg["session_id"] = "20260705T190000-ABCDZ"
    s._write_config(cfg)
    assert s.lead_chat_request_id(operator="operator", lead="lead") != rid

    cfg.pop("operator_identity", None)
    s._write_config(cfg)
    with pytest.raises(ValueError, match="operator_identity is not configured"):
        s.operator_identity()

    cfg["operator_identity"] = "dev"
    s._write_config(cfg)
    with pytest.raises(ValueError, match="not a reserved bus principal"):
        s.operator_identity()

    cfg["operator_identity"] = "operator"
    s._write_config(cfg)
    with pytest.raises(ValueError, match="must not equal the lead"):
        s.operator_identity(lead="operator")


def test_lead_chat_request_id_is_shared_by_cli_and_endpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = _store(tmp_path)
    _available(s)
    expected = s.lead_chat_request_id(operator="operator", lead="lead")

    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lead_chat"]["request_id"] == expected

    got = web.build_lead_chat(web.RootDescriptor(store=s, label="root"))
    assert got["request_id"] == expected


def test_lead_chat_intent_schema_is_body_only() -> None:
    assert intents.validate_intent("lead_chat_send", {"body": "hello"}) == []
    assert intents.validate_intent("lead_chat_send", {"body": "hello", "to": "lead"})
    assert intents.validate_intent("lead_chat_send", {"subject": "x"})


def test_queued_lead_chat_send_never_authorizes_operator_sender(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _available(s)
    with pytest.raises(ValueError, match="reserved bus principal"):
        s.send(sender="operator", recipient="lead", body="spoofed")
    rec = s.write_intent(
        "lead_chat_send",
        {"body": "agent forged"},
        origin={"source": "agent", "from": "dev"},
    )

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    stored = s.read_intent(rec["intent_id"])
    assert stored["state"] == Store.INTENT_DENIED
    assert stored["code"] == "lead_chat_send_not_queue_authorized"
    assert not [
        m for m in s.valid_messages()
        if m.sender == "operator" and m.body == "agent forged"
    ]


def test_lead_chat_stable_meta_shape_is_exact(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    stable = intents.lead_chat_stable_meta(
        s, operator="operator", lead="lead")

    assert set(stable) == {
        "request_id",
        "lead_chat",
        "operator_identity",
        "operator_facing_lead",
    }
    assert stable == {
        "request_id": s.lead_chat_request_id(operator="operator", lead="lead"),
        "lead_chat": "true",
        "operator_identity": "operator",
        "operator_facing_lead": "lead",
    }


def test_lead_chat_liveness_requires_fresh_heartbeat_and_wrapped_health(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    now = _now_iso()
    s.write_health("lead", hm.build_snapshot(
        agent="lead",
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_IDLE_WAITING,
        updated_at=now,
        since=now,
        last_progress_at=now,
        reason_code=hm.STATE_IDLE_WAITING,
    ))
    missing = s.lead_chat_liveness(lead="lead")
    assert missing["available"] is False
    assert missing["code"] == "lead_unavailable"
    assert missing["reason"] == "lead heartbeat is missing"

    s.write_heartbeat("lead")
    s.write_health("lead", hm.build_snapshot(
        agent="lead",
        cli="codex",
        mode="manual",
        state=hm.STATE_IDLE_WAITING,
        updated_at=now,
        since=now,
        last_progress_at=now,
        reason_code=hm.STATE_IDLE_WAITING,
    ))
    unwrapped = s.lead_chat_liveness(lead="lead")
    assert unwrapped["available"] is False
    assert unwrapped["code"] == "lead_unwrapped"

    _stale_heartbeat(s)
    s.write_health("lead", hm.build_snapshot(
        agent="lead",
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_IDLE_WAITING,
        updated_at=now,
        since=now,
        last_progress_at=now,
        reason_code=hm.STATE_IDLE_WAITING,
    ))
    stale = s.lead_chat_liveness(lead="lead", heartbeat_stale_after=60)
    assert stale["available"] is False
    assert stale["code"] == "lead_unavailable"
    assert stale["reason"] == "lead heartbeat is stale"


def test_operator_answer_reuses_escalation_flow_from_lead_chat(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _available(s)
    _lead_question(s, "esc-choice")
    rec = s.write_intent("answer_escalation", {
        "to_request": "esc-choice",
        "body": "ship",
    })

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["applied"] == 1
    answers = [
        m for m in s.valid_messages()
        if m.sender == "operator"
        and m.recipient == "lead"
        and (m.meta or {}).get("operator_answer") == "true"
    ]
    assert len(answers) == 1
    assert answers[0].meta["request_id"] == "esc-choice"
    assert answers[0].meta["operator_origin"] == "operator"
    assert s.read_intent(rec["intent_id"])["state"] == Store.INTENT_APPLIED


def test_operator_answer_drain_denies_when_lead_goes_unavailable(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _available(s)
    _lead_question(s, "esc-choice")
    rec = s.write_intent("answer_escalation", {
        "to_request": "esc-choice",
        "body": "ship",
    })
    _stale_heartbeat(s)

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    assert s.read_intent(rec["intent_id"])["code"] == "lead_unavailable"
    assert not [
        m for m in s.valid_messages()
        if m.sender == "operator"
        and m.recipient == "lead"
        and (m.meta or {}).get("operator_answer") == "true"
    ]


def test_lead_chat_answer_intent_is_limited_to_current_lead(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _available(s)
    _lead_question(s, "esc-dev", sender="dev")
    rec = s.write_intent(
        "answer_escalation",
        {"to_request": "esc-dev", "body": "ship"},
        origin={
            "source": "web-lead-chat",
            "lead_chat_request_id": s.lead_chat_request_id(
                operator="operator", lead="lead"),
        },
    )

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["denied"] == 1
    assert s.read_intent(rec["intent_id"])["code"] == "plan_revalidation_failed"
    assert not [
        m for m in s.valid_messages()
        if m.sender == "operator" and m.recipient == "dev"
    ]


def test_lead_escalate_defaults_to_operator_principal(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert cli.main([
        "--root", str(tmp_path), "escalate",
        "--from", "lead", "--quiet", "-m", "choose",
    ]) == 0

    messages = list(s.valid_messages())
    assert len(messages) == 1
    assert messages[0].sender == "lead"
    assert messages[0].recipient == "operator"
    assert messages[0].meta["needs_operator"] == "true"
    assert messages[0].meta["request_id"].startswith("esc-")


def test_api_lead_chat_get_post_and_pending_decision_shape(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _available(s)
    _lead_question(s)
    _lead_question(s, "esc-dev", sender="dev")
    srv, _t, base = web.serve_in_thread(s, port=0, enable_actions=True)
    try:
        token = _session(base)
        payload = _get_json(f"{base}/api/lead-chat")
        assert payload["available"] is True
        assert payload["status"] == "idle"
        assert payload["request_id"] == s.lead_chat_request_id(
            operator="operator", lead="lead")
        assert payload["pending_decisions"][0]["request_id"] == "esc-choice"
        assert payload["pending_decisions"][0]["options"] == ["ship", "hold"]
        assert {p["request_id"] for p in payload["pending_decisions"]} == {
            "esc-choice"
        }

        before = s.list_intents()
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_lead_chat(base, token, {"body": "x", "from": "operator"})
        assert exc.value.code == 400
        assert s.list_intents() == before

        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_lead_chat(base, token, {
                "to_request": "esc-dev",
                "body": "ship",
            })
        assert exc.value.code == 409
        assert json.loads(exc.value.read())["error"] == "decision_not_pending"
        assert s.list_intents() == before

        send = _post_lead_chat(base, token, {"body": "hello"})
        assert send["kind"] == "lead_chat_send"
        assert send["state"] == "sent"
        assert send["message_id"]
        assert send["request_id"] == payload["request_id"]
        lead_chat_messages = [
            m for m in s.valid_messages()
            if m.sender == "operator"
            and m.recipient == "lead"
            and (m.meta or {}).get("lead_chat") == "true"
        ]
        assert len(lead_chat_messages) == 1
        assert lead_chat_messages[0].body == "hello"
        assert lead_chat_messages[0].meta == {
            "request_id": payload["request_id"],
            "lead_chat": "true",
            "operator_identity": "operator",
            "operator_facing_lead": "lead",
        }
        assert s.list_intents() == before

        answer = _post_lead_chat(base, token, {
            "to_request": "esc-choice",
            "body": "ship",
        })
        assert answer["kind"] == "answer_escalation"
        assert {r["kind"] for r in s.list_intents()} == {"answer_escalation"}
        assert intents.drain_intents(s, pid=123, max_per_tick=1)["applied"] == 1
        refreshed = _get_json(f"{base}/api/lead-chat")
        bodies = [m["body"] for m in refreshed["messages"]]
        assert "body visible in lead chat only" in bodies
        assert "ship" in bodies
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_lead_chat_unavailable_never_queues(tmp_path: Path) -> None:
    s = _store(tmp_path)
    srv, _t, base = web.serve_in_thread(s, port=0, enable_actions=True)
    try:
        token = _session(base)
        payload = _get_json(f"{base}/api/lead-chat")
        assert payload["available"] is False
        assert payload["error"] == "lead_unavailable"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_lead_chat(base, token, {"body": "hello"})
        assert exc.value.code == 409
        assert json.loads(exc.value.read())["error"] == "lead_unavailable"
        assert s.list_intents() == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_operator_principal_is_not_in_agent_only_walks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = _store(tmp_path)
    s.send(sender="lead", recipient="operator", body="operator-visible")
    for audience in (
        {"kind": "all"},
        {"kind": "role", "value": "reviewer"},
        {"kind": "group", "value": "reviewers"},
    ):
        s.write_intent("broadcast", {
            "audience": audience,
            "body": f"hello {audience['kind']}",
            "message_kind": "message",
        })
    summary = intents.drain_intents(s, pid=123, max_per_tick=10)
    assert summary["applied"] == 3
    assert not [
        m for m in s.valid_messages()
        if m.recipient == "operator" and m.body.startswith("hello ")
    ]

    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert "operator" not in [a["name"] for a in status_payload["agents"]]
    web_status = web.status_payload(s)
    assert "operator" not in web_status["agents"]
    assert "operator" not in web_status["agent_health"]

    doctor_report = doctor.run(tmp_path).to_dict()
    heartbeat_names = [
        c["name"] for c in doctor_report["checks"]
        if c["name"].startswith("heartbeat.")
    ]
    assert "heartbeat.operator" not in heartbeat_names

    report = sup.build_report(s, now_epoch=time.time())
    assert "operator" not in report["roster"]
    assert "operator" not in report["agents"]
    obs = sup.build_supervisor_observation(s, now_epoch=time.time())
    assert "operator" not in [a["name"] for a in obs["agents"]]
