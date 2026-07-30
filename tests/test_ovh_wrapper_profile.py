from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agenttalk import ovh_gateway
from agenttalk.store import Store
from agenttalk.wrapper import run
from agenttalk.wrapper import session
from agenttalk.wrapper.loop import (
    CLASS_AMBIGUOUS,
    CLASS_CONFIG_BLOCKED,
    CLASS_GATEWAY_HELD,
    CLASS_INFRA,
)


def test_non_profile_child_environment_is_unchanged(tmp_path, monkeypatch) -> None:
    ambient = {
        "PATH": "p",
        "UNRELATED_SECRET": "still-historical",
        "ANTHROPIC_API_KEY": "still-historical",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "still-historical",
        "AGENTTALK_LEAD_LOOP_LEASE": "removed-by-existing-contract",
        "AGENTTALK_WRAPPER_GENERATION": "removed-by-existing-contract",
        "AGENTTALK_INBOUND_REQUEST_ID": "removed-by-existing-contract",
        "AGENTTALK_WRAPPER_STDOUT_LOG": "controller-only",
        "AGENTTALK_WRAPPER_STDERR_LOG": "controller-only",
        "AGENTTALK_WRAPPER_LOG_MAX_BYTES": "controller-only",
        "AGENTTALK_WRAPPER_LOG_SEGMENTS": "controller-only",
        "AGENTTALK_WRAPPER_LOG_NONCE": "controller-only",
    }
    monkeypatch.setattr(os, "environ", ambient.copy())
    result = run._child_env(tmp_path)
    assert result == {
        "PATH": "p",
        "UNRELATED_SECRET": "still-historical",
        "ANTHROPIC_API_KEY": "still-historical",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "still-historical",
        "AGENTTALK_PY": str(run.Path(run.sys.executable).resolve()),
        "AGENTTALK_ROOT": str(tmp_path.resolve()),
    }


def test_ovh_qwen_child_environment_starts_from_allowlist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": "safe-path",
            "SystemRoot": "C:\\Windows",
            "TEMP": "C:\\Temp",
            "AGENTTALK_SELF": "qwen-dev-1",
            "AGENTTALK_NO_CHILD_WINDOW": "1",
            "AGENTTALK_LEAD_LOOP_LEASE": "must-not-pass",
            "AGENTTALK_WRAPPER_GENERATION": "stale-must-not-pass",
            "AGENTTALK_INBOUND_REQUEST_ID": "stale-must-not-pass",
            "AGENTTALK_WRAPPER_STDOUT_LOG": "must-not-pass",
            "AGENTTALK_WRAPPER_STDERR_LOG": "must-not-pass",
            "AGENTTALK_WRAPPER_LOG_MAX_BYTES": "must-not-pass",
            "AGENTTALK_WRAPPER_LOG_SEGMENTS": "must-not-pass",
            "AGENTTALK_WRAPPER_LOG_NONCE": "must-not-pass",
            "ANTHROPIC_API_KEY": "must-not-pass",
            "ANTHROPIC_AUTH_TOKEN": "ambient-must-not-pass",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
            "CLAUDE_CONFIG_DIR": "C:\\Users\\operator\\.claude",
            "HOME": "C:\\Users\\operator",
            "USERPROFILE": "C:\\Users\\operator",
            "OVH_KEY": "must-not-pass",
            "UNRELATED_SECRET": "must-not-pass",
        },
    )
    env = run._child_env(
        tmp_path,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "front-token",
        },
        wrapper_generation="generation",
        inbound_request_id="request",
    )
    assert env["PATH"] == "safe-path"
    assert env["SystemRoot"] == "C:\\Windows"
    assert env["AGENTTALK_SELF"] == "qwen-dev-1"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "front-token" not in env.values()
    assert env["ANTHROPIC_MODEL"] == "Qwen3.5-397B-A17B"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    assert env["MAX_THINKING_TOKENS"] == "0"
    assert env["CLAUDE_CONFIG_DIR"] == str(
        (tmp_path / ".agenttalk" / "gateway" / "claude-profile").resolve()
    )
    assert env["AGENTTALK_WRAPPER_GENERATION"] == "generation"
    assert env["AGENTTALK_INBOUND_REQUEST_ID"] == "request"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OVH_KEY" not in env
    assert "UNRELATED_SECRET" not in env
    # Home is scoped to the disposable workspace clone (NOT the operator's real home):
    # the child needs a resolvable Path.home()/LOCALAPPDATA so `agenttalk reply` does
    # not crash in signing.default_keys_dir(), but must never see the operator's home.
    assert env["HOME"] == str(tmp_path.resolve())
    assert env["USERPROFILE"] == str(tmp_path.resolve())
    assert env["LOCALAPPDATA"] == str((tmp_path / "AppData" / "Local").resolve())
    assert "C:\\Users\\operator" not in env.values()
    assert "C:\\Users\\operator\\.claude" not in env.values()
    assert "AGENTTALK_LEAD_LOOP_LEASE" not in env
    assert "ambient-must-not-pass" not in env.values()
    assert "stale-must-not-pass" not in env.values()
    assert "AGENTTALK_WRAPPER_STDOUT_LOG" not in env
    assert "AGENTTALK_WRAPPER_STDERR_LOG" not in env
    assert "AGENTTALK_WRAPPER_LOG_MAX_BYTES" not in env
    assert "AGENTTALK_WRAPPER_LOG_SEGMENTS" not in env
    assert "AGENTTALK_WRAPPER_LOG_NONCE" not in env


@pytest.mark.parametrize(
    "operator_home_env",
    [
        pytest.param(
            {"HOME": "/home/operator", "XDG_CONFIG_HOME": "/home/operator/.config"},
            id="posix-style-input",
        ),
        pytest.param(
            {
                "HOME": "C:\\Users\\operator",
                "USERPROFILE": "C:\\Users\\operator",
                "LOCALAPPDATA": "C:\\Users\\operator\\AppData\\Local",
                "APPDATA": "C:\\Users\\operator\\AppData\\Roaming",
            },
            id="windows-style-input",
        ),
    ],
)
def test_ovh_qwen_child_env_scopes_all_home_vars_on_both_platforms(
    tmp_path, monkeypatch, operator_home_env
) -> None:
    # #56: the allowlist strips the child's home vars, so `agenttalk` run INSIDE the
    # model's own turn cannot resolve its config/state home and crashes. The scoped
    # env must supply the full home set (HOME + Windows USERPROFILE/LOCALAPPDATA/APPDATA)
    # pointed at the disposable workspace clone, for both POSIX-style and Windows-style
    # operator environments, and must never echo the operator's real home to the paid
    # external worker. (_child_env sets these unconditionally, so no os.name branch to
    # patch here; monkeypatching os.name on a Windows host also breaks pytest's Path
    # repr on failure.)
    ambient = {"PATH": "safe-path", "AGENTTALK_SELF": "qwen-dev-1"}
    ambient.update(operator_home_env)
    monkeypatch.setattr(os, "environ", ambient)

    env = run._child_env(
        tmp_path,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "front-token",
        },
    )

    workspace = str(tmp_path.resolve())
    # Every home var the child (and the tools it shells out to) may consult is present
    # and scoped to the workspace clone.
    assert env["HOME"] == workspace
    assert env["USERPROFILE"] == workspace
    assert env["LOCALAPPDATA"] == str((tmp_path / "AppData" / "Local").resolve())
    assert env["APPDATA"] == str((tmp_path / "AppData" / "Roaming").resolve())
    # The operator's real home never leaks into the paid worker's environment.
    for leaked in operator_home_env.values():
        assert leaked not in env.values()


def test_ovh_qwen_child_env_lets_signing_resolve_home_without_crash(
    tmp_path, monkeypatch
) -> None:
    # REGRESSION: with the allowlist stripping LOCALAPPDATA/USERPROFILE/HOME, a child
    # shelling out to `agenttalk reply` crashed with "Could not determine home
    # directory" inside signing.default_keys_dir() (Path.home()) before it could even
    # decide signing was off. Applying the scoped child env must let key-path resolution
    # return a path UNDER the workspace and never raise.
    from agenttalk import signing

    monkeypatch.setattr(
        os,
        "environ",
        {"PATH": "safe-path", "SystemRoot": "C:\\Windows", "AGENTTALK_SELF": "qwen-dev-1"},
    )
    env = run._child_env(
        tmp_path,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "front-token",
        },
    )
    # Simulate the child process seeing exactly this environment.
    monkeypatch.setattr(os, "environ", dict(env))
    key_path = signing.resolve_key_path("deadbeef")   # must NOT raise
    workspace = str(tmp_path.resolve())
    assert str(key_path).startswith(workspace)         # resolved under the scoped home
    assert not key_path.exists()                       # signing stays unenforced (no key)


def test_ovh_qwen_turn_capability_replaces_master_front_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os, "environ", {"PATH": "safe-path"})

    env = run._child_env(
        tmp_path,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "master-front-token",
        },
        gateway_capability="atgw-child-" + "a" * 43,
    )

    assert env["ANTHROPIC_AUTH_TOKEN"] == "atgw-child-" + "a" * 43
    assert "master-front-token" not in env.values()


def test_real_qwen_spawner_mints_capability_for_immutable_message(
    tmp_path,
    monkeypatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    opened: list[dict[str, str]] = []
    child_environments: list[dict[str, str]] = []

    class FakeLedger:
        def open_child_turn(self, **scope):
            opened.append(scope)
            return SimpleNamespace(token="atgw-child-" + "c" * 43)

    class FakeProcStream:
        returncode = 0

        def __init__(self, *_args, child_env, **_kwargs):
            child_environments.append(child_env)

        def __iter__(self):
            yield json.dumps(
                {"type": "stream_event", "event": {"type": "message_start"}}
            )
            yield json.dumps(
                {"type": "stream_event", "event": {"type": "message_stop"}}
            )

    monkeypatch.setattr(ovh_gateway, "SpendLedger", FakeLedger)
    monkeypatch.setattr(run, "_ProcStream", FakeProcStream)
    state = session.SessionState(cli="claude", claude_session_id="session-1")
    drive = run.make_drive(
        store,
        "qwen-dev-1",
        "claude",
        state,
        ["claude"],
        render=False,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "master-front-token",
        },
    )

    outcome = drive(
        {
            "id": "immutable-message-id",
            "from": "lead",
            "kind": "question",
            "body": "do work",
            "meta": {"request_id": "q-parent"},
        }
    )

    assert outcome.ok is True
    assert opened == [
        {
            "agent": "qwen-dev-1",
            "message_id": "immutable-message-id",
            "request_id": "q-parent",
            "issuer_token": "master-front-token",
        }
    ]
    assert child_environments[0]["ANTHROPIC_AUTH_TOKEN"] == (
        "atgw-child-" + "c" * 43
    )
    assert "master-front-token" not in child_environments[0].values()


def test_qwen_spawner_never_launches_when_capability_issue_fails(
    tmp_path,
    monkeypatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])

    class BlockedLedger:
        def open_child_turn(self, **_scope):
            raise ovh_gateway.ChildTurnCapBlocked("cap schema unavailable")

    def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("paid child spawned before durable cap issuance")

    monkeypatch.setattr(ovh_gateway, "SpendLedger", BlockedLedger)
    monkeypatch.setattr(run, "_ProcStream", must_not_spawn)
    drive = run.make_drive(
        store,
        "qwen-dev-1",
        "claude",
        session.SessionState(cli="claude", claude_session_id="session-1"),
        ["claude"],
        render=False,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "master-front-token",
        },
    )

    outcome = drive(
        {
            "id": "immutable-message-id",
            "from": "lead",
            "kind": "question",
            "body": "do work",
            "meta": {"request_id": "q-parent"},
        }
    )

    assert outcome.ok is False
    assert outcome.failure_class == CLASS_CONFIG_BLOCKED
    assert "capability unavailable" in outcome.summary


def test_ovh_qwen_profile_rejects_unpinned_gateway_or_extra_env(tmp_path) -> None:
    with pytest.raises(ValueError, match="pinned loopback"):
        run._child_env(
            tmp_path,
            backend_profile="ovh-qwen",
            profile_env={
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "ANTHROPIC_AUTH_TOKEN": "token",
            },
        )
    with pytest.raises(ValueError, match="unsupported keys"):
        run._child_env(
            tmp_path,
            backend_profile="ovh-qwen",
            profile_env={
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
                "ANTHROPIC_AUTH_TOKEN": "token",
                "ANTHROPIC_API_KEY": "forbidden",
            },
        )
    with pytest.raises(ValueError, match="pinned model alias"):
        run._child_env(
            tmp_path,
            backend_profile="ovh-qwen",
            profile_env={
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
                "ANTHROPIC_AUTH_TOKEN": "token",
                "ANTHROPIC_MODEL": "other-model",
            },
        )
    with pytest.raises(ValueError, match="pinned output-token cap"):
        run._child_env(
            tmp_path,
            backend_profile="ovh-qwen",
            profile_env={
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
                "ANTHROPIC_AUTH_TOKEN": "token",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
            },
        )


def test_ovh_qwen_profile_refuses_a_path_resolving_outside_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    original_resolve = run.Path.resolve
    outside = tmp_path.parent / "operator-claude-profile"

    def resolve_with_profile_escape(path, *args, **kwargs):
        if path.name == "claude-profile":
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(run.Path, "resolve", resolve_with_profile_escape)
    with pytest.raises(ValueError, match="must resolve inside"):
        run._child_env(
            tmp_path,
            backend_profile="ovh-qwen",
            profile_env={
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
                "ANTHROPIC_AUTH_TOKEN": "token",
            },
        )


@pytest.mark.parametrize("code", ["ATGW_POLICY_BLOCKED", "ATGW_LEDGER_BLOCKED"])
def test_ovh_policy_and_ledger_blocks_as_terminal_text_stay_config_blocked(code) -> None:
    # A ledger/policy block surfaced as terminal TEXT is a 503 seen by an ALREADY-MINTED child
    # turn mid-stream: it stays config_blocked (unchanged from master). CLASS_GATEWAY_HELD is
    # scoped to the MINT-TIME hold only (see the signal test below) - a mid-turn hold cannot
    # self-heal because the minted turn's 300s wall-clock keeps burning during the hold, so a
    # post-clear re-mint hits ChildTurnCapExceeded -> config_blocked anyway (Fable review of
    # b173f36, MAJOR-1). Full mid-turn recovery is the turn-reaper follow-up, not a classification.
    classification, summary = run._classify_drive_failure(
        {"terminal": True, "terminal_text": f'API Error: 503 {{"type":"{code}"}}'},
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_CONFIG_BLOCKED
    assert "hold" in summary


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "review prose quotes ATGW_POLICY_BLOCKED",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "printf harmless",
                "exit_code": 1,
                "status": "failed",
                "aggregated_output": "review mentions status code: 422",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": ("x" * 5000) + " ATGW_POLICY_BLOCKED",
            },
        },
    ],
    ids=["assistant-json", "tool-json", "long-json-before-tail-bound"],
)
def test_ovh_parsed_json_payload_cannot_become_raw_gateway_diagnostic(
    tmp_path,
    payload,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    stream = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(payload),
    ]
    drive = run.make_drive(
        store,
        "qwen-dev-1",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=lambda _argv, _stdin: stream,
        clock=lambda: 0.0,
        render=False,
        backend_profile="ovh-qwen",
    )

    outcome = drive({
        "id": "inbound",
        "from": "lead",
        "kind": "message",
        "body": "work",
        "meta": {},
    })

    assert outcome.ok is False
    assert outcome.failure_class == CLASS_AMBIGUOUS


@pytest.mark.parametrize("quoted_marker", ["status code: 422", "status code: 503"])
def test_ovh_failed_bus_tool_output_cannot_become_gateway_diagnostic(
    tmp_path,
    quoted_marker,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    tool_output = f"Access is denied; quoted finding says {quoted_marker}"
    stream = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": (
                    '& "$env:AGENTTALK_PY" -m agenttalk reply '
                    "--to-id inbound -m answer"
                ),
                "exit_code": 1,
                "status": "failed",
                "aggregated_output": tool_output,
            },
        }),
    ]
    drive = run.make_drive(
        store,
        "qwen-dev-1",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=lambda _argv, _stdin: stream,
        clock=lambda: 0.0,
        render=False,
        backend_profile="ovh-qwen",
    )

    outcome = drive({
        "id": "inbound",
        "from": "lead",
        "kind": "message",
        "body": "work",
        "meta": {},
    })

    assert outcome.ok is False
    assert outcome.failure_class == CLASS_CONFIG_BLOCKED
    assert "Access is denied" in outcome.summary
    assert "OVH gateway" not in outcome.summary


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("ATGW_POLICY_BLOCKED", CLASS_CONFIG_BLOCKED),
        ("API Error: status code: 503", CLASS_INFRA),
    ],
)
def test_ovh_discarded_non_json_gateway_diagnostic_still_classifies(
    tmp_path,
    diagnostic,
    expected,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    stream = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "turn.started"}),
        diagnostic,
    ]
    drive = run.make_drive(
        store,
        "qwen-dev-1",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=lambda _argv, _stdin: stream,
        clock=lambda: 0.0,
        render=False,
        backend_profile="ovh-qwen",
    )

    outcome = drive({
        "id": "inbound",
        "from": "lead",
        "kind": "message",
        "body": "work",
        "meta": {},
    })

    assert outcome.ok is False
    assert outcome.failure_class == expected


def test_gateway_transient_hold_signal_classifies_gateway_held() -> None:
    # The mint-time LedgerHold surfaces as an explicit sig flag, classified BEFORE any text
    # heuristic and independent of backend profile.
    classification, summary = run._classify_drive_failure(
        {"gateway_transient_hold": True, "error": "durable child turn capability unavailable"},
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_GATEWAY_HELD
    assert "held" in summary


def test_qwen_spawner_classifies_gateway_held_on_ledger_hold_without_spawning(
    tmp_path,
    monkeypatch,
) -> None:
    # The REAL make_drive path: a mint-time LedgerHold must (a) never spawn a paid child and
    # (b) classify CLASS_GATEWAY_HELD (transient), NOT config_blocked. This pins the except
    # ORDERING at the mint site - LedgerHold is a GatewayError subclass, so it must be caught
    # first; if the broad `except GatewayError` shadowed it, this would be config_blocked (#62).
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])

    class HeldLedger:
        def open_child_turn(self, **_scope):
            raise ovh_gateway.LedgerHold("gateway has a durable accounting hold")

    def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("paid child spawned while the gateway was held")

    monkeypatch.setattr(ovh_gateway, "SpendLedger", HeldLedger)
    monkeypatch.setattr(run, "_ProcStream", must_not_spawn)
    drive = run.make_drive(
        store,
        "qwen-dev-1",
        "claude",
        session.SessionState(cli="claude", claude_session_id="session-1"),
        ["claude"],
        render=False,
        backend_profile="ovh-qwen",
        profile_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "ANTHROPIC_AUTH_TOKEN": "master-front-token",
        },
    )

    outcome = drive(
        {
            "id": "immutable-message-id",
            "from": "lead",
            "kind": "question",
            "body": "do work",
            "meta": {"request_id": "q-parent"},
        }
    )

    assert outcome.ok is False
    assert outcome.failure_class == CLASS_GATEWAY_HELD
    assert "held" in outcome.summary


def test_ovh_child_turn_cap_is_terminal_config_blocked() -> None:
    classification, summary = run._classify_drive_failure(
        {
            "terminal": True,
            "terminal_text": (
                'API Error: 403 {"type":"ATGW_CHILD_TURN_CAP_EXCEEDED"}'
            ),
        },
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_CONFIG_BLOCKED
    assert "budget exhausted" in summary


@pytest.mark.parametrize(
    "fixture",
    [
        "API Error: Connection refused (127.0.0.1:4000)",
        "API Error: status code: 429",
        'API Error: 502 {"type":"ATGW_INFRA_UNAVAILABLE"}',
        "API Error: http 503",
    ],
)
def test_ovh_gateway_outages_are_infra_never_poison(fixture) -> None:
    classification, _ = run._classify_drive_failure(
        {"terminal": True, "terminal_text": fixture},
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_INFRA


@pytest.mark.parametrize(
    "fixture",
    [
        "API Error: status code: 422",
        'API Error: 400 {"type":"ATGW_CONFIG_ERROR"}',
    ],
)
def test_ovh_route_failures_are_terminal_configuration_errors(fixture) -> None:
    classification, _ = run._classify_drive_failure(
        {"terminal": True, "terminal_text": fixture},
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_CONFIG_BLOCKED


def test_unknown_qwen_failure_keeps_existing_ambiguous_fallback() -> None:
    classification, _ = run._classify_drive_failure(
        {"terminal": True, "terminal_text": "unexpected model-specific output"},
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_AMBIGUOUS
