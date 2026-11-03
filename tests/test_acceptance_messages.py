"""Acceptance diagnostics describe the inputs without exposing private locators."""

import json

import pytest

from agenttalk import acceptance as A, acceptance_cold, acceptance_registry as R, cli
import test_acceptance_registry as records
import test_acceptance as attempts
from test_acceptance import (
    attach,
    bundle,
    cold_phase,
    open_attempt,
    write_json,
)

environment, plan, registry = records.environment, records.plan, records.registry
case, case_v2, case_v3 = attempts.case, attempts.case_v2, attempts.case_v3


@pytest.mark.parametrize(
    "change",
    [
        {"binding": []},
        {"binding": {}},
        {"binding": {"attempt_id": "wrong"}},
        {"reviewer": "wrong"},
    ],
)
def test_cold_binding_error_names_both_comparisons(case_v3, capsys, change):
    assert open_attempt(case_v3) == 0
    capsys.readouterr()

    assert cold_phase(case_v3, "commit", change=change) == 2

    error = capsys.readouterr().err
    assert "binding must equal the seven keys and values of close.acceptance_route" in error
    for key in (
        "instance_id",
        "attempt_id",
        "revision",
        "project_id",
        "plan_hash",
        "registry_hash",
        "obligations_hash",
    ):
        assert key in error
    assert "reviewer must equal plan.cold_policy.reviewer" in error


@pytest.mark.parametrize(
    "missing, label",
    [
        ("plan", "--plan file (schema-4 plan JSON)"),
        ("registry", "plan.registry_ref file (schema-2 registry JSON)"),
        ("observation", "--observation file (schema-1 observation JSON)"),
    ],
)
def test_preflight_unreadable_input_names_role_and_shape(tmp_path, capsys, missing, label):
    plan = tmp_path / "private-plan.json"
    registry = tmp_path / "private-registry.json"
    if missing != "plan":
        plan.write_text(json.dumps({"registry_ref": registry.name}), encoding="utf-8")
    if missing == "observation":
        registry.write_text("{}", encoding="utf-8")
    arguments = ["close", "acceptance", "preflight", "--plan", str(plan), "--cache-root", str(tmp_path), "--json"]
    if missing == "observation":
        arguments.extend(["--observation", str(tmp_path / "private-observation.json")])

    assert cli.main(arguments) == 3

    payload = capsys.readouterr().out
    assert label in json.loads(payload)["holds"][0]["detail"]
    assert str(tmp_path) not in payload
    assert "private" not in payload


def test_preflight_evaluation_refusal_has_no_input_file_label(tmp_path, capsys):
    plan = tmp_path / "plan.json"
    registry = tmp_path / "registry.json"
    observation = tmp_path / "observation.json"
    plan.write_text(json.dumps({"registry_ref": registry.name}), encoding="utf-8")
    registry.write_text("{}", encoding="utf-8")  # Readable, but refused during evaluation.
    observation.write_text("{}", encoding="utf-8")

    assert (
        cli.main(
            [
                "close",
                "acceptance",
                "preflight",
                "--plan",
                str(plan),
                "--observation",
                str(observation),
                "--cache-root",
                str(tmp_path),
                "--json",
            ]
        )
        == 3
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refusal"
    detail = payload["holds"][0]["detail"]
    assert not detail.startswith(("--plan file", "plan.registry_ref file", "--observation file"))
    assert "registry: expected fields schema_version entries files" in detail


def test_cold_schema_error_names_report_version(case_v3, capsys):
    assert open_attempt(case_v3) == 0
    capsys.readouterr()

    assert cold_phase(case_v3, "commit", change={"schema_version": 3}) == 2

    assert "cold report schema_version must be 1" in capsys.readouterr().err


def test_attach_schema_error_names_bundle_version(case, capsys):
    assert open_attempt(case) == 0
    value = bundle(case)
    value["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", value)
    capsys.readouterr()

    assert attach(case) == 2

    assert "acceptance bundle schema_version must be 1" in capsys.readouterr().err


def test_reconciliation_schema_error_names_report_version():
    value = {
        "schema_version": 2,
        "commit_hash": "a" * 64,
        "bundle_hash": "b" * 64,
        "revealed": True,
        "findings": [],
        "closeout": {},
    }
    route = {"cold_commit_hash": value["commit_hash"], "bundle_hash": value["bundle_hash"]}
    with pytest.raises(A.AcceptanceError) as error:
        acceptance_cold.validate_reconciliation(value, route, {})
    assert "cold reconciliation schema_version must be 1" in str(error.value)


def test_registry_digest_error_names_exact_bytes(case, capsys):
    path = case["inputs"] / "registry.json"
    path.write_bytes(path.read_bytes() + b"\n")  # Same JSON, different pinned bytes.

    assert open_attempt(case) == 3

    error = capsys.readouterr().err
    assert "plan.registry_digest must equal the SHA-256 of the registry file's bytes" in error


def test_schema4_registry_digest_error_names_exact_bytes(plan, registry):
    plan["registry_digest"] = "0" * 64
    with pytest.raises(A.AcceptanceError) as error:
        R.policy(json.dumps(plan).encode(), json.dumps(registry).encode())
    assert "plan.registry_digest must equal the SHA-256 of the registry file's bytes" in str(error.value)


def test_registry_shape_error_describes_entry_fields(case, capsys):
    write_json(case["inputs"] / "registry.json", {})

    assert open_attempt(case) == 3

    error = capsys.readouterr().err
    assert "schema_version must be 1" in error
    assert "each entries item must have id, kind, version, sha256" in error
    assert "kind must be toolchain, checker or service" in error


@pytest.mark.parametrize(
    "command, terms",
    [
        ("attach", ("Bundle JSON", "acceptance_route", "runs", "rows", "artifacts", "relative", "docs/ACCEPTANCE.md")),
        (
            "cold",
            (
                "Report JSON",
                "schema_version 1",
                "binding",
                "delivery_manifest",
                "observations",
                "commit_hash",
                "bundle_hash",
                "findings",
                "docs/ACCEPTANCE.md",
            ),
        ),
    ],
)
def test_acceptance_file_help_describes_input(command, terms, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["close", "acceptance", command, "--help"])
    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for term in terms:
        assert term in help_text
