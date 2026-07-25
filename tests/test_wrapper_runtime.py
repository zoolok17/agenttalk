from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest

from agenttalk import wrapper_runtime as wr
from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api, run, session
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

    def should_not_drive(_record: dict) -> bool:
        raise AssertionError("attempt cap must dispose before drive")

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
        on_runtime_dead_letter=(
            lambda disposed: writer.dead_letter(message_id=disposed.get("id"))
        ),
    )

    view = wr.read_runtime(store.state_dir, "worker", now_epoch=NOW)
    assert store.dead_lettered_count("worker") == 1
    assert view["status"] == wr.STATUS_VALID
    assert view["record"]["phase"] == wr.PHASE_TERMINAL
    assert view["record"]["message_id"] == sent.id
    assert view["record"]["last_outcome"] == wr.OUTCOME_DEAD_LETTER


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


@pytest.mark.parametrize("interval", [-1.0, float("nan"), True])
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
