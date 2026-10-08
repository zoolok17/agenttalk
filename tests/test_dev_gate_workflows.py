import ast
import json
import itertools
import os
import runpy
import shutil
import subprocess
import sys
import textwrap
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


def _bash():
    bash = shutil.which("bash")
    if sys.platform == "win32":
        # PATH's bash may be the WSL launcher, even when Git for Windows is installed.
        git = shutil.which("git")
        parents = Path(git).resolve().parents if git else ()
        bash = next((str(parent / folder / "bash.exe") for parent in parents
                     for folder in ("bin", "usr/bin") if (parent / folder / "bash.exe").is_file()), None)
    assert bash and Path(bash).is_file(), "Git Bash (Windows) or bash is needed for the CI shell contract"
    return bash


@pytest.mark.parametrize("git_folder", ["cmd", "mingw64/bin", "usr/bin"])
@pytest.mark.parametrize("bash_folder", ["bin", "usr/bin"])
def test_windows_bash_searches_git_parents_not_the_wsl_stub(tmp_path, monkeypatch, git_folder, bash_folder):
    git = tmp_path / "Git" / git_folder / "git.exe"
    bash = tmp_path / "Git" / bash_folder / "bash.exe"
    stub = tmp_path / "Windows/System32/bash.exe"
    for path in (git, bash, stub):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: str(git if name == "git" else stub))
    assert Path(_bash()) == bash


def test_windows_bash_refuses_the_wsl_stub_when_git_is_absent(tmp_path, monkeypatch):
    stub = tmp_path / "Windows/System32/bash.exe"
    stub.parent.mkdir(parents=True)
    stub.touch()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "git" else str(stub))
    with pytest.raises(AssertionError, match="Git Bash"):
        _bash()


@pytest.mark.parametrize("source", ["# comment-only.md\n", 'DOC = f"{folder}/formatted-only.md"\n'])
def test_mentions_without_a_complete_string_literal_require_full_checks(tmp_path, source):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_reference.py").write_text(source, encoding="utf-8")
    name = "comment-only.md" if source.startswith("#") else "formatted-only.md"
    assert not _scope()["docs_only"]([f"docs/{name}"], tmp_path)


def test_document_reference_in_nested_test_folder_requires_full_checks(tmp_path):
    nested = tmp_path / "tests/support"
    nested.mkdir(parents=True)
    (nested / "reader.py").write_text('DOC = "nested-only.md"\n', encoding="utf-8")
    assert not _scope()["docs_only"](["docs/nested-only.md"], tmp_path)


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
def test_docs_only_is_a_narrow_all_files_rule(paths, expected, tmp_path):
    (tmp_path / "tests").mkdir()
    assert _scope()["docs_only"](paths, tmp_path) is expected


def test_documents_used_by_tests_are_never_skippable():
    classify = _scope()["docs_only"]
    for path in ("docs/supervisor-hosting.md", "docs/ISSUES.md",
                 "docs/examples/write-for-humans-samples.md", "README.md", "CHANGELOG.md"):
        assert not classify([path], Path.cwd()), path


@pytest.mark.parametrize("name", ["new-contract.md", "new contract.md", "quoted'contract.md"])
def test_new_document_reference_automatically_requires_full_checks(tmp_path, name):
    tests = tmp_path / "tests"
    tests.mkdir()
    path = f"docs/{name}"
    classify = _scope()["docs_only"]
    assert classify([path], tmp_path)
    (tests / "test_new.py").write_text(
        f'from pathlib import Path\ndef test_doc():\n    Path({path!r}).read_text()\n',
        encoding="utf-8",
    )
    assert not classify([path], tmp_path)


def test_computed_document_read_cannot_silently_escape_the_scope_scan(tmp_path):
    from doc_read_guard import DocReadGuard

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ci_scope.py").write_text(Path("scripts/ci_scope.py").read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    document = tmp_path / "docs" / ("-".join(["computed", "name"]) + ".md")
    document.write_text("test input", encoding="utf-8")
    guard = DocReadGuard(tmp_path)
    with pytest.raises(AssertionError, match="Tests opened documents that CI would skip"):
        with guard.check():
            document.read_text(encoding="utf-8")
    # Once declared in a test, the same read needs the full gate and is allowed.
    (tmp_path / "tests/test_reader.py").write_text(f'DOC = {document.name!r}\n', encoding="utf-8")
    guard = DocReadGuard(tmp_path)
    with guard.check():
        document.read_text(encoding="utf-8")


def _guard_probe(tmp_path, *, phase, workers=0, classifier=True):
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    document = tmp_path / "docs" / ("-".join(["Unlisted", "Guide"]) + ".md")
    document.write_text("test input", encoding="utf-8")
    if classifier:
        (tmp_path / "scripts").mkdir()
        shutil.copyfile("scripts/ci_scope.py", tmp_path / "scripts/ci_scope.py")
    # Copy the actual guard hooks, without unrelated gateway or process fixtures.
    source = Path("tests/conftest.py").read_text(encoding="utf-8")
    names = {"pytest_sessionstart", "pytest_sessionfinish", "pytest_make_collect_report",
             "pytest_runtest_makereport", "_check_document_reads"}
    hooks = ["\n".join(source.splitlines()[min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1:
                                           node.end_lineno])
             for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names]
    (tmp_path / "tests/conftest.py").write_text(
        "import pytest\nimport warnings\nfrom pathlib import Path\nfrom doc_read_guard import DocReadGuard\n"
        + "\n".join(hooks),
        encoding="utf-8",
    )
    read = '(Path("docs") / ("-".join(["Unlisted", "Guide"]) + ".md")).read_text()'
    body = {
        "collection": read + '\ndef test_ok():\n    pass\n',
        "test": 'def test_ok():\n    ' + read + '\n',
        "session-teardown": '@pytest.fixture(scope="session", autouse=True)\ndef late_read():\n'
                            '    yield\n    ' + read + '\ndef test_ok():\n    pass\n',
    }[phase]
    (tmp_path / "tests/test_reader.py").write_text('import pytest\nfrom pathlib import Path\n' + body,
                                                 encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path("tests").resolve()), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    env.pop("AGENTTALK_ROOT", None)
    args = ["-p", "xdist.plugin", "-n", str(workers)] if workers else []
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("phase", ["collection", "test", "session-teardown"])
def test_guard_reports_computed_reads_as_failures_in_serial_and_xdist(tmp_path, phase, workers):
    result = _guard_probe(tmp_path, phase=phase, workers=workers)
    output = result.stdout + result.stderr
    assert result.returncode in (1, 2), output
    assert "Tests opened documents that CI would skip: docs/Unlisted-Guide.md" in output
    assert "INTERNALERROR" not in output


def test_guard_missing_classifier_warns_without_breaking_sdist_tests(tmp_path):
    result = _guard_probe(tmp_path, phase="test", classifier=False)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Document read guard disabled: scripts/ci_scope.py is missing" in output


def test_pr_scope_reads_the_whole_diff_and_both_sides_of_renames(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], timeout=30).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic Author")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "gc.auto", "0")
    git("config", "maintenance.auto", "false")
    (tmp_path / "tests").mkdir()
    (tmp_path / "code.py").write_text("content\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    assert not _scope()["pr_is_docs_only"](
        {"pull_request": {"base": {"sha": base}, "head": {"sha": base}}}, tmp_path)
    (tmp_path / "docs").mkdir()
    for n in range(301):
        (tmp_path / "docs" / f"page{n}.md").write_text("documentation\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "docs")
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": git("rev-parse", "HEAD")}}}
    classify = _scope()["pr_is_docs_only"]
    assert classify(event, tmp_path)
    # Master changes code after the branch fork: a two-dot diff wrongly treats
    # that base-only change as part of the documentation PR.
    git("checkout", "-qb", "advanced-base", base)
    (tmp_path / "code.py").write_text("new master code\n", encoding="utf-8")
    git("commit", "-qam", "advance base")
    event["pull_request"]["base"]["sha"] = git("rev-parse", "HEAD")
    assert classify(event, tmp_path)
    git("checkout", "--detach", event["pull_request"]["head"]["sha"])
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
    assert 'git show "$BASE_SHA:scripts/ci_scope.py"' in jobs[0]
    assert 'python "$RUNNER_TEMP/ci_scope.py"' in jobs[0]
    assert 'echo "docs_only=false"' in jobs[0]  # first rollout: no classifier on base
    assert "needs: scope" in jobs[1].split("  dev-gate-aggregate:")[0]
    assert "if: needs.scope.outputs.docs_only == 'false'" in jobs[1]
    assert "needs: [scope, docs-checks, dev-gate-leg]" in workflow
    assert "SCOPE_RESULT: ${{ needs.scope.result }}" in workflow
    assert "DOCS_RESULT: ${{ needs.docs-checks.result }}" in workflow
    assert "LEG_RESULT: ${{ needs.dev-gate-leg.result }}" in workflow
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


def test_aggregate_executes_all_192_outcome_combinations(tmp_path):
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Require the selected checks to succeed")[1].split("      - uses:")[0]
    script = textwrap.dedent(step.split("        run: |\n")[1])
    outcomes = ("success", "failure", "cancelled", "skipped")
    for scope, docs_only, docs, legs in itertools.product(outcomes, ("true", "false", ""), outcomes, outcomes):
        expected = scope == "success" and ((docs_only == "true" and docs == "success")
                                          or (docs_only == "false" and legs == "success"))
        env = {**os.environ, "SCOPE_RESULT": scope, "DOCS_ONLY": docs_only,
               "DOCS_RESULT": docs, "LEG_RESULT": legs, "GITHUB_STEP_SUMMARY": (tmp_path / "summary").as_posix()}
        result = subprocess.run([_bash(), "--noprofile", "--norc", "-e", "-c", script],
                                env=env, capture_output=True, timeout=10)
        assert (result.returncode == 0) == expected, (scope, docs_only, docs, legs, result.stderr)


@pytest.mark.parametrize("base_has_classifier", [False, True])
def test_scope_shell_uses_the_base_copy_or_runs_full_checks(tmp_path, base_has_classifier):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], timeout=30).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic Author")
    git("config", "user.email", "synthetic@example.invalid")
    git("config", "gc.auto", "0")
    git("config", "maintenance.auto", "false")
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "code.py").write_text("base\n", encoding="utf-8")
    classifier = tmp_path / "scripts/ci_scope.py"
    if base_has_classifier:
        classifier.write_text(Path("scripts/ci_scope.py").read_text(encoding="utf-8"), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/note.md").write_text("prose\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "docs")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": base},
                                                  "head": {"sha": git("rev-parse", "HEAD")}}}), encoding="utf-8")
    runner = tmp_path / "runner"
    runner.mkdir()
    env = {**os.environ, "GITHUB_EVENT_NAME": "pull_request", "BASE_SHA": base,
           "GITHUB_EVENT_PATH": str(event), "RUNNER_TEMP": runner.as_posix(),
           "GITHUB_OUTPUT": (runner / "output").as_posix()}
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Classify all changed paths")[1].split("  docs-checks:")[0]
    script = textwrap.dedent(step.split("        run: |\n")[1])
    # Poison just the checkout: even an honest docs PR must use the base bytes.
    classifier.write_text('raise RuntimeError("PR classifier ran")\n', encoding="utf-8")
    subprocess.run([_bash(), "--noprofile", "--norc", "-e", "-c", script],
                   cwd=tmp_path, env=env, capture_output=True, timeout=30, check=True)
    assert (runner / "output").read_text().strip() == f"docs_only={str(base_has_classifier).lower()}"
