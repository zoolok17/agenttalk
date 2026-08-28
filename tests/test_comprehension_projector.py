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
    missing = list_section_names - set(payload["omitted_counts"])
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
