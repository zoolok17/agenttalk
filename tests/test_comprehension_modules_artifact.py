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
    adapter at all - only the former sets adapter_parse_failed."""
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Broken.java", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="README.md", byte_count=1, content_digest="b"),
    ])
    records = ma.build_modules(
        discovery, {}, parse_failed_paths=frozenset({"p/Broken.java"}))
    by_path = {r.paths[0]: r for r in records}
    assert by_path["p/Broken.java"].adapter_parse_failed is True
    assert by_path["p/Broken.java"].language == "java"
    assert by_path["README.md"].adapter_parse_failed is False


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
    assert component.container_unit_id is None  # top-level, nothing contains it
    assert file_record.container_unit_id == component.unit_id  # the component contains the file


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

    assert outer.container_unit_id is None
    assert inner.container_unit_id == outer.unit_id
    assert file_record.container_unit_id == outer.unit_id


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
