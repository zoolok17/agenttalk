"""#55 slice-1 PR-B item 5: dependencies.json record assembly
(DESIGN-55-comprehension-plane.md, Artifact 2). Cross-file target
resolution (design step 6) is exercised here, not in the adapter tests -
the adapter (item 3) only ever emits LOCAL, unresolved-target claims.
"""

from __future__ import annotations

import pytest

from agenttalk.comprehension import dependencies_artifact as da
from agenttalk.comprehension.adapters import java as java_adapter


def _parse(relative_path: str, source: str) -> java_adapter.JavaFileResult:
    return java_adapter.parse_java_source(relative_path, source)


# ----------------------------------------------------------- import (external)

def test_import_edge_resolves_as_external():
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n")}
    records = da.build_dependencies(results)
    imports = [r for r in records if r.relation == "import"]
    assert len(imports) == 1
    assert imports[0].target_external == "java.util.List"
    assert imports[0].resolution_state == "resolved"
    assert imports[0].target_unit_id is None


def test_import_of_an_in_scan_type_resolves_to_the_same_unit_inheritance_would():
    """D-1 (reviewer-3, PR-B delta review round 2): importing an in-scan
    type must resolve internally, exactly like the identical type would
    via `extends` - not be recorded as external with no link to the unit,
    which was a conditional whose two branches produced the same value
    (an unfinished intention, not a decision)."""
    results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        "p/Foo.java": _parse(
            "p/Foo.java", "package p;\nimport p.Base;\nclass Foo {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.confidence == "high"
    assert import_edge.target_external is None
    base_unit_id = da._java_component_unit_id("p/Base.java", "p.Base")
    assert import_edge.target_unit_id == base_unit_id

    # Prove it against the SAME unit the inheritance path resolves to.
    inherit_results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        "p/Bar.java": _parse("p/Bar.java", "package p;\nclass Bar extends p.Base {}\n"),
    }
    inherit_records = da.build_dependencies(inherit_results)
    inherit_edge = next(r for r in inherit_records if r.relation == "inherit")
    assert import_edge.target_unit_id == inherit_edge.target_unit_id


def test_import_with_a_simple_name_collision_still_classifies_external():
    """The fix must not overcorrect into similarity guessing: a genuinely
    external import (no exact qualified-name match in this scan) stays
    external even when its bare SIMPLE name happens to collide with an
    unrelated in-scan type's simple name - imports get the exact registry
    lookup only, never the inheritance path's simple-name fallback."""
    results = {
        # An in-scan type that just happens to share java.util.List's
        # simple name, under a different package.
        "q/List.java": _parse("q/List.java", "package q;\nclass List {}\n"),
        "p/Foo.java": _parse(
            "p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external == "java.util.List"
    assert import_edge.target_unit_id is None


# ----------------------------------------------------------- inherit (cross-file resolution)

def test_inherit_edge_resolves_to_a_type_declared_in_a_different_file():
    results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        # extends the FULLY QUALIFIED name, exactly as the by_qualified_name
        # registry stores it - this is the "high" confidence path.
        "p/Foo.java": _parse("p/Foo.java", "package p;\nclass Foo extends p.Base {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.confidence == "high"  # exact qualified-name match: p.Base
    base_unit_id = da._java_component_unit_id("p/Base.java", "p.Base")
    assert inherit.target_unit_id == base_unit_id


def test_inherit_edge_by_bare_simple_name_resolves_with_medium_confidence():
    results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        # extends "Base" (no package prefix) - only a SIMPLE-name match is possible.
        "q/Foo.java": _parse("q/Foo.java", "package q;\nclass Foo extends Base {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.confidence == "medium"


def test_inherit_edge_is_ambiguous_when_two_files_declare_the_same_simple_name():
    results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        "q/Base.java": _parse("q/Base.java", "package q;\nclass Base {}\n"),
        "r/Foo.java": _parse("r/Foo.java", "package r;\nclass Foo extends Base {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "ambiguous"
    assert inherit.target_unit_id is None
    assert inherit.target_unresolved == "Base"


def test_inherit_edge_is_unresolved_when_no_candidate_exists():
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nclass Foo extends NoSuchClass {}\n")}
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_unresolved == "NoSuchClass"


# ----------------------------------------------------------- test relation (cross-file)

def test_test_edge_resolves_to_the_unit_under_test_in_another_file():
    results = {
        "src/main/java/p/Foo.java": _parse("src/main/java/p/Foo.java", "package p;\nclass Foo {}\n"),
        "src/test/java/p/FooTest.java": _parse(
            "src/test/java/p/FooTest.java", "package p;\nclass FooTest {}\n"),
    }
    records = da.build_dependencies(results)
    test_edge = next(r for r in records if r.relation == "test")
    assert test_edge.resolution_state == "resolved"
    foo_unit_id = da._java_component_unit_id("src/main/java/p/Foo.java", "p.Foo")
    assert test_edge.target_unit_id == foo_unit_id


# ----------------------------------------------------------- route (external)

def test_route_edge_resolves_as_external_with_declared_evidence():
    results = {
        "Controller.java": _parse(
            "Controller.java",
            'package p;\nclass Controller {\n  @RequestMapping("/api/widgets")\n  void list() {}\n}\n',
        ),
    }
    records = da.build_dependencies(results)
    route = next(r for r in records if r.relation == "route")
    assert route.target_external == "/api/widgets"
    assert route.evidence_class == "declared"
    assert route.resolution_state == "resolved"


# ----------------------------------------------------------- build (pom.xml, non-java from-path)

def test_build_edges_from_pom_xml_are_attributed_to_the_pom_file():
    pom_edges = java_adapter.parse_maven_pom(
        "pom.xml",
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
    )
    records = da.build_dependencies({}, build_edges_by_path={"pom.xml": pom_edges})
    assert len(records) == 1
    assert records[0].relation == "build"
    assert records[0].target_external == "org.springframework:spring-core"
    assert records[0].from_unit_id == da.digests.unit_id(
        kind="file", paths=["pom.xml"], qualified_name=None)


# ----------------------------------------------------------- determinism / integrity

def test_edge_id_is_deterministic_across_two_builds():
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n")}
    first = da.build_dependencies(results)
    second = da.build_dependencies(results)
    assert {r.edge_id for r in first} == {r.edge_id for r in second}


def test_unsupported_relation_claim_raises_rather_than_silently_passing():
    bad_result = java_adapter.JavaFileResult(
        units=[java_adapter.JavaUnitClaim(
            relative_path="p/Foo.java", qualified_name="p.Foo", simple_name="Foo",
            line=1, classification="production",
        )],
        edges=[java_adapter.JavaEdgeClaim(
            from_qualified_name="p.Foo", relation="calls",  # not in the closed S1 vocabulary
            target="x", target_kind="external", evidence_class="extracted", line=1, phase="runtime",
        )],
        entry_points=[],
    )
    with pytest.raises(da.UnsupportedRelationClaimed):
        da.build_dependencies({"p/Foo.java": bad_result})


def test_to_json_round_trips_all_fields():
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n")}
    record = da.build_dependencies(results)[0]
    payload = record.to_json()
    assert payload["edge_id"] == record.edge_id
    assert payload["relation"] == "import"
