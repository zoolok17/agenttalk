"""#55 slice-1 PR-B item 3: the bundled Java adapter
(DESIGN-55-comprehension-plane.md, Artifact 2's closed relation vocabulary;
approved PR-B plan item-3 relation-scope decision, 2026-08-27). Small,
synthetic fixtures only - not Amperian itself, kept fast and hermetic.
"""

from __future__ import annotations

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
    # shot at exact internal resolution; a static import's target is a
    # member path, and a wildcard import names a package - neither is a
    # single type, so both stay plain external.
    by_target = {e.target: e for e in imports}
    assert by_target["java.util.List"].target_kind == "internal_exact_or_external"
    assert by_target["java.util.Collections.emptyList"].target_kind == "external"
    assert by_target["com.example.other.*"].target_kind == "external"


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
    edges = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"org.springframework:spring-core", "junit:junit"}
    assert all(e.relation == "build" for e in edges)
    assert all(e.evidence_class == "declared" for e in edges)


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
    edges = java.parse_maven_pom("pom.xml", pom)
    assert {e.target for e in edges} == {"org.springframework:spring-core"}


# ----------------------------------------------------------- web.xml (route)

def test_parse_web_xml_extracts_servlet_mapping_routes():
    web_xml = """<web-app>
  <servlet-mapping>
    <servlet-name>dispatcher</servlet-name>
    <url-pattern>/api/*</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points = java.parse_web_xml("WEB-INF/web.xml", web_xml)
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
    entry_points = java.parse_web_xml("WEB-INF/web.xml", web_xml)
    assert [e.name for e in entry_points] == ["/api/*"]


# ----------------------------------------------------------- honest gaps

def test_unsupported_relations_are_named_not_silently_omitted():
    assert java.UNSUPPORTED_RELATIONS == ("data", "configuration")


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
