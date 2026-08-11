"""WP-2: the agent supervisor's Python core.

The safety table (plan_actions) is the heart of the feature — it must be
CI-testable WITHOUT launching terminals, so these tests drive it via plain
fixtures. The generated PS/bash scripts are thin executors (documented-manual).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import (
    attention as att,
    checkpoint,
    cli,
    ephemeral as eph,
    health as hm,
    launch_admission,
    supervisor as sup,
    wrapper_runtime as wrt,
)
from agenttalk.store import Store, _process_alive


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _wrapper_log_agent_dir(name: str) -> str:
    return f"agent-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]}"


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


def _ps_filetime(microsecond: int) -> str:
    return str(134_116_392_310_000_000 + microsecond * 10)


def _test_start_filetime(start: str) -> str | None:
    match = re.fullmatch(
        r"2026-07-04T07:20:31\.([0-9]{6})0\+00:00",
        start,
    )
    return _ps_filetime(int(match.group(1))) if match else None


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


def _record_launch_context(
    *,
    cli_name: str = "codex",
    grace_seconds: float = 120,
    wrapped: bool = False,
    brain_pattern: str | None = None,
) -> dict:
    return sup._launch_record_context(
        agent="worker",
        cli=cli_name,
        grace_seconds=grace_seconds,
        wrapped=wrapped,
        brain_pattern=brain_pattern or cli_name,
    )


def test_launch_record_context_bounds_pattern_bytes_without_rejecting_unicode() -> None:
    context = _record_launch_context(brain_pattern="雪" * 100)
    assert context["brain_pattern"] == "雪" * 100
    assert sup.decode_launch_record_context(
        context,
        agent="worker",
        cli="codex",
    )[1]["brain_pattern"] == "雪" * 100

    boundary = _record_launch_context(brain_pattern="x" * 4096)
    assert len(boundary["brain_pattern"].encode("utf-8")) == 4096

    with pytest.raises(ValueError, match="brain pattern"):
        _record_launch_context(brain_pattern="x" * 4097)

    with pytest.raises(ValueError, match="too large"):
        _record_launch_context(
            cli_name="x" * 8192,
            brain_pattern="child",
        )

    oversized = _record_launch_context()
    oversized["cli"] = "x" * 8192
    with pytest.raises(ValueError, match="too large"):
        sup.decode_launch_record_context(
            oversized,
            agent="worker",
            cli="x" * 8192,
        )


@pytest.mark.parametrize("control", ["\x7f", "\x85"], ids=["del", "c1"])
def test_launch_record_context_rejects_del_and_c1_at_producer_and_decoder(
    control: str,
) -> None:
    with pytest.raises(ValueError, match="brain pattern"):
        _record_launch_context(brain_pattern=f"child{control}")

    persisted = _record_launch_context()
    persisted["brain_pattern"] = f"child{control}"
    with pytest.raises(ValueError, match="agent projection"):
        sup.decode_launch_record_context(
            persisted,
            agent="worker",
            cli="codex",
        )


def _write_idle_wrapper_runtime(
    store: Store,
    *,
    agent: str = "worker",
    at: float = NOW,
    wrapper_pid: int = 300,
    wrapper_start: str | None = WRAP_START,
) -> None:
    writer = wrt.WrapperRuntimeWriter(
        store.state_dir,
        agent,
        "test-wrapper-1",
        wrapper_pid=wrapper_pid,
        wrapper_start=wrapper_start,
        clock=lambda: at,
    )
    writer.idle()


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


def test_generated_process_snapshot_excludes_windows_idle_pid_zero() -> None:
    block = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Get-ProcSnapshot($path) {"):
        sup.PS_TEMPLATE.index("function Stop-Tree($targets) {")
    ]
    assert "$pidValue = [int]$p.ProcessId" in block
    assert "if ($pidValue -le 0) { continue }" in block
    assert "pid = $pidValue" in block


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


_UNUSABLE_RESTART_MARKER_PAYLOADS = [
    pytest.param(b'{"agent": "worker"}', id="missing-request-id"),
    pytest.param(
        b'{"agent": "worker", "request_id": ""}',
        id="empty-request-id",
    ),
    pytest.param(
        b'{"agent": "worker", "request_id": 7}',
        id="non-string-request-id",
    ),
    pytest.param(
        b'{"agent": "worker", "request_id": "unsafe id"}',
        id="unsafe-request-id",
    ),
    pytest.param(b'{broken', id="malformed-json"),
    pytest.param(b'["not", "an", "object"]', id="non-object"),
    pytest.param(b"\xff", id="invalid-utf8"),
]


@pytest.mark.parametrize("marker_payload", _UNUSABLE_RESTART_MARKER_PAYLOADS)
def test_restart_request_reader_marks_existing_unusable_marker(
    tmp_path: Path,
    marker_payload: bytes,
) -> None:
    store = _team(tmp_path)
    (store.state_dir / "worker.restart-request").write_bytes(marker_payload)

    assert store.read_restart_request("worker") == {
        "_agenttalk_restart_request_status": "unusable",
    }


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


def test_request_restart_during_process_tree_hold_does_not_promise_relaunch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    _write_attended_process_tree_reset_fixture(s)

    assert _run([
        "request-restart", "--for", "worker", "--from", "lead", "--reason", "recover",
    ], tmp_path) == 0

    output = capsys.readouterr().out
    assert "will relaunch" not in output
    assert "blocked, not pending progress" in output
    items = cli._collect_attention_items(  # noqa: SLF001
        s,
        for_agent="lead",
        roster=["lead", "worker"],
    )
    hold = next(item for item in items if item["source"] == "process_tree_hold")
    assert hold["restart_request"]["pending_progress"] is False
    assert hold["restart_request"]["request_id"].startswith("rr-")


@pytest.mark.parametrize("corrupt_supervisor_state", [False, True])
def test_request_restart_without_known_hold_never_promises_relaunch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    corrupt_supervisor_state: bool,
) -> None:
    s = _team(tmp_path)
    s.set_role("lead", "lead")
    if corrupt_supervisor_state:
        (s.dir / "supervisor-state.json").write_text("{broken", encoding="utf-8")

    assert _run([
        "request-restart", "--for", "worker", "--from", "lead",
        "--reason", "recover",
    ], tmp_path) == 0

    output = capsys.readouterr().out
    assert "will relaunch" not in output
    assert "does not establish that relaunch is currently admissible" in output


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


@pytest.mark.parametrize(
    "request_id",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param(7, id="integer"),
        pytest.param(True, id="boolean"),
        pytest.param("unsafe id", id="unsafe-string"),
    ],
)
def test_clear_restart_rejects_unusable_id_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_id: object,
) -> None:
    store = _team(tmp_path)

    def unexpected_read(_agent: str) -> None:
        pytest.fail("invalid clear id reached the restart-marker reader")

    monkeypatch.setattr(store, "read_restart_request", unexpected_read)

    assert store.clear_restart_request("worker", request_id) is False


@pytest.mark.parametrize(
    "request_id",
    [
        pytest.param(None, id="none"),
        pytest.param("rr-old", id="safe-mismatch"),
    ],
)
@pytest.mark.parametrize("marker_payload", _UNUSABLE_RESTART_MARKER_PAYLOADS)
def test_clear_restart_preserves_unusable_marker_bytes(
    tmp_path: Path,
    marker_payload: bytes,
    request_id: object,
) -> None:
    store = _team(tmp_path)
    marker_path = store.state_dir / "worker.restart-request"
    marker_path.write_bytes(marker_payload)

    assert store.clear_restart_request("worker", request_id) is False
    assert marker_path.read_bytes() == marker_payload


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
    assert "Start-WrapperProcess $startArgs" in ps
    assert "ArgumentList" in ps and "PassThru = $true" in ps
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


def test_supervisor_config_loader_binds_the_exact_accepted_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "supervisor.json"
    accepted = json.dumps({"agents": {"worker": {"cli": "codex"}}}).encode()
    path.write_bytes(accepted)
    digest = hashlib.sha256(accepted).hexdigest()

    assert sup.load_supervisor_config(path, expected_sha256=digest) == {
        "agents": {"worker": {"cli": "codex"}},
    }

    path.write_bytes(accepted + b"\n")
    with pytest.raises(
        sup.SupervisorPersistenceError,
        match="changed after PowerShell accepted it",
    ):
        sup.load_supervisor_config(path, expected_sha256=digest)


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


def test_supervise_plan_holds_when_accepted_config_bytes_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    config_path = s.dir / "supervisor.json"
    accepted = json.dumps(_CONFIG).encode("utf-8")
    accepted_sha256 = hashlib.sha256(accepted).hexdigest()
    config_path.write_bytes(accepted + b"\n")
    state_file = s.dir / "supervisor-state.json"
    state_file.write_text(
        json.dumps({"agents": {"worker": {}}}), encoding="utf-8",
    )

    rc = _run([
        "supervise", "--plan", "--state-file", str(state_file),
        "--supervisor-config-sha256", accepted_sha256,
        "--now", str(NOW),
    ], tmp_path)

    captured = capsys.readouterr()
    assert rc == 3
    assert captured.out == ""
    assert "changed after PowerShell accepted it" in captured.err


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


def test_supervise_record_launch_refuses_missing_reserved_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = _team(tmp_path)
    state_file = s.dir / "supervisor-state.json"

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    assert rc == 3
    assert "accepted launch record context is missing" in capsys.readouterr().err
    assert not state_file.exists()


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
        json.dumps({
            "agents": {
                "worker": {
                    "last_launch_epoch": NOW - 100,
                    "pending_launch_record": _record_launch_context(
                        cli_name="claude",
                        grace_seconds=75,
                    ),
                },
            },
        }),
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
    assert "pending_launch_record" not in persisted


def test_record_launch_uses_pre_spawn_context_after_config_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _team(tmp_path)
    config_path = store.dir / "supervisor.json"
    config_path.write_text(
        json.dumps({
            "agents": {
                "worker": {
                    "auto_restart": True,
                    "cli": "codex",
                    "wrapped": False,
                    "brain_pattern": "accepted-child",
                },
            },
            "launch_grace_seconds": 75,
        }),
        encoding="utf-8",
    )
    accepted_config = {
        "root": TEST_ROOT,
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "codex",
                "activity_hook": True,
                "wrapped": False,
                "brain_pattern": "accepted-child",
            },
        },
        "backoff": _CONFIG["backoff"],
        "launch_grace_seconds": 75,
    }
    plan = _plan(
        _report(restart_request=_auth_marker("rr-record-context")),
        {"agents": {"worker": _ready(backoff_next_epoch=0)}},
        config=accepted_config,
        snapshot=[],
    )
    assert plan["action"] == sup.RELAUNCH
    accepted_context = plan["record_launch_context"]
    assert accepted_context == {
        "schema_version": 1,
        "agent": "worker",
        "cli": "codex",
        "grace_seconds": 75.0,
        "wrapped": False,
        "brain_pattern": "accepted-child",
    }
    # This is the dangerous cut point: the child already exists, but the file
    # accepted by the planner changes before record-launch runs. Recording must
    # use the bounded pre-spawn context, not reopen this mutable file.
    config_path.write_text(
        json.dumps({
            "agents": {
                "worker": {
                    "auto_restart": True,
                    "cli": "codex",
                    "wrapped": True,
                    "brain_pattern": "drifted-child",
                },
            },
            "launch_grace_seconds": 999,
        }),
        encoding="utf-8",
    )
    state_file = store.dir / "supervisor-state.json"
    reserved_state = plan["next_state"]
    reserved_state["pending_launch_record"] = accepted_context
    state_file.write_text(
        json.dumps({"agents": {"worker": reserved_state}}),
        encoding="utf-8",
    )
    pre_snapshot = store.dir / "record-pre.json"
    post_snapshot = store.dir / "record-post.json"
    pre_snapshot.write_text("[]", encoding="utf-8")
    post_snapshot.write_text(
        json.dumps([
            _proc(
                778,
                777,
                "accepted-child.exe",
                "accepted-child.exe",
                BRAIN_START,
            ),
        ]),
        encoding="utf-8",
    )

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--pid-start", LAUNCHER_START,
        "--state-file", str(state_file), "--now", str(NOW),
        "--pre-snapshot-file", str(pre_snapshot),
        "--post-snapshot-file", str(post_snapshot),
    ], tmp_path)

    assert rc == 0
    persisted = sup.load_supervisor_state(state_file)["agents"]["worker"]
    assert persisted["launch_grace_until"] == NOW + 75
    assert [row["pid"] for row in persisted["managed_pids"]] == [778]
    assert persisted["managed_pids"][0]["seed_descendants"] is True
    assert "pending_launch_record" not in persisted
    persisted_before_replay = state_file.read_bytes()

    replay_rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--pid-start", LAUNCHER_START,
        "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    assert replay_rc == 3
    assert "accepted launch record context is missing" in capsys.readouterr().err
    assert state_file.read_bytes() == persisted_before_replay


def test_record_launch_rejects_missing_accepted_context_before_state_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _team(tmp_path)
    state_file = store.dir / "supervisor-state.json"
    state_file.write_text(
        json.dumps({"agents": {"worker": {"sentinel": "unchanged"}}}),
        encoding="utf-8",
    )
    before = state_file.read_bytes()

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    assert rc == 3
    assert "accepted launch record context is missing" in capsys.readouterr().err
    assert state_file.read_bytes() == before


@pytest.mark.parametrize(
    "corruption",
    [
        "float_schema",
        "extra_field",
        "wrong_agent",
        "string_grace",
        "integer_wrapped",
        "non_string_brain_pattern",
        "oversized_brain_pattern",
        "control_brain_pattern",
    ],
)
def test_record_launch_rejects_malformed_reserved_context_before_state_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    corruption: str,
) -> None:
    store = _team(tmp_path)
    context = _record_launch_context()
    if corruption == "float_schema":
        context["schema_version"] = 1.0
    elif corruption == "extra_field":
        context["unaccepted"] = True
    elif corruption == "wrong_agent":
        context["agent"] = "lead"
    elif corruption == "string_grace":
        context["grace_seconds"] = "120"
    elif corruption == "integer_wrapped":
        context["wrapped"] = 1
    elif corruption == "non_string_brain_pattern":
        context["brain_pattern"] = None
    elif corruption == "oversized_brain_pattern":
        context["brain_pattern"] = "x" * 4097
    else:
        context["brain_pattern"] = "child\nother"
    state_file = store.dir / "supervisor-state.json"
    state_file.write_text(
        json.dumps({
            "agents": {
                "worker": {
                    "sentinel": "unchanged",
                    "pending_launch_record": context,
                },
            },
        }),
        encoding="utf-8",
    )
    before = state_file.read_bytes()

    rc = _run([
        "supervise", "--record-launch", "--for", "worker", "--cli", "codex",
        "--pid", "777", "--state-file", str(state_file), "--now", str(NOW),
    ], tmp_path)

    assert rc == 3
    assert "accepted launch record" in capsys.readouterr().err
    assert state_file.read_bytes() == before


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
    # Eight durability cut points: the eighth clears the one-shot accepted
    # launch context when Start-Process returns no PID, so stale authority is
    # not left behind for a later record call.
    assert poll_loop.count("Save-StateForPoll $state") == 8
    assert poll_loop.count("if (-not (Save-StateForPoll $state))") == 8
    assert poll_loop.count("continue supervisorPoll") >= 8
    reserve = "Set-AgentState $state $name $p.next_state\n          if (-not (Save-StateForPoll $state))"
    launch_index = poll_loop.index("$res = Launch $name $p $homeEnv")
    record_index = poll_loop.index("$recordArgs = @('--root', $Root, 'supervise', '--record-launch'")
    assert reserve in poll_loop
    assert poll_loop.index(reserve) < launch_index
    assert "Save-StateForPoll $state" not in poll_loop[launch_index:record_index]
    configured_recovery = poll_loop[poll_loop.index(
        "{ $_ -in 'relaunch','stuck_recover' }"
    ):poll_loop.index("$barrierPath = Join-Path", poll_loop.index(
        "{ $_ -in 'relaunch','stuck_recover' }"
    ))]
    assert configured_recovery.index(
        "Set-AgentState $state $name $p.barrier_state"
    ) < configured_recovery.index(
        "Save-StateForPoll $state"
    ) < configured_recovery.index("Stop-Tree $p.kill_targets")
    configured_barrier_hold = poll_loop[
        poll_loop.index("if (($null -eq $barrier) -or $barrier.blocked)"):
        poll_loop.index("# SEED the agent")
    ]
    assert configured_barrier_hold.index(
        ".owned_process_tree.status = 'invalid'"
    ) < configured_barrier_hold.index(
        "Set-AgentState $state $name $p.barrier_state"
    ) < configured_barrier_hold.index(
        "Save-StateForPoll $state"
    ) < configured_barrier_hold.index(
        "Write-Warning"
    ) < configured_barrier_hold.rindex("continue")
    ephemeral_loop = poll_loop[poll_loop.index(
        "foreach ($rid in $plan.ephemeral_reviewers"
    ):]
    assert "Set-EphemeralState $state $rid $p.next_entry" in ephemeral_loop
    assert ephemeral_loop.index(
        "Set-EphemeralState $state $rid $p.next_entry"
    ) < ephemeral_loop.index("Stop-Tree $p.kill_targets")
    assert ephemeral_loop.index(
        "Stop-Tree $p.kill_targets"
    ) < ephemeral_loop.index(
        "--launch-barrier --for $p.agent --request-id $rid"
    ) < ephemeral_loop.index(
        "$archiveArgs = @('--root', $Root, 'supervise', '--archive-launch-request'"
    )
    ephemeral_barrier_hold = ephemeral_loop[
        ephemeral_loop.index(
            "if (($null -eq $teardownBarrier) -or $teardownBarrier.blocked)"
        ):
        ephemeral_loop.index("$completionJson = '{}'")
    ]
    assert ephemeral_barrier_hold.index(
        ".owned_process_tree.status = 'invalid'"
    ) < ephemeral_barrier_hold.index(
        "Set-EphemeralState $state $rid $p.next_entry"
    ) < ephemeral_barrier_hold.index(
        "Save-StateForPoll $state"
    ) < ephemeral_barrier_hold.index(
        "Write-Warning"
    ) < ephemeral_barrier_hold.rindex("continue")
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


def test_generated_archive_mutations_carry_live_instance_identity() -> None:
    ps = sup.PS_TEMPLATE
    helper = ps[
        ps.index("function Supervisor-InstanceIdentityArgs"):
        ps.index("$InstanceToken = $null")
    ]
    assert "if (-not $InstanceToken)" in helper
    assert (
        "return @('--instance-token', $InstanceToken) + "
        "(Supervisor-IdentityArgs)"
    ) in helper

    poll_loop = ps[
        ps.index("$pollNum = 0\n:supervisorPoll do {"):
        ps.index("} while (-not $Once)")
    ]
    archive_assignment = (
        "$archiveArgs = @('--root', $Root, 'supervise', "
        "'--archive-launch-request'"
    )
    archive_chunks = poll_loop.split(archive_assignment)
    assert len(archive_chunks) == 5
    for chunk in archive_chunks[1:]:
        assignment = chunk.split(
            "Invoke-CheckedSupervisorMutation", 1,
        )[0]
        assert "(Supervisor-InstanceIdentityArgs)" in assignment


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


def test_supervisor_tutorial_documents_work_heartbeat_is_not_cli_progress() -> None:
    text = Path("docs/supervisor-tutorial.md").read_text(encoding="utf-8")
    assert "work-heartbeat timer is coordination visibility" in text
    assert "never advances `progress_sequence`" in text
    assert "never automatic kill authority" in text


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
    # Only the two green wrapped states count as healthy, not every
    # action=='none' state (CLI_CHILD_UNKNOWN and LAUNCHING are non-green).
    assert "$p.state -in 'HEALTHY_IDLE','HEALTHY_WORKING'" in ps
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


@pytest.mark.parametrize("decision_state", ["PROCESS_TREE_INVALID", "PROCESS_TREE_TRUNCATED"])
def test_restart_request_blocked_by_process_tree_refusal_is_not_pending_progress(
    decision_state: str,
) -> None:
    report = _report(
        heartbeat_stale=True,
        restart_request=_auth_marker("rr-held"),
    )["agents"]["worker"]
    assessment = sup.supervisor_agent_assessment(
        "worker",
        report,
        {
            "action": sup.WARN_ONLY,
            "state": decision_state,
            "reason": "operator-visible process ownership refusal",
        },
    )

    assert assessment["restart_request"] == {
        "present": True,
        "pending": False,
        "blocked": True,
        "state": "blocked_by_process_tree_hold",
        "request_id": "rr-held",
        "requested_by": "lead",
    }


def test_completed_restart_marker_is_not_reported_as_pending_progress() -> None:
    report = _report(
        heartbeat_stale=False,
        restart_request=_auth_marker("rr-complete"),
    )["agents"]["worker"]
    assessment = sup.supervisor_agent_assessment(
        "worker",
        report,
        {
            "action": sup.CLEAR_MARKER,
            "state": "HEALTHY_IDLE",
            "reason": "restart reached readiness",
            "clear_marker": "rr-complete",
            "next_state": {
                "restart_request_state": "readiness_seen",
                "pending_restart_request_id": "rr-complete",
                "restart_requested_by": "lead",
            },
        },
    )

    assert assessment["restart_request"] == {
        "present": True,
        "pending": False,
        "blocked": False,
        "state": "readiness_seen",
        "request_id": "rr-complete",
        "requested_by": "lead",
    }


def test_current_restart_is_not_completed_by_an_old_request_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    state["agents"]["worker"].update({
        "restart_request_state": "readiness_seen",
        "pending_restart_request_id": "rr-old",
        "restart_requested_by": "lead",
    })
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    store.write_restart_request(
        "worker",
        {"agent": "worker", **_auth_marker("rr-new")},
    )

    stale_plan = {
        "action": sup.WARN_ONLY,
        "state": "PROCESS_TREE_TRUNCATED",
        "reason": "operator-visible process ownership refusal",
        "next_state": state["agents"]["worker"],
    }

    def fixed_plan(*_args, **_kwargs) -> dict:
        return {"agents": {"worker": stale_plan}}

    monkeypatch.setattr(sup, "plan_actions", fixed_plan)

    observation = sup.build_supervisor_observation(
        store,
        now_epoch=NOW,
        state=state,
        supervisor_config=_WRAP_CONFIG,
        snapshot=_wrap_snap(),
    )
    assessment = next(
        row for row in observation["agents"] if row["name"] == "worker"
    )
    marker = store.read_restart_request("worker")
    attention_item = att.process_tree_hold_items(
        state,
        restart_requests={"worker": marker},
        reset_admissions={"evaluated": True, "admissions": {}},
    )[0]

    assert assessment["restart_request"] == {
        "present": True,
        "pending": False,
        "blocked": True,
        "state": "blocked_by_process_tree_hold",
        "request_id": "rr-new",
        "requested_by": "lead",
    }
    assert attention_item["restart_request"] == {
        "request_id": "rr-new",
        "state": "blocked_by_process_tree_hold",
        "pending_progress": False,
    }


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param({"agent": "worker"}, id="missing-request-id"),
        pytest.param(
            {"agent": "worker", "request_id": ""},
            id="empty-request-id",
        ),
        pytest.param(
            {"agent": "worker", "request_id": 7},
            id="non-string-request-id",
        ),
    ],
)
def test_unusable_current_restart_never_borrows_old_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: dict,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    state["agents"]["worker"].update({
        "restart_request_state": "readiness_seen",
        "pending_restart_request_id": "rr-old",
        "restart_requested_by": "lead",
    })
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    (store.state_dir / "worker.restart-request").write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    stale_plan = {
        "action": sup.WARN_ONLY,
        "state": "PROCESS_TREE_INVALID",
        "reason": "operator-visible process ownership refusal",
        "next_state": state["agents"]["worker"],
    }
    monkeypatch.setattr(
        sup,
        "plan_actions",
        lambda *_args, **_kwargs: {"agents": {"worker": stale_plan}},
    )

    observation = sup.build_supervisor_observation(
        store,
        now_epoch=NOW,
        state=state,
        supervisor_config=_WRAP_CONFIG,
        snapshot=_wrap_snap(),
    )
    assessment = next(
        row for row in observation["agents"] if row["name"] == "worker"
    )
    assert observation["report"]["agents"]["worker"]["restart_request"] == {
        "unavailable": True,
    }
    unusable_marker = store.read_restart_request("worker")
    attention_item = att.process_tree_hold_items(
        state,
        restart_requests={"worker": unusable_marker},
        reset_admissions={"evaluated": True, "admissions": {}},
    )[0]

    assert assessment["restart_request"] == {
        "present": True,
        "pending": False,
        "blocked": True,
        "state": "blocked_by_process_tree_hold",
        "request_id": None,
        "unavailable": True,
        "requested_by": None,
    }
    assert attention_item["restart_request"] == {
        "request_id": None,
        "state": "blocked_by_process_tree_hold",
        "pending_progress": False,
        "unavailable": True,
    }


def test_unusable_restart_request_is_unknown_without_a_hold() -> None:
    progress = sup.restart_request_progress(
        {
            "restart_request_state": "readiness_seen",
            "pending_restart_request_id": "rr-old",
        },
        {"_agenttalk_restart_request_status": "unusable"},
    )

    assert progress == {
        "present": True,
        "pending": False,
        "blocked": False,
        "state": "unknown",
        "request_id": None,
        "unavailable": True,
    }


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
    assert "AGENTTALK_SHIM_PARENT" not in shim


def test_ps_template_applies_and_restores_env() -> None:
    """The executor applies the agent env (AGENTTALK_ROOT + AGENTTALK_PY +
    PYTHONPATH-src + per-agent env + CODEX_HOME) around Start-Process and
    RESTORES the supervisor's own env afterward. Launch-Spec removes the shim
    selector while a regular configured launch preserves ambient values."""
    ps = sup.PS_TEMPLATE
    ordinal_map = "[hashtable]::new([StringComparer]::Ordinal)"
    assert ps.count(f"$applied = {ordinal_map}") == 2
    assert ps.count(f"$saved = {ordinal_map}") == 2
    assert ps.count(
        "[AgenttalkSupervisorNativeV3]::CompareStringOrdinal("
    ) >= 2
    assert "$applied['AGENTTALK_ROOT'] = $Root" in ps
    assert "$applied['AGENTTALK_PY'] = $AgenttalkPython" in ps
    assert "$a.env" in ps                                  # applies per-agent env
    assert "'src') + ';' + $env:PYTHONPATH" in ps          # src on PYTHONPATH for module import
    assert "finally" in ps                                 # restore in a finally
    # LiteralPath is load-bearing: configured names may contain wildcard
    # characters, and restoration must remove only the exact variable that was
    # absent before launch. The runtime Launch-Spec test below proves both the
    # absent and pre-existing-value restoration branches.
    assert "Remove-Item -LiteralPath (\"Env:\"" in ps
    assert "Set-Item -LiteralPath (\"Env:\"" in ps
    assert "Remove-Item -Path (\"Env:\"" not in ps
    assert "Invoke-Expression" not in ps
    # Launch wires Quote-Arg -> a single joined command-line -> Start-Process
    # (BLOCKER 2): the raw $argv array must NEVER go straight to -ArgumentList,
    # or a token with a space splits in two at the handoff.
    assert "function Quote-Arg" in ps
    assert "$argline = (@($argv) | ForEach-Object { Quote-Arg" in ps
    assert "$startArgs['ArgumentList'] = $argline" in ps
    assert "$startArgs['ArgumentList'] = $argv" not in ps


def test_ps_template_applies_resolved_window_style_to_all_launches() -> None:
    ps = sup.PS_TEMPLATE
    assert ps.count("WindowStyle = $windowStyle") == 2
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
    sf.write_text(json.dumps({"agents": {"worker": {
        "pending_launch_record": _record_launch_context(),
    }}}), encoding="utf-8")
    # codex launch success: no --session-id passed
    assert _run(["supervise", "--record-launch", "--for", "worker", "--cli", "codex",
                 "--pid", "777", "--state-file", str(sf)], tmp_path) == 0
    e = json.loads(sf.read_text(encoding="utf-8"))["agents"]["worker"]
    assert e["pid"] == 777 and e["launched"] is True and e["session_id"] is None
    # claude launch success: minted id pinned
    sf.write_text(json.dumps({"agents": {"worker": {
        "pending_launch_record": _record_launch_context(cli_name="claude"),
    }}}), encoding="utf-8")
    assert _run(["supervise", "--record-launch", "--for", "worker", "--cli", "claude",
                 "--pid", "888", "--session-id", "sess-x", "--state-file", str(sf),
                 ],
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
    pending = _record_launch_context()
    st = {"agents": {"worker": {"launcher_pid": 199, "launching": True,
                                "launch_grace_until": NOW + 100,
                                "readiness_seen": False,
                                "pending_launch_record": pending}}}
    p = _plan(_report(heartbeat_stale=True), st, snapshot=[])
    assert p["action"] == sup.NONE and p["state"] == "LAUNCHING"
    assert p["discover_brain"] is True
    assert p["next_state"]["pending_launch_record"] == pending


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
    idx = sup._snap_index_and_excluded(_codex_tree())[0]
    b = sup._discover_brain(idx, "codex-test", LAUNCHER_PID, "codex",
                            allow_launcher_self=False)
    assert b["pid"] == BRAIN_PID                      # the TUI, not 199 / not 300


def test_discover_brain_tui_wins_even_when_launcher_iterates_first() -> None:
    idx = sup._snap_index_and_excluded(_codex_tree(order_launcher_first=True))[0]
    b = sup._discover_brain(idx, "codex-test", LAUNCHER_PID, "codex",
                            allow_launcher_self=False)
    assert b["pid"] == BRAIN_PID
    # and after the launcher has EXITED, the TUI is still found (parent_pid persists)
    idx2 = sup._snap_index_and_excluded(_codex_tree(launcher=False))[0]
    assert sup._discover_brain(idx2, "codex-test", LAUNCHER_PID, "codex",
                               allow_launcher_self=False)["pid"] == BRAIN_PID


def test_allow_launcher_self_true_picks_launcher() -> None:
    """A non-forking CLI (allow_launcher_self=true) still selects the launcher as
    its own brain (claude.exe) - the codex exclusion must not regress it."""
    idx = sup._snap_index_and_excluded(_codex_tree())[0]
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


@pytest.mark.parametrize(
    "child_tail_contains_loop",
    [False, True],
    ids=["missing-loop", "loop-only-in-child-tail"],
)
def test_supervise_bootstrap_check_requires_loop_before_child_tail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    child_tail_contains_loop: bool,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno"):
        s.write_heartbeat(name)
    wrapped = _wrapped_supervisor_agent("Zeno", "codex")
    windows_args = wrapped["launch"]["windows_args"]
    windows_args.remove("--loop")
    if child_tail_contains_loop:
        windows_args.append("--loop")
    _write_supervisor_config(s, {"Zeno": wrapped})

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    check = next(
        check
        for check in payload["checks"]
        if check["id"] == "supervisor_agent_launch_admission_refused"
        and check.get("agent") == "Zeno"
    )
    assert "requires --loop before the child delimiter" in check["detail"]


def test_supervise_bootstrap_check_rejects_environment_name_with_equals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno"):
        s.write_heartbeat(name)
    wrapped = _wrapped_supervisor_agent("Zeno", "codex")
    wrapped["env"] = {"BAD=NAME": "value"}
    _write_supervisor_config(s, {"Zeno": wrapped})

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    check = next(
        item
        for item in payload["checks"]
        if item["id"] == "supervisor_agent_environment_invalid"
        and item.get("agent") == "Zeno"
    )
    assert check["facts"]["issues"] == ["invalid_name"]

    wrapped["env"] = {"TAB\tNAME": "value"}
    _write_supervisor_config(s, {"Zeno": wrapped})
    rc = _run(["supervise", "--bootstrap-check"], tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert not [
        item
        for item in payload["checks"]
        if item["id"] == "supervisor_agent_environment_invalid"
    ]


def test_supervise_bootstrap_check_consumes_shared_typed_admission(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno"):
        s.write_heartbeat(name)

    invalid = []
    bad_type = _wrapped_supervisor_agent("Zeno", "codex")
    bad_type["launch"]["module_args_from"] = "1x"
    invalid.append(bad_type)
    out_of_range = _wrapped_supervisor_agent("Zeno", "codex")
    out_of_range["launch"]["module_args_from"] = 99
    invalid.append(out_of_range)
    wrong_token = _wrapped_supervisor_agent("Zeno", "codex")
    wrong_token["launch"]["windows_args"][0:0] = ["-u", "-Xutf8"]
    wrong_token["launch"]["module_args_from"] = 1
    invalid.append(wrong_token)
    disallowed_prefix = _wrapped_supervisor_agent("Zeno", "codex")
    disallowed_prefix["launch"]["windows_args"].insert(0, "-Z")
    disallowed_prefix["launch"]["module_args_from"] = 1
    invalid.append(disallowed_prefix)
    absent_with_prefix = _wrapped_supervisor_agent("Zeno", "codex")
    absent_with_prefix["launch"]["windows_args"].insert(0, "-u")
    invalid.append(absent_with_prefix)

    for configured in invalid:
        _write_supervisor_config(s, {"Zeno": configured})
        rc = _run(["supervise", "--bootstrap-check"], tmp_path)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert any(
            check["id"] == "supervisor_agent_launch_admission_refused"
            and check.get("agent") == "Zeno"
            for check in payload["checks"]
        )

    valid_prefix = _wrapped_supervisor_agent("Zeno", "codex")
    valid_prefix["launch"]["windows_args"].insert(0, "-u")
    valid_prefix["launch"]["module_args_from"] = 1
    for configured in (
        valid_prefix,
        _wrapped_supervisor_agent("Zeno", "codex"),
    ):
        _write_supervisor_config(s, {"Zeno": configured})
        rc = _run(["supervise", "--bootstrap-check"], tmp_path)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert any(
            check["id"] == "supervisor_agent_launch_admission_valid"
            and check.get("agent") == "Zeno"
            for check in payload["checks"]
        )


def test_supervise_bootstrap_check_accepts_the_filled_manual_archetype_scaffold(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Round 16 connector finding: making the module_args_from resolver
    delegation run unconditionally (round 14) also ran it against
    launches that are not Python at all. The MANUAL archetype's own
    generated scaffold (CONFIG_TEMPLATE's AGENT_NAME entry: a direct
    claude.exe/codex.exe launch, windows_args ["{SESSION_ARGS}"]) has no
    '-m'/'agenttalk' anywhere in windows_args - the whole question of a
    module boundary is meaningless for it - so a correctly filled-in
    version of our OWN scaffold failed our OWN validator with
    supervisor_agent_launch_module_args_from_wrong_token. Fixed by gating
    the whole module_args_from block on windows_file's stem being a
    recognized Python interpreter (the SAME set the runtime's own
    _agenttalk_argv checks), not by re-narrowing to "only when
    module_args_from is present" (that would reopen round 14's
    absent-prefix gap for genuinely Python launches). Kept as its own
    permanent test: our own generated scaffold failing our own validator
    must stay impossible to reintroduce."""
    s = _team(tmp_path, "Polaris,Deneb")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Deneb"):
        s.write_heartbeat(name)
    manual_template = json.loads(sup.CONFIG_TEMPLATE)["agents"]["AGENT_NAME"]
    assert "wrapped" not in manual_template
    manual = {
        **manual_template,
        "cwd": TEST_ROOT,
        "env": {"AGENTTALK_SELF": "Deneb"},
        "launch": {
            **manual_template["launch"],
            "windows_file": r"C:\Users\you\.local\bin\claude.exe",
        },
    }
    assert "{SESSION_ARGS}" in manual["launch"]["windows_args"]
    _write_supervisor_config(s, {"Deneb": manual})

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    payload = json.loads(capsys.readouterr().out)
    assert any(
        check["id"] == "supervisor_agent_launch_admission_valid"
        and check.get("agent") == "Deneb"
        for check in payload["checks"]
    )
    assert rc == 0


def test_supervise_bootstrap_check_accepts_constrained_ovh_qwen_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path, "Polaris,qwen-dev-1")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    s.set_trust_class("qwen-dev-1", "external-worker")
    for name in ("Polaris", "qwen-dev-1"):
        s.write_heartbeat(name)
    qwen = _wrapped_supervisor_agent("qwen-dev-1", "claude")
    qwen.pop("env")
    qwen.update({
        "backend_profile": "ovh-qwen",
        "model": "Qwen3.5-397B-A17B",
        "trust_class": "external-worker",
    })
    _write_supervisor_config(s, {"qwen-dev-1": qwen})
    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    profile = next(
        check for check in payload["checks"]
        if check["id"] == "supervisor_ovh_qwen_profile"
    )
    assert profile["status"] == "ok"


def test_supervise_bootstrap_check_rejects_uncapped_qwen_lead_loop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path, "Polaris,qwen-dev-1")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    s.set_trust_class("qwen-dev-1", "external-worker")
    for name in ("Polaris", "qwen-dev-1"):
        s.write_heartbeat(name)
    qwen = _wrapped_supervisor_agent("qwen-dev-1", "claude")
    qwen.pop("env")
    qwen["launch"]["windows_args"].insert(
        qwen["launch"]["windows_args"].index("--"), "--lead-loop"
    )
    qwen.update({
        "backend_profile": "ovh-qwen",
        "model": "Qwen3.5-397B-A17B",
        "trust_class": "external-worker",
    })
    _write_supervisor_config(s, {"qwen-dev-1": qwen})
    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    profile = next(
        check
        for check in payload["checks"]
        if check["id"] == "supervisor_ovh_qwen_profile"
    )
    assert profile["status"] == "error"
    assert "lead-loop is unsupported" in profile["detail"]


def test_supervise_bootstrap_check_rejects_ambient_provider_key_for_qwen(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path, "Polaris,qwen-dev-1")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    s.set_trust_class("qwen-dev-1", "external-worker")
    for name in ("Polaris", "qwen-dev-1"):
        s.write_heartbeat(name)
    qwen = _wrapped_supervisor_agent("qwen-dev-1", "claude")
    qwen.pop("env")
    qwen.update({
        "backend_profile": "ovh-qwen",
        "model": "Qwen3.5-397B-A17B",
        "trust_class": "external-worker",
    })
    _write_supervisor_config(s, {"qwen-dev-1": qwen})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-qwen")

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    profile = next(
        check for check in payload["checks"]
        if check["id"] == "supervisor_ovh_qwen_profile"
    )
    assert profile["status"] == "error"
    assert "ambient provider keys" in profile["detail"]


def test_supervisor_template_checks_qwen_secrets_before_agent_env() -> None:
    ps = sup.PS_TEMPLATE
    profile_check = ps.index("backend_profile -ceq 'ovh-qwen'")
    agent_env = ps.index("if ($a.env)")
    assert profile_check < agent_env
    assert "OVH_KEY" in ps[profile_check:agent_env]
    assert "ANTHROPIC_API_KEY" in ps[profile_check:agent_env]


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


def test_supervise_bootstrap_check_uses_shared_root_normalization(
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
    refused_agents = {
        c.get("agent") for c in payload["checks"]
        if c["id"] == "supervisor_agent_launch_admission_refused"
    }
    valid_agents = {
        c.get("agent") for c in payload["checks"]
        if c["id"] == "supervisor_agent_launch_admission_valid"
    }
    assert refused_agents == {"Cygnus"}
    assert "Altair" in valid_agents


def test_supervise_bootstrap_accepts_shared_parser_root_abbreviation_and_tail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path, "Polaris,Zeno")
    s.set_role("Polaris", "lead")
    s.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno"):
        s.write_heartbeat(name)
    wrapped = _wrapped_supervisor_agent("Zeno", "codex")
    args = wrapped["launch"]["windows_args"]
    args[args.index("--root")] = "--roo"
    args.remove("--")
    _write_supervisor_config(s, {"Zeno": wrapped})

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(
        check["id"] == "supervisor_agent_launch_admission_valid"
        and check.get("agent") == "Zeno"
        for check in payload["checks"]
    )


@pytest.mark.parametrize("loop_option", ["--lo", "--loo"])
def test_supervise_bootstrap_check_accepts_loop_abbreviations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    loop_option: str,
) -> None:
    store = _team(tmp_path, "Polaris,Zeno")
    store.set_role("Polaris", "lead")
    store.set_operator_facing("Polaris")
    for name in ("Polaris", "Zeno"):
        store.write_heartbeat(name)
    wrapped = _wrapped_supervisor_agent("Zeno", "codex")
    args = wrapped["launch"]["windows_args"]
    args[args.index("--loop")] = loop_option
    _write_supervisor_config(store, {"Zeno": wrapped})

    rc = _run(["supervise", "--bootstrap-check"], tmp_path)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(
        check["id"] == "supervisor_agent_launch_admission_valid"
        and check.get("agent") == "Zeno"
        for check in payload["checks"]
    )


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
    # The agent env carries AGENTTALK_PY. The baked AGENTTALK_PYTHON assignment
    # remains shim-local; a regular launch may still inherit an ambient value.
    assert "$applied['AGENTTALK_ROOT'] = $Root" in ps
    assert "$applied['AGENTTALK_PY'] = $AgenttalkPython" in ps
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
    ci = pf.index("$plan.cli -ceq 'codex'")
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
    # Phase C: a WRAPPED agent is preflighted before the codex branch using the
    # dispatch form carried by the typed artifact, never as the child CLI.
    wrap_branch = pf[pf.index("$plan.launch_mode -eq 'wrap'"):ci]
    # round 24 connector finding: the wrapped probe must carry the SAME
    # declared prefix tokens the real launch uses, not a fixed
    # `-m agenttalk --version` stand-in - see
    # test_preflight_wrapped_smoke_test_uses_the_configured_prefix.
    assert "& $file @prefixTokens -m agenttalk --version" in wrap_branch
    assert "& $file --version" in wrap_branch
    assert "$null -eq $admission.module_args_from" in wrap_branch
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


def _hook_commands(path: Path, event: str) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        h["command"]
        for group in data["hooks"][event]
        for h in group.get("hooks", [])
        if isinstance(h, dict) and "command" in h
    ]


def _post_tool_commands(path: Path) -> list[str]:
    return _hook_commands(path, "PostToolUse")


def _managed_hook_group(matcher: str, command: str) -> list[dict]:
    return [{
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }]


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
    assert data["hooks"]["PreCompact"] == [{
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": "agenttalk checkpoint save --hook || agenttalk heartbeat --hook",
        }],
    }]
    assert data["hooks"]["SessionStart"] == [{
        "matcher": "compact",
        "hooks": [{
            "type": "command",
            "command": "agenttalk checkpoint resume --hook || agenttalk heartbeat --hook",
        }],
    }]
    # idempotent: second install adds nothing
    first_install = settings.read_text(encoding="utf-8")
    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    assert settings.read_text(encoding="utf-8") == first_install
    data2 = json.loads(settings.read_text(encoding="utf-8"))
    post = [h["command"] for g in data2["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert post.count("agenttalk heartbeat --hook") == 1


def test_install_activity_hook_adds_checkpoint_hooks_to_existing_heartbeat(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": "agenttalk heartbeat --hook"}],
    }]}}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    assert _hook_commands(settings, "PreCompact") == [
        "agenttalk checkpoint save --hook || agenttalk heartbeat --hook",
    ]
    assert _hook_commands(settings, "SessionStart") == [
        "agenttalk checkpoint resume --hook || agenttalk heartbeat --hook",
    ]


def test_install_activity_hook_reports_partial_malformed_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    malformed = {"matcher": "*"}
    settings.write_text(
        json.dumps({"hooks": {"PreCompact": malformed}}),
        encoding="utf-8",
    )

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    output = capsys.readouterr().out
    assert f"installed: {settings} [PostToolUse]" in output
    assert (
        f"skipped (malformed hooks.PreCompact): {settings} [PreCompact]"
        in output
    )
    assert f"installed: {settings} [SessionStart]" in output
    assert "already:" not in output
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["PreCompact"] == malformed
    assert _hook_commands(settings, "SessionStart") == [
        "agenttalk checkpoint resume --hook || agenttalk heartbeat --hook",
    ]


def test_install_activity_hook_reports_malformed_hooks_container(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"hooks": []})
    settings.write_text(original, encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    output = capsys.readouterr().out
    for event in ("PostToolUse", "PreCompact", "SessionStart"):
        assert f"skipped (malformed hooks): {settings} [{event}]" in output
    assert "already:" not in output
    assert settings.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("entry_type", "first_status"),
    [
        (None, "installed"),
        ("prompt", "installed"),
        ("command", "already"),
    ],
    ids=["missing-type", "wrong-type", "correct-type"],
)
@pytest.mark.parametrize(
    "fallback_agent",
    [None, "lead"],
    ids=["neutral", "preserved-fallback"],
)
def test_install_activity_hook_repairs_managed_hook_types(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    entry_type: str | None,
    first_status: str,
    fallback_agent: str | None,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)

    def managed(command: str) -> dict:
        item = {"command": command}
        if entry_type is not None:
            item["type"] = entry_type
        return item

    expected = {
        "PostToolUse": (
            sup.fallback_activity_hook_command(fallback_agent)
            if fallback_agent is not None
            else sup.ACTIVITY_HOOK_COMMAND
        ),
        "PreCompact": sup.checkpoint_hook_command("save", fallback_agent),
        "SessionStart": sup.checkpoint_hook_command("resume", fallback_agent),
    }
    original = json.dumps({"hooks": {
        "PostToolUse": [
            {"matcher": "*", "hooks": [managed(expected["PostToolUse"])]},
        ],
        "PreCompact": [
            {"matcher": "*", "hooks": [managed(expected["PreCompact"])]},
        ],
        "SessionStart": [
            {
                "matcher": "compact",
                "hooks": [managed(expected["SessionStart"])],
            },
        ],
    }}, indent=2)
    settings.write_text(original, encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    output = capsys.readouterr().out
    for event in ("PostToolUse", "PreCompact", "SessionStart"):
        assert f"{first_status}: {settings} [{event}]" in output
    repaired = settings.read_text(encoding="utf-8")
    if entry_type == "command":
        assert repaired == original

    hooks = json.loads(repaired)["hooks"]
    for event, command in expected.items():
        items = [
            item
            for group in hooks[event]
            for item in group["hooks"]
            if item.get("command") == command
        ]
        assert items == [{"type": "command", "command": command}]

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    second_output = capsys.readouterr().out
    for event in ("PostToolUse", "PreCompact", "SessionStart"):
        assert f"already: {settings} [{event}]" in second_output
    assert settings.read_text(encoding="utf-8") == repaired


def test_claude_hook_snippet_includes_all_managed_hooks() -> None:
    hooks = json.loads(sup.claude_hook_snippet())["hooks"]

    assert hooks["PostToolUse"] == [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": "agenttalk heartbeat --hook"}],
    }]
    assert hooks["PreCompact"] == [{
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": "agenttalk checkpoint save --hook || agenttalk heartbeat --hook",
        }],
    }]
    assert hooks["SessionStart"] == [{
        "matcher": "compact",
        "hooks": [{
            "type": "command",
            "command": "agenttalk checkpoint resume --hook || agenttalk heartbeat --hook",
        }],
    }]


def test_install_activity_hook_upgrades_and_dedupes_checkpoint_hooks(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {
        "PreCompact": [
            {"matcher": "Edit", "hooks": [
                {"type": "command", "command": "agenttalk checkpoint save --hook"},
                {"type": "command", "command": "echo preserve-pre"},
            ]},
            {"matcher": "*", "hooks": [
                {"type": "command", "command": "agenttalk checkpoint save --hook"},
            ]},
        ],
        "SessionStart": [
            {"matcher": "startup", "hooks": [
                {"type": "command", "command": "agenttalk checkpoint resume --hook"},
                {"type": "command", "command": "echo preserve-start"},
            ]},
        ],
    }}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    data = json.loads(settings.read_text(encoding="utf-8"))
    precompact = [
        (group["matcher"], hook["command"])
        for group in data["hooks"]["PreCompact"]
        for hook in group.get("hooks", [])
        if hook.get("command", "").startswith("agenttalk checkpoint")
    ]
    session_start = [
        (group["matcher"], hook["command"])
        for group in data["hooks"]["SessionStart"]
        for hook in group.get("hooks", [])
        if hook.get("command", "").startswith("agenttalk checkpoint")
    ]
    assert precompact == [
        ("*", "agenttalk checkpoint save --hook || agenttalk heartbeat --hook"),
    ]
    assert session_start == [
        (
            "compact",
            "agenttalk checkpoint resume --hook || agenttalk heartbeat --hook",
        ),
    ]
    assert "echo preserve-pre" in _hook_commands(settings, "PreCompact")
    assert "echo preserve-start" in _hook_commands(settings, "SessionStart")


def test_install_activity_hook_prunes_only_groups_emptied_by_relocation(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)
    settings = s.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {
        "PreCompact": [
            {"matcher": "manual", "hooks": [
                {"type": "command", "command": "agenttalk checkpoint save --hook"},
            ]},
            {"matcher": "operator-empty", "hooks": []},
        ],
        "SessionStart": [
            {"matcher": "startup", "hooks": [
                {"type": "command", "command": "agenttalk checkpoint resume --hook"},
            ]},
            {"matcher": "operator-empty", "hooks": []},
        ],
    }}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert [group["matcher"] for group in hooks["PreCompact"]] == [
        "operator-empty",
        "*",
    ]
    assert [group["matcher"] for group in hooks["SessionStart"]] == [
        "operator-empty",
        "compact",
    ]


def test_install_activity_hook_codex_uses_group_shape(tmp_path: Path) -> None:
    """Blocker 2: the Codex hook must be the matcher-GROUP shape (mirroring
    Claude), not a flat {type,command} — else it mis-installs and the
    presence-check duplicates a correctly-shaped existing hook."""
    s = _team(tmp_path)
    assert _run(["supervise", "--install-activity-hook", "--codex-only"], tmp_path) == 0
    hooks_file = s.root / ".codex" / "hooks.json"
    assert hooks_file.exists()
    assert not (s.root / ".claude" / "settings.json").exists()  # codex-only
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"PostToolUse"}  # checkpoint hooks are Claude-only in B1
    groups = hooks["PostToolUse"]
    # matcher-group shape: each group has a NESTED hooks list
    assert groups[0]["matcher"] == "*"
    assert groups[0]["hooks"][0]["command"] == "agenttalk heartbeat --hook"
    # idempotent (presence-check sees the nested shape)
    assert _run(["supervise", "--install-activity-hook", "--codex-only"], tmp_path) == 0
    groups2 = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    cmds = [h["command"] for g in groups2 for h in g["hooks"]]
    assert cmds.count("agenttalk heartbeat --hook") == 1


def test_install_activity_hook_codex_keeps_checkpoint_hooks_claude_only(
    tmp_path: Path,
) -> None:
    s = _team(tmp_path)

    assert _run(["supervise", "--install-activity-hook", "--codex"], tmp_path) == 0

    claude_settings = s.root / ".claude" / "settings.json"
    assert _hook_commands(claude_settings, "PreCompact") == [
        "agenttalk checkpoint save --hook || agenttalk heartbeat --hook",
    ]
    assert _hook_commands(claude_settings, "SessionStart") == [
        "agenttalk checkpoint resume --hook || agenttalk heartbeat --hook",
    ]

    codex_hooks = json.loads(
        (s.root / ".codex" / "hooks.json").read_text(encoding="utf-8"),
    )["hooks"]
    assert set(codex_hooks) == {"PostToolUse"}
    assert [
        hook["command"]
        for group in codex_hooks["PostToolUse"]
        for hook in group["hooks"]
    ] == ["agenttalk heartbeat --hook"]


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
    assert _hook_commands(settings, "PreCompact") == [
        "agenttalk checkpoint save --hook --fallback-for lead"
        " || agenttalk heartbeat --hook --fallback-for lead",
    ]
    assert _hook_commands(settings, "SessionStart") == [
        "agenttalk checkpoint resume --hook --fallback-for lead"
        " || agenttalk heartbeat --hook --fallback-for lead",
    ]
    assert not (s.root / ".codex" / "hooks.json").exists()

    assert _run(["supervise", "--install-activity-hook", "--interactive-for", "lead"], tmp_path) == 0
    assert _post_tool_commands(settings).count("agenttalk heartbeat --hook --fallback-for lead") == 1
    assert _hook_commands(settings, "PreCompact").count(
        "agenttalk checkpoint save --hook --fallback-for lead"
        " || agenttalk heartbeat --hook --fallback-for lead",
    ) == 1
    assert _hook_commands(settings, "SessionStart").count(
        "agenttalk checkpoint resume --hook --fallback-for lead"
        " || agenttalk heartbeat --hook --fallback-for lead",
    ) == 1


def _legacy_agenttalk_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "legacy-bin"
    fake_bin.mkdir()
    if os.name == "nt":
        (fake_bin / "agenttalk.cmd").write_text(
            "@echo off\r\n"
            "if /i \"%~1\"==\"checkpoint\" exit /b 2\r\n"
            "if /i \"%~1\"==\"heartbeat\" exit /b 0\r\n"
            "exit /b 9\r\n",
            encoding="ascii",
        )
    else:
        executable = fake_bin / "agenttalk"
        executable.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = checkpoint ] && exit 2\n"
            "[ \"$1\" = heartbeat ] && exit 0\n"
            "exit 9\n",
            encoding="ascii",
        )
        executable.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
    }


def _real_argparse_failure_agenttalk_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "real-argparse-bin"
    fake_bin.mkdir()
    if os.name == "nt":
        (fake_bin / "agenttalk.cmd").write_text(
            "@echo off\r\n"
            'if /i "%~1"=="checkpoint" goto checkpoint\r\n'
            'if /i "%~1"=="heartbeat" exit /b 0\r\n'
            "exit /b 9\r\n"
            ":checkpoint\r\n"
            f'"{sys.executable}" -m agenttalk checkpoint --definitely-invalid\r\n'
            "exit /b %errorlevel%\r\n",
            encoding="ascii",
        )
    else:
        executable = fake_bin / "agenttalk"
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = checkpoint ]; then\n'
            f"  exec {shlex.quote(sys.executable)} -m agenttalk "
            "checkpoint --definitely-invalid\n"
            "fi\n"
            '[ "$1" = heartbeat ] && exit 0\n'
            "exit 9\n",
            encoding="ascii",
        )
        executable.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }


_GUARDED_CHECKPOINT_COMMANDS = [
    "agenttalk checkpoint save --hook --fallback-for lead"
    " || agenttalk heartbeat --hook --fallback-for lead",
    "agenttalk checkpoint resume --hook --fallback-for lead"
    " || agenttalk heartbeat --hook --fallback-for lead",
]


def test_checkpoint_hook_guard_keeps_real_argparse_usage_off_stdout(
    tmp_path: Path,
) -> None:
    command = sup.CHECKPOINT_SAVE_HOOK_COMMAND
    shell = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
        if os.name == "nt"
        else ["/bin/sh", "-c", command]
    )

    result = subprocess.run(
        shell,
        env=_real_argparse_failure_agenttalk_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "usage:" in result.stderr.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh contract")
@pytest.mark.parametrize("command", _GUARDED_CHECKPOINT_COMMANDS)
def test_checkpoint_hook_guard_masks_legacy_exit_two_in_posix_sh(
    tmp_path: Path,
    command: str,
) -> None:
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env=_legacy_agenttalk_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd contract")
@pytest.mark.parametrize("command", _GUARDED_CHECKPOINT_COMMANDS)
def test_checkpoint_hook_guard_masks_legacy_exit_two_in_cmd(
    tmp_path: Path,
    command: str,
) -> None:
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command],
        env=_legacy_agenttalk_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="Windows PowerShell Core contract",
)
@pytest.mark.parametrize("command", _GUARDED_CHECKPOINT_COMMANDS)
def test_checkpoint_hook_guard_masks_legacy_exit_two_in_pwsh(
    command: str,
) -> None:
    legacy_agenttalk = (
        "function agenttalk { "
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]] $Rest); "
        "if ($Rest[0] -eq 'checkpoint') { cmd.exe /d /c exit 2 } "
        "elseif ($Rest[0] -eq 'heartbeat') { cmd.exe /d /c exit 0 } "
        "else { cmd.exe /d /c exit 9 }"
        "}; "
    )
    result = subprocess.run(
        [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            legacy_agenttalk + command,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


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


@pytest.mark.parametrize(
    ("existing_agent", "expected_status"),
    [
        ("worker", "installed"),
        ("lead", "already"),
    ],
    ids=["stale-fallback-rebound", "matching-fallback-unchanged"],
)
def test_install_activity_hook_explicit_identity_controls_all_managed_hooks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    existing_agent: str,
    expected_status: str,
) -> None:
    store = _team(tmp_path)
    store.set_operator_facing("lead")
    settings = store.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {
        "PostToolUse": _managed_hook_group(
            "*", sup.fallback_activity_hook_command(existing_agent),
        ),
        "PreCompact": _managed_hook_group(
            "*", sup.checkpoint_hook_command("save", existing_agent),
        ),
        "SessionStart": _managed_hook_group(
            "compact", sup.checkpoint_hook_command("resume", existing_agent),
        ),
    }}), encoding="utf-8")

    assert _run(
        ["supervise", "--install-activity-hook", "--interactive-for", "lead"],
        tmp_path,
    ) == 0

    output = capsys.readouterr().out
    for event in ("PostToolUse", "PreCompact", "SessionStart"):
        assert f"{expected_status}: {settings} [{event}]" in output
    assert _post_tool_commands(settings) == [
        sup.fallback_activity_hook_command("lead"),
    ]
    assert _hook_commands(settings, "PreCompact") == [
        sup.checkpoint_hook_command("save", "lead"),
    ]
    assert _hook_commands(settings, "SessionStart") == [
        sup.checkpoint_hook_command("resume", "lead"),
    ]


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

    assert "installed:" in capsys.readouterr().out
    cmds = _post_tool_commands(settings)
    assert cmds == [fallback]
    assert _recognized_heartbeat_commands(cmds) == [fallback]
    assert _hook_commands(settings, "PreCompact") == [
        "agenttalk checkpoint save --hook --fallback-for lead"
        " || agenttalk heartbeat --hook --fallback-for lead",
    ]
    assert _hook_commands(settings, "SessionStart") == [
        "agenttalk checkpoint resume --hook --fallback-for lead"
        " || agenttalk heartbeat --hook --fallback-for lead",
    ]

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0
    assert "already:" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("shape", "expected_statuses"),
    [
        (
            "only-precompact-bound",
            {"PostToolUse": "already", "PreCompact": "already", "SessionStart": "installed"},
        ),
        (
            "only-sessionstart-bound",
            {"PostToolUse": "already", "PreCompact": "installed", "SessionStart": "already"},
        ),
        (
            "both-neutral",
            {"PostToolUse": "already", "PreCompact": "installed", "SessionStart": "installed"},
        ),
    ],
)
def test_install_activity_hook_uses_one_checkpoint_fallback_for_partial_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    shape: str,
    expected_statuses: dict[str, str],
) -> None:
    store = _team(tmp_path)
    settings = store.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)

    def group(matcher: str, command: str) -> list[dict]:
        return [{
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command}],
        }]

    if shape == "only-precompact-bound":
        hooks = {
            "PostToolUse": group(
                "*", sup.fallback_activity_hook_command("worker"),
            ),
            "PreCompact": group(
                "*", sup.checkpoint_hook_command("save", "lead"),
            ),
        }
    elif shape == "only-sessionstart-bound":
        hooks = {
            "PostToolUse": group(
                "*", sup.fallback_activity_hook_command("worker"),
            ),
            "SessionStart": group(
                "compact", sup.checkpoint_hook_command("resume", "lead"),
            ),
        }
    else:
        hooks = {
            "PostToolUse": group(
                "*", sup.fallback_activity_hook_command("lead"),
            ),
            "PreCompact": group(
                "*", sup.checkpoint_hook_command("save"),
            ),
            "SessionStart": group(
                "compact", sup.checkpoint_hook_command("resume"),
            ),
        }
    settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    output = capsys.readouterr().out
    for event, status in expected_statuses.items():
        assert f"{status}: {settings} [{event}]" in output
    save_command = sup.checkpoint_hook_command("save", "lead")
    resume_command = sup.checkpoint_hook_command("resume", "lead")
    assert _hook_commands(settings, "PreCompact") == [save_command]
    assert _hook_commands(settings, "SessionStart") == [resume_command]

    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b"{}"), encoding="utf-8"),
    )
    save_argv = shlex.split(save_command.split(" || ", 1)[0])[1:]
    assert _run(save_argv, tmp_path) == 0
    assert capsys.readouterr() == ("", "")
    saved = checkpoint.read_checkpoint(store, "lead")
    assert saved is not None
    assert saved["agent"] == "lead"
    assert checkpoint.read_checkpoint(store, "worker") is None

    resume_argv = shlex.split(resume_command.split(" || ", 1)[0])[1:]
    assert _run(resume_argv, tmp_path) == 0
    envelope = json.loads(capsys.readouterr().out)
    context = envelope["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Checkpoint reload for lead:")


@pytest.mark.parametrize(
    ("checkpoint_agent", "expected_agent", "precompact_status"),
    [
        ("retired", "worker", "installed"),
        ("lead", "lead", "already"),
    ],
    ids=["retired-fallback-ignored", "active-fallback-preserved"],
)
def test_install_activity_hook_neutral_uses_only_rostered_checkpoint_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    checkpoint_agent: str,
    expected_agent: str,
    precompact_status: str,
) -> None:
    store = _team(tmp_path, "lead,worker,retired")
    store.retire_agent("retired", reason="renamed")
    settings = store.root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {
        "PostToolUse": _managed_hook_group(
            "*", sup.fallback_activity_hook_command("worker"),
        ),
        "PreCompact": _managed_hook_group(
            "*", sup.checkpoint_hook_command("save", checkpoint_agent),
        ),
    }}), encoding="utf-8")

    assert _run(["supervise", "--install-activity-hook"], tmp_path) == 0

    output = capsys.readouterr().out
    expected_statuses = {
        "PostToolUse": "already",
        "PreCompact": precompact_status,
        "SessionStart": "installed",
    }
    for event, status in expected_statuses.items():
        assert f"{status}: {settings} [{event}]" in output
    assert _post_tool_commands(settings) == [
        sup.fallback_activity_hook_command("worker"),
    ]
    save_command = sup.checkpoint_hook_command("save", expected_agent)
    resume_command = sup.checkpoint_hook_command("resume", expected_agent)
    assert _hook_commands(settings, "PreCompact") == [save_command]
    assert _hook_commands(settings, "SessionStart") == [resume_command]

    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(b"{}"), encoding="utf-8"),
    )
    save_argv = shlex.split(save_command.split(" || ", 1)[0])[1:]
    assert _run(save_argv, tmp_path) == 0
    assert capsys.readouterr() == ("", "")
    saved = checkpoint.read_checkpoint(store, expected_agent)
    assert saved is not None
    assert saved["agent"] == expected_agent
    assert checkpoint.read_checkpoint(store, "retired") is None

    resume_argv = shlex.split(resume_command.split(" || ", 1)[0])[1:]
    assert _run(resume_argv, tmp_path) == 0
    envelope = json.loads(capsys.readouterr().out)
    context = envelope["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(f"Checkpoint reload for {expected_agent}:")


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
    text = raw.decode("ascii")
    assert "function Get-SupervisorEnvironmentCapture" not in text
    assert "$prepEnvironmentCapture" not in text
    assert "--launch-environment-stdin" not in text
    assert "--launch-agenttalk-python $AgenttalkPython" in text
    assert "--launch-src-on-pythonpath" in text
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

def _run_supervisor_config_reader(
    tmp_path: Path,
    environment: dict[str, str],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell Core is unavailable")
    config_path = tmp_path / "supervisor.json"
    config_path.write_text(
        "\ufeff" + json.dumps({
            "agents": {},
            "ephemeral_reviewers": {
                "allowed_profiles": {"unicode-profile": {"env": environment}},
            },
        }),
        encoding="utf-8",
    )
    output = tmp_path / "environment-names.json"
    reader = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Read-SupervisorConfig"):
        sup.PS_TEMPLATE.index("$cfg = Read-SupervisorConfig")
    ]
    script = tmp_path / "read-supervisor-config.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$ConfigPath = {_pslit(str(config_path))}",
            reader,
            "$config = Read-SupervisorConfig",
            "$profileEnv = $config.ephemeral_reviewers.allowed_profiles."
            "'unicode-profile'.env",
            "$payload = [pscustomobject]@{ "
            "names = @($profileEnv.PSObject.Properties.Name); "
            "sha256 = $script:SupervisorConfigSha256 }",
            "$payload | ConvertTo-Json | "
            f"Set-Content {_pslit(str(output))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )
    return subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    ), output


def test_supervisor_config_transport_preserves_unicode_environment_names(
    tmp_path: Path,
) -> None:
    environment = {
        "AGENTTALK_P1A_雪": "snow",
        "AGENTTALK_P1A_K": "ascii-k",
        "AGENTTALK_P1A_K": "kelvin",
        "AGENTTALK_ſELF": "long-s",
    }

    result, output = _run_supervisor_config_reader(tmp_path, environment)

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(output.read_text(encoding="utf-8-sig"))
    assert set(payload["names"]) == set(environment)
    assert payload["sha256"] == hashlib.sha256(
        (tmp_path / "supervisor.json").read_bytes()
    ).hexdigest()


def test_supervisor_config_transport_rejects_ambiguous_environment_names(
    tmp_path: Path,
) -> None:
    result, output = _run_supervisor_config_reader(
        tmp_path,
        {
            "AGENTTALK_P1A_Μ": "greek-mu",
            "AGENTTALK_P1A_µ": "micro",
        },
    )

    assert result.returncode != 0
    assert not output.exists()


def test_ephemeral_launcher_applies_environment_names_literally(
    tmp_path: Path,
) -> None:
    shells = tuple(
        shell for shell in _windows_powershell_hosts()
        if shell is not None
    )
    if not shells:
        return
    helpers = _exec_helpers(tmp_path)
    launchers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch($name"):
        sup.PS_TEMPLATE.index("# Console action log")
    ]
    output = tmp_path / "literal-environment.json"
    launch_argv = [
        "-m", "agenttalk", "--root", str(tmp_path), "wrap", "--for",
        "reviewer", "--cli", "codex", "--loop", "--one-shot",
        "--to-request", "lr-env", "--", "codex.exe",
    ]
    regular_admission = _accepted_launch_artifact(
        population="regular", agent="reviewer", cli="codex",
        windows_file="python.exe", argv=launch_argv, cwd=str(tmp_path),
        wrapped=True, module_args_from=0, nonce_insert_at=2,
        child_argv=["codex.exe"],
    )
    ephemeral_admission = {
        **regular_admission,
        "population": "ephemeral",
    }
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(str(tmp_path))}",
        "$AgenttalkPython = 'python.exe'",
        "$SrcOnPyPath = $true",
        "$snowName = 'AGENTTALK_P1A_' + [char]0x96EA",
        "$kelvinName = 'AGENTTALK_P1A_' + [char]0x212A",
        "$asciiKName = 'AGENTTALK_P1A_K'",
        "$longSName = 'AGENTTALK_' + [char]0x017F + 'ELF'",
        "$WrapperLogMaxBytes = 1024",
        "$WrapperLogSegments = 2",
        "$WrapperLogEnvKeys = @('AGENTTALK_WRAPPER_STDOUT_LOG',"
        "'AGENTTALK_WRAPPER_STDERR_LOG','AGENTTALK_WRAPPER_LOG_MAX_BYTES',"
        "'AGENTTALK_WRAPPER_LOG_SEGMENTS','AGENTTALK_WRAPPER_LOG_NONCE')",
        helpers,
        "function New-WrapperLogTargets($name, $nonce) {",
        "  return [pscustomobject]@{ stdout = 'stdout'; stderr = 'stderr'; "
        "generation_dir = 'generation' }",
        "}",
        "function Discard-PendingWrapperLogTargets($targets) {}",
        "function Proc-Start($id) { return '1' }",
        "function Quote-Arg([string]$arg) { return $arg }",
        "$script:inside = @()",
        "function Start-WrapperProcess($startArgs) {",
        "  $script:inside += [pscustomobject]@{",
        "    wildcard = [Environment]::GetEnvironmentVariable('AGENTTALK_P1A_*')",
        "    sibling_a = $env:AGENTTALK_P1A_A",
        "    sibling_b = $env:AGENTTALK_P1A_B",
        "    snow = [Environment]::GetEnvironmentVariable($snowName)",
        "    ascii_k = [Environment]::GetEnvironmentVariable($asciiKName)",
        "    kelvin = [Environment]::GetEnvironmentVariable($kelvinName)",
        "    long_s = [Environment]::GetEnvironmentVariable($longSName)",
        "    pythonpath = [Environment]::GetEnvironmentVariable('PYTHONPATH')",
        "    wrapper_nonce = [Environment]::GetEnvironmentVariable("
        "'AGENTTALK_WRAPPER_LOG_NONCE')",
        "    python = [Environment]::GetEnvironmentVariable('AGENTTALK_PYTHON')",
        "  }",
        "  return [pscustomobject]@{ Process = [pscustomobject]@{ Id = 42 }; Redirected = $false }",
        "}",
        launchers,
        f"$regularAdmission = ({_pslit(json.dumps(regular_admission))} | ConvertFrom-Json)",
        f"$ephemeralAdmission = ({_pslit(json.dumps(ephemeral_admission))} | ConvertFrom-Json)",
        "$env:AGENTTALK_P1A_A = 'old-a'",
        "$env:AGENTTALK_P1A_B = 'old-b'",
        "$env:AGENTTALK_PYTHON = 'controller-only'",
        "$env:PYTHONPATH = 'ORIGINAL'",
        "$profileEnv = [pscustomobject]@{}",
        "$profileEnv | Add-Member -NotePropertyName 'AGENTTALK_P1A_*' -NotePropertyValue 'literal-new'",
        "$profileEnv | Add-Member -NotePropertyName 'AGENTTALK_P1A_A' -NotePropertyValue 'new-a'",
        "$profileEnv | Add-Member -NotePropertyName $snowName -NotePropertyValue 'unicode'",
        "$profileEnv | Add-Member -NotePropertyName $asciiKName -NotePropertyValue 'ascii-k'",
        "$profileEnv | Add-Member -NotePropertyName $kelvinName -NotePropertyValue 'kelvin'",
        "$profileEnv | Add-Member -NotePropertyName $longSName -NotePropertyValue 'long-s'",
        "$profileEnv | Add-Member -NotePropertyName "
        "'agenttalk_wrapper_log_nonce' -NotePropertyValue 'configured-alias'",
        "$spec = [pscustomobject]@{",
        "  cli = 'codex'; cwd = $Root; env = $profileEnv;",
        "  window_style = 'Hidden'; window_style_warning = $null;",
        "  launch_admission = $ephemeralAdmission;",
        "  launch = [pscustomobject]@{",
        "    windows_file = 'python.exe';",
        "    windows_args = @('-m','agenttalk','--root',$Root,'wrap','--for',"
        "      'reviewer','--cli','codex','--loop','--one-shot','--to-request',"
        "      'lr-env','--','codex.exe')",
        "  }",
        "}",
        "$cfg = [pscustomobject]@{ agents = [pscustomobject]@{} }",
        "$agentConfig = [pscustomobject]@{",
        "  cli = 'codex'; wrapped = $true; cwd = $Root; env = $profileEnv;",
        "  launch = $spec.launch",
        "}",
        "$cfg.agents | Add-Member -NotePropertyName 'reviewer' -NotePropertyValue $agentConfig",
        "$plan = [pscustomobject]@{",
        "  launch_mode = 'wrap'; session_id = $null; session_args = @();",
        "  window_style = 'Hidden'; window_style_warning = $null;",
        "  launch_admission = $regularAdmission",
        "}",
        "$null = Launch 'reviewer' $plan $null",
        "$null = Launch-Spec 'reviewer' $spec $null",
        "[pscustomobject]@{",
        "  inside = $script:inside;",
        "  after_a = $env:AGENTTALK_P1A_A;",
        "  after_b = $env:AGENTTALK_P1A_B;",
        "  after_wildcard = [Environment]::GetEnvironmentVariable('AGENTTALK_P1A_*')",
        "  after_snow = [Environment]::GetEnvironmentVariable($snowName);",
        "  after_ascii_k = [Environment]::GetEnvironmentVariable($asciiKName);",
        "  after_kelvin = [Environment]::GetEnvironmentVariable($kelvinName);",
        "  after_long_s = [Environment]::GetEnvironmentVariable($longSName);",
        "} | ConvertTo-Json -Depth 4 | "
        f"Set-Content {_pslit(str(output))} -Encoding utf8",
    ])
    script = tmp_path / "literal-environment.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    for shell in shells:
        result = subprocess.run(
            [shell, "-NoProfile", "-File", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        payload = json.loads(output.read_text(encoding="utf-8-sig"))
        common = {
            "wildcard": "literal-new",
            "sibling_a": "new-a",
            "sibling_b": "old-b",
            "snow": "unicode",
            "ascii_k": "ascii-k",
            "kelvin": "kelvin",
            "long_s": "long-s",
        }
        inside = payload["inside"]
        assert len(inside) == 2
        for row in inside:
            for key, value in common.items():
                assert row[key] == value
            assert re.fullmatch(r"[0-9a-f]{32}", row["wrapper_nonce"])
            assert row["wrapper_nonce"] != "configured-alias"
        src_path = str(tmp_path / "src")
        assert all(row["pythonpath"] == f"{src_path};ORIGINAL" for row in inside)
        assert inside[0]["python"] == "controller-only"
        assert all(row["python"] is None for row in inside[1:])
        assert payload["after_a"] == "old-a"
        assert payload["after_b"] == "old-b"
        assert payload["after_wildcard"] is None
        assert payload["after_snow"] is None
        assert payload["after_ascii_k"] is None
        assert payload["after_kelvin"] is None
        assert payload["after_long_s"] is None

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


def _windows_powershell_hosts() -> tuple[str | None, ...]:
    if os.name != "nt":
        return (None,)
    hosts = tuple(
        dict.fromkeys(
            path
            for path in (
                shutil.which("pwsh"),
                shutil.which("powershell"),
            )
            if path is not None
        )
    )
    return hosts or (None,)


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
def test_generated_ps1_holds_malformed_config_poll_until_refresh_recovers(
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
    warning = "supervisor.json refresh failed; holding this poll"
    config_path = store.dir / "supervisor.json"
    valid_config = config_path.read_text(encoding="utf-8")
    try:
        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _state_has_agent(state_path, "lead")
            and _log_contains(log_path, "supervisor: lead:"),
        )
        time.sleep(0.25)
        _replace_text_when_unlocked(config_path, "{")
        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _log_occurrences(log_path, warning) >= 1,
        )
        _replace_text_when_unlocked(state_path, json.dumps({"agents": {}}))

        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _log_occurrences(log_path, warning) >= 3,
        )
        assert proc.poll() is None
        assert not _state_has_agent(state_path, "lead")

        _replace_text_when_unlocked(config_path, valid_config)
        _wait_for_live_supervisor(
            proc,
            log_path,
            lambda: _state_has_agent(state_path, "lead"),
        )
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

    public static Thread LockExistingUntilSignaled(
        string path, EventWaitHandle readySignal, EventWaitHandle releaseSignal)
    {
        Thread thread = new Thread(() =>
        {
            using (FileStream stream = new FileStream(
                path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                readySignal.Set();
                if (!releaseSignal.WaitOne(TimeSpan.FromSeconds(30)))
                {
                    throw new TimeoutException("state write lock release was not signaled");
                }
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
        "$readySignal = [Threading.ManualResetEvent]::new($false)",
        "$releaseSignal = [Threading.ManualResetEvent]::new($false)",
        "$locker = [AgenttalkStateWriteLock]::LockExistingUntilSignaled(",
        "  $StatePath, $readySignal, $releaseSignal)",
        "if (-not $readySignal.WaitOne(10000)) {",
        "  throw 'state write lock did not become ready'",
        "}",
        "try {",
        "  $next = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{ pid = 202 } } }",
        "  $results = @()",
        "  for ($poll = 0; $poll -lt 2; $poll++) {",
        "    $results += [bool](Save-StateForPoll $next)",
        "  }",
        "} finally {",
        "  $null = $releaseSignal.Set()",
        "}",
        "if (-not $locker.Join(10000)) {",
        "  throw 'state write lock did not release'",
        "}",
        "$readySignal.Dispose()",
        "$releaseSignal.Dispose()",
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
            "function Get-CimInstance {",
            "  [CmdletBinding()]",
            "  param(",
            "    [Parameter(Position=0)][string]$ClassName,",
            "    [string]$Filter",
            "  )",
            "  return @()",
            "}",
            "function Start-Process {",
            "  [CmdletBinding()]",
            "  param(",
            "    [Parameter(Mandatory=$true)][string]$FilePath,",
            "    [string[]]$ArgumentList,",
            "    [string]$WorkingDirectory,",
            "    [System.Diagnostics.ProcessWindowStyle]$WindowStyle,",
            "    [string]$RedirectStandardOutput,",
            "    [string]$RedirectStandardError,",
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
            "  if ($PSBoundParameters.ContainsKey('RedirectStandardOutput')) {",
            "    $startArgs.RedirectStandardOutput = $RedirectStandardOutput",
            "  }",
            "  if ($PSBoundParameters.ContainsKey('RedirectStandardError')) {",
            "    $startArgs.RedirectStandardError = $RedirectStandardError",
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
        assert lock_ready.exists(), first_output
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


def test_generated_proc_snapshot_emits_exact_live_filetime(tmp_path: Path) -> None:
    """The shipped snapshot binds CIM ancestry to one exact live start token."""
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    snapshot_path = tmp_path / "process-snapshot.json"
    out = tmp_path / "process-snapshot-result.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        helpers,
        f"$snapshotPath = {_pslit(str(snapshot_path))}",
        "$expectedTicks = ([datetimeoffset](Get-Process -Id $PID -ErrorAction Stop)"
        ".StartTime).UtcDateTime.ToFileTimeUtc()",
        "$script:roundedCreation = [datetimeoffset]([datetime]::FromFileTimeUtc("
        "$expectedTicks - ($expectedTicks % 10)))",
        "function Get-CimInstance { "
        "[pscustomobject]@{ ProcessId = [int]$PID; ParentProcessId = 0; "
        "Name = 'pwsh.exe'; CommandLine = $null; "
        "CreationDate = $script:roundedCreation } }",
        "if (-not (Get-ProcSnapshot $snapshotPath)) { throw 'snapshot unavailable' }",
        "$rows = @(Get-Content -Raw $snapshotPath | ConvertFrom-Json)",
        "$row = @($rows | Where-Object { [int]$_.pid -eq [int]$PID })[0]",
        "$expected = $expectedTicks.ToString("
        "[Globalization.CultureInfo]::InvariantCulture)",
        "@{ expected = $expected; actual = $row.start_filetime; "
        "has_start = [bool]$row.start_time } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "proc_snapshot_exact_filetime.ps1"
    hp.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(hp)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    assert payload["has_start"] is True
    assert payload["actual"] == payload["expected"]
    assert "Get-Process -Id ([int]$p.ProcessId)" not in helpers
    assert "foreach ($liveProc in @(Get-Process" in helpers


def test_stop_tree_rejects_rounded_start_collision_by_exact_filetime(
    tmp_path: Path,
) -> None:
    """An exact FILETIME mismatch must veto a rounded-start match."""
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    out = tmp_path / "stop-tree-exact-filetime.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        helpers,
        "$script:roundedStart = '2026-07-04T07:20:31.5767870+00:00'",
        "$script:liveTicks = [long]133960872315767870",
        "$script:stops = @(); $script:closed = @()",
        "function Proc-Start { param($procId) return $script:roundedStart }",
        "function Open-AgenttalkProcessHandle { param($procId); "
        "return ('handle-' + [string]$procId) }",
        "function Get-AgenttalkProcessHandleStartFiletime { param($handle); "
        "return $script:liveTicks.ToString("
        "[Globalization.CultureInfo]::InvariantCulture) }",
        "function Stop-AgenttalkProcessHandle { param($handle); "
        "$script:stops += $handle; return $true }",
        "function Close-AgenttalkProcessHandle { param($handle); "
        "$script:closed += $handle }",
        "$wrong = @(@{ pid = 4321; source = 'owned_process_tree'; "
        "start = $script:roundedStart; "
        "start_filetime = ([long]($script:liveTicks + 10)).ToString("
        "[Globalization.CultureInfo]::InvariantCulture) })",
        "Stop-Tree $wrong",
        "$mismatchStops = $script:stops.Count",
        "$right = @(@{ pid = 4321; source = 'owned_process_tree'; "
        "start = $script:roundedStart; "
        "start_filetime = $script:liveTicks.ToString("
        "[Globalization.CultureInfo]::InvariantCulture) })",
        "Stop-Tree $right",
        "@{ mismatch_stops = $mismatchStops; total_stops = $script:stops.Count; "
        "stopped_handle = $script:stops[-1]; "
        "closed_handles = $script:closed } | ConvertTo-Json -Depth 4 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "stop-tree-exact-filetime.ps1"
    hp.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(hp)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert json.loads(out.read_text(encoding="utf-8-sig")) == {
        "mismatch_stops": 0,
        "total_stops": 1,
        "stopped_handle": "handle-4321",
        "closed_handles": ["handle-4321", "handle-4321"],
    }


def test_stop_tree_verifies_and_terminates_through_one_native_handle(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    assert "WaitForSingleObject" in helpers
    assert "[uint32]0x101001" in helpers
    assert "$terminationWaitBudgetMilliseconds = 5000" in helpers
    out = tmp_path / "stop-tree-native-handle.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        helpers,
        "$script:ticks = '133960872315767870'",
        "$script:opened = @(); $script:verified = @(); "
        "$script:terminated = @(); $script:waited = @(); "
        "$script:closed = @(); $script:events = @(); "
        "$script:stopProcessCalls = 0",
        "function Proc-Start { param($procId) return "
        "'2026-07-04T07:20:31.5767870+00:00' }",
        "function Open-AgenttalkProcessHandle { param($procId); "
        "$h = 'handle-' + [string]$procId; $script:opened += $h; "
        "$script:events += ('open:' + $h); return $h }",
        "function Get-AgenttalkProcessHandleStartFiletime { param($handle); "
        "$script:verified += $handle; $script:events += ('verify:' + $handle); "
        "return $script:ticks }",
        "function Stop-AgenttalkProcessHandle { param($handle); "
        "$script:terminated += $handle; $script:events += ('terminate:' + $handle); "
        "return $true }",
        "function Wait-AgenttalkProcessHandleExit { "
        "param($handle, $timeoutMilliseconds); "
        "$script:waited += @{ handle = $handle; timeout = $timeoutMilliseconds }; "
        "$script:events += ('wait:' + $handle); return $true }",
        "function Close-AgenttalkProcessHandle { param($handle); "
        "$script:closed += $handle; $script:events += ('close:' + $handle) }",
        "function Get-Process { param($Id, $ErrorAction); "
        "return [pscustomobject]@{ Id = $Id; "
        "StartTime = [datetime]::FromFileTimeUtc([long]$script:ticks) } }",
        "function Stop-Process { param($Id, $InputObject, [switch]$Force, "
        "$ErrorAction); $script:stopProcessCalls++ }",
        "$target = @(@{ pid = 4321; source = 'owned_process_tree'; "
        "start = '2026-07-04T07:20:31.5767870+00:00'; "
        "start_filetime = $script:ticks })",
        "Stop-Tree $target",
        "@{ opened = $script:opened; verified = $script:verified; "
        "terminated = $script:terminated; waited = $script:waited; "
        "closed = $script:closed; events = $script:events; "
        "stop_process_calls = $script:stopProcessCalls } | "
        "ConvertTo-Json -Depth 4 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    hp = tmp_path / "stop-tree-native-handle.ps1"
    hp.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(hp)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    assert payload["opened"] == ["handle-4321"]
    assert payload["verified"] == ["handle-4321"]
    assert payload["terminated"] == ["handle-4321"]
    assert payload["waited"][0]["handle"] == "handle-4321"
    assert 0 < payload["waited"][0]["timeout"] <= 5000
    assert payload["closed"] == ["handle-4321"]
    assert payload["events"] == [
        "open:handle-4321",
        "verify:handle-4321",
        "terminate:handle-4321",
        "wait:handle-4321",
        "close:handle-4321",
    ]
    assert payload["stop_process_calls"] == 0


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


def test_ps_template_prepares_redirects_for_regular_and_ephemeral_launches() -> None:
    regular = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch($name"):
        sup.PS_TEMPLATE.index("function Launch-Spec")
    ]
    ephemeral = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch-Spec"):
        sup.PS_TEMPLATE.index("# Console action log")
    ]

    for block, configured_env in (
        (regular, "if ($a.env)"),
        (ephemeral, "if ($spec.env)"),
    ):
        assert "New-WrapperLogTargets" in block
        assert "RedirectStandardOutput" in block
        assert "RedirectStandardError" in block
        assert "AGENTTALK_WRAPPER_LOG_NONCE" in block
        assert "Start-WrapperProcess $startArgs" in block
        assert block.index(configured_env) < block.index(
            "AGENTTALK_WRAPPER_LOG_NONCE"
        )
        assert block.index(configured_env) < block.index(
            "CompareStringOrdinal("
        )
        assert block.index("CompareStringOrdinal(") < block.index(
            "$applied['AGENTTALK_WRAPPER_LOG_NONCE']"
        )
        assert "Add-SupervisorLaunchNonceAt" in block
        assert "[bool]$admission.wrapper_dispatch" in block
    assert "Test-AgenttalkWrapInvocation" not in sup.PS_TEMPLATE
    assert "$plan.launch_mode -eq 'wrap'" in regular


def test_ps_wrapper_log_targets_preserve_output_and_prune_old_generations(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    log_root = tmp_path / "wrapper logs"
    result_path = tmp_path / "wrapper-log-targets.json"
    code = (
        "import sys; "
        "print('ACTUAL-STDOUT', flush=True); "
        "print('ACTUAL-STDERR', file=sys.stderr, flush=True)"
    )
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(log_root))}",
        f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
    ]
    for index in range(sup.WRAPPER_LOG_GENERATIONS + 2):
        rows.extend([
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            (
                "$preservedStdout = $target.stdout"
                if index == 2
                else "$null = $target.stdout"
            ),
            "$lastStdout = $target.stdout",
            "$lastStderr = $target.stderr",
            "$startArgs = @{ FilePath = "
            f"{_pslit(sys.executable)}; ArgumentList = "
            f"{_pslit(subprocess.list2cmdline(['-X', 'utf8', '-c', code]))}; "
            "WorkingDirectory = "
            f"{_pslit(str(tmp_path))}; PassThru = $true; "
            "RedirectStandardOutput = $target.stdout; "
            "RedirectStandardError = $target.stderr }",
            "$p = (Start-WrapperProcess $startArgs).Process",
            "$p.WaitForExit()",
            "Confirm-WrapperLogTarget $target",
        ])
    rows.extend([
        "$dirs = @(Get-ChildItem -LiteralPath "
        f"(Join-Path $WrapperLogRoot {_pslit(_wrapper_log_agent_dir('worker'))}) "
        "-Directory | Sort-Object Name)",
        "@{ count = $dirs.Count; "
        "stdout = [IO.File]::ReadAllText($lastStdout); "
        "stderr = [IO.File]::ReadAllText($lastStderr); "
        "preserved = [IO.File]::ReadAllText($preservedStdout) } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ])
    script = tmp_path / "wrapper-log-targets.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    # Round 23: retention now prunes to the newest ALREADY-CONFIRMED
    # WRAPPER_LOG_GENERATIONS, not (WRAPPER_LOG_GENERATIONS - 1) reserving
    # a slot for the one just created (see Invoke-WrapperLogRetentionPrune's
    # own comment). Round 24 CORRECTION: this was originally described here
    # as "one more than quota for one cycle, self-corrects on the next
    # launch's own prune" - the connector proved by execution that this is
    # wrong: it is STEADY STATE, not a transient, for as long as launches
    # keep confirming - see
    # test_ps_wrapper_log_retention_settles_one_over_quota_not_at_quota for
    # the dedicated proof (10 cycles, quota+1 from cycle 5 onward, never
    # quota again). Kept as a loose bound here since THIS test's own point
    # is the byte-level tee behavior, not the retention count.
    assert sup.WRAPPER_LOG_GENERATIONS <= payload["count"] <= sup.WRAPPER_LOG_GENERATIONS + 1
    assert "ACTUAL-STDOUT" in payload["stdout"]
    assert "ACTUAL-STDERR" in payload["stderr"]
    assert "ACTUAL-STDOUT" in payload["preserved"]


def test_ps_wrapper_log_retention_settles_one_over_quota_not_at_quota(
    tmp_path: Path,
) -> None:
    """Round 24 connector finding, settled by EXECUTION rather than
    re-reading: my own round-23 report claimed an all-confirming string
    sits at quota+1 for ONE cycle, self-correcting on the next launch's own
    prune. That claim is wrong. Invoke-WrapperLogRetentionPrune runs BEFORE
    the generation it just created is confirmed, so it can only ever trim
    EXISTING (already-resolved) generations down to the quota - the one
    just created then becomes a (quota+1)-th confirmed generation once it
    succeeds, and nothing prunes again until the NEXT launch repeats
    exactly the same pattern. That is a permanent steady state for as long
    as launches keep confirming, not a one-off overshoot: this drives 10
    consecutive create-then-confirm cycles against a quota of 4 and asserts
    the trajectory outright (not just a bound) - count climbs 1,2,3,4 while
    still under quota, then sits at 5 from the point it first exceeds quota
    through the last cycle, never dropping back to 4."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    log_root = tmp_path / "wrapper logs"
    result_path = tmp_path / "settle.json"
    quota = 4
    cycles = 10
    rows = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(log_root))}",
        f"$WrapperLogGenerations = {quota}",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        "$counts = @()",
    ]
    for _ in range(cycles):
        rows.extend([
            "$target = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
            "Confirm-WrapperLogTarget $target",
            "$dirs = @(Get-ChildItem -LiteralPath "
            f"(Join-Path $WrapperLogRoot {_pslit(_wrapper_log_agent_dir('worker'))}) "
            "-Directory)",
            "$counts += $dirs.Count",
        ])
    rows.append(
        "@{ counts = $counts } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8"
    )
    script = tmp_path / "settle-retention.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    counts = json.loads(result_path.read_text(encoding="utf-8-sig"))["counts"]
    # Climbing to quota while under it, then PERMANENTLY at quota+1 - never
    # back down to quota once it first overshoots.
    assert counts == [1, 2, 3, 4, 5, 5, 5, 5, 5, 5]


def test_ps_wrapper_log_prune_survives_backward_clock_correction(
    tmp_path: Path,
) -> None:
    """Finding 3 (PR 98 connector re-review, head 2297ce10): pruning must not
    trust the wall-clock generation name as the sole age key. G1 is renamed to
    a deceptive far-future name (simulating a generation created while the
    clock was skewed ahead) but keeps its true (oldest) launch sequence; G2 is
    created afterwards with a real, "older-looking" name but is genuinely the
    newer launch. Once a third generation forces a prune, G2 - not the
    deceptively-named G1 - must be the one retained."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    log_root = tmp_path / "wrapper logs"
    result_path = tmp_path / "clock-skew-retention.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(log_root))}",
        "$WrapperLogGenerations = 2",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        "$g1 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $g1",
        "$g1Dir = $g1.generation_dir",
        "$g1Parent = Split-Path $g1Dir -Parent",
        "$futureName = '22991231T235959000Z-' + ('f' * 32)",
        "$g1Future = Join-Path $g1Parent $futureName",
        "Move-Item -LiteralPath $g1Dir -Destination $g1Future",
        "$g2 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $g2",
        "$g2Dir = $g2.generation_dir",
        "$g3 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $g3",
        "$g3Dir = $g3.generation_dir",
        # Round 23: retention now keeps the newest ALREADY-CONFIRMED quota
        # (2), not (quota - 1) reserving a slot for the one about to be
        # created - so g1/g2/g3 all still exist right after g3's own
        # confirm (3 confirmed, one over quota, corrected on the NEXT
        # launch's own prune). A 4th launch (never confirmed - only its
        # CREATION, which runs the prune, matters here) is what actually
        # forces the eviction this test exists to prove: sort_key must
        # still rank g1 oldest by its true launch sequence despite its
        # deceptive future name.
        "$g4 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "@{ future_exists = (Test-Path -LiteralPath $g1Future); "
        "g2_exists = (Test-Path -LiteralPath $g2Dir); "
        "g3_exists = (Test-Path -LiteralPath $g3Dir) } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "clock-skew-retention.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload == {
        "future_exists": False,
        "g2_exists": True,
        "g3_exists": True,
    }


def test_ps_wrapper_log_sequence_survives_failover_to_fallback_root(
    tmp_path: Path,
) -> None:
    """Finding A (PR 98 connector re-review, head 4323e20): the previous
    round's launch-sequence fix computed its counter per-root, which is not a
    global ordering key either - the exact defect class it was meant to fix,
    surviving in a narrower window. Primary is rejected (a reparse point -
    the SAME privilege-independent mechanism
    test_ps_wrapper_log_root_reparse_is_rejected_before_traversal already
    proves works on CI; an ACL deny does not, since the CI runner account
    owns the directory) while fallback is built up to sequence 3, then
    primary becomes available again. The next launch lands on primary but
    must still record sequence 4, not restart at 1 for a root it has never
    used before.

    The precondition (primary genuinely rejected during build-up) is
    asserted explicitly below, not assumed - a silently-unmet precondition
    here would exercise no failover at all and still report a false pass."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    victim = tmp_path / "victim"
    victim.mkdir()
    primary = tmp_path / "primary-junction"
    fallback = tmp_path / "fallback"
    result_path = tmp_path / "sequence-failover.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$null = New-Item -ItemType Junction -Path {_pslit(str(primary))} "
        f"-Target {_pslit(str(victim))}",
        f"$WrapperLogRoot = {_pslit(str(primary))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
        "$WrapperLogGenerations = 10",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        # Primary is a reparse point and must be rejected for all three of
        # these - the precondition this test depends on.
        "$preBuildup = @()",
        "1..3 | ForEach-Object {",
        "  $t = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "  Confirm-WrapperLogTarget $t",
        "  $preBuildup += [string]$t.generation_dir",
        "}",
        # Primary becomes available again - remove the junction (the target
        # directory's own contents are untouched; only the redirect goes).
        f"Remove-Item -LiteralPath {_pslit(str(primary))} -Force",
        "$t2 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $t2",
        "$seqFile = Join-Path $t2.generation_dir '.sequence'",
        "@{ preBuildup = @($preBuildup); selected = [string]$t2.generation_dir; "
        "sequence = [IO.File]::ReadAllText($seqFile).Trim() } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "sequence-failover.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    pre_buildup = payload["preBuildup"]
    assert len(pre_buildup) == 3, f"expected 3 build-up launches, got {pre_buildup!r}"
    for selected in pre_buildup:
        assert Path(selected).is_relative_to(fallback), (
            "precondition unmet: primary's reparse point did not force this "
            f"build-up launch onto fallback - {selected!r} is not under {fallback}"
        )
    assert Path(payload["selected"]).is_relative_to(primary)
    assert payload["sequence"] == "4"


def test_ps_wrapper_log_attempt_cleaned_up_when_pending_marker_write_fails(
    tmp_path: Path,
) -> None:
    """Finding C (PR 98 connector re-review, head ee177af): a failure writing
    the .pending marker AFTER the generation directory was already created
    used to leave that directory behind with neither a .pending nor a
    .committed marker - retention preserves every markerless directory
    forever (it cannot tell an orphan from a genuinely interrupted launch),
    so a persistent write failure would accumulate directories outside the
    quota with no way to ever clean them up.

    New-WrapperLogPendingMarker is redefined here (a privilege-independent,
    deterministic fault injection - no ACL trick that CI's runner account
    could bypass) to simulate exactly that failure mode."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    root = tmp_path / "logs"
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "pending-failure.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(root))}",
        "$WrapperLogGenerations = 10",
        helpers,
        "function New-WrapperLogPendingMarker([string]$attempt) { "
        "throw 'simulated .pending write failure' }",
        "$target = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        f"$agentDir = Join-Path {_pslit(str(root))} {_pslit(agent_leaf)}",
        "$leftover = if (Test-Path -LiteralPath $agentDir) { "
        "@(Get-ChildItem -LiteralPath $agentDir -Directory).Count } else { 0 }",
        "@{ targetIsNull = ($null -eq $target); leftover = $leftover } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "pending-failure.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["targetIsNull"] is True
    assert payload["leftover"] == 0, (
        "an orphaned, markerless generation directory was left behind after "
        "the .pending write failed"
    )


def test_ps_wrapper_log_prune_refuses_when_root_scan_is_uncertain(
    tmp_path: Path,
) -> None:
    """Finding B (PR 98 connector re-review, head 6495534): the global launch
    sequence, round 3 on the same ordering defect. A root that EXISTS but
    cannot be scanned (unreadable, disconnected, rejected) might be hiding a
    higher true sequence than the reachable roots show - silently treating
    it as contributing nothing to the max reintroduces the exact
    not-a-global-ordering-key defect this whole mechanism exists to close,
    just relocated to the offline-root window.

    Structural answer chosen: option (ii) from the lead's brief - retention
    REFUSES TO PRUNE for a launch whose own sequence could not be
    established as reliable, rather than pruning on an ordering it knows
    might be wrong. It resumes normally once every root is reachable again.

    Primary's agent directory is turned into a reparse point (the same
    privilege-independent mechanism proven on CI elsewhere in this suite,
    applied to the agent dir rather than the root) so it EXISTS but cannot
    be scanned - the real generations underneath are untouched and restored
    afterward to verify none were pruned."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "uncertain-prune.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(primary))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
        "$WrapperLogGenerations = 2",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        # Build primary's history - quota=2 legitimately prunes down to the
        # 2 newest during this normal build-up. Round 23: retention keeps
        # the newest ALREADY-CONFIRMED quota, not (quota - 1) reserving a
        # slot for the one about to be created - 3 confirms in a row can
        # leave one over quota until the NEXT launch's own prune catches
        # up, so one more CREATE (never confirmed - only its creation,
        # which runs the prune, matters) settles primary back to exactly 2
        # before this test's own precondition check.
        "1..3 | ForEach-Object {",
        "  $t = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "  Confirm-WrapperLogTarget $t",
        "}",
        "$settle = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Discard-PendingWrapperLogTargets $settle",
        f"$primaryAgentDir = Join-Path {_pslit(str(primary))} {_pslit(agent_leaf)}",
        "$before = @(Get-ChildItem -LiteralPath $primaryAgentDir -Directory).Count",
        # Primary's agent dir EXISTS but cannot be scanned - the real
        # generations move aside untouched, reachable again once restored.
        "Rename-Item -LiteralPath $primaryAgentDir -NewName 'worker-real'",
        f"$victimReal = Join-Path {_pslit(str(primary))} 'worker-real'",
        "$null = New-Item -ItemType Junction -Path $primaryAgentDir -Target $victimReal",
        "$t2 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $t2",
        "Remove-Item -LiteralPath $primaryAgentDir -Force",
        f"Rename-Item -LiteralPath $victimReal -NewName {_pslit(agent_leaf)}",
        "$after = @(Get-ChildItem -LiteralPath $primaryAgentDir -Directory).Count",
        "@{ before = $before; after = $after; "
        "uncertain = [bool]$t2.sequence_uncertain; "
        "selected = [string]$t2.generation_dir } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "uncertain-prune.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["before"] == 2, "quota=2 did not settle primary to 2 during build-up"
    assert payload["uncertain"] is True
    assert Path(payload["selected"]).is_relative_to(fallback)
    assert payload["after"] == 2, (
        "primary lost a generation to pruning during a launch whose ordering "
        "was known to be unreliable"
    )


def test_ps_wrapper_log_sequence_not_uncertain_when_root_has_no_agent_dir(
    tmp_path: Path,
) -> None:
    """I3 (PR 98 cold review): a configured root the operator set up but this
    agent has simply never used - no per-agent directory there yet - is
    absent, not unscannable. There is nothing on that root to miss.
    Flagging it uncertain anyway (testing the ROOT's existence instead of
    the AGENT DIR's) turns the previous round's "refuse this cycle" into
    "never prune again": the fallback root exists (an operator configured
    it) for the ENTIRE lifetime of an agent that never happens to use it,
    so every single launch would be marked uncertain forever, and quota
    would never be enforced at all."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    fallback.mkdir()  # the root exists; this agent has never used it
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "absent-agent-dir.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(primary))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
        "$WrapperLogGenerations = 2",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        "$results = @()",
        "1..3 | ForEach-Object {",
        "  $t = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "  Confirm-WrapperLogTarget $t",
        "  $results += [pscustomobject]@{ uncertain = [bool]$t.sequence_uncertain }",
        "}",
        # Round 23: 3 confirms in a row against quota=2 can leave one over
        # quota until the NEXT launch's own prune catches up (retention no
        # longer reserves a slot for the one about to be created - see
        # Invoke-WrapperLogRetentionPrune) - one more settle-down launch,
        # never confirmed, before checking dirCount below.
        "$settle = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Discard-PendingWrapperLogTargets $settle",
        f"$primaryAgentDir = Join-Path {_pslit(str(primary))} {_pslit(agent_leaf)}",
        "$dirCount = @(Get-ChildItem -LiteralPath $primaryAgentDir -Directory).Count",
        "@{ results = @($results); dirCount = $dirCount } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "absent-agent-dir.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    results = payload["results"]
    assert len(results) == 3
    for row in results:
        assert row["uncertain"] is False, (
            "a fallback root this agent has never used was wrongly flagged "
            "uncertain just because the root itself exists"
        )
    assert payload["dirCount"] == 2, (
        "retention never pruned - the absent-agent-dir false uncertainty "
        "turned 'refuse this cycle' into 'never prune again'"
    )


def test_ps_wrapper_log_prune_bound_recovers_from_persistent_uncertainty(
    tmp_path: Path,
) -> None:
    """I3's bound: refusing to prune under uncertainty is honest, but it must
    not be able to accumulate forever if a root stays genuinely unscannable
    for its entire lifetime (a real, if rare, operator misconfiguration -
    not the transient case the mechanism is meant for). Primary is a
    reparse point from before the FIRST launch and stays one throughout;
    every launch lands on fallback and is marked uncertain every time.
    Pruning must still resume once fallback's own count is far enough past
    quota - imperfect ordering is better than unbounded accumulation."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    victim = tmp_path / "victim"
    victim.mkdir()
    primary = tmp_path / "primary"
    primary.mkdir()
    fallback = tmp_path / "fallback"
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "bounded-uncertainty.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(primary))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
        "$WrapperLogGenerations = 2",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        f"$primaryAgentDir = Join-Path {_pslit(str(primary))} {_pslit(agent_leaf)}",
        "$null = New-Item -ItemType Junction -Path $primaryAgentDir -Target "
        f"{_pslit(str(victim))}",
        "$uncertainFlags = @()",
        "1..8 | ForEach-Object {",
        "  $t = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "  Confirm-WrapperLogTarget $t",
        "  $uncertainFlags += [bool]$t.sequence_uncertain",
        "}",
        f"$fallbackAgentDir = Join-Path {_pslit(str(fallback))} {_pslit(agent_leaf)}",
        "$finalCount = @(Get-ChildItem -LiteralPath $fallbackAgentDir -Directory).Count",
        "@{ uncertainFlags = @($uncertainFlags); finalCount = $finalCount } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "bounded-uncertainty.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert all(payload["uncertainFlags"]), (
        "primary was expected to stay unscannable for every one of the 8 "
        "launches - the bound test is meaningless if it ever became certain"
    )
    assert payload["finalCount"] < 8, (
        "8 launches with permanent uncertainty left all 8 generations on "
        "disk - the refuse-to-prune escape valve has no bound"
    )
    assert payload["finalCount"] <= 6, (
        f"final count {payload['finalCount']} exceeds the intended bound"
    )


def test_ps_wrapper_log_sequence_write_failure_marks_uncertain_and_defers_prune(
    tmp_path: Path,
) -> None:
    """Round 12 connector finding, #139-family: a transient failure writing
    a generation's .sequence file left sequence_uncertain reflecting only
    the earlier root-SCAN result, not the write itself - the generation
    still launched and committed with no .sequence file at all.
    Invoke-WrapperLogRetentionPrune's retention sort ranks a missing-.sequence
    generation BELOW every sequence-bearing one regardless of actual
    launch order ('0-name' sorts before '1-sequence'), so a transient write
    failure on the NEWEST generation could make retention prune it while
    keeping a genuinely OLDER one.

    Write-WrapperLogSequenceFile is overridden (a plain PowerShell function
    redefinition, not an OS-level fault injection - the write call is
    factored into its own named helper for exactly this reason) to fail on
    the second launch only. With quota=1, retention would prune down to 1
    survivor if it trusted the write - it must instead defer pruning this
    cycle because sequence_uncertain is now true, so both generations
    survive."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "sequence-write-failure.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(primary))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
        "$WrapperLogGenerations = 1",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        "$t1 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $t1",
        "function Write-WrapperLogSequenceFile([string]$path, [long]$value) {",
        "  throw 'simulated transient .sequence write failure'",
        "}",
        "$t2 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $t2",
        f"$agentDir = Join-Path {_pslit(str(primary))} {_pslit(agent_leaf)}",
        "$survivors = @(Get-ChildItem -LiteralPath $agentDir -Directory | "
        "  ForEach-Object { $_.Name })",
        "$t2SeqFile = Join-Path ([string]$t2.generation_dir) '.sequence'",
        "@{ uncertain1 = [bool]$t1.sequence_uncertain; "
        "uncertain2 = [bool]$t2.sequence_uncertain; "
        "t2HasSequenceFile = (Test-Path -LiteralPath $t2SeqFile); "
        "survivorCount = $survivors.Count; "
        "g1Name = (Split-Path ([string]$t1.generation_dir) -Leaf); "
        "g2Name = (Split-Path ([string]$t2.generation_dir) -Leaf); "
        "survivors = $survivors } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "sequence-write-failure.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["uncertain1"] is False
    assert payload["t2HasSequenceFile"] is False
    assert payload["uncertain2"] is True, (
        "a transient .sequence WRITE failure must propagate into "
        "sequence_uncertain, not just a failed root scan"
    )
    assert payload["survivorCount"] == 2, (
        "retention pruned down to quota=1 despite the write failure - "
        "it could have evicted the newer generation and kept the older one"
    )
    assert payload["g1Name"] in payload["survivors"]
    assert payload["g2Name"] in payload["survivors"]


def test_ps_wrapper_log_sequence_uncertainty_persists_to_the_next_launch(
    tmp_path: Path,
) -> None:
    """Round 13 connector finding: round 12's sequence_uncertain does not
    outlive its own launch. It exists only in the return value of the ONE
    New-WrapperLogTargets call that hit the write failure - nothing
    persists it to disk - so once that generation commits without a
    .sequence file, Get-NextWrapperLogSequence's scan on the NEXT launch
    silently skips it (missing .sequence -> continue, no uncertain flag)
    and treats ordering as reliable again. Concretely: G1/G2/G3 commit
    with real sequence numbers, G4's write fails and commits without one
    (uncertain on G4's own launch, pruning deferred that cycle per round
    12), then G5 launches normally with a REAL sequence write. Before this
    fix G5 is NOT uncertain (its own write succeeded and the scan does not
    look back), so retention proceeds and ranks G4 - chronologically
    between G3 and G5 - below every sequence-bearing entry: G1 (the
    OLDEST) and G4 (NEWER than G2/G3) both get pruned while G2/G3 survive.
    After this fix a missing/unreadable .sequence file on a COMMITTED
    generation propagates into uncertain on every SUBSEQUENT scan until
    that generation is gone, so G5 is uncertain too and pruning keeps
    deferring - G4 survives."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "sequence-uncertainty-persists.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$WrapperLogRoot = {_pslit(str(primary))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
        "$WrapperLogGenerations = 3",
        helpers,
        _CONFIRM_WRAPPER_LOG_TARGET_PS,
        "1..3 | ForEach-Object {",
        "  $t = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "  Confirm-WrapperLogTarget $t",
        "}",
        "function Write-WrapperLogSequenceFile([string]$path, [long]$value) {",
        "  throw 'simulated transient .sequence write failure'",
        "}",
        "$t4 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $t4",
        # Redefine back to the real body (a later same-name function
        # definition overwrites the earlier one in place in PowerShell's
        # function: drive - Remove-Item would delete it entirely, leaving
        # nothing callable, not "restore the original").
        "function Write-WrapperLogSequenceFile([string]$path, [long]$value) {",
        "  [IO.File]::WriteAllText($path, [string]$value, (New-Object Text.UTF8Encoding($false)))",
        "}",
        "$t5 = New-WrapperLogTargets 'worker' ([Guid]::NewGuid().ToString('N'))",
        "Confirm-WrapperLogTarget $t5",
        f"$agentDir = Join-Path {_pslit(str(primary))} {_pslit(agent_leaf)}",
        "$survivors = @(Get-ChildItem -LiteralPath $agentDir -Directory | "
        "  ForEach-Object { $_.Name })",
        "$g4Name = (Split-Path ([string]$t4.generation_dir) -Leaf)",
        "@{ uncertain4 = [bool]$t4.sequence_uncertain; "
        "uncertain5 = [bool]$t5.sequence_uncertain; "
        "survivorCount = $survivors.Count; "
        "g4Name = $g4Name; "
        "survivors = $survivors } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "sequence-uncertainty-persists.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["uncertain4"] is True
    assert payload["uncertain5"] is True, (
        "a missing .sequence file on a committed generation must make the "
        "NEXT launch's scan uncertain too, not just the launch that hit "
        "the write failure"
    )
    assert payload["g4Name"] in payload["survivors"], (
        "G4 has no .sequence file but is chronologically newer than G2/G3 "
        "- retention must not prune it while an older sequence-bearing "
        "generation survives"
    )
    assert payload["survivorCount"] == 5, (
        "pruning should still be deferred at G5's commit since ordering "
        "remains unreliable while G4's missing sequence is unresolved"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows platform detection")
def test_ps_wrapper_log_security_does_not_depend_on_ambient_os_marker(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    out = tmp_path / "platform-detection.json"
    script = tmp_path / "platform-detection.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            "$env:OS = $null",
            "$env:PATH = Join-Path $env:SystemRoot 'System32'",
            f"$WrapperLogRoot = {_pslit(str(tmp_path / 'primary'))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
            helpers,
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "@{ created = ($null -ne $target); "
            "stdout = (Test-Path -LiteralPath $target.stdout); "
            "stderr = (Test-Path -LiteralPath $target.stderr) } | "
            "ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert json.loads(out.read_text(encoding="utf-8-sig")) == {
        "created": True,
        "stdout": False,
        "stderr": False,
    }


def test_ps_wrapper_log_cleanup_failure_uses_new_generation_and_still_launches(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    log_root = tmp_path / "primary"
    agent_root = log_root / _wrapper_log_agent_dir("worker")
    old = agent_root / "20260730T010203004Z-0123456789abcdef0123456789abcdef"
    old.mkdir(parents=True)
    (old / "stdout.log").write_text("dead-wrapper-evidence", encoding="utf-8")
    (old / ".committed").write_text("", encoding="utf-8")
    # Round 23: retention keeps the newest quota (1 here) ALREADY-CONFIRMED
    # generations, not (quota - 1) - with only `old` pre-existing and
    # quota=1, eviction would never even be ATTEMPTED (1 owned <= keep 1),
    # so the locked-Remove-Item failure this test exists to prove tolerant
    # of would never actually fire. A second, newer committed generation
    # makes `old` the genuine oldest that eviction targets and fails to
    # remove.
    newer = agent_root / "20260730T010203005Z-0123456789abcdef0123456789abcdef"
    newer.mkdir(parents=True)
    (newer / "stdout.log").write_text("newer-wrapper-evidence", encoding="utf-8")
    (newer / ".committed").write_text("", encoding="utf-8")
    result_path = tmp_path / "cleanup-failure.json"
    code = "print('LAUNCH-SURVIVED', flush=True)"
    script = tmp_path / "cleanup-failure.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            "$env:OS = $null",
            f"$WrapperLogRoot = {_pslit(str(log_root))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            "$WrapperLogGenerations = 1",
            "function Remove-Item { throw 'simulated locked generation' }",
            helpers,
            _CONFIRM_WRAPPER_LOG_TARGET_PS,
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "$startArgs = @{ FilePath = "
            f"{_pslit(sys.executable)}; ArgumentList = "
            f"{_pslit(subprocess.list2cmdline(['-X', 'utf8', '-c', code]))}; "
            "WorkingDirectory = "
            f"{_pslit(str(tmp_path))}; PassThru = $true; "
            "RedirectStandardOutput = $target.stdout; "
            "RedirectStandardError = $target.stderr }",
            "$p = (Start-WrapperProcess $startArgs).Process",
            "$p.WaitForExit()",
            "Confirm-WrapperLogTarget $target",
            "@{ pid = $p.Id; output = [IO.File]::ReadAllText($target.stdout); "
            f"old = [IO.File]::ReadAllText({_pslit(str(old / 'stdout.log'))}) }} | "
            f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["pid"] > 0
    assert "LAUNCH-SURVIVED" in payload["output"]
    assert payload["old"] == "dead-wrapper-evidence"
    assert "simulated locked generation" in (result.stdout + result.stderr)


def test_ps_wrapper_log_retention_is_global_across_primary_and_fallback(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    agent_leaf = _wrapper_log_agent_dir("worker")
    names = [
        "20260729T010203001Z-00000000000000000000000000000001",
        "20260729T010203002Z-00000000000000000000000000000002",
        "20260729T010203003Z-00000000000000000000000000000003",
        "20260729T010203004Z-00000000000000000000000000000004",
        "20260729T010203005Z-00000000000000000000000000000005",
    ]
    for index, name in enumerate(names):
        root = primary if index < 2 else fallback
        generation = root / agent_leaf / name
        generation.mkdir(parents=True)
        (generation / "stdout.log").write_text(name, encoding="utf-8")
        (generation / ".committed").write_text("", encoding="utf-8")
    out = tmp_path / "global-retention.json"
    script = tmp_path / "global-retention.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$WrapperLogRoot = {_pslit(str(primary))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
            f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
            helpers,
            _CONFIRM_WRAPPER_LOG_TARGET_PS,
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "Confirm-WrapperLogTarget $target",
            "$dirs = @(",
            f"  Get-ChildItem -LiteralPath {_pslit(str(primary / agent_leaf))} "
            "-Directory -ErrorAction SilentlyContinue",
            f"  Get-ChildItem -LiteralPath {_pslit(str(fallback / agent_leaf))} "
            "-Directory -ErrorAction SilentlyContinue",
            ")",
            "@{ count = $dirs.Count; selected = $target.generation_dir; "
            f"oldest_exists = (Test-Path {_pslit(str(primary / agent_leaf / names[0]))}) "
            "} | ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    # Round 23: retention keeps the newest WRAPPER_LOG_GENERATIONS ALREADY-
    # COMMITTED generations, then this test's own final confirm adds one
    # more - see Invoke-WrapperLogRetentionPrune's own comment for why that
    # is the honest, bounded (never unbounded) cost of not reserving a slot
    # for an unconfirmed guess.
    assert payload["count"] == sup.WRAPPER_LOG_GENERATIONS + 1
    assert Path(payload["selected"]).is_relative_to(primary)
    assert payload["oldest_exists"] is False


def test_ps_failed_launch_generations_never_evict_prior_evidence(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    agent_root = primary / _wrapper_log_agent_dir("worker")
    old = agent_root / "20260729T010203001Z-00000000000000000000000000000001"
    old.mkdir(parents=True)
    (old / "stdout.log").write_text("dead-wrapper-evidence", encoding="utf-8")
    (old / ".committed").write_text("", encoding="utf-8")
    out = tmp_path / "failed-launch-retention.json"
    script = tmp_path / "failed-launch-retention.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$WrapperLogRoot = {_pslit(str(primary))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            "$WrapperLogGenerations = 1",
            helpers,
            "1..6 | ForEach-Object {",
            "  $target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "  Discard-PendingWrapperLogTargets $target",
            "}",
            "$dirs = @(Get-ChildItem -LiteralPath "
            f"{_pslit(str(agent_root))} -Directory)",
            "@{ count = $dirs.Count; "
            f"evidence = [IO.File]::ReadAllText({_pslit(str(old / 'stdout.log'))}) "
            "} | ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    assert payload == {"count": 1, "evidence": "dead-wrapper-evidence"}


@pytest.mark.skipif(os.name != "nt", reason="Windows locked-file retention")
def test_ps_locked_uncommitted_generation_never_displaces_real_evidence(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    agent_root = primary / _wrapper_log_agent_dir("worker")
    old = agent_root / "20260729T010203001Z-00000000000000000000000000000001"
    failed = agent_root / "20260729T010203999Z-00000000000000000000000000000002"
    old.mkdir(parents=True)
    failed.mkdir()
    (old / "stdout.log").write_text("dead-wrapper-evidence", encoding="utf-8")
    (old / ".committed").write_text("", encoding="utf-8")
    (failed / ".pending").write_text("", encoding="utf-8")
    out = tmp_path / "locked-pending.json"
    script = tmp_path / "locked-pending.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$WrapperLogRoot = {_pslit(str(primary))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            "$WrapperLogGenerations = 2",
            helpers,
            _CONFIRM_WRAPPER_LOG_TARGET_PS,
            "$lock = [IO.File]::Open("
            f"{_pslit(str(failed / '.pending'))}, [IO.FileMode]::Open, "
            "[IO.FileAccess]::Read, [IO.FileShare]::None)",
            "try {",
            "  $target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "  Confirm-WrapperLogTarget $target",
            "} finally { $lock.Dispose() }",
            "$dirs = @(Get-ChildItem -LiteralPath "
            f"{_pslit(str(agent_root))} -Directory)",
            "@{ count = $dirs.Count; "
            f"old_exists = (Test-Path {_pslit(str(old))}); "
            f"pending_exists = (Test-Path {_pslit(str(failed))}) "
            "} | ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    assert payload == {
        "count": 3,
        "old_exists": True,
        "pending_exists": True,
    }
    assert "preserving unresolved wrapper log generation" in (
        result.stdout + result.stderr
    )


def test_ps_markerless_failed_generation_never_displaces_real_evidence(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    primary = tmp_path / "primary"
    agent_root = primary / _wrapper_log_agent_dir("worker")
    old = agent_root / "20260729T010203001Z-00000000000000000000000000000001"
    failed = agent_root / "20260729T010203999Z-00000000000000000000000000000002"
    old.mkdir(parents=True)
    failed.mkdir()
    (old / "stdout.log").write_text("dead-wrapper-evidence", encoding="utf-8")
    (old / ".committed").write_text("", encoding="utf-8")
    out = tmp_path / "markerless-retention.json"
    script = tmp_path / "markerless-retention.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$WrapperLogRoot = {_pslit(str(primary))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            "$WrapperLogGenerations = 2",
            helpers,
            _CONFIRM_WRAPPER_LOG_TARGET_PS,
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "Confirm-WrapperLogTarget $target",
            "$dirs = @(Get-ChildItem -LiteralPath "
            f"{_pslit(str(agent_root))} -Directory)",
            "@{ count = $dirs.Count; "
            f"old_exists = (Test-Path {_pslit(str(old))}); "
            f"failed_exists = (Test-Path {_pslit(str(failed))}) "
            "} | ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    assert payload == {
        "count": 3,
        "old_exists": True,
        "failed_exists": True,
    }
    assert "preserving unresolved wrapper log generation" in (
        result.stdout + result.stderr
    )


def test_ps_wrapper_log_agent_paths_do_not_alias_windows_names(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    root = tmp_path / "logs"
    out = tmp_path / "agent-leaves.json"
    script = tmp_path / "agent-leaves.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$WrapperLogRoot = {_pslit(str(root))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
            helpers,
            "$a = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "$b = New-WrapperLogTargets 'worker.' "
            "([Guid]::NewGuid().ToString('N'))",
            "$c = New-WrapperLogTargets 'NUL' "
            "([Guid]::NewGuid().ToString('N'))",
            "@{ leaves = @(",
            "  (Split-Path (Split-Path $a.generation_dir -Parent) -Leaf),",
            "  (Split-Path (Split-Path $b.generation_dir -Parent) -Leaf),",
            "  (Split-Path (Split-Path $c.generation_dir -Parent) -Leaf)",
            ") } | ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
            "Discard-PendingWrapperLogTargets $a",
            "Discard-PendingWrapperLogTargets $b",
            "Discard-PendingWrapperLogTargets $c",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    leaves = json.loads(out.read_text(encoding="utf-8-sig"))["leaves"]
    assert len(set(leaves)) == 3
    assert all(leaf.startswith("agent-") for leaf in leaves)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction guard")
def test_ps_wrapper_log_root_reparse_is_rejected_before_traversal(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region wrapper-log-helpers"):
        sup.PS_TEMPLATE.index("# endregion wrapper-log-helpers")
    ]
    victim = tmp_path / "victim"
    victim.mkdir()
    primary = tmp_path / "primary-junction"
    fallback = tmp_path / "fallback"
    out = tmp_path / "reparse-root.json"
    script = tmp_path / "reparse-root.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$null = New-Item -ItemType Junction -Path {_pslit(str(primary))} "
            f"-Target {_pslit(str(victim))}",
            f"$WrapperLogRoot = {_pslit(str(primary))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(fallback))}",
            f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
            helpers,
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "@{ selected = $target.generation_dir; "
            f"victim_children = @(Get-ChildItem -LiteralPath {_pslit(str(victim))}).Count "
            "} | ConvertTo-Json | "
            f"Set-Content {_pslit(str(out))} -Encoding utf8",
            "Discard-PendingWrapperLogTargets $target",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(out.read_text(encoding="utf-8-sig"))
    assert Path(payload["selected"]).is_relative_to(fallback)
    assert payload["victim_children"] == 0
    assert "reparse point" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "shell",
    _windows_powershell_hosts(),
    ids=lambda shell: Path(shell).stem if shell else "unavailable",
)
def test_ps_wrapper_redirect_closes_supervisor_capture_pipes_before_child_exit(
    tmp_path: Path,
    shell: str | None,
) -> None:
    """A long-lived wrapper must not inherit the supervisor caller's PIPEs."""
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    log_root = tmp_path / "logs"
    stop_path = tmp_path / "stop"
    pid_path = tmp_path / "child-pid.txt"
    result_path = tmp_path / "launch-result.json"
    code = "\n".join([
        "import os",
        "import sys",
        "import time",
        "from pathlib import Path",
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')",
        "print('PIPE-CLOSURE-STDOUT', flush=True)",
        "print('PIPE-CLOSURE-STDERR', file=sys.stderr, flush=True)",
        f"stop = Path({str(stop_path)!r})",
        "while not stop.exists():",
        "    time.sleep(0.02)",
    ])
    script = tmp_path / "capture-pipe-closure.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            "$env:OS = $null",
            f"$WrapperLogRoot = {_pslit(str(log_root))}",
            f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
            f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
            helpers,
            "$target = New-WrapperLogTargets 'worker' "
            "([Guid]::NewGuid().ToString('N'))",
            "$startArgs = @{ FilePath = "
            f"{_pslit(sys.executable)}; ArgumentList = "
            f"{_pslit(subprocess.list2cmdline(['-X', 'utf8', '-c', code]))}; "
            f"WorkingDirectory = {_pslit(str(tmp_path))}; PassThru = $true; "
            "RedirectStandardOutput = $target.stdout; "
            "RedirectStandardError = $target.stderr }",
            "$p = (Start-WrapperProcess $startArgs).Process",
            "@{ pid = $p.Id; stdout = $target.stdout; stderr = $target.stderr } | "
            f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    proc = subprocess.Popen(
        [shell, "-NoProfile", "-File", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # A cold Windows PowerShell 5.1 host under a loaded runner can take
        # a while just to START - that startup latency has nothing to do
        # with the property under test (pipe inheritance) and must not
        # share a budget with it. Wait for the deterministic signal that
        # the PS parent finished its own work (it wrote the result JSON
        # right after the async launch returned) before timing the pipe
        # closure itself, so a slow start can never masquerade as either a
        # timeout OR a false "pipes stayed open" report.
        startup_deadline = time.monotonic() + 60
        while time.monotonic() < startup_deadline and not result_path.exists():
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        assert result_path.exists(), (
            f"PS parent never reached its result-write instruction: "
            f"{proc.poll()}"
        )
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stop_path.write_text("stop", encoding="utf-8")
            stdout, stderr = proc.communicate(timeout=15)
            pytest.fail(
                "supervisor capture pipes stayed open until the wrapper exited: "
                f"{stdout}{stderr}"
            )
        assert proc.returncode == 0, f"{stdout}{stderr}"
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_path.exists():
            time.sleep(0.02)
        assert pid_path.exists(), "wrapper never reached its first observable instruction"
        worker_pid = int(pid_path.read_text(encoding="utf-8"))
        assert worker_pid > 0
        # In an installed-wheel run, sys.executable is a venv launcher:
        # CreateProcess reports that long-lived launcher's PID while the base
        # interpreter child executes this code with a different PID (#50).
        # Pipe closure depends on the launcher staying live and the worker's
        # output reaching only the selected files, not on PID equality.
        # Checked in-process (no auxiliary PowerShell spawn, no extra
        # wall-clock budget to share with the property under test).
        assert _process_alive(int(payload["pid"]))
        deadline = time.monotonic() + 5
        captured_stdout = ""
        captured_stderr = ""
        while time.monotonic() < deadline:
            captured_stdout = Path(payload["stdout"]).read_text(
                encoding="utf-8",
                errors="replace",
            )
            captured_stderr = Path(payload["stderr"]).read_text(
                encoding="utf-8",
                errors="replace",
            )
            if (
                "PIPE-CLOSURE-STDOUT" in captured_stdout
                and "PIPE-CLOSURE-STDERR" in captured_stderr
            ):
                break
            time.sleep(0.02)
        assert "PIPE-CLOSURE-STDOUT" in captured_stdout
        assert "PIPE-CLOSURE-STDERR" in captured_stderr
    finally:
        stop_path.write_text("stop", encoding="utf-8")
        # Best-effort teardown only: kill in-process (no auxiliary PowerShell
        # spawn to add its own timeout risk) and never let cleanup fail a
        # test that already passed its actual assertions.
        if pid_path.exists():
            try:
                child_pid = int(pid_path.read_text(encoding="utf-8"))
                os.kill(child_pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass


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
    admission = text[
        text.index("# region launch-admission-helpers"):
        text.index("# endregion launch-admission-helpers")
    ]
    assert "function Stop-Tree" in block and "function Seed-CodexHome" in block
    return (
        "function Assert-ActionsEnabled([string]$what) { return $true }\n"
        + block
        + "\n"
        + admission
    )


def _accepted_launch_artifact(
    *,
    population: str,
    agent: str,
    cli: str,
    windows_file: str,
    argv: list[str],
    cwd: str,
    wrapped: bool,
    module_args_from: int | None = None,
    nonce_insert_at: int | None = None,
    child_argv: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "status": "accepted",
        "population": population,
        "agent": agent,
        "cli": cli,
        "wrapped": wrapped,
        "windows_file": windows_file,
        "argv": argv,
        "cwd": cwd,
        "module_args_from": module_args_from,
        "wrapper_dispatch": wrapped,
        "nonce_insert_at": nonce_insert_at if wrapped else None,
        "child_argv": list(child_argv or []) if wrapped else [],
    }


# Round 23: Complete-WrapperLogTargets is gone - marking a generation
# committed is the WRAPPER's own job now (installed_standard_streams_from_
# environment, Python side), not something the supervisor calls. Tests that
# exercise retention still need to simulate "the wrapper confirmed this
# generation" without invoking the real wrapper process; this is the exact
# two-line marker transition New-WrapperLogTargets's own .pending write
# and the wrapper's own confirmation perform for real. Best-effort, same as
# the real Python function it stands in for (_confirm_wrapper_log_generation)
# - some of these tests override Remove-Item globally to simulate a locked
# generation, and this must not itself throw when that happens.
_CONFIRM_WRAPPER_LOG_TARGET_PS = (
    "function Confirm-WrapperLogTarget($target) {\n"
    "  try {\n"
    "    [IO.File]::WriteAllText((Join-Path $target.generation_dir '.committed'), "
    "'', (New-Object Text.UTF8Encoding($false)))\n"
    "    Remove-Item -LiteralPath (Join-Path $target.generation_dir '.pending') "
    "-Force -ErrorAction SilentlyContinue\n"
    "  } catch {}\n"
    "}"
)


def _pslit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


@pytest.mark.parametrize(
    "shell",
    _windows_powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
@pytest.mark.parametrize("launcher", ["Launch", "Launch-Spec"])
@pytest.mark.parametrize(
    "failure_mode",
    ["second-before-write", "empty-after-write"],
)
def test_launch_environment_apply_failure_restores_parent_without_spawn(
    tmp_path: Path,
    shell: str | None,
    launcher: str,
    failure_mode: str,
) -> None:
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    helpers = _exec_helpers(tmp_path)
    launchers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch($name"):
        sup.PS_TEMPLATE.index("# Console action log")
    ]
    output = tmp_path / "environment-rollback.json"
    script = tmp_path / "environment-rollback.ps1"
    launch_argv = [
        "-m", "agenttalk", "--root", str(tmp_path), "wrap", "--for",
        "reviewer", "--cli", "codex", "--loop", "--one-shot",
        "--to-request", "lr-rollback", "--", "codex.exe",
    ]
    regular_admission = _accepted_launch_artifact(
        population="regular", agent="reviewer", cli="codex",
        windows_file="python.exe", argv=launch_argv, cwd=str(tmp_path),
        wrapped=True, module_args_from=0, nonce_insert_at=2,
        child_argv=["codex.exe"],
    )
    ephemeral_admission = {
        **regular_admission,
        "population": "ephemeral",
    }
    invoke = (
        "$null = Launch 'reviewer' $plan $null"
        if launcher == "Launch"
        else "$null = Launch-Spec 'reviewer' $spec $null"
    )
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$Root = {_pslit(str(tmp_path))}",
            "$AgenttalkPython = 'python.exe'",
            "$SrcOnPyPath = $true",
            "$WrapperLogMaxBytes = 1024",
            "$WrapperLogSegments = 2",
            "$WrapperLogEnvKeys = @(",
            "  'AGENTTALK_WRAPPER_STDOUT_LOG',",
            "  'AGENTTALK_WRAPPER_STDERR_LOG',",
            "  'AGENTTALK_WRAPPER_LOG_MAX_BYTES',",
            "  'AGENTTALK_WRAPPER_LOG_SEGMENTS',",
            "  'AGENTTALK_WRAPPER_LOG_NONCE')",
            helpers,
            "$script:discardCount = 0",
            "function New-WrapperLogTargets($name, $nonce) {",
            "  return [pscustomobject]@{ stdout = 'stdout.log'; stderr = 'stderr.log' }",
            "}",
            "function Discard-PendingWrapperLogTargets($targets) {",
            "  $script:discardCount += 1",
            "}",
            "function Proc-Start($id) { return '1' }",
            "function Quote-Arg([string]$arg) { return $arg }",
            "$script:spawnCount = 0",
            "function Start-WrapperProcess($startArgs) {",
            "  $script:spawnCount += 1",
            "  return [pscustomobject]@{ Process = [pscustomobject]@{ Id = 42 }; Redirected = $false }",
            "}",
            launchers,
            f"$regularAdmission = ({_pslit(json.dumps(regular_admission))} | ConvertFrom-Json)",
            f"$ephemeralAdmission = ({_pslit(json.dumps(ephemeral_admission))} | ConvertFrom-Json)",
            "$profileEnv = [pscustomobject]@{",
            "  AGENTTALK_FAILURE_PRESENT = 'during-present';",
            "  AGENTTALK_FAILURE_ABSENT = 'during-absent';",
            "  AGENTTALK_FAILURE_EMPTY = 'during-empty'",
            "}",
            "$spec = [pscustomobject]@{",
            "  cli = 'codex'; cwd = $Root; env = $profileEnv;",
            "  window_style = 'Hidden'; window_style_warning = $null;",
            "  launch_admission = $ephemeralAdmission;",
            "  launch = [pscustomobject]@{",
            "    windows_file = 'python.exe';",
            "    windows_args = @('-m','agenttalk','--root',$Root,'wrap','--for',",
            "      'reviewer','--cli','codex','--loop','--one-shot','--to-request',",
            "      'lr-rollback','--','codex.exe')",
            "  }",
            "}",
            "$cfg = [pscustomobject]@{ agents = [pscustomobject]@{} }",
            "$agentConfig = [pscustomobject]@{",
            "  cli = 'codex'; wrapped = $true; cwd = $Root; env = $profileEnv;",
            "  launch = $spec.launch",
            "}",
            "$cfg.agents | Add-Member -NotePropertyName 'reviewer' -NotePropertyValue $agentConfig",
            "$plan = [pscustomobject]@{",
            "  launch_mode = 'wrap'; session_id = $null; session_args = @();",
            "  window_style = 'Hidden'; window_style_warning = $null;",
            "  launch_admission = $regularAdmission",
            "}",
            "$names = @(",
            "  'AGENTTALK_ROOT', 'AGENTTALK_PY', 'PYTHONPATH',",
            "  'AGENTTALK_NO_CHILD_WINDOW', 'AGENTTALK_FAILURE_PRESENT',",
            "  'AGENTTALK_FAILURE_ABSENT', 'AGENTTALK_FAILURE_EMPTY',",
            "  'AGENTTALK_PYTHON',",
            "  'AGENTTALK_SHIM_ACTIVE', 'AGENTTALK_SHIM_PARENT_PYTHONPATH',",
            "  'AGENTTALK_SHIM_PARENT_PYTHONPATH_ABSENT',",
            "  'AGENTTALK_WRAPPER_STDOUT_LOG', 'AGENTTALK_WRAPPER_STDERR_LOG',",
            "  'AGENTTALK_WRAPPER_LOG_MAX_BYTES', 'AGENTTALK_WRAPPER_LOG_SEGMENTS',",
            "  'AGENTTALK_WRAPPER_LOG_NONCE')",
            "foreach ($name in $names) {",
            "  if ($name -eq 'AGENTTALK_FAILURE_ABSENT') {",
            "    [Environment]::SetEnvironmentVariable($name, $null, 'Process')",
            "  } elseif ($name -eq 'AGENTTALK_FAILURE_EMPTY') {",
            "    $null = [AgenttalkSupervisorNativeV3]::DeleteEnvironmentVariable($name)",
            "    if (-not [AgenttalkSupervisorNativeV3]::SetEmptyEnvironmentVariable($name)) {",
            "      throw 'failed to seed an empty process-environment value'",
            "    }",
            "  } else {",
            "    [Environment]::SetEnvironmentVariable($name, ('before-' + $name), 'Process')",
            "  }",
            "}",
            "function Get-EnvironmentSnapshot {",
            "  $snapshot = [ordered]@{}",
            "  foreach ($name in $names) {",
            "    $state = [AgenttalkSupervisorNativeV3]::ReadEnvironmentVariable($name)",
            "    $snapshot[$name] = [ordered]@{ present = $state.Present; value = $state.Value }",
            "  }",
            "  return $snapshot",
            "}",
            "$before = Get-EnvironmentSnapshot",
            f"$failureMode = {_pslit(failure_mode)}",
            "$script:environmentOperation = 0",
            "$script:injected = $false",
            "function Set-Item {",
            "  [CmdletBinding()] param([string]$LiteralPath, $Value)",
            "  if ($LiteralPath -like 'Env:*') {",
            "    $script:environmentOperation += 1",
            "    if (-not $script:injected -and",
            "        $failureMode -eq 'empty-after-write' -and",
            "        $LiteralPath -eq 'Env:AGENTTALK_FAILURE_EMPTY') {",
            "      Microsoft.PowerShell.Management\\Set-Item @PSBoundParameters",
            "      $script:injected = $true",
            "      throw 'injected environment application failure after empty write'",
            "    }",
            "    if (-not $script:injected -and",
            "        $failureMode -eq 'second-before-write' -and",
            "        $script:environmentOperation -eq 2) {",
            "      $script:injected = $true",
            "      throw 'injected environment application failure'",
            "    }",
            "  }",
            "  Microsoft.PowerShell.Management\\Set-Item @PSBoundParameters",
            "}",
            "function Remove-Item {",
            "  [CmdletBinding()] param([string]$LiteralPath)",
            "  if ($LiteralPath -like 'Env:*') {",
            "    $script:environmentOperation += 1",
            "    if (-not $script:injected -and",
            "        $failureMode -eq 'second-before-write' -and",
            "        $script:environmentOperation -eq 2) {",
            "      $script:injected = $true",
            "      throw 'injected environment application failure'",
            "    }",
            "  }",
            "  Microsoft.PowerShell.Management\\Remove-Item @PSBoundParameters",
            "}",
            "$caught = $false",
            "try {",
            f"  {invoke}",
            "} catch {",
            "  $caught = $true",
            "}",
            "$after = Get-EnvironmentSnapshot",
            "[pscustomobject]@{",
            "  injected = $script:injected; caught = $caught;",
            "  spawn_count = $script:spawnCount; discard_count = $script:discardCount;",
            "  before = $before; after = $after",
            "} | ConvertTo-Json -Depth 5 | ",
            f"  Set-Content {_pslit(str(output))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(output.read_text(encoding="utf-8-sig"))
    assert payload["before"]["AGENTTALK_FAILURE_EMPTY"] == {
        "present": True,
        "value": "",
    }
    assert payload["injected"] is True
    assert payload["caught"] is True
    assert payload["spawn_count"] == 0
    assert payload["discard_count"] == 1
    assert payload["after"] == payload["before"]


# Exhaustive policy data for the one remaining interpreter-prefix resolver.
# Generated PowerShell no longer re-parses this grammar; it consumes the typed
# admission artifact and its producer-owned module/nonce positions.
_PREFIX_TOKEN_AGREEMENT_TABLE: list[tuple[str, bool]] = [
    # allowed: this project's actual needs (I2, 5th-leak-round allowlist)
    ("-u", True), ("-I", True), ("-S", True), ("-B", True), ("-E", True), ("-P", True),
    ("-Xutf8", True), ("-Xdev", True), ("-X3", True),
    # round 15 connector finding: -W (attached form only, same shape as
    # -X) and -b (bare, same shape as -B) were never evaluated either way.
    ("-b", True), ("-bb", True),
    ("-Wignore", True), ("-Werror", True), ("-Wdefault", True),
    # -W's own separate-token form (no attached value) is not supported,
    # same reasoning as -X's own case a few lines below.
    ("-W", False),
    # execution-mode tokens (I2, rounds 1-4)
    ("-", False), ("-m", False), ("-mfoo", False), ("-magenttalk", False),
    ("-c", False), ("-cprint(1)", False), ("--", False),
    ("script.py", False), ("helper.py", False), ("agenttalk", False),
    # terminating options - run NO program at all (I2, 5th leak)
    ("--version", False), ("-V", False), ("-h", False), ("-?", False),
    ("--help", False), ("--help-env", False), ("--help-xoptions", False),
    ("--help-all", False),
    # -X's own separate-token form (no attached value) is not supported
    ("-X", False),
    # deliberately excluded despite looking superficially safe
    ("-O", False), ("-OO", False), ("-i", False),
    # invented / never-real spellings, including the wrong-case forms of
    # this round's two additions - -w is not a CPython flag at all, and
    # a case-insensitive compare would equate it with -W by accident,
    # the same class of mistake -i-vs-I already guards against above.
    ("-Z", False), ("-3", False), ("-Q", False), ("-w", False), ("-wignore", False),
    # round 21 connector finding: -X presite=MODULE (debug builds) imports
    # an arbitrary module before site initialization - before -m agenttalk
    # is ever reached - the same before-main property as -c/-m/a bare
    # script path, just spelled as a -X sub-option. The ONE -X attached
    # form this allowlist must refuse despite the blanket -X* accept above.
    ("-Xpresite=mod", False), ("-Xpresite=some.module", False),
    # bare, no "=value" attached at all - the denial is a PREFIX match on
    # "presite", not on "presite=", specifically so this doesn't slip
    # through: "presite".startswith("presite=") is False, so a value-less
    # spelling would otherwise fall through to the blanket -X* accept.
    ("-Xpresite", False),
    # a DIFFERENT, invented -X sub-option that merely SHARES presite's
    # first few characters must still be allowed - the denial is scoped to
    # "presite" itself, not to a broad "-Xpre*" family.
    ("-Xpreflight", True),
    # round 24 connector finding, PROVEN by running it: -Wignore::evil.W
    # imports 'evil' (warnings._getcategory: category.rpartition('.') then
    # __import__(module)) before -m agenttalk is ever reached - the same
    # before-main property as -X presite=, just reachable through -W's own
    # attached-value grammar (action:message:category:module:lineno)
    # instead of a sub-option. Refuse whenever a category field (the 3rd
    # colon-separated component) is present and dotted, regardless of
    # action or how many trailing fields follow it.
    ("-Wignore::evil.W", False), ("-Werror::evil.W", False),
    ("-Wignore:msg:evil.W", False), ("-Wignore:msg:evil.W:mod:5", False),
    # a category with NO dot never reaches _getcategory's import branch -
    # only a plain attribute lookup on the builtins module - so this stays
    # allowed even though a category field is present.
    ("-Wignore::UserWarning", True), ("-Wignore::DeprecationWarning", True),
    # a dot in the MESSAGE field (index 1, not the category field at index
    # 2) never reaches the import branch at all - only 2 colon-separated
    # parts means there IS no category field yet.
    ("-Wignore:some.message.text", True),
    # a dot in the MODULE field (index 3, used only as a regex match
    # against the emitting frame's __name__, never imported) with a
    # dot-free category must stay allowed.
    ("-Wignore::UserWarning:some.module.path:5", True),
    # an explicit but EMPTY category field ("::" with nothing between) is
    # falsy - _getcategory returns the default Warning class without
    # importing anything.
    ("-Wignore::", True),
]


def _pick_pwsh_anywhere() -> str | None:
    """Unlike _pick_powershell (deliberately Windows-only, for tests
    exercising Windows-specific behavior like .cmd shims and Start-Process
    quoting), this is for tests that only exercise pure PowerShell-language
    logic - string/array comparisons with no OS-specific call underneath.
    GitHub's ubuntu-latest and macos-latest runners ship pwsh too (checked
    against actions/runner-images' own software manifests, not assumed),
    so a test using this picker runs on every CI leg, not just Windows -
    which matters for a cross-language AGREEMENT contract: a check that
    silently skips on 8 of 12 legs is a check that is mostly not run."""
    return shutil.which("pwsh")


@pytest.mark.parametrize(
    ("token", "expected"),
    _PREFIX_TOKEN_AGREEMENT_TABLE,
)
def test_python_interpreter_prefix_allowlist_is_exhaustive(
    token: str,
    expected: bool,
) -> None:
    assert sup._allowed_interpreter_prefix_token(token) is expected  # noqa: SLF001


def test_supervisor_launch_nonce_injection_consumes_typed_position(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    python_artifact = _accepted_launch_artifact(
        population="regular", agent="worker", cli="codex",
        windows_file="python.exe",
        argv=[
            "-u", "-m", "agenttalk", "--root", "R", "wrap", "--for",
            "worker", "--loop", "--", "codex.exe", "", "tail",
        ],
        cwd="R", wrapped=True, module_args_from=1, nonce_insert_at=3,
        child_argv=["codex.exe", "", "tail"],
    )
    direct_artifact = _accepted_launch_artifact(
        population="regular", agent="worker", cli="codex",
        windows_file="agenttalk.exe",
        argv=[
            "--root", "R", "wrap", "--for", "worker", "--loop", "--",
            "codex.exe",
        ],
        cwd="R", wrapped=True, nonce_insert_at=0,
        child_argv=["codex.exe"],
    )
    manual_artifact = _accepted_launch_artifact(
        population="regular", agent="manual", cli="codex",
        windows_file="codex.exe", argv=["exec", "review"], cwd="R",
        wrapped=False,
    )
    payload = {
        "python": python_artifact,
        "direct": direct_artifact,
        "manual": manual_artifact,
    }
    out = tmp_path / "typed-nonce-injection.json"
    script = tmp_path / "typed-nonce-injection.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            helpers,
            f"$payload = ({_pslit(json.dumps(payload))} | ConvertFrom-Json)",
            f"$nonce = {_pslit(SUPERVISOR_NONCE)}",
            "$python = Add-SupervisorLaunchNonceAt $payload.python $nonce",
            "$direct = Add-SupervisorLaunchNonceAt $payload.direct $nonce",
            "$manual = Add-SupervisorLaunchNonceAt $payload.manual $nonce",
            "[pscustomobject]@{",
            "  python = $python",
            "  direct = $direct",
            "  manual = $manual",
            "} | ConvertTo-Json -Depth 7 | ",
            f"  Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["python"]["injected"] is True
    assert data["python"]["argv"] == [
        "-u", "-m", "agenttalk", "--supervisor-launch-nonce",
        SUPERVISOR_NONCE, "--root", "R", "wrap", "--for", "worker",
        "--loop", "--", "codex.exe", "", "tail",
    ]
    assert data["direct"]["injected"] is True
    assert data["direct"]["argv"][:2] == [
        "--supervisor-launch-nonce", SUPERVISOR_NONCE,
    ]
    assert data["manual"]["injected"] is False
    assert data["manual"]["argv"] == ["exec", "review"]
    assert data["manual"]["missing_reason"] == "unsupported_launch_argv"


@pytest.mark.parametrize(
    "shell",
    _windows_powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
def test_ps_regular_wrapped_launch_consumes_planned_loop_admission(
    tmp_path: Path,
    shell: str | None,
) -> None:
    if shell is None:
        return
    case_args = [
        (
            "loop-before-tail",
            [
                "-m", "agenttalk", "wrap", "--for", "worker", "--loop",
                "--", "codex.exe",
            ],
            True,
        ),
        (
            "missing-loop",
            [
                "-m", "agenttalk", "wrap", "--for", "worker", "--",
                "codex.exe",
            ],
            False,
        ),
        (
            "loop-only-in-child-tail",
            [
                "-m", "agenttalk", "wrap", "--for", "worker", "--",
                "codex.exe", "--loop",
            ],
            False,
        ),
    ]
    cases = []
    for label, windows_args, expected_spawn in case_args:
        agent = {
            "backend_profile": None,
            "cli": "codex",
            "wrapped": True,
            "cwd": str(tmp_path),
            "env": None,
            "launch": {
                "windows_file": "python.exe",
                "windows_args": windows_args,
            },
        }
        plan = {
            "agents": {
                "worker": {
                    "action": sup.RELAUNCH,
                    "launch_mode": "wrap",
                    "session_args": [],
                    "window_style": "Hidden",
                    "window_style_warning": None,
                },
            },
        }
        sup.attach_regular_launch_admissions(
            plan,
            {"agents": {"worker": agent}},
            root=tmp_path,
        )
        cases.append({
            "label": label,
            "agent": agent,
            "plan": plan["agents"]["worker"],
            "expected_spawn": expected_spawn,
        })

    helpers = _exec_helpers(tmp_path)
    launchers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch($name"):
        sup.PS_TEMPLATE.index("# Console action log")
    ]
    out = tmp_path / "wrapped-loop-admission.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(str(tmp_path))}",
        "$AgenttalkPython = 'python.exe'",
        "$SrcOnPyPath = $false",
        "$WrapperLogEnvKeys = @()",
        helpers,
        "$script:spawned = $false",
        "$script:warnings = @()",
        "function Write-Warning { param([string]$Message) "
        "$script:warnings += $Message }",
        "function New-WrapperLogTargets($name, $nonce) { return $null }",
        "function Discard-PendingWrapperLogTargets($targets) {}",
        "function Quote-Arg([string]$arg) { return $arg }",
        "function Proc-Start($id) { return '1' }",
        "function Start-WrapperProcess($startArgs) {",
        "  $script:spawned = $true",
        "  return [pscustomobject]@{",
        "    Process = [pscustomobject]@{ Id = 4242 }",
        "    Redirected = $false",
        "  }",
        "}",
        launchers,
        f"$cases = ({_pslit(json.dumps(cases))} | ConvertFrom-Json)",
        "$rows = @()",
        "foreach ($case in @($cases)) {",
        "  $script:spawned = $false",
        "  $script:warnings = @()",
        "  $cfg = [pscustomobject]@{ agents = [pscustomobject]@{ "
        "worker = $case.agent } }",
        "  $result = Launch 'worker' $case.plan $null",
        "  $rows += [pscustomobject]@{",
        "    label = $case.label",
        "    spawned = $script:spawned",
        "    returned = ($null -ne $result)",
        "    warnings = @($script:warnings)",
        "  }",
        "}",
        "$rows | ConvertTo-Json -Depth 6 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    script = tmp_path / "wrapped-loop-admission.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    rows = {
        row["label"]: row
        for row in json.loads(out.read_text(encoding="utf-8-sig"))
    }
    for label, _windows_args, expected_spawn in case_args:
        row = rows[label]
        assert row["spawned"] is expected_spawn, row
        assert row["returned"] is expected_spawn, row
        if expected_spawn:
            assert row["warnings"] == [], row
        else:
            warning = "\n".join(row["warnings"])
            assert "launch admission refused" in warning, row
            assert "wrapped launch requires --loop before the child delimiter" in warning, row


def test_regular_plan_finalizer_emits_typed_normalized_argv(
    tmp_path: Path,
) -> None:
    root = str(tmp_path)
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": root,
        "env": {"TAB\tNAME": "preserved"},
        "launch": {
            "windows_file": "python.exe",
            "module_args_from": 0,
            "windows_args": [
                "-m", "agenttalk", "--root", "{ROOT}", "wrap",
                "--for", "worker", "--cli", "codex", "--loop", "--",
                "codex.exe", "alpha", "", "omega",
            ],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    result = sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=root,
    )

    artifact = result["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["population"] == "regular"
    assert artifact["argv"] == [
        "-m", "agenttalk", "--root", root, "wrap",
        "--for", "worker", "--cli", "codex", "--loop", "--",
        "codex.exe", "alpha", "", "omega",
    ]
    assert artifact["child_argv"] == ["codex.exe", "alpha", "", "omega"]
    assert artifact["nonce_insert_at"] == 2


@pytest.mark.parametrize("loop_option", ["--lo", "--loo"])
def test_regular_plan_finalizer_accepts_loop_abbreviations(
    tmp_path: Path,
    loop_option: str,
) -> None:
    root = str(tmp_path)
    args = [
        "-m", "agenttalk", "--root", "{ROOT}", "wrap", "--for", "worker",
        "--cli", "codex", loop_option, "--", "codex.exe",
    ]
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {
            "agents": {
                "worker": {
                    "cli": "codex",
                    "wrapped": True,
                    "cwd": root,
                    "launch": {
                        "windows_file": "python.exe",
                        "windows_args": args,
                        "module_args_from": 0,
                    },
                },
            },
        },
        root=root,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["argv"][-3:] == [loop_option, "--", "codex.exe"]
    assert artifact["child_argv"] == ["codex.exe"]


def test_regular_plan_finalizer_binds_or_refuses_claude_session_identity(
    tmp_path: Path,
) -> None:
    row = {
        "cli": "claude",
        "wrapped": False,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": "claude.exe",
            "windows_args": ["{SESSION_ARGS}"],
        },
    }

    fresh_detail = sup._launch_detail({}, row)
    fresh_plan = {
        "agents": {"worker": {"action": sup.RELAUNCH, **fresh_detail}},
    }
    minted: list[str] = []

    def mint() -> str:
        minted.append("session-fresh")
        return minted[-1]

    sup.attach_regular_launch_admissions(
        fresh_plan,
        {"agents": {"worker": row}},
        root=tmp_path,
        session_id_factory=mint,
    )
    fresh = fresh_plan["agents"]["worker"]
    assert minted == ["session-fresh"]
    assert fresh["session_id"] == "session-fresh"
    assert fresh["launch_admission"]["status"] == "accepted"
    assert "session-fresh" in fresh["launch_admission"]["argv"]
    assert "{SESSION_ID}" not in fresh["launch_admission"]["argv"]

    resume_detail = sup._launch_detail(
        {"session_id": "session-existing", "resume_available": True},
        row,
    )
    resume_plan = {
        "agents": {"worker": {"action": sup.RELAUNCH, **resume_detail}},
    }
    sup.attach_regular_launch_admissions(
        resume_plan,
        {"agents": {"worker": row}},
        root=tmp_path,
        session_id_factory=lambda: pytest.fail("resume must not mint a session id"),
    )
    resume = resume_plan["agents"]["worker"]
    assert resume["launch_admission"]["status"] == "accepted"
    assert "session-existing" in resume["launch_admission"]["argv"]

    legacy_detail = sup._launch_detail({"launched": True}, row)
    legacy_plan = {
        "agents": {"worker": {"action": sup.RELAUNCH, **legacy_detail}},
    }
    sup.attach_regular_launch_admissions(
        legacy_plan,
        {"agents": {"worker": row}},
        root=tmp_path,
        session_id_factory=lambda: pytest.fail("resume must not mint a session id"),
    )
    legacy = legacy_plan["agents"]["worker"]["launch_admission"]
    assert legacy["status"] == "refused"
    assert "session identity is unavailable" in legacy["reason"]
    assert "argv" not in legacy


def test_regular_plan_finalizer_preserves_literal_session_prompt_text(
    tmp_path: Path,
) -> None:
    prompt = "Replace: explain the literal {AGENT} marker"
    row = {
        "cli": "claude",
        "wrapped": False,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": "claude.exe",
            "windows_args": ["{SESSION_ARGS}"],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "fresh",
                "session_args": ["-p", prompt],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["argv"] == ["-p", prompt]


@pytest.mark.parametrize(
    ("workspace_args", "accepted"),
    [
        (["--add-dir=-foo"], True),
        (["--add-dir", "-foo"], False),
        (["--add-dir="], False),
        (["--add-dir", ""], False),
    ],
    ids=[
        "inline-option-like-value", "separate-option-like-value",
        "empty-inline-value", "empty-separate-value",
    ],
)
def test_regular_codex_workspace_option_matches_real_value_forms(
    tmp_path: Path,
    workspace_args: list[str],
    accepted: bool,
) -> None:
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": "python.exe",
            "windows_args": [
                "-m", "agenttalk", "wrap", "--for", "worker", "--loop",
                "--", "codex.exe", *workspace_args,
            ],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert (artifact["status"] == "accepted") is accepted
    if accepted:
        assert artifact["child_argv"][-1] == "--add-dir=-foo"
    else:
        assert "workspace arguments are invalid" in artifact["reason"]


def test_regular_plan_finalizer_preserves_argparse_last_occurrence(
    tmp_path: Path,
) -> None:
    """The shared seam must not reject a launch the real parser accepts."""
    args = [
        "-m", "agenttalk", "wrap", "--for", "other", "--for", "worker",
        "--cli", "claude", "--cli", "codex", "--loop", "--",
        "codex.exe",
    ]
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": "python.exe",
            "windows_args": args,
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["argv"] == [
        "-m", "agenttalk", "--root", str(tmp_path), *args[2:],
    ]
    assert artifact["module_args_from"] == 0


@pytest.mark.parametrize(
    "command_args",
    [
        ["-O", "-m", "agenttalk"],
        ["-W", "default", "-m", "agenttalk"],
        ["-uWdefault", "-m", "agenttalk"],
        ["-X", "utf8", "-m", "agenttalk"],
        ["-uXutf8", "-m", "agenttalk"],
        ["-im", "agenttalk"],
        ["-Omagenttalk"],
    ],
    ids=[
        "optimization", "separate-warning", "clustered-warning",
        "separate-xoption", "clustered-xoption", "clustered-module",
        "attached-clustered-module",
    ],
)
def test_python_selector_finds_agenttalk_after_real_interpreter_prefixes(
    command_args: list[str],
) -> None:
    tail = ["wrap", "--for", "worker", "--loop", "--", "codex.exe"]

    assert launch_admission.python_agenttalk_module_argv(
        [*command_args, *tail],
        program_kind="python",
    ) == tuple(tail)


@pytest.mark.parametrize(
    "command_args",
    [
        ["-V", "-m", "agenttalk", "wrap"],
        ["--help", "-m", "agenttalk", "wrap"],
        ["-c", "print('ok')", "-m", "agenttalk", "wrap"],
        ["helper.py", "-m", "agenttalk", "wrap"],
        ["--", "-m", "agenttalk", "wrap"],
        ["-", "-m", "agenttalk", "wrap"],
    ],
    ids=["version", "help", "command", "script", "delimiter", "stdin"],
)
def test_python_selector_stops_after_non_module_execution_target(
    command_args: list[str],
) -> None:
    assert launch_admission.python_agenttalk_module_argv(
        command_args,
        program_kind="python",
    ) is None


def test_python_selector_accepts_py_launcher_selector() -> None:
    tail = ("wrap", "--for", "worker", "--loop", "--", "codex.exe")

    assert launch_admission.python_agenttalk_module_argv(
        ["-3.14-64", "-m", "agenttalk", *tail],
        program_kind="py",
    ) == tail


@pytest.mark.parametrize("listing_option", ["-0", "--list", "--list-paths"])
def test_python_selector_stops_at_py_launcher_listing(
    listing_option: str,
) -> None:
    assert launch_admission.python_agenttalk_module_argv(
        [listing_option, "-m", "agenttalk", "wrap"],
        program_kind="py",
    ) is None


@pytest.mark.parametrize(
    "configured_root",
    [
        ["--roo", "D:/configured"],
        ["--roo=D:/configured"],
    ],
)
def test_regular_plan_finalizer_canonicalizes_valid_root_abbreviations(
    tmp_path: Path,
    configured_root: list[str],
) -> None:
    root = str(tmp_path)
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": root,
        "launch": {
            "windows_file": "python.exe",
            "module_args_from": 0,
            "windows_args": [
                "-m", "agenttalk", *configured_root, "wrap", "--for",
                "worker", "--loop", "--", "codex.exe",
            ],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=root,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    argv = artifact["argv"]
    root_tokens = [
        argument
        for argument in argv
        if argument == "--root" or argument.startswith("--root=")
    ]
    assert len(root_tokens) == 1
    if root_tokens[0] == "--root":
        assert argv[argv.index("--root") + 1] == root
    else:
        assert root_tokens[0] == f"--root={root}"
    assert not any(argument.startswith("--roo") and not argument.startswith("--root") for argument in argv)


@pytest.mark.parametrize("configured_root", [[], ["--root", "D:/stale"]])
def test_regular_plan_finalizer_pins_root_before_wrap_subcommand(
    tmp_path: Path,
    configured_root: list[str],
) -> None:
    root = str(tmp_path)
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": root,
        "launch": {
            "windows_file": "python.exe",
            "windows_args": [
                "-m", "agenttalk", *configured_root, "wrap", "--for",
                "worker", "--loop", "--", "codex.exe",
            ],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=root,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    argv = artifact["argv"]
    wrap_index = argv.index("wrap")
    child_delimiter = argv.index("--", wrap_index)
    assert argv[wrap_index - 2:wrap_index] == ["--root", root]
    assert argv[child_delimiter + 1:] == ["codex.exe"]


def test_regular_plan_finalizer_does_not_invent_transport_bounds(
    tmp_path: Path,
) -> None:
    """The shared seam models execution, not the old display-only bounds."""
    root = str(tmp_path)
    long_argument = "x" * 5000
    child_arguments = [f"arg-{index}" for index in range(300)]
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": root,
        "env": {"N" * 5000: "preserved"},
        "launch": {
            "windows_file": "python.exe",
            "module_args_from": 0,
            "windows_args": [
                "-m", "agenttalk", "--root", root, "wrap", "--for",
                "worker", "--loop", "--", "codex.exe", long_argument,
                "tab\targument", "line-one\nline-two", *child_arguments,
            ],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=root,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["child_argv"] == [
        "codex.exe", long_argument, "tab\targument", "line-one\nline-two",
        *child_arguments,
    ]


@pytest.mark.parametrize("irrelevant_index", [99, "junk", -1, True])
def test_regular_manual_launch_discards_irrelevant_module_index(
    tmp_path: Path,
    irrelevant_index: object,
) -> None:
    row = {
        "cli": "codex",
        "wrapped": False,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": "python.exe",
            "windows_args": ["helper.py", "review"],
            "module_args_from": irrelevant_index,
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "fresh",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["module_args_from"] is None


def test_regular_empty_cwd_keeps_legacy_root_fallback(tmp_path: Path) -> None:
    row = {
        "cli": "codex",
        "wrapped": False,
        "cwd": "",
        "launch": {
            "windows_file": "codex.exe",
            "windows_args": ["exec", "review"],
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "fresh",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["cwd"] == str(tmp_path)


def test_regular_console_entry_wrap_discards_irrelevant_module_index(
    tmp_path: Path,
) -> None:
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": "agenttalk.exe",
            "windows_args": [
                "--root", str(tmp_path), "wrap", "--for", "worker",
                "--loop", "--", "codex.exe",
            ],
            "module_args_from": 1,
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["module_args_from"] is None
    assert artifact["nonce_insert_at"] == 0


@pytest.mark.parametrize(
    ("windows_file", "windows_args", "module_args_from"),
    [
        (
            "agenttalk.exe",
            ["wrap", "--unknown-wrapper-option", "--", "codex.exe"],
            None,
        ),
        (
            "python.exe",
            [
                "-m", "agenttalk", "wrap", "--unknown-wrapper-option",
                "--", "codex.exe",
            ],
            0,
        ),
        (
            "python.exe",
            [
                "-m", "agenttalk", "wrap", "--loop", "--for", "worker",
                "--", "codex.exe",
            ],
            "bogus",
        ),
        (
            "python.exe",
            [
                "-u", "-m", "agenttalk", "wrap", "--loop", "--for",
                "worker", "--", "codex.exe",
            ],
            None,
        ),
        (
            "python.exe",
            [
                "-O", "-m", "agenttalk", "wrap", "--loop", "--for",
                "worker", "--", "codex.exe",
            ],
            None,
        ),
        (
            "python.exe",
            [
                "-uWdefault", "-im", "agenttalk", "wrap", "--loop",
                "--for", "worker", "--", "codex.exe",
            ],
            None,
        ),
        (
            "py.exe",
            [
                "-3.14", "-m", "agenttalk", "wrap", "--loop", "--for",
                "worker", "--", "codex.exe",
            ],
            None,
        ),
        (
            "agenttalk.exe",
            ["--", "wrap", "--unknown-wrapper-option", "--", "codex.exe"],
            None,
        ),
    ],
)
def test_regular_manual_declaration_cannot_hide_a_refused_wrap_attempt(
    tmp_path: Path,
    windows_file: str,
    windows_args: list[str],
    module_args_from: object,
) -> None:
    row = {
        "cli": "codex",
        "wrapped": False,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": windows_file,
            "windows_args": windows_args,
            "module_args_from": module_args_from,
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "fresh",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "refused"
    assert (
        "unrecognized arguments: --unknown-wrapper-option" in artifact["reason"]
        or "wrapped launch declaration is missing" in artifact["reason"]
    )


@pytest.mark.parametrize(
    ("windows_file", "windows_args", "module_args_from"),
    [
        ("agenttalk.exe", ["send", "--to", "lead", "-m", "hello"], None),
        (
            "python.exe",
            ["-m", "agenttalk", "send", "--to", "lead", "-m", "hello"],
            0,
        ),
        (
            "python.exe",
            ["-u", "-m", "agenttalk", "send", "--to", "lead", "-m", "hello"],
            "bogus",
        ),
        (
            "python.exe",
            ["-V", "-m", "agenttalk", "wrap", "--for", "worker"],
            None,
        ),
        (
            "python.exe",
            ["helper.py", "-m", "agenttalk", "wrap", "--for", "worker"],
            None,
        ),
    ],
)
def test_regular_manual_agenttalk_non_wrap_command_remains_valid(
    tmp_path: Path,
    windows_file: str,
    windows_args: list[str],
    module_args_from: object,
) -> None:
    row = {
        "cli": "codex",
        "wrapped": False,
        "cwd": str(tmp_path),
        "launch": {
            "windows_file": windows_file,
            "windows_args": windows_args,
            "module_args_from": module_args_from,
        },
    }
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "fresh",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert artifact["status"] == "accepted"
    assert artifact["wrapper_dispatch"] is False


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unknown-wrapper-option", "unrecognized arguments: --mystery"),
        ("missing-child", "a launch command is required after `--`"),
        ("cli-mismatch", "cli does not match"),
        ("invalid-environment-name", "launch environment is invalid"),
    ],
)
def test_regular_plan_finalizer_refuses_shared_admission_failures(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    args = [
        "-m", "agenttalk", "--root", "{ROOT}", "wrap",
        "--for", "worker", "--cli", "codex", "--loop", "--",
        "codex.exe",
    ]
    row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": str(tmp_path),
        "env": {},
        "launch": {
            "windows_file": "python.exe",
            "module_args_from": 0,
            "windows_args": args,
        },
    }
    if case == "unknown-wrapper-option":
        args.insert(args.index("--loop"), "--mystery")
    elif case == "missing-child":
        del args[args.index("--"):]
    elif case == "cli-mismatch":
        row["cli"] = "claude"
    else:
        row["env"] = {"BAD=NAME": "value"}
    plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }

    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"worker": row}},
        root=tmp_path,
    )

    artifact = plan["agents"]["worker"]["launch_admission"]
    assert frozenset(artifact) == {
        "schema_version", "status", "population", "agent", "reason",
    }
    assert artifact["status"] == "refused"
    assert reason in artifact["reason"]


def _launch_admission_runtime_blocks(tmp_path: Path) -> tuple[str, str, str]:
    helpers = _exec_helpers(tmp_path)
    quote_arg = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("# region quote-arg"):
        sup.PS_TEMPLATE.index("# endregion quote-arg")
    ]
    launchers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch($name"):
        sup.PS_TEMPLATE.index("# Console action log")
    ]
    return helpers, quote_arg, launchers


@pytest.mark.parametrize(
    "shell",
    _windows_powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
@pytest.mark.parametrize("loop_option", ["--lo", "--loo"])
def test_launchers_consume_accepted_admission_argv_without_dropping_empty_argument(
    tmp_path: Path,
    shell: str | None,
    loop_option: str,
) -> None:
    """The typed artifact, not a second reconstruction, is executed.

    Both populations carry a middle empty child argument.  The exact shipped
    Quote-Arg path must serialize it as ``\"\"``; filtering the argv before
    quoting or making Quote-Arg reject empty strings makes this control fail.
    """
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    helpers, quote_arg, launchers = _launch_admission_runtime_blocks(tmp_path)
    cwd = str(tmp_path)
    launch_root = "R"
    regular_argv = [
        "-m", "agenttalk", "--root", launch_root, "wrap", "--for", "worker",
        "--cli", "codex", loop_option, "--", "codex.exe", "alpha", "", "omega",
    ]
    ephemeral_argv = [
        "-m", "agenttalk", "--root", launch_root, "wrap", "--for", "reviewer",
        "--cli", "codex", "--from", "reviewer", loop_option, "--one-shot",
        "--to-request", "R1",
        "--", "codex.exe", "alpha", "", "omega",
    ]
    regular_config = {
        "agents": {
            "worker": {
                "cli": "codex",
                "wrapped": True,
                "cwd": cwd,
                "launch": {
                    "windows_file": "python.exe",
                    "windows_args": regular_argv,
                    "module_args_from": 0,
                },
            },
        },
    }
    regular_plan = {
        "agents": {
            "worker": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }
    sup.attach_regular_launch_admissions(
        regular_plan,
        regular_config,
        root=launch_root,
    )
    regular_artifact = regular_plan["agents"]["worker"]["launch_admission"]
    assert regular_artifact["status"] == "accepted"
    ephemeral_row = {
        "cli": "codex",
        "wrapped": True,
        "cwd": cwd,
        "launch": {
            "windows_file": "python.exe",
            "windows_args": ephemeral_argv,
            "module_args_from": 0,
        },
    }
    ephemeral_candidate, ephemeral_problem = sup._effective_launch_candidate(  # noqa: SLF001
        ephemeral_row,
        population="ephemeral",
        agent="reviewer",
        root=launch_root,
        request_id="R1",
    )
    assert ephemeral_problem is None
    assert ephemeral_candidate is not None
    manual_plan = {
        "agents": {
            "manual": {
                "action": sup.RELAUNCH,
                "launch_mode": "fresh",
                "session_args": [],
            },
        },
    }
    manual_config = {
        "agents": {
            "manual": {
                "cli": "codex",
                "wrapped": False,
                "cwd": cwd,
                "launch": {
                    "windows_file": "configured.exe",
                    "windows_args": ["alpha", "", "omega"],
                    "module_args_from": 99,
                },
            },
        },
    }
    sup.attach_regular_launch_admissions(
        manual_plan,
        manual_config,
        root=launch_root,
    )
    manual_artifact = manual_plan["agents"]["manual"]["launch_admission"]
    assert manual_artifact["status"] == "accepted"
    assert manual_artifact["module_args_from"] is None
    payload = {
        "regular": regular_artifact,
        "ephemeral": ephemeral_candidate.artifact(),
        "manual": manual_artifact,
    }
    output = tmp_path / "accepted-launch-admissions.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(launch_root)}",
        "$AgenttalkPython = 'python.exe'",
        "$SrcOnPyPath = $false",
        "$WrapperLogMaxBytes = 1024",
        "$WrapperLogSegments = 2",
        "$WrapperLogEnvKeys = @()",
        helpers,
        quote_arg,
        "$script:rows = @()",
        "$script:caseName = $null",
        "function Write-Warning { param([string]$Message) throw $Message }",
        "function New-WrapperLogTargets($name, $nonce) { return $null }",
        "function Discard-PendingWrapperLogTargets($targets) {}",
        "function Proc-Start($id) { return '1' }",
        "function Start-WrapperProcess($startArgs) {",
        "  $script:rows += [pscustomobject]@{",
        "    name = $script:caseName",
        "    file = [string]$startArgs.FilePath",
        "    cwd = [string]$startArgs.WorkingDirectory",
        "    argument_line = [string]$startArgs.ArgumentList",
        "  }",
        "  return [pscustomobject]@{",
        "    Process = [pscustomobject]@{ Id = 4242 }",
        "    Redirected = $false",
        "  }",
        "}",
        launchers,
        f"$payload = ({_pslit(json.dumps(payload))} | ConvertFrom-Json)",
        "$cfg = [pscustomobject]@{ agents = [pscustomobject]@{",
        "  worker = [pscustomobject]@{ backend_profile = $null; env = $null }",
        "  manual = [pscustomobject]@{ backend_profile = $null; env = $null }",
        "} }",
        "$plan = [pscustomobject]@{",
        "  launch_mode = 'wrap'",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "  launch_admission = $payload.regular",
        "}",
        "$script:caseName = 'regular'",
        "$regularResult = Launch 'worker' $plan $null",
        "$spec = [pscustomobject]@{",
        "  env = $null",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "  launch_admission = $payload.ephemeral",
        "}",
        "$script:caseName = 'ephemeral'",
        "$ephemeralResult = Launch-Spec 'reviewer' $spec $null",
        "$manualPlan = [pscustomobject]@{",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "  launch_admission = $payload.manual",
        "}",
        "$script:caseName = 'manual'",
        "$manualResult = Launch 'manual' $manualPlan $null",
        "[pscustomobject]@{",
        "  rows = $script:rows",
        "  regular_returned = ($null -ne $regularResult)",
        "  ephemeral_returned = ($null -ne $ephemeralResult)",
        "  manual_returned = ($null -ne $manualResult)",
        "} | ConvertTo-Json -Depth 8 | "
        f"Set-Content {_pslit(str(output))} -Encoding utf8",
    ])
    script = tmp_path / "accepted-launch-admissions.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(output.read_text(encoding="utf-8-sig"))
    assert data["regular_returned"] is True
    assert data["ephemeral_returned"] is True
    assert data["manual_returned"] is True
    rows = {row["name"]: row for row in data["rows"]}
    for name, admitted_argv in (
        ("regular", regular_argv),
        ("ephemeral", ephemeral_argv),
    ):
        row = rows[name]
        assert row["file"] == "python.exe", row
        assert row["cwd"] == cwd, row
        serialized = row["argument_line"].split(" ")
        assert serialized[:3] == [
            "-m", "agenttalk", "--supervisor-launch-nonce",
        ], row
        assert re.fullmatch(r"[0-9a-f]{32}", serialized[3]), row
        expected = [token if token else '\"\"' for token in admitted_argv]
        assert serialized[4:] == expected[2:], row
    manual_row = rows["manual"]
    assert manual_row["file"] == "configured.exe"
    assert manual_row["cwd"] == cwd
    assert manual_row["argument_line"].split(" ") == ["alpha", '\"\"', "omega"]


@pytest.mark.parametrize(
    "shell",
    _windows_powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
def test_launchers_refuse_unusable_admission_before_environment_or_spawn(
    tmp_path: Path,
    shell: str | None,
) -> None:
    """Missing, old, refused, and malformed artifacts fail before effects."""
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    helpers, quote_arg, launchers = _launch_admission_runtime_blocks(tmp_path)
    cwd = str(tmp_path)
    launch_root = "R"
    accepted = {
        "schema_version": 1,
        "status": "accepted",
        "population": "regular",
        "agent": "worker",
        "cli": "codex",
        "wrapped": False,
        "windows_file": "configured.exe",
        "argv": ["configured", "launch"],
        "cwd": cwd,
        "module_args_from": None,
        "wrapper_dispatch": False,
        "nonce_insert_at": None,
        "child_argv": [],
    }
    invalid_cases = [
        {"label": "missing", "present": False, "admission": None},
        {
            "label": "old",
            "present": True,
            "admission": {**accepted, "schema_version": 0},
        },
        {
            "label": "refused",
            "present": True,
            "admission": {
                "schema_version": 1,
                "status": "refused",
                "population": "regular",
                "agent": "worker",
                "reason": "closed parser refused unknown option --mystery",
            },
        },
        {
            "label": "malformed",
            "present": True,
            "admission": {**accepted, "argv": "not-an-array"},
        },
    ]
    output = tmp_path / "refused-launch-admissions.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(launch_root)}",
        "$AgenttalkPython = 'python.exe'",
        "$SrcOnPyPath = $false",
        "$WrapperLogMaxBytes = 1024",
        "$WrapperLogSegments = 2",
        "$WrapperLogEnvKeys = @()",
        helpers,
        quote_arg,
        "$script:rows = @()",
        "$script:spawnCount = 0",
        "$script:logTargetCount = 0",
        "$script:envMutationCount = 0",
        "$script:warnings = @()",
        "function Write-Warning { param([string]$Message) "
        "$script:warnings += $Message }",
        "function Set-Item { param([string]$LiteralPath, [object]$Value) "
        "$script:envMutationCount += 1 }",
        "function New-WrapperLogTargets($name, $nonce) {",
        "  $script:logTargetCount += 1",
        "  return [pscustomobject]@{ stdout = 'stdout'; stderr = 'stderr' }",
        "}",
        "function Discard-PendingWrapperLogTargets($targets) {}",
        "function Proc-Start($id) { return '1' }",
        "function Start-WrapperProcess($startArgs) {",
        "  $script:spawnCount += 1",
        "  return [pscustomobject]@{",
        "    Process = [pscustomobject]@{ Id = 4242 }",
        "    Redirected = $false",
        "  }",
        "}",
        launchers,
        f"$cases = ({_pslit(json.dumps(invalid_cases))} | ConvertFrom-Json)",
        "foreach ($population in @('regular','ephemeral')) {",
        "  foreach ($case in @($cases)) {",
        "    $script:spawnCount = 0",
        "    $script:logTargetCount = 0",
        "    $script:envMutationCount = 0",
        "    $script:warnings = @()",
        "    if ($population -ceq 'regular') {",
        "      $cfg = [pscustomobject]@{ agents = [pscustomobject]@{ worker = "
        "[pscustomobject]@{",
        "        backend_profile = $null; cli = 'codex'; wrapped = $false;",
        "        cwd = $Root; env = $null;",
        "        launch = [pscustomobject]@{ windows_file = 'configured.exe'; "
        "windows_args = @('configured','launch') }",
        "      } } }",
        "      $carrier = [pscustomobject]@{ launch_mode = 'fresh'; "
        "window_style = 'Hidden'; window_style_warning = $null; "
        "session_id = $null; session_args = @() }",
        "      if ($case.present) { $carrier | Add-Member launch_admission "
        "$case.admission -Force }",
        "      $result = Launch 'worker' $carrier $null",
        "    } else {",
        "      $carrier = [pscustomobject]@{",
        "        cli = 'codex'; cwd = $Root; env = $null;",
        "        window_style = 'Hidden'; window_style_warning = $null;",
        "        launch = [pscustomobject]@{",
        "          windows_file = 'python.exe';",
        "          windows_args = @('-m','agenttalk','--root',$Root,'wrap',",
        "            '--for','reviewer','--cli','codex','--loop','--one-shot',",
        "            '--to-request','R1','--','codex.exe')",
        "        }",
        "      }",
        "      if ($case.present) {",
        "        $ephemeralAdmission = $case.admission | Select-Object *",
        "        $ephemeralAdmission.population = 'ephemeral'",
        "        $ephemeralAdmission.agent = 'reviewer'",
        "        $carrier | Add-Member launch_admission $ephemeralAdmission -Force",
        "      }",
        "      $result = Launch-Spec 'reviewer' $carrier $null",
        "    }",
        "    $script:rows += [pscustomobject]@{",
        "      population = $population",
        "      label = $case.label",
        "      returned = ($null -ne $result)",
        "      spawn_count = $script:spawnCount",
        "      log_target_count = $script:logTargetCount",
        "      env_mutation_count = $script:envMutationCount",
        "      warnings = @($script:warnings)",
        "    }",
        "  }",
        "}",
        "$script:rows | ConvertTo-Json -Depth 8 | "
        f"Set-Content {_pslit(str(output))} -Encoding utf8",
    ])
    script = tmp_path / "refused-launch-admissions.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    rows = json.loads(output.read_text(encoding="utf-8-sig"))
    assert len(rows) == 8
    for row in rows:
        assert row["returned"] is False, row
        assert row["spawn_count"] == 0, row
        assert row["log_target_count"] == 0, row
        assert row["env_mutation_count"] == 0, row
        warnings = "\n".join(row["warnings"])
        assert "launch admission" in warnings.casefold(), row
        assert "not launching" in warnings.casefold(), row
        if row["label"] == "refused":
            assert "closed parser refused unknown option --mystery" in warnings, row


@pytest.mark.parametrize(
    "shell",
    _windows_powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
def test_launch_admission_resolver_rejects_type_coercion_and_shape_drift(
    tmp_path: Path,
    shell: str | None,
) -> None:
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    helpers = _exec_helpers(tmp_path)
    accepted = _accepted_launch_artifact(
        population="regular", agent="worker", cli="codex",
        windows_file="configured.exe", argv=["alpha", "beta"],
        cwd=str(tmp_path), wrapped=False,
    )
    refused = {
        "schema_version": 1,
        "status": "refused",
        "population": "regular",
        "agent": "worker",
        "reason": "named refusal",
    }
    cases = {
        "boolean-schema": {**accepted, "schema_version": True},
        "numeric-status": {**accepted, "status": 7},
        "numeric-population": {**accepted, "population": 7},
        "numeric-agent": {**accepted, "agent": 7},
        "numeric-cli": {**accepted, "cli": 7},
        "numeric-executable": {**accepted, "windows_file": 7},
        "numeric-cwd": {**accepted, "cwd": 7},
        "numeric-refusal-reason": {**refused, "reason": 7},
        "manual-module-index": {**accepted, "module_args_from": 0},
        "extra-property": {**accepted, "unexpected": True},
        "missing-property": {
            key: value
            for key, value in accepted.items()
            if key != "child_argv"
        },
    }
    out = tmp_path / "launch-admission-types.json"
    script = tmp_path / "launch-admission-types.ps1"
    script.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            helpers,
            "$script:warnings = @()",
            "function Write-Warning { param([string]$Message) "
            "$script:warnings += $Message }",
            f"$cases = ({_pslit(json.dumps(cases))} | ConvertFrom-Json)",
            "$rows = @()",
            "foreach ($property in $cases.PSObject.Properties) {",
            "  $script:warnings = @()",
            "  $result = Resolve-LaunchAdmission $property.Value 'worker' 'regular'",
            "  $rows += [pscustomobject]@{",
            "    name = $property.Name",
            "    accepted = ($null -ne $result)",
            "    warnings = @($script:warnings)",
            "  }",
            "}",
            "$rows | ConvertTo-Json -Depth 5 | ",
            f"  Set-Content {_pslit(str(out))} -Encoding utf8",
        ]),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    rows = json.loads(out.read_text(encoding="utf-8-sig"))
    assert {row["name"] for row in rows} == set(cases)
    for row in rows:
        assert row["accepted"] is False, row
        assert "not launching" in "\n".join(row["warnings"]).casefold(), row


def test_supervisor_wrapper_logging_is_driven_by_typed_admission(
    tmp_path: Path,
) -> None:
    """Only an accepted wrapper-dispatch artifact receives bounded logs."""
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    launchers = sup.PS_TEMPLATE[
        sup.PS_TEMPLATE.index("function Launch($name"):
        sup.PS_TEMPLATE.index("# Console action log")
    ]
    root = str(tmp_path)
    wrapped_argv = [
        "-m", "agenttalk", "--root", root, "wrap", "--for", "worker",
        "--cli", "codex", "--loop", "--", "codex.exe",
    ]
    regular = _accepted_launch_artifact(
        population="regular", agent="worker", cli="codex",
        windows_file="python.exe", argv=wrapped_argv, cwd=root,
        wrapped=True, module_args_from=0, nonce_insert_at=2,
        child_argv=["codex.exe"],
    )
    manual = _accepted_launch_artifact(
        population="regular", agent="manual", cli="codex",
        windows_file="codex.exe", argv=["exec", "review"], cwd=root,
        wrapped=False,
    )
    ephemeral = _accepted_launch_artifact(
        population="ephemeral", agent="reviewer", cli="codex",
        windows_file="python.exe",
        argv=[
            "-m", "agenttalk", "--root", root, "wrap", "--for", "reviewer",
            "--cli", "codex", "--from", "reviewer", "--loop", "--one-shot",
            "--to-request", "R1", "--", "codex.exe",
        ],
        cwd=root, wrapped=True, module_args_from=0, nonce_insert_at=2,
        child_argv=["codex.exe"],
    )
    refused = {
        "schema_version": 1,
        "status": "refused",
        "population": "ephemeral",
        "agent": "blocked",
        "reason": "closed parser refused an unknown wrapper option",
    }
    payload = {
        "regular": regular,
        "manual": manual,
        "ephemeral": ephemeral,
        "refused": refused,
    }
    out = tmp_path / "wrapper-logging-admission.json"
    harness = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(root)}",
        "$AgenttalkPython = 'python.exe'",
        "$SrcOnPyPath = $false",
        f"$WrapperLogMaxBytes = {sup.WRAPPER_LOG_MAX_BYTES}",
        f"$WrapperLogSegments = {sup.WRAPPER_LOG_SEGMENT_COUNT}",
        "$WrapperLogEnvKeys = @(",
        "  'AGENTTALK_WRAPPER_STDOUT_LOG',",
        "  'AGENTTALK_WRAPPER_STDERR_LOG',",
        "  'AGENTTALK_WRAPPER_LOG_MAX_BYTES',",
        "  'AGENTTALK_WRAPPER_LOG_SEGMENTS',",
        "  'AGENTTALK_WRAPPER_LOG_NONCE')",
        helpers,
        "$script:rows = @()",
        "$script:caseName = $null",
        "$script:targetCount = 0",
        "$script:warnings = @()",
        "function Write-Warning { param([string]$Message) "
        "$script:warnings += $Message }",
        "function New-WrapperLogTargets($name, $nonce) {",
        "  $script:targetCount += 1",
        "  return [pscustomobject]@{",
        f"    stdout = {_pslit(str(tmp_path / 'stdout.log'))}",
        f"    stderr = {_pslit(str(tmp_path / 'stderr.log'))}",
        f"    generation_dir = {_pslit(str(tmp_path / 'generation'))}",
        "    agent_name = $name",
        "  }",
        "}",
        "function Discard-PendingWrapperLogTargets($targets) {}",
        "function Proc-Start($id) { return '1' }",
        "function Quote-Arg([string]$arg) { return $arg }",
        "function Start-WrapperProcess($startArgs) {",
        "  $script:rows += [pscustomobject]@{",
        "    name = $script:caseName",
        "    stdout_redirect = $startArgs.ContainsKey('RedirectStandardOutput')",
        "    stderr_redirect = $startArgs.ContainsKey('RedirectStandardError')",
        "    nonce = [Environment]::GetEnvironmentVariable(",
        "      'AGENTTALK_WRAPPER_LOG_NONCE')",
        "  }",
        "  return [pscustomobject]@{",
        "    Process = [pscustomobject]@{ Id = 4242 }",
        "    Redirected = $startArgs.ContainsKey('RedirectStandardOutput')",
        "  }",
        "}",
        launchers,
        f"$payload = ({_pslit(json.dumps(payload))} | ConvertFrom-Json)",
        "$cfg = [pscustomobject]@{ agents = [pscustomobject]@{",
        "  worker = [pscustomobject]@{ backend_profile = $null; env = $null }",
        "  manual = [pscustomobject]@{ backend_profile = $null; env = $null }",
        "} }",
        "$script:caseName = 'regular'",
        "$null = Launch 'worker' ([pscustomobject]@{",
        "  launch_admission = $payload.regular",
        "  launch_mode = 'wrap'",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "}) $null",
        "$script:caseName = 'manual'",
        "$null = Launch 'manual' ([pscustomobject]@{",
        "  launch_admission = $payload.manual",
        "  launch_mode = 'fresh'",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "}) $null",
        "$script:caseName = 'ephemeral'",
        "$null = Launch-Spec 'reviewer' ([pscustomobject]@{",
        "  launch_admission = $payload.ephemeral",
        "  env = $null",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "}) $null",
        "$script:caseName = 'refused'",
        "$null = Launch-Spec 'blocked' ([pscustomobject]@{",
        "  launch_admission = $payload.refused",
        "  env = $null",
        "  window_style = 'Hidden'",
        "  window_style_warning = $null",
        "}) $null",
        "[pscustomobject]@{",
        "  rows = $script:rows",
        "  target_count = $script:targetCount",
        "  warnings = $script:warnings",
        "} | ConvertTo-Json -Depth 6 | ",
        f"  Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    script = tmp_path / "wrapper-logging-admission.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    rows = {row["name"]: row for row in data["rows"]}
    assert set(rows) == {"regular", "manual", "ephemeral"}
    assert rows["regular"]["stdout_redirect"] is True
    assert rows["regular"]["stderr_redirect"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", rows["regular"]["nonce"])
    assert rows["ephemeral"]["stdout_redirect"] is True
    assert rows["ephemeral"]["stderr_redirect"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", rows["ephemeral"]["nonce"])
    assert rows["manual"]["stdout_redirect"] is False
    assert rows["manual"]["stderr_redirect"] is False
    assert rows["manual"]["nonce"] is None
    assert data["target_count"] == 2
    warnings = "\n".join(data["warnings"])
    assert "closed parser refused an unknown wrapper option" in warnings


def test_ps_start_wrapper_process_fallback_strips_logging_env_vars(
    tmp_path: Path,
) -> None:
    """Cold-review finding on a51639d..ee177af, rated HIGH: Launch applies the
    wrapper-log capability env vars (AGENTTALK_WRAPPER_STDOUT_LOG etc. + the
    nonce) to the process environment BEFORE calling Start-WrapperProcess -
    they must be present ahead of time for a successfully-redirected launch
    to inherit them. But when the exact-handle launcher fails (Constrained
    Language Mode, a missing C# compiler, Start-Process shadowed by a
    non-Cmdlet proxy) and Start-WrapperProcess falls back to an unredirected
    Start-Process, those env vars used to survive into that child untouched.
    The child would then authenticate against
    wrapper_logs._authenticated_environment() and install BoundedStreamTee
    over its INHERITED CONSOLE instead of a redirected file - forwarding is
    capped at a few hundred KiB, so the operator's visible window goes
    silent forever once that budget is spent, while the record still looks
    healthy otherwise.

    Fixed structurally in Start-WrapperProcess itself - the ONE place that
    knows redirection did not happen - rather than requiring every caller
    (Launch, Launch-Spec) to remember to clear the keys themselves."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    result_path = tmp_path / "fallback-env-strip.json"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        helpers,
        # Force the exact-handle path unavailable, exactly like Constrained
        # Language Mode or a missing C# compiler would.
        "function Initialize-WrapperLogLauncherType { return $false }",
        "$script:capturedEnv = $null",
        "$script:capturedRedirected = $null",
        "function Start-Process {",
        "  param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle,",
        "        [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)",
        "  $script:capturedEnv = @{}",
        "  foreach ($k in $WrapperLogEnvKeys) {",
        "    $script:capturedEnv[$k] = [Environment]::GetEnvironmentVariable($k)",
        "  }",
        "  $script:capturedRedirected = (",
        "    [bool]$RedirectStandardOutput -or [bool]$RedirectStandardError)",
        "  return [pscustomobject]@{ Id = 4242 }",
        "}",
        "$WrapperLogEnvKeys = @("
        "'AGENTTALK_WRAPPER_STDOUT_LOG','AGENTTALK_WRAPPER_STDERR_LOG',"
        "'AGENTTALK_WRAPPER_LOG_MAX_BYTES','AGENTTALK_WRAPPER_LOG_SEGMENTS',"
        "'AGENTTALK_WRAPPER_LOG_NONCE')",
        # Simulate what Launch already did before calling here: apply the
        # capability env vars to THIS process's environment.
        f"$env:AGENTTALK_WRAPPER_STDOUT_LOG = {_pslit(str(tmp_path / 'stdout.log'))}",
        f"$env:AGENTTALK_WRAPPER_STDERR_LOG = {_pslit(str(tmp_path / 'stderr.log'))}",
        "$env:AGENTTALK_WRAPPER_LOG_MAX_BYTES = '1048576'",
        "$env:AGENTTALK_WRAPPER_LOG_SEGMENTS = '4'",
        "$env:AGENTTALK_WRAPPER_LOG_NONCE = ('a' * 32)",
        "$startArgs = @{ FilePath = 'fake.exe'; "
        f"WorkingDirectory = {_pslit(str(tmp_path))}; "
        "WindowStyle = 'Hidden'; PassThru = $true; "
        f"RedirectStandardOutput = {_pslit(str(tmp_path / 'stdout.log'))}; "
        f"RedirectStandardError = {_pslit(str(tmp_path / 'stderr.log'))} }}",
        "$launch = Start-WrapperProcess $startArgs",
        "@{ capturedEnv = $script:capturedEnv; "
        "capturedRedirected = $script:capturedRedirected; "
        "procId = $launch.Process.Id; "
        "reportedRedirected = [bool]$launch.Redirected } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "fallback-env-strip.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["procId"] == 4242
    assert payload["capturedRedirected"] is False, (
        "the fallback launch still passed redirection to Start-Process"
    )
    assert payload["reportedRedirected"] is False, (
        "Start-WrapperProcess reported Redirected=true for a fallback launch "
        "- callers rely on this to decide whether to commit or discard"
    )
    captured = payload["capturedEnv"]
    for key in (
        "AGENTTALK_WRAPPER_STDOUT_LOG",
        "AGENTTALK_WRAPPER_STDERR_LOG",
        "AGENTTALK_WRAPPER_LOG_MAX_BYTES",
        "AGENTTALK_WRAPPER_LOG_SEGMENTS",
        "AGENTTALK_WRAPPER_LOG_NONCE",
    ):
        assert captured.get(key) is None, (
            f"{key} survived into the unredirected fallback child - it would "
            "authenticate and install the bounded tee over the inherited "
            "console instead of a redirected file"
        )


def test_ps_start_wrapper_process_reports_unredirected_when_both_sides_degrade(
    tmp_path: Path,
) -> None:
    """I1, 2nd leak (PR 98 connector re-review of fccb376): OpenOutputOrNull
    substitutes NUL for EITHER side independently and never throws, so
    ::Start returns normally even when BOTH stdout and stderr degraded to
    NUL - the caller only checked whether ::Start THREW, not what it
    actually opened, so Redirected was unconditionally true. Drive the
    REAL exact-handle launcher (not stubbed) with both target paths inside
    a directory that is never created, forcing CreateFile to fail for both
    sides exactly like a stranded/unavailable log root would."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    result_path = tmp_path / "both-degraded.json"
    missing_dir = tmp_path / "no-such-directory"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        helpers,
        "$startArgs = @{ FilePath = "
        f"{_pslit(sys.executable)}; ArgumentList = "
        f"{_pslit(subprocess.list2cmdline(['-c', 'pass']))}; "
        "WorkingDirectory = "
        f"{_pslit(str(tmp_path))}; PassThru = $true; "
        "RedirectStandardOutput = "
        f"{_pslit(str(missing_dir / 'stdout.log'))}; "
        "RedirectStandardError = "
        f"{_pslit(str(missing_dir / 'stderr.log'))} }}",
        "$launch = Start-WrapperProcess $startArgs",
        "$launch.Process.WaitForExit()",
        "@{ procId = $launch.Process.Id; "
        "reportedRedirected = [bool]$launch.Redirected } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "both-degraded.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert not missing_dir.exists(), "the missing directory must never get created"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["procId"]
    assert payload["reportedRedirected"] is False, (
        "both stdout and stderr degraded to NUL - no inherited stream points at "
        "either advertised file, so Redirected must be false or the caller "
        "commits an empty generation as real evidence"
    )


def test_ps_start_wrapper_process_reports_redirected_when_one_side_degrades(
    tmp_path: Path,
) -> None:
    """Companion to the both-degraded case above: when only ONE side
    degrades to NUL, the OTHER side is still a genuine inherited stream
    pointed at its advertised file - real, if partial, evidence. Per the
    stated invariant ("committable iff at least one advertised base log is
    genuinely pointed at"), Redirected stays true and the generation is
    still committed. Documented here rather than left implicit: a
    one-side-degraded generation DOES commit and DOES participate in
    retention like any other completed generation - only the both-degraded
    (zero real streams) case is refused."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    result_path = tmp_path / "one-side-degraded.json"
    missing_dir = tmp_path / "no-such-directory"
    good_stdout = tmp_path / "stdout.log"
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        helpers,
        "$startArgs = @{ FilePath = "
        f"{_pslit(sys.executable)}; ArgumentList = "
        f"{_pslit(subprocess.list2cmdline(['-c', 'pass']))}; "
        "WorkingDirectory = "
        f"{_pslit(str(tmp_path))}; PassThru = $true; "
        "RedirectStandardOutput = "
        f"{_pslit(str(good_stdout))}; "
        "RedirectStandardError = "
        f"{_pslit(str(missing_dir / 'stderr.log'))} }}",
        "$launch = Start-WrapperProcess $startArgs",
        "$launch.Process.WaitForExit()",
        "@{ procId = $launch.Process.Id; "
        "reportedRedirected = [bool]$launch.Redirected } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "one-side-degraded.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert not missing_dir.exists(), "the missing directory must never get created"
    assert good_stdout.exists(), "the genuinely-open side must actually be written"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["procId"]
    assert payload["reportedRedirected"] is True, (
        "one side is a genuine inherited stream - it is real evidence, so the "
        "generation still commits"
    )


def test_ps_start_wrapper_process_strips_env_before_both_degraded_fallback(
    tmp_path: Path,
) -> None:
    """I1, 2nd leak (PR 98 connector re-review of 0c3179d): Redirected was a
    correct predicate computed too LATE - by the time it returned false,
    ::Start had already CreateProcess+ResumeThread'd the child with the
    caller's already-applied wrapper-log capability env vars (nonce,
    AGENTTALK_WRAPPER_STDOUT_LOG/STDERR_LOG) inherited regardless of which
    handles it actually got. A child that inherits those authenticates
    and has BoundedStreamTee recreate the generation directory on its own,
    even after the caller discards the pending record - a markerless
    directory retention preserves forever.

    Fixed at the source: ::Start now throws BEFORE CreateProcess when both
    sides degrade, routing through the SAME catch that an exact-handle
    failure already uses - which strips the env vars before falling back.
    Drive the REAL exact-handle launcher (not stubbed) with both target
    paths inside a directory that is never created, and confirm the env
    vars a would-be child inherits are ALREADY stripped by the time the
    fallback runs - not just that Redirected ends up false."""
    # NOTE: this must NOT shadow Start-Process with a PowerShell function
    # (unlike the fallback-strip test above) - Start-WrapperProcess gates
    # the exact-handle attempt on `Get-Command Start-Process).CommandType
    # -eq 'Cmdlet'`, so shadowing it would skip the real ::Start call
    # entirely and always take the already-unavailable branch, passing
    # regardless of whether the fix under test exists. Instead, let the
    # REAL fallback Start-Process run for real (a harmless `-c pass`), and
    # check THIS process's own environment afterward - the fallback path
    # mutates it in place before spawning, so it is directly observable
    # without intercepting the spawn call.
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    helpers = ps[
        ps.index("# region wrapper-log-helpers"):
        ps.index("# endregion wrapper-log-helpers")
    ]
    result_path = tmp_path / "both-degraded-env-strip.json"
    missing_dir = tmp_path / "no-such-directory"
    env_keys = [
        "AGENTTALK_WRAPPER_STDOUT_LOG", "AGENTTALK_WRAPPER_STDERR_LOG",
        "AGENTTALK_WRAPPER_LOG_MAX_BYTES", "AGENTTALK_WRAPPER_LOG_SEGMENTS",
        "AGENTTALK_WRAPPER_LOG_NONCE",
    ]
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        helpers,
        "$WrapperLogEnvKeys = @(" + ",".join(_pslit(k) for k in env_keys) + ")",
        # Simulate what Launch already did before calling here: apply the
        # capability env vars to THIS process's environment.
        f"$env:AGENTTALK_WRAPPER_STDOUT_LOG = {_pslit(str(tmp_path / 'advertised-stdout.log'))}",
        f"$env:AGENTTALK_WRAPPER_STDERR_LOG = {_pslit(str(tmp_path / 'advertised-stderr.log'))}",
        "$env:AGENTTALK_WRAPPER_LOG_MAX_BYTES = '1048576'",
        "$env:AGENTTALK_WRAPPER_LOG_SEGMENTS = '4'",
        "$env:AGENTTALK_WRAPPER_LOG_NONCE = ('a' * 32)",
        # A REAL, launchable executable - not a nonexistent filename - so
        # the only thing that can make ::Start throw is the both-degraded
        # check itself, not an unrelated CreateProcess failure.
        "$startArgs = @{ FilePath = "
        f"{_pslit(sys.executable)}; ArgumentList = "
        f"{_pslit(subprocess.list2cmdline(['-c', 'pass']))}; "
        "WorkingDirectory = "
        f"{_pslit(str(tmp_path))}; "
        "WindowStyle = 'Hidden'; PassThru = $true; "
        "RedirectStandardOutput = "
        f"{_pslit(str(missing_dir / 'stdout.log'))}; "
        "RedirectStandardError = "
        f"{_pslit(str(missing_dir / 'stderr.log'))} }}",
        "$launch = Start-WrapperProcess $startArgs",
        "$launch.Process.WaitForExit()",
        "$strippedEnv = @{}",
        "foreach ($k in $WrapperLogEnvKeys) { "
        "$strippedEnv[$k] = [Environment]::GetEnvironmentVariable($k) }",
        "@{ procId = $launch.Process.Id; "
        "reportedRedirected = [bool]$launch.Redirected; "
        "strippedEnv = $strippedEnv } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "both-degraded-env-strip.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert not missing_dir.exists(), "the missing directory must never get created"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["procId"]
    assert payload["reportedRedirected"] is False
    stripped = payload["strippedEnv"]
    for key in env_keys:
        assert stripped.get(key) is None, (
            f"{key} survived into the both-degraded fallback child - it would "
            "authenticate and have BoundedStreamTee recreate the discarded "
            "generation directory on its own"
        )


def test_ps_launch_discards_targets_when_fallback_is_unredirected(
    tmp_path: Path,
) -> None:
    """I1's third face (PR 98 cold review): Launch's post-launch check only
    looked for a PID, not whether redirection actually happened, so a
    fallback (unredirected) launch still committed its generation as real
    evidence - an empty, never-written-to directory that participates in
    future retention decisions and can outrank (and evict) a genuinely
    real generation. Launch must discard the pending generation instead of
    completing it whenever Start-WrapperProcess reports Redirected=false."""
    shell = _pick_powershell()
    if not shell:
        return
    ps = sup.PS_TEMPLATE
    # One contiguous slice: typed admission helpers through Launch/Launch-Spec,
    # before the console-action region starts.
    helpers = ps[
        ps.index("# region exec-helpers"):
        ps.index("# Console action log")
    ]
    log_root = tmp_path / "logs"
    agent_leaf = _wrapper_log_agent_dir("worker")
    result_path = tmp_path / "fallback-commit.json"
    launch_argv = [
        "-m", "agenttalk", "--root", str(tmp_path), "wrap", "--for",
        "worker", "--cli", "codex", "--loop", "--", "codex.exe",
    ]
    admission = _accepted_launch_artifact(
        population="regular", agent="worker", cli="codex",
        windows_file="python.exe", argv=launch_argv, cwd=str(tmp_path),
        wrapped=True, module_args_from=0, nonce_insert_at=2,
        child_argv=["codex.exe"],
    )
    rows: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$Root = {_pslit(str(tmp_path))}",
        "$AgenttalkPython = 'python.exe'",
        "$SrcOnPyPath = $false",
        f"$WrapperLogRoot = {_pslit(str(log_root))}",
        f"$WrapperLogFallbackRoot = {_pslit(str(tmp_path / 'fallback'))}",
        f"$WrapperLogGenerations = {sup.WRAPPER_LOG_GENERATIONS}",
        f"$WrapperLogMaxBytes = {sup.WRAPPER_LOG_MAX_BYTES}",
        f"$WrapperLogSegments = {sup.WRAPPER_LOG_SEGMENT_COUNT}",
        helpers,
        # Force the exact-handle launcher unavailable, so Start-WrapperProcess
        # falls back to a real, unredirected Start-Process (faked here only
        # to avoid an actual spawn).
        "function Initialize-WrapperLogLauncherType { return $false }",
        "function Start-Process {",
        "  param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle,",
        "        [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)",
        "  return [pscustomobject]@{ Id = 4242 }",
        "}",
        "function Proc-Start($id) { return '1' }",
        "function Quote-Arg([string]$arg) { return $arg }",
        "function Assert-ActionsEnabled([string]$what) { return $true }",
        "$agent = [pscustomobject]@{ backend_profile = $null; wrapped = $true; "
        "cwd = $Root; env = $null; launch = [pscustomobject]@{ "
        "windows_file = 'python.exe'; windows_args = @('-m','agenttalk','wrap',"
        "'--for','worker','--loop','--','codex.exe') } }",
        "$cfg = [pscustomobject]@{ agents = [pscustomobject]@{ worker = $agent } }",
        f"$admission = ({_pslit(json.dumps(admission))} | ConvertFrom-Json)",
        "$plan = [pscustomobject]@{ launch_mode = 'wrap'; window_style = 'Hidden'; "
        "window_style_warning = $null; session_id = $null; session_args = @(); "
        "launch_admission = $admission }",
        "$out = Launch 'worker' $plan $null",
        f"$agentDir = Join-Path {_pslit(str(log_root))} {_pslit(agent_leaf)}",
        "$dirs = if (Test-Path -LiteralPath $agentDir) { "
        "@(Get-ChildItem -LiteralPath $agentDir -Directory) } else { @() }",
        "$committed = @($dirs | Where-Object { "
        "Test-Path -LiteralPath (Join-Path $_.FullName '.committed') })",
        "@{ pid = $out.pid; dirCount = $dirs.Count; committedCount = $committed.Count } | "
        f"ConvertTo-Json | Set-Content {_pslit(str(result_path))} -Encoding utf8",
    ]
    script = tmp_path / "fallback-commit.ps1"
    script.write_text("\n".join(rows), encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["pid"] == 4242
    assert payload["committedCount"] == 0, (
        "an empty generation was committed as real evidence for a fallback "
        "launch that never actually redirected into it"
    )
    assert payload["dirCount"] == 0, (
        "the discarded pending generation directory was left behind on disk"
    )


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
    admission_helpers = text[
        text.index("# region launch-admission-helpers"):start
    ]
    admission = _accepted_launch_artifact(
        population="regular", agent="worker", cli="codex",
        windows_file="dummy.exe", argv=[], cwd=str(tmp_path), wrapped=False,
    )
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
        admission_helpers,
        launch_fn,
        f"$admission = ({_pslit(json.dumps(admission))} | ConvertFrom-Json)",
        "$plan = [pscustomobject]@{ session_id = $null; session_args = @(); "
        "launch_mode = 'fresh'; launch_admission = $admission }",
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
# A wrapped agent is supervised THROUGH `agenttalk wrap --loop`: the Python
# wrapper is the long-lived root, while wrapper-runtime.json identifies each
# per-turn CLI launcher. The supervisor independently discovers the real CLI
# brain (including a Codex TUI after its launcher exits) and retains the
# start-guarded managed tree for recovery. Session continuity is owned by the
# wrapper, so the supervisor injects no session args.

WRAP_LAUNCHER_PID, WRAP_CHILD_PID, WRAP_TUI_PID = 300, 301, 302

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
             f"--root {root} wrap --for worker --cli {cli} --loop -- {name}"
         ),
         "start_time": WRAP_START,
         "start_filetime": _ps_filetime(500000)},
        {"pid": child_pid, "parent_pid": launcher_pid, "name": name,
         "command_line": f"{name} exec --json", "start_time": child_start,
         "start_filetime": _test_start_filetime(child_start)},
    ]


class _CertifiedForkSnapshot(list):
    launcher_lifetime = {
        "source": wrt.LAUNCHER_LIFETIME_SOURCE,
        "creation_filetime": _ps_filetime(600000),
        "exit_filetime": _ps_filetime(750000),
    }


def _codex_forked_brain_snap() -> list[dict]:
    wrapper = _wrap_snap()[0]
    return _CertifiedForkSnapshot([
        wrapper,
        {
            "pid": WRAP_TUI_PID,
            "parent_pid": WRAP_CHILD_PID,
            "name": "codex.exe",
            "command_line": "codex tui",
            "start_time": _ps_iso(700000),
            "start_filetime": _ps_filetime(700000),
        },
    ])


def _wrap_ready(**over) -> dict:
    st = {"launcher_pid": WRAP_LAUNCHER_PID, "launcher_start": WRAP_START,
          "launcher_nonce": SUPERVISOR_NONCE,
          "launcher_nonce_injected": True,
          "launcher_nonce_source": "agenttalk_global_arg",
          "runtime_wrapper_generation": "wrapper-1",
          "owned_process_tree_pending": True,
          "readiness_seen": True, "launching": False,
          "last_launch_epoch": NOW - 1000}
    st.update(over)
    return st


def _wrapper_runtime_view(
    *,
    phase: str = "idle",
    now: float = NOW,
    updated_age: float = 1.0,
    progress_age: float | None = None,
    outcome: str | None = None,
    wrapper_pid: int = WRAP_LAUNCHER_PID,
    wrapper_start: str = WRAP_START,
    wrapper_generation: str = "wrapper-1",
    turn_generation: int = 1,
    progress_sequence: int = 0,
    launcher_pid: int | None = None,
    launcher_start: str | None = None,
    launcher_creation_filetime: str | None = None,
    launcher_exit_filetime: str | None = None,
) -> dict:
    active_turn = phase != "idle"
    if phase == "active":
        launcher_pid = WRAP_CHILD_PID if launcher_pid is None else launcher_pid
        launcher_start = WRAP_CHILD_START if launcher_start is None else launcher_start
    if phase == "terminal" and launcher_pid is None:
        launcher_pid = WRAP_CHILD_PID
        launcher_start = WRAP_CHILD_START
    if progress_age is not None:
        progress_sequence = max(1, progress_sequence)
    lifetime = None
    if launcher_exit_filetime is not None:
        lifetime = {
            "source": wrt.LAUNCHER_LIFETIME_SOURCE,
            "creation_filetime": (
                launcher_creation_filetime or _ps_filetime(600000)
            ),
            "exit_filetime": launcher_exit_filetime,
        }
    record = {
        "schema_version": 2 if lifetime is not None else 1,
        "agent": "worker",
        "wrapper_pid": wrapper_pid,
        "wrapper_start": wrapper_start,
        "wrapper_generation": wrapper_generation,
        "phase": phase,
        "turn_generation": turn_generation,
        "turn_id": f"turn-{turn_generation}" if active_turn else None,
        "message_id": "msg-runtime" if active_turn else None,
        "cli_launcher_pid": launcher_pid if active_turn else None,
        "cli_launcher_start": launcher_start if active_turn else None,
        "progress_sequence": progress_sequence,
        "last_progress_at": (
            _iso(now - progress_age) if progress_age is not None else None
        ),
        "last_outcome": outcome,
        "updated_at": _iso(now - updated_age),
    }
    if lifetime is not None:
        record["cli_launcher_lifetime"] = lifetime
    return {
        "status": "valid",
        "record": record,
        "updated_age_seconds": updated_age,
        "progress_age_seconds": progress_age,
    }


def _plan_wrap(report, state, *, now=NOW, snapshot=None, config=_WRAP_CONFIG):
    report = dict(report)
    agents = dict(report.get("agents") or {})
    worker = dict(agents.get("worker") or {})
    worker.setdefault("wrapper_runtime", _wrapper_runtime_view(now=now))
    lifetime = getattr(snapshot, "launcher_lifetime", None)
    runtime_view = worker.get("wrapper_runtime")
    runtime_record = (
        runtime_view.get("record")
        if isinstance(runtime_view, dict)
        and isinstance(runtime_view.get("record"), dict)
        else None
    )
    if isinstance(lifetime, dict) and isinstance(runtime_record, dict):
        runtime_view = dict(runtime_view)
        runtime_record = dict(runtime_record)
        runtime_record["schema_version"] = 2
        runtime_record["cli_launcher_lifetime"] = dict(lifetime)
        runtime_view["record"] = runtime_record
        worker["wrapper_runtime"] = runtime_view
    agents["worker"] = worker
    report["agents"] = agents
    snap = [] if snapshot is None else snapshot
    return sup.plan_actions(report, state, config, now_epoch=now,
                            snapshot=snap)["agents"]["worker"]


@pytest.mark.parametrize(
    ("config", "report"),
    [
        (
            {
                "agents": {
                    "worker": {
                        "auto_restart": False,
                        "cli": "codex",
                        "wrapped": True,
                    }
                }
            },
            _report(),
        ),
        (
            {
                "agents": {
                    "worker": {
                        "auto_restart": True,
                        "cli": "codex",
                        "wrapped": True,
                    }
                }
            },
            {"agents": {}},
        ),
    ],
    ids=["auto-restart-disabled", "report-missing"],
)
def test_plan_ignores_unsupervised_or_unreported_agents(
    config: dict,
    report: dict,
) -> None:
    plan = sup.plan_actions(
        report,
        {"agents": {}},
        config,
        now_epoch=NOW,
        snapshot=[],
    )
    assert plan["agents"] == {}


def test_wrapped_liveness_requires_runtime_before_discovering_brain() -> None:
    snap = _wrap_snap()
    lv = sup._liveness(snap, _wrap_ready(), {"cli": "codex", "wrapped": True},
                       "worker", NOW, root_key=sup._root_key(TEST_ROOT))
    assert lv["brain_pid"] is None and lv["brain_start"] is None
    assert lv["discovered_brain"] is False
    assert lv["runtime_status"] == wrt.STATUS_ABSENT
    # A command-line/ancestry match without the strict runtime generation is
    # observation only and grants no wrapped teardown authority.
    assert lv["managed_pids"] == []
    assert lv["kill_targets"] == []
    # CONTRAST: the SAME snapshot, NOT wrapped, discovers that codex.exe child as
    # the brain - proving the wrapped path is what suppresses it.
    lv2 = sup._liveness(snap, _wrap_ready(), {"cli": "codex"}, "worker", NOW)
    assert lv2["brain_pid"] == WRAP_CHILD_PID and lv2["discovered_brain"] is True


def test_wrapped_liveness_accepts_normalized_v1_runtime_view() -> None:
    raw_view = _wrapper_runtime_view(
        phase="active",
        progress_age=1.0,
        progress_sequence=2,
    )
    normalized = wrt.validate_record(
        raw_view["record"],
        expected_agent="worker",
        now_epoch=NOW,
    )
    normalized.pop("_updated_epoch")
    normalized.pop("_last_progress_epoch")
    raw_view["record"] = normalized

    plan = _plan_wrap(
        _report(heartbeat_stale=False, wrapper_runtime=raw_view),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap(),
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_UNKNOWN"
    assert plan["next_state"]["owned_process_tree"]["status"] == "complete"
    assert plan["next_state"]["owned_process_tree"]["entries"][1]["pid"] == (
        WRAP_CHILD_PID
    )


def test_wrapped_idle_accepts_bounded_concurrent_runtime_write_lead() -> None:
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(updated_age=-1.0),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["state"] == "HEALTHY_IDLE"


def test_wrapped_active_turn_with_dead_cli_brain_is_not_healthy_idle() -> None:
    active_health = hm.build_snapshot(
        agent="worker",
        cli="codex",
        mode="wrapper-loop",
        state=hm.STATE_WORKING_TURN,
        updated_at=_iso(NOW - 1),
        since=_iso(NOW - 60),
        last_progress_at=_iso(NOW - 1),
        request_id="q-active",
        msg_id="20990101-000000-000000-ACTIVE",
        reason_code="progress_event",
    )
    active_health.update({"age_seconds": 1.0, "stale": False, "advisory": True})

    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            health=active_health,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=60.0,
                progress_age=60.0,
                progress_sequence=3,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],  # live wrapper only; the active CLI brain is gone
    )

    assert plan["state"] != "HEALTHY_IDLE"


def test_wrapped_terminal_stale_runtime_cannot_be_healthy_idle() -> None:
    terminal_runtime = _wrapper_runtime_view(
        phase="terminal",
        updated_age=1.0,
        progress_age=3600.0,
        progress_sequence=4,
        turn_generation=8,
        outcome="success",
    )

    plan = _plan_wrap(
        _report(heartbeat_stale=False, wrapper_runtime=terminal_runtime),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["state"] == "CLI_CHILD_UNKNOWN"


@pytest.mark.parametrize("outcome", ["failed", "dead_letter", "success"])
def test_wrapped_stale_live_terminal_uses_existing_recovery_path(
    outcome: str,
) -> None:
    terminal_runtime = _wrapper_runtime_view(
        phase="terminal",
        updated_age=3000.0,
        progress_age=3000.0,
        progress_sequence=4,
        turn_generation=8,
        outcome=outcome,
    )

    plan = _plan_wrap(
        _report(
            heartbeat_stale=True,
            heartbeat_age_seconds=3000.0,
            wrapper_runtime=terminal_runtime,
        ),
        {"agents": {"worker": _wrap_ready(backoff_next_epoch=0.0)}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["state"] == "STUCK_OR_DEAD"


def test_wrapped_idle_without_cli_child_is_healthy_idle() -> None:
    plan = _plan_wrap(
        _report(heartbeat_stale=False),
        {"agents": {"worker": _wrap_ready(readiness_seen=False)}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "HEALTHY_IDLE"
    assert plan["next_state"]["readiness_seen"] is True


def test_wrapped_idle_absent_wrapper_is_non_green_before_heartbeat_stales() -> None:
    earned = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-absent-wrapper-fresh-certificate",
    )["next_state"]["owned_process_tree"]
    plan = _plan_wrap(
        _report(heartbeat_stale=False),
        {"agents": {"worker": _wrap_ready(owned_process_tree=earned)}},
        now=NOW + 1,
        snapshot=[],
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "WRAPPER_MISSING"
    assert plan["next_state"]["owned_process_tree"]["status"] == "absent"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_absent"
    )
    assert plan["next_state"]["owned_process_tree"]["entries"] == earned["entries"]
    assert plan["next_state"]["owned_process_tree"]["refreshed_at"] != (
        earned["refreshed_at"]
    )


def test_wrapped_idle_absent_wrapper_uses_existing_stale_recovery_path() -> None:
    earned = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-absent-wrapper-stale-certificate",
    )["next_state"]["owned_process_tree"]
    plan = _plan_wrap(
        _report(heartbeat_stale=True, heartbeat_age_seconds=3000.0),
        {"agents": {"worker": _wrap_ready(owned_process_tree=earned)}},
        now=NOW + 1,
        snapshot=[],
    )

    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["state"] == "STUCK_OR_DEAD"
    assert plan["kill_targets"] == []
    assert plan["barrier_state"]["owned_process_tree"]["status"] == "absent"


@pytest.mark.parametrize(
    ("phase", "updated_age", "progress_age", "outcome", "heartbeat_stale",
     "expected_action", "expected_state"),
    [
        ("starting", 1.0, None, None, False, sup.NONE, "CLI_CHILD_STARTING"),
        ("starting", 31.0, None, None, False, sup.NONE, "CLI_CHILD_UNKNOWN"),
        ("terminal", 1.0, 1.0, "success", False, sup.NONE, "HEALTHY_WORKING"),
        ("terminal", 2500.0, 2500.0, "success", False, sup.NONE, "CLI_CHILD_UNKNOWN"),
        ("terminal", 1.0, None, "success", False, sup.NONE, "CLI_CHILD_UNKNOWN"),
        ("terminal", 1.0, 1.0, "failed", False, sup.NONE, "TURN_FAILED"),
        ("idle", 3000.0, None, None, True, sup.STUCK_RECOVER, "STUCK_OR_DEAD"),
    ],
    ids=[
        "starting-in-grace",
        "starting-after-grace",
        "terminal-success-finalizing",
        "terminal-success-stale-progress",
        "terminal-success-unclassified-progress",
        "terminal-failure",
        "idle-stale-heartbeat",
    ],
)
def test_wrapped_non_active_runtime_recovery_matrix(
    phase: str,
    updated_age: float,
    progress_age: float | None,
    outcome: str | None,
    heartbeat_stale: bool,
    expected_action: str,
    expected_state: str,
) -> None:
    runtime = _wrapper_runtime_view(
        phase=phase,
        updated_age=updated_age,
        progress_age=progress_age,
        progress_sequence=2,
        outcome=outcome,
    )
    plan = _plan_wrap(
        _report(
            heartbeat_stale=heartbeat_stale,
            heartbeat_age_seconds=3000.0 if heartbeat_stale else 1.0,
            wrapper_runtime=runtime,
        ),
        {"agents": {"worker": _wrap_ready(backoff_next_epoch=0.0)}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["action"] == expected_action
    assert plan["state"] == expected_state


def test_wrapped_claude_launcher_self_is_healthy_working() -> None:
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {"auto_restart": True, "cli": "claude", "wrapped": True}
        },
    }
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap(cli="claude"),
        config=config,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "HEALTHY_WORKING"
    assert plan["next_state"]["brain_pid"] == WRAP_CHILD_PID


def test_wrapped_codex_forked_launcher_discovers_live_tui_grandchild() -> None:
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_codex_forked_brain_snap(),
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "HEALTHY_WORKING"
    assert plan["next_state"]["brain_pid"] == WRAP_TUI_PID


@pytest.mark.parametrize("wait_agent", ["worker-2", "worker"])
def test_wrapped_unbound_wait_tree_cannot_certify_current_turn_brain(
    wait_agent: str,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        {
            "pid": 910,
            "parent_pid": 1,
            "name": "codex.exe",
            "command_line": "codex prior-generation tui",
            "start_time": _ps_iso(900000),
        },
        {
            "pid": 911,
            "parent_pid": 910,
            "name": "python.exe",
            "command_line": (
                f"python -m agenttalk --root {TEST_ROOT} "
                f"wait --for {wait_agent}"
            ),
            "start_time": _ps_iso(910000),
        },
    ]
    wait = sup._wait_row_for(sup._snap_index_and_excluded(snapshot)[0], "worker")
    assert (wait is not None) is (wait_agent == "worker")

    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=2,
                turn_generation=7,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=snapshot,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_UNKNOWN"
    assert plan["next_state"]["brain_pid"] is None
    assert plan["kill_targets"] == []


@pytest.mark.parametrize(
    "candidate_start",
    [None, "opaque-start", _ps_iso(500000)],
)
def test_wrapped_launcher_edge_without_current_turn_start_evidence_is_unknown(
    candidate_start: str | None,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        {
            "pid": 920,
            "parent_pid": WRAP_CHILD_PID,
            "name": "codex.exe",
            "command_line": "codex historical child",
            "start_time": candidate_start,
        },
    ]
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=snapshot,
    )

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["brain_pid"] is None
    assert plan["kill_targets"] == []


def test_wrapped_comparable_linux_start_tokens_bind_current_turn_brain() -> None:
    boot_id = "12345678-1234-1234-1234-123456789abc"
    wrapper_start = f"linux:{boot_id}:90"
    wrapper = {
        **_wrap_snap()[0],
        "start_time": wrapper_start,
    }
    snapshot = [
        wrapper,
        {
            "pid": WRAP_CHILD_PID,
            "parent_pid": WRAP_LAUNCHER_PID,
            "name": "codex.exe",
            "command_line": "codex launcher",
            "start_time": f"linux:{boot_id}:100",
        },
        {
            "pid": 920,
            "parent_pid": WRAP_CHILD_PID,
            "name": "codex.exe",
            "command_line": "codex current child",
            "start_time": f"linux:{boot_id}:101",
        },
    ]
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=2,
                wrapper_start=wrapper_start,
                launcher_start=f"linux:{boot_id}:100",
            ),
        ),
        {"agents": {"worker": _wrap_ready(launcher_start=wrapper_start)}},
        snapshot=snapshot,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "HEALTHY_WORKING"
    assert plan["next_state"]["brain_pid"] == 920
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert all(entry["start_filetime"] is None for entry in tree["entries"][1:])
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        tree,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation=tree["wrapper_generation"],
        launch_nonce=tree["launch_nonce"],
    ) is not None

    brain_entry = next(entry for entry in tree["entries"] if entry["pid"] == 920)
    brain_entry["start_filetime"] = "999"
    second = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=3,
                wrapper_start=wrapper_start,
                launcher_start=f"linux:{boot_id}:100",
            ),
        ),
        {"agents": {"worker": plan["next_state"]}},
        now=NOW + 1,
        snapshot=snapshot,
    )

    assert second["next_state"]["owned_process_tree"]["status"] == "complete"


def test_wrapped_active_absent_brain_requires_two_same_generation_polls() -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=60.0,
            progress_age=60.0,
            progress_sequence=2,
        ),
    )
    first = _plan_wrap(
        report,
        {"agents": {"worker": _wrap_ready(consecutive_fails=2)}},
        snapshot=_wrap_snap()[:1],
    )
    assert first["action"] == sup.NONE
    assert first["state"] == "CLI_CHILD_MISSING"
    assert first["next_state"]["runtime_dead_polls"] == 1
    assert first["next_state"]["consecutive_fails"] == 2

    second = _plan_wrap(
        report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=_wrap_snap()[:1],
    )
    assert second["action"] == sup.STUCK_RECOVER
    assert second["state"] == "CLI_CHILD_DEAD"
    assert second["next_state"]["consecutive_fails"] == 3


@pytest.mark.parametrize("phase", ["starting", "active", "terminal"])
def test_wrapped_dead_wrapper_recovers_from_every_non_idle_runtime_phase(
    phase: str,
) -> None:
    runtime = _wrapper_runtime_view(
        phase=phase,
        updated_age=3000.0,
        progress_age=3000.0 if phase != "starting" else None,
        progress_sequence=2,
        outcome="success" if phase == "terminal" else None,
    )
    report = _report(
        heartbeat_stale=True,
        heartbeat_age_seconds=3000.0,
        wrapper_runtime=runtime,
    )
    earned = _owned_tree_plan(
        _wrap_snap(),
        request_id=f"rr-dead-wrapper-{phase}-certificate",
    )["next_state"]["owned_process_tree"]
    first = _plan_wrap(
        report,
        {
            "agents": {
                "worker": _wrap_ready(
                    backoff_next_epoch=0.0,
                    owned_process_tree=earned,
                    owned_process_tree_pending=False,
                ),
            },
        },
        snapshot=[],
    )

    assert first["action"] == sup.STUCK_RECOVER
    assert first["state"] == "STUCK_OR_DEAD"

    second = _plan_wrap(
        report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[],
    )
    assert second["action"] == sup.BACKOFF_WAIT
    assert second["state"] == "STUCK_OR_DEAD"


def test_wrapped_non_green_child_breaks_continuous_health_window() -> None:
    state = _wrap_ready(
        consecutive_fails=4,
        backoff_next_epoch=NOW + 100,
        healthy_since=NOW - 179,
    )
    missing = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=60.0,
                progress_age=60.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": state}},
        snapshot=_wrap_snap()[:1],
    )

    assert missing["state"] == "CLI_CHILD_MISSING"
    assert missing["next_state"]["healthy_since"] is None
    assert missing["next_state"]["consecutive_fails"] == 4
    assert missing["next_state"]["backoff_next_epoch"] == NOW + 100

    healthy = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 2,
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=3,
            ),
        ),
        {"agents": {"worker": missing["next_state"]}},
        now=NOW + 2,
        snapshot=_codex_forked_brain_snap(),
    )

    assert healthy["state"] == "HEALTHY_WORKING"
    assert healthy["next_state"]["healthy_since"] == NOW + 2
    assert healthy["next_state"]["consecutive_fails"] == 4
    assert healthy["next_state"]["backoff_next_epoch"] == NOW + 100


def test_wrapped_real_progress_ends_spawn_grace_before_child_death() -> None:
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=1,
            ),
        ),
        {
            "agents": {
                "worker": _wrap_ready(
                    launching=True,
                    readiness_seen=False,
                    launch_grace_until=NOW + 100,
                )
            }
        },
        snapshot=_wrap_snap()[:1],
    )

    assert plan["state"] == "CLI_CHILD_MISSING"
    assert plan["next_state"]["runtime_dead_polls"] == 1


def test_wrapped_prior_turn_sequence_does_not_end_current_spawn_grace() -> None:
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=None,
                progress_sequence=5,
                turn_generation=2,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_STARTING"
    assert plan["next_state"]["runtime_dead_polls"] == 0


def test_wrapped_live_brain_without_progress_stalls_after_confirmation() -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=2500.0,
            progress_age=None,
            progress_sequence=0,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=0,
        runtime_progress_seen_epoch=NOW - 2500,
    )

    first = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
    )
    assert first["action"] == sup.NONE
    assert first["state"] == "CLI_CHILD_STALL_SUSPECT"

    second = _plan_wrap(
        report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=_codex_forked_brain_snap(),
    )
    assert second["action"] == sup.STUCK_RECOVER
    assert second["state"] == "CLI_CHILD_STALLED"


def test_wrapped_claude_stall_waits_for_stale_heartbeat_without_watchdog() -> None:
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {"auto_restart": True, "cli": "claude", "wrapped": True}
        },
    }
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=300.0,
            progress_age=300.0,
            progress_sequence=2,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - 300,
        runtime_stall_polls=1,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(cli="claude"),
        config=config,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_STALLED"
    assert "heartbeat is fresh" in plan["reason"]

    # The default 900-second ticker cap is finite. At turn age 1081 its final
    # stamp is 181 seconds old, so the ordinary stale-heartbeat path takes over.
    stale_now = NOW + 781
    stale = _plan_wrap(
        _report(
            heartbeat_stale=True,
            heartbeat_age_seconds=181.0,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=stale_now,
                updated_age=1081.0,
                progress_age=1081.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": plan["next_state"]}},
        now=stale_now,
        snapshot=_wrap_snap(cli="claude"),
        config=config,
    )

    assert stale["action"] == sup.STUCK_RECOVER
    assert stale["state"] == "CLI_CHILD_STALLED"
    assert "heartbeat is stale" in stale["reason"]


def test_wrapped_watchdog_recovery_reserves_progress_coalescing_margin(
    tmp_path: Path,
) -> None:
    config = {**_WRAP_CONFIG, "poll_seconds": 1}
    now = [NOW]
    writer = wrt.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "wrapper-1",
        wrapper_pid=WRAP_LAUNCHER_PID,
        wrapper_start=WRAP_START,
        clock=lambda: now[0],
    )
    writer.starting(message_id="msg-runtime", turn_id="turn-1")
    writer.active(WRAP_CHILD_PID, WRAP_CHILD_START)
    durable = writer.progress()
    now[0] += 4.9
    hidden = writer.progress()
    runtime = wrt.read_runtime(tmp_path, "worker", now_epoch=NOW + 2400.1)
    report = _report(heartbeat_stale=False, wrapper_runtime=runtime)
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=1,
        runtime_progress_seen_epoch=NOW,
    )

    assert durable["progress_sequence"] == 1
    assert hidden["progress_sequence"] == 2
    assert runtime["record"]["progress_sequence"] == 1

    # Polling every second must not recover until hidden event #2's true age
    # reaches the 2400-second threshold.
    first = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        now=NOW + 2400.1,
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )
    second = _plan_wrap(
        report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 2401.1,
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )

    assert first["action"] == sup.NONE
    assert first["state"] == "CLI_CHILD_STALL_SUSPECT"
    assert second["action"] == sup.NONE
    assert second["state"] == "CLI_CHILD_STALLED"
    assert "coalescing allowance" in second["reason"]

    safe = _plan_wrap(
        report,
        {"agents": {"worker": second["next_state"]}},
        now=NOW + 2405.0,
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )

    assert safe["action"] == sup.STUCK_RECOVER
    assert safe["state"] == "CLI_CHILD_STALLED"


def test_wrapped_stalled_forked_brain_is_an_attributed_kill_target() -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=2500.0,
            progress_age=2500.0,
            progress_sequence=2,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - 2500,
        runtime_stall_polls=1,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
    )

    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["state"] == "CLI_CHILD_STALLED"
    assert {
        "pid": WRAP_TUI_PID,
        "start": _ps_iso(700000),
        "start_filetime": _ps_filetime(700000),
        "reason": "owned_process_tree",
        "source": "owned_process_tree",
    } in plan["kill_targets"]


def test_wrapped_invalid_watchdog_floor_never_authorizes_stall_recovery() -> None:
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "codex",
                "wrapped": True,
                "turn_watchdog": {"turn_elapsed_seconds": "invalid"},
            }
        },
    }
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=2500.0,
            progress_age=None,
            progress_sequence=0,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=0,
        runtime_progress_seen_epoch=NOW - 2500,
        runtime_stall_polls=1,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_STALLED"
    assert "turn_elapsed_seconds is invalid" in plan["reason"]


def test_wrapped_ambiguous_brain_is_unknown_and_never_killed() -> None:
    snapshot = [
        _wrap_snap()[0],
        {
            "pid": WRAP_TUI_PID,
            "parent_pid": 999,
            "name": "codex.exe",
            "command_line": "codex unrelated",
            "start_time": _ps_iso(700000),
        },
    ]
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=snapshot,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_UNKNOWN"
    assert plan["kill_targets"] == []


def test_wrapped_progress_sequence_advance_stays_healthy_working() -> None:
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=1,
        runtime_progress_seen_epoch=NOW - 2500,
        runtime_stall_polls=1,
    )
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=2500.0,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
    )

    assert plan["state"] == "HEALTHY_WORKING"
    assert plan["next_state"]["runtime_progress_seen_epoch"] == NOW
    assert plan["next_state"]["runtime_stall_polls"] == 0


def test_wrapped_stale_progress_requires_threshold_and_confirming_poll() -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=1.0,
            progress_age=2500.0,
            progress_sequence=2,
        ),
    )
    first = _plan_wrap(
        report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_codex_forked_brain_snap(),
    )
    assert first["action"] == sup.NONE
    assert first["state"] == "CLI_CHILD_STALL_SUSPECT"

    second = _plan_wrap(
        report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=_codex_forked_brain_snap(),
    )
    assert second["action"] == sup.STUCK_RECOVER
    assert second["state"] == "CLI_CHILD_STALLED"


def test_wrapped_stall_below_watchdog_floor_waits_for_floor_before_recovery() -> None:
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "codex",
                "wrapped": True,
                "stuck_after_seconds": 2000,
            }
        },
    }
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=1.0,
            progress_age=2103.0,
            progress_sequence=2,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - 2103,
        runtime_stall_polls=1,
    )
    first = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )
    second = _plan_wrap(
        report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )
    third = _plan_wrap(
        report,
        {"agents": {"worker": second["next_state"]}},
        now=NOW + 3,
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )

    assert second["action"] == sup.NONE
    assert second["state"] == "CLI_CHILD_STALLED"
    assert "hard watchdog floor" in second["reason"]
    assert third["action"] == sup.STUCK_RECOVER
    assert third["state"] == "CLI_CHILD_STALLED"


def test_wrapped_low_stuck_opt_in_stale_heartbeat_authorizes_active_recovery() -> None:
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "codex",
                "wrapped": True,
                "stuck_after_seconds": 120,
                "allow_low_stuck_after": True,
            }
        },
    }
    report = _report(
        heartbeat_stale=True,
        heartbeat_age_seconds=130.0,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=130.0,
            progress_age=130.0,
            progress_sequence=2,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - 130,
        runtime_stall_polls=1,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )

    assert plan["action"] == sup.STUCK_RECOVER
    assert plan["state"] == "CLI_CHILD_STALLED"
    assert "heartbeat is stale" in plan["reason"]


@pytest.mark.parametrize(
    "case",
    [
        {
            "id": "recent-progress",
            "cli": "claude",
            "stuck_after": 180,
            "elapsed": 100,
            "heartbeat_stale": False,
            "watchdog": {"enabled": False},
            "stall_polls": 0,
            "action": sup.NONE,
            "state": "HEALTHY_WORKING",
        },
        {
            "id": "first-stale-poll",
            "cli": "claude",
            "stuck_after": 180,
            "elapsed": 190,
            "heartbeat_stale": True,
            "watchdog": {"enabled": False},
            "stall_polls": 0,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALL_SUSPECT",
        },
        {
            "id": "coalescing-allowance",
            "cli": "claude",
            "stuck_after": 180,
            "elapsed": 183,
            "heartbeat_stale": True,
            "watchdog": {"enabled": False},
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "coalescing allowance",
        },
        {
            "id": "heartbeat-authority-watchdog-off",
            "cli": "claude",
            "stuck_after": 180,
            "elapsed": 190,
            "heartbeat_stale": True,
            "watchdog": {"enabled": False},
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "heartbeat is stale",
        },
        {
            "id": "heartbeat-authority-low-opt-in",
            "cli": "codex",
            "stuck_after": 120,
            "elapsed": 130,
            "heartbeat_stale": True,
            "allow_low": True,
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "heartbeat is stale",
        },
        {
            "id": "heartbeat-authority-invalid-watchdog-floor",
            "cli": "codex",
            "stuck_after": 2400,
            "elapsed": 2500,
            "heartbeat_stale": True,
            "watchdog": {"enabled": True, "turn_elapsed_seconds": 0},
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "heartbeat is stale",
        },
        {
            "id": "heartbeat-authority-zero-watchdog-poll-fallback",
            "cli": "codex",
            "stuck_after": 1800,
            "elapsed": 1900,
            "heartbeat_stale": True,
            "allow_low": True,
            "watchdog": {"enabled": True, "poll_seconds": 0},
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "heartbeat is stale",
        },
        {
            "id": "heartbeat-denied-low-no-opt-in",
            "cli": "codex",
            "stuck_after": 120,
            "elapsed": 130,
            "heartbeat_stale": True,
            "allow_low": False,
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "hard watchdog floor",
        },
        {
            "id": "heartbeat-denied-invalid-work-heartbeat",
            "cli": "claude",
            "stuck_after": 180,
            "elapsed": 190,
            "heartbeat_stale": True,
            "watchdog": {"enabled": False},
            "work_heartbeat": {"enabled": True, "interval_seconds": 0},
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "recovery guards are not authoritative",
        },
        {
            "id": "fresh-heartbeat-watchdog-off",
            "cli": "claude",
            "stuck_after": 180,
            "elapsed": 300,
            "heartbeat_stale": False,
            "watchdog": {"enabled": False},
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "heartbeat is fresh",
        },
        {
            "id": "watchdog-authority-valid-floor",
            "cli": "codex",
            "stuck_after": 2400,
            "elapsed": 2500,
            "heartbeat_stale": False,
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "hard watchdog deadline",
        },
        {
            "id": "watchdog-low-opt-in-before-floor",
            "cli": "codex",
            "stuck_after": 120,
            "elapsed": 130,
            "heartbeat_stale": False,
            "allow_low": True,
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "opted-in low stale threshold",
        },
        {
            "id": "watchdog-low-opt-in-after-floor",
            "cli": "codex",
            "stuck_after": 120,
            "elapsed": 2110,
            "heartbeat_stale": False,
            "allow_low": True,
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "hard watchdog deadline",
        },
        {
            "id": "watchdog-low-without-opt-in-before-floor",
            "cli": "codex",
            "stuck_after": 120,
            "elapsed": 130,
            "heartbeat_stale": False,
            "allow_low": False,
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "hard watchdog floor",
        },
        {
            "id": "watchdog-low-without-opt-in-after-floor",
            "cli": "codex",
            "stuck_after": 120,
            "elapsed": 2110,
            "heartbeat_stale": False,
            "allow_low": False,
            "stall_polls": 1,
            "action": sup.STUCK_RECOVER,
            "state": "CLI_CHILD_STALLED",
            "reason": "hard watchdog deadline",
        },
        {
            "id": "watchdog-invalid-floor",
            "cli": "codex",
            "stuck_after": 2400,
            "elapsed": 2500,
            "heartbeat_stale": False,
            "watchdog": {"enabled": True, "turn_elapsed_seconds": 0},
            "stall_polls": 1,
            "action": sup.NONE,
            "state": "CLI_CHILD_STALLED",
            "reason": "turn_elapsed_seconds is invalid",
        },
    ],
    ids=lambda case: case["id"],
)
def test_wrapped_active_stall_recovery_authority_matrix(case: dict) -> None:
    cli_name = case["cli"]
    agent_config = {
        "auto_restart": True,
        "cli": cli_name,
        "wrapped": True,
        "stuck_after_seconds": case["stuck_after"],
    }
    if "allow_low" in case:
        agent_config["allow_low_stuck_after"] = case["allow_low"]
    if "watchdog" in case:
        agent_config["turn_watchdog"] = case["watchdog"]
    if "work_heartbeat" in case:
        agent_config["work_heartbeat"] = case["work_heartbeat"]
    config = {**_WRAP_CONFIG, "agents": {"worker": agent_config}}
    elapsed = float(case["elapsed"])
    heartbeat_stale = bool(case["heartbeat_stale"])
    report = _report(
        heartbeat_stale=heartbeat_stale,
        heartbeat_age_seconds=(
            float(case["stuck_after"]) + 10 if heartbeat_stale else 1.0
        ),
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=elapsed,
            progress_age=elapsed,
            progress_sequence=2,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - elapsed,
        runtime_stall_polls=case["stall_polls"],
    )
    snapshot = (
        _wrap_snap(cli="claude")
        if cli_name == "claude"
        else _codex_forked_brain_snap()
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=snapshot,
        config=config,
    )

    assert plan["action"] == case["action"]
    assert plan["state"] == case["state"]
    if "reason" in case:
        assert case["reason"] in plan["reason"]


def test_wrapped_active_live_brain_without_progress_below_threshold_is_non_green(
) -> None:
    elapsed = 60.0
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=elapsed,
            progress_age=None,
            progress_sequence=0,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=0,
        runtime_progress_seen_epoch=NOW - elapsed,
    )
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {
                "auto_restart": True,
                "cli": "claude",
                "wrapped": True,
                "stuck_after_seconds": 180,
            }
        },
    }

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(cli="claude"),
        config=config,
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "CLI_CHILD_NO_PROGRESS"


def test_wrapped_active_live_brain_with_invalid_progress_sequence_is_unknown(
) -> None:
    runtime = _wrapper_runtime_view(
        phase="active",
        updated_age=60.0,
        progress_age=1.0,
        progress_sequence=2,
    )
    runtime["record"]["progress_sequence"] = "2"

    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=runtime,
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap(cli="claude"),
        config={
            **_WRAP_CONFIG,
            "agents": {
                "worker": {
                    "auto_restart": True,
                    "cli": "claude",
                    "wrapped": True,
                }
            },
        },
    )

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_runtime_record_invalid"
    )


@pytest.mark.parametrize(
    ("include_value", "value", "expected_action"),
    [
        (True, True, sup.STUCK_RECOVER),
        (True, False, sup.NONE),
        (True, "true", sup.NONE),
        (True, "false", sup.NONE),
        (True, "", sup.NONE),
        (True, 0, sup.NONE),
        (True, 1, sup.NONE),
        (True, None, sup.NONE),
        (False, None, sup.NONE),
    ],
    ids=[
        "boolean-true",
        "boolean-false",
        "string-true",
        "string-false",
        "empty-string",
        "integer-zero",
        "integer-one",
        "null",
        "absent",
    ],
)
def test_wrapped_low_stuck_opt_in_requires_literal_boolean_true(
    include_value: bool,
    value: object,
    expected_action: str,
) -> None:
    agent_config = {
        "auto_restart": True,
        "cli": "codex",
        "wrapped": True,
        "stuck_after_seconds": 120,
        "turn_watchdog": {"enabled": False},
    }
    if include_value:
        agent_config["allow_low_stuck_after"] = value
    config = {**_WRAP_CONFIG, "agents": {"worker": agent_config}}
    elapsed = 130.0
    report = _report(
        heartbeat_stale=True,
        heartbeat_age_seconds=elapsed,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=elapsed,
            progress_age=elapsed,
            progress_sequence=2,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - elapsed,
        runtime_stall_polls=1,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
        config=config,
    )

    assert plan["action"] == expected_action
    assert plan["state"] == "CLI_CHILD_STALLED"


@pytest.mark.parametrize(
    ("runtime_overrides", "state_overrides"),
    [
        ({"wrapper_generation": "wrapper-2"}, {}),
        ({"turn_generation": 2}, {}),
    ],
)
def test_wrapped_generation_change_resets_dead_confirmation(
    runtime_overrides: dict,
    state_overrides: dict,
) -> None:
    first_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=60.0,
            progress_age=60.0,
            progress_sequence=2,
        ),
    )
    first = _plan_wrap(
        first_report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )
    second_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=60.0,
            progress_age=60.0,
            progress_sequence=2,
            **runtime_overrides,
        ),
    )
    state = {**first["next_state"], **state_overrides}
    second = _plan_wrap(
        second_report,
        {"agents": {"worker": state}},
        now=NOW + 1,
        snapshot=_wrap_snap()[:1],
    )

    if runtime_overrides.get("wrapper_generation") == "wrapper-2":
        assert second["action"] == sup.WARN_ONLY
        assert second["state"] == "PROCESS_TREE_INVALID"
        assert second["next_state"]["runtime_wrapper_generation"] == "wrapper-2"
        assert second["next_state"]["owned_process_tree"]["reason_code"] == (
            "process_tree_invalid_generation_adoption_pending"
        )
        rebound = _plan_wrap(
            second_report,
            {"agents": {"worker": second["next_state"]}},
            now=NOW + 2,
            snapshot=_wrap_snap()[:1],
        )
        assert rebound["state"] == "CLI_CHILD_MISSING"
        assert rebound["next_state"]["runtime_dead_polls"] == 1
    else:
        assert second["action"] == sup.NONE
        assert second["state"] == "CLI_CHILD_MISSING"
        assert second["next_state"]["runtime_dead_polls"] == 1


@pytest.mark.parametrize(
    "runtime_overrides",
    [
        {"wrapper_generation": "wrapper-2"},
        {"turn_generation": 2},
    ],
    ids=["wrapper-generation", "turn-generation"],
)
def test_wrapped_generation_change_resets_stall_confirmation(
    runtime_overrides: dict,
) -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=2500.0,
            progress_age=2500.0,
            progress_sequence=2,
            **runtime_overrides,
        ),
    )
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=2,
        runtime_progress_seen_epoch=NOW - 2500,
        runtime_stall_polls=1,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
    )

    if runtime_overrides.get("wrapper_generation") == "wrapper-2":
        assert plan["action"] == sup.WARN_ONLY
        assert plan["state"] == "PROCESS_TREE_INVALID"
        assert plan["next_state"]["runtime_wrapper_generation"] == "wrapper-2"
        assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
            "process_tree_invalid_generation_adoption_pending"
        )
        rebound = _plan_wrap(
            report,
            {"agents": {"worker": plan["next_state"]}},
            now=NOW + 1,
            snapshot=_codex_forked_brain_snap(),
        )
        assert rebound["state"] == "CLI_CHILD_STALL_SUSPECT"
        assert rebound["next_state"]["runtime_stall_polls"] == 1
    else:
        assert plan["action"] == sup.NONE
        assert plan["state"] == "CLI_CHILD_STALL_SUSPECT"
        assert plan["next_state"]["runtime_stall_polls"] == 1


def test_wrapped_child_dead_respects_backoff_and_readiness_cap() -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=60.0,
            progress_age=60.0,
            progress_sequence=2,
        ),
    )
    first = _plan_wrap(
        report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )

    backoff_state = {
        **first["next_state"],
        "backoff_next_epoch": NOW + 100,
        "consecutive_fails": 4,
    }
    waiting = _plan_wrap(
        report,
        {"agents": {"worker": backoff_state}},
        now=NOW + 1,
        snapshot=_wrap_snap()[:1],
    )
    assert waiting["action"] == sup.BACKOFF_WAIT
    assert waiting["state"] == "CLI_CHILD_DEAD"
    assert waiting["next_state"]["consecutive_fails"] == 4

    capped_state = {
        **first["next_state"],
        "launching": True,
        "readiness_seen": False,
        "launch_grace_until": NOW - 1,
        "readiness_fails": 3,
    }
    capped = _plan_wrap(
        report,
        {"agents": {"worker": capped_state}},
        now=NOW + 1,
        snapshot=_wrap_snap()[:1],
    )
    assert capped["action"] == sup.READINESS_GAVE_UP
    assert capped["state"] == "READINESS_GAVE_UP"
    assert capped["kill_targets"] == []


def test_child_health_restart_does_not_redrive_bus_committed_inbound(
    tmp_path: Path,
) -> None:
    from agenttalk.wrapper import loop as wrapper_loop

    store = _team(tmp_path)
    committed = store.send(
        sender="lead",
        recipient="worker",
        body="publish durable work",
    )
    initial_seen: list[str] = []

    def initial_drive(record: dict) -> bool:
        initial_seen.append(record["id"])
        return True

    assert wrapper_loop.run_loop(
        store,
        "worker",
        initial_drive,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_turns=1,
    ) == 1
    assert initial_seen == [committed.id]
    assert store.cursor("worker") == committed.id

    # The wrapper can crash after the validated bus commits while its health
    # self-report still says the now-absent child was active. Two confirming
    # polls correctly authorize #72 recovery without touching the bus cursor.
    dead_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=60.0,
            progress_age=60.0,
            progress_sequence=2,
        ),
    )
    first = _plan_wrap(
        dead_report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )
    recovered = _plan_wrap(
        dead_report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=_wrap_snap()[:1],
    )
    assert recovered["action"] == sup.STUCK_RECOVER
    assert recovered["state"] == "CLI_CHILD_DEAD"
    assert store.cursor("worker") == committed.id

    later = store.send(sender="lead", recipient="worker", body="later work")
    restarted_seen: list[str] = []

    def restarted_drive(record: dict) -> bool:
        restarted_seen.append(record["id"])
        return True

    assert wrapper_loop.run_loop(
        store,
        "worker",
        restarted_drive,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        max_turns=1,
    ) == 1
    assert restarted_seen == [later.id]
    assert committed.id not in restarted_seen
    assert store.cursor("worker") == later.id


def test_wrapped_dead_letter_is_turn_failed_not_success() -> None:
    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="terminal",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=3,
                outcome="dead_letter",
            ),
        ),
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )

    assert plan["action"] == sup.NONE
    assert plan["state"] == "TURN_FAILED"


def test_wrapped_missing_runtime_is_unknown_with_rollout_remediation() -> None:
    plan = sup.plan_actions(
        _report(heartbeat_stale=False),
        {"agents": {"worker": _wrap_ready()}},
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=_wrap_snap()[:1],
    )["agents"]["worker"]

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_runtime_absent"
    )


def test_reset_remedy_does_not_name_command_when_runtime_currently_invalid() -> None:
    """Task #150 round 9 connector finding, the third instance of the same
    wrong direction: --reset-process-tree-ownership (cli.py) requires the
    CURRENT wrapper runtime record to read STATUS_VALID before it will
    act on a configured agent, entirely independent of the tree's own
    entries/rejected_count - round 2 and round 3 each closed the ONE
    precondition that had actually bitten (an entryless placeholder's
    missing nonce; a rejected candidate outside the identity list) by
    patching that specific case, leaving this one unclosed. A tree can
    hold real, non-empty entries (inherited from an earlier complete
    poll) while the runtime record is exactly what made THIS poll
    invalid - the remedy must not name a command that will refuse the
    operator for that reason. Asserts the operator-visible message, not
    the internal predicate."""
    earned = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-reset-remedy-runtime-invalid",
    )["next_state"]["owned_process_tree"]
    assert earned["status"] == "complete"
    assert earned["entries"]

    plan = sup.plan_actions(
        _report(heartbeat_stale=False),
        {"agents": {"worker": _wrap_ready(owned_process_tree=earned)}},
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=_wrap_snap()[:1],
    )["agents"]["worker"]
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["entries"]  # has_entries branch, not the entryless one
    assert tree["reason_code"] == "process_tree_invalid_runtime_absent"
    assert "--reset-process-tree-ownership" not in plan["reason"]
    assert "no scripted remedy applies" in plan["reason"]
    assert "complete current ownership" in plan["reason"]


def test_reset_remedy_does_not_name_command_when_wrapper_identity_mismatches_state() -> None:
    """Task #150 round 10 finding 2: round 9's fix gated the remedy on
    runtime_status == STATUS_VALID alone - one signal _wrapped_liveness
    computes, not the reset command's actual admission predicate.
    --reset-process-tree-ownership (cli.py,
    process_tree_ownership_reset_evidence) also refuses when a STATUS_VALID
    runtime record's own wrapper_pid/wrapper_start disagrees with this
    state's stored launcher_pid/launcher_start - a record can parse fine
    (STATUS_VALID) while still failing that agreement, which is exactly
    what sends _wrapped_liveness down wrapper_state_mismatch
    (process_tree_invalid_wrapper_state_mismatch) with a nonempty inherited
    tree. The remedy must not recommend a command that refuses for that
    reason either. Asserts the operator-visible message, not the internal
    predicate."""
    earned = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-reset-remedy-wrapper-mismatch",
    )["next_state"]["owned_process_tree"]
    assert earned["status"] == "complete"
    assert earned["entries"]

    mismatched_runtime = _wrapper_runtime_view(
        wrapper_pid=WRAP_LAUNCHER_PID + 500,
        wrapper_start=_ps_iso(999000),
    )
    plan = sup.plan_actions(
        _report(heartbeat_stale=False, wrapper_runtime=mismatched_runtime),
        {"agents": {"worker": _wrap_ready(owned_process_tree=earned)}},
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=_wrap_snap()[:1],
    )["agents"]["worker"]
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["entries"]  # has_entries branch, not the entryless one
    assert tree["reason_code"] == "process_tree_invalid_wrapper_state_mismatch"
    assert "--reset-process-tree-ownership" not in plan["reason"]
    assert "reported wrapper identity agrees with the supervisor's recorded launcher" in (
        plan["reason"]
    )


def test_wrapped_torn_runtime_read_is_unknown_without_partial_fields(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    (store.state_dir / "worker.heartbeat").write_text(_iso(NOW), encoding="utf-8")
    wrt.runtime_path(store.state_dir, "worker").write_bytes(b'{"phase":"idle"')

    report = sup.build_report(
        store,
        now_epoch=NOW,
        supervisor_config=_WRAP_CONFIG,
    )
    view = report["agents"]["worker"]["wrapper_runtime"]
    assert view == {"status": wrt.STATUS_INVALID, "error": "malformed"}

    plan = sup.plan_actions(
        report,
        {"agents": {"worker": _wrap_ready()}},
        _WRAP_CONFIG,
        now_epoch=NOW,
        snapshot=_wrap_snap(root=str(tmp_path))[:1],
    )["agents"]["worker"]
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["action"] == sup.WARN_ONLY
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_runtime_invalid"
    )


def test_wrapped_torn_read_cannot_erase_sequence_high_water_mark() -> None:
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=5,
        runtime_progress_seen_epoch=NOW - 10,
    )
    torn = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime={"status": "invalid", "error": "malformed"},
        ),
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
    )
    assert torn["state"] == "PROCESS_TREE_INVALID"
    assert torn["action"] == sup.WARN_ONLY
    assert torn["next_state"]["runtime_progress_sequence"] == 5

    lower = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                updated_age=1.0,
                progress_age=1.0,
                progress_sequence=4,
            ),
        ),
        {"agents": {"worker": torn["next_state"]}},
        now=NOW + 1,
        snapshot=_codex_forked_brain_snap(),
    )
    assert lower["state"] == "PROCESS_TREE_INVALID"
    assert lower["next_state"]["runtime_sequence_regressed"] is True
    assert lower["next_state"]["runtime_progress_sequence"] == 5


def test_wrapped_same_turn_sequence_regression_is_sticky_unknown() -> None:
    state = _wrap_ready(
        runtime_wrapper_generation="wrapper-1",
        runtime_turn_generation=1,
        runtime_progress_sequence=5,
        runtime_progress_seen_epoch=NOW - 10,
    )
    regressed_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=1.0,
            progress_age=1.0,
            progress_sequence=4,
        ),
    )
    first = _plan_wrap(
        regressed_report,
        {"agents": {"worker": state}},
        snapshot=_codex_forked_brain_snap(),
    )
    assert first["state"] == "CLI_CHILD_UNKNOWN"
    assert first["next_state"]["runtime_sequence_regressed"] is True
    assert first["next_state"]["runtime_progress_sequence"] == 5

    newer_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=1.0,
            progress_age=1.0,
            progress_sequence=6,
        ),
    )
    second = _plan_wrap(
        newer_report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=_codex_forked_brain_snap(),
    )
    assert second["state"] == "CLI_CHILD_UNKNOWN"
    assert second["action"] == sup.NONE


@pytest.mark.parametrize(
    ("plan_state", "expected"),
    [
        ("CLI_CHILD_DEAD", sup.AVAILABILITY_UNAVAILABLE),
        ("CLI_CHILD_STALLED", sup.AVAILABILITY_UNAVAILABLE),
        ("CLI_CHILD_UNKNOWN", sup.AVAILABILITY_UNKNOWN),
    ],
)
def test_wrapped_child_state_blocks_fresh_heartbeat_availability(
    plan_state: str,
    expected: str,
) -> None:
    availability = sup.project_coordination_availability(
        "worker",
        {"heartbeat_stale": False},
        {"state": plan_state},
        {"wrapped": True},
    )

    assert availability["state"] == expected
    assert availability["state"] != sup.AVAILABILITY_AVAILABLE


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
    _write_idle_wrapper_runtime(s)

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    worker = report["agents"]["worker"]
    assert worker["heartbeat_stale"] is True
    assert worker["health"]["state"] == hm.STATE_UNKNOWN
    assert worker["health"]["reason_code"] == "health_stale_ttl"
    assert worker["config_blocked_hold"]["agent"] == "worker"
    wrapper_generation = worker["wrapper_runtime"]["record"]["wrapper_generation"]

    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(
                                backoff_next_epoch=0,
                                runtime_wrapper_generation=wrapper_generation,
                            )}},
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
    _write_idle_wrapper_runtime(s)

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    assert report["agents"]["worker"].get("config_blocked_hold") is None
    wrapper_generation = report["agents"]["worker"]["wrapper_runtime"]["record"][
        "wrapper_generation"
    ]
    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(
                                backoff_next_epoch=0,
                                runtime_wrapper_generation=wrapper_generation,
                            )}},
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
    _write_idle_wrapper_runtime(s)

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    wrapper_generation = report["agents"]["worker"]["wrapper_runtime"]["record"][
        "wrapper_generation"
    ]
    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(
                                backoff_next_epoch=0,
                                runtime_wrapper_generation=wrapper_generation,
                            )}},
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
    _write_idle_wrapper_runtime(s)

    report = sup.build_report(s, now_epoch=NOW + 2500, supervisor_config=_WRAP_CONFIG)
    assert report["agents"]["worker"].get("config_blocked_hold") is None
    wrapper_generation = report["agents"]["worker"]["wrapper_runtime"]["record"][
        "wrapper_generation"
    ]
    plan = sup.plan_actions(report,
                            {"agents": {"worker": _wrap_ready(
                                backoff_next_epoch=0,
                                runtime_wrapper_generation=wrapper_generation,
                            )}},
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
    # discovery to preserve) while the strict bounded tree tracks the child.
    p = _plan_wrap(_report(heartbeat_stale=False),
                   {"agents": {"worker": _wrap_ready()}}, snapshot=_wrap_snap())
    assert p["action"] == sup.NONE and p["state"] == "HEALTHY_IDLE"
    assert p["next_state"]["brain_pid"] is None
    assert p["next_state"]["managed_pids"] == []
    assert [
        entry["pid"]
        for entry in p["next_state"]["owned_process_tree"]["entries"]
    ] == [WRAP_LAUNCHER_PID, WRAP_CHILD_PID]


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
    fields: dict = {"heartbeat_stale": stale}
    if stale:
        fields["restart_request"] = _auth_marker("rr-ownership")
    rpt = _report(**fields)
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


_OWNERSHIP_ATTR_CONFIG = {
    **_WRAP_CONFIG,
    "agents": {
        "worker": {
            "auto_restart": True,
            "cli": "codex",
            "activity_hook": True,
        }
    },
}


def _proc(
    pid: int,
    parent: int,
    name: str,
    command_line: str | None,
    start: str,
    *,
    start_filetime: str | None = None,
) -> dict:
    return {
        "pid": pid,
        "parent_pid": parent,
        "name": name,
        "command_line": command_line,
        "start_time": start,
        "start_filetime": (
            _test_start_filetime(start)
            if start_filetime is None
            else start_filetime
        ),
    }


def _wrap_cmd(*, root: str = TEST_ROOT, agent: str = "worker",
              nonce: str = SUPERVISOR_NONCE) -> str:
    return (
        "python -m agenttalk "
        f"--supervisor-launch-nonce {nonce} "
        f"--root {root} wrap --for {agent} --loop -- codex"
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
        _OWNERSHIP_ATTR_CONFIG,
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
            _OWNERSHIP_ATTR_CONFIG,
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
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert [t["pid"] for t in p["kill_targets"]] == [10, 11, 12]
    assert p["kill_targets"][1]["reason"] == "live_chain_descendant"
    assert p["kill_targets"][2]["reason"] == "live_chain_descendant"
    snapshot_by_pid = {row["pid"]: row for row in snap}
    assert all(
        target["start_filetime"]
        == snapshot_by_pid[target["pid"]]["start_filetime"]
        for target in p["kill_targets"]
    )


def test_process_ownership_fresh_kill_target_retains_snapshot_filetime() -> None:
    start = _ps_iso(100000)
    snapshot = [_proc(10, 1, "python.exe", _wrap_cmd(), start)]

    plan = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=start),
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snapshot,
    )["agents"]["worker"]

    target = next(item for item in plan["kill_targets"] if item["pid"] == 10)
    assert target["start_filetime"] == snapshot[0]["start_filetime"]


def _owned_tree_plan(
    snapshot: list[dict],
    *,
    request_id: str = "rr-owned-tree",
    runtime_overrides: dict | None = None,
) -> dict:
    report = _report(
        restart_request=_auth_marker(request_id),
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
            **(runtime_overrides or {}),
        ),
    )
    state = {
        "agents": {
            "worker": _wrap_ready(
                runtime_wrapper_generation="wrapper-1",
                backoff_next_epoch=0,
            )
        }
    }
    return _plan_wrap(report, state, snapshot=snapshot)


def test_owned_process_tree_crosses_shell_hosts_and_feeds_stop_tree_parent_first() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]

    plan = _owned_tree_plan(snapshot)

    tree = plan["next_state"]["owned_process_tree"]
    assert [(entry["pid"], entry["role"]) for entry in tree["entries"]] == [
        (WRAP_LAUNCHER_PID, "wrapper"),
        (WRAP_CHILD_PID, "cli_launcher"),
        (302, "cli_brain"),
        (303, "tool_descendant"),
        (304, "tool_descendant"),
    ]
    assert [entry["parent_pid"] for entry in tree["entries"]] == [
        1,
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        302,
        303,
    ]
    assert tree["status"] == "complete"
    assert tree["truncated"] is False
    assert tree["observed_count"] == tree["recorded_count"] == 5
    assert [target["pid"] for target in plan["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        302,
        303,
        304,
    ]


def test_owned_process_tree_kill_targets_retain_exact_start_filetime() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
    ]

    plan = _owned_tree_plan(snapshot, request_id="rr-exact-kill-target")
    entries = {
        entry["pid"]: entry
        for entry in plan["next_state"]["owned_process_tree"]["entries"]
    }

    assert plan["kill_targets"]
    assert all(
        target["start_filetime"] == entries[target["pid"]]["start_filetime"]
        for target in plan["kill_targets"]
    )


def test_owned_process_tree_accepts_exact_child_order_inside_same_iso_tick() -> None:
    snapshot = _wrap_snap()
    launcher = snapshot[-1]
    child = _proc(
        303,
        launcher["pid"],
        "node.exe",
        "node tool.js",
        launcher["start_time"],
        start_filetime=str(int(launcher["start_filetime"]) + 1),
    )

    plan = _owned_tree_plan(
        [*snapshot, child],
        request_id="rr-exact-child-order",
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [entry["pid"] for entry in tree["entries"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        child["pid"],
    ]


@pytest.mark.parametrize(
    ("filetime_delta", "reason_code"),
    [
        (0, "process_tree_invalid_equal_start_edge"),
        (-1, "process_tree_invalid_inverted_start_edge"),
    ],
    ids=["equal", "inverted"],
)
def test_owned_process_tree_rejects_nonincreasing_exact_child_order(
    filetime_delta: int,
    reason_code: str,
) -> None:
    snapshot = _wrap_snap()
    launcher = snapshot[-1]
    child = _proc(
        303,
        launcher["pid"],
        "node.exe",
        "node tool.js",
        _ps_iso(700000),
        start_filetime=str(int(launcher["start_filetime"]) + filetime_delta),
    )

    plan = _owned_tree_plan(
        [*snapshot, child],
        request_id="rr-inverted-exact-child-order",
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == reason_code
    assert plan["kill_targets"] == []


def test_strict_child_order_keeps_linux_token_exact_when_filetime_is_stale() -> None:
    boot_id = "12345678-1234-1234-1234-123456789abc"
    parent = {
        "pid": 10,
        "parent_pid": 1,
        "start_time": f"linux:{boot_id}:100",
        "start_filetime": "999",
    }
    child = {
        "pid": 11,
        "parent_pid": 10,
        "start_time": f"linux:{boot_id}:101",
        "start_filetime": "1",
    }

    assert sup._strict_owned_child_edge(parent, child) is None  # noqa: SLF001


def test_recorded_identity_prefers_linux_token_over_stale_filetime() -> None:
    boot_id = "12345678-1234-1234-1234-123456789abc"
    expected_start = f"linux:{boot_id}:100"
    recycled = {
        "start_time": f"linux:{boot_id}:101",
        "start_filetime": "999",
    }
    same = {
        "start_time": expected_start,
        "start_filetime": "1000",
    }

    assert sup._recorded_process_identity_state(  # noqa: SLF001
        recycled,
        expected_start=expected_start,
        expected_filetime="999",
    ) == "different"
    assert sup._recorded_process_identity_state(  # noqa: SLF001
        same,
        expected_start=expected_start,
        expected_filetime="999",
    ) == "same"


def test_valid_owned_process_tree_prefers_exact_child_order() -> None:
    snapshot = [
        *_wrap_snap(),
        _proc(303, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
    ]
    tree = _owned_tree_plan(
        snapshot,
        request_id="rr-valid-exact-order",
    )["next_state"]["owned_process_tree"]
    same_tick = json.loads(json.dumps(tree))
    parent, child = same_tick["entries"][-2:]
    child["start"] = parent["start"]
    child["start_filetime"] = str(int(parent["start_filetime"]) + 1)

    assert sup._valid_owned_process_tree(  # noqa: SLF001
        same_tick,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation=same_tick["wrapper_generation"],
        launch_nonce=same_tick["launch_nonce"],
    ) is not None


@pytest.mark.parametrize("status", ["complete", "absent"])
def test_valid_owned_process_tree_requires_filetime_for_windows_authority(
    status: str,
) -> None:
    tree = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-valid-authoritative-filetime",
    )["next_state"]["owned_process_tree"]
    tree["status"] = status
    tree["reason_code"] = "process_tree_absent" if status == "absent" else None
    tree["entries"][-1]["start_filetime"] = None

    assert sup._valid_owned_process_tree(  # noqa: SLF001
        tree,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation=tree["wrapper_generation"],
        launch_nonce=tree["launch_nonce"],
    ) is None


@pytest.mark.parametrize("status", ["invalid", "truncated"])
def test_valid_owned_process_tree_keeps_nullable_hold_evidence_readable(
    status: str,
) -> None:
    tree = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-valid-nullable-hold",
    )["next_state"]["owned_process_tree"]
    tree["status"] = status
    tree["reason_code"] = (
        "process_tree_invalid_exact_start_filetime_unavailable"
        if status == "invalid"
        else "process_tree_truncated"
    )
    tree["entries"][-1]["start_filetime"] = None
    if status == "truncated":
        tree["observed_count"] += 1
        tree["omitted_count"] = 1
        tree["truncated"] = True
        # The source tree was a genuine "complete" walk (walk_complete
        # True) - a real truncated result never carries that forward,
        # since its own omitted_count > 0 already contradicts it.
        tree.pop("walk_complete", None)

    assert sup._valid_owned_process_tree(  # noqa: SLF001
        tree,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation=tree["wrapper_generation"],
        launch_nonce=tree["launch_nonce"],
    ) is not None


def test_valid_owned_process_tree_keeps_nullable_hold_evidence_strictly_ordered() -> None:
    tree = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-valid-nullable-hold-order",
    )["next_state"]["owned_process_tree"]
    tree["status"] = "invalid"
    tree["reason_code"] = "process_tree_invalid_exact_start_filetime_unavailable"
    parent, child = tree["entries"][-2:]
    child["start"] = parent["start"]
    child["start_filetime"] = None

    assert sup._valid_owned_process_tree(  # noqa: SLF001
        tree,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation=tree["wrapper_generation"],
        launch_nonce=tree["launch_nonce"],
    ) is None


@pytest.mark.parametrize(
    "start_filetime",
    [None, str(1 << 64)],
    ids=["missing", "outside-uint64"],
)
def test_owned_process_tree_holds_when_windows_filetime_is_unavailable(
    start_filetime: str | None,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
    ]
    snapshot[-1]["start_filetime"] = start_filetime

    plan = _owned_tree_plan(snapshot, request_id="rr-missing-exact-filetime")

    assert plan["next_state"]["owned_process_tree"]["status"] == "invalid"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_exact_start_filetime_unavailable"
    )
    assert plan["kill_targets"] == []


def test_owned_process_tree_omits_absent_unexact_windows_launcher() -> None:
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            updated_age=60.0,
            progress_age=60.0,
            progress_sequence=2,
        ),
    )
    plan = _plan_wrap(
        report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=_wrap_snap()[:1],
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [entry["pid"] for entry in tree["entries"]] == [
        WRAP_LAUNCHER_PID,
    ]
    assert plan["state"] == "CLI_CHILD_MISSING"
    assert plan["kill_targets"] == []


def test_owned_process_tree_reserves_detached_gate_runner_without_name_inference() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(
            303,
            302,
            "python.exe",
            "python -m agenttalk dev-gate --profile release",
            _ps_iso(800000),
        ),
    ]
    first = _owned_tree_plan(snapshot, request_id="rr-gate-role-first")
    first_tree = first["next_state"]["owned_process_tree"]
    assert first_tree["entries"][-1]["role"] == "tool_descendant"

    registered = json.loads(json.dumps(first_tree))
    registered["entries"][-1]["role"] = "detached_gate_runner"
    validate = lambda value: sup._valid_owned_process_tree(  # noqa: E731, SLF001
        value,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation="wrapper-1",
        launch_nonce=SUPERVISOR_NONCE,
    )
    assert validate(registered) is not None

    unknown = json.loads(json.dumps(registered))
    unknown["entries"][-1]["role"] = "background_job"
    assert validate(unknown) is None

    prior_state = first["next_state"]
    prior_state["owned_process_tree"] = registered
    refreshed = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-gate-role-refresh"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": prior_state}},
        now=NOW + 1,
        snapshot=snapshot,
    )
    assert (
        refreshed["next_state"]["owned_process_tree"]["entries"][-1]["role"]
        == "detached_gate_runner"
    )


@pytest.mark.parametrize(
    ("descendant_count", "expected_truncated"),
    [(63, False), (64, True)],
    ids=["exact-cap", "cap-plus-one"],
)
def test_owned_process_tree_bound_holds_and_escalates_when_truncated(
    tmp_path: Path,
    descendant_count: int,
    expected_truncated: bool,
) -> None:
    snapshot = [_wrap_snap()[0]]
    parent = WRAP_LAUNCHER_PID
    for offset in range(descendant_count):
        pid = WRAP_CHILD_PID + offset
        snapshot.append(
            _proc(
                pid,
                parent,
                "codex.exe" if offset == 0 else "node.exe",
                "codex exec --json" if offset == 0 else f"node worker-{offset}.js",
                _ps_iso(600000 + offset * 1000),
            )
        )
        parent = pid

    plan = _owned_tree_plan(snapshot, request_id=f"rr-tree-{descendant_count}")
    tree = plan["next_state"]["owned_process_tree"]

    assert tree["observed_count"] == descendant_count + 1
    assert tree["recorded_count"] == min(descendant_count + 1, 64)
    assert tree["omitted_count"] == max(0, descendant_count + 1 - 64)
    assert tree["truncated"] is expected_truncated
    if not expected_truncated:
        assert tree["status"] == "complete"
        assert plan["action"] == sup.RELAUNCH
        assert len(plan["kill_targets"]) == 64
        return

    assert tree["status"] == "truncated"
    assert tree["reason_code"] == "process_tree_truncated"
    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_TRUNCATED"
    assert plan["notify"] is True
    assert plan["kill_first"] is False
    assert plan["kill_orphans"] is False
    assert plan["kill_targets"] == []
    assert "observed 65" in plan["reason"]
    assert "cap 64" in plan["reason"]

    store = _team(tmp_path)
    sup.save_supervisor_state(
        store.dir / "supervisor-state.json",
        {"agents": {"worker": plan["next_state"]}},
    )
    items = cli._collect_attention_items(  # noqa: SLF001 - integration boundary
        store,
        for_agent=None,
        roster=["lead", "worker"],
    )
    attention_item = next(
        item for item in items if item["item_id"] == "process_tree_hold:worker"
    )
    assert attention_item["source"] == "process_tree_hold"
    assert attention_item["human_can_unblock_now"] is True
    assert attention_item["risk_severity"] == "high"
    assert "observed 65" in attention_item["why_it_matters"]
    assert "Automatic teardown" in attention_item["why_it_matters"]
    assert "omits 1 observed identity" in attention_item["recommendation"]

    # A smaller reachable prefix does not prove the omitted identity ended.
    # Keep HOLD until an attended new launch/generation clears the record.
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-tree-truncated-again"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": plan["next_state"]}},
        now=NOW + 1,
        snapshot=[*snapshot[:-2], snapshot[-1]],
    )
    assert second["action"] == sup.WARN_ONLY
    assert second["state"] == "PROCESS_TREE_TRUNCATED"
    assert second["kill_targets"] == []
    assert second["next_state"]["owned_process_tree"] == tree


@pytest.mark.parametrize(
    ("state_overrides", "runtime_overrides", "snapshot_nonce"),
    [
        ({"launcher_start": _ps_iso(100000)}, {}, SUPERVISOR_NONCE),
        ({}, {"wrapper_start": _ps_iso(100000)}, SUPERVISOR_NONCE),
        ({}, {}, OTHER_NONCE),
    ],
    ids=["state-runtime-disagree", "runtime-live-disagree", "live-nonce-disagree"],
)
def test_owned_process_tree_requires_state_runtime_and_live_wrapper_agreement(
    state_overrides: dict,
    runtime_overrides: dict,
    snapshot_nonce: str,
) -> None:
    snapshot = _wrap_snap()
    snapshot[0]["command_line"] = _wrap_cmd(nonce=snapshot_nonce)
    report = _report(
        heartbeat_stale=True,
        heartbeat_age_seconds=3000.0,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
            **runtime_overrides,
        ),
    )
    state = _wrap_ready(**state_overrides)

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=snapshot,
    )

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["kill_targets"] == []
    assert plan["next_state"]["owned_process_tree"]["status"] == "invalid"


def test_owned_process_tree_emits_current_snapshot_start_representation() -> None:
    wrapper_runtime_start = "2026-07-04T07:20:31.500000+00:00"
    child_runtime_start = "2026-07-04T07:20:31.600000+00:00"
    report = _report(
        restart_request=_auth_marker("rr-current-start"),
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            wrapper_start=wrapper_runtime_start,
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=child_runtime_start,
        ),
    )
    state = _wrap_ready(
        launcher_start=wrapper_runtime_start,
        runtime_wrapper_generation="wrapper-1",
        backoff_next_epoch=0,
    )

    plan = _plan_wrap(
        report,
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(),
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert [entry["start"] for entry in tree["entries"]] == [
        WRAP_START,
        WRAP_CHILD_START,
    ]
    assert [target["start"] for target in plan["kill_targets"]] == [
        WRAP_START,
        WRAP_CHILD_START,
    ]


def test_owned_process_tree_refresh_removes_exited_leaf_and_preserves_discovery() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    first_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            now=NOW,
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
            progress_age=1.0,
            progress_sequence=2,
        ),
    )
    first = _plan_wrap(
        first_report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=snapshot,
    )
    first_tree = first["next_state"]["owned_process_tree"]
    discovered = {
        (entry["pid"], entry["start"]): entry["discovered_at"]
        for entry in first_tree["entries"]
    }

    second_report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            now=NOW + 1,
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
            progress_age=1.0,
            progress_sequence=3,
        ),
    )
    second = _plan_wrap(
        second_report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=snapshot[:-1],
    )
    second_tree = second["next_state"]["owned_process_tree"]

    assert [entry["pid"] for entry in second_tree["entries"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        302,
        303,
    ]
    assert second_tree["refreshed_at"] != first_tree["refreshed_at"]
    assert all(
        entry["discovered_at"] == discovered[(entry["pid"], entry["start"])]
        for entry in second_tree["entries"]
    )


def test_owned_process_tree_absent_launcher_refuses_unproven_pid_reuse_graft() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            990,
            WRAP_CHILD_PID,
            "node.exe",
            "node foreign.js",
            _ps_iso(900000),
        ),
    ]

    plan = _owned_tree_plan(
        snapshot,
        request_id="rr-virtual-graft",
        runtime_overrides={
            "launcher_exit_filetime": _ps_filetime(750000),
        },
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == (
        "process_tree_invalid_unproven_virtual_parent_descendant"
    )
    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["kill_targets"] == []


def test_owned_process_tree_exact_launcher_lifetime_proves_first_forked_child() -> None:
    plan = _owned_tree_plan(
        _codex_forked_brain_snap(),
        request_id="rr-certified-fork",
        runtime_overrides={
            "launcher_exit_filetime": _ps_filetime(750000),
        },
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [entry["pid"] for entry in tree["entries"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        WRAP_TUI_PID,
    ]
    assert [target["pid"] for target in plan["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_TUI_PID,
    ]
    assert plan["action"] == sup.RELAUNCH


def test_owned_process_tree_exact_launcher_lifetime_replaces_missing_start_token() -> None:
    snapshot = _codex_forked_brain_snap()
    snapshot.launcher_lifetime = {
        "source": wrt.LAUNCHER_LIFETIME_SOURCE,
        "creation_filetime": "134276232316000000",
        "exit_filetime": "134276232317500000",
    }
    snapshot[1]["start_filetime"] = "134276232317000000"
    runtime = _wrapper_runtime_view(
        phase="active",
        launcher_pid=WRAP_CHILD_PID,
        progress_sequence=2,
    )
    runtime["record"]["cli_launcher_start"] = None

    plan = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-certified-missing-start"),
            wrapper_runtime=runtime,
        ),
        {
            "agents": {
                "worker": _wrap_ready(
                    runtime_wrapper_generation="wrapper-1",
                    backoff_next_epoch=0,
                )
            }
        },
        snapshot=snapshot,
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    launcher = next(
        entry for entry in tree["entries"] if entry["role"] == "cli_launcher"
    )
    assert sup._start_tokens_match(  # noqa: SLF001
        launcher["start"], WRAP_CHILD_START
    )
    assert launcher["start_filetime"] == snapshot.launcher_lifetime[
        "creation_filetime"
    ]
    assert [target["pid"] for target in plan["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_TUI_PID,
    ]
    assert plan["action"] == sup.RELAUNCH


def test_owned_process_tree_missing_launcher_identity_without_certificate_is_hold() -> None:
    snapshot = list(_codex_forked_brain_snap())
    runtime = _wrapper_runtime_view(
        phase="active",
        launcher_pid=WRAP_CHILD_PID,
        progress_sequence=2,
    )
    runtime["record"]["cli_launcher_start"] = None

    plan = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-uncertified-missing-start"),
            wrapper_runtime=runtime,
        ),
        {
            "agents": {
                "worker": _wrap_ready(
                    runtime_wrapper_generation="wrapper-1",
                    backoff_next_epoch=0,
                )
            }
        },
        snapshot=snapshot,
    )

    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_launcher_identity_unavailable"
    )
    assert plan["kill_targets"] == []


def test_owned_process_tree_recycled_launcher_ignores_foreign_children() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            1,
            "unrelated.exe",
            "unrelated replacement",
            _ps_iso(800000),
        ),
        _proc(
            990,
            WRAP_CHILD_PID,
            "node.exe",
            "node foreign.js",
            _ps_iso(900000),
        ),
    ]

    plan = _owned_tree_plan(
        snapshot,
        request_id="rr-recycled-exited-launcher",
        runtime_overrides={
            "launcher_exit_filetime": _ps_filetime(750000),
        },
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [entry["pid"] for entry in tree["entries"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
    ]
    assert [target["pid"] for target in plan["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
    ]
    assert plan["action"] == sup.RELAUNCH


def test_owned_process_tree_recycled_launcher_collision_uses_exact_filetime() -> None:
    recorded_filetime = _ps_filetime(600000)
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            1,
            "unrelated.exe",
            "unrelated replacement",
            WRAP_CHILD_START,
            start_filetime=str(int(recorded_filetime) + 10),
        ),
    ]

    plan = _owned_tree_plan(
        snapshot,
        request_id="rr-recycled-launcher-collision",
        runtime_overrides={
            "launcher_creation_filetime": recorded_filetime,
            "launcher_exit_filetime": _ps_filetime(750000),
        },
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [target["pid"] for target in plan["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
    ]
    assert plan["action"] == sup.RELAUNCH


def test_owned_process_tree_ambiguous_launcher_keeps_parent_mismatch_hold() -> None:
    recorded_filetime = _ps_filetime(600000)
    ambiguous_launcher = _proc(
        WRAP_CHILD_PID,
        1,
        "codex.exe",
        "codex exec --json",
        WRAP_CHILD_START,
    )
    ambiguous_launcher["start_filetime"] = None

    plan = _owned_tree_plan(
        [_wrap_snap()[0], ambiguous_launcher],
        request_id="rr-ambiguous-launcher-parent-mismatch",
        runtime_overrides={
            "launcher_creation_filetime": recorded_filetime,
            "launcher_exit_filetime": _ps_filetime(750000),
        },
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == "process_tree_invalid_launcher_parent_mismatch"
    assert plan["kill_targets"] == []


@pytest.mark.parametrize(
    "child_filetime",
    [
        None,
        _ps_filetime(600000),
        _ps_filetime(750000),
        _ps_filetime(750001),
    ],
    ids=["missing", "equals-creation", "equals-exit", "after-exit"],
)
def test_owned_process_tree_launcher_lifetime_boundaries_fail_closed(
    child_filetime: str | None,
) -> None:
    snapshot = _codex_forked_brain_snap()
    snapshot[1]["start_filetime"] = child_filetime

    plan = _owned_tree_plan(
        snapshot,
        request_id="rr-lifetime-boundary",
        runtime_overrides={
            "launcher_exit_filetime": _ps_filetime(750000),
        },
    )

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["kill_targets"] == []


def test_owned_process_tree_prior_identity_bridges_exited_intermediate() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    first = _owned_tree_plan(snapshot, request_id="rr-earned-tree")
    second_report = _report(
        restart_request=_auth_marker("rr-bridge-tree"),
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            now=NOW + 1,
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
            progress_sequence=2,
        ),
    )

    second = _plan_wrap(
        second_report,
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[snapshot[0], snapshot[1], snapshot[2], snapshot[4]],
    )

    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [entry["pid"] for entry in tree["entries"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        302,
        303,
        304,
    ]
    assert [target["pid"] for target in second["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        302,
        304,
    ]
    assert second["action"] == sup.RELAUNCH


def test_owned_process_tree_linux_prior_identity_bridges_exited_intermediate() -> None:
    boot_id = "12345678-1234-1234-1234-123456789abc"
    wrapper_start = f"linux:{boot_id}:90"
    launcher_start = f"linux:{boot_id}:100"
    parent_start = f"linux:{boot_id}:101"
    child_start = f"linux:{boot_id}:102"
    wrapper = {
        **_wrap_snap()[0],
        "start_time": wrapper_start,
        "start_filetime": None,
    }
    snapshot = [
        wrapper,
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            launcher_start,
        ),
        _proc(303, WRAP_CHILD_PID, "pwsh", "pwsh tool.ps1", parent_start),
        _proc(304, 303, "node", "node repl.js", child_start),
    ]
    first = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-linux-earned-tree"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                wrapper_start=wrapper_start,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=launcher_start,
                progress_sequence=2,
            ),
        ),
        {
            "agents": {
                "worker": _wrap_ready(
                    launcher_start=wrapper_start,
                    backoff_next_epoch=0,
                )
            }
        },
        snapshot=snapshot,
    )
    first_tree = first["next_state"]["owned_process_tree"]
    prior_child = next(entry for entry in first_tree["entries"] if entry["pid"] == 304)
    prior_child["start_filetime"] = "999"

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-linux-bridge-tree"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                wrapper_start=wrapper_start,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=launcher_start,
                progress_sequence=3,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[snapshot[0], snapshot[1], snapshot[3]],
    )

    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [entry["pid"] for entry in tree["entries"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        303,
        304,
    ]
    assert [target["pid"] for target in second["kill_targets"]] == [
        WRAP_LAUNCHER_PID,
        WRAP_CHILD_PID,
        304,
    ]


def test_owned_process_tree_recycled_virtual_parent_ignores_replacement_child() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node old.js", _ps_iso(800200)),
    ]
    first = _owned_tree_plan(
        snapshot,
        request_id="rr-earned-recycled-parent",
    )
    replacement = _proc(
        303,
        1,
        "unrelated.exe",
        "unrelated replacement",
        _ps_iso(800500),
    )
    assert sup._start_tokens_match(  # noqa: SLF001
        replacement["start_time"],
        snapshot[3]["start_time"],
    )
    second_snapshot = [
        snapshot[0],
        snapshot[1],
        snapshot[2],
        replacement,
        snapshot[4],
        _proc(305, 303, "node.exe", "node new.js", _ps_iso(800600)),
    ]

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-recycled-parent"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )

    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [(entry["pid"], entry["start"]) for entry in tree["entries"]] == [
        (WRAP_LAUNCHER_PID, WRAP_START),
        (WRAP_CHILD_PID, WRAP_CHILD_START),
        (302, _ps_iso(700000)),
        (303, _ps_iso(800000)),
        (304, _ps_iso(800200)),
    ]
    assert [(target["pid"], target["start"]) for target in second["kill_targets"]] == [
        (WRAP_LAUNCHER_PID, WRAP_START),
        (WRAP_CHILD_PID, WRAP_CHILD_START),
        (302, _ps_iso(700000)),
        (304, _ps_iso(800200)),
    ]
    assert second["action"] == sup.RELAUNCH


def test_owned_process_tree_live_parent_adopts_recycled_child_pid() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh old.ps1", _ps_iso(800000)),
    ]
    first = _owned_tree_plan(
        snapshot,
        request_id="rr-live-parent-recycled-child",
    )
    replacement = _proc(
        303,
        302,
        "pwsh.exe",
        "pwsh new.ps1",
        _ps_iso(850000),
    )

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-live-parent-new-child"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[snapshot[0], snapshot[1], snapshot[2], replacement],
    )

    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert [(entry["pid"], entry["start"]) for entry in tree["entries"]] == [
        (WRAP_LAUNCHER_PID, WRAP_START),
        (WRAP_CHILD_PID, WRAP_CHILD_START),
        (302, _ps_iso(700000)),
        (303, replacement["start_time"]),
    ]
    assert [(target["pid"], target["start"]) for target in second["kill_targets"]] == [
        (WRAP_LAUNCHER_PID, WRAP_START),
        (WRAP_CHILD_PID, WRAP_CHILD_START),
        (302, _ps_iso(700000)),
        (303, replacement["start_time"]),
    ]
    assert second["action"] == sup.RELAUNCH


def test_owned_process_tree_recycled_child_resets_discovery_time() -> None:
    snapshot = [
        *_wrap_snap(),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh old.ps1", _ps_iso(800000)),
    ]
    first = _owned_tree_plan(
        snapshot,
        request_id="rr-recycled-child-discovery",
    )
    prior_entries = {
        entry["pid"]: entry
        for entry in first["next_state"]["owned_process_tree"]["entries"]
    }
    prior = next(
        entry
        for entry in first["next_state"]["owned_process_tree"]["entries"]
        if entry["pid"] == 303
    )
    replacement = _proc(
        303,
        302,
        "pwsh.exe",
        "pwsh new.ps1",
        prior["start"],
        start_filetime=str(int(prior["start_filetime"]) + 10),
    )

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-recycled-child-discovery-2"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[*snapshot[:-1], replacement],
    )

    second_tree = second["next_state"]["owned_process_tree"]
    current = next(
        entry
        for entry in second_tree["entries"]
        if entry["pid"] == replacement["pid"]
    )
    assert current["start_filetime"] == replacement["start_filetime"]
    assert current["discovered_at"] == second_tree["refreshed_at"]
    current_entries = {entry["pid"]: entry for entry in second_tree["entries"]}
    for pid in (WRAP_LAUNCHER_PID, WRAP_CHILD_PID, 302):
        assert current_entries[pid]["discovered_at"] == prior_entries[pid]["discovered_at"]


def test_owned_process_tree_virtual_parent_rejects_new_unearned_child() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    first = _owned_tree_plan(snapshot, request_id="rr-earned-parent")
    second_snapshot = [
        snapshot[0],
        snapshot[1],
        snapshot[2],
        snapshot[4],
        _proc(305, 303, "node.exe", "node foreign.js", _ps_iso(910000)),
    ]

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-unearned-child"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )

    assert second["next_state"]["owned_process_tree"]["status"] == "invalid"
    assert second["state"] == "PROCESS_TREE_INVALID"
    assert second["kill_targets"] == []


def test_owned_process_tree_reparented_prior_identity_is_hold() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    first = _owned_tree_plan(snapshot, request_id="rr-reparent-prior")
    reparented_leaf = dict(snapshot[4])
    reparented_leaf["parent_pid"] = 1

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-reparented-leaf"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[snapshot[0], snapshot[1], snapshot[2], reparented_leaf],
    )

    assert second["action"] == sup.WARN_ONLY
    assert second["state"] == "PROCESS_TREE_INVALID"
    assert second["kill_targets"] == []
    assert second["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_prior_parent_drift"
    )


def test_owned_process_tree_wrapper_exit_with_live_prior_leaf_is_hold() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    first = _owned_tree_plan(snapshot, request_id="rr-wrapper-exit-prior")

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-wrapper-exit-live-leaf"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[snapshot[4]],
    )

    assert second["action"] == sup.WARN_ONLY
    assert second["state"] == "PROCESS_TREE_INVALID"
    assert second["kill_first"] is False
    assert second["kill_targets"] == []
    assert second["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_wrapper_absent_live_descendant"
    )


def test_owned_process_tree_unrecognized_generation_holds_orphan_child() -> None:
    child = _proc(
        WRAP_CHILD_PID,
        WRAP_LAUNCHER_PID,
        "codex.exe",
        "codex exec --json",
        WRAP_CHILD_START,
    )
    report = _report(
        restart_request=_auth_marker("rr-first-generation-orphan"),
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
        ),
    )
    state = {
        "agents": {
            "worker": _wrap_ready(
                runtime_wrapper_generation=None,
                backoff_next_epoch=0,
            )
        }
    }

    adoption = _plan_wrap(report, state, snapshot=[child])

    assert adoption["action"] == sup.WARN_ONLY
    assert adoption["state"] == "PROCESS_TREE_INVALID"
    assert adoption["kill_targets"] == []
    assert adoption["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_wrapper_absent_live_descendant"
    )
    assert adoption["next_state"]["runtime_wrapper_generation"] is None

    absent = _plan_wrap(
        report,
        {"agents": {"worker": adoption["next_state"]}},
        now=NOW + 1,
        snapshot=[child],
    )

    assert absent["action"] == sup.WARN_ONLY
    assert absent["kill_targets"] == []
    assert absent["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_wrapper_absent_live_descendant"
    )


def test_owned_process_tree_unrecognized_generation_does_not_adopt_before_relaunch() -> None:
    report = _report(
        heartbeat_stale=True,
        wrapper_runtime=_wrapper_runtime_view(
            phase="idle",
            wrapper_generation="wrapper-2",
        ),
    )
    state = {
        "agents": {
            "worker": _wrap_ready(
                runtime_wrapper_generation="wrapper-1",
                backoff_next_epoch=0,
            ),
        },
    }

    adoption = _plan_wrap(report, state, snapshot=[])

    assert adoption["action"] == sup.STUCK_RECOVER
    assert adoption["state"] == "STUCK_OR_DEAD"
    assert adoption["kill_targets"] == []
    assert adoption["next_state"]["runtime_wrapper_generation"] == "wrapper-1"


def test_owned_process_tree_adopted_generation_missing_record_holds_deep_orphan() -> None:
    orphan = _proc(
        404,
        403,
        "node.exe",
        "node orphaned-tool.js",
        _ps_iso(950000),
    )
    report = _report(
        heartbeat_stale=True,
        wrapper_runtime=_wrapper_runtime_view(
            phase="active",
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
        ),
    )

    held = _plan_wrap(
        report,
        {
            "agents": {
                "worker": _wrap_ready(
                    runtime_wrapper_generation="wrapper-1",
                    owned_process_tree_pending=False,
                    backoff_next_epoch=0,
                ),
            },
        },
        snapshot=[orphan],
    )

    assert held["action"] == sup.WARN_ONLY
    assert held["state"] == "PROCESS_TREE_INVALID"
    assert held["kill_targets"] == []
    assert held["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_legacy_managed_pids"
    )
    assert held["next_state"]["legacy_process_evidence"]["entries"][0] == {
        "pid": WRAP_LAUNCHER_PID,
        "start": WRAP_START,
        "source": "wrapper",
    }


def test_owned_process_tree_first_upgrade_holds_legacy_reparented_identity() -> None:
    orphan_start = _ps_iso(950000)
    orphan = _proc(
        404,
        1,
        "node.exe",
        "node legacy-orphan.js",
        orphan_start,
    )
    state = _wrap_ready(
        owned_process_tree_pending=False,
        managed_pids=[{
            "pid": 404,
            "start": orphan_start,
            "kind": "legacy",
        }],
        backoff_next_epoch=0,
    )

    held = _plan_wrap(
        _report(
            heartbeat_stale=True,
            wrapper_runtime=_wrapper_runtime_view(phase="idle"),
        ),
        {"agents": {"worker": state}},
        snapshot=[orphan],
    )

    assert held["action"] == sup.WARN_ONLY
    assert held["state"] == "PROCESS_TREE_INVALID"
    assert held["kill_targets"] == []
    assert held["next_state"]["managed_pids"] == []
    assert held["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_legacy_managed_pids"
    )
    evidence = held["next_state"]["legacy_process_evidence"]
    assert evidence["status"] == "migration_hold"
    assert evidence["entries"] == [
        {
            "pid": WRAP_LAUNCHER_PID,
            "start": WRAP_START,
            "source": "wrapper",
        },
        {
            "pid": 404,
            "start": orphan_start,
            "source": "managed_pids",
        },
    ]

    held_again = _plan_wrap(
        _report(
            heartbeat_stale=True,
            wrapper_runtime=_wrapper_runtime_view(phase="idle"),
        ),
        {"agents": {"worker": held["next_state"]}},
        now=NOW + 1,
        snapshot=[orphan],
    )
    assert held_again["action"] == sup.WARN_ONLY
    assert held_again["next_state"]["legacy_process_evidence"] == evidence

    attended = {"agents": {"worker": held_again["next_state"]}}
    sup.reset_process_tree_ownership_after_attended_teardown(
        attended,
        "worker",
        hold_source_hash="a" * 64,
        acknowledged_by="lead",
        verified_launch_nonce=SUPERVISOR_NONCE,
        expected_root=TEST_ROOT,
        runtime_record=_wrapper_runtime_view()["record"],
        recorded_identities_gone=True,
        reason="attended legacy migration teardown",
        now_epoch=NOW + 1,
    )
    assert attended["process_tree_resets"][-1] == {
        "schema_version": 1,
        "agent": "worker",
        "hold_source_hash": "a" * 64,
        "acknowledged_by": "lead",
        "verified_launch_nonce": SUPERVISOR_NONCE,
        "verified_identity_count": 2,
        "acknowledged_at": _iso(NOW + 1),
        "reason": "attended legacy migration teardown",
        "previous": {
            "tree_status": "invalid",
            "tree_reason_code": "process_tree_invalid_legacy_managed_pids",
            "wrapper_generation": "wrapper-1",
            "launch_nonce": SUPERVISOR_NONCE,
            "legacy_source_hash": evidence["source_hash"],
        },
    }
    sup.record_launch(
        attended,
        "worker",
        cli="codex",
        pid=500,
        pid_start=_ps_iso(990000),
        now_epoch=NOW + 2,
        cfg_agent={"wrapped": True},
        launcher_nonce=OTHER_NONCE,
        launcher_nonce_injected=True,
    )
    assert "legacy_process_evidence" not in attended["agents"]["worker"]
    assert "owned_process_tree" not in attended["agents"]["worker"]


def _write_attended_process_tree_reset_fixture(store: Store) -> tuple[dict, str]:
    plan = _owned_tree_plan(_wrap_snap())
    entry = json.loads(json.dumps(plan["next_state"]))
    tree = entry["owned_process_tree"]
    tree.update({
        "root_key": sup._root_key(str(store.root.resolve())),
        "status": "truncated",
        "reason_code": "process_tree_truncated",
        "observed_count": tree["recorded_count"] + 1,
        "omitted_count": 1,
        "truncated": True,
    })
    # The source tree was a genuine "complete" walk (walk_complete True),
    # but truncating it by hand here does not re-run that walk - a real
    # truncated result would never carry walk_complete True forward (its
    # own omitted_count > 0 already contradicts it). Same "a producer that
    # doesn't walk must not claim to have walked" rule as the fix itself.
    tree.pop("walk_complete", None)
    state = {"agents": {"worker": entry}}
    state_path = store.dir / "supervisor-state.json"
    sup.save_supervisor_state(state_path, state)
    writer = wrt.WrapperRuntimeWriter(
        store.state_dir,
        "worker",
        "wrapper-1",
        wrapper_pid=WRAP_LAUNCHER_PID,
        # Runtime APIs can report six fractional digits while CIM reports
        # seven; reset agreement must compare semantic start tokens.
        wrapper_start="2026-07-04T07:20:31.500000+00:00",
        clock=lambda: NOW,
    )
    writer.idle()
    from agenttalk import attention as attention_mod

    hold = attention_mod.process_tree_hold_items(state)[0]
    return state, hold["source_hash"]


def _current_configured_reset_item(
    store: Store,
    state: dict,
    *,
    identity_gone,
) -> dict:
    restart_marker = store.read_restart_request("worker")
    restart_requests = (
        {"worker": restart_marker}
        if isinstance(restart_marker, dict)
        else {}
    )
    admissions = sup.evaluate_process_tree_reset_admissions(
        store,
        state,
        actor="lead",
        now_epoch=NOW,
        identity_gone=identity_gone,
    )
    return att.process_tree_hold_items(
        state,
        supervisor_config=cli._load_supervisor_config(store),  # noqa: SLF001
        root=store.root,
        restart_requests=restart_requests,
        reset_admissions=admissions,
    )[0]


def _attended_process_tree_reset_args(source_hash: str) -> list[str]:
    return [
        "supervise",
        "--reset-process-tree-ownership",
        "--for",
        "worker",
        "--hold-source-hash",
        source_hash,
        "--verified-launch-nonce",
        SUPERVISOR_NONCE,
        "--acknowledge-no-live-supervisor",
        "--acknowledge-owned-processes-stopped",
        "--reason",
        "all recorded identities verified stopped",
        "--from",
        "lead",
        "--now",
        str(NOW),
    ]


def test_process_tree_reset_evidence_preserves_exact_filetime(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)

    evidence = sup.process_tree_ownership_reset_evidence(
        state,
        "worker",
        expected_root=store.root,
        verified_launch_nonce=SUPERVISOR_NONCE,
        runtime_record=_wrapper_runtime_view()["record"],
        now_epoch=NOW,
    )

    tree_entries = state["agents"]["worker"]["owned_process_tree"]["entries"]
    projected = [
        row for row in evidence["identities"]
        if row["source"] == "owned_process_tree"
    ]
    assert [
        (row["pid"], row["start"], row.get("start_filetime"))
        for row in projected
    ] == [
        (row["pid"], row["start"], row.get("start_filetime"))
        for row in tree_entries
    ]


def test_process_tree_unknown_admits_configured_attended_reset_with_independent_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    state["agents"]["worker"]["wrapper_recognition"] = {
        "status": "unknown",
        "reason_code": "command_line_unreadable",
    }
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    def identity_gone(_pid, _start, _filetime=None) -> bool:
        return True
    monkeypatch.setattr(cli, "_owner_identity_gone", identity_gone)
    monkeypatch.setattr(cli.time, "time", lambda: NOW)

    blocked = sup.evaluate_process_tree_reset_admissions(
        store,
        state,
        actor="lead",
        now_epoch=NOW,
        identity_gone=identity_gone,
    )
    assert blocked["blocked_admissions"]["worker"] == {
        "mode": "configured_reset",
        "agent": "worker",
        "missing_precondition": "supervisor_kill_switch_absent",
    }

    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    admissions = sup.evaluate_process_tree_reset_admissions(
        store,
        state,
        actor="lead",
        now_epoch=NOW,
        identity_gone=identity_gone,
    )
    assert admissions["admissions"]["worker"]["mode"] == "configured_reset"

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=identity_gone,
    )
    assert cli.main(item["operator_argv"][1:]) == 0
    persisted = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    assert "wrapper_recognition" not in persisted["agents"]["worker"]
    assert persisted["agents"]["worker"]["owned_process_tree_pending"] is True


@pytest.mark.parametrize("blocked_by", ["live_identity", "generation_mismatch"])
def test_process_tree_unknown_attended_reset_still_requires_independent_evidence(
    tmp_path: Path,
    blocked_by: str,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    entry = state["agents"]["worker"]
    entry["wrapper_recognition"] = {
        "status": "unknown",
        "reason_code": "command_line_unreadable",
    }
    identities_gone = blocked_by != "live_identity"
    if blocked_by == "generation_mismatch":
        entry["runtime_wrapper_generation"] = "wrapper-other"

    admissions = sup.evaluate_process_tree_reset_admissions(
        store,
        state,
        actor="lead",
        now_epoch=NOW,
        identity_gone=(
            lambda _pid, _start, _filetime=None: identities_gone
        ),
    )
    assert admissions["admissions"] == {}

    before = json.loads(json.dumps(state))
    expected_error = (
        "every recorded pid/start identity"
        if blocked_by == "live_identity"
        else "does not agree on a valid root, generation"
    )
    with pytest.raises(ValueError, match=expected_error):
        sup.reset_process_tree_ownership_after_attended_teardown(
            state,
            "worker",
            hold_source_hash="a" * 64,
            acknowledged_by="lead",
            verified_launch_nonce=SUPERVISOR_NONCE,
            expected_root=store.root,
            runtime_record=_wrapper_runtime_view()["record"],
            recorded_identities_gone=identities_gone,
            reason="independent reset evidence remains incomplete",
            now_epoch=NOW,
        )
    assert state == before


def test_process_tree_unknown_ephemeral_reset_evidence_remains_refused() -> None:
    request_id = "lr-unknown"
    state = {
        "ephemeral_reviewers": {
            "active": {
                request_id: {
                    "request_id": request_id,
                    "agent": "reviewer",
                    "wrapper_recognition": {
                        "status": "unknown",
                        "reason_code": "command_line_unreadable",
                    },
                },
            },
        },
    }

    with pytest.raises(ValueError, match="recognition is unknown and retryable"):
        sup.process_tree_ownership_reset_evidence(
            state,
            "reviewer",
            request_id=request_id,
            expected_root=TEST_ROOT,
            verified_launch_nonce=SUPERVISOR_NONCE,
            runtime_record={},
            now_epoch=NOW,
        )


def test_live_supervisor_hides_reset_remedy_and_rejects_previously_admitted_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    def identity_gone(_pid, _start, _start_filetime=None) -> bool:
        return True
    monkeypatch.setattr(cli, "_owner_identity_gone", identity_gone)
    monkeypatch.setattr(cli.time, "time", lambda: NOW)

    admitted = _current_configured_reset_item(
        store,
        state,
        identity_gone=identity_gone,
    )
    assert admitted["operator_argv"]

    store.supervisor_instance_path().write_text(
        json.dumps({
            "root": str(store.root),
            "pid": 999999,
            "pid_start": "linux:0123456789abcdef0123456789abcdef:1",
            "token": "b" * 32,
            "started_at": _iso(NOW),
        }),
        encoding="utf-8",
    )
    refused = _current_configured_reset_item(
        store,
        state,
        identity_gone=identity_gone,
    )

    assert "operator_argv" not in refused
    assert "no scripted remedy applies in this state" in refused["recommendation"]
    assert cli.main(admitted["operator_argv"][1:]) == 3


def test_malformed_reset_audit_hides_command_that_handler_would_refuse(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state["process_tree_resets"] = [{"malformed": True}]

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=lambda _pid, _start, _start_filetime=None: True,
    )

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


@pytest.mark.parametrize(
    "persistence_poison",
    ["escaped-\ud800-surrogate", float("nan")],
    ids=["unpaired-surrogate", "non-finite-number"],
)
def test_unpersistable_supervisor_state_hides_every_mutating_remedy(
    tmp_path: Path,
    persistence_poison: object,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state["unrelated_persistence_poison"] = persistence_poison

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=lambda _pid, _start, _start_filetime=None: True,
    )

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


def test_reset_admission_rechecks_kill_switch_after_identity_probe(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    kill_switch = store.dir / "supervisor.kill"
    kill_switch.write_text("stop", encoding="utf-8")

    def remove_kill_switch(_pid, _start, _start_filetime=None) -> bool:
        if kill_switch.exists():
            kill_switch.unlink()
        return True

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=remove_kill_switch,
    )

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert ".agenttalk/supervisor.kill" in item["recommendation"]
    assert "absent" in item["recommendation"]


def test_missing_kill_switch_names_the_only_operator_satisfiable_precondition(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=lambda _pid, _start, _start_filetime=None: True,
    )

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert ".agenttalk/supervisor.kill" in item["recommendation"]
    assert "absent" in item["recommendation"]
    assert "while the supervisor remains stopped" in item["recommendation"]

    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    admitted = _current_configured_reset_item(
        store,
        state,
        identity_gone=lambda _pid, _start, _start_filetime=None: True,
    )
    assert admitted["operator_argv"]
    assert ".agenttalk/supervisor.kill" not in admitted["recommendation"]


def test_missing_kill_switch_is_not_named_when_recorded_identity_is_live(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    probed: list[int] = []

    def identity_still_live(pid, _start, _start_filetime=None) -> bool:
        probed.append(pid)
        return False

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=identity_still_live,
    )

    assert probed
    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert ".agenttalk/supervisor.kill" not in item["recommendation"]


def test_malformed_ephemeral_journal_does_not_erase_configured_refusal_card(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    state["ephemeral_reviewers"] = {
        "active": {
            "lr-bad-journal": {
                "request_id": "lr-bad-journal",
                "agent": "reviewer",
                "owned_process_tree": {"status": "invalid"},
                "held_terminal": {
                    "terminal_state": eph.STATE_TIMED_OUT,
                    "reason": "ephemeral reviewer timed out without a result",
                    "completion": {
                        "status": eph.COMPLETION_NONE,
                        "terminal": False,
                        "hold": True,
                    },
                },
            },
        },
        "attended_archive_pending": [],
    }
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)

    items = cli._collect_attention_items(  # noqa: SLF001
        store,
        for_agent="lead",
        roster=["lead", "worker"],
    )

    assert any(
        item.get("item_id") == "process_tree_hold:worker"
        for item in items
    )
    assert not any(
        item.get("item_id") == "source_error:process_tree_hold"
        for item in items
    )


@pytest.mark.parametrize("surface", ["cli", "web"])
def test_oversized_process_identity_keeps_hold_visible_on_attention_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    from agenttalk import store as store_mod
    from agenttalk import web

    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    oversized_pid = 1 << 100
    entry = state["agents"]["worker"]
    entry["launcher_pid"] = oversized_pid
    tree = entry["owned_process_tree"]
    tree["entries"][0]["pid"] = oversized_pid
    tree["entries"][0]["start_filetime"] = None
    tree["entries"][1]["parent_pid"] = oversized_pid
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)

    runtime_path = wrt.runtime_path(store.state_dir, "worker")
    runtime_record = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_record["wrapper_pid"] = oversized_pid
    runtime_path.write_text(json.dumps(runtime_record), encoding="utf-8")

    probed: list[int] = []

    class PosixOverflowOS:
        name = "posix"

        def kill(self, pid: int, signal_number: int) -> None:
            probed.append(pid)
            raise OverflowError("pid does not fit pid_t")

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

    monkeypatch.setattr(store_mod, "os", PosixOverflowOS())
    if surface == "cli":
        items = cli._collect_attention_items(  # noqa: SLF001
            store,
            for_agent="lead",
            roster=["lead", "worker"],
        )
    else:
        items = web._collect_web_attention_items(  # noqa: SLF001
            store,
            ["lead", "worker"],
            "lead",
        )

    assert oversized_pid in probed
    hold_items = [
        item for item in items
        if item.get("item_id") == "process_tree_hold:worker"
    ]
    assert len(hold_items) == 1
    assert "operator_argv" not in hold_items[0]
    assert not any(
        item.get("item_id") == "source_error:process_tree_hold"
        for item in items
    )


def test_out_of_range_filetime_keeps_hold_visible_and_blocks_reset_admission(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    impossible_filetime = str(1 << 64)
    state["agents"]["worker"]["owned_process_tree"]["entries"][-1][
        "start_filetime"
    ] = impossible_filetime
    probed: list[tuple[int, str, str | None]] = []

    def identity_gone(
        pid: int,
        start: str,
        start_filetime: str | None = None,
    ) -> bool:
        probed.append((pid, start, start_filetime))
        return True

    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=identity_gone,
    )

    assert item["item_id"] == "process_tree_hold:worker"
    assert "operator_argv" not in item
    assert probed == []


def test_out_of_range_filetime_is_preserved_when_reset_handler_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state["agents"]["worker"]["owned_process_tree"]["entries"][-1][
        "start_filetime"
    ] = str(1 << 64)
    state_path = store.dir / "supervisor-state.json"
    sup.save_supervisor_state(state_path, state)
    source_hash = att.process_tree_hold_items(state)[0]["source_hash"]
    before = state_path.read_bytes()
    probed: list[tuple[int, str, str | None]] = []

    def identity_gone(
        pid: int,
        start: str,
        start_filetime: str | None = None,
    ) -> bool:
        probed.append((pid, start, start_filetime))
        return True

    monkeypatch.setattr(cli, "_owner_identity_gone", identity_gone)

    assert _run(_attended_process_tree_reset_args(source_hash), tmp_path) == 3
    assert probed == []
    assert state_path.read_bytes() == before


def test_attended_process_tree_reset_is_audited_and_rearms_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    probed_identities: list[tuple[int, str, str | None]] = []

    def identity_gone(
        pid: int,
        start: str,
        start_filetime: str | None = None,
    ) -> bool:
        probed_identities.append((pid, start, start_filetime))
        return True

    monkeypatch.setattr(cli, "_owner_identity_gone", identity_gone)
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=identity_gone,
    )
    source_hash = item["source_hash"]

    assert item["operator_argv"][5:7] == ["--for", "worker"]
    assert cli.main(item["operator_argv"][1:]) == 0

    expected_identities = [
        (row["pid"], row["start"], row.get("start_filetime"))
        for row in state["agents"]["worker"]["owned_process_tree"]["entries"]
    ]
    # Projection proves admission, the command rebuilds that projection under
    # the lifecycle lock, and the mutation path independently rechecks it.
    assert probed_identities == expected_identities * 3

    persisted = sup.load_supervisor_state(
        store.dir / "supervisor-state.json"
    )
    entry = persisted["agents"]["worker"]
    assert entry["owned_process_tree_pending"] is True
    assert entry["managed_pids"] == []
    assert "owned_process_tree" not in entry
    assert "legacy_process_evidence" not in entry
    assert "launcher_nonce" not in entry
    revoked = entry["revoked_wrapper_runtime"]
    assert revoked == {
        "schema_version": 1,
        "agent": "worker",
        "root_key": sup._root_key(str(store.root.resolve())),
        "wrapper_pid": WRAP_LAUNCHER_PID,
        "wrapper_start": "2026-07-04T07:20:31.500000+00:00",
        "wrapper_generation": "wrapper-1",
        "launch_nonce": SUPERVISOR_NONCE,
        "runtime_record_digest": revoked["runtime_record_digest"],
        "hold_source_hash": source_hash,
        "revoked_at": _iso(NOW),
    }
    assert re.fullmatch(r"[0-9a-f]{64}", revoked["runtime_record_digest"])
    audit = persisted["process_tree_resets"][-1]
    assert audit["acknowledged_by"] == "lead"
    assert audit["hold_source_hash"] == source_hash
    assert audit["verified_launch_nonce"] == SUPERVISOR_NONCE
    assert audit["verified_identity_count"] == 2

    from agenttalk import attention as attention_mod

    assert attention_mod.process_tree_hold_items(persisted) == []
    sup.record_launch(
        persisted,
        "worker",
        cli="codex",
        pid=500,
        pid_start=_ps_iso(990000),
        now_epoch=NOW + 1,
        cfg_agent={"wrapped": True},
        launcher_nonce=OTHER_NONCE,
        launcher_nonce_injected=True,
    )
    assert persisted["agents"]["worker"]["owned_process_tree_pending"] is True
    assert persisted["agents"]["worker"]["launcher_nonce"] == OTHER_NONCE
    assert persisted["agents"]["worker"]["revoked_wrapper_runtime"] == revoked


def test_attended_process_tree_reset_retires_stale_runtime_before_relaunch_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    kill_switch = store.dir / "supervisor.kill"
    kill_switch.write_text("stop", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_owner_identity_gone",
        lambda _pid, _start, _start_filetime=None: True,
    )
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    item = _current_configured_reset_item(
        store,
        state,
        identity_gone=lambda _pid, _start, _start_filetime=None: True,
    )

    assert cli.main(item["operator_argv"][1:]) == 0
    reset_state = sup.load_supervisor_state(
        store.dir / "supervisor-state.json"
    )
    # Exercise the real persistence boundary: the old strict runtime file
    # remains, while supervisor state atomically records that exact observation
    # as retired. The subsequent operator restart request must reach RELAUNCH.
    assert wrt.read_runtime(
        store.state_dir,
        "worker",
        now_epoch=NOW + 1,
    )["status"] == wrt.STATUS_VALID
    assert reset_state["agents"]["worker"]["revoked_wrapper_runtime"]
    kill_switch.unlink()
    store.write_restart_request(
        "worker",
        {"agent": "worker", **_auth_marker("rr-after-attended-reset")},
    )
    config = {
        **_WRAP_CONFIG,
        "root": str(store.root.resolve()),
    }
    report = sup.build_report(
        store,
        now_epoch=NOW + 1,
        state=reset_state,
        supervisor_config=config,
    )

    plan = sup.plan_actions(
        report,
        reset_state,
        config,
        now_epoch=NOW + 1,
        snapshot=[],
    )["agents"]["worker"]

    assert plan["action"] == sup.RELAUNCH
    assert plan["state"] == "MANUAL_RESTART"
    assert plan["kill_first"] is False
    assert plan["kill_targets"] == []
    assert "owned_process_tree" not in plan["next_state"]
    assert plan["next_state"]["revoked_wrapper_runtime"] == (
        reset_state["agents"]["worker"]["revoked_wrapper_runtime"]
    )
    assert plan["barrier_state"]["revoked_wrapper_runtime"] == (
        reset_state["agents"]["worker"]["revoked_wrapper_runtime"]
    )

    replacement_state = {"agents": {"worker": plan["next_state"]}}
    replacement_start = _ps_iso(990000)
    sup.record_launch(
        replacement_state,
        "worker",
        cli="codex",
        pid=500,
        pid_start=replacement_start,
        now_epoch=NOW + 2,
        cfg_agent={"wrapped": True},
        launcher_nonce=OTHER_NONCE,
        launcher_nonce_injected=True,
    )
    second_report = json.loads(json.dumps(report))
    second_report["agents"]["worker"]["restart_request"] = _auth_marker(
        "rr-before-replacement-runtime"
    )
    replacement_row = {
        "pid": 500,
        "parent_pid": 1,
        "name": "python.exe",
        "command_line": (
            "python -m agenttalk "
            f"--supervisor-launch-nonce {OTHER_NONCE} "
            f"--root {store.root.resolve()} "
            "wrap --for worker --cli codex --loop -- codex"
        ),
        "start_time": replacement_start,
        "start_filetime": _ps_filetime(990000),
    }
    second_plan = sup.plan_actions(
        second_report,
        replacement_state,
        config,
        now_epoch=NOW + 60,
        snapshot=[replacement_row],
    )["agents"]["worker"]
    assert second_plan["action"] == sup.WARN_ONLY
    assert second_plan["state"] == "PROCESS_TREE_INVALID"
    assert second_plan["kill_targets"] == []
    assert second_plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_runtime_revoked_replacement_unverified"
    )

    # The boundary applies only to the exact retired observation. A new wrapper
    # generation follows the ordinary fail-closed adoption path.
    fresh_report = json.loads(json.dumps(report))
    fresh_report["agents"]["worker"]["restart_request"] = None
    fresh_report["agents"]["worker"]["wrapper_runtime"] = (
        _wrapper_runtime_view(
            now=NOW + 2,
            wrapper_pid=500,
            wrapper_start=_ps_iso(990000),
            wrapper_generation="wrapper-2",
        )
    )
    adoption = sup.plan_actions(
        fresh_report,
        replacement_state,
        config,
        now_epoch=NOW + 2,
        snapshot=[replacement_row],
    )["agents"]["worker"]
    assert adoption["action"] == sup.WARN_ONLY
    assert adoption["state"] == "PROCESS_TREE_INVALID"
    assert adoption["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_generation_adoption_pending"
    )


@pytest.mark.parametrize("damage", ["digest", "null", "boolean_schema"])
def test_malformed_runtime_revocation_boundary_remains_hold(
    damage: str,
) -> None:
    state = {"agents": {"worker": _wrap_ready()}}
    tree_plan = _owned_tree_plan(_wrap_snap())
    state["agents"]["worker"].update(tree_plan["next_state"])
    tree = state["agents"]["worker"]["owned_process_tree"]
    tree.update({
        "status": "truncated",
        "reason_code": "process_tree_truncated",
        "observed_count": tree["recorded_count"] + 1,
        "omitted_count": 1,
        "truncated": True,
    })
    # The source tree was a genuine "complete" walk (walk_complete True) -
    # a real truncated result never carries that forward, since its own
    # omitted_count > 0 already contradicts it.
    tree.pop("walk_complete", None)
    runtime = _wrapper_runtime_view()["record"]
    sup.reset_process_tree_ownership_after_attended_teardown(
        state,
        "worker",
        hold_source_hash="a" * 64,
        acknowledged_by="lead",
        verified_launch_nonce=SUPERVISOR_NONCE,
        expected_root=TEST_ROOT,
        runtime_record=runtime,
        recorded_identities_gone=True,
        reason="attended teardown",
        now_epoch=NOW,
    )
    boundary = state["agents"]["worker"]["revoked_wrapper_runtime"]
    if damage == "digest":
        boundary["runtime_record_digest"] = "corrupt"
    elif damage == "boolean_schema":
        boundary["schema_version"] = True
    else:
        state["agents"]["worker"]["revoked_wrapper_runtime"] = None

    plan = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-corrupt-revocation"),
            wrapper_runtime=_wrapper_runtime_view(),
        ),
        state,
        snapshot=[],
    )

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["kill_targets"] == []
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_runtime_revocation_record"
    )
    assert plan["next_state"]["revoked_wrapper_runtime"] == (
        state["agents"]["worker"]["revoked_wrapper_runtime"]
    )


@pytest.mark.parametrize(
    "failure",
    [
        "kill_switch_absent",
        "live_instance",
        "stale_hash",
        "nonce_mismatch",
        "identity_live",
        "current_runtime_launcher_live",
        "runtime_missing",
        "runtime_wrapper_mismatch",
        "invalid_instance",
        "unauthorized_actor",
        "malformed_audit",
        "kill_switch_removed_during_probe",
    ],
)
def test_attended_process_tree_reset_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = _team(tmp_path, "lead,ops,worker")
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    _state, source_hash = _write_attended_process_tree_reset_fixture(store)
    args = _attended_process_tree_reset_args(source_hash)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_owner_identity_gone",
        lambda _pid, _start, _start_filetime=None: True,
    )

    if failure == "kill_switch_absent":
        (store.dir / "supervisor.kill").unlink()
    elif failure == "live_instance":
        store.supervisor_instance_path().write_text(
            json.dumps({
                "root": str(store.root),
                "pid": 999999,
                "pid_start": "linux:0123456789abcdef0123456789abcdef:1",
                "token": "b" * 32,
                "started_at": _iso(NOW),
            }),
            encoding="utf-8",
        )
    elif failure == "invalid_instance":
        store.supervisor_instance_path().write_text("{broken", encoding="utf-8")
    elif failure == "stale_hash":
        args[args.index(source_hash)] = "c" * 64
    elif failure == "nonce_mismatch":
        args[args.index(SUPERVISOR_NONCE)] = OTHER_NONCE
    elif failure == "identity_live":
        monkeypatch.setattr(
            cli,
            "_owner_identity_gone",
            lambda _pid, _start, _start_filetime=None: False,
        )
    elif failure == "current_runtime_launcher_live":
        writer = wrt.WrapperRuntimeWriter(
            store.state_dir,
            "worker",
            "wrapper-1",
            wrapper_pid=WRAP_LAUNCHER_PID,
            wrapper_start="2026-07-04T07:20:31.500000+00:00",
            clock=lambda: NOW,
        )
        writer.starting(message_id="msg-later", turn_id="turn-later")
        writer.active(901, _ps_iso(800000))
        monkeypatch.setattr(
            cli,
            "_owner_identity_gone",
            lambda pid, _start, _start_filetime=None: pid != 901,
        )
    elif failure == "runtime_missing":
        wrt.runtime_path(store.state_dir, "worker").unlink()
    elif failure == "runtime_wrapper_mismatch":
        writer = wrt.WrapperRuntimeWriter(
            store.state_dir,
            "worker",
            "wrapper-other",
            wrapper_pid=WRAP_LAUNCHER_PID,
            wrapper_start=WRAP_START,
            clock=lambda: NOW,
        )
        writer.idle()
    elif failure == "unauthorized_actor":
        args[args.index("lead", args.index("--from"))] = "ops"
    elif failure == "malformed_audit":
        damaged = sup.load_supervisor_state(
            store.dir / "supervisor-state.json"
        )
        damaged["process_tree_resets"] = "not-a-list"
        sup.save_supervisor_state(
            store.dir / "supervisor-state.json",
            damaged,
        )
    elif failure == "kill_switch_removed_during_probe":
        def remove_kill_switch(
            _pid: int,
            _start: str,
            _start_filetime: str | None = None,
        ) -> bool:
            (store.dir / "supervisor.kill").unlink(missing_ok=True)
            return True

        monkeypatch.setattr(cli, "_owner_identity_gone", remove_kill_switch)

    assert _run(args, tmp_path) == 3
    persisted = sup.load_supervisor_state(
        store.dir / "supervisor-state.json"
    )
    if failure == "malformed_audit":
        assert persisted.get("process_tree_resets") == "not-a-list"
    else:
        assert persisted.get("process_tree_resets") in (None, [])
    assert persisted["agents"]["worker"]["owned_process_tree"]["status"] == (
        "truncated"
    )


def test_attended_process_tree_reset_refuses_noncanonical_state_file(
    tmp_path: Path,
) -> None:
    store = _team(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    _state, source_hash = _write_attended_process_tree_reset_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    args = [
        *_attended_process_tree_reset_args(source_hash),
        "--state-file",
        str(tmp_path / "other-state.json"),
    ]

    assert _run(args, tmp_path) == 2
    persisted = sup.load_supervisor_state(
        store.dir / "supervisor-state.json"
    )
    assert persisted.get("process_tree_resets") in (None, [])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_launcher", "garbage-start-token"),
        ("brain", "garbage-start-token"),
        ("managed", "garbage-start-token"),
    ],
)
def test_process_tree_reset_refuses_unparseable_current_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store = _team(tmp_path)
    state, _source_hash = _write_attended_process_tree_reset_fixture(store)
    entry = state["agents"]["worker"]
    runtime = _wrapper_runtime_view()["record"]
    if field == "runtime_launcher":
        runtime = _wrapper_runtime_view(
            phase="active",
            launcher_pid=901,
            launcher_start=value,
        )["record"]
    elif field == "brain":
        entry["brain_pid"] = 902
        entry["brain_start"] = value
    else:
        entry["managed_pids"] = [{"pid": 903, "start": value}]

    with pytest.raises(ValueError, match="unparseable pid/start identity"):
        sup.process_tree_ownership_reset_evidence(
            state,
            "worker",
            expected_root=store.root,
            verified_launch_nonce=SUPERVISOR_NONCE,
            runtime_record=runtime,
            now_epoch=NOW,
        )


def test_owned_process_tree_strict_generation_and_utc_matrix() -> None:
    tree = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-owned-tree-schema-matrix",
    )["next_state"]["owned_process_tree"]
    validate = lambda value: sup._valid_owned_process_tree(  # noqa: E731, SLF001
        value,
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),
        wrapper_generation=None,
        launch_nonce=SUPERVISOR_NONCE,
    )

    invalid_null_generation = json.loads(json.dumps(tree))
    invalid_null_generation.update({
        "status": "invalid",
        "reason_code": "process_tree_invalid_test",
        "wrapper_generation": None,
    })
    assert validate(invalid_null_generation) is not None

    malformed_generation = json.loads(json.dumps(invalid_null_generation))
    malformed_generation["wrapper_generation"] = "bad generation\n"
    assert validate(malformed_generation) is None

    complete_null_generation = json.loads(json.dumps(tree))
    complete_null_generation["wrapper_generation"] = None
    assert validate(complete_null_generation) is None

    truncated_null_generation = json.loads(json.dumps(tree))
    truncated_null_generation.update({
        "status": "truncated",
        "reason_code": "process_tree_truncated",
        "observed_count": tree["recorded_count"] + 1,
        "omitted_count": 1,
        "truncated": True,
        "wrapper_generation": None,
    })
    assert validate(truncated_null_generation) is None

    absence_certificate = json.loads(json.dumps(tree))
    absence_certificate.update({
        "status": "absent",
        "reason_code": "process_tree_absent",
    })
    assert validate(absence_certificate) is not None

    naive_refreshed_at = json.loads(json.dumps(tree))
    naive_refreshed_at["refreshed_at"] = "2026-07-04T07:20:31"
    assert validate(naive_refreshed_at) is None


def test_owned_process_tree_unreadable_live_wrapper_is_retryable_unknown() -> None:
    snapshot = _wrap_snap()
    first = _owned_tree_plan(snapshot, request_id="rr-wrapper-proof-prior")
    unreadable_wrapper = dict(snapshot[0])
    unreadable_wrapper["command_line"] = None

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-wrapper-proof-lost"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[unreadable_wrapper, snapshot[1]],
    )

    assert second["action"] == sup.WARN_ONLY
    assert second["state"] == "PROCESS_TREE_UNKNOWN"
    assert second["kill_targets"] == []
    assert second["clear_marker"] is None
    assert "rr-wrapper-proof-lost" not in second["next_state"]["consumed_rids"]
    assert second["next_state"]["owned_process_tree"] == (
        first["next_state"]["owned_process_tree"]
    )
    assert second["next_state"]["wrapper_recognition"] == {
        "status": "unknown",
        "reason_code": "command_line_unreadable",
    }

    third = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-wrapper-proof-lost"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 2,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=3,
            ),
        ),
        {"agents": {"worker": second["next_state"]}},
        now=NOW + 2,
        snapshot=snapshot,
    )

    assert third["action"] == sup.RELAUNCH
    assert third["state"] == "MANUAL_RESTART"
    assert third["kill_targets"]
    assert "wrapper_recognition" not in third["next_state"]


def test_owned_process_tree_snapshot_unavailable_is_retryable_unknown() -> None:
    snapshot = _wrap_snap()
    first = _owned_tree_plan(snapshot, request_id="rr-snapshot-prior")

    second = sup.plan_actions(
        _report(
            restart_request=_auth_marker("rr-snapshot-retry"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        _WRAP_CONFIG,
        now_epoch=NOW + 1,
        snapshot=None,
    )["agents"]["worker"]

    assert second["action"] == sup.WARN_ONLY
    assert second["state"] == "PROCESS_TREE_UNKNOWN"
    assert second["kill_targets"] == []
    assert second["next_state"]["owned_process_tree"] == (
        first["next_state"]["owned_process_tree"]
    )
    assert second["next_state"]["wrapper_recognition"] == {
        "status": "unknown",
        "reason_code": "snapshot_unavailable",
    }


def test_owned_process_tree_unknown_cannot_adopt_new_runtime_generation() -> None:
    snapshot = _wrap_snap()
    first = _owned_tree_plan(snapshot, request_id="rr-generation-prior")
    unreadable_wrapper = dict(snapshot[0])
    unreadable_wrapper["command_line"] = None

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-generation-hold"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                wrapper_generation="wrapper-2",
                turn_generation=2,
                progress_sequence=9,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[unreadable_wrapper, snapshot[1]],
    )

    assert second["state"] == "PROCESS_TREE_UNKNOWN"
    assert second["kill_targets"] == []
    assert second["next_state"]["runtime_wrapper_generation"] == "wrapper-1"
    assert second["next_state"]["runtime_turn_generation"] == 1
    assert second["next_state"]["runtime_progress_sequence"] == 0
    assert second["next_state"]["owned_process_tree"] == (
        first["next_state"]["owned_process_tree"]
    )


def test_owned_process_tree_invalid_record_cannot_launder_omitted_brain() -> None:
    uncertified = list(_codex_forked_brain_snap())
    first = _owned_tree_plan(
        uncertified,
        request_id="rr-invalid-prior",
    )
    assert first["next_state"]["owned_process_tree"]["status"] == "invalid"
    brain = dict(_codex_forked_brain_snap()[1])
    brain["parent_pid"] = 1

    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-invalid-prior-again"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=[_wrap_snap()[0], brain],
    )

    assert second["action"] == sup.WARN_ONLY
    assert second["state"] == "PROCESS_TREE_INVALID"
    assert second["kill_targets"] == []
    assert second["next_state"]["owned_process_tree"] == (
        first["next_state"]["owned_process_tree"]
    )


def test_owned_process_tree_cap_selection_has_linear_parent_reads() -> None:
    reads = 0

    class CountingRow(dict):
        def get(self, key, default=None):
            nonlocal reads
            if key == "parent_pid":
                reads += 1
            return super().get(key, default)

    snapshot = [CountingRow(_wrap_snap()[0])]
    parent = WRAP_LAUNCHER_PID
    for offset in range(512):
        pid = WRAP_CHILD_PID + offset
        snapshot.append(CountingRow(_proc(
            pid,
            parent,
            "codex.exe" if offset < 2 else "node.exe",
            "codex exec --json" if offset == 0 else f"node worker-{offset}.js",
            _ps_iso(600000 + offset * 100),
        )))
        parent = pid

    plan = _owned_tree_plan(snapshot, request_id="rr-linear-cap")

    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "truncated"
    assert tree["observed_count"] == len(snapshot)
    assert reads <= len(snapshot) * 20


def test_owned_process_tree_rejects_prior_schema_drift_and_launch_resets_it() -> None:
    snapshot = _wrap_snap()
    report = _report(
        heartbeat_stale=False,
        wrapper_runtime=_wrapper_runtime_view(
            phase="idle",
            now=NOW,
        ),
    )
    first = _plan_wrap(
        report,
        {"agents": {"worker": _wrap_ready()}},
        snapshot=snapshot,
    )
    prior = first["next_state"]
    prior["owned_process_tree"]["unexpected"] = "must invalidate the strict prior"

    refreshed = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(
                phase="idle",
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": prior}},
        now=NOW + 1,
        snapshot=snapshot,
    )
    refreshed_tree = refreshed["next_state"]["owned_process_tree"]
    assert refreshed["action"] == sup.WARN_ONLY
    assert refreshed["state"] == "PROCESS_TREE_INVALID"
    assert refreshed["kill_targets"] == []
    assert refreshed_tree["status"] == "invalid"
    assert refreshed_tree["reason_code"] == (
        "process_tree_invalid_prior_record_invalid"
    )
    assert "unexpected" not in refreshed_tree

    launch_state = {"agents": {"worker": refreshed["next_state"]}}
    sup.record_launch(
        launch_state,
        "worker",
        cli="codex",
        pid=WRAP_LAUNCHER_PID,
        pid_start=WRAP_START,
        now_epoch=NOW + 2,
        pre_snapshot=_wrap_snap()[:1],
        post_snapshot=_wrap_snap(),
        cfg_agent={"cli": "codex", "wrapped": True},
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
    )
    assert "owned_process_tree" not in launch_state["agents"]["worker"]
    assert launch_state["agents"]["worker"]["managed_pids"] == []


def test_owned_process_tree_malformed_prior_cannot_strand_live_leaf_on_restart() -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    first = _owned_tree_plan(snapshot, request_id="rr-prior-complete")
    damaged = first["next_state"]
    damaged["owned_process_tree"]["root_key"] = "c:/corrupt-envelope"
    damaged["owned_process_tree"]["unexpected"] = "schema drift"

    restarted = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-prior-damaged"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 1,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=2,
            ),
        ),
        {"agents": {"worker": damaged}},
        now=NOW + 1,
        # The intermediate shell has exited while its exact live leaf remains.
        snapshot=[snapshot[0], snapshot[1], snapshot[2], snapshot[4]],
    )

    assert restarted["action"] == sup.WARN_ONLY
    assert restarted["state"] == "PROCESS_TREE_INVALID"
    assert restarted["kill_first"] is False
    assert restarted["kill_targets"] == []
    assert restarted["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_prior_record_invalid"
    )

    still_held = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-prior-damaged-again"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                now=NOW + 2,
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                progress_sequence=3,
            ),
        ),
        {"agents": {"worker": restarted["next_state"]}},
        now=NOW + 2,
        snapshot=[snapshot[0], snapshot[1], snapshot[2], snapshot[4]],
    )

    assert still_held["action"] == sup.WARN_ONLY
    assert still_held["state"] == "PROCESS_TREE_INVALID"
    assert still_held["kill_targets"] == []
    assert still_held["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_prior_record_invalid"
    )


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("agent", "other"),
        ("root_key", "c:/other"),
        ("wrapper_generation", "wrapper-other"),
        ("launch_nonce", OTHER_NONCE),
    ],
)
def test_owned_process_tree_identity_drift_is_sticky_hold(
    field: str,
    other: str,
) -> None:
    state = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    current = _owned_tree_plan(_wrap_snap())["next_state"]["owned_process_tree"]
    current[field] = other
    state["owned_process_tree"] = current

    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(phase="idle"),
        ),
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(),
    )

    tree = plan["next_state"]["owned_process_tree"]
    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["kill_targets"] == []
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == "process_tree_invalid_prior_record_invalid"


def test_owned_process_tree_narrowed_rebuild_does_not_promote_to_absent() -> None:
    """The walk_complete inversion, finding 3 from the round that preceded
    it: a malformed prior discards its own historical entries entirely
    (prior_by_pid empty) before _owned_process_tree's walk even starts, so
    the walk can only rediscover whatever is CURRENTLY reachable live from
    the wrapper - a narrower graph than whatever the ORIGINAL, pre-
    malformation tree tracked. That narrowed walk can admit everything IT
    saw cleanly (rejected_count == 0, omitted_count == 0, non-empty
    entries) and used to read exactly as "complete" as a genuine full
    walk, even though it never had the chance to check on anything the
    malformation caused it to forget (a live descendant of an
    already-exited, no-longer-verifiable intermediate, say - unreachable
    from the wrapper by a live strict edge, never rejected because
    discovery never had a seed to reach it from).

    walk_complete closes this by construction (not prior_record_invalid is
    one of its own terms). Asserts the operator-visible outcome: a later
    poll where this narrowed record's own entries all read absent must NOT
    promote to absent (declaring the whole tree gone), because the record
    never proved completeness over the graph that matters, only over the
    smaller one it was able to rebuild."""
    healthy = _owned_tree_plan(
        _wrap_snap(), request_id="rr-walkcomplete-narrowed-rebuild",
    )
    tree = healthy["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert tree.get("walk_complete") is True

    corrupted = dict(tree)
    corrupted["root_key"] = "c:/some/other/root"  # fails _valid_owned_process_tree
    state = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    state["owned_process_tree"] = corrupted

    rebuilt = _plan_wrap(
        _report(heartbeat_stale=False, wrapper_runtime=_wrapper_runtime_view(phase="idle")),
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(),
    )
    narrowed = rebuilt["next_state"]["owned_process_tree"]
    assert narrowed["status"] == "invalid"
    assert narrowed["reason_code"] == "process_tree_invalid_prior_record_invalid"
    assert narrowed["rejected_count"] == 0
    assert narrowed["omitted_count"] == 0
    assert narrowed["entries"]
    # The exact gap this closes: rejected_count/omitted_count alone read as
    # a complete, trustworthy walk here - walk_complete correctly does not.
    assert narrowed.get("walk_complete") is not True

    # The operator-visible outcome: even with the narrowed record's own
    # entries all now absent, this must NOT promote to absent - it never
    # proved completeness over the graph that matters.
    later = sup._unverified_owned_process_tree(  # noqa: SLF001
        narrowed, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert later["status"] == "invalid"


@pytest.mark.parametrize(
    "field",
    [
        "start",
        "refreshed_at",
        "discovered_at",
        "wrapper_generation",
        "reason_code",
    ],
)
def test_owned_process_tree_oversized_prior_fields_fail_closed(field: str) -> None:
    state = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    current = _owned_tree_plan(_wrap_snap())["next_state"]["owned_process_tree"]
    oversized = "9" * 5000
    if field == "start":
        current["entries"][0]["start"] = (
            "linux:12345678123412341234123456789abc:" + oversized
        )
    elif field == "refreshed_at":
        current["refreshed_at"] = _iso(NOW) + oversized
    elif field == "discovered_at":
        current["entries"][0]["discovered_at"] = _iso(NOW) + oversized
    elif field == "wrapper_generation":
        current["wrapper_generation"] = oversized
    else:
        current["status"] = "invalid"
        current["reason_code"] = oversized
    state["owned_process_tree"] = current

    plan = _plan_wrap(
        _report(
            heartbeat_stale=False,
            wrapper_runtime=_wrapper_runtime_view(phase="idle"),
        ),
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(),
    )

    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["kill_targets"] == []
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_prior_record_invalid"
    )


def test_process_ownership_equal_start_graft_needs_independent_provenance() -> None:
    start = _ps_iso(100000)
    snap = [
        _proc(10, 1, "python.exe", _wrap_cmd(), start),
        _proc(11, 10, "codex.exe", "codex exec --json", start),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=start),
        _OWNERSHIP_ATTR_CONFIG,
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
        cfg_agent={"cli": "codex"},
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )
    managed = state["agents"]["worker"]["managed_pids"]
    assert managed[0]["source"] == "launch_child_provenance"
    assert managed[0]["pid"] == 11
    assert managed[0]["seed_descendants"] is True
    state["agents"]["worker"]["last_launch_epoch"] = NOW - 100

    p = sup.plan_actions(_ownership_report(), state, _OWNERSHIP_ATTR_CONFIG,
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
        cfg_agent={"cli": "codex"},
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )
    assert state["agents"]["worker"]["managed_pids"] == []
    state["agents"]["worker"]["last_launch_epoch"] = NOW - 100
    p = sup.plan_actions(_ownership_report(), state, _OWNERSHIP_ATTR_CONFIG,
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
        _OWNERSHIP_ATTR_CONFIG,
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
        _proc(
            13,
            10,
            "python.exe",
            "python -m agenttalk --root D:\\other wrap --for worker --loop -- codex",
            _ps_iso(220000),
        ),
        _proc(14, 10, "python.exe", f"python -m agenttalk --root {TEST_ROOT} frob", _ps_iso(230000)),
        _proc(15, 10, "cmd.exe", "cmd.exe /c something", _ps_iso(240000)),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _OWNERSHIP_ATTR_CONFIG,
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
    last_wins = (
        f"python -m agenttalk --roo {TEST_ROOT} wrap --fo other "
        "--for worker --loo -- codex"
    )
    assert sup.parse_agenttalk_wrap_invocation(
        last_wins,
        sup._root_key(TEST_ROOT),
        "worker",
    ) is True
    assert sup.parse_agenttalk_wrap_invocation(
        last_wins,
        sup._root_key(TEST_ROOT),
        "other",
    ) is False
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
        "python -m agenttalk --root D:\\other wrap --for worker --loop -- codex",
        f"python -m agenttalk --root {TEST_ROOT} wrap --for other --loop -- codex",
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


@pytest.mark.parametrize(
    ("command_line", "status", "reason_code"),
    [
        (None, sup.WrapRecognitionStatus.UNKNOWN, "command_line_unreadable"),
        ("   ", sup.WrapRecognitionStatus.UNKNOWN, "command_line_unreadable"),
        (
            f'python -m agenttalk --root "{TEST_ROOT} wrap --for worker --loop',
            sup.WrapRecognitionStatus.UNKNOWN,
            "command_line_tokenization_failed",
        ),
        (
            "python -m agenttalk --root",
            sup.WrapRecognitionStatus.UNKNOWN,
            "wrapper_parse_error",
        ),
        (
            "python -m agenttalk --supervisor-launch-nonce",
            sup.WrapRecognitionStatus.UNKNOWN,
            "wrapper_parse_error",
        ),
        (
            f"python -m agenttalk --supervisor-launch-nonce= --root {TEST_ROOT} "
            "wrap --for worker --loop -- codex",
            sup.WrapRecognitionStatus.UNKNOWN,
            "empty_wrapper_option_value",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop",
            sup.WrapRecognitionStatus.UNKNOWN,
            "child_command_missing",
        ),
        (
            "python -m agenttalk wrap --for worker --loop -- codex",
            sup.WrapRecognitionStatus.UNKNOWN,
            "root_identity_missing",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --loop -- codex",
            sup.WrapRecognitionStatus.UNKNOWN,
            "agent_identity_missing",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} status",
            sup.WrapRecognitionStatus.NOT_MATCHED,
            "subcommand_mismatch",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} init",
            sup.WrapRecognitionStatus.NOT_MATCHED,
            "subcommand_mismatch",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wra",
            sup.WrapRecognitionStatus.UNKNOWN,
            "wrapper_parse_error",
        ),
        (
            "python -m agenttalk --root D:\\other wrap --for worker "
            "--cli codex --loop -- codex",
            sup.WrapRecognitionStatus.NOT_MATCHED,
            "root_mismatch",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for other "
            "--cli codex --loop -- codex",
            sup.WrapRecognitionStatus.NOT_MATCHED,
            "agent_mismatch",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker "
            "--cli claude --loop -- claude",
            sup.WrapRecognitionStatus.NOT_MATCHED,
            "cli_mismatch",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker "
            "--cli gemini --loop -- gemini",
            sup.WrapRecognitionStatus.NOT_MATCHED,
            "cli_mismatch",
        ),
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker "
            "--loop -- codex",
            sup.WrapRecognitionStatus.MATCHED,
            "matched",
        ),
    ],
)
def test_process_ownership_wrap_recognition_distinguishes_unknown_and_foreign(
    command_line: object,
    status: sup.WrapRecognitionStatus,
    reason_code: str,
) -> None:
    recognition = sup.recognize_agenttalk_wrap_invocation(
        command_line,
        sup._root_key(TEST_ROOT),
        "worker",
        "codex",
    )

    assert recognition.status is status
    assert recognition.reason_code == reason_code


def test_process_ownership_known_subcommands_match_the_real_cli_parser() -> None:
    parser = cli.build_parser()
    command_choices = next(
        action.choices
        for action in parser._actions
        if action.dest == "cmd" and isinstance(action.choices, dict)
    )

    assert sup._AGENTTALK_SUBCOMMANDS == frozenset(command_choices)


@pytest.mark.parametrize(
    ("argv", "expected_loop", "expected_one_shot", "expected_cli"),
    [
        (
            ["wrap", "--for", "worker", "--", "codex"],
            False,
            False,
            "codex",
        ),
        (
            [
                "wrap", "--for", "reviewer", "--cli", "codex", "--loop",
                "--one-shot", "--to-request", "R1", "--", "codex",
            ],
            True,
            True,
            "codex",
        ),
        (
            ["wrap", "--for", "worker", "--loop", "--", "codex"],
            True,
            False,
            "codex",
        ),
    ],
)
def test_shared_wrap_admission_keeps_complete_positive_shapes(
    argv: list[str],
    expected_loop: bool,
    expected_one_shot: bool,
    expected_cli: str,
) -> None:
    parsed = launch_admission.parse_agenttalk_wrap_command(argv)

    assert isinstance(parsed, launch_admission.WrapInvocation)
    assert parsed.loop is expected_loop
    assert parsed.one_shot is expected_one_shot
    assert parsed.cli == expected_cli
    assert parsed.child_argv == ("codex",)


def test_shared_wrap_admission_stays_strict_when_child_is_missing() -> None:
    parsed = launch_admission.parse_agenttalk_wrap_command(
        ["wrap", "--for", "worker", "--loop"]
    )

    assert isinstance(parsed, launch_admission.WrapRefusal)
    assert parsed.code == "child_command_missing"


def test_process_ownership_parse_agenttalk_wrap_accepts_declared_prefix() -> None:
    """PR 98 connector, the seam finding: this argv grammar is implemented
    twice, and widening the PowerShell allowlist to accept a declared
    interpreter-option prefix (I2) was not enough on its own -
    _agenttalk_argv here never learned about module_args_from, so the
    documented -Xutf8 launch config (module_args_from: 1) got nonce
    injection and logging from the PowerShell side while this Python-side
    re-parse of the OBSERVED, live process's command line silently
    rejected it - disabling launcher-derived descendant attribution and
    scoped cleanup for exactly the configuration this PR's own docs tell
    operators to use."""
    command_line = (
        f"python -Xutf8 -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex"
    )
    root_key = sup._root_key(TEST_ROOT)
    # Without the declared boundary (module_args_from omitted, the old
    # behavior), -Xutf8 is indistinguishable from "this isn't a
    # python -m agenttalk invocation at all" - fails closed, reproducing
    # the documented config's real-world symptom.
    assert sup.parse_agenttalk_wrap_invocation(command_line, root_key, "worker") is False
    # With module_args_from declared - exactly the documented config -
    # this must now be recognized.
    assert sup.parse_agenttalk_wrap_invocation(
        command_line, root_key, "worker", 1,
    ) is True
    # A declared boundary that does not actually hold '-m agenttalk' must
    # still fail closed - the declaration is verified, not blindly
    # trusted; the shared Python resolver verifies the declared position.
    assert sup.parse_agenttalk_wrap_invocation(
        command_line, root_key, "worker", 99,
    ) is False
    # A prefix token not on the allowlist is refused even with a declared
    # boundary - the allowlist applies regardless of where it points.
    bad_prefix = (
        f"python -Z -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex"
    )
    assert sup.parse_agenttalk_wrap_invocation(
        bad_prefix, root_key, "worker", 1,
    ) is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "post-#120-merge: _liveness now routes a wrapped:true agent through "
        "_wrapped_liveness, which returns kill_targets=[] (PROCESS_TREE_INVALID, "
        "'operator attention required') whenever no valid wrapper_runtime record "
        "is present - it no longer falls back to _attribution's targets the way "
        "pre-merge _wrapped_liveness did. This test's _ownership_state()/"
        "_ownership_report() fixtures supply no runtime record, so kill_targets "
        "is now unconditionally empty regardless of the module_args_from fix "
        "under test. #120's OWN attribution-layer tests were migrated to "
        "_OWNERSHIP_ATTR_CONFIG (activity_hook, not wrapped) for exactly this "
        "reason. Escalated to the lead rather than silently re-pointing this "
        "test at that config, since it is genuinely unclear whether the "
        "underlying protection (a foreign agent's plain-form process must not "
        "be admitted as a kill target) still needs coverage on the wrapped/"
        "owned-process-tree path, which does its own structural (non-command-"
        "line) admission and was not audited for this class of risk in this "
        "round. Remove this marker once that is resolved either way."
    ),
)
def test_process_ownership_declared_prefix_does_not_leak_into_other_agents_branch() -> None:
    """Round 10 connector finding, the cross-agent kill: module_args_from
    describes THIS agent's configured launcher and nothing else. worker
    declares module_args_from=1 (the documented -Xutf8 config from the
    prior test). A sibling process is agent "other"'s own ordinary,
    undeclared invocation - python -m agenttalk ... wait --for other -
    which made no declaration of its own. Before the fix, _strict_child_edge
    parsed that sibling using worker's declared offset anyway: at offset 1
    the sibling's leading '-m' reads as an unverified prefix token, fails
    the allowlist, and _agenttalk_invocation returns None - "not recognized
    as agenttalk at all" - rather than "recognized, and it's not mine".
    _row_branch_reason then returned None instead of
    same_root_other_agent_branch, so _strict_child_edge raised no
    objection and accepted the sibling as worker's own live chain
    descendant: a foreign agent's process, one scoped-cleanup pass away
    from being killed."""
    config = {
        **_WRAP_CONFIG,
        "agents": {
            "worker": {**_WRAP_CONFIG["agents"]["worker"],
                      "launch": {"module_args_from": 1}},
        },
    }
    launcher_start = _ps_iso(100000)
    other_start = _ps_iso(200000)
    snap = [
        _proc(10, 1, "python.exe",
              f"python -Xutf8 -m agenttalk --supervisor-launch-nonce {SUPERVISOR_NONCE} "
              f"--root {TEST_ROOT} wrap --for worker --loop -- codex",
              launcher_start),
        _proc(11, 10, "python.exe",
              f"python -m agenttalk --root {TEST_ROOT} wait --for other",
              other_start),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=launcher_start),
        config,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert {t["pid"] for t in p["kill_targets"]} == {10}
    assert p["diagnostics"]["same_root_other_agent_branch"] >= 1


def test_process_ownership_launcher_pid_reuse_cannot_be_rescued_by_wrap_text() -> None:
    snap = [
        _proc(
            10,
            1,
            "python.exe",
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex",
            _ps_iso(200000),
        ),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(launcher_pid=10, launcher_start=_ps_iso(100000)),
        _OWNERSHIP_ATTR_CONFIG,
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
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]


def test_process_ownership_confirmed_launcher_requires_branch_clean_wrapper_nonce() -> None:
    legitimate = [
        _wrap_cmd(),
        "python -m agenttalk "
        f"--root {TEST_ROOT} --supervisor-launch-nonce {SUPERVISOR_NONCE} "
        "wrap --for worker --loop -- codex",
        "python -m agenttalk "
        f"--supervisor-launch-nonce={SUPERVISOR_NONCE} --root {TEST_ROOT} "
        "wrap --for worker --loop -- codex",
        f"agenttalk --supervisor-launch-nonce {SUPERVISOR_NONCE} --root {TEST_ROOT} wrap --for worker --loop -- codex",
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


@pytest.mark.parametrize(
    "state_over",
    [
        {
            "launcher_nonce_injected": False,
            "launcher_nonce_missing_reason": "unsupported_launch_argv",
        },
        {"launcher_nonce": "BAD"},
    ],
    ids=["missing-stored-nonce", "malformed-stored-nonce"],
)
def test_process_ownership_unreadable_row_dominates_bad_stored_nonce_state(
    state_over: dict,
) -> None:
    plan = _launcher_plan(None, state_over=state_over)

    assert plan["kill_targets"] == []
    assert plan["diagnostics"]["launcher_nonce_cmdline_unreadable"] == 1
    assert plan["diagnostics"].get("launcher_nonce_missing_state", 0) == 0
    assert plan["diagnostics"].get("launcher_nonce_malformed", 0) == 0


def test_process_ownership_launcher_nonce_fail_closed_matrix() -> None:
    cases = [
        (
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex",
            "launcher_nonce_absent",
        ),
        (_wrap_cmd(nonce=OTHER_NONCE), "launcher_nonce_mismatch"),
        (
            f"python -m agenttalk --supervisor-launch-nonce short --root {TEST_ROOT} wrap --for worker --loop -- codex",
            "launcher_nonce_malformed",
        ),
        (
            "python -m agenttalk "
            f"--supervisor-launch-nonce {SUPERVISOR_NONCE} "
            f"--supervisor-launch-nonce {OTHER_NONCE} "
            f"--root {TEST_ROOT} wrap --for worker --loop -- codex",
            "launcher_nonce_duplicate",
        ),
    ]
    for command_line, counter in cases:
        p = _launcher_plan(command_line)
        assert p["kill_targets"] == []
        assert p["diagnostics"][counter] == 1


@pytest.mark.parametrize(
    "command_line",
    [
        f"python -m agenttalk --supervisor-launch-nonce short --root {TEST_ROOT} "
        "wrap --for worker --loop",
        "python -m agenttalk "
        f"--supervisor-launch-nonce {SUPERVISOR_NONCE} "
        f"--supervisor-launch-nonce {OTHER_NONCE} --root {TEST_ROOT} "
        "wrap --for worker --loop",
        f"python -m agenttalk --root {TEST_ROOT} wrap "
        f"--supervisor-launch-nonce {SUPERVISOR_NONCE} --for worker --loop -- codex",
    ],
)
def test_process_ownership_incomplete_or_misplaced_nonce_observation_is_unknown(
    command_line: str,
) -> None:
    plan = _launcher_plan(command_line)

    assert plan["kill_targets"] == []
    assert plan["diagnostics"]["launcher_wrap_observation_unknown"] == 1


def test_process_ownership_child_tail_nonce_is_readable_contradiction() -> None:
    plan = _launcher_plan(
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop "
        f"-- codex --supervisor-launch-nonce {SUPERVISOR_NONCE}",
    )

    assert plan["kill_targets"] == []
    assert plan["diagnostics"]["launcher_nonce_after_subcommand_or_tail"] == 1


def test_process_ownership_pre_upgrade_launcher_without_nonce_is_cleanup_miss_not_cross_kill() -> None:
    p = _launcher_plan(
        f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex",
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
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW + 1,
        snapshot=[_proc(10, 1, "codex.exe", "codex exec", _ps_iso(100000))],
    )["agents"]["worker"]
    assert p["kill_targets"] == []
    assert p["diagnostics"]["launcher_nonce_unsupported_argv"] == 1


def test_ephemeral_launch_uses_no_legacy_or_command_line_kill_authority() -> None:
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
    assert entry["managed_pids"] == []
    assert entry["provenance_diagnostics"] == {}
    assert "owned_process_tree" not in entry

    report = {
        "root_key": sup._root_key(TEST_ROOT),
        "agents": {},
        "launch_requests": [],
        "ephemeral_reviewers": {"active": {"R1": {}}},
    }
    # A name/command-line match for a different PID never grants teardown.
    snap = [_proc(22, 1, "python.exe", _wrap_cmd(), _ps_iso(220000))]
    plan = sup.plan_actions(report, state, _WRAP_CONFIG,
                            now_epoch=NOW + 2, snapshot=snap)
    held = plan["ephemeral_reviewers"]["R1"]
    assert held["action"] == eph.ACTION_NONE
    assert held["state"] == "process_tree_hold"
    assert held["kill_targets"] == []
    assert held["archive"] is False
    assert "owned_process_tree" not in held["next_entry"]
    assert held["next_entry"]["process_tree_hold_reason"] == "runtime_absent"
    assert held["next_entry"]["held_terminal"] == {
        "terminal_state": eph.STATE_TIMED_OUT,
        "reason": (
            "ephemeral reviewer timed out without a typed terminal "
            "review-result"
        ),
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
    }

    missing = json.loads(json.dumps(state))
    missing["ephemeral_reviewers"]["active"]["R1"].pop("launcher_nonce")
    missing["ephemeral_reviewers"]["active"]["R1"]["launcher_nonce_injected"] = False
    suppressed = sup.plan_actions(report, missing, _WRAP_CONFIG,
                                  now_epoch=NOW + 2, snapshot=snap)
    timeout_suppressed = suppressed["ephemeral_reviewers"]["R1"]
    assert timeout_suppressed["kill_targets"] == []
    assert timeout_suppressed["action"] == eph.ACTION_NONE
    assert timeout_suppressed["archive"] is False


def test_ephemeral_review_survives_duplicate_row_poll_not_archived_failed() -> None:
    """Task #150 round 7 connector finding: a snapshot containing two rows
    for an ACTIVE EPHEMERAL REVIEWER's launcher pid (even identical rows)
    made _snap_index exclude that pid, and _ephemeral_process_alive read
    the resulting idx.get(pid)-is-None the same as "process exited" -
    permanently archiving a healthy review as failed, since a persisted
    held_terminal is deliberately unrewritable by any later report. This
    tests the OUTCOME an operator would see - the review survives the
    glitched poll and is still active, not the internal alive-flag value
    - per the explicit instruction that a test proving _ephemeral_process_
    alive now returns None is not the same as a test proving the review
    is not archived."""
    start = _ps_iso(100000)
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
    sup.record_ephemeral_launch(
        state,
        "R1",
        pid=10,
        pid_start=start,
        now_epoch=NOW,
        timeout_seconds=100000,  # far from expiring on this poll
        root_key=sup._root_key(TEST_ROOT),
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )
    report = {
        "root_key": sup._root_key(TEST_ROOT),
        "agents": {},
        "launch_requests": [],
        "ephemeral_reviewers": {"active": {"R1": {}}},
    }
    duplicated_launcher = _proc(10, 1, "codex.exe", "codex exec", start)
    glitched_snapshot = [duplicated_launcher, dict(duplicated_launcher)]
    plan = sup.plan_actions(
        report, state, _WRAP_CONFIG, now_epoch=NOW + 1, snapshot=glitched_snapshot,
    )
    result = plan["ephemeral_reviewers"]["R1"]
    assert result["action"] != eph.ACTION_FAILED
    assert result["next_entry"].get("held_terminal") is None


def test_ephemeral_launch_spec_preserves_declared_module_args_from() -> None:
    """Round 13 connector finding, the third location this field has been
    lost: launch_spec() rebuilt its "launch" sub-dict from only
    windows_file and windows_args, silently dropping module_args_from (and
    any other field of the profile's launch config) - an ephemeral
    reviewer configured with the documented -Xutf8 prefix ran fine as
    Python but got index 0 checked, nonce injection and bounded logging
    disabled, and was recorded WITHOUT nonce-backed process attribution.
    launch_spec now starts the rebuild from a COPY of the profile's launch
    dict rather than two hand-picked keys, so a field nobody remembers to
    list here survives rather than reading as unset."""
    marker = {"request_id": "R1", "skill": "review", "profile": "codex-evidence-reviewer"}
    profile = {
        "cli": "codex",
        "launch": {
            "windows_file": "python.exe",
            "windows_args": ["-Xutf8", "-m", "agenttalk", "wrap", "--for", "{AGENT}"],
            "module_args_from": 1,
        },
    }
    spec = eph.launch_spec(marker, profile, "adversary-1")
    assert spec["launch"]["module_args_from"] == 1
    assert spec["launch"]["windows_file"] == "python.exe"
    assert spec["launch"]["windows_args"][-1] == "adversary-1"


def test_ephemeral_record_prepared_persists_declared_module_args_from() -> None:
    """Round 14 connector finding, the persistence half of the rebuild
    class: launch_spec() (round 13) lets Launch-Spec inject the nonce and
    start logging at launch time, but module_args_from was never
    PERSISTED in the active ephemeral entry - record_prepared hand-listed
    individual fields (request_id, agent, ..., cli) rather than carrying
    the profile's own launch config wholesale, so the field survived the
    launch and died in the record the moment the next poll needed it.
    Stored wholesale here (entry["launch"] = dict(launch)), not as a
    hand-picked key, so a future field of launch survives the same way."""
    state: dict = {}
    launch = {"windows_file": "python.exe", "module_args_from": 1}
    effective_launch_binding = eph.make_effective_launch_binding(
        {
            "request_id": "R1",
            "requested_by": "lead",
            "profile": "codex-evidence-reviewer",
            "agent": "adversary-1",
            "review_request_msg_id": "m1",
        },
        {"cli": "codex", "launch": launch},
        review_request_sha256="a" * 64,
    )
    eph.record_prepared(
        state,
        request_id="R1",
        agent="adversary-1",
        requested_by="lead",
        profile="codex-evidence-reviewer",
        timeout_seconds=1800,
        now_epoch=NOW,
        review_request_id="m1",
        cli="codex",
        launch=launch,
        effective_launch_binding=effective_launch_binding,
    )
    entry = state["ephemeral_reviewers"]["active"]["R1"]
    assert entry["launch"] == launch
    assert entry["identity_binding_version"] == 1
    assert entry["effective_launch_binding"] == effective_launch_binding


def test_ephemeral_record_prepared_keeps_active_allocation_history() -> None:
    state = {
        "ephemeral_reviewers": {
            "active": {
                "R1": {
                    "request_id": "R1",
                    "agent": "adversary-1",
                },
            },
            "launch_history": [{
                "request_id": "R1",
                "agent": "adversary-1",
                "at_epoch": NOW - 86401,
            }],
        },
    }

    effective_launch_binding = eph.make_effective_launch_binding(
        {
            "request_id": "R2",
            "requested_by": "lead",
            "profile": "codex-evidence-reviewer",
            "agent": "adversary-2",
            "review_request_msg_id": "m2",
        },
        {"cli": "codex", "launch": {}},
        review_request_sha256="b" * 64,
    )
    eph.record_prepared(
        state,
        request_id="R2",
        agent="adversary-2",
        requested_by="lead",
        profile="codex-evidence-reviewer",
        timeout_seconds=1800,
        now_epoch=NOW,
        review_request_id="m2",
        effective_launch_binding=effective_launch_binding,
    )

    assert state["ephemeral_reviewers"]["launch_history"] == [
        {
            "request_id": "R1",
            "agent": "adversary-1",
            "at_epoch": NOW - 86401,
        },
        {
            "request_id": "R2",
            "agent": "adversary-2",
            "at_epoch": NOW,
        },
    ]


def test_ephemeral_owned_process_view_reconstructs_declared_module_args_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same finding, the read side: _ephemeral_owned_process_view rebuilt
    cfg_agent as {"cli": ..., "wrapped": True} - two hand-picked keys, no
    launch sub-dict at all - so _wrapped_liveness's _is_confirmed_launcher
    call always resolved module_args_from to None regardless of what the
    profile declared, and a declared-prefix launcher's nonce was rejected
    at offset 0 on every poll after the first. Captures the actual
    cfg_agent _wrapped_liveness is called with, rather than driving a full
    runtime-record scenario end to end, to isolate exactly this
    reconstruction step."""
    captured: dict = {}

    def _fake_wrapped_liveness(snapshot, entry, cfg_agent, agent, now_epoch,
                               runtime_view, *, root_key):
        captured["cfg_agent"] = cfg_agent
        return {
            "kill_targets": [],
            "owned_process_tree": None,
            "owned_process_tree_refreshed": False,
            "child_reason": "test_capture",
        }

    monkeypatch.setattr(sup, "_wrapped_liveness", _fake_wrapped_liveness)

    entry = {
        "request_id": "R1",
        "agent": "adversary-1",
        "cli": "codex",
        "launch": {"module_args_from": 1},
    }
    sup._ephemeral_owned_process_view(
        None, entry, {}, NOW, root_key=sup._root_key(TEST_ROOT),
    )
    assert captured["cfg_agent"]["launch"]["module_args_from"] == 1


def test_ephemeral_owned_process_view_retries_unknown_without_teardown_authority() -> None:
    snapshot = _wrap_snap()
    first = _owned_tree_plan(snapshot, request_id="rr-ephemeral-prior")
    entry = json.loads(json.dumps(first["next_state"]))
    entry.update({"request_id": "R1", "agent": "worker", "cli": "codex"})
    unreadable_wrapper = dict(snapshot[0])
    unreadable_wrapper["command_line"] = None
    report_entry = {
        "wrapper_runtime": _wrapper_runtime_view(
            phase="active",
            now=NOW + 1,
            launcher_pid=WRAP_CHILD_PID,
            launcher_start=WRAP_CHILD_START,
            progress_sequence=2,
        ),
    }

    held_entry, held_liveness, complete = sup._ephemeral_owned_process_view(
        [unreadable_wrapper, snapshot[1]],
        entry,
        report_entry,
        NOW + 1,
        root_key=sup._root_key(TEST_ROOT),
    )

    assert complete is False
    assert held_liveness["kill_targets"] == []
    assert held_entry["owned_process_tree"] == entry["owned_process_tree"]
    assert held_entry["wrapper_recognition"] == {
        "status": "unknown",
        "reason_code": "command_line_unreadable",
    }

    retried_entry, retried_liveness, complete = sup._ephemeral_owned_process_view(
        snapshot,
        held_entry,
        report_entry,
        NOW + 2,
        root_key=sup._root_key(TEST_ROOT),
    )

    assert complete is True
    assert retried_liveness["owned_process_tree_refreshed"] is True
    assert "wrapper_recognition" not in retried_entry
    assert "process_tree_hold_reason" not in retried_entry


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
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["kill_targets"] == [{
        "pid": 11,
        "start": _ps_iso(200000),
        "start_filetime": _ps_filetime(200000),
        "reason": "launch_child_provenance",
        "source": "launch_child_provenance",
    }]
    p_fresh = sup.plan_actions(
        _ownership_report(stale=False),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(base))],
        ),
        _OWNERSHIP_ATTR_CONFIG,
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
        _OWNERSHIP_ATTR_CONFIG,
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
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p_missing["kill_targets"] == []
    assert p_missing["diagnostics"]["prior_field_missing"] == 1
    assert p_missing["diagnostics"]["prior_ttl_expired"] == 1


def test_process_ownership_duplicated_prior_descendant_still_killed_with_launcher() -> None:
    """Task #150 round 8 connector finding: the SAME idx.get(pid)-is-None
    conflation round 7 fixed for the ephemeral path also reaches the
    legacy (non-wrapped) attribution model, arriving in TWO valid_priors
    loops instead of one - dropping both the kill target AND the
    persisted provenance for a tracked descendant whose row is merely
    duplicated this poll, not gone. The observable failure this produces
    is exactly the duplicate-wrapper hazard the whole PR exists to
    prevent: a recovery kills the launcher, the glitch clears before the
    next snapshot, and the untargeted descendant is left alive alongside
    a freshly launched replacement. This asserts the outcome an operator
    would see - the descendant IS a kill target, killed in the SAME
    operation as the launcher, and its provenance survives the glitched
    poll - not _attribution's own internal classification."""
    prior = {
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
    duplicated_descendant = _proc(11, 1, "codex.exe", "codex exec --json", _ps_iso(200000))
    snap = [
        _proc(10, 1, "codex.exe", "codex exec", _ps_iso(100000)),
        duplicated_descendant,
        dict(duplicated_descendant),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(prior))],
        ),
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    kill_target_pids = {t["pid"] for t in p["kill_targets"]}
    assert 11 in kill_target_pids  # the ambiguous descendant, killed alongside the launcher

    # Separately: a ROUTINE poll (fresh heartbeat, no restart decision) must
    # not lose this descendant's provenance either - a manual-restart poll
    # legitimately starts managed_pids fresh on relaunch, so it cannot
    # isolate the persistence half of this fix on its own.
    p_routine = sup.plan_actions(
        _ownership_report(stale=False),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(prior))],
        ),
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    managed_pids_after = {m["pid"] for m in p_routine["next_state"]["managed_pids"]}
    assert 11 in managed_pids_after  # provenance survives the glitched poll


def test_process_ownership_opaque_child_of_excluded_seed_still_killed_with_launcher() -> None:
    """Task #150 round 10 finding 1, corrected by round 12 finding 2:
    round 10 made an excluded tracked pid's opaque child a kill target in
    the SAME operation, by reusing the excluded seed's synthetic row
    (prior pid/start) as the _strict_child_edge PARENT for BFS discovery.
    That synthetic identity is only ever re-verified by Stop-Tree, at kill
    time, for the SEED's own kill - nothing re-verifies it before it feeds
    an independent kill decision about the CHILD. If pid 11 has been
    RECYCLED (a genuinely different process now holds it), the synthetic
    row's stale start time is almost always earlier than any of the
    replacement's own children, so the ordering check meant to REJECT an
    unrelated descendant would instead pass every one of them - a new
    hazard round 10's fix itself created. Round 12 stopped descending
    through an excluded parent at all: pid 12 is neither invented as a
    kill target nor silently lost - it is unaccounted, which now correctly
    HOLDS the whole poll (no kill, no relaunch, not even the seed) rather
    than proceeding on a kill decision made from an unverifiable identity.
    Asserts the operator-visible outcome: the poll holds."""
    prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 11,
        "start": _ps_iso(200000),
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 10,
        "last_fresh_attribution_epoch": NOW - 10,
        "seed_descendants": True,
        "source_launcher_pid": 10,
        "source_launcher_start": _ps_iso(100000),
        "source_launcher_nonce": SUPERVISOR_NONCE,
    }
    duplicated_descendant = _proc(11, 1, "codex.exe", "codex exec --json", _ps_iso(200000))
    opaque_child = _proc(12, 11, "node.exe", "node worker.js", _ps_iso(250000))
    snap = [
        _proc(10, 1, "codex.exe", "codex exec", _ps_iso(100000)),
        duplicated_descendant,
        dict(duplicated_descendant),
        opaque_child,
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(prior))],
        ),
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["action"] == sup.WARN_ONLY
    assert p["state"] == "UNACCOUNTED_LIVE_DESCENDANT"
    assert p["kill_targets"] == []  # not even the excluded seed itself is killed
    assert p["diagnostics"]["excluded_live_descendant_unaccounted"] == 1


def test_process_ownership_excluded_opaque_child_is_unaccounted_not_lost() -> None:
    """Task #150 round 11 finding 2 (discovery), corrected by round 12
    finding 1 (the remedy): _children_map(idx) is built only from
    idx.items() - a newly discovered child excluded this poll (duplicate/
    ambiguous rows) never contributed a row to idx, so it never
    contributes an edge either, regardless of its parent's own trust
    state. A valid, non-excluded seed_descendants root's opaque child is
    therefore invisible to the BFS discovery entirely, not merely skipped
    after being found - a structurally different gap than round 10's
    finding 1 (an excluded PARENT still has a trusted prior identity to
    fall back on; a first-time-seen EXCLUDED CHILD has no independent
    anchor at all, the same "no snapshot-only signal to disambiguate a
    first-time-seen identity" residual _snap_all_edges already documents
    for a malformed pid).

    Round 11 surfaced this via a diagnostic counter alone, which round 12
    correctly called out as a check that runs and is never acted on: a
    manual restart request would still have killed only the trusted
    parent and relaunched, stranding the unaccounted child exactly as
    before. Asserts the ACTUAL operator-visible outcome now: the poll
    HOLDS (no kill, no relaunch) rather than proceeding on partial
    accounting, while still never inventing an untrustworthy kill target
    for the ambiguous child itself."""
    prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 11,
        "start": _ps_iso(200000),
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 10,
        "last_fresh_attribution_epoch": NOW - 10,
        "seed_descendants": True,
        "source_launcher_pid": 10,
        "source_launcher_start": _ps_iso(100000),
        "source_launcher_nonce": SUPERVISOR_NONCE,
    }
    opaque_child = _proc(12, 11, "node.exe", "node worker.js", _ps_iso(250000))
    snap = [
        _proc(10, 1, "codex.exe", "codex exec", _ps_iso(100000)),
        _proc(11, 1, "codex.exe", "codex exec --json", _ps_iso(200000)),
        opaque_child,
        dict(opaque_child),
    ]
    p = sup.plan_actions(
        _ownership_report(),
        _ownership_state(
            launcher_pid=10,
            launcher_start=_ps_iso(100000),
            managed_pids=[json.loads(json.dumps(prior))],
        ),
        _OWNERSHIP_ATTR_CONFIG,
        now_epoch=NOW,
        snapshot=snap,
    )["agents"]["worker"]
    assert p["action"] == sup.WARN_ONLY
    assert p["state"] == "UNACCOUNTED_LIVE_DESCENDANT"
    assert p["kill_targets"] == []  # HOLD - not even the trusted parent is killed
    assert p["diagnostics"]["excluded_live_descendant_unaccounted"] == 1


def test_process_ownership_ambiguous_seed_hold_survives_the_provenance_ttl() -> None:
    """Task #150 round 13 connector finding: keeping an excluded prior
    entry UNCHANGED across polls (round 8's fix) left its own
    last_fresh_attribution_epoch frozen at whenever it was last
    unambiguous - _prior_valid's TTL check then expires it after
    _PROVENANCE_TTL_SECONDS of CONTINUOUS ambiguity, even though the pid
    is present in conflicting rows on every single one of those polls. A
    TTL is the right instrument for evidence nobody has rechecked; this
    pid IS rechecked every poll, it just cannot be disambiguated - a live
    observation of an unresolved condition, not aging evidence. Expiring
    it drops the seed the UNACCOUNTED_LIVE_DESCENDANT HOLD (round 12)
    depends on to discover its own child at all, so a pending authorized
    restart relaunches without targeting either process.

    Simulates three consecutive polls with the identical ambiguity present
    throughout (same duplicate-rowed seed, same opaque child), spaced
    2000 seconds apart - each individual gap is under the one-hour TTL,
    but the CUMULATIVE gap from the first poll to the third (4000s)
    exceeds it. Asserts the operator-visible outcome on the THIRD poll:
    the HOLD still fires - not a changed epoch value."""
    prior = {
        "attribution_model": "process_ownership_v1",
        "root_key": sup._root_key(TEST_ROOT),
        "agent": "worker",
        "request_id": None,
        "pid": 11,
        "start": _ps_iso(200000),
        "source": "launch_child_provenance",
        "captured_at_epoch": NOW - 10,
        "last_fresh_attribution_epoch": NOW - 10,
        "seed_descendants": True,
        "source_launcher_pid": 10,
        "source_launcher_start": _ps_iso(100000),
        "source_launcher_nonce": SUPERVISOR_NONCE,
    }
    duplicated_seed = _proc(11, 1, "codex.exe", "codex exec --json", _ps_iso(200000))
    opaque_child = _proc(12, 11, "node.exe", "node worker.js", _ps_iso(250000))
    snap = [
        _proc(10, 1, "codex.exe", "codex exec", _ps_iso(100000)),
        duplicated_seed,
        dict(duplicated_seed),
        opaque_child,
    ]
    assert sup._PROVENANCE_TTL_SECONDS == 3600.0  # noqa: SLF001

    state = _ownership_state(
        launcher_pid=10,
        launcher_start=_ps_iso(100000),
        managed_pids=[json.loads(json.dumps(prior))],
    )
    poll_epochs = [NOW, NOW + 2000, NOW + 4000]
    p = None
    for epoch in poll_epochs:
        p = sup.plan_actions(
            _ownership_report(),
            state,
            _OWNERSHIP_ATTR_CONFIG,
            now_epoch=epoch,
            snapshot=snap,
        )["agents"]["worker"]
        state = {"agents": {"worker": {**state["agents"]["worker"], **p["next_state"]}}}

    assert poll_epochs[-1] - poll_epochs[0] > sup._PROVENANCE_TTL_SECONDS  # noqa: SLF001
    assert p["action"] == sup.WARN_ONLY
    assert p["state"] == "UNACCOUNTED_LIVE_DESCENDANT"
    assert p["kill_targets"] == []
    assert p["diagnostics"]["excluded_live_descendant_unaccounted"] == 1
    managed_after = {m["pid"]: m for m in p["next_state"]["managed_pids"]}
    assert managed_after[11]["last_fresh_attribution_epoch"] == poll_epochs[-1]


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
            f"python -m agenttalk --root {TEST_ROOT} wrap --for worker --loop -- codex",
            _ps_iso(100000),
        )
    ]
    p = sup.plan_actions(_ownership_report(), state, _OWNERSHIP_ATTR_CONFIG,
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
        _OWNERSHIP_ATTR_CONFIG,
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
    p = sup.plan_actions(
        _ownership_report(stale=False),
        state,
        _OWNERSHIP_ATTR_CONFIG,
                         now_epoch=NOW, snapshot=snap)["agents"]["worker"]
    managed = {m["pid"]: m for m in p["next_state"]["managed_pids"]}
    assert managed[11]["source"] == "legacy_rederived"
    assert 12 not in managed
    assert p["diagnostics"]["legacy_unverifiable_dropped"] == 1


def test_process_ownership_drifted_launcher_command_line_match_is_not_kill_authority() -> None:
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
    assert p["kill_targets"] == []


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


def test_launch_barrier_exact_state_launcher_blocks_without_command_line() -> None:
    start = _ps_iso(100000)
    snap = [_proc(10, 1, "python.exe", None, start)]

    result = sup.evaluate_launch_barrier(
        snap,
        _ownership_state(launcher_pid=10, launcher_start=start),
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
    )

    assert result["allow_launch"] is False
    assert result["blocked"] is True
    assert result["reason"] == "state_launcher_survived"
    assert result["survivors"] == [
        {"kind": "state_launcher", "pid": 10, "name": "python.exe"}
    ]


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_reuses_tree_exact_state_launcher_identity(
    ephemeral: bool,
) -> None:
    entry = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-barrier-exact-state-launcher",
    )["next_state"]
    recorded = entry["owned_process_tree"]["entries"][0]
    replacement = _proc(
        recorded["pid"],
        1,
        "unrelated.exe",
        None,
        recorded["start"],
        start_filetime=str(int(recorded["start_filetime"]) + 10),
    )
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )

    assert sup._start_tokens_match(  # noqa: SLF001
        replacement["start_time"],
        recorded["start"],
    )
    result = sup.evaluate_launch_barrier(
        [replacement],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result == {
        "allow_launch": True,
        "blocked": False,
        "reason": "clear",
        "snapshot_available": True,
        "survivor_count": 0,
        "survivors": [],
    }


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_tree_same_state_launcher_blocks_once(
    ephemeral: bool,
) -> None:
    entry = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-barrier-same-state-launcher",
    )["next_state"]
    live = dict(_wrap_snap()[0])
    live["command_line"] = None
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )

    result = sup.evaluate_launch_barrier(
        [live],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["allow_launch"] is False
    assert result["reason"] == "owned_process_survived"
    assert result["survivors"] == [
        {"kind": "owned_process", "pid": live["pid"], "name": "python.exe"}
    ]


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_does_not_reuse_nonroot_tree_pid_judgment(
    ephemeral: bool,
) -> None:
    snapshot = [
        *_wrap_snap(),
        _proc(302, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
    ]
    entry = _owned_tree_plan(
        snapshot,
        request_id="rr-barrier-nonroot-state-alias",
    )["next_state"]
    replacement = _proc(
        302,
        1,
        "unrelated.exe",
        None,
        _ps_iso(800000),
    )
    entry["launcher_pid"] = replacement["pid"]
    entry["launcher_start"] = replacement["start_time"]
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )

    result = sup.evaluate_launch_barrier(
        [replacement],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["allow_launch"] is False
    assert result["reason"] == "state_launcher_survived"
    assert result["survivors"] == [
        {
            "kind": "state_launcher",
            "pid": replacement["pid"],
            "name": "unrelated.exe",
        }
    ]


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_rejects_nullable_authoritative_tree(
    ephemeral: bool,
) -> None:
    entry = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-barrier-nullable-state-launcher",
    )["next_state"]
    recorded = entry["owned_process_tree"]["entries"][0]
    recorded["start_filetime"] = None
    live = dict(_wrap_snap()[0])
    live["command_line"] = None
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )

    result = sup.evaluate_launch_barrier(
        [live],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["allow_launch"] is False
    assert result["reason"] == "owned_process_tree_unavailable"
    assert result["survivors"] == []


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_blocks_exact_owned_leaf_survivor(
    ephemeral: bool,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    plan = _owned_tree_plan(snapshot, request_id="rr-barrier-owned-leaf")
    entry = plan["next_state"]
    surviving_leaf = dict(snapshot[-1])
    # A missing fresh FILETIME is ambiguity, not evidence of PID recycling.
    surviving_leaf["start_filetime"] = None
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )

    result = sup.evaluate_launch_barrier(
        [surviving_leaf],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["allow_launch"] is False
    assert result["reason"] == "owned_process_identity_ambiguous"
    assert result["survivors"] == [
        {
            "kind": "owned_process_identity_ambiguous",
            "pid": 304,
            "name": "node.exe",
        }
    ]


@pytest.mark.parametrize("ephemeral", [False, True])
@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("duplicate", "snapshot_duplicate_pid"),
        ("invalid", "snapshot_invalid_process_row"),
    ],
)
def test_launch_barrier_rejects_malformed_pid_graph_before_indexing(
    ephemeral: bool,
    case: str,
    expected_reason: str,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    entry = _owned_tree_plan(
        snapshot,
        request_id="rr-barrier-malformed-snapshot",
    )["next_state"]
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )
    exact_leaf = dict(snapshot[-1])
    if case == "duplicate":
        recycled_leaf = _proc(
            exact_leaf["pid"],
            exact_leaf["parent_pid"],
            "node.exe",
            "node recycled.js",
            _ps_iso(990000),
        )
        candidates = [
            [exact_leaf, recycled_leaf],
            [recycled_leaf, exact_leaf],
        ]
    else:
        candidates = [[{
            **exact_leaf,
            "pid": True,
        }]]

    for post_kill in candidates:
        result = sup.evaluate_launch_barrier(
            post_kill,
            state,
            _WRAP_CONFIG,
            "worker",
            root_key=sup._root_key(TEST_ROOT),
            request_id="lr-barrier" if ephemeral else None,
        )

        assert result == {
            "allow_launch": False,
            "blocked": True,
            "reason": expected_reason,
            "snapshot_available": True,
            "survivor_count": 0,
            "survivors": [],
        }


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_blocks_unrecorded_post_plan_descendant_closure(
    ephemeral: bool,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "codex.exe", "codex tui", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh -File tool.ps1", _ps_iso(800000)),
        _proc(304, 303, "node.exe", "node repl.js", _ps_iso(900000)),
    ]
    entry = _owned_tree_plan(
        snapshot,
        request_id="rr-barrier-late-descendant",
    )["next_state"]
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )
    post_kill = [
        _proc(305, 304, "pwsh.exe", "pwsh late.ps1", _ps_iso(910000)),
        _proc(306, 305, "node.exe", "node late.js", _ps_iso(920000)),
    ]

    result = sup.evaluate_launch_barrier(
        post_kill,
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["blocked"] is True
    assert result["reason"] == "owned_descendant_edge_survived"
    assert result["survivor_count"] == 2
    assert {item["pid"] for item in result["survivors"]} == {305, 306}
    assert all(
        item["kind"] == "owned_descendant_edge"
        for item in result["survivors"]
    )


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_excludes_definitively_recycled_closure_seed(
    ephemeral: bool,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
    ]
    entry = _owned_tree_plan(
        snapshot,
        request_id="rr-barrier-recycled-seed",
    )["next_state"]
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )
    recorded = entry["owned_process_tree"]["entries"][-1]
    recycled = _proc(
        recorded["pid"],
        1,
        "unrelated.exe",
        "unrelated replacement",
        recorded["start"],
        start_filetime=str(int(recorded["start_filetime"]) + 10),
    )
    foreign_child = _proc(
        303,
        recycled["pid"],
        "foreign.exe",
        "foreign child",
        _ps_iso(800000),
    )

    result = sup.evaluate_launch_barrier(
        [recycled, foreign_child],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result == {
        "allow_launch": True,
        "blocked": False,
        "reason": "clear",
        "snapshot_available": True,
        "survivor_count": 0,
        "survivors": [],
    }


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_retains_pre_recycle_descendant_of_recycled_seed(
    ephemeral: bool,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
    ]
    entry = _owned_tree_plan(
        snapshot,
        request_id="rr-barrier-pre-recycle-child",
    )["next_state"]
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )
    recorded = entry["owned_process_tree"]["entries"][-1]
    recycled = _proc(
        recorded["pid"],
        1,
        "unrelated.exe",
        "unrelated replacement",
        _ps_iso(900000),
    )
    replacement_filetime = int(recycled["start_filetime"])
    old_child = _proc(
        303,
        recycled["pid"],
        "old-child.exe",
        "old child",
        recycled["start_time"],
        start_filetime=str(replacement_filetime - 10),
    )
    old_grandchild = _proc(
        304,
        old_child["pid"],
        "old-grandchild.exe",
        "old grandchild",
        _ps_iso(920000),
    )
    foreign_child = _proc(
        305,
        recycled["pid"],
        "foreign.exe",
        "foreign child",
        recycled["start_time"],
        start_filetime=str(replacement_filetime + 10),
    )
    equal_child = _proc(
        306,
        recycled["pid"],
        "equal.exe",
        "equal child",
        recycled["start_time"],
        start_filetime=str(replacement_filetime),
    )
    ambiguous_child = _proc(
        307,
        recycled["pid"],
        "ambiguous.exe",
        "ambiguous child",
        recycled["start_time"],
    )
    ambiguous_child["start_filetime"] = None

    result = sup.evaluate_launch_barrier(
        [
            recycled,
            old_child,
            old_grandchild,
            foreign_child,
            equal_child,
            ambiguous_child,
        ],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["blocked"] is True
    assert result["reason"] == "owned_descendant_edge_survived"
    assert result["survivor_count"] == 3
    assert {item["pid"] for item in result["survivors"]} == {303, 304, 307}
    assert all(
        item["kind"] == "owned_descendant_edge"
        for item in result["survivors"]
    )


@pytest.mark.parametrize("ephemeral", [False, True])
def test_launch_barrier_retains_old_branch_with_nested_recycled_pid(
    ephemeral: bool,
) -> None:
    snapshot = [
        _wrap_snap()[0],
        _proc(
            WRAP_CHILD_PID,
            WRAP_LAUNCHER_PID,
            "codex.exe",
            "codex exec --json",
            WRAP_CHILD_START,
        ),
        _proc(302, WRAP_CHILD_PID, "node.exe", "node tool.js", _ps_iso(700000)),
        _proc(303, 302, "pwsh.exe", "pwsh old.ps1", _ps_iso(800000)),
    ]
    entry = _owned_tree_plan(
        snapshot,
        request_id="rr-barrier-nested-recycle",
    )["next_state"]
    state = (
        {"ephemeral_reviewers": {"active": {"lr-barrier": entry}}}
        if ephemeral
        else {"agents": {"worker": entry}}
    )
    recycled_parent = _proc(
        302,
        1,
        "unrelated.exe",
        "unrelated parent replacement",
        _ps_iso(900000),
    )
    parent_filetime = int(recycled_parent["start_filetime"])
    recycled_child = _proc(
        303,
        recycled_parent["pid"],
        "pwsh.exe",
        "pwsh old-branch.ps1",
        recycled_parent["start_time"],
        start_filetime=str(parent_filetime - 10),
    )

    result = sup.evaluate_launch_barrier(
        [recycled_parent, recycled_child],
        state,
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),
        request_id="lr-barrier" if ephemeral else None,
    )

    assert result["blocked"] is True
    assert result["reason"] == "owned_descendant_edge_survived"
    assert result["survivors"] == [
        {
            "kind": "owned_descendant_edge",
            "pid": recycled_child["pid"],
            "name": "pwsh.exe",
        }
    ]


def test_owned_process_absence_certificate_requires_definite_identity_result() -> None:
    tree = _owned_tree_plan(
        _wrap_snap(),
        request_id="rr-absence-identity",
    )["next_state"]["owned_process_tree"]
    leaf = tree["entries"][-1]

    absent = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree,
        [],
        now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert absent["status"] == "absent"

    recycled = _proc(
        leaf["pid"],
        leaf["parent_pid"],
        "codex.exe",
        "codex recycled",
        _ps_iso(990000),
    )
    recycled_result = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree,
        [recycled],
        now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert recycled_result["status"] == "absent"

    ambiguous = dict(recycled)
    ambiguous["start_time"] = None
    ambiguous["start_filetime"] = None
    ambiguous_result = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree,
        [ambiguous],
        now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert ambiguous_result["status"] == "invalid"

    duplicate_result = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree,
        [dict(snapshot_row := _wrap_snap()[-1]), {
            **snapshot_row,
            "start_time": _ps_iso(990000),
            "start_filetime": _ps_filetime(990000),
        }],
        now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert duplicate_result["status"] == "invalid"
    assert duplicate_result["reason_code"] == (
        "process_tree_invalid_snapshot_duplicate_pid"
    )

    invalid_result = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree,
        [{**snapshot_row, "parent_pid": "not-an-int"}],
        now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert invalid_result["status"] == "invalid"
    assert invalid_result["reason_code"] == (
        "process_tree_invalid_snapshot_invalid_process_row"
    )


# ------------------------------------------------------ Task #150: the HOLD
# must not pre-empt the LAUNCH path for a confirmed-absent agent.


def test_unverified_owned_process_tree_promotes_invalid_to_absent_when_entries_complete_and_gone() -> None:
    """Task #150 connector finding: an invalid tree is not categorically
    excluded from absence re-derivation - only an invalid tree whose
    entries are a COMPLETE description of what was found (omitted_count ==
    0, at least one entry) qualifies, the same evidentiary bar the
    complete/absent case already trusts. The production incident: a single
    UNRELATED row elsewhere in that poll's whole-host snapshot failed
    _snapshot_pid_integrity_error and marked EVERY currently-polled agent's
    tree invalid that poll, even though each agent's own entries were
    untouched and remained a complete, faithful record."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-invalid-promotes",
    )["next_state"]["owned_process_tree"]
    poisoned = dict(tree)
    poisoned["status"] = "invalid"
    poisoned["reason_code"] = "process_tree_invalid_snapshot_invalid_process_row"
    assert poisoned["omitted_count"] == 0
    assert poisoned["entries"]

    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        poisoned, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "absent"
    assert result["reason_code"] == "process_tree_absent"


def test_unverified_owned_process_tree_truncated_never_promotes_even_when_gone() -> None:
    """A truncated prior's entries are incomplete by definition
    (omitted_count > 0) - proving the RECORDED subset gone says nothing
    about whatever the size cap dropped, so it must never promote to
    absent, unlike an invalid-but-complete prior."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-truncated-never",
    )["next_state"]["owned_process_tree"]
    truncated = dict(tree)
    truncated["status"] = "truncated"
    truncated["reason_code"] = "process_tree_truncated"
    truncated["omitted_count"] = 3
    truncated["observed_count"] = truncated["recorded_count"] + 3

    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        truncated, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "truncated"


def test_unverified_owned_process_tree_invalid_with_no_entries_never_promotes() -> None:
    """A schema-drift/generation-adoption placeholder (built by
    _invalid_owned_process_tree_record, entries always []) never had a real
    tree walk to begin with - there is nothing to disprove against, so it
    must stay sticky HOLD evidence even against a fully empty snapshot,
    guarding the vacuous-truth trap (all(state in ... for state in []) is
    True)."""
    placeholder = sup._invalid_owned_process_tree_record(  # noqa: SLF001
        agent="worker",
        root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation=None,
        launch_nonce=None,
        now_epoch=NOW,
        reason_code="process_tree_invalid_prior_record_invalid",
    )
    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        placeholder, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "invalid"


def test_unverified_owned_process_tree_invalid_conversion_does_not_inherit_rejected_count() -> None:
    """Task #150 round 11 finding 1: converting a previously COMPLETE tree
    to invalid (because THIS poll's runtime record is invalid or
    mismatched, not because of anything about the entries themselves)
    performs no walk of its own - the resulting record must not carry the
    original walk's rejected_count forward as if it were this poll's own
    answer. Left uncorrected, a LATER poll's entries_are_complete check
    would read that stale zero as proof this invalid record is complete,
    re-admitting it for absence-promotion on an accounting question
    nobody actually asked this poll. Missing reads as unknown - already
    ineligible for re-derivation - which is the correct meaning: this
    poll genuinely does not know. Same "a producer that doesn't walk must
    not claim to have walked" rule round 4 already applied to the
    PowerShell post-kill barrier's own mutation sites."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-round11-rejected-count-carry",
    )["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert tree.get("rejected_count") == 0

    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree, _wrap_snap(), now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "invalid"
    assert "rejected_count" not in result


def test_unverified_owned_process_tree_invalid_stays_held_when_entry_still_alive() -> None:
    """The safety half: an invalid-but-complete prior must NOT promote to
    absent when the current snapshot proves an entry is still alive -
    exactly the same bar the complete/absent case already uses."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-still-alive",
    )["next_state"]["owned_process_tree"]
    poisoned = dict(tree)
    poisoned["status"] = "invalid"
    poisoned["reason_code"] = "process_tree_invalid_snapshot_invalid_process_row"

    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        poisoned, _wrap_snap(), now_epoch=NOW + 1,
        reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "invalid"


def test_plan_actions_confirmed_absent_wrapper_recovers_despite_unrelated_snapshot_poisoning() -> None:
    """Task #150, the production incident reproduced end to end. Poll 1: one
    UNRELATED process row on the same host (e.g. a system process CIM could
    not resolve a parent for) fails _snapshot_pid_integrity_error for that
    WHOLE poll, marking this otherwise-healthy idle wrapped agent's owned
    tree invalid even though its own row was clean - matching the
    production report exactly ("276 rows, zero failures... the detail
    string is actively misleading"). Poll 2, much later: the SAME agent is
    now confirmed completely absent from an entirely empty snapshot, yet
    the persisted tree is still that sticky invalid record from poll 1. It
    must still reach a relaunch decision (STUCK_OR_DEAD / stuck_recover)
    rather than being held on PROCESS_TREE_INVALID forever - the exact
    defect: a confirmed-absent agent could not self-heal by any means."""
    poisoning_row = {
        "pid": 4, "parent_pid": None, "name": "System",
        "command_line": None,
        "start_time": "2026-01-01T00:00:00.0000000+00:00",
        "start_filetime": None,
    }
    wrapper_row = _wrap_snap()[0]
    poisoned_snapshot = [wrapper_row, poisoning_row]

    state = {"agents": {"worker": _wrap_ready(runtime_wrapper_generation="wrapper-1")}}
    poll1 = _plan_wrap(
        _report(wrapper_runtime=_wrapper_runtime_view(phase="idle", now=NOW)),
        state,
        snapshot=poisoned_snapshot,
    )
    assert poll1["state"] == "PROCESS_TREE_INVALID"
    assert poll1["next_state"]["owned_process_tree"]["status"] == "invalid"
    assert poll1["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_snapshot_invalid_process_row"
    )
    assert "--reset-process-tree-ownership" not in poll1["reason"]
    assert "no scripted remedy applies" in poll1["reason"]
    assert "`agenttalk attention`" in poll1["reason"]

    state_after_poll1 = {
        "agents": {
            "worker": {**state["agents"]["worker"], **poll1["next_state"]},
        }
    }
    later = NOW + 600
    poll2 = _plan_wrap(
        _report(
            heartbeat_stale=True,
            wrapper_runtime=_wrapper_runtime_view(
                phase="idle", now=later, updated_age=600,
            ),
        ),
        state_after_poll1,
        snapshot=[],
        now=later,
    )

    assert poll2["state"] == "STUCK_OR_DEAD"
    assert poll2["action"] == sup.STUCK_RECOVER
    assert poll2["next_state"]["owned_process_tree"]["status"] == "absent"

    barrier = sup.evaluate_launch_barrier(
        [],
        {"agents": {"worker": {**state_after_poll1["agents"]["worker"], **poll2["next_state"]}}},
        _WRAP_CONFIG,
        "worker",
        root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
    )
    assert barrier["allow_launch"] is True
    assert barrier["blocked"] is False


def test_owned_process_tree_rejected_child_blocks_absence_promotion_blocker() -> None:
    """Task #150 connector finding 1, the BLOCKER: omitted_count only ever
    counted the gap between ADMITTED nodes and the size-cap truncation of
    already-admitted nodes - never a candidate branch the walk SAW and
    explicitly excluded for an unrelated reason. Reproduces the exact
    duplicate-wrapper hazard: the CLI launcher has exited (absent from the
    snapshot), carries no exact lifetime certificate, and left a LIVE
    child behind. The launcher is excluded as an unproven virtual-parent
    descendant - so its child is never even reached by the walk - while
    omitted_count stays 0 (only the wrapper was ever admitted, so there is
    no size-cap gap to report). Without rejected_count, this record would
    satisfy the round-1 eligibility bar and promote to absent the moment
    the wrapper also exits, even though the launcher's child is still
    alive and would then get orphaned by a replacement wrapper launching
    alongside it."""
    exited_launcher_pid = WRAP_CHILD_PID  # the CLI launcher, per _owned_process_tree's terms
    live_grandchild_pid = 999
    snapshot = [
        _wrap_snap()[0],  # the wrapper - alive
        # exited_launcher_pid itself is deliberately ABSENT from the snapshot.
        _proc(
            live_grandchild_pid, exited_launcher_pid, "node.exe", "node tool.js",
            _ps_iso(700000),
        ),
    ]
    plan = _owned_tree_plan(
        snapshot,
        request_id="rr-150-blocker-rejected-child",
        # No cli_launcher_lifetime override - no exact lifetime certificate,
        # exactly the finding's precondition.
    )
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == (
        "process_tree_invalid_unproven_virtual_parent_descendant"
    )
    assert [entry["pid"] for entry in tree["entries"]] == [WRAP_LAUNCHER_PID]
    assert tree["omitted_count"] == 0, (
        "the exact hazard: the size-cap counter alone looks like a complete "
        "record even though a live child was excluded"
    )
    assert tree["rejected_count"] >= 1, (
        "the fix: a candidate the walk saw and excluded must be counted "
        "even when omitted_count cannot see it"
    )

    # The blocker, made concrete: if the wrapper ALSO now disappears, this
    # record must NOT promote to absent - the live grandchild is still
    # unaccounted for.
    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        tree, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "invalid", (
        "a rejected branch must keep this record sticky - promoting it here "
        "is the duplicate-wrapper hazard arriving through the round-1 fix"
    )


def test_wrapped_liveness_sets_refreshed_flag_when_invalid_promotes_to_absent() -> None:
    """Task #150 connector finding 4: _current_proof_failed used to set
    owned_process_tree_refreshed only when the PRIOR status was already
    {complete, absent} - a stale duplicate of _unverified_owned_process_
    tree's own eligibility gate that drifted the moment that gate widened
    to admit an invalid-but-complete prior too. A promoted-invalid record
    (tree.status becomes "absent") must set this flag exactly like a
    promoted-complete one does - it is the same call, the same real
    _wrapped_liveness (shared by configured AND ephemeral agents), driven
    through the identical poisoned-poll-then-absent-poll sequence as the
    end-to-end test above, but reading the intermediate liveness dict
    directly rather than the final plan."""
    poisoning_row = {
        "pid": 4, "parent_pid": None, "name": "System",
        "command_line": None,
        "start_time": "2026-01-01T00:00:00.0000000+00:00",
        "start_filetime": None,
    }
    st = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    cfg_agent = _WRAP_CONFIG["agents"]["worker"]
    liveness1 = sup._wrapped_liveness(  # noqa: SLF001
        [_wrap_snap()[0], poisoning_row],
        st, cfg_agent, "worker", NOW,
        _wrapper_runtime_view(phase="idle", now=NOW),
        root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
    )
    assert liveness1["owned_process_tree"]["status"] == "invalid"

    st_after_poll1 = {**st, "owned_process_tree": liveness1["owned_process_tree"]}
    later = NOW + 600
    liveness2 = sup._wrapped_liveness(  # noqa: SLF001
        [],
        st_after_poll1, cfg_agent, "worker", later,
        _wrapper_runtime_view(phase="idle", now=later, updated_age=600),
        root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
    )
    assert liveness2["owned_process_tree"]["status"] == "absent"
    assert liveness2["owned_process_tree_refreshed"] is True, (
        "an ephemeral reviewer reads exactly this flag to decide "
        "teardown_ready - unset here means held rather than archived, "
        "indefinitely, under supervise --once"
    )


def test_ephemeral_teardown_ready_when_wrapped_liveness_reports_refreshed_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of finding 4, isolating _ephemeral_owned_process_view's
    consumption of both refreshed-tree and matched-recognition authority via the same
    fake-_wrapped_liveness pattern the existing module_args_from
    reconstruction test uses - so a regression in either the producer
    (_wrapped_liveness, tested above) or this consumer is caught
    independently."""
    def _fake_wrapped_liveness(snapshot, entry, cfg_agent, agent, now_epoch,
                               runtime_view, *, root_key):
        return {
            "kill_targets": [],
            "owned_process_tree": {"status": "absent", "reason_code": "process_tree_absent"},
            "owned_process_tree_refreshed": True,
            "wrapper_recognition": {"status": "matched", "reason_code": "matched"},
            "child_reason": "test_absent",
        }

    monkeypatch.setattr(sup, "_wrapped_liveness", _fake_wrapped_liveness)
    entry = {
        "request_id": "R1", "agent": "adversary-1", "cli": "codex",
        "launch": {},
    }
    _next_entry, _liveness, teardown_ready = sup._ephemeral_owned_process_view(  # noqa: SLF001
        [], entry, {}, NOW, root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
    )
    assert teardown_ready is True


def test_process_tree_hold_message_names_no_remedy_for_entryless_placeholder() -> None:
    """Task #150 connector finding 3: --reset-process-tree-ownership requires
    the tree evidence to carry a wrapper_generation/launch_nonce that agrees
    with a CURRENT, valid runtime record - true for a tree
    _owned_process_tree itself walked, but an entryless placeholder
    (_invalid_owned_process_tree_record - schema drift, a revoked runtime,
    legacy migration evidence) has no such nonce to agree with, so the
    command fails for precisely the case that names it. The HOLD message
    for an entryless record must say so rather than naming a command that
    cannot work."""
    st = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    # A schema-drifted persisted record - _valid_owned_process_tree returns
    # None for it. Reaching the EMPTY-entries placeholder specifically
    # requires a FAILED wrapper-liveness check (here: the wrapper is fully
    # absent) - a HEALTHY wrapper reaches _owned_process_tree's own fresh
    # walk instead, which still populates real entries even with the same
    # malformed prior (a different mechanism, same reason_code text - not
    # this finding).
    st["owned_process_tree"] = {"not": "the expected schema at all"}
    plan = _plan_wrap(
        _report(
            heartbeat_stale=True,
            wrapper_runtime=_wrapper_runtime_view(phase="idle", now=NOW),
        ),
        {"agents": {"worker": st}},
        snapshot=[],
    )
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_prior_record_invalid"
    )
    assert not plan["next_state"]["owned_process_tree"]["entries"]
    assert "--reset-process-tree-ownership" not in plan["reason"]
    assert "no scripted remedy applies" in plan["reason"]
    assert "process_tree_invalid_prior_record_invalid" not in plan["reason"]
    assert "could not establish complete current ownership" in plan["reason"]


def test_process_tree_truncated_refusal_names_the_missing_evidence_not_a_reset() -> None:
    """A live supervisor cannot truthfully advertise its stopped-only reset."""
    limit = sup._OWNED_PROCESS_TREE_LIMIT  # noqa: SLF001
    snapshot = [_wrap_snap()[0]] + [
        _proc(400 + i, WRAP_LAUNCHER_PID, "node.exe", "node tool.js", _ps_iso(700000 + i))
        for i in range(limit)
    ]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-truncated-remedy")
    assert plan["state"] == "PROCESS_TREE_TRUNCATED"
    assert plan["next_state"]["owned_process_tree"]["omitted_count"] > 0
    assert "observed 65 identities over the safe cap 64" in plan["reason"]
    assert "--reset-process-tree-ownership" not in plan["reason"]
    assert "no scripted remedy applies" in plan["reason"]


def test_v2_record_missing_rejected_count_stays_valid_but_ineligible() -> None:
    """Task #150 round 3 connector finding 2, the blocker on round 2's own
    blocker fix: a bare schema_version bump would have made every record
    persisted before rejected_count existed fail validation SOLELY on the
    new field's absence - and on the wrapped-liveness path a live,
    healthy wrapper then rebuilds that now-"invalid" prior into a fresh
    process_tree_invalid_prior_record_invalid on EVERY poll from then on,
    reintroducing the sticky fleet-wide HOLD this PR exists to remove, at
    the moment of upgrade, for agents that were never anything but
    healthy. rejected_count is therefore OPTIONAL in the persisted schema:
    a v2-shaped record (missing it entirely) must still validate as
    authoritative - proving the upgrade itself does not brick a healthy
    fleet.

    The walk_complete inversion: eligibility now reads walk_complete, not
    rejected_count/omitted_count directly, so a genuinely pre-inversion
    record must be missing BOTH optional fields to correctly read as
    unknown/ineligible - a record missing only rejected_count (walk_complete
    still True, honestly earned by whatever walk set it) is a schema state
    the inversion says IS eligible, since rejected_count stopped being
    independently load-bearing for the safety decision. Case (d) asserts
    that directly: it is the inversion's own defining property, not
    incidental behavior."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-v2-migration",
    )["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert tree["walk_complete"] is True
    pre_inversion_shaped = dict(tree)
    del pre_inversion_shaped["rejected_count"]
    del pre_inversion_shaped["walk_complete"]

    # (a) still readable/valid authority - the upgrade itself must not
    # brick a healthy agent's own persisted record.
    validated = sup._valid_owned_process_tree(  # noqa: SLF001
        pre_inversion_shaped, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    )
    assert validated is not None
    assert "rejected_count" not in validated
    assert "walk_complete" not in validated

    # (b) a healthy, ALIVE wrapper polling against this pre-inversion prior
    # must NOT be forced into PROCESS_TREE_INVALID merely because its own
    # persisted record predates rejected_count/walk_complete - the exact
    # self-inflicted regression the bare version bump caused.
    st = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    st["owned_process_tree"] = pre_inversion_shaped
    healthy_plan = _plan_wrap(
        _report(wrapper_runtime=_wrapper_runtime_view(phase="idle", now=NOW)),
        {"agents": {"worker": st}},
        snapshot=_wrap_snap(),
    )
    assert healthy_plan["state"] != "PROCESS_TREE_INVALID"
    assert healthy_plan["next_state"]["owned_process_tree"]["status"] == "complete"

    # (c) once invalid for an UNRELATED reason, a pre-inversion prior must
    # not be promoted to absent just because omitted_count reads 0 - a
    # missing walk_complete must block eligibility exactly like an explicit
    # False would.
    poisoned = dict(pre_inversion_shaped)
    poisoned["status"] = "invalid"
    poisoned["reason_code"] = "process_tree_invalid_snapshot_invalid_process_row"
    result = sup._unverified_owned_process_tree(  # noqa: SLF001
        poisoned, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert result["status"] == "invalid"

    # (d) the inversion's own defining property: a record missing ONLY
    # rejected_count (walk_complete still True, honestly earned by the walk
    # that set it) IS eligible - rejected_count stopped being independently
    # load-bearing for this decision the moment walk_complete existed to
    # carry it. Same snapshot, same poisoning, only the missing field
    # differs from case (c).
    missing_rejected_only = dict(tree)
    del missing_rejected_only["rejected_count"]
    missing_rejected_only["status"] = "invalid"
    missing_rejected_only["reason_code"] = "process_tree_invalid_snapshot_invalid_process_row"
    promoted = sup._unverified_owned_process_tree(  # noqa: SLF001
        missing_rejected_only, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert promoted["status"] == "absent"


def test_validator_refuses_complete_or_absent_with_nonzero_rejected_count() -> None:
    """Task #150 round 3 connector finding 4: the validator parsed
    rejected_count but never constrained it against status - a corrupted
    or partially-updated record claiming complete/absent while also
    recording a nonzero rejection is internally contradictory (those two
    statuses both promise nothing was excluded) and must not read as
    authoritative just because the count field itself still parses."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-validator-constraint",
    )["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    corrupted = dict(tree)
    corrupted["rejected_count"] = 1
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        corrupted, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is None

    absent_tree = dict(corrupted)
    absent_tree["status"] = "absent"
    absent_tree["reason_code"] = "process_tree_absent"
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        absent_tree, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is None

    # The control: rejected_count == 0 on complete/absent is exactly what
    # every real construction site already writes, and must keep validating.
    clean = dict(tree)
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        clean, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None


def test_validator_refuses_walk_complete_true_with_false_preconditions() -> None:
    """Task #150 walk_complete inversion connector finding: without this
    check the inversion is a REGRESSION, not hardening. The argument for
    walk_complete is that a reader should trust one positive flag rather
    than re-derive completeness from fields that can be inherited or
    defaulted - but a validator that only checks the field's TYPE lets a
    persisted record claim walk_complete True while omitted_count or
    rejected_count is actually nonzero, or entries is empty. The old
    entries_are_complete re-derivation would have caught exactly that
    record; a bare type-check on a lone boolean does not - strictly worse
    than what it replaced. Uses status "invalid" throughout (not complete/
    absent) so this asserts the NEW True-implies-conjunction check in
    isolation from the existing nonzero-rejected_count-on-complete/absent
    check tested above."""
    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-walkcomplete-validator-constraint",
    )["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert tree["walk_complete"] is True

    def as_invalid_with(**overrides: object) -> dict:
        corrupted = dict(tree)
        corrupted["status"] = "invalid"
        corrupted["reason_code"] = "process_tree_invalid_test"
        corrupted.update(overrides)
        return corrupted

    # walk_complete True but rejected_count says otherwise.
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        as_invalid_with(rejected_count=1), agent="worker",
        root_key=sup._root_key(TEST_ROOT), wrapper_generation="wrapper-1",  # noqa: SLF001
        launch_nonce=SUPERVISOR_NONCE,
    ) is None
    # walk_complete True but omitted_count says otherwise (also bumping
    # observed_count/truncated to keep the OTHER count cross-checks from
    # masking this one behind an unrelated rejection).
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        as_invalid_with(
            omitted_count=1, observed_count=tree["observed_count"] + 1, truncated=True,
        ),
        agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is None
    # walk_complete True but rejected_count is UNKNOWN (missing) - the walk
    # that supposedly earned the flag could not have known rejected_count
    # was zero without recording it.
    walk_complete_no_rejected = as_invalid_with()
    del walk_complete_no_rejected["rejected_count"]
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        walk_complete_no_rejected, agent="worker",
        root_key=sup._root_key(TEST_ROOT), wrapper_generation="wrapper-1",  # noqa: SLF001
        launch_nonce=SUPERVISOR_NONCE,
    ) is None
    # walk_complete True but entries is empty (only legal for status
    # "invalid", which this already is - isolates entries specifically).
    empty_entries = as_invalid_with()
    empty_entries["entries"] = []
    empty_entries["recorded_count"] = 0
    empty_entries["omitted_count"] = empty_entries["observed_count"]
    empty_entries["truncated"] = empty_entries["observed_count"] > 0
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        empty_entries, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is None

    # The inverse direction, explicitly: a clean conjunction with
    # walk_complete absent or False must stay VALID (not rejected) and
    # simply unknown/ineligible - the validator must never demand the
    # flag be present.
    missing_flag = as_invalid_with()
    del missing_flag["walk_complete"]
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        missing_flag, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None
    false_flag = as_invalid_with(walk_complete=False)
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        false_flag, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None

    # The control: a genuinely clean, honestly-earned walk_complete True
    # keeps validating - the fix must not reject real construction sites.
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        as_invalid_with(), agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None

    # The operator-visible outcome: a contradictory record can never even
    # become `prior` for eligibility - it fails the trust boundary
    # (_valid_owned_process_tree) before _unverified_owned_process_tree
    # ever reads walk_complete, so a wrapped poll HOLDS
    # (process_tree_invalid_prior_record_invalid) rather than promoting to
    # absent, exactly as if the record were malformed any other way.
    state = _wrap_ready(runtime_wrapper_generation="wrapper-1")
    state["owned_process_tree"] = as_invalid_with(rejected_count=1)
    plan = _plan_wrap(
        _report(heartbeat_stale=False, wrapper_runtime=_wrapper_runtime_view(phase="idle")),
        {"agents": {"worker": state}},
        snapshot=_wrap_snap(),
    )
    assert plan["action"] == sup.WARN_ONLY
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["owned_process_tree"]["reason_code"] == (
        "process_tree_invalid_prior_record_invalid"
    )


def test_snap_index_excludes_rather_than_picks_a_duplicate_pid() -> None:
    """Task #150 round 3 connector finding 1: last-write-wins on a
    duplicate pid silently picks ONE of two rows - if a malformed or
    unrelated row wins over a genuinely live child, every later
    idx.get(that_pid) call sees the wrong identity with no signal
    anything was lost. Excluding the duplicated pid entirely makes it
    "not found" for every caller, which the existing missing_child_row
    rejection already handles correctly."""
    good_child = _proc(999, WRAP_LAUNCHER_PID, "node.exe", "node tool.js", _ps_iso(700000))
    duplicate_bad_row = {**good_child, "parent_pid": None}
    idx = sup._snap_index_and_excluded([_wrap_snap()[0], good_child, duplicate_bad_row])[0]  # noqa: SLF001
    assert 999 not in idx
    assert WRAP_LAUNCHER_PID in idx  # unrelated pids are unaffected


def test_owned_process_tree_duplicate_pid_orphan_still_rejected() -> None:
    """Task #150 round 4 connector finding 1: _snap_index's own exclusion
    popped the duplicated pid's row out of idx BEFORE _children_map(idx)
    was built (round 3's `children`), which destroyed the only edge
    connecting the duplicated pid to its OWN parent - so neither the main
    walk nor the discovery-closure invariant could ever reach it, or
    anything below it. The whole-snapshot "duplicate_pid" fact still set
    the tree invalid, but rejected_count stayed 0 - a path that lost two
    real candidates (the duplicated intermediate and its live child)
    without saying so, the exact silent-loss class this invariant exists
    to close, just one level earlier than round 3 checked."""
    intermediate_pid = 500
    live_child_pid = 501
    duplicated_intermediate = _proc(
        intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000),
    )
    duplicate_of_same_pid = {
        **duplicated_intermediate,
        "start_time": _ps_iso(700001),
    }
    snapshot = [
        *_wrap_snap(),
        duplicated_intermediate,
        duplicate_of_same_pid,
        _proc(live_child_pid, intermediate_pid, "node.exe", "node build.js", _ps_iso(800000)),
    ]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-duplicate-pid-orphan")
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert "duplicate_pid" in tree["reason_code"]
    assert tree["rejected_count"] >= 1
    assert intermediate_pid not in [e["pid"] for e in tree["entries"]]
    assert live_child_pid not in [e["pid"] for e in tree["entries"]]


def test_owned_process_tree_recycled_root_old_side_child_still_counted() -> None:
    """Task #150 round 4 connector finding 2: round 3's recycled-root
    exclusion was too blanket - it treated EVERY current child of a
    recycled pid as the replacement's foreign descendant, but a child
    whose exact start strictly PREDATES the replacement belongs to the
    OLD, genuinely-owned process, not the new one. evaluate_launch_barrier
    already draws exactly this line (see its recycled_parent_rows/
    replacement_key handling); the discovery-closure invariant must agree
    with it rather than drawing its own, wider one.

    Modeled directly on test_owned_process_tree_recycled_launcher_ignores_
    foreign_children (the test whose "990" fixture forced round 3's
    blanket exclusion): the replacement's own parent_pid is unrelated
    (not the wrapper), and no lifetime certificate is configured, so
    launcher_pid is admitted by NEITHER the declared-launcher path NOR
    generic BFS - the discovery-closure invariant is the only thing that
    can reach anything below it at all. That test's own foreign child
    postdates the replacement and must stay excluded (unchanged here);
    this test adds a second child that PREDATES the replacement and must
    now be caught as a real, unaccounted loss instead of silently
    dropped alongside it."""
    old_child_pid = 890
    old_child_start = _ps_iso(700000)  # predates the replacement below
    replacement_start = _ps_iso(800000)
    foreign_child_pid = 990
    foreign_child_start = _ps_iso(900000)  # postdates the replacement

    snapshot = [
        _wrap_snap()[0],
        _proc(WRAP_CHILD_PID, 1, "unrelated.exe", "unrelated replacement", replacement_start),
        _proc(old_child_pid, WRAP_CHILD_PID, "node.exe", "node build.js", old_child_start),
        _proc(foreign_child_pid, WRAP_CHILD_PID, "node.exe", "node foreign.js", foreign_child_start),
    ]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-recycled-old-side")
    tree = plan["next_state"]["owned_process_tree"]
    # The old-side child is a real, unaccounted loss (its true parent is
    # gone, replaced) - the invariant must now catch it.
    assert tree["status"] == "invalid"
    assert tree["rejected_count"] >= 1
    entry_pids = {e["pid"] for e in tree["entries"]}
    assert old_child_pid not in entry_pids  # rejected, never admitted
    # The replacement's OWN (foreign) child must still NOT be counted as
    # ours - unchanged from the existing sibling test's own assertion.
    assert foreign_child_pid not in entry_pids


def test_post_kill_barrier_hold_cannot_present_a_trustworthy_zero() -> None:
    """Task #150 round 4 connector finding 3: the post-kill and ephemeral
    teardown barriers (the .ps1 template, around the relaunch/stuck_recover
    and ephemeral_complete/timeout/failed branches) are a SECOND producer
    of invalid owned_process_tree records - they mutate a formerly-complete
    tree's status/reason_code straight in PowerShell, entirely outside
    _owned_process_tree's own walk, and never ran any accounting of their
    own. Before this fix they left rejected_count untouched at its old
    "complete" value (0), so process_tree_invalid_post_kill_* sailed
    through _unverified_owned_process_tree's eligibility bar even though
    the barrier's own survivors were never counted anywhere. The fix
    removes the property entirely rather than writing a number - the same
    "missing means unknown, not zero" shape the v2-migration fix already
    established for a field a record simply never had, reused here for a
    field this record's PRODUCER never computed.

    The walk_complete inversion added a sibling removal at the same two
    mutation sites: this relabeling is a status change by the BARRIER's
    own accounting, not a Python walk, so a stale walk_complete=True would
    now be the load-bearing lie removing only rejected_count used to be -
    the fixed shape below must strip both to accurately represent what the
    barrier actually does today."""
    ps1_source = sup.PS_TEMPLATE
    assert ps1_source.count(
        "owned_process_tree.PSObject.Properties.Remove('rejected_count')",
    ) == 2
    assert ps1_source.count(
        "owned_process_tree.PSObject.Properties.Remove('walk_complete')",
    ) == 2

    tree = _owned_tree_plan(
        _wrap_snap(), request_id="rr-150-post-kill-producer",
    )["next_state"]["owned_process_tree"]
    assert tree["status"] == "complete"
    assert tree["walk_complete"] is True

    # The FIXED shape: the barrier removed rejected_count AND walk_complete
    # entirely when it externally invalidated the tree. Still valid,
    # readable authority - just not eligible for the walk's own
    # re-derivation.
    post_kill_fixed = dict(tree)
    post_kill_fixed["status"] = "invalid"
    post_kill_fixed["reason_code"] = "process_tree_invalid_post_kill_launch_barrier_unavailable"
    del post_kill_fixed["rejected_count"]
    del post_kill_fixed["walk_complete"]
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        post_kill_fixed, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None
    held = sup._unverified_owned_process_tree(  # noqa: SLF001
        post_kill_fixed, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert held["status"] == "invalid"

    # The PRE-FIX shape, for contrast: rejected_count AND walk_complete
    # both left at their stale "complete" values - this is exactly the
    # hazard finding 3 named, and it genuinely does promote, proving the
    # fix closes a real gap rather than a hypothetical one.
    post_kill_stale = dict(tree)
    post_kill_stale["status"] = "invalid"
    post_kill_stale["reason_code"] = "process_tree_invalid_post_kill_launch_barrier_unavailable"
    promoted = sup._unverified_owned_process_tree(  # noqa: SLF001
        post_kill_stale, [], now_epoch=NOW + 1, reason_code="process_tree_invalid_test",
    )
    assert promoted["status"] == "absent"


def test_reset_remedy_warns_on_legacy_v2_record_missing_rejected_count() -> None:
    """Task #150 round 5 connector finding 2: the round-3 remedy clause
    only fired for a KNOWN positive rejected_count - a legacy v2 record
    (missing the field entirely, deliberately still valid authority per
    the v2-migration fix) got NO warning at all, telling the operator to
    verify only the named/omitted identities when the truth is the walk
    that produced this record never counted rejections in the first
    place. Unknown must warn like unknown, not silently read as zero -
    the same "absent must warn like unknown" correction finding 6 made
    for the round-2 remedy, now needed again for the same field's
    missing-vs-zero distinction."""
    limit = sup._OWNED_PROCESS_TREE_LIMIT
    snapshot = [_wrap_snap()[0]] + [
        _proc(400 + i, WRAP_LAUNCHER_PID, "node.exe", "node tool.js", _ps_iso(700000 + i))
        for i in range(limit)
    ]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-v2-remedy-first")
    assert plan["state"] == "PROCESS_TREE_TRUNCATED"
    truncated_tree = plan["next_state"]["owned_process_tree"]
    v2_shaped = dict(truncated_tree)
    del v2_shaped["rejected_count"]
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        v2_shaped, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None

    second_state = dict(plan["next_state"])
    second_state["owned_process_tree"] = v2_shaped
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-150-v2-remedy-second"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": second_state}},
        now=NOW + 1,
        snapshot=snapshot,
    )
    assert second["state"] == "PROCESS_TREE_TRUNCATED"
    assert "rejected_count" not in second["next_state"]["owned_process_tree"]
    item = att.process_tree_hold_items(
        {"agents": {"worker": second["next_state"]}},
        reset_admissions={"evaluated": True, "admissions": {}},
    )[0]
    assert "UNKNOWN, not zero" in item["recommendation"]
    assert "Ownership record carries no rejected-candidate accounting" in (
        item["recommendation"]
    )
    assert "Operator must confirm" in item["recommendation"]


def test_owned_process_tree_rejects_walk_when_launcher_missing_exact_filetime() -> None:
    """Task #150 round 3 connector finding 3: a Windows row lacking exact
    start_filetime used to be ADMITTED by add_node while
    _valid_owned_process_tree independently REFUSES that same entry for
    status complete/absent - the two disagreeing with each other meant a
    tree carrying this entry could reach "absent" under round 2's own
    eligibility check and then fail its own schema validation the moment
    anything re-read it. mark_rejected on this path means such an entry
    can never contribute to a rejected_count of 0, so it can never reach
    a status the validator would then refuse."""
    launcher_no_filetime = {
        **_proc(WRAP_CHILD_PID, WRAP_LAUNCHER_PID, "codex.exe", "codex exec --json", WRAP_CHILD_START),
        "start_filetime": None,
    }
    snapshot = [_wrap_snap()[0], launcher_no_filetime]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-imprecise-filetime")
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["rejected_count"] >= 1
    # The entry is still captured (admitted), just never eligible to reach
    # a status the validator would then refuse.
    assert WRAP_CHILD_PID in [e["pid"] for e in tree["entries"]]


def test_owned_process_tree_accounts_for_orphaned_intermediates_live_child() -> None:
    """Task #150 round 3 connector finding 5: a rebuild after a corrupt
    prior cannot reach an orphan below an already-exited intermediate
    parent, because the rehydration loop that would normally re-admit a
    prior descendant as a virtual bridge is gated entirely behind a
    TRUSTED prior_authority (status == "complete"). An invalid prior that
    is still READABLE (schema-valid, non-empty entries, just not trusted
    for rehydration - the generation-adoption-pending exemption is the
    one invalid reason_code this walk still rebuilds against instead of
    holding on) never runs that rehydration loop at all, so a live
    descendant whose ONLY evidence is the untrusted prior's own entries
    goes completely unaccounted: not admitted, not omitted, not rejected
    - simply invisible. The discovery-closure reconciliation catches this
    mechanically because it seeds discovery roots from every prior entry
    regardless of whether prior_authority ended up trusted, then expands
    through the CURRENT snapshot's raw parent-child edges - which still
    names the orphan's live child even though admission logic never
    walks into it."""
    orphaned_intermediate_pid = 500
    live_grandchild_pid = 501
    first_snapshot = [
        *_wrap_snap(),
        _proc(orphaned_intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
        _proc(live_grandchild_pid, orphaned_intermediate_pid, "node.exe", "node build.js", _ps_iso(800000)),
    ]
    first = _owned_tree_plan(first_snapshot, request_id="rr-150-orphan-first")
    complete_tree = first["next_state"]["owned_process_tree"]
    assert complete_tree["status"] == "complete"
    assert {e["pid"] for e in complete_tree["entries"]} >= {
        orphaned_intermediate_pid,
        live_grandchild_pid,
    }

    # Recast the same, otherwise-untouched complete tree as an untrusted-
    # but-readable prior - the one invalid reason_code this walk rebuilds
    # against rather than holding on - so prior_authority becomes None
    # while prior_entries (still carrying the orphan and its child) stays
    # populated. This is deliberately hand-built rather than reached via
    # a real generation-adoption poll: the mechanism under test is
    # whether the WALK correctly ignores an untrusted prior's rehydration
    # authority while still using its entries for discovery, not whether
    # this exact status/reason_code/entries combination is reachable
    # through today's callers.
    untrusted_state = dict(first["next_state"])
    untrusted_tree = dict(complete_tree)
    untrusted_tree["status"] = "invalid"
    untrusted_tree["reason_code"] = "process_tree_invalid_generation_adoption_pending"
    untrusted_state["owned_process_tree"] = untrusted_tree
    assert sup._valid_owned_process_tree(  # noqa: SLF001
        untrusted_tree, agent="worker", root_key=sup._root_key(TEST_ROOT),  # noqa: SLF001
        wrapper_generation="wrapper-1", launch_nonce=SUPERVISOR_NONCE,
    ) is not None

    # Second poll: the intermediate has exited (no row at all); its child
    # remains live, now a true orphan in the CURRENT snapshot - exactly
    # the shape an untrusted prior's own rehydration can no longer reach.
    second_snapshot = [
        *_wrap_snap(),
        _proc(live_grandchild_pid, orphaned_intermediate_pid, "node.exe", "node build.js", _ps_iso(800000)),
    ]
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-150-orphan-second"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": untrusted_state}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )
    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["rejected_count"] >= 1
    assert live_grandchild_pid not in [e["pid"] for e in tree["entries"]]


def test_owned_process_tree_does_not_double_count_same_rejected_candidate() -> None:
    """Task #150 round 9 connector finding: a candidate rejected once
    during the main walk (here, an unproven virtual-parent descendant
    below a TRUSTED but now-exited intermediate) is ALSO absent from
    `owned`, so the discovery-closure loop finds it again and used to
    call mark_rejected a second time for the identical pid - one lost
    candidate reported as two. Round 3's own "harmless, rejected_count
    is a gate not a tally" justification for this was true when written
    and stopped being true the moment round 3 (and round 5) added an
    operator-facing message that reports the number as a count - a
    value's tolerance for imprecision is a property of its consumers,
    not the value itself. Asserts the operator-visible outcome: the
    remedy message reports ONE excluded candidate, not two."""
    intermediate_pid = 500
    anchor_child_pid = 501
    new_child_pid = 502
    first_snapshot = [
        *_wrap_snap(),
        _proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
        _proc(anchor_child_pid, intermediate_pid, "node.exe", "node anchor.js", _ps_iso(800000)),
    ]
    first = _owned_tree_plan(first_snapshot, request_id="rr-150-no-double-count-first")
    complete_tree = first["next_state"]["owned_process_tree"]
    assert complete_tree["status"] == "complete"
    assert {e["pid"] for e in complete_tree["entries"]} >= {intermediate_pid, anchor_child_pid}

    # Second poll: the intermediate itself has exited (no row at all, so
    # prior_authority's own rehydration must rebuild it as a virtual
    # bridge via anchor_child_pid, still alive, needing it as an
    # ancestor). A brand-new live child - never recorded in the prior -
    # appears below that same virtual bridge: unproven at the main-walk
    # site (line ~3455) AND separately reachable via discovery_edges
    # from the same intermediate_pid discovery root.
    second_snapshot = [
        *_wrap_snap(),
        _proc(anchor_child_pid, intermediate_pid, "node.exe", "node anchor.js", _ps_iso(800000)),
        _proc(new_child_pid, intermediate_pid, "node.exe", "node new.js", _ps_iso(900000)),
    ]
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-150-no-double-count-second"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )
    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert new_child_pid not in [e["pid"] for e in tree["entries"]]
    # The internal counter, checked once for context:
    assert tree["rejected_count"] == 1
    # The operator-visible outcome the standing test rule asks for: the
    # remedy message must report ONE excluded candidate, not two.
    item = att.process_tree_hold_items(
        {"agents": {"worker": second["next_state"]}},
        reset_admissions={"evaluated": True, "admissions": {}},
    )[0]
    assert "excludes 1 candidate identity" in item["recommendation"]
    assert "excludes 2 candidate identities" not in item["recommendation"]
    assert "Operator must confirm" in item["recommendation"]


def test_owned_process_tree_prior_entry_malformed_pid_still_finds_live_child() -> None:
    """Task #150 round 9 connector finding 3: a row whose OWN pid field is
    malformed (as opposed to round 5's malformed parent_pid) contributes
    no edge either direction, since there is no valid pid to key it by.
    This test confirms the case the connector says is already safe: for
    a PRIOR-TRACKED intermediate, the trusted-rehydration block never
    even looks at this poll's malformed row - it reconstructs the
    intermediate's identity from the prior record's own start/filetime
    and re-verifies the live child against that same prior chain,
    bypassing the malformed current row entirely. Locks in behavior that
    was already correct by construction (not a new fix) so it cannot
    silently regress."""
    intermediate_pid = 500
    live_child_pid = 501
    first_snapshot = [
        *_wrap_snap(),
        _proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
        _proc(live_child_pid, intermediate_pid, "node.exe", "node build.js", _ps_iso(800000)),
    ]
    first = _owned_tree_plan(first_snapshot, request_id="rr-150-malformed-pid-prior-first")
    complete_tree = first["next_state"]["owned_process_tree"]
    assert complete_tree["status"] == "complete"
    assert {e["pid"] for e in complete_tree["entries"]} >= {intermediate_pid, live_child_pid}

    # Second poll: same trusted prior, but the intermediate's own row now
    # has a malformed pid field - the child is unchanged and still alive.
    malformed_intermediate = {
        **_proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
        "pid": None,
    }
    second_snapshot = [
        *_wrap_snap(),
        malformed_intermediate,
        _proc(live_child_pid, intermediate_pid, "node.exe", "node build.js", _ps_iso(800000)),
    ]
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-150-malformed-pid-prior-second"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": first["next_state"]}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )
    tree = second["next_state"]["owned_process_tree"]
    # The whole-snapshot integrity check still (correctly) sees a
    # malformed row and marks the tree invalid this poll - that is
    # orthogonal to whether the specific tracked identities were lost.
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == "process_tree_invalid_snapshot_invalid_process_row"
    assert {intermediate_pid, live_child_pid} <= {e["pid"] for e in tree["entries"]}


def test_owned_process_tree_first_seen_malformed_pid_is_a_documented_residual() -> None:
    """Task #150 round 9 connector finding 3, the genuinely unbounded half:
    a first-time-seen intermediate (never a prior entry) whose row has a
    malformed pid contributes no edge either direction, so nothing ever
    learns it exists at all - not as a root (no prior_entries pid to seed
    it), not as anyone's discovered child (the one edge that would have
    named it depends on ITS OWN row, which is unusable). Its own live
    child is consequently unreachable too. Every other fix in this
    family works by looking a row up BY its own intact pid field; that
    is exactly the field missing here, and there is no snapshot-only
    signal to distinguish this row from an unrelated malformed row
    elsewhere on the host. This test documents the residual by
    execution rather than asserting a fix - see _snap_all_edges's own
    comment for why treating this as boundable would revive the
    host-wide-poisoning failure mode round 5 already ruled out."""
    intermediate_pid = 500
    live_child_pid = 501
    malformed_intermediate = {
        **_proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
        "pid": None,
    }
    snapshot = [
        *_wrap_snap(),
        malformed_intermediate,
        _proc(live_child_pid, intermediate_pid, "node.exe", "node build.js", _ps_iso(800000)),
    ]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-malformed-pid-first-seen")
    tree = plan["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["reason_code"] == "process_tree_invalid_snapshot_invalid_process_row"
    # The documented gap: neither identity is accounted for, and the
    # whole-snapshot fact that produced "invalid" is not itself a
    # per-candidate rejection (matching every other whole-snapshot site
    # in this file), so rejected_count stays 0 despite a real, live,
    # untracked descendant.
    assert tree["rejected_count"] == 0
    assert intermediate_pid not in [e["pid"] for e in tree["entries"]]
    assert live_child_pid not in [e["pid"] for e in tree["entries"]]


def test_owned_process_tree_rejects_prior_entry_with_malformed_parent_pid() -> None:
    """Task #150 round 5 connector finding 1: _snap_all_edges correctly
    cannot build an edge for a row with a malformed parent_pid - but
    "cannot place" must not collapse to "not ours". A PRIOR entry (an
    owned intermediate the walk once admitted) whose CURRENT row has a
    malformed parent_pid contributes no edge either way, and since a
    discovery ROOT is deliberately never itself checked against `owned`
    (only its descendants are - a root simply exiting is normal), a
    childless malformed-parent root would sit at rejected_count 0
    forever if nothing else names it. This test isolates exactly that
    shape (no grandchild at all, so the existing discovery-closure
    reconciliation for a live descendant plays no part) under an
    untrusted-but-readable prior, where the trusted-rehydration block's
    own prior_parent_drift check does not run either."""
    intermediate_pid = 500
    first_snapshot = [
        *_wrap_snap(),
        _proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
    ]
    first = _owned_tree_plan(first_snapshot, request_id="rr-150-malformed-parent-first")
    complete_tree = first["next_state"]["owned_process_tree"]
    assert complete_tree["status"] == "complete"
    assert intermediate_pid in [e["pid"] for e in complete_tree["entries"]]

    untrusted_state = dict(first["next_state"])
    untrusted_tree = dict(complete_tree)
    untrusted_tree["status"] = "invalid"
    untrusted_tree["reason_code"] = "process_tree_invalid_generation_adoption_pending"
    untrusted_state["owned_process_tree"] = untrusted_tree

    # Second poll: same pid, same start/filetime (still "same" identity,
    # not a recycled/different one) - only parent_pid is now malformed,
    # exactly the shape a transient WMI/CIM hiccup on one row produces.
    malformed_row = {
        **_proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
        "parent_pid": None,
    }
    second_snapshot = [*_wrap_snap(), malformed_row]
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-150-malformed-parent-second"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": untrusted_state}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )
    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["rejected_count"] >= 1
    assert intermediate_pid not in [e["pid"] for e in tree["entries"]]


def test_owned_process_tree_rejects_prior_entry_excluded_by_snap_index() -> None:
    """Task #150 round 6 connector finding: "unindexable" and "invalid
    parent" are different failure modes, and round 5's fix only covered
    the second. When a PREVIOUSLY OWNED pid's row is DUPLICATED this
    poll, _snap_index correctly excludes it entirely (ambiguous, not
    "not found") - idx.get(that_pid) then returns None, same as a
    genuinely exited descendant would. Round 5's check only inspects
    parent_pid on a row it can actually retrieve, so it silently
    `continue`s past an excluded pid exactly like it would past one that
    simply exited - even though this one is present, not gone. Bounded
    strictly to prior_entries (via the pid, not by trusting the excluded
    row's own content, which does not exist), this cannot poison an
    unrelated agent's count: it only ever inspects a pid already
    recorded as one of ours."""
    intermediate_pid = 500
    first_snapshot = [
        *_wrap_snap(),
        _proc(intermediate_pid, WRAP_CHILD_PID, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000)),
    ]
    first = _owned_tree_plan(first_snapshot, request_id="rr-150-excluded-prior-first")
    complete_tree = first["next_state"]["owned_process_tree"]
    assert complete_tree["status"] == "complete"
    assert intermediate_pid in [e["pid"] for e in complete_tree["entries"]]

    untrusted_state = dict(first["next_state"])
    untrusted_tree = dict(complete_tree)
    untrusted_tree["status"] = "invalid"
    untrusted_tree["reason_code"] = "process_tree_invalid_generation_adoption_pending"
    untrusted_state["owned_process_tree"] = untrusted_tree

    # Second poll: the SAME pid appears twice, both rows now claiming an
    # UNRELATED parent (matching the round-5 isolation technique) rather
    # than the launcher - so this pid contributes no edge to EITHER idx
    # (excluded, ambiguous) OR the permissive discovery_edges map (its
    # declared parent no longer connects it to the wrapper at all), and
    # round 4's own discovery-closure fix - which finds a duplicated pid
    # via a STILL-VALID edge from a root - plays no part. The only path
    # left is prior_entries seeding it as a root directly.
    duplicated_row = _proc(intermediate_pid, 1, "pwsh.exe", "pwsh tool.ps1", _ps_iso(700000))
    duplicate_of_same_pid = {**duplicated_row, "start_time": _ps_iso(700001)}
    second_snapshot = [*_wrap_snap(), duplicated_row, duplicate_of_same_pid]
    second = _plan_wrap(
        _report(
            restart_request=_auth_marker("rr-150-excluded-prior-second"),
            wrapper_runtime=_wrapper_runtime_view(
                phase="active",
                launcher_pid=WRAP_CHILD_PID,
                launcher_start=WRAP_CHILD_START,
                now=NOW + 1,
            ),
        ),
        {"agents": {"worker": untrusted_state}},
        now=NOW + 1,
        snapshot=second_snapshot,
    )
    tree = second["next_state"]["owned_process_tree"]
    assert tree["status"] == "invalid"
    assert tree["rejected_count"] >= 1
    assert intermediate_pid not in [e["pid"] for e in tree["entries"]]


def test_rejected_candidate_refusal_does_not_expose_internal_reset_advice() -> None:
    launcher_no_filetime = {
        **_proc(WRAP_CHILD_PID, WRAP_LAUNCHER_PID, "codex.exe", "codex exec --json", WRAP_CHILD_START),
        "start_filetime": None,
    }
    snapshot = [_wrap_snap()[0], launcher_no_filetime]
    plan = _owned_tree_plan(snapshot, request_id="rr-150-reset-remedy-rejected")
    assert plan["state"] == "PROCESS_TREE_INVALID"
    assert plan["next_state"]["owned_process_tree"]["rejected_count"] >= 1
    assert "rejected_count" not in plan["reason"]
    assert "--reset-process-tree-ownership" not in plan["reason"]
    assert "exact process lifetime identities" in plan["reason"]


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
              f"python -m agenttalk --root {TEST_ROOT} wrap --for other --loop -- codex",
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
    block_start = ps.index("{ $_ -in 'relaunch','stuck_recover' }")
    block_end = ps.index("# SEED the agent", block_start)
    block = ps[block_start:block_end]
    assert block.index(
        "Set-AgentState $state $name $p.barrier_state"
    ) < block.index(
        "Save-StateForPoll $state"
    ) < block.index(
        "Stop-Tree $p.kill_targets"
    ) < block.index("--launch-barrier")
    assert "Get-ProcSnapshot $barrierPath" in block
    assert "Write-Warning" in block
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
    if "supervisor.json refresh failed; holding this poll" not in before_state:
        pytest.fail("config refresh failure does not name the held poll")
    wait = "Wait-ForNextPoll $cfg"
    hold = "continue supervisorPoll"
    if wait not in before_state or hold not in before_state:
        pytest.fail("config refresh failure can reach planning or effects")
    if before_state.index(wait) > before_state.index(hold):
        pytest.fail("config refresh failure must wait before retrying")


def test_ps_template_binds_python_consumers_to_accepted_config_snapshot() -> None:
    ps = sup.PS_TEMPLATE
    assert "[IO.File]::ReadAllBytes($ConfigPath)" in ps
    assert "$cfgSha256 = $script:SupervisorConfigSha256" in ps
    assert "$nextCfgSha256 = $script:SupervisorConfigSha256" in ps
    assert ps.count("--supervisor-config-sha256 $cfgSha256") == 4
    reserve = (
        "$p.next_state | Add-Member -NotePropertyName pending_launch_record"
    )
    assert reserve in ps
    reserve_index = ps.index(reserve)
    preflight_index = ps.rindex("Preflight $name", 0, reserve_index)
    launch_index = ps.index("$res = Launch $name", reserve_index)
    assert preflight_index < reserve_index < launch_index
    record_start = ps.index("$recordArgs = @('--root', $Root, 'supervise', '--record-launch'")
    record_end = ps.index("Invoke-CheckedSupervisorMutation", record_start)
    record = ps[record_start:record_end]
    assert "record-launch-context" not in record
    assert "supervisor-config-sha256" not in record
    no_pid = ps[record_end:ps.index("'clear_marker'", record_end)]
    remove_context = (
        "$p.next_state.PSObject.Properties.Remove('pending_launch_record')"
    )
    assert remove_context in no_pid
    assert no_pid.index(remove_context) < no_pid.index("Save-StateForPoll $state")
    plan = ps[ps.index("$planText ="):ps.index("$pollNum++")]
    assert "$LASTEXITCODE -ne 0" in plan
    assert "continue supervisorPoll" in plan


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


def test_preflight_wrapped_codex_validates_python_not_codex_sandbox(
    tmp_path: Path,
) -> None:
    """Wrapped preflight consumes the admitted child and wrapper prefix."""
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    wlog, clog = tmp_path / "wrap.log", tmp_path / "codex.log"
    wstub, cstub = tmp_path / "python.cmd", tmp_path / "codexcli.cmd"
    native_codex = tmp_path / "codex.exe"
    _stub_cmd(wstub, wlog)
    _stub_cmd(cstub, clog)
    native_codex.write_text("", encoding="utf-8")
    plan = {
        "agents": {
            "wrapped-codex": {
                "action": sup.RELAUNCH,
                "launch_mode": "wrap",
                "session_args": [],
            },
        },
    }
    sup.attach_regular_launch_admissions(
        plan,
        {"agents": {"wrapped-codex": {
            "cli": "codex",
            "wrapped": True,
            "cwd": str(tmp_path),
            "launch": {
                "windows_file": str(wstub),
                # Omitted module_args_from legitimately means index zero.
                "windows_args": [
                    "-m", "agenttalk", "wrap", "--for", "wrapped-codex",
                    "--loop", "--", str(native_codex),
                ],
            },
        }}},
        root=tmp_path,
    )
    admission = plan["agents"]["wrapped-codex"]["launch_admission"]
    assert admission["status"] == "accepted"
    assert admission["module_args_from"] == 0
    out = tmp_path / "pf.json"
    harness = "\n".join([
        helpers,
        f"$Root = {_pslit(str(tmp_path))}",
        "$SrcOnPyPath = $false",
        f"$AgenttalkPython = {_pslit(str(wstub))}",
        f"$admission = ({_pslit(json.dumps(admission))} | ConvertFrom-Json)",
        "$prefixTokens = Get-LaunchAdmissionPrefixTokens $admission",
        f"$wrapOk = Preflight 'wrapped-codex' "
        "([pscustomobject]@{ cli='codex'; launch_mode='wrap' }) "
        f"{_pslit(str(wstub))} $null $prefixTokens $admission",
        f"$codexOk = Preflight 'plain-codex' "
        "([pscustomobject]@{ cli='codex'; launch_mode='resume' }) "
        f"{_pslit(str(cstub))} $null",
        "@{ wrapOk=$wrapOk; codexOk=$codexOk } | ConvertTo-Json | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    script = tmp_path / "pf_harness.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    wrap_args = wlog.read_text(encoding="utf-8") if wlog.exists() else ""
    codex_args = clog.read_text(encoding="utf-8") if clog.exists() else ""
    assert data["wrapOk"] is True
    assert "-m agenttalk --version" in wrap_args
    assert "sandbox" not in wrap_args
    assert "sandbox" not in codex_args


@pytest.mark.parametrize("shell", _windows_powershell_hosts())
def test_preflight_wrapped_console_entry_uses_direct_version_probe(
    tmp_path: Path,
    shell: str | None,
) -> None:
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    entry_log = tmp_path / "agenttalk-entry.log"
    entry = tmp_path / "agenttalk.cmd"
    _stub_cmd(entry, entry_log)
    pinned = tmp_path / "python.cmd"
    _stub_cmd(pinned, tmp_path / "pinned-python.log")
    child = tmp_path / "claude.exe"
    child.write_text("", encoding="utf-8")
    admission = _accepted_launch_artifact(
        population="regular", agent="wrapped", cli="claude",
        windows_file=str(entry),
        argv=[
            "wrap", "--for", "wrapped", "--cli", "claude", "--loop",
            "--", str(child),
        ],
        cwd=str(tmp_path), wrapped=True, module_args_from=None,
        nonce_insert_at=0, child_argv=[str(child)],
    )
    out = tmp_path / "pf-console.json"
    harness = "\n".join([
        helpers,
        f"$Root = {_pslit(str(tmp_path))}",
        "$SrcOnPyPath = $false",
        f"$AgenttalkPython = {_pslit(str(pinned))}",
        f"$admission = ({_pslit(json.dumps(admission))} | ConvertFrom-Json)",
        "$prefixTokens = Get-LaunchAdmissionPrefixTokens $admission",
        f"$ok = Preflight 'wrapped' "
        "([pscustomobject]@{ cli='claude'; launch_mode='wrap' }) "
        f"{_pslit(str(entry))} $null $prefixTokens $admission",
        "[pscustomobject]@{ ok=$ok; prefixTokens=@($prefixTokens) } | "
        "ConvertTo-Json -Depth 3 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    script = tmp_path / "pf-console.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    args = entry_log.read_text(encoding="utf-8")
    assert data == {"ok": True, "prefixTokens": []}
    assert "--version" in args
    assert "-m agenttalk" not in args


def test_preflight_wrapped_smoke_test_uses_admitted_prefix(
    tmp_path: Path,
) -> None:
    shell = _pick_powershell()
    if not shell:
        return
    helpers = _exec_helpers(tmp_path)
    wlog = tmp_path / "wrap.log"
    wstub = tmp_path / "pywrap.cmd"
    _stub_cmd(wstub, wlog)
    native_cli = tmp_path / "claude.exe"
    native_cli.write_text("", encoding="utf-8")
    admission = _accepted_launch_artifact(
        population="regular", agent="wrapped", cli="claude",
        windows_file=str(wstub),
        argv=[
            "-E", "-m", "agenttalk", "wrap", "--for", "wrapped", "--cli",
            "claude", "--loop", "--", str(native_cli),
        ],
        cwd=str(tmp_path), wrapped=True, module_args_from=1,
        nonce_insert_at=3, child_argv=[str(native_cli)],
    )
    out = tmp_path / "pf_prefix.json"
    harness = "\n".join([
        helpers,
        f"$Root = {_pslit(str(tmp_path))}",
        "$SrcOnPyPath = $false",
        f"$AgenttalkPython = {_pslit(str(wstub))}",
        f"$admission = ({_pslit(json.dumps(admission))} | ConvertFrom-Json)",
        "$prefixTokens = Get-LaunchAdmissionPrefixTokens $admission",
        f"$wrapOk = Preflight 'wrapped' "
        "([pscustomobject]@{ cli='claude'; launch_mode='wrap' }) "
        f"{_pslit(str(wstub))} $null $prefixTokens $admission",
        "[pscustomobject]@{ wrapOk=$wrapOk; prefixTokens=@($prefixTokens) } | "
        "ConvertTo-Json -Depth 3 | "
        f"Set-Content {_pslit(str(out))} -Encoding utf8",
    ])
    script = tmp_path / "pf_prefix_harness.ps1"
    script.write_text(harness, encoding="utf-8-sig")

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    wrap_args = wlog.read_text(encoding="utf-8") if wlog.exists() else ""
    assert data["wrapOk"] is True
    assert data["prefixTokens"] == ["-E"]
    assert "-E -m agenttalk --version" in wrap_args


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
    statuses = out[str(settings)]
    assert all("unreadable" not in status for status in statuses.values())
    assert set(statuses.values()) <= {"installed", "already"}
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
    statuses = out[str(hooks)]
    assert all("unreadable" not in status for status in statuses.values())
    data = json.loads(hooks.read_text(encoding="utf-8-sig"))
    assert data.get("customOperatorKey") == 7
