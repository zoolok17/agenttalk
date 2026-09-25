"""Acceptance 1a through real project Git, retained files and the close CLI."""

from copy import deepcopy
import hashlib
import json
import subprocess

import pytest

from agenttalk import acceptance, acceptance_coverage as coverage, cli, close
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


def accept_all(case, close_id="attempt"):
    for partition in case["plan"]["partitions"]:
        assert command(case, "ack", "--id", close_id, "--lens", "acceptance-run-" + partition["id"],
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
                   "--from", "lead", "--file", str(case["inputs"] / "bundle.json"))


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
    assert open_attempt(case, "--lens", "acceptance-run-bar", "--allow", "acceptance-run-bar:runner-b") == 3
    assert "acceptance_policy_invalid" in capsys.readouterr().err
    assert not close.close_path(case["store"], "attempt").exists()


def test_acceptance_nonregular_input_refused(tmp_path):
    with pytest.raises(acceptance.AcceptanceError, match="regular file"):
        acceptance.prepare(Store(tmp_path / "bus"), tmp_path, tmp_path, "f" * 40, "milestone")


def test_acceptance_old_engine_schema_guard_rejects_route(case):
    assert open_attempt(case) == 0
    record = close.load_close(case["store"], "attempt")
    # The pre-change _is_wellformed rule immediately rejects every version != 1.
    assert record["schema_version"] != 1
    assert close._is_wellformed(record)
    del record["acceptance_route"]
    assert not close._is_wellformed(record)


def test_acceptance_truncated_existing_blob_names_recovery_path(case):
    data = (case["inputs"] / "plan.json").read_bytes()
    path = case["store"].dir / "acceptance" / "sha256" / hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True)
    path.write_bytes(data[:10])
    with pytest.raises(acceptance.AcceptanceError) as error:
        acceptance.prepare(case["store"], case["inputs"] / "plan.json", case["project"],
                           case["sha"], "milestone")
    assert str(path) in str(error.value)
    assert path.read_bytes() == data[:10]


def test_acceptance_interrupted_retention_never_installs_partial_digest(case, monkeypatch):
    data = (case["inputs"] / "plan.json").read_bytes()
    path = case["store"].dir / "acceptance" / "sha256" / hashlib.sha256(data).hexdigest()

    def fail_fsync(_):
        assert not path.exists()
        raise OSError("interrupted before publication")

    with monkeypatch.context() as patch:
        patch.setattr(acceptance.os, "fsync", fail_fsync)
        with pytest.raises(OSError):
            acceptance.prepare(case["store"], case["inputs"] / "plan.json", case["project"],
                               case["sha"], "milestone")
    assert not path.exists()
    assert open_attempt(case) == 0


def test_acceptance_attach_attributes_event_and_advisory_authority(case, capsys, monkeypatch):
    assert open_attempt(case) == 0
    bundle(case)
    monkeypatch.setattr(cli, "_close_lead_set", lambda _: {"lead"})
    monkeypatch.setattr(cli, "_iso_now", lambda: "2026-09-24T14:00:00Z")
    assert command(case, "acceptance", "attach", "--id", "attempt", "--from", "runner-a",
                   "--file", str(case["inputs"] / "bundle.json")) == 0
    assert "not a recognized close lead" in capsys.readouterr().err
    record = close.load_close(case["store"], "attempt")
    route = record["acceptance_route"]
    assert route["attached_by"] == "runner-a"
    assert route["attached_at"] == "2026-09-24T14:00:00Z"
    assert record["events"][-1] == {"event": "acceptance:attach", "by": "runner-a",
                                     "at": route["attached_at"], "bundle_hash": route["bundle_hash"]}


def test_acceptance_runner_dirty_after_run_rejected(case):
    assert open_attempt(case) == 0
    data = bundle(case)
    data["runs"][0]["status_after"] = " M source.txt"
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt")["acceptance_route"]["bundle_hash"] is None


def test_acceptance_observed_boolean_is_not_exit_code(case, capsys):
    case["plan"]["rows"][0]["expected"] = 1
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    bundle(case, build_exit=True)
    assert attach(case) == 0
    assert "acceptance_policy_invalid" in check(case, capsys)


def test_acceptance_raw_other_run_same_partition_holds(case, capsys):
    assert open_attempt(case) == 0
    data = bundle(case)
    extra = dict(data["runs"][0], id="other-run")
    data["runs"].append(extra)
    artifact = data["artifacts"][0]
    raw_path = case["inputs"] / artifact["path"]
    raw = json.loads(raw_path.read_text())
    raw["run_id"] = extra["id"]
    artifact["sha256"] = write_json(raw_path, raw)
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    assert "acceptance_row_unbound" in check(case, capsys)


@pytest.mark.parametrize("malformed", ["value", "json", "missing", "digest"])
def test_acceptance_malformed_informational_keeps_gating_outcome(case, capsys, malformed):
    case["plan"]["rows"][1].update(policy="informational", comparator="exact-failure-set", expected=[])
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    data = bundle(case, build_exit=1, tool_value="not-a-list")
    if malformed == "json":
        raw = case["inputs"] / data["artifacts"][1]["path"]
        raw.write_bytes(b"{")
        data["artifacts"][1]["sha256"] = hashlib.sha256(b"{").hexdigest()
        write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    retained = case["store"].dir / "acceptance" / "sha256" / data["artifacts"][1]["sha256"]
    if malformed == "missing":
        retained.unlink()
    elif malformed == "digest":
        retained.write_bytes(b"corrupt")
    assert "acceptance_row_failed" in check(case, capsys)
    snapshot = acceptance.resolve(case["store"], close.load_close(case["store"], "attempt"))
    assert snapshot["outcomes"][0]["passed"] is False
    assert snapshot["outcomes"][1]["passed"] is None
    assert snapshot["outcomes"][1]["error"]
    if malformed == "value":
        assert snapshot["holds"] == []
    else:
        code = "acceptance_policy_invalid" if malformed == "json" else "acceptance_record_missing"
        assert code in {c for c, _ in snapshot["holds"]}


def test_acceptance_incompatible_lens_does_not_burn_id(case):
    assert open_attempt(case, "--lens", "acceptance-run-bar") == 3
    assert not close.close_path(case["store"], "attempt").exists()
    assert open_attempt(case) == 0


def test_acceptance_single_repo_names_dirty_runtime_paths(case, capsys):
    case["store"] = Store(case["project"])
    case["store"].init(["lead", "runner-a", "runner-b"])
    assert open_attempt(case) == 3
    assert ".agenttalk/config.json" in capsys.readouterr().err
    (case["project"] / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    git(case["project"], "add", ".gitignore")
    git(case["project"], "commit", "-qm", "ignore runtime")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    assert open_attempt(case) == 0


def test_acceptance_observed_failure_overflow_is_row_failure(case, capsys):
    case["plan"]["rows"][0].update(comparator="exact-failure-set", expected=[])
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    bundle(case, build_exit=[f"failure-{i}" for i in range(257)])
    assert attach(case) == 0
    codes = check(case, capsys)
    assert "acceptance_row_failed" in codes
    assert "acceptance_policy_invalid" not in codes


def test_acceptance_freeze_timestamp_is_actual_freeze(case, monkeypatch):
    times = iter(["2026-09-24T14:00:00Z", "2026-09-24T14:00:01Z"])
    monkeypatch.setattr(cli, "_iso_now", lambda: next(times))
    assert open_attempt(case) == 0
    record = close.load_close(case["store"], "attempt")
    assert record["opened_at"] == "2026-09-24T14:00:00Z"
    assert record["acceptance_route"]["frozen_at"] == "2026-09-24T14:00:01Z"


def test_acceptance_hard_link_unavailable_falls_back_atomically(case, monkeypatch):
    original = acceptance.os.link
    def unsupported(source, target, **kwargs):
        if source.name.startswith(".pending-"):
            raise PermissionError("synthetic retained-evidence hard-link refusal")
        return original(source, target, **kwargs)
    monkeypatch.setattr(acceptance.os, "link", unsupported)
    assert open_attempt(case) == 0
    bundle(case)
    assert attach(case) == 0


def test_acceptance_missing_gating_bytes_is_unmeasured_not_failed(case, capsys):
    assert open_attempt(case) == 0
    data = bundle(case)
    assert attach(case) == 0
    (case["store"].dir / "acceptance" / "sha256" / data["artifacts"][0]["sha256"]).unlink()
    codes = check(case, capsys)
    assert "acceptance_record_missing" in codes
    assert "acceptance_row_failed" not in codes


def test_acceptance_attribution_without_bundle_holds(case, capsys):
    assert open_attempt(case) == 0
    with close.close_transaction(case["store"], "attempt") as tx:
        tx.record["acceptance_route"]["attached_by"] = "stray"
        tx.commit()
    assert "acceptance_policy_invalid" in check(case, capsys)


@pytest.fixture
def case_v2(case):
    case["plan"]["schema_version"] = 2
    case["plan"]["project_id"] = acceptance.project_id(acceptance.verify_project(case["project"], case["sha"]))
    write_json(case["inputs"] / "plan.json", case["plan"])
    cfg = case["store"].load_config()
    cfg["operator_identity"] = "operator"
    cfg["roles"] = {"lead": "lead"}
    cfg["agents"].extend(["author", "reproducer"])
    write_json(case["store"].dir / "config.json", cfg)
    return case


def bundle_v2(case, **kwargs):
    data = bundle(case, **kwargs)
    data["schema_version"] = 2
    proof = case["inputs"] / "access.txt"
    proof.write_text("Synthetic seats use separate access; verifier reads retained copies.", encoding="utf-8")
    data["artifacts"].append({"id": "access", "path": "access.txt",
                              "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()})
    data["verifier_access"] = {"id": "verifier", "evidence": "access"}
    data["reproductions"] = []
    for run, row in zip(data["runs"], case["plan"]["rows"], strict=True):
        run["access_id"] = "seat-" + run["actor"]
        rid = "reproduce-" + run["id"]
        raw = {"schema_version": 1, "run_id": rid, "revision": case["sha"], "values": {row["field"]: row["expected"]}}
        aid = "reproduced-" + row["id"]
        digest = write_json(case["inputs"] / (aid + ".json"), raw)
        data["artifacts"].append({"id": aid, "path": aid + ".json", "sha256": digest})
        data["reproductions"].append({"id": rid, "source_run": run["id"], "actor": "reproducer",
                                      "access_id": "reproduction-seat", "access_evidence": "access",
                                      "revision": case["sha"], "head_before": case["sha"], "head_after": case["sha"],
                                      "status_before": "", "status_after": "",
                                      "rows": [{"id": row["id"], "artifact": aid}]})
    if case["plan"]["schema_version"] == 3:
        data["schema_version"] = 3
        hygiene_bundle(case, data)
    write_json(case["inputs"] / "bundle.json", data)
    return data


def hygiene_bundle(case, data):
    """Synthetic cooperative records, never a claim these fixtures executed tools."""
    from agenttalk import acceptance_hygiene as hygiene
    data.setdefault("recovery_approvals", [])
    data["hygiene"] = "hygiene"
    data["artifacts"] = [a for a in data["artifacts"] if not a["id"].startswith("hygiene")]

    def artifact(aid, value):
        value.update(schema_version=1, binding=hygiene.binding(data))
        digest = write_json(case["inputs"] / (aid + ".json"), value)
        data["artifacts"].append({"id": aid, "path": aid + ".json", "sha256": digest})

    for run in data["runs"] + data["reproductions"]:
        run["environment"] = "hygiene-env-" + run["id"]
        run["offline_proof"] = "hygiene-offline-" + run["id"]
        artifact(run["environment"], {"run_id": run["id"], "version_banners": ["synthetic version 1"],
                 "scratch": "isolated scratch", "cache_overlay": "fresh overlay", "service_data": "fresh data",
                 "scratch_isolated": True, "cache_overlay_fresh": True, "service_data_fresh": True,
                 "outputs_outside_checkout": True, "services": []})
        artifact(run["offline_proof"], {"run_id": run["id"], "mode": "external-denial", "egress_denied": True,
                 "owned_loopback_only": True, "positive_control": True, "attempted_fetch": False,
                 "evidence": "synthetic external denial and owned-loopback control log"})
    artifact("hygiene", {"sealed_manifest": hygiene.execution_manifest(data),
             "bundle_digest": hygiene.execution_digest(data), "retained_readable": True, "scratch_removed": True,
             "services_stopped": True, "ports_released": True,
             "confidentiality": {"positive_control": True, "matches": [], "evidence": "synthetic scan log"}})


def snapshot(case):
    return acceptance.resolve(case["store"], close.load_close(case["store"], "attempt"))


def retained_parent(case, result):
    return acceptance.decode(acceptance._retained(case["store"], result["parent"]["record_hash"]))


def publish_hold(case):
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "hold",
                   "--reason", "preserve this attempt") == 3


def successor(case, *, parent="attempt", new_id="next", reduction=None):
    args = ["acceptance", "successor", "--id", new_id, "--parent", parent, "--from", "lead",
            "--acceptance-plan", str(case["inputs"] / "plan.json"), "--project-repo", str(case["project"]),
            "--revision", case["sha"], "--reason", "synthetic amendment"]
    if reduction:
        args.extend(["--scope-reduction", str(reduction)])
    return command(case, *args)


def complete_child(case, child="next", *, build_exit=None):
    # Existing fixture helpers name 'attempt'; redirect only the synthetic input
    # builder's close lookup and CLI ID, leaving persisted identities untouched.
    parent = close.load_close(case["store"], child)
    data = bundle_v2(case, build_exit=case["plan"]["rows"][0]["expected"] if build_exit is None else build_exit)
    data.update(close_id=child, **{k: parent["acceptance_route"][k] for k in
                                ("instance_id", "attempt_id", "project_id", "revision", "plan_hash", "registry_hash")})
    write_json(case["inputs"] / "bundle.json", data)
    assert command(case, "acceptance", "attach", "--id", child, "--from", "lead",
                   "--file", str(case["inputs"] / "bundle.json")) == 0
    return acceptance.resolve(case["store"], close.load_close(case["store"], child))


@pytest.mark.parametrize("missing", [None, "run", "comparison", "original", "reproduced"])
def test_acceptance_cooperative_go_requires_every_comparison_and_run_reproduction(case_v2, capsys, missing):
    case = case_v2
    assert open_attempt(case) == 0
    data = bundle_v2(case, build_exit=1 if missing == "original" else 0)
    if missing in {"run", "comparison"}:
        removed = data["reproductions"][0]["rows"].pop()
        data["artifacts"] = [a for a in data["artifacts"] if a["id"] != removed["artifact"]]
        if missing == "run":
            data["reproductions"].pop(0)
    if missing == "reproduced":
        artifact = next(a for a in data["artifacts"] if a["id"] == "reproduced-build")
        artifact["sha256"] = write_json(case["inputs"] / artifact["path"],
                                       {"schema_version": 1, "run_id": "reproduce-run-build",
                                        "revision": case["sha"], "values": {"exit_code": 1}})
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    accept_all(case)
    codes = check(case, capsys)
    if missing is None:
        assert codes == {"acceptance_cold_missing"}
        assert len(snapshot(case)["reproductions"]) == 2
    elif missing in {"run", "comparison"}:
        assert "acceptance_trust_unresolved" in codes
    else:
        assert "acceptance_row_failed" in codes


@pytest.mark.parametrize("change", ["runner", "verifier", "shared-runner", "shared-verifier", "author"])
def test_acceptance_reproduction_same_actor_or_shared_access_holds(case_v2, capsys, change):
    case = case_v2
    assert open_attempt(case) == 0
    data = bundle_v2(case)
    rep = data["reproductions"][0]
    if change == "runner":
        rep["actor"] = "runner-a"
    elif change == "verifier":
        rep["actor"] = "lead"
    elif change == "author":
        rep["actor"] = "author"
    else:
        rep["access_id"] = "seat-runner-a" if change == "shared-runner" else "verifier"
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    assert "acceptance_trust_unresolved" in check(case, capsys)


@pytest.mark.parametrize("actor", ["runner-a", "author"])
def test_acceptance_verifier_must_not_be_runner_or_author(case_v2, capsys, actor):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case)
    assert command(case, "acceptance", "attach", "--id", "attempt", "--from", actor,
                   "--file", str(case["inputs"] / "bundle.json")) == 0
    assert "acceptance_trust_unresolved" in check(case, capsys)


def test_acceptance_historical_attempt_reads_objects_after_checkout_moves(case_v2, capsys):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    original = close.close_path(case["store"], "attempt").read_bytes()
    (case["project"] / "source.txt").write_text("next", encoding="utf-8")
    git(case["project"], "commit", "-qam", "next revision")
    codes = check(case, capsys)
    assert "acceptance_row_failed" in codes
    assert "acceptance_project_unverified" not in codes
    assert close.close_path(case["store"], "attempt").read_bytes() == original
    assert command(case, "check", "--id", "attempt", "--json") == 3
    assert json.loads(capsys.readouterr().out)["acceptance_evaluation"] == "historical; not GO-publication eligibility"


def test_acceptance_attach_requires_live_clean_candidate(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    (case_v2["project"] / "source.txt").write_text("dirty", encoding="utf-8")
    assert attach(case_v2) == 2


def test_acceptance_project_id_cannot_name_unrelated_repository(case_v2, capsys):
    case_v2["plan"]["project_id"] = "declared-but-unverified"
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert open_attempt(case_v2) == 3
    assert "acceptance_project_unverified" in capsys.readouterr().err


def test_acceptance_successor_preserves_hold(case_v2):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    accept_all(case)
    publish_hold(case)
    before = close.close_path(case["store"], "attempt").read_bytes()
    case["plan"]["rows"][0]["expected"] = 1
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case) == 0
    child = complete_child(case)
    assert "acceptance_plan_stale" in {c for c, _ in child["holds"]}
    assert child["outcomes"][0]["passed"] is None
    assert child["parent"]["verdict"] == "HOLD"
    assert retained_parent(case, child)["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False
    assert close.close_path(case["store"], "attempt").read_bytes() == before
    assert command(case, "publish", "--id", "next", "--from", "lead", "--verdict", "hold") == 3
    # Unapproved same-SHA policy movement must not disappear in a later successor.
    assert successor(case, parent="next", new_id="third") == 0
    third = complete_child(case, "third")
    assert "acceptance_plan_stale" in {c for c, _ in third["holds"]}


def test_acceptance_same_sha_policy_change_stales_acks(case_v2):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case)
    assert attach(case) == 0
    accept_all(case)
    publish_hold(case)
    parent = close.load_close(case["store"], "attempt")
    case["plan"]["plan_id"] = "amended-policy"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case) == 0
    assert close.load_close(case["store"], "next")["lens_acks"] == {}
    complete_child(case)
    with close.close_transaction(case["store"], "next") as tx:
        tx.record["lens_acks"] = parent["lens_acks"]
        tx.commit()
    child = acceptance.resolve(case["store"], close.load_close(case["store"], "next"))
    assert "acceptance_lens_not_independent" in {c for c, _ in child["holds"]}


def test_acceptance_successor_reretains_identical_policy(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    assert successor(case_v2) == 0
    parent = close.load_close(case_v2["store"], "attempt")["acceptance_route"]
    child = close.load_close(case_v2["store"], "next")["acceptance_route"]
    assert child["plan_hash"] == parent["plan_hash"]
    assert child["registry_hash"] == parent["registry_hash"]
    assert child["attempt_id"] != parent["attempt_id"]


def reduction_input(case, *, sender="operator", expires="9999-01-01T00:00:00Z"):
    from agenttalk.acceptance_history import approval_payload
    parent = close.load_close(case["store"], "attempt")
    old_bundle = acceptance.decode(acceptance._retained(case["store"], parent["acceptance_route"]["bundle_hash"]))
    case["plan"]["rows"][0]["policy"] = "informational"
    plan_hash = write_json(case["inputs"] / "plan.json", case["plan"])
    changes = coverage.changes(coverage.history(case["store"], parent), case["plan"])
    reduction = {"rows": list(changes), "changes": changes,
                 "reason": "time-budget dependent metric", "alternatives": ["fixed effort"],
                 "impact": "measurement no longer gates", "owner": "owner", "expires_at": expires,
                 "cause": "measured-variance", "evidence": [old_bundle["artifacts"][0]["sha256"]],
                 "decision_ref": "pending"}
    message = case["store"].send(sender=sender, recipient="lead", kind="message",
                                 body=json.dumps(approval_payload(parent, "next", plan_hash, reduction)),
                                 _allow_reserved_sender=sender == "operator")
    reduction["decision_ref"] = message.id
    path = case["inputs"] / "reduction.json"
    write_json(path, reduction)
    return path


@pytest.mark.parametrize("sender,expires", [("lead", "9999-01-01T00:00:00Z"),
                                           ("operator", "2000-01-01T00:00:00Z")])
def test_acceptance_scope_reduction_lead_only_or_expired_approval_holds(case_v2, sender, expires):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    reduction = reduction_input(case_v2, sender=sender, expires=expires)
    assert successor(case_v2, reduction=reduction) == 2
    assert not close.close_path(case_v2["store"], "next").exists()


def test_acceptance_operator_scope_reduction_preserves_failure_and_reports_reduced_scope(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    reduction = reduction_input(case_v2)
    assert successor(case_v2, reduction=reduction) == 0
    child = complete_child(case_v2)
    assert child["report_label"] == "reduced scope"
    assert child["outcomes"][0]["passed"] is None
    assert child["outcomes"][0]["original_outcome"]["passed"] is False
    assert "acceptance_scope_reduction_unapproved" not in {c for c, _ in child["holds"]}
    assert retained_parent(case_v2, child)["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False


def test_acceptance_unapproved_scope_reduction_remains_hold(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    case_v2["plan"]["rows"][0]["policy"] = "informational"
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert successor(case_v2) == 0
    child = complete_child(case_v2)
    assert "acceptance_scope_reduction_unapproved" in {c for c, _ in child["holds"]}


def test_acceptance_gate_label_and_waiver_cannot_clear(case_v2, capsys):
    from agenttalk import gates
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    accept_all(case_v2)
    gates.set_gate(case_v2["store"].root, name="acceptance", status="green", severity="blocker",
                   scope="milestone", actor="lead", evidence_source="automation_ci", evidence=["claimed-green"])
    assert "acceptance_row_failed" in check(case_v2, capsys)
    gates.waive_gate(case_v2["store"].root, name="acceptance", operator="operator", reason="synthetic waiver",
                     scope="milestone", expires="9999-01-01T00:00:00Z")
    assert "acceptance_row_failed" in check(case_v2, capsys)


def test_acceptance_reopen_creates_linked_successor(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    assert command(case_v2, "reopen", "--id", "attempt", "--successor", "next", "--from", "lead",
                   "--acceptance-plan", str(case_v2["inputs"] / "plan.json"),
                   "--project-repo", str(case_v2["project"]), "--revision", case_v2["sha"],
                   "--reason", "repeat with retained history") == 0
    assert close.load_close(case_v2["store"], "attempt")["status"] == close.PUBLISHED
    assert close.load_close(case_v2["store"], "next")["acceptance_route"]["parent_record_hash"]


def test_acceptance_scope_approval_expiry_survives_successor_chain(case_v2, monkeypatch):
    from datetime import datetime, timezone
    from agenttalk import acceptance_history as history
    monkeypatch.setattr(history, "_now", lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    reduction = reduction_input(case_v2, expires="2027-01-01T00:00:00Z")
    assert successor(case_v2, reduction=reduction) == 0
    complete_child(case_v2)
    assert command(case_v2, "publish", "--id", "next", "--from", "lead", "--verdict", "hold") == 3
    assert successor(case_v2, parent="next", new_id="third") == 0
    third = complete_child(case_v2, "third")
    assert third["report_label"] == "reduced scope"
    assert third["outcomes"][0]["passed"] is None
    monkeypatch.setattr(history, "_now", lambda: datetime(2028, 1, 1, tzinfo=timezone.utc))
    result = acceptance.resolve(case_v2["store"], close.load_close(case_v2["store"], "third"))
    assert "acceptance_scope_reduction_unapproved" in {c for c, _ in result["holds"]}


def test_acceptance_failed_trust_keeps_gating_comparison(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    data = bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    proof = next(a for a in data["artifacts"] if a["id"] == "access")
    (case_v2["store"].dir / "acceptance" / "sha256" / proof["sha256"]).unlink()
    assert "acceptance_row_failed" in check(case_v2, capsys)


def test_acceptance_unrecognized_verifier_cannot_satisfy_trust(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert command(case_v2, "acceptance", "attach", "--id", "attempt", "--from", "cold",
                   "--file", str(case_v2["inputs"] / "bundle.json")) == 0
    assert "acceptance_trust_unresolved" in check(case_v2, capsys)


def test_acceptance_successor_keeps_parent_counter_obligation(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    assert command(case_v2, "ack", "--id", "attempt", "--lens", "acceptance-run-bar", "--from", "runner-a",
                   "--status", "counter", "--counter", "unfixed", "--finding", "still broken") == 0
    publish_hold(case_v2)
    assert successor(case_v2) == 0
    child = close.load_close(case_v2["store"], "next")
    assert child["counters"]["unfixed"]["decision"] == close.COUNTER_PENDING
    result = complete_child(case_v2)
    assert "acceptance_residual_open" in {c for c, _ in result["holds"]}


def test_acceptance_new_revision_successor_preserves_historical_failure(case_v2):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    (case["project"] / "source.txt").write_text("fixed revision", encoding="utf-8")
    git(case["project"], "commit", "-qam", "synthetic fix")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    assert successor(case) == 0
    child = complete_child(case)
    assert child["outcomes"][0]["passed"] is True
    assert retained_parent(case, child)["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False
    assert "acceptance_project_unverified" not in {code for code, _ in child["holds"]}


@pytest.mark.parametrize("mode", ["na", "override"])
def test_acceptance_na_and_override_cannot_satisfy_partition(case_v2, capsys, mode):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    accept_all(case_v2)
    if mode == "na":
        assert command(case_v2, "ack", "--id", "attempt", "--lens", "acceptance-run-bar", "--from", "runner-a",
                       "--status", "na", "--reason", "synthetic skip") == 0
    else:
        assert command(case_v2, "ack", "--id", "attempt", "--lens", "acceptance-run-bar", "--from", "lead",
                       "--status", "accept", "--override", "--risk-class", "quality", "--release-blocker", "no",
                       "--tests-referenced", "synthetic", "--tests-executed", "synthetic", "--evidence", "claim",
                       "--residual-risk", "staged") == 0
    assert "acceptance_lens_not_independent" in check(case_v2, capsys)


def test_acceptance_same_sha_typed_expected_change_holds(case_v2):
    case_v2["plan"]["rows"][0]["comparator"] = "exact-value"
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    case_v2["plan"]["rows"][0]["expected"] = False
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert successor(case_v2) == 0
    child = complete_child(case_v2)
    assert "acceptance_plan_stale" in {c for c, _ in child["holds"]}


@pytest.mark.parametrize("field,value", [("successor_close_id", "another"), ("plan_hash", "f" * 64),
                                        ("parent_attempt_id", "another"), ("schema_version", True)])
def test_acceptance_scope_approval_exact_binding_required(case_v2, field, value):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    path = reduction_input(case_v2)
    reduction = json.loads(path.read_text())
    msg = json.loads((case_v2["store"].messages_dir / (reduction["decision_ref"] + ".json")).read_text())
    body = json.loads(msg["body"])
    body[field] = value
    other = case_v2["store"].send(sender="operator", recipient="lead", kind="message", body=json.dumps(body),
                                 _allow_reserved_sender=True)
    reduction["decision_ref"] = other.id
    write_json(path, reduction)
    assert successor(case_v2, reduction=path) == 2
    assert not close.close_path(case_v2["store"], "next").exists()


def test_acceptance_go_publish_requires_live_candidate(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    accept_all(case_v2)
    (case_v2["project"] / "source.txt").write_text("dirty", encoding="utf-8")
    capsys.readouterr()
    assert command(case_v2, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3
    output = capsys.readouterr()
    assert "acceptance_project_unverified" in output.out + output.err


def test_acceptance_malformed_route_ack_still_holds(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    with close.close_transaction(case_v2["store"], "attempt") as tx:
        tx.record["acceptance_route"] = "malformed"
        tx.commit()
    accept_all(case_v2)
    assert "acceptance_policy_invalid" in check(case_v2, capsys)


def test_acceptance_reopen_flags_require_successor(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    before = close.close_path(case_v2["store"], "attempt").read_bytes()
    assert command(case_v2, "reopen", "--id", "attempt", "--from", "lead",
                   "--acceptance-plan", str(case_v2["inputs"] / "plan.json")) == 2
    assert "require --successor" in capsys.readouterr().err
    assert close.close_path(case_v2["store"], "attempt").read_bytes() == before


@pytest.mark.parametrize("new_revision", [False, True])
def test_acceptance_gating_amendment_requires_operator_at_any_revision(case_v2, new_revision):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    if new_revision:
        (case["project"] / "source.txt").write_text("another revision", encoding="utf-8")
        git(case["project"], "commit", "-qam", "synthetic change")
        case["sha"] = git(case["project"], "rev-parse", "HEAD")
    case["plan"]["rows"][0]["expected"] = 1
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case) == 0
    complete_child(case)
    accept_all(case, "next")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "next"))
    assert "acceptance_category_moved_unreviewed" in {code for code, _ in result["holds"]}
    # This is a direct policy hold, independent of the staged cold-review hold.
    assert result["outcomes"][0]["passed"] is None
    assert result["outcomes"][0]["original_outcome"]["passed"] is False
    # A later source revision cannot launder an earlier unapproved amendment.
    assert command(case, "publish", "--id", "next", "--from", "lead", "--verdict", "hold") == 3
    (case["project"] / "source.txt").write_text("third revision", encoding="utf-8")
    git(case["project"], "commit", "-qam", "synthetic follow-up")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    assert successor(case, parent="next", new_id="third") == 0
    third = complete_child(case, "third")
    assert "acceptance_category_moved_unreviewed" in {code for code, _ in third["holds"]}
    assert third["outcomes"][0]["passed"] is None
    assert third["outcomes"][0]["original_outcome"]["passed"] is False


@pytest.mark.parametrize("sender,tamper", [("operator", None), ("lead", None), ("operator", "old"),
                                         ("operator", "new"), ("operator", "expired"),
                                         ("operator", "boolean-body"), ("operator", "float-body"),
                                         ("operator", "child-failure"), ("operator", "descendant-failure"),
                                         ("operator", "rows"), ("operator", "retained-diff")])
def test_acceptance_exact_operator_policy_amendment_preserves_failure(case_v2, sender, tamper):
    from agenttalk import acceptance_history as history
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    parent = close.load_close(case["store"], "attempt")
    (case["project"] / "source.txt").write_text("next revision", encoding="utf-8")
    git(case["project"], "commit", "-qam", "synthetic change")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    case["plan"]["rows"][0]["expected"] = 1
    plan_hash = write_json(case["inputs"] / "plan.json", case["plan"])
    changes = coverage.changes(coverage.history(case["store"], parent), case["plan"])
    target = coverage.target_id(case["plan"]["rows"][0])
    change = {"rows": list(changes), "reason": "correct the assertion", "alternatives": ["retain old assertion"],
              "impact": "policy changed, original failure remains", "owner": "owner",
              "expires_at": "2000-01-01T00:00:00Z" if tamper == "expired" else "9999-01-01T00:00:00Z",
              "cause": "policy-amendment", "evidence": [parent["acceptance_route"]["bundle_hash"]],
              "decision_ref": "pending", "changes": deepcopy(changes)}
    if tamper in {"old", "new"}:
        change["changes"][target][tamper][0]["expected"] = 9
    if tamper == "rows":
        change["rows"] = ["tool"]
    body = deepcopy(history.approval_payload(parent, "next", plan_hash, change))
    if tamper in {"boolean-body", "float-body"}:
        body["reduction"]["changes"][target]["new"][0]["expected"] = True if tamper == "boolean-body" else 1.0
    message = case["store"].send(sender=sender, recipient="lead", kind="message",
                                 body=json.dumps(body),
                                 _allow_reserved_sender=sender == "operator")
    change["decision_ref"] = message.id
    path = case["inputs"] / "amendment.json"
    write_json(path, change)
    rc = successor(case, reduction=path)
    if sender != "operator" or tamper in {"expired", "boolean-body", "float-body"}:
        assert rc == 2
        return
    assert rc == 0
    if tamper == "retained-diff":
        with close.close_transaction(case["store"], "next") as tx:
            route = tx.record["acceptance_route"]
            amendment = acceptance.decode(acceptance._retained(case["store"], route["amendment_hash"]))
            amendment["assertion_changes"] = {}
            route["amendment_hash"] = acceptance._retain(case["store"], json.dumps(amendment).encode())
            tx.commit()
    result = complete_child(case, build_exit=9 if tamper == "child-failure" else 1)
    if tamper == "retained-diff":
        assert "acceptance_category_moved_unreviewed" in {code for code, _ in result["holds"]}
        return
    if tamper == "descendant-failure":
        assert command(case, "publish", "--id", "next", "--from", "lead", "--verdict", "hold") == 3
        assert successor(case, parent="next", new_id="third") == 0
        result = complete_child(case, "third", build_exit=9)
    codes = {code for code, _ in result["holds"]}
    assert ("acceptance_category_moved_unreviewed" in codes) == (tamper in {"old", "new", "rows", "descendant-failure"})
    assert result["report_label"] == "policy amended"
    assert result["outcomes"][0]["passed"] is None
    failing = tamper in {"child-failure", "descendant-failure"}
    assert result["outcomes"][0]["comparison_passed"] is not failing
    if failing:
        assert "acceptance_row_failed" in codes
    assert result["outcomes"][0]["original_outcome"]["passed"] is False
    # Expiry remains an evaluation requirement after the approval was retained.
    if tamper is None:
        from datetime import datetime, timezone
        from unittest.mock import patch
        with patch.object(history, "_now", return_value=datetime.max.replace(tzinfo=timezone.utc)):
            expired = acceptance.resolve(case["store"], close.load_close(case["store"], "next"))
        assert "acceptance_category_moved_unreviewed" in {code for code, _ in expired["holds"]}


@pytest.mark.parametrize("field", ["authors", "runners"])
def test_acceptance_successor_cannot_shrink_independence_lists(case_v2, field):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    if field == "authors":
        case_v2["plan"]["authors"] = ["another-author"]
    else:
        case_v2["plan"]["partitions"][0]["agents"] = ["runner-b"]
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert successor(case_v2) == 0
    result = complete_child(case_v2)
    assert any(code == "acceptance_lens_not_independent" and "successor" in detail
               for code, detail in result["holds"])


def test_acceptance_pre_attachment_accepts_are_stale(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    accept_all(case_v2)
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    assert "acceptance_lens_not_independent" in check(case_v2, capsys)
    accept_all(case_v2)
    assert check(case_v2, capsys) == {"acceptance_cold_missing"}


def test_acceptance_live_check_matches_go_publish(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    accept_all(case_v2)
    (case_v2["project"] / "source.txt").write_text("dirty", encoding="utf-8")
    assert "acceptance_project_unverified" in check(case_v2, capsys)
    assert command(case_v2, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3
    assert "acceptance_project_unverified" in capsys.readouterr().out


def test_acceptance_parent_snapshot_is_digest_reference(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    assert successor(case_v2) == 0
    child = complete_child(case_v2)
    assert set(child["parent"]) == {"close_id", "record_hash", "verdict"}
    assert retained_parent(case_v2, child)["final"]["verdict"] == "HOLD"


def test_acceptance_duplicate_reproduction_holds(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    data = bundle_v2(case_v2)
    duplicate = deepcopy(data["reproductions"][0])
    duplicate["id"] = "second-reproduction"
    data["reproductions"].append(duplicate)
    write_json(case_v2["inputs"] / "bundle.json", data)
    assert attach(case_v2) == 0
    assert "acceptance_trust_unresolved" in check(case_v2, capsys)


def test_acceptance_reproduction_dirty_after_holds(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    data = bundle_v2(case_v2)
    data["reproductions"][0]["status_after"] = " M source.txt"
    write_json(case_v2["inputs"] / "bundle.json", data)
    assert attach(case_v2) == 0
    assert "acceptance_project_unverified" in check(case_v2, capsys)


def test_acceptance_allowed_runner_override_holds(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    accept_all(case_v2)
    # The CLI ignores --override from a non-lead, even an allowed runner.
    # Exercise a recorded override through the state transition used by writers.
    with close.close_transaction(case_v2["store"], "attempt") as tx:
        previous = tx.record["lens_acks"]["acceptance-run-bar"]
        close.apply_ack(tx.record, lens_id="acceptance-run-bar", agent="runner-a", status="accept",
                        from_role=None, at=previous["at"], evidence=previous["evidence"], override=True)
        tx.commit()
    assert "acceptance_lens_not_independent" in check(case_v2, capsys)


def test_acceptance_reproduction_cannot_reuse_original_run_id(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    data = bundle_v2(case_v2)
    data["reproductions"][0]["id"] = data["runs"][0]["id"]
    write_json(case_v2["inputs"] / "bundle.json", data)
    assert attach(case_v2) == 2
    assert "reproduction run reference is invalid" in capsys.readouterr().err


def test_acceptance_operator_message_embedded_id_must_match(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    path = reduction_input(case_v2)
    decision = json.loads(path.read_text())["decision_ref"]
    message_path = case_v2["store"].messages_dir / (decision + ".json")
    message = json.loads(message_path.read_text())
    message["id"] = "different-id"
    write_json(message_path, message)
    assert successor(case_v2, reduction=path) == 2


def test_acceptance_resolver_rechecks_derived_project_id(case_v2):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    record = close.load_close(case_v2["store"], "attempt")
    route = record["acceptance_route"]
    plan = deepcopy(case_v2["plan"])
    plan["project_id"] = "foreign-project"
    route["project_id"] = plan["project_id"]
    route["plan_hash"] = acceptance._retain(case_v2["store"], json.dumps(plan).encode())
    result = acceptance.resolve(case_v2["store"], record)
    assert "acceptance_project_unverified" in {code for code, _ in result["holds"]}


def test_acceptance_ancestry_depth_cap_holds(case_v2):
    from agenttalk import acceptance_history as history
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    assert successor(case_v2) == 0
    complete_child(case_v2)
    child = close.load_close(case_v2["store"], "next")
    # Exercise the boundary with real retained parent/policy bytes.
    history.evaluate(case_v2["store"], child, case_v2["plan"], {"holds": [], "outcomes": []}, depth=31)
    with pytest.raises(acceptance.AcceptanceError, match="ancestry exceeds"):
        history.evaluate(case_v2["store"], child, case_v2["plan"], {"holds": [], "outcomes": []}, depth=32)


@pytest.mark.parametrize("expected,approved", [(0, False), (1, False), (1, True)])
@pytest.mark.parametrize("informational_hop", [False, True])
def test_acceptance_regating_compares_last_gating_ancestor(case_v2, expected, approved, informational_hop):
    case = case_v2
    original_row = deepcopy(case["plan"]["rows"][0])
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    reduction = reduction_input(case)
    assert successor(case, reduction=reduction) == 0
    complete_child(case)
    assert command(case, "publish", "--id", "next", "--from", "lead", "--verdict", "hold") == 3
    parent = "next"
    if informational_hop:
        # Informational rows may evolve; the immediate parent is not the baseline.
        case["plan"]["rows"][0]["expected"] = 1
        write_json(case["inputs"] / "plan.json", case["plan"])
        (case["project"] / "source.txt").write_text("informational revision", encoding="utf-8")
        git(case["project"], "commit", "-qam", "synthetic informational change")
        case["sha"] = git(case["project"], "rev-parse", "HEAD")
        renewal = target_approval(case, parent, "info")
        assert successor(case, parent=parent, new_id="info", reduction=renewal) == 0
        complete_child(case, "info")
        assert command(case, "publish", "--id", "info", "--from", "lead", "--verdict", "hold") == 3
        parent = "info"
    case["plan"]["rows"][0].update(policy="gating", expected=expected)
    plan_hash = write_json(case["inputs"] / "plan.json", case["plan"])
    (case["project"] / "source.txt").write_text("regated revision", encoding="utf-8")
    git(case["project"], "commit", "-qam", "synthetic regate")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    approval_file = None
    if approved:
        from agenttalk import acceptance_history as history
        parent_record = close.load_close(case["store"], parent)
        changes = coverage.changes(coverage.group([original_row]), case["plan"])
        change = {"rows": list(changes), "reason": "reviewed regating policy", "alternatives": ["restore baseline"],
                  "impact": "amended assertion", "owner": "owner", "expires_at": "9999-01-01T00:00:00Z",
                  "cause": "policy-amendment", "evidence": [parent_record["acceptance_route"]["bundle_hash"]],
                  "decision_ref": "pending",
                  "changes": changes}
        message = case["store"].send(sender="operator", recipient="lead", kind="message",
                                     body=json.dumps(
                                         history.approval_payload(parent_record, "regated", plan_hash, change)),
                                     _allow_reserved_sender=True)
        change["decision_ref"] = message.id
        approval_file = case["inputs"] / "regating-approval.json"
        write_json(approval_file, change)
    assert successor(case, parent=parent, new_id="regated", reduction=approval_file) == 0
    complete_child(case, "regated")
    accept_all(case, "regated")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "regated"))
    codes = {code for code, _ in result["holds"]}
    if expected == 1:
        assert ("acceptance_category_moved_unreviewed" in codes) is not approved
        assert result["outcomes"][0]["passed"] is None
        assert result["outcomes"][0]["original_outcome"]["passed"] is False
    else:
        assert "acceptance_category_moved_unreviewed" not in codes
        assert "acceptance_plan_stale" not in codes
        assert result["outcomes"][0]["passed"] is True


def test_acceptance_never_gating_row_can_enter_without_amendment(case_v2):
    case_v2["plan"]["rows"][0]["policy"] = "informational"
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2, build_exit=1)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    case_v2["plan"]["rows"][0].update(policy="gating", expected=1)
    write_json(case_v2["inputs"] / "plan.json", case_v2["plan"])
    assert successor(case_v2) == 0
    result = complete_child(case_v2)
    assert "acceptance_category_moved_unreviewed" not in {code for code, _ in result["holds"]}
    assert "acceptance_plan_stale" not in {code for code, _ in result["holds"]}
    assert result["outcomes"][0]["passed"] is True


def attach_current_plan(case, close_id):
    record = close.load_close(case["store"], close_id)
    route = record["acceptance_route"]
    data = {key: route[key] for key in ("instance_id", "attempt_id", "project_id", "revision",
                                       "plan_hash", "registry_hash")}
    data.update(schema_version=2, close_id=close_id, runs=[], rows=[], artifacts=[], reproductions=[],
                verifier_access={"id": "verifier", "evidence": "access"})
    proof = case["inputs"] / "access.txt"
    proof.write_text("Synthetic separate access evidence", encoding="utf-8")
    data["artifacts"].append({"id": "access", "path": proof.name,
                              "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()})
    for partition in case["plan"]["partitions"]:
        rows = [r for r in case["plan"]["rows"] if r["partition"] == partition["id"]]
        run_id = "run-" + partition["id"]
        data["runs"].append({"id": run_id, "partition": partition["id"], "actor": partition["agents"][0],
                              "access_id": "seat-" + partition["id"], "revision": case["sha"],
                              "head_before": case["sha"], "head_after": case["sha"],
                              "status_before": "", "status_after": ""})
        rep_id = "reproduce-" + run_id
        data["reproductions"].append({"id": rep_id, "source_run": run_id, "actor": "reproducer",
                                      "access_id": "reproduction-seat", "access_evidence": "access",
                                      "revision": case["sha"], "head_before": case["sha"], "head_after": case["sha"],
                                      "status_before": "", "status_after": "",
                                      "rows": [{"id": r["id"], "artifact": "reproduced-" + r["artifact"]}
                                               for r in rows]})
        values = {}
        for row in rows:
            values.setdefault(row["artifact"], {}).setdefault(row["field"], row["expected"])
            data["rows"].append({"id": row["id"], "run_id": run_id})
        for aid, observed in values.items():
            for prefix, rid in (("", run_id), ("reproduced-", rep_id)):
                name = prefix + aid
                digest = write_json(case["inputs"] / (name + ".json"),
                                    {"schema_version": 1, "run_id": rid, "revision": case["sha"], "values": observed})
                data["artifacts"].append({"id": name, "path": name + ".json", "sha256": digest})
    write_json(case["inputs"] / "bundle.json", data)
    assert command(case, "acceptance", "attach", "--id", close_id, "--from", "lead",
                   "--file", str(case["inputs"] / "bundle.json")) == 0


@pytest.mark.parametrize("reshape,hold", [
    ("rename", False), ("rename-weaken", True), ("split-equivalent", False),
    ("split-strengthen", False), ("split-targets", True), ("merge-equivalent", False),
    ("merge-drop-conjunct", True), ("merge-targets", True), ("move-partition", True),
    ("move-artifact", True), ("move-field", True), ("comparator-equivalent", False),
    ("comparator-strengthen", False), ("comparator-weaken", True),
    ("informational-rename-weaken", True), ("informational-restore", False),
    ("delete", True), ("informational-only", True), ("new-target", False),
])
def test_acceptance_target_coverage_reshape_table(case_v2, reshape, hold):
    from agenttalk import acceptance_history as history
    case = case_v2
    build = case["plan"]["rows"][0]
    if reshape in {"comparator-strengthen", "comparator-weaken"}:
        build.update(comparator="exact-failure-set" if reshape == "comparator-strengthen" else "exact-value",
                     field="failures", expected=["synthetic-failure"])
    if reshape in {"merge-equivalent", "merge-drop-conjunct"}:
        case["plan"]["rows"].append(dict(build, id="duplicate", expected=0 if reshape == "merge-equivalent" else 1))
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    attach_current_plan(case, "attempt")
    publish_hold(case)
    parent = "attempt"
    if reshape.startswith("informational-") and reshape != "informational-only":
        reduction = reduction_input(case)
        assert successor(case, reduction=reduction) == 0
        attach_current_plan(case, "next")
        assert command(case, "publish", "--id", "next", "--from", "lead", "--verdict", "hold") == 3
        parent = "next"
    if reshape in {"rename", "rename-weaken", "informational-rename-weaken"}:
        build.update(id="renamed", policy="gating", expected=1 if hold else 0)
    elif reshape in {"split-equivalent", "split-strengthen"}:
        case["plan"]["rows"].append(dict(build, id="extra", expected=1 if reshape == "split-strengthen" else 0))
    elif reshape == "split-targets":
        build["artifact"] = "split-one"
        case["plan"]["rows"].append(dict(build, id="extra", artifact="split-two"))
    elif reshape.startswith("merge-"):
        case["plan"]["rows"] = [r for r in case["plan"]["rows"] if r["id"] != "duplicate"]
        if reshape == "merge-targets":
            case["plan"]["rows"][1]["policy"] = "informational"
    elif reshape == "move-partition":
        case["plan"]["rows"].append(dict(build, id="placeholder", artifact="placeholder", policy="informational"))
        build["partition"] = "tools"
    elif reshape == "move-artifact":
        build["artifact"] = "different-artifact"
    elif reshape == "move-field":
        build.update(field="different_field", comparator="exact-value")
    elif reshape.startswith("comparator-"):
        build["comparator"] = "exact-failure-set" if reshape == "comparator-weaken" else "exact-value"
    elif reshape == "informational-restore":
        build["policy"] = "gating"
    elif reshape == "delete":
        case["plan"]["rows"] = [r for r in case["plan"]["rows"] if r["id"] != "build"]
        case["plan"]["rows"].append(dict(build, id="placeholder", artifact="placeholder", policy="informational"))
    elif reshape == "informational-only":
        build["policy"] = "informational"
    elif reshape == "new-target":
        case["plan"]["rows"].append(dict(build, id="extra", artifact="extra"))
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case, parent=parent, new_id="reshaped") == 0
    record = close.load_close(case["store"], "reshaped")
    result = {"holds": [], "outcomes": [{"id": r["id"], "policy": r["policy"], "passed": True}
                                          for r in case["plan"]["rows"]]}
    # The policy evaluator is independent of run/reproduction readiness; the real
    # parent store and retained plans exercise its ancestry and approval inputs.
    history.evaluate(case["store"], record, case["plan"], result)
    codes = {code for code, _ in result["holds"]}
    assert ("acceptance_category_moved_unreviewed" in codes) == hold
    if not hold:
        assert not codes


def target_approval(case, parent_id, child_id, *, cause="policy-amendment"):
    from agenttalk import acceptance_history as history
    parent = close.load_close(case["store"], parent_id)
    changes = coverage.changes(coverage.history(case["store"], parent), case["plan"])
    approval = {"rows": list(changes), "changes": changes, "reason": "reviewed coverage change",
                "alternatives": ["restore all coverage"], "impact": "reduced protection", "owner": "owner",
                "expires_at": "9999-01-01T00:00:00Z", "cause": cause,
                "evidence": [parent["acceptance_route"]["bundle_hash"]], "decision_ref": "pending"}
    plan_hash = write_json(case["inputs"] / "plan.json", case["plan"])
    message = case["store"].send(sender="operator", recipient="lead", kind="message",
                                 body=json.dumps(history.approval_payload(parent, child_id, plan_hash, approval)),
                                 _allow_reserved_sender=True)
    approval["decision_ref"] = message.id
    path = case["inputs"] / (child_id + "-approval.json")
    write_json(path, approval)
    return path


def test_acceptance_approved_target_deletion_and_renamed_return_keep_ancestor_failure(case_v2):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    original = deepcopy(case["plan"]["rows"][0])
    case["plan"]["rows"][0].update(id="placeholder", artifact="placeholder", policy="informational")
    approval = target_approval(case, "attempt", "deleted")
    assert successor(case, new_id="deleted", reduction=approval) == 0
    attach_current_plan(case, "deleted")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "deleted"))
    key = coverage.target_id(original)
    assert result["coverage_changes"][key]["new"] == []
    assert result["coverage_changes"][key]["approved"] is True
    assert result["coverage_changes"][key]["sources"][0]["outcome"]["passed"] is False
    assert result["report_label"] == "reduced scope"
    assert "acceptance_category_moved_unreviewed" not in {c for c, _ in result["holds"]}
    assert command(case, "publish", "--id", "deleted", "--from", "lead", "--verdict", "hold") == 3
    case["plan"]["rows"][0] = dict(original, id="renamed", expected=1)
    approval = target_approval(case, "deleted", "returned")
    assert successor(case, parent="deleted", new_id="returned", reduction=approval) == 0
    attach_current_plan(case, "returned")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "returned"))
    assert "acceptance_category_moved_unreviewed" not in {c for c, _ in result["holds"]}
    assert "acceptance_record_missing" not in {c for c, _ in result["holds"]}
    assert result["outcomes"][0]["passed"] is None
    assert result["outcomes"][0]["original_outcome"]["passed"] is False


def test_acceptance_target_history_depth_is_bounded_before_child_creation(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    record = close.load_close(case_v2["store"], "attempt")
    # Isolate the retained-parent walk with real validated plans and digests;
    # deeper amendment validation must not run before the depth refusal.
    for _ in range(32):
        digest = acceptance._retain(case_v2["store"], json.dumps(record).encode())
        record["acceptance_route"].update(parent_record_hash=digest, amendment_hash=digest)
    assert coverage.history(case_v2["store"], record)
    excessive = json.loads(json.dumps(record))
    digest = acceptance._retain(case_v2["store"], json.dumps(record).encode())
    excessive["acceptance_route"].update(parent_record_hash=digest, amendment_hash=digest)
    with pytest.raises(acceptance.AcceptanceError, match="ancestry exceeds"):
        coverage.history(case_v2["store"], excessive)
    write_json(close.close_path(case_v2["store"], "attempt"), record)
    assert successor(case_v2) == 2
    assert "ancestry exceeds" in capsys.readouterr().err
    assert not close.close_path(case_v2["store"], "next").exists()


def test_acceptance_strongest_target_coverage_survives_approved_weakening(case_v2):
    case = case_v2
    case["plan"]["rows"][0].update(comparator="exact-value", field="failures", expected=["a", "b"])
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    attach_current_plan(case, "attempt")
    publish_hold(case)
    case["plan"]["rows"][0]["comparator"] = "exact-failure-set"
    approval = target_approval(case, "attempt", "weaker")
    assert successor(case, new_id="weaker", reduction=approval) == 0
    attach_current_plan(case, "weaker")
    assert command(case, "publish", "--id", "weaker", "--from", "lead", "--verdict", "hold") == 3
    # Returning to the strongest historical predicate is free; merely renaming
    # the approved weaker predicate still needs this attempt's exact approval.
    parent = close.load_close(case["store"], "weaker")
    protected = coverage.history(case["store"], parent)
    assert coverage.changes(protected, case["plan"])
    case["plan"]["rows"][0].update(id="restored", comparator="exact-value")
    assert not coverage.changes(protected, case["plan"])


def test_acceptance_duplicate_list_is_not_stronger_failure_set_coverage():
    # The raw failure-set comparator rejects duplicate IDs, whereas exact-value
    # accepts this exact list; implication must not silently ignore that rule.
    candidate = {"kind": "exact-json", "expected": ["a", "a"]}
    obligation = {"kind": "failure-set", "expected": ["a"]}
    assert not coverage.implies(candidate, obligation)


@pytest.mark.parametrize("cause", ["unavailable-tool", "measured-variance"])
def test_acceptance_reduction_cannot_authorize_replacement_coverage(case_v2, cause):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    case["plan"]["rows"][0]["expected"] = 1
    approval = target_approval(case, "attempt", "next", cause=cause)
    assert successor(case, reduction=approval) == 0
    result = complete_child(case)
    assert "acceptance_category_moved_unreviewed" in {c for c, _ in result["holds"]}


@pytest.fixture
def case_v3(case_v2):
    case = case_v2
    base = case["sha"]
    (case["project"] / "source.txt").write_text("synthetic candidate\n", encoding="utf-8")
    git(case["project"], "commit", "-qam", "candidate from verified base")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    case["plan"]["schema_version"] = 3
    case["plan"]["cold_policy"] = {
        "reviewer": "cold", "absence_disclosure": "", "change_base": base,
        "roster": [{"actor": actor, "vendor": vendor} for actor, vendor in
                   [("lead", "alpha"), ("author", "alpha"), ("runner-a", "alpha"),
                    ("runner-b", "alpha"), ("reproducer", "beta"), ("cold", "beta")]]}
    write_json(case["inputs"] / "plan.json", case["plan"])
    return case


def cold_phase(case, phase, close_id="attempt", *, change=None, actor="cold"):
    from agenttalk import acceptance_cold
    record = close.load_close(case["store"], close_id)
    route = record["acceptance_route"]
    if phase == "commit":
        manifest = []
        for kind, contents in [("source", (case["project"] / "source.txt").read_bytes()),
                               ("access", b"separate cold seat access")]:
            resource = case["inputs"] / ("cold-" + kind + ".txt")
            resource.write_bytes(contents)
            manifest.append({"kind": kind, "path": resource.name, "sha256": hashlib.sha256(contents).hexdigest()})
        data = {"schema_version": 1, "binding": acceptance_cold.binding(record),
                "reviewer": case["plan"]["cold_policy"]["reviewer"], "context_id": "fresh-context",
                "claims_exposed": False, "authored_in_scope": False,
                "prior_exposure": False, "leakage_reviewed": True,
                "access_id": "cold-seat", "access_evidence": manifest[1]["sha256"],
                "delivery_manifest": manifest, "observations": [], "blind_spots": []}
    else:
        data = {"schema_version": 1, "commit_hash": route["cold_commit_hash"],
                "bundle_hash": route["bundle_hash"], "revealed": True, "findings": []}
    if callable(change):
        change(data)
    elif change:
        data.update(change)
    if phase == "reconcile":
        from agenttalk import acceptance_hygiene
        if route["bundle_hash"] and route["cold_commit_hash"]:
            manifest, digest = acceptance_hygiene.final_manifest(case["store"], route, data)
        else:
            # Deliberately premature reports must reach the CLI ordering guard.
            manifest, digest = [], "0" * 64
        data["closeout"] = {"sealed_manifest": manifest, "report_digest": digest,
                            "confidentiality": {"positive_control": True, "matches": [],
                                                "evidence": "synthetic final record sweep"}}
    path = case["inputs"] / (phase + ".json")
    write_json(path, data)
    return command(case, "acceptance", "cold", "--id", close_id, "--phase", phase,
                   "--file", str(path), "--from", actor)


def ack_lens(case, lens, actor, close_id="attempt", *extra):
    return command(case, "ack", "--id", close_id, "--lens", lens, "--from", actor,
                   "--status", "accept", "--risk-class", "quality", "--release-blocker", "no",
                   "--tests-referenced", "synthetic", "--tests-executed", "synthetic",
                   "--residual-risk", "cooperative declarations", "--evidence", "retained evidence", *extra)


def final_accepts(case, data, close_id="attempt"):
    accept_all(case, close_id)
    for rep in data["reproductions"]:
        assert ack_lens(case, "acceptance-repro-" + rep["id"], rep["actor"], close_id) == 0
    assert ack_lens(case, "acceptance-cold", case["plan"]["cold_policy"]["reviewer"], close_id) == 0


def complete_v3(case, *, close_id="attempt", build_exit=0, amend=None):
    reviewer = case["plan"]["cold_policy"]["reviewer"]
    assert cold_phase(case, "commit", close_id, actor=reviewer) == 0
    data = bundle_v2(case, build_exit=build_exit)
    record = close.load_close(case["store"], close_id)
    data.update(schema_version=3, close_id=close_id,
                **{k: record["acceptance_route"][k] for k in
                   ("instance_id", "attempt_id", "revision", "plan_hash", "registry_hash", "project_id")})
    hygiene_bundle(case, data)
    if amend:
        amend(data)
    write_json(case["inputs"] / "bundle.json", data)
    assert command(case, "acceptance", "attach", "--id", close_id, "--from", "lead",
                   "--file", str(case["inputs"] / "bundle.json")) == 0
    assert cold_phase(case, "reconcile", close_id, actor=reviewer) == 0
    final_accepts(case, data, close_id)
    return data


def assign_fresh_cold(case, actor):
    case["plan"]["cold_policy"]["reviewer"] = actor
    case["plan"]["cold_policy"]["roster"].append({"actor": actor, "vendor": "beta"})
    cfg = case["store"].load_config()
    cfg["agents"].append(actor)
    write_json(case["store"].dir / "config.json", cfg)
    write_json(case["inputs"] / "plan.json", case["plan"])


def test_acceptance_complete_supported_fixture_go(case_v3, capsys):
    case = case_v3
    assert open_attempt(case, "--lens", "acceptance-run-bar", "--allow", "acceptance-run-bar:runner-a",
                        "--lens", "acceptance-cold", "--allow", "acceptance-cold:cold") == 0
    complete_v3(case)
    capsys.readouterr()
    assert command(case, "check", "--id", "attempt", "--json") == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "GO"
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 0
    saved = close.load_close(case["store"], "attempt")["final"]
    assert saved["acceptance_snapshot"]["cold_checked"] is True
    assert saved["close_result"]["verdict"] == "GO"


@pytest.mark.parametrize("field", ["claims_exposed", "authored_in_scope", "prior_exposure"])
def test_acceptance_final_cold_rejects_author_or_claims_exposure(case_v3, field):
    assert open_attempt(case_v3) == 0
    assert cold_phase(case_v3, "commit", change={field: True}) == 2
    assert close.load_close(case_v3["store"], "attempt")["acceptance_route"]["cold_commit_hash"] is None


@pytest.mark.parametrize("mode", ["required", "missing-disclosure", "disclosed"])
def test_acceptance_available_second_vendor_required_otherwise_absence_disclosed(case_v3, capsys, mode):
    case = case_v3
    policy = case["plan"]["cold_policy"]
    for entry in policy["roster"]:
        if mode != "required" or entry["actor"] == "cold":
            entry["vendor"] = "alpha"
    if mode == "disclosed":
        policy["absence_disclosure"] = "Only one vendor available for this frozen assignment."
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    complete_v3(case)
    if mode == "disclosed":
        assert command(case, "check", "--id", "attempt") == 0
        assert snapshot(case)["cold"]["absence_disclosure"] == policy["absence_disclosure"]
    else:
        assert "acceptance_lens_not_independent" in check(case, capsys)


@pytest.mark.parametrize("missing", ["reproducer", "cold", "reconcile", "wrong-actor", "override"])
def test_acceptance_final_attestations_are_required_and_bound(case_v3, capsys, missing):
    case = case_v3
    assert open_attempt(case) == 0
    data = complete_v3(case)
    with close.close_transaction(case["store"], "attempt") as tx:
        key = "acceptance-repro-" + data["reproductions"][0]["id"]
        if missing == "cold":
            tx.record["lens_acks"].pop("acceptance-cold")
        elif missing == "reconcile":
            tx.record["acceptance_route"]["cold_reconcile_hash"] = None
        elif missing == "reproducer":
            tx.record["lens_acks"].pop(key)
        elif missing == "wrong-actor":
            tx.record["lens_acks"][key]["from"] = "lead"
        else:
            tx.record["lens_acks"][key]["override"] = True
        tx.commit()
    assert {"acceptance_cold_missing", "acceptance_lens_not_independent"} & check(case, capsys)


def test_acceptance_publish_rechecks_changed_bytes(case_v3, capsys):
    case = case_v3
    assert open_attempt(case) == 0
    data = complete_v3(case)
    assert command(case, "check", "--id", "attempt") == 0
    path = case["store"].dir / "acceptance" / "sha256" / data["artifacts"][0]["sha256"]
    path.write_bytes(b"changed after check")
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3
    assert close.load_close(case["store"], "attempt")["status"] == "open"


def test_acceptance_increment_one_integration_hold_then_successor_go(case_v3, capsys):
    case = case_v3
    assert open_attempt(case, "--lens", "acceptance-run-bar", "--allow", "acceptance-run-bar:runner-a",
                        "--lens", "acceptance-cold", "--allow", "acceptance-cold:cold") == 0
    complete_v3(case, build_exit=1)
    assert command(case, "draft", "--id", "attempt", "--from", "lead", "-m", "artifact failure retained") == 0
    codes = check(case, capsys)
    assert "acceptance_row_failed" in codes
    assert "acceptance_cold_missing" not in codes
    publish_hold(case)
    parent = close.load_close(case["store"], "attempt")
    assert parent["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False
    assign_fresh_cold(case, "cold-next")
    assert successor(case) == 0
    complete_v3(case, close_id="next")
    assert command(case, "check", "--id", "next") == 0
    assert command(case, "publish", "--id", "next", "--from", "lead", "--verdict", "go") == 0
    assert close.load_close(case["store"], "attempt") == parent


def test_acceptance_parent_audit_lists_all_successor_alternatives(case_v2, capsys):
    assert open_attempt(case_v2) == 0
    bundle_v2(case_v2)
    assert attach(case_v2) == 0
    publish_hold(case_v2)
    assert successor(case_v2, new_id="first") == 0
    assert successor(case_v2, new_id="second") == 0
    capsys.readouterr()
    assert command(case_v2, "show", "--id", "attempt") == 0
    children = json.loads(capsys.readouterr().out)["acceptance_successors"]
    assert {item["close_id"] for item in children} == {"first", "second"}
    assert all(item["status"] == "open" for item in children)


def test_acceptance_coverage_comparators_are_explicit():
    for comparator in acceptance.COMPARATORS:
        expected = [] if comparator == "exact-failure-set" else 0
        assert coverage.predicate({"comparator": comparator, "expected": expected})
    with pytest.raises(acceptance.AcceptanceError, match="unsupported coverage comparator"):
        coverage.predicate({"comparator": "future-at-most", "expected": 0})


def test_acceptance_strongest_pruning_has_canonical_approval_shape():
    exact = {"kind": "exact-json", "expected": ["a", "b"]}
    weaker = {"kind": "failure-set", "expected": ["a", "b"]}
    assert coverage.strongest([weaker, exact, weaker]) == [exact]


@pytest.mark.parametrize("change", ["report-bytes", "event-order", "context-replay", "binding", "reporter"])
def test_acceptance_cold_commitment_tampering_holds(case_v3, capsys, change):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    with close.close_transaction(case["store"], "attempt") as tx:
        route = tx.record["acceptance_route"]
        if change == "report-bytes":
            (case["store"].dir / "acceptance" / "sha256" / route["cold_commit_hash"]).write_bytes(b"tampered")
        elif change == "event-order":
            tx.record["events"].reverse()
        elif change == "context-replay":
            route["cold_reconcile_hash"] = route["cold_commit_hash"]
        elif change == "binding":
            tx.record["lens_acks"]["acceptance-cold"]["acceptance_binding"]["cold_commit_hash"] = "f" * 64
        else:
            next(e for e in tx.record["events"] if e["event"] == "acceptance:cold-commit")["by"] = "lead"
        tx.commit()
    assert check(case, capsys)
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("actor", ["author", "runner-a", "lead", "reproducer"])
def test_acceptance_final_cold_actor_must_be_disjoint(case_v3, capsys, actor):
    case = case_v3
    case["plan"]["cold_policy"]["reviewer"] = actor
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit", actor=actor) == 0
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    assert cold_phase(case, "reconcile", actor=actor) == 0
    final_accepts(case, data)
    assert "acceptance_lens_not_independent" in check(case, capsys)


def test_acceptance_cold_phases_reject_wrong_actor_and_late_commit(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit", actor="lead") == 2
    assert cold_phase(case, "reconcile") == 2
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    before = close.load_close(case["store"], "attempt")
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt") == before
    assert cold_phase(case, "commit") == 0
    assert attach(case) == 0
    assert cold_phase(case, "commit") == 2


@pytest.mark.parametrize("disposition", ["open", "resolved"])
def test_acceptance_cold_blocking_residual_is_additive(case_v3, capsys, disposition):
    case = case_v3
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit", change={"observations": [
        {"id": "finding", "blocking": True, "evidence": "independent observation"}]}) == 0
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    assert cold_phase(case, "reconcile", change={"findings": [
        {"id": "finding", "disposition": disposition, "evidence": "reviewer reconciliation"}]}) == 0
    final_accepts(case, data)
    if disposition == "open":
        assert "acceptance_residual_open" in check(case, capsys)
    else:
        assert command(case, "check", "--id", "attempt") == 0


def test_acceptance_publish_resolves_once_and_saves_that_snapshot(case_v3, monkeypatch):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    original = acceptance.resolve
    snapshots = []

    def capture(store, record, **kwargs):
        assert kwargs["live"] is True
        result = original(store, record, **kwargs)
        snapshots.append(deepcopy(result))
        return result

    monkeypatch.setattr(acceptance, "resolve", capture)
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 0
    assert len(snapshots) == 1
    assert close.load_close(case["store"], "attempt")["final"]["acceptance_snapshot"] == snapshots[0]


def test_acceptance_unapproved_lineage_requires_new_root(case_v3, capsys):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    publish_hold(case)
    case["plan"]["rows"][0]["expected"] = 1
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case, new_id="lost") == 0
    complete_v3(case, close_id="lost", build_exit=1)
    assert command(case, "publish", "--id", "lost", "--from", "lead", "--verdict", "hold") == 3
    case["plan"]["rows"][0]["expected"] = 0
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case, parent="lost", new_id="restored") == 0
    complete_v3(case, close_id="restored")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "restored"))
    assert "acceptance_category_moved_unreviewed" in {c for c, _ in result["holds"]}
    assert command(case, "publish", "--id", "restored", "--from", "lead", "--verdict", "go") == 3
    assign_fresh_cold(case, "cold-root")
    assert open_attempt(case, "--id", "new-root") == 0
    complete_v3(case, close_id="new-root")
    # Recovery resets procedural lineage, not the accumulated assertions.
    assert command(case, "check", "--id", "new-root") == 3


def test_acceptance_amendment_keeps_unrelated_outcome_passed(case_v2):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case, build_exit=1)
    assert attach(case) == 0
    publish_hold(case)
    case["plan"]["rows"][0]["expected"] = 1
    approval = target_approval(case, "attempt", "next")
    assert successor(case, reduction=approval) == 0
    result = complete_child(case)
    assert result["outcomes"][0]["passed"] is None
    assert result["outcomes"][1]["passed"] is True
    assert "disposition" not in result["outcomes"][1]


def test_acceptance_unblinded_delta_reviewer_cannot_be_final_cold(case_v3, capsys):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    publish_hold(case)
    assert successor(case) == 0
    complete_v3(case, close_id="next")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "next"))
    assert "acceptance_cold_missing" in {c for c, _ in result["holds"]}


@pytest.mark.parametrize("change", ["shared-access", "leaked-plan", "missing-delivery"])
def test_acceptance_cold_delivery_access_and_retention(case_v3, capsys, change):
    case = case_v3
    assert open_attempt(case) == 0
    if change == "shared-access":
        assert cold_phase(case, "commit", change={"access_id": "verifier"}) == 0
    elif change == "leaked-plan":
        assert cold_phase(case, "commit", change={"delivery_manifest": [
            {"kind": "plan", "path": "plan.json", "sha256": "f" * 64}]}) == 2
        return
    else:
        assert cold_phase(case, "commit") == 0
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    assert cold_phase(case, "reconcile") == 0
    final_accepts(case, data)
    if change == "missing-delivery":
        route = close.load_close(case["store"], "attempt")["acceptance_route"]
        initial = acceptance.decode(acceptance._retained(case["store"], route["cold_commit_hash"]))
        digest = initial["delivery_manifest"][0]["sha256"]
        (case["store"].dir / "acceptance" / "sha256" / digest).unlink()
    codes = check(case, capsys)
    assert {"acceptance_record_missing", "acceptance_lens_not_independent"} & codes


def test_acceptance_complete_cold_cannot_clear_ordinary_counter(case_v3, capsys):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    assert command(case, "ack", "--id", "attempt", "--lens", "acceptance-run-bar", "--from", "runner-a",
                   "--status", "counter", "--counter", "ordinary", "--finding", "unfixed obligation") == 0
    assert command(case, "check", "--id", "attempt") == 3
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("role", ["opener", "freezer", "attacher", "author", "runner", "reproducer",
                                  "event", "ancestor-opener", "ancestor-freezer", "ancestor-event", "amender"])
def test_acceptance_every_recorded_actor_is_excluded_from_final_cold(case_v3, role):
    case = case_v3
    ancestor = role.startswith("ancestor-") or role == "amender"
    if ancestor:
        assign_fresh_cold(case, "first-cold")
    if role == "author":
        case["plan"]["authors"].append("cold")
    if role == "runner":
        case["plan"]["partitions"][0]["agents"].append("cold")
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    with close.close_transaction(case["store"], "attempt") as tx:
        if role.endswith("opener"):
            tx.record["opened_by"] = "cold"
        elif role.endswith("freezer"):
            tx.record["acceptance_route"]["frozen_by"] = "cold"
        elif role.endswith("event"):
            close._event(tx.record, "custom:preparation", "cold", "2026-01-01T00:00:00Z")
        tx.commit()
    complete_v3(case)
    target = "attempt"
    if ancestor:
        publish_hold(case)
        case["plan"]["cold_policy"]["reviewer"] = "cold"
        write_json(case["inputs"] / "plan.json", case["plan"])
        assert successor(case) == 0
        if role == "amender":
            with close.close_transaction(case["store"], "next") as tx:
                route = tx.record["acceptance_route"]
                amendment = acceptance.decode(acceptance._retained(case["store"], route["amendment_hash"]))
                amendment["by"] = "cold"
                route["amendment_hash"] = acceptance._retain(case["store"], json.dumps(amendment).encode())
                tx.commit()
        complete_v3(case, close_id="next")
        target = "next"
    record = close.load_close(case["store"], target)
    route, plan = acceptance._policy(case["store"], record)
    data = acceptance.decode(acceptance._retained(case["store"], route["bundle_hash"]))
    if role == "attacher":
        record["acceptance_route"]["attached_by"] = "cold"
    if role == "reproducer":
        data["reproductions"][0]["actor"] = "cold"
    # Isolate actor provenance from vendor, authority and unrelated ordinary holds.
    from agenttalk import acceptance_cold
    with pytest.raises(acceptance.AcceptanceError) as error:
        acceptance_cold.evaluate(case["store"], record, plan, data, {"holds": [], "outcomes": []})
    assert error.value.code == "acceptance_lens_not_independent"


@pytest.mark.parametrize("change", ["same-revision", "same-targets-new-revision"])
def test_acceptance_new_root_cannot_reuse_unblinded_reviewer(case_v3, change):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    publish_hold(case)
    if change == "same-targets-new-revision":
        (case["project"] / "source.txt").write_text("another revision", encoding="utf-8")
        git(case["project"], "add", ".")
        git(case["project"], "commit", "-qm", "new revision same protected targets")
        case["sha"] = git(case["project"], "rev-parse", "HEAD")
    assert open_attempt(case, "--id", "new-root") == 0
    complete_v3(case, close_id="new-root")
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "new-root"))
    assert "acceptance_cold_missing" in {c for c, _ in result["holds"]}
    assert command(case, "publish", "--id", "new-root", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("damage", ["unrelated-close", "child-parent"])
def test_acceptance_show_reports_corruption_without_hiding_record(case_v2, capsys, damage):
    case = case_v2
    assert open_attempt(case) == 0
    bundle_v2(case)
    assert attach(case) == 0
    publish_hold(case)
    assert successor(case) == 0
    if damage == "unrelated-close":
        close.close_path(case["store"], "junk").write_text("invalid JSON", encoding="utf-8")
    else:
        digest = close.load_close(case["store"], "next")["acceptance_route"]["parent_record_hash"]
        (case["store"].dir / "acceptance" / "sha256" / digest).unlink()
    capsys.readouterr()
    assert command(case, "show", "--id", "attempt") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["close_id"] == "attempt"
    assert shown["acceptance_successors_error"][0]["close_id"] == ("junk" if damage == "unrelated-close" else "next")
    if damage == "unrelated-close":
        assert shown["acceptance_successors"][0]["close_id"] == "next"


@pytest.mark.parametrize("kind", ["source", "safety", "toolchain", "access"])
def test_acceptance_withheld_plan_digest_cannot_be_mislabelled(case_v3, kind):
    case = case_v3
    assert open_attempt(case) == 0

    def add_withheld(data):
        data["delivery_manifest"].append({"kind": kind, "path": "plan.json",
                                          "sha256": data["binding"]["plan_hash"]})

    assert cold_phase(case, "commit", change=add_withheld) == 2
    assert close.load_close(case["store"], "attempt")["acceptance_route"]["cold_commit_hash"] is None


@pytest.mark.parametrize("invalid", ["unknown-kind", "missing-source", "mismatched-access"])
def test_acceptance_delivery_guards_each_refuse_otherwise_valid_report(case_v3, invalid):
    case = case_v3
    assert open_attempt(case) == 0

    def damage(data):
        if invalid == "unknown-kind":
            data["delivery_manifest"].append(dict(data["delivery_manifest"][0], kind="author-claims"))
        elif invalid == "missing-source":
            data["delivery_manifest"] = [d for d in data["delivery_manifest"] if d["kind"] != "source"]
        else:
            data["access_evidence"] = "f" * 64

    assert cold_phase(case, "commit", change=damage) == 2


def test_acceptance_pre_reconciliation_accepts_are_stale(case_v3, capsys):
    case = case_v3
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit") == 0
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    final_accepts(case, data)
    assert cold_phase(case, "reconcile") == 0
    assert ack_lens(case, "acceptance-cold", "cold") == 0
    assert "acceptance_lens_not_independent" in check(case, capsys)
    final_accepts(case, data)
    assert command(case, "check", "--id", "attempt") == 0


def test_acceptance_reproduction_must_use_available_second_vendor(case_v3, capsys):
    case = case_v3
    next(e for e in case["plan"]["cold_policy"]["roster"] if e["actor"] == "reproducer")["vendor"] = "alpha"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    complete_v3(case)
    assert "acceptance_lens_not_independent" in check(case, capsys)


def test_acceptance_monoculture_roster_must_include_participants(case_v3, capsys):
    case = case_v3
    policy = case["plan"]["cold_policy"]
    policy["roster"] = [dict(e, vendor="alpha") for e in policy["roster"] if e["actor"] != "reproducer"]
    policy["absence_disclosure"] = "Only one vendor available."
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    complete_v3(case)
    assert "acceptance_lens_not_independent" in check(case, capsys)


def test_acceptance_duplicate_attach_commitment_event_holds(case_v3, capsys):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    with close.close_transaction(case["store"], "attempt") as tx:
        event = next(e for e in tx.record["events"] if e["event"] == "acceptance:attach")
        tx.record["events"].insert(1, deepcopy(event))
        tx.commit()
    assert "acceptance_cold_missing" in check(case, capsys)


def test_acceptance_attach_installs_reproducer_lenses(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit") == 0
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    lenses = {lens["id"]: lens for lens in close.load_close(case["store"], "attempt")["required_lenses"]}
    for rep in data["reproductions"]:
        assert lenses["acceptance-repro-" + rep["id"]]["allowed_agents"] == [rep["actor"]]


@pytest.mark.parametrize("reviewer", ["lead", "liaison"])
def test_acceptance_plan_opener_and_distinct_attacher_cannot_review(case_v3, reviewer):
    case = case_v3
    cfg = case["store"].load_config()
    cfg["agents"].append("liaison")
    cfg["operator_facing"] = "liaison"
    write_json(case["store"].dir / "config.json", cfg)
    policy = case["plan"]["cold_policy"]
    policy["reviewer"] = reviewer
    policy["roster"].append({"actor": "liaison", "vendor": "beta"})
    next(e for e in policy["roster"] if e["actor"] == "lead")["vendor"] = "beta"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit", actor=reviewer) == 0
    data = bundle_v2(case)
    data["schema_version"] = 3
    write_json(case["inputs"] / "bundle.json", data)
    assert command(case, "acceptance", "attach", "--id", "attempt", "--from", "liaison",
                   "--file", str(case["inputs"] / "bundle.json")) == 0
    assert cold_phase(case, "reconcile", actor=reviewer) == 0
    final_accepts(case, data)
    # Valid different vendor and configured verifier isolate actor exclusion.
    assert {c for c, _ in snapshot(case)["holds"]} == {"acceptance_lens_not_independent"}


@pytest.mark.parametrize("history,blocked", [("descendant", True), ("ancestor", True),
                                             ("unrelated", False), ("unverifiable", True)])
def test_acceptance_relabelled_recovery_uses_source_ancestry(case_v3, monkeypatch, history, blocked):
    case = case_v3
    base = case["sha"]
    (case["project"] / "source.txt").write_text("first branch", encoding="utf-8")
    git(case["project"], "commit", "-qam", "first branch")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    assert open_attempt(case) == 0
    complete_v3(case)
    publish_hold(case)
    if history in {"unrelated", "ancestor"}:
        git(case["project"], "checkout", "--detach", base)
    if history != "ancestor":
        (case["project"] / "source.txt").write_text("recovery branch", encoding="utf-8")
        git(case["project"], "commit", "-qam", "recovery branch")
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    for row in case["plan"]["rows"]:
        row["id"] += "x"
        row["artifact"] += "x"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case, "--id", "recovery") == 0
    complete_v3(case, close_id="recovery")
    if history == "unverifiable":
        run = subprocess.run

        def unavailable(args, **kwargs):
            if "merge-base" in args:
                return subprocess.CompletedProcess(args, 128, "", "object unavailable")
            return run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", unavailable)
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "recovery"))
    assert ("acceptance_cold_missing" in {c for c, _ in result["holds"]}) is blocked
    assert command(case, "publish", "--id", "recovery", "--from", "lead", "--verdict", "go") == (3 if blocked else 0)


@pytest.mark.parametrize("damage", ["unknown-project", "same-project", "different-project"])
def test_acceptance_exposure_corruption_scoped_with_remedy(case_v3, damage):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    path = close.close_path(case["store"], "junk")
    if damage == "unknown-project":
        path.write_text("broken JSON", encoding="utf-8")
    else:
        rec = close.load_close(case["store"], "attempt")
        rec["acceptance_route"]["plan_hash"] = "f" * 64
        if damage == "different-project":
            rec["acceptance_route"]["project_id"] = "git-" + "a" * 60
        write_json(path, rec)
    result = snapshot(case)
    if damage == "different-project":
        assert result["holds"] == []
    else:
        message = next(msg for code, msg in result["holds"] if code == "acceptance_cold_missing")
        assert "junk" in message and str(path) in message
        assert "quarantine" in message and "trusted backup" in message


@pytest.mark.parametrize("scope", ["milestone", "global", "remediation", "unrelated"])
def test_acceptance_gate_actor_provenance(case_v3, scope):
    from agenttalk import gates
    case = case_v3
    assert open_attempt(case) == 0
    if scope == "remediation":
        with close.close_transaction(case["store"], "attempt") as tx:
            tx.record["remediation_items"]["repair"] = {"gate": "reviewed-gate", "owner": "lead"}
            tx.commit()
    gates.set_gate(case["store"].root, name="reviewed-gate", status="green", severity="info",
                   scope=scope, actor="cold", evidence_source="manual_review")
    complete_v3(case)
    holds = {c for c, _ in snapshot(case)["holds"]}
    assert ("acceptance_lens_not_independent" in holds) is (scope != "unrelated")


def test_acceptance_gate_retains_earlier_evidence_actor(case_v3):
    from agenttalk import gates
    case = case_v3
    assert open_attempt(case) == 0
    gates.set_gate(case["store"].root, name="reviewed-gate", status="green", severity="info",
                   scope="milestone", actor="cold", evidence_source="manual_review", evidence=["reviewed inputs"])
    gates.set_gate(case["store"].root, name="reviewed-gate", status="green", severity="info",
                   scope="milestone", actor="lead", evidence_source="manual_review")
    complete_v3(case)
    assert "acceptance_lens_not_independent" in {c for c, _ in snapshot(case)["holds"]}


def test_acceptance_ancestor_bundle_actor_without_ack_is_excluded(case_v3):
    case = case_v3
    assign_fresh_cold(case, "first-cold")
    assert open_attempt(case) == 0
    complete_v3(case)
    with close.close_transaction(case["store"], "attempt") as tx:
        route = tx.record["acceptance_route"]
        data = acceptance.decode(acceptance._retained(case["store"], route["bundle_hash"]))
        data["reproductions"][0]["actor"] = "cold"
        route["bundle_hash"] = acceptance._retain(case["store"], json.dumps(data).encode())
        tx.commit()
    publish_hold(case)
    case["plan"]["cold_policy"]["reviewer"] = "cold"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert successor(case) == 0
    complete_v3(case, close_id="next")
    from agenttalk import acceptance_cold
    rec = close.load_close(case["store"], "next")
    route, plan = acceptance._policy(case["store"], rec)
    data = acceptance.decode(acceptance._retained(case["store"], route["bundle_hash"]))
    with pytest.raises(acceptance.AcceptanceError) as exc:
        acceptance_cold.evaluate(case["store"], rec, plan, data, {"holds": []})
    assert exc.value.code == "acceptance_lens_not_independent"


def test_acceptance_ancestor_raw_artifact_is_withheld(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    data = complete_v3(case)
    publish_hold(case)
    assign_fresh_cold(case, "fresh")
    assert successor(case) == 0
    artifact = data["artifacts"][0]

    def leak(report):
        report["delivery_manifest"].append({"kind": "safety", "path": artifact["path"], "sha256": artifact["sha256"]})

    assert cold_phase(case, "commit", "next", actor="fresh", change=leak) == 2


def test_acceptance_evaluation_rechecks_withheld_delivery(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    with close.close_transaction(case["store"], "attempt") as tx:
        route = tx.record["acceptance_route"]
        initial = acceptance.decode(acceptance._retained(case["store"], route["cold_commit_hash"]))
        initial["delivery_manifest"].append({"kind": "safety", "path": "plan.json", "sha256": route["plan_hash"]})
        route["cold_commit_hash"] = acceptance._retain(case["store"], json.dumps(initial).encode())
        reconcile = acceptance.decode(acceptance._retained(case["store"], route["cold_reconcile_hash"]))
        reconcile["commit_hash"] = route["cold_commit_hash"]
        route["cold_reconcile_hash"] = acceptance._retain(case["store"], json.dumps(reconcile).encode())
        for event in tx.record["events"]:
            if event["event"] in {"acceptance:cold-commit", "acceptance:cold-reconcile"}:
                phase = event["event"].split("-")[-1]
                event["report_hash"] = route["cold_" + phase + "_hash"]
        for ack in tx.record["lens_acks"].values():
            ack["acceptance_binding"] = acceptance.ack_binding(tx.record)
        tx.commit()
    assert "acceptance_cold_missing" in {c for c, _ in snapshot(case)["holds"]}


def test_acceptance_later_reveal_does_not_taint_earlier_commit(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    assert open_attempt(case, "--id", "later") == 0
    complete_v3(case, close_id="later")
    assert snapshot(case)["holds"] == []


def test_acceptance_owner_provenance_excludes_reviewer(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    with close.close_transaction(case["store"], "attempt") as tx:
        tx.record["remediation_items"]["repair"] = {"owner": "cold"}
        tx.commit()
    complete_v3(case)
    assert "acceptance_lens_not_independent" in {c for c, _ in snapshot(case)["holds"]}


def test_acceptance_exposure_requires_one_commit_event(case_v3):
    from agenttalk import acceptance_audit
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    rec = close.load_close(case["store"], "attempt")
    rec["events"].append(deepcopy(next(e for e in rec["events"] if e["event"] == "acceptance:cold-commit")))
    with pytest.raises(acceptance.AcceptanceError, match="ambiguous"):
        acceptance_audit.check_prior_exposure(case["store"], rec, case["plan"], "cold")


def test_acceptance_monoculture_roster_includes_attacher(case_v3):
    case = case_v3
    policy = case["plan"]["cold_policy"]
    policy["roster"] = [dict(e, vendor="alpha") for e in policy["roster"] if e["actor"] != "lead"]
    policy["absence_disclosure"] = "Only one vendor available."
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    complete_v3(case)
    assert "acceptance_lens_not_independent" in {c for c, _ in snapshot(case)["holds"]}


@pytest.mark.parametrize("rewrite,blocked", [("rebase", True), ("squash", True), ("cherry-pick", True),
                                             ("different", False), ("unverifiable", True)])
def test_acceptance_rewritten_change_identity_table(case_v3, monkeypatch, rewrite, blocked):
    case = case_v3
    base = case["sha"]
    changes = []
    for name in ("change-a.txt", "change-b.txt"):
        (case["project"] / name).write_text("reviewed change\n", encoding="utf-8")
        git(case["project"], "add", name)
        git(case["project"], "commit", "-qm", "part of reviewed change")
        changes.append(git(case["project"], "rev-parse", "HEAD"))
    case["sha"] = changes[-1]
    case["plan"]["cold_policy"]["change_base"] = base
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    complete_v3(case)
    publish_hold(case)
    git(case["project"], "branch", "reviewed", changes[-1])
    git(case["project"], "checkout", "--detach", base)
    (case["project"] / "new-base.txt").write_text("base moved\n", encoding="utf-8")
    git(case["project"], "add", "new-base.txt")
    git(case["project"], "commit", "-qm", "new base")
    new_base = git(case["project"], "rev-parse", "HEAD")
    if rewrite == "rebase":
        git(case["project"], "rebase", "--onto", new_base, base, "reviewed")
    elif rewrite == "squash":
        git(case["project"], "merge", "--squash", changes[-1])
        git(case["project"], "commit", "-qm", "same change squashed")
    elif rewrite == "different":
        (case["project"] / "different.txt").write_text("different change\n", encoding="utf-8")
        git(case["project"], "add", "different.txt")
        git(case["project"], "commit", "-qm", "different change")
    else:
        git(case["project"], "cherry-pick", *changes)
    case["sha"] = git(case["project"], "rev-parse", "HEAD")
    case["plan"]["cold_policy"]["change_base"] = new_base
    for row in case["plan"]["rows"]:
        row["id"] += "x"
        row["artifact"] += "x"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case, "--id", "rewritten") == 0
    complete_v3(case, close_id="rewritten")
    if rewrite == "unverifiable":
        run = subprocess.run

        def unavailable(args, **kwargs):
            if "patch-id" in args:
                return subprocess.CompletedProcess(args, 128, b"", b"patch-id unavailable")
            return run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", unavailable)
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "rewritten"))
    assert ("acceptance_cold_missing" in {c for c, _ in result["holds"]}) is blocked
    assert command(case, "publish", "--id", "rewritten", "--from", "lead", "--verdict", "go") == (3 if blocked else 0)


@pytest.mark.parametrize("fault", ["shallow", "abbreviated"])
def test_acceptance_ancestry_preconditions_fail_closed(case_v3, monkeypatch, fault):
    from agenttalk import acceptance_audit
    case = case_v3
    run = subprocess.run
    if fault == "shallow":
        def shallow(args, **kwargs):
            if "--is-shallow-repository" in args:
                return subprocess.CompletedProcess(args, 0, b"true\n", b"")
            return run(args, **kwargs)
        monkeypatch.setattr(subprocess, "run", shallow)
    earlier = case["sha"][:7] if fault == "abbreviated" else case["sha"]
    with pytest.raises(acceptance.AcceptanceError) as exc:
        acceptance_audit.related_revisions({"locator": str(case["project"])}, earlier, case["sha"])
    assert exc.value.code == "acceptance_cold_missing"


def test_acceptance_ancestry_ignores_replace_graft(case_v3):
    from agenttalk import acceptance_audit
    case = case_v3
    base = case["plan"]["cold_policy"]["change_base"]
    git(case["project"], "replace", "--graft", case["sha"])
    assert acceptance_audit.related_revisions({"locator": str(case["project"])}, base, case["sha"])


def test_acceptance_unreadable_gate_attribution_holds(case_v3):
    from agenttalk import gates
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    gates.gates_path(case["store"].root).write_text("unreadable", encoding="utf-8")
    assert "acceptance_cold_missing" in {c for c, _ in snapshot(case)["holds"]}


def test_acceptance_later_gate_metadata_and_evidence_do_not_taint_commit(case_v3):
    from agenttalk import gates
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    gates.set_gate(case["store"].root, name="later-gate", status="green", severity="info",
                   scope="global", actor="cold", evidence_source="manual_review", evidence=["later activity"])
    assert snapshot(case)["holds"] == []


@pytest.mark.parametrize("base", ["missing", "abbreviated", "unknown", "candidate", "unrelated"])
def test_acceptance_change_base_verified_before_creation(case_v3, base):
    case = case_v3
    if base == "missing":
        case["plan"]["cold_policy"].pop("change_base")
    elif base == "unrelated":
        head = case["sha"]
        git(case["project"], "checkout", "--detach", case["plan"]["cold_policy"]["change_base"])
        (case["project"] / "other-base.txt").write_text("other history", encoding="utf-8")
        git(case["project"], "add", "other-base.txt")
        git(case["project"], "commit", "-qm", "unrelated base")
        case["plan"]["cold_policy"]["change_base"] = git(case["project"], "rev-parse", "HEAD")
        git(case["project"], "checkout", "--detach", head)
    else:
        case["plan"]["cold_policy"]["change_base"] = {
            "abbreviated": case["sha"][:7], "unknown": "f" * 40, "candidate": case["sha"]}[base]
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 3
    assert not close.close_path(case["store"], "attempt").exists()


def test_acceptance_recovery_root_preserves_failed_obligation(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case, build_exit=1)
    publish_hold(case)
    case["plan"]["rows"][0]["expected"] = 1
    assign_fresh_cold(case, "fresh-root-reviewer")
    assert open_attempt(case, "--id", "recovery") == 0
    complete_v3(case, close_id="recovery", build_exit=1)
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "recovery"))
    assert "acceptance_category_moved_unreviewed" in {c for c, _ in result["holds"]}
    assert command(case, "publish", "--id", "recovery", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_acceptance_informational_required_artifact_integrity_holds(case_v3, damage):
    case = case_v3
    case["plan"]["rows"][1]["policy"] = "informational"
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    data = complete_v3(case)
    artifact = next(a for a in data["artifacts"] if a["id"] == "tool")
    path = case["store"].dir / "acceptance" / "sha256" / artifact["sha256"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt retained measurement")
    assert "acceptance_record_missing" in {c for c, _ in snapshot(case)["holds"]}
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("rewrite", ["same-sha", "rebase", "squash", "different"])
def test_acceptance_recovery_obligations_follow_change_table(case_v3, rewrite):
    case = case_v3
    base = case["sha"]
    commits = []
    for name in ("first.txt", "second.txt"):
        (case["project"] / name).write_text("reviewed change\n", encoding="utf-8")
        git(case["project"], "add", name)
        git(case["project"], "commit", "-qm", "reviewed change part")
        commits.append(git(case["project"], "rev-parse", "HEAD"))
    case["sha"] = commits[-1]
    case["plan"]["cold_policy"]["change_base"] = base
    write_json(case["inputs"] / "plan.json", case["plan"])
    assert open_attempt(case) == 0
    complete_v3(case, build_exit=1)
    publish_hold(case)
    if rewrite != "same-sha":
        git(case["project"], "branch", "original", case["sha"])
        git(case["project"], "checkout", "--detach", base)
        (case["project"] / "base.txt").write_text("base update\n", encoding="utf-8")
        git(case["project"], "add", "base.txt")
        git(case["project"], "commit", "-qm", "base update")
        new_base = git(case["project"], "rev-parse", "HEAD")
        if rewrite == "rebase":
            git(case["project"], "rebase", "--onto", new_base, base, "original")
        elif rewrite == "squash":
            git(case["project"], "merge", "--squash", commits[-1])
            git(case["project"], "commit", "-qm", "same diff squashed")
        else:
            (case["project"] / "different.txt").write_text("different change\n", encoding="utf-8")
            git(case["project"], "add", "different.txt")
            git(case["project"], "commit", "-qm", "different change")
        case["sha"] = git(case["project"], "rev-parse", "HEAD")
        case["plan"]["cold_policy"]["change_base"] = new_base
    # Disjoint labels must not hide a same-content obligation; genuinely
    # different history/content also needs disjoint targets to be independent.
    for row in case["plan"]["rows"]:
        row["id"] += "-new"
        row["artifact"] += "-new"
    case["plan"]["rows"][0]["expected"] = 1
    assign_fresh_cold(case, "new-reviewer")
    assert open_attempt(case, "--id", "recovery") == 0
    complete_v3(case, close_id="recovery", build_exit=1)
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "recovery"))
    blocked = rewrite != "different"
    assert ("acceptance_category_moved_unreviewed" in {c for c, _ in result["holds"]}) is blocked
    if blocked:
        sources = result["related_obligations"][0]["protected"].values()
        assert any(s["outcome"].get("passed") is False for p in sources for s in p["sources"])
    assert command(case, "publish", "--id", "recovery", "--from", "lead", "--verdict", "go") == (3 if blocked else 0)


@pytest.mark.parametrize("resolution", ["passing", "approved", "tampered", "expired", "lead"])
def test_acceptance_recovery_requires_original_pass_or_exact_operator_approval(case_v3, resolution):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case, build_exit=1)
    publish_hold(case)
    original = close.load_close(case["store"], "attempt")
    if resolution != "passing":
        case["plan"]["rows"][0]["expected"] = 1
    assign_fresh_cold(case, "fresh-root")
    approval_path = target_approval(case, "attempt", "recovery") if resolution != "passing" else None
    assert open_attempt(case, "--id", "recovery") == 0

    def approve(data):
        if not approval_path:
            return
        reduction = json.loads(approval_path.read_text())
        raw = (case["store"].messages_dir / (reduction["decision_ref"] + ".json")).read_bytes()
        if resolution == "lead":
            message = json.loads(raw)
            message["from"] = "lead"
            raw = json.dumps(message).encode()
            (case["store"].messages_dir / (reduction["decision_ref"] + ".json")).write_bytes(raw)
        if resolution == "tampered":
            reduction["changes"][next(iter(reduction["changes"]))]["new"][0]["expected"] = 2
        path = case["inputs"] / "operator.json"
        path.write_bytes(raw)
        data["recovery_approvals"] = [{"prior_attempt_id": original["acceptance_route"]["attempt_id"],
                                       "reduction": reduction, "approval_artifact": "operator"}]
        data["artifacts"].append({"id": "operator", "path": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
        hygiene_bundle(case, data)

    complete_v3(case, close_id="recovery", build_exit=0 if resolution == "passing" else 1, amend=approve)
    if resolution == "expired":
        # Expiry is rechecked at GO time, not just when attachment succeeds.
        from agenttalk import acceptance_history
        from datetime import datetime, timezone
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(acceptance_history, "_now", lambda: datetime.max.replace(tzinfo=timezone.utc))
            assert command(case, "publish", "--id", "recovery", "--from", "lead", "--verdict", "go") == 3
        return
    result = acceptance.resolve(case["store"], close.load_close(case["store"], "recovery"))
    assert close.load_close(case["store"], "attempt") == original
    if resolution == "approved":
        assert result["outcomes"][0]["original_outcome"]["passed"] is False
        assert result["outcomes"][0]["disposition"] == "policy-amended"
    assert command(case, "publish", "--id", "recovery", "--from", "lead", "--verdict", "go") == (
        0 if resolution in {"passing", "approved"} else 3)


def test_acceptance_ld3_reproducer_can_never_be_final_cold_even_after_commit(case_v3):
    case = case_v3
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit") == 0
    data = bundle_v2(case)
    for rep in data["reproductions"]:
        rep["actor"] = "cold"
    hygiene_bundle(case, data)
    write_json(case["inputs"] / "bundle.json", data)
    assert attach(case) == 0
    assert cold_phase(case, "reconcile") == 0
    final_accepts(case, data)
    assert "acceptance_lens_not_independent" in {c for c, _ in snapshot(case)["holds"]}


@pytest.mark.parametrize("fault", ["environment", "offline", "fetch", "positive-control", "cleanup",
                                  "manifest", "confidentiality", "replay", "reproduction", "service", "isolation"])
def test_acceptance_hygiene_evidence_required_for_go(case_v3, fault):
    case = case_v3
    assert open_attempt(case) == 0

    def damage(data):
        run = data["reproductions"][0] if fault == "reproduction" else data["runs"][0]
        aid = (run["environment"] if fault in {"environment", "service", "isolation"} else
               run["offline_proof"] if fault in {"offline", "fetch", "positive-control", "reproduction"} else "hygiene")
        artifact = next(a for a in data["artifacts"] if a["id"] == aid)
        path = case["inputs"] / artifact["path"]
        value = json.loads(path.read_text())
        if fault == "environment":
            value["version_banners"] = []
        elif fault == "isolation":
            value["cache_overlay_fresh"] = False
        elif fault in {"offline", "reproduction"}:
            value["egress_denied"] = False
        elif fault == "fetch":
            value["attempted_fetch"] = True
        elif fault == "positive-control":
            value["positive_control"] = False
        elif fault == "service":
            value["services"] = [{"pid": 123, "ports": [12345], "owned": True, "stopped": False,
                                   "ports_released": False, "evidence": "service remains running"}]
        elif fault == "cleanup":
            value["scratch_removed"] = False
        elif fault == "manifest":
            value["sealed_manifest"] = []
        elif fault == "confidentiality":
            value["confidentiality"]["positive_control"] = False
        else:
            value["binding"]["attempt_id"] = "another-attempt"
        artifact["sha256"] = write_json(path, value)
        # Re-seal the manifest around deliberately invalid run evidence, so its
        # own guard, not merely a changed manifest, must refuse publication.
        if aid != "hygiene":
            from agenttalk import acceptance_hygiene as hygiene
            proof = next(a for a in data["artifacts"] if a["id"] == "hygiene")
            value = json.loads((case["inputs"] / proof["path"]).read_text())
            value.update(sealed_manifest=hygiene.execution_manifest(data), bundle_digest=hygiene.execution_digest(data))
            proof["sha256"] = write_json(case["inputs"] / proof["path"], value)

    complete_v3(case, amend=damage)
    result = snapshot(case)
    assert not result.get("hygiene_checked")
    assert result["holds"]
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("fault", ["manifest", "report", "positive-control", "match", "missing-artifact"])
def test_acceptance_final_sealed_closeout_is_rechecked(case_v3, fault):
    case = case_v3
    assert open_attempt(case) == 0
    complete_v3(case)
    assert snapshot(case)["hygiene_checked"] is True
    with close.close_transaction(case["store"], "attempt") as tx:
        route = tx.record["acceptance_route"]
        value = acceptance.decode(acceptance._retained(case["store"], route["cold_reconcile_hash"]))
        if fault == "manifest":
            value["closeout"]["sealed_manifest"] = []
        elif fault == "report":
            value["closeout"]["report_digest"] = "0" * 64
        elif fault == "positive-control":
            value["closeout"]["confidentiality"]["positive_control"] = False
        elif fault == "match":
            value["closeout"]["confidentiality"]["matches"] = ["unresolved sensitive content"]
        else:
            bundle = acceptance.decode(acceptance._retained(case["store"], route["bundle_hash"]))
            artifact = next(a for a in bundle["artifacts"] if a["id"] == "hygiene")
            (case["store"].dir / "acceptance" / "sha256" / artifact["sha256"]).unlink()
        route["cold_reconcile_hash"] = acceptance._retain(case["store"], json.dumps(value).encode())
        for event in tx.record["events"]:
            if event["event"] == "acceptance:cold-reconcile":
                event["report_hash"] = route["cold_reconcile_hash"]
        for ack in tx.record["lens_acks"].values():
            ack["acceptance_binding"] = acceptance.ack_binding(tx.record)
        tx.commit()
    assert command(case, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("field", ["hygiene", "environment", "offline_proof"])
def test_acceptance_schema3_cannot_attach_without_required_hygiene(case_v3, field):
    case = case_v3
    assert open_attempt(case) == 0
    assert cold_phase(case, "commit") == 0
    data = bundle_v2(case)
    (data if field == "hygiene" else data["runs"][0]).pop(field)
    write_json(case["inputs"] / "bundle.json", data)
    before = close.load_close(case["store"], "attempt")
    assert attach(case) == 2
    assert close.load_close(case["store"], "attempt") == before


def test_acceptance_pure_go_fold_requires_hygiene():
    assert "acceptance_record_missing" in {code for code, _ in acceptance.evaluate(
        {"trust_checked": True, "cold_checked": True, "holds": [], "outcomes": []})}
