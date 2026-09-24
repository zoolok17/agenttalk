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
    assert snapshot["holds"] == []


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
    write_json(case["inputs"] / "bundle.json", data)
    return data


def snapshot(case):
    return acceptance.resolve(case["store"], close.load_close(case["store"], "attempt"))


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


def complete_child(case, child="next"):
    # Existing fixture helpers name 'attempt'; redirect only the synthetic input
    # builder's close lookup and CLI ID, leaving persisted identities untouched.
    parent = close.load_close(case["store"], child)
    data = bundle_v2(case, build_exit=case["plan"]["rows"][0]["expected"])
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
    assert child["outcomes"][0]["passed"] is True
    assert child["parent"]["final"]["verdict"] == "HOLD"
    assert child["parent"]["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False
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
    reduction = {"rows": ["build"], "reason": "time-budget dependent metric", "alternatives": ["fixed effort"],
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
    assert child["parent"]["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False


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
    assert child["parent"]["final"]["acceptance_snapshot"]["outcomes"][0]["passed"] is False
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
