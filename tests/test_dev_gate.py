from __future__ import annotations

import copy
import json
import os
import platform
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import dev_gate
from agenttalk.cli import build_parser, cmd_dev_gate


def _manifest() -> dict:
    return json.loads(Path("dev-gate.json").read_text(encoding="utf-8"))


def _check(check_id: str, manifest: dict, minor: str, *, status: str = "pass") -> dict:
    kind = check_id.split("-", 1)[0]
    mode = None
    python = None
    provenance = None
    if check_id.startswith("pytest-"):
        _, mode, suffix = check_id.split("-")
        python = f"{suffix[2]}.{suffix[3:]}"
        kind = "pytest"
        provenance = {
            "expected_root": str((Path.cwd() / "src").resolve()),
            "observed_path": str((Path.cwd() / "src" / "agenttalk" / "__init__.py").resolve()),
            "version": "0.78.1",
        }
    elif check_id.startswith(("wheel-install-", "wheel-dependency-check-", "wheel-contract-")):
        prefix, suffix = check_id.rsplit("-", 1)
        python = f"{suffix[2]}.{suffix[3:]}"
        kind = prefix
        mode = "wheel"
        if prefix == "wheel-contract":
            provenance = {
                "expected_root": str((Path.cwd() / "wheel").resolve()),
                "observed_path": str((Path.cwd() / "wheel" / "agenttalk" / "__init__.py").resolve()),
                "version": "0.78.1",
            }
    elif check_id == "package-build":
        kind = "python-build"
    elif check_id in {"git-binding", "final-binding", "pip-audit"}:
        kind = check_id
    python_path = str((Path.cwd() / "python").resolve())
    tool_path = python_path
    runtime_environment = None
    if python is not None and mode == "wheel":
        role = "test" if check_id.startswith("pytest-wheel-") else "runtime"
        prefix_path = (Path.cwd() / f"{role}-venv-{python.replace('.', '')}").resolve()
        runtime_python = prefix_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        runtime_environment = {
            "role": role,
            "requested": python,
            "creator_path": python_path,
            "python_path": str(runtime_python),
            "prefix": str(prefix_path),
            "base_prefix": str((Path.cwd() / f"base-{python.replace('.', '')}").resolve()),
            "system_site_packages": False,
        }
        tool_path = str(runtime_python)
        if provenance is not None:
            provenance = {
                "expected_root": str(prefix_path),
                "observed_path": str(prefix_path / "site-packages" / "agenttalk" / "__init__.py"),
                "version": "0.78.1",
            }
    if check_id in {"git-binding", "final-binding"}:
        tool_path = str((Path.cwd() / ("git.exe" if os.name == "nt" else "git")).resolve())
        argv = [tool_path, "status", "--porcelain=v1", "--untracked-files=all"]
    elif check_id.startswith("pytest-"):
        spec = manifest["checks"]["pytest"]
        argv = dev_gate.isolated_tool_argv(
            tool_path,
            "pytest",
            *spec["args"],
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str((Path.cwd() / "pytest-temp").resolve()),
            *spec["paths"],
            candidate_import_root=(Path.cwd() / "src").resolve() if mode == "source" else None,
        )
    elif check_id == "package-build":
        argv = dev_gate.isolated_tool_argv(
            python_path,
            "build",
            "--no-isolation",
            "--sdist",
            "--wheel",
            "--outdir",
            str((Path.cwd() / "dist").resolve()),
        )
    elif check_id.startswith("wheel-install-"):
        argv = dev_gate.isolated_tool_argv(
            tool_path,
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--index-url",
            manifest["checks"]["wheel-contract"]["dependency_index"],
            str((Path.cwd() / "dist" / "agenttalk.whl").resolve()),
        )
    elif check_id.startswith("wheel-dependency-check-"):
        argv = dev_gate.isolated_tool_argv(tool_path, "pip", "check")
    elif check_id.startswith("wheel-contract-"):
        argv = dev_gate.isolated_tool_argv(
            tool_path,
            "agenttalk",
            "--version",
        )
    elif check_id == "ruff":
        argv = dev_gate.isolated_tool_argv(
            python_path,
            "ruff",
            "check",
            "--no-cache",
            *manifest["checks"]["ruff"]["paths"],
        )
    elif check_id == "bandit":
        spec = manifest["checks"]["bandit"]
        argv = dev_gate.isolated_tool_argv(
            python_path,
            "bandit",
            "-r",
            *spec["paths"],
            "-x",
            ",".join(spec["exclude"]),
        )
    elif check_id == "pip-audit":
        argv = dev_gate.isolated_tool_argv(
            python_path,
            "pip_audit",
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--requirement",
            str((Path.cwd() / "audit-requirements.txt").resolve()),
        )
    elif check_id == "semgrep":
        tool_path = str((Path.cwd() / ("semgrep.exe" if os.name == "nt" else "semgrep")).resolve())
        spec = manifest["checks"]["semgrep"]
        argv = [tool_path, "scan", *[f"--config={value}" for value in spec["configs"]]]
        argv.extend(["--error", "--timeout", str(spec["rule_timeout_seconds"]), "--strict"])
    elif check_id == "zizmor":
        tool_path = str((Path.cwd() / ("zizmor.exe" if os.name == "nt" else "zizmor")).resolve())
        argv = [tool_path, *manifest["checks"]["zizmor"]["paths"]]
    elif check_id == "gitleaks":
        tool_path = str((Path.cwd() / ("gitleaks.exe" if os.name == "nt" else "gitleaks")).resolve())
        argv = [
            tool_path,
            "git",
            "--config",
            str((Path.cwd() / "candidate-static" / manifest["checks"]["gitleaks"]["config"]).resolve()),
            "--log-opts=--all",
            "--redact",
            "--no-color",
            "--no-banner",
            str(Path.cwd().resolve()),
        ]
    else:
        raise AssertionError(f"unsupported check fixture {check_id}")
    return {
        "id": check_id,
        "kind": kind,
        "mode": mode,
        "python": python,
        "required": True,
        "status": status,
        "argv": argv,
        "tool": {"path": tool_path, "version": "1"},
        "exit_code": 0 if status == "pass" else 1,
        "duration_ms": 1,
        "reason_code": None if status == "pass" else "check_failed",
        "diagnostic": "",
        "log": {"path": str(Path("log.txt").resolve()), "sha256": "a" * 64},
        "import_provenance": provenance,
        "runtime_environment": runtime_environment,
    }


def _leg_artifact(manifest: dict, leg: str, *, status: str = "pass") -> dict:
    profile = manifest["profiles"]["release"]
    common_digest = dev_gate.logical_plan_digest(manifest, "release")
    required = dev_gate.required_check_ids(manifest, "release", execution_scope="ci-leg", ci_leg=leg)
    return {
        "schema_version": 1,
        "artifact_type": "agenttalk-dev-gate-run",
        "run_id": f"run-{leg.replace('/', '-')}",
        "started_at": "2026-07-20T00:00:00Z",
        "finished_at": "2026-07-20T00:00:01Z",
        "profile": "release",
        "verdict": "pass" if status == "pass" else "block",
        "complete": False,
        "execution_scope": "ci-leg",
        "ci_leg": leg,
        "subject": {
            "candidate_sha": "1" * 40,
            "candidate_tree": "2" * 40,
            "version": "0.78.1",
            "clean_before": True,
            "clean_after": True,
            "head_stable": True,
        },
        "manifest": {
            "path": "dev-gate.json",
            "schema_version": 1,
            "git_blob_id": "3" * 40,
            "sha256": dev_gate.sha256_bytes(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ),
            "logical_plan_sha256": common_digest,
        },
        "authority": {
            "declared_required_ci_matrix": dev_gate.expected_ci_legs(manifest, "release"),
            "local_interpreters": profile["local"]["python_minors"],
            "ci_aggregate_authoritative": True,
            "ci_native_exceptions": manifest["ci_native_exceptions"],
        },
        "runner": {
            "agenttalk_version": "0.78.1",
            "module_path": "src/agenttalk/dev_gate.py",
            "git_blob_id": "4" * 40,
            "module_sha256": "5" * 64,
            "os": leg.split("/", 1)[0],
            "architecture": "x86_64",
        },
        "isolation": {
            "temp_outside_candidate": True,
            "temp_outside_store": True,
            "pytest_cache_disabled": True,
            "bytecode_disabled": True,
            "phase_isolated_exports": True,
            "pip_configuration_disabled": True,
            "child_path_sanitized": True,
        },
        "interpreters": [
            {
                "requested": leg.split("/", 1)[1],
                "path": str((Path.cwd() / "python").resolve()),
                "implementation": "CPython",
                "version": leg.split("/", 1)[1] + ".0",
                "status": "pass",
            }
        ],
        "required_check_ids": required,
        "checks": [
            _check(
                check_id,
                manifest,
                leg.split("/", 1)[1],
                status=status if index == 0 else "pass",
            )
            for index, check_id in enumerate(required)
        ],
        "artifacts": {
            "sdist": {
                "path": str((Path.cwd() / "dist" / "agenttalk.tar.gz").resolve()),
                "filename": "agenttalk.tar.gz",
                "sha256": "6" * 64,
                "size_bytes": 1,
            },
            "wheel": {
                "path": str((Path.cwd() / "dist" / "agenttalk.whl").resolve()),
                "filename": "agenttalk.whl",
                "sha256": "7" * 64,
                "size_bytes": 1,
            },
            **(
                {
                    "audit_requirements": {
                        "path": str((Path.cwd() / "audit-requirements.txt").resolve()),
                        "filename": "audit-requirements.txt",
                        "sha256": "8" * 64,
                        "size_bytes": 1,
                    }
                }
                if leg == profile["ci"]["canonical_static_leg"]
                else {}
            ),
        },
        "external_inputs": [
            *[
                {
                    "check_id": check_id,
                    "kind": "live-package-index",
                    "locator": manifest["checks"]["wheel-contract"]["dependency_index"],
                    "mutable": True,
                    "identity": "live-service-unversioned",
                    "observed_at": "2026-07-20T00:00:00Z",
                }
                for check_id in required
                if check_id.startswith("wheel-install-") or check_id.startswith("pytest-wheel-")
            ],
            *(
                [
                {
                    "check_id": "pip-audit",
                    "kind": "live-advisory-database",
                    "locator": "PyPI advisory database",
                    "mutable": True,
                    "identity": "live-service-unversioned",
                    "observed_at": "2026-07-20T00:00:00Z",
                },
                *[
                    {
                        "check_id": "semgrep",
                        "kind": "live-rule-registry",
                        "locator": locator,
                        "mutable": True,
                        "identity": "live-registry-unversioned",
                        "observed_at": "2026-07-20T00:00:00Z",
                    }
                    for locator in ("p/python", "p/security-audit")
                ],
                ]
                if leg == profile["ci"]["canonical_static_leg"]
                else []
            ),
        ],
        "blockers": [] if status == "pass" else [{"code": "check_failed", "check_id": required[0], "detail": "x"}],
        "summary": {
            "required": len(required),
            "passed": len(required) if status == "pass" else len(required) - 1,
            "blocked": 0 if status == "pass" else 1,
        },
    }


def _binding(manifest: dict) -> dev_gate.CandidateBinding:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return dev_gate.CandidateBinding(
        root=Path.cwd(),
        candidate_sha="1" * 40,
        candidate_tree="2" * 40,
        manifest_git_blob="3" * 40,
        manifest_sha256=dev_gate.sha256_bytes(raw),
        manifest_bytes=raw,
        runner_git_blob="4" * 40,
        runner_module_sha256="5" * 64,
        clean=True,
        dirty_entries=(),
        in_progress=(),
    )


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _synthetic_wheel(
    path: Path,
    *,
    package_code: str,
    requirements: tuple[str, ...] = (),
) -> Path:
    metadata = [
        "Metadata-Version: 2.1",
        "Name: agenttalk",
        "Version: 0.78.1",
        *[f"Requires-Dist: {requirement}" for requirement in requirements],
        "",
    ]
    files = {
        "agenttalk/__init__.py": package_code,
        "agenttalk-0.78.1.dist-info/METADATA": "\n".join(metadata),
        "agenttalk-0.78.1.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: agenttalk-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record_name = "agenttalk-0.78.1.dist-info/RECORD"
    files[record_name] = "".join(f"{name},,\n" for name in [*files, record_name])
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_manifest_declares_real_ci_matrix_separately_from_local_interpreters() -> None:
    manifest = dev_gate.validate_manifest(_manifest())

    assert manifest["profiles"]["release"]["local"]["python_minors"] == ["3.10", "3.14"]
    assert dev_gate.expected_ci_legs(manifest, "release") == [
        f"{os_name}/{python}"
        for os_name in ("linux", "windows", "macos")
        for python in ("3.10", "3.11", "3.12", "3.13")
    ]
    assert manifest["profiles"]["release"]["ci"]["canonical_static_leg"] == "linux/3.12"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update({"unknown": True}),
        lambda data: data["profiles"]["release"]["ci"].update({"python_minors": ["3.10"]}),
        lambda data: data["profiles"]["release"]["ci"].update({"canonical_static_leg": "linux/3.14"}),
        lambda data: data["checks"].pop("semgrep"),
        lambda data: data.update({"ci_native_exceptions": []}),
        lambda data: data["checks"]["pytest"].update({"paths": ["tests/test_dev_gate.py"]}),
        lambda data: data["checks"]["ruff"].update({"paths": []}),
        lambda data: data["checks"]["bandit"].update({"exclude": ["src"]}),
        lambda data: data["checks"]["gitleaks"].update({"require_full_history": False}),
        lambda data: data["checks"]["pip-audit"].update({"strict": False}),
        lambda data: data["checks"]["semgrep"].update({"configs": []}),
        lambda data: data["checks"]["semgrep"].update({"error": False}),
        lambda data: data["checks"]["package-build"].update({"sdist": False}),
        lambda data: data["checks"]["package-build"].update({"required_sdist_paths": []}),
        lambda data: data["checks"]["wheel-contract"].update({"required_wheel_resources": []}),
    ],
)
def test_manifest_cannot_weaken_required_floor(mutate) -> None:
    manifest = _manifest()
    mutate(manifest)

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_manifest(manifest)


@pytest.mark.parametrize("check_id", sorted(dev_gate.TIMEOUT_CHECKS))
def test_manifest_requires_every_subprocess_timeout(check_id: str) -> None:
    manifest = _manifest()
    manifest["checks"][check_id].pop("timeout_seconds")

    with pytest.raises(dev_gate.GateBlock, match="timeout_seconds"):
        dev_gate.validate_manifest(manifest)


def test_logical_plan_digest_is_runtime_path_independent_and_semantic() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    original = dev_gate.logical_plan_digest(manifest, "release")

    runtime_a = {"python": r"C:\venvs\py310\python.exe", "temp": r"D:\tmp\a"}
    runtime_b = {"python": "/opt/python3.10/bin/python", "temp": "/opt/agenttalk/runtime-b"}
    assert dev_gate.logical_plan_digest(manifest, "release", runtime=runtime_a) == original
    assert dev_gate.logical_plan_digest(manifest, "release", runtime=runtime_b) == original

    changed = copy.deepcopy(manifest)
    changed["checks"]["pytest"]["timeout_seconds"] += 1
    assert dev_gate.logical_plan_digest(changed, "release") != original


def test_required_check_ids_expand_source_and_wheel_and_static_only_on_canonical_leg() -> None:
    manifest = dev_gate.validate_manifest(_manifest())

    canonical = dev_gate.required_check_ids(
        manifest, "release", execution_scope="ci-leg", ci_leg="linux/3.12"
    )
    ordinary = dev_gate.required_check_ids(
        manifest, "release", execution_scope="ci-leg", ci_leg="windows/3.10"
    )

    assert "pytest-source-py312" in canonical
    assert "pytest-wheel-py312" in canonical
    assert "semgrep" in canonical and "zizmor" in canonical and "pip-audit" in canonical
    assert "semgrep" not in ordinary and "zizmor" not in ordinary and "pip-audit" not in ordinary


def test_artifact_validator_rejects_missing_duplicate_or_unknown_required_result() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "windows/3.10")
    dev_gate.validate_run_artifact(artifact, manifest)

    missing = copy.deepcopy(artifact)
    missing["checks"].pop()
    with pytest.raises(dev_gate.GateBlock, match="cardinality"):
        dev_gate.validate_run_artifact(missing, manifest)

    duplicate = copy.deepcopy(artifact)
    duplicate["checks"].append(copy.deepcopy(duplicate["checks"][0]))
    with pytest.raises(dev_gate.GateBlock, match="cardinality"):
        dev_gate.validate_run_artifact(duplicate, manifest)

    unknown = copy.deepcopy(artifact)
    unknown["checks"][0]["status"] = "skipped"
    with pytest.raises(dev_gate.GateBlock, match="status"):
        dev_gate.validate_run_artifact(unknown, manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["artifacts"].pop("wheel"),
        lambda artifact: artifact["interpreters"].clear(),
        lambda artifact: artifact["runner"].update({"os": "linux"}),
        lambda artifact: artifact["checks"][0].update({"exit_code": 99}),
        lambda artifact: next(
            check for check in artifact["checks"] if check["id"].startswith("pytest-")
        ).update({"import_provenance": None}),
    ],
)
def test_passing_leg_requires_every_supporting_proof(mutate) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "windows/3.10")
    mutate(artifact)

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_canonical_leg_requires_live_security_input_evidence() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.12")
    artifact["external_inputs"] = []

    with pytest.raises(dev_gate.GateBlock, match="external"):
        dev_gate.validate_run_artifact(artifact, manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["subject"].update({"candidate_sha": 7}),
        lambda artifact: artifact["subject"].update({"clean_before": 1}),
        lambda artifact: artifact["manifest"].update({"sha256": 7}),
        lambda artifact: artifact["checks"][0].update({"id": 7}),
        lambda artifact: artifact.update({"verdict": []}),
        lambda artifact: artifact.update({"schema_version": True}),
        lambda artifact: artifact["manifest"].update({"schema_version": True}),
        lambda artifact: artifact["interpreters"][0].update({"status": []}),
        lambda artifact: artifact["checks"][0].update({"status": []}),
        lambda artifact: artifact["checks"][0].update({"exit_code": False}),
    ],
)
def test_malformed_evidence_types_fail_with_gate_block(mutate) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "windows/3.10")
    mutate(artifact)

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_passing_check_command_must_match_committed_plan() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "windows/3.10")
    command = str((Path.cwd() / ("cmd.exe" if os.name == "nt" else "true")).resolve())
    artifact["checks"][0]["argv"] = [command]
    artifact["checks"][0]["tool"]["path"] = command

    with pytest.raises(dev_gate.GateBlock, match="command"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_import_provenance_rejects_parent_traversal() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "windows/3.10")
    pytest_record = next(check for check in artifact["checks"] if check["id"].startswith("pytest-"))
    pytest_record["import_provenance"] = {
        "expected_root": "/trusted/export",
        "observed_path": "/trusted/export/../../stale-editable/agenttalk/__init__.py",
        "version": artifact["subject"]["version"],
    }

    with pytest.raises(dev_gate.GateBlock, match="provenance"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_canonical_external_input_types_cannot_crash_validation() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.12")
    artifact["external_inputs"][0]["check_id"] = []

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_aggregate_requires_exact_unique_matrix_and_common_binding() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest, "release")]

    aggregate = dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, _binding(manifest))
    assert aggregate["complete"] is True
    assert aggregate["verdict"] == "pass"
    assert len(aggregate["legs"]) == 12

    with pytest.raises(dev_gate.GateBlock, match="missing"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts[:-1], _binding(manifest))

    with pytest.raises(dev_gate.GateBlock, match="duplicate"):
        dev_gate.aggregate_leg_artifacts(
            manifest, "release", [*artifacts, artifacts[0]], _binding(manifest)
        )

    mismatched = copy.deepcopy(artifacts)
    mismatched[-1]["subject"]["candidate_sha"] = "9" * 40
    with pytest.raises(dev_gate.GateBlock, match="candidate_sha"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", mismatched, _binding(manifest))


def test_aggregate_is_complete_but_blocked_when_one_leg_failed() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest, "release")]
    artifacts[0] = _leg_artifact(manifest, artifacts[0]["ci_leg"], status="fail")

    aggregate = dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, _binding(manifest))

    assert aggregate["complete"] is True
    assert aggregate["verdict"] == "block"


def test_malformed_aggregate_header_blocks_without_type_error() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest)]
    aggregate = dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, _binding(manifest))
    aggregate["verdict"] = []

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_aggregate_artifact(aggregate, manifest)


def test_aggregate_rejects_leg_set_not_bound_to_current_checkout() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest)]
    current = _binding(manifest)
    current = dev_gate.CandidateBinding(
        **{**current.__dict__, "candidate_sha": "9" * 40}
    )

    with pytest.raises(dev_gate.GateBlock, match="current checkout"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, current)

    current = _binding(manifest)
    current = dev_gate.CandidateBinding(
        **{**current.__dict__, "runner_module_sha256": "9" * 64}
    )
    with pytest.raises(dev_gate.GateBlock, match="current checkout"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, current)


def test_committed_manifest_binding_ignores_checkout_newline_conversion_and_rejects_dirt(tmp_path: Path) -> None:
    (tmp_path / "dev-gate.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.78.1"\n', encoding="utf-8"
    )
    runner = tmp_path / "src" / "agenttalk" / "dev_gate.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# fixture runner\n", encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "dev-gate.json", "pyproject.toml", "src/agenttalk/dev_gate.py")
    _git(tmp_path, "commit", "-m", "base")

    binding = dev_gate.capture_candidate_binding(tmp_path)
    committed = subprocess.run(
        ["git", "show", "HEAD:dev-gate.json"], cwd=tmp_path, capture_output=True, check=True
    ).stdout
    assert binding.manifest_sha256 == dev_gate.sha256_bytes(committed)
    committed_runner = subprocess.run(
        ["git", "show", "HEAD:src/agenttalk/dev_gate.py"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    ).stdout
    assert binding.runner_module_sha256 == dev_gate.sha256_bytes(committed_runner)
    assert binding.runner_git_blob == _git(tmp_path, "rev-parse", "HEAD:src/agenttalk/dev_gate.py")
    assert binding.clean is True

    (tmp_path / "untracked.txt").write_text("dirty", encoding="utf-8")
    dirty = dev_gate.capture_candidate_binding(tmp_path)
    assert dirty.clean is False


def test_parse_cli_leg_rejects_unknown_os_or_unpinned_python() -> None:
    manifest = dev_gate.validate_manifest(_manifest())

    assert dev_gate.parse_ci_leg("Windows/3.10", manifest, "release") == "windows/3.10"
    with pytest.raises(dev_gate.GateBlock):
        dev_gate.parse_ci_leg("freebsd/3.10", manifest, "release")
    with pytest.raises(dev_gate.GateBlock):
        dev_gate.parse_ci_leg("linux/3.14", manifest, "release")


def test_base_environment_scrubs_import_and_environment_contamination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "stale-editable")
    monkeypatch.setenv("PYTHONHOME", "stale-home")
    monkeypatch.setenv("VIRTUAL_ENV", "stale-venv")
    monkeypatch.setenv("PIP_CONFIG_FILE", "attacker-pip.ini")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://attacker.invalid/simple")

    env = dev_gate._base_env(tmp_path)

    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["PIP_NO_CACHE_DIR"] == "1"
    assert "PIP_EXTRA_INDEX_URL" not in env
    assert {env[name] for name in ("TMP", "TEMP", "TMPDIR")} == {str(tmp_path)}


@pytest.mark.parametrize(
    "poison",
    ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONOPTIMIZE", "GIT_DIR", "GIT_WORK_TREE"],
)
def test_base_environment_drops_gate_control_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poison: str
) -> None:
    monkeypatch.setenv(poison, "attacker-controlled")

    assert poison not in dev_gate._base_env(tmp_path)


def test_isolated_tool_launcher_cannot_be_shadowed_by_candidate_module(tmp_path: Path) -> None:
    sentinel = tmp_path / "shadow-ran.txt"
    (tmp_path / "pytest.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('shadow')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        dev_gate.isolated_tool_argv(
            sys.executable,
            "pytest",
            "--version",
            candidate_import_root=tmp_path,
        ),
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not sentinel.exists()


def test_isolated_source_launcher_prefers_committed_export_over_candidate_cwd(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    committed_src = tmp_path / "committed" / "src"
    package = committed_src / "agenttalk"
    candidate.mkdir()
    package.mkdir(parents=True)
    committed_sentinel = tmp_path / "committed-ran.txt"
    shadow_sentinel = tmp_path / "shadow-ran.txt"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        f"from pathlib import Path\nPath({str(committed_sentinel)!r}).write_text('committed')\n",
        encoding="utf-8",
    )
    (candidate / "agenttalk.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_sentinel)!r}).write_text('shadow')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            dev_gate._ISOLATED_SOURCE_LAUNCHER,
            str(committed_src),
        ],
        cwd=candidate,
        env={**os.environ, "PYTHONPATH": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert committed_sentinel.read_text(encoding="utf-8") == "committed"
    assert not shadow_sentinel.exists()


def test_wheel_install_resolves_declared_dependencies_in_isolated_venv(tmp_path: Path) -> None:
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    creator = dev_gate.InterpreterInfo(
        requested=minor,
        path=Path(sys.executable).resolve(),
        implementation="CPython",
        version=platform.python_version(),
    )
    interpreter, proof = dev_gate._create_isolated_venv(
        creator=creator,
        root=tmp_path / "runtime",
        role="runtime",
        logs_dir=tmp_path / "logs",
    )
    wheel = _synthetic_wheel(
        tmp_path / "agenttalk-0.78.1-py3-none-any.whl",
        package_code="__version__ = '0.78.1'\n",
        requirements=("definitely-missing-agenttalk-dependency==999999",),
    )
    manifest = _manifest()
    empty_index = tmp_path / "empty-index"
    empty_index.mkdir()
    manifest["checks"]["wheel-contract"]["dependency_index"] = empty_index.as_uri()

    record = dev_gate._install_wheel(
        interpreter=interpreter,
        runtime_environment=proof,
        wheel=wheel,
        source_root=tmp_path,
        env=dev_gate._base_env(tmp_path),
        manifest=manifest,
        logs_dir=tmp_path / "logs",
    )

    assert record["status"] == "fail"
    assert "--no-deps" not in record["argv"]
    assert record["runtime_environment"]["system_site_packages"] is False
    assert record["runtime_environment"]["creator_path"] == str(Path(sys.executable).resolve())
    assert dev_gate._is_within(interpreter.path, Path(proof["prefix"]))


def test_wheel_venv_forces_copied_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    creator = dev_gate.InterpreterInfo(
        requested=minor,
        path=Path(sys.executable).resolve(),
        implementation="CPython",
        version=platform.python_version(),
    )
    observed: list[str] = []

    def fail_create(**kwargs):
        observed.extend(kwargs["argv"])
        return dev_gate.CommandOutcome(
            argv=tuple(kwargs["argv"]),
            returncode=1,
            duration_ms=1,
            status="fail",
            reason_code="nonzero_exit",
            diagnostic="fixture stop",
            log_path=tmp_path / "venv-create.log",
        )

    monkeypatch.setattr(dev_gate, "run_command", fail_create)

    with pytest.raises(dev_gate.GateBlock, match="wheel_environment_create_failed"):
        dev_gate._create_isolated_venv(
            creator=creator,
            root=tmp_path / "runtime",
            role="runtime",
            logs_dir=tmp_path / "logs",
        )

    assert "--copies" in observed


def test_wheel_import_cannot_see_bootstrap_site_packages(tmp_path: Path) -> None:
    pytest.importorskip("pytest")
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    creator = dev_gate.InterpreterInfo(
        requested=minor,
        path=Path(sys.executable).resolve(),
        implementation="CPython",
        version=platform.python_version(),
    )
    interpreter, proof = dev_gate._create_isolated_venv(
        creator=creator,
        root=tmp_path / "runtime",
        role="runtime",
        logs_dir=tmp_path / "logs",
    )
    wheel = _synthetic_wheel(
        tmp_path / "agenttalk-0.78.1-py3-none-any.whl",
        package_code="import pytest\n__version__ = '0.78.1'\n",
    )
    manifest = _manifest()
    empty_index = tmp_path / "empty-index"
    empty_index.mkdir()
    manifest["checks"]["wheel-contract"]["dependency_index"] = empty_index.as_uri()
    install = dev_gate._install_wheel(
        interpreter=interpreter,
        runtime_environment=proof,
        wheel=wheel,
        source_root=tmp_path,
        env=dev_gate._base_env(tmp_path),
        manifest=manifest,
        logs_dir=tmp_path / "logs",
    )
    assert install["status"] == "pass"

    with pytest.raises(dev_gate.GateBlock, match="import_probe_failed"):
        dev_gate.import_probe(
            interpreter=interpreter,
            expected_root=Path(proof["prefix"]),
            cwd=tmp_path,
            env=dev_gate._base_env(tmp_path),
            temp_root=tmp_path,
            logs_dir=tmp_path / "logs",
            expected_version="0.78.1",
            label="isolated-runtime",
            candidate_import_root=None,
        )


def test_pip_audit_consumes_resolved_wheel_snapshot_without_bootstrap_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirements = tmp_path / "resolved.txt"
    requirements.write_text("example-dependency==1.2.3\n", encoding="utf-8")
    interpreter = dev_gate.InterpreterInfo(
        requested=f"{sys.version_info.major}.{sys.version_info.minor}",
        path=Path(sys.executable).resolve(),
        implementation="CPython",
        version=platform.python_version(),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(dev_gate, "_probe_python_module", lambda *_args, **_kwargs: "pip-audit 2")

    def run(**kwargs):
        calls.append(list(kwargs["argv"]))
        log_path = kwargs["logs_dir"] / f"{kwargs['check_id']}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("No known vulnerabilities found\n", encoding="utf-8")
        return dev_gate.CommandOutcome(
            argv=tuple(kwargs["argv"]),
            returncode=0,
            duration_ms=1,
            status="pass",
            reason_code=None,
            diagnostic="",
            log_path=log_path,
        )

    monkeypatch.setattr(dev_gate, "run_command", run)
    record, artifact = dev_gate._pip_audit_check(
        interpreter=interpreter,
        source_root=tmp_path,
        env=dev_gate._base_env(tmp_path),
        manifest=_manifest(),
        requirements=requirements,
        logs_dir=tmp_path / "logs",
    )

    assert record["status"] == "pass"
    assert artifact is not None and artifact["sha256"] == dev_gate._sha256_file(requirements)
    assert len(calls) == 1
    assert "freeze" not in calls[0]
    assert "--no-deps" in calls[0]
    assert "--disable-pip" in calls[0]
    assert calls[0][-2:] == ["--requirement", str(requirements)]


def test_runtime_dependency_snapshot_excludes_candidate_but_keeps_resolved_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = dev_gate.InterpreterInfo(
        requested=f"{sys.version_info.major}.{sys.version_info.minor}",
        path=Path(sys.executable).resolve(),
        implementation="CPython",
        version=platform.python_version(),
    )

    def run(**kwargs):
        log_path = kwargs["logs_dir"] / "freeze.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "agenttalk @ file:///candidate/agenttalk.whl\nresolved-dependency==4.5.6\n",
            encoding="utf-8",
        )
        return dev_gate.CommandOutcome(
            argv=tuple(kwargs["argv"]),
            returncode=0,
            duration_ms=1,
            status="pass",
            reason_code=None,
            diagnostic="",
            log_path=log_path,
        )

    monkeypatch.setattr(dev_gate, "run_command", run)
    output = dev_gate._runtime_dependency_snapshot(
        interpreter=interpreter,
        source_root=tmp_path,
        env=dev_gate._base_env(tmp_path),
        output=tmp_path / "audit-requirements.txt",
        timeout_seconds=300,
        logs_dir=tmp_path / "logs",
    )

    assert output.read_text(encoding="utf-8") == "resolved-dependency==4.5.6\n"


def test_safe_candidate_export_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "no")

    with pytest.raises(dev_gate.GateBlock, match="unsafe archive member"):
        dev_gate._safe_extract_zip(archive, tmp_path / "out")

    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("member", [r"..\escape.txt", "C:/escape.txt"])
def test_safe_candidate_export_rejects_windows_escape_forms(tmp_path: Path, member: str) -> None:
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(member, "no")

    with pytest.raises(dev_gate.GateBlock, match="unsafe archive member"):
        dev_gate._safe_extract_zip(archive, tmp_path / "out")


def test_each_gate_phase_uses_a_distinct_committed_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(dev_gate.validate_manifest(_manifest()))
    calls: list[tuple[Path, Path]] = []

    def record_export(_binding, destination: Path, archive: Path) -> None:
        assert _binding is binding
        calls.append((destination, archive))

    monkeypatch.setattr(dev_gate, "export_candidate", record_export)

    roots = [
        dev_gate._export_phase(binding, tmp_path, phase)
        for phase in ("source", "package", "wheel", "static")
    ]

    assert len(set(roots)) == 4
    assert len({archive for _, archive in calls}) == 4
    assert [destination for destination, _ in calls] == roots


def test_evidence_validator_rejects_missing_nested_fields() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.10")

    del artifact["runner"]["module_sha256"]

    with pytest.raises(dev_gate.GateBlock, match="runner"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_evidence_validator_rejects_wheel_check_without_isolated_runtime() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.10")
    install = next(check for check in artifact["checks"] if check["id"] == "wheel-install-py310")
    install["runtime_environment"] = None

    with pytest.raises(dev_gate.GateBlock, match="runtime_environment"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_evidence_validator_rejects_reused_runtime_venv_for_wheel_tests() -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.10")
    runtime = next(
        check["runtime_environment"]
        for check in artifact["checks"]
        if check["id"] == "wheel-install-py310"
    )
    wheel_test = next(check for check in artifact["checks"] if check["id"] == "pytest-wheel-py310")
    wheel_test["runtime_environment"] = {**runtime, "role": "test"}
    wheel_test["tool"]["path"] = runtime["python_path"]
    wheel_test["argv"][0] = runtime["python_path"]
    wheel_test["import_provenance"] = {
        **wheel_test["import_provenance"],
        "expected_root": runtime["prefix"],
        "observed_path": str(Path(runtime["prefix"]) / "site-packages" / "agenttalk" / "__init__.py"),
    }

    with pytest.raises(dev_gate.GateBlock, match="reused the runtime contract venv"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_write_run_evidence_is_normalized_and_roundtrip_validated(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "macos/3.13")
    evidence = tmp_path / "evidence.json"

    digest = dev_gate.write_run_evidence(evidence, artifact, manifest)

    raw = evidence.read_bytes()
    assert raw.endswith(b"\n")
    assert digest == dev_gate.sha256_bytes(raw)
    assert json.loads(raw) == artifact
    assert os.path.isabs(artifact["checks"][0]["log"]["path"])


def test_aggregate_evidence_roundtrips_with_exact_leg_input_digests(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    binding = _binding(manifest)
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest)]
    input_digests = {
        artifact["ci_leg"]: format(index + 1, "x") * 64
        for index, artifact in enumerate(artifacts)
    }
    aggregate = dev_gate.aggregate_leg_artifacts(
        manifest,
        "release",
        artifacts,
        binding,
        input_sha256_by_leg=input_digests,
    )
    evidence = tmp_path / "aggregate.json"

    digest = dev_gate.write_aggregate_evidence(
        evidence,
        aggregate,
        manifest,
        current_binding=binding,
    )

    assert digest == dev_gate.sha256_bytes(evidence.read_bytes())
    assert [leg["artifact_sha256"] for leg in aggregate["legs"]] == list(input_digests.values())


def test_cli_exposes_no_skip_dev_gate_surface() -> None:
    parser = build_parser()

    local = parser.parse_args(
        [
            "dev-gate",
            "--profile",
            "release",
            "--python",
            "3.10=/opt/cpython310/python",
            "--evidence",
            "/opt/evidence.json",
        ]
    )
    assert local.cmd == "dev-gate"
    assert local.ci_leg is None and local.aggregate is None

    ci = parser.parse_args(["dev-gate", "--ci-leg", "linux/3.12"])
    assert ci.ci_leg == "linux/3.12"

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["dev-gate", "--ci-leg", "linux/3.12", "--aggregate", "/opt/legs"]
        )


def test_cli_early_block_emits_normalized_machine_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "preflight.json"
    args = build_parser().parse_args(
        ["dev-gate", "--ci-leg", "linux/3.10", "--evidence", str(evidence)]
    )

    monkeypatch.setattr(dev_gate, "reenter_candidate_source", lambda _root, _argv: None)

    def block(**_kwargs):
        raise dev_gate.GateBlock("ci_leg_platform_mismatch", "expected Linux")

    monkeypatch.setattr(dev_gate, "execute_gate", block)

    assert cmd_dev_gate(args) == 2
    emitted = json.loads(evidence.read_text(encoding="utf-8"))
    dev_gate.validate_preflight_artifact(emitted)
    assert emitted["verdict"] == "block"
    assert emitted["complete"] is False
    assert emitted["blocker"]["code"] == "ci_leg_platform_mismatch"
    summary = json.loads(capsys.readouterr().out)
    assert summary["evidence"] == str(evidence.resolve())
    assert summary["candidate_sha"] == emitted["subject"]["candidate_sha"]


def test_cli_unexpected_io_failure_emits_normalized_machine_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "preflight-io.json"
    args = build_parser().parse_args(["dev-gate", "--evidence", str(evidence)])
    monkeypatch.setattr(dev_gate, "reenter_candidate_source", lambda _root, _argv: None)

    def fail(**_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(dev_gate, "execute_gate", fail)

    assert cmd_dev_gate(args) == 2
    emitted = json.loads(evidence.read_text(encoding="utf-8"))
    dev_gate.validate_preflight_artifact(emitted)
    assert emitted["blocker"]["code"] == "gate_internal_error"


def test_reentry_executes_committed_export_despite_hidden_worktree_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    runner = repo / "src" / "agenttalk" / "dev_gate.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("COMMITTED = True\n", encoding="utf-8")
    (repo / "dev-gate.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "update-index", "--assume-unchanged", "src/agenttalk/dev_gate.py")
    runner.write_text("MUTATED = True\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain=v1") == ""
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(dev_gate, "_default_external_base", lambda _root, _store: external)
    real_run = subprocess.run
    observed: dict[str, str] = {}

    def run(argv, **kwargs):
        if argv[0] == sys.executable:
            committed_runner = Path(kwargs["env"]["PYTHONPATH"]) / "agenttalk" / "dev_gate.py"
            observed["runner"] = committed_runner.read_text(encoding="utf-8")
            assert argv[1:4] == ["-I", "-c", dev_gate._ISOLATED_SOURCE_LAUNCHER]
            assert Path(argv[4]) == committed_runner.parent.parent
            return SimpleNamespace(returncode=7)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(dev_gate.subprocess, "run", run)

    assert dev_gate.reenter_candidate_source(repo, ["--profile", "release"]) == 7
    assert observed["runner"] == "COMMITTED = True\n"


def test_git_binding_ignores_environment_redirection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dev-gate.json").write_text("{}\n", encoding="utf-8")
    runner = tmp_path / "src" / "agenttalk" / "dev_gate.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# fixture runner\n", encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "dev-gate.json", "src/agenttalk/dev_gate.py")
    _git(tmp_path, "commit", "-m", "base")
    expected_sha = _git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker-controlled-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path.parent))

    binding = dev_gate.capture_candidate_binding(tmp_path)

    assert binding.clean is True
    assert binding.candidate_sha == expected_sha


def test_executable_resolution_ignores_candidate_relative_path_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = dev_gate._git_executable()
    fake_name = "git.exe" if os.name == "nt" else "git"
    fake = tmp_path / fake_name
    fake.write_text("not the real git\n", encoding="utf-8")
    if os.name != "nt":
        fake.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "." + os.pathsep + str(real_git.parent))

    assert dev_gate._git_executable() == real_git


def test_git_and_gitleaks_child_path_exclude_candidate_absolute_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = dev_gate._git_executable()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    fake_name = "git.exe" if os.name == "nt" else "git"
    fake = candidate / fake_name
    fake.write_text("not the real git\n", encoding="utf-8")
    if os.name != "nt":
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(candidate) + os.pathsep + str(real_git.parent))

    assert dev_gate._git_executable(candidate) == real_git
    child_env = dev_gate._gitleaks_environment(candidate)
    assert child_env["PATH"] == str(real_git.parent)
    assert str(candidate) not in child_env["PATH"]


def test_external_gate_paths_cannot_enter_candidate_or_store(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    store = tmp_path / "store"
    candidate.mkdir()
    store.mkdir()

    with pytest.raises(dev_gate.GateBlock, match="outside the candidate"):
        dev_gate._ensure_external(candidate / "evidence.json", candidate, store, "evidence")
    with pytest.raises(dev_gate.GateBlock, match="outside AGENTTALK_ROOT"):
        dev_gate._ensure_external(store / "temp", candidate, store, "temp")
