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
    """FIX ROUND 23 (nineteenth cold read, F3 MAJOR, wrong-data):
    README.md matches worker.py's own benign-extension allowlist (never
    even recorded a worker problem) - it is genuinely non-code
    infrastructure, not "production" application code, the reader's own
    named example of this class."""
    discovery = _discovery([EnumeratedFile(relative_path="README.md", byte_count=3, content_digest="abc")])
    records = ma.build_modules(discovery, {})
    assert len(records) == 1
    record = records[0]
    assert record.kind == "file"
    assert record.language == "unknown"
    assert record.paths == ["README.md"]
    assert record.container_unit_id is None
    assert record.classification == ["infrastructure"]


def test_a_file_under_a_test_path_is_classified_test():
    discovery = _discovery([
        EnumeratedFile(relative_path="src/test/resources/fixture.txt", byte_count=1, content_digest="d"),
    ])
    records = ma.build_modules(discovery, {})
    assert records[0].classification == ["test"]


def test_an_encoding_undecodable_non_adapter_xml_publishes_no_classification():
    """FIX ROUND 28 (twenty-fourth cold read, F2 BLOCKER, round-27
    regression, wrong-data): round 27's own F3 fix widened non_
    degrading_unsupported_language_paths to ALSO include encoding-
    undecodable non-adapter .xml files - correct for the DEGRADE
    question, but it fed the SAME set into classification, publishing a
    confident ["infrastructure"] for a file this producer admits it
    never actually read. `encoding_undecodable` in the worker's own
    recorded reasons must publish an EMPTY classification list instead -
    the closed vocabulary has no "unknown" member to guess into."""
    discovery = _discovery([
        EnumeratedFile(relative_path="beans.xml", byte_count=96, content_digest="e"),
    ])
    records = ma.build_modules(
        discovery, {},
        worker_problem_reasons_by_path={"beans.xml": ["encoding_undecodable"]},
        non_degrading_unsupported_language_paths=frozenset({"beans.xml"}),
    )
    assert len(records) == 1
    assert records[0].classification == []


def test_a_non_degrading_unsupported_language_path_without_encoding_undecodable_still_gets_infrastructure():
    """Companion control: round 28's own F2 fix narrows ONLY the
    encoding-undecodable case - a genuinely benign, readable non-
    degrading file (worker.py's own real positive "not code-bearing"
    evidence, e.g. an unsupported_language reason with no decode
    failure) is unaffected and still gets "infrastructure"."""
    discovery = _discovery([
        EnumeratedFile(relative_path="config.properties", byte_count=10, content_digest="f"),
    ])
    records = ma.build_modules(
        discovery, {},
        worker_problem_reasons_by_path={"config.properties": ["unsupported_language"]},
        non_degrading_unsupported_language_paths=frozenset({"config.properties"}),
    )
    assert len(records) == 1
    assert records[0].classification == ["infrastructure"]


def test_a_readable_tier2_xml_keeps_its_production_classification_unaffected():
    """Companion control: a genuinely-decodable tier-2 XML (a real
    Spring bean/Struts config - worker.py records its own degrading
    "unsupported_language" problem, but does NOT add it to the non-
    degrading set, since decoding succeeded and the shape is real
    unmodeled application code) must be entirely unaffected by the F2
    fix - production, same as before round 27/28 ever touched this
    path."""
    discovery = _discovery([
        EnumeratedFile(relative_path="beans.xml", byte_count=96, content_digest="g"),
    ])
    records = ma.build_modules(
        discovery, {},
        worker_problem_reasons_by_path={"beans.xml": ["unsupported_language"]},
    )
    assert len(records) == 1
    assert records[0].classification == ["production"]


def test_the_confident_infrastructure_boundary_is_platform_mandate_not_ci_flavor():
    """FIX ROUND 35 (twenty-ninth cold read, F8 LOW, JUDGE - argued, not
    churned): the reader measured what looked like two asymmetries -
    release.sh classifies infrastructure while release.py does not;
    Dockerfile classifies infrastructure while a top-level .github-ci.yml
    does not. Both are the SAME one rule (see
    _is_confident_infrastructure_path's own docstring): membership
    requires a name/extension/path segment MANDATED by one specific real
    tool or platform, not merely something that looks CI/build-flavored.
    A shell script's mere extension already proves a build/release role
    (round 23); a .py file's does not, since Python is just as often a
    genuine polyglot application service (round 17b) - so release.sh and
    release.py are correctly asymmetric, not inconsistently classified.
    Likewise a real GitHub Actions workflow already earns infrastructure
    through the well-known .github/workflows/ directory convention (the
    PATH-SEGMENT rule), while an arbitrary top-level .github-ci.yml is not
    a filename any platform mandates and correctly stays unclassified,
    same as any other arbitrary .yml. This test locks in both pairs so a
    future change cannot silently re-widen either rule (.py/.js by
    extension, or an arbitrary CI-flavored basename) without failing
    here first."""
    discovery = _discovery([
        EnumeratedFile(relative_path="release.sh", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="release.py", byte_count=1, content_digest="b"),
        EnumeratedFile(relative_path="Dockerfile", byte_count=1, content_digest="c"),
        EnumeratedFile(relative_path=".github-ci.yml", byte_count=1, content_digest="d"),
        EnumeratedFile(relative_path=".github/workflows/build.yml", byte_count=1, content_digest="e"),
    ])
    # All five are tier-3 (worker.py's own non-benign, non-adapter-
    # handled, non-degrading "unsupported_language") - none is on
    # worker.py's own BENIGN extension/basename list (that list is
    # reserved for genuinely inert files like README/.gitignore), so
    # every one of them reaches _is_confident_infrastructure_path as the
    # sole discriminator, never the separate "no worker problem at all"
    # branch a real README/.gitignore takes instead.
    tier3_paths = frozenset({
        "release.sh", "release.py", "Dockerfile", ".github-ci.yml",
        ".github/workflows/build.yml",
    })
    records = ma.build_modules(
        discovery, {},
        worker_problem_reasons_by_path={path: ["unsupported_language"] for path in tier3_paths},
        non_degrading_unsupported_language_paths=tier3_paths,
    )
    classification_by_path = {r.paths[0]: r.classification for r in records}
    assert classification_by_path["release.sh"] == ["infrastructure"]
    assert classification_by_path["release.py"] == []
    assert classification_by_path["Dockerfile"] == ["infrastructure"]
    assert classification_by_path[".github-ci.yml"] == []
    assert classification_by_path[".github/workflows/build.yml"] == ["infrastructure"]


def test_a_file_under_a_bare_test_package_segment_stays_production():
    """FIX ROUND 15 (eleventh cold read, F3 MAJOR, wrong-data): a bare
    "/test/" package segment NOT under the real build-convention root
    (src/test/...) has no corroborating evidence available at this
    file-record layer (no import/framework information here) - it must
    NEVER be guessed "test", the same "same bug class as CR10-7" fix
    the adapter's own per-type classifier already applies.

    FIX ROUND 23 (F3 MAJOR): this fixture's own .txt extension is
    ALSO a benign, non-degrading shape (worker.py's own allowlist) -
    it now derives "infrastructure" rather than "production" (a plain
    text file is not application code either), but the ORIGINAL point
    still holds unchanged: it is never "test" just because of the bare
    package segment."""
    discovery = _discovery([
        EnumeratedFile(
            relative_path="src/main/resources/com/lab/test/fixture.txt",
            byte_count=1, content_digest="d"),
    ])
    records = ma.build_modules(discovery, {})
    assert records[0].classification != ["test"]
    assert records[0].classification == ["infrastructure"]


def test_a_repository_root_test_directory_file_is_sufficient_alone():
    """FIX ROUND 15b (reviewer-3's MINOR 2, measured on an Ant layout): a
    REPOSITORY-ROOT test/ directory is a build convention exactly like
    src/test - sufficient alone at this layer too."""
    discovery = _discovery([
        EnumeratedFile(relative_path="test/fixtures/data.txt", byte_count=1, content_digest="d"),
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
        discovery, {}, worker_problem_reasons_by_path={"p/Broken.java": ["parse_failed"]})
    by_path = {r.paths[0]: r for r in records}
    assert by_path["p/Broken.java"].adapter_problem_reason == "parse_failed"
    assert by_path["p/Broken.java"].adapter_problem_reasons == ["parse_failed"]
    assert by_path["p/Broken.java"].language == "java"
    assert by_path["README.md"].adapter_problem_reason is None
    assert by_path["README.md"].adapter_problem_reasons == []


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
        worker_problem_reasons_by_path={
            "p/Huge.java": ["resource_limit"], "p/Skipped.java": ["path_excluded"],
        },
    )
    by_path = {r.paths[0]: r for r in records}
    assert by_path["p/Huge.java"].adapter_problem_reason == "resource_limit"
    assert by_path["p/Skipped.java"].adapter_problem_reason == "path_excluded"


def test_a_java_file_with_more_than_one_recorded_problem_publishes_a_single_reason_and_the_full_list():
    """MINOR 5 (sixth cold read, fix round 9): a path can legitimately
    have more than one distinct worker-recorded reason - the closed,
    enumerated adapter_problem_reason vocabulary must stay a SINGLE
    value (never a compound string like "no_types_extracted+
    resource_limit"), while the full sorted, deduplicated list is
    published separately, losing nothing."""
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Multi.java", byte_count=1, content_digest="a"),
    ])
    records = ma.build_modules(
        discovery, {},
        worker_problem_reasons_by_path={"p/Multi.java": ["no_types_extracted", "resource_limit"]},
    )
    record = records[0]
    assert record.adapter_problem_reason == "no_types_extracted"
    assert record.adapter_problem_reasons == ["no_types_extracted", "resource_limit"]


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


def test_language_matching_is_case_insensitive_matching_the_workers_own_dispatch(
) -> None:
    """FIX ROUND 37 (thirty-first cold read, F4 MAJOR, wrong-data,
    .cr31-upperext verbatim): _language_for_path matched case-
    SENSITIVELY (a plain endswith/in check) while worker.py's own
    dispatch - the thing that decides whether a file is actually
    parsed at all - matches case-insensitively (rel_lower/rel_name_
    lower). A .JAVA file was PARSED as Java (a real component unit,
    from java_results) but published language "unknown" - a
    contradiction between two facts about the identical file in the
    same run. Also covers the basename half (POM.XML) and a control
    (uppercase extensions this producer never maps to a language at
    all, unaffected either way)."""
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Upper.JAVA", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="POM.XML", byte_count=1, content_digest="b"),
        EnumeratedFile(relative_path="Notes.JSP", byte_count=1, content_digest="c"),
        EnumeratedFile(relative_path="dump.SQL", byte_count=1, content_digest="d"),
    ])
    java_results = {
        "p/Upper.JAVA": java_adapter.JavaFileResult(),
        "POM.XML": java_adapter.JavaFileResult(),
    }
    records = ma.build_modules(discovery, java_results)
    by_path = {r.paths[0]: r for r in records}
    assert by_path["p/Upper.JAVA"].language == "java"
    assert by_path["POM.XML"].language == "xml"
    assert by_path["Notes.JSP"].language == "unknown"
    assert by_path["dump.SQL"].language == "unknown"


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


def test_a_component_publishes_its_own_fully_qualified_name():
    """FIX ROUND 15 (eleventh cold read, N2 MINOR): a consumer had no way
    to recover a component's own FULLY QUALIFIED name from this artifact
    - only display_name (the rightmost simple-name segment), ambiguous
    the moment two same-named classes exist in different packages. A
    file-kind unit has no qualified name of its own - stays None."""
    source = "package p;\nclass Foo {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Foo.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Foo.java": _java_result("p/Foo.java", source)}
    records = ma.build_modules(discovery, java_results)
    component = next(r for r in records if r.kind == "component")
    file_record = next(r for r in records if r.kind == "file")
    assert component.qualified_name == "p.Foo"
    assert file_record.qualified_name is None


def test_a_pom_coordinate_component_publishes_the_artifact_id_as_display_name():
    """FIX ROUND 17b (reviewer-3's rejection of round 17, MINOR 4): a
    pom coordinate's own component re-derived display_name from
    qualified_name via a bare rightmost-dot-segment split
    ("com.acme:shop-web" -> "acme:shop-web" - neither the groupId nor
    the artifactId) instead of publishing parse_maven_pom's own,
    already-correct simple_name ("shop-web") - the CR13c simple_name
    carry becoming visible on a second producer. An ordinary Java
    type's own simple_name already agrees with the old derivation (see
    the companion fully-qualified-name test above, unaffected), so
    trusting the claim's own simple_name generally is a no-op there and
    the actual fix here."""
    pom_units, _edges, _count = java_adapter.parse_maven_pom(
        "pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>shop-web</artifactId></project>",
    )
    discovery = _discovery([
        EnumeratedFile(relative_path="pom.xml", byte_count=1, content_digest="a"),
    ])
    java_results = {"pom.xml": java_adapter.JavaFileResult(units=pom_units)}
    records = ma.build_modules(discovery, java_results)
    component = next(r for r in records if r.kind == "component")
    assert component.qualified_name == "com.acme:shop-web"
    assert component.display_name == "shop-web"


def test_a_pom_coordinate_component_and_its_file_record_both_publish_xml_language():
    """FIX ROUND 18 (fourteenth cold read, F4 MINOR, wrong-data): a
    pom's own published ``language`` used to FLIP between "java" and
    "xml" within a single run depending entirely on whether that
    specific pom happened to declare its own project-level groupId (a
    "component"-kind unit, hardcoded language="java" - false for an XML
    document) versus staying "file"-kind only (correctly "xml" via
    _language_for_path). Every pom-produced unit must now carry the
    identical, truthful language value regardless of which path it
    took - mirrors the reader's own reactor fixture with three poms:
    one with its own groupId, one inheriting groupId from <parent>, and
    one with neither."""
    own_group_units, _e1, _c1 = java_adapter.parse_maven_pom(
        "own/pom.xml",
        "<project><groupId>com.acme</groupId><artifactId>own</artifactId></project>",
    )
    inherited_units, _e2, _c2 = java_adapter.parse_maven_pom(
        "inherited/pom.xml",
        "<project><parent><groupId>com.acme</groupId>"
        "<artifactId>acme-parent</artifactId><version>1.0</version></parent>"
        "<artifactId>inherited</artifactId></project>",
    )
    discovery = _discovery([
        EnumeratedFile(relative_path="own/pom.xml", byte_count=1, content_digest="a"),
        EnumeratedFile(relative_path="inherited/pom.xml", byte_count=1, content_digest="b"),
        EnumeratedFile(relative_path="neither/pom.xml", byte_count=1, content_digest="c"),
    ])
    java_results = {
        "own/pom.xml": java_adapter.JavaFileResult(units=own_group_units),
        "inherited/pom.xml": java_adapter.JavaFileResult(units=inherited_units),
        "neither/pom.xml": java_adapter.JavaFileResult(),
    }
    records = ma.build_modules(discovery, java_results)
    languages = {record.language for record in records}
    assert languages == {"xml"}


def test_two_components_declaring_the_same_qualified_name_share_a_conflict_id():
    """FIX ROUND 16 (twelfth cold read, B1 BLOCKER, wrong-data): two units
    genuinely declaring the identical fully-qualified name (a real
    collision, e.g. two Maven modules both under ``com.acme``) used to
    publish ``conflict_id=None`` on both - the field existed
    (ModuleRecord.conflict_id) but nothing ever populated it. Mirrors
    reviewer-3's own ``.cr12-dup`` fixture shape: two modules each
    declaring ``com.acme.Config``. Unrelated components (a third,
    uniquely-named class) must stay conflict_id=None."""
    source_a = "package com.acme;\nclass Config {\n}\n"
    source_b = "package com.acme;\nclass Config {\n}\n"
    source_c = "package com.acme;\nclass Other {\n}\n"
    discovery = _discovery([
        EnumeratedFile(
            relative_path="modA/Config.java", byte_count=len(source_a), content_digest="a"),
        EnumeratedFile(
            relative_path="modB/Config.java", byte_count=len(source_b), content_digest="b"),
        EnumeratedFile(
            relative_path="modC/Other.java", byte_count=len(source_c), content_digest="c"),
    ])
    java_results = {
        "modA/Config.java": _java_result("modA/Config.java", source_a),
        "modB/Config.java": _java_result("modB/Config.java", source_b),
        "modC/Other.java": _java_result("modC/Other.java", source_c),
    }
    records = ma.build_modules(discovery, java_results)
    components = {r.paths[0]: r for r in records if r.kind == "component"}
    assert components["modA/Config.java"].conflict_id is not None
    assert (
        components["modA/Config.java"].conflict_id == components["modB/Config.java"].conflict_id
    )
    assert components["modC/Other.java"].conflict_id is None


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


def test_a_java_file_with_real_units_still_carries_its_own_worker_problem_reasons():
    """FIX ROUND 13b (reviewer-3's B1 class-closer companion fix): a
    file-wide worker-recorded problem (route_annotation_unassociated,
    route_value_unrecoverable, ...) used to reach adapter_problem_
    reason(s) ONLY through the "zero units extracted" fallback branch -
    a file that DOES have real declared types (the ordinary case for
    every one of those problem kinds; a class with a broken route
    annotation still has a valid declared type) silently dropped the
    reason here, so no readiness check downstream could ever see it.
    Both the per-type component AND the file unit must carry it -
    correct for a genuinely file-wide problem with no single owning
    type."""
    source = "package p;\nclass Foo {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Foo.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Foo.java": _java_result("p/Foo.java", source)}
    records = ma.build_modules(
        discovery, java_results,
        worker_problem_reasons_by_path={"p/Foo.java": ["route_value_unrecoverable"]},
    )
    component = next(r for r in records if r.kind == "component")
    file_record = next(r for r in records if r.kind == "file")
    assert component.adapter_problem_reason == "route_value_unrecoverable"
    assert component.adapter_problem_reasons == ["route_value_unrecoverable"]
    assert file_record.adapter_problem_reason == "route_value_unrecoverable"
    assert file_record.adapter_problem_reasons == ["route_value_unrecoverable"]


def test_an_attributed_worker_problem_reaches_only_its_own_enclosing_unit():
    """FIX ROUND 13c (reviewer-3's part 1 on round 13b): an ATTRIBUTED
    worker problem (keyed by (path, qualified_name) - e.g.
    cli_main_unrecognized, owned by one specific declared type) must
    reach ONLY that one unit's own record - never its siblings in the
    same file, and never the file-kind record itself (the file has no
    single "own entry-point signature" the way a specific type does)."""
    source = "package p;\nclass Alpha {\n}\nclass Beta {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="p/Multi.java", byte_count=len(source), content_digest="digest1"),
    ])
    java_results = {"p/Multi.java": _java_result("p/Multi.java", source)}
    records = ma.build_modules(
        discovery, java_results,
        worker_problem_reasons_by_unit={("p/Multi.java", "p.Beta"): ["cli_main_unrecognized"]},
    )
    alpha = next(r for r in records if r.display_name == "Alpha")
    beta = next(r for r in records if r.display_name == "Beta")
    file_record = next(r for r in records if r.kind == "file")
    assert alpha.adapter_problem_reason is None
    assert alpha.adapter_problem_reasons == []
    assert beta.adapter_problem_reason == "cli_main_unrecognized"
    assert beta.adapter_problem_reasons == ["cli_main_unrecognized"]
    assert file_record.adapter_problem_reason is None
    assert file_record.adapter_problem_reasons == []


def test_a_cross_file_reason_still_attaches_to_the_named_classs_own_unit():
    """FIX ROUND 21c (reviewer-3's re-delta, THE CARRY, wrong-data): a
    web.xml <listener>'s own unsupported_entry_point_shape problem is
    correctly recorded against web.xml's own path (it has no unit of
    its own) but NAMES a class declared in a completely different
    .java file - worker_problem_reasons_by_unit's own (path,
    qualified_name) key can never match across files.
    worker_problem_reasons_by_qualified_name resolves this via the
    class's own qualified name alone, regardless of which file the
    reason was originally recorded against."""
    source = "package com.acme;\nclass XmlListener {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="WEB-INF/web.xml", byte_count=1, content_digest="w"),
        EnumeratedFile(
            relative_path="com/acme/XmlListener.java", byte_count=len(source),
            content_digest="digest1"),
    ])
    java_results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(),
        "com/acme/XmlListener.java": _java_result("com/acme/XmlListener.java", source),
    }
    records = ma.build_modules(
        discovery, java_results,
        worker_problem_reasons_by_qualified_name={
            "com.acme.XmlListener": ["unsupported_entry_point_shape"]},
    )
    listener_unit = next(r for r in records if r.display_name == "XmlListener")
    assert listener_unit.adapter_problem_reason == "unsupported_entry_point_shape"
    assert listener_unit.adapter_problem_reasons == ["unsupported_entry_point_shape"]


def test_a_cross_file_reason_for_a_class_not_resolved_in_scan_invents_no_unit():
    """Companion negative case (the reviewer's own third test): a
    listener-class the run never actually walked (outside scope, or
    excluded) has ZERO claimants in the registry - the reason is left
    unattached, never fabricating a unit that does not exist. The
    web.xml-attributed problems.json record (built elsewhere, from the
    SAME worker problem) is the only trace of it either way, unchanged
    by this function."""
    discovery = _discovery([
        EnumeratedFile(relative_path="WEB-INF/web.xml", byte_count=1, content_digest="w"),
    ])
    java_results = {"WEB-INF/web.xml": java_adapter.JavaFileResult()}
    records = ma.build_modules(
        discovery, java_results,
        worker_problem_reasons_by_qualified_name={
            "com.acme.NotInScan": ["unsupported_entry_point_shape"]},
    )
    assert len(records) == 1
    assert records[0].kind == "file"
    assert records[0].adapter_problem_reasons == []


def test_a_cross_file_reason_for_a_duplicate_qualified_name_is_left_ambiguous():
    """A genuine registry collision (two units declaring the identical
    qualified name) already gets its own separate, visible conflict_id
    problem - a cross-file reason must not silently pick one of the two
    candidates to attach to."""
    source = "package com.acme;\nclass XmlListener {\n}\n"
    discovery = _discovery([
        EnumeratedFile(relative_path="WEB-INF/web.xml", byte_count=1, content_digest="w"),
        EnumeratedFile(
            relative_path="a/XmlListener.java", byte_count=len(source), content_digest="d1"),
        EnumeratedFile(
            relative_path="b/XmlListener.java", byte_count=len(source), content_digest="d2"),
    ])
    java_results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(),
        "a/XmlListener.java": _java_result("a/XmlListener.java", source),
        "b/XmlListener.java": _java_result("b/XmlListener.java", source),
    }
    records = ma.build_modules(
        discovery, java_results,
        worker_problem_reasons_by_qualified_name={
            "com.acme.XmlListener": ["unsupported_entry_point_shape"]},
    )
    components = [r for r in records if r.kind == "component"]
    assert len(components) == 2
    assert all(c.adapter_problem_reasons == [] for c in components)


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


def test_a_file_with_an_oversized_basename_gets_a_bounded_display_name():
    """FIX ROUND 42 (thirty-sixth cold read, F3 MAJOR, .cr36-label,
    completeness - THE RECONCILIATION): `display_name` (a file's own
    basename, or a component's own simple_name) used to publish fully
    unbounded, against Artifact-1's own "Bounded derived or declared
    label" promise and invariant 8's declared ceilings - the only
    backstop was the 16MiB whole-run refusal (graceful degradation with
    no inventory at all). `display_name` is never re-hashed or re-
    looked-up (`unit_id` is keyed on `paths`/`qualified_name`, never
    this field), so bounding it at display carries none of the
    collision risk round 41 was protecting against for an identity
    field - it is a pure label. The file's own `paths` entry (the real
    identity) stays raw/unbounded either way."""
    from agenttalk.comprehension.adapters.java import _MAX_ROUTE_TARGET_LENGTH

    oversized_name = "x" * (_MAX_ROUTE_TARGET_LENGTH + 50) + ".txt"
    discovery = _discovery([
        EnumeratedFile(relative_path=oversized_name, byte_count=3, content_digest="abc"),
    ])
    records = ma.build_modules(discovery, {})
    assert len(records) == 1
    assert records[0].display_name.endswith("...(truncated)")
    assert len(records[0].display_name) <= _MAX_ROUTE_TARGET_LENGTH + len("...(truncated)")
    assert records[0].paths == [oversized_name]
