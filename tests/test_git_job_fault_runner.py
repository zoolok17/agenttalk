"""#907: the planted-fault runner (`docs/verification/907/faults.ps1`) must never discard an edit it did not make.

The runner changes tracked files in place and puts them back with `git checkout`. A file with uncommitted edits would
lose them, so it must refuse to start when ANY file that a selected fault changes is dirty (the list is derived from the
faults themselves), and when `git status` cannot be read at all. These tests run it in a throw-away git repository with
stand-in files, on every PowerShell that is installed; the faults are never planted except in the one control run.
"""

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

# The four files the faults change; the job module holds the text that fault F01 looks for (so a run can plant it).
FILES = {
    "src/agenttalk/cli.py": "def cli():\n    return 1\n",
    "src/agenttalk/git_job.py": (
        "class Native:\n    def create_job(self):\n"
        "        handle = self._kernel.CreateJobObjectW(None, None)\n"
        "        return int(handle) if handle else None\n"
    ),
    "scripts/check_git_job_scheduled_task.ps1": "# the scheduled-task script\n",
    "tests/conftest.py": "# the test configuration\n",
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


def run_runner(shell, repo, tmp_path, *args):
    return subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-File", str(RUNNER), "-Repo", str(repo), "-Python", sys.executable,
         "-Out", str(tmp_path / "results.txt"), "-BaseTemp", str(tmp_path / "bt"), *args],
        capture_output=True, text=True, timeout=180, check=False,
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
    assert done.returncode != 0 and "Refusing to start" in done.stdout + done.stderr
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
