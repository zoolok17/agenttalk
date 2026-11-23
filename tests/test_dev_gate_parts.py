"""#378: each Windows CI leg runs as four parts, and the aggregate accepts only every part of every Windows leg."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import dev_gate as gate
from agenttalk.cli import build_parser, cmd_dev_gate
from test_dev_gate import _binding, _ci_inputs, _gate_repo, _leg_artifact, _manifest

PARTS = ["source-a", "source-b", "wheel-a", "wheel-b"]
GROUP_A = gate.WINDOWS_GROUP_A


def _part(artifacts, leg, part):
    return next(a for a in artifacts if (a["ci_leg"], a.get("part")) == (leg, part))


def _pytest_check(artifact):
    return next(c for c in artifact["checks"] if c["id"].startswith("pytest-"))


def _aggregate(manifest, artifacts, tmp_path):
    return gate.aggregate_leg_artifacts(manifest, "release", artifacts, _binding(manifest, root=_gate_repo(tmp_path)))


# --- the manifest declares the parts, and the code pins them


def test_manifest_declares_the_four_windows_parts():
    spec = gate.validate_manifest(_manifest())["checks"]["pytest"]
    assert spec["windows_parts"] == gate.WINDOWS_PARTS == PARTS


@pytest.mark.parametrize("change", ["missing-field", "dropped", "reordered", "extra", "renamed", "two-parts"])
def test_manifest_refuses_any_other_part_list(change):
    manifest = _manifest()
    spec = manifest["checks"]["pytest"]
    if change == "missing-field":
        spec.pop("windows_parts")
    elif change == "dropped":
        spec["windows_parts"].pop()
    elif change == "reordered":
        spec["windows_parts"].reverse()
    elif change == "extra":
        spec["windows_parts"].append("wheel-c")
    elif change == "renamed":
        spec["windows_parts"][0] = "source-x"
    else:
        spec["windows_parts"] = ["a", "b"]
    with pytest.raises(gate.GateBlock, match="windows_parts"):
        gate.validate_manifest(manifest)


# --- what each part runs


def test_parts_split_one_windows_leg_without_losing_or_adding_a_check():
    manifest = _manifest()
    whole = gate.required_check_ids(manifest, execution_scope="ci-leg", ci_leg="windows/3.12")
    by_part = {
        part: gate.required_check_ids(manifest, execution_scope="ci-leg", ci_leg="windows/3.12", part=part)
        for part in PARTS
    }
    assert by_part["source-a"] == by_part["source-b"] == ["git-binding", "pytest-source-py312", "final-binding"]
    assert by_part["wheel-a"] == by_part["wheel-b"] == [check for check in whole if check != "pytest-source-py312"]
    assert set().union(*by_part.values()) == set(whole)
    assert [gate.parse_ci_part(part, manifest, "windows/3.12") for part in PARTS] == [
        ("source", "A"), ("source", "B"), ("wheel", "A"), ("wheel", "B"),
    ]


@pytest.mark.parametrize(
    "leg,part,scope",
    [("linux/3.12", "source-a", "ci-leg"), ("macos/3.10", "wheel-b", "ci-leg"),
     ("windows/3.12", "wheel-c", "ci-leg"), ("windows/3.12", "", "ci-leg"), (None, "source-a", "local")],
)
def test_a_part_exists_only_for_a_declared_windows_leg(leg, part, scope):
    with pytest.raises(gate.GateBlock) as refused:
        gate.required_check_ids(_manifest(), execution_scope=scope, ci_leg=leg, part=part)
    assert refused.value.code == "ci_part_invalid"


def test_expected_inputs_are_every_leg_and_every_windows_part_in_order():
    inputs = gate.expected_ci_inputs(_manifest())
    assert len(inputs) == 8 + 4 * 4
    assert inputs[:6] == [
        ("linux/3.10", None), ("linux/3.11", None), ("linux/3.12", None), ("linux/3.13", None),
        ("windows/3.10", "source-a"), ("windows/3.10", "source-b"),
    ]
    assert [gate.ci_input_label(leg, part) for leg, part in inputs][4:8] == [
        "windows/3.10/source-a", "windows/3.10/source-b", "windows/3.10/wheel-a", "windows/3.10/wheel-b",
    ]


# --- one part's evidence


@pytest.mark.parametrize("part", PARTS)
def test_part_evidence_is_valid_with_only_its_own_checks_and_group(part):
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "windows/3.11", part=part)
    record = gate.validate_run_artifact(artifact, manifest)
    assert record["part"] == part
    assert [run["group"] for run in _pytest_check(record)["group_runs"]] == [part[-1].upper()]


@pytest.mark.parametrize(
    "damage,code",
    [("linux-leg", "ci_part_invalid"), ("undeclared", "ci_part_invalid"), ("not-text", "ci_part_invalid"),
     ("local-scope", "evidence_schema_invalid"), ("both-groups", "pytest_selection_invalid"),
     ("wrong-group", "pytest_selection_invalid"), ("source-with-wheel", "evidence_schema_invalid"),
     ("not-its-share", "pytest_selection_invalid"), ("whole-leg-checks", "evidence_cardinality_invalid")],
)
def test_part_evidence_refuses_a_part_that_is_not_exactly_one_declared_part(damage, code):
    manifest = _manifest()
    part = "wheel-a" if damage == "wrong-group" else "source-b"
    artifact = _leg_artifact(manifest, "windows/3.11", part=part)
    whole = _leg_artifact(manifest, "windows/3.11")
    check = _pytest_check(artifact)
    if damage == "linux-leg":
        artifact = _leg_artifact(manifest, "linux/3.11")
        artifact["part"] = "source-a"
    elif damage == "undeclared":
        artifact["part"] = "source-c"
    elif damage == "not-text":
        artifact["part"] = 2
    elif damage == "local-scope":
        artifact["execution_scope"] = "local"
    elif damage == "both-groups":
        check["group_runs"] = copy.deepcopy(next(c for c in whole["checks"] if c["id"] == check["id"])["group_runs"])
    elif damage == "wrong-group":
        check["group_runs"][0]["group"] = "B"
    elif damage == "source-with-wheel":
        artifact["artifacts"] = copy.deepcopy(whole["artifacts"])
    elif damage == "not-its-share":
        check["group_runs"][0]["selection"].append(f"{GROUP_A[0]}::test_one")
    else:
        artifact["required_check_ids"] = whole["required_check_ids"]
    with pytest.raises(gate.GateBlock) as refused:
        gate.validate_run_artifact(artifact, manifest)
    assert refused.value.code == code


# --- the aggregate


def test_aggregate_passes_with_every_part_of_every_windows_leg(tmp_path):
    manifest = gate.validate_manifest(_manifest())
    aggregate = _aggregate(manifest, _ci_inputs(manifest), tmp_path)
    assert aggregate["complete"] is True and aggregate["verdict"] == "pass" and aggregate["blockers"] == []
    labels = [gate.ci_input_label(row["ci_leg"], row["part"]) for row in aggregate["legs"]]
    assert labels == [gate.ci_input_label(leg, part) for leg, part in gate.expected_ci_inputs(manifest)]
    assert {row["part"] for row in aggregate["legs"] if row["ci_leg"].startswith("windows/")} == set(PARTS)
    assert {row["part"] for row in aggregate["legs"] if not row["ci_leg"].startswith("windows/")} == {None}
    gate.validate_aggregate_artifact(aggregate, manifest)


@pytest.mark.parametrize("missing", PARTS)
def test_aggregate_fails_when_any_one_part_is_missing(tmp_path, missing):
    manifest = gate.validate_manifest(_manifest())
    artifacts = [a for a in _ci_inputs(manifest) if (a["ci_leg"], a.get("part")) != ("windows/3.12", missing)]
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code == "aggregate_missing_leg"
    assert f"windows/3.12/{missing}" in refused.value.detail


def test_aggregate_fails_on_a_duplicate_part(tmp_path):
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    duplicate = copy.deepcopy(_part(artifacts, "windows/3.13", "wheel-b"))
    duplicate["run_id"] = "a second run of the same part"
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, [*artifacts, duplicate], tmp_path)
    assert refused.value.code == "aggregate_duplicate_leg"
    assert "windows/3.13/wheel-b" in refused.value.detail


@pytest.mark.parametrize("extra", ["whole-windows-leg", "whole-leg-in-place-of-parts"])
def test_aggregate_fails_on_an_extra_or_unsplit_windows_leg(tmp_path, extra):
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    whole = _leg_artifact(manifest, "windows/3.10")
    if extra == "whole-windows-leg":
        artifacts.append(whole)
    else:
        artifacts = [a for a in artifacts if a["ci_leg"] != "windows/3.10"] + [whole]
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code in {"aggregate_unexpected_leg", "aggregate_missing_leg"}
    assert "windows/3.10" in refused.value.detail


def test_aggregate_fails_on_an_undeclared_part(tmp_path):
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    _part(artifacts, "windows/3.10", "wheel-b")["part"] = "wheel-c"
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code == "ci_part_invalid"


@pytest.mark.parametrize("field", ["candidate_sha", "candidate_tree"])
def test_aggregate_fails_when_one_part_ran_on_another_commit(tmp_path, field):
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    _part(artifacts, "windows/3.11", "source-b")["subject"][field] = "9" * 40
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code == "aggregate_binding_mismatch"


@pytest.mark.parametrize("mode", ["source", "wheel"])
def test_aggregate_fails_when_the_two_halves_do_not_add_up_to_one_collection(tmp_path, mode):
    """Each part is valid alone (its group is its share of its own full collection), but the B part collected a
    test the A part never saw, so the two halves are not one suite."""
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    check = _pytest_check(_part(artifacts, "windows/3.12", f"{mode}-b"))
    check["selection"].append("tests/test_only_here.py::test_one")
    check["group_runs"][0]["selection"].append("tests/test_only_here.py::test_one")
    gate.validate_run_artifact(_part(artifacts, "windows/3.12", f"{mode}-b"), manifest)
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code == "aggregate_part_mismatch"
    assert f"windows/3.12 {mode}" in refused.value.detail


@pytest.mark.parametrize("damage", ["overlap", "incomplete"])
def test_aggregate_fails_on_overlapping_or_incomplete_groups(tmp_path, damage):
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    a_run = _pytest_check(_part(artifacts, "windows/3.10", "source-a"))["group_runs"][0]
    b_run = _pytest_check(_part(artifacts, "windows/3.10", "source-b"))["group_runs"][0]
    if damage == "overlap":
        b_run["selection"].append(a_run["selection"][0])
    else:
        a_run["selection"].pop()
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code == "pytest_selection_invalid"


def test_aggregate_fails_when_the_wheel_parts_tested_different_wheels(tmp_path):
    manifest = gate.validate_manifest(_manifest())
    artifacts = _ci_inputs(manifest)
    _part(artifacts, "windows/3.13", "wheel-b")["artifacts"]["wheel"]["sha256"] = "e" * 64
    with pytest.raises(gate.GateBlock) as refused:
        _aggregate(manifest, artifacts, tmp_path)
    assert refused.value.code == "aggregate_part_mismatch"
    assert "windows/3.13 wheel parts tested different wheels" in refused.value.detail


def test_aggregate_is_complete_but_blocked_when_one_part_failed(tmp_path):
    manifest = gate.validate_manifest(_manifest())
    artifacts = [
        _leg_artifact(manifest, leg, part=part, status="fail" if (leg, part) == ("windows/3.10", "wheel-a") else "pass")
        for leg, part in gate.expected_ci_inputs(manifest)
    ]
    aggregate = _aggregate(manifest, artifacts, tmp_path)
    assert aggregate["complete"] is True and aggregate["verdict"] == "block"
    assert [b["check_id"] for b in aggregate["blockers"]] == ["windows/3.10/wheel-a"]
    gate.validate_aggregate_artifact(aggregate, manifest)


@pytest.mark.parametrize(
    "damage,reason",
    [("row-missing", "exact ordered CI matrix"), ("row-without-part", "group times are incomplete"),
     ("part-on-linux", "only a declared Windows CI leg runs in parts"), ("both-groups", "group times are malformed"),
     ("other-mode", "group times are incomplete")],
)
def test_aggregate_record_must_hold_every_part_row_with_only_its_own_times(tmp_path, damage, reason):
    manifest = gate.validate_manifest(_manifest())
    aggregate = _aggregate(manifest, _ci_inputs(manifest), tmp_path)
    rows = aggregate["legs"]
    row = next(r for r in rows if (r["ci_leg"], r["part"]) == ("windows/3.10", "source-a"))
    if damage == "row-missing":
        rows.remove(row)
    elif damage == "row-without-part":
        row["part"] = None
    elif damage == "part-on-linux":
        rows[0]["part"] = "source-a"
    elif damage == "both-groups":
        row["pytest_group_times_ms"]["pytest-source-py310"]["B"] = 1
    else:
        row["pytest_group_times_ms"] = {"pytest-wheel-py310": {"A": 1}}
    with pytest.raises(gate.GateBlock, match=reason):
        gate.validate_aggregate_artifact(aggregate, manifest)


def _write_inputs(tmp_path, manifest, artifacts):
    inputs = tmp_path / "inputs"
    for index, artifact in enumerate(artifacts):
        directory = inputs / f"input-{index}"
        directory.mkdir(parents=True)
        (directory / "dev-gate-evidence.json").write_text(json.dumps(artifact), encoding="utf-8")
    return inputs


@pytest.mark.parametrize("case", ["every-part", "duplicate-part", "missing-part"])
def test_execute_aggregate_reads_one_file_per_part(tmp_path, monkeypatch, case):
    manifest = gate.validate_manifest(_manifest())
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    binding = _binding(manifest, root=candidate)
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: binding)
    monkeypatch.setattr(gate, "load_bound_manifest", lambda binding: manifest)
    monkeypatch.setattr(gate, "_committed_version", lambda root: "0.78.1")
    validate = gate.validate_run_artifact
    # Synthetic artifacts have no sibling log files; keep the real schema checks.
    monkeypatch.setattr(gate, "validate_run_artifact", lambda artifact, manifest, **_: validate(artifact, manifest))
    artifacts = _ci_inputs(manifest)
    if case == "duplicate-part":
        artifacts.append(copy.deepcopy(_part(artifacts, "windows/3.11", "source-a")))
    elif case == "missing-part":
        artifacts.remove(_part(artifacts, "windows/3.11", "source-a"))
    result = gate.execute_aggregate(
        root=candidate, input_root=_write_inputs(tmp_path, manifest, artifacts), temp_base=tmp_path / "out"
    )
    if case == "every-part":
        assert result.exit_code == 0 and result.artifact["verdict"] == "pass"
        assert len(result.artifact["legs"]) == 24
        return
    assert result.exit_code == 1 and result.artifact["complete"] is False
    blocker = result.artifact["blockers"][0]
    codes = {"duplicate-part": "aggregate_duplicate_leg", "missing-part": "aggregate_missing_leg"}
    assert blocker["code"] == codes[case]
    assert "windows/3.11/source-a" in blocker["detail"]


# --- running a part


@pytest.mark.parametrize(
    "leg,part,trial_mode",
    [("windows/3.12", "source-a", "source"), ("linux/3.12", "source-a", None), (None, "wheel-a", None),
     ("windows/3.12", "wheel-c", None)],
)
def test_execute_gate_runs_a_part_only_for_a_declared_windows_leg(tmp_path, monkeypatch, leg, part, trial_mode):
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: SimpleNamespace())
    monkeypatch.setattr(gate, "load_bound_manifest", lambda binding: gate.validate_manifest(_manifest()))
    with pytest.raises(gate.GateBlock) as refused:
        gate.execute_gate(root=tmp_path, ci_leg=leg, part=part, trial_mode=trial_mode)
    assert refused.value.code == "ci_part_invalid"


@pytest.mark.parametrize("group", ["A", "B"])
def test_a_real_part_runs_only_its_group_and_proves_its_share(tmp_path, monkeypatch, group):
    root = tmp_path / "candidate"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    for path in [*GROUP_A, "tests/test_new.py"]:
        (root / path).write_text("def test_one(): pass\n")
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
        only_group=group,
    )
    assert result["status"] == "pass", result["diagnostic"]
    [run] = result["group_runs"]
    assert run["group"] == group
    expected = sorted(f"{path}::test_one" for path in GROUP_A) if group == "A" else ["tests/test_new.py::test_one"]
    assert run["selection"] == expected
    assert len(result["selection"]) == len(GROUP_A) + 1
    assert not (tmp_path / "logs" / f"{result['id']}-{'B' if group == 'A' else 'A'}.log").exists()


def test_cli_refuses_a_part_with_the_aggregate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "discover_repo_root", lambda cwd=None: tmp_path)
    monkeypatch.setattr(gate, "reenter_candidate_source", lambda root, argv: None)
    monkeypatch.setattr(gate, "write_preflight_block_evidence", lambda **kw: (_ for _ in ()).throw(OSError("none")))
    args = build_parser().parse_args(["dev-gate", "--aggregate", str(tmp_path), "--part", "source-a"])
    assert cmd_dev_gate(args) == 2
    assert "[aggregate_argument_invalid] --part is not valid with --aggregate" in capsys.readouterr().err
