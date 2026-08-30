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
    "units_without_feature", "unmapped_entry_points",
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
    payload at the call site."""
    if readiness_state in _STRUCTURALLY_UNREACHABLE_ASSESSMENT_STATES:
        return {
            "readiness_state_filter_note": (
                f"'{readiness_state}' cannot occur for any unit this slice (see "
                "readiness_artifact.ASSESSMENT_STATE_CAVEAT) - an empty result here means "
                "the state is structurally unreachable, not that no unit currently matches it"
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


def _dependency_summary(dependencies: list[DependencyRecord]) -> dict[str, int]:
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
    }
    for edge in dependencies:
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
    return summary


def _fan_counts(dependencies: list[DependencyRecord]) -> tuple[dict[str, int], dict[str, int]]:
    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = {}
    for edge in dependencies:
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
    values exclusively)."""
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

    all_feature_unit_ids = {u for f in features for u in f.unit_ids}
    units_without_feature = [m.unit_id for m in modules if m.unit_id not in all_feature_unit_ids]
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
            "readiness_signals": len(readiness_signals),
            "problems": len(problems),
        },
        "dependency_summary": _dependency_summary(dependencies),
        "high_fan_out_units": high_fan_out_rows,
        "high_fan_in_units": high_fan_in_rows,
        "units_without_feature": units_without_feature_rows,
        "unmapped_entry_points": unmapped_entry_points_rows,
        "problems": problem_rows,
        "truncated": bool(
            units_omitted or dependency_omitted or problem_omitted
            or feature_omitted or entry_point_omitted
            or readiness_signal_omitted or readiness_summary_omitted
            or units_without_feature_omitted or unmapped_entry_points_omitted
            or high_fan_out_omitted or high_fan_in_omitted
        ),
        "omitted_counts": {
            "units": units_omitted, "dependencies": dependency_omitted, "problems": problem_omitted,
            "features": feature_omitted, "entry_points": entry_point_omitted,
            "readiness_signals": readiness_signal_omitted,
            "readiness_summaries": readiness_summary_omitted,
            "units_without_feature": units_without_feature_omitted,
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
