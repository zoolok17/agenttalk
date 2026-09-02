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

import posixpath
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
    validate_relative_path,
    validate_scan_id,
)
from .errors import ComprehensionError, EnvelopeError, bounded_detail, bounded_os_error_detail
from .privacy import PrivacyPreflightResult

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
    # FIX ROUND 21 (seventeenth cold read, CR17-4 MAJOR): same bucket as
    # every other genuine whole-file evidence gap above.
    "encoding_undecodable": "warning",
    "path_excluded": "warning",
    "resource_limit": "warning",
    "non_utf8_path": "warning",
    "case_collision": "warning",
    # BLOCKER 1b (fifth cold read, fix round 8): a .java file whose parse
    # succeeded but extracted zero declared types - closing the zero-
    # extraction evidence hole as a class (worker.py).
    "no_types_extracted": "warning",
    # FIX ROUND 24 (twentieth cold read, F1b): a pom.xml's own analogue,
    # same bucket.
    "no_pom_facts_extracted": "warning",
    # FIX ROUND 24 (twentieth cold read, F4 MINOR): same bucket.
    "dependency_value_unrecoverable": "warning",
    # FIX ROUND 35 (twenty-ninth cold read, F1 BLOCKER): same bucket - the
    # pom's own coordinate twin of the dependency-site reason above.
    "coordinate_value_unrecoverable": "warning",
    # FIX ROUND 24 (micro-round 24b, item 1): same bucket.
    "no_web_xml_facts_extracted": "warning",
    # BLOCKER fail-safe (sixth cold read, fix round 10): a route
    # annotation the java adapter could not confidently associate with
    # a class or a method - under-claimed rather than published wrong.
    "route_annotation_unassociated": "warning",
    # BLOCKER fail-safe part 2 (seventh cold read, fix round 11): a route
    # annotation's own value (or its enclosing class's route prefix)
    # could not be recovered as a literal - under-claimed rather than
    # composed against a guessed/implicit-empty value.
    "route_value_unrecoverable": "warning",
    # FIX ROUND 13b (reviewer-3's B1 class-closer on round 13): a method
    # literally named main that the adapter's strict cli_main detector
    # could not confidently classify - under-claimed (readiness reports
    # entry_points_mapped unknown, never a confident no-entry-point)
    # rather than a silent, possibly-wrong negative.
    "cli_main_unrecognized": "warning",
    # FIX ROUND 14 (tenth cold read, CR10-5 JUDGE, completeness): a file
    # in a recognized-but-unsupported source language (worker.py) -
    # under-claimed evidence, the same "warning" bucket every other
    # missing-evidence reason in this table already gets.
    "unsupported_language": "warning",
    # FIX ROUND 16 (twelfth cold read, B1 BLOCKER): two units DECLARING
    # the identical fully-qualified name - a real collision modules_
    # artifact.py's _populate_duplicate_qualified_name_conflicts already
    # detects and tags (conflict_id), but never surfaced anywhere an
    # operator actually reads until now.
    "duplicate_qualified_name": "warning",
    # FIX ROUND 29 (twenty-fifth cold read, F1 BLOCKER): a web.xml
    # declaring the SAME servlet-name/filter-name twice with different
    # class values - a real collision, a DIFFERENT root cause from
    # duplicate_qualified_name above (this one is entirely local to one
    # descriptor file, never a cross-file FQN collision) but the same
    # "recorded, degrading" bucket.
    "duplicate_descriptor_name": "warning",
    # FIX ROUND 29 (twenty-fifth cold read, F9c JUDGE): a web.xml
    # <servlet-mapping>/<filter-mapping> naming a servlet-name/
    # filter-name that NO <servlet>/<filter> element declares at all -
    # a different descriptor-inconsistency root cause from
    # duplicate_descriptor_name (genuinely absent, never a collision
    # between two declarations) but the same "recorded, degrading"
    # bucket.
    "undeclared_descriptor_name": "warning",
    # FIX ROUND 30 (twenty-sixth cold read, F1 BLOCKER): a web.xml
    # <servlet>/<filter> declaring a name but backed by neither a usable
    # class nor (servlet-only) a <jsp-file> - a THIRD descriptor-
    # inconsistency root cause (genuinely declared, but nothing this
    # producer can attribute a route to), same "recorded, degrading"
    # bucket as its two siblings above.
    "descriptor_name_without_class": "warning",
    # FIX ROUND 31 (twenty-seventh cold read, N4 JUDGE, taken): two
    # DIFFERENT servlet-names mapped to the IDENTICAL <url-pattern> - a
    # container-rejected descriptor (undefined dispatch), the mirror
    # shape of the three descriptor-name reasons above (one PATTERN,
    # two names, rather than one NAME, two backings) - same "recorded,
    # degrading" bucket.
    #
    # MICRO-ROUND 31b (reviewer-3 delta, R4, declared): this reason is
    # FILE-SCOPED - java.py's own detector only ever compares mappings
    # within ONE web.xml, never against an annotation-declared route or
    # a DIFFERENT web.xml's own mappings, so its absence means "no
    # collision within this one descriptor," never "no route collision
    # exists anywhere in this run" (a real, declared under-reporting
    # scope, not wrong data - the cross-source check stays out of this
    # slice).
    "duplicate_route_target": "warning",
    # FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, part (b) - THE
    # CLASS-CLOSER): a recognized-but-unsupported route-like annotation
    # (JAX-WS's own @WebMethod) - under-claimed evidence, the same
    # "warning" bucket every other missing-evidence reason already gets.
    "unsupported_entry_point_shape": "warning",
    # FIX ROUND 18 (fourteenth cold read, F6 JUDGE, taken): a binary-
    # sniffed-and-excluded file whose own extension is code-bearing -
    # the same "warning" bucket every other missing-evidence reason
    # already gets.
    "binary_excluded_code_bearing_file": "warning",
    # FIX ROUND 19 (fifteenth cold read, F4 MAJOR, wrong-data): a
    # generated/vendor-named directory nested under an uncarved bare
    # src/ root that swallows real code (discovery.py) - the same
    # "warning" bucket every other missing-evidence reason already gets.
    "excluded_region_contains_code": "warning",
    # FIX ROUND 19b (reviewer-3's rejection of round 19, THE MAJOR,
    # wrong-data): the excluded-directory peek's own entry cap exceeded
    # before a code-bearing file could be confirmed present or absent -
    # honestly unknown, never silently folded into the same confident
    # "no code" outcome a fully-explored directory gets. Same "warning"
    # bucket every other missing-evidence reason already gets.
    "excluded_region_peek_truncated": "warning",
    # FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR - THE REACTOR
    # RULE): a pom's own declared <module> resolving into an excluded
    # region - positive, direct evidence the region holds first-party
    # source. Same "warning" bucket every other missing-evidence reason
    # already gets.
    "module_directory_excluded": "warning",
    # FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-rule
    # VISIBILITY): one per triggering root/pom, naming why externality
    # was suppressed run-wide. Same bucket as every other named-gap
    # reason code above.
    "externality_suppressed": "warning",
    # FIX ROUND 21 (seventeenth cold read, CR17-1 BLOCKER): a forced
    # --recover-stale-lock recovery - safety-relevant provenance, same
    # bucket as every other named-gap reason code above.
    "scan_lock_forcibly_recovered": "warning",
    # FIX ROUND 26b (reviewer-3 delta on `38a21f3`, item 2, R4 carry
    # OVERTURNED - closed): a binary-excluded, non-adapter-handled .xml
    # file this run could not root-sniff to determine its tier - visible
    # (same "warning" bucket) but DELIBERATELY never in the run's own
    # degrading-problem set below (see `status`'s own comment) - this
    # run has no evidence either way, and degrading would brand every
    # repo carrying an unreadable logback.xml, the round-16 dilution
    # this producer's own tier calibration already refuses to reopen.
    "binary_excluded_root_sniffed_xml": "warning",
}
_DEFAULT_PROBLEM_SEVERITY = "warning"


def _problem_record(
    reason_code: str, path: str | None, detail: str, *, qualified_name: str | None = None,
    conflict_id: str | None = None,
) -> dict[str, Any]:
    """FIX ROUND 13d (reviewer-3's LOW on round 13c): ``qualified_name``
    was internal-only - round 13c attributed a ``cli_main_unrecognized``
    problem to its own enclosing declared type (never broadcasting it to
    every sibling), but ``problems.json`` itself dropped that attribution
    on the floor, so readiness's own signal named the unit while the ONE
    surface an operator actually reads (problems.json) could only say
    "somewhere in this file" - no way to join the two. Published WHEN
    PRESENT, the key omitted entirely otherwise (never a null) - the
    same absent-not-null idiom every other optional field in this
    artifact family already follows.

    FIX ROUND 24 (twentieth cold read, F8a, design-promised, taken): the
    design's own item 4 ("`problems.json` records that `conflict_id`,
    every claimant, and the disputed fields") named a field this
    function never had a parameter for at all - a ``duplicate_qualified_
    name`` problem published no ``conflict_id``, so a consumer could not
    join the problem back to the two (or more) ``modules.json`` units
    that DO share one. Same absent-not-null idiom as ``qualified_name``."""
    record = {
        "problem_id": digests.problem_id(reason_code=reason_code, path=path, detail=detail),
        "reason_code": reason_code,
        "severity": _PROBLEM_SEVERITY_BY_REASON_CODE.get(reason_code, _DEFAULT_PROBLEM_SEVERITY),
        "path": path,
        "detail": detail,
    }
    if qualified_name is not None:
        record["qualified_name"] = qualified_name
    if conflict_id is not None:
        record["conflict_id"] = conflict_id
    return record


#: FIX ROUND 28 (twenty-fourth cold read, F8, declare-not-silently-leave-
#: to-a-docstring): round 26's own F7 note declared record_count's
#: definition ("the TOTAL number of individual records across every
#: top-level collection... not the length of any one named array") only
#: in a docstring a consumer of the PUBLISHED artifact never sees - the
#: same gap PROVENANCE_CAVEAT/CLASSIFICATION_CAVEAT/FEATURES_STRUCTURAL_
#: CAVEAT already close for their own promises. Published in scan.json
#: itself now, not left implicit in source a consumer has no path to.
RECORD_COUNT_DEFINITION = (
    "each artifacts[] entry's own record_count is the TOTAL number of "
    "individual records across every top-level collection that artifact "
    "publishes, not the length of any one named array - readiness.json's "
    "record_count sums signals+summaries, features.json's sums entry_points"
    "+features; modules.json/dependencies.json/problems.json each publish "
    "exactly one collection, so their own record_count equals that single "
    "array's length."
)


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
    schema_version/record_count/content_digest from each entry).

    FIX ROUND 26 (twenty-second cold read, F7 note, doc-only): ``record_
    count`` is the TOTAL number of individual records across every
    top-level collection this document publishes, not the length of any
    one named array - a document with several distinct record kinds
    (readiness.json's own ``signals``/``summaries``; features.json's own
    ``entry_points``/``features``) sums across all of them (see this
    call's own caller for each document's exact sum, and Note 2's own
    fix-round-4 history for why an unsummed count previously understated
    it).

    FIX ROUND 28 (F8): that definition is now ALSO published in-artifact,
    see ``RECORD_COUNT_DEFINITION`` above - this docstring is unchanged
    (still the right place for an implementer reading this function),
    it is simply no longer the ONLY place a reader could find it."""
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
    # FIX ROUND 28 (twenty-fourth cold read, F9, wrong-refusal-timing):
    # this pairing refusal used to live INSIDE the except branch below -
    # only reached when the preflight actually found something to
    # acknowledge. A caller who passed --acknowledge-unignored-private-
    # store with no --work-id against a repo that happened to have no
    # unignored private store at all silently proceeded, no different
    # from never having passed the flag - the caller's own invalid
    # invocation was masked by a preflight outcome it had no way to
    # predict up front. The pairing is a property of the ARGUMENTS
    # themselves (design: "applies to one run bound to an existing work
    # item"), never of what the preflight happens to find - checked
    # first, unconditionally, before the preflight ever runs.
    #
    # MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R4, taken): the
    # predicate itself now lives in privacy.py, shared with cli.py's own
    # identical check - see that function's own docstring for why the
    # two CALL SITES stay deliberately separate (CR17-1's own load-
    # bearing two-attempt lock/privacy ordering).
    pairing_error = privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=acknowledge_unignored, work_id=work_id)
    if pairing_error is not None:
        raise ScanRefused(pairing_error)
    # FIX ROUND 35 (twenty-ninth cold read, F2 MAJOR part (a) - the newest
    # mechanism's own dead end): this used to run the preflight FIRST,
    # unconditionally, and only ever converted to `acknowledged_unignored`
    # when THAT SAME preflight raised - so an attended, correctly-paired
    # acknowledgment was reachable ONLY for a preflight-detected refusal,
    # never for round 34's own store-wide post-publish refusal (which the
    # cheap, early preflight cannot see by construction - that is exactly
    # why the store-wide check exists). When the preflight passed but the
    # deeper check later refused, the CLI's own attempt-2 retry (already
    # acknowledge_unignored=True at this point) hit this SAME preflight,
    # which passed AGAIN, returned "ignored" AGAIN, and the store-wide
    # check refused AGAIN - forever, with the refusal's own message
    # directing the operator to a flag that provably could never change
    # the outcome. An attended, correctly-paired acknowledgment (the
    # pairing above already REQUIRES a non-empty work_id) now applies
    # UNCONDITIONALLY - it is a statement that the operator has already
    # accepted this run's own privacy risk, never conditional on which
    # LAYER would have refused it. `vcs_kind` is still detected the same
    # way `run_privacy_preflight`'s own worktree check would - never
    # skipped, since scan.json's own `privacy.vcs_kind` field must still
    # be accurate either way.
    if acknowledge_unignored:
        vcs_kind = "git" if privacy._is_git_worktree(root) else "none"  # noqa: SLF001
        return privacy.acknowledge_unignored_private_store(
            root, vcs_kind=vcs_kind, work_id=work_id, matched_rule=None,
        )
    return privacy.run_privacy_preflight(root)


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

    # FIX ROUND 21 (seventeenth cold read, CR17-1 BLOCKER, part 3): a
    # forced lock recovery is a safety-relevant event this run's own
    # provenance must not stay silent about - captured here (before the
    # record is gone) and turned into a named, degrading problem once
    # `problems` is assembled below. `None` means either the override
    # was never requested, or nothing was actually there to clear (a
    # genuine no-op, not itself notable).
    forced_lock_recovery_record = None
    if recover_stale_lock:
        forced_lock_recovery_record = lock.recover_stale_lock(comprehension_dir)

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
            # FIX ROUND 19 (fifteenth cold read, F7 MINOR, same class as
            # CR10-12): this message reaches the CLI's plain stderr
            # output the same way CR10-12's own VcsPrivacyRefused
            # message did - the raw, absolute local root named next to a
            # projection family that otherwise never persists one. The
            # basename is enough to identify which directory was empty.
            raise ScanRefused(
                f"no files were enumerated under {root.name!r} - refusing to publish a "
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
        # FIX ROUND 13c (reviewer-3's part 1 on round 13b): a worker
        # problem carrying its own qualified_name (an adapter-attributed
        # reason - e.g. cli_main_unrecognized, owned by ONE declared
        # type) must never broadcast into every unit's adapter_problem_
        # reason(s) via the path-wide map below - only the specific
        # (path, qualified_name) it names. Reasons with no qualified_name
        # (parse failures, resource caps, route fail-safes, ...) keep
        # today's file-wide broadcast, unchanged.
        worker_problem_reasons_by_path: dict[str, list[str]] = {
            p.relative_path: [] for p in worker_result.problems if p.qualified_name is None
        }
        worker_problem_reasons_by_unit: dict[tuple[str, str], list[str]] = {}
        # FIX ROUND 21c (reviewer-3's re-delta, THE CARRY, wrong-data):
        # every worker problem carrying a qualified_name ALSO accumulates
        # here, keyed by that bare qualified_name alone - no relative_path
        # at all. A same-file adapter reason (cli_main_unrecognized,
        # route_annotation_unassociated, ...) is already correctly
        # attributed via worker_problem_reasons_by_unit above; this is
        # additive for exactly the CROSS-FILE case that tuple can never
        # match - a web.xml <listener>'s own unsupported_entry_point_shape
        # problem is recorded at web.xml's own path (the declaring file,
        # correctly - web.xml genuinely has no unit of its own to
        # broadcast to), naming a class declared in an entirely
        # DIFFERENT .java file. modules_artifact.build_modules resolves
        # this via the SAME exact-qualified-name registry lookup
        # features_artifact.py's own owner resolution already uses -
        # never applied when the name is ambiguous (2+ claimants) or
        # unresolved in-scan (0 claimants), in which case the
        # web.xml-attributed problems.json record stands on its own,
        # unchanged, exactly as it always did before this fix.
        worker_problem_reasons_by_qualified_name: dict[str, list[str]] = {}
        for p in worker_result.problems:
            if p.qualified_name is not None:
                reasons = worker_problem_reasons_by_unit.setdefault(
                    (p.relative_path, p.qualified_name), [])
                qualified_name_reasons = worker_problem_reasons_by_qualified_name.setdefault(
                    p.qualified_name, [])
                if p.reason_code not in qualified_name_reasons:
                    qualified_name_reasons.append(p.reason_code)
            else:
                reasons = worker_problem_reasons_by_path[p.relative_path]
            if p.reason_code not in reasons:
                reasons.append(p.reason_code)
        for reasons in worker_problem_reasons_by_path.values():
            reasons.sort()
        for reasons in worker_problem_reasons_by_unit.values():
            reasons.sort()
        for reasons in worker_problem_reasons_by_qualified_name.values():
            reasons.sort()

        # FIX ROUND 23 (nineteenth cold read, F3 MAJOR, wrong-data): the
        # non-degrading half of worker.py's own "unsupported_language"
        # reason (TIER 3 - a build/tooling/infra file this producer was
        # never going to model) is the exact in-run evidence modules_
        # artifact.py's own classification derivation needs to
        # distinguish it from the DEGRADING half (TIER 2 - a real,
        # unmodeled application-code file) - both currently collapse
        # into the identical reason_code string once threaded through
        # worker_problem_reasons_by_path above.
        # FIX ROUND 27 (twenty-third cold read, F3 MAJOR, wrong-data):
        # widened to also include the non-degrading half of
        # "encoding_undecodable" - a binary-excluded and an encoding-
        # undecodable, non-adapter-handled .xml file are EPISTEMICALLY
        # IDENTICAL (this run cannot know the file's own tier without
        # reading it, in either case), and round 26b's own binary ruling
        # already refused to degrade OR misclassify the binary-excluded
        # twin ("degrading every repo carrying an unreadable
        # logback.xml") - an encoding-undecodable one previously kept
        # BOTH the production default (never reaching this set at all)
        # AND WorkerProblem.degrades_run's own True default, reopening
        # the identical round-16/round-23-F3 harm this set exists to
        # prevent. worker.py's own xml-root-sniff decode site now passes
        # degrades_run=False for exactly this non-adapter-handled-XML
        # case (never for .java/pom.xml/web.xml, which stay degrading -
        # they are code-bearing by definition), so filtering on
        # `not p.degrades_run` here correctly isolates only that case.
        non_degrading_unsupported_language_paths = frozenset(
            p.relative_path for p in worker_result.problems
            if not p.degrades_run
            and p.reason_code in ("unsupported_language", "encoding_undecodable")
        )
        # MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R2, wrong-data):
        # computed HERE, before build_modules, so it can feed a real
        # modules.json unit for each such path - see build_modules's own
        # binary_excluded_root_sniffed_xml_digests parameter docstring.
        # Reused below (unchanged) for the existing problems.json record
        # this same predicate already produces - one predicate, two
        # consumers, never two independently-drifting copies of it.
        binary_excluded_root_sniffed_xml_digests = {
            entry["path"]: entry["content_digest"]
            for entry in discovery_result.excluded_roots
            if entry["category"] == "binary"
            and "content_digest" in entry
            and worker.is_a_root_sniffed_xml_extension(entry["path"])
        }
        # FIX ROUND 31 (twenty-seventh cold read, F3 MINOR, completeness),
        # WIDENED by FIX ROUND 32 (twenty-eighth cold read, F8 LOW, JUDGE,
        # taken): the exact same additive-unit shape as the root-sniffed-
        # xml dict above, for EVERY binary-excluded file this run would
        # otherwise have tried to understand as code (the SAME predicate
        # `binary_excluded_code_bearing_problems` below already uses, so
        # the two can never independently drift) - round 31 itself only
        # ever populated this for pom.xml/web.xml specifically; a binary-
        # excluded .java (or a binary-excluded tier-2 shape like .jsp/
        # .kt) got the identical real, DEGRADING problem but still no
        # synthesized unit, the SAME epistemic state with different
        # visibility the reader's own F8 measured. See build_modules's
        # own binary_excluded_code_bearing_digests parameter docstring.
        binary_excluded_code_bearing_digests = {
            entry["path"]: entry["content_digest"]
            for entry in discovery_result.excluded_roots
            if entry["category"] == "binary"
            and "content_digest" in entry
            and worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
                entry["path"])
        }
        # FIX ROUND 29 (twenty-fifth cold read, F1 BLOCKER, wrong-data):
        # every web.xml this run parsed may have recorded its own
        # duplicated servlet-name/filter-name conflicts (java.parse_
        # web_xml's own descriptor_name_conflicts) - aggregated across
        # every producer here, the same "collect once, thread through"
        # pattern non_degrading_unsupported_language_paths above follows.
        descriptor_name_conflicts = [
            conflict
            for result in java_results.values()
            for conflict in result.descriptor_name_conflicts
        ]
        modules = modules_artifact.build_modules(
            discovery_result, java_results,
            worker_problem_reasons_by_path=worker_problem_reasons_by_path,
            worker_problem_reasons_by_unit=worker_problem_reasons_by_unit,
            worker_problem_reasons_by_qualified_name=worker_problem_reasons_by_qualified_name,
            non_degrading_unsupported_language_paths=non_degrading_unsupported_language_paths,
            binary_excluded_root_sniffed_xml_digests=binary_excluded_root_sniffed_xml_digests,
            binary_excluded_code_bearing_digests=binary_excluded_code_bearing_digests,
            descriptor_name_conflicts=descriptor_name_conflicts,
        )
        # M7 (cold-read, PR-B fix round 3): discovery already computed
        # each file's own content digest - dependencies_artifact.py and
        # features_artifact.py's producers carried source_digest=None
        # unconditionally, never wired to it.
        file_digests = {f.relative_path: f.content_digest for f in discovery_result.files}
        # F2 MAJOR (eighth cold read, fix round 12): every path this
        # SAME run recorded a worker-level problem for (an adapter
        # resource cap, a read/parse failure) - an import naming one of
        # these files' declared types must resolve unresolved, never a
        # false-positive external claim over evidence that is merely
        # missing, not genuinely third-party.
        #
        # FIX ROUND 21 (seventeenth cold read, the reader's own LOW-
        # CONFIDENCE flag, verified real): this used to consult ONLY
        # worker-level problems - a file discovery itself excludes
        # outright BEFORE the worker ever sees it (the 64MiB per-file
        # cap, an unreadable file's own stat()/read() failure, the
        # MAX_FILESYSTEM_ENTRIES entry-count cap) never reaches
        # java_results at all, so it published no unit and no worker
        # problem either - an importer of that file's declared type then
        # fell all the way through to a false confident EXTERNAL claim
        # over genuinely in-repo (merely oversized/unreadable/unwalked)
        # source, the exact same class F2 above already closed for the
        # worker-level case. Every discovery-level problem already
        # carries its own path (or None for a whole-run problem with no
        # single file, e.g. an unreadable .gitmodules) - harmless to
        # include unconditionally, since a non-.java path can never
        # match `_degraded_java_suffix_match` anyway.
        degraded_paths = frozenset(worker_problem_reasons_by_path) | frozenset(
            p["path"] for p in discovery_result.problems if p.get("path") is not None)
        # FIX ROUND 18 (fourteenth cold read, F6 JUDGE, taken): a file
        # discovery excluded outright as binary content (a NUL byte in
        # its sniffed prefix) records ONLY a bare exclusion count/
        # excluded_roots entry, never a problem - correct for a genuine
        # binary blob, but silently identical for a UTF-16-encoded
        # .java file (a legal javac input) or a tier-2 code-bearing
        # file (.jsp, .kt, ...) that happened to trip the same
        # heuristic: the run still reports complete/zero problems even
        # though real code went unread. Named and degrading here,
        # exactly the same severity tier 2 already gets - a genuinely
        # binary extension (absent from both the adapter-handled and
        # tier-2 sets) stays exactly as silent as it is today. Moved
        # ahead of build_dependencies (round 20) - its own finding now
        # also feeds the poison rule below (a code-bearing file THIS
        # RUN excluded is exactly as poisoning as a code-bearing
        # DIRECTORY discovery's own peek finds).
        binary_excluded_code_bearing_problems = [
            _problem_record(
                "binary_excluded_code_bearing_file",
                entry["path"],
                "a file whose extension this run would otherwise have tried to understand "
                "as code was excluded outright as binary content (a NUL byte in its sniffed "
                "prefix) - a UTF-16-encoded source file is a legal compiler input that trips "
                "this heuristic; recorded as a genuine unread-code gap, never silently "
                "vanished the way a genuinely binary file correctly still is",
            )
            for entry in discovery_result.excluded_roots
            if entry["category"] == "binary"
            and worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
                entry["path"])
        ]
        # FIX ROUND 26b (reviewer-3 delta on `38a21f3`, item 2, R4 carry
        # OVERTURNED - closed, wrong-data): a binary-excluded, non-
        # adapter-handled .xml file (a Spring bean/Struts config XML, or
        # an ordinary logback.xml - this run cannot tell which without
        # decoding it, which is exactly what binary exclusion prevents)
        # used to vanish completely silently - complete, 0 problems, no
        # poison - while its UTF-8 twin would DEGRADE the run if it were
        # tier-2 code-bearing. RECORDED here (visible, addressable in
        # problems.json) but deliberately kept OUT of both the `status`
        # degradation OR-chain below and `externality_poisoned` - this
        # run has no evidence the file was actually code-bearing, and
        # guessing toward degrading would brand every repo carrying an
        # unreadable logback.xml, the round-16 dilution this producer's
        # own tier calibration already refuses to reopen. Excludes
        # pom.xml/web.xml (already covered, and degrading, above) by
        # construction - `is_a_root_sniffed_xml_extension` itself
        # excludes both adapter-handled basenames.
        # MICRO-ROUND 28b (R2): iterates the SAME dict build_modules just
        # consumed above (one predicate, two consumers) rather than
        # recomputing the path set independently - the two could
        # otherwise silently drift apart.
        binary_excluded_root_sniffed_xml_problems = [
            _problem_record(
                "binary_excluded_root_sniffed_xml",
                path,
                "an XML file excluded outright as binary content (a NUL byte in its "
                "sniffed prefix) could not be root-element-sniffed to determine whether "
                "it is code-bearing (e.g. Spring bean/Struts config XML) or ordinary "
                "tooling/config XML (e.g. logback.xml) - recorded as a genuine unread-"
                "file gap, but never guessed toward a degrading verdict this run has no "
                "evidence for",
            )
            for path in binary_excluded_root_sniffed_xml_digests
        ]
        # FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR - THE REACTOR
        # RULE): a pom's own declared <module> entry whose path resolves
        # into a region this run excluded outright is positive, DIRECT
        # evidence that region holds real, first-party source - stronger
        # than the generic peek (discovery.py), since it is the build
        # tool's own explicit declaration. Resolved here (not in java.py
        # or worker.py, neither of which has discovery's own excluded-
        # root paths available) against each declaring pom's own
        # directory; both sides of the comparison are plain repo-
        # relative paths in the SAME coordinate space, so - unlike the
        # now-retired qualified-name-vs-directory string match - a
        # straightforward prefix/equality check is exact, not a guess.
        excluded_root_paths_for_reactor_rule = [r["path"] for r in discovery_result.excluded_roots]
        reactor_rule_problems = []
        for pom_path, result in java_results.items():
            pom_dir = pom_path.rsplit("/", 1)[0] if "/" in pom_path else ""
            for module_path in result.declared_module_paths:
                resolved = posixpath.normpath(
                    posixpath.join(pom_dir, module_path)) if pom_dir else posixpath.normpath(
                    module_path)
                resolved = resolved.replace("\\", "/")
                if any(
                    resolved == root or resolved.startswith(root + "/")
                    for root in excluded_root_paths_for_reactor_rule
                ):
                    reactor_rule_problems.append(_problem_record(
                        "module_directory_excluded",
                        pom_path,
                        f"this pom declares <module>{module_path}</module>, whose own path "
                        f"({resolved!r}) resolves into a region this run excluded outright - "
                        "positive evidence the excluded region holds real, first-party source, "
                        "not third-party build output",
                    ))
        # FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR, wrong-data -
        # THE POISON RULE): retires the round-16 string-matching
        # excluded-region guard for the containment question entirely
        # (inert for the mainstream Maven layout, where an excluded
        # root's own recorded path - a bare directory name - has no
        # string relationship at all to the unwalked source arbitrarily
        # deeper inside it). A registry miss may publish a confident
        # EXTERNAL claim only when this run can vouch every excluded
        # region was genuinely code-free - discovery's own run-wide peek
        # result (generated/vendor DIRECTORIES), OR'd with a code-bearing
        # binary-excluded FILE (round 18's own F6 finding - the single-
        # file mirror image of the same directory peek), OR'd with the
        # reactor rule's own finding (a pom explicitly declaring a
        # module inside an excluded region is decisive on its own,
        # regardless of what the generic peek happened to find).
        externality_poisoned = (
            discovery_result.excluded_region_may_contain_target
            or bool(binary_excluded_code_bearing_problems)
            or bool(reactor_rule_problems)
        )
        # FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-
        # rule VISIBILITY): reviewer-3 measured the poison rule firing
        # SILENTLY in exactly the mainstream shapes round 20 widened it
        # for - a vendored-module repo and a truncated-peek build/ repo
        # both published every third-party dependency as unresolved on a
        # complete/problem_count-0 run, with NO reason on any surface
        # (dependency records indistinguishable from an ordinary
        # unresolved miss; no scan.json flag; problems.json empty). One
        # NEW, uniform `externality_suppressed` problem per triggering
        # root/pom - ADDITIVE to whatever reason-specific problem that
        # root already carries (`excluded_region_peek_truncated`/
        # `binary_excluded_code_bearing_file`/`module_directory_
        # excluded` each answer "what is wrong with THIS root"; this one
        # answers "this root is why EVERY unresolved external miss in
        # this run stayed unresolved instead of a confident external
        # claim" - a materially different fact worth its own record even
        # when it co-occurs with an existing one).
        externality_suppressed_problems = [
            _problem_record(
                "externality_suppressed", entry["path"],
                "this excluded region "
                + ("contains at least one adapter-handled or tier-2 code-bearing file"
                   if entry["trigger"] == "peek_positive" else
                   "could not be fully peeked before its entry cap - unknown whether it "
                   "holds code")
                + " - every external-registry-miss import in this run resolves unresolved "
                  "rather than a confident external claim because of this root",
            )
            for entry in discovery_result.poisoning_excluded_roots
        ] + [
            _problem_record(
                "externality_suppressed", p["path"],
                "this file was excluded outright as binary content but is code-bearing - "
                "every external-registry-miss import in this run resolves unresolved rather "
                "than a confident external claim because of it",
            )
            for p in binary_excluded_code_bearing_problems
        ] + [
            _problem_record(
                "externality_suppressed", p["path"],
                "this pom's own declared <module> resolves into an excluded region - every "
                "external-registry-miss import in this run resolves unresolved rather than a "
                "confident external claim because of it",
            )
            for p in reactor_rule_problems
        ]
        dependencies = dependencies_artifact.build_dependencies(
            java_results, file_digests=file_digests, degraded_paths=degraded_paths,
            externality_poisoned=externality_poisoned)
        entry_points, features = features_artifact.build_features(
            java_results, file_digests=file_digests)
        readiness_signals, readiness_summaries = readiness_artifact.build_readiness(
            modules, dependencies, features, entry_points,
            externality_poisoned=externality_poisoned)

        # FIX ROUND 29 (twenty-fifth cold read, F2 MAJOR, wrong-data):
        # design step 8 ("Normalize records, resolve only evidenced
        # edges, merge declarations") has no referential-integrity pass
        # at all this slice - a producer bug minting a dangling
        # reference (an edge/entry-point/feature/signal/module naming a
        # unit, entry point, or feature that does not exist) would
        # publish an immutable run reporting clean/complete forever,
        # discoverable only if a caller happened to also run `validate`
        # afterward. The SAME sweep `validate_run` runs against records
        # read back from disk now runs HERE too, against these same
        # records still in memory, before any of them is ever written
        # to staging - a producer bug is refused at its own source,
        # never merely detectable after the fact.
        _publish_time_dangling = _dangling_reference_categories(
            modules=modules, dependencies=dependencies, entry_points=entry_points,
            features=features, readiness_signals=readiness_signals,
        )
        if any(ids for _, ids in _publish_time_dangling):
            # MICRO-ROUND 29b (JUDGE, note-only, lean take): this refusal
            # fires after `create_staging_dir` (line ~495) but before any
            # artifact is ever written there - it leaves behind an
            # orphaned `.staging/<id>/` holding only `owner.json`.
            # staging.py's own Note 10 already judges this DESIGNED, not a
            # leak (bounded, one per failed attempt, self-clearing the
            # moment this still-live process actually ends, since the
            # NEXT run's own lock-acquisition reclaim then sees a dead
            # owner) - the same dead-or-leave-alone contract this refusal
            # must not special-case around. Named here instead, pointing
            # an operator at the existing remedy rather than leaving them
            # to discover `prune --staging` separately (the same
            # named-not-silent idiom round 17's own CR13-10 carry already
            # asks for).
            raise ComprehensionError(
                "refusing to publish: this run's own records contain a dangling "
                "cross-artifact reference - "
                f"{_dangling_reference_detail(_publish_time_dangling)} "
                "(this run's own .staging/ directory is left in place, self-clearing "
                "on the next scan's own lock-acquisition reclaim once this process "
                "exits; run `agenttalk comprehension prune --staging` to reclaim it "
                "sooner)"
            )

        # FIX ROUND 32 (twenty-eighth cold read, F4(a) MAJOR, completeness):
        # the SAME "refuse at the source, never merely detectable after
        # the fact" discipline as the dangling-reference sweep just above -
        # see _module_path_confinement_violations's own docstring.
        _publish_time_path_violations = _module_path_confinement_violations(modules)
        if _publish_time_path_violations:
            raise ComprehensionError(
                "refusing to publish: this run's own module records contain an "
                "unconfined path - " + "; ".join(_publish_time_path_violations) + " "
                "(this run's own .staging/ directory is left in place, self-clearing "
                "on the next scan's own lock-acquisition reclaim once this process "
                "exits; run `agenttalk comprehension prune --staging` to reclaim it "
                "sooner)"
            )

        # FIX ROUND 29 (twenty-fifth cold read, F6 polish, wrong-data):
        # design step 8 names "deterministic ordering" as a real publish-
        # validation requirement, alongside referential integrity above -
        # nothing enforced it (micro-round 28b's own binary-excluded-
        # root-sniffed-XML synthesized units landed PREPENDED to modules,
        # never interleaved in path order, until modules_artifact.py's
        # own build_modules sorted its return value). A cheap, judge-
        # taken assertion here catches a FUTURE regression at its own
        # source the same way the dangling-reference check above does,
        # rather than leaving "deterministic ordering" a claim nothing
        # actually checks.
        _expected_module_order = sorted(modules, key=lambda m: (m.paths[0] if m.paths else "", m.unit_id))
        if modules != _expected_module_order:
            # MICRO-ROUND 29b (JUDGE, note-only): the same orphaned-
            # staging-dir disposition as the dangling-reference refusal
            # above - see its own comment.
            raise ComprehensionError(
                "refusing to publish: modules.json's own records are not in "
                "deterministic (path-then-unit_id) order "
                "(this run's own .staging/ directory is left in place, self-clearing "
                "on the next scan's own lock-acquisition reclaim once this process "
                "exits; run `agenttalk comprehension prune --staging` to reclaim it "
                "sooner)"
            )

        # N1 (third cold read, fix round 5): find_case_fold_collisions
        # existed with its own passing unit tests and zero production
        # callers - the same dead-code shape round 3's M9 found for
        # parse_web_xml. Two paths that collide once case-folded (a real
        # risk once a run crosses to/from a case-insensitive filesystem)
        # is a named problem code the design itself expects; it was never
        # actually emitted anywhere in the pipeline.
        case_collisions = find_case_fold_collisions(relative_paths)

        # FIX ROUND 16 (twelfth cold read, B1 BLOCKER, part 3): one
        # problem per DISTINCT conflict_id (never per colliding unit -
        # that would publish the same collision twice, once per side),
        # grouping modules_artifact.py's own conflict_id tagging by the
        # id itself since that is the one value both colliding units
        # share.
        #
        # MICRO-ROUND 29b (reviewer-3's delta on round 29's own F1, one-
        # condition wrong-data fix): `conflict_id` gained a SECOND
        # meaning in round 29 - `conflict_kind` now distinguishes an FQN
        # collision (`duplicate_qualified_name`) from a web.xml
        # descriptor-name collision (`duplicate_descriptor_name`) sharing
        # the SAME `conflict_id` tagging mechanism - but this emitter,
        # written before `conflict_kind` existed, grouped by `conflict_id`
        # alone and hardcoded `reason_code="duplicate_qualified_name"` for
        # every group. A descriptor-name conflict's own candidate classes
        # now ALSO flow through here, publishing a SECOND, factually false
        # problem row (`"p.A" declared in [A.java, B.java]"` - `p.A` is
        # declared in `A.java` only) that contradicts `modules.json`'s own
        # `conflict_kind` for the same `conflict_id`, duplicating
        # `java.py`'s own already-correct `duplicate_descriptor_name` row
        # (round 29 F1). Filtered to groups whose `conflict_kind` is
        # actually `duplicate_qualified_name` - the descriptor conflict
        # already has its own accurate row; this emitter must not
        # generate a second, generic one for it.
        modules_by_conflict_id: dict[str, list[modules_artifact.ModuleRecord]] = {}
        for m in modules:
            if m.conflict_id is not None and m.conflict_kind == "duplicate_qualified_name":
                modules_by_conflict_id.setdefault(m.conflict_id, []).append(m)
        duplicate_qualified_name_problems = [
            _problem_record(
                "duplicate_qualified_name",
                None,
                bounded_detail(
                    f"{group[0].qualified_name!r} declared in "
                    f"{sorted(p for m in group for p in m.paths)}"),
                qualified_name=group[0].qualified_name,
                # FIX ROUND 24 (twentieth cold read, F8a): every unit in
                # `group` shares this SAME conflict_id by construction
                # (that is how `modules_by_conflict_id` grouped them) -
                # `group[0]` is as good a source for it as any other.
                conflict_id=group[0].conflict_id,
            )
            for group in modules_by_conflict_id.values()
        ]

        # FIX ROUND 21 (seventeenth cold read, CR17-1 BLOCKER, part 3): a
        # forced --recover-stale-lock recovery is a safety-relevant event
        # this run's own provenance must not stay silent about.
        #
        # FIX ROUND 21b (reviewer-3's re-delta, MINOR 1, wrong-data): a
        # forced clear over a MALFORMED/unreadable record (``lock.
        # recover_stale_lock``'s own ``record_unreadable`` sentinel) is
        # MORE safety-relevant than an ordinary dead-owner reclaim, not
        # less - this run could not verify who (if anyone) held the lock,
        # or when, before clearing it. Named as its own distinct detail
        # rather than silently reusing the pid/acquired_at wording with
        # fabricated-looking ``None`` values.
        forced_lock_recovery_problems = [
            _problem_record(
                "scan_lock_forcibly_recovered", None,
                "an attended --recover-stale-lock action cleared an existing scan.lock "
                "whose own record could not be parsed (pid unknown, acquisition time "
                "unknown) before this run began - a forced clear over an unreadable "
                "record could not verify who, if anyone, held the lock beforehand"
                if forced_lock_recovery_record.get("record_unreadable")
                else
                f"an attended --recover-stale-lock action cleared an existing scan.lock "
                f"(previously recorded pid {forced_lock_recovery_record.get('pid')!r}, "
                f"acquired {forced_lock_recovery_record.get('acquired_at')!r}) before this "
                "run began - the prior owner was not provably live, but its own scan was "
                "never confirmed complete",
            )
        ] if forced_lock_recovery_record is not None else []
        problems = [
            _problem_record(p["reason_code"], p.get("path"), p["detail"])
            for p in discovery_result.problems
        ] + [
            _problem_record(
                p.reason_code, p.relative_path, p.detail, qualified_name=p.qualified_name)
            for p in worker_result.problems
        ] + [
            _problem_record(
                "case_collision", second, bounded_detail(f"case-folds identically to {first!r}"))
            for first, second in case_collisions
        ] + duplicate_qualified_name_problems + binary_excluded_code_bearing_problems + (
            binary_excluded_root_sniffed_xml_problems) + reactor_rule_problems + (
            externality_suppressed_problems) + forced_lock_recovery_problems
        # FIX ROUND 14b (reviewer-3's ratified CR10-5 split): a worker
        # problem's own `degrades_run` (worker.py) distinguishes
        # "recorded, visible" from "the run's status also degrades over
        # this" - the first time those two claims diverge for the SAME
        # reason code (unsupported_language: a tooling/config file is
        # worth recording but not worth degrading a healthy run over).
        # Every OTHER problem source here (discovery, case collisions)
        # still always degrades, unchanged.
        degrading_worker_problems = any(p.degrades_run for p in worker_result.problems)
        # FIX ROUND 20b (THE MAJOR - poison-rule VISIBILITY, part 2): a
        # poisoned run's own external surface is unknown for its
        # ENTIRE remaining lifetime (every future registry miss resolves
        # unresolved, not just the ones already recorded above) - "not
        # complete in any useful sense," the reviewer's own ruling,
        # consistent with F4/19b's own "visible absence over silent
        # claims" precedent. This is a NEW degradation source distinct
        # from F4's own deliberately narrower src-ancestry-gated
        # degradation (unchanged) - a run poisoned by an ordinary
        # generated-sources peek hit now also degrades, correctly: its
        # dependency resolution really is incomplete, unlike round 16b's
        # own dilution case (where nothing was actually wrong).
        # FIX ROUND 26b (item 2): `binary_excluded_root_sniffed_xml_
        # problems` is DELIBERATELY absent from this OR-chain (and from
        # `externality_poisoned` above) - recorded, never degrading, see
        # its own comment above for why guessing a verdict here would be
        # the round-16 dilution this producer's own tier calibration
        # already refuses to reopen.
        status = "degraded" if (
            discovery_result.degraded or discovery_result.problems or case_collisions
            or degrading_worker_problems or duplicate_qualified_name_problems
            or binary_excluded_code_bearing_problems or reactor_rule_problems
            or externality_poisoned or forced_lock_recovery_problems
        ) else "complete"

        modules_doc = {
            **_envelope(
                artifact_type=MODULES_ARTIFACT_TYPE, schema_version=modules_artifact.MODULES_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "units": [m.to_json() for m in modules],
            # FIX ROUND 28 (twenty-fourth cold read, F3 JUDGE, declared):
            # see CLASSIFICATION_CAVEAT's own docstring - the same
            # "declare it, don't leave it to be independently
            # rediscovered" discipline ASSESSMENT_STATE_CAVEAT/
            # structural_caveat already follow for their own artifacts.
            "classification_caveat": modules_artifact.CLASSIFICATION_CAVEAT,
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
            # FIX ROUND 22 (eighteenth cold read, F7 MINOR): declared
            # here, not just in features_artifact.py's own module
            # constant - see FEATURES_STRUCTURAL_CAVEAT, the same
            # "declare it, don't leave it to be independently
            # rediscovered" discipline ASSESSMENT_STATE_CAVEAT already
            # follows for readiness.json.
            "structural_caveat": features_artifact.FEATURES_STRUCTURAL_CAVEAT,
        }
        readiness_doc = {
            **_envelope(
                artifact_type=READINESS_ARTIFACT_TYPE, schema_version=READINESS_SCHEMA_VERSION,
                scan_id=scan_id, generated_at=generated_at),
            "signals": [s.to_json() for s in readiness_signals],
            "summaries": [s.to_json() for s in readiness_summaries],
            # FIX ROUND 16 (twelfth cold read, N2 MINOR): declared here,
            # not just in readiness_artifact.py's own module docstring -
            # see ASSESSMENT_STATE_CAVEAT.
            "assessment_state_caveat": readiness_artifact.ASSESSMENT_STATE_CAVEAT,
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
        # FIX ROUND 16 (twelfth cold read, B2 BLOCKER, part 2): a bare
        # category -> count record (`exclusions`) hid WHICH path was
        # excluded and WHY - the same "excluded roots with an explicit
        # boundary reason" scan.json field the design already names.
        # Bounded the same way `boundaries` already is.
        excluded_root_rows, excluded_roots_omitted = _bounded_boundaries(
            discovery_result.excluded_roots)
        # FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-
        # rule VISIBILITY, part 3): a run-level flag beside `excluded_
        # roots` so a machine consumer can distinguish "this dependency
        # is unresolved because the resolver genuinely could not place
        # it" from "this run declined to claim externality run-wide" -
        # without this, the two are indistinguishable from dependencies.
        # json alone. Bounded the same way every other list here is.
        externality_suppressed_roots, externality_suppressed_roots_omitted = _bounded_boundaries([
            {"path": entry["path"], "trigger": entry["trigger"]}
            for entry in discovery_result.poisoning_excluded_roots
        ] + [
            {"path": p["path"], "trigger": "binary_exclusion"}
            for p in binary_excluded_code_bearing_problems
        ] + [
            {"path": p["path"], "trigger": "reactor"}
            for p in reactor_rule_problems
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
            # FIX ROUND 30 (twenty-sixth cold read, F3 MINOR, completeness):
            # see FINGERPRINT_CAVEAT's own docstring - the same "declare
            # the gap in-artifact, don't leave it to be independently
            # rediscovered" discipline ASSESSMENT_STATE_CAVEAT/
            # CLASSIFICATION_CAVEAT/PROVENANCE_CAVEAT/structural_caveat
            # already follow for their own artifacts.
            "fingerprint_caveat": discovery.FINGERPRINT_CAVEAT,
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
            # Round 11c (reviewer-3 delta on round 11b, vehicle change):
            # merges discovery's own enumeration-level exclusion counts
            # with the worker's adapter-level ones (currently: a pom's
            # profile-scoped dependency, a DECLARED scope limitation -
            # never a run-degrading problem the way an unreadable
            # .gitmodules or an unrecoverable route value is). Same flat
            # category -> count idiom either way; no key collision
            # today (the two counters never share a category name).
            "exclusions": dict(sorted({
                **discovery_result.exclusions, **worker_result.exclusions,
            }.items())),
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
            "excluded_roots": excluded_root_rows,
            "excluded_roots_omitted_count": excluded_roots_omitted,
            "externality_suppressed": externality_poisoned,
            "externality_suppressed_roots": externality_suppressed_roots,
            "externality_suppressed_roots_omitted_count": externality_suppressed_roots_omitted,
            "unsupported_relations": list(java_adapter.UNSUPPORTED_RELATIONS),
            "unsupported_invoke_shapes": list(java_adapter.UNSUPPORTED_INVOKE_SHAPES),
            # FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, part (b) -
            # THE CLASS-CLOSER): the entry-point edition of the same
            # enumerated-coverage-gap idiom the two lines above already
            # publish.
            "unsupported_entry_point_shapes": list(java_adapter.UNSUPPORTED_ENTRY_POINT_SHAPES),
            # FIX ROUND 21c (reviewer-3's re-delta, THE ASK - second
            # instance, closing the class): the SAME static-capability-
            # declaration shape as the three fields above, for the entry-
            # point KIND vocabulary itself - see ENTRY_POINT_KINDS's own
            # docstring.
            "entry_point_kinds": dict(java_adapter.ENTRY_POINT_KINDS),
            # FIX ROUND 24 (twentieth cold read, F8b, declare-not-
            # silently-guess): see readiness_artifact.PROVENANCE_CAVEAT's
            # own docstring - the SAME "declare it in scan.json, don't
            # leave it to be independently rediscovered" discipline
            # ASSESSMENT_STATE_CAVEAT/FEATURES_STRUCTURAL_CAVEAT already
            # follow, published here rather than only in readiness.json
            # since the gap spans producer identity across every
            # artifact, not readiness signals alone.
            "provenance_caveat": readiness_artifact.PROVENANCE_CAVEAT,
            "record_counts": record_counts,
            "record_count_definition": RECORD_COUNT_DEFINITION,
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
        record_counts=record_counts, now=now, privacy_result=privacy_result,
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


def _resolve_run_id(run_id: str | None, index_doc: dict[str, Any]) -> str:
    """FIX ROUND 29 (twenty-fifth cold read, F7 polish, wrong-data): a
    ``--run`` value of ``""`` (or whitespace-only) used to fall through
    the bare ``run_id or _index_field(...)`` falsy check exactly like
    ``None`` (not provided) - silently resolving to the LATEST run
    instead of ever reaching the closed scan-ID grammar's own refusal
    (``envelope.validate_scan_id``, which already correctly rejects an
    empty string - it just never got the chance to). Shared by
    ``get_status``/``get_report``/``validate_run`` so the same mistake
    can never independently recur at a fourth call site: ``None`` means
    "not provided, use the latest"; anything else - including an empty
    or whitespace-only string - is treated as a REAL, explicit value the
    caller must account for, refused here before it can be silently
    substituted."""
    if run_id is not None and not run_id.strip():
        raise EnvelopeError(f"--run must not be empty or whitespace-only, got {run_id!r}")
    return run_id if run_id is not None else _index_field(index_doc, "latest_scan_id")


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
        # FIX ROUND 21 (seventeenth cold read, CR17-8 MINOR, the class-
        # closer): an OSError embeds the FULL absolute local path via its
        # own str(exc) (exc.filename) - bounded_os_error_detail is the
        # same machine-local-path-leak-safe helper worker.py's own M-3
        # fix already established.
        raise ComprehensionError(
            bounded_os_error_detail("scan.json's bytes could not be read for verification", exc)
        ) from exc
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


#: FIX ROUND 28 (twenty-fourth cold read, F7, completeness): get_status's
#: own docstring already states its narrower read-cost tier (scan.json's
#: own envelope/anchor only - never modules/dependencies/features/
#: readiness/problems) - that distinction lived only in source, invisible
#: to an actual caller who might otherwise read a healthy status response
#: as having already checked every artifact's own digest. Declared in the
#: payload now, pointing at `validate` as the real full-run verification
#: path - the same "declare it, don't leave it to be independently
#: rediscovered" discipline every other caveat/note here already follows.
STATUS_ARTIFACT_INTEGRITY_HINT = (
    "status verifies only scan.json's own envelope and index anchor - it does "
    "NOT check modules.json/dependencies.json/features.json/readiness.json/"
    "problems.json's own digests or record counts (report/validate do, in "
    "full, every time); run `validate` for full-run artifact integrity "
    "verification."
)


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
        # FIX ROUND 19 (fifteenth cold read, F7 MINOR, same class as
        # CR10-12 - swept across all three identical copies of this
        # message, get_status/get_report/validate_run): the raw absolute
        # local root reaching the CLI's plain stderr output, the same
        # class CR10-12 (privacy.py) already fixed once. The basename is
        # enough to identify which directory was never scanned.
        raise NotScanned(f"no comprehension run has ever been published under {root.name!r}")
    scan_id = _resolve_run_id(run_id, index_doc)
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
        # FIX ROUND 22 (eighteenth cold read, F5 MAJOR, completeness): a
        # reparse-point boundary concealing a real source tree was
        # recorded in scan.json's own bounded `boundaries` list but
        # never surfaced here - status previously gave no hint at all
        # that a boundary might be hiding unscanned source. The TRUE
        # total (the bounded list's own length plus whatever this run's
        # own cap omitted) - status's own read-cost tier (scan.json
        # only, no other artifact) makes a bare count the right size
        # here; `report --json` carries the full bounded list.
        "boundary_count": (
            len(_scan_field(scan_doc, "boundaries", scan_id))
            + _scan_field(scan_doc, "boundaries_omitted_count", scan_id)
        ),
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
        "artifact_integrity_hint": STATUS_ARTIFACT_INTEGRITY_HINT,
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
        raise NotScanned(f"no comprehension run has ever been published under {root.name!r}")
    scan_id = _resolve_run_id(run_id, index_doc)
    records = _load_run_records(comprehension_dir, scan_id)
    anchor_state = _scan_json_anchor_state(index_doc, scan_id, records["scan"], records["run_dir"])
    _verify_artifact_digests(records["scan"], records["raw_docs"], records["run_dir"])
    # FIX ROUND 32 (twenty-eighth cold read, F4(b) MAJOR, completeness,
    # JUDGE - taken): `validate` catches a dangling cross-artifact
    # reference; `report` never checked at all - a run with one (a
    # producer bug, or a hand-edited artifact, that still passes schema/
    # envelope/digest verification) was projected and emitted with the
    # SAME dangling records `validate` would flag, `scan_json_integrity`
    # verified alongside them. Reuses the identical sweep `validate_run`
    # already runs (never a second, narrower notion of "referentially
    # sound" invented here) - the SAME M-1 precedent this function's own
    # digest check follows ("report verifies... before projecting it...
    # rather than projecting tampered content as fact") extends naturally
    # to this dimension too.
    _report_dangling = _dangling_reference_categories(
        modules=records["modules"], dependencies=records["dependencies"],
        entry_points=records["entry_points"], features=records["features"],
        readiness_signals=records["readiness_signals"],
    )
    if any(ids for _, ids in _report_dangling):
        raise ComprehensionError(
            "refusing to report: this run's own records contain a dangling "
            "cross-artifact reference - "
            f"{_dangling_reference_detail(_report_dangling)} "
            "(run `agenttalk comprehension validate` for the full detail)"
        )
    # FIX ROUND 32 (F4(a) MAJOR, completeness): the same path-confinement
    # sweep publish-time and `validate` now run - see _module_path_
    # confinement_violations's own docstring.
    _report_path_violations = _module_path_confinement_violations(records["modules"])
    if _report_path_violations:
        raise ComprehensionError(
            "refusing to report: this run's own module records contain an "
            "unconfined path - " + "; ".join(_report_path_violations) + " "
            "(run `agenttalk comprehension validate` for the full detail)"
        )
    payload = projector.project_comprehension(
        scan_id=_scan_field(records["scan"], "scan_id", scan_id),
        generated_at=_scan_field(records["scan"], "generated_at", scan_id),
        # F7 (eighth cold read): this passed None unconditionally - the
        # design's own invariant 4 ("readers bind to a scan ID AND
        # manifest digest") had no digest to bind to at all.
        #
        # FIX ROUND 17 (thirteenth cold read, CR13-5 MAJOR, wrong-data):
        # F7 wired scan.json's own `content_digest` field
        # (run_content_digest over this run's artifact_summaries) here -
        # but that digest is GENERATION-INDEPENDENT by design (two
        # separate scans of identical, unchanged sources legitimately
        # produce the SAME content_digest), defeating the field's own
        # contracted purpose: binding a reader to ONE CONCRETE
        # generation. scan.json's own on-disk byte_sha256 (already
        # computed above by ``_scan_json_anchor_state``/
        # ``_verify_artifact_digests``, recomputed here rather than
        # threaded through their return shapes) IS generation-specific -
        # every republish of this exact scan_id (even over byte-
        # identical source) writes a fresh `generated_at`/run_id into
        # scan.json, so its own byte digest differs run over run.
        manifest_digest=digests.sha256_file(records["run_dir"] / "scan.json"),
        status=_scan_field(records["scan"], "status", scan_id),
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
    # Round 11c (reviewer-3 delta on round 11b): a DECLARED, deliberate
    # exclusion (e.g. a pom's profile-scoped dependency) must be visible
    # in the projection, not just the manifest (scan.json) - layered the
    # same way, straight off the already-verified loaded document.
    payload["exclusions"] = _scan_field(records["scan"], "exclusions", scan_id)
    # FIX ROUND 20 (sixteenth cold read, m2 JUDGE, completeness): a
    # code-bearing excluded root with no src ancestry (so F4 never
    # degrades) and no pom-declared <module> path (so THE REACTOR RULE
    # never fires) still stays fully silent to a report --json caller -
    # the poison rule protects import RESOLUTION (an affected edge
    # correctly reports unresolved rather than a confident external
    # guess) but never surfaces WHICH directories were excluded in the
    # first place, leaving a caller with no way to independently judge
    # whether one of them might hold real, first-party source. Rather
    # than widen F4's own deliberately narrow degradation (a real
    # comprehension failure over source this run actually walked),
    # this declares the boundary honestly: scan.json already computes
    # and bounds excluded_roots (M4/round 6, Minor 7/round 7) - layered
    # onto the projection the same way exclusions already is (round
    # 11c), never recomputed here.
    payload["excluded_roots"] = _scan_field(records["scan"], "excluded_roots", scan_id)
    payload["excluded_roots_omitted_count"] = _scan_field(
        records["scan"], "excluded_roots_omitted_count", scan_id)
    # FIX ROUND 22 (eighteenth cold read, F5 MAJOR, completeness): a
    # reparse-point boundary concealing a real source tree is recorded
    # in scan.json's own `boundaries` (M4/round 6) but neither
    # `report --json` NOR `status --json` ever surfaced it - a consumer
    # saw complete/0 problems/exclusions that quietly omit the entire
    # skipped subtree, with no way to independently judge whether a
    # boundary might be hiding real, unscanned source (R-15a requires
    # report expose indexed/excluded scope + caps/truncation). Layered
    # onto the projection the same way `excluded_roots` already is
    # (round 20's own m2), never recomputed here.
    payload["boundaries"] = _scan_field(records["scan"], "boundaries", scan_id)
    payload["boundaries_omitted_count"] = _scan_field(
        records["scan"], "boundaries_omitted_count", scan_id)
    return payload


#: FIX ROUND 28 (F4): the SAME per-artifact section-key formula
#: ``run_scan`` uses to compute ``record_counts`` at publish time
#: (~945 above) - kept here as the one other place that formula must
#: stay in lock-step, since a verifier recomputing it independently
#: (rather than importing a shared function) would silently drift the
#: moment either side changed without the other.
_RECORD_COUNT_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "modules.json": ("units",),
    "dependencies.json": ("edges",),
    "features.json": ("entry_points", "features"),
    "readiness.json": ("signals", "summaries"),
    "problems.json": ("problems",),
}


def _actual_record_count(name: str, doc: dict[str, Any]) -> int:
    return sum(len(doc.get(key) or []) for key in _RECORD_COUNT_SECTION_KEYS[name])


def _dangling_reference_categories(
    *,
    modules: list[modules_artifact.ModuleRecord],
    dependencies: list[dependencies_artifact.DependencyRecord],
    entry_points: list[features_artifact.EntryPointRecord],
    features: list[features_artifact.FeatureRecord],
    readiness_signals: list[readiness_artifact.ReadinessSignal],
) -> tuple[tuple[str, list[str]], ...]:
    """FIX ROUND 29 (twenty-fifth cold read, F2 MAJOR, wrong-data): the
    ONE cross-artifact reference sweep both ``validate_run`` (below, on
    records read back from disk) and ``run_scan`` itself (publish time,
    on the SAME record types still in memory, before anything is ever
    written to staging) now call - the identical "one predicate, two
    call sites" discipline MICRO-ROUND 28b's own pairing-predicate
    already established, so the two can never independently drift.

    Publish-time validation (design step 8) previously had NO
    referential-integrity pass at all - a producer bug minting a
    dangling reference would publish a run that reports clean/complete
    forever, discoverable only if a caller happened to also run
    ``validate`` afterward. Sharing this function means the SAME bug
    that would make ``validate`` report invalid now REFUSES to publish
    in the first place.

    Started as round 28's own five categories (edge.from_unit_id,
    entry_point.owning_unit_id/declared_in_unit_id, feature.unit_ids/
    entry_point_ids, readiness signals[].unit_id, module.
    container_unit_id) - widened here by THREE more the reader measured
    unswept (the reader re-signed the digest chain and got ``valid:
    true`` + ``report`` exit 0 with a fabricated ``target_unit_id``):
    ``dependencies[].target_unit_id`` (a ``resolved`` edge naming no
    real unit), ``dependencies[].candidate_unit_ids`` (an ``ambiguous``
    edge's own candidate set), and ``entry_points[].feature_ids`` (an
    entry point naming a feature that does not exist). Generalized as a
    list of (label, ids) categories rather than an enumerated
    combinatorial branch - the same discipline round 28's own version
    of this function already established, now with three more members
    a future reference needs no new branch to join, only one more
    list entry."""
    unit_ids = {m.unit_id for m in modules}
    entry_point_ids = {e.entry_point_id for e in entry_points}
    feature_ids = {f.feature_id for f in features}
    dangling_edges = [e.edge_id for e in dependencies if e.from_unit_id not in unit_ids]
    dangling_entry_points = [
        e.entry_point_id for e in entry_points if e.owning_unit_id not in unit_ids]
    dangling_declared_in = [
        e.entry_point_id for e in entry_points
        if e.declared_in_unit_id and e.declared_in_unit_id not in unit_ids
    ]
    dangling_feature_unit_refs = [
        f.feature_id for f in features if any(u not in unit_ids for u in f.unit_ids)]
    dangling_feature_entry_point_refs = [
        f.feature_id for f in features
        if any(ep not in entry_point_ids for ep in f.entry_point_ids)
    ]
    dangling_signals = [s.signal_id for s in readiness_signals if s.unit_id not in unit_ids]
    dangling_containers = [
        m.unit_id for m in modules
        if m.container_unit_id is not None and m.container_unit_id not in unit_ids
    ]
    # FIX ROUND 29 (F2): the three newly-covered families.
    dangling_target_unit_ids = [
        e.edge_id for e in dependencies
        if e.resolution_state == "resolved" and e.target_unit_id is not None
        and e.target_unit_id not in unit_ids
    ]
    dangling_candidate_unit_ids = [
        e.edge_id for e in dependencies
        if e.resolution_state == "ambiguous"
        and any(c not in unit_ids for c in e.candidate_unit_ids)
    ]
    dangling_entry_point_feature_ids = [
        e.entry_point_id for e in entry_points
        if any(fid not in feature_ids for fid in e.feature_ids)
    ]
    return (
        ("edge(s) reference an unknown from_unit_id", dangling_edges),
        ("entry point(s) reference an unknown owning_unit_id", dangling_entry_points),
        ("entry point(s) reference an unknown declared_in_unit_id", dangling_declared_in),
        ("feature(s) reference an unknown unit_id", dangling_feature_unit_refs),
        ("feature(s) reference an unknown entry_point_id", dangling_feature_entry_point_refs),
        ("readiness signal(s) reference an unknown unit_id", dangling_signals),
        ("module(s) reference an unknown container_unit_id", dangling_containers),
        ("edge(s) reference an unknown target_unit_id", dangling_target_unit_ids),
        ("edge(s) reference an unknown candidate_unit_id", dangling_candidate_unit_ids),
        ("entry point(s) reference an unknown feature_id", dangling_entry_point_feature_ids),
    )


def _dangling_reference_detail(
    categories: tuple[tuple[str, list[str]], ...],
) -> str | None:
    """``None`` when every category is clean; otherwise one joined
    sentence naming every non-empty category and its count - shared by
    both of this function's own callers so the wording can never drift
    between them."""
    return " and ".join(
        f"{len(ids)} {label}" for label, ids in categories if ids
    ) or None


def _module_path_confinement_violations(
    modules: list[modules_artifact.ModuleRecord],
) -> list[str]:
    """FIX ROUND 32 (twenty-eighth cold read, F4 MAJOR, completeness):
    neither ``validate`` nor publish-time ever checked that a module's own
    persisted ``paths``/``source_digests`` entries are still confined,
    project-relative POSIX paths (design, "Local storage model") - a
    corrupted (or maliciously edited) ``modules.json`` naming an absolute
    path (``C:/Windows/win.ini``) reported ``valid:true`` from ``validate``
    and was projected and emitted verbatim by ``report``, neither ever
    having a reason to distrust a record whose digest/schema/cross-
    reference checks all otherwise pass - this dimension was simply never
    checked at all, by anything, after publication.

    Reuses ``envelope.validate_relative_path`` - the SAME syntactic
    confinement predicate every OTHER persisted path in this package is
    already held to - never a second, parallel notion of "confined"
    invented here. Shares this ONE predicate across publish-time (``run_
    scan``, refusing before anything reaches staging) and ``validate_run``
    (below, on records read back from disk) exactly the way ``_dangling_
    reference_categories`` already shares its own sweep between the two -
    the same "one predicate, every call site" discipline, so the two can
    never independently drift on what "confined" means.

    Returns one description per violating (unit_id, path) pair - a module
    can name more than one escaping path across ``paths`` and ``source_
    digests``, and every one is worth surfacing, not just the first."""
    violations: list[str] = []
    for module in modules:
        for path in module.paths:
            try:
                validate_relative_path(path, label=f"module {module.unit_id}'s own path")
            except EnvelopeError as exc:
                violations.append(str(exc))
        for path in module.source_digests:
            try:
                validate_relative_path(
                    path, label=f"module {module.unit_id}'s own source_digests key")
            except EnvelopeError as exc:
                violations.append(str(exc))
    return violations


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
    that class of byte-level-only difference.

    FIX ROUND 28 (twenty-fourth cold read, F4, completeness): this
    verified byte_sha256/content_digest but never the declared
    ``record_count`` itself - a scan.json falsifying that one field
    (e.g. understating modules.json's true unit count) passed every
    check here even though the design's own ceiling/summary machinery
    treats it as load-bearing. Recomputed from the SAME per-artifact
    section-key formula ``run_scan`` itself uses to compute
    ``record_counts`` at publish time (this module, ~945), applied to
    the document actually on disk rather than trusted from scan.json."""
    declared_artifacts = scan_doc.get("artifacts")
    if not declared_artifacts:
        raise ComprehensionError("scan.json is missing its artifacts digest summary")
    # N4 (seventh cold read, fix round 11 - defense in depth): this loop
    # only ever verifies what scan.json ITSELF declares - an artifact
    # that was LOADED (and is feeding status/report/validate's own
    # output) but silently absent from a tampered/truncated declared
    # list would never reach a digest check at all. Asserting the
    # loaded and declared NAME SETS match exactly closes that gap: a
    # loaded-but-undeclared artifact is caught here, before its content
    # is ever trusted, rather than only if its digest ALSO happens to be
    # declared (and then checked) elsewhere.
    declared_names = {
        require_field(entry, "name", doc_name="scan.json's artifacts entry")
        for entry in declared_artifacts
    }
    if declared_names != set(raw_docs):
        undeclared = sorted(set(raw_docs) - declared_names)
        unloaded = sorted(declared_names - set(raw_docs))
        raise ComprehensionError(
            "scan.json's declared artifacts do not match what this run actually loaded - "
            f"loaded but undeclared: {undeclared or 'none'}; declared but never loaded: "
            f"{unloaded or 'none'}"
        )
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
            # FIX ROUND 21 (seventeenth cold read, CR17-8 MINOR, the
            # class-closer): same fix as scan.json's own verification
            # above.
            raise ComprehensionError(
                bounded_os_error_detail(f"{name}'s bytes could not be read for verification", exc)
            ) from exc
        if actual_byte_sha256 != require_field(entry, "byte_sha256", doc_name=entry_label):
            raise ComprehensionError(
                f"{name}'s byte_sha256 does not match its declared value in scan.json")
        require_field(entry, "artifact_type", doc_name=entry_label)
        require_field(entry, "schema_version", doc_name=entry_label)
        declared_record_count = require_field(entry, "record_count", doc_name=entry_label)
        actual_record_count = _actual_record_count(name, doc)
        if actual_record_count != declared_record_count:
            raise ComprehensionError(
                f"{name}'s record_count ({declared_record_count}) does not match the "
                f"{actual_record_count} record(s) actually present in its own sections"
            )
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
        raise NotScanned(f"no comprehension run has ever been published under {root.name!r}")
    # FIX ROUND 30 (twenty-sixth cold read, F5 polish, wrong-data): an
    # empty or whitespace-only --run is a CALLER-level malformed-argument
    # error - the SAME class get_status/get_report already raise
    # directly for (exit 2 command error), never validate's own
    # "report a run's own problems via valid:False, don't raise" data
    # contract (round 29's own F7 fix wrongly folded this one shape into
    # that contract alongside a WELL-FORMED-but-nonexistent run_id, a
    # genuinely different case that correctly stays inside the try/
    # except below, unchanged). ``_resolve_run_id`` runs here now,
    # unconditionally, BEFORE the try block - matching get_status/
    # get_report exactly for THIS one shape.
    scan_id = _resolve_run_id(run_id, index_doc)
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
        # MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R3 note): this
        # sentence still named only the ORIGINAL digest-era checks even
        # after round 28's own F4 fix added record-count verification
        # (_verify_artifact_digests, above) and the widened cross-
        # artifact reference sweep (dangling_edges/dangling_entry_points/
        # dangling_declared_in/dangling_feature_*/dangling_signals/
        # dangling_containers, below) - the SAME artifact_integrity_hint
        # discipline (declare what a mechanism actually covers, don't
        # leave a caller to assume the OLD, narrower scope) applied to
        # validate's own success detail, not just status's pointer at it.
        detail = (
            "all artifacts verified: schema, envelope identity, scan_id consistency, "
            "per-artifact/run-level content digests, declared record counts against "
            "actual on-disk records, and cross-artifact unit/entry-point/feature/"
            "signal reference integrity"
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
    # FIX ROUND 29 (twenty-fifth cold read, F2 MAJOR): the sweep itself
    # now lives in _dangling_reference_categories, shared with publish-
    # time (run_scan, before anything is ever written to staging) - see
    # its own docstring. An empty-records fallback (every category
    # empty) when validation failed before records ever loaded, the
    # same "nothing to check yet" shape this function's own try/except
    # above already handles for `valid`.
    _dangling_categories = _dangling_reference_categories(
        modules=records["modules"] if records else [],
        dependencies=records["dependencies"] if records else [],
        entry_points=records["entry_points"] if records else [],
        features=records["features"] if records else [],
        readiness_signals=records["readiness_signals"] if records else [],
    )
    # FIX ROUND 32 (twenty-eighth cold read, F4(a) MAJOR, completeness):
    # the same sweep publish-time now runs (see _module_path_confinement_
    # violations's own docstring) - folded in here alongside the dangling-
    # reference sweep so a single `invalid` boolean and detail sentence
    # cover both integrity dimensions, never a second silent gap the
    # reader would have to separately discover.
    _path_violations = _module_path_confinement_violations(
        records["modules"] if records else [])
    dangling_detail = _dangling_reference_detail(_dangling_categories)
    invalid = any(ids for _, ids in _dangling_categories) or bool(_path_violations)
    invalid_detail = " and ".join(
        part for part in (dangling_detail, "; ".join(_path_violations) or None) if part
    )
    return {
        "scan_id": scan_id,
        "valid": valid and not invalid,
        "detail": detail if not invalid else invalid_detail,
        "scan_json_integrity": anchor_state,
        "external_revalidation": {
            "performed": False,
            "reason_code": "no_external_evidence_pointers_this_slice",
        },
    }
