import re
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


def test_security_workflow_contains_only_declared_codeql_exception() -> None:
    workflow = Path(".github/workflows/security.yml").read_text(encoding="utf-8")

    assert "  codeql:" in workflow
    for migrated_job in ("  ruff:", "  bandit:", "  pip-audit:", "  gitleaks:", "  semgrep:", "  zizmor:"):
        assert migrated_job not in workflow
    assert "github/codeql-action/init@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow
    assert "github/codeql-action/analyze@78ed0c7291d93e40c51b085850dc669a4c3ab73b" in workflow


def test_release_provenance_workflow_is_explicit_sha_bound_and_read_only() -> None:
    workflow = Path(".github/workflows/release-provenance.yml").read_text(encoding="utf-8")
    trigger = workflow.split("permissions:", 1)[0]

    assert "workflow_dispatch:" in trigger
    assert "candidate_sha:" in trigger
    assert "version:" in trigger
    assert trigger.count("required: true") == 2
    for forbidden_trigger in (
        "push:",
        "pull_request:",
        "pull_request_target:",
        "workflow_run:",
        "release:",
        "schedule:",
    ):
        assert forbidden_trigger not in trigger
    assert 'CANDIDATE_REF: ${{ github.ref }}' in workflow
    assert 'EVENT_SHA: ${{ github.sha }}' in workflow
    assert 'RELEASE_WORKFLOW_SHA: ${{ github.workflow_sha }}' in workflow
    assert 'CANDIDATE_SHA: ${{ inputs.candidate_sha }}' in workflow
    assert 'ref: ${{ inputs.candidate_sha }}' in workflow
    assert "candidate_sha: ${{ inputs.candidate_sha }}" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "contents: write" not in workflow
    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow
    assert "environment:" not in workflow
    assert "git tag" not in workflow
    assert "gh release" not in workflow
    assert "refs/tags/" not in workflow


def test_release_provenance_workflow_retains_gate_built_bytes_and_all_evidence() -> None:
    release = Path(".github/workflows/release-provenance.yml").read_text(encoding="utf-8")
    tests = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "uses: ./.github/workflows/tests.yml" in release
    assert "uses: ./.github/workflows/security.yml" in release
    assert "prepare_release_provenance.py assemble" in release
    assert "pattern: ${{ env.ARTIFACT_PREFIX }}-leg-*" in release
    assert "retention-days: 90" in release
    assert "artifact-digest" in release
    assert release.count("continue-on-error: true") == 3
    assert "if: ${{ !cancelled() && needs.validate.result == 'success' }}" in release
    assert "!cancelled() && success()" in release
    assert "if: always() && needs.gate.result == 'success'" not in release
    assert "release_evidence_stale: use a fresh dispatch instead of a partial workflow rerun" in release
    assert "run-id:" not in release
    assert "github-token:" not in release
    assert "repository:" not in release.split("Download gate evidence", 1)[-1]
    assert "python -m build" not in release
    assert "prepare_release_provenance.py export" in tests
    assert "matrix.os.id == 'linux' && matrix.python-version == '3.12'" in tests
    assert '--temp-root "${{ runner.temp }}/dev-gate-run"' in tests


def test_release_provenance_workflow_external_actions_are_commit_pinned() -> None:
    workflow = Path(".github/workflows/release-provenance.yml").read_text(encoding="utf-8")

    external_actions = re.findall(r"uses: ([^./\s][^@\s]+)@([^\s]+)", workflow)
    assert external_actions
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in external_actions)


def test_reusable_gate_and_codeql_checkout_the_explicit_candidate() -> None:
    tests = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    security = Path(".github/workflows/security.yml").read_text(encoding="utf-8")

    assert "workflow_call:" in tests
    assert "workflow_call:" in security
    assert "candidate_sha:" in tests
    assert "candidate_sha:" in security
    assert tests.count("ref: ${{ inputs.candidate_sha || github.sha }}") == 2
    assert security.count("ref: ${{ inputs.candidate_sha || github.sha }}") == 1
    assert tests.split("permissions:", 1)[0].count("required: true") == 4
    assert security.split("permissions:", 1)[0].count("required: true") == 1
    assert "group: tests-${{ github.workflow }}-${{ github.ref }}" in tests
    assert "group: security-${{ github.workflow }}-${{ github.ref }}" in security
    assert "inputs.artifact_prefix || 'dev-gate'" in tests
