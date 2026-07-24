"""Assurance scan evidence producer.

This module is intentionally stdlib-only. It detects project shape, runs a
bounded set of installed tools and built-in checks, compares findings to a
reviewed baseline, and writes a normalized artifact. It never decides GO/HOLD.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - scanner runs fixed argv lists with shell disabled
import sys
import tempfile
import time
import venv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agenttalk import __version__, gates
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.coverage_parse import parse_coverage_percent


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "assurance-scan-run"
RUNNER_VERSION = f"agenttalk-{__version__}"

DEFAULT_MANIFEST = Path(".agenttalk") / "assurance.json"
DEFAULT_BASELINE = Path(".agenttalk") / "assurance" / "baseline.json"
DEFAULT_RUNS_DIR = Path(".agenttalk") / "assurance" / "runs"

PROFILES = ("change", "release", "deep")
STATUS_VOCAB = (
    "pass",
    "fail-blocking",
    "fail-advisory",
    "skipped-not-installed",
    "skipped-not-applicable",
    "skipped-network-disabled",
    "error-required-tool",
    "error-optional-tool",
    "timeout-required",
    "timeout-optional",
)
DELTA_STATUSES = (
    "new",
    "unchanged",
    "worsened",
    "fixed",
    "accepted-applied",
    "accepted-expired",
)
SEVERITIES = ("info", "low", "medium", "high", "critical")
SEVERITY_RANK = {name: idx for idx, name in enumerate(SEVERITIES)}

DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "target",
    "coverage",
}
DEFAULT_EXCLUDED_RELATIVE = {
    ".agenttalk/assurance/runs",
}
TEXT_EXTENSIONS = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".pyi",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
BINARY_EXTENSIONS = {
    ".7z",
    ".bmp",
    ".class",
    ".db",
    ".dll",
    ".eot",
    ".exe",
    ".gif",
    ".ico",
    ".jar",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".so",
    ".ttf",
    ".wasm",
    ".webp",
    ".woff",
    ".woff2",
    ".whl",
    ".zip",
}

NETWORK_TOOLS = {"osv-scanner", "pip-audit"}
SECURITY_TOOLS = {"bandit", "semgrep", "gitleaks", "osv-scanner", "pip-audit"}
MANIFEST_TOP_LEVEL_KEYS = {
    "accepted_findings",
    "custom_commands",
    "generated_artifacts",
    "monorepo",
    "paths",
    "profiles",
    "python",
    "schema_version",
    "thresholds",
    "tools",
}
PROFILE_KEYS = {
    "network_allowed",
    "required_tools",
    "severity_floor",
}
EXECUTABLE_ARTIFACT_KINDS = {"binary", "js", "powershell", "python", "shell"}
EXECUTABLE_ARTIFACT_EXTENSIONS = {".bat", ".cmd", ".exe", ".js", ".ps1", ".py", ".sh"}
GENERATED_KIND_ALIASES = {
    "bash": "shell",
    "exe": "binary",
    "javascript": "js",
    "ps1": "powershell",
    "pwsh": "powershell",
    "sh": "shell",
}
EXECUTED_STATUSES = {"pass", "fail-blocking", "fail-advisory"}
_COVERAGE_ARTIFACTS = ("coverage.xml", "coverage.json")
_COVERAGE_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
_COVERAGE_RECOVERY_DIR = Path(".agenttalk") / "assurance" / "coverage-recovery"
_COVERAGE_RECOVERY_MARKER = "transaction.json"
_COVERAGE_RECOVERY_SCHEMA = 1
_GIT_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")
_GITHUB_REPOSITORY_RE = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_ASCII_DIGITS_RE = re.compile(r"\A[0-9]+\Z")
_COMMAND_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class AssuranceUsageError(ValueError):
    """Invalid CLI, manifest, or baseline input."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


@dataclass
class ArtifactPaths:
    run_dir: Path
    artifact: Path
    summary: Path | None = None


@dataclass
class ScanPlan:
    root: Path
    profile: str
    manifest: dict[str, Any]
    baseline: dict[str, Any]
    detection: dict[str, Any]
    provenance: dict[str, Any]
    tools: list[dict[str, Any]]
    network_default: bool = False
    run_id: str = field(default_factory=_new_run_id)
    runner_errors: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    root: Path
    profile: str
    manifest: dict[str, Any]
    baseline: dict[str, Any]
    detection: dict[str, Any]
    provenance: dict[str, Any]
    tools_considered: list[str]
    tools_run: list[dict[str, Any]]
    tools_skipped: list[dict[str, Any]]
    required_missing: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    residual_risk: list[dict[str, Any]]
    runner_errors: list[str]
    run_id: str
    generated_at: str = field(default_factory=_now_iso)
    accepted_findings: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"applied": [], "expired": []})
    native_suppressions: dict[str, Any] = field(default_factory=lambda: {"count_by_tool": {}, "examples": []})
    raw_logs: dict[str, str] = field(default_factory=dict)
    artifact: dict[str, Any] | None = None


@dataclass
class _CoverageArtifactState:
    eligible: set[str]
    backups: dict[str, Path]
    quarantine: Path | None
    preparation_error: str | None = None


def _default_manifest(path: Path | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profiles": {profile: {} for profile in PROFILES},
        "tools": {},
        "thresholds": {},
        "custom_commands": {},
        "paths": {"include": [], "exclude": [], "generated": [], "vendor": []},
        "accepted_findings": [],
        "generated_artifacts": [],
        "monorepo": {"packages": []},
        "python": {},
        "_path": str(path or DEFAULT_MANIFEST),
        "_validation_errors": [],
    }


def _default_baseline(path: Path | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": "none",
        "findings": [],
        "_path": str(path or DEFAULT_BASELINE),
        "_validation_errors": [],
    }


def load_manifest(root: Path | str, path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate `.agenttalk/assurance.json`.

    Missing manifests are allowed and become a conservative default. Malformed
    manifests raise :class:`AssuranceUsageError`; the CLI catches that and emits
    a blocking validation finding in the artifact.
    """
    root_path = Path(root).resolve()
    manifest_path = _resolve_under_root(root_path, path or DEFAULT_MANIFEST)
    if not manifest_path.exists():
        return _default_manifest(manifest_path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceUsageError(f"malformed assurance manifest: {exc}") from exc
    manifest = _normalize_manifest(data, manifest_path)
    return manifest


def load_baseline(root: Path | str, path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate `.agenttalk/assurance/baseline.json`."""
    root_path = Path(root).resolve()
    baseline_path = _resolve_under_root(root_path, path or DEFAULT_BASELINE)
    if not baseline_path.exists():
        return _default_baseline(baseline_path)
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceUsageError(f"malformed assurance baseline: {exc}") from exc
    baseline = _normalize_baseline(data, baseline_path)
    return baseline


def detect_project(root: Path | str, manifest: dict[str, Any]) -> dict[str, Any]:
    root_path = Path(root).resolve()
    stacks: list[dict[str, Any]] = []
    root_stack = _detect_one_root(root_path, root_path, manifest)
    if root_stack:
        stacks.extend(root_stack)
    children: list[dict[str, Any]] = []
    for child in _manifest_packages(manifest):
        child_path = _resolve_under_root(root_path, child.get("path", ""))
        child_stacks = _detect_one_root(root_path, child_path, manifest)
        if not child_stacks:
            child_stacks = [
                {
                    "id": "mixed",
                    "root": _rel(root_path, child_path),
                    "confidence": "low",
                    "markers": [],
                    "dependency_posture": "unknown",
                }
            ]
        for stack in child_stacks:
            stack["package_name"] = child.get("name")
        children.extend(child_stacks)
    return {"stacks": stacks, "monorepo_children": children}


def collect_provenance(
    root: Path | str,
    manifest: dict[str, Any],
    profile: str,
    baseline: dict[str, Any] | None = None,
    *,
    changed_from: str | None = None,
    changed_to: str | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    baseline = baseline or _default_baseline()
    manifest_path = _resolve_under_root(root_path, manifest.get("_path") or DEFAULT_MANIFEST)
    baseline_path = _resolve_under_root(root_path, baseline.get("_path") or DEFAULT_BASELINE)
    git_sha = _git_output(root_path, ["rev-parse", "HEAD"])
    git_status = _git_output(root_path, ["status", "--porcelain"])
    git_dirty = None if git_status is None else bool(git_status)
    changed_files = _changed_files(root_path, changed_from=changed_from, changed_to=changed_to)
    manifest_rel = _slash(_rel(root_path, manifest_path))
    baseline_rel = _slash(_rel(root_path, baseline_path))
    packages = _packages_to_resolve(root_path, manifest)
    resolved = [_resolve_package(root_path, package) for package in packages]
    return {
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "changed_from": changed_from,
        "changed_to": changed_to,
        "changed_files": changed_files,
        "manifest_path": manifest_rel,
        "manifest_sha256": _sha256_file(manifest_path),
        "manifest_changed_in_scan_range": manifest_rel in changed_files,
        "baseline_path": baseline_rel,
        "baseline_sha256": _sha256_file(baseline_path),
        "baseline_changed_in_scan_range": baseline_rel in changed_files,
        "resolved_package_paths": resolved,
        "profile": profile,
    }


def build_plan(
    root: Path | str,
    profile: str,
    manifest: dict[str, Any],
    detection: dict[str, Any],
    baseline: dict[str, Any],
    provenance: dict[str, Any],
) -> ScanPlan:
    if profile not in PROFILES:
        raise AssuranceUsageError(f"profile must be one of {', '.join(PROFILES)}")
    root_path = Path(root).resolve()
    tools: list[dict[str, Any]] = []

    def add(spec: dict[str, Any]) -> None:
        spec = dict(spec)
        tool_cfg = _tool_config(manifest, spec["tool_id"])
        if tool_cfg.get("enabled") is False:
            return
        if isinstance(tool_cfg.get("command"), list):
            command = tool_cfg["command"]
            if all(isinstance(part, str) and part for part in command):
                spec["command"] = list(command)
        if isinstance(tool_cfg.get("install_hint"), str):
            spec["install_hint"] = tool_cfg["install_hint"]
        if isinstance(tool_cfg.get("timeout_seconds"), int):
            spec["timeout_seconds"] = tool_cfg["timeout_seconds"]
        spec.setdefault("timeout_seconds", 60)
        spec.setdefault("network_required", False)
        spec.setdefault("network_allowed", _network_allowed(manifest, profile, spec["tool_id"]))
        spec["required"] = _is_required(
            manifest,
            profile,
            spec["tool_id"],
            bool(spec.get("required") or tool_cfg.get("required")),
        )
        tools.append(spec)

    add({"tool_id": "manifest-validate", "dimension": "other", "built_in": "manifest"})
    add({"tool_id": "baseline-validate", "dimension": "other", "built_in": "baseline"})
    add(
        {
            "tool_id": "provenance",
            "dimension": "supply_chain",
            "built_in": "provenance",
            "required": True,
        }
    )
    add(
        {
            "tool_id": "encoding-hygiene",
            "dimension": "encoding",
            "built_in": "encoding",
            "required": True,
        }
    )
    add(
        {
            "tool_id": "git-diff-check",
            "dimension": "encoding",
            "built_in": "git_diff",
            "required": True,
        }
    )
    add({"tool_id": "generated-artifacts", "dimension": "generated_artifact", "built_in": "generated"})

    stack_ids = _stack_ids(detection)
    python_present = "python" in stack_ids
    if python_present:
        add(
            {
                "tool_id": "python-compileall",
                "dimension": "quality",
                "built_in": "compile",
                "required": True,
            }
        )
        if _ruff_applicable(root_path, manifest, profile):
            add({"tool_id": "ruff", "dimension": "quality", "executable": "ruff", "args": ["check", "."]})
            add(
                {
                    "tool_id": "ruff-format",
                    "dimension": "quality",
                    "executable": "ruff",
                    "args": ["format", "--check", "."],
                }
            )
        test_cmd = _test_command(root_path, manifest)
        if test_cmd:
            add(
                {
                    "tool_id": "tests",
                    "dimension": "quality",
                    "command": test_cmd,
                    "required": True,
                    "timeout_seconds": 300,
                }
            )
        if _mypy_configured(root_path, manifest, profile):
            add({"tool_id": "mypy", "dimension": "quality", "executable": "mypy", "args": ["."]})
        if _pyright_configured(root_path, manifest, profile):
            add({"tool_id": "pyright", "dimension": "quality", "executable": "pyright", "args": []})
        coverage_cmd = _custom_command(manifest, "coverage")
        if coverage_cmd:
            add(
                {
                    "tool_id": "coverage",
                    "dimension": "quality",
                    "command": coverage_cmd,
                    "timeout_seconds": 300,
                }
            )
        add({"tool_id": "complexity", "dimension": "complexity", "built_in": "complexity"})
        add({"tool_id": "bandit", "dimension": "security", "executable": "bandit", "args": ["-q", "-r", "."]})
        if _semgrep_applicable(root_path, manifest):
            add(
                {
                    "tool_id": "semgrep",
                    "dimension": "security",
                    "executable": "semgrep",
                    "args": ["--config", _semgrep_config(root_path, manifest), "--json", "."],
                    "network_required": _semgrep_requires_network(root_path, manifest),
                }
            )
        add(
            {
                "tool_id": "gitleaks",
                "dimension": "secrets",
                "executable": "gitleaks",
                "args": ["detect", "--source", ".", "--no-git", "--report-format", "json"],
            }
        )
        add(
            {
                "tool_id": "osv-scanner",
                "dimension": "deps",
                "executable": "osv-scanner",
                "args": ["--format", "json", "."],
                "network_required": True,
                "install_hint": "Install osv-scanner or allow pip-audit fallback.",
            }
        )
        add(
            {
                "tool_id": "pip-audit",
                "dimension": "deps",
                "executable": "pip-audit",
                "args": ["--format", "json"],
                "network_required": True,
                "install_hint": "Install pip-audit or configure osv-scanner.",
            }
        )
        if profile == "release":
            add({"tool_id": "python-build", "dimension": "packaging", "built_in": "python_build"})
            add({"tool_id": "twine-check", "dimension": "packaging", "built_in": "twine_check"})
            add({"tool_id": "install-smoke", "dimension": "packaging", "built_in": "install_smoke"})
            add({"tool_id": "import-smoke", "dimension": "packaging", "built_in": "import_smoke"})

    for language in ("js_ts", "go", "rust"):
        if language in stack_ids:
            add(
                {
                    "tool_id": f"{language}-basic",
                    "dimension": "quality",
                    "built_in": "language_stub",
                    "language": language,
                }
            )

    return ScanPlan(
        root=root_path,
        profile=profile,
        manifest=manifest,
        baseline=baseline,
        detection=detection,
        provenance=provenance,
        tools=tools,
        network_default=False,
    )


def run_plan(plan: ScanPlan) -> ScanResult:
    fresh_coverage_attestations: set[tuple[str, str]] = set()
    try:
        return _run_plan(plan, fresh_coverage_attestations)
    finally:
        _invalidate_stale_coverage_gate(plan, fresh_coverage_attestations)


def _run_plan(
    plan: ScanPlan,
    fresh_coverage_attestations: set[tuple[str, str]],
) -> ScanResult:
    tools_run: list[dict[str, Any]] = []
    tools_skipped: list[dict[str, Any]] = []
    required_missing: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []
    raw_logs: dict[str, str] = {}

    for spec in plan.tools:
        tool_id = spec["tool_id"]
        if spec.get("network_required") and not spec.get("network_allowed", False):
            skipped = _skipped(tool_id, "skipped-network-disabled", "network disabled by default", spec)
            tools_skipped.append(skipped)
            if spec.get("required"):
                required_missing.append({"tool_id": tool_id, "reason": skipped["reason"]})
            else:
                residual.append(_risk(spec["dimension"], skipped["reason"], "medium"))
            continue
        if spec.get("built_in"):
            run, built_findings, built_residual = _run_builtin(plan, spec)
            tools_run.append(run)
            findings.extend(built_findings)
            residual.extend(built_residual)
            if spec.get("required") and run["status"].startswith(("skipped-", "error-", "timeout-")):
                required_missing.append({"tool_id": tool_id, "reason": run["status"]})
            continue
        command = _resolve_command(spec)
        if command is None:
            status = "error-required-tool" if spec.get("required") else "skipped-not-installed"
            reason = f"{tool_id} is not installed"
            skipped = _skipped(tool_id, status, reason, spec)
            tools_skipped.append(skipped)
            if spec.get("required"):
                required_missing.append({"tool_id": tool_id, "reason": reason})
            else:
                residual.append(_risk(spec["dimension"], reason, "medium", tool_id=tool_id))
            continue
        coverage_state = _prepare_coverage_artifacts(plan.root) if tool_id == "coverage" else None
        try:
            run, ext_findings, raw, stdout = _run_external(plan.root, spec, command)
        except BaseException as exc:
            if coverage_state is not None:
                coverage_outputs, coverage_cleanup_ok, coverage_artifact_error = _finish_coverage_artifacts(
                    plan.root, coverage_state
                )
                if coverage_artifact_error is not None:
                    plan.runner_errors.append(coverage_artifact_error)
                failure_status = "error-required-tool" if spec.get("required") else "error-optional-tool"
                _emit_coverage_gate(
                    plan,
                    {"status": failure_status, "exit_code": None},
                    "",
                    coverage_outputs,
                    artifacts_clean=coverage_cleanup_ok,
                    artifact_error=coverage_artifact_error,
                    failure_reason=f"coverage runner failed with {type(exc).__name__}",
                )
                fresh_coverage_attestations.add(_coverage_attestation_key(plan))
            raise
        coverage_outputs: dict[str, str | None] = {}
        coverage_cleanup_ok = True
        coverage_artifact_error: str | None = None
        if coverage_state is not None:
            coverage_outputs, coverage_cleanup_ok, coverage_artifact_error = _finish_coverage_artifacts(
                plan.root, coverage_state
            )
            if coverage_artifact_error is not None:
                plan.runner_errors.append(coverage_artifact_error)
        tools_run.append(run)
        if raw:
            raw_logs[tool_id] = raw
        findings.extend(ext_findings)
        if run["status"] in {"error-required-tool", "timeout-required"}:
            required_missing.append({"tool_id": tool_id, "reason": run["status"]})
        elif run["status"] in {"error-optional-tool", "timeout-optional"}:
            residual.append(_risk(spec["dimension"], f"{tool_id} {run['status']}", "medium", tool_id=tool_id))
        if coverage_state is not None:
            _emit_coverage_gate(
                plan,
                run,
                stdout,
                coverage_outputs,
                artifacts_clean=coverage_cleanup_ok,
                artifact_error=coverage_artifact_error,
            )
            fresh_coverage_attestations.add(_coverage_attestation_key(plan))

    considered = [spec["tool_id"] for spec in plan.tools]
    result = ScanResult(
        root=plan.root,
        profile=plan.profile,
        manifest=plan.manifest,
        baseline=plan.baseline,
        detection=plan.detection,
        provenance=plan.provenance,
        tools_considered=considered,
        tools_run=tools_run,
        tools_skipped=tools_skipped,
        required_missing=required_missing,
        findings=findings,
        residual_risk=residual,
        runner_errors=list(plan.runner_errors),
        run_id=plan.run_id,
        raw_logs=raw_logs,
    )
    return result


def apply_baseline(
    result: ScanResult,
    baseline: dict[str, Any],
    manifest: dict[str, Any],
    changed_files: list[str] | None = None,
) -> ScanResult:
    baseline_by_fp = {
        str(item.get("fingerprint")): item
        for item in baseline.get("findings", [])
        if isinstance(item, dict) and item.get("fingerprint")
    }
    accepted = {
        str(item.get("fingerprint")): item
        for item in manifest.get("accepted_findings", [])
        if isinstance(item, dict) and item.get("fingerprint")
    }
    seen: set[str] = set()
    applied: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    floor_by_dim = _severity_floor(result.profile, manifest)
    measured_tools = _executed_tools(result)

    for finding in result.findings:
        item = _normalize_finding(finding)
        fp = item["fingerprint"]
        seen.add(fp)
        acceptance = accepted.get(fp)
        if acceptance and not _acceptance_applies(acceptance, item):
            normalized.append(
                _normalize_applied_finding(
                    _finding(
                        "manifest-validate",
                        "accepted-scope-mismatch",
                        "other",
                        "high",
                        item.get("path"),
                        item.get("line"),
                        f"accepted finding scope does not match {item['tool_id']}:{item['rule_id']}",
                    ),
                    floor_by_dim,
                )
            )
            acceptance = None
        if acceptance:
            if _acceptance_expired(acceptance):
                item["status"] = "accepted-expired"
                expired.append(acceptance)
            else:
                item["status"] = "accepted-applied"
                applied.append(acceptance)
        elif fp not in baseline_by_fp:
            item["status"] = "new"
        else:
            base = baseline_by_fp[fp]
            item["status"] = "worsened" if _is_worse(item, base) else "unchanged"
        item["blocking"] = _finding_blocks(item, floor_by_dim)
        normalized.append(item)

    for fp, base in sorted(baseline_by_fp.items()):
        if fp in seen:
            continue
        tool_id = _baseline_tool_id(base)
        status = "fixed" if tool_id in measured_tools else "unchanged"
        message = "baseline finding no longer present"
        if status != "fixed":
            message = "baseline finding was not reassessed because its originating tool did not run"
            result.residual_risk.append(
                _risk(
                    str(base.get("dimension") or "other"),
                    f"baseline finding {fp} was not reassessed because {tool_id} did not run",
                    _severity(str(base.get("severity") or "medium")),
                    tool_id=tool_id,
                )
            )
        fixed = {
            "fingerprint": fp,
            "status": status,
            "dimension": str(base.get("dimension") or "other"),
            "severity": _severity(str(base.get("severity") or "info")),
            "tool_id": tool_id,
            "rule_id": str(base.get("rule_id") or "baseline"),
            "path": base.get("path"),
            "line": None,
            "message": message,
            "raw_ref": None,
            "blocking": False,
        }
        normalized.append(fixed)

    result.findings = sorted(normalized, key=_finding_sort_key)
    result.accepted_findings = {"applied": applied, "expired": expired}
    _recompute_tool_statuses(result)
    result.artifact = _artifact_from_result(result, changed_files or [])
    return result


def write_artifact(result: ScanResult, out_dir: Path | str, summary: Path | str | None = None) -> ArtifactPaths:
    out_path = Path(out_dir)
    if not out_path.is_absolute():
        out_path = result.root / out_path
    run_dir = out_path / result.run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    artifact = copy.deepcopy(result.artifact or _artifact_from_result(result, []))
    raw_paths: dict[str, str] = {}
    for tool_id, text in result.raw_logs.items():
        raw_path = raw_dir / f"{_safe_id(tool_id)}.txt"
        raw_path.write_text(text, encoding="utf-8")
        raw_paths[tool_id] = _slash(_rel(result.root, raw_path))
    for run in artifact["tools"]["run"]:
        tool_id = run.get("tool_id")
        if tool_id in raw_paths:
            run["raw_log"] = raw_paths[tool_id]
    for finding in artifact["findings"]:
        tool_id = finding.get("tool_id")
        if finding.get("raw_ref") is None and tool_id in raw_paths:
            finding["raw_ref"] = raw_paths[tool_id]
    artifact_path = run_dir / "artifact.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path: Path | None = None
    if summary:
        summary_path = Path(summary)
        if not summary_path.is_absolute():
            summary_path = run_dir / summary_path
    else:
        summary_path = run_dir / "summary.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(_summary_text(artifact), encoding="utf-8")
    result.artifact = artifact
    return ArtifactPaths(run_dir=run_dir, artifact=artifact_path, summary=summary_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agenttalk.assurance")
    parser.add_argument("--root", default=".")
    parser.add_argument("--profile", choices=PROFILES, default="change")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--out", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--changed-from")
    parser.add_argument("--changed-to")
    parser.add_argument("--summary")
    parser.add_argument("--json-only", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    root = Path(args.root).resolve()
    runner_errors: list[str] = []
    try:
        manifest = load_manifest(root, args.manifest)
    except AssuranceUsageError as exc:
        manifest = _default_manifest(_resolve_under_root(root, args.manifest))
        manifest["_validation_errors"] = [str(exc)]
    try:
        baseline = load_baseline(root, args.baseline)
    except AssuranceUsageError as exc:
        baseline = _default_baseline(_resolve_under_root(root, args.baseline))
        baseline["_validation_errors"] = [str(exc)]
    try:
        detection = detect_project(root, manifest)
        provenance = collect_provenance(
            root,
            manifest,
            args.profile,
            baseline,
            changed_from=args.changed_from,
            changed_to=args.changed_to,
        )
        plan = build_plan(root, args.profile, manifest, detection, baseline, provenance)
        plan.runner_errors.extend(runner_errors)
        result = run_plan(plan)
        result = apply_baseline(result, baseline, manifest, provenance.get("changed_files") or [])
        paths = write_artifact(result, args.out, summary=args.summary)
    except AssuranceUsageError as exc:
        sys.stderr.write(f"agenttalk assurance: {exc}\n")
        return 2
    except Exception as exc:  # noqa: BLE001 - fail-safe is no artifact, bounded stderr
        sys.stderr.write(f"agenttalk assurance: could not produce artifact: {exc}\n")
        return 1
    if args.json_only:
        print(str(paths.artifact))
    else:
        artifact = result.artifact or {}
        summary = artifact.get("verdict_summary", {})
        print(
            "assurance artifact: "
            f"{paths.artifact} "
            f"(blocking={summary.get('blocking_findings_count', 0)}, "
            f"required_skipped={summary.get('skipped_required_count', 0)})"
        )
    return 0


# --------------------------------------------------------------------------- validation


def _normalize_manifest(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AssuranceUsageError("assurance manifest must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise AssuranceUsageError("assurance manifest schema_version must be 1")
    unknown = sorted(set(data) - MANIFEST_TOP_LEVEL_KEYS)
    if unknown:
        raise AssuranceUsageError(f"assurance manifest has unknown top-level key(s): {', '.join(unknown)}")
    manifest = _default_manifest(path)
    for key in ("profiles", "tools", "thresholds", "custom_commands", "paths", "monorepo", "python"):
        if key in data:
            if not isinstance(data[key], dict):
                raise AssuranceUsageError(f"manifest {key} must be an object")
            manifest[key] = data[key]
    _validate_profiles(manifest["profiles"])
    _validate_manifest_commands(manifest)
    for key in ("accepted_findings", "generated_artifacts"):
        if key in data:
            if not isinstance(data[key], list):
                raise AssuranceUsageError(f"manifest {key} must be a list")
            manifest[key] = data[key]
    _validate_accepted_findings(manifest["accepted_findings"])
    _validate_generated_artifacts(manifest["generated_artifacts"])
    return manifest


def _normalize_baseline(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AssuranceUsageError("assurance baseline must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise AssuranceUsageError("assurance baseline schema_version must be 1")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise AssuranceUsageError("baseline findings must be a list")
    for item in findings:
        if not isinstance(item, dict) or not item.get("fingerprint"):
            raise AssuranceUsageError("each baseline finding needs a fingerprint")
    baseline = _default_baseline(path)
    baseline.update(
        {
            "baseline_id": str(data.get("baseline_id") or "baseline"),
            "updated_at": data.get("updated_at"),
            "updated_at_commit": data.get("updated_at_commit"),
            "findings": findings,
        }
    )
    return baseline


def _validate_profiles(profiles: dict[str, Any]) -> None:
    for name, cfg in profiles.items():
        if name not in PROFILES:
            raise AssuranceUsageError(f"manifest profiles has unknown profile {name!r}")
        if not isinstance(cfg, dict):
            raise AssuranceUsageError(f"manifest profiles.{name} must be an object")
        unknown = sorted(set(cfg) - PROFILE_KEYS)
        if unknown:
            raise AssuranceUsageError(f"manifest profiles.{name} has unknown key(s): {', '.join(unknown)}")


def _validate_manifest_commands(manifest: dict[str, Any]) -> None:
    for name, command in manifest["custom_commands"].items():
        _validate_command_argv(command, f"custom_commands.{name}")
    for tool_id, config in manifest["tools"].items():
        if isinstance(config, dict) and "command" in config:
            _validate_command_argv(config["command"], f"tools.{tool_id}.command")


def _validate_command_argv(command: Any, location: str) -> None:
    if not isinstance(command, list) or not command:
        raise AssuranceUsageError(f"manifest {location} must be a non-empty argv list")
    for index, part in enumerate(command):
        if not isinstance(part, str) or not part:
            raise AssuranceUsageError(f"manifest {location}[{index}] must be a non-empty string")
        if index == 0 and not part.strip():
            raise AssuranceUsageError(f"manifest {location}[0] must name an executable")
        if _COMMAND_CONTROL_RE.search(part):
            raise AssuranceUsageError(f"manifest {location}[{index}] contains a control character")


def _validate_accepted_findings(items: list[Any]) -> None:
    required = {"fingerprint", "reason", "owner", "scope", "expires"}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise AssuranceUsageError(f"accepted_findings[{idx}] must be an object")
        missing = [key for key in sorted(required) if not str(item.get(key) or "").strip()]
        if missing:
            raise AssuranceUsageError(f"accepted_findings[{idx}] missing {', '.join(missing)}")
        scope = _normalize_acceptance_scope(str(item["scope"]))
        if _blanket_acceptance_scope(scope):
            raise AssuranceUsageError(f"accepted_findings[{idx}] has a blanket scope")
        try:
            _parse_expiry(str(item["expires"]))
        except ValueError as exc:
            raise AssuranceUsageError(f"accepted_findings[{idx}] has an invalid expires: {item['expires']!r}") from exc


def _validate_generated_artifacts(items: list[Any]) -> None:
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise AssuranceUsageError(f"generated_artifacts[{idx}] must be an object")
        for key in ("id", "path"):
            if not str(item.get(key) or "").strip():
                raise AssuranceUsageError(f"generated_artifacts[{idx}] missing {key}")
        if str(item.get("kind") or "").strip():
            kind = _generated_kind(item.get("kind"))
            if kind not in EXECUTABLE_ARTIFACT_KINDS and kind != "other":
                raise AssuranceUsageError(f"generated_artifacts[{idx}].kind is unknown: {item.get('kind')}")
        executed = item.get("executed_by_tests", [])
        if executed is not None and not isinstance(executed, list):
            raise AssuranceUsageError(f"generated_artifacts[{idx}].executed_by_tests must be a list")


def _normalize_acceptance_scope(scope: str) -> str:
    normalized = _slash(scope.strip()).strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _blanket_acceptance_scope(scope: str) -> bool:
    normalized = scope.strip().strip("./").casefold()
    return not normalized or normalized in {"*", "**", "all", "global"} or "*" in normalized


def _generated_kind(value: Any) -> str:
    kind = str(value or "").strip().casefold()
    return GENERATED_KIND_ALIASES.get(kind, kind)


def _generated_artifact_is_executable(item: dict[str, Any]) -> bool:
    kind = _generated_kind(item.get("kind"))
    if kind in EXECUTABLE_ARTIFACT_KINDS:
        return True
    suffix = Path(str(item.get("path") or "")).suffix.casefold()
    return suffix in EXECUTABLE_ARTIFACT_EXTENSIONS


# --------------------------------------------------------------------------- detection


def _detect_one_root(repo_root: Path, root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    stacks: list[dict[str, Any]] = []
    markers: list[str]
    markers = _existing(root, ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "noxfile.py"])
    markers.extend(_glob_names(root, "requirements-*.txt"))
    if (root / "src").is_dir() or list(root.glob("*.py")):
        markers.append("python-files")
    if markers:
        stacks.append(_stack(repo_root, root, "python", markers, _python_posture(root)))
    markers = _existing(root, ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json"])
    if markers:
        stacks.append(_stack(repo_root, root, "js_ts", markers, _js_posture(root)))
    markers = _existing(root, ["go.mod", "go.sum"])
    if markers:
        posture = "deps_with_lock" if (root / "go.sum").exists() else "deps_no_lock"
        stacks.append(_stack(repo_root, root, "go", markers, posture))
    markers = _existing(root, ["Cargo.toml", "Cargo.lock"])
    if markers:
        posture = "deps_with_lock" if (root / "Cargo.lock").exists() else "deps_no_lock"
        stacks.append(_stack(repo_root, root, "rust", markers, posture))
    markers = []
    if (root / ".github" / "workflows").is_dir():
        markers.extend(_slash(_rel(root, p)) for p in sorted((root / ".github" / "workflows").glob("*.y*ml")))
    if list(root.glob("*.sh")):
        markers.append("shell-scripts")
    if markers:
        stacks.append(_stack(repo_root, root, "ci", markers, "unknown"))
    markers = _existing(root, ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"])
    markers.extend(_glob_names(root, "*.tf"))
    if markers:
        stacks.append(_stack(repo_root, root, "iac", markers, "unknown"))
    return stacks


def _stack(repo_root: Path, root: Path, stack_id: str, markers: list[str], posture: str) -> dict[str, Any]:
    return {
        "id": stack_id,
        "root": _slash(_rel(repo_root, root)),
        "confidence": "high" if len(markers) > 1 or stack_id == "python" else "medium",
        "markers": sorted(set(markers)),
        "dependency_posture": posture,
    }


def _existing(root: Path, names: list[str]) -> list[str]:
    return [name for name in names if (root / name).exists()]


def _glob_names(root: Path, pattern: str) -> list[str]:
    return sorted(_slash(_rel(root, p)) for p in root.glob(pattern))


def _python_posture(root: Path) -> str:
    if any((root / name).exists() for name in ("requirements.txt", "requirements.lock", "uv.lock", "Pipfile.lock")):
        return "deps_with_lock"
    if (root / "pyproject.toml").exists() or list(root.glob("requirements-*.txt")):
        return "deps_no_lock"
    return "stdlib_only"


def _js_posture(root: Path) -> str:
    if any((root / name).exists() for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")):
        return "deps_with_lock"
    return "deps_no_lock"


def _manifest_packages(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    monorepo = manifest.get("monorepo") if isinstance(manifest.get("monorepo"), dict) else {}
    packages = monorepo.get("packages", [])
    return [p for p in packages if isinstance(p, dict) and p.get("path")]


def _stack_ids(detection: dict[str, Any]) -> set[str]:
    stacks = list(detection.get("stacks") or []) + list(detection.get("monorepo_children") or [])
    return {str(stack.get("id")) for stack in stacks if isinstance(stack, dict)}


# --------------------------------------------------------------------------- plan helpers


def _profile(manifest: dict[str, Any], profile: str) -> dict[str, Any]:
    profiles = manifest.get("profiles") if isinstance(manifest.get("profiles"), dict) else {}
    value = profiles.get(profile, {})
    return value if isinstance(value, dict) else {}


def _tool_config(manifest: dict[str, Any], tool_id: str) -> dict[str, Any]:
    tools = manifest.get("tools") if isinstance(manifest.get("tools"), dict) else {}
    value = tools.get(tool_id, {})
    return value if isinstance(value, dict) else {}


def _is_required(manifest: dict[str, Any], profile: str, tool_id: str, default: bool = False) -> bool:
    profile_cfg = _profile(manifest, profile)
    required_tools = profile_cfg.get("required_tools", [])
    tool_cfg = _tool_config(manifest, tool_id)
    return bool(default or tool_id in required_tools or tool_cfg.get("required"))


def _network_allowed(manifest: dict[str, Any], profile: str, tool_id: str) -> bool:
    profile_cfg = _profile(manifest, profile)
    network_allowed = profile_cfg.get("network_allowed", {})
    if isinstance(network_allowed, dict):
        return bool(network_allowed.get(tool_id, False))
    return False


def _severity_floor(profile: str, manifest: dict[str, Any]) -> dict[str, str]:
    default = {
        "security": "medium",
        "deps": "medium",
        "secrets": "medium",
        "quality": "medium",
        "complexity": "high",
        "packaging": "medium",
        "encoding": "medium",
        "generated_artifact": "medium",
        "supply_chain": "medium",
        "other": "medium",
    }
    profile_cfg = _profile(manifest, profile)
    floors = profile_cfg.get("severity_floor", {})
    if isinstance(floors, dict):
        for dim, sev in floors.items():
            default[str(dim)] = _severity(str(sev))
    return default


def _ruff_applicable(root: Path, manifest: dict[str, Any], profile: str) -> bool:
    return (
        "ruff" in _profile(manifest, profile).get("required_tools", [])
        or "ruff" in manifest.get("tools", {})
        or "[tool.ruff" in _read_text_quiet(root / "pyproject.toml")
        or (root / "ruff.toml").exists()
    )


def _mypy_configured(root: Path, manifest: dict[str, Any], profile: str) -> bool:
    return (
        "mypy" in _profile(manifest, profile).get("required_tools", [])
        or "mypy" in manifest.get("tools", {})
        or (root / "mypy.ini").exists()
        or (root / ".mypy.ini").exists()
        or "[tool.mypy" in _read_text_quiet(root / "pyproject.toml")
    )


def _pyright_configured(root: Path, manifest: dict[str, Any], profile: str) -> bool:
    return (
        "pyright" in _profile(manifest, profile).get("required_tools", [])
        or "pyright" in manifest.get("tools", {})
        or (root / "pyrightconfig.json").exists()
        or "[tool.pyright" in _read_text_quiet(root / "pyproject.toml")
    )


def _semgrep_applicable(root: Path, manifest: dict[str, Any]) -> bool:
    return "semgrep" in manifest.get("tools", {}) or (root / ".semgrep").exists()


def _semgrep_config(root: Path, manifest: dict[str, Any]) -> str:
    cfg = _tool_config(manifest, "semgrep").get("config")
    if isinstance(cfg, str) and cfg.strip():
        return cfg
    return ".semgrep"


def _semgrep_requires_network(root: Path, manifest: dict[str, Any]) -> bool:
    cfg = _semgrep_config(root, manifest)
    if cfg.startswith(("http://", "https://")):
        return True
    path = Path(cfg)
    if path.is_absolute():
        return not path.exists()
    if cfg.startswith((".", "/", "\\")):
        return False
    return not (root / cfg).exists()


def _test_command(root: Path, manifest: dict[str, Any]) -> list[str] | None:
    custom = _custom_command(manifest, "test")
    if custom:
        return custom
    if (root / "tests").is_dir() and (root / "pyproject.toml").exists():
        return [sys.executable, "-m", "pytest", "-q"]
    return None


def _custom_command(manifest: dict[str, Any], key: str) -> list[str] | None:
    custom = manifest.get("custom_commands") if isinstance(manifest.get("custom_commands"), dict) else {}
    cmd = custom.get(key)
    if isinstance(cmd, list) and all(isinstance(part, str) and part for part in cmd):
        return list(cmd)
    return None


# --------------------------------------------------------------------------- runners


def _run_builtin(
    plan: ScanPlan, spec: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    start = time.monotonic()
    findings: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []
    built_in = spec["built_in"]
    try:
        if built_in == "manifest":
            findings.extend(_validation_findings(plan.manifest, "manifest-validate", "other"))
        elif built_in == "baseline":
            findings.extend(_validation_findings(plan.baseline, "baseline-validate", "other"))
        elif built_in == "provenance":
            findings.extend(_provenance_findings(plan.provenance))
        elif built_in == "encoding":
            findings.extend(_encoding_findings(plan.root, plan.manifest))
        elif built_in == "git_diff":
            if not _git_available(plan.root):
                return (
                    _run_record(
                        spec,
                        "skipped-not-applicable",
                        start,
                        command=["git", "diff", "--check"],
                    ),
                    findings,
                    [_risk("encoding", "git diff --check not applicable outside a git checkout", "medium")],
                )
            findings.extend(_git_diff_findings(plan.root))
        elif built_in == "generated":
            findings.extend(_generated_artifact_findings(plan.root, plan.manifest, plan.profile))
        elif built_in == "compile":
            findings.extend(_compile_findings(plan.root, plan.manifest))
        elif built_in == "complexity":
            findings.extend(_complexity_findings(plan.root, plan.manifest))
        elif built_in == "python_build":
            return _run_python_build(plan, spec, start)
        elif built_in == "twine_check":
            return _run_twine_check(plan, spec, start)
        elif built_in == "install_smoke":
            return _run_install_smoke(plan, spec, start)
        elif built_in == "import_smoke":
            findings.extend(_import_smoke_findings(plan.root, plan.manifest))
        elif built_in == "language_stub":
            lang = str(spec.get("language"))
            residual.append(
                _risk(
                    "security" if lang == "rust" else "quality",
                    f"{lang} detected; v1 records residual risk unless installed checks are configured",
                    "medium",
                )
            )
            return (
                _run_record(
                    spec,
                    "skipped-not-applicable",
                    start,
                    command=["agenttalk-assurance", "language-stub", lang],
                ),
                findings,
                residual,
            )
    except Exception as exc:  # noqa: BLE001 - per-check error must stay in artifact
        findings.append(
            _finding(
                tool_id=spec["tool_id"],
                rule_id="builtin-error",
                dimension=spec["dimension"],
                severity="high" if spec.get("required") else "medium",
                path=None,
                line=None,
                message=f"built-in check failed: {exc}",
            )
        )
    status = _pre_baseline_status(
        findings,
        required=bool(spec.get("required")),
        floor=_severity_floor(plan.profile, plan.manifest),
    )
    return _run_record(spec, status, start, command=["agenttalk-assurance", spec["tool_id"]]), findings, residual


def _run_external(
    root: Path,
    spec: dict[str, Any],
    command: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    start = time.monotonic()
    timeout = int(spec.get("timeout_seconds") or 60)
    raw = ""
    try:
        completed = subprocess.run(  # nosec B603 - argv list, shell disabled by default
            command,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        status = "error-required-tool" if spec.get("required") else "error-optional-tool"
        raw = f"{type(exc).__name__}: {exc}"
        run = _run_record(
            spec,
            status,
            start,
            command=command,
            exit_code=None,
            raw_log_pending=True,
        )
        return run, [], raw, ""
    except subprocess.TimeoutExpired as exc:
        status = "timeout-required" if spec.get("required") else "timeout-optional"
        stdout = _process_output_text(exc.stdout)
        stderr = _process_output_text(exc.stderr)
        raw = stdout + stderr
        run = _run_record(
            spec,
            status,
            start,
            command=command,
            exit_code=None,
            raw_log_pending=bool(raw),
        )
        return run, [], raw, stdout
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    raw = stdout + ("\n" if stdout and stderr else "") + stderr
    findings = _parse_tool_findings(spec, stdout, stderr)
    if findings:
        status = _pre_baseline_status(
            findings,
            required=bool(spec.get("required")),
            floor={spec["dimension"]: "medium"},
        )
    elif completed.returncode == 0:
        status = "pass"
    else:
        status = "error-required-tool" if spec.get("required") else "error-optional-tool"
    run = _run_record(
        spec,
        status,
        start,
        command=command,
        exit_code=completed.returncode,
        raw_log_pending=bool(raw),
    )
    return run, findings, raw, stdout


def _process_output_text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _coverage_recovery_reference(root: Path, transaction: Path) -> str:
    marker = transaction / _COVERAGE_RECOVERY_MARKER
    try:
        return _slash(str(marker.relative_to(root)))
    except ValueError:
        return _slash(str(marker))


def _coverage_recovery_directory_reference(root: Path, transaction: Path) -> str:
    try:
        return _slash(str(transaction.relative_to(root)))
    except ValueError:
        return _slash(str(transaction))


def _load_coverage_transaction(marker: Path) -> list[str]:
    try:
        is_regular = not marker.is_symlink() and marker.is_file()
    except OSError as exc:
        raise ValueError(f"unreadable transaction marker: {type(exc).__name__}") from exc
    if not is_regular:
        raise ValueError("transaction marker is not a regular file")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable transaction marker: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "artifacts"}:
        raise ValueError("transaction marker has an invalid shape")
    if value["schema_version"] != _COVERAGE_RECOVERY_SCHEMA:
        raise ValueError("transaction marker has an unsupported schema")
    artifacts = value["artifacts"]
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not all(isinstance(name, str) for name in artifacts)
        or len(artifacts) != len(set(artifacts))
        or any(name not in _COVERAGE_ARTIFACTS for name in artifacts)
    ):
        raise ValueError("transaction marker has invalid artifacts")
    return list(artifacts)


def _recover_coverage_transaction(root: Path, transaction: Path, artifacts: list[str]) -> str | None:
    marker = transaction / _COVERAGE_RECOVERY_MARKER
    for name in artifacts:
        target = root / name
        backup = transaction / name
        try:
            backup_exists = backup.exists() or backup.is_symlink()
            target_exists = target.exists() or target.is_symlink()
            backup_is_regular = not backup.is_symlink() and backup.is_file()
            target_is_regular = not target.is_symlink() and target.is_file()
        except OSError as exc:
            return f"could not inspect {name}: {type(exc).__name__}"
        if backup_exists:
            if not backup_is_regular:
                return f"quarantined {name} is not a regular file"
            if target_exists and not _remove_generated_coverage_artifact(target):
                return f"could not remove generated {name}"
            try:
                os.replace(backup, target)
            except OSError as exc:
                return f"could not restore {name}: {type(exc).__name__}"
        elif not target_exists or not target_is_regular:
            return f"both original and quarantined {name} are missing or invalid"
    try:
        leftovers = sorted(path.name for path in transaction.iterdir() if path != marker)
    except OSError as exc:
        return f"could not inspect recovered transaction: {type(exc).__name__}"
    if leftovers:
        return f"recovered transaction has unexpected files: {', '.join(leftovers)}"
    try:
        marker.unlink()
        transaction.rmdir()
    except OSError as exc:
        return f"could not remove recovered transaction: {type(exc).__name__}"
    return None


def _recover_coverage_transactions(root: Path) -> str | None:
    recovery_root = root / _COVERAGE_RECOVERY_DIR
    try:
        if not recovery_root.exists():
            return None
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            return "coverage recovery path is not a regular directory"
        entries = list(recovery_root.iterdir())
        invalid_entries = [path.name for path in entries if path.is_symlink() or not path.is_dir()]
        if invalid_entries:
            return f"coverage recovery directory has invalid entries: {', '.join(sorted(invalid_entries))}"
        transactions = sorted(entries)
    except OSError as exc:
        return f"could not inspect recovery directory: {type(exc).__name__}"
    for transaction in transactions:
        marker = transaction / _COVERAGE_RECOVERY_MARKER
        try:
            marker_exists = marker.exists() or marker.is_symlink()
        except OSError as exc:
            return (
                f"could not inspect recovery marker: {type(exc).__name__}; "
                f"recovery directory: {_coverage_recovery_directory_reference(root, transaction)}"
            )
        if not marker_exists:
            try:
                if any(transaction.iterdir()):
                    return (
                        "markerless recovery transaction is not empty; "
                        f"recovery directory: {_coverage_recovery_directory_reference(root, transaction)}"
                    )
                transaction.rmdir()
            except OSError as exc:
                return (
                    f"could not remove empty recovery transaction: {type(exc).__name__}; "
                    f"recovery directory: {_coverage_recovery_directory_reference(root, transaction)}"
                )
            continue
        try:
            artifacts = _load_coverage_transaction(marker)
        except ValueError as exc:
            return f"{exc}; recovery marker: {_coverage_recovery_reference(root, transaction)}"
        error = _recover_coverage_transaction(root, transaction, artifacts)
        if error is not None:
            return f"{error}; recovery marker: {_coverage_recovery_reference(root, transaction)}"
    try:
        recovery_root.rmdir()
    except OSError:
        pass
    return None


def _prepare_coverage_artifacts(root: Path) -> _CoverageArtifactState:
    """Quarantine prior reports so only artifacts created by this run are parsed."""
    recovery_error = _recover_coverage_transactions(root)
    if recovery_error is not None:
        return _CoverageArtifactState(set(), {}, None, recovery_error)

    eligible: set[str] = set()
    candidates: list[str] = []
    preparation_errors: list[str] = []
    for name in _COVERAGE_ARTIFACTS:
        path = root / name
        try:
            exists = path.exists() or path.is_symlink()
            is_regular = not path.is_symlink() and path.is_file()
        except OSError as exc:
            preparation_errors.append(f"could not inspect {name}: {type(exc).__name__}")
            continue
        if not exists:
            eligible.add(name)
        elif not is_regular:
            preparation_errors.append(f"pre-run {name} is not a regular file")
        else:
            candidates.append(name)

    backups: dict[str, Path] = {}
    quarantine: Path | None = None
    if candidates:
        recovery_root = root / _COVERAGE_RECOVERY_DIR
        try:
            recovery_root.mkdir(parents=True, exist_ok=True)
            quarantine = Path(tempfile.mkdtemp(prefix="transaction-", dir=recovery_root))
            marker = quarantine / _COVERAGE_RECOVERY_MARKER
            _atomic_write_text(
                marker,
                json.dumps(
                    {
                        "schema_version": _COVERAGE_RECOVERY_SCHEMA,
                        "artifacts": candidates,
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
        except OSError as exc:
            if quarantine is not None:
                try:
                    quarantine.rmdir()
                except OSError:
                    pass
            preparation_errors.append(f"could not create recovery transaction: {type(exc).__name__}")
            quarantine = None
        if quarantine is not None:
            for name in candidates:
                path = root / name
                backup = quarantine / name
                try:
                    os.replace(path, backup)
                except OSError as exc:
                    preparation_errors.append(f"could not quarantine {name}: {type(exc).__name__}")
                    continue
                backups[name] = backup
                eligible.add(name)

    return _CoverageArtifactState(
        eligible=eligible,
        backups=backups,
        quarantine=quarantine,
        preparation_error="; ".join(preparation_errors) or None,
    )


def _read_coverage_artifact(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(_COVERAGE_ARTIFACT_MAX_BYTES + 1)
        if len(data) > _COVERAGE_ARTIFACT_MAX_BYTES:
            return None
        return data.decode("utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return None


def _remove_generated_coverage_artifact(path: Path) -> bool:
    try:
        if not path.exists() and not path.is_symlink():
            return True
        if path.is_symlink() or path.is_file():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def _finish_coverage_artifacts(
    root: Path,
    state: _CoverageArtifactState,
) -> tuple[dict[str, str | None], bool, str | None]:
    """Capture fresh reports, then restore the pre-run worktree before attestation."""
    outputs: dict[str, str | None] = {}
    cleanup_errors = [state.preparation_error] if state.preparation_error is not None else []
    transaction_complete = True
    for name in _COVERAGE_ARTIFACTS:
        path = root / name
        if name in state.eligible:
            outputs[name] = _read_coverage_artifact(path)
            if not _remove_generated_coverage_artifact(path):
                cleanup_errors.append(f"could not remove generated {name}")
        backup = state.backups.get(name)
        if backup is None:
            continue
        try:
            if path.exists() or path.is_symlink():
                cleanup_errors.append(f"generated {name} still blocks restore")
                transaction_complete = False
                continue
            os.replace(backup, path)
        except OSError as exc:
            cleanup_errors.append(f"could not restore {name}: {type(exc).__name__}")
            transaction_complete = False
    if state.quarantine is not None and transaction_complete:
        marker = state.quarantine / _COVERAGE_RECOVERY_MARKER
        try:
            leftovers = [path for path in state.quarantine.iterdir() if path != marker]
        except OSError as exc:
            cleanup_errors.append(f"could not inspect recovery transaction: {type(exc).__name__}")
        else:
            if leftovers:
                cleanup_errors.append("recovery transaction still contains quarantined files")
            else:
                try:
                    marker.unlink()
                except OSError as exc:
                    cleanup_errors.append(f"could not remove recovery marker: {type(exc).__name__}")
                else:
                    try:
                        state.quarantine.rmdir()
                    except OSError as exc:
                        cleanup_errors.append(f"could not remove empty recovery transaction: {type(exc).__name__}")
    if not cleanup_errors:
        return outputs, True, None
    detail = f"coverage artifact cleanup failed ({'; '.join(cleanup_errors)})"
    if state.quarantine is not None:
        marker = state.quarantine / _COVERAGE_RECOVERY_MARKER
        try:
            marker_exists = marker.exists() or marker.is_symlink()
            quarantine_exists = state.quarantine.exists()
        except OSError:
            quarantine_exists = True
            marker_exists = False
        if marker_exists:
            detail += f"; recovery marker: {_coverage_recovery_reference(root, state.quarantine)}"
        elif quarantine_exists:
            detail += f"; recovery directory: {_coverage_recovery_directory_reference(root, state.quarantine)}"
    return outputs, False, detail


def _github_actions_evidence(
    root: Path,
    provenance: dict[str, Any],
    *,
    artifacts_clean: bool,
) -> str | None:
    """Return a revision-bound CI reference only for a clean pre/post Git state."""
    revision = str(provenance.get("git_sha") or "")
    github_sha = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if os.environ.get("CI", "").lower() != "true" or os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return None
    if not artifacts_clean or provenance.get("git_dirty") is not False:
        return None
    if not _GIT_SHA_RE.fullmatch(revision) or github_sha.lower() != revision.lower():
        return None
    if (
        not _ASCII_DIGITS_RE.fullmatch(run_id)
        or not _ASCII_DIGITS_RE.fullmatch(run_attempt)
        or not _GITHUB_REPOSITORY_RE.fullmatch(repository)
    ):
        return None
    current_revision = _git_output(root, ["rev-parse", "HEAD"])
    current_status = _git_output(root, ["status", "--porcelain"])
    if current_revision is None or current_status is None or current_status:
        return None
    if current_revision.lower() != revision.lower():
        return None
    return f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{run_attempt}"


def _coverage_attestation_key(plan: ScanPlan) -> tuple[str, str]:
    revision = str(plan.provenance.get("git_sha") or "").lower()
    return plan.profile, revision


def _invalidate_stale_coverage_gate(
    plan: ScanPlan,
    fresh_coverage_attestations: set[tuple[str, str]],
) -> None:
    if _coverage_attestation_key(plan) in fresh_coverage_attestations:
        return

    from agenttalk.store import Store

    name = f"coverage:{plan.profile}"
    revision = str(plan.provenance.get("git_sha") or "") or None
    reason = "no fresh coverage measurement this run"
    with Store(plan.root).config_lock():
        state = gates.load_gate_state(plan.root)
        existing = state.get("gates", {}).get(name)
        if not isinstance(existing, dict) or existing.get("status") == "waived":
            return
        if (
            existing.get("status") == "red"
            and existing.get("reason") == reason
            and existing.get("revision") == revision
        ):
            return
        gates.set_gate(
            plan.root,
            name=name,
            status="red",
            severity="blocker",
            scope=plan.profile,
            actor="assurance-finalizer",
            evidence_source="local_command",
            evidence=[f"assurance-run:{plan.run_id}"],
            reason=reason,
            revision=revision,
        )


def _emit_coverage_gate(
    plan: ScanPlan,
    run: dict[str, Any],
    stdout: str,
    artifact_outputs: dict[str, str | None],
    *,
    artifacts_clean: bool,
    artifact_error: str | None = None,
    failure_reason: str | None = None,
) -> None:
    from agenttalk.store import Store

    xml_text = artifact_outputs.get("coverage.xml")
    json_text = artifact_outputs.get("coverage.json")
    percent = parse_coverage_percent(stdout, xml_text=xml_text, json_text=json_text)
    command_succeeded = run.get("status") == "pass" and run.get("exit_code") == 0
    evidence_details = {"coverage_percent": float(percent)} if percent is not None else None
    with Store(plan.root).config_lock():
        # This is a point-in-time revision + clean-worktree attestation. A
        # mutation racing the check-to-persist gap is the accepted #66/#31
        # residual and is detected by close provenance/verify, not serialized here.
        ci_evidence = _github_actions_evidence(
            plan.root,
            plan.provenance,
            artifacts_clean=artifacts_clean,
        )
        is_green = command_succeeded and percent is not None and ci_evidence is not None
        evidence_source = "automation_ci" if ci_evidence is not None else "local_command"
        if failure_reason is not None:
            reason = failure_reason
        elif not command_succeeded:
            reason = "coverage command did not complete successfully"
        elif percent is None:
            reason = "coverage command succeeded but no overall percentage could be parsed"
        elif ci_evidence is None:
            reason = "coverage percentage is not bound to an attested clean CI revision"
        else:
            reason = "coverage command succeeded with a parsed, revision-bound CI measurement"
        if artifact_error is not None:
            reason = f"{reason}; {artifact_error}"
        evidence = [ci_evidence] if ci_evidence is not None else [f"assurance-run:{plan.run_id}"]
        gates.set_gate(
            plan.root,
            name=f"coverage:{plan.profile}",
            status="green" if is_green else "red",
            severity="blocker",
            scope=plan.profile,
            actor="assurance-ci" if ci_evidence is not None else "assurance-local",
            evidence_source=evidence_source,
            evidence=evidence,
            evidence_details=evidence_details,
            reason=reason,
            revision=str(plan.provenance.get("git_sha") or "") or None,
        )


def _run_record(
    spec: dict[str, Any],
    status: str,
    start: float,
    *,
    command: list[str],
    exit_code: int | None = 0,
    raw_log_pending: bool = False,
) -> dict[str, Any]:
    return {
        "tool_id": spec["tool_id"],
        "dimension": spec["dimension"],
        "command": [str(part) for part in command],
        "version": spec.get("version"),
        "status": status,
        "required": bool(spec.get("required")),
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - start) * 1000),
        "network_allowed": bool(spec.get("network_allowed", False)),
        "raw_log": "__pending__" if raw_log_pending else None,
    }


def _skipped(tool_id: str, status: str, reason: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "status": status,
        "required": bool(spec.get("required")),
        "reason": reason,
        "install_hint": spec.get("install_hint") or _tool_install_hint(tool_id),
    }


def _resolve_command(spec: dict[str, Any]) -> list[str] | None:
    explicit = spec.get("command")
    if explicit:
        return [str(part) for part in explicit]
    exe_name = str(spec.get("executable") or spec["tool_id"])
    exe = shutil.which(exe_name)
    if exe is None:
        return None
    spec["version"] = _tool_version(exe)
    return [exe, *[str(arg) for arg in spec.get("args", [])]]


def _tool_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [executable, "--version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    return text[0][:200] if text else None


def _tool_install_hint(tool_id: str) -> str | None:
    hints = {
        "ruff": "Install ruff or remove it from required_tools.",
        "ruff-format": "Install ruff or remove ruff-format from required_tools.",
        "bandit": "Install bandit for Python SAST evidence.",
        "semgrep": "Install semgrep to run project-local rules.",
        "gitleaks": "Install gitleaks for secrets evidence.",
        "osv-scanner": "Install osv-scanner, or configure pip-audit fallback.",
        "pip-audit": "Install pip-audit, or configure osv-scanner.",
        "mypy": "Install mypy or remove it from required_tools.",
        "pyright": "Install pyright or remove it from required_tools.",
        "twine": "Install twine for release artifact checks.",
    }
    return hints.get(tool_id)


# --------------------------------------------------------------------------- built-ins


def _validation_findings(data: dict[str, Any], tool_id: str, dimension: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for err in data.get("_validation_errors", []):
        findings.append(
            _finding(
                tool_id=tool_id,
                rule_id="schema",
                dimension=dimension,
                severity="high",
                path=data.get("_path"),
                line=None,
                message=str(err),
            )
        )
    return findings


def _provenance_findings(provenance: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in provenance.get("resolved_package_paths", []):
        if item.get("import_path") and not item.get("expected_under_root"):
            findings.append(
                _finding(
                    tool_id="provenance",
                    rule_id="import-outside-repo",
                    dimension="supply_chain",
                    severity="high",
                    path=item.get("import_path"),
                    line=None,
                    message=f"package {item.get('package')} resolved outside the scanned repo",
                )
            )
    if provenance.get("manifest_changed_in_scan_range"):
        findings.append(
            _finding(
                tool_id="provenance",
                rule_id="manifest-changed-in-range",
                dimension="supply_chain",
                severity="medium",
                path=provenance.get("manifest_path"),
                line=None,
                message="assurance manifest changed in the scanned range",
            )
        )
    if provenance.get("baseline_changed_in_scan_range"):
        findings.append(
            _finding(
                tool_id="provenance",
                rule_id="baseline-changed-in-range",
                dimension="supply_chain",
                severity="medium",
                path=provenance.get("baseline_path"),
                line=None,
                message="assurance baseline changed in the scanned range",
            )
        )
    return findings


def _encoding_findings(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    excludes = set(DEFAULT_EXCLUDES)
    paths_cfg = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    excludes.update(str(p).strip("/\\") for p in paths_cfg.get("exclude", []) if isinstance(p, str))
    vendor = [str(p).strip("/\\") for p in paths_cfg.get("vendor", []) if isinstance(p, str)]
    for file_path in _iter_files(root, excludes=excludes, excluded_relative=DEFAULT_EXCLUDED_RELATIVE | set(vendor)):
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            findings.append(
                _finding(
                    "encoding-hygiene",
                    "read-error",
                    "encoding",
                    "medium",
                    _slash(_rel(root, file_path)),
                    None,
                    str(exc),
                )
            )
            continue
        if not _is_text_like(file_path, data):
            continue
        rel = _slash(_rel(root, file_path))
        if b"\x00" in data:
            findings.append(
                _finding(
                    "encoding-hygiene",
                    "nul-byte",
                    "encoding",
                    "high",
                    rel,
                    None,
                    "NUL byte in source or text asset",
                )
            )
            continue
        bad_control = [byte for byte in data if byte < 32 and byte not in (9, 10, 13)]
        if bad_control:
            findings.append(
                _finding(
                    "encoding-hygiene",
                    "control-byte",
                    "encoding",
                    "high",
                    rel,
                    None,
                    "unexpected control byte in text file",
                )
            )
        if data.startswith(b"\xef\xbb\xbf"):
            findings.append(_finding("encoding-hygiene", "bom", "encoding", "low", rel, None, "UTF-8 BOM present"))
        if _mixed_eol(data):
            findings.append(
                _finding(
                    "encoding-hygiene", "mixed-eol", "encoding", "medium", rel, None, "mixed line endings in one file"
                )
            )
    return findings


def _git_diff_findings(root: Path) -> list[dict[str, Any]]:
    git = shutil.which("git")
    if git is None or not _git_available(root):
        return []
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [git, "diff", "--check"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            _finding(
                "git-diff-check",
                "tool-error",
                "encoding",
                "medium",
                None,
                None,
                f"git diff --check failed to run: {exc}",
            )
        ]
    if completed.returncode == 0:
        return []
    msg = (completed.stdout or completed.stderr or "git diff --check reported whitespace errors").strip()
    return [_finding("git-diff-check", "diff-check", "encoding", "high", None, None, msg[:500])]


def _generated_artifact_findings(root: Path, manifest: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in manifest.get("generated_artifacts", []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "")
        path = _resolve_under_root(root, rel)
        if not path.exists():
            findings.append(
                _finding(
                    "generated-artifacts",
                    "declared-missing",
                    "generated_artifact",
                    "high",
                    rel,
                    None,
                    "declared generated artifact is missing",
                )
            )
        executed = item.get("executed_by_tests") or []
        if profile == "release" and _generated_artifact_is_executable(item) and not executed:
            findings.append(
                _finding(
                    "generated-artifacts",
                    "declared-unexecuted",
                    "generated_artifact",
                    "high",
                    rel,
                    None,
                    "declared executable generated artifact has no executed test evidence",
                )
            )
    return findings


def _compile_findings(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    paths = _python_files(root, manifest)
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            line = exc.lineno if isinstance(exc, SyntaxError) else None
            findings.append(
                _finding("python-compileall", "syntax", "quality", "high", _slash(_rel(root, path)), line, str(exc))
            )
    return findings


def _complexity_findings(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = manifest.get("thresholds") if isinstance(manifest.get("thresholds"), dict) else {}
    complexity = thresholds.get("complexity", {}) if isinstance(thresholds.get("complexity"), dict) else {}
    max_lines = complexity.get("max_function_lines")
    if not isinstance(max_lines, int) or max_lines <= 0:
        return []
    findings: list[dict[str, Any]] = []
    for path in _python_files(root, manifest):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        current: tuple[str, int] | None = None
        for idx, line in enumerate(lines, start=1):
            if re.match(r"^\s*def\s+\w+\(", line):
                if current and idx - current[1] > max_lines:
                    findings.append(
                        _finding(
                            "complexity",
                            "function-lines",
                            "complexity",
                            "medium",
                            _slash(_rel(root, path)),
                            current[1],
                            f"function {current[0]} exceeds {max_lines} lines",
                        )
                    )
                name = line.split("def ", 1)[1].split("(", 1)[0]
                current = (name, idx)
        if current and len(lines) + 1 - current[1] > max_lines:
            findings.append(
                _finding(
                    "complexity",
                    "function-lines",
                    "complexity",
                    "medium",
                    _slash(_rel(root, path)),
                    current[1],
                    f"function {current[0]} exceeds {max_lines} lines",
                )
            )
    return findings


def _run_python_build(
    plan: ScanPlan,
    spec: dict[str, Any],
    start: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not (plan.root / "pyproject.toml").exists():
        return _run_record(spec, "skipped-not-applicable", start, command=["python", "-m", "build"]), [], []
    if _module_available("build"):
        findings: list[dict[str, Any]] = []
        try:
            with tempfile.TemporaryDirectory(prefix="agenttalk-assurance-build-") as td:
                command, completed, dists = _build_dist_into(plan.root, Path(td) / "dist")
        except Exception as exc:  # noqa: BLE001 - temp/build setup failure is evidence, not a crash
            findings.append(_finding("python-build", "build-error", "packaging", "high", None, None, str(exc)))
            return _run_record(spec, "fail-blocking", start, command=[sys.executable, "-m", "build"]), findings, []
        if completed.returncode != 0:
            findings.append(
                _finding(
                    "python-build",
                    "build-failed",
                    "packaging",
                    "high",
                    None,
                    None,
                    (completed.stderr or completed.stdout or "build failed")[:500],
                )
            )
        elif not dists:
            findings.append(
                _finding(
                    "python-build",
                    "build-no-artifacts",
                    "packaging",
                    "high",
                    None,
                    None,
                    "build produced no artifacts",
                )
            )
        status = "pass" if not findings else "fail-blocking"
        return _run_record(spec, status, start, command=command, exit_code=completed.returncode), findings, []
    if spec.get("required"):
        return _run_record(spec, "error-required-tool", start, command=[sys.executable, "-m", "build"]), [], []
    return (
        _run_record(spec, "skipped-not-installed", start, command=[sys.executable, "-m", "build"]),
        [],
        [_risk("packaging", "python build module is not installed", "medium", tool_id="python-build")],
    )


def _run_twine_check(
    plan: ScanPlan,
    spec: dict[str, Any],
    start: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not (plan.root / "pyproject.toml").exists():
        return _run_record(spec, "skipped-not-applicable", start, command=["twine", "check", "<dist>"]), [], []
    twine = shutil.which("twine")
    if twine is None:
        status = "error-required-tool" if spec.get("required") else "skipped-not-installed"
        residual = (
            []
            if spec.get("required")
            else [_risk("packaging", "twine is not installed", "medium", tool_id="twine-check")]
        )
        return _run_record(spec, status, start, command=["twine", "check", "<dist>"]), [], residual
    spec["version"] = _tool_version(twine)
    if not _module_available("build"):
        status = "error-required-tool" if spec.get("required") else "skipped-not-installed"
        residual = (
            []
            if spec.get("required")
            else [
                _risk(
                    "packaging", "python build module is not installed for twine check", "medium", tool_id="twine-check"
                )
            ]
        )
        return _run_record(spec, status, start, command=[twine, "check", "<dist>"]), [], residual
    findings: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="agenttalk-assurance-twine-") as td:
            _build_command, build_completed, dists = _build_dist_into(plan.root, Path(td) / "dist")
            if build_completed.returncode != 0 or not dists:
                findings.append(
                    _finding(
                        "twine-check",
                        "build-before-twine-failed",
                        "packaging",
                        "high",
                        None,
                        None,
                        (build_completed.stderr or build_completed.stdout or "build produced no artifacts")[:500],
                    )
                )
                return _run_record(spec, "fail-blocking", start, command=[twine, "check", "<dist>"]), findings, []
            command = [twine, "check", *[str(path) for path in dists]]
            completed = subprocess.run(  # nosec B603 - resolved executable, argv list
                command,
                cwd=plan.root,
                text=True,
                capture_output=True,
                timeout=int(spec.get("timeout_seconds") or 60),
                check=False,
            )
    except subprocess.TimeoutExpired:
        status = "timeout-required" if spec.get("required") else "timeout-optional"
        return _run_record(spec, status, start, command=[twine, "check", "<dist>"], exit_code=None), [], []
    except Exception as exc:  # noqa: BLE001
        findings.append(_finding("twine-check", "twine-error", "packaging", "high", None, None, str(exc)))
        return _run_record(spec, "fail-blocking", start, command=[twine, "check", "<dist>"]), findings, []
    if completed.returncode != 0:
        findings.append(
            _finding(
                "twine-check",
                "twine-check-failed",
                "packaging",
                "high",
                None,
                None,
                (completed.stderr or completed.stdout or "twine check failed")[:500],
            )
        )
    status = "pass" if not findings else "fail-blocking"
    return _run_record(spec, status, start, command=command, exit_code=completed.returncode), findings, []


def _run_install_smoke(
    plan: ScanPlan,
    spec: dict[str, Any],
    start: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="agenttalk-assurance-") as td:
            temp_root = Path(td)
            dists = sorted((plan.root / "dist").glob("*.whl")) + sorted((plan.root / "dist").glob("*.tar.gz"))
            if not dists:
                if not (plan.root / "pyproject.toml").exists():
                    return (
                        _run_record(
                            spec,
                            "skipped-not-applicable",
                            start,
                            command=[sys.executable, "-m", "venv", "<tmp>"],
                        ),
                        [],
                        [],
                    )
                if not _module_available("build"):
                    status = "error-required-tool" if spec.get("required") else "skipped-not-installed"
                    residual = (
                        []
                        if spec.get("required")
                        else [
                            _risk(
                                "packaging",
                                "python build module is not installed for install smoke",
                                "medium",
                                tool_id="install-smoke",
                            )
                        ]
                    )
                    return _run_record(spec, status, start, command=[sys.executable, "-m", "build"]), [], residual
                _build_command, build_completed, dists = _build_dist_into(plan.root, temp_root / "dist")
                if build_completed.returncode != 0 or not dists:
                    findings.append(
                        _finding(
                            "install-smoke",
                            "build-before-install-failed",
                            "packaging",
                            "high",
                            None,
                            None,
                            (build_completed.stderr or build_completed.stdout or "build produced no artifacts")[:500],
                        )
                    )
                    return (
                        _run_record(spec, "fail-blocking", start, command=[sys.executable, "-m", "build"]),
                        findings,
                        [],
                    )
            env_dir = Path(td) / "venv"
            venv.EnvBuilder(with_pip=True).create(env_dir)
            py = env_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
            command = [
                str(py),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(plan.root / "dist"),
                str(dists[-1]),
            ]
            completed = subprocess.run(  # nosec B603 - venv executable and argv list
                command,
                cwd=plan.root,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
    except Exception as exc:  # noqa: BLE001
        findings.append(_finding("install-smoke", "install-smoke-error", "packaging", "high", None, None, str(exc)))
        return _run_record(spec, "fail-blocking", start, command=["python", "-m", "venv", "<tmp>"]), findings, []
    if completed.returncode != 0:
        findings.append(
            _finding(
                "install-smoke",
                "install-smoke-failed",
                "packaging",
                "high",
                None,
                None,
                (completed.stderr or completed.stdout or "install failed")[:500],
            )
        )
    status = "pass" if not findings else "fail-blocking"
    return _run_record(spec, status, start, command=command, exit_code=completed.returncode), findings, []


def _build_dist_into(root: Path, out_dir: Path) -> tuple[list[str], subprocess.CompletedProcess[str], list[Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(out_dir)]
    completed = subprocess.run(  # nosec B603 - sys.executable and argv list
        command,
        cwd=root,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    dists = sorted(out_dir.glob("*.whl")) + sorted(out_dir.glob("*.tar.gz"))
    return command, completed, dists


def _import_smoke_findings(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for package in _packages_to_resolve(root, manifest):
        resolved = _resolve_package(root, package)
        if not resolved.get("import_path"):
            findings.append(
                _finding(
                    "import-smoke", "import-failed", "packaging", "high", None, None, f"could not import {package}"
                )
            )
    return findings


def _module_available(name: str) -> bool:
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
    try:
        completed = subprocess.run(  # nosec B603 - sys.executable and argv list
            [sys.executable, "-c", code, name],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


# --------------------------------------------------------------------------- parsing and findings


def _parse_tool_findings(spec: dict[str, Any], stdout: str, stderr: str) -> list[dict[str, Any]]:
    text = stdout.strip() or stderr.strip()
    if not text:
        return []
    data = _loads_json_loose(text)
    if data is None:
        return []
    tool_id = spec["tool_id"]
    dimension = spec["dimension"]
    findings: list[dict[str, Any]] = []
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        for raw in data["findings"]:
            if isinstance(raw, dict):
                findings.append(_finding_from_generic(tool_id, dimension, raw))
    elif isinstance(data, dict) and isinstance(data.get("results"), list):
        if tool_id == "osv-scanner":
            findings.extend(_osv_findings(data))
        elif tool_id == "pip-audit":
            findings.extend(_pip_audit_findings(data))
        else:
            for raw in data["results"]:
                if isinstance(raw, dict):
                    findings.append(_finding_from_generic(tool_id, dimension, raw))
    elif isinstance(data, dict) and isinstance(data.get("vulnerabilities"), list):
        findings.extend(_pip_audit_findings(data))
    elif isinstance(data, list):
        for raw in data:
            if isinstance(raw, dict):
                findings.append(_finding_from_generic(tool_id, dimension, raw))
    return findings


def _loads_json_loose(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _finding_from_generic(tool_id: str, dimension: str, raw: dict[str, Any]) -> dict[str, Any]:
    rule_id = str(
        raw.get("rule_id")
        or raw.get("test_id")
        or raw.get("RuleID")
        or raw.get("check_id")
        or raw.get("id")
        or "finding"
    )
    path = raw.get("path") or raw.get("filename") or raw.get("File") or raw.get("file")
    line = raw.get("line") or raw.get("line_number") or raw.get("StartLine")
    message = str(
        raw.get("message") or raw.get("issue_text") or raw.get("Description") or raw.get("summary") or rule_id
    )
    severity = _severity(str(raw.get("severity") or raw.get("issue_severity") or raw.get("Severity") or "medium"))
    return _finding(tool_id, rule_id, dimension, severity, path, _int_or_none(line), message)


def _osv_findings(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in data.get("results", []):
        if not isinstance(result, dict):
            continue
        for package in result.get("packages", []):
            if not isinstance(package, dict):
                continue
            pkg = package.get("package", {}) if isinstance(package.get("package"), dict) else {}
            name = pkg.get("name") or package.get("name") or "unknown"
            ecosystem = pkg.get("ecosystem") or package.get("ecosystem") or "unknown"
            version = package.get("version") or pkg.get("version") or "unknown"
            for vuln in package.get("vulnerabilities", []):
                if not isinstance(vuln, dict):
                    continue
                advisory = str(vuln.get("id") or vuln.get("database_specific", {}).get("cwe_ids", ["vulnerability"])[0])
                msg = str(vuln.get("summary") or f"{ecosystem} package {name} {version} has {advisory}")
                out.append(_finding("osv-scanner", advisory, "deps", _osv_severity(vuln), None, None, msg))
    return out


def _osv_severity(vuln: dict[str, Any]) -> str:
    severity = vuln.get("severity")
    if isinstance(severity, str):
        return _severity(severity)
    if isinstance(severity, list):
        ranked = [
            parsed
            for item in severity
            if isinstance(item, dict)
            for parsed in (_cvss_score_to_severity(item.get("score")),)
            if parsed
        ]
        if ranked:
            return max(ranked, key=_severity_rank)
    return "high"


def _cvss_score_to_severity(score: Any) -> str | None:
    text = str(score or "").strip()
    if text.upper().startswith("CVSS:"):
        return None
    match = re.search(r"(?<!\d)(10(?:\.0)?|[0-9](?:\.[0-9])?)(?!\d)", text)
    if not match:
        return None
    value = float(match.group(1))
    if value >= 9.0:
        return "critical"
    if value >= 7.0:
        return "high"
    if value >= 4.0:
        return "medium"
    if value > 0:
        return "low"
    return "info"


def _pip_audit_findings(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dep in data.get("dependencies", []):
        if not isinstance(dep, dict):
            continue
        name = dep.get("name") or "unknown"
        version = dep.get("version") or "unknown"
        for vuln in dep.get("vulns", []):
            if isinstance(vuln, dict):
                vid = str(vuln.get("id") or "vulnerability")
                out.append(
                    _finding(
                        "pip-audit",
                        vid,
                        "deps",
                        "high",
                        None,
                        None,
                        f"{name} {version}: {vuln.get('description') or vid}",
                    )
                )
    for vuln in data.get("vulnerabilities", []):
        if isinstance(vuln, dict):
            vid = str(vuln.get("id") or vuln.get("vulnerability_id") or "vulnerability")
            out.append(_finding("pip-audit", vid, "deps", "high", None, None, str(vuln.get("description") or vid)))
    return out


def _finding(
    tool_id: str,
    rule_id: str,
    dimension: str,
    severity: str,
    path: Any,
    line: int | None,
    message: str,
) -> dict[str, Any]:
    severity = _severity(severity)
    path_value = str(path) if path is not None else None
    message_value = str(message).strip() or rule_id
    return {
        "fingerprint": finding_fingerprint(tool_id, rule_id, path_value, message_value),
        "status": "new",
        "dimension": str(dimension),
        "severity": severity,
        "tool_id": str(tool_id),
        "rule_id": str(rule_id),
        "path": path_value,
        "line": line,
        "message": message_value,
        "raw_ref": None,
        "blocking": False,
    }


def finding_fingerprint(tool_id: str, rule_id: str, path: str | None, message: str, extra: str = "") -> str:
    norm = "|".join(
        [
            str(tool_id).casefold(),
            str(rule_id).casefold(),
            _slash(path or "").casefold(),
            re.sub(r"\s+", " ", str(message).strip()).casefold(),
            str(extra).casefold(),
        ]
    )
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]


def _normalize_finding(finding: dict[str, Any]) -> dict[str, Any]:
    item = dict(finding)
    item["severity"] = _severity(str(item.get("severity") or "medium"))
    item["dimension"] = str(item.get("dimension") or "other")
    item["tool_id"] = str(item.get("tool_id") or "unknown")
    item["rule_id"] = str(item.get("rule_id") or "finding")
    item["message"] = str(item.get("message") or item["rule_id"])
    if not item.get("fingerprint"):
        item["fingerprint"] = finding_fingerprint(item["tool_id"], item["rule_id"], item.get("path"), item["message"])
    item["line"] = _int_or_none(item.get("line"))
    item["raw_ref"] = item.get("raw_ref")
    return item


def _severity(value: str) -> str:
    lower = value.strip().casefold()
    aliases = {"warn": "medium", "warning": "medium", "error": "high", "blocker": "critical"}
    lower = aliases.get(lower, lower)
    return lower if lower in SEVERITY_RANK else "medium"


def _pre_baseline_status(findings: list[dict[str, Any]], *, required: bool, floor: dict[str, str]) -> str:
    if not findings:
        return "pass"
    blocking = any(
        _severity_rank(f["severity"]) >= _severity_rank(floor.get(f.get("dimension"), "medium")) for f in findings
    )
    return "fail-blocking" if blocking or required else "fail-advisory"


def _finding_blocks(item: dict[str, Any], floor_by_dim: dict[str, str]) -> bool:
    if item["status"] == "accepted-applied":
        return False
    if item["status"] in {"fixed", "unchanged"}:
        return False
    floor = floor_by_dim.get(item["dimension"], floor_by_dim.get("other", "medium"))
    return _severity_rank(item["severity"]) >= _severity_rank(floor)


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(_severity(severity), SEVERITY_RANK["medium"])


def _normalize_applied_finding(finding: dict[str, Any], floor_by_dim: dict[str, str]) -> dict[str, Any]:
    item = _normalize_finding(finding)
    item["status"] = "new"
    item["blocking"] = _finding_blocks(item, floor_by_dim)
    return item


def _executed_tools(result: ScanResult) -> set[str]:
    return {str(run.get("tool_id")) for run in result.tools_run if str(run.get("status")) in EXECUTED_STATUSES}


def _baseline_tool_id(item: dict[str, Any]) -> str:
    return str(item.get("tool") or item.get("tool_id") or "baseline")


def _acceptance_applies(acceptance: dict[str, Any], finding: dict[str, Any]) -> bool:
    dimension = acceptance.get("dimension")
    if dimension and str(dimension) != str(finding.get("dimension")):
        return False
    path = _slash(str(finding.get("path") or "")).strip()
    if not path:
        return True
    scope = _normalize_acceptance_scope(str(acceptance.get("scope") or ""))
    scope = scope.rstrip("/")
    return path == scope or path.startswith(scope + "/")


def _finding_sort_key(item: dict[str, Any]) -> tuple[str, str, str, str, int, str]:
    return (
        str(item.get("dimension") or ""),
        str(item.get("tool_id") or ""),
        str(item.get("rule_id") or ""),
        str(item.get("path") or ""),
        _int_or_none(item.get("line")) or 0,
        str(item.get("fingerprint") or ""),
    )


def _is_worse(current: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return _severity_rank(current["severity"]) > _severity_rank(str(baseline.get("severity") or "info"))


def _acceptance_expired(item: dict[str, Any]) -> bool:
    try:
        return _parse_expiry(str(item.get("expires"))) < datetime.now(timezone.utc)
    except ValueError:
        return True


def _parse_expiry(value: str) -> datetime:
    raw = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recompute_tool_statuses(result: ScanResult) -> None:
    blocking_by_tool = {finding["tool_id"] for finding in result.findings if finding.get("blocking")}
    advisory_by_tool = {
        finding["tool_id"]
        for finding in result.findings
        if finding["status"] in {"new", "worsened", "accepted-expired"} and not finding.get("blocking")
    }
    for run in result.tools_run:
        status = run["status"]
        if status.startswith("error-") or status.startswith("timeout-") or status.startswith("skipped-"):
            continue
        tool_id = run["tool_id"]
        if tool_id in blocking_by_tool:
            run["status"] = "fail-blocking"
        elif tool_id in advisory_by_tool:
            run["status"] = "fail-advisory"
        else:
            run["status"] = "pass"


# --------------------------------------------------------------------------- artifact


def _artifact_from_result(result: ScanResult, changed_files: list[str]) -> dict[str, Any]:
    summary = _verdict_summary(result)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "run_id": result.run_id,
        "generated_at": result.generated_at,
        "profile": result.profile,
        "root": str(result.root),
        "scanner": {
            "skill_name": "assurance-scan",
            "runner_version": RUNNER_VERSION,
            "runner_path": str(Path(__file__).resolve()),
        },
        "provenance": result.provenance,
        "detection": result.detection,
        "tools": {
            "considered": result.tools_considered,
            "run": result.tools_run,
            "skipped": result.tools_skipped,
            "required_missing": result.required_missing,
        },
        "findings": result.findings,
        "accepted_findings": result.accepted_findings,
        "native_suppressions": result.native_suppressions,
        "attestation": _derive_attestation(result),
        "verdict_summary": summary,
        "residual_risk": _dedupe_risks(result.residual_risk),
    }


def _verdict_summary(result: ScanResult) -> dict[str, Any]:
    blocking = [f for f in result.findings if f.get("blocking")]
    advisory = [
        f
        for f in result.findings
        if f.get("status") in {"new", "worsened", "accepted-expired"} and not f.get("blocking")
    ]
    skipped_required = list(result.required_missing)
    skipped_optional = [
        item
        for item in result.tools_skipped
        if not item.get("required")
        and item.get("status")
        in {"skipped-not-installed", "skipped-network-disabled", "error-optional-tool", "timeout-optional"}
    ]
    skipped_optional.extend(
        item
        for item in result.tools_run
        if not item.get("required") and item.get("status") in {"error-optional-tool", "timeout-optional"}
    )
    prov = result.provenance
    return {
        "blocking_findings_count": len(blocking),
        "advisory_findings_count": len(advisory),
        "skipped_required_count": len(skipped_required),
        "skipped_optional_count": len(skipped_optional),
        "manifest_self_waiver_risk": bool(
            prov.get("manifest_changed_in_scan_range") or prov.get("baseline_changed_in_scan_range")
        ),
        "runner_errors": list(result.runner_errors),
    }


def _derive_attestation(result: ScanResult) -> dict[str, Any]:
    reasons: list[str] = []

    def tool_records(tool_ids: set[str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for run in result.tools_run:
            if run["tool_id"] in tool_ids:
                records.append(run)
        for skipped in result.tools_skipped:
            if skipped["tool_id"] in tool_ids:
                records.append(skipped)
        return records

    def has_required_problem(records: list[dict[str, Any]]) -> bool:
        return any(
            str(record.get("status")) in {"error-required-tool", "timeout-required"}
            or (str(record.get("status")).startswith("skipped-") and bool(record.get("required")))
            for record in records
        )

    def has_blocking(dimensions: set[str]) -> bool:
        return any(
            finding.get("blocking")
            and (
                finding.get("dimension") in dimensions
                or finding.get("tool_id") in {"manifest-validate", "baseline-validate"}
            )
            for finding in result.findings
        )

    def evidence_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            record
            for record in records
            if str(record.get("status")) in EXECUTED_STATUSES and not _vacuous_attestation_record(result, record)
        ]

    def assess_any(name: str, tools: set[str], dimensions: set[str], required: bool = False) -> str:
        records = tool_records(tools)
        if has_required_problem(records):
            reasons.append(f"{name}: required scan skipped or errored")
            return "unknown"
        if has_blocking(dimensions):
            reasons.append(f"{name}: blocking finding present")
            return "unknown"
        if evidence_records(records):
            return "good"
        if required:
            reasons.append(f"{name}: no executed evidence")
            return "unknown"
        return "not_assessed"

    def assess_dimensions(
        name: str, tools: set[str], dimensions: set[str], required_dimensions: tuple[str, ...]
    ) -> str:
        records = tool_records(tools)
        if has_required_problem(records):
            reasons.append(f"{name}: required scan skipped or errored")
            return "unknown"
        if has_blocking(dimensions):
            reasons.append(f"{name}: blocking finding present")
            return "unknown"
        evidence = evidence_records(records)
        missing = [
            dimension
            for dimension in required_dimensions
            if not any(record.get("dimension") == dimension for record in evidence)
        ]
        if missing:
            reasons.append(f"{name}: missing executed {', '.join(missing)} evidence")
            return "unknown"
        return "good"

    good = assess_any(
        "GOOD",
        {"encoding-hygiene", "git-diff-check", "python-compileall", "ruff", "ruff-format", "tests", "complexity"},
        {"encoding", "quality", "complexity"},
        required=True,
    )
    robust = assess_any(
        "ROBUST",
        {"tests", "coverage", "generated-artifacts", "mypy", "pyright"},
        {"quality", "generated_artifact"},
        required=False,
    )
    secure = assess_dimensions(
        "SECURE",
        SECURITY_TOOLS,
        {"security", "deps", "secrets", "supply_chain"},
        ("security", "deps", "secrets"),
    )
    if "rust" in _stack_ids(result.detection) and secure == "good":
        secure = "unknown"
        reasons.append("SECURE: Rust detected; clippy/basic checks alone are not Rust SAST evidence")
    return {"GOOD": good, "ROBUST": robust, "SECURE": secure, "reasons": reasons}


def _summary_text(artifact: dict[str, Any]) -> str:
    summary = artifact.get("verdict_summary", {})
    att = artifact.get("attestation", {})
    return (
        "# Assurance Scan Summary\n\n"
        f"- run_id: `{artifact.get('run_id')}`\n"
        f"- profile: `{artifact.get('profile')}`\n"
        f"- blocking_findings_count: `{summary.get('blocking_findings_count', 0)}`\n"
        f"- advisory_findings_count: `{summary.get('advisory_findings_count', 0)}`\n"
        f"- skipped_required_count: `{summary.get('skipped_required_count', 0)}`\n"
        f"- GOOD: `{att.get('GOOD')}`\n"
        f"- ROBUST: `{att.get('ROBUST')}`\n"
        f"- SECURE: `{att.get('SECURE')}`\n"
    )


def _vacuous_attestation_record(result: ScanResult, record: dict[str, Any]) -> bool:
    if record.get("tool_id") == "generated-artifacts":
        return not bool(result.manifest.get("generated_artifacts"))
    return False


# --------------------------------------------------------------------------- filesystem/git helpers


def _resolve_under_root(root: Path, path: Path | str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    return (root / p).resolve()


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _slash(value: str) -> str:
    return str(value).replace("\\", "/")


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "tool"


def _sha256_file(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _git_available(root: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _git_output(root: Path, args: list[str]) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [git, *args],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _changed_files(root: Path, *, changed_from: str | None, changed_to: str | None) -> list[str]:
    git = shutil.which("git")
    if git is None:
        return []
    files: set[str] = set()
    args = [git, "diff", "--name-only"]
    if changed_from:
        args.append(changed_from)
        if changed_to:
            args.append(changed_to)
    elif changed_to:
        args.append(changed_to)
    else:
        args.append("HEAD")
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            args,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode == 0:
        files.update(_slash(line.strip()) for line in completed.stdout.splitlines() if line.strip())
    files.update(_git_status_changed_files(root))
    return sorted(files)


def _git_status_changed_files(root: Path) -> set[str]:
    git = shutil.which("git")
    if git is None:
        return set()
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [git, "status", "--porcelain=v1", "-z", "-uall"],
            cwd=root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode != 0:
        return set()
    out: set[str] = set()
    entries = [entry for entry in completed.stdout.split(b"\0") if entry]
    idx = 0
    while idx < len(entries):
        entry = entries[idx].decode("utf-8", errors="replace")
        if len(entry) >= 4:
            path = entry[3:]
            if path:
                out.add(_slash(path))
        if entry[:1] in {"R", "C"} or entry[1:2] in {"R", "C"}:
            idx += 2
        else:
            idx += 1
    return out


def _packages_to_resolve(root: Path, manifest: dict[str, Any]) -> list[str]:
    py = manifest.get("python") if isinstance(manifest.get("python"), dict) else {}
    packages: list[str] = []
    if isinstance(py.get("package"), str):
        packages.append(py["package"])
    if isinstance(py.get("packages"), list):
        packages.extend(str(item) for item in py["packages"] if isinstance(item, str))
    pyproject = _read_text_quiet(root / "pyproject.toml")
    match = re.search(r"(?m)^name\s*=\s*[\"']([^\"']+)[\"']", pyproject)
    if match:
        packages.append(match.group(1).replace("-", "_"))
    src = root / "src"
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists():
                packages.append(child.name)
    deduped: list[str] = []
    for package in packages:
        safe = re.sub(r"[^A-Za-z0-9_.]", "_", package).strip("_")
        if safe and safe not in deduped:
            deduped.append(safe)
    return deduped[:5]


def _resolve_package(root: Path, package: str) -> dict[str, Any]:
    code = (
        "import importlib.util, pathlib, sys; "
        "spec=importlib.util.find_spec(sys.argv[1]); "
        "locations = None if spec is None else spec.submodule_search_locations; "
        "path = None if spec is None else (spec.origin or (locations and list(locations)[0])); "
        "print(path or '')"
    )
    env = os.environ.copy()
    extra = os.pathsep.join([str(root / "src"), str(root)])
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        completed = subprocess.run(  # nosec B603 - sys.executable and argv list
            [sys.executable, "-c", code, package],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"package": package, "import_path": None, "expected_under_root": False}
    import_path = completed.stdout.strip() if completed.returncode == 0 else ""
    expected = False
    if import_path:
        try:
            Path(import_path).resolve().relative_to(root.resolve())
            expected = True
        except ValueError:
            expected = False
    return {"package": package, "import_path": import_path or None, "expected_under_root": expected}


def _iter_files(root: Path, *, excludes: set[str], excluded_relative: set[str]) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if rel_parts and rel_parts[0] in excludes and not (root / rel_parts[0] / "__init__.py").exists():
            continue
        rel = _slash(_rel(root, path))
        if any(rel == item or rel.startswith(item.rstrip("/") + "/") for item in excluded_relative):
            continue
        out.append(path)
    return out


def _is_text_like(path: Path, data: bytes) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return False
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _mixed_eol(data: bytes) -> bool:
    crlf = data.count(b"\r\n")
    without_crlf = data.replace(b"\r\n", b"")
    return crlf > 0 and (b"\n" in without_crlf or b"\r" in without_crlf)


def _python_files(root: Path, manifest: dict[str, Any]) -> list[Path]:
    paths_cfg = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    excludes = set(DEFAULT_EXCLUDES)
    excludes.update(str(p).strip("/\\") for p in paths_cfg.get("exclude", []) if isinstance(p, str))
    return [
        path
        for path in _iter_files(root, excludes=excludes, excluded_relative=DEFAULT_EXCLUDED_RELATIVE)
        if path.suffix == ".py"
    ]


def _read_text_quiet(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _risk(dimension: str, reason: str, severity: str, *, tool_id: str | None = None) -> dict[str, Any]:
    item = {"dimension": dimension, "reason": reason, "severity": _severity(severity)}
    if tool_id:
        item["tool_id"] = tool_id
    return item


def _dedupe_risks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("dimension")), str(item.get("reason")))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
