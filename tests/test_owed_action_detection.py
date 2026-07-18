"""Detection-grade owed-action protocol acceptance tests (design v3.1.1 §14)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli, doctor, signing, threads
from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api
from agenttalk.wrapper.obligations import (
    COMPLIANCE_BREAKER_TRIP,
    DETECTION_GRADE,
    DispatchRefused,
    DetectionCommitGate,
    GateError,
    LedgerUnreadable,
    PolicySnapshot,
    ResolverState,
    StaleRevision,
    note_manual_close,
    operation_payload_digest,
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
    store.write_waiting(agent, {
        "mode": "wrapper-loop",
        "wrapper_generation": fence,
        "wait_token": fence,
        "pid": os.getpid(),
    })
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


def _record_captured_intent(
    store: Store,
    gate: DetectionCommitGate,
    permit,
) -> str:
    row = _ledger(gate)["obligations"][permit.key_digest]["reservations"][permit.nonce]
    intent = row["operation_intent"]
    body = permit.draft_path.read_text(encoding="utf-8")
    digest = operation_payload_digest(
        operation=intent["operation"],
        body=body,
        kind=intent["kind"],
        recipient=intent["recipient"],
        in_reply_to=intent.get("in_reply_to"),
        request_id=intent.get("request_id"),
        broadcast_id=intent.get("broadcast_id"),
        origin_request_id=intent.get("origin_request_id"),
        origin_inbound_id=intent.get("origin_inbound_id"),
    )
    store.record_operation_intent(
        sender="beta",
        operation_nonce=permit.nonce,
        operation_digest=digest,
        body=body,
        operation_intent=intent,
    )
    return digest


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


def _record_delivery_exhaustion(
    store: Store,
    gate: DetectionCommitGate,
    *,
    request_id: str,
    compliance_dominant: bool,
) -> None:
    _question(store, request_id)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(
        permit,
        action_attempted=not compliance_dominant,
        action_infra=not compliance_dominant,
    )
    current = gate.resolve(record)
    gate.delivery_failed(
        record,
        admitted.key,
        reason="test exhaustion",
        expected_revision=current.scoped_revision,
    )


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


def _transfer_question(
    store: Store,
    source_key,
    destination: str,
    destination_policy: PolicySnapshot,
    *,
    consult: object = False,
):
    return store.send(
        sender=source_key.requester,
        recipient=destination,
        kind="question",
        body="transferred question",
        meta={
            "request_id": source_key.correlation_id,
            "consult": consult,
            "transfer_from_key_digest": source_key.digest,
            "transfer_policy_generation": destination_policy.generation,
        },
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


def test_operator_disabled_policy_is_inactive_and_audited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    policy = PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": DETECTION_GRADE, "enabled": False}},
    }, "beta")
    gate = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    record = _record(store)

    resolution = gate.admit_or_finalize(record)

    assert policy.status == ResolverState.INACTIVE
    assert resolution.state == ResolverState.INACTIVE
    assert resolution.allows_legacy_commit is True
    assert any(
        row["transition"] == "POLICY_INACTIVE"
        for row in _ledger(gate)["transitions"]
    )
    assert gate.finalize(
        record,
        resolution,
        expected_revision=resolution.ledger_revision,
    ).state == ResolverState.INACTIVE
    assert store.cursor("beta") == inbound.id
    assert gate.status()["status"] == "INACTIVE"
    assert PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": "security", "enabled": False}},
    }, "beta").status == ResolverState.INACTIVE


@pytest.mark.parametrize("enabled", [None, 0, "false"])
def test_malformed_enabled_policy_fails_closed(enabled: object) -> None:
    snapshot = PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": DETECTION_GRADE, "enabled": enabled}},
    }, "beta")

    assert snapshot.status == ResolverState.BLOCKED_POLICY


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


def test_pre_admission_answer_cannot_satisfy_required_escalation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store, escalation_required=True)
    store.send(
        sender="beta",
        recipient="alpha",
        body="answered instead of escalating",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )

    resolution = _gate(store).admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None
    assert resolution.key.obligation_class == "human_escalation"


def test_pre_admission_human_escalation_normalizes_without_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store, escalation_required=True)
    gate = _gate(store)
    roster = gate.roster_snapshot()
    escalation = store.send(
        sender="beta",
        recipient="lead",
        kind="question",
        body="operator decision needed",
        meta={
            "request_id": "esc-1",
            "origin_request_id": "q-1",
            "origin_inbound_id": inbound.id,
            "in_reply_to": inbound.id,
            "expected_roster_revision": roster["revision"],
        },
    )
    record = _record(store)

    resolution = gate.admit_or_finalize(record)

    assert resolution.state == ResolverState.SATISFIED
    assert resolution.key is None
    assert resolution.evidence_id == escalation.id
    gate.finalize(record, resolution, expected_revision=resolution.ledger_revision)
    assert store.cursor("beta") == inbound.id


def test_pre_admission_delivery_failed_cannot_terminalize(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    gate._validated_messages()  # noqa: SLF001 - inject a forbidden canonical transition
    with store._exclusive_lock(gate.path.with_suffix(".lock"), timeout=10.0):
        ledger = gate._load()  # noqa: SLF001 - crash-boundary fixture
        gate._append(  # noqa: SLF001 - crash-boundary fixture
            ledger,
            "DELIVERY_FAILED",
            scope=inbound.id,
            data={"inbound_id": inbound.id},
        )
        gate._write(ledger)  # noqa: SLF001 - crash-boundary fixture

    resolution = gate.admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None


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


@pytest.mark.parametrize("stale_boundary", ["lease", "cursor"])
def test_reserve_dispatch_revalidates_live_fence_and_cursor_head(
    tmp_path: Path,
    stale_boundary: str,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))
    assert resolution.key is not None
    if stale_boundary == "lease":
        store.write_waiting("beta", {
            "mode": "wrapper-loop",
            "wrapper_generation": "wrapper-2",
        })
    else:
        store.advance_cursor("beta", inbound.id)

    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(resolution, purpose="initial")

    admission = _ledger(gate)["obligations"][resolution.key.digest]
    assert admission["paid_dispatches_total"] == 0


def test_reserve_dispatch_revalidates_readiness_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))
    assert resolution.key is not None
    with store._exclusive_lock(gate.path.with_suffix(".lock"), timeout=10.0):
        ledger = gate._load()  # noqa: SLF001 - generation-race fixture
        ledger["obligations"][resolution.key.digest]["readiness_generation"] = "stale"
        gate._write(ledger)  # noqa: SLF001 - generation-race fixture

    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(resolution, purpose="initial")

    admission = _ledger(gate)["obligations"][resolution.key.digest]
    assert admission["paid_dispatches_total"] == 0


def test_reservation_nonce_is_idempotent_and_purpose_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))
    assert resolution.key is not None
    nonce = "1" * 32
    budgets = {
        "token_ceiling": 4096,
        "wall_time_seconds": 60.0,
        "reserved_cost": 1.0,
        "concurrency": 1,
    }

    first = gate.reserve_dispatch(
        resolution,
        purpose="initial",
        nonce=nonce,
        budgets=budgets,
    )
    repeated = gate.reserve_dispatch(
        resolution,
        purpose="initial",
        nonce=nonce,
        budgets=budgets,
    )

    assert repeated == first
    assert _ledger(gate)["obligations"][resolution.key.digest][
        "paid_dispatches_total"
    ] == 1
    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(
            resolution,
            purpose="recovery",
            nonce=nonce,
            budgets=budgets,
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("token_ceiling", 0),
        ("wall_time_seconds", float("inf")),
        ("reserved_cost", 101.0),
        ("concurrency", 2),
    ],
)
def test_reservation_rejects_unbounded_or_exhausted_per_call_budget(
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))
    assert resolution.key is not None
    budgets = {
        "token_ceiling": 4096,
        "wall_time_seconds": 60.0,
        "reserved_cost": 1.0,
        "concurrency": 1,
    }
    budgets[field] = invalid

    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(resolution, purpose="initial", budgets=budgets)

    assert _ledger(gate)["obligations"][resolution.key.digest][
        "paid_dispatches_total"
    ] == 0


def test_competing_reservations_have_one_cas_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    owner = _gate(store)
    resolution = owner.admit_or_finalize(_record(store))
    assert resolution.key is not None
    barrier = threading.Barrier(3)
    permits: list[object] = []
    refusals: list[Exception] = []

    def reserve(nonce: str) -> None:
        contender = _gate(store)
        barrier.wait()
        try:
            permits.append(contender.reserve_dispatch(
                resolution,
                purpose="initial",
                nonce=nonce,
            ))
        except (DispatchRefused, StaleRevision) as exc:
            refusals.append(exc)

    workers = [
        threading.Thread(target=reserve, args=(character * 32,))
        for character in ("a", "b")
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert len(permits) == 1
    assert len(refusals) == 1
    ledger = _ledger(owner)
    admission = ledger["obligations"][resolution.key.digest]
    assert admission["paid_dispatches_total"] == 1
    assert sum(
        row["transition"] == "DISPATCH_RESERVED"
        for row in ledger["transitions"]
    ) == 1


def test_concurrency_budget_is_agent_wide_across_scoped_admissions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-1")
    _question(store, "q-2")
    gate = _gate(store)
    first = gate.admit_or_finalize(_record(store, "q-1"))
    second = gate.admit_or_finalize(_record(store, "q-2"))
    assert first.key is not None and second.key is not None

    gate.reserve_dispatch(first, purpose="initial")
    with pytest.raises(DispatchRefused, match="concurrency"):
        gate.reserve_dispatch(second, purpose="initial")

    ledger = _ledger(gate)
    assert sum(
        row.get("state") in {"reserved", "dispatching"}
        for admission in ledger["obligations"].values()
        if isinstance(admission, dict)
        for row in admission.get("reservations", {}).values()
        if isinstance(row, dict)
    ) == 1


def test_terminal_admission_keeps_its_inflight_concurrency_slot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-1")
    _question(store, "q-2")
    gate = _gate(store)
    first_record = _record(store, "q-1")
    first = gate.admit_or_finalize(first_record)
    assert first.key is not None
    permit = gate.reserve_dispatch(first, purpose="initial")
    gate.dispatch_record(first_record, permit)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="rescind",
        body="withdrawn while dispatch is live",
        meta={"request_id": "q-1"},
    )
    terminal = gate.resolve(first_record)
    gate.finalize(first_record, terminal)
    second = gate.admit_or_finalize(_record(store, "q-2"))

    assert terminal.state == ResolverState.SUPERSEDED
    with pytest.raises(DispatchRefused, match="concurrency"):
        gate.reserve_dispatch(second, purpose="initial")

    admission = _ledger(gate)["obligations"][first.key.digest]
    assert admission["state"] == "finalized"
    assert admission["reservations"][permit.nonce]["state"] == "dispatching"


def test_dead_owner_terminal_admission_releases_slot_for_next_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-1")
    _question(store, "q-2")
    owner = _gate(store, fence="wrapper-1")
    first_record = _record(store, "q-1")
    first = owner.admit_or_finalize(first_record)
    assert first.key is not None
    old_permit = owner.reserve_dispatch(first, purpose="initial")
    owner.dispatch_record(first_record, old_permit)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="rescind",
        body="withdrawn while dispatch is live",
        meta={"request_id": "q-1"},
    )
    terminal = owner.resolve(first_record)
    owner.finalize(first_record, terminal)
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    second = restarted.admit_or_finalize(_record(store, "q-2"))
    new_permit = restarted.reserve_dispatch(second, purpose="initial")

    ledger = _ledger(restarted)
    old_row = ledger["obligations"][first.key.digest]["reservations"][
        old_permit.nonce
    ]
    assert new_permit.paid_dispatches_total == 1
    assert old_row["state"] == "cancelled_terminal"
    assert any(
        row["transition"] == "TERMINAL_DISPATCH_RECONCILED"
        and row.get("key_digest") == first.key.digest
        for row in ledger["transitions"]
    )


def test_unknown_admission_state_never_releases_a_dispatch_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-1")
    _question(store, "q-2")
    owner = _gate(store, fence="wrapper-1")
    first_record = _record(store, "q-1")
    first = owner.admit_or_finalize(first_record)
    assert first.key is not None
    old_permit = owner.reserve_dispatch(first, purpose="initial")
    owner.dispatch_record(first_record, old_permit)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="rescind",
        body="withdrawn while dispatch is live",
        meta={"request_id": "q-1"},
    )
    owner.finalize(first_record, owner.resolve(first_record))
    ledger = _ledger(owner)
    ledger["obligations"][first.key.digest]["state"] = "unknown-corrupt-state"
    owner.path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    second = restarted.admit_or_finalize(_record(store, "q-2"))
    with pytest.raises(DispatchRefused, match="concurrency"):
        restarted.reserve_dispatch(second, purpose="initial")

    old_row = _ledger(restarted)["obligations"][first.key.digest]["reservations"][
        old_permit.nonce
    ]
    assert old_row["state"] == "dispatching"


def test_managed_mode_transition_cannot_cancel_a_live_ordinary_dispatch(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-1")
    _question(store, "q-2")
    ordinary = _gate(store, fence="wrapper-1")
    first_record = _record(store, "q-1")
    first = ordinary.admit_or_finalize(first_record)
    assert first.key is not None
    old_permit = ordinary.reserve_dispatch(first, purpose="initial")
    ordinary.dispatch_record(first_record, old_permit)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="rescind",
        body="withdrawn while dispatch is live",
        meta={"request_id": "q-1"},
    )
    ordinary.finalize(first_record, ordinary.resolve(first_record))
    store.set_managed_lead_loop("beta")
    lease = store.acquire_lead_loop_lease(
        "beta",
        owner_pid=os.getpid(),
        wrapper_generation="wrapper-2",
    )
    assert lease is not None

    managed = _gate(store, fence="wrapper-2")
    second = managed.admit_or_finalize(_record(store, "q-2"))
    with pytest.raises(DispatchRefused, match="concurrency"):
        managed.reserve_dispatch(second, purpose="initial")

    old_row = _ledger(managed)["obligations"][first.key.digest]["reservations"][
        old_permit.nonce
    ]
    assert old_row["state"] == "dispatching"


def test_missing_waiting_marker_refuses_paid_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None
    store.clear_waiting("beta")

    with pytest.raises(DispatchRefused, match="fence"):
        gate.reserve_dispatch(admitted, purpose="initial")

    assert _ledger(gate)["obligations"][admitted.key.digest][
        "paid_dispatches_total"
    ] == 0


def test_reservation_precedes_drive_and_second_dispatch_is_xor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    resolution = gate.admit_or_finalize(_record(store))

    first = gate.reserve_dispatch(resolution, purpose="initial")
    assert first.paid_dispatches_total == 1
    gate.dispatch_record(_record(store), first)
    gate.mark_dispatch_result(first, action_attempted=False)
    refreshed = gate.resolve(_record(store))
    second = gate.reserve_dispatch(refreshed, purpose="recovery")
    assert second.paid_dispatches_total == 2

    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(gate.resolve(_record(store)), purpose="continuation")


@pytest.mark.parametrize("scoped", [False, True])
def test_action_rejected_gets_one_corrected_retry_within_paid_cap(
    tmp_path: Path,
    scoped: bool,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    purposes: list[str] = []

    def drive(record: dict) -> loop.DriveOutcome:
        purposes.append(record["owed_action"]["purpose"])
        if len(purposes) == 1:
            return loop.DriveOutcome(
                ok=False,
                failure_class=loop.CLASS_AMBIGUOUS,
                summary="deterministic schema rejection",
                bus_action_attempted=True,
                bus_action_rejected=True,
            )
        Path(record["owed_action"]["draft_path"]).write_text(
            "399",
            encoding="utf-8",
        )
        assert cli.main([
            "--root",
            str(tmp_path),
            *record["owed_action"]["argv"][3:],
            "--quiet",
        ]) == 0
        return loop.DriveOutcome(ok=True, bus_action_attempted=True)

    turns = loop.run_loop(
        store,
        "beta",
        drive,
        only_request_id="q-1" if scoped else None,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_turns=1,
        max_polls=4,
    )

    admission = next(iter(_ledger(gate)["obligations"].values()))
    transitions = _ledger(gate)["transitions"]
    rejected = [
        index
        for index, row in enumerate(transitions)
        if row["transition"] == "ACTION_REJECTED"
    ]
    recovery = next(
        index
        for index, row in enumerate(transitions)
        if row["transition"] == "DISPATCH_RESERVED"
        and row["data"].get("purpose") == "recovery"
    )
    first_nonce = next(iter(admission["reservations"]))

    assert turns == 1
    assert purposes == ["initial", "recovery"]
    assert admission["paid_dispatches_total"] == 2
    assert admission["paid_recoveries_total"] == 1
    assert admission["reservations"][first_nonce]["state"] == "action_rejected"
    assert len(rejected) == 1
    assert rejected[0] < recovery


def test_wrong_class_retry_waits_for_durable_rejection_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, escalation_required=True)
    owner = _gate(store, fence="wrapper-1")
    record = _record(store)
    admitted = owner.admit_or_finalize(record)
    assert admitted.key is not None
    first = owner.reserve_dispatch(admitted, purpose="initial")
    owner.dispatch_record(record, first)
    first.draft_path.write_text("I answered instead", encoding="utf-8")
    assert cli.main([
        "--root",
        str(tmp_path),
        "reply",
        "--from",
        "beta",
        "--to-id",
        inbound.id,
        "--operation-nonce",
        first.nonce,
        "--file",
        str(first.draft_path),
        "--quiet",
    ]) == 0
    # Model the old split-write crash point: the side effect/result is durable,
    # but its replay-proven wrong class has not yet been classified.
    owner.mark_dispatch_result(first, action_attempted=True)
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    purposes: list[str] = []

    def corrected_drive(recovery_record: dict) -> loop.DriveOutcome:
        owed = recovery_record["owed_action"]
        purposes.append(owed["purpose"])
        Path(owed["draft_path"]).write_text(
            "Operator decision needed",
            encoding="utf-8",
        )
        assert cli.main([
            "--root",
            str(tmp_path),
            *owed["argv"][3:],
            "--quiet",
        ]) == 0
        return loop.DriveOutcome(ok=True, bus_action_attempted=True)

    assert loop.run_loop(
        store,
        "beta",
        corrected_drive,
        commit_gate=restarted,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_turns=1,
        max_polls=2,
    ) == 1

    ledger = _ledger(restarted)
    admission = ledger["obligations"][admitted.key.digest]
    rejection = next(
        index
        for index, row in enumerate(ledger["transitions"])
        if row["transition"] == "ACTION_REJECTED"
    )
    recovery = next(
        index
        for index, row in enumerate(ledger["transitions"])
        if row["transition"] == "DISPATCH_RESERVED"
        and row["data"].get("purpose") == "recovery"
    )
    assert purposes == ["recovery"]
    assert admission["paid_dispatches_total"] == 2
    assert admission["reservations"][first.nonce]["state"] == "action_rejected"
    assert rejection < recovery


def test_unsatisfied_attempt_revalidation_failure_returns_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    first = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, first)
    gate.mark_dispatch_result(first, action_attempted=True)
    original = gate._validated_messages
    reads = 0

    def fail_second_read():
        nonlocal reads
        reads += 1
        if reads == 2:
            raise LedgerUnreadable("injected second replay failure")
        return original()

    monkeypatch.setattr(gate, "_validated_messages", fail_second_read)

    blocked = gate.resolve(record)

    assert blocked.state == ResolverState.BLOCKED
    assert "injected second replay failure" in blocked.reason
    assert _ledger(gate)["obligations"][admitted.key.digest][
        "paid_dispatches_total"
    ] == 1


@pytest.mark.parametrize(
    ("first_class", "second_class", "second_purpose", "expected_states"),
    [
        ("missing", "infrastructure", "recovery", ("completed", "uncaptured_infra")),
        ("infrastructure", "rejected", "continuation", ("uncaptured_infra", "action_rejected")),
        ("rejected", "ambiguous", "recovery", ("action_rejected", "completed")),
        ("ambiguous", "missing", "continuation", ("completed", "completed")),
        ("wrong_class", "wrong_class", "recovery", ("action_rejected", "action_rejected")),
    ],
)
def test_all_class_mixes_never_reserve_a_third_paid_dispatch(
    tmp_path: Path,
    first_class: str,
    second_class: str,
    second_purpose: str,
    expected_states: tuple[str, str],
) -> None:
    store = _store(tmp_path)
    inbound = _question(
        store,
        escalation_required="wrong_class" in {first_class, second_class},
    )
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None

    def classify(permit, classification: str) -> None:
        if classification == "infrastructure":
            gate.mark_dispatch_result(
                permit,
                action_attempted=True,
                action_infra=True,
            )
        elif classification == "rejected":
            gate.mark_dispatch_result(
                permit,
                action_attempted=True,
                action_rejected=True,
            )
        elif classification == "wrong_class":
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
            gate.mark_dispatch_result(permit, action_attempted=True)
            assert gate.resolve(record).state == ResolverState.OWED_UNSATISFIED
            gate.mark_unsatisfied_attempt(
                permit,
                reason="answered instead of escalating",
            )
        elif classification == "ambiguous":
            # Ambiguous/no-authoritative-evidence and explicit missing both
            # reach the gate as non-proof; the wrapper's failure class only
            # affects diagnostics outside the paid reservation ledger.
            gate.mark_dispatch_result(permit, action_attempted=False)
        else:
            gate.mark_dispatch_result(permit, action_attempted=False)

    first = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, first)
    classify(first, first_class)
    if second_purpose == "continuation":
        gate.schedule_continuation(admitted.key, producer_token="producer-1")
    second = gate.reserve_dispatch(gate.resolve(record), purpose=second_purpose)
    gate.dispatch_record(record, second)
    classify(second, second_class)

    with pytest.raises(DispatchRefused, match="paid dispatch budget exhausted"):
        gate.reserve_dispatch(
            gate.resolve(record),
            purpose="continuation" if second_purpose == "recovery" else "recovery",
            nonce="f" * 32,
        )

    ledger = _ledger(gate)
    admission = ledger["obligations"][admitted.key.digest]
    assert admission["paid_dispatches_total"] == 2
    assert admission["paid_initial_dispatches_total"] == 1
    assert admission["paid_recoveries_total"] == (second_purpose == "recovery")
    assert admission["paid_continuations_total"] == (second_purpose == "continuation")
    assert admission["reservations"][first.nonce]["state"] == expected_states[0]
    assert admission["reservations"][second.nonce]["state"] == expected_states[1]
    assert sum(
        row["transition"] == "ACTION_REJECTED"
        for row in ledger["transitions"]
    ) == sum(
        classification in {"rejected", "wrong_class"}
        for classification in (first_class, second_class)
    )


def test_dispatch_record_rejects_a_tampered_permit_draft_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    tampered = replace(permit, draft_path=tmp_path / "outside-owned.txt")

    with pytest.raises(DispatchRefused, match="draft path"):
        gate.dispatch_record(record, tampered)

    admission = _ledger(gate)["obligations"][permit.key_digest]
    assert admission["reservations"][permit.nonce]["state"] == "reserved"
    assert not tampered.draft_path.exists()


def test_scheduled_continuation_excludes_recovery_before_second_launch(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    resolution = gate.admit_or_finalize(record)
    assert resolution.key is not None
    first = gate.reserve_dispatch(resolution, purpose="initial")
    gate.dispatch_record(record, first)
    gate.mark_dispatch_result(first, action_attempted=False)
    gate.schedule_continuation(resolution.key, producer_token="producer-1")

    assert gate.next_dispatch_purpose(resolution.key) == "continuation"
    refreshed = gate.resolve(record)
    with pytest.raises(DispatchRefused):
        gate.reserve_dispatch(refreshed, purpose="recovery")
    continuation = gate.reserve_dispatch(refreshed, purpose="continuation")
    admission = _ledger(gate)["obligations"][resolution.key.digest]
    assert continuation.paid_dispatches_total == 2
    assert admission["paid_continuations_total"] == 1
    assert admission["recovery_used"] is False


def test_missing_dispatch_result_is_durable_before_recovery_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    resolution = gate.admit_or_finalize(record)
    assert resolution.key is not None
    first = gate.reserve_dispatch(resolution, purpose="initial")
    gate.dispatch_record(record, first)

    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )
    restarted = _gate(store, fence="wrapper-2")
    refreshed = restarted.resolve(record)
    assert refreshed.state == ResolverState.OWED_UNSATISFIED
    assert restarted.next_dispatch_purpose(resolution.key) == "recovery"
    ledger = _ledger(restarted)
    missing = [
        row for row in ledger["transitions"]
        if row["transition"] == "DISPATCH_RESULT_MISSING"
    ]
    assert len(missing) == 1
    second = restarted.reserve_dispatch(refreshed, purpose="recovery")
    assert second.paid_dispatches_total == 2
    transitions = _ledger(restarted)["transitions"]
    assert next(
        index for index, row in enumerate(transitions)
        if row["transition"] == "DISPATCH_RESULT_MISSING"
    ) < next(
        index for index, row in enumerate(transitions)
        if row["transition"] == "DISPATCH_RESERVED" and row["data"]["total"] == 2
    )


def test_live_same_fence_owner_cannot_be_declared_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)

    contender = _gate(store, fence="wrapper-1")

    assert contender.next_dispatch_purpose(admitted.key) is None
    admission = _ledger(contender)["obligations"][admitted.key.digest]
    assert admission["paid_dispatches_total"] == 1
    assert [row["state"] for row in admission["reservations"].values()] == [
        "dispatching"
    ]


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
        assert any(
            row["state"] == "dispatching"
            for row in admission["reservations"].values()
        )
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


@pytest.mark.parametrize("scoped", [False, True])
def test_loop_never_dispatches_when_reservation_cas_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
) -> None:
    store = _store(tmp_path)
    request_id = "q-scoped" if scoped else "q-global"
    _question(store, request_id)
    gate = _gate(store)
    calls = 0

    def refuse(*_args, **_kwargs):
        raise DispatchRefused("injected reservation CAS loss")

    def drive(_record: dict) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(gate, "reserve_dispatch", refuse)
    loop.run_loop(
        store,
        "beta",
        drive,
        only_request_id=request_id if scoped else None,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    assert calls == 0
    assert store.cursor("beta") == ""


def test_loop_routes_scheduled_second_call_through_continuation_reservation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    purposes: list[str] = []

    def drive(record: dict) -> loop.DriveOutcome:
        purpose = record["owed_action"]["purpose"]
        purposes.append(purpose)
        if purpose == "initial":
            admission = _ledger(gate)["obligations"][
                record["owed_action"]["obligation_key_digest"]
            ]
            gate.schedule_continuation(
                gate._key_from(admission["key"]),  # noqa: SLF001 - loop contract fixture
                producer_token="producer-1",
            )
        return loop.DriveOutcome(ok=True)

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=3,
    )

    assert purposes == ["initial", "continuation"]
    admission = next(iter(_ledger(gate)["obligations"].values()))
    assert admission["paid_dispatches_total"] == 2
    assert admission["paid_continuations_total"] == 1
    assert admission["paid_recoveries_total"] == 0


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


def test_durable_retry_barrier_records_missing_outcome_before_restart_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    permit.draft_path.write_text("captured answer", encoding="utf-8")
    _record_captured_intent(store, gate, permit)
    gate.mark_dispatch_result(
        permit,
        action_attempted=True,
        action_infra=True,
    )
    current = gate.resolve(record)

    def crash_after_barrier(*_args, **_kwargs):
        admission = _ledger(gate)["obligations"][admitted.key.digest]
        assert admission["operation_infra_attempts"] == 2
        assert admission["operation_infra_retry_inflight"] is True
        raise RuntimeError("captured retry crash")

    assert gate.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=current.scoped_revision,
    ) is True
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations.subprocess.run",
        crash_after_barrier,
    )
    with pytest.raises(RuntimeError, match="captured retry crash"):
        gate.retry_captured_operation(permit, record)
    restarted = _gate(store, fence="wrapper-1")
    assert restarted.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=current.scoped_revision,
    ) is True
    admission = _ledger(restarted)["obligations"][admitted.key.digest]

    assert admission["operation_infra_attempts"] == 3
    assert admission["operation_infra_first_at"]
    transitions = _ledger(restarted)["transitions"]
    missing_index = next(
        index for index, row in enumerate(transitions)
        if row["transition"] == "OPERATION_RETRY_OUTCOME_MISSING"
    )
    retry_indexes = [
        index for index, row in enumerate(transitions)
        if row["transition"] == "OPERATION_RETRY_RECORDED"
    ]
    assert missing_index < retry_indexes[-1]
    restarted.complete_retry_barrier(admitted.key, category="operation_infra")
    assert restarted.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=current.scoped_revision,
    ) is True
    assert (
        _ledger(restarted)["obligations"][admitted.key.digest][
            "operation_infra_attempts"
        ]
        == 4
    )


def test_captured_escalation_retry_reuses_persisted_target_and_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, escalation_required=True)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    permit.draft_path.write_text("captured operator request", encoding="utf-8")
    _record_captured_intent(store, gate, permit)
    gate.mark_dispatch_result(permit, action_attempted=True, action_infra=True)
    current = gate.resolve(record)
    assert gate.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=current.scoped_revision,
    ) is True
    store.set_operator_facing("gamma")
    observed: list[str] = []

    def capture(argv, **_kwargs):
        observed.extend(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("agenttalk.wrapper.obligations.subprocess.run", capture)

    assert gate.retry_captured_operation(permit, record) is True
    initial = dispatched["owed_action"]["argv"]
    assert observed[observed.index("--to") + 1] == initial[initial.index("--to") + 1]
    assert observed[observed.index("--meta") + 1] == initial[initial.index("--meta") + 1]


def test_unmarked_torn_draft_is_never_retried_as_a_captured_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    owner = _gate(store, fence="wrapper-1")
    record = _record(store)
    admitted = owner.admit_or_finalize(record)
    assert admitted.key is not None
    permit = owner.reserve_dispatch(admitted, purpose="initial")
    owner.dispatch_record(record, permit)
    permit.draft_path.write_text("TRUNCATED MID-WRITE: ", encoding="utf-8")
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    restarted.resolve(record)

    assert restarted.captured_operation(admitted.key) is None
    assert restarted.next_dispatch_purpose(admitted.key) == "recovery"
    row = _ledger(restarted)["obligations"][admitted.key.digest]["reservations"][
        permit.nonce
    ]
    assert row["state"] == "dispatch_result_missing"


def test_draft_capture_survives_new_generation_without_second_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    owner = _gate(store, fence="wrapper-1")
    record = _record(store)
    admitted = owner.admit_or_finalize(record)
    assert admitted.key is not None
    permit = owner.reserve_dispatch(admitted, purpose="initial")
    owner.dispatch_record(record, permit)
    permit.draft_path.write_text("captured before append", encoding="utf-8")
    _record_captured_intent(store, owner, permit)
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    recovered = restarted.resolve(record)
    captured = restarted.captured_operation(admitted.key)

    assert recovered.state == ResolverState.OWED_UNSATISFIED
    assert captured is not None
    assert captured.nonce == permit.nonce
    assert restarted.next_dispatch_purpose(admitted.key) is None
    admission = _ledger(restarted)["obligations"][admitted.key.digest]
    assert admission["paid_dispatches_total"] == 1
    assert admission["operation_infra_attempts"] == 1
    assert admission["operation_infra_first_at"]
    assert admission["reservations"][permit.nonce]["operation_payload_digest"]
    assert any(
        row["transition"] == "OPERATION_INTENT_RECOVERED"
        for row in _ledger(restarted)["transitions"]
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
    failed = gate.delivery_failed(
        _record(store),
        admitted.key,
        reason="test failure",
        expected_revision=admitted.scoped_revision,
    )
    assert failed.state == ResolverState.INDETERMINATE
    assert failed.reason == "cursor projection failed: OSError"

    ledger = _ledger(gate)
    assert ledger["delivery_index"]["q-1"][0]["state"] == "delivery_failed"
    assert ledger["cursor_dispositions"]["beta"]["inbound_id"] == inbound.id
    assert store.cursor("beta") == ""

    restarted = _gate(store)
    terminal = restarted.resolve(_record(store))
    assert terminal.state == ResolverState.DELIVERY_EXHAUSTED
    restarted.finalize(_record(store), terminal)
    assert store.cursor("beta") == inbound.id
    assert restarted.delivery_status("q-1", "alpha")["state"] == "delivery_failed"
    assert _ledger(restarted)["obligations"][admitted.key.digest]["state"] == (
        "delivery_failed"
    )


def test_delivery_failed_is_idempotent_for_one_exact_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(permit, action_attempted=False)
    current = gate.resolve(record)

    first = gate.delivery_failed(
        record,
        admitted.key,
        reason="canonical reason",
        expected_revision=current.scoped_revision,
    )
    second = gate.delivery_failed(
        record,
        admitted.key,
        reason="must be ignored",
        expected_revision=current.scoped_revision,
    )

    assert first.state == second.state == ResolverState.DELIVERY_EXHAUSTED
    ledger = _ledger(gate)
    assert sum(
        row["transition"] == "DELIVERY_FAILED" for row in ledger["transitions"]
    ) == 1
    assert len(ledger["delivery_index"]["q-1"]) == 1
    assert ledger["breakers"]["beta"][
        "owed_action_cap_exhaustions_consecutive"
    ] == 1
    assert second.reason == "canonical reason"


def test_delivery_failed_rejects_a_mismatched_exact_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store, "q-1")
    _question(store, "q-2")
    gate = _gate(store)
    first_record = _record(store, "q-1")
    admitted = gate.admit_or_finalize(first_record)
    assert admitted.key is not None

    with pytest.raises(GateError, match="exact inbound"):
        gate.delivery_failed(
            _record(store, "q-2"),
            admitted.key,
            reason="wrong record",
            expected_revision=admitted.scoped_revision,
        )

    ledger = _ledger(gate)
    assert not [
        row for row in ledger["transitions"] if row["transition"] == "DELIVERY_FAILED"
    ]


def test_delivery_failed_loses_when_scoped_revision_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    store.send(
        sender="beta",
        recipient="alpha",
        kind="composing",
        body="new scoped evidence",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    gate.resolve(record)

    with pytest.raises(StaleRevision, match="scoped revision"):
        gate.delivery_failed(
            record,
            admitted.key,
            reason="stale failure disposition",
            expected_revision=admitted.scoped_revision,
        )

    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["state"] == "open"


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
    assert breaker["config_blocked"] is True
    assert breaker["config_blocked_reason"] == "owed_action_compliance_breaker"
    assert breaker["owed_action_cap_exhaustions_consecutive"] == 3
    assert len(breaker["compliance_exhaustion_references"]) == 3
    assert calls == 6
    assert store.cursor("beta") == messages[2].id
    alerts = [
        event for event in _ledger(gate)["transitions"]
        if event["transition"] == "COMPLIANCE_BREAKER_ALERT"
    ]
    assert len(alerts) == 1
    delivered_alerts = [
        message
        for message in store.valid_messages()
        if message.sender == "beta"
        and message.recipient == "lead"
        and (message.meta or {}).get("compliance_breaker_alert_generation")
        == breaker["generation"]
    ]
    assert len(delivered_alerts) == 1


def test_breaker_trip_persists_config_block_before_cursor_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store)
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-crash-1",
        compliance_dominant=True,
    )
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-crash-2",
        compliance_dominant=True,
    )
    inbound = _question(store, "q-crash-3")
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(permit, action_attempted=False)
    current = gate.resolve(record)
    monkeypatch.setattr(
        gate,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(RuntimeError("cursor crash")),
    )

    failed = gate.delivery_failed(
        record,
        admitted.key,
        reason="third compliance exhaustion",
        expected_revision=current.scoped_revision,
    )
    assert failed.state == ResolverState.INDETERMINATE

    assert store.cursor("beta") != inbound.id
    assert _ledger(gate)["breakers"]["beta"]["tripped"] is True
    hold = store.read_config_blocked_hold("beta")
    assert hold is not None
    assert hold["summary"] == "owed_action_compliance_breaker"
    restarted = _gate(store, fence="wrapper-1")
    terminal = restarted.resolve(record)
    assert terminal.state == ResolverState.DELIVERY_EXHAUSTED
    restarted.finalize(record, terminal)
    assert store.cursor("beta") == inbound.id
    blocked_inbound = _question(store, "q-crash-4")
    blocked = restarted.resolve(_record(store))
    assert blocked.state == ResolverState.BLOCKED_COMPLIANCE
    assert blocked_inbound.id not in _ledger(restarted)["inbound_index"]


def test_breaker_alert_outbox_reconciles_crash_after_bus_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store)
    for index in range(2):
        _record_delivery_exhaustion(
            store,
            gate,
            request_id=f"q-alert-crash-{index}",
            compliance_dominant=True,
        )
    _question(store, "q-alert-crash-3")
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(permit, action_attempted=False)
    current = gate.resolve(record)
    publish = store.send_operation

    def publish_then_crash(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("crash after alert publication")

    monkeypatch.setattr(store, "send_operation", publish_then_crash)
    with pytest.raises(RuntimeError, match="after alert publication"):
        gate.delivery_failed(
            record,
            admitted.key,
            reason="third compliance exhaustion",
            expected_revision=current.scoped_revision,
        )
    monkeypatch.setattr(store, "send_operation", publish)

    before = _ledger(gate)
    generation = before["breakers"]["beta"]["generation"]
    assert before["breakers"]["beta"]["alerts"][str(generation)]["state"] == "pending"
    assert not [
        row for row in before["transitions"]
        if row["transition"] == "COMPLIANCE_BREAKER_ALERT"
    ]
    store.set_operator_facing("gamma")
    restarted = _gate(store, fence="wrapper-1")
    assert restarted.resolve(record).state == ResolverState.DELIVERY_EXHAUSTED

    delivered = [
        message for message in store.valid_messages()
        if (message.meta or {}).get("compliance_breaker_alert_generation") == generation
    ]
    after = _ledger(restarted)
    assert len(delivered) == 1
    assert delivered[0].recipient == "lead"
    assert after["breakers"]["beta"]["alerts"][str(generation)]["state"] == "delivered"
    assert sum(
        row["transition"] == "COMPLIANCE_BREAKER_ALERT"
        for row in after["transitions"]
    ) == 1


def test_breaker_hold_projection_failure_heals_on_terminal_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store)
    for index in range(2):
        _record_delivery_exhaustion(
            store,
            gate,
            request_id=f"q-hold-crash-{index}",
            compliance_dominant=True,
        )
    _question(store, "q-hold-crash-3")
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(permit, action_attempted=False)
    current = gate.resolve(record)
    project = store.write_config_blocked_hold
    monkeypatch.setattr(
        store,
        "write_config_blocked_hold",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("hold projection crash")),
    )

    with pytest.raises(OSError, match="hold projection crash"):
        gate.delivery_failed(
            record,
            admitted.key,
            reason="third compliance exhaustion",
            expected_revision=current.scoped_revision,
        )
    assert _ledger(gate)["breakers"]["beta"]["tripped"] is True
    assert store.read_config_blocked_hold("beta") is None
    monkeypatch.setattr(store, "write_config_blocked_hold", project)

    restarted = _gate(store, fence="wrapper-1")
    assert restarted.resolve(record).state == ResolverState.DELIVERY_EXHAUSTED
    hold = store.read_config_blocked_hold("beta")
    assert hold is not None
    assert hold["summary"] == "owed_action_compliance_breaker"


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


def test_delayed_prior_generation_answer_does_not_satisfy_same_rid_reask(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _question(store, "reused")
    store.advance_cursor("beta", first.id)
    second = _question(store, "reused")
    store.send(
        sender="beta",
        recipient="alpha",
        body="delayed old answer",
        meta={"request_id": "reused", "in_reply_to": first.id},
    )

    resolution = _gate(store).admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None and resolution.key.inbound_id == second.id


def test_pre_admission_answer_requires_exact_correlation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.send(
        sender="beta",
        recipient="alpha",
        body="wrong correlation",
        meta={
            "request_id": "q-other",
            "origin_request_id": "q-1",
            "in_reply_to": inbound.id,
        },
    )

    resolution = _gate(store).admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.OWED_UNSATISFIED
    assert resolution.key is not None


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


def test_semantic_no_admission_claim_rejects_correlated_revision_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, consult=True)
    gate = _gate(store)
    original = gate._validated_messages  # noqa: SLF001 - deterministic CAS race

    def replay_then_race():
        messages, ledger = original()
        store.send(
            sender="gamma",
            recipient="beta",
            body="correlated concurrent append",
            meta={"request_id": "q-1"},
        )
        return messages, ledger

    monkeypatch.setattr(gate, "_validated_messages", replay_then_race)

    resolution = gate.admit_or_finalize(_record(store))

    assert resolution.state == ResolverState.INDETERMINATE
    assert not _ledger(gate)["no_admission_claims"]


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
    first_health = json.loads(gate.proof_health_path.read_text(encoding="utf-8"))
    assert first_health["first_failure_at"] == "2026-01-01T00:00:00Z"
    assert first_health["last_failure_at"] == "2026-01-01T00:00:00Z"
    assert first_health["last_rebuild_at"] == "2026-01-01T00:00:00Z"
    assert first_health["elapsed_seconds"] == 0
    assert first_health.get("exhausted") is not True
    health = gate.record_proof_failure(error_class="DifferentError", path="other-path")

    assert health["exhausted"] is True
    assert health["first_failure_at"] == first_health["first_failure_at"]
    assert health["last_failure_at"] == "2026-01-01T00:15:01Z"
    assert health["failures"] == 2
    assert health["elapsed_seconds"] >= 900
    assert health["fingerprint"] == {
        "error_class": "DifferentError", "path": "other-path",
    }
    assert health["incident"]["kind"] == "PROOF_REPLAY_INCIDENT"
    assert health["incident"]["authority"] == "elapsed_time"
    assert health["incident_id"] == health["incident"]["incident_id"]
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
    gate.delivery_failed(
        _record(store),
        admitted.key,
        reason="delivery exhausted",
        expected_revision=admitted.scoped_revision,
    )

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


def test_infrastructure_exhaustion_uses_separate_counter_without_resetting_compliance(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store)
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-compliance-1",
        compliance_dominant=True,
    )
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-infra",
        compliance_dominant=False,
    )
    between = gate.status()["breaker"]
    assert between["owed_action_cap_exhaustions_consecutive"] == 1
    assert between["proof_infra_exhaustions_consecutive"] == 1
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-compliance-2",
        compliance_dominant=True,
    )
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-compliance-3",
        compliance_dominant=True,
    )

    breaker = gate.status()["breaker"]
    assert breaker["tripped"] is True
    assert breaker["owed_action_cap_exhaustions_consecutive"] == 3
    assert breaker["proof_infra_exhaustions_consecutive"] == 1


def test_final_infrastructure_failure_does_not_blame_an_earlier_missing_action(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    first = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, first)
    gate.mark_dispatch_result(first, action_attempted=False)
    recovery = gate.reserve_dispatch(gate.resolve(record), purpose="recovery")
    gate.dispatch_record(record, recovery)
    gate.mark_dispatch_result(
        recovery,
        action_attempted=True,
        action_infra=True,
    )
    current = gate.resolve(record)

    gate.delivery_failed(
        record,
        admitted.key,
        reason="captured operation exhausted",
        expected_revision=current.scoped_revision,
    )

    breaker = gate.status()["breaker"]
    assert breaker["owed_action_cap_exhaustions_consecutive"] == 0
    assert breaker["proof_infra_exhaustions_consecutive"] == 1


def test_missing_recovery_result_overrides_prior_compliance_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store, fence="wrapper-1")
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    first = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, first)
    gate.mark_dispatch_result(first, action_attempted=False)
    recovery = gate.reserve_dispatch(gate.resolve(record), purpose="recovery")
    gate.dispatch_record(record, recovery)
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    refreshed = restarted.resolve(record)
    assert restarted.next_dispatch_purpose(admitted.key) is None
    restarted.delivery_failed(
        record,
        admitted.key,
        reason="recovery result was lost",
        expected_revision=refreshed.scoped_revision,
    )

    assert refreshed.state == ResolverState.OWED_UNSATISFIED
    breaker = restarted.status()["breaker"]
    assert breaker["owed_action_cap_exhaustions_consecutive"] == 0
    assert breaker["proof_infra_exhaustions_consecutive"] == 1


def test_successful_finalization_resets_compliance_streak_before_cursor_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store)
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-reset-streak-1",
        compliance_dominant=True,
    )
    _question(store, "q-reset-streak-2")
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)
    terminal = gate.resolve(record)
    monkeypatch.setattr(
        gate,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(RuntimeError("cursor crash")),
    )

    finalized = gate.finalize(record, terminal)
    assert finalized.state == ResolverState.INDETERMINATE

    breaker = _ledger(gate)["breakers"]["beta"]
    assert breaker["owed_action_cap_exhaustions_consecutive"] == 0
    assert breaker["compliance_exhaustion_references"] == []


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
    trip_generation = gate.status()["breaker"]["generation"]

    with pytest.raises(PermissionError):
        gate.reset_compliance_breaker(actor="gamma", reason="not authorized")
    gate.reset_compliance_breaker(actor="lead", reason="operator approved prompt repair")

    breaker = gate.status()["breaker"]
    assert breaker["tripped"] is False
    assert breaker["generation"] == trip_generation + 1
    assert store.read_config_blocked_hold("beta") is None
    reset = next(
        row for row in reversed(_ledger(gate)["transitions"])
        if row["transition"] == "COMPLIANCE_BREAKER_RESET"
    )
    assert reset["data"] == {
        "agent": "beta",
        "actor": "lead",
        "reason": "operator approved prompt repair",
        "generation": breaker["generation"],
    }


def test_breaker_status_and_reset_never_clear_an_unrelated_config_hold(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store)
    _record_delivery_exhaustion(
        store,
        gate,
        request_id="q-infra",
        compliance_dominant=False,
    )
    store.write_config_blocked_hold("beta", summary="unrelated_launch_config_error")

    gate.status()
    gate.reset_compliance_breaker(actor="lead", reason="reset owed-action state")

    hold = store.read_config_blocked_hold("beta")
    assert hold is not None
    assert hold["summary"] == "unrelated_launch_config_error"


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

    unfenced = owner.finalize(record, resolution)
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

    assert unfenced.state == ResolverState.INDETERMINATE
    assert rejected.state == ResolverState.INDETERMINATE
    assert finalized.state == ResolverState.NOT_OWED


def test_open_no_admission_claim_is_recoverable_by_new_wrapper_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.send(
        sender="beta",
        recipient="alpha",
        body="399",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    record = _record(store)
    first = _gate(store, fence="wrapper-1").admit_or_finalize(record)
    store.write_waiting("beta", {
        "mode": "wrapper-loop",
        "wrapper_generation": "wrapper-2",
    })
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "dead",
    )

    restarted = _gate(store, fence="wrapper-2")
    recovered = restarted.admit_or_finalize(record)

    assert first.state == ResolverState.SATISFIED
    assert recovered.state == ResolverState.SATISFIED
    assert _ledger(restarted)["no_admission_claims"][inbound.id]["fence"] == "wrapper-2"
    restarted.finalize(
        record,
        recovered,
        expected_revision=recovered.ledger_revision,
    )
    assert store.cursor("beta") == inbound.id


def test_live_no_admission_claim_owner_cannot_be_displaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.send(
        sender="beta",
        recipient="alpha",
        body="399",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    record = _record(store)
    _gate(store, fence="wrapper-1").admit_or_finalize(record)
    store.write_waiting("beta", {
        "mode": "wrapper-loop",
        "wrapper_generation": "wrapper-2",
    })
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations._process_liveness",
        lambda _pid: "alive",
    )

    resolution = _gate(store, fence="wrapper-2").admit_or_finalize(record)

    assert resolution.state == ResolverState.INDETERMINATE
    assert _ledger(_gate(store))["no_admission_claims"][inbound.id]["fence"] == (
        "wrapper-1"
    )


def test_finalized_no_admission_disposition_replays_cursor_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    store.send(
        sender="beta",
        recipient="alpha",
        body="399",
        meta={"request_id": "q-1", "in_reply_to": inbound.id},
    )
    record = _record(store)
    owner = _gate(store, fence="wrapper-1")
    terminal = owner.admit_or_finalize(record)
    monkeypatch.setattr(
        owner,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(RuntimeError("crash barrier")),
    )
    finalized = owner.finalize(
        record,
        terminal,
        expected_revision=terminal.ledger_revision,
    )
    assert finalized.state == ResolverState.INDETERMINATE
    assert store.cursor("beta") == ""

    restarted = _gate(store, fence="wrapper-2")
    recovered = restarted.admit_or_finalize(record)
    restarted.finalize(
        record,
        recovered,
        expected_revision=recovered.ledger_revision,
    )

    assert store.cursor("beta") == inbound.id


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
    blocked = gate.status()
    assert blocked["status"] == "BLOCKED"
    assert blocked["proof_health"]["state"] == "blocked"
    assert store.cursor("beta") == ""

    assert gate.resolve(_record(store)).state == ResolverState.OWED_UNSATISFIED
    assert gate.status()["status"] == "ACTIVE (detection-grade)"


def test_unreadable_proof_health_is_visible_as_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    gate.admit_or_finalize(_record(store))
    gate.proof_health_path.write_text("{", encoding="utf-8")

    status = gate.status()

    assert status["status"] == "BLOCKED"
    assert status["proof_health"] == {"state": "blocked", "unreadable": True}


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


def test_operation_nonce_publication_is_atomic_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    original = cli._operation_idempotency  # noqa: SLF001 - crash-race fixture

    def run_pair(nonce: str, bodies: tuple[str, str]) -> list[int]:
        barrier = threading.Barrier(2)

        def synchronized(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(timeout=10)
            return result

        monkeypatch.setattr(cli, "_operation_idempotency", synchronized)
        results: list[int] = []

        def invoke(body: str) -> None:
            results.append(cli.main([
                "--root",
                str(tmp_path),
                "reply",
                "--from",
                "beta",
                "--to-id",
                inbound.id,
                "--operation-nonce",
                nonce,
                "-m",
                body,
                "--quiet",
            ]))

        workers = [threading.Thread(target=invoke, args=(body,)) for body in bodies]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
        assert all(not worker.is_alive() for worker in workers)
        monkeypatch.setattr(cli, "_operation_idempotency", original)
        return results

    same_nonce = "a" * 32
    assert sorted(run_pair(same_nonce, ("399", "399"))) == [0, 0]
    same = [
        message for message in store.valid_messages()
        if (message.meta or {}).get("operation_nonce") == same_nonce
    ]
    assert len(same) == 1

    conflicting_nonce = "b" * 32
    assert sorted(run_pair(conflicting_nonce, ("400", "401"))) == [0, 2]
    conflicting = [
        message for message in store.valid_messages()
        if (message.meta or {}).get("operation_nonce") == conflicting_nonce
    ]
    assert len(conflicting) == 1


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


def test_post_budget_composing_is_durably_missing_and_cannot_escape_paid_cap(
    tmp_path: Path,
) -> None:
    current = {"value": "2026-01-01T00:00:00Z"}
    store = _store(tmp_path)
    _question(store)
    gate = _gate(
        store,
        now=lambda: current["value"],
        producer_alive=lambda _token: True,
    )
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    first = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, first)
    gate.schedule_continuation(admitted.key, producer_token="producer-1")
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["composing_argv"][3:],
        "--quiet",
    ]) == 0
    current["value"] = "2026-01-01T01:00:00Z"
    assert gate.resolve(record).state == ResolverState.IN_PROGRESS
    current["value"] = "2026-01-01T01:00:00.001Z"

    expired = gate.resolve(record)
    replayed = gate.resolve(record)

    ledger = _ledger(gate)
    admission = ledger["obligations"][admitted.key.digest]
    evidence_id = admission.get("post_budget_composing_evidence_id")
    assert expired.state == ResolverState.OWED_UNSATISFIED
    assert expired.reason == "post_budget_composing"
    assert expired.evidence_id == evidence_id
    assert replayed.evidence_id == evidence_id
    assert admission["owed_action_missing_seen"] is True
    assert admission["first_dispatch_classified"] is True
    assert admission["last_exhaustion_class"] == "compliance"
    assert sum(
        row["transition"] == "OWED_ACTION_MISSING"
        and row.get("source_id") == evidence_id
        and row["data"].get("evidence") == "post_budget_composing"
        for row in ledger["transitions"]
    ) == 1

    gate.mark_dispatch_result(
        first,
        action_attempted=True,
        action_infra=True,
    )
    assert _ledger(gate)["obligations"][admitted.key.digest][
        "last_exhaustion_class"
    ] == "infrastructure"
    reasserted = gate.resolve(record)
    assert reasserted.reason == "post_budget_composing"
    assert _ledger(gate)["obligations"][admitted.key.digest][
        "last_exhaustion_class"
    ] == "compliance"
    second = gate.reserve_dispatch(expired, purpose="continuation")
    gate.dispatch_record(record, second)
    gate.mark_dispatch_result(second, action_attempted=False)
    with pytest.raises(DispatchRefused, match="paid dispatch budget exhausted"):
        gate.reserve_dispatch(
            gate.resolve(record),
            purpose="recovery",
            nonce="e" * 32,
        )
    assert _ledger(gate)["obligations"][admitted.key.digest][
        "paid_dispatches_total"
    ] == 2


def test_post_budget_composing_outranks_child_infra_in_loop(tmp_path: Path) -> None:
    current = {"value": "2026-01-01T00:00:00Z"}
    store = _store(tmp_path)
    _question(store)
    gate = _gate(
        store,
        now=lambda: current["value"],
        producer_alive=lambda _token: True,
    )
    admitted = gate.admit_or_finalize(_record(store))
    assert admitted.key is not None

    def drive(record: dict) -> loop.DriveOutcome:
        gate.schedule_continuation(admitted.key, producer_token="producer-1")
        assert cli.main([
            "--root",
            str(tmp_path),
            *record["owed_action"]["composing_argv"][3:],
            "--quiet",
        ]) == 0
        current["value"] = "2026-01-01T01:00:00.001Z"
        return loop.DriveOutcome(
            ok=False,
            failure_class=loop.CLASS_INFRA,
            bus_action_infra=True,
        )

    assert loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    ) == 0

    ledger = _ledger(gate)
    admission = ledger["obligations"][admitted.key.digest]
    first = next(iter(admission["reservations"].values()))
    assert first["state"] == "uncaptured_infra"
    assert admission["last_exhaustion_class"] == "compliance"
    assert not any(
        row["transition"] == "OPERATION_RETRY_RECORDED"
        for row in ledger["transitions"]
    )


def test_post_budget_composing_preserves_captured_operation_infra(
    tmp_path: Path,
) -> None:
    current = {"value": "2026-01-01T00:00:00Z"}
    store = _store(tmp_path)
    _question(store, escalation_required=True)
    gate = _gate(
        store,
        now=lambda: current["value"],
        producer_alive=lambda _token: True,
    )
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    first = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, first)
    gate.schedule_continuation(admitted.key, producer_token="producer-1")
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["composing_argv"][3:],
        "--quiet",
    ]) == 0
    current["value"] = "2026-01-01T01:00:00.001Z"
    assert gate.resolve(record).reason == "post_budget_composing"
    first.draft_path.write_text("399", encoding="utf-8")
    captured_digest = _record_captured_intent(store, gate, first)
    gate.mark_dispatch_result(
        first,
        action_attempted=True,
        action_infra=True,
    )

    replayed = gate.resolve(record)
    ledger = _ledger(gate)
    admission = ledger["obligations"][admitted.key.digest]
    row = admission["reservations"][first.nonce]
    captured = gate.captured_operation(admitted.key)

    assert replayed.reason == "post_budget_composing"
    assert admission["last_exhaustion_class"] == "infrastructure"
    assert row["state"] == "action_infra"
    assert row["operation_payload_digest"] == captured_digest
    assert captured is not None
    assert captured.nonce == first.nonce

    first.draft_path.unlink()
    after_loss = gate.resolve(record)
    admission_after_loss = _ledger(gate)["obligations"][admitted.key.digest]
    assert after_loss.reason == "post_budget_composing"
    assert admission_after_loss["last_exhaustion_class"] == "infrastructure"

    corrupt = _ledger(gate)
    corrupt_row = corrupt["obligations"][admitted.key.digest]["reservations"][
        first.nonce
    ]
    corrupt_row["operation_intent"]["recipient"] = "gamma"
    gate.path.write_text(json.dumps(corrupt), encoding="utf-8")

    invalid = gate.resolve(record)
    assert invalid.state == ResolverState.INDETERMINATE
    assert "captured infrastructure evidence" in invalid.reason


@pytest.mark.parametrize("escalation_required", [False, True])
def test_canonical_terminal_blocks_stale_post_budget_reservation_when_hook_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    escalation_required: bool,
) -> None:
    current = {"value": "2026-01-01T00:00:00Z"}
    store = _store(tmp_path)
    _question(store, escalation_required=escalation_required)
    gate = _gate(
        store,
        now=lambda: current["value"],
        producer_alive=lambda _token: True,
    )
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    first = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, first)
    gate.schedule_continuation(admitted.key, producer_token="producer-1")
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["composing_argv"][3:],
        "--quiet",
    ]) == 0
    current["value"] = "2026-01-01T01:00:00.001Z"
    stale = gate.resolve(record)
    gate.mark_dispatch_result(first, action_attempted=False)

    def fail_eager_index(_store, _message) -> None:
        raise OSError("injected projection hook failure")

    monkeypatch.setattr(
        "agenttalk.wrapper.obligations.note_bus_message",
        fail_eager_index,
    )
    first.draft_path.write_text("399", encoding="utf-8")
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["argv"][3:],
        "--quiet",
    ]) == 0

    with pytest.raises((DispatchRefused, StaleRevision)):
        gate.reserve_dispatch(stale, purpose="continuation")

    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["paid_dispatches_total"] == 1
    assert gate.resolve(record).state == ResolverState.SATISFIED


def test_wrong_class_answer_does_not_satisfy_required_escalation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store, escalation_required=True)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    with pytest.raises(GateError, match="durably classified"):
        gate.mark_unsatisfied_attempt(permit, reason="answered instead of escalating")
    gate.dispatch_record(record, permit)
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
    gate.mark_dispatch_result(
        permit,
        action_attempted=True,
        action_rejected=unresolved.state == ResolverState.OWED_UNSATISFIED,
    )
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
    beta.delivery_failed(
        beta_record,
        beta_open.key,
        reason="head-local exhaustion",
        expected_revision=beta_open.scoped_revision,
    )
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


def test_transfer_is_pre_admission_fenced_and_commits_one_complete_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-transfer-atomic")
    policy = _policy("beta", "gamma")
    source = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    store.write_waiting("beta", {
        "mode": "wrapper-loop", "wrapper_generation": "wrapper-1",
        "wait_token": "wrapper-1", "pid": os.getpid(),
    })
    record = _record(store)
    admitted = source.admit_or_finalize(record)
    assert admitted.key is not None
    target = _transfer_question(store, admitted.key, "gamma", policy)
    destination = _gate(
        store,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )

    held = destination.admit_or_finalize(_record(store, agent="gamma"))
    assert held.state == ResolverState.INDETERMINATE
    assert target.id not in _ledger(source)["inbound_index"]

    current = source.resolve(record)
    roster = source.roster_snapshot()
    transferred = source.transfer(
        record,
        admitted.key,
        destination="gamma",
        new_inbound_id=target.id,
        destination_policy=policy,
        actor="lead",
        expected_roster_revision=roster["revision"],
        expected_revision=current.scoped_revision,
    )
    ledger = _ledger(source)
    next_digest = ledger["inbound_index"][target.id]
    next_admission = ledger["obligations"][next_digest]
    transaction = [
        row for row in ledger["transitions"]
        if row["transition"] in {"OBLIGATION_ADMITTED", "TRANSFERRED"}
        and (
            row.get("key_digest") in {admitted.key.digest, next_digest}
            or row.get("source_id") == target.id
        )
    ]

    assert transferred.state == ResolverState.TRANSFERRED
    assert ledger["obligations"][admitted.key.digest]["state"] == "transferred"
    assert next_admission["state"] == "open"
    assert next_admission["fence"] == "unclaimed"
    assert next_admission["activation_generation"] == policy.generation
    assert next_admission["readiness_generation"] == policy.generation
    assert next_admission["paid_dispatches_total"] == 0
    assert next_admission.get("terminal_state") is None
    assert [row["transition"] for row in transaction[-2:]] == [
        "OBLIGATION_ADMITTED", "TRANSFERRED",
    ]
    assert ledger["cursor_dispositions"]["beta"]["inbound_id"] == record["id"]
    assert store.cursor("beta") == record["id"]
    claimed = destination.admit_or_finalize(_record(store, agent="gamma"))
    assert claimed.state == ResolverState.OWED_UNSATISFIED
    assert sum(
        row.get("key", {}).get("inbound_id") == target.id
        for row in ledger["obligations"].values()
        if isinstance(row, dict)
    ) == 1


def test_transfer_crash_or_stale_roster_leaves_source_open_and_target_unadmitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-transfer-crash")
    policy = _policy("beta", "gamma")
    source = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    store.write_waiting("beta", {
        "mode": "wrapper-loop", "wrapper_generation": "wrapper-1",
        "wait_token": "wrapper-1", "pid": os.getpid(),
    })
    record = _record(store)
    admitted = source.admit_or_finalize(record)
    assert admitted.key is not None
    target = _transfer_question(store, admitted.key, "gamma", policy)
    current = source.resolve(record)
    stale_roster = source.roster_snapshot()
    store.set_operator_facing("gamma")

    with pytest.raises(StaleRevision):
        source.transfer(
            record,
            admitted.key,
            destination="gamma",
            new_inbound_id=target.id,
            destination_policy=policy,
            actor="lead",
            expected_roster_revision=stale_roster["revision"],
            expected_revision=current.scoped_revision,
        )
    assert _ledger(source)["obligations"][admitted.key.digest]["state"] == "open"
    assert target.id not in _ledger(source)["inbound_index"]

    current = source.resolve(record)
    roster = source.roster_snapshot()
    real_write = source._write
    monkeypatch.setattr(
        source,
        "_write",
        lambda _ledger_value: (_ for _ in ()).throw(OSError("transfer crash")),
    )
    with pytest.raises(OSError, match="transfer crash"):
        source.transfer(
            record,
            admitted.key,
            destination="gamma",
            new_inbound_id=target.id,
            destination_policy=policy,
            actor="gamma",
            expected_roster_revision=roster["revision"],
            expected_revision=current.scoped_revision,
        )
    monkeypatch.setattr(source, "_write", real_write)
    after = _ledger(source)
    assert after["obligations"][admitted.key.digest]["state"] == "open"
    assert target.id not in after["inbound_index"]


@pytest.mark.parametrize(
    "destination_policy,consult",
    [
        (PolicySnapshot(ResolverState.BLOCKED_POLICY, "unreadable"), False),
        (PolicySnapshot(ResolverState.INACTIVE, "disabled", DETECTION_GRADE), False),
        (_policy("beta", "gamma"), {"unsupported": True}),
    ],
)
def test_transfer_rejects_unadmittable_destination_before_source_close(
    tmp_path: Path,
    destination_policy: PolicySnapshot,
    consult: object,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-transfer-invalid")
    source_policy = _policy("beta", "gamma")
    source = DetectionCommitGate(store, "beta", source_policy, fence="wrapper-1")
    store.write_waiting("beta", {
        "mode": "wrapper-loop", "wrapper_generation": "wrapper-1",
        "wait_token": "wrapper-1", "pid": os.getpid(),
    })
    record = _record(store)
    admitted = source.admit_or_finalize(record)
    assert admitted.key is not None
    target = _transfer_question(
        store,
        admitted.key,
        "gamma",
        destination_policy,
        consult=consult,
    )
    current = source.resolve(record)
    roster = source.roster_snapshot()

    with pytest.raises((GateError, ValueError)):
        source.transfer(
            record,
            admitted.key,
            destination="gamma",
            new_inbound_id=target.id,
            destination_policy=destination_policy,
            actor="lead",
            expected_roster_revision=roster["revision"],
            expected_revision=current.scoped_revision,
        )
    ledger = _ledger(source)
    assert ledger["obligations"][admitted.key.digest]["state"] == "open"
    assert target.id not in ledger["inbound_index"]


def test_human_escalation_pins_roster_revision_and_repoint_rejects_append(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-escalation-roster", escalation_required=True)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    row = _ledger(gate)["obligations"][permit.key_digest]["reservations"][permit.nonce]
    pinned = row["operation_intent"]["expected_roster_revision"]
    assert row["operation_intent"]["origin_obligation_key_digest"] == permit.key_digest
    assert f"expected_roster_revision={pinned}" in dispatched["owed_action"]["argv"]

    permit.draft_path.write_text("operator input required", encoding="utf-8")
    store.set_operator_facing("gamma")
    assert cli.main([
        "--root", str(tmp_path), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]) == 2
    assert not any(
        (message.meta or {}).get("origin_inbound_id") == record["id"]
        for message in store.valid_messages()
    )


def test_operator_resolution_rejects_record_key_mismatch_without_cursor_move(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _question(store, "q-operator-a")
    gate = _gate(store)
    first_record = _record(store)
    admitted = gate.admit_or_finalize(first_record)
    assert admitted.key is not None
    second = _question(store, "q-operator-b")
    second_record = recv_api.to_record(
        second,
        mode="global",
        cursor_before="",
        cursor_after=second.id,
    )
    roster = gate.roster_snapshot()

    with pytest.raises(GateError, match="exact inbound"):
        gate.operator_resolve(
            second_record,
            admitted.key,
            actor="lead",
            expected_roster_revision=roster["revision"],
            reason="wrong record",
        )
    assert _ledger(gate)["obligations"][admitted.key.digest]["state"] == "open"
    assert store.cursor("beta") != second.id
    assert first.id == first_record["id"]


def test_quorum_pins_earliest_canonical_qualifying_events_and_exact_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, ["alpha", "beta", "gamma", "lead"])
    members = ["beta", "gamma", "lead"]
    for member in members:
        _broadcast_question(
            store,
            member,
            broadcast_id="b-canonical-quorum",
            members=members,
            policy="quorum",
            quorum=2,
        )
    beta = _gate(store, policy_agents=tuple(members))
    gamma = _gate(
        store, agent="gamma", fence="wrapper-gamma",
        policy_agents=("gamma", "beta", "lead"),
    )
    lead = _gate(
        store, agent="lead", fence="wrapper-lead",
        policy_agents=("lead", "beta", "gamma"),
    )
    beta_record = _record(store)
    gamma_record = _record(store, agent="gamma")
    lead_record = _record(store, agent="lead")
    beta_resolution, beta_permit = _answer_reserved(tmp_path, beta, beta_record)
    beta.finalize(beta_record, beta.resolve(beta_record))
    gamma_open = gamma.admit_or_finalize(gamma_record)
    gamma_permit = gamma.reserve_dispatch(gamma_open, purpose="initial")
    gamma_dispatch = gamma.dispatch_record(gamma_record, gamma_permit)
    gamma_permit.draft_path.write_text("gamma answer", encoding="utf-8")
    assert cli.main([
        "--root", str(tmp_path), *gamma_dispatch["owed_action"]["argv"][3:], "--quiet",
    ]) == 0
    lead_resolution, lead_permit = _answer_reserved(tmp_path, lead, lead_record)
    lead.finalize(lead_record, lead.resolve(lead_record))
    messages = store.valid_messages()
    beta_id = next(
        message.id for message in messages
        if (message.meta or {}).get("operation_nonce") == beta_permit.nonce
    )
    gamma_id = next(
        message.id for message in messages
        if (message.meta or {}).get("operation_nonce") == gamma_permit.nonce
    )
    lead_id = next(
        message.id for message in messages
        if (message.meta or {}).get("operation_nonce") == lead_permit.nonce
    )
    ledger = _ledger(lead)
    aggregates = [
        row for row in ledger["transitions"]
        if row["transition"] == "BROADCAST_POLICY_SATISFIED"
        and row["data"].get("aggregate") is True
        and row["data"].get("broadcast_id") == "b-canonical-quorum"
    ]

    assert beta_resolution.key is not None and lead_resolution.key is not None
    assert len(aggregates) == 1
    assert aggregates[0]["data"]["winning_ids"] == [beta_id, gamma_id]
    assert lead_id not in aggregates[0]["data"]["winning_ids"]
    assert aggregates[0]["data"]["broadcast_policy_version"] == 1
    assert aggregates[0]["data"]["winning_classes"] == ["answer", "answer"]
    assert isinstance(aggregates[0]["data"]["transaction_id"], str)
    assert gamma.resolve(gamma_record).state == ResolverState.SATISFIED


def test_broadcast_policy_conflict_and_manual_close_do_not_qualify(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    _broadcast_question(
        store, "beta", broadcast_id="b-conflict", members=members, policy="any",
    )
    _broadcast_question(
        store, "gamma", broadcast_id="b-conflict", members=members, policy="each",
    )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    gamma = _gate(
        store, agent="gamma", fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    beta_record = _record(store)
    gamma_record = _record(store, agent="gamma")
    assert beta.admit_or_finalize(beta_record).state == ResolverState.BLOCKED
    assert gamma.admit_or_finalize(gamma_record).state == ResolverState.BLOCKED

    manual_store = _store(tmp_path.parent / "manual-close")
    for member in members:
        _broadcast_question(
            manual_store,
            member,
            broadcast_id="b-manual",
            members=members,
            policy="any",
        )
    manual_beta = _gate(manual_store, policy_agents=("beta", "gamma"))
    manual_gamma = _gate(
        manual_store, agent="gamma", fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    br = _record(manual_store)
    gr = _record(manual_store, agent="gamma")
    manual_beta.admit_or_finalize(br)
    gamma_open = manual_gamma.admit_or_finalize(gr)
    note_manual_close(manual_store, "beta", "b-manual")
    manual_beta.finalize(br, manual_beta.resolve(br))
    assert manual_gamma.resolve(gr).state == ResolverState.OWED_UNSATISFIED
    assert gamma_open.key is not None


def test_any_threshold_closes_requester_and_late_member_without_model_call(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    _broadcast_question(
        store, "beta", broadcast_id="b-late-copy", members=members, policy="any",
    )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    beta_record = _record(store)
    _answer_reserved(tmp_path, beta, beta_record)
    beta.finalize(beta_record, beta.resolve(beta_record))

    requester = recv_api.poll(store, "alpha", scoped_request_id="b-late-copy")
    assert requester["scoped"]["closed"] is True
    assert requester["scoped"]["delivery_terminal"]["state"] == (
        ResolverState.BROADCAST_POLICY_SATISFIED.value
    )

    gamma_inbound = _broadcast_question(
        store, "gamma", broadcast_id="b-late-copy", members=members, policy="any",
    )
    gamma = _gate(
        store, agent="gamma", fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    gamma_record = _record(store, agent="gamma")
    policy_closed = gamma.admit_or_finalize(gamma_record)
    assert policy_closed.state == ResolverState.BROADCAST_POLICY_SATISFIED
    gamma.finalize(gamma_record, policy_closed)
    assert store.cursor("gamma") == gamma_inbound.id
    assert gamma.next_dispatch_purpose(policy_closed.key) is None if policy_closed.key else True


def test_broadcast_policy_close_and_dispatch_reservation_have_one_cas_winner(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    for member in members:
        _broadcast_question(
            store, member, broadcast_id="b-reserve-first", members=members, policy="any",
        )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    gamma = _gate(
        store, agent="gamma", fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    beta_record = _record(store)
    gamma_record = _record(store, agent="gamma")
    gamma_open = gamma.admit_or_finalize(gamma_record)
    gamma_permit = gamma.reserve_dispatch(gamma_open, purpose="initial")
    _answer_reserved(tmp_path, beta, beta_record)
    beta.finalize(beta_record, beta.resolve(beta_record))

    ledger = _ledger(gamma)
    gamma_row = ledger["obligations"][gamma_permit.key_digest]
    assert gamma_row["state"] == "open"
    assert gamma_row["reservations"][gamma_permit.nonce]["state"] == "reserved"
    dispatched = gamma.dispatch_record(gamma_record, gamma_permit)
    gamma_permit.draft_path.write_text("late reserved answer", encoding="utf-8")
    assert cli.main([
        "--root", str(tmp_path), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]) == 0
    assert gamma.resolve(gamma_record).state == ResolverState.SATISFIED
    aggregates = [
        row for row in _ledger(gamma)["transitions"]
        if row["transition"] == "BROADCAST_POLICY_SATISFIED"
        and row["data"].get("aggregate") is True
    ]
    assert len(aggregates) == 1
    assert len(aggregates[0]["data"]["winning_ids"]) == 1


def test_late_response_requires_exact_closed_member_actor_and_anchor(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    beta_inbound = _broadcast_question(
        store, "beta", broadcast_id="b-late", members=members, policy="any",
    )
    gamma_inbound = _broadcast_question(
        store, "gamma", broadcast_id="b-late", members=members, policy="any",
    )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    _answer_reserved(tmp_path, beta, _record(store))
    beta.finalize(_record(store), beta.resolve(_record(store)))
    store.send(
        sender="lead", recipient="alpha", body="wrong actor",
        meta={"broadcast_id": "b-late", "in_reply_to": gamma_inbound.id},
    )
    store.send(
        sender="gamma", recipient="beta", body="wrong recipient",
        meta={"broadcast_id": "b-late", "in_reply_to": gamma_inbound.id},
    )
    store.send(
        sender="gamma", recipient="alpha", kind="composing", body="control",
        meta={"broadcast_id": "b-late", "in_reply_to": gamma_inbound.id},
    )
    valid = store.send(
        sender="gamma", recipient="alpha", body="late answer",
        meta={"broadcast_id": "b-late", "in_reply_to": gamma_inbound.id},
    )
    beta.resolve(_record(store))
    late = [
        row for row in _ledger(beta)["transitions"]
        if row["transition"] == "LATE_RESPONSE"
        and row["data"].get("broadcast_id") == "b-late"
    ]
    assert beta_inbound.id != gamma_inbound.id
    assert [row["source_id"] for row in late] == [valid.id]


def test_each_requester_wait_uses_pinned_exact_qualifying_events(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    members = ["beta", "gamma"]
    for member in members:
        _broadcast_question(
            store, member, broadcast_id="b-each-pinned", members=members, policy="each",
        )
    beta = _gate(store, policy_agents=("beta", "gamma"))
    gamma = _gate(
        store, agent="gamma", fence="wrapper-gamma",
        policy_agents=("gamma", "beta"),
    )
    beta_record = _record(store)
    gamma_record = _record(store, agent="gamma")
    beta.admit_or_finalize(beta_record)
    gamma.admit_or_finalize(gamma_record)
    store.send(
        sender="beta", recipient="alpha", body="unreserved chatter",
        meta={"broadcast_id": "b-each-pinned"},
    )
    store.send(
        sender="gamma", recipient="alpha", body="unreserved chatter",
        meta={"broadcast_id": "b-each-pinned"},
    )
    assert recv_api.poll(
        store, "alpha", scoped_request_id="b-each-pinned",
    )["scoped"]["closed"] is False

    _answer_reserved(tmp_path, beta, beta_record)
    beta.finalize(beta_record, beta.resolve(beta_record))
    assert recv_api.poll(
        store, "alpha", scoped_request_id="b-each-pinned",
    )["scoped"]["closed"] is False
    _answer_reserved(tmp_path, gamma, gamma_record)
    gamma.finalize(gamma_record, gamma.resolve(gamma_record))
    terminal = recv_api.poll(
        store, "alpha", scoped_request_id="b-each-pinned",
    )["scoped"]
    assert terminal["closed"] is True
    assert terminal["delivery_terminal"]["state"] == (
        ResolverState.BROADCAST_POLICY_SATISFIED.value
    )


@pytest.mark.parametrize(
    "meta",
    [
        {"broadcast_id": "legacy-b"},
        {
            "broadcast_id": "legacy-b",
            "membership_snapshot": ["beta"],
            "response_policy": "any",
        },
        {
            "broadcast_id": "legacy-b",
            "membership_snapshot": ["beta"],
            "response_policy": "any",
            "broadcast_policy_version": 99,
        },
    ],
)
def test_legacy_broadcast_is_classification_unknown_with_public_telemetry(
    tmp_path: Path,
    meta: dict,
) -> None:
    store = _store(tmp_path)
    store.send(
        sender="alpha",
        recipient="beta",
        kind="question",
        body="legacy broadcast",
        meta=meta,
    )
    gate = _gate(store)

    resolution = gate.admit_or_finalize(_record(store))
    replayed = gate.admit_or_finalize(_record(store))
    status = gate.status()

    assert resolution.state == ResolverState.CLASSIFICATION_UNKNOWN
    assert replayed.state == ResolverState.CLASSIFICATION_UNKNOWN
    assert _ledger(gate)["telemetry"]["legacy_broadcast_unenforced_total"] == 1
    assert status["legacy_broadcast"] == {
        "enforcement": "none",
        "unenforced_total": 1,
    }


def test_blocked_policy_never_dispatches_or_advances_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-blocked-policy-runtime")
    gate = DetectionCommitGate(
        store,
        "beta",
        PolicySnapshot(ResolverState.BLOCKED_POLICY, "unreadable", reason="corrupt"),
        fence="wrapper-1",
    )
    before = store.cursor("beta")

    blocked = gate.admit_or_finalize(_record(store))

    assert blocked.state == ResolverState.BLOCKED_POLICY
    assert blocked.key is None
    assert store.cursor("beta") == before
    assert inbound.id not in _ledger(gate).get("inbound_index", {}) if gate.path.exists() else True


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
    for _index in range(limit):
        assert gate.record_retry_barrier(
            admitted.key,
            category=category,
            expected_revision=admitted.scoped_revision,
        ) is True
        gate.complete_retry_barrier(admitted.key, category=category)
    assert gate.record_retry_barrier(
        admitted.key,
        category=category,
        expected_revision=admitted.scoped_revision,
    ) is False
    restarted = _gate(store, fence="wrapper-1")

    assert restarted.record_retry_barrier(
        admitted.key,
        category=category,
        expected_revision=admitted.scoped_revision,
    ) is False
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
    finalized = gate.finalize(record, terminal)
    assert finalized.state == ResolverState.INDETERMINATE
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


def test_cursor_projection_reservation_reconciles_crash_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-projection-crash-barrier")
    owner = _gate(store, fence="wrapper-1")
    record = _record(store)
    _answer_reserved(tmp_path, owner, record)
    terminal = owner.resolve(record)
    monkeypatch.setattr(
        owner,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(SystemExit("hard crash")),
    )

    with pytest.raises(SystemExit, match="hard crash"):
        owner.finalize(record, terminal)

    admission = _ledger(owner)["obligations"][terminal.key.digest]
    assert admission["cursor_projection_inflight"] is True
    assert admission["cursor_projection_misses"] == 0

    restarted = _gate(store, fence="wrapper-1")
    monkeypatch.setattr(
        restarted,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(OSError("retry failed")),
    )
    retried = restarted.finalize(record, restarted.resolve(record))

    assert retried.state == ResolverState.INDETERMINATE
    admission = _ledger(restarted)["obligations"][terminal.key.digest]
    assert admission["cursor_projection_inflight"] is False
    assert admission["cursor_projection_misses"] == 2
    assert sum(
        row["transition"] == "CURSOR_FINALIZED"
        for row in _ledger(restarted)["transitions"]
    ) == 1


def test_cursor_projection_elapsed_bound_starts_at_crashed_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-projection-elapsed-crash")
    owner = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:00:00Z",
    )
    record = _record(store)
    _answer_reserved(tmp_path, owner, record)
    terminal = owner.resolve(record)
    monkeypatch.setattr(
        owner,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(SystemExit("hard crash")),
    )
    with pytest.raises(SystemExit, match="hard crash"):
        owner.finalize(record, terminal)
    admission = _ledger(owner)["obligations"][terminal.key.digest]
    assert admission["cursor_projection_reserved_at"] == "2026-01-01T00:00:00Z"

    restarted = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:15:01Z",
    )
    attempted = False

    def project(_record: dict) -> None:
        nonlocal attempted
        attempted = True

    monkeypatch.setattr(restarted, "_advance_record_cursor", project)
    result = restarted.finalize(record, restarted.resolve(record))

    assert result.state == ResolverState.INDETERMINATE
    assert result.reason == "cursor projection retry bound exhausted"
    assert attempted is False
    admission = _ledger(restarted)["obligations"][terminal.key.digest]
    assert admission["cursor_projection_misses"] == 1
    assert admission["cursor_projection_first_at"] == "2026-01-01T00:00:00Z"
    assert admission["cursor_projection_blocked"] is True
    assert restarted.resolve(record).state == ResolverState.BLOCKED


def test_persistent_cursor_projection_failure_blocks_after_durable_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-projection-bound")
    owner = _gate(store, fence="wrapper-1")
    record = _record(store)
    _answer_reserved(tmp_path, owner, record)
    key = owner.resolve(record).key
    assert key is not None

    for _attempt in range(12):
        restarted = _gate(store, fence="wrapper-1")
        monkeypatch.setattr(
            restarted,
            "_advance_record_cursor",
            lambda _record: (_ for _ in ()).throw(OSError("persistent failure")),
        )
        result = restarted.finalize(record, restarted.resolve(record))
        assert result.state == ResolverState.INDETERMINATE

    ledger = _ledger(restarted)
    admission = ledger["obligations"][key.digest]
    assert admission["cursor_projection_misses"] == 12
    assert admission["cursor_projection_blocked"] is True
    assert admission["cursor_projection_inflight"] is False
    assert store.cursor("beta") == ""
    assert restarted.resolve(record).state == ResolverState.BLOCKED
    assert restarted.status()["status"] == "BLOCKED"
    assert sum(
        row["transition"] == "CURSOR_FINALIZED"
        for row in ledger["transitions"]
    ) == 1
    assert sum(
        row["transition"] == "CURSOR_PROJECTION_BLOCKED"
        for row in ledger["transitions"]
    ) == 1


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
        gate.delivery_failed(
            record,
            admitted.key,
            reason="cannot persist",
            expected_revision=admitted.scoped_revision,
        )
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


def test_at_cap_failed_delivery_transaction_is_complete_before_cursor_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-atomic-failure")
    gate = _gate(store)
    calls = 0

    def drive(_record: dict) -> loop.DriveOutcome:
        nonlocal calls
        calls += 1
        return loop.DriveOutcome(ok=True)

    monkeypatch.setattr(
        gate,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(OSError("cursor projection crash")),
    )

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=4,
    )

    ledger = _ledger(gate)
    key_digest = ledger["inbound_index"][inbound.id]
    admission = ledger["obligations"][key_digest]
    incident = [
        row for row in ledger["transitions"]
        if row["transition"] == "COMPLIANCE_INCIDENT"
        and row.get("key_digest") == key_digest
    ]
    failed = [
        row for row in ledger["transitions"]
        if row["transition"] == "DELIVERY_FAILED"
        and row.get("key_digest") == key_digest
    ]

    assert calls == 2
    assert len(incident) == len(failed) == 1
    assert admission["state"] == "delivery_failed"
    assert admission["paid_dispatches_total"] == 2
    assert admission["delivery_failure_sequence"] == failed[0]["sequence"]
    assert ledger["breakers"]["beta"][
        "owed_action_cap_exhaustions_consecutive"
    ] == 1
    indexed = ledger["delivery_index"]["q-atomic-failure"]
    assert len(indexed) == 1
    assert indexed[0]["key_digest"] == key_digest
    assert indexed[0]["incident_sequence"] == incident[0]["sequence"]
    assert ledger["cursor_dispositions"]["beta"] == {
        "inbound_id": inbound.id,
        "mode": "global",
        "state": "delivery_failed",
        "at": ledger["cursor_dispositions"]["beta"]["at"],
    }

    stack: list[object] = [ledger]
    dead_letter_references: list[dict] = []
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get("kind") == "dead_letter_reference":
                dead_letter_references.append(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    assert len(dead_letter_references) == 1
    reference = json.dumps(dead_letter_references[0], sort_keys=True)
    assert inbound.id in reference
    assert key_digest in reference
    assert str(incident[0]["sequence"]) in reference
    assert str(failed[0]["sequence"]) in reference
    for nonce in admission["reservations"]:
        assert nonce in reference

    assert store.cursor("beta") == ""
    assert any(message.id == inbound.id for message in store.valid_messages())


def test_blocked_pairwise_waiter_wakes_from_transactional_delivery_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    _question(store, "q-blocked-waiter")
    gate = _gate(store)
    marker_written = threading.Event()
    original_write_marker = cli._write_waiting_marker  # noqa: SLF001

    def write_marker_then_signal(*args, **kwargs):
        result = original_write_marker(*args, **kwargs)
        marker_written.set()
        return result

    monkeypatch.setattr(cli, "_write_waiting_marker", write_marker_then_signal)
    wait_results: list[int] = []

    def wait_for_failure() -> None:
        wait_results.append(cli.main([
            "--root",
            str(tmp_path),
            "wait",
            "--for",
            "alpha",
            "--to-request",
            "q-blocked-waiter",
            "--timeout",
            "2",
            "--interval",
            "0.1",
        ]))

    waiter = threading.Thread(target=wait_for_failure)
    waiter.start()
    assert marker_written.wait(timeout=5)
    assert wait_results == []

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
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert calls == 2
    assert wait_results == [4]
    output = capsys.readouterr().out
    assert "AGENTTALK :: DELIVERY FAILED" in output
    payload = json.loads(next(
        line for line in reversed(output.splitlines()) if line.startswith("{")
    ))
    assert payload["state"] == "delivery_failed"
    assert payload["delivery_generation"] == 1
    assert isinstance(payload["incident_sequence"], int)


def test_failed_broadcast_member_does_not_terminalize_pinned_quorum_recv(
    tmp_path: Path,
) -> None:
    members = ["beta", "gamma", "lead"]
    store = _store(tmp_path, ["alpha", *members])
    for member in members:
        _broadcast_question(
            store,
            member,
            broadcast_id="b-pinned-quorum",
            members=members,
            policy="quorum",
            quorum=2,
        )
    beta = _gate(store, policy_agents=tuple(members))
    gamma = _gate(
        store,
        agent="gamma",
        fence="wrapper-gamma",
        policy_agents=("gamma", "beta", "lead"),
    )
    beta_record = _record(store)
    gamma_record = _record(store, agent="gamma")
    beta_open = beta.admit_or_finalize(beta_record)
    assert beta_open.key is not None
    beta.delivery_failed(
        beta_record,
        beta_open.key,
        reason="one member exhausted",
        expected_revision=beta_open.scoped_revision,
    )

    before_answer = recv_api.poll(
        store,
        "alpha",
        scoped_request_id="b-pinned-quorum",
    )
    assert before_answer["scoped"]["closed"] is False
    assert before_answer["scoped"]["delivery_failed"] is None

    _answer_reserved(tmp_path, gamma, gamma_record, body="one qualifying answer")
    gamma.finalize(gamma_record, gamma.resolve(gamma_record))
    after_one_answer = recv_api.poll(
        store,
        "alpha",
        scoped_request_id="b-pinned-quorum",
    )

    assert after_one_answer["scoped"]["closed"] is False
    assert after_one_answer["scoped"]["delivery_failed"] is None


def test_failed_delivery_replay_exhausts_assignment_but_leaves_conversation_unanswered(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-unanswered")
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    gate.delivery_failed(
        record,
        admitted.key,
        reason="delivery exhausted",
        expected_revision=admitted.scoped_revision,
    )

    replayed = gate.resolve(record)
    requester_threads = threads.derive_threads(
        store.valid_messages(),
        agent="alpha",
        cursor="",
    )
    requester_thread = next(
        row for row in requester_threads if row.request_id == "q-unanswered"
    )

    assert replayed.state == ResolverState.DELIVERY_EXHAUSTED
    assert replayed.key == admitted.key
    assert gate.delivery_status("q-unanswered", "alpha")["state"] == "delivery_failed"
    assert requester_thread.state == "open-outbound"
    assert any(message.id == inbound.id for message in store.valid_messages())


def test_explicit_reask_after_failed_delivery_gets_fresh_budget_without_reopening_old(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    old_inbound = _question(store, "q-new-delivery")
    gate = _gate(store)
    old_record = _record(store)
    old = gate.admit_or_finalize(old_record)
    assert old.key is not None
    old_permit = gate.reserve_dispatch(old, purpose="initial")
    gate.dispatch_record(old_record, old_permit)
    gate.mark_dispatch_result(old_permit, action_attempted=False)
    old_current = gate.resolve(old_record)
    gate.delivery_failed(
        old_record,
        old.key,
        reason="old delivery exhausted",
        expected_revision=old_current.scoped_revision,
    )
    old_before_reask = dict(_ledger(gate)["obligations"][old.key.digest])

    new_inbound = _question(store, "q-new-delivery")
    restarted = _gate(store, fence="wrapper-2")
    new_record = _record(store)
    assert new_record["id"] == new_inbound.id
    new = restarted.admit_or_finalize(new_record)
    assert new.key is not None

    assert new.key.digest != old.key.digest
    assert (
        new.key.question_generation,
        new.key.delivery_generation,
    ) != (
        old.key.question_generation,
        old.key.delivery_generation,
    )
    ledger = _ledger(restarted)
    assert ledger["obligations"][old.key.digest] == old_before_reask
    assert ledger["obligations"][old.key.digest]["state"] == "delivery_failed"
    assert ledger["obligations"][new.key.digest]["state"] == "open"
    assert ledger["obligations"][new.key.digest]["paid_dispatches_total"] == 0

    new_permit = restarted.reserve_dispatch(new, purpose="initial")
    after_reservation = _ledger(restarted)["obligations"]
    assert new_permit.paid_dispatches_total == 1
    assert after_reservation[new.key.digest]["paid_dispatches_total"] == 1
    assert after_reservation[old.key.digest]["paid_dispatches_total"] == (
        old_before_reask["paid_dispatches_total"]
    )
    assert after_reservation[old.key.digest]["state"] == "delivery_failed"
    assert any(message.id == old_inbound.id for message in store.valid_messages())
    assert restarted.delivery_status("q-new-delivery", "alpha") is None


def _captured_infra_attempt(
    store: Store,
    gate: DetectionCommitGate,
    *,
    rid: str | None = None,
):
    record = _record(store, rid)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    permit.draft_path.write_text("captured answer", encoding="utf-8")
    _record_captured_intent(store, gate, permit)
    gate.mark_dispatch_result(
        permit,
        action_attempted=True,
        action_infra=True,
    )
    return record, admitted, permit, dispatched


def _race_scoped_revision(store: Store, sequence: int, *, rid: str = "q-1") -> None:
    store.send(
        sender="gamma",
        recipient="alpha",
        kind="note",
        body=f"correlated revision race {sequence}",
        meta={"request_id": rid},
    )


def _consume_operation_retry_budget(
    gate: DetectionCommitGate,
    admitted,
    *,
    already_executed: int = 1,
) -> int:
    executions = already_executed
    current = gate.resolve(_record(gate.store))
    while executions < 16:
        assert gate.record_retry_barrier(
            admitted.key,
            category="operation_infra",
            expected_revision=current.scoped_revision,
        ) is True
        executions += 1
        gate.complete_retry_barrier(admitted.key, category="operation_infra")
    return executions


def test_finalization_cas_miss_is_durable_when_finalize_returns_and_after_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store, now=lambda: "2026-01-01T00:00:00Z")
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)
    terminal = gate.resolve(record)
    _race_scoped_revision(store, 1)

    missed = gate.finalize(record, terminal)
    admission = _ledger(gate)["obligations"][terminal.key.digest]

    assert missed.state == ResolverState.INDETERMINATE
    assert admission["finalization_misses"] == 1
    assert admission["finalization_first_at"] == "2026-01-01T00:00:00Z"
    assert store.cursor("beta") != inbound.id

    restarted = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:00:01Z",
    )
    persisted = _ledger(restarted)["obligations"][terminal.key.digest]
    assert persisted["finalization_misses"] == 1
    assert persisted["finalization_first_at"] == "2026-01-01T00:00:00Z"


def test_three_finalization_misses_back_off_indeterminate_without_model_spend(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)
    results = []

    for sequence in range(1, 4):
        terminal = gate.resolve(record)
        _race_scoped_revision(store, sequence)
        results.append(gate.finalize(record, terminal).state)
        gate = _gate(store, fence="wrapper-1")

    admission = _ledger(gate)["obligations"][terminal.key.digest]
    assert results == [ResolverState.INDETERMINATE] * 3
    assert admission["finalization_misses"] == 3
    assert admission["paid_dispatches_total"] == 1
    assert admission["state"] == "open"
    assert store.cursor("beta") == ""


def test_twelfth_finalization_miss_replays_latest_terminal_and_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)

    for sequence in range(1, 12):
        terminal = gate.resolve(record)
        _race_scoped_revision(store, sequence)
        assert gate.finalize(record, terminal).state == ResolverState.INDETERMINATE

    original_finalize = gate.finalize
    raced = False

    def race_twelfth_finalize(current_record, resolution, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            _race_scoped_revision(store, 12)
        return original_finalize(current_record, resolution, **kwargs)

    monkeypatch.setattr(gate, "finalize", race_twelfth_finalize)
    model_calls = 0

    def drive(_record: dict) -> bool:
        nonlocal model_calls
        model_calls += 1
        return True

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=2,
    )

    admission = _ledger(gate)["obligations"][terminal.key.digest]
    assert raced is True
    assert admission["finalization_misses"] == 12
    assert admission["state"] == "finalized"
    assert admission["terminal_state"] == ResolverState.SATISFIED.value
    assert store.cursor("beta") == inbound.id
    assert model_calls == 0


def test_no_admission_finalization_bound_is_visible_across_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    record = _record(store)
    policy = PolicySnapshot.from_mapping({"schema_version": 1, "agents": {}}, "beta")
    outcomes = []

    for sequence in range(1, 13):
        gate = DetectionCommitGate(
            store,
            "beta",
            policy,
            fence="wrapper-1",
            now=lambda: "2026-01-01T00:00:00Z",
        )
        resolution = gate.admit_or_finalize(record)
        _race_scoped_revision(store, sequence)
        outcomes.append(gate.finalize(
            record,
            resolution,
            expected_revision=resolution.ledger_revision,
        ).state)

    assert outcomes[:11] == [ResolverState.INDETERMINATE] * 11
    assert outcomes[11] == ResolverState.BLOCKED
    claim = _ledger(gate)["no_admission_claims"][record["id"]]
    assert claim["finalization_misses"] == 12
    assert claim["finalization_first_at"] == "2026-01-01T00:00:00Z"

    restarted = DetectionCommitGate(
        store,
        "beta",
        policy,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:00:01Z",
    )
    assert restarted.admit_or_finalize(record).state == ResolverState.BLOCKED
    assert store.cursor("beta") == ""


def test_operation_infra_clock_starts_at_initial_captured_failure_and_survives_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store, now=lambda: "2026-01-01T00:00:00Z")
    _record_, admitted, _permit, _dispatched = _captured_infra_attempt(store, gate)
    admission = _ledger(gate)["obligations"][admitted.key.digest]

    assert admission["operation_infra_attempts"] == 1
    assert admission["operation_infra_first_at"] == "2026-01-01T00:00:00Z"

    restarted = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:14:59Z",
    )
    persisted = _ledger(restarted)["obligations"][admitted.key.digest]
    assert persisted["operation_infra_attempts"] == 1
    assert persisted["operation_infra_first_at"] == "2026-01-01T00:00:00Z"


def test_operation_retry_barrier_allows_exactly_sixteen_executions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    _record_, admitted, _permit, _dispatched = _captured_infra_attempt(store, gate)

    executions = _consume_operation_retry_budget(gate, admitted)
    current = gate.resolve(_record(store))

    assert executions == 16
    assert gate.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=current.scoped_revision,
    ) is False
    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["operation_infra_attempts"] == 16


def test_stale_terminal_refuses_operation_retry_before_execution(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record, admitted, _permit, dispatched = _captured_infra_attempt(store, gate)
    stale = gate.resolve(record)
    before = _ledger(gate)["obligations"][admitted.key.digest][
        "operation_infra_attempts"
    ]
    assert cli.main([
        "--root", str(tmp_path), *dispatched["owed_action"]["argv"][3:], "--quiet",
    ]) == 0

    allowed = gate.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=stale.scoped_revision,
    )

    assert allowed is False
    assert gate.resolve(record).state == ResolverState.SATISFIED
    assert _ledger(gate)["obligations"][admitted.key.digest][
        "operation_infra_attempts"
    ] == before


def test_operation_cap_fresh_replay_lets_concurrent_terminal_win(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record, admitted, _permit, dispatched = _captured_infra_attempt(store, gate)
    assert _consume_operation_retry_budget(gate, admitted) == 16
    original_captured = gate.captured_operation
    published = False

    def publish_before_cap_check(key):
        nonlocal published
        captured = original_captured(key)
        if not published:
            published = True
            assert cli.main([
                "--root",
                str(tmp_path),
                *dispatched["owed_action"]["argv"][3:],
                "--quiet",
            ]) == 0
        return captured

    monkeypatch.setattr(gate, "captured_operation", publish_before_cap_check)
    model_calls = 0

    def drive(_record: dict) -> bool:
        nonlocal model_calls
        model_calls += 1
        return True

    loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=2,
    )

    assert published is True
    assert model_calls == 0
    assert store.cursor("beta") == inbound.id
    assert gate.resolve(record).state == ResolverState.SATISFIED
    assert gate.delivery_status("q-1", "alpha") is None


def test_unlocalizable_operation_exhaustion_is_durably_blocked_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record, admitted, permit, _dispatched = _captured_infra_attempt(store, gate)
    assert _consume_operation_retry_budget(gate, admitted) == 16
    original_captured = gate.captured_operation

    def fail_replay_after_initial_resolution(key):
        captured = original_captured(key)

        def unreadable_replay():
            raise LedgerUnreadable("global projection unavailable at exhaustion")

        monkeypatch.setattr(gate, "_validated_messages", unreadable_replay)
        return captured

    monkeypatch.setattr(gate, "captured_operation", fail_replay_after_initial_resolution)
    model_calls = 0

    def drive(_record: dict) -> bool:
        nonlocal model_calls
        model_calls += 1
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

    assert model_calls == 0
    assert store.cursor("beta") != inbound.id
    assert gate.delivery_status("q-1", "alpha") is None

    restarted = _gate(store, fence="wrapper-1")
    assert restarted.resolve(record).state == ResolverState.BLOCKED
    admission = _ledger(restarted)["obligations"][admitted.key.digest]
    assert admission["state"] == "blocked"


def test_operation_infra_elapsed_ceiling_survives_restart_without_another_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store, now=lambda: "2026-01-01T00:00:00Z")
    record, admitted, permit, _dispatched = _captured_infra_attempt(store, gate)

    restarted = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:15:01Z",
    )
    current = restarted.resolve(record)
    assert restarted.record_retry_barrier(
        admitted.key,
        category="operation_infra",
        expected_revision=current.scoped_revision,
    ) is False
    admission = _ledger(restarted)["obligations"][admitted.key.digest]

    assert admission["operation_infra_attempts"] == 1
    assert admission["operation_infra_first_at"] == "2026-01-01T00:00:00Z"
    settled = restarted.settle_retry_exhaustion(
        record,
        admitted.key,
        category="operation_infra",
        reason="operation elapsed ceiling exhausted",
        permit=permit,
    )
    assert settled.state == ResolverState.BLOCKED
    assert restarted.delivery_status("q-1", "alpha") is None


@pytest.mark.parametrize("only_request_id", [None, "q-loop-infra-bound"])
def test_loop_operation_infra_bound_requires_proven_head_local_failure(
    tmp_path: Path,
    only_request_id: str | None,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-loop-infra-bound")
    gate = _gate(store)
    record, admitted, _permit, _dispatched = _captured_infra_attempt(
        store,
        gate,
        rid=only_request_id,
    )
    ledger = _ledger(gate)
    admission = ledger["obligations"][admitted.key.digest]
    admission["operation_infra_attempts"] = 16
    gate._write(ledger)  # noqa: SLF001 - inject the durable cross-restart cap

    turns = loop.run_loop(
        store,
        "beta",
        lambda _record: pytest.fail("model was called after operation cap"),
        commit_gate=gate,
        only_request_id=only_request_id,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    assert turns == 0
    assert store.cursor("beta") == ""
    assert any(message.id == inbound.id for message in store.valid_messages())
    assert gate.delivery_status("q-loop-infra-bound", "alpha") is None
    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["state"] == "blocked"
    assert "not proven" in admission["blocked_reason"]
    assert gate.resolve(record).state == ResolverState.BLOCKED


def test_finalization_elapsed_ceiling_survives_restart_and_replays_terminal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store, now=lambda: "2026-01-01T00:00:00Z")
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)
    first = gate.resolve(record)
    _race_scoped_revision(store, 1)
    assert gate.finalize(record, first).state == ResolverState.INDETERMINATE

    restarted = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:15:01Z",
    )
    latest = restarted.resolve(record)
    _race_scoped_revision(store, 2)
    exhausted = restarted.finalize(record, latest)

    assert exhausted.state == ResolverState.INDETERMINATE
    assert exhausted.reason == "finalization CAS contention exhausted"
    admission = _ledger(restarted)["obligations"][latest.key.digest]
    assert admission["finalization_misses"] == 2
    assert admission["finalization_first_at"] == "2026-01-01T00:00:00Z"
    settled = restarted.settle_retry_exhaustion(
        record,
        latest.key,
        category="finalization",
        reason="finalization elapsed ceiling exhausted",
    )
    assert settled.state == ResolverState.SATISFIED
    assert store.cursor("beta") == inbound.id


def test_unwritable_failed_delivery_becomes_visible_blocked_without_cursor_advance(
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
        lambda _ledger: (_ for _ in ()).throw(OSError("disposition unavailable")),
    )

    blocked = gate.fail_delivery_or_block(
        record,
        admitted.key,
        reason="cannot persist failed delivery",
        expected_revision=admitted.scoped_revision,
    )

    assert blocked.state == ResolverState.BLOCKED
    assert store.cursor("beta") == ""
    health = json.loads(gate.proof_health_path.read_text(encoding="utf-8"))
    assert health["state"] == "blocked"
    assert health["disposition_block"] is True
    restarted = _gate(store, fence="wrapper-1")
    assert restarted.admit_or_finalize(record).state == ResolverState.BLOCKED
    assert restarted.status()["status"] == "BLOCKED"


def test_head_local_failed_delivery_does_not_dispose_an_unrelated_head(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _question(store, "q-local")
    second = _question(store, "q-unrelated")
    gate = _gate(store)
    record, admitted, permit, _dispatched = _captured_infra_attempt(store, gate)
    permit.draft_path.write_text("tampered after durable capture", encoding="utf-8")
    current = gate.resolve(record)

    result = gate.fail_head_local_corruption_or_block(
        record,
        admitted.key,
        permit,
        reason="captured operation proof is corrupt",
        expected_revision=current.scoped_revision,
    )

    assert result.state == ResolverState.DELIVERY_EXHAUSTED
    assert gate.delivery_status("q-local", "alpha") is not None
    assert gate.delivery_status("q-unrelated", "alpha") is None
    assert any(message.id == first.id for message in store.valid_messages())
    assert any(message.id == second.id for message in store.valid_messages())
    proof = _ledger(gate)["obligations"][admitted.key.digest][
        "head_local_proof_failure"
    ]
    assert proof["kind"] == "HEAD_LOCAL_PROOF_FAILURE"
    assert proof["inbound_id"] == first.id
    assert proof["operation_nonce"] == permit.nonce
    assert proof["expected_payload_sha256"] != proof["observed_payload_sha256"]
    assert _record(store)["id"] == second.id


def test_unreadable_head_local_artifact_blocks_instead_of_failed_delivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-unproven-local")
    gate = _gate(store)
    record, admitted, permit, _dispatched = _captured_infra_attempt(store, gate)
    permit.draft_path.unlink()
    current = gate.resolve(record)

    result = gate.fail_head_local_corruption_or_block(
        record,
        admitted.key,
        permit,
        reason="unreadable local artifact",
        expected_revision=current.scoped_revision,
    )

    assert result.state == ResolverState.BLOCKED
    assert store.cursor("beta") == ""
    assert gate.delivery_status("q-unproven-local", "alpha") is None
    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["state"] == "blocked"
    assert "unreadable" in admission["blocked_reason"]


@pytest.mark.parametrize("tamper", ["marker", "reservation"])
def test_disagreeing_durable_operation_proofs_cannot_authorize_failed_delivery(
    tmp_path: Path,
    tamper: str,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-untrusted-local-proof")
    gate = _gate(store)
    record, admitted, permit, _dispatched = _captured_infra_attempt(store, gate)
    if tamper == "marker":
        marker_path = store._operation_intent_path(  # noqa: SLF001 - corruption injection
            "beta",
            permit.nonce,
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["operation_digest"] = "0" * 64
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    else:
        ledger = _ledger(gate)
        ledger["obligations"][admitted.key.digest]["reservations"][permit.nonce][
            "operation_payload_digest"
        ] = "0" * 64
        gate._write(ledger)  # noqa: SLF001 - corruption injection
    current = gate.resolve(record)

    result = gate.fail_head_local_corruption_or_block(
        record,
        admitted.key,
        permit,
        reason="untrusted durable proof",
        expected_revision=current.scoped_revision,
    )

    assert result.state == ResolverState.BLOCKED
    assert store.cursor("beta") == ""
    assert gate.delivery_status("q-untrusted-local-proof", "alpha") is None
    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["state"] == "blocked"
    assert "proofs disagree" in admission["blocked_reason"]
    assert not any(
        row["transition"] == "DELIVERY_FAILED"
        for row in _ledger(gate)["transitions"]
    )


def test_terminal_append_and_finalizer_concurrency_has_no_deadlock_or_stale_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)
    terminal = gate.resolve(record)
    start = threading.Barrier(2)
    results: list[ResolverState] = []
    failures: list[BaseException] = []

    def append_evidence() -> None:
        try:
            start.wait(timeout=5)
            store.send(
                sender="gamma",
                recipient="alpha",
                kind="note",
                body="concurrent correlated evidence",
                meta={"request_id": "q-1"},
            )
        except BaseException as exc:  # pragma: no cover - assertion captures thread errors
            failures.append(exc)

    def finalize_terminal() -> None:
        try:
            start.wait(timeout=5)
            results.append(gate.finalize(record, terminal).state)
        except BaseException as exc:  # pragma: no cover - assertion captures thread errors
            failures.append(exc)

    threads_ = [
        threading.Thread(target=append_evidence),
        threading.Thread(target=finalize_terminal),
    ]
    for worker in threads_:
        worker.start()
    for worker in threads_:
        worker.join(timeout=10)

    assert not failures
    assert all(not worker.is_alive() for worker in threads_)
    assert results[0] in {ResolverState.SATISFIED, ResolverState.INDETERMINATE}
    if results[0] == ResolverState.INDETERMINATE:
        latest = gate.resolve(record)
        assert gate.finalize(record, latest).state == ResolverState.SATISFIED
    assert store.cursor("beta") == inbound.id
    assert sum(
        row["transition"] == "CURSOR_FINALIZED"
        for row in _ledger(gate)["transitions"]
    ) == 1


def test_identical_proof_error_fingerprint_emits_one_elapsed_authority(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    gate = _gate(store, now=lambda: "2026-01-01T00:00:00Z")
    first = gate.record_proof_failure(error_class="OSError", path="ledger.json")
    restarted = _gate(
        store,
        fence="wrapper-1",
        now=lambda: "2026-01-01T00:15:01Z",
    )
    exhausted = restarted.record_proof_failure(
        error_class="OSError",
        path="ledger.json",
    )
    again = restarted.record_proof_failure(
        error_class="OSError",
        path="ledger.json",
    )

    assert exhausted["exhausted"] is True
    assert exhausted["fingerprint"] == first["fingerprint"]
    assert exhausted["first_failure_at"] == first["first_failure_at"]
    assert again["alerted_at"] == exhausted["alerted_at"]
    assert again["incident_id"] == exhausted["incident_id"]
    assert again["incident"] == exhausted["incident"]


def test_stale_nonterminal_retry_revision_does_not_exhaust_operation_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record, admitted, permit, _dispatched = _captured_infra_attempt(store, gate)
    original_captured = gate.captured_operation
    raced = False

    def race_before_barrier(key):
        nonlocal raced
        captured = original_captured(key)
        if not raced:
            raced = True
            _race_scoped_revision(store, 1)
        return captured

    monkeypatch.setattr(gate, "captured_operation", race_before_barrier)
    monkeypatch.setattr(
        gate,
        "retry_captured_operation",
        lambda *_args, **_kwargs: pytest.fail("stale retry executed"),
    )

    turns = loop.run_loop(
        store,
        "beta",
        lambda _record: pytest.fail("model was called"),
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert raced is True
    assert turns == 0
    assert admission["operation_infra_attempts"] == 1
    assert admission["state"] == "open"
    assert permit.draft_path.exists()
    assert gate.retry_bound_exhausted(
        admitted.key,
        category="operation_infra",
    ) is False
    assert gate.delivery_status("q-1", "alpha") is None


def test_immediate_operation_retry_does_not_claim_turn_on_finalization_cas_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    permits: list[object] = []
    dispatched_records: list[dict] = []
    original_reserve = gate.reserve_dispatch
    original_finalize = gate.finalize

    def reserve(*args, **kwargs):
        permit = original_reserve(*args, **kwargs)
        permits.append(permit)
        return permit

    def drive(dispatch_record: dict) -> loop.DriveOutcome:
        dispatched_records.append(dispatch_record)
        permit = permits[-1]
        permit.draft_path.write_text("captured answer", encoding="utf-8")
        _record_captured_intent(store, gate, permit)
        return loop.DriveOutcome(
            ok=False,
            failure_class=loop.CLASS_INFRA,
            bus_action_attempted=True,
            bus_action_infra=True,
        )

    def publish_retry(_permit, _record) -> bool:
        argv = dispatched_records[-1]["owed_action"]["argv"]
        assert cli.main(["--root", str(tmp_path), *argv[3:], "--quiet"]) == 0
        return True

    raced = False

    def race_finalize(current_record, resolution, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            _race_scoped_revision(store, 1)
        return original_finalize(current_record, resolution, **kwargs)

    monkeypatch.setattr(gate, "reserve_dispatch", reserve)
    monkeypatch.setattr(gate, "retry_captured_operation", publish_retry)
    monkeypatch.setattr(gate, "finalize", race_finalize)

    turns = loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    admission = _ledger(gate)["obligations"][permits[0].key_digest]
    assert raced is True
    assert turns == 0
    assert admission["finalization_misses"] == 1
    assert admission["state"] == "open"
    assert permits[0].draft_path.exists()


@pytest.mark.parametrize("only_request_id", [None, "q-no-key-retry"])
def test_no_admission_finalization_contention_never_redrives_successful_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    only_request_id: str | None,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-no-key-retry")
    store.write_waiting("beta", {
        "mode": "wrapper-loop",
        "wrapper_generation": "wrapper-1",
        "wait_token": "wrapper-1",
        "pid": os.getpid(),
    })
    policy = PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": DETECTION_GRADE, "enabled": False}},
    }, "beta")
    gate = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    original_finalize = gate.finalize
    finalize_calls = 0
    drive_calls = 0

    def race_each_finalize(current_record, resolution, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        _race_scoped_revision(
            store,
            finalize_calls,
            rid="q-no-key-retry",
        )
        return original_finalize(current_record, resolution, **kwargs)

    def drive(_record: dict) -> bool:
        nonlocal drive_calls
        drive_calls += 1
        return True

    monkeypatch.setattr(gate, "finalize", race_each_finalize)

    turns = loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        only_request_id=only_request_id,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=3,
    )

    claim = _ledger(gate)["no_admission_claims"][inbound.id]
    assert turns == 0
    assert drive_calls == 1
    assert finalize_calls == 3
    assert claim["state"] == "finalization_pending"
    assert claim["finalization_misses"] == 3
    assert claim["drive_succeeded_at"]
    assert store.cursor("beta") == ""


def test_no_admission_disposition_crash_replays_without_redriving_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-no-key-projection")
    store.write_waiting("beta", {
        "mode": "wrapper-loop",
        "wrapper_generation": "wrapper-1",
        "wait_token": "wrapper-1",
        "pid": os.getpid(),
    })
    policy = PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": DETECTION_GRADE, "enabled": False}},
    }, "beta")
    owner = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    record = _record(store)
    resolution = owner.admit_or_finalize(record)
    retained = owner.record_no_admission_success(record, resolution)
    monkeypatch.setattr(
        owner,
        "_advance_record_cursor",
        lambda _record: (_ for _ in ()).throw(OSError("projection crash")),
    )
    finalized = owner.finalize(
        record,
        retained,
        expected_revision=retained.ledger_revision,
    )
    assert finalized.state == ResolverState.INDETERMINATE
    assert store.cursor("beta") == ""

    restarted = DetectionCommitGate(
        store,
        "beta",
        policy,
        fence="wrapper-1",
    )
    turns = loop.run_loop(
        store,
        "beta",
        lambda _record: pytest.fail("model was redriven"),
        commit_gate=restarted,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=1,
    )

    assert turns == 1
    assert store.cursor("beta") == inbound.id


@pytest.mark.parametrize("only_request_id", [None, "q-no-key-append"])
def test_no_admission_success_survives_correlated_append_during_drive(
    tmp_path: Path,
    only_request_id: str | None,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store, "q-no-key-append")
    store.write_waiting("beta", {
        "mode": "wrapper-loop",
        "wrapper_generation": "wrapper-1",
        "wait_token": "wrapper-1",
        "pid": os.getpid(),
    })
    policy = PolicySnapshot.from_mapping({
        "schema_version": 1,
        "agents": {"beta": {"grade": DETECTION_GRADE, "enabled": False}},
    }, "beta")
    gate = DetectionCommitGate(store, "beta", policy, fence="wrapper-1")
    drive_calls = 0

    def drive(_record: dict) -> bool:
        nonlocal drive_calls
        drive_calls += 1
        store.send(
            sender="gamma",
            recipient="alpha",
            kind="note",
            body="concurrent correlated append",
            meta={"request_id": "q-no-key-append"},
        )
        return True

    turns = loop.run_loop(
        store,
        "beta",
        drive,
        commit_gate=gate,
        only_request_id=only_request_id,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
        max_polls=2,
    )

    assert turns == 1
    assert drive_calls == 1
    claim = _ledger(gate)["no_admission_claims"][inbound.id]
    assert claim["state"] == "finalized"
    if only_request_id is None:
        assert store.cursor("beta") == inbound.id
    else:
        assert store.cursor("beta") == ""
        assert store.thread_seen("beta", only_request_id) == inbound.id


def test_exhaustion_second_terminal_race_never_synthesizes_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    inbound = _question(store)
    gate = _gate(store)
    record = _record(store)
    _answer_reserved(tmp_path, gate, record)

    for sequence in range(1, 13):
        terminal = gate.resolve(record)
        _race_scoped_revision(store, sequence)
        missed = gate.finalize(record, terminal)
        assert missed.state == ResolverState.INDETERMINATE
    assert missed.reason == "finalization CAS contention exhausted"

    original_finalize = gate.finalize
    raced = False

    def race_settlement_finalize(current_record, resolution, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            _race_scoped_revision(store, 13)
        return original_finalize(current_record, resolution, **kwargs)

    monkeypatch.setattr(gate, "finalize", race_settlement_finalize)
    settled = gate.settle_retry_exhaustion(
        record,
        terminal.key,
        category="finalization",
        reason="finalization bound exhausted",
    )

    assert raced is True
    assert settled.state == ResolverState.SATISFIED
    assert not any(
        row["transition"] == "OBLIGATION_BLOCKED"
        for row in _ledger(gate)["transitions"]
    )
    restarted = _gate(store, fence="wrapper-1")
    latest = restarted.resolve(record)
    assert latest.state == ResolverState.SATISFIED
    assert restarted.finalize(record, latest).state == ResolverState.SATISFIED
    assert store.cursor("beta") == inbound.id


@pytest.mark.parametrize(
    ("new_responder", "masked"),
    [("beta", True), ("gamma", False)],
)
def test_explicit_reask_masks_failed_delivery_before_new_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_responder: str,
    masked: bool,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-pre-admission-reask")
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    gate.delivery_failed(
        record,
        admitted.key,
        reason="old generation exhausted",
        expected_revision=admitted.scoped_revision,
    )
    old = dict(_ledger(gate)["obligations"][admitted.key.digest])

    monkeypatch.setattr(
        "agenttalk.wrapper.obligations.note_bus_message",
        lambda _store, _message: None,
    )
    new_inbound = store.send(
        sender="alpha",
        recipient=new_responder,
        kind="question",
        body="retry the unanswered request",
        meta={"request_id": "q-pre-admission-reask"},
    )

    assert new_inbound.id not in _ledger(gate)["messages"]
    status = gate.delivery_status("q-pre-admission-reask", "alpha")
    assert (status is None) is masked
    scoped = recv_api.poll(
        store,
        "alpha",
        scoped_request_id="q-pre-admission-reask",
    )["scoped"]
    assert scoped["closed"] is (not masked)
    assert (scoped["delivery_failed"] is None) is masked
    assert _ledger(gate)["obligations"][admitted.key.digest] == old


@pytest.mark.parametrize(
    "replacement_meta",
    [
        {"request_id": "q-invalid-reask", "consult": True},
        {
            "broadcast_id": "q-invalid-reask",
            "broadcast_policy_version": 1,
        },
    ],
)
def test_non_owed_or_malformed_reask_cannot_mask_failed_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_meta: dict,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-invalid-reask")
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    gate.delivery_failed(
        record,
        admitted.key,
        reason="old assignment exhausted",
        expected_revision=admitted.scoped_revision,
    )
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations.note_bus_message",
        lambda _store, _message: None,
    )

    store.send(
        sender="alpha",
        recipient="beta",
        kind="question",
        body="not an admissible replacement",
        meta=replacement_meta,
    )

    status = gate.delivery_status("q-invalid-reask", "alpha")
    assert status is not None
    assert status["state"] == "delivery_failed"
    scoped = recv_api.poll(
        store,
        "alpha",
        scoped_request_id="q-invalid-reask",
    )["scoped"]
    assert scoped["closed"] is True
    assert scoped["delivery_failed"] == status


@pytest.mark.parametrize(
    "tear",
    ["requester_index", "cursor_disposition", "operation_reference"],
)
def test_torn_failed_delivery_transaction_blocks_without_advancing_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tear: str,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-torn-delivery")
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(permit, action_attempted=False)
    current = gate.resolve(record)
    monkeypatch.setattr(gate, "_advance_record_cursor", lambda _record: None)
    gate.delivery_failed(
        record,
        admitted.key,
        reason="delivery exhausted",
        expected_revision=current.scoped_revision,
    )
    ledger = _ledger(gate)
    if tear == "requester_index":
        del ledger["delivery_index"]["q-torn-delivery"]
    elif tear == "cursor_disposition":
        del ledger["cursor_dispositions"]["beta"]
    else:
        ledger["obligations"][admitted.key.digest]["dead_letter_reference"][
            "operation_nonces"
        ] = []
    gate._write(ledger)  # noqa: SLF001 - deterministic torn-transaction injection

    restarted = _gate(store, fence="wrapper-1")
    blocked = restarted.delivery_failed(
        record,
        admitted.key,
        reason="idempotent projection recovery",
        expected_revision=restarted.resolve(record).scoped_revision,
    )

    assert blocked.state == ResolverState.BLOCKED
    assert store.cursor("beta") == ""
    admission = _ledger(restarted)["obligations"][admitted.key.digest]
    assert admission["state"] == "blocked"
    assert "structurally torn" in admission["blocked_reason"]
    assert restarted.resolve(record).state == ResolverState.BLOCKED


def test_corrupt_config_during_failed_disposition_becomes_visible_blocked(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store)
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    store.config_path.write_text("{", encoding="utf-8")

    blocked = gate.fail_delivery_or_block(
        record,
        admitted.key,
        reason="configuration unreadable during disposition",
        expected_revision=admitted.scoped_revision,
    )

    assert blocked.state == ResolverState.BLOCKED
    assert store.cursor("beta") == ""
    admission = _ledger(gate)["obligations"][admitted.key.digest]
    assert admission["state"] == "blocked"
    assert gate.resolve(record).state == ResolverState.BLOCKED


def test_canonical_terminal_with_missed_eager_hook_wins_failed_disposition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-terminal-wins")
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    dispatched = gate.dispatch_record(record, permit)
    permit.draft_path.write_text("canonical answer", encoding="utf-8")
    monkeypatch.setattr(
        "agenttalk.wrapper.obligations.note_bus_message",
        lambda _store, _message: None,
    )
    assert cli.main([
        "--root",
        str(tmp_path),
        *dispatched["owed_action"]["argv"][3:],
        "--quiet",
    ]) == 0

    result = gate.fail_delivery_or_block(
        record,
        admitted.key,
        reason="stale compliance decision",
        expected_revision=admitted.scoped_revision,
    )

    assert result.state == ResolverState.SATISFIED
    assert gate.resolve(record).state == ResolverState.SATISFIED
    assert gate.delivery_status("q-terminal-wins", "alpha") is None
    assert store.cursor("beta") == record["id"]
    assert not any(
        row["transition"] in {"DELIVERY_FAILED", "OBLIGATION_BLOCKED"}
        for row in _ledger(gate)["transitions"]
    )


def test_operator_resolution_recovers_failed_delivery_without_resetting_old_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    _question(store, "q-operator-recovery")
    gate = _gate(store)
    record = _record(store)
    admitted = gate.admit_or_finalize(record)
    assert admitted.key is not None
    permit = gate.reserve_dispatch(admitted, purpose="initial")
    gate.dispatch_record(record, permit)
    gate.mark_dispatch_result(permit, action_attempted=False)
    current = gate.resolve(record)
    gate.delivery_failed(
        record,
        admitted.key,
        reason="assignment exhausted",
        expected_revision=current.scoped_revision,
    )
    spent = dict(_ledger(gate)["obligations"][admitted.key.digest])
    roster = gate.roster_snapshot()

    resolved = gate.operator_resolve(
        record,
        admitted.key,
        actor="lead",
        expected_roster_revision=roster["revision"],
        reason="operator accepted the unresolved conversation",
    )

    recovered = _ledger(gate)["obligations"][admitted.key.digest]
    assert resolved.state == ResolverState.OPERATOR_RESOLVED
    assert recovered["state"] == "operator_resolved"
    assert recovered["exhausted"] is True
    assert recovered["paid_dispatches_total"] == spent["paid_dispatches_total"]
    terminal = gate.delivery_status("q-operator-recovery", "alpha")
    assert terminal is not None
    assert terminal["state"] == "operator_resolved"
    assert terminal["actor"] == "lead"
    assert terminal["reason"] == "operator accepted the unresolved conversation"
    scoped = recv_api.poll(
        store,
        "alpha",
        scoped_request_id="q-operator-recovery",
    )["scoped"]
    assert scoped["closed"] is True
    assert scoped["delivery_failed"] is None
    assert scoped["delivery_terminal"] == terminal

    rc = cli.main([
        "--root",
        str(tmp_path),
        "wait",
        "--for",
        "alpha",
        "--to-request",
        "q-operator-recovery",
        "--timeout",
        "1",
        "--interval",
        "0.1",
    ])
    output = capsys.readouterr().out
    assert rc == 7
    assert "OPERATOR RESOLVED" in output
    assert "DELIVERY FAILED" not in output
    assert '"state": "operator_resolved"' in output


def test_reassignment_after_failed_delivery_gets_fresh_generation_and_budget(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _question(store, "q-reassigned-recovery")
    source = _gate(store)
    record = _record(store)
    admitted = source.admit_or_finalize(record)
    assert admitted.key is not None
    source.delivery_failed(
        record,
        admitted.key,
        reason="source assignment exhausted",
        expected_revision=admitted.scoped_revision,
    )
    old_spend = _ledger(source)["obligations"][admitted.key.digest][
        "paid_dispatches_total"
    ]
    next_inbound = store.send(
        sender="alpha",
        recipient="gamma",
        kind="question",
        body="reassign the unanswered request",
        meta={"request_id": "q-reassigned-recovery"},
    )

    source.transfer(
        admitted.key,
        destination="gamma",
        new_inbound_id=next_inbound.id,
    )

    ledger = _ledger(source)
    old = ledger["obligations"][admitted.key.digest]
    next_digest = ledger["inbound_index"][next_inbound.id]
    reassigned = ledger["obligations"][next_digest]
    assert old["state"] == "transferred"
    assert old["exhausted"] is True
    assert old["paid_dispatches_total"] == old_spend
    assert reassigned["state"] == "open"
    assert reassigned["exhausted"] is False
    assert reassigned["paid_dispatches_total"] == 0
    assert reassigned["key"]["delivery_generation"] == (
        admitted.key.delivery_generation + 1
    )
    assert source.delivery_status("q-reassigned-recovery", "alpha") is None
