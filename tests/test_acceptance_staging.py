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


def record(case, close_id="attempt"):
    return close.load_close(case["store"], close_id)


def capsule(case, phase="open"):
    route = record(case)["acceptance_route"]
    return A.decode(A._retained(case["store"], route[f"preflight_{phase}_hash"]))


def attachment(case, change=None, *, commit=True, close_id="attempt"):
    from agenttalk import acceptance_staging as staging
    if commit:
        assert legacy.cold_phase(case, "commit", close_id, actor=case["plan"]["cold_policy"]["reviewer"]) == 0
    data = legacy.bundle_v2(case)
    data["schema_version"] = 4
    data.update(close_id=close_id, **{
        k: record(case, close_id)["acceptance_route"][k] for k in
        ("instance_id", "attempt_id", "revision", "plan_hash", "registry_hash", "project_id")})
    legacy.hygiene_bundle(case, data)
    envelope = {"schema_version": 1, "binding": staging.binding(record(case, close_id)),
                "observation": deepcopy(case["stage"]["observation"])}
    if change:
        change(envelope)
    path = case["inputs"] / "preflight.json"
    digest = legacy.write_json(path, envelope)
    data["preflight_observation"] = {"path": path.name, "sha256": digest, "size": path.stat().st_size}
    legacy.hygiene_bundle(case, data)
    for entry in envelope["observation"]["entries"]:
        for ref in (entry["banner"], entry["offline"]["log"]):
            shutil.copyfile(case["stage"]["root"] / ref["path"], case["inputs"] / ref["path"])
    legacy.write_json(case["inputs"] / "bundle.json", data)
    return data


def test_pending_freeze_ack_remains_unbound(candidate, monkeypatch):
    from agenttalk import acceptance_staging as staging
    def fail(*args, **kwargs):
        raise OSError("injected retention failure")
    monkeypatch.setattr(staging, "freeze", fail)
    assert open_candidate(candidate) != 0
    assert record(candidate)["acceptance_route"] == {"pending": True}
    partition = candidate["plan"]["partitions"][0]
    lens = "acceptance-run-" + partition["id"]
    assert legacy.ack_lens(candidate, lens, partition["agents"][0]) == 0
    assert "acceptance_binding" not in record(candidate)["lens_acks"][lens]


@pytest.mark.parametrize("fault", ["scope", "lens", "project"])
def test_invalid_open_does_not_hash(candidate, monkeypatch, fault):
    calls = []
    monkeypatch.setattr(P, "evaluate", lambda *a, **k: calls.append(True))
    if fault == "scope":
        candidate["plan"]["scope"] = "wrong"
    elif fault == "project":
        candidate["plan"]["project_id"] = "wrong"
    else:
        candidate["plan"]["partitions"][0]["agents"] = []
    save(candidate)
    assert open_candidate(candidate) != 0
    assert calls == []


@pytest.mark.parametrize("fault", ["cold", "second", "published"])
def test_invalid_attach_does_not_hash(candidate, monkeypatch, fault):
    assert open_candidate(candidate) == 0
    attachment(candidate, commit=fault != "cold")
    if fault == "second":
        assert legacy.attach(candidate) == 0
    elif fault == "published":
        legacy.publish_hold(candidate)
    calls = []
    monkeypatch.setattr(P, "evaluate", lambda *a, **k: calls.append(True))
    assert legacy.attach(candidate) != 0
    assert calls == []


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
    assert "advisory-manifest:pin" in refs
    assert ("java:banner" in refs) == (fault != "claimed-pass")
    assert ("java:log" in refs) == (fault != "claimed-pass")
    assert legacy.attach(candidate) != 0


@pytest.mark.parametrize("key", ["close_id", "instance_id", "attempt_id", "project_id", "revision",
                               "plan_hash", "registry_hash"])
def test_schema4_attach_binding_must_match(candidate, key):
    assert open_candidate(candidate) == 0
    attachment(candidate, lambda e: e["binding"].update({key: "different"}))
    assert legacy.attach(candidate) != 0
    route = record(candidate)["acceptance_route"]
    assert route["bundle_hash"] is route["preflight_attach_hash"] is None


def test_schema4_public_snapshot_private_locator_and_cold_guard(candidate):
    assert open_candidate(candidate) == 0
    attachment(candidate)
    assert legacy.attach(candidate) == 0
    result = A.resolve(candidate["store"], record(candidate), live=True)
    encoded_locator = json.dumps(str(candidate["stage"]["root"]))[1:-1]
    assert encoded_locator not in json.dumps(result)
    assert "acceptance_cold_missing" in {code for code, _ in A.evaluate(result)}
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
    attachment(candidate)
    original = P.evaluate

    def cold_arrives(*args, **kwargs):
        report = original(*args, **kwargs)
        with close.close_transaction(candidate["store"], "attempt") as transaction:
            transaction.record["acceptance_route"]["cache_root"] += "/changed"
            transaction.commit()
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


def test_preflight_binding_and_ack_keys_are_literal(candidate):
    from agenttalk import acceptance_staging as staging
    assert open_candidate(candidate) == 0
    assert set(staging.binding(record(candidate))) == {
        "close_id", "instance_id", "attempt_id", "project_id", "revision", "plan_hash", "registry_hash"}
    assert set(A.ack_binding(record(candidate))) == {
        "instance_id", "attempt_id", "revision", "plan_hash", "registry_hash", "bundle_hash",
        "cold_commit_hash", "cold_reconcile_hash", "obligations_hash", "environment_hash",
        "preflight_open_hash", "preflight_attach_hash"}


@pytest.mark.parametrize("fault", ["item-field", "inputs", "version", "observation"])
def test_capture_nested_structure_and_observation_are_revalidated(candidate, fault):
    from agenttalk import acceptance_staging as staging
    assert open_candidate(candidate) == 0
    attachment(candidate)
    assert legacy.attach(candidate) == 0
    value = capsule(candidate, "attach")
    if fault == "item-field":
        value["inputs"][0]["unexpected"] = True
    elif fault == "inputs":
        value["inputs"] = {}
    elif fault == "version":
        value["schema_version"] = 2
    else:
        value["observation_hash"] = "f" * 64
    with close.close_transaction(candidate["store"], "attempt") as transaction:
        transaction.record["acceptance_route"]["preflight_attach_hash"] = A._retain(
            candidate["store"], staging.canonical(value))
        transaction.commit()
    with pytest.raises(A.AcceptanceError):
        staging.pending_snapshot(candidate["store"], record(candidate))


@pytest.mark.parametrize("fault", ["ref-field", "version"])
def test_attachment_closed_ref_and_envelope_version(candidate, fault):
    from agenttalk import acceptance_staging as staging
    assert open_candidate(candidate) == 0
    data = attachment(candidate, (lambda e: e.update(schema_version=2)) if fault == "version" else None)
    if fault == "ref-field":
        data["preflight_observation"]["unexpected"] = True
        legacy.write_json(candidate["inputs"] / "bundle.json", data)
    with pytest.raises(A.AcceptanceError):
        staging.prepare_attachment(candidate["store"], "attempt", candidate["inputs"] / "bundle.json", data)
    assert legacy.attach(candidate) == 2


def test_preflight_attachment_refuses_legacy_route(case_v3):
    from agenttalk import acceptance_staging as staging
    assert legacy.open_attempt(case_v3) == 0
    with pytest.raises(A.AcceptanceError, match="schema-4 route"):
        staging.prepare_attachment(case_v3["store"], "attempt", case_v3["inputs"] / "bundle.json", {})


def test_legacy_open_refuses_cache_argument(case_v3):
    assert legacy.open_attempt(case_v3, "--cache-root", str(case_v3["inputs"])) != 0
    assert not close.close_path(case_v3["store"], "attempt").exists()


def test_project_input_link_keeps_typed_refusal(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    with pytest.raises(A.LinkedPathError):
        A._path(tmp_path, "plan.json")


def test_capture_retains_both_environment_roles_and_banner(staged):
    override = deepcopy(staged["plan"]["environment"])
    ref = {"path": "override.json", "size": 0, "sha256": "a" * 64}
    preflight.evidence(staged, ref, preflight.encoded(override))
    for obj in (staged["plan"], staged["observation"]):
        obj["environment"]["row_overrides"] = [{"id": "build", "environment": deepcopy(ref)}]
    captured = []
    assert preflight.run(staged, capture=captured)["status"] == "pass"
    data = [raw for _, raw in captured]
    assert data.count(preflight.encoded(override)) == 2
    banner = staged["root"] / staged["observation"]["entries"][0]["banner"]["path"]
    assert banner.read_bytes() in data
    refs = [ref for ref, _ in captured]
    assert len(set(refs)) == len(refs)
    assert {"build:planned", "build:observed", "java:banner", "java:log"} <= set(refs)


def complete(candidate):
    assert open_candidate(candidate) == 0
    data = attachment(candidate)
    assert legacy.attach(candidate) == 0
    assert legacy.cold_phase(candidate, "reconcile") == 0
    legacy.final_accepts(candidate, data)


def test_schema4_live_check_and_publish_reach_go(candidate):
    complete(candidate)
    assert legacy.command(candidate, "check", "--id", "attempt") == 0
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 0
    saved = record(candidate)["final"]["acceptance_snapshot"]
    assert "status" not in saved["preflight"]


@pytest.mark.parametrize("fault", ["mutation", "deletion", "expiry"])
def test_schema4_live_prerequisites_rechecked_before_go(candidate, monkeypatch, fault):
    from datetime import timedelta
    complete(candidate)
    assert legacy.command(candidate, "check", "--id", "attempt") == 0
    path = candidate["stage"]["root"] / "jdk.dat"
    if fault == "mutation":
        path.write_bytes(b"changed staged bytes")
    elif fault == "deletion":
        path.unlink()
    else:
        monkeypatch.setattr(P, "decision_time", lambda: preflight.NOW + timedelta(days=400))
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3
    assert record(candidate)["status"] != close.PUBLISHED


def test_publish_hashes_outside_lock_and_checks_metadata_inside(candidate, monkeypatch):
    from agenttalk import acceptance_live
    complete(candidate)
    read, recheck = P._read_pin, acceptance_live.recheck
    reads, checks = [], []
    def observed_read(*args, **kwargs):
        assert not getattr(close._writer_locks, "held", set())
        reads.append(True)
        return read(*args, **kwargs)
    def observed_recheck(*args, **kwargs):
        assert getattr(close._writer_locks, "held", set())
        checks.append(True)
        return recheck(*args, **kwargs)
    monkeypatch.setattr(P, "_read_pin", observed_read)
    monkeypatch.setattr(acceptance_live, "recheck", observed_recheck)
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 0
    assert reads and checks


@pytest.mark.parametrize("fault", ["changed", "missing", "expired", "route"])
def test_publish_rechecks_after_hashing_before_durable_go(candidate, monkeypatch, fault):
    from contextlib import contextmanager
    from datetime import timedelta
    complete(candidate)
    original = close.close_transaction
    @contextmanager
    def after_hash(*args, **kwargs):
        path = candidate["stage"]["root"] / "jdk.dat"
        if fault == "changed":
            path.write_bytes(b"changed at publication boundary")
        elif fault == "missing":
            path.unlink()
        elif fault == "expired":
            monkeypatch.setattr(P, "decision_time", lambda: preflight.NOW + timedelta(days=400))
        with original(*args, **kwargs) as tx:
            if fault == "route":
                tx.record["acceptance_route"]["cache_root"] += "/changed"
            yield tx
    monkeypatch.setattr(close, "close_transaction", after_hash)
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3
    assert record(candidate)["status"] != close.PUBLISHED


def test_historical_cache_absence_is_visible_without_rewriting_verdict(candidate, capsys):
    complete(candidate)
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 0
    (candidate["stage"]["root"] / "jdk.dat").unlink()
    result = A.resolve(candidate["store"], record(candidate))
    historical = result["preflight"]["historical"]
    assert historical and all(p["status"] == "artifact not retained, pinned by digest" for p in historical)
    assert not A.evaluate(result)
    assert record(candidate)["final"]["verdict"] == "GO"
    capsys.readouterr()
    assert legacy.command(candidate, "check", "--id", "attempt", "--json") == 3  # already published
    assert json.loads(capsys.readouterr().out)["preflight"]["historical"] == historical


def test_preflight_seal_includes_all_retained_inputs(candidate):
    from agenttalk import acceptance_staging as staging
    complete(candidate)
    result = A.resolve(candidate["store"], record(candidate))
    route = record(candidate)["acceptance_route"]
    expected = {route[key] for key in ("environment_hash", "preflight_open_hash", "preflight_attach_hash")}
    for phase in ("open", "attach"):
        captured = capsule(candidate, phase)
        expected.update(item["sha256"] for item in captured["inputs"])
        if captured["observation_hash"]:
            expected.add(captured["observation_hash"])
    assert staging.evidence(candidate["store"], record(candidate)) == expected
    assert expected <= set(result["hygiene"]["sealed_manifest"])
    digest = capsule(candidate, "attach")["inputs"][-1]["sha256"]
    (candidate["store"].dir / "acceptance" / "sha256" / digest).unlink()
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


def test_schema4_successor_hashes_before_parent_lock(candidate, monkeypatch):
    complete(candidate)
    legacy.publish_hold(candidate)
    original = P._read_pin
    calls = []
    def read(*args, **kwargs):
        assert not getattr(close._writer_locks, "held", set())
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(P, "_read_pin", read)
    assert legacy.command(candidate, "acceptance", "successor", "--id", "next", "--parent", "attempt",
                          "--from", "lead", "--acceptance-plan", str(candidate["inputs"] / "plan.json"),
                          "--project-repo", str(candidate["project"]), "--revision", candidate["sha"],
                          "--cache-root", str(candidate["stage"]["root"]), "--reason", "fresh attempt") == 0
    assert calls
    assert A.schema(close.load_close(candidate["store"], "next")["acceptance_route"]["schema_version"]).preflight


def test_ld2_preserves_registry_requirements(candidate):
    from agenttalk import acceptance_coverage as coverage
    protected = coverage.group(candidate["plan"]["rows"])
    plan = deepcopy(candidate["plan"])
    for row in plan["rows"]:
        row["registry_entries"] = []
    assert coverage.changes(protected, plan)


def test_schema4_route_cannot_downgrade_to_schema3(candidate):
    complete(candidate)
    value = record(candidate)
    value["acceptance_route"]["schema_version"] = 3
    assert A.evaluate(A.resolve(candidate["store"], value))


def test_documented_java_staging_example(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path
    example = Path(__file__).parents[1] / "docs" / "examples"
    runpy.run_path(str(example / "stage_acceptance.py"))["stage"](tmp_path)
    assert json.loads((tmp_path / "registry.json").read_text(encoding="utf-8")) == json.loads(
        (example / "java-registry.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(P, "decision_time", lambda: preflight.NOW)
    result = P.evaluate((tmp_path / "plan.json").read_bytes(), (tmp_path / "registry.json").read_bytes(),
                        tmp_path, observation_bytes=(tmp_path / "observation.json").read_bytes())
    assert result["status"] == "pass"
    (tmp_path / "jdk-21.zip").unlink()
    result = P.evaluate((tmp_path / "plan.json").read_bytes(), (tmp_path / "registry.json").read_bytes(),
                        tmp_path, observation_bytes=(tmp_path / "observation.json").read_bytes())
    assert result["status"] == "not-run"
    assert (P.UNAVAILABLE, "jdk") in {(h["code"], h["ref"]) for h in result["holds"]}


def test_schema4_successor_retains_history_and_can_reach_go(candidate):
    from agenttalk import acceptance_staging as staging
    complete(candidate)
    legacy.publish_hold(candidate)
    old_evidence = staging.evidence(candidate["store"], record(candidate))
    legacy.assign_fresh_cold(candidate, "next-cold")
    save(candidate)
    assert legacy.command(candidate, "acceptance", "successor", "--id", "next", "--parent", "attempt",
                          "--from", "lead", "--acceptance-plan", str(candidate["inputs"] / "plan.json"),
                          "--project-repo", str(candidate["project"]), "--revision", candidate["sha"],
                          "--cache-root", str(candidate["stage"]["root"]), "--reason", "fresh attempt") == 0
    data = attachment(candidate, close_id="next")
    assert legacy.command(candidate, "acceptance", "attach", "--id", "next", "--from", "lead",
                          "--file", str(candidate["inputs"] / "bundle.json")) == 0
    assert legacy.cold_phase(candidate, "reconcile", "next", actor="next-cold") == 0
    legacy.final_accepts(candidate, data, "next")
    result = A.resolve(candidate["store"], record(candidate, "next"))
    assert old_evidence <= set(result["hygiene"]["sealed_manifest"])
    assert legacy.command(candidate, "publish", "--id", "next", "--from", "lead", "--verdict", "go") == 0


def test_staged_change_during_hashing_is_not_a_verified_read(candidate, monkeypatch):
    from pathlib import Path
    complete(candidate)
    original = P._read_pin
    def changed(root, pin, budget, **kwargs):
        result = original(root, pin, budget, **kwargs)
        if pin.get("id") == "jdk":
            (Path(root) / pin["path"]).write_bytes(b"changed during hashing")
        return result
    monkeypatch.setattr(P, "_read_pin", changed)
    assert legacy.command(candidate, "publish", "--id", "attempt", "--from", "lead", "--verdict", "go") == 3


def test_conflicting_lens_assignment_refuses_before_evaluation(candidate, monkeypatch):
    calls = []
    monkeypatch.setattr(P, "evaluate", lambda *a, **k: calls.append(True))
    lens = "acceptance-run-" + candidate["plan"]["partitions"][0]["id"]
    assert legacy.open_attempt(candidate, "--cache-root", str(candidate["stage"]["root"]),
                               "--lens", lens, "--allow", lens + ":wrong-agent") != 0
    assert calls == []
