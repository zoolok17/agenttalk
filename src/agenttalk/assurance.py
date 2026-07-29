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
import stat
import subprocess  # nosec B404 - scanner runs fixed argv lists with shell disabled
import sys
import tempfile
import time
import uuid
import venv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agenttalk import __version__, gates
from agenttalk.coverage_contract import ASSURANCE_PROFILES, coverage_gate_name
from agenttalk.coverage_parse import parse_coverage_percent


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "assurance-scan-run"
RUNNER_VERSION = f"agenttalk-{__version__}"

DEFAULT_MANIFEST = Path(".agenttalk") / "assurance.json"
DEFAULT_BASELINE = Path(".agenttalk") / "assurance" / "baseline.json"
DEFAULT_RUNS_DIR = Path(".agenttalk") / "assurance" / "runs"

PROFILES = ASSURANCE_PROFILES
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
# This is the complete class of report paths that the coverage measurement
# discovers. Arbitrary configured outputs, and case variants on case-sensitive
# filesystems, are outside this boundary.
_COVERAGE_ARTIFACTS = ("coverage.xml", "coverage.json")
_LEGACY_COVERAGE_RECOVERY_DIR = Path(".agenttalk") / "assurance" / "coverage-recovery"
_GIT_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")
_GITHUB_REPOSITORY_RE = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_ASCII_DIGITS_RE = re.compile(r"\A[0-9]+\Z")
_COMMAND_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NO_COVERAGE_GATE_CAS = object()
_NO_PRECOMPUTED_CI_EVIDENCE = object()


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
        "_source_sha256": None,
        "_validation_errors": [],
    }


def _default_baseline(path: Path | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": "none",
        "findings": [],
        "_path": str(path or DEFAULT_BASELINE),
        "_source_sha256": None,
        "_validation_errors": [],
    }


def _read_selected_policy_bytes(path: Path, *, label: str) -> bytes:
    """Read one selected policy generation without following a link."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    try:
        before = os.lstat(path)
        fd = os.open(str(path), os.O_RDONLY | binary | nofollow)
    except OSError as exc:
        raise AssuranceUsageError(f"could not read {label}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _reparse_stat(opened) or not os.path.samestat(before, opened):
            raise AssuranceUsageError(f"{label} path changed or is not a regular, non-reparse file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = os.lstat(path)
        if not stat.S_ISREG(current.st_mode) or _reparse_stat(current) or not os.path.samestat(opened, current):
            raise AssuranceUsageError(f"{label} path changed while it was being read: {path}")
        return b"".join(chunks)
    except OSError as exc:
        raise AssuranceUsageError(f"could not read {label}: {exc}") from exc
    finally:
        os.close(fd)


def load_manifest(root: Path | str, path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate `.agenttalk/assurance.json`.

    Missing manifests are allowed and become a conservative default. Malformed
    manifests raise :class:`AssuranceUsageError`; the CLI catches that and emits
    a blocking validation finding in the artifact.
    """
    root_path = Path(root).resolve()
    manifest_path = _resolve_selected_path(root_path, path or DEFAULT_MANIFEST)
    if not _selected_policy_file_present(
        root_path,
        manifest_path,
        label="assurance manifest",
    ):
        return _default_manifest(manifest_path)
    try:
        source = _read_selected_policy_bytes(
            manifest_path,
            label="assurance manifest",
        )
        data = json.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceUsageError(f"malformed assurance manifest: {exc}") from exc
    manifest = _normalize_manifest(data, manifest_path)
    manifest["_source_sha256"] = hashlib.sha256(source).hexdigest()
    return manifest


def load_baseline(root: Path | str, path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate `.agenttalk/assurance/baseline.json`."""
    root_path = Path(root).resolve()
    baseline_path = _resolve_selected_path(root_path, path or DEFAULT_BASELINE)
    if not _selected_policy_file_present(
        root_path,
        baseline_path,
        label="assurance baseline",
    ):
        return _default_baseline(baseline_path)
    try:
        source = _read_selected_policy_bytes(
            baseline_path,
            label="assurance baseline",
        )
        data = json.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceUsageError(f"malformed assurance baseline: {exc}") from exc
    baseline = _normalize_baseline(data, baseline_path)
    baseline["_source_sha256"] = hashlib.sha256(source).hexdigest()
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
    manifest_path = _resolve_selected_path(
        root_path,
        manifest.get("_path") or DEFAULT_MANIFEST,
    )
    baseline_path = _resolve_selected_path(
        root_path,
        baseline.get("_path") or DEFAULT_BASELINE,
    )
    manifest_rel = _slash(_lexical_rel(root_path, manifest_path))
    baseline_rel = _slash(_lexical_rel(root_path, baseline_path))
    manifest_source_sha = manifest.get("_source_sha256")
    baseline_source_sha = baseline.get("_source_sha256")
    manifest_source_sha = manifest_source_sha if isinstance(manifest_source_sha, str) else None
    baseline_source_sha = baseline_source_sha if isinstance(baseline_source_sha, str) else None
    git_sha = _git_output(root_path, ["rev-parse", "HEAD"])
    git_dirty = _git_worktree_dirty(
        root_path,
        protected_paths=(manifest_rel, baseline_rel),
        protected_hashes=(manifest_source_sha, baseline_source_sha),
    )
    changed_files = _changed_files(root_path, changed_from=changed_from, changed_to=changed_to)
    packages = _packages_to_resolve(root_path, manifest)
    resolved = [_resolve_package(root_path, package) for package in packages]
    return {
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "changed_from": changed_from,
        "changed_to": changed_to,
        "changed_files": changed_files,
        "manifest_path": manifest_rel,
        "manifest_sha256": manifest_source_sha,
        "manifest_changed_in_scan_range": manifest_rel in changed_files,
        "baseline_path": baseline_rel,
        "baseline_sha256": baseline_source_sha,
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
    coverage_gate_snapshot = _snapshot_coverage_gate(plan.root, plan.profile)
    fresh_coverage_attestations: set[tuple[str, str]] = set()
    try:
        result = _run_plan(
            plan,
            fresh_coverage_attestations,
            coverage_gate_snapshot,
        )
    except BaseException as scan_exc:
        try:
            _invalidate_stale_coverage_gate(
                root=plan.root,
                profile=plan.profile,
                revision=plan.provenance.get("git_sha"),
                run_id=plan.run_id,
                fresh_coverage_attestations=fresh_coverage_attestations,
                expected_gate=coverage_gate_snapshot,
            )
        except Exception as finalization_exc:
            raise scan_exc from finalization_exc
        raise
    else:
        _invalidate_stale_coverage_gate(
            root=plan.root,
            profile=plan.profile,
            revision=plan.provenance.get("git_sha"),
            run_id=plan.run_id,
            fresh_coverage_attestations=fresh_coverage_attestations,
            expected_gate=coverage_gate_snapshot,
        )
        return result


def _run_plan(
    plan: ScanPlan,
    fresh_coverage_attestations: set[tuple[str, str]],
    coverage_gate_snapshot: dict[str, Any] | None,
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
        if tool_id == "coverage":
            run, ext_findings, raw, stdout = _run_coverage_external(
                plan,
                spec,
                command,
                fresh_coverage_attestations,
                coverage_gate_snapshot,
            )
        else:
            run, ext_findings, raw, stdout = _run_external(plan.root, spec, command)
        tools_run.append(run)
        if raw:
            raw_logs[tool_id] = raw
        findings.extend(ext_findings)
        if run["status"] in {"error-required-tool", "timeout-required"}:
            required_missing.append({"tool_id": tool_id, "reason": run["status"]})
        elif run["status"] in {"error-optional-tool", "timeout-optional"}:
            residual.append(_risk(spec["dimension"], f"{tool_id} {run['status']}", "medium", tool_id=tool_id))

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


def _ensure_plain_output_directory(path: Path) -> Path:
    """Create/validate a directory path one non-reparse component at a time."""
    absolute = Path(os.path.abspath(path))
    anchor = Path(absolute.anchor)
    current = anchor
    for component in absolute.parts[1:]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
            except OSError as exc:
                raise AssuranceUsageError(f"could not create assurance output directory {current}: {exc}") from exc
            current_stat = os.lstat(current)
        except OSError as exc:
            raise AssuranceUsageError(f"could not inspect assurance output directory {current}: {exc}") from exc
        if not _plain_directory_stat(current_stat):
            raise AssuranceUsageError(
                f"assurance output path has a symlink, reparse point, or non-directory component: {current}"
            )
    return absolute


def _make_new_plain_output_directory(path: Path) -> None:
    """Create one fresh output directory, refusing every existing object."""
    _ensure_plain_output_directory(path.parent)
    try:
        os.mkdir(path)
    except OSError as exc:
        raise AssuranceUsageError(f"assurance output run directory must be new: {path} ({exc})") from exc
    created = os.lstat(path)
    if not _plain_directory_stat(created):
        raise AssuranceUsageError(f"assurance output directory is not plain: {path}")


def _write_new_plain_text(path: Path, text: str) -> None:
    """Create one output leaf exclusively without following links."""
    _ensure_plain_output_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        raise AssuranceUsageError(f"assurance output file must be new: {path} ({exc})") from exc
    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
        if not stat.S_ISREG(opened.st_mode) or _reparse_stat(opened) or not os.path.samestat(opened, current):
            raise AssuranceUsageError(f"assurance output file is not a plain new file: {path}")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def write_artifact(result: ScanResult, out_dir: Path | str, summary: Path | str | None = None) -> ArtifactPaths:
    out_path = Path(out_dir)
    if not out_path.is_absolute():
        out_path = result.root / out_path
    out_path = _ensure_plain_output_directory(out_path)
    run_dir = out_path / result.run_id
    _make_new_plain_output_directory(run_dir)
    raw_dir = run_dir / "raw"
    _make_new_plain_output_directory(raw_dir)
    artifact = copy.deepcopy(result.artifact or _artifact_from_result(result, []))
    raw_paths: dict[str, str] = {}
    for tool_id, text in result.raw_logs.items():
        raw_path = raw_dir / f"{_safe_id(tool_id)}.txt"
        _write_new_plain_text(raw_path, text)
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
    _write_new_plain_text(
        artifact_path,
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
    )
    summary_path: Path | None = None
    if summary:
        summary_path = Path(summary)
        if not summary_path.is_absolute():
            summary_path = run_dir / summary_path
    else:
        summary_path = run_dir / "summary.md"
    summary_path = Path(os.path.abspath(summary_path))
    _write_new_plain_text(summary_path, _summary_text(artifact))
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
    fresh_coverage_attestations: set[tuple[str, str]] = set()
    run_id = _new_run_id()
    revision: str | None = None
    runner_errors: list[str] = []
    coverage_gate_snapshot: dict[str, Any] | None = None
    coverage_gate_snapshot_captured = False
    status = 1
    result: ScanResult | None = None
    paths: ArtifactPaths | None = None
    try:
        coverage_gate_snapshot = _snapshot_coverage_gate(root, args.profile)
        coverage_gate_snapshot_captured = True
        revision = _git_output(root, ["rev-parse", "HEAD"])
        try:
            manifest = load_manifest(root, args.manifest)
        except AssuranceUsageError as exc:
            manifest = _default_manifest(_resolve_selected_path(root, args.manifest))
            manifest["_validation_errors"] = [str(exc)]
        try:
            baseline = load_baseline(root, args.baseline)
        except AssuranceUsageError as exc:
            baseline = _default_baseline(_resolve_selected_path(root, args.baseline))
            baseline["_validation_errors"] = [str(exc)]
        detection = detect_project(root, manifest)
        provenance = collect_provenance(
            root,
            manifest,
            args.profile,
            baseline,
            changed_from=args.changed_from,
            changed_to=args.changed_to,
        )
        revision = str(provenance.get("git_sha") or "") or revision
        plan = build_plan(root, args.profile, manifest, detection, baseline, provenance)
        plan.run_id = run_id
        plan.runner_errors.extend(runner_errors)
        result = _run_plan(
            plan,
            fresh_coverage_attestations,
            coverage_gate_snapshot,
        )
        result = apply_baseline(result, baseline, manifest, provenance.get("changed_files") or [])
        paths = write_artifact(result, args.out, summary=args.summary)
    except AssuranceUsageError as exc:
        sys.stderr.write(f"agenttalk assurance: {exc}\n")
        status = 2
    except Exception as exc:  # noqa: BLE001 - fail-safe is no artifact, bounded stderr
        sys.stderr.write(f"agenttalk assurance: could not produce artifact: {exc}\n")
        status = 1
    else:
        status = 0
    finally:
        if coverage_gate_snapshot_captured:
            try:
                _invalidate_stale_coverage_gate(
                    root=root,
                    profile=args.profile,
                    revision=revision,
                    run_id=run_id,
                    fresh_coverage_attestations=fresh_coverage_attestations,
                    expected_gate=coverage_gate_snapshot,
                )
            except Exception as exc:  # noqa: BLE001 - finalization is a bounded CLI failure
                sys.stderr.write(f"agenttalk assurance: could not finalize coverage gate: {exc}\n")
                if status == 0:
                    status = 1

    if status != 0:
        return status
    if paths is None or result is None:
        sys.stderr.write("agenttalk assurance: could not produce artifact: missing result\n")
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
    # Coverage must retain bare CR versus CRLF until its fail-closed parser sees
    # the stream; text-mode universal-newline translation destroys that evidence.
    # Its process result is also specialized below: scanner-shaped JSON in stdout
    # is evidence text, not a generic assurance finding.
    is_coverage_command = spec.get("tool_id") == "coverage"
    capture_bytes = is_coverage_command
    raw = ""
    try:
        completed = subprocess.run(  # nosec B603 - argv list, shell disabled by default
            command,
            cwd=root,
            text=not capture_bytes,
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

    if capture_bytes:
        stdout_bytes = completed.stdout if isinstance(completed.stdout, bytes) else b""
        stderr = _process_output_text(completed.stderr)
        try:
            stdout = stdout_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raw_stdout = _process_output_text(stdout_bytes)
            status = "error-required-tool" if spec.get("required") else "error-optional-tool"
            detail = f"coverage stdout is not valid UTF-8: {exc}"
            raw = detail + ("\n" + raw_stdout if raw_stdout else "")
            if stderr:
                raw += "\n" + stderr
            run = _run_record(
                spec,
                status,
                start,
                command=command,
                exit_code=completed.returncode,
                raw_log_pending=True,
            )
            return run, [], raw, ""
        raw_stdout = stdout
    else:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        raw_stdout = stdout

    raw = raw_stdout + ("\n" if raw_stdout and stderr else "") + stderr
    findings = [] if is_coverage_command else _parse_tool_findings(spec, stdout, stderr)
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


def _run_coverage_external(
    plan: ScanPlan,
    spec: dict[str, Any],
    command: list[str],
    fresh_coverage_attestations: set[tuple[str, str]],
    coverage_gate_snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    """Run one fail-closed, root-serialized coverage measurement.

    Stdout is the sole coverage evidence channel. Before and after the
    subprocess, both names in ``_COVERAGE_ARTIFACTS`` and any branch-local
    legacy recovery residue are classified without following or mutating them.
    Any such path refuses or invalidates the measurement; agenttalk leaves it
    untouched for manual recovery.

    The configured subprocess is not filesystem-isolated. A late output from
    this producer can therefore make the next scan refuse (a deliberate
    false-DOWN until #107), but agenttalk never guesses that a root report is
    its own and never deletes it. Arbitrary output paths and case variants on
    case-sensitive filesystems are outside the exact tuple boundary. Coverage
    stdout is captured as bytes and decoded as strict UTF-8 so universal-newline
    translation cannot hide a bare-carriage-return rewrite. Stdout is bounded at
    the parser boundary, not while ``capture_output=True`` buffers the subprocess
    streams (#106). Command success comes only from subprocess exit/timeout,
    spawn, and decoding outcomes; generic scanner-shaped JSON in coverage stdout
    is never reclassified as an assurance finding.
    """
    from agenttalk.store import Store

    lock_timeout = max(10.0, float(spec.get("timeout_seconds") or 60) + 10.0)
    command_started = False
    runner_exception: tuple[BaseException, Any] | None = None
    stage = "acquiring"
    outcome: tuple[dict[str, Any], list[dict[str, Any]], str, str] | None = None
    gate_run: dict[str, Any] | None = None
    gate_stdout = ""
    gate_path_error: str | None = None
    gate_failure_reason: str | None = None
    gate_ci_evidence: str | None = None
    gate_config_generation: str | None = None
    transaction_token = uuid.uuid4().hex
    attestation_key = _coverage_attestation_key(
        plan.profile,
        plan.provenance.get("git_sha"),
    )
    fallback_expected_gate = copy.deepcopy(coverage_gate_snapshot)
    coverage_context: Any = None
    try:
        coverage_context = Store(plan.root).coverage_transaction_lock(
            timeout=lock_timeout,
        )
        coverage_context.__enter__()
    except BaseException as exc:
        if not isinstance(exc, (OSError, TimeoutError)):
            raise
        detail = f"coverage transaction failed ({type(exc).__name__}: {exc})"
        plan.runner_errors.append(detail)
        failure_status = "error-required-tool" if spec.get("required") else "error-optional-tool"
        run = _run_record(
            spec,
            failure_status,
            time.monotonic(),
            command=command,
            exit_code=None,
        )
        _emit_coverage_gate(
            plan,
            run,
            "",
            path_error=detail,
            failure_reason=("coverage transaction failed; coverage command was not run"),
            expected_gate=fallback_expected_gate,
        )
        fresh_coverage_attestations.add(attestation_key)
        return run, [], "", ""

    coverage_entered = True
    try:
        # Every holder crosses the coverage-only handoff and takes the config
        # lock briefly to obtain its expected gate generation before it can run
        # or mutate the tree. It publishes a unique provisional red unless an
        # active operator waiver must be preserved.
        with Store(plan.root).coverage_handoff_lock(timeout=lock_timeout):
            fallback_expected_gate = _emit_coverage_gate(
                plan,
                {"status": "running", "exit_code": None},
                "",
                path_error=None,
                failure_reason=(f"coverage measurement is in progress (transaction {transaction_token})"),
                transaction_complete=False,
            )
        stage = "preflight"
        path_error = _classify_coverage_path_conflicts(plan.root)
        if path_error is not None:
            plan.runner_errors.append(path_error)
            failure_status = "error-required-tool" if spec.get("required") else "error-optional-tool"
            run = _run_record(
                spec,
                failure_status,
                time.monotonic(),
                command=command,
                exit_code=None,
            )
            gate_run = run
            gate_path_error = path_error
            gate_failure_reason = (
                "coverage command refused because a canonical report or "
                "legacy recovery path requires manual recovery; "
                "coverage command was not run"
            )
            outcome = (run, [], "", "")
        else:
            try:
                stage = "running"
                command_started = True
                run, findings, raw, stdout = _run_external(
                    plan.root,
                    spec,
                    command,
                )
            except BaseException as exc:
                runner_exception = (exc, exc.__traceback__)
                stage = "finishing"
                path_error = _classify_coverage_path_conflicts(plan.root)
                if path_error is not None:
                    plan.runner_errors.append(path_error)
                failure_status = "error-required-tool" if spec.get("required") else "error-optional-tool"
                gate_run = {"status": failure_status, "exit_code": None}
                gate_path_error = path_error
                gate_failure_reason = f"coverage runner failed with {type(exc).__name__}"
            else:
                stage = "finishing"
                path_error = _classify_coverage_path_conflicts(plan.root)
                if path_error is not None:
                    plan.runner_errors.append(path_error)
                gate_run = run
                gate_stdout = stdout
                gate_path_error = path_error
                gate_failure_reason = (
                    None
                    if path_error is None
                    else (
                        "a canonical report or legacy recovery path appeared "
                        "during the coverage command; agenttalk left the "
                        "path untouched for manual recovery"
                    )
                )
                outcome = (run, findings, raw, stdout)

        if gate_run is None:
            raise RuntimeError("coverage transaction completed without a gate result")
        if (
            gate_path_error is None
            and gate_run.get("status") == "pass"
            and gate_run.get("exit_code") == 0
            and gate_failure_reason is None
        ):
            # Establish a current-client config-generation fence, then probe
            # without holding the global config lock. Finalization validates
            # the fence and re-probes outside the lock if another current
            # config transaction interposed.
            try:
                with Store(plan.root).config_lock() as config_generation:
                    gate_config_generation = config_generation[1]
            except (OSError, TimeoutError) as exc:
                gate_failure_reason = (
                    "coverage Git attestation could not establish a config "
                    f"transaction fence ({type(exc).__name__}: {exc})"
                )
                plan.runner_errors.append(gate_failure_reason)
            else:
                gate_ci_evidence = _github_actions_evidence(
                    plan.root,
                    plan.provenance,
                    canonical_paths_clear=True,
                )

        stage = "handoff"
        with Store(plan.root).coverage_handoff_lock(timeout=lock_timeout):
            # Release the root-global coverage lock while retaining only the
            # coverage handoff lock. The next holder can acquire coverage.lock,
            # but cannot obtain its expected gate generation or start its
            # command until this final CAS completes. Unrelated config/gate
            # operations remain free during a slow or failed lock release.
            coverage_entered = False
            stage = "releasing"
            try:
                coverage_context.__exit__(None, None, None)
            except BaseException as release_exc:
                detail = f"coverage transaction failed ({type(release_exc).__name__}: {release_exc})"
                plan.runner_errors.append(detail)
                failure_status = "error-required-tool" if spec.get("required") else "error-optional-tool"
                failure_run = _run_record(
                    spec,
                    failure_status,
                    time.monotonic(),
                    command=command,
                    exit_code=None,
                )
                command_disposition = (
                    "coverage result was discarded" if command_started else "coverage command was not run"
                )
                try:
                    _emit_coverage_gate(
                        plan,
                        failure_run,
                        "",
                        path_error=detail,
                        failure_reason=(f"coverage transaction failed; {command_disposition}"),
                        expected_gate=fallback_expected_gate,
                    )
                except BaseException as finalization_exc:
                    if runner_exception is not None:
                        original, traceback = runner_exception
                        plan.runner_errors.append(
                            "coverage transaction secondary failure during "
                            f"{stage} ({type(finalization_exc).__name__}: "
                            f"{finalization_exc})"
                        )
                        raise original.with_traceback(traceback) from finalization_exc
                    raise
                fresh_coverage_attestations.add(attestation_key)
                if runner_exception is not None:
                    original, traceback = runner_exception
                    plan.runner_errors.append(
                        "coverage transaction secondary failure during "
                        f"{stage} ({type(release_exc).__name__}: {release_exc})"
                    )
                    raise original.with_traceback(traceback) from release_exc
                return failure_run, [], "", ""

            stage = "committing"
            _emit_coverage_gate(
                plan,
                gate_run,
                gate_stdout,
                path_error=gate_path_error,
                failure_reason=gate_failure_reason,
                expected_gate=fallback_expected_gate,
                precomputed_ci_evidence=gate_ci_evidence,
                expected_config_generation=gate_config_generation,
            )
    finally:
        if coverage_entered:
            coverage_entered = False
            coverage_context.__exit__(*sys.exc_info())

    if runner_exception is not None:
        fresh_coverage_attestations.add(attestation_key)
        original, traceback = runner_exception
        raise original.with_traceback(traceback)
    if outcome is None:  # defensive: every non-exception path assigns an outcome
        raise RuntimeError("coverage transaction completed without a result")
    fresh_coverage_attestations.add(attestation_key)
    return outcome


def _classify_coverage_path_conflicts(root: Path) -> str | None:
    """Classify the complete known report-path boundary without mutation.

    ``os.lstat`` classifies the two canonical root names and any branch-local
    legacy recovery root, transaction, and immediate marker/backup/unexpected
    entries. Marker contents are never read, and symlink/reparse directories
    are never traversed. Missing paths are clear; every existing or
    uninspectable path is UNKNOWN and therefore refuses untouched. ``None``
    means clear; every refusal is represented by its non-optional diagnostic.
    """

    conflicts: list[str] = []
    inspection_errors: list[str] = []

    def reference(path: Path) -> str:
        try:
            return _slash(str(path.relative_to(root)))
        except ValueError:
            return _slash(str(path))

    def classify(path: Path) -> os.stat_result | None:
        path_reference = reference(path)
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            inspection_errors.append(f"{path_reference} ({type(exc).__name__}: {exc})")
            return None
        conflicts.append(path_reference)
        return path_stat

    def is_plain_directory(path_stat: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(path_stat, "st_file_attributes", 0)
        return stat.S_ISDIR(path_stat.st_mode) and not (reparse_flag and file_attributes & reparse_flag)

    for name in _COVERAGE_ARTIFACTS:
        classify(root / name)

    recovery_root = root / _LEGACY_COVERAGE_RECOVERY_DIR
    recovery_stat = classify(recovery_root)
    if recovery_stat is not None and is_plain_directory(recovery_stat):
        try:
            with os.scandir(recovery_root) as entries:
                transactions = sorted(
                    (Path(entry.path) for entry in entries),
                    key=lambda path: path.name,
                )
        except OSError as exc:
            inspection_errors.append(f"{reference(recovery_root)} ({type(exc).__name__}: {exc})")
            transactions = []
        for transaction in transactions:
            transaction_stat = classify(transaction)
            if transaction_stat is None or not is_plain_directory(transaction_stat):
                continue
            try:
                with os.scandir(transaction) as entries:
                    transaction_entries = sorted(
                        (Path(entry.path) for entry in entries),
                        key=lambda path: path.name,
                    )
            except OSError as exc:
                inspection_errors.append(f"{reference(transaction)} ({type(exc).__name__}: {exc})")
                continue
            for entry in transaction_entries:
                classify(entry)

    if not conflicts and not inspection_errors:
        return None

    details: list[str] = []
    if conflicts:
        details.append("agenttalk left paths untouched for manual recovery: " + ", ".join(conflicts))
    if inspection_errors:
        details.append("paths could not be inspected: " + ", ".join(inspection_errors))
    details.append("move or remove the named paths only after verifying their ownership")
    return "coverage report path safety check failed (" + "; ".join(details) + ")"


def _github_actions_evidence(
    root: Path,
    provenance: dict[str, Any],
    *,
    canonical_paths_clear: bool,
) -> str | None:
    """Return CI evidence only for a clean revision and clear report paths.

    The worktree probe discounts only exact, regular AgentTalk runtime outputs;
    arbitrary runtime neighbors and every other repository change remain dirty.
    """
    revision = str(provenance.get("git_sha") or "")
    github_sha = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if os.environ.get("CI", "").lower() != "true" or os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return None
    if not canonical_paths_clear or provenance.get("git_dirty") is not False:
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
    current_dirty = _git_worktree_dirty(
        root,
        protected_paths=(
            str(provenance.get("manifest_path") or DEFAULT_MANIFEST),
            str(provenance.get("baseline_path") or DEFAULT_BASELINE),
        ),
        protected_hashes=(
            (str(provenance["manifest_sha256"]) if provenance.get("manifest_sha256") else None),
            (str(provenance["baseline_sha256"]) if provenance.get("baseline_sha256") else None),
        ),
    )
    if current_revision is None or current_dirty is not False:
        return None
    if current_revision.lower() != revision.lower():
        return None
    return f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{run_attempt}"


def _coverage_attestation_key(profile: str, revision: str | None) -> tuple[str, str]:
    return profile, str(revision or "").lower()


def _load_coverage_gate_state(root: Path) -> dict[str, Any]:
    state = gates.load_gate_state(root)
    load_error = state.get("load_error")
    if load_error:
        raise OSError(f"could not read coverage gate state: {load_error}")
    return state


def _snapshot_coverage_gate(root: Path, profile: str) -> dict[str, Any] | None:
    """Capture the target gate as the finalizer's compare-and-swap token.

    The coverage transaction lock already spans report-path preflight through gate
    emission. It cannot order an older non-coverage scan that finishes after a
    newer coverage scan; holding it across every full scan would also serialize
    unrelated tools and recurse into the deliberately non-reentrant lock.
    """
    from agenttalk.store import Store

    with Store(root).config_lock():
        state = _load_coverage_gate_state(root)
        existing = state.get("gates", {}).get(coverage_gate_name(profile))
        return copy.deepcopy(existing) if isinstance(existing, dict) else None


def _coverage_waiver_active(gate: object) -> bool:
    if not isinstance(gate, dict) or gate.get("status") != "waived":
        return False
    waiver = gate.get("waiver")
    return isinstance(waiver, dict) and gates._waiver_active(
        waiver,
        now=datetime.now(timezone.utc),
    )


def _waiver_overlays_expected_gate(
    gate: object,
    expected_gate: object,
) -> bool:
    """Return whether ``gate`` is a waiver written over our gate generation.

    ``gates.waive_gate`` starts from the existing gate and changes only this
    overlay. Comparing every retained field—including the unique provisional
    transaction reason—lets an expired/invalid interposed waiver yield to the
    fresh measurement without treating an unrelated newer gate as ours.
    """
    if not isinstance(gate, dict) or gate.get("status") != "waived" or not isinstance(expected_gate, dict):
        return False
    overlay_keys = {
        "status",
        "scope",
        "updated_by",
        "updated_at",
        "evidence_source",
        "waiver",
    }
    retained_gate = {key: value for key, value in gate.items() if key not in overlay_keys}
    retained_expected = {key: value for key, value in expected_gate.items() if key not in overlay_keys}
    return retained_gate == retained_expected


def _invalidate_stale_coverage_gate(
    *,
    root: Path,
    profile: str,
    revision: str | None,
    run_id: str,
    fresh_coverage_attestations: set[tuple[str, str]],
    expected_gate: dict[str, Any] | None,
) -> None:
    if _coverage_attestation_key(profile, revision) in fresh_coverage_attestations:
        return

    from agenttalk.store import Store

    name = coverage_gate_name(profile)
    revision_value = str(revision or "") or None
    reason = "no fresh coverage measurement this run"
    with Store(root).config_lock():
        state = _load_coverage_gate_state(root)
        existing = state.get("gates", {}).get(name)
        # The config lock makes this comparison and the red write below one
        # atomic CAS. Any intervening gate write (green, red, or waiver) wins.
        if existing != expected_gate:
            return
        if not isinstance(existing, dict) or _coverage_waiver_active(existing):
            return
        if (
            existing.get("status") == "red"
            and existing.get("reason") == reason
            and existing.get("revision") == revision_value
        ):
            return
        gates.set_gate(
            root,
            name=name,
            status="red",
            severity="blocker",
            scope=profile,
            actor="assurance-finalizer",
            evidence_source="local_command",
            evidence=[f"assurance-run:{run_id}"],
            reason=reason,
            revision=revision_value,
        )


def _emit_coverage_gate(
    plan: ScanPlan,
    run: dict[str, Any],
    stdout: str,
    *,
    path_error: str | None,
    failure_reason: str | None = None,
    expected_gate: object = _NO_COVERAGE_GATE_CAS,
    precomputed_ci_evidence: object = _NO_PRECOMPUTED_CI_EVIDENCE,
    expected_config_generation: str | None = None,
    transaction_complete: bool = True,
) -> dict[str, Any] | None:
    from agenttalk.store import Store

    percent = parse_coverage_percent(stdout)
    command_succeeded = run.get("status") == "pass" and run.get("exit_code") == 0
    evidence_details = {"coverage_percent": float(percent)} if percent is not None else None
    canonical_paths_clear = path_error is None
    gate_name = coverage_gate_name(plan.profile)
    ci_evidence = None
    if transaction_complete and canonical_paths_clear and command_succeeded and failure_reason is None:
        if precomputed_ci_evidence is _NO_PRECOMPUTED_CI_EVIDENCE:
            # Direct callers have no enclosing coverage transaction. Git probes
            # still stay outside the global config lock.
            ci_evidence = _github_actions_evidence(
                plan.root,
                plan.provenance,
                canonical_paths_clear=True,
            )
        elif isinstance(precomputed_ci_evidence, str):
            ci_evidence = precomputed_ci_evidence

    def commit_gate() -> dict[str, Any] | None:
        state = _load_coverage_gate_state(plan.root)
        existing = state.get("gates", {}).get(gate_name)
        if expected_gate is not _NO_COVERAGE_GATE_CAS and existing != expected_gate:
            if _coverage_waiver_active(existing):
                return copy.deepcopy(existing)
            if not _waiver_overlays_expected_gate(existing, expected_gate):
                return copy.deepcopy(existing) if isinstance(existing, dict) else None
        # Automated measurements are last-write-wins only while the target is
        # not protected by an active, valid operator waiver.
        if _coverage_waiver_active(existing):
            return copy.deepcopy(existing)
        # This remains a point-in-time revision + worktree attestation. The
        # config lock never serialized arbitrary worktree writers, so moving
        # Git probes outside it does not create that race. A mutation after the
        # probe can still evade this write unless a later verifier reprobes;
        # closing that accepted #66/#31 residual needs a broader provenance
        # envelope, not a long-held config lock.
        is_green = (
            transaction_complete
            and canonical_paths_clear
            and command_succeeded
            and percent is not None
            and ci_evidence is not None
        )
        evidence_source = "automation_ci" if ci_evidence is not None else "local_command"
        if failure_reason is not None:
            reason = failure_reason
        elif not command_succeeded:
            reason = "coverage command did not complete successfully"
        elif percent is None:
            reason = "coverage command succeeded but no overall percentage could be parsed"
        elif not transaction_complete:
            reason = "coverage measurement is provisional until transaction lock release"
        elif ci_evidence is None:
            reason = "coverage percentage is not bound to an attested clean CI revision"
        else:
            reason = "coverage command succeeded with a parsed, revision-bound CI measurement"
        if path_error is not None:
            reason = f"{reason}; {path_error}"
        evidence = [ci_evidence] if ci_evidence is not None else [f"assurance-run:{plan.run_id}"]
        gate = gates.set_gate(
            plan.root,
            name=gate_name,
            status="green" if is_green else "red",
            severity="blocker",
            scope=plan.profile,
            actor="assurance-ci" if ci_evidence is not None else "assurance-local",
            evidence_source=evidence_source,
            evidence=evidence if transaction_complete else None,
            evidence_details=evidence_details if transaction_complete else None,
            reason=reason,
            revision=str(plan.provenance.get("git_sha") or "") or None,
        )
        return copy.deepcopy(gate)

    config_generation = expected_config_generation
    reprobes_remaining = 2
    while True:
        with Store(plan.root).config_lock() as acquired_generation:
            previous_generation, current_generation = acquired_generation
            if config_generation is None or previous_generation == config_generation:
                return commit_gate()
            if reprobes_remaining == 0:
                # Repeated current-client config churn means the cached Git
                # observation cannot be bound to this write. Fail closed.
                ci_evidence = None
                return commit_gate()
        # Never run Git under the config lock. The coverage-only handoff held by
        # the caller still prevents another coverage command from crossing this
        # finalization boundary. This acquisition's token fences the re-probe.
        ci_evidence = _github_actions_evidence(
            plan.root,
            plan.provenance,
            canonical_paths_clear=canonical_paths_clear,
        )
        config_generation = current_generation
        reprobes_remaining -= 1


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


def _resolve_selected_path(root: Path, path: Path | str) -> Path:
    """Return a lexical absolute policy path without following links."""
    selected = Path(path)
    if not selected.is_absolute():
        selected = root / selected
    return Path(os.path.abspath(selected))


def _lexical_rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _selected_policy_file_present(
    root: Path,
    path: Path,
    *,
    label: str,
) -> bool:
    """Validate a selected policy object, distinguishing absent from hidden.

    Missing untracked defaults are allowed. A tracked-but-missing path (for
    example hidden by ``skip-worktree``), an outside-root path, or any
    symlink/reparse/non-regular component refuses before a weaker default can
    replace the selected policy.
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AssuranceUsageError(f"{label} path must stay within the assurance root: {path}") from exc
    if not relative.parts:
        raise AssuranceUsageError(f"{label} path is not a file: {path}")

    current = root
    missing = False
    for component in relative.parts[:-1]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            missing = True
            break
        except OSError as exc:
            raise AssuranceUsageError(f"could not inspect {label} path {path}: {exc}") from exc
        if not _plain_directory_stat(current_stat):
            raise AssuranceUsageError(f"{label} path has a symlink, reparse point, or non-directory parent: {current}")

    if not missing:
        try:
            selected_stat = os.lstat(path)
        except FileNotFoundError:
            missing = True
        except OSError as exc:
            raise AssuranceUsageError(f"could not inspect {label} path {path}: {exc}") from exc
        else:
            if not stat.S_ISREG(selected_stat.st_mode) or _reparse_stat(selected_stat):
                raise AssuranceUsageError(f"{label} path must be a regular, non-reparse file: {path}")
            return True

    tracked = _git_index_tracks_path(root, path)
    if tracked is None:
        raise AssuranceUsageError(f"could not determine whether missing {label} is tracked: {path}")
    if tracked:
        raise AssuranceUsageError(f"tracked {label} is missing from the worktree: {path}")
    return False


def _git_index_tracks_path(root: Path, path: Path) -> bool | None:
    """Return whether Git stage 0 contains this exact lexical pathname."""
    git = shutil.which("git")
    if git is None:
        return False
    try:
        repository = subprocess.run(  # nosec B603 - resolved executable, argv list
            [git, "rev-parse", "--path-format=absolute", "--show-toplevel"],
            cwd=root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if repository.returncode != 0:
        return False
    raw_root = repository.stdout.strip()
    if not raw_root or b"\0" in raw_root or b"\n" in raw_root or b"\r" in raw_root:
        return None
    repository_root = Path(os.fsdecode(raw_root))
    try:
        relative = path.relative_to(repository_root)
    except ValueError:
        return False
    relative_text = _slash(str(relative))
    try:
        listed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [
                git,
                "--literal-pathspecs",
                "ls-files",
                "--cached",
                "--stage",
                "-z",
                "--",
                relative_text,
            ],
            cwd=repository_root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None
    selected_key = os.path.normcase(os.path.abspath(path))
    for entry in listed.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_path = entry.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            return None
        if fields[2] != b"0":
            continue
        tracked_path = repository_root / os.fsdecode(raw_path)
        if os.path.normcase(os.path.abspath(tracked_path)) == selected_key:
            return True
    return False


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


def _git_worktree_dirty(
    root: Path,
    *,
    protected_paths: tuple[str, ...] = (),
    protected_hashes: tuple[str | None, ...] = (),
) -> bool | None:
    """Return Git dirtiness without self-counting exact AgentTalk outputs.

    ``agenttalk init`` does not edit ``.gitignore``. Exact init/assurance state
    may therefore be untracked without invalidating its producer, but arbitrary
    neighbors below ``.agenttalk/`` remain dirty. Existing selected manifests
    and baselines must be tracked independently of ignore rules. Tracked
    AgentTalk state is repository state: every worktree modification remains
    dirty, including exact scanner pathnames.
    Selected inputs are independently proved Git-clean-equivalent to their
    index blobs, so status-hiding index flags cannot make a changed policy look
    clean while normal Git clean filters (for example CRLF normalization)
    remain supported.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        repository_result = subprocess.run(  # nosec B603 - resolved executable, argv list
            [
                git,
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--show-prefix",
            ],
            cwd=root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [
                git,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if repository_result.returncode != 0 or completed.returncode != 0:
        return None
    repository_metadata = repository_result.stdout.replace(b"\r\n", b"\n")
    if not repository_metadata.endswith(b"\n"):
        return None
    metadata_lines = repository_metadata[:-1].split(b"\n")
    if len(metadata_lines) != 2 or not metadata_lines[0]:
        return None
    repository_root = Path(os.fsdecode(metadata_lines[0])).resolve()
    repository_prefix = metadata_lines[1]
    if (
        b"\r" in repository_prefix
        or b"\n" in repository_prefix
        or (repository_prefix and not repository_prefix.endswith(b"/"))
    ):
        return None
    runtime_root_state = _plain_agenttalk_runtime_root(root)
    if runtime_root_state is None:
        return None
    if runtime_root_state is False:
        return True
    protected_tracked = _selected_inputs_tracked(
        git,
        root,
        repository_root,
        protected_paths,
        protected_hashes,
    )
    if protected_tracked is None:
        return None
    if protected_tracked is False:
        return True
    tracked_visibility = _tracked_index_visibility_clean(
        git,
        root,
        repository_root,
        repository_prefix,
        protected_paths,
    )
    if tracked_visibility is None:
        return None
    if tracked_visibility is False:
        return True
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            # A malformed status stream is unknown, never clean.
            return None
        status = entry[:2]
        path = entry[3:]
        if repository_prefix:
            if not path.startswith(repository_prefix):
                return True
            selected_path = path[len(repository_prefix) :]
        else:
            selected_path = path
        if _status_path_matches_protected_input(
            root,
            selected_path,
            protected_paths,
        ):
            return True
        runtime_relative = _agenttalk_runtime_relative(root, selected_path)
        if runtime_relative is not None:
            if status == b"??" and _is_agenttalk_created_untracked_path(
                root,
                runtime_relative,
            ):
                continue
        return True
    return False


def _tracked_index_visibility_clean(
    git: str,
    root: Path,
    repository_root: Path,
    repository_prefix: bytes,
    protected_paths: tuple[str, ...],
) -> bool | None:
    """Refuse tracked paths hidden from the ordinary worktree status probe.

    ``git status`` honors ``skip-worktree`` and ``assume-unchanged``. A clean
    attestation cannot infer that a hidden file is unchanged. Selected policy
    inputs are the sole exception because ``_selected_inputs_tracked`` has
    already compared their exact loaded bytes, filesystem identity, clean
    filtered content, and stage-zero index blob.
    """
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [
                git,
                "ls-files",
                "-v",
                "-z",
            ],
            cwd=repository_root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1:2] != b" ":
            return None
        tag = entry[:1]
        if tag == b"H":
            continue
        repository_path = entry[2:]
        if tag in {b"S", b"h"} and (not repository_prefix or repository_path.startswith(repository_prefix)):
            selected_path = repository_path[len(repository_prefix) :] if repository_prefix else repository_path
            if _index_path_matches_protected_input(
                root,
                selected_path,
                protected_paths,
            ):
                continue
        return False
    return True


def _index_path_matches_protected_input(
    root: Path,
    git_path: bytes,
    protected_paths: tuple[str, ...],
) -> bool:
    """Match the selected lexical path, never a hardlink identity alias."""
    candidate = root / os.fsdecode(git_path)
    candidate_key = os.path.normcase(os.path.abspath(candidate))
    for raw in protected_paths:
        protected = Path(raw)
        if not protected.is_absolute():
            protected = root / protected
        if candidate_key == os.path.normcase(os.path.abspath(protected)):
            return True
    return False


def _selected_inputs_tracked(
    git: str,
    root: Path,
    repository_root: Path,
    protected_paths: tuple[str, ...],
    protected_hashes: tuple[str | None, ...],
) -> bool | None:
    """Prove each selected input is tracked and Git-clean-equal to its index."""
    if protected_hashes and len(protected_hashes) != len(protected_paths):
        return None
    expected_hashes = protected_hashes if protected_hashes else (None,) * len(protected_paths)
    selected: list[tuple[Path, os.stat_result | None, str, str | None]] = []
    for raw, expected_hash in zip(
        protected_paths,
        expected_hashes,
        strict=True,
    ):
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        try:
            candidate.relative_to(root)
            repository_relative = candidate.relative_to(repository_root)
        except ValueError:
            return False
        try:
            present = _selected_policy_file_present(
                root,
                candidate,
                label="selected assurance input",
            )
        except AssuranceUsageError:
            return False
        if present:
            try:
                candidate_stat: os.stat_result | None = os.lstat(candidate)
            except OSError:
                return None
            if expected_hash is None or _sha256_file(candidate) != expected_hash:
                return False
        else:
            candidate_stat = None
            if expected_hash is not None:
                return False
        relative_text = _slash(str(repository_relative))
        selected.append((candidate, candidate_stat, relative_text, expected_hash))
    if not selected:
        return True
    try:
        completed = subprocess.run(  # nosec B603 - resolved executable, argv list
            [
                git,
                "--literal-pathspecs",
                "ls-files",
                "--cached",
                "--stage",
                "-z",
                "--",
                *(item[2] for item in selected),
            ],
            cwd=repository_root,
            text=False,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    tracked: list[tuple[os.stat_result | None, str, str, str]] = []
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_path = entry.partition(b"\t")
        fields = metadata.split(b" ")
        if (
            not separator
            or len(fields) != 3
            or fields[2] != b"0"
            or re.fullmatch(rb"[0-9a-f]{40,64}", fields[1]) is None
        ):
            return None
        tracked_relative = os.fsdecode(raw_path)
        tracked_path = repository_root / tracked_relative
        try:
            tracked_stat: os.stat_result | None = os.lstat(tracked_path)
        except FileNotFoundError:
            tracked_stat = None
        except OSError:
            return None
        tracked.append(
            (
                tracked_stat,
                fields[1].decode("ascii"),
                tracked_relative,
                os.path.normcase(os.path.abspath(tracked_path)),
            )
        )
    for candidate, candidate_stat, _, _expected_hash in selected:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        pathname_entries = [
            (tracked_stat, oid, tracked_relative)
            for tracked_stat, oid, tracked_relative, tracked_key in tracked
            if candidate_key == tracked_key
        ]
        if candidate_stat is None:
            if pathname_entries:
                return False
            continue
        matching_entries = {
            (oid, tracked_relative)
            for tracked_stat, oid, tracked_relative in pathname_entries
            if tracked_stat is not None and os.path.samestat(candidate_stat, tracked_stat)
        }
        if not matching_entries:
            return False
        matches_index = False
        for oid, tracked_relative in matching_entries:
            try:
                hashed = subprocess.run(  # nosec B603 - resolved executable, argv list
                    [
                        git,
                        "hash-object",
                        f"--path={tracked_relative}",
                        "--",
                        str(candidate),
                    ],
                    cwd=repository_root,
                    text=False,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if hashed.returncode != 0:
                return None
            hashed_oid = hashed.stdout.strip()
            if re.fullmatch(rb"[0-9a-f]{40,64}", hashed_oid) is None:
                return None
            if hashed_oid.decode("ascii") == oid:
                matches_index = True
                break
        if not matches_index:
            return False
    return True


def _status_path_matches_protected_input(
    root: Path,
    git_path: bytes,
    protected_paths: tuple[str, ...],
) -> bool:
    """Give selected manifest/baseline dirtiness precedence over exemptions."""
    candidate = root / os.fsdecode(git_path)
    candidate_key = os.path.normcase(os.path.abspath(candidate))
    try:
        candidate_stat = os.lstat(candidate)
    except OSError:
        candidate_stat = None
    for raw in protected_paths:
        protected = Path(raw)
        if not protected.is_absolute():
            protected = root / protected
        if candidate_key == os.path.normcase(os.path.abspath(protected)):
            return True
        if candidate_stat is None:
            continue
        try:
            if os.path.samestat(candidate_stat, os.lstat(protected)):
                return True
        except OSError:
            continue
    return False


def _plain_agenttalk_runtime_root(root: Path) -> bool | None:
    """Return whether an existing canonical runtime root is a real directory."""
    try:
        runtime_stat = os.lstat(root / ".agenttalk")
    except FileNotFoundError:
        return True
    except OSError:
        return None
    return _plain_directory_stat(runtime_stat)


def _reparse_stat(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _plain_directory_stat(path_stat: os.stat_result) -> bool:
    return stat.S_ISDIR(path_stat.st_mode) and not _reparse_stat(path_stat)


def _regular_runtime_file_stat(
    root: Path,
    relative: str,
) -> os.stat_result | None:
    """Validate every runtime component without following reparse parents."""
    components = relative.split("/")
    if not components or any(component in {"", ".", ".."} for component in components):
        return None
    current = root / ".agenttalk"
    for component in components[:-1]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except OSError:
            return None
        if not _plain_directory_stat(current_stat):
            return None
    try:
        leaf_stat = os.lstat(current / components[-1])
    except OSError:
        return None
    if not stat.S_ISREG(leaf_stat.st_mode) or _reparse_stat(leaf_stat):
        return None
    return leaf_stat


def _agenttalk_runtime_relative(root: Path, git_path: bytes) -> str | None:
    """Return ``git_path`` relative to the actual AgentTalk runtime tree.

    Git preserves on-disk spelling. Case-insensitive filesystems can therefore
    report ``.AgentTalk`` for Store's ``.agenttalk`` path, while case-sensitive
    filesystems (including suitably configured Windows directories) can keep
    both names distinct. Compare filesystem identities instead of guessing from
    the host OS; symlink aliases are not treated as scanner-owned.
    """
    decoded = os.fsdecode(git_path)
    component, separator, relative = decoded.partition("/")
    if _plain_agenttalk_runtime_root(root) is not True:
        return None
    if component == ".agenttalk":
        return relative if separator else ""
    if component.casefold() != ".agenttalk":
        return None
    try:
        canonical = os.lstat(root / ".agenttalk")
        reported = os.lstat(root / component)
    except OSError:
        return None
    if not os.path.samestat(canonical, reported):
        return None
    return relative if separator else ""


def _same_runtime_path(root: Path, left: str, right: str) -> bool:
    """Compare two runtime spellings by actual filesystem identity."""
    left_stat = _regular_runtime_file_stat(root, left)
    if left_stat is None:
        return False
    if left == right:
        return True
    right_stat = _regular_runtime_file_stat(root, right)
    if right_stat is None:
        return False
    return os.path.samestat(left_stat, right_stat)


def _is_agenttalk_created_untracked_path(root: Path, relative: str) -> bool:
    """Match only paths AgentTalk init or assurance can create."""
    from agenttalk.store import validate_agent_name

    persistent = {
        "config.json",
        "config.lock",
        ".config.lock.generation",
        "assurance/coverage.lock",
        "assurance/.coverage.lock.generation",
        "assurance/coverage-handoff.lock",
        "assurance/.coverage-handoff.lock.generation",
        "gates.json",
    }
    if any(_same_runtime_path(root, relative, candidate) for candidate in persistent):
        return True

    cursor = re.fullmatch(
        r"state/([^/]+)\.cursor",
        relative,
        flags=re.IGNORECASE,
    )
    if cursor is not None:
        try:
            agent_name = validate_agent_name(cursor.group(1))
        except ValueError:
            return False
        candidate = "state/" + agent_name + ".cursor"
        if _same_runtime_path(root, relative, candidate):
            return True

    lowercase_patterns = (
        r"config\.json\.[a-z0-9_]{8}",
        r"gates\.json\.[a-z0-9_]{8}",
        r"(?:\.config\.lock|assurance/\.(?:coverage|coverage-handoff)\.lock)\."
        r"[0-9a-f]{32}\.(?:prepare|unlink)",
        r"(?:\.\.config\.lock|"
        r"assurance/\.\.(?:coverage|coverage-handoff)\.lock)\."
        r"[0-9a-f]{32}\.prepare\.[0-9a-f]{32}\.unlink",
    )
    for pattern in lowercase_patterns:
        if re.fullmatch(pattern, relative, flags=re.IGNORECASE):
            if _same_runtime_path(root, relative, relative.lower()):
                return True

    cursor_temp = re.fullmatch(
        r"state/([^/]+)\.cursor\.([a-z0-9_]{8})",
        relative,
        flags=re.IGNORECASE,
    )
    if cursor_temp is not None:
        try:
            agent_name = validate_agent_name(cursor_temp.group(1))
        except ValueError:
            return False
        candidate = "state/" + agent_name + ".cursor." + cursor_temp.group(2).lower()
        if _same_runtime_path(root, relative, candidate):
            return True

    run_output = re.fullmatch(
        r"assurance/runs/"
        r"([0-9]{8}T[0-9]{6}\.[0-9]{6}Z)/"
        r"(artifact\.json|summary\.md|raw/([A-Za-z0-9_.-]+)\.txt)",
        relative,
        flags=re.IGNORECASE,
    )
    if run_output is None:
        return False
    run_id = run_output.group(1).upper()
    tool_id = run_output.group(3)
    if tool_id is None:
        leaf = run_output.group(2).lower()
    else:
        safe_tool_id = _safe_id(tool_id)
        if safe_tool_id != tool_id:
            return False
        leaf = f"raw/{safe_tool_id}.txt"
    candidate = f"assurance/runs/{run_id}/{leaf}"
    return _same_runtime_path(root, relative, candidate)


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
