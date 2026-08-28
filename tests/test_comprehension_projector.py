"""#55 slice-1 PR-B item 8: the ONE pure projection function
(DESIGN-55-comprehension-plane.md, "Contract for the migration-program UI
(#208)"). Single-projector parity: this same function will back both
`report --json` (item 9) and PR-D's future GET route - tested here purely
as a function of already-assembled records, with no CLI/HTTP involved.
"""

from __future__ import annotations

from agenttalk.comprehension import projector as pr
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


def _edge(edge_id: str, from_unit_id: str, *, resolution_state: str, target_unit_id=None, target_external=None):
    return DependencyRecord(
        edge_id=edge_id, from_unit_id=from_unit_id, relation="invoke", phase="runtime",
        optional=False, evidence_class="extracted", resolution_state=resolution_state,
        target_unit_id=target_unit_id, target_external=target_external,
    )


def _feature(feature_id: str, unit_ids: list[str], state: str = "candidate") -> FeatureRecord:
    return FeatureRecord(
        feature_id=feature_id, label=feature_id, state=state, origin="detected",
        unit_ids=unit_ids, entry_point_ids=[],
    )


def _entry_point(entry_point_id: str, owning_unit_id: str, feature_ids: list[str]) -> EntryPointRecord:
    return EntryPointRecord(
        entry_point_id=entry_point_id, kind="http_route", name="/x",
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
    assert payload["schema_version"] == pr.PROJECTION_SCHEMA_VERSION


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


def test_feature_id_filter_narrows_units_features_and_entry_points():
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [_entry_point("ep1", "u1", ["f1"]), _entry_point("ep2", "u2", ["f2"])]
    payload = pr.project_comprehension(**_base_kwargs(
        modules=[_unit("u1"), _unit("u2")], features=features,
        entry_points=entry_points, feature_id="f1"))
    assert [u["unit_id"] for u in payload["units"]] == ["u1"]
    assert [f["feature_id"] for f in payload["features"]] == ["f1"]
    assert [e["entry_point_id"] for e in payload["entry_points"]] == ["ep1"]


def test_readiness_state_filter_narrows_signals_and_summaries():
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    signals = [_signal("u1"), _signal("u2")]
    payload = pr.project_comprehension(**_base_kwargs(
        readiness_signals=signals, readiness_summaries=summaries, readiness_state="blocked"))
    assert [s["unit_id"] for s in payload["readiness"]["summaries"]] == ["u1"]
    assert [s["unit_id"] for s in payload["readiness"]["signals"]] == ["u1"]


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
    }


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


def test_features_entry_points_and_readiness_are_also_bounded(monkeypatch):
    """M10 (cold-read, PR-B fix round 3): features/entry_points/readiness
    signals+summaries previously had no row cap and no truncation/omitted
    count at all - unbounded on a large repo. Now bounded the same way
    every other section already was."""
    monkeypatch.setattr(pr, "_MAX_ROWS_PER_SECTION", 1)
    features = [_feature("f1", ["u1"]), _feature("f2", ["u2"])]
    entry_points = [_entry_point("ep1", "u1", ["f1"]), _entry_point("ep2", "u2", ["f2"])]
    signals = [_signal("u1"), _signal("u2")]
    summaries = [_summary("u1", "blocked"), _summary("u2", "assessed")]
    payload = pr.project_comprehension(**_base_kwargs(
        features=features, entry_points=entry_points,
        readiness_signals=signals, readiness_summaries=summaries,
    ))
    assert payload["truncated"] is True
    assert len(payload["features"]) == 1
    assert len(payload["entry_points"]) == 1
    assert len(payload["readiness"]["signals"]) == 1
    assert len(payload["readiness"]["summaries"]) == 1
    assert payload["omitted_counts"]["features"] == 1
    assert payload["omitted_counts"]["entry_points"] == 1
    assert payload["omitted_counts"]["readiness_signals"] == 1
    assert payload["omitted_counts"]["readiness_summaries"] == 1


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
