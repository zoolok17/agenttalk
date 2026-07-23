from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agenttalk import assurance, gates


REVISION = "0123456789abcdef0123456789abcdef01234567"


def _plan(root: Path, script: str, *, profile: str = "release") -> assurance.ScanPlan:
    return assurance.ScanPlan(
        root=root,
        profile=profile,
        manifest={"schema_version": 1},
        baseline={"schema_version": 1, "findings": []},
        detection={},
        provenance={"git_sha": REVISION, "git_dirty": False},
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


def _set_github_ci(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", REVISION)
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


def _coverage_gate(root: Path, scope: str = "release") -> dict:
    state = gates.load_gate_state(root)
    return state["gates"][f"coverage:{scope}"]


def test_successful_ci_coverage_run_emits_revision_bound_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_github_ci(monkeypatch)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')"))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["severity"] == "blocker"
    assert gate["scope"] == "release"
    assert gate["evidence_source"] == "automation_ci"
    assert gate["revision"] == REVISION
    assert gate["evidence"][-1]["coverage_percent"] == 88.0
    assert isinstance(gate["evidence"][-1]["coverage_percent"], float)


def test_unparseable_ci_coverage_run_never_emits_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_github_ci(monkeypatch)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 1 99%')"))
    assert _coverage_gate(tmp_path)["status"] == "green"
    assurance.run_plan(_plan(tmp_path, "print('coverage completed without a total')"))

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
    _set_github_ci(monkeypatch)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 1 99%'); raise SystemExit(1)"))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence"][-1]["coverage_percent"] == 99.0


def test_coverage_spawn_error_emits_red_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_github_ci(monkeypatch)
    plan = _plan(tmp_path, "raise AssertionError('unused')")
    plan.tools[0]["command"] = [str(tmp_path / "missing-coverage-command.exe")]

    assurance.run_plan(plan)

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]


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
    _set_github_ci(monkeypatch)
    script = (
        "from pathlib import Path; "
        f"Path({artifact_name!r}).write_text({payload!r}, encoding='utf-8')"
    )

    assurance.run_plan(_plan(tmp_path, script))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(expected)


def test_stale_coverage_artifact_cannot_override_current_stdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_github_ci(monkeypatch)
    (tmp_path / "coverage.xml").write_text(
        '<coverage line-rate="0.01"/>',
        encoding="utf-8",
    )

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')"))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == 88.0


def test_identical_artifact_replaced_by_command_counts_as_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_github_ci(monkeypatch)
    payload = '<coverage line-rate="0.75"/>'
    (tmp_path / "coverage.xml").write_text(payload, encoding="utf-8")
    script = (
        "import os; from pathlib import Path; "
        f"Path('coverage.next').write_text({payload!r}, encoding='utf-8'); "
        "os.replace('coverage.next', 'coverage.xml')"
    )

    assurance.run_plan(_plan(tmp_path, script))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == 75.0


def test_ci_sha_mismatch_cannot_attest_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_github_ci(monkeypatch)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)

    assurance.run_plan(_plan(tmp_path, "print('TOTAL 100 12 88%')"))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence_source"] == "local_command"
