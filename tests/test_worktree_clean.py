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


@pytest.mark.parametrize(
    "state, clean",
    [("clean", True), ("tracked", False), ("untracked-top", False), ("untracked-directory", False), ("ignored", True)],
)
def test_worktree_clean_with_untracked_files_hidden(repo, state, clean):
    root, _git = repo
    if state == "tracked":
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif state in {"untracked-top", "untracked-directory", "ignored"}:
        relative = {
            "untracked-top": "untracked.txt",
            "untracked-directory": "new-folder/untracked.txt",
            "ignored": ".agenttalk/ignored.txt",
        }[state]
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("new\n", encoding="utf-8")

    assert cli._worktree_clean(root) is clean


def test_worktree_clean_without_git_is_unknown(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("Git is unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert cli._worktree_clean(tmp_path) is None


@pytest.mark.parametrize("action", ["open", "reopen"])
@pytest.mark.parametrize("relative", ["untracked.txt", "new-folder/untracked.txt"])
def test_close_records_untracked_files_as_dirty(repo, action, relative):
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
    untracked = root / relative
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("uncommitted work\n", encoding="utf-8")

    args = ["--root", str(root), "close", action, "--id", "clean-check", "--from", "lead", "--revision", revision]
    if action == "open":
        args.extend(["--scope", "release", "--non-lane-isolation-not-asserted"])
    assert cli.main(args) == 0
    assert close.load_close(store, "clean-check")["revision_clean"] is False


@pytest.mark.parametrize("relative", ["untracked.txt", "new-folder/untracked.txt"])
def test_janitor_sees_untracked_files_when_git_hides_them(repo, relative):
    root, _git = repo
    untracked = root / relative
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("work to preserve\n", encoding="utf-8")

    assert janitor.is_dirty_worktree(root) is True


@pytest.mark.parametrize(
    "relative, ignored",
    [("after-commit.txt", False), ("new-folder/after-commit.txt", False), (".agenttalk/ignored.txt", True)],
)
def test_janitor_checks_new_files_left_after_the_wip_commit(repo, monkeypatch, relative, ignored):
    root, _git = repo
    (root / "before-commit.txt").write_text("preserve in WIP\n", encoding="utf-8")
    real_run = subprocess.run

    def leave_new_work(argv, *args, **kwargs):
        result = real_run(argv, *args, **kwargs)
        if "commit" in argv and result.returncode == 0:
            new_work = root / relative
            new_work.parent.mkdir(parents=True, exist_ok=True)
            new_work.write_text("still needs preserving\n", encoding="utf-8")
        return result

    monkeypatch.setattr(subprocess, "run", leave_new_work)
    result = janitor.wip_commit_dirty_worktree(root, default_branches=["main", "master"])

    assert result.refused is not ignored
    assert (root / relative).read_text(encoding="utf-8") == "still needs preserving\n"


def test_janitor_ignored_files_do_not_make_a_worktree_dirty(repo):
    root, _git = repo
    ignored = root / ".agenttalk" / "ignored.txt"
    ignored.parent.mkdir(exist_ok=True)
    ignored.write_text("ignored store data\n", encoding="utf-8")

    assert janitor.is_dirty_worktree(root) is False
    assert ignored.read_text(encoding="utf-8") == "ignored store data\n"


@pytest.mark.parametrize("site", ["cli", "janitor", "post-commit"])
def test_status_mode_normal_for_yes_no_checks(tmp_path, monkeypatch, site):
    calls = []
    (tmp_path / ".git").touch()

    def read(root, argv):
        calls.append(argv)
        return 0, ""

    def checked(root, *argv):
        calls.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(janitor, "_run_git_checked", checked)
    if site == "cli":
        assert cli._worktree_clean(tmp_path) is True
    elif site == "janitor":
        assert janitor.is_dirty_worktree(tmp_path) is False
    else:
        monkeypatch.setattr(janitor, "worktree_branch", lambda path: "feature")
        monkeypatch.setattr(janitor.shutil, "which", lambda name: "git")
        monkeypatch.setattr(
            janitor.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        )
        assert janitor.wip_commit_dirty_worktree(tmp_path, default_branches=["main"]).refused is False
    assert calls == [["status", "--porcelain", "--untracked-files=normal"]]
