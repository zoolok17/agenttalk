from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from agenttalk import cli
from agenttalk import ovh_gateway
from agenttalk import ovh_gateway_service
from agenttalk.ovh_gateway import SpendLedger, default_install_marker_path, default_ledger_path
from agenttalk.ovh_gateway import MODEL_ALIAS
from agenttalk.wrapper import run as wrapper_run
from agenttalk.store import Store


TEST_CHILD_CAP_ISSUER = "atgw-" + "i" * 43


def _make_qwen_wrap_store(tmp_path) -> Store:
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
    return store


def _worker_gateway_projection(ledger: SpendLedger) -> dict:
    ledger_status = ledger.status()
    return {
        "ready": True,
        "operational_ready": True,
        "errors": [],
        "worker_spend_ready": ledger_status["worker_spend_ready"],
        "worker_spend_errors": ledger_status["worker_spend_errors"],
    }


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


def test_gateway_cli_runtime_rebind_routes_candidate_and_prints_result(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    candidate = tmp_path / "unusual runtime" / "launcher.shim"
    captured: dict[str, object] = {}

    def fake_rebind(received_root, *, litellm_executable):
        captured["root"] = received_root
        captured["litellm_executable"] = litellm_executable
        return {
            "runtime_rebound": True,
            "changed": True,
            "litellm_executable": str(candidate),
        }

    monkeypatch.setattr(ovh_gateway_service, "rebind_runtime", fake_rebind)

    rc = cli.main([
        "--root",
        str(root),
        "gateway",
        "runtime-rebind",
        "--litellm-executable",
        str(candidate),
    ])

    assert rc == 0
    assert captured == {
        "root": root.resolve(),
        "litellm_executable": str(candidate),
    }
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_rebound"] is True
    assert result["litellm_executable"] == str(candidate)


def test_gateway_cli_runtime_rebind_help_states_probe_authority_and_exit_contract(
    capsys,
) -> None:
    for argv in (["gateway", "--help"], ["gateway", "runtime-rebind", "--help"]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(argv)
        assert exc_info.value.code == 0
        rendered = capsys.readouterr().out.casefold()
        assert "trusted" in rendered
        assert "filesystem authority" in rendered
        assert "unsandboxed" in rendered
        assert "exit 3" in rendered
        assert "exit 2" in rendered


@pytest.mark.parametrize(
    ("error_type", "reason", "expected_rc"),
    [
        (
            ovh_gateway_service.LiteLLMRuntimeProbeFailed,
            "litellm_runtime_probe_failed",
            2,
        ),
        (
            ovh_gateway_service.LiteLLMRuntimeProbeUnknown,
            "litellm_runtime_probe_unknown",
            3,
        ),
    ],
)
def test_gateway_cli_runtime_rebind_surfaces_named_probe_refusal(
    tmp_path,
    monkeypatch,
    capsys,
    error_type,
    reason,
    expected_rc,
) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    candidate = tmp_path / "runtime.exe"
    refusal = (
        f"{reason}: retry "
        f'agenttalk --root "{root.resolve()}" gateway runtime-rebind '
        f'--litellm-executable "{candidate}"'
    )

    def refuse_rebind(_root, *, litellm_executable):
        assert _root == root.resolve()
        assert litellm_executable == str(candidate)
        message = refusal.removeprefix(f"{reason}: ")
        raise error_type(message)

    monkeypatch.setattr(ovh_gateway_service, "rebind_runtime", refuse_rebind)

    rc = cli.main([
        "--root",
        str(root),
        "gateway",
        "runtime-rebind",
        "--litellm-executable",
        str(candidate),
    ])

    output = capsys.readouterr()
    assert rc == expected_rc
    assert output.out == ""
    assert output.err == f"agenttalk gateway runtime-rebind: {refusal}\n"


@pytest.mark.parametrize(
    ("error_factory", "reason", "expected_rc"),
    [
        (
            lambda path: ovh_gateway_service.GatewayLifecycleContended(
                ovh_gateway_service.LifecycleLockContended({
                    "pid": 42,
                    "process_identity": {
                        "scheme": "win32-filetime-v1",
                        "value": "123",
                    },
                    "operation": "reconfigure",
                    "acquired_at": "2026-08-17T12:00:00.000000Z",
                })
            ),
            "gateway_lifecycle_contended",
            2,
        ),
        (
            lambda path: ovh_gateway_service.GatewayLifecycleUnknown(
                ovh_gateway_service.LifecycleLockUnknown(path, "metadata is corrupt")
            ),
            "gateway_lifecycle_unknown",
            3,
        ),
    ],
)
def test_gateway_cli_runtime_rebind_surfaces_typed_lifecycle_refusal(
    tmp_path,
    monkeypatch,
    capsys,
    error_factory,
    reason,
    expected_rc,
) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    candidate = tmp_path / "runtime.exe"
    refusal = error_factory(root / ".agenttalk" / "gateway" / "lifecycle.lock")

    def refuse_rebind(_root, *, litellm_executable):
        assert _root == root.resolve()
        assert litellm_executable == str(candidate)
        raise refusal

    monkeypatch.setattr(ovh_gateway_service, "rebind_runtime", refuse_rebind)

    rc = cli.main([
        "--root",
        str(root),
        "gateway",
        "runtime-rebind",
        "--litellm-executable",
        str(candidate),
    ])

    output = capsys.readouterr()
    assert rc == expected_rc
    assert output.out == ""
    assert output.err.startswith(
        f"agenttalk gateway runtime-rebind: {reason}: "
    )


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


def test_gateway_cli_atomically_installs_child_caps_on_existing_ledger(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    ledger = SpendLedger(default_ledger_path(), default_install_marker_path())
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("DROP TABLE child_attempts")
        conn.execute("DROP TABLE child_capabilities")
        conn.execute("DROP TABLE child_turns")
        conn.execute(
            "DELETE FROM metadata WHERE key IN (?, ?, ?)",
            (
                "child_cap_schema_version",
                "child_cap_policy_hash",
                "child_cap_issuer_sha256",
            ),
        )
        conn.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    marker["ledger_schema_version"] = 1
    ledger.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    ovh_gateway.write_secret_file(
        ovh_gateway.default_front_token_path(), TEST_CHILD_CAP_ISSUER
    )

    assert cli.main(["--root", str(root), "gateway", "cap-install"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["installed"] is True
    assert ledger.status()["child_cap_ready"] is True


def test_gateway_status_not_ready_uses_operational_error_exit(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    Store(root).init(["lead"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert cli.main(["--root", str(root), "gateway", "status"]) == 2


def test_qwen_wrap_requires_authenticated_gateway_readiness_before_token_read(
    tmp_path,
    monkeypatch,
) -> None:
    store = _make_qwen_wrap_store(tmp_path)
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


def test_qwen_wrap_rejects_uncapped_lead_loop_cadence(
    tmp_path,
    capsys,
) -> None:
    store = _make_qwen_wrap_store(tmp_path)
    store.set_managed_lead_loop("qwen-dev-1")

    rc = cli.main([
        "--root",
        str(tmp_path),
        "wrap",
        "--for",
        "qwen-dev-1",
        "--cli",
        "claude",
        "--loop",
        "--lead-loop",
        "--",
        sys.executable,
        "-c",
        "pass",
    ])

    assert rc == 2
    assert "does not support --lead-loop" in capsys.readouterr().err
    assert store.read_lead_loop_lease("qwen-dev-1") is None


@pytest.mark.parametrize(
    ("canary_state", "expected_reason"),
    [
        ("absent", "dashboard_canary_absent"),
        ("mismatch", "dashboard_canary_mismatch"),
        ("mismatch-cleared", "dashboard_canary_mismatch"),
    ],
)
def test_qwen_wrap_worker_spend_preflight_blocks_unaccepted_canary_before_token_read(
    tmp_path,
    monkeypatch,
    canary_state,
    expected_reason,
) -> None:
    store = _make_qwen_wrap_store(tmp_path)
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    if canary_state != "absent":
        ledger.reserve("1" * 32)
        ledger.settle(
            "1" * 32,
            model=MODEL_ALIAS,
            input_tokens=1_000,
            output_tokens=100,
        )
        ledger.verify_dashboard_canary("1" * 32, observed_delta_micro_eur=0)
    if canary_state == "mismatch-cleared":
        ledger.clear_hold(reason="operator inspected mismatch")

    def status_projection(_root):
        return _worker_gateway_projection(ledger)

    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ovh_gateway_service, "gateway_status", status_projection)

    def token_must_not_be_read(_path):
        raise AssertionError("front token read before worker/spend readiness")

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
    assert expected_reason in hold["summary"]


def test_qwen_wrap_worker_spend_preflight_allows_accepted_canary(
    tmp_path,
    monkeypatch,
) -> None:
    _make_qwen_wrap_store(tmp_path)
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    ledger.verify_dashboard_canary("1" * 32, observed_delta_micro_eur=960)

    def status_projection(_root):
        return _worker_gateway_projection(ledger)

    token_reads: list[str] = []

    def read_front_token(path):
        token_reads.append(str(path))
        return "front-token"

    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ovh_gateway_service, "gateway_status", status_projection)
    monkeypatch.setattr(ovh_gateway, "read_secret_file", read_front_token)
    monkeypatch.setattr(
        wrapper_run,
        "preflight_launch_runtime",
        lambda argv, _cli, _root, _env: wrapper_run.LaunchPreflightResult(list(argv)),
    )
    monkeypatch.setattr(cli, "_wrap_loop_mode", lambda *_args, **_kwargs: 0)

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

    assert rc == 0
    assert len(token_reads) == 1
