"""Tests for janitor.py - the cross-platform scratch janitor (#148).

Builds synthetic trees per the issue's acceptance criteria AND the
reviewer-3 fix-round probes (P1-P6, MUT-A/B). Every git/filesystem
operation runs inside tmp_path - nothing here touches a real project or
the real OS temp root.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
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


def _backdate(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def _make_junction(link: Path, target: Path) -> bool:
    """Windows NTFS junction (no admin rights required, unlike a symlink).
    Returns False (skip the test) if junction creation itself fails."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


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

    # --- .worktrees/: every dir is a candidate, no family filter -------
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

    # --- tmp root: narrower, case-sensitive, dirs-only, age-windowed ---
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    old_matching_dir = tmp_root / "pytest-of-someone"
    old_matching_dir.mkdir()
    _backdate(old_matching_dir, 5)
    fresh_matching_dir = tmp_root / "pytest-of-inuse"
    fresh_matching_dir.mkdir()  # fresh mtime - inside the age window, must survive
    old_matching_file = tmp_root / "pytest-of-notadir.log"
    old_matching_file.write_text("log", encoding="utf-8")
    _backdate(old_matching_file, 5)
    wrong_case_dir = tmp_root / "PYTEST-OF-WRONGCASE"
    wrong_case_dir.mkdir()
    _backdate(wrong_case_dir, 5)
    foreign = tmp_root / "agenttalk-review-keepme"  # matches a family - MUT-B fix
    foreign.mkdir()
    _backdate(foreign, 5)
    (foreign / "important.txt").write_text("keep", encoding="utf-8")

    # --- scratch root: staleness from the newest mtime IN the tree -----
    scratch_root = tmp_path / "atk-scratch"
    stale = scratch_root / "dev-2" / "old-task"
    stale.mkdir(parents=True)
    _backdate(stale, 10)
    live_but_old_dir = scratch_root / "dev-2" / "live-task"
    live_nested = live_but_old_dir / "nested" / "deep"
    live_nested.mkdir(parents=True)
    _backdate(live_but_old_dir, 10)
    _backdate(live_but_old_dir / "nested", 10)
    (live_nested / "edited-now.txt").write_text("fresh", encoding="utf-8")
    fresh = scratch_root / "dev-2" / "new-task"
    fresh.mkdir(parents=True)

    cfg = janitor.JanitorConfig(
        repo=repo,
        scratch_root=scratch_root,
        keep_days=3,
        tmp_keep_days=1,
        tmp_root=tmp_root,
        repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES,
        tmp_families=["pytest-of-*", "agenttalk-review-*"],
        foreign=["agenttalk-review-keepme"],
        default_branches=["master", "main"],
    )
    return {
        "repo": repo, "cfg": cfg, "wt_dirty": wt_dirty,
        "wt_default_dirty": wt_default_dirty, "tmp_root": tmp_root,
        "scratch_root": scratch_root, "stale": stale,
        "live_but_old_dir": live_but_old_dir, "fresh": fresh,
        "old_matching_dir": old_matching_dir, "fresh_matching_dir": fresh_matching_dir,
        "old_matching_file": old_matching_file, "wrong_case_dir": wrong_case_dir,
        "foreign": foreign,
    }


# --------------------------------------------------------------------- report


def test_report_finds_repo_root_candidates(tree):
    candidates, errors = janitor.find_candidates(tree["cfg"])
    assert errors == []
    paths = {c.path for c in candidates}
    assert (tree["repo"] / ".review-abc123") in paths
    assert (tree["repo"] / ".review-notes.md") in paths
    assert (tree["repo"] / "docs") not in paths


def test_report_finds_worktrees_dir_candidates(tree):
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["wt_dirty"] in paths
    assert tree["wt_default_dirty"] in paths


def test_report_lists_dirty_worktrees(tree):
    report = janitor.build_report(tree["cfg"])
    dirty = set(report.dirty_worktrees)
    assert tree["wt_dirty"] in dirty
    assert tree["wt_default_dirty"] in dirty


def test_report_lists_foreign_kept(tree):
    report = janitor.build_report(tree["cfg"])
    assert tree["foreign"] in set(report.foreign_kept)


def test_report_mode_does_not_touch_anything(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.format_report(report, tree["cfg"], apply=False)
    assert (tree["repo"] / ".review-abc123").exists()
    assert tree["wt_dirty"].exists()
    assert tree["stale"].exists()


# ------------------------------------------------------------------ P4/MUT-B


def test_p4_temp_root_old_matching_dir_is_a_candidate(tree):
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["old_matching_dir"] in paths


def test_p4_temp_root_fresh_matching_dir_survives_age_window(tree):
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["fresh_matching_dir"] not in paths


def test_p4_temp_root_matching_file_is_never_a_candidate(tree):
    """Files are excluded from the temp root entirely - only directories."""
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["old_matching_file"] not in paths


def test_p4_temp_root_matching_is_case_sensitive(tree):
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["wrong_case_dir"] not in paths


def test_mutb_foreign_name_matches_a_real_family(tree):
    """The foreign fixture name must match a configured family - otherwise
    the exclusion is never exercised (reviewer-3 MUT-B: the old fixture's
    'reqforge' matched nothing, so this cell was vacuous)."""
    assert janitor._matches_any(tree["foreign"].name, tree["cfg"].tmp_families)


def test_apply_keeps_foreign_folder_that_matches_a_family(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert (tree["foreign"] / "important.txt").exists()


def test_apply_removes_matching_non_foreign_old_tmp_dir(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert not tree["old_matching_dir"].exists()
    assert tree["fresh_matching_dir"].exists()
    assert tree["old_matching_file"].exists()


# --------------------------------------------------------------------- P3


def test_p3_staleness_uses_newest_mtime_in_tree_not_dir_mtime(tree):
    """A task directory with an old top-level mtime but a file edited
    seconds ago three levels down must NOT be classified stale."""
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["live_but_old_dir"] not in paths
    assert tree["stale"] in paths


def test_apply_preserves_live_scratch_work_despite_old_dir_mtime(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert tree["live_but_old_dir"].exists()
    assert (tree["live_but_old_dir"] / "nested" / "deep" / "edited-now.txt").exists()
    assert not tree["stale"].exists()
    assert tree["fresh"].exists()


# --------------------------------------------------------------------- P1/P2


def test_p1_detached_worktree_dirty_is_reported(tree, tmp_path):
    # The main repo (fixture) is already on a detached HEAD - master itself
    # is held by wt_default_dirty, so a SECOND detached checkout at the same
    # commit is fine (only a non-detached branch checkout is exclusive).
    detached = tree["repo"] / ".worktrees" / "wt-detached"
    _git(tree["repo"], "worktree", "add", "-q", "--detach", str(detached), "master")
    (detached / "tracked.txt").write_text("detached-change\n", encoding="utf-8")
    report = janitor.build_report(tree["cfg"])
    assert detached in set(report.dirty_worktrees)


def test_p1_detached_worktree_dirty_is_refused_not_orphan_committed(tree):
    detached = tree["repo"] / ".worktrees" / "wt-detached"
    _git(tree["repo"], "worktree", "add", "-q", "--detach", str(detached), "master")
    (detached / "tracked.txt").write_text("detached-change\n", encoding="utf-8")
    result = janitor.wip_commit_dirty_worktree(detached, default_branches=["master", "main"])
    assert result.refused is True
    # No commit was made: the file is still modified, not committed.
    status = subprocess.run(["git", "-C", str(detached), "status", "--porcelain"],
                             capture_output=True, text=True, check=True).stdout
    assert "tracked.txt" in status


def test_p2_untracked_only_worktree_is_dirty(tree):
    wt_untracked = tree["repo"] / ".worktrees" / "wt-untracked"
    _git(tree["repo"], "worktree", "add", "-q", "-b", "feature-untracked",
         str(wt_untracked), "master")
    (wt_untracked / "new-file.txt").write_text("new", encoding="utf-8")
    assert janitor.is_dirty_worktree(wt_untracked) is True


def test_p2_apply_stages_and_commits_untracked_files(tree):
    wt_untracked = tree["repo"] / ".worktrees" / "wt-untracked"
    _git(tree["repo"], "worktree", "add", "-q", "-b", "feature-untracked",
         str(wt_untracked), "master")
    (wt_untracked / "new-file.txt").write_text("new", encoding="utf-8")
    result = janitor.wip_commit_dirty_worktree(wt_untracked, default_branches=["master", "main"])
    assert result.refused is False
    log = subprocess.run(["git", "-C", str(tree["repo"]), "show",
                           "feature-untracked:new-file.txt"],
                          capture_output=True, text=True, check=True).stdout
    assert log == "new"


# --------------------------------------------------------------------- P5


def test_p5_unlistable_directory_reported_as_failed_not_raised(tree, monkeypatch):
    real_safe_iterdir = janitor._safe_iterdir
    blocked = tree["repo"]

    def _blocked_iterdir(path):
        if path == blocked:
            return [], path
        return real_safe_iterdir(path)

    monkeypatch.setattr(janitor, "_safe_iterdir", _blocked_iterdir)
    candidates, errors = janitor.find_candidates(tree["cfg"])
    assert blocked in errors


def test_p5_build_report_never_raises_and_surfaces_access_errors(tree, monkeypatch):
    def _raise_iterdir(self):
        raise PermissionError("simulated access denied")

    monkeypatch.setattr(Path, "iterdir", _raise_iterdir)
    report = janitor.build_report(tree["cfg"])  # must not raise
    assert report.access_errors  # at least the repo root failed to list
    text = janitor.format_report(report, tree["cfg"], apply=False)
    assert "FAILED to list" in text


# --------------------------------------------------------------------- MUT-A


def test_apply_removes_only_allow_listed_paths(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert not (tree["repo"] / ".review-abc123").exists()
    assert not (tree["repo"] / ".review-notes.md").exists()
    assert (tree["repo"] / "docs" / "keep.md").exists()
    assert (tree["repo"] / "tracked.txt").exists()


def test_apply_refuses_default_branch_dirty_worktree(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    log = subprocess.run(
        ["git", "-C", str(tree["repo"]), "log", "--oneline", "master"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "wip: preserve" not in log
    assert tree["wt_default_dirty"].exists()
    assert (tree["wt_default_dirty"] / "tracked.txt").read_text(encoding="utf-8") == "three\n"


def test_apply_prunes_worktree_registrations(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    remaining = janitor.get_registered_worktrees(tree["repo"])
    assert tree["wt_dirty"] not in remaining
    assert tree["wt_default_dirty"] in remaining


def test_apply_is_idempotent(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    report2 = janitor.build_report(tree["cfg"])
    second = janitor.apply(tree["cfg"], report2)
    assert "FAILED" not in second


# --------------------------------------------------------------- simulated permission failure


def test_simulated_permission_failure_reported_as_failed_not_skipped(tree, monkeypatch):
    def _always_fail(path):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    monkeypatch.setattr(janitor.platform, "system", lambda: "Linux")
    target = tree["repo"] / ".review-abc123"
    assert target.exists()
    result = janitor.remove_stubborn(target)
    assert result == "FAILED"
    assert target.exists()


def test_simulated_permission_failure_surfaces_in_apply_report(tree, monkeypatch):
    def _always_fail(path):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    monkeypatch.setattr(janitor.platform, "system", lambda: "Linux")
    report = janitor.build_report(tree["cfg"])
    text = janitor.apply(tree["cfg"], report)
    assert "FAILED" in text
    assert str(tree["repo"] / ".review-abc123") in text


# ------------------------------------------------------------------- P6A/P6B


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_p6a_junction_candidate_removes_the_link_not_the_target(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    (real_target / "precious.txt").write_text("do not delete", encoding="utf-8")
    link = repo / ".review-link"
    if not _make_junction(link, real_target):
        pytest.skip("could not create a junction in this environment")

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert (real_target / "precious.txt").exists()
    assert not link.exists()  # the link itself IS removed


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_p6b_failed_junction_removal_never_escalates_into_the_target(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    (real_target / "precious.txt").write_text("do not delete", encoding="utf-8")
    link = repo / ".review-link"
    if not _make_junction(link, real_target):
        pytest.skip("could not create a junction in this environment")

    calls = []
    real_run = subprocess.run

    def _spy_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    def _always_fail_rmtree(path):
        raise OSError("simulated stubborn link")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail_rmtree)
    monkeypatch.setattr(janitor.subprocess, "run", _spy_run)
    result = janitor.remove_stubborn(link)
    assert result == "FAILED"
    assert (real_target / "precious.txt").exists()
    # No escalation tool (takeown/icacls/robocopy) was ever invoked for a link.
    escalation_tools = {"takeown", "icacls", "robocopy"}
    for call in calls:
        exe_name = Path(call[0]).stem.lower()
        assert exe_name not in escalation_tools
