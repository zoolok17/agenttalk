"""Tests for janitor.py - the cross-platform scratch janitor (#148).

Builds synthetic trees per the issue's acceptance criteria AND the
reviewer-3 fix-round probes (P1-P6, MUT-A/B). Every git/filesystem
operation runs inside tmp_path - nothing here touches a real project or
the real OS temp root.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agenttalk import janitor


def _directory_link(link, target):
    if platform.system() == "Windows":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("replacement", ["junction", "plain"])
def test_399_parent_above_root_changed_after_scan(tmp_path, replacement):
    parent = tmp_path / "parent"
    t = _stale_scratch_task(parent)
    report = janitor.build_report(t["cfg"])
    assert t["task"] in [c.path for c in report.candidates]
    original = tmp_path / "original"
    parent.rename(original)
    replacement_parent = tmp_path / "replacement" if replacement == "junction" else parent
    victim = replacement_parent / "atk-scratch" / "seat" / "task"
    victim.mkdir(parents=True)
    sentinel = victim / "sentinel.bin"
    sentinel.write_bytes(t["data"])
    if replacement == "junction":
        _directory_link(parent, replacement_parent)
    try:
        text = janitor.apply(t["cfg"], report)
        assert sentinel.exists(), "the replacement account's sentinel was deleted"
        assert sentinel.read_bytes() == t["data"]
        assert (original / "atk-scratch" / "seat" / "task" / "old.txt").exists()
        assert "FAILED: 1" in text
        assert "[scratch]" in text and "ancestor" in text
    finally:
        if replacement == "junction":
            if platform.system() == "Windows":
                parent.rmdir()
            else:
                parent.unlink()


@pytest.mark.parametrize("configured_root", ["repo", "scratch", "tmp"])
def test_399_linked_ancestor_at_scan_is_refused(tmp_path, configured_root):
    real = tmp_path / "real"
    t = _stale_scratch_task(real)
    alias = tmp_path / "alias"
    _directory_link(alias, real)
    repo = t["cfg"].repo
    config = repo / ".agenttalk" / "config.json"
    config.parent.mkdir()
    scratch = alias / "atk-scratch" if configured_root == "scratch" else real / "atk-scratch"
    temp = alias / "tmp" if configured_root == "tmp" else real / "tmp"
    config.write_text(json.dumps({"scratch": {"root": str(scratch), "tmp_root": str(temp)}}), encoding="utf-8")
    try:
        cfg = janitor.JanitorConfig.load(alias / "repo" if configured_root == "repo" else repo)
        report = janitor.build_report(cfg)
        text = janitor.apply(cfg, report)
        assert (t["task"] / "old.txt").exists(), "a pre-existing ancestor link was followed"
        assert "FAILED" in text and "ancestor" in text and "link" in text
        assert not report.candidates, "discovery must not enter a linked ancestor"
    finally:
        if platform.system() == "Windows":
            alias.rmdir()
        else:
            alias.unlink()


@pytest.mark.parametrize("root_source", ["flag", "environment", "working-directory"])
def test_399_cli_keeps_ancestor_links_visible(tmp_path, monkeypatch, capsys, root_source):
    from agenttalk.cli import main

    t = _stale_scratch_task(tmp_path / "real")
    alias = tmp_path / "alias"
    _directory_link(alias, tmp_path / "real")
    config = t["cfg"].repo / ".agenttalk" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"scratch": {"tmp_root": str(t["cfg"].tmp_root)}}), encoding="utf-8")
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    args = ["janitor", "--apply"]
    if root_source == "flag":
        args = ["--root", str(alias / "repo"), *args]
    elif root_source == "environment":
        monkeypatch.setenv("AGENTTALK_ROOT", str(alias / "repo"))
    else:
        # Some operating systems return the physical path from getcwd; exercise
        # the logical path too, as supplied by Windows before normalization.
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: alias / "repo"))
    try:
        main(args)
        assert (t["task"] / "old.txt").exists()
        output = capsys.readouterr().out
        assert "FAILED" in output and "ancestor" in output
    finally:
        if platform.system() == "Windows":
            alias.rmdir()
        else:
            alias.unlink()


@pytest.mark.parametrize("when", ["scan", "apply"])
def test_399_unreadable_ancestor_keeps_files(tmp_path, monkeypatch, when):
    t = _stale_scratch_task(tmp_path / "parent")
    if when == "apply":
        report = janitor.build_report(t["cfg"])
    _denied_lstat_for(tmp_path / "parent", monkeypatch, start_denied=True)
    if when == "scan":
        report = janitor.build_report(t["cfg"])
    text = janitor.apply(t["cfg"], report)
    assert (t["task"] / "old.txt").exists()
    assert "FAILED" in text and "ancestor" in text and "simulated metadata denied" in text


def test_399_higher_ancestor_changed_but_inner_folders_unchanged(tmp_path):
    parent = tmp_path / "parent"
    t = _stale_scratch_task(parent / "inner")
    report = janitor.build_report(t["cfg"])
    parent.rename(tmp_path / "original")
    parent.mkdir()
    (tmp_path / "original" / "inner").rename(parent / "inner")

    text = janitor.apply(t["cfg"], report)

    assert (t["task"] / "old.txt").exists()
    assert "FAILED: 1" in text and "changed identity" in text


def test_399_drive_root_identity_is_rechecked(tmp_path, monkeypatch):
    t = _stale_scratch_task(tmp_path)
    report = janitor.build_report(t["cfg"])
    drive_root = Path(tmp_path.anchor)
    real_lstat = os.lstat

    def replaced_drive_identity(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if Path(path) == drive_root:
            fields = list(st)
            fields[1] = st.st_ino + 1
            extra = {"st_file_attributes": st.st_file_attributes} if hasattr(st, "st_file_attributes") else {}
            return os.stat_result(fields, extra)
        return st

    monkeypatch.setattr(os, "lstat", replaced_drive_identity)
    text = janitor.apply(t["cfg"], report)
    assert (t["task"] / "old.txt").exists()
    assert "FAILED: 1" in text and "changed identity" in text


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                    capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # Repo-local, before the first commit: a modern git can start detached
    # background maintenance on commit, which creates and deletes
    # `.git/objects/maintenance.lock` - a file this fixture's own
    # `task_dir.rglob("*")` + `_backdate` pattern can list and then fail to
    # stamp (already gone), or whose creation/deletion can itself refresh
    # mtimes inside a tree these tests are deliberately backdating to look
    # stale. Disabled so every commit in this file is fully synchronous.
    _git(repo, "config", "maintenance.auto", "false")
    _git(repo, "config", "gc.auto", "0")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")


def _backdate(path: Path, days: float, *, follow_symlinks: bool = True) -> None:
    stamp = time.time() - days * 86400
    if follow_symlinks or platform.system() != "Windows":
        os.utime(path, (stamp, stamp), follow_symlinks=follow_symlinks)
        return
    # os.utime's follow_symlinks=False is unimplemented on this platform
    # (NotImplementedError), and the default (follow_symlinks=True) sets
    # a Windows junction's TARGET mtime, not the link's own - confirmed
    # empirically. The reliable way to set a reparse point's OWN mtime is
    # the Win32 API directly: open it WITHOUT following
    # (FILE_FLAG_OPEN_REPARSE_POINT) and set its time via SetFileTime.
    _win32_set_reparse_point_mtime(path, stamp)


def _win32_set_reparse_point_mtime(path: Path, unix_stamp: float) -> None:
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    EPOCH_AS_FILETIME = 116444736000000000  # 1601-01-01 -> 1970-01-01, in 100ns units
    HUNDREDS_OF_NANOSECONDS = 10000000

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle in (0, -1):
        raise OSError(f"CreateFileW failed for {path}: {ctypes.get_last_error()}")
    try:
        ft_value = int(unix_stamp * HUNDREDS_OF_NANOSECONDS) + EPOCH_AS_FILETIME
        filetime = _FILETIME(ft_value & 0xFFFFFFFF, ft_value >> 32)
        if not ctypes.windll.kernel32.SetFileTime(handle, None, None, ctypes.byref(filetime)):
            raise OSError(f"SetFileTime failed for {path}: {ctypes.get_last_error()}")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _backdate_tree(root: Path, days: float) -> None:
    """Backdate every entry in `root`'s tree, then `root` itself. Skips a
    path that no longer exists by the time its turn comes: git's own
    background maintenance (started detached on a commit, on a modern
    git) creates and deletes files like
    `.git/objects/maintenance.lock` - one can be listed by `rglob` and
    gone by the time this loop reaches it, which must not fail the
    fixture (`maintenance.auto`/`gc.auto` are disabled in `_init_repo`
    for the same reason; this is the defense-in-depth half of that
    fix)."""
    for p in sorted(root.rglob("*"), reverse=True):
        try:
            _backdate(p, days)
        except FileNotFoundError:
            continue
    _backdate(root, days)


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
    old_matching_file = tmp_root / "surefire-report.txt"
    old_matching_file.write_text("report", encoding="utf-8")
    _backdate(old_matching_file, 5)
    fresh_matching_file = tmp_root / "surefire-inuse.txt"
    fresh_matching_file.write_text("in progress", encoding="utf-8")  # fresh - must survive
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
        tmp_families=["pytest-of-*", "agenttalk-review-*", "surefire-*"],
        foreign=["agenttalk-review-keepme"],
        default_branches=["master", "main"],
    )
    return {
        "repo": repo, "cfg": cfg, "wt_dirty": wt_dirty,
        "wt_default_dirty": wt_default_dirty, "tmp_root": tmp_root,
        "scratch_root": scratch_root, "stale": stale,
        "live_but_old_dir": live_but_old_dir, "fresh": fresh,
        "old_matching_dir": old_matching_dir, "fresh_matching_dir": fresh_matching_dir,
        "old_matching_file": old_matching_file, "fresh_matching_file": fresh_matching_file,
        "wrong_case_dir": wrong_case_dir, "foreign": foreign,
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


def test_p4_temp_root_old_matching_file_is_a_candidate(tree):
    """Files ARE eligible temp-root candidates (some real families, like a
    Mockito boot log or a surefire report, are files, not directories) -
    gated by the same age window as directories, never excluded by type."""
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["old_matching_file"] in paths


def test_p4_temp_root_fresh_matching_file_survives_age_window(tree):
    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert tree["fresh_matching_file"] not in paths


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


def test_apply_removes_matching_non_foreign_old_tmp_entries(tree):
    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert not tree["old_matching_dir"].exists()
    assert not tree["old_matching_file"].exists()
    assert tree["fresh_matching_dir"].exists()
    assert tree["fresh_matching_file"].exists()


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
            return [], (path, "simulated access denied")
        return real_safe_iterdir(path)

    monkeypatch.setattr(janitor, "_safe_iterdir", _blocked_iterdir)
    candidates, errors = janitor.find_candidates(tree["cfg"])
    assert blocked in [p for p, _reason in errors]


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
    target = tree["repo"] / ".review-abc123"
    assert target.exists()
    result, reason = janitor.remove_plainly(target)
    assert result == "FAILED"
    assert reason == "simulated permission denied"
    assert target.exists()


def test_simulated_permission_failure_surfaces_in_apply_report(tree, monkeypatch):
    def _always_fail(path):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    report = janitor.build_report(tree["cfg"])
    text = janitor.apply(tree["cfg"], report)
    assert "FAILED" in text
    # Kept and named relative to its scanned root, with the reason (#342).
    assert "  [repo] .review-abc123: simulated permission denied" in text.splitlines()
    assert (tree["repo"] / ".review-abc123").exists()


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
    result, _reason = janitor.remove_plainly(link)
    assert result == "FAILED"
    assert (real_target / "precious.txt").exists()
    # No escalation tool (takeown/icacls/robocopy) was ever invoked for a link.
    escalation_tools = {"takeown", "icacls", "robocopy"}
    for call in calls:
        exe_name = Path(call[0]).stem.lower()
        assert exe_name not in escalation_tools


# ---------------- unchecked commit failure / nested temp staleness / links ---


def test_r1_refusing_precommit_hook_refuses_not_silently_loses_work(tmp_path):
    """A pre-commit hook that exits non-zero must refuse the WIP commit,
    not silently report success while the change is lost."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    wt = repo / ".worktrees" / "wt-hook"
    _git(repo, "worktree", "add", "-q", "-b", "feature-hook", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    result = janitor.wip_commit_dirty_worktree(wt, default_branches=["master", "main"])
    assert result.refused is True
    assert "REFUSED" in result.message
    assert (wt / "precious.txt").exists()
    assert (wt / "precious.txt").read_text(encoding="utf-8") == "do not lose me"


def test_r1_refusing_precommit_hook_apply_preserves_the_worktree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    wt = repo / ".worktrees" / "wt-hook"
    _git(repo, "worktree", "add", "-q", "-b", "feature-hook", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert wt in set(report.dirty_worktrees)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()
    assert (wt / "precious.txt").read_text(encoding="utf-8") == "do not lose me"


def test_r2_temp_root_directory_aged_by_newest_nested_file_not_own_mtime(tree):
    """The P3 flaw, recurring in the shared temp root: a directory whose
    OWN mtime is old but whose nested file was written moments ago must
    not be classified stale."""
    live_old_dir = tree["tmp_root"] / "pytest-of-liveold"
    nested = live_old_dir / "case0"
    nested.mkdir(parents=True)
    _backdate(live_old_dir, 5)
    _backdate(nested, 5)
    (nested / "live.txt").write_text("fresh", encoding="utf-8")

    candidates, _ = janitor.find_candidates(tree["cfg"])
    paths = {c.path for c in candidates}
    assert live_old_dir not in paths


def test_r2_apply_preserves_live_nested_file_in_old_tmp_directory(tree):
    live_old_dir = tree["tmp_root"] / "pytest-of-liveold2"
    nested = live_old_dir / "case0"
    nested.mkdir(parents=True)
    _backdate(live_old_dir, 5)
    _backdate(nested, 5)
    (nested / "live.txt").write_text("fresh", encoding="utf-8")

    report = janitor.build_report(tree["cfg"])
    janitor.apply(tree["cfg"], report)
    assert (nested / "live.txt").exists()


def test_r3_posix_symlink_directory_removes_link_not_target(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_target_dir = tmp_path / "real-target-dir"
    real_target_dir.mkdir()
    (real_target_dir / "precious.txt").write_text("do not delete", encoding="utf-8")
    link_dir = repo / ".review-symlink"
    try:
        os.symlink(real_target_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege/support not available in this environment")

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert (real_target_dir / "precious.txt").exists()
    assert not os.path.lexists(link_dir)


def test_r3_posix_symlink_file_removes_link_not_target(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_target_file = tmp_path / "real-target-file.txt"
    real_target_file.write_text("do not delete", encoding="utf-8")
    link_file = repo / ".review-symlink.md"
    try:
        os.symlink(real_target_file, link_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege/support not available in this environment")

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert real_target_file.exists()
    assert not os.path.lexists(link_file)


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_n1_old_link_above_candidate_is_itself_the_candidate_not_walked_through(tmp_path):
    """scratch_root/<agent> as a junction, older than keep_days: the agent
    entry itself must be treated as a candidate (never entered) -
    matching the repo-root pass's handling of a link, not silently
    walked through to whatever is behind it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    scratch_root.mkdir()
    real_agent_dir = tmp_path / "real-agent-dir"
    task_dir = real_agent_dir / "old-task"
    task_dir.mkdir(parents=True)
    _backdate(task_dir, 10)
    (task_dir / "keep.txt").write_text("behind the link", encoding="utf-8")
    agent_link = scratch_root / "dev-2"
    if not _make_junction(agent_link, real_agent_dir):
        pytest.skip("could not create a junction in this environment")
    _backdate(agent_link, 10, follow_symlinks=False)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, _ = janitor.find_candidates(cfg)
    paths = {c.path for c in candidates}
    assert agent_link in paths  # the link itself is the candidate
    assert task_dir not in paths  # never walked through it

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert not os.path.lexists(agent_link)  # the link is gone
    assert task_dir.exists()  # the target behind it was never touched
    assert (task_dir / "keep.txt").exists()


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_n1_fresh_link_above_candidate_survives_the_age_window(tmp_path):
    """The same link-as-candidate handling above must still respect
    keep_days: a junction created moments ago (an operator's scratch
    relocation, or a seat's live task link) is not yet a candidate just
    because it is link-like."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    scratch_root.mkdir()
    real_agent_dir = tmp_path / "real-agent-dir-fresh"
    real_agent_dir.mkdir()
    agent_link = scratch_root / "dev-3"
    if not _make_junction(agent_link, real_agent_dir):
        pytest.skip("could not create a junction in this environment")
    # Deliberately NOT backdated - this link is fresh.

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, _ = janitor.find_candidates(cfg)
    paths = {c.path for c in candidates}
    assert agent_link not in paths

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert os.path.lexists(agent_link)  # the fresh link survives


# ------------------------------- fail-closed discovery / dangling-link lexists


def test_r1_git_unresolvable_fails_closed_under_worktrees(tmp_path, monkeypatch):
    """git missing must not be read as "zero worktrees registered" - a
    dirty, unrecognized worktree under .worktrees/ would otherwise be
    removed as a plain candidate, with no WIP-commit-or-refuse gate ever
    applied to it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".worktrees" / "wt-orphan"
    _git(repo, "worktree", "add", "-q", "-b", "feature-orphan", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    real_which = janitor.shutil.which

    def _no_git(name, *args, **kwargs):
        if name == "git":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(janitor.shutil, "which", _no_git)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert wt not in {c.path for c in candidates}
    assert any(p == (repo / ".worktrees") for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()
    assert "FAILED to list" in text


def test_r1_worktree_list_failure_fails_closed_under_worktrees(tmp_path, monkeypatch):
    """The same fail-closed behaviour when git IS resolvable but `worktree
    list` itself exits non-zero (a damaged .git, "detected dubious
    ownership", etc.) - discovery cannot tell that apart from "no
    worktrees", so it must not treat it that way."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".worktrees" / "wt-orphan2"
    _git(repo, "worktree", "add", "-q", "-b", "feature-orphan2", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    real_run = janitor.subprocess.run

    def _fail_worktree_list(cmd, *args, **kwargs):
        if len(cmd) >= 3 and cmd[1] == "-C" and "worktree" in cmd and "list" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: simulated failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(janitor.subprocess, "run", _fail_worktree_list)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert wt not in {c.path for c in candidates}
    assert any(p == (repo / ".worktrees") for p, _reason in access_errors)


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_n2_dangling_link_removal_failure_reported_via_lexists_not_exists(tmp_path, monkeypatch):
    """A dangling link (its target deleted out from under it) whose own
    unlink fails must be reported FAILED - `path.exists()` would follow
    the (now-broken) link and read False, misreporting "removed" even
    though the link itself is still on disk."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = tmp_path / "target-to-vanish"
    target.mkdir()
    link = repo / ".review-danglink"
    if not _make_junction(link, target):
        pytest.skip("could not create a junction in this environment")
    shutil.rmtree(target)  # the link is now dangling

    def _always_fail(path):
        raise OSError("simulated stubborn dangling link")

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    result, _reason = janitor.remove_plainly(link)
    assert result == "FAILED"
    assert os.path.lexists(link)  # the link itself is still there


# --------------------------------- containment direction / fail-closed fallback


def test_r1f_git_working_detached_worktree_nested_in_stale_task_survives(tmp_path):
    """Round-5 containment fix: a scratch task directory that is itself
    stale (by newest-mtime) but HOLDS a detached, dirty worktree (the ops
    doc's own Rule 2 layout: `git worktree add --detach
    <scratch>/wt-<sha>`) must not be removed out from under that worktree
    just because the outer directory's own age check fires - staleness and
    "protect uncommitted work" are separate signals, and a refusal on the
    inner worktree must propagate to the outer candidate that contains
    it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-abc"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")
    # Backdate the whole nested tree so the OUTER task directory reads as
    # stale by newest-mtime, exactly like a review checkout nobody has
    # touched in a while but that still carries uncommitted work.
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert task_dir in {c.path for c in report.candidates}
    assert wt in set(report.dirty_worktrees)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()
    assert task_dir.exists()


def test_r1g_git_unresolvable_branch_worktree_nested_in_stale_task_survives(tmp_path, monkeypatch):
    """Round-5 fix, git-unresolvable side: even without git available to
    identify what is or isn't a worktree, a stale-looking scratch task
    directory whose tree contains a `.git` entry must be refused outright
    (via the git-independent _tree_contains_git_entry fallback), not
    removed as an ordinary stale candidate - the fallback substitutes for
    the WIP-commit/refuse gate that discovery ordinarily provides."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review2"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-branch"
    _git(repo, "worktree", "add", "-q", "-b", "feature-nested", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")
    _backdate_tree(task_dir, 10)

    real_which = janitor.shutil.which

    def _no_git(name, *args, **kwargs):
        if name == "git":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(janitor.shutil, "which", _no_git)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert task_dir not in {c.path for c in candidates}
    assert any(p == task_dir for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()
    assert task_dir.exists()


def test_r1h_git_unresolvable_repo_root_family_match_that_is_actually_a_worktree_survives(
    tmp_path, monkeypatch
):
    """Cheap round-5 pin: a repo-root directory that HAPPENS to match an
    ordinary family name (`.review-*`) but is actually a git worktree
    survives even when git is unresolvable - the .git-in-tree fallback
    guard applies uniformly across every pass, not just the scratch-root
    one."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".review-fakewt"
    _git(repo, "worktree", "add", "-q", "-b", "feature-fake", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    real_which = janitor.shutil.which

    def _no_git(name, *args, **kwargs):
        if name == "git":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(janitor.shutil, "which", _no_git)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert wt not in {c.path for c in candidates}
    assert any(p == wt for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()


def test_mb_git_status_failure_is_dirty_not_clean(tmp_path, monkeypatch):
    """A worktree whose `git status --porcelain` itself fails (a corrupted
    index, a lock file left behind by a crashed process) must be treated
    as dirty, not clean - matching the module's fail-closed rule
    everywhere else discovery can't be trusted (same style as the existing
    rc=128 `worktree list` mutation, applied to the status call)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".worktrees" / "wt-statusfail"
    _git(repo, "worktree", "add", "-q", "-b", "feature-statusfail", str(wt), "master")

    real_run = janitor.subprocess.run

    def _fail_status(cmd, *args, **kwargs):
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="",
                                                 stderr="fatal: simulated status failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(janitor.subprocess, "run", _fail_status)
    assert janitor.is_dirty_worktree(wt) is True


def test_mc_commit_succeeds_but_postcommit_status_fails_is_refused_and_kept(tmp_path, monkeypatch):
    """A WIP commit whose `git commit` call itself succeeds but whose
    immediate post-commit `git status --porcelain` confirmation fails must
    still refuse - an unconfirmed "clean" is not a confirmed one, and
    apply() must not remove (or report success for) a worktree on the
    strength of an unverified commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".worktrees" / "wt-mc"
    _git(repo, "worktree", "add", "-q", "-b", "feature-mc", str(wt), "master")
    (wt / "new-file.txt").write_text("new", encoding="utf-8")

    real_run = janitor.subprocess.run

    def _fail_status_for_wt(cmd, *args, **kwargs):
        if "status" in cmd and "--porcelain" in cmd and str(wt) in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="",
                                                 stderr="fatal: simulated status failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(janitor.subprocess, "run", _fail_status_for_wt)

    result = janitor.wip_commit_dirty_worktree(wt, default_branches=["master", "main"])
    assert result.refused is True

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert wt in set(report.dirty_worktrees)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "new-file.txt").exists()


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_me_fresh_task_level_link_survives_the_age_window(tmp_path):
    """The agent-level fresh-junction protection (N1) has a task-level
    sibling: scratch_root/<agent>/<task> can itself be a junction (a task
    relocated onto another volume, or linked from a different clone) just
    as much as scratch_root/<agent> can - the age gate must apply there
    too, not just one level up."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    agent_dir = scratch_root / "dev-4"
    agent_dir.mkdir(parents=True)
    real_task_dir = tmp_path / "real-task-dir-fresh"
    real_task_dir.mkdir()
    task_link = agent_dir / "task-fresh"
    if not _make_junction(task_link, real_task_dir):
        pytest.skip("could not create a junction in this environment")
    # Deliberately NOT backdated - this link is fresh.

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, _ = janitor.find_candidates(cfg)
    paths = {c.path for c in candidates}
    assert task_link not in paths

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert os.path.lexists(task_link)  # the fresh link survives


# --------------------------------------------- shared-root ownership (round 6)


def test_p1_foreign_clones_worktree_nested_in_stale_task_survives(tmp_path):
    """Round-6 R1: every seat shares one scratch root (Rule 1), and each
    seat's review worktree belongs to ITS OWN clone (Rule 2) - a worktree
    registered to a DIFFERENT clone is, to cfg.repo's own `worktree list`,
    invisible. A batch --apply pointed at repo A must not delete repo B's
    dirty worktree just because repo A has never heard of it."""
    repo_a = tmp_path / "repo-a"
    _init_repo(repo_a)
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_b)

    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-abc"
    _git(repo_b, "worktree", "add", "-q", "--detach", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo_a, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert task_dir not in {c.path for c in candidates}
    assert any(p == task_dir for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()
    assert task_dir.exists()


def test_p1c_own_clones_worktree_in_same_layout_goes_through_the_normal_gate(tmp_path):
    """Control for P1: the identical layout, but the janitor is pointed at
    the worktree's OWN clone this time - proves the ownership check does
    not over-refuse a legitimate case; the existing detached-HEAD refusal
    (not the new ownership check) is what keeps it."""
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_b)

    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review-control"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-abc"
    _git(repo_b, "worktree", "add", "-q", "--detach", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo_b, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert task_dir in {c.path for c in report.candidates}  # not refused at add()-time
    assert wt in set(report.dirty_worktrees)  # it IS cfg.repo's own registered worktree
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()
    assert task_dir.exists()


def test_p2_standalone_clone_nested_in_stale_task_survives(tmp_path):
    """Round-6 R1: a standalone clone (its own `git init`, not any repo's
    worktree) nested inside a stale scratch task must survive too - the
    ownership check is "is this even a git tree this janitor knows about",
    not just "is it registered to MY clone specifically"."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review-standalone"
    task_dir.mkdir(parents=True)
    clone = task_dir / "standalone-clone"
    _init_repo(clone)
    (clone / "uncommitted.txt").write_text("do not lose me", encoding="utf-8")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert task_dir not in {c.path for c in candidates}
    assert any(p == task_dir for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert clone.exists()
    assert (clone / "uncommitted.txt").exists()
    assert task_dir.exists()


def test_p3_clean_detached_worktree_with_unreachable_head_survives(tmp_path):
    """Round-6 R2: only DIRTY worktrees reached the refuse gate before - a
    CLEAN detached worktree holding a local commit reachable from no
    branch or tag (a reviewer's fixup, or a mutation-test commit, left on
    a `git worktree add --detach` checkout with no further edits since) is
    otherwise removed along with its task, and the final `worktree prune`
    drops its HEAD - the commit becomes unreachable garbage, exactly the
    loss a detached WIP commit is already refused to avoid."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review-unreachable"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-clean-detached"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "master")
    (wt / "extra.txt").write_text("local commit content", encoding="utf-8")
    _git(wt, "add", "extra.txt")
    _git(wt, "commit", "-q", "-m", "local commit on no branch")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert wt not in set(report.dirty_worktrees)  # clean - status --porcelain is empty
    # git prints "HEAD" (not empty) for a detached rev-parse --abbrev-ref
    assert janitor.worktree_branch(wt) in janitor._NEVER_AUTO_COMMIT_BRANCHES_SENTINELS
    assert janitor.worktree_head_reachable(wt) is False  # on no branch or tag

    janitor.apply(cfg, report)
    assert wt.exists()
    assert task_dir.exists()
    unreachable_sha = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    rev_list = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--all"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert unreachable_sha in rev_list


def test_r3_unlistable_subdirectory_counts_as_containing_git(tmp_path, monkeypatch):
    """R3: an unlistable subdirectory inside a tree must count as
    "contains .git" (conservative fail-closed), not "no .git found here" -
    pins the exact branch of the walk a code-level mutation would leave
    uncaught by every existing cell (none of which makes a nested
    subdirectory unlistable)."""
    root = tmp_path / "root"
    blocked = root / "unlistable"
    blocked.mkdir(parents=True)

    real_safe_iterdir = janitor._safe_iterdir

    def _blocked_iterdir(path):
        if path == blocked:
            return [], (path, "simulated access denied")
        return real_safe_iterdir(path)

    monkeypatch.setattr(janitor, "_safe_iterdir", _blocked_iterdir)
    assert janitor._tree_contains_git_entry(root) is True
    found, unlistable = janitor._find_git_entries(root)
    assert found == []
    assert unlistable is True


def test_r3_unlistable_nested_subdir_at_repo_root_is_refused(tmp_path, monkeypatch):
    """The same property, end to end through find_candidates() at the
    repo-root pass (no age gate there, so this isolates the git-in-tree
    check from the staleness walk's own, unrelated error handling)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = repo / ".review-deep"
    blocked = candidate / "unlistable"
    blocked.mkdir(parents=True)

    real_which = janitor.shutil.which

    def _no_git(name, *args, **kwargs):
        if name == "git":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(janitor.shutil, "which", _no_git)

    real_safe_iterdir = janitor._safe_iterdir

    def _blocked_iterdir(path):
        if path == blocked:
            return [], (path, "simulated access denied")
        return real_safe_iterdir(path)

    monkeypatch.setattr(janitor, "_safe_iterdir", _blocked_iterdir)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert candidate not in {c.path for c in candidates}
    assert any(p == candidate for p, _reason in access_errors)


def test_r3_clone_git_directory_under_untrusted_discovery_is_refused(tmp_path, monkeypatch):
    """R3: `.git` as a DIRECTORY (an ordinary clone), not just a
    worktree's gitdir FILE, must be recognized by the walk - every
    existing untrusted-discovery cell used a worktree gitfile only,
    leaving this branch uncaught."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = repo / ".review-clone"
    _init_repo(candidate)  # a standalone clone - .git is a DIRECTORY here
    (candidate / "extra.txt").write_text("do not lose me", encoding="utf-8")

    real_which = janitor.shutil.which

    def _no_git(name, *args, **kwargs):
        if name == "git":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(janitor.shutil, "which", _no_git)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert candidate not in {c.path for c in candidates}
    assert any(p == candidate for p, _reason in access_errors)


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_r3_link_inside_candidate_is_never_descended_into(tmp_path, monkeypatch):
    """R3: a link inside a candidate's tree pointing at something that
    holds `.git` must never be descended into - the candidate proceeds
    normally (removed, since it is otherwise an ordinary eligible
    candidate) and whatever the link points at is left completely alone,
    matching every other "never follow a link" guarantee in this
    module."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    other_repo = tmp_path / "other-repo-with-dirty-wt"
    _init_repo(other_repo)
    other_wt = other_repo / ".worktrees" / "wt-elsewhere"
    _git(other_repo, "worktree", "add", "-q", "--detach", str(other_wt), "master")
    (other_wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    real_which = janitor.shutil.which

    def _no_git(name, *args, **kwargs):
        if name == "git":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(janitor.shutil, "which", _no_git)

    candidate = repo / ".review-withlink"
    candidate.mkdir()
    link = candidate / "elsewhere"
    if not _make_junction(link, other_wt):
        pytest.skip("could not create a junction in this environment")

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    # The candidate itself proceeds - the link inside it is never entered,
    # so its tree does not "contain .git" as far as this janitor can see.
    assert candidate in {c.path for c in candidates}
    assert not any(p == candidate for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert not candidate.exists()  # the candidate itself IS removed
    assert other_wt.exists()  # but the link's target was never entered
    assert (other_wt / "precious.txt").exists()


# ----------------------------------------------- .worktrees/ ownership (round 7)


def test_p5_worktrees_dir_foreign_clones_dirty_worktree_survives(tmp_path):
    """Round-7 R1: the ownership check must also apply to .worktrees/
    entries - that pass adds EVERY directory found there regardless of
    name, not just ones git actually recognizes. A worktree registered
    to a DIFFERENT clone, dropped directly under repo A's own
    .worktrees/, must not be removed just because repo A's own `worktree
    list` has never heard of it."""
    repo_a = tmp_path / "repo-a"
    _init_repo(repo_a)
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_b)

    wt = repo_a / ".worktrees" / "wt-foreign"
    _git(repo_b, "worktree", "add", "-q", "--detach", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    cfg = janitor.JanitorConfig(
        repo=repo_a, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert wt not in {c.path for c in candidates}
    assert any(p == wt for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()


def test_p6_worktrees_dir_standalone_clone_survives(tmp_path):
    """Round-7 R1: a standalone clone (not any worktree at all) dropped
    directly under .worktrees/ must survive too - "every directory there
    is a candidate" is about the family filter being absent, not about
    ownership being irrelevant."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    clone = repo / ".worktrees" / "standalone"
    _init_repo(clone)
    (clone / "uncommitted.txt").write_text("do not lose me", encoding="utf-8")

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert clone not in {c.path for c in candidates}
    assert any(p == clone for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert clone.exists()
    assert (clone / "uncommitted.txt").exists()


def test_p7_own_worktree_with_missing_admin_entry_survives(tmp_path):
    """Round-7 R1: this repo's OWN worktree, registered normally, whose
    `.git/worktrees/<name>` admin entry has separately gone missing (git
    no longer lists it at all) is exactly the "real, unrecovered work"
    is_dirty_worktree's own docstring names - it must survive just like a
    genuinely foreign tree, not be removed because git can no longer
    vouch for it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".worktrees" / "wt-orphaned-admin"
    _git(repo, "worktree", "add", "-q", "-b", "feature-orphaned", str(wt), "master")
    (wt / "precious.txt").write_text("do not lose me", encoding="utf-8")

    # Simulate the admin entry vanishing: `git worktree list` no longer
    # knows about it, but the worktree's own directory (and its .git
    # gitdir-pointer FILE) is untouched.
    admin_line = (wt / ".git").read_text(encoding="utf-8").strip()
    assert admin_line.startswith("gitdir: ")
    admin_dir = Path(admin_line[len("gitdir: "):])
    shutil.rmtree(admin_dir)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    assert wt not in set(janitor.get_registered_worktrees(repo))  # git no longer lists it
    candidates, access_errors = janitor.find_candidates(cfg)
    assert wt not in {c.path for c in candidates}
    assert any(p == wt for p, _reason in access_errors)

    report = janitor.build_report(cfg)
    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "precious.txt").exists()


def test_mo3_trusted_discovery_refuses_unlistable_subtree(tmp_path, monkeypatch):
    """R2/MO3: the TRUSTED-discovery ownership branch must also refuse on
    an unlistable subtree (`if foreign or unlistable`), not just a
    foreign .git - every existing unlistable-subtree cell runs under
    UNTRUSTED discovery, so dropping `or unlistable` from the trusted
    branch alone would stay green against them."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = repo / ".review-unlistable-trusted"
    blocked = candidate / "unlistable"
    blocked.mkdir(parents=True)

    real_safe_iterdir = janitor._safe_iterdir

    def _blocked_iterdir(path):
        if path == blocked:
            return [], (path, "simulated access denied")
        return real_safe_iterdir(path)

    monkeypatch.setattr(janitor, "_safe_iterdir", _blocked_iterdir)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    candidates, access_errors = janitor.find_candidates(cfg)
    assert candidate not in {c.path for c in candidates}
    assert any(p == candidate for p, _reason in access_errors)


def test_mh2_reachability_check_failure_is_treated_as_unreachable(tmp_path, monkeypatch):
    """R2/MH2: a `for-each-ref --contains` call that itself fails must be
    read as unreachable (fail closed), not reachable - no other cell
    makes the reachability check itself fail, so flipping this branch
    alone would stay green."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = repo / ".worktrees" / "wt-mh2"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "master")

    real_run = janitor.subprocess.run

    def _fail_for_each_ref(cmd, *args, **kwargs):
        if "for-each-ref" in cmd and str(wt) in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="",
                                                 stderr="fatal: simulated failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(janitor.subprocess, "run", _fail_for_each_ref)
    assert janitor.worktree_head_reachable(wt) is False


def test_p8c_clean_detached_worktree_at_branch_tip_in_stale_task_is_removed(tmp_path):
    """R2/MH3 control: a clean detached worktree whose HEAD IS reachable
    (checked out exactly at a branch tip, no extra commits) must still be
    removable - dropping refs/heads from the reachability check would
    refuse every detached worktree unconditionally, and nothing else
    would notice."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review-reachable"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-clean-detached-reachable"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "master")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert wt not in set(report.dirty_worktrees)  # clean
    assert janitor.worktree_branch(wt) in janitor._NEVER_AUTO_COMMIT_BRANCHES_SENTINELS
    assert janitor.worktree_head_reachable(wt) is True  # exactly at master's tip

    janitor.apply(cfg, report)
    assert not task_dir.exists()  # removed - nothing here needed protecting


# ------------------------------------------------ refs/remotes crediting (round 8)


def test_p8_worktree_at_remote_only_commit_is_removed(tmp_path):
    """Round-8 R1: worktree_head_reachable credits refs/remotes on
    purpose - a review worktree detached at a fetched PR head (Rule 2's
    own recommended form) is routinely reachable ONLY via a remote-
    tracking ref until it lands on a local branch. A clean detached
    worktree of exactly such a commit must remain removable, not be
    refused just because no LOCAL branch or tag contains it."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "remote-only.txt").write_text("remote commit", encoding="utf-8")
    _git(origin, "add", "remote-only.txt")
    _git(origin, "commit", "-q", "-m", "remote-only commit")

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "origin")

    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review-remote-only"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-remote-only"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "origin/master")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert wt not in set(report.dirty_worktrees)  # clean
    assert janitor.worktree_branch(wt) in janitor._NEVER_AUTO_COMMIT_BRANCHES_SENTINELS
    assert janitor.worktree_head_reachable(wt) is True  # only refs/remotes/origin/* contains it

    janitor.apply(cfg, report)
    assert not task_dir.exists()  # removed - reachable via the remote-tracking ref


def test_p8f_local_fixup_on_remote_only_worktree_is_refused(tmp_path):
    """Companion to P8: a LOCAL fixup commit made on top of that same
    detached checkout is reachable from NOTHING (not even refs/remotes,
    since the fixup itself was never pushed anywhere) - the refs/remotes
    crediting must not paper over a genuinely local-only commit. Guards
    the dangerous direction if the credited ref set is ever widened
    further."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "remote-only.txt").write_text("remote commit", encoding="utf-8")
    _git(origin, "add", "remote-only.txt")
    _git(origin, "commit", "-q", "-m", "remote-only commit")

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "origin")

    scratch_root = tmp_path / "atk-scratch"
    task_dir = scratch_root / "dev-9" / "old-review-remote-fixup"
    task_dir.mkdir(parents=True)
    wt = task_dir / "wt-remote-fixup"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "origin/master")
    (wt / "fixup.txt").write_text("local fixup", encoding="utf-8")
    _git(wt, "add", "fixup.txt")
    _git(wt, "commit", "-q", "-m", "local fixup on top of the remote-only commit")
    _backdate_tree(task_dir, 10)

    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert wt not in set(report.dirty_worktrees)  # clean - the fixup IS committed
    assert janitor.worktree_head_reachable(wt) is False  # the fixup itself is nowhere else

    janitor.apply(cfg, report)
    assert wt.exists()
    assert (wt / "fixup.txt").exists()
    assert task_dir.exists()


# ------------------------------- #342: a failed delete is kept and reported, never forced


_ESCALATION_TOOLS = {"takeown", "icacls", "robocopy"}


def _make_dir_symlink(link: Path, target: Path) -> bool:
    """A directory symbolic link. Returns False (skip the test) where creating one needs a
    privilege the test process lacks (Windows without Developer Mode or elevation)."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


def _stale_task_with_nested_link(tmp_path: Path, kind: str) -> dict:
    """A scratch task older than keep_days whose tree holds a nested folder link
    (`sub/link-out`) to a folder OUTSIDE the task. A sentinel file of random bytes sits
    beyond the link; the link's own mtime is backdated too, so the task is a candidate."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    data = os.urandom(256)
    sentinel.write_bytes(data)
    scratch_root = tmp_path / "atk-scratch"
    task = scratch_root / "dev-9" / "old-task"
    (task / "sub").mkdir(parents=True)
    (task / "sub" / "own.txt").write_text("own", encoding="utf-8")
    link = task / "sub" / "link-out"
    if kind == "junction":
        if platform.system() != "Windows":
            pytest.skip("NTFS junctions are Windows-only")
        if not _make_junction(link, outside):
            pytest.skip("could not create a junction in this environment")
    elif not _make_dir_symlink(link, outside):
        pytest.skip("directory symlink privilege/support not available in this environment")
    _backdate(link, 10, follow_symlinks=False)
    _backdate(task / "sub" / "own.txt", 10)
    _backdate(task / "sub", 10)
    _backdate(task, 10)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_root, repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    return {"cfg": cfg, "task": task, "link": link, "outside": outside,
            "sentinel": sentinel, "data": data}


def _spy_subprocess(monkeypatch) -> list:
    calls: list = []
    real_run = subprocess.run

    def _spy_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(janitor.subprocess, "run", _spy_run)
    return calls


def _escalations(calls: list) -> list:
    return [c for c in calls if Path(str(c[0])).stem.lower() in _ESCALATION_TOOLS]


def _assert_outside_untouched(t: dict) -> None:
    assert t["sentinel"].read_bytes() == t["data"]
    assert sorted(p.name for p in t["outside"].iterdir()) == ["sentinel.bin"]


@pytest.mark.parametrize("kind", ["junction", "dir-symlink"])
def test_342_failed_delete_keeps_and_reports_and_never_reaches_through_a_nested_link(
    tmp_path, monkeypatch, kind
):
    """#342: the ordinary delete is forced to fail on an old candidate holding a nested
    folder link. Nothing beyond the link may change (byte-checked), no ownership, ACL or
    mirror tool may run, the candidate is kept as it was, and the apply report names it
    relative to its scanned root with the reason."""
    t = _stale_task_with_nested_link(tmp_path, kind)
    calls = _spy_subprocess(monkeypatch)

    def _always_fail(path):
        raise PermissionError(13, "simulated access denied", str(path / "sub" / "own.txt"))

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    report = janitor.build_report(t["cfg"])
    assert t["task"] in [c.path for c in report.candidates]

    text = janitor.apply(t["cfg"], report)

    _assert_outside_untouched(t)
    assert _escalations(calls) == []
    assert (t["task"] / "sub" / "own.txt").read_text(encoding="utf-8") == "own"
    assert os.path.lexists(t["link"])
    summary = text.split("removed summary:", 1)[1]
    assert "  FAILED: 1" in summary
    assert "removed-after" not in summary
    expected = (f"  [scratch] {Path('dev-9', 'old-task')}: simulated access denied "
                f"(at {Path('sub', 'own.txt')})")
    assert expected in summary.splitlines()
    assert str(t["task"]) not in summary  # relative to the scanned root, not absolute


@pytest.mark.parametrize("kind", ["junction", "dir-symlink"])
def test_342_ordinary_delete_removes_a_nested_link_as_a_link_never_entering_it(
    tmp_path, monkeypatch, kind
):
    """The ordinary path: the real link-safe delete removes the candidate, and the nested
    link with it AS A LINK - the folder it points at, and the sentinel in it, stay
    byte-identical - with no ownership, ACL or mirror tool involved."""
    t = _stale_task_with_nested_link(tmp_path, kind)
    calls = _spy_subprocess(monkeypatch)
    report = janitor.build_report(t["cfg"])
    assert t["task"] in [c.path for c in report.candidates]

    text = janitor.apply(t["cfg"], report)

    assert not os.path.lexists(t["task"])
    _assert_outside_untouched(t)
    assert _escalations(calls) == []
    assert "  removed: 1" in text.split("removed summary:", 1)[1]


def test_342_remove_plainly_reports_failure_with_its_reason_and_runs_nothing(tmp_path, monkeypatch):
    """The removal itself: a failed ordinary delete is ('FAILED', reason) with the path
    left in place, and no process is started at all."""
    target = tmp_path / "old"
    (target / "inner").mkdir(parents=True)
    calls = _spy_subprocess(monkeypatch)

    def _always_fail(path):
        raise PermissionError(13, "simulated access denied", str(path / "inner"))

    monkeypatch.setattr(janitor, "_rmtree", _always_fail)
    outcome, reason = janitor.remove_plainly(target)
    assert outcome == "FAILED"
    assert reason == "simulated access denied (at inner)"
    assert target.is_dir()
    assert calls == []


def test_342_remove_plainly_outcomes_without_failure(tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    (present / "f.txt").write_text("x", encoding="utf-8")
    assert janitor.remove_plainly(present) == ("removed", None)
    assert not present.exists()
    assert janitor.remove_plainly(tmp_path / "never-there") == ("absent", None)


def test_342_janitor_has_no_forced_delete_left():
    """No ownership takeover, ACL rewrite or mirror delete anywhere in the module, and no
    'removed-after-...' outcome: when the ordinary delete fails, the path is kept."""
    import inspect

    source = inspect.getsource(janitor).lower()
    for word in ("takeown", "icacls", "robocopy", "removed-after", "remove_stubborn"):
        assert word not in source, word


def test_342_failed_path_is_named_relative_to_the_closest_scanned_root(tmp_path):
    """A kept path is named relative to the scanned root that holds it most closely,
    even when one root sits inside another (a scratch root configured inside the repo)."""
    repo = tmp_path / "repo"
    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=repo / "scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=[], repo_file_families=[],
        tmp_families=[], foreign=[], default_branches=["master"],
    )
    named = janitor._scanned_root_relative
    assert named(repo / "scratch" / "dev-9" / "t", cfg) == f"[scratch] {Path('dev-9', 't')}"
    assert named(repo / ".review-x", cfg) == "[repo] .review-x"
    assert named(tmp_path / "tmp" / "pytest-of-x", cfg) == "[tmp] pytest-of-x"
    assert named(tmp_path / "elsewhere", cfg) == str(tmp_path / "elsewhere")


# ------------- #342 fix round 1: replacement races, unknown is not absent, relative listing failures


def _stale_scratch_task(tmp_path: Path) -> dict:
    """A plain stale scratch task `scratch/seat/task` (no link anywhere), its config, and an
    outside folder holding a same-named task with a sentinel - the target a swapped-in
    junction would point at (the cold read's probe shape)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    scratch_root = tmp_path / "atk-scratch"
    seat = scratch_root / "seat"
    task = seat / "task"
    task.mkdir(parents=True)
    (task / "old.txt").write_text("old", encoding="utf-8")
    _backdate(task / "old.txt", 10)
    _backdate(task, 10)
    outside = tmp_path / "outside"
    (outside / "task").mkdir(parents=True)
    sentinel = outside / "task" / "sentinel.bin"
    data = os.urandom(256)
    sentinel.write_bytes(data)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=scratch_root, keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_root, repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    return {"cfg": cfg, "seat": seat, "task": task, "outside": outside,
            "sentinel": sentinel, "data": data, "tmp_path": tmp_path}


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_342_ancestor_replaced_by_a_junction_after_discovery_is_kept_and_reported(tmp_path):
    """Cold read probe 1: a plain stale task is found, then its parent seat folder is
    renamed away and a junction to an outside folder put in its place. The delete must
    not happen: the outside sentinel is byte-identical, the task is kept and reported."""
    t = _stale_scratch_task(tmp_path)
    report = janitor.build_report(t["cfg"])
    assert t["task"] in [c.path for c in report.candidates]
    t["seat"].rename(tmp_path / "atk-scratch" / "original-seat")
    if not _make_junction(t["seat"], t["outside"]):
        pytest.skip("could not create a junction in this environment")

    text = janitor.apply(t["cfg"], report)

    assert t["sentinel"].read_bytes() == t["data"]
    assert sorted(p.name for p in (t["outside"] / "task").iterdir()) == ["sentinel.bin"]
    assert (tmp_path / "atk-scratch" / "original-seat" / "task" / "old.txt").exists()
    summary = text.split("removed summary:", 1)[1]
    assert "  FAILED: 1" in summary
    assert (f"  [scratch] {Path('seat', 'task')}: the folder seat on its path is now a link "
            "(junction, symbolic link or other reparse point); nothing was deleted") in summary.splitlines()


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_342_candidate_that_became_a_link_after_discovery_is_kept_and_reported(tmp_path):
    """A candidate found as a plain folder that is a junction by the time of the delete is
    not removed (not even as a link): it is no longer the thing that was found."""
    t = _stale_scratch_task(tmp_path)
    report = janitor.build_report(t["cfg"])
    t["task"].rename(t["seat"] / "original-task")
    if not _make_junction(t["task"], t["outside"] / "task"):
        pytest.skip("could not create a junction in this environment")

    text = janitor.apply(t["cfg"], report)

    assert t["sentinel"].read_bytes() == t["data"]
    assert os.path.lexists(t["task"])  # the junction is kept too
    assert (f"  [scratch] {Path('seat', 'task')}: it is now a link (junction, symbolic link or "
            "other reparse point), and it was not when it was found; nothing was deleted") in text.splitlines()


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
@pytest.mark.xfail(
    not shutil.rmtree.avoids_symlink_attacks, strict=True,
    reason=(
        "Bounded residual (#342, PR #386 fix round 1): shutil.rmtree on this platform is not "
        "protected against a concurrent swap (avoids_symlink_attacks is False), so a folder "
        "replaced by a junction INSIDE its own delete window is deleted through. Out of scope: "
        "agenttalk is a trusted, single-user local tool, and janitor runs while no other process "
        "rewrites that tree. The cold read's probe 2 shape, kept so the residual stays visible."
    ),
)
def test_342_residual_candidate_swapped_inside_the_stdlib_delete_window(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    data = os.urandom(64)
    sentinel.write_bytes(data)
    real_lstat = os.lstat
    swapped = []

    def swap_after_lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if not swapped and Path(path) == candidate:
            swapped.append(True)
            candidate.rename(tmp_path / "original-candidate")
            assert _make_junction(candidate, outside)
        return st

    real_rmtree = shutil.rmtree

    def raced_rmtree(path, *args, **kwargs):
        monkeypatch.setattr(os, "lstat", swap_after_lstat)
        try:
            return real_rmtree(path, *args, **kwargs)
        finally:
            monkeypatch.setattr(os, "lstat", real_lstat)

    monkeypatch.setattr(shutil, "rmtree", raced_rmtree)
    janitor.remove_plainly(candidate)
    assert swapped
    assert sentinel.read_bytes() == data


def _denied_lstat_for(target: Path, monkeypatch, *, start_denied: bool):
    """os.lstat that raises PermissionError for `target` while `state['denied']` is true."""
    real_lstat = os.lstat
    state = {"denied": start_denied}

    def lstat(path, *args, **kwargs):
        if state["denied"] and Path(path) == target:
            raise PermissionError(13, "simulated metadata denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(janitor.os, "lstat", lstat)
    return state


def test_342_initial_existence_check_failing_is_failed_not_absent(tmp_path, monkeypatch):
    """Only a confirmed not-found may give 'absent': a denied metadata read is FAILED, with
    its reason, and the path is left in place (the cold read reproduced 'absent' on 3.10)."""
    target = tmp_path / "old"
    (target / "inner").mkdir(parents=True)
    _denied_lstat_for(target, monkeypatch, start_denied=True)
    outcome, reason = janitor.remove_plainly(target)
    assert outcome == "FAILED"
    assert reason == "could not check whether it exists: simulated metadata denied"
    assert target.is_dir()


def test_342_final_existence_check_failing_is_failed_not_removed(tmp_path, monkeypatch):
    """A failed delete followed by a denied metadata read must not be reported 'removed'."""
    target = tmp_path / "old"
    (target / "inner").mkdir(parents=True)
    state = _denied_lstat_for(target, monkeypatch, start_denied=False)

    def failing_delete(path):
        state["denied"] = True
        raise PermissionError(13, "simulated access denied", str(path / "inner"))

    monkeypatch.setattr(janitor, "_rmtree", failing_delete)
    outcome, reason = janitor.remove_plainly(target)
    assert outcome == "FAILED"
    assert reason == "simulated access denied (at inner)"
    assert target.is_dir()


def test_342_successful_delete_with_an_unconfirmable_result_is_failed(tmp_path, monkeypatch):
    """The delete raised nothing, but whether the path is gone cannot be read: FAILED."""
    target = tmp_path / "old"
    target.mkdir()
    state = _denied_lstat_for(target, monkeypatch, start_denied=False)

    def silent_delete(path):
        state["denied"] = True

    monkeypatch.setattr(janitor, "_rmtree", silent_delete)
    assert janitor.remove_plainly(target) == (
        "FAILED", "could not confirm it is gone: simulated metadata denied")


def test_342_listing_failures_are_relative_and_carry_no_filename(tree, monkeypatch):
    """The listing-failure part of the report names each location relative to its scanned
    root, and the reason is the system's words only, without the exception's filename."""
    stale = tree["cfg"].scratch_root / "dev-2" / "old-task"
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self == stale:
            raise PermissionError(13, "simulated listing denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    report = janitor.build_report(tree["cfg"])
    text = janitor.format_report(report, tree["cfg"], apply=False)
    assert f"  [scratch] {Path('dev-2', 'old-task')}: simulated listing denied" in text.splitlines()
    listing = text.split("FAILED to list", 1)[1].split("\n  20", 1)[0]
    assert str(stale) not in listing


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_342_scanned_root_replaced_by_a_junction_after_discovery_is_kept(tmp_path):
    """The same check covers the scanned root itself: the scratch root renamed away and a
    junction put in its place after discovery keeps every candidate under it."""
    t = _stale_scratch_task(tmp_path)
    report = janitor.build_report(t["cfg"])
    assert t["task"] in [c.path for c in report.candidates]
    outside_root = tmp_path / "outside-root"
    (outside_root / "seat" / "task").mkdir(parents=True)
    sentinel = outside_root / "seat" / "task" / "sentinel.bin"
    sentinel.write_bytes(t["data"])
    (tmp_path / "atk-scratch").rename(tmp_path / "original-scratch")
    if not _make_junction(tmp_path / "atk-scratch", outside_root):
        pytest.skip("could not create a junction in this environment")

    text = janitor.apply(t["cfg"], report)

    assert sentinel.read_bytes() == t["data"]
    assert (f"  [scratch] {Path('seat', 'task')}: the scanned root [scratch] is a link or no "
            "longer a plain folder; nothing was deleted") in text.splitlines()


def test_342_a_failed_check_of_a_folder_on_the_path_keeps_the_candidate(tmp_path, monkeypatch):
    """When a folder between the scanned root and the candidate cannot be checked (its
    metadata read is denied), the candidate is kept and reported, never deleted."""
    t = _stale_scratch_task(tmp_path)
    report = janitor.build_report(t["cfg"])
    assert t["task"] in [c.path for c in report.candidates]
    state = _denied_lstat_for(t["seat"], monkeypatch, start_denied=False)
    state["denied"] = True

    text = janitor.apply(t["cfg"], report)

    assert (t["task"] / "old.txt").read_text(encoding="utf-8") == "old"
    assert (f"  [scratch] {Path('seat', 'task')}: could not check the folder seat on its path: "
            "simulated metadata denied; nothing was deleted") in text.splitlines()


@pytest.mark.skipif(platform.system() != "Windows", reason="NTFS junctions are Windows-only")
def test_342_candidate_that_was_a_link_and_is_no_longer_one_is_kept(tmp_path):
    """The third documented case: a candidate found as a link is removed as the link
    (test_p6a); if it is a plain folder by the time of the delete, it is kept and
    reported, never deleted as a folder."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = tmp_path / "real-target"
    target.mkdir()
    link = repo / ".review-link"
    if not _make_junction(link, target):
        pytest.skip("could not create a junction in this environment")
    cfg = janitor.JanitorConfig(
        repo=repo, scratch_root=tmp_path / "atk-scratch", keep_days=3, tmp_keep_days=1,
        tmp_root=tmp_path / "tmp", repo_dir_families=janitor.DEFAULT_REPO_DIR_FAMILIES,
        repo_file_families=janitor.DEFAULT_REPO_FILE_FAMILIES, tmp_families=[],
        foreign=[], default_branches=["master", "main"],
    )
    report = janitor.build_report(cfg)
    assert [c.link for c in report.candidates if c.path == link] == [True]
    os.rmdir(link)  # the link itself, never its target
    link.mkdir()
    (link / "now-a-folder.txt").write_text("kept", encoding="utf-8")

    text = janitor.apply(cfg, report)

    assert (link / "now-a-folder.txt").read_text(encoding="utf-8") == "kept"
    assert ("  [repo] .review-link: it was a link when it was found and is not one now; "
            "nothing was deleted") in text.splitlines()



@pytest.mark.parametrize("source", ["flag", "environment", "working-directory"])
def test_410_linked_main_checkout_never_gets_wip(tmp_path, monkeypatch, capsys, source):
    from agenttalk.cli import main

    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    _git(repo, "checkout", "-qb", "feature/work")
    (repo / "tracked.txt").write_text("unfinished", encoding="utf-8")
    (repo / ".agenttalk").mkdir()
    (repo / ".agenttalk/config.json").write_text(json.dumps({"scratch": {
        "root": str(t["cfg"].scratch_root), "tmp_root": str(t["cfg"].tmp_root),
    }}), encoding="utf-8")
    alias = tmp_path / "alias"
    _directory_link(alias, repo)
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    args = ["janitor", "--apply"]
    if source == "flag":
        args = ["--root", str(alias), *args]
    elif source == "environment":
        monkeypatch.setenv("AGENTTALK_ROOT", str(alias))
    else:
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: alias))
    try:
        main(args)
        assert janitor._run_git(repo, "rev-list", "--count", "HEAD").strip() == "1"
        assert "WIP committed" not in capsys.readouterr().out
    finally:
        alias.rmdir() if platform.system() == "Windows" else alias.unlink()


@pytest.mark.parametrize("spelling", ["dotdot", "link"])
def test_410_registered_worktree_identity_uses_physical_path(tmp_path, spelling):
    t = _stale_scratch_task(tmp_path)
    cfg = t["cfg"]
    wt = cfg.repo / ".worktrees/finished"
    _git(cfg.repo, "worktree", "add", "-qb", "finished", str(wt))
    alias = tmp_path / "alias"
    if spelling == "link":
        _directory_link(alias, cfg.repo)
        cfg.repo = alias
    else:
        (cfg.repo / "sub").mkdir()
        cfg.repo = cfg.repo / "sub/.."
    try:
        report = janitor.build_report(cfg)
        assert not report.access_errors
        assert cfg.repo / ".worktrees/finished" in [c.path for c in report.candidates]
        text = janitor.apply(cfg, report)
        if spelling == "link":
            # Identity is recognized, but the separate link-safety rule still refuses deletion.
            assert wt.exists() and "scanned root [repo] is a link" in text
        else:
            assert not wt.exists()
    finally:
        if spelling == "link":
            alias.rmdir() if platform.system() == "Windows" else alias.unlink()


@pytest.mark.parametrize("ignored", [".env", "nested/.agenttalk/messages/one.json", "local-data.bin",
                                    ".pytest_cache/nested/.env"])
@pytest.mark.parametrize("dirty", [False, True])
def test_410_ignored_work_is_kept_without_commit(tmp_path, ignored, dirty):
    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    wt = repo / ".worktrees/finished"
    _git(repo, "worktree", "add", "-qb", "finished", str(wt))
    (repo / ".git/info/exclude").write_text(".env\n.agenttalk/\nlocal-data.bin\n.pytest_cache/\n", encoding="utf-8")
    keep = wt / ignored
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("irreplaceable", encoding="utf-8")
    if dirty:
        (wt / "tracked.txt").write_text("unfinished", encoding="utf-8")
    text = janitor.apply(t["cfg"], janitor.build_report(t["cfg"]))
    assert keep.exists(), "ignored work was deleted"
    assert keep.read_text(encoding="utf-8") == "irreplaceable"
    assert janitor._run_git(wt, "rev-list", "--count", "HEAD").strip() == "1"
    assert "kept" in text.lower() and "ignored" in text.lower()


@pytest.mark.parametrize("name", [".env", "nested/.agenttalk/messages/one.json"])
def test_410_unignored_protected_work_is_kept(tmp_path, name):
    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    wt = repo / ".worktrees/finished"
    _git(repo, "worktree", "add", "-qb", "finished", str(wt))
    keep = wt / name
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("irreplaceable", encoding="utf-8")
    text = janitor.apply(t["cfg"], janitor.build_report(t["cfg"]))
    assert keep.exists()
    assert janitor._run_git(wt, "rev-list", "--count", "HEAD").strip() == "1"
    assert "protected worktree content" in text


@pytest.mark.parametrize("cache", ["__pycache__", ".pytest_cache", ".ruff_cache", "example.egg-info"])
def test_410_known_ignored_caches_allow_removal(tmp_path, cache):
    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    wt = repo / ".worktrees/finished"
    _git(repo, "worktree", "add", "-qb", "finished", str(wt))
    (repo / ".git/info/exclude").write_text(cache + "/\n", encoding="utf-8")
    (wt / cache).mkdir()
    (wt / cache / "cache.bin").write_bytes(b"rebuildable")
    janitor.apply(t["cfg"], janitor.build_report(t["cfg"]))
    assert not wt.exists()


def test_410_dirty_worktree_outside_candidates_never_gets_wip(tmp_path):
    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    wt = tmp_path / "active"
    _git(repo, "worktree", "add", "-qb", "active", str(wt))
    (wt / "tracked.txt").write_text("unfinished", encoding="utf-8")
    text = janitor.apply(t["cfg"], janitor.build_report(t["cfg"]))
    assert janitor._run_git(wt, "rev-list", "--count", "HEAD").strip() == "1"
    assert janitor.is_dirty_worktree(wt)
    assert "WIP committed" not in text


def test_410_candidate_inside_active_worktree_is_kept(tmp_path):
    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    wt = tmp_path / "active"
    _git(repo, "worktree", "add", "-qb", "active", str(wt))
    t["cfg"].scratch_root = wt
    task = wt / "seat/task"
    task.mkdir(parents=True)
    keep = task / "unfinished.txt"
    keep.write_text("unfinished", encoding="utf-8")
    _backdate_tree(task, 10)
    text = janitor.apply(t["cfg"], janitor.build_report(t["cfg"]))
    assert keep.exists()
    assert janitor._run_git(wt, "rev-list", "--count", "HEAD").strip() == "1"
    assert "inside a registered worktree" in text


def test_410_unknown_ignored_files_keep_worktree(tmp_path, monkeypatch):
    t = _stale_scratch_task(tmp_path)
    repo = t["cfg"].repo
    wt = repo / ".worktrees/finished"
    _git(repo, "worktree", "add", "-qb", "finished", str(wt))
    (wt / "tracked.txt").write_text("unfinished", encoding="utf-8")
    run_git = janitor._run_git_checked

    def unreadable_ignored(path, *args):
        if args[0] == "ls-files":
            return 1, "", "simulated ignored-file failure"
        return run_git(path, *args)

    monkeypatch.setattr(janitor, "_run_git_checked", unreadable_ignored)
    text = janitor.apply(t["cfg"], janitor.build_report(t["cfg"]))
    assert wt.exists()
    assert janitor._run_git(wt, "rev-list", "--count", "HEAD").strip() == "1"
    assert "could not check ignored files" in text


def test_410_parent_swap_between_candidate_removals(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    t = _stale_scratch_task(parent)
    second = t["seat"] / "task-two"
    second.mkdir()
    (second / "old.txt").write_text("keep", encoding="utf-8")
    _backdate_tree(second, 10)
    report = janitor.build_report(t["cfg"])
    assert len(report.candidates) == 2
    decoy = tmp_path / "decoy"
    victim = decoy / "atk-scratch/seat/task-two/sentinel"
    victim.parent.mkdir(parents=True)
    victim.write_text("untouched", encoding="utf-8")
    real_remove = janitor.remove_plainly
    calls = []

    def remove_then_swap(path):
        calls.append(path)
        result = real_remove(path)
        if len(calls) == 1:
            parent.rename(tmp_path / "original")
            _directory_link(parent, decoy)
        return result

    monkeypatch.setattr(janitor, "remove_plainly", remove_then_swap)
    try:
        text = janitor.apply(t["cfg"], report)
        assert victim.exists(), "second deletion reached the replacement parent"
        assert victim.read_text(encoding="utf-8") == "untouched"
        assert (tmp_path / "original/atk-scratch/seat/task-two/old.txt").exists()
        assert len(calls) == 1 and "FAILED: 1" in text
    finally:
        if janitor.is_link_like(parent):
            parent.rmdir() if platform.system() == "Windows" else parent.unlink()


@pytest.mark.parametrize("failure", ["scan", "remove"])
def test_410_cli_failure_returns_nonzero(tmp_path, monkeypatch, capsys, failure):
    from agenttalk.cli import main

    t = _stale_scratch_task(tmp_path)
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.setattr(janitor.JanitorConfig, "load", classmethod(lambda cls, root: t["cfg"]))
    if failure == "scan":
        monkeypatch.setattr(janitor, "_ancestor_ids", lambda cfg: ({}, [(cfg.repo, "scan refused")]))
    else:
        monkeypatch.setattr(janitor, "remove_plainly", lambda path: ("FAILED", "denied"))
    assert main(["--root", str(t["cfg"].repo), "janitor", "--apply"]) != 0
    assert "FAILED" in capsys.readouterr().out
