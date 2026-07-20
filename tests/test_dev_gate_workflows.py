from pathlib import Path


def test_ci_voting_jobs_invoke_only_the_committed_gate_plan() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "id: linux" in workflow
    assert "id: windows" in workflow
    assert "id: macos" in workflow
    assert "python-version: ['3.10', '3.11', '3.12', '3.13']" in workflow
    assert "fetch-depth: 0" in workflow
    assert workflow.count("python -m agenttalk dev-gate") == 2
    assert workflow.count("python -m pip install -r dev-gate-requirements.txt") == 1
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


def test_security_workflow_contains_only_declared_codeql_exception() -> None:
    workflow = Path(".github/workflows/security.yml").read_text(encoding="utf-8")

    assert "  codeql:" in workflow
    for migrated_job in ("  ruff:", "  bandit:", "  pip-audit:", "  gitleaks:", "  semgrep:", "  zizmor:"):
        assert migrated_job not in workflow
    assert "github/codeql-action/init@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow
    assert "github/codeql-action/analyze@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow
