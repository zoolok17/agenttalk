"""Cleanliness checks include new files even when Git hides them by default."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agenttalk import cli, close, janitor
from agenttalk.store import Store


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git is required for the real-repository cleanliness tests")
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        # Surface a sandbox refusal even if Git only prints a warning and exits zero.
        assert "permission denied" not in result.stderr.lower(), result.stderr
        return result.stdout.strip()

    git("init", "-q", "-b", "feature-clean-check")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "status.showUntrackedFiles", "no")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    git("add", "tracked.txt", ".gitignore")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "test baseline")
    return root, git


@pytest.mark.parametrize("state, clean", [("clean", True), ("tracked", False), ("untracked", False)])
def test_worktree_clean_with_untracked_files_hidden(repo, state, clean):
    root, _git = repo
    if state == "tracked":
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif state == "untracked":
        (root / "new-folder").mkdir()
        (root / "new-folder" / "untracked.txt").write_text("new\n", encoding="utf-8")

    assert cli._worktree_clean(root) is clean


def test_worktree_clean_without_git_is_unknown(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("Git is unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert cli._worktree_clean(tmp_path) is None


@pytest.mark.parametrize("action", ["open", "reopen"])
def test_close_records_untracked_files_as_dirty(repo, action):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = git("rev-parse", "HEAD")
    if action == "reopen":
        assert (
            cli.main(
                [
                    "--root",
                    str(root),
                    "close",
                    "open",
                    "--id",
                    "clean-check",
                    "--from",
                    "lead",
                    "--scope",
                    "release",
                    "--revision",
                    revision,
                    "--non-lane-isolation-not-asserted",
                ]
            )
            == 0
        )
        assert close.load_close(store, "clean-check")["revision_clean"] is True
    (root / "untracked.txt").write_text("uncommitted work\n", encoding="utf-8")

    args = ["--root", str(root), "close", action, "--id", "clean-check", "--from", "lead", "--revision", revision]
    if action == "open":
        args.extend(["--scope", "release", "--non-lane-isolation-not-asserted"])
    assert cli.main(args) == 0
    assert close.load_close(store, "clean-check")["revision_clean"] is False


def test_janitor_sees_untracked_files_when_git_hides_them(repo):
    root, _git = repo
    (root / "untracked.txt").write_text("work to preserve\n", encoding="utf-8")

    assert janitor.is_dirty_worktree(root) is True


def test_janitor_refuses_a_new_file_left_after_the_wip_commit(repo, monkeypatch):
    root, _git = repo
    (root / "before-commit.txt").write_text("preserve in WIP\n", encoding="utf-8")
    real_run = subprocess.run

    def leave_new_work(argv, *args, **kwargs):
        result = real_run(argv, *args, **kwargs)
        if "commit" in argv and result.returncode == 0:
            (root / "after-commit.txt").write_text("still needs preserving\n", encoding="utf-8")
        return result

    monkeypatch.setattr(subprocess, "run", leave_new_work)
    result = janitor.wip_commit_dirty_worktree(root, default_branches=["main", "master"])

    assert result.refused is True
    assert (root / "after-commit.txt").read_text(encoding="utf-8") == "still needs preserving\n"


def test_janitor_ignored_files_do_not_make_a_worktree_dirty(repo):
    root, _git = repo
    ignored = root / ".agenttalk" / "ignored.txt"
    ignored.parent.mkdir(exist_ok=True)
    ignored.write_text("ignored store data\n", encoding="utf-8")

    assert janitor.is_dirty_worktree(root) is False
    assert ignored.read_text(encoding="utf-8") == "ignored store data\n"
