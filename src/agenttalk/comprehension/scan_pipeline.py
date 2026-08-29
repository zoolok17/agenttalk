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
from .envelope import (
    find_case_fold_collisions,
    read_json_document,
    require_field,
    resolve_under_root,
    validate_envelope,
    validate_scan_id,
)
from .errors import ComprehensionError, EnvelopeError, bounded_detail
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


def _write_json_document(path: Path, document: dict[str, Any]) -> bytes:
    """Returns the exact canonical bytes written, so the caller can
    compute this artifact's byte SHA-256 (M2, cold-read PR-B fix round 3)
    without a second, separate read of what was just written."""
    canonical = digests.canonical_json_bytes(document)
    path.write_text(canonical.decode("utf-8"), encoding="utf-8")
    return canonical


#: Minor 7 (fifth cold read, fix round 7): the same cap + omitted-count
#: discipline projector.py's own _bounded applies to every REPORT
#: section - scan.json's "boundaries" list is written here, at scan
#: time, not by projector.py, so it needs its own small instance of the
#: same mechanism rather than an unbounded raw list.
#:
#: LOW-1 (round 7c, reviewer-3 delta on 95d9cd8): PROVISIONAL, same as
#: every other cap this package enforces (discovery.py's three resource
#: caps, ceilings.py's per-artifact/run byte-and-record limits) - pending
#: the PR-B exit-gate measurement against a representative corpus (task
#: #55 slice-1 dispatch, C-6). One discipline in one place: that
#: measurement re-tunes every cap it covers, this one included, rather
#: than a sixth cap quietly living outside it.
_MAX_BOUNDARIES = 1000


def _bounded_boundaries(entries: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    if len(entries) <= _MAX_BOUNDARIES:
        return entries, 0
    return entries[:_MAX_BOUNDARIES], len(entries) - _MAX_BOUNDARIES


#: N3 (third cold read, fix round 5): DESIGN-55-comprehension-plane.md's
#: problems.json section names ``severity`` as part of the record shape
#: (#208 downstream consumes it) but no severity was ever assigned - this
#: module's own judgment call (the design names the three severities but
#: does not pin one to each reason code, the same open call
#: readiness_artifact.py's per-check severities already are). None of
#: this slice's reason codes ever stop the SCAN from publishing (that
#: class of failure - a fatal confinement/publication error - publishes
#: no run and no problems.json at all) - every one instead means "one
#: entry was omitted, or one adapter's evidence is missing", real
#: degradation, never merely informational. All warning, pending a
#: future distinction if review wants one; PROVISIONAL like every other
#: not-yet-measured judgment call in this module.
_PROBLEM_SEVERITY_BY_REASON_CODE = {
    "parse_failed": "warning",
    "path_excluded": "warning",
    "resource_limit": "warning",
    "non_utf8_path": "warning",
    "case_collision": "warning",
    # BLOCKER 1b (fifth cold read, fix round 8): a .java file whose parse
    # succeeded but extracted zero declared types - closing the zero-
    # extraction evidence hole as a class (worker.py).
    "no_types_extracted": "warning",
    # BLOCKER fail-safe (sixth cold read, fix round 10): a route
    # annotation the java adapter could not confidently associate with
    # a class or a method - under-claimed rather than published wrong.
    "route_annotation_unassociated": "warning",
}
_DEFAULT_PROBLEM_SEVERITY = "warning"


def _problem_record(reason_code: str, path: str | None, detail: str) -> dict[str, Any]:
    return {
        "problem_id": digests.problem_id(reason_code=reason_code, path=path, detail=detail),
        "reason_code": reason_code,
        "severity": _PROBLEM_SEVERITY_BY_REASON_CODE.get(reason_code, _DEFAULT_PROBLEM_SEVERITY),
        "path": path,
        "detail": detail,
    }


def _artifact_summary(
    *, name: str, artifact_type: str, schema_version: int, record_count: int,
    doc: dict[str, Any], canonical_bytes: bytes,
) -> dict[str, Any]:
    """M2 (cold-read, PR-B fix round 3): the design requires "per-artifact
    relative path, byte SHA-256, canonical content digest, record count,
    schema version" for every durable artifact - digests.sha256_bytes and
    digests.canonical_content_digest already existed with no production
    caller. The run-level content_digest below is computed from exactly
    this shape (digests.run_content_digest reads artifact_type/
    schema_version/record_count/content_digest from each entry)."""
    return {
        "name": name,
        "artifact_type": artifact_type,
        "schema_version": schema_version,
        "record_count": record_count,
        "byte_sha256": digests.sha256_bytes(canonical_bytes),
        "content_digest": digests.canonical_content_digest(doc),
    }


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
    # N2 (fourth cold read, fix round 6): the design names "start and
    # completion times" as scan.json fields, distinct from generated_at
    # (each artifact's own envelope-generation snapshot) - captured here,
    # at the true start of this call, before privacy/lock/discovery ever
    # run.
    started_at = _utc_now_iso(now)
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
    # M-5 (second cold read, PR-B fix round 4): staging.reclaim_abandoned_
    # staging had ZERO production callers despite its own docstring
    # claiming "called automatically at lock acquisition" - wired here,
    # matching both that docstring and the design's own phrasing ("At
    # lock acquisition, the scanner reclaims only unpublished staging
    # directories..."). Cleans up whatever a PRIOR crashed or refused run
    # left behind before this run adds its own.
    staging.reclaim_abandoned_staging(comprehension_dir)

    try:
        scan_id = _generate_scan_id(now)

        # M-5 (second cold read, PR-B fix round 4): discovery and the
        # empty-scope refusal now run BEFORE staging is created - the
        # staging directory used to be created first, so every refusal
        # past this point (empty scope, and anything else that used to
        # follow) left an abandoned .staging/<scan_id>-<nonce>/ directory
        # behind with no automatic or manual remedy. Neither depends on
        # the other, so this reorder changes nothing about what a
        # successful scan actually does.
        discovery_result = discovery.enumerate_scope(root, comprehension_dir)
        if not discovery_result.files:
            # M3 (cold-read, PR-B fix round 3): an empty scope (nothing
            # addressable was enumerated at all) is a command error, not a
            # valid, publishable, complete zero-unit run - the design
            # names this a caller mistake (wrong --root, or an
            # over-broad exclusion policy), never a legitimate result to
            # publish and hand back as "complete".
            #
            # N1 (fifth cold read, fix round 8): "--root" is the GLOBAL
            # flag, not a comprehension subcommand option - it must
            # precede "comprehension" itself (`agenttalk --root <path>
            # comprehension scan`; placed after, it is an "unrecognized
            # argument"). The resolved root is already named above; this
            # names the remedy's actual shape too, empirically verified.
            raise ScanRefused(
                f"no files were enumerated under {root} - refusing to publish a "
                "vacuous zero-unit run; check the global --root (it must precede the "
                "comprehension subcommand, e.g. `agenttalk --root <path> comprehension "
                "scan`) and the exclusion policy")

        staging_handle = staging.create_staging_dir(scan_id=scan_id, lock_handle=lock_handle)
        generated_at = _utc_now_iso(now)
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
        # B3 (cold-read, PR-B fix round 3); closed as a class, M-2 (third
        # cold read, fix round 5): a file the adapter never produced a
        # positive result for must never be reported as
        # source_understood=satisfied just because its extension maps to
        # a known language - readiness needs to know WHICH paths degraded
        # and WHY, not just which ones the worker reported a
        # ``parse_failed`` for specifically (round 3 threaded only that
        # one reason; a resource-cap skip or a re-confinement rejection
        # went unthreaded the same way, twice more). Every worker problem,
        # by whatever reason_code, is threaded here.
        #
        # N3 (fifth cold read, fix round 8): a plain dict comprehension
        # over ``worker_result.problems`` is LAST-WINS for a path with
        # more than one recorded problem - whichever happened to be
        # listed last silently overwrote every earlier reason for that
        # SAME path, with no ordering guarantee callers should rely on.
        # Reasons are collected per path, sorted and deduplicated, so
        # the result is both deterministic (never depends on
        # worker_result.problems's own list order) and lossless (every
        # distinct reason survives, not just one).
        #
        # MINOR 5 (sixth cold read, fix round 9): round 8's own fix
        # joined those reasons into ONE string with "+" and published it
        # as ``adapter_problem_reason`` - a value OUTSIDE the enumerated
        # reason-code vocabulary every reader of that field expects
        # (readiness's own ``f"adapter_{reason}"`` construction, and any
        # future consumer matching against the known set). The
        # vocabulary stays CLOSED: each path maps to its full SORTED
        # LIST of reasons here; modules_artifact picks the first as the
        # single enumerated ``adapter_problem_reason`` and publishes the
        # complete list separately, losing nothing.
        worker_problem_reasons_by_path: dict[str, list[str]] = {
            p.relative_path: [] for p in worker_result.problems
        }
        for p in worker_result.problems:
            reasons = worker_problem_reasons_by_path[p.relative_path]
            if p.reason_code not in reasons:
                reasons.append(p.reason_code)
        for reasons in worker_problem_reasons_by_path.values():
            reasons.sort()

        modules = modules_artifact.build_modules(
            discovery_result, java_results,
            worker_problem_reasons_by_path=worker_problem_reasons_by_path,
        )
        # M7 (cold-read, PR-B fix round 3): discovery already computed
        # each file's own content digest - dependencies_artifact.py and
        # features_artifact.py's producers carried source_digest=None
        # unconditionally, never wired to it.
        file_digests = {f.relative_path: f.content_digest for f in discovery_result.files}
        dependencies = dependencies_artifact.build_dependencies(
            java_results, file_digests=file_digests)
        entry_points, features = features_artifact.build_features(
            java_results, file_digests=file_digests)
        readiness_signals, readiness_summaries = readiness_artifact.build_readiness(
            modules, dependencies, features)

        # N1 (third cold read, fix round 5): find_case_fold_collisions
        # existed with its own passing unit tests and zero production
        # callers - the same dead-code shape round 3's M9 found for
        # parse_web_xml. Two paths that collide once case-folded (a real
        # risk once a run crosses to/from a case-insensitive filesystem)
        # is a named problem code the design itself expects; it was never
        # actually emitted anywhere in the pipeline.
        case_collisions = find_case_fold_collisions(relative_paths)

        problems = [
            _problem_record(p["reason_code"], p.get("path"), p["detail"])
            for p in discovery_result.problems
        ] + [
            _problem_record(p.reason_code, p.relative_path, p.detail)
            for p in worker_result.problems
        ] + [
            _problem_record(
                "case_collision", second, bounded_detail(f"case-folds identically to {first!r}"))
            for first, second in case_collisions
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

        modules_bytes = _write_json_document(staging_handle.path / "modules.json", modules_doc)
        dependencies_bytes = _write_json_document(
            staging_handle.path / "dependencies.json", dependencies_doc)
        features_bytes = _write_json_document(staging_handle.path / "features.json", features_doc)
        readiness_bytes = _write_json_document(
            staging_handle.path / "readiness.json", readiness_doc)
        problems_bytes = _write_json_document(staging_handle.path / "problems.json", problems_doc)

        record_counts = {
            "modules.json": len(modules_doc["units"]),
            "dependencies.json": len(dependencies_doc["edges"]),
            "features.json": len(features_doc["entry_points"]) + len(features_doc["features"]),
            # Note 2 (second cold read, fix round 4): this previously
            # counted signals only, not summaries - features.json already
            # sums both of ITS record kinds (entry_points + features), so
            # readiness.json's declared count understated its true record
            # count (42 vs 49 on the reviewer's fixture). The publish
            # ceiling was therefore enforced against an understated count -
            # the fail-OPEN direction ceilings.py itself explicitly refuses
            # for other cases (an unmeasured/negative count).
            "readiness.json": len(readiness_doc["signals"]) + len(readiness_doc["summaries"]),
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

        # M2 (cold-read, PR-B fix round 3): per-artifact byte/content
        # digests + the run-level content_digest - digests.py's own
        # sha256_bytes/canonical_content_digest/run_content_digest existed
        # with no production caller, and validate_run claimed "full-run
        # integrity" while only checking envelope/schema/scan_id
        # consistency. scan.json itself is the SUMMARY of these five
        # artifacts, not a sixth entry in its own digest - a self-
        # referential digest has no fixed point.
        artifact_summaries = [
            _artifact_summary(
                name="modules.json", artifact_type=MODULES_ARTIFACT_TYPE,
                schema_version=modules_artifact.MODULES_SCHEMA_VERSION,
                record_count=record_counts["modules.json"], doc=modules_doc,
                canonical_bytes=modules_bytes,
            ),
            _artifact_summary(
                name="dependencies.json", artifact_type=DEPENDENCIES_ARTIFACT_TYPE,
                schema_version=dependencies_artifact.DEPENDENCIES_SCHEMA_VERSION,
                record_count=record_counts["dependencies.json"], doc=dependencies_doc,
                canonical_bytes=dependencies_bytes,
            ),
            _artifact_summary(
                name="features.json", artifact_type=FEATURES_ARTIFACT_TYPE,
                schema_version=FEATURES_SCHEMA_VERSION,
                record_count=record_counts["features.json"], doc=features_doc,
                canonical_bytes=features_bytes,
            ),
            _artifact_summary(
                name="readiness.json", artifact_type=READINESS_ARTIFACT_TYPE,
                schema_version=READINESS_SCHEMA_VERSION,
                record_count=record_counts["readiness.json"], doc=readiness_doc,
                canonical_bytes=readiness_bytes,
            ),
            _artifact_summary(
                name="problems.json", artifact_type=PROBLEMS_ARTIFACT_TYPE,
                schema_version=PROBLEMS_SCHEMA_VERSION,
                record_count=record_counts["problems.json"], doc=problems_doc,
                canonical_bytes=problems_bytes,
            ),
        ]
        run_digest = digests.run_content_digest(artifact_summaries)
        completed_at = _utc_now_iso(now)
        boundary_rows, boundaries_omitted = _bounded_boundaries([
            {"path": b.relative_path, "kind": b.boundary_kind}
            for b in discovery_result.boundaries
        ])

        scan_doc = {
            **_envelope(
                artifact_type=SCAN_ARTIFACT_TYPE, schema_version=SCAN_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
            "generator_version": GENERATOR_VERSION,
            "adapters": [{
                "name": java_adapter.ADAPTER_NAME, "version": java_adapter.ADAPTER_VERSION,
                "rule_version": java_adapter.RULE_VERSION,
            }],
            "root_binding": privacy_result.root_binding,
            # M5 (cold-read, PR-B fix round 3): the privacy disposition
            # this run acted under lived only in scan.lock, deleted at
            # release - the audit trail the attended override
            # (--acknowledge-unignored-private-store --work-id) exists to
            # create did not survive the run at all. Recorded here so it
            # does.
            "privacy": {
                "vcs_privacy": privacy_result.vcs_privacy,
                "vcs_kind": privacy_result.vcs_kind,
                "matched_rule": privacy_result.matched_rule,
                "work_id": privacy_result.work_id,
            },
            "platform_identity": {
                "os_family": discovery_result.platform_identity.os_family,
                "architecture": discovery_result.platform_identity.architecture,
                "path_normalization_version": discovery_result.platform_identity.path_normalization_version,
                "case_sensitive": discovery_result.platform_identity.case_sensitive,
                "unicode_normalizing": discovery_result.platform_identity.unicode_normalizing,
            },
            "whole_scope_fingerprint": discovery_result.whole_scope_fingerprint,
            "fingerprint_complete": discovery_result.fingerprint_complete,
            # N2 (fourth cold read, fix round 6): the design names "the
            # effective include/exclude rules... and their configuration
            # digest" as a scan.json field. config.json parsing itself is
            # out of scope this slice (no caller-configurable rules exist
            # yet - a separate, named decision), but the CURRENT hardcoded
            # default-exclude rule sets already exist and already shape
            # whole_scope_fingerprint - without this, a future change to
            # them silently changes what the fingerprint means, with no
            # recorded rule identity to explain why.
            "exclude_rule_digest": discovery.effective_exclude_rule_digest(),
            "exclusions": dict(sorted(discovery_result.exclusions.items())),
            # M4 (fourth cold read, fix round 6): a bare integer count hid
            # WHAT was actually skipped - the design names "excluded roots
            # with an explicit boundary reason" as a scan.json field, not
            # a count. discovery.py already computes each boundary's own
            # root-relative path and kind (BoundaryEntry); this was
            # discarded down to len(...) rather than published. The
            # projection-exposure half (report/status surfacing this) is
            # a separate, larger question - named as a carry, not fixed
            # here (see the PR description, R-15a).
            #
            # Minor 7 (fifth cold read, fix round 7): this list was
            # published fully UNBOUNDED - every other list-shaped section
            # has been progressively capped across three prior rounds
            # (M10 round 3, M-4 round 4, M2 round 6); this one, added the
            # same round as M2, broke that same discipline one list, one
            # round later. Bounded the same way (cap + omitted count),
            # just via a local helper - scan.json is written here, not by
            # projector.py, which only bounds the separate REPORT payload.
            "boundaries": boundary_rows,
            "boundaries_omitted_count": boundaries_omitted,
            "unsupported_relations": list(java_adapter.UNSUPPORTED_RELATIONS),
            "record_counts": record_counts,
            "problem_count": len(problems),
            "artifacts": artifact_summaries,
            "content_digest": run_digest,
        }
        scan_bytes = _write_json_document(staging_handle.path / "scan.json", scan_doc)

        # MAJOR 3 (fifth cold read, fix round 7): scan.json is the ROOT of
        # the integrity chain - every other artifact is verified against a
        # digest scan.json itself declares (_verify_artifact_digests), but
        # nothing anchors scan.json's own digest anywhere. index.json's
        # existing compare-and-set write path is the reviewer's proposed
        # anchor point: recording scan.json's byte_sha256 and canonical
        # content_digest here, in the SAME run_summary dict that already
        # flows unchanged into each index run entry (publish.py's
        # _build_successor_index), gives status/validate something
        # external to scan.json to verify it against.
        run_summary = {
            "scan_id": scan_id,
            "status": status,
            "scan_json_byte_sha256": digests.sha256_bytes(scan_bytes),
            "scan_json_content_digest": digests.canonical_content_digest(scan_doc),
        }
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


def _scan_field(scan_doc: dict[str, Any], key: str, scan_id: str) -> Any:
    """MAJOR 2 (fifth cold read, fix round 7): mirrors _load_run_records's
    own ``_records`` typed-malformed contract (M1, round 6) for scan.json's
    top-level scalar fields - a missing key raises the same typed
    ComprehensionError every other malformed-artifact path already raises,
    never a bare KeyError. round 6's N1 fix moved status off the record-
    conversion loop entirely (the one place M1's guard lived), leaving
    scan.json - the ONE artifact status still reads - with no guard of its
    own on the exact same malformed-body shape.

    MAJOR 2 (fifth cold read, fix round 8): now a thin wrapper around
    envelope.require_field, the ONE typed-access helper for every field
    read off any loaded published document - closing this class for
    good rather than growing a fourth/fifth hand-rolled per-document
    guard."""
    return require_field(scan_doc, key, doc_name=f"{scan_id}'s scan.json")


def _index_field(index_doc: dict[str, Any], key: str) -> Any:
    """MAJOR 2 (fifth cold read, fix round 8): index.json's own body
    fields (``latest_scan_id``, ``runs``) were read with raw, unguarded
    subscripts in get_status/get_report/validate_run - a malformed-but-
    envelope-valid index.json (a real, on-disk scenario every OTHER
    loaded document in this package already guards against) raised an
    untyped KeyError straight through the real CLI. Mirrors _scan_field
    for the other loaded document every read command starts from."""
    return require_field(index_doc, key, doc_name="index.json")


#: Round 7b (reviewer-3 delta on 84ef111): the index run-summary
#: retention cap (``_INDEX_RUNS_MAX`` in publish.py) can age an older
#: run's anchor entry out of ``index.json`` entirely - after which
#: status/report/validate all raised the SAME hard refusal a genuine
#: tamper does, permanently, for an otherwise-untouched, immutable
#: on-disk run. A missing anchor is bookkeeping retention, not evidence
#: of tampering, and must never brick a real run forever - it degrades
#: to this explicit, labeled "unverified" outcome instead. A run summary
#: that IS present but PREDATES this anchor (a legacy index entry with
#: neither key at all, from a run published before round 7) gets the
#: exact same treatment - genuinely no anchor to check against, not a
#: mismatch. Only a run summary present WITH both keys, whose values
#: disagree with what is actually on disk, is real, distinguishable
#: tamper evidence and still refuses hard.
_SCAN_JSON_ANCHOR_NOT_RECORDED = "scan_json_index_anchor_not_recorded"


def _scan_json_anchor_state(
    index_doc: dict[str, Any], scan_id: str, scan_doc: dict[str, Any], run_dir: Path,
) -> dict[str, Any]:
    """MAJOR 3 (fifth cold read, fix round 7), degraded per round 7b:
    scan.json is the ROOT of the integrity chain - every OTHER artifact
    is verified against a digest scan.json itself declares
    (_verify_artifact_digests), but nothing external to scan.json ever
    recorded what ITS OWN digest should be, so status/report/validate
    all implicitly trusted scan.json's on-disk bytes/content as ground
    truth with no verification step: a bytes-only tamper AND a semantic
    tamper (falsifying completeness/the fingerprint) both passed status
    healthy, report clean, and validate VALID:TRUE - the strongest
    positive claim the plane makes, false on a modified run. run_scan
    records scan.json's byte_sha256 and canonical content_digest in
    index.json's run summary at publish time; this verifies the CURRENT
    on-disk scan.json against those recorded values when they exist,
    and reports an explicit ``{"state": "unverified", ...}`` outcome
    (never a refusal) when they do not - see _SCAN_JSON_ANCHOR_NOT_
    RECORDED's own comment for why a missing anchor must degrade rather
    than brick the run."""
    anchor = None
    for run_summary in index_doc.get("runs") or []:
        if run_summary.get("scan_id") == scan_id:
            anchor = run_summary
            break
    recorded_byte_sha256 = anchor.get("scan_json_byte_sha256") if anchor else None
    recorded_content_digest = anchor.get("scan_json_content_digest") if anchor else None
    if recorded_byte_sha256 is None or recorded_content_digest is None:
        return {"state": "unverified", "reason_code": _SCAN_JSON_ANCHOR_NOT_RECORDED}
    try:
        actual_byte_sha256 = digests.sha256_file(run_dir / "scan.json")
    except OSError as exc:
        raise ComprehensionError(f"scan.json's bytes could not be read for verification: {exc}") from exc
    if actual_byte_sha256 != recorded_byte_sha256:
        raise ComprehensionError(
            "scan.json's byte_sha256 does not match its anchor recorded in index.json")
    if digests.canonical_content_digest(scan_doc) != recorded_content_digest:
        raise ComprehensionError(
            "scan.json's content_digest does not match its anchor recorded in index.json")
    return {"state": "verified"}


#: Minor 2 (round 7b): the same required-body-field contract _scan_field
#: enforces for status/report - validate never checked scan.json's own
#: scalar fields at all (only the separate "artifacts" digest-summary
#: list), so it reported valid:true for a scan.json missing a required
#: field where status/report both exit 2 typed on the identical input.
_SCAN_JSON_REQUIRED_BODY_FIELDS = (
    "scan_id", "status", "generated_at", "adapters", "problem_count",
    "record_counts", "root_binding", "whole_scope_fingerprint", "fingerprint_complete",
)


def _require_scan_json_body_fields(scan_doc: dict[str, Any], scan_id: str) -> None:
    for key in _SCAN_JSON_REQUIRED_BODY_FIELDS:
        _scan_field(scan_doc, key, scan_id)


def get_status(root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    """design: "Show the latest run, completeness, source revision/
    fingerprint, freshness, adapter coverage, and problem counts."

    N1 (fourth cold read, fix round 6): round 5's M-1 fix made this call
    the SAME full per-artifact digest verification report/validate
    perform - but the design states an explicit, narrower read-cost tier
    for status specifically: "status verifies the index and scan.json.
    report, pack construction, and /api/comprehension then verify the
    exact-byte digest and schema of each artifact they actually load;
    they do not rescan unrelated artifacts on every response"
    (DESIGN-55-comprehension-plane.md, "Validation tiers and size
    ceilings"). Round 5's fix was a genuine, deliberate, FAIL-CLOSED
    overshoot of that tier, not a security regression fixed here - a
    named, accepted bounded-read-cost trade-off restored to what the
    design actually specifies: status does not verify (and so cannot
    catch tamper in) modules/dependencies/features/readiness/problems;
    report and validate still do, in full, every time."""
    comprehension_dir = paths.comprehension_dir(Path(root).resolve() / ".agenttalk")
    index_doc, index_digest = publish.read_current_index(comprehension_dir)
    if index_doc is None:
        raise NotScanned(f"no comprehension run has ever been published under {root}")
    scan_id = run_id or _index_field(index_doc, "latest_scan_id")
    run_dir = _resolved_run_dir(comprehension_dir, scan_id)
    scan_doc = _load_single_artifact(
        run_dir, scan_id, "scan.json", SCAN_ARTIFACT_TYPE, SCAN_SCHEMA_VERSION)
    anchor_state = _scan_json_anchor_state(index_doc, scan_id, scan_doc, run_dir)
    return {
        "latest_scan_id": _index_field(index_doc, "latest_scan_id"),
        "index_digest": index_digest,
        "run_summaries": _index_field(index_doc, "runs"),
        "scan_id": _scan_field(scan_doc, "scan_id", scan_id),
        "status": _scan_field(scan_doc, "status", scan_id),
        "generated_at": _scan_field(scan_doc, "generated_at", scan_id),
        "adapters": _scan_field(scan_doc, "adapters", scan_id),
        "problem_count": _scan_field(scan_doc, "problem_count", scan_id),
        "record_counts": _scan_field(scan_doc, "record_counts", scan_id),
        "root_binding": _scan_field(scan_doc, "root_binding", scan_id),
        # Note 1 (second cold read, fix round 4): this function's own
        # docstring cites the design's "source revision/fingerprint" as
        # part of what status shows - it was never actually returned.
        "whole_scope_fingerprint": _scan_field(scan_doc, "whole_scope_fingerprint", scan_id),
        "fingerprint_complete": _scan_field(scan_doc, "fingerprint_complete", scan_id),
        "scan_json_integrity": anchor_state,
        "freshness": {
            "state": "not_evaluated", "reason_code": "freshness_not_implemented_this_slice",
        },
    }


def _load_single_artifact(
    run_dir: Path, scan_id: str, name: str, artifact_type: str, schema_version: int,
) -> dict[str, Any]:
    """Read and envelope/schema-validate ONE artifact document - shared
    by :func:`get_status` (which, per the design's own read-cost tier,
    loads scan.json only) and :func:`_load_run_records` (which loads
    every artifact, for callers that verify what they actually load)."""
    try:
        doc = read_json_document(run_dir / name)
    except EnvelopeError as exc:
        raise ComprehensionError(f"{scan_id}'s {name} could not be read: {exc}") from exc
    return validate_envelope(doc, artifact_type=artifact_type, schema_version=schema_version)


def _load_run_records(comprehension_dir: Path, scan_id: str) -> dict[str, Any]:
    run_dir = _resolved_run_dir(comprehension_dir, scan_id)

    def _load(name: str, artifact_type: str, schema_version: int) -> dict[str, Any]:
        return _load_single_artifact(run_dir, scan_id, name, artifact_type, schema_version)

    # M1 (fourth cold read, fix round 6): validate_envelope only checks the
    # COMMON envelope fields (schema_version/artifact_type/scan_id/
    # generated_at) - an artifact that passes it can still be missing a
    # section key (e.g. modules.json with no "units") or have one record
    # inside a section missing a required field. Every record-conversion
    # call site used to index straight into the raw doc/record with no
    # guard, raising an untyped KeyError through every read command
    # (status/report/validate) via the real CLI - BEFORE the digest check
    # that would have caught genuine tamper ever ran, since record loading
    # happens first. Round 5's N2 closed this exact class one layer up,
    # for index.json; this closes the artifact layer, at the one place
    # every record conversion already funnels through - not per call site.
    def _records(doc: dict[str, Any], key: str, name: str, converter) -> list:
        try:
            items = doc[key]
            return [converter(item) for item in items]
        except (KeyError, TypeError) as exc:
            raise ComprehensionError(
                f"{scan_id}'s {name} contains a malformed record: {exc}") from exc

    scan_doc = _load("scan.json", SCAN_ARTIFACT_TYPE, SCAN_SCHEMA_VERSION)
    modules_doc = _load("modules.json", MODULES_ARTIFACT_TYPE, modules_artifact.MODULES_SCHEMA_VERSION)
    dependencies_doc = _load(
        "dependencies.json", DEPENDENCIES_ARTIFACT_TYPE, dependencies_artifact.DEPENDENCIES_SCHEMA_VERSION)
    features_doc = _load("features.json", FEATURES_ARTIFACT_TYPE, FEATURES_SCHEMA_VERSION)
    readiness_doc = _load("readiness.json", READINESS_ARTIFACT_TYPE, READINESS_SCHEMA_VERSION)
    problems_doc = _load("problems.json", PROBLEMS_ARTIFACT_TYPE, PROBLEMS_SCHEMA_VERSION)

    return {
        "scan": scan_doc,
        "run_dir": run_dir,
        "raw_docs": {
            "modules.json": modules_doc,
            "dependencies.json": dependencies_doc,
            "features.json": features_doc,
            "readiness.json": readiness_doc,
            "problems.json": problems_doc,
        },
        "modules": _records(
            modules_doc, "units", "modules.json", modules_artifact.module_record_from_json),
        "dependencies": _records(
            dependencies_doc, "edges", "dependencies.json",
            dependencies_artifact.dependency_record_from_json),
        "entry_points": _records(
            features_doc, "entry_points", "features.json",
            features_artifact.entry_point_record_from_json),
        "features": _records(
            features_doc, "features", "features.json", features_artifact.feature_record_from_json),
        "readiness_signals": _records(
            readiness_doc, "signals", "readiness.json", readiness_artifact.readiness_signal_from_json),
        "readiness_summaries": _records(
            readiness_doc, "summaries", "readiness.json",
            readiness_artifact.unit_readiness_summary_from_json),
        "problems": _records(problems_doc, "problems", "problems.json", lambda item: item),
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
    parity, C-2).

    M-1 (third cold read, fix round 5): this projected every loaded
    artifact as truth with no digest check at all - only ``validate``
    ever verified a run's declared per-artifact digests, so a tampered
    modules.json (or any of the other four) silently flowed straight
    into a "report" a caller has no reason to distrust. The design's
    read-path rule is that report verifies the exact-byte digest and
    schema of what it actually loads before projecting it; a mismatch
    now raises the same typed :class:`ComprehensionError` ``validate``
    raises, rather than projecting tampered content as fact."""
    comprehension_dir = paths.comprehension_dir(Path(root).resolve() / ".agenttalk")
    index_doc, _digest = publish.read_current_index(comprehension_dir)
    if index_doc is None:
        raise NotScanned(f"no comprehension run has ever been published under {root}")
    scan_id = run_id or _index_field(index_doc, "latest_scan_id")
    records = _load_run_records(comprehension_dir, scan_id)
    anchor_state = _scan_json_anchor_state(index_doc, scan_id, records["scan"], records["run_dir"])
    _verify_artifact_digests(records["scan"], records["raw_docs"], records["run_dir"])
    payload = projector.project_comprehension(
        scan_id=_scan_field(records["scan"], "scan_id", scan_id),
        generated_at=_scan_field(records["scan"], "generated_at", scan_id),
        manifest_digest=None, status=_scan_field(records["scan"], "status", scan_id),
        modules=records["modules"], dependencies=records["dependencies"],
        entry_points=records["entry_points"], features=records["features"],
        readiness_signals=records["readiness_signals"],
        readiness_summaries=records["readiness_summaries"],
        problems=records["problems"],
        unit_id=unit_id, feature_id=feature_id, readiness_state=readiness_state,
        dependencies_only=dependencies_only,
    )
    # Round 7b: layered onto the projection here, not threaded through
    # projector.project_comprehension's own signature - the anchor is a
    # scan_pipeline (index-lookup) concern, not something the pure
    # projector function has any business knowing about.
    payload["scan_json_integrity"] = anchor_state
    return payload


def _verify_artifact_digests(
    scan_doc: dict[str, Any], raw_docs: dict[str, dict[str, Any]], run_dir: Path,
) -> None:
    """M2 (cold-read, PR-B fix round 3): validate_run claimed "full-run
    integrity" while only checking envelope/schema/scan_id consistency -
    never the per-artifact/run-level digests the design actually requires
    (invariant 7). Recomputes each artifact's canonical content digest
    from the document ACTUALLY ON DISK and compares against what
    scan.json declared, then recomputes the run-level content_digest from
    those same declared summaries and compares against scan.json's own
    declared value. Raises ComprehensionError on any mismatch - the
    design's own "two byte-identical scans must produce the same
    canonical content digest" acceptance property now has something to
    check against.

    M-3 (second cold read, fix round 4): the byte SHA-256 check must read
    the file's ACTUAL BYTES ON DISK (``digests.sha256_file``) - the first
    version recomputed it from ``canonical_json_bytes(doc)``, i.e. the
    PARSED-then-RE-SERIALIZED document, which normalizes away any
    byte-level difference (whitespace, formatting) that doesn't change
    the parsed content. That silently defeated the entire point of a
    byte-level digest: a whitespace-only rewrite of a published artifact
    passed validation because the re-canonicalized bytes matched, even
    though the file on disk no longer did. The content-digest check above
    is unaffected by this fix - it is deliberately insensitive to exactly
    that class of byte-level-only difference."""
    declared_artifacts = scan_doc.get("artifacts")
    if not declared_artifacts:
        raise ComprehensionError("scan.json is missing its artifacts digest summary")
    for entry in declared_artifacts:
        # MAJOR 2 (fifth cold read, fix round 8): every field this loop -
        # and digests.run_content_digest below, over these SAME entries -
        # reads off a loaded scan.json artifacts entry now goes through
        # require_field, never a raw subscript. digests.py stays
        # dependency-free (it never imports envelope.py); validating
        # every field it will need UP FRONT here means a missing one
        # raises with a clear, typed message before ever reaching that
        # generic helper's own bare subscript.
        name = require_field(entry, "name", doc_name="scan.json's artifacts entry")
        entry_label = f"scan.json's {name} artifacts entry"
        doc = raw_docs.get(name)
        if doc is None:
            raise ComprehensionError(f"scan.json names an artifact {name!r} that was never loaded")
        if digests.canonical_content_digest(doc) != require_field(
            entry, "content_digest", doc_name=entry_label,
        ):
            raise ComprehensionError(
                f"{name}'s content_digest does not match its declared value in scan.json")
        try:
            actual_byte_sha256 = digests.sha256_file(run_dir / name)
        except OSError as exc:
            raise ComprehensionError(f"{name}'s bytes could not be read for verification: {exc}") from exc
        if actual_byte_sha256 != require_field(entry, "byte_sha256", doc_name=entry_label):
            raise ComprehensionError(
                f"{name}'s byte_sha256 does not match its declared value in scan.json")
        require_field(entry, "artifact_type", doc_name=entry_label)
        require_field(entry, "schema_version", doc_name=entry_label)
        require_field(entry, "record_count", doc_name=entry_label)
    if digests.run_content_digest(declared_artifacts) != scan_doc.get("content_digest"):
        raise ComprehensionError(
            "scan.json's run-level content_digest does not match its declared artifacts")


def validate_run(root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    """design: "Perform full-run integrity validation." Reads and verifies
    every artifact's envelope/schema AND the per-artifact/run-level
    digests (M2, cold-read PR-B fix round 3); the design's separate
    EXTERNAL-evidence-pointer revalidation step has nothing to revalidate
    yet in this slice (see module docstring) - ``external_revalidation``
    is reported as an explicit, named gap rather than silently omitted."""
    comprehension_dir = paths.comprehension_dir(Path(root).resolve() / ".agenttalk")
    index_doc, _digest = publish.read_current_index(comprehension_dir)
    if index_doc is None:
        raise NotScanned(f"no comprehension run has ever been published under {root}")
    scan_id = run_id or _index_field(index_doc, "latest_scan_id")
    # Round 7b: a default the anchor state stays at if an EARLIER step in
    # the try block below (record loading/conversion, the required-field
    # check) fails first - this run's scan.json integrity genuinely was
    # never evaluated in that case. Filed under the SAME "unverified"
    # state an aged-out anchor uses (round 7c: this comment previously
    # described it as a distinct third state; the code's choice - one
    # state, a distinguishing reason_code - is the better one, so the
    # comment is corrected to match the code, not the other way around).
    anchor_state: dict[str, Any] = {
        "state": "unverified", "reason_code": "not_evaluated_before_an_earlier_failure",
    }
    try:
        records = _load_run_records(comprehension_dir, scan_id)
        # Minor 2 (round 7b): validate never checked scan.json's OWN
        # scalar fields at all (only the separate "artifacts" digest
        # list _verify_artifact_digests checks) - a scan.json missing a
        # required field reported valid:true here where status/report
        # both exit 2 typed on the identical input.
        _require_scan_json_body_fields(records["scan"], scan_id)
        anchor_state = _scan_json_anchor_state(index_doc, scan_id, records["scan"], records["run_dir"])
        _verify_artifact_digests(records["scan"], records["raw_docs"], records["run_dir"])
        valid = True
        detail = (
            "all artifacts verified: schema, envelope identity, scan_id consistency, and "
            "per-artifact/run-level content digests"
        )
        # Round 7c (reviewer-3 delta on 95d9cd8): valid:true's own detail
        # sentence claimed "all artifacts verified" even when scan.json's
        # OWN anchor is unverified (aged out or never recorded) - the
        # state existed only in the JSON payload's separate
        # "scan_json_integrity" field, invisible anywhere a human
        # actually reads. valid stays true (the boolean is right - an
        # unverified anchor is not evidence of a bad run) but the
        # sentence itself must now say so, not overclaim.
        if anchor_state.get("state") != "verified":
            detail += (
                f"; scan.json's own integrity is UNVERIFIED "
                f"({anchor_state.get('reason_code')}) - not checked against a recorded anchor"
            )
    except ComprehensionError as exc:
        valid = False
        detail = str(exc)
        records = None
    unit_ids = {m.unit_id for m in records["modules"]} if records else set()
    dangling_edges = [
        e.edge_id for e in (records["dependencies"] if records else [])
        if e.from_unit_id not in unit_ids
    ] if records else []
    # ROUND 9b (sixth cold read, honesty tightening): dangling EDGES were
    # already flagged (above) - dangling ENTRY POINTS (an owning_unit_id
    # naming no real unit, exactly the same "unattributable synthesized
    # owner" shape round 9's own BLOCKER fixed at the adapter level) were
    # not, so a THIS class of wrong-data could still slip past validate
    # undetected on the entry-point side even though the edge side would
    # have caught its own instance.
    dangling_entry_points = [
        e.entry_point_id for e in (records["entry_points"] if records else [])
        if e.owning_unit_id not in unit_ids
    ] if records else []
    invalid = dangling_edges or dangling_entry_points
    if dangling_edges and dangling_entry_points:
        dangling_detail = (
            f"{len(dangling_edges)} edge(s) reference an unknown from_unit_id and "
            f"{len(dangling_entry_points)} entry point(s) reference an unknown owning_unit_id"
        )
    elif dangling_edges:
        dangling_detail = f"{len(dangling_edges)} edge(s) reference an unknown from_unit_id"
    elif dangling_entry_points:
        dangling_detail = (
            f"{len(dangling_entry_points)} entry point(s) reference an unknown owning_unit_id"
        )
    else:
        dangling_detail = None
    return {
        "scan_id": scan_id,
        "valid": valid and not invalid,
        "detail": detail if not invalid else dangling_detail,
        "scan_json_integrity": anchor_state,
        "external_revalidation": {
            "performed": False,
            "reason_code": "no_external_evidence_pointers_this_slice",
        },
    }
