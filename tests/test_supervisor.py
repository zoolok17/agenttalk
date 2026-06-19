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
    # snapshot defaults to [] (captured but EMPTY = no brain present), not None
    # (None means the capture FAILED -> SNAPSHOT_UNAVAILABLE).
    snap = [] if snapshot is None else snapshot
    return sup.plan_actions(report, state, config, now_epoch=now,
                            snapshot=snap)["agents"]["worker"]


# ----------------------------------------- the 5 required safety scenarios

def test_scenario_i_dead_pid_relaunches() -> None:
    # brain recorded but ABSENT from the snapshot (and no orphan wait) -> DEAD.
    p = _plan(_report(), {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=[])
    assert p["action"] == sup.RELAUNCH and p["state"] == "DEAD"
    assert p["next_state"]["consecutive_fails"] == 1
    assert p["next_state"]["backoff_next_epoch"] == NOW + 30  # base


def test_scenario_ii_alive_stale_without_hook_is_suspect_warn_not_kill() -> None:
    # brain alive + ready + stale + NO activity hook (default _CONFIG) ->
    # ACTIVE_OR_BUSY: SUSPECT only, never kill (the WP-2 trap).
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
    assert p["kill_first"] is True          # brain alive -> kill tree before relaunch
    assert p["kill_targets"]                # non-empty (brain + managed)
    assert p["bypass_backoff"] is True      # bypasses the future backoff_next
    assert "rr-1" in p["next_state"]["consumed_rids"]


def test_scenario_iv_protected_death_is_warn_only() -> None:
    p = _plan(_report(protected=True), {"agents": {"worker": _ready()}}, snapshot=[])
    assert p["action"] == sup.WARN_ONLY
    assert p["notify"] is True
    # never a relaunch/kill of a protected agent


def test_scenario_v_backoff_escalates_then_resets() -> None:
    # 1st death -> relaunch, backoff base (30)
    s1 = {"agents": {"worker": _ready(consecutive_fails=0, backoff_next_epoch=0)}}
    p1 = _plan(_report(), s1, snapshot=[])
    assert p1["action"] == sup.RELAUNCH and p1["next_state"]["backoff_next_epoch"] == NOW + 30
    # still dead, inside backoff -> wait
    s2 = {"agents": {"worker": _ready(consecutive_fails=1, backoff_next_epoch=NOW + 30)}}
    assert _plan(_report(), s2, now=NOW + 5, snapshot=[])["action"] == sup.BACKOFF_WAIT
    # backoff elapsed -> relaunch, delay doubles (60)
    p3 = _plan(_report(), s2, now=NOW + 31, snapshot=[])
    assert p3["action"] == sup.RELAUNCH
    assert p3["next_state"]["consecutive_fails"] == 2
    assert p3["next_state"]["backoff_next_epoch"] == (NOW + 31) + 60
    # sustained liveness (brain alive + ready + fresh hb) resets the backoff
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
    # the manual relaunch already fired (rid consumed) but the brain is STILL
    # gone -> do NOT bypass backoff again every poll; honor the backoff window.
    p = _plan(_report(restart_request={"request_id": "rr-1"}),
              {"agents": {"worker": _ready(consumed_rids=["rr-1"],
                                           backoff_next_epoch=NOW + 9999)}}, snapshot=[])
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


def test_supervise_plan_cli_with_fixtures(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    (s.dir / "supervisor.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    report_file = tmp_path / "rpt.json"
    report_file.write_text(json.dumps(_report()), encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"agents": {"worker": {}}}), encoding="utf-8")
    snap_file = tmp_path / "snap.json"   # empty snapshot = captured, no brain -> DEAD
    snap_file.write_text("[]", encoding="utf-8")
    rc = _run(["supervise", "--plan", "--report-file", str(report_file),
               "--state-file", str(state_file), "--snapshot-file", str(snap_file),
               "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["agents"]["worker"]["action"] == sup.RELAUNCH
    assert plan["agents"]["worker"]["state"] == "DEAD"


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
    # claude brain alive + ready + stale + hook -> STUCK_RECOVER (resume the
    # pinned session). _ready() makes resume_available true.
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": _ready(session_id="SID")}},
                   snapshot=_snap(cli="claude"))
    assert p["action"] == sup.STUCK_RECOVER and p["state"] == "STUCK"
    assert p["kill_first"] is True            # alive-but-stuck -> kill tree first
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
    # brain GONE still routes to relaunch (DEAD), distinct from stuck_recover
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": _ready()}}, snapshot=[])
    assert p["action"] == sup.RELAUNCH and p["state"] == "DEAD"


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
    assert p["session_args"] == ["resume", "--last", "-a", "never", "-s",
                                 "workspace-write", "$agenttalk-listen"]
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
    # brain GONE, but an orphan `agenttalk wait` still heartbeats -> ZOMBIE_WAIT:
    # reap the orphan set (kill_orphans + kill_targets) THEN relaunch.
    snap = [{"pid": WAIT_PID, "parent_pid": 1, "name": "python.exe",
             "command_line": "agenttalk wait --for worker", "start_time": "t-wait"}]
    p = _plan(_report(heartbeat_stale=False),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=snap)
    assert p["action"] == sup.RELAUNCH and p["state"] == "ZOMBIE_WAIT"
    assert p["kill_orphans"] is True
    assert WAIT_PID in [t["pid"] for t in p["kill_targets"]]


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
    p = _plan(_report(heartbeat_stale=True), st, snapshot=[])
    assert p["action"] == sup.RELAUNCH and p["state"] == "DEAD"
    assert p["next_state"]["consecutive_fails"] == 1


def test_readiness_failed_after_grace_kills_and_relaunches() -> None:
    # brain came up but NEVER reached the listen loop (no first heartbeat); grace
    # expired -> READINESS_FAILED: kill tree + relaunch (catches an early -p exit).
    st = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                "launch_grace_until": NOW - 1, "readiness_seen": False,
                                "backoff_next_epoch": 0}}}
    p = _plan(_report(heartbeat_stale=True), st, snapshot=_snap())
    assert p["action"] == sup.RELAUNCH and p["state"] == "READINESS_FAILED"
    assert p["kill_first"] is True and p["kill_targets"]


def test_pid_reuse_start_mismatch_is_not_alive() -> None:
    # recorded brain pid is present in the snapshot but with a DIFFERENT
    # start-time (a recycled pid) -> NOT our process -> DEAD, not healthy.
    snap = [{"pid": BRAIN_PID, "parent_pid": 1, "name": "notcodex.exe",
             "command_line": "something-else", "start_time": "DIFFERENT"}]
    p = _plan(_report(heartbeat_stale=False),
              {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=snap)
    assert p["state"] == "DEAD" and p["action"] == sup.RELAUNCH


def test_snapshot_unavailable_fails_closed() -> None:
    # requires_brain_pid (codex) + a FAILED capture (None) -> SNAPSHOT_UNAVAILABLE:
    # warn, NO kill, NO relaunch (never storm off a missing snapshot).
    p = sup.plan_actions(_report(heartbeat_stale=True),
                         {"agents": {"worker": _ready(last_warn_epoch=0)}}, _CONFIG,
                         now_epoch=NOW, snapshot=None)["agents"]["worker"]
    assert p["state"] == "SNAPSHOT_UNAVAILABLE"
    assert p["action"] == sup.SNAPSHOT_UNAVAILABLE
    assert p["kill_first"] is False and p["kill_targets"] == []


def test_non_forking_cli_uses_legacy_pid_alive_when_no_snapshot() -> None:
    # an explicitly NON-forking CLI (requires_brain_pid=false) may use the legacy
    # single-pid path when the snapshot is unavailable.
    cfg = {"agents": {"worker": {"auto_restart": True, "cli": "claude",
                                 "requires_brain_pid": False}},
           "launch_grace_seconds": 120}
    dead = sup.plan_actions(_report(), {"agents": {"worker": {"pid_alive": False,
                                                              "backoff_next_epoch": 0}}},
                            cfg, now_epoch=NOW, snapshot=None)["agents"]["worker"]
    assert dead["action"] == sup.RELAUNCH and dead["state"] == "DEAD"
    alive = sup.plan_actions(_report(), {"agents": {"worker": {"pid_alive": True}}},
                             cfg, now_epoch=NOW, snapshot=None)["agents"]["worker"]
    assert alive["action"] == sup.NONE and alive["state"] == "HEALTHY_IDLE"


def test_resume_mode_fresh_then_last() -> None:
    # first launch (never reached readiness, no resume_available) -> fresh; after
    # readiness has been reached -> resume --last.
    st_fresh = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                      "launch_grace_until": NOW - 1, "readiness_seen": False,
                                      "backoff_next_epoch": 0}}}
    p = _plan(_report(heartbeat_stale=True), st_fresh, snapshot=[])
    assert p["resume_mode"] == "fresh"
    p2 = _plan(_report(heartbeat_stale=False),
               {"agents": {"worker": _ready(backoff_next_epoch=0)}}, snapshot=[])
    assert p2["state"] == "DEAD" and p2["resume_mode"] == "last"


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


def test_zombie_wait_via_carried_managed_pid_null_cmdline(tmp_path: Path) -> None:
    """impl-review BLOCKER 2: with the brain gone and the orphan wait's command
    line now unavailable, a prior start-matching managed wait pid must be carried
    forward, counted as wait_alive, and reaped (ZOMBIE_WAIT, not a silent DEAD)."""
    snap = [{"pid": WAIT_PID, "parent_pid": 1, "name": "python.exe",
             "command_line": None, "start_time": "wait-start"}]
    st = {"agents": {"worker": _ready(
        backoff_next_epoch=0,
        managed_pids=[{"pid": WAIT_PID, "start": "wait-start", "kind": "wait",
                       "last_seen": 0}])}}
    p = _plan(_report(heartbeat_stale=False), st, snapshot=snap)
    assert p["state"] == "ZOMBIE_WAIT" and p["kill_orphans"] is True
    assert WAIT_PID in [t["pid"] for t in p["kill_targets"]]


def test_codex_failed_first_launch_is_fresh_not_resume() -> None:
    """impl-review MAJOR 1: codex resume must be driven by resume_available, NOT
    legacy `launched` (set at launch time, before readiness). A failed first
    launch (launched=true, resume_available=false) must relaunch FRESH."""
    cfg = {"agents": {"worker": {"cli": "codex", "auto_restart": True}},
           "launch_grace_seconds": 120}
    st = {"agents": {"worker": {"launcher_pid": LAUNCHER_PID, "launching": True,
                                "launch_grace_until": NOW - 1, "readiness_seen": False,
                                "resume_available": False, "launched": True,
                                "backoff_next_epoch": 0}}}
    p = sup.plan_actions(_report(heartbeat_stale=True), st, cfg,
                         now_epoch=NOW, snapshot=[])["agents"]["worker"]
    assert p["state"] == "DEAD" and p["resume_mode"] == "fresh"
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
    existing = ('model = "gpt-5"\napproval_policy = "on-request"\n\n'
                '[windows]\nfoo = 1\n')
    out = sup.codex_config_overlay(existing, repo_path=r"C:\proj\agenttalk",
                                   windows_sandbox="unelevated")
    assert 'model = "gpt-5"' in out                       # operator key preserved
    assert 'approval_policy = "never"' in out             # managed key set...
    assert 'approval_policy = "on-request"' not in out    # ...replacing the old value
    assert 'sandbox_mode = "workspace-write"' in out
    assert 'foo = 1' in out                               # other [windows] key kept
    assert 'sandbox = "unelevated"' in out                # the UAC fix
    # writable_roots is a DOUBLE-QUOTED path with escaped backslashes
    assert r'writable_roots = ["C:\\proj\\agenttalk"]' in out
    assert out.count("[windows]") == 1                    # no duplicate table
    # idempotent
    assert sup.codex_config_overlay(out, repo_path=r"C:\proj\agenttalk",
                                    windows_sandbox="unelevated") == out
    # empty input still yields a valid overlay with both tables
    fresh = sup.codex_config_overlay("", repo_path="C:\\x", windows_sandbox="elevated")
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
    assert "agenttalk heartbeat" in cmds
    # idempotent: second install adds nothing
    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    data2 = json.loads(settings.read_text(encoding="utf-8"))
    post = [h["command"] for g in data2["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert post.count("agenttalk heartbeat") == 1


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
    assert groups[0]["hooks"][0]["command"] == "agenttalk heartbeat"
    # idempotent (presence-check sees the nested shape)
    assert _run(["supervise", "--install-activity-hook", "--codex-only"], tmp_path) == 0
    groups2 = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    cmds = [h["command"] for g in groups2 for h in g["hooks"]]
    assert cmds.count("agenttalk heartbeat") == 1


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
    snap_file.write_text("[]", encoding="utf-8")   # captured, no brain -> DEAD
    # EXACTLY the args supervisor.ps1 invokes (live report; no --json).
    rc = _run(["supervise", "--plan", "--state-file", str(state_file),
               "--snapshot-file", str(snap_file), "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)   # must be valid JSON
    assert plan["agents"]["worker"]["action"] == sup.RELAUNCH


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
