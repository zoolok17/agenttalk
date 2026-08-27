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


def _unit(unit_id: str, *, language: str = "java", classification: str = "production") -> ModuleRecord:
    return ModuleRecord(
        unit_id=unit_id, kind="component", display_name=unit_id, language=language,
        paths=[f"{unit_id}.java"], source_digests={}, classification=[classification],
        container_unit_id=None, producers=[],
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


def test_source_understood_unsatisfied_and_blocks_the_unit():
    signals, summaries = ra.build_readiness([_unit("u1", language="unknown")], [], [])
    assert _signal_by_check(signals, "source_understood").stored_status == "unsatisfied"
    assert summaries[0].stored_assessment_state == "blocked"


# ----------------------------------------------------------- dependencies_resolved

def test_dependencies_resolved_satisfied_with_no_outgoing_edges():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "satisfied"


def test_dependencies_resolved_unsatisfied_when_an_edge_is_unresolved():
    edges = [_edge("u1", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "unsatisfied"


def test_dependencies_resolved_unknown_when_an_edge_is_ambiguous():
    edges = [_edge("u1", resolution_state="ambiguous")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "unknown"


def test_dependencies_resolved_satisfied_when_all_edges_resolved():
    edges = [_edge("u1", resolution_state="resolved", target_unit_id="u2")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "satisfied"


# ----------------------------------------------------------- entry_points_mapped / feature_linked

def test_entry_points_mapped_not_applicable_without_a_feature_link():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "entry_points_mapped").stored_status == "not_applicable"


def test_entry_points_mapped_satisfied_when_the_unit_owns_a_feature():
    features = [_feature("Foo", "candidate", ["u1"])]
    signals, _ = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "entry_points_mapped").stored_status == "satisfied"


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

def test_test_evidence_located_satisfied_for_a_test_classified_unit():
    signals, _ = ra.build_readiness([_unit("u1", classification="test")], [], [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "satisfied"


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
