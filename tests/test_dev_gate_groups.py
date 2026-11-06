"""Windows groups preserve every collected test in both package modes."""

import json
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
