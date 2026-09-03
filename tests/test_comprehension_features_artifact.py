"""#55 slice-1 PR-B item 6: features.json record assembly
(DESIGN-55-comprehension-plane.md, Artifact 3)."""

from __future__ import annotations

from agenttalk.comprehension import features_artifact as fa
from agenttalk.comprehension.adapters import java as java_adapter


def _parse(relative_path: str, source: str) -> java_adapter.JavaFileResult:
    return java_adapter.parse_java_source(relative_path, source)


def test_a_main_method_produces_one_candidate_feature_with_one_entry_point():
    results = {"App.java": _parse(
        "App.java",
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
    )}
    entry_points, features = fa.build_features(results)
    assert len(entry_points) == 1
    assert entry_points[0].kind == "cli_main"
    assert len(features) == 1
    assert features[0].state == "candidate"
    assert features[0].origin == "detected"
    assert features[0].label == "App"
    assert entry_points[0].feature_ids == [features[0].feature_id]


def test_source_digest_is_populated_from_file_digests():
    """M7 (cold-read, PR-B fix round 3): every producer here carried
    source_digest=None unconditionally - the design's per-fact producer
    identity, source content digest included, was never actually
    populated even though the digest was already available upstream."""
    results = {"App.java": _parse(
        "App.java",
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
    )}
    entry_points, features = fa.build_features(
        results, file_digests={"App.java": "deadbeef"})
    assert entry_points[0].producers[0]["source_digest"] == "deadbeef"
    assert features[0].producers[0]["source_digest"] == "deadbeef"


def test_source_digest_defaults_to_none_without_file_digests():
    results = {"App.java": _parse(
        "App.java",
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
    )}
    entry_points, features = fa.build_features(results)
    assert entry_points[0].producers[0]["source_digest"] is None


def test_web_xml_entry_point_gets_a_clean_label_not_the_file_extension():
    """Note 4 (second cold read, fix round 4): web.xml's synthetic
    qualified_name (f"{relative_path}#{servlet_name}") is not a dotted
    Java type name - splitting on "." landed in the middle of the file
    path's own ".xml" extension, producing a garbage label like
    "xml#legacy" instead of the actual servlet name."""
    entry_points, _web_problems, _edges, _descriptor_name_conflicts = java_adapter.parse_web_xml(
        "WEB-INF/web.xml",
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>legacy</servlet-name>\n"
        "    <url-pattern>/legacy/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
    )
    results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(entry_points=entry_points),
    }
    entry_point_records, features = fa.build_features(results)
    assert len(features) == 1
    assert features[0].label == "legacy"
    # FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data,
    # declared control): a web.xml servlet-mapping entry point's own
    # evidence_class is "declared" (an explicit descriptor declaration,
    # no source-code inference involved) - producers[].basis must match
    # it, never the hardcoded "extracted" every producer used to publish
    # regardless.
    assert entry_point_records[0].evidence_class == "declared"
    assert entry_point_records[0].producers[0]["basis"] == "declared"
    assert features[0].producers[0]["basis"] == "declared"


def test_web_xml_entry_point_owner_is_the_real_servlet_class_when_declared():
    """FIX ROUND 17 (thirteenth cold read, CR13-2 MAJOR, wrong-data): a
    mapped route whose <servlet-class> IS declared and resolves to a
    real in-scan unit must be owned by that class, not the web.xml
    file - features_artifact.build_features already resolves an entry
    point's owner through an exact qualified_name match; parse_web_xml
    now publishes the real class name, so no further plumbing is
    needed here."""
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
    entry_points, _web_problems, _edges, _descriptor_name_conflicts = (
        java_adapter.parse_web_xml("WEB-INF/web.xml", web_xml))
    servlet_source = (
        "package com.acme.web;\nclass DispatcherServlet {\n}\n"
    )
    results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(entry_points=entry_points),
        "com/acme/web/DispatcherServlet.java": java_adapter.parse_java_source(
            "com/acme/web/DispatcherServlet.java", servlet_source),
    }
    entry_point_records, features = fa.build_features(results)
    servlet_unit_id = fa.digests.unit_id(
        kind="component", paths=["com/acme/web/DispatcherServlet.java"],
        qualified_name="com.acme.web.DispatcherServlet",
    )
    assert len(entry_point_records) == 1
    assert entry_point_records[0].owning_unit_id == servlet_unit_id
    assert len(features) == 1
    assert features[0].label == "DispatcherServlet"
    assert features[0].unit_ids == [servlet_unit_id]


def test_web_xml_entry_point_owner_resolves_when_the_servlet_class_has_an_interior_comment():
    """FIX ROUND 38 (thirty-second cold read, F2 BLOCKER, .cr32-svclass,
    wrong-data): a comment interior to a <servlet-class>'s own value
    (Sv<!--x-->One) used to publish the owner's own qualified_name as
    the corrupted, blanked-whitespace spelling ("com.acme.Sv        One")
    - which can never exact-match the REAL in-scan class com.acme.
    SvOne, detaching the route from its real owner (falls back to the
    file, per the companion test below) and handing the real class a
    confident no_entry_point negative on an otherwise complete run, zero
    problems. The twin of test_web_xml_entry_point_owner_is_the_real_
    servlet_class_when_declared above, with an interior comment spliced
    out of the class value instead of a clean one."""
    web_xml = """<web-app>
  <servlet>
    <servlet-name>one</servlet-name>
    <servlet-class>com.acme.Sv<!--internal build tag-->One</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>one</servlet-name>
    <url-pattern>/one/*</url-pattern>
  </servlet-mapping>
</web-app>
"""
    entry_points, _web_problems, _edges, _descriptor_name_conflicts = (
        java_adapter.parse_web_xml("WEB-INF/web.xml", web_xml))
    assert entry_points[0].qualified_name == "com.acme.SvOne"
    servlet_source = "package com.acme;\nclass SvOne {\n}\n"
    results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(entry_points=entry_points),
        "com/acme/SvOne.java": java_adapter.parse_java_source(
            "com/acme/SvOne.java", servlet_source),
    }
    entry_point_records, features = fa.build_features(results)
    servlet_unit_id = fa.digests.unit_id(
        kind="component", paths=["com/acme/SvOne.java"], qualified_name="com.acme.SvOne",
    )
    assert len(entry_point_records) == 1
    assert entry_point_records[0].owning_unit_id == servlet_unit_id
    assert len(features) == 1
    assert features[0].unit_ids == [servlet_unit_id]


def test_web_xml_entry_point_still_owned_by_the_file_when_the_class_is_out_of_scan():
    """Companion: a <servlet-class> that IS declared but does not
    resolve to any in-scan unit (out-of-scope compiled dependency, or
    genuinely missing from this scan) must keep the web.xml file as
    owner - the same safe fallback an unresolved synthetic name already
    got - never a confident claim about a class this run cannot see."""
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
    entry_points, _web_problems, _edges, _descriptor_name_conflicts = (
        java_adapter.parse_web_xml("WEB-INF/web.xml", web_xml))
    results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(entry_points=entry_points),
    }
    entry_point_records, features = fa.build_features(results)
    file_unit_id = fa._java_file_unit_id("WEB-INF/web.xml")
    assert len(entry_point_records) == 1
    assert entry_point_records[0].owning_unit_id == file_unit_id
    # The class name is still visible - the feature label names it,
    # rather than only the bare servlet-name string.
    assert features[0].label == "DispatcherServlet"


def test_two_servlet_mappings_in_one_web_xml_produce_two_features_not_one():
    """FIX ROUND 14 (tenth cold read, CR10-8 MINOR, wrong-data): a
    web.xml with two <servlet-mapping> entries has no real declared-type
    owner for either claim, so both fell back to the SAME file unit - and
    grouping by owning_unit_id alone then collapsed them into ONE feature
    labelled after whichever servlet happened to be first, silently
    folding the second servlet's identity under the wrong name. Each
    independent file-fallback claim must get its own feature."""
    entry_points, _web_problems, _edges, _descriptor_name_conflicts = java_adapter.parse_web_xml(
        "WEB-INF/web.xml",
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>legacy</servlet-name>\n"
        "    <url-pattern>/legacy/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
    )
    results = {
        "WEB-INF/web.xml": java_adapter.JavaFileResult(entry_points=entry_points),
    }
    entry_point_records, features = fa.build_features(results)
    assert len(entry_point_records) == 2
    assert len(features) == 2
    assert sorted(f.label for f in features) == ["dispatcher", "legacy"]
    # each feature owns exactly its own entry point, not both
    for feature in features:
        assert len(feature.entry_point_ids) == 1
        owned = next(
            ep for ep in entry_point_records if ep.entry_point_id == feature.entry_point_ids[0])
        assert owned.name == ("/api/*" if feature.label == "dispatcher" else "/legacy/*")


def test_two_routes_on_the_same_controller_group_into_one_feature():
    source = (
        "package p;\nclass Controller {\n"
        '  @GetMapping("/api/widgets")\n  void list() {}\n'
        '  @PostMapping("/api/widgets")\n  void create() {}\n'
        "}\n"
    )
    results = {"Controller.java": _parse("Controller.java", source)}
    entry_points, features = fa.build_features(results)
    assert len(entry_points) == 2
    assert len(features) == 1
    assert sorted(features[0].entry_point_ids) == sorted(e.entry_point_id for e in entry_points)


def test_get_and_post_on_the_same_path_are_two_distinct_entry_points_not_a_duplicate_id():
    """M-5 (third cold read, fix round 5): GET and POST on the identical
    path used to compute the SAME entry_point_id (the HTTP method was not
    part of its identity) - the sibling test above only ever counted RAW
    entry-point records, which stayed 2 either way, so it never actually
    proved the two entry points were DISTINCT rather than a duplicate
    primary key published twice. Two methods on one route are two entry
    points to a migration reader, never collapsed into one."""
    source = (
        "package p;\nclass Controller {\n"
        '  @GetMapping("/orders")\n  void list() {}\n'
        '  @PostMapping("/orders")\n  void create() {}\n'
        "}\n"
    )
    results = {"Controller.java": _parse("Controller.java", source)}
    entry_points, features = fa.build_features(results)
    ids = [e.entry_point_id for e in entry_points]
    assert len(ids) == len(set(ids)), "two distinct routes published the SAME entry_point_id"
    assert {e.name for e in entry_points} == {"GET /orders", "POST /orders"}
    assert sorted(features[0].entry_point_ids) == sorted(ids)


def test_duplicate_entry_point_claims_coalesce_to_one_record_with_merged_producers():
    """M-5 (third cold read, fix round 5): two claims that genuinely
    normalize to the SAME entry_point_id (kind+owning_unit_id+name) must
    coalesce to one record with merged producers - the same merge rule
    dependencies_artifact.py's edge coalescing already applies (M6, round
    3) - never publish a duplicate "primary key" twice, and never list
    the same ID twice on the owning feature."""
    claim = java_adapter.JavaEntryPointClaim(
        qualified_name="p.App", kind="cli_main", name="main", line=3, evidence_class="extracted")
    result = java_adapter.JavaFileResult(
        units=[java_adapter.JavaUnitClaim(
            relative_path="App.java", qualified_name="p.App", simple_name="App",
            line=1, classification="production",
        )],
        entry_points=[claim, claim],
    )
    entry_points, features = fa.build_features(
        {"App.java": result}, file_digests={"App.java": "deadbeef"})
    assert len(entry_points) == 1
    assert len(features[0].entry_point_ids) == 1
    # FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data): this
    # claim's own evidence_class is "extracted" - producers[].basis must
    # match it exactly, never a hardcoded literal that happens to agree
    # here but not for a "declared"/"inferred" claim elsewhere.
    assert entry_points[0].producers[0]["basis"] == "extracted"
    assert features[0].producers[0]["basis"] == "extracted"
    assert len(entry_points[0].producers) == 1


def test_entry_points_on_different_classes_produce_separate_features():
    results = {
        "App.java": _parse(
            "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n"),
        "Controller.java": _parse(
            "Controller.java",
            'package p;\nclass Controller {\n  @GetMapping("/x")\n  void get() {}\n}\n',
        ),
    }
    entry_points, features = fa.build_features(results)
    assert len(features) == 2
    assert {f.label for f in features} == {"App", "Controller"}


def test_confirmed_labels_promote_feature_state():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    _entry_points, features = fa.build_features(results, confirmed_labels=frozenset({"App"}))
    assert features[0].state == "confirmed"


def test_unconfirmed_label_stays_candidate():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    _entry_points, features = fa.build_features(results, confirmed_labels=frozenset({"SomeOther"}))
    assert features[0].state == "candidate"


def test_no_entry_points_means_no_features():
    results = {"Widget.java": _parse("Widget.java", "package p;\nclass Widget {}\n")}
    entry_points, features = fa.build_features(results)
    assert entry_points == []
    assert features == []


def test_ids_are_deterministic_across_two_builds():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    first_ep, first_f = fa.build_features(results)
    second_ep, second_f = fa.build_features(results)
    assert {e.entry_point_id for e in first_ep} == {e.entry_point_id for e in second_ep}
    assert {f.feature_id for f in first_f} == {f.feature_id for f in second_f}


def test_to_json_sorts_id_lists():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    entry_points, features = fa.build_features(results)
    payload = features[0].to_json()
    assert payload["entry_point_ids"] == sorted(payload["entry_point_ids"])
    assert entry_points[0].to_json()["feature_ids"] == [features[0].feature_id]
