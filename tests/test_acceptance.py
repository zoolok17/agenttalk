"""Acceptance 1a through real project Git, retained files and the close CLI."""

from copy import deepcopy
import hashlib
import json
import subprocess

import pytest

from agenttalk import acceptance, cli, close
from agenttalk.store import Store


def write_json(path, value):
    data = json.dumps(value, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def case(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.name", "Synthetic Author")
    git(project, "config", "user.email", "synthetic@example.invalid")
    (project / "source.txt").write_text("synthetic source\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-qm", "synthetic fixture")
    sha = git(project, "rev-parse", "HEAD")
    store = Store(tmp_path / "bus")
    store.init(["lead", "runner-a", "runner-b", "cold"])
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    digest = write_json(inputs / "registry.json", {"schema_version": 1, "entries": []})
    plan = {"schema_version": 1, "plan_id": "plan", "project_id": "project", "scope": "milestone",
            "authors": ["author"], "trust_profile": "cooperative", "registry_ref": "registry.json",
            "registry_digest": digest,
            "partitions": [{"id": "bar", "agents": ["runner-a"]}, {"id": "tools", "agents": ["runner-b"]}],
            "rows": [{"id": "build", "partition": "bar", "policy": "gating", "comparator": "exit-code",
                      "expected": 0, "artifact": "build", "field": "exit_code"},
                     {"id": "tool", "partition": "tools", "policy": "gating", "comparator": "exact-value",
                      "expected": 3, "artifact": "tool", "field": "count"}]}
    write_json(inputs / "plan.json", plan)
    return {"project": project, "sha": sha, "store": store, "inputs": inputs, "plan": plan}


def command(case, *args):
    return cli.main(["--root", str(case["store"].root), "close", *args])


def open_attempt(case, *extra):
    return command(case, "open", "--id", "attempt", "--from", "lead", "--scope", "milestone",
                   "--revision", case["sha"], "--non-lane-isolation-not-asserted",
                   "--acceptance-plan", str(case["inputs"] / "plan.json"),
                   "--project-repo", str(case["project"]), *extra)


def accept_all(case):
    for partition in case["plan"]["partitions"]:
        assert command(case, "ack", "--id", "attempt", "--lens", "acceptance-run-" + partition["id"],
                       "--from", partition["agents"][0], "--status", "accept", "--risk-class", "quality",
                       "--release-blocker", "no", "--tests-referenced", "synthetic",
                       "--tests-executed", "synthetic", "--residual-risk", "increment incomplete",
                       "--evidence", "caller-claims-pass") == 0


def bundle(case, *, build_exit=0, tool_value=3):
    rec = close.load_close(case["store"], "attempt")
    route = rec["acceptance_route"]
    value = {key: route[key] for key in ("instance_id", "attempt_id", "project_id", "revision",
                                        "plan_hash", "registry_hash")}
    value.update(schema_version=1, close_id="attempt", runs=[], rows=[], artifacts=[])
    for row, observed in zip(case["plan"]["rows"], (build_exit, tool_value), strict=True):
        partition = next(p for p in case["plan"]["partitions"] if p["id"] == row["partition"])
        run_id = "run-" + row["id"]
        value["runs"].append({"id": run_id, "partition": row["partition"], "actor": partition["agents"][0],
                              "revision": case["sha"], "head_before": case["sha"], "head_after": case["sha"],
                              "status_before": "", "status_after": ""})
        value["rows"].append({"id": row["id"], "run_id": run_id})
        raw = {"schema_version": 1, "run_id": run_id, "revision": case["sha"], "values": {row["field"]: observed}}
        name = row["artifact"] + ".json"
        digest = write_json(case["inputs"] / name, raw)
        value["artifacts"].append({"id": row["artifact"], "path": name, "sha256": digest})
    write_json(case["inputs"] / "bundle.json", value)
    return value


def attach(case):
    return command(case, "acceptance", "attach", "--id", "attempt",
                   "--file", str(case["inputs"] / "bundle.json"))


def check(case, capsys):
    capsys.readouterr()
    assert command(case, "check", "--id", "attempt", "--json") == 3
    result = json.loads(capsys.readouterr().out)
    return {h["code"] for h in result["holds"]}


def test_acceptance_acks_without_bundle_hold(case, capsys):
    assert open_attempt(case) == 0
    accept_all(case)
    assert "acceptance_record_missing" in check(case, capsys)


def test_acceptance_tampered_raw_result_holds_with_accept_acks(case, capsys):
    assert open_attempt(case) == 0
    data = bundle(case)
    assert attach(case) == 0
    accept_all(case)
    raw = case["store"].dir / "acceptance" / "sha256" / data["artifacts"][0]["sha256"]
    raw.write_text('{"claimed":"pass"}', encoding="utf-8")
    assert "acceptance_record_missing" in check(case, capsys)


def test_acceptance_raw_failure_overrides_accept_acks(case, capsys):
    assert open_attempt(case) == 0
    bundle(case, build_exit=1)
    assert attach(case) == 0
    accept_all(case)
    assert "acceptance_row_failed" in check(case, capsys)


def test_acceptance_foreign_unverifiable_sha_holds(case, capsys):
    case["sha"] = "f" * 40
    assert open_attempt(case) == 3
    assert "acceptance_project_unverified" in capsys.readouterr().err
    assert not close.close_path(case["store"], "attempt").exists()


def test_acceptance_replaced_instance_rejects_bundle(case):
    assert open_attempt(case) == 0
    data = bundle(case)
    # Model a delete/recreate outside the close API; acks/generation may look identical.
    with close.close_transaction(case["store"], "attempt") as tx:
        original = deepcopy(tx.record)
    replacement = deepcopy(original)
    replacement["instance_id"] = "f" * 32
    replacement["acceptance_route"]["instance_id"] = replacement["instance_id"]
    write_json(close.close_path(case["store"], "attempt"), replacement)
    assert attach(case) == 2
    rec = close.load_close(case["store"], "attempt")
    assert rec["acceptance_route"]["bundle_hash"] is None
    assert rec["instance_id"] != data["instance_id"]


def test_acceptance_unsupported_comparator_holds(case, capsys):
    case["plan"]["rows"][0]["comparator"] = "run-arbitrary-script"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 3
    assert "unsupported acceptance comparator" in capsys.readouterr().err


def test_acceptance_two_partitions_keep_both_acks(case):
    assert open_attempt(case) == 0
    accept_all(case)
    rec = close.load_close(case["store"], "attempt")
    assert {key: value["from"] for key, value in rec["lens_acks"].items()} == {
        "acceptance-run-bar": "runner-a", "acceptance-run-tools": "runner-b"}


@pytest.mark.parametrize("policy", [None, {"schema_version": 1, "scopes": {}}])
def test_acceptance_deleted_live_policy_cannot_bypass_route(case, capsys, policy):
    dod = case["store"].dir / "dod.json"
    write_json(dod, {"schema_version": 1, "scopes": {"milestone": {"acceptance": {"required": True}}}})
    assert open_attempt(case) == 0
    accept_all(case)
    if policy is None:
        dod.unlink()
    else:
        write_json(dod, policy)
    assert "acceptance_record_missing" in check(case, capsys)


def test_acceptance_policy_requires_missing_route(case, capsys):
    write_json(case["store"].dir / "dod.json",
               {"schema_version": 1, "scopes": {"milestone": {"acceptance": {"required": True}}}})
    assert command(case, "open", "--id", "attempt", "--from", "lead", "--scope", "milestone",
                   "--revision", case["sha"], "--non-lane-isolation-not-asserted") == 0
    assert "acceptance_policy_invalid" in check(case, capsys)


def test_acceptance_partial_increment_never_go(case, capsys):
    assert open_attempt(case) == 0
    bundle(case)
    assert attach(case) == 0
    accept_all(case)
    assert check(case, capsys) == {"acceptance_trust_unresolved"}
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


def test_acceptance_copies_evidence_before_source_cleanup(case, capsys):
    assert open_attempt(case) == 0
    data = bundle(case)
    assert attach(case) == 0
    accept_all(case)
    for artifact in data["artifacts"]:
        (case["inputs"] / artifact["path"]).unlink()
    (case["inputs"] / "bundle.json").unlink()
    (case["inputs"] / "plan.json").unlink()
    (case["inputs"] / "registry.json").unlink()
    assert check(case, capsys) == {"acceptance_trust_unresolved"}


@pytest.mark.parametrize("mode", ["before", "after", "other-head"])
def test_acceptance_project_cleanliness_is_project_not_store(case, capsys, mode):
    if mode != "before":
        assert open_attempt(case) == 0
    (case["project"] / "source.txt").write_text("changed", encoding="utf-8")
    if mode == "other-head":
        git(case["project"], "commit", "-qam", "next synthetic revision")
    if mode == "before":
        assert open_attempt(case) == 3
    else:
        assert "acceptance_project_unverified" in check(case, capsys)


@pytest.mark.parametrize("field", ["instance_id", "attempt_id", "project_id", "revision", "plan_hash", "registry_hash"])
def test_acceptance_bundle_binding_rejected_without_mutation(case, field):
    assert open_attempt(case) == 0
    data = bundle(case)
    before = close.load_close(case["store"], "attempt")
    data[field] = "different"
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt") == before


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "actor", "partition", "digest", "escape", "unknown"])
def test_acceptance_malformed_bundle_cannot_attach(case, mutation):
    assert open_attempt(case) == 0
    data = bundle(case)
    if mutation == "missing":
        data["rows"].pop()
    elif mutation == "duplicate":
        data["rows"].append(data["rows"][0])
    elif mutation == "actor":
        data["runs"][0]["actor"] = "unassigned"
    elif mutation == "partition":
        data["rows"][0]["run_id"] = data["runs"][1]["id"]
    elif mutation == "digest":
        data["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "escape":
        data["artifacts"][0]["path"] = "../outside.json"
    else:
        data["claimed_verdict"] = "GO"
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt")["acceptance_route"]["bundle_hash"] is None


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
                                 b'{"a":1e999}', b'\xff', b'{'])
def test_acceptance_json_fails_closed(raw):
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.decode(raw)


def test_acceptance_json_size_bound():
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.decode(b" " * (acceptance.MAX_BYTES + 1))


@pytest.mark.parametrize("comparator,expected,observed,failed", [
    ("exit-code", 0, 0, False), ("exit-code", 0, 2, True),
    ("exact-value", {"a": 1}, {"a": 1}, False), ("exact-value", 1, True, True),
    ("exact-failure-set", ["a", "b"], ["b", "a"], False),
    ("exact-failure-set", ["a", "b"], ["a", "c"], True),
    ("exact-failure-set", ["a", "b"], ["a"], True),
])
def test_acceptance_builtin_assertions(case, capsys, comparator, expected, observed, failed):
    row = case["plan"]["rows"][0]
    row.update(comparator=comparator, expected=expected)
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    bundle(case, build_exit=observed)
    assert attach(case) == 0
    accept_all(case)
    assert ("acceptance_row_failed" in check(case, capsys)) is failed


def test_acceptance_attach_is_immutable(case):
    assert open_attempt(case) == 0
    bundle(case)
    assert attach(case) == 0
    before = close.load_close(case["store"], "attempt")
    bundle(case, build_exit=1)
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt") == before


def test_acceptance_force_and_reopen_cannot_erase_route(case):
    assert open_attempt(case) == 0
    before = close.load_close(case["store"], "attempt")
    assert command(case, "reopen", "--id", "attempt", "--from", "lead") == 2
    assert command(case, "open", "--id", "attempt", "--from", "lead", "--scope", "milestone",
                   "--revision", case["sha"], "--force", "--non-lane-isolation-not-asserted") == 2
    assert close.load_close(case["store"], "attempt") == before


def test_acceptance_corrupt_frozen_plan_holds(case, capsys):
    assert open_attempt(case) == 0
    rec = close.load_close(case["store"], "attempt")
    path = case["store"].dir / "acceptance" / "sha256" / rec["acceptance_route"]["plan_hash"]
    path.write_bytes(b"{}")
    assert "acceptance_plan_stale" in check(case, capsys)


def test_acceptance_pending_freeze_never_clears(case, capsys, monkeypatch):
    def interrupted(*_):
        raise close.CloseError("simulated interrupted freeze")
    monkeypatch.setattr(acceptance, "freeze", interrupted)
    assert open_attempt(case) == 2
    assert "acceptance_policy_invalid" in check(case, capsys)


def test_acceptance_pure_verdict_cannot_omit_route_evaluation(case):
    assert open_attempt(case) == 0
    rec = close.load_close(case["store"], "attempt")
    result = close.compute_verdict(rec, {"verdict": "GO", "gates": [], "blockers": []})
    assert "acceptance_record_missing" in {h["code"] for h in result["holds"]}


def test_acceptance_signoffs_refuse_bus_repo_diff(case, capsys):
    assert open_attempt(case) == 0
    write_json(case["store"].dir / "signoffs.json",
               {"schema_version": 1, "defaults": {}, "risk_policies": {}, "allow_unmapped": True})
    assert command(case, "signoffs", "plan", "--id", "attempt", "--risk-class", "quality") == 2
    assert "explicit --changed-path" in capsys.readouterr().err
    assert command(case, "signoffs", "plan", "--id", "attempt", "--risk-class", "quality",
                   "--changed-path", "source.txt") == 0


def test_acceptance_informational_failure_is_preserved_but_not_gating(case, capsys):
    case["plan"]["rows"][0]["policy"] = "informational"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    bundle(case, build_exit=1)
    assert attach(case) == 0
    accept_all(case)
    assert check(case, capsys) == {"acceptance_trust_unresolved"}
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "attempt"))
    assert result["outcomes"][0] == {"id": "build", "policy": "informational", "passed": False}


@pytest.mark.parametrize("change", ["unknown", "duplicate", "profile", "boolean-exit", "registry"])
def test_acceptance_invalid_plan_refused_before_close_creation(case, change):
    if change == "unknown":
        case["plan"]["ignored_gating_rows"] = []
    elif change == "duplicate":
        case["plan"]["rows"].append(case["plan"]["rows"][0])
    elif change == "profile":
        case["plan"]["trust_profile"] = "hardened"
    elif change == "boolean-exit":
        case["plan"]["rows"][0]["expected"] = False
    else:
        case["plan"]["registry_digest"] = "0" * 64
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 3
    assert not close.close_path(case["store"], "attempt").exists()


def test_acceptance_symlink_artifact_refused(case):
    assert open_attempt(case) == 0
    data = bundle(case)
    raw = case["inputs"] / data["artifacts"][0]["path"]
    outside = case["inputs"].parent / "outside.json"
    outside.write_bytes(raw.read_bytes())
    raw.unlink()
    try:
        raw.symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit symbolic links")
    assert attach(case) == 2


def test_acceptance_attachment_write_failure_preserves_close(case, monkeypatch):
    assert open_attempt(case) == 0
    bundle(case)
    before = close.load_close(case["store"], "attempt")

    def failed_write(*_):
        raise OSError("simulated retained-store failure")

    monkeypatch.setattr(acceptance, "_retain", failed_write)
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt") == before


def test_acceptance_explicit_partition_assignment_reused(case):
    assert open_attempt(case, "--lens", "acceptance-run-bar", "--allow", "acceptance-run-bar:runner-a") == 0
    accept_all(case)
    rec = close.load_close(case["store"], "attempt")
    assert [lens["id"] for lens in rec["required_lenses"]] == ["acceptance-run-bar", "acceptance-run-tools"]


def test_acceptance_conflicting_partition_assignment_refused(case, capsys):
    assert open_attempt(case, "--lens", "acceptance-run-bar", "--allow", "acceptance-run-bar:runner-b") == 2
    assert "acceptance_policy_invalid" in check(case, capsys)


def test_acceptance_nonregular_input_refused(tmp_path):
    with pytest.raises(acceptance.AcceptanceError, match="regular file"):
        acceptance.prepare(Store(tmp_path / "bus"), tmp_path, tmp_path, "f" * 40, "milestone")
