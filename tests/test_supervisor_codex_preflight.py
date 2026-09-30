"""Codex relaunch ACL admission (#147)."""
import os
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from agenttalk import cli, supervisor as sup
from agenttalk import codex_preflight as cp
from agenttalk.store import Store


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


def _directory_link(target, link):
    try:
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'filesystem cannot create directory links: {exc}')


def test_acl_reset_skips_real_codex_home_junction(tmp_path):
    directory = tmp_path / 'store'
    home = directory / 'codex-home' / 'myagent'
    home.mkdir(parents=True)
    guard = home / '.beside.generation'
    guard.touch()
    target = tmp_path / 'shared-skills'
    target.mkdir()
    (target / '.outside.generation').touch()
    _directory_link(target, home / 'skills')
    reset = Mock(return_value=subprocess.CompletedProcess([], 0))
    cp.reset_guard_acls(directory, windows=True, run=reset)
    assert reset.call_count == 1
    assert reset.call_args.args[0][-3:] == [str(guard), '/reset', '/L']


@pytest.mark.parametrize('top_level', [False, True])
def test_linked_generation_or_store_root_is_rejected(tmp_path, top_level):
    directory = tmp_path / 'store'
    directory.mkdir()
    target = tmp_path / 'outside'
    target.mkdir()
    link = directory / '.linked.generation'
    _directory_link(target, link)
    reset = Mock(return_value=subprocess.CompletedProcess([], 0))
    with pytest.raises(cp.PreflightError, match='linked store'):
        cp.reset_guard_acls(link if top_level else directory, windows=True, run=reset)
    reset.assert_not_called()


def test_acl_reset_skips_other_linked_files(tmp_path):
    directory = tmp_path / 'store'
    directory.mkdir()
    target = tmp_path / 'outside.txt'
    target.touch()
    try:
        (directory / 'linked.txt').symlink_to(target)
    except OSError as exc:
        pytest.skip(f'filesystem cannot create file symlinks: {exc}')
    reset = Mock(return_value=subprocess.CompletedProcess([], 0))
    cp.reset_guard_acls(directory, windows=True, run=reset)
    reset.assert_not_called()


def test_hard_link_guard_is_rejected_before_acl_reset(tmp_path):
    target = tmp_path / 'outside.txt'
    target.write_text('must not be reset')
    directory = tmp_path / 'store'
    directory.mkdir()
    guard = directory / '.retirement.generation'
    try:
        os.link(target, guard)
    except OSError as exc:
        pytest.skip(f'filesystem cannot create hard links: {exc}')
    assert guard.stat().st_nlink == 2
    reset = Mock(return_value=subprocess.CompletedProcess([], 0))
    with pytest.raises(cp.PreflightError, match='hardlink count'):
        cp.reset_guard_acls(directory, windows=True, run=reset)
    reset.assert_not_called()
    assert target.read_text() == 'must not be reset'
    assert guard.stat().st_ino == target.stat().st_ino


@pytest.mark.parametrize('liaison', [None, 'worker'])
def test_lead_acl_failure_is_visible_to_human_operator(tmp_path, liaison):
    from agenttalk.web import _web_needs_operator, _lead_chat_pending_decisions
    store = _store(tmp_path)
    if liaison:
        store.set_operator_facing(liaison)
    operator, lead = store.lead_chat_identities()
    cp.notify_failure(store, lead, 'CODEX_ACL_PREFLIGHT_FAILED')
    message, = store.all_messages()
    assert (message.sender, message.recipient) == (lead, operator)
    assert [r['request_id'] for r in _web_needs_operator(store, operator)] == [message.meta['request_id']]
    decisions = _lead_chat_pending_decisions(store, operator, lead)
    assert [r['request_id'] for r in decisions] == [message.meta['request_id']]


def test_lead_acl_failure_without_operator_identity_is_not_marked_delivered(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, 'operator_identity_raw', lambda: None)
    with pytest.raises(cp.PreflightError, match='operator_identity'):
        cp.notify_failure(store, 'lead', 'CODEX_ACL_PREFLIGHT_FAILED')
    assert list(store.all_messages()) == []


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
function Save-StateForPoll($state) {
  $state | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $PSScriptRoot 'hold.json')
  $script:events += 'saved'; return $true
}
function Invoke-CheckedSupervisorMutation($label, $argv) {
  if ($argv -notcontains '--codex-launch-failure') { throw 'wrong command' }
  $script:events += 'escalated'; return $true
}
$state = @{}
$plan = [pscustomobject]@{ next_state = [pscustomobject]@{} }
Set-CodexAccessHold 'worker' $plan $state 'CODEX_ACL_PREFLIGHT_FAILED'
if (($script:events -join ',') -ne 'saved,escalated,saved') { throw 'not same-poll escalation' }
$state = Get-Content -Raw (Join-Path $PSScriptRoot 'hold.json') | ConvertFrom-Json
if (-not $state.worker.codex_access_hold.notified) { throw 'not latched' }
'''
    if not delivered:
        test = test.replace("'saved,escalated,saved'", "'saved,escalated'")
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
            'CODEX_ACL_PREFLIGHT_FAILED', '--for', 'worker']
    assert cli.main(args) == 0
    messages = list(store.all_messages())
    assert len(messages) == 1
    assert messages[0].kind == 'question'
    assert messages[0].recipient == 'lead'
    assert messages[0].subject == 'CODEX_ACL_PREFLIGHT_FAILED'
    (store.dir / 'supervisor.kill').touch()
    assert cli.main(args) == 3
    assert len(list(store.all_messages())) == 1
