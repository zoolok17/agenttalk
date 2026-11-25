"""#907: the planted-fault runner (`docs/verification/907/faults.ps1`) must never discard an edit it did not make.

The runner changes tracked files in place and puts them back with `git checkout`. A file with uncommitted edits would
lose them, so it must refuse to start when ANY file that a selected fault changes is dirty (the list is derived from the
faults themselves), and when `git status` cannot be read at all. These tests run it in a throw-away git repository with
stand-in files, on every PowerShell that is installed; the faults are never planted except in the one control run.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "docs" / "verification" / "907" / "faults.ps1"
SHELLS = [found for found in (shutil.which("powershell"), shutil.which("pwsh")) if found]
GIT = shutil.which("git")

pytestmark = [
    pytest.mark.skipif(not SHELLS, reason="no PowerShell on this machine"),
    pytest.mark.skipif(GIT is None, reason="git is not on PATH"),
]

# The files the faults change (the runner itself too); the job module holds the text that fault F01 looks for, so a
# control run can plant it.
FILES = {
    "src/agenttalk/cli.py": "def cli():\n    return 1\n",
    "src/agenttalk/git_job.py": (
        "class Native:\n    def create_job(self):\n"
        "        handle = self._kernel.CreateJobObjectW(None, None)\n"
        "        return int(handle) if handle else None\n"
    ),
    "scripts/check_git_job_scheduled_task.ps1": "# the scheduled-task script\n",
    "tests/conftest.py": "# the test configuration\n",
    "docs/verification/907/faults.ps1": "# a stand-in for the runner itself: some faults change the runner\n",
    # The tests that fault F01 is run against: four of them fail when the fault is planted (it writes a limit value).
    # Their ids, and the message each fails with, look like paths and a user name: none may reach the results (#920).
    "tests/test_git_job_windows.py": (
        "import pathlib\n\nimport pytest\n\n"
        'SOURCE = pathlib.Path(__file__).resolve().parents[1] / "src" / "agenttalk" / "git_job.py"\n\n\n'
        '@pytest.mark.parametrize("n", ["private/report.txt", "/private", "C:\\\\Users\\\\Jane Doe\\\\x", "Janedoe"])\n'
        "def test_limits_hold(n):\n"
        '    assert "0x2000" not in SOURCE.read_text(encoding="utf-8"), n\n\n\n'
        "def test_limits_flag_the_file_when_asked(tmp_path):  # tmp_path: pytest makes its base-temp folder\n"
        "    import os\n    import subprocess\n\n"
        '    if os.environ.get("FLAG_THE_FILE"):  # a flag set while the fault is planted, as a person might\n'
        '        subprocess.run(["git", "-C", str(SOURCE.parents[2]), "update-index", "--skip-worktree", '
        '"src/agenttalk/git_job.py"], check=True)\n'
    ),
}
EDIT = "\n# an edit of mine that no one has committed yet\n"


def git(repo, *args):
    done = subprocess.run(
        [GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q")
    for name, text in FILES.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "base")
    return folder


def run_runner(shell, repo, tmp_path, *args, env=None):
    return subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-File", str(RUNNER), "-Repo", str(repo), "-Python", sys.executable,
         "-Out", str(tmp_path / "results.txt"), "-BaseTemp", str(tmp_path / "bt"), *args],
        capture_output=True, text=True, timeout=180, check=False, env=dict(os.environ, **(env or {})),
    )  # fmt: skip


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize(
    "dirty, only",
    [
        ("scripts/check_git_job_scheduled_task.ps1", "F34-scheduler-half-of-the-completion-rule-skipped"),
        ("tests/conftest.py", "F37-marker-ignored-so-real-lane-tests-run-without-a-job"),
        ("src/agenttalk/git_job.py", "F01-kill-on-close-set"),
        (
            "scripts/check_git_job_scheduled_task.ps1",
            "",
        ),  # every fault selected: the script is a target of some of them
    ],
    ids=["script", "test-config", "product-file", "all-faults"],
)
def test_the_runner_refuses_to_start_when_a_file_a_fault_changes_has_uncommitted_edits(
    tmp_path, repo, shell, dirty, only
):
    path = repo / dirty
    path.write_text(path.read_text(encoding="utf-8") + EDIT, encoding="utf-8", newline="\n")
    mine = path.read_bytes()
    done = run_runner(shell, repo, tmp_path, *(["-Only", only] if only else []))
    assert done.returncode != 0
    assert "Refusing to start" in done.stdout + done.stderr and "uncommitted" in done.stdout + done.stderr
    assert dirty in done.stdout + done.stderr  # it names the files that stand in the way
    assert path.read_bytes() == mine, "the runner changed or discarded an edit that was not its own"
    assert not (tmp_path / "results.txt").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_runner_refuses_to_start_when_git_cannot_be_read(tmp_path, shell):
    not_a_repo = tmp_path / "plain-folder"
    not_a_repo.mkdir()
    done = run_runner(shell, not_a_repo, tmp_path, "-Only", "F34-scheduler-half-of-the-completion-rule-skipped")
    text = done.stdout + done.stderr
    assert done.returncode != 0 and "Refusing to start" in text
    assert "git could not list" in text, "it must say that git could not be read, not fall back to another refusal"
    assert not (tmp_path / "results.txt").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_an_edit_in_a_file_that_no_selected_fault_changes_does_not_stop_the_runner(tmp_path, repo, shell):
    """The list is derived from the faults that are selected: F01 changes only the job module, so an uncommitted edit of
    the script does not stand in its way, the fault is planted and put back, and the edit is still there afterwards."""
    script = repo / "scripts" / "check_git_job_scheduled_task.ps1"
    script.write_text(script.read_text(encoding="utf-8") + EDIT, encoding="utf-8", newline="\n")
    mine = script.read_bytes()
    module = repo / "src" / "agenttalk" / "git_job.py"
    committed = module.read_bytes()
    done = run_runner(shell, repo, tmp_path, "-Only", "F01-kill-on-close-set")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "F01-kill-on-close-set" in (tmp_path / "results.txt").read_text(encoding="utf-8-sig")
    assert script.read_bytes() == mine and module.read_bytes().replace(b"\r\n", b"\n") == committed


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_every_failing_test_of_a_fault_is_counted_and_none_gets_a_row_of_its_own(tmp_path, repo, shell):
    """The results keep counts only (#920): a fault that fails four tests (F01 against the stand-in tests) says so in
    numbers, and no row per test is written."""
    done = run_runner(shell, repo, tmp_path, "-Only", "F01-kill-on-close-set")
    assert done.returncode == 0, done.stdout + done.stderr
    text = (tmp_path / "results.txt").read_text(encoding="utf-8-sig")
    expected = "F01-kill-on-close-set: CAUGHT -- ran 5, passed 1, failed 4, errors 0, skipped 0; failed by assertion 4,"
    assert expected in text, text
    assert [line for line in text.splitlines() if line.startswith(("F01", " "))] == [text.splitlines()[1]], text


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_results_file_keeps_no_pytest_text(tmp_path, repo, shell):
    """#920: the failing tests' ids and messages look like paths and a user name; none of it is written, and neither is
    pytest's own summary ("4 failed, 1 passed in 0.10s"; the runner's own words are "passed 1, failed 4")."""
    done = run_runner(shell, repo, tmp_path, "-Only", "F01-kill-on-close-set")
    assert done.returncode == 0, done.stdout + done.stderr
    text = (tmp_path / "results.txt").read_text(encoding="utf-8-sig")
    for word in ("private", "report.txt", "Jane", "Doe", "Janedoe", "test_limits_hold", "test_git_job_windows"):
        assert word not in text, (word, text)
    assert re.search(r"\d+ (passed|failed)", text) is None, text


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_relative_repo_out_and_basetemp_from_another_location_still_run_the_named_fault(tmp_path, repo, shell):
    """#920: PowerShell's location is the repository's parent folder, the process's folder is another one. Relative
    paths are read from PowerShell's location, before the runner changes folder; the fault is planted and its tests
    run."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    command = (f"Set-Location -LiteralPath '{repo.parent}'; & '{RUNNER}' -Repo '{repo.name}' -Python '{sys.executable}'"
               " -Out results.txt -BaseTemp bt -Only F01-kill-on-close-set; exit $LASTEXITCODE")
    done = subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-Command", command], cwd=elsewhere, capture_output=True, text=True, timeout=180,
        check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    text = (repo.parent / "results.txt").read_text(encoding="utf-8-sig")
    assert "F01-kill-on-close-set: CAUGHT -- ran 5," in text, text
    assert (repo.parent / "bt").is_dir() and not (repo / "bt").exists() and not (repo / "results.txt").exists()


# #920: a run in which no selected fault ran its tests is not a pass. (file changed and committed first, its new text,
# what the environment adds, and the fault's line)
NOTHING_RAN = [
    pytest.param("src/agenttalk/git_job.py", "class Native:\n    pass\n", {}, "NOT PLANTED", id="fault-text-not-found"),
    pytest.param("tests/test_git_job_windows.py", "# no tests here\n", {}, "NO TESTS RAN", id="no-tests-collected"),
    pytest.param(None, None, {"PYTEST_ADDOPTS": "--no-such-option"}, "NO RESULT", id="no-counts-to-read"),
]


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("target, content, env, verdict", NOTHING_RAN)
def test_a_run_in_which_no_selected_fault_ran_its_tests_exits_non_zero(
    tmp_path, repo, shell, target, content, env, verdict
):
    if target:
        (repo / target).write_text(content, encoding="utf-8", newline="\n")
        git(repo, "commit", "-q", "-am", "the change this case needs")
    done = run_runner(shell, repo, tmp_path, "-Only", "F01-kill-on-close-set", env=env)
    text = (tmp_path / "results.txt").read_text(encoding="utf-8-sig")
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"F01-kill-on-close-set: {verdict}" in text and "nothing was tested" in text, text


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("option", ["--assume-unchanged", "--skip-worktree"])
def test_the_runner_refuses_a_target_that_git_status_cannot_see(tmp_path, repo, shell, option):
    """`git status` does not show an edit in a file that is marked assume-unchanged or skip-worktree. With the first a
    checkout would discard the edit, with the second the planted fault would stay in the file while the runner said that
    everything was put back. The runner refuses to start, names the files and the way out, and touches nothing."""
    module = repo / "src" / "agenttalk" / "git_job.py"
    module.write_text(module.read_text(encoding="utf-8") + EDIT, encoding="utf-8", newline="\n")
    git(repo, "update-index", option, "src/agenttalk/git_job.py")
    assert git(repo, "status", "--porcelain").strip() == "", "the flag must hide the edit from git status"
    mine = module.read_bytes()
    done = run_runner(shell, repo, tmp_path, "-Only", "F01-kill-on-close-set")
    text = " ".join((done.stdout + done.stderr).replace("|", " ").split())
    assert done.returncode != 0 and "Refusing to start" in text and "assume-unchanged or skip-worktree" in text, text
    assert "src/agenttalk/git_job.py" in text and "no-assume-unchanged" in text, text
    assert module.read_bytes() == mine, "the runner changed or discarded an edit that was not its own"
    assert not (tmp_path / "results.txt").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_runner_writes_the_original_back_and_says_so_when_git_checkout_does_not_restore_a_file(
    tmp_path, repo, shell
):
    """While the fault is planted the stand-in test marks the file skip-worktree, so `git checkout` leaves the planted
    text in it. The runner checks the file against the original bytes it saved, finds it not restored, writes the
    original back, checks again, and says that it did. It never says that everything was put back without that check."""
    module = repo / "src" / "agenttalk" / "git_job.py"
    committed = module.read_bytes()
    done = run_runner(shell, repo, tmp_path, "-Only", "F01-kill-on-close-set", env={"FLAG_THE_FILE": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert module.read_bytes() == committed, "the planted fault stayed in the file"
    text = (tmp_path / "results.txt").read_text(encoding="utf-8-sig").replace("\\", "/")
    assert "git checkout did not restore src/agenttalk/git_job.py" in text, text
    assert "every source file was put back (checked byte for byte against the original)" in text, text
