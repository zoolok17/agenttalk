"""Trial evidence never votes; every assigned check and every transport link is required."""

import copy
import json
import zipfile
from pathlib import Path

import pytest

from agenttalk import dev_gate as gate
from agenttalk import dev_gate_trial as trial
from test_dev_gate import _leg_artifact, _manifest


@pytest.fixture
def manifest():
    return _manifest()


@pytest.fixture
def context(manifest):
    record = _leg_artifact(manifest, "windows/3.12")
    return {"candidate_sha": record["subject"]["candidate_sha"], "candidate_tree": record["subject"]["candidate_tree"],
            "manifest_sha256": record["manifest"]["sha256"], "repository": "test/repo", "run_id": "42",
            "attempt": "2", "workflow": "test/repo/.github/workflows/tests.yml@refs/pull/1/merge"}


def bundle(directory, manifest, context, mode, leg="windows/3.12"):
    directory.mkdir()
    record = _leg_artifact(manifest, leg)
    assigned = trial.partition(manifest, leg, mode)
    for index, check in enumerate(record["checks"]):
        if check["id"] not in assigned:
            record["checks"][index] = gate._blocked_record(check["id"], "other trial mode", directory / "original")
        else:
            log = directory / "original" / (check["id"] + ".log")
            log.parent.mkdir(exist_ok=True)
            log.write_text("passed\n", encoding="utf-8")
            check["log"] = {"path": str(log), "sha256": gate._sha256_file(log)}
    if mode == "source":
        record["artifacts"] = {}
        record["external_inputs"] = []
    else:
        for item in record["artifacts"].values():
            path = directory / "packages" / item["filename"]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"test package bytes")
            item.update(sha256=gate._sha256_file(path), size_bytes=path.stat().st_size)
    record["verdict"] = "block"
    record["blockers"] = gate._blockers_for_checks(record["checks"])
    record["summary"] = {"required": len(record["checks"]), "passed": len(assigned),
                         "blocked": len(record["checks"]) - len(assigned)}
    gate.write_run_evidence(directory / "partial.json", record, manifest)
    installs = {}
    if mode == "wheel":
        minor = leg.split("/")[1]
        python = next(c for c in record["checks"] if c["id"].startswith("pytest-wheel-"))["tool"]["path"]
        wheel = record["artifacts"]["wheel"]
        log = directory / "wheel-test-install.log"
        log.write_text("installed candidate\n", encoding="utf-8")
        installs[minor] = {"wheel_sha256": wheel["sha256"], "python_path": python, "exit_code": 0,
                          "argv": gate.isolated_tool_argv(python, "pip", "install", "--disable-pip-version-check",
                                                        "--no-input", "--no-cache-dir", "--index-url",
                                                        manifest["checks"]["wheel-contract"]["dependency_index"],
                                                        wheel["path"]),
                          "log": {"artifact_path": log.name, "sha256": gate._sha256_file(log)}}
    envelope = {"artifact_type": trial.TYPE, "authoritative": False, "status": "complete", "context": context,
                "leg": leg, "mode": mode, "assigned": assigned, "wheel_test_installs": installs}
    seal(directory, envelope)
    return envelope


def seal(directory, envelope):
    envelope["files"] = {p.relative_to(directory).as_posix(): gate._sha256_file(p)
                         for p in directory.rglob("*") if p.is_file() and p.name != "trial.json"}
    trial.write_json(directory / "trial.json", envelope)


@pytest.mark.parametrize("leg", ["windows/3.10", "windows/3.11", "windows/3.12", "windows/3.13"])
def test_partition_preserves_every_manifest_check_and_duplicates_only_bindings(manifest, leg):
    source, wheel = [set(trial.partition(manifest, leg, mode)) for mode in trial.MODES]
    assert source | wheel == set(gate.required_check_ids(manifest, execution_scope="ci-leg", ci_leg=leg))
    assert source & wheel == {"git-binding", "final-binding"}
    assert "package-build" in wheel
    assert any(c.startswith("wheel-contract-") for c in wheel)


@pytest.mark.parametrize("mode", trial.MODES)
def test_partial_result_validates_but_cannot_claim_a_full_gate(tmp_path, manifest, context, mode):
    directory = tmp_path / mode
    bundle(directory, manifest, context, mode)
    record = trial.validate_part(directory, context, manifest, "windows/3.12", mode)
    assert record["verdict"] == "block" and record["complete"] is False
    forged = copy.deepcopy(record)
    forged["verdict"] = "pass"
    with pytest.raises(gate.GateBlock, match="verdict"):
        gate.validate_run_artifact(forged, manifest, bundle_root=directory)


@pytest.mark.parametrize("field", ["candidate_sha", "candidate_tree", "manifest_sha256", "repository", "run_id",
                                  "attempt", "workflow"])
def test_mixed_context_blocks(tmp_path, manifest, context, field):
    directory = tmp_path / "source"
    envelope = bundle(directory, manifest, context, "source")
    envelope["context"] = {**context, field: "different"}
    trial.write_json(directory / "trial.json", envelope)
    with pytest.raises(gate.GateBlock, match="mixed"):
        trial.validate_part(directory, context, manifest, "windows/3.12", "source")


@pytest.mark.parametrize("damage", ["missing", "corrupt", "log-digest", "package-digest", "tested-wheel",
                                   "test-interpreter", "failed-install", "mode", "assigned", "cancelled", "voting"])
def test_bad_or_incomplete_proofs_block(tmp_path, manifest, context, damage):
    directory = tmp_path / "wheel"
    envelope = bundle(directory, manifest, context, "wheel")
    if damage == "missing":
        (directory / "partial.json").unlink()
    elif damage == "corrupt":
        (directory / "partial.json").write_text("corrupt", encoding="utf-8")
    elif damage == "log-digest":
        (directory / "logs/package-build.log").write_text("changed", encoding="utf-8")
        seal(directory, envelope)  # inner proof must still reject it
    elif damage == "package-digest":
        (directory / "packages/agenttalk.whl").write_bytes(b"another wheel")
        seal(directory, envelope)
    elif damage in {"tested-wheel", "test-interpreter", "failed-install"}:
        proof = envelope["wheel_test_installs"]["3.12"]
        key, value = {"tested-wheel": ("wheel_sha256", "wrong"),
                      "test-interpreter": ("python_path", "wrong"), "failed-install": ("exit_code", 1)}[damage]
        proof[key] = value
        trial.write_json(directory / "trial.json", envelope)
    else:
        key, value = {"mode": ("mode", "source"), "assigned": ("assigned", []),
                      "cancelled": ("status", "cancelled"), "voting": ("authoritative", True)}[damage]
        envelope[key] = value
        trial.write_json(directory / "trial.json", envelope)
    with pytest.raises(gate.GateBlock):
        trial.validate_part(directory, context, manifest, "windows/3.12", "wheel")


def inputs(manifest, context):
    artifacts, jobs = [], []
    for leg in gate.expected_ci_legs(manifest):
        if not leg.startswith("windows/"):
            continue
        for mode in trial.MODES:
            artifacts.append({"id": len(artifacts) + 1, "name": trial.artifact_name(context, leg, mode),
                              "expired": False, "workflow_run": {"id": 42}, "digest": "sha256:" + "a" * 64})
            jobs.append({"id": len(jobs) + 10, "name": trial.job_name(leg, mode), "run_id": 42,
                         "run_attempt": 2, "status": "completed", "conclusion": "success"})
    return artifacts, jobs


def test_exact_matrix_selected_by_id_and_attempt_not_download_name(manifest, context):
    artifacts, jobs = inputs(manifest, context)
    old = {**artifacts[0], "id": 900, "name": artifacts[0]["name"].replace("-42-2-", "-42-1-")}
    chosen = trial.select_inputs([old, *artifacts], jobs, context, manifest)
    assert len(chosen) == 8 and {a["id"] for _, _, a, _ in chosen} == set(range(1, 9))


@pytest.mark.parametrize("damage", ["one-mode", "missing", "duplicate", "cancelled", "job-missing", "job-duplicate",
                                   "mixed-attempt", "mixed-run", "wrong-artifact-run", "no-digest", "expired"])
def test_missing_duplicate_cancelled_and_mixed_inputs_block(manifest, context, damage):
    artifacts, jobs = inputs(manifest, context)
    if damage == "one-mode":
        artifacts = [a for a in artifacts if a["name"].endswith("source")]
    elif damage == "missing":
        artifacts.pop()
    elif damage == "duplicate":
        artifacts.append(copy.deepcopy(artifacts[0]))
    elif damage == "job-missing":
        jobs.pop()
    elif damage == "job-duplicate":
        jobs.append(copy.deepcopy(jobs[0]))
    elif damage == "cancelled":
        jobs[0]["conclusion"] = "cancelled"
    elif damage == "mixed-attempt":
        jobs[0]["run_attempt"] = 1
    elif damage == "mixed-run":
        jobs[0]["run_id"] = 41
    elif damage == "wrong-artifact-run":
        artifacts[0]["workflow_run"]["id"] = 41
    elif damage == "no-digest":
        artifacts[0]["digest"] = ""
    elif damage == "expired":
        artifacts[0]["expired"] = True
    with pytest.raises(gate.GateBlock):
        trial.select_inputs(artifacts, jobs, context, manifest)


def test_failed_only_rerun_cannot_borrow_success_from_previous_attempt(manifest, context):
    artifacts, jobs = inputs(manifest, context)
    artifacts[0]["name"] = artifacts[0]["name"].replace("-42-2-", "-42-1-")
    with pytest.raises(gate.GateBlock, match="rerun all"):
        trial.select_inputs(artifacts, jobs, context, manifest)


def test_trial_workflow_does_not_change_the_required_gate_or_deadlines(manifest):
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    original, extra = workflow.split("  windows-mode-trial:")
    assert "needs: dev-gate-leg" in original
    assert "windows-mode-trial" not in original
    assert "mode: [source, wheel]" in extra and "timeout-minutes: 330" in extra
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in extra
    assert manifest["checks"]["pytest"]["windows_timeout_seconds"] == 9000
    assert "2026-11-06" in manifest["checks"]["pytest"]["windows_timeout_reason"]


@pytest.mark.parametrize("mode,leg", [("source", None), ("wheel", "linux/3.12"), ("both", "windows/3.12")])
def test_trial_cannot_change_local_or_other_platform_execution(tmp_path, monkeypatch, manifest, mode, leg):
    from types import SimpleNamespace
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: SimpleNamespace())
    monkeypatch.setattr(gate, "load_bound_manifest", lambda binding: manifest)
    with pytest.raises(gate.GateBlock, match="trial modes"):
        gate.execute_gate(root=tmp_path, ci_leg=leg, trial_mode=mode)


@pytest.mark.parametrize("corrupt_download", [False, True])
def test_collector_verifies_downloaded_zip_digests_and_each_inner_proof(
    tmp_path, monkeypatch, manifest, context, corrupt_download,
):
    artifacts, jobs = inputs(manifest, context)
    blobs = {}
    for artifact, job in zip(artifacts, jobs, strict=True):
        suffix = artifact["name"].split("-windows-")[1]
        minor, mode = suffix.split("-")
        directory = tmp_path / str(artifact["id"])
        bundle(directory, manifest, context, mode, f"windows/{minor}")
        archive = tmp_path / f"{artifact['id']}.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            for path in directory.rglob("*"):
                if path.is_file():
                    zipped.write(path, path.relative_to(directory).as_posix())
        artifact["digest"] = "sha256:" + gate._sha256_file(archive)
        blobs[artifact["id"]] = archive.read_bytes()
        job.update(started_at="2026-10-07T00:01:00Z", completed_at="2026-10-07T00:04:00Z",
                   steps=[{"name": "Run trial mode", "started_at": "2026-10-07T00:02:00Z"}])
    downloaded = []

    def api(endpoint, *, pages=False, destination=None):
        if destination is not None:
            artifact_id = int(endpoint.split("/")[-2])
            downloaded.append(artifact_id)
            destination.write_bytes(b"corrupt" if corrupt_download else blobs[artifact_id])
        elif endpoint.endswith("/attempts/2"):
            return {"id": 42, "run_attempt": 2, "run_started_at": "2026-10-07T00:00:00Z"}
        elif "jobs?" in endpoint:
            return [{"jobs": jobs}]
        else:
            return [{"artifacts": artifacts}]

    monkeypatch.setattr(trial, "gh", api)
    monkeypatch.setattr(trial, "identity", lambda root: context)
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: None)
    monkeypatch.setattr(gate, "load_bound_manifest", lambda binding: manifest)
    output = tmp_path / "collected"
    if corrupt_download:
        with pytest.raises(gate.GateBlock, match="download digest"):
            trial.aggregate(tmp_path, output)
    else:
        assert trial.aggregate(tmp_path, output) == 0
        result = json.loads((output / "trial-aggregate.json").read_bytes())
        assert result["verdict"] == "trial-complete" and result["authoritative"] is False
        assert downloaded == list(range(1, 9))
        assert all(r["queue_s"] == 60 and r["setup_s"] == 60 for r in result["parts"])


@pytest.mark.parametrize("mode,expected", [(None, ["source", "package", "wheel"]),
                                          ("source", ["source"]), ("wheel", ["package", "wheel"])])
def test_execution_partition_uses_existing_commands_and_full_mode_is_unchanged(
    tmp_path, monkeypatch, manifest, mode, expected,
):
    from types import SimpleNamespace

    root = tmp_path / "candidate"
    root.mkdir()
    template = _leg_artifact(manifest, "windows/3.12")
    binding = SimpleNamespace(clean=True, candidate_sha="1" * 40, candidate_tree="2" * 40,
                              manifest_git_blob="3" * 40, manifest_sha256=template["manifest"]["sha256"],
                              runner_git_blob="4" * 40)
    interpreter = gate.InterpreterInfo("3.12", Path(template["interpreters"][0]["path"]), "CPython", "3.12.0")
    checks = {c["id"]: c for c in template["checks"]}
    seen = []
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: binding)
    monkeypatch.setattr(gate, "load_bound_manifest", lambda b: manifest)
    monkeypatch.setattr(gate, "_platform_label", lambda: "windows")
    monkeypatch.setattr(gate, "_committed_version", lambda root: "0.78.1")
    monkeypatch.setattr(gate, "resolve_interpreters", lambda *a: [interpreter])
    monkeypatch.setattr(gate, "_binding_record", lambda name, *a, **kw: checks[name])
    monkeypatch.setattr(gate, "_same_binding", lambda *a: True)
    monkeypatch.setattr(gate, "_module_blob_sha256", lambda b: "5" * 64)
    monkeypatch.setattr(gate, "_export_phase", lambda b, run, phase: root)
    monkeypatch.setattr(gate, "write_run_evidence", lambda *a: "a" * 64)

    def pytest_mode(**kw):
        seen.append(kw["mode"])
        return checks[f"pytest-{kw['mode']}-py312"]

    def package(**kw):
        seen.append("package")
        return checks["package-build"], template["artifacts"], root / "package.whl"

    monkeypatch.setattr(gate, "_run_pytest_mode", pytest_mode)
    monkeypatch.setattr(gate, "run_package_build", package)
    monkeypatch.setattr(gate, "_create_isolated_venv", lambda **kw: (interpreter, {"prefix": str(root)}))
    monkeypatch.setattr(gate, "_install_wheel", lambda **kw: checks["wheel-install-py312"])
    monkeypatch.setattr(gate, "_wheel_dependency_check", lambda **kw: checks["wheel-dependency-check-py312"])
    monkeypatch.setattr(gate, "_wheel_contract", lambda **kw: checks["wheel-contract-py312"])
    monkeypatch.setattr(gate, "_runtime_dependency_snapshot", lambda **kw: root / "snapshot")
    monkeypatch.setattr(gate, "_prepare_wheel_test_environment", lambda **kw: (interpreter, {"prefix": str(root)}))
    result = gate.execute_gate(root=root, ci_leg="windows/3.12", trial_mode=mode,
                               temp_base=tmp_path / "external", evidence_path=tmp_path / "evidence/result.json")
    assert seen == expected
    assert result.artifact["verdict"] == ("pass" if mode is None else "block")


def test_installed_test_environment_records_the_actual_wheel_and_interpreter(tmp_path, monkeypatch, manifest):
    from types import SimpleNamespace

    creator = gate.InterpreterInfo("3.12", tmp_path / "python", "CPython", "3.12.0")
    wheel = tmp_path / "built.whl"
    wheel.write_bytes(b"exact built wheel")
    proof = {"prefix": str(tmp_path / "test")}
    monkeypatch.setattr(gate, "_create_isolated_venv", lambda **kw: (creator, proof))

    def command(**kw):
        log = tmp_path / (kw["check_id"] + ".log")
        log.write_text("successful install", encoding="utf-8")
        return SimpleNamespace(status="pass", argv=tuple(kw["argv"]), returncode=0, log_path=log)

    monkeypatch.setattr(gate, "run_command", command)
    collected = {}
    gate._prepare_wheel_test_environment(creator=creator, wheel=wheel, root=tmp_path / "test",
                                         source_root=tmp_path, env={}, manifest=manifest, logs_dir=tmp_path,
                                         install_evidence=collected)
    assert collected["3.12"]["argv"][-1] == str(wheel)
    assert collected["3.12"]["python_path"] == str(creator.path)
    assert collected["3.12"]["wheel_sha256"] == gate._sha256_file(wheel)
    assert collected["3.12"]["exit_code"] == 0
