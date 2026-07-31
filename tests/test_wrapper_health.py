"""Advisory wrapper-health contract."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import (
    cli,
    health as hm,
    supervisor as sup,
    web,
    wrapper_runtime as wrt,
)
from agenttalk.store import Store
from agenttalk.wrapper import run, session


NOW = 1_000_000.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _store(root: Path) -> Store:
    s = Store(root)
    s.init(["alpha", "beta"])
    return s


def _set_hb(store: Store, agent: str, epoch: float) -> None:
    (store.state_dir / f"{agent}.heartbeat").write_text(_iso(epoch), encoding="utf-8")


def _wrapped_cfg(**agent_overrides) -> dict:
    agent = {"auto_restart": True, "cli": "claude", "wrapped": True}
    agent.update(agent_overrides)
    return {
        "agents": {"beta": agent},
        "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
        "suspect_warn_interval_seconds": 300,
        "launch_grace_seconds": 120,
        "health": {"ttl_seconds": 300, "heartbeat_skew_seconds": 30},
    }


def _ready_state(**overrides) -> dict:
    st = {"readiness_seen": True, "resume_available": True, "launching": False}
    st.update(overrides)
    return {"agents": {"beta": st}}


def _bound_idle_runtime(store: Store, *, now_epoch: float = NOW) -> tuple[dict, list]:
    wrapper_pid = 555
    wrapper_start = _iso(now_epoch - 100)
    nonce = "A" * 32
    runtime = wrt.WrapperRuntimeWriter(
        store.state_dir,
        "beta",
        "health-test-wrapper",
        wrapper_pid=wrapper_pid,
        wrapper_start=wrapper_start,
        clock=lambda: now_epoch,
    ).idle()
    state = _ready_state(
        launcher_pid=wrapper_pid,
        launcher_start=wrapper_start,
        launcher_nonce=nonce,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
        runtime_wrapper_generation=runtime["wrapper_generation"],
    )
    snapshot = [{
        "pid": wrapper_pid,
        "parent_pid": 1,
        "name": "python.exe",
        "command_line": (
            "python -m agenttalk "
            f"--supervisor-launch-nonce {nonce} "
            f"--root {store.root} wrap --for beta --cli claude --loop"
        ),
        "start_time": wrapper_start,
        "start_filetime": str(
            int((now_epoch - 100 + 11_644_473_600) * 10_000_000)
        ),
    }]
    return state, snapshot


def _rec(body: str = "body") -> dict:
    return {
        "id": "20990101-000000-000000-HEAL",
        "from": "alpha",
        "kind": "message",
        "subject": "s",
        "body": body,
        "correlation_id": None,
        "request_id": "rq-1",
        "broadcast_id": None,
    }


def _codex_lines(*objs: dict) -> list[str]:
    return [json.dumps(o) for o in objs]


def _health_state(store: Store) -> str:
    return store.read_health("beta", ttl_seconds=999999)["state"]


def _health_reason(store: Store) -> str:
    return store.read_health("beta", ttl_seconds=999999)["reason_code"]


def test_health_write_read_schema_is_stable_and_atomic(tmp_path: Path) -> None:
    s = _store(tmp_path)
    snap = hm.build_snapshot(
        agent="beta",
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_IDLE_WAITING,
        updated_at=_iso(NOW),
        since=_iso(NOW),
        last_progress_at=None,
        reason_code="idle_waiting",
    )
    s.write_health("beta", snap)

    raw = json.loads(s.health_path("beta").read_text(encoding="utf-8"))
    assert list(raw.keys()) == list(hm.SCHEMA_KEYS)
    assert raw["state"] == hm.STATE_IDLE_WAITING
    assert not [p for p in s.state_dir.iterdir() if p.name.startswith("beta.health.") and p.suffix != ".json"]

    view = s.read_health("beta", now_epoch=NOW + 5, ttl_seconds=60)
    assert view["state"] == hm.STATE_IDLE_WAITING
    assert view["age_seconds"] == 5.0
    assert view["advisory"] is True


def test_corrupt_health_degrades_unknown_and_supervisor_does_not_crash(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.health_path("beta").write_text('{"schema_version":', encoding="utf-8")
    state, snapshot = _bound_idle_runtime(s)

    report = sup.build_report(s, now_epoch=NOW, supervisor_config=_wrapped_cfg())
    assert report["agents"]["beta"]["health"]["state"] == hm.STATE_UNKNOWN

    plan = sup.plan_actions(report, state, _wrapped_cfg(), now_epoch=NOW,
                            snapshot=snapshot)["agents"]["beta"]
    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["health"]["state"] == hm.STATE_UNKNOWN


def test_stale_health_with_fresh_heartbeat_is_ignored_with_warning(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _set_hb(s, "beta", NOW - 5)
    state, snapshot = _bound_idle_runtime(s)
    s.write_health("beta", hm.build_snapshot(
        agent="beta",
        cli="claude",
        mode="wrapper-loop",
        state=hm.STATE_WORKING_TURN,
        updated_at=_iso(NOW - 100),
        since=_iso(NOW - 100),
        last_progress_at=_iso(NOW - 100),
        reason_code="progress_event",
    ))

    report = sup.build_report(s, now_epoch=NOW, supervisor_config=_wrapped_cfg())
    h = report["agents"]["beta"]["health"]
    assert h["state"] == hm.STATE_UNKNOWN
    assert "health_older_than_heartbeat" in h["warnings"]

    plan = sup.plan_actions(report, state, _wrapped_cfg(), now_epoch=NOW,
                            snapshot=snapshot)["agents"]["beta"]
    assert plan["action"] == sup.NONE
    assert plan["state"] == "HEALTHY_IDLE"


def test_future_dated_working_health_degrades_unknown_and_does_not_delay_recovery(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _set_hb(s, "beta", NOW - 1000)
    state, snapshot = _bound_idle_runtime(s)
    s.write_health("beta", hm.build_snapshot(
        agent="beta",
        cli="claude",
        mode="wrapper-loop",
        state=hm.STATE_WORKING_SILENT,
        updated_at=_iso(NOW + 86400),
        since=_iso(NOW + 86400),
        last_progress_at=_iso(NOW + 86400),
        reason_code="turn_spawned",
    ))

    cfg = _wrapped_cfg()
    report = sup.build_report(s, now_epoch=NOW, supervisor_config=cfg)
    h = report["agents"]["beta"]["health"]
    assert h["state"] == hm.STATE_UNKNOWN
    assert "health_future_timestamp" in h["warnings"]

    plan = sup.plan_actions(report, state, cfg, now_epoch=NOW,
                            snapshot=snapshot)["agents"]["beta"]
    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["state"] == "STUCK_OR_DEAD"


def test_small_future_health_within_skew_is_accepted(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _set_hb(s, "beta", NOW - 1000)
    s.write_health("beta", hm.build_snapshot(
        agent="beta",
        cli="claude",
        mode="wrapper-loop",
        state=hm.STATE_WORKING_SILENT,
        updated_at=_iso(NOW + 5),
        since=_iso(NOW + 5),
        last_progress_at=_iso(NOW + 5),
        reason_code="turn_spawned",
    ))

    h = s.read_health("beta", now_epoch=NOW, ttl_seconds=300,
                      heartbeat_skew_seconds=30)
    assert h["state"] == hm.STATE_WORKING_SILENT
    assert h["age_seconds"] == 0.0
    assert h["stale"] is False


def test_working_health_with_stale_heartbeat_does_not_delay_recovery_or_restart(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _set_hb(s, "beta", NOW - 1000)
    state, snapshot = _bound_idle_runtime(s)
    s.write_health("beta", hm.build_snapshot(
        agent="beta",
        cli="claude",
        mode="wrapper-loop",
        state=hm.STATE_WORKING_SILENT,
        updated_at=_iso(NOW - 10),
        since=_iso(NOW - 10),
        last_progress_at=None,
        reason_code="turn_spawned",
    ))

    cfg = _wrapped_cfg()
    report = sup.build_report(s, now_epoch=NOW, supervisor_config=cfg)
    plan = sup.plan_actions(report, state, cfg, now_epoch=NOW,
                            snapshot=snapshot)["agents"]["beta"]
    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["state"] == "STUCK_OR_DEAD"
    assert plan["health"]["state"] == hm.STATE_WORKING_SILENT

    s.set_operator_facing("alpha")
    s.write_restart_request("beta", {
        "request_id": "rr-1",
        "requested_by": "alpha",
        "authorized_by": "alpha",
        "authority_result": "authorized",
        "authority_reason": "test",
        "force_protected": False,
        "force_protected_authorized": False,
    })
    report2 = sup.build_report(s, now_epoch=NOW, supervisor_config=cfg)
    plan2 = sup.plan_actions(report2, state, cfg, now_epoch=NOW,
                             snapshot=snapshot)["agents"]["beta"]
    assert plan2["action"] == sup.RELAUNCH
    assert plan2["clear_marker"] is None
    assert plan2["next_state"]["restart_request_state"] == "applied_pending_readiness"


def test_non_wrapped_working_health_with_stale_heartbeat_still_needs_stuck_signal(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    _set_hb(s, "beta", NOW - 1000)
    s.write_health("beta", hm.build_snapshot(
        agent="beta",
        cli="claude",
        mode="wrapper-loop",
        state=hm.STATE_WORKING_SILENT,
        updated_at=_iso(NOW - 10),
        since=_iso(NOW - 10),
        last_progress_at=None,
        reason_code="turn_spawned",
    ))

    cfg = _wrapped_cfg(wrapped=False, activity_hook=False)
    plan = sup.plan_actions(
        sup.build_report(s, now_epoch=NOW, supervisor_config=cfg),
        _ready_state(),
        cfg,
        now_epoch=NOW,
        snapshot=[],
    )["agents"]["beta"]

    assert plan["action"] == sup.SUSPECT_WARN
    assert plan["state"] == "ACTIVE_OR_BUSY"
    assert "advisory-health-conflict" in plan["health"]["warnings"]


class _Stream:
    def __init__(self, lines: list[str], returncode: int | None = None) -> None:
        self._lines = lines
        self.returncode = returncode

    def __iter__(self):
        return iter(self._lines)


def test_health_failure_and_degraded_mappings(tmp_path: Path) -> None:
    cases = [
        ("poison", _codex_lines({"type": "turn.failed",
                                  "error": {"message": "prompt is too long"}}),
         hm.STATE_ERRORED_POISON),
        ("ambiguous", _codex_lines({"type": "turn.started"}), hm.STATE_ERRORED_AMBIGUOUS),
        ("generic_retryable", _codex_lines({"type": "turn.started"},
                                            {"type": "error",
                                             "message": "rate limit exceeded"}),
         hm.STATE_ERRORED_AMBIGUOUS),
        ("crashed", _Stream(_codex_lines({"type": "turn.started"},
                                          {"type": "turn.completed"}), returncode=7),
         hm.STATE_CRASHED_OR_EXITED),
    ]
    for name, stream, expected in cases:
        s = _store(tmp_path / name)
        st = session.SessionState(cli="codex")
        drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                               spawn=lambda _a, _i, stream=stream: stream,
                               clock=lambda: 0.0, render=False)
        out = drive(_rec())
        assert out.ok is False
        assert _health_state(s) == expected

    s = _store(tmp_path / "structured_outage")
    st = session.SessionState(cli="claude", claude_session_id="sess-1")
    drive = run.make_drive(
        s, "beta", "claude", st, ["claude"],
        spawn=lambda _a, _i: [
            json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
            json.dumps({"type": "rate_limit_event",
                        "rate_limit_info": {"status": "throttled"}}),
        ],
        clock=lambda: 0.0,
        render=False,
    )
    out = drive(_rec())
    assert out.ok is False
    assert _health_state(s) == hm.STATE_RATE_LIMITED_OR_OUTAGE

    s = _store(tmp_path / "config_blocked_spawn")
    st = session.SessionState(cli="codex")

    def denied_spawn(_argv, _stdin):
        raise PermissionError(13, "Access is denied")

    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=denied_spawn, clock=lambda: 0.0, render=False)
    out = drive(_rec())
    assert out.ok is False
    assert _health_state(s) == hm.STATE_ERRORED_AMBIGUOUS
    assert _health_reason(s) == "config_blocked"

    s = _store(tmp_path / "worktree_collision")
    st = session.SessionState(cli="codex")
    stream = _codex_lines(
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"type": "command_execution",
                  "command": "git worktree add .worktrees/lane lane/existing",
                  "exit_code": 128,
                  "aggregated_output": (
                      "fatal: 'lane/existing' is already checked out at "
                      "'D:/repo/.worktrees/other'"
                  )}},
    )
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda _a, _i: stream, clock=lambda: 0.0, render=False)
    out = drive(_rec())
    assert out.ok is False
    assert _health_state(s) == hm.STATE_ERRORED_AMBIGUOUS
    assert _health_reason(s) == "worktree_branch_already_checked_out"

    s = _store(tmp_path / "degraded")
    st = session.SessionState(cli="codex")
    leak = '<invoke name="Read"><parameter name="file_path">x</parameter></invoke>'
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda _a, _i: _codex_lines(
                               {"type": "turn.started"},
                               {"type": "item.completed",
                                "item": {"type": "agent_message", "text": leak}},
                               {"type": "turn.completed"}),
                           clock=lambda: 0.0, render=False)
    assert drive(_rec()).ok is True
    assert _health_state(s) == hm.STATE_DEGRADED_OUTPUT


def test_classify_failure_maps_gateway_held_to_outage_before_the_error_branch() -> None:
    # #62: a TRANSIENT gateway hold is honestly an outage-like, retryable wait - NOT idle, NOT
    # dead, NOT a generic exec error. It also sets sig["error"], so the gateway_held branch must
    # win over the sig["error"] -> "spawn_exec_error" mapping, keeping the reason_code honest so
    # status/doctor show a worker blocked-on-a-held-gateway (that will self-heal on clear).
    from agenttalk.wrapper.health import classify_failure
    from agenttalk.wrapper.loop import CLASS_GATEWAY_HELD

    state, reason = classify_failure(
        {"error": "durable child turn capability unavailable"}, CLASS_GATEWAY_HELD
    )
    assert state == hm.STATE_RATE_LIMITED_OR_OUTAGE
    assert reason == "gateway_held"


def test_health_writer_parked_surfaces_blocked_state_with_resolved_request_id(
    tmp_path: Path,
) -> None:
    # #58: a config-blocked PARK must write a distinct, visible state (not leave a frozen
    # 'idle'), carrying the parked head's ids. request_id is resolved from meta (the real bus
    # shape, #17) - not read bare top-level where it is absent.
    from agenttalk.wrapper.health import WrapperHealthWriter

    s = _store(tmp_path)
    w = WrapperHealthWriter(s, "beta", "claude", mode="wrapper-loop")
    w.idle()                                        # start from idle_waiting
    assert w.state == hm.STATE_IDLE_WAITING
    w.parked({"id": "20990101-000000-000000-HEAL", "meta": {"request_id": "q-held-7"}})

    assert w.state == hm.STATE_ERRORED_AMBIGUOUS    # no longer masquerading as idle
    view = s.read_health("beta", ttl_seconds=999999)
    assert view["state"] == hm.STATE_ERRORED_AMBIGUOUS
    assert view["reason_code"] == "config_blocked"
    raw = s.read_health_raw("beta")
    assert raw["request_id"] == "q-held-7"          # resolved from meta, not a frozen None
    assert raw["msg_id"] == "20990101-000000-000000-HEAL"


def test_health_json_never_contains_message_or_output_content(tmp_path: Path) -> None:
    s = _store(tmp_path)
    secret = "SECRET_HEALTH_LEAK_74f78b"  # gitleaks:allow
    st = session.SessionState(cli="codex")
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda _a, _i: _codex_lines(
                               {"type": "turn.started"},
                               {"type": "item.started",
                                "item": {"type": "command_execution",
                                         "command": f"echo {secret}"}},
                               {"type": "item.completed",
                                "item": {"type": "command_execution",
                                         "aggregated_output": secret}},
                               {"type": "item.completed",
                                "item": {"type": "agent_message", "text": "done"}},
                               {"type": "turn.completed"}),
                           clock=lambda: 0.0, render=False)
    assert drive(_rec(secret)).ok is True
    raw = s.health_path("beta").read_text(encoding="utf-8")
    assert secret not in raw
    assert "done" not in raw


def test_status_report_plan_and_web_surface_health_as_advisory(
    tmp_path: Path,
    capsys,
) -> None:
    s = _store(tmp_path)
    now = time.time()
    _set_hb(s, "beta", now)
    state, snapshot = _bound_idle_runtime(s, now_epoch=now)
    s.write_health("beta", hm.build_snapshot(
        agent="beta",
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_IDLE_WAITING,
        reason_code="idle_waiting",
    ))

    report = sup.build_report(s, now_epoch=now, supervisor_config=_wrapped_cfg())
    assert report["agents"]["beta"]["health"]["state"] == hm.STATE_IDLE_WAITING
    plan = sup.plan_actions(report, state, _wrapped_cfg(), now_epoch=now,
                            snapshot=snapshot)["agents"]["beta"]
    assert plan["health"] == {
        "state": hm.STATE_IDLE_WAITING,
        "age_seconds": plan["health"]["age_seconds"],
        "warnings": [],
        "advisory": True,
    }
    assert plan["action"] == sup.NONE

    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    beta = next(a for a in payload["agents"] if a["name"] == "beta")
    assert beta["health"]["state"] == hm.STATE_IDLE_WAITING

    web_status = web.status_payload(s)
    assert web_status["agent_health"]["beta"]["state"] == hm.STATE_IDLE_WAITING
