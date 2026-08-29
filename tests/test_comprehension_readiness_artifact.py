"""#55 slice-1 PR-B item 7: readiness.json record assembly
(DESIGN-55-comprehension-plane.md, Artifact 4). Builds ModuleRecord/
DependencyRecord/FeatureRecord fixtures directly rather than through the
full adapter pipeline, to isolate the policy-evaluation logic itself.
"""

from __future__ import annotations

from agenttalk.comprehension import readiness_artifact as ra
from agenttalk.comprehension.dependencies_artifact import DependencyRecord
from agenttalk.comprehension.features_artifact import FeatureRecord
from agenttalk.comprehension.modules_artifact import ModuleRecord


def _unit(
    unit_id: str, *, language: str = "java", classification: str = "production",
    adapter_problem_reason: str | None = None, adapter_problem_reasons: list[str] | None = None,
    container_unit_id: str | None = None,
) -> ModuleRecord:
    return ModuleRecord(
        unit_id=unit_id, kind="component", display_name=unit_id, language=language,
        paths=[f"{unit_id}.java"], source_digests={}, classification=[classification],
        container_unit_id=container_unit_id, producers=[],
        adapter_problem_reason=adapter_problem_reason,
        adapter_problem_reasons=adapter_problem_reasons or [],
    )


def _edge(
    from_unit_id: str, *, relation: str = "invoke", resolution_state: str, target_unit_id=None,
) -> DependencyRecord:
    return DependencyRecord(
        edge_id=f"edge-{from_unit_id}-{relation}-{resolution_state}", from_unit_id=from_unit_id,
        relation=relation, phase="runtime", optional=False, evidence_class="extracted",
        resolution_state=resolution_state, target_unit_id=target_unit_id,
    )


def _feature(label: str, state: str, unit_ids: list[str]) -> FeatureRecord:
    return FeatureRecord(
        feature_id=f"feature-{label}", label=label, state=state, origin="detected",
        unit_ids=unit_ids, entry_point_ids=[],
    )


def _signal_by_check(signals, check):
    return next(s for s in signals if s.check == check)


# ----------------------------------------------------------- source_understood

def test_source_understood_satisfied_for_a_known_language_unit():
    signals, _summaries = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "source_understood").stored_status == "satisfied"


def test_assessed_is_currently_unreachable_this_slice() -> None:
    """Documents an intentional, honest consequence of the design rather
    than a bug: boundaries_identified is ALWAYS unknown this slice (no
    data/configuration producer exists yet), so no unit - however
    thoroughly evidenced everywhere else - can roll up to "assessed" until
    a later slice adds that producer. "Unknown is first-class... never
    collapse to ready" (design core invariant 5) applies to the rollup
    itself, not just individual signals."""
    edges = [_edge("u1", resolution_state="resolved", target_unit_id="u2")]
    features = [_feature("Foo", "confirmed", ["u1"])]
    signals, summaries = ra.build_readiness(
        [_unit("u1", classification="test")], edges, features)
    assert all(
        s.stored_status in ("satisfied", "not_applicable")
        for s in signals if s.check != "boundaries_identified"
    )
    assert _signal_by_check(signals, "boundaries_identified").stored_status == "unknown"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_source_understood_unknown_and_needs_evidence_with_no_adapter():
    """M-2 (second cold read, fix round 4): "no adapter for this
    language" is an ABSENCE of positive evidence, not a positive claim
    that the source is definitely not understood - the design's own
    rollup rule draws this exact line (DESIGN-55-comprehension-plane.md,
    Artifact 4: "any required scan-time blocker that is UNSATISFIED
    yields blocked; any required scan-time UNKNOWN yields
    needs_evidence"). Reports unknown/needs_evidence, never the stronger
    unsatisfied/blocked - a confident blocker for every non-code file
    would make blocked the default headline state on a real repo, where
    most files are non-code."""
    signals, summaries = ra.build_readiness([_unit("u1", language="unknown")], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "no_adapter_for_language"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_source_understood_unknown_when_the_adapter_failed_to_parse():
    """B3 (cold-read, PR-B fix round 3): a file the adapter attempted and
    failed to parse (or could not even read) must be genuinely UNKNOWN,
    never "satisfied" just because its extension maps to a known
    language - "satisfied" is a positive claim this unit never earned."""
    signals, summaries = ra.build_readiness(
        [_unit(
            "u1", language="java", adapter_problem_reason="parse_failed",
            adapter_problem_reasons=["parse_failed"],
        )], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_parse_failed"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_source_understood_unknown_when_the_adapter_work_resource_cap_skipped_it():
    """M-2 (third cold read, fix round 5): CLOSES THE CLASS - round 3
    threaded ONLY the ``parse_failed`` reason; a file the worker skipped
    for the per-file adapter-work resource cap fell through the exact
    same "no positive evidence, but reported satisfied anyway" gap a
    second time. source_understood must derive from the PRESENCE of
    positive adapter evidence, never from the absence of one specific,
    named failure kind - so this (and any future worker failure reason)
    is unknown by construction, with a reason_code that still names the
    real cause."""
    signals, summaries = ra.build_readiness(
        [_unit(
            "u1", language="java", adapter_problem_reason="resource_limit",
            adapter_problem_reasons=["resource_limit"],
        )], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_resource_limit"
    assert summaries[0].stored_assessment_state == "needs_evidence"


# ----------------------------------------------------------- dependencies_resolved

def test_dependencies_resolved_satisfied_with_no_outgoing_edges():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "satisfied"


def test_dependencies_resolved_unsatisfied_when_an_edge_is_unresolved():
    edges = [_edge("u1", relation="import", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "unsatisfied"


def test_dependencies_resolved_unknown_when_an_edge_is_ambiguous():
    edges = [_edge("u1", relation="import", resolution_state="ambiguous")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "unknown"


def test_dependencies_resolved_satisfied_when_an_unresolved_edge_is_invoke_only():
    """FIX ROUND 12 (eighth cold read, F2/F5 folded in): dependencies_
    resolved is scoped to import/inherit/build relations - the design's
    "direct internal dependencies" - so an unresolved ``invoke`` edge
    (the JDK-noise shape: an unqualified call the adapter cannot
    recognize as external, like ``Math.max(...)``) must never drive an
    ordinary class to unsatisfied."""
    edges = [_edge("u1", relation="invoke", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"
    assert signal.reason_code == "no_declared_dependencies"


def test_dependencies_resolved_satisfied_when_all_edges_resolved():
    """FIX ROUND 12b (reviewer-3): this used to rely on `_edge`'s default
    relation ("invoke") - post-scoping (round 12's F2/F5 fix), an invoke
    edge is filtered out entirely, so this test passed via the
    no-qualifying-edges/no_declared_dependencies branch, byte-identical to a unit
    with zero edges at all, and would have survived deletion of the
    all-resolved branch it names. relation="import" (like its moved
    siblings) makes this a real dependency edge that actually reaches
    and exercises that branch."""
    edges = [_edge("u1", relation="import", resolution_state="resolved", target_unit_id="u2")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"
    assert signal.reason_code == "dependencies_resolved"


def _file_unit(unit_id: str) -> ModuleRecord:
    return ModuleRecord(
        unit_id=unit_id, kind="file", display_name=unit_id, language="java",
        paths=[f"{unit_id}.java"], source_digests={}, classification=["production"],
        container_unit_id=None, producers=[],
    )


# --------------------------------- dependencies_resolved, CR10-1 (round 14): file-scoped imports

def test_component_with_no_own_edges_but_a_single_sibling_free_file_inherits_the_files_status():
    """FIX ROUND 14 (CR10-1): a component with NO edges of its own, in a
    file with exactly ONE top-level declared type, has no attribution
    ambiguity at all - the file's own import edges honestly ARE this
    type's own dependency picture. Must mirror the file's aggregate
    status, never a vacuous satisfied."""
    file_unit = _file_unit("Solo")
    component = _unit("comp1", container_unit_id="Solo")
    edges = [_edge("Solo", relation="import", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    component_signal = next(
        s for s in signals if s.unit_id == "comp1" and s.check == "dependencies_resolved")
    assert component_signal.stored_status == "unsatisfied"


def test_component_with_no_own_edges_and_multiple_siblings_reports_unknown_not_satisfied():
    """FIX ROUND 14 (CR10-1 MAJOR, the reviewer's Service/ServiceCache
    shape): a component with no edges of its own, sharing a file with
    ANOTHER top-level declared type, cannot honestly credit the file's
    import evidence to just this one sibling - never a vacuous
    satisfied/no_declared_dependencies (the un-evidenced positive the
    readiness policy refuses everywhere else), degrades to unknown with
    a named reason instead."""
    file_unit = _file_unit("Multi")
    service = _unit("Service", container_unit_id="Multi")
    service_cache = _unit("ServiceCache", container_unit_id="Multi")
    edges = [_edge("Multi", relation="import", resolution_state="resolved", target_unit_id="ext")]
    signals, _ = ra.build_readiness([file_unit, service, service_cache], edges, [])
    for unit_id in ("Service", "ServiceCache"):
        signal = next(
            s for s in signals if s.unit_id == unit_id and s.check == "dependencies_resolved")
        assert signal.stored_status == "unknown", unit_id
        assert signal.reason_code == "file_scoped_dependencies_not_attributed", unit_id


def test_component_with_its_own_edges_is_unaffected_by_file_scoped_imports():
    """FIX ROUND 14 (CR10-1 control): a component with its OWN real
    body evidence (an invoke/inherit/test edge attributed to it
    directly) must keep reporting on that evidence alone, regardless of
    what the file's own import edges say - own evidence always wins,
    never overridden by file-level corroboration."""
    file_unit = _file_unit("Multi")
    service = _unit("Service", container_unit_id="Multi")
    service_cache = _unit("ServiceCache", container_unit_id="Multi")
    edges = [
        _edge("Multi", relation="import", resolution_state="unresolved"),
        _edge("Service", relation="inherit", resolution_state="resolved", target_unit_id="ext"),
    ]
    signals, _ = ra.build_readiness([file_unit, service, service_cache], edges, [])
    service_signal = next(
        s for s in signals if s.unit_id == "Service" and s.check == "dependencies_resolved")
    assert service_signal.stored_status == "satisfied"


# ------------------------------------------- dependencies_resolved, file units (N6, round 6)

def test_file_units_dependencies_resolved_derives_from_contained_units_not_vacuously_satisfied():
    """N6 (fourth cold read, fix round 6): edges attach to the declared
    TYPE, never the FILE that contains it - a file unit's own
    dependencies_resolved used to always be satisfied/no_declared_dependencies
    (it never receives outgoing edges directly), a structurally
    always-on positive signal that was never actually evidence of
    anything. A file whose CONTAINED component unit has an unresolved
    edge must roll that up onto the file's own signal, not report a
    vacuous satisfied."""
    component = ModuleRecord(
        unit_id="comp1", kind="component", display_name="comp1", language="java",
        paths=["file1.java"], source_digests={}, classification=["production"],
        container_unit_id="file1", producers=[],
    )
    file_unit = _file_unit("file1")
    edges = [_edge("comp1", relation="import", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    file_signal = next(
        s for s in signals if s.unit_id == "file1" and s.check == "dependencies_resolved")
    assert file_signal.stored_status == "unsatisfied"


def test_file_units_with_no_contained_units_and_no_direct_edges_are_not_applicable():
    """N6 (fourth cold read, fix round 6): a plain non-code file (or one
    the adapter never understood) has no meaningful "dependencies"
    concept at all - not_applicable, never a confident positive earned
    by nothing."""
    file_unit = _file_unit("file1")
    signals, _ = ra.build_readiness([file_unit], [], [])
    file_signal = next(
        s for s in signals if s.unit_id == "file1" and s.check == "dependencies_resolved")
    assert file_signal.stored_status == "not_applicable"


def test_file_units_with_a_direct_edge_of_their_own_are_still_evaluated():
    """N6 (fourth cold read, fix round 6): a pom.xml-style file has no
    component children at all, yet build edges attach DIRECTLY to its
    own file unit (there being no component-level unit for that
    producer) - this must still be evaluated on its own direct edges,
    not swept into not_applicable just because it has no CONTAINED
    units."""
    file_unit = _file_unit("pom_xml")
    edges = [_edge("pom_xml", relation="build", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([file_unit], edges, [])
    file_signal = next(
        s for s in signals if s.unit_id == "pom_xml" and s.check == "dependencies_resolved")
    assert file_signal.stored_status == "unsatisfied"


# ----------------------------------------------------------- entry_points_mapped / feature_linked

def test_entry_points_mapped_not_applicable_without_a_feature_link():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "entry_points_mapped").stored_status == "not_applicable"


def test_entry_points_mapped_satisfied_when_the_unit_owns_a_feature():
    features = [_feature("Foo", "candidate", ["u1"])]
    signals, _ = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "entry_points_mapped").stored_status == "satisfied"


def test_entry_points_mapped_unknown_when_an_unrecognized_main_like_shape_was_recorded():
    """FIX ROUND 13b (reviewer-3's B1 class-closer): a unit whose file
    carries the adapter's "cli_main_unrecognized" problem (a method
    literally named main, returning void, that the strict cli_main
    detector could not confidently classify) must report entry_points_
    mapped UNKNOWN, never the confident not_applicable/no_entry_point
    negative - the same three-state move round 11 already made for an
    unrecoverable route value."""
    unit = _unit("u1", adapter_problem_reasons=["cli_main_unrecognized"])
    signals, _ = ra.build_readiness([unit], [], [])
    signal = _signal_by_check(signals, "entry_points_mapped")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "cli_main_unrecognized"


def test_feature_linked_unsatisfied_with_no_feature_at_all():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "feature_linked").stored_status == "unsatisfied"


def test_feature_linked_unknown_when_only_a_candidate_feature_links_it():
    features = [_feature("Foo", "candidate", ["u1"])]
    signals, summaries = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "feature_linked").stored_status == "unknown"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_feature_linked_satisfied_when_a_confirmed_feature_links_it():
    features = [_feature("Foo", "confirmed", ["u1"])]
    signals, _ = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "feature_linked").stored_status == "satisfied"


# ----------------------------------------------------------- test_evidence_located

def test_test_evidence_located_not_applicable_for_a_test_classified_unit():
    """FIX ROUND 14 (CR10-7, the tautology half): a test class satisfying
    this check about ITSELF is meaningless - the check's subject is the
    PRODUCTION unit a test pairs to, never the test class's own record."""
    signals, _ = ra.build_readiness([_unit("u1", classification="test")], [], [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "not_applicable"


def test_test_evidence_located_satisfied_when_targeted_by_a_test_edge():
    edges = [_edge("u2", relation="test", resolution_state="resolved", target_unit_id="u1")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "satisfied"


def test_test_evidence_located_unknown_otherwise():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "unknown"


# ----------------------------------------------------------- boundaries_identified (always unknown)

def test_boundaries_identified_is_always_unknown_this_slice():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    boundary_signal = _signal_by_check(signals, "boundaries_identified")
    assert boundary_signal.stored_status == "unknown"
    assert boundary_signal.basis == "detected"


# ----------------------------------------------------------- determinism

def test_signal_id_is_deterministic():
    first, _ = ra.build_readiness([_unit("u1")], [], [])
    second, _ = ra.build_readiness([_unit("u1")], [], [])
    assert {s.signal_id for s in first} == {s.signal_id for s in second}


def test_every_signal_names_the_default_policy():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert all(s.policy["policy_id"] == ra.POLICY_ID for s in signals)
    assert all(s.policy["policy_version"] == ra.POLICY_VERSION for s in signals)
