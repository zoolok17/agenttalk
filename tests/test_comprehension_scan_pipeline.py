"""#55 slice-1 PR-B item 9: end-to-end scan pipeline orchestration
(DESIGN-55-comprehension-plane.md, "Scan behavior"). Every test threads a
REAL PrivacyPreflightResult against a real git repo, per the same
discipline test_comprehension_lock.py established for PR-A.

The sanitized worker's OWN subprocess boundary is already covered by
test_comprehension_worker.py; these tests monkeypatch
scan_pipeline.worker.run_sanitized_worker to call worker.process_paths
in-process directly (same return type, same logic) so pipeline
orchestration is tested without depending on this dev host's ambient
`agenttalk` install being importable under a stripped, no-PYTHONPATH
subprocess environment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agenttalk.comprehension import scan_pipeline
from agenttalk.comprehension import worker as workermod
from agenttalk.comprehension.ceilings import ArtifactLimitExceeded
from agenttalk.comprehension.errors import VcsPrivacyRefused


@pytest.fixture(autouse=True)
def _inprocess_worker(monkeypatch):
    monkeypatch.setattr(
        scan_pipeline.worker, "run_sanitized_worker",
        lambda root, relative_paths, **_kwargs: workermod.process_paths(root, relative_paths),
    )


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")


def _write_sample_java_project(root: Path) -> None:
    app_dir = root / "src" / "main" / "java" / "p"
    app_dir.mkdir(parents=True)
    (app_dir / "App.java").write_text(
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
        encoding="utf-8",
    )
    (root / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )


@pytest.fixture()
def java_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    _write_sample_java_project(tmp_path)
    return tmp_path


# ----------------------------------------------------------- run_scan

def test_run_scan_publishes_a_complete_run(java_repo: Path) -> None:
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    assert outcome.run_dir.is_dir()
    assert (outcome.run_dir / "modules.json").exists()
    assert (outcome.run_dir / "dependencies.json").exists()
    assert (outcome.run_dir / "features.json").exists()
    assert (outcome.run_dir / "readiness.json").exists()
    assert (outcome.run_dir / "scan.json").exists()


def test_scan_json_names_unsupported_invoke_shapes_as_a_declared_gap(
    java_repo: Path,
) -> None:
    """FIX ROUND 14 (tenth cold read, CR10-3 JUDGE): a constructor call
    is a coverage gap within the otherwise-supported "invoke" relation -
    named here the same explicit, enumerated way UNSUPPORTED_RELATIONS
    already names data/configuration, never silent."""
    import json

    from agenttalk.comprehension.adapters import java as java_adapter

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["unsupported_invoke_shapes"] == list(
        java_adapter.UNSUPPORTED_INVOKE_SHAPES)


def test_scan_json_names_unsupported_entry_point_shapes_as_a_declared_gap(
    java_repo: Path,
) -> None:
    """FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, part (b) - THE
    CLASS-CLOSER): the entry-point edition of the same enumerated-
    coverage-gap idiom UNSUPPORTED_INVOKE_SHAPES/UNSUPPORTED_RELATIONS
    already establish."""
    import json

    from agenttalk.comprehension.adapters import java as java_adapter

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["unsupported_entry_point_shapes"] == list(
        java_adapter.UNSUPPORTED_ENTRY_POINT_SHAPES)


def test_scan_json_names_the_entry_point_kind_vocabulary_as_a_declared_capability(
    java_repo: Path,
) -> None:
    """FIX ROUND 21c (reviewer-3's re-delta, THE ASK - second instance,
    closing the class): a consumer reading an entry point's own `kind`
    (e.g. "http_filter") needs a declared meaning to know it is
    deliberately excluded from a served-route count, not merely a
    differently-spelled synonym for "http_route" - the same static-
    capability-declaration shape unsupported_entry_point_shapes and its
    two siblings already establish."""
    import json

    from agenttalk.comprehension.adapters import java as java_adapter

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["entry_point_kinds"] == dict(java_adapter.ENTRY_POINT_KINDS)
    assert set(scan_doc["entry_point_kinds"]) == {"cli_main", "http_route", "http_filter"}


def test_a_served_route_a_filter_and_a_listener_report_correctly_distinct_shapes(
    java_repo: Path,
) -> None:
    """FIX ROUND 21b (reviewer-3's re-delta, THE MAJOR, wrong-data,
    end-to-end): the reviewer's own 3-class shape verbatim - one class
    with a real served route (@WebServlet), one with an interception-
    only @WebFilter, one lifecycle-only @WebListener. Before this fix,
    the filter published kind=http_route indistinguishable from the
    servlet's own real route, inflating an app with ONE served endpoint
    to look like TWO. Now: exactly one served route in the served
    count, the filter visible under its own http_filter kind (not
    counted as served), and the listener still unmodeled (enrolled as
    unsupported_entry_point_shape, never a confident negative)."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "p"
    (pkg_dir / "OrdersServlet.java").write_text(
        'package p;\n@WebServlet("/orders")\npublic class OrdersServlet '
        'extends HttpServlet {}\n',
        encoding="utf-8",
    )
    (pkg_dir / "AuthFilter.java").write_text(
        'package p;\n@WebFilter(urlPatterns = {"/api/*"})\n'
        "public class AuthFilter implements Filter {}\n",
        encoding="utf-8",
    )
    (pkg_dir / "AppLifecycleListener.java").write_text(
        "package p;\n@WebListener\npublic class AppLifecycleListener "
        "implements ServletContextListener {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)

    assert report["counts"]["entry_points_by_kind"].get("http_route") == 1
    assert report["counts"]["entry_points_by_kind"].get("http_filter") == 1
    assert "http_route" not in [
        e["kind"] for e in report["entry_points"] if e["name"] == "/api/*"]
    filter_entries = [e for e in report["entry_points"] if e["name"] == "/api/*"]
    assert len(filter_entries) == 1
    assert filter_entries[0]["kind"] == "http_filter"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    listener_problems = [
        p for p in problems_doc["problems"]
        if p.get("qualified_name") == "p.AppLifecycleListener"
        and p["reason_code"] == "unsupported_entry_point_shape"
    ]
    assert len(listener_problems) == 1
    # The listener publishes no entry point at all (only the problem
    # above) - just the java_repo fixture's own App.main (cli_main) plus
    # the served route and the filter, nothing from the listener.
    assert len(report["entry_points"]) == 3
    assert report["counts"]["entry_points_by_kind"] == {
        "cli_main": 1, "http_filter": 1, "http_route": 1,
    }

    # DECIDED (round 21b, THE MAJOR's own entry_points_mapped question):
    # a filter-only unit still reports entry_points_mapped satisfied -
    # this signal answers "did this run find a real, evidenced boundary
    # construct," not "does this unit serve a complete route," and a
    # declared filter genuinely is such a construct.
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    filter_unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == "component" and u["display_name"] == "AuthFilter")
    filter_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == filter_unit_id and s["check"] == "entry_points_mapped")
    assert filter_signal["stored_status"] == "satisfied"


def test_web_xml_listener_reason_attaches_to_its_own_java_files_unit_end_to_end(
    java_repo: Path,
) -> None:
    """FIX ROUND 21c (reviewer-3's re-delta, THE CARRY, wrong-data,
    end-to-end): the reviewer's own exact shape - a web.xml <listener>
    names a class declared in a completely different .java file.
    Before this fix, the resulting unsupported_entry_point_shape
    problem (correctly recorded against web.xml's own path - web.xml
    has no unit of its own) never reached the listener class's own
    unit, so readiness published the confident negative not_applicable/
    no_entry_point on a class this run already knew carried an
    unmodeled listener. The @WebListener ANNOTATION spelling never had
    this problem (recorded same-file already) - both must report
    identically now."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "p"
    (pkg_dir / "XmlListener.java").write_text(
        "package p;\npublic class XmlListener implements ServletContextListener {}\n",
        encoding="utf-8",
    )
    (pkg_dir / "AnnotationListener.java").write_text(
        "package p;\n@WebListener\npublic class AnnotationListener "
        "implements ServletContextListener {}\n",
        encoding="utf-8",
    )
    (java_repo / "WEB-INF").mkdir(parents=True)
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <listener>\n"
        "    <listener-class>p.XmlListener</listener-class>\n"
        "  </listener>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    xml_listener_problems = [
        p for p in problems_doc["problems"]
        if p.get("qualified_name") == "p.XmlListener"
        and p["reason_code"] == "unsupported_entry_point_shape"
    ]
    assert len(xml_listener_problems) == 1
    # Recorded against web.xml's own path - unchanged, never moved.
    assert xml_listener_problems[0]["path"] == "WEB-INF/web.xml"

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))

    for class_name in ("XmlListener", "AnnotationListener"):
        unit_id = next(
            u["unit_id"] for u in modules_doc["units"]
            if u["kind"] == "component" and u["display_name"] == class_name)
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == unit_id and s["check"] == "entry_points_mapped")
        assert signal["stored_status"] == "unknown", class_name
        assert signal["reason_code"] == "unsupported_entry_point_shape", class_name


def _readiness_signal(readiness_doc, modules_doc, *, display_name, kind, check):
    unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == kind and u["display_name"] == display_name)
    return next(
        s for s in readiness_doc["signals"] if s["unit_id"] == unit_id and s["check"] == check)


def test_a_single_type_files_own_unit_mirrors_its_components_entry_point_signals(
    java_repo: Path,
) -> None:
    """FIX ROUND 22 (eighteenth cold read, F1 MAJOR, wrong-data): the
    reader's own .cr18-jee shape - a file's own entry_points_mapped/
    feature_linked used to be computed straight from entry_point_
    owner_ids/feature_states_by_unit, which build_features attaches to
    the COMPONENT's own unit_id whenever its qualified name resolves
    (the common case) - never the file's. A single-type file's own
    FILE-kind unit therefore published the confident negative
    (not_applicable/no_entry_point, unsatisfied/no_feature_link) even
    though its one contained class genuinely serves a real, mapped
    entry point in the SAME run - two contradictory published facts
    about the identical file. Now the file mirrors its one component's
    verdict exactly, for BOTH a served route (OrderServlet, kind
    http_route) and an interception-only filter (AuditFilter, kind
    http_filter - round 21b's own distinct kind)."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "p"
    (pkg_dir / "OrderServlet.java").write_text(
        'package p;\n@WebServlet("/orders")\npublic class OrderServlet '
        'extends HttpServlet {}\n',
        encoding="utf-8",
    )
    (pkg_dir / "AuditFilter.java").write_text(
        'package p;\n@WebFilter(urlPatterns = {"/audit/*"})\n'
        "public class AuditFilter implements Filter {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    for class_name in ("OrderServlet", "AuditFilter"):
        component_signal = _readiness_signal(
            readiness_doc, modules_doc, display_name=class_name, kind="component",
            check="entry_points_mapped")
        file_signal = _readiness_signal(
            readiness_doc, modules_doc, display_name=f"{class_name}.java", kind="file",
            check="entry_points_mapped")
        assert component_signal["stored_status"] == "satisfied", class_name
        assert file_signal["stored_status"] == "satisfied", class_name
        assert file_signal["reason_code"] == component_signal["reason_code"], class_name

    projection = scan_pipeline.get_report(java_repo)
    units_without_feature_ids = set(projection.get("units_without_feature", []))
    for class_name in ("OrderServlet", "AuditFilter"):
        file_unit_id = next(
            u["unit_id"] for u in modules_doc["units"]
            if u["kind"] == "file" and u["display_name"] == f"{class_name}.java")
        assert file_unit_id not in units_without_feature_ids, class_name


def test_a_multi_type_files_own_unit_never_reports_more_confident_than_its_worst_component(
    java_repo: Path,
) -> None:
    """FIX ROUND 22 (F1 MAJOR): a multi-type file aggregates worse-of
    across its own direct top-level components - a real satisfied claim
    from one class (a served route) wins over an unrelated sibling's
    mere absence of one (a plain helper class), since the two are
    different, non-conflicting facts about different declared types."""
    import json

    source = (
        "package p;\n"
        '@WebServlet("/wide")\n'
        "class WideServlet extends HttpServlet {}\n"
        "class PlainHelper {}\n"
    )
    (java_repo / "src" / "main" / "java" / "p" / "Multi.java").write_text(
        source, encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="Multi.java", kind="file",
        check="entry_points_mapped")
    assert file_signal["stored_status"] == "satisfied"
    assert file_signal["reason_code"] == "entry_point_mapped"


def test_a_genuinely_entry_point_free_file_keeps_its_honest_negative(java_repo: Path) -> None:
    """FIX ROUND 22 (F1 MAJOR): a companion control - a file whose only
    contained class genuinely has no entry point and no feature link
    keeps reporting the honest negative, unaffected by this round's fix
    (never a regression toward an un-evidenced positive)."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "PlainDao.java").write_text(
        "package p;\nclass PlainDao {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="PlainDao.java", kind="file",
        check="entry_points_mapped")
    component_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="PlainDao", kind="component",
        check="entry_points_mapped")
    assert file_signal["stored_status"] == "not_applicable"
    assert component_signal["stored_status"] == "not_applicable"
    feature_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="PlainDao.java", kind="file",
        check="feature_linked")
    assert feature_signal["stored_status"] == "unsatisfied"


def test_non_code_infrastructure_files_no_longer_publish_as_production(
    java_repo: Path,
) -> None:
    """FIX ROUND 23 (nineteenth cold read, F3 MAJOR, wrong-data): the
    reader's own .cr19-cls shape - README, LICENSE, Dockerfile, mvnw, a
    CI YAML, and a release script all used to publish classification=
    production, indistinguishable from real application code to a
    consumer scoping migration work by classification==production. The
    run already knows these are not application code (worker.py's own
    non-degrading unsupported_language problem, or complete silence for
    a benign-extension file like README.md) - now derived as
    "infrastructure" instead. A genuine Java class and an unmodeled
    TIER-2 shape (a .jsp - real, unmodeled application code, NOT
    infrastructure) are both unaffected."""
    import json

    (java_repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (java_repo / "LICENSE").write_text("Apache 2.0\n", encoding="utf-8")
    (java_repo / "Dockerfile").write_text("FROM eclipse-temurin:21\n", encoding="utf-8")
    (java_repo / "mvnw").write_text("#!/bin/sh\necho mvnw\n", encoding="utf-8")
    (java_repo / ".github" / "workflows").mkdir(parents=True)
    (java_repo / ".github" / "workflows" / "ci.yml").write_text(
        "on: push\njobs: {}\n", encoding="utf-8")
    (java_repo / "release.sh").write_text("#!/bin/sh\necho release\n", encoding="utf-8")
    (java_repo / "src" / "main" / "webapp" / "index.jsp").parent.mkdir(
        parents=True, exist_ok=True)
    (java_repo / "src" / "main" / "webapp" / "index.jsp").write_text(
        "<%= \"hi\" %>\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))

    infra_names = {
        "README.md", "LICENSE", "Dockerfile", "mvnw", "ci.yml", "release.sh"}
    for name in infra_names:
        unit = next(u for u in modules_doc["units"] if u["display_name"] == name)
        assert unit["classification"] == ["infrastructure"], name

    app_unit = next(u for u in modules_doc["units"] if u["display_name"] == "App.java")
    assert app_unit["classification"] == ["production"]

    jsp_unit = next(u for u in modules_doc["units"] if u["display_name"] == "index.jsp")
    assert jsp_unit["classification"] == ["production"]


def test_polyglot_application_source_in_tier_3_is_not_classified_infrastructure(
    java_repo: Path,
) -> None:
    """FIX ROUND 32 (twenty-eighth cold read, F3 MAJOR, wrong-data):
    mirrors the reader's own .cr28-polyapp shape - a real Node/Express
    service (server.js) and a Python ETL job (etl_job.py) are BOTH tier-3
    (worker.py's own round-17b "routinely incidental" exclusion from
    tier 2) alongside a Dockerfile, but they are genuine, unmodeled
    APPLICATION source, not build/tooling/infra - the OLD tier-membership
    discriminator classified all of them identically as "infrastructure".
    Neither now gets a guessed classification at all; the Dockerfile
    (confidently infrastructure) and a real Java class (production) stay
    exactly as before."""
    import json

    (java_repo / "server.js").write_text(
        "const app = require('express')();\napp.listen(3000);\n", encoding="utf-8")
    (java_repo / "etl_job.py").write_text(
        "def run():\n    pass\n", encoding="utf-8")
    (java_repo / "Dockerfile").write_text("FROM eclipse-temurin:21\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))

    server_unit = next(u for u in modules_doc["units"] if u["display_name"] == "server.js")
    etl_unit = next(u for u in modules_doc["units"] if u["display_name"] == "etl_job.py")
    dockerfile_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Dockerfile")
    app_unit = next(u for u in modules_doc["units"] if u["display_name"] == "App.java")

    assert server_unit["classification"] == []
    assert etl_unit["classification"] == []
    assert dockerfile_unit["classification"] == ["infrastructure"]
    assert app_unit["classification"] == ["production"]


def test_a_files_own_signal_rolls_up_through_a_nested_entry_point_class(
    java_repo: Path,
) -> None:
    """FIX ROUND 22b (reviewer-3's delta on round 22, R1, wrong-data -
    SCOPE overturned): the reviewer's own repro verbatim - a statically
    NESTED @WebListener class is never a DIRECT child of the FILE unit
    (a nested type's own container is its outer type, never the file
    directly - N6/round 6) - round 22's own F1 fix consulted direct
    children only, so Host.java still published the confident
    no_entry_point negative while p.Host.Inner correctly reported
    unknown in the SAME run. Now walks the full containment chain (the
    same _transitive_descendants helper round 15b already uses for
    this identical unit/file relationship) - Host.java correctly
    mirrors its nested descendant's own unknown."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Host.java").write_text(
        "package p;\npublic class Host {\n"
        "  @WebListener\n"
        "  static class Inner {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    inner_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="Inner", kind="component",
        check="entry_points_mapped")
    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="Host.java", kind="file",
        check="entry_points_mapped")
    assert inner_signal["stored_status"] == "unknown"
    assert inner_signal["reason_code"] == "unsupported_entry_point_shape"
    assert file_signal["stored_status"] == "unknown"
    assert file_signal["reason_code"] == "unsupported_entry_point_shape"


def test_a_files_own_signal_rolls_up_through_double_nested_entry_point_class(
    java_repo: Path,
) -> None:
    """FIX ROUND 22b (R1): deep double-nesting - the entry-point-
    carrying class is TWO levels of static nesting below the file's own
    top-level type, still reached by the full containment-chain walk."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Outer.java").write_text(
        "package p;\npublic class Outer {\n"
        "  static class Middle {\n"
        "    @WebListener\n"
        "    static class Deep {}\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="Outer.java", kind="file",
        check="entry_points_mapped")
    assert file_signal["stored_status"] == "unknown"
    assert file_signal["reason_code"] == "unsupported_entry_point_shape"


def test_a_dependency_free_multi_route_controller_publishes_zero_external_end_to_end(
    java_repo: Path,
) -> None:
    """FIX ROUND 22 (eighteenth cold read, F2 MAJOR, wrong-data,
    end-to-end): the reader's own measured shape - a dependency-free
    controller with several routes used to publish external:N and a
    high_fan_out_units entry naming it, purely from its own route
    edges. Now routes are visible under their own dedicated count, and
    a real external import in a DIFFERENT class still counts normally."""
    pkg_dir = java_repo / "src" / "main" / "java" / "p"
    routes = "\n".join(f'  @GetMapping("/wide/{i}")\n  public void h{i}() {{}}' for i in range(7))
    (pkg_dir / "WideController.java").write_text(
        f"package p;\n@RestController\npublic class WideController {{\n{routes}\n}}\n", encoding="utf-8")
    (pkg_dir / "UsesExternal.java").write_text(
        "package p;\nimport java.util.List;\nclass UsesExternal {\n"
        "  List<String> items;\n"
        "}\n",
        encoding="utf-8",
    )

    scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)

    assert report["dependency_summary"]["routes"] == 7
    assert report["dependency_summary"]["external"] >= 1
    assert not any(
        row["unit_id"] == _component_unit_id(report, "WideController")
        for row in report["high_fan_out_units"])


def test_web_xml_declaring_an_in_scan_servlet_route_is_satisfied_end_to_end_f1(
    java_repo: Path,
) -> None:
    """FIX ROUND 27 (twenty-third cold read, F1 BLOCKER, wrong-data,
    .cr23-webxml-neg, end-to-end): a web.xml that DECLARES a route
    whose <servlet-class> resolves in-scan used to publish entry_
    points_mapped not_applicable/no_entry_point on a complete/0-problem
    run - ownership moves to the implementing class (CR13-2), and
    nothing credited the DECLARING file with the evidence. Verified
    through the REAL pipeline, not a hand-built fixture."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "com" / "acme"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "CheckoutServlet.java").write_text(
        "package com.acme;\npublic class CheckoutServlet {}\n", encoding="utf-8")
    webinf = java_repo / "WEB-INF"
    webinf.mkdir(exist_ok=True)
    (webinf / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>Checkout</servlet-name>\n"
        "    <servlet-class>com.acme.CheckoutServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>Checkout</servlet-name>\n"
        "    <url-pattern>/checkout</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    webxml_unit = next(
        u for u in modules_doc["units"]
        if u["kind"] == "file" and "WEB-INF/web.xml" in u["paths"])
    signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == webxml_unit["unit_id"] and s["check"] == "entry_points_mapped")
    assert signal["stored_status"] == "satisfied"


def test_web_xml_metadata_complete_suppresses_an_annotation_route_end_to_end(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, M2 MAJOR, wrong-data,
    end-to-end): Servlet 3.0 s8.1 - a web.xml declaring metadata-
    complete="true" makes the container ignore EVERY servlet/filter
    annotation, but this fact is only knowable from web.xml, and the
    @WebServlet-decorated route lives in a DIFFERENT file entirely -
    verified through the real pipeline (worker.py's own cross-file
    pre-scan), not a single-file adapter call."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "com" / "acme"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "LegacyServlet.java").write_text(
        "package com.acme;\n"
        "@WebServlet(urlPatterns = {\"/legacy\"})\n"
        "public class LegacyServlet extends HttpServlet {\n"
        "}\n",
        encoding="utf-8")
    webinf = java_repo / "WEB-INF"
    webinf.mkdir(exist_ok=True)
    (webinf / "web.xml").write_text(
        '<web-app metadata-complete="true">\n'
        "</web-app>\n",
        encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "LegacyServlet")
    assert "unsupported_entry_point_shape" in servlet_unit["adapter_problem_reasons"]

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_edges = [e for e in dependencies_doc["edges"] if e["relation"] == "route"]
    assert route_edges == []

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    metadata_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unsupported_entry_point_shape" and "metadata-complete" in p["detail"]
    ]
    assert len(metadata_problems) == 1


def test_web_xml_metadata_complete_absent_control_end_to_end(java_repo: Path) -> None:
    """Control for the fix above: an ordinary web.xml with no metadata-
    complete attribute at all must keep publishing the annotation
    route exactly as before this round."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "com" / "acme"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "LegacyServlet.java").write_text(
        "package com.acme;\n"
        "@WebServlet(urlPatterns = {\"/legacy\"})\n"
        "public class LegacyServlet extends HttpServlet {\n"
        "}\n",
        encoding="utf-8")
    webinf = java_repo / "WEB-INF"
    webinf.mkdir(exist_ok=True)
    (webinf / "web.xml").write_text("<web-app>\n</web-app>\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_edges = [e for e in dependencies_doc["edges"] if e["relation"] == "route"]
    assert [e["target_external"] for e in route_edges] == ["/legacy"]


def test_web_xml_mapping_naming_an_annotation_only_servlet_resolves_end_to_end(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, M3 MAJOR, wrong-data,
    end-to-end): the cross-file join itself - a web.xml <servlet-
    mapping> names a servlet that is declared ONLY by a real, in-scan
    @WebServlet(name=...) annotation IN A DIFFERENT FILE, with no
    matching <servlet> element in web.xml at all. Verified through the
    real pipeline (worker.py's own deferred-web.xml pass, which needs
    the FULL cross-file name registry regardless of which file this
    run happens to read first) - not a hand-built fixture at the
    adapter level."""
    import json

    pkg_dir = java_repo / "src" / "main" / "java" / "com" / "acme"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "CheckoutServlet.java").write_text(
        "package com.acme;\n"
        "@WebServlet(name = \"Checkout\", urlPatterns = {\"/checkout\"})\n"
        "public class CheckoutServlet extends HttpServlet {\n"
        "}\n",
        encoding="utf-8")
    webinf = java_repo / "WEB-INF"
    webinf.mkdir(exist_ok=True)
    (webinf / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>Checkout</servlet-name>\n"
        "    <url-pattern>/checkout</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(p["reason_code"] == "undeclared_descriptor_name" for p in problems_doc["problems"])

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "CheckoutServlet")
    signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == servlet_unit["unit_id"] and s["check"] == "entry_points_mapped")
    assert signal["stored_status"] == "satisfied"

    webxml_unit = next(
        u for u in modules_doc["units"]
        if u["kind"] == "file" and "WEB-INF/web.xml" in u["paths"])
    webxml_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == webxml_unit["unit_id"] and s["check"] == "entry_points_mapped")
    assert webxml_signal["stored_status"] == "not_applicable"


def test_web_xml_route_and_filter_edges_match_the_entry_point_count_end_to_end_f4(
    java_repo: Path,
) -> None:
    """FIX ROUND 27 (F4, mechanism confirmed, .cr23-jee, end-to-end): a
    web.xml declaring both a servlet route and a filter used to report
    matching entry points but a dependency_summary.routes count that
    disagreed (2 entry points, 0 routes, since neither emitted its own
    edge) - now both counts agree."""
    pkg_dir = java_repo / "src" / "main" / "java" / "com" / "acme"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "CheckoutServlet.java").write_text(
        "package com.acme;\npublic class CheckoutServlet {}\n", encoding="utf-8")
    (pkg_dir / "AuthFilter.java").write_text(
        "package com.acme;\npublic class AuthFilter {}\n", encoding="utf-8")
    webinf = java_repo / "WEB-INF"
    webinf.mkdir(exist_ok=True)
    (webinf / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>Checkout</servlet-name>\n"
        "    <servlet-class>com.acme.CheckoutServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>Checkout</servlet-name>\n"
        "    <url-pattern>/checkout</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <filter>\n"
        "    <filter-name>Auth</filter-name>\n"
        "    <filter-class>com.acme.AuthFilter</filter-class>\n"
        "  </filter>\n"
        "  <filter-mapping>\n"
        "    <filter-name>Auth</filter-name>\n"
        "    <url-pattern>/secure/*</url-pattern>\n"
        "  </filter-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")

    scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)
    web_xml_entry_points = [
        e for e in report["entry_points"] if e["name"] in ("/checkout", "/secure/*")]
    assert len(web_xml_entry_points) == 2
    assert report["dependency_summary"]["routes"] >= 2


def _component_unit_id(report, display_name):
    return next(
        u["unit_id"] for u in report["units"]
        if u["kind"] == "component" and u["display_name"] == display_name)


def test_run_scan_web_servlet_and_jax_rs_routes_publish_end_to_end_cr13_c(
    java_repo: Path,
) -> None:
    """FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, wrong-data):
    mirrors the reader's own .cr13-c shape - both @WebServlet and JAX-RS
    @Path publish real entry points end to end, no synthetic edge
    construction. Both used to publish NO entry point and NO problem at
    all - the class landing on the confident negative.

    CORRECTED (round 39, F3 MAJOR, wrong-data - confident false
    positive): the JAX-RS half of this fixture (a method-level @Path
    with no verb designator of its own) is JAX-RS's own sub-resource-
    locator idiom (JSR-339) - it never handles a request directly, so
    publishing it as a confident served http_route was itself a false
    positive this round corrects. It no longer publishes a route or
    edge; the @WebServlet half is unaffected (a real, verb-less-by-
    design servlet mapping, a different mechanism entirely)."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "DispatcherServlet.java").write_text(
        "package p;\n"
        "\n"
        "@WebServlet(urlPatterns = {\"/api/*\"})\n"
        "public class DispatcherServlet extends HttpServlet {\n"
        "}\n",
        encoding="utf-8",
    )
    (java_repo / "src" / "main" / "java" / "p" / "OrderResource.java").write_text(
        "package p;\n"
        "\n"
        "@Path(\"/orders\")\n"
        "public class OrderResource {\n"
        "  @Path(\"/list\")\n"
        "  public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    route_targets = {
        r["target_external"] for r in dependencies_doc["edges"] if r["relation"] == "route"
    }
    assert "/api/*" in route_targets
    assert "/orders/list" not in route_targets
    entry_point_names = {e["name"] for e in features_doc["entry_points"]}
    assert "/api/*" in entry_point_names
    assert "/orders/list" not in entry_point_names
    locator_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "unsupported_entry_point_shape"
        and p.get("qualified_name") == "p.OrderResource"
    ]
    assert len(locator_problems) == 1


def test_run_scan_a_web_method_class_reports_entry_points_mapped_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, part (b) - THE
    CLASS-CLOSER): a class carrying a recognized-but-unsupported route-
    like annotation (JAX-WS's own @WebMethod) must report
    entry_points_mapped unknown, never the confident not_applicable/
    no_entry_point negative - end to end, the declared gap must also
    survive to problems.json."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "OrderEndpoint.java").write_text(
        "package p;\n"
        "\n"
        "public class OrderEndpoint {\n"
        "  @WebMethod\n"
        "  public void placeOrder() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    endpoint_unit = next(u for u in modules_doc["units"] if u["display_name"] == "OrderEndpoint")
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == endpoint_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"
    assert entry_points_mapped["reason_code"] == "unsupported_entry_point_shape"
    assert any(
        p["reason_code"] == "unsupported_entry_point_shape" for p in problems_doc["problems"])


def test_run_scan_a_scheduled_job_class_reports_entry_points_mapped_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 19 (fifteenth cold read, F3 MAJOR, wrong-data): one of
    the five newly-enrolled entry-point families (@Scheduled) must
    report entry_points_mapped unknown end to end, the same as
    @WebMethod already does - never the confident not_applicable/
    no_entry_point negative."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "JobRunner.java").write_text(
        "package p;\n"
        "\n"
        "public class JobRunner {\n"
        "  @Scheduled(fixedRate = 5000)\n"
        "  public void cleanup() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    job_unit = next(u for u in modules_doc["units"] if u["display_name"] == "JobRunner")
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == job_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"
    assert entry_points_mapped["reason_code"] == "unsupported_entry_point_shape"


def test_run_scan_a_jax_rs_verb_only_resource_reports_entry_points_mapped_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 17b (reviewer-3's rejection of round 17, THE MAJOR):
    the DOMINANT real-world JAX-RS idiom (a class-level @Path with
    verb-only methods, no method-level @Path of their own) used to
    report the confident negative not_applicable/no_entry_point, while
    @WebMethod (built the exact same round) correctly reported unknown -
    the class-closer mechanism built but applied to only one family
    member. End to end, must now agree."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "OrderResource.java").write_text(
        "package p;\n"
        "\n"
        "@Path(\"/orders\")\n"
        "public class OrderResource {\n"
        "  @GET\n"
        "  public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    resource_unit = next(u for u in modules_doc["units"] if u["display_name"] == "OrderResource")
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == resource_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"
    assert entry_points_mapped["reason_code"] == "unsupported_entry_point_shape"
    assert any(
        p["reason_code"] == "unsupported_entry_point_shape" for p in problems_doc["problems"])


def test_run_scan_jax_rs_methods_sharing_a_path_but_differing_by_verb_stay_distinct(
    java_repo: Path,
) -> None:
    """FIX ROUND 32 (twenty-eighth cold read, F2 BLOCKER, wrong-data):
    mirrors the reader's own InvoiceResource shape - two JAX-RS methods on
    the identical @Path ("/invoices/{id}") but different verbs (@GET vs
    @DELETE) used to compose the identical target string and collapse
    into ONE published entry point, silently losing that they are two
    different handlers. A third method (its own distinct @Path) must
    still publish separately as before - three methods, three entry
    points, three distinct ids."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "InvoiceResource.java").write_text(
        "package p;\n"
        "\n"
        "@Path(\"/invoices\")\n"
        "public class InvoiceResource {\n"
        "  @GET\n"
        "  @Path(\"/{id}\")\n"
        "  public void getById() {}\n"
        "\n"
        "  @DELETE\n"
        "  @Path(\"/{id}\")\n"
        "  public void deleteById() {}\n"
        "\n"
        "  @POST\n"
        "  @Path(\"/\")\n"
        "  public void create() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))

    route_entry_points = [e for e in features_doc["entry_points"] if e["kind"] == "http_route"]
    assert len(route_entry_points) == 3
    assert len({e["entry_point_id"] for e in route_entry_points}) == 3
    entry_point_names = {e["name"] for e in route_entry_points}
    assert "GET /invoices/{id}" in entry_point_names
    assert "DELETE /invoices/{id}" in entry_point_names
    assert "/invoices/{id}" not in entry_point_names  # never bare, unfolded


def test_run_scan_jax_rs_method_path_with_no_verb_marker_publishes_no_route(
    java_repo: Path,
) -> None:
    """CORRECTED (round 39, thirty-third cold read, F3 MAJOR, wrong-data
    - confident false positive): this test used to assert (named a
    "deliberate limit") that a method-level @Path with NO sibling verb
    marker anywhere in its own annotation stack still published its
    bare-path name as a real, served http_route - measured wrong. This
    IS JAX-RS's own sub-resource-locator idiom (JSR-339): it never
    handles a request directly, so no route/entry point is published
    for it now; the enrolled-but-unmodeled shape
    (jax_rs_sub_resource_locator) records a real problem instead."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "OrderResource.java").write_text(
        "package p;\n"
        "\n"
        "@Path(\"/orders\")\n"
        "public class OrderResource {\n"
        "  @Path(\"/list\")\n"
        "  public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point_names = {e["name"] for e in features_doc["entry_points"]}
    assert "/orders/list" not in entry_point_names

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    locator_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "unsupported_entry_point_shape"
        and p.get("qualified_name") == "p.OrderResource"
    ]
    assert len(locator_problems) == 1
    assert "/orders/list" in locator_problems[0]["detail"]


def test_run_scan_a_mapped_route_alongside_an_unrecognized_main_reports_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 18b (reviewer-3's priority-1 co-occurrence probe on
    round 18's F2 readiness reorder): a class with a REAL, composed
    @GetMapping route AND ALSO an unrecognizable main-like method
    (main(String) - outside the recognized array/varargs grammar) used
    to report the confident SATISFIED positive (has_entry_point=True
    won outright over the attributed cli_main_unrecognized reason) -
    the same has_entry_point-wins-first bug F2's own mixed-JAX-RS-class
    fix closed, now proven to reach a SECOND, independent reason code
    beyond the one F2 was written for. The reviewer rules the new
    answer correct: a unit with one mapped route and one unclassifiable
    construct is not fully mapped - entry_points_mapped must report
    unknown, never satisfied."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "OrderController.java").write_text(
        "package p;\n"
        "\n"
        "@RestController\n"
        "public class OrderController {\n"
        "  @GetMapping(\"/orders\")\n"
        "  public void list() {}\n"
        "\n"
        "  public static void main(String args) {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    controller_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "OrderController")
    assert "cli_main_unrecognized" in controller_unit["adapter_problem_reasons"]

    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == controller_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"
    assert entry_points_mapped["reason_code"] == "cli_main_unrecognized"

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_targets = {
        r["target_external"] for r in dependencies_doc["edges"] if r["relation"] == "route"
    }
    assert "GET /orders" in route_targets


def test_run_scan_a_mixed_jax_rs_resource_reports_entry_points_mapped_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 18 (fourteenth cold read, F2 MAJOR, wrong-data): mirrors
    the reader's own Repro B - a PURE verb-only class (round 17b) and a
    MIXED class (some routes compose, one verb-only method does not)
    must AGREE on entry_points_mapped=unknown within the same run - the
    mixed class used to publish the confident SATISFIED negative
    instead, even though its own list() route is genuinely missing."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "PureVerbOnly.java").write_text(
        "package p;\n"
        "\n"
        "@Path(\"/legacy\")\n"
        "public class PureVerbOnly {\n"
        "  @GET\n"
        "  public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (java_repo / "src" / "main" / "java" / "p" / "OrderResource.java").write_text(
        "package p;\n"
        "\n"
        "@Path(\"/orders\")\n"
        "public class OrderResource {\n"
        "  @GET\n"
        "  public void list() {}\n"
        "\n"
        "  @GET\n"
        "  @Path(\"/{id}\")\n"
        "  public void get() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    def _entry_points_mapped(display_name: str) -> dict:
        unit = next(u for u in modules_doc["units"] if u["display_name"] == display_name)
        return next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == unit["unit_id"] and s["check"] == "entry_points_mapped"
        )

    pure_signal = _entry_points_mapped("PureVerbOnly")
    mixed_signal = _entry_points_mapped("OrderResource")
    assert pure_signal["stored_status"] == "unknown"
    assert mixed_signal["stored_status"] == "unknown"
    assert pure_signal["reason_code"] == mixed_signal["reason_code"] == "unsupported_entry_point_shape"

    # the composed route must still publish - the marker mechanism never
    # suppresses a route that genuinely composed. FIX ROUND 32 (F2
    # BLOCKER): get()'s own @GET now folds into the published name (see
    # that fix) - this route was never bare "/orders/{id}" to begin with,
    # it just used to publish as if it were.
    route_targets = {
        r["target_external"] for r in dependencies_doc["edges"] if r["relation"] == "route"
    }
    assert "GET /orders/{id}" in route_targets


def test_report_carries_the_real_manifest_digest_f7(java_repo: Path) -> None:
    """FIX ROUND 12 (eighth cold read, F7): get_report passed
    manifest_digest=None to the projector unconditionally, even though a
    real digest binding scan.json's invariant 4 ("readers bind to a scan
    ID and manifest digest") requires was already available.

    FIX ROUND 17 (thirteenth cold read, CR13-5 MAJOR, wrong-data): F7
    itself wired the WRONG digest - scan.json's own `content_digest`
    (run_content_digest over this run's artifact_summaries) is
    GENERATION-INDEPENDENT by design (two separate scans of identical,
    unchanged sources legitimately produce the SAME content_digest),
    defeating the field's own contracted purpose of binding to ONE
    CONCRETE generation. `manifest_digest` must instead equal
    scan.json's own on-disk byte_sha256 (SHA-256 of its exact bytes) -
    real generation identity, never equal to content_digest for the
    typical case (their inputs are different hashes over different
    things), and covered by the companion generation-independence test
    below."""
    import hashlib

    outcome = scan_pipeline.run_scan(java_repo)
    scan_bytes = (outcome.run_dir / "scan.json").read_bytes()
    report = scan_pipeline.get_report(java_repo)
    assert report["manifest_digest"] == hashlib.sha256(scan_bytes).hexdigest()
    assert report["manifest_digest"]


def test_manifest_digest_differs_across_two_scans_while_content_digest_agrees(
    java_repo: Path,
) -> None:
    """FIX ROUND 17 (thirteenth cold read, CR13-5 MAJOR, wrong-data):
    the reader's own reproduction - two scans of IDENTICAL, unchanged
    sources must return DIFFERENT manifest_digest values (each binds to
    its own concrete generation - a fresh generated_at/run_id each time
    means scan.json's own bytes differ run over run), while scan.json's
    own content_digest (a fact about the SOURCE, not the run) legitimately
    stays the same across both - proving the old binding (content_digest)
    would have silently returned the identical manifest_digest for two
    genuinely different generations, exactly the bug this fix closes."""
    import json

    first_outcome = scan_pipeline.run_scan(java_repo)
    first_scan_doc = json.loads((first_outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    first_report = scan_pipeline.get_report(java_repo)

    second_outcome = scan_pipeline.run_scan(java_repo)
    second_scan_doc = json.loads((second_outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    second_report = scan_pipeline.get_report(java_repo)

    assert first_scan_doc["content_digest"] == second_scan_doc["content_digest"]
    assert first_report["manifest_digest"] != second_report["manifest_digest"]


def test_scan_json_publishes_start_completion_times_and_exclude_rule_digest(
    java_repo: Path,
) -> None:
    """N2 (fourth cold read, fix round 6): the design names scan.json
    fields this run never populated - "start and completion times"
    (distinct from generated_at, a single envelope-generation snapshot)
    and "the effective... exclude rules... configuration digest" (without
    it, a future change to the hardcoded exclude lists silently changes
    what whole_scope_fingerprint means, with no recorded explanation)."""
    import json

    from agenttalk.comprehension import discovery as discoverymod

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["started_at"]
    assert scan_doc["completed_at"]
    assert scan_doc["started_at"] <= scan_doc["completed_at"]
    assert scan_doc["exclude_rule_digest"] == discoverymod.effective_exclude_rule_digest()


def test_run_scan_carries_the_pom_xml_build_edge_through_the_worker(java_repo: Path) -> None:
    """B-3 (reviewer-3, PR-B delta review): pom.xml's build edge must
    reach dependencies.json via the sanitized worker's own java_results
    channel - not a direct parent-process read of the file."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    build_edges = [e for e in doc["edges"] if e["relation"] == "build"]
    assert build_edges and build_edges[0]["target_external"] == "org.springframework:spring-core"
    # Round 11c: the fixture pom has no profile-scoped dependency at all -
    # no exclusion count, no dilution of an otherwise-empty exclusions map.
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "profile_scoped_dependencies" not in scan_doc["exclusions"]


def test_run_scan_counts_profile_scoped_pom_dependencies_and_stays_complete(
    java_repo: Path,
) -> None:
    """Round 11c (reviewer-3 delta on round 11b, VEHICLE CHANGE), end to
    end: a pom's profile-scoped dependency must be visible as a named
    exclusion COUNT in scan.json's manifest - never a run-degrading
    problem. Maven profiles are common enough in real repos that the
    round-11b problem-based vehicle would have scanned a large share of
    them degraded PERMANENTLY over a DECLARED, deliberate scope
    limitation - not the same kind of thing as an unreadable
    .gitmodules or an unrecoverable route value."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies>"
        "<profiles><profile><dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>profile-dep</artifactId>"
        "</dependency></dependencies></profile></profiles>"
        "</project>",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["exclusions"]["profile_scoped_dependencies"] == 1

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert problems_doc["problems"] == []

    report = scan_pipeline.get_report(java_repo)
    assert report["exclusions"]["profile_scoped_dependencies"] == 1


def test_run_scan_reports_a_case_collision_between_two_enumerated_paths(
    java_repo: Path, monkeypatch,
) -> None:
    """N1 (third cold read, fix round 5): envelope.find_case_fold_collisions
    existed with its own passing unit tests and zero production callers -
    the same dead-code shape round 3's M9 found for parse_web_xml. Wires it
    into the scan so two enumerated paths that collide once case-folded (a
    real risk once a run crosses to/from a case-insensitive filesystem)
    actually publish the design-named case_collision problem, instead of
    silently never being checked at all. Injects the second, colliding
    path via discovery.enumerate_scope's own return value - two really
    differently-cased files cannot coexist on this dev host's own
    (case-insensitive) filesystem, so this is the only portable way to
    prove the collision without one."""
    import dataclasses
    import json

    from agenttalk.comprehension import discovery as discoverymod

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        colliding = discoverymod.EnumeratedFile(
            relative_path="src/main/java/p/APP.JAVA", byte_count=1, content_digest="deadbeef")
        return dataclasses.replace(result, files=[*result.files, colliding])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)

    outcome = scan_pipeline.run_scan(java_repo)
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    collisions = [p for p in problems_doc["problems"] if p["reason_code"] == "case_collision"]
    assert len(collisions) == 1
    assert collisions[0]["path"] == "src/main/java/p/APP.JAVA"

    # FIX ROUND 48 (forty-second cold read, F2 MAJOR, wrong-data, dead
    # code since round 36 - .cr42-nfc): case_collision is registered in
    # readiness_artifact._READINESS_CHECKS_BY_REASON_CODE as a whole-
    # file-evidence-gap reason (source_understood among them), but
    # nothing ever fed it into either colliding unit's own
    # adapter_problem_reasons before this fix - both units (the real
    # "App.java" AND the injected "APP.JAVA") must report
    # source_understood as unknown, never a confident satisfied/
    # adapter_understood computed over evidence this run cannot
    # actually trust which file it came from.
    # MICRO-ROUND 48c (F4, CI RED on all four Linux legs - the round-37
    # phantom-APP.JAVA lesson's own mirror): this assertion used to
    # require EXACTLY 4 colliding units (file + component, both paths) -
    # true on this dev host's own case-INSENSITIVE filesystem (Windows/
    # default macOS), where reading the injected, differently-cased
    # "APP.JAVA" path secretly succeeds (the OS folds case at lookup),
    # so the worker actually parses a FOURTH, phantom component unit for
    # it. On a real case-SENSITIVE filesystem (Linux, this PR's own CI),
    # that exact-case path has NO backing file at all - reading it
    # genuinely fails (`parse_failed`), so no component unit is ever
    # constructed for "APP.JAVA" at all - only 3 real units exist:
    # the real App.java's own file+component pair, and APP.JAVA's own
    # file-kind unit alone (build_modules unconditionally constructs one
    # file-kind record per `discovery.files` entry, regardless of
    # whether the worker could read it - see readiness_artifact.py's own
    # `path_excluded` analysis, MICRO-ROUND 48c, for the identical
    # "reaches a real unit either way" mechanism). Verified empirically
    # by simulating a case-sensitive read failure for "APP.JAVA" -
    # exactly 3 units result, and the injected file-kind unit's own
    # `adapter_problem_reasons` carries BOTH `case_collision` and
    # `parse_failed` together. This is a TRUE platform difference in the
    # UNIT SET itself, never a wiring gap - the wiring's own actual
    # invariant (every unit that DOES exist and touches a colliding path
    # carries the reason) holds identically on both platforms, so this
    # assertion now checks THAT invariant directly - a floor of 3 (the
    # two file-kind records plus App.java's own always-real component,
    # true on every platform), a ceiling of 4 (this dev host's own
    # case-insensitive-filesystem phantom, never more), and universal
    # membership (every unit actually touching either colliding path
    # carries the reason - never merely counted after already filtering
    # for it, the tautology the old assertion's own construction hid).
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    colliding_paths = {"src/main/java/p/App.java", "src/main/java/p/APP.JAVA"}
    colliding_units = [
        u for u in modules_doc["units"] if colliding_paths & set(u["paths"])
    ]
    assert 3 <= len(colliding_units) <= 4
    colliding_unit_ids = set()
    for unit in colliding_units:
        assert "case_collision" in unit["adapter_problem_reasons"], unit["unit_id"]
        colliding_unit_ids.add(unit["unit_id"])
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    source_understood_signals = {
        s["unit_id"]: s for s in readiness_doc["signals"] if s["check"] == "source_understood"
    }
    for unit_id in colliding_unit_ids:
        assert source_understood_signals[unit_id]["stored_status"] == "unknown"
        assert "case_collision" in source_understood_signals[unit_id]["reason_code"]


def test_run_scan_a_non_colliding_file_keeps_a_confident_source_understood_control(
    java_repo: Path,
) -> None:
    """Control for the case-fold/unicode-normalization readiness-wiring
    fix above: an ordinary run with no colliding path at all must keep
    reporting a confident source_understood for its own real class,
    proving the fix does not overreact to every unit."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    app_unit = next(u for u in modules_doc["units"] if u["display_name"] == "App")
    assert app_unit["adapter_problem_reasons"] == []
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == app_unit["unit_id"] and s["check"] == "source_understood"
    )
    assert signal["stored_status"] == "satisfied"


def test_run_scan_reports_a_unicode_normalization_collision_not_a_false_case_collision(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 36 (thirtieth cold read, F4 MAJOR, completeness, .cr30-
    uni verbatim): an NFC/NFD Unicode canonical-equivalence pair (a
    precomposed 'é' versus its decomposed combining-mark form) collides
    only once Unicode-normalized, never by a bare case-fold - publishing
    it as case_collision would assert a FALSE cause (per this round's
    own invariant). Injected the same portable way the plain case-fold
    test above does (two genuinely NFC/NFD-distinct paths are not
    reliably both writable-and-distinguishable on every real host
    filesystem - the reader's own NTFS caveat)."""
    import dataclasses
    import json
    import unicodedata

    from agenttalk.comprehension import discovery as discoverymod

    nfc_name = unicodedata.normalize("NFC", "Café.java")
    nfd_name = unicodedata.normalize("NFD", "Café.java")
    assert nfc_name != nfd_name

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        first = discoverymod.EnumeratedFile(
            relative_path=f"src/main/java/p/{nfc_name}", byte_count=1, content_digest="aaa")
        second = discoverymod.EnumeratedFile(
            relative_path=f"src/main/java/p/{nfd_name}", byte_count=1, content_digest="bbb")
        return dataclasses.replace(result, files=[*result.files, first, second])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)

    outcome = scan_pipeline.run_scan(java_repo)
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    collisions = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unicode_normalization_collision"]
    assert len(collisions) == 1
    assert collisions[0]["path"] == f"src/main/java/p/{nfd_name}"
    assert not any(p["reason_code"] == "case_collision" for p in problems_doc["problems"])
    # FIX ROUND 37 (thirty-first cold read, F6 LOW, wrong-data): this
    # template was measured well over MAX_PROBLEM_DETAIL_LENGTH (200) by
    # construction, before even adding a real path - every instance
    # published a silently mid-word-mangled fragment. Shortened; for an
    # ordinary path like this one, the detail must be whole, never
    # truncated at all (bounded_detail's own new marker covers only the
    # pathological case, not the common one).
    #
    # FIX ROUND 38 (thirty-second cold read, F5 polish, wrong-data):
    # round 37's own shortening was ITSELF measured still too long (168
    # of 200 chars fixed, leaving only ~32 for the quoted path) - see
    # test_run_scan_a_unicode_normalization_collision_with_a_realistic_
    # deep_path_is_not_truncated below for the fixture that actually
    # exercises the bound; this wording-only assertion just tracks the
    # current (shortened again) template text.
    assert not collisions[0]["detail"].endswith("...(truncated)")
    assert collisions[0]["detail"].endswith("e.g. accents)")

    # FIX ROUND 48 (F2 MAJOR, wrong-data, .cr42-nfc): the same readiness-
    # wiring fix as the plain case-fold test above - BOTH colliding
    # units (the NFC- and NFD-spelled paths, neither of which is a real
    # readable file here) must report source_understood as unknown with
    # the collision reason, never a confident answer.
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    colliding_unit_ids = {
        u["unit_id"] for u in modules_doc["units"]
        if "unicode_normalization_collision" in u["adapter_problem_reasons"]
    }
    assert len(colliding_unit_ids) == 2
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    source_understood_signals = {
        s["unit_id"]: s for s in readiness_doc["signals"] if s["check"] == "source_understood"
    }
    for unit_id in colliding_unit_ids:
        # NOTE: these two paths are also genuinely unreadable (neither
        # exists on disk with real content) - both parse_failed AND
        # unicode_normalization_collision apply, and only one reason
        # surfaces as the signal's own displayed reason_code; the
        # correctness property this fix owns is stored_status itself
        # (already the honest "unknown" either way), not which of the
        # two equally-disqualifying reasons happens to be shown.
        assert source_understood_signals[unit_id]["stored_status"] == "unknown"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "unicode_normalization_collision" in scan_doc["degraded_by"]
    assert "case_collision" not in scan_doc["degraded_by"]


def test_run_scan_a_unicode_normalization_collision_with_a_realistic_deep_path_is_not_truncated(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 38 (thirty-second cold read, F5 polish, .cr32 verbatim,
    wrong-data): round 37's own F6 fix claimed this template "fits
    comfortably under the bound for an ordinary path" - measured FALSE:
    the fixed text alone was 168 of the 200-char MAX_PROBLEM_DETAIL_
    LENGTH, leaving only ~32 chars for the quoted colliding path
    (`first!r`, quotes included) - any path over ~30 characters, which
    is every realistic Maven/Java package path, already truncated. A
    genuinely deep, realistic path (`src/main/java/com/acme/platform/
    orders/service/internal/InvoiceProcessor.java`, 76 characters) is
    used here specifically because it is exactly the kind of path
    round 37's own claim said would fit but measurably did not."""
    import dataclasses
    import json
    import unicodedata

    from agenttalk.comprehension import discovery as discoverymod

    deep_dir = "src/main/java/com/acme/platform/orders/service/internal"
    raw_name = f"{deep_dir}/InvóiceProcessor.java"
    nfc_name = unicodedata.normalize("NFC", raw_name)
    nfd_name = unicodedata.normalize("NFD", raw_name)
    assert nfc_name != nfd_name

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        first = discoverymod.EnumeratedFile(
            relative_path=nfc_name, byte_count=1, content_digest="aaa")
        second = discoverymod.EnumeratedFile(
            relative_path=nfd_name, byte_count=1, content_digest="bbb")
        return dataclasses.replace(result, files=[*result.files, first, second])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)

    outcome = scan_pipeline.run_scan(java_repo)
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    collisions = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unicode_normalization_collision"]
    assert len(collisions) == 1
    assert not collisions[0]["detail"].endswith("...(truncated)")
    assert repr(nfc_name) in collisions[0]["detail"]


def test_scan_json_publishes_boundary_path_and_kind_not_just_a_count(
    java_repo: Path, monkeypatch,
) -> None:
    """M4 (fourth cold read, fix round 6, scan.json half): scan.json's
    "boundaries" field used to be a bare integer count
    (len(discovery_result.boundaries)) - the design names "excluded roots
    with an explicit boundary reason" as a scan.json field, not a count.
    A caller reading scan.json had no way to know WHICH path was a
    boundary or WHY (reproduced with a real junction: status complete,
    problems [], boundaries: 1, the junction's own name absent from
    every published artifact). Injects a synthetic boundary via
    discovery.enumerate_scope's own return value (symlink/junction
    creation is not permitted in this sandbox)."""
    import dataclasses
    import json

    from agenttalk.comprehension import discovery as discoverymod

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        boundary = discoverymod.BoundaryEntry(
            relative_path="vendor/external-link", boundary_kind="symlink")
        return dataclasses.replace(result, boundaries=[*result.boundaries, boundary])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["boundaries"] == [{"path": "vendor/external-link", "kind": "symlink"}]
    assert scan_doc["boundaries_omitted_count"] == 0


def test_scan_json_boundaries_list_is_bounded_not_unbounded(
    java_repo: Path, monkeypatch,
) -> None:
    """Minor 7 (fifth cold read, fix round 7): every list-shaped
    scan.json/report section has been progressively capped across three
    prior rounds (M10 round 3, M-4 round 4, M2 round 6) - this list,
    added the same round as M2, was published fully unbounded, breaking
    that same discipline one list, one round later. Injects more
    synthetic boundaries than a monkeypatched cap allows and confirms
    both the cap and the omitted count actually apply."""
    import dataclasses
    import json

    from agenttalk.comprehension import discovery as discoverymod

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        extra = [
            discoverymod.BoundaryEntry(relative_path=f"vendor/link-{i}", boundary_kind="symlink")
            for i in range(3)
        ]
        return dataclasses.replace(result, boundaries=[*result.boundaries, *extra])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)
    monkeypatch.setattr(scan_pipeline, "_MAX_BOUNDARIES", 2)

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert len(scan_doc["boundaries"]) == 2
    assert scan_doc["boundaries_omitted_count"] == 1


def test_report_and_status_surface_a_reparse_point_boundary(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 22 (eighteenth cold read, F5 MAJOR, completeness): the
    reader's own .cr18-link shape - a reparse-point boundary concealing
    a real source tree was recorded in scan.json's own `boundaries` but
    NEITHER `report --json` NOR `status --json` ever surfaced it - a
    consumer of either saw complete/0 problems/exclusions that quietly
    omit the entire skipped subtree, with no way to independently judge
    whether a boundary might be hiding real, unscanned source. Injects
    a synthetic boundary via discovery.enumerate_scope's own return
    value (real junction/symlink creation is not permitted in this
    sandbox, the same established technique the scan.json-level tests
    above already use)."""
    import dataclasses

    from agenttalk.comprehension import discovery as discoverymod

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        boundary = discoverymod.BoundaryEntry(
            relative_path="vendor/external-link", boundary_kind="symlink")
        return dataclasses.replace(result, boundaries=[*result.boundaries, boundary])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)

    scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)
    status = scan_pipeline.get_status(java_repo)

    assert report["boundaries"] == [{"path": "vendor/external-link", "kind": "symlink"}]
    assert report["boundaries_omitted_count"] == 0
    assert status["boundary_count"] == 1


def test_report_and_status_show_zero_boundaries_on_a_boundary_free_repo(
    java_repo: Path,
) -> None:
    """Companion control: an ordinary repo with no reparse-point/symlink
    boundary at all shows empty/zero in both surfaces, never a stale or
    fabricated value."""
    scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)
    status = scan_pipeline.get_status(java_repo)

    assert report["boundaries"] == []
    assert report["boundaries_omitted_count"] == 0
    assert status["boundary_count"] == 0


def test_get_report_exposes_degraded_by_so_a_caller_can_tell_why(java_repo: Path) -> None:
    """M (cold-read PR-B fix round 47 completeness): scan.json's own
    degraded_by (the sorted set of reason_codes that actually set
    status="degraded") was published but exposed by NO read command - a
    report --json caller saw status="degraded" with no way to tell why
    short of separately loading problems.json and re-deriving which
    reason_codes carry degrades_run themselves. Reuses the reader's own
    duplicate-servlet-name fixture (an ordinary, real degrading shape)
    to prove the field now reaches report --json."""
    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "ServletA.java").write_text(
        "package com.acme.web;\nclass ServletA {\n}\n", encoding="utf-8")
    (web_dir / "ServletB.java").write_text(
        "package com.acme.web;\nclass ServletB {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletA</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletB</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"
    report = scan_pipeline.get_report(java_repo)
    assert report["degraded_by"] == ["duplicate_descriptor_name"]


def test_canary_sweep_no_artifact_or_report_leaks_the_absolute_root_or_a_planted_canary(
    java_repo: Path, monkeypatch,
) -> None:
    """M-3 / Note 5 (third cold read, fix round 5): the design's
    targeted-evidence list names unique canaries that must never appear
    in any published artifact, report, or pack - the mechanism that would
    have caught M-3 (an OSError's own absolute-path text leaking into
    problems.json via ``str(exc)``) before a reviewer had to find it by
    hand. Plants a canary in a Java comment (content no producer this
    slice ever copies verbatim) AND forces one file's read to fail (the
    exact M-3 shape, reproduced via discovery's own read_bytes call) so a
    problem record with a ``detail`` is actually exercised, then sweeps
    every published artifact's raw bytes plus ``report``'s own serialized
    output for either the canary or the absolute root path string."""
    import json

    canary = "CANARY_SECRET_9f21ac6b4e2d"
    (java_repo / "src" / "main" / "java" / "p" / "Marked.java").write_text(
        f"package p;\n// {canary}\nclass Marked {{}}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Unreadable.java").write_text(
        "package p;\nclass Unreadable {}\n", encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def _read_bytes(self: Path):
        if self.name == "Unreadable.java":
            # A REAL OSError from a failed OS call carries its own
            # filename (str(exc) then embeds it, e.g. "[Errno 13]
            # Permission denied: 'C:\\...\\Unreadable.java'") - a plain
            # OSError("message") does not, and would not reproduce the
            # M-3 leak mechanism at all.
            raise OSError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    outcome = scan_pipeline.run_scan(java_repo)
    # A plain, alphanumeric marker unique to this run's absolute root -
    # NOT the raw path string. On Windows, str(OSError(...))'s own
    # formatting already backslash-escapes the filename it embeds, and
    # JSON serialization escapes it AGAIN - a literal `str(root) in
    # haystack` check would never match regardless of a leak, since the
    # separators are doubled (or quadrupled) by the time either fires.
    # pytest's own tmp_path leaf name has no such special characters, so
    # it survives both escaping passes unchanged and still proves the
    # same thing: this run's own absolute, machine-local root path must
    # never appear in a published artifact.
    root_marker = java_repo.resolve().name

    artifact_names = (
        "modules.json", "dependencies.json", "features.json",
        "readiness.json", "problems.json", "scan.json",
    )
    haystacks = {
        name: (outcome.run_dir / name).read_text(encoding="utf-8") for name in artifact_names
    }
    haystacks["report --json"] = json.dumps(scan_pipeline.get_report(java_repo))

    problems_doc = json.loads(haystacks["problems.json"])
    assert any(p["reason_code"] == "parse_failed" for p in problems_doc["problems"]), (
        "sanity check: the simulated read failure must actually produce a problem "
        "with a detail, or this test proves nothing"
    )

    for name, haystack in haystacks.items():
        assert canary not in haystack, f"planted canary leaked into {name}"
        assert root_marker not in haystack, f"absolute root path leaked into {name}"


def test_run_scan_does_not_block_readiness_for_the_pom_xml_it_understood(
    java_repo: Path,
) -> None:
    """M-2 (second cold read, fix round 4): pom.xml goes THROUGH the java
    adapter package (it produced a real build edge - see the sibling test
    above) yet previously rolled up as source_understood=unsatisfied/
    no_adapter_for_language/severity=blocker - a self-contradiction. Its
    readiness signal must now be satisfied, and its unit must never be
    "blocked" purely because of that one (previously self-contradictory)
    signal."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    pom_unit = next(u for u in modules_doc["units"] if u["paths"] == ["pom.xml"])
    assert pom_unit["language"] == "xml"
    source_understood = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == pom_unit["unit_id"] and s["check"] == "source_understood"
    )
    assert source_understood["stored_status"] == "satisfied"
    summary = next(s for s in readiness_doc["summaries"] if s["unit_id"] == pom_unit["unit_id"])
    assert summary["stored_assessment_state"] != "blocked"


def test_run_scan_a_route_value_constant_never_flips_source_understood_for_ordinary_siblings(
    java_repo: Path,
) -> None:
    """FIX ROUND 13c (reviewer-3's part 2 probe, verbatim): round 13b's
    general companion fix (threading a file-wide worker problem into
    EVERY unit's adapter_problem_reason(s), even when the file has real
    declared types) exposed a regression - three ordinary classes plus
    ONE route path written as a constant (route_value_unrecoverable, a
    narrow, entry-adjacent fact, not a comprehension failure) used to
    flip source_understood to UNKNOWN on all four units (3 classes + the
    file record) - a blocker-severity check degraded for an entirely
    ordinary Java idiom. Round 13c's explicit reason-class routing must
    keep source_understood satisfied on all four, while the route
    problem itself keeps its own existing, unchanged visibility
    (problems.json, the route's own absence from dependencies/entry
    points)."""
    (java_repo / "src" / "main" / "java" / "p" / "Siblings.java").write_text(
        "package p;\n"
        "class Alpha {\n}\n"
        "class Beta {\n}\n"
        "class Gamma {\n"
        "  @GetMapping(SomeConstants.PATH)\n"
        "  void list() {}\n"
        "}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    siblings_units = [
        u for u in modules_doc["units"]
        if u["paths"] == ["src/main/java/p/Siblings.java"]
    ]
    assert len(siblings_units) == 4  # Alpha, Beta, Gamma, and the file record

    for unit in siblings_units:
        source_understood = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == unit["unit_id"] and s["check"] == "source_understood"
        )
        assert source_understood["stored_status"] == "satisfied", unit["display_name"]

    assert any(p["reason_code"] == "route_value_unrecoverable" for p in problems_doc["problems"])
    assert not any(r["relation"] == "route" for r in json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))["edges"])
    # FIX ROUND 13d (reviewer-3's LOW on round 13c) established the
    # idiom this problem family follows: an unattributed problem omits
    # the qualified_name KEY entirely, never publishes it as null.
    # FIX ROUND 35 (twenty-ninth cold read, F10 LOW, wrong-data): this
    # fixture's own route problem is NOT actually file-wide - Gamma is
    # its one, entirely knowable owning type, the same fact its
    # @WebServlet sibling already attributed - the round-13d comment's
    # premise about THIS fixture was simply wrong. Now attributed like
    # any other route_value_unrecoverable whose enclosing type is known;
    # the absent-not-null idiom itself stands unchanged for the problems
    # that are genuinely unattributable.
    route_problem = next(
        p for p in problems_doc["problems"] if p["reason_code"] == "route_value_unrecoverable")
    assert route_problem["qualified_name"] == "p.Gamma"


def test_run_scan_reports_unknown_not_satisfied_for_a_resource_capped_java_file(
    java_repo: Path, monkeypatch,
) -> None:
    """M-2 (third cold read, fix round 5): CLOSES THE CLASS - round 3
    threaded only the ``parse_failed`` worker reason into readiness;
    a .java file the worker skipped for the per-file adapter-work
    resource cap (``resource_limit``) fell through the same "no positive
    adapter evidence, but reported satisfied anyway" gap a second time
    (round 4 fixed a third instance, the no-adapter-for-language case).
    Its extension still maps to a known language, but the adapter never
    actually looked at its content - source_understood must be unknown,
    with a reason_code that names the real (resource_limit) cause, never
    a confident satisfied."""
    monkeypatch.setattr(workermod, "_MAX_ADAPTER_INPUT_BYTES", 10)
    (java_repo / "src" / "main" / "java" / "p" / "Huge.java").write_text(
        "package p;\nclass Huge {\n  void run() { Foo.bar(); }\n}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    huge_unit = next(u for u in modules_doc["units"] if u["paths"] == ["src/main/java/p/Huge.java"])
    assert huge_unit["language"] == "java"
    assert huge_unit["adapter_problem_reason"] == "resource_limit"
    source_understood = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == huge_unit["unit_id"] and s["check"] == "source_understood"
    )
    assert source_understood["stored_status"] == "unknown"
    assert source_understood["reason_code"] == "adapter_resource_limit"


def test_run_scan_never_publishes_an_import_of_a_resource_capped_file_as_external(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 12 (eighth cold read, F2 MAJOR): reproduced shape - a
    generated file this SAME run degraded away for the per-file adapter
    resource cap (the 9MB BigTable-over-the-8MiB-cap shape the reviewer
    named) used to have its declared type published as a confident
    EXTERNAL dependency the moment an importer referenced it - the
    registry has no entry for it BECAUSE it degraded, not because it is
    genuinely third-party. The importer must resolve unresolved, and
    dependencies_resolved must NOT report satisfied over a dependency
    this run never actually verified."""
    # Only BigTable.java (padded via a comment, stripped before parsing but
    # counted toward the RAW byte cap check) exceeds the cap - Consumer.java
    # stays a small, ordinarily-parsed file, exactly like the reviewer's
    # real shape (an oversized GENERATED file, not every file in the repo).
    monkeypatch.setattr(workermod, "_MAX_ADAPTER_INPUT_BYTES", 100)
    (java_repo / "src" / "main" / "java" / "p" / "BigTable.java").write_text(
        "package p;\nclass BigTable {\n  // " + ("x" * 200) + "\n}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport p.BigTable;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r["evidence_class"] == "extracted")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge["target_external"] is None
    assert import_edge["target_unresolved"] == "p.BigTable"

    consumer_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Consumer")
    dependencies_resolved = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == consumer_unit["unit_id"] and s["check"] == "dependencies_resolved"
    )
    assert dependencies_resolved["stored_status"] == "unsatisfied"


def test_run_scan_ordinary_jdk_invoke_calls_never_drive_dependencies_resolved_unsatisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 12 (F2/F5 folded in) + FIX ROUND 14 (CR10-4): an
    ordinary class calling a well-known java.lang method with no import
    evidence (Math.max) now resolves that invoke edge as EXTERNAL
    (java.lang.Math - round 14's known-external recognition, closing
    the noise at its source, not just at the readiness layer) - and
    must never drive dependencies_resolved to unsatisfied either way,
    both because it resolves cleanly AND because dependencies_resolved
    stays scoped to import/inherit/build relations per the design's own
    "direct internal dependencies" wording."""
    (java_repo / "src" / "main" / "java" / "p" / "PricingService.java").write_text(
        "package p;\nclass PricingService {\n"
        "  int cap(int a, int b) { return Math.max(a, b); }\n"
        "}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    invoke_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "invoke" and r["target_external"] == "java.lang.Math")
    assert invoke_edge["resolution_state"] == "resolved"

    pricing_unit = next(u for u in modules_doc["units"] if u["display_name"] == "PricingService")
    dependencies_resolved = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == pricing_unit["unit_id"] and s["check"] == "dependencies_resolved"
    )
    assert dependencies_resolved["stored_status"] == "satisfied"
    assert dependencies_resolved["reason_code"] == "no_modeled_dependencies"


def test_run_scan_a_custom_exception_extending_runtimeexception_reports_dependencies_resolved_satisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 14 (tenth cold read, CR10-4 MAJOR): round 12 scoped
    dependencies_resolved away from invoke noise but left inherit, which
    has the identical property - java.lang needs no import, so every
    custom exception (extends RuntimeException) published a confident
    unsatisfied/unresolved_dependency at warning severity on entirely
    healthy code. Must resolve RuntimeException as java.lang-known-
    external and report satisfied, end to end."""
    (java_repo / "src" / "main" / "java" / "p" / "OrderNotFoundException.java").write_text(
        "package p;\nclass OrderNotFoundException extends RuntimeException {\n"
        "  OrderNotFoundException(String id) { super(id); }\n"
        "}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    inherit_edge = next(r for r in dependencies_doc["edges"] if r["relation"] == "inherit")
    assert inherit_edge["resolution_state"] == "resolved"
    assert inherit_edge["target_external"] == "java.lang.RuntimeException"

    exc_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "OrderNotFoundException")
    dependencies_resolved = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == exc_unit["unit_id"] and s["check"] == "dependencies_resolved"
    )
    assert dependencies_resolved["stored_status"] == "satisfied"


def test_run_scan_a_servlet_subclass_via_import_reports_dependencies_resolved_satisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 15 (eleventh cold read, M8 MAJOR, wrong-data, promoted
    from polish - same class as CR10-4): a servlet subclass whose
    superclass is named through an ordinary import (not a java.lang
    default) used to publish a confident dependencies_resolved
    UNSATISFIED - a published deficiency on entirely healthy code, end
    to end, exactly what CR10-4 already fixed for the java.lang case."""
    (java_repo / "src" / "main" / "java" / "p" / "MyServlet.java").write_text(
        "package p;\n"
        "import javax.servlet.http.HttpServlet;\n"
        "class MyServlet extends HttpServlet {\n"
        "}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    inherit_edge = next(r for r in dependencies_doc["edges"] if r["relation"] == "inherit")
    assert inherit_edge["resolution_state"] == "resolved"
    assert inherit_edge["target_external"] == "javax.servlet.http.HttpServlet"

    servlet_unit = next(u for u in modules_doc["units"] if u["display_name"] == "MyServlet")
    dependencies_resolved = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == servlet_unit["unit_id"] and s["check"] == "dependencies_resolved"
    )
    assert dependencies_resolved["stored_status"] == "satisfied"


def test_run_scan_a_real_junit_test_calling_the_target_reports_test_evidence_located_satisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 15b (reviewer-3's F4 leg 3, MAJOR - closing an
    unreachable branch round 15 itself introduced): round 15's own
    requirement of extracted/declared evidence on the TEST relation made
    "satisfied" unreachable on any real run - the reviewer's own
    reproduced shape: a real JUnit BillingEngineTest imports @Test and
    calls BillingEngine.charge(), an extracted, resolved invoke edge -
    and readiness still said no_test_evidence_found, a false statement
    over evidence this same run extracted. End to end, no synthetic
    edge construction."""
    (java_repo / "src" / "main" / "java" / "p" / "BillingEngine.java").write_text(
        "package p;\nclass BillingEngine {\n  static void charge() {}\n}\n", encoding="utf-8")
    test_dir = java_repo / "src" / "test" / "java" / "p"
    test_dir.mkdir(parents=True)
    (test_dir / "BillingEngineTest.java").write_text(
        "package p;\n"
        "import org.junit.Test;\n"
        "class BillingEngineTest {\n"
        "  @Test void chargeWorks() { BillingEngine.charge(); }\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    invoke_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "invoke" and r.get("evidence_class") == "extracted"
        and r["resolution_state"] == "resolved"
    )
    assert invoke_edge["target_unit_id"] is not None

    billing_engine_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "BillingEngine")
    test_evidence = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == billing_engine_unit["unit_id"] and s["check"] == "test_evidence_located"
    )
    assert test_evidence["stored_status"] == "satisfied"


def test_run_scan_an_import_only_test_reference_reports_test_evidence_located_satisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 15c (reviewer-3's approval-conditioned MINOR): the
    import arm of F4 leg 3's fix (an extracted IMPORT edge, not just
    invoke, from a test-classified unit to a production unit counting as
    test evidence) was untested and load-bearing - mutation E (narrowing
    the relation tuple from (invoke, import) to (invoke,) alone) left
    every then-existing test green. The reviewer's own measurement: the
    MOST COMMON Java test shape - import + construct + instance call -
    produces NO invoke edge at all (a constructor call is a declared,
    named coverage gap; an instance call on a local variable is not a
    TYPE-qualified call the adapter recognizes) - so the import edge is
    the ONLY extracted evidence available; drop it and the majority
    real-world shape reopens this round's own defect.

    OrderTest imports p.model.Order, constructs one, and calls an
    instance method on it - asserted FIRST that no invoke edge from the
    test to Order exists at all, so the satisfied verdict below can only
    be coming from the import arm, never invoke."""
    (java_repo / "src" / "main" / "java" / "p" / "model" / "Order.java").parent.mkdir(
        parents=True, exist_ok=True)
    (java_repo / "src" / "main" / "java" / "p" / "model" / "Order.java").write_text(
        "package p.model;\nclass Order {\n  void confirm() {}\n}\n", encoding="utf-8")
    test_dir = java_repo / "src" / "test" / "java" / "p"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "OrderTest.java").write_text(
        "package p;\n"
        "import p.model.Order;\n"
        "import org.junit.Test;\n"
        "class OrderTest {\n"
        "  @Test void confirmWorks() {\n"
        "    Order order = new Order();\n"
        "    order.confirm();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    order_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Order")

    # Structure note (reviewer's own ask): prove the import arm ALONE
    # carries this, not invoke - no invoke edge targets Order at all.
    assert not any(
        r["relation"] == "invoke" and r.get("target_unit_id") == order_unit["unit_id"]
        for r in dependencies_doc["edges"]
    )

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("evidence_class") == "extracted"
        and r["resolution_state"] == "resolved" and r["target_unit_id"] == order_unit["unit_id"]
    )
    assert import_edge["target_unit_id"] == order_unit["unit_id"]

    test_evidence = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == order_unit["unit_id"] and s["check"] == "test_evidence_located"
    )
    assert test_evidence["stored_status"] == "satisfied"


def test_a_mixed_files_own_import_counts_as_test_evidence_for_its_target(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 48c (F3, the argued analysis, measured): readiness_
    artifact.py's own `test_unit_ids` set-comprehension uses BARE
    membership (`"test" in classification`), unchanged since before
    round 44's own classification-union fix - argued, in the analysis
    beside that line, to be the CORRECT answer for its own question
    ("does an edge FROM this unit count as coming from a test-
    classified source?"), unlike `_check_test_evidence_located`'s own
    exemption check (a DIFFERENT question, correctly narrowed to
    exclusive membership by round 48's own F3).

    Colocates a plain production class (`Helper`) and a test-suffix-
    named, test-framework-evidenced class (`HelperTest`) in the SAME
    physical file, outside any recognized test source root - the
    file's own classification is genuinely mixed, `["production",
    "test"]`. That file's own `import p.Target;` (Java attributes
    imports at FILE scope, never to one declared type inside it -
    adapters.java's own `file_scope_qualified`) still resolves as real,
    extracted test evidence for `Target` - the intended behavior the
    analysis concludes bare membership must keep producing, not a
    residual bug to fix."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Target.java").write_text(
        "package p;\nclass Target {\n  void run() {}\n}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Mixed.java").write_text(
        "package p;\n"
        "import p.Target;\n"
        "import org.junit.Test;\n"
        "class Helper {\n"
        "}\n"
        "class HelperTest {\n"
        "  @Test void checks() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    mixed_file_unit = next(
        u for u in modules_doc["units"]
        if u["kind"] == "file" and any(p.endswith("p/Mixed.java") for p in u["paths"])
    )
    assert sorted(mixed_file_unit["classification"]) == ["production", "test"]

    target_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Target")

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("evidence_class") == "extracted"
        and r["resolution_state"] == "resolved" and r["target_unit_id"] == target_unit["unit_id"]
    )
    assert import_edge["from_unit_id"] == mixed_file_unit["unit_id"]

    test_evidence = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == target_unit["unit_id"] and s["check"] == "test_evidence_located"
    )
    assert test_evidence["stored_status"] == "satisfied"


def test_run_scan_a_same_package_no_import_test_reference_stays_unknown_never_satisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 16b (reviewer-3's rejection of round 16, M1 JUDGE -
    ruling KEEP option (b), reject (a)): the same-package test
    convention (a test class needs no import to reference a subject in
    its OWN package - Java's own rule) leaves ONLY the name-derived
    ``test`` pairing edge (``evidence_class="inferred"``) when the test
    body's only reference is a constructor call (F5/CR10-3's own
    declared, named coverage gap - no invoke edge). Ruled explicitly:
    same-package LOCALITY does nothing to make the name-derived GUESS
    any more true (the reviewer's own cr11-fx4 IntegrationTests/
    Integration collision lives in one package by construction, and
    upgrading same-package guesses to count would make THAT exact false
    positive satisfied again) - the subject must report unknown/
    no_test_evidence_found, NEVER a satisfied earned only by the
    inferred edge. Third named limit in a row that used to live only in
    prose (round 12b's wildcard limit, round 15c's own three declared
    reads) - pinned here as a real test, not just a PR-description
    claim."""
    (java_repo / "src" / "main" / "java" / "p" / "Widget.java").write_text(
        "package p;\nclass Widget {\n  void spin() {}\n}\n", encoding="utf-8")
    test_dir = java_repo / "src" / "test" / "java" / "p"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "WidgetTest.java").write_text(
        "package p;\n"
        "import org.junit.Test;\n"
        "class WidgetTest {\n"
        "  @Test void spinWorks() {\n"
        "    Widget widget = new Widget();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    widget_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Widget")

    # No import edge at all (same package, none needed) and no invoke
    # edge (a bare constructor call) targets Widget - only the inferred
    # test-pairing edge references it.
    assert not any(
        r["relation"] == "import" and r.get("target_unit_id") == widget_unit["unit_id"]
        for r in dependencies_doc["edges"]
    )
    assert not any(
        r["relation"] == "invoke" and r.get("target_unit_id") == widget_unit["unit_id"]
        for r in dependencies_doc["edges"]
    )
    test_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "test" and r.get("target_unit_id") == widget_unit["unit_id"]
    )
    assert test_edge["evidence_class"] == "inferred"

    test_evidence = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == widget_unit["unit_id"] and s["check"] == "test_evidence_located"
    )
    assert test_evidence["stored_status"] == "unknown"
    # FIX ROUND 17 (thirteenth cold read, CR13-7 MINOR): the inferred
    # test-pairing edge above IS something this run found - the more
    # precise "insufficient_test_evidence" reason names that a pairing
    # was located but is not enough alone, distinct from
    # "no_test_evidence_found" (nothing at all). The (b)-pin itself is
    # about the STATUS staying unknown, unaffected by this wording split.
    assert test_evidence["reason_code"] == "insufficient_test_evidence"


def test_run_scan_unrecognized_main_like_shape_reports_entry_points_mapped_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 13b/13c (reviewer-3's B1 class-closer, attribution, and
    routing): a method literally named main, returning void, with a
    parameter shape genuinely outside the recognized grammar (String-
    typed but not any recognized array/varargs form - round 13c's own
    JLS-certain-negative classification does not apply here, since the
    base type IS String) must never publish a confident "no entry
    point" - end to end, the adapter's cli_main_unrecognized problem
    (attributed to this ONE unit) must surface as readiness's
    entry_points_mapped UNKNOWN, with problems.json naming the exact
    reason, WITHOUT flipping source_understood (round 13c's explicit
    reason-class routing) on an otherwise real scan run."""
    (java_repo / "src" / "main" / "java" / "p" / "App.java").write_text(
        "package p;\nclass App {\n"
        "  public static void main(String args) {\n"
        "  }\n"
        "}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    app_unit = next(u for u in modules_doc["units"] if u["display_name"] == "App")
    assert "cli_main_unrecognized" in app_unit["adapter_problem_reasons"]

    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == app_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"
    assert entry_points_mapped["reason_code"] == "cli_main_unrecognized"
    cli_main_problem = next(
        p for p in problems_doc["problems"] if p["reason_code"] == "cli_main_unrecognized")
    # FIX ROUND 13d (reviewer-3's LOW on round 13c): qualified_name was
    # internal-only - readiness named the unit while problems.json, the
    # ONE surface an operator actually reads, could only say "somewhere
    # in this file". Published on the problem record so a reader can
    # join the two.
    assert cli_main_problem["qualified_name"] == "p.App"

    source_understood = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == app_unit["unit_id"] and s["check"] == "source_understood"
    )
    assert source_understood["stored_status"] == "satisfied"


def test_run_scan_a_path_constants_class_reports_entry_points_and_feature_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, M3 MAJOR, wrong-data): mirrors
    the reader's own .cr16-e shape - a path-constants class
    (@RequestMapping(ApiPaths.ORDERS), a common idiom) used to publish
    entry_points_mapped not_applicable/no_entry_point AND feature_linked
    unsatisfied as CONFIDENT NEGATIVES, while the run itself recorded
    (via route_value_unrecoverable) that it could not read the route at
    all. Both signals must now report unknown, and problems.json must
    name BOTH the class-level annotation's own unrecoverable value AND
    the method-level fail-safe it triggers."""
    (java_repo / "src" / "main" / "java" / "p" / "Controller.java").write_text(
        "package p;\n"
        "\n"
        "@RequestMapping(ApiPaths.ORDERS)\n"
        "public class Controller {\n"
        "  @GetMapping(\"/list\")\n"
        "  public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    controller_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "Controller")
    assert "route_value_unrecoverable" in controller_unit["adapter_problem_reasons"]

    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == controller_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    feature_linked = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == controller_unit["unit_id"] and s["check"] == "feature_linked"
    )
    assert entry_points_mapped["stored_status"] == "unknown"
    assert entry_points_mapped["reason_code"] == "route_value_unrecoverable"
    assert feature_linked["stored_status"] == "unknown"
    assert feature_linked["reason_code"] == "route_value_unrecoverable"

    route_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "route_value_unrecoverable"]
    assert len(route_problems) == 2


def test_run_scan_a_class_with_no_route_at_all_stays_the_honest_negative(
    java_repo: Path,
) -> None:
    """Companion negative case - a plain class with no route annotation
    at all must keep its confident not_applicable/unsatisfied negatives,
    unaffected by the M3 routing change."""
    (java_repo / "src" / "main" / "java" / "p" / "PlainService.java").write_text(
        "package p;\n"
        "\n"
        "public class PlainService {\n"
        "  public void doWork() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    import json

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    plain_unit = next(u for u in modules_doc["units"] if u["display_name"] == "PlainService")
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == plain_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    feature_linked = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == plain_unit["unit_id"] and s["check"] == "feature_linked"
    )
    assert entry_points_mapped["stored_status"] == "not_applicable"
    assert feature_linked["stored_status"] == "unsatisfied"


def test_run_scan_populates_source_digest_on_dependency_and_feature_producers(
    java_repo: Path,
) -> None:
    """M7 (cold-read, PR-B fix round 3): end to end, not just at the
    builder-unit level - every producer in dependencies.json and
    features.json must carry a real, non-null source_digest."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    assert dependencies_doc["edges"]
    for edge in dependencies_doc["edges"]:
        for producer in edge["producers"]:
            assert producer["source_digest"] is not None
    assert features_doc["entry_points"]
    for entry_point in features_doc["entry_points"]:
        for producer in entry_point["producers"]:
            assert producer["source_digest"] is not None
    for feature in features_doc["features"]:
        for producer in feature["producers"]:
            assert producer["source_digest"] is not None


def test_run_scan_carries_a_web_xml_servlet_route_through_the_worker(
    java_repo: Path,
) -> None:
    """M9 (cold-read, PR-B fix round 3): parse_web_xml existed with its
    own passing unit tests but no dispatch anywhere in the pipeline. Prove
    it end to end: a servlet-mapping route in web.xml must reach
    features.json's entry_points."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )
    outcome = scan_pipeline.run_scan(java_repo)
    doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    assert any(e["name"] == "/api/*" and e["kind"] == "http_route" for e in doc["entry_points"])


def test_run_scan_a_listener_naming_an_out_of_scan_class_marks_the_file_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 37 (thirty-first cold read, F3 MAJOR, wrong-data,
    .cr31-listenonly verbatim): a web.xml <listener> naming a class NOT
    in this scan at all (a jar-shipped listener, e.g. Spring's own
    ContextLoaderListener - the NORMAL real-world shape) used to reach
    NOTHING: not the (never-matching) same-file map, not the cross-file
    qualified-name map (zero in-scan claimants), so web.xml's own FILE
    unit published the confident not_applicable/no_entry_point negative,
    directly contradicting this SAME run's own problems.json record
    ("not confidently absent either"). The declaring file's own unit
    (modules.json already publishes one for web.xml) must report
    unknown/unsupported_entry_point_shape instead."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <listener>\n"
        "    <listener-class>org.springframework.web.context.ContextLoaderListener"
        "</listener-class>\n"
        "  </listener>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="web.xml", kind="file",
        check="entry_points_mapped")
    assert file_signal["stored_status"] == "unknown"
    assert file_signal["reason_code"] == "unsupported_entry_point_shape"


def test_run_scan_a_listener_naming_an_in_scan_class_marks_both_component_and_file_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 37 (F3 MAJOR, .cr31-listenerclass verbatim): when the
    <listener>'s own class IS in scan (one claimant), the EXISTING
    round-21c cross-file attribution correctly marks that class's own
    component unit unknown - but web.xml's own declaring-file unit must
    ALSO be unknown: "this file names an unmodeled entry-point
    mechanism" is a fact about web.xml itself, never contingent on
    whether the referenced class happens to also be in scope."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "MyListener.java").write_text(
        "package p;\nclass MyListener {}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <listener>\n"
        "    <listener-class>p.MyListener</listener-class>\n"
        "  </listener>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    component_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="MyListener", kind="component",
        check="entry_points_mapped")
    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="web.xml", kind="file",
        check="entry_points_mapped")
    assert component_signal["stored_status"] == "unknown"
    assert file_signal["stored_status"] == "unknown"
    assert file_signal["reason_code"] == "unsupported_entry_point_shape"


def test_run_scan_three_unmodeled_shapes_never_let_a_real_route_win_satisfied(
    java_repo: Path,
) -> None:
    """FIX ROUND 37 (F3 MAJOR, .cr31-shapes verbatim): a web.xml carrying
    THREE unmodeled entry-point shapes (a <listener> naming an out-of-
    scan class, a servlet-name-scoped filter, and a startup-only
    servlet) ALONGSIDE a genuine, real, mapped <servlet-mapping> route -
    the file's own entry_points_mapped must stay unknown, never the
    confident satisfied a real route elsewhere in the same file used to
    win outright (an attributed reason always wins over a bare
    has_entry_point=True - see _check_entry_points_mapped's own
    ordering)."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <listener>\n"
        "    <listener-class>org.springframework.web.context.ContextLoaderListener"
        "</listener-class>\n"
        "  </listener>\n"
        "  <filter>\n"
        "    <filter-name>auditFilter</filter-name>\n"
        "    <filter-class>p.AuditFilter</filter-class>\n"
        "  </filter>\n"
        "  <filter-mapping>\n"
        "    <filter-name>auditFilter</filter-name>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "  </filter-mapping>\n"
        "  <servlet>\n"
        "    <servlet-name>startupOnly</servlet-name>\n"
        "    <servlet-class>p.StartupOnlyServlet</servlet-class>\n"
        "    <load-on-startup>1</load-on-startup>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>p.DispatcherServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))

    assert any(
        e["name"] == "/api/*" and e["kind"] == "http_route" for e in features_doc["entry_points"])

    file_signal = _readiness_signal(
        readiness_doc, modules_doc, display_name="web.xml", kind="file",
        check="entry_points_mapped")
    assert file_signal["stored_status"] == "unknown"
    assert file_signal["reason_code"] == "unsupported_entry_point_shape"


def test_run_scan_links_a_web_xml_route_to_its_real_servlet_class_end_to_end(
    java_repo: Path,
) -> None:
    """FIX ROUND 17 (thirteenth cold read, CR13-2 MAJOR, wrong-data):
    end to end, no synthetic edge construction - a web.xml <servlet>/
    <servlet-mapping> pair naming a REAL in-scan class must make that
    class's own entry_points_mapped satisfied (never the confident
    not_applicable/no_entry_point negative it used to get, while the
    route it serves was silently owned by the web.xml FILE unit
    instead)."""
    import json

    (java_repo / "src" / "main" / "java" / "com" / "acme" / "web").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "web" / "DispatcherServlet.java").write_text(
        "package com.acme.web;\nclass DispatcherServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "DispatcherServlet")
    entry_point = next(
        e for e in features_doc["entry_points"] if e["name"] == "/api/*")
    assert entry_point["owning_unit_id"] == servlet_unit["unit_id"]

    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == servlet_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "satisfied"


def test_run_scan_a_web_xml_servlet_class_binary_spelling_attributes_to_the_class_end_to_end(
    java_repo: Path,
) -> None:
    """FIX ROUND 46 (fortieth cold read, F2 MAJOR, wrong-data - THE
    DESCRIPTOR GATE IS BLIND TO THE BINARY SPELLING, .cr40-desc): end to
    end - a real container requires the JVM's own binary class name
    (`com.acme.web.Dispatcher$Inner`) in <servlet-class> for a nested
    class, never the source-dotted spelling (`com.acme.web.Dispatcher.
    Inner`) this adapter's own qualified_name always publishes. Before
    this fix, an exact string match never fired for this shape at all -
    the route fell back to the web.xml FILE unit, and the resolved
    class's own entry_points_mapped stayed a confident negative even
    though this run's own features.json already named it as the
    served route's owner (had it resolved). Now resolves and attributes
    to the CLASS's own unit - the same satisfied signal
    test_run_scan_links_a_web_xml_route_to_its_real_servlet_class_end_
    to_end asserts for the dot-spelled case, proven here for the
    binary-spelled one too."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "Dispatcher.java").write_text(
        "package com.acme.web;\n"
        "class Dispatcher {\n"
        "  static class Inner {\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.Dispatcher$Inner</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/dollar-api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    inner_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Inner")
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/dollar-api/*")
    assert entry_point["owning_unit_id"] == inner_unit["unit_id"]

    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == inner_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "satisfied"


def test_run_scan_a_web_xml_servlet_class_naming_an_abstract_class_degrades_end_to_end(
    java_repo: Path,
) -> None:
    """FIX ROUND 45 (thirty-ninth cold read, F2 MAJOR - THE MATRIX'S OWN
    MISSING COLUMN, .cr39-descabs): end to end - a web.xml <servlet-
    class> naming a real, in-scan ABSTRACT class must degrade the run,
    publish an unsupported_entry_point_shape/descriptor_route_on_
    uninstantiable_class problem, suppress the route entirely (no
    feature, no entry point), and report the resolved class's own
    entry_points_mapped as unknown - never the confident satisfied
    test_run_scan_links_a_web_xml_route_to_its_real_servlet_class_end_
    to_end asserts for a concrete class, and never a silently-published
    served route for a class a container can never instantiate.

    FIX ROUND 47 (forty-first cold read, B3 MAJOR, wrong-data - THE
    MANDATED STRUCTURAL GUARD): this test used to check FOUR artifacts
    (scan.json, problems.json, features.json, modules.json/readiness.json)
    and skip dependencies.json entirely - exactly the leak B3 measured:
    dependencies_artifact.build_dependencies's own route-edge builder
    never consulted registrability at all, publishing a real route edge
    for the IDENTICAL uninstantiable class features.json simultaneously
    suppressed as an entry point (one report self-contradicting:
    entry_points: 0 alongside dependency_summary.routes: 1). Now asserts
    ALL FIVE artifacts agree for this one suppressed route."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "AbstractServlet.java").write_text(
        "package com.acme.web;\npublic abstract class AbstractServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>abs</servlet-name>\n"
        "    <servlet-class>com.acme.web.AbstractServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>abs</servlet-name>\n"
        "    <url-pattern>/abs/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["status"] == "degraded"
    assert "unsupported_entry_point_shape" in scan_doc["degraded_by"]

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    descriptor_problem = next(
        p for p in problems_doc["problems"]
        if p["qualified_name"] == "com.acme.web.AbstractServlet")
    assert descriptor_problem["reason_code"] == "unsupported_entry_point_shape"
    assert "descriptor_route_on_uninstantiable_class" in descriptor_problem["detail"]
    assert descriptor_problem["path"] == "WEB-INF/web.xml"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    assert not any(e["name"] == "/abs/*" for e in features_doc["entry_points"])
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "AbstractServlet")
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == servlet_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"

    # FIX ROUND 47 (B3 MAJOR - THE MANDATED STRUCTURAL GUARD): the fifth
    # artifact this test used to skip entirely - dependencies.json must
    # never publish a route edge for the identical uninstantiable class.
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    assert not any(e["route_kind"] is not None for e in dependencies_doc["edges"])


def test_run_scan_a_web_xml_servlet_class_with_a_src_test_duplicate_degrades_end_to_end(
    java_repo: Path,
) -> None:
    """FIX ROUND 47 (forty-first cold read, B2 BLOCKER, wrong-data -
    THE DESCRIPTOR FAMILY, .cr41-duptest - the realistic trigger): a
    src/test/java copy of the SAME fully-qualified abstract class
    (a real, common shape - a test-scoped stub sharing the production
    class's own name) used to make by_qualified_name empty itself for
    this name (a genuine duplicate-qualified-name collision), so
    round 45/46's own downstream registrability check silently SKIPPED
    (`resolved_unit_id is None`), publishing a confident served route
    for a class BOTH declarations agree is uninstantiable, on a run
    with zero problems recorded for it. The upstream verdict now
    decides this honestly regardless of duplicate status - end to end,
    across all five artifacts."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "AbstractServlet.java").write_text(
        "package com.acme.web;\npublic abstract class AbstractServlet {\n}\n", encoding="utf-8")
    test_dir = java_repo / "src" / "test" / "java" / "com" / "acme" / "web"
    test_dir.mkdir(parents=True)
    (test_dir / "AbstractServlet.java").write_text(
        "package com.acme.web;\npublic abstract class AbstractServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>abs</servlet-name>\n"
        "    <servlet-class>com.acme.web.AbstractServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>abs</servlet-name>\n"
        "    <url-pattern>/dup-abs/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["status"] == "degraded"
    assert "unsupported_entry_point_shape" in scan_doc["degraded_by"]

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    # NOTE: this run's own pre-existing duplicate-qualified-name conflict
    # machinery ALSO records its own, separate problem for this same
    # qualified_name (a genuine, different fact - two units declaring
    # the identical FQN) - filtered to this test's own reason_code
    # specifically, never picking whichever problem happens to be first.
    descriptor_problem = next(
        p for p in problems_doc["problems"]
        if p["qualified_name"] == "com.acme.web.AbstractServlet"
        and p["reason_code"] == "unsupported_entry_point_shape")
    assert "descriptor_route_on_uninstantiable_class" in descriptor_problem["detail"]
    assert "2 duplicate declarations" in descriptor_problem["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    assert not any(e["name"] == "/dup-abs/*" for e in features_doc["entry_points"])

    # MICRO-ROUND 47b (F2, the PARTIAL half of 0a): this docstring already
    # claimed "across all five artifacts" but only ever asserted on
    # problems.json/features.json (plus scan.json's own summary) -
    # modules.json and readiness.json (the one most able to diverge
    # independently: entry_points_mapped=unknown has its own mechanism,
    # never guaranteed by the descriptor-suppression checks above alone)
    # were both missing. Added so the claim is actually true.
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "AbstractServlet")
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == servlet_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_a_mixed_duplicate_descriptor_target_publishes_its_own_descriptor_problem(
    java_repo: Path, swap_order: bool,
) -> None:
    """MICRO-ROUND 47b (F1, the OVERTURNED half of 0b, .cr41-solshapes-
    mixed end-to-end): a duplicate FQN where one declaration is concrete/
    instantiable and the other is abstract - compute_descriptor_
    registrability_verdicts's own MIXED branch (dependencies_artifact.py)
    already builds a detail stating the duplicate declarations DISAGREE
    on instantiability so no confident owner exists, and features_
    artifact.build_features's own suppression loop already turns ANY
    suppressed verdict (single, all-duplicate-uninstantiable, OR mixed)
    into a descriptor_registrability_problem unconditionally - the SAME
    mechanism, the SAME reason_code (unsupported_entry_point_shape), one
    branch, already shared with the all-uninstantiable cell right beside
    it. This end-to-end truth-table entry proves it reaches problems.json
    for BOTH declaration orders, not just the isolated verdict text."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    test_dir = java_repo / "src" / "test" / "java" / "com" / "acme" / "web"
    test_dir.mkdir(parents=True)
    concrete_src = "package com.acme.web;\npublic class Mixed {\n}\n"
    abstract_src = "package com.acme.web;\npublic abstract class Mixed {\n}\n"
    if swap_order:
        (web_dir / "Mixed.java").write_text(abstract_src, encoding="utf-8")
        (test_dir / "Mixed.java").write_text(concrete_src, encoding="utf-8")
    else:
        (web_dir / "Mixed.java").write_text(concrete_src, encoding="utf-8")
        (test_dir / "Mixed.java").write_text(abstract_src, encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>mixed</servlet-name>\n"
        "    <servlet-class>com.acme.web.Mixed</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>mixed</servlet-name>\n"
        "    <url-pattern>/mixed/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["status"] == "degraded"
    assert "unsupported_entry_point_shape" in scan_doc["degraded_by"]

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    descriptor_problem = next(
        p for p in problems_doc["problems"]
        if p["qualified_name"] == "com.acme.web.Mixed"
        and p["reason_code"] == "unsupported_entry_point_shape")
    assert "descriptor_route_on_uninstantiable_class" in descriptor_problem["detail"]
    assert "disagree on instantiability" in descriptor_problem["detail"]
    assert "no confident owner exists" in descriptor_problem["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    assert not any(e["name"] == "/mixed/*" for e in features_doc["entry_points"])
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    assert not any(e["route_kind"] is not None for e in dependencies_doc["edges"])


def test_run_scan_an_annotation_suppressed_route_agrees_across_all_five_artifacts(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 47b (F2, the annotation-family twin the mandate
    asked for): the web.xml descriptor family's own all-five-artifacts
    agreement lock (test_run_scan_a_web_xml_servlet_class_naming_an_
    abstract_class_degrades_end_to_end, above) has no ANNOTATION-family
    twin - @WebServlet/@WebFilter/Spring/JAX-RS all self-suppress at the
    java.py adapter level (before result.entry_points is ever built,
    never routed through the shared descriptor-registrability verdict
    at all), so the agreement claim ("every artifact agrees this route
    was never served") deserves its own independent end-to-end lock,
    never assumed to hold just because the descriptor family's own lock
    does."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "BaseServlet.java").write_text(
        "package com.acme.web;\n\n"
        "@WebServlet(\"/api\")\n"
        "public abstract class BaseServlet extends HttpServlet {\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["status"] == "degraded"
    assert "unsupported_entry_point_shape" in scan_doc["degraded_by"]

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    descriptor_problem = next(
        p for p in problems_doc["problems"]
        if p["qualified_name"] == "com.acme.web.BaseServlet")
    assert descriptor_problem["reason_code"] == "unsupported_entry_point_shape"
    assert "webservlet_on_uninstantiable_class" in descriptor_problem["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    assert not any(e["name"] == "/api" for e in features_doc["entry_points"])
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    assert not any(e["route_kind"] is not None for e in dependencies_doc["edges"])
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "BaseServlet")
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    entry_points_mapped = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == servlet_unit["unit_id"] and s["check"] == "entry_points_mapped"
    )
    assert entry_points_mapped["stored_status"] == "unknown"

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    assert not any(e["route_kind"] is not None for e in dependencies_doc["edges"])


def test_run_scan_a_route_and_a_filter_report_their_own_split_dependency_summary_counts(
    java_repo: Path,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F4 MAJOR, completeness):
    end to end - one servlet-mapping route and one filter-mapping filter
    in the same run must publish `dependency_summary.routes_by_kind ==
    {"http_filter": 1, "http_route": 1}`, joinable directly against
    `counts.entry_points_by_kind`'s own identical key vocabulary,
    instead of a single pre-aggregated `routes: 2` integer with nothing
    to tell the two apart."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "DispatcherServlet.java").write_text(
        "package com.acme.web;\nclass DispatcherServlet {\n}\n", encoding="utf-8")
    (web_dir / "AuthFilter.java").write_text(
        "package com.acme.web;\nclass AuthFilter {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <filter>\n"
        "    <filter-name>auth</filter-name>\n"
        "    <filter-class>com.acme.web.AuthFilter</filter-class>\n"
        "  </filter>\n"
        "  <filter-mapping>\n"
        "    <filter-name>auth</filter-name>\n"
        "    <url-pattern>/secure/*</url-pattern>\n"
        "  </filter-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_kinds = sorted(e["route_kind"] for e in dependencies_doc["edges"] if e["route_kind"])
    assert route_kinds == ["http_filter", "http_route"]

    payload = scan_pipeline.get_report(java_repo)
    assert payload["dependency_summary"]["routes"] == 2
    assert payload["dependency_summary"]["routes_by_kind"] == {"http_filter": 1, "http_route": 1}
    # java_repo's own default fixture also carries a `main` method
    # (cli_main), unrelated to this fix - checked as a subset, not
    # asserting the whole dict, so that fixture detail cannot break this
    # test.
    assert payload["counts"]["entry_points_by_kind"]["http_filter"] == 1
    assert payload["counts"]["entry_points_by_kind"]["http_route"] == 1


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_a_duplicate_servlet_name_with_conflicting_classes_publishes_a_conflict(
    java_repo: Path, swap_order: bool,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F1 BLOCKER, wrong-data): a
    web.xml declaring the SAME servlet-name twice with two DIFFERENT
    <servlet-class> values used to resolve LAST-DECLARATION-WINS,
    silently - a confident route owner (the position-dependent winner)
    and a confident no_entry_point/no_feature_link negative for the
    "losing" class, on a complete, zero-problem run. Neither candidate
    class may be a position-dependent winner now: the mapped route
    falls back to the web.xml FILE's own ownership (the same fallback
    an unmatched name already gets), a visible `duplicate_descriptor_name`
    problem names both claimants, both classes share a conflict_id, and
    both report entry_points_mapped/feature_linked unknown - never a
    confident negative for either. Parametrized on block order: the
    reader proved order-dependence by swapping the two <servlet> blocks
    and getting a DIFFERENT published owner from identical facts - this
    test asserts the fixed output is IDENTICAL either order."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "ServletA.java").write_text(
        "package com.acme.web;\nclass ServletA {\n}\n", encoding="utf-8")
    (web_dir / "ServletB.java").write_text(
        "package com.acme.web;\nclass ServletB {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    servlet_blocks = [
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletA</servlet-class>\n"
        "  </servlet>\n",
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletB</servlet-class>\n"
        "  </servlet>\n",
    ]
    if swap_order:
        servlet_blocks.reverse()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n" + "".join(servlet_blocks) +
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.acme.web.ServletA" in matching[0]["detail"]
    assert "com.acme.web.ServletB" in matching[0]["detail"]
    # MICRO-ROUND 29b (reviewer-3's delta on F1, wrong-data): the round-16
    # problems emitter used to group by conflict_id alone and hardcode
    # reason_code=duplicate_qualified_name, publishing a SECOND, factually
    # false row for this exact conflict ("'com.acme.web.ServletA' declared
    # in [...]" - it is declared in ServletA.java only) that contradicted
    # this same conflict_id's own conflict_kind and duplicated the row
    # above. Exactly one problem row for this conflict now - never two.
    assert not any(p["reason_code"] == "duplicate_qualified_name" for p in problems_doc["problems"])
    assert len(problems_doc["problems"]) == 1

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_a = next(u for u in modules_doc["units"] if u["display_name"] == "ServletA")
    servlet_b = next(u for u in modules_doc["units"] if u["display_name"] == "ServletB")
    assert servlet_a["conflict_id"] is not None
    assert servlet_a["conflict_id"] == servlet_b["conflict_id"]
    assert servlet_a["conflict_kind"] == "duplicate_descriptor_name"
    assert servlet_b["conflict_kind"] == "duplicate_descriptor_name"

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/api/*")
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]
    assert entry_point["owning_unit_id"] not in (servlet_a["unit_id"], servlet_b["unit_id"])

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    for unit_id in (servlet_a["unit_id"], servlet_b["unit_id"]):
        for check in ("entry_points_mapped", "feature_linked"):
            signal = next(
                s for s in readiness_doc["signals"]
                if s["unit_id"] == unit_id and s["check"] == check)
            assert signal["stored_status"] == "unknown"
            assert signal["reason_code"] == "duplicate_descriptor_name"


def test_run_scan_a_descriptor_conflict_with_only_one_in_scan_candidate_reports_unknown(
    java_repo: Path,
) -> None:
    """FIX ROUND 31 (twenty-seventh cold read, F1 BLOCKER, wrong-data):
    the descriptor-conflict readiness override used to require 2+
    IN-SCAN candidates before stamping a conflict_id at all
    ("fewer than 2 in-scan candidates means there is nothing this run
    can actually see conflicting") - empirically FALSE. The common real
    shape has exactly ONE in-scan claimant: the rival backing here is a
    jar class (com.jar.RemoteServlet) never in this scan - java.py's
    own duplicate_descriptor_name problem is published either way,
    naming LocalServlet as one of the rival backings, but the OLD gate
    left LocalServlet with no conflict_id at all - readiness gave it a
    CONFIDENT negative, byte-identical to Unrelated (a POJO with zero
    descriptor involvement), on a run that both saw and published the
    conflict. Now: LocalServlet reports unknown/duplicate_descriptor_
    name and gets a conflict_id; Unrelated keeps its own honest,
    unaffected confident negative - the fix must not over-apply."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "x"
    web_dir.mkdir(parents=True)
    (web_dir / "LocalServlet.java").write_text(
        "package com.x;\nclass LocalServlet {\n}\n", encoding="utf-8")
    (web_dir / "Unrelated.java").write_text(
        "package com.x;\nclass Unrelated {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.x.LocalServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.jar.RemoteServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.x.LocalServlet" in matching[0]["detail"]
    assert "com.jar.RemoteServlet" in matching[0]["detail"]

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    local_servlet = next(u for u in modules_doc["units"] if u["display_name"] == "LocalServlet")
    unrelated = next(u for u in modules_doc["units"] if u["display_name"] == "Unrelated")
    assert local_servlet["conflict_id"] is not None
    assert local_servlet["conflict_kind"] == "duplicate_descriptor_name"
    assert unrelated["conflict_id"] is None

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    for check in ("entry_points_mapped", "feature_linked"):
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == local_servlet["unit_id"] and s["check"] == check)
        assert signal["stored_status"] == "unknown"
        assert signal["reason_code"] == "duplicate_descriptor_name"

    # Unrelated must keep its own honest, unaffected confident negative -
    # the fix must not sweep up every unlinked unit into "unknown".
    entry_points_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == unrelated["unit_id"] and s["check"] == "entry_points_mapped")
    assert entry_points_signal["stored_status"] == "not_applicable"
    assert entry_points_signal["reason_code"] == "no_entry_point"
    feature_linked_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == unrelated["unit_id"] and s["check"] == "feature_linked")
    assert feature_linked_signal["stored_status"] == "unsatisfied"
    assert feature_linked_signal["reason_code"] == "no_feature_link"


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_two_servlet_class_elements_in_one_block_is_a_conflict(
    java_repo: Path, swap_order: bool,
) -> None:
    """FIX ROUND 31 (twenty-seventh cold read, F2 MAJOR, wrong-data): TWO
    <servlet-class> elements in ONE <servlet> block used to resolve
    SILENTLY to the FIRST (a bare .search() call is first-match-only,
    so the second element never became a declaration at all, never
    reaching the conflict machinery) - /dbl published confidently owned
    by FirstServlet (a fact no evidence actually supports), while
    SecondServlet got the confident no_entry_point negative, complete,
    ZERO problems. Byte-for-byte the defect micro-round 30b's own R1
    fixed for class+jsp, unswept to the same-element-kind case - a
    direct violation of "declaration order inside a block is never
    authoritative". Now a real conflict naming BOTH classes.
    Parametrized on which element appears first WITHIN the one block -
    order-independence proven both ways."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "FirstServlet.java").write_text(
        "package com.acme.web;\nclass FirstServlet {\n}\n", encoding="utf-8")
    (web_dir / "SecondServlet.java").write_text(
        "package com.acme.web;\nclass SecondServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    class_lines = [
        "    <servlet-class>com.acme.web.FirstServlet</servlet-class>\n",
        "    <servlet-class>com.acme.web.SecondServlet</servlet-class>\n",
    ]
    if swap_order:
        class_lines.reverse()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dbl</servlet-name>\n"
        + "".join(class_lines) +
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dbl</servlet-name>\n"
        "    <url-pattern>/dbl/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.acme.web.FirstServlet" in matching[0]["detail"]
    assert "com.acme.web.SecondServlet" in matching[0]["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/dbl/*")
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    first_servlet = next(u for u in modules_doc["units"] if u["display_name"] == "FirstServlet")
    second_servlet = next(u for u in modules_doc["units"] if u["display_name"] == "SecondServlet")
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]
    assert entry_point["owning_unit_id"] not in (first_servlet["unit_id"], second_servlet["unit_id"])
    assert first_servlet["conflict_id"] is not None
    assert first_servlet["conflict_id"] == second_servlet["conflict_id"]
    assert first_servlet["conflict_kind"] == "duplicate_descriptor_name"
    assert second_servlet["conflict_kind"] == "duplicate_descriptor_name"


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_two_filter_class_elements_in_one_block_is_a_conflict(
    java_repo: Path, swap_order: bool,
) -> None:
    """FIX ROUND 31 (F2 MAJOR): the filter twin of the servlet test
    above.

    MICRO-ROUND 31b (reviewer-3 delta, R2 one-sentence fix, wrong-
    data): this name is declared exactly ONCE (one <filter> block),
    with two disagreeing backings within it - the detail must never
    overclaim "is declared more than once" (the servlet path's own
    identical shape was already reworded around occurrence count; this
    fix never traveled to its filter twin until now)."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "FirstFilter.java").write_text(
        "package com.acme.web;\nclass FirstFilter {\n}\n", encoding="utf-8")
    (web_dir / "SecondFilter.java").write_text(
        "package com.acme.web;\nclass SecondFilter {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    class_lines = [
        "    <filter-class>com.acme.web.FirstFilter</filter-class>\n",
        "    <filter-class>com.acme.web.SecondFilter</filter-class>\n",
    ]
    if swap_order:
        class_lines.reverse()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <filter>\n"
        "    <filter-name>dbl</filter-name>\n"
        + "".join(class_lines) +
        "  </filter>\n"
        "  <filter-mapping>\n"
        "    <filter-name>dbl</filter-name>\n"
        "    <url-pattern>/dbl/*</url-pattern>\n"
        "  </filter-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.acme.web.FirstFilter" in matching[0]["detail"]
    assert "com.acme.web.SecondFilter" in matching[0]["detail"]
    assert "more than once" not in matching[0]["detail"]

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    first_filter = next(u for u in modules_doc["units"] if u["display_name"] == "FirstFilter")
    second_filter = next(u for u in modules_doc["units"] if u["display_name"] == "SecondFilter")
    assert first_filter["conflict_id"] is not None
    assert first_filter["conflict_id"] == second_filter["conflict_id"]
    assert first_filter["conflict_kind"] == "duplicate_descriptor_name"


def test_run_scan_two_jsp_file_elements_in_one_block_is_a_conflict(java_repo: Path) -> None:
    """FIX ROUND 31 (F2 MAJOR): TWO <jsp-file> elements in one <servlet>
    block silently kept the first, the problem detail naming only the
    first path - now a real conflict naming BOTH jsp paths as candidate
    labels. Neither backing has a unit at all (no class element), so
    this conflict is visible ONLY via the problem row, never via a
    conflict_id (nothing this run can stamp one on) - matching round
    31's own F1 disposition for a non-class candidate."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dbljsp</servlet-name>\n"
        "    <jsp-file>/WEB-INF/jsp/a.jsp</jsp-file>\n"
        "    <jsp-file>/WEB-INF/jsp/b.jsp</jsp-file>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dbljsp</servlet-name>\n"
        "    <url-pattern>/dbljsp/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "a.jsp" in matching[0]["detail"]
    assert "b.jsp" in matching[0]["detail"]


def test_run_scan_two_identical_servlet_class_elements_in_one_block_collapses_silently(
    java_repo: Path,
) -> None:
    """FIX ROUND 31 (F2 MAJOR control): TWO <servlet-class> elements in
    one block naming the IDENTICAL class - the harmless merge-artifact
    twin, same as round 29's own benign-twin precedent for two whole
    <servlet> blocks. Must collapse silently: no conflict, no problem,
    resolves normally to the one real class."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "DispatcherServlet.java").write_text(
        "package com.acme.web;\nclass DispatcherServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>\n"
        "    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert problems_doc["problems"] == []

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "DispatcherServlet")
    assert servlet_unit["conflict_id"] is None

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/api/*")
    assert entry_point["owning_unit_id"] == servlet_unit["unit_id"]


def test_run_scan_two_servlets_mapped_to_the_same_url_pattern_publishes_a_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 31 (twenty-seventh cold read, N4 JUDGE, taken - lean):
    two DIFFERENT servlet-names, each with its own unambiguous backing
    class, mapped to the IDENTICAL <url-pattern> - a container-rejected
    descriptor (undefined dispatch), previously zero problems. Now
    records a duplicate_route_target problem naming both owners; both
    servlets keep their own otherwise-unambiguous, unaffected readiness
    (this is a route-target collision, never a descriptor-name
    conflict - neither servlet gets a conflict_id).

    FIX ROUND 32 (twenty-eighth cold read, F7 LOW, JUDGE - taken,
    CORRECTION): round 31's own claim that this flips the run to
    DEGRADED is corrected here - duplicate_route_target is a fact ABOUT
    well-parsed descriptor content, matching none of the design's three
    degradation conditions (a parse failure, an evidence gap, or a
    resource cap). Recorded, non-degrading now, the same bucket
    `binary_excluded_root_sniffed_xml` (round 26b) already established
    for the identical shape of claim."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "ServletA.java").write_text(
        "package com.acme.web;\nclass ServletA {\n}\n", encoding="utf-8")
    (web_dir / "ServletB.java").write_text(
        "package com.acme.web;\nclass ServletB {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletA</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletB</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <url-pattern>/mix/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <url-pattern>/mix/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_route_target"]
    assert len(matching) == 1
    assert "com.acme.web.ServletA" in matching[0]["detail"]
    assert "com.acme.web.ServletB" in matching[0]["detail"]
    assert "/mix/*" in matching[0]["detail"]

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_a = next(u for u in modules_doc["units"] if u["display_name"] == "ServletA")
    servlet_b = next(u for u in modules_doc["units"] if u["display_name"] == "ServletB")
    assert servlet_a["conflict_id"] is None
    assert servlet_b["conflict_id"] is None

    # Both servlets keep their own otherwise-unambiguous entry_points_
    # mapped verdict, unaffected by the new problem - the fix records a
    # visible row without ever reaching either unit's own
    # adapter_problem_reasons (the synthetic qualified_name above never
    # matches a real unit).
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    for unit_id in (servlet_a["unit_id"], servlet_b["unit_id"]):
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == unit_id and s["check"] == "entry_points_mapped")
        assert signal["stored_status"] == "satisfied"


def test_run_scan_two_out_of_scan_servlets_mapped_to_the_same_pattern_get_distinct_ids(
    java_repo: Path,
) -> None:
    """FIX ROUND 38 (thirty-second cold read, F1 BLOCKER, wrong-data):
    the twin of the test above, but with BOTH servlet-classes OUT OF
    SCAN (never declared anywhere in this run) rather than real, in-scan
    classes. Each entry point then falls back to the SAME synthetic file
    owner (`WEB-INF/web.xml`'s own file unit, since neither resolves via
    `owning_unit_by_qualified_name`) - `entry_point_id` used to hash only
    `(kind, owning_unit_id, name)`, and both mappings share all three
    (same `http_route` kind, same file owner, same composed name since
    they share one url-pattern) even though `features_artifact.
    build_features`'s own `group_key` already keeps them in two SEPARATE
    feature groups via each claim's own `qualified_name`. Before this
    round's own fix (entry_point_id now also hashes `qualified_name`),
    this published 2 entry-point records sharing 1 distinct id - two
    features cross-claiming one id, record_count overstating the
    distinct-record total, `validate` reporting valid:true over a live
    stable-ID corruption. Mutation-verified: reverting the qualified_name
    hash input reproduces exactly this (1 distinct id for 2 records)."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.OutOfScanA</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <servlet-class>com.acme.web.OutOfScanB</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <url-pattern>/shared/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <url-pattern>/shared/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    all_entry_points = features_doc["entry_points"]
    # java_repo's own fixture also writes a real App.java with a main()
    # method, publishing an unrelated cli_main entry point alongside the
    # two http_route mappings under test - filtered out here.
    entry_points = [ep for ep in all_entry_points if ep["kind"] == "http_route"]
    assert len(entry_points) == 2
    ids = {ep["entry_point_id"] for ep in entry_points}
    assert len(ids) == 2, "the two out-of-scan mappings must not collide on entry_point_id"

    all_features = features_doc["features"]
    route_feature_ids = ids
    features = [f for f in all_features if set(f["entry_point_ids"]) & route_feature_ids]
    assert len(features) == 2
    claimed_ids = {ep_id for f in features for ep_id in f["entry_point_ids"]}
    assert claimed_ids == ids, "each feature must claim its own entry_point_id, never share one"
    for ep in entry_points:
        assert ep.get("conflict_id") is None

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    features_summary = next(a for a in scan_doc["artifacts"] if a["name"] == "features.json")
    assert features_summary["record_count"] == len(all_entry_points) + len(all_features)


def test_run_scan_two_out_of_scan_classes_sharing_a_simple_name_completes_not_refuses(
    java_repo: Path,
) -> None:
    """FIX ROUND 39 (thirty-third cold read, F1 BLOCKER, .cr33-fid2,
    wrong-data): round 38's own id-family sweep ruled `feature_id`
    "safe by construction" - false: for an out-of-scan class, `label`
    is the simple name and `unit_ids` is the SAME synthetic file
    owner, so two jar-shipped classes sharing a simple name in
    different packages (two `LoginServlet`s, an utterly ordinary
    shape) collided on `feature_id`, and round 38's own publish-time
    sweep then REFUSED TO PUBLISH the whole run - permanently
    unscannable, since no --scope/--exclude exists to narrow past it
    this slice. Different url-patterns here (unlike the entry_point_id
    fixture above) isolate the bug to feature_id specifically - the
    entry points themselves never collided."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.vendor.pkg1.LoginServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <servlet-class>com.vendor.pkg2.LoginServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <url-pattern>/a/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <url-pattern>/b/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    login_features = [f for f in features_doc["features"] if f["label"] == "LoginServlet"]
    assert len(login_features) == 2
    assert len({f["feature_id"] for f in login_features}) == 2

    validate_result = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert validate_result["valid"] is True


def test_run_scan_three_ordinary_routes_publish_unaffected_by_the_identity_fix(
    java_repo: Path,
) -> None:
    """FIX ROUND 40 (thirty-fourth cold read, Part A F1+F2, .cr34-canary,
    control): a plain fixture of three ordinary, short, non-colliding
    routes across three distinct servlet classes - none long enough to
    truncate, none containing an escaped character - must publish
    exactly as before this round's own identity/display split: 3
    entry points, 3 edges, each with its own distinct id, names
    unaffected. The identity fix only ever changes behavior when the
    raw and bounded-display values DIFFER; this fixture keeps them
    identical throughout, so it is the fix's own no-op case."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.AlphaServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <servlet-class>com.acme.web.BetaServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>c</servlet-name>\n"
        "    <servlet-class>com.acme.web.GammaServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <url-pattern>/alpha</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <url-pattern>/beta</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>c</servlet-name>\n"
        "    <url-pattern>/gamma</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_points = [ep for ep in features_doc["entry_points"] if ep["kind"] == "http_route"]
    assert len(entry_points) == 3
    assert {ep["name"] for ep in entry_points} == {"/alpha", "/beta", "/gamma"}
    assert len({ep["entry_point_id"] for ep in entry_points}) == 3

    dependencies_doc = json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_edges = [e for e in dependencies_doc["edges"] if e["relation"] == "route"]
    assert len(route_edges) == 3
    assert {e["target_external"] for e in route_edges} == {"/alpha", "/beta", "/gamma"}
    assert len({e["edge_id"] for e in route_edges}) == 3

    validate_result = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert validate_result["valid"] is True


def test_run_scan_two_url_patterns_truncating_to_the_same_display_string_get_distinct_ids(
    java_repo: Path,
) -> None:
    """FIX ROUND 40 (thirty-fourth cold read, Part A F1+F2 BLOCKER,
    .cr34-collide, wrong-data): `_bounded_route_target`
    (sanitize + truncate to 200 chars) used to be applied at LEAF
    EXTRACTION time, before the truncated value was fed into
    `digests.entry_point_id`/`digests.edge_id` as the route's own
    identity - so two GENUINELY DIFFERENT declared url-patterns that
    merely share a >200-char prefix truncated to the IDENTICAL bounded
    string, and collided on both ids even though nothing about the
    real, undisplayed data was actually the same. Both mappings share
    one servlet-class (isolating the bug to the route value itself,
    not the owner/qualified_name entry_point_id's round-38 fix already
    keeps apart). Fixed by threading the RAW, pre-bounding value
    alongside the bounded display value all the way to
    `JavaEntryPointClaim.identity_name`/`JavaEdgeClaim.identity_target`,
    consumed at each id's own call site in preference to the (lossy)
    display value - the published `name`/`target_external` fields keep
    using the unchanged, safe, bounded value."""
    import json

    prefix = "/" + "x" * 199
    pattern_a = prefix + "A" * 60
    pattern_b = prefix + "B" * 60
    assert pattern_a != pattern_b
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.SharedServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        f"    <url-pattern>{pattern_a}</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        f"    <url-pattern>{pattern_b}</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_points = [ep for ep in features_doc["entry_points"] if ep["kind"] == "http_route"]
    assert len(entry_points) == 2
    # Both publish the SAME (truncated) display name - the display
    # projection is unaffected by this fix.
    assert len({ep["name"] for ep in entry_points}) == 1
    assert len({ep["entry_point_id"] for ep in entry_points}) == 2, (
        "two genuinely different url-patterns must not collide on "
        "entry_point_id merely because they truncate identically"
    )

    dependencies_doc = json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_edges = [e for e in dependencies_doc["edges"] if e["relation"] == "route"]
    assert len(route_edges) == 2
    assert len({e["target_external"] for e in route_edges}) == 1
    assert len({e["edge_id"] for e in route_edges}) == 2, (
        "two genuinely different url-patterns must not collide on "
        "edge_id merely because they truncate identically"
    )

    validate_result = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert validate_result["valid"] is True


def test_run_scan_a_real_invisible_character_and_its_own_escaped_spelling_get_distinct_ids(
    java_repo: Path,
) -> None:
    """FIX ROUND 40 (thirty-fourth cold read, Part A F1+F2 BLOCKER,
    .cr34-dupname / .cr34-enc, wrong-data): the SAME lossy-projection
    bug as the truncation case above, reached through the escaping
    half of `_bounded_route_target` instead of the length half - a
    route containing a REAL ZERO WIDTH SPACE (U+200B) escapes to the
    six-character literal text ``\\u200b`` for display, and a DIFFERENT
    route that spells that same six-character text out LITERALLY
    (never containing the real character at all) passes through
    unescaped - both bounded display strings come out byte-identical,
    even though the real, undisplayed url-patterns were never the
    same. Same fix, same mechanism as the truncation test above.

    CORRECTED (round 42, thirty-sixth cold read, F2 MINOR): this test
    used to assert the two ALSO display identically - true only because
    the escape choke point was itself non-injective at the time (a
    literal backslash was never escaped, so pattern_b's own leading
    backslash passed through unchanged). Round 42's own F2 fix escapes
    a literal backslash first, closing that gap too - pattern_b's own
    leading backslash now escapes to two literal backslashes, so the
    two patterns no longer display identically EITHER. Kept as a
    control here for exactly that: the id-level fix (F1+F2) and the
    injective-escape fix (F2, round 42) are independent guarantees, and
    this pair now demonstrates both holding simultaneously."""
    import json

    real_zwsp = chr(0x200B)
    pattern_a = "/shared/" + real_zwsp
    pattern_b = "/shared/" + "\\u200b"
    assert pattern_a != pattern_b
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.SharedServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        f"    <url-pattern>{pattern_a}</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        f"    <url-pattern>{pattern_b}</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_points = [ep for ep in features_doc["entry_points"] if ep["kind"] == "http_route"]
    assert len(entry_points) == 2
    # CORRECTED (round 42, F2): now that the escape itself is
    # injective, this pair no longer displays identically either - see
    # this test's own docstring correction.
    assert len({ep["name"] for ep in entry_points}) == 2, (
        "a real ZWSP and its own literal escaped spelling must not display identically "
        "now that the escape choke point is injective (round 42's own F2 fix)"
    )
    assert len({ep["entry_point_id"] for ep in entry_points}) == 2, (
        "a real invisible character and its own escaped literal spelling "
        "must not collide on entry_point_id merely because they display "
        "identically"
    )

    dependencies_doc = json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    route_edges = [e for e in dependencies_doc["edges"] if e["relation"] == "route"]
    assert len(route_edges) == 2
    # CORRECTED (round 42, F2): same correction as entry_points' own
    # name assertion above.
    assert len({e["target_external"] for e in route_edges}) == 2, (
        "a real ZWSP and its own literal escaped spelling must not display identically "
        "now that the escape choke point is injective (round 42's own F2 fix)"
    )
    assert len({e["edge_id"] for e in route_edges}) == 2, (
        "a real invisible character and its own escaped literal spelling "
        "must not collide on edge_id merely because they display identically"
    )

    validate_result = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert validate_result["valid"] is True


def test_run_scan_a_secrets_xml_collision_stays_complete_not_degraded(java_repo: Path) -> None:
    """FIX ROUND 39 (thirty-third cold read, F2 MAJOR, .cr33-secxml,
    wrong-data - THE SELF-CONTRADICTION): round 38's own F4 fix
    recorded a secret-excluded `secrets.xml` collision as a visible,
    NON-degrading problem (`SECRET_PATTERNS_CAVEAT` says so explicitly)
    - but `run_scan`'s own status/degraded_by computation used to
    derive from the bare TRUTHINESS of `discovery_result.problems`
    (any discovery problem at all degrades, never checking each
    problem's own `degrades_run`), so this exact run published
    status=degraded + degraded_by containing `secret_pattern_matched_
    code_bearing_file` - directly contradicting the SAME run's own
    problem detail and caveat, both of which say "recorded, NOT
    DEGRADING." One of the two published facts was false in every such
    run. Fixed by deriving status/degraded_by from each discovery
    problem's own (now-real) `degrades_run` flag."""
    import json

    (java_repo / "secrets.xml").write_text(
        "<beans><bean id=\"x\" class=\"com.acme.X\"/></beans>", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "secret_pattern_matched_code_bearing_file"]
    assert len(matching) == 1

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "secret_pattern_matched_code_bearing_file" not in scan_doc["degraded_by"]


def test_a_secret_excluded_code_bearing_file_produces_no_unit(java_repo: Path) -> None:
    """MICRO-ROUND 48b (F3, the dead-table-entry lock): readiness_
    artifact.py's own `_READINESS_CHECKS_BY_REASON_CODE["secret_pattern_
    matched_code_bearing_file"]` entry is dead code TODAY because a
    secret-excluded file never gets a ModuleRecord/unit at all - a
    structural fact a later round could silently change (e.g. by adding
    a synthesized-unit pass for this reason code the way `binary_
    excluded_root_sniffed_xml`/`binary_excluded_code_bearing_file`
    already have). This test locks the STRUCTURAL FACT itself, not the
    dead-code consequence, so a future change that makes the entry live
    trips THIS test - a controlled, expected failure naming exactly what
    changed - rather than a cold read rediscovering the same gap cold."""
    import json

    (java_repo / "secrets.xml").write_text(
        "<beans><bean id=\"x\" class=\"com.acme.X\"/></beans>", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    assert not any(
        "secrets.xml" in record["paths"] for record in modules_doc["units"]
    )
    assert not any(
        "secret_pattern_matched_code_bearing_file" in record.get("adapter_problem_reasons", [])
        for record in modules_doc["units"]
    )


def test_a_non_utf8_path_produces_no_unit(java_repo: Path, monkeypatch) -> None:
    """MICRO-ROUND 48c (F1, widening the 48b dead-entry family):
    `non_utf8_path` shares the identical structural shape as `secret_
    pattern_matched_code_bearing_file` above - its own, sole emitter
    sits inside discovery.py's own enumeration walk, which `continue`s
    past the offending entry BEFORE it is ever appended to `discovery.
    files`, so `modules_artifact.build_modules`'s own main loop (which
    only ever constructs a record by iterating `discovery.files` itself)
    can never construct one for it. Locks the structural fact directly,
    the same way the secret-excluded test above does - a real non-UTF-8
    filename is POSIX-only and not reliably constructible from every
    dev/CI platform (see test_comprehension_discovery.py's own precedent
    for this), so the underlying check is monkeypatched to fire for one
    real, ordinary path instead - the SAME technique test_comprehension_
    discovery.py's own `test_an_unrepresentable_filename_marks_the_
    fingerprint_incomplete` already establishes."""
    import json

    from agenttalk.comprehension import discovery as discoverymod

    (java_repo / "weird.txt").write_bytes(b"anything")
    monkeypatch.setattr(
        discoverymod, "_non_utf8_path_problem_detail",
        lambda relative: (
            {"path": relative, "detail": "simulated non-utf8 path"}
            if relative == "weird.txt" else None
        ),
    )

    outcome = scan_pipeline.run_scan(java_repo)

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert any(p["reason_code"] == "non_utf8_path" for p in problems_doc["problems"])

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    assert not any("weird.txt" in record["paths"] for record in modules_doc["units"])
    assert not any(
        "non_utf8_path" in record.get("adapter_problem_reasons", [])
        for record in modules_doc["units"]
    )


def test_path_excluded_is_confirmed_reachable_on_a_real_unit(java_repo: Path, monkeypatch) -> None:
    """MICRO-ROUND 48c (F1, the corrected half): unlike the two dead
    members above, `path_excluded`'s own emitter (worker.py's `resolve_
    under_root` defense-in-depth re-confinement check) runs on a path
    that is UNCONDITIONALLY still in `discovery.files` at that point
    (scan_pipeline.py hands the worker exactly that list, unfiltered) -
    so build_modules's own main loop unconditionally constructs a real
    unit for it regardless, and attributes this reason via `worker_
    problem_reasons_by_path`. This is NOT a member of the dead-entry
    family despite superficially similar wording - locks the CORRECTED
    claim (reachable, not dead) end-to-end, complementing test_
    comprehension_modules_artifact.py's own synthetic-input proof of the
    same fact at the build_modules level directly."""
    import json

    (java_repo / "blocked.txt").write_bytes(b"anything")
    real_resolve = workermod.resolve_under_root

    def _fake_resolve_under_root(value, *, root, label="path"):
        if value == "blocked.txt":
            raise workermod.EnvelopeError(
                f"{label} resolves outside the project root: {value!r}")
        return real_resolve(value, root=root, label=label)

    monkeypatch.setattr(workermod, "resolve_under_root", _fake_resolve_under_root)

    outcome = scan_pipeline.run_scan(java_repo)

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert any(
        p["reason_code"] == "path_excluded" and p.get("path") == "blocked.txt"
        for p in problems_doc["problems"]
    )

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    matching = [record for record in modules_doc["units"] if "blocked.txt" in record["paths"]]
    assert len(matching) == 1
    assert "path_excluded" in matching[0]["adapter_problem_reasons"]


def _assert_reason_never_attaches_to_a_real_unit(outcome, reason_code: str) -> None:
    import json

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert any(p["reason_code"] == reason_code for p in problems_doc["problems"])
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    assert not any(
        reason_code in record.get("adapter_problem_reasons", []) for record in modules_doc["units"])


def test_duplicate_descriptor_name_never_attaches_to_a_real_unit(java_repo: Path) -> None:
    """MICRO-ROUND 49b (F3, settle - reviewer-3's own round-48-KeyError-
    class hunt): duplicate_descriptor_name anchors its own PROBLEM to a
    SYNTHETIC qualified_name (f"{relative_path}#{name}") by design (two
    real owners, no single one to anchor to) - never a real unit's own
    qualified_name, never None (which would broadcast file-wide instead),
    so it never reaches a real unit via `adapter_problem_reasons`/this
    readiness table. This fixture deliberately names two candidate
    classes (com.acme.A/B) that do NOT exist in java_repo, so neither
    resolves in-scan and NEITHER path attaches - `duplicate_descriptor_
    name` also has a SEPARATE, live anchor (f"{relative_path}#servlet#
    {name}", the REAL measured format `_populate_descriptor_name_
    conflicts` hashes into a real `conflict_id`) that DOES stamp a real
    unit's own `conflict_kind` whenever at least one candidate class
    resolves in-scan - a different, already-live question this fixture
    does not exercise and this test does not claim to settle. Proven,
    not merely argued, for the table path only: constructed the exact
    reproducer and measured modules.json's own units for a match on
    `adapter_problem_reasons` - zero."""
    (java_repo / "WEB-INF").mkdir(parents=True, exist_ok=True)
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet><servlet-name>s1</servlet-name><servlet-class>com.acme.A</servlet-class></servlet>\n"
        "  <servlet><servlet-name>s1</servlet-name><servlet-class>com.acme.B</servlet-class></servlet>\n"
        "</web-app>\n",
        encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    _assert_reason_never_attaches_to_a_real_unit(outcome, "duplicate_descriptor_name")


def test_duplicate_route_target_never_attaches_to_a_real_unit(java_repo: Path) -> None:
    """MICRO-ROUND 49b (F3's own duplicate_route_target twin): anchors to
    f"{relative_path}#duplicate_route_target#{url_pattern}" by design -
    the identical "two real owners, no single anchor" shape."""
    (java_repo / "WEB-INF").mkdir(parents=True, exist_ok=True)
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping><servlet-name>a</servlet-name><url-pattern>/x</url-pattern></servlet-mapping>\n"
        "  <servlet-mapping><servlet-name>b</servlet-name><url-pattern>/x</url-pattern></servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    _assert_reason_never_attaches_to_a_real_unit(outcome, "duplicate_route_target")


def test_descriptor_name_without_class_never_attaches_to_a_real_unit(java_repo: Path) -> None:
    """MICRO-ROUND 49b (F3's own descriptor_name_without_class twin): a
    <servlet> declaring a name but backed by neither a usable class nor
    a <jsp-file> - anchors to f"{relative_path}#{name}", the same
    synthetic-marker shape."""
    (java_repo / "WEB-INF").mkdir(parents=True, exist_ok=True)
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet><servlet-name>s1</servlet-name></servlet>\n"
        "</web-app>\n",
        encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    _assert_reason_never_attaches_to_a_real_unit(outcome, "descriptor_name_without_class")


def test_undeclared_descriptor_name_never_attaches_to_a_real_unit(java_repo: Path) -> None:
    """MICRO-ROUND 49b (F3's own undeclared_descriptor_name twin): a
    <servlet-mapping> naming a servlet declared nowhere at all - anchors
    to f"{relative_path}#{name}", the same synthetic-marker shape."""
    (java_repo / "WEB-INF").mkdir(parents=True, exist_ok=True)
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping><servlet-name>ghost</servlet-name><url-pattern>/g</url-pattern></servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    _assert_reason_never_attaches_to_a_real_unit(outcome, "undeclared_descriptor_name")


def test_duplicate_qualified_name_never_attaches_via_adapter_problem_reasons(java_repo: Path) -> None:
    """MICRO-ROUND 49b (F3's own duplicate_qualified_name settlement -
    structurally DIFFERENT from its four siblings above): this reason
    never even reaches adapter_problem_reasons at all - it is published
    exclusively via ModuleRecord's own conflict_kind/conflict_id fields,
    which readiness_artifact.py's own conflict-override loop consults
    directly, never through _READINESS_CHECKS_BY_REASON_CODE. Two
    files declaring the identical fully-qualified name is the real
    reproducer; the readiness signal still correctly reports unknown -
    via conflict_kind, not a table lookup on this string."""
    (java_repo / "src" / "main" / "java" / "p" / "Dup1.java").write_text(
        "package p;\nclass Widget {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Dup2.java").write_text(
        "package p;\nclass Widget {}\n", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    _assert_reason_never_attaches_to_a_real_unit(outcome, "duplicate_qualified_name")


def test_run_scan_a_genuinely_degrading_discovery_problem_still_degrades(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 39 (F2 MAJOR, control): the fix above must not flip
    every discovery problem to non-degrading - a real resource-limit
    hit (a file over the per-file byte cap) still publishes
    status=degraded and names its own reason_code in degraded_by,
    exactly as before this round."""
    import json

    from agenttalk.comprehension import discovery as discoverymod

    monkeypatch.setattr(discoverymod, "MAX_PER_FILE_BYTES", 200)
    (java_repo / "big.bin").write_bytes(b"\xff" * 500)

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "resource_limit" in scan_doc["degraded_by"]


def test_run_scan_mixed_degrading_and_non_degrading_discovery_problems(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 39 (F2 MAJOR, mixed control): a run with BOTH a
    non-degrading secrets.xml collision AND a genuinely-degrading
    resource-limit hit must publish status=degraded (the genuine
    problem alone earns it) with degraded_by naming ONLY the
    degrading reason_code, never the non-degrading one - proving the
    fix filters per-problem, not merely "the first problem decides
    the whole run."""
    import json

    from agenttalk.comprehension import discovery as discoverymod

    monkeypatch.setattr(discoverymod, "MAX_PER_FILE_BYTES", 200)
    (java_repo / "secrets.xml").write_text(
        "<beans><bean id=\"x\" class=\"com.acme.X\"/></beans>", encoding="utf-8")
    (java_repo / "big.bin").write_bytes(b"\xff" * 500)

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "resource_limit" in scan_doc["degraded_by"]
    assert "secret_pattern_matched_code_bearing_file" not in scan_doc["degraded_by"]


def test_run_scan_two_servlets_mapped_to_different_url_patterns_publishes_no_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 31 (N4 control): two DIFFERENT servlet-names mapped to
    two DIFFERENT url-patterns is the ordinary, healthy shape - must
    never trigger duplicate_route_target."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "ServletA.java").write_text(
        "package com.acme.web;\nclass ServletA {\n}\n", encoding="utf-8")
    (web_dir / "ServletB.java").write_text(
        "package com.acme.web;\nclass ServletB {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletA</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <servlet-class>com.acme.web.ServletB</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <url-pattern>/a/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <url-pattern>/b/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(p["reason_code"] == "duplicate_route_target" for p in problems_doc["problems"])


def test_run_scan_two_servlet_names_backed_by_the_same_class_mapped_to_one_pattern_publishes_a_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 32 (twenty-eighth cold read, F6 MINOR, wrong-data): case
    C - two DIFFERENT servlet-names (a real, legal descriptor shape - one
    servlet class registered under two names) both backed by the SAME
    class, mapped to the identical <url-pattern>, used to dedup down to
    ONE owner (keyed on the resolved CLASS) and publish zero problems -
    contradicting this check's own "2+ different servlets" claim. Now
    keyed on the (name, class) pair, so two distinct names always count
    as two, regardless of what class either resolves to.

    FIX ROUND 32 (F7 LOW, JUDGE - taken): duplicate_route_target is
    recorded, non-degrading (see that fix's own reasoning) - status
    stays complete."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "SharedServlet.java").write_text(
        "package com.acme.web;\nclass SharedServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <servlet-class>com.acme.web.SharedServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <servlet-class>com.acme.web.SharedServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>a</servlet-name>\n"
        "    <url-pattern>/shared/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>b</servlet-name>\n"
        "    <url-pattern>/shared/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_route_target"]
    assert len(matching) == 1
    assert "a" in matching[0]["detail"]
    assert "b" in matching[0]["detail"]
    assert "com.acme.web.SharedServlet" in matching[0]["detail"]
    assert "/shared/*" in matching[0]["detail"]


def test_run_scan_the_same_servlet_name_mapped_twice_to_one_pattern_publishes_no_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 32 (F6's own case D regression control): the SAME
    servlet-name mapped to the identical <url-pattern> twice (two
    <servlet-mapping> elements, one name) is never a "2+ different
    servlets" collision - the (name, class) pair dedup this fix relies on
    must still collapse this to a single element, exactly as the old
    class-keyed dedup did."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "SoleServlet.java").write_text(
        "package com.acme.web;\nclass SoleServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>sole</servlet-name>\n"
        "    <servlet-class>com.acme.web.SoleServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>sole</servlet-name>\n"
        "    <url-pattern>/dup/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>sole</servlet-name>\n"
        "    <url-pattern>/dup/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(p["reason_code"] == "duplicate_route_target" for p in problems_doc["problems"])


def test_run_scan_a_duplicate_filter_name_with_conflicting_classes_publishes_a_conflict(
    java_repo: Path,
) -> None:
    """FIX ROUND 29 (F1 BLOCKER): the filter twin of the servlet-name
    conflict above - <filter-name> declared twice with two different
    <filter-class> values."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "FilterA.java").write_text(
        "package com.acme.web;\nclass FilterA {\n}\n", encoding="utf-8")
    (web_dir / "FilterB.java").write_text(
        "package com.acme.web;\nclass FilterB {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <filter>\n"
        "    <filter-name>auth</filter-name>\n"
        "    <filter-class>com.acme.web.FilterA</filter-class>\n"
        "  </filter>\n"
        "  <filter>\n"
        "    <filter-name>auth</filter-name>\n"
        "    <filter-class>com.acme.web.FilterB</filter-class>\n"
        "  </filter>\n"
        "  <filter-mapping>\n"
        "    <filter-name>auth</filter-name>\n"
        "    <url-pattern>/secure/*</url-pattern>\n"
        "  </filter-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.acme.web.FilterA" in matching[0]["detail"]
    assert "com.acme.web.FilterB" in matching[0]["detail"]
    # MICRO-ROUND 29b (reviewer-3's delta on F1, wrong-data): the filter
    # twin of the servlet-name check above - the old emitter would have
    # published a second, false duplicate_qualified_name row for this
    # SAME conflict_id.
    assert not any(p["reason_code"] == "duplicate_qualified_name" for p in problems_doc["problems"])
    assert len(problems_doc["problems"]) == 1

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    filter_a = next(u for u in modules_doc["units"] if u["display_name"] == "FilterA")
    filter_b = next(u for u in modules_doc["units"] if u["display_name"] == "FilterB")
    assert filter_a["conflict_id"] is not None
    assert filter_a["conflict_id"] == filter_b["conflict_id"]
    assert filter_a["conflict_kind"] == "duplicate_descriptor_name"


def test_run_scan_a_three_way_duplicate_servlet_name_publishes_one_conflict_row(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 29b (reviewer-3's own matrix, one-condition fix): the
    SAME servlet-name declared THREE times with three different class
    values - the round-16 problems emitter's own bug generalizes to any
    group size, not just 2. Exactly one problem row (java.py's own
    duplicate_descriptor_name), never a second, false
    duplicate_qualified_name row for the same conflict_id."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    for letter in ("A", "B", "C"):
        (web_dir / f"Servlet{letter}.java").write_text(
            f"package com.acme.web;\nclass Servlet{letter} {{\n}}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    servlet_blocks = "".join(
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        f"    <servlet-class>com.acme.web.Servlet{letter}</servlet-class>\n"
        "  </servlet>\n"
        for letter in ("A", "B", "C")
    )
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n" + servlet_blocks +
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    for letter in ("A", "B", "C"):
        assert f"com.acme.web.Servlet{letter}" in matching[0]["detail"]
    assert not any(p["reason_code"] == "duplicate_qualified_name" for p in problems_doc["problems"])
    assert len(problems_doc["problems"]) == 1

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlets = [
        next(u for u in modules_doc["units"] if u["display_name"] == f"Servlet{letter}")
        for letter in ("A", "B", "C")
    ]
    assert len({s["conflict_id"] for s in servlets}) == 1
    assert all(s["conflict_kind"] == "duplicate_descriptor_name" for s in servlets)


def test_run_scan_the_four_name_matrix_each_declared_name_gets_its_own_honest_row(
    java_repo: Path,
) -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F1 BLOCKER, THE ROOT CAUSE):
    _servlet_class_by_name's own OLD class-keyed map was invisible to
    any declaration that did not carry a usable class - four servlet-
    mapping names, each hitting a DIFFERENT shape the old map could not
    distinguish, must each publish its own ONE honest problem row.

    - "ghost": no <servlet> element at all -> undeclared_descriptor_name
      (round 29 F9c, unaffected by this round - the control).
    - "jspBacked": a <jsp-file>-backed servlet, no <servlet-class> at
      all -> jsp_file_servlet (unsupported_entry_point_shape), naming
      the JSP path - NEVER undeclared_descriptor_name (F1(1a)): this
      name IS declared, just not class-backed.
    - "nameOnly": a bare <description>-only <servlet> block, no class,
      no jsp-file -> descriptor_name_without_class - NEVER
      undeclared_descriptor_name (F1(1b)).
    - "unreadableClass": a <servlet-class> present but undecodable
      (split CDATA) -> route_value_unrecoverable ONLY - NEVER ALSO
      undeclared_descriptor_name for the same anchor (F1(1c), the
      self-contradiction the reader's own repro named: the name is
      literally sitting in a list called `undecodable`)."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>jspBacked</servlet-name>\n"
        "    <jsp-file>/WEB-INF/jsp/admin.jsp</jsp-file>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>nameOnly</servlet-name>\n"
        "    <description>legacy servlet, class retired</description>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>unreadableClass</servlet-name>\n"
        "    <servlet-class><![CDATA[com.a]]>b<![CDATA[cme.Admin]]></servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>ghost</servlet-name>\n"
        "    <url-pattern>/ghost/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>jspBacked</servlet-name>\n"
        "    <url-pattern>/jsp/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>nameOnly</servlet-name>\n"
        "    <url-pattern>/name-only/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>unreadableClass</servlet-name>\n"
        "    <url-pattern>/unreadable/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    by_reason: dict[str, list[dict]] = {}
    for p in problems_doc["problems"]:
        by_reason.setdefault(p["reason_code"], []).append(p)

    ghost_rows = [p for p in by_reason.get("undeclared_descriptor_name", []) if "ghost" in p["detail"]]
    assert len(ghost_rows) == 1

    jsp_rows = [
        p for p in by_reason.get("unsupported_entry_point_shape", []) if "jspBacked" in p["detail"]]
    assert len(jsp_rows) == 1
    assert "admin.jsp" in jsp_rows[0]["detail"]

    name_only_rows = [
        p for p in by_reason.get("descriptor_name_without_class", []) if "nameOnly" in p["detail"]]
    assert len(name_only_rows) == 1

    unreadable_rows = [
        p for p in by_reason.get("route_value_unrecoverable", [])
        if p["qualified_name"] == "WEB-INF/web.xml#unreadableClass"]
    assert len(unreadable_rows) == 1

    # THE self-contradiction (F1(1c)) and its two siblings: none of the
    # three genuinely-declared-but-unbacked names may ALSO appear as an
    # undeclared_descriptor_name row.
    undeclared_details = " ".join(p["detail"] for p in by_reason.get("undeclared_descriptor_name", []))
    assert "jspBacked" not in undeclared_details
    assert "nameOnly" not in undeclared_details
    assert "unreadableClass" not in undeclared_details


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_a_class_and_jsp_file_pair_for_the_same_name_is_a_conflict(
    java_repo: Path, swap_order: bool,
) -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F1(1d)/.cr26-desc3 case E,
    BLOCKER, wrong-data): the SAME servlet-name declared twice - once
    class-backed, once <jsp-file>-backed - used to resolve CONFIDENTLY
    to the class (the old map only ever saw the one declaration that
    happened to carry a decodable class), zero problems, no conflict_id
    - the exact defect round 29's own F1 exists to prevent, reachable
    through a declaration shape the old detector could not see. Now a
    real conflict: one `duplicate_descriptor_name` row, and the mapped
    route falls back to the synthetic per-mapping owner (the web.xml
    file), never confidently to the class. Parametrized on block order -
    order-independence proven both ways."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "MixedServlet.java").write_text(
        "package com.acme.web;\nclass MixedServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    servlet_blocks = [
        "  <servlet>\n"
        "    <servlet-name>mixed</servlet-name>\n"
        "    <servlet-class>com.acme.web.MixedServlet</servlet-class>\n"
        "  </servlet>\n",
        "  <servlet>\n"
        "    <servlet-name>mixed</servlet-name>\n"
        "    <jsp-file>/WEB-INF/jsp/mixed.jsp</jsp-file>\n"
        "  </servlet>\n",
    ]
    if swap_order:
        servlet_blocks.reverse()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n" + "".join(servlet_blocks) +
        "  <servlet-mapping>\n"
        "    <servlet-name>mixed</servlet-name>\n"
        "    <url-pattern>/mixed/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.acme.web.MixedServlet" in matching[0]["detail"]
    assert "mixed.jsp" in matching[0]["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/mixed/*")
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    mixed_servlet_unit = next(u for u in modules_doc["units"] if u["display_name"] == "MixedServlet")
    # THE remedy-claim truthfulness fix: the route falls back to the
    # web.xml FILE, never confidently to the class - the exact defect
    # this test exists to prevent.
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]
    assert entry_point["owning_unit_id"] != mixed_servlet_unit["unit_id"]


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_a_single_block_naming_both_a_class_and_a_jsp_file_is_a_conflict(
    java_repo: Path, swap_order: bool,
) -> None:
    """MICRO-ROUND 30b (reviewer-3 delta, R1 note-only, wrong-data): a
    SINGLE <servlet> block declaring BOTH <servlet-class> AND <jsp-
    file> is spec-ILLEGAL (the schema makes them a choice) - this used
    to resolve SILENTLY to the class (checked first, jsp-file only
    considered when no class was present), the jsp-file discarded with
    no trace at all - complete, zero rows, on a descriptor that cannot
    actually deploy in any real container. Now a real conflict: one
    duplicate_descriptor_name row naming BOTH candidates, and the
    mapped route falls back to the synthetic per-mapping owner, never
    confidently to the class. Parametrized on which element appears
    first WITHIN the one block - order-independence proven both ways."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "BothBackedServlet.java").write_text(
        "package com.acme.web;\nclass BothBackedServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    class_line = "    <servlet-class>com.acme.web.BothBackedServlet</servlet-class>\n"
    jsp_line = "    <jsp-file>/WEB-INF/jsp/both.jsp</jsp-file>\n"
    inner_lines = [class_line, jsp_line]
    if swap_order:
        inner_lines.reverse()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>bothbacked</servlet-name>\n"
        + "".join(inner_lines) +
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>bothbacked</servlet-name>\n"
        "    <url-pattern>/bothbacked/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    assert len(matching) == 1
    assert "com.acme.web.BothBackedServlet" in matching[0]["detail"]
    assert "both.jsp" in matching[0]["detail"]
    # Never the pre-fix "is declared more than once" overclaim - this
    # name is declared exactly once, with two disagreeing backings.
    assert "more than once" not in matching[0]["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/bothbacked/*")
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    both_backed_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "BothBackedServlet")
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]
    assert entry_point["owning_unit_id"] != both_backed_unit["unit_id"]

    # FIX ROUND 31 (twenty-seventh cold read, F1 BLOCKER): the jsp-file
    # half of this conflict has no unit at all - only ONE in-scan
    # candidate (BothBackedServlet itself). The old >=2-in-scan gate
    # left it with no conflict_id, a confident negative byte-identical
    # to an uninvolved POJO - now it must report unknown/
    # duplicate_descriptor_name and carry a conflict_id.
    assert both_backed_unit["conflict_id"] is not None
    assert both_backed_unit["conflict_kind"] == "duplicate_descriptor_name"
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    for check in ("entry_points_mapped", "feature_linked"):
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == both_backed_unit["unit_id"] and s["check"] == check)
        assert signal["stored_status"] == "unknown"
        assert signal["reason_code"] == "duplicate_descriptor_name"


@pytest.mark.parametrize("swap_order", [False, True])
def test_run_scan_a_half_undecodable_conflict_never_confidently_resolves(
    java_repo: Path, swap_order: bool,
) -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F1(1e)/.cr26-desc3 case F,
    BLOCKER, wrong-data): the SAME servlet-name declared twice - one
    <servlet-class> decodable, one not - used to resolve CONFIDENTLY to
    whichever decoded, publishing ONLY a `route_value_unrecoverable` row
    whose own remedy claim ("falls back to the synthetic per-mapping
    owner") was FALSE for that run (it actually resolved to the real
    class). Now a real conflict: exactly ONE `duplicate_descriptor_name`
    row (never a second, contradicting `route_value_unrecoverable` row
    for the same anchor), and the mapped route genuinely falls back to
    the synthetic owner - the remedy claim is true again. Parametrized
    on block order - order-independence proven both ways."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "HalfBadServlet.java").write_text(
        "package com.acme.web;\nclass HalfBadServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    servlet_blocks = [
        "  <servlet>\n"
        "    <servlet-name>halfbad</servlet-name>\n"
        "    <servlet-class>com.acme.web.HalfBadServlet</servlet-class>\n"
        "  </servlet>\n",
        "  <servlet>\n"
        "    <servlet-name>halfbad</servlet-name>\n"
        "    <servlet-class><![CDATA[com.a]]>b<![CDATA[cme.Other]]></servlet-class>\n"
        "  </servlet>\n",
    ]
    if swap_order:
        servlet_blocks.reverse()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n" + "".join(servlet_blocks) +
        "  <servlet-mapping>\n"
        "    <servlet-name>halfbad</servlet-name>\n"
        "    <url-pattern>/halfbad/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    # Exactly one row for this anchor - never a second, contradicting
    # route_value_unrecoverable row alongside the conflict row.
    matching_conflict = [
        p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_descriptor_name"]
    matching_unrecoverable = [
        p for p in problems_doc["problems"] if p["reason_code"] == "route_value_unrecoverable"]
    assert len(matching_conflict) == 1
    assert matching_unrecoverable == []
    assert "com.acme.web.HalfBadServlet" in matching_conflict[0]["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/halfbad/*")
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    half_bad_unit = next(u for u in modules_doc["units"] if u["display_name"] == "HalfBadServlet")
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]
    assert entry_point["owning_unit_id"] != half_bad_unit["unit_id"]


def test_run_scan_a_benign_duplicate_servlet_declaration_collapses_silently(
    java_repo: Path,
) -> None:
    """Companion control (F1's own JUDGE call): two IDENTICAL
    declarations (same servlet-name, SAME class - the harmless merge-
    artifact twin) must not be treated as a conflict at all - no
    problem, no conflict_id, the mapping resolves normally to the one
    real class, exactly the single-declaration behavior."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "DispatcherServlet.java").write_text(
        "package com.acme.web;\nclass DispatcherServlet {\n}\n", encoding="utf-8")
    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <servlet-class>com.acme.web.DispatcherServlet</servlet-class>\n"
        "  </servlet>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(p["reason_code"] == "duplicate_descriptor_name" for p in problems_doc["problems"])
    # MICRO-ROUND 29b (reviewer-3's own matrix): the benign twin must
    # publish NO conflict row of either reason code - never
    # duplicate_qualified_name either, since no conflict_id is stamped
    # at all for an identical-class re-declaration.
    assert problems_doc["problems"] == []

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    servlet_unit = next(
        u for u in modules_doc["units"] if u["display_name"] == "DispatcherServlet")
    assert servlet_unit["conflict_id"] is None

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/api/*")
    assert entry_point["owning_unit_id"] == servlet_unit["unit_id"]


def test_run_scan_unsupported_invoke_shapes_publish_zero_instance_rows(
    java_repo: Path,
) -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F2 MAJOR, completeness):
    a class carrying BOTH a field-injected collaborator call and a
    constructor call - the two UNSUPPORTED_INVOKE_SHAPES members -
    publishes ZERO problems.json instance rows for either (deliberately,
    weighed against noise), ONE import edge (the real, resolvable
    dependency), and dependencies_resolved=satisfied - the exact shape
    the design doc's own (now-narrowed) capability-declaration sentence
    and java.py's own comment both describe. Regression coverage for the
    documentation-only fix: this behavior itself is unchanged by round
    30, only the sentence claiming otherwise was corrected."""
    import json

    controller_dir = java_repo / "src" / "main" / "java" / "p" / "web"
    controller_dir.mkdir(parents=True)
    (controller_dir / "OrderController.java").write_text(
        "package p.web;\n"
        "import p.OrderService;\n"
        "class OrderController {\n"
        "  private OrderService orderService;\n"
        "  void run() {\n"
        "    orderService.place(1);\n"
        "    throw new RuntimeException(\"boom\");\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    service_dir = java_repo / "src" / "main" / "java" / "p"
    (service_dir / "OrderService.java").write_text(
        "package p;\nclass OrderService {\n  void place(int id) {}\n}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    controller_problems = [
        p for p in problems_doc["problems"] if p.get("qualified_name") == "p.web.OrderController"]
    assert controller_problems == []

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    controller_component_unit = next(
        u for u in modules_doc["units"] if u.get("qualified_name") == "p.web.OrderController")
    controller_file_unit = next(
        u for u in modules_doc["units"]
        if u["kind"] == "file" and u["paths"] == ["src/main/java/p/web/OrderController.java"])

    # FIX ROUND 14 (CR10-1): an import edge attaches to its FILE unit,
    # never the declared type - the ONE real, resolvable dependency this
    # class has lives on the file unit's own from_unit_id.
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    controller_edges = [
        e for e in dependencies_doc["edges"]
        if e["from_unit_id"] == controller_file_unit["unit_id"]]
    assert len(controller_edges) == 1
    assert controller_edges[0]["relation"] == "import"
    assert controller_edges[0]["resolution_state"] == "resolved"

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    dependencies_resolved_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == controller_component_unit["unit_id"] and s["check"] == "dependencies_resolved")
    assert dependencies_resolved_signal["stored_status"] == "satisfied"


def test_run_scan_a_servlet_mapping_naming_an_undeclared_servlet_publishes_a_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F9c JUDGE, wrong-data): a
    <servlet-mapping> naming a <servlet-name> that NO <servlet> element
    declares at all (a ghost mapping, never merely a duplicate) used to
    fall through to the synthetic per-mapping owner with NO problem
    recorded at all - resolved+feature published, zero problems, on a
    complete run, for a real descriptor inconsistency. Now records its
    own `undeclared_descriptor_name` problem, naming the ghost name; the
    entry point still publishes via the same synthetic-owner fallback
    (unchanged), just no longer silently."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>ghost</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [
        p for p in problems_doc["problems"] if p["reason_code"] == "undeclared_descriptor_name"]
    assert len(matching) == 1
    assert "ghost" in matching[0]["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/api/*")
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]


def test_run_scan_a_filter_mapping_naming_an_undeclared_filter_publishes_a_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 29 (F9c JUDGE): the filter twin of the servlet ghost-
    mapping fix above."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_text(
        "<web-app>\n"
        "  <filter-mapping>\n"
        "    <filter-name>ghost</filter-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </filter-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [
        p for p in problems_doc["problems"] if p["reason_code"] == "undeclared_descriptor_name"]
    assert len(matching) == 1
    assert "ghost" in matching[0]["detail"]

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_point = next(e for e in features_doc["entry_points"] if e["name"] == "/api/*")
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_unit = next(
        u for u in modules_doc["units"] if u["kind"] == "file" and u["paths"] == ["WEB-INF/web.xml"])
    assert entry_point["owning_unit_id"] == web_xml_unit["unit_id"]


def test_run_scan_publishes_problems_json_and_it_reaches_the_report(
    java_repo: Path, monkeypatch,
) -> None:
    """B2 (cold-read, PR-B fix round 3): every problem record used to be
    computed then discarded - problems.json was never written, and a
    degraded run published no account of what degraded it. Force a
    problem by making the worker report one, and assert it survives all
    the way through to get_report()."""
    from agenttalk.comprehension import worker as workermod2

    real_run = workermod2.process_paths

    def _inject_a_problem(root, relative_paths, **_kwargs):
        result = real_run(root, relative_paths)
        result.problems.append(workermod2.WorkerProblem(
            reason_code="parse_failed", relative_path="src/main/java/p/App.java",
            detail="synthetic problem for the B2 regression test"))
        return result

    monkeypatch.setattr(scan_pipeline.worker, "run_sanitized_worker", _inject_a_problem)

    import json

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"
    assert (outcome.run_dir / "problems.json").exists()
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    # N3 (third cold read, fix round 5): problem_id/severity are now part
    # of the record shape (the design's own "stable ID... severity"
    # requirement) - checked individually rather than folded into one
    # dict-equality assertion, so this test still reads as "the synthetic
    # problem survived to problems.json" rather than as a schema pin.
    assert len(doc["problems"]) == 1
    problem = doc["problems"][0]
    assert problem["reason_code"] == "parse_failed"
    assert problem["path"] == "src/main/java/p/App.java"
    assert problem["detail"] == "synthetic problem for the B2 regression test"
    assert problem["severity"] == "warning"
    assert problem["problem_id"]

    report = scan_pipeline.get_report(java_repo)
    assert report["problems"] == doc["problems"]
    assert report["counts"]["problems"] == 1


def test_run_scan_over_a_jsp_estate_degrades_with_a_named_unsupported_language_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 14 (tenth cold read, CR10-5 JUDGE, completeness): the
    design names ``unsupported_language`` as a problem code and a
    ``degraded`` trigger ("part of the selected source is unsupported")
    - a run over a JSP/properties/Spring-XML/SQL estate used to publish
    complete with problem_count 0, contradicting that text. This is the
    real end-to-end path (no synthetic problem injection): a genuine
    ordinary Java project plus one real .jsp file on disk."""
    (java_repo / "index.jsp").write_text(
        "<%@ page language=\"java\" %>\n<html></html>\n", encoding="utf-8")

    import json

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    jsp_problems = [p for p in doc["problems"] if p["path"] == "index.jsp"]
    assert len(jsp_problems) == 1
    assert jsp_problems[0]["reason_code"] == "unsupported_language"
    assert jsp_problems[0]["severity"] == "warning"


def test_run_scan_a_utf16_java_file_degrades_instead_of_silently_vanishing(
    java_repo: Path,
) -> None:
    """FIX ROUND 18 (fourteenth cold read, F6 JUDGE, taken): mirrors the
    reader's own .cr14-enc shape - a UTF-16-encoded .java file (a legal
    javac input, genuinely present in legacy Windows-authored codebases)
    trips discovery's own NUL-byte binary sniff and used to be silently
    excluded (recorded only under excluded_roots's "binary" category),
    the run still reporting complete/zero problems even though real
    code went unread. Now degrades with a named problem, exactly like a
    tier-2 code-bearing file already does."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Legacy.java").write_bytes(
        "package p;\nclass Legacy {}\n".encode("utf-16"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert any(
        e["category"] == "binary" and e["path"].endswith("Legacy.java")
        for e in scan_doc["excluded_roots"]
    )

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    legacy_problems = [p for p in problems_doc["problems"] if p["path"].endswith("Legacy.java")]
    # FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-rule
    # VISIBILITY): a code-bearing binary-excluded file now ALSO poisons
    # externality run-wide (round 20's own M1+M2) - this file's own
    # binary_excluded_code_bearing_file problem is joined by a SECOND,
    # additive externality_suppressed record naming the same path as one
    # of the poison rule's own triggers, not replaced by it.
    assert len(legacy_problems) == 2
    reason_codes = {p["reason_code"] for p in legacy_problems}
    assert reason_codes == {"binary_excluded_code_bearing_file", "externality_suppressed"}


@pytest.mark.parametrize("basename", ["beans.xml", "struts-config.xml", "logback.xml"])
def test_run_scan_a_utf16_root_sniffed_xml_records_but_does_not_degrade(
    java_repo: Path, basename: str,
) -> None:
    """FIX ROUND 26b (reviewer-3 delta on `38a21f3`, item 2, R4 carry
    OVERTURNED - closed, wrong-data): a binary-excluded, non-adapter-
    handled .xml file (round 26's own basename widening only ever
    caught pom.xml/web.xml) used to vanish completely - complete, 0
    problems, no poison - while its UTF-8 twin would DEGRADE the run if
    it happened to be tier-2 code-bearing (beans.xml/struts-config.xml)
    - a wrong-data silence. FIXED AS THE HONEST MIDDLE GROUND: this run
    cannot decode the file to sniff its root element, so it cannot tell
    a tier-2 shape from a tier-3 one (logback.xml) either - recorded
    (visible, addressable) but never degrading, for ALL THREE basenames
    alike (the boundary case, logback.xml, gets the identical
    treatment - guessing toward degrading would brand every repo
    carrying an unreadable logback.xml).

    MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R2, wrong-data): the
    caveat published alongside this fix claims this binary-excluded
    twin gets "the same epistemics" as its encoding-undecodable
    sibling - MEASURED FALSE until now: the undecodable twin publishes
    a real unit (empty classification, six unknown readiness rows)
    while this one published no unit at all. Now publishes the
    identical unit-level form too - problems.json is unaffected
    (unchanged assertions below)."""
    import json

    (java_repo / basename).write_bytes("<beans><!-- café --></beans>\n".encode("utf-16"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert any(
        e["category"] == "binary" and e["path"] == basename
        for e in scan_doc["excluded_roots"]
    )

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["path"] == basename]
    assert len(matching) == 1
    assert matching[0]["reason_code"] == "binary_excluded_root_sniffed_xml"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    units = [u for u in modules_doc["units"] if u["paths"] == [basename]]
    assert len(units) == 1
    assert units[0]["classification"] == []
    assert units[0]["adapter_problem_reasons"] == ["binary_excluded_root_sniffed_xml"]

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    unit_signals = [s for s in readiness_doc["signals"] if s["unit_id"] == units[0]["unit_id"]]
    assert len(unit_signals) == 6
    assert all(s["stored_status"] == "unknown" for s in unit_signals)


def test_run_scan_publishes_modules_in_path_sorted_order_not_prepended(
    java_repo: Path,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F6 polish, wrong-data):
    micro-round 28b's own binary-excluded-root-sniffed-XML synthesized
    units are built in their OWN loop, BEFORE the main per-file loop -
    they landed PREPENDED to every other record, never interleaved in
    path order the way every other unit already is (the design's own
    publish-validation step names "deterministic ordering" as a real
    requirement). `z-config/logback.xml` sorts AFTER the default
    fixture's own `pom.xml`/`src/...` units alphabetically - it must
    NOT be first in modules.json's own units list."""
    import json

    (java_repo / "z-config").mkdir()
    (java_repo / "z-config" / "logback.xml").write_bytes(
        "<beans><!-- café --></beans>\n".encode("utf-16"))

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    unit_paths = [u["paths"][0] for u in modules_doc["units"]]
    assert unit_paths == sorted(unit_paths)
    assert unit_paths[0] != "z-config/logback.xml"


def test_run_scan_refuses_to_publish_modules_out_of_deterministic_order(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 29 (F6 polish): the publish-time guard - a future
    producer bug reintroducing non-deterministic module ordering must be
    refused at its own source, the same way a dangling reference already
    is (F2), rather than leaving "deterministic ordering" a design claim
    nothing actually checks."""
    from agenttalk.comprehension import modules_artifact as modulesmod

    real_build_modules = modulesmod.build_modules

    def _reverse_the_order(*args, **kwargs):
        return list(reversed(real_build_modules(*args, **kwargs)))

    monkeypatch.setattr(scan_pipeline.modules_artifact, "build_modules", _reverse_the_order)

    with pytest.raises(scan_pipeline.ComprehensionError, match="deterministic") as exc_info:
        scan_pipeline.run_scan(java_repo)

    # MICRO-ROUND 29b (JUDGE, note-only, chosen behavior): this refusal
    # fires after staging creation but before anything is written there -
    # staging.py's own Note 10 judges the resulting orphaned `.staging/`
    # directory DESIGNED, not a leak (bounded, self-clearing on the next
    # scan's own lock-acquisition reclaim) - so the directory is left in
    # place, never proactively deleted here, but the refusal message now
    # names the existing remedy rather than leaving it to be discovered
    # separately.
    assert "prune --staging" in str(exc_info.value)
    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    staging_entries = list(scan_pipeline.paths.staging_dir(comp_dir).iterdir())
    assert len(staging_entries) == 1
    assert [p.name for p in staging_entries[0].iterdir()] == ["owner.json"]


def test_run_scan_refuses_to_publish_a_problem_id_collision(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 36 (thirtieth cold read, F2 MAJOR, part (c)): the
    publish-time backstop - a future problem emitter that forgets a
    distinguishing datum the same way round 36's own F1/F2 reactor and
    coordinate shapes did must be refused at its own source, never
    silently published with an understated problem_count. Forces the
    condition directly (rather than needing a fresh emitter bug, since
    F1 already closed the two known reachable shapes) by monkeypatching
    the shared detector itself - the same "one predicate, two call
    sites" wiring the dangling-reference refusal above already
    establishes."""
    monkeypatch.setattr(
        scan_pipeline, "_problem_id_collisions", lambda problems: ["deadbeef"])

    with pytest.raises(scan_pipeline.ComprehensionError, match="problem_id"):
        scan_pipeline.run_scan(java_repo)


@pytest.mark.parametrize("id_attr", [
    "unit_id", "edge_id", "entry_point_id", "feature_id", "signal_id",
])
def test_run_scan_refuses_to_publish_an_id_family_collision(
    java_repo: Path, monkeypatch, id_attr: str,
) -> None:
    """FIX ROUND 38 (thirty-second cold read, F1 BLOCKER, part (c)): the
    problem_id-collision backstop above (round 36's F2), generalized -
    ``entry_point_id``'s own round-38 F1 blocker (two genuinely distinct
    entry-point records that fell back to the same synthetic file owner
    shared an identical id, before this round's own fix widened its hash
    input) proved this "byte-identical or refuse" invariant was never
    swept for any family but ``problems.json``. Every OTHER family
    (``unit_id``, ``edge_id``, ``feature_id``, ``signal_id``) must be
    refused the identical way. Forces the condition one family at a time
    by monkeypatching the shared generic detector to report a collision
    only for the family under test, the real detector otherwise -
    proving each family's own publish-time call site is actually wired,
    not merely that the shared function exists."""
    real_id_family_collisions = scan_pipeline._id_family_collisions
    target = id_attr

    def _fake(records, *, id_attr):
        if id_attr == target:
            return ["deadbeef"]
        return real_id_family_collisions(records, id_attr=id_attr)

    monkeypatch.setattr(scan_pipeline, "_id_family_collisions", _fake)

    with pytest.raises(scan_pipeline.ComprehensionError, match=f"{id_attr}\\(s\\)"):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_two_zero_route_jax_rs_classes_in_one_file_does_not_brick_the_scan(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 36b (reviewer-3 delta on `0d8d6c9`, THE COUPLING
    DEFECT - availability regression): the JAX-RS class-closer's own two
    emitters carried their distinguishing datum (the class) BESIDE the
    detail (`qualified_name`), not IN it - the two sites round 36's own
    F1 sweep missed. Round 36's own new problem_id-collision detector
    then correctly proved two genuinely distinct facts (two different
    @Path classes in the SAME ordinary, legal .java file) shared one id
    and hard-refused, converting a reporting gap into an availability
    bug: the scan bricked entirely (ComprehensionError, no run
    published) for a shape no existing test exercised. Fixed by naming
    the class in both closer details - the scan must complete, publish
    two distinct problems, with two distinct problem_ids."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Resources.java").write_text(
        "package p;\n"
        "import javax.ws.rs.GET;\n"
        "import javax.ws.rs.Path;\n"
        "\n"
        "@Path(\"/orders\")\n"
        "class OrderResource {\n"
        "    @GET\n"
        "    public void list() {}\n"
        "}\n"
        "\n"
        "@Path(\"/items\")\n"
        "class ItemResource {\n"
        "    @GET\n"
        "    public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    shape_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unsupported_entry_point_shape"]
    assert {p.get("qualified_name") for p in shape_problems} == {"p.OrderResource", "p.ItemResource"}
    assert len({p["problem_id"] for p in shape_problems}) == 2


def test_run_scan_two_listeners_on_one_minified_web_xml_line_does_not_brick_the_scan(
    java_repo: Path,
) -> None:
    """FIX ROUND 37 (thirty-first cold read, F1 BLOCKER - availability,
    .cr31-listener verbatim): round 36b fixed the two JAX-RS closer
    sites, but the reader's own AST sweep found NINETEEN more emitters
    whose detail's only discriminator is {line} while the true
    distinguishing datum (the class) sits beside it in qualified_name.
    Any two same-kind declarations sharing ONE SOURCE LINE - a minified/
    one-line web.xml with two <listener> elements, utterly ordinary
    Spring estate output - collided and bricked the scan entirely.
    Fixed structurally at digests.problem_id itself (qualified_name now
    an input) rather than per-site."""
    import json

    (java_repo / "src" / "main" / "webapp" / "WEB-INF").mkdir(parents=True)
    (java_repo / "src" / "main" / "webapp" / "WEB-INF" / "web.xml").write_text(
        '<web-app>'
        '<listener><listener-class>p.FooListener</listener-class></listener>'
        '<listener><listener-class>p.BarListener</listener-class></listener>'
        '</web-app>\n',
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    listener_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unsupported_entry_point_shape"
        and p.get("qualified_name") in {"p.FooListener", "p.BarListener"}]
    assert {p["qualified_name"] for p in listener_problems} == {"p.FooListener", "p.BarListener"}
    assert len({p["problem_id"] for p in listener_problems}) == 2


def test_run_scan_two_startup_only_servlets_on_one_line_does_not_brick_the_scan(
    java_repo: Path,
) -> None:
    """FIX ROUND 37 (F1 BLOCKER - availability, .cr31-startup2 verbatim):
    the identical coupling defect at the @WebServlet startup-only-
    registration site (no value/urlPatterns attribute at all) - two
    startup-only servlet classes declared on ONE source line share an
    identical line-only detail, previously colliding on problem_id."""
    import json

    (java_repo / "src" / "main" / "java" / "p").mkdir(parents=True, exist_ok=True)
    (java_repo / "src" / "main" / "java" / "p" / "Startups.java").write_text(
        "package p;\n"
        "import javax.servlet.annotation.WebServlet;\n"
        '@WebServlet(name = "foo") class FooStartup {} '
        '@WebServlet(name = "bar") class BarStartup {}\n',
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    startup_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unsupported_entry_point_shape"
        and p.get("qualified_name") in {"p.FooStartup", "p.BarStartup"}]
    assert {p["qualified_name"] for p in startup_problems} == {"p.FooStartup", "p.BarStartup"}
    assert len({p["problem_id"] for p in startup_problems}) == 2


def test_run_scan_two_scheduled_methods_on_one_line_in_different_classes_does_not_brick(
    java_repo: Path,
) -> None:
    """FIX ROUND 37 (F1 BLOCKER - availability, .cr31-collide verbatim):
    the identical coupling defect in the _UNENROLLED_ENTRY_POINT_
    FAMILIES class-closer loop (@Scheduled/@KafkaListener/...) - two
    @Scheduled methods in two DIFFERENT classes, declared on ONE source
    line, share an identical line-only detail, previously colliding on
    problem_id."""
    import json

    (java_repo / "src" / "main" / "java" / "p").mkdir(parents=True, exist_ok=True)
    (java_repo / "src" / "main" / "java" / "p" / "Jobs.java").write_text(
        "package p;\n"
        "class FooJob { @Scheduled void run() {} } "
        "class BarJob { @Scheduled void run() {} }\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    scheduled_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "unsupported_entry_point_shape"
        and p.get("qualified_name") in {"p.FooJob", "p.BarJob"}]
    assert {p["qualified_name"] for p in scheduled_problems} == {"p.FooJob", "p.BarJob"}
    assert len({p["problem_id"] for p in scheduled_problems}) == 2


def test_run_scan_an_encoding_undecodable_root_sniffed_xml_publishes_no_classification(
    java_repo: Path,
) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F2 BLOCKER, round-27
    regression, wrong-data, end to end): the one-byte reader repro -
    a Latin-1-encoded beans.xml (a lone high-bit byte, no NUL bytes, so
    it passes discovery's binary sniff and only fails to decode at the
    worker's own root-sniff site) records `encoding_undecodable` and
    stays non-degrading (round 27's own F3 ruling, unchanged), but must
    publish an EMPTY classification - not the confident `infrastructure`
    round 27's own fix wrongly fed it."""
    import json

    (java_repo / "beans.xml").write_bytes("<beans><!-- café --></beans>\n".encode("latin-1"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["path"] == "beans.xml"]
    assert len(matching) == 1
    assert matching[0]["reason_code"] == "encoding_undecodable"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    beans_units = [u for u in modules_doc["units"] if u["paths"] == ["beans.xml"]]
    assert len(beans_units) == 1
    assert beans_units[0]["classification"] == []


def test_run_scan_a_readable_root_sniffed_xml_still_gets_a_decided_classification(
    java_repo: Path,
) -> None:
    """Companion control, one-byte pair with the test above: the SAME
    beans.xml content, genuinely UTF-8-decodable this time, must be
    entirely unaffected by the F2 fix - a real, decided `production`
    classification (this is genuinely unmodeled application code, tier-
    2 code-bearing, so the run also degrades)."""
    import json

    (java_repo / "beans.xml").write_bytes("<beans><!-- café --></beans>\n".encode("utf-8"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    beans_units = [u for u in modules_doc["units"] if u["paths"] == ["beans.xml"]]
    assert len(beans_units) == 1
    assert beans_units[0]["classification"] == ["production"]


def test_run_scan_a_utf16_pom_xml_still_degrades_unaffected_by_the_root_sniffed_xml_carve_out(
    java_repo: Path,
) -> None:
    """Companion control: round 26's own F4 fix (pom.xml/web.xml always
    degrade when binary-excluded) must be unaffected by this round's own
    new, deliberately-non-degrading root-sniffed-xml problem - the two
    predicates are mutually exclusive by construction
    (`is_a_root_sniffed_xml_extension` excludes both adapter-handled
    basenames), verified here end to end rather than merely assumed.

    FIX ROUND 31 (twenty-seventh cold read, F3 MINOR, completeness): a
    binary/UTF-16-excluded pom.xml/web.xml previously got NO modules.json
    unit at all - the MORE migration-material file (a build/routing
    descriptor, always code-bearing by definition) was the unaddressable
    one, while a UTF-16 logback.xml (not adapter-handled) already got a
    real unit (round 26b/micro-round 28b). Now published: empty
    classification, the SAME already-recorded binary_excluded_code_
    bearing_file reason in adapter_problem_reasons, and six honest
    unknown readiness rows - never a confident guess over a file this
    run admits it never read."""
    import json

    (java_repo / "pom.xml").write_bytes(
        "<project><groupId>g</groupId><artifactId>a</artifactId></project>\n"
        .encode("utf-16"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["path"] == "pom.xml"]
    reason_codes = {p["reason_code"] for p in matching}
    assert "binary_excluded_code_bearing_file" in reason_codes
    assert "binary_excluded_root_sniffed_xml" not in reason_codes

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    pom_units = [u for u in modules_doc["units"] if u["paths"] == ["pom.xml"]]
    assert len(pom_units) == 1
    assert pom_units[0]["classification"] == []
    assert "binary_excluded_code_bearing_file" in pom_units[0]["adapter_problem_reasons"]

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    pom_signals = [s for s in readiness_doc["signals"] if s["unit_id"] == pom_units[0]["unit_id"]]
    assert len(pom_signals) == 6
    assert all(s["stored_status"] == "unknown" for s in pom_signals)


def test_run_scan_a_binary_excluded_web_xml_also_gets_its_own_unit(java_repo: Path) -> None:
    """FIX ROUND 31 (twenty-seventh cold read, F3 MINOR, completeness):
    the web.xml twin of the pom.xml test above - .cr27-enc2 verbatim.
    A binary-excluded web.xml is the MORE migration-material file (a
    routing descriptor) yet was the unaddressable one; a UTF-16
    logback.xml (not adapter-handled) already got a unit. Both must
    now be true at once, in the SAME run - the fix must not regress the
    already-working non-adapter-handled case while closing this one."""
    import json

    (java_repo / "WEB-INF").mkdir()
    (java_repo / "WEB-INF" / "web.xml").write_bytes(
        "<web-app></web-app>\n".encode("utf-16"))
    (java_repo / "logback.xml").write_bytes("<configuration></configuration>\n".encode("utf-16"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    web_xml_units = [u for u in modules_doc["units"] if u["paths"] == ["WEB-INF/web.xml"]]
    assert len(web_xml_units) == 1
    assert web_xml_units[0]["classification"] == []
    assert "binary_excluded_code_bearing_file" in web_xml_units[0]["adapter_problem_reasons"]

    logback_units = [u for u in modules_doc["units"] if u["paths"] == ["logback.xml"]]
    assert len(logback_units) == 1
    assert logback_units[0]["classification"] == []
    assert "binary_excluded_root_sniffed_xml" in logback_units[0]["adapter_problem_reasons"]


def test_run_scan_a_binary_excluded_java_file_also_gets_its_own_unit(java_repo: Path) -> None:
    """FIX ROUND 32 (twenty-eighth cold read, F8 LOW, JUDGE - taken):
    round 31's own F3 fix closed the gap for pom.xml/web.xml specifically
    (via a narrow adapter-handled-XML-basename predicate) - a binary-
    excluded .java file got the identical real, DEGRADING
    binary_excluded_code_bearing_file problem (round 18's own F6) but
    STILL no synthesized unit at all, the SAME epistemic state ("this run
    never read this file") with different visibility from its web.xml
    twin. Widened to the full code-bearing predicate
    (worker.is_a_code_bearing_extension_worth_degrading_when_silently_
    excluded) so a .java (and, by the same widened predicate, any tier-2
    _DEGRADING_CODE_EXTENSIONS shape) now gets the same treatment."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme"
    web_dir.mkdir(parents=True)
    (web_dir / "Legacy.java").write_bytes(
        "package com.acme;\nclass Legacy {\n}\n".encode("utf-16"))

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [
        p for p in problems_doc["problems"] if p["path"] == "src/main/java/com/acme/Legacy.java"]
    reason_codes = {p["reason_code"] for p in matching}
    assert "binary_excluded_code_bearing_file" in reason_codes

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    legacy_units = [
        u for u in modules_doc["units"] if u["paths"] == ["src/main/java/com/acme/Legacy.java"]]
    assert len(legacy_units) == 1
    assert legacy_units[0]["classification"] == []
    assert "binary_excluded_code_bearing_file" in legacy_units[0]["adapter_problem_reasons"]

    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    legacy_signals = [
        s for s in readiness_doc["signals"] if s["unit_id"] == legacy_units[0]["unit_id"]]
    assert len(legacy_signals) == 6
    assert all(s["stored_status"] == "unknown" for s in legacy_signals)


def test_run_scan_a_utf16_gitmodules_degrades_with_a_parse_failed_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 26b (reviewer-3 delta on `38a21f3`, item 1, wrong-data):
    a UTF-16-encoded .gitmodules used to decode with errors="replace"
    into garbled text that simply matched no "path = ..." line - an
    EMPTY boundary set via the ordinary success path (not the existing
    unreadable-file problem path), so a real submodule's own foreign
    source silently walked straight into the fingerprint AND this run's
    own inventory as first-party units, on a complete/zero-problem run.
    A named, degrading problem is recorded instead - the submodule
    directory still cannot be excluded, but the run is no longer
    silently claiming completeness over it.

    FIX ROUND 47 (forty-first cold read, B1 BLOCKER): the parse now
    delegates entirely to a real `git config -f` subprocess (see
    `_submodule_boundary_paths`'s own docstring) - git's own config
    parser rejects a UTF-16-encoded file outright (`fatal: bad config
    line 1`, exit 128), landing in the SAME generic `parse_failed`
    branch every other git-invocation failure does, never a dedicated
    `encoding_undecodable` reason this producer no longer separately
    detects (there is no manual decode step left to have its own
    undecodable case at all)."""
    import json

    (java_repo / ".gitmodules").write_bytes(
        '[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib.git\n'
        .encode("utf-16"))
    submodule_dir = java_repo / "lib"
    submodule_dir.mkdir()
    (submodule_dir / "Foreign.java").write_text(
        "package foreign;\nclass Foreign {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["path"] == ".gitmodules"]
    assert len(matching) == 1
    assert matching[0]["reason_code"] == "parse_failed"


def test_run_scan_a_utf8_gitmodules_stays_the_existing_clean_boundary_behavior(
    java_repo: Path,
) -> None:
    """Companion control: an ordinary UTF-8 .gitmodules must be
    unaffected by the new encoding guard - the submodule boundary is
    still correctly identified and excluded, run stays complete."""
    import json

    (java_repo / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib.git\n',
        encoding="utf-8")
    submodule_dir = java_repo / "lib"
    submodule_dir.mkdir()
    (submodule_dir / "Foreign.java").write_text(
        "package foreign;\nclass Foreign {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert any(b["path"] == "lib" for b in scan_doc["boundaries"])
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(p["path"] == ".gitmodules" for p in problems_doc["problems"])


def test_run_scan_a_latin1_java_file_degrades_and_its_importer_stays_unresolved(
    java_repo: Path,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, CR17-4 MAJOR, wrong-data):
    mirrors the reader's own .cr17-enc2 pair, end to end - a Latin-1/
    CP1252-encoded .java file (a real, common European-legacy-estate
    shape) used to decode with a fabricated, truncated qualified name
    (the U+FFFD replacement character falls outside \\w), publishing a
    confident external claim for its importer over what is actually
    in-repo source, on a complete/zero-problem run. Now: the run
    degrades, a named problem is recorded, no fabricated unit exists at
    all, and the importer correctly stays unresolved rather than a
    false confident external claim."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Café.java").write_bytes(
        "package p;\npublic class Café {}\n".encode("latin-1"))
    (java_repo / "src" / "main" / "java" / "p" / "other").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "p" / "other" / "Consumer.java").write_text(
        "package p.other;\nimport p.Café;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    cafe_problems = [p for p in problems_doc["problems"] if "Café" in p["path"]]
    assert len(cafe_problems) == 1
    assert cafe_problems[0]["reason_code"] == "encoding_undecodable"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    # No COMPONENT-kind unit at all for this file (the fabricated-name
    # shape) - only its ordinary file-kind record, unaffected either way
    # (every file gets one regardless of whether adapter analysis ran).
    assert not any(u["kind"] == "component" and "Café" in (u.get("paths") or [""])[0]
                   for u in modules_doc["units"])

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and (r.get("target_unresolved") or "").endswith("Café"))
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None


def test_run_scan_a_discovery_excluded_oversized_java_file_lets_its_importer_stay_unresolved(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, the reader's own LOW-
    CONFIDENCE flag, wrong-data): a file discovery excludes OUTRIGHT at
    its own layer (the per-file byte cap here; an unreadable file's
    stat()/read() failure and the entry-count cap are the same shape)
    never reaches the worker at all - no unit, no worker-level problem
    either. Before this fix ``degraded_paths`` consulted ONLY worker-
    level problems, so an importer of this file's declared type fell
    through to a false confident external claim over what is genuinely
    in-repo (merely oversized) source. Now: the run degrades, the
    oversized file's own path is named in a ``resource_limit`` problem,
    no component unit exists for it, and the importer correctly stays
    unresolved rather than a false confident external claim."""
    import json

    from agenttalk.comprehension import discovery as discoverymod

    monkeypatch.setattr(discoverymod, "MAX_PER_FILE_BYTES", 500)
    padding = "// padding to exceed the per-file byte cap\n" * 20
    (java_repo / "src" / "main" / "java" / "p" / "Big.java").write_text(
        f"package p;\n{padding}public class Big {{}}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "other").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "p" / "other" / "Consumer.java").write_text(
        "package p.other;\nimport p.Big;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    big_problems = [p for p in problems_doc["problems"] if p["path"].endswith("Big.java")]
    assert big_problems
    assert any(p["reason_code"] == "resource_limit" for p in big_problems)

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    assert not any(u["kind"] == "component" and "Big" in (u.get("paths") or [""])[0]
                   for u in modules_doc["units"])

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and (r.get("target_unresolved") or "").endswith("Big"))
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None


def test_run_scan_a_bom_prefixed_java_file_lets_an_importer_resolve_internal(
    java_repo: Path,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, B1 BLOCKER, wrong-data): mirrors
    the reader's own .cr16-h pair, end to end - a UTF-8-BOM-prefixed
    .java file (CRLF line endings too) used to publish a WRONG qualified
    name (the bare simple name, its package lost to the BOM defeating
    _PACKAGE_RE's own anchor), so an importer of the real type published
    a confident EXTERNAL claim for genuine in-repo source. Must now
    resolve the real package and let an importer resolve INTERNAL."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Legacy.java").write_bytes(
        ("﻿" + "package p;\r\nclass Legacy {}\r\n").encode("utf-8"))
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport p.Legacy;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    assert any(u.get("qualified_name") == "p.Legacy" for u in modules_doc["units"])
    legacy_unit_id = next(
        u["unit_id"] for u in modules_doc["units"] if u.get("qualified_name") == "p.Legacy")
    import_edge = next(r for r in dependencies_doc["edges"] if r["relation"] == "import")
    assert import_edge["resolution_state"] == "resolved"
    assert import_edge.get("target_external") is None
    assert import_edge.get("target_unit_id") == legacy_unit_id


def test_run_scan_a_genuinely_binary_file_still_stays_silent(java_repo: Path) -> None:
    """Companion negative case - a genuinely binary file (an extension
    on neither the adapter-handled nor the tier-2 code-bearing list)
    must stay exactly as silent as it is today: excluded, recorded as
    a bare exclusion count, never a problem, never a degraded run."""
    (java_repo / "photo.png").write_bytes(b"\x00\x01\x02binarydata")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    import json

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert problems_doc["problems"] == []


#: FIX ROUND 14b (reviewer-3's ratified CR10-5 split): the reviewer's own
#: seven-single-file-repo battery - an otherwise entirely healthy
#: java+pom repo plus exactly one more file of each named kind. Before
#: this round, ALL SEVEN degraded (the blanket unsupported_language ->
#: degrade rule); the ratified rule keeps only JSP/SQL/Spring-bean-XML
#: degrading - the reviewer's own reader test ("would a migration reader
#: say the inventory missed something they NEEDED") is true of those
#: three and false of the other four.
_CR10_5B_SEVEN_REPO_BATTERY = [
    ("logback.xml", "<configuration><root level=\"INFO\"/></configuration>", False),
    ("checkstyle.xml", "<module name=\"Checker\"></module>", False),
    ("messages.properties", "greeting=hello\n", False),
    ("application.properties", "server.port=8080\n", False),
    ("applicationContext.xml", "<beans><bean id=\"x\" class=\"y\"/></beans>", True),
    ("index.jsp", "<%@ page language=\"java\" %>\n<html></html>\n", True),
    ("schema.sql", "CREATE TABLE t (id INT);\n", True),
]


@pytest.mark.parametrize("filename,content,expect_degraded", _CR10_5B_SEVEN_REPO_BATTERY)
def test_run_scan_seven_repo_battery_degrades_only_the_code_bearing_kinds(
    java_repo: Path, filename: str, content: str, expect_degraded: bool,
) -> None:
    """FIX ROUND 14b (reviewer-3's ratified CR10-5 split, its own
    measurement): every kind is still recorded as a visible
    unsupported_language problem - only whether the RUN degrades varies
    by kind."""
    import json

    (java_repo / filename).write_text(content, encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == ("degraded" if expect_degraded else "complete")
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == filename]
    assert len(matches) == 1
    assert matches[0]["reason_code"] == "unsupported_language"


def test_run_scan_a_minimal_spring_repo_with_only_a_properties_file_stays_complete(
    java_repo: Path,
) -> None:
    """FIX ROUND 14b: the reviewer's own minimal-Spring-repo shape (class
    + pom + README + application.properties) used to scan DEGRADED
    before this round's split - deleting the properties file made it
    complete, which is exactly backwards for a healthy repo. The
    properties file is now recorded (visible), never degrading."""
    import json

    (java_repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (java_repo / "application.properties").write_text("server.port=8080\n", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == "application.properties"]
    assert len(matches) == 1
    assert matches[0]["reason_code"] == "unsupported_language"


def test_run_scan_a_bean_xml_estate_still_degrades(java_repo: Path) -> None:
    """FIX ROUND 14b: Spring bean XML is code-bearing configuration a
    migration reader would call "missed" - it keeps degrading the run,
    unlike ordinary tooling XML."""
    (java_repo / "applicationContext.xml").write_text(
        "<beans><bean id=\"x\" class=\"y\"/></beans>", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"


def test_run_scan_an_unreadable_root_element_xml_estate_stays_complete(java_repo: Path) -> None:
    """FIX ROUND 14b: when the root-element sniff cannot determine a
    root at all, this fails toward the SAFE side (record-only) rather
    than guessing a code-bearing shape - the run stays complete."""
    import json

    (java_repo / "mystery.xml").write_text("not actually xml at all", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == "mystery.xml"]
    assert len(matches) == 1
    assert "could not be determined" in matches[0]["detail"]


def test_run_scan_a_fake_beans_tag_inside_a_processing_instruction_stays_complete(
    java_repo: Path,
) -> None:
    """FIX ROUND 14c (reviewer-3's own real-file repro, pulled forward):
    a well-formed XML file whose root is <cfg> - a literal "<beans"
    living inside a processing instruction's raw content must never
    publish a FALSE root-element detail (asserting Spring bean XML for
    a file that never declared one) and must never degrade the run
    over it."""
    import json

    (java_repo / "weird.xml").write_text("<?custom-pi <beans> ?>\n<cfg/>\n", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == "weird.xml"]
    assert len(matches) == 1
    assert "beans" not in matches[0]["detail"]
    assert "cfg" in matches[0]["detail"]


def test_run_scan_a_fake_beans_tag_inside_a_doctype_entity_value_stays_complete(
    java_repo: Path,
) -> None:
    """FIX ROUND 14c: same false-detail hazard, via a DOCTYPE internal
    subset's <!ENTITY> replacement text instead of a PI."""
    import json

    (java_repo / "weird2.xml").write_text(
        "<!DOCTYPE cfg [\n"
        "  <!ENTITY foo \"<beans>fake</beans>\">\n"
        "]>\n"
        "<cfg/>\n",
        encoding="utf-8",
    )
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == "weird2.xml"]
    assert len(matches) == 1
    assert "beans" not in matches[0]["detail"]
    assert "cfg" in matches[0]["detail"]


def test_run_scan_an_unterminated_comment_containing_a_fake_beans_tag_stays_complete(
    java_repo: Path,
) -> None:
    """FIX ROUND 14c: malformed input (an unterminated comment) must
    fail toward record-only, never a guessed degradation, even when the
    unclosed comment happens to contain a literal <beans."""
    import json

    (java_repo / "broken.xml").write_text(
        "<!-- unterminated comment containing <beans\n<cfg/>\n", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == "broken.xml"]
    assert len(matches) == 1
    assert "could not be determined" in matches[0]["detail"]


def test_run_scan_an_uppercase_beans_root_stays_complete(java_repo: Path) -> None:
    """FIX ROUND 14c (reviewer-3's micro-note): XML element names are
    case-sensitive - <BEANS> is a DIFFERENT name from Spring's own
    lowercase <beans> and must never be folded into a match it never
    earned."""
    (java_repo / "shout.xml").write_text(
        "<BEANS><BEAN id=\"x\" class=\"y\"/></BEANS>", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"


def test_run_scan_the_spring_dtd_form_beans_file_still_degrades(java_repo: Path) -> None:
    """FIX ROUND 14c: the DOCTYPE blanking must not blank past the
    doctype into the real root - Spring's own classic DTD-form beans
    file (a real, common shape) is the regression that proves it."""
    (java_repo / "legacy-context.xml").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE beans PUBLIC \"-//SPRING//DTD BEAN 2.0//EN\" "
        "\"http://www.springframework.org/dtd/spring-beans-2.0.dtd\">\n"
        "<beans><bean id=\"x\" class=\"y\"/></beans>\n",
        encoding="utf-8",
    )
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"


def test_run_scan_a_malformed_java_file_degrades_with_a_problem_and_unknown_readiness(
    java_repo: Path,
) -> None:
    """FIX ROUND 15 (eleventh cold read, F5 MAJOR, wrong-data, cr11-fx10
    verbatim): genuinely malformed Java (an unterminated char literal)
    made the sanitizer blank the rest of the file silently - the run
    published complete/0 problems, and this file's own
    source_understood incorrectly reported satisfied (PathUtil alone
    keeps units non-empty, so the zero-types guard never fires). Real
    end-to-end path: a genuine ordinary Java project plus one real
    malformed file on disk."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Mixed.java").write_text(
        "package p;\n"
        "class PathUtil {\n"
        "  char bad = '\n"
        "class FileController {\n"
        '  @GetMapping("/one") void a() {}\n'
        "}\n",
        encoding="utf-8",
    )
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matches = [p for p in doc["problems"] if p["path"] == "src/main/java/p/Mixed.java"]
    assert len(matches) == 1
    assert matches[0]["reason_code"] == "parse_failed"
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    path_util_unit = next(u for u in modules_doc["units"] if u.get("qualified_name") == "p.PathUtil"
                           or u["display_name"] == "PathUtil")
    source_understood = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == path_util_unit["unit_id"] and s["check"] == "source_understood"
    )
    assert source_understood["stored_status"] == "unknown"


def test_worker_problem_reason_by_path_joins_sorted_unique_reasons_for_one_path(
    java_repo: Path, monkeypatch,
) -> None:
    """N3 (fifth cold read, fix round 8): a plain dict comprehension over
    the worker's own problem list was LAST-WINS for a path with more
    than one recorded problem - whichever happened to be listed last
    silently discarded every earlier reason for that same path, with no
    ordering guarantee. A genuinely unrecognized-content .java file
    already organically records "no_types_extracted" (round 8's own
    BLOCKER 1b) - a SECOND, synthetic problem is injected for that SAME
    path in deliberately non-alphabetical order.

    MINOR 5 (sixth cold read, fix round 9): round 8's own fix joined
    both reasons into ONE compound string and published it as
    adapter_problem_reason - a value outside the closed, enumerated
    reason-code vocabulary. adapter_problem_reason must now stay a
    single enumerated value (the first, sorted); the full
    sorted-deduplicated list, still lossless, publishes separately as
    adapter_problem_reasons."""
    import json

    from agenttalk.comprehension import worker as workermod2

    (java_repo / "src" / "main" / "java" / "p" / "Garbage.java").write_text(
        "package p;\nfoo bar baz;\n", encoding="utf-8")

    real_run = workermod2.process_paths

    def _inject_a_second_problem_for_the_same_path(root, relative_paths, **_kwargs):
        result = real_run(root, relative_paths)
        result.problems.append(workermod2.WorkerProblem(
            reason_code="resource_limit", relative_path="src/main/java/p/Garbage.java",
            detail="synthetic second problem for the same path"))
        return result

    monkeypatch.setattr(
        scan_pipeline.worker, "run_sanitized_worker", _inject_a_second_problem_for_the_same_path)

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    garbage_unit = next(
        u for u in modules_doc["units"] if u["paths"] == ["src/main/java/p/Garbage.java"])
    assert garbage_unit["adapter_problem_reason"] == "no_types_extracted"
    assert garbage_unit["adapter_problem_reasons"] == ["no_types_extracted", "resource_limit"]


def test_run_scan_degrades_and_reports_unknown_for_a_java_file_with_no_recognized_declaration(
    java_repo: Path,
) -> None:
    """BLOCKER 1b (fifth cold read, fix round 8), end to end: a .java
    file whose parse succeeds but extracts zero declared types used to
    publish status:complete, problem_count:0, and readiness
    source_understood:satisfied - positive evidence for a file this
    adapter never actually understood. Reproduced with genuinely
    unrecognized top-level content (not a comment, not an import, not
    any known declaration keyword) - must now degrade the scan, publish
    an explicit problem, and report source_understood unknown for that
    file's own unit."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Garbage.java").write_text(
        "package p;\nfoo bar baz;\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "no_types_extracted"]
    assert len(matching) == 1
    assert matching[0]["path"] == "src/main/java/p/Garbage.java"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    garbage_unit = next(
        u for u in modules_doc["units"] if u["paths"] == ["src/main/java/p/Garbage.java"])
    assert garbage_unit["adapter_problem_reason"] == "no_types_extracted"


def test_run_scan_publishes_no_route_claims_from_a_zero_type_java_file(java_repo: Path) -> None:
    """BLOCKER (sixth cold read, fix round 9), end to end: a file that
    degrades honestly (zero units, no_types_extracted, degraded) must
    not ALSO publish a route edge/entry point attributed to a
    synthesized owner. Reproduced with valid, unicode-escaped-brace Java
    source (the language decodes \\uXXXX escapes before lexing; this
    adapter's sanitizer does not, so its own brace-matching never finds
    the type's body at all) - the pre-fix behavior published the class-
    level route prefix and the method's own route despite zero units.

    ROUND 9b (honesty tightening): the original version of this
    assertion checked for zero route edges/entry points RUN-WIDE - a
    multi-file fixture with even one OTHER, legitimately-routed file
    could either mask a real per-file leak (if that file's own routes
    happened to also be absent) or fail this test for an unrelated
    reason (if it had routes of its own). Scoped instead to what the
    leak actually attaches to: a zero-type file still gets its own
    default FILE-kind unit in modules.json (every enumerated file does),
    and _enclosing_qualified_name's synthesized fallback resolves to
    exactly that same file unit - NOT a dangling/unknown one, so a
    "no edge references an unknown unit" check would not have caught
    this. The scoped check instead asserts no edge/entry point is
    attributed to THIS file's own unit_id specifically, regardless of
    what any other file in the fixture happens to contain."""
    import json

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
    (java_repo / "src" / "main" / "java" / "p" / "Controller2.java").write_text(
        src, encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [p for p in problems_doc["problems"] if p["reason_code"] == "no_types_extracted"]
    assert any(p["path"] == "src/main/java/p/Controller2.java" for p in matching)

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    zero_type_file_unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["paths"] == ["src/main/java/p/Controller2.java"])

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    edges_from_the_zero_type_file = [
        e for e in dependencies_doc["edges"] if e["from_unit_id"] == zero_type_file_unit_id]
    assert edges_from_the_zero_type_file == []

    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    entry_points_owned_by_the_zero_type_file = [
        e for e in features_doc["entry_points"] if e["owning_unit_id"] == zero_type_file_unit_id]
    assert entry_points_owned_by_the_zero_type_file == []


def test_run_scan_does_not_flag_package_info_java_as_a_type_extraction_problem(
    java_repo: Path,
) -> None:
    """The legitimate typeless case, end to end - package-info.java, even
    with its own package-level annotation, must never be reported as
    source_understood unknown via the new no_types_extracted problem."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "package-info.java").write_text(
        "/**\n * Javadoc.\n */\n@Deprecated\npackage p;\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not [p for p in problems_doc["problems"] if p["reason_code"] == "no_types_extracted"]

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    package_info_unit = next(
        u for u in modules_doc["units"]
        if u["paths"] == ["src/main/java/p/package-info.java"])
    assert package_info_unit["adapter_problem_reason"] is None


def test_run_scan_an_escaped_comment_delimiter_degrades_and_reports_unknown_end_to_end(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, M5, judged - detect-and-
    degrade, end to end): a real file containing a \\uXXXX escape that
    decodes to a structural character degrades the run and reports
    source_understood unknown for that unit - visible, never silently
    trusted either way."""
    import json

    backslash = chr(92)
    escaped_comment_open = backslash + "u002F" + backslash + "u002A"
    (java_repo / "src" / "main" / "java" / "p" / "Real.java").write_text(
        "package p;\n"
        "public class Real {\n"
        "  " + escaped_comment_open + " a real compiler reads this as a comment */\n"
        "  void m() {}\n"
        "}\n",
        encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "source_uses_structural_unicode_escapes"]
    assert len(matching) == 1
    assert matching[0]["path"] == "src/main/java/p/Real.java"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    real_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Real")
    source_understood = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == real_unit["unit_id"] and s["check"] == "source_understood")
    assert source_understood["stored_status"] == "unknown"


def test_run_scan_does_not_flag_module_info_java_as_a_type_extraction_problem(
    java_repo: Path,
) -> None:
    """MAJOR 2 (sixth cold read, fix round 9), end to end: module-info.java
    must never flip an otherwise-clean run to degraded via the new
    no_types_extracted problem - it legitimately declares a `module`
    block, not a class/interface/enum/record."""
    import json

    (java_repo / "src" / "main" / "java" / "module-info.java").write_text(
        "module com.acme.app {\n    requires java.base;\n}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not [p for p in problems_doc["problems"] if p["reason_code"] == "no_types_extracted"]

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    module_info_unit = next(
        u for u in modules_doc["units"]
        if u["paths"] == ["src/main/java/module-info.java"])
    assert module_info_unit["adapter_problem_reason"] is None


def test_run_scan_does_not_flag_a_route_annotation_on_an_annotation_type(
    java_repo: Path,
) -> None:
    """Round 10b (reviewer-3 delta on round 10), end to end: a route
    annotation stacked on an `@interface` declaration - the documented
    Spring composed-annotation idiom Spring's own verb annotations are
    themselves defined with - must never flip an otherwise-clean run to
    degraded via the new route_annotation_unassociated problem. The run
    stays complete and problem-free."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "GetMapping2.java").write_text(
        "package p;\n\n"
        "@Target(java.lang.annotation.ElementType.METHOD)\n"
        "@Retention(java.lang.annotation.RetentionPolicy.RUNTIME)\n"
        "@RequestMapping(method = RequestMethod.GET)\n"
        "public @interface GetMapping2 {\n"
        '    String value() default "";\n'
        "}\n",
        encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "route_annotation_unassociated"
    ]

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    get_mapping_unit = next(
        u for u in modules_doc["units"]
        if u["paths"] == ["src/main/java/p/GetMapping2.java"])
    assert get_mapping_unit["adapter_problem_reason"] is None


def test_scan_json_record_counts_includes_itself(java_repo: Path) -> None:
    """N6-record_counts (cold-read, PR-B fix round 3): scan.json's own
    record_counts field must count scan.json itself (always exactly 1) -
    it previously only gained that entry in the in-memory dict AFTER
    scan.json was already written to disk, so the PUBLISHED document's
    own record_counts disagreed with what ceilings.py actually enforced
    (post-mutation, one entry richer)."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert doc["record_counts"]["scan.json"] == 1
    status = scan_pipeline.get_status(java_repo)
    assert status["record_counts"]["scan.json"] == 1


def test_scan_json_records_the_privacy_disposition(java_repo: Path) -> None:
    """M5 (cold-read, PR-B fix round 3): the privacy disposition this run
    acted under used to live only in scan.lock, deleted at release - the
    audit trail the attended override exists to create did not survive
    the run at all."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert doc["privacy"]["vcs_privacy"] == "ignored"
    assert doc["privacy"]["vcs_kind"] == "git"
    assert doc["privacy"]["work_id"] is None


def test_scan_json_records_an_acknowledged_privacy_disposition_with_work_id(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    outcome = scan_pipeline.run_scan(
        tmp_path, acknowledge_unignored=True, work_id="migrate-app")
    import json

    doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert doc["privacy"]["vcs_privacy"] == "acknowledged_unignored"
    assert doc["privacy"]["work_id"] == "migrate-app"


def test_run_scan_does_not_publish_owner_json_into_the_run(java_repo: Path) -> None:
    """M4 (cold-read, PR-B fix round 3): owner.json (host identity, PID,
    and the writer lock's own owner token) repeats the lock's identity
    for staging reclaim - it must never survive into the published,
    immutable run directory."""
    outcome = scan_pipeline.run_scan(java_repo)
    assert not (outcome.run_dir / "owner.json").exists()


def test_run_scan_refuses_an_empty_scope(tmp_path: Path) -> None:
    """M3 (cold-read, PR-B fix round 3): a scope with nothing addressable
    enumerated at all is a command error (wrong --root, or an over-broad
    exclusion policy), never a valid, publishable, complete zero-unit
    run. Privacy proof comes from ``.git/info/exclude`` (private git
    metadata, never tracked content) rather than a ``.gitignore`` file -
    the latter would itself be one enumerable file, defeating the "truly
    empty scope" scenario this test needs."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".agenttalk/\n", encoding="utf-8")

    with pytest.raises(scan_pipeline.ScanRefused, match="no files were enumerated"):
        scan_pipeline.run_scan(tmp_path)
    from agenttalk.comprehension import publish

    comp_dir = scan_pipeline.paths.comprehension_dir(tmp_path / ".agenttalk")
    doc, _digest = publish.read_current_index(comp_dir)
    assert doc is None  # no run published
    # M-5 (second cold read, PR-B fix round 4): staging used to be
    # created BEFORE this refusal ran, leaking an abandoned
    # .staging/<scan_id>-<nonce>/ directory on every refused scan.
    staging_root = scan_pipeline.paths.staging_dir(comp_dir)
    assert not staging_root.is_dir() or list(staging_root.iterdir()) == []


def test_empty_scope_refusal_names_the_global_roots_actual_position(tmp_path: Path) -> None:
    """N1 (fifth cold read, fix round 8): "--root" is the GLOBAL flag
    (registered on the top-level parser before subparsers), not a
    comprehension subcommand option - empirically verified:
    `agenttalk --root <path> comprehension scan` works, while
    `agenttalk comprehension scan --root <path>` fails with
    "unrecognized arguments" (comprehension's own subparser defines no
    --root of its own). The bare word "--root" invited a reader to place
    it after the subcommand instead; the refusal now names where it
    actually has to go."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".agenttalk/\n", encoding="utf-8")

    with pytest.raises(scan_pipeline.ScanRefused, match=r"agenttalk --root <path> comprehension scan"):
        scan_pipeline.run_scan(tmp_path)


def test_empty_scope_refusal_never_leaks_the_absolute_root(tmp_path: Path) -> None:
    """FIX ROUND 19 (fifteenth cold read, F7 MINOR, same class as
    CR10-12): this message reaches the CLI's plain stderr output the
    same way CR10-12's own VcsPrivacyRefused message did - the raw
    absolute local root named next to a projection family that
    otherwise never persists one."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".agenttalk/\n", encoding="utf-8")

    with pytest.raises(scan_pipeline.ScanRefused) as exc_info:
        scan_pipeline.run_scan(tmp_path)
    assert str(tmp_path) not in str(exc_info.value)
    assert tmp_path.name in str(exc_info.value)


def test_get_status_for_an_unknown_root_never_leaks_the_absolute_root(tmp_path: Path) -> None:
    """FIX ROUND 19 (fifteenth cold read, F7 MINOR, same class as
    CR10-12, swept across get_status/get_report/validate_run - all
    three share this identical message): a caller-supplied root with no
    published run at all (never scanned, or a run id naming nothing
    that exists) must never echo the raw absolute local root back."""
    with pytest.raises(scan_pipeline.NotScanned) as exc_info:
        scan_pipeline.get_status(tmp_path)
    assert str(tmp_path) not in str(exc_info.value)
    assert tmp_path.name in str(exc_info.value)


def test_get_report_for_a_missing_run_id_never_leaks_the_absolute_root(
    java_repo: Path,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, CR17-8 MINOR, the class-
    closer): a DIFFERENT shape from the existing NotScanned leak fix
    above - this repo HAS a real published run (index.json exists,
    NotScanned never fires), but ``--run`` names a syntactically valid
    scan_id that was never actually published. Resolving that path
    succeeds (only a later existence check fails), reaching
    envelope.read_json_document's own OSError/EnvelopeError path - which
    used to embed the FULL absolute run_dir path (twice: once in its own
    message, once again via the OSError's own str(exc))."""
    scan_pipeline.run_scan(java_repo)
    with pytest.raises(scan_pipeline.ComprehensionError) as exc_info:
        scan_pipeline.get_report(java_repo, run_id="20260101T000000Z-abcd1234")
    assert str(java_repo) not in str(exc_info.value)
    assert "scan.json" in str(exc_info.value)


def test_run_scan_reclaims_an_abandoned_staging_dir_from_a_prior_crash(
    java_repo: Path, monkeypatch,
) -> None:
    """M-5 (second cold read, PR-B fix round 4): reclaim_abandoned_staging
    had ZERO production callers - an abandoned staging directory (the
    shape a crashed or refused prior scan would leave behind) was never
    cleaned up automatically. Now wired at lock acquisition, matching
    both staging.py's own docstring and the design's own phrasing ("At
    lock acquisition, the scanner reclaims only unpublished staging
    directories...")."""
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import privacy as privacymod
    from agenttalk.comprehension import staging as stagingmod

    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    privacy_result = privacymod.run_privacy_preflight(java_repo)
    abandoned_lock = lockmod.acquire_scan_lock(
        comp_dir, privacy=privacy_result, predecessor_index_digest=None)
    abandoned_handle = stagingmod.create_staging_dir(
        scan_id="20260101T000000Z-abcd1234", lock_handle=abandoned_lock)
    lockmod.release_scan_lock(abandoned_lock)
    assert abandoned_handle.path.exists()

    # The abandoned directory's owner.json names THIS test process's own
    # pid (a real, live process) - simulate it being definitely dead, the
    # same way test_comprehension_staging.py's own reclaim tests do.
    monkeypatch.setattr(stagingmod, "process_observation", lambda pid: ("dead", None))

    scan_pipeline.run_scan(java_repo)

    assert not abandoned_handle.path.exists()


# ----------------------------------------------------------- M1: read-path run-id confinement

def test_get_status_rejects_a_run_id_outside_the_runs_tree(java_repo: Path, tmp_path: Path) -> None:
    """M1 (cold-read, PR-B fix round 3): the write path validates and
    resolve-confines a scan_id under runs/ before it ever touches disk -
    every read path must do the same, so a caller-supplied --run value
    can never open a document sitting outside the published-runs tree."""
    scan_pipeline.run_scan(java_repo)
    outside = tmp_path / "outside-runs-tree"
    outside.mkdir()
    (outside / "scan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(scan_pipeline.EnvelopeError):
        scan_pipeline.get_status(java_repo, run_id="../../outside-runs-tree")


def test_get_report_rejects_a_malformed_run_id(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    with pytest.raises(scan_pipeline.EnvelopeError):
        scan_pipeline.get_report(java_repo, run_id="../../../etc/passwd")


def test_validate_run_reports_invalid_for_a_malformed_run_id(java_repo: Path) -> None:
    """validate_run's own contract catches ComprehensionError (which
    EnvelopeError is a subclass of) and reports it via the return value
    rather than raising - M1's confinement still holds here: the
    malformed id is rejected before any document outside runs/ is ever
    opened, just surfaced as valid=False instead of a raised exception."""
    scan_pipeline.run_scan(java_repo)
    result = scan_pipeline.validate_run(java_repo, run_id="not/a/real/scan/id")
    assert result["valid"] is False


def test_run_scan_refuses_without_privacy_proof_and_writes_nothing(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")  # does NOT ignore .agenttalk/
    _write_sample_java_project(tmp_path)
    with pytest.raises(VcsPrivacyRefused):
        scan_pipeline.run_scan(tmp_path)
    assert not (tmp_path / ".agenttalk").exists()


def test_run_scan_with_acknowledge_but_no_work_id_refuses(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    with pytest.raises(scan_pipeline.ScanRefused, match="work-id"):
        scan_pipeline.run_scan(tmp_path, acknowledge_unignored=True)


def test_run_scan_with_acknowledge_but_no_work_id_refuses_even_with_no_privacy_issue(
    tmp_path: Path,
) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F9, wrong-refusal-timing):
    the sibling test above only exercises the pairing refusal on a repo
    the preflight was ALSO going to refuse anyway - the refusal used to
    live inside the preflight's own except branch, so a caller who
    passed --acknowledge-unignored-private-store with no --work-id
    against a repo with NO privacy issue at all (.agenttalk correctly
    ignored here) silently proceeded, no different from never having
    passed the flag. The pairing is a property of the arguments
    themselves, never of what the preflight happens to find - must
    refuse here too."""
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agenttalk/\nbuild/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    with pytest.raises(scan_pipeline.ScanRefused, match="work-id"):
        scan_pipeline.run_scan(tmp_path, acknowledge_unignored=True)
    assert not (tmp_path / ".agenttalk").exists()


def test_run_scan_with_acknowledge_and_work_id_proceeds(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    outcome = scan_pipeline.run_scan(
        tmp_path, acknowledge_unignored=True, work_id="migrate-app")
    assert outcome.status == "complete"


def test_run_scan_store_wide_dead_end_now_escapes_with_acknowledge_and_work_id(
    tmp_path: Path,
) -> None:
    """FIX ROUND 35 (twenty-ninth cold read, F2 MAJOR part (a), .cr29-
    deadend verbatim): a rule that re-includes a real artifact filename
    specifically (modules.json, never named by any of the preflight's own
    three FIXED literal probes) passes the cheap, early preflight but is
    caught by round 34's own store-wide post-publish check. Before this
    fix, an operator retrying with --acknowledge-unignored-private-store
    hit the IDENTICAL passing preflight, got the automatic "ignored"
    disposition again, and the store-wide check refused again - FOREVER,
    with the refusal's own message directing the operator to a flag that
    provably could never change the outcome. A properly-attended
    acknowledge+work-id run must now proceed."""
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/runs/\n"
        "!.agenttalk/comprehension/runs/*/\n"
        "!.agenttalk/comprehension/runs/*/modules.json\n",
        encoding="utf-8",
    )
    _write_sample_java_project(tmp_path)
    with pytest.raises(VcsPrivacyRefused, match="modules.json"):
        scan_pipeline.run_scan(tmp_path)
    outcome = scan_pipeline.run_scan(
        tmp_path, acknowledge_unignored=True, work_id="migrate-app")
    assert outcome.status == "complete"


def test_run_scan_store_wide_dead_end_still_refuses_headless(tmp_path: Path) -> None:
    """Companion control (.cr29-privflip's own headless half): the SAME
    scenario, with no attended acknowledgment - must keep refusing on
    every retry, never silently proceed just because a prior attempt also
    failed."""
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/runs/\n"
        "!.agenttalk/comprehension/runs/*/\n"
        "!.agenttalk/comprehension/runs/*/modules.json\n",
        encoding="utf-8",
    )
    _write_sample_java_project(tmp_path)
    with pytest.raises(VcsPrivacyRefused, match="modules.json"):
        scan_pipeline.run_scan(tmp_path)
    with pytest.raises(VcsPrivacyRefused, match="modules.json"):
        scan_pipeline.run_scan(tmp_path)


def test_a_second_scan_chains_the_predecessor_digest(java_repo: Path) -> None:
    first = scan_pipeline.run_scan(java_repo)
    (java_repo / "src" / "main" / "java" / "p" / "Other.java").write_text(
        "package p;\nclass Other {\n}\n", encoding="utf-8")
    second = scan_pipeline.run_scan(java_repo)
    assert second.index["predecessor_digest"] is not None
    assert second.index["latest_scan_id"] == second.scan_id
    assert second.scan_id != first.scan_id


def test_scan_json_content_digest_is_stable_across_two_real_content_identical_scans(
    java_repo: Path,
) -> None:
    """MAJOR 3 (sixth cold read, fix round 9): round 8's own fix (added
    started_at/completed_at to GENERATION_IDENTITY_KEYS) was NOT
    sufficient - field-diffing two REAL scans of this same, unchanged
    repo isolated scan.json's own artifacts[].byte_sha256: each OTHER
    artifact's byte digest is computed over that artifact's own on-disk
    bytes, which embed ITS OWN envelope's scan_id/generated_at - so
    byte_sha256 is generation identity, one level removed, and hashing
    it into scan.json's canonical content digest imported that variance
    right back in. Round 8's own determinism test used a hand-built
    fixture that omitted the "artifacts" key entirely - the exact shape
    that would have caught this - so it passed while the real bug
    remained (fixture-conceals-the-defect, instance four). This test
    runs the real pipeline TWICE and compares the real, on-disk
    documents, not a hand-built stand-in."""
    import json

    from agenttalk.comprehension import digests as digestsmod

    first = scan_pipeline.run_scan(java_repo)
    second = scan_pipeline.run_scan(java_repo)

    first_doc = json.loads((first.run_dir / "scan.json").read_text(encoding="utf-8"))
    second_doc = json.loads((second.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert first_doc["scan_id"] != second_doc["scan_id"]
    assert first_doc["artifacts"] != second_doc["artifacts"]  # byte_sha256 genuinely differs
    assert digestsmod.canonical_content_digest(first_doc) == digestsmod.canonical_content_digest(second_doc)


def test_recover_stale_lock_flag_clears_a_dead_owners_lock(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, CR17-1 BLOCKER, safety
    contract): the flag no longer clears ANY lock unconditionally - this
    fixture's own lock is genuinely stale (a dead owner, per the
    monkeypatched liveness check), so the recovery is safe, and the run
    now also RECORDS the forced recovery as a named, degrading problem
    rather than silently proceeding as if nothing unusual happened."""
    import json

    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import paths as pathsmod
    from agenttalk.comprehension import privacy as privacymod

    comp_dir = pathsmod.comprehension_dir(java_repo / ".agenttalk")
    privacy_result = privacymod.run_privacy_preflight(java_repo)
    stale = lockmod.acquire_scan_lock(comp_dir, privacy=privacy_result, predecessor_index_digest=None)
    assert stale.path.exists()

    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("dead", None))
    outcome = scan_pipeline.run_scan(java_repo, recover_stale_lock=True)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert any(
        p["reason_code"] == "scan_lock_forcibly_recovered" for p in problems_doc["problems"])


def test_recover_stale_lock_flag_refuses_a_provably_live_local_owner(java_repo: Path) -> None:
    """The other half of CR17-1's own fix: a live local owner (this test's
    own process, by construction) must never be silently cleared by the
    flag, even when it is explicitly passed - ScanLockContended, the
    exact same refusal an ordinary contended acquire already raises."""
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import paths as pathsmod
    from agenttalk.comprehension import privacy as privacymod
    from agenttalk.comprehension.errors import ScanLockContended

    comp_dir = pathsmod.comprehension_dir(java_repo / ".agenttalk")
    privacy_result = privacymod.run_privacy_preflight(java_repo)
    live = lockmod.acquire_scan_lock(comp_dir, privacy=privacy_result, predecessor_index_digest=None)
    assert live.path.exists()

    with pytest.raises(ScanLockContended):
        scan_pipeline.run_scan(java_repo, recover_stale_lock=True)
    assert live.path.exists()  # never deleted
    lockmod.release_scan_lock(live)


# ----------------------------------------------------------- get_status

def test_get_status_reports_the_latest_scan(java_repo: Path) -> None:
    outcome = scan_pipeline.run_scan(java_repo)
    status = scan_pipeline.get_status(java_repo)
    assert status["latest_scan_id"] == outcome.scan_id
    assert status["status"] == "complete"
    assert status["freshness"]["state"] == "not_evaluated"


def test_get_status_declares_its_own_narrower_verification_tier(java_repo: Path) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F7, completeness): status's
    own narrower read-cost tier (scan.json's own envelope/anchor only -
    never modules/dependencies/features/readiness/problems, unlike
    report/validate) previously lived only in this function's own
    docstring, invisible to an actual caller. Declared in the payload
    now, pointing at `validate` as the real full-run verification path."""
    scan_pipeline.run_scan(java_repo)
    status = scan_pipeline.get_status(java_repo)
    assert status["artifact_integrity_hint"] == scan_pipeline.STATUS_ARTIFACT_INTEGRITY_HINT
    assert "validate" in status["artifact_integrity_hint"]


def test_get_status_declares_root_binding_is_never_reverified_on_read(java_repo: Path) -> None:
    """M (cold-read PR-B fix round 47 completeness): root_binding is
    checked against the current root exactly once, at write time
    (lock.acquire_scan_lock) - no read command recomputes or re-verifies
    it, so a run directory renamed/transplanted into a different project
    root still reports valid:true/status "complete" for it. Declared
    alongside the field itself now, the same discipline
    STATUS_ARTIFACT_INTEGRITY_HINT already follows for a different gap."""
    scan_pipeline.run_scan(java_repo)
    status = scan_pipeline.get_status(java_repo)
    assert (
        status["root_binding_verification_caveat"]
        == scan_pipeline.ROOT_BINDING_VERIFICATION_CAVEAT
    )
    assert "root_binding" in status


def test_get_status_before_any_scan_raises_not_scanned(tmp_path: Path) -> None:
    with pytest.raises(scan_pipeline.NotScanned):
        scan_pipeline.get_status(tmp_path)


@pytest.mark.parametrize("bad_run_id", ["", "   "])
def test_get_status_refuses_an_empty_or_whitespace_run_id(java_repo: Path, bad_run_id: str) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F7 polish, wrong-data): a
    `--run` value of "" (or whitespace-only) used to fall through the
    bare `run_id or _index_field(...)` falsy check exactly like `None`
    (not provided) - silently resolving to the LATEST run instead of
    ever reaching the closed scan-ID grammar's own refusal, which
    already correctly rejects an empty string; it just never got the
    chance to fire."""
    scan_pipeline.run_scan(java_repo)
    with pytest.raises(scan_pipeline.EnvelopeError, match="empty or whitespace"):
        scan_pipeline.get_status(java_repo, run_id=bad_run_id)


@pytest.mark.parametrize("bad_run_id", ["", "   "])
def test_get_report_refuses_an_empty_or_whitespace_run_id(java_repo: Path, bad_run_id: str) -> None:
    """FIX ROUND 29 (F7 polish): the get_report twin of the test above."""
    scan_pipeline.run_scan(java_repo)
    with pytest.raises(scan_pipeline.EnvelopeError, match="empty or whitespace"):
        scan_pipeline.get_report(java_repo, run_id=bad_run_id)


@pytest.mark.parametrize("bad_run_id", ["", "   "])
def test_validate_run_raises_for_an_empty_or_whitespace_run_id(
    java_repo: Path, bad_run_id: str,
) -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F5 polish, wrong-data): an
    empty or whitespace-only --run is a CALLER-level malformed-argument
    error, the SAME class get_status/get_report already raise directly
    for - round 29's own F7 fix wrongly folded this shape into
    validate's own "report a run's own problems via valid:False, don't
    raise" data contract, alongside a well-formed-but-nonexistent or
    malformed-but-non-empty run_id (a genuinely different case - see
    test_validate_run_reports_invalid_for_a_malformed_run_id, still
    reported via valid:False, unchanged)."""
    scan_pipeline.run_scan(java_repo)
    with pytest.raises(scan_pipeline.EnvelopeError, match="empty or whitespace"):
        scan_pipeline.validate_run(java_repo, run_id=bad_run_id)


def _delete_index_field(java_repo: Path, key: str) -> None:
    import json

    index_path = scan_pipeline.paths.index_path(
        scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk"))
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    del doc[key]
    index_path.write_text(json.dumps(doc), encoding="utf-8")


def test_get_status_refuses_an_index_json_missing_latest_scan_id_instead_of_crashing(
    java_repo: Path,
) -> None:
    """MAJOR 2 (fifth cold read, fix round 8): index.json's own body
    fields (latest_scan_id/runs) were read with raw, unguarded subscripts
    in get_status/get_report/validate_run - envelope validation only
    requires schema_version/artifact_type/scan_id/generated_at, never
    index.json's OWN fields, so a malformed-but-envelope-valid index.json
    missing latest_scan_id raised an untyped KeyError straight through
    every read command."""
    scan_pipeline.run_scan(java_repo)
    _delete_index_field(java_repo, "latest_scan_id")

    with pytest.raises(scan_pipeline.ComprehensionError, match="latest_scan_id"):
        scan_pipeline.get_status(java_repo)
    with pytest.raises(scan_pipeline.ComprehensionError, match="latest_scan_id"):
        scan_pipeline.get_report(java_repo)
    with pytest.raises(scan_pipeline.ComprehensionError, match="latest_scan_id"):
        scan_pipeline.validate_run(java_repo)


def test_get_status_refuses_an_index_json_missing_runs_instead_of_crashing(
    java_repo: Path,
) -> None:
    scan_pipeline.run_scan(java_repo)
    _delete_index_field(java_repo, "runs")

    with pytest.raises(scan_pipeline.ComprehensionError, match="runs"):
        scan_pipeline.get_status(java_repo)


def _delete_scan_json_artifact_field(java_repo: Path, run_dir: Path, key: str) -> None:
    import json

    scan_path = run_dir / "scan.json"
    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    del doc["artifacts"][0][key]
    canonical_bytes = scan_pipeline.digests.canonical_json_bytes(doc)
    scan_path.write_bytes(canonical_bytes)
    # Re-sign the index anchor so this isolates the artifacts-entry
    # guard from the separate scan.json anchor-mismatch check.
    index_path = scan_pipeline.paths.index_path(
        scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk"))
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    for run_summary in index_doc["runs"]:
        run_summary["scan_json_byte_sha256"] = scan_pipeline.digests.sha256_bytes(canonical_bytes)
        run_summary["scan_json_content_digest"] = scan_pipeline.digests.canonical_content_digest(doc)
    index_path.write_text(json.dumps(index_doc), encoding="utf-8")


def test_get_report_and_validate_refuse_a_scan_json_artifacts_entry_missing_byte_sha256(
    java_repo: Path,
) -> None:
    """MAJOR 2 (fifth cold read, fix round 8): _verify_artifact_digests
    indexed scan.json's own "artifacts" digest-summary entries with raw,
    unguarded subscripts - an entry missing byte_sha256 (envelope-valid
    otherwise) raised an untyped KeyError through report, and through
    validate too (whose crash-as-exit-1 was indistinguishable from its
    own legitimate valid:false, also exit 1)."""
    outcome = scan_pipeline.run_scan(java_repo)
    _delete_scan_json_artifact_field(java_repo, outcome.run_dir, "byte_sha256")

    with pytest.raises(scan_pipeline.ComprehensionError, match="byte_sha256"):
        scan_pipeline.get_report(java_repo)

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "byte_sha256" in result["detail"]


# ----------------------------------------------------------- get_report

def test_get_report_returns_the_projection(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)
    assert report["status"] == "complete"
    assert report["counts"]["units"] > 0
    assert any(f["label"] == "App" for f in report["features"])


def test_get_report_unit_filter_narrows_the_projection(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    full_report = scan_pipeline.get_report(java_repo)
    one_unit_id = full_report["units"][0]["unit_id"]
    filtered = scan_pipeline.get_report(java_repo, unit_id=one_unit_id)
    assert [u["unit_id"] for u in filtered["units"]] == [one_unit_id]


# ----------------------------------------------------------- validate_run

def test_validate_run_reports_valid_for_a_healthy_run(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is True
    assert result["external_revalidation"] == {
        "performed": False, "reason_code": "no_external_evidence_pointers_this_slice",
    }


def test_validate_run_success_detail_names_the_record_count_and_reference_checks(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R3 note): the
    success detail sentence still named only the ORIGINAL digest-era
    checks even after round 28's own F4 fix added record-count
    verification and the widened cross-artifact reference sweep - the
    same artifact_integrity_hint discipline (declare what a mechanism
    actually covers) applied to validate's own success detail too.

    FIX ROUND 47 (THE SENTENCE AUDIT): the sentence had drifted again -
    module path confinement (round 32), problem_id/id-family collision-
    freedom (rounds 36/38/39), and readiness-summary reference integrity
    (M11, this round) were all checks `invalid` actually depended on but
    this sentence never named.

    MICRO-ROUND 49 (forty-third cold read, C4, THE SENTENCE AUDIT
    again): the new problem_count/degraded_by/status-vs-problems.json
    cross-check folded into this same sentence."""
    scan_pipeline.run_scan(java_repo)
    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is True
    assert "record count" in result["detail"]
    assert "reference" in result["detail"]
    assert "scan_id consistency" in result["detail"]
    assert "readiness-summary" in result["detail"]
    assert "path confinement" in result["detail"]
    assert "collision-freedom" in result["detail"]
    assert "problem_count" in result["detail"]
    assert "degraded_by" in result["detail"]


def test_validate_run_catches_an_artifact_whose_own_scan_id_does_not_match_the_run(
    java_repo: Path,
) -> None:
    """FIX ROUND 47 (forty-first cold read, M5 MAJOR, wrong-data - THE
    SENTENCE AUDIT): validate's own success detail claimed "scan_id
    consistency" as one of its checks, but no code anywhere actually
    compared a loaded artifact's own internal scan_id field against the
    run being read. A run directory whose own modules.json carries a
    DIFFERENT scan_id (e.g. copied from another run, or hand-edited)
    passed silently. Reproduced with the digest/record_count otherwise
    self-consistent (content_digest is UNAFFECTED - scan_id is a
    GENERATION_IDENTITY key already stripped before content-hashing -
    only byte_sha256 needs re-signing) so this test isolates the NEW
    scan_id check specifically, not the pre-existing digest check."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    modules_path = outcome.run_dir / "modules.json"
    doc = json.loads(modules_path.read_text(encoding="utf-8"))
    doc["scan_id"] = "some-other-run-entirely"
    modules_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(doc).decode("utf-8"), encoding="utf-8")
    # Re-sign byte_sha256 only - content_digest is unaffected (scan_id is
    # stripped before content-hashing) and record_count is unaffected
    # (units unchanged) - so the pre-existing digest/record_count checks
    # would NOT catch this on their own, isolating the new check.
    scan_path = outcome.run_dir / "scan.json"
    scan_doc = json.loads(scan_path.read_text(encoding="utf-8"))
    for entry in scan_doc["artifacts"]:
        if entry["name"] == "modules.json":
            entry["byte_sha256"] = scan_pipeline.digests.sha256_file(modules_path)
    scan_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(scan_doc).decode("utf-8"), encoding="utf-8")

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "scan_id" in result["detail"]
    assert "some-other-run-entirely" in result["detail"]

    with pytest.raises(scan_pipeline.ComprehensionError, match="scan_id"):
        scan_pipeline.get_report(java_repo)


def test_get_status_own_scan_id_is_provably_the_requested_run(java_repo: Path) -> None:
    """FIX ROUND 47 (M5 MAJOR): get_status's own scan.json load goes
    through the SAME choke point now - its own returned scan_id field
    is provably the requested run's own identity, never scan.json's
    internal field read back uncompared (a mismatch would raise before
    get_status's own return statement is ever reached)."""
    outcome = scan_pipeline.run_scan(java_repo)
    payload = scan_pipeline.get_status(java_repo)
    assert payload["scan_id"] == outcome.scan_id


def test_run_scan_refuses_to_publish_an_entry_point_with_an_unknown_owning_unit(
    java_repo: Path, monkeypatch,
) -> None:
    """ROUND 9b (sixth cold read, honesty tightening): validate already
    flagged an EDGE referencing an unknown from_unit_id (dangling_edges)
    but never an ENTRY POINT referencing an unknown owning_unit_id - the
    same "unattributable synthesized owner" shape round 9's own BLOCKER
    fixed at the adapter level could still slip past validate
    undetected on the entry-point side.

    FIX ROUND 29 (twenty-fifth cold read, F2 MAJOR): this used to let
    `run_scan` PUBLISH the injected orphan (self-consistent digests
    throughout - nothing byte-level was ever tampered) and rely on a
    SEPARATE `validate_run` call to catch it after the fact. The SAME
    sweep now also runs at publish time, against these same in-memory
    records, before anything is ever written to staging - injecting a
    dangling reference here now means `run_scan` itself refuses,
    never a run that quietly reports "valid: false" only if a caller
    happens to check afterward."""
    from agenttalk.comprehension import features_artifact as featuresmod

    real_build_features = featuresmod.build_features

    def _inject_a_dangling_entry_point(*args, **kwargs):
        entry_points, features, _problems = real_build_features(*args, **kwargs)
        orphan = featuresmod.EntryPointRecord(
            entry_point_id="orphan-entry-point", kind="http_route", name="GET /orphan",
            owning_unit_id="does-not-exist", feature_ids=[], evidence_class="declared",
        )
        return [*entry_points, orphan], features, _problems

    monkeypatch.setattr(scan_pipeline.features_artifact, "build_features", _inject_a_dangling_entry_point)

    with pytest.raises(scan_pipeline.ComprehensionError, match="owning_unit_id") as exc_info:
        scan_pipeline.run_scan(java_repo)

    # MICRO-ROUND 29b (JUDGE, note-only, chosen behavior): the same
    # orphaned-staging-dir disposition as the F6 ordering refusal - see
    # its own test for the full reasoning. Named here once, at this
    # representative F2 category, since the disposition is a property of
    # the shared raise site, not of which dangling category triggered it.
    assert "prune --staging" in str(exc_info.value)
    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    staging_entries = list(scan_pipeline.paths.staging_dir(comp_dir).iterdir())
    assert len(staging_entries) == 1
    assert [p.name for p in staging_entries[0].iterdir()] == ["owner.json"]


def test_run_scan_refuses_to_publish_an_entry_point_with_an_unknown_declared_in_unit(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F4, completeness): the
    dangling-reference sweep never covered declared_in_unit_id (round
    27's own new field). FIX ROUND 29 (F2): now caught at PUBLISH time,
    not merely by a later validate_run call - see the sibling test
    above."""
    from agenttalk.comprehension import features_artifact as featuresmod

    real_build_features = featuresmod.build_features

    def _inject_a_dangling_declared_in(*args, **kwargs):
        entry_points, features, _problems = real_build_features(*args, **kwargs)
        real_owner = entry_points[0].owning_unit_id if entry_points else "does-not-exist-owner"
        orphan = featuresmod.EntryPointRecord(
            entry_point_id="orphan-declared-in", kind="http_route", name="GET /orphan",
            owning_unit_id=real_owner, feature_ids=[], evidence_class="declared",
            declared_in_unit_id="does-not-exist-declarer",
        )
        return [*entry_points, orphan], features, _problems

    monkeypatch.setattr(
        scan_pipeline.features_artifact, "build_features", _inject_a_dangling_declared_in)

    with pytest.raises(scan_pipeline.ComprehensionError, match="declared_in_unit_id"):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_feature_with_an_unknown_unit_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 28 (F4, completeness) + FIX ROUND 29 (F2, now caught at
    publish time): feature.unit_ids was never swept for dangling
    references either."""
    from agenttalk.comprehension import features_artifact as featuresmod

    real_build_features = featuresmod.build_features

    def _inject_a_dangling_feature_unit(*args, **kwargs):
        entry_points, features, _problems = real_build_features(*args, **kwargs)
        orphan = featuresmod.FeatureRecord(
            feature_id="orphan-feature-unit", label="Orphan", state="candidate",
            origin="detected", unit_ids=["does-not-exist"], entry_point_ids=[],
        )
        return entry_points, [*features, orphan], _problems

    monkeypatch.setattr(
        scan_pipeline.features_artifact, "build_features", _inject_a_dangling_feature_unit)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="feature\\(s\\) reference an unknown unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_feature_with_an_unknown_entry_point_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 28 (F4, completeness) + FIX ROUND 29 (F2, now caught at
    publish time): feature.entry_point_ids was never swept for dangling
    references either."""
    from agenttalk.comprehension import features_artifact as featuresmod

    real_build_features = featuresmod.build_features

    def _inject_a_dangling_feature_entry_point(*args, **kwargs):
        entry_points, features, _problems = real_build_features(*args, **kwargs)
        orphan = featuresmod.FeatureRecord(
            feature_id="orphan-feature-ep", label="Orphan", state="candidate",
            origin="detected", unit_ids=[], entry_point_ids=["does-not-exist-ep"],
        )
        return entry_points, [*features, orphan], _problems

    monkeypatch.setattr(
        scan_pipeline.features_artifact, "build_features", _inject_a_dangling_feature_entry_point)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="feature\\(s\\) reference an unknown entry_point_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_readiness_signal_with_an_unknown_unit_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 28 (F4, completeness) + FIX ROUND 29 (F2, now caught at
    publish time): readiness signals[].unit_id was never swept for
    dangling references either."""
    from agenttalk.comprehension import readiness_artifact as readinessmod

    real_build_readiness = readinessmod.build_readiness

    def _inject_a_dangling_signal(*args, **kwargs):
        signals, summaries = real_build_readiness(*args, **kwargs)
        orphan = readinessmod.ReadinessSignal(
            signal_id="orphan-signal", unit_id="does-not-exist", check="source_understood",
            stored_status="unknown", severity="warning", basis="detected",
            reason_code="test_injected",
        )
        return [*signals, orphan], summaries

    monkeypatch.setattr(
        scan_pipeline.readiness_artifact, "build_readiness", _inject_a_dangling_signal)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="readiness signal\\(s\\) reference an unknown unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_module_with_an_unknown_container_unit_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 28 (F4, completeness) + FIX ROUND 29 (F2, now caught at
    publish time): module.container_unit_id was never swept for
    dangling references either."""
    from agenttalk.comprehension import modules_artifact as modulesmod

    real_build_modules = modulesmod.build_modules

    def _inject_a_dangling_container(*args, **kwargs):
        records = real_build_modules(*args, **kwargs)
        orphan = modulesmod.ModuleRecord(
            unit_id="orphan-module", kind="file", display_name="Orphan.txt", language="unknown",
            paths=["Orphan.txt"], source_digests={}, classification=[],
            container_unit_id="does-not-exist", producers=[],
        )
        return [*records, orphan]

    monkeypatch.setattr(
        scan_pipeline.modules_artifact, "build_modules", _inject_a_dangling_container)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="module\\(s\\) reference an unknown container_unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_dependency_edge_with_an_unknown_target_unit_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F2 MAJOR, wrong-data): the
    reader re-signed the digest chain and got `valid: true` + `report`
    exit 0 with a fabricated `target_unit_id` - dependencies[].
    target_unit_id was never swept at all, at either validate or
    publish time."""
    from agenttalk.comprehension import dependencies_artifact as depsmod

    real_build_dependencies = depsmod.build_dependencies

    def _inject_a_dangling_target(*args, **kwargs):
        edges = real_build_dependencies(*args, **kwargs)
        orphan = depsmod.DependencyRecord(
            edge_id="orphan-edge",
            from_unit_id=scan_pipeline.digests.unit_id(
                kind="component", paths=["src/main/java/p/App.java"], qualified_name="p.App"),
            relation="import", phase="runtime",
            optional=False, evidence_class="extracted", resolution_state="resolved",
            target_unit_id="does-not-exist",
        )
        return [*edges, orphan]

    monkeypatch.setattr(
        scan_pipeline.dependencies_artifact, "build_dependencies", _inject_a_dangling_target)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="edge\\(s\\) reference an unknown target_unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_dependency_edge_with_an_unknown_candidate_unit_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 29 (F2 MAJOR): the ambiguous-resolution twin of the
    target_unit_id test above - dependencies[].candidate_unit_ids was
    equally unswept."""
    from agenttalk.comprehension import dependencies_artifact as depsmod

    real_build_dependencies = depsmod.build_dependencies

    def _inject_a_dangling_candidate(*args, **kwargs):
        edges = real_build_dependencies(*args, **kwargs)
        orphan = depsmod.DependencyRecord(
            edge_id="orphan-edge",
            from_unit_id=scan_pipeline.digests.unit_id(
                kind="component", paths=["src/main/java/p/App.java"], qualified_name="p.App"),
            relation="import", phase="runtime",
            optional=False, evidence_class="extracted", resolution_state="ambiguous",
            candidate_unit_ids=["does-not-exist"],
        )
        return [*edges, orphan]

    monkeypatch.setattr(
        scan_pipeline.dependencies_artifact, "build_dependencies", _inject_a_dangling_candidate)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="edge\\(s\\) reference an unknown candidate_unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_dependency_edge_with_an_unknown_target_unit_id_even_when_unresolved(
    java_repo: Path, monkeypatch,
) -> None:
    """M10 (cold-read PR-B fix round 47): the target_unit_id sweep used to
    gate on resolution_state == "resolved" - but projector._fan_counts
    reads edge.target_unit_id unconditionally (no resolution_state check
    at all), so a dangling target_unit_id on a non-"resolved" edge would
    silently feed the projector's own fan-in count while this sweep stayed
    blind to it. Proves the sweep now catches it regardless of
    resolution_state (a producer bug, not today's real producer's own
    shape, which never actually sets target_unit_id off "resolved" - this
    is the sweep's OWN defense against that assumption drifting)."""
    from agenttalk.comprehension import dependencies_artifact as depsmod

    real_build_dependencies = depsmod.build_dependencies

    def _inject_a_dangling_unresolved_target(*args, **kwargs):
        edges = real_build_dependencies(*args, **kwargs)
        orphan = depsmod.DependencyRecord(
            edge_id="orphan-edge",
            from_unit_id=scan_pipeline.digests.unit_id(
                kind="component", paths=["src/main/java/p/App.java"], qualified_name="p.App"),
            relation="import", phase="runtime",
            optional=False, evidence_class="extracted", resolution_state="unresolved",
            target_unit_id="does-not-exist",
        )
        return [*edges, orphan]

    monkeypatch.setattr(
        scan_pipeline.dependencies_artifact, "build_dependencies", _inject_a_dangling_unresolved_target)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="edge\\(s\\) reference an unknown target_unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_a_readiness_summary_with_an_unknown_unit_id(
    java_repo: Path, monkeypatch,
) -> None:
    """M11 (cold-read PR-B fix round 47): readiness.json's own
    summaries[].unit_id had NO cross-reference sweep at all - every other
    unit_id-shaped reference (signals[].unit_id, edges, entry points,
    features, module containers) was already covered; UnitReadinessSummary
    was the one family missed."""
    from agenttalk.comprehension import readiness_artifact as readinessmod

    real_build_readiness = readinessmod.build_readiness

    def _inject_a_dangling_summary(*args, **kwargs):
        signals, summaries = real_build_readiness(*args, **kwargs)
        orphan = readinessmod.UnitReadinessSummary(
            unit_id="does-not-exist", stored_assessment_state="needs_evidence",
        )
        return signals, [*summaries, orphan]

    monkeypatch.setattr(
        scan_pipeline.readiness_artifact, "build_readiness", _inject_a_dangling_summary)

    with pytest.raises(
        scan_pipeline.ComprehensionError,
        match="readiness summary\\(s\\) reference an unknown unit_id",
    ):
        scan_pipeline.run_scan(java_repo)


def test_run_scan_refuses_to_publish_an_entry_point_with_an_unknown_feature_id(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 29 (F2 MAJOR): entry_points[].feature_ids was equally
    unswept - an entry point naming a feature that does not exist."""
    from agenttalk.comprehension import features_artifact as featuresmod

    real_build_features = featuresmod.build_features

    def _inject_a_dangling_feature_id(*args, **kwargs):
        entry_points, features, _problems = real_build_features(*args, **kwargs)
        real_owner = entry_points[0].owning_unit_id if entry_points else scan_pipeline.digests.unit_id(
            kind="component", paths=["src/main/java/p/App.java"], qualified_name="p.App")
        orphan = featuresmod.EntryPointRecord(
            entry_point_id="orphan-entry-point-feature", kind="http_route", name="GET /orphan",
            owning_unit_id=real_owner, feature_ids=["does-not-exist-feature"],
            evidence_class="declared",
        )
        return [*entry_points, orphan], features, _problems

    monkeypatch.setattr(
        scan_pipeline.features_artifact, "build_features", _inject_a_dangling_feature_id)

    with pytest.raises(
        scan_pipeline.ComprehensionError, match="entry point\\(s\\) reference an unknown feature_id",
    ):
        scan_pipeline.run_scan(java_repo)


# --------------------- FIX ROUND 32 (twenty-eighth cold read, F4 MAJOR,
# completeness): path confinement is now checked at publish time, on
# `validate`, and on `report` - none of the three checked it before.

def test_run_scan_refuses_to_publish_a_module_with_an_unconfined_path(
    java_repo: Path, monkeypatch,
) -> None:
    """A module record whose own `paths` entry escapes project
    confinement (an absolute path, e.g. rewritten to C:/Windows/win.ini)
    used to publish cleanly - nothing checked this dimension at all."""
    from agenttalk.comprehension import modules_artifact as modulesmod

    real_build_modules = modulesmod.build_modules

    def _inject_an_unconfined_path(*args, **kwargs):
        records = real_build_modules(*args, **kwargs)
        escaping = modulesmod.ModuleRecord(
            unit_id="escaping-module", kind="file", display_name="win.ini", language="unknown",
            paths=["C:/Windows/win.ini"], source_digests={}, classification=[],
            container_unit_id=None, producers=[],
        )
        return [*records, escaping]

    monkeypatch.setattr(scan_pipeline.modules_artifact, "build_modules", _inject_an_unconfined_path)

    with pytest.raises(scan_pipeline.ComprehensionError, match="unconfined path"):
        scan_pipeline.run_scan(java_repo)


def test_validate_and_report_refuse_a_published_run_with_an_unconfined_path(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 32 (F4(a) MAJOR): the reader's own t4/t7 shapes - a
    module's own `paths` entry escaping confinement was never checked on
    READ either. `_verify_artifact_digests` checks the RAW on-disk JSON
    docs, unaffected by patching the record CONVERTER's return value here,
    so this proves the read-path check fires independently of the digest
    check, on an otherwise genuinely well-formed, already-published run."""
    import dataclasses

    outcome = scan_pipeline.run_scan(java_repo)
    real_from_json = scan_pipeline.modules_artifact.module_record_from_json

    def _corrupt_the_first_modules_path(payload):
        record = real_from_json(payload)
        return dataclasses.replace(record, paths=["C:/Windows/win.ini"])

    monkeypatch.setattr(
        scan_pipeline.modules_artifact, "module_record_from_json", _corrupt_the_first_modules_path)

    result = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert result["valid"] is False
    assert "must be relative, not absolute" in result["detail"]

    with pytest.raises(scan_pipeline.ComprehensionError, match="unconfined path"):
        scan_pipeline.get_report(java_repo, run_id=outcome.scan_id)


def test_report_refuses_a_published_run_with_a_dangling_container_reference(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 32 (F4(b) MAJOR, completeness, JUDGE - taken): `validate`
    already caught a dangling cross-artifact reference (round 28/29);
    `report` never checked at all - a run with one was projected and
    emitted with `scan_json_integrity` verified alongside it. Reuses the
    identical sweep `validate` already runs, on an otherwise genuinely
    well-formed, already-published run (the digest check, over the RAW
    on-disk JSON, is unaffected by patching the record converter here)."""
    import dataclasses

    outcome = scan_pipeline.run_scan(java_repo)
    real_from_json = scan_pipeline.modules_artifact.module_record_from_json

    def _corrupt_the_first_modules_container(payload):
        record = real_from_json(payload)
        return dataclasses.replace(record, container_unit_id="does-not-exist")

    monkeypatch.setattr(
        scan_pipeline.modules_artifact, "module_record_from_json",
        _corrupt_the_first_modules_container)

    with pytest.raises(scan_pipeline.ComprehensionError, match="unknown container_unit_id"):
        scan_pipeline.get_report(java_repo, run_id=outcome.scan_id)


def test_validate_run_catches_a_problem_id_shared_by_non_identical_records(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 36 (thirtieth cold read, F2 MAJOR, part (c)): the
    problem_id-collision sweep publish time now refuses on (run_scan)
    is shared here too, so a run corrupted by some OTHER path (or
    published before this check existed) is still caught by `validate`
    rather than silently reporting valid:true forever. Mirrors the
    dangling-container test's own technique: corrupts the IN-MEMORY
    records validate builds (never the on-disk bytes, which stay
    genuinely digest-clean) via a monkeypatched loader, isolating this
    one check from the separate digest-integrity dimension. A clean
    control runs first, proving the corruption (not some unrelated
    fixture problem) is what flips valid to False."""
    (java_repo / "notes.xyz").write_text("hello", encoding="utf-8")
    outcome = scan_pipeline.run_scan(java_repo)

    clean = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert clean["valid"] is True

    real_load = scan_pipeline._load_run_records

    def _duplicate_the_first_problem_with_different_bytes(comprehension_dir, scan_id):
        records = dict(real_load(comprehension_dir, scan_id))
        problems = list(records["problems"])
        assert problems, "fixture must record at least one real problem"
        corrupted = dict(problems[0])
        corrupted["detail"] = corrupted["detail"] + " (a different fact, same id)"
        records["problems"] = [*problems, corrupted]
        return records

    monkeypatch.setattr(
        scan_pipeline, "_load_run_records", _duplicate_the_first_problem_with_different_bytes)

    result = scan_pipeline.validate_run(java_repo, run_id=outcome.scan_id)
    assert result["valid"] is False
    assert "problem_id" in result["detail"]


def test_scan_json_carries_per_artifact_and_run_level_digests(java_repo: Path) -> None:
    """M2 (cold-read, PR-B fix round 3): scan.json must carry per-artifact
    byte SHA-256 + canonical content digest + record count + schema
    version, and a run-level content_digest - digests.py's own machinery
    for this existed with no production caller."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    names = {a["name"] for a in doc["artifacts"]}
    assert names == {
        "modules.json", "dependencies.json", "features.json", "readiness.json", "problems.json",
    }
    for artifact in doc["artifacts"]:
        assert artifact["byte_sha256"]
        assert artifact["content_digest"]
        assert artifact["record_count"] >= 0
        assert artifact["schema_version"] >= 1
    assert doc["content_digest"]


def test_verify_artifact_digests_catches_a_falsified_record_count(tmp_path: Path) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F4, completeness): scan.json's
    own declared ``record_count`` is derivable from the artifact's own
    on-disk content the exact same way byte_sha256/content_digest
    already are - a scan.json that falsifies ONLY this one field (both
    digests correct) previously passed every check here. No end-to-end
    reproduction exists through run_scan/validate_run's own public
    surface (record_counts is correct by construction from the same doc
    it publishes, and any post-hoc edit to scan.json itself trips the
    OUTER anchor check - MAJOR 3/round 7 - before ever reaching this
    function) - a direct unit test of the defense-in-depth check itself,
    guarding a future change that breaks that construction invariant."""
    from agenttalk.comprehension import digests

    modules_doc = {
        "schema_version": 1, "artifact_type": scan_pipeline.MODULES_ARTIFACT_TYPE,
        "scan_id": "scan-1", "generated_at": "2026-08-27T00:00:00Z",
        "units": [{"unit_id": "u1"}, {"unit_id": "u2"}],
    }
    modules_bytes = digests.canonical_json_bytes(modules_doc)
    (tmp_path / "modules.json").write_bytes(modules_bytes)
    entry = {
        "name": "modules.json", "artifact_type": scan_pipeline.MODULES_ARTIFACT_TYPE,
        "schema_version": 1,
        "content_digest": digests.canonical_content_digest(modules_doc),
        "byte_sha256": digests.sha256_bytes(modules_bytes),
        "record_count": 1,  # falsified - the doc actually has 2 units
    }
    scan_doc = {"artifacts": [entry], "content_digest": digests.run_content_digest([entry])}
    with pytest.raises(scan_pipeline.ComprehensionError, match="record_count"):
        scan_pipeline._verify_artifact_digests(scan_doc, {"modules.json": modules_doc}, tmp_path)


def test_validate_run_catches_a_tampered_artifact_via_its_digest(java_repo: Path) -> None:
    """M2 (cold-read, PR-B fix round 3): validate_run must actually detect
    a mismatch between an artifact's declared digest and its real
    on-disk content - the design's own "full-run integrity" claim,
    exercised end to end rather than just at the unit level."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    modules_path = outcome.run_dir / "modules.json"
    doc = json.loads(modules_path.read_text(encoding="utf-8"))
    doc["units"] = []  # tamper: content no longer matches the declared digest
    modules_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(doc).decode("utf-8"), encoding="utf-8")

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "content_digest" in result["detail"] or "byte_sha256" in result["detail"]


def test_validate_run_catches_a_whitespace_only_rewrite_of_an_artifact(
    java_repo: Path,
) -> None:
    """M-3 (second cold read, fix round 4): the byte SHA-256 check
    previously recomputed sha256(canonical_json_bytes(doc)) from the
    PARSED document, not the file's real bytes - a whitespace-only
    rewrite (identical parsed content, different bytes on disk) passed
    validation because the re-canonicalized bytes matched the declared
    value regardless of what was actually on disk. Reproduced: rewriting
    modules.json with extra indentation/spacing (same units, same JSON
    value) must now be caught."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    modules_path = outcome.run_dir / "modules.json"
    doc = json.loads(modules_path.read_text(encoding="utf-8"))
    # Pretty-printed with extra whitespace - parses to the IDENTICAL
    # value as the canonical, compact form scan.json's byte_sha256 was
    # computed from, but the bytes on disk are now different.
    modules_path.write_text(json.dumps(doc, indent=4, sort_keys=True), encoding="utf-8")

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "byte_sha256" in result["detail"]


def test_validate_run_catches_a_record_counts_map_that_disagrees_with_its_own_artifacts_list(
    java_repo: Path,
) -> None:
    """M (cold-read PR-B fix round 47 completeness): scan.json's own
    top-level record_counts map was only ever validated for PRESENCE
    (the "record_counts" key exists) - never that its own per-name
    values agree with artifacts[].record_count, the value
    _verify_artifact_digests already proves matches what is genuinely
    on disk. Reproduced: hand-edit ONLY record_counts["modules.json"]
    (re-signing scan.json's own byte_sha256/content_digest anchor in
    index.json so this isolates the new consistency check from the
    pre-existing anchor-mismatch check, the same isolation technique
    the scan_id-consistency test above uses)."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    scan_path = outcome.run_dir / "scan.json"
    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    doc["record_counts"]["modules.json"] += 1
    canonical_bytes = scan_pipeline.digests.canonical_json_bytes(doc)
    scan_path.write_bytes(canonical_bytes)
    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    index_path = scan_pipeline.paths.index_path(comp_dir)
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    for run_summary in index_doc["runs"]:
        if run_summary["scan_id"] == outcome.scan_id:
            run_summary["scan_json_byte_sha256"] = scan_pipeline.digests.sha256_bytes(canonical_bytes)
            run_summary["scan_json_content_digest"] = scan_pipeline.digests.canonical_content_digest(doc)
    index_path.write_text(json.dumps(index_doc), encoding="utf-8")

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "record_counts" in result["detail"]


def _tamper_scan_json_top_level_field(java_repo: Path, outcome, mutate) -> None:
    """Shared isolation technique for the three tests below - hand-edit
    ONE top-level scan.json field, then re-sign scan.json's own byte_
    sha256/content_digest anchor in index.json so the pre-existing
    anchor-mismatch check never fires first and masks the new
    consistency check being exercised, the same isolation the record_
    counts test above already establishes."""
    import json

    scan_path = outcome.run_dir / "scan.json"
    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    mutate(doc)
    canonical_bytes = scan_pipeline.digests.canonical_json_bytes(doc)
    scan_path.write_bytes(canonical_bytes)
    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    index_path = scan_pipeline.paths.index_path(comp_dir)
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    for run_summary in index_doc["runs"]:
        if run_summary["scan_id"] == outcome.scan_id:
            run_summary["scan_json_byte_sha256"] = scan_pipeline.digests.sha256_bytes(canonical_bytes)
            run_summary["scan_json_content_digest"] = scan_pipeline.digests.canonical_content_digest(doc)
    index_path.write_text(json.dumps(index_doc), encoding="utf-8")


def test_validate_run_catches_a_problem_count_that_disagrees_with_problems_json(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, C4, completeness): scan.
    json's own problem_count was only ever validated for PRESENCE, never
    cross-checked against problems.json's own actual record count the
    way record_counts already is above."""
    outcome = scan_pipeline.run_scan(java_repo)
    _tamper_scan_json_top_level_field(
        java_repo, outcome, lambda doc: doc.__setitem__("problem_count", doc["problem_count"] + 1))

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "problem_count" in result["detail"]


def test_validate_run_catches_a_degraded_by_reason_with_no_backing_problem(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49 (C4's own degraded_by twin): a degraded_by entry
    naming a reason code with no matching problem record in problems.
    json at all - fabricated, never backed by real evidence - passed
    every check before this round."""
    outcome = scan_pipeline.run_scan(java_repo)
    _tamper_scan_json_top_level_field(
        java_repo, outcome,
        lambda doc: doc.__setitem__(
            "degraded_by", sorted({*doc["degraded_by"], "no_such_reason_code_exists"})))

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "degraded_by" in result["detail"]


def test_validate_run_catches_a_status_degraded_by_inconsistency(java_repo: Path) -> None:
    """MICRO-ROUND 49 (C4's own status twin): status="degraded" declared
    over a genuinely empty degraded_by - internally inconsistent, never
    checked against problems.json's own honest (empty, for this clean
    fixture) content before this round."""
    outcome = scan_pipeline.run_scan(java_repo)
    _tamper_scan_json_top_level_field(
        java_repo, outcome, lambda doc: doc.__setitem__("status", "degraded"))

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "status" in result["detail"]


def test_validate_run_catches_a_loaded_artifact_silently_dropped_from_scan_jsons_declared_list(
    java_repo: Path,
) -> None:
    """N4 (seventh cold read, fix round 11 - defense in depth):
    _verify_artifact_digests only ever checked what scan.json ITSELF
    declares - an artifact removed from a tampered/truncated declared
    list (while the file itself is ALSO tampered) would never reach a
    digest check at all, since the verification loop only iterates
    declared entries. Reproduced: drop problems.json's own entry from
    scan.json's declared artifacts AND tamper problems.json's real
    content - the old code would have silently accepted this (nothing
    left to check problems.json against); the new loaded-vs-declared
    assertion catches the drop itself, independent of whatever content
    tamper rides along with it."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    problems_path = outcome.run_dir / "problems.json"
    doc = json.loads(problems_path.read_text(encoding="utf-8"))
    doc["problems"] = [{"problem_id": "fake", "reason_code": "parse_failed",
                         "severity": "warning", "path": None, "detail": "tampered"}]
    problems_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(doc).decode("utf-8"), encoding="utf-8")

    scan_path = outcome.run_dir / "scan.json"
    scan_doc = json.loads(scan_path.read_text(encoding="utf-8"))
    scan_doc["artifacts"] = [
        a for a in scan_doc["artifacts"] if a["name"] != "problems.json"
    ]
    # Also re-derive the run-level content_digest from the now-truncated
    # artifacts list, so it stays SELF-CONSISTENT - isolating that the
    # loaded-vs-declared assertion is what catches this, not the
    # (already separately tested) run-level digest mismatch that a
    # naive truncation would otherwise trip instead.
    scan_doc["content_digest"] = scan_pipeline.digests.run_content_digest(scan_doc["artifacts"])
    new_scan_bytes = scan_pipeline.digests.canonical_json_bytes(scan_doc)
    scan_path.write_text(new_scan_bytes.decode("utf-8"), encoding="utf-8")

    # Re-anchor index.json to the rewritten scan.json's own real bytes/
    # content, so THIS test isolates the loaded-vs-declared assertion
    # specifically - not the (already separately tested) scan.json-own-
    # integrity anchor check that would otherwise fire first on ANY
    # scan.json rewrite.
    index_path = scan_pipeline.paths.index_path(
        scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk"))
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    for run_summary in index_doc["runs"]:
        if run_summary["scan_id"] == outcome.scan_id:
            run_summary["scan_json_byte_sha256"] = scan_pipeline.digests.sha256_bytes(new_scan_bytes)
            run_summary["scan_json_content_digest"] = scan_pipeline.digests.canonical_content_digest(scan_doc)
    index_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(index_doc).decode("utf-8"), encoding="utf-8")

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "problems.json" in result["detail"]


def test_validate_run_before_any_scan_raises_not_scanned(tmp_path: Path) -> None:
    with pytest.raises(scan_pipeline.NotScanned):
        scan_pipeline.validate_run(tmp_path)


def _corrupt_modules_json_missing_unit_id(run_dir: Path) -> None:
    """Envelope-valid (schema_version/artifact_type/scan_id/generated_at
    are all still present and correct) but one record inside "units" is
    missing its own required "unit_id" key - the exact malformed-but-
    envelope-valid shape M-1 (fourth cold read, fix round 6) named."""
    import json

    modules_path = run_dir / "modules.json"
    doc = json.loads(modules_path.read_text(encoding="utf-8"))
    del doc["units"][0]["unit_id"]
    modules_path.write_text(json.dumps(doc), encoding="utf-8")


def test_get_report_refuses_a_malformed_record_instead_of_crashing(java_repo: Path) -> None:
    """M-1 (fourth cold read, fix round 6): a record missing a required
    key raised an untyped KeyError straight through report - record
    conversion happened before validate's own digest check ever got a
    chance to run. Must now raise the same typed ComprehensionError every
    other malformed-input shape already raises, never a traceback."""
    outcome = scan_pipeline.run_scan(java_repo)
    _corrupt_modules_json_missing_unit_id(outcome.run_dir)

    with pytest.raises(scan_pipeline.ComprehensionError, match="malformed record"):
        scan_pipeline.get_report(java_repo)


def test_validate_run_reports_valid_false_for_a_malformed_record_not_a_crash(
    java_repo: Path,
) -> None:
    """M-1 (fourth cold read, fix round 6): validate's own purpose is to
    report on a doubtful run - a raw traceback (exit 1) is indistinguishable
    from validate's own legitimate valid:false (also exit 1) to a scripted
    caller. validate must return valid:false naming the artifact, never
    crash."""
    outcome = scan_pipeline.run_scan(java_repo)
    _corrupt_modules_json_missing_unit_id(outcome.run_dir)

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "modules.json" in result["detail"]
    assert "malformed record" in result["detail"]


def _tamper_modules_json(run_dir: Path) -> None:
    import json

    modules_path = run_dir / "modules.json"
    doc = json.loads(modules_path.read_text(encoding="utf-8"))
    doc["units"] = []
    modules_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(doc).decode("utf-8"), encoding="utf-8")


def test_get_report_refuses_a_tampered_artifact_instead_of_projecting_it_as_truth(
    java_repo: Path,
) -> None:
    """M-1 (third cold read, fix round 5): only ``validate`` ever checked
    a run's declared per-artifact digests - ``report`` projected whatever
    was on disk as truth, with no digest check at all. A tampered
    modules.json must now make ``report`` refuse with the same typed
    error ``validate`` already raises, not silently project the tampered
    content."""
    outcome = scan_pipeline.run_scan(java_repo)
    _tamper_modules_json(outcome.run_dir)

    with pytest.raises(scan_pipeline.ComprehensionError, match="content_digest|byte_sha256"):
        scan_pipeline.get_report(java_repo)


def test_get_status_does_not_verify_unrelated_artifacts_by_design(
    java_repo: Path,
) -> None:
    """N1 (fourth cold read, fix round 6): round 5's M-1 fix made
    ``status`` perform the SAME full per-artifact digest verification
    ``report``/``validate`` do - but the design states an explicit,
    narrower read-cost tier for status: "status verifies the index and
    scan.json... they do not rescan unrelated artifacts on every
    response" (DESIGN-55-comprehension-plane.md, "Validation tiers and
    size ceilings"). A tampered modules.json is therefore NOT caught by
    status (a named, accepted bounded-cost trade-off) - ``report`` and
    ``validate`` still catch the SAME tamper every time, in full."""
    outcome = scan_pipeline.run_scan(java_repo)
    _tamper_modules_json(outcome.run_dir)

    payload = scan_pipeline.get_status(java_repo)
    assert payload["status"] == "complete"

    with pytest.raises(scan_pipeline.ComprehensionError, match="content_digest|byte_sha256"):
        scan_pipeline.get_report(java_repo)
    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False


# ------------------------------------------ MAJOR 3 (fifth cold read, fix round 7):
# scan.json integrity anchoring

def _rewrite_scan_json_whitespace_only(run_dir: Path) -> None:
    """A bytes-only tamper: identical parsed value, different bytes on
    disk - mirrors _rewrite... for modules.json above
    (test_validate_run_catches_a_whitespace_only_rewrite_of_an_artifact),
    now applied to scan.json itself, which nothing previously anchored."""
    import json

    scan_path = run_dir / "scan.json"
    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    scan_path.write_text(json.dumps(doc, indent=4, sort_keys=True), encoding="utf-8")


def _falsify_scan_json_semantically(run_dir: Path) -> None:
    """A semantic tamper: a genuinely different parsed value - falsifies
    completeness and the fingerprint, the strongest positive claim
    status/validate make, exactly the shape the dispatch names."""
    import json

    scan_path = run_dir / "scan.json"
    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    doc["fingerprint_complete"] = True
    doc["whole_scope_fingerprint"] = "0" * 64
    scan_path.write_text(
        scan_pipeline.digests.canonical_json_bytes(doc).decode("utf-8"), encoding="utf-8")


def test_get_status_catches_a_bytes_only_tamper_of_scan_json(java_repo: Path) -> None:
    """MAJOR 3 (fifth cold read, fix round 7): scan.json is the ROOT of
    the integrity chain - every other artifact is verified against a
    digest scan.json itself declares, but nothing external to scan.json
    ever recorded what ITS OWN digest should be. A bytes-only rewrite of
    scan.json (identical parsed content) used to pass status healthy;
    it must now be caught against the byte_sha256 anchor index.json
    records at publish time."""
    outcome = scan_pipeline.run_scan(java_repo)
    _rewrite_scan_json_whitespace_only(outcome.run_dir)

    with pytest.raises(scan_pipeline.ComprehensionError, match="byte_sha256"):
        scan_pipeline.get_status(java_repo)


def test_get_status_catches_a_semantic_tamper_of_scan_json(java_repo: Path) -> None:
    """MAJOR 3 (fifth cold read, fix round 7): a semantic tamper -
    falsifying fingerprint_complete/whole_scope_fingerprint - used to
    pass status healthy and would have made validate report VALID:TRUE,
    its all-verified message, on a modified run. A semantic tamper
    necessarily changes the on-disk bytes too (re-canonicalized from the
    falsified value), so either anchor check catching it is a correct,
    sufficient outcome - not specifically content_digest."""
    outcome = scan_pipeline.run_scan(java_repo)
    _falsify_scan_json_semantically(outcome.run_dir)

    with pytest.raises(scan_pipeline.ComprehensionError, match="content_digest|byte_sha256"):
        scan_pipeline.get_status(java_repo)


def test_validate_run_catches_a_bytes_only_tamper_of_scan_json(java_repo: Path) -> None:
    outcome = scan_pipeline.run_scan(java_repo)
    _rewrite_scan_json_whitespace_only(outcome.run_dir)

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "byte_sha256" in result["detail"]


def test_validate_run_catches_a_semantic_tamper_of_scan_json(java_repo: Path) -> None:
    outcome = scan_pipeline.run_scan(java_repo)
    _falsify_scan_json_semantically(outcome.run_dir)

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "content_digest" in result["detail"] or "byte_sha256" in result["detail"]


def test_get_report_catches_a_semantic_tamper_of_scan_json(java_repo: Path) -> None:
    """The design's own read-path sentence extends this same anchor
    check to report (it verifies "the exact-byte digest... of each
    artifact [it] actually loads", and it loads scan.json)."""
    outcome = scan_pipeline.run_scan(java_repo)
    _falsify_scan_json_semantically(outcome.run_dir)

    with pytest.raises(scan_pipeline.ComprehensionError, match="content_digest|byte_sha256"):
        scan_pipeline.get_report(java_repo)


# --------------------------------- MAJOR (round 7b): aged-out anchor must degrade

def test_get_status_degrades_to_unverified_when_the_index_anchor_has_aged_out(
    java_repo: Path, monkeypatch,
) -> None:
    """MAJOR, availability (round 7b, reviewer-3 delta on 84ef111): the
    index run-summary retention cap (publish._INDEX_RUNS_MAX) can age an
    older run's anchor out of index.json entirely - after which status
    raised the SAME hard refusal a genuine tamper does, PERMANENTLY, for
    an otherwise-untouched, immutable on-disk run (bookkeeping retention
    is not evidence of tampering). A missing anchor must degrade to an
    explicit, labeled unverified outcome and the run must stay readable -
    a present-but-mismatched anchor (verified below) still refuses hard."""
    monkeypatch.setattr(scan_pipeline.publish, "_INDEX_RUNS_MAX", 1)
    aged_out = scan_pipeline.run_scan(java_repo)
    current = scan_pipeline.run_scan(java_repo)  # pushes aged_out's anchor out of the retained window

    status = scan_pipeline.get_status(java_repo, run_id=aged_out.scan_id)
    assert status["scan_json_integrity"] == {
        "state": "unverified", "reason_code": "scan_json_index_anchor_not_recorded",
    }

    # The CURRENT run's own anchor is very much present - a real tamper
    # against IT must still refuse hard; the degrade above applies only
    # to a genuinely missing anchor, never a present-but-wrong one.
    _rewrite_scan_json_whitespace_only(current.run_dir)
    with pytest.raises(scan_pipeline.ComprehensionError, match="byte_sha256"):
        scan_pipeline.get_status(java_repo, run_id=current.scan_id)


def test_validate_run_degrades_to_unverified_and_stays_valid_when_the_index_anchor_has_aged_out(
    java_repo: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(scan_pipeline.publish, "_INDEX_RUNS_MAX", 1)
    aged_out = scan_pipeline.run_scan(java_repo)
    scan_pipeline.run_scan(java_repo)

    result = scan_pipeline.validate_run(java_repo, run_id=aged_out.scan_id)
    assert result["valid"] is True
    assert result["scan_json_integrity"] == {
        "state": "unverified", "reason_code": "scan_json_index_anchor_not_recorded",
    }
    # BLOCKER (round 7c, reviewer-3 delta on 95d9cd8): valid:true's own
    # detail sentence used to claim "all artifacts verified" unqualified
    # even when scan.json's own anchor was never checked - the state
    # existed only in the separate JSON field, invisible anywhere a
    # human actually reads. valid stays true; the sentence must now say
    # so.
    assert "UNVERIFIED" in result["detail"]
    assert "scan_json_index_anchor_not_recorded" in result["detail"]


def test_get_report_degrades_to_unverified_when_the_index_anchor_has_aged_out(
    java_repo: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(scan_pipeline.publish, "_INDEX_RUNS_MAX", 1)
    aged_out = scan_pipeline.run_scan(java_repo)
    scan_pipeline.run_scan(java_repo)

    payload = scan_pipeline.get_report(java_repo, run_id=aged_out.scan_id)
    assert payload["scan_json_integrity"] == {
        "state": "unverified", "reason_code": "scan_json_index_anchor_not_recorded",
    }


def test_validate_run_reports_invalid_for_a_scan_json_missing_a_required_field(
    java_repo: Path,
) -> None:
    """Minor 2 (round 7b): validate never checked scan.json's own scalar
    fields at all (only the separate "artifacts" digest-summary list) -
    it reported valid:true for a scan.json missing a required field
    where status/report both exit 2 typed on the identical input."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    scan_path = outcome.run_dir / "scan.json"
    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    del doc["problem_count"]
    canonical_bytes = scan_pipeline.digests.canonical_json_bytes(doc)
    scan_path.write_bytes(canonical_bytes)
    # Re-sign the index anchor so this test isolates the required-field
    # check from the separate anchor-mismatch check (both are legitimate
    # typed refusals, but this proves the field check specifically).
    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    index_path = scan_pipeline.paths.index_path(comp_dir)
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    for run_summary in index_doc["runs"]:
        if run_summary["scan_id"] == outcome.scan_id:
            run_summary["scan_json_byte_sha256"] = scan_pipeline.digests.sha256_bytes(canonical_bytes)
            run_summary["scan_json_content_digest"] = scan_pipeline.digests.canonical_content_digest(doc)
    index_path.write_text(json.dumps(index_doc), encoding="utf-8")

    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "scan.json" in result["detail"]
    assert "problem_count" in result["detail"]


# ----------------------------------------------------------- failure-path lock release (F-2)

def test_run_scan_failure_surfaces_the_original_error_even_if_release_also_fails(
    java_repo: Path, monkeypatch,
) -> None:
    """F-2 (reviewer-3, PR-B delta review): if the lock release ITSELF
    refuses while unwinding from an original failure, the ORIGINAL failure
    must still be what the caller sees - never silently replaced by the
    release refusal. The release refusal is attached as the cause, not
    substituted for the original exception."""
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import modules_artifact

    class _OriginalFailure(RuntimeError):
        pass

    class _ReleaseFailure(RuntimeError):
        pass

    def _boom_build_modules(*_args, **_kwargs):
        raise _OriginalFailure("original pipeline failure")

    def _boom_release(*_args, **_kwargs):
        raise _ReleaseFailure("release also refused")

    monkeypatch.setattr(modules_artifact, "build_modules", _boom_build_modules)
    monkeypatch.setattr(lockmod, "release_scan_lock", _boom_release)

    with pytest.raises(_OriginalFailure) as excinfo:
        scan_pipeline.run_scan(java_repo)
    assert isinstance(excinfo.value.__cause__, _ReleaseFailure)


# ----------------------------------------------------------- ceilings integration

def test_run_scan_refuses_and_publishes_no_run_when_a_ceiling_is_exceeded(
    java_repo: Path, monkeypatch,
) -> None:
    from agenttalk.comprehension import ceilings

    monkeypatch.setattr(ceilings, "PER_ARTIFACT_BYTES_MAX", 1)
    with pytest.raises(ArtifactLimitExceeded):
        scan_pipeline.run_scan(java_repo)
    from agenttalk.comprehension import publish

    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    doc, _digest = publish.read_current_index(comp_dir)
    assert doc is None  # no run published


# ----------------------------------------------------------- FIX ROUND 16 (twelfth cold read)

def test_readiness_json_declares_the_assessment_state_caveat(java_repo: Path) -> None:
    """FIX ROUND 16 (twelfth cold read, N2 MINOR): CR10-11's own finding
    (assessment_state is currently a CONSTANT - needs_evidence - for
    every unit this slice) lived only in readiness_artifact.py's own
    module docstring and two test docstrings - a reader of readiness.json
    alone had no way to discover it. Published as a real field now."""
    import json

    from agenttalk.comprehension import readiness_artifact

    outcome = scan_pipeline.run_scan(java_repo)
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    assert readiness_doc["assessment_state_caveat"] == readiness_artifact.ASSESSMENT_STATE_CAVEAT


def test_features_json_declares_the_structural_caveat(java_repo: Path) -> None:
    """FIX ROUND 22 (eighteenth cold read, F7 MINOR): the SAME "declare
    it, don't leave it to be independently rediscovered" discipline
    ASSESSMENT_STATE_CAVEAT already established - unmapped_entry_points
    (projector.py) is structurally always empty this slice (build_
    features attaches every entry point to a feature by construction),
    and every feature's own state is always "candidate" (never
    "confirmed" - that requires an unimplemented config.json
    declaration). Published as a real field on features.json now."""
    import json

    from agenttalk.comprehension import features_artifact

    outcome = scan_pipeline.run_scan(java_repo)
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))
    assert features_doc["structural_caveat"] == features_artifact.FEATURES_STRUCTURAL_CAVEAT
    assert all(f["state"] == "candidate" for f in features_doc["features"])


def test_scan_json_declares_the_provenance_caveat(java_repo: Path) -> None:
    """FIX ROUND 24 (twentieth cold read, F8b, declare-not-silently-
    guess): the SAME "declare it, don't leave it to be independently
    rediscovered" discipline ASSESSMENT_STATE_CAVEAT/FEATURES_
    STRUCTURAL_CAVEAT already established - readiness signal producers,
    dependency/readiness evidence pointers, and producer identity's own
    config/policy digest are all empty/absent this slice, none of it
    previously declared anywhere a consumer could discover without
    already knowing to check. Published as a real scan.json field now.

    FIX ROUND 28 (twenty-fourth cold read, F10, completeness): problems.
    json's own records are a DIFFERENT shape of the same gap - a
    producers/evidence list that exists but stays empty (every OTHER
    artifact) versus a producers/evidence field that is structurally
    ABSENT (problems.json has neither field at all). The caveat now
    names this half too, checked here via the string itself rather than
    only equality against the module constant (equality alone would
    stay green even if the problems-half sentence were dropped again -
    it would just be comparing two now-DIFFERENT constants that happen
    to still match each other)."""
    import json

    from agenttalk.comprehension import readiness_artifact

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "problems.json" in scan_doc["provenance_caveat"]
    assert scan_doc["provenance_caveat"] == readiness_artifact.PROVENANCE_CAVEAT


@pytest.mark.parametrize("basename,expected_classification", [
    # the specific, genuinely-always-tooling basename the round-48 N2
    # narrowing kept in _CONFIDENT_INFRASTRUCTURE_BASENAMES.
    ("gradle.properties", ["infrastructure"]),
    # control: an ARBITRARY .properties file (Spring Boot's own
    # convention) - genuine production runtime configuration, never
    # confidently infrastructure since round 48's own N2 narrowing.
    ("application.properties", []),
])
def test_run_scan_the_properties_basename_split_end_to_end(
    java_repo: Path, basename: str, expected_classification: list[str],
) -> None:
    """MICRO-ROUND 48c (F2, measurement vs described intent): reviewer-3
    measured both `application.properties` and an arbitrary `some-
    other.properties` as `classification=[]` (honest, ratified) but
    could not reach the POSITIVE basename case with its own fixtures -
    neither one is a member of `_CONFIDENT_INFRASTRUCTURE_BASENAMES`,
    so neither ever exercises the rule this round 48's own N2 fix
    actually added. Settled here with a genuine measurement of BOTH
    directions, end to end through the real pipeline (not the direct
    build_modules unit test test_comprehension_modules_artifact.py
    already has) - the basename rule DOES exist in code exactly as
    described: a real `gradle.properties` classifies `["infrastructure"]`
    (worker.py's own non-degrading `unsupported_language` tier-3 path,
    then `_is_confident_infrastructure_path`'s basename membership), a
    real `application.properties` classifies `[]` (same tier-3 path,
    but not on the basename allowlist) - no code/description reconcile
    was needed; the gap was fixture coverage only."""
    import json

    (java_repo / basename).write_text(
        "org.gradle.jvmargs=-Xmx2g\n" if basename == "gradle.properties"
        else "datasource.url=jdbc:postgresql://prod-db/app\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    record = next(r for r in modules_doc["units"] if basename in r["paths"])
    assert record["classification"] == expected_classification


def test_run_scan_build_gradle_classifies_infrastructure_end_to_end(java_repo: Path) -> None:
    """MICRO-ROUND 49 (forty-third cold read, C7, wrong-data): the
    wrapper SCRIPT (gradlew) and its own config (gradle.properties)
    were already confident infrastructure, but the build script itself
    - `build.gradle` - measured classification `[]`, an inconsistency
    with no code/description reconcile needed since it satisfies
    `_CONFIDENT_INFRASTRUCTURE_BASENAMES`'s own stated rule exactly the
    same way its two siblings already do."""
    import json

    (java_repo / "build.gradle").write_text(
        "plugins {\n    id 'java'\n}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    record = next(r for r in modules_doc["units"] if "build.gradle" in r["paths"])
    assert record["classification"] == ["infrastructure"]


def test_scan_json_declares_the_route_composition_caveat(java_repo: Path) -> None:
    """FIX ROUND 48 (forty-second cold read, F5 MAJOR completeness, the
    round-35 standard - "the ARTIFACT is the surface"): the deployment
    base-path composition limit (JAX-RS's own @ApplicationPath, a
    Spring DispatcherServlet mapped past bare '/') was declared only in
    source comments, this adapter's own capability description, and the
    design doc - never in a published artifact, even though
    entry_point_kinds right beside it tells a consumer http_route is
    "counted as a served endpoint" with no hint the published name may
    be missing a deployment-level prefix. Every sibling limit already
    publishes in-artifact; this is the one that did not."""
    import json

    from agenttalk.comprehension.adapters import java as java_adapter

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "ApplicationPath" in scan_doc["route_composition_caveat"]
    assert scan_doc["route_composition_caveat"] == java_adapter.ROUTE_COMPOSITION_CAVEAT


def test_run_scan_an_application_path_annotation_publishes_a_non_degrading_signal(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 48b (F2, .cr42-routes shape): an @ApplicationPath
    actually parsed this run publishes a one-line, non-degrading,
    informational `deployment_base_path_declared` problem naming the
    value - never composed into the published route (unchanged from
    round 43/45), but no longer silent either. status stays complete:
    this is informational, the same non-degrading treatment
    `duplicate_route_target` already gets, never a sign anything was
    missed."""
    import json

    web_dir = java_repo / "src" / "main" / "java" / "com" / "acme" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "RestConfig.java").write_text(
        "package com.acme.web;\n"
        "@javax.ws.rs.ApplicationPath(\"/api\")\n"
        "public class RestConfig extends Application {\n"
        "}\n",
        encoding="utf-8",
    )
    (web_dir / "OrderResource.java").write_text(
        "package com.acme.web;\n"
        "@Path(\"/orders\")\n"
        "public class OrderResource {\n"
        "    @GET\n"
        "    @Path(\"/list\")\n"
        "    public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    matching = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "deployment_base_path_declared"]
    assert len(matching) == 1
    assert "/api" in matching[0]["detail"]

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "deployment_base_path_declared" not in scan_doc["degraded_by"]


def test_run_scan_an_absent_application_path_publishes_no_signal(java_repo: Path) -> None:
    """Control: the sample project's own routes (no @ApplicationPath
    anywhere) must never publish `deployment_base_path_declared` - the
    signal is genuine-presence-only, never a per-run blanket note."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(
        p["reason_code"] == "deployment_base_path_declared" for p in problems_doc["problems"])


def test_scan_json_declares_the_work_id_binding_is_unverified(java_repo: Path) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F5 MAJOR, completeness): the
    attended --acknowledge-unignored-private-store override's own
    work_id is caller-supplied and NOT verified against a real work item
    this slice (no work-item subcommand/plane exists yet to check
    existence against) - the SECOND instance this round of
    provenance_caveat's own former "every other unimplemented promise is
    declared" boast going false. Declared explicitly now (as an
    enumerated item, not a boast), checked via the string itself rather
    than only equality against the module constant, the same discipline
    the sibling problems.json test above already follows."""
    import json

    from agenttalk.comprehension import readiness_artifact

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert "work_id" in scan_doc["provenance_caveat"]
    assert "NOT verified against a real work item" in scan_doc["provenance_caveat"]
    assert scan_doc["provenance_caveat"] == readiness_artifact.PROVENANCE_CAVEAT


def test_scan_json_declares_the_record_count_definition(java_repo: Path) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F8, declare-not-silently-
    leave-to-a-docstring): round 26's own F7 note declared record_count's
    definition (summed across a document's own several record kinds, not
    the length of any one named array) only in a docstring a consumer of
    the published artifact never sees - the same "declare it, don't
    leave it to be independently rediscovered" discipline every other
    caveat here already establishes. Published as a real scan.json field
    now."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["record_count_definition"] == scan_pipeline.RECORD_COUNT_DEFINITION


def test_modules_json_declares_the_classification_caveat(java_repo: Path) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F3 JUDGE): the SAME
    "declare it, don't leave it to be independently rediscovered"
    discipline ASSESSMENT_STATE_CAVEAT/FEATURES_STRUCTURAL_CAVEAT/
    PROVENANCE_CAVEAT already established - a `complete` run may still
    carry an encoding-undecodable, non-adapter .xml file with NO decided
    classification at all. Published as a real modules.json field now."""
    import json

    from agenttalk.comprehension import modules_artifact

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    assert modules_doc["classification_caveat"] == modules_artifact.CLASSIFICATION_CAVEAT


def test_scan_json_declares_the_fingerprint_caveat(java_repo: Path) -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F3 MINOR, completeness): the
    SAME "declare it, don't leave it to be independently rediscovered"
    discipline the other *_CAVEAT fields already establish -
    whole_scope_fingerprint's own entry-level-not-content-level
    sensitivity for generated_or_vendor/resource_limit_oversized (and
    dependency_cache's own complete absence from it) was previously
    declared only in a discovery.py comment while fingerprint_complete
    published true. Published as a real scan.json field now."""
    import json

    from agenttalk.comprehension import discovery

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["fingerprint_caveat"] == discovery.FINGERPRINT_CAVEAT


def test_scan_json_declares_the_secret_patterns_caveat(java_repo: Path) -> None:
    """FIX ROUND 35 (twenty-ninth cold read, F4 MINOR, completeness): the
    SAME in-artifact declaration discipline - the secret-file exclusion
    list's own provisional-set boundary previously lived only in a
    discovery.py comment. Published as a real scan.json field now."""
    import json

    from agenttalk.comprehension import discovery

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["secret_patterns_caveat"] == discovery.SECRET_PATTERNS_CAVEAT


def test_scan_json_declares_the_duplicate_route_target_caveat(java_repo: Path) -> None:
    """FIX ROUND 36 (thirtieth cold read, F5 MINOR, completeness): the
    SAME in-artifact declaration discipline - duplicate_route_target's
    own file-scoped check boundary (never cross-checked against a
    @WebServlet annotation route, never cross-checked against a second
    web.xml) previously lived only in a java.py source comment.
    Published as a real scan.json field now."""
    import json

    from agenttalk.comprehension.adapters import java as java_adapter

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["duplicate_route_target_caveat"] == java_adapter.DUPLICATE_ROUTE_TARGET_CAVEAT


def test_scan_json_degraded_by_is_empty_on_a_complete_run(java_repo: Path) -> None:
    """FIX ROUND 35 (twenty-ninth cold read, F5 MINOR, completeness):
    regression control - a genuinely complete run names no reasons at all."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert outcome.status == "complete"
    assert scan_doc["degraded_by"] == []


def test_scan_json_degraded_by_names_the_reason_that_actually_degraded(
    java_repo: Path,
) -> None:
    """FIX ROUND 35 (F5 MINOR, completeness): `degraded` on its own never
    told a consumer WHICH of the (up to seven, and growing) independent
    warning-severity reasons actually drove the degradation - severity
    does not discriminate. Mirrors the reader's own duplicate-qualified-
    name reactor shape."""
    import json

    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app" / "Boot.java").write_text(
        "package com.acme.app;\nimport com.acme.Config;\nclass Boot {}\n", encoding="utf-8")
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert outcome.status == "degraded"
    assert scan_doc["degraded_by"] == ["duplicate_qualified_name"]


def test_scan_json_degraded_by_combines_multiple_independent_reasons(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 35 (F5 MINOR, completeness): two INDEPENDENT degrading
    sources in the SAME run (a case-fold collision, discovery-level, and a
    duplicate qualified name, modules-level) must both be named, sorted -
    proving the set aggregates across OR-chain terms rather than only
    ever reporting the first one found.

    FIX ROUND 37b (thirty-first cold read's own reclassification -
    URGENT, wrong-data, platform divergence): this test's own injected
    colliding path used to be a SINGLE phantom, "src/main/java/p/
    APP.JAVA" - never written to disk, but case-folding IDENTICAL to
    the java_repo fixture's own real "src/main/java/p/App.java".
    worker.process_paths's own file read (resolved.read_bytes(),
    worker.py) is not mocked here - only discovery.enumerate_scope is -
    so on a case-INSENSITIVE filesystem (Windows, this dev host) that
    read SILENTLY SUCCEEDED, resolving "APP.JAVA" to the real "App.java"
    bytes; on a case-SENSITIVE one (Linux, the real CI legs) the
    identical read genuinely FAILS (no file exists with that exact
    case), recording an EXTRA parse_failed problem the Windows run never
    saw - divergent published facts (the exact class of bug this whole
    round's own F2/F4 fixes were closing elsewhere) for the identical
    test, previously invisible because CI evidence was misread as an
    unrelated infra stall. Fixed at the fixture: BOTH colliding paths are
    now purely phantom (neither ever exists on disk, in any case,
    on any platform), so the read fails identically everywhere,
    deterministically producing parse_failed on both platforms - the
    correct, platform-independent expectation is that all THREE reasons
    are named, not two."""
    import dataclasses
    import json

    from agenttalk.comprehension import discovery as discoverymod

    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")

    real_enumerate_scope = discoverymod.enumerate_scope

    def _enumerate_scope(root, comprehension_dir):
        result = real_enumerate_scope(root, comprehension_dir)
        # Both phantom - neither is ever written to disk, so their own
        # case-fold collision is proven at the discovery layer alone,
        # never accidentally resolved against a real file by the OS's
        # own case-insensitive path lookup (worker.process_paths's own
        # file read is not mocked here).
        first_phantom = discoverymod.EnumeratedFile(
            relative_path="src/main/java/p/Ghost.java", byte_count=1, content_digest="aaa")
        second_phantom = discoverymod.EnumeratedFile(
            relative_path="src/main/java/p/GHOST.JAVA", byte_count=1, content_digest="bbb")
        return dataclasses.replace(result, files=[*result.files, first_phantom, second_phantom])

    monkeypatch.setattr(scan_pipeline.discovery, "enumerate_scope", _enumerate_scope)

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert outcome.status == "degraded"
    assert scan_doc["degraded_by"] == ["case_collision", "duplicate_qualified_name", "parse_failed"]


def test_run_scan_a_duplicate_qualified_name_publishes_ambiguous_import_and_a_problem(
    java_repo: Path,
) -> None:
    """FIX ROUND 16 (twelfth cold read, B1 BLOCKER, wrong-data): mirrors
    reviewer-3's own ``.cr12-dup`` fixture - two modules each declaring
    ``com.acme.Config``, imported by a third file. End-to-end: the
    import edge must publish ambiguous (never a confident external
    claim over a real naming collision), the two colliding components
    must share a conflict_id, and problems.json must name the collision
    exactly once (never once per colliding side)."""
    import json

    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app" / "Boot.java").write_text(
        "package com.acme.app;\nimport com.acme.Config;\nclass Boot {}\n", encoding="utf-8")
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "com.acme.Config")
    assert import_edge["resolution_state"] == "ambiguous"
    assert len(import_edge["candidate_unit_ids"]) == 2

    config_units = [u for u in modules_doc["units"] if u.get("qualified_name") == "com.acme.Config"]
    assert len(config_units) == 2
    assert config_units[0]["conflict_id"] is not None
    assert config_units[0]["conflict_id"] == config_units[1]["conflict_id"]
    # MICRO-ROUND 29b (reviewer-3's own matrix): a genuine FQN collision
    # must still publish its own row, unaffected by the fix that
    # suppresses the SIBLING (descriptor-name) conflict_kind's second,
    # false row.
    assert config_units[0]["conflict_kind"] == "duplicate_qualified_name"

    dup_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_qualified_name"]
    assert len(dup_problems) == 1
    assert dup_problems[0]["qualified_name"] == "com.acme.Config"
    assert outcome.status == "degraded"


def test_a_conflicted_units_readiness_signals_stay_unknown_not_confident(
    java_repo: Path,
) -> None:
    """FIX ROUND 22 (eighteenth cold read, F4, wrong-data, narrow
    trigger): the reader's own .cr18-dup shape - a unit carrying a
    conflict_id (a genuine duplicate-qualified-name collision) used to
    publish CONFIDENT readiness on every signal, even though the
    design's own merge rule 4 says a verbatim dependent's readiness
    stays unknown until the conflict is explicitly resolved, and
    round 21c's own 2+-claimant skip means neither claimant ever
    receives this run's own real entry-point/feature facts either. Both
    colliding units now report unknown/duplicate_qualified_name on
    dependencies_resolved/entry_points_mapped/feature_linked/test_
    evidence_located - the conflict problem itself is still recorded
    (unchanged), and a non-conflicted twin class in the SAME run stays
    fully confident (this override is scoped to conflicted units only)."""
    import json

    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app" / "Boot.java").write_text(
        "package com.acme.app;\nimport com.acme.Config;\nclass Boot {}\n", encoding="utf-8")
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modA" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "modB" / "src" / "main" / "java" / "com" / "acme" / "Config.java").write_text(
        "package com.acme;\nclass Config {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "other").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "other" / "Plain.java").write_text(
        "package com.acme.other;\nclass Plain {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    conflict_checks = (
        "dependencies_resolved", "entry_points_mapped", "feature_linked",
        "test_evidence_located",
    )
    config_unit_ids = [
        u["unit_id"] for u in modules_doc["units"]
        if u.get("qualified_name") == "com.acme.Config"]
    assert len(config_unit_ids) == 2
    for unit_id in config_unit_ids:
        for check in conflict_checks:
            signal = next(
                s for s in readiness_doc["signals"]
                if s["unit_id"] == unit_id and s["check"] == check)
            assert signal["stored_status"] == "unknown", (unit_id, check)
            assert signal["reason_code"] == "duplicate_qualified_name", (unit_id, check)
        source_signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == unit_id and s["check"] == "source_understood")
        assert source_signal["reason_code"] != "duplicate_qualified_name"

    # FIX ROUND 23 (nineteenth cold read, F4 MINOR, wrong-data + a
    # stale claim in round 22's own comment): a FILE never carries a
    # conflict_id itself - the reader's own .cr19-dup four-row shape
    # (2 components + their 2 single-type-owning files) - both
    # Config.java FILE units (single-top-level-type, the one component
    # being the conflicted claimant) must ALSO report unknown on
    # dependencies_resolved/test_evidence_located now, not just the
    # component itself.
    config_file_unit_ids = [
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == "file" and u["display_name"] == "Config.java"]
    assert len(config_file_unit_ids) == 2
    for unit_id in config_file_unit_ids:
        for check in ("dependencies_resolved", "test_evidence_located"):
            signal = next(
                s for s in readiness_doc["signals"]
                if s["unit_id"] == unit_id and s["check"] == check)
            assert signal["stored_status"] == "unknown", (unit_id, check)
            assert signal["reason_code"] == "duplicate_qualified_name", (unit_id, check)

    # The conflict problem itself is still recorded, unaffected.
    assert any(p["reason_code"] == "duplicate_qualified_name" for p in problems_doc["problems"])

    # FIX ROUND 24 (twentieth cold read, F8a, design-promised, taken):
    # the design's own item 4 promises problems.json records "that
    # conflict_id, every claimant, and the disputed fields" - a consumer
    # could not join the problem back to the two units that DO share a
    # conflict_id at all. Both colliding units' own modules.json
    # conflict_id must match the problem's own conflict_id exactly.
    conflict_problem = next(
        p for p in problems_doc["problems"] if p["reason_code"] == "duplicate_qualified_name")
    unit_conflict_ids = {
        u["conflict_id"] for u in modules_doc["units"] if u["unit_id"] in config_unit_ids}
    assert unit_conflict_ids == {conflict_problem["conflict_id"]}
    assert conflict_problem["conflict_id"] is not None

    # A non-conflicted twin class in the SAME run stays fully confident.
    plain_unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == "component" and u["display_name"] == "Plain")
    plain_dependencies_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == plain_unit_id and s["check"] == "dependencies_resolved")
    assert plain_dependencies_signal["reason_code"] != "duplicate_qualified_name"


def test_a_conflicted_components_own_multi_type_file_also_stays_unknown_r4(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 23b (reviewer-3's own R4 consistency ask, taken): round
    23's own F4 fix only extended the file-level override to a SINGLE
    top-level-type file - a MULTI-type file (the reviewer's own
    Multi.java repro) kept publishing a CONFIDENT dependencies_resolved
    satisfied (TRUE about the file's own unioned edges - a clean sibling
    class really does have a resolved import), while entry_points_
    mapped/feature_linked's own file-aggregation ALREADY reports unknown
    for the identical file, since one of its two top-level types is
    itself conflicted - two different policies for the same 2+-children
    shape on one record. Widened to match: ANY conflicted component
    anywhere in the file's own containment chain now overrides
    dependencies_resolved/test_evidence_located too, regardless of how
    many top-level types the file declares."""
    import json

    (java_repo / "modC" / "src" / "main" / "java" / "com" / "acme" / "multiconflict").mkdir(
        parents=True)
    (java_repo / "modC" / "src" / "main" / "java" / "com" / "acme" / "multiconflict" / "Dup.java"
     ).write_text("package com.acme.multiconflict;\nclass Dup {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "multiconflict").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "other").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "other" / "Plain2.java").write_text(
        "package com.acme.other;\nclass Plain2 {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "multiconflict" / "Dup.java"
     ).write_text(
        "package com.acme.multiconflict;\n"
        "import com.acme.other.Plain2;\n"
        "class Dup {}\n"
        "class Sibling {\n"
        "    Plain2 p;\n"
        "}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    multi_file_unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == "file" and u["display_name"] == "Dup.java"
        and len([
            m for m in modules_doc["units"]
            if m["kind"] == "component" and m.get("container_unit_id") == u["unit_id"]
        ]) == 2
    )
    for check in ("dependencies_resolved", "test_evidence_located"):
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == multi_file_unit_id and s["check"] == check)
        assert signal["stored_status"] == "unknown", check
        assert signal["reason_code"] == "duplicate_qualified_name", check


def test_a_component_with_a_conflicted_nested_descendant_also_stays_unknown_f6(
    java_repo: Path,
) -> None:
    """FIX ROUND 24 (twentieth cold read, F6 MINOR, consistency,
    ``.cr20-nest``): round 22's own F1 invariant ("never more confident
    than your components") was only ever applied at the FILE level -
    `Outer` (a real, unique `@WebServlet` with its own resolved import)
    contains a statically NESTED class `Inner`, whose own qualified name
    (`com.acme.Outer.Inner`) collides with an entirely unrelated
    top-level class also named `Inner` declared in a package literally
    named `com.acme.Outer` (a real, if unusual, shaded/relocated-package
    shape this producer's own coarse qualified-name computation cannot
    tell apart from genuine nesting) - `Outer` ITSELF is never a
    conflict claimant (its own qualified name `com.acme.Outer` is
    unique), so the pre-existing per-unit `conflict_id` override never
    fires for it directly; only the NEW component-level nested-
    descendant aggregation this round adds can catch it. The enclosing
    FILE already correctly rolled up to unknown via the existing file-
    level aggregation - `Outer`, one level in, must now too."""
    import json

    (java_repo / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "other").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "other" / "Plain3.java").write_text(
        "package com.acme.other;\nclass Plain3 {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "Outer.java").write_text(
        "package com.acme;\n"
        "import com.acme.other.Plain3;\n"
        '@WebServlet("/outer")\n'
        "class Outer {\n"
        "    Plain3 p;\n"
        "    static class Inner {}\n"
        "}\n",
        encoding="utf-8",
    )
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "Outer").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "Outer" / "Inner.java").write_text(
        "package com.acme.Outer;\nclass Inner {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))

    outer_unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == "component" and u["qualified_name"] == "com.acme.Outer")
    assert next(
        u for u in modules_doc["units"] if u["unit_id"] == outer_unit_id
    )["conflict_id"] is None

    for check in ("dependencies_resolved", "entry_points_mapped", "feature_linked",
                  "test_evidence_located"):
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == outer_unit_id and s["check"] == check)
        assert signal["stored_status"] == "unknown", check
        assert signal["reason_code"] == "duplicate_qualified_name", check


def test_run_scan_a_pom_extracting_nothing_reports_source_understood_unknown_f1b(
    java_repo: Path,
) -> None:
    """FIX ROUND 24 (twentieth cold read, F1b, wrong-data): a pom that
    parses without error but registers no coordinate/edge/reactor-module
    at all must publish `source_understood`/`dependencies_resolved` as
    unknown, not the confident satisfied a bare `language != unknown`
    check used to derive - the exact mechanism that let this round's
    own F1 namespace-prefixed pom silently vanish while reading as a
    complete, zero-problem run."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project>\n  <modelVersion>4.0.0</modelVersion>\n</project>\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))

    assert any(p["reason_code"] == "no_pom_facts_extracted" for p in problems_doc["problems"])
    pom_unit_id = next(u["unit_id"] for u in modules_doc["units"] if "pom.xml" in u["paths"])
    for check in ("source_understood", "dependencies_resolved"):
        signal = next(
            s for s in readiness_doc["signals"]
            if s["unit_id"] == pom_unit_id and s["check"] == check)
        assert signal["stored_status"] == "unknown", check
        assert signal["reason_code"] == "adapter_no_pom_facts_extracted", check


def test_run_scan_an_import_of_a_binary_sniffed_excluded_file_is_unresolved_not_external(
    java_repo: Path,
) -> None:
    """FIX ROUND 19 (fifteenth cold read, F1 BLOCKER, wrong-data): mirrors
    the reader's own ``.cr15-d`` UTF-16-import shape - a UTF-16-encoded
    ``.java`` file trips discovery's binary sniff (round 18's own F6)
    and is excluded outright, recorded as a full FILE path (WITH its own
    ``src/main/java/...`` scaffolding) in ``excluded_roots``. An import
    of the type it would have declared has no scaffolding of its own
    (``p.Legacy``) - the old ``_excluded_region_match`` only matched
    when an excluded root's path started with the SAME string as the
    scaffolding-free qualified name, which never happens once the
    excluded root carries real scaffolding. Must resolve unresolved,
    never a confident external claim, and this run's dependency_summary
    must not silently count it third-party."""
    import json

    (java_repo / "src" / "main" / "java" / "p" / "Legacy.java").write_bytes(
        "package p;\nclass Legacy {}\n".encode("utf-16"))
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport p.Legacy;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "p.Legacy")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None


def test_run_scan_an_import_of_an_ant_style_excluded_vendor_package_is_unresolved_not_external(
    java_repo: Path,
) -> None:
    """FIX ROUND 19 (fifteenth cold read, F1 BLOCKER, wrong-data): mirrors
    the reader's own ``.cr15-c`` Ant/Eclipse shape - a bare ``src/``
    source root (never ``src/main/java``, so round 18's own F1
    recognition does not apply here) has a domain package literally
    named ``vendor`` excluded as ``generated_or_vendor`` at
    ``src/vendor`` - a directory path carrying the ``src/`` scaffolding
    a wildcard-free qualified name never spells. Same bug, same fix,
    different excluded-root shape (a DIRECTORY, not a single file)."""
    import json

    (java_repo / "src" / "vendor" / "Helper.java").parent.mkdir(parents=True, exist_ok=True)
    (java_repo / "src" / "vendor" / "Helper.java").write_text(
        "package vendor;\nclass Helper {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport vendor.Helper;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))

    assert any(
        e["path"] == "src/vendor" and e["category"] == "generated_or_vendor"
        for e in scan_doc["excluded_roots"]
    )
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "vendor.Helper")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None


def test_run_scan_a_wildcard_import_of_an_ant_style_excluded_vendor_package_is_unresolved(
    java_repo: Path,
) -> None:
    """FIX ROUND 19 (fifteenth cold read, F1 BLOCKER sweep): mirrors the
    same Ant/Eclipse shape as the plain-import companion test above, but
    through the wildcard-import sibling predicate
    (_excluded_region_package_match), which had the identical repo-root
    anchoring bug."""
    import json

    (java_repo / "src" / "vendor").mkdir(parents=True, exist_ok=True)
    (java_repo / "src" / "vendor" / "Helper.java").write_text(
        "package vendor;\nclass Helper {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport vendor.*;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "vendor.*")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None


def test_run_scan_a_vendored_reactor_module_reports_unresolved_via_the_reactor_rule(
    java_repo: Path,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR, wrong-data - THE
    POISON RULE + THE REACTOR RULE): mirrors the reader's own .cr16-c
    vendor-module reactor shape - the root pom declares
    <module>vendor</module>, and "vendor" is EXCLUDED outright
    (matches generated_or_vendor by name) - discovery records it as a
    bare directory path ("vendor"), while the unwalked module's own
    source lives arbitrarily deeper (vendor/src/main/java/...), a
    relationship no string match over a qualified name alone could ever
    recover. The app module's own build edge (a <dependency> on the
    vendored module's coordinate) AND an import edge into the vendored
    module's own package must BOTH resolve unresolved - never a
    confident third-party EXTERNAL claim over a declared, in-reactor
    module - and the run must degrade via the reactor rule's own named
    problem, naming the module."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<packaging>pom</packaging>"
        "<modules><module>vendor</module><module>app</module></modules>"
        "</project>",
        encoding="utf-8",
    )
    (java_repo / "vendor" / "src" / "main" / "java" / "com" / "acme" / "vendor").mkdir(
        parents=True)
    (java_repo / "vendor" / "src" / "main" / "java" / "com" / "acme" / "vendor" / "Widget.java"
     ).write_text("package com.acme.vendor;\nclass Widget {}\n", encoding="utf-8")
    (java_repo / "vendor" / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>vendor-lib</artifactId></project>",
        encoding="utf-8",
    )
    (java_repo / "app").mkdir(parents=True)
    (java_repo / "app" / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>app</artifactId>"
        "<dependencies><dependency>"
        "<groupId>com.acme</groupId><artifactId>vendor-lib</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    (java_repo / "app" / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "app" / "src" / "main" / "java" / "com" / "acme" / "app" / "Consumer.java"
     ).write_text(
        "package com.acme.app;\nimport com.acme.vendor.Widget;\nclass Consumer {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    reactor_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "module_directory_excluded"]
    assert len(reactor_problems) == 1
    assert reactor_problems[0]["path"] == "pom.xml"

    # FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-rule
    # VISIBILITY): the reactor rule's own problem is JOINED, not
    # replaced, by the new run-wide poison-visibility record naming this
    # SAME pom as one of the poison rule's own triggers.
    poison_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "externality_suppressed"]
    assert any(p["path"] == "pom.xml" for p in poison_problems)

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True
    assert any(r["path"] == "pom.xml" for r in scan_doc["externality_suppressed_roots"])

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    build_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "build" and r.get("target_unresolved") == "com.acme:vendor-lib")
    assert build_edge["resolution_state"] == "unresolved"
    assert build_edge.get("target_external") is None

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "com.acme.vendor.Widget")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None

    # FIX ROUND 20 (sixteenth cold read, m2 JUDGE, completeness): the
    # poison rule protects import RESOLUTION above, but a report --json
    # caller still had no way to see WHICH directory was excluded and
    # judge for itself whether it might hold real source - scan.json
    # already computes and bounds this (M4/round 6); the projection now
    # surfaces it too, the same way `exclusions` already is.
    report = scan_pipeline.get_report(java_repo)
    assert {"path": "vendor", "category": "generated_or_vendor"} in report["excluded_roots"]
    assert report["excluded_roots_omitted_count"] == 0


def test_run_scan_a_pom_declared_module_named_build_reports_unresolved_via_the_reactor_rule(
    java_repo: Path,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR, wrong-data - THE
    REACTOR RULE): mirrors the reader's own .cr16-b root build/ module
    shape - a module directory that HAPPENS to be literally named
    "build" (one of the generated/vendor names, excluded by name
    regardless of Maven's own explicit declaration otherwise). The
    build tool's own declaration is authoritative positive evidence,
    stronger than the generic peek - the reactor rule must fire (and
    degrade) regardless of what the peek's own generic extension-sniff
    happens to find."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<packaging>pom</packaging>"
        "<modules><module>build</module></modules>"
        "</project>",
        encoding="utf-8",
    )
    (java_repo / "build").mkdir(parents=True)
    (java_repo / "build" / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>build-module</artifactId></project>",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    reactor_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "module_directory_excluded"]
    assert len(reactor_problems) == 1

    # FIX ROUND 20b (THE MAJOR - poison-rule VISIBILITY): same additive
    # poison record as the vendored-reactor-module test above.
    poison_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "externality_suppressed"]
    assert any(p["path"] == "pom.xml" for p in poison_problems)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True


def test_run_scan_reactor_modules_outside_root_and_missing_are_both_recorded(
    java_repo: Path,
) -> None:
    """FIX ROUND 35 (twenty-ninth cold read, F3 MAJOR, completeness,
    .cr29-reactor2 verbatim): a <module> pointing OUTSIDE the scanned
    root (climbing above it via "..") or at a NONEXISTENT directory used
    to leave NO trace at all - no boundary, no problem, complete/0 -
    even though the excluded-region shape (the sibling tests above)
    correctly records module_directory_excluded. Both new shapes now get
    the same visible, degrading treatment."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<packaging>pom</packaging>"
        "<modules>"
        "<module>../outside-repo</module>"
        "<module>nonexistent-dir</module>"
        "</modules>"
        "</project>",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    outside_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "module_outside_scan_root"]
    assert len(outside_problems) == 1
    assert outside_problems[0]["path"] == "pom.xml"

    missing_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "module_directory_missing"]
    assert len(missing_problems) == 1
    assert missing_problems[0]["path"] == "pom.xml"

    # Same externality-poisoning consequence the excluded-region shape
    # already gets - a reactor member this run cannot see is positive
    # evidence of real, unmodeled first-party source.
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True


def test_run_scan_each_reactor_trigger_gets_its_own_truthful_suppression_detail_and_id(
    java_repo: Path,
) -> None:
    """FIX ROUND 36 (thirtieth cold read, F1 BLOCKER + F2 MAJOR,
    wrong-data, .cr30-reactor-outside/-missing/-excluded verbatim): the
    externality_suppressed detail synthesized from reactor_rule_problems
    used to be ONE fixed literal - "this pom's own declared <module>
    resolves into an excluded region" - emitted identically for all
    three reactor triggers. TRUE only for module_directory_excluded;
    MEASURED FALSE for module_outside_scan_root and module_directory_
    missing (no excluded region exists in either case) - a migration
    reader sent to inspect an exclusion that does not exist. One pom
    declaring all three bad <module> shapes at once also proves F2: N
    bad reactor entries in the SAME pom must publish N distinct
    problem_ids, never collide on one shared literal detail."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<packaging>pom</packaging>"
        "<modules>"
        "<module>../outside-repo</module>"
        "<module>nonexistent-dir</module>"
        "<module>vendor</module>"
        "</modules>"
        "</project>",
        encoding="utf-8",
    )
    (java_repo / "vendor" / "src" / "main" / "java" / "com" / "acme" / "vendor").mkdir(
        parents=True)
    (java_repo / "vendor" / "src" / "main" / "java" / "com" / "acme" / "vendor" / "Widget.java"
     ).write_text("package com.acme.vendor;\nclass Widget {}\n", encoding="utf-8")
    (java_repo / "vendor" / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>vendor-lib</artifactId></project>",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    # "vendor" is itself excluded by name AND holds real code, so
    # discovery's own generic peek ALSO contributes its own "peek_
    # positive" externality_suppressed record alongside the three
    # reactor-rule-driven ones this test targets - filtered to the
    # <module>-naming ones specifically.
    suppression_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "externality_suppressed" and "<module>" in p["detail"]]
    assert len(suppression_problems) == 3

    by_module = {}
    for p in suppression_problems:
        if "../outside-repo" in p["detail"]:
            by_module["outside"] = p
        elif "nonexistent-dir" in p["detail"]:
            by_module["missing"] = p
        elif "<module>vendor</module>" in p["detail"]:
            by_module["excluded"] = p
    assert set(by_module) == {"outside", "missing", "excluded"}

    assert "resolves OUTSIDE this run's own scanned root" in by_module["outside"]["detail"]
    assert "excluded region" not in by_module["outside"]["detail"]

    assert "does not exist as a directory" in by_module["missing"]["detail"]
    assert "excluded region" not in by_module["missing"]["detail"]

    assert "resolves into a region this run excluded outright" in by_module["excluded"]["detail"]

    # F2: three genuinely distinct facts about the SAME pom must publish
    # three distinct problem_ids - never collide on one shared literal.
    problem_ids = {p["problem_id"] for p in suppression_problems}
    assert len(problem_ids) == 3

    # F2 "honest count": scan.json's own problem_count must equal the
    # actual number of records in problems.json - never overstated by a
    # since-fixed id collision, never understated by an over-eager
    # coalesce of genuinely distinct records.
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["problem_count"] == len(problems_doc["problems"])

    # FIX ROUND 37 (thirty-first cold read, F7 LOW, wrong-data, .cr31-
    # reactor verbatim): externality_suppressed_roots is a SET of (path,
    # trigger) roots this run poisoned externality over, never an event
    # log - the SAME pom's own THREE reactor triggers above used to
    # publish three byte-identical {"path": "pom.xml", "trigger":
    # "reactor"} rows here; deduped to one.
    reactor_roots = [
        r for r in scan_doc["externality_suppressed_roots"]
        if r["path"] == "pom.xml" and r["trigger"] == "reactor"]
    assert len(reactor_roots) == 1


def test_run_scan_two_missing_reactor_modules_sharing_a_long_prefix_get_distinct_ids(
    java_repo: Path,
) -> None:
    """FIX ROUND 42 (thirty-sixth cold read, F1 MAJOR, .cr36-reactor,
    wrong-data): round 41's own F4 fix bounded `_problem_record`'s own
    `detail` unconditionally at the chokepoint, but never audited the
    templates it newly bounds against round 40's own invariant ("every
    template places its distinguishing datum within the first 200
    chars"). The reactor-rule templates put `module_path` right after a
    ~27-character fixed prefix ("this pom declares <module>") AND passed
    NO `qualified_name` at all - so two DIFFERENT declared `<module>`
    paths sharing a common prefix of ~180+ characters (an ordinary shape
    for a deep-enterprise reactor's own directory layout) diverge only
    PAST the 200-char bound: both truncate to the IDENTICAL bounded
    detail, and with no other distinguishing datum (same reason_code,
    same pom path, qualified_name=None), the two collide on problem_id
    and the deliberate byte-identical coalescer silently merges them -
    one declared-but-missing reactor member vanishes with zero signal,
    problem_count understates the true count, validate blesses it.
    Fixed by threading a synthetic qualified_name carrying the FULL,
    raw (never-bounded) module path - the same pattern `duplicate_
    route_target`'s own synthetic qualified_name already used safely."""
    import json

    prefix = "sub/" * 45  # 180 characters, comfortably past the 200-char bound once wrapped
    assert len(prefix) == 180
    module_a = f"{prefix}dirA"
    module_b = f"{prefix}dirB"
    assert module_a != module_b

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<packaging>pom</packaging>"
        "<modules>"
        f"<module>{module_a}</module>"
        f"<module>{module_b}</module>"
        "</modules>"
        "</project>",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    missing_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "module_directory_missing"]
    assert len(missing_problems) == 2, (
        "two genuinely different reactor modules must not coalesce into one record "
        "merely because they truncate identically"
    )
    assert len({p["problem_id"] for p in missing_problems}) == 2
    assert {p["qualified_name"] for p in missing_problems} == {
        f"pom.xml#module#{module_a}", f"pom.xml#module#{module_b}",
    }

    suppression_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "externality_suppressed" and "<module>" in p["detail"]]
    assert len(suppression_problems) == 2
    assert len({p["problem_id"] for p in suppression_problems}) == 2

    # Honest count: scan.json's own problem_count must equal the actual
    # number of records in problems.json.
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["problem_count"] == len(problems_doc["problems"])


def test_run_scan_a_pom_with_an_unrecoverable_own_coordinate_poisons_externality_run_wide(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 35b (reviewer-3 delta on `32a5fa6`, R1c wrong-data,
    the reviewer's own undefined-entity shape verbatim): round 35's own
    F1 fix stopped a CDATA/entity-broken pom coordinate from resolving a
    genuine intra-reactor SIBLING edge to a fabricated CONFIDENT EXTERNAL
    claim directly - but the poison OR-chain (`externality_poisoned`) was
    never taught the new `coordinate_value_unrecoverable` reason code, so
    a genuinely UNRELATED external-registry-miss dependency elsewhere in
    the SAME run still resolved confident external, on a run that itself
    RECORDS it cannot read this pom's own coordinate - the same
    epistemic gap F3's own reactor-rule problems already poison on
    (in-scan content this run cannot identify at all). A single pom with
    both an unrecoverable own coordinate AND an ordinary external
    dependency: the dependency's own build edge must resolve unresolved,
    never a confident external claim, while this run cannot vouch its
    own reactor is fully known."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId>"
        "<artifactId>bad&undefinedent;name</artifactId>"
        "<dependencies><dependency>"
        "<groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    coordinate_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "coordinate_value_unrecoverable"]
    assert len(coordinate_problems) == 1
    assert coordinate_problems[0]["path"] == "pom.xml"

    poison_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "externality_suppressed"]
    assert any(p["path"] == "pom.xml" for p in poison_problems)

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True
    assert any(
        r["path"] == "pom.xml" and r["trigger"] == "coordinate_unrecoverable"
        for r in scan_doc["externality_suppressed_roots"])
    assert "externality_suppressed" in scan_doc["degraded_by"]

    dependencies_doc = json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    build_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "build" and r.get("target_unresolved") == "org.slf4j:slf4j-api")
    assert build_edge["resolution_state"] == "unresolved"
    assert build_edge.get("target_external") is None


def test_run_scan_two_undecodable_coordinates_in_one_pom_get_distinct_suppression_ids(
    java_repo: Path,
) -> None:
    """FIX ROUND 36 (thirtieth cold read, F2 MAJOR part (b), wrong-data):
    the reactor case was not the only way to reach N byte-identical
    externality_suppressed records sharing ONE problem_id - the
    coordinate branch keyed its own suppression detail only on
    relative_path, so a pom with BOTH its own project-level artifactId
    AND its <parent> groupId undecodable (two independent WorkerProblems
    for the SAME path) published two byte-identical suppression records
    colliding on one id, understating the true problem count the same
    way the reactor shape did."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project>\n"
        "  <parent><groupId>com&badent;other</groupId>"
        "<artifactId>other-parent</artifactId></parent>\n"
        "  <artifactId>bad&badent2;name</artifactId>\n"
        "</project>\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    coordinate_problems = [
        p for p in problems_doc["problems"]
        if p["reason_code"] == "coordinate_value_unrecoverable"]
    assert len(coordinate_problems) == 2

    suppression_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "externality_suppressed"]
    assert len(suppression_problems) == 2
    assert len({p["problem_id"] for p in suppression_problems}) == 2
    assert len({p["detail"] for p in suppression_problems}) == 2


def test_run_scan_an_undeclared_vendored_module_silently_poisoned_now_degrades_visibly(
    java_repo: Path,
) -> None:
    """FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-rule
    VISIBILITY): reviewer-3's own measured silent-poison repro - a
    vendored module with real code sits under an EXCLUDED "vendor" dir
    that NO pom declares as a <module> (so the reactor rule never fires)
    and that has no src-segment ancestry of its own (so F4's own
    narrower degradation never fires either) - only discovery's generic
    peek finds the code. Before this round: every third-party import
    (org.slf4j here) silently resolved unresolved on a complete/
    problem_count-0 run, with NO record anywhere naming why. Now: the
    run degrades, a named `externality_suppressed` problem records the
    triggering root, scan.json's own flag is set, and the import
    correctly stays unresolved throughout (never a false confident
    external claim over the swallowed vendor code either)."""
    import json

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<dependencies><dependency>"
        "<groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    (java_repo / "vendor" / "src" / "main" / "java" / "com" / "acme" / "vendor").mkdir(
        parents=True)
    (java_repo / "vendor" / "src" / "main" / "java" / "com" / "acme" / "vendor" / "Widget.java"
     ).write_text("package com.acme.vendor;\nclass Widget {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app" / "Consumer.java").write_text(
        "package com.acme.app;\n"
        "import com.acme.vendor.Widget;\n"
        "import org.slf4j.Logger;\n"
        "class Consumer {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    poison_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "externality_suppressed"]
    assert any(p["path"] == "vendor" for p in poison_problems)
    assert not any(p["reason_code"] == "module_directory_excluded" for p in problems_doc["problems"])

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True
    assert any(
        r["path"] == "vendor" and r["trigger"] == "peek_positive"
        for r in scan_doc["externality_suppressed_roots"]
    )

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    vendor_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "com.acme.vendor.Widget")
    assert vendor_edge["resolution_state"] == "unresolved"
    slf4j_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "org.slf4j.Logger")
    assert slf4j_edge["resolution_state"] == "unresolved"
    assert slf4j_edge.get("target_external") is None

    # FIX ROUND 20c (readiness carry, inherited from round 20 - THE
    # MAJOR): the Consumer unit's only unresolved edges are both
    # externality misses (poisoned by "vendor") - dependencies_resolved
    # must report unknown/externality_suppressed, never the blocker-
    # severity unsatisfied/unresolved_dependency claim over a producer
    # that actually abstained.
    assert slf4j_edge["externality_suppressed"] is True
    assert vendor_edge["externality_suppressed"] is True
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    readiness_doc = json.loads((outcome.run_dir / "readiness.json").read_text(encoding="utf-8"))
    consumer_unit = next(u for u in modules_doc["units"] if u["display_name"] == "Consumer")
    dependencies_resolved = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == consumer_unit["unit_id"] and s["check"] == "dependencies_resolved")
    assert dependencies_resolved["stored_status"] == "unknown"
    assert dependencies_resolved["reason_code"] == "externality_suppressed"


def test_run_scan_a_truncated_peek_build_dir_silently_poisoned_now_degrades_visibly(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 20b (THE MAJOR - poison-rule VISIBILITY): the reviewer's
    second silent-poison repro - a repo-root `build/` dir (no src
    ancestry, no pom-declared module) whose peek exhausts its own entry
    cap before ruling code out. Same visible outcome as the peek-positive
    case above, via the OTHER trigger: `peek_truncated`."""
    import json

    from agenttalk.comprehension import discovery

    (java_repo / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<dependencies><dependency>"
        "<groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app" / "Consumer.java").write_text(
        "package com.acme.app;\nimport org.slf4j.Logger;\nclass Consumer {}\n",
        encoding="utf-8",
    )
    build_dir = java_repo / "build"
    build_dir.mkdir()
    real_scandir = discovery.os.scandir

    class _FakeEntry:
        def __init__(self, name: str) -> None:
            self.name = name
            self.path = str(build_dir / name)

        def is_symlink(self) -> bool:
            return False

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return False

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            return True

    bulk = [_FakeEntry(f"Compiled{i}.class")
            for i in range(discovery._MAX_EXCLUDED_DIRECTORY_PEEK_ENTRIES + 10)]

    def _fake_scandir(path):
        from pathlib import Path
        if isinstance(path, (str, Path)) and Path(path) == build_dir:
            return bulk
        return real_scandir(path)

    monkeypatch.setattr(discovery.os, "scandir", _fake_scandir)

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    poison_problems = [
        p for p in problems_doc["problems"] if p["reason_code"] == "externality_suppressed"]
    assert any(p["path"] == "build" for p in poison_problems)

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True
    assert any(
        r["path"] == "build" and r["trigger"] == "peek_truncated"
        for r in scan_doc["externality_suppressed_roots"]
    )

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    slf4j_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "org.slf4j.Logger")
    assert slf4j_edge["resolution_state"] == "unresolved"
    assert slf4j_edge.get("target_external") is None

    # MICRO-ROUND 49 (forty-third cold read, polish): this template's
    # own two branches used to be 216/229 characters - ALWAYS past
    # bounded_detail's 200-character bound, truncating away the entire
    # "because of this root" consequence clause regardless of any
    # variable data. Both branches now fit within bound - never ends
    # with the truncation marker, and the consequence clause (moved to
    # lead the template) survives intact.
    from agenttalk.comprehension.errors import TRUNCATION_MARKER
    poison_detail = next(p for p in poison_problems if p["path"] == "build")["detail"]
    assert not poison_detail.endswith(TRUNCATION_MARKER)
    assert "resolves unresolved" in poison_detail
    assert "entry cap" in poison_detail


def test_run_scan_a_normal_repo_with_target_full_of_class_files_keeps_confident_externals(
    java_repo: Path,
) -> None:
    """Companion negative case - a normal, healthy Maven repo's own
    target/ directory, full of genuinely compiled .class output and no
    pom-declared module pointing into it, must NOT poison externality:
    an ordinary import of a genuine third-party type still resolves a
    confident external claim, exactly as before this round."""
    import json

    target_dir = java_repo / "target" / "classes" / "p"
    target_dir.mkdir(parents=True)
    (target_dir / "App.class").write_bytes(b"\xca\xfe\xba\xbe")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport org.apache.commons.lang3.StringUtils;\nclass Consumer {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import"
        and r.get("target_external") == "org.apache.commons.lang3.StringUtils")
    assert import_edge["resolution_state"] == "resolved"

    # FIX ROUND 20b (THE MAJOR - poison-rule VISIBILITY): the control
    # case must carry NO poison artifacts at all - a genuine compiled-
    # output target/ never trips the peek, the flag, or the problem.
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is False
    assert scan_doc["externality_suppressed_roots"] == []
    problems_doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert not any(p["reason_code"] == "externality_suppressed" for p in problems_doc["problems"])


def test_run_scan_a_compiled_repo_with_generated_sources_keeps_confident_externals(
    java_repo: Path,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, CR17-5 MAJOR, completeness -
    calibration, CRITICAL for the exit-gate measurement): mirrors the
    reader's own .cr17-built shape - an ordinary COMPILED Maven repo
    with real annotation-processor-generated .java under target/
    generated-sources (MapStruct/Lombok/JPA-metamodel/protobuf are all
    ubiquitous here) used to poison the ENTIRE run's externality surface
    on this single most common repo state. The run must stay complete,
    with precise externals, and no poison artifacts at all - the SAME
    control the plain-.class-only test above already gets, now also
    holding when target/ additionally contains real generated .java."""
    import json

    generated_dir = java_repo / "target" / "generated-sources" / "annotations" / "p"
    generated_dir.mkdir(parents=True)
    (generated_dir / "ConsumerMapperImpl.java").write_text(
        "package p;\nclass ConsumerMapperImpl {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport org.apache.commons.lang3.StringUtils;\nclass Consumer {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import"
        and r.get("target_external") == "org.apache.commons.lang3.StringUtils")
    assert import_edge["resolution_state"] == "resolved"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is False
    assert scan_doc["externality_suppressed_roots"] == []


def test_run_scan_a_built_checkouts_target_classes_keeps_confident_externals(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, C1, wrong-data, end to
    end): unlike target/generated-sources/ above (build-tool-GENERATED
    source), target/classes/ holds resources COPIED byte-identical from
    src/main/resources by the build's own resource-processing step - a
    real .sql/.jsp there has no code-generation angle at all, but this
    position was NOT recognized before this round, so the single most
    ordinary repo state (anyone who ran a build before committing)
    poisoned this run's entire externality surface for a copy of
    content this run ALREADY read at its own real source-root path."""
    import json

    classes_dir = java_repo / "target" / "classes"
    classes_dir.mkdir(parents=True)
    (classes_dir / "schema.sql").write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport org.apache.commons.lang3.StringUtils;\nclass Consumer {}\n",
        encoding="utf-8",
    )

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import"
        and r.get("target_external") == "org.apache.commons.lang3.StringUtils")
    assert import_edge["resolution_state"] == "resolved"

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is False
    assert scan_doc["externality_suppressed_roots"] == []


def test_run_scan_a_first_party_class_only_under_target_classes_still_poisons(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49b (BLOCKER, reviewer-3's own two-SHA repro, end to
    end): the exact fixture the reviewer used to block the gate - src
    imports gen.Mapper; gen/Mapper.java lives SOLELY under target/
    classes/ (no src/ copy at all). The first version of the C1 fix
    published this as a confidently-RESOLVED EXTERNAL dependency on
    complete/0 - the same repo state at 65ab95c (before C1) was honest
    (unresolved + externality_suppressed + degraded), so this was a
    genuine regression, not merely an unfixed gap. Reproduced pre-fix
    exactly as reported."""
    import json

    classes_dir = java_repo / "target" / "classes" / "gen"
    classes_dir.mkdir(parents=True)
    (classes_dir / "Mapper.java").write_text(
        "package gen;\nclass Mapper {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport gen.Mapper;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"

    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "gen.Mapper")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None

    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True
    assert any(r["path"] == "target" for r in scan_doc["externality_suppressed_roots"])


def test_run_scan_a_java_copy_of_an_already_inventoried_class_under_target_classes_still_poisons(
    java_repo: Path,
) -> None:
    """MICRO-ROUND 49b (F1's own key-(a)-vs-key-(b) control): a .java
    file under target/classes/ that happens to be a byte-for-byte COPY
    of an already-inventoried src/ file. Key (a) (this producer's own
    chosen fix - exempt by EXTENSION, never .java, regardless of
    content) still poisons this shape, deliberately: distinguishing
    "a copy of something real" from "a first-party class with no other
    home" would need key (b)'s own digest-deduplication machinery this
    round did not build - key (a) is conservatively correct (never
    masks a genuinely misplaced real file) at the cost of this one
    provably-harmless shape also poisoning. Named, not silently
    assumed: if a future round measures this as a real, common false
    positive, key (b) is the documented alternative."""
    import json

    src_content = "package p;\nclass Widget {}\n"
    (java_repo / "src" / "main" / "java" / "p").mkdir(parents=True, exist_ok=True)
    (java_repo / "src" / "main" / "java" / "p" / "Widget.java").write_text(
        src_content, encoding="utf-8")
    classes_dir = java_repo / "target" / "classes" / "p"
    classes_dir.mkdir(parents=True)
    (classes_dir / "Widget.java").write_text(src_content, encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "Consumer.java").write_text(
        "package p;\nimport some.external.Thing;\nclass Consumer {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))
    assert scan_doc["externality_suppressed"] is True


def test_run_scan_an_import_into_an_excluded_generated_dir_is_unresolved_not_deleted(
    java_repo: Path,
) -> None:
    """FIX ROUND 16 (twelfth cold read, B2 BLOCKER, wrong-data): a bare
    generated/vendor directory name (``out/``) at repo ROOT (never inside
    a recognized source root, so the source-root exemption never fires)
    is genuinely excluded outright - never walked. An import naming a
    type whose hypothetical file lives under it must resolve unresolved,
    not a confident external claim, and scan.json's own excluded_roots
    list must record the exclusion an operator can actually read."""
    import json

    (java_repo / "out").mkdir(parents=True)
    (java_repo / "out" / "PaymentGateway.java").write_text(
        "package out;\nclass PaymentGateway {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "p" / "OrderService.java").write_text(
        "package p;\nimport out.PaymentGateway;\nclass OrderService {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "out.PaymentGateway")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None

    assert any(
        e["path"] == "out" and e["category"] == "generated_or_vendor"
        for e in scan_doc["excluded_roots"]
    )


def test_run_scan_a_hexagonal_out_package_inside_a_source_root_is_not_excluded(
    java_repo: Path,
) -> None:
    """FIX ROUND 16 (twelfth cold read, B2 BLOCKER part 1, wrong-data):
    mirrors reviewer-3's own ``.cr12-hex`` fixture - ``out`` is a routine
    Java package segment inside ``src/main/java/.../port/out/...`` (a
    standard hexagonal-architecture layout), never a build-output
    directory at that depth. It must stay in-scan, never silently
    deleted from the inventory."""
    import json

    port_out = java_repo / "src" / "main" / "java" / "com" / "acme" / "order" / "port" / "out"
    port_out.mkdir(parents=True)
    (port_out / "PaymentGateway.java").write_text(
        "package com.acme.order.port.out;\nclass PaymentGateway {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    scan_doc = json.loads((outcome.run_dir / "scan.json").read_text(encoding="utf-8"))

    assert any(
        u.get("qualified_name") == "com.acme.order.port.out.PaymentGateway"
        for u in modules_doc["units"]
    )
    assert not any(
        e["path"].endswith("/port/out") or e["path"] == "port/out"
        for e in scan_doc["excluded_roots"]
    )


def test_run_scan_a_wildcard_import_of_an_in_scan_package_is_unresolved_not_external(
    java_repo: Path,
) -> None:
    """FIX ROUND 16 (twelfth cold read, B3 BLOCKER, wrong-data): mirrors
    reviewer-3's own ``.cr12-wildcard`` fixture - Report.java wildcard-
    imports ``com.acme.util.*``, and ``com.acme.util.DateHelper`` is
    genuinely in-scan. Must not be miscounted as a confident external
    (third-party) dependency."""
    import json

    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "app" / "Report.java").write_text(
        "package com.acme.app;\nimport com.acme.util.*;\nclass Report {}\n", encoding="utf-8")
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "util").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "util" / "DateHelper.java").write_text(
        "package com.acme.util;\nclass DateHelper {}\n", encoding="utf-8")

    outcome = scan_pipeline.run_scan(java_repo)
    dependencies_doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))

    import_edge = next(
        r for r in dependencies_doc["edges"]
        if r["relation"] == "import" and r.get("target_unresolved") == "com.acme.util.*")
    assert import_edge["resolution_state"] == "unresolved"
    assert import_edge.get("target_external") is None


# ------------------- round 41 (F4 MAJOR, completeness): _problem_record's own chokepoint bound

def test_problem_record_bounds_an_unbounded_detail_at_the_chokepoint():
    """FIX ROUND 41 (thirty-fifth cold read, Part A F4 MAJOR, .cr35-
    longmod, completeness): `_problem_record` - the ONE function every
    problem this run publishes passes through - never called
    `bounded_detail` itself; round 40's own sweep only ever touched
    java.py's own emitters, so 12 of THIS function's own 15 call sites
    published an unbounded detail (one reader-measured site reached 707
    characters, 3.3x the declared 214-char ceiling). Now bounded
    centrally, unconditionally, here."""
    from agenttalk.comprehension.errors import MAX_PROBLEM_DETAIL_LENGTH

    oversized_detail = "this pom declares a module whose own path " + "x" * 700
    assert len(oversized_detail) > 214
    record = scan_pipeline._problem_record("externality_suppressed", "pom.xml", oversized_detail)
    assert len(record["detail"]) <= MAX_PROBLEM_DETAIL_LENGTH + len("...(truncated)")
    assert record["detail"].endswith("...(truncated)")


def test_problem_record_never_produces_a_broken_marker_from_an_embedded_bounded_value():
    """FIX ROUND 41 (Part A F4, .cr35-longmod, completeness - the
    embedded-marker case): several `_problem_record` callers build a
    detail by interpolating an ALREADY-bounded inner value (e.g. a
    WorkerProblem's own detail, already routed through bounded_detail
    once) into a larger outer template. If the OUTER string still needs
    truncating, a naive re-slice could land INSIDE the inner value's
    own already-terminal marker, publishing a broken half-marker
    followed by a fresh one. `bounded_detail`'s own idempotency (a
    string already in final form is returned unchanged) means the
    OUTER template here is what actually gets sliced - this test proves
    the final published detail's own marker is genuinely TERMINAL, not
    a broken fragment buried mid-string."""
    from agenttalk.comprehension.errors import MAX_PROBLEM_DETAIL_LENGTH, bounded_detail

    already_bounded_inner = bounded_detail("a nested detail " + "y" * 300)
    assert already_bounded_inner.endswith("...(truncated)")
    outer_detail = (
        f"this pom's own project-level coordinate is unrecoverable ({already_bounded_inner}) - "
        "every external-registry-miss import in this run resolves unresolved rather than a "
        "confident external claim because of it"
    )
    record = scan_pipeline._problem_record("externality_suppressed", "pom.xml", outer_detail)
    assert len(record["detail"]) <= MAX_PROBLEM_DETAIL_LENGTH + len("...(truncated)")
    # The marker sits at the very END of the published detail, never
    # mid-string - a single, terminal occurrence, not two markers or a
    # marker cut in half.
    assert record["detail"].endswith("...(truncated)")
    assert record["detail"].count("...(truncated)") == 1


def test_problem_record_is_idempotent_on_an_already_bounded_detail():
    """FIX ROUND 41 (Part A F4, completeness): calling _problem_record
    with a detail that is ALREADY in bounded_detail's own final form
    (within bounds, already marker-terminated) must not re-slice it -
    the one case where re-processing could only ever damage, never
    improve, an already-correct result."""
    from agenttalk.comprehension.errors import bounded_detail

    already_bounded = bounded_detail("a short detail " + "z" * 300)
    record = scan_pipeline._problem_record("externality_suppressed", "pom.xml", already_bounded)
    assert record["detail"] == already_bounded

