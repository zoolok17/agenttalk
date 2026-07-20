"""Strict, SHA-bound developer gate for the agenttalk repository.

Unlike :mod:`agenttalk.assurance`, this module is voting: a required omission,
tool failure, or incomplete evidence set is a BLOCK.  The pure manifest and
evidence helpers live here so the subprocess-heavy runner can be tested without
recursively launching the project's own pytest suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
# Subprocesses use explicit argv lists and never enable a shell.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from pathlib import PureWindowsPath
from typing import Any, Sequence

from agenttalk import __version__
from agenttalk._atomic import write_text


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "agenttalk-dev-gate-run"
AGGREGATE_ARTIFACT_TYPE = "agenttalk-dev-gate-aggregate"
PREFLIGHT_ARTIFACT_TYPE = "agenttalk-dev-gate-preflight-block"
DEFAULT_MANIFEST = "dev-gate.json"
DEFAULT_PROFILE = "release"

REQUIRED_CI_OSES = ("linux", "windows", "macos")
REQUIRED_CI_PYTHONS = ("3.10", "3.11", "3.12", "3.13")
REQUIRED_LOCAL_PYTHONS = ("3.10", "3.14")
REQUIRED_MODES = ("source", "wheel")
REQUIRED_CHECK_KINDS = {
    "pytest",
    "ruff",
    "bandit",
    "gitleaks",
    "pip-audit",
    "semgrep",
    "zizmor",
    "package-build",
    "wheel-install",
    "wheel-dependency-check",
    "wheel-contract",
    "git-binding",
    "final-binding",
}
REQUIRED_CONFIGURED_CHECKS = {
    "pytest": "pytest",
    "ruff": "ruff",
    "bandit": "bandit",
    "gitleaks": "gitleaks-git",
    "pip-audit": "pip-audit",
    "semgrep": "semgrep",
    "zizmor": "zizmor",
    "package-build": "python-build",
    "wheel-contract": "wheel-contract",
}
CHECK_STATUSES = {"pass", "fail", "error", "missing", "timeout", "blocked_dependency"}
TIMEOUT_CHECKS = set(REQUIRED_CONFIGURED_CHECKS)


# Resolve a Python-backed gate tool while isolated from the candidate CWD and
# PYTHONPATH, then add the candidate import root only after the real tool's
# ``__main__`` module has been fixed.  This prevents a checked-in or ignored
# ``pytest.py``/``pip.py``/etc. from turning a voting check into a no-op.
_ISOLATED_TOOL_LAUNCHER = """
import importlib.util
import sys

module = sys.argv.pop(1)
candidate_import_root = sys.argv.pop(1)
spec = importlib.util.find_spec(module + ".__main__")
if spec is None or spec.loader is None:
    raise SystemExit("required module has no executable __main__: " + module)
code = spec.loader.get_code(spec.name)
if code is None:
    raise SystemExit("required module has no executable code: " + module)
if candidate_import_root:
    sys.path.insert(0, candidate_import_root)
sys.argv[0] = spec.origin or module
namespace = {
    "__name__": "__main__",
    "__file__": spec.origin,
    "__cached__": spec.cached,
    "__loader__": spec.loader,
    "__package__": spec.parent,
    "__spec__": spec,
}
exec(code, namespace, namespace)
""".strip()


# Re-entry is different from a tool launch: the committed export is the only
# application package that may be resolved at all.  ``-I`` removes CWD,
# PYTHONPATH, user-site, and environment contamination before this fixed shim
# inserts that trusted external export.
_ISOLATED_SOURCE_LAUNCHER = """
import runpy
import sys

committed_src = sys.argv.pop(1)
sys.path.insert(0, committed_src)
runpy.run_module("agenttalk", run_name="__main__", alter_sys=True)
""".strip()


class GateBlock(RuntimeError):
    """Expected fail-closed gate outcome with a stable reason code."""

    def __init__(self, code: str, detail: str, *, check_id: str | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.check_id = check_id


@dataclass(frozen=True)
class CandidateBinding:
    root: Path
    candidate_sha: str
    candidate_tree: str
    manifest_git_blob: str
    manifest_sha256: str
    manifest_bytes: bytes
    runner_git_blob: str
    runner_module_sha256: str
    clean: bool
    dirty_entries: tuple[str, ...]
    in_progress: tuple[str, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateBlock("manifest_schema_invalid", f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GateBlock("manifest_schema_invalid", f"{label} must be an array")
    if len(value) != len({json.dumps(item, sort_keys=True) for item in value}):
        raise GateBlock("manifest_schema_invalid", f"{label} contains duplicates")
    return value


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise GateBlock("manifest_schema_invalid", f"{label} has unknown field(s): {', '.join(unknown)}")


def validate_manifest(data: Any) -> dict[str, Any]:
    """Validate and return the strict committed dev-gate manifest."""

    manifest = _require_object(data, "manifest")
    _reject_unknown(manifest, {"schema_version", "profiles", "checks", "ci_native_exceptions"}, "manifest")
    if (
        not isinstance(manifest.get("schema_version"), int)
        or isinstance(manifest.get("schema_version"), bool)
        or manifest["schema_version"] != SCHEMA_VERSION
    ):
        raise GateBlock("manifest_schema_invalid", "schema_version must be 1")

    profiles = _require_object(manifest.get("profiles"), "profiles")
    if set(profiles) != {DEFAULT_PROFILE}:
        raise GateBlock("manifest_schema_invalid", "profiles must contain only release")
    profile = _require_object(profiles[DEFAULT_PROFILE], "profiles.release")
    _reject_unknown(profile, {"test_modes", "local", "ci"}, "profiles.release")
    modes = _require_list(profile.get("test_modes"), "profiles.release.test_modes")
    if modes != list(REQUIRED_MODES):
        raise GateBlock("manifest_floor_weakened", "release test_modes must be source and wheel")

    local = _require_object(profile.get("local"), "profiles.release.local")
    _reject_unknown(local, {"python_minors", "required_checks"}, "profiles.release.local")
    local_pythons = _require_list(local.get("python_minors"), "profiles.release.local.python_minors")
    if local_pythons != list(REQUIRED_LOCAL_PYTHONS):
        raise GateBlock("manifest_floor_weakened", "local Python minors must be 3.10 and 3.14")
    local_checks = _require_list(local.get("required_checks"), "profiles.release.local.required_checks")
    required_local = {
        "git-binding",
        "pytest",
        "ruff",
        "bandit",
        "gitleaks",
        "pip-audit",
        "semgrep",
        "zizmor",
        "package-build",
        "wheel-install",
        "wheel-dependency-check",
        "wheel-contract",
        "final-binding",
    }
    if set(local_checks) != required_local:
        raise GateBlock("manifest_floor_weakened", "local required-check floor changed")

    ci = _require_object(profile.get("ci"), "profiles.release.ci")
    _reject_unknown(
        ci,
        {"oses", "python_minors", "canonical_static_leg", "per_leg_checks", "canonical_checks"},
        "profiles.release.ci",
    )
    oses = _require_list(ci.get("oses"), "profiles.release.ci.oses")
    pythons = _require_list(ci.get("python_minors"), "profiles.release.ci.python_minors")
    if oses != list(REQUIRED_CI_OSES) or pythons != list(REQUIRED_CI_PYTHONS):
        raise GateBlock("manifest_floor_weakened", "CI matrix must be 3 OS x Python 3.10-3.13")
    canonical = ci.get("canonical_static_leg")
    expected_legs = [f"{os_name}/{python}" for os_name in oses for python in pythons]
    if canonical not in expected_legs:
        raise GateBlock("manifest_schema_invalid", "canonical_static_leg is outside the declared matrix")
    if canonical != "linux/3.12":
        raise GateBlock("manifest_floor_weakened", "canonical_static_leg must remain linux/3.12")
    per_leg = _require_list(ci.get("per_leg_checks"), "profiles.release.ci.per_leg_checks")
    canonical_checks = _require_list(ci.get("canonical_checks"), "profiles.release.ci.canonical_checks")
    required_per_leg = {
        "git-binding",
        "pytest",
        "package-build",
        "wheel-install",
        "wheel-dependency-check",
        "wheel-contract",
        "final-binding",
    }
    required_canonical = {"ruff", "bandit", "gitleaks", "pip-audit", "semgrep", "zizmor"}
    if set(per_leg) != required_per_leg or set(canonical_checks) != required_canonical:
        raise GateBlock("manifest_floor_weakened", "CI required-check floor changed")

    checks = _require_object(manifest.get("checks"), "checks")
    if set(checks) != set(REQUIRED_CONFIGURED_CHECKS):
        raise GateBlock("manifest_floor_weakened", "configured required-check set changed")
    allowed_check_fields = {
        "kind",
        "paths",
        "args",
        "exclude",
        "error",
        "config",
        "configs",
        "require_full_history",
        "strict",
        "rule_timeout_seconds",
        "timeout_seconds",
        "sdist",
        "wheel",
        "required_sdist_paths",
        "required_wheel_resources",
        "dependency_index",
        "test_requirement",
    }
    for check_id, expected_kind in REQUIRED_CONFIGURED_CHECKS.items():
        spec = _require_object(checks.get(check_id), f"checks.{check_id}")
        _reject_unknown(spec, allowed_check_fields, f"checks.{check_id}")
        if spec.get("kind") != expected_kind:
            raise GateBlock("manifest_floor_weakened", f"checks.{check_id}.kind changed")
        timeout = spec.get("timeout_seconds")
        if check_id in TIMEOUT_CHECKS and (
            not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0
        ):
            raise GateBlock("manifest_schema_invalid", f"checks.{check_id}.timeout_seconds must be positive")

    required_contract = {
        "pytest": {"paths": ["tests"], "args": ["-q"], "test_requirement": "pytest>=8.0"},
        "ruff": {"paths": ["src", "tests"]},
        "bandit": {"paths": ["src"], "exclude": ["src/agenttalk/skills"]},
        "gitleaks": {"config": ".gitleaks.toml", "require_full_history": True},
        "pip-audit": {"strict": True},
        "semgrep": {
            "configs": ["p/python", "p/security-audit", ".semgrep/agenttalk.yml"],
            "error": True,
            "strict": False,
            "rule_timeout_seconds": 120,
        },
        "zizmor": {"paths": [".github/workflows"]},
        "package-build": {
            "sdist": True,
            "wheel": True,
            "required_sdist_paths": [
                "dev-gate.json",
                "dev-gate-requirements.txt",
                "docs/AGENTTALK-NEW-USER-MANUAL.pdf",
                "src/agenttalk/web_static/console.js",
            ],
        },
        "wheel-contract": {
            "dependency_index": "https://pypi.org/simple",
            "timeout_seconds": 600,
            "required_wheel_resources": [
                "agenttalk/web_static/console.css",
                "agenttalk/web_static/console.js",
            ]
        },
    }
    for check_id, expected_fields in required_contract.items():
        spec = checks[check_id]
        for field, expected in expected_fields.items():
            if spec.get(field) != expected:
                raise GateBlock(
                    "manifest_floor_weakened",
                    f"checks.{check_id}.{field} must remain {expected!r}",
                )

    exceptions = _require_list(manifest.get("ci_native_exceptions"), "ci_native_exceptions")
    if len(exceptions) != 1:
        raise GateBlock("manifest_floor_weakened", "CodeQL must be the single CI-native exception")
    exception = _require_object(exceptions[0], "ci_native_exceptions[0]")
    _reject_unknown(
        exception,
        {"id", "workflow", "job", "required", "executed_by_gate", "reason"},
        "ci_native_exceptions[0]",
    )
    if (
        exception.get("id") != "codeql"
        or exception.get("workflow") != ".github/workflows/security.yml"
        or exception.get("job") != "codeql"
        or exception.get("required") is not True
        or exception.get("executed_by_gate") is not False
        or not isinstance(exception.get("reason"), str)
        or not exception["reason"].strip()
    ):
        raise GateBlock("manifest_floor_weakened", "CodeQL exception contract changed")
    return manifest


def expected_ci_legs(manifest: dict[str, Any], profile: str = DEFAULT_PROFILE) -> list[str]:
    validated = validate_manifest(manifest)
    try:
        ci = validated["profiles"][profile]["ci"]
    except KeyError as exc:
        raise GateBlock("profile_unknown", f"unknown profile {profile!r}") from exc
    return [f"{os_name}/{python}" for os_name in ci["oses"] for python in ci["python_minors"]]


def parse_ci_leg(value: str, manifest: dict[str, Any], profile: str = DEFAULT_PROFILE) -> str:
    if not isinstance(value, str) or value.count("/") != 1:
        raise GateBlock("ci_leg_invalid", "CI leg must be <os>/<python>")
    os_name, python = value.split("/", 1)
    normalized = f"{os_name.lower()}/{python}"
    if normalized not in expected_ci_legs(manifest, profile):
        raise GateBlock("ci_leg_invalid", f"undeclared CI leg {value!r}")
    return normalized


def _normalized_logical_plan(manifest: dict[str, Any], profile: str) -> dict[str, Any]:
    validated = validate_manifest(manifest)
    if profile not in validated["profiles"]:
        raise GateBlock("profile_unknown", f"unknown profile {profile!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "profile_config": validated["profiles"][profile],
        "checks": validated["checks"],
        "ci_native_exceptions": validated["ci_native_exceptions"],
        "mandatory_floor": sorted(REQUIRED_CHECK_KINDS),
    }


def logical_plan_digest(
    manifest: dict[str, Any],
    profile: str = DEFAULT_PROFILE,
    *,
    runtime: dict[str, Any] | None = None,
) -> str:
    """Digest the logical plan only; machine paths deliberately do not participate."""

    del runtime
    payload = json.dumps(_normalized_logical_plan(manifest, profile), sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def _python_suffix(version: str) -> str:
    return "py" + version.replace(".", "")


def required_check_ids(
    manifest: dict[str, Any],
    profile: str = DEFAULT_PROFILE,
    *,
    execution_scope: str,
    ci_leg: str | None = None,
) -> list[str]:
    validated = validate_manifest(manifest)
    profile_config = validated["profiles"][profile]
    if execution_scope == "local":
        pythons = profile_config["local"]["python_minors"]
        base_checks = profile_config["local"]["required_checks"]
    elif execution_scope == "ci-leg":
        if ci_leg is None:
            raise GateBlock("ci_leg_invalid", "ci-leg scope requires a leg")
        normalized_leg = parse_ci_leg(ci_leg, validated, profile)
        python = normalized_leg.split("/", 1)[1]
        pythons = [python]
        ci = profile_config["ci"]
        base_checks = list(ci["per_leg_checks"])
        if normalized_leg == ci["canonical_static_leg"]:
            base_checks.extend(ci["canonical_checks"])
    else:
        raise GateBlock("execution_scope_invalid", f"unknown execution scope {execution_scope!r}")

    result: list[str] = []
    for check_id in base_checks:
        if check_id == "pytest":
            for python in pythons:
                for mode in profile_config["test_modes"]:
                    result.append(f"pytest-{mode}-{_python_suffix(python)}")
        elif check_id in {"wheel-install", "wheel-dependency-check", "wheel-contract"}:
            for python in pythons:
                result.append(f"{check_id}-{_python_suffix(python)}")
        else:
            result.append(check_id)
    return result


def _require_artifact_fields(mapping: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(mapping))
    unknown = sorted(set(mapping) - required)
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unknown:
            parts.append("unknown " + ", ".join(unknown))
        raise GateBlock("evidence_schema_invalid", f"{label}: {'; '.join(parts)}")


def _is_hash(value: Any, *lengths: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and bool(re.fullmatch(r"[0-9a-f]+", value))
    )


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_absolute_path_text(value: Any) -> bool:
    if not _is_nonempty_string(value):
        return False
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _path_contains(parent: str, child: str) -> bool:
    if PureWindowsPath(parent).is_absolute() and PureWindowsPath(child).is_absolute():
        parent_path = PureWindowsPath(parent)
        child_path = PureWindowsPath(child)
    elif PurePosixPath(parent).is_absolute() and PurePosixPath(child).is_absolute():
        parent_path = PurePosixPath(parent)
        child_path = PurePosixPath(child)
    else:
        return False
    if ".." in parent_path.parts or ".." in child_path.parts:
        return False
    try:
        child_path.relative_to(parent_path)
    except ValueError:
        return False
    return True


def _valid_timestamp(value: Any) -> bool:
    if not _is_nonempty_string(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _check_semantics(check_id: str) -> tuple[str, str | None, str | None, bool]:
    pytest_match = re.fullmatch(r"pytest-(source|wheel)-py(\d)(\d+)", check_id)
    if pytest_match:
        return (
            "pytest",
            pytest_match.group(1),
            f"{pytest_match.group(2)}.{pytest_match.group(3)}",
            True,
        )
    wheel_match = re.fullmatch(
        r"(wheel-install|wheel-dependency-check|wheel-contract)-py(\d)(\d+)", check_id
    )
    if wheel_match:
        return (
            wheel_match.group(1),
            "wheel",
            f"{wheel_match.group(2)}.{wheel_match.group(3)}",
            wheel_match.group(1) == "wheel-contract",
        )
    kinds = {
        "git-binding": "git-binding",
        "package-build": "python-build",
        "ruff": "ruff",
        "bandit": "bandit",
        "gitleaks": "gitleaks",
        "pip-audit": "pip-audit",
        "semgrep": "semgrep",
        "zizmor": "zizmor",
        "final-binding": "final-binding",
    }
    try:
        return kinds[check_id], None, None, False
    except KeyError as exc:
        raise GateBlock("evidence_schema_invalid", f"unknown check ID {check_id!r}") from exc


def _path_basename(value: str) -> str:
    if PureWindowsPath(value).is_absolute():
        return PureWindowsPath(value).name.casefold()
    return PurePosixPath(value).name.casefold()


def _validate_check_command(
    *,
    check_id: str,
    item: dict[str, Any],
    manifest: dict[str, Any],
    interpreter_paths: dict[str, str],
    expected_minors: list[str],
) -> None:
    """Require a passing check to prove the committed command shape."""

    argv = item["argv"]
    tool_path = item["tool"]["path"]
    if not argv or argv[0] != tool_path:
        raise GateBlock("evidence_command_mismatch", f"{check_id} tool and argv differ")

    def require_python(minor: str) -> str:
        path = interpreter_paths.get(minor)
        if path is None or tool_path != path:
            raise GateBlock(
                "evidence_command_mismatch",
                f"{check_id} did not use the declared CPython {minor}",
            )
        return path

    def require_runtime_python(minor: str, role: str) -> str:
        direct = interpreter_paths.get(minor)
        if direct is None:
            raise GateBlock("evidence_command_mismatch", f"{check_id} lacks creator CPython {minor}")
        runtime = _validate_runtime_environment(
            item["runtime_environment"],
            label=f"{check_id}.runtime_environment",
            direct_python=direct,
            expected_minor=minor,
            expected_role=role,
        )
        if tool_path != runtime["python_path"]:
            raise GateBlock("evidence_command_mismatch", f"{check_id} did not use its isolated venv")
        return runtime["python_path"]

    if check_id in {"git-binding", "final-binding"}:
        expected = [tool_path, "status", "--porcelain=v1", "--untracked-files=all"]
        valid = _path_basename(tool_path) in {"git", "git.exe"} and argv == expected
    elif check_id.startswith("pytest-"):
        _, mode, suffix = check_id.split("-")
        minor = f"{suffix[2]}.{suffix[3:]}"
        python = require_python(minor) if mode == "source" else require_runtime_python(minor, "test")
        spec = manifest["checks"]["pytest"]
        launcher = [python, "-I", "-c", _ISOLATED_TOOL_LAUNCHER, "pytest"]
        suffix_args = [*spec["args"], "-p", "no:cacheprovider", "--basetemp"]
        valid = (
            item["mode"] == mode
            and len(argv) == len(launcher) + 1 + len(suffix_args) + 1 + len(spec["paths"])
            and argv[: len(launcher)] == launcher
            and (
                _is_absolute_path_text(argv[len(launcher)])
                if mode == "source"
                else argv[len(launcher)] == ""
            )
            and argv[len(launcher) + 1 : len(launcher) + 1 + len(suffix_args)] == suffix_args
            and _is_absolute_path_text(argv[len(launcher) + 1 + len(suffix_args)])
            and argv[len(launcher) + 2 + len(suffix_args) :] == spec["paths"]
        )
    elif check_id == "package-build":
        python = require_python(expected_minors[0])
        prefix = [
            python,
            "-I",
            "-c",
            _ISOLATED_TOOL_LAUNCHER,
            "build",
            "",
            "--no-isolation",
            "--sdist",
            "--wheel",
            "--outdir",
        ]
        valid = len(argv) == len(prefix) + 1 and argv[: len(prefix)] == prefix and _is_absolute_path_text(argv[-1])
    elif check_id.startswith("wheel-install-"):
        suffix = check_id.rsplit("-", 1)[1]
        minor = f"{suffix[2]}.{suffix[3:]}"
        python = require_runtime_python(minor, "runtime")
        prefix = [
            python,
            "-I",
            "-c",
            _ISOLATED_TOOL_LAUNCHER,
            "pip",
            "",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--index-url",
            manifest["checks"]["wheel-contract"]["dependency_index"],
        ]
        valid = (
            len(argv) == len(prefix) + 1
            and argv[: len(prefix)] == prefix
            and _is_absolute_path_text(argv[-1])
        )
    elif check_id.startswith("wheel-dependency-check-"):
        suffix = check_id.rsplit("-", 1)[1]
        minor = f"{suffix[2]}.{suffix[3:]}"
        python = require_runtime_python(minor, "runtime")
        valid = argv == isolated_tool_argv(python, "pip", "check")
    elif check_id.startswith("wheel-contract-"):
        suffix = check_id.rsplit("-", 1)[1]
        minor = f"{suffix[2]}.{suffix[3:]}"
        python = require_runtime_python(minor, "runtime")
        valid = argv == isolated_tool_argv(python, "agenttalk", "--version")
    elif check_id == "ruff":
        python = require_python(expected_minors[-1])
        valid = argv == isolated_tool_argv(
            python,
            "ruff",
            "check",
            "--no-cache",
            *manifest["checks"]["ruff"]["paths"],
        )
    elif check_id == "bandit":
        python = require_python(expected_minors[-1])
        spec = manifest["checks"]["bandit"]
        valid = argv == isolated_tool_argv(
            python,
            "bandit",
            "-r",
            *spec["paths"],
            "-x",
            ",".join(spec["exclude"]),
        )
    elif check_id == "pip-audit":
        python = require_python(expected_minors[-1])
        prefix = isolated_tool_argv(
            python,
            "pip_audit",
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--requirement",
        )
        valid = len(argv) == len(prefix) + 1 and argv[: len(prefix)] == prefix and _is_absolute_path_text(argv[-1])
    elif check_id == "semgrep":
        spec = manifest["checks"]["semgrep"]
        expected = [tool_path, "scan", *[f"--config={value}" for value in spec["configs"]]]
        expected.extend(["--error", "--timeout", str(spec["rule_timeout_seconds"])])
        valid = _path_basename(tool_path) in {"semgrep", "semgrep.exe"} and argv == expected
    elif check_id == "zizmor":
        expected = [tool_path, *manifest["checks"]["zizmor"]["paths"]]
        valid = _path_basename(tool_path) in {"zizmor", "zizmor.exe"} and argv == expected
    elif check_id == "gitleaks":
        spec = manifest["checks"]["gitleaks"]
        valid = (
            _path_basename(tool_path) in {"gitleaks", "gitleaks.exe"}
            and len(argv) == 9
            and argv[1:3] == ["git", "--config"]
            and _is_absolute_path_text(argv[3])
            and _path_basename(argv[3]) == _path_basename(spec["config"])
            and argv[4:8] == ["--log-opts=--all", "--redact", "--no-color", "--no-banner"]
            and _is_absolute_path_text(argv[8])
        )
    else:
        raise GateBlock("evidence_command_mismatch", f"no command contract for {check_id}")
    if not valid:
        raise GateBlock("evidence_command_mismatch", f"{check_id} argv does not match the committed plan")


def _validate_import_provenance(value: Any, *, label: str, expected_version: str) -> None:
    item = _require_object(value, label)
    _require_artifact_fields(item, {"expected_root", "observed_path", "version"}, label)
    if (
        not _is_absolute_path_text(item["expected_root"])
        or not _is_absolute_path_text(item["observed_path"])
        or not _path_contains(item["expected_root"], item["observed_path"])
        or item["version"] != expected_version
    ):
        raise GateBlock("evidence_schema_invalid", f"{label} is not a bound import proof")


def _validate_runtime_environment(
    value: Any,
    *,
    label: str,
    direct_python: str,
    expected_minor: str,
    expected_role: str,
) -> dict[str, Any]:
    item = _require_object(value, label)
    _require_artifact_fields(
        item,
        {
            "role",
            "requested",
            "creator_path",
            "python_path",
            "prefix",
            "base_prefix",
            "system_site_packages",
        },
        label,
    )
    if (
        item["role"] != expected_role
        or item["requested"] != expected_minor
        or item["creator_path"] != direct_python
        or not _is_absolute_path_text(item["python_path"])
        or not _is_absolute_path_text(item["prefix"])
        or not _is_absolute_path_text(item["base_prefix"])
        or not _path_contains(item["prefix"], item["python_path"])
        or item["prefix"] == item["base_prefix"]
        or item["system_site_packages"] is not False
    ):
        raise GateBlock("evidence_schema_invalid", f"{label} is not an isolated venv proof")
    return item


def validate_run_artifact(artifact: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate a run artifact and every proof required by its declared plan."""

    record = _require_object(artifact, "artifact")
    required_top = {
        "schema_version",
        "artifact_type",
        "run_id",
        "started_at",
        "finished_at",
        "profile",
        "verdict",
        "complete",
        "execution_scope",
        "ci_leg",
        "subject",
        "manifest",
        "authority",
        "runner",
        "isolation",
        "interpreters",
        "required_check_ids",
        "checks",
        "artifacts",
        "external_inputs",
        "blockers",
        "summary",
    }
    _require_artifact_fields(record, required_top, "artifact")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != SCHEMA_VERSION
        or record["artifact_type"] != ARTIFACT_TYPE
        or not _is_nonempty_string(record["run_id"])
        or not _valid_timestamp(record["started_at"])
        or not _valid_timestamp(record["finished_at"])
    ):
        raise GateBlock("evidence_schema_invalid", "unexpected artifact schema or type")
    if record["profile"] != DEFAULT_PROFILE:
        raise GateBlock("evidence_schema_invalid", "unexpected profile")
    if not isinstance(record["verdict"], str) or record["verdict"] not in {"pass", "block"}:
        raise GateBlock("evidence_schema_invalid", "verdict must be pass or block")
    scope = record["execution_scope"]
    if scope == "ci-leg":
        if record["complete"] is not False:
            raise GateBlock("evidence_schema_invalid", "CI leg evidence must be incomplete")
        leg = parse_ci_leg(record["ci_leg"], manifest, record["profile"])
        expected_ids = required_check_ids(
            manifest, record["profile"], execution_scope="ci-leg", ci_leg=leg
        )
        expected_minors = [leg.split("/", 1)[1]]
        expected_os = leg.split("/", 1)[0]
    elif scope == "local":
        if record["complete"] is not True or record["ci_leg"] is not None:
            raise GateBlock("evidence_schema_invalid", "local evidence must be complete with no CI leg")
        expected_ids = required_check_ids(manifest, record["profile"], execution_scope="local")
        expected_minors = list(REQUIRED_LOCAL_PYTHONS)
        expected_os = None
    else:
        raise GateBlock("evidence_schema_invalid", "execution_scope must be local or ci-leg")
    if not isinstance(record["required_check_ids"], list) or record["required_check_ids"] != expected_ids:
        raise GateBlock("evidence_cardinality_invalid", "required check IDs do not match the logical plan")

    subject = _require_object(record["subject"], "subject")
    _require_artifact_fields(
        subject,
        {"candidate_sha", "candidate_tree", "version", "clean_before", "clean_after", "head_stable"},
        "subject",
    )
    if (
        not _is_hash(subject["candidate_sha"], 40, 64)
        or not _is_hash(subject["candidate_tree"], 40, 64)
        or not _is_nonempty_string(subject["version"])
        or any(
            not isinstance(subject[field], bool)
            for field in ("clean_before", "clean_after", "head_stable")
        )
    ):
        raise GateBlock("evidence_schema_invalid", "candidate binding is malformed")

    manifest_record = _require_object(record["manifest"], "manifest")
    _require_artifact_fields(
        manifest_record,
        {"path", "schema_version", "git_blob_id", "sha256", "logical_plan_sha256"},
        "manifest",
    )
    if (
        manifest_record["path"] != DEFAULT_MANIFEST
        or not isinstance(manifest_record["schema_version"], int)
        or isinstance(manifest_record["schema_version"], bool)
        or manifest_record["schema_version"] != SCHEMA_VERSION
        or not _is_hash(manifest_record["git_blob_id"], 40, 64)
        or not _is_hash(manifest_record["sha256"], 64)
        or not _is_hash(manifest_record["logical_plan_sha256"], 64)
        or manifest_record["logical_plan_sha256"] != logical_plan_digest(manifest, record["profile"])
    ):
        raise GateBlock("evidence_binding_mismatch", "manifest binding does not match the logical plan")

    authority = _require_object(record["authority"], "authority")
    _require_artifact_fields(
        authority,
        {
            "declared_required_ci_matrix",
            "local_interpreters",
            "ci_aggregate_authoritative",
            "ci_native_exceptions",
        },
        "authority",
    )
    if (
        authority["declared_required_ci_matrix"] != expected_ci_legs(manifest, record["profile"])
        or authority["local_interpreters"] != list(REQUIRED_LOCAL_PYTHONS)
        or authority["ci_aggregate_authoritative"] is not True
        or authority["ci_native_exceptions"] != manifest["ci_native_exceptions"]
    ):
        raise GateBlock("evidence_binding_mismatch", "authority declaration does not match")

    runner = _require_object(record["runner"], "runner")
    _require_artifact_fields(
        runner,
        {
            "agenttalk_version",
            "module_path",
            "git_blob_id",
            "module_sha256",
            "os",
            "architecture",
        },
        "runner",
    )
    if (
        runner["agenttalk_version"] != subject["version"]
        or runner["module_path"] != "src/agenttalk/dev_gate.py"
        or not _is_hash(runner["git_blob_id"], 40, 64)
        or not _is_hash(runner["module_sha256"], 64)
        or not _is_nonempty_string(runner["os"])
        or not _is_nonempty_string(runner["architecture"])
        or (expected_os is not None and runner["os"] != expected_os)
    ):
        raise GateBlock("evidence_binding_mismatch", "runner identity does not match the declared leg")

    isolation = _require_object(record["isolation"], "isolation")
    _require_artifact_fields(
        isolation,
        {
            "temp_outside_candidate",
            "temp_outside_store",
            "pytest_cache_disabled",
            "bytecode_disabled",
            "phase_isolated_exports",
            "pip_configuration_disabled",
            "child_path_sanitized",
        },
        "isolation",
    )
    if any(value is not True for value in isolation.values()):
        raise GateBlock("evidence_schema_invalid", "all isolation guarantees must be true")

    interpreters = _require_list(record["interpreters"], "interpreters")
    if len(interpreters) != len(expected_minors):
        raise GateBlock("evidence_cardinality_invalid", "interpreter evidence is incomplete")
    observed_minors: list[str] = []
    for index, interpreter in enumerate(interpreters):
        item = _require_object(interpreter, f"interpreters[{index}]")
        _require_artifact_fields(
            item,
            {"requested", "path", "implementation", "version", "status"},
            f"interpreters[{index}]",
        )
        if (
            not isinstance(item["requested"], str)
            or item["requested"] not in expected_minors
            or not isinstance(item["status"], str)
            or item["status"] not in {"pass", "missing", "mismatch"}
        ):
            raise GateBlock("evidence_schema_invalid", f"interpreters[{index}] is invalid")
        observed_minors.append(item["requested"])
        if item["status"] == "pass" and (
            not _is_absolute_path_text(item["path"])
            or item["implementation"] != "CPython"
            or not _is_nonempty_string(item["version"])
            or not item["version"].startswith(item["requested"] + ".")
        ):
            raise GateBlock("evidence_schema_invalid", f"interpreters[{index}] lacks a direct CPython proof")
        if item["status"] != "pass" and not _is_nonempty_string(item["version"]):
            raise GateBlock("evidence_schema_invalid", f"interpreters[{index}] lacks a failure detail")
    if observed_minors != expected_minors or len(observed_minors) != len(set(observed_minors)):
        raise GateBlock("evidence_cardinality_invalid", "interpreter evidence does not match the plan")
    interpreter_paths = {
        row["requested"]: row["path"] for row in interpreters if row["status"] == "pass"
    }

    checks = record["checks"]
    if not isinstance(checks, list):
        raise GateBlock("evidence_schema_invalid", "checks must be an array")
    actual_ids = [check.get("id") if isinstance(check, dict) else None for check in checks]
    if (
        not all(isinstance(check_id, str) for check_id in actual_ids)
        or len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != set(expected_ids)
    ):
        raise GateBlock("evidence_cardinality_invalid", "check result cardinality does not match required IDs")
    check_fields = {
        "id",
        "kind",
        "mode",
        "python",
        "required",
        "status",
        "argv",
        "tool",
        "exit_code",
        "duration_ms",
        "reason_code",
        "diagnostic",
        "log",
        "import_provenance",
        "runtime_environment",
    }
    checks_by_id: dict[str, dict[str, Any]] = {}
    for index, check in enumerate(checks):
        item = _require_object(check, f"checks[{index}]")
        _require_artifact_fields(item, check_fields, f"checks[{index}]")
        check_id = item["id"]
        expected_kind, expected_mode, expected_python, needs_provenance = _check_semantics(check_id)
        if not isinstance(item["status"], str) or item["status"] not in CHECK_STATUSES:
            raise GateBlock("evidence_status_invalid", f"unknown check status {item['status']!r}")
        if (
            item["required"] is not True
            or item["kind"] != expected_kind
            or item["mode"] != expected_mode
            or item["python"] != expected_python
            or not isinstance(item["duration_ms"], int)
            or isinstance(item["duration_ms"], bool)
            or item["duration_ms"] < 0
            or not isinstance(item["diagnostic"], str)
        ):
            raise GateBlock("evidence_schema_invalid", f"checks[{index}] semantics are invalid")
        if not isinstance(item["argv"], list) or not all(isinstance(arg, str) for arg in item["argv"]):
            raise GateBlock("evidence_schema_invalid", f"checks[{index}].argv must be an array of strings")
        tool = _require_object(item["tool"], f"checks[{index}].tool")
        _require_artifact_fields(tool, {"path", "version"}, f"checks[{index}].tool")
        log = _require_object(item["log"], f"checks[{index}].log")
        _require_artifact_fields(log, {"path", "sha256"}, f"checks[{index}].log")
        if not _is_absolute_path_text(log["path"]) or not _is_hash(log["sha256"], 64):
            raise GateBlock("evidence_schema_invalid", f"checks[{index}].log is malformed")
        if item["status"] == "pass":
            if (
                not isinstance(item["exit_code"], int)
                or isinstance(item["exit_code"], bool)
                or item["exit_code"] != 0
                or item["reason_code"] is not None
                or not item["argv"]
                or not _is_absolute_path_text(tool["path"])
                or not _is_nonempty_string(tool["version"])
            ):
                raise GateBlock("evidence_schema_invalid", f"checks[{index}] lacks passing execution evidence")
            _validate_check_command(
                check_id=check_id,
                item=item,
                manifest=manifest,
                interpreter_paths=interpreter_paths,
                expected_minors=expected_minors,
            )
        elif (
            (item["exit_code"] is not None and (
                not isinstance(item["exit_code"], int) or isinstance(item["exit_code"], bool)
            ))
            or not _is_nonempty_string(item["reason_code"])
        ):
            raise GateBlock("evidence_schema_invalid", f"checks[{index}] lacks blocking execution evidence")
        if needs_provenance and item["status"] == "pass":
            _validate_import_provenance(
                item["import_provenance"],
                label=f"checks[{index}].import_provenance",
                expected_version=subject["version"],
            )
        elif item["import_provenance"] is not None:
            _validate_import_provenance(
                item["import_provenance"],
                label=f"checks[{index}].import_provenance",
                expected_version=subject["version"],
            )
        is_wheel_runtime_check = (
            check_id.startswith("pytest-wheel-")
            or check_id.startswith("wheel-install-")
            or check_id.startswith("wheel-dependency-check-")
            or check_id.startswith("wheel-contract-")
        )
        if (
            is_wheel_runtime_check
            and item["status"] == "pass"
            and item["import_provenance"] is not None
            and item["import_provenance"]["expected_root"]
            != item["runtime_environment"]["prefix"]
        ):
            raise GateBlock(
                "evidence_binding_mismatch",
                f"checks[{index}] import proof is outside its isolated venv",
            )
        if not is_wheel_runtime_check and item["runtime_environment"] is not None:
            raise GateBlock(
                "evidence_schema_invalid",
                f"checks[{index}] unexpectedly claims a wheel runtime environment",
            )
        checks_by_id[check_id] = item

    for minor in expected_minors:
        suffix = _python_suffix(minor)
        runtime_ids = [
            f"wheel-install-{suffix}",
            f"wheel-dependency-check-{suffix}",
            f"wheel-contract-{suffix}",
        ]
        passing_runtime = [
            checks_by_id[check_id]
            for check_id in runtime_ids
            if check_id in checks_by_id and checks_by_id[check_id]["status"] == "pass"
        ]
        if passing_runtime:
            first = passing_runtime[0]["runtime_environment"]
            if any(check["runtime_environment"] != first for check in passing_runtime[1:]):
                raise GateBlock(
                    "evidence_binding_mismatch",
                    f"wheel runtime proofs differ for Python {minor}",
                )
        wheel_test = checks_by_id.get(f"pytest-wheel-{suffix}")
        if wheel_test is not None and wheel_test["status"] == "pass" and passing_runtime:
            test_environment = wheel_test["runtime_environment"]
            if test_environment["prefix"] == passing_runtime[0]["runtime_environment"]["prefix"]:
                raise GateBlock(
                    "evidence_schema_invalid",
                    f"wheel tests reused the runtime contract venv for Python {minor}",
                )

    artifacts = _require_object(record["artifacts"], "artifacts")
    allowed_artifacts = {"sdist", "wheel"}
    if "pip-audit" in expected_ids:
        allowed_artifacts.add("audit_requirements")
    if not set(artifacts).issubset(allowed_artifacts):
        raise GateBlock("evidence_schema_invalid", "artifact evidence contains an unknown entry")
    for artifact_id, artifact_record in artifacts.items():
        item = _require_object(artifact_record, f"artifacts.{artifact_id}")
        _require_artifact_fields(item, {"path", "filename", "sha256", "size_bytes"}, f"artifacts.{artifact_id}")
        possible_names = {PurePosixPath(str(item["path"])).name, PureWindowsPath(str(item["path"])).name}
        if (
            not _is_absolute_path_text(item["path"])
            or not _is_nonempty_string(item["filename"])
            or item["filename"] not in possible_names
            or not _is_hash(item["sha256"], 64)
            or not isinstance(item["size_bytes"], int)
            or isinstance(item["size_bytes"], bool)
            or item["size_bytes"] < 0
        ):
            raise GateBlock("evidence_schema_invalid", f"artifacts.{artifact_id} is malformed")
    if checks_by_id["package-build"]["status"] == "pass" and not {"sdist", "wheel"}.issubset(artifacts):
        raise GateBlock("evidence_cardinality_invalid", "passing package build lacks sdist or wheel evidence")
    if checks_by_id.get("pip-audit", {}).get("status") == "pass" and "audit_requirements" not in artifacts:
        raise GateBlock("evidence_cardinality_invalid", "passing pip-audit lacks requirements evidence")
    if "wheel" in artifacts:
        for check_id, check in checks_by_id.items():
            if (
                check_id.startswith("wheel-install-")
                and check["status"] == "pass"
                and check["argv"][-1] != artifacts["wheel"]["path"]
            ):
                raise GateBlock("evidence_binding_mismatch", "wheel install did not use the evidenced wheel")
    pip_audit = checks_by_id.get("pip-audit")
    if (
        pip_audit is not None
        and pip_audit["status"] == "pass"
        and pip_audit["argv"][-1] != artifacts["audit_requirements"]["path"]
    ):
        raise GateBlock("evidence_binding_mismatch", "pip-audit did not use the evidenced dependency snapshot")

    external_inputs = _require_list(record["external_inputs"], "external_inputs")
    observed_external: set[tuple[str, str, str, str]] = set()
    allowed_external: set[tuple[str, str, str, str]] = set()
    if "pip-audit" in expected_ids:
        allowed_external.add(
            ("pip-audit", "live-advisory-database", "PyPI advisory database", "live-service-unversioned")
        )
    if "semgrep" in expected_ids:
        allowed_external.update(
            ("semgrep", "live-rule-registry", locator, "live-registry-unversioned")
            for locator in ("p/python", "p/security-audit")
        )
    dependency_index = manifest["checks"]["wheel-contract"]["dependency_index"]
    allowed_external.update(
        (
            check_id,
            "live-package-index",
            dependency_index,
            "live-service-unversioned",
        )
        for check_id in expected_ids
        if check_id.startswith("wheel-install-") or check_id.startswith("pytest-wheel-")
    )
    for index, external_input in enumerate(external_inputs):
        item = _require_object(external_input, f"external_inputs[{index}]")
        _require_artifact_fields(
            item,
            {"check_id", "kind", "locator", "mutable", "identity", "observed_at"},
            f"external_inputs[{index}]",
        )
        if not all(
            isinstance(item[field], str)
            for field in ("check_id", "kind", "locator", "identity")
        ):
            raise GateBlock("evidence_schema_invalid", f"external_inputs[{index}] is malformed")
        key = (item["check_id"], item["kind"], item["locator"], item["identity"])
        if item["mutable"] is not True or not _valid_timestamp(item["observed_at"]) or key not in allowed_external:
            raise GateBlock("evidence_schema_invalid", f"external_inputs[{index}] is malformed")
        if key in observed_external:
            raise GateBlock("evidence_cardinality_invalid", "external input evidence contains duplicates")
        observed_external.add(key)
    required_external: set[tuple[str, str, str, str]] = set()
    if checks_by_id.get("pip-audit", {}).get("status") == "pass":
        required_external.update(key for key in allowed_external if key[0] == "pip-audit")
    if checks_by_id.get("semgrep", {}).get("status") == "pass":
        required_external.update(key for key in allowed_external if key[0] == "semgrep")
    required_external.update(
        key
        for key in allowed_external
        if checks_by_id.get(key[0], {}).get("status") == "pass"
        and key[1] == "live-package-index"
    )
    if not required_external.issubset(observed_external):
        raise GateBlock("evidence_cardinality_invalid", "passing live checks lack external input evidence")

    blockers = _require_list(record["blockers"], "blockers")
    blocker_ids: list[str] = []
    for index, blocker in enumerate(blockers):
        item = _require_object(blocker, f"blockers[{index}]")
        _require_artifact_fields(item, {"code", "check_id", "detail"}, f"blockers[{index}]")
        if not all(_is_nonempty_string(item[field]) for field in ("code", "check_id", "detail")):
            raise GateBlock("evidence_schema_invalid", f"blockers[{index}] is malformed")
        blocker_ids.append(item["check_id"])
    failed_ids = [check_id for check_id in expected_ids if checks_by_id[check_id]["status"] != "pass"]
    if blocker_ids != failed_ids:
        raise GateBlock("evidence_cardinality_invalid", "blockers do not match failed required checks")

    summary = _require_object(record["summary"], "summary")
    _require_artifact_fields(summary, {"required", "passed", "blocked"}, "summary")
    if not all(
        isinstance(summary[key], int) and not isinstance(summary[key], bool) and summary[key] >= 0
        for key in summary
    ):
        raise GateBlock("evidence_schema_invalid", "summary counts must be non-negative integers")
    observed_passed = sum(check["status"] == "pass" for check in checks)
    if (
        summary["required"] != len(checks)
        or summary["passed"] != observed_passed
        or summary["blocked"] != len(checks) - observed_passed
    ):
        raise GateBlock("evidence_binding_mismatch", "summary counts do not match check results")

    passed = not failed_ids
    stable = subject["clean_before"] is True and subject["clean_after"] is True and subject["head_stable"] is True
    expected_verdict = "pass" if passed and stable else "block"
    if record["verdict"] != expected_verdict:
        raise GateBlock("evidence_false_pass", "artifact verdict does not match its proofs")
    if record["verdict"] == "pass" and any(row["status"] != "pass" for row in interpreters):
        raise GateBlock("evidence_false_pass", "passing artifact lacks every required interpreter")
    return record


def write_run_evidence(path: Path, artifact: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Write one normalized run artifact and validate the bytes read back."""

    validate_run_artifact(artifact, manifest)
    payload = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        write_text(path, payload, encoding="utf-8", newline="\n")
        raw = path.read_bytes()
        loaded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateBlock("evidence_write_failed", f"cannot write and read evidence {path}: {exc}") from exc
    if raw != payload.encode("utf-8"):
        raise GateBlock("evidence_roundtrip_mismatch", "evidence bytes changed during durable write")
    validate_run_artifact(loaded, manifest)
    return sha256_bytes(raw)


def validate_preflight_artifact(artifact: Any) -> dict[str, Any]:
    """Validate the minimal evidence emitted when the full plan cannot start."""

    record = _require_object(artifact, "preflight")
    _require_artifact_fields(
        record,
        {
            "schema_version",
            "artifact_type",
            "run_id",
            "started_at",
            "finished_at",
            "verdict",
            "complete",
            "execution_scope",
            "request",
            "subject",
            "runner",
            "blocker",
        },
        "preflight",
    )
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != SCHEMA_VERSION
        or record["artifact_type"] != PREFLIGHT_ARTIFACT_TYPE
        or record["verdict"] != "block"
        or record["complete"] is not False
        or record["execution_scope"] != "preflight"
        or not _is_nonempty_string(record["run_id"])
        or not _valid_timestamp(record["started_at"])
        or not _valid_timestamp(record["finished_at"])
    ):
        raise GateBlock("evidence_schema_invalid", "preflight header is invalid")
    request = _require_object(record["request"], "preflight.request")
    _require_artifact_fields(
        request,
        {"profile", "ci_leg", "aggregate", "requested_evidence", "requested_temp_root"},
        "preflight.request",
    )
    if not _is_nonempty_string(request["profile"]) or any(
        value is not None and not isinstance(value, str)
        for key, value in request.items()
        if key != "profile"
    ):
        raise GateBlock("evidence_schema_invalid", "preflight request is invalid")
    subject = _require_object(record["subject"], "preflight.subject")
    _require_artifact_fields(
        subject,
        {"candidate_root", "candidate_sha", "candidate_tree", "clean"},
        "preflight.subject",
    )
    if not _is_absolute_path_text(subject["candidate_root"]):
        raise GateBlock("evidence_schema_invalid", "preflight candidate root is invalid")
    for field in ("candidate_sha", "candidate_tree"):
        if subject[field] is not None and not _is_hash(subject[field], 40, 64):
            raise GateBlock("evidence_schema_invalid", f"preflight {field} is invalid")
    if subject["clean"] is not None and not isinstance(subject["clean"], bool):
        raise GateBlock("evidence_schema_invalid", "preflight cleanliness is invalid")
    runner = _require_object(record["runner"], "preflight.runner")
    _require_artifact_fields(runner, {"agenttalk_version", "os", "architecture"}, "preflight.runner")
    if not all(_is_nonempty_string(runner[field]) for field in runner):
        raise GateBlock("evidence_schema_invalid", "preflight runner is invalid")
    blocker = _require_object(record["blocker"], "preflight.blocker")
    _require_artifact_fields(blocker, {"code", "detail", "check_id"}, "preflight.blocker")
    if (
        not _is_nonempty_string(blocker["code"])
        or not _is_nonempty_string(blocker["detail"])
        or (blocker["check_id"] is not None and not _is_nonempty_string(blocker["check_id"]))
    ):
        raise GateBlock("evidence_schema_invalid", "preflight blocker is invalid")
    return record


def write_preflight_block_evidence(
    *,
    root: Path,
    profile: str,
    ci_leg: str | None,
    aggregate: Path | None,
    evidence_path: Path | None,
    temp_base: Path | None,
    problem: GateBlock,
) -> tuple[Path, str, dict[str, Any]]:
    """Best-effort normalized BLOCK evidence for failures before a run artifact exists."""

    started_at = _utc_now()
    candidate_root = root.resolve()
    configured_store = os.environ.get("AGENTTALK_ROOT")
    store_root = Path(configured_store).resolve() if configured_store else None
    try:
        external_base = _ensure_external(
            (temp_base or _default_external_base(candidate_root, store_root)).resolve(),
            candidate_root,
            store_root,
            "preflight temp root",
        )
    except GateBlock:
        external_base = _default_external_base(candidate_root, store_root)
    external_base.mkdir(parents=True, exist_ok=True)
    requested_evidence = str(evidence_path.resolve()) if evidence_path is not None else None
    output_path = evidence_path or (
        external_base / f"agenttalk-dev-gate-preflight-{uuid.uuid4().hex}.json"
    )
    try:
        output_path = _external_location(
            output_path,
            candidate_root=candidate_root,
            store_root=store_root,
            label="preflight evidence path",
        )
    except GateBlock:
        output_path = external_base / f"agenttalk-dev-gate-preflight-{uuid.uuid4().hex}.json"
    try:
        binding = capture_candidate_binding(candidate_root)
    except GateBlock:
        binding = None
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": PREFLIGHT_ARTIFACT_TYPE,
        "run_id": uuid.uuid4().hex,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "verdict": "block",
        "complete": False,
        "execution_scope": "preflight",
        "request": {
            "profile": profile,
            "ci_leg": ci_leg,
            "aggregate": str(aggregate.resolve()) if aggregate is not None else None,
            "requested_evidence": requested_evidence,
            "requested_temp_root": str(temp_base.resolve()) if temp_base is not None else None,
        },
        "subject": {
            "candidate_root": str(candidate_root),
            "candidate_sha": binding.candidate_sha if binding is not None else None,
            "candidate_tree": binding.candidate_tree if binding is not None else None,
            "clean": binding.clean if binding is not None else None,
        },
        "runner": {
            "agenttalk_version": __version__,
            "os": _platform_label(),
            "architecture": platform.machine() or "unknown",
        },
        "blocker": {
            "code": problem.code,
            "detail": problem.detail,
            "check_id": problem.check_id,
        },
    }
    validate_preflight_artifact(artifact)
    payload = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        write_text(output_path, payload, encoding="utf-8", newline="\n")
        raw = output_path.read_bytes()
        loaded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateBlock("evidence_write_failed", f"cannot write preflight evidence: {exc}") from exc
    if raw != payload.encode("utf-8"):
        raise GateBlock("evidence_roundtrip_mismatch", "preflight evidence bytes changed during write")
    validate_preflight_artifact(loaded)
    return output_path, sha256_bytes(raw), artifact


def aggregate_leg_artifacts(
    manifest: dict[str, Any],
    profile: str,
    artifacts: list[dict[str, Any]],
    current_binding: CandidateBinding,
    input_sha256_by_leg: dict[str, str] | None = None,
) -> dict[str, Any]:
    validated = validate_manifest(manifest)
    expected = expected_ci_legs(validated, profile)
    validated_artifacts = [validate_run_artifact(artifact, validated) for artifact in artifacts]
    legs = [artifact["ci_leg"] for artifact in validated_artifacts]
    duplicates = sorted({leg for leg in legs if leg is not None and legs.count(leg) > 1})
    if duplicates:
        raise GateBlock("aggregate_duplicate_leg", "duplicate CI leg(s): " + ", ".join(duplicates))
    missing = sorted(set(expected) - set(legs))
    unexpected = sorted(set(legs) - set(expected), key=str)
    if missing:
        raise GateBlock("aggregate_missing_leg", "missing CI leg(s): " + ", ".join(missing))
    if unexpected:
        raise GateBlock("aggregate_unexpected_leg", "unexpected CI leg(s): " + ", ".join(map(str, unexpected)))

    binding_fields = (
        ("subject", "candidate_sha"),
        ("subject", "candidate_tree"),
        ("subject", "version"),
        ("manifest", "git_blob_id"),
        ("manifest", "sha256"),
        ("manifest", "logical_plan_sha256"),
        ("runner", "git_blob_id"),
        ("runner", "module_sha256"),
    )
    for group, field in binding_fields:
        values = {artifact[group][field] for artifact in validated_artifacts}
        if len(values) != 1:
            raise GateBlock("aggregate_binding_mismatch", f"leg {group}.{field} values differ")
    if not current_binding.clean:
        raise GateBlock("aggregate_checkout_dirty", "current checkout is not clean")
    current_values = {
        ("subject", "candidate_sha"): current_binding.candidate_sha,
        ("subject", "candidate_tree"): current_binding.candidate_tree,
        ("subject", "version"): _committed_version(current_binding.root),
        ("manifest", "git_blob_id"): current_binding.manifest_git_blob,
        ("manifest", "sha256"): current_binding.manifest_sha256,
        ("runner", "git_blob_id"): current_binding.runner_git_blob,
        ("runner", "module_sha256"): current_binding.runner_module_sha256,
    }
    for (group, field), expected_value in current_values.items():
        if validated_artifacts[0][group][field] != expected_value:
            raise GateBlock(
                "aggregate_binding_mismatch",
                f"leg {group}.{field} does not match the current checkout",
            )
    by_leg = {artifact["ci_leg"]: artifact for artifact in validated_artifacts}
    ordered = [by_leg[leg] for leg in expected]
    if input_sha256_by_leg is not None:
        if set(input_sha256_by_leg) != set(expected) or not all(
            _is_hash(value, 64) for value in input_sha256_by_leg.values()
        ):
            raise GateBlock("aggregate_input_digest_invalid", "input artifact digest map is incomplete")
    verdict = "pass" if all(artifact["verdict"] == "pass" for artifact in ordered) else "block"
    now = _utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": AGGREGATE_ARTIFACT_TYPE,
        "run_id": uuid.uuid4().hex,
        "started_at": now,
        "finished_at": now,
        "profile": profile,
        "verdict": verdict,
        "complete": True,
        "execution_scope": "ci-aggregate",
        "subject": ordered[0]["subject"],
        "manifest": ordered[0]["manifest"],
        "authority": ordered[0]["authority"],
        "legs": [
            {
                "ci_leg": artifact["ci_leg"],
                "run_id": artifact["run_id"],
                "verdict": artifact["verdict"],
                "artifact_sha256": (
                    input_sha256_by_leg[artifact["ci_leg"]]
                    if input_sha256_by_leg is not None
                    else sha256_bytes(
                        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    )
                ),
            }
            for artifact in ordered
        ],
        "blockers": [
            {"code": "ci_leg_blocked", "check_id": artifact["ci_leg"], "detail": "CI leg blocked"}
            for artifact in ordered
            if artifact["verdict"] != "pass"
        ],
    }


def _absolute_path_entries(
    path_value: str,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> list[Path]:
    """Return existing absolute PATH directories outside candidate-controlled roots."""

    roots = [Path.cwd(), *forbidden_roots]
    raw_store = os.environ.get("AGENTTALK_ROOT")
    if raw_store:
        store_root = Path(raw_store)
        if store_root.is_absolute():
            roots.append(store_root)
    result: list[Path] = []
    observed: set[str] = set()
    for raw_directory in path_value.split(os.pathsep):
        cleaned = raw_directory.strip().strip('"')
        if not cleaned:
            continue
        directory = Path(cleaned)
        if not directory.is_absolute() or not directory.is_dir():
            continue
        resolved = directory.resolve()
        if any(_is_within(resolved, root) for root in roots):
            continue
        key = os.path.normcase(str(resolved))
        if key in observed:
            continue
        observed.add(key)
        result.append(resolved)
    return result


def _sanitized_path_value(
    path_value: str,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> str:
    return os.pathsep.join(str(path) for path in _absolute_path_entries(path_value, forbidden_roots=forbidden_roots))


def _executable_on_absolute_path(
    name: str,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> Path | None:
    """Resolve only from absolute PATH entries, never the candidate CWD."""

    path_value = os.environ.get("PATH", "")
    extensions = [""]
    if os.name == "nt" and not Path(name).suffix:
        extensions.extend(
            extension.lower()
            for extension in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if extension
        )
    for directory in _absolute_path_entries(path_value, forbidden_roots=forbidden_roots):
        for extension in extensions:
            candidate = directory / (name + extension)
            if not candidate.is_file():
                continue
            if os.name != "nt" and not os.access(candidate, os.X_OK):
                continue
            return candidate.resolve()
    return None


def _git_executable(root: Path | None = None) -> Path:
    executable = _executable_on_absolute_path(
        "git",
        forbidden_roots=(() if root is None else (root,)),
    )
    if executable is None:
        raise GateBlock("git_unavailable", "git executable is unavailable on absolute PATH entries")
    return executable


def _git_environment(root: Path | None = None) -> dict[str, str]:
    env = _base_env(
        Path(tempfile.gettempdir()),
        forbidden_roots=(() if root is None else (root,)),
    )
    for key in tuple(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _gitleaks_environment(root: Path) -> dict[str, str]:
    """Give gitleaks exactly the attested Git directory for child lookup."""

    env = _git_environment(root)
    env["PATH"] = str(_git_executable(root).parent)
    return env


def _git(root: Path, args: list[str], *, binary: bool = False) -> str | bytes:
    executable = _git_executable(root)
    try:
        # Git is resolved to an absolute executable and receives a fixed argv list.
        completed = subprocess.run(  # nosec B603
            [str(executable), *args],
            cwd=root,
            env=_git_environment(root),
            capture_output=True,
            text=not binary,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateBlock("git_unavailable", f"git command failed: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr if isinstance(completed.stderr, str) else completed.stderr.decode("utf-8", "replace")
        raise GateBlock("git_failed", (stderr or "git command failed").strip()[:500])
    return completed.stdout


def _git_hash_worktree_bytes(root: Path, path: str, data: bytes) -> str:
    """Hash checkout bytes through Git's clean filters for a platform-neutral blob ID."""

    executable = _git_executable(root)
    try:
        completed = subprocess.run(  # nosec B603
            [str(executable), "hash-object", f"--path={path}", "--stdin"],
            cwd=root,
            env=_git_environment(root),
            input=data,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateBlock("git_unavailable", f"git hash-object failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GateBlock("git_failed", (detail or "git hash-object failed")[:500])
    return completed.stdout.decode("ascii", "strict").strip()


def capture_candidate_binding(root: Path, manifest_path: str = DEFAULT_MANIFEST) -> CandidateBinding:
    resolved = root.resolve()
    sha = str(_git(resolved, ["rev-parse", "HEAD"])).strip()
    tree = str(_git(resolved, ["rev-parse", "HEAD^{tree}"])).strip()
    manifest_blob = str(_git(resolved, ["rev-parse", f"HEAD:{manifest_path}"])).strip()
    manifest_bytes = _git(resolved, ["show", f"HEAD:{manifest_path}"], binary=True)
    if not isinstance(manifest_bytes, bytes):
        raise GateBlock("git_failed", "git show returned text instead of committed manifest bytes")
    runner_bytes = _git(resolved, ["show", "HEAD:src/agenttalk/dev_gate.py"], binary=True)
    if not isinstance(runner_bytes, bytes):
        raise GateBlock("git_failed", "git show returned text instead of committed runner bytes")
    runner_blob = str(_git(resolved, ["rev-parse", "HEAD:src/agenttalk/dev_gate.py"])).strip()
    status = str(_git(resolved, ["status", "--porcelain=v1", "--untracked-files=all"]))
    dirty_entries = tuple(line for line in status.splitlines() if line)
    git_dir_raw = str(_git(resolved, ["rev-parse", "--git-dir"])).strip()
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = resolved / git_dir
    markers = {
        "MERGE_HEAD": git_dir / "MERGE_HEAD",
        "CHERRY_PICK_HEAD": git_dir / "CHERRY_PICK_HEAD",
        "REVERT_HEAD": git_dir / "REVERT_HEAD",
        "rebase-merge": git_dir / "rebase-merge",
        "rebase-apply": git_dir / "rebase-apply",
    }
    in_progress = tuple(name for name, path in markers.items() if path.exists())
    return CandidateBinding(
        root=resolved,
        candidate_sha=sha,
        candidate_tree=tree,
        manifest_git_blob=manifest_blob,
        manifest_sha256=sha256_bytes(manifest_bytes),
        manifest_bytes=manifest_bytes,
        runner_git_blob=runner_blob,
        runner_module_sha256=sha256_bytes(runner_bytes),
        clean=not dirty_entries and not in_progress,
        dirty_entries=dirty_entries,
        in_progress=in_progress,
    )


@dataclass(frozen=True)
class InterpreterInfo:
    requested: str
    path: Path
    implementation: str
    version: str


@dataclass(frozen=True)
class CommandOutcome:
    argv: tuple[str, ...]
    returncode: int | None
    duration_ms: int
    status: str
    reason_code: str | None
    diagnostic: str
    log_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _platform_label() -> str:
    name = platform.system().lower()
    return {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(name, name)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _ensure_external(path: Path, candidate: Path, store_root: Path | None, label: str) -> Path:
    resolved = path.resolve()
    if _is_within(resolved, candidate):
        raise GateBlock("isolation_invalid", f"{label} must be outside the candidate worktree")
    if store_root is not None and _is_within(resolved, store_root):
        raise GateBlock("isolation_invalid", f"{label} must be outside AGENTTALK_ROOT")
    return resolved


def discover_repo_root(cwd: Path | None = None) -> Path:
    start = (cwd or Path.cwd()).resolve()
    output = str(_git(start, ["rev-parse", "--show-toplevel"])).strip()
    if not output:
        raise GateBlock("git_failed", "git did not return a repository root")
    return Path(output).resolve()


def _version_from_pyproject(text: str) -> str:
    project_match = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if project_match is None:
        raise GateBlock("version_unavailable", "pyproject.toml has no [project] table")
    versions = re.findall(r'(?m)^version\s*=\s*["\']([^"\']+)["\']\s*$', project_match.group(1))
    if len(versions) != 1:
        raise GateBlock("version_unavailable", "pyproject.toml must declare exactly one project version")
    return versions[0]


def _candidate_version(source_root: Path) -> str:
    pyproject = source_root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateBlock("version_unavailable", f"cannot read pyproject.toml: {exc}") from exc
    return _version_from_pyproject(text)


def _committed_version(root: Path) -> str:
    raw = _git(root, ["show", "HEAD:pyproject.toml"], binary=True)
    if not isinstance(raw, bytes):
        raise GateBlock("git_failed", "git show returned text for pyproject.toml")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateBlock("version_unavailable", "committed pyproject.toml is not UTF-8") from exc
    return _version_from_pyproject(text)


def load_bound_manifest(binding: CandidateBinding) -> dict[str, Any]:
    try:
        raw = json.loads(binding.manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateBlock("manifest_schema_invalid", f"committed manifest is invalid JSON: {exc}") from exc
    return validate_manifest(raw)


def _base_env(
    temp_root: Path,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> dict[str, str]:
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "USERPROFILE",
        "WINDIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PATH"] = _sanitized_path_value(
        env.get("PATH", ""),
        forbidden_roots=(temp_root, *forbidden_roots),
    )
    env.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INPUT": "1",
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "TMPDIR": str(temp_root),
        }
    )
    return env


def source_environment(base: dict[str, str], source_root: Path) -> dict[str, str]:
    env = dict(base)
    env["PYTHONPATH"] = str((source_root / "src").resolve())
    return env


def _log_tail(path: Path, limit: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    *,
    check_id: str,
    argv: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    logs_dir: Path,
) -> CommandOutcome:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / (re.sub(r"[^A-Za-z0-9_.-]+", "-", check_id) + ".log")
    started = time.monotonic()
    returncode: int | None = None
    status = "error"
    reason_code: str | None = "spawn_error"
    try:
        with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as log:
            # The caller supplies an explicit argv sequence; shell execution is disabled.
            completed = subprocess.run(  # nosec B603
                [str(item) for item in argv],
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        returncode = completed.returncode
        if returncode == 0:
            status = "pass"
            reason_code = None
        else:
            status = "fail"
            reason_code = "check_failed"
    except subprocess.TimeoutExpired:
        status = "timeout"
        reason_code = "check_timeout"
        try:
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"\nagenttalk dev-gate: timed out after {timeout_seconds}s\n")
        except OSError:
            pass
    except OSError as exc:
        status = "missing" if isinstance(exc, FileNotFoundError) else "error"
        reason_code = "required_tool_missing" if isinstance(exc, FileNotFoundError) else "spawn_error"
        try:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except OSError:
            pass
    return CommandOutcome(
        argv=tuple(str(item) for item in argv),
        returncode=returncode,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        status=status,
        reason_code=reason_code,
        diagnostic=_log_tail(log_path),
        log_path=log_path,
    )


def _record_from_outcome(
    check_id: str,
    kind: str,
    outcome: CommandOutcome,
    *,
    tool_path: str,
    tool_version: str | None,
    mode: str | None = None,
    python: str | None = None,
    import_provenance: dict[str, Any] | None = None,
    runtime_environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_hash = _sha256_file(outcome.log_path) if outcome.log_path.exists() else ""
    return {
        "id": check_id,
        "kind": kind,
        "mode": mode,
        "python": python,
        "required": True,
        "status": outcome.status,
        "argv": list(outcome.argv),
        "tool": {"path": tool_path, "version": tool_version},
        "exit_code": outcome.returncode,
        "duration_ms": outcome.duration_ms,
        "reason_code": outcome.reason_code,
        "diagnostic": outcome.diagnostic,
        "log": {"path": str(outcome.log_path), "sha256": log_hash},
        "import_provenance": import_provenance,
        "runtime_environment": runtime_environment,
    }


def _blocked_record(check_id: str, detail: str, logs_dir: Path) -> dict[str, Any]:
    kind, mode, python, _ = _check_semantics(check_id)
    log_path = logs_dir / (re.sub(r"[^A-Za-z0-9_.-]+", "-", check_id) + ".log")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path.write_text(detail + "\n", encoding="utf-8")
    return {
        "id": check_id,
        "kind": kind,
        "mode": mode,
        "python": python,
        "required": True,
        "status": "blocked_dependency",
        "argv": [],
        "tool": {"path": "", "version": None},
        "exit_code": None,
        "duration_ms": 0,
        "reason_code": "blocked_dependency",
        "diagnostic": detail,
        "log": {"path": str(log_path), "sha256": _sha256_file(log_path)},
        "import_provenance": None,
        "runtime_environment": None,
    }


def _python_from_check_id(check_id: str) -> str | None:
    match = re.search(r"-py(\d)(\d+)$", check_id)
    if match is None:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def parse_python_overrides(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if value.count("=") != 1:
            raise GateBlock("python_mapping_invalid", "--python must be <minor>=<absolute-executable>")
        minor, raw_path = value.split("=", 1)
        if minor not in set(REQUIRED_LOCAL_PYTHONS) | set(REQUIRED_CI_PYTHONS):
            raise GateBlock("python_mapping_invalid", f"unsupported Python minor {minor!r}")
        if minor in result:
            raise GateBlock("python_mapping_invalid", f"duplicate Python mapping for {minor}")
        path = Path(raw_path)
        if not path.is_absolute():
            raise GateBlock("python_mapping_invalid", f"Python mapping for {minor} must be absolute")
        result[minor] = path.resolve()
    return result


def _probe_interpreter(path: Path, requested: str, temp_root: Path, logs_dir: Path) -> InterpreterInfo:
    env = _base_env(temp_root)
    script = (
        "import json,platform,sys;"
        "print(json.dumps({'executable':sys.executable,'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),'minor':f'{sys.version_info.major}.{sys.version_info.minor}'}))"
    )
    outcome = run_command(
        check_id=f"python-probe-{_python_suffix(requested)}",
        argv=[str(path), "-c", script],
        cwd=temp_root,
        env=env,
        timeout_seconds=30,
        logs_dir=logs_dir,
    )
    if outcome.status != "pass":
        raise GateBlock("python_unavailable", outcome.diagnostic or f"cannot execute Python {requested}")
    try:
        payload = json.loads(outcome.log_path.read_text(encoding="utf-8").splitlines()[-1])
    except (OSError, IndexError, json.JSONDecodeError, TypeError) as exc:
        raise GateBlock("python_probe_invalid", f"Python {requested} returned invalid probe output") from exc
    if payload.get("minor") != requested or payload.get("implementation") != "CPython":
        raise GateBlock(
            "python_version_mismatch",
            f"requested CPython {requested}, observed {payload.get('implementation')} {payload.get('version')}",
        )
    resolved = Path(str(payload.get("executable", "")))
    if not resolved.is_absolute():
        raise GateBlock("python_probe_invalid", f"Python {requested} did not report an absolute executable")
    return InterpreterInfo(
        requested=requested,
        path=resolved.resolve(),
        implementation=str(payload["implementation"]),
        version=str(payload["version"]),
    )


def _discover_python(minor: str, temp_root: Path, logs_dir: Path) -> InterpreterInfo:
    try:
        return _probe_interpreter(Path(sys.executable), minor, temp_root, logs_dir)
    except GateBlock as exc:
        if exc.code not in {"python_version_mismatch", "python_probe_invalid", "python_unavailable"}:
            raise
    if os.name == "nt":
        launcher_path = _executable_on_absolute_path("py")
        launcher = str(launcher_path) if launcher_path is not None else None
        if launcher is None:
            raise GateBlock("python_unavailable", f"no mapping or py launcher for Python {minor}")
        locator = run_command(
            check_id=f"python-locate-{_python_suffix(minor)}",
            argv=[launcher, f"-{minor}", "-c", "import sys;print(sys.executable)"],
            cwd=temp_root,
            env=_base_env(temp_root),
            timeout_seconds=30,
            logs_dir=logs_dir,
        )
        if locator.status != "pass":
            raise GateBlock("python_unavailable", locator.diagnostic or f"Python {minor} is unavailable")
        lines = [line.strip() for line in locator.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise GateBlock("python_probe_invalid", f"py -{minor} returned no executable")
        candidate = Path(lines[-1])
    else:
        executable_path = _executable_on_absolute_path(f"python{minor}")
        executable = str(executable_path) if executable_path is not None else None
        if executable is None:
            raise GateBlock("python_unavailable", f"python{minor} is unavailable")
        candidate = Path(executable)
    return _probe_interpreter(candidate, minor, temp_root, logs_dir)


def resolve_interpreters(
    minors: Sequence[str],
    overrides: dict[str, Path],
    temp_root: Path,
    logs_dir: Path,
) -> list[InterpreterInfo]:
    result: list[InterpreterInfo] = []
    for minor in minors:
        if minor in overrides:
            result.append(_probe_interpreter(overrides[minor], minor, temp_root, logs_dir))
        else:
            result.append(_discover_python(minor, temp_root, logs_dir))
    return result


def isolated_tool_argv(
    interpreter: str | Path,
    module: str,
    *args: str,
    candidate_import_root: Path | None = None,
) -> list[str]:
    """Return a CWD-shadow-safe argv for a Python-backed gate tool."""

    import_root = "" if candidate_import_root is None else str(candidate_import_root.resolve())
    return [
        str(interpreter),
        "-I",
        "-c",
        _ISOLATED_TOOL_LAUNCHER,
        module,
        import_root,
        *args,
    ]


def _create_isolated_venv(
    *,
    creator: InterpreterInfo,
    root: Path,
    role: str,
    logs_dir: Path,
) -> tuple[InterpreterInfo, dict[str, Any]]:
    """Create and prove a fresh no-system-site-packages venv."""

    if role not in {"runtime", "test"}:
        raise GateBlock("wheel_environment_invalid", f"unknown wheel environment role {role!r}")
    create = run_command(
        check_id=f"venv-create-{role}-{_python_suffix(creator.requested)}",
        argv=isolated_tool_argv(creator.path, "venv", "--clear", "--copies", str(root)),
        cwd=root.parent,
        env=_base_env(root.parent),
        timeout_seconds=300,
        logs_dir=logs_dir,
    )
    if create.status != "pass":
        raise GateBlock(
            "wheel_environment_create_failed",
            create.diagnostic or f"cannot create {role} venv for Python {creator.requested}",
        )
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    config = root / "pyvenv.cfg"
    try:
        config_text = config.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateBlock("wheel_environment_invalid", f"cannot read {config}: {exc}") from exc
    if not re.search(r"(?im)^include-system-site-packages\s*=\s*false\s*$", config_text):
        raise GateBlock("wheel_environment_invalid", f"{role} venv enables system site-packages")
    script = (
        "import json,platform,sys;"
        "print(json.dumps({'executable':sys.executable,'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),'minor':f'{sys.version_info.major}.{sys.version_info.minor}',"
        "'prefix':sys.prefix,'base_prefix':sys.base_prefix}))"
    )
    probe = run_command(
        check_id=f"venv-probe-{role}-{_python_suffix(creator.requested)}",
        argv=[str(python), "-I", "-c", script],
        cwd=root.parent,
        env=_base_env(root.parent),
        timeout_seconds=30,
        logs_dir=logs_dir,
    )
    if probe.status != "pass":
        raise GateBlock("wheel_environment_invalid", probe.diagnostic or f"cannot probe {role} venv")
    try:
        payload = json.loads(probe.log_path.read_text(encoding="utf-8").splitlines()[-1])
    except (OSError, IndexError, json.JSONDecodeError, TypeError) as exc:
        raise GateBlock("wheel_environment_invalid", f"{role} venv returned invalid proof") from exc
    observed_python = Path(str(payload.get("executable", ""))).resolve()
    observed_prefix = Path(str(payload.get("prefix", ""))).resolve()
    observed_base = Path(str(payload.get("base_prefix", ""))).resolve()
    if (
        payload.get("minor") != creator.requested
        or payload.get("implementation") != "CPython"
        or observed_python != python.resolve()
        or observed_prefix != root.resolve()
        or observed_base == observed_prefix
    ):
        raise GateBlock("wheel_environment_invalid", f"{role} venv proof does not match its creator")
    info = InterpreterInfo(
        requested=creator.requested,
        path=observed_python,
        implementation="CPython",
        version=str(payload["version"]),
    )
    proof = {
        "role": role,
        "requested": creator.requested,
        "creator_path": str(creator.path),
        "python_path": str(observed_python),
        "prefix": str(observed_prefix),
        "base_prefix": str(observed_base),
        "system_site_packages": False,
    }
    return info, proof


def _probe_python_module(interpreter: InterpreterInfo, module: str, temp_root: Path, logs_dir: Path) -> str:
    outcome = run_command(
        check_id=f"tool-probe-{module.replace('_', '-')}",
        argv=isolated_tool_argv(interpreter.path, module, "--version"),
        cwd=temp_root,
        env=_base_env(temp_root),
        timeout_seconds=60,
        logs_dir=logs_dir,
    )
    if outcome.status != "pass":
        raise GateBlock("required_tool_missing", f"{module} is unavailable: {outcome.diagnostic}")
    lines = [line.strip() for line in outcome.log_path.read_text(encoding="utf-8", errors="replace").splitlines()]
    return next((line for line in lines if line), "unknown")[:200]


def _probe_executable(name: str, temp_root: Path, logs_dir: Path) -> tuple[Path, str]:
    executable = _executable_on_absolute_path(name)
    if executable is None:
        raise GateBlock(
            "required_tool_missing",
            f"{name} executable is unavailable on absolute PATH entries",
        )
    outcome = run_command(
        check_id=f"tool-probe-{name}",
        argv=[str(executable), "--version" if name != "gitleaks" else "version"],
        cwd=temp_root,
        env=_base_env(temp_root),
        timeout_seconds=60,
        logs_dir=logs_dir,
    )
    if outcome.status != "pass":
        raise GateBlock("required_tool_missing", f"cannot probe {name}: {outcome.diagnostic}")
    lines = [line.strip() for line in outcome.log_path.read_text(encoding="utf-8", errors="replace").splitlines()]
    return executable, next((line for line in lines if line), "unknown")[:200]


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            member = PurePosixPath(info.filename)
            if (
                member.is_absolute()
                or ".." in member.parts
                or "\\" in info.filename
                or (member.parts and ":" in member.parts[0])
            ):
                raise GateBlock("candidate_export_invalid", f"unsafe archive member {info.filename!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise GateBlock("candidate_export_invalid", f"symlink archive member is not supported: {info.filename}")
            target = destination.joinpath(*member.parts)
            if not _is_within(target, destination):
                raise GateBlock("candidate_export_invalid", f"unsafe archive member {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def export_candidate(binding: CandidateBinding, destination: Path, archive_path: Path) -> None:
    git = _git_executable(binding.root)
    try:
        # Git is resolved to an absolute executable and receives a fixed archive argv.
        completed = subprocess.run(  # nosec B603
            [str(git), "archive", "--format=zip", "--output", str(archive_path), binding.candidate_sha],
            cwd=binding.root,
            env=_git_environment(binding.root),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateBlock("candidate_export_failed", str(exc)) from exc
    if completed.returncode != 0:
        raise GateBlock("candidate_export_failed", (completed.stderr or completed.stdout or "git archive failed")[:500])
    _safe_extract_zip(archive_path, destination)


def _parse_import_probe(path: Path) -> dict[str, Any]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        payload = json.loads(lines[-1])
    except (OSError, IndexError, json.JSONDecodeError, TypeError) as exc:
        raise GateBlock("import_probe_invalid", "import probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GateBlock("import_probe_invalid", "import probe payload must be an object")
    return payload


def import_probe(
    *,
    interpreter: InterpreterInfo,
    expected_root: Path,
    cwd: Path,
    env: dict[str, str],
    temp_root: Path,
    logs_dir: Path,
    expected_version: str,
    label: str,
    candidate_import_root: Path | None,
) -> dict[str, Any]:
    del temp_root
    script = (
        "import json,pathlib,sys;"
        "candidate_import_root=sys.argv.pop(1);"
        "candidate_import_root and sys.path.insert(0,candidate_import_root);"
        "import agenttalk;"
        "print(json.dumps({'path':str(pathlib.Path(agenttalk.__file__).resolve()),"
        "'version':agenttalk.__version__}))"
    )
    outcome = run_command(
        check_id=f"import-probe-{label}",
        argv=[
            str(interpreter.path),
            "-I",
            "-c",
            script,
            "" if candidate_import_root is None else str(candidate_import_root.resolve()),
        ],
        cwd=cwd,
        env=env,
        timeout_seconds=60,
        logs_dir=logs_dir,
    )
    if outcome.status != "pass":
        raise GateBlock("import_probe_failed", outcome.diagnostic or f"{label} import probe failed")
    payload = _parse_import_probe(outcome.log_path)
    observed_path = Path(str(payload.get("path", "")))
    if not observed_path.is_absolute() or not _is_within(observed_path, expected_root):
        raise GateBlock(
            "import_provenance_mismatch",
            f"{label} imported {observed_path}, expected under {expected_root}",
        )
    if payload.get("version") != expected_version:
        raise GateBlock(
            "version_mismatch",
            f"{label} imported version {payload.get('version')!r}, expected {expected_version!r}",
        )
    return {
        "expected_root": str(expected_root.resolve()),
        "observed_path": str(observed_path.resolve()),
        "version": str(payload["version"]),
    }


SDIST_SENTINELS = (
    ".tmp/agenttalk-sdist-sentinel.txt",
    ".pytest-cache-local/agenttalk-sdist-sentinel.txt",
    "HANDOFF-agenttalk-sdist-sentinel.md",
    "agenttalk-local-sdist-sentinel.md",
    "dogfood-test-plan.html",
    "docs/local-feedback-sentinel.zip",
)


def _change_record_failure(record: dict[str, Any], code: str, detail: str) -> dict[str, Any]:
    changed = dict(record)
    changed["status"] = "fail"
    changed["reason_code"] = code
    changed["diagnostic"] = detail[-2000:]
    if changed.get("exit_code") == 0:
        changed["exit_code"] = 1
    return changed


def _write_packaging_sentinels(package_root: Path) -> None:
    for relative in SDIST_SENTINELS:
        path = package_root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not ship\n", encoding="utf-8")


def _sdist_contract(sdist: Path, required_paths: Sequence[str]) -> list[str]:
    problems: list[str] = []
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            names = [PurePosixPath(member.name).as_posix() for member in archive.getmembers()]
    except (OSError, tarfile.TarError) as exc:
        return [f"cannot inspect sdist: {exc}"]
    for sentinel in SDIST_SENTINELS:
        if any(name == sentinel or name.endswith("/" + sentinel) for name in names):
            problems.append(f"sdist contains forbidden sentinel {sentinel}")
    for required in required_paths:
        if not any(name == required or name.endswith("/" + required) for name in names):
            problems.append(f"sdist is missing {required}")
    return problems


def _wheel_archive_contract(wheel: Path, required_resources: Sequence[str]) -> list[str]:
    problems: list[str] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"cannot inspect wheel: {exc}"]
    for required in required_resources:
        package_relative = required.removeprefix("agenttalk/")
        archive_path = f"agenttalk/{package_relative}"
        if archive_path not in names:
            problems.append(f"wheel is missing {archive_path}")
    return problems


def run_package_build(
    *,
    interpreter: InterpreterInfo,
    package_root: Path,
    out_dir: Path,
    env: dict[str, str],
    manifest: dict[str, Any],
    logs_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path | None]:
    check_id = "package-build"
    spec = manifest["checks"][check_id]
    _write_packaging_sentinels(package_root)
    try:
        tool_version = _probe_python_module(interpreter, "build", package_root, logs_dir)
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir), {}, None
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = isolated_tool_argv(
        interpreter.path,
        "build",
        "--no-isolation",
        "--sdist",
        "--wheel",
        "--outdir",
        str(out_dir),
    )
    outcome = run_command(
        check_id=check_id,
        argv=argv,
        cwd=package_root,
        env=env,
        timeout_seconds=int(spec["timeout_seconds"]),
        logs_dir=logs_dir,
    )
    record = _record_from_outcome(
        check_id,
        "python-build",
        outcome,
        tool_path=str(interpreter.path),
        tool_version=tool_version,
    )
    if outcome.status != "pass":
        return record, {}, None
    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return (
            _change_record_failure(
                record,
                "package_artifact_cardinality",
                "build must produce one wheel and one sdist",
            ),
            {},
            None,
        )
    problems = _sdist_contract(sdists[0], spec.get("required_sdist_paths") or [])
    problems.extend(
        _wheel_archive_contract(
            wheels[0],
            manifest["checks"]["wheel-contract"]["required_wheel_resources"],
        )
    )
    artifacts = {
        "sdist": {
            "path": str(sdists[0]),
            "filename": sdists[0].name,
            "sha256": _sha256_file(sdists[0]),
            "size_bytes": sdists[0].stat().st_size,
        },
        "wheel": {
            "path": str(wheels[0]),
            "filename": wheels[0].name,
            "sha256": _sha256_file(wheels[0]),
            "size_bytes": wheels[0].stat().st_size,
        },
    }
    if problems:
        record = _change_record_failure(record, "package_contract_failed", "; ".join(problems))
    return record, artifacts, wheels[0]


def _run_pytest_mode(
    *,
    mode: str,
    interpreter: InterpreterInfo,
    source_root: Path,
    import_root: Path,
    env: dict[str, str],
    expected_version: str,
    manifest: dict[str, Any],
    basetemp: Path,
    logs_dir: Path,
    runtime_environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check_id = f"pytest-{mode}-{_python_suffix(interpreter.requested)}"
    spec = manifest["checks"]["pytest"]
    try:
        tool_version = _probe_python_module(interpreter, "pytest", source_root, logs_dir)
        provenance = import_probe(
            interpreter=interpreter,
            expected_root=import_root,
            cwd=source_root,
            env=env,
            temp_root=basetemp.parent,
            logs_dir=logs_dir,
            expected_version=expected_version,
            label=f"{mode}-{_python_suffix(interpreter.requested)}",
            candidate_import_root=import_root if mode == "source" else None,
        )
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir)
    argv = isolated_tool_argv(
        interpreter.path,
        "pytest",
        *spec.get("args", []),
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(basetemp),
        *spec.get("paths", []),
        candidate_import_root=import_root if mode == "source" else None,
    )
    outcome = run_command(
        check_id=check_id,
        argv=argv,
        cwd=source_root,
        env=env,
        timeout_seconds=int(spec["timeout_seconds"]),
        logs_dir=logs_dir,
    )
    return _record_from_outcome(
        check_id,
        "pytest",
        outcome,
        tool_path=str(interpreter.path),
        tool_version=tool_version,
        mode=mode,
        python=interpreter.requested,
        import_provenance=provenance,
        runtime_environment=runtime_environment,
    )


def _install_wheel(
    *,
    interpreter: InterpreterInfo,
    runtime_environment: dict[str, Any],
    wheel: Path | None,
    source_root: Path,
    env: dict[str, str],
    manifest: dict[str, Any],
    logs_dir: Path,
) -> dict[str, Any]:
    check_id = f"wheel-install-{_python_suffix(interpreter.requested)}"
    if wheel is None:
        return _blocked_record(check_id, "package build did not produce a wheel", logs_dir)
    probe = run_command(
        check_id=f"tool-probe-pip-{_python_suffix(interpreter.requested)}",
        argv=isolated_tool_argv(interpreter.path, "pip", "--version"),
        cwd=source_root,
        env=env,
        timeout_seconds=60,
        logs_dir=logs_dir,
    )
    if probe.status != "pass":
        return _blocked_record(check_id, "pip is unavailable for wheel installation", logs_dir)
    tool_version = next(
        (line for line in probe.log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()),
        "unknown",
    )
    index = manifest["checks"]["wheel-contract"]["dependency_index"]
    outcome = run_command(
        check_id=check_id,
        argv=isolated_tool_argv(
            interpreter.path,
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--index-url",
            index,
            str(wheel),
        ),
        cwd=source_root,
        env=env,
        timeout_seconds=int(manifest["checks"]["wheel-contract"]["timeout_seconds"]),
        logs_dir=logs_dir,
    )
    return _record_from_outcome(
        check_id,
        "wheel-install",
        outcome,
        tool_path=str(interpreter.path),
        tool_version=tool_version[:200],
        mode="wheel",
        python=interpreter.requested,
        runtime_environment=runtime_environment,
    )


def _wheel_dependency_check(
    *,
    interpreter: InterpreterInfo,
    runtime_environment: dict[str, Any],
    source_root: Path,
    env: dict[str, str],
    install_record: dict[str, Any],
    timeout_seconds: int,
    logs_dir: Path,
) -> dict[str, Any]:
    check_id = f"wheel-dependency-check-{_python_suffix(interpreter.requested)}"
    if install_record["status"] != "pass":
        return _blocked_record(check_id, "wheel installation failed", logs_dir)
    outcome = run_command(
        check_id=check_id,
        argv=isolated_tool_argv(interpreter.path, "pip", "check"),
        cwd=source_root,
        env=env,
        timeout_seconds=timeout_seconds,
        logs_dir=logs_dir,
    )
    return _record_from_outcome(
        check_id,
        "wheel-dependency-check",
        outcome,
        tool_path=str(interpreter.path),
        tool_version=interpreter.version,
        mode="wheel",
        python=interpreter.requested,
        runtime_environment=runtime_environment,
    )


def _wheel_contract(
    *,
    interpreter: InterpreterInfo,
    runtime_environment: dict[str, Any],
    source_root: Path,
    env: dict[str, str],
    expected_version: str,
    manifest: dict[str, Any],
    install_record: dict[str, Any],
    logs_dir: Path,
) -> dict[str, Any]:
    check_id = f"wheel-contract-{_python_suffix(interpreter.requested)}"
    if install_record["status"] != "pass":
        return _blocked_record(check_id, "wheel installation failed", logs_dir)
    try:
        provenance = import_probe(
            interpreter=interpreter,
            expected_root=Path(runtime_environment["prefix"]),
            cwd=source_root,
            env=env,
            temp_root=source_root.parent,
            logs_dir=logs_dir,
            expected_version=expected_version,
            label=f"wheel-contract-{_python_suffix(interpreter.requested)}",
            candidate_import_root=None,
        )
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir)
    problems: list[str] = []
    installed_package = Path(provenance["observed_path"]).parent
    for resource in manifest["checks"]["wheel-contract"]["required_wheel_resources"]:
        relative = resource.removeprefix("agenttalk/")
        source = source_root / "src" / "agenttalk" / relative
        installed = installed_package / relative
        if not source.is_file() or not installed.is_file():
            problems.append(f"missing required resource {resource}")
        elif source.read_bytes() != installed.read_bytes():
            problems.append(f"installed resource differs from source: {resource}")
    outcome = run_command(
        check_id=check_id,
        argv=isolated_tool_argv(
            interpreter.path,
            "agenttalk",
            "--version",
        ),
        cwd=source_root,
        env=env,
        timeout_seconds=int(manifest["checks"]["wheel-contract"]["timeout_seconds"]),
        logs_dir=logs_dir,
    )
    record = _record_from_outcome(
        check_id,
        "wheel-contract",
        outcome,
        tool_path=str(interpreter.path),
        tool_version=interpreter.version,
        mode="wheel",
        python=interpreter.requested,
        import_provenance=provenance,
        runtime_environment=runtime_environment,
    )
    if problems:
        record = _change_record_failure(record, "wheel_resource_mismatch", "; ".join(problems))
    return record


def _prepare_wheel_test_environment(
    *,
    creator: InterpreterInfo,
    wheel: Path | None,
    root: Path,
    source_root: Path,
    env: dict[str, str],
    manifest: dict[str, Any],
    logs_dir: Path,
) -> tuple[InterpreterInfo, dict[str, Any]]:
    if wheel is None:
        raise GateBlock("wheel_test_environment_failed", "package build did not produce a wheel")
    interpreter, proof = _create_isolated_venv(
        creator=creator,
        root=root,
        role="test",
        logs_dir=logs_dir,
    )
    index = manifest["checks"]["wheel-contract"]["dependency_index"]
    for label, requirement in (
        ("candidate", str(wheel)),
        ("pytest", manifest["checks"]["pytest"]["test_requirement"]),
    ):
        outcome = run_command(
            check_id=f"wheel-test-install-{label}-{_python_suffix(creator.requested)}",
            argv=isolated_tool_argv(
                interpreter.path,
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-cache-dir",
                "--index-url",
                index,
                requirement,
            ),
            cwd=source_root,
            env=env,
            timeout_seconds=int(manifest["checks"]["wheel-contract"]["timeout_seconds"]),
            logs_dir=logs_dir,
        )
        if outcome.status != "pass":
            raise GateBlock(
                "wheel_test_environment_failed",
                outcome.diagnostic or f"cannot install {label} into isolated wheel test environment",
            )
    consistency = run_command(
        check_id=f"wheel-test-pip-check-{_python_suffix(creator.requested)}",
        argv=isolated_tool_argv(interpreter.path, "pip", "check"),
        cwd=source_root,
        env=env,
        timeout_seconds=int(manifest["checks"]["wheel-contract"]["timeout_seconds"]),
        logs_dir=logs_dir,
    )
    if consistency.status != "pass":
        raise GateBlock(
            "wheel_test_environment_failed",
            consistency.diagnostic or "isolated wheel test environment has dependency conflicts",
        )
    return interpreter, proof


def _runtime_dependency_snapshot(
    *,
    interpreter: InterpreterInfo,
    source_root: Path,
    env: dict[str, str],
    output: Path,
    timeout_seconds: int,
    logs_dir: Path,
) -> Path:
    outcome = run_command(
        check_id=f"wheel-freeze-{_python_suffix(interpreter.requested)}",
        argv=isolated_tool_argv(interpreter.path, "pip", "freeze", "--exclude-editable"),
        cwd=source_root,
        env=env,
        timeout_seconds=timeout_seconds,
        logs_dir=logs_dir,
    )
    if outcome.status != "pass":
        raise GateBlock("wheel_dependency_snapshot_failed", outcome.diagnostic or "pip freeze failed")
    try:
        lines = outcome.log_path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise GateBlock("wheel_dependency_snapshot_failed", f"cannot read dependency snapshot: {exc}") from exc
    filtered = [
        line
        for line in lines
        if not re.match(r"(?i)^agenttalk(?:==|\s*@\s*)", line.strip())
    ]
    write_text(output, "\n".join(filtered) + ("\n" if filtered else ""), encoding="utf-8", newline="\n")
    return output


def _python_module_check(
    *,
    check_id: str,
    module: str,
    argv: Sequence[str],
    interpreter: InterpreterInfo,
    source_root: Path,
    env: dict[str, str],
    timeout_seconds: int,
    logs_dir: Path,
) -> dict[str, Any]:
    try:
        version = _probe_python_module(interpreter, module, source_root, logs_dir)
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir)
    outcome = run_command(
        check_id=check_id,
        argv=isolated_tool_argv(interpreter.path, module, *argv),
        cwd=source_root,
        env=env,
        timeout_seconds=timeout_seconds,
        logs_dir=logs_dir,
    )
    return _record_from_outcome(
        check_id,
        check_id,
        outcome,
        tool_path=str(interpreter.path),
        tool_version=version,
    )


def _executable_check(
    *,
    check_id: str,
    executable_name: str,
    argv: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    logs_dir: Path,
) -> dict[str, Any]:
    try:
        executable, version = _probe_executable(executable_name, cwd, logs_dir)
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir)
    outcome = run_command(
        check_id=check_id,
        argv=[str(executable), *argv],
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        logs_dir=logs_dir,
    )
    return _record_from_outcome(
        check_id,
        check_id,
        outcome,
        tool_path=str(executable),
        tool_version=version,
    )


def _pip_audit_check(
    *,
    interpreter: InterpreterInfo,
    source_root: Path,
    env: dict[str, str],
    manifest: dict[str, Any],
    requirements: Path | None,
    logs_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    check_id = "pip-audit"
    if requirements is None or not requirements.is_file():
        return _blocked_record(
            check_id,
            "resolved wheel dependency snapshot is unavailable",
            logs_dir,
        ), None
    try:
        version = _probe_python_module(interpreter, "pip_audit", source_root, logs_dir)
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir), None
    spec = manifest["checks"][check_id]
    outcome = run_command(
        check_id=check_id,
        argv=isolated_tool_argv(
            interpreter.path,
            "pip_audit",
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--requirement",
            str(requirements),
        ),
        cwd=source_root,
        env=env,
        timeout_seconds=int(spec["timeout_seconds"]),
        logs_dir=logs_dir,
    )
    record = _record_from_outcome(
        check_id,
        check_id,
        outcome,
        tool_path=str(interpreter.path),
        tool_version=version,
    )
    artifact = {
        "path": str(requirements.resolve()),
        "filename": requirements.name,
        "sha256": _sha256_file(requirements),
        "size_bytes": requirements.stat().st_size,
    }
    return record, artifact


def run_static_checks(
    *,
    interpreter: InterpreterInfo,
    source_root: Path,
    candidate_root: Path,
    env: dict[str, str],
    manifest: dict[str, Any],
    required_ids: set[str],
    audit_requirements: Path | None,
    logs_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    records: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    external_inputs: list[dict[str, Any]] = []
    if "ruff" in required_ids:
        spec = manifest["checks"]["ruff"]
        records["ruff"] = _python_module_check(
            check_id="ruff",
            module="ruff",
            argv=["check", "--no-cache", *spec["paths"]],
            interpreter=interpreter,
            source_root=source_root,
            env=env,
            timeout_seconds=int(spec["timeout_seconds"]),
            logs_dir=logs_dir,
        )
    if "bandit" in required_ids:
        spec = manifest["checks"]["bandit"]
        records["bandit"] = _python_module_check(
            check_id="bandit",
            module="bandit",
            argv=["-r", *spec["paths"], "-x", ",".join(spec["exclude"])],
            interpreter=interpreter,
            source_root=source_root,
            env=env,
            timeout_seconds=int(spec["timeout_seconds"]),
            logs_dir=logs_dir,
        )
    if "gitleaks" in required_ids:
        spec = manifest["checks"]["gitleaks"]
        shallow = str(_git(candidate_root, ["rev-parse", "--is-shallow-repository"])).strip()
        if spec["require_full_history"] and shallow != "false":
            records["gitleaks"] = _blocked_record(
                "gitleaks", "full Git history is required for gitleaks", logs_dir
            )
        else:
            records["gitleaks"] = _executable_check(
                check_id="gitleaks",
                executable_name="gitleaks",
                argv=[
                    "git",
                    "--config",
                    str((source_root / spec["config"]).resolve()),
                    "--log-opts=--all",
                    "--redact",
                    "--no-color",
                    "--no-banner",
                    str(candidate_root.resolve()),
                ],
                cwd=candidate_root,
                env=_gitleaks_environment(candidate_root),
                timeout_seconds=int(spec["timeout_seconds"]),
                logs_dir=logs_dir,
            )
    if "pip-audit" in required_ids:
        record, artifact = _pip_audit_check(
            interpreter=interpreter,
            source_root=source_root,
            env=env,
            manifest=manifest,
            requirements=audit_requirements,
            logs_dir=logs_dir,
        )
        records["pip-audit"] = record
        if artifact is not None:
            artifacts["audit_requirements"] = artifact
        external_inputs.append(
            {
                "check_id": "pip-audit",
                "kind": "live-advisory-database",
                "locator": "PyPI advisory database",
                "mutable": True,
                "identity": "live-service-unversioned",
                "observed_at": _utc_now(),
            }
        )
    if "semgrep" in required_ids:
        spec = manifest["checks"]["semgrep"]
        argv = ["scan"]
        for config in spec["configs"]:
            argv.append(f"--config={config}")
            if config.startswith("p/"):
                external_inputs.append(
                    {
                        "check_id": "semgrep",
                        "kind": "live-rule-registry",
                        "locator": config,
                        "mutable": True,
                        "identity": "live-registry-unversioned",
                        "observed_at": _utc_now(),
                    }
                )
        argv.extend(["--error", "--timeout", str(spec["rule_timeout_seconds"])])
        records["semgrep"] = _executable_check(
            check_id="semgrep",
            executable_name="semgrep",
            argv=argv,
            cwd=source_root,
            env=env,
            timeout_seconds=int(spec["timeout_seconds"]),
            logs_dir=logs_dir,
        )
    if "zizmor" in required_ids:
        spec = manifest["checks"]["zizmor"]
        records["zizmor"] = _executable_check(
            check_id="zizmor",
            executable_name="zizmor",
            argv=spec["paths"],
            cwd=source_root,
            env=env,
            timeout_seconds=int(spec["timeout_seconds"]),
            logs_dir=logs_dir,
        )
    return records, artifacts, external_inputs


def _synthetic_record(
    *,
    check_id: str,
    status: str,
    reason_code: str | None,
    diagnostic: str,
    logs_dir: Path,
    argv: Sequence[str],
    tool_path: str,
    tool_version: str,
) -> dict[str, Any]:
    log_path = logs_dir / f"{check_id}.log"
    logs_dir.mkdir(parents=True, exist_ok=True)
    write_text(log_path, diagnostic + ("\n" if diagnostic else ""), encoding="utf-8", newline="\n")
    return {
        "id": check_id,
        "kind": check_id,
        "mode": None,
        "python": None,
        "required": True,
        "status": status,
        "argv": list(argv),
        "tool": {"path": tool_path, "version": tool_version},
        "exit_code": 0 if status == "pass" else 1,
        "duration_ms": 0,
        "reason_code": reason_code,
        "diagnostic": diagnostic,
        "log": {"path": str(log_path.resolve()), "sha256": _sha256_file(log_path)},
        "import_provenance": None,
        "runtime_environment": None,
    }


def _binding_record(
    check_id: str,
    binding: CandidateBinding,
    *,
    matches: bool,
    detail: str,
    logs_dir: Path,
) -> dict[str, Any]:
    try:
        git, version = _probe_executable("git", binding.root, logs_dir)
    except GateBlock as exc:
        return _blocked_record(check_id, exc.detail, logs_dir)
    status = "pass" if binding.clean and matches else "fail"
    diagnostic = detail
    if not binding.clean:
        dirty = list(binding.dirty_entries) + list(binding.in_progress)
        diagnostic = "candidate worktree is not clean: " + "; ".join(dirty[:20])
    return _synthetic_record(
        check_id=check_id,
        status=status,
        reason_code=None if status == "pass" else "candidate_binding_changed",
        diagnostic=diagnostic,
        logs_dir=logs_dir,
        argv=[str(git), "status", "--porcelain=v1", "--untracked-files=all"],
        tool_path=str(git),
        tool_version=version,
    )


def _same_binding(before: CandidateBinding, after: CandidateBinding) -> bool:
    return (
        before.candidate_sha == after.candidate_sha
        and before.candidate_tree == after.candidate_tree
        and before.manifest_git_blob == after.manifest_git_blob
        and before.manifest_sha256 == after.manifest_sha256
        and before.runner_git_blob == after.runner_git_blob
        and before.runner_module_sha256 == after.runner_module_sha256
    )


def _interpreter_row(
    requested: str, info: InterpreterInfo | None, problem: GateBlock | None = None
) -> dict[str, Any]:
    if info is None:
        return {
            "requested": requested,
            "path": "",
            "implementation": "",
            "version": problem.detail if problem is not None else "unavailable",
            "status": "missing",
        }
    return {
        "requested": requested,
        "path": str(info.path),
        "implementation": info.implementation,
        "version": info.version,
        "status": "pass",
    }


def _blockers_for_checks(checks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "code": check["reason_code"] or check["status"],
            "check_id": check["id"],
            "detail": check["diagnostic"] or f"{check['id']} did not pass",
        }
        for check in checks
        if check["status"] != "pass"
    ]


@dataclass(frozen=True)
class GateRunResult:
    exit_code: int
    evidence_path: Path
    evidence_sha256: str
    artifact: dict[str, Any]
    run_root: Path | None = None


def _external_location(
    path: Path,
    *,
    candidate_root: Path,
    store_root: Path | None,
    label: str,
) -> Path:
    resolved = _ensure_external(path, candidate_root, store_root, label)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _default_external_base(candidate_root: Path, store_root: Path | None) -> Path:
    base = Path(tempfile.gettempdir()).resolve()
    return _ensure_external(base, candidate_root, store_root, "system temp directory")


def _requested_minors(
    manifest: dict[str, Any], profile: str, ci_leg: str | None
) -> tuple[str, str | None, list[str]]:
    if ci_leg is None:
        return "local", None, list(manifest["profiles"][profile]["local"]["python_minors"])
    leg = parse_ci_leg(ci_leg, manifest, profile)
    return "ci-leg", leg, [leg.split("/", 1)[1]]


def _module_blob_sha256(binding: CandidateBinding) -> str:
    return binding.runner_module_sha256


def _export_phase(binding: CandidateBinding, run_root: Path, phase: str) -> Path:
    if phase not in {"source", "package", "wheel", "static"}:
        raise GateBlock("phase_invalid", f"unknown gate phase {phase!r}")
    destination = run_root / f"candidate-{phase}"
    archive = run_root / f"candidate-{phase}.zip"
    export_candidate(binding, destination, archive)
    return destination


def execute_gate(
    *,
    root: Path,
    profile: str = DEFAULT_PROFILE,
    ci_leg: str | None = None,
    evidence_path: Path | None = None,
    temp_base: Path | None = None,
    python_overrides: dict[str, Path] | None = None,
) -> GateRunResult:
    """Execute one local precheck or one explicitly named CI matrix leg."""

    started_at = _utc_now()
    run_id = uuid.uuid4().hex
    candidate_root = root.resolve()
    binding = capture_candidate_binding(candidate_root)
    manifest = load_bound_manifest(binding)
    if profile not in manifest["profiles"]:
        raise GateBlock("profile_unknown", f"unknown profile {profile!r}")
    scope, normalized_leg, minors = _requested_minors(manifest, profile, ci_leg)
    if normalized_leg is not None:
        expected_os = normalized_leg.split("/", 1)[0]
        observed_os = _platform_label()
        if expected_os != observed_os:
            raise GateBlock(
                "ci_leg_platform_mismatch",
                f"declared leg {normalized_leg} is running on {observed_os}",
            )

    configured_store = os.environ.get("AGENTTALK_ROOT")
    store_root = Path(configured_store).resolve() if configured_store else None
    external_base = _ensure_external(
        (temp_base or _default_external_base(candidate_root, store_root)).resolve(),
        candidate_root,
        store_root,
        "gate temp root",
    )
    external_base.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="agenttalk-dev-gate-", dir=str(external_base))).resolve()
    _ensure_external(run_root, candidate_root, store_root, "gate run directory")
    logs_dir = run_root / "logs"
    dist_root = run_root / "dist"
    output_path = evidence_path
    if output_path is None:
        output_path = external_base / f"agenttalk-dev-gate-{binding.candidate_sha[:12]}-{run_id}.json"
    output_path = _external_location(
        output_path,
        candidate_root=candidate_root,
        store_root=store_root,
        label="evidence path",
    )

    version = _committed_version(candidate_root)
    required_ids = required_check_ids(
        manifest,
        profile,
        execution_scope=scope,
        ci_leg=normalized_leg,
    )
    required_set = set(required_ids)
    checks_by_id: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    external_inputs: list[dict[str, Any]] = []
    checks_by_id["git-binding"] = _binding_record(
        "git-binding",
        binding,
        matches=True,
        detail="candidate SHA, tree, committed manifest/runner, and clean worktree captured",
        logs_dir=logs_dir,
    )

    interpreters: dict[str, InterpreterInfo] = {}
    interpreter_rows: list[dict[str, Any]] = []
    overrides = python_overrides or {}
    if binding.clean:
        for minor in minors:
            try:
                info = resolve_interpreters([minor], overrides, run_root, logs_dir)[0]
            except GateBlock as exc:
                interpreter_rows.append(_interpreter_row(minor, None, exc))
            else:
                interpreters[minor] = info
                interpreter_rows.append(_interpreter_row(minor, info))
    else:
        problem = GateBlock("candidate_dirty", "candidate worktree is not clean")
        interpreter_rows.extend(_interpreter_row(minor, None, problem) for minor in minors)

    base_env = _base_env(run_root)
    execution_problem: str | None = None
    if not binding.clean:
        execution_problem = "candidate worktree must be clean before any gate check runs"

    if execution_problem is None:
        try:
            source_root = _export_phase(binding, run_root, "source")
            source_env = source_environment(base_env, source_root)
            for minor in minors:
                interpreter = interpreters.get(minor)
                if interpreter is None:
                    continue
                check_id = f"pytest-source-{_python_suffix(minor)}"
                checks_by_id[check_id] = _run_pytest_mode(
                    mode="source",
                    interpreter=interpreter,
                    source_root=source_root,
                    import_root=source_root / "src",
                    env=source_env,
                    expected_version=version,
                    manifest=manifest,
                    basetemp=run_root / f"pt-s-{_python_suffix(minor)}",
                    logs_dir=logs_dir,
                )

            build_interpreter = next((interpreters[minor] for minor in minors if minor in interpreters), None)
            wheel: Path | None = None
            package_root = _export_phase(binding, run_root, "package")
            package_env = dict(base_env)
            if build_interpreter is None:
                checks_by_id["package-build"] = _blocked_record(
                    "package-build", "no required interpreter is available", logs_dir
                )
            else:
                build_record, package_artifacts, wheel = run_package_build(
                    interpreter=build_interpreter,
                    package_root=package_root,
                    out_dir=dist_root,
                    env=package_env,
                    manifest=manifest,
                    logs_dir=logs_dir,
                )
                checks_by_id["package-build"] = build_record
                artifacts.update(package_artifacts)

            wheel_source_root = _export_phase(binding, run_root, "wheel")
            wheel_source_env = dict(base_env)
            dependency_snapshots: dict[str, Path] = {}
            for minor in minors:
                creator = interpreters.get(minor)
                if creator is None:
                    continue
                wheel_test_id = f"pytest-wheel-{_python_suffix(minor)}"
                install_id = f"wheel-install-{_python_suffix(minor)}"
                dependency_id = f"wheel-dependency-check-{_python_suffix(minor)}"
                contract_id = f"wheel-contract-{_python_suffix(minor)}"
                try:
                    runtime_interpreter, runtime_proof = _create_isolated_venv(
                        creator=creator,
                        root=run_root / f"runtime-{_python_suffix(minor)}",
                        role="runtime",
                        logs_dir=logs_dir,
                    )
                except GateBlock as exc:
                    checks_by_id[install_id] = _blocked_record(install_id, exc.detail, logs_dir)
                    checks_by_id[dependency_id] = _blocked_record(dependency_id, exc.detail, logs_dir)
                    checks_by_id[contract_id] = _blocked_record(contract_id, exc.detail, logs_dir)
                    checks_by_id[wheel_test_id] = _blocked_record(wheel_test_id, exc.detail, logs_dir)
                    continue
                install = _install_wheel(
                    interpreter=runtime_interpreter,
                    runtime_environment=runtime_proof,
                    wheel=wheel,
                    source_root=wheel_source_root,
                    env=wheel_source_env,
                    manifest=manifest,
                    logs_dir=logs_dir,
                )
                checks_by_id[install_id] = install
                if install["status"] == "pass":
                    external_inputs.append(
                        {
                            "check_id": install_id,
                            "kind": "live-package-index",
                            "locator": manifest["checks"]["wheel-contract"]["dependency_index"],
                            "mutable": True,
                            "identity": "live-service-unversioned",
                            "observed_at": _utc_now(),
                        }
                    )
                dependency = _wheel_dependency_check(
                    interpreter=runtime_interpreter,
                    runtime_environment=runtime_proof,
                    source_root=wheel_source_root,
                    env=wheel_source_env,
                    install_record=install,
                    timeout_seconds=int(
                        manifest["checks"]["wheel-contract"]["timeout_seconds"]
                    ),
                    logs_dir=logs_dir,
                )
                checks_by_id[dependency_id] = dependency
                contract = _wheel_contract(
                    interpreter=runtime_interpreter,
                    runtime_environment=runtime_proof,
                    source_root=wheel_source_root,
                    env=wheel_source_env,
                    expected_version=version,
                    manifest=manifest,
                    install_record=install,
                    logs_dir=logs_dir,
                )
                checks_by_id[contract_id] = contract
                if dependency["status"] == "pass":
                    try:
                        dependency_snapshots[minor] = _runtime_dependency_snapshot(
                            interpreter=runtime_interpreter,
                            source_root=wheel_source_root,
                            env=wheel_source_env,
                            output=run_root / f"audit-requirements-{_python_suffix(minor)}.txt",
                            timeout_seconds=int(
                                manifest["checks"]["wheel-contract"]["timeout_seconds"]
                            ),
                            logs_dir=logs_dir,
                        )
                    except GateBlock as exc:
                        checks_by_id[dependency_id] = _change_record_failure(
                            dependency,
                            exc.code,
                            exc.detail,
                        )
                try:
                    test_interpreter, test_proof = _prepare_wheel_test_environment(
                        creator=creator,
                        wheel=wheel,
                        root=run_root / f"test-{_python_suffix(minor)}",
                        source_root=wheel_source_root,
                        env=wheel_source_env,
                        manifest=manifest,
                        logs_dir=logs_dir,
                    )
                except GateBlock as exc:
                    checks_by_id[wheel_test_id] = _blocked_record(wheel_test_id, exc.detail, logs_dir)
                else:
                    checks_by_id[wheel_test_id] = _run_pytest_mode(
                        mode="wheel",
                        interpreter=test_interpreter,
                        source_root=wheel_source_root,
                        import_root=Path(test_proof["prefix"]),
                        env=wheel_source_env,
                        expected_version=version,
                        manifest=manifest,
                        basetemp=run_root / f"pt-w-{_python_suffix(minor)}",
                        logs_dir=logs_dir,
                        runtime_environment=test_proof,
                    )
                    if checks_by_id[wheel_test_id]["status"] == "pass":
                        external_inputs.append(
                            {
                                "check_id": wheel_test_id,
                                "kind": "live-package-index",
                                "locator": manifest["checks"]["wheel-contract"]["dependency_index"],
                                "mutable": True,
                                "identity": "live-service-unversioned",
                                "observed_at": _utc_now(),
                            }
                        )

            static_interpreter = next(
                (interpreters[minor] for minor in reversed(minors) if minor in interpreters),
                None,
            )
            static_ids = required_set & {"ruff", "bandit", "gitleaks", "pip-audit", "semgrep", "zizmor"}
            static_root = _export_phase(binding, run_root, "static")
            static_env = dict(base_env)
            if static_ids and static_interpreter is None:
                for check_id in static_ids:
                    checks_by_id[check_id] = _blocked_record(
                        check_id, "no interpreter is available for static checks", logs_dir
                    )
            elif static_ids and static_interpreter is not None:
                static_records, static_artifacts, observed_external = run_static_checks(
                    interpreter=static_interpreter,
                    source_root=static_root,
                    candidate_root=candidate_root,
                    env=static_env,
                    manifest=manifest,
                    required_ids=static_ids,
                    audit_requirements=dependency_snapshots.get(static_interpreter.requested),
                    logs_dir=logs_dir,
                )
                checks_by_id.update(static_records)
                artifacts.update(static_artifacts)
                external_inputs.extend(observed_external)
        except (GateBlock, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            execution_problem = f"gate execution aborted safely: {exc}"

    try:
        final_binding = capture_candidate_binding(candidate_root)
    except GateBlock as exc:
        final_binding = None
        checks_by_id["final-binding"] = _blocked_record("final-binding", exc.detail, logs_dir)
    else:
        checks_by_id["final-binding"] = _binding_record(
            "final-binding",
            final_binding,
            matches=_same_binding(binding, final_binding),
            detail="candidate SHA, tree, manifest, runner, and cleanliness remained stable",
            logs_dir=logs_dir,
        )

    for check_id in required_ids:
        if check_id not in checks_by_id:
            checks_by_id[check_id] = _blocked_record(
                check_id,
                execution_problem or "a required interpreter or predecessor is unavailable",
                logs_dir,
            )
    checks = [checks_by_id[check_id] for check_id in required_ids]
    blockers = _blockers_for_checks(checks)
    clean_after = final_binding.clean if final_binding is not None else False
    head_stable = final_binding is not None and _same_binding(binding, final_binding)
    verdict = "pass" if not blockers and binding.clean and clean_after and head_stable else "block"
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "profile": profile,
        "verdict": verdict,
        "complete": scope == "local",
        "execution_scope": scope,
        "ci_leg": normalized_leg,
        "subject": {
            "candidate_sha": binding.candidate_sha,
            "candidate_tree": binding.candidate_tree,
            "version": version,
            "clean_before": binding.clean,
            "clean_after": clean_after,
            "head_stable": head_stable,
        },
        "manifest": {
            "path": DEFAULT_MANIFEST,
            "schema_version": manifest["schema_version"],
            "git_blob_id": binding.manifest_git_blob,
            "sha256": binding.manifest_sha256,
            "logical_plan_sha256": logical_plan_digest(manifest, profile),
        },
        "authority": {
            "declared_required_ci_matrix": expected_ci_legs(manifest, profile),
            "local_interpreters": list(manifest["profiles"][profile]["local"]["python_minors"]),
            "ci_aggregate_authoritative": True,
            "ci_native_exceptions": manifest["ci_native_exceptions"],
        },
        "runner": {
            "agenttalk_version": __version__,
            "module_path": "src/agenttalk/dev_gate.py",
            "git_blob_id": binding.runner_git_blob,
            "module_sha256": _module_blob_sha256(binding),
            "os": _platform_label(),
            "architecture": platform.machine() or "unknown",
        },
        "isolation": {
            "temp_outside_candidate": not _is_within(run_root, candidate_root),
            "temp_outside_store": store_root is None or not _is_within(run_root, store_root),
            "pytest_cache_disabled": True,
            "bytecode_disabled": True,
            "phase_isolated_exports": True,
            "pip_configuration_disabled": True,
            "child_path_sanitized": True,
        },
        "interpreters": interpreter_rows,
        "required_check_ids": required_ids,
        "checks": checks,
        "artifacts": artifacts,
        "external_inputs": external_inputs,
        "blockers": blockers,
        "summary": {
            "required": len(checks),
            "passed": sum(check["status"] == "pass" for check in checks),
            "blocked": sum(check["status"] != "pass" for check in checks),
        },
    }
    digest = write_run_evidence(output_path, artifact, manifest)
    return GateRunResult(
        exit_code=0 if verdict == "pass" else 1,
        evidence_path=output_path,
        evidence_sha256=digest,
        artifact=artifact,
        run_root=run_root,
    )


def validate_aggregate_artifact(
    artifact: Any,
    manifest: dict[str, Any],
    *,
    current_binding: CandidateBinding | None = None,
) -> dict[str, Any]:
    record = _require_object(artifact, "aggregate")
    required = {
        "schema_version",
        "artifact_type",
        "run_id",
        "started_at",
        "finished_at",
        "profile",
        "verdict",
        "complete",
        "execution_scope",
        "subject",
        "manifest",
        "authority",
        "legs",
        "blockers",
    }
    _require_artifact_fields(record, required, "aggregate")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != SCHEMA_VERSION
        or record["artifact_type"] != AGGREGATE_ARTIFACT_TYPE
        or record["execution_scope"] != "ci-aggregate"
        or record["profile"] != DEFAULT_PROFILE
        or not isinstance(record["verdict"], str)
        or record["verdict"] not in {"pass", "block"}
        or not isinstance(record["complete"], bool)
        or not _is_nonempty_string(record["run_id"])
        or not _valid_timestamp(record["started_at"])
        or not _valid_timestamp(record["finished_at"])
    ):
        raise GateBlock("evidence_schema_invalid", "aggregate header is invalid")
    subject = _require_object(record["subject"], "aggregate.subject")
    _require_artifact_fields(
        subject,
        {"candidate_sha", "candidate_tree", "version", "clean_before", "clean_after", "head_stable"},
        "aggregate.subject",
    )
    if (
        not _is_hash(subject["candidate_sha"], 40, 64)
        or not _is_hash(subject["candidate_tree"], 40, 64)
        or not _is_nonempty_string(subject["version"])
        or any(
            not isinstance(subject[field], bool)
            for field in ("clean_before", "clean_after", "head_stable")
        )
    ):
        raise GateBlock("evidence_schema_invalid", "aggregate subject hashes are malformed")
    manifest_record = _require_object(record["manifest"], "aggregate.manifest")
    _require_artifact_fields(
        manifest_record,
        {"path", "schema_version", "git_blob_id", "sha256", "logical_plan_sha256"},
        "aggregate.manifest",
    )
    if (
        manifest_record["path"] != DEFAULT_MANIFEST
        or not isinstance(manifest_record["schema_version"], int)
        or isinstance(manifest_record["schema_version"], bool)
        or manifest_record["schema_version"] != SCHEMA_VERSION
        or not _is_hash(manifest_record["git_blob_id"], 40, 64)
        or not _is_hash(manifest_record["sha256"], 64)
        or not _is_hash(manifest_record["logical_plan_sha256"], 64)
        or manifest_record["logical_plan_sha256"] != logical_plan_digest(manifest, record["profile"])
    ):
        raise GateBlock("evidence_binding_mismatch", "aggregate manifest binding is invalid")
    authority = _require_object(record["authority"], "aggregate.authority")
    _require_artifact_fields(
        authority,
        {"declared_required_ci_matrix", "local_interpreters", "ci_aggregate_authoritative", "ci_native_exceptions"},
        "aggregate.authority",
    )
    expected = expected_ci_legs(manifest, record["profile"])
    if (
        authority["declared_required_ci_matrix"] != expected
        or authority["local_interpreters"] != list(REQUIRED_LOCAL_PYTHONS)
        or authority["ci_aggregate_authoritative"] is not True
        or authority["ci_native_exceptions"] != manifest["ci_native_exceptions"]
    ):
        raise GateBlock("evidence_binding_mismatch", "aggregate authority declaration is invalid")
    legs = _require_list(record["legs"], "aggregate.legs")
    leg_ids: list[str] = []
    for index, leg in enumerate(legs):
        item = _require_object(leg, f"aggregate.legs[{index}]")
        _require_artifact_fields(
            item,
            {"ci_leg", "run_id", "verdict", "artifact_sha256"},
            f"aggregate.legs[{index}]",
        )
        if (
            not isinstance(item["ci_leg"], str)
            or item["ci_leg"] not in expected
            or not _is_nonempty_string(item["run_id"])
            or not isinstance(item["verdict"], str)
            or item["verdict"] not in {"pass", "block"}
        ):
            raise GateBlock("evidence_schema_invalid", f"aggregate.legs[{index}] is invalid")
        if not _is_hash(item["artifact_sha256"], 64):
            raise GateBlock("evidence_schema_invalid", f"aggregate.legs[{index}] digest is malformed")
        leg_ids.append(item["ci_leg"])
    if len(leg_ids) != len(set(leg_ids)):
        raise GateBlock("evidence_cardinality_invalid", "aggregate contains duplicate legs")
    blockers = _require_list(record["blockers"], "aggregate.blockers")
    for index, blocker in enumerate(blockers):
        item = _require_object(blocker, f"aggregate.blockers[{index}]")
        _require_artifact_fields(item, {"code", "check_id", "detail"}, f"aggregate.blockers[{index}]")
        if not all(_is_nonempty_string(item[field]) for field in ("code", "check_id", "detail")):
            raise GateBlock("evidence_schema_invalid", f"aggregate.blockers[{index}] is invalid")
    if record["complete"]:
        if leg_ids != expected:
            raise GateBlock("evidence_cardinality_invalid", "complete aggregate lacks the exact ordered CI matrix")
        should_pass = all(leg["verdict"] == "pass" for leg in legs)
        if (record["verdict"] == "pass") != should_pass or (should_pass and blockers):
            raise GateBlock("evidence_false_pass", "aggregate verdict does not match its legs")
    elif record["verdict"] != "block" or not blockers:
        raise GateBlock("evidence_false_pass", "incomplete aggregate must block with a reason")
    if current_binding is not None:
        if record["complete"] and not current_binding.clean:
            raise GateBlock("aggregate_checkout_dirty", "current checkout is not clean")
        comparisons = {
            "candidate_sha": current_binding.candidate_sha,
            "candidate_tree": current_binding.candidate_tree,
            "version": _committed_version(current_binding.root),
        }
        if any(subject[field] != value for field, value in comparisons.items()):
            raise GateBlock("aggregate_binding_mismatch", "aggregate subject does not match current checkout")
        if (
            manifest_record["git_blob_id"] != current_binding.manifest_git_blob
            or manifest_record["sha256"] != current_binding.manifest_sha256
        ):
            raise GateBlock("aggregate_binding_mismatch", "aggregate manifest does not match current checkout")
    return record


def write_aggregate_evidence(
    path: Path,
    artifact: dict[str, Any],
    manifest: dict[str, Any],
    *,
    current_binding: CandidateBinding,
) -> str:
    validate_aggregate_artifact(artifact, manifest, current_binding=current_binding)
    payload = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        write_text(path, payload, encoding="utf-8", newline="\n")
        raw = path.read_bytes()
        loaded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateBlock("evidence_write_failed", f"cannot write aggregate evidence {path}: {exc}") from exc
    if raw != payload.encode("utf-8"):
        raise GateBlock("evidence_roundtrip_mismatch", "aggregate evidence bytes changed during write")
    validate_aggregate_artifact(loaded, manifest, current_binding=current_binding)
    return sha256_bytes(raw)


def _aggregate_failure(
    *,
    manifest: dict[str, Any],
    profile: str,
    binding: CandidateBinding,
    version: str,
    started_at: str,
    problem: GateBlock,
    legs: list[dict[str, Any]],
    stable: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": AGGREGATE_ARTIFACT_TYPE,
        "run_id": uuid.uuid4().hex,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "profile": profile,
        "verdict": "block",
        "complete": False,
        "execution_scope": "ci-aggregate",
        "subject": {
            "candidate_sha": binding.candidate_sha,
            "candidate_tree": binding.candidate_tree,
            "version": version,
            "clean_before": binding.clean,
            "clean_after": stable,
            "head_stable": stable,
        },
        "manifest": {
            "path": DEFAULT_MANIFEST,
            "schema_version": manifest["schema_version"],
            "git_blob_id": binding.manifest_git_blob,
            "sha256": binding.manifest_sha256,
            "logical_plan_sha256": logical_plan_digest(manifest, profile),
        },
        "authority": {
            "declared_required_ci_matrix": expected_ci_legs(manifest, profile),
            "local_interpreters": list(manifest["profiles"][profile]["local"]["python_minors"]),
            "ci_aggregate_authoritative": True,
            "ci_native_exceptions": manifest["ci_native_exceptions"],
        },
        "legs": legs,
        "blockers": [{"code": problem.code, "check_id": "aggregate", "detail": problem.detail}],
    }


def execute_aggregate(
    *,
    root: Path,
    input_root: Path,
    profile: str = DEFAULT_PROFILE,
    evidence_path: Path | None = None,
    temp_base: Path | None = None,
) -> GateRunResult:
    """Aggregate the exact declared CI matrix and bind it to the current checkout."""

    started_at = _utc_now()
    candidate_root = root.resolve()
    binding = capture_candidate_binding(candidate_root)
    manifest = load_bound_manifest(binding)
    version = _committed_version(candidate_root)
    configured_store = os.environ.get("AGENTTALK_ROOT")
    store_root = Path(configured_store).resolve() if configured_store else None
    external_base = _ensure_external(
        (temp_base or _default_external_base(candidate_root, store_root)).resolve(),
        candidate_root,
        store_root,
        "gate temp root",
    )
    external_base.mkdir(parents=True, exist_ok=True)
    output_path = evidence_path or (
        external_base / f"agenttalk-dev-gate-aggregate-{binding.candidate_sha[:12]}-{uuid.uuid4().hex}.json"
    )
    output_path = _external_location(
        output_path,
        candidate_root=candidate_root,
        store_root=store_root,
        label="aggregate evidence path",
    )
    input_dir = input_root.resolve()
    files = sorted(path for path in input_dir.rglob("*.json") if path.resolve() != output_path)
    artifacts: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    observed_legs: list[dict[str, Any]] = []
    problem: GateBlock | None = None
    if not files:
        problem = GateBlock("aggregate_missing_evidence", f"no JSON evidence found under {input_dir}")
    for path in files:
        if problem is not None:
            break
        try:
            raw = path.read_bytes()
            artifact = json.loads(raw.decode("utf-8"))
            validated = validate_run_artifact(artifact, manifest)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, GateBlock) as exc:
            detail = exc.detail if isinstance(exc, GateBlock) else str(exc)
            problem = GateBlock("aggregate_input_invalid", f"{path}: {detail}")
            break
        leg = validated["ci_leg"]
        if leg in digests:
            problem = GateBlock("aggregate_duplicate_leg", f"duplicate CI leg {leg}")
            break
        digest = sha256_bytes(raw)
        digests[leg] = digest
        artifacts.append(validated)
        observed_legs.append(
            {
                "ci_leg": leg,
                "run_id": validated["run_id"],
                "verdict": validated["verdict"],
                "artifact_sha256": digest,
            }
        )
    if problem is None:
        try:
            artifact = aggregate_leg_artifacts(
                manifest,
                profile,
                artifacts,
                binding,
                input_sha256_by_leg=digests,
            )
        except GateBlock as exc:
            problem = exc
    try:
        final_binding = capture_candidate_binding(candidate_root)
        stable = final_binding.clean and _same_binding(binding, final_binding)
    except GateBlock as exc:
        final_binding = binding
        stable = False
        if problem is None:
            problem = exc
    if problem is None and not stable:
        problem = GateBlock("aggregate_binding_changed", "checkout changed while aggregating evidence")
    if problem is not None:
        artifact = _aggregate_failure(
            manifest=manifest,
            profile=profile,
            binding=binding,
            version=version,
            started_at=started_at,
            problem=problem,
            legs=observed_legs,
            stable=stable,
        )
    else:
        artifact["started_at"] = started_at
        artifact["finished_at"] = _utc_now()
    digest = write_aggregate_evidence(
        output_path,
        artifact,
        manifest,
        current_binding=binding,
    )
    return GateRunResult(
        exit_code=0 if artifact["verdict"] == "pass" else 1,
        evidence_path=output_path,
        evidence_sha256=digest,
        artifact=artifact,
    )


def reenter_candidate_source(root: Path, argv: Sequence[str]) -> int | None:
    """Re-exec once from a committed export, never from mutable checkout bytes."""

    candidate_root = root.resolve()
    observed_module = Path(__file__).resolve()
    committed_src_text = os.environ.get("AGENTTALK_DEV_GATE_COMMITTED_SRC")
    if committed_src_text:
        committed_src = Path(committed_src_text).resolve()
        binding = capture_candidate_binding(candidate_root)
        if _is_within(committed_src, candidate_root) or not _is_within(observed_module, committed_src):
            raise GateBlock(
                "candidate_source_reentry_failed",
                f"re-entered process imported {observed_module}, expected under external {committed_src}",
            )
        try:
            observed_blob = _git_hash_worktree_bytes(
                candidate_root,
                "src/agenttalk/dev_gate.py",
                observed_module.read_bytes(),
            )
        except OSError as exc:
            raise GateBlock("candidate_source_reentry_failed", f"cannot hash executing runner: {exc}") from exc
        if observed_blob != binding.runner_git_blob:
            raise GateBlock(
                "candidate_source_reentry_failed",
                "executing runner does not clean-filter to the HEAD blob",
            )
        return None

    binding = capture_candidate_binding(candidate_root)
    configured_store = os.environ.get("AGENTTALK_ROOT")
    store_root = Path(configured_store).resolve() if configured_store else None
    external_base = _default_external_base(candidate_root, store_root)
    external_base.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with tempfile.TemporaryDirectory(
            prefix="agenttalk-dev-gate-bootstrap-", dir=str(external_base)
        ) as bootstrap_text:
            bootstrap_root = Path(bootstrap_text).resolve()
            committed_root = bootstrap_root / "candidate"
            export_candidate(binding, committed_root, bootstrap_root / "candidate.zip")
            committed_src = (committed_root / "src").resolve()
            env["PYTHONPATH"] = str(committed_src)
            env["AGENTTALK_DEV_GATE_COMMITTED_SRC"] = str(committed_src)
            # Re-entry uses this interpreter with a fixed module argv; shell execution is disabled.
            # argv is the gate's own pinned/validated command line (-I isolated, no shell), not
            # tainted env input; suppress the semgrep tainted-env sink (anchored on the argv list)
            # to match the nosec on the call above.
            completed = subprocess.run(  # nosec B603
                [  # nosemgrep
                    sys.executable,
                    "-I",
                    "-c",
                    _ISOLATED_SOURCE_LAUNCHER,
                    str(committed_src),
                    "dev-gate",
                    *argv,
                ],
                cwd=candidate_root,
                env=env,
                check=False,
            )
    except OSError as exc:
        raise GateBlock("candidate_source_reentry_failed", str(exc)) from exc
    return completed.returncode
