"""#55 slice-1 PR-B item 3: the bundled Java adapter
(DESIGN-55-comprehension-plane.md, Artifact 2's closed relation vocabulary;
approved PR-B plan item-3 relation-scope decision, 2026-08-27). Small,
synthetic fixtures only - not Amperian itself, kept fast and hermetic.
"""

from __future__ import annotations

import pytest

from agenttalk.comprehension.adapters import java


def _edges(result: java.JavaFileResult, relation: str) -> list[java.JavaEdgeClaim]:
    return [e for e in result.edges if e.relation == relation]


# ----------------------------------------------------------- package / imports

def test_extracts_package_and_imports():
    src = """
package com.example.app;

import java.util.List;
import static java.util.Collections.emptyList;
import com.example.other.*;

class Foo {
}
"""
    result = java.parse_java_source("com/example/app/Foo.java", src)
    imports = _edges(result, "import")
    targets = {e.target for e in imports}
    assert targets == {"java.util.List", "java.util.Collections.emptyList", "com.example.other.*"}
    assert all(e.evidence_class == "extracted" for e in imports)
    assert result.units[0].qualified_name == "com.example.app.Foo"

    # D-1 (reviewer-3, PR-B delta review round 2): a plain import gets a
    # shot at exact internal resolution. N5 (fourth cold read, fix round
    # 6): a static import's target is a member path - not itself a shot
    # at exact resolution, but its TYPE PREFIX is - java.util.Collections
    # (not itself in-scan here) still classifies external once resolved.
    # A wildcard NON-static import names a package, never a single type,
    # so it gets its own target_kind (FIX ROUND 16, twelfth cold read,
    # B3 BLOCKER) rather than plain "external" - dependencies_artifact.py
    # still checks whether the package itself is in-scan before ever
    # publishing a confident external claim.
    by_target = {e.target: e for e in imports}
    assert by_target["java.util.List"].target_kind == "internal_exact_or_external"
    assert (
        by_target["java.util.Collections.emptyList"].target_kind
        == "internal_static_import_exact_or_external"
    )
    assert by_target["com.example.other.*"].target_kind == "external_wildcard_import"


def test_import_edge_is_file_scoped_not_attributed_to_the_first_declared_type():
    """FIX ROUND 14 (tenth cold read, CR10-1 MAJOR): an import is a
    FILE-scoped Java fact - every type in the file sees it, regardless
    of which one actually uses it. Publishing it against the FIRST
    declared type (the old behavior) fabricated a type-scoped claim: a
    public-class-plus-package-private-helper file (the everyday legacy
    shape) credited the FIRST class with the helper's own dependency (a
    false edge) while the helper itself published none at all. The
    import edge's own from_qualified_name must never equal either
    declared type's qualified name - dependencies_artifact.py's exact-
    match-or-file-unit fallback then routes it to the FILE unit."""
    src = """
package p;
import java.util.List;
public class Service {
}
class ServiceCache {
}
"""
    result = java.parse_java_source("p/Service.java", src)
    imports = _edges(result, "import")
    assert len(imports) == 1
    assert imports[0].from_qualified_name not in {"p.Service", "p.ServiceCache"}


def test_import_inside_a_comment_is_not_extracted():
    src = """
package p;
// import java.util.List;
/* import java.util.Map; */
class Foo {}
"""
    result = java.parse_java_source("Foo.java", src)
    assert _edges(result, "import") == []


# ----------------------------------------------------------- types / nesting

def test_extracts_a_nested_class_with_correct_qualified_name():
    src = """
package com.example;

class Outer {
    class Inner {
    }
}
"""
    result = java.parse_java_source("com/example/Outer.java", src)
    names = {u.qualified_name for u in result.units}
    assert names == {"com.example.Outer", "com.example.Outer.Inner"}


def test_a_type_nested_three_deep_gets_an_uncorrupted_qualified_name():
    """M-4 (third cold read, fix round 5): a depth-2 nested type's
    container_prefix (one stack entry) happens to look correct even with
    the OLD ``".".join(all stack entries)`` bug, since joining a single
    entry with nothing is a no-op - the bug is invisible exactly where
    the old test stopped. At depth 3, Innermost's prefix used to
    concatenate BOTH Outer's and Inner's already-fully-qualified names
    together: "com.acme.Outer.com.acme.Outer.Inner.Innermost", not
    "com.acme.Outer.Inner.Innermost"."""
    src = """
package com.acme;

class Outer {
    class Inner {
        class Innermost {
        }
    }
}
"""
    result = java.parse_java_source("com/acme/Outer.java", src)
    names = {u.qualified_name for u in result.units}
    assert names == {
        "com.acme.Outer", "com.acme.Outer.Inner", "com.acme.Outer.Inner.Innermost",
    }


def test_a_type_nested_four_deep_gets_an_uncorrupted_qualified_name():
    """M-4: depth 4 compounds the same corruption further at every
    additional level if the bug is present at all - a second, deeper
    data point confirming the fix holds beyond the minimum repro."""
    src = """
package com.acme;

class A {
    class B {
        class C {
            class D {
            }
        }
    }
}
"""
    result = java.parse_java_source("com/acme/A.java", src)
    names = {u.qualified_name for u in result.units}
    assert names == {
        "com.acme.A", "com.acme.A.B", "com.acme.A.B.C", "com.acme.A.B.C.D",
    }


def test_class_name_inside_a_string_literal_is_not_extracted_as_a_type():
    src = """
package p;
class Real {
    String s = "class Fake { }";
}
"""
    result = java.parse_java_source("Real.java", src)
    names = {u.qualified_name for u in result.units}
    assert names == {"p.Real"}


# ----------------------------------------------------------- inherit

def test_extends_and_implements_produce_inherit_edges():
    src = """
package p;
class Foo extends BaseThing implements Runnable, java.io.Closeable {
}
"""
    result = java.parse_java_source("Foo.java", src)
    inherit = _edges(result, "inherit")
    targets = {e.target for e in inherit}
    assert targets == {"BaseThing", "Runnable", "java.io.Closeable"}
    assert all(e.evidence_class == "extracted" for e in inherit)


def test_interface_extends_multiple_interfaces():
    src = """
package p;
interface Combo extends Foo, Bar {
}
"""
    result = java.parse_java_source("Combo.java", src)
    inherit = _edges(result, "inherit")
    assert {e.target for e in inherit} == {"Foo", "Bar"}


# ------------------------------------------------- BLOCKER 1a header battery
# (fifth cold read, fix round 8): _TYPE_HEADER_RE (the old, single fixed-
# shape regex) could not match a bounded/intersection generic parameter
# list, a sealed+permits header, or a record declaration - an unmatched
# header dropped the type SILENTLY: zero units, status complete,
# problem_count 0, on a file this adapter never actually understood. Each
# case below is exactly one of the reviewer's battery shapes; every one
# must extract a real unit, never silently zero.

def test_bounded_generic_type_parameter_is_extracted():
    src = """
package p;
class Box<T extends Comparable<T>> {
    void list() {}
}
"""
    result = java.parse_java_source("Box.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Box"]


def test_intersection_bounded_generic_type_parameter_is_extracted():
    src = """
package p;
class Pair<T extends Number & Comparable<T>> {
    void list() {}
}
"""
    result = java.parse_java_source("Pair.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Pair"]


def test_sealed_class_with_permits_is_extracted():
    src = """
package p;
public sealed class Shape permits Circle, Square {
    void list() {}
}
final class Circle extends Shape {}
final class Square extends Shape {}
"""
    result = java.parse_java_source("Shape.java", src)
    assert {u.qualified_name for u in result.units} == {"p.Shape", "p.Circle", "p.Square"}
    inherit = _edges(result, "inherit")
    assert {e.target for e in inherit if e.from_qualified_name == "p.Circle"} == {"Shape"}


def test_record_declaration_is_extracted():
    src = """
package p;
record Point(int x, int y) implements Comparable<Point> {
    void list() {}
}
"""
    result = java.parse_java_source("Point.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Point"]
    inherit = _edges(result, "inherit")
    assert {e.target for e in inherit} == {"Comparable"}


def test_generic_record_declaration_is_extracted():
    src = """
package p;
record Pair<A, B>(A first, B second) {
}
"""
    result = java.parse_java_source("Pair.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Pair"]


# --------------------------------------- MINOR 4 (sixth cold read, fix round
# 9): "record" is a CONTEXTUAL keyword - unlike class/interface/enum (fully
# reserved), it remains legal as an ordinary identifier. The keyword+
# identifier anchor previously accepted "record" followed by any word as a
# declaration regardless of context, most concretely: a parameter/variable
# literally named "record" immediately followed by the "instanceof" operator
# published a phantom unit named "instanceof". A real record declaration
# always has a component parameter list (even an empty one); requiring it
# closes this false-positive family without narrowing real record support.

def test_record_used_as_a_parameter_name_before_instanceof_is_not_a_phantom_type():
    src = """
package p;
class Controller {
    void process(Object record) {
        if (record instanceof String s) {
            System.out.println(s);
        }
    }
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Controller"]


def test_record_used_as_a_local_variable_name_before_instanceof_is_not_a_phantom_type():
    src = """
package p;
class Controller {
    void process() {
        Object record = compute();
        if (record instanceof String) { }
    }
    Object compute() { return null; }
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Controller"]


def test_record_used_as_a_field_name_is_not_a_phantom_type():
    src = """
package p;
class Controller {
    private Object record;
    void use() {
        Object x = record;
    }
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Controller"]


def test_record_used_as_an_instanceof_pattern_variable_name_is_not_a_phantom_type():
    """Java 16+ instanceof pattern variables can be named "record" too
    (still just an ordinary identifier position)."""
    src = """
package p;
class Controller {
    void process(Object obj) {
        if (obj instanceof String record) {
            System.out.println(record);
        }
    }
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Controller"]


def test_class_literal_followed_by_instanceof_is_not_a_phantom_type():
    """BLOCKER, second report (sixth cold read, fix round 9b): round 9's
    own fix tightened the "record" anchor (a mandatory component list)
    but left a DIFFERENT, also real variant open - a CLASS LITERAL
    (`Foo.class`) is valid Java grammar in any expression position (e.g.
    `String.class instanceof Object`), and "class" there is followed by
    whitespace then an ordinary identifier ("instanceof") - the SAME
    shape a real declaration has. This is the reviewer's own reported
    shape, reproduced exactly (not a cousin): a class-literal
    `instanceof` check inside a normal method body published a phantom
    unit named "instanceof"."""
    src = """
package p;
class Controller {
    void check() {
        if (String.class instanceof Object) {
            System.out.println("weird");
        }
    }
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert [u.qualified_name for u in result.units] == ["p.Controller"]


def test_class_level_request_mapping_composes_on_a_bounded_generic_controller():
    """The end-to-end proving case: a class-level route prefix on a
    header shape the OLD regex could not match must still compose with
    its method-level route - not publish the prefix as its own served
    entry point (the exact pre-M5 wrong shape an unmatched header used
    to reproduce)."""
    src = """
package p;

@RequestMapping("/api/base")
public class Controller<T extends Comparable<T>> {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/base/list"


def test_a_file_with_zero_extracted_types_publishes_no_route_or_entry_point_claims():
    """BLOCKER (sixth cold read, fix round 9): route/entry-point emission
    never consulted whether the file actually yielded any types - a file
    that degrades honestly (zero units, no_types_extracted, unknown,
    degraded) still published the class-level route prefix as its own
    invocable route and the method value as the whole route, declared-
    class, as stable entry-point IDs, all attributed to a SYNTHESIZED
    fallback owner that names no real unit at all.

    Proven with valid Java this adapter cannot see the body of: the
    language decodes \\uXXXX unicode escapes BEFORE lexing (real javac
    compiles this fine - a class body delimited by \\u007B/\\u007D braces
    instead of literal `{`/`}`), but this adapter's sanitizer does not
    decode them, so its own brace-matching never finds the type's body
    at all - zero units - while every OTHER extraction loop (route
    annotations included) keeps scanning the surrounding text
    regardless. Must now publish zero edges/entry points from this file,
    not launder them under a synthesized owner."""
    backslash = chr(92)
    open_brace = backslash + "u007B"
    close_brace = backslash + "u007D"
    src = (
        "package p;\n"
        '@RequestMapping("/api/orders")\n'
        "public class Controller " + open_brace + "\n"
        '    @GetMapping("/list")\n'
        "    void list() " + open_brace + close_brace + "\n"
        + close_brace + "\n"
    )
    result = java.parse_java_source("Controller.java", src)
    assert result.units == []
    assert result.edges == []
    assert result.entry_points == []


# ----------------------------------------------------------- test classification + relation

def test_test_suffixed_class_is_classified_test_and_produces_a_test_edge():
    src = """
package p;
class FooTest {
}
"""
    result = java.parse_java_source("src/test/java/p/FooTest.java", src)
    assert result.units[0].classification == "test"
    test_edges = _edges(result, "test")
    assert len(test_edges) == 1
    assert test_edges[0].target == "Foo"
    assert test_edges[0].phase == "test"


def test_src_test_path_classifies_as_test_even_without_naming_convention():
    src = "package p;\nclass Helper {\n}\n"
    result = java.parse_java_source("src/test/java/p/Helper.java", src)
    assert result.units[0].classification == "test"


def test_ordinary_class_is_classified_production():
    src = "package p;\nclass Widget {\n}\n"
    result = java.parse_java_source("src/main/java/p/Widget.java", src)
    assert result.units[0].classification == "production"


def test_a_production_class_ending_in_it_without_corroboration_stays_production():
    """FIX ROUND 14 (tenth cold read, CR10-7 MINOR, wrong-data, verbatim
    shape): _TEST_NAME_SUFFIX matches any name ending in "IT" (meant for
    JUnit's own "OrderServiceIT" integration-test convention) - so an
    entirely ordinary production class named AUDIT, in src/main/java,
    with no test-framework import anywhere in the file, used to publish
    as unit_type=test with a FABRICATED test edge to the nonexistent
    stripped-suffix target "AUD". A name-suffix hit without CORROBORATING
    evidence (a test source root, or a test-framework import) must stay
    production and emit no test edge at all."""
    src = "package p;\npublic class AUDIT {\n}\n"
    result = java.parse_java_source("src/main/java/p/AUDIT.java", src)
    assert result.units[0].classification == "production"
    assert _edges(result, "test") == []


def test_a_name_suffix_hit_with_a_test_framework_import_corroborates_test_classification():
    """FIX ROUND 14 (CR10-7 control): a name-suffix hit alongside a real
    test-framework import (JUnit) IS corroborated evidence - even
    outside a test source root, this must still classify test and emit
    its test edge, the same as before this fix for the legitimate case."""
    src = "package p;\nimport org.junit.Test;\nclass FooIT {\n}\n"
    result = java.parse_java_source("src/it/java/p/FooIT.java", src)
    assert result.units[0].classification == "test"
    test_edges = _edges(result, "test")
    assert len(test_edges) == 1
    assert test_edges[0].target == "Foo"


def test_a_bare_test_package_segment_without_corroboration_stays_production():
    """FIX ROUND 15 (eleventh cold read, F3 MAJOR, wrong-data, verbatim
    shape): the PATH heuristic classified a bare "/test/" segment with NO
    corroboration at all - the same bug class CR10-7 already fixed for
    the NAME heuristic, left standing for the path one.
    src/main/java/com/lab/test/TestOrder.java (a package literally named
    "test", common in lab/QA-domain legacy code) used to publish
    classification=[test] on a complete run with zero supporting
    evidence. A bare /test/ segment NOT under the build-convention
    src/test/ root, with no test-framework import either, must stay
    production."""
    src = "package com.lab.test;\npublic class TestOrder {\n}\n"
    result = java.parse_java_source("src/main/java/com/lab/test/TestOrder.java", src)
    assert result.units[0].classification == "production"


def test_src_test_java_still_classifies_as_test_with_no_further_corroboration():
    """FIX ROUND 15 (F3 control): the real build-convention root
    (src/test/java) IS sufficient evidence entirely on its own - that's
    what it actually means for Maven/Gradle, not a guess."""
    src = "package p;\npublic class Helper {\n}\n"
    result = java.parse_java_source("src/test/java/p/Helper.java", src)
    assert result.units[0].classification == "test"


def test_a_corroborated_bare_test_segment_still_classifies_as_test():
    """FIX ROUND 15 (F3 control): a bare /test/ segment WITH a same-file
    test-framework import is corroborated the same way the name
    heuristic already is - still classifies test, not silently lost by
    the tightened path rule."""
    src = "package com.lab.test;\nimport org.junit.Test;\npublic class OrderProbe {\n}\n"
    result = java.parse_java_source("src/main/java/com/lab/test/OrderProbe.java", src)
    assert result.units[0].classification == "test"


def test_a_repository_root_test_directory_is_sufficient_alone():
    """FIX ROUND 15b (reviewer-3's MINOR 2, measured on an Ant layout): a
    REPOSITORY-ROOT test/ directory is a build convention exactly like
    src/test (the classic pre-Maven Ant project layout) - sufficient
    alone, no test-framework import needed."""
    src = "package com.acme;\npublic class TestFixtures {\n}\n"
    result = java.parse_java_source("test/com/acme/TestFixtures.java", src)
    assert result.units[0].classification == "test"


def test_a_package_segment_literally_named_test_still_stays_production():
    """FIX ROUND 15b control: the root-anchoring in MINOR 2 must not
    reopen the exact hole F3 closed - a test SEGMENT declared inside a
    package path (not the repository root itself) still needs
    corroboration, or stays production."""
    src = "package com.lab.test;\npublic class TestOrder {\n}\n"
    result = java.parse_java_source("com/lab/test/TestOrder.java", src)
    assert result.units[0].classification == "production"


# ----------------------------------------------------------- invoke

def test_qualified_call_resolves_against_an_import():
    src = """
package p;
import java.util.Collections;
class Foo {
    void run() {
        Collections.emptyList();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = _edges(result, "invoke")
    assert len(invoke) == 1
    assert invoke[0].target == "java.util.Collections"
    # Second cold read, B-1 (fix round 4): an import-mediated call gets the
    # SAME target_kind an import edge itself gets (internal_exact_or_
    # external) - java.util.Collections isn't declared in-scan, so it
    # still resolves external at the dependencies_artifact layer (see
    # test_comprehension_dependencies_artifact.py's coverage of that), but
    # the adapter must never hand out a confident "external" stamp before
    # the registry has even had a chance to look.
    assert invoke[0].target_kind == "internal_exact_or_external"
    assert invoke[0].evidence_class == "extracted"


def test_qualified_call_against_a_locally_declared_type_is_an_internal_candidate():
    src = """
package p;
class Helper {
    static void doWork() {}
}
class Foo {
    void run() {
        Helper.doWork();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = _edges(result, "invoke")
    assert any(e.target == "Helper" and e.target_kind == "internal_candidate" for e in invoke)


def test_an_all_caps_qualifier_mints_no_invoke_edge():
    """FIX ROUND 19 (fifteenth cold read, F8 MINOR, JUDGE - taken):
    LOG.info(...) - a static field access (private static final Logger
    LOG = ...), never a type-qualified call - used to mint an invoke
    edge treating the field as if it were a locally-declared/import-
    unresolved TYPE, since LOG's own uppercase-leading shape looks
    exactly like a type reference to the regex. Java's own ALL_CAPS
    constant-naming convention is now recognized as a heuristic and
    skipped entirely - no edge minted - for a qualifier that is neither
    locally declared nor import-recognized."""
    src = """
package p;
class Foo {
    private static final Logger LOG = LoggerFactory.getLogger(Foo.class);
    void run() {
        LOG.info("hello");
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = _edges(result, "invoke")
    assert not any(e.target == "LOG" for e in invoke)


def test_an_all_caps_locally_declared_type_still_mints_an_invoke_edge():
    """Companion negative case for the F8 JUDGE - the ALL_CAPS skip
    applies ONLY to a qualifier neither locally declared nor import-
    recognized; a genuine locally-declared type that happens to be
    spelled ALL_CAPS (unusual, but legal) must still resolve normally,
    unaffected."""
    src = """
package p;
class CONSTANTS {
    static void reload() {}
}
class Foo {
    void run() {
        CONSTANTS.reload();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = _edges(result, "invoke")
    assert any(e.target == "CONSTANTS" and e.target_kind == "internal_candidate" for e in invoke)


def test_qualified_call_written_fully_qualified_captures_the_full_dotted_qualifier():
    """FIX ROUND 13 (ninth cold read, CR9-1 BLOCKER): a call deliberately
    written fully qualified - the legacy-vs-rewrite migration idiom this
    plane exists to inventory, e.g. `com.acme.legacy.OrderService.lookup(
    )` disambiguating two same-simple-name classes - used to lose its own
    package prefix entirely (only the last dotted segment was captured),
    then get silently REWRITTEN via whichever import happened to bind
    that bare simple name, publishing a dependency on the WRONG class.
    The qualifier must now carry the full dotted spelling the source
    wrote, verbatim - never an import rewrite."""
    src = """
package p;
import com.acme.v2.OrderService;
class MigrationBridge {
    void run() {
        com.acme.legacy.OrderService.lookup();
    }
}
"""
    result = java.parse_java_source("MigrationBridge.java", src)
    invoke = _edges(result, "invoke")
    assert len(invoke) == 1
    assert invoke[0].target == "com.acme.legacy.OrderService"
    # Never "internal_exact_or_external" (the import-rewrite path) - a
    # dotted qualifier is inline-FQN evidence, exact-match-or-unresolved
    # only, the same discipline round 12 applies to inherit/test. FIX
    # ROUND 14 (CR10-2): invoke now shares "internal_candidate" with
    # inherit/test - one ladder, one target_kind, for all three.
    assert invoke[0].target_kind == "internal_candidate"


def test_qualified_call_with_a_package_prefixed_nested_type_captures_the_whole_chain():
    """FIX ROUND 13b (reviewer-3's B2 BLOCKER on round 13): the FIRST cut
    of the CR9-1 fix required the prefix segments to be lowercase-led
    (package-shaped) - so a package-prefixed NESTED type reference
    (`com.acme.Outer.Inner.x()`) still reduced to its bare tail "Inner"
    (since "Outer", capitalized, broke the all-lowercase prefix match),
    which then met the same bare-keyed import table CR9-1 closed one
    door on - CR9-1's exact mechanism through a second door. The
    qualifier must capture the FULL chain regardless of segment case."""
    src = """
package p;
import com.wrong.Inner;
class Foo {
    void run() {
        com.acme.Outer.Inner.x();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = _edges(result, "invoke")
    assert len(invoke) == 1
    assert invoke[0].target == "com.acme.Outer.Inner"
    # FIX ROUND 14 (CR10-2): invoke now shares "internal_candidate" with
    # inherit/test - one ladder, one target_kind, for all three.
    assert invoke[0].target_kind == "internal_candidate"


def test_lowercase_qualifier_still_produces_no_invoke_edge():
    """FIX ROUND 13b (B2 control): a plain lowercase-led qualifier
    (an ordinary local variable/field reference, never a type) must
    still produce no invoke edge at all - the widened prefix must not
    overshoot into treating instance-qualified calls as type-qualified
    ones."""
    src = """
package p;
class Foo {
    void run() {
        myClass.call();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    assert _edges(result, "invoke") == []


def test_qualified_call_with_an_unusual_capitalized_package_segment_keeps_the_full_spelling():
    """FIX ROUND 13b (B2 control): an unusual but legal identifier
    spelling (a capitalized package segment, "Com") must not be
    silently truncated either - the full source spelling is retained
    verbatim, unresolved, never a guessed/truncated variant."""
    src = """
package p;
class Foo {
    void run() {
        Com.acme.Foo.call();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = _edges(result, "invoke")
    assert len(invoke) == 1
    assert invoke[0].target == "Com.acme.Foo"


def test_static_imported_member_qualifier_resolves_to_the_owning_class():
    """FIX ROUND 13 (ninth cold read, CR9-5 MINOR, wrong-data): a
    static-imported member used bare as an invoke qualifier
    (`import static com.acme.Config.LOGGER;` then `LOGGER.info(...)`)
    used to publish the invoke edge against the UNSPLIT member path
    ("com.acme.Config.LOGGER"), while the import edge on the same line
    correctly resolves to the owning class - two edges in one run
    contradicting each other about the same in-scan file. The invoke
    qualifier must resolve to the OWNING CLASS, the same normalization
    the import edge itself already applies."""
    src = """
package p;
import static com.acme.Config.LOGGER;
class Foo {
    void run() {
        LOGGER.info("x");
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = next(e for e in _edges(result, "invoke") if e.target == "com.acme.Config")
    assert invoke.target_kind == "internal_exact_or_external"


def test_invoke_edge_is_attributed_to_the_enclosing_type_not_the_first_declared_type():
    src = """
package p;
class Helper {
    static void doWork() {}
}
class Foo {
    void run() {
        Helper.doWork();
    }
}
"""
    result = java.parse_java_source("Foo.java", src)
    invoke = next(e for e in _edges(result, "invoke") if e.target == "Helper")
    # Note 10 (second cold read, fix round 4): the call is textually inside
    # Foo.run(), not Helper (the FIRST declared type in the file) - a file
    # with more than one top-level type must attribute the edge to the type
    # whose body actually contains the call, not always to the first one.
    assert invoke.from_qualified_name == "p.Foo"


def test_route_edge_and_entry_point_are_attributed_to_the_enclosing_type():
    src = """
package p;
class Other {
}
class Controller {
    @RequestMapping("/api/widgets")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert routes[0].from_qualified_name == "p.Controller"
    http_entry_points = [e for e in result.entry_points if e.kind == "http_route"]
    assert http_entry_points[0].qualified_name == "p.Controller"


# ----------------------------------------------------------- route (declared only)

def test_request_mapping_with_literal_path_produces_a_declared_route():
    src = """
package p;
class Controller {
    @RequestMapping("/api/widgets")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "/api/widgets"
    assert routes[0].evidence_class == "declared"
    assert routes[0].target_kind == "external_route"
    http_entry_points = [e for e in result.entry_points if e.kind == "http_route"]
    assert len(http_entry_points) == 1
    assert http_entry_points[0].name == "/api/widgets"


def test_get_mapping_value_attribute_is_recovered():
    src = """
package p;
class Controller {
    @GetMapping(value = "/api/widgets/{id}")
    void get() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    # M-5 (third cold read, fix round 5): a verb-specific annotation's own
    # HTTP method is now folded into the route's identity.
    assert routes[0].target == "GET /api/widgets/{id}"


def test_get_and_post_on_the_same_path_produce_distinct_route_targets():
    """M-5 (third cold read, fix round 5): a GET and a POST handler on the
    identical path are two different code paths to a migration reader -
    without the HTTP method folded into the route's own identity, both
    produced the SAME target string, and downstream (features_artifact.py)
    the SAME entry_point_id for two genuinely distinct entry points."""
    src = """
package p;
class Controller {
    @GetMapping("/orders")
    void list() {}

    @PostMapping("/orders")
    void create() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    targets = {r.target for r in routes}
    assert targets == {"GET /orders", "POST /orders"}


def test_request_mapping_finds_value_even_when_a_different_attribute_comes_first():
    """M8 (cold-read, PR-B fix round 3): the FIRST string literal in the
    argument list is not necessarily the route path - Spring allows any
    attribute order. Reproduced pre-fix: `produces` before `value` yielded
    an http_route named "application/json", with the real "/orders" route
    absent entirely."""
    src = """
package p;
class Controller {
    @RequestMapping(produces = "application/json", value = "/orders")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "/orders"


def test_route_value_after_a_nested_call_argument_is_not_truncated_away():
    """N10 (third cold read, fix round 5): the old ``\\([^)]*\\)`` regex
    captured up to the FIRST ``)`` found ANYWHERE in the argument list -
    an earlier attribute containing its OWN nested call
    (``someHelper(x, y)``) closed that regex's capture right after its
    own paren, silently losing every attribute after it, including the
    real ``value`` this whole mechanism exists to find. Reproduced
    pre-fix: this fell back to the bare annotation label
    ("p.Controller#RequestMapping"), with "/api/widgets" entirely lost."""
    src = """
package p;
class Controller {
    @RequestMapping(produces = someHelper(x, y), value = "/api/widgets")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "/api/widgets"


def test_route_value_ignores_a_commented_out_line_before_the_live_one():
    """B1 (fourth cold read, fix round 6): value extraction used to match
    the ORIGINAL (unsanitized) text directly - a commented-out `value =`
    line is live text to that match, so it won outright over the real
    one below it, publishing dead code as declared-class evidence AND as
    the entry point's own stable ID. Reproduced pre-fix: this returned
    "/v1/legacy-removed" (the commented-out value), never "/v2/orders"."""
    src = """
package p;
class Controller {
    @RequestMapping(
        // value = "/v1/legacy-removed"
        value = "/v2/orders"
    )
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "/v2/orders"


def test_route_value_ignores_a_block_comment_before_the_live_attribute():
    """B1 (fourth cold read, fix round 6): same class as the line-comment
    case, via a block comment wedged directly in the argument list."""
    src = """
package p;
class Controller {
    @GetMapping(/* value = "/OLD" */ value = "/NEW")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /NEW"


def test_route_value_with_an_escaped_quote_is_not_truncated():
    """B1 (fourth cold read, fix round 6), closing the KNOWN ISSUE named
    in round 5's PR description: an escaped quote inside the route value
    used to truncate the captured content at the escape (the old
    `[^"]*` capture has no concept of escaping). _java_string_literal_content
    reuses _strip_comments_and_strings's own escaped-quote skip, so the
    literal's FULL content is now recovered.

    Minor 6 (fifth cold read, fix round 7): round 6 published the RAW
    source spelling (`\\"`, backslash included - two characters) as the
    route's target and stable ID, rather than the ONE character it
    actually represents at runtime. Asserting exact equality (not a
    substring check, which cannot distinguish a correctly-decoded value
    from a malformed/raw one) against the properly UNESCAPED value."""
    src = r"""
package p;
class Controller {
    @RequestMapping(value = "/api/\"quoted\"/thing")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == '/api/"quoted"/thing'


def test_route_value_as_a_text_block_is_recovered():
    """B1 (fourth cold read, fix round 6): a Java 15+ triple-quoted text
    block is a different string-literal shape entirely (delimited by
    `\"\"\"`, not a single `"`) - _java_string_literal_content handles it
    the same way _strip_comments_and_strings already does when
    sanitizing, so this is recovered rather than mis-parsed as an empty
    or truncated ordinary literal.

    Minor 6 (fifth cold read, fix round 7): round 6 published the RAW
    substring between the `\"\"\"` markers - leading newline and
    indentation included - rather than Java's own incidental-whitespace-
    stripped value (JEP 378). Asserting EXACT equality (a substring
    check cannot distinguish the correctly-normalized value from a
    malformed one that merely happens to contain it)."""
    src = '''
package p;
class Controller {
    @RequestMapping(value = """
        /api/textblock""")
    void list() {}
}
'''
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "/api/textblock"


def test_route_value_as_a_text_block_dedents_using_the_closing_delimiters_own_indentation():
    """LOW-3 (round 7c, reviewer-3 delta on 95d9cd8): the JLS text-block
    algorithm counts the CLOSING DELIMITER's own line toward the common
    minimal indentation even though that line is blank - an earlier
    version excluded it (only non-blank lines counted), diverging from
    javac exactly when the delimiter's own line is indented LESS than
    every content line. Here the delimiter sits at 6 spaces while both
    content lines sit at 8 - each published line must retain exactly the
    2-space difference, not be fully dedented to zero."""
    src = '''
package p;
class Controller {
    @RequestMapping(value = """
        line1
        line2
      """)
    void list() {}
}
'''
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    # FIX ROUND 20 (P1 JUDGE, taken): the dedent still produces a real
    # embedded newline internally - published escaped (\n -> \\n) by
    # _sanitize_route_name_control_chars, never as a raw control
    # character in a published route name.
    assert routes[0].target == "/  line1\\n  line2"


def test_route_value_with_a_bidi_override_or_line_separator_is_escaped_not_raw():
    """FIX ROUND 22 (eighteenth cold read, F6 MINOR, wrong-data):
    _sanitize_route_name_control_chars's own docstring promises "safe,
    single-line, printable text" - but only C0/DEL were ever escaped. A
    RIGHT-TO-LEFT OVERRIDE (U+202E, the classic "Trojan Source" spoofing
    character - it can make a route's own published rendering read
    backwards) and a Unicode LINE SEPARATOR (U+2028, a real line break
    invisible to a C0-only check) both passed through RAW. Both now
    escape to a visible \\uXXXX form, the same choke point the C0
    control-char fix (round 20's own P1) already established."""
    rtl_override = chr(0x202E)
    line_separator = chr(0x2028)
    src = (
        "package p;\n"
        "class Controller {\n"
        f'  @GetMapping("/api{rtl_override}evil{line_separator}end")\n'
        "  void list() {}\n"
        "}\n"
    )
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api\\u202eevil\\u2028end"
    assert rtl_override not in routes[0].target
    assert line_separator not in routes[0].target


def test_route_value_with_an_arabic_letter_mark_is_escaped_not_raw():
    """FIX ROUND 22b (reviewer-3's delta on round 22, R5, wrong-data):
    U+061C ARABIC LETTER MARK is the THIRD implicit directional mark by
    the escape set's own stated criterion (Unicode 6.3 added ALM
    alongside the isolate controls already in the set) - previously
    missing, passed through RAW."""
    alm = chr(0x061C)
    src = (
        "package p;\n"
        "class Controller {\n"
        f'  @GetMapping("/api{alm}end")\n'
        "  void list() {}\n"
        "}\n"
    )
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api\\u061cend"
    assert alm not in routes[0].target


def test_route_value_with_a_c1_control_character_nel_is_escaped_not_raw():
    """FIX ROUND 23 (nineteenth cold read, F5 LOW, wrong-data): the C1
    control block (U+0080-U+009F, including U+0085 NEL - a line
    terminator in XML 1.1 and many renderers) sits under the SAME
    control-character criterion as C0/DEL, not the Unicode-
    exhaustiveness rule this file's own BIDI/line-separator set
    deliberately declines to chase - it was simply missing. U+00A0/
    U+200B stay out (not control characters)."""
    nel = chr(0x0085)
    src = (
        "package p;\n"
        "class Controller {\n"
        f'  @GetMapping("/api{nel}end")\n'
        "  void list() {}\n"
        "}\n"
    )
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api\\x85end"
    assert nel not in routes[0].target


# ----------------------------------------------------------- malformed java (round 15 F5)

def test_an_unterminated_char_literal_is_detected_as_malformed_not_silently_truncated():
    """FIX ROUND 15 (eleventh cold read, F5 MAJOR, wrong-data, cr11-fx10
    verbatim): genuinely malformed Java (an unterminated char literal)
    made the sanitizer blank the rest of the file silently - PathUtil
    (declared BEFORE the malformed literal) still publishes, but
    FileController (declared after, with two routes) vanishes with NO
    problem recorded, and because PathUtil alone means units is
    non-empty, the zero-types guard never fires either. The sanitizer
    now detects it reached EOF still inside the unterminated literal and
    reports it."""
    src = (
        "package p;\n"
        "class PathUtil {\n"
        "  char bad = '\n"
        "class FileController {\n"
        '  @GetMapping("/one") void a() {}\n'
        '  @GetMapping("/two") void b() {}\n'
        "}\n"
    )
    result = java.parse_java_source("Mixed.java", src)
    assert [u.qualified_name for u in result.units] == ["p.PathUtil"]
    assert _edges(result, "route") == []
    assert any(p.reason_code == "parse_failed" for p in result.problems)


def test_an_unclosed_block_comment_is_detected_as_malformed_not_silently_truncated():
    """FIX ROUND 15 (F5 MAJOR, wrong-data): the second reviewer-verified
    trigger shape - an unclosed /* block comment swallows the rest of
    the file the same way an unterminated char literal does."""
    src = (
        "package p;\n"
        "class PathUtil {\n"
        "}\n"
        "/* comment that never closes\n"
        "class FileController {\n"
        '  @GetMapping("/one") void a() {}\n'
        "}\n"
    )
    result = java.parse_java_source("Mixed2.java", src)
    assert [u.qualified_name for u in result.units] == ["p.PathUtil"]
    assert _edges(result, "route") == []
    assert any(p.reason_code == "parse_failed" for p in result.problems)


def test_sixteen_valid_literal_and_comment_shapes_are_never_flagged_malformed():
    """FIX ROUND 15 (F5 regression battery): the reviewer explicitly
    verified 16 valid literal/comment shapes all sanitize correctly -
    the trigger is malformed input only. Every legal shape here must
    report malformed=False; none of them ends a comment/string/char
    construct exactly at EOF without a closing marker."""
    valid_sources = [
        "class A { }",  # no literals/comments at all
        "class A { } // trailing line comment, no newline",
        "class A { } // trailing line comment\n",
        "class A { /* a block comment */ }",
        "class A { /** a javadoc comment */ }",
        "class A { /* multi\nline\ncomment */ }",
        'class A { String s = "hello"; }',
        'class A { String s = ""; }',
        r'class A { String s = "esc\"aped"; }',
        r'class A { String s = "trailing backslash-backslash\\"; }',
        "class A { char c = 'x'; }",
        r"class A { char c = '\''; }",
        r"class A { char c = '\\'; }",
        'class A { String s = """\n  text block\n  """; }',
        'class A { String s = """already closed""" + "more"; }',
        "class A { int x = 1; /* trailing comment at very end */ }",
    ]
    assert len(valid_sources) == 16
    for src in valid_sources:
        _sanitized, malformed = java._strip_comments_and_strings(src)
        assert malformed is False, f"wrongly flagged malformed: {src!r}"


def test_standalone_method_route_without_a_leading_slash_is_normalized_like_a_composed_one():
    """LOW-2 (round 7c, reviewer-3 delta on 95d9cd8): leading-slash
    normalization previously lived ONLY inside _compose_route_path (the
    class-prefix half) - a STANDALONE method route (no class-level
    prefix at all) with no leading ``/`` of its own published exactly as
    written, while an otherwise-identical route composed with even an
    empty class prefix got normalized. Two spellings for the same served
    path depending on something the route itself has no say over."""
    src = """
package p;
class Controller {
    @GetMapping("list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /list"


def test_request_mapping_path_attribute_is_recovered_ahead_of_an_unrelated_literal():
    src = """
package p;
class Controller {
    @RequestMapping(method = "GET", path = "/orders")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert routes[0].target == "/orders"


def test_two_request_mappings_on_one_path_distinguished_by_method_attribute_do_not_coalesce():
    """N2 (fifth cold read, fix round 8): a plain @RequestMapping's own
    method = RequestMethod.X attribute was never parsed at all - two
    @RequestMapping routes on the SAME path, differing only by this
    attribute, both published with no method prefix (unlike
    @GetMapping/@PostMapping, which fold their own verb implicitly) and
    silently coalesced into ONE entry point by round 5's own coalescing
    rule - correct for a genuine duplicate, wrong here since these are
    two different handlers."""
    src = """
package p;
class Controller {
    @RequestMapping(value = "/orders", method = RequestMethod.GET)
    void list() {}

    @RequestMapping(value = "/orders", method = RequestMethod.POST)
    void create() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert {r.target for r in routes} == {"GET /orders", "POST /orders"}
    http_entry_points = [e for e in result.entry_points if e.kind == "http_route"]
    assert {e.name for e in http_entry_points} == {"GET /orders", "POST /orders"}


def test_route_value_recovery_continues_past_a_non_literal_named_attribute_match():
    """Minor 5 (fifth cold read, fix round 7): round 6 took only the
    FIRST value|path attribute-name match and required an IMMEDIATELY
    following literal - a non-literal path attribute (here, a call
    expression) ahead of the real, literal value attribute made the
    whole function give up where it previously recovered a route (a
    coverage narrowing versus pre-round-6 behavior). Recovery must
    continue searching past the non-literal match instead."""
    src = """
package p;
class Controller {
    @RequestMapping(path = someExpr(), value = "/orders")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "/orders"


def test_route_target_is_length_bounded_not_stored_as_an_unbounded_raw_excerpt():
    """invariant 3 (design: "must not store... string-literal bodies") -
    a route target is a normalized identifier, never an unbounded raw
    source excerpt; an oversized literal is truncated rather than copied
    verbatim regardless of size."""
    from agenttalk.comprehension.adapters.java import _MAX_ROUTE_TARGET_LENGTH

    oversized = "/" + ("x" * (_MAX_ROUTE_TARGET_LENGTH + 50))
    src = f"""
package p;
class Controller {{
    @RequestMapping("{oversized}")
    void list() {{}}
}}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes[0].target) <= _MAX_ROUTE_TARGET_LENGTH + len("...(truncated)")
    assert routes[0].target != oversized


def test_a_route_annotation_with_an_embedded_newline_publishes_an_escaped_name():
    """FIX ROUND 20 (sixteenth cold read, P1 JUDGE, taken): a Java text
    block can carry a RAW newline directly inside a string literal
    (JEP 378) - the annotation's own escapes decode per Java semantics
    (Minor 6, round 7), so this legitimately decodes to a route value
    containing an actual control character. A published name with a raw
    '\\n' is hostile to every downstream consumer (problems.json/
    dependencies.json/features.json, a CLI table, a future UI) - escaped
    to a visible, printable representation rather than published raw."""
    src = '''
package p;
class Controller {
    @RequestMapping("""
/orders
/more
""")
    void list() {}
}
'''
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert "\n" not in routes[0].target
    assert "\\n" in routes[0].target


def test_bare_request_mapping_with_no_path_still_produces_a_named_route():
    src = """
package p;
class Controller {
    @RequestMapping
    void handle() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "p.Controller#RequestMapping"


def test_class_level_request_mapping_composes_with_a_method_level_route():
    """M5 (fourth cold read, fix round 6; fixture corrected fifth cold
    read, fix round 7): a class-level @RequestMapping prefix and a
    method-level route value used to publish as two INDEPENDENT routes -
    the method's own published value was a bare fragment of the actually
    -served path ("/list" published, "/api/orders/list" actually
    served) in the field named for the whole route. Composition is
    Spring's own declared semantics, not inference. The bare class-level
    annotation itself (no method mapping of its own) must not ALSO
    publish as its own route.

    Round 7: the header carries a modifier (``public``) deliberately -
    round 6's own fixture used a BARE ``class Controller {`` with no
    modifier, which accidentally never exercised the walk's failure mode
    (the type header's match position sits AFTER any modifier keyword;
    round 6's walk only skipped whitespace and bare annotations, landing
    short of the header - and silently failing - on every MODIFIED
    declaration, i.e. almost every real one). A bare declaration cannot
    prove this fix; this one can and does."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/orders/list"
    http_entry_points = [e for e in result.entry_points if e.kind == "http_route"]
    assert len(http_entry_points) == 1
    assert http_entry_points[0].name == "GET /api/orders/list"


def test_class_level_request_mapping_composes_on_a_modified_interface():
    """M5 (fifth cold read, fix round 7): the type header regex matches
    ``class``, ``interface``, or ``enum`` alike, and Spring's own
    declared composition semantics apply the same way to an interface
    carrying a class-level mapping - the dispatch's explicit second
    fixture shape ("plus an interface case")."""
    src = """
package p;

@RequestMapping("/api/orders")
public interface Controller {
    @GetMapping("/list")
    void list();
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/orders/list"


def test_method_level_route_with_no_class_level_mapping_is_unchanged():
    """M5 (fourth cold read, fix round 6): a class with no class-level
    route annotation at all must publish the method's own route exactly
    as before - composition only applies when a class-level prefix
    genuinely exists."""
    src = """
package p;
class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /list"


def test_class_level_request_mapping_with_a_valueless_method_annotation_uses_the_prefix_alone():
    """M5 composition note (fifth cold read, fix round 7): a bare
    ``@GetMapping`` (no value of its own) inside a prefixed class still
    serves the class's own prefix in Spring - round 6 gated composition
    on the method having its own literal, so a valueless method
    annotation fell through to the synthetic ``Type#Annotation``
    fallback and LOST the class prefix entirely rather than publishing
    it alone."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller {
    @GetMapping
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/orders"


def test_class_level_request_mapping_prefix_without_a_leading_slash_still_composes():
    """M5 composition note (fifth cold read, fix round 7): a class-level
    prefix lacking its own leading ``/`` (unusual but syntactically
    valid) must still compose into an absolute-looking route, not a
    relative fragment."""
    src = """
package p;

@RequestMapping("orders")
public final class Controller {
    @GetMapping("list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /orders/list"


def test_class_level_request_mapping_survives_a_stacked_annotation_with_nested_parens():
    """M5 composition note (fifth cold read, fix round 7): a stacked
    annotation between the route annotation and the type header may
    carry its OWN nested-paren argument (e.g. a static-method-call
    default) - the walk must skip it depth-aware, exactly like
    _matching_close_paren already does for the route annotation's own
    arguments, not give up and silently drop the composition."""
    src = """
package p;

@RequestMapping("/api/orders")
@ConditionalOnProperty(name = "x", havingValue = String.valueOf(true))
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/orders/list"


# ------------------------------------ B1 (sixth cold read, fix round 10):
# the composition-walk class recurred a THIRD time (round 6 M5, round 7
# B1, now this) - each fix enumerated the trivia grammar and the next
# ordinary shape fell outside it. Structural order: anchor BACKWARD from
# each extracted type header instead of walking forward across an
# enumerated grammar - these are the reviewer's own two proving cases.

def test_class_level_request_mapping_survives_a_fully_qualified_stacked_annotation():
    """Proving case (a): a FULLY-QUALIFIED stacked annotation - the dot in
    ``org.springframework...`` stopped the old forward walk's bare ``@\\w+``
    match, resurrecting the phantom prefix-as-route bug."""
    src = """
package p;

@RequestMapping("/api/orders")
@org.springframework.stereotype.Component
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/orders/list"
    assert result.problems == []


def test_class_level_request_mapping_survives_a_non_sealed_modifier():
    """Proving case (b): the ``non-sealed`` modifier - the hyphen stopped
    the old forward walk's enumerated-keyword identifier match."""
    src = """
package p;

sealed interface Shape permits Controller {}

@RequestMapping("/api/orders")
public non-sealed class Controller implements Shape {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert len(routes) == 1
    assert routes[0].target == "GET /api/orders/list"
    assert result.problems == []


def test_cannot_associate_route_annotation_is_suppressed_with_a_problem_not_published():
    """Fail-safe direction (fix round 10, the class-closer): when
    association still cannot be established for a class-level-looking
    route annotation - here, a stray statement terminator breaks the
    declaration-trivia span backward anchoring requires - the outcome
    must be suppression + a named problem, NEVER the annotation's own
    literal value published as if it were a complete route."""
    src = """
package p;

@RequestMapping("/api/orders");
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    # The real method-level route still gets published - uncomposed,
    # since the broken class-level annotation never registers a prefix -
    # but the broken annotation's OWN literal is never published as a
    # route in its own right, and the enclosing class name is untouched.
    assert [r.target for r in routes] == ["GET /list"]
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "route_annotation_unassociated"
    assert "could not be confidently associated" in result.problems[0].detail


# --------------------------------------- round 10b (reviewer-3 delta on round
# 10): @interface is a legitimate non-route context - suppress WITHOUT the
# false problem (the same shape package-info.java/module-info.java already
# get in worker.py).

def test_route_annotation_on_a_public_annotation_type_declaration_is_silent():
    """Realistic spelling 1: `public @interface` with modifiers/other
    stacked annotations in between - exactly how Spring itself defines
    @GetMapping et al. Suppression is correct (a meta-annotation
    declaration serves no route of its own); a problem record is not -
    this is a documented, common idiom, not an unforeseen shape."""
    src = """
package p;

@Target(java.lang.annotation.ElementType.METHOD)
@Retention(java.lang.annotation.RetentionPolicy.RUNTIME)
@RequestMapping(method = RequestMethod.GET)
public @interface GetMapping2 {
    String value() default "";
}
"""
    result = java.parse_java_source("GetMapping2.java", src)
    assert _edges(result, "route") == []
    assert result.problems == []
    assert [u.qualified_name for u in result.units] == ["p.GetMapping2"]


def test_route_annotation_on_a_bare_annotation_type_declaration_is_silent():
    """Realistic spelling 2: a bare `@interface`, no modifier, the route
    annotation directly stacked with nothing else in between."""
    src = """
package p;

@RequestMapping(method = RequestMethod.POST)
@interface PostMapping2 {
}
"""
    result = java.parse_java_source("PostMapping2.java", src)
    assert _edges(result, "route") == []
    assert result.problems == []
    assert [u.qualified_name for u in result.units] == ["p.PostMapping2"]


def test_annotation_type_declaration_still_yields_exactly_one_unit():
    """Round 10c checkpoint question (does treating @interface as a
    first-class extracted header ADD a unit to modules.json?): no - an
    annotation-type declaration was ALREADY extracted as a unit before
    this round (only its route-annotation ASSOCIATION changed here), and
    it remains an honest component unit: a genuinely declared type in
    the file, migration-relevant the same as any other declared type."""
    src = """
package p;
public @interface GetMapping2 {
    String value() default "";
}
"""
    result = java.parse_java_source("GetMapping2.java", src)
    assert len(result.units) == 1
    assert result.units[0].qualified_name == "p.GetMapping2"


# ------------------------------------------ round 10c (reviewer-3 delta on
# round 10b): make @interface a first-class extracted header (span starting
# at its own `@`) instead of a nearest-following-extracted-header proximity
# exemption - the exemption had no adjacency requirement, so when the
# genuinely-offending declaration was itself unmatchable (absent from the
# extracted list), the old test skipped past it to an UNRELATED @interface
# later in the file and wrongly exempted it (visibility loss: the fail-safe's
# problem record went missing exactly where it should have fired).

def test_route_annotation_before_an_unmatchable_header_still_flags_even_with_a_later_interface():
    """The leak battery, ordering 1: a genuinely-offending route
    annotation (stacked on a class whose OWN header is unmatchable - an
    unterminated generic bound our depth-aware scanner cannot close, so
    it never reaches the extracted types list at all) must still be
    flagged as unassociated, REGARDLESS of an unrelated @interface
    declared later in the same file. Round 10b's proximity exemption
    would have wrongly skipped past the missing header to this later,
    unrelated @interface and silently exempted it."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller<T extends Comparable<T {
    @GetMapping("/list")
    void list() {}
}

@interface Unrelated {
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    assert len(result.problems) == 2
    assert all(p.reason_code == "route_annotation_unassociated" for p in result.problems)
    assert all("could not be confidently associated" in p.detail for p in result.problems)
    assert [u.qualified_name for u in result.units] == ["p.Unrelated"]


def test_route_annotation_before_an_unmatchable_header_flags_with_a_later_ordinary_interface():
    """The normal-interface discriminator (unmatchable-header control):
    an ordinary (non-annotation) `interface` declared later in the file
    must never be mistaken for an `@interface` and must never suppress
    the problem - only a REAL annotation-type declaration's own leading
    `@` does that. Isolates that the problem fires because of the
    unmatchable header itself, not merely because a later type happens
    to exist."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller<T extends Comparable<T {
    @GetMapping("/list")
    void list() {}
}

interface Unrelated {
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    assert len(result.problems) == 2
    assert all(p.reason_code == "route_annotation_unassociated" for p in result.problems)
    assert all("could not be confidently associated" in p.detail for p in result.problems)
    assert [u.qualified_name for u in result.units] == ["p.Unrelated"]


def test_route_value_multi_element_array_publishes_every_element():
    """MAJOR 1 (sixth cold read, fix round 10): a declared multi-value
    route array used to publish only its first path, silently dropping
    every other declared route."""
    src = """
package p;
class Controller {
    @GetMapping({"/list", "/all"})
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert {r.target for r in routes} == {"GET /list", "GET /all"}
    http_entry_points = {e.name for e in result.entry_points if e.kind == "http_route"}
    assert http_entry_points == {"GET /list", "GET /all"}


def test_multi_value_class_prefix_composes_every_prefix_element():
    """MAJOR 1 (sixth cold read, fix round 10): a multi-value class-level
    prefix composes EVERY element against each method-level route, not
    just the first."""
    src = """
package p;

@RequestMapping({"/a", "/b"})
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert {r.target for r in routes} == {"GET /a/list", "GET /b/list"}


def test_braced_multi_value_method_attribute_does_not_coalesce_two_handlers():
    """N4 fold-in (sixth cold read, fix round 10): the array-literal
    shorthand also applies to a @RequestMapping's own ``method = {...}``
    attribute - the old regex read only the first RequestMethod.X inside
    it, silently re-coalescing two distinct handlers into one."""
    src = """
package p;
class Controller {
    @RequestMapping(value = "/thing", method = {RequestMethod.GET, RequestMethod.POST})
    void thing() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert {r.target for r in routes} == {"GET /thing", "POST /thing"}


# --------------------------------------------- round 11 (seventh cold read):
# the route pipeline has THREE stages - recognize the annotation, associate
# it, recover its value. Round 10 de-enumerated ASSOCIATION only; recognition
# was still six enumerated simple names (a fully-qualified route annotation
# was invisible), and value recovery was literal-only (a constant reference,
# a concatenation, or a qualified method spelling silently composed against
# an implicit EMPTY value instead of being treated as unknown).

def test_fully_qualified_class_level_request_mapping_still_composes():
    """B1 shape 1: a fully-qualified class-level @RequestMapping used to
    be invisible to recognition entirely - no prefix ever registered,
    so the method published as a bare fragment (GET /list for a served
    /api/orders/list)."""
    src = """
package p;

@org.springframework.web.bind.annotation.RequestMapping("/api/orders")
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["GET /api/orders/list"]
    assert result.problems == []


def test_both_annotations_fully_qualified_still_composes():
    """B1 shape 5: when BOTH the class-level and method-level annotations
    are fully qualified, recognition used to see neither - zero entry
    points, completely silent (no problem either, since an annotation
    that is never recognized at all never reaches the fail-safe)."""
    src = """
package p;

@org.springframework.web.bind.annotation.RequestMapping("/api/orders")
public class Controller {
    @org.springframework.web.bind.annotation.GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["GET /api/orders/list"]
    assert result.problems == []


def test_class_level_constant_reference_value_is_unrecoverable_not_an_empty_prefix():
    """B1 shape 2: a class-level route annotation whose value is a
    CONSTANT REFERENCE (not a literal) used to silently register NO
    prefix at all (indistinguishable from a genuinely valueless
    annotation) - the method then published as a bare, uncomposed
    fragment. The fail-safe must suppress the whole class's routes and
    name why, not guess an empty prefix."""
    src = """
package p;

@RequestMapping(ApiPaths.ORDERS)
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    # FIX ROUND 20 (sixteenth cold read, M3 MAJOR, wrong-data): the
    # class-level annotation's OWN unrecoverable value now also records
    # its own problem (attributed to the class), alongside the existing
    # method-level fail-safe that fires because the class's own prefix
    # is unrecoverable - two DISTINCT facts about the same class, both
    # visible, never just the method-level half.
    assert len(result.problems) == 2
    assert all(p.reason_code == "route_value_unrecoverable" for p in result.problems)
    assert any(
        "could not be recovered as a literal" in p.detail and p.qualified_name == "p.Controller"
        for p in result.problems
    )


def test_class_level_string_concatenation_is_unrecoverable_not_a_fabricated_fragment():
    """B1 shape 3: a class-level value built from STRING CONCATENATION
    used to silently recover only its FIRST literal segment (the parser
    has no concept of `+` at all) - "/api" + "/orders" registered just
    "/api" as the prefix, composing with the method's own "/list" into
    "GET /api/list", a path the application never actually serves. A
    fabrication worse than a bare fragment - must be unrecoverable, not
    a truncated guess."""
    src = """
package p;

@RequestMapping("/api" + "/orders")
public class Controller {
    @GetMapping("/list")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    # FIX ROUND 20 (sixteenth cold read, M3 MAJOR, wrong-data): same as
    # the constant-reference shape above - the class-level annotation's
    # own unrecoverable value now records its own problem too.
    assert len(result.problems) == 2
    assert all(p.reason_code == "route_value_unrecoverable" for p in result.problems)


def test_method_level_constant_reference_value_is_unrecoverable_not_the_bare_class_prefix():
    """B1 shape 4: a METHOD-level route annotation whose own value is a
    constant reference used to fall into the "genuinely valueless"
    composition branch (an empty method value), publishing the CLASS's
    own prefix alone as if it were the method's complete route - this is
    the exact blind spot a "no value" check cannot see: can't-read and
    doesn't-exist must never collapse to the same outcome."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller {
    @GetMapping(SomeConstants.LIST)
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "route_value_unrecoverable"
    assert "could not be recovered as a literal" in result.problems[0].detail


def test_route_array_with_one_unrecoverable_element_is_unrecoverable_not_truncated():
    """The same silent-truncation risk as string concatenation, but for
    the array-literal shorthand: a mix of a real literal and a constant
    reference must never publish just the literal element(s) as if the
    array held only those."""
    src = """
package p;
class Controller {
    @GetMapping({"/list", SOME_CONST})
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "route_value_unrecoverable"


def test_genuinely_valueless_method_annotation_still_composes_the_prefix_alone():
    """The blind spot the reviewer named: a bare @GetMapping with
    genuinely NO value at all is legitimate (Spring's own "serves the
    prefix alone" semantics) and must still compose normally - never
    conflated with the can't-read case above, which looks similar
    (both end up "no method-level literal") but means something
    completely different."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller {
    @GetMapping
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["GET /api/orders"]
    assert result.problems == []


def test_static_imported_bare_method_constant_does_not_coalesce_two_handlers():
    """N1: a static-imported bare enum constant (`method = GET`, no
    `RequestMethod.` qualifier present in the source at all) used to go
    unrecognized, silently coalescing with a differently-qualified
    sibling into one identical, method-less target."""
    src = """
package p;
class Controller {
    @RequestMapping(value = "/thing", method = GET)
    void getThing() {}
    @RequestMapping(value = "/thing", method = POST)
    void postThing() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert {r.target for r in routes} == {"GET /thing", "POST /thing"}


def test_fully_qualified_method_constant_does_not_coalesce_two_handlers():
    """N1: a fully-qualified `method = org...RequestMethod.X` spelling
    used to go unrecognized the same way (the old regex required the
    literal substring "RequestMethod." positioned immediately after
    `method =`, which a package-qualified spelling never satisfies)."""
    src = """
package p;
class Controller {
    @RequestMapping(value = "/thing", method = org.springframework.web.bind.annotation.RequestMethod.GET)
    void getThing() {}
    @RequestMapping(value = "/thing", method = RequestMethod.POST)
    void postThing() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert {r.target for r in routes} == {"GET /thing", "POST /thing"}


def test_non_enum_method_value_never_publishes_as_an_invented_http_verb():
    """FIX ROUND 13 (ninth cold read, CR9-4 MINOR, wrong-data): a
    non-enum method= value (a random constant, not one of Spring's
    closed RequestMethod constants) used to publish VERBATIM as if it
    were a real HTTP verb - method=HttpConstants.READ_METHOD yielded
    entry point "READ_METHOD /api/orders", a verb this tool invented.
    The invalid token is dropped, falling back to the bare,
    method-unknown path - the same legitimate state a plain
    @RequestMapping with no method attribute already publishes, never a
    suppressed route (the path itself is genuine, correctly-recovered
    evidence)."""
    src = """
package p;
class Controller {
    @RequestMapping(value = "/api/orders", method = HttpConstants.READ_METHOD)
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["/api/orders"]
    assert result.problems == []


def test_request_method_values_array_index_is_also_validated():
    """CR9-4: the same non-enum discipline applies to EVERY recovered
    token in a multi-value method={...} attribute, not just a bare
    single value."""
    src = """
package p;
class Controller {
    @RequestMapping(value = "/api/orders", method = RequestMethod.values()[0])
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    # Whatever bare token(s) the recovery regex pulls out of this
    # non-constant expression, none is a real RequestMethod constant -
    # all dropped, same bare-path fallback as the single-value case.
    assert [r.target for r in routes] == ["/api/orders"]
    assert result.problems == []


# ----------------------------------------------------------- CR9-3: produces/consumes-only mapping

def test_get_mapping_with_only_a_produces_attribute_still_composes_the_prefix():
    """FIX ROUND 13 (ninth cold read, CR9-3 MAJOR, completeness):
    @GetMapping(produces="application/json") with NO value/path
    attribute at all is ordinary Spring - it serves the class's own
    prefix, exactly like a bare @GetMapping. The mere PRESENCE of an
    unrelated named attribute must never flip this into the
    unrecoverable-value case (there is no value expression here to fail
    to recover in the first place)."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller {
    @GetMapping(produces = "application/json")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["GET /api/orders"]
    assert result.problems == []


def test_post_mapping_with_consumes_and_produces_still_composes_the_prefix():
    """CR9-3: more than one unrelated named attribute ahead of a
    genuinely absent value/path must not change the outcome."""
    src = """
package p;

@RequestMapping("/api/orders")
public class Controller {
    @PostMapping(consumes = "application/json", produces = "application/json")
    void create() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["POST /api/orders"]
    assert result.problems == []


def test_get_mapping_produces_only_alongside_a_genuinely_unrecoverable_value_stays_suppressed():
    """CR9-3 must not overcorrect: an attribute that IS an attempted
    value/path but unreadable (a constant reference) must still suppress
    the route as unrecoverable - only a DIFFERENT-named-attribute-only
    shape is legitimately valueless."""
    src = """
package p;
class Controller {
    @GetMapping(value = SomeConstants.LIST, produces = "application/json")
    void list() {}
}
"""
    result = java.parse_java_source("Controller.java", src)
    assert _edges(result, "route") == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "route_value_unrecoverable"


# ----------------------------------------------------------- entry points

def test_main_method_is_a_cli_main_entry_point():
    src = """
package p;
class App {
    public static void main(String[] args) {
    }
}
"""
    result = java.parse_java_source("App.java", src)
    mains = [e for e in result.entry_points if e.kind == "cli_main"]
    assert len(mains) == 1
    assert mains[0].qualified_name == "p.App"


def test_no_main_method_means_no_cli_main_entry_point():
    src = "package p;\nclass Widget {\n}\n"
    result = java.parse_java_source("Widget.java", src)
    assert [e for e in result.entry_points if e.kind == "cli_main"] == []


#: FIX ROUND 13 (ninth cold read, CR9-2 MAJOR): the reviewer's own 9-row
#: legal-spelling matrix for `public static void main` - 3 rows the old
#: single-token-sequence regex already matched (anchors, must stay
#: green), 6 it silently missed (must now be detected). Every row is a
#: real, compiler-legal Java main signature.
_MAIN_SIGNATURE_MATRIX = [
    ("baseline", "public static void main(String[] args)"),
    ("varargs", "public static void main(String... args)"),
    ("modifier order swapped", "static public void main(String[] args)"),
    ("C-style array after the param name", "public static void main(String args[])"),
    ("final parameter", "public static void main(final String[] args)"),
    ("fully-qualified java.lang.String", "public static void main(java.lang.String[] args)"),
    ("no space before the array brackets", "public static void main(String []args)"),
    ("extra synchronized modifier", "public synchronized static void main(String[] args)"),
    ("synchronized between static and void", "public static synchronized void main(String[] args)"),
    # FIX ROUND 13b (reviewer-3's B1 BLOCKER on round 13): the round-13
    # "total for the legal grammar" claim was false - these five legal
    # spellings were still silently missed.
    ("annotation before the modifiers", "@Deprecated\n    public static void main(String[] args)"),
    ("annotation interleaved between modifiers", "public @Deprecated static void main(String[] args)"),
    ("type-parameter section (JLS 8.4, never actually generic in valid "
     "use, but the grammar allows the token)",
     "public static <T> void main(String[] args)"),
    ("JSR-308 annotation before the parameter type", "public static void main(@NotNull String[] args)"),
    ("JSR-308 annotation on the array itself", "public static void main(String @NotNull [] args)"),
]


@pytest.mark.parametrize("shape_name,signature", _MAIN_SIGNATURE_MATRIX)
def test_main_signature_matrix_every_legal_spelling_is_detected(shape_name, signature):
    src = f"package p;\nclass App {{\n    {signature} {{\n    }}\n}}\n"
    result = java.parse_java_source("App.java", src)
    mains = [e for e in result.entry_points if e.kind == "cli_main"]
    assert len(mains) == 1, f"shape not detected: {shape_name} ({signature!r})"
    assert mains[0].qualified_name == "p.App"
    assert result.problems == []


def test_main_without_both_public_and_static_is_not_a_cli_main_entry_point():
    """De-enumerating to "public and static, any order, any other
    modifiers alongside" must not overshoot into treating ANY modifier
    combination as main - a package-private or instance `main` (missing
    `public`, or missing `static`) is not a JVM entry point at all. This
    shape IS fully recognized by the strict matcher (just confidently
    rejected for a missing modifier), so it must never publish the
    round-13b class-closer's "unrecognized" problem either - a
    structurally-understood, JLS-certain negative, not an unparseable one."""
    for signature in (
        "static void main(String[] args)",         # missing public
        "public void main(String[] args)",          # missing static
        "void main(String[] args)",                  # missing both
    ):
        src = f"package p;\nclass App {{\n    {signature} {{\n    }}\n}}\n"
        result = java.parse_java_source("App.java", src)
        assert [e for e in result.entry_points if e.kind == "cli_main"] == [], signature
        assert result.problems == [], signature


def test_unrecognized_main_like_shape_degrades_to_a_named_problem_not_silence():
    """FIX ROUND 13b (reviewer-3's B1 class-closer): a method literally
    named main, returning void, whose overall shape the strict matcher
    could not recognize AT ALL must never silently vanish into a
    confident "no entry point" - it degrades to a named, visible
    problem instead, since it MIGHT be a legal spelling this adapter
    still does not cover. A bare, non-array String parameter is
    String-TYPED (so round 13c's JLS-certain wrong-TYPE check does not
    apply) but matches no recognized array/varargs form either - the
    genuine spelling-variant uncertainty this class-closer exists for,
    not a JLS-certain wrong-arity/wrong-type shape."""
    src = """
package p;
class App {
    public static void main(String args) {
    }
}
"""
    result = java.parse_java_source("App.java", src)
    assert [e for e in result.entry_points if e.kind == "cli_main"] == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "cli_main_unrecognized"
    assert "did not match" in result.problems[0].detail
    assert result.problems[0].qualified_name == "p.App"


def test_jls_certain_wrong_signature_shapes_are_silent_never_unrecognized():
    """FIX ROUND 13c (reviewer-3's MILDER ask): main(int[]), main(), and
    main(String[], int) are JLS-CERTAIN negatives - the JVM entry-point
    signature is EXACTLY one String[]/varargs parameter, so a wrong
    arity or a plainly-wrong base type can never be the entry point
    regardless of modifiers. These must classify with the private/non-
    static certain-negative branch (silent, no problem) - "unknown" is
    reserved for shapes this adapter genuinely could not classify."""
    for signature in (
        "public static void main(int[] args)",           # wrong base type
        "public static void main()",                       # wrong arity (zero)
        "public static void main(String[] args, int extra)",  # wrong arity (two)
    ):
        src = f"package p;\nclass App {{\n    {signature} {{\n    }}\n}}\n"
        result = java.parse_java_source("App.java", src)
        assert [e for e in result.entry_points if e.kind == "cli_main"] == [], signature
        assert result.problems == [], signature


def test_second_top_level_type_with_its_own_main_gets_its_own_cli_main_entry_point():
    src = """
package p;
class First {
    public static void main(String[] args) {
    }
}
class Second {
    public static void main(String[] args) {
    }
}
"""
    result = java.parse_java_source("Multi.java", src)
    mains = {e.qualified_name for e in result.entry_points if e.kind == "cli_main"}
    # Note 10 (second cold read, fix round 4): a single re.search kept only
    # the FIRST main method in the whole file - a second top-level type's
    # own main was silently dropped as an entry point entirely.
    assert mains == {"p.First", "p.Second"}


def test_unrecognized_main_problem_is_attributed_only_to_its_own_enclosing_type():
    """FIX ROUND 13c (reviewer-3's part 1 on round 13b): a
    cli_main_unrecognized problem used to be a plain file-level record
    with no owning type at all - broadcast (by modules_artifact.py's
    generic wiring) onto EVERY declared type in the file. In a 3-class
    file where only the THIRD has a main-like method, Alpha and Beta
    must carry no attribution at all; only Gamma does."""
    src = """
package p;
class Alpha {
}
class Beta {
}
class Gamma {
    public static void main(String args) {
    }
}
"""
    result = java.parse_java_source("Multi.java", src)
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "cli_main_unrecognized"
    assert result.problems[0].qualified_name == "p.Gamma"


# ----------------------------------------------------------- pom.xml (build)

def test_parse_maven_pom_extracts_dependency_build_edges():
    pom = """<project>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
      <version>5.0.0</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
    </dependency>
  </dependencies>
</project>
"""
    _units, edges, profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"org.springframework:spring-core", "junit:junit"}
    assert profile_scoped_count == 0
    assert all(e.relation == "build" for e in edges)
    assert all(e.evidence_class == "declared" for e in edges)
    assert all(e.phase == "build" and e.optional is False for e in edges)


def test_parse_maven_pom_reads_optional_and_test_scope_instead_of_asserting_defaults():
    """M3 (fourth cold read, fix round 6): <optional>/<scope> were read
    past and discarded - every edge asserted optional:false, phase:build
    as a positive fact regardless of what the pom actually declared.
    Reproduced pre-fix: an optional=true, scope=test dependency still
    published optional:false, phase:build."""
    pom = """<project>
  <dependencies>
    <dependency>
      <groupId>org.mockito</groupId>
      <artifactId>mockito-core</artifactId>
      <scope>test</scope>
      <optional>true</optional>
    </dependency>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
    </dependency>
  </dependencies>
</project>
"""
    edges = {e.target: e for e in java.parse_maven_pom("pom.xml", pom)[1]}
    mockito = edges["org.mockito:mockito-core"]
    assert mockito.optional is True
    assert mockito.phase == "test"
    spring = edges["org.springframework:spring-core"]
    assert spring.optional is False
    assert spring.phase == "build"


def test_parse_maven_pom_ignores_a_commented_out_dependency():
    """M-1 (second cold read, fix round 4): a dependency block inside an
    XML comment must not publish as evidence_class=declared alongside a
    live one - commented-out dependencies are common in legacy poms."""
    pom = """<project>
  <dependencies>
    <!--
    <dependency>
      <groupId>commented</groupId>
      <artifactId>out-dependency</artifactId>
    </dependency>
    -->
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
    </dependency>
  </dependencies>
</project>
"""
    _units, edges, profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"org.springframework:spring-core"}
    assert profile_scoped_count == 0


def test_parse_maven_pom_excludes_dependency_management_entries():
    """M1 (seventh cold read MAJOR, wrong-data): reviewer's proving test -
    one dependencyManagement entry (a parent/BOM pom can carry dozens;
    these are NOT dependencies of this module) plus one real entry must
    publish exactly one edge, naming the real one."""
    pom = """<project>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.acme</groupId>
        <artifactId>bom-managed-dep</artifactId>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>real-dep</artifactId>
    </dependency>
  </dependencies>
</project>
"""
    _units, edges, profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"com.acme:real-dep"}
    # M1 (round 11b): managed-scoped exclusion is cost-free (never the
    # module's own graph) - never counted.
    assert profile_scoped_count == 0


def test_parse_maven_pom_counts_profile_scoped_dependencies():
    """M1: a <profile>'s own dependencies are conditionally active, not
    unconditional direct dependencies of the module - excluded from the
    edges (named decision), never published undifferentiated alongside
    real ones.

    Round 11c (reviewer-3 delta on round 11b, VEHICLE CHANGE): a profile
    CAN be active by default, so its dependency is a potentially live
    one - the exclusion must be visible, but as a named exclusion COUNT
    (scan.json's existing idiom), never a run-degrading problem the way
    round 11b's own fix made it (Maven profiles are common enough that a
    large share of real repos would scan degraded permanently over a
    DECLARED, deliberate scope limitation)."""
    pom = """<project>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>real-dep</artifactId>
    </dependency>
  </dependencies>
  <profiles>
    <profile>
      <dependencies>
        <dependency>
          <groupId>com.acme</groupId>
          <artifactId>profile-dep</artifactId>
        </dependency>
      </dependencies>
    </profile>
  </profiles>
</project>
"""
    _units, edges, profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"com.acme:real-dep"}
    assert profile_scoped_count == 1


def test_parse_maven_pom_with_only_managed_and_plugin_scoped_dependencies_is_silent():
    """Round 11b/11c: the reviewer's own second test shape - managed and
    plugin exclusion is judged cost-free (never the module's own
    dependency graph), so a pom containing ONLY those two (no
    profile-scoped dependency at all) must yield no count."""
    pom = """<project>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.acme</groupId>
        <artifactId>bom-managed-dep</artifactId>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <build>
    <plugins>
      <plugin>
        <dependencies>
          <dependency>
            <groupId>com.acme</groupId>
            <artifactId>plugin-dep</artifactId>
          </dependency>
        </dependencies>
      </plugin>
    </plugins>
  </build>
</project>
"""
    _units, edges, profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert edges == []
    assert profile_scoped_count == 0


def test_parse_maven_pom_excludes_plugin_scoped_dependencies():
    """M1: a <plugin>'s own dependencies are the BUILD TOOL's, not the
    module's - excluded (named decision), never published
    undifferentiated alongside real ones."""
    pom = """<project>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>real-dep</artifactId>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <dependencies>
          <dependency>
            <groupId>com.acme</groupId>
            <artifactId>plugin-dep</artifactId>
          </dependency>
        </dependencies>
      </plugin>
    </plugins>
  </build>
</project>
"""
    _units, edges, profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"com.acme:real-dep"}
    # M1 (round 11b): plugin-scoped exclusion is cost-free (the build
    # tool's own dependency, never the module's) - never counted.
    assert profile_scoped_count == 0


# ----------------------------------------------------------- @WebServlet / JAX-RS (route)

def test_web_servlet_annotation_publishes_its_own_url_patterns_as_routes():
    """FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, wrong-data, part
    (a)): @WebServlet published NO entry point and NO problem at all -
    the enumerated-recognizer class, entry-point edition. Unlike
    @RequestMapping, it is not composable - the annotation decorates the
    class directly and its own urlPatterns ARE the complete route(s)."""
    src = """
package p;

@WebServlet(urlPatterns = {"/api/*", "/legacy/*"})
public class DispatcherServlet extends HttpServlet {
}
"""
    result = java.parse_java_source("DispatcherServlet.java", src)
    routes = _edges(result, "route")
    assert sorted(r.target for r in routes) == ["/api/*", "/legacy/*"]
    http_entry_points = [e for e in result.entry_points if e.kind == "http_route"]
    assert sorted(e.name for e in http_entry_points) == ["/api/*", "/legacy/*"]
    assert all(e.qualified_name == "p.DispatcherServlet" for e in http_entry_points)


def test_web_servlet_annotation_recovers_a_bare_positional_value():
    """@WebServlet("/api/*") - the bare positional form, no named
    urlPatterns/value attribute at all - still recovered via the
    existing positional-literal fallback."""
    src = """
package p;

@WebServlet("/api/*")
public class DispatcherServlet extends HttpServlet {
}
"""
    result = java.parse_java_source("DispatcherServlet.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["/api/*"]


def test_web_filter_annotation_publishes_its_own_url_patterns_as_a_distinct_filter_kind():
    """FIX ROUND 21 (seventeenth cold read, CR17-3 MAJOR, wrong-data -
    JUDGE, taken): @WebFilter shares @WebServlet's own shape exactly
    (class-level, not composable, urlPatterns IS the complete
    intercepted pattern) - contained enough to MODEL, unlike
    @WebListener (a lifecycle callback, enrolled as unsupported
    instead, see the companion test below).

    FIX ROUND 21b (reviewer-3's re-delta, THE MAJOR, wrong-data,
    OVERTURNS round 21's own kind="http_route" choice): a filter
    INTERCEPTS, it does not SERVE - publishing kind="http_route" made
    an app with one served endpoint plus one filter inventory as TWO
    served routes, byte-identically. Now kind="http_filter" - the URL
    pattern still survives as real migration information (the route
    edge below, and the entry point's own name), just under a kind that
    is never confused with a served route."""
    src = """
package p;

@WebFilter(urlPatterns = {"/api/*", "/secure/*"})
public class AuthFilter implements Filter {
}
"""
    result = java.parse_java_source("AuthFilter.java", src)
    routes = _edges(result, "route")
    assert sorted(r.target for r in routes) == ["/api/*", "/secure/*"]
    assert not any(e.kind == "http_route" for e in result.entry_points)
    filter_entry_points = [e for e in result.entry_points if e.kind == "http_filter"]
    assert sorted(e.name for e in filter_entry_points) == ["/api/*", "/secure/*"]
    assert all(e.qualified_name == "p.AuthFilter" for e in filter_entry_points)


def test_web_listener_annotation_is_enrolled_not_confidently_absent():
    """FIX ROUND 21 (CR17-3 MAJOR, wrong-data): @WebListener has no URL
    pattern of its own to model - a lifecycle callback, not a routable
    request handler - so it gets the class-closer treatment
    (unsupported_entry_point_shape), never a confident negative."""
    src = """
package p;

@WebListener
public class AppLifecycleListener implements ServletContextListener {
}
"""
    result = java.parse_java_source("AppLifecycleListener.java", src)
    assert any(
        p.reason_code == "unsupported_entry_point_shape"
        and p.qualified_name == "p.AppLifecycleListener"
        for p in result.problems
    )
    assert not any(e.kind == "http_route" for e in result.entry_points)


def test_web_xml_filter_is_modeled_as_http_filter_attributed_to_its_own_filter_class():
    """FIX ROUND 21 (CR17-3 MAJOR, wrong-data): a web.xml <filter> - the
    direct XML twin of <servlet> - used to publish nothing at all.

    FIX ROUND 21b (reviewer-3's re-delta, THE MAJOR's own web.xml-
    symmetry question, taken - OVERTURNS round 21's own enroll-only
    choice): <servlet>/<servlet-mapping> already models at this exact
    fidelity (CR13-2, round 17) - leaving <filter>/<filter-mapping>
    enrolled-only was the identical two-opposite-answers contradiction
    the reviewer raised for @WebFilter, just for the XML shape instead
    of the annotation. Now MODELED, joined against <filter-mapping>'s
    own filter-name the same way a servlet-mapping already joins
    <servlet-class>, published as kind="http_filter" - never
    "http_route", a filter intercepts, it does not serve."""
    web_xml = """<web-app>
  <filter>
    <filter-name>auth</filter-name>
    <filter-class>com.acme.web.AuthFilter</filter-class>
  </filter>
  <filter-mapping>
    <filter-name>auth</filter-name>
    <url-pattern>/*</url-pattern>
  </filter-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert not any(p.reason_code == "unsupported_entry_point_shape" for p in problems)
    assert not any(e.kind == "http_route" for e in entry_points)
    filter_entry_points = [e for e in entry_points if e.kind == "http_filter"]
    assert len(filter_entry_points) == 1
    assert filter_entry_points[0].qualified_name == "com.acme.web.AuthFilter"
    assert filter_entry_points[0].name == "/*"


def test_web_xml_listener_is_enrolled_attributed_to_its_own_listener_class():
    """FIX ROUND 21 (CR17-3 MAJOR, wrong-data): a web.xml <listener> -
    the direct XML twin of @WebListener - used to publish nothing at
    all. Now enrolled as unsupported_entry_point_shape, attributed to
    its own <listener-class>."""
    web_xml = """<web-app>
  <listener>
    <listener-class>com.acme.web.AppLifecycleListener</listener-class>
  </listener>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert any(
        p.reason_code == "unsupported_entry_point_shape"
        and p.qualified_name == "com.acme.web.AppLifecycleListener"
        for p in problems
    )
    assert entry_points == []


def test_web_xml_filter_with_no_filter_class_falls_back_to_the_synthetic_owner():
    """FIX ROUND 21b: companion negative case for the now-modeled
    <filter>/<filter-mapping> pair - a malformed/incomplete <filter>
    (no <filter-class>) is simply not in ``_filter_class_by_name``'s own
    mapping, so its <filter-mapping> falls back to the synthetic
    ``{relative_path}#{filter_name}`` owner, the exact same accepted
    asymmetry an unmatched <servlet-mapping> already has (round 17's own
    CR13-2 docstring) - never a silent drop, still published."""
    web_xml = """<web-app>
  <filter>
    <filter-name>auth</filter-name>
  </filter>
  <filter-mapping>
    <filter-name>auth</filter-name>
    <url-pattern>/*</url-pattern>
  </filter-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert not any(p.reason_code == "unsupported_entry_point_shape" for p in problems)
    filter_entry_points = [e for e in entry_points if e.kind == "http_filter"]
    assert len(filter_entry_points) == 1
    assert filter_entry_points[0].qualified_name == "WEB-INF/web.xml#auth"
    assert filter_entry_points[0].name == "/*"


def test_web_xml_filter_mapping_by_servlet_name_is_enrolled_not_silent():
    """FIX ROUND 22 (eighteenth cold read, F3 MAJOR, wrong-data): a
    <filter-mapping> naming a <servlet-name> instead of a <url-pattern>
    (a real, DTD-valid dispatch alternative) used to publish nothing at
    all - a NAMED LIMIT comment that was itself the defect. Now enrolled
    as unsupported_entry_point_shape (servlet_name_scoped_filter),
    attributed to the filter's own resolved class."""
    web_xml = """<web-app>
  <filter>
    <filter-name>auth</filter-name>
    <filter-class>com.acme.web.AuthFilter</filter-class>
  </filter>
  <filter-mapping>
    <filter-name>auth</filter-name>
    <servlet-name>orders</servlet-name>
  </filter-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert entry_points == []
    matching = [
        p for p in problems
        if p.reason_code == "unsupported_entry_point_shape"
        and p.qualified_name == "com.acme.web.AuthFilter"
    ]
    assert len(matching) == 1
    assert "servlet_name_scoped_filter" in matching[0].detail


def test_web_xml_startup_only_servlet_with_no_mapping_is_enrolled_not_silent():
    """FIX ROUND 22 (F3 MAJOR, wrong-data): a <servlet> carrying
    <load-on-startup> but never named by any <servlet-mapping> (the
    standard startup-only servlet idiom) used to publish nothing at
    all. Now enrolled, attributed to its own <servlet-class>. An
    unrelated MAPPED servlet in the same file is unaffected."""
    web_xml = """<web-app>
  <servlet>
    <servlet-name>init</servlet-name>
    <servlet-class>com.acme.web.InitServlet</servlet-class>
    <load-on-startup>1</load-on-startup>
  </servlet>
  <servlet>
    <servlet-name>orders</servlet-name>
    <servlet-class>com.acme.web.OrdersServlet</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>orders</servlet-name>
    <url-pattern>/orders</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert len(entry_points) == 1
    assert entry_points[0].qualified_name == "com.acme.web.OrdersServlet"
    matching = [
        p for p in problems
        if p.reason_code == "unsupported_entry_point_shape"
        and p.qualified_name == "com.acme.web.InitServlet"
    ]
    assert len(matching) == 1
    assert "startup_only_servlet" in matching[0].detail


def test_web_servlet_annotation_startup_only_no_url_is_enrolled_not_silent():
    """FIX ROUND 22 (F3 MAJOR, wrong-data): @WebServlet(name=...,
    loadOnStartup=1) with no value/urlPatterns attribute at all (the
    annotation-form startup-only idiom) used to fall through the paths
    loop zero times - no entry point, no problem. Now enrolled. A
    normal @WebServlet with a real URL in the same run is unaffected."""
    src = """
package p;

@WebServlet(name = "init", loadOnStartup = 1)
public class InitServlet extends HttpServlet {
}

@WebServlet("/orders")
public class OrdersServlet extends HttpServlet {
}
"""
    result = java.parse_java_source("Servlets.java", src)
    http_routes = [e for e in result.entry_points if e.kind == "http_route"]
    assert len(http_routes) == 1
    assert http_routes[0].qualified_name == "p.OrdersServlet"
    matching = [
        p for p in result.problems
        if p.reason_code == "unsupported_entry_point_shape" and p.qualified_name == "p.InitServlet"
    ]
    assert len(matching) == 1
    assert "startup_only_servlet" in matching[0].detail


def test_web_filter_annotation_servlet_names_only_is_enrolled_not_silent():
    """FIX ROUND 22 (F3 MAJOR, wrong-data): @WebFilter(servletNames=
    {...}) with no value/urlPatterns attribute at all used to fall
    through the paths loop zero times - no entry point, no problem.
    Now enrolled."""
    src = """
package p;

@WebFilter(servletNames = {"orders"})
public class OrdersAuthFilter implements Filter {
}
"""
    result = java.parse_java_source("OrdersAuthFilter.java", src)
    assert result.entry_points == []
    matching = [
        p for p in result.problems
        if p.reason_code == "unsupported_entry_point_shape"
        and p.qualified_name == "p.OrdersAuthFilter"
    ]
    assert len(matching) == 1
    assert "servlet_name_scoped_filter" in matching[0].detail


# --------------------------------------------------- round 23 F1/F2 (web.xml/pom attribute tolerance + XML decode)

def test_web_xml_structural_tag_matrix_tolerates_attributes_prefixes_and_whitespace():
    """FIX ROUND 23 (nineteenth cold read, F1 BLOCKER, wrong-data): every
    structural web.xml regex anchored on the BARE literal tag - a legal
    <servlet id="...">, a namespace-prefixed <j:servlet-mapping>, or
    whitespace/newlines inside the tag (<servlet\\n id="x">) matched
    NOTHING, so the whole descriptor published nothing at all - the
    DEFAULT OUTPUT shape of IBM RAD/WSAD tooling (an id attribute on
    every structural element), exactly the WebSphere-era estate this
    scanner targets. c1/c8 are bare-tag controls that already worked;
    c2-c7 each isolate one tolerance gap."""
    web_xml = """<web-app>
  <servlet>
    <servlet-name>c1</servlet-name>
    <servlet-class>com.C1</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>c1</servlet-name>
    <url-pattern>/c1</url-pattern>
  </servlet-mapping>
  <servlet id="Servlet_2">
    <servlet-name>c2</servlet-name>
    <servlet-class>com.C2</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>c2</servlet-name>
    <url-pattern>/c2</url-pattern>
  </servlet-mapping>
  <j:servlet-mapping>
    <servlet-name>c3</servlet-name>
    <url-pattern>/c3</url-pattern>
  </j:servlet-mapping>
  <servlet
      id="Servlet_7">
    <servlet-name>c7</servlet-name>
    <servlet-class>com.C7</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>c7</servlet-name>
    <url-pattern>/c7</url-pattern>
  </servlet-mapping>
  <filter id="Filter_1">
    <filter-name>c6</filter-name>
    <filter-class>com.C6Filter</filter-class>
  </filter>
  <filter-mapping>
    <filter-name>c6</filter-name>
    <url-pattern>/c6</url-pattern>
  </filter-mapping>
  <listener id="Listener_1">
    <listener-class>com.C6Listener</listener-class>
  </listener>
  <servlet-mapping >
    <servlet-name>c8</servlet-name>
    <url-pattern>/c8</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    routes_by_pattern = {e.name: e.qualified_name for e in entry_points}
    assert routes_by_pattern["/c1"] == "com.C1"
    assert routes_by_pattern["/c2"] == "com.C2"
    assert routes_by_pattern["/c3"] == "WEB-INF/web.xml#c3"
    assert routes_by_pattern["/c7"] == "com.C7"
    assert routes_by_pattern["/c6"] == "com.C6Filter"
    assert routes_by_pattern["/c8"] == "WEB-INF/web.xml#c8"
    listener_problems = [
        p for p in problems
        if p.reason_code == "unsupported_entry_point_shape" and p.qualified_name == "com.C6Listener"
    ]
    assert len(listener_problems) == 1


def test_web_xml_attribute_bearing_servlet_filter_and_listener_end_to_end():
    """FIX ROUND 23 (F1 BLOCKER): the reader's own .cr19-webxmlattr
    shape - a servlet, a filter, and a listener, each with an id
    attribute on its own structural tag, all published/enrolled in the
    SAME run."""
    web_xml = """<web-app>
  <servlet id="s1">
    <servlet-name>admin</servlet-name>
    <servlet-class>com.acme.AdminServlet</servlet-class>
  </servlet>
  <servlet-mapping id="m1">
    <servlet-name>admin</servlet-name>
    <url-pattern>/admin/*</url-pattern>
  </servlet-mapping>
  <filter id="f1">
    <filter-name>auth</filter-name>
    <filter-class>com.acme.AuthFilter</filter-class>
  </filter>
  <filter-mapping id="fm1">
    <filter-name>auth</filter-name>
    <url-pattern>/*</url-pattern>
  </filter-mapping>
  <listener id="l1">
    <listener-class>com.acme.AppListener</listener-class>
  </listener>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    routes_by_pattern = {e.name: (e.qualified_name, e.kind) for e in entry_points}
    assert routes_by_pattern["/admin/*"] == ("com.acme.AdminServlet", "http_route")
    assert routes_by_pattern["/*"] == ("com.acme.AuthFilter", "http_filter")
    assert any(
        p.reason_code == "unsupported_entry_point_shape"
        and p.qualified_name == "com.acme.AppListener"
        for p in problems
    )


def test_parse_maven_pom_dependency_with_an_attribute_on_its_own_tag():
    """FIX ROUND 23 (F1 BLOCKER): the reader's own .cr19-pomattr shape -
    _DEPENDENCY_BLOCK_RE had the identical bare-tag anchor as web.xml's
    own regexes; a <dependency id="x"> silently dropped the entire
    dependency."""
    pom = """<project>
  <dependencies>
    <dependency id="x">
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
    </dependency>
  </dependencies>
</project>
"""
    _units, edges, _profile_scoped_count = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"org.springframework:spring-core"}


def test_web_xml_url_pattern_cdata_and_entity_decoding():
    """FIX ROUND 23 (F1(d) + F2 MAJOR, wrong-data): a CDATA-wrapped
    <url-pattern> published nothing at all (F1(d)); an XML entity
    reference (&#47;, &amp;) published VERBATIM as the literal escape
    sequence instead of the character it names (F2) - a real route
    /c5/x published as the false string /c5&#47;x. Both now decode to
    the real value; the reader's own .cr19-xml shape
    (&#47;admin&amp;danger -> /admin&danger) and the &#10; composition
    with the EXISTING control-char sanitizer (must come out escaped,
    never a raw embedded newline)."""
    web_xml = """<web-app>
  <servlet-mapping>
    <servlet-name>s4</servlet-name>
    <url-pattern><![CDATA[/c4]]></url-pattern>
  </servlet-mapping>
  <servlet-mapping>
    <servlet-name>s5</servlet-name>
    <url-pattern>/c5&#47;x</url-pattern>
  </servlet-mapping>
  <servlet-mapping>
    <servlet-name>sxml</servlet-name>
    <url-pattern>&#47;admin&amp;danger</url-pattern>
  </servlet-mapping>
  <servlet-mapping>
    <servlet-name>snl</servlet-name>
    <url-pattern>/nl&#10;end</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    names = {e.name for e in entry_points}
    assert "/c4" in names
    assert "/c5/x" in names
    assert "/admin&danger" in names
    assert "/nl\\nend" in names
    assert not any("\n" in e.name for e in entry_points)
    assert problems == []


def test_web_xml_url_pattern_with_an_undefined_entity_is_unrecoverable():
    """FIX ROUND 23 (F2 MAJOR): an entity reference this producer has no
    general-entity table for (a custom DTD-declared entity) must never
    publish a guessed or partially-decoded value - routed to the
    existing route_value_unrecoverable honesty instead."""
    web_xml = """<web-app>
  <servlet-mapping>
    <servlet-name>sbad</servlet-name>
    <url-pattern>/bad&undefined;end</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert entry_points == []
    matching = [p for p in problems if p.reason_code == "route_value_unrecoverable"]
    assert len(matching) == 1


def test_jax_rs_path_composes_class_and_method_level_like_spring_request_mapping():
    """FIX ROUND 17 (CR13-3 MAJOR, part (a)): JAX-RS's own @Path composes
    EXACTLY like a plain @RequestMapping already does - a class-level
    @Path is a prefix, and a method-level @Path composes against it. The
    verb stays unknown (JAX-RS's separate @GET/@POST/... designators are
    a named limit, not recognized here - see _ROUTE_ANNOTATIONS's own
    comment: merging a separate verb annotation with an adjacent @Path
    would need per-method grouping this adapter does not have)."""
    src = """
package p;

@Path("/orders")
public class OrderResource {
    @Path("/list")
    public void list() {}
}
"""
    result = java.parse_java_source("OrderResource.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["/orders/list"]


def test_jax_rs_verb_only_methods_get_the_class_closer_not_a_silent_negative():
    """FIX ROUND 17b (reviewer-3's rejection of round 17, THE MAJOR):
    the DOMINANT real-world JAX-RS idiom - a class-level @Path with
    verb-only methods (@GET/@POST, no method-level @Path of their own) -
    used to silently produce ZERO entry points AND zero problems, the
    class landing on the confident negative entry_points_mapped=
    not_applicable/no_entry_point, while @WebMethod (built the exact
    same round) correctly reported unknown/unsupported_entry_point_shape.
    The reviewer's own OrderResource shape: no route composes (verb-only
    methods are not recognized - the named limit), so the class must now
    get the SAME class-closer treatment."""
    src = """
package p;

@Path("/orders")
public class OrderResource {
    @GET
    public void list() {}

    @POST
    public void create() {}
}
"""
    result = java.parse_java_source("OrderResource.java", src)
    assert not any(e.kind == "http_route" for e in result.entry_points)
    problem = next(p for p in result.problems if p.reason_code == "unsupported_entry_point_shape")
    assert problem.qualified_name == "p.OrderResource"


def test_jax_rs_path_with_a_real_method_level_route_is_not_flagged_the_class_closer():
    """Companion negative case - the reviewer's own ItemResource row: a
    class-level @Path with a REAL method-level @Path composing against
    it must stay satisfied, never get the class-closer treatment
    reserved for a class where nothing ever composed."""
    src = """
package p;

@Path("/items")
public class ItemResource {
    @Path("/list")
    public void list() {}
}
"""
    result = java.parse_java_source("ItemResource.java", src)
    assert any(e.kind == "http_route" for e in result.entry_points)
    assert not any(p.reason_code == "unsupported_entry_point_shape" for p in result.problems)


def test_a_mixed_jax_rs_class_gets_the_class_closer_for_its_uncomposed_verb_method():
    """FIX ROUND 18 (fourteenth cold read, F2 MAJOR, wrong-data): the
    reader's own repro - the DOMINANT real REST shape, a collection GET
    (verb-only, no method-level @Path) alongside an item GET (@GET
    PLUS its own @Path, composing normally). Round 17b's class-closer
    only fired when a class produced ZERO routes at all - this class
    produces ONE real route (get()) and used to publish
    entry_points_mapped SATISFIED even though list()'s route is
    genuinely missing from the inventory. The composed route must
    still publish (the marker mechanism never suppresses a real one),
    AND the class must now also get the named problem."""
    src = """
package p;

@Path("/orders")
public class OrderResource {
    @GET
    public void list() {}

    @GET
    @Path("/{id}")
    public void get() {}
}
"""
    result = java.parse_java_source("OrderResource.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["/orders/{id}"]
    problem = next(p for p in result.problems if p.reason_code == "unsupported_entry_point_shape")
    assert problem.qualified_name == "p.OrderResource"


def test_a_mixed_jax_rs_class_with_an_intervening_annotation_still_composes():
    """The verb marker's own annotation stack must tolerate an
    intervening, unrelated annotation (@Produces is common between a
    verb designator and its own @Path) - never mistaking it for a
    stack break that would falsely orphan a method whose route DID
    compose."""
    src = """
package p;

@Path("/orders")
public class OrderResource {
    @GET
    @Produces("application/json")
    @Path("/{id}")
    public void get() {}
}
"""
    result = java.parse_java_source("OrderResource.java", src)
    assert not any(p.reason_code == "unsupported_entry_point_shape" for p in result.problems)


def test_the_annotation_stack_cursor_advances_monotonically_past_nested_swagger_annotations():
    """FIX ROUND 18b (reviewer-3's pre-verified MAJOR on round 18's F2):
    _ANY_ANNOTATION_RE also matches an annotation NESTED inside another
    annotation's own argument list (@ApiResponses({@ApiResponse(...)}) -
    the normal Swagger-documented JAX-RS shape) - finditer resumes
    scanning right after the outer annotation's own NAME (before its
    parens), walks straight into that argument list, and finds the
    nested one as a separate match. The stack-grouping walk assigned
    previous_span_end UNCONDITIONALLY, so this nested match's own
    (smaller) span REGRESSED the cursor backward into the middle of the
    outer annotation's own parens - the next real annotation then saw
    the outer's own trailing "})" in the gap and incorrectly started a
    new stack, corrupting stack membership for every annotation that
    follows in the WHOLE FILE, not just one method: a fully mapped
    resource could publish its own route AND still get flagged
    unsupported_entry_point_shape. previous_span_end now advances
    monotonically (never regresses).

    Four rows in ONE file, sharing one cursor across all of them - the
    exact cross-boundary corruption this bug caused: (1) Swagger order A
    (@GET, then the nested-Swagger annotation, then @Path - the nested
    annotation SANDWICHED between the marker and its own @Path) stays
    clean; (2) Swagger order B (the mirror image - @Path, then the
    nested annotation, then @GET) stays clean; (3) the mixed class
    (round 18's own F2 shape) still gets flagged; (4) the intervening-
    @Produces bridge (the companion test above) still stays clean."""
    src = """
package p;

@Path("/swagger-a")
public class SwaggerOrderA {
    @GET
    @ApiResponses({@ApiResponse(code = 200, message = "OK")})
    @Path("/{id}")
    public void get() {}
}

@Path("/swagger-b")
public class SwaggerOrderB {
    @Path("/{id}")
    @ApiResponses({@ApiResponse(code = 200, message = "OK")})
    @GET
    public void get() {}
}

@Path("/mixed")
public class MixedResource {
    @GET
    public void list() {}

    @GET
    @Path("/{id}")
    public void get() {}
}

@Path("/bridge")
public class BridgeResource {
    @GET
    @Produces("application/json")
    @Path("/{id}")
    public void get() {}
}
"""
    result = java.parse_java_source("Resources.java", src)
    flagged = {
        p.qualified_name for p in result.problems if p.reason_code == "unsupported_entry_point_shape"
    }
    assert flagged == {"p.MixedResource"}


def test_the_annotation_stack_tolerates_a_modifier_keyword_between_annotations():
    """FIX ROUND 19 (fifteenth cold read, F6 MINOR, wrong-data - degrades
    a healthy run): mirrors the reader's own ``.cr15-d`` ReportResource
    shape - the JLS permits a modifier and an annotation to interleave
    in either order (``public @GET String one()`` is exactly as legal
    as ``@GET public String one()``). The stack-grouping walk treated
    ANY non-whitespace content between two annotations as a break, so a
    modifier keyword sitting between a verb marker and its own @Path
    incorrectly split them into separate stacks - a FULLY MAPPED
    resource got the false coverage-gap problem, unknown, and degraded.
    Tolerated the same way an intervening unrelated annotation already
    is. A true positive (the mixed-class shape, no modifier involved)
    must stay flagged, unaffected."""
    src = """
package p;

@Path("/reports")
public class ReportResource {
    @GET
    public @Path("/{id}") String one() { return null; }
}

@Path("/mixed")
public class MixedResource {
    @GET
    public void list() {}

    @GET
    @Path("/{id}")
    public void get() {}
}
"""
    result = java.parse_java_source("Resources.java", src)
    flagged = {
        p.qualified_name for p in result.problems if p.reason_code == "unsupported_entry_point_shape"
    }
    assert flagged == {"p.MixedResource"}


def test_a_route_annotation_on_a_field_publishes_nothing():
    """FIX ROUND 20 (sixteenth cold read, m1 MINOR, wrong-data): mirrors
    the reader's own .cr16-l field shape - a route annotation is only
    ever legal (JAX-RS/Spring) on a method, never a field. Used to
    publish a full, confident entry point + edge + feature + satisfied
    anyway, since nothing checked WHAT kind of member the annotation
    actually decorates. The missing precondition is structural (this
    does not compile as real JAX-RS/Spring) - publishes nothing, the
    same confident "no route here" a class with no route annotation at
    all correctly gets."""
    src = """
package p;

public class OrderResource {
    @GetMapping("/orders")
    private String route;
}
"""
    result = java.parse_java_source("OrderResource.java", src)
    assert _edges(result, "route") == []
    assert not any(e.kind == "http_route" for e in result.entry_points)
    assert result.problems == []


def test_a_route_annotation_on_an_abstract_interface_method_still_composes():
    """Companion control case for m1 - an interface's own abstract method
    (no body, just a bare parameter list then `;`) is still a genuine
    METHOD declaration and must stay published, unaffected by the field
    check - the reader's own flagged "legitimately registered Spring"
    shape."""
    src = """
package p;

public interface OrderApi {
    @GetMapping("/orders")
    String list();
}
"""
    result = java.parse_java_source("OrderApi.java", src)
    routes = _edges(result, "route")
    assert [r.target for r in routes] == ["GET /orders"]


def test_web_method_annotation_is_the_named_class_closer_not_a_silent_negative():
    """FIX ROUND 17 (CR13-3 MAJOR, part (b) - THE CLASS-CLOSER): a route-
    like annotation family this adapter recognizes as a routing
    mechanism but has not modeled (JAX-WS's own @WebMethod) must publish
    a NAMED problem, attributed to its own enclosing type - never
    silently fall through to a confident "no entry point" negative."""
    src = """
package p;

public class OrderEndpoint {
    @WebMethod
    public void placeOrder() {}
}
"""
    result = java.parse_java_source("OrderEndpoint.java", src)
    assert not any(e.kind == "http_route" for e in result.entry_points)
    problem = next(p for p in result.problems if p.reason_code == "unsupported_entry_point_shape")
    assert problem.qualified_name == "p.OrderEndpoint"


def test_five_unenrolled_entry_point_families_get_the_named_class_closer():
    """FIX ROUND 19 (fifteenth cold read, F3 MAJOR, wrong-data): mirrors
    the reader's own ``.cr15-f`` five-family shape - @Scheduled,
    @KafkaListener, @MessageDriven, an EJB @Remote component, and
    @ServerEndpoint all used to publish the confident
    not_applicable/no_entry_point negative on an otherwise complete run.
    The same class-closer treatment @WebMethod already gets, enrolled
    for all five, each under its own named shape."""
    src = """
package p;

public class JobRunner {
    @Scheduled(fixedRate = 5000)
    public void cleanup() {}
}

public class OrderEventConsumer {
    @KafkaListener(topics = "orders")
    public void onOrder(String message) {}
}

@MessageDriven
public class OrderMdb {
}

@Remote
public class BillingService {
}

@ServerEndpoint("/chat")
public class ChatEndpoint {
}
"""
    result = java.parse_java_source("Various.java", src)
    problems_by_class = {
        p.qualified_name: p for p in result.problems if p.reason_code == "unsupported_entry_point_shape"
    }
    assert set(problems_by_class) == {
        "p.JobRunner", "p.OrderEventConsumer", "p.OrderMdb", "p.BillingService", "p.ChatEndpoint",
    }
    assert "spring_scheduled" in problems_by_class["p.JobRunner"].detail
    assert "kafka_listener" in problems_by_class["p.OrderEventConsumer"].detail
    assert "jms_message_driven" in problems_by_class["p.OrderMdb"].detail
    assert "ejb_remote_component" in problems_by_class["p.BillingService"].detail
    assert "websocket_server_endpoint" in problems_by_class["p.ChatEndpoint"].detail


def test_a_plain_class_with_none_of_the_five_families_stays_not_applicable():
    """Companion negative case - an ordinary class with no recognized
    entry-point annotation at all must stay the confident
    not_applicable/no_entry_point negative, unaffected."""
    src = """
package p;

public class PlainService {
    public void doWork() {}
}
"""
    result = java.parse_java_source("PlainService.java", src)
    assert not any(p.reason_code == "unsupported_entry_point_shape" for p in result.problems)


def test_a_local_only_stateless_ejb_is_not_flagged_as_a_remote_component():
    """Companion negative case for the ejb_remote_component JUDGE - a
    purely LOCAL @Stateless session bean (no @Remote alongside it) has
    no external entry point at all; the existing confident negative is
    already correct for it and must stay unaffected."""
    src = """
package p;

@Stateless
public class LocalOnlyService {
}
"""
    result = java.parse_java_source("LocalOnlyService.java", src)
    assert not any(p.reason_code == "unsupported_entry_point_shape" for p in result.problems)


# ----------------------------------------------------------- web.xml (route)

def test_parse_web_xml_extracts_servlet_mapping_routes():
    web_xml = """<web-app>
  <servlet-mapping>
    <servlet-name>dispatcher</servlet-name>
    <url-pattern>/api/*</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert len(entry_points) == 1
    assert entry_points[0].name == "/api/*"
    assert entry_points[0].kind == "http_route"
    assert entry_points[0].evidence_class == "declared"


def test_parse_web_xml_ignores_a_commented_out_servlet_mapping():
    """M-1 (second cold read, fix round 4): same fix as parse_maven_pom -
    a servlet-mapping inside an XML comment must not publish as a route."""
    web_xml = """<web-app>
  <!--
  <servlet-mapping>
    <servlet-name>disabled</servlet-name>
    <url-pattern>/disabled/*</url-pattern>
  </servlet-mapping>
  -->
  <servlet-mapping>
    <servlet-name>dispatcher</servlet-name>
    <url-pattern>/api/*</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert [e.name for e in entry_points] == ["/api/*"]


def test_parse_web_xml_captures_every_url_pattern_in_one_servlet_mapping():
    """FIX ROUND 15 (eleventh cold read, F1 MAJOR, wrong-data): a single
    <servlet-mapping> may carry SEVERAL <url-pattern> children (legal,
    and legacy apps routinely do it) - only the FIRST used to publish,
    the rest silently vanished with no problem recorded. Reviewer's own
    cr11-fx2 shape verbatim: /legacy/*, /old/*, *.do in one mapping."""
    web_xml = """<web-app>
  <servlet-mapping>
    <servlet-name>legacy</servlet-name>
    <url-pattern>/legacy/*</url-pattern>
    <url-pattern>/old/*</url-pattern>
    <url-pattern>*.do</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert sorted(e.name for e in entry_points) == ["*.do", "/legacy/*", "/old/*"]
    assert len({e.qualified_name for e in entry_points}) == 1
    assert all(e.kind == "http_route" and e.evidence_class == "declared" for e in entry_points)


def test_parse_web_xml_links_a_mapping_to_its_declared_servlet_class():
    """FIX ROUND 17 (thirteenth cold read, CR13-2 MAJOR, wrong-data):
    <servlet-class> was NEVER read at all - twice carried as an M5/M7
    fast-follow, now the actual fix. A <servlet> element's own
    servlet-name/servlet-class pair, joined against <servlet-mapping>'s
    identical servlet-name, must give the mapped route its real
    implementing class as the entry point's own qualified_name -
    features_artifact.build_features already resolves an entry point's
    owner through an exact qualified_name match against the SAME
    registry every other producer's units build through; no further
    plumbing needed once the real class name is published here."""
    web_xml = """<web-app>
  <servlet>
    <servlet-name>dispatcher</servlet-name>
    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>dispatcher</servlet-name>
    <url-pattern>/api/*</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert len(entry_points) == 1
    assert entry_points[0].qualified_name == "com.acme.web.DispatcherServlet"


def test_parse_web_xml_falls_back_to_the_synthetic_owner_with_no_matching_servlet():
    """Companion negative case: a <servlet-mapping> with no matching
    <servlet> element (malformed, or genuinely absent) keeps the OLD
    synthetic {relative_path}#{servlet_name} placeholder - the fix only
    closes the specific case where a real <servlet-class> exists and was
    simply never read."""
    web_xml = """<web-app>
  <servlet-mapping>
    <servlet-name>legacy</servlet-name>
    <url-pattern>/legacy/*</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert entry_points[0].qualified_name == "WEB-INF/web.xml#legacy"


def test_parse_web_xml_url_pattern_is_length_bounded():
    """FIX ROUND 13 (ninth cold read, CR9-6, judged completeness): a
    url-pattern published VERBATIM, UNBOUNDED, while every Java route
    target is already length-bounded (invariant 3) - routed through the
    same per-field bounding discipline (_bounded_route_target)."""
    from agenttalk.comprehension.adapters.java import _MAX_ROUTE_TARGET_LENGTH

    oversized = "/" + ("x" * (_MAX_ROUTE_TARGET_LENGTH + 50))
    web_xml = f"""<web-app>
  <servlet-mapping>
    <servlet-name>dispatcher</servlet-name>
    <url-pattern>{oversized}</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert len(entry_points[0].name) <= _MAX_ROUTE_TARGET_LENGTH + len("...(truncated)")
    assert entry_points[0].name != oversized


def test_parse_maven_pom_group_and_artifact_id_are_length_bounded():
    """FIX ROUND 13 (CR9-6): same per-field bounding discipline applied
    to a pom's own groupId/artifactId - a hostile or merely enormous pom
    used to publish either verbatim, unbounded."""
    from agenttalk.comprehension.adapters.java import _MAX_ROUTE_TARGET_LENGTH

    oversized_group = "g" * (_MAX_ROUTE_TARGET_LENGTH + 50)
    pom = (
        "<project><dependencies><dependency>"
        f"<groupId>{oversized_group}</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>"
    )
    _units, edges, _profile_count = java.parse_maven_pom("pom.xml", pom)
    assert len(edges) == 1
    assert len(edges[0].target) <= 2 * (_MAX_ROUTE_TARGET_LENGTH + len("...(truncated)")) + 1
    assert oversized_group not in edges[0].target


# ----------------------------------------- F3 MAJOR (round 18): parent groupId fallback

def test_parse_maven_pom_registers_a_unit_using_a_groupid_inherited_from_parent():
    """FIX ROUND 18 (fourteenth cold read, F3 MAJOR, wrong-data): a child
    pom that inherits its groupId from its own <parent> block (the
    standard, common Maven reactor spelling) used to register no unit at
    all - round 17's own NAMED LIMIT called this a safe under-claim, but
    the reviewer states it is actually an OVER-claim: a sibling's
    dependency edge on that child publishes a confident resolved/
    external, not an honest unresolved. Now registers the unit using the
    parent's groupId, paired with the pom's own (never-inherited)
    artifactId."""
    pom = (
        "<project>"
        "<parent><groupId>com.acme</groupId><artifactId>acme-parent</artifactId>"
        "<version>1.0</version></parent>"
        "<artifactId>acme-core</artifactId>"
        "</project>"
    )
    units, _edges, _profile_count = java.parse_maven_pom("acme-core/pom.xml", pom)
    assert len(units) == 1
    assert units[0].qualified_name == "com.acme:acme-core"
    assert units[0].simple_name == "acme-core"


def test_parse_maven_pom_prefers_its_own_explicit_groupid_over_parents():
    """The existing explicit-groupId case must stay green, unchanged -
    an own project-level <groupId> always wins over a <parent>'s,
    regardless of textual order in the file."""
    pom = (
        "<project>"
        "<parent><groupId>com.acme</groupId><artifactId>acme-parent</artifactId>"
        "<version>1.0</version></parent>"
        "<groupId>com.acme.override</groupId>"
        "<artifactId>acme-core</artifactId>"
        "</project>"
    )
    units, _edges, _profile_count = java.parse_maven_pom("acme-core/pom.xml", pom)
    assert len(units) == 1
    assert units[0].qualified_name == "com.acme.override:acme-core"


def test_parse_maven_pom_registers_no_unit_for_a_pom_with_neither_groupid_nor_parent():
    """A pathological pom with NEITHER an explicit project-level groupId
    NOR a readable <parent> still registers no unit at all - unchanged
    from before this round; this adapter has no basis to invent an
    identity for it. (Whether a dependent's edge on such a coordinate
    should be classified `unresolved` rather than `external` is a
    separate, open registry-policy question left for reviewer-3 - see
    the PR description.)"""
    pom = "<project><artifactId>acme-core</artifactId></project>"
    units, _edges, _profile_count = java.parse_maven_pom("acme-core/pom.xml", pom)
    assert units == []


# ----------------------------------------------------------- xml root sniff (round 14b)

def test_sniff_xml_root_element_recognizes_spring_beans():
    assert java.sniff_xml_root_element(
        "<beans><bean id=\"x\" class=\"y\"/></beans>") == "beans"


def test_sniff_xml_root_element_recognizes_logback_configuration():
    assert java.sniff_xml_root_element(
        "<configuration><root level=\"INFO\"/></configuration>") == "configuration"


def test_sniff_xml_root_element_recognizes_checkstyle_module():
    assert java.sniff_xml_root_element("<module name=\"Checker\"></module>") == "module"


def test_sniff_xml_root_element_skips_the_prolog_and_comments():
    text = (
        "<?xml version=\"1.0\"?>\n"
        "<!-- a leading comment with a <fake/> tag inside it -->\n"
        "<beans/>\n"
    )
    assert java.sniff_xml_root_element(text) == "beans"


def test_sniff_xml_root_element_strips_a_namespace_prefix():
    assert java.sniff_xml_root_element("<b:beans xmlns:b=\"x\"/>") == "beans"


def test_sniff_xml_root_element_returns_none_when_undeterminable():
    assert java.sniff_xml_root_element("not actually xml at all") is None


# ------------------------------------------------- xml root sniff hostile inputs (round 14c)

def test_sniff_xml_root_element_ignores_a_fake_beans_tag_inside_a_processing_instruction():
    """FIX ROUND 14c (reviewer-3's own real-file repro, pulled forward):
    a PI's raw content is not markup at all - a literal "<beans" living
    inside one (`<?custom-pi <beans> ?>`) must never be read as the real
    root, or problems.json ends up asserting a root the source never
    actually declared."""
    text = "<?custom-pi <beans> ?>\n<cfg/>\n"
    assert java.sniff_xml_root_element(text) == "cfg"


def test_sniff_xml_root_element_ignores_a_fake_beans_tag_inside_a_doctype_entity_value():
    """FIX ROUND 14c: a DOCTYPE internal subset's <!ENTITY> replacement
    text is not markup either - blanking the WHOLE doctype declaration
    (including its internal subset), offset-preserving, keeps this from
    ever being read as the real root."""
    text = (
        "<!DOCTYPE cfg [\n"
        "  <!ENTITY foo \"<beans>fake</beans>\">\n"
        "]>\n"
        "<cfg/>\n"
    )
    assert java.sniff_xml_root_element(text) == "cfg"


def test_sniff_xml_root_element_returns_none_for_an_unterminated_comment():
    """FIX ROUND 14c: an unterminated comment (no matching --> anywhere)
    is malformed input - everything from that point on cannot be
    trusted, so a <beans> tag living "inside" it must never be read as
    a real root. Fails toward record-only (None), the safe side."""
    text = "<!-- unterminated comment containing <beans\n<cfg/>\n"
    assert java.sniff_xml_root_element(text) is None


def test_sniff_xml_root_element_is_case_sensitive():
    """FIX ROUND 14c (reviewer-3's micro-note): XML element names are
    case-sensitive - the sniff itself must never fold case, so a caller
    comparing against an exact expected spelling gets an honest answer."""
    assert java.sniff_xml_root_element("<BEANS/>") == "BEANS"
    assert java.sniff_xml_root_element("<BEANS/>") != "beans"


def test_sniff_xml_root_element_recognizes_the_spring_dtd_form_doctype_as_beans():
    """FIX ROUND 14c: the DOCTYPE blanking must not blank PAST the
    doctype into the real root that follows it - Spring's own classic
    DTD-form beans file (a real, common shape) is the regression that
    proves the bounded [^\\[>]/[^>] character classes cannot cross the
    declaration's own closing '>'."""
    text = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE beans PUBLIC \"-//SPRING//DTD BEAN 2.0//EN\" "
        "\"http://www.springframework.org/dtd/spring-beans-2.0.dtd\">\n"
        "<beans><bean id=\"x\" class=\"y\"/></beans>\n"
    )
    assert java.sniff_xml_root_element(text) == "beans"


# ----------------------------------------------------------- honest gaps

def test_unsupported_relations_are_named_not_silently_omitted():
    assert java.UNSUPPORTED_RELATIONS == ("data", "configuration")


def test_unsupported_invoke_shapes_are_named_not_silently_omitted():
    """FIX ROUND 14 (tenth cold read, CR10-3 JUDGE): a constructor call
    (`new OrderNotFound(id)`) produces no invoke edge - a sub-shape gap
    within the otherwise-supported "invoke" relation, not a whole
    deferred relation, so it gets its own narrower, equally explicit
    enumeration rather than silence."""
    assert java.UNSUPPORTED_INVOKE_SHAPES == ("constructor_call",)


def test_a_constructor_call_produces_no_invoke_edge():
    result = java.parse_java_source(
        "OrderService.java",
        "package p;\nclass OrderService {\n"
        "  void run() { throw new OrderNotFound(1); }\n"
        "}\n",
    )
    assert not any(e.relation == "invoke" for e in result.edges)


# ----------------------------------------------------------- line lookup perf (M11)

def test_line_at_matches_naive_line_counting_for_every_offset():
    text = "line1\nline2\nline3\nline4\n"
    offsets = java._newline_offsets(text)
    for probe in range(len(text)):
        assert java._line_at(offsets, probe) == text.count("\n", 0, probe) + 1


def test_parsing_a_large_file_completes_well_under_a_generous_bound():
    """M11 (cold-read, PR-B fix round 3): _line_at previously recomputed a
    line count from offset 0 on EVERY call - once per import, per type,
    per invocation, per route match - making the adapter's total cost
    quadratic in file size (measured pre-fix: 0.27 MiB in 0.79s, 0.53 MiB
    in 3.02s, 1.07 MiB in 12.33s, ~4x per doubling; the 64 MiB per-file cap
    extrapolates to hours). A generous bound here (not a tight benchmark,
    to avoid CI flakiness) would fail hard under the old quadratic
    behavior but comfortably passes under the fixed O(n) + O(log n)-per-
    lookup behavior - this file parses in well under a second locally."""
    import time

    many_imports = "".join(f"import p.C{i};\n" for i in range(20_000))
    source = "package p;\n" + many_imports + "class Big {}\n"
    start = time.monotonic()
    result = java.parse_java_source("Big.java", source)
    elapsed = time.monotonic() - start
    assert len(_edges(result, "import")) == 20_000
    assert elapsed < 5.0, f"parsing took {elapsed:.2f}s - possible quadratic regression"
