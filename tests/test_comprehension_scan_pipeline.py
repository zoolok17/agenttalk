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
        f"package p;\npublic class WideController {{\n{routes}\n}}\n", encoding="utf-8")
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
    all - the class landing on the confident negative."""
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

    route_targets = {
        r["target_external"] for r in dependencies_doc["edges"] if r["relation"] == "route"
    }
    assert "/api/*" in route_targets
    assert "/orders/list" in route_targets
    entry_point_names = {e["name"] for e in features_doc["entry_points"]}
    assert "/api/*" in entry_point_names
    assert "/orders/list" in entry_point_names


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
    # suppresses a route that genuinely composed.
    route_targets = {
        r["target_external"] for r in dependencies_doc["edges"] if r["relation"] == "route"
    }
    assert "/orders/{id}" in route_targets


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
    # FIX ROUND 13d (reviewer-3's LOW on round 13c): an UNATTRIBUTED
    # problem (this one is file-wide, no single owning type) must omit
    # the qualified_name KEY entirely - never publish it as null - the
    # same absent-not-null idiom every other optional field in this
    # artifact family already follows.
    route_problem = next(
        p for p in problems_doc["problems"] if p["reason_code"] == "route_value_unrecoverable")
    assert "qualified_name" not in route_problem


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
    assert dependencies_resolved["reason_code"] == "no_declared_dependencies"


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


def test_run_scan_with_acknowledge_and_work_id_proceeds(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    outcome = scan_pipeline.run_scan(
        tmp_path, acknowledge_unignored=True, work_id="migrate-app")
    assert outcome.status == "complete"


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


def test_get_status_before_any_scan_raises_not_scanned(tmp_path: Path) -> None:
    with pytest.raises(scan_pipeline.NotScanned):
        scan_pipeline.get_status(tmp_path)


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


def test_validate_run_flags_an_entry_point_with_an_unknown_owning_unit(
    java_repo: Path, monkeypatch,
) -> None:
    """ROUND 9b (sixth cold read, honesty tightening): validate already
    flagged an EDGE referencing an unknown from_unit_id (dangling_edges)
    but never an ENTRY POINT referencing an unknown owning_unit_id - the
    same "unattributable synthesized owner" shape round 9's own BLOCKER
    fixed at the adapter level could still slip past validate
    undetected on the entry-point side. Injects a synthetic entry point
    via features_artifact.build_features's own return value (so the
    artifact is digested consistently from the start, not mutated after
    publication) rather than corrupting an on-disk artifact."""
    from agenttalk.comprehension import features_artifact as featuresmod

    real_build_features = featuresmod.build_features

    def _inject_a_dangling_entry_point(*args, **kwargs):
        entry_points, features = real_build_features(*args, **kwargs)
        orphan = featuresmod.EntryPointRecord(
            entry_point_id="orphan-entry-point", kind="http_route", name="GET /orphan",
            owning_unit_id="does-not-exist", feature_ids=[], evidence_class="declared",
        )
        return [*entry_points, orphan], features

    monkeypatch.setattr(scan_pipeline.features_artifact, "build_features", _inject_a_dangling_entry_point)

    scan_pipeline.run_scan(java_repo)
    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is False
    assert "owning_unit_id" in result["detail"]


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

    # The conflict problem itself is still recorded, unaffected.
    assert any(p["reason_code"] == "duplicate_qualified_name" for p in problems_doc["problems"])

    # A non-conflicted twin class in the SAME run stays fully confident.
    plain_unit_id = next(
        u["unit_id"] for u in modules_doc["units"]
        if u["kind"] == "component" and u["display_name"] == "Plain")
    plain_dependencies_signal = next(
        s for s in readiness_doc["signals"]
        if s["unit_id"] == plain_unit_id and s["check"] == "dependencies_resolved")
    assert plain_dependencies_signal["reason_code"] != "duplicate_qualified_name"


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
