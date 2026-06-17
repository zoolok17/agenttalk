"""WP-2: the agent supervisor's Python core.

The safety table (plan_actions) is the heart of the feature — it must be
CI-testable WITHOUT launching terminals, so these tests drive it via plain
fixtures. The generated PS/bash scripts are thin executors (documented-manual).
"""

from __future__ import annotations

import json
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


def test_scenario_ii_alive_stale_without_hook_is_suspect_warn_not_kill() -> None:
    # alive + stale + NO activity hook (the default _CONFIG) -> SUSPECT only:
    # we can't tell stuck from busy, so never relaunch/kill (the WP-2 trap).
    p = _plan(_report(heartbeat_stale=True),
              {"agents": {"worker": {"pid_alive": True, "last_warn_epoch": 0}}})
    assert p["action"] == sup.SUSPECT_WARN
    assert p["kill_first"] is False
    # rate-limited: warned recently -> NONE this poll
    p3 = _plan(_report(heartbeat_stale=True),
               {"agents": {"worker": {"pid_alive": True, "last_warn_epoch": NOW - 10}}})
    assert p3["action"] == sup.NONE
    # fresh heartbeat -> healthy regardless of hook
    assert _plan(_report(heartbeat_stale=False),
                 {"agents": {"worker": {"pid_alive": True}}})["action"] == sup.NONE


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
    state_file.write_text(json.dumps({"agents": {"worker": {"pid_alive": False}}}),
                          encoding="utf-8")
    rc = _run(["supervise", "--plan", "--report-file", str(report_file),
               "--state-file", str(state_file), "--now", str(NOW)], tmp_path)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["agents"]["worker"]["action"] == sup.RELAUNCH


_HOOK_CONFIG = {
    "agents": {"worker": {"auto_restart": True, "activity_hook": True, "cli": "claude"}},
    "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
}


def _plan_hook(report, state, *, now=NOW, config=_HOOK_CONFIG):
    return sup.plan_actions(report, state, config, now_epoch=now)["agents"]["worker"]


# ---------------------------------------- WP-3: stuck-recovery matrix

def test_stuck_recover_when_alive_stale_and_hook_on() -> None:
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": {"pid_alive": True, "session_id": "SID"}}})
    assert p["action"] == sup.STUCK_RECOVER
    assert p["kill_first"] is True            # alive-but-stuck -> kill before resume
    assert p["launch_mode"] == "resume"
    assert p["session_args"] == ["--resume", "SID", "--permission-mode", "dontAsk",
                                 "--allowedTools", "Bash(agenttalk *)",
                                 "-p", "/agenttalk.listen"]
    assert p["next_state"]["consecutive_fails"] == 1   # backoff applies


def test_stuck_alive_fresh_is_none() -> None:
    p = _plan_hook(_report(heartbeat_stale=False),
                   {"agents": {"worker": {"pid_alive": True}}})
    assert p["action"] == sup.NONE


def test_stuck_protected_is_warn_only() -> None:
    p = _plan_hook(_report(protected=True, heartbeat_stale=True),
                   {"agents": {"worker": {"pid_alive": True}}})
    assert p["action"] == sup.WARN_ONLY
    assert p["notify"] is True               # never kill a protected human channel


def test_stuck_dead_pid_is_relaunch_not_stuck() -> None:
    # death still routes to relaunch (v1), distinct from stuck_recover
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": {"pid_alive": False}}})
    assert p["action"] == sup.RELAUNCH


def test_stuck_recover_requires_hook_else_suspect() -> None:
    cfg = {"agents": {"worker": {"auto_restart": True, "activity_hook": False}}}
    p = sup.plan_actions(_report(heartbeat_stale=True),
                         {"agents": {"worker": {"pid_alive": True, "last_warn_epoch": 0}}},
                         cfg, now_epoch=NOW)["agents"]["worker"]
    assert p["action"] == sup.SUSPECT_WARN   # no hook -> never kill


# ---------------------------------------- WP-3: session-id lifecycle + args

def test_session_id_fresh_then_resume() -> None:
    # no session_id yet -> fresh launch (token list still carries {SESSION_ID})
    p = _plan_hook(_report(heartbeat_stale=True),
                   {"agents": {"worker": {"pid_alive": True}}})
    assert p["launch_mode"] == "fresh"
    assert p["session_id"] is None
    assert "{SESSION_ID}" in p["session_args"]            # token list
    # once the script has pinned a session_id -> resume reuses it
    p2 = _plan_hook(_report(heartbeat_stale=True),
                    {"agents": {"worker": {"pid_alive": True, "session_id": "abc-123"}}})
    assert p2["launch_mode"] == "resume"
    assert "abc-123" in p2["session_args"]


def test_session_args_per_cli_explicit_skill() -> None:
    # Claude: explicit /agenttalk.listen as a SINGLE token, both modes.
    assert sup.session_args("claude", "fresh", None) == [
        "--session-id", "{SESSION_ID}", "-p", "/agenttalk.listen"]
    assert sup.session_args("claude", "resume", "X") == [
        "--resume", "X", "--permission-mode", "dontAsk",
        "--allowedTools", "Bash(agenttalk *)", "-p", "/agenttalk.listen"]
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


def test_ps_arglist_quotes_dollar_literally() -> None:
    # blocker 1(b): the prompt is ONE single-quoted literal — `$` never expands,
    # nesting quotes never reshape tokens.
    rendered = sup.ps_arglist(["-a", "never", "$agenttalk-listen"])
    assert rendered == "'-a','never','$agenttalk-listen'"
    assert "'$agenttalk-listen'" in rendered
    # embedded single quotes are doubled
    assert sup.ps_arglist(["it's"]) == "'it''s'"


def test_codex_relaunch_command_uses_codex_args() -> None:
    cfg = {"agents": {"c": {"auto_restart": True, "activity_hook": True, "cli": "codex"}}}
    rpt = {"agents": {"c": {"protected": False, "heartbeat_stale": True,
                            "restart_request": None}}}
    # codex has no pinned session_id; the `launched` flag drives resume-by-last
    p = sup.plan_actions(rpt, {"agents": {"c": {"pid_alive": True, "launched": True}}},
                         cfg, now_epoch=NOW)["agents"]["c"]
    assert p["cli"] == "codex" and p["launch_mode"] == "resume"
    assert p["session_args"] == ["resume", "--last", "-a", "never", "-s",
                                 "workspace-write", "$agenttalk-listen"]
    assert "--session-id" not in p["session_args"]
    assert p["session_args_ps"].endswith("'$agenttalk-listen'")  # quote-safe


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


def test_state_round_trip_preserves_pid_and_session(tmp_path: Path) -> None:
    """Blocker 3: a healthy/no-op tick must NOT drop the supervisor-owned pid /
    session_id / launched — else the next loop sees pid=null, thinks the agent
    died, and relaunches a HEALTHY agent every poll (and loses the session)."""
    st = {"agents": {"worker": {"pid_alive": True, "pid": 4321,
                                "session_id": "sess-9", "launched": True}}}
    p = _plan_hook(_report(heartbeat_stale=False), st)
    assert p["action"] == sup.NONE
    ns = p["next_state"]
    assert ns["pid"] == 4321
    assert ns["session_id"] == "sess-9"
    assert ns["launched"] is True


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
