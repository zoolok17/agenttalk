from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import attention as attn
from agenttalk import cli, coordination_stall as stalls, doctor, health, signing, supervisor, web
from agenttalk.store import Store
from agenttalk.wrapper import run as wrapper_run


NOW = 1_788_000_000.0
NOW_ISO = datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", "Z")


def _team(tmp_path: Path) -> tuple[Store, dict]:
    store = Store(tmp_path)
    store.init(["codex", "dev-2", "lead"])
    store.set_operator_facing("lead")
    config = {
        "root": str(tmp_path),
        "launch_grace_seconds": 30,
        "stuck_after_seconds": 60,
        "agents": {
            "codex": {
                "auto_restart": True,
                "wrapped": True,
                "cli": "codex",
                "allow_low_stuck_after": True,
            },
            "dev-2": {
                "auto_restart": True,
                "wrapped": True,
                "cli": "claude",
            },
        },
    }
    (store.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    return store, config


def _heartbeat(store: Store, agent: str, at: float = NOW) -> None:
    value = datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")
    (store.state_dir / f"{agent}.heartbeat").write_text(value, encoding="utf-8")


def _health(store: Store, agent: str, state: str, *, reason: str) -> None:
    store.write_health(
        agent,
        health.build_snapshot(
            agent=agent,
            cli="codex" if agent == "codex" else "claude",
            mode="wrapper-loop",
            state=state,
            updated_at=NOW_ISO,
            since=NOW_ISO,
            reason_code=reason,
        ),
    )


def _poll_availability(store: Store, config: dict, *, now: float = NOW) -> None:
    report = supervisor.build_report(
        store,
        now_epoch=now,
        supervisor_config=config,
        state={},
    )
    plan = supervisor.plan_actions(report, {}, config, now_epoch=now, snapshot=[])
    supervisor.record_coordination_availability_observation(
        store,
        report,
        plan,
        config,
        now_epoch=now,
    )


def _open_wait(
    store: Store,
    *,
    generation: str = "generation-1",
    await_reply: bool = True,
    working: bool = False,
) -> str:
    rid = "q-tonight-fixture"
    store.send(
        sender="codex",
        recipient="dev-2",
        kind="question",
        subject="Need review",
        body="Please review the patch.",
        meta={"request_id": rid},
    )
    store.write_waiting(
        "codex",
        {
            "agent": "codex",
            "mode": "wrapper-loop",
            "wait_token": generation,
            "wrapper_generation": generation,
        },
    )
    _heartbeat(store, "codex")
    _health(
        store,
        "codex",
        health.STATE_WORKING_TURN if working else health.STATE_IDLE_WAITING,
        reason="turn_started" if working else "wrapper_idle",
    )
    if await_reply:
        store.write_awaiting(
            "codex",
            {
                "schema_version": 1,
                "agent": "codex",
                "request_id": rid,
                "wrapper_generation": generation,
                "wait_token": "await-tonight",
                "started_at": NOW_ISO,
                "source": "send",
            },
        )
    return rid


def _make_target_unavailable(store: Store, config: dict) -> None:
    _health(store, "dev-2", health.STATE_CRASHED_OR_EXITED, reason="wrapper_child_exited")
    _poll_availability(store, config)
    _poll_availability(store, config, now=NOW + 1)


def _snapshot(store: Store, config: dict, *, now: float = NOW + 1) -> dict:
    return stalls.build_snapshot(
        store,
        now_epoch=now,
        supervisor_config=config,
        supervisor_state={},
        process_snapshot=[],
    )


def test_wrapped_explicit_wait_to_down_peer_emits_one_plain_stall(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    rid = _open_wait(store)
    _make_target_unavailable(store, config)

    snapshot = _snapshot(store, config)

    assert snapshot["diagnostics"] == []
    assert len(snapshot["items"]) == 1
    item = snapshot["items"][0]
    assert item["kind"] == "wait_target_unavailable"
    assert item["request_id"] == rid
    assert item["responders"] == ["dev-2"]
    assert item["reason"] == (
        "codex is waiting for dev-2, which is unavailable. "
        "Reassign the request or restore dev-2."
    )
    assert item["advisory"] is True


@pytest.mark.parametrize(
    ("case", "await_reply", "working"),
    [
        ("generic_wrapper_idle", False, False),
        ("ordinary_open_outbound", False, False),
        ("working_waiter", True, True),
    ],
)
def test_wrapped_wait_false_positive_guards(
    tmp_path: Path, case: str, await_reply: bool, working: bool,
) -> None:
    store, config = _team(tmp_path)
    if case == "generic_wrapper_idle":
        store.write_waiting(
            "codex",
            {
                "agent": "codex",
                "mode": "wrapper-loop",
                "wait_token": "generation-1",
                "wrapper_generation": "generation-1",
            },
        )
        _heartbeat(store, "codex")
        _health(store, "codex", health.STATE_IDLE_WAITING, reason="wrapper_idle")
    else:
        _open_wait(store, await_reply=await_reply, working=working)
    _make_target_unavailable(store, config)

    assert _snapshot(store, config)["items"] == []


def test_available_or_manual_unknown_responder_suppresses_warning(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    _heartbeat(store, "dev-2")
    _health(store, "dev-2", health.STATE_IDLE_WAITING, reason="wrapper_idle")
    _poll_availability(store, config)
    _poll_availability(store, config, now=NOW + 1)
    assert _snapshot(store, config)["items"] == []

    config["agents"].pop("dev-2")
    (store.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    (store.state_dir / "dev-2.heartbeat").unlink()
    (store.state_dir / "dev-2.health.json").unlink()
    _poll_availability(store, config, now=NOW + 2)
    _poll_availability(store, config, now=NOW + 3)
    assert _snapshot(store, config, now=NOW + 3)["items"] == []


@pytest.mark.parametrize(
    ("heartbeat_offset", "expected_state", "expects_stall"),
    [
        (
            health.DEFAULT_HEARTBEAT_SKEW_SECONDS + 300,
            supervisor.AVAILABILITY_UNKNOWN,
            False,
        ),
        (
            health.DEFAULT_HEARTBEAT_SKEW_SECONDS - 1,
            supervisor.AVAILABILITY_AVAILABLE,
            False,
        ),
        (-10_000, supervisor.AVAILABILITY_UNAVAILABLE, True),
    ],
)
def test_heartbeat_time_evidence_controls_coordination_availability(
    tmp_path: Path,
    heartbeat_offset: float,
    expected_state: str,
    expects_stall: bool,
) -> None:
    store, config = _team(tmp_path)
    # This test isolates heartbeat timestamp policy. Wrapped agents require the
    # separate strict runtime/CLI-child evidence exercised in test_supervisor.
    config["agents"]["dev-2"].update({"wrapped": False, "activity_hook": True})
    _open_wait(store)
    _heartbeat(store, "dev-2", at=NOW + heartbeat_offset)

    _poll_availability(store, config, now=NOW)
    _poll_availability(store, config, now=NOW + 1)
    observed, problems = supervisor.read_coordination_availability_observation(store)

    assert problems == []
    assert observed["dev-2"]["state"] == expected_state
    items = _snapshot(store, config, now=NOW + 1)["items"]
    assert bool(items) is expects_stall


def test_missing_liveness_is_unknown_and_retired_state_is_debounced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _config = _team(tmp_path)
    missing = supervisor.project_coordination_availability(
        "missing",
        None,
        None,
        None,
    )
    assert missing["state"] == supervisor.AVAILABILITY_UNKNOWN
    assert missing["evidence_code"] == "report_missing"

    monkeypatch.setattr(store, "retired_agents", lambda: ["retired-peer"])
    report = {"agents": {}}
    plan = {"agents": {}}
    config = {"agents": {}, "poll_seconds": 1}
    terminal = supervisor.project_coordination_availability(
        "retired-peer",
        None,
        None,
        None,
        retired=True,
    )
    supervisor.record_coordination_availability_observation(
        store, report, plan, config, now_epoch=NOW,
    )
    observed, problems = supervisor.read_coordination_availability_observation(store)
    assert problems == []
    assert not stalls._confirmed(
        terminal,
        observed["retired-peer"],
        now_epoch=NOW,
        poll_seconds=1,
    )

    supervisor.record_coordination_availability_observation(
        store, report, plan, config, now_epoch=NOW + 1,
    )
    observed, problems = supervisor.read_coordination_availability_observation(store)
    assert problems == []
    assert stalls._confirmed(
        terminal,
        observed["retired-peer"],
        now_epoch=NOW + 1,
        poll_seconds=1,
    )


def test_multi_responder_wait_requires_every_target_to_be_unavailable(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    config["agents"]["lead"] = {
        "auto_restart": True,
        "wrapped": True,
        "cli": "claude",
    }
    (store.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    rid = "b-multi-wait"
    meta = {
        "request_id": rid,
        "broadcast_id": rid,
        "audience": "reviewers",
        "audience_resolved": "dev-2,lead",
        "batch_total": "2",
    }
    for recipient in ("dev-2", "lead"):
        store.send(
            sender="codex",
            recipient=recipient,
            kind="question",
            body="review?",
            meta=meta,
        )
    store.write_waiting("codex", {
        "agent": "codex",
        "mode": "wrapper-loop",
        "wait_token": "generation-1",
        "wrapper_generation": "generation-1",
    })
    _heartbeat(store, "codex")
    _health(store, "codex", health.STATE_IDLE_WAITING, reason="wrapper_idle")
    store.write_awaiting("codex", {
        "schema_version": 1,
        "agent": "codex",
        "request_id": rid,
        "wrapper_generation": "generation-1",
        "wait_token": "await-multi",
        "started_at": NOW_ISO,
        "source": "send",
    })
    for agent in ("dev-2", "lead"):
        _health(store, agent, health.STATE_CRASHED_OR_EXITED, reason="wrapper_child_exited")
    _poll_availability(store, config)
    _poll_availability(store, config, now=NOW + 1)

    items = _snapshot(store, config)["items"]
    assert len(items) == 1
    assert items[0]["responders"] == ["dev-2", "lead"]

    config["agents"].pop("lead")
    (store.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    (store.state_dir / "lead.health.json").unlink()
    _poll_availability(store, config, now=NOW + 2)
    _poll_availability(store, config, now=NOW + 3)
    assert _snapshot(store, config, now=NOW + 3)["items"] == []


def test_unavailable_requires_two_matching_supervisor_polls(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    _health(store, "dev-2", health.STATE_CRASHED_OR_EXITED, reason="wrapper_child_exited")

    _poll_availability(store, config)
    assert _snapshot(store, config)["items"] == []

    _poll_availability(store, config, now=NOW + 1)
    assert len(_snapshot(store, config)["items"]) == 1


def test_coordination_snapshot_never_advances_supervisor_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, config = _team(tmp_path)
    calls = []
    real_observe = supervisor.observe_actions

    def observe(*args, **kwargs):
        calls.append(True)
        return real_observe(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only snapshot called the transition planner")

    monkeypatch.setattr(stalls.supervisor, "observe_actions", observe)
    monkeypatch.setattr(stalls.supervisor, "plan_actions", forbidden)

    snapshot = _snapshot(store, config)

    assert snapshot["diagnostics"] == []
    assert calls == [True]


def test_terminal_responder_is_a_reassign_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    report = supervisor.build_report(
        store,
        now_epoch=NOW,
        supervisor_config=config,
        state={},
    )
    plan = {"agents": {"dev-2": {"state": "LEAD_LOOP_STOOD_DOWN"}}}
    supervisor.record_coordination_availability_observation(
        store, report, plan, config, now_epoch=NOW,
    )
    supervisor.record_coordination_availability_observation(
        store, report, plan, config, now_epoch=NOW + 1,
    )
    monkeypatch.setattr(stalls.supervisor, "observe_actions", lambda *_args, **_kwargs: plan)

    item = _snapshot(store, config)["items"][0]
    assert item["evidence"][0]["state"] == supervisor.AVAILABILITY_TERMINAL
    assert item["evidence"][0]["subtype"] == "stood_down"
    assert item["reason"] == (
        "codex is waiting for dev-2, which is no longer active. Reassign the request."
    )


def test_generation_mismatch_and_torn_record_fail_quiet_with_diagnostic(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    _open_wait(store, generation="old-generation")
    store.write_waiting(
        "codex",
        {
            "agent": "codex",
            "mode": "wrapper-loop",
            "wait_token": "new-generation",
            "wrapper_generation": "new-generation",
        },
    )
    torn = store.state_dir / "awaiting" / "codex" / "new-generation" / "torn.json"
    torn.parent.mkdir(parents=True, exist_ok=True)
    torn.write_text("{", encoding="utf-8")
    _make_target_unavailable(store, config)

    snapshot = _snapshot(store, config)

    assert snapshot["items"] == []
    assert {d["code"] for d in snapshot["diagnostics"]} == {"await_record_invalid"}


def test_wrapped_await_state_is_strict_body_free_and_bounded(tmp_path: Path) -> None:
    store, _config = _team(tmp_path)
    record = {
        "schema_version": 1,
        "agent": "codex",
        "request_id": "q-strict",
        "wrapper_generation": "generation-1",
        "wait_token": "await-strict",
        "started_at": NOW_ISO,
        "source": "send",
    }
    with pytest.raises(ValueError, match="invalid wrapped await record"):
        store.write_awaiting("codex", {**record, "body": "must never persist"})

    forged = store.awaiting_dir / "codex" / "generation-1" / "await-forged.json"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(json.dumps({
        **record,
        "wait_token": "await-forged",
        "secret": "not allowed",
    }), encoding="utf-8")
    records, problems = store.list_awaiting("codex")
    assert records == []
    assert problems == [{
        "code": "await_record_invalid",
        "path": "codex/generation-1/await-forged.json",
    }]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wrapper_generation", ".."),
        ("wrapper_generation", "generation:1"),
        ("wait_token", "../escape"),
    ],
)
def test_wrapped_await_rejects_unsafe_path_tokens(
    tmp_path: Path, field: str, value: str,
) -> None:
    store, _config = _team(tmp_path)
    record = {
        "schema_version": 1,
        "agent": "codex",
        "request_id": "q-strict-path",
        "wrapper_generation": "generation-1",
        "wait_token": "await-strict",
        "started_at": NOW_ISO,
        "source": "send",
    }

    with pytest.raises(ValueError, match="invalid wrapped await record"):
        store.write_awaiting("codex", {**record, field: value})

    assert store.list_awaiting("codex") == ([], [])


def test_malformed_availability_observation_is_diagnostic_not_a_stall(
    tmp_path: Path,
) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    _make_target_unavailable(store, config)
    (store.state_dir / "coordination-availability.json").write_text(
        json.dumps({
            "schema_version": 1,
            "updated_at_epoch": NOW + 1,
            "agents": {
                "dev-2": {
                    "state": supervisor.AVAILABILITY_UNAVAILABLE,
                    "subtype": "heartbeat_stale",
                    "evidence_code": "heartbeat_stale_past_threshold",
                    "consecutive": "forged",
                    "first_observed_epoch": NOW,
                    "last_observed_epoch": NOW + 1,
                },
            },
        }),
        encoding="utf-8",
    )

    snapshot = _snapshot(store, config)

    assert snapshot["items"] == []
    assert snapshot["diagnostics"] == [
        {"code": "availability_observation_entry_invalid"},
    ]


def test_hmac_invalid_opener_cannot_establish_a_wait_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "hmac.key"))
    store, config = _team(project)
    signing.init_key(store.project_id())
    _open_wait(store)
    opener = store.valid_messages()[-1]
    path = store.messages_dir / f"{opener.id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["meta"].pop("signature")
    path.write_text(json.dumps(raw), encoding="utf-8")
    _make_target_unavailable(store, config)

    assert _snapshot(store, config)["items"] == []


def test_manual_scoped_wait_remains_supported(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    rid = "q-manual"
    store.send(
        sender="codex",
        recipient="dev-2",
        kind="question",
        body="manual question",
        meta={"request_id": rid},
    )
    store.write_waiting(
        "codex",
        {
            "agent": "codex",
            "pid": os.getpid(),
            "kind": "scoped",
            "to_request": rid,
            "wait_token": "manual-wait",
            "since": NOW_ISO,
            "deadline_epoch": NOW + 120,
        },
    )
    _make_target_unavailable(store, config)

    items = _snapshot(store, config)["items"]

    assert len(items) == 1
    assert items[0]["source"] == "manual_wait"


def test_reply_clears_wrapped_await_and_terminal_thread_never_warns(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    rid = _open_wait(store)
    _make_target_unavailable(store, config)

    store.send(
        sender="dev-2",
        recipient="codex",
        kind="message",
        body="answer",
        meta={"request_id": rid},
    )

    assert store.list_awaiting("codex")[0] == []
    assert _snapshot(store, config)["items"] == []


def test_send_await_reply_binds_live_generation_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    store, _config = _team(tmp_path)
    generation = "current-generation"
    store.write_waiting(
        "codex",
        {
            "agent": "codex",
            "wait_token": generation,
            "wrapper_generation": generation,
            "lead_loop": True,
            "managed": True,
        },
    )
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, generation)
    monkeypatch.setenv(wrapper_run.INBOUND_REQUEST_ID_ENV, "q-parent")

    rc = cli.main([
        "--root", str(tmp_path), "send", "--from", "codex", "--to", "dev-2",
        "--kind", "question", "--await-reply", "-m", "consult",
    ])

    assert rc == 0
    records, problems = store.list_awaiting("codex")
    assert problems == []
    assert len(records) == 1
    assert records[0]["wrapper_generation"] == generation
    assert records[0]["source"] == "send"
    assert store.wrapper_wait_generation("codex") == generation
    sent = store.valid_messages()[-1]
    assert sent.meta["request_id"] == records[0]["request_id"]
    assert sent.meta["parent_request"] == "q-parent"
    assert f"await_reply_token={records[0]['wait_token']}" in capsys.readouterr().out


def test_await_reply_requires_wrapper_and_cancel_is_token_conditional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _config = _team(tmp_path)
    before = len(store.valid_messages())
    rc = cli.main([
        "--root", str(tmp_path), "send", "--from", "codex", "--to", "dev-2",
        "--kind", "question", "--await-reply", "-m", "consult",
    ])
    assert rc == 2
    assert len(store.valid_messages()) == before

    generation = "current-generation"
    store.write_waiting(
        "codex",
        {
            "agent": "codex",
            "mode": "wrapper-loop",
            "wait_token": generation,
            "wrapper_generation": generation,
        },
    )
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, generation)
    assert cli.main([
        "--root", str(tmp_path), "send", "--from", "codex", "--to", "dev-2",
        "--kind", "question", "--await-reply", "--quiet", "-m", "consult",
    ]) == 0
    record = store.list_awaiting("codex")[0][0]

    assert cli.main([
        "--root", str(tmp_path), "await-cancel", "--from", "codex",
        "--token", "await-wrong",
    ]) == 3
    assert len(store.list_awaiting("codex")[0]) == 1
    assert cli.main([
        "--root", str(tmp_path), "await-cancel", "--from", "codex",
        "--token", record["wait_token"], "--quiet",
    ]) == 0
    assert store.list_awaiting("codex")[0] == []


def test_reply_dry_run_cannot_create_a_wrapped_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _config = _team(tmp_path)
    generation = "current-generation"
    store.write_waiting("codex", {
        "agent": "codex",
        "mode": "wrapper-loop",
        "wait_token": generation,
        "wrapper_generation": generation,
    })
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, generation)
    store.send(
        sender="dev-2",
        recipient="codex",
        kind="question",
        body="review this",
        meta={"request_id": "q-anchor"},
    )

    rc = cli.main([
        "--root", str(tmp_path), "reply", "--from", "codex",
        "--to-request", "q-anchor", "--kind", "review-request",
        "--dry-run", "--await-reply",
    ])

    assert rc == 2
    assert store.list_awaiting("codex")[0] == []


def test_reply_await_reply_requires_a_counter_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _config = _team(tmp_path)
    generation = "current-generation"
    store.write_waiting("codex", {
        "agent": "codex",
        "mode": "wrapper-loop",
        "wait_token": generation,
        "wrapper_generation": generation,
    })
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, generation)
    store.send(
        sender="dev-2",
        recipient="codex",
        kind="question",
        body="status?",
        meta={"request_id": "q-answer"},
    )

    rc = cli.main([
        "--root", str(tmp_path), "reply", "--from", "codex",
        "--to-request", "q-answer", "--kind", "question",
        "--await-reply", "-m", "not a counter-review",
    ])

    assert rc == 2
    assert store.list_awaiting("codex")[0] == []


def test_child_env_injects_and_strips_wrapped_wait_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, "stale")
    monkeypatch.setenv(wrapper_run.INBOUND_REQUEST_ID_ENV, "q-stale")

    clean = wrapper_run._child_env(tmp_path)
    bound = wrapper_run._child_env(
        tmp_path,
        wrapper_generation="generation-2",
        inbound_request_id="q-current",
    )

    assert wrapper_run.WRAPPER_GENERATION_ENV not in clean
    assert wrapper_run.INBOUND_REQUEST_ID_ENV not in clean
    assert bound[wrapper_run.WRAPPER_GENERATION_ENV] == "generation-2"
    assert bound[wrapper_run.INBOUND_REQUEST_ID_ENV] == "q-current"


def test_manual_restart_barrier_requires_current_marker_grace_and_two_polls(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    marker = {
        "agent": "dev-2",
        "request_id": "rr-current",
        "source": "manual",
        "requested_by": "lead",
        "authorized_by": "lead",
        "authority_result": "authorized",
        "authority_reason": "operator-facing liaison",
        "at": datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat().replace("+00:00", "Z"),
        "at_epoch": NOW - 60,
    }
    store.write_restart_request("dev-2", marker)
    barrier = {
        "blocked": True,
        "reason": "same_agent_wrapper_survived",
        "survivors": [{"kind": "own_wrapper", "pid": 42, "name": "python"}],
    }

    supervisor.record_supervisor_launch_barrier_observation(
        store, "dev-2", barrier, now_epoch=NOW,
    )
    assert _snapshot(store, config)["items"] == []
    supervisor.record_supervisor_launch_barrier_observation(
        store, "dev-2", barrier, now_epoch=NOW + 1,
    )

    items = _snapshot(store, config)
    assert len(items["items"]) == 1
    assert items["items"][0]["kind"] == "manual_restart_blocked"
    assert items["items"][0]["reason"] == (
        "Restart of dev-2 is still blocked by an older live process."
    )

    supervisor.record_supervisor_launch_barrier_observation(
        store,
        "dev-2",
        {"blocked": False, "reason": "clear", "survivors": []},
        now_epoch=NOW + 2,
    )
    assert _snapshot(store, config, now=NOW + 2)["items"] == []


def test_detector_has_no_coordination_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    _make_target_unavailable(store, config)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("detector attempted a coordination write")

    for name in (
        "send",
        "set_cursor",
        "advance_cursor",
        "write_restart_request",
        "clear_restart_request",
        "clear_waiting",
        "clear_waiting_if_token",
        "write_awaiting",
        "clear_awaiting_if_token",
    ):
        monkeypatch.setattr(store, name, forbidden)

    assert len(_snapshot(store, config)["items"]) == 1


def test_supervisor_observation_does_not_mutate_the_action_plan(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    report = supervisor.build_report(
        store,
        now_epoch=NOW,
        supervisor_config=config,
        state={},
    )
    plan = supervisor.plan_actions(report, {}, config, now_epoch=NOW, snapshot=[])
    before = json.dumps(plan, sort_keys=True)

    supervisor.record_coordination_availability_observation(
        store, report, plan, config, now_epoch=NOW,
    )

    assert json.dumps(plan, sort_keys=True) == before


def test_stall_identity_and_disposition_survive_age_ticks(tmp_path: Path) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    _make_target_unavailable(store, config)
    first_signal = _snapshot(store, config, now=NOW + 1)["items"][0]
    later_signal = _snapshot(store, config, now=NOW + 20)["items"][0]
    first = attn.coordination_stall_items([first_signal])[0]
    later = attn.coordination_stall_items([later_signal])[0]

    assert later["age_seconds"] > first["age_seconds"]
    assert later["item_id"] == first["item_id"]
    assert later["dedupe_key"] == first["dedupe_key"]
    assert later["source_hash"] == first["source_hash"]

    disposition = {
        "schema_version": 1,
        "event_id": "att-stall-dismiss",
        "item_id": first["item_id"],
        "source": attn.SOURCE_COORDINATION_STALL,
        "action": attn.ACTION_DISMISS,
        "actor": "lead",
        "reason": "known outage",
        "at": NOW_ISO,
        "source_snapshot": {"source_hash": first["source_hash"], "refs": []},
    }
    folded = attn.fold_dispositions([disposition])
    applied = attn.apply_disposition(later, folded, now_iso=NOW_ISO)
    assert applied["state"] == "dismissed"


def test_only_consult_and_handoff_skills_adopt_wrapped_await_reply() -> None:
    skills = Path(__file__).parents[1] / "src" / "agenttalk" / "skills"
    adopters = {
        path.relative_to(skills).as_posix()
        for path in skills.rglob("*")
        if path.is_file() and "--await-reply" in path.read_text(encoding="utf-8")
    }
    assert adopters == {
        "claude/agenttalk.consult.md",
        "claude/agenttalk.handoff.md",
        "codex/agenttalk-consult/SKILL.md",
        "codex/agenttalk-handoff/SKILL.md",
    }


def test_coordination_stall_projects_once_to_attention_doctor_status_and_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, config = _team(tmp_path)
    _open_wait(store)
    _make_target_unavailable(store, config)
    monkeypatch.setattr(stalls.time, "time", lambda: NOW + 1)

    attention_items = cli._collect_attention_items(
        store,
        for_agent="lead",
        roster=["codex", "dev-2", "lead"],
    )
    projected = [item for item in attention_items if item["source"] == attn.SOURCE_COORDINATION_STALL]
    assert len(projected) == 1
    assert projected[0]["advisory"] is True
    assert projected[0]["human_can_unblock_now"] is True

    doctor_report = doctor.run(tmp_path)
    checks = [check for check in doctor_report.checks if check.name == "coordination_stall"]
    assert len(checks) == 1
    assert checks[0].status == "warn"

    status = cli._gather_status(store)
    assert len(status["coordination_stalls"]) == 1
    assert sum("codex is waiting for dev-2" in warning for warning in status["warnings"]) == 1

    web_payload = web.build_attention(web.RootDescriptor(store, "root"), agents=[])
    web_items = [item for item in web_payload["items"] if item["source"] == "coordination_stall"]
    assert len(web_items) == 1
    assert "codex is waiting for dev-2" in web_items[0]["detail"]
