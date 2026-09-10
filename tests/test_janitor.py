"""Tests for janitor.py - the cross-platform scratch janitor (#148).

Builds a synthetic tree per the issue's acceptance criteria: scratch
families in the repo root, the temp root, and .worktrees/, one dirty
registered worktree (on a non-default branch), one dirty worktree ON the
default branch, and one foreign folder under the temp root. Every git
operation runs inside tmp_path - nothing here touches a real project.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agenttalk import janitor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                    capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    # --- repo-root scratch families -------------------------------------
    (repo / ".review-abc123").mkdir()
    (repo / ".review-abc123" / "x.txt").write_text("x", encoding="utf-8")
    (repo / ".review-notes.md").write_text("notes", encoding="utf-8")
    # Never removed regardless of family match.
    (repo / "docs").mkdir()
    (repo / "docs" / "keep.md").write_text("keep", encoding="utf-8")

    # --- .worktrees/ -------------------------------------------------------
    worktrees_dir = repo / ".worktrees"
    worktrees_dir.mkdir()

    wt_dirty = worktrees_dir / "wt-feature"
    _git(repo, "worktree", "add", "-q", "-b", "feature-x", str(wt_dirty), "master")
    (wt_dirty / "tracked.txt").write_text("two\n", encoding="utf-8")

    # A worktree literally ON the default branch: git refuses to have the
    # same branch checked out twice, so detach the MAIN repo's own HEAD
    # first, freeing "master" for the worktree to hold directly (not
    # detached) - genuinely reproduces "a worktree on the default branch".
    _git(repo, "checkout", "-q", "--detach", "master")
    wt_default_dirty = worktrees_dir / "wt-default"
    _git(repo, "worktree", "add", "-q", str(wt_default_dirty), "master")
    (wt_default_dirty / "tracked.txt").write_text("three\n", encoding="utf-8")

    # --- tmp root ------------------------------------------------------
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    (tmp_root / "pytest-abc").mkdir()
    (tmp_root / "review-xyz").mkdir()
    (tmp_root / "reqforge").mkdir()  # foreign: kept, never a candidate
    (tmp_root / "reqforge" / "important.txt").write_text("keep", encoding="utf-8")

    # --- scratch root: one stale, one fresh -----------------------------
    scratch_root = tmp_path / "atk-scratch"
    stale = scratch_root / "dev-2" / "old-task"
    fresh = scratch_root / "dev-2" / "new-task"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    import os
    import time
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))

    cfg = janitor.JanitorConfig(
        repo=repo,
        scratch_root=scratch_root,
        keep_days=3,
        tmp_root=tmp_root,
        repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES,
        tmp_families=janitor.DEFAULT_TMP_FAMILIES,
        foreign=["reqforge"],
        default_branches=["master", "main"],
    )
    return {
        "repo": repo, "cfg": cfg, "wt_dirty": wt_dirty,
        "wt_default_dirty": wt_default_dirty, "tmp_root": tmp_root,
        "scratch_root": scratch_root, "stale": stale, "fresh": fresh,
    }


def test_report_finds_repo_root_candidates(tree):
    report = janitor.build_report(tree["cfg"])
    paths = {c.path for c in report.candidates}
    assert (tree["repo"] / ".review-abc123").resolve() in paths
    assert (tree["repo"] / ".review-notes.md").resolve() in paths
    assert (tree["repo"] / "docs").resolve() not in paths


def test_report_finds_worktrees_dir_candidates(tree):
    report = janitor.build_report(tree["cfg"])
    paths = {c.path for c in report.candidates}
    assert tree["wt_dirty"].resolve() in paths
    assert tree["wt_default_dirty"].resolve() in paths


def test_report_finds_tmp_family_candidates_and_excludes_foreign(tree):
    report = janitor.build_report(tree["cfg"])
    paths = {c.path for c in report.candidates}
    assert (tree["tmp_root"] / "pytest-abc").resolve() in paths
    assert (tree["tmp_root"] / "review-xyz").resolve() in paths
    assert (tree["tmp_root"] / "reqforge").resolve() not in paths


def test_report_finds_only_stale_scratch_dir(tree):
    report = janitor.build_report(tree["cfg"])
    paths = {c.path for c in report.candidates}
    assert tree["stale"].resolve() in paths
    assert tree["fresh"].resolve() not in paths


def test_report_lists_dirty_worktrees(tree):
    report = janitor.build_report(tree["cfg"])
    dirty = {p.resolve() for p in report.dirty_worktrees}
    assert tree["wt_dirty"].resolve() in dirty
    assert tree["wt_default_dirty"].resolve() in dirty


def test_report_lists_foreign_kept(tree):
    report = janitor.build_report(tree["cfg"])
    kept = {p.resolve() for p in report.foreign_kept}
    assert (tree["tmp_root"] / "reqforge").resolve() in kept


def test_report_mode_does_not_touch_anything(tree):
    janitor.build_report(tree["cfg"])
    janitor.format_report(janitor.build_report(tree["cfg"]), tree["cfg"], apply=False)
    assert (tree["repo"] / ".review-abc123").exists()
    assert tree["wt_dirty"].exists()
    assert tree["stale"].exists()


def test_apply_preserves_dirty_non_default_worktree_as_wip_commit(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    log = subprocess.run(
        ["git", "-C", str(tree["repo"]), "log", "--oneline", "feature-x"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "wip: preserve uncommitted worktree state" in log


def test_apply_refuses_default_branch_dirty_worktree(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    # Never auto-committed on master.
    log = subprocess.run(
        ["git", "-C", str(tree["repo"]), "log", "--oneline", "master"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "wip: preserve" not in log
    # Never removed either - the uncommitted change must survive.
    assert tree["wt_default_dirty"].exists()
    assert (tree["wt_default_dirty"] / "tracked.txt").read_text(encoding="utf-8") == "three\n"


def test_apply_removes_only_allow_listed_paths(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert not (tree["repo"] / ".review-abc123").exists()
    assert not (tree["repo"] / ".review-notes.md").exists()
    assert not (tree["tmp_root"] / "pytest-abc").exists()
    assert not (tree["tmp_root"] / "review-xyz").exists()
    assert not tree["stale"].exists()
    # Never touched: tracked files, docs/, the fresh scratch dir.
    assert (tree["repo"] / "docs" / "keep.md").exists()
    assert (tree["repo"] / "tracked.txt").exists()
    assert tree["fresh"].exists()


def test_apply_keeps_foreign_folder(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert (tree["tmp_root"] / "reqforge" / "important.txt").exists()


def test_apply_prunes_worktree_registrations(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    remaining = janitor.get_registered_worktrees(tree["repo"])
    # wt_dirty's directory was removed after its WIP commit -> pruned away.
    assert tree["wt_dirty"].resolve() not in remaining
    # wt_default_dirty was refused (never removed) -> still registered.
    assert tree["wt_default_dirty"].resolve() in remaining


def test_apply_is_idempotent(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    report2 = janitor.build_report(tree["cfg"])
    second = janitor.apply(tree["cfg"], report2)
    assert "FAILED" not in second


def test_simulated_permission_failure_reported_as_failed_not_skipped(tree, monkeypatch):
    def _always_fail(path):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    # No escalation path outside Windows: remove_stubborn returns FAILED
    # directly once the plain remove fails and platform.system() != 'Windows'.
    monkeypatch.setattr(janitor.platform, "system", lambda: "Linux")
    target = tree["repo"] / ".review-abc123"
    assert target.exists()
    result = janitor.remove_stubborn(target)
    assert result == "FAILED"
    assert target.exists()  # not silently removed either


def test_simulated_permission_failure_surfaces_in_apply_report(tree, monkeypatch):
    def _always_fail(path):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    monkeypatch.setattr(janitor.platform, "system", lambda: "Linux")
    report = janitor.build_report(tree["cfg"])
    text = janitor.apply(tree["cfg"], report)
    assert "FAILED" in text
    assert str((tree["repo"] / ".review-abc123").resolve()) in text
