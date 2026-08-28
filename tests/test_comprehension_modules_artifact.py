"""#55 slice-1 PR-B item 4: modules.json record assembly
(DESIGN-55-comprehension-plane.md, Artifact 1)."""

from __future__ import annotations

from agenttalk.comprehension import modules_artifact as ma
from agenttalk.comprehension.adapters import java as java_adapter
from agenttalk.comprehension.discovery import DiscoveryResult, EnumeratedFile, PlatformIdentity

_PLATFORM = PlatformIdentity(
    os_family="nt", architecture="x64", path_normalization_version=1,
    case_sensitive=False, unicode_normalizing=False,
)


def _discovery(files: list[EnumeratedFile]) -> DiscoveryResult:
    return DiscoveryResult(platform_identity=_PLATFORM, files=files)


# ----------------------------------------------------------- non-Java files

def test_a_non_java_file_becomes_a_single_file_unit():
    discovery = _discovery([EnumeratedFile(relative_path="README.md", byte_count=3, content_digest="abc")])
    records = ma.build_modules(discovery, {})
    assert len(records) == 1
    record = records[0]
    assert record.kind == "file"
    assert record.language == "unknown"
    assert record.paths == ["README.md"]
    assert record.container_unit_id is None
    assert record.classification == ["production"]


def test_a_file_under_a_test_path_is_classified_test():
    discovery = _discovery([
        EnumeratedFile(relative_path="src/test/resources/fixture.txt", byte_count=1, content_digest="d"),
    ])
    records = ma.build_modules(discovery, {})
    assert records[0].classification == ["test"]


def test_a_parse_failed_java_file_is_flagged_distinctly_from_no_adapter():
    """B3 (cold-read, PR-B fix round 3): a .java file absent from
    java_results because the adapter failed (or the bytes could not be
    read) must be distinguishable from an ordinary non-java file with no
    adapter at all - only the former carries an adapter_problem_reason."""
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Broken.java", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="README.md", byte_count=1, content_digest="b"),
    ])
    records = ma.build_modules(
        discovery, {}, worker_problem_reason_by_path={"p/Broken.java": "parse_failed"})
    by_path = {r.paths[0]: r for r in records}
    assert by_path["p/Broken.java"].adapter_problem_reason == "parse_failed"
    assert by_path["p/Broken.java"].language == "java"
    assert by_path["README.md"].adapter_problem_reason is None


def test_a_resource_capped_java_file_also_carries_its_own_problem_reason():
    """M-2 (third cold read, fix round 5): round 3 threaded ONLY the
    ``parse_failed`` reason - a file the worker skipped for a DIFFERENT
    reason (the per-file adapter-work resource cap; a re-confinement
    rejection) fell through this exact same gap a second and third time.
    Threading EVERY worker problem, by its own reason_code, closes the
    class instead of adding a fourth manually-tracked negative case."""
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Huge.java", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="p/Skipped.java", byte_count=1, content_digest="b"),
    ])
    records = ma.build_modules(
        discovery, {},
        worker_problem_reason_by_path={
            "p/Huge.java": "resource_limit", "p/Skipped.java": "path_excluded",
        },
    )
    by_path = {r.paths[0]: r for r in records}
    assert by_path["p/Huge.java"].adapter_problem_reason == "resource_limit"
    assert by_path["p/Skipped.java"].adapter_problem_reason == "path_excluded"


def test_pom_xml_and_web_xml_are_recognized_as_adapter_understood():
    """M-2 (second cold read, fix round 4): pom.xml/web.xml go THROUGH the
    java adapter package (build_dependencies/build_features already
    consume their edges/entry points) but previously named no language of
    their own, so build_modules classified them "unknown" - identical to
    a file no adapter has ever touched - even though java_results proves
    an adapter demonstrably understood them."""
    discovery = _discovery([
        EnumeratedFile(relative_path="pom.xml", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="WEB-INF/web.xml", byte_count=1, content_digest="b"),
    ])
    java_results = {
        "pom.xml": java_adapter.JavaFileResult(),
        "WEB-INF/web.xml": java_adapter.JavaFileResult(),
    }
    records = ma.build_modules(discovery, java_results)
    by_path = {r.paths[0]: r for r in records}
    assert by_path["pom.xml"].language == "xml"
    assert by_path["WEB-INF/web.xml"].language == "xml"


# ----------------------------------------------------------- java files

def _java_result(relative_path: str, source: str) -> java_adapter.JavaFileResult:
    return java_adapter.parse_java_source(relative_path, source)


def test_a_java_file_with_one_top_level_type_produces_a_component_and_a_file_unit():
    source = "package p;\nclass Foo {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Foo.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Foo.java": _java_result("p/Foo.java", source)}
    records = ma.build_modules(discovery, java_results)

    kinds = sorted(r.kind for r in records)
    assert kinds == ["component", "file"]

    component = next(r for r in records if r.kind == "component")
    file_record = next(r for r in records if r.kind == "file")
    assert component.display_name == "Foo"
    assert component.language == "java"
    # Note 3 (second cold read, fix round 4): the FILE contains the
    # top-level type declared inside it, never the reverse - the file is
    # the top of its own containment chain (container_unit_id=None), and
    # the component's own container points AT the file.
    assert component.container_unit_id == file_record.unit_id
    assert file_record.container_unit_id is None


def test_a_nested_class_is_contained_by_its_outer_class():
    source = "package p;\nclass Outer {\n  class Inner {\n  }\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Outer.java", byte_count=len(source), content_digest="digest2"),
    ])
    java_results = {"p/Outer.java": _java_result("p/Outer.java", source)}
    records = ma.build_modules(discovery, java_results)

    outer = next(r for r in records if r.display_name == "Outer")
    inner = next(r for r in records if r.display_name == "Inner")
    file_record = next(r for r in records if r.kind == "file")

    # Note 3 (second cold read, fix round 4): the outer (top-level) type
    # is contained by the FILE, not the reverse; the inner type is
    # contained by its outer type, unchanged.
    assert outer.container_unit_id == file_record.unit_id
    assert inner.container_unit_id == outer.unit_id
    assert file_record.container_unit_id is None


def test_a_type_nested_three_deep_is_contained_by_its_immediate_outer_type():
    """M-4 (third cold read, fix round 5): the depth-2 test above stops
    exactly where the adapter's qualified-name corruption starts (a
    single stack entry, joined with nothing, happens to look correct
    either way) - this exercises the containment CHAIN one level deeper,
    where a corrupted qualified name would make _parent_qualified_name's
    rsplit("." , 1) lookup fail to find its immediate parent among the
    known names, and Innermost would fall back to being contained by the
    FILE instead of by Inner."""
    source = (
        "package p;\nclass Outer {\n  class Inner {\n  class Innermost {\n  }\n  }\n}\n"
    )
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Outer.java", byte_count=len(source), content_digest="d3"),
    ])
    java_results = {"p/Outer.java": _java_result("p/Outer.java", source)}
    records = ma.build_modules(discovery, java_results)

    outer = next(r for r in records if r.display_name == "Outer")
    inner = next(r for r in records if r.display_name == "Inner")
    innermost = next(r for r in records if r.display_name == "Innermost")
    file_record = next(r for r in records if r.kind == "file")

    assert innermost.unit_id != file_record.unit_id
    assert innermost.container_unit_id == inner.unit_id
    assert inner.container_unit_id == outer.unit_id
    assert outer.container_unit_id == file_record.unit_id


def test_unit_id_is_deterministic_across_two_builds():
    source = "package p;\nclass Foo {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Foo.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Foo.java": _java_result("p/Foo.java", source)}
    first = ma.build_modules(discovery, java_results)
    second = ma.build_modules(discovery, java_results)
    assert {r.unit_id for r in first} == {r.unit_id for r in second}


def test_java_file_with_no_declared_type_falls_back_to_a_plain_file_unit():
    source = "package p;\n// nothing declared\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Empty.java", byte_count=len(source), content_digest="digest3"),
    ])
    java_results = {"p/Empty.java": _java_result("p/Empty.java", source)}
    records = ma.build_modules(discovery, java_results)
    assert len(records) == 1
    assert records[0].kind == "file"


def test_component_producer_names_the_java_adapter():
    source = "package p;\nclass Foo {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Foo.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Foo.java": _java_result("p/Foo.java", source)}
    records = ma.build_modules(discovery, java_results)
    component = next(r for r in records if r.kind == "component")
    assert component.producers[0]["producer"] == java_adapter.ADAPTER_NAME
    assert component.producers[0]["producer_version"] == java_adapter.ADAPTER_VERSION
    assert component.producers[0]["source_digest"] == "digest1"


def test_to_json_sorts_paths_and_classification():
    source = "package p;\nclass Foo {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Foo.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Foo.java": _java_result("p/Foo.java", source)}
    records = ma.build_modules(discovery, java_results)
    payload = records[0].to_json()
    assert payload["paths"] == sorted(payload["paths"])
    assert "unit_id" in payload and "producers" in payload
