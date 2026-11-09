"""Windows groups preserve every collected test in both package modes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import dev_gate as gate
from test_dev_gate import _binding, _gate_repo, _leg_artifact, _manifest


GROUP_A = [
    "tests/test_acceptance.py",
    "tests/test_lanes.py",
    "tests/test_supervisor.py",
    "tests/test_web.py",
    "tests/test_cli.py",
    "tests/test_owed_action_detection.py",
]


def test_378_group_definition_and_new_files_default_to_b():
    manifest = gate.validate_manifest(_manifest())
    spec = manifest["checks"]["pytest"]
    assert spec["windows_group_a"] == GROUP_A
    assert gate.pytest_group_selectors(spec, "A") == GROUP_A
    assert gate.pytest_group_selectors(spec, "B") == ["tests", *[f"--ignore={path}" for path in GROUP_A]]
    assert all(Path(path).is_file() for path in GROUP_A)


@pytest.mark.parametrize("mutation", ["missing", "changed", "duplicate"])
def test_378_manifest_refuses_group_definition_drift(mutation):
    manifest = _manifest()
    files = manifest["checks"]["pytest"]["windows_group_a"]
    if mutation == "missing":
        files.pop()
    elif mutation == "changed":
        files[0] = "tests/renamed.py"
    else:
        files.append(files[0])
    with pytest.raises(gate.GateBlock, match="windows_group_a"):
        gate.validate_manifest(manifest)


def test_378_windows_evidence_requires_group_runs():
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "windows/3.10")
    for check in artifact["checks"]:
        check.pop("group_runs", None)
        check.pop("selection", None)
    with pytest.raises(gate.GateBlock, match="group"):
        gate.validate_run_artifact(artifact, manifest)


@pytest.mark.parametrize(
    "damage",
    [
        "missing-group",
        "renamed-file",
        "overlap",
        "neither",
        "wrong-group",
        "duplicate-id",
        "wrong-command",
        "failed-run",
        "bad-time",
    ],
)
def test_378_aggregate_refuses_incomplete_or_changed_group_selection(damage, tmp_path):
    manifest = _manifest()
    artifacts = [_leg_artifact(manifest, leg) for leg in gate.expected_ci_legs(manifest)]
    windows = next(a for a in artifacts if a["ci_leg"] == "windows/3.10")
    check = next(c for c in windows["checks"] if c["id"].startswith("pytest-source-"))
    groups = check["group_runs"]
    if damage == "missing-group":
        groups.pop()
    elif damage == "renamed-file":
        old = groups[0]["selection"][0]
        renamed = old.replace("test_acceptance.py", "test_renamed.py")
        check["selection"][0] = renamed
        groups[0]["selection"].remove(old)
        groups[1]["selection"].append(renamed)
    elif damage == "overlap":
        groups[1]["selection"].append(groups[0]["selection"][0])
    elif damage == "neither":
        groups[1]["selection"].clear()
    elif damage == "wrong-group":
        groups[1]["selection"].append(groups[0]["selection"].pop())
    elif damage == "duplicate-id":
        groups[0]["selection"].append(groups[0]["selection"][0])
    elif damage == "wrong-command":
        groups[1]["argv"].pop()
    elif damage == "failed-run":
        groups[1]["status"] = "fail"
        groups[1]["exit_code"] = 1
    else:
        groups[1]["duration_ms"] = True
    with pytest.raises(gate.GateBlock) as failure:
        gate.aggregate_leg_artifacts(manifest, "release", artifacts, _binding(manifest, root=_gate_repo(tmp_path)))
    assert failure.value.code in {"pytest_selection_invalid", "manifest_schema_invalid", "evidence_command_mismatch"}


def test_378_aggregate_reports_each_group_time(tmp_path):
    manifest = _manifest()
    artifacts = [_leg_artifact(manifest, leg) for leg in gate.expected_ci_legs(manifest)]
    aggregate = gate.aggregate_leg_artifacts(
        manifest, "release", artifacts, _binding(manifest, root=_gate_repo(tmp_path))
    )
    leg = next(row for row in aggregate["legs"] if row["ci_leg"] == "windows/3.10")
    assert leg["pytest_group_times_ms"] == {
        "pytest-source-py310": {"A": 10, "B": 15},
        "pytest-wheel-py310": {"A": 10, "B": 15},
    }
    gate.validate_aggregate_artifact(aggregate, manifest)


@pytest.mark.parametrize("missing", [False, True], ids=["deleted", "renamed"])
def test_378_missing_or_renamed_group_a_file_blocks_before_execution(tmp_path, monkeypatch, missing):
    for path in GROUP_A[1:]:
        target = tmp_path / path
        target.parent.mkdir(exist_ok=True)
        target.write_text("def test_ok(): pass\n")
    if missing:
        (tmp_path / "tests/test_renamed.py").write_text("def test_ok(): pass\n")
    monkeypatch.setattr(gate, "_probe_python_module", lambda *a: "pytest")
    monkeypatch.setattr(gate, "import_probe", lambda **kw: {})
    monkeypatch.setattr(gate, "run_command", lambda **kw: pytest.fail("missing A file must block before pytest"))
    result = gate._run_pytest_mode(
        mode="source",
        interpreter=gate.InterpreterInfo("3.14", Path(sys.executable), "CPython", "3.14.0"),
        source_root=tmp_path,
        import_root=tmp_path / "src",
        env={},
        expected_version="1",
        manifest=_manifest(),
        basetemp=tmp_path / "pt",
        logs_dir=tmp_path / "logs",
        windows_groups=True,
    )
    assert result["status"] != "pass"
    assert "test_acceptance.py" in result["diagnostic"]


@pytest.mark.parametrize("fault", [None, "group-failure", "selection-gap"])
def test_378_real_group_runs_record_exact_collections_and_new_file(tmp_path, monkeypatch, fault):
    root = tmp_path / "candidate"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/pytest.py").write_text("raise RuntimeError('candidate pytest shadow ran')\n")
    for path in [*GROUP_A, "tests/test_new.py"]:
        (root / path).write_text("def test_one(): pass\n")
    if fault == "group-failure":
        (root / "tests/test_new.py").write_text("def test_one(): assert False\n")
    elif fault == "selection-gap":
        (root / "tests/test_extra.py").write_text("def test_one(): pass\n")
        (root / "conftest.py").write_text(
            "def pytest_collection_modifyitems(config, items):\n"
            "    if not config.option.collectonly:\n"
            "        items[:] = [i for i in items if 'test_new.py' not in i.nodeid]\n"
        )
    monkeypatch.setattr(gate, "_probe_python_module", lambda *a: "pytest")
    monkeypatch.setattr(gate, "import_probe", lambda **kw: {})
    monkeypatch.delenv("AGENTTALK_DEV_GATE_POSIX_PARALLEL", raising=False)
    result = gate._run_pytest_mode(
        mode="source",
        interpreter=gate.InterpreterInfo("3.14", Path(sys.executable), "CPython", "3.14.0"),
        source_root=root,
        import_root=root / "src",
        env=gate._base_env(tmp_path),
        expected_version="1",
        manifest=_manifest(),
        basetemp=tmp_path / "pt",
        logs_dir=tmp_path / "logs",
        windows_groups=True,
    )
    if fault:
        assert result["status"] != "pass"
        assert len(result["group_runs"]) == 2
        assert all(Path(group["log"]["path"]).is_file() for group in result["group_runs"])
        return
    assert result["status"] == "pass", result["diagnostic"]
    a, b = result["group_runs"]
    assert a["selection"] == sorted(f"{path}::test_one" for path in GROUP_A)
    assert b["selection"] == ["tests/test_new.py::test_one"]
    assert set(a["selection"]) | set(b["selection"]) == set(result["selection"])
    assert result["duration_ms"] >= sum(g["duration_ms"] for g in (a, b))
    assert all("--durations=0" in g["argv"] and g["duration_ms"] > 0 for g in (a, b))
    assert 0 <= result["collection_duration_ms"] <= result["duration_ms"]
    assert all(0 <= g["collection_duration_ms"] <= g["duration_ms"] for g in (a, b))


def test_378_group_logs_are_collected_and_verified(tmp_path):
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "windows/3.10")
    for check in artifact["checks"]:
        logs = [(check["id"], check["log"])]
        logs += [(f"{check['id']}-{group['group']}", group["log"]) for group in check.get("group_runs", [])]
        for name, log in logs:
            source = tmp_path / "raw" / f"{name}.log"
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(b"test log\n")
            log.update(path=str(source), sha256=gate.sha256_bytes(source.read_bytes()))
    output = tmp_path / "bundle/evidence.json"
    gate.write_run_evidence(output, artifact, manifest)
    loaded = json.loads(output.read_text())
    gate.validate_run_artifact(loaded, manifest, bundle_root=output.parent)
    group = next(c for c in loaded["checks"] if "group_runs" in c)["group_runs"][0]
    (output.parent / group["log"]["artifact_path"]).write_text("changed")
    with pytest.raises(gate.GateBlock, match="log"):
        gate.validate_run_artifact(loaded, manifest, bundle_root=output.parent)


@pytest.fixture
def two_tests_in_a_file():
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "windows/3.10")
    check = next(c for c in artifact["checks"] if c["id"].startswith("pytest-source-"))
    second = GROUP_A[0] + "::test_two"
    check["selection"].append(second)
    check["group_runs"][0]["selection"].append(second)
    gate.validate_run_artifact(artifact, manifest)
    return manifest, artifact, check, second


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("overlap", "overlap"),
        ("b-in-a", "B contains a test from an A file"),
        ("empty-full", "full pytest collection is empty"),
        ("short-mode", "mode time is shorter"),
        ("different-command", "differs from the committed selection"),
    ],
)
def test_681_each_group_guard_reports_its_own_violation(two_tests_in_a_file, fault, reason):
    manifest, artifact, check, second = two_tests_in_a_file
    a, b = check["group_runs"]
    if fault == "overlap":
        b["selection"].append(second)
    elif fault == "b-in-a":
        a["selection"].remove(second)
        b["selection"].append(second)
    elif fault == "empty-full":
        check["selection"].clear()
    elif fault == "short-mode":
        check["duration_ms"] = sum(group["duration_ms"] for group in (a, b)) - 1
    else:
        b["argv"][-1] = "--ignore=tests/renamed.py"
    with pytest.raises(gate.GateBlock, match=reason):
        gate.validate_run_artifact(artifact, manifest)


def test_681_aggregate_reports_full_and_group_collection_times(tmp_path):
    manifest = _manifest()
    artifacts = [_leg_artifact(manifest, leg) for leg in gate.expected_ci_legs(manifest)]
    aggregate = gate.aggregate_leg_artifacts(
        manifest, "release", artifacts, _binding(manifest, root=_gate_repo(tmp_path))
    )
    leg = next(row for row in aggregate["legs"] if row["ci_leg"] == "windows/3.10")
    assert leg["pytest_collection_times_ms"] == {
        "pytest-source-py310": {"full": 5, "A": 3, "B": 4},
        "pytest-wheel-py310": {"full": 5, "A": 3, "B": 4},
    }
    gate.validate_aggregate_artifact(aggregate, manifest)


@pytest.mark.parametrize("bad_time", [None, -1, True, "1", 100])
@pytest.mark.parametrize("phase", ["full", "A"])
def test_681_passing_modes_require_real_collection_times(two_tests_in_a_file, bad_time, phase):
    manifest, artifact, check, _ = two_tests_in_a_file
    target = check if phase == "full" else check["group_runs"][0]
    target["collection_duration_ms"] = bad_time
    with pytest.raises(gate.GateBlock, match="collection"):
        gate.validate_run_artifact(artifact, manifest)


def test_681_real_group_launcher_sets_the_program_name_and_preserves_arguments(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    observed = tmp_path / "observed.json"
    selection = tmp_path / "selection.json"
    test = candidate / "test_launcher.py"
    test.write_text(
        "import importlib.util, json, os, sys\n"
        "from pathlib import Path\n"
        "def test_program_name():\n"
        f"    Path({str(observed)!r}).write_text(json.dumps("
        "{'argv': sys.argv, 'sentinel': os.environ.get('LAUNCHER_SENTINEL')}))\n"
        "    assert sys.argv[0] != '-c'\n"
        "    assert sys.argv[0] == importlib.util.find_spec('pytest.__main__').origin\n"
    )
    arguments = ["-q", "-rs", "--durations=0", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "pt"), str(test)]
    env = gate._base_env(tmp_path)
    env["LAUNCHER_SENTINEL"] = "unchanged"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            gate._ISOLATED_PYTEST_GROUP_LAUNCHER,
            "pytest",
            str(candidate),
            str(selection),
            *arguments,
        ],
        cwd=candidate,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(observed.read_text())
    assert record["argv"][1:] == arguments and record["sentinel"] == "unchanged"
    assert "1 passed" in result.stdout
    report = json.loads(selection.read_text())
    assert report["ids"] == ["test_launcher.py::test_program_name"]
    assert isinstance(report["collection_duration_ms"], int)


def test_681_launcher_measures_only_the_collection_hook(tmp_path, monkeypatch):
    import importlib
    import runpy
    import sys
    import time
    from types import SimpleNamespace

    report = tmp_path / "selection.json"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(gate._ISOLATED_PYTEST_GROUP_LAUNCHER)

    def main(args, plugins):
        session = SimpleNamespace(items=[])
        clock[0] = 10.0  # Startup is outside the collection hook.
        collection = plugins[0].pytest_collection(session)
        next(collection)
        assert not report.exists()
        clock[0] = 10.125
        session.items = [SimpleNamespace(nodeid="tests/test_one.py::test_one")]
        with pytest.raises(StopIteration):
            next(collection)
        clock[0] = 20.0  # Running tests is outside collection too.
        return 0

    fake_pytest = SimpleNamespace(hookimpl=lambda **kwargs: lambda method: method, main=main)
    clock = [1.0]
    with monkeypatch.context() as patch:
        patch.setattr(importlib, "import_module", lambda name: fake_pytest)
        patch.setattr(sys, "argv", ["launcher", "pytest", "", str(report)])
        patch.setattr(time, "perf_counter", lambda: clock[0])
        with pytest.raises(SystemExit, match="0"):
            runpy.run_path(str(launcher))
    assert json.loads(report.read_text()) == {
        "ids": ["tests/test_one.py::test_one"],
        "collection_duration_ms": 125,
    }


@pytest.mark.parametrize(
    "damage",
    [
        "missing-mode",
        "extra-mode",
        "not-object",
        "missing-phase",
        "extra-phase",
        "negative",
        "boolean",
        "string",
        "null",
    ],
)
def test_681_aggregate_refuses_invalid_collection_times(tmp_path, damage):
    manifest = _manifest()
    artifacts = [_leg_artifact(manifest, leg) for leg in gate.expected_ci_legs(manifest)]
    aggregate = gate.aggregate_leg_artifacts(
        manifest, "release", artifacts, _binding(manifest, root=_gate_repo(tmp_path))
    )
    leg = next(row for row in aggregate["legs"] if row["ci_leg"] == "windows/3.10")
    times = leg["pytest_collection_times_ms"]
    if damage == "missing-mode":
        times.pop("pytest-source-py310")
    elif damage == "extra-mode":
        times["pytest-extra-py310"] = times["pytest-source-py310"].copy()
    elif damage == "not-object":
        times["pytest-source-py310"] = []
    elif damage == "missing-phase":
        times["pytest-source-py310"].pop("full")
    elif damage == "extra-phase":
        times["pytest-source-py310"]["extra"] = 0
    else:
        times["pytest-source-py310"]["A"] = {"negative": -1, "boolean": True, "string": "1", "null": None}[damage]
    with pytest.raises(gate.GateBlock, match="collection times"):
        gate.validate_aggregate_artifact(aggregate, manifest)


def test_681_execute_aggregate_keeps_collection_times_when_another_leg_is_missing(tmp_path, monkeypatch):
    manifest = _manifest()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    binding = _binding(manifest, root=candidate)
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: binding)
    monkeypatch.setattr(gate, "load_bound_manifest", lambda binding: manifest)
    monkeypatch.setattr(gate, "_committed_version", lambda root: "0.78.1")
    validate = gate.validate_run_artifact
    # Synthetic artifacts have no sibling log files; retain the real schema checks.
    monkeypatch.setattr(
        gate, "validate_run_artifact", lambda artifact, manifest, **kwargs: validate(artifact, manifest)
    )
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "leg.json").write_text(json.dumps(_leg_artifact(manifest, "windows/3.10")))
    result = gate.execute_aggregate(root=candidate, input_root=inputs, temp_base=tmp_path / "output")
    assert result.exit_code == 1
    assert result.artifact["verdict"] == "block"
    assert len(result.artifact["legs"]) == 1
    assert result.artifact["legs"][0]["pytest_collection_times_ms"] == {
        "pytest-source-py310": {"full": 5, "A": 3, "B": 4},
        "pytest-wheel-py310": {"full": 5, "A": 3, "B": 4},
    }
    assert json.loads(result.evidence_path.read_text())["legs"] == result.artifact["legs"]


@pytest.mark.parametrize("group_name", ["A", "B"])
@pytest.mark.parametrize("damage", ["intact", "missing", "corrupt"])
def test_681_collected_group_logs_require_bundle_reference(tmp_path, monkeypatch, group_name, damage):
    from test_dev_gate import _write_check_logs

    manifest = _manifest()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    binding = _binding(manifest, root=candidate)
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: binding)
    monkeypatch.setattr(gate, "load_bound_manifest", lambda binding: manifest)
    monkeypatch.setattr(gate, "_committed_version", lambda root: "0.78.1")
    inputs = tmp_path / "inputs"
    selected = None
    for leg in gate.expected_ci_legs(manifest):
        artifact = _leg_artifact(manifest, leg)
        directory = inputs / leg.replace("/", "-")
        directory.mkdir(parents=True)
        _write_check_logs(artifact, directory / "runner")
        evidence = directory / "evidence.json"
        gate.write_run_evidence(evidence, artifact, manifest)
        if leg == "windows/3.10":
            selected = artifact, evidence
    artifact, evidence = selected
    check = next(c for c in artifact["checks"] if c["id"].startswith("pytest-source-"))
    group = next(g for g in check["group_runs"] if g["group"] == group_name)
    log_path = evidence.parent / group["log"]["artifact_path"]
    if damage != "intact":
        group["log"].pop("artifact_path")
        if damage == "missing":
            log_path.unlink()
        else:
            log_path.write_bytes(b"corrupt group output")
        evidence.write_text(json.dumps(artifact), encoding="utf-8")

    result = gate.execute_aggregate(root=candidate, input_root=inputs, temp_base=tmp_path / "output")

    if damage == "intact":
        assert result.exit_code == 0 and result.artifact["verdict"] == "pass"
    else:
        assert result.exit_code == 1 and result.artifact["verdict"] == "block"
        assert result.artifact["blockers"][0]["code"] == "aggregate_input_invalid"
        assert "pytest group log: missing artifact_path" in result.artifact["blockers"][0]["detail"]
