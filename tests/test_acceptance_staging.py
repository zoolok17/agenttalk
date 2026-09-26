"""Schema-4 lifecycle capture uses synthetic staging and session Git templates."""

from copy import deepcopy
import json
import shutil

import pytest

from agenttalk import acceptance as A, acceptance_preflight as P, close
import test_acceptance as legacy
import test_acceptance_preflight as preflight

case, case_v2, case_v3 = legacy.case, legacy.case_v2, legacy.case_v3
environment, observation, plan, registry = (preflight.environment, preflight.observation,
                                          preflight.plan, preflight.registry)
staged = preflight.staged


@pytest.fixture
def candidate(case_v3, staged):
    case = case_v3
    case["plan"].update(schema_version=4, environment=deepcopy(staged["plan"]["environment"]))
    for row in case["plan"]["rows"]:
        row["registry_entries"] = ["java"]
    staged["plan"] = case["plan"]
    case["stage"] = staged
    save(case)
    return case


def save(case):
    preflight.bind(case["stage"])
    legacy.write_json(case["inputs"] / "registry.json", case["stage"]["registry"])
    # write_json sorts keys; the digest must bind those exact bytes.
    digest = A._hash((case["inputs"] / "registry.json").read_bytes())
    case["plan"]["registry_digest"] = digest
    case["stage"]["observation"]["registry_hash"] = digest
    legacy.write_json(case["inputs"] / "plan.json", case["plan"])


def open_candidate(case):
    return legacy.open_attempt(case, "--cache-root", str(case["stage"]["root"]))


def record(case):
    return close.load_close(case["store"], "attempt")


def capsule(case, phase="open"):
    route = record(case)["acceptance_route"]
    return A.decode(A._retained(case["store"], route[f"preflight_{phase}_hash"]))


def attachment(case, change=None, *, commit=True):
    from agenttalk import acceptance_staging as staging
    if commit:
        assert legacy.cold_phase(case, "commit") == 0
    data = legacy.bundle_v2(case)
    data["schema_version"] = 4
    legacy.hygiene_bundle(case, data)
    envelope = {"schema_version": 1, "binding": staging.binding(record(case)),
                "observation": deepcopy(case["stage"]["observation"])}
    if change:
        change(envelope)
    path = case["inputs"] / "preflight.json"
    digest = legacy.write_json(path, envelope)
    data["preflight_observation"] = {"path": path.name, "sha256": digest, "size": path.stat().st_size}
    for entry in envelope["observation"]["entries"]:
        for ref in (entry["banner"], entry["offline"]["log"]):
            shutil.copyfile(case["stage"]["root"] / ref["path"], case["inputs"] / ref["path"])
    legacy.write_json(case["inputs"] / "bundle.json", data)
    return data


@pytest.mark.parametrize("fault", [None, "missing", "changed", "expired", "absent-cache"])
def test_schema4_open_freezes_bound_hold_without_acquisition(candidate, fault):
    if fault == "missing":
        (candidate["stage"]["root"] / "jdk.dat").unlink()
    elif fault == "changed":
        (candidate["stage"]["root"] / "jdk.dat").write_bytes(b"changed")
    elif fault == "expired":
        candidate["stage"]["registry"]["files"][-1]["expires_at"] = "2026-06-01T00:00:00Z"
        save(candidate)
    elif fault == "absent-cache":
        candidate["stage"]["root"] /= "absent-cache"
    assert open_candidate(candidate) == 0
    value = capsule(candidate)
    route = record(candidate)["acceptance_route"]
    assert value["binding"]["attempt_id"] == route["attempt_id"]
    assert value["binding"]["registry_hash"] == route["registry_hash"]
    codes = {h["code"] for h in value["report"]["holds"]}
    assert {None: P.UNPROVEN, "missing": P.UNAVAILABLE, "changed": P.MISMATCH,
            "expired": P.EXPIRED, "absent-cache": P.UNAVAILABLE}[fault] in codes
    assert A._retained(candidate["store"], route["environment_hash"])


@pytest.mark.parametrize("fault", ["digest", "unknown", "escape", "cache-escape"])
def test_schema4_bad_policy_refuses_before_route_creation(candidate, fault):
    if fault == "digest":
        candidate["plan"]["registry_digest"] = "0" * 64
    elif fault == "unknown":
        candidate["plan"]["unexpected"] = True
    elif fault == "escape":
        candidate["stage"]["registry"]["files"][0]["path"] = "../outside"
        save(candidate)
    else:
        candidate["stage"]["root"] /= ".."
    legacy.write_json(candidate["inputs"] / "plan.json", candidate["plan"])
    assert open_candidate(candidate) != 0
    assert not close.close_path(candidate["store"], "attempt").exists()


@pytest.mark.parametrize("fault", [None, "changed-pin", "claimed-pass"])
def test_schema4_attach_recomputes_and_retains_proof(candidate, fault):
    assert open_candidate(candidate) == 0
    if fault == "changed-pin":
        (candidate["stage"]["root"] / "jdk.dat").write_bytes(b"changed since open")
    change = (lambda e: e["observation"].update(status="pass")) if fault == "claimed-pass" else None
    attachment(candidate, change)
    assert legacy.attach(candidate) == 0
    value = capsule(candidate, "attach")
    assert value["report"]["status"] == ("pass" if fault is None else "fail")
    assert value["observation_hash"]
    for item in value["inputs"]:
        assert len(A._retained(candidate["store"], item["sha256"])) == item["size"]
    refs = {i["ref"] for i in value["inputs"]}
    assert "advisory-manifest" in refs
    assert ("java" in refs) == (fault != "claimed-pass")
    assert legacy.attach(candidate) != 0


@pytest.mark.parametrize("key", ["close_id", "instance_id", "attempt_id", "project_id", "revision",
                               "plan_hash", "registry_hash"])
def test_schema4_attach_binding_must_match(candidate, key):
    assert open_candidate(candidate) == 0
    attachment(candidate, lambda e: e["binding"].update({key: "different"}))
    assert legacy.attach(candidate) != 0
    route = record(candidate)["acceptance_route"]
    assert route["bundle_hash"] is route["preflight_attach_hash"] is None


def test_schema4_public_snapshot_private_locator_and_m3a_go_guard(candidate):
    assert open_candidate(candidate) == 0
    attachment(candidate)
    assert legacy.attach(candidate) == 0
    result = A.resolve(candidate["store"], record(candidate), live=True)
    encoded_locator = json.dumps(str(candidate["stage"]["root"]))[1:-1]
    assert encoded_locator not in json.dumps(result)
    assert P.UNAVAILABLE in {code for code, _ in A.evaluate(result)}
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


def test_schema4_distributions_are_not_retained(candidate):
    pin = candidate["stage"]["registry"]["files"][0]
    assert pin["role"] == "distribution"
    preflight.evidence(candidate["stage"], pin, b"unique synthetic binary, not evidence")
    save(candidate)
    assert open_candidate(candidate) == 0
    assert not (candidate["store"].dir / "acceptance" / "sha256" / pin["sha256"]).exists()
    assert pin["id"] not in {i["ref"] for i in capsule(candidate)["inputs"]}


def test_schema4_linked_cache_refuses_without_private_path(candidate, monkeypatch, capsys):
    from agenttalk import acceptance_registry as R
    original = R.staged_path

    def linked(root, relative):
        if str(root) == str(candidate["stage"]["root"]):
            raise A.LinkedPathError("staging root contains a link/reparse point")
        return original(root, relative)

    monkeypatch.setattr(R, "staged_path", linked)
    assert open_candidate(candidate) == 3
    output = capsys.readouterr()
    assert "acceptance_policy_invalid" in output.err
    assert str(candidate["stage"]["root"]) not in output.err + output.out
    assert not close.close_path(candidate["store"], "attempt").exists()


@pytest.mark.parametrize("fault", ["envelope-pin", "envelope-field", "missing-proof", "changed-proof"])
def test_schema4_attachment_refusal_and_failed_evidence(candidate, fault, capsys):
    assert open_candidate(candidate) == 0
    data = attachment(candidate, (lambda e: e.update(pass_flag=True)) if fault == "envelope-field" else None)
    if fault == "envelope-pin":
        data["preflight_observation"]["sha256"] = "0" * 64
        legacy.write_json(candidate["inputs"] / "bundle.json", data)
    elif fault in ("missing-proof", "changed-proof"):
        path = candidate["inputs"] / candidate["stage"]["observation"]["entries"][0]["offline"]["log"]["path"]
        if fault == "missing-proof":
            path.unlink()
        else:
            path.write_bytes(b"altered proof")
    result = legacy.attach(candidate)
    if fault.startswith("envelope"):
        # Attach keeps the shipped CLI's invalid-input exit code.
        assert result == 2
        expected = "acceptance_record_missing" if fault == "envelope-pin" else "acceptance_policy_invalid"
        assert expected in capsys.readouterr().err
        assert record(candidate)["acceptance_route"]["bundle_hash"] is None
    else:
        assert result == 0
        value = capsule(candidate, "attach")
        assert value["report"]["status"] != "pass"
        if fault == "changed-proof":
            assert any(A._retained(candidate["store"], item["sha256"]) == b"altered proof"
                       for item in value["inputs"])


@pytest.mark.parametrize("fault", ["environment", "open-report", "declarative", "binding"])
def test_schema4_retained_capture_corruption_holds(candidate, fault):
    assert open_candidate(candidate) == 0
    route = record(candidate)["acceptance_route"]
    value = capsule(candidate)
    digest = {"environment": route["environment_hash"], "open-report": route["preflight_open_hash"],
              "declarative": value["inputs"][0]["sha256"], "binding": None}[fault]
    if digest:
        (candidate["store"].dir / "acceptance" / "sha256" / digest).write_bytes(b"tampered")
    else:
        with close.close_transaction(candidate["store"], "attempt") as transaction:
            transaction.record["acceptance_route"]["attempt_id"] = "changed-attempt"
            transaction.commit()
    result = A.resolve(candidate["store"], record(candidate))
    assert {code for code, _ in result["holds"]} & {
        "acceptance_record_missing", "acceptance_plan_stale", "acceptance_row_unbound"}


@pytest.mark.parametrize("fault", ["boolean-size", "bad-ref", "empty-digest", "extra-field"])
def test_schema4_capsule_manifest_is_strict(candidate, fault):
    from agenttalk import acceptance_staging as staging
    assert open_candidate(candidate) == 0
    value = capsule(candidate)
    if fault == "boolean-size":
        value["inputs"][0].update(size=True, sha256=A._retain(candidate["store"], b"x"))
    elif fault == "bad-ref":
        value["inputs"][0]["ref"] = "invalid ref"
    elif fault == "empty-digest":
        value["observation_hash"] = ""
    else:
        value["extra"] = True
    with close.close_transaction(candidate["store"], "attempt") as transaction:
        transaction.record["acceptance_route"]["preflight_open_hash"] = A._retain(
            candidate["store"], staging.canonical(value))
        transaction.commit()
    result = A.resolve(candidate["store"], record(candidate))
    assert "acceptance_policy_invalid" in {code for code, _ in result["holds"]}


def test_schema4_route_changed_during_preflight_does_not_attach(candidate, monkeypatch, capsys):
    assert open_candidate(candidate) == 0
    attachment(candidate, commit=False)
    original = P.evaluate

    def cold_arrives(*args, **kwargs):
        report = original(*args, **kwargs)
        assert legacy.cold_phase(candidate, "commit") == 0
        return report

    monkeypatch.setattr(P, "evaluate", cold_arrives)
    assert legacy.attach(candidate) == 2
    assert "route changed during preflight capture" in capsys.readouterr().err
    route = record(candidate)["acceptance_route"]
    assert route["cold_commit_hash"]
    assert route["bundle_hash"] is route["preflight_attach_hash"] is None


def test_schema4_report_and_bundle_are_paired(candidate):
    assert open_candidate(candidate) == 0
    value = record(candidate)
    value["acceptance_route"]["preflight_attach_hash"] = value["acceptance_route"]["preflight_open_hash"]
    with pytest.raises(A.AcceptanceError) as error:
        A._route(value)
    assert error.value.code == "acceptance_row_unbound"


@pytest.mark.parametrize("locator", ["relative-cache", "parent", "nul"])
def test_schema4_stored_cache_locator_has_closed_syntax(candidate, locator):
    assert open_candidate(candidate) == 0
    value = record(candidate)
    root = str(candidate["stage"]["root"])
    value["acceptance_route"]["cache_root"] = {
        "relative-cache": "relative-cache", "parent": root + "/../cache", "nul": root + "\x00"}[locator]
    with pytest.raises(A.AcceptanceError):
        A._route(value)


def test_legacy_prepare_keeps_scope_refusal_before_registry_read(case):
    case["plan"]["scope"] = "different"
    legacy.write_json(case["inputs"] / "plan.json", case["plan"])
    (case["inputs"] / "registry.json").unlink()
    with pytest.raises(A.AcceptanceError, match="plan scope differs"):
        A.prepare(case["store"], case["inputs"] / "plan.json", case["project"], case["sha"], "milestone")
