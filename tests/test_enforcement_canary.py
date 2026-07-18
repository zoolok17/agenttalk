"""Zero-spend, real-wrapper canary for detection-grade owed-action enforcement."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api, run, session
from agenttalk.wrapper.obligations import (
    DetectionCommitGate,
    MAX_PAID_DISPATCHES_TOTAL,
    ResolverState,
)


STUB = Path(__file__).parent / "fixtures" / "enforcement_canary_stub.py"
CONTROL_ENV = "AGENTTALK_ENFORCEMENT_CANARY_CONTROL"


def _store(root: Path, agent: str) -> Store:
    store = Store(root)
    store.init(["requester", agent])
    store.set_operator_facing("requester")
    return store


def _question(store: Store, agent: str, request_id: str):
    return store.send(
        sender="requester",
        recipient=agent,
        kind="question",
        subject="zero-spend enforcement canary",
        body="Reply with the deterministic canary acknowledgement.",
        meta={"request_id": request_id},
    )


def _policy(path: Path, agent: str) -> None:
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "agents": {agent: {"grade": "detection", "enabled": True}},
        }),
        encoding="utf-8",
    )


def _configure_child_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    control_path: Path,
) -> None:
    source_root = Path(__file__).parents[1] / "src"
    inherited = os.environ.get("PYTHONPATH")
    pythonpath = str(source_root)
    if inherited:
        pythonpath = pythonpath + os.pathsep + inherited
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.setenv("AGENTTALK_PY", sys.executable)
    monkeypatch.setenv(CONTROL_ENV, str(control_path))


def _write_control(root: Path, agent: str, *, mode: str) -> tuple[Path, Path]:
    trace_path = root / "stub-trace.jsonl"
    control_path = root / "stub-control.json"
    control_path.write_text(
        json.dumps({
            "agent": agent,
            "mode": mode,
            "reply_body": "deterministic canary acknowledgement",
            "root": str(root),
            "trace_path": str(trace_path),
        }),
        encoding="utf-8",
    )
    return control_path, trace_path


def _run_wrapped_turn(
    store: Store,
    agent: str,
    *,
    max_polls: int,
) -> tuple[int, DetectionCommitGate]:
    wrapper_generation = "enforcement-canary-wrapper"
    gate = DetectionCommitGate.from_environment(
        store,
        agent,
        fence=wrapper_generation,
    )
    state = session.SessionState(cli="codex")
    drive = run.make_drive(
        store,
        agent,
        "codex",
        state,
        [sys.executable, str(STUB)],
        render=False,
        clock=lambda: 0.0,
        agenttalk_preflight=lambda: run.preflight_agenttalk_runtime(
            workspace_root=Path(__file__).parents[1],
        ),
        persist=lambda value: session.save_session(store, agent, value),
        wrapper_generation=wrapper_generation,
    )
    turns = loop.run_loop(
        store,
        agent,
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=max_polls,
        max_turns=1,
        wrapper_generation=wrapper_generation,
    )
    return turns, gate


def _trace(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _ledger(root: Path) -> dict:
    path = root / ".agenttalk" / "state" / "owed-action" / "ledger.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _admission(ledger: dict, inbound_id: str) -> tuple[str, dict]:
    key_digest = ledger["inbound_index"][inbound_id]
    return key_digest, ledger["obligations"][key_digest]


def _transitions(ledger: dict, name: str) -> list[dict]:
    return [row for row in ledger["transitions"] if row["transition"] == name]


def test_compliant_stub_executes_reserved_reply_once_without_penalty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "compliant"
    agent = "canary-compliant"
    request_id = "q-canary-compliant"
    store = _store(root, agent)
    inbound = _question(store, agent, request_id)
    record = recv_api.next_record(store, agent)
    assert record is not None
    policy_path = root / "operator-policy.json"
    _policy(policy_path, agent)
    control_path, trace_path = _write_control(root, agent, mode="compliant")
    _configure_child_runtime(monkeypatch, control_path=control_path)
    monkeypatch.setenv("AGENTTALK_COMMIT_GATE_POLICY", str(policy_path))

    turns, gate = _run_wrapped_turn(store, agent, max_polls=3)

    ledger = _ledger(root)
    key_digest, admission = _admission(ledger, inbound.id)
    traces = _trace(trace_path)
    resolved = gate.resolve(record)
    gate_status = gate.status()
    replies = [
        message
        for message in store.valid_messages()
        if message.sender == agent and message.meta.get("in_reply_to") == inbound.id
    ]
    assert turns == 1
    assert len(_transitions(ledger, "OBLIGATION_ADMITTED")) == 1
    assert len(_transitions(ledger, "DISPATCH_RESERVED")) == 1
    assert len(traces) == 1
    assert traces[0]["command_executed"] is True
    assert traces[0]["command_timed_out"] is False
    assert traces[0]["transport_allowlisted"] is True
    assert traces[0]["bus_exit_code"] == 0
    assert traces[0]["owed_transport_present"] is True
    assert traces[0]["purpose"] == "initial"
    assert traces[0]["exact_inbound_id"] == inbound.id
    assert traces[0]["obligation_key_digest"] == key_digest
    assert traces[0]["python_executable"] == sys.executable
    assert traces[0]["cursor_before_child"] == ""
    assert traces[0]["cursor_after_child"] == ""
    assert admission["paid_dispatches_total"] == 1
    assert len(admission["reservations"]) == 1
    assert next(iter(admission["reservations"].values()))["action_attempted"] is True
    assert admission["state"] == "finalized"
    assert admission["terminal_state"] == "satisfied"
    assert admission["owed_action_missing_seen"] is False
    assert resolved.state == ResolverState.SATISFIED
    assert gate_status["status"] == "ACTIVE (detection-grade)"
    assert gate_status["grade"] == "detection"
    assert len(replies) == 1
    assert resolved.evidence_id == replies[0].id
    assert admission["terminal_evidence_id"] == replies[0].id
    assert replies[0].meta["operation_nonce"] == traces[0]["dispatch_nonce"]
    assert ledger["messages"][replies[0].id]["operation_payload_valid"] is True
    assert store.cursor(agent) == inbound.id
    assert ledger["cursor_dispositions"][agent]["state"] == "satisfied"
    assert not _transitions(ledger, "OWED_ACTION_MISSING")
    assert not _transitions(ledger, "DELIVERY_FAILED")
    assert ledger["breakers"].get(agent, {}).get(
        "owed_action_cap_exhaustions_consecutive",
        0,
    ) == 0
    assert key_digest


def test_print_not_run_is_recovered_then_becomes_visible_failed_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "print-not-run"
    agent = "canary-print-only"
    request_id = "q-canary-print-only"
    store = _store(root, agent)
    inbound = _question(store, agent, request_id)
    record = recv_api.next_record(store, agent)
    assert record is not None
    policy_path = root / "operator-policy.json"
    _policy(policy_path, agent)
    control_path, trace_path = _write_control(root, agent, mode="print_not_run")
    _configure_child_runtime(monkeypatch, control_path=control_path)
    monkeypatch.setenv("AGENTTALK_COMMIT_GATE_POLICY", str(policy_path))

    turns, gate = _run_wrapped_turn(store, agent, max_polls=4)

    ledger = _ledger(root)
    key_digest, admission = _admission(ledger, inbound.id)
    traces = _trace(trace_path)
    resolved = gate.resolve(record)
    delivery = gate.delivery_status(request_id, "requester")
    requester_view = recv_api.poll(
        store,
        "requester",
        scoped_request_id=request_id,
    )["scoped"]
    reservations = list(admission["reservations"].values())
    incidents = _transitions(ledger, "COMPLIANCE_INCIDENT")
    delivery_transitions = _transitions(ledger, "DELIVERY_FAILED")
    assert turns == 0
    assert len(_transitions(ledger, "OBLIGATION_ADMITTED")) == 1
    assert len(_transitions(ledger, "DISPATCH_RESERVED")) == (
        MAX_PAID_DISPATCHES_TOTAL
    )
    assert len(traces) == MAX_PAID_DISPATCHES_TOTAL == 2
    assert [row["purpose"] for row in traces] == ["initial", "recovery"]
    assert "resume" not in traces[0]["argv"]
    assert "resume" in traces[1]["argv"]
    assert len({row["dispatch_nonce"] for row in traces}) == 2
    assert all(row["obligation_key_digest"] == key_digest for row in traces)
    assert all(row["command_executed"] is False for row in traces)
    assert all(row["owed_transport_present"] is True for row in traces)
    assert all(row["python_executable"] == sys.executable for row in traces)
    assert all(row["cursor_before_child"] == "" for row in traces)
    assert all(row["cursor_after_child"] == "" for row in traces)
    assert admission["paid_dispatches_total"] == MAX_PAID_DISPATCHES_TOTAL
    assert len(reservations) == MAX_PAID_DISPATCHES_TOTAL
    assert all(row["action_attempted"] is False for row in reservations)
    assert admission["paid_initial_dispatches_total"] == 1
    assert admission["paid_recoveries_total"] == 1
    assert admission["paid_continuations_total"] == 0
    assert admission["recovery_used"] is True
    assert admission["owed_action_missing_seen"] is True
    assert len(_transitions(ledger, "OWED_ACTION_MISSING")) == 2
    assert admission["state"] == "delivery_failed"
    assert resolved.state == ResolverState.DELIVERY_EXHAUSTED
    assert len(incidents) == 1
    assert len(delivery_transitions) == 1
    assert delivery is not None
    assert delivery["kind"] == "DELIVERY_FAILED"
    assert delivery["state"] == "delivery_failed"
    assert delivery["key_digest"] == key_digest
    assert delivery["incident_sequence"] == incidents[0]["sequence"]
    assert delivery_transitions[0]["data"]["incident_sequence"] == incidents[0][
        "sequence"
    ]
    assert requester_view["delivery_failed"]["key_digest"] == key_digest
    assert ledger["cursor_dispositions"][agent]["state"] == "delivery_failed"
    assert store.cursor(agent) == inbound.id
    assert ledger["breakers"][agent]["owed_action_cap_exhaustions_consecutive"] == 1
    assert ledger["breakers"][agent]["proof_infra_exhaustions_consecutive"] == 0
    assert ledger["breakers"][agent]["tripped"] is False
    assert ledger["breakers"][agent]["config_blocked"] is False
    assert any(message.id == inbound.id for message in store.valid_messages())


def test_unconfigured_stub_keeps_legacy_wrapper_behavior_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "inactive"
    agent = "canary-inactive"
    request_id = "q-canary-inactive"
    store = _store(root, agent)
    inbound = _question(store, agent, request_id)
    control_path, trace_path = _write_control(root, agent, mode="print_not_run")
    _configure_child_runtime(monkeypatch, control_path=control_path)
    monkeypatch.delenv("AGENTTALK_COMMIT_GATE_POLICY", raising=False)

    turns, _gate = _run_wrapped_turn(store, agent, max_polls=2)

    ledger = _ledger(root)
    traces = _trace(trace_path)
    assert turns == 1
    assert len(traces) == 1
    assert traces[0]["owed_transport_present"] is False
    assert traces[0]["command_executed"] is False
    assert traces[0]["python_executable"] == sys.executable
    assert store.cursor(agent) == inbound.id
    assert not ledger["obligations"]
    assert not ledger["inbound_index"]
    assert not _transitions(ledger, "OBLIGATION_ADMITTED")
    assert not _transitions(ledger, "DELIVERY_FAILED")
