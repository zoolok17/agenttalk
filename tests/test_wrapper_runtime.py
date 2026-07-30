from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest

from agenttalk import wrapper_runtime as wr
from agenttalk import supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api, run, session
from agenttalk.wrapper.obligations import Resolution, ResolverState
from agenttalk.wrapper.work_heartbeat import WorkHeartbeatConfig


NOW = 1_800_000_000.0


def _writer(tmp_path: Path, *, clock=lambda: NOW) -> wr.WrapperRuntimeWriter:
    return wr.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=clock,
    )


@pytest.mark.parametrize(
    "agent",
    [
        "../worker",
        "worker\n",
        "w" * 65,
    ],
)
def test_runtime_path_uses_sanctioned_agent_name_validation(
    tmp_path: Path,
    agent: str,
) -> None:
    with pytest.raises(wr.RuntimeRecordError):
        wr.runtime_path(tmp_path, agent)


def test_runtime_writer_publishes_closed_lifecycle_and_monotonic_progress(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)

    idle = writer.idle()
    starting = writer.starting(message_id="msg-1", turn_id="turn-1")
    active = writer.active(456, "start-456")
    progress = writer.progress()
    terminal = writer.terminal(wr.OUTCOME_SUCCESS)
    final_idle = writer.idle()

    assert [row["phase"] for row in (
        idle, starting, active, progress, terminal, final_idle
    )] == ["idle", "starting", "active", "active", "terminal", "idle"]
    assert progress["progress_sequence"] == 1
    assert terminal["progress_sequence"] == 1
    assert final_idle["progress_sequence"] == 1
    assert final_idle["last_outcome"] == wr.OUTCOME_SUCCESS
    assert final_idle["turn_id"] is None
    assert wr.read_runtime(tmp_path, "worker", now_epoch=NOW)["status"] == wr.STATUS_VALID


def test_runtime_writer_coalesces_progress_and_forces_terminal_high_water(
    tmp_path: Path,
) -> None:
    now = [NOW]
    writer = wr.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: now[0],
        progress_write_interval_seconds=5.0,
    )
    writer.starting(message_id="msg-1", turn_id="turn-1")
    writer.active(456, "start-456")

    first = writer.progress()
    now[0] += 1.0
    second = writer.progress()
    now[0] += 1.0
    third = writer.progress()
    durable = wr.read_runtime(tmp_path, "worker", now_epoch=now[0])

    assert [first["progress_sequence"], second["progress_sequence"],
            third["progress_sequence"]] == [1, 2, 3]
    assert durable["record"]["progress_sequence"] == 1

    now[0] = NOW + 5.0
    fourth = writer.progress()
    durable = wr.read_runtime(tmp_path, "worker", now_epoch=now[0])

    assert fourth["progress_sequence"] == 4
    assert durable["record"]["progress_sequence"] == 4

    now[0] += 1.0
    fifth = writer.progress()
    terminal = writer.terminal(wr.OUTCOME_SUCCESS)
    durable = wr.read_runtime(tmp_path, "worker", now_epoch=now[0])

    assert fifth["progress_sequence"] == 5
    assert terminal["progress_sequence"] == 5
    assert durable["record"]["phase"] == wr.PHASE_TERMINAL
    assert durable["record"]["progress_sequence"] == 5


def test_dead_letter_relabels_terminal_without_inventing_progress(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    writer.starting(message_id="msg-1")
    writer.active(456, "start-456")
    failed = writer.terminal(wr.OUTCOME_FAILED)

    disposed = writer.dead_letter()

    assert disposed["last_outcome"] == wr.OUTCOME_DEAD_LETTER
    assert disposed["progress_sequence"] == failed["progress_sequence"]


def test_dead_letter_without_a_local_child_publishes_terminal_disposition(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    writer.idle()

    disposed = writer.dead_letter(message_id="msg-reconciled")

    assert disposed["phase"] == wr.PHASE_TERMINAL
    assert disposed["message_id"] == "msg-reconciled"
    assert disposed["turn_generation"] == 1
    assert disposed["progress_sequence"] == 0
    assert disposed["last_progress_at"] is None
    assert disposed["last_outcome"] == wr.OUTCOME_DEAD_LETTER


def test_reconciled_attempt_cap_dead_letters_without_launching_a_child(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="poison",
        kind="message",
    )
    record = recv_api.next_record(store, "worker")
    assert record is not None
    for index in range(3):
        store.record_attempt_start(
            "worker", record, attempt_id=f"a-{index}", at="t"
        )
        store.record_attempt_result(
            "worker",
            sent.id,
            failure_class=loop.CLASS_POISON,
            summary="failed",
            at="t",
        )

    writer = _writer(store.state_dir)
    terminal_records: list[dict] = []

    def should_not_drive(_record: dict) -> bool:
        raise AssertionError("attempt cap must dispose before drive")

    def record_dead_letter(disposed: dict) -> None:
        terminal_records.append(
            writer.dead_letter(message_id=disposed.get("id"))
        )

    loop.run_loop(
        store,
        "worker",
        should_not_drive,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=2,
        k_poison=3,
        now_iso=lambda: "t",
        on_runtime_idle=writer.idle,
        on_runtime_dead_letter=record_dead_letter,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.dead_lettered_count("worker") == 1
    assert len(terminal_records) == 1
    assert terminal_records[0]["phase"] == wr.PHASE_TERMINAL
    assert terminal_records[0]["message_id"] == sent.id
    assert terminal_records[0]["last_outcome"] == wr.OUTCOME_DEAD_LETTER
    assert view["status"] == wr.STATUS_VALID
    assert view["record"]["phase"] == wr.PHASE_IDLE
    assert view["record"]["message_id"] is None
    assert view["record"]["last_outcome"] == wr.OUTCOME_DEAD_LETTER


@pytest.mark.parametrize(
    ("case", "drive_outcome", "terminal_outcome", "expected_phases"),
    [
        (
            "normal_success",
            loop.DriveOutcome(ok=True),
            wr.OUTCOME_SUCCESS,
            [wr.PHASE_IDLE, wr.PHASE_TERMINAL, wr.PHASE_IDLE],
        ),
        (
            "drive_failure",
            loop.DriveOutcome(
                ok=False,
                failure_class=loop.CLASS_INFRA,
                summary="retryable failure",
            ),
            wr.OUTCOME_FAILED,
            [wr.PHASE_IDLE, wr.PHASE_TERMINAL],
        ),
        (
            "config_blocked_park",
            loop.DriveOutcome(
                ok=False,
                failure_class=loop.CLASS_CONFIG_BLOCKED,
                summary="configuration is blocked",
            ),
            wr.OUTCOME_FAILED,
            [wr.PHASE_IDLE, wr.PHASE_TERMINAL],
        ),
        (
            "gateway_hold",
            loop.DriveOutcome(
                ok=False,
                failure_class=loop.CLASS_GATEWAY_HELD,
                summary="gateway is held",
            ),
            wr.OUTCOME_FAILED,
            [wr.PHASE_IDLE, wr.PHASE_TERMINAL],
        ),
    ],
    ids=[
        "normal_success",
        "drive_failure",
        "config_blocked_park",
        "gateway_hold",
    ],
)
def test_continuous_loop_runtime_boundary_matrix(
    tmp_path: Path,
    case: str,
    drive_outcome: loop.DriveOutcome,
    terminal_outcome: str,
    expected_phases: list[str],
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body=case,
        kind="message",
    )
    writer = _writer(store.state_dir)
    phases: list[str] = []

    def idle() -> None:
        phases.append(writer.idle()["phase"])

    def drive(record: dict) -> loop.DriveOutcome:
        writer.starting(message_id=record["id"], turn_id=f"turn-{case}")
        phases.append(writer.terminal(terminal_outcome)["phase"])
        return drive_outcome

    loop.run_loop(
        store,
        "worker",
        drive,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        max_turns=1,
        k_poison=0,
        k_escalate=0,
        on_runtime_idle=idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert phases == expected_phases
    assert view["record"]["phase"] == expected_phases[-1]
    if case == "normal_success":
        assert store.cursor("worker") == sent.id
    else:
        assert store.cursor("worker") == ""


def test_unhandled_drive_exception_does_not_publish_idle_after_turn_start(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.send(
        sender="lead",
        recipient="worker",
        body="raise after ownership starts",
        kind="message",
    )
    writer = _writer(store.state_dir)

    def drive(record: dict) -> bool:
        writer.starting(message_id=record["id"], turn_id="turn-crashed")
        raise RuntimeError("simulated wrapper crash")

    with pytest.raises(RuntimeError, match="simulated wrapper crash"):
        loop.run_loop(
            store,
            "worker",
            drive,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
            max_polls=1,
            on_runtime_idle=writer.idle,
        )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == ""
    assert view["record"]["phase"] == wr.PHASE_STARTING


@pytest.mark.parametrize(
    ("ok", "expected_phase"),
    [
        (True, wr.PHASE_IDLE),
        (False, wr.PHASE_TERMINAL),
    ],
    ids=["cadence_success", "cadence_failure"],
)
def test_cadence_runtime_boundary_matrix(
    tmp_path: Path,
    ok: bool,
    expected_phase: str,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    writer = _writer(store.state_dir)

    def cadence() -> loop.CadenceResult:
        writer.starting(message_id=None, turn_id="turn-cadence")
        writer.terminal(wr.OUTCOME_SUCCESS if ok else wr.OUTCOME_FAILED)
        return loop.CadenceResult(ran=True, ok=ok, drove_turn=True)

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("cadence must not consume an inbound"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        cadence=cadence,
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert view["record"]["phase"] == expected_phase


def test_rescinded_terminal_control_returns_runtime_to_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="rescind the pending turn",
        kind="rescind",
    )
    record = recv_api.next_record(store, "worker")
    assert record is not None
    record = {**record, "scoped": {"closed": True, "superseded": False}}
    records = iter([record])
    monkeypatch.setattr(
        loop.recv_api,
        "next_record",
        lambda *_args, **_kwargs: next(records, None),
    )
    writer = _writer(store.state_dir)
    idle_records: list[dict] = []

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("rescinded control must not be driven"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        on_runtime_idle=lambda: idle_records.append(writer.idle()),
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == sent.id
    assert len(idle_records) == 2
    assert view["record"]["phase"] == wr.PHASE_IDLE


def test_landed_work_reconciliation_returns_runtime_to_idle(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="already landed",
        kind="question",
    )
    writer = _writer(store.state_dir)
    key = object()

    class LandedGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-landed")
            writer.terminal(wr.OUTCOME_SUCCESS)
            return Resolution(
                ResolverState.SATISFIED,
                "validated bus already landed the work",
                key=key,
                ledger_revision=1,
            )

        def finalize(self, record: dict, resolution: Resolution, **_kwargs) -> Resolution:
            recv_api.commit(store, "worker", record)
            return Resolution(
                ResolverState.SATISFIED,
                "landed work reconciled",
                key=resolution.key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("landed work must not be redriven"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        commit_gate=LandedGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == sent.id
    assert view["record"]["phase"] == wr.PHASE_IDLE


def test_terminal_cas_exhaustion_settlement_returns_runtime_and_supervisor_to_idle(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="already landed",
        kind="question",
    )
    writer = _writer(store.state_dir)
    key = object()

    class TerminalSettlementGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-settle")
            writer.terminal(wr.OUTCOME_SUCCESS)
            return Resolution(
                ResolverState.SATISFIED,
                "validated bus already landed the work",
                key=key,
                ledger_revision=1,
            )

        def finalize(self, record: dict, resolution: Resolution, **_kwargs) -> Resolution:
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalization CAS contention exhausted",
                key=resolution.key,
            )

        def settle_retry_exhaustion(
            self,
            record: dict,
            current_key: object,
            **_kwargs,
        ) -> Resolution:
            assert current_key is key
            recv_api.commit(store, "worker", record)
            return Resolution(
                ResolverState.SATISFIED,
                "terminal settlement advanced the validated cursor",
                key=key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("settlement must not redrive the model"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=2,
        commit_gate=TerminalSettlementGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == sent.id
    assert view["status"] == wr.STATUS_VALID
    assert view["record"]["phase"] == wr.PHASE_IDLE

    config = {
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "codex",
                "wrapped": True,
            }
        }
    }
    report = {
        "protected": False,
        "heartbeat_stale": False,
        "heartbeat_age_seconds": 1.0,
        "restart_request": None,
    }
    state = {
        "readiness_seen": True,
        "launching": False,
        "backoff_next_epoch": 0.0,
    }
    liveness = {
        "runtime_status": wr.STATUS_VALID,
        "runtime_record": view["record"],
        "runtime_updated_age_seconds": view["updated_age_seconds"],
        "runtime_progress_age_seconds": view["progress_age_seconds"],
        "wrapper_state": "alive",
        "managed_pids": [],
        "kill_targets": [],
    }
    plan = sup._plan_one(
        "worker",
        report,
        state,
        config,
        config["agents"]["worker"],
        liveness,
        now_epoch=NOW,
    )
    assert plan["state"] == "HEALTHY_IDLE"


def test_terminal_cas_exhaustion_with_pending_cursor_stays_terminal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="terminal but not consumed",
        kind="question",
    )
    writer = _writer(store.state_dir)
    key = object()

    class PendingTerminalSettlementGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-pending")
            writer.terminal(wr.OUTCOME_SUCCESS)
            return Resolution(
                ResolverState.SATISFIED,
                "validated terminal is ready to finalize",
                key=key,
                ledger_revision=1,
            )

        def finalize(self, _record: dict, resolution: Resolution, **_kwargs) -> Resolution:
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalization CAS contention exhausted",
                key=resolution.key,
            )

        def settle_retry_exhaustion(
            self,
            _record: dict,
            current_key: object,
            **_kwargs,
        ) -> Resolution:
            assert current_key is key
            return Resolution(
                ResolverState.SATISFIED,
                "canonical terminal replayed; cursor projection remains pending",
                key=key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("terminal replay must not redrive the model"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        commit_gate=PendingTerminalSettlementGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == ""
    assert sent.id != store.cursor("worker")
    assert view["record"]["phase"] == wr.PHASE_TERMINAL

    config = {
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "codex",
                "wrapped": True,
            }
        }
    }
    plan = sup._plan_one(
        "worker",
        {
            "protected": False,
            "heartbeat_stale": False,
            "heartbeat_age_seconds": 1.0,
            "restart_request": None,
        },
        {
            "readiness_seen": True,
            "launching": False,
            "backoff_next_epoch": 0.0,
        },
        config,
        config["agents"]["worker"],
        {
            "runtime_status": wr.STATUS_VALID,
            "runtime_record": view["record"],
            "runtime_updated_age_seconds": view["updated_age_seconds"],
            "runtime_progress_age_seconds": view["progress_age_seconds"],
            "wrapper_state": "alive",
            "managed_pids": [],
            "kill_targets": [],
        },
        now_epoch=NOW,
    )
    assert plan["state"] == "CLI_CHILD_UNKNOWN"


def test_delivery_exhaustion_settlement_returns_runtime_to_idle(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="delivery cannot be completed",
        kind="question",
    )
    writer = _writer(store.state_dir)
    key = object()

    class DeliveryExhaustedGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-delivery")
            writer.terminal(wr.OUTCOME_FAILED)
            return Resolution(
                ResolverState.OWED_UNSATISFIED,
                "delivery is still owed",
                key=key,
                scoped_revision=1,
            )

        def captured_operation(self, _key: object) -> None:
            return None

        def next_dispatch_purpose(self, _key: object) -> None:
            return None

        def dispatch_exhausted(self, _key: object) -> bool:
            return True

        def fail_delivery_or_block(
            self,
            record: dict,
            current_key: object,
            **_kwargs,
        ) -> Resolution:
            assert current_key is key
            recv_api.commit(store, "worker", record)
            return Resolution(
                ResolverState.DELIVERY_EXHAUSTED,
                "delivery exhausted and cursor advanced",
                key=key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("delivery exhaustion must not redrive the model"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        commit_gate=DeliveryExhaustedGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == sent.id
    assert view["record"]["phase"] == wr.PHASE_IDLE
    assert view["record"]["last_outcome"] == wr.OUTCOME_FAILED


def test_delivery_exhaustion_with_pending_cursor_stays_terminal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.send(
        sender="lead",
        recipient="worker",
        body="delivery terminal is not yet consumed",
        kind="question",
    )
    writer = _writer(store.state_dir)
    key = object()

    class PendingDeliveryGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-delivery-pending")
            writer.terminal(wr.OUTCOME_FAILED)
            return Resolution(
                ResolverState.OWED_UNSATISFIED,
                "delivery is still owed",
                key=key,
                scoped_revision=1,
            )

        def captured_operation(self, _key: object) -> None:
            return None

        def next_dispatch_purpose(self, _key: object) -> None:
            return None

        def dispatch_exhausted(self, _key: object) -> bool:
            return True

        def fail_delivery_or_block(
            self,
            _record: dict,
            current_key: object,
            **_kwargs,
        ) -> Resolution:
            assert current_key is key
            return Resolution(
                ResolverState.DELIVERY_EXHAUSTED,
                "canonical delivery terminal; cursor projection remains pending",
                key=key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("delivery exhaustion must not redrive the model"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        commit_gate=PendingDeliveryGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == ""
    assert view["record"]["phase"] == wr.PHASE_TERMINAL


def test_authorized_release_returns_runtime_to_idle(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.set_operator_facing("lead")
    released = store.send(
        sender="lead",
        recipient="worker",
        body="stand down",
        kind="release",
        meta={
            "release_authority": "human",
            "operator_decision": "true",
            "authority_reason": "operator requested stand-down",
        },
    )
    writer = _writer(store.state_dir)
    writer.starting(message_id="prior-turn", turn_id="turn-prior")
    writer.terminal(wr.OUTCOME_FAILED)
    idle_records: list[dict] = []

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("authorized release must not be driven"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        on_runtime_idle=lambda: idle_records.append(writer.idle()),
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == released.id
    assert len(idle_records) == 2
    assert view["record"]["phase"] == wr.PHASE_IDLE


@pytest.mark.parametrize(
    ("ok", "expected_phases"),
    [
        (True, [wr.PHASE_IDLE, wr.PHASE_TERMINAL, wr.PHASE_IDLE]),
        (False, [wr.PHASE_IDLE, wr.PHASE_TERMINAL]),
    ],
    ids=["one_shot_success", "one_shot_failure"],
)
def test_one_shot_runtime_boundary_matrix(
    tmp_path: Path,
    ok: bool,
    expected_phases: list[str],
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    request_id = "rq-one-shot"
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="scoped work",
        kind="question",
        meta={"request_id": request_id},
    )
    writer = _writer(store.state_dir)
    phases: list[str] = []

    def idle() -> None:
        phases.append(writer.idle()["phase"])

    def drive(record: dict) -> loop.DriveOutcome:
        writer.starting(message_id=record["id"], turn_id="turn-one-shot")
        phases.append(
            writer.terminal(
                wr.OUTCOME_SUCCESS if ok else wr.OUTCOME_FAILED
            )["phase"]
        )
        return loop.DriveOutcome(
            ok=ok,
            failure_class=None if ok else loop.CLASS_INFRA,
            summary="" if ok else "retryable one-shot failure",
        )

    turns = loop.run_loop(
        store,
        "worker",
        drive,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        max_turns=1,
        only_request_id=request_id,
        on_runtime_idle=idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert turns == (1 if ok else 0)
    assert phases == expected_phases
    assert view["record"]["phase"] == expected_phases[-1]
    if ok:
        assert store.thread_seen("worker", request_id) == sent.id
    else:
        assert recv_api.poll(
            store,
            "worker",
            scoped_request_id=request_id,
        )["record"]["id"] == sent.id


def test_one_shot_committed_terminal_finalization_returns_runtime_to_idle(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    request_id = "rq-one-shot-finalized"
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="already landed scoped work",
        kind="question",
        meta={"request_id": request_id},
    )
    writer = _writer(store.state_dir)
    key = object()

    class CommittedOneShotGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-one-shot-finalized")
            writer.terminal(wr.OUTCOME_SUCCESS)
            return Resolution(
                ResolverState.SATISFIED,
                "validated scoped terminal",
                key=key,
                ledger_revision=1,
            )

        def finalize(self, record: dict, resolution: Resolution, **_kwargs) -> Resolution:
            recv_api.commit(store, "worker", record)
            return Resolution(
                ResolverState.SATISFIED,
                "scoped consume boundary committed",
                key=resolution.key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("committed scoped work must not be redriven"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        only_request_id=request_id,
        commit_gate=CommittedOneShotGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == ""
    assert store.thread_seen("worker", request_id) == sent.id
    assert view["record"]["phase"] == wr.PHASE_IDLE


def test_one_shot_terminal_cas_exhaustion_with_pending_thread_seen_stays_terminal(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    request_id = "rq-one-shot-pending"
    store.send(
        sender="lead",
        recipient="worker",
        body="scoped terminal is pending",
        kind="question",
        meta={"request_id": request_id},
    )
    writer = _writer(store.state_dir)
    key = object()

    class PendingOneShotSettlementGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-one-shot-pending")
            writer.terminal(wr.OUTCOME_SUCCESS)
            return Resolution(
                ResolverState.SATISFIED,
                "validated scoped terminal is ready to finalize",
                key=key,
                ledger_revision=1,
            )

        def finalize(self, _record: dict, resolution: Resolution, **_kwargs) -> Resolution:
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalization CAS contention exhausted",
                key=resolution.key,
            )

        def settle_retry_exhaustion(
            self,
            _record: dict,
            current_key: object,
            **_kwargs,
        ) -> Resolution:
            assert current_key is key
            return Resolution(
                ResolverState.SATISFIED,
                "canonical scoped terminal replayed; thread-seen remains pending",
                key=key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("terminal replay must not redrive scoped work"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        only_request_id=request_id,
        commit_gate=PendingOneShotSettlementGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == ""
    assert store.thread_seen("worker", request_id) == ""
    assert view["record"]["phase"] == wr.PHASE_TERMINAL


@pytest.mark.parametrize(
    ("commit_projection", "expected_phase"),
    [
        (True, wr.PHASE_IDLE),
        (False, wr.PHASE_TERMINAL),
    ],
    ids=["committed", "pending"],
)
def test_one_shot_delivery_terminal_runtime_requires_committed_thread_seen(
    tmp_path: Path,
    commit_projection: bool,
    expected_phase: str,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    request_id = "rq-one-shot-delivery"
    sent = store.send(
        sender="lead",
        recipient="worker",
        body="scoped delivery exhaustion",
        kind="question",
        meta={"request_id": request_id},
    )
    writer = _writer(store.state_dir)
    key = object()

    class OneShotDeliveryGate:
        fence = "generation-1"

        def admit_or_finalize(self, record: dict) -> Resolution:
            writer.starting(message_id=record["id"], turn_id="turn-one-shot-delivery")
            writer.terminal(wr.OUTCOME_FAILED)
            return Resolution(
                ResolverState.OWED_UNSATISFIED,
                "delivery is still owed",
                key=key,
                scoped_revision=1,
            )

        def captured_operation(self, _key: object) -> None:
            return None

        def next_dispatch_purpose(self, _key: object) -> None:
            return None

        def dispatch_exhausted(self, _key: object) -> bool:
            return True

        def fail_delivery_or_block(
            self,
            record: dict,
            current_key: object,
            **_kwargs,
        ) -> Resolution:
            assert current_key is key
            if commit_projection:
                recv_api.commit(store, "worker", record)
            return Resolution(
                ResolverState.DELIVERY_EXHAUSTED,
                "canonical scoped delivery terminal",
                key=key,
            )

    loop.run_loop(
        store,
        "worker",
        lambda _record: pytest.fail("delivery exhaustion must not redrive scoped work"),
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_polls=1,
        only_request_id=request_id,
        commit_gate=OneShotDeliveryGate(),
        on_runtime_idle=writer.idle,
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.cursor("worker") == ""
    assert store.thread_seen("worker", request_id) == (
        sent.id if commit_projection else ""
    )
    assert view["record"]["phase"] == expected_phase


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update({"extra": True}),
        lambda row: row.update({"phase": "future"}),
        lambda row: row.update({"progress_sequence": -1}),
        lambda row: row.update({"updated_at": "not-a-time"}),
        lambda row: row.update({"phase": "active", "cli_launcher_pid": None}),
    ],
)
def test_validate_record_rejects_unknown_or_inconsistent_fields(
    tmp_path: Path,
    mutate,
) -> None:
    row = _writer(tmp_path).idle()
    mutate(row)

    with pytest.raises(wr.RuntimeRecordError):
        wr.validate_record(row, expected_agent="worker", now_epoch=NOW)


def test_torn_or_bom_prefixed_runtime_read_is_one_invalid_observation(
    tmp_path: Path,
) -> None:
    path = wr.runtime_path(tmp_path, "worker")
    path.write_bytes(b'{"schema_version":1')
    assert wr.read_runtime(tmp_path, "worker", now_epoch=NOW) == {
        "status": wr.STATUS_INVALID,
        "error": "malformed",
    }

    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_writer(tmp_path).idle()).encode())
    view = wr.read_runtime(tmp_path, "worker", now_epoch=NOW)
    assert view == {"status": wr.STATUS_INVALID, "error": "malformed"}


def test_duplicate_runtime_key_is_rejected_without_salvaging_fields(
    tmp_path: Path,
) -> None:
    wr.runtime_path(tmp_path, "worker").write_bytes(
        b'{"phase":"idle","phase":"active"}'
    )

    assert wr.read_runtime(tmp_path, "worker", now_epoch=NOW) == {
        "status": wr.STATUS_INVALID,
        "error": "malformed",
    }


def test_future_runtime_timestamp_fails_closed(tmp_path: Path) -> None:
    row = _writer(tmp_path).idle()
    row["updated_at"] = wr._utc_iso(NOW + wr.MAX_FUTURE_SKEW_SECONDS + 1)
    wr.runtime_path(tmp_path, "worker").write_text(json.dumps(row), encoding="utf-8")

    assert wr.read_runtime(tmp_path, "worker", now_epoch=NOW)["status"] == wr.STATUS_INVALID


def test_bounded_concurrent_write_lead_is_valid_with_zero_age(tmp_path: Path) -> None:
    row = _writer(tmp_path).idle()
    row["updated_at"] = wr._utc_iso(NOW + wr.MAX_FUTURE_SKEW_SECONDS)
    wr.runtime_path(tmp_path, "worker").write_text(json.dumps(row), encoding="utf-8")

    view = wr.read_runtime(tmp_path, "worker", now_epoch=NOW)

    assert view["status"] == wr.STATUS_VALID
    assert view["updated_age_seconds"] == 0.0


@pytest.mark.parametrize(
    ("wrapper_generation", "wrapper_pid"),
    [
        (None, 123),
        ("generation-1", True),
    ],
)
def test_writer_constructor_rejects_non_scalar_identity_fields(
    tmp_path: Path,
    wrapper_generation,
    wrapper_pid,
) -> None:
    with pytest.raises(wr.RuntimeRecordError):
        wr.WrapperRuntimeWriter(
            tmp_path,
            "worker",
            wrapper_generation,
            wrapper_pid=wrapper_pid,
            wrapper_start="start",
        )


@pytest.mark.parametrize("interval", [-1.0, 5.001, float("nan"), True])
def test_writer_constructor_rejects_invalid_progress_write_interval(
    tmp_path: Path,
    interval: object,
) -> None:
    with pytest.raises(wr.RuntimeRecordError):
        wr.WrapperRuntimeWriter(
            tmp_path,
            "worker",
            "generation-1",
            wrapper_pid=123,
            wrapper_start="start",
            progress_write_interval_seconds=interval,
        )


def test_writer_fsyncs_before_replace_and_leaves_no_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real_fsync = wr.os.fsync

    def observe(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(wr.os, "fsync", observe)

    _writer(tmp_path).idle()

    assert calls
    assert list(tmp_path.glob("*.tmp")) == []
    assert wr.runtime_path(tmp_path, "worker").read_bytes().startswith(b"{")


def test_directory_fsync_failure_is_not_reported_as_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_directory_fsync(_path: Path) -> None:
        raise OSError(errno.EIO, "simulated directory fsync failure")

    monkeypatch.setattr(wr, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(wr.RuntimeWriteError):
        _writer(tmp_path).idle()
    assert list(tmp_path.glob("*.tmp")) == []


def test_make_drive_advances_progress_only_for_real_adapter_events(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.init(["worker"])
    state = session.SessionState(cli="codex")
    writer = _writer(store.state_dir)
    heartbeats = {"count": 0}

    class Stream(list):
        returncode = 0

    def heartbeat() -> None:
        heartbeats["count"] += 1

    def spawn(_argv, _stdin):
        writer.active(456, "start-456")
        heartbeat()
        heartbeat()
        heartbeat()
        return Stream([
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message",'
            '"text":"done"}}',
            '{"type":"turn.completed","usage":{}}',
        ])

    drive = run.make_drive(
        store,
        "worker",
        "codex",
        state,
        ["codex"],
        spawn=spawn,
        clock=lambda: 0.0,
        heartbeat=heartbeat,
        runtime_writer=writer,
        render=False,
        min_interval=999.0,
    )
    outcome = drive({
        "id": "msg-1",
        "from": "lead",
        "to": "worker",
        "kind": "message",
        "subject": "task",
        "body": "do it",
        "meta": {},
    })
    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)

    assert outcome.ok is True
    assert heartbeats["count"] >= 4
    assert view["status"] == wr.STATUS_VALID
    assert view["record"]["phase"] == wr.PHASE_TERMINAL
    assert view["record"]["last_outcome"] == wr.OUTCOME_SUCCESS
    assert view["record"]["progress_sequence"] == 2


def test_work_heartbeat_ticks_do_not_advance_runtime_progress(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    writer.starting(message_id="msg-1", turn_id="turn-1")
    stamps = {"count": 0}
    stream = run._ProcStream(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            "import time; time.sleep(0.25)",
        ],
        None,
        work_heartbeat=WorkHeartbeatConfig(
            enabled=True,
            interval_seconds=0.03,
            max_turn_seconds=0.15,
        ),
        work_heartbeat_stamp=lambda: stamps.__setitem__(
            "count", stamps["count"] + 1
        ),
        on_spawn=writer.active,
    )

    list(stream)
    view = wr.read_runtime(tmp_path, "worker", now_epoch=NOW)

    assert stamps["count"] > 0
    assert view["status"] == wr.STATUS_VALID
    assert view["record"]["phase"] == wr.PHASE_ACTIVE
    assert view["record"]["progress_sequence"] == 0
    assert view["record"]["last_progress_at"] is None


def test_proc_stream_reports_exact_child_exit_after_output_is_drained() -> None:
    exits: list[tuple[int, str | None, int]] = []
    stream = run._ProcStream(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            "import sys; print('actual-output'); raise SystemExit(7)",
        ],
        None,
        on_exit=lambda pid, start, rc: exits.append((pid, start, rc)),
    )

    output = list(stream)

    assert output == ["actual-output\n"]
    assert stream.returncode == 7
    assert exits == [(stream.pid, stream.pid_start, 7)]
