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

def test_source_digest_is_populated_from_file_digests():
    """M7 (cold-read, PR-B fix round 3): source_digest was set to None
    once per file and never actually assigned from discovery's already-
    computed content digest."""
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n")}
    records = da.build_dependencies(results, file_digests={"p/Foo.java": "deadbeef"})
    assert records[0].producers[0]["source_digest"] == "deadbeef"


def test_source_digest_defaults_to_none_without_file_digests():
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n")}
    records = da.build_dependencies(results)
    assert records[0].producers[0]["source_digest"] is None


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


# ----------------------------------------------------------- coalescing (M6)

def test_byte_identical_invoke_edges_coalesce_to_one_record_with_merged_producers():
    """M6 (cold-read, PR-B fix round 3): three identical call sites to the
    same target from the same unit produce the SAME edge_id (the adapter
    records no per-call-site distinguishing evidence this slice) - the
    design's merge rule requires these coalesce to ONE record with merged
    producers, not three duplicate records inflating record_counts,
    ceilings, and every fan-in/fan-out count. Reproduced pre-fix: 3 call
    sites gave 3 records, 1 unique edge_id."""
    source = (
        "package p;\n"
        "class Caller {\n"
        "  void run() {\n"
        "    Foo.bar();\n"
        "    Foo.bar();\n"
        "    Foo.bar();\n"
        "  }\n"
        "}\n"
    )
    results = {"p/Caller.java": _parse("p/Caller.java", source)}
    records = da.build_dependencies(results)
    invoke_records = [r for r in records if r.relation == "invoke"]
    assert len(invoke_records) == 1
    assert len({r.edge_id for r in invoke_records}) == 1
    assert len(invoke_records[0].producers) == 1


# ----------------------------------------------------------- M12: registry collisions and invoke over-reach

def test_duplicate_qualified_name_never_resolves_confidently_to_either_claimant():
    """M12 (cold-read, PR-B fix round 3): two files DECLARING the exact
    same fully-qualified name used to last-wins in the registry - a
    scan-order-dependent, silently wrong resolution. Now neither
    claimant is offered by exact lookup; resolution falls through to the
    existing simple-name-ambiguity path, which correctly reports
    ambiguous (2 candidates sharing that simple name - themselves)."""
    results = {
        "a/Dup.java": _parse("a/Dup.java", "package p;\nclass Dup {}\n"),
        "b/Dup.java": _parse("b/Dup.java", "package p;\nclass Dup {}\n"),
        "c/Foo.java": _parse("c/Foo.java", "package p;\nclass Foo extends p.Dup {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "ambiguous"
    assert inherit.target_unit_id is None


def test_invoke_on_an_unrecognized_qualifier_does_not_capture_an_unrelated_same_named_class():
    """M12 (cold-read, PR-B fix round 3): a call like `Optional.of(...)`
    with no local declaration and no import must NOT resolve against an
    unrelated same-named class declared elsewhere in the scan (a
    JDK-shadowing or common test-helper name) - the design's own "never
    invents an internal target because names look similar" invariant,
    previously violated because the invoke else-branch fed the GLOBAL
    simple-name matcher unconditionally."""
    results = {
        "unrelated/pkg/Optional.java": _parse(
            "unrelated/pkg/Optional.java", "package unrelated.pkg;\nclass Optional {}\n"),
        "p/Caller.java": _parse(
            "p/Caller.java",
            "package p;\nclass Caller {\n  void run() {\n    Optional.of(1);\n  }\n}\n",
        ),
    }
    records = da.build_dependencies(results)
    invoke = next(r for r in records if r.relation == "invoke")
    assert invoke.resolution_state == "unresolved"
    assert invoke.target_unit_id is None
    assert invoke.target_unresolved == "Optional"


# ----------------------------------------------------------- second cold read B-1: import-mediated invoke

def test_import_mediated_invoke_of_an_in_scan_type_resolves_to_the_same_internal_unit():
    """Second cold read B-1 (fix round 4, BLOCKER): a call whose qualifier
    resolves through an import was stamped external unconditionally,
    never consulting the registry - so calling an imported IN-SCAN type
    (the normal cross-package case in Java) filed a real internal
    dependency as third-party, emptied it from fan-in, and let readiness
    claim dependencies_resolved=satisfied over nothing. The import edge
    and the invoke edge for the SAME imported name must resolve to the
    SAME internal unit - proven directly against each other, not just
    against a separately-computed unit id."""
    results = {
        "p/OrderService.java": _parse(
            "p/OrderService.java", "package p;\nclass OrderService {}\n"),
        "q/OrderController.java": _parse(
            "q/OrderController.java",
            "package q;\n"
            "import p.OrderService;\n"
            "class OrderController {\n"
            "  void run() {\n"
            "    OrderService.create();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    invoke_edge = next(r for r in records if r.relation == "invoke")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external is None
    assert invoke_edge.resolution_state == "resolved"
    assert invoke_edge.target_external is None
    assert invoke_edge.target_unit_id == import_edge.target_unit_id
    assert invoke_edge.target_unit_id == da._java_component_unit_id(
        "p/OrderService.java", "p.OrderService")


def test_import_mediated_invoke_of_a_genuinely_external_type_still_classifies_external():
    """Second cold read B-1 (fix round 4): the fix must not overcorrect -
    a call through an import of a type that is NOT declared anywhere in
    this scan (the ordinary JDK/library case) must still resolve
    external, exactly as before."""
    results = {
        "p/Foo.java": _parse(
            "p/Foo.java",
            "package p;\n"
            "import java.util.Collections;\n"
            "class Foo {\n"
            "  void run() {\n"
            "    Collections.emptyList();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    invoke_edge = next(r for r in records if r.relation == "invoke")
    assert invoke_edge.resolution_state == "resolved"
    assert invoke_edge.target_external == "java.util.Collections"
    assert invoke_edge.target_unit_id is None


# ----------------------------------------------------------- fourth cold read N5: static imports

def test_static_import_of_an_in_scan_type_resolves_internally():
    """N5 (fourth cold read, fix round 6): a static import's target is a
    member path (Type.MEMBER), never itself a type's own qualified name -
    but the TYPE PREFIX (everything but the last segment) is itself
    fully qualified and exact-matchable, the same way D-1 already
    established for a plain import. Stamping every static import
    "external" unconditionally counted this in-scan dependency
    (`import static p.OrderService.create` where OrderService IS in-scan)
    as external, the same fan-in loss D-1 fixed for plain imports."""
    results = {
        "p/OrderService.java": _parse(
            "p/OrderService.java", "package p;\nclass OrderService {\n  static void create() {}\n}\n"),
        "q/OrderController.java": _parse(
            "q/OrderController.java",
            "package q;\n"
            "import static p.OrderService.create;\n"
            "class OrderController {\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external is None
    assert import_edge.target_unit_id == da._java_component_unit_id(
        "p/OrderService.java", "p.OrderService")


def test_static_import_of_a_genuinely_external_member_still_classifies_external():
    """N5 (fourth cold read, fix round 6): the fix must not overcorrect -
    a static import of a type genuinely not declared anywhere in this
    scan (the ordinary JDK/library case) must still resolve external,
    with the FULL original member-path spelling preserved as evidence."""
    results = {
        "p/Foo.java": _parse(
            "p/Foo.java",
            "package p;\n"
            "import static java.util.Collections.emptyList;\n"
            "class Foo {\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external == "java.util.Collections.emptyList"
    assert import_edge.target_unit_id is None


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
    """Dead-parameter removal (reviewer-3, PR-B delta review round 2):
    build_dependencies used to take a separate build_edges_by_path
    parameter for this case; since B-3 routed pom.xml through the
    sanitized worker (worker.process_paths dispatches it into the SAME
    java_results channel every other adapter claim uses, wrapped as a
    JavaFileResult with only edges populated), this test now exercises
    that exact production shape instead of a bespoke parameter."""
    pom_edges, _pom_problems = java_adapter.parse_maven_pom(
        "pom.xml",
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
    )
    results = {"pom.xml": java_adapter.JavaFileResult(edges=pom_edges)}
    records = da.build_dependencies(results)
    assert len(records) == 1
    assert records[0].relation == "build"
    assert records[0].target_external == "org.springframework:spring-core"
    assert records[0].from_unit_id == da.digests.unit_id(
        kind="file", paths=["pom.xml"], qualified_name=None)


def test_optional_and_scope_test_thread_through_to_the_dependency_record():
    """M3 (fourth cold read, fix round 6): DependencyRecord.optional was
    hardcoded False in _edge_claim_to_record regardless of what the
    adapter's own claim said - the field existed and was in the published
    schema, but nothing ever set it from real evidence."""
    pom_edges, _pom_problems = java_adapter.parse_maven_pom(
        "pom.xml",
        "<project><dependencies><dependency>"
        "<groupId>org.mockito</groupId><artifactId>mockito-core</artifactId>"
        "<scope>test</scope><optional>true</optional>"
        "</dependency></dependencies></project>",
    )
    results = {"pom.xml": java_adapter.JavaFileResult(edges=pom_edges)}
    records = da.build_dependencies(results)
    assert len(records) == 1
    assert records[0].optional is True
    assert records[0].phase == "test"


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
