"""WP-2: the agent supervisor's Python core.

The safety table (plan_actions) is the heart of the feature — it must be
CI-testable WITHOUT launching terminals, so these tests drive it via plain
fixtures. The generated PS/bash scripts are thin executors (documented-manual).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import cli, ephemeral as eph, health as hm, supervisor as sup
from agenttalk.store import Store


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _team(tmp_path: Path, agents: str = "lead,worker") -> Store:
    s = Store(tmp_path)
    s.init(agents.split(","))
    return s


NOW = 1_000_000.0
TEST_ROOT = r"D:\agenttalk-test-root"
SUPERVISOR_NONCE = "A" * 32
OTHER_NONCE = "B" * 32


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _ps_iso(microsecond: int) -> str:
    return f"2026-07-04T07:20:31.{microsecond:06d}0+00:00"


LAUNCHER_START = _ps_iso(100000)
BRAIN_START = _ps_iso(200000)
RUNNER_START = _ps_iso(300000)
WAIT_START = _ps_iso(400000)
WRAP_START = _ps_iso(500000)
WRAP_CHILD_START = _ps_iso(600000)


_CONFIG = {
    "root": TEST_ROOT,
    "agents": {"worker": {"auto_restart": True, "cli": "codex"}},
    "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
    "suspect_warn_interval_seconds": 300,
    "launch_grace_seconds": 120,
}
_HOOK_CODEX_CONFIG = {
    **_CONFIG,
    "agents": {"worker": {"auto_restart": True, "cli": "codex",
                          "activity_hook": True}},
}


def _wrapped_supervisor_agent(name: str, cli_name: str, *, explicit_root: bool = True) -> dict:
    args = ["-m", "agenttalk"]
    if explicit_root:
        args += ["--root", "{ROOT}"]
    args += ["wrap", "--for", name, "--cli", cli_name, "--loop", "--", f"{cli_name}.exe"]
    if cli_name == "codex":
        args += ["--disable", "hooks"]
    return {
        "cli": cli_name,
        "auto_restart": True,
        "wrapped": True,
        "cwd": TEST_ROOT,
        "env": {"AGENTTALK_SELF": name},
        "launch": {"windows_file": "python.exe", "windows_args": args},
    }


def _write_supervisor_config(store: Store, agents: dict) -> None:
    (store.dir / "supervisor.json").write_text(
        json.dumps({"schema_version": 2, "agents": agents}, indent=2),
        encoding="utf-8",
    )


def _auth_marker(rid: str = "rr-1", *, force_protected: bool = False,
                 live_ack: bool = False) -> dict:
    return {
        "request_id": rid,
        "requested_by": "lead",
        "authorized_by": "lead",
        "authority_result": "authorized",
        "authority_reason": "sole_lead",
        "force_protected": force_protected,
        "force_protected_authorized": force_protected,
        "force_protected_authorized_by": "lead" if force_protected else None,
        "acknowledge_live_protected_kill": live_ack,
        "acknowledge_live_protected_kill_authorized": live_ack,
        "acknowledge_live_protected_kill_by": "lead" if live_ack else None,
    }


def test_ps_template_claims_instance_drains_and_releases_under_brake() -> None:
    ps = sup.PS_TEMPLATE
    assert "'supervise', '--claim-instance'" in ps
    assert "'supervise', '--drain-intents'" in ps
    assert "'supervise', '--release-instance'" in ps
    assert "supervise --launch-barrier" in ps
    assert "'--pid-start', $SupervisorStart" in ps
    assert "Assert-ActionsEnabled 'drain-intents'" in ps
    assert "finally" in ps


def test_supervise_drain_intents_manual_claims_and_releases_instance(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    s.write_intent("send", {"target": "worker", "body": "hello"})

    rc = _run(["supervise", "--drain-intents", "--pid", "123", "--pid-start", "start"], tmp_path)

    assert rc == 0
    assert s.read_supervisor_instance() is None
    assert s.messages_for("worker")[0].body == "hello"


def test_supervise_claim_instance_refuses_kill_switch_and_release_allows_cleanup(tmp_path: Path) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    assert _run(["supervise", "--claim-instance", "--pid", "123"], tmp_path) == 3

    (s.dir / "supervisor.kill").unlink()
    rec = s.claim_supervisor_instance(pid=123, pid_start="start")
    assert rec is not None
    (s.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    assert _run([
        "supervise", "--release-instance", "--pid", "123",
        "--pid-start", "start", "--instance-token", rec["token"],
    ], tmp_path) == 0
    assert s.read_supervisor_instance() is None

# ---- snapshot-model fixtures (the 8-state classifier reads a process snapshot) ----
BRAIN_PID, LAUNCHER_PID, WAIT_PID = 200, 199, 400


def _snap(*, cli="codex", brain=True, wait=True, agent="worker",
          brain_pid=BRAIN_PID, brain_start=BRAIN_START):
    """A synthetic process snapshot. ``brain`` adds the long-lived CLI brain
    process; ``wait`` adds the agent's `agenttalk wait` child (its command line
    is what lets discovery climb to the brain + detect a zombie wait)."""
    name = "codex.exe" if cli == "codex" else "claude.exe"
    rows = []
    if brain:
        rows.append({"pid": brain_pid, "parent_pid": LAUNCHER_PID, "name": name,
                     "command_line": name, "start_time": brain_start})
    if wait:
        rows.append({"pid": WAIT_PID, "parent_pid": brain_pid if brain else 1,
                     "name": "python.exe",
                     "command_line": f"python -m agenttalk --root {TEST_ROOT} wait --for {agent} --timeout 1800",
                     "start_time": WAIT_START})
    return rows


def _ready(**over) -> dict:
    """A supervisor-state entry for an agent that has reached readiness once
    (brain recorded, resume available, out of grace)."""
    st = {"brain_pid": BRAIN_PID, "brain_start": BRAIN_START, "readiness_seen": True,
          "resume_available": True, "launching": False}
    st.update(over)
    return st


def _report(**agent_fields) -> dict:
    # a stale heartbeat implies a LARGE age (older than any stuck threshold),
    # unless the caller pins heartbeat_age_seconds explicitly.
    stale = agent_fields.get("heartbeat_stale", False)
    base = {"protected": False, "heartbeat_stale": False,
            "heartbeat_age_seconds": 9999.0 if stale else 1.0,
            "waiting_pid_alive": False, "restart_request": None}
    base.update(agent_fields)
    return {"agents": {"worker": base}}


def _plan(report, state, *, now=NOW, snapshot=None, config=_CONFIG):
    # snapshot defaults to [] (captured but EMPTY = no process rows), not None
    # (None means the capture FAILED; heartbeat liveness still applies).
    snap = [] if snapshot is None else snapshot
    return sup.plan_actions(report, state, config, now_epoch=now,
                            snapshot=snap)["agents"]["worker"]


# ----------------------------------------- the 5 required safety scenarios

def test_scenario_i_fresh_heartbeat_ignores_missing_brain_snapshot() -> None:
    # Heartbeat freshness is the liveness authority: a missing brain snapshot no
    # longer kills a healthy, heartbeating agent.
    p = _plan(_report(), {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=[])
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"


def test_scenario_ii_alive_stale_without_hook_is_suspect_warn_not_kill() -> None:
    # stale + NO activity hook (default _CONFIG) -> ACTIVE_OR_BUSY: SUSPECT only,
    # never kill (the WP-2 trap).
    p = _plan(_report(heartbeat_stale=True),
              {"agents": {"worker": _ready(last_warn_epoch=0)}}, snapshot=_snap())
    assert p["action"] == sup.SUSPECT_WARN and p["state"] == "ACTIVE_OR_BUSY"
    assert p["kill_first"] is False
    # rate-limited: warned recently -> NONE this poll
    p3 = _plan(_report(heartbeat_stale=True),
               {"agents": {"worker": _ready(last_warn_epoch=NOW - 10)}}, snapshot=_snap())
    assert p3["action"] == sup.NONE
    # fresh heartbeat -> healthy regardless of hook
    assert _plan(_report(heartbeat_stale=False),
                 {"agents": {"worker": _ready()}}, snapshot=_snap())["action"] == sup.NONE


def test_scenario_iii_manual_marker_relaunches_and_waits_for_readiness() -> None:
    marker = _auth_marker("rr-1")
    p = _plan(_report(restart_request=marker),
              {"agents": {"worker": _ready(backoff_next_epoch=NOW + 9999)}},
              snapshot=_snap())
    assert p["action"] == sup.RELAUNCH and p["state"] == "MANUAL_RESTART"
    assert p["clear_marker"] is None
    assert p["kill_first"] is True          # best-effort cleanup before relaunch
    assert p["kill_targets"]                # non-empty (launcher + managed)
    assert p["bypass_backoff"] is True      # bypasses the future backoff_next
    assert "rr-1" in p["next_state"]["consumed_rids"]
    assert p["next_state"]["restart_request_state"] == "applied_pending_readiness"


def test_scenario_iv_protected_stale_heartbeat_is_warn_only() -> None:
    p = _plan(_report(protected=True, heartbeat_stale=True),
              {"agents": {"worker": _ready()}}, snapshot=[],
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.WARN_ONLY
    assert p["state"] == "STUCK_OR_DEAD"
    assert p["notify"] is True
    # never a relaunch/kill of a protected agent


def test_scenario_v_backoff_escalates_then_resets() -> None:
    # 1st stale heartbeat with activity hook -> stuck recovery, backoff base (30)
    s1 = {"agents": {"worker": _ready(consecutive_fails=0, backoff_next_epoch=0)}}
    p1 = _plan(_report(heartbeat_stale=True), s1, snapshot=[],
               config=_HOOK_CODEX_CONFIG)
    assert p1["action"] == sup.STUCK_RECOVER
    assert p1["next_state"]["backoff_next_epoch"] == NOW + 30
    # still stale, inside backoff -> wait
    s2 = {"agents": {"worker": _ready(consecutive_fails=1, backoff_next_epoch=NOW + 30)}}
    assert _plan(_report(heartbeat_stale=True), s2, now=NOW + 5, snapshot=[],
                 config=_HOOK_CODEX_CONFIG)["action"] == sup.BACKOFF_WAIT
    # backoff elapsed -> recovery, delay doubles (60)
    p3 = _plan(_report(heartbeat_stale=True), s2, now=NOW + 31, snapshot=[],
               config=_HOOK_CODEX_CONFIG)
    assert p3["action"] == sup.STUCK_RECOVER
    assert p3["next_state"]["consecutive_fails"] == 2
    assert p3["next_state"]["backoff_next_epoch"] == (NOW + 31) + 60
    # sustained liveness (fresh hb) resets the backoff
    s4 = {"agents": {"worker": _ready(consecutive_fails=2, healthy_since=NOW - 200)}}
    p4 = _plan(_report(), s4, snapshot=_snap())
    assert p4["action"] == sup.NONE
    assert p4["next_state"]["consecutive_fails"] == 0
    assert p4["next_state"]["backoff_next_epoch"] == 0.0


# ----------------------------------------- marker / protected edge cases

def test_protected_marker_without_force_is_refused_and_stays_visible() -> None:
    p = _plan(_report(protected=True, heartbeat_stale=True,
                      restart_request=_auth_marker("rr-9")),
              {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.REFUSE_PROTECTED
    assert p["clear_marker"] is None
    assert p["notify"] is True


def test_protected_marker_with_force_relaunches() -> None:
    p = _plan(_report(protected=True, heartbeat_stale=True,
                      restart_request=_auth_marker("rr-9", force_protected=True)),
              {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.RELAUNCH
    assert p["clear_marker"] is None


def test_unauthorized_restart_marker_refuses_and_stays_visible() -> None:
    p = _plan(_report(restart_request={"request_id": "rr-forged",
                                       "requested_by": "peer",
                                       "force_protected": True}),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}})
    assert p["action"] == sup.REFUSE_PROTECTED
    assert p["state"] == "RESTART_UNAUTHORIZED"
    assert p["clear_marker"] is None


def test_fresh_protected_force_requires_second_live_kill_ack() -> None:
    marker = _auth_marker("rr-live", force_protected=True)
    p = _plan(_report(protected=True, heartbeat_stale=False, restart_request=marker),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}},
              snapshot=_snap())
    assert p["action"] == sup.REFUSE_PROTECTED
    assert p["state"] == "LIVE_PROTECTED_REFUSED"
    assert p["clear_marker"] is None


def test_fresh_protected_second_live_kill_ack_relaunches() -> None:
    marker = _auth_marker("rr-live", force_protected=True, live_ack=True)
    p = _plan(_report(protected=True, heartbeat_stale=False, restart_request=marker),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}},
              snapshot=_snap())
    assert p["action"] == sup.RELAUNCH
    assert p["clear_marker"] is None
    assert p["next_state"]["restart_requested_by"] == "lead"


def test_restart_cooldown_defers_without_consuming_marker() -> None:
    marker = _auth_marker("rr-cool")
    p = _plan(_report(restart_request=marker),
              {"agents": {"worker": _ready(last_launch_epoch=NOW - 10)}},
              config={**_CONFIG, "restart_cooldown_seconds": 45},
              snapshot=_snap())
    assert p["action"] == sup.BACKOFF_WAIT
    assert p["state"] == "RESTART_COOLDOWN"
    assert p["clear_marker"] is None
    assert "requested_by=lead" in p["reason"]


def test_consumed_marker_still_dead_does_not_bypass_backoff() -> None:
    # the manual relaunch already fired (rid consumed) but the heartbeat is STILL
    # stale -> do NOT bypass backoff again every poll; honor the backoff window.
    p = _plan(_report(heartbeat_stale=True, restart_request=_auth_marker("rr-1")),
              {"agents": {"worker": _ready(consumed_rids=["rr-1"],
                                           backoff_next_epoch=NOW + 9999)}},
              snapshot=[], config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.BACKOFF_WAIT
    assert p["clear_marker"] is None        # never silently clear a failed request


def test_consumed_marker_now_alive_clears() -> None:
    p = _plan(_report(restart_request=_auth_marker("rr-1")),
              {"agents": {"worker": _ready(consumed_rids=["rr-1"])}}, snapshot=_snap())
    assert p["action"] == sup.CLEAR_MARKER
    assert p["clear_marker"] == "rr-1"


def test_snapshot_unavailable_uses_only_versioned_provenanced_priors() -> None:
    report = _report(restart_request=_auth_marker("rr-1"))
    prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 333,
        "start": WAIT_START,
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 1,
        "last_fresh_attribution_epoch": NOW - 1,
        "seed_descendants": True,
        "source_launcher_pid": 111,
        "source_launcher_start": LAUNCHER_START,
        "source_launcher_nonce": SUPERVISOR_NONCE,
    }
    state = {"agents": {"worker": _ready(
        launcher_pid=111,
        launcher_start=LAUNCHER_START,
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
        managed_pids=[{"pid": 222}, {"pid": 444, "start": WAIT_START}, prior],
    )}}
    plan = sup.plan_actions(report, state, _CONFIG, now_epoch=NOW, snapshot=None)
    targets = plan["agents"]["worker"]["kill_targets"]
    assert targets == [{"pid": 333, "start": WAIT_START,
                        "reason": "provenanced_prior", "source": "provenanced_prior"}]
    assert plan["agents"]["worker"]["diagnostics"]["legacy_unverifiable_dropped"] == 2
    assert plan["agents"]["worker"]["diagnostics"]["snapshot_unavailable_no_descendants"] == 1


def test_only_auto_restart_agents_are_planned() -> None:
    cfg = {"agents": {"worker": {"auto_restart": False}}}
    plan = sup.plan_actions(_report(), {"agents": {"worker": _ready()}},
                            cfg, now_epoch=NOW, snapshot=[])
    assert plan["agents"] == {}


# ----------------------------------------- build_report (bus-side facts)

def test_report_protected_is_operator_facing_union_all_leads(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead1,lead2,worker")
    # force a 2-lead config (set_role enforces single-lead, so write directly)
    cfg = json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["roles"] = {"lead1": "lead", "lead2": "lead"}
    s.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    rpt = sup.build_report(s, now_epoch=NOW)
    assert rpt["agents"]["lead1"]["protected"] is True   # BOTH leads protected
    assert rpt["agents"]["lead2"]["protected"] is True
    assert rpt["agents"]["worker"]["protected"] is False


def test_report_heartbeat_stale_and_waiting(tmp_path: Path, monkeypatch) -> None:
    s = _team(tmp_path)
    s.write_heartbeat("worker")  # fresh now
    monkeypatch.setattr("agenttalk.supervisor._process_alive", lambda pid: pid == 4242)
    s.write_waiting("worker", {"agent": "worker", "pid": 4242, "deadline_epoch": None})
    hb_ts = s.read_heartbeat("worker").timestamp()
    # just after the heartbeat -> fresh; far later -> stale
    fresh = sup.build_report(s, now_epoch=hb_ts + 1, suspect_after_seconds=120)
    assert fresh["agents"]["worker"]["heartbeat_stale"] is False
    assert fresh["agents"]["worker"]["heartbeat_evidence"] == sup.HEARTBEAT_EVIDENCE_OBSERVED
    assert fresh["agents"]["worker"]["waiting_pid_alive"] is True
    stale = sup.build_report(s, now_epoch=hb_ts + 999, suspect_after_seconds=120)
    assert stale["agents"]["worker"]["heartbeat_stale"] is True
    assert stale["agents"]["worker"]["heartbeat_evidence"] == sup.HEARTBEAT_EVIDENCE_OBSERVED
    # an agent that never heartbeated reads as stale
    assert stale["agents"]["lead"]["heartbeat_stale"] is True
    assert stale["agents"]["lead"]["heartbeat_age_seconds"] is None
    assert stale["agents"]["lead"]["heartbeat_evidence"] == sup.HEARTBEAT_EVIDENCE_MISSING


def test_report_rejects_heartbeat_beyond_future_skew(tmp_path: Path) -> None:
    s = _team(tmp_path)
    future = NOW + hm.DEFAULT_HEARTBEAT_SKEW_SECONDS + 1
    (s.state_dir / "worker.heartbeat").write_text(_iso(future), encoding="utf-8")

    report = sup.build_report(s, now_epoch=NOW, suspect_after_seconds=120)

    worker = report["agents"]["worker"]
    assert worker["heartbeat_stale"] is True
    assert worker["heartbeat_age_seconds"] is None
    assert worker["heartbeat_evidence"] == sup.HEARTBEAT_EVIDENCE_FUTURE_SKEW

    legacy_report = json.loads(json.dumps(report))
    for agent in legacy_report["agents"].values():
        agent.pop("heartbeat_evidence")
    assert sup.plan_actions(report, {}, _CONFIG, now_epoch=NOW, snapshot=[]) == (
        sup.plan_actions(legacy_report, {}, _CONFIG, now_epoch=NOW, snapshot=[])
    )


def test_report_allows_heartbeat_within_future_skew(tmp_path: Path) -> None:
    s = _team(tmp_path)
    future = NOW + hm.DEFAULT_HEARTBEAT_SKEW_SECONDS
    (s.state_dir / "worker.heartbeat").write_text(_iso(future), encoding="utf-8")

    report = sup.build_report(s, now_epoch=NOW, suspect_after_seconds=120)

    worker = report["agents"]["worker"]
    assert worker["heartbeat_stale"] is False
    assert worker["heartbeat_age_seconds"] == 0.0
    assert worker["heartbeat_evidence"] == sup.HEARTBEAT_EVIDENCE_OBSERVED


def test_report_reflects_restart_request(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    _run(["request-restart", "--for", "worker", "--from", "lead", "--reason", "x"], tmp_path)
    rpt = sup.build_report(s, now_epoch=NOW)
    rr = rpt["agents"]["worker"]["restart_request"]
    assert rr is not None and rr["agent"] == "worker" and rr["request_id"].startswith("rr-")


# ----------------------------------------- request-restart + clear command

def test_request_restart_writes_marker(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    assert _run(["request-restart", "--for", "worker", "--from", "lead",
                 "--reason", "outage", "--force-protected"], tmp_path) == 0
    m = s.read_restart_request("worker")
    assert m["agent"] == "worker" and m["source"] == "manual"
    assert m["requested_by"] == "lead" and m["force_protected"] is True
    assert m["authority_result"] == "authorized"
    assert m["force_protected_authorized_by"] == "lead"
    assert m["reason"] == "outage" and m["request_id"].startswith("rr-")


def test_request_restart_missing_requested_by_is_denied(tmp_path: Path) -> None:
    s = _team(tmp_path)
    auth = sup.resolve_restart_request_authority(s, None, force_protected=True)
    assert auth["authority_result"] == "denied"
    assert auth["force_protected_authorized"] is False


def test_request_restart_unauthorized_peer_cannot_force_protected(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    assert _run(["request-restart", "--for", "lead", "--from", "worker",
                 "--force-protected"], tmp_path) == 2
    assert s.read_restart_request("lead") is None


def test_restart_authority_operator_facing_controls_live_kill_ack(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead,ops,worker")
    s.set_role("lead", "lead")
    s.set_operator_facing("ops")
    lead_auth = sup.resolve_restart_request_authority(
        s, "lead", force_protected=True, acknowledge_live_protected_kill=True)
    assert lead_auth["authority_result"] == "denied"
    ops_auth = sup.resolve_restart_request_authority(
        s, "ops", force_protected=True, acknowledge_live_protected_kill=True)
    assert ops_auth["authority_result"] == "authorized"
    assert ops_auth["acknowledge_live_protected_kill_authorized"] is True


def test_restart_marker_revalidates_authority_at_plan_time(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead,ops,worker")
    s.set_role("lead", "lead")
    s.set_operator_facing("ops")
    assert _run(["request-restart", "--for", "lead", "--from", "ops",
                 "--force-protected", "--acknowledge-live-protected-kill"],
                tmp_path) == 0
    marker = s.read_restart_request("lead")
    assert marker is not None and marker["authority_result"] == "authorized"
    assert marker["force_protected_authorized"] is True
    assert marker["acknowledge_live_protected_kill_authorized"] is True

    s.set_operator_facing(None)
    (s.state_dir / "lead.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    cfg = {**_CONFIG, "agents": {"lead": {"auto_restart": True, "cli": "codex"}}}
    report = sup.build_report(s, now_epoch=NOW + 1, supervisor_config=cfg)
    live_marker = report["agents"]["lead"]["restart_request"]
    assert live_marker["authority_still_valid"] is False
    assert live_marker["force_protected_still_authorized"] is False
    assert live_marker["acknowledge_live_protected_kill_still_authorized"] is False

    plan = sup.plan_actions(
        report,
        {"agents": {"lead": _ready(backoff_next_epoch=0)}},
        cfg,
        now_epoch=NOW + 1,
        snapshot=_snap(agent="lead"),
    )["agents"]["lead"]
    assert plan["action"] == sup.REFUSE_PROTECTED
    assert plan["state"] == "RESTART_UNAUTHORIZED"
    assert plan["clear_marker"] is None
    assert s.read_restart_request("lead")["request_id"] == marker["request_id"]


def test_request_restart_unknown_agent_exit_2(tmp_path: Path) -> None:
    _team(tmp_path)
    assert _run(["request-restart", "--for", "ghost"], tmp_path) == 2


def test_clear_restart_only_matching_rid(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.write_restart_request("worker", {"agent": "worker", "request_id": "rr-new"})
    # a stale rid must NOT clear a newer marker
    assert _run(["supervise", "--clear-restart", "--for", "worker",
                 "--request-id", "rr-old"], tmp_path) == 0
    assert s.read_restart_request("worker") is not None
    # the matching rid clears it
    assert _run(["supervise", "--clear-restart", "--for", "worker",
                 "--request-id", "rr-new"], tmp_path) == 0
    assert s.read_restart_request("worker") is None


def test_clear_restart_request_store_level_compare(tmp_path: Path) -> None:
    # C5b: clear_restart_request runs read/compare/unlink UNDER store._config_lock so a
    # concurrent write_restart_request cannot replace the marker between the compare and
    # the unlink. Behaviorally: a stale clearer (old id) never removes a newer marker;
    # the matching id does. (Both ops are lock-protected now.)
    s = _team(tmp_path)
    s.write_restart_request("worker", {"agent": "worker", "request_id": "rr-1"})
    # a newer request supersedes the marker
    s.write_restart_request("worker", {"agent": "worker", "request_id": "rr-2"})
    # the stale clearer intending rr-1 must NOT remove the newer rr-2 marker
    assert s.clear_restart_request("worker", "rr-1") is False
    assert (s.read_restart_request("worker") or {}).get("request_id") == "rr-2"
    # the matching clearer removes it
    assert s.clear_restart_request("worker", "rr-2") is True
    assert s.read_restart_request("worker") is None
    # clearing an absent marker is a no-op False
    assert s.clear_restart_request("worker", "rr-2") is False


# ----------------------------------------- supervise --init / --plan CLI

def test_supervise_init_generates_and_is_idempotent(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    assert _run(["supervise", "--init"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "written" in out
    assert (s.dir / "supervisor.json").exists()
    assert (s.dir / "supervisor.ps1").exists()
    assert (s.dir / "supervisor-task.ps1").exists()
    assert (s.dir / "deadman.ps1").exists()
    assert (s.dir / "bin" / "agenttalk.cmd").exists()   # the project-local shim
    assert not (s.dir / "supervisor.sh").exists()  # v1: PowerShell only
    # script anchors: calls the Python plan (NO invalid --json flag), launches
    # the REAL exe via -FilePath/-ArgumentList (NOT Invoke-Expression), applies
    # + restores env, the dry-run hook, and preserves state on a failed launch.
    ps = (s.dir / "supervisor.ps1").read_text(encoding="utf-8")
    assert "supervise --plan --state-file" in ps
    assert "--record-events" in ps
    assert "--json" not in ps                       # blocker regression guard
    assert "Start-Process -FilePath" in ps and "-ArgumentList" in ps and "-PassThru" in ps
    assert "Invoke-Expression" not in ps            # file/args executor, no expr
    assert "windows_file" in ps                     # launches the real exe
    assert "DryRun" in ps
    assert "keeping marker/state for retry" in ps   # clear-only-on-success path
    task = (s.dir / "supervisor-task.ps1").read_text(encoding="utf-8-sig")
    assert "New-ScheduledTaskTrigger -AtLogOn" in task
    assert "StartWhenAvailable" in task
    assert "MultipleInstances IgnoreNew" in task
    assert "RestartCount" in task and "ExecutionTimeLimit" in task
    assert "WorkingDirectory" in task
    assert "LastRunTime" in task and "LastTaskResult" in task
    assert "-NoLogo -NoProfile -NonInteractive -File" in task
    assert "--prepare-task-install" in task
    assert "--commit-task-install" in task
    assert "--clear-task-binding" in task
    assert "function Find-CheckoutTasks" in task
    assert "function Get-ActionSupervisorPath" in task
    assert "[System.IO.Path]::GetFullPath($path)" in task
    assert "different task binding(s) already target this checkout" in task
    assert "remains installed; binding was not cleared" in task
    assert "Register-ScheduledTask" in task and "-Force" not in task
    assert "function Remove-PreparedTaskIfOwned" in task
    assert "Remove-PreparedTaskIfOwned $PowerShell $Arguments $Root" in task
    assert "powershell.exe" not in task.casefold()
    assert "supervisor.ps1" in task and "-Quiet" in task
    deadman_ps = (s.dir / "deadman.ps1").read_text(encoding="utf-8-sig")
    assert "deadman" in deadman_ps
    assert "supervisor-state.json" not in deadman_ps
    # idempotent: a second --init overwrites nothing
    assert _run(["supervise", "--init"], tmp_path) == 0
    assert "all files already exist" in capsys.readouterr().out


def test_supervisor_report_surfaces_kill_switch_and_mutations_refuse(
    tmp_path: Path, capsys,
) -> None:
    s = _team(tmp_path)
    s.write_restart_request("worker", {"agent": "worker", "request_id": "rr-1"})
    (s.dir / "supervisor.kill").write_text("anything", encoding="utf-8")

    rc = _run(["supervise", "--clear-restart", "--for", "worker",
               "--request-id", "rr-1"], tmp_path)
    assert rc == 3
    assert (s.read_restart_request("worker") or {}).get("request_id") == "rr-1"

    rc = _run(["supervise", "--report", "--now", str(NOW)], tmp_path)
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kill_switch_active"] is True


def test_ps_template_kill_switch_guards_mutating_boundaries() -> None:
    ps = sup.PS_TEMPLATE
    assert "$KillSwitchPath" in ps
    assert "function Actions-Enabled" in ps
    assert "function Assert-ActionsEnabled" in ps
    assert "function Save-State($state)" in ps
    assert "if (-not (Actions-Enabled)) { return }" in ps
    assert "Get-ProcSnapshot $SnapPath" in ps
    assert "if (Actions-Enabled)" in ps
    assert "Assert-ActionsEnabled (\"agent {0} {1}\"" in ps
    assert "Assert-ActionsEnabled (\"launch-request {0} {1}\"" in ps
    assert "Assert-ActionsEnabled (\"ephemeral {0} {1}\"" in ps
    assert "supervisor.kill" in ps


def test_python_supervisor_state_recovers_only_from_validated_backup(tmp_path: Path) -> None:
    path = tmp_path / "supervisor-state.json"
    first = {"agents": {"worker": {"pid": 101}}}
    second = {"agents": {"worker": {"pid": 202}}}
    sup.save_supervisor_state(path, first)
    sup.save_supervisor_state(path, second)

    path.write_text('{"agents":', encoding="utf-8")
    assert sup.load_supervisor_state(path) == first

    sup.supervisor_state_backup_path(path).write_text("[]", encoding="utf-8")
    with pytest.raises(sup.SupervisorPersistenceError):
        sup.load_supervisor_state(path)


def test_python_supervisor_state_interrupted_replace_preserves_primary(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "supervisor-state.json"
    first = {"agents": {"worker": {"pid": 101}}}
    second = {"agents": {"worker": {"pid": 202}}}
    sup.save_supervisor_state(path, first)
    real_replace = sup.os.replace

    def fail_primary_replace(src, dst) -> None:
        if Path(dst) == path:
            raise OSError("injected primary replace failure")
        real_replace(src, dst)

    monkeypatch.setattr(sup.os, "replace", fail_primary_replace)
    with pytest.raises(OSError, match="injected primary replace failure"):
        sup.save_supervisor_state(path, second)

    assert sup.load_supervisor_state(path) == first


def test_supervisor_config_loader_accepts_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.json"
    expected = {"schema_version": 2, "agents": {"worker": {"cli": "codex"}}}
    path.write_text("\ufeff" + json.dumps(expected), encoding="utf-8")

    assert sup.load_supervisor_config(path) == expected


def test_supervise_plan_loads_bom_prefixed_project_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(
        "\ufeff" + json.dumps(_CONFIG), encoding="utf-8",
    )
    state_file = s.dir / "supervisor-state.json"
    state_file.write_text(
        json.dumps({"agents": {"worker": {}}}), encoding="utf-8",
    )
    snapshot_file = s.dir / "supervisor-snapshot.json"
    snapshot_file.write_text("[]", encoding="utf-8")

    rc = _run([
        "supervise", "--plan", "--state-file", str(state_file),
        "--snapshot-file", str(snapshot_file), "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert set(plan["agents"]) == {"worker"}


def test_supervise_rejects_non_object_project_config_without_emitting_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text("[]", encoding="utf-8")
    state_file = s.dir / "supervisor-state.json"
    state_file.write_text(
        json.dumps({"agents": {"worker": {}}}), encoding="utf-8",
    )

    rc = _run([
        "supervise", "--plan", "--state-file", str(state_file),
        "--now", str(NOW),
    ], tmp_path)

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "supervisor config must be a JSON object" in captured.err


def test_supervise_report_recovers_backup_without_rewriting_corrupt_primary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(
        json.dumps(_CONFIG), encoding="utf-8",
    )
    state_file = s.dir / "supervisor-state.json"
    backup_file = sup.supervisor_state_backup_path(state_file)
    state_file.write_text('{"agents":', encoding="utf-8")
    backup_file.write_text(
        json.dumps({"agents": {"worker": {"session_id": "backup-session"}}}),
        encoding="utf-8",
    )
    primary_before = state_file.read_bytes()
    backup_before = backup_file.read_bytes()

    rc = _run([
        "supervise", "--report", "--state-file", str(state_file),
        "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["agents"]["worker"]["session_id"] == "backup-session"
    assert state_file.read_bytes() == primary_before
    assert backup_file.read_bytes() == backup_before


def test_supervise_record_launch_refuses_two_corrupt_state_copies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    state_file = s.dir / "supervisor-state.json"
    backup_file = sup.supervisor_state_backup_path(state_file)
    state_file.write_text('{"agents":', encoding="utf-8")
    backup_file.write_text("[]", encoding="utf-8")
    primary_before = state_file.read_bytes()
    backup_before = backup_file.read_bytes()

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "supervisor state" in captured.err
    assert state_file.read_bytes() == primary_before
    assert backup_file.read_bytes() == backup_before


def test_supervise_plan_refuses_two_corrupt_state_copies_without_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(
        json.dumps(_CONFIG), encoding="utf-8",
    )
    state_file = s.dir / "supervisor-state.json"
    backup_file = sup.supervisor_state_backup_path(state_file)
    state_file.write_text('{"agents":', encoding="utf-8")
    backup_file.write_text("[]", encoding="utf-8")
    primary_before = state_file.read_bytes()
    backup_before = backup_file.read_bytes()

    rc = _run([
        "supervise", "--plan", "--state-file", str(state_file),
        "--now", str(NOW),
    ], tmp_path)

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "supervisor state" in captured.err
    assert state_file.read_bytes() == primary_before
    assert backup_file.read_bytes() == backup_before


def test_supervise_record_launch_creates_missing_state_with_atomic_codec(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    state_file = s.dir / "supervisor-state.json"

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    state = sup.load_supervisor_state(state_file)
    assert state["agents"]["worker"]["launcher_pid"] == 777


def test_record_launch_authoritatively_persists_launch_clock_and_grace(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    (store.dir / "supervisor.json").write_text(
        json.dumps({
            "agents": {"worker": {"auto_restart": True, "cli": "claude"}},
            "launch_grace_seconds": 75,
        }),
        encoding="utf-8",
    )
    state_file = store.dir / "supervisor-state.json"
    state_file.write_text(
        json.dumps({"agents": {"worker": {"last_launch_epoch": NOW - 100}}}),
        encoding="utf-8",
    )

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "claude",
        "--pid", "777", "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    persisted = sup.load_supervisor_state(state_file)["agents"]["worker"]
    assert persisted["last_launch_epoch"] == NOW
    assert persisted["launch_grace_until"] == NOW + 75


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_supervisor_state_writer_retries_retryable_windows_replace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    state_file = tmp_path / "supervisor-state.json"
    real_replace = os.replace
    attempts = 0

    def contended_replace(source: str, destination: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("state file is in use")
            error.winerror = winerror
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(sup.os, "replace", contended_replace)

    sup.save_supervisor_state(
        state_file,
        {"agents": {"worker": {"launcher_pid": 777}}},
    )

    assert attempts == 3
    assert sup.load_supervisor_state(state_file)["agents"]["worker"]["launcher_pid"] == 777


def test_supervisor_state_writer_does_not_retry_permanent_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def disk_full_replace(source: str, destination: str) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(28, "disk full", destination)

    monkeypatch.setattr(sup.os, "replace", disk_full_replace)

    with pytest.raises(OSError, match="disk full"):
        sup.save_supervisor_state(
            tmp_path / "supervisor-state.json",
            {"agents": {"worker": {}}},
        )

    assert attempts == 1


def test_ps_state_helpers_are_atomic_backed_up_and_fail_closed() -> None:
    ps = sup.PS_TEMPLATE
    block = ps[ps.index("# region state-helpers"):ps.index("# endregion state-helpers")]
    mutations = ps[
        ps.index("# region checked-mutations"):
        ps.index("# endregion checked-mutations")
    ]
    poll_loop = ps[ps.index("$pollNum = 0\n:supervisorPoll do {"):ps.index("} while (-not $Once)")]
    assert "$StateBackupPath" in ps
    assert "[System.IO.FileStream]" in block
    assert ".Flush($true)" in block
    assert "[System.IO.File]::Replace" in block
    assert "[System.IO.File]::Move" in block
    assert "function Invoke-StateFileSwapWithRetry" in block
    assert "Test-IsRetryableStateWriteException $_.Exception" in block
    assert "for ($attempt = 1; $attempt -le 8; $attempt++)" in block
    assert "$delayMs = [Math]::Min(250, $delayMs * 2)" in block
    assert "function Save-StateForPoll" in block
    assert "state write failed this poll; will retry next poll" in block
    assert poll_loop.count("Save-StateForPoll $state") == 3
    assert poll_loop.count("if (-not (Save-StateForPoll $state))") == 3
    assert poll_loop.count("continue supervisorPoll") >= 3
    reserve = "Set-AgentState $state $name $p.next_state\n          if (-not (Save-StateForPoll $state))"
    launch_index = poll_loop.index("$res = Launch $name $p $homeEnv")
    record_index = poll_loop.index("$recordArgs = @('--root', $Root, 'supervise', '--record-launch'")
    assert reserve in poll_loop
    assert poll_loop.index(reserve) < launch_index
    assert "Save-StateForPoll $state" not in poll_loop[launch_index:record_index]
    assert "$LASTEXITCODE" in mutations
    assert poll_loop.count(
        'Invoke-CheckedSupervisorMutation ("record-launch {0}"'
    ) == 1
    assert poll_loop.count(
        'Invoke-CheckedSupervisorMutation ("record-ephemeral-launch {0}"'
    ) == 1
    assert poll_loop.count(
        'Invoke-CheckedSupervisorMutation ("archive-launch-request {0}"'
    ) == 4
    assert "& $AgenttalkCmd --root $Root supervise --record-launch" not in poll_loop
    assert "& $AgenttalkCmd --root $Root supervise --record-ephemeral-launch" not in poll_loop
    assert "& $AgenttalkCmd --root $Root supervise --archive-launch-request" not in poll_loop
    assert "Save-State $state" not in poll_loop
    assert "function Test-StateObject" in block
    assert "throw" in block
    assert "Set-Content $StatePath" not in ps


def test_supervisor_hosting_doc_covers_degraded_mode_and_services() -> None:
    text = Path("docs/supervisor-hosting.md").read_text(encoding="utf-8")
    assert "Scheduled Task" in text
    assert "supervisor.kill" in text
    assert "Degraded Mode" in text
    assert "agenttalk threads --for <agent>" in text
    assert "agenttalk wait --for <agent> --timeout 1800" in text
    assert "WinSW" in text and "NSSM" in text
    assert "session 0" in text
    assert "sc.exe" not in text.lower()


def test_supervisor_tutorial_documents_disabled_work_heartbeat_recovery_limit() -> None:
    text = Path("docs/supervisor-tutorial.md").read_text(encoding="utf-8")
    assert "work_heartbeat.enabled=false" in text
    assert "disables automatic stale recovery" in text
    assert "warning-only" in text


def test_ps_template_console_action_log_and_quiet() -> None:
    # 0.29.0 observability: the NORMAL loop (not just -DryRun) logs each agent's
    # state+action+reason - real actions ALWAYS print, steady no-action agents
    # print on state CHANGE only (no per-poll flood); -Quiet suppresses all of it.
    ps = sup.PS_TEMPLATE
    assert "[switch]$Quiet" in ps                       # the suppress switch
    assert "-not $Quiet" in ps                          # the log is gated on it
    assert "$p.action -ne 'none'" in ps                 # always-print a real action
    assert "$lastLogged" in ps                          # change-detection memory
    assert "agents healthy" in ps                       # periodic liveness summary
    # the healthy count is HEALTHY_IDLE ONLY, not every action=='none' state
    # (LAUNCHING / rate-limited ACTIVE_OR_BUSY also have no action) - codex r1.
    assert "$p.state -eq 'HEALTHY_IDLE'" in ps
    assert "if ($DryRun)" in ps                         # DryRun keeps its own print
    # reviewer-1 r1/r2: -Quiet must silence ALL warnings - including the ones in the
    # helper functions on the relaunch path - so it sets $WarningPreference once
    # (inherited by called functions) rather than gating each Write-Warning. And the
    # warning actions are NOT double-logged by the info Write-Host (excluded there).
    assert ("'warn_only','suspect_warn','refuse_protected','snapshot_unavailable',"
            "'readiness_gave_up' -notcontains") in ps
    assert "$WarningPreference = 'SilentlyContinue'" in ps   # -Quiet silences all warnings


def test_ps_template_readiness_gave_up_warns_and_notifies() -> None:
    # 0.31.2 (reviewer-1): the new terminal READINESS_GAVE_UP action must be
    # surfaced - routed through the SAME Write-Warning + bus-notify branch as the
    # other warn actions (warn_only/suspect_warn/...), NOT fall through to the
    # silent `default` that only persists state. The whole point of the cap is to
    # STOP unattended churn while telling the operator intervention is required.
    ps = sup.PS_TEMPLATE
    warn_branch = ps[ps.index("{ $_ -in 'warn_only'"):]
    warn_branch = warn_branch[:warn_branch.index("default {")]
    assert "'readiness_gave_up'" in warn_branch              # routed through warn/notify
    assert "Write-Warning" in warn_branch                    # surfaced to the console
    # the notify path sends a bus note when notify + notify_sender/notify_to are set
    assert "$p.notify -and $cfg.notify_sender -and $cfg.notify_to" in warn_branch
    assert "--kind note" in warn_branch


def test_supervise_plan_cli_with_fixtures(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    report_file = tmp_path / "rpt.json"
    report_file.write_text(json.dumps(_report()), encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    snap_file = tmp_path / "snap.json"   # empty snapshot is fine when heartbeat is fresh
    snap_file.write_text("[]", encoding="utf-8")
    rc = _run(["supervise", "--plan", "--report-file", str(report_file),
               "--state-file", str(state_file), "--snapshot-file", str(snap_file),
               "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["agents"]["worker"]["action"] == sup.NONE
    assert plan["agents"]["worker"]["state"] == "HEALTHY_IDLE"


def test_supervisor_assessment_copies_real_plan_decisions_verbatim() -> None:
    conflict_health = {
        "state": hm.STATE_WORKING_SILENT,
        "age_seconds": 1.0,
        "warnings": [],
        "advisory": True,
    }
    cases = [
        _plan(_report(protected=True, heartbeat_stale=True),
              {"agents": {"worker": _ready()}}, snapshot=[],
              config=_HOOK_CODEX_CONFIG),
        _plan(_report(heartbeat_stale=True),
              {"agents": {"worker": _ready(last_warn_epoch=0)}}, snapshot=_snap()),
        _plan(_report(protected=True, restart_request=_auth_marker("rr-protected")),
              {"agents": {"worker": _ready()}}, snapshot=_snap()),
        _plan(_report(heartbeat_stale=True),
              {"agents": {"worker": _ready(
                  launching=True, readiness_seen=False, launch_grace_until=NOW - 1,
                  readiness_fails=3, backoff_next_epoch=0, last_warn_epoch=0,
              )}}, snapshot=[], config=_HOOK_CODEX_CONFIG),
        _plan(_report(heartbeat_stale=True,
                      config_blocked_hold={"summary": r"secret C:\Users\Milos\token"}),
              {"agents": {"worker": _ready()}}, snapshot=[],
              config=_HOOK_CODEX_CONFIG),
        _plan(_report(heartbeat_stale=True,
                      lead_loop_exit={"state": "stood_down"}),
              {"agents": {"worker": _ready()}}, snapshot=[],
              config=_HOOK_CODEX_CONFIG),
        _plan(_report(heartbeat_stale=True,
                      lead_loop_exit={"state": "blocked"},
                      lead_loop={"armed": True}),
              {"agents": {"worker": _ready()}}, snapshot=[],
              config=_HOOK_CODEX_CONFIG),
        _plan(_report(restart_request={"request_id": "rr-forged",
                                       "requested_by": "peer",
                                       "force_protected": True}),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}}),
        _plan(_report(restart_request=_auth_marker("rr-cool")),
              {"agents": {"worker": _ready(last_launch_epoch=NOW - 10)}},
              config={**_CONFIG, "restart_cooldown_seconds": 45},
              snapshot=_snap()),
        _plan(_report(heartbeat_stale=True, health=conflict_health),
              {"agents": {"worker": _ready(last_warn_epoch=0)}}, snapshot=_snap()),
        _plan(_report(), {"agents": {"worker": _ready()}}, snapshot=_snap()),
        _plan(_report(heartbeat_stale=True),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=[],
              config=_HOOK_CODEX_CONFIG),
        _plan(_report(restart_request=_auth_marker("rr-manual")),
              {"agents": {"worker": _ready(backoff_next_epoch=NOW + 9999)}},
              snapshot=_snap()),
    ]

    seen = {(p["action"], p["state"]) for p in cases}
    assert (sup.WARN_ONLY, "STUCK_OR_DEAD") in seen
    assert (sup.SUSPECT_WARN, "ACTIVE_OR_BUSY") in seen
    assert (sup.REFUSE_PROTECTED, "REFUSE_PROTECTED") in seen
    assert (sup.READINESS_GAVE_UP, "READINESS_GAVE_UP") in seen
    assert (sup.NONE, "CONFIG_BLOCKED") in seen
    assert (sup.NONE, "LEAD_LOOP_STOOD_DOWN") in seen
    assert (sup.NONE, "LEAD_LOOP_BLOCKED") in seen
    assert (sup.REFUSE_PROTECTED, "RESTART_UNAUTHORIZED") in seen
    assert (sup.BACKOFF_WAIT, "RESTART_COOLDOWN") in seen
    assert (sup.NONE, "HEALTHY_IDLE") in seen
    assert (sup.STUCK_RECOVER, "STUCK_OR_DEAD") in seen
    assert (sup.RELAUNCH, "MANUAL_RESTART") in seen

    for plan in cases:
        assessment = sup.supervisor_agent_assessment(
            "worker",
            _report()["agents"]["worker"],
            plan,
        )
        assert assessment["decision"]["action"] == plan["action"]
        assert assessment["decision"]["state"] == plan["state"]
        assert assessment["decision"]["reason"] == plan["reason"]

    conflict = next(p for p in cases if "advisory-health-conflict" in p["health"]["warnings"])
    conflict_assessment = sup.supervisor_agent_assessment(
        "worker", _report()["agents"]["worker"], conflict)
    assert "advisory-health-conflict" in conflict_assessment["decision"]["health"]["warnings"]


def test_supervisor_cli_json_decision_matches_plan_actions(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_HOOK_CONFIG), encoding="utf-8")
    (s.state_dir / "worker.heartbeat").write_text(_iso(NOW - 5000), encoding="utf-8")
    state_file = tmp_path / "state.json"
    state = {"agents": {"worker": _ready(session_id="SID")}}
    state_file.write_text(json.dumps(state), encoding="utf-8")
    snap_file = s.dir / "supervisor-snapshot.json"
    snap_file.write_text(json.dumps(_snap(cli="claude")), encoding="utf-8")

    rc = _run([
        "supervisor", "--json", "--state-file", str(state_file),
        "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    expected = payload["plan"]["agents"]["worker"]
    worker = next(a for a in payload["agents"] if a["name"] == "worker")
    assert worker["decision"]["action"] == expected["action"]
    assert worker["decision"]["state"] == expected["state"]
    assert worker["decision"]["reason"] == expected["reason"]
    assert expected["action"] == sup.STUCK_RECOVER


def test_supervisor_cli_json_redacts_embedded_report_config_blocked(
        tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_HOOK_CONFIG), encoding="utf-8")
    secret = "child failed: /secret/path token=sk-FAKE-AGENTTALK-ABC123SECRET"
    s.write_config_blocked_hold("worker", summary=secret)

    rc = _run(["supervisor", "--json", "--now", str(NOW)], tmp_path)
    out = capsys.readouterr().out

    assert rc == 0
    assert "/secret/path" not in out
    assert "sk-FAKE-AGENTTALK-ABC123SECRET" not in out
    payload = json.loads(out)
    worker = next(a for a in payload["agents"] if a["name"] == "worker")
    assert worker["config_blocked_hold"] == {
        "present": True,
        "summary_code": "config_blocked",
    }
    assert payload["report"]["agents"]["worker"]["config_blocked_hold"] == {
        "present": True,
        "summary_code": "config_blocked",
    }


def test_supervisor_event_ring_bounded_redacted_and_transition_only(tmp_path: Path) -> None:
    s = _team(tmp_path)
    for i in range(8):
        sup.record_supervisor_plan_events(
            s,
            {
                "agents": {
                    "worker": {
                        "action": sup.NONE,
                        "state": f"STATE{i}",
                        "reason": r"failed C:\Users\Milos\secret token=sk-FAKE-AGENTTALK-TEST",
                        "notify": bool(i % 2),
                    }
                }
            },
            now_epoch=NOW + i,
            cap=5,
            summary_interval_seconds=999999,
        )

    events, warnings = sup.read_supervisor_events(s)
    raw = sup.supervisor_events_path(s).read_text(encoding="utf-8")
    assert warnings == []
    assert len(events) == 5
    assert "STATE7" in raw
    assert "sk-FAKE-AGENTTALK-TEST" not in raw
    assert r"C:\Users\Milos" not in raw
    assert "failed" not in raw

    before = list(events)
    same = {
        "agents": {
            "worker": {
                "action": sup.NONE,
                "state": "STATE7",
                "reason": "changed secret",
                "notify": True,
            }
        }
    }
    sup.record_supervisor_plan_events(
        s, same, now_epoch=NOW + 100, cap=5, summary_interval_seconds=999999)
    after, _ = sup.read_supervisor_events(s)
    assert after == before


def test_supervisor_event_ring_redacts_config_blocked_summary(tmp_path: Path) -> None:
    s = _team(tmp_path)
    secret = r"Command=C:\Users\Milos\secret-tool.exe error token=sk-FAKE-AGENTTALK-CONFIG"
    sup.record_supervisor_plan_events(
        s,
        {"agents": {"worker": {"action": sup.NONE, "state": "CONFIG_BLOCKED", "reason": secret}}},
        now_epoch=NOW,
    )

    raw = sup.supervisor_events_path(s).read_text(encoding="utf-8")
    assert "CONFIG_BLOCKED" in raw
    assert "sk-FAKE-AGENTTALK-CONFIG" not in raw
    assert r"C:\Users\Milos" not in raw
    assert "secret-tool" not in raw


def test_supervisor_event_ring_reader_sanitizes_polluted_rows(tmp_path: Path) -> None:
    s = _team(tmp_path)
    path = sup.supervisor_events_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = r"C:\Users\Milos\secret-token-sk-FAKE-AGENTTALK-LIVE-PATH"
    polluted = {
        "schema_version": 1,
        "kind": "agent_decision",
        "at": secret,
        "at_epoch": secret,
        "agent": "worker",
        "action": sup.NONE,
        "state": "STATE1",
        "reason": secret,
        "reason_code": secret,
        "notify": False,
        "clear_marker": False,
        "fingerprint": "worker|NONE|STATE1|state1|silent|keep",
        "extra": secret,
    }
    path.write_text(json.dumps(polluted) + "\n", encoding="utf-8")

    events, warnings = sup.read_supervisor_events(s)
    rendered = json.dumps(events)

    assert warnings == ["supervisor_events_sanitized:1"]
    assert "sk-FAKE-AGENTTALK-LIVE-PATH" not in rendered
    assert "reason" not in events[0]
    assert "extra" not in events[0]
    assert events[0]["reason_code"] == "unknown"
    assert events[0]["at"] == "unknown"
    assert events[0]["fingerprint"] == "worker|none|STATE1|unknown|silent|keep"

    observation = sup.build_supervisor_observation(
        s,
        now_epoch=NOW,
        state={},
        supervisor_config=_CONFIG,
        snapshot=[],
        event_limit=10,
    )
    assert "sk-FAKE-AGENTTALK-LIVE-PATH" not in json.dumps(observation["event_ring"])

    sup.record_supervisor_plan_events(
        s,
        {"agents": {"worker": {"action": sup.NONE, "state": "STATE1", "notify": False}}},
        now_epoch=NOW + 1,
        cap=5,
        summary_interval_seconds=999999,
    )
    rewritten = path.read_text(encoding="utf-8")
    after, _warnings = sup.read_supervisor_events(s)
    decisions = [e for e in after if e["kind"] == "agent_decision"]
    assert len(decisions) == 2
    assert "sk-FAKE-AGENTTALK-LIVE-PATH" not in rewritten


def test_supervisor_event_ring_reader_drops_non_finite_epoch(
        tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    path = sup.supervisor_events_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version":1,"kind":"poll_summary",'
        '"at":"2026-07-05T00:00:00Z","at_epoch":NaN,'
        '"planned_agents":1,"healthy_idle":0,"states":{"HEALTHY_IDLE":1}}\n',
        encoding="utf-8",
    )

    events, warnings = sup.read_supervisor_events(s)

    assert warnings == ["supervisor_events_sanitized:1"]
    assert "at_epoch" not in events[0]
    assert json.dumps(events, allow_nan=False)

    rc = _run(["supervisor", "--json", "--now", str(NOW)], s.root)
    out = capsys.readouterr().out
    assert rc == 0
    assert "NaN" not in out
    json.loads(out)

    sup.record_supervisor_plan_events(
        s,
        {"agents": {"worker": {"action": sup.NONE, "state": "HEALTHY_IDLE"}}},
        now_epoch=NOW + 999,
        cap=5,
        summary_interval_seconds=300,
    )
    raw = path.read_text(encoding="utf-8")
    after, _warnings = sup.read_supervisor_events(s)
    summaries = [e for e in after if e["kind"] == "poll_summary"]
    assert any(e.get("at_epoch") == NOW + 999 for e in summaries)
    assert "NaN" not in raw


def test_supervisor_event_ring_torn_read_degrades(tmp_path: Path) -> None:
    s = _team(tmp_path)
    path = sup.supervisor_events_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind":"agent_decision","agent":"worker"}\n{torn\n',
                    encoding="utf-8")

    events, warnings = sup.read_supervisor_events(s)

    assert events == [{
        "schema_version": 1,
        "kind": "agent_decision",
        "at": "unknown",
        "agent": "worker",
        "action": "unknown",
        "state": "unknown",
        "reason_code": "unknown",
        "notify": False,
        "clear_marker": False,
        "fingerprint": "worker|unknown|unknown|unknown|silent|keep",
    }]
    assert warnings == ["supervisor_events_sanitized:1", "supervisor_events_torn:2"]


def test_supervisor_event_write_lock_timeout_is_fail_safe(tmp_path: Path, monkeypatch) -> None:
    s = _team(tmp_path)

    class BusyLock:
        def __enter__(self):
            raise TimeoutError("busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(s, "_config_lock", lambda **_kwargs: BusyLock())

    sup.append_supervisor_events(s, [{"kind": "agent_decision"}])

    assert not sup.supervisor_events_path(s).exists()


def test_supervise_plan_record_events_uses_ring_not_bus(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    report_file = tmp_path / "rpt.json"
    report_file.write_text(json.dumps(_report()), encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    snap_file = tmp_path / "snap.json"
    snap_file.write_text("[]", encoding="utf-8")

    rc = _run([
        "supervise", "--plan", "--record-events", "--report-file", str(report_file),
        "--state-file", str(state_file), "--snapshot-file", str(snap_file),
        "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["agents"]["worker"]["state"] == "HEALTHY_IDLE"
    events, warnings = sup.read_supervisor_events(s)
    assert warnings == []
    assert events
    assert s.all_messages() == []


def test_supervise_plan_prints_when_record_events_raises(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _team(tmp_path)
    (tmp_path / ".agenttalk" / "supervisor.json").write_text(
        json.dumps(_CONFIG), encoding="utf-8")
    report_file = tmp_path / "rpt.json"
    report_file.write_text(json.dumps(_report()), encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    snap_file = tmp_path / "snap.json"
    snap_file.write_text("[]", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("recording side channel failed")

    monkeypatch.setattr(cli.sup, "record_supervisor_plan_events", boom)

    rc = _run([
        "supervise", "--plan", "--record-events", "--report-file", str(report_file),
        "--state-file", str(state_file), "--snapshot-file", str(snap_file),
        "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["agents"]["worker"]["state"] == "HEALTHY_IDLE"


_HOOK_CONFIG = {
    "root": TEST_ROOT,
    "agents": {"worker": {"auto_restart": True, "activity_hook": True, "cli": "claude"}},
    "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
    "launch_grace_seconds": 120,
}


def _plan_hook(report, state, *, now=NOW, config=_HOOK_CONFIG, snapshot=None):
    snap = [] if snapshot is None else snapshot
    return sup.plan_actions(report, state, config, now_epoch=now,
                            snapshot=snap)["agents"]["worker"]


# ---------------------------------------- WP-3: stuck-recovery matrix

def test_stuck_recover_when_alive_stale_and_hook_on() -> None:
    # stale heartbeat + hook -> STUCK_RECOVER (resume the pinned session).
    # _ready() makes resume_available true.
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": _ready(session_id="SID")}},
                   snapshot=_snap(cli="claude"))
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"
    assert p["kill_first"] is True            # best-effort cleanup before resume
    assert p["launch_mode"] == "resume"
    # unattended default permission mode (blocker #2): bypassPermissions on resume
    assert p["session_args"] == ["--resume", "SID", "--permission-mode",
                                 "bypassPermissions", "--allowedTools",
                                 "Bash(agenttalk *)", "-p", "/agenttalk.listen"]
    assert p["next_state"]["consecutive_fails"] == 1   # backoff applies


def test_stuck_alive_fresh_is_none() -> None:
    p = _plan_hook(_report(heartbeat_stale=False),
                   {"agents": {"worker": _ready()}}, snapshot=_snap(cli="claude"))
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"


def test_stuck_protected_is_warn_only() -> None:
    p = _plan_hook(_report(protected=True, heartbeat_stale=True),
                   {"agents": {"worker": _ready()}}, snapshot=_snap(cli="claude"))
    assert p["action"] == sup.WARN_ONLY
    assert p["notify"] is True               # never kill a protected human channel


def test_stuck_dead_pid_is_relaunch_not_stuck() -> None:
    # stale heartbeat routes to stuck recovery even when no brain is discovered.
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": _ready()}}, snapshot=[])
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"


def test_stuck_recover_requires_hook_else_suspect() -> None:
    cfg = {"agents": {"worker": {"auto_restart": True, "activity_hook": False,
                                 "cli": "codex"}}}
    p = sup.plan_actions(_report(heartbeat_stale=True),
                         {"agents": {"worker": _ready(last_warn_epoch=0)}},
                         cfg, now_epoch=NOW, snapshot=_snap())["agents"]["worker"]
    assert p["action"] == sup.SUSPECT_WARN   # no hook -> never kill


# ---------------------------------------- WP-3: session-id lifecycle + args

def test_session_id_fresh_then_resume() -> None:
    # no session/resume yet -> fresh launch (token list still carries {SESSION_ID}).
    # A brain that reached the listen loop but is now stuck, never resumed before.
    base = {"brain_pid": BRAIN_PID, "brain_start": BRAIN_START, "readiness_seen": True}
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": dict(base)}}, snapshot=_snap(cli="claude"))
    assert p["launch_mode"] == "fresh"
    assert p["session_id"] is None
    assert "{SESSION_ID}" in p["session_args"]            # token list
    # once the script has pinned a session_id -> resume reuses it
    p2 = _plan_hook(_report(heartbeat_stale=True),
                    {"agents": {"worker": dict(base, session_id="abc-123")}},
                    snapshot=_snap(cli="claude"))
    assert p2["launch_mode"] == "resume"
    assert "abc-123" in p2["session_args"]


def test_session_args_per_cli_explicit_skill() -> None:
    # Claude: explicit /agenttalk.listen as a SINGLE token, both modes; the
    # unattended permission mode (blocker #2) is on BOTH fresh and resume, from
    # the {PERM_MODE} token (default bypassPermissions).
    assert sup.session_args("claude", "fresh", None) == [
        "--session-id", "{SESSION_ID}", "--permission-mode", "bypassPermissions",
        "-p", "/agenttalk.listen"]
    assert sup.session_args("claude", "resume", "X") == [
        "--resume", "X", "--permission-mode", "bypassPermissions",
        "--allowedTools", "Bash(agenttalk *)", "-p", "/agenttalk.listen"]
    # the perm mode is configurable
    assert "dontAsk" in sup.session_args("claude", "fresh", None, perm_mode="dontAsk")
    # Codex (blocker 1): NO fake --session-id on fresh; resume via --last; the
    # EXPLICIT $agenttalk-listen skill is a SINGLE token in both modes.
    cfresh = sup.session_args("codex", "fresh", None)
    assert "--session-id" not in cfresh
    assert cfresh[-1] == "$agenttalk-listen"
    cresume = sup.session_args("codex", "resume", None)
    assert cresume[:2] == ["resume", "--last"] and cresume[-1] == "$agenttalk-listen"
    # per-agent override wins (token list)
    over = {"session": {"resume": ["--resume", "{SESSION_ID}", "--yolo"]}}
    assert sup.session_args("claude", "resume", "Z", over) == ["--resume", "Z", "--yolo"]


def test_config_launch_is_file_args_not_monitorable_cmd() -> None:
    """Launch-layer rework: the config exposes a real-exe windows_file + a
    windows_args ARRAY with the {SESSION_ARGS} splice element — NOT a
    Start-Process expression, and NEVER a monitorable .cmd shim (a shim hands
    off + exits -> relaunch storm)."""
    agent = json.loads(sup.CONFIG_TEMPLATE)["agents"]["AGENT_NAME"]
    launch = agent["launch"]
    assert "windows_file" in launch and "windows" not in launch  # new shape
    assert "{SESSION_ARGS}" in launch["windows_args"]            # splice element
    assert ".cmd" not in launch["windows_file"]                 # not a shim pid
    assert "REAL CLI executable" in agent["_comment_launch"]
    assert ".cmd" in agent["_comment_launch"]                   # warns against it


def test_codex_relaunch_command_uses_codex_args() -> None:
    cfg = {"agents": {"c": {"auto_restart": True, "activity_hook": True, "cli": "codex"}}}
    rpt = {"agents": {"c": {"protected": False, "heartbeat_stale": True,
                            "heartbeat_age_seconds": 999.0, "restart_request": None}}}
    snap = [{"pid": BRAIN_PID, "parent_pid": LAUNCHER_PID, "name": "codex.exe",
             "command_line": "codex", "start_time": BRAIN_START},
            {"pid": WAIT_PID, "parent_pid": BRAIN_PID, "name": "python.exe",
             "command_line": "agenttalk wait --for c", "start_time": "t-wait"}]
    # codex has no pinned session_id; resume_available drives resume-by-last
    st = {"agents": {"c": {"brain_pid": BRAIN_PID, "brain_start": BRAIN_START,
                           "readiness_seen": True, "resume_available": True}}}
    p = sup.plan_actions(rpt, st, cfg, now_epoch=NOW, snapshot=snap)["agents"]["c"]
    assert p["cli"] == "codex" and p["launch_mode"] == "resume"
    # activity_hook=true codex -> the GLOBAL --dangerously-bypass-hook-trust is
    # prepended (before the `resume` subcommand) so the changed hook hash does not
    # strand the unattended launch (0.31.1); the rest is the codex resume args.
    assert p["session_args"] == ["--dangerously-bypass-hook-trust", "resume", "--last",
                                 "-a", "never", "-s", "workspace-write", "$agenttalk-listen"]
    assert "--session-id" not in p["session_args"]


def test_agenttalk_shim_resolves_both_install_modes() -> None:
    """The generated shim makes a bare `agenttalk` resolve for BOTH a source
    checkout (prepend <root>/src to PYTHONPATH) and a pip install (python -m),
    using a known Python (no console-script PATH discovery)."""
    shim = sup.agenttalk_shim(r"C:\py\python.exe")
    assert "-m agenttalk %*" in shim                       # pip-install path
    assert 'AGENTTALK_PYTHON=C:\\py\\python.exe' in shim    # known python baked
    assert "src\\agenttalk\\__init__.py" in shim           # source-checkout guard
    assert "PYTHONPATH" in shim


def test_ps_template_applies_and_restores_env() -> None:
    """The executor applies the agent env (AGENTTALK_ROOT + AGENTTALK_PY +
    PYTHONPATH-src + per-agent env + CODEX_HOME) around Start-Process and
    RESTORES the supervisor's own env afterward. AGENTTALK_PYTHON stays
    supervisor-only for the shim."""
    ps = sup.PS_TEMPLATE
    assert "$applied = @{ AGENTTALK_ROOT = $Root; AGENTTALK_PY = $AgenttalkPython }" in ps
    assert "$a.env" in ps                                  # applies per-agent env
    assert "'src') + ';' + $env:PYTHONPATH" in ps          # src on PYTHONPATH for module import
    assert "finally" in ps                                 # restore in a finally
    assert "Remove-Item -Path (\"Env:\"" in ps             # restore: unset what wasn't set
    assert "Invoke-Expression" not in ps
    # Launch wires Quote-Arg -> a single joined command-line -> Start-Process
    # (BLOCKER 2): the raw $argv array must NEVER go straight to -ArgumentList,
    # or a token with a space splits in two at the handoff.
    assert "function Quote-Arg" in ps
    assert "$argline = (@($argv) | ForEach-Object { Quote-Arg" in ps
    assert "-ArgumentList $argline" in ps
    assert "-ArgumentList $argv" not in ps


def test_ps_template_applies_resolved_window_style_to_all_launches() -> None:
    ps = sup.PS_TEMPLATE
    assert ps.count("-WindowStyle $windowStyle") == 4
    assert "$windowStyle = if ($plan.window_style)" in ps
    assert "$windowStyle = if ($spec.window_style)" in ps
    assert "AGENTTALK_NO_CHILD_WINDOW" in ps


def test_ps_template_uses_utc_epoch_for_now() -> None:
    """The `--now` epoch passed to `supervise --plan` MUST be UTC: heartbeats are
    stamped in UTC and the plan compares now-vs-heartbeat for staleness. On
    Windows PowerShell 5.1 `Get-Date -UFormat %s` returns a LOCAL-time epoch, so
    on a non-UTC machine `$now` is skewed by the TZ offset and every heartbeat
    looks stale -> the supervisor false-kills healthy agents (the UTC+2 / 7201s
    skew that broke live test #6). Guard the locale-independent UTC form."""
    ps = sup.PS_TEMPLATE
    assert "$now = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()" in ps
    # the local-time trap must not be the actual now-assignment (it may still be
    # named in an explanatory comment, so ban the ASSIGNMENT form, not the phrase)
    assert "Parse((Get-Date -UFormat %s))" not in ps


def test_record_launch_codex_marks_launched_no_fake_id(tmp_path: Path) -> None:
    """Blocker 2/launch-state: the SCRIPT-side launch-success rule (via the
    --record-launch command) — Codex gets launched=true + NO pinned id; Claude
    pins the minted id. Tested through the real command, not a hand-injected
    planner input."""
    _team(tmp_path)
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    # codex launch success: no --session-id passed
    assert _run(["supervise", "--record-launch", "--for", "worker", "--cli", "codex",
                 "--pid", "777", "--state-file", str(sf)], tmp_path) == 0
    e = json.loads(sf.read_text(encoding="utf-8"))["agents"]["worker"]
    assert e["pid"] == 777 and e["launched"] is True and e["session_id"] is None
    # claude launch success: minted id pinned
    sf.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    assert _run(["supervise", "--record-launch", "--for", "worker", "--cli", "claude",
                 "--pid", "888", "--session-id", "sess-x", "--state-file", str(sf)],
                tmp_path) == 0
    e2 = json.loads(sf.read_text(encoding="utf-8"))["agents"]["worker"]
    assert e2["pid"] == 888 and e2["launched"] is True and e2["session_id"] == "sess-x"


def test_state_round_trip_preserves_supervisor_fields(tmp_path: Path) -> None:
    """BLOCKER-3 (new schema): a healthy/no-op tick must NOT drop the
    supervisor-owned discovery/liveness fields - else the next loop loses
    brain_pid/readiness/resume_available and mis-classifies a HEALTHY agent."""
    st = {"agents": {"worker": _ready(
        session_id="sess-9",
        managed_pids=[{"pid": WAIT_PID, "start": "t-wait", "kind": "wait", "last_seen": 0}])}}
    p = _plan_hook(_report(heartbeat_stale=False), st, snapshot=_snap(cli="claude"))
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"
    ns = p["next_state"]
    assert ns["brain_pid"] == BRAIN_PID and ns["brain_start"] == BRAIN_START
    assert ns["readiness_seen"] is True and ns["resume_available"] is True
    assert ns["session_id"] == "sess-9"
    assert ns["launching"] is False


# ---------------------------------------- liveness redesign: snapshot matrix

def test_launcher_exits_brain_lives_is_healthy() -> None:
    """THE core regression for the forking-launcher bug: the launcher pid is GONE
    from the snapshot, but the long-lived brain (discovered by climbing the
    parent chain from the agent's live `wait`) is alive + ready -> HEALTHY_IDLE,
    NOT a relaunch. The old single-pid model stormed here."""
    snap = [  # NOTE: no launcher pid 199 present - it exited after handoff
        {"pid": BRAIN_PID, "parent_pid": 1, "name": "codex.exe",
         "command_line": "codex", "start_time": BRAIN_START},
        {"pid": 300, "parent_pid": BRAIN_PID, "name": "codex-command-runner.exe",
         "command_line": "runner", "start_time": "t3"},
        {"pid": WAIT_PID, "parent_pid": 300, "name": "python.exe",
         "command_line": "agenttalk wait --for worker", "start_time": "t-wait"}]
    # state knows only the (dead) launcher - brain not yet recorded; in grace.
    st = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                "launch_grace_until": NOW + 100, "readiness_seen": False}}}
    p = _plan(_report(heartbeat_stale=False), st, snapshot=snap)
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"
    # brain was DISCOVERED + readiness cleared launching in the same tick
    assert p["next_state"]["brain_pid"] == BRAIN_PID
    assert p["next_state"]["readiness_seen"] is True
    assert p["next_state"]["launching"] is False


def test_zombie_wait_reaps_orphans_then_relaunches() -> None:
    # Fresh heartbeat wins over a missing brain even when a wait row exists.
    snap = [{"pid": WAIT_PID, "parent_pid": 1, "name": "python.exe",
             "command_line": "agenttalk wait --for worker", "start_time": "t-wait"}]
    p = _plan(_report(heartbeat_stale=False),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=snap)
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"


def test_in_grace_launcher_dead_is_none_no_brain_yet() -> None:
    # still in grace, brain not found yet, launcher already dead -> LAUNCHING
    # (NOT a failure): launcher death during grace is expected.
    st = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                "launch_grace_until": NOW + 100, "readiness_seen": False}}}
    p = _plan(_report(heartbeat_stale=True), st, snapshot=[])
    assert p["action"] == sup.NONE and p["state"] == "LAUNCHING"
    assert p["discover_brain"] is True


def test_no_brain_by_grace_expiry_relaunches() -> None:
    st = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                "launch_grace_until": NOW - 1, "readiness_seen": False,
                                "backoff_next_epoch": 0}}}
    p = _plan(_report(heartbeat_stale=True), st, snapshot=[],
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"
    assert p["next_state"]["consecutive_fails"] == 1


def test_readiness_failed_after_grace_kills_and_relaunches() -> None:
    # no first heartbeat after grace -> stale heartbeat recovery; brain presence
    # only contributes kill targets.
    st = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                "launch_grace_until": NOW - 1, "readiness_seen": False,
                                "backoff_next_epoch": 0}}}
    p = _plan(_report(heartbeat_stale=True), st, snapshot=_snap(),
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"
    assert p["kill_first"] is True and p["kill_targets"]


def test_pid_reuse_start_mismatch_is_not_alive() -> None:
    # A recycled/mismatched brain pid no longer drives liveness; fresh heartbeat
    # keeps the agent healthy.
    snap = [{"pid": BRAIN_PID, "parent_pid": 1, "name": "notcodex.exe",
             "command_line": "something-else", "start_time": "DIFFERENT"}]
    p = _plan(_report(heartbeat_stale=False),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=snap)
    assert p["state"] == "HEALTHY_IDLE" and p["action"] == sup.NONE


def test_snapshot_unavailable_does_not_block_heartbeat_liveness() -> None:
    # Snapshot failure only reduces kill-target quality; heartbeat staleness
    # remains authoritative.
    p = sup.plan_actions(_report(heartbeat_stale=True),
                         {"agents": {"worker": _ready(last_warn_epoch=0)}},
                         _HOOK_CODEX_CONFIG,
                         now_epoch=NOW, snapshot=None)["agents"]["worker"]
    assert p["state"] == "STUCK_OR_DEAD"
    assert p["action"] == sup.STUCK_RECOVER


def test_non_forking_cli_uses_heartbeat_when_no_snapshot() -> None:
    # Even legacy/non-forking configs now use heartbeat freshness for liveness.
    cfg = {"agents": {"worker": {"auto_restart": True, "cli": "claude",
                                 "requires_brain_pid": False,
                                 "activity_hook": True}},
           "launch_grace_seconds": 120}
    dead = sup.plan_actions(_report(heartbeat_stale=True),
                            {"agents": {"worker": {"pid_alive": False,
                                                   "backoff_next_epoch": 0}}},
                            cfg, now_epoch=NOW, snapshot=None)["agents"]["worker"]
    assert dead["action"] == sup.STUCK_RECOVER and dead["state"] == "STUCK_OR_DEAD"
    alive = sup.plan_actions(_report(), {"agents": {"worker": {"pid_alive": True}}},
                             cfg, now_epoch=NOW, snapshot=None)["agents"]["worker"]
    assert alive["action"] == sup.NONE and alive["state"] == "HEALTHY_IDLE"


def test_resume_mode_fresh_then_last() -> None:
    # first launch (never reached readiness, no resume_available) -> fresh; after
    # readiness has been reached -> resume --last.
    st_fresh = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                      "launch_grace_until": NOW - 1, "readiness_seen": False,
                                      "backoff_next_epoch": 0}}}
    p = _plan(_report(heartbeat_stale=True), st_fresh, snapshot=[],
              config=_HOOK_CODEX_CONFIG)
    assert p["resume_mode"] == "fresh"
    p2 = _plan(_report(heartbeat_stale=True),
               {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=[],
               config=_HOOK_CODEX_CONFIG)
    assert p2["state"] == "STUCK_OR_DEAD" and p2["resume_mode"] == "last"


# -------------------------- impl-review r1 regressions (codex findings) ------

def test_brain_discovery_with_null_command_lines(tmp_path: Path) -> None:
    """impl-review BLOCKER 1: discovery must work by name + ancestry even when
    `command_line` is null AND the launcher row has exited (partial snapshot).
    codex.exe(200,parent=199) + wait(400,parent=200), both command_line=None,
    launcher 199 gone -> brain 200 discovered, readiness clears on fresh hb."""
    snap = [{"pid": BRAIN_PID, "parent_pid": LAUNCHER_PID, "name": "codex.exe",
             "command_line": None, "start_time": BRAIN_START},
            {"pid": WAIT_PID, "parent_pid": BRAIN_PID, "name": "python.exe",
             "command_line": None, "start_time": "t-wait"}]
    st = {"agents": {"worker": {"launcher_pid": LAUNCHER_PID, "launching": True,
                                "launch_grace_until": NOW + 100, "readiness_seen": False}}}
    p = _plan(_report(heartbeat_stale=False), st, snapshot=snap)
    assert p["state"] == "HEALTHY_IDLE"
    assert p["next_state"]["brain_pid"] == BRAIN_PID
    assert p["next_state"]["readiness_seen"] is True


# --------------------- test #4: never pin the forking LAUNCHER as the brain ---

def _codex_tree(*, launcher=True, order_launcher_first=True):
    """A real forking-codex process tree: launcher codex.exe(199) -> TUI
    codex.exe(200) -> codex-command-runner.exe(300) -> python `wait`(400)."""
    L = {"pid": LAUNCHER_PID, "parent_pid": 1, "name": "codex.exe",
         "command_line": "codex launcher", "start_time": "t1"}
    T = {"pid": BRAIN_PID, "parent_pid": LAUNCHER_PID, "name": "codex.exe",
         "command_line": "codex tui", "start_time": BRAIN_START}
    R = {"pid": 300, "parent_pid": BRAIN_PID, "name": "codex-command-runner.exe",
         "command_line": "runner", "start_time": "t3"}
    W = {"pid": WAIT_PID, "parent_pid": 300, "name": "python.exe",
         "command_line": "agenttalk wait --for codex-test", "start_time": "t-wait"}
    rows = ([L] if (launcher and order_launcher_first) else []) + [T, R, W]
    if launcher and not order_launcher_first:
        rows.append(L)
    return rows


def _codex_cfg():
    return {"agents": {"codex-test": {"cli": "codex", "auto_restart": True,
                                      "activity_hook": True}},
            "launch_grace_seconds": 120, "stuck_after_seconds": 120}


def _codex_rpt(stale=False):
    return {"agents": {"codex-test": {"protected": False, "heartbeat_stale": stale,
                                      "heartbeat_age_seconds": 9999.0 if stale else 1.0,
                                      "restart_request": None}}}


def test_discover_brain_picks_tui_not_launcher() -> None:
    """test #4 root cause: _discover_brain must pick the long-lived TUI (200),
    NEVER the forking launcher (199, which exits after handoff). The
    codex-command-runner.exe (300, also matches the 'codex' substring) is below
    the TUI so it never wins either."""
    idx = sup._snap_index(_codex_tree())
    b = sup._discover_brain(idx, "codex-test", LAUNCHER_PID, "codex",
                            allow_launcher_self=False)
    assert b["pid"] == BRAIN_PID                      # the TUI, not 199 / not 300


def test_discover_brain_tui_wins_even_when_launcher_iterates_first() -> None:
    idx = sup._snap_index(_codex_tree(order_launcher_first=True))
    b = sup._discover_brain(idx, "codex-test", LAUNCHER_PID, "codex",
                            allow_launcher_self=False)
    assert b["pid"] == BRAIN_PID
    # and after the launcher has EXITED, the TUI is still found (parent_pid persists)
    idx2 = sup._snap_index(_codex_tree(launcher=False))
    assert sup._discover_brain(idx2, "codex-test", LAUNCHER_PID, "codex",
                               allow_launcher_self=False)["pid"] == BRAIN_PID


def test_allow_launcher_self_true_picks_launcher() -> None:
    """A non-forking CLI (allow_launcher_self=true) still selects the launcher as
    its own brain (claude.exe) - the codex exclusion must not regress it."""
    idx = sup._snap_index(_codex_tree())
    b = sup._discover_brain(idx, "codex-test", LAUNCHER_PID, "codex",
                            allow_launcher_self=True)
    assert b["pid"] == LAUNCHER_PID


def test_stale_launcher_pin_repairs_to_tui_not_zombie() -> None:
    """test #4 false-kill: a stored brain_pid == launcher_pid (a bad pin from the
    old discovery) must be REPAIRED to the real TUI while a fresh heartbeat stays
    healthy. Holds whether the launcher is still alive or already exited."""
    for launcher_alive in (True, False):
        st = {"agents": {"codex-test": {
            "brain_pid": LAUNCHER_PID, "brain_start": "t1", "launcher_pid": LAUNCHER_PID,
            "readiness_seen": True, "resume_available": True, "launching": False,
            "launch_grace_until": NOW - 1}}}
        p = sup.plan_actions(_codex_rpt(stale=False), st, _codex_cfg(),
                             now_epoch=NOW,
                             snapshot=_codex_tree(launcher=launcher_alive))["agents"]["codex-test"]
        assert p["state"] == "HEALTHY_IDLE" and p["action"] == sup.NONE, launcher_alive
        assert p["next_state"]["brain_pid"] == BRAIN_PID         # repaired to the TUI
        assert p["kill_targets"] == []                           # nothing killed


def test_allow_launcher_self_default_is_false_for_codex() -> None:
    assert sup._liveness_cfg({"cli": "codex"})["allow_launcher_self"] is False
    assert sup._liveness_cfg({"cli": "claude"})["allow_launcher_self"] is True
    # explicit per-agent override wins
    assert sup._liveness_cfg({"cli": "codex", "allow_launcher_self": True})[
        "allow_launcher_self"] is True


def test_stale_heartbeat_reaps_carried_managed_pid_null_cmdline(tmp_path: Path) -> None:
    """A versioned start-matching managed pid with a now-null command line is
    still a safe cleanup target when heartbeat staleness triggers recovery."""
    snap = [{"pid": WAIT_PID, "parent_pid": 1, "name": "python.exe",
             "command_line": None, "start_time": WAIT_START}]
    prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": WAIT_PID,
        "start": WAIT_START,
        "source": "first_confirmed_child_provenance",
        "captured_at_epoch": NOW - 1,
        "last_fresh_attribution_epoch": NOW - 1,
        "seed_descendants": False,
    }
    st = {"agents": {"worker": _ready(
        backoff_next_epoch=0,
        managed_pids=[prior])}}
    p = _plan(_report(heartbeat_stale=True), st, snapshot=snap,
              config=_HOOK_CODEX_CONFIG)
    assert p["state"] == "STUCK_OR_DEAD" and p["kill_orphans"] is True
    assert WAIT_PID in [t["pid"] for t in p["kill_targets"]]


def test_codex_failed_first_launch_is_fresh_not_resume() -> None:
    """impl-review MAJOR 1: codex resume must be driven by resume_available, NOT
    legacy `launched` (set at launch time, before readiness). A failed first
    launch (launched=true, resume_available=false) must relaunch FRESH."""
    cfg = {"agents": {"worker": {"cli": "codex", "auto_restart": True,
                                 "activity_hook": True}},
           "launch_grace_seconds": 120}
    st = {"agents": {"worker": {"launcher_pid": LAUNCHER_PID, "launching": True,
                                "launch_grace_until": NOW - 1, "readiness_seen": False,
                                "resume_available": False, "launched": True,
                                "backoff_next_epoch": 0}}}
    p = sup.plan_actions(_report(heartbeat_stale=True), st, cfg,
                         now_epoch=NOW, snapshot=[])["agents"]["worker"]
    assert p["state"] == "STUCK_OR_DEAD" and p["resume_mode"] == "fresh"
    assert "--last" not in (p["session_args"] or [])


def test_effective_codex_home_isolation_emitted_in_plan() -> None:
    """impl-review MAJOR 2: the plan must carry the EFFECTIVE isolation flag (the
    per-CLI default merged), so the executor seeds CODEX_HOME exactly when the
    planner assumed it - even if the raw config omits the field."""
    codex_cfg = {"agents": {"worker": {"cli": "codex", "auto_restart": True}}}
    pc = sup.plan_actions(_report(), {"agents": {"worker": {}}}, codex_cfg,
                          now_epoch=NOW, snapshot=[])["agents"]["worker"]
    assert pc["codex_home_isolation"] is True       # codex default true
    claude_cfg = {"agents": {"worker": {"cli": "claude", "auto_restart": True}}}
    pl = sup.plan_actions(_report(), {"agents": {"worker": {}}}, claude_cfg,
                          now_epoch=NOW, snapshot=[])["agents"]["worker"]
    assert pl["codex_home_isolation"] is False      # claude default false
    # an explicit override wins
    off_cfg = {"agents": {"worker": {"cli": "codex", "auto_restart": True,
                                     "codex_home_isolation": False}}}
    po = sup.plan_actions(_report(), {"agents": {"worker": {}}}, off_cfg,
                          now_epoch=NOW, snapshot=[])["agents"]["worker"]
    assert po["codex_home_isolation"] is False


# ---------------------------------------- 0.28.1 blocker #2: unattended auto-mode

def test_codex_config_overlay_sets_keys_preserves_others_idempotent() -> None:
    # (d) unrelated sections / comments / quoting preserved verbatim; (b) an
    # existing [windows] WITH a sibling key -> set sandbox in-place, keep sibling.
    existing = ('# operator notes\nmodel = "gpt-5"\napproval_policy = "on-request"\n\n'
                '[windows]\nfoo = 1\n\n[mcp_servers.fs]\ncommand = "node"\n')
    out = sup.codex_config_overlay(existing, repo_path=r"C:\proj\agenttalk",
                                   windows_sandbox="unelevated")
    assert "# operator notes" in out                      # comment preserved
    assert 'model = "gpt-5"' in out                       # operator key preserved
    assert "[mcp_servers.fs]" in out and 'command = "node"' in out  # unrelated section kept
    assert 'approval_policy = "never"' in out             # managed key set...
    assert 'approval_policy = "on-request"' not in out    # ...replacing the old value
    assert 'sandbox_mode = "workspace-write"' in out
    assert 'foo = 1' in out                               # sibling [windows] key kept
    assert 'sandbox = "unelevated"' in out                # the UAC fix
    # (e) writable_roots is a DOUBLE-QUOTED path with escaped backslashes
    assert r'writable_roots = ["C:\\proj\\agenttalk"]' in out
    assert out.count("[windows]") == 1                    # no duplicate table
    # (c) idempotent re-apply -> identical, no dupes
    assert sup.codex_config_overlay(out, repo_path=r"C:\proj\agenttalk",
                                    windows_sandbox="unelevated") == out
    # (a) empty/no config.toml -> created with ALL managed keys + both tables
    fresh = sup.codex_config_overlay("", repo_path="C:\\x", windows_sandbox="elevated")
    assert 'approval_policy = "never"' in fresh and 'sandbox_mode = "workspace-write"' in fresh
    assert 'sandbox = "elevated"' in fresh and "[sandbox_workspace_write]" in fresh
    assert "AGENTTALK_PY" not in out
    assert "shell_environment_policy" not in out


def test_seed_claude_settings_merges_default_mode() -> None:
    out = sup.seed_claude_settings('{"model": "opus"}', mode="bypassPermissions")
    data = json.loads(out)
    assert data == {"model": "opus", "defaultMode": "bypassPermissions"}
    # empty / malformed -> minimal valid
    assert json.loads(sup.seed_claude_settings(None))["defaultMode"] == "bypassPermissions"
    assert json.loads(sup.seed_claude_settings("{bad json"))["defaultMode"] == "bypassPermissions"


def test_claude_permission_mode_resolution() -> None:
    assert sup.claude_permission_mode({}, {}) == "bypassPermissions"          # default
    assert sup.claude_permission_mode({"claude_permission_mode": "plan"}, {}) == "plan"  # top
    assert sup.claude_permission_mode({"claude_permission_mode": "plan"},
                                      {"claude_permission_mode": "dontAsk"}) == "dontAsk"  # per-agent wins


def test_window_style_resolution_default_global_agent_and_invalid() -> None:
    assert sup.resolve_window_style({}, {}) == ("Hidden", None)
    assert sup.resolve_window_style({"window_style": "normal"}, {}) == ("Normal", None)
    assert sup.resolve_window_style(
        {"window_style": "normal"}, {"window_style": "minimized"}) == ("Minimized", None)

    style, warning = sup.resolve_window_style(
        {"window_style": "normal"}, {"window_style": "maximized"})
    assert style == "Hidden"
    assert warning is not None and "invalid per-agent window_style" in warning
    assert "defaulting to hidden" in warning


def test_plan_carries_resolved_window_style_and_visible_warning() -> None:
    cfg = {
        "window_style": "normal",
        "agents": {"worker": {"auto_restart": True, "activity_hook": True,
                              "cli": "codex", "window_style": "minimized"}},
    }
    p = _plan(_report(heartbeat_stale=True),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}},
              config=cfg)
    assert p["action"] == sup.STUCK_RECOVER
    assert p["window_style"] == "Minimized"
    assert p["window_style_warning"] is None

    bad_cfg = {"window_style": "sideways",
               "agents": {"worker": {"auto_restart": True, "activity_hook": True,
                                      "cli": "codex"}}}
    p_bad = _plan(_report(heartbeat_stale=True),
                  {"agents": {"worker": _ready(backoff_next_epoch=0)}},
                  config=bad_cfg)
    assert p_bad["window_style"] == "Hidden"
    assert "invalid global window_style" in p_bad["window_style_warning"]


def test_seed_codex_config_cli_overlays_in_place(tmp_path: Path) -> None:
    _team(tmp_path)
    home = tmp_path / "iso"
    home.mkdir()
    (home / "config.toml").write_text('model = "x"\n', encoding="utf-8")
    rc = _run(["supervise", "--seed-codex-config", "--home", str(home),
               "--repo", str(tmp_path), "--sandbox", "unelevated"], tmp_path)
    assert rc == 0
    txt = (home / "config.toml").read_text(encoding="utf-8")
    assert 'model = "x"' in txt and 'approval_policy = "never"' in txt
    assert 'sandbox = "unelevated"' in txt


def test_seed_claude_settings_cli_merges(tmp_path: Path) -> None:
    _team(tmp_path)
    d = tmp_path / "launch"
    d.mkdir()
    (d / ".claude").mkdir()
    (d / ".claude" / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    rc = _run(["supervise", "--seed-claude-settings", "--dir", str(d),
               "--mode", "bypassPermissions"], tmp_path)
    assert rc == 0
    data = json.loads((d / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["model"] == "opus" and data["defaultMode"] == "bypassPermissions"


def test_supervise_bootstrap_check_accepts_wrapped_claude_and_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno,Ramanujan")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno", "Ramanujan"):
        s.write_heartbeat(name)
    _write_supervisor_config(s, {
        "Zeno": _wrapped_supervisor_agent("Zeno", "codex"),
        "Ramanujan": _wrapped_supervisor_agent("Ramanujan", "claude"),
    })

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "ok"
    assert payload["supported_clis"] == ["claude", "codex"]
    assert not [c for c in payload["checks"] if c["status"] == "error"]
    by_agent_cli = {
        (c.get("agent"), c.get("facts", {}).get("cli"))
        for c in payload["checks"]
        if c["id"] == "supervisor_agent_cli_supported"
    }
    assert ("Zeno", "codex") in by_agent_cli
    assert ("Ramanujan", "claude") in by_agent_cli


def test_supervise_bootstrap_check_warns_on_roster_only_placeholders(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno,claude,codex")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno"):
        s.write_heartbeat(name)
    _write_supervisor_config(s, {
        "Zeno": _wrapped_supervisor_agent("Zeno", "codex"),
    })

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "warn"
    stale_roster = {
        c.get("agent") for c in payload["checks"]
        if c["id"] == "roster_identity_not_live"
    }
    assert {"claude", "codex"} <= stale_roster


def test_supervise_bootstrap_check_errors_on_stale_managed_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    s.write_heartbeat("Polaris")
    _write_supervisor_config(s, {
        "Zeno": _wrapped_supervisor_agent("Zeno", "codex"),
    })

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "error"
    assert any(
        c["id"] == "supervisor_agent_not_fresh" and c.get("agent") == "Zeno"
        for c in payload["checks"]
    )


def test_supervise_bootstrap_check_requires_explicit_root_for_wrapped_claude_and_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Cygnus,Altair")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Cygnus", "Altair"):
        s.write_heartbeat(name)
    misplaced_claude = _wrapped_supervisor_agent(
        "Cygnus", "claude", explicit_root=False)
    misplaced_args = misplaced_claude["launch"]["windows_args"]
    wrap_pos = misplaced_args.index("wrap") + 1
    misplaced_args[wrap_pos:wrap_pos] = ["--root", "{ROOT}"]
    _write_supervisor_config(s, {
        "Cygnus": misplaced_claude,
        "Altair": _wrapped_supervisor_agent("Altair", "codex", explicit_root=False),
    })

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    missing_root_agents = {
        c.get("agent") for c in payload["checks"]
        if c["id"] == "supervisor_wrapped_missing_root"
    }
    assert missing_root_agents == {"Cygnus", "Altair"}


def test_plan_emits_windows_sandbox_and_perm_mode() -> None:
    cfg = {"agents": {"worker": {"cli": "codex", "auto_restart": True,
                                 "windows_sandbox": "elevated"}},
           "claude_permission_mode": "dontAsk"}
    p = sup.plan_actions(_report(), {"agents": {"worker": {}}}, cfg,
                         now_epoch=NOW, snapshot=[])["agents"]["worker"]
    assert p["windows_sandbox"] == "elevated"     # per-agent override emitted
    assert p["perm_mode"] == "dontAsk"            # top-level knob emitted


def test_claude_session_args_carry_bypass_permissions_fresh_and_resume() -> None:
    cfg = {"agents": {"c": {"cli": "claude", "auto_restart": True, "activity_hook": True}}}
    rpt = {"agents": {"c": {"protected": False, "heartbeat_stale": True,
                            "heartbeat_age_seconds": 999.0, "restart_request": None}}}
    # a claude agent that reached readiness + went stuck -> resume w/ perm mode
    st = {"agents": {"c": {"brain_pid": BRAIN_PID, "brain_start": BRAIN_START,
                           "readiness_seen": True, "resume_available": True,
                           "session_id": "SID"}}}
    snap = [{"pid": BRAIN_PID, "parent_pid": LAUNCHER_PID, "name": "claude.exe",
             "command_line": "claude", "start_time": BRAIN_START},
            {"pid": WAIT_PID, "parent_pid": BRAIN_PID, "name": "python.exe",
             "command_line": "agenttalk wait --for c", "start_time": "t-wait"}]
    p = sup.plan_actions(rpt, st, cfg, now_epoch=NOW, snapshot=snap)["agents"]["c"]
    assert "--permission-mode" in p["session_args"]
    i = p["session_args"].index("--permission-mode")
    assert p["session_args"][i + 1] == "bypassPermissions"


def test_ps_template_seeds_preflights_and_drops_baked_python_for_agent() -> None:
    ps = sup.PS_TEMPLATE
    # the agent env carries AGENTTALK_PY, while AGENTTALK_PYTHON and the
    # .agenttalk/bin shim remain supervisor-only.
    assert "$applied = @{ AGENTTALK_ROOT = $Root; AGENTTALK_PY = $AgenttalkPython }" in ps
    assert "AGENTTALK_PYTHON = $AgenttalkPython" not in ps   # not in the AGENT env
    # Seed-CodexHome copies config.toml then overlays via the python core
    assert "function Seed-CodexHome" in ps
    assert "supervise --seed-codex-config" in ps
    # claude settings seed + the PREFLIGHT fail-closed gate
    assert "supervise --seed-claude-settings" in ps
    assert "function Preflight" in ps
    assert "AGENTTALK_PY -m agenttalk --version" in ps      # the smoke-test
    assert "fail closed" in ps.lower()
    # reviewer-1 r1: the CODEX preflight must set PYTHONPATH the SAME way Launch
    # does (src on a checkout) so it tests the agent's REAL import env and does
    # not fail closed on a checkout where agenttalk is not globally installed.
    pf = ps[ps.index("function Preflight"):]
    pf = pf[:pf.index("\n:supervisorPoll do {")]             # before the poll body
    ci = pf.index("$plan.cli -eq 'codex'")
    codex_branch = pf[ci:pf.index("} else {", ci)]          # from codex to the codex/claude divider
    assert "'src') + ';' + $env:PYTHONPATH" in codex_branch
    # 0.31.1: the non-wrapped Codex preflight is the PLAIN import gate under the
    # Codex env (seeded CODEX_HOME + PYTHONPATH), NOT a `codex sandbox ...` probe -
    # the sandbox flags drift across Codex CLI releases and a hard-coded probe
    # false-fail-closed on valid agents.
    assert "$env:CODEX_HOME = $codexHome" in codex_branch    # still under the codex home
    assert "& $AgenttalkPython -m agenttalk --version" in codex_branch
    assert "& $file sandbox" not in codex_branch             # no codex sandbox probe
    assert "-P :workspace" not in codex_branch               # the drift-prone flag is gone
    # Phase C: a WRAPPED agent ($file is python, not the CLI) is preflighted BEFORE
    # the codex branch and validates the python wrapper, NOT the codex sandbox.
    wrap_branch = pf[pf.index("$plan.launch_mode -eq 'wrap'"):ci]
    assert "& $file -m agenttalk --version" in wrap_branch
    assert "Test-WrappedBaseCli" in wrap_branch
    assert "& $file sandbox" not in wrap_branch    # never treats $file as the codex CLI


# ---------------------------------------- WP-3: heartbeat command (throttled)

def test_heartbeat_command_stamps(tmp_path: Path) -> None:
    s = _team(tmp_path)
    assert s.read_heartbeat("worker") is None
    assert _run(["heartbeat", "--for", "worker"], tmp_path) == 0
    assert s.read_heartbeat("worker") is not None


def test_heartbeat_throttle_is_noop_when_fresh(tmp_path: Path) -> None:
    s = _team(tmp_path)
    _run(["heartbeat", "--for", "worker"], tmp_path)
    first = s.read_heartbeat("worker")
    # immediate re-stamp with a large throttle -> no-op (timestamp unchanged)
    assert _run(["heartbeat", "--for", "worker", "--min-interval", "9999"], tmp_path) == 0
    assert s.read_heartbeat("worker") == first
    # min-interval 0 always stamps
    assert _run(["heartbeat", "--for", "worker", "--min-interval", "0"], tmp_path) == 0


def test_heartbeat_unknown_agent_exit_2(tmp_path: Path) -> None:
    _team(tmp_path)
    # _resolve_self rejects an off-roster identity with a usage exit (2).
    with pytest.raises(SystemExit) as e:
        _run(["heartbeat", "--for", "ghost"], tmp_path)
    assert e.value.code == 2


def test_heartbeat_hook_mode_off_roster_returns_0_and_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """0.31.1 (reviewer-1): --hook (PostToolUse) mode must NEVER block a tool call
    AND must be SILENT on an unresolved/off-roster identity - the strict resolver
    writes to stderr BEFORE raising, so hook mode redirects stdout+stderr. Returns
    0, writes no heartbeat, and emits NOTHING on stdout/stderr (else it spams every
    tool call)."""
    s = _team(tmp_path)
    assert _run(["heartbeat", "--for", "ghost", "--hook"], tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""   # silent: no per-call spam
    assert s.read_heartbeat("ghost") is None
    assert s.read_heartbeat("worker") is None     # nothing stamped for anyone


def test_heartbeat_hook_mode_valid_identity_still_writes(tmp_path: Path) -> None:
    s = _team(tmp_path)
    assert s.read_heartbeat("worker") is None
    assert _run(["heartbeat", "--for", "worker", "--hook"], tmp_path) == 0
    assert s.read_heartbeat("worker") is not None  # valid identity -> normal stamp


def test_heartbeat_hook_mode_uninitialized_store_returns_0_and_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    # no store.init() here: a missing/uninitialized store must not block the tool,
    # and must be silent (no stderr from the strict store/identity resolution).
    assert _run(["heartbeat", "--for", "worker", "--hook"], tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_heartbeat_hook_fallback_writes_without_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)

    assert _run(["heartbeat", "--hook", "--fallback-for", "lead"], tmp_path) == 0

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert s.read_heartbeat("lead") is not None
    assert s.read_heartbeat("worker") is None


def test_heartbeat_hook_env_overrides_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path)
    monkeypatch.setenv("AGENTTALK_SELF", "worker")

    assert _run(["heartbeat", "--hook", "--fallback-for", "lead"], tmp_path) == 0

    assert s.read_heartbeat("worker") is not None
    assert s.read_heartbeat("lead") is None


@pytest.mark.parametrize("fallback", ["ghost", "../lead"])
def test_heartbeat_hook_invalid_or_off_roster_fallback_is_silent_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fallback: str,
) -> None:
    s = _team(tmp_path)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)

    assert _run(["heartbeat", "--hook", "--fallback-for", fallback], tmp_path) == 0

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert s.read_heartbeat("lead") is None
    assert s.read_heartbeat("worker") is None


def test_heartbeat_fallback_requires_hook_and_is_not_strict_authority(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)

    assert _run(["heartbeat", "--fallback-for", "lead"], tmp_path) == 2

    captured = capsys.readouterr()
    assert "--fallback-for requires --hook" in captured.err
    assert s.read_heartbeat("lead") is None


def test_heartbeat_hook_fallback_obeys_throttle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)

    assert _run(["heartbeat", "--hook", "--fallback-for", "lead"], tmp_path) == 0
    first = s.read_heartbeat("lead")
    assert first is not None

    assert _run(
        ["heartbeat", "--hook", "--fallback-for", "lead", "--min-interval", "9999"],
        tmp_path,
    ) == 0

    assert s.read_heartbeat("lead") == first


# ---------------------------------------- WP-3: install-activity-hook (merge-safe)


def _post_tool_commands(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        h["command"]
        for group in data["hooks"]["PostToolUse"]
        for h in group.get("hooks", [])
        if isinstance(h, dict) and "command" in h
    ]


def _recognized_heartbeat_commands(commands: list[str]) -> list[str]:
    return [
        command
        for command in commands
        if (
            command == "agenttalk heartbeat"
            or command == "agenttalk heartbeat --hook"
            or command.startswith("agenttalk heartbeat --hook --fallback-for ")
        )
    ]


def test_install_activity_hook_merges_and_is_idempotent(tmp_path: Path) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    # a pre-existing unrelated hook + key must be preserved
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo pre"}]}]}}), encoding="utf-8")
    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"                       # preserved
    assert data["hooks"]["PreToolUse"]                   # preserved
    cmds = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert "agenttalk heartbeat --hook" in cmds            # 0.31.1: soft hook command
    # idempotent: second install adds nothing
    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    data2 = json.loads(settings.read_text(encoding="utf-8"))
    post = [h["command"] for g in data2["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert post.count("agenttalk heartbeat --hook") == 1


def test_install_activity_hook_codex_uses_group_shape(tmp_path: Path) -> None:
    """Blocker 2: the Codex hook must be the matcher-GROUP shape (mirroring
    Claude), not a flat {type,command} — else it mis-installs and the
    presence-check duplicates a correctly-shaped existing hook."""
    s = _team(tmp_path)
    assert _run(["supervise", "--install-activity-hook", "--codex-only"], tmp_path) == 0
    hooks_file = s.root / ".codex" / "hooks.json"
    assert hooks_file.exists()
    assert not (s.root / ".claude" / "settings.json").exists()  # codex-only
    groups = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    # matcher-group shape: each group has a NESTED hooks list
    assert groups[0]["matcher"] == "*"
    assert groups[0]["hooks"][0]["command"] == "agenttalk heartbeat --hook"
    # idempotent (presence-check sees the nested shape)
    assert _run(["supervise", "--install-activity-hook", "--codex-only"], tmp_path) == 0
    groups2 = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    cmds = [h["command"] for g in groups2 for h in g["hooks"]]
    assert cmds.count("agenttalk heartbeat --hook") == 1


def test_install_activity_hook_upgrades_and_dedupes_legacy_bare(tmp_path: Path) -> None:
    """0.31.1: a pre-existing BARE `agenttalk heartbeat` hook (which BLOCKS a tool
    call on a bad identity) is UPGRADED in place to `--hook`, and a duplicate bare
    entry is removed - upgrades never leave a blocking or duplicated hook."""
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    # two legacy bare entries (in separate groups) + an unrelated hook to preserve
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "agenttalk heartbeat"},
            {"type": "command", "command": "echo other"}]},
        {"matcher": "Edit", "hooks": [
            {"type": "command", "command": "agenttalk heartbeat"}]},
    ]}}), encoding="utf-8")
    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert cmds.count("agenttalk heartbeat --hook") == 1     # upgraded + deduped
    assert "agenttalk heartbeat" not in cmds                 # no bare (blocking) hook left
    assert "echo other" in cmds                              # unrelated hook preserved


def test_install_activity_hook_interactive_writes_fallback_and_preserves(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo pre"}]}]},
    }), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead"], tmp_path) == 0

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"]
    cmds = _post_tool_commands(settings)
    assert cmds == ["agenttalk heartbeat --hook --fallback-for lead"]
    assert not (s.root / ".codex" / "hooks.json").exists()

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead"], tmp_path) == 0
    assert _post_tool_commands(settings).count("agenttalk heartbeat --hook --fallback-for lead") == 1


@pytest.mark.parametrize("existing", [
    "agenttalk heartbeat --hook",
    "agenttalk heartbeat",
])
def test_install_activity_hook_interactive_upgrades_existing_heartbeat_without_dup(
    tmp_path: Path,
    existing: str,
) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": existing},
            {"type": "command", "command": "echo other"}]},
        {"matcher": "Edit", "hooks": [
            {"type": "command", "command": existing}]},
    ]}}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead"], tmp_path) == 0

    cmds = _post_tool_commands(settings)
    assert cmds.count("agenttalk heartbeat --hook --fallback-for lead") == 1
    assert existing not in cmds
    assert "echo other" in cmds


def test_install_activity_hook_interactive_rebinds_wrong_fallback_without_dup(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "agenttalk heartbeat --hook --fallback-for worker"}]},
        {"matcher": "Edit", "hooks": [
            {"type": "command", "command": "agenttalk heartbeat --hook"}]},
    ]}}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead"], tmp_path) == 0

    assert _post_tool_commands(settings) == ["agenttalk heartbeat --hook --fallback-for lead"]


def test_install_activity_hook_neutral_does_not_downgrade_existing_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    fallback = "agenttalk heartbeat --hook --fallback-for lead"
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": fallback}]},
    ]}}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    assert "already:" in capsys.readouterr().out
    cmds = _post_tool_commands(settings)
    assert cmds == [fallback]
    assert _recognized_heartbeat_commands(cmds) == [fallback]


def test_install_activity_hook_neutral_dedupes_mixed_fallback_and_neutral(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    fallback = "agenttalk heartbeat --hook --fallback-for lead"
    neutral = "agenttalk heartbeat --hook"
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "command", "command": fallback},
            {"type": "command", "command": "echo other"}]},
        {"matcher": "Edit", "hooks": [
            {"type": "command", "command": neutral}]},
    ]}}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    cmds = _post_tool_commands(settings)
    assert _recognized_heartbeat_commands(cmds) == [fallback]
    assert "echo other" in cmds


def test_install_activity_hook_interactive_refuses_non_liaison(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "worker"], tmp_path) == 2

    assert "operator-facing liaison" in capsys.readouterr().err


def test_install_activity_hook_interactive_allows_sole_lead_without_liaison(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead"], tmp_path) == 0

    settings = s.root / ".claude" / "settings.json"
    assert _post_tool_commands(settings) == ["agenttalk heartbeat --hook --fallback-for lead"]


@pytest.mark.parametrize("flag", ["--codex", "--codex-only"])
def test_install_activity_hook_interactive_refuses_codex_modes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    flag: str,
) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead", flag], tmp_path) == 2

    assert "cannot be combined" in capsys.readouterr().err
    assert not (s.root / ".codex" / "hooks.json").exists()


def test_generated_ps1_is_bom_ascii_and_parses(tmp_path: Path) -> None:
    """Generated PowerShell is BOM-free ASCII and parses under supported Core."""
    s = _team(tmp_path)
    assert _run(["supervise", "--init"], tmp_path) == 0
    ps1 = s.dir / "supervisor.ps1"
    raw = ps1.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "supervisor.ps1 must be BOM-free"
    non_ascii = [b for b in raw if b > 0x7F]
    assert not non_ascii, f"supervisor.ps1 body must be ASCII-only; found {non_ascii[:5]}"
    shell = shutil.which("pwsh")
    if not shell:
        return
    check = (
        "$e=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}',"
        "[ref]$null,[ref]$e); if($e -and $e.Count){ $e[0].Message; exit 1 }")
    res = subprocess.run([shell, "-NoProfile", "-Command", check],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, (
        f"supervisor.ps1 failed to parse under {shell}: {res.stdout}{res.stderr}")

def test_generated_helper_ps1_are_bom_ascii_and_parse(tmp_path: Path) -> None:
    s = _team(tmp_path)
    assert _run(["supervise", "--init"], tmp_path) == 0
    ps_files = [s.dir / "supervisor-task.ps1", s.dir / "deadman.ps1"]
    for ps1 in ps_files:
        raw = ps1.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{ps1.name} must be BOM-free"
        non_ascii = [b for b in raw if b > 0x7F]
        assert not non_ascii, f"{ps1.name} body must be ASCII-only; found {non_ascii[:5]}"

    shell = shutil.which("pwsh")
    if not shell:
        return
    for ps1 in ps_files:
        check = (
            "$e=$null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}',"
            "[ref]$null,[ref]$e); if($e -and $e.Count){ $e[0].Message; exit 1 }")
        res = subprocess.run([shell, "-NoProfile", "-Command", check],
                             capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, (
            f"{ps1.name} failed to parse under {shell}: {res.stdout}{res.stderr}")


def test_supervise_plan_exact_generated_command_runs(tmp_path: Path, capsys) -> None:
    """Regression for the BLOCKER: the generated script's command line
    (`supervise --plan --state-file S --snapshot-file SNAP --now N`, LIVE report,
    NO --json) must parse and emit valid JSON - the prior templates passed a
    non-existent --json flag and would have argparse-failed at runtime."""
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    state_file = s.dir / "supervisor-state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    snap_file = s.dir / "supervisor-snapshot.json"
    snap_file.write_text("[]", encoding="utf-8")   # captured, no brain; no hook -> suspect
    # EXACTLY the args supervisor.ps1 invokes (live report; no --json).
    rc = _run(["supervise", "--plan", "--state-file", str(state_file),
               "--snapshot-file", str(snap_file), "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)   # must be valid JSON
    assert plan["agents"]["worker"]["action"] == sup.SUSPECT_WARN
    assert plan["agents"]["worker"]["state"] == "ACTIVE_OR_BUSY"


def _pick_powershell() -> str | None:
    """Return supported PowerShell Core on Windows, if installed.

    These runtime tests exercise the .cmd shim and Windows Start-Process arg
    quoting, which are Windows-only - a `.cmd` batch file can't execute under
    pwsh on Linux/macOS (GitHub's POSIX runners DO ship pwsh, so a bare
    which() check is not enough). Gate on the OS, not just shell presence."""
    if os.name != "nt":
        return None
    return shutil.which("pwsh")


def _select_test_powershell(root: Path, shell: str) -> None:
    assert _run(["supervise", "--select-pwsh", "--pwsh", shell], root) == 0


def _checkout_runtime_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Make generated-shim subprocesses execute the checkout under test."""
    env = dict(os.environ if base is None else base)
    source = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    env["AGENTTALK_PYTHON"] = sys.executable
    return env


def _live_supervisor_config(*agents: str) -> dict:
    return {
        "agents": {
            name: {"auto_restart": True, "cli": "claude"}
            for name in agents
        },
        "poll_seconds": 2,
        "backoff": {
            "base_seconds": 30,
            "cap_seconds": 900,
            "reset_after_seconds": 180,
        },
        "suspect_warn_interval_seconds": 300,
        "launch_grace_seconds": 120,
    }


def _wait_for_live_supervisor(
    proc: subprocess.Popen,
    log_path: Path,
    predicate,
    *,
    timeout: float = 30,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, json.JSONDecodeError):
            pass
        rc = proc.poll()
        if rc is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"live supervisor exited {rc} before condition; log={log!r}")
        time.sleep(0.05)
    log = log_path.read_text(encoding="utf-8", errors="replace")
    pytest.fail(f"timed out waiting for live supervisor; log={log!r}")


def _stop_live_supervisor(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _state_has_agent(state_path: Path, agent: str) -> bool:
    if not state_path.exists():
        return False
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    return isinstance(state.get("agents", {}).get(agent), dict)


def _log_contains(log_path: Path, text: str) -> bool:
    if not log_path.exists():
        return False
    return text in log_path.read_text(encoding="utf-8", errors="replace")


def _log_occurrences(log_path: Path, text: str) -> int:
    if not log_path.exists():
        return 0
    return log_path.read_text(encoding="utf-8", errors="replace").count(text)


def _replace_text_when_unlocked(path: Path, text: str, *, timeout: float = 5) -> None:
    staged = path.with_name(path.name + ".test-next")
    staged.write_text(text, encoding="utf-8")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(staged, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _start_live_generated_supervisor(
    tmp_path: Path,
    shell: str,
) -> tuple[Store, subprocess.Popen, object, Path]:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    (store.dir / "supervisor.json").write_text(
        json.dumps(_live_supervisor_config("lead")),
        encoding="utf-8",
    )
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    log_path = tmp_path / "live-supervisor.log"
    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [shell, "-NoProfile", "-File", str(store.dir / "supervisor.ps1")],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(tmp_path),
        env=_checkout_runtime_env(),
    )
    return store, proc, log_handle, log_path


@pytest.mark.source_layout
def test_generated_ps1_survives_malformed_config_poll_with_last_good(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    store, proc, log_handle, log_path = _start_live_generated_supervisor(
        tmp_path,
        shell,
    )
    state_path = store.dir / "supervisor-state.json"
    warning = "supervisor.json refresh failed; keeping last-good config"
    try:
        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _state_has_agent(state_path, "lead")
            and _log_contains(log_path, "supervisor: lead:"),
        )
        time.sleep(0.25)
        _replace_text_when_unlocked(
            state_path,
            json.dumps({"agents": {}}),
        )
        (store.dir / "supervisor.json").write_text("{", encoding="utf-8")

        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _log_occurrences(log_path, warning) >= 2
            and _state_has_agent(state_path, "lead"),
        )
        assert proc.poll() is None
    finally:
        _stop_live_supervisor(proc)
        log_handle.close()


@pytest.mark.source_layout
def test_generated_ps1_hot_adds_agent_across_live_polls(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    store, proc, log_handle, log_path = _start_live_generated_supervisor(
        tmp_path,
        shell,
    )
    state_path = store.dir / "supervisor-state.json"
    try:
        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _state_has_agent(state_path, "lead")
            and _log_contains(log_path, "supervisor: lead:"),
        )
        store.add_agent("hot-added", role="lead")
        config_path = store.dir / "supervisor.json"
        _replace_text_when_unlocked(
            config_path,
            json.dumps(_live_supervisor_config("lead", "hot-added")),
        )

        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _state_has_agent(state_path, "hot-added")
            and _log_contains(log_path, "supervisor: hot-added:"),
        )
        assert proc.poll() is None
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        assert state["agents"]["hot-added"]["pid_alive"] is False
    finally:
        _stop_live_supervisor(proc)
        log_handle.close()


@pytest.mark.source_layout
def test_generated_ps1_holds_poll_when_preplan_state_save_is_contended(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    store = _team(tmp_path)
    config = {
        "agents": {
            "worker": {
                "activity_hook": True,
                "auto_restart": True,
                "cli": "claude",
            },
        },
        "backoff": {
            "base_seconds": 30,
            "cap_seconds": 900,
            "reset_after_seconds": 180,
        },
        "launch_grace_seconds": 120,
        "poll_seconds": 1,
    }
    (store.dir / "supervisor.json").write_text(
        json.dumps(config),
        encoding="utf-8",
    )
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    state_path = store.dir / "supervisor-state.json"
    initial = {"agents": {"worker": _ready(backoff_next_epoch=0)}}
    state_path.write_text(json.dumps(initial), encoding="utf-8")
    wrapper = tmp_path / "run-supervisor-with-state-lock.ps1"
    wrapper.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$StatePath = {_pslit(str(state_path))}",
            f"$Supervisor = {_pslit(str(store.dir / 'supervisor.ps1'))}",
            "$lock = [IO.FileStream]::new($StatePath, [IO.FileMode]::Open, "
            "[IO.FileAccess]::Read, [IO.FileShare]::Read)",
            "try { & $Supervisor -Once } finally { $lock.Dispose() }",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(wrapper)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
        env=_checkout_runtime_env(),
    )

    combined = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, combined
    assert "state write failed this poll; will retry next poll" in combined
    assert "supervisor: worker:" not in combined
    persisted = sup.load_supervisor_state(state_path)["agents"]["worker"]
    assert persisted["backoff_next_epoch"] == 0
    assert persisted.get("launching") is False


_STATE_WRITE_LOCK_CSHARP = r"""
using System;
using System.IO;
using System.Threading;

public static class AgenttalkStateWriteLock
{
    private static bool RetryObserved(string path)
    {
        try
        {
            return File.Exists(path) && new FileInfo(path).Length > 0;
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static void WaitForRetry(string retryPath)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(5);
        while (DateTime.UtcNow < deadline && !RetryObserved(retryPath))
        {
            Thread.Sleep(1);
        }
    }

    public static Thread LockExistingUntilRetry(
        string path, string readyPath, string retryPath)
    {
        Thread thread = new Thread(() =>
        {
            using (FileStream stream = new FileStream(
                path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                File.WriteAllText(readyPath, "ready");
                WaitForRetry(retryPath);
            }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }

    public static Thread LockExistingForMilliseconds(
        string path, string readyPath, int milliseconds)
    {
        Thread thread = new Thread(() =>
        {
            using (FileStream stream = new FileStream(
                path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                File.WriteAllText(readyPath, "ready");
                Thread.Sleep(milliseconds);
            }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }

    public static Thread LockNextTempUntilRetry(
        string directory, string readyPath, string retryPath)
    {
        Thread thread = new Thread(() =>
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(5);
            FileStream stream = null;
            while (DateTime.UtcNow < deadline && stream == null)
            {
                foreach (string candidate in Directory.GetFiles(directory, ".at-*.tmp"))
                {
                    try
                    {
                        stream = new FileStream(
                            candidate, FileMode.Open, FileAccess.Read, FileShare.None);
                        break;
                    }
                    catch (IOException)
                    {
                    }
                }
                if (stream == null)
                {
                    Thread.Sleep(1);
                }
            }
            if (stream == null)
            {
                File.WriteAllText(readyPath, "timeout");
                return;
            }
            using (stream)
            {
                File.WriteAllText(readyPath, "ready");
                WaitForRetry(retryPath);
            }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }
}
""".strip()


def _state_write_contention_harness_prefix(
    *,
    state_path: Path,
    ready_path: Path,
    retry_path: Path,
) -> list[str]:
    ps = sup.PS_TEMPLATE
    helpers = ps[ps.index("# region state-helpers"):ps.index("# endregion state-helpers")]
    return [
        "$ErrorActionPreference = 'Stop'",
        f"$StatePath = {_pslit(str(state_path))}",
        "$StateBackupPath = \"$StatePath.bak\"",
        "$KillSwitchPath = Join-Path (Split-Path -Parent $StatePath) 'supervisor.kill'",
        f"$ReadyPath = {_pslit(str(ready_path))}",
        f"$RetryPath = {_pslit(str(retry_path))}",
        "function Actions-Enabled { return $true }",
        "Add-Type -TypeDefinition @'",
        _STATE_WRITE_LOCK_CSHARP,
        "'@",
        "function Start-Sleep {",
        "  param([int]$Milliseconds)",
        "  [IO.File]::AppendAllText($RetryPath, \"$Milliseconds`n\")",
        "  Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds $Milliseconds",
        "}",
        helpers,
    ]


@pytest.mark.parametrize("swap_path", ["replace", "move"])
def test_ps_state_atomic_swap_retries_windows_sharing_violation(
    tmp_path: Path,
    swap_path: str,
) -> None:
    if os.name != "nt":
        return
    shell = _pick_powershell()
    if not shell:
        return
    state_path = tmp_path / "supervisor-state.json"
    ready_path = tmp_path / "lock-ready.txt"
    retry_path = tmp_path / "retry-observed.txt"
    result_path = tmp_path / "state-swap-result.json"
    if swap_path == "replace":
        state_path.write_text(
            json.dumps({"agents": {"worker": {"pid": 101}}}),
            encoding="utf-8",
        )

    harness = _state_write_contention_harness_prefix(
        state_path=state_path,
        ready_path=ready_path,
        retry_path=retry_path,
    )
    if swap_path == "replace":
        harness += [
            "$locker = [AgenttalkStateWriteLock]::LockExistingUntilRetry(",
            "  $StatePath, $ReadyPath, $RetryPath)",
            "while (-not (Microsoft.PowerShell.Management\\Test-Path "
            "-LiteralPath $ReadyPath)) {",
            "  Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds 1",
            "}",
        ]
    else:
        harness += [
            "$locker = [AgenttalkStateWriteLock]::LockNextTempUntilRetry(",
            "  (Split-Path -Parent $StatePath), $ReadyPath, $RetryPath)",
            "$script:MoveGateWaited = $false",
            "function Test-Path {",
            "  param([string]$LiteralPath)",
            "  $exists = Microsoft.PowerShell.Management\\Test-Path "
            "-LiteralPath $LiteralPath",
            "  if (-not $script:MoveGateWaited -and $LiteralPath -eq $StatePath "
            "-and -not $exists) {",
            "    while (-not (Microsoft.PowerShell.Management\\Test-Path "
            "-LiteralPath $ReadyPath)) {",
            "      Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds 1",
            "    }",
            "    if ([IO.File]::ReadAllText($ReadyPath) -ne 'ready') {",
            "      throw 'temp lock did not become ready'",
            "    }",
            "    $script:MoveGateWaited = $true",
            "  }",
            "  return $exists",
            "}",
        ]
    harness += [
        "$next = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{ pid = 202 } } }",
        "Write-StateFileAtomic $StatePath $next",
        "$null = $locker.Join(5000)",
        "$retryCount = @(Get-Content -LiteralPath $RetryPath).Count",
        "$saved = Read-StateFile $StatePath",
        "@{ pid = $saved.agents.worker.pid; retries = $retryCount } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / f"state-swap-{swap_path}.ps1"
    script.write_text("\n".join(harness), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["pid"] == 202
    assert payload["retries"] >= 1


def test_ps_poll_state_save_warns_and_survives_persistent_contention(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        return
    shell = _pick_powershell()
    if not shell:
        return
    state_path = tmp_path / "supervisor-state.json"
    state_path.write_text(
        json.dumps({"agents": {"worker": {"pid": 101}}}),
        encoding="utf-8",
    )
    ready_path = tmp_path / "persistent-lock-ready.txt"
    retry_path = tmp_path / "persistent-retries.txt"
    result_path = tmp_path / "poll-save-result.json"
    harness = _state_write_contention_harness_prefix(
        state_path=state_path,
        ready_path=ready_path,
        retry_path=retry_path,
    )
    harness += [
        "$locker = [AgenttalkStateWriteLock]::LockExistingForMilliseconds(",
        "  $StatePath, $ReadyPath, 2500)",
        "while (-not (Microsoft.PowerShell.Management\\Test-Path "
        "-LiteralPath $ReadyPath)) {",
        "  Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds 1",
        "}",
        "$next = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{ pid = 202 } } }",
        "$results = @()",
        "for ($poll = 0; $poll -lt 2; $poll++) {",
        "  $results += [bool](Save-StateForPoll $next)",
        "}",
        "$primary = Read-StateFile $StatePath",
        "@{ polls = $results.Count; saved = @($results | Where-Object { $_ }).Count; "
        "pid = $primary.agents.worker.pid } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "poll-save-contention.ps1"
    script.write_text("\n".join(harness), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    combined = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, combined
    assert combined.count(
        "supervisor: state write failed this poll; will retry next poll"
    ) >= 2
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload == {"polls": 2, "saved": 0, "pid": 101}


def test_ps_poll_state_save_only_softens_sharing_and_lock_violations(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[ps.index("# region state-helpers"):ps.index("# endregion state-helpers")]
    result_path = tmp_path / "poll-save-error-classification.json"
    state_path = tmp_path / "supervisor-state.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$StatePath = {_pslit(str(state_path))}",
        "$StateBackupPath = \"$StatePath.bak\"",
        "$KillSwitchPath = Join-Path (Split-Path -Parent $StatePath) 'supervisor.kill'",
        "function Actions-Enabled { return $true }",
        helpers,
        "$script:Failure = ''",
        "function Save-State($state) {",
        "  switch ($script:Failure) {",
        "    'sharing' { throw [IO.IOException]::new('sharing', -2147024864) }",
        "    'lock' { throw [IO.IOException]::new('lock', -2147024863) }",
        "    'disk_full' { throw [IO.IOException]::new('disk full', -2147024784) }",
        "    'unauthorized' { throw [UnauthorizedAccessException]::new('denied') }",
        "  }",
        "}",
        "$next = [pscustomobject]@{ agents = [pscustomobject]@{} }",
        "$script:Failure = 'sharing'",
        "$sharingSoft = -not [bool](Save-StateForPoll $next)",
        "$script:Failure = 'lock'",
        "$lockSoft = -not [bool](Save-StateForPoll $next)",
        "$script:Failure = 'disk_full'",
        "$diskFullPropagated = $false",
        "try { Save-StateForPoll $next | Out-Null } catch { $diskFullPropagated = $true }",
        "$script:Failure = 'unauthorized'",
        "$unauthorizedPropagated = $false",
        "try { Save-StateForPoll $next | Out-Null } catch { $unauthorizedPropagated = $true }",
        "@{ sharing_soft = $sharingSoft; lock_soft = $lockSoft; "
        "disk_full_propagated = $diskFullPropagated; "
        "unauthorized_propagated = $unauthorizedPropagated } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ])
    script = tmp_path / "poll-save-error-classification.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert json.loads(result_path.read_text(encoding="utf-8-sig")) == {
        "sharing_soft": True,
        "lock_soft": True,
        "disk_full_propagated": True,
        "unauthorized_propagated": True,
    }


def test_spawned_launch_is_not_acknowledged_when_record_launch_cannot_commit(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    store = _team(tmp_path)
    (store.dir / "supervisor.json").write_text(
        json.dumps({
            "agents": {"worker": {"auto_restart": True, "cli": "claude"}},
            "launch_grace_seconds": 120,
        }),
        encoding="utf-8",
    )
    assert _run(["supervise", "--init"], tmp_path) == 0
    state_path = store.dir / "supervisor-state.json"
    state_path.write_text(
        json.dumps({
            "agents": {
                "worker": {
                    "launcher_pid": 111,
                    "last_launch_epoch": NOW - 100,
                },
            },
        }),
        encoding="utf-8",
    )
    store.write_restart_request("worker", _auth_marker("record-launch-race"))
    marker_path = store.state_dir / "worker.restart-request"
    child_script = tmp_path / "record-launch-child.py"
    child_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    result_path = tmp_path / "record-launch-contention-result.json"
    generated = (store.dir / "supervisor.ps1").read_text(encoding="utf-8-sig")
    mutation_helpers = generated[
        generated.index("# region checked-mutations"):
        generated.index("# endregion checked-mutations")
    ]
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$AgenttalkCmd = {_pslit(str(store.dir / 'bin' / 'agenttalk.cmd'))}",
        f"$Root = {_pslit(str(tmp_path))}",
        f"$StatePath = {_pslit(str(state_path))}",
        f"$MarkerPath = {_pslit(str(marker_path))}",
        f"$ChildScript = {_pslit(str(child_script))}",
        f"$ResultPath = {_pslit(str(result_path))}",
        f"$Python = {_pslit(sys.executable)}",
        mutation_helpers,
        "$child = Start-Process -FilePath $Python "
        "-ArgumentList ('\"{0}\"' -f $ChildScript) -PassThru",
        "$lock = [IO.FileStream]::new($StatePath, [IO.FileMode]::Open, "
        "[IO.FileAccess]::Read, [IO.FileShare]::Read)",
        "try {",
        "  $recordArgs = @('--root', $Root, 'supervise', '--record-launch', "
        "'--for', 'worker', '--cli', 'claude', '--pid', [string]$child.Id, "
        f"'--now', '{NOW}', '--state-file', $StatePath)",
        "  $recorded = Invoke-CheckedSupervisorMutation "
        "'record-launch worker' $recordArgs",
        "  if ($recorded) {",
        "    & $AgenttalkCmd --root $Root supervise --clear-restart --for worker "
        "--request-id record-launch-race | Out-Null",
        "  }",
        "} finally {",
        "  $lock.Dispose()",
        "  Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue",
        "}",
        "@{ recorded = [bool]$recorded; marker_present = (Test-Path $MarkerPath) } | "
        "ConvertTo-Json | Set-Content $ResultPath -Encoding utf8",
    ])
    script = tmp_path / "record-launch-contention.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
        env=_checkout_runtime_env(),
    )

    combined = f"{result.stdout}{result.stderr}"
    assert result.returncode == 0, combined
    assert "record-launch worker failed" in combined
    assert json.loads(result_path.read_text(encoding="utf-8-sig")) == {
        "recorded": False,
        "marker_present": True,
    }
    persisted = sup.load_supervisor_state(state_path)["agents"]["worker"]
    assert persisted["launcher_pid"] == 111
    assert persisted["last_launch_epoch"] == NOW - 100


@pytest.mark.parametrize("wrapped", [False, True], ids=["legacy-direct", "wrapped"])
@pytest.mark.source_layout
def test_generated_ps1_two_polls_do_not_duplicate_launch_after_postspawn_contention(
    tmp_path: Path,
    wrapped: bool,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state_path = store.dir / "supervisor-state.json"
    launch_log = tmp_path / "postspawn-launches.txt"
    lock_ready = tmp_path / "postspawn-lock-ready.txt"
    stop_path = tmp_path / "postspawn-stop.txt"
    fake_modules = tmp_path / "fake-launch-modules"
    fake_modules.mkdir()
    child_source = "\n".join([
        "import os",
        "import time",
        "from pathlib import Path",
        "launch_log = Path(os.environ['AGENTTALK_TEST_LAUNCH_LOG'])",
        "with launch_log.open('a', encoding='utf-8') as stream:",
        "    stream.write(f'{os.getpid()}\\n')",
        "stop = Path(os.environ['AGENTTALK_TEST_STOP'])",
        "while not stop.exists():",
        "    time.sleep(0.02)",
        "",
    ])
    (fake_modules / "legacy_agent.py").write_text(child_source, encoding="utf-8")
    fake_agenttalk = fake_modules / "agenttalk"
    fake_agenttalk.mkdir()
    (fake_agenttalk / "__init__.py").write_text("", encoding="utf-8")
    (fake_agenttalk / "__main__.py").write_text(child_source, encoding="utf-8")

    if wrapped:
        launch_args = [
            "-m", "agenttalk", "--root", "{ROOT}", "wrap", "--for", "worker",
            "--cli", "claude", "--loop", "--", sys.executable,
        ]
    else:
        launch_args = ["-m", "legacy_agent", "{SESSION_ARGS}"]
    config = {
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "claude",
                "wrapped": wrapped,
                "cwd": str(tmp_path),
                "env": {
                    "AGENTTALK_TEST_LAUNCH_LOG": str(launch_log),
                    "AGENTTALK_TEST_STOP": str(stop_path),
                    "PYTHONPATH": str(fake_modules),
                },
                "launch": {
                    "windows_file": sys.executable,
                    "windows_args": launch_args,
                },
            },
        },
        "backoff": {
            "base_seconds": 30,
            "cap_seconds": 900,
            "reset_after_seconds": 180,
        },
        "launch_grace_seconds": 120,
        "poll_seconds": 1,
    }
    (store.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    state_path.write_text(
        json.dumps({
            "agents": {
                "worker": {
                    "backoff_next_epoch": 0,
                    "launching": False,
                    "last_launch_epoch": 0,
                    "readiness_seen": True,
                    "resume_available": True,
                    "session_id": "abc",
                },
            },
        }),
        encoding="utf-8",
    )
    store.write_restart_request("worker", _auth_marker("rr-postspawn"))

    supervisor_path = store.dir / "supervisor.ps1"
    runner_script = tmp_path / "run-supervisor-with-postspawn-lock.ps1"
    runner_script.write_text(
        "\n".join([
            "param(",
            "  [Parameter(Mandatory=$true)][string]$SupervisorPath,",
            "  [Parameter(Mandatory=$true)][string]$TestStatePath,",
            "  [Parameter(Mandatory=$true)][string]$LaunchLogPath,",
            "  [Parameter(Mandatory=$true)][string]$LockReadyPath,",
            "  [switch]$InjectStateLock",
            ")",
            "$ErrorActionPreference = 'Stop'",
            "$script:InjectedStateLock = $null",
            "function Start-Process {",
            "  [CmdletBinding()]",
            "  param(",
            "    [Parameter(Mandatory=$true)][string]$FilePath,",
            "    [string[]]$ArgumentList,",
            "    [string]$WorkingDirectory,",
            "    [System.Diagnostics.ProcessWindowStyle]$WindowStyle,",
            "    [switch]$PassThru",
            "  )",
            "  $startArgs = @{",
            "    FilePath = $FilePath",
            "    WorkingDirectory = $WorkingDirectory",
            "    WindowStyle = $WindowStyle",
            "    PassThru = [bool]$PassThru",
            "  }",
            "  if ($PSBoundParameters.ContainsKey('ArgumentList')) {",
            "    $startArgs.ArgumentList = $ArgumentList",
            "  }",
            "  $proc = Microsoft.PowerShell.Management\\Start-Process @startArgs",
            "  if ($InjectStateLock) {",
            "    $script:InjectedStateLock = [IO.FileStream]::new(",
            "      $TestStatePath, [IO.FileMode]::Open, [IO.FileAccess]::Read,",
            "      [IO.FileShare]::Read)",
            "    [IO.File]::WriteAllText($LockReadyPath, 'ready')",
            "  }",
            "  $deadline = [DateTime]::UtcNow.AddSeconds(15)",
            "  $logged = $false",
            "  do {",
            "    if (Test-Path -LiteralPath $LaunchLogPath) {",
            "      $logged = [IO.File]::ReadAllLines($LaunchLogPath) -contains [string]$proc.Id",
            "    }",
            "    if (-not $logged) {",
            "      Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds 10",
            "    }",
            "  } until ($logged -or [DateTime]::UtcNow -ge $deadline)",
            "  if (-not $logged) { throw \"launched pid $($proc.Id) was not logged\" }",
            "  return $proc",
            "}",
            "try {",
            "  . $SupervisorPath -Once",
            "} finally {",
            "  if ($null -ne $script:InjectedStateLock) {",
            "    $script:InjectedStateLock.Dispose()",
            "  }",
            "}",
        ]),
        encoding="utf-8-sig",
    )
    command = [
        shell, "-NoProfile", "-File", str(runner_script),
        "-SupervisorPath", str(supervisor_path),
        "-TestStatePath", str(state_path),
        "-LaunchLogPath", str(launch_log),
        "-LockReadyPath", str(lock_ready),
    ]
    env = _checkout_runtime_env()
    launched_pids: list[int] = []
    try:
        first = subprocess.run(
            [*command, "-InjectStateLock"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
            env=env,
        )
        first_output = f"{first.stdout}{first.stderr}"
        assert first.returncode == 0, first_output
        assert lock_ready.read_text(encoding="utf-8") == "ready"
        assert (
            "state write failed this poll" in first_output
            or "record-launch worker failed" in first_output
        ), first_output
        first_pids = [
            int(line)
            for line in launch_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(first_pids) == 1
        reserved = sup.load_supervisor_state(state_path)["agents"]["worker"]

        second = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
            env=env,
        )
        assert second.returncode == 0, f"{second.stdout}{second.stderr}"
        launched_pids = [
            int(line)
            for line in launch_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        persisted = sup.load_supervisor_state(state_path)["agents"]["worker"]
        assert len(launched_pids) == 1
        assert "record-launch worker failed" in first_output
        assert reserved["launching"] is True
        assert "rr-postspawn" in reserved["consumed_rids"]
        assert reserved["pending_restart_request_id"] == "rr-postspawn"
        assert reserved.get("launcher_pid") is None
        assert persisted["launching"] is True
        assert "rr-postspawn" in persisted["consumed_rids"]
    finally:
        stop_path.write_text("stop", encoding="utf-8")
        if launch_log.exists():
            launched_pids = [
                int(line)
                for line in launch_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        for pid in set(launched_pids):
            subprocess.run(
                [
                    shell, "-NoProfile", "-Command",
                    f"Wait-Process -Id {pid} -Timeout 5 -ErrorAction SilentlyContinue; "
                    f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )


def test_ps_state_helpers_recover_backup_and_refuse_two_corrupt_copies(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[ps.index("# region state-helpers"):ps.index("# endregion state-helpers")]
    state_path = tmp_path / "supervisor-state.json"
    result_path = tmp_path / "state-helper-result.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$StatePath = {_pslit(str(state_path))}",
        "$StateBackupPath = \"$StatePath.bak\"",
        "$KillSwitchPath = Join-Path (Split-Path -Parent $StatePath) 'supervisor.kill'",
        "function Actions-Enabled { return $true }",
        helpers,
        "$first = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{ pid = 101 } } }",
        "$second = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{ pid = 202 } } }",
        "Save-State $first",
        "Save-State $second",
        "[IO.File]::WriteAllText($StatePath, '{broken')",
        "$recovered = Load-State",
        "[IO.File]::WriteAllText($StateBackupPath, '[]')",
        "$failedClosed = $false",
        "try { $null = Load-State } catch { $failedClosed = $true }",
        "@{ recoveredPid = $recovered.agents.worker.pid; failedClosed = $failedClosed } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ])
    script = tmp_path / "state-helper-test.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload == {"recoveredPid": 101, "failedClosed": True}


def test_ps_set_agent_state_adds_new_agent_to_fresh_and_reloaded_state(
    tmp_path: Path,
) -> None:
    """The poll must create state for an agent added after the supervisor starts."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[ps.index("# region state-helpers"):ps.index("# endregion state-helpers")]
    result_path = tmp_path / "set-agent-state-result.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$fresh = [pscustomobject]@{ agents = [pscustomobject]@{} }",
        "$reloaded = '{\"agents\":{}}' | ConvertFrom-Json",
        helpers,
        "Set-AgentState $fresh 'new-agent' ([pscustomobject]@{ state = 'FRESH' })",
        "Set-AgentState $reloaded 'new-agent' ([pscustomobject]@{ state = 'RELOADED' })",
        "@{ fresh = $fresh.agents.'new-agent'.state; "
        "reloaded = $reloaded.agents.'new-agent'.state } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ])
    script = tmp_path / "set-agent-state-test.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload == {"fresh": "FRESH", "reloaded": "RELOADED"}


def _replace_proc_start(ps1: Path, body: str) -> None:
    text = ps1.read_text(encoding="utf-8-sig")
    start = text.index("function Proc-Start($procId) {")
    end = text.index("function Get-ProcSnapshot", start)
    ps1.write_text(
        text[:start] + "function Proc-Start($procId) {\n" + body + "\n}\n" + text[end:],
        encoding="utf-8-sig",
    )


def _replace_proc_snapshot_with_empty(ps1: Path) -> None:
    """Keep generated-script tests independent of the host process table."""
    text = ps1.read_text(encoding="utf-8-sig")
    start = text.index("function Get-ProcSnapshot($path) {")
    end = text.index("function Stop-Tree", start)
    replacement = """function Get-ProcSnapshot($path) {
  $u8 = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($path, '[]', $u8)
  return $true
}
"""
    ps1.write_text(
        text[:start] + replacement + text[end:],
        encoding="utf-8-sig",
    )


def test_generated_ps1_runs_bus_calls_without_console_script_on_path(tmp_path: Path) -> None:
    """0.28.1 BLOCKER 1 (RUNTIME): the generated supervisor.ps1 must make ITS OWN
    bus calls via the project-local shim (`& $AgenttalkCmd`), so they work even
    when the `agenttalk` console script is NOT on PATH (only `python -m agenttalk`
    resolves - a source/sandbox env). We run the REAL .ps1 with `-Once -DryRun`
    under a PATH stripped to the Windows dirs and assert it produces the plan with
    no `is not recognized` / CommandNotFound error. A structural template check
    can't catch a bare `agenttalk` regressing back in - only running it can."""
    shell = _pick_powershell()
    if not shell:
        return
    s = _team(tmp_path)                                   # roster: lead, worker
    # A real supervisor.json (the shipped template uses an AGENT_NAME placeholder);
    # write it BEFORE --init so init leaves it intact and only emits the .ps1 + shim.
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    ps1 = s.dir / "supervisor.ps1"
    assert ps1.exists() and (s.dir / "bin" / "agenttalk.cmd").exists()
    # PATH reduced to the Windows dirs only: no Python Scripts dir, so a bare
    # `agenttalk[.exe]` console script is unreachable - only the baked shim works.
    windir = os.environ.get("WINDIR", r"C:\Windows")
    reduced = _checkout_runtime_env()
    reduced["PATH"] = os.pathsep.join([
        os.path.join(windir, "System32"), windir,
        os.path.join(windir, "System32", "WindowsPowerShell", "v1.0")])
    res = subprocess.run(
        [shell, "-NoProfile", "-File", str(ps1), "-Once", "-DryRun"],
        capture_output=True, text=True, timeout=120, env=reduced, cwd=str(tmp_path))
    combined = res.stdout + res.stderr
    assert res.returncode == 0, f"supervisor.ps1 -Once -DryRun failed: {combined}"
    assert "is not recognized" not in combined and "CommandNotFound" not in combined, combined
    # the DryRun plan line for the dead worker actually printed (the bus call ran)
    assert "worker:" in res.stdout, f"no plan emitted; stdout={res.stdout!r} stderr={res.stderr!r}"


def test_proc_start_falls_back_to_get_process_when_cim_denied(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    out = tmp_path / "proc_start.json"
    harness = "\n".join([
        helpers,
        "function Get-CimInstance { throw 'cim denied' }",
        "$start = Proc-Start $PID",
        f"@{{ has_start = [bool]$start }} | ConvertTo-Json | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "proc_start_fallback.ps1"
    hp.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"{res.stdout}{res.stderr}"
    assert json.loads(out.read_text(encoding="utf-8-sig"))["has_start"] is True


def test_generated_ps1_tamper_refuses_before_claim(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    ps1 = s.dir / "supervisor.ps1"
    _replace_proc_start(ps1, "  return $null")

    res = subprocess.run(
        [shell, "-NoProfile", "-File", str(ps1), "-Once", "-Quiet"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
        env=_checkout_runtime_env(),
    )
    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "refresh-scripts" in combined
    assert s.read_supervisor_instance() is None


@pytest.mark.source_layout
def test_generated_ps1_quiet_suppresses_warning_path(tmp_path: Path) -> None:
    """0.29.0 (reviewer-1 r1): -Quiet must silence the WHOLE console log, including
    the Write-Warning ACTION paths - not just the new info Write-Host. We run the
    REAL generated .ps1 against a PROTECTED, stale agent (the lead: role=lead =>
    protected => warn_only, which NEVER launches a process, so it is safe to run
    the real loop in CI). Once WITHOUT -Quiet the warning prints; once WITH -Quiet
    the console is silent. A structural string check can't prove the gating works."""
    shell = _pick_powershell()
    if not shell:
        return
    s = _team(tmp_path)                                   # roster: lead (protected), worker
    # Supervise the LEAD: a stale protected agent is warn_only - it warns and never
    # launches, so this exercises a Write-Warning path with no real Start-Process.
    config = {"agents": {"lead": {"auto_restart": True, "cli": "claude"}},
              "backoff": {"base_seconds": 30, "cap_seconds": 900,
                          "reset_after_seconds": 180},
              "suspect_warn_interval_seconds": 300, "launch_grace_seconds": 120}
    (s.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    ps1 = s.dir / "supervisor.ps1"
    # readiness state (past initial launch grace) + NO heartbeat written => stale.
    (s.dir / "supervisor-state.json").write_text(
        json.dumps({"agents": {"lead": _ready()}}), encoding="utf-8")

    def _once(*extra: str) -> str:
        r = subprocess.run(
            [shell, "-NoProfile", "-File", str(ps1), "-Once", *extra],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
            env=_checkout_runtime_env(),
        )
        return r.stdout + r.stderr

    noisy = _once()                                       # normal run -> warning prints
    quiet = _once("-Quiet")                               # quiet run -> console silent
    assert "lead" in noisy, f"expected a console warning for the stale protected lead; got {noisy!r}"
    assert "lead" not in quiet, f"-Quiet must suppress the warning console output; got {quiet!r}"


@pytest.mark.source_layout
def test_generated_ps1_quiet_suppresses_relaunch_helper_warnings(tmp_path: Path) -> None:
    """0.29.0 (reviewer-1 r2): -Quiet must also silence warnings emitted by the
    HELPER functions (Seed-CodexHome / Preflight / Launch) on the relaunch path,
    not just the loop's own Write-Warning lines. We drive a stale claude agent with
    the activity hook on (=> stuck_recover => relaunch path) and NO launch
    .windows_file, so a helper warns but NO real process is launched. Once without
    -Quiet a warning/log prints; once with -Quiet the console is silent - which is
    only true because the script sets $WarningPreference, not per-line guards."""
    shell = _pick_powershell()
    if not shell:
        return
    s = _team(tmp_path)
    config = {"agents": {"worker": {"auto_restart": True, "cli": "claude",
                                    "activity_hook": True}},
              "backoff": {"base_seconds": 30, "cap_seconds": 900,
                          "reset_after_seconds": 180},
              "suspect_warn_interval_seconds": 300, "launch_grace_seconds": 120}
    (s.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    _select_test_powershell(tmp_path, shell)
    ps1 = s.dir / "supervisor.ps1"
    state_file = s.dir / "supervisor-state.json"

    def _once(*extra: str) -> str:
        # RESET to the stale-but-ready state before EACH run so both deterministically
        # hit stuck_recover -> the relaunch path. (A prior normal run mutates state into
        # backoff, which would otherwise downgrade the second run to backoff_wait and
        # skip the helper-warning path - reviewer-1 r3 note.) readiness = past grace,
        # not in backoff; NO heartbeat written => stale; missing windows_file => a
        # HELPER warns with no real Start-Process.
        state_file.write_text(
            json.dumps({"agents": {"worker": _ready(backoff_next_epoch=0)}}),
            encoding="utf-8")
        r = subprocess.run(
            [shell, "-NoProfile", "-File", str(ps1), "-Once", *extra],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
            env=_checkout_runtime_env(),
        )
        return r.stdout + r.stderr

    noisy = _once()
    quiet = _once("-Quiet")
    assert "worker" in noisy, f"expected a relaunch-path warning/log for worker; got {noisy!r}"
    assert "worker" not in quiet, f"-Quiet must suppress relaunch-path helper warnings; got {quiet!r}"


def test_generated_ps1_quotes_args_with_spaces_as_single_arg(tmp_path: Path) -> None:
    """0.28.1 BLOCKER 2 (RUNTIME): the launcher must quote each token per Windows
    rules and hand Start-Process ONE command-line string, or an arg containing a
    space splits into two at the handoff. We extract the EXACT Quote-Arg function
    from the generated .ps1 and drive the SAME (Quote-Arg + -join + Start-Process)
    path the launcher uses, launching python with a space-containing arg from a
    cwd that ALSO contains spaces, and assert the child saw it as ONE argument."""
    shell = _pick_powershell()
    if not shell:
        return
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    ps1_text = (s.dir / "supervisor.ps1").read_text(encoding="utf-8-sig")
    start = ps1_text.index("# region quote-arg")
    end = ps1_text.index("# endregion quote-arg")
    quote_arg = ps1_text[start:end]                       # the verbatim function
    assert "function Quote-Arg" in quote_arg
    cwd_spaces = tmp_path / "dir with spaces"
    cwd_spaces.mkdir()
    out = tmp_path / "argv.json"
    py = __import__("sys").executable
    # python -c reads sys.argv[0]='-c', the rest are the passed tokens; the LAST
    # token is the output path. The launcher's exact arg path is reproduced below.
    code = ("import json,sys,os; p=sys.argv[-1]; "
            "open(p,'w').write(json.dumps({'argv': sys.argv[1:-1], 'cwd': os.getcwd()}))")

    def _ps_lit(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"

    harness = "\n".join([
        quote_arg,
        f"$py = {_ps_lit(py)}",
        f"$argv = @('-c', {_ps_lit(code)}, 'a b c', 'plain', {_ps_lit(str(out))})",
        "$argline = (@($argv) | ForEach-Object { Quote-Arg ([string]$_) }) -join ' '",
        f"$p = Start-Process -FilePath $py -ArgumentList $argline "
        f"-WorkingDirectory {_ps_lit(str(cwd_spaces))} -PassThru -Wait",
        "exit $p.ExitCode",
    ])
    harness_path = tmp_path / "quote_harness.ps1"
    harness_path.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(harness_path)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"harness failed: {res.stdout}{res.stderr}"
    data = json.loads(out.read_text(encoding="utf-8"))
    # the space-containing arg survived as ONE argument (not split into 'a','b','c')
    assert data["argv"] == ["a b c", "plain"], data["argv"]
    # and the spaced working directory was honored
    assert data["cwd"].replace("\\", "/").endswith("dir with spaces"), data["cwd"]


def test_manual_restart_marker_clears_only_after_readiness(tmp_path: Path) -> None:
    """Slice 1: applying a manual restart leaves the marker latched until a
    later fresh heartbeat/readiness tick. Launch success alone is not release."""
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    s.write_restart_request("worker", {"agent": "worker", **_auth_marker("rr-1")})
    rpt = sup.build_report(s, now_epoch=NOW)
    plan = sup.plan_actions(rpt, {"agents": {"worker": {"pid_alive": False}}},
                            {"agents": {"worker": {"auto_restart": True}}},
                            now_epoch=NOW)["agents"]["worker"]
    assert plan["action"] == sup.RELAUNCH and plan["clear_marker"] is None
    assert s.read_restart_request("worker") is not None
    (s.state_dir / "worker.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    clear = sup.plan_actions(
        sup.build_report(s, now_epoch=NOW),
        {"agents": {"worker": _ready(consumed_rids=["rr-1"], readiness_seen=True)}},
        {"agents": {"worker": {"auto_restart": True}}},
        now_epoch=NOW,
    )["agents"]["worker"]
    assert clear["action"] == sup.CLEAR_MARKER and clear["clear_marker"] == "rr-1"
    _run(["supervise", "--clear-restart", "--for", "worker",
          "--request-id", "rr-1"], tmp_path)
    assert s.read_restart_request("worker") is None


def _exec_helpers(tmp_path: Path) -> str:
    """Extract the verbatim exec-helpers (Proc-Start/Get-ProcSnapshot/Stop-Tree/
    Seed-CodexHome) from a freshly generated supervisor.ps1, so the runtime tests
    drive the EXACT shipped PowerShell."""
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    text = (s.dir / "supervisor.ps1").read_text(encoding="utf-8-sig")
    block = text[text.index("# region exec-helpers"):text.index("# endregion exec-helpers")]
    assert "function Stop-Tree" in block and "function Seed-CodexHome" in block
    return "function Assert-ActionsEnabled([string]$what) { return $true }\n" + block


def _pslit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def test_supervisor_launch_nonce_injection_powershell_helper(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    out = tmp_path / "nonce_injection.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        helpers,
        f"$nonce = {_pslit(SUPERVISOR_NONCE)}",
        "$py = Add-SupervisorLaunchNonce 'python.exe' @('-m','agenttalk','--root','R','wrap') $nonce",
        "$console = Add-SupervisorLaunchNonce 'agenttalk.exe' @('--root','R','wrap') $nonce",
        "$native = Add-SupervisorLaunchNonce 'codex.exe' @('exec') $nonce",
        "@{ py = $py; console = $console; native = $native } | ConvertTo-Json -Depth 6 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "nonce_injection.ps1"
    hp.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"{res.stdout}{res.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["py"]["injected"] is True
    assert data["py"]["argv"] == [
        "-m", "agenttalk", "--supervisor-launch-nonce", SUPERVISOR_NONCE,
        "--root", "R", "wrap",
    ]
    assert data["console"]["injected"] is True
    assert data["console"]["argv"] == [
        "--supervisor-launch-nonce", SUPERVISOR_NONCE, "--root", "R", "wrap",
    ]
    assert data["native"]["injected"] is False
    assert data["native"]["missing_reason"] == "unsupported_launch_argv"


def test_wrapped_launch_helper_inserts_root_before_wrap_for_legacy_configs(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    out = tmp_path / "wrap_root.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(str(tmp_path))}",
        helpers,
        "$legacyPy = Ensure-AgenttalkWrapRootArg @('-m','agenttalk','wrap',"
        "'--for','Cygnus','--cli','codex','--loop','--','codex.exe')",
        "$legacyConsole = Ensure-AgenttalkWrapRootArg @('wrap','--for',"
        "'Altair','--cli','codex','--loop','--','codex.exe')",
        "$alreadyRooted = Ensure-AgenttalkWrapRootArg @('-m','agenttalk','--root','R','wrap','--for','Vega','--loop')",
        "$nonWrap = Ensure-AgenttalkWrapRootArg @('-m','agenttalk','wait','--for','Cygnus')",
        "@{ legacyPy = $legacyPy; legacyConsole = $legacyConsole; "
        "alreadyRooted = $alreadyRooted; nonWrap = $nonWrap } | ConvertTo-Json -Depth 6 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "wrap_root.ps1"
    hp.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"{res.stdout}{res.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["legacyPy"][:5] == ["-m", "agenttalk", "--root", str(tmp_path), "wrap"]
    assert data["legacyConsole"][:3] == ["--root", str(tmp_path), "wrap"]
    assert data["alreadyRooted"][:5] == ["-m", "agenttalk", "--root", "R", "wrap"]
    assert data["nonWrap"] == ["-m", "agenttalk", "wait", "--for", "Cygnus"]


def test_launch_rechecks_kill_switch_after_branch_guard(tmp_path: Path) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    assert _run(["supervise", "--init"], tmp_path) == 0
    text = (s.dir / "supervisor.ps1").read_text(encoding="utf-8-sig")
    start = text.index("function Launch($name")
    end = text.index("function Launch-Spec", start)
    launch_fn = text[start:end]
    out = tmp_path / "launch_guard.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(str(tmp_path))}",
        f"$KillSwitchPath = {_pslit(str(s.dir / 'supervisor.kill'))}",
        "function Actions-Enabled { return -not (Test-Path $KillSwitchPath) }",
        "function Assert-ActionsEnabled([string]$what) { "
        "if (Actions-Enabled) { return $true }; return $false }",
        "function Start-Process { throw 'Start-Process should not run after kill switch' }",
        "$cfg = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{ cwd = $Root; env = $null; launch = "
        "[pscustomobject]@{ windows_file = 'dummy.exe'; windows_args = @() } } } }",
        "$AgenttalkPython = 'python'; $SrcOnPyPath = $false",
        "$branchOk = Assert-ActionsEnabled 'branch guard'",
        "New-Item -ItemType File -Force -Path $KillSwitchPath | Out-Null",
        launch_fn,
        "$plan = [pscustomobject]@{ session_id = $null; session_args = @(); "
        "launch_mode = 'fresh' }",
        "$res = Launch 'worker' $plan $null",
        f"@{{ branchOk = $branchOk; resultIsNull = ($null -eq $res); "
        f"killExists = (Test-Path $KillSwitchPath) }} | ConvertTo-Json | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "launch_guard.ps1"
    hp.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"{res.stdout}{res.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data == {"branchOk": True, "resultIsNull": True, "killExists": True}


def test_stop_tree_kills_real_two_level_tree_start_guarded(tmp_path: Path) -> None:
    """liveness redesign (RUNTIME): Stop-Tree must reap a real 2-level process
    tree leaves-first AND refuse a pid whose start-time no longer matches (the
    anti-pid-reuse guard, codex discipline note 2). Exercises the SHIPPED helper."""
    shell = _pick_powershell()
    if not shell:
        return
    import sys as _sys
    import time as _time
    helpers = _exec_helpers(tmp_path)
    py = _sys.executable
    pidfile = tmp_path / "childpid.txt"
    parent_code = ("import subprocess,sys,time;"
                   "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
                   "open(sys.argv[1],'w').write(str(c.pid));time.sleep(120)")
    parent = subprocess.Popen([py, "-c", parent_code, str(pidfile)])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            _time.sleep(0.05)
        child_pid = int(pidfile.read_text().strip())
        parent_pid = parent.pid
        out = tmp_path / "tree.json"
        # targets brain-FIRST (parent then child); Stop-Tree reverses => child
        # (leaf) dies first. A recycled-pid guard case: a bogus start never kills.
        harness = "\n".join([
            helpers,
            f"$parent = {parent_pid}; $child = {child_pid}",
            "$targets = @( @{ pid = $parent; start = (Proc-Start $parent) },"
            "             @{ pid = $child;  start = (Proc-Start $child) } )",
            # a guard target: real pid but WRONG start-time -> must NOT be killed
            "$guardAlive = (Proc-Start $PID) -ne $null",
            "$guard = @( @{ pid = $PID; start = 'WRONG-START' } )",
            "Stop-Tree $guard",
            "Start-Sleep -Milliseconds 200",
            "$guardStill = (Proc-Start $PID) -ne $null",
            "$missing = @( @{ pid = $PID } )",
            "Stop-Tree $missing",
            "Start-Sleep -Milliseconds 200",
            "$missingStill = (Proc-Start $PID) -ne $null",
            "Stop-Tree $targets",
            "Start-Sleep -Milliseconds 400",
            "$alive = @(); foreach($id in $parent,$child){ if(Get-Process -Id $id "
            "-ErrorAction SilentlyContinue){ $alive += $id } }",
            f"@{{ alive = $alive; guard_survived = ($guardStill -and $guardAlive); "
            f"missing_survived = ($missingStill -and $guardAlive) }} | "
            f"ConvertTo-Json | Set-Content {_pslit(str(out))} -Encoding utf8",
        ])
        hp = tmp_path / "tree_harness.ps1"
        hp.write_text(harness, encoding="utf-8-sig")
        res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                             capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, f"{res.stdout}{res.stderr}"
        data = json.loads(out.read_text(encoding="utf-8-sig"))
        alive = data["alive"]
        alive = [] if alive is None else ([alive] if isinstance(alive, int) else alive)
        assert alive == [], f"tree not fully reaped: {alive}"
        assert data["guard_survived"] is True   # start-time guard spared the recycled pid
        assert data["missing_survived"] is True  # missing start is never a kill target
    finally:
        try:
            parent.kill()
        except OSError:
            pass


def test_seed_codex_home_provisions_and_fails_closed(tmp_path: Path) -> None:
    """liveness redesign (RUNTIME): Seed-CodexHome provisions a per-agent isolated
    CODEX_HOME (auth.json + config.toml + skills/agenttalk-listen), NEVER shares
    sessions/, and FAILS CLOSED (returns nothing) when auth is missing."""
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    # a fake source CODEX_HOME with the required bits + a sessions/ that must NOT leak
    src = tmp_path / "src-codex"
    (src / "skills" / "agenttalk-listen").mkdir(parents=True)
    (src / "skills" / "agenttalk-listen" / "SKILL.md").write_text("listen", encoding="utf-8")
    (src / "sessions").mkdir()
    (src / "sessions" / "rollout-x.jsonl").write_text("{}", encoding="utf-8")
    (src / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
    (src / "config.toml").write_text("k = 1", encoding="utf-8")
    out = tmp_path / "seed.json"
    # Seed-CodexHome overlays config.toml via `& $AgenttalkCmd ... --seed-codex-config`,
    # so the harness must define $Root + the generated shim path.
    cmd_path = tmp_path / ".agenttalk" / "bin" / "agenttalk.cmd"
    preamble = [f"$Root = {_pslit(str(tmp_path))}",
                f"$AgenttalkCmd = {_pslit(str(cmd_path))}"]
    harness = "\n".join([
        helpers, *preamble,
        f"$env:CODEX_HOME = {_pslit(str(src))}",
        "$h = Seed-CodexHome 'codex-test' 'unelevated'",
        "$res = @{ home = $h;"
        " auth = (Test-Path (Join-Path $h 'auth.json'));"
        " config = (Test-Path (Join-Path $h 'config.toml'));"
        " skill = (Test-Path (Join-Path $h 'skills\\agenttalk-listen'));"
        " sessions_shared = (Test-Path (Join-Path $h 'sessions'));"
        " overlaid = ([bool](Select-String -Path (Join-Path $h 'config.toml')"
        " -Pattern 'approval_policy = \"never\"' -Quiet)) }",
        f"$res | ConvertTo-Json | Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "seed_harness.ps1"
    hp.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"{res.stdout}{res.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["home"], "seeding returned no home"
    assert data["auth"] and data["config"] and data["skill"]
    assert data["sessions_shared"] is False, "isolated home must NOT share sessions/"
    assert data["overlaid"], "config.toml must be overlaid with the unattended keys"
    # FAIL CLOSED: a source missing auth.json -> no home returned
    (src / "auth.json").unlink()
    out2 = tmp_path / "seed2.json"
    # use a DIFFERENT agent so the (already-seeded) home isn't reused
    harness2 = "\n".join([
        helpers, *preamble,
        f"$env:CODEX_HOME = {_pslit(str(src))}",
        "$h = Seed-CodexHome 'codex-test-2' 'unelevated'",
        f"@{{ home = $h }} | ConvertTo-Json | Set-Content {_pslit(str(out2))} -Encoding utf8",
    ])
    hp2 = tmp_path / "seed_harness2.ps1"
    hp2.write_text(harness2, encoding="utf-8-sig")
    r2 = subprocess.run([shell, "-NoProfile", "-File", str(hp2)],
                        capture_output=True, text=True, timeout=120)
    assert r2.returncode == 0, f"{r2.stdout}{r2.stderr}"
    d2 = json.loads(out2.read_text(encoding="utf-8-sig"))
    assert not d2["home"], "missing auth must FAIL CLOSED (no home)"


# ---------------------------------------- Phase C: wrapped:true (wrap --loop)
#
# A wrapped agent is supervised THROUGH `agenttalk wrap --loop`: the wrapper
# (python) IS the long-lived root, so brain discovery is retired (brain_pid
# stays None) but the per-turn child (codex exec / claude -p) is still reaped
# via the start-guarded managed_pids tree-kill. Because the wrapper is
# instrumented by construction (heartbeat on idle-wait + streaming progress), a
# wrapped agent treats a stale heartbeat as confirm-stuck and RECOVERS without
# the activity hook installed. Session continuity is owned by the wrapper, so
# the supervisor injects NO session args (launch_mode "wrap", session_args []).

WRAP_LAUNCHER_PID, WRAP_CHILD_PID = 300, 301

_WRAP_CONFIG = {
    "root": TEST_ROOT,
    "agents": {"worker": {"auto_restart": True, "cli": "codex", "wrapped": True}},
    "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
    "launch_grace_seconds": 120,
}


def _wrap_snap(*, cli="codex", launcher_pid=WRAP_LAUNCHER_PID,
               child_pid=WRAP_CHILD_PID, child_start="t-child",
               root: str = TEST_ROOT):
    """A wrapped-agent snapshot: the python wrapper (launcher) + its per-turn CLI
    child. The child is a real codex.exe/claude.exe row, so WITHOUT wrapping the
    brain-discovery would mistake it for the long-lived brain - the wrapped path
    must instead treat it as just a managed (reapable) descendant."""
    name = "codex.exe" if cli == "codex" else "claude.exe"
    if child_start == "t-child":
        child_start = WRAP_CHILD_START
    return [
        {"pid": launcher_pid, "parent_pid": 1, "name": "python.exe",
         "command_line": (
             "python -m agenttalk "
             f"--supervisor-launch-nonce {SUPERVISOR_NONCE} "
             f"--root {root} wrap --for worker --cli {cli} --loop"
         ),
         "start_time": WRAP_START},
        {"pid": child_pid, "parent_pid": launcher_pid, "name": name,
         "command_line": f"{name} exec --json", "start_time": child_start},
    ]


def _wrap_ready(**over) -> dict:
    st = {"launcher_pid": WRAP_LAUNCHER_PID, "launcher_start": WRAP_START,
          "launcher_nonce": SUPERVISOR_NONCE,
          "launcher_nonce_injected": True,
          "launcher_nonce_source": "agenttalk_global_arg",
          "readiness_seen": True, "launching": False,
          "last_launch_epoch": NOW - 1000}
    st.update(over)
    return st


def _plan_wrap(report, state, *, now=NOW, snapshot=None, config=_WRAP_CONFIG):
    snap = [] if snapshot is None else snapshot
    return sup.plan_actions(report, state, config, now_epoch=now,
                            snapshot=snap)["agents"]["worker"]


def test_wrapped_liveness_retires_brain_keeps_managed() -> None:
    snap = _wrap_snap()
    lv = sup._liveness(snap, _wrap_ready(), {"cli": "codex", "wrapped": True},
                       "worker", NOW, root_key=sup._root_key(TEST_ROOT))
    assert lv["brain_pid"] is None and lv["brain_start"] is None
    assert lv["discovered_brain"] is False
    # the per-turn child is still tracked for the start-guarded tree-kill
    pids = [m["pid"] for m in lv["managed_pids"]]
    assert WRAP_CHILD_PID in pids
    # CONTRAST: the SAME snapshot, NOT wrapped, discovers that codex.exe child as
    # the brain - proving the wrapped path is what suppresses it.
    lv2 = sup._liveness(snap, _wrap_ready(), {"cli": "codex"}, "worker", NOW)
    assert lv2["brain_pid"] == WRAP_CHILD_PID and lv2["discovered_brain"] is True


def test_wrapped_launch_detail_has_no_session_args() -> None:
    for cli_name in ("codex", "claude"):
        d = sup._launch_detail({"session_id": "x", "resume_available": True},
                               {"cli": cli_name, "wrapped": True})
        assert d["launch_mode"] == "wrap" and d["resume_mode"] == "wrap"
        assert d["session_args"] == [] and d["session_id"] is None


def test_wrapped_restart_on_stale_without_hook() -> None:
    # The crux: a wrapped codex agent has NO activity_hook, yet a stale heartbeat
    # past grace+backoff RECOVERS (the non-wrapped no-hook codex is SUSPECT_WARN -
    # see test_stuck_recover_requires_hook_else_suspect).
    p = _plan_wrap(_report(heartbeat_stale=True),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap())
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"
    # the wrapper owns session: no fresh/resume branch, no session tokens
    assert p["launch_mode"] == "wrap" and p["session_args"] == []
    # the relaunch reaps the wrapper (launcher) AND the live per-turn child,
    # every target start-guarded against pid reuse
    assert p["kill_first"] is True
    tpids = {t["pid"] for t in p["kill_targets"]}
    assert WRAP_LAUNCHER_PID in tpids and WRAP_CHILD_PID in tpids
    assert all("start" in t for t in p["kill_targets"])
    assert p["next_state"]["brain_pid"] is None


def test_wrapped_config_blocked_hold_marker_holds_when_stale_until_restart() -> None:
    hold = {"agent": "worker", "state": "config_blocked"}
    p = _plan_wrap(_report(heartbeat_stale=True, config_blocked_hold=hold),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap())
    assert p["action"] == sup.NONE and p["state"] == "CONFIG_BLOCKED"
    assert p["kill_first"] is False
    assert p["kill_targets"] == []
    assert "config_blocked" in p["reason"]

    marker = _auth_marker("rr-config-fix")
    p2 = _plan_wrap(_report(heartbeat_stale=True, config_blocked_hold=hold,
                            restart_request=marker),
                    {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                    snapshot=_wrap_snap())
    assert p2["action"] == sup.RELAUNCH and p2["state"] == "MANUAL_RESTART"
    assert p2["clear_marker"] is None
    assert p2["kill_first"] is True


def _write_config_blocked_health(store: Store, agent: str, at_epoch: float) -> None:
    store.write_health(agent, hm.build_snapshot(
        agent=agent,
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_ERRORED_AMBIGUOUS,
        updated_at=_iso(at_epoch),
        since=_iso(at_epoch),
        last_progress_at=None,
        reason_code="config_blocked",
    ))


def _write_raw_config_blocked_hold(store: Store, agent: str, payload: object) -> None:
    p = store.config_blocked_hold_path(agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def test_config_blocked_hold_marker_survives_health_ttl_end_to_end(tmp_path: Path) -> None:
    s = _team(tmp_path)
    (s.state_dir / "worker.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    _write_config_blocked_health(s, "worker", NOW)
    s.write_config_blocked_hold("worker", summary="command=codex; error=shim")

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    worker = report["agents"]["worker"]
    assert worker["heartbeat_stale"] is True
    assert worker["health"]["state"] == hm.STATE_UNKNOWN
    assert worker["health"]["reason_code"] == "health_stale_ttl"
    assert worker["config_blocked_hold"]["agent"] == "worker"

    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                            _WRAP_CONFIG, now_epoch=NOW + 2500,
                            snapshot=_wrap_snap(root=str(tmp_path)))["agents"]["worker"]
    assert plan["action"] == sup.NONE and plan["state"] == "CONFIG_BLOCKED"
    assert plan["kill_first"] is False
    assert plan["kill_targets"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"agent": "other", "state": "config_blocked"},
        {"agent": "worker", "state": "wrong_state"},
        {"agent": "worker"},
        [],
    ],
)
def test_malformed_config_blocked_hold_marker_does_not_suppress_recovery(
    tmp_path: Path,
    payload: object,
) -> None:
    s = _team(tmp_path)
    (s.state_dir / "worker.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    _write_config_blocked_health(s, "worker", NOW)
    _write_raw_config_blocked_hold(s, "worker", payload)

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    assert report["agents"]["worker"].get("config_blocked_hold") is None
    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                            _WRAP_CONFIG, now_epoch=NOW + 2500,
                            snapshot=_wrap_snap(root=str(tmp_path)))["agents"]["worker"]
    assert plan["action"] == sup.STUCK_RECOVER and plan["state"] == "STUCK_OR_DEAD"
    assert plan["kill_first"] is True


def test_config_blocked_hold_request_restart_overrides_and_clear_preserves_hold(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    (s.state_dir / "worker.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    _write_config_blocked_health(s, "worker", NOW)
    s.write_config_blocked_hold("worker", summary="command=codex; error=shim")
    s.write_restart_request("worker", {"agent": "worker", **_auth_marker("rr-fix")})

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                            _WRAP_CONFIG, now_epoch=NOW + 2500,
                            snapshot=_wrap_snap(root=str(tmp_path)))["agents"]["worker"]
    assert plan["action"] == sup.RELAUNCH and plan["state"] == "MANUAL_RESTART"
    assert plan["clear_marker"] is None

    assert _run(["supervise", "--clear-restart", "--for", "worker",
                 "--request-id", "rr-fix"], tmp_path) == 0
    assert s.read_restart_request("worker") is None
    assert s.read_config_blocked_hold("worker") is not None


def test_wrapped_codex_without_config_blocked_marker_still_recovers_end_to_end(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    (s.state_dir / "worker.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    _write_config_blocked_health(s, "worker", NOW)

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    assert report["agents"]["worker"].get("config_blocked_hold") is None
    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                            _WRAP_CONFIG, now_epoch=NOW + 2500,
                            snapshot=_wrap_snap(root=str(tmp_path)))["agents"]["worker"]
    assert plan["action"] == sup.STUCK_RECOVER and plan["state"] == "STUCK_OR_DEAD"
    assert plan["kill_first"] is True


def test_wrapped_claude_also_recovers_on_stale_without_hook() -> None:
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "claude",
                                 "wrapped": True}}}
    p = _plan_wrap(_report(heartbeat_stale=True),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(cli="claude"), config=cfg)
    assert p["action"] == sup.STUCK_RECOVER and p["session_args"] == []


def test_wrapped_in_grace_does_not_request_brain_discovery() -> None:
    st = {"agents": {"worker": _wrap_ready(launching=True, readiness_seen=False,
                                           launch_grace_until=NOW + 60)}}
    p = _plan_wrap(_report(heartbeat_stale=True), st, snapshot=_wrap_snap())
    assert p["action"] == sup.NONE and p["state"] == "LAUNCHING"
    assert p["discover_brain"] is False
    # CONTRAST: a non-wrapped agent in grace DOES ask the executor to discover.
    st2 = {"agents": {"worker": _ready(launching=True, readiness_seen=False,
                                       launch_grace_until=NOW + 60)}}
    p2 = _plan(_report(heartbeat_stale=True), st2, snapshot=_snap())
    assert p2["state"] == "LAUNCHING" and p2["discover_brain"] is True


def test_wrapped_healthy_idle_keeps_brain_none() -> None:
    # a fresh heartbeat is healthy; the carried-through brain stays None (no
    # discovery to preserve) while managed_pids still tracks the child.
    p = _plan_wrap(_report(heartbeat_stale=False),
                   {"agents": {"worker": _wrap_ready()}}, snapshot=_wrap_snap())
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"
    assert p["next_state"]["brain_pid"] is None
    assert [m["pid"] for m in p["next_state"]["managed_pids"]] == [WRAP_CHILD_PID]


def test_config_template_ships_wrapped_example() -> None:
    cfg = json.loads(sup.CONFIG_TEMPLATE)
    w = cfg["agents"]["AGENT_NAME_WRAPPED"]
    assert w["wrapped"] is True
    # the codex sample ships a threshold ABOVE the per-turn watchdog deadline +
    # margin (v0.46.0: 1800 + 300 -> default 2400) so the supervisor never preempts
    # the watchdog; it also ships a turn_watchdog block (default-ON for wrapped codex).
    assert w["stuck_after_seconds"] == 2400
    assert w["turn_watchdog"]["enabled"] is True
    assert w["turn_watchdog"]["turn_elapsed_seconds"] == 1800
    args = w["launch"]["windows_args"]
    assert "{SESSION_ARGS}" not in args          # wrapper owns session; no splice
    assert args[:5] == ["-m", "agenttalk", "--root", "{ROOT}", "wrap"] and "--loop" in args
    # 0.31.2: the wrapped codex child is launched with `--disable hooks` by default
    # (the wrapper owns the heartbeat; sidesteps the codex hook-trust prompt).
    assert args[-2:] == ["--disable", "hooks"]
    # steer-to-wrapped: the comment marks wrapped recommended/default + non-wrapped legacy
    assert "RECOMMENDED" in w["_comment_wrapped"] and "LEGACY" in w["_comment_wrapped"]


def test_config_template_keeps_deadman_out_of_supervisor_config() -> None:
    assert "deadman" not in json.loads(sup.CONFIG_TEMPLATE)


# ---------- per-CLI wrapped stuck_after defaults + the codex low-threshold guardrail

def test_resolve_stuck_after_per_cli_defaults_and_override() -> None:
    # per-CLI wrapped defaults
    assert sup.resolve_stuck_after({}, {"cli": "claude", "wrapped": True}) == 180.0
    assert sup.resolve_stuck_after({}, {"cli": "codex", "wrapped": True}) == 2400.0
    # an explicit per-agent override always wins
    assert sup.resolve_stuck_after(
        {}, {"cli": "codex", "wrapped": True, "stuck_after_seconds": 1500}) == 1500.0
    # non-wrapped keeps the global behavior (config, then the built-in default)
    assert sup.resolve_stuck_after({"stuck_after_seconds": 77}, {"cli": "codex"}) == 77.0
    assert sup.resolve_stuck_after({}, {"cli": "codex"}) == 120.0


def test_wrapped_codex_default_threshold_tolerates_reasoning_gap() -> None:
    # A 300s silent pure-reasoning gap is STALE under the global 120s threshold,
    # but a wrapped codex re-derives against its 2400s default -> NOT stale -> the
    # planner does NOT false-kill it mid-reasoning.
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=300.0),
                   {"agents": {"worker": _wrap_ready()}}, snapshot=_wrap_snap())
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"


def test_wrapped_codex_low_stuck_after_refuses_restart() -> None:
    # An UNSAFE-low threshold (< 600s floor) without opt-in: REFUSE restart
    # authority (warn-only) + notify - never silently coerce to 900.
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex",
                                 "wrapped": True, "stuck_after_seconds": 120}}}
    p = _plan_wrap(_report(heartbeat_stale=True),
                   {"agents": {"worker": _wrap_ready(last_warn_epoch=0,
                                                     backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.SUSPECT_WARN and p["state"] == "ACTIVE_OR_BUSY"
    assert p["kill_first"] is False and p["notify"] is True
    assert "allow_low_stuck_after" in p["reason"]


def test_wrapped_codex_low_stuck_after_opt_in_restarts() -> None:
    # The operator explicitly opts in -> the low threshold is honored and
    # restart-on-stale is restored.
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex",
                                 "wrapped": True, "stuck_after_seconds": 120,
                                 "allow_low_stuck_after": True}}}
    p = _plan_wrap(_report(heartbeat_stale=True),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.STUCK_RECOVER


def test_wrapped_claude_has_no_codex_floor() -> None:
    # the < 600s guardrail is codex-only; a wrapped claude with a tight threshold
    # is honored (it stays fresh through reasoning via deltas).
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "claude",
                                 "wrapped": True, "stuck_after_seconds": 120}}}
    p = _plan_wrap(_report(heartbeat_stale=True),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(cli="claude"), config=cfg)
    assert p["action"] == sup.STUCK_RECOVER


# ---------- the per-turn watchdog TIMING-INVARIANT guard (v0.46.0, gate #3)

def test_wrapped_codex_stuck_after_below_watchdog_deadline_refuses_restart() -> None:
    # stuck_after=1500 is ABOVE the 600s floor but <= turn_elapsed(1800)+margin(300)=2100,
    # so the supervisor would PREEMPT the per-turn watchdog (relaunch into the same wedge).
    # REFUSE restart-on-stale (warn-only) with the watchdog-specific reason.
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex",
                                 "wrapped": True, "stuck_after_seconds": 1500}}}
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
                   {"agents": {"worker": _wrap_ready(last_warn_epoch=0,
                                                     backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.SUSPECT_WARN and p["kill_first"] is False
    assert "turn watchdog deadline" in p["reason"]


def test_wrapped_codex_above_watchdog_deadline_recovers() -> None:
    # the DEFAULT wrapped-codex stuck_after (2400) sits above 2100 -> NOT preempted -> a
    # genuinely stale wrapper still recovers normally.
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap())
    assert p["action"] == sup.STUCK_RECOVER


def test_wrapped_codex_watchdog_guard_opt_out_restarts() -> None:
    # allow_low_stuck_after=true opts out of BOTH the floor guard and the watchdog guard.
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex", "wrapped": True,
                                 "stuck_after_seconds": 1500, "allow_low_stuck_after": True}}}
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.STUCK_RECOVER


def test_wrapped_codex_watchdog_disabled_uses_only_floor_guard() -> None:
    # turn_watchdog.enabled=false -> no watchdog -> the timing guard does NOT apply; a
    # stuck_after of 1500 (above the 600 floor) recovers normally.
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex", "wrapped": True,
                                 "stuck_after_seconds": 1500,
                                 "turn_watchdog": {"enabled": False}}}}
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.STUCK_RECOVER


def test_wrapped_codex_subfloor_watchdog_disabled_supervisor_still_recovers() -> None:
    # Issue B (reviewer-1 repro): enabled=true but turn_elapsed=1100 (< 1200 floor) with NO
    # allow_low_turn_elapsed -> the WRAPPER disables the watchdog. The supervisor must NOT
    # then refuse restart-on-stale (else the wedge is silent with NO recovery). Both layers
    # share watchdog_effectively_live, so the planner sees it as NOT live -> recovers on stale.
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex", "wrapped": True,
                                 "stuck_after_seconds": 1300,
                                 "turn_watchdog": {"enabled": True,
                                                   "turn_elapsed_seconds": 1100}}}}
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.STUCK_RECOVER             # never left with neither


def test_wrapped_codex_subfloor_watchdog_with_optin_preempts() -> None:
    # the SAME sub-floor turn_elapsed but WITH allow_low_turn_elapsed -> the watchdog IS live,
    # so the supervisor again refuses restart-on-stale (the watchdog will handle the wedge).
    cfg = {**_WRAP_CONFIG,
           "agents": {"worker": {"auto_restart": True, "cli": "codex", "wrapped": True,
                                 "stuck_after_seconds": 1300,
                                 "turn_watchdog": {"enabled": True, "turn_elapsed_seconds": 1100,
                                                   "allow_low_turn_elapsed": True}}}}
    p = _plan_wrap(_report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
                   {"agents": {"worker": _wrap_ready(last_warn_epoch=0, backoff_next_epoch=0)}},
                   snapshot=_wrap_snap(), config=cfg)
    assert p["action"] == sup.SUSPECT_WARN and "turn watchdog deadline" in p["reason"]


def test_config_template_documents_watchdog_and_provenance() -> None:
    # gate #4/#5 doc tests: the wrapped-codex template must surface the per-turn watchdog,
    # its known limitation, and the codex_home_isolation config-provenance risk.
    cfg = json.loads(sup.CONFIG_TEMPLATE)
    w = cfg["agents"]["AGENT_NAME_WRAPPED"]
    wd_comment = w["_comment_turn_watchdog"]
    assert "two-factor" in wd_comment.lower() and "KNOWN LIMITATION" in wd_comment
    prov = w["_comment_codex_home_isolation_provenance"]
    assert "approval_policy=never" in prov and "MCP" in prov and "headless" in prov.lower()
    # the timing-invariant is documented in the stuck_after comment
    assert "watchdog" in w["_comment_wrapped_stuck_after"].lower()


def _ownership_report(stale: bool = True) -> dict:
    rpt = _report(heartbeat_stale=stale)
    rpt["root_key"] = sup._root_key(TEST_ROOT)
    return rpt


def _ownership_state(**over) -> dict:
    st = _wrap_ready(backoff_next_epoch=0)
    st.update(over)
    if (
        isinstance(st.get("launcher_pid"), int)
        and isinstance(st.get("launcher_start"), str)
        and "launcher_nonce_injected" not in over
    ):
        st["launcher_nonce"] = SUPERVISOR_NONCE
        st["launcher_nonce_injected"] = True
        st["launcher_nonce_source"] = "agenttalk_global_arg"
    return {"agents": {"worker": st}}


def _proc(pid: int, parent: int, name: str, command_line: str | None,
          start: str) -> dict:
    return {
        "pid": pid,
        "parent_pid": parent,
        "name": name,
        "command_line": command_line,
        "start_time": start,
    }


def _wrap_cmd(*, root: str = TEST_ROOT, agent: str = "worker",
              nonce: str = SUPERVISOR_NONCE) -> str:
    return (
        "python -m agenttalk "
        f"--supervisor-launch-nonce {nonce} "
        f"--root {root} wrap --for {agent} --loop"
    )


def test_process_ownership_iso_epoch_parses_real_powershell_o_and_strict_edges() -> None:
    parent_start = "2026-07-04T07:20:31.5767870+00:00"
    child_start = "2026-07-04T07:20:31.5767880+00:00"
    assert sup._iso_epoch(parent_start) is not None
    assert sup._iso_epoch(child_start) > sup._iso_epoch(parent_start)

    snap = [
        _proc(
            10, 1, "python.exe",
            _wrap_cmd(),
            parent_start,
        ),
        _proc(11, 10, "codex.exe", "codex exec --json", child_start),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=parent_start),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10, 11}

    for bad_start, counter in [
        (parent_start, "equal_start_edge"),
        ("2026-07-04T07:20:31.5767860+00:00", "inverted_start_edge"),
        (None, "unparseable_start_edge"),
        ("not-a-date", "unparseable_start_edge"),
    ]:
        bad_snap = [snap[0], {**snap[1], "start_time": bad_start}]
        p_bad = sup.plan_actions(
            _ownership_report(),
            _ownership_state(launcher_pid=10, launcher_start=parent_start),
            _WRAP_CONFIG,
            now_epoch=NOW,
            snapshot=bad_snap,
        )["agents"]["worker"]
        assert {t["pid"] for t in p_bad["kill_targets"]} == {10}
        assert p_bad["diagnostics"][counter] == 1


def test_process_ownership_strict_live_chain_reaps_multi_hop_when_ordered() -> None:
    snap = [
        _proc(10, 1, "python.exe", _wrap_cmd(), _ps_iso(100000)),
        _proc(11, 10, "codex.exe", "codex exec --json", _ps_iso(200000)),
        _proc(12, 11, "node.exe", "node build.js", _ps_iso(300000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert [t["pid"] for t in p["kill_targets"]] == [10, 11, 12]
    assert p["kill_targets"][1]["reason"] == "live_chain_descendant"
    assert p["kill_targets"][2]["reason"] == "live_chain_descendant"


def test_process_ownership_equal_start_graft_needs_independent_provenance() -> None:
    start = _ps_iso(100000)
    snap = [
        _proc(10, 1, "python.exe", _wrap_cmd(), start),
        _proc(11, 10, "codex.exe", "codex exec --json", start),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=start),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10}
    assert p["diagnostics"]["equal_start_edge"] == 1


def test_process_ownership_launch_time_same_tick_child_absent_from_baseline_is_provenanced() -> None:
    start = _ps_iso(100000)
    pre = [_proc(10, 1, "python.exe", _wrap_cmd(), start)]
    post = [*pre, _proc(11, 10, "codex.exe", "codex exec --json", start)]
    state = {"agents": {"worker": {}}}
    sup.record_launch(
        state,
        "worker",
        cli="codex",
        pid=10,
        pid_start=start,
        now_epoch=NOW,
        pre_snapshot=pre,
        post_snapshot=post,
        cfg_agent={"cli": "codex", "wrapped": True},
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )
    managed = state["agents"]["worker"]["managed_pids"]
    assert managed[0]["source"] == "launch_child_provenance"
    assert managed[0]["pid"] == 11
    assert managed[0]["seed_descendants"] is True

    p = sup.plan_actions(_ownership_report(), state, _WRAP_CONFIG,
                         now_epoch=NOW + 1, snapshot=post)["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10, 11}
    assert any(t["source"] == "launch_child_provenance" for t in p["kill_targets"])


def test_process_ownership_launch_time_same_tick_child_present_in_baseline_is_not_provenanced() -> None:
    start = _ps_iso(100000)
    child = _proc(11, 10, "codex.exe", "codex exec --json", start)
    pre = [
        _proc(10, 1, "python.exe", _wrap_cmd(), start),
        child,
    ]
    state = {"agents": {"worker": {}}}
    sup.record_launch(
        state,
        "worker",
        cli="codex",
        pid=10,
        pid_start=start,
        now_epoch=NOW,
        pre_snapshot=pre,
        post_snapshot=pre,
        cfg_agent={"cli": "codex", "wrapped": True},
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )
    assert state["agents"]["worker"]["managed_pids"] == []
    p = sup.plan_actions(_ownership_report(), state, _WRAP_CONFIG,
                         now_epoch=NOW + 1, snapshot=pre)["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10}


def test_process_ownership_stale_ppid_grafts_are_excluded_single_and_multi_hop() -> None:
    snap = [
        _proc(10, 1, "python.exe", _wrap_cmd(), _ps_iso(300000)),
        _proc(11, 10, "codex.exe", "codex exec --json", _ps_iso(200000)),
        _proc(12, 11, "node.exe", "node build.js", _ps_iso(400000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(300000)),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10}
    assert p["diagnostics"]["inverted_start_edge"] == 1


def test_process_ownership_branch_cuts_same_root_foreign_shell_and_generic_cli() -> None:
    snap = [
        _proc(10, 1, "python.exe", _wrap_cmd(), _ps_iso(100000)),
        _proc(11, 10, "python.exe", f"python -m agenttalk --root {TEST_ROOT} wait --for other", _ps_iso(200000)),
        _proc(12, 10, "python.exe", f"python -m agenttalk --root {TEST_ROOT} send --to worker -m hi", _ps_iso(210000)),
        _proc(13, 10, "python.exe", "python -m agenttalk --root D:\\other wrap --for worker --loop", _ps_iso(220000)),
        _proc(14, 10, "python.exe", f"python -m agenttalk --root {TEST_ROOT} frob", _ps_iso(230000)),
        _proc(15, 10, "cmd.exe", "cmd.exe /c something", _ps_iso(240000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10}
    assert p["diagnostics"]["same_root_other_agent_branch"] >= 1
    assert p["diagnostics"]["unknown_root_cli"] >= 2
    assert p["diagnostics"]["foreign_root_branch"] >= 1
    assert p["diagnostics"]["shell_boundary"] >= 1


def test_process_ownership_root_wait_brain_stops_at_windows_shell_hosts() -> None:
    for shell_name in ["conhost.exe", "WindowsTerminal.exe", "powershell.exe",
                       "cmd.exe", "OpenConsole.exe", "explorer.exe"]:
        snap = [
            _proc(5, 1, shell_name, shell_name, _ps_iso(100000)),
            _proc(6, 5, "codex.exe", "codex tui", _ps_iso(200000)),
            _proc(7, 6, "python.exe", f"python -m agenttalk --root {TEST_ROOT} wait --for worker", _ps_iso(300000)),
        ]
        p = sup.plan_actions(
            _ownership_report(),
            {"agents": {"worker": _ready(backoff_next_epoch=0)}},
            _HOOK_CODEX_CONFIG,
            now_epoch=NOW,
            snapshot=snap,
        )["agents"]["worker"]
        assert {t["pid"] for t in p["kill_targets"]} == {6, 7}
        assert p["diagnostics"]["shell_boundary"] >= 1


def test_process_ownership_parse_agenttalk_wrap_fail_closed_matrix() -> None:
    good = f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex"
    assert sup.parse_agenttalk_wrap_invocation(good, sup._root_key(TEST_ROOT), "worker") is True
    good_nonce = (
        f"python -m agenttalk --supervisor-launch-nonce {SUPERVISOR_NONCE} "
        f"--root {TEST_ROOT} wrap --for worker --loop -- codex"
    )
    assert sup.parse_agenttalk_wrap_invocation(
        good_nonce, sup._root_key(TEST_ROOT), "worker") is True
    assert sup.parse_agenttalk_wait_invocation(
        f"python -m agenttalk --supervisor-launch-nonce {SUPERVISOR_NONCE} "
        f"--root {TEST_ROOT} wait --for worker",
        sup._root_key(TEST_ROOT),
        "worker",
    ) is True
    bad = [
        f"cmd.exe /c python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop",
        "python -m agenttalk wrap --for worker --loop",
        f"python -m agenttalk wrap --root {TEST_ROOT} --for worker --loop",
        f"python -m agenttalk --root {TEST_ROOT} wrap --supervisor-launch-nonce {SUPERVISOR_NONCE} --for worker --loop",
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker -- --loop",
        f"python -m agenttalk --root \"{TEST_ROOT} wrap --for worker --loop",
        "python -m agenttalk --root D:\\other wrap --for worker --loop",
        f"python -m agenttalk --root {TEST_ROOT} wrap --for other --loop",
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker",
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker -- codex --loop",
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop "
        f"-- codex --supervisor-launch-nonce {SUPERVISOR_NONCE}",
    ]
    for command_line in bad:
        assert sup.parse_agenttalk_wrap_invocation(
            command_line, sup._root_key(TEST_ROOT), "worker") is False
    assert sup.parse_agenttalk_wait_invocation(
        f"python -m agenttalk --root {TEST_ROOT} wait --supervisor-launch-nonce {SUPERVISOR_NONCE} --for worker",
        sup._root_key(TEST_ROOT),
        "worker",
    ) is False


def test_process_ownership_launcher_pid_reuse_cannot_be_rescued_by_wrap_text() -> None:
    snap = [
        _proc(10, 1, "python.exe", f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop", _ps_iso(200000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["kill_targets"] == []
    assert p["diagnostics"]["pid_reuse_suppressed"] == 1


def _launcher_plan(command_line: str | None, *, name: str = "python.exe",
                   state_over: dict | None = None) -> dict:
    start = _ps_iso(100000)
    state_fields = {"launcher_pid": 10, "launcher_start": start}
    if state_over:
        state_fields.update(state_over)
    snap = [_proc(10, 1, name, command_line, start)]
    return sup.plan_actions(
        _ownership_report(),
        _ownership_state(**state_fields),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]


def test_process_ownership_confirmed_launcher_requires_branch_clean_wrapper_nonce() -> None:
    legitimate = [
        _wrap_cmd(),
        f"python -m agenttalk --root {TEST_ROOT} --supervisor-launch-nonce {SUPERVISOR_NONCE} wrap --for worker --loop",
        f"python -m agenttalk --supervisor-launch-nonce={SUPERVISOR_NONCE} --root {TEST_ROOT} wrap --for worker --loop",
        f"agenttalk --supervisor-launch-nonce {SUPERVISOR_NONCE} --root {TEST_ROOT} wrap --for worker --loop",
    ]
    for command_line in legitimate:
        name = "agenttalk.exe" if command_line.startswith("agenttalk ") else "python.exe"
        p = _launcher_plan(command_line, name=name)
        assert [t["pid"] for t in p["kill_targets"]] == [10]
        assert p["kill_targets"][0]["reason"] == "confirmed_launcher"

    for command_line in [
        _wrap_cmd(root=r"D:\foreign-root"),
        _wrap_cmd(agent="other"),
    ]:
        p = _launcher_plan(command_line)
        assert p["kill_targets"] == []
        assert p["diagnostics"]["foreign_launcher_suppressed"] == 1


def test_process_ownership_launcher_nonce_blocks_generic_and_unreadable_collisions() -> None:
    generic = _launcher_plan("notepad.exe --some-arg", name="notepad.exe")
    assert generic["kill_targets"] == []
    assert generic["diagnostics"]["launcher_wrap_parse_failed"] == 1

    unreadable = _launcher_plan(None)
    assert unreadable["kill_targets"] == []
    assert unreadable["diagnostics"]["launcher_nonce_cmdline_unreadable"] == 1


def test_process_ownership_launcher_nonce_fail_closed_matrix() -> None:
    cases = [
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop",
            "launcher_nonce_absent",
        ),
        (_wrap_cmd(nonce=OTHER_NONCE), "launcher_nonce_mismatch"),
        (
            f"python -m agenttalk --supervisor-launch-nonce short --root {TEST_ROOT} wrap --for worker --loop",
            "launcher_nonce_malformed",
        ),
        (
            "python -m agenttalk "
            f"--supervisor-launch-nonce {SUPERVISOR_NONCE} "
            f"--supervisor-launch-nonce {OTHER_NONCE} "
            f"--root {TEST_ROOT} wrap --for worker --loop",
            "launcher_nonce_duplicate",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap "
            f"--supervisor-launch-nonce {SUPERVISOR_NONCE} --for worker --loop",
            "launcher_nonce_after_subcommand_or_tail",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop "
            f"-- codex --supervisor-launch-nonce {SUPERVISOR_NONCE}",
            "launcher_nonce_after_subcommand_or_tail",
        ),
    ]
    for command_line, counter in cases:
        p = _launcher_plan(command_line)
        assert p["kill_targets"] == []
        assert p["diagnostics"][counter] == 1


def test_process_ownership_pre_upgrade_launcher_without_nonce_is_cleanup_miss_not_cross_kill() -> None:
    p = _launcher_plan(
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop",
        state_over={"launcher_nonce_injected": False},
    )
    assert p["kill_targets"] == []
    assert p["diagnostics"]["launcher_nonce_missing_state"] == 1


def test_process_ownership_unsupported_launch_argv_records_unusable_nonce_state() -> None:
    state = {"agents": {"worker": {}}}
    sup.record_launch(
        state,
        "worker",
        cli="codex",
        pid=10,
        pid_start=_ps_iso(100000),
        now_epoch=NOW,
        cfg_agent={"cli": "codex"},
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce_injected=False,
        launcher_nonce_missing_reason="unsupported_launch_argv",
    )
    entry = state["agents"]["worker"]
    assert entry["launcher_nonce_injected"] is False
    assert entry["launcher_nonce_missing_reason"] == "unsupported_launch_argv"
    assert "launcher_nonce" not in entry

    p = sup.plan_actions(
        _ownership_report(),
        state,
        _WRAP_CONFIG,
        now_epoch=NOW + 1,
        snapshot=[_proc(10, 1, "codex.exe", "codex exec", _ps_iso(100000))],
    )["agents"]["worker"]
    assert p["kill_targets"] == []
    assert p["diagnostics"]["launcher_nonce_unsupported_argv"] == 1


def test_process_ownership_ephemeral_launch_spec_records_and_checks_nonce() -> None:
    state = {
        "ephemeral_reviewers": {
            "active": {
                "R1": {
                    "request_id": "R1",
                    "agent": "worker",
                    "phase": eph.STATE_REQUESTED,
                }
            }
        }
    }
    start = _ps_iso(100000)
    sup.record_ephemeral_launch(
        state,
        "R1",
        pid=10,
        pid_start=start,
        now_epoch=NOW,
        timeout_seconds=1,
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )
    entry = state["ephemeral_reviewers"]["active"]["R1"]
    assert entry["launcher_nonce"] == SUPERVISOR_NONCE
    assert entry["launcher_nonce_injected"] is True

    report = {
        "root_key": sup._root_key(TEST_ROOT),
        "agents": {},
        "launch_requests": [],
        "ephemeral_reviewers": {"active": {"R1": {}}},
    }
    snap = [_proc(10, 1, "python.exe", _wrap_cmd(), start)]
    plan = sup.plan_actions(report, state, _WRAP_CONFIG,
                            now_epoch=NOW + 2, snapshot=snap)
    timeout = plan["ephemeral_reviewers"]["R1"]
    assert timeout["action"] == eph.ACTION_TIMEOUT
    assert [t["pid"] for t in timeout["kill_targets"]] == [10]

    missing = json.loads(json.dumps(state))
    missing["ephemeral_reviewers"]["active"]["R1"].pop("launcher_nonce")
    missing["ephemeral_reviewers"]["active"]["R1"]["launcher_nonce_injected"] = False
    suppressed = sup.plan_actions(report, missing, _WRAP_CONFIG,
                                  now_epoch=NOW + 2, snapshot=snap)
    timeout_suppressed = suppressed["ephemeral_reviewers"]["R1"]
    assert timeout_suppressed["kill_targets"] == []
    diag = missing["ephemeral_reviewers"]["active"]["R1"]["provenance_diagnostics"]
    assert diag["launcher_nonce_missing_state"] == 1


def test_process_ownership_provenanced_prior_exact_fields_request_and_ttl() -> None:
    base = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 11,
        "start": _ps_iso(200000),
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 10,
        "last_fresh_attribution_epoch": NOW - 10,
        "seed_descendants": False,
        "source_launcher_pid": 10,
        "source_launcher_start": _ps_iso(100000),
        "source_launcher_nonce": SUPERVISOR_NONCE,
    }
    snap = [_proc(11, 1, "codex.exe", "codex exec --json", _ps_iso(200000))]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(base))],
        ),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["kill_targets"] == [{"pid": 11, "start": _ps_iso(200000),
                                  "reason": "launch_child_provenance",
                                  "source": "launch_child_provenance"}]
    p_fresh = sup.plan_actions(
        _ownership_report(stale=False),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(base))],
        ),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p_fresh["next_state"]["managed_pids"][0]["last_fresh_attribution_epoch"] == NOW - 10

    mismatch = {**base, "request_id": "R1"}
    p_bad = sup.plan_actions(
        _ownership_report(),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[mismatch],
        ),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p_bad["kill_targets"] == []
    assert p_bad["diagnostics"]["prior_request_mismatch"] == 1

    missing = dict(base)
    missing.pop("request_id")
    expired = {**base, "last_fresh_attribution_epoch": NOW - 4000}
    p_missing = sup.plan_actions(
        _ownership_report(),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[missing, expired],
        ),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p_missing["kill_targets"] == []
    assert p_missing["diagnostics"]["prior_field_missing"] == 1
    assert p_missing["diagnostics"]["prior_ttl_expired"] == 1


def test_process_ownership_stale_launcher_prior_does_not_rescue_row_without_nonce() -> None:
    prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 10,
        "start": _ps_iso(100000),
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 10,
        "last_fresh_attribution_epoch": NOW - 10,
        "seed_descendants": True,
        "source_launcher_pid": 10,
        "source_launcher_start": _ps_iso(100000),
        "source_launcher_nonce": SUPERVISOR_NONCE,
    }
    state = _ownership_state(
        launcher_pid=10,
        launcher_start=_ps_iso(100000),
        managed_pids=[prior],
    )
    snap = [
        _proc(
            10, 1, "python.exe",
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop",
            _ps_iso(100000),
        )
    ]
    p = sup.plan_actions(_ownership_report(), state, _WRAP_CONFIG,
                         now_epoch=NOW, snapshot=snap)["agents"]["worker"]
    assert p["kill_targets"] == []
    assert p["next_state"]["managed_pids"] == []
    assert p["diagnostics"]["launcher_nonce_absent"] == 1


def test_process_ownership_prior_source_specific_launcher_nonce_requirement() -> None:
    launcher_prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 11,
        "start": _ps_iso(200000),
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 10,
        "last_fresh_attribution_epoch": NOW - 10,
        "seed_descendants": False,
    }
    wait_prior = {
        **launcher_prior,
        "pid": 12,
        "start": _ps_iso(300000),
        "source": "first_confirmed_child_provenance",
    }
    snap = [
        _proc(11, 1, "codex.exe", "codex exec", _ps_iso(200000)),
        _proc(12, 1, "python.exe", f"python -m agenttalk --root {TEST_ROOT} wait --for worker", _ps_iso(300000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(managed_pids=[launcher_prior, wait_prior]),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["diagnostics"]["prior_field_missing"] == 1
    assert {t["pid"] for t in p["kill_targets"]} == {12}


def test_process_ownership_legacy_managed_pids_rederive_only_when_freshly_attributable() -> None:
    snap = [
        _proc(10, 1, "python.exe", _wrap_cmd(), _ps_iso(100000)),
        _proc(11, 10, "codex.exe", "codex exec --json", _ps_iso(200000)),
        _proc(12, 99, "node.exe", "node old.js", _ps_iso(200000)),
    ]
    state = _ownership_state(
        launcher_pid=10,
        launcher_start=_ps_iso(100000),
        managed_pids=[
            {"pid": 11, "start": _ps_iso(200000), "kind": "legacy"},
            {"pid": 12, "start": _ps_iso(200000), "kind": "legacy"},
        ],
    )
    p = sup.plan_actions(_ownership_report(stale=False), state, _WRAP_CONFIG,
                         now_epoch=NOW, snapshot=snap)["agents"]["worker"]
    managed = {m["pid"]: m for m in p["next_state"]["managed_pids"]}
    assert managed[11]["source"] == "legacy_rederived"
    assert 12 not in managed
    assert p["diagnostics"]["legacy_unverifiable_dropped"] == 1


def test_process_ownership_drifted_launcher_targets_cmdline_matched_wrapper() -> None:
    snap = [
        _proc(22, 1, "python.exe", _wrap_cmd(), _ps_iso(220000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["kill_targets"] == [{
        "pid": 22,
        "start": _ps_iso(220000),
        "reason": "own_wrapper",
        "source": "own_wrapper",
    }]


def test_launch_barrier_duplicate_orphan_wrapper_blocks_replacement() -> None:
    snap = [
        _proc(22, 1, "python.exe", _wrap_cmd(), _ps_iso(220000)),
    ]
    result = sup.evaluate_launch_barrier(
        snap,
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
    )
    assert result["blocked"] is True
    assert result["allow_launch"] is False
    assert result["reason"] == "same_agent_wrapper_survived"
    assert result["survivor_count"] == 1
    assert result["survivors"] == [{"kind": "own_wrapper", "pid": 22, "name": "python.exe"}]


def test_launch_barrier_same_agent_wait_survivor_blocks_replacement() -> None:
    snap = [
        _proc(31, 1, "python.exe",
              f"python -m agenttalk --root {TEST_ROOT} wait --for worker",
              _ps_iso(310000)),
    ]
    result = sup.evaluate_launch_barrier(
        snap,
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
    )
    assert result["blocked"] is True
    assert result["reason"] == "same_agent_wait_survived"


def test_launch_barrier_clear_snapshot_allows_replacement() -> None:
    snap = [
        _proc(44, 1, "python.exe",
              f"python -m agenttalk --root {TEST_ROOT} wrap --for other --loop",
              _ps_iso(440000)),
    ]
    result = sup.evaluate_launch_barrier(
        snap,
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
    )
    assert result["allow_launch"] is True
    assert result["blocked"] is False
    assert result["reason"] == "clear"


def test_launch_barrier_snapshot_unavailable_does_not_stack_prior_wrapper() -> None:
    blocked = sup.evaluate_launch_barrier(
        None,
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
    )
    assert blocked["blocked"] is True
    assert blocked["reason"] == "snapshot_unavailable_prior_maybe_alive"

    first_launch = sup.evaluate_launch_barrier(
        None,
        {"agents": {"worker": {}}},
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
    )
    assert first_launch["allow_launch"] is True


def test_launch_barrier_state_enters_backoff_without_fake_launch_grace() -> None:
    state = {"agents": {"worker": _wrap_ready(
        consecutive_fails=0,
        backoff_next_epoch=0,
        launch_grace_until=0,
    )}}
    p = _plan_wrap(
        _report(heartbeat_stale=True),
        state,
        snapshot=_wrap_snap(),
    )

    assert p["action"] == sup.STUCK_RECOVER
    assert p["next_state"]["launching"] is True
    barrier = p["barrier_state"]
    assert barrier["launching"] is False
    assert barrier["readiness_seen"] is True
    assert barrier["launch_grace_until"] == 0.0
    assert barrier["consecutive_fails"] == 1
    assert barrier["backoff_next_epoch"] == NOW + 30

    p2 = _plan_wrap(
        _report(heartbeat_stale=True),
        {"agents": {"worker": barrier}},
        now=NOW + 1,
        snapshot=_wrap_snap(),
    )
    assert p2["action"] == sup.BACKOFF_WAIT
    assert p2["state"] == "STUCK_OR_DEAD"


def test_launch_barrier_state_preserves_manual_restart_until_spawn() -> None:
    marker = _auth_marker("rr-barrier")
    p = _plan_wrap(
        _report(heartbeat_stale=True, restart_request=marker),
        {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
        snapshot=_wrap_snap(),
    )

    assert p["action"] == sup.RELAUNCH
    assert "rr-barrier" in p["next_state"]["consumed_rids"]
    barrier = p["barrier_state"]
    assert "rr-barrier" not in barrier["consumed_rids"]
    assert barrier["launching"] is False
    assert barrier.get("restart_request_state") != "applied_pending_readiness"

    p2 = _plan_wrap(
        _report(heartbeat_stale=True, restart_request=marker),
        {"agents": {"worker": barrier}},
        now=NOW + 1,
        snapshot=_wrap_snap(),
    )
    assert p2["action"] == sup.RELAUNCH
    assert p2["state"] == "MANUAL_RESTART"
    assert p2["clear_marker"] is None


def test_launch_barrier_event_is_deduped(tmp_path: Path) -> None:
    s = _team(tmp_path)
    sup.record_supervisor_launch_barrier_event(
        s, "worker",
        reason_code="same_agent_wrapper_survived",
        now_epoch=NOW,
    )
    sup.record_supervisor_launch_barrier_event(
        s, "worker",
        reason_code="same_agent_wrapper_survived",
        now_epoch=NOW + 1,
    )
    events, warnings = sup.read_supervisor_events(s)
    assert warnings == []
    decisions = [e for e in events if e.get("kind") == "agent_decision"]
    assert len(decisions) == 1
    assert decisions[0]["action"] == "launch_barrier"
    assert decisions[0]["reason_code"] == "same_agent_wrapper_survived"


def test_launch_barrier_event_dedupes_across_generated_plan_sequence(tmp_path: Path) -> None:
    s = _team(tmp_path)
    plan = {
        "agents": {
            "worker": {
                "action": sup.STUCK_RECOVER,
                "state": "STUCK_OR_DEAD",
                "reason": "stale heartbeat",
                "notify": True,
            },
        },
    }

    for i in range(2):
        sup.record_supervisor_plan_events(
            s, plan,
            now_epoch=NOW + (i * 2),
            summary_interval_seconds=999999,
        )
        sup.record_supervisor_launch_barrier_event(
            s, "worker",
            reason_code="same_agent_wrapper_survived",
            now_epoch=NOW + (i * 2) + 1,
        )

    events, warnings = sup.read_supervisor_events(s)
    assert warnings == []
    decisions = [e for e in events if e.get("kind") == "agent_decision"]
    assert [e["action"] for e in decisions] == [
        sup.STUCK_RECOVER,
        "launch_barrier",
    ]


def test_supervise_launch_barrier_cli_reports_block_and_records_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    state_file = s.dir / "supervisor-state.json"
    snapshot_file = s.dir / "supervisor-barrier-worker.json"
    state_file.write_text(
        json.dumps(_ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000))),
        encoding="utf-8",
    )
    snapshot_file.write_text(
        json.dumps([_proc(22, 1, "python.exe", _wrap_cmd(root=str(tmp_path)), _ps_iso(220000))]),
        encoding="utf-8",
    )

    rc = _run([
        "supervise", "--launch-barrier",
        "--for", "worker",
        "--state-file", str(state_file),
        "--snapshot-file", str(snapshot_file),
        "--record-events",
        "--now", str(NOW),
    ], tmp_path)

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["blocked"] is True
    assert result["reason"] == "same_agent_wrapper_survived"
    events, warnings = sup.read_supervisor_events(s)
    assert warnings == []
    assert [e["action"] for e in events if e.get("kind") == "agent_decision"] == [
        "launch_barrier",
    ]


def test_ps_template_rechecks_launch_barrier_after_stop_tree_before_launch() -> None:
    ps = sup.PS_TEMPLATE
    block_start = ps.index("if (($p.kill_first -or $p.kill_orphans)")
    block_end = ps.index("# SEED the agent", block_start)
    block = ps[block_start:block_end]
    assert block.index("Stop-Tree $p.kill_targets") < block.index("--launch-barrier")
    assert "Get-ProcSnapshot $barrierPath" in block
    assert "Write-Warning" in block
    if "Set-AgentState $state $name $p.barrier_state" not in block:
        pytest.fail("launch-barrier state update bypasses Set-AgentState")
    assert "continue" in block
    launch_idx = ps.index("$res = Launch $name", block_end)
    assert block_end < launch_idx


def test_ps_template_routes_dynamic_agent_state_writes_through_helper() -> None:
    ps = sup.PS_TEMPLATE
    if "function Set-AgentState($state, $name, $value)" not in ps:
        pytest.fail("generated supervisor has no Set-AgentState helper")
    if "$state.agents.$name =" in ps:
        pytest.fail("generated supervisor still dot-assigns dynamic agent state")
    if ps.count("$state.agents | Add-Member") != 1:
        pytest.fail("agent-state properties must be created only by Set-AgentState")


def test_ps_template_refreshes_config_before_each_poll() -> None:
    ps = sup.PS_TEMPLATE
    loop = ps[ps.index("do {"):ps.index("} while (-not $Once)")]
    refresh = "$nextCfg = Read-SupervisorConfig"
    if refresh not in loop:
        pytest.fail("poll uses stale supervisor config for hot-added agents")
    if loop.index(refresh) > loop.index("$state = Load-State"):
        pytest.fail("supervisor config must refresh before poll state initialization")
    before_state = loop[:loop.index("$state = Load-State")]
    if "try {" not in before_state or "catch {" not in before_state:
        pytest.fail("per-poll config refresh is not guarded")
    if "keeping last-good config" not in before_state:
        pytest.fail("config refresh failure does not preserve last-good behavior")


def test_process_ownership_stop_tree_closed_set_pin() -> None:
    ps = sup.PS_TEMPLATE
    block = ps[ps.index("function Stop-Tree"):ps.index("function Seed-CodexHome")]
    assert "Get-CimInstance" not in block
    assert "ParentProcessId" not in block
    assert "foreach ($t in $arr)" in block


def test_report_parity_wrapped_codex_uses_per_cli_threshold(tmp_path: Path) -> None:
    # REPORT PARITY: the operator-facing build_report must match the planner's
    # per-CLI decision (the operator watches the supervisor console during the
    # dogfood). A 300s-old heartbeat is STALE under the global 120s default but
    # FRESH under the wrapped-codex 2400s threshold - the report must show fresh.
    s = _team(tmp_path)
    s.write_heartbeat("worker")
    hb_ts = s.read_heartbeat("worker").timestamp()
    sup_cfg = {"agents": {"worker": {"cli": "codex", "wrapped": True}}}
    # global view (no supervisor_config): stale at 120s
    glob = sup.build_report(s, now_epoch=hb_ts + 300, stuck_after_seconds=120)
    assert glob["agents"]["worker"]["heartbeat_stale"] is True
    # parity view (supervisor_config passed): fresh under the per-CLI 2400s
    parity = sup.build_report(s, now_epoch=hb_ts + 300, stuck_after_seconds=120,
                              supervisor_config=sup_cfg)
    assert parity["agents"]["worker"]["heartbeat_stale"] is False
    assert parity["agents"]["worker"]["stuck_after_seconds"] == 2400.0
    # a non-wrapped agent in the same report keeps the global threshold
    assert parity["agents"]["lead"]["stuck_after_seconds"] == 120.0


# ---------- 0.31.1: codex hook-trust bypass for non-wrapped codex + activity_hook

def test_session_args_codex_activity_hook_injects_bypass_hook_trust() -> None:
    """A non-wrapped codex with the activity hook installed gets the GLOBAL
    --dangerously-bypass-hook-trust PREPENDED (before the subcommand), so the
    changed hook hash never strands an unattended launch on a trust prompt."""
    fresh = sup.session_args("codex", "fresh", None, {"activity_hook": True})
    assert fresh[0] == "--dangerously-bypass-hook-trust"   # global flag, before -a / prompt
    resume = sup.session_args("codex", "resume", None, {"activity_hook": True})
    assert resume[0] == "--dangerously-bypass-hook-trust"  # before the `resume` subcommand
    assert resume[1] == "resume"
    # no activity hook -> no installed hook -> no prompt to bypass -> flag ABSENT
    assert "--dangerously-bypass-hook-trust" not in sup.session_args(
        "codex", "fresh", None, {"activity_hook": False})
    assert "--dangerously-bypass-hook-trust" not in sup.session_args(
        "codex", "fresh", None, None)
    # claude is unaffected (codex-only)
    assert "--dangerously-bypass-hook-trust" not in sup.session_args(
        "claude", "fresh", "sid", {"activity_hook": True})
    # idempotent: an operator who already put it in a session override is not doubled
    over = {"activity_hook": True,
            "session": {"fresh": ["--dangerously-bypass-hook-trust", "-a", "never", "x"]}}
    assert sup.session_args("codex", "fresh", None, over).count(
        "--dangerously-bypass-hook-trust") == 1


def test_plan_nonwrapped_codex_hook_relaunch_carries_bypass() -> None:
    cfg = {"agents": {"worker": {"auto_restart": True, "cli": "codex",
                                 "activity_hook": True}},
           "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
           "launch_grace_seconds": 120}
    p = sup.plan_actions(_report(heartbeat_stale=True),
                         {"agents": {"worker": _ready(backoff_next_epoch=0)}},
                         cfg, now_epoch=NOW, snapshot=_snap())["agents"]["worker"]
    assert p["action"] == sup.STUCK_RECOVER
    assert "--dangerously-bypass-hook-trust" in p["session_args"]


def test_plan_wrapped_codex_has_no_bypass_flag() -> None:
    # wrapped codex owns its session (session_args=[]); no hook, no bypass flag.
    p = _plan_wrap(_report(heartbeat_stale=True),
                   {"agents": {"worker": _wrap_ready(backoff_next_epoch=0)}},
                   snapshot=_wrap_snap())
    assert "--dangerously-bypass-hook-trust" not in (p["session_args"] or [])


# ---------- 0.31.2: readiness give-up cap (no infinite churn on never-ready resume)

def _never_ready(**over) -> dict:
    """State for an agent that LAUNCHED but never produced a first heartbeat
    (launching, readiness never seen, grace expired)."""
    st = {"launching": True, "readiness_seen": False, "launch_grace_until": NOW - 1,
          "backoff_next_epoch": 0, "last_launch_epoch": NOW - 1000}
    st.update(over)
    return st


def test_readiness_cap_relaunches_below_cap_then_gives_up() -> None:
    # below the cap (readiness_fails=2 < default 3): still relaunches, counter ++.
    p2 = _plan_hook(_report(heartbeat_stale=True),
                    {"agents": {"worker": _never_ready(readiness_fails=2)}}, snapshot=[])
    assert p2["action"] == sup.STUCK_RECOVER
    assert p2["next_state"]["readiness_fails"] == 3
    # AT the cap (3 >= 3): GIVE UP - no relaunch, no kill, manual intervention.
    p3 = _plan_hook(_report(heartbeat_stale=True),
                    {"agents": {"worker": _never_ready(readiness_fails=3, last_warn_epoch=0)}},
                    snapshot=[])
    assert p3["action"] == sup.READINESS_GAVE_UP and p3["state"] == "READINESS_GAVE_UP"
    assert p3["kill_first"] is False and p3["notify"] is True
    assert p3["next_state"]["readiness_fails"] == 3          # sticky (not incremented)
    # sticky + warn rate-limited: a recent warn -> NONE this tick, still GAVE_UP state
    p3b = _plan_hook(_report(heartbeat_stale=True),
                     {"agents": {"worker": _never_ready(readiness_fails=3, last_warn_epoch=NOW - 1)}},
                     snapshot=[])
    assert p3b["action"] == sup.NONE and p3b["state"] == "READINESS_GAVE_UP"


def test_readiness_cap_resets_on_fresh_heartbeat() -> None:
    # a fresh heartbeat THIS tick = readiness reached -> counter clears, healthy.
    p = _plan_hook(_report(heartbeat_stale=False),
                   {"agents": {"worker": _never_ready(readiness_fails=3)}}, snapshot=[])
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"
    assert p["next_state"]["readiness_fails"] == 0


def test_manual_restart_clears_readiness_give_up() -> None:
    # operator restart-request resets the give-up counter (a way out of GAVE_UP).
    p = _plan_hook(_report(heartbeat_stale=True, restart_request=_auth_marker("rr-x")),
                   {"agents": {"worker": _never_ready(readiness_fails=9,
                                                      backoff_next_epoch=NOW + 9999)}},
                   snapshot=[])
    assert p["action"] == sup.RELAUNCH and p["next_state"]["readiness_fails"] == 0


def test_stuck_recovery_of_ready_agent_is_not_immediately_capped() -> None:
    # a previously-READY agent that just went stale is a normal stuck-recovery; its
    # first relaunch starts the readiness counter at 1 (not capped on tick one).
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": _ready(backoff_next_epoch=0, readiness_fails=0)}},
                   snapshot=_snap(cli="claude"))
    assert p["action"] == sup.STUCK_RECOVER and p["next_state"]["readiness_fails"] == 1


def _stub_cmd(path: Path, log: Path) -> None:
    """A fake CLI executable: append its args to ``log`` and exit 0. Lets the
    Preflight RUNTIME test assert WHICH invocation ran without a real codex/python."""
    path.write_text(f'@echo off\r\n>>"{log}" echo %*\r\nexit /b 0\r\n', encoding="utf-8")


def test_preflight_wrapped_codex_validates_python_not_codex_sandbox(tmp_path: Path) -> None:
    """reviewer-1 P1 (RUNTIME): for a wrapped agent windows_file is PYTHON, not the
    CLI, so Preflight must smoke-test `& $file -m agenttalk --version` and NOT
    `& $file sandbox ...` - otherwise `python.exe sandbox` exits nonzero and the
    wrapped:true launch fails closed before Launch(). Drives the EXACT shipped
    Preflight (extracted from the generated .ps1)."""
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)        # includes function Preflight
    wlog, clog = tmp_path / "wrap.log", tmp_path / "codex.log"
    wstub, cstub = tmp_path / "pywrap.cmd", tmp_path / "codexcli.cmd"
    native_codex = tmp_path / "codex.exe"
    _stub_cmd(wstub, wlog)
    _stub_cmd(cstub, clog)
    native_codex.write_text("", encoding="utf-8")
    out = tmp_path / "pf.json"
    preamble = [
        f"$Root = {_pslit(str(tmp_path))}",
        "$SrcOnPyPath = $false",
        f"$AgenttalkPython = {_pslit(str(wstub))}",
        (
            "$cfg = @{ agents = @{ 'wrapped-codex' = @{ launch = @{ "
            f"windows_args = @('-m','agenttalk','wrap','--loop','--',{_pslit(str(native_codex))}) "
            "} } } }"
        ),
    ]
    harness = "\n".join([
        helpers, *preamble,
        # wrapped codex: $file is the python wrapper stub; launch_mode 'wrap'
        f"$wrapOk = Preflight 'wrapped-codex' (@{{ cli='codex'; launch_mode='wrap' }}) {_pslit(str(wstub))} $null",
        # non-wrapped codex (0.31.1): must NOT call `$file sandbox ...` - it runs the
        # AGENTTALK_PY gate, so the $file stub is never invoked as a codex CLI
        # (its log stays empty / has no 'sandbox').
        f"$codexOk = Preflight 'plain-codex' (@{{ cli='codex'; launch_mode='resume' }}) {_pslit(str(cstub))} $null",
        # only the return values via JSON; the stubs logged their argv to files we
        # read directly in Python (Get-Content -Raw decorates the string, which
        # ConvertTo-Json then mangles into an object).
        "@{ wrapOk=$wrapOk; codexOk=$codexOk } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "pf_harness.ps1"
    hp.write_text(harness, encoding="utf-8-sig")
    res = subprocess.run([shell, "-NoProfile", "-File", str(hp)],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"{res.stdout}{res.stderr}"
    d = json.loads(out.read_text(encoding="utf-8-sig"))
    wrap_args = wlog.read_text(encoding="utf-8") if wlog.exists() else ""
    codex_args = clog.read_text(encoding="utf-8") if clog.exists() else ""
    # wrapped codex: preflight PASSES, ran `-m agenttalk --version`, NEVER `sandbox`
    assert d["wrapOk"] is True
    assert "-m agenttalk --version" in wrap_args
    assert "sandbox" not in wrap_args
    # non-wrapped codex (0.31.1): the drift-prone `$file sandbox ...` probe is GONE -
    # the $file stub is never invoked, so its log has no 'sandbox'. (codexOk depends
    # on the ambient python having agenttalk importable, so it is not asserted.)
    assert "sandbox" not in codex_args


# ----------------------------------------- WP2: lead-loop controller exit-marker rules

def test_lead_loop_stood_down_marker_suppresses_relaunch() -> None:
    # A stale wrapped controller that exited via a VALID human release/end leaves a
    # `stood_down` exit marker -> the supervisor must NOT relaunch it (else auto_restart
    # defeats the v0.39 stand-down). Without the marker the same stale state relaunches.
    p = _plan(_report(heartbeat_stale=True, lead_loop_exit={"state": "stood_down"}),
              {"agents": {"worker": _ready()}}, snapshot=_snap(),
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.NONE and p["state"] == "LEAD_LOOP_STOOD_DOWN"


def test_lead_loop_blocked_marker_holds_only_while_owner_live() -> None:
    # A `blocked` exit marker HOLDs (no relaunch) ONLY while the incumbent is a LIVE
    # guarding owner (lead_loop view armed) - relaunching would fight the live owner.
    p = _plan(_report(heartbeat_stale=True, lead_loop_exit={"state": "blocked"},
                      lead_loop={"armed": True, "owner_liveness": "alive", "present": True}),
              {"agents": {"worker": _ready()}}, snapshot=_snap(),
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.NONE and p["state"] == "LEAD_LOOP_BLOCKED"


def test_lead_loop_blocked_marker_recovers_when_owner_dead() -> None:
    # lead verify P2: once the blocking owner DIES/wedges (lead_loop view not armed), a
    # stale `blocked` marker must NOT permanently HOLD - the block has cleared, so the
    # controller RELAUNCHES + takes over (auto-recovery). Without the liveness gate this
    # was a permanent HOLD reachable under default config (wrapped stuck_after < TTL).
    p = _plan(_report(heartbeat_stale=True, lead_loop_exit={"state": "blocked"},
                      lead_loop={"armed": False, "owner_liveness": "dead", "present": True}),
              {"agents": {"worker": _ready()}}, snapshot=_snap(),
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"


def test_lead_loop_crash_no_marker_still_relaunches() -> None:
    # A CRASH writes NO exit marker -> the stale controller still relaunches (recovery);
    # this is the contrast that proves the marker is what suppresses relaunch.
    p = _plan(_report(heartbeat_stale=True),  # no lead_loop_exit
              {"agents": {"worker": _ready()}}, snapshot=_snap(),
              config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK_OR_DEAD"


def test_lead_loop_manual_restart_overrides_stood_down_marker() -> None:
    # An operator request-RESTART (section 0, highest priority) overrides the stand-down
    # HOLD: the operator deliberately wants the controller back -> RELAUNCH.
    marker = _auth_marker("rr-ll")
    p = _plan(_report(heartbeat_stale=True, restart_request=marker,
                      lead_loop_exit={"state": "stood_down"}),
              {"agents": {"worker": _ready(backoff_next_epoch=NOW + 9999)}},
              snapshot=_snap(), config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.RELAUNCH and p["state"] == "MANUAL_RESTART"


# --------------------------------------------- UTF-8 BOM tolerance (PowerShell 5.1)

def test_seed_codex_config_tolerates_bom_only_placeholder(tmp_path: Path) -> None:
    """A codex-home config.toml seeded by Windows PowerShell 5.1
    `Set-Content '' -Encoding utf8` is a BOM-only file. `supervise --seed-codex-config`
    must read it with utf-8-sig so the BOM does not propagate into the overlaid config
    the external codex CLI parses — a leading BOM + duplicate [projects] tables make the
    agent fail to start (the live mismatch from the 2026-07-14 incident audit)."""
    _team(tmp_path)
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "config.toml").write_bytes(b"\xef\xbb\xbf")          # BOM-only placeholder
    rc = _run(["supervise", "--seed-codex-config", "--home", str(home),
               "--repo", str(tmp_path)], tmp_path)
    assert rc == 0
    raw = (home / "config.toml").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "overlaid config.toml must not carry a BOM"
    text = (home / "config.toml").read_text(encoding="utf-8")
    # managed overlay applied cleanly, not skewed/duplicated by a leading BOM
    assert text.count('approval_policy = "never"') == 1
    assert "[sandbox_workspace_write]" in text
    # re-seed is idempotent: still exactly one managed root key (no BOM re-injected)
    assert _run(["supervise", "--seed-codex-config", "--home", str(home),
                 "--repo", str(tmp_path)], tmp_path) == 0
    text2 = (home / "config.toml").read_text(encoding="utf-8")
    assert text2.count('approval_policy = "never"') == 1
    assert not (home / "config.toml").read_bytes().startswith(b"\xef\xbb\xbf")


def test_seed_claude_settings_tolerates_bom_and_preserves_operator_keys(tmp_path: Path) -> None:
    """An operator .claude/settings.json saved by Notepad/PowerShell can carry a UTF-8
    BOM. `supervise --seed-claude-settings` must read it with utf-8-sig; otherwise
    json.loads raises, `existing` is dropped, and the operator's settings are SILENTLY
    DISCARDED instead of merged (data loss)."""
    _team(tmp_path)
    d = tmp_path / "proj"
    (d / ".claude").mkdir(parents=True)
    settings = d / ".claude" / "settings.json"
    settings.write_bytes(b"\xef\xbb\xbf" + b'{"customOperatorKey": 123}')
    rc = _run(["supervise", "--seed-claude-settings", "--dir", str(d)], tmp_path)
    assert rc == 0
    data = json.loads(settings.read_text(encoding="utf-8-sig"))
    assert data.get("customOperatorKey") == 123, "operator key must survive, not be discarded"
    assert "defaultMode" in data


def test_install_activity_hook_preserves_bom_prefixed_operator_settings(tmp_path: Path) -> None:
    """An operator .claude/settings.json saved with a UTF-8 BOM must be READ and MERGED,
    not treated as unreadable (which skips the hook install and forces manual merge).
    v0.75.3/D-26: the hook installer reads with utf-8-sig."""
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_bytes(b"\xef\xbb\xbf" + b'{"customOperatorKey": 42}')
    out = sup.install_activity_hook(s, claude=True, codex=False)
    st = out[str(settings)]
    assert "unreadable" not in st                      # was "skipped (unreadable...)" before the fix
    assert st in ("installed", "already")
    data = json.loads(settings.read_text(encoding="utf-8-sig"))
    assert data.get("customOperatorKey") == 42         # operator key preserved through the merge


def test_generated_supervisor_ps1_uses_bom_free_json_writes(tmp_path: Path) -> None:
    """Guard against reintroducing `Set-Content -Encoding utf8` (which emits a UTF-8 BOM
    under Windows PowerShell 5.1) for JSON/TOML writes: the generated supervisor.ps1 must
    write process snapshots and the codex config.toml seed via BOM-free WriteAllText
    (v0.75.3, D-26)."""
    _team(tmp_path)
    assert _run(["supervise", "--init"], tmp_path) == 0
    ps1 = (tmp_path / ".agenttalk" / "supervisor.ps1").read_text(encoding="utf-8-sig")
    assert "WriteAllText" in ps1
    assert "Set-Content $path -Encoding utf8" not in ps1          # snapshot writes
    assert "Set-Content $dc '' -Encoding utf8" not in ps1         # config.toml empty seed


def test_seed_codex_config_repairs_semantically_equal_duplicate_tables(tmp_path: Path) -> None:
    """The LAUNCH seed (`supervise --seed-codex-config`) must self-heal the REAL pre-fix
    corruption: an operator's DOUBLE-quoted [projects."<repo>"] header PLUS agenttalk's
    canonical LITERAL-quoted [projects.'<repo>'] header for the SAME normalized path —
    different spellings, one TOML table, so tomllib rejects the second. The collapse must
    match SEMANTICALLY (not byte-identically) and be SCOPED to the seeded project, else the
    wrapped Codex agent on an affected machine still can't start (codex-reviewer-1 P1,
    v0.75.3). Regression drives the EXACT seed command."""
    from agenttalk import codex_config as cxc
    _team(tmp_path)
    home = tmp_path / "codexhome"
    home.mkdir()
    cfg = home / "config.toml"
    cxc.enable_project(cfg, tmp_path)                       # canonical literal-quoted block
    literal = cfg.read_text(encoding="utf-8-sig")
    norm = cxc._normalize_path(tmp_path)
    double_hdr = '[projects."' + norm.replace("\\", "\\\\").replace('"', '\\"') + '"]'
    # operator double-quoted header for the SAME path, then agenttalk's literal-quoted block
    cfg.write_text(f'{double_hdr}\ntrust_level = "trusted"\n\n' + literal, encoding="utf-8")
    assert cfg.read_text(encoding="utf-8-sig").count("[projects.") == 2   # semantically-equal dup
    rc = _run(["supervise", "--seed-codex-config", "--home", str(home),
               "--repo", str(tmp_path)], tmp_path)
    assert rc == 0
    healed = cfg.read_text(encoding="utf-8")
    assert healed.count("[projects.") == 1                 # SEMANTICALLY collapsed to one table
    try:                                                   # and it parses as valid TOML now
        import tomllib
        tomllib.loads(healed)                              # must not raise "Cannot declare ... twice"
    except ModuleNotFoundError:
        pass                                               # tomllib is 3.11+; count check suffices on 3.10


def test_codex_config_status_reports_duplicate_tables(tmp_path: Path, capsys) -> None:
    """`codex-config --status` must SURFACE a duplicated (invalid-TOML) config instead
    of printing section_present=True as if healthy (codex-reviewer-1 P1, v0.75.3)."""
    from agenttalk import codex_config as cxc
    _team(tmp_path)
    cfg = tmp_path / "config.toml"
    proj = tmp_path / "proj"
    proj.mkdir()
    cxc.enable_project(cfg, proj)
    block = cfg.read_text(encoding="utf-8-sig")
    cfg.write_text(block.strip("\n") + "\n\n" + block.strip("\n") + "\n", encoding="utf-8")
    rc = _run(["codex-config", "--status", "--project", str(proj),
               "--config-path", str(cfg)], tmp_path)
    assert rc == 0
    assert "duplicate_sections" in capsys.readouterr().out


def test_install_activity_hook_preserves_bom_prefixed_codex_hooks(tmp_path: Path) -> None:
    """The .codex/hooks.json branch must also read a BOM'd operator file, not skip it as
    unreadable (codex-reviewer-1 P2, v0.75.3)."""
    s = _team(tmp_path)
    hooks = s.root / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_bytes(b"\xef\xbb\xbf" + b'{"customOperatorKey": 7}')
    out = sup.install_activity_hook(s, claude=False, codex=True)
    st = out[str(hooks)]
    assert "unreadable" not in st
    data = json.loads(hooks.read_text(encoding="utf-8-sig"))
    assert data.get("customOperatorKey") == 7
