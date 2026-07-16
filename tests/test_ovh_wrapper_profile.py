from __future__ import annotations

import os

import pytest

from agenttalk.wrapper import run
from agenttalk.wrapper.loop import CLASS_AMBIGUOUS, CLASS_CONFIG_BLOCKED, CLASS_INFRA


def test_non_profile_child_environment_is_unchanged(tmp_path, monkeypatch) -> None:
    ambient = {
        "PATH": "p",
        "UNRELATED_SECRET": "still-historical",
        "ANTHROPIC_API_KEY": "still-historical",
        "AGENTTALK_LEAD_LOOP_LEASE": "removed-by-existing-contract",
        "AGENTTALK_WRAPPER_GENERATION": "removed-by-existing-contract",
        "AGENTTALK_INBOUND_REQUEST_ID": "removed-by-existing-contract",
    }
    monkeypatch.setattr(os, "environ", ambient.copy())
    result = run._child_env(tmp_path)
    assert result == {
        "PATH": "p",
        "UNRELATED_SECRET": "still-historical",
        "ANTHROPIC_API_KEY": "still-historical",
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
            "ANTHROPIC_API_KEY": "must-not-pass",
            "ANTHROPIC_AUTH_TOKEN": "ambient-must-not-pass",
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
    assert env["ANTHROPIC_AUTH_TOKEN"] == "front-token"
    assert env["ANTHROPIC_MODEL"] == "Qwen3.5-397B-A17B"
    assert env["CLAUDE_CONFIG_DIR"] == str(
        (tmp_path / ".agenttalk" / "gateway" / "claude-profile").resolve()
    )
    assert env["AGENTTALK_WRAPPER_GENERATION"] == "generation"
    assert env["AGENTTALK_INBOUND_REQUEST_ID"] == "request"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OVH_KEY" not in env
    assert "UNRELATED_SECRET" not in env
    assert "HOME" not in env
    assert "USERPROFILE" not in env
    assert "C:\\Users\\operator\\.claude" not in env.values()
    assert "AGENTTALK_LEAD_LOOP_LEASE" not in env
    assert "ambient-must-not-pass" not in env.values()
    assert "stale-must-not-pass" not in env.values()


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
def test_ovh_policy_and_ledger_blocks_are_terminal_non_poison(code) -> None:
    classification, summary = run._classify_drive_failure(
        {"terminal": True, "terminal_text": f'API Error: 503 {{"type":"{code}"}}'},
        backend_profile="ovh-qwen",
    )
    assert classification == CLASS_CONFIG_BLOCKED
    assert "hold" in summary


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
