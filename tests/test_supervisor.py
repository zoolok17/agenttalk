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
from pathlib import Path

import pytest

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

# ---- snapshot-model fixtures (the 8-state classifier reads a process snapshot) ----
BRAIN_PID, BRAIN_START, LAUNCHER_PID, WAIT_PID = 200, "t-brain", 199, 400


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
                     "command_line": f"... agenttalk wait --for {agent} --timeout 1800",
                     "start_time": "t-wait"})
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


def test_scenario_iii_manual_marker_relaunches_and_clears() -> None:
    marker = {"request_id": "rr-1", "force_protected": False}
    p = _plan(_report(restart_request=marker),
              {"agents": {"worker": _ready(backoff_next_epoch=NOW + 9999)}},
              snapshot=_snap())
    assert p["action"] == sup.RELAUNCH and p["state"] == "MANUAL_RESTART"
    assert p["clear_marker"] == "rr-1"
    assert p["kill_first"] is True          # best-effort cleanup before relaunch
    assert p["kill_targets"]                # non-empty (launcher + managed)
    assert p["bypass_backoff"] is True      # bypasses the future backoff_next
    assert "rr-1" in p["next_state"]["consumed_rids"]


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
    # the manual relaunch already fired (rid consumed) but the heartbeat is STILL
    # stale -> do NOT bypass backoff again every poll; honor the backoff window.
    p = _plan(_report(heartbeat_stale=True, restart_request={"request_id": "rr-1"}),
              {"agents": {"worker": _ready(consumed_rids=["rr-1"],
                                           backoff_next_epoch=NOW + 9999)}},
              snapshot=[], config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.BACKOFF_WAIT
    assert p["clear_marker"] is None        # never silently clear a failed request


def test_consumed_marker_now_alive_clears() -> None:
    p = _plan(_report(restart_request={"request_id": "rr-1"}),
              {"agents": {"worker": _ready(consumed_rids=["rr-1"])}}, snapshot=_snap())
    assert p["action"] == sup.CLEAR_MARKER
    assert p["clear_marker"] == "rr-1"


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
    assert (s.dir / "bin" / "agenttalk.cmd").exists()   # the project-local shim
    assert not (s.dir / "supervisor.sh").exists()  # v1: PowerShell only
    # script anchors: calls the Python plan (NO invalid --json flag), launches
    # the REAL exe via -FilePath/-ArgumentList (NOT Invoke-Expression), applies
    # + restores env, the dry-run hook, and preserves state on a failed launch.
    ps = (s.dir / "supervisor.ps1").read_text(encoding="utf-8")
    assert "supervise --plan --state-file" in ps
    assert "--json" not in ps                       # blocker regression guard
    assert "Start-Process -FilePath" in ps and "-ArgumentList" in ps and "-PassThru" in ps
    assert "Invoke-Expression" not in ps            # file/args executor, no expr
    assert "windows_file" in ps                     # launches the real exe
    assert "DryRun" in ps
    assert "keeping marker/state for retry" in ps   # clear-only-on-success path
    # idempotent: a second --init overwrites nothing
    assert _run(["supervise", "--init"], tmp_path) == 0
    assert "all files already exist" in capsys.readouterr().out


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


_HOOK_CONFIG = {
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
    """The executor applies the agent env (AGENTTALK_ROOT + PYTHONPATH-src +
    per-agent env + CODEX_HOME) around Start-Process and RESTORES the
    supervisor's own env afterward. It does NOT bake the absolute
    AGENTTALK_PYTHON into the AGENT env (blocker #2 point 8 - in-sandbox uses
    `python -m`); that stays supervisor-only."""
    ps = sup.PS_TEMPLATE
    assert "$applied = @{ AGENTTALK_ROOT = $Root }" in ps   # lean agent env
    assert "$a.env" in ps                                  # applies per-agent env
    assert "'src') + ';' + $env:PYTHONPATH" in ps          # src on PYTHONPATH for `python -m`
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
    """A prior start-matching managed wait pid with a now-null command line is
    still a safe cleanup target when heartbeat staleness triggers recovery."""
    snap = [{"pid": WAIT_PID, "parent_pid": 1, "name": "python.exe",
             "command_line": None, "start_time": "wait-start"}]
    st = {"agents": {"worker": _ready(
        backoff_next_epoch=0,
        managed_pids=[{"pid": WAIT_PID, "start": "wait-start", "kind": "wait",
                       "last_seen": 0}])}}
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
    # the agent env no longer carries the absolute AGENTTALK_PYTHON nor the
    # .agenttalk/bin shim PATH prepend (in-sandbox agent uses `python -m`).
    assert "$applied = @{ AGENTTALK_ROOT = $Root }" in ps
    assert "AGENTTALK_PYTHON = $AgenttalkPython" not in ps   # not in the AGENT env
    # Seed-CodexHome copies config.toml then overlays via the python core
    assert "function Seed-CodexHome" in ps
    assert "supervise --seed-codex-config" in ps
    # claude settings seed + the PREFLIGHT fail-closed gate
    assert "supervise --seed-claude-settings" in ps
    assert "function Preflight" in ps
    assert "python -m agenttalk --version" in ps            # the smoke-test
    assert "fail closed" in ps.lower()
    # reviewer-1 r1: the CODEX preflight must set PYTHONPATH the SAME way Launch
    # does (src on a checkout) so it tests the agent's REAL import env and does
    # not fail closed on a checkout where agenttalk is not globally installed.
    pf = ps[ps.index("function Preflight"):]
    pf = pf[:pf.index("\ndo {")]                            # the Preflight body
    ci = pf.index("$plan.cli -eq 'codex'")
    codex_branch = pf[ci:pf.index("} else {", ci)]          # from codex to the codex/claude divider
    assert "'src') + ';' + $env:PYTHONPATH" in codex_branch
    # 0.31.1: the non-wrapped Codex preflight is the PLAIN import gate under the
    # Codex env (seeded CODEX_HOME + PYTHONPATH), NOT a `codex sandbox ...` probe -
    # the sandbox flags drift across Codex CLI releases and a hard-coded probe
    # false-fail-closed on valid agents.
    assert "$env:CODEX_HOME = $codexHome" in codex_branch    # still under the codex home
    assert "& python -m agenttalk --version" in codex_branch
    assert "& $file sandbox" not in codex_branch             # no codex sandbox probe
    assert "-P :workspace" not in codex_branch               # the drift-prone flag is gone
    # Phase C: a WRAPPED agent ($file is python, not the CLI) is preflighted BEFORE
    # the codex branch and validates the python wrapper, NOT the codex sandbox.
    wrap_branch = pf[pf.index("$plan.launch_mode -eq 'wrap'"):ci]
    assert "& $file -m agenttalk --version" in wrap_branch
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


# ---------------------------------------- WP-3: install-activity-hook (merge-safe)

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


def test_generated_ps1_is_bom_ascii_and_parses(tmp_path: Path) -> None:
    """0.28.1 regression: the GENERATED supervisor.ps1 must (a) be ASCII-only
    (no em-dash etc.) and BOM-prefixed so Windows PowerShell 5.1 decodes it, and
    (b) actually PARSE under PowerShell. The prior tests structural-checked the
    template but never ran the .ps1 through PS — so a non-ASCII char + BOM-less
    write cascaded into parse errors and the script never ran."""
    s = _team(tmp_path)
    assert _run(["supervise", "--init"], tmp_path) == 0
    ps1 = s.dir / "supervisor.ps1"
    raw = ps1.read_bytes()
    # (a) UTF-8 BOM + the BODY is ASCII-only (a non-ASCII regression fails here)
    assert raw[:3] == b"\xef\xbb\xbf", "supervisor.ps1 must be written with a UTF-8 BOM"
    body = raw[3:]
    non_ascii = [b for b in body if b > 0x7F]
    assert not non_ascii, f"supervisor.ps1 body must be ASCII-only; found {non_ascii[:5]}"
    # (b) it actually parses — under EVERY PowerShell present, PREFERRING
    # Windows PowerShell 5.1 (the bug was 5.1-specific); skip only if none.
    shells = [sh for sh in (shutil.which("powershell"), shutil.which("pwsh")) if sh]
    if not shells:
        return
    check = (
        "$e=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}',"
        "[ref]$null,[ref]$e); if($e -and $e.Count){ $e[0].Message; exit 1 }")
    for sh in shells:
        res = subprocess.run([sh, "-NoProfile", "-Command", check],
                             capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, (
            f"supervisor.ps1 failed to parse under {sh}: {res.stdout}{res.stderr}")


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
    """Windows PowerShell 5.1 first (the launch-layer bugs are 5.1-specific), then
    pwsh. None if neither is present (skip the Windows-gated runtime tests).

    These runtime tests exercise the .cmd shim and Windows Start-Process arg
    quoting, which are Windows-only - a `.cmd` batch file can't execute under
    pwsh on Linux/macOS (GitHub's POSIX runners DO ship pwsh, so a bare
    which() check is not enough). Gate on the OS, not just shell presence."""
    if os.name != "nt":
        return None
    for sh in ("powershell", "pwsh"):
        found = shutil.which(sh)
        if found:
            return found
    return None


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
    ps1 = s.dir / "supervisor.ps1"
    assert ps1.exists() and (s.dir / "bin" / "agenttalk.cmd").exists()
    # PATH reduced to the Windows dirs only: no Python Scripts dir, so a bare
    # `agenttalk[.exe]` console script is unreachable - only the baked shim works.
    windir = os.environ.get("WINDIR", r"C:\Windows")
    reduced = dict(os.environ)
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
    ps1 = s.dir / "supervisor.ps1"
    # readiness state (past initial launch grace) + NO heartbeat written => stale.
    (s.dir / "supervisor-state.json").write_text(
        json.dumps({"agents": {"lead": _ready()}}), encoding="utf-8")

    def _once(*extra: str) -> str:
        r = subprocess.run([shell, "-NoProfile", "-File", str(ps1), "-Once", *extra],
                           capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
        return r.stdout + r.stderr

    noisy = _once()                                       # normal run -> warning prints
    quiet = _once("-Quiet")                               # quiet run -> console silent
    assert "lead" in noisy, f"expected a console warning for the stale protected lead; got {noisy!r}"
    assert "lead" not in quiet, f"-Quiet must suppress the warning console output; got {quiet!r}"


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
        r = subprocess.run([shell, "-NoProfile", "-File", str(ps1), "-Once", *extra],
                           capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
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
    return block


def _pslit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


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
            "Stop-Tree $targets",
            "Start-Sleep -Milliseconds 400",
            "$alive = @(); foreach($id in $parent,$child){ if(Get-Process -Id $id "
            "-ErrorAction SilentlyContinue){ $alive += $id } }",
            f"@{{ alive = $alive; guard_survived = ($guardStill -and $guardAlive) }} | "
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
    "agents": {"worker": {"auto_restart": True, "cli": "codex", "wrapped": True}},
    "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
    "launch_grace_seconds": 120,
}


def _wrap_snap(*, cli="codex", launcher_pid=WRAP_LAUNCHER_PID,
               child_pid=WRAP_CHILD_PID, child_start="t-child"):
    """A wrapped-agent snapshot: the python wrapper (launcher) + its per-turn CLI
    child. The child is a real codex.exe/claude.exe row, so WITHOUT wrapping the
    brain-discovery would mistake it for the long-lived brain - the wrapped path
    must instead treat it as just a managed (reapable) descendant."""
    name = "codex.exe" if cli == "codex" else "claude.exe"
    return [
        {"pid": launcher_pid, "parent_pid": 1, "name": "python.exe",
         "command_line": f"python -m agenttalk wrap --for worker --cli {cli} --loop",
         "start_time": "t-wrap"},
        {"pid": child_pid, "parent_pid": launcher_pid, "name": name,
         "command_line": f"{name} exec --json", "start_time": child_start},
    ]


def _wrap_ready(**over) -> dict:
    st = {"launcher_pid": WRAP_LAUNCHER_PID, "launcher_start": "t-wrap",
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
                       "worker", NOW)
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
    # the codex sample ships a conservative heavy-reasoning threshold (Codex
    # ruling: 1200 for a known implementer/reviewer role, not the bare 900)
    assert w["stuck_after_seconds"] == 1200
    args = w["launch"]["windows_args"]
    assert "{SESSION_ARGS}" not in args          # wrapper owns session; no splice
    assert args[:3] == ["-m", "agenttalk", "wrap"] and "--loop" in args
    # 0.31.2: the wrapped codex child is launched with `--disable hooks` by default
    # (the wrapper owns the heartbeat; sidesteps the codex hook-trust prompt).
    assert args[-2:] == ["--disable", "hooks"]
    # steer-to-wrapped: the comment marks wrapped recommended/default + non-wrapped legacy
    assert "RECOMMENDED" in w["_comment_wrapped"] and "LEGACY" in w["_comment_wrapped"]


# ---------- per-CLI wrapped stuck_after defaults + the codex low-threshold guardrail

def test_resolve_stuck_after_per_cli_defaults_and_override() -> None:
    # per-CLI wrapped defaults
    assert sup.resolve_stuck_after({}, {"cli": "claude", "wrapped": True}) == 180.0
    assert sup.resolve_stuck_after({}, {"cli": "codex", "wrapped": True}) == 900.0
    # an explicit per-agent override always wins
    assert sup.resolve_stuck_after(
        {}, {"cli": "codex", "wrapped": True, "stuck_after_seconds": 1500}) == 1500.0
    # non-wrapped keeps the global behavior (config, then the built-in default)
    assert sup.resolve_stuck_after({"stuck_after_seconds": 77}, {"cli": "codex"}) == 77.0
    assert sup.resolve_stuck_after({}, {"cli": "codex"}) == 120.0


def test_wrapped_codex_default_threshold_tolerates_reasoning_gap() -> None:
    # A 300s silent pure-reasoning gap is STALE under the global 120s threshold,
    # but a wrapped codex re-derives against its 900s default -> NOT stale -> the
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


def test_report_parity_wrapped_codex_uses_per_cli_threshold(tmp_path: Path) -> None:
    # REPORT PARITY: the operator-facing build_report must match the planner's
    # per-CLI decision (the operator watches the supervisor console during the
    # dogfood). A 300s-old heartbeat is STALE under the global 120s default but
    # FRESH under the wrapped-codex 900s threshold - the report must show fresh.
    s = _team(tmp_path)
    s.write_heartbeat("worker")
    hb_ts = s.read_heartbeat("worker").timestamp()
    sup_cfg = {"agents": {"worker": {"cli": "codex", "wrapped": True}}}
    # global view (no supervisor_config): stale at 120s
    glob = sup.build_report(s, now_epoch=hb_ts + 300, stuck_after_seconds=120)
    assert glob["agents"]["worker"]["heartbeat_stale"] is True
    # parity view (supervisor_config passed): fresh under the per-CLI 900s
    parity = sup.build_report(s, now_epoch=hb_ts + 300, stuck_after_seconds=120,
                              supervisor_config=sup_cfg)
    assert parity["agents"]["worker"]["heartbeat_stale"] is False
    assert parity["agents"]["worker"]["stuck_after_seconds"] == 900.0
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
    p = _plan_hook(_report(heartbeat_stale=True, restart_request={"request_id": "rr-x"}),
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
    _stub_cmd(wstub, wlog)
    _stub_cmd(cstub, clog)
    out = tmp_path / "pf.json"
    preamble = [f"$Root = {_pslit(str(tmp_path))}", "$SrcOnPyPath = $false"]
    harness = "\n".join([
        helpers, *preamble,
        # wrapped codex: $file is the python wrapper stub; launch_mode 'wrap'
        f"$wrapOk = Preflight 'wrapped-codex' (@{{ cli='codex'; launch_mode='wrap' }}) {_pslit(str(wstub))} $null",
        # non-wrapped codex (0.31.1): must NOT call `$file sandbox ...` - it runs the
        # ambient `python -m agenttalk --version` gate, so the $file stub is never
        # invoked (its log stays empty / has no 'sandbox').
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
    marker = {"request_id": "rr-ll", "force_protected": False}
    p = _plan(_report(heartbeat_stale=True, restart_request=marker,
                      lead_loop_exit={"state": "stood_down"}),
              {"agents": {"worker": _ready(backoff_next_epoch=NOW + 9999)}},
              snapshot=_snap(), config=_HOOK_CODEX_CONFIG)
    assert p["action"] == sup.RELAUNCH and p["state"] == "MANUAL_RESTART"
