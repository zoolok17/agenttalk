"""Build a read-only, SHA-bound release-candidate provenance bundle."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
# Preflight invokes only fixed git argv without a shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from agenttalk import dev_gate


CANONICAL_LEG = "linux/3.12"
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_AGE = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)
PROVENANCE_SCHEMA_VERSION = 1
RETENTION_DAYS = 90

MISSING = "release_evidence_missing"
STALE = "release_evidence_stale"
SHA_MISMATCH = "release_evidence_sha_mismatch"
CORRUPT = "release_evidence_corrupt"
DIGEST_MISMATCH = "release_evidence_digest_mismatch"
GATE_FAILED = "release_gate_failed"
VERSION_MISMATCH = "release_version_mismatch"
CANDIDATE_INVALID = "release_candidate_invalid"

_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STABLE_VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
_ARTIFACT_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str


class ReleaseEvidenceError(ValueError):
    """A fail-closed refusal with stable, machine-readable reason codes."""

    def __init__(self, issues: Refusal | Iterable[Refusal]):
        collected = (issues,) if isinstance(issues, Refusal) else tuple(issues)
        if not collected:
            raise ValueError("at least one refusal is required")
        unique = tuple(dict.fromkeys(collected))
        self.issues = unique
        self.codes = tuple(dict.fromkeys(issue.code for issue in unique))
        self.code = self.codes[0] if len(self.codes) == 1 else "release_evidence_multiple"
        super().__init__("; ".join(f"[{issue.code}] {issue.detail}" for issue in unique))


@dataclass(frozen=True)
class GateContext:
    binding: dev_gate.CandidateBinding
    manifest: dict[str, Any]
    expected_legs: tuple[str, ...]
    canonical_leg: str

    def validate_run(self, record: dict[str, Any]) -> dict[str, Any]:
        return dev_gate.validate_run_artifact(record, self.manifest)

    def validate_aggregate(self, record: dict[str, Any]) -> dict[str, Any]:
        return dev_gate.validate_aggregate_artifact(
            record,
            self.manifest,
            current_binding=self.binding,
        )


def _refuse(code: str, detail: str) -> ReleaseEvidenceError:
    return ReleaseEvidenceError(Refusal(code, detail))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise _refuse(CORRUPT, f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _refuse(CORRUPT, f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise _refuse(CORRUPT, f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _stable_version_tuple(value: str) -> tuple[int, int, int]:
    if _STABLE_VERSION.fullmatch(value) is None:
        raise _refuse(CANDIDATE_INVALID, "release version must use stable X.Y.Z syntax")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _validate_identity(candidate_sha: str, version: str) -> None:
    if _SHA40.fullmatch(candidate_sha) is None:
        raise _refuse(CANDIDATE_INVALID, "candidate SHA must be 40 lowercase hexadecimal characters")
    _stable_version_tuple(version)


def _is_reparse_point(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(flag and attributes & flag)


def _is_plain_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not _is_reparse_point(info)


def _is_plain_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not _is_reparse_point(info)


def _git(repo_root: Path, *args: str) -> str:
    try:
        # A bare nosec is deliberate: Bandit 1.9.4 does not reliably honor both
        # B603 and B607 in one targeted list. The executable is fixed, argv is
        # assembled only by this module, and shell execution is never enabled.
        completed = subprocess.run(  # nosec
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise _refuse(CANDIDATE_INVALID, f"git {' '.join(args)} failed: {exc}") from exc
    return completed.stdout.strip()


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _refuse(CANDIDATE_INVALID, f"cannot read {label}: {exc}") from exc


def _declared_module_version(path: Path) -> str:
    try:
        module = ast.parse(_read_text(path, str(path)))
    except SyntaxError as exc:
        raise _refuse(CANDIDATE_INVALID, f"cannot parse {path}: {exc}") from exc
    values = [
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(values) != 1:
        raise _refuse(CANDIDATE_INVALID, f"{path} must declare __version__ exactly once")
    return values[0]


def _validate_version_surfaces(repo_root: Path, version: str) -> None:
    project = _read_text(repo_root / "pyproject.toml", "pyproject.toml")
    project_match = re.search(r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", project)
    if project_match is None:
        raise _refuse(CANDIDATE_INVALID, "pyproject.toml has no [project] table")
    project_body = project_match.group("body")
    versions = re.findall(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project_body)
    if versions != [version]:
        raise _refuse(VERSION_MISMATCH, "pyproject.toml version does not match the dispatch")
    if re.search(r"(?ms)^dependencies\s*=\s*\[\s*\]\s*$", project_body) is None:
        raise _refuse(CANDIDATE_INVALID, "runtime dependencies must remain empty")

    module_version = _declared_module_version(repo_root / "src" / "agenttalk" / "__init__.py")
    if module_version != version:
        raise _refuse(VERSION_MISMATCH, "agenttalk.__version__ does not match the dispatch")

    changelog = _read_text(repo_root / "CHANGELOG.md", "CHANGELOG.md")
    if re.search(rf"(?m)^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", changelog) is None:
        raise _refuse(VERSION_MISMATCH, "CHANGELOG.md lacks the dated release heading")

    pin_pattern = re.compile(r"agenttalk\.git@v([0-9]+\.[0-9]+\.[0-9]+)")
    for relative in (
        Path("README.md"),
        Path("docs/USER-MANUAL.md"),
        Path("docs/AGENTTALK-NEW-USER-MANUAL.md"),
    ):
        pins = pin_pattern.findall(_read_text(repo_root / relative, str(relative)))
        if not pins or any(pin != version for pin in pins):
            raise _refuse(VERSION_MISMATCH, f"{relative} install pins do not all match v{version}")

    onboarding = _read_text(
        repo_root / "docs" / "AGENTTALK-NEW-USER-MANUAL.md",
        "docs/AGENTTALK-NEW-USER-MANUAL.md",
    )
    onboarding_baseline = re.search(
        r"Current release baseline: v([0-9]+\.[0-9]+\.[0-9]+)", onboarding
    )
    if onboarding_baseline is None or onboarding_baseline.group(1) != version:
        raise _refuse(
            VERSION_MISMATCH,
            "docs/AGENTTALK-NEW-USER-MANUAL.md baseline does not match the dispatch",
        )

    onboarding_pdf = repo_root / "docs" / "AGENTTALK-NEW-USER-MANUAL.pdf"
    pdf_raw = _read_regular_file(onboarding_pdf, "generated onboarding PDF", MAX_EVIDENCE_BYTES)
    pdf_tail = pdf_raw.rstrip()
    if (
        not pdf_raw.startswith(b"%PDF-")
        or not pdf_tail.endswith(b"%%EOF")
        or b"startxref" not in pdf_tail[-4096:]
    ):
        raise _refuse(VERSION_MISMATCH, "the generated onboarding PDF has no valid PDF envelope")

    roadmap = _read_text(repo_root / "docs" / "ROADMAP.md", "docs/ROADMAP.md")
    baseline = re.search(r"(?m)^\*\*Current shipped baseline:\*\* v([0-9]+\.[0-9]+\.[0-9]+)", roadmap)
    if baseline is None or baseline.group(1) != version:
        raise _refuse(VERSION_MISMATCH, "docs/ROADMAP.md shipped baseline does not match the dispatch")

    assurance = _read_text(repo_root / "docs" / "ASSURANCE.md", "docs/ASSURANCE.md")
    if re.search(rf"(?m)^### v{re.escape(version)}(?:\s|$)", assurance) is None:
        raise _refuse(VERSION_MISMATCH, "docs/ASSURANCE.md lacks this release's ledger entry")


def validate_preflight(
    *,
    repo_root: Path,
    candidate_sha: str,
    event_sha: str,
    event_ref: str,
    workflow_sha: str,
    version: str,
    run_attempt: int = 1,
) -> None:
    """Validate the exact post-bump master candidate before starting the gate."""

    _validate_identity(candidate_sha, version)
    if run_attempt != 1:
        raise _refuse(STALE, "release provenance requires a fresh dispatch, not a partial workflow rerun")
    if event_ref != "refs/heads/master":
        raise _refuse(CANDIDATE_INVALID, "release provenance dispatches must select refs/heads/master")
    if event_sha != candidate_sha:
        raise _refuse(SHA_MISMATCH, "dispatch event SHA differs from the explicit candidate SHA")
    if workflow_sha != candidate_sha:
        raise _refuse(SHA_MISMATCH, "executing workflow SHA differs from the explicit candidate SHA")
    observed_sha = _git(repo_root, "rev-parse", "HEAD")
    if observed_sha != candidate_sha:
        raise _refuse(SHA_MISMATCH, "checked-out HEAD differs from the explicit candidate SHA")
    if _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise _refuse(CANDIDATE_INVALID, "release candidate checkout is not clean")
    _validate_version_surfaces(repo_root, version)

    requested = _stable_version_tuple(version)
    stable_tags = []
    for tag in _git(repo_root, "tag", "--list", "v*").splitlines():
        match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", tag)
        if match is not None:
            stable_tags.append(tuple(int(part) for part in match.groups()))
    if stable_tags:
        latest = max(stable_tags)
        if requested <= latest:
            raise _refuse(VERSION_MISMATCH, "release version must be newer than every existing stable tag")
        latest_tag = "v" + ".".join(str(part) for part in latest)
        onboarding_pdf = "docs/AGENTTALK-NEW-USER-MANUAL.pdf"
        changed_paths = {
            line.strip()
            for line in _git(
                repo_root,
                "diff",
                "--name-only",
                f"{latest_tag}..HEAD",
                "--",
                onboarding_pdf,
            ).splitlines()
            if line.strip()
        }
        if onboarding_pdf not in changed_paths:
            raise _refuse(
                VERSION_MISMATCH,
                f"{onboarding_pdf} was not regenerated after {latest_tag}",
            )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _refuse(CORRUPT, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise _refuse(CORRUPT, f"non-finite JSON number is forbidden: {value}")


def _read_regular_file(path: Path, label: str, limit: int) -> bytes:
    try:
        before_link = path.lstat()
    except FileNotFoundError as exc:
        raise _refuse(MISSING, f"{label} is missing") from exc
    except OSError as exc:
        raise _refuse(CORRUPT, f"cannot inspect {label}: {exc}") from exc
    if not _is_plain_file(before_link):
        raise _refuse(CORRUPT, f"{label} must be a regular non-reparse file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _refuse(CORRUPT, f"cannot open {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise _refuse(CORRUPT, f"cannot read {label}: {exc}") from exc
    finally:
        os.close(descriptor)
    if not _is_plain_file(before) or not _is_plain_file(after) or not _is_plain_file(current):
        raise _refuse(CORRUPT, f"{label} changed to a non-regular or reparse file")
    if (
        not os.path.samestat(before_link, before)
        or not os.path.samestat(before, after)
        or not os.path.samestat(after, current)
    ):
        raise _refuse(CORRUPT, f"{label} changed identity while it was read")
    if len(raw) > limit:
        raise _refuse(CORRUPT, f"{label} exceeds the {limit}-byte limit")
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise _refuse(CORRUPT, f"{label} changed while it was read")
    if before.st_size != len(raw):
        raise _refuse(CORRUPT, f"{label} size changed while it was read")
    return raw


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_file(path, label, MAX_EVIDENCE_BYTES)
    try:
        loaded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ReleaseEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _refuse(CORRUPT, f"cannot parse {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise _refuse(CORRUPT, f"{label} must contain one JSON object")
    return loaded, raw


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path, label: str, limit: int = MAX_PACKAGE_BYTES) -> tuple[str, int]:
    raw = _read_regular_file(path, label, limit)
    return _sha256_bytes(raw), len(raw)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except OSError as exc:
        raise _refuse(CORRUPT, f"cannot write {path}: {exc}") from exc


def _gate_context(repo_root: Path) -> GateContext:
    try:
        binding = dev_gate.capture_candidate_binding(repo_root)
        manifest = dev_gate.load_bound_manifest(binding)
        expected = tuple(dev_gate.expected_ci_legs(manifest, "release"))
        canonical = manifest["profiles"]["release"]["ci"]["canonical_static_leg"]
    except (dev_gate.GateBlock, KeyError, TypeError) as exc:
        raise _refuse(CORRUPT, f"cannot load the committed gate authority: {exc}") from exc
    return GateContext(binding, manifest, expected, canonical)


def _check_subject(record: dict[str, Any], candidate_sha: str, version: str, label: str) -> list[Refusal]:
    subject = record.get("subject")
    if not isinstance(subject, dict):
        return [Refusal(CORRUPT, f"{label}.subject is missing or malformed")]
    issues: list[Refusal] = []
    if subject.get("candidate_sha") != candidate_sha:
        issues.append(Refusal(SHA_MISMATCH, f"{label} is bound to a different candidate SHA"))
    if subject.get("version") != version:
        issues.append(Refusal(VERSION_MISMATCH, f"{label} is bound to a different version"))
    return issues


def _check_freshness(
    value: Any,
    *,
    label: str,
    preflight_at: datetime,
    now: datetime,
) -> list[Refusal]:
    try:
        observed = _parse_time(value, label)
    except ReleaseEvidenceError as exc:
        return list(exc.issues)
    if observed < preflight_at - MAX_CLOCK_SKEW:
        return [Refusal(STALE, f"{label} predates this dispatch preflight")]
    if observed > now + MAX_CLOCK_SKEW:
        return [Refusal(STALE, f"{label} is implausibly in the future")]
    if now - observed > MAX_EVIDENCE_AGE:
        return [Refusal(STALE, f"{label} exceeds the 24-hour freshness window")]
    return []


def _empty_output_directory(path: Path, *, runner_temp: Path) -> Path:
    try:
        runner_info = runner_temp.lstat()
    except OSError as exc:
        raise _refuse(CORRUPT, f"cannot inspect RUNNER_TEMP: {exc}") from exc
    if not _is_plain_directory(runner_info):
        raise _refuse(CORRUPT, "RUNNER_TEMP must be a plain non-reparse directory")
    runner = runner_temp.resolve(strict=True)
    if not path.is_absolute():
        raise _refuse(CORRUPT, "output directory must be absolute")
    resolved_parent = path.parent.resolve(strict=True)
    if not resolved_parent.is_relative_to(runner):
        raise _refuse(CORRUPT, "output directory must be inside RUNNER_TEMP")
    try:
        current = path.lstat()
    except FileNotFoundError:
        path.mkdir()
    else:
        if not _is_plain_directory(current):
            raise _refuse(CORRUPT, "output directory must be a plain non-reparse directory")
        if any(path.iterdir()):
            raise _refuse(CORRUPT, "output directory must be empty")
    try:
        created = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _refuse(CORRUPT, f"cannot verify output directory: {exc}") from exc
    if not _is_plain_directory(created) or not resolved.is_relative_to(runner):
        raise _refuse(CORRUPT, "output directory escaped RUNNER_TEMP or became a reparse point")
    return path


def _validate_package_record(record: Any, kind: str, expected_name: str) -> tuple[int, str, str]:
    if not isinstance(record, dict):
        raise _refuse(CORRUPT, f"canonical {kind} evidence is missing")
    filename = record.get("filename")
    source_text = record.get("path")
    size = record.get("size_bytes")
    digest = record.get("sha256")
    if filename != expected_name or not isinstance(source_text, str) or Path(source_text).name != expected_name:
        raise _refuse(CORRUPT, f"canonical {kind} filename is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES:
        raise _refuse(CORRUPT, f"canonical {kind} size is invalid")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise _refuse(CORRUPT, f"canonical {kind} digest is invalid")
    return size, digest, source_text


def _copy_evidenced_package(
    *,
    source_text: str,
    destination: Path,
    runner_temp: Path,
    expected_size: int,
    expected_digest: str,
    label: str,
) -> None:
    source = Path(source_text)
    if not source.is_absolute():
        raise _refuse(CORRUPT, f"{label} source path must be absolute")
    try:
        link_info = source.lstat()
        resolved_source = source.resolve(strict=True)
        resolved_runner = runner_temp.resolve(strict=True)
    except ValueError as exc:
        raise _refuse(CORRUPT, f"{label} source path is malformed: {exc}") from exc
    except OSError as exc:
        raise _refuse(MISSING, f"cannot resolve {label}: {exc}") from exc
    if not _is_plain_file(link_info):
        raise _refuse(CORRUPT, f"{label} source must be a regular non-reparse file")
    if not resolved_source.is_relative_to(resolved_runner):
        raise _refuse(CORRUPT, f"{label} source is outside RUNNER_TEMP")
    raw = _read_regular_file(source, label, MAX_PACKAGE_BYTES)
    if len(raw) != expected_size:
        raise _refuse(DIGEST_MISMATCH, f"{label} size does not match gate evidence")
    if _sha256_bytes(raw) != expected_digest:
        raise _refuse(DIGEST_MISMATCH, f"{label} digest does not match gate evidence")
    _write_new(destination, raw)
    copied_digest, copied_size = _sha256_file(destination, f"copied {label}")
    if copied_size != expected_size or copied_digest != expected_digest:
        raise _refuse(DIGEST_MISMATCH, f"copied {label} changed during custody transfer")


def _package_names(version: str) -> dict[str, str]:
    return {
        "sdist": f"agenttalk-{version}.tar.gz",
        "wheel": f"agenttalk-{version}-py3-none-any.whl",
    }


def export_canonical_packages(
    *,
    repo_root: Path,
    raw_evidence_path: Path,
    runner_temp: Path,
    output_dir: Path,
    candidate_sha: str,
    version: str,
) -> None:
    """Copy the passing canonical leg's exact built bytes before runner cleanup."""

    _validate_identity(candidate_sha, version)
    context = _gate_context(repo_root)
    record, _ = _load_json(raw_evidence_path, "canonical gate evidence")
    subject_issues = _check_subject(record, candidate_sha, version, "canonical gate evidence")
    if subject_issues:
        raise ReleaseEvidenceError(subject_issues)
    try:
        validated = context.validate_run(record)
    except dev_gate.GateBlock as exc:
        raise _refuse(CORRUPT, f"canonical gate evidence is invalid: {exc.code}: {exc.detail}") from exc
    if validated.get("ci_leg") != context.canonical_leg or context.canonical_leg != CANONICAL_LEG:
        raise _refuse(CORRUPT, "gate evidence is not from the committed canonical linux/3.12 leg")
    if validated.get("verdict") != "pass":
        raise _refuse(GATE_FAILED, "canonical gate leg did not pass")
    destination = _empty_output_directory(output_dir, runner_temp=runner_temp)
    artifacts = validated.get("artifacts")
    if not isinstance(artifacts, dict):
        raise _refuse(CORRUPT, "canonical gate evidence has no package artifacts")
    for kind, expected_name in _package_names(version).items():
        size, digest, source_text = _validate_package_record(artifacts.get(kind), kind, expected_name)
        _copy_evidenced_package(
            source_text=source_text,
            destination=destination / expected_name,
            runner_temp=runner_temp,
            expected_size=size,
            expected_digest=digest,
            label=f"canonical {kind}",
        )


def write_gate_receipt(
    *,
    repo_root: Path,
    aggregate_path: Path,
    output_path: Path,
    candidate_sha: str,
    version: str,
    artifact_prefix: str,
    repository: str,
    workflow_ref: str,
    workflow_sha: str,
    actor: str,
    run_id: int,
    run_attempt: int,
    preflight_at: str,
    created_at: datetime | None = None,
) -> None:
    """Bind the aggregate to the GitHub run/attempt that produced it."""

    _validate_identity(candidate_sha, version)
    if run_attempt != 1:
        raise _refuse(STALE, "release provenance requires a fresh dispatch, not a partial workflow rerun")
    if workflow_sha != candidate_sha:
        raise _refuse(SHA_MISMATCH, "executing workflow SHA differs from the explicit candidate SHA")
    if _ARTIFACT_PREFIX.fullmatch(artifact_prefix) is None:
        raise _refuse(CORRUPT, "artifact prefix is malformed")
    context = _gate_context(repo_root)
    aggregate, raw = _load_json(aggregate_path, "gate aggregate")
    issues = _check_subject(aggregate, candidate_sha, version, "gate aggregate")
    if issues:
        raise ReleaseEvidenceError(issues)
    try:
        validated = context.validate_aggregate(aggregate)
    except dev_gate.GateBlock as exc:
        raise _refuse(CORRUPT, f"gate aggregate is invalid: {exc.code}: {exc.detail}") from exc
    if validated.get("verdict") != "pass" or validated.get("complete") is not True or validated.get("blockers"):
        raise _refuse(GATE_FAILED, "gate aggregate is not a complete pass")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise _refuse(CORRUPT, "GitHub run ID is invalid")
    if not isinstance(run_attempt, int) or isinstance(run_attempt, bool) or run_attempt <= 0:
        raise _refuse(CORRUPT, "GitHub run attempt is invalid")
    preflight = _parse_time(preflight_at, "preflight_at")
    created = created_at or _utc_now()
    if created < preflight - MAX_CLOCK_SKEW:
        raise _refuse(STALE, "gate receipt predates this dispatch preflight")
    receipt = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "artifact_type": "agenttalk-release-gate-receipt",
        "subject": {
            "candidate_sha": candidate_sha,
            "candidate_tree": aggregate["subject"]["candidate_tree"],
            "version": version,
        },
        "github": {
            "repository": repository,
            "workflow_ref": workflow_ref,
            "workflow_sha": workflow_sha,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "actor": actor,
        },
        "artifact_prefix": artifact_prefix,
        "preflight_at": _format_time(preflight),
        "created_at": _format_time(created),
        "aggregate": {
            "filename": "dev-gate-aggregate.json",
            "sha256": _sha256_bytes(raw),
        },
    }
    _write_new(output_path, _json_bytes(receipt))


def _directory_entries(path: Path, label: str, issues: list[Refusal]) -> dict[str, Path]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        issues.append(Refusal(MISSING, f"{label} directory is missing"))
        return {}
    except OSError as exc:
        issues.append(Refusal(CORRUPT, f"cannot inspect {label}: {exc}"))
        return {}
    if not _is_plain_directory(info):
        issues.append(Refusal(CORRUPT, f"{label} must be a plain non-reparse directory"))
        return {}
    try:
        return {entry.name: entry for entry in path.iterdir()}
    except OSError as exc:
        issues.append(Refusal(CORRUPT, f"cannot enumerate {label}: {exc}"))
        return {}


def _expect_entries(
    path: Path,
    label: str,
    expected: set[str],
    issues: list[Refusal],
) -> dict[str, Path]:
    entries = _directory_entries(path, label, issues)
    for missing in sorted(expected - set(entries)):
        issues.append(Refusal(MISSING, f"{label} is missing {missing}"))
    for extra in sorted(set(entries) - expected):
        issues.append(Refusal(CORRUPT, f"{label} contains unexpected entry {extra}"))
    return entries


def _load_for_bundle(path: Path, label: str, issues: list[Refusal]) -> tuple[dict[str, Any], bytes] | None:
    try:
        return _load_json(path, label)
    except ReleaseEvidenceError as exc:
        issues.extend(exc.issues)
        return None


def _validate_receipt(
    receipt: dict[str, Any],
    *,
    candidate_sha: str,
    version: str,
    artifact_prefix: str,
    run_id: int,
    run_attempt: int,
    repository: str,
    workflow_ref: str,
    workflow_sha: str,
    actor: str,
    candidate_tree: str | None,
    preflight_at: datetime,
    now: datetime,
) -> list[Refusal]:
    issues: list[Refusal] = []
    if set(receipt) != {
        "schema_version",
        "artifact_type",
        "subject",
        "github",
        "artifact_prefix",
        "preflight_at",
        "created_at",
        "aggregate",
    }:
        issues.append(Refusal(CORRUPT, "gate receipt fields do not match schema v1"))
    if receipt.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        issues.append(Refusal(CORRUPT, "gate receipt schema version is unsupported"))
    if receipt.get("artifact_type") != "agenttalk-release-gate-receipt":
        issues.append(Refusal(CORRUPT, "gate receipt artifact type is invalid"))
    issues.extend(_check_subject(receipt, candidate_sha, version, "gate receipt"))
    subject = receipt.get("subject")
    if not isinstance(subject, dict) or set(subject) != {"candidate_sha", "candidate_tree", "version"}:
        issues.append(Refusal(CORRUPT, "gate receipt subject fields are invalid"))
    elif subject.get("candidate_tree") != candidate_tree:
        issues.append(Refusal(SHA_MISMATCH, "gate receipt is bound to a different candidate tree"))
    github = receipt.get("github")
    expected_github = {
        "repository": repository,
        "workflow_ref": workflow_ref,
        "workflow_sha": workflow_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "actor": actor,
    }
    if not isinstance(github, dict) or set(github) != set(expected_github):
        issues.append(Refusal(CORRUPT, "gate receipt GitHub binding is missing"))
    else:
        if github.get("run_id") != run_id or github.get("run_attempt") != run_attempt:
            issues.append(Refusal(STALE, "gate receipt belongs to a different GitHub run or attempt"))
        if github.get("workflow_sha") != workflow_sha:
            issues.append(Refusal(SHA_MISMATCH, "gate receipt executing workflow is bound to another SHA"))
        for field in ("repository", "workflow_ref", "actor"):
            if github.get(field) != expected_github[field]:
                issues.append(Refusal(CORRUPT, f"gate receipt GitHub {field} binding differs"))
    if receipt.get("artifact_prefix") != artifact_prefix:
        issues.append(Refusal(STALE, "gate receipt uses a different attempt artifact namespace"))
    try:
        recorded_preflight = _parse_time(receipt.get("preflight_at"), "gate receipt preflight_at")
    except ReleaseEvidenceError as exc:
        issues.extend(exc.issues)
    else:
        if recorded_preflight != preflight_at:
            issues.append(Refusal(STALE, "gate receipt belongs to a different dispatch preflight"))
    issues.extend(
        _check_freshness(
            receipt.get("created_at"),
            label="gate receipt created_at",
            preflight_at=preflight_at,
            now=now,
        )
    )
    aggregate = receipt.get("aggregate")
    if (
        not isinstance(aggregate, dict)
        or set(aggregate) != {"filename", "sha256"}
        or aggregate.get("filename") != "dev-gate-aggregate.json"
        or not isinstance(aggregate.get("sha256"), str)
        or _SHA256.fullmatch(aggregate["sha256"]) is None
    ):
        issues.append(Refusal(CORRUPT, "gate receipt aggregate binding is invalid"))
    return issues


def assemble_provenance_bundle(
    *,
    repo_root: Path,
    aggregate_dir: Path,
    legs_dir: Path,
    packages_dir: Path,
    runner_temp: Path,
    output_dir: Path,
    candidate_sha: str,
    version: str,
    artifact_prefix: str,
    repository: str,
    workflow_ref: str,
    workflow_sha: str,
    actor: str,
    event_name: str,
    run_id: int,
    run_attempt: int,
    run_url: str,
    preflight_at: str,
    codeql_result: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify every retained byte and assemble one self-contained candidate artifact."""

    _validate_identity(candidate_sha, version)
    if run_attempt != 1:
        raise _refuse(STALE, "release provenance requires a fresh dispatch, not a partial workflow rerun")
    if workflow_sha != candidate_sha:
        raise _refuse(SHA_MISMATCH, "executing workflow SHA differs from the explicit candidate SHA")
    if event_name != "workflow_dispatch":
        raise _refuse(CANDIDATE_INVALID, "release provenance must originate from workflow_dispatch")
    if _ARTIFACT_PREFIX.fullmatch(artifact_prefix) is None:
        raise _refuse(CORRUPT, "artifact prefix is malformed")
    current_time = (now or _utc_now()).astimezone(timezone.utc)
    preflight = _parse_time(preflight_at, "preflight_at")
    issues = _check_freshness(
        _format_time(preflight),
        label="dispatch preflight_at",
        preflight_at=preflight,
        now=current_time,
    )
    context = _gate_context(repo_root)
    if context.binding.candidate_sha != candidate_sha:
        issues.append(Refusal(SHA_MISMATCH, "current checkout differs from the explicit candidate SHA"))
    if context.canonical_leg != CANONICAL_LEG:
        issues.append(Refusal(CORRUPT, "committed canonical gate leg is not linux/3.12"))

    aggregate_entries = _expect_entries(
        aggregate_dir,
        "aggregate artifact",
        {"dev-gate-aggregate.json", "release-gate-receipt.json"},
        issues,
    )
    package_names = _package_names(version)
    package_entries = _expect_entries(
        packages_dir,
        "canonical package artifact",
        set(package_names.values()),
        issues,
    )
    expected_leg_dirs = {
        f"{artifact_prefix}-leg-{leg.replace('/', '-')}": leg for leg in context.expected_legs
    }
    leg_root_entries = _expect_entries(legs_dir, "gate leg artifacts", set(expected_leg_dirs), issues)

    aggregate_loaded = (
        _load_for_bundle(aggregate_entries["dev-gate-aggregate.json"], "gate aggregate", issues)
        if "dev-gate-aggregate.json" in aggregate_entries
        else None
    )
    receipt_loaded = (
        _load_for_bundle(aggregate_entries["release-gate-receipt.json"], "gate receipt", issues)
        if "release-gate-receipt.json" in aggregate_entries
        else None
    )
    raw_by_leg: dict[str, tuple[dict[str, Any], bytes, Path]] = {}
    for directory_name, leg in expected_leg_dirs.items():
        directory = leg_root_entries.get(directory_name)
        if directory is None:
            continue
        entries = _expect_entries(directory, f"{leg} artifact", {"dev-gate-evidence.json"}, issues)
        evidence_path = entries.get("dev-gate-evidence.json")
        if evidence_path is None:
            continue
        loaded = _load_for_bundle(evidence_path, f"{leg} evidence", issues)
        if loaded is not None:
            raw_by_leg[leg] = (*loaded, evidence_path)

    aggregate: dict[str, Any] | None = None
    aggregate_raw: bytes | None = None
    if aggregate_loaded is not None:
        aggregate, aggregate_raw = aggregate_loaded
        issues.extend(_check_subject(aggregate, candidate_sha, version, "gate aggregate"))
        issues.extend(
            _check_freshness(
                aggregate.get("finished_at"),
                label="gate aggregate finished_at",
                preflight_at=preflight,
                now=current_time,
            )
        )
        if aggregate.get("verdict") != "pass" or aggregate.get("complete") is not True or aggregate.get("blockers"):
            issues.append(Refusal(GATE_FAILED, "gate aggregate is not a complete pass"))
        try:
            context.validate_aggregate(aggregate)
        except dev_gate.GateBlock as exc:
            issues.append(Refusal(CORRUPT, f"gate aggregate is invalid: {exc.code}: {exc.detail}"))

    receipt: dict[str, Any] | None = None
    receipt_raw: bytes | None = None
    if receipt_loaded is not None:
        receipt, receipt_raw = receipt_loaded
        aggregate_subject = aggregate.get("subject") if isinstance(aggregate, dict) else None
        issues.extend(
            _validate_receipt(
                receipt,
                candidate_sha=candidate_sha,
                version=version,
                artifact_prefix=artifact_prefix,
                run_id=run_id,
                run_attempt=run_attempt,
                repository=repository,
                workflow_ref=workflow_ref,
                workflow_sha=workflow_sha,
                actor=actor,
                candidate_tree=(
                    aggregate_subject.get("candidate_tree") if isinstance(aggregate_subject, dict) else None
                ),
                preflight_at=preflight,
                now=current_time,
            )
        )
        if aggregate_raw is not None:
            aggregate_record = receipt.get("aggregate")
            recorded_digest = aggregate_record.get("sha256") if isinstance(aggregate_record, dict) else None
            if recorded_digest != _sha256_bytes(aggregate_raw):
                issues.append(Refusal(DIGEST_MISMATCH, "gate receipt aggregate digest does not match"))

    aggregate_legs: dict[str, dict[str, Any]] = {}
    if aggregate is not None and isinstance(aggregate.get("legs"), list):
        aggregate_legs = {
            item.get("ci_leg"): item
            for item in aggregate["legs"]
            if isinstance(item, dict) and isinstance(item.get("ci_leg"), str)
        }
    for leg in context.expected_legs:
        loaded = raw_by_leg.get(leg)
        if loaded is None:
            continue
        record, raw, _ = loaded
        issues.extend(_check_subject(record, candidate_sha, version, f"{leg} evidence"))
        issues.extend(
            _check_freshness(
                record.get("finished_at"),
                label=f"{leg} finished_at",
                preflight_at=preflight,
                now=current_time,
            )
        )
        if record.get("ci_leg") != leg:
            issues.append(Refusal(CORRUPT, f"{leg} artifact contains evidence for another leg"))
        if record.get("verdict") != "pass":
            issues.append(Refusal(GATE_FAILED, f"{leg} did not pass"))
        try:
            context.validate_run(record)
        except dev_gate.GateBlock as exc:
            issues.append(Refusal(CORRUPT, f"{leg} evidence is invalid: {exc.code}: {exc.detail}"))
        aggregate_leg = aggregate_legs.get(leg)
        recorded_digest = aggregate_leg.get("artifact_sha256") if isinstance(aggregate_leg, dict) else None
        if recorded_digest != _sha256_bytes(raw):
            issues.append(Refusal(DIGEST_MISMATCH, f"{leg} evidence digest does not match the aggregate"))

    if codeql_result != "success":
        issues.append(Refusal(GATE_FAILED, f"CodeQL result is {codeql_result!r}, not success"))

    canonical = raw_by_leg.get(context.canonical_leg)
    package_records: dict[str, tuple[int, str, str]] = {}
    if canonical is not None:
        artifacts = canonical[0].get("artifacts")
        if not isinstance(artifacts, dict):
            issues.append(Refusal(CORRUPT, "canonical evidence has no package records"))
        else:
            for kind, expected_name in package_names.items():
                try:
                    package_records[kind] = _validate_package_record(artifacts.get(kind), kind, expected_name)
                except ReleaseEvidenceError as exc:
                    issues.extend(exc.issues)
                    continue
                package_path = package_entries.get(expected_name)
                if package_path is None:
                    continue
                digest, size = _sha256_file(package_path, f"downloaded canonical {kind}")
                expected_size, expected_digest, _ = package_records[kind]
                if size != expected_size or digest != expected_digest:
                    issues.append(Refusal(DIGEST_MISMATCH, f"downloaded canonical {kind} differs from gate evidence"))

    if issues:
        raise ReleaseEvidenceError(issues)
    if aggregate is None or aggregate_raw is None or receipt is None or receipt_raw is None or canonical is None:
        raise _refuse(CORRUPT, "validated release evidence became incomplete")

    destination = _empty_output_directory(output_dir, runner_temp=runner_temp)
    evidence_dir = destination / "gate-evidence"
    evidence_dir.mkdir()
    _write_new(evidence_dir / "dev-gate-aggregate.json", aggregate_raw)
    _write_new(evidence_dir / "release-gate-receipt.json", receipt_raw)
    leg_provenance: list[dict[str, Any]] = []
    for leg in context.expected_legs:
        record, raw, path = raw_by_leg[leg]
        expected_again = _read_regular_file(path, f"{leg} evidence recheck", MAX_EVIDENCE_BYTES)
        if expected_again != raw:
            raise _refuse(CORRUPT, f"{leg} evidence changed between validation and custody transfer")
        durable_name = f"dev-gate-leg-{leg.replace('/', '-')}.json"
        _write_new(evidence_dir / durable_name, raw)
        leg_provenance.append(
            {
                "ci_leg": leg,
                "run_id": record["run_id"],
                "finished_at": record["finished_at"],
                "filename": f"gate-evidence/{durable_name}",
                "sha256": _sha256_bytes(raw),
            }
        )

    package_provenance: dict[str, dict[str, Any]] = {}
    for kind, expected_name in package_names.items():
        expected_size, expected_digest, _ = package_records[kind]
        _copy_evidenced_package(
            source_text=str(package_entries[expected_name]),
            destination=destination / expected_name,
            runner_temp=runner_temp,
            expected_size=expected_size,
            expected_digest=expected_digest,
            label=f"downloaded canonical {kind}",
        )
        package_provenance[kind] = {
            "filename": expected_name,
            "size_bytes": expected_size,
            "sha256": expected_digest,
            "canonical_leg": context.canonical_leg,
            "tested_as": (
                "built and archive-contract checked" if kind == "sdist" else "built, installed, and runtime tested"
            ),
        }

    canonical_record = canonical[0]
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "artifact_type": "agenttalk-release-candidate-provenance",
        "verdict": "pass",
        "generated_at": _format_time(current_time),
        "freshness": {
            "preflight_at": _format_time(preflight),
            "maximum_age_seconds": int(MAX_EVIDENCE_AGE.total_seconds()),
        },
        "subject": {
            "repository": repository,
            "version": version,
            "prospective_tag": f"v{version}",
            "candidate_sha": candidate_sha,
            "candidate_tree": aggregate["subject"]["candidate_tree"],
        },
        "github": {
            "workflow_ref": workflow_ref,
            "workflow_sha": workflow_sha,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "run_url": run_url,
            "actor": actor,
            "event_name": event_name,
        },
        "gate": {
            "profile": "release",
            "canonical_leg": context.canonical_leg,
            "aggregate": {
                "filename": "gate-evidence/dev-gate-aggregate.json",
                "sha256": _sha256_bytes(aggregate_raw),
            },
            "manifest": aggregate["manifest"],
            "runner": canonical_record["runner"],
            "legs": leg_provenance,
            "external_inputs": [
                {"ci_leg": leg, "inputs": raw_by_leg[leg][0].get("external_inputs", [])}
                for leg in context.expected_legs
            ],
        },
        "codeql": {
            "workflow": ".github/workflows/security.yml",
            "job": "codeql",
            "queries": "security-extended",
            "result": codeql_result,
            "candidate_sha": candidate_sha,
            "github_run_id": run_id,
            "github_run_attempt": run_attempt,
        },
        "packages": package_provenance,
        "retention": {
            "carrier": "github-actions-artifact",
            "requested_days": RETENTION_DAYS,
            "permanent": False,
            "warning": "repository policy or run deletion may shorten availability",
            "next_increment": "attach these exact bytes to the GitHub Release before expiry",
        },
    }
    provenance_path = destination / "release-provenance.json"
    _write_new(provenance_path, _json_bytes(provenance))

    checksum_rows = []
    for path in sorted((item for item in destination.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise _refuse(CORRUPT, f"durable bundle contains symlink {path}")
        relative = path.relative_to(destination).as_posix()
        digest, _ = _sha256_file(path, f"durable bundle member {relative}")
        checksum_rows.append(f"{digest}  {relative}\n")
    _write_new(destination / "SHA256SUMS", "".join(checksum_rows).encode("ascii"))
    return provenance


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight", help="validate a post-bump release candidate")
    preflight.add_argument("--repo-root", type=Path, required=True)
    preflight.add_argument("--candidate-sha", required=True)
    preflight.add_argument("--event-sha", required=True)
    preflight.add_argument("--event-ref", required=True)
    preflight.add_argument("--workflow-sha", required=True)
    preflight.add_argument("--version", required=True)
    preflight.add_argument("--run-attempt", type=_positive_int, required=True)

    export = commands.add_parser("export", help="retain the canonical gate-built packages")
    export.add_argument("--repo-root", type=Path, required=True)
    export.add_argument("--raw-evidence", type=Path, required=True)
    export.add_argument("--runner-temp", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--candidate-sha", required=True)
    export.add_argument("--version", required=True)

    receipt = commands.add_parser("gate-receipt", help="bind gate evidence to this run attempt")
    receipt.add_argument("--repo-root", type=Path, required=True)
    receipt.add_argument("--aggregate", type=Path, required=True)
    receipt.add_argument("--output", type=Path, required=True)
    receipt.add_argument("--candidate-sha", required=True)
    receipt.add_argument("--version", required=True)
    receipt.add_argument("--artifact-prefix", required=True)
    receipt.add_argument("--repository", required=True)
    receipt.add_argument("--workflow-ref", required=True)
    receipt.add_argument("--workflow-sha", required=True)
    receipt.add_argument("--actor", required=True)
    receipt.add_argument("--run-id", type=_positive_int, required=True)
    receipt.add_argument("--run-attempt", type=_positive_int, required=True)
    receipt.add_argument("--preflight-at", required=True)

    assemble = commands.add_parser("assemble", help="verify and assemble durable provenance")
    assemble.add_argument("--repo-root", type=Path, required=True)
    assemble.add_argument("--aggregate-dir", type=Path, required=True)
    assemble.add_argument("--legs-dir", type=Path, required=True)
    assemble.add_argument("--packages-dir", type=Path, required=True)
    assemble.add_argument("--runner-temp", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--candidate-sha", required=True)
    assemble.add_argument("--version", required=True)
    assemble.add_argument("--artifact-prefix", required=True)
    assemble.add_argument("--repository", required=True)
    assemble.add_argument("--workflow-ref", required=True)
    assemble.add_argument("--workflow-sha", required=True)
    assemble.add_argument("--actor", required=True)
    assemble.add_argument("--event-name", required=True)
    assemble.add_argument("--run-id", type=_positive_int, required=True)
    assemble.add_argument("--run-attempt", type=_positive_int, required=True)
    assemble.add_argument("--run-url", required=True)
    assemble.add_argument("--preflight-at", required=True)
    assemble.add_argument("--codeql-result", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            validate_preflight(
                repo_root=args.repo_root,
                candidate_sha=args.candidate_sha,
                event_sha=args.event_sha,
                event_ref=args.event_ref,
                workflow_sha=args.workflow_sha,
                version=args.version,
                run_attempt=args.run_attempt,
            )
        elif args.command == "export":
            export_canonical_packages(
                repo_root=args.repo_root,
                raw_evidence_path=args.raw_evidence,
                runner_temp=args.runner_temp,
                output_dir=args.output,
                candidate_sha=args.candidate_sha,
                version=args.version,
            )
        elif args.command == "gate-receipt":
            write_gate_receipt(
                repo_root=args.repo_root,
                aggregate_path=args.aggregate,
                output_path=args.output,
                candidate_sha=args.candidate_sha,
                version=args.version,
                artifact_prefix=args.artifact_prefix,
                repository=args.repository,
                workflow_ref=args.workflow_ref,
                workflow_sha=args.workflow_sha,
                actor=args.actor,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                preflight_at=args.preflight_at,
            )
        else:
            assemble_provenance_bundle(
                repo_root=args.repo_root,
                aggregate_dir=args.aggregate_dir,
                legs_dir=args.legs_dir,
                packages_dir=args.packages_dir,
                runner_temp=args.runner_temp,
                output_dir=args.output,
                candidate_sha=args.candidate_sha,
                version=args.version,
                artifact_prefix=args.artifact_prefix,
                repository=args.repository,
                workflow_ref=args.workflow_ref,
                workflow_sha=args.workflow_sha,
                actor=args.actor,
                event_name=args.event_name,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                run_url=args.run_url,
                preflight_at=args.preflight_at,
                codeql_result=args.codeql_result,
            )
    except ReleaseEvidenceError as exc:
        for issue in exc.issues:
            print(f"{issue.code}: {issue.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
