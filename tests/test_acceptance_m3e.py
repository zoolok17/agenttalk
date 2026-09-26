"""Environment amendments and retryable staged identity races."""

from copy import deepcopy
import json
import os

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
    ("timezone", "other"), ("scratch", "other"), ("cache_overlay", "other"), ("service_data", "other")])
def test_coverage_binds_each_environment_field(plan, registry, field, value):
    old = C.group(plan["rows"], registry, plan["environment"])
    plan["environment"][field] = value
    assert C.changes(old, plan, registry)


def test_coverage_override_is_per_row_and_locator_independent(plan, registry):
    pin = {"path": "override.json", "sha256": "a" * 64, "size": 2}
    plan["environment"]["row_overrides"] = [{"id": "build", "environment": pin}]
    old = C.group(plan["rows"], registry, plan["environment"])
    pin["path"] = "relocated.json"
    plan["environment"]["row_overrides"].append({"id": "other-row", "environment": dict(pin)})
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
