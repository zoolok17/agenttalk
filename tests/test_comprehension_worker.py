"""#55 slice-1 PR-B item 1: the sanitized bundled scanner worker
(DESIGN-55-comprehension-plane.md, "System boundary" / "Privacy and offline
enforcement"). No adapter is wired in yet - these tests exercise the
process boundary, the environment allowlist, and the default
every-file-is-addressable guarantee only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk.comprehension import worker


# ----------------------------------------------------------- sanitized_worker_env

def test_sanitized_worker_env_keeps_only_the_fixed_allowlist():
    source = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "HTTP_PROXY": "http://evil.example",
        "HTTPS_PROXY": "http://evil.example",
        "NO_PROXY": "example.com",
        "AGENTTALK_SELF": "claude",
        "AGENTTALK_ROOT": "D:\\somewhere",
        "GIT_AUTHOR_NAME": "someone",
        "HOSTNAME": "some-host",
        "COMPUTERNAME": "SOME-HOST",
    }
    env = worker.sanitized_worker_env(source)
    assert env == {"PATH": "/usr/bin"}


def test_sanitized_worker_env_is_case_insensitive_on_the_key_but_preserves_value():
    env = worker.sanitized_worker_env({"path": "/usr/bin", "Path": "/bin"})
    # Both keys casefold-match the allowlist entry "PATH"; both survive as
    # distinct dict keys (we do not silently collapse them) - only the
    # PRESENCE test matters here since callers pass a real os.environ-shaped
    # dict where keys are unique per platform casing convention already.
    assert set(env) == {"path", "Path"}


def test_sanitized_worker_env_defaults_to_the_real_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = worker.sanitized_worker_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("PATH") == "/usr/bin"


# ----------------------------------------------------------- process_paths

def test_process_paths_claims_every_file_with_its_size(tmp_path: Path) -> None:
    """N3 (fourth cold read, fix round 6): WorkerFileClaim used to also
    carry a content_digest - a second hash of every file's bytes, on top
    of the one discovery.py already computes for the whole-scope
    fingerprint - with zero consumers outside this module. Dropped along
    with the hashing that produced it; byte_count (still genuinely used
    to prove "this file is still addressable") remains."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")
    result = worker.process_paths(tmp_path, ["a.txt", "b.txt"])
    assert result.schema_version == worker.WORKER_SCHEMA_VERSION
    by_path = {c.relative_path: c for c in result.file_claims}
    assert by_path["a.txt"].byte_count == 5
    assert by_path["b.txt"].byte_count == 6
    assert result.problems == []


def test_process_paths_a_bom_prefixed_java_file_still_extracts_its_real_qualified_name(
    tmp_path: Path,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, B1 BLOCKER, wrong-data): mirrors
    the reader's own .cr16-h shape - a UTF-8 BOM on a .java file
    (ordinary Windows-tooling output, a legal javac input, pervasive in
    legacy estates) is not whitespace, so plain "utf-8" decoding left it
    as the file's own leading character, defeating _PACKAGE_RE's
    ``^\\s*package`` anchor - the unit published a WRONG qualified name
    (the bare simple name, package lost entirely). "utf-8-sig" strips
    the BOM; the package must now be recovered correctly, CRLF line
    endings notwithstanding."""
    (tmp_path / "Foo.java").write_bytes(
        ("﻿" + "package p;\r\nclass Foo {}\r\n").encode("utf-8"))
    result = worker.process_paths(tmp_path, ["Foo.java"])
    assert result.problems == []
    units = result.java_results["Foo.java"]["units"]
    assert len(units) == 1
    assert units[0]["qualified_name"] == "p.Foo"


def test_process_paths_a_latin1_java_file_records_a_problem_not_a_fabricated_name(
    tmp_path: Path,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, CR17-4 MAJOR, wrong-data):
    mirrors the reader's own .cr17-enc2 pair - Latin-1/CP1252 source
    (the DEFAULT encoding of many pre-Maven-3 European estates) decodes
    with errors="replace", substituting U+FFFD for the non-UTF-8 byte -
    outside \\w, so the type-name anchor regex silently skips over it,
    fabricating a TRUNCATED qualified name (``Café`` -> ``Caf``) rather
    than failing visibly. Adapter analysis is now skipped entirely and a
    named, degrading problem recorded instead - no unit, no fabricated
    name anywhere."""
    (tmp_path / "Cafe.java").write_bytes(
        "package p;\nclass Café {}\n".encode("latin-1"))
    result = worker.process_paths(tmp_path, ["Cafe.java"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "encoding_undecodable"
    assert result.problems[0].relative_path == "Cafe.java"
    java_result = result.java_results.get("Cafe.java")
    if java_result is not None:
        assert java_result["units"] == []


def test_process_paths_a_genuine_utf8_java_file_with_unicode_identifier_stays_clean(
    tmp_path: Path,
) -> None:
    """Companion control: the SAME accented identifier, correctly UTF-8
    encoded, must decode cleanly with no U+FFFD anywhere and no problem
    recorded - this is ordinary, legal Java (the JLS permits Unicode
    letters in identifiers)."""
    (tmp_path / "Cafe.java").write_bytes(
        "package p;\nclass Café {}\n".encode("utf-8"))
    result = worker.process_paths(tmp_path, ["Cafe.java"])
    assert result.problems == []
    units = result.java_results["Cafe.java"]["units"]
    assert len(units) == 1
    assert units[0]["qualified_name"] == "p.Café"


def test_process_paths_reports_a_traversal_path_as_a_problem_not_a_crash(
    tmp_path: Path,
) -> None:
    result = worker.process_paths(tmp_path, ["../../../../escaped"])
    assert result.file_claims == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "path_excluded"
    assert result.problems[0].relative_path == "../../../../escaped"


def test_process_paths_reports_an_unreadable_path_as_a_problem(tmp_path: Path) -> None:
    (tmp_path / "a_directory").mkdir()
    result = worker.process_paths(tmp_path, ["a_directory"])
    assert result.file_claims == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "parse_failed"


def test_process_paths_caps_adapter_work_and_degrades_instead_of_aborting(
    tmp_path: Path, monkeypatch,
) -> None:
    """M11 (cold-read, PR-B fix round 3): the design lists "adapter work"
    among the resource caps, but none existed - the file still gets its
    base WorkerFileClaim (still addressable), and the scan degrades via a
    bounded resource_limit problem, instead of the only prior option (the
    whole-worker timeout aborting the entire scan with no published run
    at all)."""
    monkeypatch.setattr(worker, "_MAX_ADAPTER_INPUT_BYTES", 10)
    (tmp_path / "Big.java").write_text(
        "package p;\nclass Big {\n  void run() { Foo.bar(); }\n}\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["Big.java"])
    assert result.file_claims[0].relative_path == "Big.java"  # still addressable
    assert "Big.java" not in result.java_results  # adapter analysis skipped
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "resource_limit"
    assert result.problems[0].relative_path == "Big.java"


def test_process_paths_flags_jsp_properties_and_sql_as_unsupported_language(
    tmp_path: Path,
) -> None:
    """FIX ROUND 14 (tenth cold read, CR10-5 JUDGE, completeness): the
    design names ``unsupported_language`` as a problem code and a
    ``degraded`` trigger for "part of the selected source is unsupported"
    - a run over a JSP/properties/SQL estate used to publish complete
    with problem_count 0, contradicting that text. FIX ROUND 14b
    (reviewer-3's ratified split): recording is unconditional for all
    three, but only JSP/SQL are code-bearing - properties never degrades
    the run (reviewer-3's own reader test: a reader would not say a
    properties file is "missed application code")."""
    (tmp_path / "index.jsp").write_text("<%@ page language=\"java\" %>", encoding="utf-8")
    (tmp_path / "app.properties").write_text("key=value\n", encoding="utf-8")
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["index.jsp", "app.properties", "schema.sql"])
    assert len(result.file_claims) == 3  # still addressable
    assert {p.relative_path for p in result.problems} == {
        "index.jsp", "app.properties", "schema.sql"}
    assert all(p.reason_code == "unsupported_language" for p in result.problems)
    degrades_by_path = {p.relative_path: p.degrades_run for p in result.problems}
    assert degrades_by_path["index.jsp"] is True
    assert degrades_by_path["schema.sql"] is True
    assert degrades_by_path["app.properties"] is False


@pytest.mark.parametrize("filename", [
    "Foo.jsp", "Foo.jspx", "Foo.jspf", "Foo.tag", "schema.sql", "Foo.groovy",
    "Foo.kt", "Foo.scala", "view.xhtml", "template.ftl", "template.vm",
    # FIX ROUND 17 (thirteenth cold read, CR13-1 MAJOR, part (b) - GROW
    # TIER 2): the reader's own polyglot evidence - real application-code
    # extensions this list was too narrow to catch. FIX ROUND 17b
    # (TIER-2 PARTIAL OVERTURN): .js/.ts/.py REMOVED from this list -
    # see test_process_paths_flags_js_ts_py_as_tier3_record_only below,
    # the companion negative case.
    "Program.cs", "index.php", "app.rb", "main.go", "Package.pks",
    "Package.pkb", "transform.xsl",
])
def test_process_paths_flags_every_tier2_code_bearing_extension_as_degrading(
    tmp_path: Path, filename: str,
) -> None:
    """FIX ROUND 16b (reviewer-3's rejection of round 16, BLOCKER 1 - the
    B4 CALIBRATION): the closed, recognized CODE-BEARING list (TIER 2) -
    every member individually confirmed still degrading after the
    three-tier recalibration, not just the four the round-16 battery
    happened to cover."""
    (tmp_path / filename).write_text("placeholder content\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, [filename])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].degrades_run is True


@pytest.mark.parametrize("filename", ["app.js", "app.ts", "script.py"])
def test_process_paths_flags_js_ts_py_as_tier3_record_only(
    tmp_path: Path, filename: str,
) -> None:
    """FIX ROUND 17b (reviewer-3's rejection of round 17, TIER-2 PARTIAL
    OVERTURN, measured): round 17 added .js/.ts/.py to tier 2 -
    OVERTURNED. Unlike .cs/.php/.rb/.go/.pks/.pkb/.xsl, these three are
    ROUTINELY INCIDENTAL in an ordinary Java repository (a
    `scripts/release.py` helper, a webapp's own static `app.js`/`app.ts`
    asset) - re-degrading the round-16b composite Spring Boot repo
    (this producer's own acceptance fixture for the three-tier rule
    itself) is exactly the regression this reverts. Still recorded
    (tier 3's own guarantee, never silently vanished) - just never
    degrading, the same trade already accepted everywhere else in tier
    3."""
    (tmp_path / filename).write_text("placeholder content\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, [filename])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].degrades_run is False


@pytest.mark.parametrize("relative_path", ["Legacy.java", "widget.jsp", "src/Main.kt"])
def test_is_a_code_bearing_extension_worth_degrading_true_for_adapter_and_tier2(
    relative_path: str,
) -> None:
    """FIX ROUND 18 (fourteenth cold read, F6 JUDGE, taken): an adapter-
    handled extension (.java) or a tier-2 code-bearing extension (.jsp,
    .kt, ...) is worth degrading a run over if discovery's own binary
    sniff silently excludes it."""
    assert worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        relative_path)


@pytest.mark.parametrize("relative_path", ["photo.png", "archive.bin", "notes.md"])
def test_is_a_code_bearing_extension_worth_degrading_false_for_genuinely_binary(
    relative_path: str,
) -> None:
    """Companion negative case - a genuinely binary or benign non-code
    extension must stay exactly as silent as it is today."""
    assert not worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        relative_path)


def test_process_paths_a_polyglot_legacy_web_app_now_degrades_cr13_b(tmp_path: Path) -> None:
    """FIX ROUND 17 (thirteenth cold read, CR13-1 MAJOR, wrong-data):
    mirrors the reader's own .cr13-b shape - a legacy web app whose real
    behavior lives in .js files, plus Oracle PL/SQL package spec/body
    (.pks/.pkb) and an XSL transform - all genuine, unambiguous
    application-code shapes this producer has no adapter for. Round 16's
    own three-tier rule, pre-growth, scanned this repo COMPLETE (0
    degrading) over real migration estate; must now DEGRADE.

    FIX ROUND 17b (TIER-2 PARTIAL OVERTURN): .js REMOVED from tier 2 -
    re-degrading THIS SAME composite (this producer's own three-tier-rule
    acceptance fixture) over an ordinary webapp static asset was exactly
    the regression that got it overturned. .js now stays record-only
    (tier 3); .pks/.pkb/.xsl (never merely incidental) still degrade."""
    (tmp_path / "legacy.js").write_text("function submit() {}\n", encoding="utf-8")
    (tmp_path / "OrderPkg.pks").write_text("CREATE PACKAGE order_pkg AS END;\n", encoding="utf-8")
    (tmp_path / "OrderPkg.pkb").write_text("CREATE PACKAGE BODY order_pkg AS END;\n", encoding="utf-8")
    (tmp_path / "report.xsl").write_text(
        "<xsl:stylesheet version=\"1.0\"></xsl:stylesheet>", encoding="utf-8")
    paths = ["legacy.js", "OrderPkg.pks", "OrderPkg.pkb", "report.xsl"]
    result = worker.process_paths(tmp_path, paths)
    assert len(result.problems) == 4
    assert all(p.reason_code == "unsupported_language" for p in result.problems)
    degrades_by_path = {p.relative_path: p.degrades_run for p in result.problems}
    assert degrades_by_path["legacy.js"] is False
    assert degrades_by_path["OrderPkg.pks"] is True
    assert degrades_by_path["OrderPkg.pkb"] is True
    assert degrades_by_path["report.xsl"] is True


@pytest.mark.parametrize("filename", [
    "mvnw", "mvnw.cmd", "Dockerfile", "ci.yml",
    "application.yml", "package.json",
])
def test_process_paths_flags_tier3_build_tooling_files_as_record_only(
    tmp_path: Path, filename: str,
) -> None:
    """FIX ROUND 16b (BLOCKER 1, the B4 CALIBRATION): reviewer-3's own
    38-repo battery measured an ordinary healthy Spring Boot repo
    scanning DEGRADED with 10 recorded problems - mvnw/mvnw.cmd/
    Dockerfile/LICENSE/CI YAMLs/application.yml all wrongly flipped a
    clean run to degraded under round 16's own "presumed code-bearing by
    default" rule. TIER 3 (everything non-benign, non-adapter-handled,
    and NOT on the tier-2 closed list) is now recorded - the round-16
    inversion's own win, never silently un-recorded - but never
    degrading: a build/tooling/infra/config file is not "missed
    application code" the way a JSP or a Kotlin source is.

    FIX ROUND 25 (twenty-first cold read, F9, take-it): LICENSE/
    CHANGELOG moved to the fully-benign basename set (see
    ``test_process_paths_flags_extensionless_project_metadata_files_
    as_benign`` below) - removed from this list, since they no longer
    record anything at all."""
    (tmp_path / filename).write_text("placeholder content\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, [filename])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].degrades_run is False


def test_process_paths_incoherence_application_properties_and_yml_now_agree(
    tmp_path: Path,
) -> None:
    """FIX ROUND 16b (BLOCKER 1): reviewer-3's own sharpest measured
    case - application.properties and application.yml are the IDENTICAL
    configuration, merely a different serialization, but round 16's own
    rule left them incoherent (properties record-only via its
    pre-existing carve-out, yml degrading via the default). Both must
    now record, neither degrades."""
    (tmp_path / "application.properties").write_text("key=value\n", encoding="utf-8")
    (tmp_path / "application.yml").write_text("key: value\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["application.properties", "application.yml"])
    assert len(result.problems) == 2
    assert all(p.reason_code == "unsupported_language" for p in result.problems)
    assert all(p.degrades_run is False for p in result.problems)


def test_process_paths_lock_family_stays_silent_while_package_json_records(
    tmp_path: Path,
) -> None:
    """FIX ROUND 16b (BLOCKER 1): the reviewer's own named check - the
    lockfile family (a genuine benign basename/extension) stays
    completely silent (no WorkerProblem at all), while package.json (an
    ordinary .json file, not itself a recognized lockfile basename)
    still gets recorded, tier-3, never degrading."""
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    result = worker.process_paths(
        tmp_path, ["package-lock.json", "yarn.lock", "package.json"])
    assert {p.relative_path for p in result.problems} == {"package.json"}
    assert result.problems[0].degrades_run is False


@pytest.mark.parametrize("filename", [
    "LICENSE", "NOTICE", "COPYING", "AUTHORS", "CHANGELOG",
])
def test_process_paths_flags_extensionless_project_metadata_files_as_benign(
    tmp_path: Path, filename: str,
) -> None:
    """FIX ROUND 25 (twenty-first cold read, F9, take-it): README.md is
    already benign (a recognized extension), but an EXTENSIONLESS
    LICENSE - the far more common real-world spelling, never
    "LICENSE.txt" - recorded unsupported_language, an asymmetry between
    two equally inert project-root files. A closed, well-known
    extensionless set now joins the benign basenames, silent like every
    other benign file (case-insensitive, matching this producer's own
    existing basename comparison)."""
    (tmp_path / filename).write_text("placeholder content\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, [filename])
    assert result.problems == []


def test_process_paths_a_composite_healthy_spring_boot_repo_does_not_degrade(
    tmp_path: Path,
) -> None:
    """FIX ROUND 16b (BLOCKER 1): reviewer-3's own composite shape - an
    ordinary, entirely healthy Spring Boot repo's non-code surface
    (Maven wrapper, Dockerfile, LICENSE, CI config, application.yml/
    .properties) must record every file (round 16's own win, unchanged)
    but never degrade the run over any of them.

    FIX ROUND 25 (twenty-first cold read, F9, take-it): LICENSE is now
    fully benign (silent, no problem at all) - see the extensionless-
    metadata test below - so it no longer contributes to this count."""
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "mvnw.cmd").write_text("@ECHO OFF\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM eclipse-temurin:21\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("Apache License 2.0\n", encoding="utf-8")
    (tmp_path / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (tmp_path / "application.yml").write_text("server:\n  port: 8080\n", encoding="utf-8")
    (tmp_path / "application.properties").write_text("server.port=8080\n", encoding="utf-8")
    (tmp_path / "App.java").write_text(
        "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n",
        encoding="utf-8")
    paths = [
        "mvnw", "mvnw.cmd", "Dockerfile", "LICENSE", "ci.yml",
        "application.yml", "application.properties", "App.java",
    ]
    result = worker.process_paths(tmp_path, paths)
    assert len(result.problems) == 6  # every non-adapter, non-code-bearing, non-benign file
    assert all(p.reason_code == "unsupported_language" for p in result.problems)
    assert all(p.degrades_run is False for p in result.problems)
    assert "LICENSE" not in {p.relative_path for p in result.problems}
    assert "App.java" in result.java_results


def test_process_paths_flags_a_spring_bean_xml_file_as_degrading(tmp_path: Path) -> None:
    """FIX ROUND 14b (reviewer-3's ratified split): a Spring bean XML
    file (root element ``<beans>``) IS code-bearing configuration -
    the reviewer's own reader test says a migration reader would call
    this "missed" - so it keeps degrading the run, unlike ordinary
    tooling XML (logback/checkstyle) or an unreadable root."""
    (tmp_path / "applicationContext.xml").write_text(
        "<beans><bean id=\"x\" class=\"y\"/></beans>", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["applicationContext.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].relative_path == "applicationContext.xml"
    assert result.problems[0].degrades_run is True


def test_process_paths_flags_a_struts_config_xml_file_as_degrading(tmp_path: Path) -> None:
    """FIX ROUND 23 (nineteenth cold read, F12, JUDGE - taken): a Struts
    1.x ``struts-config.xml`` file (root element ``<struts-config>``) is
    the SAME class of gap CR13-1 already measured for Spring bean XML -
    real action-mapping/routing estate, not tooling XML - so it now
    degrades the run the same way, rather than silently falling into
    tier 3's record-only default alongside genuinely inert config XML."""
    (tmp_path / "struts-config.xml").write_text(
        "<struts-config><action-mappings/></struts-config>", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["struts-config.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].relative_path == "struts-config.xml"
    assert result.problems[0].degrades_run is True


def test_process_paths_flags_logback_and_checkstyle_xml_as_record_only(tmp_path: Path) -> None:
    """FIX ROUND 14b: reviewer-3's own measurement - logback.xml
    (root ``<configuration>``) and checkstyle.xml (root ``<module>``)
    both degraded an otherwise entirely healthy repo, over files a
    migration reader would never call "missed application code". Both
    are tooling/config XML, not Spring bean XML - recorded, never
    degrading."""
    (tmp_path / "logback.xml").write_text(
        "<configuration><root level=\"INFO\"/></configuration>", encoding="utf-8")
    (tmp_path / "checkstyle.xml").write_text(
        "<module name=\"Checker\"></module>", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["logback.xml", "checkstyle.xml"])
    assert len(result.problems) == 2
    assert all(p.reason_code == "unsupported_language" for p in result.problems)
    assert all(p.degrades_run is False for p in result.problems)


def test_process_paths_flags_an_xml_file_with_no_determinable_root_as_record_only(
    tmp_path: Path,
) -> None:
    """FIX ROUND 14b: when the root-element sniff itself cannot
    determine a root (no element-shaped tag anywhere in the file), the
    result fails toward the SAFE side - record-only, never a guessed
    degradation - with a reason detail naming the sniff failure, not a
    silently-defaulted claim either way."""
    (tmp_path / "mystery.xml").write_text("not actually xml at all", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["mystery.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].degrades_run is False
    assert "could not be determined" in result.problems[0].detail


def test_process_paths_flags_previously_unenumerated_code_extensions_too(
    tmp_path: Path,
) -> None:
    """FIX ROUND 16 (twelfth cold read, B4 BLOCKER, wrong-data): mirrors
    reviewer-3's own ``.cr12-jsf`` fixture - ``.xhtml``, ``.groovy``,
    ``.tag``, ``.jspf`` are all real, code-bearing JSF/JSP-adjacent
    files that the OLD closed CODE-extension allowlist did not name -
    they used to vanish with NO java_results entry and NO WorkerProblem
    at all (not even addressed as a coverage gap). The INVERTED
    BENIGN-extension allowlist now flags every one of them
    unsupported_language, degrading (presumed code-bearing, per the
    inversion's own "guilty until proven benign" direction), still
    addressable (file_claims unaffected)."""
    (tmp_path / "view.xhtml").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "Helper.groovy").write_text("class Helper {}\n", encoding="utf-8")
    (tmp_path / "widget.tag").write_text("<jsp:root/>", encoding="utf-8")
    (tmp_path / "fragment.jspf").write_text("<%@ include file=\"x\" %>", encoding="utf-8")
    paths = ["view.xhtml", "Helper.groovy", "widget.tag", "fragment.jspf"]
    result = worker.process_paths(tmp_path, paths)
    assert len(result.file_claims) == 4  # still addressable
    assert {p.relative_path for p in result.problems} == set(paths)
    assert all(p.reason_code == "unsupported_language" for p in result.problems)
    assert all(p.degrades_run is True for p in result.problems)


def test_process_paths_does_not_flag_ordinary_non_source_files(tmp_path: Path) -> None:
    """This stays deliberately narrow - an ordinary repository's
    documentation/text/lockfile/generic-config files outside the named
    set must never flip a scan to degraded."""
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.class\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["README.md", ".gitignore"])
    assert result.problems == []


def test_process_paths_dispatch_is_not_extension_case_sensitive(tmp_path: Path) -> None:
    """Note 10 (second cold read, fix round 4): Windows and default macOS
    filesystems are case-insensitive/case-preserving - `Foo.JAVA` and
    `POM.XML` are perfectly reachable real files there, and a case-
    sensitive dispatch check would silently skip adapter dispatch for
    them."""
    (tmp_path / "Foo.JAVA").write_text("package p;\nclass Foo {}\n", encoding="utf-8")
    (tmp_path / "POM.XML").write_text(
        "<project><dependencies><dependency>"
        "<groupId>g</groupId><artifactId>a</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    result = worker.process_paths(tmp_path, ["Foo.JAVA", "POM.XML"])
    assert result.problems == []
    assert "Foo.JAVA" in result.java_results
    assert result.java_results["Foo.JAVA"]["units"][0]["qualified_name"] == "p.Foo"
    assert "POM.XML" in result.java_results
    assert result.java_results["POM.XML"]["edges"][0]["target"] == "g:a"


def test_process_paths_dispatches_pom_xml_through_the_java_results_channel(
    tmp_path: Path,
) -> None:
    """B-3 (reviewer-3, PR-B delta review): pom.xml build-relation
    extraction must happen INSIDE this worker, on the same bytes already
    read here - never a second, separate read in the parent process."""
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    result = worker.process_paths(tmp_path, ["pom.xml"])
    assert result.problems == []
    assert "pom.xml" in result.java_results
    edges = result.java_results["pom.xml"]["edges"]
    assert edges and edges[0]["target"] == "org.springframework:spring-core"
    assert edges[0]["relation"] == "build"


def test_process_paths_dispatches_web_xml_through_the_java_results_channel(
    tmp_path: Path,
) -> None:
    """M9 (cold-read, PR-B fix round 3): parse_web_xml existed with its
    own passing unit tests but no dispatch anywhere in the pipeline - a
    valid servlet-mapping web.xml produced no route at all. Wired in the
    same shape as pom.xml's build edges: same already-read bytes, same
    java_results channel."""
    (tmp_path / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )
    result = worker.process_paths(tmp_path, ["web.xml"])
    assert result.problems == []
    assert "web.xml" in result.java_results
    entry_points = result.java_results["web.xml"]["entry_points"]
    assert entry_points and entry_points[0]["name"] == "/api/*"
    assert entry_points[0]["kind"] == "http_route"


def test_process_paths_flags_a_java_file_that_parses_but_extracts_no_types(
    tmp_path: Path,
) -> None:
    """BLOCKER 1b (fifth cold read, fix round 8): a .java file whose
    parse SUCCEEDS but extracts ZERO declared types used to count as
    positive adapter evidence with no problem recorded at all -
    readiness then reported source_understood satisfied for a file this
    adapter never actually understood. Genuinely unrecognized top-level
    content (not a comment, not a package/import statement, not any
    known declaration keyword) must now be a named, explicit problem."""
    (tmp_path / "Garbage.java").write_text("package p;\nfoo bar baz;\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["Garbage.java"])
    assert "Garbage.java" in result.java_results
    assert [p.reason_code for p in result.problems] == ["no_types_extracted"]
    assert result.problems[0].relative_path == "Garbage.java"


def test_process_paths_does_not_flag_package_info_java(tmp_path: Path) -> None:
    """package-info.java legitimately declares no class/interface/enum/
    record at all - even carrying its own package-level annotation
    (a common real-world shape) - and must never be flagged as an
    unrecognized header."""
    (tmp_path / "package-info.java").write_text(
        "/**\n * Javadoc.\n */\n@Deprecated\npackage p;\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["package-info.java"])
    assert result.problems == []


def test_process_paths_does_not_flag_module_info_java(tmp_path: Path) -> None:
    """MAJOR 2 (sixth cold read, fix round 9): module-info.java legitimately
    declares a `module ... { ... }` block, not a class/interface/enum/
    record - a keyword shape this adapter's extractor does not recognize
    at all, so it ALWAYS yields zero units, the same legitimately-
    typeless shape package-info.java already is. Flipping an otherwise-
    clean run to degraded over this is a factually wrong problem."""
    (tmp_path / "module-info.java").write_text(
        "module com.acme.app {\n    requires java.base;\n    exports com.acme.app;\n}\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["module-info.java"])
    assert result.problems == []


def test_process_paths_does_not_flag_a_route_annotation_on_an_annotation_type(
    tmp_path: Path,
) -> None:
    """Round 10b (reviewer-3 delta on round 10): a route annotation
    stacked on an `@interface` (annotation-type) declaration - the
    documented Spring composed-annotation idiom Spring's own verb
    annotations (@GetMapping et al.) are themselves defined with -
    cannot associate as a class-level prefix, but is a legitimate,
    common shape, not an unforeseen one. Suppressing the route is
    correct; flipping an otherwise-clean run to degraded with a problem
    naming a file that is perfectly fine is not."""
    (tmp_path / "GetMapping2.java").write_text(
        "package p;\n\n"
        "@Target(java.lang.annotation.ElementType.METHOD)\n"
        "@Retention(java.lang.annotation.RetentionPolicy.RUNTIME)\n"
        "@RequestMapping(method = RequestMethod.GET)\n"
        "public @interface GetMapping2 {\n"
        '    String value() default "";\n'
        "}\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["GetMapping2.java"])
    assert result.problems == []
    assert result.java_results["GetMapping2.java"]["edges"] == []


def test_process_paths_does_not_flag_an_empty_or_comment_only_java_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "Empty.java").write_text(
        "package p;\n// nothing else here\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["Empty.java"])
    assert result.problems == []


def test_process_paths_flags_a_pom_that_extracts_absolutely_nothing_f1b(
    tmp_path: Path,
) -> None:
    """FIX ROUND 24 (twentieth cold read, F1b, wrong-data): the SAME
    "positive evidence, not merely absence of a negative" discipline
    BLOCKER 1b (round 8) already closed for a .java file's own zero-
    types case, extended to pom.xml - a pom parse that SUCCEEDS but
    registers no own coordinate, no dependency edge, and no reactor
    module read as a complete, zero-problem run (the exact mechanism
    that let a namespace-prefixed pom - this round's own F1 - silently
    vanish while `source_understood` confidently reported satisfied). A
    real pom always carries SOME identity under Maven's own model, so
    "nothing at all" is never legitimately minimal the way an empty
    .java file can be."""
    (tmp_path / "pom.xml").write_text(
        "<project>\n  <modelVersion>4.0.0</modelVersion>\n</project>\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["pom.xml"])
    matching = [p for p in result.problems if p.reason_code == "no_pom_facts_extracted"]
    assert len(matching) == 1
    assert matching[0].relative_path == "pom.xml"


def test_process_paths_does_not_flag_a_minimal_pom_with_a_real_coordinate_f1b(
    tmp_path: Path,
) -> None:
    """Companion control: a minimal but genuinely real pom (its own
    groupId:artifactId, no dependencies/modules) must NOT be flagged -
    a leaf module with no deps is a normal, common, legitimate shape."""
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>leaf</artifactId></project>\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["pom.xml"])
    assert result.problems == []


def test_process_paths_flags_a_web_xml_that_extracts_nothing_but_has_real_content(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 24b (reviewer-3 delta on `3a7abc2`, item 1, wrong-
    data): the SAME positive-evidence gate F1b already gives pom.xml,
    extended to web.xml - a parse that succeeds but yields zero entry
    points and zero problems, over a root that has REAL content (a
    <display-name>, here) that simply matches none of this adapter's
    five modeled element families, must not read as a complete, zero-
    problem run - exactly the shape that would mask the next web.xml
    parser blindness the same way the pom.xml one did."""
    (tmp_path / "web.xml").write_text(
        "<web-app><display-name>Legacy App</display-name></web-app>\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["web.xml"])
    matching = [p for p in result.problems if p.reason_code == "no_web_xml_facts_extracted"]
    assert len(matching) == 1
    assert matching[0].relative_path == "web.xml"


def test_process_paths_does_not_flag_a_genuinely_empty_web_app(tmp_path: Path) -> None:
    """MICRO-ROUND 24b (item 1): a genuinely empty <web-app/> - nothing
    declared at all - gets the SAME "nothing to misunderstand is itself
    a positive finding" treatment Empty.java already gets, not the new
    gate. Judged deliberately: unlike a pom (which always carries SOME
    identity under Maven's own model), an empty descriptor is a real,
    common, legitimate shape (e.g. an otherwise fully annotation-driven
    webapp with no XML-declared servlets at all)."""
    (tmp_path / "web.xml").write_text("<web-app/>\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["web.xml"])
    assert result.problems == []


def test_process_paths_does_not_flag_a_normal_web_xml_with_a_real_mapping(
    tmp_path: Path,
) -> None:
    """Companion control: a normal, real web.xml with a genuine mapping
    must not be flagged - it has real, non-empty extraction (one entry
    point), the ordinary healthy case."""
    (tmp_path / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>s1</servlet-name>\n"
        "    <url-pattern>/s1</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["web.xml"])
    assert result.problems == []


def test_process_paths_web_xml_route_publishes_a_paired_route_edge_f4(
    tmp_path: Path,
) -> None:
    """FIX ROUND 27 (twenty-third cold read, F4, mechanism confirmed):
    parse_web_xml now returns a third value (edges) - the worker must
    thread it into JavaFileResult, the same channel every annotation-
    based route's own paired edge already flows through."""
    (tmp_path / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>s1</servlet-name>\n"
        "    <url-pattern>/s1</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["web.xml"])
    edges = result.java_results["web.xml"]["edges"]
    assert len(edges) == 1
    assert edges[0]["relation"] == "route"
    assert edges[0]["target"] == "/s1"


def test_process_paths_flags_a_pom_dependency_with_an_undecodable_groupid_f4(
    tmp_path: Path,
) -> None:
    """FIX ROUND 24 (twentieth cold read, F4 MINOR, wrong-data): a
    module-own <dependency>'s own groupId that is present but
    undecodable (split CDATA) must be recorded as a real problem, not
    silently dropped from the edge list with a complete, zero-problem
    run."""
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<dependencies><dependency>"
        "<groupId><![CDATA[org.a]]>b<![CDATA[c]]></groupId>"
        "<artifactId>lib</artifactId>"
        "</dependency></dependencies></project>\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["pom.xml"])
    matching = [p for p in result.problems if p.reason_code == "dependency_value_unrecoverable"]
    assert len(matching) == 1
    assert matching[0].relative_path == "pom.xml"


def test_process_paths_does_not_flag_a_cdata_wrapped_pom_dependency_groupid_f4(
    tmp_path: Path,
) -> None:
    """Companion control: a wholly-CDATA-wrapped groupId decodes cleanly
    and must not be flagged."""
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.acme</groupId><artifactId>root</artifactId>"
        "<dependencies><dependency>"
        "<groupId><![CDATA[org.springframework]]></groupId>"
        "<artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>\n",
        encoding="utf-8")
    result = worker.process_paths(tmp_path, ["pom.xml"])
    assert result.problems == []


def test_process_paths_a_latin1_pom_records_a_problem_not_a_fabricated_coordinate(
    tmp_path: Path,
) -> None:
    """FIX ROUND 26 (twenty-second cold read, F3 BLOCKER, wrong-data,
    Amperian-critical, .cr22-enc): round 21's own CR17-4 U+FFFD guard
    existed ONLY on the .java decode site - pom.xml's own decode site
    had none at all, so a Latin-1/CP1252 pom (the default encoding of
    many pre-Maven-3 European estates - Amperian's own estate among
    them) published a FABRICATED, truncated coordinate rather than
    failing visibly. Mirrors the .java control above one producer
    over."""
    (tmp_path / "pom.xml").write_bytes(
        "<project><groupId>com.example</groupId>"
        "<artifactId>café-core</artifactId></project>\n".encode("latin-1"))
    result = worker.process_paths(tmp_path, ["pom.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "encoding_undecodable"
    assert result.problems[0].relative_path == "pom.xml"
    assert "pom.xml" not in result.java_results


def test_process_paths_a_genuine_utf8_pom_with_unicode_content_stays_clean(
    tmp_path: Path,
) -> None:
    """Companion control: the same accented coordinate, correctly UTF-8
    encoded, must decode cleanly with no problem recorded."""
    (tmp_path / "pom.xml").write_bytes(
        "<project><groupId>com.example</groupId>"
        "<artifactId>café-core</artifactId></project>\n".encode("utf-8"))
    result = worker.process_paths(tmp_path, ["pom.xml"])
    assert result.problems == []
    units = result.java_results["pom.xml"]["units"]
    assert units[0]["qualified_name"] == "com.example:café-core"


def test_process_paths_a_latin1_web_xml_records_a_problem_not_a_false_route(
    tmp_path: Path,
) -> None:
    """FIX ROUND 26 (F3 BLOCKER, wrong-data): the web.xml twin - a
    Latin-1-encoded web.xml must never publish a corrupted/fabricated
    route derived from an undecodable byte sequence."""
    (tmp_path / "web.xml").write_bytes(
        "<web-app><servlet-mapping><servlet-name>café</servlet-name>"
        "<url-pattern>/café/*</url-pattern></servlet-mapping></web-app>\n"
        .encode("latin-1"))
    result = worker.process_paths(tmp_path, ["web.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "encoding_undecodable"
    assert result.problems[0].relative_path == "web.xml"
    assert "web.xml" not in result.java_results


def test_process_paths_a_genuine_utf8_web_xml_with_unicode_content_stays_clean(
    tmp_path: Path,
) -> None:
    """Companion control: the same web.xml, correctly UTF-8 encoded."""
    (tmp_path / "web.xml").write_bytes(
        "<web-app><servlet-mapping><servlet-name>café</servlet-name>"
        "<url-pattern>/café/*</url-pattern></servlet-mapping></web-app>\n"
        .encode("utf-8"))
    result = worker.process_paths(tmp_path, ["web.xml"])
    assert result.problems == []
    entry_points = result.java_results["web.xml"]["entry_points"]
    assert entry_points[0]["name"] == "/café/*"


def test_process_paths_a_latin1_tooling_xml_records_encoding_undecodable_not_unsupported_language(
    tmp_path: Path,
) -> None:
    """FIX ROUND 26 (F3 BLOCKER, wrong-data, xml-root-sniff branch): a
    non-adapter-handled XML file (Spring bean config here) that is
    Latin-1-encoded must record `encoding_undecodable`, never guess a
    tier verdict (`unsupported_language`) from a root-element name that
    may itself be corrupted by the undecodable byte sequence."""
    (tmp_path / "beans.xml").write_bytes(
        b'<?xml version="1.0"?>\n<beans><!-- caf\xe9 --></beans>\n')
    result = worker.process_paths(tmp_path, ["beans.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "encoding_undecodable"
    assert result.problems[0].relative_path == "beans.xml"


def test_process_paths_a_latin1_tooling_xml_does_not_degrade_the_run_f3(
    tmp_path: Path,
) -> None:
    """FIX ROUND 27 (twenty-third cold read, F3 MAJOR, wrong-data): a
    binary-excluded and an encoding-undecodable, non-adapter-handled
    .xml file are epistemically identical - this run cannot root-sniff
    either one's own tier. Round 26b's own binary ruling already
    refused to degrade the binary-excluded twin (a tier-2 shape vs. an
    ordinary logback.xml are indistinguishable without reading); the
    encoding-undecodable case must get the SAME treatment, not the
    stale WorkerProblem.degrades_run=True default."""
    (tmp_path / "logback.xml").write_bytes(
        b'<?xml version="1.0"?>\n<configuration><!-- caf\xe9 --></configuration>\n')
    result = worker.process_paths(tmp_path, ["logback.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "encoding_undecodable"
    assert result.problems[0].degrades_run is False


def test_process_paths_a_genuine_utf8_tooling_xml_still_gets_the_tier2_verdict(
    tmp_path: Path,
) -> None:
    """Companion control: an ordinary UTF-8 Spring bean XML must still
    get its existing tier-2 unsupported_language verdict, unaffected by
    the new encoding guard on this decode site."""
    (tmp_path / "beans.xml").write_bytes(
        '<?xml version="1.0"?>\n<beans><!-- café --></beans>\n'.encode("utf-8"))
    result = worker.process_paths(tmp_path, ["beans.xml"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].degrades_run is True


def test_process_paths_a_latin1_properties_file_is_unaffected_by_the_f3_encoding_guard(
    tmp_path: Path,
) -> None:
    """FIX ROUND 27 (F3 MAJOR, sweep): the reviewer's own sweep question -
    do .properties/.md latin-1 twins behave? Yes, unaffected: this
    producer never decodes a .properties file's own content at all (it
    is classified purely by extension in the tier-3 branch), so an
    undecodable encoding here was never a distinguishable case to begin
    with - stays record-only, non-degrading, exactly like its UTF-8
    twin."""
    (tmp_path / "app.properties").write_bytes("café=touché\n".encode("latin-1"))
    result = worker.process_paths(tmp_path, ["app.properties"])
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "unsupported_language"
    assert result.problems[0].degrades_run is False


def test_is_a_code_bearing_extension_worth_degrading_when_silently_excluded_includes_pom_and_web_xml_f4(
) -> None:
    """FIX ROUND 26 (twenty-second cold read, F4 MAJOR, wrong-data): a
    UTF-16-encoded pom.xml/web.xml is a legal input to the same legacy
    Windows tooling that produces a UTF-16 .java file, and trips the
    identical binary-sniff heuristic (discovery.py's own NUL-byte
    prefix check) - but the predicate scan_pipeline.py consults to
    decide whether such a silent exclusion is a genuine, unaffected
    binary blob or a real code file this run failed to read at all only
    ever checked EXTENSIONS, so a basename-matched producer (pom.xml/
    web.xml have no extension of their own the extension-based check
    would recognize) fell through silently - complete, zero problems,
    while a UTF-16 .java correctly degrades. Now consults
    `_ADAPTER_HANDLED_XML_BASENAMES` too, the identical treatment."""
    assert worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        "pom.xml") is True
    assert worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        "some/nested/web.xml") is True
    assert worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        "POM.XML") is True
    assert worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        "readme.md") is False
    assert worker.is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
        "logo.png") is False


def test_process_paths_is_deterministic_regardless_of_input_order(tmp_path: Path) -> None:
    """N4 (cold-read, PR-B fix round 3): comparing bare SETS of sizes
    cannot detect a cross-contamination bug (e.g. a.txt's claim
    accidentally getting b.txt's size) - as long as both sizes appear
    SOMEWHERE across the results, a set comparison passes vacuously
    regardless of which file each is actually attributed to. Comparing
    the relative_path -> byte_count MAPPING is strictly stronger: it
    fails if any single file's size differs from its OWN expected value
    depending on what order it happened to be processed in.

    N3 (fourth cold read, fix round 6): this originally keyed on
    content_digest, since dropped (dead - see WorkerFileClaim); a.txt
    and b.txt are deliberately different SIZES so byte_count alone still
    proves per-file attribution, not just per-file existence."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")
    forward = worker.process_paths(tmp_path, ["a.txt", "b.txt"])
    backward = worker.process_paths(tmp_path, ["b.txt", "a.txt"])
    forward_by_path = {c.relative_path: c.byte_count for c in forward.file_claims}
    backward_by_path = {c.relative_path: c.byte_count for c in backward.file_claims}
    assert forward_by_path == backward_by_path
    assert forward_by_path == {"a.txt": 5, "b.txt": 6}


# ----------------------------------------------------------- _main (worker entrypoint)

def test_main_writes_a_valid_result_for_well_formed_input(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    payload = json.dumps({"root": str(tmp_path), "relative_paths": ["a.txt"]})
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
    exit_code = worker._main([])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == worker.WORKER_SCHEMA_VERSION
    assert out["file_claims"][0]["relative_path"] == "a.txt"


def test_main_refuses_malformed_json_input(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("not json"))
    exit_code = worker._main([])
    assert exit_code == 2
    err = json.loads(capsys.readouterr().err)
    assert "malformed worker input" in err["error"]


def test_main_refuses_input_missing_required_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({"root": "x"})))
    exit_code = worker._main([])
    assert exit_code == 2
    err = json.loads(capsys.readouterr().err)
    assert "root and relative_paths" in err["error"]


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ----------------------------------------------------------- JSON round-trip (B-1 regression)

def test_worker_result_json_round_trip_preserves_every_field(tmp_path: Path) -> None:
    """reviewer-3's B-1 repro, made permanent: adapter claims computed by
    process_paths must survive _result_to_json -> _result_from_json intact.
    Before this fix, java_results was silently dropped by both functions -
    process_paths computed it correctly, but a real scan run through the
    REAL subprocess (which must serialize/deserialize across stdout) always
    reconstructed an empty dict regardless of what was actually parsed."""
    (tmp_path / "A.java").write_text(
        "package p;\nclass A {\n  public static void main(String[] a) {}\n}\n",
        encoding="utf-8",
    )
    computed = worker.process_paths(tmp_path, ["A.java"])
    assert computed.java_results, "process_paths itself must have produced a java_results entry"

    round_tripped = worker._result_from_json(worker._result_to_json(computed))

    assert round_tripped.java_results == computed.java_results
    assert round_tripped.file_claims == computed.file_claims
    assert round_tripped.problems == computed.problems


def test_worker_result_json_round_trip_of_an_empty_java_results_stays_empty(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    computed = worker.process_paths(tmp_path, ["a.txt"])
    round_tripped = worker._result_from_json(worker._result_to_json(computed))
    assert round_tripped.java_results == {}


# ----------------------------------------------------------- run_sanitized_worker (mocked subprocess)

def test_run_sanitized_worker_launches_with_the_sanitized_environment_only(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-never-reach-the-worker")
    captured: dict = {}

    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = json.dumps(worker._result_to_json(
            worker.WorkerResult(schema_version=worker.WORKER_SCHEMA_VERSION)))

    def fake_run(argv, *, input, capture_output, text, env, timeout, check):
        captured["argv"] = argv
        captured["env"] = env
        captured["input"] = input
        captured["timeout"] = timeout
        return _FakeCompleted()

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"])

    assert captured["argv"] == [
        sys.executable, "-s", "-S", "-m", "agenttalk.comprehension.worker",
    ]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert json.loads(captured["input"]) == {"root": str(tmp_path), "relative_paths": ["a.txt"]}
    assert result.schema_version == worker.WORKER_SCHEMA_VERSION


def test_run_sanitized_worker_raises_worker_error_on_nonzero_exit(
    tmp_path: Path, monkeypatch,
) -> None:
    class _FakeCompleted:
        returncode = 1
        stderr = "boom"
        stdout = ""

    monkeypatch.setattr(
        worker.subprocess, "run",
        lambda *a, **k: _FakeCompleted(),  # noqa: ARG005
    )
    with pytest.raises(worker.WorkerError, match="boom"):
        worker.run_sanitized_worker(tmp_path, [])


def test_run_sanitized_worker_raises_worker_error_on_timeout(
    tmp_path: Path, monkeypatch,
) -> None:
    def fake_run(*args, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="worker", timeout=1.0)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    with pytest.raises(worker.WorkerError, match="timed out"):
        worker.run_sanitized_worker(tmp_path, [], timeout_seconds=1.0)


def test_run_sanitized_worker_raises_worker_error_on_malformed_output(
    tmp_path: Path, monkeypatch,
) -> None:
    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = "not json"

    monkeypatch.setattr(
        worker.subprocess, "run",
        lambda *a, **k: _FakeCompleted(),  # noqa: ARG005
    )
    with pytest.raises(worker.WorkerError, match="malformed output"):
        worker.run_sanitized_worker(tmp_path, [])


# ----------------------------------------------------------- real subprocess end-to-end

def test_run_sanitized_worker_end_to_end_real_subprocess(tmp_path: Path) -> None:
    """The one test that actually spawns the real child process under the
    real sanitized (allowlisted) environment.

    B-2 (reviewer-3, PR-B delta review): this used to skip whenever this
    dev environment's installed ``agenttalk`` predated this module, since
    the sanitized environment deliberately excludes PYTHONPATH and the
    child had no other way to resolve the package from a source checkout.
    That reasoning was right about *why* it skipped locally, but wrong to
    conclude the real-install case was unaffected: B-1 meant that wherever
    the child COULD start, its adapter results were silently dropped
    anyway. Now that run_sanitized_worker derives and validates the
    child's import root itself (:func:`worker._derive_child_import_root`)
    rather than relying on inherited PYTHONPATH, this must EXECUTE, not
    skip, regardless of how ``agenttalk`` happens to be installed on the
    machine running this test."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"])
    assert result.file_claims[0].relative_path == "a.txt"
    assert result.file_claims[0].byte_count == 5


def test_run_sanitized_worker_end_to_end_real_subprocess_carries_java_results(
    tmp_path: Path,
) -> None:
    """B-1 + B-2 together, through the REAL subprocess (not process_paths
    called in-process): adapter claims computed by the real child must
    survive the actual stdin/stdout JSON channel, and the child must
    actually be able to start from this source checkout to prove it."""
    (tmp_path / "A.java").write_text(
        "package p;\nclass A {\n  public static void main(String[] a) {}\n}\n",
        encoding="utf-8",
    )
    result = worker.run_sanitized_worker(tmp_path, ["A.java"])
    assert result.java_results, "adapter claims must survive the real subprocess round-trip"
    assert result.java_results["A.java"]["units"][0]["qualified_name"] == "p.A"


def test_run_sanitized_worker_derives_the_child_import_root_from_this_process(
    tmp_path: Path,
) -> None:
    """The child's PYTHONPATH must be THIS function's own derived,
    validated value - never inherited from the caller's ambient
    environment (B-2: an inherited PYTHONPATH is itself an injection
    vector)."""
    import agenttalk

    expected_root = str(Path(agenttalk.__file__).resolve().parent.parent)
    assert worker._derive_child_import_root() == expected_root

    (tmp_path / "a.txt").write_bytes(b"hello")
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"], timeout_seconds=30.0)
    assert result.file_claims[0].relative_path == "a.txt"


def test_run_sanitized_worker_starts_from_a_source_tree_layout_with_no_ambient_pythonpath(
    tmp_path: Path, monkeypatch,
) -> None:
    """B-2 (reviewer-3, PR-B delta review), regression test in the same
    shape as PR-A's
    test_host_identity_succeeds_under_the_dev_gates_allowlisted_environment:
    spawn the real child under the real sanitized env, from exactly the
    layout that broke before this fix - agenttalk importable ONLY via
    PYTHONPATH in a source checkout, with the PARENT's own ambient
    PYTHONPATH removed first, so the child could only start if this
    process derives the import root itself rather than happening to
    inherit a pre-set value."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    (tmp_path / "a.txt").write_bytes(b"hello")
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"])
    assert result.file_claims[0].relative_path == "a.txt"
    assert result.file_claims[0].byte_count == 5
