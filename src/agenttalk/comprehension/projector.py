"""The ONE pure projection function (DESIGN-55-comprehension-plane.md,
"Contract for the migration-program UI (#208)": "A server-side
comprehension projector validates one run and emits a bounded, versioned
`GET /api/comprehension` response. The same projector backs `comprehension
report --json`, preventing CLI/UI semantic drift.")

``project_comprehension`` is the single-projector-parity anchor named in
the approved PR-B plan (C-2): PR-D's future GET-only route imports and
calls THIS function with query-derived filters, exactly as item 9's
``report --json`` CLI command will with argv-derived filters. Nothing
about this module is CLI-specific or web-specific - it is a pure function
from already-validated run records to one bounded JSON-shaped dict.

Freshness is NOT implemented this slice (PR-C's job - "whole-scope
current/stale/unknown" needs the freshness pass this producer slice does
not build). The projection's ``freshness`` field is present but always
reports ``not_evaluated`` with a named reason code, never a silently
invented ``current``/``stale`` guess - core invariant 5 applies here too.
"""

from __future__ import annotations

from typing import Any

from .dependencies_artifact import DependencyRecord
from .errors import InvalidReadinessStateFilter
from .features_artifact import EntryPointRecord, FeatureRecord
from .modules_artifact import ModuleRecord
from .readiness_artifact import ASSESSMENT_STATES, ReadinessSignal, UnitReadinessSummary

PROJECTION_SCHEMA_VERSION = 1
_MAX_ROWS_PER_SECTION = 1000

#: N6 (seventh cold read, fix round 11): these six sections are computed
#: over the UNFILTERED, whole-run sets even when unit_id/feature_id/
#: readiness_state narrows the actually-returned rows elsewhere in the
#: SAME payload - "counts" said so itself (its own "scope" field), but a
#: --unit/--feature/--readiness caller inspecting one of the OTHER five
#: sections directly had no visible indication, right there, that it is
#: exempt from their filter. Named explicitly at the payload's own top
#: level, outside "counts", so any one of the six is self-describing
#: without a caller needing to already know to check a sibling section.
WHOLE_RUN_SECTIONS = (
    "counts", "dependency_summary", "high_fan_out_units", "high_fan_in_units",
    "units_without_feature", "units_with_unknown_feature_linkage", "unmapped_entry_points",
)

#: FIX ROUND 17 (thirteenth cold read, CR13-6 MINOR, JUDGE - taken): CR10-11's
#: own finding (readiness_artifact.ASSESSMENT_STATE_CAVEAT) - three of
#: ASSESSMENT_STATES's four values are structurally unreachable this
#: slice. A ``--readiness`` filter naming one of them used to return a
#: silently EMPTY units/readiness section - indistinguishable from "no
#: unit happens to be in this state right now" (a real, meaningful
#: answer for a reachable state) versus "this state cannot occur at all
#: this slice" (a structural fact about the policy, not about this run's
#: data). Named here so ``project_comprehension`` can add a visible note
#: rather than leave a caller to independently rediscover CR10-11.
_STRUCTURALLY_UNREACHABLE_ASSESSMENT_STATES = frozenset({"assessed", "blocked", "not_applicable"})


def _readiness_state_filter_note(readiness_state: str | None) -> dict[str, Any]:
    """FIX ROUND 17 (CR13-6 MINOR, JUDGE - taken): an EMPTY dict (never a
    key with a ``None``/empty value) when the filter is absent or names a
    reachable state - the same absent-not-null idiom every other optional
    field in this artifact family already follows. Spread into the
    payload at the call site.

    FIX ROUND 30 (twenty-sixth cold read, F6 note, JUDGE - taken): a
    ``--readiness <valid, reachable state>`` that simply matches ZERO
    units this run emits NO note at all - asymmetric with ``--unit``/
    ``--feature``, which always get one on a zero-match result (see
    ``_unit_or_feature_filter_note``). Declared as a deliberate
    difference, not a silent gap: ``--unit``/``--feature`` name an OPEN,
    UNVALIDATED per-run id space (round 18b's own ruling) - a typo and a
    genuine zero-match are indistinguishable without a note. ``--
    readiness`` is validated up front against a CLOSED, closed-and-
    reachable-known vocabulary (``InvalidReadinessStateFilter`` already
    refuses anything else, including the three structurally-unreachable
    states this function itself names above) - a caller reaching this
    point already knows their state was recognized, so a zero-match
    result is ordinary, unambiguous "no unit is currently in that state"
    - the same disambiguation a note would add for an open id space is
    not needed for a validated, closed one."""
    if readiness_state in _STRUCTURALLY_UNREACHABLE_ASSESSMENT_STATES:
        return {
            "readiness_state_filter_note": (
                f"'{readiness_state}' cannot occur for any unit this slice (see "
                "readiness_artifact.ASSESSMENT_STATE_CAVEAT) - an empty result here means "
                "the state is structurally unreachable, not that no unit currently matches it"
            ),
        }
    return {}


def _unit_or_feature_filter_note(
    unit_id: str | None, feature_id: str | None, filtered_modules: list[ModuleRecord],
    *, unit_id_exists: bool = True, feature_id_exists: bool = True,
    narrowed_to_zero_by_readiness: bool = False,
) -> dict[str, Any]:
    """FIX ROUND 23 (nineteenth cold read, F10, completeness - retires the
    round 18/18b carry ("`--unit` naming a nonexistent id") BY MECHANISM
    rather than leaving it a declared-and-answered note only): round 18b
    ruled correctly that `--unit`/`--feature` name an OPEN per-run id
    space with no closed vocabulary to validate against, so no REFUSAL
    belongs here - that ruling stands, unchanged. What it left unbuilt
    was a visible signal distinguishing "this id genuinely matched
    nothing" from "I forgot to pass the filter", beyond a caller having
    to actively cross-reference the bare `filters` echo (CR13-9) against
    an empty `units` list themselves. Same absent-not-null idiom
    ``_readiness_state_filter_note`` already established - present only
    when the filter narrowed the run to zero units, silent (empty dict)
    otherwise.

    FIX ROUND 30 (twenty-sixth cold read, F4 polish, wrong-data): the
    note's own wording used to claim "an id that does not exist this
    run" UNCONDITIONALLY - false when BOTH `unit_id` and `feature_id`
    are given, both individually name something real this run, but the
    unit simply is not part of that feature (a healthy, DISJOINT-filter
    empty result, not a nonexistent-id one). ``unit_id_exists``/
    ``feature_id_exists`` (each defaulting True - a caller passing only
    one filter never needs the other's existence checked) let the
    caller distinguish the two before wording the note; both are
    "healthy empty," only the REASON differs.

    M4 (cold-read, PR-B fix round 47): round 30's own fix still assumed
    only TWO possible causes for an empty result (nonexistent id, or a
    disjoint unit/feature pair) - it never accounted for a THIRD active
    filter, --readiness, also narrowing the SAME `filtered_modules` this
    note inspects. Measured: `--unit <real id>` with no `--feature`
    (so the "DISJOINT" branch cannot even fire) plus a `--readiness`
    that happens to exclude that unit produced "an id that does not
    exist this run" - false, the id exists, --readiness is what emptied
    it. Symmetrically, `--unit <real id> --feature <real id>` (unit
    genuinely IN that feature) plus an excluding `--readiness` produced
    "DISJOINT" - also false, they are not disjoint at all.
    ``narrowed_to_zero_by_readiness`` (True only when unit_id/feature_id
    ALONE would already have matched at least one module, but the run's
    own final `filtered_modules` is empty once --readiness is applied
    too) lets this note name the ACTUAL cause instead of guessing from
    unit_id_exists/feature_id_exists alone - the same "detail proves
    cause" discipline every other problem/note in this package already
    follows: assert a cause only when the code that emits the note can
    actually prove it produced this empty result."""
    if (unit_id is not None or feature_id is not None) and not filtered_modules:
        parts = []
        if unit_id is not None:
            parts.append(f"unit_id={unit_id!r}")
        if feature_id is not None:
            parts.append(f"feature_id={feature_id!r}")
        if narrowed_to_zero_by_readiness:
            reason = (
                "unit_id/feature_id matched at least one unit this run, but the additional "
                "--readiness filter then narrowed that down to zero - not a nonexistent id, "
                "and not necessarily a disjoint unit/feature combination either"
            )
        elif unit_id is not None and feature_id is not None and unit_id_exists and feature_id_exists:
            reason = (
                "unit_id and feature_id BOTH name something real this run, but they are "
                "DISJOINT - the named unit is not part of the named feature (see round "
                "18b: --unit/--feature name an open per-run id space, never a closed "
                "vocabulary to refuse against)"
            )
        else:
            reason = (
                "a healthy empty result for an id that does not exist this run (see round "
                "18b: --unit/--feature name an open per-run id space, never a closed "
                "vocabulary to refuse against)"
            )
        return {
            "unit_or_feature_filter_note": (
                f"no unit matched the requested {' and '.join(parts)} - {reason}, not a "
                "sign the filter silently did nothing"
            ),
        }
    return {}


def _bounded(records: list[Any], cap: int | None = None) -> tuple[list[Any], int]:
    # cap is read from the module global at CALL time (never a def-time
    # default) so a test can monkeypatch _MAX_ROWS_PER_SECTION and see it
    # take effect.
    effective_cap = _MAX_ROWS_PER_SECTION if cap is None else cap
    if len(records) <= effective_cap:
        return records, 0
    return records[:effective_cap], len(records) - effective_cap


#: FIX ROUND 22 (eighteenth cold read, F2 MAJOR, wrong-data): a "route"
#: edge's own ``target_external`` is a URL PATTERN (``java.py`` publishes
#: it unconditionally ``resolved`` via the generic fallback branch every
#: OTHER unrecognized ``target_kind`` also falls through to) - the
#: design's own field contract names ``target_external`` as "a
#: normalized external package/system name" (Artifact 2); a URL pattern
#: is categorically never one, the identical bare-superset argument
#: round 21b already accepted for ``entry_points_by_kind``. A
#: dependency-free 7-route controller published external:7 plus a
#: high_fan_out_units entry - route edges are entry-point SURFACE,
#: already fully counted there (features.json/entry_points_by_kind);
#: publishing them AGAIN as if they were structural dependencies double-
#: counts the identical fact under the wrong heading. Every OTHER
#: relation's own external resolution genuinely fits "package/system
#: name" (import/inherit/invoke/build/test all resolve to a real type
#: or coordinate name, even when unrecognized) - "route" is excluded
#: BY NAME, not via a narrower inclusion list, so a future relation
#: this producer might add is counted here by default unless it is
#: PROVEN to share route's own not-a-package-name defect.
_NON_DEPENDENCY_RELATIONS = frozenset({"route"})


def _dependency_summary(dependencies: list[DependencyRecord]) -> dict[str, int]:
    # NAMED LIMIT (declared, PR-B round 46, F3 - judged, not chased): this
    # summary's own `routes`/`routes_by_kind` are UNSEGMENTED by owning-
    # unit classification - a route/filter served by a test-classified
    # unit counts identically alongside a production one. See
    # readiness_artifact.PROVENANCE_CAVEAT's own item 6 for the full
    # declaration and why segmenting it is a real new join (this function
    # never receives `modules` at all), not a cheap addition here.
    # FIX ROUND 21 (seventeenth cold read, CR17-6 MINOR): round 20c's own
    # per-edge externality_suppressed marker distinguishes "this producer
    # ABSTAINED from a positive external claim because this run's own
    # external surface is unknown" from "this producer found a real,
    # unresolved dependency" - but this summary folded both into the
    # SAME bare `unresolved` count, so a #208 consumer could not tell
    # four abstentions apart from four genuine dependency problems.
    # `unresolved` itself is UNCHANGED (still the full superset count,
    # never renamed or split, so nothing reading it today silently sees
    # a different number) - `externality_suppressed` is a NEW, separate
    # subset count a caller can subtract out for a "real problems only"
    # view.
    summary = {
        "internal": 0, "external": 0, "unresolved": 0, "ambiguous": 0,
        "externality_suppressed": 0,
        # FIX ROUND 22 (F2 MAJOR): a route's own count, visible on its
        # own rather than silently vanishing from a previously-nonzero
        # external count - DECIDED (reviewer-3 ratifies): kept as its
        # own separate field here rather than moving the URL pattern off
        # target_external entirely, which would need sweeping every
        # existing consumer of that field for a fix this narrow does not
        # need - the actual defect (mis-bucketing) is fully closed
        # without it.
        "routes": 0,
    }
    # FIX ROUND 29 (twenty-fifth cold read, F4 MAJOR, completeness):
    # `routes` above counts EVERY route-relation edge as one bucket -
    # both a served route AND an intercepting filter (relation itself
    # stays "route" for both, by micro-round 27b's own ruling) - while
    # the SAME payload's own `entry_points_by_kind` already separates
    # `http_route` from `http_filter`, and `ENTRY_POINT_KINDS`'s own
    # "never counted as served" sentence makes that distinction load-
    # bearing. A pre-aggregated integer had nothing to join back to it.
    # `routes` itself stays UNCHANGED (the same "never redefine a
    # published field, add a new one" discipline `externality_
    # suppressed` above already follows) - `routes_by_kind` is the new,
    # additive dict, keyed by the IDENTICAL `http_route`/`http_filter`
    # vocabulary `entry_points_by_kind` already uses, so a caller can
    # join the two directly by key.
    routes_by_kind: dict[str, int] = {}
    for edge in dependencies:
        if edge.relation in _NON_DEPENDENCY_RELATIONS:
            summary["routes"] += 1
            if edge.route_kind is not None:
                routes_by_kind[edge.route_kind] = routes_by_kind.get(edge.route_kind, 0) + 1
            continue
        if edge.resolution_state == "resolved" and edge.target_unit_id is not None:
            summary["internal"] += 1
        elif edge.resolution_state == "resolved" and edge.target_external is not None:
            summary["external"] += 1
        elif edge.resolution_state == "ambiguous":
            summary["ambiguous"] += 1
        else:
            summary["unresolved"] += 1
            if edge.externality_suppressed:
                summary["externality_suppressed"] += 1
    summary["routes_by_kind"] = dict(sorted(routes_by_kind.items()))
    return summary


def _entry_points_by_kind(entry_points: list[EntryPointRecord]) -> dict[str, int]:
    """FIX ROUND 21b (reviewer-3's re-delta, THE MAJOR's own counted-
    means question, taken): ``counts.entry_points`` is a single bare
    total across every kind - harmless while every kind genuinely served
    a request, but ``http_filter`` (this round's own new kind - a filter
    INTERCEPTS, it does not serve) now shares that same total, so "how
    many entry points does this app have" no longer answers "how many
    does it SERVE" on its own. ``counts.entry_points`` itself is
    UNCHANGED (still the full superset count, the same discipline
    ``_dependency_summary``'s own ``unresolved`` already follows) - this
    is a new, separate per-kind breakdown a caller can read instead of
    guessing from the bare total.

    NAMED LIMIT (declared, PR-B round 46, F3 - judged, not chased): this
    breakdown is also UNSEGMENTED by owning-unit classification - see
    ``_dependency_summary``'s own identical note and readiness_artifact.
    PROVENANCE_CAVEAT's own item 6."""
    by_kind: dict[str, int] = {}
    for entry_point in entry_points:
        by_kind[entry_point.kind] = by_kind.get(entry_point.kind, 0) + 1
    return dict(sorted(by_kind.items()))


def _units_by_feature_linked_status(
    readiness_signals: list[ReadinessSignal],
) -> tuple[list[str], list[str]]:
    """FIX ROUND 28 (twenty-fourth cold read, F1 MAJOR, wrong-data):
    ``units_without_feature`` used to RECOMPUTE its own feature-linkage
    from ``features.json`` + containment alone (the retired ``_feature_
    unit_ids_including_owning_files`` this function replaces) - entirely
    blind to round 27's own two new mechanisms readiness_artifact.py's
    own ``feature_linked`` check consults (``declared_in_unit_id``
    credit; the whole-file evidence-gap map). A web.xml whose declared
    route published (readiness correctly reports ``unknown``/
    ``feature_not_confirmed``) still appeared in ``units_without_
    feature`` as a CONFIDENT negative, the exact contradiction round
    22's own F1 fix closed for the file/component split, reopened here
    for a different divergence; likewise every unit whose readiness
    reports ``unknown``/``adapter_encoding_undecodable`` (or any other
    whole-file-gap reason) - a run that RECORDED it could not read a
    file should never assert a confident "no feature" about it either.
    Fixed AS THE CLASS, not the instance: ``feature_linked`` in
    readiness.json is the single source of truth for feature linkage -
    the projection now DERIVES from it directly rather than
    recomputing an independent answer that can silently diverge.

    Returns ``(confident_negative_unit_ids, unknown_unit_ids)`` - every
    unit's own ``feature_linked`` signal is exactly one of ``satisfied``/
    ``unknown``/``unsatisfied`` (this check has no ``not_applicable``
    branch), so these two lists partition every unit whose feature
    linkage is NOT satisfied, never silently dropping the unknown ones
    the way lumping them into the confident-negative list would."""
    confident_negative: list[str] = []
    unknown: list[str] = []
    for signal in readiness_signals:
        if signal.check != "feature_linked":
            continue
        if signal.stored_status == "unsatisfied":
            confident_negative.append(signal.unit_id)
        elif signal.stored_status == "unknown":
            unknown.append(signal.unit_id)
    return confident_negative, unknown


def _fan_counts(dependencies: list[DependencyRecord]) -> tuple[dict[str, int], dict[str, int]]:
    # FIX ROUND 22 (eighteenth cold read, F2 MAJOR, wrong-data): a route
    # edge is entry-point surface, not a structural dependency - see
    # _NON_DEPENDENCY_RELATIONS's own docstring above. A dependency-free
    # controller with N routes published high_fan_out_units naming it,
    # over N facts already fully visible in features.json/entry_point_
    # kinds.
    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = {}
    for edge in dependencies:
        if edge.relation in _NON_DEPENDENCY_RELATIONS:
            continue
        fan_out[edge.from_unit_id] = fan_out.get(edge.from_unit_id, 0) + 1
        if edge.target_unit_id is not None:
            fan_in[edge.target_unit_id] = fan_in.get(edge.target_unit_id, 0) + 1
    return fan_out, fan_in


#: M3 (sixth cold read, fix round 10): this slice has no external-pointer
#: revalidation pass at all - readiness.json's own stored_assessment_state
#: is scan-time-only (design: "readiness.json and its manifest digest
#: cover only stored_status and stored_assessment_state at scan time").
#: The honest projection value is revalidated_status=unknown with a
#: NAMED reason, never a guessed current/stale/confirmed.
_REVALIDATION_NOT_IMPLEMENTED_REASON = "external_revalidation_not_implemented_this_slice"


def _assessment_state(summary: UnitReadinessSummary) -> str:
    """The design's unprefixed, projection-level ``assessment_state`` -
    "always derived from revalidated statuses... never wins a conflict
    with a more conservative revalidated result" (design, "Evidence
    pointers and trust"). This slice has no revalidation pass to
    diverge from, so it equals the stored value - stated explicitly,
    here, in the ONE place both the projected field and the
    ``--readiness``/``readiness_state`` filter read it from, rather than
    left for either to assume the equivalence independently."""
    return summary.stored_assessment_state


def _project_readiness_summary(summary: UnitReadinessSummary) -> dict[str, Any]:
    """MAJOR 3 (sixth cold read, fix round 10): the projection omitted
    every revalidated-status field and the unprefixed ``assessment_state``
    the design NAMES as part of the #208 contract - ``report --json``
    carried only ``stored_assessment_state``, a missing-key surprise for
    any design-shaped consumer. Adds them at the PROJECTION layer only
    (never persisted into readiness.json itself, which stores scan-time
    values exclusively).

    FIX ROUND 35 (twenty-ninth cold read, F6 LOW, declare): this trio is
    published on the unit SUMMARY payload only, never threaded onto an
    individual ``ReadinessSignal`` row alongside its own ``stored_status``
    - see ``readiness_artifact.PROVENANCE_CAVEAT``'s own item 5 for why
    this is a declared scope, not an oversight."""
    payload = summary.to_json()
    payload["revalidated_status"] = "unknown"
    payload["revalidated_at"] = None
    payload["revalidation_reason"] = _REVALIDATION_NOT_IMPLEMENTED_REASON
    payload["assessment_state"] = _assessment_state(summary)
    return payload


def project_comprehension(
    *,
    scan_id: str,
    generated_at: str,
    manifest_digest: str | None,
    status: str,
    modules: list[ModuleRecord],
    dependencies: list[DependencyRecord],
    entry_points: list[EntryPointRecord],
    features: list[FeatureRecord],
    readiness_signals: list[ReadinessSignal],
    readiness_summaries: list[UnitReadinessSummary],
    problems: list[dict[str, Any]] | None = None,
    unit_id: str | None = None,
    feature_id: str | None = None,
    readiness_state: str | None = None,
    dependencies_only: bool = False,
) -> dict[str, Any]:
    """Every filter parameter mirrors a CLI/API filter 1:1 (design:
    ``--unit ID``, ``--feature ID``, ``--readiness STATE``,
    ``--dependencies``) - this function does not know which caller is
    asking."""
    problems = problems or []
    # F8 (eighth cold read): an unrecognized readiness_state used to
    # silently filter every row away rather than being refused as the
    # caller mistake it is - a closed vocabulary already exists
    # (readiness_artifact.ASSESSMENT_STATES); validated once, here, for
    # every caller (CLI and the future API route alike).
    if readiness_state is not None and readiness_state not in ASSESSMENT_STATES:
        raise InvalidReadinessStateFilter(readiness_state, ASSESSMENT_STATES)

    # FIX ROUND 15 (eleventh cold read, F2 MAJOR, wrong-data): computed
    # once, shared - report --feature <id> published the design's own
    # worked example (`report --feature checkout --dependencies`) while
    # actually returning the WHOLE RUN's dependencies and readiness
    # sections unfiltered. FIX ROUND 15b (reviewer-3's own correction):
    # this comment previously claimed WHOLE_RUN_SECTIONS itself names
    # units/features/entry_points as whole-run - it does not; that
    # constant names counts/dependency_summary/high_fan_out_units/
    # high_fan_in_units/units_without_feature/unmapped_entry_points
    # only. units/features/entry_points/dependencies/readiness are all
    # ordinary FILTERED sections by omission from that list - the actual
    # bug was that dependencies/readiness were not filtered like their
    # siblings, not that some other field claimed they shouldn't be. An
    # unmatched feature_id yields an EMPTY set here (no feature's
    # unit_ids contribute), so every section scoped by it below
    # correctly narrows to nothing rather than silently falling back to
    # "everything".
    feature_unit_ids: set[str] | None = None
    if feature_id is not None:
        feature_unit_ids = {
            u for f in features if f.feature_id == feature_id for u in f.unit_ids
        }

    filtered_modules = modules
    if unit_id is not None:
        filtered_modules = [m for m in filtered_modules if m.unit_id == unit_id]
    if feature_unit_ids is not None:
        filtered_modules = [m for m in filtered_modules if m.unit_id in feature_unit_ids]

    filtered_dependencies = dependencies
    if unit_id is not None:
        filtered_dependencies = [
            e for e in filtered_dependencies
            if e.from_unit_id == unit_id or e.target_unit_id == unit_id
        ]
    if feature_unit_ids is not None:
        filtered_dependencies = [
            e for e in filtered_dependencies
            if e.from_unit_id in feature_unit_ids or e.target_unit_id in feature_unit_ids
        ]

    filtered_features = features
    if feature_id is not None:
        filtered_features = [f for f in filtered_features if f.feature_id == feature_id]
    # FIX ROUND 15b (reviewer-3's MINOR 1 - same defect F2 fixed, on the
    # sibling filter): --unit left features/entry_points WHOLE-RUN -
    # measured: `--unit BillingEngine --feature other` returned 0 units,
    # 0 deps, yet 1 entry point + 1 feature. Both record types carry an
    # owning unit id already; narrowed the same way --feature already
    # narrows them.
    if unit_id is not None:
        filtered_features = [f for f in filtered_features if unit_id in f.unit_ids]

    filtered_entry_points = entry_points
    if feature_id is not None:
        filtered_entry_points = [
            e for e in filtered_entry_points if feature_id in e.feature_ids
        ]
    if unit_id is not None:
        filtered_entry_points = [e for e in filtered_entry_points if e.owning_unit_id == unit_id]

    filtered_summaries = readiness_summaries
    filtered_signals = readiness_signals
    # FIX ROUND 15 (eleventh cold read, F2 MAJOR, wrong-data): same gap
    # as dependencies above - readiness signals/summaries never narrowed
    # by --feature either.
    if feature_unit_ids is not None:
        filtered_summaries = [s for s in filtered_summaries if s.unit_id in feature_unit_ids]
        filtered_signals = [s for s in filtered_signals if s.unit_id in feature_unit_ids]
    # M4 (cold-read, PR-B fix round 47): captured BEFORE --readiness
    # narrows filtered_modules any further, below - lets
    # _unit_or_feature_filter_note tell "unit_id/feature_id themselves
    # matched nothing" (this snapshot is already empty) apart from
    # "unit_id/feature_id matched something, but --readiness then
    # narrowed it to zero" (this snapshot is non-empty, the final
    # filtered_modules is not) - the SAME distinction round 30's own F4
    # fix drew between a nonexistent id and a disjoint unit/feature pair,
    # now extended to a third cause the note previously could not see.
    _modules_before_readiness_filter = filtered_modules
    if readiness_state is not None:
        # M3 (sixth cold read, fix round 10): filters on the design's
        # projection-level assessment_state (this slice: equal to
        # stored_assessment_state, since no revalidation pass exists yet
        # to diverge from it) - the SAME field/value _project_readiness_
        # summary below publishes, via the one shared helper.
        filtered_summaries = [
            s for s in filtered_summaries if _assessment_state(s) == readiness_state
        ]
        allowed_unit_ids = {s.unit_id for s in filtered_summaries}
        filtered_signals = [s for s in filtered_signals if s.unit_id in allowed_unit_ids]
        # F8 (eighth cold read): --unit/--feature both narrow "units"
        # already - --readiness never did, even though
        # whole_run_sections (self-describing which sections stay whole-run)
        # implies "units" IS one of the filtered ones. Same allowed-set the
        # signals/summaries above already narrow to.
        filtered_modules = [m for m in filtered_modules if m.unit_id in allowed_unit_ids]
        # FIX ROUND 16 (twelfth cold read, M3 MAJOR, wrong-data): the
        # THIRD instance of the same sibling-filter defect (F2, then
        # MINOR 1 on --unit) - dependencies/features/entry_points never
        # narrowed by --readiness either, even though none of the three
        # is a whole_run_sections member. Same allowed-set units/signals/
        # summaries above already narrow to.
        filtered_dependencies = [
            e for e in filtered_dependencies
            if e.from_unit_id in allowed_unit_ids
            or (e.target_unit_id is not None and e.target_unit_id in allowed_unit_ids)
        ]
        filtered_features = [
            f for f in filtered_features if any(u in allowed_unit_ids for u in f.unit_ids)
        ]
        filtered_entry_points = [
            e for e in filtered_entry_points if e.owning_unit_id in allowed_unit_ids
        ]
    if unit_id is not None:
        filtered_signals = [s for s in filtered_signals if s.unit_id == unit_id]
        filtered_summaries = [s for s in filtered_summaries if s.unit_id == unit_id]

    # FIX ROUND 16 (twelfth cold read, M3 MAJOR, wrong-data): problems.json
    # rows carry no unit_id of their own (only an optional path/
    # qualified_name) - the FOURTH sibling section, never narrowed by ANY
    # of the three filters until now, even though "problems" is not a
    # whole_run_sections member either. Joined against whichever units
    # survived every OTHER active filter above (filtered_modules, by now
    # reflecting the AND of --unit/--feature/--readiness) via the two
    # identifying fields a problem row can carry. A problem with NEITHER
    # field (a scan-wide refusal, attributable to no single unit) is kept
    # in every filtered view - excluding it would be a false, silent
    # exclusion, never a safe under-claim the way keeping it is.
    filtered_problems = problems
    if unit_id is not None or feature_id is not None or readiness_state is not None:
        allowed_paths = {p for m in filtered_modules for p in m.paths}
        allowed_qualified_names = {
            m.qualified_name for m in filtered_modules if m.qualified_name is not None
        }
        filtered_problems = [
            p for p in filtered_problems
            if (p.get("path") is None and p.get("qualified_name") is None)
            or p.get("path") in allowed_paths
            or p.get("qualified_name") in allowed_qualified_names
        ]

    units_without_feature, units_with_unknown_feature_linkage = _units_by_feature_linked_status(
        readiness_signals)
    unmapped_entry_points = [e.entry_point_id for e in entry_points if not e.feature_ids]

    fan_out, fan_in = _fan_counts(dependencies)
    high_fan_out = sorted(
        ({"unit_id": u, "count": c} for u, c in fan_out.items() if c > 5),
        key=lambda row: (-row["count"], row["unit_id"]),
    )
    high_fan_in = sorted(
        ({"unit_id": u, "count": c} for u, c in fan_in.items() if c > 5),
        key=lambda row: (-row["count"], row["unit_id"]),
    )

    units_rows, units_omitted = _bounded([m.to_json() for m in filtered_modules])
    dependency_rows, dependency_omitted = _bounded([e.to_json() for e in filtered_dependencies])
    problem_rows, problem_omitted = _bounded(filtered_problems)
    # M10 (cold-read, PR-B fix round 3): features/entry_points/readiness
    # signals+summaries had no row cap and no truncation/omitted count at
    # all - unbounded on a large repo (the reviewer measured readiness
    # alone as 6 signals per unit, ~60k rows on a 5k-file repo,
    # `truncated: false` regardless). Bounded the same way every other
    # section already is.
    feature_rows, feature_omitted = _bounded([f.to_json() for f in filtered_features])
    entry_point_rows, entry_point_omitted = _bounded(
        [e.to_json() for e in filtered_entry_points])
    readiness_signal_rows, readiness_signal_omitted = _bounded(
        [s.to_json() for s in filtered_signals])
    readiness_summary_rows, readiness_summary_omitted = _bounded(
        [_project_readiness_summary(s) for s in filtered_summaries])
    # M-4 (second cold read, fix round 4): round 3's own bounding fix
    # enumerated the four sections it fixed rather than asserting a
    # payload-wide invariant - these two, both plain lists of ids rather
    # than lists of row dicts, slipped through uncapped (reproduced:
    # units_without_feature returned 2401 entries on a 1200-file repo,
    # scaling 1:1 with unit count).
    units_without_feature_rows, units_without_feature_omitted = _bounded(
        sorted(units_without_feature))
    # FIX ROUND 28 (twenty-fourth cold read, F1 MAJOR): a unit whose own
    # feature_linked signal is `unknown` (a whole-file evidence gap, or
    # any other undecided reason) must NOT vanish - it is neither a
    # confident negative (units_without_feature, above) nor linked
    # (satisfied, omitted from both) - a separate bounded list + an
    # unbounded true count (below, in "counts"), so a caller can see
    # this population exists even if the row list itself truncates.
    units_with_unknown_feature_linkage_rows, units_with_unknown_feature_linkage_omitted = _bounded(
        sorted(units_with_unknown_feature_linkage))
    unmapped_entry_points_rows, unmapped_entry_points_omitted = _bounded(
        sorted(unmapped_entry_points))
    # M2 (fourth cold read, fix round 6): these two were hard-sliced to a
    # literal [:20] - not routed through _bounded at all, so they got no
    # omitted_counts entry and never set truncated, even when far more
    # than 20 units actually qualified. Same enumeration-vs-invariant
    # lesson as M-4/round 4 and round 3's own M10: a fixed slice bypasses
    # the SAME mechanism every other section already goes through.
    high_fan_out_rows, high_fan_out_omitted = _bounded(high_fan_out)
    high_fan_in_rows, high_fan_in_omitted = _bounded(high_fan_in)

    payload: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "scan_id": scan_id,
        "generated_at": generated_at,
        "manifest_digest": manifest_digest,
        "status": status,
        "freshness": {
            "state": "not_evaluated",
            "reason_code": "freshness_not_implemented_this_slice",
        },
        # FIX ROUND 23 (nineteenth cold read, F7, completeness): report
        # --json had no `cycles` field at all and no declared absence -
        # freshness/external-revalidation/assessment-states/features
        # all declare theirs (the design names cycles/hotspots/impact
        # summaries as report/UI-derived, "Artifact 2"'s own text) - a
        # caller had no way to tell "not computed this slice" apart from
        # "computed and genuinely empty." The SAME declared-absence
        # shape `freshness` already uses (CR13-6 discipline) - cycle
        # detection is NOT implemented this slice, declared rather than
        # silently omitted.
        "cycles": {
            "state": "not_evaluated",
            "reason_code": "cycles_not_implemented_this_slice",
        },
        "whole_run_sections": list(WHOLE_RUN_SECTIONS),
        # FIX ROUND 17 (thirteenth cold read, CR13-9 MINOR): the applied
        # filters, echoed verbatim - a caller getting zero rows back
        # (e.g. --unit naming an id that matches nothing, a legitimate
        # query result, never refused - see the design's own filter
        # contract) had no way to tell "this unit is genuinely absent/
        # stale" apart from "I forgot to pass the filter at all" without
        # comparing against its OWN request out-of-band. Present
        # unconditionally (never omitted), unlike the OTHER optional
        # fields in this artifact family - a caller needs a filters key
        # to exist even on an UNFILTERED response, to positively confirm
        # nothing was silently applied.
        "filters": {
            "unit_id": unit_id, "feature_id": feature_id, "readiness_state": readiness_state,
            "dependencies_only": dependencies_only,
        },
        **_readiness_state_filter_note(readiness_state),
        **_unit_or_feature_filter_note(
            unit_id, feature_id, filtered_modules,
            unit_id_exists=any(m.unit_id == unit_id for m in modules),
            feature_id_exists=any(f.feature_id == feature_id for f in features),
            narrowed_to_zero_by_readiness=(
                readiness_state is not None
                and bool(_modules_before_readiness_filter)
                and not filtered_modules
            ),
        ),
        "counts": {
            # M10 (cold-read, PR-B fix round 3): these are WHOLE-RUN
            # totals - deliberately unaffected by unit_id/feature_id/
            # readiness_state row-level filters, since dependency_summary/
            # high_fan_*/units_without_feature below are cross-cutting
            # aggregate statistics describing the ENTIRE scan, not just
            # whatever rows this particular filtered response returns.
            # "scope" makes that explicit rather than leaving a caller to
            # infer it from a mismatch against the returned row counts.
            "scope": "whole_run",
            "units": len(modules),
            "dependencies": len(dependencies),
            "features": len(features),
            "entry_points": len(entry_points),
            "entry_points_by_kind": _entry_points_by_kind(entry_points),
            "readiness_signals": len(readiness_signals),
            "problems": len(problems),
            # FIX ROUND 28 (twenty-fourth cold read, F1 MAJOR): the TRUE,
            # unbounded count - never hidden behind the bounded row
            # list's own possible truncation below.
            "units_with_unknown_feature_linkage": len(units_with_unknown_feature_linkage),
        },
        "dependency_summary": _dependency_summary(dependencies),
        "high_fan_out_units": high_fan_out_rows,
        "high_fan_in_units": high_fan_in_rows,
        "units_without_feature": units_without_feature_rows,
        "units_with_unknown_feature_linkage": units_with_unknown_feature_linkage_rows,
        "unmapped_entry_points": unmapped_entry_points_rows,
        "problems": problem_rows,
        "truncated": bool(
            units_omitted or dependency_omitted or problem_omitted
            or feature_omitted or entry_point_omitted
            or readiness_signal_omitted or readiness_summary_omitted
            or units_without_feature_omitted or units_with_unknown_feature_linkage_omitted
            or unmapped_entry_points_omitted
            or high_fan_out_omitted or high_fan_in_omitted
        ),
        "omitted_counts": {
            "units": units_omitted, "dependencies": dependency_omitted, "problems": problem_omitted,
            "features": feature_omitted, "entry_points": entry_point_omitted,
            "readiness_signals": readiness_signal_omitted,
            "readiness_summaries": readiness_summary_omitted,
            "units_without_feature": units_without_feature_omitted,
            "units_with_unknown_feature_linkage": units_with_unknown_feature_linkage_omitted,
            "unmapped_entry_points": unmapped_entry_points_omitted,
            "high_fan_out_units": high_fan_out_omitted,
            "high_fan_in_units": high_fan_in_omitted,
        },
    }

    if not dependencies_only:
        payload["units"] = units_rows
        payload["features"] = feature_rows
        payload["entry_points"] = entry_point_rows
        payload["readiness"] = {
            "signals": readiness_signal_rows,
            "summaries": readiness_summary_rows,
        }
    payload["dependencies"] = dependency_rows

    return payload
