"""WP-2: the agent supervisor's Python core.

The safety table (plan_actions) is the heart of the feature — it must be
CI-testable WITHOUT launching terminals, so these tests drive it via plain
fixtures. The generated PS/bash scripts are thin executors (documented-manual).
"""

from __future__ import annotations

import json
from pathlib import Path

from agenttalk import cli, supervisor as sup
from agenttalk.store import Store


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _team(tmp_path: Path, agents: str = "lead,worker") -> Store:
    s = Store(tmp_path)
    s.init(agents.split(","))
    return s


NOW = 1_000_000.0
_CONFIG = {
    "agents": {"worker": {"auto_restart": True}},
    "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
    "suspect_warn_interval_seconds": 300,
}


def _report(**agent_fields) -> dict:
    base = {"protected": False, "heartbeat_stale": False, "waiting_pid_alive": False,
            "restart_request": None}
    base.update(agent_fields)
    return {"agents": {"worker": base}}


def _plan(report, state, *, now=NOW):
    return sup.plan_actions(report, state, _CONFIG, now_epoch=now)["agents"]["worker"]


# ----------------------------------------- the 5 required safety scenarios

def test_scenario_i_dead_pid_relaunches() -> None:
    p = _plan(_report(), {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.RELAUNCH
    assert p["next_state"]["consecutive_fails"] == 1
    assert p["next_state"]["backoff_next_epoch"] == NOW + 30  # base


def test_scenario_ii_alive_stale_heartbeat_is_suspect_warn_not_kill() -> None:
    # alive + stale + not in a real wait -> SUSPECT only (never relaunch/kill)
    p = _plan(_report(heartbeat_stale=True, waiting_pid_alive=False),
              {"agents": {"worker": {"pid_alive": True, "last_warn_epoch": 0}}})
    assert p["action"] == sup.SUSPECT_WARN
    assert p["kill_first"] is False
    # a healthy agent legitimately blocked in a long wait is NOT suspect
    p2 = _plan(_report(heartbeat_stale=True, waiting_pid_alive=True),
               {"agents": {"worker": {"pid_alive": True}}})
    assert p2["action"] == sup.NONE
    # rate-limited: warned recently -> NONE this poll
    p3 = _plan(_report(heartbeat_stale=True),
               {"agents": {"worker": {"pid_alive": True, "last_warn_epoch": NOW - 10}}})
    assert p3["action"] == sup.NONE


def test_scenario_iii_manual_marker_relaunches_and_clears() -> None:
    marker = {"request_id": "rr-1", "force_protected": False}
    p = _plan(_report(restart_request=marker),
              {"agents": {"worker": {"pid_alive": True, "backoff_next_epoch": NOW + 9999}}})
    assert p["action"] == sup.RELAUNCH
    assert p["clear_marker"] == "rr-1"
    assert p["kill_first"] is True          # alive -> kill before relaunch
    assert p["bypass_backoff"] is True      # bypasses the future backoff_next
    assert "rr-1" in p["next_state"]["consumed_rids"]


def test_scenario_iv_protected_death_is_warn_only() -> None:
    p = _plan(_report(protected=True), {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.WARN_ONLY
    assert p["notify"] is True
    # never a relaunch/kill of a protected agent


def test_scenario_v_backoff_escalates_then_resets() -> None:
    # 1st death -> relaunch, backoff base (30)
    s1 = {"agents": {"worker": {"pid_alive": False, "consecutive_fails": 0,
                                "backoff_next_epoch": 0}}}
    p1 = _plan(_report(), s1)
    assert p1["action"] == sup.RELAUNCH and p1["next_state"]["backoff_next_epoch"] == NOW + 30
    # still dead, inside backoff -> wait
    s2 = {"agents": {"worker": {"pid_alive": False, "consecutive_fails": 1,
                                "backoff_next_epoch": NOW + 30}}}
    assert _plan(_report(), s2, now=NOW + 5)["action"] == sup.BACKOFF_WAIT
    # backoff elapsed -> relaunch, delay doubles (60)
    p3 = _plan(_report(), s2, now=NOW + 31)
    assert p3["action"] == sup.RELAUNCH
    assert p3["next_state"]["consecutive_fails"] == 2
    assert p3["next_state"]["backoff_next_epoch"] == (NOW + 31) + 60
    # sustained liveness resets the backoff
    s4 = {"agents": {"worker": {"pid_alive": True, "consecutive_fails": 2,
                                "healthy_since": NOW - 200}}}  # >= reset_after 180
    p4 = _plan(_report(), s4)
    assert p4["action"] == sup.NONE
    assert p4["next_state"]["consecutive_fails"] == 0
    assert p4["next_state"]["backoff_next_epoch"] == 0.0


# ----------------------------------------- marker / protected edge cases

def test_protected_marker_without_force_is_refused_and_cleared() -> None:
    p = _plan(_report(protected=True, restart_request={"request_id": "rr-9"}),
              {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.REFUSE_PROTECTED
    assert p["clear_marker"] == "rr-9"
    assert p["notify"] is True


def test_protected_marker_with_force_relaunches() -> None:
    p = _plan(_report(protected=True,
                      restart_request={"request_id": "rr-9", "force_protected": True}),
              {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.RELAUNCH
    assert p["clear_marker"] == "rr-9"


def test_consumed_marker_still_dead_does_not_bypass_backoff() -> None:
    # the manual relaunch already fired (rid consumed) but the agent is STILL
    # dead -> do NOT bypass backoff again every poll; honor the backoff window.
    p = _plan(_report(restart_request={"request_id": "rr-1"}),
              {"agents": {"worker": {"pid_alive": False, "consumed_rids": ["rr-1"],
                                     "backoff_next_epoch": NOW + 9999}}})
    assert p["action"] == sup.BACKOFF_WAIT
    assert p["clear_marker"] is None        # never silently clear a failed request


def test_consumed_marker_now_alive_clears() -> None:
    p = _plan(_report(restart_request={"request_id": "rr-1"}),
              {"agents": {"worker": {"pid_alive": True, "consumed_rids": ["rr-1"]}}})
    assert p["action"] == sup.CLEAR_MARKER
    assert p["clear_marker"] == "rr-1"


def test_only_auto_restart_agents_are_planned() -> None:
    cfg = {"agents": {"worker": {"auto_restart": False}}}
    plan = sup.plan_actions(_report(), {"agents": {"worker": {"pid_alive": False}}},
                            cfg, now_epoch=NOW)
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
    assert fresh["agents"]["worker"]["waiting_pid_alive"] is True
    stale = sup.build_report(s, now_epoch=hb_ts + 999, suspect_after_seconds=120)
    assert stale["agents"]["worker"]["heartbeat_stale"] is True
    # an agent that never heartbeated reads as stale
    assert stale["agents"]["lead"]["heartbeat_stale"] is True
    assert stale["agents"]["lead"]["heartbeat_age_seconds"] is None


def test_report_reflects_restart_request(tmp_path: Path) -> None:
    s = _team(tmp_path)
    _run(["request-restart", "--for", "worker", "--reason", "x"], tmp_path)
    rpt = sup.build_report(s, now_epoch=NOW)
    rr = rpt["agents"]["worker"]["restart_request"]
    assert rr is not None and rr["agent"] == "worker" and rr["request_id"].startswith("rr-")


# ----------------------------------------- request-restart + clear command

def test_request_restart_writes_marker(tmp_path: Path) -> None:
    s = _team(tmp_path)
    assert _run(["request-restart", "--for", "worker", "--from", "lead",
                 "--reason", "outage", "--force-protected"], tmp_path) == 0
    m = s.read_restart_request("worker")
    assert m["agent"] == "worker" and m["source"] == "manual"
    assert m["requested_by"] == "lead" and m["force_protected"] is True
    assert m["reason"] == "outage" and m["request_id"].startswith("rr-")


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


# ----------------------------------------- supervise --init / --plan CLI

def test_supervise_init_generates_and_is_idempotent(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    assert _run(["supervise", "--init"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "written" in out
    assert (s.dir / "supervisor.json").exists()
    assert (s.dir / "supervisor.ps1").exists()
    assert not (s.dir / "supervisor.sh").exists()  # v1: PowerShell only
    # script anchors: calls the Python plan (NO invalid --json flag), the safe
    # launcher, the wt warning, the dry-run hook, and preserves a marker on a
    # failed relaunch.
    ps = (s.dir / "supervisor.ps1").read_text(encoding="utf-8")
    assert "supervise --plan --state-file" in ps
    assert "--json" not in ps                       # blocker regression guard
    assert "Start-Process" in ps and "-PassThru" in ps
    assert "wt.exe" in ps
    assert "DryRun" in ps
    assert "keeping restart marker for retry" in ps  # clear-only-on-success path
    # idempotent: a second --init overwrites nothing
    assert _run(["supervise", "--init"], tmp_path) == 0
    assert "all files already exist" in capsys.readouterr().out


def test_supervise_plan_cli_with_fixtures(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    report_file = tmp_path / "rpt.json"
    report_file.write_text(json.dumps(_report()), encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {"pid_alive": False}}}),
                          encoding="utf-8")
    rc = _run(["supervise", "--plan", "--report-file", str(report_file),
               "--state-file", str(state_file), "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["agents"]["worker"]["action"] == sup.RELAUNCH


def test_supervise_plan_exact_generated_command_runs(tmp_path: Path, capsys) -> None:
    """Regression for the BLOCKER: the generated script's command line
    (`supervise --plan --state-file S --now N`, LIVE report, NO --json) must
    actually parse and emit valid JSON — the prior templates passed a
    non-existent --json flag and would have argparse-failed at runtime."""
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    state_file = s.dir / "supervisor-state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {"pid_alive": False}}}),
                          encoding="utf-8")
    # EXACTLY the args supervisor.ps1 invokes (live report; no --json).
    rc = _run(["supervise", "--plan", "--state-file", str(state_file),
               "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)   # must be valid JSON
    assert plan["agents"]["worker"]["action"] == sup.RELAUNCH


def test_failed_launch_preserves_marker_success_clears(tmp_path: Path) -> None:
    """Executor/plan boundary (codex MAJOR): the plan tells the executor to
    relaunch a fresh manual request and clear its rid, but the clear is a
    SEPARATE command the script runs ONLY after a confirmed relaunch. A failed
    launch (no clear call) leaves the marker for retry; a successful one clears
    it. (The PS executor gates the clear inside `if ($newPid)`.)"""
    s = _team(tmp_path)
    s.write_restart_request("worker", {"agent": "worker", "request_id": "rr-1",
                                       "force_protected": False})
    rpt = sup.build_report(s, now_epoch=NOW)
    plan = sup.plan_actions(rpt, {"agents": {"worker": {"pid_alive": False}}},
                            {"agents": {"worker": {"auto_restart": True}}},
                            now_epoch=NOW)["agents"]["worker"]
    assert plan["action"] == sup.RELAUNCH and plan["clear_marker"] == "rr-1"
    # FAILED launch -> executor does NOT call clear -> marker survives
    assert s.read_restart_request("worker") is not None
    # SUCCESSFUL launch -> executor clears via the dedicated command
    _run(["supervise", "--clear-restart", "--for", "worker",
          "--request-id", "rr-1"], tmp_path)
    assert s.read_restart_request("worker") is None
