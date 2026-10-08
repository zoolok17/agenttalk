import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import dev_gate


def test_ci_voting_jobs_invoke_only_the_committed_gate_plan() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    workflow = workflow.split("  windows-mode-trial:")[0]  # the existing voting jobs, not the extra experiment
    workflow = workflow.split("  dev-gate-leg:", 1)[1]  # full gate, not the docs-only checks

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


def test_windows_ci_job_ceiling_matches_the_windows_capacity_stopgap() -> None:
    """Windows CI capacity stopgap (following the #197 precedent 87c529a): pin
    the actual numbers so a silent drift back to the stale 180-min ceiling
    (which PR #230 measured at 177/180 min, 98%) is a test failure, not a
    rediscovery under load. Linux/macOS keep the tight cap."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    # #378: 330 min = two Windows pytest runs at the 9000s windows_timeout_seconds
    # (150 min each) plus about 30 min; temporary, expiring 2026-11-06.
    assert "timeout_minutes: 330  # #378 temporary margin, expires 2026-11-06" in workflow
    assert workflow.count("timeout_minutes: 90") == 2
    for stale in ("timeout_minutes: 270", "timeout_minutes: 180", "timeout_minutes: 120"):
        assert stale not in workflow
    assert "#378, TEMPORARY, expires 2026-11-06" in workflow
    manifest = json.loads(Path("dev-gate.json").read_text(encoding="utf-8"))
    windows_pytest_minutes = manifest["checks"]["pytest"]["windows_timeout_seconds"] / 60
    assert 2 * windows_pytest_minutes + 30 == 330


def test_xdist_parallel_is_scoped_to_posix_legs_by_the_matrix_not_dev_gate_py() -> None:
    """ci-xdist-posix: the OS-scoped decision (two xdist workers on
    Linux/macOS, Windows stays serial) must live in the workflow matrix
    expression, never as a runtime platform check inside dev_gate.py (which
    runs identically on every leg) - the two pytest-mode checks read only
    an opaque env var, the same shape as the existing
    AGENTTALK_AUTHORIZE_SYMLINK_DEVMODE/AGENTTALK_DEV_GATE_COMMITTED_SRC
    precedents."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert (
        "AGENTTALK_DEV_GATE_POSIX_PARALLEL: ${{ matrix.os.id != 'windows' && '1' || '' }}"
        in workflow
    )
    dev_gate_source = Path("src/agenttalk/dev_gate.py").read_text(encoding="utf-8")
    assert 'os.environ.get("AGENTTALK_DEV_GATE_POSIX_PARALLEL")' in dev_gate_source
    assert "sys.platform" not in dev_gate_source


# Every variable the dev-gate leg job sets, and who it is for. The gate runs pytest with
# an allowlisted environment, so a variable meant for the tests is lost unless the gate
# forwards it; a new variable must be given a role here (#320).
_LEG_ENV_ROLES = {
    # for the gate itself
    "PYTHONPATH": "gate",  # the gate sets its own per test mode
    "PYTHONDONTWRITEBYTECODE": "gate",  # the gate always sets it
    "AGENTTALK_DEV_GATE_POSIX_PARALLEL": "gate",  # read by dev_gate.py
    # for the tests, forwarded: dev_gate.FORWARDED_TEST_VARIABLES
    "AGENTTALK_TEST_GATEWAY_PORTS": "forwarded",
    # For the tests but deliberately not forwarded: it authorises tests/conftest.py to
    # switch on Windows developer mode, and with it CI and GITHUB_ACTIONS would have to
    # pass too. GitHub's hosted Windows runners are elevated and create symlinks
    # without developer mode; #320 showed no symlink test skips on a Windows leg.
    "AGENTTALK_AUTHORIZE_SYMLINK_DEVMODE": "not forwarded",
}


def _leg_env() -> dict[str, str]:
    """The env block of the dev-gate leg job, as name: raw value text."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    leg_job = workflow[workflow.index("  dev-gate-leg:\n"):workflow.index("  dev-gate-aggregate:\n")]
    leg_env = leg_job[leg_job.index("    env:\n") + len("    env:\n"):leg_job.index("    timeout-minutes:")]
    variables = {}
    for line in leg_env.splitlines():
        if line.startswith("      ") and not line.lstrip().startswith("#") and ":" in line:
            name, value = line.strip().split(":", 1)
            variables[name] = value.strip()
    return variables


def test_every_variable_the_dev_gate_leg_sets_has_a_declared_role() -> None:
    """A variable added to the leg job without a role here fails, so one meant for the
    tests cannot be dropped silently by the gate's allowlisted environment."""
    assert set(_leg_env()) == set(_LEG_ENV_ROLES)


def test_every_variable_meant_for_the_tests_is_forwarded_with_the_workflows_value() -> None:
    forwarded = {name for name, role in _LEG_ENV_ROLES.items() if role == "forwarded"}
    assert forwarded == set(dev_gate.FORWARDED_TEST_VARIABLES)
    leg_env = _leg_env()
    for name in forwarded:
        assert f"'{dev_gate.FORWARDED_TEST_VARIABLES[name]}'" in leg_env[name]


def test_every_dev_gate_leg_opts_in_to_the_gateway_port_tests() -> None:
    """#318: the gateway-port tests are skipped unless AGENTTALK_TEST_GATEWAY_PORTS=1;
    the dev-gate leg job (every matrix member) sets it, so CI coverage is unchanged."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    leg_job = workflow[workflow.index("  dev-gate-leg:\n"):workflow.index("  dev-gate-aggregate:\n")]
    leg_env = leg_job[leg_job.index("    env:\n"):leg_job.index("    timeout-minutes:")]
    assert "      AGENTTALK_TEST_GATEWAY_PORTS: '1'\n" in leg_env


def test_security_workflow_contains_only_declared_codeql_exception() -> None:
    workflow = Path(".github/workflows/security.yml").read_text(encoding="utf-8")

    assert "  codeql:" in workflow
    for migrated_job in ("  ruff:", "  bandit:", "  pip-audit:", "  gitleaks:", "  semgrep:", "  zizmor:"):
        assert migrated_job not in workflow
    assert "github/codeql-action/init@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow
    assert "github/codeql-action/analyze@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow


def _scope():
    return runpy.run_path("scripts/ci_scope.py")


@pytest.mark.parametrize("paths, expected", [
    (["README.md"], True), (["CHANGELOG.md"], True), (["SECURITY.md"], True),
    (["docs/guide.md", "docs/design/a plan.md", "README.md"], True),
    ([], False), (["docs/example.py"], False), (["design/prototype.html"], False),
    (["src/agenttalk/README.md"], False), (["src/agenttalk/skills/foo/SKILL.md"], False),
    (["skills/foo/SKILL.md"], False), (["tests/README.md"], False),
    ([".github/README.md"], False), ([".github/workflows/tests.yml"], False),
    (["pyproject.toml"], False), (["CHANGELOG.md", "src/agenttalk/cli.py"], False),
    (["docs/../src/a.md"], False), (["docs/SKILL.md"], False),
    (["docs/skills/a.md"], False), (["docs/a.md\nsource.py"], False),
])
def test_docs_only_is_a_narrow_all_files_rule(paths, expected):
    assert _scope()["docs_only"](paths) is expected


def test_pr_scope_reads_the_whole_diff_and_both_sides_of_renames(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], timeout=30).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic Author")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "gc.auto", "0")
    git("config", "maintenance.auto", "false")
    (tmp_path / "code.py").write_text("content\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    for n in range(301):
        (tmp_path / "docs" / f"page{n}.md").write_text("documentation\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "docs")
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": git("rev-parse", "HEAD")}}}
    classify = _scope()["pr_is_docs_only"]
    assert classify(event, tmp_path)
    git("mv", "code.py", "docs/code.md")
    git("commit", "-qm", "rename code into docs")
    event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    assert not classify(event, tmp_path)
    event["pull_request"]["head"]["sha"] = "-invalid"
    with pytest.raises(ValueError):
        classify(event, tmp_path)
    event["pull_request"]["head"]["sha"] = "f" * 40
    with pytest.raises(subprocess.CalledProcessError):
        classify(event, tmp_path)


@pytest.mark.parametrize("event_name", ["push", "schedule", "workflow_dispatch"])
def test_only_prs_can_take_the_lighter_path(event_name, tmp_path):
    env = {**os.environ, "GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(tmp_path / "absent")}
    result = subprocess.run([sys.executable, "scripts/ci_scope.py"], env=env,
                            capture_output=True, text=True, timeout=30, check=True)
    assert result.stdout == "docs_only=false\n"


def test_bad_pr_event_fails_without_a_success_output(tmp_path):
    event = tmp_path / "event.json"
    event.write_text("{}", encoding="utf-8")
    env = {**os.environ, "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event)}
    result = subprocess.run([sys.executable, "scripts/ci_scope.py"], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert result.stdout == ""


def test_workflow_scope_preserves_full_events_and_reports_docs_result():
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    triggers = workflow.split("jobs:")[0]
    assert "  pull_request:\n    branches: [master]" in triggers
    assert "  push:\n    branches: [master]" in triggers
    assert "paths:" not in triggers and "paths-ignore:" not in triggers
    jobs = workflow.split("  dev-gate-leg:")
    assert "python scripts/ci_scope.py" in jobs[0]
    assert "needs: scope" in jobs[1].split("  dev-gate-aggregate:")[0]
    assert "if: needs.scope.outputs.docs_only == 'false'" in jobs[1]
    assert "needs: [scope, docs-checks, dev-gate-leg]" in workflow
    assert "SCOPE_RESULT: ${{ needs.scope.result }}" in workflow
    assert "DOCS_RESULT: ${{ needs.docs-checks.result }}" in workflow
    assert "LEG_RESULT: ${{ needs.dev-gate-leg.result }}" in workflow
    assert 'test "$SCOPE_RESULT" = success' in workflow
    assert 'test "$DOCS_RESULT" = success' in workflow
    assert 'test "$LEG_RESULT" = success' in workflow
    docs = workflow.split("  docs-checks:")[1].split("  dev-gate-leg:")[0]
    assert "if: needs.scope.outputs.docs_only == 'true'" in docs
    for check in ("test_docs_plain_voice.py", "test_dev_gate_docs.py", "gitleaks git", "zizmor"):
        assert check in docs
    for job in ("windows-mode-trial", "windows-trial-aggregate"):
        body = workflow.split(f"  {job}:")[1].split("    steps:")[0]
        assert "github.event_name == 'push' && github.ref == 'refs/heads/master'" in body
        assert "workflow_dispatch" not in body and "pull_request" not in body
    aggregate = workflow.split("  dev-gate-aggregate:")[1].split("  windows-mode-trial:")[0]
    assert "    if: always()\n" in aggregate
    assert aggregate.count("if: always() && needs.scope.outputs.docs_only == 'false'") == 6
    assert "Documentation checks passed; full release gate not run." in aggregate
