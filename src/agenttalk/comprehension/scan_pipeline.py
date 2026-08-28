"""End-to-end scan orchestration (item 9): the CLI's ``scan``/``status``/
``report``/``validate`` commands are thin callers of the functions here.

Implements DESIGN-55-comprehension-plane.md's "Scan behavior" nine-step
pipeline by composing PR-A's spine (privacy/lock/staging/publish) with
PR-B's producer (discovery/worker/adapters/artifact builders/projector) -
no new extraction or storage logic lives in this module, only the glue.

Scope simplifications for this slice, flagged for review, not blocking
forks:
- ``config.json`` is not parsed yet - no ``--scope``/``--exclude``
  narrowing, no declared feature confirmations, no readiness-policy
  overrides. A scan always covers the whole project root with conservative
  defaults.
- Rich per-record evidence pointers (design: every record carries "bounded
  local evidence pointers") are not populated by items 4-7 yet - deferred
  alongside config.json, since retrofitting them is mechanical once a
  concrete pointer shape is agreed, and does not block the exit-gate cap
  measurement (item 11), which only needs discovery's own byte/entry
  counting to be correct - already true independent of this gap.
- ``validate`` performs full-run schema/cross-reference integrity only;
  the design's separate EXTERNAL-pointer revalidation step has nothing to
  revalidate yet, for the same evidence-pointer reason above.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    dependencies_artifact,
    digests,
    discovery,
    features_artifact,
    lock,
    modules_artifact,
    paths,
    privacy,
    projector,
    publish,
    readiness_artifact,
    staging,
    worker,
)
from .adapters import java as java_adapter
from .envelope import read_json_document, resolve_under_root, validate_envelope, validate_scan_id
from .errors import ComprehensionError, EnvelopeError
from .privacy import PrivacyPreflightResult, VcsPrivacyRefused

GENERATOR_VERSION = 1

MODULES_ARTIFACT_TYPE = modules_artifact.MODULES_ARTIFACT_TYPE
DEPENDENCIES_ARTIFACT_TYPE = dependencies_artifact.DEPENDENCIES_ARTIFACT_TYPE
FEATURES_ARTIFACT_TYPE = "agenttalk.comprehension.features"
READINESS_ARTIFACT_TYPE = "agenttalk.comprehension.readiness"
PROBLEMS_ARTIFACT_TYPE = "agenttalk.comprehension.problems"
SCAN_ARTIFACT_TYPE = "agenttalk.comprehension.scan"
FEATURES_SCHEMA_VERSION = 1
READINESS_SCHEMA_VERSION = 1
PROBLEMS_SCHEMA_VERSION = 1
SCAN_SCHEMA_VERSION = 1


class ScanRefused(ComprehensionError):
    """A precondition (privacy, an existing incompatible lock, a missing
    work_id for an attended acknowledgement) refused this scan before
    anything was written. Never raised mid-pipeline."""

    reason_code = "comprehension_scan_refused"


def _generate_scan_id(now: datetime | None) -> str:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    nonce = uuid.uuid4().hex[:8]
    return validate_scan_id(f"{stamp}-{nonce}")


def _utc_now_iso(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _envelope(*, artifact_type: str, schema_version: int, scan_id: str, generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": schema_version, "artifact_type": artifact_type,
        "scan_id": scan_id, "generated_at": generated_at,
    }


def _write_json_document(path: Path, document: dict[str, Any]) -> None:
    path.write_text(
        digests.canonical_json_bytes(document).decode("utf-8"), encoding="utf-8",
    )


@dataclass(frozen=True)
class ScanOutcome:
    scan_id: str
    status: str
    index: dict[str, Any]
    run_dir: Path


def _resolved_run_dir(comprehension_dir: Path, scan_id: str) -> Path:
    """M1 (cold-read, PR-B fix round 3): the WRITE path (publish.py's
    ``rename_staging_to_run``) validates and resolve-confines a scan_id
    under ``runs/`` before it ever touches disk; every READ path
    (``get_status``/``get_report``/``validate_run``) passed a caller-
    supplied ``--run`` value straight into ``paths.run_dir`` with no such
    check, so a run identifier resolving OUTSIDE the published-runs
    directory was accepted and its document read and reported as if it
    were a real, immutable run. Mirrors the write path exactly: reject
    against the closed scan-ID grammar first, then resolve-confine under
    ``runs/`` as defense in depth, before any read is attempted."""
    validate_scan_id(scan_id)
    return resolve_under_root(scan_id, root=paths.runs_dir(comprehension_dir), label="run identifier")


def _obtain_privacy(
    root: Path, *, acknowledge_unignored: bool, work_id: str | None,
) -> PrivacyPreflightResult:
    try:
        return privacy.run_privacy_preflight(root)
    except VcsPrivacyRefused as exc:
        if not acknowledge_unignored:
            raise
        if not work_id:
            raise ScanRefused(
                "--acknowledge-unignored-private-store requires --work-id "
                "(design: \"applies to one run bound to an existing work item\")"
            ) from exc
        return privacy.acknowledge_unignored_private_store(
            root, vcs_kind=exc.vcs_kind, work_id=work_id, matched_rule=None,
        )


def run_scan(
    root: Path, *,
    work_id: str | None = None,
    acknowledge_unignored: bool = False,
    recover_stale_lock: bool = False,
    now: datetime | None = None,
) -> ScanOutcome:
    """The full nine-step pipeline for one immutable run. Raises whatever
    typed error the failing step raises (``VcsPrivacyRefused``,
    ``ScanLockContended``, ``ScanLockUnrecoverable``, ``ArtifactLimitExceeded``,
    ...) - this function never converts one refusal into another; the CLI
    layer decides how to present each one (including routing to the
    escalation module for a headless caller)."""
    root = Path(root).resolve()
    agenttalk_dir = root / ".agenttalk"
    comprehension_dir = paths.comprehension_dir(agenttalk_dir)

    if recover_stale_lock:
        lock.recover_stale_lock(comprehension_dir)

    privacy_result = _obtain_privacy(
        root, acknowledge_unignored=acknowledge_unignored, work_id=work_id)

    _prior_index, predecessor_digest = publish.read_current_index(comprehension_dir)
    lock_handle = lock.acquire_scan_lock(
        comprehension_dir, privacy=privacy_result, predecessor_index_digest=predecessor_digest,
    )

    try:
        scan_id = _generate_scan_id(now)
        staging_handle = staging.create_staging_dir(scan_id=scan_id, lock_handle=lock_handle)
        generated_at = _utc_now_iso(now)

        discovery_result = discovery.enumerate_scope(root, comprehension_dir)
        if not discovery_result.files:
            # M3 (cold-read, PR-B fix round 3): an empty scope (nothing
            # addressable was enumerated at all) is a command error, not a
            # valid, publishable, complete zero-unit run - the design
            # names this a caller mistake (wrong --root, or an
            # over-broad exclusion policy), never a legitimate result to
            # publish and hand back as "complete".
            raise ScanRefused(
                f"no files were enumerated under {root} - refusing to publish a "
                "vacuous zero-unit run; check --root and the exclusion policy")
        relative_paths = [f.relative_path for f in discovery_result.files]
        worker_result = worker.run_sanitized_worker(root, relative_paths)

        # B-3 (reviewer-3, PR-B delta review): pom.xml build-relation
        # extraction is dispatched INSIDE the sanitized worker now (same as
        # every other adapter path) - worker_result.java_results already
        # carries a pom.xml's parsed build edges keyed by its own relative
        # path, so no separate parent-process read or separate
        # build_edges_by_path threading is needed here anymore.
        java_results = {
            path: java_adapter.file_result_from_json(payload)
            for path, payload in worker_result.java_results.items()
        }
        # B3 (cold-read, PR-B fix round 3): a file the adapter failed to
        # parse (or the worker could not even read) must never be
        # reported as source_understood=satisfied just because its
        # extension maps to a known language - readiness needs to know
        # WHICH paths degraded, not just which paths parsed.
        parse_failed_paths = frozenset(
            p.relative_path for p in worker_result.problems if p.reason_code == "parse_failed"
        )

        modules = modules_artifact.build_modules(
            discovery_result, java_results, parse_failed_paths=parse_failed_paths)
        dependencies = dependencies_artifact.build_dependencies(java_results)
        entry_points, features = features_artifact.build_features(java_results)
        readiness_signals, readiness_summaries = readiness_artifact.build_readiness(
            modules, dependencies, features)

        problems = [
            {"reason_code": p["reason_code"], "path": p.get("path"), "detail": p["detail"]}
            for p in discovery_result.problems
        ] + [
            {"reason_code": p.reason_code, "path": p.relative_path, "detail": p.detail}
            for p in worker_result.problems
        ]
        status = "degraded" if (discovery_result.degraded or problems) else "complete"

        modules_doc = {
            **_envelope(
                artifact_type=MODULES_ARTIFACT_TYPE, schema_version=modules_artifact.MODULES_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "units": [m.to_json() for m in modules],
        }
        dependencies_doc = {
            **_envelope(
                artifact_type=DEPENDENCIES_ARTIFACT_TYPE,
                schema_version=dependencies_artifact.DEPENDENCIES_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "edges": [e.to_json() for e in dependencies],
        }
        features_doc = {
            **_envelope(
                artifact_type=FEATURES_ARTIFACT_TYPE, schema_version=FEATURES_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "entry_points": [e.to_json() for e in entry_points],
            "features": [f.to_json() for f in features],
        }
        readiness_doc = {
            **_envelope(
                artifact_type=READINESS_ARTIFACT_TYPE, schema_version=READINESS_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "signals": [s.to_json() for s in readiness_signals],
            "summaries": [s.to_json() for s in readiness_summaries],
        }
        problems_doc = {
            **_envelope(
                artifact_type=PROBLEMS_ARTIFACT_TYPE, schema_version=PROBLEMS_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "problems": problems,
        }

        _write_json_document(staging_handle.path / "modules.json", modules_doc)
        _write_json_document(staging_handle.path / "dependencies.json", dependencies_doc)
        _write_json_document(staging_handle.path / "features.json", features_doc)
        _write_json_document(staging_handle.path / "readiness.json", readiness_doc)
        _write_json_document(staging_handle.path / "problems.json", problems_doc)

        record_counts = {
            "modules.json": len(modules_doc["units"]),
            "dependencies.json": len(dependencies_doc["edges"]),
            "features.json": len(features_doc["entry_points"]) + len(features_doc["features"]),
            "readiness.json": len(readiness_doc["signals"]),
            "problems.json": len(problems_doc["problems"]),
            # N6 (cold-read, PR-B fix round 3): scan.json's own record must
            # be counted BEFORE scan_doc is built (never after writing it) -
            # scan.json embeds this SAME dict by reference, so adding the
            # entry only after _write_json_document had already serialized
            # the document meant the PUBLISHED record_counts field always
            # disagreed with what ceilings.py actually enforced (which used
            # this dict's post-mutation state, one entry richer). scan.json
            # always contains exactly one scan record, so this is knowable
            # up front, not something that needs the document to exist first.
            "scan.json": 1,
        }

        scan_doc = {
            **_envelope(
                artifact_type=SCAN_ARTIFACT_TYPE, schema_version=SCAN_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "status": status,
            "generator_version": GENERATOR_VERSION,
            "adapters": [{
                "name": java_adapter.ADAPTER_NAME, "version": java_adapter.ADAPTER_VERSION,
                "rule_version": java_adapter.RULE_VERSION,
            }],
            "root_binding": privacy_result.root_binding,
            "platform_identity": {
                "os_family": discovery_result.platform_identity.os_family,
                "architecture": discovery_result.platform_identity.architecture,
                "path_normalization_version": discovery_result.platform_identity.path_normalization_version,
                "case_sensitive": discovery_result.platform_identity.case_sensitive,
                "unicode_normalizing": discovery_result.platform_identity.unicode_normalizing,
            },
            "whole_scope_fingerprint": discovery_result.whole_scope_fingerprint,
            "fingerprint_complete": discovery_result.fingerprint_complete,
            "exclusions": dict(sorted(discovery_result.exclusions.items())),
            "boundaries": len(discovery_result.boundaries),
            "unsupported_relations": list(java_adapter.UNSUPPORTED_RELATIONS),
            "record_counts": record_counts,
            "problem_count": len(problems),
        }
        _write_json_document(staging_handle.path / "scan.json", scan_doc)

        run_summary = {"scan_id": scan_id, "status": status}
    except BaseException as original_exc:
        # F-2 (reviewer-3, PR-B delta review): if the release itself
        # refuses (e.g. ScanLockUnrecoverable), a bare `raise` here would
        # let that NEW exception replace the original failure that
        # triggered this handler, masking the real reason from the
        # operator. Chain instead, so the original failure is always what
        # surfaces, with the release refusal attached as its cause - never
        # replaced by it.
        try:
            lock.release_scan_lock(lock_handle)
        except Exception as release_exc:  # noqa: BLE001 - the ORIGINAL failure must still surface
            raise original_exc from release_exc
        raise

    index = publish.publish_run(
        staging_handle=staging_handle, lock_handle=lock_handle,
        run_summary=run_summary, predecessor_index_digest=predecessor_digest,
        record_counts=record_counts, now=now,
    )
    run_dir = paths.run_dir(comprehension_dir, scan_id)
    return ScanOutcome(scan_id=scan_id, status=status, index=index, run_dir=run_dir)


class NotScanned(ComprehensionError):
    """No published run exists yet at this root (design: "A missing plane
    yields not_scanned, never an empty or healthy assessment.\")."""

    reason_code = "comprehension_not_scanned"


def get_status(root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    """design: "Show the latest run, completeness, source revision/
    fingerprint, freshness, adapter coverage, and problem counts." Verifies
    only the index and scan.json (design's validation-tiers "ordinary
    reads are bounded and demand-driven" - status does not read the other
    four artifacts)."""
    comprehension_dir = paths.comprehension_dir(Path(root).resolve() / ".agenttalk")
    index_doc, index_digest = publish.read_current_index(comprehension_dir)
    if index_doc is None:
        raise NotScanned(f"no comprehension run has ever been published under {root}")
    scan_id = run_id or index_doc["latest_scan_id"]
    run_dir = _resolved_run_dir(comprehension_dir, scan_id)
    scan_path = run_dir / "scan.json"
    try:
        scan_doc = validate_envelope(
            read_json_document(scan_path),
            artifact_type=SCAN_ARTIFACT_TYPE, schema_version=SCAN_SCHEMA_VERSION,
        )
    except (EnvelopeError, OSError) as exc:
        raise ComprehensionError(f"{scan_id}'s scan.json could not be verified: {exc}") from exc
    return {
        "latest_scan_id": index_doc["latest_scan_id"],
        "index_digest": index_digest,
        "run_summaries": index_doc["runs"],
        "scan_id": scan_doc["scan_id"],
        "status": scan_doc["status"],
        "generated_at": scan_doc["generated_at"],
        "adapters": scan_doc["adapters"],
        "problem_count": scan_doc["problem_count"],
        "record_counts": scan_doc["record_counts"],
        "root_binding": scan_doc["root_binding"],
        "freshness": {
            "state": "not_evaluated", "reason_code": "freshness_not_implemented_this_slice",
        },
    }


def _load_run_records(comprehension_dir: Path, scan_id: str) -> dict[str, Any]:
    run_dir = _resolved_run_dir(comprehension_dir, scan_id)

    def _load(name: str, artifact_type: str, schema_version: int) -> dict[str, Any]:
        try:
            doc = read_json_document(run_dir / name)
        except EnvelopeError as exc:
            raise ComprehensionError(f"{scan_id}'s {name} could not be read: {exc}") from exc
        return validate_envelope(doc, artifact_type=artifact_type, schema_version=schema_version)

    scan_doc = _load("scan.json", SCAN_ARTIFACT_TYPE, SCAN_SCHEMA_VERSION)
    modules_doc = _load("modules.json", MODULES_ARTIFACT_TYPE, modules_artifact.MODULES_SCHEMA_VERSION)
    dependencies_doc = _load(
        "dependencies.json", DEPENDENCIES_ARTIFACT_TYPE, dependencies_artifact.DEPENDENCIES_SCHEMA_VERSION)
    features_doc = _load("features.json", FEATURES_ARTIFACT_TYPE, FEATURES_SCHEMA_VERSION)
    readiness_doc = _load("readiness.json", READINESS_ARTIFACT_TYPE, READINESS_SCHEMA_VERSION)
    problems_doc = _load("problems.json", PROBLEMS_ARTIFACT_TYPE, PROBLEMS_SCHEMA_VERSION)

    return {
        "scan": scan_doc,
        "modules": [modules_artifact.module_record_from_json(u) for u in modules_doc["units"]],
        "dependencies": [
            dependencies_artifact.dependency_record_from_json(e) for e in dependencies_doc["edges"]
        ],
        "entry_points": [
            features_artifact.entry_point_record_from_json(e) for e in features_doc["entry_points"]
        ],
        "features": [features_artifact.feature_record_from_json(f) for f in features_doc["features"]],
        "readiness_signals": [
            readiness_artifact.readiness_signal_from_json(s) for s in readiness_doc["signals"]
        ],
        "readiness_summaries": [
            readiness_artifact.unit_readiness_summary_from_json(s) for s in readiness_doc["summaries"]
        ],
        "problems": list(problems_doc["problems"]),
    }


def get_report(
    root: Path, *,
    run_id: str | None = None,
    unit_id: str | None = None,
    feature_id: str | None = None,
    readiness_state: str | None = None,
    dependencies_only: bool = False,
) -> dict[str, Any]:
    """``report --json``'s ONE call site into :func:`projector.
    project_comprehension` - reads the persisted run back into the same
    record shapes items 4-7 build in memory, so this is provably the same
    projection PR-D's future route will also call (single-projector
    parity, C-2)."""
    comprehension_dir = paths.comprehension_dir(Path(root).resolve() / ".agenttalk")
    index_doc, _digest = publish.read_current_index(comprehension_dir)
    if index_doc is None:
        raise NotScanned(f"no comprehension run has ever been published under {root}")
    scan_id = run_id or index_doc["latest_scan_id"]
    records = _load_run_records(comprehension_dir, scan_id)
    return projector.project_comprehension(
        scan_id=records["scan"]["scan_id"], generated_at=records["scan"]["generated_at"],
        manifest_digest=None, status=records["scan"]["status"],
        modules=records["modules"], dependencies=records["dependencies"],
        entry_points=records["entry_points"], features=records["features"],
        readiness_signals=records["readiness_signals"],
        readiness_summaries=records["readiness_summaries"],
        problems=records["problems"],
        unit_id=unit_id, feature_id=feature_id, readiness_state=readiness_state,
        dependencies_only=dependencies_only,
    )


def validate_run(root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    """design: "Perform full-run integrity validation." Reads and verifies
    every artifact's envelope/schema; the design's separate EXTERNAL-
    evidence-pointer revalidation step has nothing to revalidate yet in
    this slice (see module docstring) - ``external_revalidation`` is
    reported as an explicit, named gap rather than silently omitted."""
    comprehension_dir = paths.comprehension_dir(Path(root).resolve() / ".agenttalk")
    index_doc, _digest = publish.read_current_index(comprehension_dir)
    if index_doc is None:
        raise NotScanned(f"no comprehension run has ever been published under {root}")
    scan_id = run_id or index_doc["latest_scan_id"]
    try:
        records = _load_run_records(comprehension_dir, scan_id)
        valid = True
        detail = "all artifacts verified: schema, envelope identity, and scan_id consistency"
    except ComprehensionError as exc:
        valid = False
        detail = str(exc)
        records = None
    unit_ids = {m.unit_id for m in records["modules"]} if records else set()
    dangling_edges = [
        e.edge_id for e in (records["dependencies"] if records else [])
        if e.from_unit_id not in unit_ids
    ] if records else []
    return {
        "scan_id": scan_id,
        "valid": valid and not dangling_edges,
        "detail": detail if not dangling_edges else f"{len(dangling_edges)} edge(s) reference an unknown from_unit_id",
        "external_revalidation": {
            "performed": False,
            "reason_code": "no_external_evidence_pointers_this_slice",
        },
    }
