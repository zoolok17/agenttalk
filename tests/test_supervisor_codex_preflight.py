"""Codex relaunch admission and restricted-process write proof (#147)."""
import os
import shutil
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from agenttalk import cli, supervisor as sup
from agenttalk import codex_preflight as cp
from agenttalk.store import Store


NONCE = "a" * 32


def test_acl_reset_is_bounded_and_preserves_guard_identity(tmp_path):
    guard = tmp_path / ".retirement.generation"
    guard.write_text("held guard")
    identity = guard.stat().st_ino
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    cp.reset_guard_acls(tmp_path, windows=True, run=run)
    assert run.call_args.args[0][-3:] == [str(guard), "/reset", "/L"]
    assert 0 < run.call_args.kwargs["timeout"] <= cp.ACL_TIMEOUT
    assert guard.stat().st_ino == identity
    assert guard.read_text() == "held guard"


@pytest.mark.parametrize("failure", [PermissionError("denied"), subprocess.TimeoutExpired("icacls", 5), None])
def test_unfixable_guard_is_named_failure(tmp_path, failure):
    (tmp_path / ".retirement.generation").touch()
    run = Mock(side_effect=failure, return_value=subprocess.CompletedProcess([], 5))
    with pytest.raises(cp.PreflightError, match="codex_acl_preflight_failed"):
        cp.reset_guard_acls(tmp_path, windows=True, run=run)


def test_non_windows_does_not_run_icacls(tmp_path):
    run = Mock()
    cp.reset_guard_acls(tmp_path, windows=False, run=run)
    run.assert_not_called()


def _store(tmp_path):
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.set_role("lead", "lead")
    return store


def test_receipt_requires_restricted_process_then_real_publication(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(cp, "is_restricted_process", lambda: False)
    with pytest.raises(cp.PreflightError, match="restricted"):
        cp.write_bus_proof(store, "worker", NONCE)
    assert cp.read_bus_proof(store, "worker") is None
    monkeypatch.setattr(cp, "is_restricted_process", lambda: True)
    cp.write_bus_proof(store, "worker", NONCE)
    receipt = cp.read_bus_proof(store, "worker")
    assert receipt["nonce"] == NONCE
    assert receipt["message_id"]
    assert receipt["agent"] == "worker"


def test_failed_publication_never_leaves_receipt(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(cp, "is_restricted_process", lambda: True)
    monkeypatch.setattr(store, "send", Mock(side_effect=PermissionError("generation guard")))
    with pytest.raises(PermissionError):
        cp.write_bus_proof(store, "worker", NONCE)
    assert cp.read_bus_proof(store, "worker") is None


def _plan(check, *, now=100, receipt=None):
    state = {"agents": {"worker": {"codex_launch_check": check}}}
    config = {"agents": {"worker": {"cli": "codex", "auto_restart": True}}}
    report = {"agents": {"worker": {"heartbeat_stale": False, "codex_bus_proof": receipt}}}
    return sup.plan_actions(report, state, config, now_epoch=now)["agents"]["worker"]


def test_missing_proof_is_sticky_not_healthy_or_relaunched():
    check = {"nonce": NONCE, "deadline_epoch": 120, "status": "pending"}
    pending = _plan(check)
    assert pending["state"] == "CODEX_BUS_PROOF_PENDING"
    expired = _plan(check, now=121)
    assert expired["state"] == "CODEX_FIRST_BUS_WRITE_MISSING"
    assert expired["notify"]
    assert not expired["kill_targets"]
    for now in (122, 500, 10000):
        again = _plan(expired["next_state"]["codex_launch_check"], now=now)
        assert again["state"] == "CODEX_FIRST_BUS_WRITE_MISSING"
        assert again["action"] not in (sup.RELAUNCH, sup.STUCK_RECOVER)


def test_only_this_launch_receipt_satisfies_pending_check():
    check = {"nonce": NONCE, "deadline_epoch": 120, "status": "pending"}
    old = _plan(check, receipt={"nonce": "b" * 32, "message_id": "old"})
    assert old["state"] == "CODEX_BUS_PROOF_PENDING"
    good = _plan(check, receipt={"nonce": NONCE, "message_id": "new", "agent": "worker", "ready_epoch": 99})
    assert good["next_state"]["codex_launch_check"]["status"] == "proved"


@pytest.mark.parametrize("ready", [None, 121])
def test_partial_or_late_probe_does_not_unlock_restart_loop(ready):
    check = {"nonce": NONCE, "deadline_epoch": 120, "status": "pending"}
    result = _plan(check, now=121, receipt={"nonce": NONCE, "message_id": "new",
                                          "agent": "worker", "ready_epoch": ready})
    assert result["state"] == "CODEX_FIRST_BUS_WRITE_MISSING"


@pytest.mark.parametrize("check", [None, {"status": "bogus"}, {"status": "pending", "deadline_epoch": "bad"},
                                   {"status": "pending", "nonce": NONCE, "deadline_epoch": float("inf")}])
def test_malformed_launch_check_holds_instead_of_skipping_or_crashing(check):
    result = _plan(check)
    assert result["state"] == "CODEX_FIRST_BUS_WRITE_MISSING"


def test_probe_completion_cannot_mint_host_proof(tmp_path, monkeypatch):
    store = _store(tmp_path)
    with pytest.raises(cp.PreflightError, match="no matching"):
        cp.complete_probe(store, "worker", NONCE)
    monkeypatch.setattr(cp, "is_restricted_process", lambda: True)
    cp.write_bus_proof(store, "worker", NONCE)
    cp.complete_probe(store, "worker", NONCE)
    assert cp.read_bus_proof(store, "worker")["ready_epoch"] > 0


def test_old_receipt_cannot_clear_failed_launch():
    result = _plan({"nonce": NONCE, "status": "failed", "failure": "CODEX_FIRST_BUS_WRITE_MISSING"},
                   receipt={"nonce": NONCE, "message_id": "late", "agent": "worker"})
    assert result["state"] == "CODEX_FIRST_BUS_WRITE_MISSING"


def test_wrapper_probe_failure_stops_before_inbox_setup(tmp_path, monkeypatch):
    from agenttalk.wrapper import run, loop
    store = _store(tmp_path)
    monkeypatch.setenv(cp.PROBE_ENV, NONCE)
    probe = Mock(return_value=False)
    monkeypatch.setattr(run, "run_codex_startup_probe", probe)
    consume = Mock(side_effect=AssertionError("inbox work must not start"))
    monkeypatch.setattr(loop, "run_loop", consume)
    log = Mock()
    assert cli._wrap_loop_mode(store, "worker", cli="codex", base_argv=["codex"],
                               sender="worker", min_interval=1, render=False,
                               supervisor_launch_nonce=NONCE, lifecycle_log=log) == 3
    probe.assert_called_once()
    consume.assert_not_called()
    log.wrapper_exited.assert_called_once_with(3, reason="codex_first_bus_write_missing")


def test_generated_script_reserves_probe_before_spawn_and_parses(tmp_path):
    script = sup.PS_TEMPLATE
    reserve = script.index("# Reserve the relaunch before Start-Process.")
    launch = script.index("$res = Launch $name", reserve)
    assert "codex_launch_check" in script[reserve:launch]
    assert script.index("'supervise', '--codex-preflight'") < launch
    shell = shutil.which("pwsh")
    if shell:
        path = tmp_path / "supervisor.ps1"
        path.write_text(script, encoding="utf-8")
        literal = str(path).replace("'", "''")
        command = ("$e=$null; $t=$null; "
                   f"$null=[System.Management.Automation.Language.Parser]::ParseFile('{literal}',[ref]$t,[ref]$e); "
                   "if($e.Count){$e | Out-String | Write-Output; exit 1}")
        result = subprocess.run([shell, "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted-token proof")
def test_actual_restricted_process_publishes_to_isolated_bus(tmp_path):
    if not cp.is_restricted_process():
        pytest.skip("CI host is unrestricted; exercised in the Codex sandbox")
    store = _store(tmp_path)
    cp.write_bus_proof(store, "worker", NONCE)
    proof = cp.read_bus_proof(store, "worker")
    assert proof["message_id"] == list(store.all_messages())[-1].id


def test_launch_records_exact_nonce_and_deadline():
    state = sup.record_launch({}, "worker", cli="codex", pid=123, now_epoch=100,
                              launcher_nonce=NONCE, launcher_nonce_injected=True,
                              cfg_agent={"wrapped": True})
    check = state["agents"]["worker"]["codex_launch_check"]
    assert check == {"nonce": NONCE, "deadline_epoch": 100 + cp.PROOF_TIMEOUT, "status": "pending"}
    other = sup.record_launch({}, "other", cli="claude", pid=124, now_epoch=100)
    assert "codex_launch_check" not in other["agents"]["other"]


def test_acl_hold_prevents_launch():
    result = _plan({"status": "failed", "failure": "CODEX_ACL_PREFLIGHT_FAILED"})
    assert result["state"] == "CODEX_ACL_PREFLIGHT_FAILED"
    assert result["action"] == sup.WARN_ONLY
    assert result["notify"]


def test_launch_failure_notifies_lead_without_optional_notify_config(tmp_path):
    store = _store(tmp_path)
    cp.notify_failure(store, "worker", "CODEX_FIRST_BUS_WRITE_MISSING")
    messages = list(store.all_messages())
    assert messages[-1].kind == "question"
    assert messages[-1].recipient == "lead"
    assert messages[-1].meta["needs_operator"] == "true"


def test_pending_codex_does_not_delay_other_seat():
    state = {"agents": {"worker": {"codex_launch_check": {
        "nonce": NONCE, "deadline_epoch": 200, "status": "pending"}}}}
    config = {"agents": {"worker": {"cli": "codex", "auto_restart": True},
                         "other": {"cli": "claude", "auto_restart": True}}}
    report = {"agents": {"worker": {"heartbeat_stale": True},
                         "other": {"heartbeat_stale": True, "heartbeat_age_seconds": 9999}}}
    result = sup.plan_actions(report, state, config, now_epoch=100, snapshot=[])
    assert result["agents"]["worker"]["state"] == "CODEX_BUS_PROOF_PENDING"
    ordinary = sup.plan_actions(report, {}, config, now_epoch=100, snapshot=[])
    assert result["agents"]["other"] == ordinary["agents"]["other"]
    assert result["agents"]["other"]["state"] == "ACTIVE_OR_BUSY"


@pytest.mark.parametrize("cancel", [False, True])
def test_probe_is_bounded_cancellable_and_never_retries(tmp_path, monkeypatch, cancel):
    from agenttalk.wrapper import run, turn_watchdog
    store = _store(tmp_path)
    proc = Mock(pid=123, returncode=None)
    proc.poll.return_value = None
    factory = Mock(return_value=proc)
    tick = [0.0]

    def sleep(seconds):
        tick[0] += seconds
        if cancel:
            (store.dir / "supervisor.kill").touch()

    monkeypatch.setattr(turn_watchdog, "snapshot_processes", lambda **kw: {})
    monkeypatch.setattr(run, "_read_ready_pipe_chunk", lambda stream: None)
    assert not run.run_codex_startup_probe(store, "worker", ["codex"], NONCE,
                                          timeout=0.5, clock=lambda: tick[0], sleep=sleep, popen=factory)
    assert tick[0] <= 0.5
    assert factory.call_count == 1
    proc.terminate.assert_called_once()


def test_probe_requires_receipt_even_when_child_exits_zero(tmp_path, monkeypatch):
    from agenttalk.wrapper import run
    store = _store(tmp_path)
    proc = Mock(pid=123, returncode=0)
    proc.poll.return_value = 0
    factory = Mock(return_value=proc)
    monkeypatch.setattr(run, "_read_ready_pipe_chunk", lambda stream: b"")
    assert not run.run_codex_startup_probe(store, "worker", ["codex"], NONCE, popen=factory)
    monkeypatch.setattr(cp, "is_restricted_process", lambda: True)
    cp.write_bus_proof(store, "worker", NONCE)
    assert run.run_codex_startup_probe(store, "worker", ["codex"], NONCE, popen=factory)
    assert factory.call_args.args[0][1:3] == ["exec", "--json"]
    assert "--codex-bus-proof" in factory.call_args.args[0][-1]


def test_probe_drains_real_child_output_without_buffering_it(tmp_path, monkeypatch):
    from agenttalk.wrapper import run
    store = _store(tmp_path)
    monkeypatch.setattr(cp, "read_bus_proof", lambda *args: {"nonce": NONCE})
    assert run.run_codex_startup_probe(
        store, "worker", [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"], NONCE,
        timeout=10)


def test_probe_resumes_work_session_without_fresh_fallback(tmp_path, monkeypatch):
    from agenttalk.wrapper import run, session
    store = _store(tmp_path)
    state = session.SessionState(cli="codex", codex_thread_id="work-thread", turns=4)
    monkeypatch.setattr(cp, "read_bus_proof", lambda *args: {"nonce": NONCE})
    monkeypatch.setattr(run, "_read_ready_pipe_chunk", lambda stream: b"")
    proc = Mock(returncode=0)
    proc.poll.return_value = 0
    factory = Mock(return_value=proc)
    persist = Mock()
    assert run.run_codex_startup_probe(store, "worker", ["codex"], NONCE,
                                       session_state=state, persist=persist, popen=factory)
    assert factory.call_args.args[0][1:5] == ["exec", "resume", "--json", "work-thread"]
    assert state.turns == 5
    assert state.codex_thread_id == "work-thread"
    persist.assert_called_once_with(state)


def test_probe_timeout_reaps_real_child(tmp_path, monkeypatch):
    from agenttalk.wrapper import run, turn_watchdog
    store = _store(tmp_path)
    children = []

    def spawn(*args, **kwargs):
        proc = subprocess.Popen(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(turn_watchdog, "snapshot_processes", lambda **kwargs: {})
    started = time.monotonic()
    assert not run.run_codex_startup_probe(
        store, "worker", [sys.executable, "-c", "import time; time.sleep(60)"], NONCE,
        timeout=0.2, popen=spawn)
    assert time.monotonic() - started < 10
    assert children[0].poll() is not None
    assert children[0].stdout.closed


def test_supervisor_proof_cli_dispatch_and_kill_switch(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(cp, "is_restricted_process", lambda: True)
    args = ["--root", str(tmp_path), "supervise", "--codex-bus-proof", NONCE, "--for", "worker"]
    assert cli.main(args) == 0
    assert cp.read_bus_proof(store, "worker")["nonce"] == NONCE
    (store.dir / "supervisor.kill").touch()
    assert cli.main(args) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL inheritance")
def test_real_windows_stale_acl_reset(tmp_path):
    guard = tmp_path / ".retirement.generation"
    guard.touch()
    subprocess.run(["icacls", str(guard), "/inheritance:d"], check=True, capture_output=True, timeout=10)
    before = subprocess.check_output(["icacls", str(guard)], timeout=10).decode(errors="replace")
    assert "(I)" not in before
    with guard.open("rb"):
        cp.reset_guard_acls(tmp_path)
    after = subprocess.check_output(["icacls", str(guard)], timeout=10).decode(errors="replace")
    assert "(I)" in after
