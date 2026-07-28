from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agenttalk import assurance, cli, close, coverage_parse, gates
from agenttalk.coverage_parse import MAX_COVERAGE_ARTIFACT_BYTES, parse_coverage_percent
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
    original = '{"totals": {"percent_covered": 17.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.json": original},
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
            (
                "from pathlib import Path; "
                "Path('coverage.json').write_text("
                "'{\"totals\": {\"percent_covered\": 99.0}}', encoding='utf-8')"
            ),
            revision=revision,
        )
    )

    assert (tmp_path / "coverage.json").read_text(encoding="utf-8") == original
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


def test_coverage_json_created_by_command_is_parsed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    payload = '{"totals": {"percent_covered": 92.5}}'
    script = (
        "from pathlib import Path; "
        f"Path('coverage.json').write_text({payload!r}, encoding='utf-8')"
    )

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "green"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(92.5)


@pytest.mark.parametrize("artifact_name", ["coverage.xml", "coverage.json"])
def test_symlinked_pre_run_report_aborts_before_command_and_preserves_target(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    external_dir = tmp_path.parent / f"{tmp_path.name}-external"
    external_dir.mkdir()
    target = external_dir / "operator-report"
    original = b"operator-owned coverage bytes"
    target.write_bytes(original)
    report = tmp_path / artifact_name
    _symlink_or_skip(report, target)
    command_marker = tmp_path / "coverage-command-ran"
    script = (
        "from pathlib import Path; "
        "Path('coverage-command-ran').write_text('ran', encoding='utf-8'); "
        f"Path({artifact_name!r}).write_bytes(b'attacker-controlled replacement')"
    )

    result = assurance.run_plan(_plan(tmp_path, script, revision=revision))

    assert not command_marker.exists()
    assert target.read_bytes() == original
    assert report.is_symlink()
    assert result.tools_run[0]["status"] == "error-optional-tool"
    assert "coverage command was not run" in _coverage_gate(tmp_path)["reason"]


def test_directory_pre_run_report_aborts_before_command_and_preserves_contents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    report = tmp_path / "coverage.json"
    report.mkdir()
    sentinel = report / "operator-report"
    original = b"operator-owned directory contents"
    sentinel.write_bytes(original)
    script = (
        "from pathlib import Path; "
        "Path('coverage-command-ran').write_text('ran', encoding='utf-8'); "
        "Path('coverage.json').write_bytes(b'attacker-controlled replacement')"
    )

    result = assurance.run_plan(_plan(tmp_path, script, revision=revision))

    assert not (tmp_path / "coverage-command-ran").exists()
    assert sentinel.read_bytes() == original
    assert result.tools_run[0]["status"] == "error-optional-tool"
    assert "coverage command was not run" in _coverage_gate(tmp_path)["reason"]


def test_unmovable_pre_run_report_aborts_before_command_and_preserves_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = b'{"totals": {"percent_covered": 17.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.json": original.decode("utf-8")},
    )
    report = tmp_path / "coverage.json"
    real_replace = assurance.os.replace

    def fail_quarantine(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path == report
            and destination_path.parent.name.startswith("transaction-")
            and destination_path.parent.parent.name == "coverage-recovery"
        ):
            raise PermissionError("simulated quarantine denial")
        return real_replace(source, destination)

    monkeypatch.setattr(assurance.os, "replace", fail_quarantine)
    script = (
        "from pathlib import Path; "
        "Path('coverage-command-ran').write_text('ran', encoding='utf-8'); "
        "Path('coverage.json').write_bytes(b'attacker-controlled replacement')"
    )

    result = assurance.run_plan(_plan(tmp_path, script, revision=revision))

    assert not (tmp_path / "coverage-command-ran").exists()
    assert report.read_bytes() == original
    assert result.tools_run[0]["status"] == "error-optional-tool"
    assert "coverage command was not run" in _coverage_gate(tmp_path)["reason"]


def test_preparation_abort_preserves_report_created_after_inspection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    (tmp_path / "coverage.xml").mkdir()
    created = b'{"totals": {"percent_covered": 33.0}, "owner": "operator"}'
    real_prepare = assurance._prepare_coverage_artifacts

    def prepare_then_external_write(root: Path):
        state = real_prepare(root)
        assert state.preparation_error is not None
        (root / "coverage.json").write_bytes(created)
        return state

    monkeypatch.setattr(
        assurance,
        "_prepare_coverage_artifacts",
        prepare_then_external_write,
    )

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('coverage-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "coverage-command-ran").exists()
    assert (tmp_path / "coverage.json").read_bytes() == created
    assert result.tools_run[0]["status"] == "error-optional-tool"


def test_aborted_partial_quarantine_never_deletes_reappeared_report_on_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_xml = b"<operator-owned-xml/>"
    original_json = b'{"totals": {"percent_covered": 17.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {
            "coverage.xml": original_xml.decode("utf-8"),
            "coverage.json": original_json.decode("utf-8"),
        },
    )
    xml_report = tmp_path / "coverage.xml"
    json_report = tmp_path / "coverage.json"
    reappeared = b"<new-operator-owned-xml/>"
    real_prepare = assurance._prepare_coverage_artifacts
    real_replace = assurance.os.replace

    def fail_second_quarantine(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path == json_report
            and destination_path.parent.name.startswith("transaction-")
            and destination_path.parent.parent.name == "coverage-recovery"
        ):
            raise PermissionError("simulated second quarantine denial")
        return real_replace(source, destination)

    def prepare_then_reappear(root: Path):
        state = real_prepare(root)
        assert state.preparation_error is not None
        xml_report.write_bytes(reappeared)
        return state

    monkeypatch.setattr(assurance.os, "replace", fail_second_quarantine)
    monkeypatch.setattr(
        assurance,
        "_prepare_coverage_artifacts",
        prepare_then_reappear,
    )

    first = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('first-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "first-command-ran").exists()
    assert xml_report.read_bytes() == reappeared
    assert json_report.read_bytes() == original_json
    assert any("manual recovery" in error for error in first.runner_errors)
    markers = list(
        (tmp_path / ".agenttalk" / "assurance" / "coverage-recovery").glob(
            "*/transaction.json"
        )
    )
    assert len(markers) == 1
    transaction = markers[0].parent
    assert (transaction / "coverage.xml").read_bytes() == original_xml
    assert json.loads(markers[0].read_text(encoding="utf-8"))["phase"] == "preparing"

    monkeypatch.setattr(assurance.os, "replace", real_replace)
    monkeypatch.setattr(assurance, "_prepare_coverage_artifacts", real_prepare)
    second = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('second-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "second-command-ran").exists()
    assert xml_report.read_bytes() == reappeared
    assert (transaction / "coverage.xml").read_bytes() == original_xml
    assert any("manual recovery" in error for error in second.runner_errors)


@pytest.mark.parametrize(
    ("artifact_name", "original", "reappeared"),
    [
        (
            "coverage.json",
            b'{"totals": {"percent_covered": 17.0}, "owner": "original"}',
            b'{"totals": {"percent_covered": 91.0}, "owner": "operator-new"}',
        ),
        (
            "coverage.xml",
            b'<coverage line-rate="0.17" owner="original"/>',
            b'<coverage line-rate="0.91" owner="operator-new"/>',
        ),
    ],
)
def test_running_crash_recovery_preserves_reappeared_report_and_backup(
    tmp_path: Path,
    monkeypatch,
    artifact_name: str,
    original: bytes,
    reappeared: bytes,
) -> None:
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {artifact_name: original.decode("utf-8")},
    )
    state = assurance._prepare_coverage_artifacts(tmp_path)
    assert state.preparation_error is None
    assert state.quarantine is not None
    assert assurance._mark_coverage_transaction_running(state) is None
    report = tmp_path / artifact_name
    report.write_bytes(reappeared)
    marker = state.quarantine / "transaction.json"
    backup = state.quarantine / artifact_name

    result = assurance.run_plan(
        _plan(
            tmp_path,
            "from pathlib import Path; Path('next-command-ran').touch()",
            revision=revision,
        )
    )

    assert not (tmp_path / "next-command-ran").exists()
    assert report.read_bytes() == reappeared
    assert backup.read_bytes() == original
    assert json.loads(marker.read_text(encoding="utf-8"))["phase"] == "running"
    assert any(
        artifact_name in error and "both files were preserved for manual recovery" in error
        for error in result.runner_errors
    )
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage command was not run" in gate["reason"]


def test_failed_command_output_is_cleaned_and_original_report_restored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = b'{"totals": {"percent_covered": 17.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.json": original.decode("utf-8")},
    )
    report = tmp_path / "coverage.json"
    generated = '{"totals": {"percent_covered": 91.0}, "owner": "command"}'

    result = assurance.run_plan(
        _plan(
            tmp_path,
            (
                "from pathlib import Path; "
                f"Path('coverage.json').write_text({generated!r}, encoding='utf-8'); "
                "raise SystemExit(1)"
            ),
            revision=revision,
        )
    )

    assert result.tools_run[0]["status"] == "error-optional-tool"
    assert report.read_bytes() == original
    assert not list(
        (tmp_path / ".agenttalk" / "assurance" / "coverage-recovery").glob(
            "*/transaction.json"
        )
    )
    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(91.0)


def test_coverage_phase_transition_failure_aborts_and_restores_before_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = '{"totals": {"percent_covered": 17.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.json": original},
    )
    real_atomic_write = assurance._atomic_write_text

    def fail_running_marker(path: Path, text: str, *args, **kwargs):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        if Path(path).name == "transaction.json" and payload.get("phase") == "running":
            raise OSError("simulated phase transition failure")
        return real_atomic_write(path, text, *args, **kwargs)

    monkeypatch.setattr(assurance, "_atomic_write_text", fail_running_marker)

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
    assert any("could not commit" in error for error in result.runner_errors)
    assert not list(
        (tmp_path / ".agenttalk" / "assurance" / "coverage-recovery").glob(
            "*/transaction.json"
        )
    )


def test_concurrent_coverage_transactions_do_not_cross_claim_or_lose_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = tmp_path / "coverage.json"
    original = b'{"totals": {"percent_covered": 12.0}, "owner": "operator"}'
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        {"coverage.json": original.decode("utf-8")},
    )
    a_output_ready = threading.Event()
    b_finished = threading.Event()

    def fake_run_external(root: Path, spec: dict, command: list[str]):
        scan = command[-1]
        if scan == "scan-a":
            (root / "coverage.json").write_text(
                '{"totals": {"percent_covered": 81.0}, "owner": "scan-a"}',
                encoding="utf-8",
            )
            a_output_ready.set()
            b_finished.wait(timeout=2.0)
        else:
            (root / "coverage.json").write_text(
                '{"totals": {"percent_covered": 92.0}, "owner": "scan-b"}',
                encoding="utf-8",
            )
        run = assurance._run_record(
            spec,
            "pass",
            time.monotonic(),
            command=command,
            exit_code=0,
        )
        return run, [], "", ""

    monkeypatch.setattr(assurance, "_run_external", fake_run_external)
    results: dict[str, assurance.ScanResult] = {}
    errors: list[BaseException] = []

    def scan(name: str, profile: str) -> None:
        try:
            results[name] = assurance.run_plan(
                _plan(
                    tmp_path,
                    name,
                    profile=profile,
                    revision=revision,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if name == "scan-b":
                b_finished.set()

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
    assert a_output_ready.wait(timeout=5.0)
    second.start()
    first.join(timeout=5.0)
    assert not first.is_alive()
    second.join(timeout=5.0)
    assert not second.is_alive()
    assert errors == []
    assert set(results) == {"scan-a", "scan-b"}
    assert results["scan-a"].runner_errors == []
    assert results["scan-b"].runner_errors == []
    assert report.read_bytes() == original
    assert _coverage_gate(tmp_path, "release")["evidence"][-1][
        "coverage_percent"
    ] == pytest.approx(81.0)
    assert _coverage_gate(tmp_path, "change")["evidence"][-1][
        "coverage_percent"
    ] == pytest.approx(92.0)


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
        if spec["tool_id"] == "slow-quality":
            older_entered.set()
            if not release_older.wait(timeout=5.0):
                raise RuntimeError("older scan was not released")
        else:
            (root / "coverage.json").write_text(
                '{"totals": {"percent_covered": 91.0}}',
                encoding="utf-8",
            )
        run = assurance._run_record(
            spec,
            "pass",
            time.monotonic(),
            command=command,
            exit_code=0,
        )
        return run, [], "", ""

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


@pytest.mark.parametrize(
    "payload_expression",
    [
        repr('<coverage line-rate="0.5"/>'),
        repr(
            '<!DOCTYPE coverage [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<coverage line-rate="&xxe;"/>'
        ),
        f"'<coverage>' + (' ' * {MAX_COVERAGE_ARTIFACT_BYTES + 1}) + '</coverage>'",
    ],
    ids=["valid", "doctype-external-entity", "oversized"],
)
def test_coverage_xml_is_never_accepted_as_evidence(
    tmp_path: Path,
    monkeypatch,
    payload_expression: str,
) -> None:
    revision = _ci_revision(tmp_path, monkeypatch)
    script = (
        "from pathlib import Path; "
        f"Path('coverage.xml').write_text({payload_expression}, encoding='utf-8')"
    )

    assurance.run_plan(_plan(tmp_path, script, revision=revision))

    gate = _coverage_gate(tmp_path)
    assert gate["status"] == "red"
    assert "coverage_percent" not in gate["evidence"][-1]
    assert not (tmp_path / "coverage.xml").exists()


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
    payload = '{"totals": {"percent_covered": 75.0}}'
    revision = _ci_revision(tmp_path, monkeypatch, {"coverage.json": payload})
    script = (
        "import os; from pathlib import Path; "
        f"Path('coverage.next').write_text({payload!r}, encoding='utf-8'); "
        "os.replace('coverage.next', 'coverage.json')"
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


@pytest.mark.parametrize("preexisting_json", [False, True], ids=["fresh", "backed-up"])
def test_artifact_removal_failure_keeps_transaction_open(
    tmp_path: Path,
    monkeypatch,
    preexisting_json: bool,
) -> None:
    original_xml = '<coverage line-rate="0.25"/>'
    original_json = '{"totals": {"percent_covered": 17.0}}'
    generated_json = '{"totals": {"percent_covered": 81.0}}'
    files = {"coverage.xml": original_xml}
    if preexisting_json:
        files["coverage.json"] = original_json
    revision = _ci_revision(
        tmp_path,
        monkeypatch,
        files,
    )
    real_remove = assurance._remove_generated_coverage_artifact

    def fail_fresh_json_removal(path: Path) -> bool:
        if path == tmp_path / "coverage.json":
            return False
        return real_remove(path)

    monkeypatch.setattr(
        assurance,
        "_remove_generated_coverage_artifact",
        fail_fresh_json_removal,
    )

    result = assurance.run_plan(
        _plan(
            tmp_path,
            (
                "from pathlib import Path; "
                f"Path('coverage.json').write_text({generated_json!r}, encoding='utf-8')"
            ),
            revision=revision,
        )
    )

    gate = _coverage_gate(tmp_path)
    markers = list(
        (tmp_path / ".agenttalk" / "assurance" / "coverage-recovery").glob(
            "*/transaction.json"
        )
    )
    assert gate["status"] == "red"
    assert gate["evidence"][-1]["coverage_percent"] == pytest.approx(81.0)
    assert "could not remove generated coverage.json" in gate["reason"]
    assert any("could not remove generated coverage.json" in error for error in result.runner_errors)
    assert (tmp_path / "coverage.json").read_text(encoding="utf-8") == generated_json
    assert (tmp_path / "coverage.xml").read_text(encoding="utf-8") == original_xml
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker["phase"] == "running"
    expected_artifacts = ["coverage.xml", "coverage.json"] if preexisting_json else ["coverage.xml"]
    assert marker["artifacts"] == expected_artifacts
    backup_json = markers[0].parent / "coverage.json"
    if preexisting_json:
        assert backup_json.read_text(encoding="utf-8") == original_json
    else:
        assert not backup_json.exists()


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


def test_oversized_coverage_json_is_rejected_before_parsing() -> None:
    json_text = (
        '{"totals": {"percent_covered": 50.0}, "padding": "'
        + (" " * MAX_COVERAGE_ARTIFACT_BYTES)
        + '"}'
    )

    assert parse_coverage_percent("", json_text=json_text) is None


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


@pytest.mark.parametrize("value", ["99.0", True, None, [], {}])
def test_coverage_json_requires_a_numeric_percent(value) -> None:
    json_text = json.dumps({"totals": {"percent_covered": value}})

    assert parse_coverage_percent("", json_text=json_text) is None


def test_huge_coverage_json_fails_closed_and_cli_writes_red_artifact(
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
    payload = json.dumps({"totals": {"percent_covered": 10**400}})
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
                        (
                            "from pathlib import Path; "
                            f"Path('coverage.json').write_text({payload!r}, encoding='utf-8')"
                        ),
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
    def fail_json_parse(_text: str) -> float | None:
        raise RuntimeError("simulated future parser defect")

    monkeypatch.setattr(coverage_parse, "_from_json", fail_json_parse)

    assert parse_coverage_percent(
        "TOTAL 10 0 88%",
        json_text='{"totals": {"percent_covered": 99.0}}',
    ) == pytest.approx(88.0)


def test_coverage_parser_never_raises_for_deterministic_bytes_corpus() -> None:
    directed = [
        ("huge-positive-int", json.dumps({"totals": {"percent_covered": 10**400}}).encode()),
        ("huge-negative-int", json.dumps({"totals": {"percent_covered": -(10**400)}}).encode()),
        ("positive-overflow-float", b'{"totals":{"percent_covered":1e400}}'),
        ("negative-overflow-float", b'{"totals":{"percent_covered":-1e400}}'),
        ("literal-nan", b'{"totals":{"percent_covered":NaN}}'),
        ("literal-infinity", b'{"totals":{"percent_covered":Infinity}}'),
        ("literal-negative-infinity", b'{"totals":{"percent_covered":-Infinity}}'),
        ("string-nan", b'{"totals":{"percent_covered":"NaN"}}'),
        ("string-infinity", b'{"totals":{"percent_covered":"Infinity"}}'),
        ("string-negative-infinity", b'{"totals":{"percent_covered":"-Infinity"}}'),
        ("negative-percent", b'{"totals":{"percent_covered":-0.1}}'),
        ("over-hundred", b'{"totals":{"percent_covered":100.1}}'),
        ("zero", b'{"totals":{"percent_covered":0}}'),
        ("hundred", b'{"totals":{"percent_covered":100}}'),
        ("fraction", b'{"totals":{"percent_covered":87.25}}'),
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
            b'\xef\xbb\xbf{"totals":{"percent_covered":50.0}}',
        ),
        ("bom-only", b"\xef\xbb\xbf"),
        ("stdout-total", b"TOTAL 10 0 96%"),
        (
            "rounded-over-hundred",
            b'{"totals":{"percent_covered":100.0000000000000000000000000000000001}}',
        ),
        ("rounded-negative", b'{"totals":{"percent_covered":-1e-400}}'),
        (
            "duplicate-percent",
            b'{"totals":{"percent_covered":0,"percent_covered":100}}',
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
    directed_stdout_results: dict[str, float | None] = {}
    invocation_count = 0
    for index, payload in enumerate(payloads):
        stdout_result = parse_coverage_percent(payload)
        json_result = parse_coverage_percent(b"", json_text=payload)
        invocation_count += 2
        for result in (stdout_result, json_result):
            assert result is None or (
                isinstance(result, float)
                and math.isfinite(result)
                and 0.0 <= result <= 100.0
            ), f"invalid parser result for corpus input {index}: {result!r}"
        if index >= len(payloads) - len(directed):
            name = directed[index - (len(payloads) - len(directed))][0]
            directed_results[name] = json_result
            directed_stdout_results[name] = stdout_result

    assert invocation_count == 131_666
    assert directed_results["zero"] == 0.0
    assert directed_results["hundred"] == 100.0
    assert directed_results["fraction"] == pytest.approx(87.25)
    assert directed_results["utf8-bom-valid"] == 50.0
    assert directed_stdout_results["stdout-total"] == 96.0
    for name in {
        "huge-positive-int",
        "huge-negative-int",
        "positive-overflow-float",
        "negative-overflow-float",
        "literal-nan",
        "literal-infinity",
        "literal-negative-infinity",
        "string-nan",
        "string-infinity",
        "string-negative-infinity",
        "negative-percent",
        "over-hundred",
        "rounded-over-hundred",
        "rounded-negative",
        "duplicate-percent",
        "root-null",
        "root-bool",
        "root-list",
        "root-string",
        "totals-null",
        "totals-bool",
        "totals-list",
        "totals-string",
        "totals-missing",
        "percent-null",
        "percent-bool",
        "percent-list",
        "percent-dict",
        "percent-string",
        "deep-array",
        "deep-object",
        "empty",
        "truncated",
        "invalid-utf8",
        "bom-only",
        "stdout-total",
    }:
        assert directed_results[name] is None


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
