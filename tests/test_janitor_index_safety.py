"""Preservation must never stage around hidden files or nested work."""

import contextlib

from agenttalk import janitor, worktree_release
from test_git_index_safety import git_reads
from types import SimpleNamespace
import subprocess
import os

import pytest


@contextlib.contextmanager
def _no_use_check():
    """The activity check (#858) reads a real config; these tests use a stand-in one and test something else."""
    yield


class _NoGuard:
    """A guard that finds nothing in use: the activity check has its own tests (test_janitor_active_lanes.py)."""

    def __init__(self, cfg, targets, **kwargs):
        pass

    def reread(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    final_window = staticmethod(_no_use_check)


def test_janitor_clean_status_cannot_certify_hidden_files(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    monkeypatch.setattr(janitor.shutil, "which", lambda _: "git")
    git_reads(monkeypatch, b"h precious\0")
    assert janitor.is_dirty_worktree(tmp_path) is True


def test_direct_wip_refuses_before_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor.shutil, "which", lambda _: "git")
    git_reads(monkeypatch, b"h precious\0")
    monkeypatch.setattr(janitor, "worktree_branch", lambda _: "feature")
    result = janitor.wip_commit_dirty_worktree(tmp_path, default_branches=["main"])
    assert result.refused and "Git is skipping checks" in result.message


@pytest.mark.parametrize("gate", ["preflight", "before-delete"])
def test_apply_keeps_entire_candidate_before_any_destructive_step(tmp_path, monkeypatch, gate):
    repo, candidate = tmp_path / "repo", tmp_path / "candidate"
    repo.mkdir()
    candidate.mkdir()
    worktrees = [candidate / name for name in ("first", "second")]
    for path in worktrees:
        path.mkdir()
        (path / "precious").write_bytes(b"keep all work")
    cfg = SimpleNamespace(repo=repo, tmp_root=tmp_path, scratch_root=tmp_path, default_branches=["main"])
    monkeypatch.setattr(worktree_release, "RemovalGuard", _NoGuard)
    report = janitor.JanitorReport([janitor.Candidate(candidate, 0, "tmp")], worktrees, [], [], [])
    info = tmp_path.stat()
    report.ancestor_ids = {tmp_path: (info.st_dev, info.st_ino)}
    mutations, calls = [], []
    monkeypatch.setattr(janitor, "_path_change_reason", lambda *args: None)
    monkeypatch.setattr(janitor, "ignored_worktree_keep_reason", lambda _: None)
    monkeypatch.setattr(janitor, "worktree_branch", lambda _: "feature")
    monkeypatch.setattr(janitor, "is_dirty_worktree", lambda path: gate == "preflight" and path == worktrees[0])
    monkeypatch.setattr(janitor, "remove_plainly", lambda _: mutations.append("remove") or ("removed", None))
    monkeypatch.setattr(janitor, "wip_commit_dirty_worktree", lambda *args, **kwargs: pytest.fail("no WIP is allowed"))
    monkeypatch.setattr(janitor, "_run_git", lambda *args: "")
    monkeypatch.setattr(janitor, "_strict_listing", lambda _: (worktrees, ""))

    def run(argv, **kwargs):
        calls.append(argv)
        late = gate == "before-delete" and len([a for a in calls if "-v" in a]) > 2
        hidden = "-v" in argv and (late or (gate == "preflight" and argv[2] == str(worktrees[1])))
        return subprocess.CompletedProcess(argv, 0, b"h precious\0" if hidden else b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    text = janitor.apply(cfg, report)
    assert "REFUSED (kept)" in text and not mutations
    assert all((path / "precious").read_bytes() == b"keep all work" for path in worktrees)


@pytest.mark.parametrize("late", ["flag", "submodule", "status-flag"])
def test_wip_rechecks_after_successful_commit(tmp_path, monkeypatch, late):
    monkeypatch.setattr(janitor, "worktree_branch", lambda _: "feature")
    monkeypatch.setattr(janitor.shutil, "which", lambda _: "git")
    committed = False
    status_checked = False
    mutations = []
    (tmp_path / "module").mkdir()
    (tmp_path / "module/precious").write_bytes(b"nested work")

    def run(argv, **kwargs):
        nonlocal committed, status_checked
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(argv[2]) + b"\n", b"")
        if "ls-files" in argv:
            data = b""
            if committed and late == "flag" and "-v" in argv:
                data = b"h precious\0"
            if status_checked and late == "status-flag" and "-v" in argv:
                data = b"h precious\0"
            if committed and late == "submodule" and "--stage" in argv:
                data = b"160000 " + b"a" * 40 + b" 0\tmodule\0"
            return subprocess.CompletedProcess(argv, 0, data, b"")
        if "add" in argv or "commit" in argv:
            mutations.append(argv)
            committed |= "commit" in argv
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert "status" in argv
        status_checked = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    result = janitor.wip_commit_dirty_worktree(tmp_path, default_branches=["main"])
    assert result.refused and len(mutations) == 2


def test_direct_wip_keeps_unpopulated_submodule_before_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor.shutil, "which", lambda _: "git")
    (tmp_path / "module").mkdir()
    (tmp_path / "module/precious").write_bytes(b"unrecognized nested work")
    git_reads(monkeypatch, stage=b"160000 " + b"a" * 40 + b" 0\tmodule\0")
    monkeypatch.setattr(janitor, "worktree_branch", lambda _: "feature")
    result = janitor.wip_commit_dirty_worktree(tmp_path, default_branches=["main"])
    assert result.refused and "materialized submodule" in result.message
