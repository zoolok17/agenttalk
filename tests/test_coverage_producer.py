from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import assurance, gates
from agenttalk.coverage_parse import parse_coverage_percent
from agenttalk.store import Store


REVISION = "0123456789abcdef0123456789abcdef01234567"


def _plan(
    root: Path,
    script: str,
    *,
    profile: str = "release",
    revision: str = REVISION,
    provenance: dict | None = None,
) -> assurance.ScanPlan:
    return assurance.ScanPlan(
        root=root,
        profile=profile,
        manifest={"schema_version": 1},
        baseline={"schema_version": 1, "findings": []},
        detection={},
        provenance=provenance or {"git_sha": revision, "git_dirty": False},
        tools=[
            {
                "tool_id": "coverage",
                "dimension": "quality",
                "command": [sys.executable, "-c", script],
                "timeout_seconds": 10,
            }
        ],
        run_id="coverage-producer-test",
    )


def _set_github_ci(monkeypatch, revision: str) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", revision)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/agenttalk")


def _clear_github_ci(monkeypatch) -> None:
    for name in (
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(name, raising=False)


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    completed = subprocess.run(
        [git, *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _init_git_project(root: Path, files: dict[str, str] | None = None) -> str:
    root.mkdir(parents=True, exist_ok=True)
    contents = {
        ".gitignore": ".agenttalk/\n",
        "tracked.txt": "baseline\n",
    }
    contents.update(files or {})
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "coverage@example.invalid")
    _git(root, "config", "user.name", "Coverage Producer Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test fixture")
    return _git(root, "rev-parse", "HEAD")


def _ci_revision(
    root: Path,
    monkeypatch,
    files: dict[str, str] | None = None,
) -> str:
    revision = _init_git_project(root, files)
    _set_github_ci(monkeypatch, revision)
    return revision


def _coverage_gate(root: Path, scope: str = "release") -> dict:
    state = gates.load_gate_state(root)
    return state["gates"][f"coverage:{scope}"]


def _set_green_coverage_gate(
    root: Path,
    revision: str,
    *,
    scope: str = "release",
) -> None:
    with Store(root).config_lock():
        gates.set_gate(
            root,
            name=f"coverage:{scope}",
            status="green",
            severity="blocker",
            scope=scope,
            actor="assurance-ci",
            evidence_source="automation_ci",
            evidence=["https://github.com/example/agenttalk/actions/runs/1/attempts/1"],
            evidence_details={"coverage_percent": 99.0},
            revision=revision,
        )


def test_successful_ci_coverage_run_emits_revision_bound_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["severity"] == "blocker"
    assert gate["scope"] == "release"
    assert gate["evidence_source"] == "automation_ci"
    assert gate["revision"] == revision
    assert gate["evidence"][-1]["coverage_percent"] == 88.0
    assert isinstance(gate["evidence"][-1]["coverage_percent"], float)


def test_unparseable_ci_coverage_run_never_emits_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 1 99%')", revision=revision))
    assert _coverage_gate(tmp_path)["status"] == "green"
    assurance.run_plan(_plan(tmp_path, "print('coverage completed without a total')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] == "automation_ci"
    assert "coverage_percent" not in gate["evidence"][-1]


def test_non_ci_coverage_run_cannot_self_attest_green(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_github_ci(monkeypatch)

    assurance.run_plan(_plan(tmp_path, "print('Total coverage: 91.25%')"))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] != "automation_ci"
    assert gate["evidence"][-1]["coverage_percent"] == 91.25


def test_failed_coverage_command_stays_red_with_parseable_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 100 1 99%'); raise SystemExit(1)",
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence"][-1]["coverage_percent"] == 99.0


def test_coverage_spawn_error_emits_red_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    plan = _plan(tmp_path, "raise AssertionError('unused')", revision=revision)
    plan.tools[0]["command"] = [str(tmp_path / "missing-coverage-command.exe")]

    assurance.run_plan(plan)

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]


def test_coverage_runner_exception_downgrades_preexisting_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    _set_green_coverage_gate(tmp_path, revision)
    plan = _plan(tmp_path, "raise AssertionError('unused')", revision=revision)
    plan.tools[0]["command"] = [sys.executable, "-c", "\x00"]

    with pytest.raises(ValueError, match="null|embedded"):
        assurance.run_plan(plan)

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "runner failed" in gate["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "python -m coverage",
        [],
        ["   "],
        [sys.executable, "-c", "\n"],
        [sys.executable, "-c", "\x00"],
    ],
)
def test_manifest_rejects_non_executable_or_control_character_commands(
    tmp_path: Path,
    command,
) -> None:
    manifest_path = tmp_path / ".agenttalk" / "assurance.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "custom_commands": {"coverage": command},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(assurance.AssuranceUsageError, match="custom_commands.coverage"):
        assurance.load_manifest(tmp_path)


@pytest.mark.parametrize(
    "coverage_command",
    [
        None,
        [sys.executable, "-c", "\x00"],
    ],
    ids=["absent", "invalid"],
)
def test_cli_without_fresh_coverage_measurement_invalidates_existing_green_gate(
    tmp_path: Path,
    monkeypatch,
    coverage_command: list[str] | None,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {
            "pyproject.toml": "[project]\nname = 'coverage-producer-fixture'\nversion = '1.0'\n",
            "src/fixture/__init__.py": "VALUE = 1\n",
        },
    )
    _set_green_coverage_gate(tmp_path, revision, scope="change")
    manifest: dict = {"schema_version": 1}
    if coverage_command is not None:
        manifest["custom_commands"] = {"coverage": coverage_command}
    manifest_path = tmp_path / ".agenttalk" / "assurance.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rc = assurance.main(
        [
            "--root",
            str(tmp_path),
            "--profile",
            "change",
            "--out",
            str(tmp_path / ".agenttalk" / "assurance" / "runs"),
            "--json-only",
        ]
    )

    assert rc == 0
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "red"
    assert gate["reason"] == "no fresh coverage measurement this run"


@pytest.mark.parametrize(
    ("artifact_name", "payload", "expected"),
    [
        ("coverage.xml", '<coverage line-rate="0.8734"/>', 87.34),
        ("coverage.json", '{"totals": {"percent_covered": 92.5}}', 92.5),
    ],
)
def test_coverage_artifact_created_by_command_is_parsed(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
    payload: str,
    expected: float,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    script = f"from pathlib import Path; Path({artifact_name!r}).write_text({payload!r}, encoding='utf-8')"

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(expected)


def test_stale_coverage_artifact_cannot_override_current_stdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.xml": '<coverage line-rate="0.01"/>'},
    )

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == 88.0


def test_identical_artifact_replaced_by_command_counts_as_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = '<coverage line-rate="0.75"/>'
    revision = _ci_revision(tmp_path, monkeypatch, {"coverage.xml": payload})
    script = (
        "import os; from pathlib import Path; "
        f"Path('coverage.next').write_text({payload!r}, encoding='utf-8'); "
        "os.replace('coverage.next', 'coverage.xml')"
    )

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == 75.0


def test_ci_sha_mismatch_cannot_attest_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] == "local_command"


def test_tracked_file_dirtied_by_coverage_command_cannot_attest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    script = (
        "from pathlib import Path; "
        "Path('tracked.txt').write_text('changed\\n', encoding='utf-8'); "
        "print('TOTAL 10 0 99%')"
    )

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] == "local_command"


def test_git_status_error_is_unknown_and_cannot_attest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _init_git_project(tmp_path)
    _set_github_ci(monkeypatch, revision)
    bad_index = tmp_path / "bad-index"
    bad_index.mkdir()
    monkeypatch.setenv("GIT_INDEX_FILE", str(bad_index))
    manifest = {"schema_version": 1}
    baseline = {"schema_version": 1, "findings": []}

    provenance = assurance.collect_provenance(
        tmp_path,
        manifest,
        "release",
        baseline,
    )

    assert provenance["git_sha"] == revision
    assert provenance["git_dirty"] is None
    assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 10 0 99%')",
            revision=revision,
            # Exercise the independent post-command check even if a caller
            # supplies an incorrectly optimistic pre-command state.
            provenance={"git_sha": revision, "git_dirty": False},
        )
    )
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] == "local_command"


def test_touching_stale_coverage_xml_cannot_make_it_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.xml": '<coverage line-rate="0.99"/>'},
    )
    script = (
        "import os; from pathlib import Path; path = Path('coverage.xml'); path.exists() and os.utime(path, (1, 1))"
    )

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    assert (tmp_path / "coverage.xml").read_text(encoding="utf-8") == '<coverage line-rate="0.99"/>'
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]


def test_failed_report_restore_is_marked_and_recovered_by_next_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_report = '<coverage line-rate="0.25"/>'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.xml": original_report},
    )
    real_replace = assurance.os.replace

    def replace_with_restore_failure(src, dst):
        if (
            Path(src).name == "coverage.xml"
            and Path(dst) == tmp_path / "coverage.xml"
            and ".agenttalk" in Path(src).parts
        ):
            raise PermissionError("simulated restore failure")
        return real_replace(src, dst)

    monkeypatch.setattr(assurance.os, "replace", replace_with_restore_failure)

    result = assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage artifact cleanup failed" in gate["reason"]
    assert "recovery marker" in gate["reason"]
    assert any("coverage artifact cleanup failed" in error for error in result.runner_errors)
    markers = list((tmp_path / ".agenttalk" / "assurance" / "coverage-recovery").glob("*/transaction.json"))
    assert len(markers) == 1
    assert (markers[0].parent / "coverage.xml").read_text(encoding="utf-8") == original_report
    assert not (tmp_path / "coverage.xml").exists()

    monkeypatch.setattr(assurance.os, "replace", real_replace)
    assurance.run_plan(
        _plan(
            tmp_path,
            "print('coverage completed without a total')",
            revision=revision,
        )
    )

    assert (tmp_path / "coverage.xml").read_text(encoding="utf-8") == original_report
    assert not list((tmp_path / ".agenttalk" / "assurance" / "coverage-recovery").glob("*/transaction.json"))


def test_empty_transaction_left_after_marker_unlink_is_recovered_next_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_report = '<coverage line-rate="0.25"/>'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.xml": original_report},
    )
    real_rmdir = Path.rmdir

    def fail_empty_transaction_rmdir(path: Path) -> None:
        if (
            path.parent.name == "coverage-recovery"
            and path.name.startswith("transaction-")
            and not (path / "transaction.json").exists()
        ):
            raise PermissionError("simulated transaction rmdir failure")
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_empty_transaction_rmdir)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "recovery directory" in gate["reason"]
    recovery_root = tmp_path / ".agenttalk" / "assurance" / "coverage-recovery"
    transactions = [path for path in recovery_root.iterdir() if path.is_dir()]
    assert len(transactions) == 1
    assert not list(transactions[0].iterdir())

    monkeypatch.setattr(Path, "rmdir", real_rmdir)
    assurance.run_plan(
        _plan(
            tmp_path,
            "print('coverage completed without a total')",
            revision=revision,
        )
    )

    assert (tmp_path / "coverage.xml").read_text(encoding="utf-8") == original_report
    assert not list(recovery_root.iterdir())


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("TOTAL 10 0 1100%", None),
        ("TOTAL 10 0 999%", None),
        ("TOTAL 10 0 12x34%", None),
        ("TOTAL 10 0 96%", 96.0),
        ("Total coverage: 87.34%", 87.34),
    ],
)
def test_stdout_percent_parser_consumes_the_full_token(
    stdout: str,
    expected: float | None,
) -> None:
    actual = parse_coverage_percent(stdout)

    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected)


@pytest.mark.parametrize(
    "invalid_percent",
    ["99", True, None, 99, -1.0, 101.0],
)
def test_gate_write_rejects_invalid_coverage_percent(
    tmp_path: Path,
    invalid_percent,
) -> None:
    with Store(tmp_path).config_lock():
        with pytest.raises(ValueError, match="coverage_percent"):
            gates.set_gate(
                tmp_path,
                name="coverage:release",
                status="green",
                severity="blocker",
                scope="release",
                actor="assurance-ci",
                evidence_source="automation_ci",
                evidence=["https://github.com/example/agenttalk/actions/runs/1/attempts/1"],
                evidence_details={"coverage_percent": invalid_percent},
                revision=REVISION,
            )
    assert "coverage:release" not in gates.load_gate_state(tmp_path)["gates"]


@pytest.mark.parametrize(
    ("environment_name", "invalid_id"),
    [
        ("GITHUB_RUN_ID", "１２３"),
        ("GITHUB_RUN_ATTEMPT", "²"),
    ],
)
def test_non_ascii_github_run_ids_cannot_attest(
    tmp_path: Path,
    monkeypatch,
    environment_name: str,
    invalid_id: str,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    monkeypatch.setenv(environment_name, invalid_id)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 10 0 99%')", revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] == "local_command"
