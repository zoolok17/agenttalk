from pathlib import Path

from agenttalk import dev_gate


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


def test_windows_ci_job_ceiling_matches_the_windows_capacity_stopgap() -> None:
    """Windows CI capacity stopgap (following the #197 precedent 87c529a): pin
    the actual numbers so a silent drift back to the stale 180-min ceiling
    (which PR #230 measured at 177/180 min, 98%) is a test failure, not a
    rediscovery under load. Linux/macOS keep the tight cap."""
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "timeout_minutes: 270" in workflow
    assert workflow.count("timeout_minutes: 90") == 2
    assert "timeout_minutes: 180" not in workflow
    assert "timeout_minutes: 120" not in workflow


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
