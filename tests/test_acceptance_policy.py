"""Environment amendments and retryable staged identity races."""

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from agenttalk import acceptance as A, acceptance_coverage as C, acceptance_history as H
from agenttalk import acceptance_preflight as P, acceptance_registry as R
import test_acceptance as legacy
import test_acceptance_preflight as preflight
import test_acceptance_staging as staging
from test_acceptance_staging import (
    save, record, complete, attachment, successor_command, add_informational_tool,
)

candidate, case, case_v2, case_v3 = staging.candidate, staging.case, staging.case_v2, staging.case_v3
environment, observation, plan, registry, staged = (staging.environment, staging.observation,
                                                  staging.plan, staging.registry, staging.staged)


def approval(case):
    parent = record(case)
    changes = C.changes(C.history(case["store"], parent), case["plan"], case["stage"]["registry"])
    assert changes
    value = {"rows": list(changes), "changes": changes, "cause": "policy-amendment",
             "reason": "explicit environment change", "alternatives": ["keep original"],
             "impact": "different execution policy", "owner": "operator", "expires_at": "9999-01-01T00:00:00Z",
             "evidence": [parent["acceptance_route"]["plan_hash"]], "decision_ref": "pending"}
    payload = H.approval_payload(parent, "next", A._hash((case["inputs"] / "plan.json").read_bytes()), value)
    msg = case["store"].send(sender="operator", recipient="lead", kind="message",
                             body=json.dumps(payload), _allow_reserved_sender=True)
    value["decision_ref"] = msg.id
    path = case["inputs"] / "reduction.json"
    legacy.write_json(path, value)
    return path


@pytest.mark.parametrize("change,approved", [("environment", False), ("environment", True),
    ("override", False), ("override", True), ("relocation", False)])
def test_successor_environment_and_override_amendment(candidate, change, approved):
    complete(candidate)
    legacy.publish_hold(candidate)
    stage = candidate["stage"]
    if change == "environment":
        for env in (candidate["plan"]["environment"], stage["observation"]["environment"]):
            env.update(config_digest="c" * 64, time_limit_seconds=86400,
                       memory_limit_bytes=2**40, os="synthetic-relaxed")
    elif change == "override":
        env = deepcopy(candidate["plan"]["environment"])
        env["time_limit_seconds"] = 86400
        pin = {"path": "row-override.json"}
        preflight.evidence(stage, pin, json.dumps(env).encode())
        (candidate["inputs"] / pin["path"]).write_bytes((stage["root"] / pin["path"]).read_bytes())
        for env in (candidate["plan"]["environment"], stage["observation"]["environment"]):
            env["row_overrides"] = [{"id": candidate["plan"]["rows"][0]["id"], "environment": deepcopy(pin)}]
    else:
        pin = stage["registry"]["files"][1]
        old = stage["root"] / pin["path"]
        pin["path"] = "relocated-provenance.dat"
        old.rename(stage["root"] / pin["path"])
    legacy.assign_fresh_cold(candidate, "next-cold")
    save(candidate)
    assert successor_command(candidate, approval(candidate) if approved else None) == 0
    data = attachment(candidate, close_id="next")
    assert legacy.command(candidate, "acceptance", "attach", "--id", "next", "--from", "lead",
                          "--file", str(candidate["inputs"] / "bundle.json")) == 0
    assert legacy.cold_phase(candidate, "reconcile", "next", actor="next-cold") == 0
    legacy.final_accepts(candidate, data, "next")
    result = A.resolve(candidate["store"], record(candidate, "next"))
    blocked = not approved and change != "relocation"
    assert ("acceptance_category_moved_unreviewed" in {c for c, _ in A.evaluate(result)}) == blocked
    assert legacy.command(candidate, "publish", "--id", "next", "--from", "lead",
                          "--verdict", "go") == (3 if blocked else 0)


def test_informational_open_identity_race_is_retryable(candidate, monkeypatch):
    add_informational_tool(candidate)
    complete(candidate)
    real_open, real_fstat, real_close = os.open, os.fstat, os.close
    watched = set()
    def opened(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith("jdk2.dat"):
            watched.add(fd)
        return fd
    def swapped(fd):
        result = real_fstat(fd)
        if fd in watched:
            fields = list(result)
            fields[1] += 1
            return os.stat_result(fields)
        return result
    def closed(fd):
        watched.discard(fd)
        return real_close(fd)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fstat", swapped)
    monkeypatch.setattr(os, "close", closed)
    result = A.resolve(candidate["store"], record(candidate))
    issues = [h for h in result["preflight"]["holds"] if h["ref"] == "jdk2"]
    assert issues and all(h["code"] == P.UNAVAILABLE and not h.get("mandatory") for h in issues)
    assert not A.evaluate(result)
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 0


def test_coverage_missing_definitions_names_guard(plan):
    with pytest.raises(A.AcceptanceError, match="^schema-4 coverage requires closed registry definitions$") as error:
        C.predicate(plan["rows"][0])
    assert error.value.code == "acceptance_policy_invalid"


def test_coverage_implicit_manifest_consumer_definition(registry):
    manifest = preflight.pin("manifest", "snapshot")
    registry["files"].append(manifest)
    java = registry["entries"][0]
    listed = dict(deepcopy(java), id="listed", snapshots=["manifest"])
    registry["entries"].append(listed)
    R.validate_registry(registry)
    assert java["snapshots"] == [] and java["dependencies"] == []
    before = C.definitions(registry)["java"]
    manifest["sha256"] = "b" * 64
    assert C.definitions(registry)["java"] != before


@pytest.mark.parametrize("field,value", [("config_digest", "b" * 64), ("environment_digest", "b" * 64),
    ("time_limit_seconds", 86400), ("memory_limit_bytes", 2**40), ("os", "other"), ("locale", "other"),
    ("timezone", "other"), ("scratch", "other"), ("cache_overlay", "other"), ("service_data", "other"),
    ("runtime", []), ("compiler", ["java"]), ("package_manager", ["java"]), ("services", ["service"])])
def test_coverage_binds_each_environment_field(plan, registry, field, value):
    old = C.group(plan["rows"], registry, plan["environment"])
    plan["environment"][field] = value
    assert C.changes(old, plan, registry)


def test_coverage_override_is_per_row_and_locator_independent(plan, registry):
    pin = {"path": "override.json", "sha256": "a" * 64, "size": 2}
    unrelated = {"path": "unrelated.json", "sha256": "c" * 64, "size": 3}
    plan["environment"]["row_overrides"] = [
        {"id": "other-row", "environment": unrelated}, {"id": "build", "environment": pin}]
    old = C.group(plan["rows"], registry, plan["environment"])
    pin["path"] = "relocated.json"
    unrelated["sha256"] = "d" * 64
    assert not C.changes(old, plan, registry)
    pin["sha256"] = "b" * 64
    assert C.changes(old, plan, registry)


def test_coverage_missing_environment_refused(plan, registry):
    with pytest.raises(A.AcceptanceError, match="schema-4 coverage requires planned environment"):
        C.predicate(plan["rows"][0], C.definitions(registry))


@pytest.mark.parametrize("renewal", [False, True])
def test_coverage_snapshot_renewal_and_expiry_edit_require_approval(plan, registry, renewal):
    manifest = preflight.pin("manifest", "snapshot")
    registry["files"].append(manifest)
    registry["entries"][0]["snapshots"] = ["manifest"]
    old = C.group(plan["rows"], registry, plan["environment"])
    manifest["expires_at"] = "2030-01-01T00:00:00Z"
    if renewal:
        registry["files"][0]["sha256"] = "b" * 64
        manifest["distribution"]["sha256"] = "b" * 64
        manifest["sha256"] = "c" * 64
    assert C.changes(old, plan, registry)


def test_identity_race_import_remains_policy_refusal(tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"synthetic")
    real = os.fstat
    def changed(fd):
        fields = list(real(fd))
        fields[1] += 1
        return os.stat_result(fields)
    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(A.StagedInputChangedError) as error:
        P.read_input(tmp_path / "file")
    assert error.value.code == "acceptance_policy_invalid"


def finish_successor(case, close_id="next", actor="next-cold"):
    data = attachment(case, close_id=close_id)
    assert legacy.command(case, "acceptance", "attach", "--id", close_id, "--from", "lead",
                          "--file", str(case["inputs"] / "bundle.json")) == 0
    assert legacy.cold_phase(case, "reconcile", close_id, actor=actor) == 0
    legacy.final_accepts(case, data, close_id)


@pytest.mark.parametrize("kind,approved", [("checker", False), ("toolchain", False),
    ("toolchain", True), ("service", False), ("service", True)])
def test_added_entry_kind_requires_environment_approval(candidate, kind, approved):
    complete(candidate)
    legacy.publish_hold(candidate)
    stage = candidate["stage"]
    entry = dict(deepcopy(stage["registry"]["entries"][0]), id="extra", kind=kind)
    if kind == "checker":
        entry.update(expected_banner=None, measurement={})
        for key in ("config", "comparator", "parser", "normalizer"):
            pin = preflight.pin(key, "config" if key == "config" else "adapter")
            preflight.evidence(stage, pin, b"synthetic measurement policy")
            stage["registry"]["files"].append(pin)
            entry["measurement"][key] = key
    else:
        role = "runtime" if kind == "toolchain" else "services"
        for env in (candidate["plan"]["environment"], stage["observation"]["environment"]):
            env[role].append("extra")
    stage["registry"]["entries"].append(entry)
    stage["observation"]["entries"].append(dict(deepcopy(stage["observation"]["entries"][0]), id="extra"))
    for row in candidate["plan"]["rows"]:
        row["registry_entries"].append("extra")
    legacy.assign_fresh_cold(candidate, "next-cold")
    save(candidate)
    assert successor_command(candidate, approval(candidate) if approved else None) == 0
    finish_successor(candidate)
    expected = 0 if kind == "checker" or approved else 3
    assert legacy.command(candidate, "publish", "--id", "next", "--from", "lead", "--verdict", "go") == expected


def test_approved_amendment_does_not_authorize_identical_descendant(candidate):
    complete(candidate)
    legacy.publish_hold(candidate)
    for env in (candidate["plan"]["environment"], candidate["stage"]["observation"]["environment"]):
        env["time_limit_seconds"] = 86400
    legacy.assign_fresh_cold(candidate, "next-cold")
    save(candidate)
    assert successor_command(candidate, approval(candidate)) == 0
    finish_successor(candidate)
    assert legacy.command(candidate, "publish", "--id", "next", "--from", "lead", "--verdict", "go") == 0
    legacy.assign_fresh_cold(candidate, "third-cold")
    save(candidate)  # fresh reviewer, identical protected rows/environment/registry
    assert legacy.command(candidate, "acceptance", "successor", "--id", "third", "--parent", "next",
                          "--from", "lead", "--acceptance-plan", str(candidate["inputs"] / "plan.json"),
                          "--project-repo", str(candidate["project"]), "--revision", candidate["sha"],
                          "--cache-root", str(candidate["stage"]["root"]), "--reason", "same policy") == 0
    finish_successor(candidate, "third", "third-cold")
    result = A.resolve(candidate["store"], record(candidate, "third"))
    assert {"acceptance_category_moved_unreviewed", "acceptance_plan_stale"} <= {c for c, _ in A.evaluate(result)}
    assert legacy.command(candidate, "publish", "--id", "third", "--from", "lead", "--verdict", "go") == 3


@pytest.mark.parametrize("phase,field", [(phase, field) for phase in ("opened", "after")
                                       for field in ("st_dev", "st_ino", "st_mode")])
def test_staged_identity_checks_each_observation(tmp_path, monkeypatch, phase, field):
    path = tmp_path / "input.dat"
    path.write_bytes(b"synthetic")
    original_open, original_stat, original_lstat = os.open, os.fstat, Path.lstat
    opened = []
    def changed(value):
        fields = {key: getattr(value, key) for key in ("st_dev", "st_ino", "st_mode")}
        fields[field] ^= 1
        return SimpleNamespace(**fields)
    def open_file(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fstat", lambda fd: changed(original_stat(fd)) if phase == "opened" else original_stat(fd))
    monkeypatch.setattr(Path, "lstat", lambda p, *a, **k: changed(original_lstat(p, *a, **k))
                        if phase == "after" and opened and p == path else original_lstat(p, *a, **k))
    with pytest.raises(A.StagedInputChangedError):
        with R.staged_stream(tmp_path, path.name):
            pytest.fail("a changed identity must never be yielded")


@pytest.mark.parametrize("phase,fault", [("opened", "fifo"), ("after", "fifo"), ("after", "reparse")])
def test_mid_open_unsafe_swap_is_not_retryable(tmp_path, monkeypatch, phase, fault):
    if fault == "reparse" and os.name != "nt":
        pytest.skip("Windows reparse attribute")
    path = tmp_path / "input.dat"
    path.write_bytes(b"synthetic")
    original_open, original_stat, original_lstat = os.open, os.fstat, Path.lstat
    opened = []
    def changed(value):
        fields = {key: getattr(value, key) for key in ("st_dev", "st_ino", "st_mode")}
        if fault == "fifo":
            fields["st_mode"] = stat.S_IFIFO | 0o600
        return SimpleNamespace(**fields, st_file_attributes=1024 if fault == "reparse" else 0)
    def open_file(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fstat", lambda fd: changed(original_stat(fd)) if phase == "opened" else original_stat(fd))
    monkeypatch.setattr(Path, "lstat", lambda p, *a, **k: changed(original_lstat(p, *a, **k))
                        if phase == "after" and opened and p == path else original_lstat(p, *a, **k))
    pin = {"id": "pin", "path": path.name, "size": 9, "sha256": A._hash(b"synthetic")}
    data, holds = P._read_pin(tmp_path, pin, [100])
    assert data is None and holds[0]["code"] == P.UNAVAILABLE and holds[0]["mandatory"]


@pytest.mark.parametrize("field", ["version", "provenance", "role", "distribution"])
def test_closed_pin_definition_keeps_semantic_metadata(registry, field):
    if field == "role":
        pin = preflight.pin("extra", "config")
        registry["entries"][0]["inputs"].append("extra")
    else:
        pin = preflight.pin("manifest", "snapshot")
        registry["entries"][0]["snapshots"].append("manifest")
        if field == "distribution":
            registry["files"].append(dict(deepcopy(registry["files"][0]), id="jdk2", path="jdk2.dat"))
            registry["entries"][0]["inputs"].append("jdk2")
    registry["files"].append(pin)
    before = C.definitions(registry)["java"]
    if field == "provenance":
        pin[field]["retrieved_at"] = "2026-02-01T00:00:00Z"
    elif field == "distribution":
        pin[field]["id"] = "jdk2"
    else:
        pin[field] = "adapter" if field == "role" else "new snapshot version"
    R.validate_registry(registry)
    assert C.definitions(registry)["java"] != before


@pytest.mark.parametrize("arg", ["{cache_overlay}/source.dat", "{checkout}/SOURCE.DAT",
                                "{scratch}/source.dat", "{scratch}/unrelated-source.dat-output",
                                "source.dat", "--input=source.dat"])
def test_command_tokens_cannot_name_staged_pin_paths(registry, arg):
    registry["entries"][0]["command"]["argv"].append(arg)
    with pytest.raises(A.AcceptanceError, match="command token names a staged-pin path"):
        R.validate_registry(registry)


def test_command_path_rebinding_cannot_hide_behind_pin_relocation(registry):
    registry["entries"][0]["command"]["argv"].append("{cache_overlay}/future.dat")
    R.validate_registry(registry)
    registry["files"][1]["path"] = "future.dat"
    with pytest.raises(A.AcceptanceError, match="command token names a staged-pin path"):
        R.validate_registry(registry)
