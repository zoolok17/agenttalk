"""Detection-grade owed-action protocol acceptance tests (design v3.1.1 §14)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import cli, doctor, signing
from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api
from agenttalk.wrapper.obligations import (
    COMPLIANCE_BREAKER_TRIP,
    DETECTION_GRADE,
    DispatchRefused,
    DetectionCommitGate,
    PolicySnapshot,
    ResolverState,
    StaleRevision,
)


def _store(tmp_path: Path, agents: list[str] | None = None) -> Store:
    store = Store(tmp_path)
    store.init(agents or ["alpha", "beta", "lead", "gamma"])
    store.set_operator_facing("lead")
    return store


def _policy(*agents: str) -> PolicySnapshot:
    return PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {agent: {"grade": DETECTION_GRADE} for agent in agents},
    }, agents[0] if agents else "beta")


def _gate(
    store: Store,
    *,
    fence: str = "wrapper-1",
    now=None,
    agent: str = "beta",
    policy_agents: tuple[str, ...] | None = None,
    producer_alive=None,
) -> DetectionCommitGate:
    kwargs = {"fence": fence}
    if now is not None:
        kwargs["now"] = now
    if producer_alive is not None:
        kwargs["producer_alive"] = producer_alive
    peers = tuple(name for name in (policy_agents or (agent,)) if name != agent)
    return DetectionCommitGate(store, agent, _policy(agent, *peers), **kwargs)


def _question(store: Store, rid: str = "q-1", **meta):
    return store.send(
        sender="alpha",
        recipient="beta",
        kind="question",
        body="What is 19 * 21?",
        meta={"request_id": rid, **meta},
    )


def _record(store: Store, rid: str | None = None, *, agent: str = "beta") -> dict:
    record = recv_api.next_record(store, agent, scoped_request_id=rid)
    assert record is not None
    return record


def _ledger(gate: DetectionCommitGate) -> dict:
    return json.loads(gate.path.read_text(encoding="utf-8"))


def _answer_reserved(
    root: Path,
    gate: DetectionCommitGate,
    record: dict,
    *,
    body: str = "399",
) -> tuple[object, object]:
    resolution = gate.admit_or_finalize(record)
    assert resolution.key is not None
    permit = gate.reserve_dispatch(resolution, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    Path(dispatched["owed_action"]["draft_path"]).write_text(body, encoding="utf-8")
    assert cli.main([
        "--root", str(root), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]) == 0
    return resolution, permit


def _broadcast_question(
    store: Store,
    recipient: str,
    *,
    broadcast_id: str,
    members: list[str],
    policy: str,
    quorum: int | None = None,
):
    meta = {
        "broadcast_id": broadcast_id,
        "membership_snapshot": members,
        "response_policy": policy,
        "broadcast_policy_version": 1,
    }
    if quorum is not None:
        meta["response_quorum"] = quorum
    return store.send(
        sender="alpha",
        recipient=recipient,
        kind="question",
        body="broadcast question",
        meta=meta,
    )


def test_policy_snapshot_distinguishes_absent_agent_from_corrupt_policy(tmp_path: Path) -> None:
    absent = PolicySnapshot.from_mapping({"schema_version": 1, "agents": {}}, "beta")
    corrupt = PolicySnapshot.from_mapping({"schema_version": 1, "agents": []}, "beta")

    assert absent.status == ResolverState.NOT_OWED
    assert corrupt.status == ResolverState.BLOCKED_POLICY

    path = tmp_path / "policy.json"
    path.write_text("{", encoding="utf-8")
    assert PolicySnapshot.from_path(path, "beta").status == ResolverState.BLOCKED_POLICY


def test_readable_policy_reports_detection_grade() -> None:
    snapshot = _policy("beta")

    assert snapshot.status == ResolverState.ACTIVE
    assert snapshot.grade == "detection"
    assert len(snapshot.generation) == 64


def test_admission_allocates_epoch_monotonic_sequence_and_replay_visible_key(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)

    resolution = gate.admit_or_finalize(_record(store))
    ledger = _ledger(gate)
    admitted = [
        event for event in ledger["transitions"]
        if event["transition"] == "OBLIGATION_ADMITTED"
    ]

    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None
    assert resolution.key.inbound_id == inbound.id
    assert resolution.key.store_epoch == ledger["store_epoch"]
    assert admitted[0]["data"]["key"] == resolution.key.to_dict()
    assert [row["sequence"] for row in ledger["transitions"]] == list(
        range(1, ledger["append_sequence"] + 1)
    )


def test_pre_admission_answer_normalizes_without_obligation_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    answer = store.send(
        sender="beta",
        recipient="alpha",
        body="399",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    gate = _gate(store)
    record = _record(store)

    resolution = gate.admit_or_finalize(record)
    assert resolution.state == ResolverState.SATISFIED
    assert resolution.key is None
    assert resolution.evidence_id == answer.id

    gate.finalize(record, resolution, expected_revision=resolution.ledger_revision)
    assert store.cursor("beta") == inbound.id
    assert not _ledger(gate)["obligations"]


def test_pre_admission_manual_close_and_legacy_rescind_are_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.close_thread("beta", "q-1", seen_msg_id=inbound.id)
    closed = _gate(store).admit_or_finalize(_record(store))
    assert closed.state == ResolverState.SATISFIED
    assert closed.key is None

    other = _question(store, "q-2")
    store.send(
        sender="alpha",
        recipient="beta",
        kind="rescind",
        body="withdrawn",
        meta={"request_id": "q-2", "target_msg_id": other.id},
    )
    rescinded = _gate(store).admit_or_finalize(
        recv_api.records(store, "beta")[-2]
    )
    assert rescinded.state == ResolverState.SUPERSEDED


def test_dispatch_reservation_is_scoped_revision_cas_linearization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))

    store.send(
        sender="beta",
        recipient="alpha",
        kind="composing",
        body="drafting",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )

    with pytest.raises(StaleRevision):
        gate.reserve_dispatch(resolution, purpose="initial")


def test_reservation_precedes_drive_and_second_dispatch_is_xor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))

    first = gate.reserve_dispatch(resolution, purpose="initial")
    assert first.paid_dispatches_total == 1
    refreshed = gate.resolve(_record(store))
    second = gate.reserve_dispatch(refreshed, purpose="recovery")
    assert second.paid_dispatches_total == 2

    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(gate.resolve(_record(store)), purpose="continuation")


def test_loop_never_calls_drive_without_durable_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    observed: list[dict] = []

    def drive(record: dict) -> loop.DriveOutcome:
        ledger = _ledger(gate)
        admission_id = ledger["inbound_index"][inbound.id]
        admission = ledger["obligations"][admission_id]
        assert admission["paid_dispatches_total"] == 1
        assert any(row["state"] == "reserved" for row in admission["reservations"].values())
        observed.append(record["owed_action"])
        return loop.DriveOutcome(ok=True)

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    assert len(observed) == 1
    assert observed[0]["exact_inbound_id"] == inbound.id
    assert observed[0]["body_transport"] == "structured-write-then-file"


def test_print_not_run_is_never_committed_and_caps_at_two_paid_dispatches(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    calls = 0

    def drive(_record: dict) -> loop.DriveOutcome:
        nonlocal calls
        calls += 1
        return loop.DriveOutcome(ok=True)

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=4,
    )

    assert calls == 2
    assert store.cursor("beta") == inbound.id
    status = gate.delivery_status("q-1", "alpha")
    assert status is not None and status["state"] == "delivery_failed"
    assert any(message.id == inbound.id for message in store.valid_messages())


def test_reply_lands_then_nonzero_child_commits_without_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    calls = 0

    def drive(record: dict) -> loop.DriveOutcome:
        nonlocal calls
        calls += 1
        owed = record["owed_action"]
        Path(owed["draft_path"]).write_text("399", encoding="utf-8")
        assert cli.main([
            "--root", str(tmp_path), *owed["argv"][3:], "--quiet",
        ]) == 0
        return loop.DriveOutcome(
            ok=False,
            failure_class=loop.CLASS_AMBIGUOUS,
            summary="child crashed after send",
            bus_action_attempted=True,
        )

    turns = loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_turns=1,
        max_polls=3,
    )

    assert turns == 1
    assert calls == 1
    assert store.cursor("beta") == inbound.id


def test_note_is_not_enforced_and_creates_no_spurious_reply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    note = store.send(sender="alpha", recipient="beta", kind="note", body="FYI")
    gate = _gate(store)
    calls = 0

    def drive(_record: dict) -> bool:
        nonlocal calls
        calls += 1
        return True

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_turns=1,
    )

    assert calls == 1
    assert store.cursor("beta") == note.id
    assert not [message for message in store.valid_messages() if message.sender == "beta"]


def test_composing_is_nonterminal_and_requires_live_producer_or_continuation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(_record(store), permit)
    gate.schedule_continuation(admitted.key, producer_token="producer-1")
    composing_argv = dispatched["owed_action"]["composing_argv"]
    assert cli.main([
        "--root", str(tmp_path), *composing_argv[3:], "--quiet",
    ]) == 0

    deferred = gate.resolve(_record(store))
    assert deferred.state == ResolverState.IN_PROGRESS
    assert deferred.terminal is False
    assert store.cursor("beta") == ""


def test_durable_retry_barrier_survives_restart_without_double_charge(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None

    assert gate.record_retry_barrier(admitted.key, category="operation_infra") is True
    restarted = _gate(store, fence="wrapper-1")
    assert restarted.record_retry_barrier(
        admitted.key,
        category="operation_infra",
    ) is True
    admission = _ledger(restarted)["obligations"][admitted.key.digest]

    assert admission["operation_infra_attempts"] == 1
    assert admission["operation_infra_first_at"]
    restarted.complete_retry_barrier(admitted.key, category="operation_infra")
    assert restarted.record_retry_barrier(
        admitted.key,
        category="operation_infra",
    ) is True
    assert (
        _ledger(restarted)["obligations"][admitted.key.digest][
            "operation_infra_attempts"
        ]
        == 2
    )


def test_delivery_failed_indexes_requester_and_disposition_before_cursor_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None

    def crash_projection(_record: dict) -> None:
        raise OSError("crash barrier")

    monkeypatch.setattr(gate, "_advance_record_cursor", crash_projection)
    with pytest.raises(OSError, match="crash barrier"):
        gate.delivery_failed(_record(store), admitted.key, reason="test failure")

    ledger = _ledger(gate)
    assert ledger["delivery_index"]["q-1"][0]["state"] == "delivery_failed"
    assert ledger["cursor_dispositions"]["beta"]["inbound_id"] == inbound.id
    assert store.cursor("beta") == ""

    restarted = _gate(store)
    terminal = restarted.resolve(_record(store))
    assert terminal.state == ResolverState.DELIVERY_EXHAUSTED
    restarted.finalize(_record(store), terminal)
    assert store.cursor("beta") == inbound.id


def test_compliance_breaker_trips_after_three_dominant_exhaustions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    messages = [_question(store, f"q-{index}") for index in range(1, 5)]
    gate = _gate(store)
    calls = 0

    def drive(_record: dict) -> loop.DriveOutcome:
        nonlocal calls
        calls += 1
        return loop.DriveOutcome(ok=True)

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=14,
    )

    breaker = gate.status()["breaker"]
    assert COMPLIANCE_BREAKER_TRIP == 3
    assert breaker["tripped"] is True
    assert breaker["owed_action_cap_exhaustions_consecutive"] == 3
    assert calls == 6
    assert store.cursor("beta") == messages[2].id
    alerts = [
        event for event in _ledger(gate)["transitions"]
        if event["transition"] == "COMPLIANCE_BREAKER_ALERT"
    ]
    assert len(alerts) == 1


def test_transfer_validates_destination_before_closing_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None

    with pytest.raises(ValueError):
        gate.transfer(admitted.key, destination="missing", new_inbound_id="next-1")
    assert _ledger(gate)["obligations"][admitted.key.digest]["state"] == "open"

    transferred = store.send(
        sender="alpha",
        recipient="gamma",
        kind="question",
        body="What is 19 * 21?",
        meta={"request_id": "q-1"},
    )
    gate.transfer(
        admitted.key,
        destination="gamma",
        new_inbound_id=transferred.id,
    )
    ledger = _ledger(gate)
    assert ledger["obligations"][admitted.key.digest]["state"] == "transferred"
    assert transferred.id in ledger["inbound_index"]


def test_invalid_metadata_is_classification_unknown_and_commits_with_telemetry_scope(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, consult={"unsupported": True})
    resolution = _gate(store).admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.CLASSIFICATION_UNKNOWN
    assert resolution.allows_legacy_commit is True


def test_one_shot_question_uses_same_gate_and_does_not_commit_print_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store, "q-scoped")
    gate = _gate(store)
    calls = 0

    def drive(_record: dict) -> loop.DriveOutcome:
        nonlocal calls
        calls += 1
        return loop.DriveOutcome(ok=True)

    turns = loop.run_loop(
        store,
        "beta",
        drive,
        only_request_id="q-scoped",
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=3,
    )

    assert turns == 0
    assert calls == 2
    assert store.thread_seen("beta", "q-scoped")
    assert gate.delivery_status("q-scoped", "alpha")["state"] == "delivery_failed"


def test_cli_reply_exact_anchor_and_literal_file_transport_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    body = "-$ $env:HOME $() `tick` \"quotes\"\nUnicode: \u2603\n'@\n"
    draft = tmp_path / "answer.txt"
    draft.write_text(body, encoding="utf-8", newline="")

    rc = cli.main([
        "--root", str(tmp_path), "reply", "--from", "beta",
        "--to-id", inbound.id, "--file", str(draft), "--quiet",
    ])
    answer = [message for message in store.valid_messages() if message.sender == "beta"][-1]

    assert rc == 0
    assert answer.body == body
    assert answer.meta["request_id"] == "q-1"
    assert answer.meta["in_reply_to"] == inbound.id


def test_wrong_sender_request_anchor_and_control_record_are_nonproof(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None

    store.send(
        sender="gamma", recipient="alpha", body="forged actor",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    store.send(
        sender="beta", recipient="alpha", body="wrong thread",
        meta={"request_id": "q-other", "in_reply_to": inbound.id},
    )
    store.send(
        sender="beta", recipient="alpha", kind="composing", body="not terminal",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )

    assert gate.resolve(_record(store)).state == ResolverState.OWED_UNSATISFIED


def test_prior_generation_answer_does_not_satisfy_same_rid_reask(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _question(store, "reused")
    store.send(
        sender="beta", recipient="alpha", body="old answer",
        meta={"request_id": "reused", "in_reply_to": first.id},
    )
    store.advance_cursor("beta", first.id)
    second = _question(store, "reused")
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))

    assert second.id != first.id
    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None and resolution.key.inbound_id == second.id


def test_pre_admission_finalize_ignores_unrelated_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.send(
        sender="beta", recipient="alpha", body="399",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    gate = _gate(store)
    record = _record(store)
    terminal = gate.admit_or_finalize(record)
    store.send(sender="gamma", recipient="alpha", body="concurrent unrelated")

    result = gate.finalize(
        record,
        terminal,
        expected_revision=terminal.ledger_revision,
    )

    assert result.state == ResolverState.SATISFIED


def test_pre_admission_finalize_rejects_correlated_revision_race(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.send(
        sender="beta", recipient="alpha", body="399",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    gate = _gate(store)
    record = _record(store)
    terminal = gate.admit_or_finalize(record)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="rescind",
        body="withdrawn",
        meta={"request_id": "q-1"},
    )
    gate.resolve(record)

    result = gate.finalize(
        record,
        terminal,
        expected_revision=terminal.ledger_revision,
    )

    assert result.state == ResolverState.INDETERMINATE
    assert store.cursor("beta") == ""


def test_alternating_global_projection_errors_exhaust_by_elapsed_time_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    first_gate = _gate(store)
    first_gate.admit_or_finalize(_record(store))
    first_gate.path.write_text("{", encoding="utf-8")
    times = iter(["2026-01-01T00:00:00Z", "2026-01-01T00:15:01Z"])
    gate = _gate(store, now=lambda: next(times))

    assert gate.resolve(_record(store)).state == ResolverState.BLOCKED
    health = gate.record_proof_failure(error_class="DifferentError", path="other-path")

    assert health["exhausted"] is True
    assert health["elapsed_seconds"] >= 900
    assert health["fingerprint"] == {
        "error_class": "DifferentError", "path": "other-path",
    }
    assert store.cursor("beta") == ""


def test_requester_wait_is_released_by_structured_delivery_failed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None
    gate.delivery_failed(_record(store), admitted.key, reason="delivery exhausted")

    rc = cli.main([
        "--root", str(tmp_path), "wait", "--for", "alpha",
        "--to-request", "q-1", "--timeout", "1", "--interval", "0.1",
    ])
    output = capsys.readouterr().out

    assert rc == 4
    assert "DELIVERY FAILED" in output
    assert '"state": "delivery_failed"' in output
    assert recv_api.poll(store, "alpha", scoped_request_id="q-1")["scoped"][
        "delivery_failed"
    ]["reason"] == "delivery exhausted"


def test_doctor_reports_active_detection_grade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path)
    policy = tmp_path.parent / f"{tmp_path.name}-operator-policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1,
        "agents": {"beta": {"grade": "detection"}},
    }), encoding="utf-8")
    monkeypatch.setenv("AGENTTALK_COMMIT_GATE_POLICY", str(policy))

    report = doctor.run(tmp_path)
    check = next(row for row in report.checks if row.name == "wrapped_commit_gate")

    assert check.status == "ok"
    beta = next(row for row in check.data["agents"] if row["agent"] == "beta")
    assert beta["status"] == "ACTIVE (detection-grade)"
    assert beta["security_grade"] is False


def test_invalid_hmac_reply_is_nonproof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path.parent / "owed-hmac.key"))
    store = _store(tmp_path)
    signing.init_key(store.project_id(), force=True)
    inbound = _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None
    forged = store.send(
        sender="beta", recipient="alpha", body="forged",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    path = store.messages_dir / f"{forged.id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["body"] = "tampered after signing"
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert gate.resolve(_record(store)).state == ResolverState.OWED_UNSATISFIED


def test_infra_exhaustion_does_not_trip_compliance_breaker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.mark_dispatch_result(
        permit,
        action_attempted=True,
        action_infra=True,
    )
    gate.delivery_failed(_record(store), admitted.key, reason="persistent AV lock")

    assert gate.status()["breaker"] == {}
    assert store.read_config_blocked_hold("beta") is None


def test_breaker_reset_requires_authorized_actor_and_audit_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(COMPLIANCE_BREAKER_TRIP):
        _question(store, f"q-reset-{index}")
    gate = _gate(store)
    loop.run_loop(
        store,
        "beta",
        lambda _record: loop.DriveOutcome(ok=True),
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=12,
    )
    assert gate.status()["breaker"]["tripped"] is True

    with pytest.raises(PermissionError):
        gate.reset_compliance_breaker(actor="gamma", reason="not authorized")
    gate.reset_compliance_breaker(actor="lead", reason="operator approved prompt repair")

    assert gate.status()["breaker"]["tripped"] is False
    assert store.read_config_blocked_hold("beta") is None


def test_security_grade_never_downgrades_to_detection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    policy = PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": "security"}},
    }, "beta")
    gate = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    calls = 0

    def drive(_record: dict) -> bool:
        nonlocal calls
        calls += 1
        return True

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    assert policy.status == ResolverState.BLOCKED
    assert policy.grade == "security"
    assert calls == 0
    assert store.cursor("beta") != inbound.id


def test_no_admission_finalizer_is_revision_and_fence_guarded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    policy = PolicySnapshot.from_mapping({"schema_version": 1, "agents": {}}, "beta")
    owner = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    record = _record(store)
    resolution = owner.admit_or_finalize(record)
    contender = DetectionCommitGate(store, "beta", policy, fence="wrapper-2")

    rejected = contender.finalize(
        record,
        resolution,
        expected_revision=resolution.ledger_revision,
    )
    finalized = owner.finalize(
        record,
        resolution,
        expected_revision=resolution.ledger_revision,
    )

    assert rejected.state == ResolverState.INDETERMINATE
    assert finalized.state == ResolverState.NOT_OWED


def test_sidecar_only_close_is_not_replay_proof(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    store._write_threadstate(  # noqa: SLF001 - simulate a torn legacy sidecar
        "beta",
        {"q-1": {"closed": True, "closed_reason": "sidecar-only"}},
    )

    resolution = _gate(store).admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None


def test_epoch_anchor_detects_append_counter_rollback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    gate.admit_or_finalize(_record(store))
    ledger = _ledger(gate)
    ledger["append_sequence"] -= 1
    gate.path.write_text(json.dumps(ledger), encoding="utf-8")

    resolution = gate.resolve(_record(store))

    assert resolution.state == ResolverState.BLOCKED
    assert gate.status()["status"] == "BLOCKED"
    assert store.cursor("beta") == ""


def test_operation_nonce_is_idempotent_and_payload_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    draft = Path(dispatched["owed_action"]["draft_path"])
    command = [
        "--root", str(tmp_path), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]
    draft.write_text("399", encoding="utf-8")

    assert cli.main(command) == 0
    assert cli.main(command) == 0
    draft.write_text("400", encoding="utf-8")
    assert cli.main(command) == 2

    matching = [
        message for message in store.valid_messages()
        if (message.meta or {}).get("operation_nonce") == permit.nonce
    ]
    assert len(matching) == 1
    assert matching[0].body == "399"


def test_composing_and_terminal_use_separate_tokens_and_terminal_wins(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    gate.schedule_continuation(admitted.key, producer_token="producer-1")
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["composing_argv"][3:],
        "--quiet",
    ]) == 0
    Path(dispatched["owed_action"]["draft_path"]).write_text("399", encoding="utf-8")
    assert cli.main([
        "--root", str(tmp_path), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]) == 0

    terminal = gate.resolve(record)

    assert permit.nonce != permit.composing_nonce
    assert terminal.state == ResolverState.SATISFIED


def test_composing_without_live_producer_or_continuation_does_not_park(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store, producer_alive=lambda _token: False)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["composing_argv"][3:],
        "--quiet",
    ]) == 0

    assert gate.resolve(record).state == ResolverState.OWED_UNSATISFIED


def test_wrong_class_answer_does_not_satisfy_required_escalation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store, escalation_required=True)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    permit.draft_path.write_text("I answered instead", encoding="utf-8")
    assert cli.main([
        "--root",
        str(tmp_path),
        "reply",
        "--from",
        "beta",
        "--to-id",
        inbound.id,
        "--operation-nonce",
        permit.nonce,
        "--file",
        str(permit.draft_path),
        "--quiet",
    ]) == 0
    unresolved = gate.resolve(record)
    gate.mark_unsatisfied_attempt(permit, reason="answered instead of escalating")

    assert unresolved.state == ResolverState.OWED_UNSATISFIED
    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["owed_action_missing_seen"] is True


def test_event_time_liaison_escalation_survives_later_repoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store, escalation_required=True)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    permit.draft_path.write_text("Operator decision needed", encoding="utf-8")
    assert cli.main([
        "--root", str(tmp_path), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]) == 0
    store.set_operator_facing("gamma")

    resolution = gate.resolve(record)

    assert resolution.state == ResolverState.SATISFIED
    assert resolution.compliance_success is True


def test_roster_revision_cas_rejects_stale_escalation_append(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store, escalation_required=True)
    gate = _gate(store)
    old = gate.roster_snapshot()
    store.set_operator_facing("gamma")

    with pytest.raises(ValueError, match="roster revision changed"):
        store.send(
            sender="beta",
            recipient="lead",
            kind="question",
            body="stale escalation",
            meta={
                "request_id": "esc-stale",
                "origin_request_id": "q-1",
                "origin_inbound_id": inbound.id,
                "expected_roster_revision": old["revision"],
            },
        )


def test_operator_resolution_cas_rejects_revoke_then_accepts_current_authority(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    old = gate.roster_snapshot()
    store.set_operator_facing("gamma")

    with pytest.raises(StaleRevision):
        gate.operator_resolve(
            record,
            admitted.key,
            actor="lead",
            expected_roster_revision=old["revision"],
            reason="stale authority",
        )
    current = gate.roster_snapshot()
    resolved = gate.operator_resolve(
        record,
        admitted.key,
        actor="gamma",
        expected_roster_revision=current["revision"],
        reason="operator disposition",
    )

    assert resolved.state == ResolverState.OPERATOR_RESOLVED
    assert store.cursor("beta") == record["id"]


def test_transferred_admission_is_claimed_by_destination_wrapper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    source = _gate(store, policy_agents=("beta", "gamma"))
    admitted = source.admit_or_finalize(_record(store))
    assert admitted.key is not None
    inbound = store.send(
        sender="alpha",
        recipient="gamma",
        kind="question",
        body="transferred question",
        meta={"request_id": "q-1"},
    )
    source.transfer(admitted.key, destination="gamma", new_inbound_id=inbound.id)
    destination = _gate(
        store,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    claimed = destination.admit_or_finalize(_record(store, agent="gamma"))

    assert claimed.state == ResolverState.OWED_UNSATISFIED
    assert claimed.key is not None and claimed.key.responder == "gamma"
    assert destination.reserve_dispatch(claimed, purpose="initial").paid_dispatches_total == 1


def test_each_broadcast_does_not_policy_close_other_member(tmp_path: Path) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    _broadcast_question(
        store,
        "beta",
        broadcast_id="b-each",
        members=members,
        policy="each",
    )
    _broadcast_question(
        store,
        "gamma",
        broadcast_id="b-each",
        members=members,
        policy="each",
    )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    gamma = _gate(
        store,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    gamma_record = _record(store, agent="gamma")
    gamma_open = gamma.admit_or_finalize(gamma_record)
    beta_record = _record(store)
    _answer_reserved(tmp_path, beta, beta_record)
    beta.finalize(beta_record, beta.resolve(beta_record))

    assert gamma.resolve(gamma_record).state == ResolverState.OWED_UNSATISFIED
    assert gamma_open.key is not None


def test_any_broadcast_policy_closes_member_and_records_late_reply(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    _broadcast_question(
        store,
        "beta",
        broadcast_id="b-any",
        members=members,
        policy="any",
    )
    gamma_inbound = _broadcast_question(
        store,
        "gamma",
        broadcast_id="b-any",
        members=members,
        policy="any",
    )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    gamma = _gate(
        store,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    gamma_record = _record(store, agent="gamma")
    gamma_open = gamma.admit_or_finalize(gamma_record)
    beta_record = _record(store)
    _answer_reserved(tmp_path, beta, beta_record)
    beta_terminal = beta.resolve(beta_record)
    beta.finalize(beta_record, beta_terminal)
    store.send(
        sender="gamma",
        recipient="alpha",
        body="late informational answer",
        meta={"broadcast_id": "b-any", "in_reply_to": gamma_inbound.id},
    )
    policy_closed = gamma.resolve(gamma_record)
    gamma.finalize(gamma_record, policy_closed)
    ledger = _ledger(gamma)

    assert gamma_open.key is not None
    assert policy_closed.state == ResolverState.BROADCAST_POLICY_SATISFIED
    assert ledger["obligations"][gamma_open.key.digest]["paid_dispatches_total"] == 0
    assert any(
        row["transition"] == "LATE_RESPONSE"
        and row["source_id"] is not None
        for row in ledger["transitions"]
    )


def test_quorum_closes_all_nonresponders_and_excludes_delivery_failed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, ["alpha", "beta", "gamma", "lead", "delta"])
    members = ["beta", "gamma", "lead", "delta"]
    for member in members:
        _broadcast_question(
            store,
            member,
            broadcast_id="b-quorum",
            members=members,
            policy="quorum",
            quorum=2,
        )
    beta = _gate(store, policy_agents=("beta", "gamma", "lead", "delta"))
    gamma = _gate(
        store,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta", "lead", "delta"),
    )
    lead = _gate(
        store,
        agent="lead",
        fence="wrapper-lead",
        policy_agents=("lead", "beta", "gamma", "delta"),
    )
    beta_record = _record(store)
    gamma_record = _record(store, agent="gamma")
    lead_record = _record(store, agent="lead")
    beta_open = beta.admit_or_finalize(beta_record)
    gamma.admit_or_finalize(gamma_record)
    lead_open = lead.admit_or_finalize(lead_record)
    assert beta_open.key is not None and lead_open.key is not None
    beta.delivery_failed(beta_record, beta_open.key, reason="head-local exhaustion")
    _answer_reserved(tmp_path, gamma, gamma_record)
    gamma.finalize(gamma_record, gamma.resolve(gamma_record))

    assert lead.resolve(lead_record).state == ResolverState.OWED_UNSATISFIED

    # A failed delivery did not count. A second qualifying answer is required.
    lead_resolution = lead.resolve(lead_record)
    lead_permit = lead.reserve_dispatch(lead_resolution, purpose="initial")
    lead_dispatch = lead.dispatch_record(lead_record, lead_permit)
    lead_permit.draft_path.write_text("lead answer", encoding="utf-8")
    assert cli.main([
        "--root", str(tmp_path), *lead_dispatch["owed_action"]["argv"][3:], "--quiet",
    ]) == 0
    lead.finalize(lead_record, lead.resolve(lead_record))
    ledger = _ledger(lead)
    winner_sets = [
        row["data"]["winning_ids"]
        for row in ledger["transitions"]
        if row["transition"] == "BROADCAST_POLICY_SATISFIED"
    ]

    assert all(len(winners) == 2 for winners in winner_sets)


def test_transfer_and_policy_close_win_against_stale_dispatch_reservations(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    source = _gate(store, policy_agents=("beta", "gamma"))
    stale = source.admit_or_finalize(_record(store))
    assert stale.key is not None
    transferred = store.send(
        sender="alpha",
        recipient="gamma",
        kind="question",
        body="transfer target",
        meta={"request_id": "q-1"},
    )
    source.transfer(stale.key, destination="gamma", new_inbound_id=transferred.id)
    with pytest.raises(DispatchRefused):
        source.reserve_dispatch(stale, purpose="initial")

    second = _store(tmp_path.parent / "broadcast-race")
    members = ["beta", "gamma"]
    _broadcast_question(
        second,
        "beta",
        broadcast_id="b-race",
        members=members,
        policy="any",
    )
    _broadcast_question(
        second,
        "gamma",
        broadcast_id="b-race",
        members=members,
        policy="any",
    )
    beta = _gate(second, policy_agents=("beta", "gamma"))
    gamma = _gate(
        second,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    beta_record = _record(second)
    gamma_record = _record(second, agent="gamma")
    stale_member = gamma.admit_or_finalize(gamma_record)
    _answer_reserved(second.root, beta, beta_record)
    beta.finalize(beta_record, beta.resolve(beta_record))
    with pytest.raises(DispatchRefused):
        gamma.reserve_dispatch(stale_member, purpose="initial")


def test_legacy_broadcast_is_log_only_with_telemetry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="question",
        body="legacy broadcast",
        meta={"broadcast_id": "legacy-b"},
    )
    gate = _gate(store)

    resolution = gate.admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.NOT_OWED
    assert _ledger(gate)["telemetry"]["legacy_broadcast_records"] == 1


@pytest.mark.parametrize(
    ("category", "limit", "counter"),
    [
        ("operation_infra", 16, "operation_infra_attempts"),
        ("finalization", 12, "finalization_misses"),
    ],
)
def test_nonpaid_retry_bounds_persist_across_cycles(
    tmp_path: Path,
    category: str,
    limit: int,
    counter: str,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None
    for _index in range(limit - 1):
        assert gate.record_retry_barrier(admitted.key, category=category) is True
        gate.complete_retry_barrier(admitted.key, category=category)
    assert gate.record_retry_barrier(admitted.key, category=category) is False
    restarted = _gate(store, fence="wrapper-1")

    assert restarted.record_retry_barrier(admitted.key, category=category) is False
    assert _ledger(restarted)["obligations"][admitted.key.digest][counter] == limit


def test_terminal_append_before_cursor_crash_replays_without_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)
    terminal = gate.resolve(record)
    monkeypatch.setattr(
        gate,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(RuntimeError("crash barrier")),
    )
    with pytest.raises(RuntimeError, match="crash barrier"):
        gate.finalize(record, terminal)
    assert store.cursor("beta") == ""
    calls = 0

    def drive(_record: dict) -> bool:
        nonlocal calls
        calls += 1
        return True

    restarted = _gate(store, fence="wrapper-1")
    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=restarted,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=2,
    )

    assert calls == 0
    assert store.cursor("beta") == inbound.id


def test_unwritable_disposition_never_advances_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    monkeypatch.setattr(
        gate,
        "_write",
        lambda _ledger: (_ for _ in ()).throw(OSError("store unavailable")),
    )

    with pytest.raises(OSError, match="store unavailable"):
        gate.delivery_failed(record, admitted.key, reason="cannot persist")
    assert store.cursor("beta") == ""


def test_global_ledger_corruption_blocks_without_dispatch_or_queue_drain(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    gate.admit_or_finalize(_record(store))
    gate.path.write_text("{", encoding="utf-8")
    calls = 0

    def drive(_record: dict) -> bool:
        nonlocal calls
        calls += 1
        return True

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    assert calls == 0
    assert store.cursor("beta") == ""
    assert gate.proof_health_path.exists()
    assert json.loads(gate.path.read_text(encoding="utf-8"))["store_epoch"]
    health = json.loads(gate.proof_health_path.read_text(encoding="utf-8"))
    assert health["last_rebuild_succeeded"] is True
