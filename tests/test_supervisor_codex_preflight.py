"""Codex relaunch admission and restricted-process write proof (#147)."""
import os
import json
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from agenttalk import cli, supervisor as sup
from agenttalk import codex_preflight as cp
from agenttalk.store import Store
from agenttalk.wrapper import run, loop, session


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


def _denial_stream(store):
    return [json.dumps(event) + "\n" for event in [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": "python -m agenttalk reply --to-request tk-test -m done",
            "exit_code": 1, "aggregated_output":
            f"PermissionError: [Errno 13] Permission denied: '{store.dir / '.retirement.generation'}'"}},
        {"type": "turn.completed", "usage": {}},
    ]]


def test_real_turn_denial_parks_unread_even_after_wrapper_restart(tmp_path):
    store = _store(tmp_path)
    message = store.send(sender="lead", recipient="worker", kind="question", body="work")
    spawn = Mock(side_effect=lambda *args: _denial_stream(store))
    state = session.SessionState(cli="codex", codex_thread_id="existing-thread")
    drive = run.make_drive(store, "worker", "codex", state, ["codex"], spawn=spawn,
                           render=False, clock=lambda: 0.0)
    for _ in range(2):
        loop.run_loop(store, "worker", drive, max_polls=3,
                      clock=lambda: 0.0, sleep=lambda _: None, k_poison=1, k_escalate=1)
    assert spawn.call_count == 1
    assert store.cursor("worker") == ""
    assert store.attempt_record("worker", message.id)["last_failure_class"] == "codex_bus_permission_denied"
    assert cp.has_permission_hold(store, "worker")
    assert len(list(store.all_messages())) == 1  # no probe or extra wrapper escalation


@pytest.mark.parametrize("command,output,rc", [
    ("python -m agenttalk reply --to-request x -m x", "Permission denied: /other/.agenttalk/guard", 1),
    ("python -m agenttalk check --gates", "Permission denied: {path}", 1),
    ("python -m agenttalk reply --to-request x -m x", "Permission denied: {path}", 0),
    ("cat log.txt", "Permission denied: {path}", 1),
    ("python -m agenttalk reply --to-request x -m x", "permission denied elsewhere\n{path}", 1),
])
def test_unrelated_errors_are_not_bus_acl_failures(tmp_path, command, output, rc):
    store = _store(tmp_path)
    result = run.classify_bus_execution(command, output.format(path=store.dir / 'guard'), rc,
                                        store_dir=store.dir)
    assert result["kind"] != "codex_bus_permission_denied"


def test_supervisor_bus_acl_hold_never_relaunches_and_notifies_once(tmp_path):
    store = _store(tmp_path)
    cp.write_permission_hold(store, "worker")
    config = {"agents": {"worker": {"cli": "codex", "wrapped": True, "auto_restart": True}}}
    state = {}
    for index in range(3):
        report = sup.build_report(store, now_epoch=100 + index, state=state, supervisor_config=config)
        action = sup.plan_actions(report, state, config, now_epoch=100 + index, snapshot=[])["agents"]["worker"]
        assert action["state"] == "CODEX_BUS_PERMISSION_DENIED"
        assert action["action"] == (sup.WARN_ONLY if index == 0 else sup.NONE)
        assert action["notify"] is (index == 0)
        assert not action["kill_targets"]
        state = {"agents": {"worker": action["next_state"]}}


@pytest.mark.parametrize('delivered', [True, False])
def test_same_poll_acl_hold_escalation(tmp_path, delivered):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell required")
    script = sup.PS_TEMPLATE
    start = script.index('function Set-CodexAccessHold(')
    end = script.index('\nfunction ', start + 1)
    helper = script[start:end]
    block = script[script.index('if ($seedOk -and $p.cli'):]
    assert (block.index("Set-CodexAccessHold $name $p $state 'CODEX_ACL_PREFLIGHT_FAILED'")
            < block.index('if (-not $seedOk)'))
    test = helper + '''
$Root = 'test-root'
$script:events = @()
function Set-AgentState($state, $name, $next) { $state[$name] = $next }
function Save-StateForPoll($state) { $script:events += 'saved'; return $true }
function Invoke-CheckedSupervisorMutation($label, $argv) {
  if ($argv -notcontains '--codex-launch-failure') { throw 'wrong command' }
  $script:events += 'escalated'; return $true
}
$state = @{}
$plan = [pscustomobject]@{ next_state = [pscustomobject]@{} }
Set-CodexAccessHold 'worker' $plan $state 'CODEX_ACL_PREFLIGHT_FAILED'
if (($script:events -join ',') -ne 'saved,escalated') { throw 'not same-poll escalation' }
if (-not $state.worker.codex_access_hold.notified) { throw 'not latched' }
'''
    if not delivered:
        test = test.replace("$script:events += 'escalated'; return $true",
                            "$script:events += 'escalated'; return $false")
        test = test.replace('if (-not $state.worker.codex_access_hold.notified)',
                            'if ($state.worker.codex_access_hold.notified)')
    path = tmp_path / 'same-poll.ps1'
    path.write_text(test, encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-File', str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failure_cli_escalates_without_optional_config_and_obeys_kill_switch(tmp_path):
    store = _store(tmp_path)
    args = ['--root', str(tmp_path), 'supervise', '--codex-launch-failure',
            'CODEX_BUS_PERMISSION_DENIED', '--for', 'worker']
    assert cli.main(args) == 0
    messages = list(store.all_messages())
    assert len(messages) == 1
    assert messages[0].kind == 'question'
    assert messages[0].recipient == 'lead'
    assert messages[0].subject == 'CODEX_BUS_PERMISSION_DENIED'
    (store.dir / 'supervisor.kill').touch()
    assert cli.main(args) == 3
    assert len(list(store.all_messages())) == 1


@pytest.mark.parametrize('error', ['[Errno 13] Permission denied', 'EACCES', '[WinError 5] Access is denied'])
def test_exact_store_denial_signature(tmp_path, error):
    store = _store(tmp_path)
    command = 'python -m agenttalk reply --to-request x -m x'
    output = f"{error}: '{store.dir / '.retirement.generation'}'"
    assert run.classify_bus_execution(command, output, 1, store_dir=store.dir)['kind'] == cp.BUS_PERMISSION_DENIED
    assert run.classify_bus_execution(command, output, 1)['kind'] != cp.BUS_PERMISSION_DENIED


def test_scoped_real_turn_denial_stays_unread(tmp_path):
    store = _store(tmp_path)
    store.send(sender='lead', recipient='worker', kind='task', body='work',
               meta={'request_id': 'tk-acl-test'})
    spawn = Mock(side_effect=lambda *args: _denial_stream(store))
    drive = run.make_drive(store, 'worker', 'codex', session.SessionState(cli='codex'), ['codex'],
                           spawn=spawn, render=False, clock=lambda: 0.0)
    for _ in range(2):
        assert loop.run_loop(store, 'worker', drive, only_request_id='tk-acl-test', max_polls=3,
                              clock=lambda: 0.0, sleep=lambda _: None) == 0
    assert spawn.call_count == 1
    assert store.cursor('worker') == ''
    assert cp.has_permission_hold(store, 'worker')
