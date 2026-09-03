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


def _parse_pom(relative_path: str, source: str) -> java_adapter.JavaFileResult:
    units, edges, _profile_scoped_count = java_adapter.parse_maven_pom(relative_path, source)
    return java_adapter.JavaFileResult(units=units, edges=edges)


# --------------------------------------------------------- pom build edge (internal_pom_coordinate_or_external)

def test_pom_dependency_on_a_sibling_whose_own_coordinate_has_an_interior_comment_resolves_internal():
    """FIX ROUND 38 (thirty-second cold read, F2 BLOCKER, .cr32-pomws,
    wrong-data): a comment interior to a pom's own <artifactId>
    (mod<!--x-->b) used to publish that module's own REGISTERED
    identity as the corrupted, blanked-whitespace spelling ("com.acme:
    mod        b") - a real sibling pom's <dependency> on the true
    "com.acme:modb" then found no match in the shared registry and
    resolved a false, confident EXTERNAL claim instead of internal (the
    exact over-claim class round 18's own F3 fix, and this producer's
    whole internal_pom_coordinate_or_external discipline, exist to
    prevent). Control (below, no comment): the identical dependency
    already resolves internal - proving this fix closes the comment
    shape specifically, not a coincidence of this one fixture."""
    mod_a_pom = (
        "<project><groupId>com.acme</groupId>"
        "<artifactId>mod<!--internal build tag-->b</artifactId></project>"
    )
    mod_b_pom = (
        "<project><groupId>com.acme</groupId><artifactId>consumer</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>modb</artifactId>"
        "</dependency></dependencies></project>"
    )
    results = {
        "modA/pom.xml": _parse_pom("modA/pom.xml", mod_a_pom),
        "modB/pom.xml": _parse_pom("modB/pom.xml", mod_b_pom),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None
    assert build_edge.target_unit_id is not None

    # Control: the byte-identical scenario, minus the comment - already
    # resolved internal before this round; must be unaffected by it.
    control_results = {
        "modA/pom.xml": _parse_pom(
            "modA/pom.xml",
            "<project><groupId>com.acme</groupId><artifactId>modb</artifactId></project>"),
        "modB/pom.xml": _parse_pom("modB/pom.xml", mod_b_pom),
    }
    control_records = da.build_dependencies(control_results)
    control_build_edge = next(r for r in control_records if r.relation == "build")
    assert control_build_edge.resolution_state == "resolved"
    assert control_build_edge.target_external is None
    assert control_build_edge.target_unit_id == build_edge.target_unit_id


# ----------------------------------------------------------- import (external)

def test_source_digest_is_populated_from_file_digests():
    """M7 (cold-read, PR-B fix round 3): source_digest was set to None
    once per file and never actually assigned from discovery's already-
    computed content digest."""
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nimport java.util.List;\nclass Foo {}\n")}
    records = da.build_dependencies(results, file_digests={"p/Foo.java": "deadbeef"})
    assert records[0].producers[0]["source_digest"] == "deadbeef"
    # FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data,
    # extracted control): a plain import edge's own evidence_class is
    # "extracted" (real syntactic evidence, no inference/declaration
    # involved) - producers[].basis must match it exactly, never a
    # hardcoded literal that happens to agree here but not elsewhere.
    assert records[0].evidence_class == "extracted"
    assert records[0].producers[0]["basis"] == "extracted"


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


def test_import_edge_is_attributed_to_the_file_unit_never_the_first_declared_type():
    """FIX ROUND 14 (tenth cold read, CR10-1 MAJOR, verbatim shape): a
    public class plus a package-private helper in one file - the
    everyday legacy shape. The import edge must resolve to the FILE
    unit, never either declared type - the helper (declared second)
    must never appear to have "inherited" the first class's own import,
    and the first class must never be falsely credited with it either."""
    results = {
        "p/Service.java": _parse(
            "p/Service.java",
            "package p;\nimport java.util.List;\npublic class Service {\n}\nclass ServiceCache {\n}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    service_unit_id = da._java_component_unit_id("p/Service.java", "p.Service")
    service_cache_unit_id = da._java_component_unit_id("p/Service.java", "p.ServiceCache")
    file_unit_id = da._java_file_unit_id("p/Service.java")
    assert import_edge.from_unit_id not in {service_unit_id, service_cache_unit_id}
    assert import_edge.from_unit_id == file_unit_id


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


def test_inherit_edge_by_bare_simple_name_with_no_supporting_evidence_stays_unresolved():
    """FIX ROUND 12 (eighth cold read, F1 BLOCKER): this test used to
    assert the OPPOSITE - that a bare name matching exactly one in-scan
    type ANYWHERE resolved with medium confidence, regardless of package.
    That was itself the bug the reviewer's three shapes exposed (design
    line 418: "The scanner never invents an internal target because
    names look similar."). ``q.Foo`` neither imports ``Base`` nor shares
    a package with ``p.Base`` - no evidence supports this resolution, so
    it must stay unresolved with the bare spelling retained, never a
    name-similarity guess, however unique that name happens to be."""
    results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        "q/Foo.java": _parse("q/Foo.java", "package q;\nclass Foo extends Base {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_unit_id is None
    assert inherit.target_unresolved == "Base"


def test_inherit_edge_by_bare_simple_name_resolves_via_same_package_sibling():
    """FIX ROUND 12 (F1 direction 3): a bare name matching
    ``{this file's own package}.{name}`` is a legitimate SAME-PACKAGE
    implicit resolution - Java's own scoping rule, not a name-similarity
    guess - and stays resolved, at medium confidence (implicit, not an
    exact spelled-out reference)."""
    results = {
        "p/Base.java": _parse("p/Base.java", "package p;\nclass Base {}\n"),
        "p/Foo.java": _parse("p/Foo.java", "package p;\nclass Foo extends Base {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.confidence == "medium"
    assert inherit.target_unit_id == da._java_component_unit_id("p/Base.java", "p.Base")


def test_inherit_edge_resolves_via_this_files_own_import_not_a_global_guess():
    """FIX ROUND 12 (F1 direction 2, positive control): a bare name this
    file's own import binds to a specific package resolves via THAT
    import, exactly like the identical import already resolves - the
    reader verified this path works and it must keep working."""
    results = {
        "com/corp/commons/web/BaseController.java": _parse(
            "com/corp/commons/web/BaseController.java",
            "package com.corp.commons.web;\nclass BaseController {}\n"),
        "com/acme/shop/ShopController.java": _parse(
            "com/acme/shop/ShopController.java",
            "package com.acme.shop;\n"
            "import com.corp.commons.web.BaseController;\n"
            "class ShopController extends BaseController {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.confidence == "high"
    assert inherit.target_unit_id == da._java_component_unit_id(
        "com/corp/commons/web/BaseController.java", "com.corp.commons.web.BaseController")


def test_inherit_edge_via_import_of_an_unrelated_package_never_falls_back_to_a_same_named_class():
    """FIX ROUND 12 (eighth cold read, F1 BLOCKER, reproduced shape A):
    ShopController imports com.corp.commons.web.BaseController and
    extends the bare name BaseController - an UNRELATED
    com.acme.admin.BaseController also happens to exist in-scan. The old
    code fell back to the GLOBAL simple-name match (exactly one
    candidate) and silently resolved to the wrong, unrelated class,
    directly contradicting the import edge published for the SAME
    artifact. Must never resolve to that wrong in-scan class (or the
    bare name) - the imported spelling is what was actually meant.

    FIX ROUND 15 (eleventh cold read, M8 MAJOR): round 12 left this
    "unresolved" - correct on its own narrower point (never the wrong
    class) but still a real inconsistency M8 closes: the IMPORT edge for
    this identical qualified name independently resolves target_external
    (nothing in-scan answers for it, and it is not a same-run degraded
    file), so the inherit edge now consults that same verdict rather
    than leaving two edges to contradict each other about one fact."""
    results = {
        "com/acme/admin/BaseController.java": _parse(
            "com/acme/admin/BaseController.java",
            "package com.acme.admin;\nclass BaseController {}\n"),
        "com/acme/shop/ShopController.java": _parse(
            "com/acme/shop/ShopController.java",
            "package com.acme.shop;\n"
            "import com.corp.commons.web.BaseController;\n"
            "class ShopController extends BaseController {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.target_unit_id is None
    assert inherit.target_external == "com.corp.commons.web.BaseController"


def test_inherit_edge_written_fully_qualified_never_falls_back_to_a_same_named_class():
    """FIX ROUND 12 (reproduced shape B): the extends clause spells the
    target out FULLY QUALIFIED in source, naming a package that is not
    in-scan - an unrelated same-simple-name class must never be offered
    as a guess just because it is the only same-named candidate."""
    results = {
        "com/acme/admin/BaseController.java": _parse(
            "com/acme/admin/BaseController.java",
            "package com.acme.admin;\nclass BaseController {}\n"),
        "com/acme/shop/ShopController.java": _parse(
            "com/acme/shop/ShopController.java",
            "package com.acme.shop;\n"
            "class ShopController extends com.corp.commons.web.BaseController {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_unit_id is None
    assert inherit.target_unresolved == "com.corp.commons.web.BaseController"


def test_test_edge_via_import_of_an_unrelated_package_never_falls_back_to_a_same_named_class():
    """FIX ROUND 12 (reproduced shape C): InvoiceServiceTest imports
    com.corp.legacy.InvoiceService (never in-scan) - an unrelated
    com.acme.billing.InvoiceService must never absorb the test edge just
    because it is the only in-scan class sharing that bare name.

    FIX ROUND 14 (CR10-7): a name-suffix match alone is no longer
    sufficient to classify test/emit a test edge - this fixture now
    carries a real test-framework import as its corroborating evidence
    (a genuine JUnit test class, not a bare-suffix guess)."""
    results = {
        "com/acme/billing/InvoiceService.java": _parse(
            "com/acme/billing/InvoiceService.java",
            "package com.acme.billing;\nclass InvoiceService {}\n"),
        "com/acme/billing/InvoiceServiceTest.java": _parse(
            "com/acme/billing/InvoiceServiceTest.java",
            "package com.acme.billing;\n"
            "import com.corp.legacy.InvoiceService;\n"
            "import org.junit.Test;\n"
            "class InvoiceServiceTest {}\n"),
    }
    records = da.build_dependencies(results)
    test_edge = next(r for r in records if r.relation == "test")
    assert test_edge.resolution_state == "unresolved"
    assert test_edge.target_unit_id is None
    assert test_edge.target_unresolved == "com.corp.legacy.InvoiceService"


def test_inherit_edge_via_a_wildcard_import_lands_unresolved_a_named_limit():
    """FIX ROUND 12b (reviewer-3 delta on round 12): a named LIMIT, not a
    bug - a wildcard import (``import com.acme.util.*;``) never binds a
    bare name to one specific package (the adapter classifies it
    external, not a per-type import), so a bare name Java itself WOULD
    resolve through that wildcard - and that genuinely IS declared
    in-scan - can still land unresolved if it is not also a same-file
    declaration or same-package sibling. A deliberate, safe under-claim:
    the direct consequence of deleting the global bare-name fallback
    that made F1's false positives possible."""
    results = {
        "com/acme/util/Helper.java": _parse(
            "com/acme/util/Helper.java", "package com.acme.util;\nclass Helper {}\n"),
        "com/acme/shop/Worker.java": _parse(
            "com/acme/shop/Worker.java",
            "package com.acme.shop;\n"
            "import com.acme.util.*;\n"
            "class Worker extends Helper {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_unit_id is None
    assert inherit.target_unresolved == "Helper"


def test_inherit_edge_is_ambiguous_when_two_files_declare_the_same_simple_name():
    """FIX ROUND 15 (eleventh cold read, M4 JUDGE - taken): the design's
    own text names an ambiguous edge as carrying candidates ("unresolved
    edge with candidates") - the registry knows the tied units at
    resolution time, so publishing them is asserted here too, not just
    the bare unresolved spelling."""
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
    assert sorted(inherit.candidate_unit_ids) == sorted([
        da._java_component_unit_id("p/Base.java", "p.Base"),
        da._java_component_unit_id("q/Base.java", "q.Base"),
    ])


def test_inherit_edge_is_ambiguous_with_candidates_on_a_genuine_registry_collision():
    """FIX ROUND 15 (M4 JUDGE - taken, registry-collision shape): two
    units declaring the IDENTICAL fully-qualified name (not merely a
    same-simple-name coincidence across packages) is the other ambiguous
    path (M12) - by_qualified_name drops the name entirely on a second
    claimant, so candidates must come from the separate accumulator that
    never drops anything."""
    results = {
        "a/p/Base.java": _parse("a/p/Base.java", "package p;\nclass Base {}\n"),
        "b/p/Base.java": _parse("b/p/Base.java", "package p;\nclass Base {}\n"),
        "r/Foo.java": _parse("r/Foo.java", "package r;\nclass Foo extends p.Base {}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "ambiguous"
    assert inherit.target_unit_id is None
    assert inherit.target_unresolved == "p.Base"
    assert sorted(inherit.candidate_unit_ids) == sorted([
        da._java_component_unit_id("a/p/Base.java", "p.Base"),
        da._java_component_unit_id("b/p/Base.java", "p.Base"),
    ])


def test_inherit_edge_is_unresolved_when_no_candidate_exists():
    results = {"p/Foo.java": _parse("p/Foo.java", "package p;\nclass Foo extends NoSuchClass {}\n")}
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_unresolved == "NoSuchClass"


def test_import_edge_is_ambiguous_on_a_genuine_registry_collision():
    """FIX ROUND 16 (twelfth cold read, B1 BLOCKER, wrong-data): the
    plain-import branch (``internal_exact_or_external``) used to fall
    straight to ``resolved``/``target_external`` on an exact-lookup MISS,
    never consulting ``duplicate_qualified_names`` the way the inherit/
    test ladder already does (see
    test_inherit_edge_is_ambiguous_with_candidates_on_a_genuine_registry_
    collision above) - two units genuinely declaring the identical
    fully-qualified name published as a confident, wrong EXTERNAL
    dependency for any FILE that imported that name, rather than the
    honest ambiguous-with-candidates the design's own text requires
    ("Ambiguous resolution creates an unresolved edge with candidates.").
    Mirrors reviewer-3's own ``.cr12-dup`` fixture: two modules each
    declaring ``com.acme.Config``, imported by a third."""
    results = {
        "app/src/main/java/com/acme/app/Boot.java": _parse(
            "app/src/main/java/com/acme/app/Boot.java",
            "package com.acme.app;\n"
            "import com.acme.Config;\n"
            "class Boot {}\n"),
        "modA/src/main/java/com/acme/Config.java": _parse(
            "modA/src/main/java/com/acme/Config.java", "package com.acme;\nclass Config {}\n"),
        "modB/src/main/java/com/acme/Config.java": _parse(
            "modB/src/main/java/com/acme/Config.java", "package com.acme;\nclass Config {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(
        r for r in records
        if r.relation == "import" and r.target_unresolved == "com.acme.Config")
    assert import_edge.resolution_state == "ambiguous"
    assert import_edge.target_unit_id is None
    assert import_edge.target_unresolved == "com.acme.Config"
    assert sorted(import_edge.candidate_unit_ids) == sorted([
        da._java_component_unit_id("modA/src/main/java/com/acme/Config.java", "com.acme.Config"),
        da._java_component_unit_id("modB/src/main/java/com/acme/Config.java", "com.acme.Config"),
    ])


def test_static_import_edge_is_ambiguous_on_a_genuine_registry_collision():
    """FIX ROUND 16 (B1 BLOCKER): the SAME class, via the static-import
    branch - keyed on the type prefix, not the full member path, exactly
    like the branch's own existing degraded-path check already is."""
    results = {
        "app/Boot.java": _parse(
            "app/Boot.java",
            "package com.acme.app;\n"
            "import static com.acme.Config.VALUE;\n"
            "class Boot {}\n"),
        "modA/Config.java": _parse("modA/Config.java", "package com.acme;\nclass Config {}\n"),
        "modB/Config.java": _parse("modB/Config.java", "package com.acme;\nclass Config {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "ambiguous"
    assert import_edge.target_unresolved == "com.acme.Config.VALUE"
    assert sorted(import_edge.candidate_unit_ids) == sorted([
        da._java_component_unit_id("modA/Config.java", "com.acme.Config"),
        da._java_component_unit_id("modB/Config.java", "com.acme.Config"),
    ])


def test_import_edge_is_unresolved_not_external_when_externality_poisoned():
    """FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR - THE POISON RULE,
    superseding round 16's own B2 BLOCKER string-matching approach):
    mirrors ``_degraded_java_suffix_match``'s own reasoning for the
    mirror-image relationship - a target whose hypothetical file lives
    inside a region THIS run excluded outright (never walked, so it can
    never have a registry entry) is not "genuinely external" just
    because the registry has no entry for it. The string-matching guard
    this test originally exercised (``_excluded_region_match``) is
    retired entirely - inert for the mainstream Maven layout, where an
    excluded root's own recorded path has no string relationship to the
    unwalked source arbitrarily deeper inside it. Now driven by the
    run-wide ``externality_poisoned`` flag scan_pipeline.py computes
    from discovery's own peek + the reactor rule - a registry miss must
    not silently become a confident external claim, and must not be
    silently deleted from the inventory either."""
    results = {
        "p/OrderService.java": _parse(
            "p/OrderService.java",
            "package p;\n"
            "import p.out.PaymentGateway;\n"
            "class OrderService {}\n"),
    }
    records = da.build_dependencies(results, externality_poisoned=True)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "unresolved"
    assert import_edge.target_unresolved == "p.out.PaymentGateway"
    assert import_edge.target_external is None


def test_import_edge_still_resolves_external_when_not_poisoned():
    """Companion negative case: the SAME shape with externality_poisoned
    left at its default (False) still resolves external as before - the
    poison rule only closes the specific excluded-region gap, never
    turns every registry miss into unresolved."""
    results = {
        "p/OrderService.java": _parse(
            "p/OrderService.java",
            "package p;\n"
            "import p.out.PaymentGateway;\n"
            "class OrderService {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external == "p.out.PaymentGateway"


def test_import_edge_under_poison_still_resolves_external_for_a_reserved_namespace():
    """FIX ROUND 20b (seventeenth-round dispatch, THE ASK - taken): the
    platform reserves java.*/javax.*/jakarta.* - a vendored/excluded
    region this run swallowed structurally CANNOT contain a legitimate
    declaration under any of them, so a poisoned run still resolves one
    of these as a confident EXTERNAL claim, unlike an ordinary
    third-party package (org.slf4j, tested in the same fixture) which
    correctly stays unresolved under the same poison."""
    results = {
        "p/OrderService.java": _parse(
            "p/OrderService.java",
            "package p;\n"
            "import java.util.List;\n"
            "import org.slf4j.Logger;\n"
            "class OrderService {}\n"),
    }
    records = da.build_dependencies(results, externality_poisoned=True)
    java_util_edge = next(
        r for r in records if r.relation == "import" and r.target_external == "java.util.List")
    assert java_util_edge.resolution_state == "resolved"
    slf4j_edge = next(
        r for r in records if r.relation == "import" and r.target_unresolved == "org.slf4j.Logger")
    assert slf4j_edge.resolution_state == "unresolved"
    assert slf4j_edge.target_external is None


def test_wildcard_import_edge_is_unresolved_when_its_package_is_in_scan():
    """FIX ROUND 16 (twelfth cold read, B3 BLOCKER, wrong-data): a plain
    wildcard import (``import com.acme.util.*;``) used to publish
    ``resolved``/``target_external`` UNCONDITIONALLY (java.py hardcoded
    ``target_kind = "external"``) - even when the wildcard's own package
    prefix genuinely IS declared in-scan, silently miscounting a real
    in-scan package import as third-party. Mirrors reviewer-3's own
    ``.cr12-wildcard`` fixture: Report.java wildcard-imports
    ``com.acme.util.*``, and ``com.acme.util.DateHelper`` is in-scan."""
    results = {
        "app/Report.java": _parse(
            "app/Report.java",
            "package com.acme.app;\n"
            "import com.acme.util.*;\n"
            "class Report {}\n"),
        "util/DateHelper.java": _parse(
            "util/DateHelper.java", "package com.acme.util;\nclass DateHelper {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "unresolved"
    assert import_edge.target_unresolved == "com.acme.util.*"
    assert import_edge.target_external is None


def test_wildcard_import_edge_still_resolves_external_when_its_package_is_not_in_scan():
    """Companion negative case: a wildcard import whose package matches
    NOTHING in-scan still resolves external as before - the fix only
    closes the specific in-scan-package-miscounted-as-external gap."""
    results = {
        "app/Report.java": _parse(
            "app/Report.java",
            "package com.acme.app;\n"
            "import java.util.*;\n"
            "class Report {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external == "java.util.*"


def test_wildcard_import_edge_when_externality_poisoned_stays_unresolved_too():
    """FIX ROUND 16c (reviewer-3's approval-conditioned minor on round
    16b - "the last door"): the wildcard branch consulted ONLY
    in_scan_packages, never the excluded-region check every OTHER
    registry-miss caller already goes through - ``import target.gen.*``
    published resolved/external while ``import target.Stub`` in the
    SAME file (the non-wildcard twin) correctly published unresolved:
    two different answers about one excluded tree, in the same run.
    Both must now agree, driven by the SAME run-wide
    ``externality_poisoned`` flag (round 20's own POISON RULE) as the
    plain import edge."""
    results = {
        "r/Report.java": _parse(
            "r/Report.java",
            "package r;\n"
            "import target.gen.*;\n"
            "import target.Stub;\n"
            "class Report {}\n"),
    }
    records = da.build_dependencies(results, externality_poisoned=True)
    wildcard_edge = next(
        r for r in records if r.relation == "import" and r.target_unresolved == "target.gen.*")
    plain_edge = next(
        r for r in records if r.relation == "import" and r.target_unresolved == "target.Stub")
    assert wildcard_edge.resolution_state == "unresolved"
    assert wildcard_edge.target_external is None
    assert plain_edge.resolution_state == "unresolved"
    assert plain_edge.target_external is None


def test_wildcard_import_edge_under_poison_still_resolves_external_for_a_reserved_namespace():
    """FIX ROUND 20b (THE ASK - taken): same reserved-namespace exemption
    on the wildcard branch - ``import java.util.*`` still resolves
    external under poison, while ``import org.slf4j.*`` in the same file
    correctly stays unresolved."""
    results = {
        "r/Report.java": _parse(
            "r/Report.java",
            "package r;\n"
            "import java.util.*;\n"
            "import org.slf4j.*;\n"
            "class Report {}\n"),
    }
    records = da.build_dependencies(results, externality_poisoned=True)
    java_util_edge = next(
        r for r in records if r.relation == "import" and r.target_external == "java.util.*")
    slf4j_edge = next(
        r for r in records if r.relation == "import" and r.target_unresolved == "org.slf4j.*")
    assert java_util_edge.resolution_state == "resolved"
    assert slf4j_edge.resolution_state == "unresolved"
    assert slf4j_edge.target_external is None


def test_wildcard_import_edge_still_resolves_external_when_not_poisoned():
    """Companion negative case: a wildcard import still resolves
    external as before when externality_poisoned is left at its default
    (False) - the poison rule only closes the specific gap."""
    results = {
        "r/Report.java": _parse(
            "r/Report.java",
            "package r;\nimport java.util.*;\nclass Report {}\n"),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    assert import_edge.resolution_state == "resolved"
    assert import_edge.target_external == "java.util.*"


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


def test_test_edge_resolves_to_the_wrong_convention_guess_but_never_satisfies_readiness():
    """FIX ROUND 15 (eleventh cold read, F4 MAJOR, wrong-data, cr11-fx4
    verbatim): IntegrationTests actually exercises only BillingEngine,
    but its NAME-derived pairing (strip "Tests", resolve "Integration")
    happens to resolve to an unrelated, genuinely-untested class -
    stamping coverage onto it while BillingEngine itself reports no
    evidence. The published edge's evidence_class is "inferred" (a
    convention guess, never real source evidence) - readiness's own
    tested_unit_ids computation (readiness_artifact.py) is what actually
    keeps this from satisfying test_evidence_located; this test pins the
    adapter's own half of the contract (the published evidence_class),
    the readiness-level test pins the other half."""
    results = {
        "com/acme/Integration.java": _parse(
            "com/acme/Integration.java", "package com.acme;\nclass Integration {}\n"),
        "com/acme/BillingEngine.java": _parse(
            "com/acme/BillingEngine.java", "package com.acme;\nclass BillingEngine {}\n"),
        "src/test/java/com/acme/IntegrationTests.java": _parse(
            "src/test/java/com/acme/IntegrationTests.java",
            "package com.acme;\nclass IntegrationTests {}\n"),
    }
    records = da.build_dependencies(results)
    test_edge = next(r for r in records if r.relation == "test")
    assert test_edge.resolution_state == "resolved"
    assert test_edge.evidence_class == "inferred"
    # FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data):
    # producers[].basis used to be the hardcoded literal "extracted"
    # regardless of this SAME record's own evidence_class - two
    # contradictory provenance claims about the identical edge. basis
    # must match evidence_class exactly.
    assert test_edge.producers[0]["basis"] == "inferred"
    integration_unit_id = da._java_component_unit_id("com/acme/Integration.java", "com.acme.Integration")
    assert test_edge.target_unit_id == integration_unit_id
    # BillingEngine (the class actually exercised, per the reviewer's
    # fixture story) gets NO test edge at all from this file - the
    # adapter has no mechanism to detect real usage inside a test body,
    # so it correctly stays silent about it rather than guessing.
    assert "BillingEngine" not in {r.target_unresolved for r in records if r.relation == "test"}


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


def test_two_out_of_scan_classes_routed_to_the_same_target_get_distinct_edge_ids():
    """FIX ROUND 39 (thirty-third cold read, F1(c), re-running the
    collision hunt against edge_id's own degenerate/fallback inputs,
    per reviewer-3's own standard): `build_dependencies`'s own
    `by_qualified_name.get(edge.from_qualified_name) or file_unit_id_
    by_path[path]` fallback resolves BOTH of two out-of-scan classes'
    own route edges to the SAME synthetic file unit - when they also
    share relation/target/phase (two out-of-scan servlets mapped to
    the identical <url-pattern>, the same ordinary shape round 31's
    own duplicate_route_target problem already names), the two edges
    collided BY CONSTRUCTION and silently coalesced into ONE published
    record, though they are genuinely two different declaring classes'
    own facts. `from_qualified_name` (real, different for each class,
    already available at the emission site) is now threaded into
    edge_id too. The control above (three identical calls from the
    SAME class) proves this does not disturb genuine coalescing -
    `from_qualified_name` is identical there, so it still collapses."""
    web_xml = (
        "<web-app>"
        "<servlet><servlet-name>a</servlet-name>"
        "<servlet-class>com.vendor.pkg1.OutOfScanA</servlet-class></servlet>"
        "<servlet><servlet-name>b</servlet-name>"
        "<servlet-class>com.vendor.pkg2.OutOfScanB</servlet-class></servlet>"
        "<servlet-mapping><servlet-name>a</servlet-name>"
        "<url-pattern>/shared/*</url-pattern></servlet-mapping>"
        "<servlet-mapping><servlet-name>b</servlet-name>"
        "<url-pattern>/shared/*</url-pattern></servlet-mapping>"
        "</web-app>"
    )
    entry_points, problems, edges, _conflicts = java_adapter.parse_web_xml(
        "WEB-INF/web.xml", web_xml)
    results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(
            entry_points=entry_points, edges=edges, problems=problems),
    }
    records = da.build_dependencies(results)
    route_records = [r for r in records if r.relation == "route"]
    assert len(route_records) == 2
    assert len({r.edge_id for r in route_records}) == 2


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
    scan (the ordinary JDK/library case) must still resolve external.

    FIX ROUND 15 (eleventh cold read, N1 MINOR): the member was already
    stripped for the RESOLUTION lookup key (the type, not the member, is
    what might be in-scan) - the published external name now matches
    it, naming the type the dependency actually is
    ("java.util.Collections"), never the full member path
    ("java.util.Collections.emptyList") masquerading as one."""
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
    assert import_edge.target_external == "java.util.Collections"
    assert import_edge.target_unit_id is None


# ----------------------------------------------------------- ninth cold read CR9-1/CR9-5: invoke qualifiers

def test_invoke_on_a_fully_qualified_legacy_class_never_resolves_to_an_unrelated_imported_class():
    """FIX ROUND 13 (ninth cold read, CR9-1 BLOCKER): the reviewer's own
    reproduced shape - MigrationBridge imports com.acme.v2.OrderService
    but explicitly calls the FULLY QUALIFIED com.acme.legacy.OrderService
    (the legacy-vs-rewrite migration idiom this plane exists to
    inventory). The invoke edge must resolve against the LEGACY class
    actually named, never get silently rewritten to the unrelated
    imported v2 class."""
    results = {
        "com/acme/legacy/OrderService.java": _parse(
            "com/acme/legacy/OrderService.java",
            "package com.acme.legacy;\nclass OrderService {\n  static void lookup() {}\n}\n"),
        "com/acme/v2/OrderService.java": _parse(
            "com/acme/v2/OrderService.java",
            "package com.acme.v2;\nclass OrderService {\n  static void lookup() {}\n}\n"),
        "com/acme/bridge/MigrationBridge.java": _parse(
            "com/acme/bridge/MigrationBridge.java",
            "package com.acme.bridge;\n"
            "import com.acme.v2.OrderService;\n"
            "class MigrationBridge {\n"
            "  void run() {\n"
            "    com.acme.legacy.OrderService.lookup();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    invoke = next(r for r in records if r.relation == "invoke")
    assert invoke.resolution_state == "resolved"
    assert invoke.confidence == "high"
    assert invoke.target_unit_id == da._java_component_unit_id(
        "com/acme/legacy/OrderService.java", "com.acme.legacy.OrderService")


def test_invoke_on_a_package_prefixed_nested_type_never_resolves_to_an_unrelated_imported_class():
    """FIX ROUND 13b (reviewer-3's B2 BLOCKER on round 13): the first cut
    of the CR9-1 fix required the prefix to be lowercase-led specifically
    - so `com.acme.Outer.Inner.x()` still reduced to bare "Inner", which
    then met the bare-keyed import table and resolved to an UNRELATED
    imported `com.wrong.Inner` - CR9-1's exact mechanism through a
    second door, confirmed here by unit identity via the registry with
    BOTH classes genuinely in-scan."""
    results = {
        "com/acme/Outer.java": _parse(
            "com/acme/Outer.java",
            "package com.acme;\n"
            "class Outer {\n"
            "  static class Inner {\n"
            "    static void x() {}\n"
            "  }\n"
            "}\n",
        ),
        "com/wrong/Inner.java": _parse(
            "com/wrong/Inner.java", "package com.wrong;\nclass Inner {}\n"),
        "com/acme/Foo.java": _parse(
            "com/acme/Foo.java",
            "package com.acme;\n"
            "import com.wrong.Inner;\n"
            "class Foo {\n"
            "  void run() {\n"
            "    com.acme.Outer.Inner.x();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    invoke = next(r for r in records if r.relation == "invoke")
    assert invoke.resolution_state == "resolved"
    assert invoke.target_unit_id == da._java_component_unit_id(
        "com/acme/Outer.java", "com.acme.Outer.Inner")
    wrong_unit_id = da._java_component_unit_id("com/wrong/Inner.java", "com.wrong.Inner")
    assert invoke.target_unit_id != wrong_unit_id


def test_static_import_member_qualifier_and_its_import_edge_agree_on_the_same_unit():
    """FIX ROUND 13 (ninth cold read, CR9-5 MINOR): a static-imported
    member used bare as a qualifier and the import edge for the SAME
    line must resolve to the SAME internal unit - proven directly
    against each other, the same way D-1's import/invoke parity test
    already does for a plain import."""
    results = {
        "com/acme/Config.java": _parse(
            "com/acme/Config.java",
            "package com.acme;\nclass Config {\n  static final Object LOGGER = null;\n}\n"),
        "com/acme/Foo.java": _parse(
            "com/acme/Foo.java",
            "package com.acme;\n"
            "import static com.acme.Config.LOGGER;\n"
            "class Foo {\n"
            "  void run() {\n"
            "    LOGGER.toString();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    import_edge = next(r for r in records if r.relation == "import")
    invoke_edge = next(r for r in records if r.relation == "invoke")
    assert import_edge.resolution_state == "resolved"
    assert invoke_edge.resolution_state == "resolved"
    assert invoke_edge.target_unit_id == import_edge.target_unit_id
    assert invoke_edge.target_unit_id == da._java_component_unit_id(
        "com/acme/Config.java", "com.acme.Config")


# ----------------------------------------------------------- tenth cold read CR10-2: invoke ladder unification

def test_invoke_bare_qualifier_resolves_via_the_same_package_ladder_as_inherit():
    """FIX ROUND 14 (tenth cold read, CR10-2 MAJOR, the reviewer's
    Caller/Util shape): the resolution ladder (same-file, own-import,
    same-package, else unresolved/ambiguous) was applied to inherit/test
    but invoke's bare qualifier stopped at exact-FQN-only, so
    `Caller extends Util` resolved (same-package ladder) while `Caller`'s
    OWN `Util.go()` call - the identical relationship - stayed
    unresolved in the SAME run: two contradictory facts about one
    dependency in one artifact. Both edges must now resolve to the SAME
    unit."""
    results = {
        "p/Util.java": _parse(
            "p/Util.java", "package p;\nclass Util {\n  static void go() {}\n}\n"),
        "p/Caller.java": _parse(
            "p/Caller.java",
            "package p;\nclass Caller extends Util {\n  void run() {\n    Util.go();\n  }\n}\n",
        ),
    }
    records = da.build_dependencies(results)
    inherit_edge = next(r for r in records if r.relation == "inherit")
    invoke_edge = next(r for r in records if r.relation == "invoke")
    util_unit_id = da._java_component_unit_id("p/Util.java", "p.Util")
    assert inherit_edge.resolution_state == "resolved"
    assert inherit_edge.target_unit_id == util_unit_id
    assert invoke_edge.resolution_state == "resolved"
    assert invoke_edge.target_unit_id == util_unit_id


# ----------------------------------------------------------- tenth cold read CR10-3: member-navigation chains

def test_enum_constant_navigation_resolves_the_type_never_the_constant():
    """FIX ROUND 14 (tenth cold read, CR10-3 MAJOR, verbatim shape):
    `Status.ACTIVE.code()` captured qualifier "Status.ACTIVE" - an enum
    CONSTANT, not a dependency target - and published an unresolved edge
    on that fabricated string while the REAL in-scan dependency on
    Status itself was never emitted. Must resolve the TYPE (Status),
    dropping the constant-access tail, never publish "Status.ACTIVE"
    verbatim."""
    results = {
        "p/Status.java": _parse(
            "p/Status.java", "package p;\nenum Status {\n  ACTIVE, INACTIVE\n}\n"),
        "p/Caller.java": _parse(
            "p/Caller.java",
            "package p;\nclass Caller {\n  void run() {\n    Status.ACTIVE.code();\n  }\n}\n",
        ),
    }
    records = da.build_dependencies(results)
    invoke = next(r for r in records if r.relation == "invoke")
    assert invoke.resolution_state == "resolved"
    assert invoke.target_unit_id == da._java_component_unit_id("p/Status.java", "p.Status")
    assert invoke.target_unresolved is None


def test_nested_type_navigation_resolves_the_nested_unit_not_a_fabricated_string():
    """FIX ROUND 14 (tenth cold read, CR10-3 MAJOR, verbatim shape):
    `Config.Defaults.timeout()` captured qualifier "Config.Defaults" -
    even though com.acme.legacy.Config.Defaults IS a published unit of
    this same run, its fan-in stayed 0 because the chain was never
    resolved past the unqualified head. Must resolve to the REAL nested
    unit (Config's own head resolves via import; the remaining segment
    re-attached and exact-matched against the nested type's own
    qualified name)."""
    results = {
        "com/acme/legacy/Config.java": _parse(
            "com/acme/legacy/Config.java",
            "package com.acme.legacy;\n"
            "class Config {\n"
            "  static class Defaults {\n"
            "    static int timeout() { return 0; }\n"
            "  }\n"
            "}\n",
        ),
        "com/acme/app/Caller.java": _parse(
            "com/acme/app/Caller.java",
            "package com.acme.app;\n"
            "import com.acme.legacy.Config;\n"
            "class Caller {\n"
            "  void run() {\n"
            "    Config.Defaults.timeout();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    invoke = next(r for r in records if r.relation == "invoke")
    assert invoke.resolution_state == "resolved"
    assert invoke.target_unit_id == da._java_component_unit_id(
        "com/acme/legacy/Config.java", "com.acme.legacy.Config.Defaults")


def test_five_class_legacy_estate_resolves_entirely_internal():
    """FIX ROUND 14 (CR10-3, the reviewer's .cr10-legacy shape,
    condensed): a small, entirely in-scan codebase whose dependencies
    are ALL same-package/nested/enum-constant references must publish
    dependency_summary with real internal edges, never 0 internal / N
    unresolved on a codebase with nothing external at all."""
    results = {
        "p/Status.java": _parse(
            "p/Status.java", "package p;\nenum Status {\n  ACTIVE\n}\n"),
        "p/Util.java": _parse(
            "p/Util.java", "package p;\nclass Util {\n  static void go() {}\n}\n"),
        "p/Service.java": _parse(
            "p/Service.java",
            "package p;\n"
            "class Service extends Util {\n"
            "  void run() {\n"
            "    Util.go();\n"
            "    Status.ACTIVE.name();\n"
            "  }\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    internal = [r for r in records if r.resolution_state == "resolved" and r.target_unit_id]
    unresolved = [r for r in records if r.resolution_state == "unresolved"]
    assert len(internal) == 3  # extends Util, Util.go(), Status.ACTIVE.name()
    assert unresolved == []


# ----------------------------------------------------------- tenth cold read CR10-4: java.lang known-external

def test_inherit_edge_to_a_java_lang_type_resolves_known_external():
    """FIX ROUND 14 (tenth cold read, CR10-4 MAJOR, verbatim shape):
    round 12 scoped dependencies_resolved away from invoke noise but
    left inherit with the identical property - java.lang needs no
    import, so `extends RuntimeException` published a confident
    unresolved_dependency on entirely healthy code. Must resolve as
    KNOWN-EXTERNAL, never unresolved."""
    results = {
        "p/OrderNotFoundException.java": _parse(
            "p/OrderNotFoundException.java",
            "package p;\nclass OrderNotFoundException extends RuntimeException {\n}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.target_external == "java.lang.RuntimeException"
    assert inherit.target_unit_id is None


def test_invoke_edge_to_a_java_lang_type_resolves_known_external():
    """FIX ROUND 14 (CR10-4): the SAME known-external recognition
    applies to invoke (Math.max, String.valueOf, ...) since round 12/14
    unified invoke onto the shared ladder - fixes the noise at its
    source, not just readiness's own relation-scoping workaround."""
    results = {
        "p/Foo.java": _parse(
            "p/Foo.java",
            "package p;\nclass Foo {\n  void run() {\n    Math.max(1, 2);\n  }\n}\n"),
    }
    records = da.build_dependencies(results)
    invoke = next(r for r in records if r.relation == "invoke")
    assert invoke.resolution_state == "resolved"
    assert invoke.target_external == "java.lang.Math"


def test_a_local_class_shadowing_a_java_lang_name_wins_over_known_external():
    """FIX ROUND 14 (CR10-4 control): the ladder's own evidence always
    wins first, exactly Java's shadowing rule - a LOCAL declaration (or
    an import, or a same-package sibling) sharing a java.lang name must
    resolve to that real in-scan type, never silently reclassified as
    the java.lang default just because the name matches one."""
    results = {
        "p/Foo.java": _parse(
            "p/Foo.java",
            "package p;\n"
            "class Foo extends Exception {\n"
            "}\n"
            "class Exception {\n"
            "}\n",
        ),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "resolved"
    assert inherit.target_unit_id == da._java_component_unit_id("p/Foo.java", "p.Exception")
    assert inherit.target_external is None


# ----------------------------------------------------------- eleventh cold read M8: inherit-through-import external

def test_inherit_edge_through_an_import_that_resolves_external_matches_the_import_edge():
    """FIX ROUND 15 (eleventh cold read, M8 MAJOR, wrong-data, promoted
    from polish - same class as CR10-4): a real-world servlet subclass
    (`extends HttpServlet` with `import javax.servlet.http.HttpServlet;`)
    used to publish an UNRESOLVED inherit edge while the import edge for
    the identical qualified name independently resolved target_external
    - two contradictory facts about one dependency in the same run, and
    a confident dependencies_resolved deficiency on every servlet
    subclass, entirely healthy code. Both edges must now agree."""
    results = {
        "p/MyServlet.java": _parse(
            "p/MyServlet.java",
            "package p;\n"
            "import javax.servlet.http.HttpServlet;\n"
            "class MyServlet extends HttpServlet {\n"
            "}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    import_edge = next(r for r in records if r.relation == "import")
    assert inherit.resolution_state == "resolved"
    assert inherit.target_unit_id is None
    assert inherit.target_external == "javax.servlet.http.HttpServlet"
    assert inherit.target_external == import_edge.target_external


def test_inherit_edge_through_an_import_of_a_same_run_degraded_file_stays_unresolved():
    """FIX ROUND 15 (M8 control, F2 MAJOR precedent preserved): a
    same-run degraded file (an adapter resource cap, a read/parse
    failure) must never become a confident external claim just because
    its registry entry is missing - it is missing because the file
    degraded away, not because the type is genuinely third-party. Mirrors
    the identical protection the import edge itself already has."""
    results = {
        "p/MyServlet.java": _parse(
            "p/MyServlet.java",
            "package p;\n"
            "import com.acme.legacy.BaseServlet;\n"
            "class MyServlet extends BaseServlet {\n"
            "}\n"),
    }
    records = da.build_dependencies(
        results, degraded_paths=frozenset({"com/acme/legacy/BaseServlet.java"}))
    inherit = next(r for r in records if r.relation == "inherit")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_external is None
    assert inherit.target_unresolved == "com.acme.legacy.BaseServlet"


def test_inherit_edge_through_an_import_of_a_duplicate_qualified_name_is_ambiguous_too():
    """FIX ROUND 16b (reviewer-3's rejection of round 16, BLOCKER 2 -
    "the predicate bypass"): the M8 inherit-through-import rule still
    called ``_degraded_java_suffix_match`` INLINE, missing the
    duplicate-FQN and excluded-region checks ``_classify_registry_miss``
    centralizes everywhere else. Reproduced: two in-scan ``p.Base`` +
    ``import p.Base;`` + ``extends Base`` published the IMPORT edge
    ambiguous (2 candidates) while the INHERIT edge for the IDENTICAL
    qualified name published resolved/external - round 16's own
    headline mechanism (a registry miss silently becoming a confident
    external claim) still live on this one caller. Both edges must now
    agree: ambiguous, with the same candidates."""
    results = {
        "a/p/Base.java": _parse("a/p/Base.java", "package p;\nclass Base {}\n"),
        "b/p/Base.java": _parse("b/p/Base.java", "package p;\nclass Base {}\n"),
        "r/MyServlet.java": _parse(
            "r/MyServlet.java",
            "package r;\n"
            "import p.Base;\n"
            "class MyServlet extends Base {\n"
            "}\n"),
    }
    records = da.build_dependencies(results)
    inherit = next(r for r in records if r.relation == "inherit")
    import_edge = next(r for r in records if r.relation == "import")
    assert inherit.resolution_state == "ambiguous"
    assert inherit.target_external is None
    assert import_edge.resolution_state == "ambiguous"
    assert sorted(inherit.candidate_unit_ids) == sorted(import_edge.candidate_unit_ids)
    assert sorted(inherit.candidate_unit_ids) == sorted([
        da._java_component_unit_id("a/p/Base.java", "p.Base"),
        da._java_component_unit_id("b/p/Base.java", "p.Base"),
    ])


def test_inherit_edge_through_an_import_when_externality_poisoned_stays_unresolved_too():
    """FIX ROUND 16b (BLOCKER 2, the excluded-region variant of the same
    shape) / FIX ROUND 20 (M1+M2, THE POISON RULE): an inherit edge
    through an import whose hypothetical file lives under a poisoned
    region must stay unresolved, matching the import edge, never a
    confident external claim while any excluded region might hold the
    first-party source it is missing."""
    results = {
        "r/OrderService.java": _parse(
            "r/OrderService.java",
            "package r;\n"
            "import p.out.PaymentGateway;\n"
            "class OrderService extends PaymentGateway {\n"
            "}\n"),
    }
    records = da.build_dependencies(results, externality_poisoned=True)
    inherit = next(r for r in records if r.relation == "inherit")
    import_edge = next(r for r in records if r.relation == "import")
    assert inherit.resolution_state == "unresolved"
    assert inherit.target_external is None
    assert inherit.target_unresolved == "p.out.PaymentGateway"
    assert import_edge.resolution_state == "unresolved"


# ----------------------------------------------------------- route (external)

def test_route_edge_resolves_as_external_with_declared_evidence():
    results = {
        "Controller.java": _parse(
            "Controller.java",
            'package p;\n@RestController\nclass Controller {\n  @RequestMapping("/api/widgets")\n  void list() {}\n}\n',
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
    _pom_units, pom_edges, _profile_scoped_count = java_adapter.parse_maven_pom(
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
    # FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data,
    # declared control): a pom <dependency>'s own evidence_class is
    # "declared" (an explicit build-file declaration, no source-code
    # inference involved) - producers[].basis must match it, never the
    # hardcoded "extracted" every producer used to publish regardless.
    assert records[0].evidence_class == "declared"
    assert records[0].producers[0]["basis"] == "declared"


def test_optional_and_scope_test_thread_through_to_the_dependency_record():
    """M3 (fourth cold read, fix round 6): DependencyRecord.optional was
    hardcoded False in _edge_claim_to_record regardless of what the
    adapter's own claim said - the field existed and was in the published
    schema, but nothing ever set it from real evidence."""
    _pom_units, pom_edges, _profile_scoped_count = java_adapter.parse_maven_pom(
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


def test_a_reactor_module_dependency_resolves_internal_to_the_sibling_pom():
    """FIX ROUND 17 (thirteenth cold read, CR13-4 MAJOR, wrong-data): a
    multi-module Maven reactor's module-to-module dependency (the single
    most migration-relevant internal edge a pom can declare) used to
    publish resolved/EXTERNAL unconditionally, even when the SIBLING pom
    declaring that exact groupId:artifactId sits in the same scan -
    nothing ever registered a pom's own coordinate as a resolvable unit.
    Mirrors the reader's own reactor shape: app/pom.xml depends on
    com.acme:shared-lib, and shared-lib/pom.xml declares exactly that
    coordinate."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>shared-lib</artifactId>"
        "</dependency></dependencies></project>",
    )
    shared_units, shared_edges, _c2 = java_adapter.parse_maven_pom(
        "shared-lib/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>shared-lib</artifactId></project>",
    )
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "shared-lib/pom.xml": java_adapter.JavaFileResult(units=shared_units, edges=shared_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None
    assert build_edge.target_unit_id == da._java_component_unit_id(
        "shared-lib/pom.xml", "com.acme:shared-lib")


def test_a_reactor_module_that_inherits_groupid_from_parent_resolves_internal():
    """FIX ROUND 18 (fourteenth cold read, F3 MAJOR, wrong-data): mirrors
    the reader's own parent-inheritance shape - shared-lib/pom.xml
    declares NO project-level groupId of its own, only inheriting it
    from its <parent> block (the standard, common Maven reactor
    spelling). A sibling's dependency edge on it must now resolve
    internal, the same as the explicit-groupId case above."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>shared-lib</artifactId>"
        "</dependency></dependencies></project>",
    )
    shared_units, shared_edges, _c2 = java_adapter.parse_maven_pom(
        "shared-lib/pom.xml",
        "<project><parent><groupId>com.acme</groupId>"
        "<artifactId>acme-parent</artifactId><version>1.0</version></parent>"
        "<artifactId>shared-lib</artifactId></project>",
    )
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "shared-lib/pom.xml": java_adapter.JavaFileResult(units=shared_units, edges=shared_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None
    assert build_edge.target_unit_id == da._java_component_unit_id(
        "shared-lib/pom.xml", "com.acme:shared-lib")


def test_a_cdata_wrapped_reactor_module_coordinate_still_resolves_internal():
    """FIX ROUND 35 (twenty-ninth cold read, F1 BLOCKER, wrong-data, .cr29-
    cdata verbatim): the reader's own killer consequence - shared-lib/
    pom.xml wraps its OWN project-level groupId/artifactId in CDATA (a
    real, if unusual, shape - some generators do this). Before the round
    35 fix, `_project_own_coordinate` published this UNDECODED
    ("<![CDATA[com.acme]]>:<![CDATA[shared-lib]]>"), which never matches
    app/pom.xml's own plain "com.acme:shared-lib" dependency target - the
    registry miss then satisfied the positive-grounds external test and
    published a CONFIDENT resolved/EXTERNAL claim for an in-scan module,
    the exact round-18-F3 over-claim class this producer's own registry
    exists to prevent. Now decoded first - the edge must resolve
    INTERNAL, identically to the plain-groupId control above."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>shared-lib</artifactId>"
        "</dependency></dependencies></project>",
    )
    shared_units, shared_edges, _c2 = java_adapter.parse_maven_pom(
        "shared-lib/pom.xml",
        "<project><groupId><![CDATA[com.acme]]></groupId>"
        "<artifactId><![CDATA[shared-lib]]></artifactId></project>",
    )
    assert {u.qualified_name for u in shared_units} == {"com.acme:shared-lib"}
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "shared-lib/pom.xml": java_adapter.JavaFileResult(units=shared_units, edges=shared_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None
    assert build_edge.target_unit_id == da._java_component_unit_id(
        "shared-lib/pom.xml", "com.acme:shared-lib")


def test_a_cdata_wrapped_parent_group_id_still_resolves_internal():
    """FIX ROUND 35 (F1 BLOCKER, .cr29-cdata2 variant - CDATA-in-<parent>):
    the same undecoded-publication defect, but for the <parent> block's
    own groupId this pom's own coordinate falls back to when it declares
    no project-level groupId of its own."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>shared-lib</artifactId>"
        "</dependency></dependencies></project>",
    )
    shared_units, shared_edges, _c2 = java_adapter.parse_maven_pom(
        "shared-lib/pom.xml",
        "<project><parent><groupId><![CDATA[com.acme]]></groupId>"
        "<artifactId>acme-parent</artifactId><version>1.0</version></parent>"
        "<artifactId>shared-lib</artifactId></project>",
    )
    assert {u.qualified_name for u in shared_units} == {"com.acme:shared-lib"}
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "shared-lib/pom.xml": java_adapter.JavaFileResult(units=shared_units, edges=shared_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None


def test_a_numeric_entity_reactor_module_coordinate_still_resolves_internal():
    """FIX ROUND 35 (F1 BLOCKER, .cr29-cdata2 variant - numeric-entity
    form): the reader's own measured "com&#46;acme:mod&#45;a" shape -
    numeric character references (&#46; is '.', &#45; is '-') decode via
    the same _decode_xml_text boundary, never published raw."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>shared-lib</artifactId>"
        "</dependency></dependencies></project>",
    )
    shared_units, shared_edges, _c2 = java_adapter.parse_maven_pom(
        "shared-lib/pom.xml",
        "<project><groupId>com&#46;acme</groupId>"
        "<artifactId>shared&#45;lib</artifactId></project>",
    )
    assert {u.qualified_name for u in shared_units} == {"com.acme:shared-lib"}
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "shared-lib/pom.xml": java_adapter.JavaFileResult(units=shared_units, edges=shared_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None


def test_a_genuinely_external_pom_dependency_still_resolves_external():
    """Companion negative case: a dependency naming NO in-scan pom's own
    coordinate still resolves external as before - the fix only closes
    the specific reactor-internal-miscounted-as-external gap."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
    )
    results = {"app/pom.xml": java_adapter.JavaFileResult(edges=app_edges)}
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external == "org.springframework:spring-core"


def test_a_project_groupid_property_dependency_expands_and_resolves_internal():
    """FIX ROUND 19 (fifteenth cold read, F2 MAJOR, wrong-data): mirrors
    the reader's own ``.cr15-b`` shape - ``${project.groupId}:billing-
    core``, Maven's own documented sibling-dependency idiom (avoiding
    repeating a reactor's shared groupId in every module's own pom).
    billing-core IS registered (round 18's own F3 fix works); the miss
    was purely the unexpanded property in the published target. Now
    expands to the SAME-FILE project's own effective groupId before
    the edge is even constructed, resolving internal."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>${project.groupId}</groupId><artifactId>billing-core</artifactId>"
        "</dependency></dependencies></project>",
    )
    billing_units, billing_edges, _c2 = java_adapter.parse_maven_pom(
        "billing-core/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>billing-core</artifactId></project>",
    )
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "billing-core/pom.xml": java_adapter.JavaFileResult(units=billing_units, edges=billing_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_external is None
    assert build_edge.target_unit_id == da._java_component_unit_id(
        "billing-core/pom.xml", "com.acme:billing-core")


def test_an_unexpandable_pom_property_dependency_stays_unresolved_spelling_retained():
    """FIX ROUND 19 (fifteenth cold read, F2 MAJOR, wrong-data, HARD
    RULE): a coordinate containing ANY property this adapter cannot
    expand from the same file (${custom.prop} - not one of the two
    self-referential properties parse_maven_pom knows how to resolve)
    must never satisfy the positive-grounds external test - unresolved,
    with the property spelling retained verbatim, never silently
    dropped or guessed at."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>${custom.prop}</groupId><artifactId>widget</artifactId>"
        "</dependency></dependencies></project>",
    )
    results = {"app/pom.xml": java_adapter.JavaFileResult(edges=app_edges)}
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "unresolved"
    assert build_edge.target_external is None
    assert build_edge.target_unresolved == "${custom.prop}:widget"


def test_two_poms_declaring_the_same_coordinate_make_the_dependency_ambiguous():
    """FIX ROUND 17 (CR13-4): consistency with B1 - a duplicate pom
    coordinate (two modules both declaring com.acme:shared-lib) must
    never resolve confidently to either claimant; the dependency
    publishes ambiguous with both candidates, the same shape a duplicate
    Java qualified name already gets."""
    _app_units, app_edges, _c1 = java_adapter.parse_maven_pom(
        "app/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>shared-lib</artifactId>"
        "</dependency></dependencies></project>",
    )
    a_units, a_edges, _c2 = java_adapter.parse_maven_pom(
        "a/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>shared-lib</artifactId></project>",
    )
    b_units, b_edges, _c3 = java_adapter.parse_maven_pom(
        "b/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>shared-lib</artifactId></project>",
    )
    results = {
        "app/pom.xml": java_adapter.JavaFileResult(edges=app_edges),
        "a/pom.xml": java_adapter.JavaFileResult(units=a_units, edges=a_edges),
        "b/pom.xml": java_adapter.JavaFileResult(units=b_units, edges=b_edges),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "ambiguous"
    assert build_edge.target_external is None
    assert sorted(build_edge.candidate_unit_ids) == sorted([
        da._java_component_unit_id("a/pom.xml", "com.acme:shared-lib"),
        da._java_component_unit_id("b/pom.xml", "com.acme:shared-lib"),
    ])


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


# ------------------- round 41 (F1+F2, THE STRUCTURAL CURE): pom coordinate identity

def test_two_dependency_coordinates_sharing_a_200_char_prefix_get_distinct_edges():
    """FIX ROUND 41 (thirty-fifth cold read, Part A F1 BLOCKER, .cr35-pom,
    wrong-data): round 40's own S1 audit claimed a pom coordinate was
    "never escaped or bounded in the first place" - FALSE: a pom
    <dependency>'s own groupId:artifactId was bounded (java.py's
    round-13 CR9-6 fix) AND used directly as this edge's own identity
    (hashed for edge_id, exact-matched against the registry). Two
    genuinely different dependency coordinates sharing a >200-char
    groupId prefix used to truncate to the IDENTICAL target string,
    coalescing to one resolved/external edge with zero signal that a
    real, distinct dependency vanished. Fixed (round 41's own
    structural cure): java.py no longer bounds a coordinate at
    extraction at all - the HASH input is now always raw/unbounded.

    CORRECTED (round 42, thirty-sixth cold read, F3 MAJOR, completeness
    - THE RECONCILIATION): this test used to assert `target_external`
    publishes fully raw/unbounded too - round 42 reconciles that
    against the design's own "Bounded derived or declared label"
    promise: `target_external` is a terminal DISPLAY label (never
    re-hashed or re-looked-up once assigned), so it is now bounded at
    display the same way a route/filter's own name already was - only
    `edge_id` (the actual identity) stays computed from the raw value,
    which is what this test's own real point always was."""
    prefix = "com.acme." + "x" * 200
    pom = (
        "<project><groupId>com.acme</groupId><artifactId>consumer</artifactId>"
        "<dependencies>"
        f"<dependency><groupId>{prefix}A</groupId><artifactId>lib</artifactId></dependency>"
        f"<dependency><groupId>{prefix}B</groupId><artifactId>lib</artifactId></dependency>"
        "</dependencies></project>"
    )
    results = {"pom.xml": _parse_pom("pom.xml", pom)}
    records = da.build_dependencies(results)
    build_edges = [r for r in records if r.relation == "build"]
    assert len(build_edges) == 2
    assert len({e.edge_id for e in build_edges}) == 2, (
        "two genuinely different dependency coordinates must not collide on edge_id "
        "merely because they truncate identically"
    )
    # target_external is now a bounded DISPLAY label (round 42's own
    # F3) - both truncate to the identical marker-suffixed string, but
    # edge_id (asserted above) still differs, proving the id is
    # genuinely raw-derived regardless of what the label displays.
    assert len({e.target_external for e in build_edges}) == 1
    assert all(e.target_external.endswith("...(truncated)") for e in build_edges)


def test_a_single_oversized_dependency_coordinate_resolves_using_its_own_real_identity():
    """FIX ROUND 41 (Part A F1, .cr35-pom, contrast control): a single
    oversized dependency coordinate (no collision partner) must resolve
    against a sibling module's OWN identically-oversized, real
    coordinate - proving the registry match itself uses the raw value,
    not a truncated projection that would match neither the real
    in-scan module nor the dependency's own real target."""
    prefix = "com.acme." + "x" * 200
    consumer_pom = (
        "<project><groupId>com.acme</groupId><artifactId>consumer</artifactId>"
        "<dependencies><dependency>"
        f"<groupId>{prefix}</groupId><artifactId>lib</artifactId>"
        "</dependency></dependencies></project>"
    )
    lib_pom = f"<project><groupId>{prefix}</groupId><artifactId>lib</artifactId></project>"
    results = {
        "consumer/pom.xml": _parse_pom("consumer/pom.xml", consumer_pom),
        "lib/pom.xml": _parse_pom("lib/pom.xml", lib_pom),
    }
    records = da.build_dependencies(results)
    build_edge = next(r for r in records if r.relation == "build")
    assert build_edge.resolution_state == "resolved"
    assert build_edge.target_unit_id is not None
    assert build_edge.target_external is None


def test_two_modules_with_own_coordinates_sharing_a_200_char_prefix_do_not_fabricate_a_conflict():
    """FIX ROUND 41 (Part A F2 MAJOR, .cr35-dupcoord, wrong-data): the
    module-own-coordinate twin of F1 above - two DIFFERENT modules'
    OWN groupId:artifactId, sharing a >200-char groupId prefix, used to
    publish the SAME truncated qualified_name, which this producer's
    own registry then reads as a genuine duplicate-qualified-name
    collision (a real registry-conflict mechanism, M12) - fabricating a
    shared conflict_id and a duplicate_qualified_name problem for two
    claims that were never actually in conflict. Fixed the same way as
    F1: published raw, so two genuinely different coordinates are
    genuinely different qualified_names, never spuriously identical."""
    prefix = "com.acme." + "x" * 200
    mod_a_pom = f"<project><groupId>{prefix}A</groupId><artifactId>lib</artifactId></project>"
    mod_b_pom = f"<project><groupId>{prefix}B</groupId><artifactId>lib</artifactId></project>"
    results = {
        "modA/pom.xml": _parse_pom("modA/pom.xml", mod_a_pom),
        "modB/pom.xml": _parse_pom("modB/pom.xml", mod_b_pom),
    }
    all_units = [u for result in results.values() for u in result.units]
    assert len(all_units) == 2
    assert len({u.qualified_name for u in all_units}) == 2, (
        "two genuinely different module coordinates must not collide on qualified_name "
        "merely because they truncate identically"
    )
    # No fabricated conflict: build_dependencies must not need to consult
    # a shared conflict_id for either module's own unresolved dependents
    # here (there are none) - the registry itself (_build_registry) is
    # exercised the same way features_artifact.py's own duplicate-name
    # detection is, via a plain distinct-qualified-name check above,
    # since dependencies_artifact.py has no dependents to resolve in
    # this fixture on its own.


def test_resolve_descriptor_qualified_name_translates_a_dollar_spelled_binary_name():
    """FIX ROUND 46 (fortieth cold read, F2 MAJOR, wrong-data - THE
    DESCRIPTOR GATE IS BLIND TO THE BINARY SPELLING): a real container
    requires the JVM's own binary class name (`$` separates every
    nesting level) in a descriptor - never the source-dotted spelling
    this adapter's own qualified_name always publishes. Translating
    every `$` to `.` resolves it against the source-dotted registry."""
    registry = {"com.acme.Host.NestedAbs": "unit-1"}
    assert da.resolve_descriptor_qualified_name(
        "com.acme.Host$NestedAbs", registry) == "com.acme.Host.NestedAbs"


def test_resolve_descriptor_qualified_name_prefers_an_exact_match_over_translation():
    """FIX ROUND 46 (F2 MAJOR); corrected MICRO-ROUND 46b (reviewer-3's
    own delta): the exact-match-first branch this asserts is defensive,
    harmless behavior for a HYPOTHETICAL non-source-derived registry -
    it is UNREACHABLE for this producer's own real registries, since
    `_TYPE_NAME_ANCHOR_RE`'s own `\\w+` identifier capture can never
    include a `$` character, so no qualified_name this adapter computes
    ever contains one; translation is what decides every real
    resolution here. Locks the exact-first ORDERING as a unit-level
    contract regardless, so a future change to this function cannot
    silently invert it without this test noticing."""
    registry = {"com.acme.Foo$Bar": "literal-unit", "com.acme.Foo.Bar": "nested-unit"}
    assert da.resolve_descriptor_qualified_name("com.acme.Foo$Bar", registry) == "com.acme.Foo$Bar"


def test_resolve_descriptor_qualified_name_leaves_an_unresolvable_name_unchanged():
    """FIX ROUND 46 (F2 MAJOR control): neither the exact spelling nor
    the translated form is present (a jar-shipped class, or a genuine
    typo) - returns the ORIGINAL qualified_name unchanged, so the
    caller's own existing "not in scan" fallback handling applies
    exactly as it always has, never a fabricated resolution."""
    registry: dict[str, str] = {"com.acme.SomethingElse": "unit-1"}
    assert da.resolve_descriptor_qualified_name(
        "com.acme.Host$NestedAbs", registry) == "com.acme.Host$NestedAbs"


def test_resolve_descriptor_qualified_name_is_a_no_op_without_a_dollar_sign():
    """FIX ROUND 46 (F2 MAJOR control): the ordinary, dominant case - a
    descriptor already spelled source-dotted, or naming a top-level
    class - must never even attempt translation (no `$` present at
    all), the identical cheap early-return the dominant real-world
    case already needed before this fix."""
    registry = {"com.acme.Plain": "unit-1"}
    assert da.resolve_descriptor_qualified_name("com.acme.Plain", registry) == "com.acme.Plain"
    assert da.resolve_descriptor_qualified_name("com.acme.NotThere", registry) == "com.acme.NotThere"
