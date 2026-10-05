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

from agenttalk import dev_gate, janitor
from agenttalk.cli import build_parser, cmd_dev_gate, main as cli_main
import test_janitor as _janitor_tests


def _manifest() -> dict:
    return json.loads(Path("dev-gate.json").read_text(encoding="utf-8"))


def _check(check_id: str, manifest: dict, minor: str, *, status: str = "pass", os_id: str | None = None) -> dict:
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
        posix_args = spec.get("posix_parallel_args", []) if os_id in ("linux", "macos") else []
        argv = dev_gate.isolated_tool_argv(
            tool_path,
            "pytest",
            *spec["args"],
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str((Path.cwd() / "pytest-temp").resolve()),
            *posix_args,
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
        argv.extend(["--error", "--timeout", str(spec["rule_timeout_seconds"])])
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
                os_id=leg.split("/", 1)[0],
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


def _binding(manifest: dict, *, root: Path | None = None) -> dev_gate.CandidateBinding:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return dev_gate.CandidateBinding(
        root=root or Path.cwd(),
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


def _gate_repo(tmp_path: Path) -> Path:
    root = tmp_path / "gate-repo"
    runner = root / "src" / "agenttalk" / "dev_gate.py"
    runner.parent.mkdir(parents=True)
    (root / "dev-gate.json").write_text(
        json.dumps(_manifest(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.78.1"\n',
        encoding="utf-8",
    )
    runner.write_text("# fixture runner\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "dev-gate.json", "pyproject.toml", "src/agenttalk/dev_gate.py")
    _git(root, "commit", "-m", "base")
    return root


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
        lambda data: data["checks"]["pytest"].pop("posix_parallel_args"),
        lambda data: data["checks"]["pytest"].update({"posix_parallel_args": ["-q"]}),
        lambda data: data["checks"]["pytest"].pop("xdist_requirement"),
        lambda data: data["checks"]["pytest"].update({"xdist_requirement": "pytest-xdist"}),
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


def test_committed_pytest_timeout_has_wheel_leg_headroom() -> None:
    # Regression guard (#54): the single `pytest` timeout is shared by both the
    # fast source legs (~9min) and the slow wheel legs (isolated venv + install +
    # full suite, ~30min on loaded Windows runners). At 1800s the wheel leg hit the
    # cap at ~92-99% and forced multi-cycle CI rerun loops. Keep generous headroom
    # so it cannot silently regress; the CI job allows timeout-minutes: 90.
    manifest = _manifest()
    assert manifest["checks"]["pytest"]["timeout_seconds"] >= 2400


def test_committed_pytest_timeout_matches_the_windows_capacity_stopgap() -> None:
    """Windows CI capacity stopgap: pin the exact current cap (raised 5400 -> 7200,
    following the #197 precedent 87c529a) so a silent drift is a test failure, not a
    rediscovery under load - PR #230's dev-gate windows/3.11 run had its
    source pytest killed at the old 5400s cap at 93% complete."""
    manifest = _manifest()
    assert manifest["checks"]["pytest"]["timeout_seconds"] == 7200


@pytest.mark.parametrize("leg", ["linux/3.10", "macos/3.10"])
def test_posix_leg_pytest_evidence_requires_the_parallel_args(leg: str) -> None:
    """ci-xdist-posix: a passing pytest check's argv on a Linux/macOS leg
    must genuinely carry the committed posix_parallel_args - dropping them
    (as if the leg silently regressed to serial) must be rejected, not
    silently accepted."""
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, leg)
    spec = manifest["checks"]["pytest"]
    posix_args = spec["posix_parallel_args"]
    # Strip EVERY pytest check's posix_parallel_args block (both source and
    # wheel modes are required per leg) by POSITION - it sits directly
    # before the trailing `paths`, and `list.remove()` by value is unsafe
    # here since "-p" also appears earlier for "no:cacheprovider". Touching
    # only one of the two would leave the other still correctly carrying
    # the flags, which is not what this test is isolating.
    for pytest_check in (c for c in artifact["checks"] if c["id"].startswith("pytest-")):
        paths_start = len(pytest_check["argv"]) - len(spec["paths"])
        posix_start = paths_start - len(posix_args)
        assert pytest_check["argv"][posix_start:paths_start] == posix_args
        del pytest_check["argv"][posix_start:paths_start]

    with pytest.raises(dev_gate.GateBlock, match="command"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_windows_leg_pytest_evidence_rejects_the_posix_parallel_args() -> None:
    """The converse: Windows stays exactly serial as an evidence-checked
    fact, not just a manifest declaration - a Windows check record that
    somehow carries the posix xdist flags must be rejected too."""
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "windows/3.10")
    pytest_check = next(c for c in artifact["checks"] if c["id"].startswith("pytest-"))
    spec = manifest["checks"]["pytest"]
    paths_start = len(pytest_check["argv"]) - len(spec["paths"])
    pytest_check["argv"][paths_start:paths_start] = spec["posix_parallel_args"]

    with pytest.raises(dev_gate.GateBlock, match="command"):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_run_pytest_mode_adds_posix_parallel_args_only_when_the_env_var_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ci-xdist-posix: --n/--dist are added to the real argv only when
    AGENTTALK_DEV_GATE_POSIX_PARALLEL is set (as tests.yml does for
    Linux/macOS legs only) - and are entirely absent, with no env var read
    at all producing a flag, when unset (every Windows leg, every local
    run)."""
    interpreter = dev_gate.InterpreterInfo(
        requested=f"{sys.version_info.major}.{sys.version_info.minor}",
        path=Path(sys.executable).resolve(),
        implementation="CPython",
        version=platform.python_version(),
    )
    monkeypatch.setattr(dev_gate, "_probe_python_module", lambda *_a, **_k: "pytest 8.0.0")
    monkeypatch.setattr(dev_gate, "import_probe", lambda *_a, **_k: None)
    calls: list[list[str]] = []

    def run(**kwargs):
        calls.append(list(kwargs["argv"]))
        log_path = kwargs["logs_dir"] / f"{kwargs['check_id']}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("collected\n", encoding="utf-8")
        return dev_gate.CommandOutcome(
            argv=tuple(kwargs["argv"]), returncode=0, duration_ms=1, status="pass",
            reason_code=None, diagnostic="", log_path=log_path,
        )

    monkeypatch.setattr(dev_gate, "run_command", run)
    manifest = _manifest()

    monkeypatch.delenv("AGENTTALK_DEV_GATE_POSIX_PARALLEL", raising=False)
    dev_gate._run_pytest_mode(
        mode="source", interpreter=interpreter, source_root=tmp_path,
        import_root=tmp_path / "src", env=dev_gate._base_env(tmp_path),
        expected_version="0.78.1", manifest=manifest,
        basetemp=tmp_path / "pytest-temp", logs_dir=tmp_path / "logs-unset",
    )
    assert "-n" not in calls[-1]
    assert "--dist" not in calls[-1]

    monkeypatch.setenv("AGENTTALK_DEV_GATE_POSIX_PARALLEL", "1")
    dev_gate._run_pytest_mode(
        mode="source", interpreter=interpreter, source_root=tmp_path,
        import_root=tmp_path / "src", env=dev_gate._base_env(tmp_path),
        expected_version="0.78.1", manifest=manifest,
        basetemp=tmp_path / "pytest-temp", logs_dir=tmp_path / "logs-set",
    )
    argv = calls[-1]
    assert argv.count("-p") == 2  # "no:cacheprovider" plus "xdist.plugin"
    assert "xdist.plugin" in argv
    assert argv[argv.index("-n") + 1] == "2"
    assert argv[argv.index("--dist") + 1] == "loadgroup"


def test_xdist_grouping_is_active_under_the_gates_real_pytest_invocation() -> None:
    """#250 fix round 1 (codex cold read dev-5, reproduced directly): the
    dev gate disables plugin autoload and loads xdist explicitly via
    `-p xdist.plugin` - that registers under the name "xdist.plugin", not
    the autoload entry-point name "xdist" that tests/conftest.py's old
    `config.pluginmanager.hasplugin("xdist")` check alone caught. Under the
    gate's real invocation shape, registration was {xdist: False,
    xdist.plugin: True} - no xdist_group markers were ever added, so both
    the shared-resource groups and the per-file default grouping were
    INACTIVE (the same class of silent inertness the #232 probe's round 6
    found for the marker/nodeid hook-ordering bug).

    This runs a REAL pytest subprocess with the gate's actual invocation
    shape (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, an explicit
    `-p xdist.plugin -n 2 --dist loadgroup`) against two tests from the
    SAME file and confirms grouping is genuinely active: both must land on
    the SAME worker, with the per-file group suffix visible in their
    reported node ids. Must fail on 7a9b1bc (pre-fix): registration under
    that exact shape never matched "xdist", so this file's tests would
    schedule individually, with no @-suffix, and were free to land on
    either worker."""
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(Path.cwd() / "src")}
    node_ids = (
        "tests/test_dev_gate.py::test_manifest_declares_real_ci_matrix_separately_from_local_interpreters",
        "tests/test_dev_gate.py::test_committed_pytest_timeout_has_wheel_leg_headroom",
    )
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "-p", "xdist.plugin", "-n", "2", "--dist", "loadgroup", "-v",
            *node_ids,
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    report_lines = [
        line for line in completed.stdout.splitlines()
        if line.startswith("[gw") and any(node_id.rsplit("::", 1)[1] in line for node_id in node_ids)
    ]
    assert len(report_lines) == len(node_ids), completed.stdout

    workers = set()
    for line in report_lines:
        worker = line.split("]", 1)[0].removeprefix("[")
        workers.add(worker)
        assert "@" in line and "test_dev_gate.py" in line.split("@", 1)[1], (
            f"missing the per-file xdist_group suffix in the reported test id: {line!r}"
        )
    assert len(workers) == 1, f"the two tests landed on DIFFERENT workers: {report_lines}"


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


def test_aggregate_requires_exact_unique_matrix_and_common_binding(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest, "release")]
    binding = _binding(manifest, root=_gate_repo(tmp_path))

    aggregate = dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, binding)
    assert aggregate["complete"] is True
    assert aggregate["verdict"] == "pass"
    assert len(aggregate["legs"]) == 12

    with pytest.raises(dev_gate.GateBlock, match="missing"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts[:-1], binding)

    with pytest.raises(dev_gate.GateBlock, match="duplicate"):
        dev_gate.aggregate_leg_artifacts(
            manifest, "release", [*artifacts, artifacts[0]], binding
        )

    mismatched = copy.deepcopy(artifacts)
    mismatched[-1]["subject"]["candidate_sha"] = "9" * 40
    with pytest.raises(dev_gate.GateBlock, match="candidate_sha"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", mismatched, binding)


def test_aggregate_is_complete_but_blocked_when_one_leg_failed(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest, "release")]
    artifacts[0] = _leg_artifact(manifest, artifacts[0]["ci_leg"], status="fail")

    aggregate = dev_gate.aggregate_leg_artifacts(
        manifest,
        "release",
        artifacts,
        _binding(manifest, root=_gate_repo(tmp_path)),
    )

    assert aggregate["complete"] is True
    assert aggregate["verdict"] == "block"


def test_malformed_aggregate_header_blocks_without_type_error(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest)]
    aggregate = dev_gate.aggregate_leg_artifacts(
        manifest,
        "release",
        artifacts,
        _binding(manifest, root=_gate_repo(tmp_path)),
    )
    aggregate["verdict"] = []

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_aggregate_artifact(aggregate, manifest)


def test_aggregate_rejects_leg_set_not_bound_to_current_checkout(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    artifacts = [_leg_artifact(manifest, leg) for leg in dev_gate.expected_ci_legs(manifest)]
    root = _gate_repo(tmp_path)
    current = _binding(manifest, root=root)
    current = dev_gate.CandidateBinding(
        **{**current.__dict__, "candidate_sha": "9" * 40}
    )

    with pytest.raises(dev_gate.GateBlock, match="current checkout"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, current)

    current = _binding(manifest, root=root)
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


@pytest.mark.parametrize("value, forwarded", [
    ("1", True), ("true", False), ("0", False), ("", False), (None, False),
])
def test_base_environment_passes_the_gateway_port_opt_in_only_as_exactly_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None, forwarded: bool
) -> None:
    """#318: CI's opt-in for the gateway-port tests reaches the gate's pytest runs,
    which otherwise see only the allowlisted environment."""
    if value is None:
        monkeypatch.delenv(dev_gate.GATEWAY_PORT_TESTS_VAR, raising=False)
    else:
        monkeypatch.setenv(dev_gate.GATEWAY_PORT_TESTS_VAR, value)

    env = dev_gate._base_env(tmp_path)

    assert (dev_gate.GATEWAY_PORT_TESTS_VAR in env) is forwarded
    if forwarded:
        assert env[dev_gate.GATEWAY_PORT_TESTS_VAR] == "1"


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
    _write_check_logs(artifact, tmp_path / "runner")
    _write_artifact_files(artifact, tmp_path / "runner")

    digest = dev_gate.write_run_evidence(evidence, artifact, manifest)

    raw = evidence.read_bytes()
    assert raw.endswith(b"\n")
    assert digest == dev_gate.sha256_bytes(raw)
    assert json.loads(raw) == artifact
    assert os.path.isabs(artifact["checks"][0]["log"]["path"])
    run_id = artifact["run_id"]
    for check in artifact["checks"]:
        log = check["log"]
        assert log["artifact_path"] == f"{run_id}/logs/{check['id']}.log"
        collected = evidence.parent / log["artifact_path"]
        assert collected.read_bytes() == Path(log["path"]).read_bytes()
        assert dev_gate.sha256_bytes(collected.read_bytes()) == log["sha256"]
    for artifact_id, item in artifact["artifacts"].items():
        assert item["artifact_path"] == f"{run_id}/artifacts/{artifact_id}/{item['filename']}"
        collected = evidence.parent / item["artifact_path"]
        assert dev_gate._sha256_file(collected) == item["sha256"]


def test_second_run_preserves_first_runs_copies(tmp_path: Path) -> None:
    """#338 v2 (P2, data loss - codex-agenttalk-developer-5's cold read on
    PR #339): two evidence files commonly share a parent directory, including
    the default temp location. The previous fixed `artifacts/<kind>/<name>`
    layout let a second run's copies silently overwrite a first run's,
    even though both individually wrote valid, hash-verified records.
    Durable copies now live under a namespace unique to their own run_id,
    so a second run with the SAME package filenames but DIFFERENT bytes
    cannot touch the first run's copies."""
    manifest = dev_gate.validate_manifest(_manifest())
    bundle = tmp_path / "bundle"  # the shared parent both evidence files sit in

    def _record(run_id: str, payload: bytes) -> dict:
        artifact = _leg_artifact(manifest, "linux/3.12")
        artifact["run_id"] = run_id
        run_dir = tmp_path / run_id
        _write_check_logs(artifact, run_dir)
        for item in artifact["artifacts"].values():
            old_path = item["path"]
            target = run_dir / item["filename"]
            target.write_bytes(payload)
            item.update(path=str(target.resolve()), size_bytes=len(payload),
                        sha256=dev_gate.sha256_bytes(payload))
            for check in artifact["checks"]:
                check["argv"] = [str(target.resolve()) if arg == old_path else arg for arg in check["argv"]]
        return artifact

    first = _record("run-first", b"first-run-bytes")
    evidence1 = bundle / "first.json"
    dev_gate.write_run_evidence(evidence1, first, manifest)
    saved_first = json.loads(evidence1.read_text(encoding="utf-8"))

    second = _record("run-second", b"second-run-bytes")
    evidence2 = bundle / "second.json"
    dev_gate.write_run_evidence(evidence2, second, manifest)

    for item in saved_first["artifacts"].values():
        collected = bundle / item["artifact_path"]
        assert collected.read_bytes() == b"first-run-bytes", item
        assert dev_gate._sha256_file(collected) == item["sha256"], item
    dev_gate.validate_run_artifact(saved_first, manifest, bundle_root=bundle)


def test_evidence_run_namespace_collision_is_refused(tmp_path: Path) -> None:
    """A second write_run_evidence call reusing an already-used run_id is
    refused outright - 'created exclusively' means refusing a collision, not
    silently reusing or overwriting it. #344 fix round 1 (P2, connector
    4180279368): the already-PUBLISHED namespace from the first, successful
    call must survive the second call's refusal untouched - it is
    provably NOT an unpublished leftover, since a valid record already
    references it - and the refusal itself must still name it."""
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.12")
    artifact["run_id"] = "same-run-id"
    run_dir = tmp_path / "run"
    _write_check_logs(artifact, run_dir)
    _write_artifact_files(artifact, run_dir)
    evidence = tmp_path / "bundle" / "evidence.json"
    dev_gate.write_run_evidence(evidence, artifact, manifest)
    namespace = evidence.parent / "same-run-id"
    collected_before = sorted(p.relative_to(namespace) for p in namespace.rglob("*") if p.is_file())

    again = copy.deepcopy(artifact)
    with pytest.raises(dev_gate.GateBlock, match="evidence_run_namespace_collision") as excinfo:
        dev_gate.write_run_evidence(tmp_path / "bundle" / "second.json", again, manifest)

    assert excinfo.value.run_namespace == namespace
    assert namespace.is_dir()
    assert sorted(p.relative_to(namespace) for p in namespace.rglob("*") if p.is_file()) == collected_before
    dev_gate.validate_run_artifact(
        json.loads(evidence.read_text(encoding="utf-8")), manifest, bundle_root=evidence.parent
    )


@pytest.mark.parametrize("malicious_run_id", [
    "..", ".", "/etc/passwd", "a/b", "a\\b", "C:\\Windows", "C:/Windows",
], ids=["dotdot", "dot", "posix-absolute", "posix-separator", "windows-separator",
        "windows-absolute-backslash", "windows-absolute-forwardslash"])
def test_unsafe_run_id_is_refused_before_any_path_is_built(
    tmp_path: Path, malicious_run_id: str,
) -> None:
    """#344 fix round 2 (P2, finding G, connector 4180508917 - independently
    connector- and claude-agenttalk-reviewer-3-reported): run_id is used
    directly as a filesystem path component (`path.parent / run_id`)
    without ever checking its shape. An absolute run_id makes that join
    evaluate to the absolute path outright (pathlib's own semantics),
    writing outside the bundle entirely before any containment check ever
    runs; other shapes (a bare `..`, a path separator) either escape the
    same way or raise an undocumented raw OSError instead of a clean
    GateBlock. Required to be a single safe path component before it is
    ever used to build one."""
    manifest = dev_gate.validate_manifest(_manifest())
    artifact = _leg_artifact(manifest, "linux/3.12")
    artifact["run_id"] = malicious_run_id
    _write_check_logs(artifact, tmp_path / "run")
    _write_artifact_files(artifact, tmp_path / "run")
    evidence = tmp_path / "bundle" / "evidence.json"

    with pytest.raises(dev_gate.GateBlock, match="evidence_run_id_invalid"):
        dev_gate.write_run_evidence(evidence, artifact, manifest)

    assert not evidence.exists()


def test_failed_collection_keeps_and_reports_the_namespace_without_touching_an_older_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#344 fix round 2 (P1/P2, findings D/E/F - claude-agenttalk-reviewer-3's
    cold read): round 1 collected into a separate staging directory and
    disposed of it on failure - but the evidence JSON was already durably
    published (naming the FUTURE, not-yet-real paths) before the rename
    that would have made them real, so a rename failure left a broken
    record permanently clobbering whatever valid record had been at that
    path before. There is no staging/rename anymore: collection happens
    straight into the final namespace, and the evidence JSON - naming
    only paths that already exist - is written LAST. A failure during
    collection now KEEPS the (partial) namespace - never deletes it - and
    reports it via exc.run_namespace; an older record already at the
    evidence path is completely untouched, byte for byte."""
    manifest = dev_gate.validate_manifest(_manifest())
    evidence = tmp_path / "bundle" / "evidence.json"
    older = _leg_artifact(manifest, "linux/3.10")
    older["run_id"] = "older-run-id"
    _write_check_logs(older, tmp_path / "older-run")
    _write_artifact_files(older, tmp_path / "older-run")
    dev_gate.write_run_evidence(evidence, older, manifest)
    older_bytes = evidence.read_bytes()

    failing = _leg_artifact(manifest, "linux/3.12")
    failing["run_id"] = "failing-run-id"
    _write_check_logs(failing, tmp_path / "failing-run")
    _write_artifact_files(failing, tmp_path / "failing-run")
    read_calls = 0
    original_read = dev_gate._read_check_log

    def fail_second_read(path):
        nonlocal read_calls
        read_calls += 1
        if read_calls == 2:
            raise OSError("injected unreadable log")
        return original_read(path)

    monkeypatch.setattr(dev_gate, "_read_check_log", fail_second_read)
    with pytest.raises(dev_gate.GateBlock, match="evidence_log_collection_failed") as excinfo:
        dev_gate.write_run_evidence(evidence, failing, manifest)

    namespace = evidence.parent / "failing-run-id"
    assert excinfo.value.run_namespace == namespace
    assert namespace.is_dir()  # kept, not deleted
    assert evidence.read_bytes() == older_bytes  # the older record is untouched


def test_evidence_json_write_failure_keeps_the_namespace_and_the_older_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same bar as above, for a failure writing the evidence JSON itself
    (after every file it will name is already real and verified): the
    fully-collected namespace is kept and reported, and an older record
    already at the evidence path survives untouched."""
    manifest = dev_gate.validate_manifest(_manifest())
    evidence = tmp_path / "bundle" / "evidence.json"
    older = _leg_artifact(manifest, "linux/3.10")
    older["run_id"] = "older-run-id"
    _write_check_logs(older, tmp_path / "older-run")
    _write_artifact_files(older, tmp_path / "older-run")
    dev_gate.write_run_evidence(evidence, older, manifest)
    older_bytes = evidence.read_bytes()

    failing = _leg_artifact(manifest, "linux/3.12")
    failing["run_id"] = "failing-run-id"
    _write_check_logs(failing, tmp_path / "failing-run")
    _write_artifact_files(failing, tmp_path / "failing-run")

    def fail_write(path, text, **kwargs):
        raise OSError("injected evidence write failure")

    monkeypatch.setattr(dev_gate, "write_text", fail_write)
    with pytest.raises(dev_gate.GateBlock, match="evidence_write_failed") as excinfo:
        dev_gate.write_run_evidence(evidence, failing, manifest)

    namespace = evidence.parent / "failing-run-id"
    assert excinfo.value.run_namespace == namespace
    assert namespace.is_dir()
    for item in failing["artifacts"].values():
        assert (evidence.parent / item["artifact_path"]).exists()  # already real before the write was attempted
    assert evidence.read_bytes() == older_bytes


def _write_check_logs(artifact: dict, directory: Path) -> None:
    directory.mkdir()
    for check in artifact["checks"]:
        path = directory / f"{check['id']}.log"
        path.write_text(f"Output for {check['id']}\n", encoding="utf-8")
        check["log"] = {"path": str(path.resolve()), "sha256": dev_gate.sha256_bytes(path.read_bytes())}


def _write_artifact_files(artifact: dict, directory: Path) -> None:
    """Give every artifacts.* entry a real backing file, rewriting the
    matching check's argv in lockstep so the aggregate wheel/pip-audit
    binding cross-check keeps agreeing with the recorded path -
    write_run_evidence's collection step requires `path` to be a real,
    readable file, same as it already requires for check logs."""
    directory.mkdir(exist_ok=True)
    for item in artifact["artifacts"].values():
        old_path = item["path"]
        target = directory / item["filename"]
        content = f"placeholder for {item['filename']}\n".encode("utf-8")
        target.write_bytes(content)
        item.update(path=str(target.resolve()), size_bytes=len(content), sha256=dev_gate.sha256_bytes(content))
        for check in artifact["checks"]:
            check["argv"] = [str(target.resolve()) if arg == old_path else arg for arg in check["argv"]]


def test_historical_evidence_without_artifact_path_still_validates() -> None:
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.10")
    assert all(set(check["log"]) == {"path", "sha256"} for check in artifact["checks"])
    assert dev_gate.validate_run_artifact(artifact, manifest) == artifact


def test_previous_writer_shared_logs_layout_still_validates(tmp_path: Path) -> None:
    """#344 fix round 1 (P2, connector 4180279376 - codex-agenttalk-reviewer-1's
    cold read): schema_version never changed, but a bundle the immediately
    previous writer actually produced - artifact_path populated, under the
    shared `logs/<check-id>.log` layout (no run_id namespace) - started being
    rejected as evidence_schema_invalid once this PR's per-run-namespaced
    format became the only one accepted. Both legitimate version-1 layouts
    must keep validating, with their logs re-verified either way."""
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.10")
    bundle = tmp_path / "bundle"
    (bundle / "logs").mkdir(parents=True)
    for check in artifact["checks"]:
        content = f"previous-writer log for {check['id']}\n".encode("utf-8")
        relative = f"logs/{check['id']}.log"
        (bundle / relative).write_bytes(content)
        check["log"] = {"path": check["log"]["path"], "sha256": dev_gate.sha256_bytes(content),
                         "artifact_path": relative}

    assert dev_gate.validate_run_artifact(artifact, manifest, bundle_root=bundle) == artifact


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_aggregate_reverifies_collected_logs(tmp_path: Path, damage: str) -> None:
    manifest = _manifest()
    binding = _binding(manifest, root=_gate_repo(tmp_path))
    artifacts = []
    roots = {}
    for index, leg in enumerate(dev_gate.expected_ci_legs(manifest)):
        artifact = _leg_artifact(manifest, leg)
        _write_check_logs(artifact, tmp_path / f"runner-{index}")
        _write_artifact_files(artifact, tmp_path / f"runner-{index}")
        evidence = tmp_path / f"leg-{index}" / "evidence.json"
        dev_gate.write_run_evidence(evidence, artifact, manifest)
        artifacts.append(json.loads(evidence.read_text(encoding="utf-8")))
        roots[leg] = evidence.parent
    dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, binding, bundle_roots=roots)
    with pytest.raises(dev_gate.GateBlock, match="bundle root required"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, binding)
    log = roots[artifacts[-1]["ci_leg"]] / artifacts[-1]["checks"][-1]["log"]["artifact_path"]
    if damage == "missing":
        log.unlink()
    else:
        log.write_text("post-upload corruption", encoding="utf-8")
    with pytest.raises(dev_gate.GateBlock, match="evidence_log_invalid"):
        dev_gate.aggregate_leg_artifacts(manifest, "release", artifacts, binding, bundle_roots=roots)


def test_collection_rejects_oversize_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.10")
    _write_check_logs(artifact, tmp_path / "runner")
    monkeypatch.setattr(dev_gate, "MAX_CHECK_LOG_BYTES", 64)
    log = artifact["checks"][0]["log"]
    Path(log["path"]).write_bytes(b"x" * 65)
    log["sha256"] = dev_gate.sha256_bytes(b"x" * 65)
    evidence = tmp_path / "bundle" / "evidence.json"
    with pytest.raises(dev_gate.GateBlock, match="check_log_size_exceeded"):
        dev_gate.write_run_evidence(evidence, artifact, manifest)
    assert not evidence.exists()
    assert not (evidence.parent / artifact["run_id"] / "logs" / f"{artifact['checks'][0]['id']}.log").exists()


def test_oversize_process_output_blocks_even_when_process_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev_gate, "MAX_CHECK_LOG_BYTES", 64)
    outcome = dev_gate.run_command(
        check_id="oversize", argv=[sys.executable, "-c", "print('x' * 65)"],
        cwd=tmp_path, env=dict(os.environ), timeout_seconds=30, logs_dir=tmp_path / "logs",
    )
    assert outcome.returncode == 0
    assert outcome.status == "error"
    assert outcome.reason_code == "check_log_size_exceeded"


@pytest.mark.parametrize("relative", ["../outside.log", "/absolute.log", "logs/wrong-check.log"])
def test_evidence_rejects_invalid_artifact_log_reference(relative: str) -> None:
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.10")
    artifact["checks"][0]["log"]["artifact_path"] = relative

    with pytest.raises(dev_gate.GateBlock, match="artifact_path is malformed"):
        dev_gate.validate_run_artifact(artifact, manifest)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_evidence_collection_rejects_unavailable_or_changed_log(tmp_path: Path, damage: str) -> None:
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.10")
    _write_check_logs(artifact, tmp_path / "runner")
    path = Path(artifact["checks"][-1]["log"]["path"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("changed since recording", encoding="utf-8")
    evidence = tmp_path / "bundle" / "dev-gate-evidence.json"

    with pytest.raises(dev_gate.GateBlock, match="evidence_log_collection_failed"):
        dev_gate.write_run_evidence(evidence, artifact, manifest)
    assert not evidence.exists()


@pytest.mark.parametrize("timed_out", [False, True], ids=["multiple-failures", "timeout"])
def test_collected_pytest_log_preserves_full_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timed_out: bool,
) -> None:
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.10")
    _write_check_logs(artifact, tmp_path / "runner")
    _write_artifact_files(artifact, tmp_path / "runner")
    test_file = tmp_path / "test_product.py"
    test_file.write_text(
        "import sys\n"
        "def test_first():\n"
        "    print('product stdout first')\n"
        "    raise AssertionError('first assertion sentinel')\n"
        "def test_second():\n"
        "    print('product stdout second ' + 'x' * 6000)\n"
        "    print('product stderr second', file=sys.stderr)\n"
        "    raise AssertionError('second assertion sentinel')\n",
        encoding="utf-8",
    )
    if timed_out:
        # Deterministic timeout at the process boundary, with output already on disk.
        def timeout(argv, **kwargs):
            kwargs["stdout"].write("product stdout before timeout\nproduct stderr before timeout\n")
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        monkeypatch.setattr(dev_gate.subprocess, "run", timeout)
    outcome = dev_gate.run_command(
        check_id="pytest-source-py310",
        argv=[sys.executable, "-m", "pytest", "-q", "-rN", "-p", "no:cacheprovider", str(test_file)],
        cwd=tmp_path, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        timeout_seconds=30, logs_dir=tmp_path / "runner",
    )
    assert outcome.status == ("timeout" if timed_out else "fail")
    check = next(c for c in artifact["checks"] if c["id"] == "pytest-source-py310")
    check.update(status=outcome.status, exit_code=outcome.returncode, reason_code=outcome.reason_code,
                 diagnostic=outcome.diagnostic, argv=list(outcome.argv))
    check["log"] = {"path": str(outcome.log_path), "sha256": dev_gate.sha256_bytes(outcome.log_path.read_bytes())}
    artifact.update(verdict="block", blockers=dev_gate._blockers_for_checks(artifact["checks"]))
    artifact["summary"].update(passed=len(artifact["checks"]) - 1, blocked=1)
    evidence = tmp_path / "bundle" / "dev-gate-evidence.json"

    dev_gate.write_run_evidence(evidence, artifact, manifest)

    uploaded = json.loads(evidence.read_text(encoding="utf-8"))
    for record in uploaded["checks"]:
        assert (evidence.parent / record["log"]["artifact_path"]).is_file()
    content = (evidence.parent / check["log"]["artifact_path"]).read_text(encoding="utf-8")
    if timed_out:
        assert "product stdout before timeout" in content
        assert "product stderr before timeout" in content
        assert "timed out after 30s" in content
    else:
        for marker in ("first assertion sentinel", "second assertion sentinel",
                       "product stdout first", "product stdout second", "product stderr second"):
            assert marker in content
        assert "first assertion sentinel" not in check["diagnostic"]
        assert len(check["diagnostic"]) <= 2000


def test_aggregate_evidence_roundtrips_with_exact_leg_input_digests(tmp_path: Path) -> None:
    manifest = dev_gate.validate_manifest(_manifest())
    binding = _binding(manifest, root=_gate_repo(tmp_path))
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

    repo = _gate_repo(tmp_path)
    monkeypatch.setattr(dev_gate, "discover_repo_root", lambda: repo)
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
    repo = _gate_repo(tmp_path)
    monkeypatch.setattr(dev_gate, "discover_repo_root", lambda: repo)
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


@pytest.mark.parametrize("still_active", [True, False], ids=["still-active", "kept"])
def test_temp_root_is_refused_when_nested_inside_another_runs_marked_folder(
    tmp_path: Path, still_active: bool,
) -> None:
    """#344 fix round 1 (P1 - codex-agenttalk-reviewer-1's cold read, finding
    2): --temp-root inside an existing run's folder used to be accepted
    outright - that run's own later cleanup would then delete whatever a
    second run placed there, kept or not. A run writes its own ownership
    marker at allocation; a --temp-root choice nested anywhere inside a
    marked folder is now refused regardless of whether that owner is still
    mid-run or was explicitly kept - the marker alone is what matters, not
    which."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    owner = tmp_path / "owner-run"
    owner.mkdir()
    dev_gate._write_run_marker(owner, "owner-run-id")
    if not still_active:
        (owner / "blocked.log").write_text("kept after a block\n", encoding="utf-8")
    nested = owner / "nested-temp-root"

    with pytest.raises(dev_gate.GateBlock, match="another run's folder"):
        dev_gate._ensure_external(nested, candidate, None, "gate temp root")


@pytest.mark.parametrize("still_active", [True, False], ids=["still-active", "kept"])
def test_evidence_path_is_refused_when_nested_inside_another_runs_marked_folder(
    tmp_path: Path, still_active: bool,
) -> None:
    """Same bar as the --temp-root case above, for --evidence: connector
    reproduction used exactly this shape (A/saved-B/B.json)."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    owner = tmp_path / "owner-run"
    owner.mkdir()
    dev_gate._write_run_marker(owner, "owner-run-id")
    if not still_active:
        (owner / "blocked.log").write_text("kept after a block\n", encoding="utf-8")
    nested_evidence = owner / "saved-B" / "B.json"

    with pytest.raises(dev_gate.GateBlock, match="another run's folder"):
        dev_gate._external_location(
            nested_evidence, candidate_root=candidate, store_root=None, label="evidence path"
        )


def test_finalize_run_root_keeps_when_its_tree_holds_a_nested_runs_marker(tmp_path: Path) -> None:
    """#344 fix round 1 (P1, finding 2): if a second run's data ever ends up
    inside this run's folder anyway - bypassing the allocation-time refusal
    above (an older client, or any path that check does not yet cover) -
    this run's own cleanup must still refuse to delete through it. Simulates
    exactly that bypass by placing a foreign marker directly, without going
    through the (now refusing) real allocation path."""
    run_root = tmp_path / "agenttalk-dev-gate-owner"
    run_root.mkdir()
    dev_gate._write_run_marker(run_root, "owner-run-id")
    nested = run_root / "somehow-nested" / "agenttalk-dev-gate-other"
    nested.mkdir(parents=True)
    dev_gate._write_run_marker(nested, "other-run-id")
    (nested / "still-needed.txt").write_text("a concurrent or kept run needs this\n", encoding="utf-8")

    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="owner-run-id")

    assert reported == run_root
    assert run_root.exists()
    assert (nested / "still-needed.txt").read_text(encoding="utf-8") == "a concurrent or kept run needs this\n"


def test_finalize_run_root_keeps_when_its_tree_holds_a_foreign_bundle_without_a_json_suffix(
    tmp_path: Path,
) -> None:
    """#344 fix round 2 (P1, finding C - claude-agenttalk-reviewer-3's cold
    read, independently connector-reported): round 1 only recognised a
    foreign evidence record by a literal `.json` filename suffix - a
    record saved under any other name (fully within a --evidence caller's
    control) was invisible to the scan regardless of content. Detection
    now goes by marker alone: a foreign BUNDLE (the marker
    write_run_evidence writes into every namespace it creates, plus its
    record) is recognized by that marker, independent of what the record
    file itself happens to be named, and independent of what its own
    parent folder is named."""
    run_root = tmp_path / "agenttalk-dev-gate-owner"
    run_root.mkdir()
    dev_gate._write_run_marker(run_root, "owner-run-id")
    foreign_bundle = run_root / "some-arbitrary-folder-name"
    foreign_bundle.mkdir()
    dev_gate._write_run_marker(foreign_bundle, "other-run-id")
    (foreign_bundle / "record-with-no-extension").write_text(
        json.dumps({"artifact_type": "agenttalk-dev-gate-run", "run_id": "other-run-id"}),
        encoding="utf-8",
    )

    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="owner-run-id")

    assert reported == run_root
    assert run_root.exists()
    assert (foreign_bundle / "record-with-no-extension").exists()


def test_finalize_run_root_keeps_when_a_nested_marker_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#344 fix round 2 (P1, finding B - claude-agenttalk-reviewer-3's cold
    read): an unreadable candidate marker must fail CLOSED (keep), the
    same as a marker naming a different run - never be treated as absent
    just because it could not be read."""
    run_root = tmp_path / "agenttalk-dev-gate-owner"
    run_root.mkdir()
    dev_gate._write_run_marker(run_root, "owner-run-id")
    nested = run_root / "nested"
    nested.mkdir()
    dev_gate._write_run_marker(nested, "owner-run-id")  # would otherwise look like OUR OWN data
    marker = nested / dev_gate.RUN_MARKER_NAME

    def unreadable(*args, **kwargs):
        raise PermissionError("simulated unreadable marker")

    monkeypatch.setattr(dev_gate.Path, "read_text", unreadable)
    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="owner-run-id")

    assert reported == run_root
    assert run_root.exists()
    assert marker.exists()


def test_finalize_run_root_still_removes_a_file_a_person_placed_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted exception: a file a person places by hand inside a live
    run folder - no marker of its own - is the gate's documented private
    scratch, not something this scan protects. Only data the gate itself
    writes (always marked) blocks removal.

    #344 fix round 2 (P3, finding I): mocked non-elevated so this assertion
    of actual removal is deterministic even under an elevated CI runner."""
    monkeypatch.setattr(janitor, "_running_elevated", lambda: False)
    run_root = tmp_path / "agenttalk-dev-gate-owner"
    run_root.mkdir()
    dev_gate._write_run_marker(run_root, "owner-run-id")
    by_hand = run_root / "notes.txt"
    by_hand.write_text("a person's own scratch note", encoding="utf-8")

    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="owner-run-id")

    assert reported is None
    assert not run_root.exists()


def test_finalize_run_root_still_removes_its_own_nested_marker_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan must not become a false-positive trap: a nested marker
    naming this SAME run_id (its own published bundle, created inside its
    own run folder during this same run) must never block its own
    removal - only a marker naming a DIFFERENT run does.

    #344 fix round 2 (P3, finding I): mocked non-elevated so this assertion
    of actual removal is deterministic even under an elevated CI runner."""
    monkeypatch.setattr(janitor, "_running_elevated", lambda: False)
    run_root = tmp_path / "agenttalk-dev-gate-owner"
    run_root.mkdir()
    dev_gate._write_run_marker(run_root, "owner-run-id")
    own_bundle = run_root / "own-namespace"
    own_bundle.mkdir()
    dev_gate._write_run_marker(own_bundle, "owner-run-id")
    (own_bundle / "result.json").write_text(
        json.dumps({"artifact_type": "agenttalk-dev-gate-run", "run_id": "owner-run-id"}),
        encoding="utf-8",
    )

    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="owner-run-id")

    assert reported is None
    assert not run_root.exists()


# ------------------------------------------------------- run folder lifecycle (#338 v2)


def test_malformed_artifact_entry_raises_gateblock_not_keyerror() -> None:
    """#338 v2 (P3 on PR #339's HOLD - codex-agenttalk-developer-5's cold
    read): a record with artifact_path present but filename missing must
    raise GateBlock, never KeyError. Required-field presence and type are
    confirmed before any derived field (the artifact_path format check) is
    computed from them."""
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "linux/3.12")
    item = artifact["artifacts"]["wheel"]
    item["artifact_path"] = f"{artifact['run_id']}/artifacts/wheel/agenttalk.whl"
    del item["filename"]

    with pytest.raises(dev_gate.GateBlock):
        dev_gate.validate_run_artifact(artifact, manifest)


def test_committed_version_is_validated_before_any_run_folder_is_allocated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#338 v2 (P2 on PR #339's HOLD): a committed pyproject.toml with no
    version made _committed_version raise AFTER the run folder was already
    allocated, leaving an unreported folder behind (run_dir: null while one
    existed on disk). The committed version is now read before the run
    folder is allocated at all, so this failure mode leaves nothing behind
    to report."""
    repo = _gate_repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text('[project]\nname = "agenttalk"\n', encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "missing version fixture")
    monkeypatch.setattr(dev_gate, "discover_repo_root", lambda: repo)
    monkeypatch.setattr(dev_gate, "reenter_candidate_source", lambda *a: None)
    external = tmp_path / "external"
    args = build_parser().parse_args(["dev-gate", "--temp-root", str(external)])

    assert cmd_dev_gate(args) == 2
    output = capsys.readouterr()
    run_dirs = [p for p in external.iterdir() if p.is_dir() and p.name.startswith("agenttalk-dev-gate-")] \
        if external.exists() else []
    assert run_dirs == []  # no run folder was ever allocated to report
    summary = json.loads(output.out)
    assert summary.get("run_dir") is None, output


def test_evidence_directory_named_like_a_run_folder_survives_janitor(tmp_path: Path) -> None:
    """#338 v2 (P2 on PR #339's HOLD): this recast deliberately does NOT teach
    janitor to recognise gate run folders by name - a name cannot prove a
    folder is disposable, and a permanent --evidence destination can happen
    to look exactly like one. Confirm an aged, name-shaped evidence
    directory is never selected as a candidate and survives even an
    explicit --apply pass."""
    repo = tmp_path / "repo"
    _janitor_tests._init_repo(repo)
    external = tmp_path / "external"
    evidence = external / "agenttalk-dev-gate-20261004" / "result.json"
    dev_gate._external_location(evidence, candidate_root=repo, store_root=None, label="evidence path")
    evidence.write_text('{"durable": true}', encoding="utf-8")
    _janitor_tests._backdate_tree(evidence.parent, 5)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=external, repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES,
        tmp_families=janitor.DEFAULT_TMP_FAMILIES, foreign=[], default_branches=["master", "main"],
    )
    candidates, errors = janitor.find_candidates(cfg)
    assert not errors
    assert evidence.parent not in {c.path for c in candidates}

    janitor.apply(cfg, janitor.JanitorReport(candidates, [], [], [], []))
    assert evidence.exists()


def test_should_keep_run_dir_decision() -> None:
    assert dev_gate._should_keep_run_dir(keep_run_dir=True, verdict="pass") is True
    assert dev_gate._should_keep_run_dir(keep_run_dir=False, verdict="block") is True
    assert dev_gate._should_keep_run_dir(keep_run_dir=False, verdict="pass") is False


def test_finalize_run_root_removes_a_passing_runs_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#344 fix round 2 (P3, finding I): mocked non-elevated so this
    assertion of actual removal is deterministic even under an elevated
    CI runner."""
    monkeypatch.setattr(janitor, "_running_elevated", lambda: False)
    run_root = tmp_path / "agenttalk-dev-gate-fixture"
    run_root.mkdir()
    (run_root / "logs").mkdir()
    (run_root / "logs" / "pytest-source-py310.log").write_text("ok\n", encoding="utf-8")

    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="fixture-run-id")
    assert reported is None
    assert not run_root.exists()


def test_finalize_run_root_keeps_when_asked_or_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "agenttalk-dev-gate-fixture"
    run_root.mkdir()
    marker = run_root / "locked.log"
    marker.write_text("build failed\n", encoding="utf-8")

    assert dev_gate._finalize_run_root(run_root, keep=True, run_id="fixture-run-id") == run_root
    assert run_root.is_dir()
    assert marker.read_text(encoding="utf-8") == "build failed\n"

    monkeypatch.setattr(janitor, "remove_conservatively", lambda path: False)
    reported = dev_gate._finalize_run_root(run_root, keep=False, run_id="fixture-run-id")
    assert reported == run_root
    assert run_root.exists()


# ------------------------------------------------------- run folder lifecycle (#344 fix round 1)


def test_cli_names_the_retained_folder_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#344 fix round 1 (P2, connector 4180279366 - codex-agenttalk-reviewer-1's
    cold read): execute_gate's own boundary already attached a retained run
    folder to ANY exception escaping _run_gate_after_allocation, including
    KeyboardInterrupt - but cmd_dev_gate's own `except Exception` never sees
    a KeyboardInterrupt (it is a BaseException), so it always reached
    cli.main's generic, unconditional `except KeyboardInterrupt` unreported:
    a Ctrl-C after a real run folder was allocated printed only
    'agenttalk: interrupted' and exited 130, naming nothing. Exercised
    through the real public entry point, `cli.main`, not cmd_dev_gate
    directly - that public boundary is exactly what missed it."""
    repo = _gate_repo(tmp_path)
    monkeypatch.setattr(dev_gate, "discover_repo_root", lambda: repo)
    monkeypatch.setattr(dev_gate, "reenter_candidate_source", lambda *a: None)

    def interrupted(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(dev_gate, "_run_gate_after_allocation", interrupted)
    external = tmp_path / "external"

    rc = cli_main(["dev-gate", "--temp-root", str(external)])

    assert rc == 130
    run_dirs = [p for p in external.iterdir() if p.is_dir() and p.name.startswith("agenttalk-dev-gate-")]
    assert len(run_dirs) == 1  # never deleted
    err = capsys.readouterr().err
    assert str(run_dirs[0]) in err, err


def test_cli_names_the_retained_namespace_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#344 fix round 2 (P2, finding F - claude-agenttalk-reviewer-3's cold
    read, connector 4180508912): write_run_evidence attaches a kept,
    unpublished namespace to its own exception via `exc.run_namespace` -
    but no caller ever read that attribute: execute_gate's own boundary
    only ever sets `.run_root`, and cmd_dev_gate's exception handler only
    ever read `.run_root` back. The namespace stayed real on disk and
    completely undiscoverable from the command's own output. The CLI now
    reads and reports `.run_namespace` the same way it already does
    `.run_root`, in both the JSON summary and on stderr."""
    repo = _gate_repo(tmp_path)
    monkeypatch.setattr(dev_gate, "discover_repo_root", lambda: repo)
    monkeypatch.setattr(dev_gate, "reenter_candidate_source", lambda *a: None)
    namespace = tmp_path / "external" / "some-run-id"

    def fail(*args, **kwargs):
        block = dev_gate.GateBlock("evidence_log_collection_failed", "synthetic collection failure")
        block.run_namespace = namespace
        raise block

    monkeypatch.setattr(dev_gate, "write_run_evidence", fail)
    external = tmp_path / "external"
    args = build_parser().parse_args(["dev-gate", "--temp-root", str(external)])

    assert cmd_dev_gate(args) == 2

    out, err = capsys.readouterr()
    summary = json.loads(out)
    assert summary.get("run_namespace") == str(namespace), out
    assert str(namespace) in err, err
