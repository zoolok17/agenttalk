from pathlib import Path


def test_ci_voting_jobs_invoke_only_the_committed_gate_plan() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "id: linux" in workflow
    assert "id: windows" in workflow
    assert "id: macos" in workflow
    assert "python-version: ['3.10', '3.11', '3.12', '3.13']" in workflow
    assert "fetch-depth: 0" in workflow
    assert workflow.count("python -I -m agenttalk dev-gate") == 2
    assert "python -m agenttalk dev-gate" not in workflow
    assert workflow.count("python -I -m pip install -r dev-gate-requirements.txt") == 2
    assert workflow.count("python -I -m pip install --no-deps --no-build-isolation .") == 2
    assert "pip install -e" not in workflow
    for escaped_tool_argv in (
        "python -m pytest",
        "ruff check",
        "bandit -r",
        "pip-audit --strict",
        "semgrep scan",
        "zizmor .github",
        "gitleaks git",
        "python -m build",
    ):
        assert escaped_tool_argv not in workflow


def test_ci_evidence_actions_and_gitleaks_archive_are_immutable() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in workflow
    assert "gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz" in workflow
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in workflow
    assert "if-no-files-found: error" in workflow
    assert "if: always()" in workflow


def test_leg_upload_includes_the_json_and_its_sibling_logs() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    leg = workflow.split("  dev-gate-aggregate:")[0]
    assert '--evidence "${{ runner.temp }}/dev-gate-evidence/dev-gate-evidence.json"' in leg
    assert "path: ${{ runner.temp }}/dev-gate-evidence/\n" in leg


def test_leg_uploads_timings_as_a_separate_unverified_artifact() -> None:
    """RECAST (#231, stopping rule): the timing record is DIAGNOSTICS, never
    evidence - it must be a SEPARATE artifact from dev-gate-evidence, using
    the same pinned upload-artifact action, tolerant of never having been
    produced at all (if-no-files-found: ignore, unlike the evidence upload's
    if-no-files-found: error), and uploaded even if a prior step failed."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    leg = workflow.split("  dev-gate-aggregate:")[0]
    assert "AGENTTALK_DEV_GATE_TIMINGS_DIR: ${{ runner.temp }}/dev-gate-timings" in leg
    assert "name: dev-gate-timings-${{ matrix.os.id }}-${{ matrix.python-version }}" in leg
    assert "path: ${{ runner.temp }}/dev-gate-timings/\n" in leg
    assert "if-no-files-found: ignore" in leg
    timings_step = leg.split("- name: Upload leg timings")[1]
    assert "if: always()" in timings_step.split("- name:")[0]
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in timings_step.split("- name:")[0]


def test_timings_upload_is_non_voting_but_evidence_upload_stays_strict() -> None:
    """FIX ROUND 1 (#231 recast, codex cold read dev-5, M2): if-no-files-found:
    ignore only covers an EMPTY search - a real read/upload-service error in
    the pinned action still calls core.setFailed and would fail the leg over
    a diagnostic. The timings step needs its own continue-on-error: true; the
    evidence step must NOT have it, since a broken evidence upload is a real
    gate failure."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    leg = workflow.split("  dev-gate-aggregate:")[0]
    evidence_step = leg.split("- name: Upload leg evidence")[1].split("- name:")[0]
    timings_step = leg.split("- name: Upload leg timings")[1].split("- name:")[0]
    assert "continue-on-error" not in evidence_step
    assert "continue-on-error: true" in timings_step


def test_windows_ci_job_ceiling_matches_the_231_f3_stopgap() -> None:
    """#231 F3 (Windows CI capacity stopgap, following the #197 precedent
    87c529a): pin the actual numbers so a silent drift back to the stale
    180-min ceiling (which PR #230 measured at 177/180 min, 98%) is a test
    failure, not a rediscovery under load. Linux/macOS keep the tight cap."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "timeout_minutes: 270" in workflow
    assert workflow.count("timeout_minutes: 90") == 2
    assert "timeout_minutes: 180" not in workflow
    assert "timeout_minutes: 120" not in workflow


def test_security_workflow_contains_only_declared_codeql_exception() -> None:
    workflow = Path(".github/workflows/security.yml").read_text(encoding="utf-8")

    assert "  codeql:" in workflow
    for migrated_job in ("  ruff:", "  bandit:", "  pip-audit:", "  gitleaks:", "  semgrep:", "  zizmor:"):
        assert migrated_job not in workflow
    assert "github/codeql-action/init@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow
    assert "github/codeql-action/analyze@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow
