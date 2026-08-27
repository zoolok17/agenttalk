"""#55 slice-1 PR-B fix round, F-1 (reviewer-3, PR-B delta review): the
boundary's existence proof.

"The pipeline tests replace the subprocess entry point with a direct
in-process call, so every pipeline test bypasses the boundary where the
loss occurs" - test_comprehension_scan_pipeline.py and
test_comprehension_cli.py both monkeypatch
scan_pipeline.worker.run_sanitized_worker to an in-process call for speed,
which is fine for pipeline-wiring coverage (reviewer-3: "Pipeline tests may
keep the in-process stub for speed") but means NOTHING in that suite ever
exercised the real stdin/stdout JSON channel the worker actually uses in
production - exactly the channel B-1 silently dropped adapter claims on.

This module deliberately does NOT stub run_sanitized_worker. It runs the
real, full nine-step pipeline over a small fixture repo through the actual
child process, under the real sanitized environment, and asserts NON-EMPTY
adapter-derived artifacts - an assertion that fails if either claims are
silently dropped (B-1) or the child cannot start at all (B-2). It must
execute (not skip) on every platform this repository's CI runs on; there is
no opt-in gate here, unlike test_comprehension_network_deny.py's OS-level
mechanism tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agenttalk.comprehension import scan_pipeline


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")


def _write_fixture_repo(root: Path) -> None:
    app_dir = root / "src" / "main" / "java" / "p"
    app_dir.mkdir(parents=True)
    (app_dir / "App.java").write_text(
        "package p;\n"
        "class App {\n"
        "  public static void main(String[] args) {\n"
        "    new Helper().run();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (app_dir / "Helper.java").write_text(
        "package p;\nclass Helper {\n  void run() {}\n}\n",
        encoding="utf-8",
    )
    (root / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )


def test_full_scan_through_the_real_worker_subprocess_yields_nonempty_adapter_artifacts(
    tmp_path: Path,
) -> None:
    """The one test in this suite that spawns the REAL sanitized worker
    subprocess through the full run_scan() pipeline, with no monkeypatched
    boundary anywhere. Fails outright (rather than silently passing on an
    empty result) if B-1 regresses - adapter claims dropped across the
    JSON channel - or if B-2 regresses - the child cannot start at all
    from this exact source-tree layout."""
    _init_git_repo(tmp_path)
    _write_fixture_repo(tmp_path)

    outcome = scan_pipeline.run_scan(tmp_path)
    assert outcome.status == "complete"

    modules_doc = json.loads((outcome.run_dir / "modules.json").read_text(encoding="utf-8"))
    dependencies_doc = json.loads(
        (outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    features_doc = json.loads((outcome.run_dir / "features.json").read_text(encoding="utf-8"))

    component_units = [u for u in modules_doc["units"] if u["kind"] == "component"]
    assert component_units, (
        "no component units survived the real worker subprocess - adapter claims were "
        "silently dropped (B-1) or the worker never ran at all")

    assert dependencies_doc["edges"], (
        "no dependency edges survived the real worker subprocess (B-1/B-3 regression)")
    build_edges = [e for e in dependencies_doc["edges"] if e["relation"] == "build"]
    assert build_edges and build_edges[0]["target_external"] == "org.springframework:spring-core", (
        "pom.xml's build edge did not survive the real worker subprocess (B-3 regression)")

    assert features_doc["entry_points"], (
        "no entry points survived the real worker subprocess (B-1 regression)")
    assert any(e["kind"] == "cli_main" for e in features_doc["entry_points"])
