from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agenttalk import assurance, cli, close, coverage_parse, gates
from agenttalk.coverage_parse import parse_coverage_percent
from agenttalk.store import Store


REVISION = "0123456789abcdef0123456789abcdef01234567"


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _run_assurance_cli(root: Path, *, profile: str = "change") -> int:
    return assurance.main(
        [
            "--root",
            str(root),
            "--profile",
            profile,
            "--out",
            str(root / ".agenttalk" / "assurance" / "runs"),
            "--json-only",
        ]
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


@pytest.mark.parametrize(
    "close_scope",
    [*sorted(close.RELEASE_CLASS_SCOPES), "custom"],
)
@pytest.mark.parametrize("profile", assurance.PROFILES)
def test_each_supported_close_scope_can_select_each_producible_coverage_gate(
    tmp_path: Path,
    monkeypatch,
    close_scope: str,
    profile: str,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 100 12 88%')",
            profile=profile,
            revision=revision,
        )
    )

    state = gates.load_gate_state(tmp_path)
    gate_name = f"coverage:{profile}"
    assert set(state["gates"]) == {gate_name}

    policy = close.validate_dod_policy(
        {
            "schema_version": 1,
            "scopes": {
                close_scope: {
                    "coverage": {
                        "gate": gate_name,
                        "min_percent": 80.0,
                        "max_age_days": 14,
                    }
                }
            },
        }
    )
    required = close.derive_required_dod(policy, close_scope)["dimensions"]
    record = {
        "scope": close_scope,
        "gate_scope": close_scope,
        "revision": revision,
    }
    resolved = cli._resolve_dod_coverage_gate(
        Store(tmp_path),
        required["coverage"],
        record,
    )

    assert resolved["gate"] == gate_name
    assert resolved["gate_scope"] == profile
    assert close.evaluate_dod(
        record,
        {
            "policy_present": True,
            "policy_error": None,
            "required_dimensions": required,
            "coverage": resolved,
        },
    ) == []


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


def test_stderr_only_coverage_summary_stays_red(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    assurance.run_plan(
        _plan(
            tmp_path,
            "import sys; print('TOTAL 100 1 99%', file=sys.stderr)",
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]


def test_bare_carriage_return_rewrite_stays_red_through_subprocess_capture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    assurance.run_plan(
        _plan(
            tmp_path,
            (
                "import sys; "
                "sys.stdout.write('Total coverage: 99%\\r"
                "progress output replaced the summary')"
            ),
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]


def test_non_utf8_coverage_stdout_stays_red_without_aborting_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "import sys; sys.stdout.buffer.write(b'TOTAL 100 1 99%\\xff')",
            revision=revision,
        )
    )

    assert result.tools_run[0]["status"] == "error-optional-tool"
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]


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


def test_coverage_lock_acquisition_failure_aborts_before_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = '{"totals": {"percent_covered": 17.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.json": original},
    )

    class DeniedLock:
        def __enter__(self):
            raise TimeoutError("simulated coverage lock contention")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        Store,
        "coverage_transaction_lock",
        lambda self, **_kwargs: DeniedLock(),
    )

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('coverage-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage-command-ran").exists()
    assert (tmp_path / "coverage.json").read_text(encoding="utf-8") == original
    assert result.tools_run[0]["status"] == "error-optional-tool"
    assert _coverage_gate(tmp_path)["status"] == "red"
    assert "coverage command was not run" in _coverage_gate(tmp_path)["reason"]


def test_coverage_lock_release_failure_discards_provisional_green(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {".gitignore": ".agenttalk/\ncoverage.xml\ncoverage.json\n"},
    )

    class FailingReleaseLock:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            raise OSError("simulated coverage lock release failure")

    monkeypatch.setattr(
        Store,
        "coverage_transaction_lock",
        lambda self, **_kwargs: FailingReleaseLock(),
    )

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 100 1 99%')",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage.json").exists()
    assert not (tmp_path / "coverage.xml").exists()
    assert result.tools_run[0]["status"] == "error-optional-tool"
    assert any("release failure" in error for error in result.runner_errors)
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage result was discarded" in gate["reason"]


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

    rc = _run_assurance_cli(tmp_path)

    assert rc == 0
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "red"
    assert gate["reason"] == "no fresh coverage measurement this run"


def test_cli_pre_plan_manifest_path_failure_invalidates_existing_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    _set_green_coverage_gate(tmp_path, revision, scope="change")
    manifest_path = tmp_path / ".agenttalk" / "assurance.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "monorepo": {
                    "packages": [
                        {
                            "name": "broken",
                            "path": "\x00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run_assurance_cli(tmp_path)

    assert rc == 1
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "red"
    assert gate["reason"] == "no fresh coverage measurement this run"


def test_cli_pre_plan_detection_error_invalidates_existing_green_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    _set_green_coverage_gate(tmp_path, revision, scope="change")

    def fail_detection(*_args, **_kwargs):
        raise PermissionError("simulated detection failure")

    monkeypatch.setattr(assurance, "detect_project", fail_detection)

    rc = _run_assurance_cli(tmp_path)

    assert rc == 1
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "red"
    assert gate["reason"] == "no fresh coverage measurement this run"


def test_cli_pre_plan_failure_does_not_fabricate_coverage_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _ci_revision(tmp_path, monkeypatch)

    def fail_detection(*_args, **_kwargs):
        raise PermissionError("simulated detection failure")

    monkeypatch.setattr(assurance, "detect_project", fail_detection)

    rc = _run_assurance_cli(tmp_path)

    assert rc == 1
    assert "coverage:change" not in gates.load_gate_state(tmp_path)["gates"]


def test_cli_pre_plan_failure_preserves_coverage_waiver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _ci_revision(tmp_path, monkeypatch)
    with Store(tmp_path).config_lock():
        gates.waive_gate(
            tmp_path,
            name="coverage:change",
            operator="test-operator",
            reason="accepted fixture waiver",
            scope="change",
            expires="2099-01-01T00:00:00Z",
        )

    def fail_detection(*_args, **_kwargs):
        raise PermissionError("simulated detection failure")

    monkeypatch.setattr(assurance, "detect_project", fail_detection)

    rc = _run_assurance_cli(tmp_path)

    assert rc == 1
    assert _coverage_gate(tmp_path, "change")["status"] == "waived"


def test_cli_pre_plan_failure_replaces_expired_coverage_waiver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _ci_revision(tmp_path, monkeypatch)
    with Store(tmp_path).config_lock():
        gates.waive_gate(
            tmp_path,
            name="coverage:change",
            operator="test-operator",
            reason="expired fixture waiver",
            scope="change",
            expires="2000-01-01T00:00:00Z",
        )

    def fail_detection(*_args, **_kwargs):
        raise PermissionError("simulated detection failure")

    monkeypatch.setattr(assurance, "detect_project", fail_detection)

    rc = _run_assurance_cli(tmp_path)

    assert rc == 1
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "red"
    assert gate["reason"] == "no fresh coverage measurement this run"


@pytest.mark.parametrize(
    ("script", "expected_status"),
    [
        (
            (
                "from pathlib import Path; "
                "Path('.agenttalk/coverage-command-ran').write_text('ran', encoding='utf-8'); "
                "print('TOTAL 100 0 100%')"
            ),
            "pass",
        ),
        (
            (
                "from pathlib import Path; "
                "Path('.agenttalk/coverage-command-ran').write_text('ran', encoding='utf-8'); "
                "print('TOTAL 100 0 100%'); "
                "raise SystemExit(1)"
            ),
            "error-optional-tool",
        ),
    ],
    ids=["green-measurement", "red-measurement"],
)
def test_completed_coverage_scan_preserves_existing_waiver(
    tmp_path: Path,
    monkeypatch,
    script: str,
    expected_status: str,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    with Store(tmp_path).config_lock():
        gates.waive_gate(
            tmp_path,
            name="coverage:change",
            operator="test-operator",
            reason="accepted fixture waiver",
            scope="change",
            expires="2099-01-01T00:00:00Z",
        )
    waiver = json.loads(json.dumps(_coverage_gate(tmp_path, "change")))

    result = assurance.run_plan(
        _plan(
            tmp_path,
            script,
            profile="change",
            revision=revision,
        )
    )

    assert result.tools_run[0]["status"] == expected_status
    assert (tmp_path / ".agenttalk" / "coverage-command-ran").read_text(encoding="utf-8") == "ran"
    assert _coverage_gate(tmp_path, "change") == waiver


def test_completed_coverage_scan_updates_existing_nonwaived_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    with Store(tmp_path).config_lock():
        gates.set_gate(
            tmp_path,
            name="coverage:change",
            status="red",
            severity="blocker",
            scope="change",
            actor="previous-assurance-run",
            evidence_source="local_command",
            evidence=["assurance-run:previous"],
            reason="previous measurement was red",
            revision=revision,
        )

    assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 100 12 88%')",
            profile="change",
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "green"
    assert gate["updated_by"] == "assurance-ci"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(88.0)


def test_completed_coverage_scan_replaces_expired_waiver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    with Store(tmp_path).config_lock():
        gates.waive_gate(
            tmp_path,
            name="coverage:change",
            operator="test-operator",
            reason="expired fixture waiver",
            scope="change",
            expires="2000-01-01T00:00:00Z",
        )

    assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 100 12 88%')",
            profile="change",
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "green"
    assert gate["updated_by"] == "assurance-ci"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(88.0)


def test_completed_coverage_scan_replaces_invalid_waiver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    with Store(tmp_path).config_lock():
        gates.waive_gate(
            tmp_path,
            name="coverage:change",
            operator="test-operator",
            reason="fixture waiver that will be invalidated",
            scope="change",
            expires="2099-01-01T00:00:00Z",
        )
        state = gates.load_gate_state(tmp_path)
        state["gates"]["coverage:change"]["waiver"]["operator"] = ""
        gates.write_gate_state(tmp_path, state)

    assurance.run_plan(
        _plan(
            tmp_path,
            "print('TOTAL 100 12 88%')",
            profile="change",
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "green"
    assert gate["updated_by"] == "assurance-ci"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(88.0)


def test_waiver_written_during_coverage_scan_survives_emission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    command_started = threading.Event()
    release_command = threading.Event()
    errors: list[BaseException] = []

    def blocked_run_external(root: Path, spec: dict, command: list[str]):
        command_started.set()
        if not release_command.wait(timeout=5.0):
            raise RuntimeError("coverage command was not released")
        run = assurance._run_record(
            spec,
            "pass",
            time.monotonic(),
            command=command,
            exit_code=0,
        )
        return run, [], "TOTAL 100 12 88%", "TOTAL 100 12 88%"

    monkeypatch.setattr(assurance, "_run_external", blocked_run_external)

    def execute_scan() -> None:
        try:
            assurance.run_plan(
                _plan(
                    tmp_path,
                    "unused",
                    profile="change",
                    revision=revision,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    scan = threading.Thread(target=execute_scan, name="coverage-waiver-ordering")
    try:
        scan.start()
        assert command_started.wait(timeout=5.0)
        with Store(tmp_path).config_lock():
            gates.waive_gate(
                tmp_path,
                name="coverage:change",
                operator="test-operator",
                reason="waived while coverage was running",
                scope="change",
                expires="2099-01-01T00:00:00Z",
            )
    finally:
        release_command.set()
        scan.join(timeout=10.0)

    assert not scan.is_alive()
    assert errors == []
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "waived"
    assert gate["waiver"]["reason"] == "waived while coverage was running"


def test_cli_finalization_lock_failure_returns_one_after_artifact_write(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _ci_revision(tmp_path, monkeypatch)

    class FailingLock:
        def __enter__(self):
            raise TimeoutError("simulated coverage finalization lock timeout")

        def __exit__(self, *_args):
            return False

    real_config_lock = Store.config_lock
    lock_calls = 0

    def fail_only_finalization_lock(store: Store, **kwargs):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            return real_config_lock(store, **kwargs)
        return FailingLock()

    monkeypatch.setattr(
        Store,
        "config_lock",
        fail_only_finalization_lock,
    )

    rc = _run_assurance_cli(tmp_path)

    captured = capsys.readouterr()
    artifacts = list(
        (tmp_path / ".agenttalk" / "assurance" / "runs").glob("*/artifact.json")
    )
    assert rc == 1
    assert lock_calls == 2
    assert len(artifacts) == 1
    assert captured.out == ""
    assert "could not finalize coverage gate" in captured.err
    assert "simulated coverage finalization lock timeout" in captured.err
    assert "Traceback" not in captured.err


def test_cli_gate_snapshot_load_error_fails_before_scan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _ci_revision(tmp_path, monkeypatch)

    monkeypatch.setattr(
        gates,
        "load_gate_state",
        lambda _root: {
            "schema_version": 1,
            "required_gates": [],
            "gates": {},
            "load_error": "simulated unreadable gate state",
        },
    )

    rc = _run_assurance_cli(tmp_path)

    captured = capsys.readouterr()
    artifacts = list(
        (tmp_path / ".agenttalk" / "assurance" / "runs").glob("*/artifact.json")
    )
    assert rc == 1
    assert artifacts == []
    assert "could not produce artifact" in captured.err
    assert "simulated unreadable gate state" in captured.err
    assert "Traceback" not in captured.err


def test_cli_finalizer_load_error_preserves_gate_and_returns_one_after_artifact(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    _set_green_coverage_gate(tmp_path, revision, scope="change")
    gate_path = tmp_path / ".agenttalk" / "gates.json"
    original_gate_bytes = gate_path.read_bytes()
    real_load_gate_state = gates.load_gate_state
    load_calls = 0

    def fail_only_finalizer_load(root: Path) -> dict:
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            return real_load_gate_state(root)
        return {
            "schema_version": 1,
            "required_gates": [],
            "gates": {},
            "load_error": "simulated transient finalizer read failure",
        }

    monkeypatch.setattr(gates, "load_gate_state", fail_only_finalizer_load)

    rc = _run_assurance_cli(tmp_path)

    captured = capsys.readouterr()
    artifacts = list(
        (tmp_path / ".agenttalk" / "assurance" / "runs").glob("*/artifact.json")
    )
    assert rc == 1
    assert load_calls == 2
    assert len(artifacts) == 1
    assert gate_path.read_bytes() == original_gate_bytes
    assert "could not finalize coverage gate" in captured.err
    assert "simulated transient finalizer read failure" in captured.err
    assert "Traceback" not in captured.err


def test_cli_finalization_persist_failure_returns_one_after_artifact_write(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    _set_green_coverage_gate(tmp_path, revision, scope="change")

    def fail_gate_write(*_args, **_kwargs):
        raise OSError("simulated full disk during coverage finalization")

    monkeypatch.setattr(gates, "set_gate", fail_gate_write)

    rc = _run_assurance_cli(tmp_path)

    captured = capsys.readouterr()
    artifacts = list(
        (tmp_path / ".agenttalk" / "assurance" / "runs").glob("*/artifact.json")
    )
    assert rc == 1
    assert len(artifacts) == 1
    assert _coverage_gate(tmp_path, "change")["status"] == "green"
    assert captured.out == ""
    assert "could not finalize coverage gate" in captured.err
    assert "simulated full disk during coverage finalization" in captured.err
    assert "Traceback" not in captured.err


def test_cli_finalization_failure_does_not_mask_scan_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    _set_green_coverage_gate(tmp_path, revision, scope="change")

    def fail_detection(*_args, **_kwargs):
        raise PermissionError("simulated scan failure")

    def fail_gate_write(*_args, **_kwargs):
        raise OSError("simulated finalization failure")

    monkeypatch.setattr(assurance, "detect_project", fail_detection)
    monkeypatch.setattr(gates, "set_gate", fail_gate_write)

    rc = _run_assurance_cli(tmp_path)

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "could not produce artifact: simulated scan failure" in captured.err
    assert "could not finalize coverage gate: simulated finalization failure" in captured.err
    assert "Traceback" not in captured.err


def test_run_plan_finalization_failure_does_not_mask_scan_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = _plan(tmp_path, "print('unused')")

    def fail_scan(*_args, **_kwargs):
        raise PermissionError("simulated library scan failure")

    def fail_finalization(*_args, **_kwargs):
        raise OSError("simulated library finalization failure")

    monkeypatch.setattr(assurance, "_run_plan", fail_scan)
    monkeypatch.setattr(assurance, "_invalidate_stale_coverage_gate", fail_finalization)

    with pytest.raises(PermissionError) as exc_info:
        assurance.run_plan(plan)

    assert str(exc_info.value) == "simulated library scan failure"
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "simulated library finalization failure"


def test_cli_fresh_ci_coverage_measurement_stays_green(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {
            "pyproject.toml": "[project]\nname = 'coverage-producer-fixture'\nversion = '1.0'\n",
            "src/fixture/__init__.py": "VALUE = 1\n",
        },
    )
    manifest_path = tmp_path / ".agenttalk" / "assurance.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "custom_commands": {
                    "coverage": [
                        sys.executable,
                        "-c",
                        "print('TOTAL 100 12 88%')",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run_assurance_cli(tmp_path)

    assert rc == 0
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "green"
    assert gate["revision"] == revision
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(88.0)


@pytest.mark.parametrize("artifact_name", ["coverage.xml", "coverage.json"])
def test_preexisting_canonical_report_refuses_without_running_or_changing_bytes(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
) -> None:
    original = b"\x00operator-owned coverage report\r\n\xff"
    revision = _ci_revision(tmp_path, monkeypatch)
    report = tmp_path / artifact_name
    report.write_bytes(original)
    original_digest = _sha256(report)

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('coverage-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage-command-ran").exists()
    assert _sha256(report) == original_digest
    assert report.read_bytes() == original
    assert result.tools_run[0]["status"] == "error-optional-tool"
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert artifact_name in gate["reason"]
    assert "coverage command was not run" in gate["reason"]


def test_preflight_names_all_canonical_reports_before_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    reports = {
        tmp_path / "coverage.xml": b"<operator-owned/>",
        tmp_path / "coverage.json": b'{"owner":"operator"}',
    }
    before = {}
    for path, payload in reports.items():
        path.write_bytes(payload)
        before[path] = _sha256(path)

    assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('coverage-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage-command-ran").exists()
    assert {path: _sha256(path) for path in reports} == before
    reason = _coverage_gate(tmp_path)["reason"]
    assert "coverage.xml" in reason
    assert "coverage.json" in reason


@pytest.mark.parametrize("artifact_name", ["coverage.xml", "coverage.json"])
@pytest.mark.parametrize("object_kind", ["directory", "symlink"])
def test_nonregular_canonical_report_refuses_untouched(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
    object_kind: str,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    report = tmp_path / artifact_name
    if object_kind == "directory":
        report.mkdir()
        protected = report / "operator-owned"
    else:
        target_dir = tmp_path.parent / f"{tmp_path.name}-{artifact_name}-target"
        target_dir.mkdir()
        protected = target_dir / "operator-owned"
        _symlink_or_skip(report, protected)
    protected.write_bytes(b"operator-owned nonregular target")
    original_digest = _sha256(protected)

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('coverage-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage-command-ran").exists()
    assert _sha256(protected) == original_digest
    if object_kind == "directory":
        assert report.is_dir()
    else:
        assert report.is_symlink()
    assert result.tools_run[0]["status"] == "error-optional-tool"
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert artifact_name in gate["reason"]


def test_clean_tree_emits_gate_from_stdout_without_canonical_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)

    assurance.run_plan(
        _plan(tmp_path, "print('TOTAL 100 12 88%')", revision=revision)
    )

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(88.0)
    assert not (tmp_path / "coverage.xml").exists()
    assert not (tmp_path / "coverage.json").exists()


@pytest.mark.parametrize("artifact_name", ["coverage.xml", "coverage.json"])
@pytest.mark.parametrize("operation", ["create", "replace"])
def test_external_writer_after_clean_preflight_is_left_untouched_and_forces_red(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
    operation: str,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {".gitignore": ".agenttalk/\ncoverage.xml\ncoverage.json\n"},
    )
    preflight_passed = threading.Event()
    writer_finished = threading.Event()
    report = tmp_path / artifact_name
    final_bytes = b'{"totals":{"percent_covered":99.0},"owner":"external"}'
    canonical_unlinks: list[Path] = []
    real_unlink = Path.unlink

    def observe_unlink(path: Path, *args, **kwargs) -> None:
        if path in {
            tmp_path / "coverage.xml",
            tmp_path / "coverage.json",
        }:
            canonical_unlinks.append(path)
        real_unlink(path, *args, **kwargs)

    def external_writer() -> None:
        assert preflight_passed.wait(timeout=5.0)
        if operation == "replace":
            report.write_bytes(b"first external value")
            replacement = tmp_path / f"{artifact_name}.next"
            replacement.write_bytes(final_bytes)
            replacement.replace(report)
        else:
            report.write_bytes(final_bytes)
        writer_finished.set()

    def fake_run_external(root: Path, spec: dict, command: list[str]):
        assert root == tmp_path
        preflight_passed.set()
        assert writer_finished.wait(timeout=5.0)
        run = assurance._run_record(
            spec,
            "pass",
            time.monotonic(),
            command=command,
            exit_code=0,
        )
        return run, [], "", "TOTAL 100 12 88%"

    monkeypatch.setattr(Path, "unlink", observe_unlink)
    monkeypatch.setattr(assurance, "_run_external", fake_run_external)
    writer = threading.Thread(target=external_writer, name="external-coverage-writer")
    writer.start()
    try:
        assurance.run_plan(_plan(tmp_path, "unused", revision=revision))
    finally:
        writer.join(timeout=5.0)

    assert not writer.is_alive()
    assert writer_finished.is_set()
    assert report.read_bytes() == final_bytes
    assert canonical_unlinks == []
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert artifact_name in gate["reason"]
    measured = gate["evidence"][-1].get("coverage_percent")
    assert measured != 99.0
    if measured is not None:
        assert measured == pytest.approx(88.0)


def test_postflight_coverage_json_is_not_consumed_as_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {".gitignore": ".agenttalk/\ncoverage.xml\ncoverage.json\n"},
    )
    report = tmp_path / "coverage.json"
    payload = b'{"totals":{"percent_covered":99.0},"owner":"command"}'
    script = (
        "from pathlib import Path; "
        f"Path('coverage.json').write_bytes({payload!r})"
    )

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    assert report.read_bytes() == payload
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage.json" in gate["reason"]
    assert "coverage_percent" not in gate["evidence"][-1]


def test_concurrent_stdout_coverage_scans_serialize_without_cross_claiming_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    first_entered = threading.Event()
    second_attempted_lock = threading.Event()
    second_entered = threading.Event()
    serialization_violations: list[str] = []
    real_coverage_lock = Store.coverage_transaction_lock

    @contextmanager
    def observed_coverage_lock(
        store: Store,
        *,
        timeout: float = 70.0,
        poll: float = 0.05,
    ):
        if threading.current_thread().name == "coverage-scan-b":
            second_attempted_lock.set()
        with real_coverage_lock(store, timeout=timeout, poll=poll):
            yield

    def fake_run_external(root: Path, spec: dict, command: list[str]):
        assert root == tmp_path
        scan = command[-1]
        if scan == "scan-a":
            first_entered.set()
            assert second_attempted_lock.wait(timeout=5.0)
            if second_entered.is_set():
                serialization_violations.append(
                    "scan-b entered while scan-a held the coverage lock"
                )
            percent = 81
        else:
            second_entered.set()
            percent = 92
        run = assurance._run_record(
            spec,
            "pass",
            time.monotonic(),
            command=command,
            exit_code=0,
        )
        return run, [], "", f"TOTAL 100 {100 - percent} {percent}%"

    monkeypatch.setattr(Store, "coverage_transaction_lock", observed_coverage_lock)
    monkeypatch.setattr(assurance, "_run_external", fake_run_external)
    results: dict[str, assurance.ScanResult] = {}
    errors: list[BaseException] = []

    def scan(name: str, profile: str) -> None:
        try:
            results[name] = assurance.run_plan(
                _plan(tmp_path, name, profile=profile, revision=revision)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(
        target=scan,
        args=("scan-a", "release"),
        name="coverage-scan-a",
    )
    second = threading.Thread(
        target=scan,
        args=("scan-b", "change"),
        name="coverage-scan-b",
    )
    first.start()
    assert first_entered.wait(timeout=5.0)
    second.start()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert serialization_violations == []
    assert second_entered.is_set()
    assert set(results) == {"scan-a", "scan-b"}
    assert _coverage_gate(tmp_path, "release")["evidence"][-1][
        "coverage_percent"
    ] == pytest.approx(81.0)
    assert _coverage_gate(tmp_path, "change")["evidence"][-1][
        "coverage_percent"
    ] == pytest.approx(92.0)


@pytest.mark.parametrize(
    ("marker_payload", "marker_expected"),
    [
        (None, False),
        ("{torn", True),
        (json.dumps({"schema_version": 1, "artifacts": ["coverage.json"]}), True),
        (
            json.dumps(
                {
                    "schema_version": 2,
                    "artifacts": ["coverage.json"],
                    "phase": "running",
                }
            ),
            True,
        ),
    ],
    ids=["missing-marker", "torn-marker", "legacy-v1", "legacy-v2"],
)
def test_legacy_recovery_residue_refuses_and_is_left_for_manual_recovery(
    tmp_path: Path,
    monkeypatch,
    marker_payload: str | None,
    marker_expected: bool,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    transaction = (
        tmp_path
        / ".agenttalk"
        / "assurance"
        / "coverage-recovery"
        / "transaction-legacy"
    )
    transaction.mkdir(parents=True)
    backup = transaction / "coverage.json"
    backup.write_bytes(b"legacy operator-owned backup")
    root_report = tmp_path / "coverage.xml"
    root_report.write_bytes(b"current operator-owned report")
    marker = transaction / "transaction.json"
    if marker_payload is not None:
        marker.write_text(marker_payload, encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in (backup, marker)
        if path.exists()
    }

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('coverage-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage-command-ran").exists()
    assert backup.read_bytes() == before[backup]
    assert root_report.read_bytes() == b"current operator-owned report"
    assert marker.exists() is marker_expected
    if marker_expected:
        assert marker.read_bytes() == before[marker]
    assert result.tools_run[0]["status"] == "error-optional-tool"
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage command was not run" in gate["reason"]
    assert "coverage.xml" in gate["reason"]
    assert "coverage-recovery/transaction-legacy/coverage.json" in gate[
        "reason"
    ].replace("\\", "/")


def test_older_noncoverage_scan_cannot_invalidate_newer_coverage_attestation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    older_entered = threading.Event()
    release_older = threading.Event()
    results: dict[str, assurance.ScanResult] = {}
    errors: list[BaseException] = []

    older = _plan(
        tmp_path,
        "unused",
        profile="change",
        revision=revision,
    )
    older.run_id = "older-noncoverage-run"
    older.tools = [
        {
            "tool_id": "slow-quality",
            "dimension": "quality",
            "command": ["slow-quality"],
            "timeout_seconds": 10,
        }
    ]
    newer = _plan(
        tmp_path,
        "unused",
        profile="change",
        revision=revision,
    )
    newer.run_id = "newer-coverage-run"

    def fake_run_external(root: Path, spec: dict, command: list[str]):
        stdout = ""
        if spec["tool_id"] == "slow-quality":
            older_entered.set()
            if not release_older.wait(timeout=5.0):
                raise RuntimeError("older scan was not released")
        else:
            stdout = "TOTAL 100 9 91%"
        run = assurance._run_record(
            spec,
            "pass",
            time.monotonic(),
            command=command,
            exit_code=0,
        )
        return run, [], "", stdout

    monkeypatch.setattr(assurance, "_run_external", fake_run_external)

    def execute(name: str, plan: assurance.ScanPlan) -> None:
        try:
            results[name] = assurance.run_plan(plan)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    older_thread = threading.Thread(
        target=execute,
        args=("older", older),
        name="older-noncoverage-scan",
    )
    newer_thread = threading.Thread(
        target=execute,
        args=("newer", newer),
        name="newer-coverage-scan",
    )
    intermediate: dict | None = None
    try:
        older_thread.start()
        assert older_entered.wait(timeout=5.0)
        newer_thread.start()
        newer_thread.join(timeout=10.0)
        assert not newer_thread.is_alive()
        intermediate = json.loads(json.dumps(_coverage_gate(tmp_path, "change")))
        assert intermediate["status"] == "green"
        assert intermediate["evidence"][-1]["coverage_percent"] == pytest.approx(91.0)
    finally:
        release_older.set()
        older_thread.join(timeout=10.0)
        newer_thread.join(timeout=10.0)

    assert not older_thread.is_alive()
    assert not newer_thread.is_alive()
    assert errors == []
    assert set(results) == {"older", "newer"}
    assert intermediate is not None
    assert _coverage_gate(tmp_path, "change") == intermediate


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


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("TOTAL 10 0 1100%", None),
        ("TOTAL 10 0 999%", None),
        ("TOTAL 10 0 12x34%", None),
        ("TOTAL 10 0 87,34%", None),
        ("TOTAL 10 0 96%", 96.0),
        ("Total coverage: 87,34%", None),
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
    ("stdout", "expected"),
    [
        (
            "\x1b[32mTOTAL\x1b[0m 100 12 \x1b[1;32m88%\x1b[0m",
            88.0,
        ),
        (
            "\x1b[36mTotal coverage:\x1b[0m \x1b[1;36m87.34%\x1b[0m",
            87.34,
        ),
    ],
    ids=["coverage-total-row", "pytest-cov-total-coverage"],
)
def test_coverage_parser_accepts_bounded_ansi_sgr_decoration(
    stdout: str,
    expected: float,
) -> None:
    assert parse_coverage_percent(stdout) == pytest.approx(expected)


def test_ansi_sgr_decoration_does_not_create_a_coverage_summary() -> None:
    stdout = "\x1b[32mcoverage command completed at 99%\x1b[0m"

    assert parse_coverage_percent(stdout) is None


@pytest.mark.parametrize(
    "stdout",
    [
        "\x1b[2K Total coverage: 99%",
        "\x9b2K Total coverage: 99%",
        f"\x1b[{'1;' * 17}31m Total coverage: 99%",
    ],
    ids=["non-sgr-control", "c1-control", "overlong-sgr"],
)
def test_coverage_parser_rejects_unsupported_ansi_sequences(stdout: str) -> None:
    assert parse_coverage_percent(stdout) is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            "Total coverage: 99%\nTOTAL 100 50 50%",
            50.0,
        ),
        (
            "TOTAL 100 50 50%\nTotal coverage: 99%",
            99.0,
        ),
    ],
    ids=["coverage-row-last", "pytest-cov-summary-last"],
)
def test_coverage_parser_uses_last_summary_across_supported_formats(
    stdout: str,
    expected: float,
) -> None:
    assert parse_coverage_percent(stdout) == pytest.approx(expected)


def test_coverage_parser_does_not_fallback_from_invalid_final_summary() -> None:
    stdout = "Total coverage: 99%\nTOTAL 100 50 101%"

    assert parse_coverage_percent(stdout) is None


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_coverage_parser_accepts_newline_delimited_summary(line_ending: str) -> None:
    stdout = f"coverage output{line_ending}TOTAL 100 50 50%{line_ending}"

    assert parse_coverage_percent(stdout) == pytest.approx(50.0)


def test_coverage_parser_rejects_bare_carriage_return_rewrites() -> None:
    stdout = "Total coverage: 99%\rprogress output replaced the summary"

    assert parse_coverage_percent(stdout) is None


@pytest.mark.parametrize("as_bytes", [False, True], ids=["str", "bytes"])
def test_oversized_coverage_stdout_is_rejected_before_scanning(
    monkeypatch,
    as_bytes: bool,
) -> None:
    monkeypatch.setattr(coverage_parse, "MAX_COVERAGE_ARTIFACT_BYTES", 64)
    stdout = "TOTAL 10 0 96%\n" + ("x" * 64)
    source = stdout.encode("utf-8") if as_bytes else stdout

    assert parse_coverage_percent(source) is None


@pytest.mark.parametrize("as_bytes", [False, True], ids=["str", "bytes"])
def test_coverage_stdout_at_size_limit_is_accepted(
    monkeypatch,
    as_bytes: bool,
) -> None:
    monkeypatch.setattr(coverage_parse, "MAX_COVERAGE_ARTIFACT_BYTES", 64)
    total = "TOTAL 10 0 96%\n"
    stdout = total + ("x" * (64 - len(total)))
    source = stdout.encode("utf-8") if as_bytes else stdout

    assert parse_coverage_percent(source) == pytest.approx(96.0)


def test_coverage_stdout_limit_counts_utf8_bytes(monkeypatch) -> None:
    monkeypatch.setattr(coverage_parse, "MAX_COVERAGE_ARTIFACT_BYTES", 64)
    stdout = "TOTAL 10 0 96%\n" + ("\N{LATIN SMALL LETTER E WITH ACUTE}" * 25)
    assert len(stdout) < 64
    assert len(stdout.encode("utf-8")) > 64

    assert parse_coverage_percent(stdout) is None


def test_huge_coverage_stdout_fails_closed_and_cli_writes_red_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {
            "pyproject.toml": (
                "[project]\n"
                "name = 'coverage-overflow-fixture'\n"
                "version = '1.0'\n"
            ),
            "src/fixture/__init__.py": "VALUE = 1\n",
        },
    )
    stdout = "Total coverage: " + ("9" * 400) + "%"
    manifest_path = tmp_path / ".agenttalk" / "assurance.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "custom_commands": {
                    "coverage": [
                        sys.executable,
                        "-c",
                        f"print({stdout!r})",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run_assurance_cli(tmp_path, profile="change")

    artifacts = list(
        (tmp_path / ".agenttalk" / "assurance" / "runs").glob("*/artifact.json")
    )
    assert rc == 0
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    coverage_run = next(
        run for run in artifact["tools"]["run"] if run["tool_id"] == "coverage"
    )
    assert coverage_run["status"] == "pass"
    gate = _coverage_gate(tmp_path, "change")
    assert gate["status"] == "red"
    assert "no overall percentage could be parsed" in gate["reason"]
    assert "coverage_percent" not in gate["evidence"][-1]
    assert gate["revision"] == revision


def test_coverage_parser_contains_unexpected_source_exception(
    monkeypatch,
) -> None:
    def fail_stdout_parse(_text: str) -> float | None:
        raise RuntimeError("simulated future parser defect")

    monkeypatch.setattr(coverage_parse, "_from_stdout", fail_stdout_parse)

    assert parse_coverage_percent("TOTAL 10 0 88%") is None


def test_coverage_parser_never_raises_for_deterministic_bytes_corpus() -> None:
    directed = [
        ("huge-positive-int", b"Total coverage: " + (b"9" * 400) + b"%"),
        ("huge-negative-int", b"Total coverage: -" + (b"9" * 400) + b"%"),
        ("positive-overflow-float", b"Total coverage: 1e400%"),
        ("negative-overflow-float", b"Total coverage: -1e400%"),
        ("literal-nan", b"Total coverage: NaN%"),
        ("literal-infinity", b"Total coverage: Infinity%"),
        ("literal-negative-infinity", b"Total coverage: -Infinity%"),
        ("string-nan", b"TOTAL 10 0 'NaN%'"),
        ("string-infinity", b"TOTAL 10 0 'Infinity%'"),
        ("string-negative-infinity", b"TOTAL 10 0 '-Infinity%'"),
        ("negative-percent", b"Total coverage: -0.1%"),
        ("over-hundred", b"Total coverage: 100.1%"),
        ("zero", b"Total coverage: 0%"),
        ("hundred", b"Total coverage: 100%"),
        ("fraction", b"Total coverage: 87.25%"),
        ("root-null", b"null"),
        ("root-bool", b"true"),
        ("root-list", b"[]"),
        ("root-string", b'"totals"'),
        ("totals-null", b'{"totals":null}'),
        ("totals-bool", b'{"totals":true}'),
        ("totals-list", b'{"totals":[]}'),
        ("totals-string", b'{"totals":"wrong"}'),
        ("totals-missing", b'{"other":1}'),
        ("percent-null", b'{"totals":{"percent_covered":null}}'),
        ("percent-bool", b'{"totals":{"percent_covered":true}}'),
        ("percent-list", b'{"totals":{"percent_covered":[]}}'),
        ("percent-dict", b'{"totals":{"percent_covered":{}}}'),
        ("percent-string", b'{"totals":{"percent_covered":"50"}}'),
        ("deep-array", (b"[" * 1200) + b"0" + (b"]" * 1200)),
        ("deep-object", (b'{"x":' * 1200) + b"0" + (b"}" * 1200)),
        ("empty", b""),
        ("truncated", b'{"totals":{"percent_covered":'),
        ("invalid-utf8", b'{"totals":\xff}'),
        (
            "utf8-bom-valid",
            b"\xef\xbb\xbfTOTAL 10 5 50%",
        ),
        ("bom-only", b"\xef\xbb\xbf"),
        ("stdout-total", b"TOTAL 10 0 96%"),
        (
            "rounded-over-hundred",
            b"Total coverage: 100.0000000000000000000000000000000001%",
        ),
        ("rounded-negative", b"Total coverage: -0.0000000000000000000000000000000001%"),
        (
            "multiple-totals-last-wins",
            b"Total coverage: 0%\nTotal coverage: 100%",
        ),
    ]
    payloads = (
        [b""]
        + [bytes((value,)) for value in range(256)]
        + [
            bytes((first, second))
            for first in range(256)
            for second in range(256)
        ]
        + [payload for _name, payload in directed]
    )
    assert len(directed) == 40
    assert len(payloads) == 65_833

    directed_results: dict[str, float | None] = {}
    invocation_count = 0
    for index, payload in enumerate(payloads):
        result = parse_coverage_percent(payload)
        invocation_count += 1
        assert result is None or (
            isinstance(result, float)
            and math.isfinite(result)
            and 0.0 <= result <= 100.0
        ), f"invalid parser result for corpus input {index}: {result!r}"
        if index >= len(payloads) - len(directed):
            name = directed[index - (len(payloads) - len(directed))][0]
            directed_results[name] = result

    assert invocation_count == 65_833
    expected = {
        "zero": 0.0,
        "hundred": 100.0,
        "fraction": 87.25,
        "utf8-bom-valid": 50.0,
        "stdout-total": 96.0,
        "multiple-totals-last-wins": 100.0,
    }
    for name, result in directed_results.items():
        if name in expected:
            assert result == pytest.approx(expected[name])
        else:
            assert result is None


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
