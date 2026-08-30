"""#55 slice-1 PR-B item 8: the ONE pure projection function
(DESIGN-55-comprehension-plane.md, "Contract for the migration-program UI
(#208)"). Single-projector parity: this same function will back both
`report --json` (item 9) and PR-D's future GET route - tested here purely
as a function of already-assembled records, with no CLI/HTTP involved.
"""

from __future__ import annotations

import pytest

from agenttalk.comprehension import projector as pr
from agenttalk.comprehension.errors import InvalidReadinessStateFilter
from agenttalk.comprehension.dependencies_artifact import DependencyRecord
from agenttalk.comprehension.features_artifact import EntryPointRecord, FeatureRecord
from agenttalk.comprehension.modules_artifact import ModuleRecord
from agenttalk.comprehension.readiness_artifact import ReadinessSignal, UnitReadinessSummary


def _unit(unit_id: str) -> ModuleRecord:
    return ModuleRecord(
        unit_id=unit_id, kind="component", display_name=unit_id, language="java",
        paths=[f"{unit_id}.java"], source_digests={}, classification=["production"],
        container_unit_id=None, producers=[],
    )


def _edge(
    edge_id: str, from_unit_id: str, *, resolution_state: str, target_unit_id=None,
    target_external=None, externality_suppressed: bool = False, relation: str = "invoke",
):
    return DependencyRecord(
        edge_id=edge_id, from_unit_id=from_unit_id, relation=relation, phase="runtime",
        optional=False, evidence_class="extracted", resolution_state=resolution_state,
        target_unit_id=target_unit_id, target_external=target_external,
        externality_suppressed=externality_suppressed,
    )


def _feature(feature_id: str, unit_ids: list[str], state: str = "candidate") -> FeatureRecord:
    return FeatureRecord(
        feature_id=feature_id, label=feature_id, state=state, origin="detected",
        unit_ids=unit_ids, entry_point_ids=[],
    )


def _entry_point(
    entry_point_id: str, owning_unit_id: str, feature_ids: list[str], *, kind: str = "http_route",
) -> EntryPointRecord:
    return EntryPointRecord(
        entry_point_id=entry_point_id, kind=kind, name="/x",
        owning_unit_id=owning_unit_id, feature_ids=feature_ids, evidence_class="declared",
    )


def _summary(unit_id: str, state: str) -> UnitReadinessSummary:
    return UnitReadinessSummary(unit_id=unit_id, stored_assessment_state=state)


def _signal(unit_id: str) -> ReadinessSignal:
    return ReadinessSignal(
        signal_id=f"sig-{unit_id}", unit_id=unit_id, check="source_understood",
        stored_status="satisfied", severity="blocker", basis="detected", reason_code="ok",
    )


def _base_kwargs(**overrides):
    kwargs = {
        "scan_id": "scan-1", "generated_at": "2026-08-27T00:00:00Z",
        "manifest_digest": "deadbeef", "status": "complete", "modules": [],
        "dependencies": [], "entry_points": [], "features": [],
        "readiness_signals": [], "readiness_summaries": [],
    }
    kwargs.update(overrides)
    return kwargs


# ----------------------------------------------------------- basic shape

def test_counts_and_freshness_stub():
    payload = pr.project_comprehension(**_base_kwargs(modules=[_unit("u1"), _unit("u2")]))
    assert payload["counts"]["units"] == 2
    assert payload["freshness"] == {
        "state": "not_evaluated", "reason_code": "freshness_not_implemented_this_slice",
    }
    # FIX ROUND 23 (nineteenth cold read, F7, completeness): the SAME
    # declared-absence shape as freshness - cycle detection is not
    # implemented this slice, declared rather than silently omitted.
    assert payload["cycles"] == {
        "state": "not_evaluated", "reason_code": "cycles_not_implemented_this_slice",
    }
    assert payload["schema_version"] == pr.PROJECTION_SCHEMA_VERSION


def test_entry_points_by_kind_breaks_down_the_bare_total_by_kind():
    """FIX ROUND 21b (reviewer-3's re-delta, THE MAJOR's own counted-
    means question): counts.entry_points is a single bare total across
    every kind - http_filter (round 21b's own new kind - a filter
    intercepts, it does not serve) sharing that total means "how many
    entry points" no longer answers "how many are SERVED" on its own.
    counts.entry_points itself stays the unchanged full superset (never
    renamed) - entry_points_by_kind is the new, separate breakdown a
    caller reads instead of guessing from the bare total."""
    entry_points = [
        _entry_point("ep-route", "u1", [], kind="http_route"),
        _entry_point("ep-filter-1", "u1", [], kind="http_filter"),
        _entry_point("ep-filter-2", "u2", [], kind="http_filter"),
        _entry_point("ep-cli", "u3", [], kind="cli_main"),
    ]
    payload = pr.project_comprehension(**_base_kwargs(entry_points=entry_points))
    assert payload["counts"]["entry_points"] == 4
    assert payload["counts"]["entry_points_by_kind"] == {
        "cli_main": 1, "http_filter": 2, "http_route": 1,
    }


# ----------------------------------------------------------- filters

def test_unit_id_filter_narrows_units_and_dependencies():
    edges = [
        _edge("e1", "u1", resolution_state="resolved", target_unit_id="u2"),
        _edge("e2", "u3", resolution_state="resolved", target_unit_id="u4"),
    ]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u3")], dependencies=edges, unit_id="u1"))
    assert [u["unit_id"] for u in payload["units"]] == ["u1"]
    assert [d["edge_id"] for d in payload["dependencies"]] == ["e1"]


def test_unit_id_filter_narrows_features_and_entry_points_too():
    """FIX ROUND 15b (reviewer-3's MINOR 1 - same defect F2 fixed, on the
    sibling filter): --unit narrowed units/dependencies but left
    features/entry_points WHOLE-RUN - measured: `--unit BillingEngine
    --feature other` returned 0 units, 0 deps, yet 1 entry point + 1
    feature. Both record types carry an owning unit id already."""
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [_entry_point("ep1", "u1", ["f1"]), _entry_point("ep2", "u2", ["f2"])]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], features=features,
        entry_points=entry_points, unit_id="u1"))
    assert [f["feature_id"] for f in payload["features"]] == ["f1"]
    assert [e["entry_point_id"] for e in payload["entry_points"]] == ["ep1"]


def test_unit_id_and_unrelated_feature_id_together_yield_empty_features_and_entry_points():
    """FIX ROUND 15b: the reviewer's own exact measurement - a --unit
    selector combined with an UNRELATED --feature selector must yield
    empty features/entry_points too (the unit belongs to neither the
    unrelated feature nor any feature at all here), never a silent
    whole-run fallback for either."""
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [_entry_point("ep1", "u1", ["f1"]), _entry_point("ep2", "u2", ["f2"])]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], features=features,
        entry_points=entry_points, unit_id="u1", feature_id="f2"))
    assert payload["units"] == []
    assert payload["features"] == []
    assert payload["entry_points"] == []


def test_feature_id_filter_narrows_units_features_and_entry_points():
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [_entry_point("ep1", "u1", ["f1"]), _entry_point("ep2", "u2", ["f2"])]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], features=features,
        entry_points=entry_points, feature_id="f1"))
    assert [u["unit_id"] for u in payload["units"]] == ["u1"]
    assert [f["feature_id"] for f in payload["features"]] == ["f1"]
    assert [e["entry_point_id"] for e in payload["entry_points"]] == ["ep1"]


def test_feature_id_filter_narrows_dependencies_and_readiness_too():
    """FIX ROUND 15 (eleventh cold read, F2 MAJOR, wrong-data): --feature
    narrowed units/features/entry_points but published the WHOLE RUN's
    dependencies and readiness sections unfiltered, contradicting
    whole_run_sections's own claim that only those three stay whole-run
    (the design's own worked example is `report --feature checkout
    --dependencies`). Scoped by the feature's member unit set: an edge
    touching EITHER a member unit (u1) or an unrelated one (u3/u4) - only
    the member-touching edge survives; readiness rows for the
    unrelated unit (u2) are dropped too."""
    features = [_feature("f1", ["u1"])]
    edges = [
        _edge("e1", "u1", resolution_state="resolved", target_unit_id="u2"),
        _edge("e2", "u3", resolution_state="resolved", target_unit_id="u4"),
    ]
    summaries = [_summary("u1", "assessed"), _summary("u2", "assessed")]
    signals = [_signal("u1"), _signal("u2")]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2"), _unit("u3"), _unit("u4")],
        features=features, dependencies=edges,
        readiness_signals=signals, readiness_summaries=summaries, feature_id="f1"))
    assert [d["edge_id"] for d in payload["dependencies"]] == ["e1"]
    assert [s["unit_id"] for s in payload["readiness"]["summaries"]] == ["u1"]
    assert [s["unit_id"] for s in payload["readiness"]["signals"]] == ["u1"]


def test_a_nonexistent_feature_id_yields_empty_scoped_sections():
    """FIX ROUND 15 (eleventh cold read, F2 MAJOR, wrong-data): even a
    feature_id that matches NO real feature returned every dependency
    and readiness row run-wide - the projection asserted a scoping it
    never actually performed. An unmatched selector now yields EMPTY
    scoped sections, never a silent fallback to "everything"."""
    features = [_feature("f1", ["u1"])]
    edges = [_edge("e1", "u1", resolution_state="resolved", target_unit_id="u2")]
    summaries = [_summary("u1", "assessed")]
    signals = [_signal("u1")]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1")], features=features, dependencies=edges,
        readiness_signals=signals, readiness_summaries=summaries,
        feature_id="does-not-exist"))
    assert payload["units"] == []
    assert payload["dependencies"] == []
    assert payload["readiness"]["summaries"] == []
    assert payload["readiness"]["signals"] == []
    # FIX ROUND 17 (thirteenth cold read, CR13-9 MINOR): zero rows for a
    # stale/nonexistent selector must be distinguishable from zero rows
    # genuinely returned with no filter applied at all - the echoed
    # filters key makes the applied feature_id visible right here.
    assert payload["filters"]["feature_id"] == "does-not-exist"


def test_filters_key_echoes_every_applied_filter_verbatim():
    """FIX ROUND 17 (thirteenth cold read, CR13-9 MINOR): report --json
    must echo its applied filters so a caller can positively confirm
    what was asked for, rather than inferring it out-of-band by
    comparing against their own request."""
    payload = pr.project_comprehension(**_base_kwargs(
        unit_id="u1", feature_id="f1", readiness_state="needs_evidence",
        dependencies_only=True,
    ))
    assert payload["filters"] == {
        "unit_id": "u1", "feature_id": "f1", "readiness_state": "needs_evidence",
        "dependencies_only": True,
    }


def test_filters_key_is_present_and_all_null_with_no_filter_applied():
    """Companion: an UNFILTERED response still carries the filters key
    (never omitted, unlike this artifact family's usual absent-not-null
    idiom) - a caller needs the key to exist even here, to positively
    confirm nothing was silently applied."""
    payload = pr.project_comprehension(**_base_kwargs())
    assert payload["filters"] == {
        "unit_id": None, "feature_id": None, "readiness_state": None,
        "dependencies_only": False,
    }


def test_readiness_state_filter_narrows_signals_and_summaries():
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    signals = [_signal("u1"), _signal("u2")]
    payload = pr.project_comprehension(**_base_kwargs(
        readiness_signals=signals, readiness_summaries=summaries, readiness_state="blocked"))
    assert [s["unit_id"] for s in payload["readiness"]["summaries"]] == ["u1"]
    assert [s["unit_id"] for s in payload["readiness"]["signals"]] == ["u1"]


def test_readiness_state_filter_also_narrows_units_f8():
    """FIX ROUND 12 (eighth cold read, F8): --unit/--feature both narrow
    "units" already - --readiness never did, even though
    whole_run_sections's own self-description implies "units" is one of
    the filtered sections."""
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], readiness_summaries=summaries,
        readiness_state="blocked"))
    assert [u["unit_id"] for u in payload["units"]] == ["u1"]


def test_readiness_state_filter_narrows_dependencies_features_entry_points_and_problems_too():
    """FIX ROUND 16 (twelfth cold read, M3 MAJOR, wrong-data): the THIRD
    instance of the same sibling-filter defect (F2, then MINOR 1 on
    --unit) - dependencies/features/entry_points, and now problems too
    (a FOURTH section, never narrowed by ANY filter until now, joined by
    path/qualified_name since a problem row carries no unit_id of its
    own), never narrowed by --readiness even though none of the four is
    a whole_run_sections member."""
    modules = [_unit("u1"), _unit("u2")]
    edges = [
        _edge("e1", "u1", resolution_state="resolved", target_unit_id="u2"),
        _edge("e2", "u2", resolution_state="resolved", target_unit_id="u2"),
    ]
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [_entry_point("ep1", "u1", []), _entry_point("ep2", "u2", [])]
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    problems = [
        {"reason_code": "parse_failed", "path": "u1.java", "detail": "x"},
        {"reason_code": "parse_failed", "path": "u2.java", "detail": "y"},
        {"reason_code": "case_collision", "path": None, "detail": "z"},
    ]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=modules, dependencies=edges, features=features, entry_points=entry_points,
        readiness_summaries=summaries, problems=problems, readiness_state="blocked",
    ))
    assert [e["edge_id"] for e in payload["dependencies"]] == ["e1"]
    assert [f["feature_id"] for f in payload["features"]] == ["f1"]
    assert [e["entry_point_id"] for e in payload["entry_points"]] == ["ep1"]
    assert {p["detail"] for p in payload["problems"]} == {"x", "z"}


def test_readiness_state_filter_rejects_an_unrecognized_state_f8():
    """FIX ROUND 12 (F8): an unrecognized --readiness value used to
    silently match nothing (empty rows, exit 0) - indistinguishable from
    "every unit was genuinely filtered out". Refused as the caller
    mistake it is, against the closed vocabulary."""
    with pytest.raises(InvalidReadinessStateFilter):
        pr.project_comprehension(**_base_kwargs(readiness_state="not-a-real-state"))


@pytest.mark.parametrize("state", ["assessed", "blocked", "not_applicable"])
def test_readiness_state_filter_on_a_structurally_unreachable_state_adds_a_note(state):
    """FIX ROUND 17 (thirteenth cold read, CR13-6 MINOR, JUDGE - taken):
    CR10-11's own finding - three of ASSESSMENT_STATES's four values are
    structurally unreachable this slice (a permanently-unknown
    boundaries_identified signal rules out assessed/not_applicable;
    source_understood never returning unsatisfied rules out blocked). A
    caller filtering on one of these used to get a silently EMPTY result,
    indistinguishable from "no unit happens to match" - now a visible
    note names the structural reason."""
    payload = pr.project_comprehension(**_base_kwargs(readiness_state=state))
    assert state in payload["readiness_state_filter_note"]
    assert payload["readiness"]["summaries"] == []


def test_readiness_state_filter_on_the_reachable_state_adds_no_note():
    """Companion negative case: needs_evidence is the one reachable
    state this slice - filtering on it must never carry the
    structurally-unreachable note."""
    payload = pr.project_comprehension(**_base_kwargs(
        readiness_summaries=[_summary("u1", "needs_evidence")], readiness_state="needs_evidence"))
    assert "readiness_state_filter_note" not in payload


def test_no_readiness_state_filter_adds_no_note():
    payload = pr.project_comprehension(**_base_kwargs())
    assert "readiness_state_filter_note" not in payload


@pytest.mark.parametrize("filter_kwargs", [
    {"unit_id": "u1"},
    {"feature_id": "f1"},
    {"readiness_state": "blocked"},
])
def test_every_filter_narrows_every_non_whole_run_section_the_same_way(filter_kwargs):
    """FIX ROUND 16 (twelfth cold read, M3 MAJOR - the class-closer): F2,
    MINOR 1, and now M3 were each a SEPARATE cold read discovering the
    identical shape - one more section quietly exempt from whichever
    filter a caller happened to test with. Parametrized across all three
    filters against the SAME fixture (u1 in scope, u2 not) so a FUTURE
    new filter (or a future new section) that regresses this class only
    has to fail ONE parametrized case here, never wait for a fourth cold
    read to notice by hand. whole_run_sections itself names the sections
    this deliberately does not check."""
    modules = [_unit("u1"), _unit("u2")]
    edges = [
        _edge("e1", "u1", resolution_state="resolved", target_unit_id="u2"),
        _edge("e2", "u2", resolution_state="resolved", target_unit_id="u2"),
    ]
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [
        _entry_point("ep1", "u1", ["f1"]), _entry_point("ep2", "u2", ["f2"]),
    ]
    signals = [_signal("u1"), _signal("u2")]
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    problems = [
        {"reason_code": "parse_failed", "path": "u1.java", "detail": "keep"},
        {"reason_code": "parse_failed", "path": "u2.java", "detail": "drop"},
    ]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=modules, dependencies=edges, features=features, entry_points=entry_points,
        readiness_signals=signals, readiness_summaries=summaries, problems=problems,
        **filter_kwargs,
    ))
    assert [u["unit_id"] for u in payload["units"]] == ["u1"]
    assert [e["edge_id"] for e in payload["dependencies"]] == ["e1"]
    assert [f["feature_id"] for f in payload["features"]] == ["f1"]
    assert [e["entry_point_id"] for e in payload["entry_points"]] == ["ep1"]
    assert [s["unit_id"] for s in payload["readiness"]["signals"]] == ["u1"]
    assert [s["unit_id"] for s in payload["readiness"]["summaries"]] == ["u1"]
    assert [p["detail"] for p in payload["problems"]] == ["keep"]


# ------------------------------ MAJOR 3 (sixth cold read, fix round 10):
# the #208 contract's stored/revalidated readiness field list
# (DESIGN-55-comprehension-plane.md, "Evidence pointers and trust").

def test_readiness_summary_row_carries_every_design_named_field():
    """Parity test against the design field list: `report --json`/the
    projection must emit stored_assessment_state, revalidated_status,
    revalidated_at, revalidation_reason, and the unprefixed
    assessment_state - not just the raw stored value readiness.json
    itself persists."""
    payload = pr.project_comprehension(**_base_kwargs(
        readiness_summaries=[_summary("u1", "assessed")]))
    row = payload["readiness"]["summaries"][0]
    assert row.keys() >= {
        "unit_id", "stored_assessment_state", "assessment_state",
        "revalidated_status", "revalidated_at", "revalidation_reason",
    }


def test_readiness_summary_assessment_state_equals_stored_this_slice():
    """This slice has no external-pointer revalidation pass, so the
    design's unprefixed assessment_state (design: "always derived from
    revalidated statuses... never wins a conflict with a more
    conservative revalidated result") equals the stored value - there is
    no revalidated result yet to diverge from."""
    payload = pr.project_comprehension(**_base_kwargs(
        readiness_summaries=[_summary("u1", "blocked")]))
    row = payload["readiness"]["summaries"][0]
    assert row["assessment_state"] == "blocked"
    assert row["stored_assessment_state"] == "blocked"


def test_readiness_summary_revalidated_status_is_honest_unknown_not_persisted():
    """revalidated_status/revalidated_at/revalidation_reason are honest
    unknowns with a NAMED reason (no guessed current/stale/confirmed) -
    and are a PROJECTION-only addition, never persisted into
    readiness.json's own stored summary shape (UnitReadinessSummary.
    to_json() is unchanged)."""
    summary = _summary("u1", "assessed")
    assert "revalidated_status" not in summary.to_json()
    payload = pr.project_comprehension(**_base_kwargs(readiness_summaries=[summary]))
    row = payload["readiness"]["summaries"][0]
    assert row["revalidated_status"] == "unknown"
    assert row["revalidated_at"] is None
    assert row["revalidation_reason"]


def test_readiness_state_filter_matches_the_projected_assessment_state_field():
    """M3: --readiness/readiness_state filters on assessment_state (this
    slice: equal to stored_assessment_state), the SAME field/value the
    projected row publishes - not a parallel, independently-derived
    check that could silently drift from it."""
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    payload = pr.project_comprehension(**_base_kwargs(
        readiness_summaries=summaries, readiness_state="blocked"))
    rows = payload["readiness"]["summaries"]
    assert [r["unit_id"] for r in rows] == ["u1"]
    assert all(r["assessment_state"] == "blocked" for r in rows)


def test_dependencies_only_omits_the_other_sections():
    edges = [_edge("e1", "u1", resolution_state="resolved", target_external="java.util.List")]
    payload = pr.project_comprehension(**_base_kwargs(dependencies=edges, dependencies_only=True))
    assert "units" not in payload
    assert "features" not in payload
    assert "entry_points" not in payload
    assert "readiness" not in payload
    assert payload["dependencies"][0]["edge_id"] == "e1"
    assert payload["dependency_summary"]["external"] == 1


# ----------------------------------------------------------- coverage-gap fields

def test_units_without_feature_lists_unlinked_units():
    features = [_feature("f1", ["u1"])]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], features=features))
    assert payload["units_without_feature"] == ["u2"]


def test_unmapped_entry_points_lists_entry_points_with_no_feature_link():
    entry_points = [_entry_point("ep1", "u1", [])]
    payload = pr.project_comprehension(**_base_kwargs(entry_points=entry_points))
    assert payload["unmapped_entry_points"] == ["ep1"]


# ----------------------------------------------------------- dependency summary / fan-out

def test_dependency_summary_categorizes_every_edge():
    edges = [
        _edge("e1", "u1", resolution_state="resolved", target_unit_id="u2"),
        _edge("e2", "u1", resolution_state="resolved", target_external="java.util.List"),
        _edge("e3", "u1", resolution_state="unresolved", target_unit_id=None),
        _edge("e4", "u1", resolution_state="ambiguous", target_unit_id=None),
    ]
    payload = pr.project_comprehension(**_base_kwargs(dependencies=edges))
    assert payload["dependency_summary"] == {
        "internal": 1, "external": 1, "unresolved": 1, "ambiguous": 1,
        "externality_suppressed": 0, "routes": 0,
    }


def test_dependency_summary_never_counts_route_edges_as_external_dependencies():
    """FIX ROUND 22 (eighteenth cold read, F2 MAJOR, wrong-data): the
    reader's own .cr18-fanout shape - a route edge's own target_external
    is a URL PATTERN, not the design's own "normalized external package/
    system name" - a dependency-free 7-route controller used to publish
    external:7, indistinguishable from 7 genuine external library
    dependencies. Routes now get their own separate, visible count
    instead of silently inflating (or vanishing from) external."""
    edges = [
        _edge(f"route-{i}", "u1", resolution_state="resolved",
              target_external=f"/api/{i}", relation="route")
        for i in range(7)
    ]
    payload = pr.project_comprehension(**_base_kwargs(dependencies=edges))
    assert payload["dependency_summary"] == {
        "internal": 0, "external": 0, "unresolved": 0, "ambiguous": 0,
        "externality_suppressed": 0, "routes": 7,
    }
    assert payload["high_fan_out_units"] == []


def test_dependency_summary_still_counts_a_real_external_import_alongside_routes():
    """A genuine external import in the SAME unit as several routes must
    still be counted - routes are excluded specifically, not every edge
    from a unit that happens to also have routes."""
    edges = [
        _edge("route-1", "u1", resolution_state="resolved",
              target_external="/api/orders", relation="route"),
        _edge("import-1", "u1", resolution_state="resolved",
              target_external="org.springframework.web.bind.annotation.RestController",
              relation="import"),
    ]
    payload = pr.project_comprehension(**_base_kwargs(dependencies=edges))
    assert payload["dependency_summary"]["external"] == 1
    assert payload["dependency_summary"]["routes"] == 1
    # Below the >5 high-fan-out threshold either way - just confirms the
    # route edge was excluded from fan_out without silently dropping the
    # real import edge too.
    assert payload["high_fan_out_units"] == []


def test_dependency_summary_carries_a_separate_externality_suppressed_count():
    """FIX ROUND 21 (seventeenth cold read, CR17-6 MINOR): round 20c's
    own per-edge externality_suppressed marker distinguishes an
    ABSTENTION (this run's own external surface is unknown) from a
    genuine unresolved dependency problem - folding both into the same
    bare `unresolved` count let four abstentions render as four
    dependency problems in a #208 consumer. `unresolved` itself stays
    the full superset count (unchanged); `externality_suppressed` is
    the new, separate subset a caller can subtract out."""
    edges = [
        _edge(
            "e1", "u1", resolution_state="unresolved", target_unit_id=None,
            externality_suppressed=True),
        _edge(
            "e2", "u1", resolution_state="unresolved", target_unit_id=None,
            externality_suppressed=True),
        _edge("e3", "u1", resolution_state="unresolved", target_unit_id=None),
    ]
    payload = pr.project_comprehension(**_base_kwargs(dependencies=edges))
    assert payload["dependency_summary"]["unresolved"] == 3
    assert payload["dependency_summary"]["externality_suppressed"] == 2


def test_high_fan_out_and_fan_in_units_are_reported():
    edges = [_edge(f"e{i}", "hub", resolution_state="resolved", target_unit_id="target") for i in range(6)]
    payload = pr.project_comprehension(**_base_kwargs(dependencies=edges))
    assert payload["high_fan_out_units"][0] == {"unit_id": "hub", "count": 6}
    assert payload["high_fan_in_units"][0] == {"unit_id": "target", "count": 6}


# ----------------------------------------------------------- truncation

def test_truncation_reports_omitted_counts(monkeypatch):
    monkeypatch.setattr(pr, "_MAX_ROWS_PER_SECTION", 1)
    payload = pr.project_comprehension(**_base_kwargs(modules=[_unit("u1"), _unit("u2")]))
    assert payload["truncated"] is True
    assert payload["omitted_counts"]["units"] == 1
    assert len(payload["units"]) == 1


def test_no_projection_list_anywhere_exceeds_the_row_cap(monkeypatch):
    """M-4 (second cold read, fix round 4): round 3's own M10 bounding fix
    enumerated the four sections it fixed rather than asserting a
    payload-wide invariant - which is exactly why units_without_feature
    and unmapped_entry_points (both plain lists of ids, not lists of row
    dicts, introduced earlier than M10) slipped through uncapped
    (reproduced: 2401 entries on a 1200-file repo, scaling 1:1 with unit
    count). Walks the ENTIRE payload recursively instead of naming
    sections: no list anywhere may exceed the cap, and every section
    whose real input exceeded it must report a nonzero omitted count.

    M2 (fourth cold read, fix round 6): this test's own fixture left
    high_fan_out_units/high_fan_in_units EMPTY (no unit's fan-out/fan-in
    ever exceeded the >5 threshold with only 2 edges) - an empty section
    produces no list to walk and a correctly-zero omitted count either
    way, so this invariant test COULD NOT have caught high_fan_out_units/
    high_fan_in_units bypassing _bounded entirely (hard-sliced to [:20],
    no omitted_counts entry, truncated never set). Every section is now
    populated well past the cap - including features, bumped from one
    item to two, the one section that previously sat AT the cap with a
    legitimately-zero omitted count, indistinguishable from "never
    populated" by omitted_counts alone - and a meta-assertion below
    checks every omitted_counts entry is nonzero, so a future fixture
    that forgets to populate ANY section (fan or otherwise) fails this
    test instead of silently passing over an empty one."""
    monkeypatch.setattr(pr, "_MAX_ROWS_PER_SECTION", 1)
    modules = [_unit("u1"), _unit("u2"), _unit("u3")]
    edges = [
        _edge("e1", "u1", resolution_state="resolved", target_unit_id="u2"),
        _edge("e2", "u1", resolution_state="resolved", target_unit_id="u3"),
    ]
    # Pushes u1 and u2 both past the fan-OUT threshold (>5), and u3/u4
    # both past the fan-IN threshold - two qualifying units per section,
    # so the (monkeypatched) cap of 1 actually clips one from each.
    fan_out_edges = [
        _edge(f"fo1-{i}", "u1", resolution_state="unresolved") for i in range(5)
    ] + [
        _edge(f"fo2-{i}", "u2", resolution_state="unresolved") for i in range(6)
    ]
    fan_in_edges = [
        _edge(f"fi1-{i}", f"src1-{i}", resolution_state="resolved", target_unit_id="u3")
        for i in range(6)
    ] + [
        _edge(f"fi2-{i}", f"src2-{i}", resolution_state="resolved", target_unit_id="u4")
        for i in range(6)
    ]
    edges = edges + fan_out_edges + fan_in_edges
    features = [_feature("f1", ["u1"]), _feature("f2", ["u1"])]  # u2, u3 have no feature link
    entry_points = [_entry_point("ep1", "u2", []), _entry_point("ep2", "u3", [])]  # both unmapped
    signals = [_signal("u1"), _signal("u2")]
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    problems = [
        {"reason_code": "parse_failed", "path": "a", "detail": "x"},
        {"reason_code": "parse_failed", "path": "b", "detail": "y"},
    ]

    payload = pr.project_comprehension(**_base_kwargs(
        modules=modules, dependencies=edges, features=features,
        entry_points=entry_points, readiness_signals=signals,
        readiness_summaries=summaries, problems=problems,
    ))

    def _assert_no_list_exceeds_cap(value: object, path: str) -> None:
        # N6 (seventh cold read, fix round 11): whole_run_sections is a
        # fixed enumeration of SECTION NAMES, not a row/data section
        # subject to growth with scan size - it is never routed through
        # _bounded and is exempt from this row-cap invariant on that
        # basis, the same way this walk already has to exempt anything
        # that is not row data.
        if path == "payload.whole_run_sections":
            return
        if isinstance(value, list):
            assert len(value) <= 1, f"{path}: list of {len(value)} exceeds the row cap"
            for i, item in enumerate(value):
                _assert_no_list_exceeds_cap(item, f"{path}[{i}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                _assert_no_list_exceeds_cap(item, f"{path}.{key}")

    _assert_no_list_exceeds_cap(payload, "payload")

    assert payload["truncated"] is True

    def _list_section_names(node: object, prefix: str, found: set) -> None:
        # M2 invariant, structural version (fifth cold read, fix round
        # 7): the reviewer DEMONSTRATED this test's own blind spot - a
        # hard-sliced, unregistered NEW section (a list with no
        # omitted_counts entry at all, exactly M2's own bug shape)
        # passed all twelve existing assertions here, because they only
        # ever checked the HAND-MAINTAINED omitted_counts dict against
        # itself, never against the payload's own shape. This walks the
        # payload instead: every list-valued key (joined with its parent
        # key for one level of nesting, e.g. readiness.signals ->
        # "readiness_signals", matching the flat omitted_counts naming)
        # must have a matching entry - an assertion ABOUT the payload,
        # not about this hand-maintained registry. Deliberately does NOT
        # recurse into a list's own row contents (a row's internal
        # fields are data, not further sections).
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            name = f"{prefix}_{key}" if prefix else key
            if isinstance(value, list):
                found.add(name)
            elif isinstance(value, dict):
                _list_section_names(value, name, found)

    list_section_names: set = set()
    _list_section_names(payload, "", list_section_names)
    # N6 (seventh cold read, fix round 11): whole_run_sections is a fixed
    # enumeration of section NAMES, not a bounded row/data section - it
    # is never routed through _bounded, so it has no omitted_counts entry
    # by design, not by oversight.
    missing = list_section_names - set(payload["omitted_counts"]) - {"whole_run_sections"}
    assert not missing, (
        f"payload section(s) with no matching omitted_counts entry: {missing} - "
        "a new bounded list must be routed through the same cap+omitted-count "
        "mechanism every existing section already uses"
    )

    # Every section whose real input exceeded the cap must report it -
    # never silently leave omitted_counts at 0 for a section that WAS
    # actually truncated.
    assert payload["omitted_counts"] == {
        "units": 2, "dependencies": len(edges) - 1, "problems": 1,
        "features": 1, "entry_points": 1,
        "readiness_signals": 1, "readiness_summaries": 1,
        "units_without_feature": 1, "unmapped_entry_points": 1,
        "high_fan_out_units": 1, "high_fan_in_units": 1,
    }
    # Meta-assertion (M2, fourth cold read, fix round 6): every capped
    # section in this fixture must actually have been exercised - a
    # section left at omitted_counts == 0 here would mean either the cap
    # was never really hit (a fixture bug) or the section itself was
    # silently empty (exactly how M2 slipped past this same test before).
    assert all(count > 0 for count in payload["omitted_counts"].values()), (
        f"a section was never actually exercised by this fixture: {payload['omitted_counts']}"
    )


def test_counts_section_states_its_whole_run_scope():
    """M10 (cold-read, PR-B fix round 3): counts/dependency_summary/
    high_fan_*/units_without_feature are computed over the UNFILTERED,
    whole-run sets even when unit_id/feature_id/readiness_state narrows
    the actual returned rows - a caller must not have to infer that
    mismatch, so "scope" states it explicitly."""
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], unit_id="u1"))
    assert payload["counts"]["scope"] == "whole_run"
    assert payload["counts"]["units"] == 2  # whole-run, not the 1 row unit_id actually returns
    assert len(payload["units"]) == 1


def test_whole_run_sections_are_named_outside_counts_for_a_filtered_caller():
    """N6 (seventh cold read, fix round 11): a --unit/--feature/
    --readiness caller inspecting dependency_summary/high_fan_out_units/
    high_fan_in_units/units_without_feature/unmapped_entry_points
    DIRECTLY (not via counts) had no visible indication those five
    (plus counts itself) are exempt from their filter - counts's own
    "scope" note was invisible from any of those sibling sections. Named
    explicitly at the payload's own top level now."""
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], unit_id="u1"))
    assert set(payload["whole_run_sections"]) == {
        "counts", "dependency_summary", "high_fan_out_units", "high_fan_in_units",
        "units_without_feature", "unmapped_entry_points",
    }
    # every named section is actually present in this same payload.
    assert set(payload["whole_run_sections"]) <= payload.keys()
