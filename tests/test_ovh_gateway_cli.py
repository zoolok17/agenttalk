from __future__ import annotations

import json
import sys

import pytest

from agenttalk import cli
from agenttalk import ovh_gateway
from agenttalk import ovh_gateway_service
from agenttalk.ovh_gateway import SpendLedger, default_install_marker_path, default_ledger_path
from agenttalk.ovh_gateway import MODEL_ALIAS
from agenttalk.store import Store


def test_gateway_cli_initializes_once_and_controls_manual_hold(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")

    rc = cli.main([
        "--root",
        str(root),
        "gateway",
        "init",
        "--litellm-executable",
        str(executable),
        "--opening-eur",
        "0.58",
        "--opening-evidence",
        "OVH AI Endpoints dashboard, observed 2026-07-16 morning",
    ])

    assert rc == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["initialized"] is True
    assert initialized["opening_micro_eur"] == 580_000
    assert "atgw-" not in json.dumps(initialized)

    assert cli.main([
        "--root",
        str(root),
        "gateway",
        "hold",
        "--reason",
        "dashboard mismatch",
    ]) == 0
    held = json.loads(capsys.readouterr().out)
    assert held["held"] is True
    ledger = SpendLedger(default_ledger_path(), default_install_marker_path())
    assert ledger.status()["service_hold"] == "manual: dashboard mismatch"

    assert cli.main([
        "--root",
        str(root),
        "gateway",
        "clear-hold",
        "--reason",
        "operator approved",
    ]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["held"] is False
    assert ledger.status()["service_hold"] is None

    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    assert cli.main([
        "--root",
        str(root),
        "gateway",
        "canary-verify",
        "1" * 32,
        "--dashboard-delta-eur",
        "0.00096",
    ]) == 0
    canary = json.loads(capsys.readouterr().out)
    assert canary["accepted"] is True
    assert canary["expected_micro_eur"] == 960


def test_gateway_cli_rejects_caller_supplied_actual_reconciliation(
    tmp_path,
    capsys,
) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "--root",
            str(root),
            "gateway",
            "reconcile",
            "1" * 32,
            "--outcome",
            "charge-actual",
            "--reason",
            "caller supplied value",
        ])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_gateway_status_not_ready_uses_operational_error_exit(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert cli.main(["--root", str(root), "gateway", "status"]) == 2


def test_qwen_wrap_requires_authenticated_gateway_readiness_before_token_read(
    tmp_path,
    monkeypatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    store.set_trust_class("qwen-dev-1", "external-worker")
    (store.dir / "supervisor.json").write_text(
        json.dumps({
            "agents": {
                "qwen-dev-1": {
                    "backend_profile": "ovh-qwen",
                    "cli": "claude",
                    "model": "Qwen3.5-397B-A17B",
                    "trust_class": "external-worker",
                    "wrapped": True,
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        ovh_gateway_service,
        "gateway_status",
        lambda _root: {"ready": False, "errors": ["internal_liveliness_failed"]},
    )

    def token_must_not_be_read(_path):
        raise AssertionError("front token read before gateway readiness")

    monkeypatch.setattr(ovh_gateway, "read_secret_file", token_must_not_be_read)

    rc = cli.main([
        "--root",
        str(tmp_path),
        "wrap",
        "--for",
        "qwen-dev-1",
        "--cli",
        "claude",
        "--loop",
        "--",
        sys.executable,
        "-c",
        "pass",
    ])

    assert rc == 1
    hold = store.read_config_blocked_hold("qwen-dev-1")
    assert hold is not None
    assert "internal_liveliness_failed" in hold["summary"]
