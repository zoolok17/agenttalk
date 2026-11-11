"""Real local remotes and checkouts; every possible deletion is inside tmp_path."""
import contextlib
import json
import os
import shutil
import subprocess
import threading
import types
from pathlib import Path

import pytest

from agenttalk import janitor, lane_occupancy, worktree_release
from agenttalk.store import Store


def git(repo, *args):
    result = subprocess.run([shutil.which("git"), "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def estate(tmp_path, monkeypatch, request):
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    # resolve /var on macOS only in the fixture, never in the command under test.
    base = tmp_path.resolve()
    repo = base / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "gc.auto", "0")
    if hasattr(request, "param"):
        git(repo, "config", "core.autocrlf", request.param)
    (repo / "source.txt").write_text("original\n")
    (repo / ".gitignore").write_text(".agenttalk/\n.worktrees/\n.env\n*.db\n__pycache__/\n.pytest_cache/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    remote = base / "remote.git"
    git(repo, "clone", "--bare", str(repo), str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    scratch = base / "scratch-parent" / "scratch"
    scratch.mkdir(parents=True)
    temp = base / "temp"
    temp.mkdir()
    (repo / ".agenttalk").mkdir()
    (repo / ".agenttalk/config.json").write_text(json.dumps({
        "scratch": {"root": str(scratch), "tmp_root": str(temp)}}))
    wt = scratch / "done"
    git(repo, "worktree", "add", "-b", "finished", str(wt))
    return repo, wt, janitor.JanitorConfig.load(repo)


def run(cfg, target):
    from agenttalk.worktree_release import release
    return release(cfg, target)


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_acknowledged_damage_allows_an_unrelated_checkout_release(estate, field):

    from agenttalk import cli, lanes

    from test_lanes import _lane

    repo, wt, cfg = estate

    store = Store(repo)

    config = json.loads(store.config_path.read_text())

    config["agents"] = ["lead", "dev"]

    store.config_path.write_text(json.dumps(config))

    row = _lane(lane_id="retired", worktree_path=str(repo / "other"), worktree_toplevel_canonical=str(repo / "other"))

    row[field] = "\t"

    lanes.save_lanes(store, {"schema_version": 1, "lanes": {"retired": row}})

    assert cli.main(["--root", str(repo), "lane", "abandon", "--id", "retired", "--acknowledge-missing-worktree"]) == 0

    before = lanes.lanes_path(store).read_bytes()

    code, text = run(cfg, wt)

    assert code == 0 and "REMOVED" in text, text

    assert not wt.exists()

    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_acknowledged_damage_preserves_surviving_path_and_occupancy(estate, monkeypatch, field):

    from agenttalk import lanes

    from test_lanes import _lane

    repo, wt, cfg = estate

    row = _lane(
        lane_id="retired",
        status=lanes.STATUS_ABANDONED,
        worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED,
        worktree_path=str(wt),
        worktree_toplevel_canonical=str(wt),
    )

    row[field] = "\t"

    monkeypatch.setattr(worktree_release, "_activity", lambda *_: {"retired": row})

    claims = []

    assert worktree_release._free_lanes(cfg, wt, lambda _, lane_id: claims.append(lane_id)) == ["retired"]

    assert claims == ["retired"]

    with pytest.raises(worktree_release.Refused, match="in use"):
        worktree_release._free_lanes(cfg, wt, lambda *_: lane_occupancy.IN_USE)


def directory_link(link, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("case,reason", [
    ("dirty", "uncommitted"), ("untracked", "untracked"),
    ("ignored", "ignored"), ("store", ".agenttalk"),
    ("unmerged", "ancestor"), ("squash", "squash"), ("local-only-merge", "ancestor"),
    ("link", "link"), ("unregistered", "registered"),
    ("outside", "scanned"), ("lane", "lane"),
    ("launch", "launch"), ("corrupt", "read"),
])
def test_release_refusals_keep_checkout(estate, tmp_path, case, reason):
    repo, wt, cfg = estate
    state = repo / ".agenttalk/state"
    state.mkdir()
    link = None
    if case == "dirty":
        (wt / "source.txt").write_text("unfinished")
    elif case == "untracked":
        (wt / "new.txt").write_text("keep")
    elif case == "ignored":
        (wt / "work.db").write_text("keep")
    elif case == "store":
        (wt / ".agenttalk").mkdir()
        (wt / ".agenttalk/evidence").write_text("keep")
    elif case in {"unmerged", "squash", "local-only-merge"}:
        (wt / "source.txt").write_text("new work")
        git(wt, "commit", "-qam", "feature")
        if case == "squash":
            git(repo, "merge", "--squash", "finished")
            git(repo, "commit", "-qm", "squashed")
            git(repo, "push", "origin", "main")
        elif case == "local-only-merge":
            git(repo, "merge", "--ff-only", "finished")
    elif case == "link":
        target = tmp_path / "sentinel"
        target.mkdir()
        (target / "keep").write_text("keep")
        cache = wt / "__pycache__"
        cache.mkdir()
        link = cache / "linked"
        directory_link(link, target)
        assert git(wt, "status", "--porcelain", "--untracked-files=all") == ""
    elif case == "unregistered":
        wt = cfg.scratch_root / "ordinary"
        wt.mkdir()
        (wt / "source.txt").write_text("keep")
    elif case == "outside":
        wt = tmp_path / "outside"
        git(repo, "worktree", "add", "-b", "outside", str(wt))
    elif case == "lane":
        (state / "lanes.json").write_text(json.dumps({"lanes": {"one": {
            "status": "active", "worktree_path": str(wt)}}}))
    elif case == "launch":
        (state / "supervisor-state.json").write_text(json.dumps({"agents": {
            "seat": {"cwd": str(wt / "subdir")}}}))
    elif case == "corrupt":
        (state / "lanes.json").write_text("broken")
    before_head = git(repo, "rev-parse", "refs/heads/finished")
    try:
        code, text = run(cfg, wt)
        assert code != 0 and reason in text.lower(), text
        assert wt.is_dir() and (wt / "source.txt").exists()
        assert git(repo, "rev-parse", "refs/heads/finished") == before_head, "refusal must never WIP-commit"
    finally:
        if link is not None:
            link.rmdir() if os.name == "nt" else link.unlink()


def test_release_fetches_default_and_preserves_branch_and_commits(estate):
    repo, wt, cfg = estate
    (wt / "source.txt").write_text("finished work")
    git(wt, "commit", "-qam", "finished")
    head = git(wt, "rev-parse", "HEAD")
    git(repo, "push", "origin", "finished:main")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__/source.pyc").write_bytes(b"cache")
    code, text = run(cfg, wt)
    assert code == 0 and "REMOVED" in text, text
    assert not wt.exists()
    assert git(repo, "rev-parse", "finished") == head
    assert git(repo, "show", f"{head}:source.txt") == "finished work"
    assert str(wt).replace("\\", "/") not in git(repo, "worktree", "list", "--porcelain")


def test_report_lists_eligible_size_without_removing(estate):
    repo, wt, cfg = estate
    code, text = run(cfg, None)
    assert code == 0 and "WOULD RELEASE" in text and "bytes" in text and str(wt) in text
    assert wt.is_dir() and (wt / "source.txt").exists()
    assert "KEPT" in text  # main checkout is never a release target


def test_report_lists_both_and_release_removes_only_named_checkout(estate):
    repo, wt, cfg = estate
    second = cfg.scratch_root / "second"
    git(repo, "worktree", "add", "-b", "second", str(second))
    code, text = run(cfg, None)
    assert code == 0 and text.count("WOULD RELEASE") == 2 and str(second) in text
    assert run(cfg, wt)[0] == 0
    assert not wt.exists() and (second / "source.txt").exists()


@pytest.mark.parametrize("protected", [".agenttalk", ".env"])
def test_protected_data_inside_known_cache_is_kept(estate, protected):
    _, wt, cfg = estate
    cache = wt / "__pycache__"
    cache.mkdir()
    (cache / protected).write_text("keep")
    code, text = run(cfg, wt)
    assert code != 0 and protected in text
    assert (cache / protected).read_text() == "keep"


@pytest.mark.parametrize("swapped_folder", ["parent", "root"])
def test_parent_swap_after_check_refuses_before_git_remove(estate, tmp_path, monkeypatch, swapped_folder):
    from agenttalk import worktree_release as release_mod
    repo, wt, cfg = estate
    original = release_mod._check_worktree
    saved = tmp_path / "saved"
    decoy = tmp_path / "decoy"
    swapped_path = cfg.scratch_root.parent if swapped_folder == "parent" else cfg.scratch_root
    relative = wt.relative_to(swapped_path)
    (decoy / relative).mkdir(parents=True)
    sentinel = decoy / relative / "source.txt"
    sentinel.write_text("unrelated")
    swapped = False

    def swap(*args, **kwargs):
        nonlocal swapped
        result = original(*args, **kwargs)
        if not swapped:
            swapped_path.rename(saved)
            directory_link(swapped_path, decoy)
            swapped = True
        return result

    monkeypatch.setattr(release_mod, "_check_worktree", swap)
    try:
        code, text = run(cfg, wt)
        assert code != 0 and ("ancestor" in text or "link" in text), text
        assert sentinel.read_text() == "unrelated"
        assert (saved / relative / "source.txt").exists()
    finally:
        if swapped:
            swapped_path.rmdir() if os.name == "nt" else swapped_path.unlink()


def test_cli_release_flags_and_report(estate, capsys):
    from agenttalk.cli import main
    repo, wt, _ = estate
    assert main(["--root", str(repo), "janitor", "--release-report"]) == 0
    assert "WOULD RELEASE" in capsys.readouterr().out
    assert main(["--root", str(repo), "janitor", "--release", str(wt)]) == 0
    assert not wt.exists()


@pytest.mark.parametrize("case", ["main", "fetch-failure", "configured-launch", "queued-launch", "environment-file",
                                  "locked-registration", "missing-pointer"])
def test_additional_refusals(estate, case):
    repo, wt, cfg = estate
    if case == "main":
        wt = repo
    elif case == "fetch-failure":
        git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    elif case == "configured-launch":
        (repo / ".agenttalk/supervisor.json").write_text(json.dumps({"agents": {"seat": {"cwd": str(wt)}}}))
    elif case == "queued-launch":
        requests = repo / ".agenttalk/state/launch-requests"
        requests.mkdir(parents=True)
        (requests / "one.json").write_text(json.dumps({"state": "queued", "workspace_path": str(wt)}))
    elif case == "locked-registration":
        git(repo, "worktree", "lock", str(wt))
    elif case == "missing-pointer":
        (wt / ".git").rename(wt / "saved-pointer")
    else:
        (wt / ".env").write_text("private test data")
    code, text = run(cfg, wt)
    assert code != 0 and ("KEPT" in text or "FAILED" in text), text
    assert (wt / "source.txt").exists()


def test_report_does_not_offer_locked_or_dirty_checkout(estate):
    repo, wt, cfg = estate
    git(repo, "worktree", "lock", str(wt))
    code, text = run(cfg, None)
    assert code == 0 and "WOULD RELEASE" not in text and "locked" in text
    git(repo, "worktree", "unlock", str(wt))
    (wt / "source.txt").write_text("unfinished")
    code, text = run(cfg, None)
    assert code == 0 and "WOULD RELEASE" not in text and "uncommitted" in text


def test_git_environment_override_is_refused(estate, monkeypatch):
    repo, wt, cfg = estate
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    code, text = run(cfg, wt)
    assert code != 0 and "GIT_DIR" in text
    assert (wt / "source.txt").exists()


def test_release_from_secondary_checkout_refuses_missing_main_activity(estate):
    _, wt, cfg = estate
    import dataclasses
    code, text = run(dataclasses.replace(cfg, repo=wt), wt)
    assert code != 0 and "main repository" in text
    assert (wt / "source.txt").exists()


def test_unreadable_launch_directory_is_not_treated_as_empty(estate, monkeypatch):
    repo, wt, cfg = estate
    requests = repo / ".agenttalk/state/launch-requests"
    requests.mkdir(parents=True)
    original = Path.iterdir

    def denied(path):
        if path == requests:
            raise PermissionError("launch directory cannot be read")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    code, text = run(cfg, wt)
    assert code != 0 and "cannot be read" in text
    assert (wt / "source.txt").exists()


def test_git_remove_failure_never_forces_or_deletes_files(estate, monkeypatch):
    from agenttalk import worktree_release as release_mod
    _, wt, cfg = estate
    original = release_mod._git
    removals = []

    def refused(repo, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            removals.append(args)
            return 1, ""
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(release_mod, "_git", refused)
    code, text = run(cfg, wt)
    assert code != 0 and "KEPT" in text and "failed" in text
    assert removals == [("worktree", "remove", "--", str(wt))]
    assert (wt / "source.txt").exists()


def test_existing_parent_link_is_refused(estate, tmp_path):
    _, wt, cfg = estate
    link = tmp_path / "alias"
    directory_link(link, cfg.scratch_root)
    try:
        code, text = run(cfg, link / wt.name)
        assert code != 0 and "link" in text, text
        assert (wt / "source.txt").exists()
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing behavior; links above also run on POSIX")
def test_open_windows_file_keeps_all_checkout_files(estate):
    _, wt, cfg = estate
    before = sorted(p.name for p in wt.iterdir())
    with (wt / "source.txt").open("r"):
        code, text = run(cfg, wt)
    assert code != 0 and "Windows refused" in text, text
    assert sorted(p.name for p in wt.iterdir()) == before


def test_new_untracked_file_after_check_is_kept(estate, monkeypatch):
    from agenttalk import worktree_release as release_mod
    _, wt, cfg = estate
    original = release_mod._check_worktree

    def add_file(*args, **kwargs):
        result = original(*args, **kwargs)
        (wt / "late.txt").write_text("work started again")
        return result

    monkeypatch.setattr(release_mod, "_check_worktree", add_file)
    code, text = run(cfg, wt)
    assert code != 0 and "untracked" in text, text
    assert (wt / "late.txt").read_text() == "work started again"


def test_missing_identity_recheck_mutant_is_detected(estate, tmp_path, monkeypatch):
    """Seed the named fault, prove the ordinary parent-swap assertion catches it.

    The fake remove only records that the unsafe Git boundary was reached. Even
    deliberately broken code never removes the decoy or any other real folder.
    """
    from agenttalk import worktree_release as release_mod
    _, wt, cfg = estate
    original_check = release_mod._check_worktree
    original_git = release_mod._git
    original_unchanged = release_mod._unchanged
    saved = tmp_path / "saved-mutant"
    decoy = tmp_path / "decoy-mutant"
    (decoy / wt.name).mkdir(parents=True)
    checks = 0
    swapped = False
    reached_remove = []

    def swap(*args):
        nonlocal checks, swapped
        result = original_check(*args)
        checks += 1
        if checks == 2:  # after the last eligibility check, immediately before removal
            cfg.scratch_root.rename(saved)
            directory_link(cfg.scratch_root, decoy)
            swapped = True
        return result

    def fake_remove(repo, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            reached_remove.append(True)
            return 0, ""
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(release_mod, "_check_worktree", swap)
    monkeypatch.setattr(release_mod, "_git", fake_remove)
    monkeypatch.setattr(release_mod, "_unchanged", lambda ids: None)
    try:
        code, _ = run(cfg, wt)
        with pytest.raises(AssertionError, match="identity guard"):
            assert code != 0 and not reached_remove, "identity guard did not stop removal"
        assert reached_remove
        cfg.scratch_root.rmdir() if os.name == "nt" else cfg.scratch_root.unlink()
        saved.rename(cfg.scratch_root)
        swapped = False
        checks = 0
        reached_remove.clear()
        monkeypatch.setattr(release_mod, "_unchanged", original_unchanged)
        code, text = run(cfg, wt)
        assert code != 0 and "link" in text and not reached_remove
    finally:
        if swapped:
            cfg.scratch_root.rmdir() if os.name == "nt" else cfg.scratch_root.unlink()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_changes_are_kept(estate, flag):
    _, wt, cfg = estate
    git(wt, "update-index", flag, "source.txt")
    (wt / "source.txt").write_text("unfinished hidden work")
    assert git(wt, "status", "--porcelain") == "", "reproduce the misleading clean status"
    code, text = run(cfg, wt)
    assert code != 0 and flag[2:] in text, text
    assert (wt / "source.txt").read_text() == "unfinished hidden work"


@pytest.mark.parametrize("state_directory", [False, True])
def test_official_supervisor_state_keeps_checkout(estate, state_directory):
    repo, wt, cfg = estate
    if state_directory:
        (repo / ".agenttalk/state").mkdir()
    (repo / ".agenttalk/supervisor-state.json").write_text(json.dumps({
        "agents": {"seat": {"cwd": str(wt)}}}))
    code, text = run(cfg, wt)
    assert code != 0 and "launch record" in text, text
    assert (wt / "source.txt").exists()


@pytest.mark.parametrize("field", ["cwd", "launch_cwd", "workspace_path", "worktree_path"])
def test_relative_activity_path_is_ambiguous(estate, field):
    repo, wt, cfg = estate
    (repo / ".agenttalk/supervisor.json").write_text(json.dumps({
        "agents": {"seat": {field: "seat/task"}}}))
    code, text = run(cfg, wt)
    assert code != 0 and "relative" in text and "ambiguous" in text, text
    assert (wt / "source.txt").exists()


def test_replacement_commit_cannot_fake_remote_ancestry(estate, monkeypatch):
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    repo, wt, cfg = estate
    base = git(repo, "rev-parse", "main")
    (wt / "source.txt").write_text("not merged remotely")
    git(wt, "commit", "-qam", "unmerged")
    unmerged = git(wt, "rev-parse", "HEAD")
    git(repo, "replace", "--graft", base, unmerged)
    assert git(repo, "merge-base", "--is-ancestor", unmerged, base) == ""
    code, text = run(cfg, wt)
    assert code != 0 and "ancestor" in text, text
    assert (wt / "source.txt").read_text() == "not merged remotely"


def test_head_moves_between_checks_keeps_checkout(estate, monkeypatch):
    from agenttalk import worktree_release as release_mod
    repo, wt, cfg = estate
    base = git(wt, "rev-parse", "HEAD")
    (wt / "source.txt").write_text("merged work")
    git(wt, "commit", "-qam", "merged")
    git(wt, "push", "origin", "finished:main")
    original = release_mod._check_worktree
    checks = []

    def move_head(*args):
        result = original(*args)
        checks.append(result[1])
        if len(checks) == 1:
            git(wt, "checkout", "--detach", base)
        return result

    monkeypatch.setattr(release_mod, "_check_worktree", move_head)
    code, text = run(cfg, wt)
    assert len(checks) == 2 and checks[0] != checks[1], checks
    assert code != 0 and "HEAD changed" in text, text
    assert (wt / "source.txt").read_text() == "original\n"


@pytest.mark.parametrize("reason", ["unmerged", "dirty", "launch", "locked", "main", "unregistered"])
def test_cheap_refusal_does_not_walk_checkout(estate, monkeypatch, reason):
    from agenttalk import worktree_release as release_mod
    repo, wt, cfg = estate
    if reason == "unmerged":
        (wt / "source.txt").write_text("unmerged")
        git(wt, "commit", "-qam", "unmerged")
    elif reason == "dirty":
        (wt / "source.txt").write_text("unfinished")
    elif reason == "launch":
        (repo / ".agenttalk/supervisor.json").write_text(json.dumps({"cwd": str(wt)}))
    elif reason == "locked":
        git(repo, "worktree", "lock", str(wt))
    elif reason == "main":
        wt = repo
    else:
        wt = cfg.scratch_root / "ordinary"
        wt.mkdir()

    def walked(path):
        pytest.fail("walked a checkout that a cheap check should refuse")

    monkeypatch.setattr(release_mod, "_tree_size", walked)
    code, text = run(cfg, wt)
    assert code != 0 and "KEPT" in text, text
    assert wt.exists()


@pytest.mark.parametrize("metadata", [".git", ".GIT"])
def test_nested_repository_in_cache_is_kept(estate, metadata):
    _, wt, cfg = estate
    nested = wt / "__pycache__" / "nested"
    nested.mkdir(parents=True)
    git(nested, "init")
    git(nested, "config", "user.name", "Test Author")
    git(nested, "config", "user.email", "test@example.invalid")
    (nested / "work.txt").write_text("unpushed work")
    git(nested, "add", ".")
    git(nested, "commit", "-qm", "only copy")
    head = git(nested, "rev-parse", "HEAD")
    if metadata != ".git":
        (nested / ".git").rename(nested / "metadata-temp")
        (nested / "metadata-temp").rename(nested / metadata)
    code, text = run(cfg, wt)
    assert code != 0 and "nested repository" in text, text
    assert (nested / "work.txt").read_text() == "unpushed work"
    assert git(nested, f"--git-dir={nested / metadata}", "rev-parse", "HEAD") == head


@pytest.mark.parametrize("when", ["before-checks", "before-remove"])
def test_stale_fsmonitor_cannot_hide_edits(estate, tmp_path, monkeypatch, when):
    from agenttalk import worktree_release as release_mod
    repo, wt, cfg = estate
    hook = tmp_path / "stale-monitor.sh"
    hook.write_bytes(b"#!/bin/sh\nprintf 'unchanged-token\\0'\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", hook.as_posix())
    git(repo, "config", "core.untrackedCache", "true")
    for _ in range(2):
        git(wt, "status", "--porcelain")
    def edit():
        (wt / "source.txt").write_text("edit hidden by stale monitor\n")
        assert git(wt, "status", "--porcelain") == "", "the real hook must hide the edit"

    if when == "before-checks":
        edit()
    else:
        original = release_mod._check_worktree
        checks = 0

        def late_edit(*args):
            nonlocal checks
            result = original(*args)
            checks += 1
            if checks == 2:
                edit()
            return result

        monkeypatch.setattr(release_mod, "_check_worktree", late_edit)
    code, text = run(cfg, wt)
    reason = "uncommitted" if when == "before-checks" else "git worktree failed"
    assert code != 0 and reason in text, text
    assert (wt / "source.txt").read_text() == "edit hidden by stale monitor\n"


@pytest.mark.parametrize("report", [False, True])
def test_unrelated_relative_lanes_do_not_block_release(estate, report):
    repo, wt, cfg = estate
    state = repo / ".agenttalk/state"
    state.mkdir()
    # Synthetic shape from the review: five relative lanes (three active),
    # three absolute active lanes. No desktop records are read by this test.
    lanes = {f"relative-{i}": {"status": "active" if i < 3 else "abandoned",
                              "worktree_path": f".worktrees/other-{i}"} for i in range(5)}
    lanes.update({f"absolute-{i}": {"status": "active",
                                  "worktree_path": str(repo / ".worktrees" / f"absolute-{i}")}
                  for i in range(3)})
    (state / "lanes.json").write_text(json.dumps({"lanes": lanes}))
    code, text = run(cfg, None if report else wt)
    assert code == 0 and ("WOULD RELEASE" if report else "REMOVED") in text, text
    assert wt.exists() == report


@pytest.mark.parametrize("binding", ["repo", "cwd", "canonical", "launch-lane"])
def test_relative_lane_still_protects_its_checkout(estate, tmp_path, monkeypatch, binding):
    repo, wt, cfg = estate
    state = repo / ".agenttalk/state"
    state.mkdir()
    row = {"status": "active", "worktree_path": os.path.relpath(wt, repo)}
    if binding == "cwd":
        monkeypatch.chdir(tmp_path)
        row["worktree_path"] = os.path.relpath(wt, tmp_path)
    elif binding == "canonical":
        row["worktree_path"] = "old-relative-spelling"
        row["worktree_toplevel_canonical"] = str(wt)
    elif binding == "launch-lane":
        row["status"] = "delivered"
        requests = state / "launch-requests"
        requests.mkdir()
        (requests / "request.json").write_text(json.dumps({"state": "pending", "lane_id": "held"}))
    (state / "lanes.json").write_text(json.dumps({"lanes": {"held": row}}))
    code, text = run(cfg, wt)
    assert code != 0 and "held" in text and "lanes.json" in text, text
    assert (wt / "source.txt").exists()


def test_bare_repository_in_cache_is_kept(estate):
    repo, wt, cfg = estate
    bare = wt / "__pycache__" / "backup.git"
    bare.parent.mkdir()
    git(repo, "clone", "--bare", str(repo), str(bare))
    # The only ref to this commit is in the nested bare repository.
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    git(bare, "config", "user.name", "Test Author")
    git(bare, "config", "user.email", "test@example.invalid")
    head = git(bare, "commit-tree", tree, "-m", "only nested copy")
    git(bare, "update-ref", "refs/heads/only-here", head)
    code, text = run(cfg, wt)
    assert code != 0 and "repository" in text, text
    assert git(bare, "rev-parse", "refs/heads/only-here") == head


@pytest.mark.parametrize("settings", [[], ["core.checkStat", "minimal"],
                                     ["core.trustctime", "false"], ["both"]])
def test_stat_identical_edit_is_kept(estate, settings):
    repo, wt, cfg = estate
    if settings == ["both"]:
        git(repo, "config", "core.checkStat", "minimal")
        git(repo, "config", "core.trustctime", "false")
    elif settings:
        git(repo, "config", *settings)
    source = wt / "source.txt"
    # Make the cached time older than the index without sleeps or a racy index.
    os.utime(source, (1_600_000_000, 1_600_000_000))
    git(wt, "update-index", "--refresh")
    before = source.stat()
    changed = b"X" + source.read_bytes()[1:]
    source.write_bytes(changed)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    if settings == ["both"]:
        assert git(wt, "status", "--porcelain") == "", "reproduce the misleading stat cache"
    code, text = run(cfg, wt)
    assert code != 0 and ("content" in text or "uncommitted" in text), text
    assert source.read_bytes() == changed


@pytest.mark.parametrize("estate", ["false", "true", "input"], indirect=True)
@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "edited"])
def test_crlf_tracked_content_release(estate, dirty):
    repo, wt, cfg = estate
    (wt / "source.txt").write_bytes(b"original\n")
    (wt / ".gitattributes").write_text("source.txt text eol=crlf\n")
    git(wt, "add", ".gitattributes")
    git(wt, "add", "--renormalize", "source.txt")
    git(wt, "commit", "-qm", "line ending policy")
    git(repo, "fetch", "origin")
    git(wt, "push", "origin", "finished:main")
    # Have Git write the checkout bytes and their index stat data together.
    # A manual LF-to-CRLF rewrite can leave a stat-dirty, content-clean fixture.
    (wt / "source.txt").unlink()
    git(wt, "checkout-index", "--force", "--index", "--", "source.txt")
    assert git(wt, "status", "--porcelain", "--untracked-files=all") == ""
    assert (wt / "source.txt").read_bytes() == b"original\r\n"
    if dirty:
        (wt / "source.txt").write_bytes(b"modified\r\n")
    code, text = run(cfg, wt)
    if dirty:
        assert code != 0 and "uncommitted" in text, text
        assert (wt / "source.txt").read_bytes() == b"modified\r\n"
    else:
        assert code == 0 and "REMOVED" in text, text


def _commit_exact(repo, wt, files, autocrlf):
    """Commit these exact bytes (no conversion), publish them, then turn the line-ending setting on."""
    for name, data in files.items():
        (wt / name).write_bytes(data)
    git(wt, "-c", "core.autocrlf=false", "add", "--", *files)
    git(wt, "commit", "-qm", "exact bytes")
    git(repo, "fetch", "origin")
    git(wt, "push", "origin", "finished:main")
    git(repo, "config", "core.autocrlf", autocrlf)
    assert git(wt, "status", "--porcelain", "--untracked-files=all") == ""


def _same_size_same_time_edit(wt, path):
    """Change a file without changing its size or time, so Git's status cannot see it."""
    # On Linux and macOS the rewrite moves the file's ctime, and with the default core.trustctime=true a second
    # boundary between the refresh and the rewrite would make status see the edit before the code under test runs.
    git(wt, "config", "core.trustctime", "false")
    os.utime(path, (1_600_000_000, 1_600_000_000))
    git(wt, "update-index", "--refresh")
    before = path.stat()
    changed = b"X" + path.read_bytes()[1:]
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert git(wt, "status", "--porcelain") == "", "reproduce the misleading stat cache"
    return changed


@pytest.mark.parametrize("setting", ["true", "input"])
@pytest.mark.parametrize("data", [b"one\r\ntwo\r\n", b"one\r\ntwo\nthree\r\n"], ids=["crlf", "mixed"])
def test_a_clean_worktree_with_a_committed_crlf_file_is_released(estate, setting, data):
    repo, wt, cfg = estate
    _commit_exact(repo, wt, {"crlf.txt": data}, setting)
    code, text = run(cfg, wt)
    assert code == 0 and "REMOVED" in text, text


@pytest.mark.parametrize("setting", ["true", "input"])
@pytest.mark.parametrize(
    "rules", [b"* text=auto\r\n", b"source.txt text eol=crlf\r\n"], ids=["rule-for-all-guard", "rule-for-other-file"]
)
def test_a_gitattributes_committed_with_crlf_is_released(estate, rules, setting):
    # the second rule leaves .gitattributes itself out of the conversion: kept by master, released now
    repo, wt, cfg = estate
    _commit_exact(repo, wt, {".gitattributes": rules}, setting)
    code, text = run(cfg, wt)
    assert code == 0 and "REMOVED" in text, text


@pytest.mark.parametrize("edit", ["append", "same-size-same-time"])
def test_an_edit_to_a_committed_crlf_file_is_kept(estate, edit):
    repo, wt, cfg = estate
    _commit_exact(repo, wt, {"crlf.txt": b"one\r\ntwo\r\n"}, "true")
    path = wt / "crlf.txt"
    if edit == "append":
        changed = path.read_bytes() + b"three\r\n"
        path.write_bytes(changed)
    else:
        changed = _same_size_same_time_edit(wt, path)
    code, text = run(cfg, wt)
    assert code != 0 and ("uncommitted" if edit == "append" else "content differs") in text, text
    assert path.read_bytes() == changed


@pytest.mark.skipif(os.name == "nt", reason="a Windows file name cannot hold a carriage return or a line feed")
@pytest.mark.parametrize("name", ["odd\nname.txt", "odd\rname.txt", "odd\r"])
@pytest.mark.parametrize("edit", [False, True], ids=["clean", "edited"])
def test_a_tracked_name_holding_a_line_end_is_checked_one_file_at_a_time(estate, name, edit):
    repo, wt, cfg = estate
    _commit_exact(repo, wt, {name: b"one\ntwo\n"}, "false")
    changed = _same_size_same_time_edit(wt, wt / name) if edit else None
    code, text = run(cfg, wt)
    if edit:
        assert code != 0 and "content differs" in text and (wt / name).read_bytes() == changed, text
    else:
        assert code == 0 and "REMOVED" in text, text


@pytest.mark.parametrize("name", ['"source.txt"', '"./source.txt"'])
@pytest.mark.parametrize("flags", [(), ("--no-filters",)], ids=["converted", "raw"])
def test_a_name_starting_with_a_quote_is_never_read_as_a_different_file(estate, name, flags):
    # Git reads an input line that starts with a double quote as a quoted path and would hash source.txt instead;
    # the literal name does not exist here, so the one-file call must be the one that answers, with a refusal.
    _, wt, _ = estate
    with pytest.raises(worktree_release.Refused):
        worktree_release._hash_files(wt, [name], {name: 0}, frozenset(), *flags)


def test_the_full_check_refuses_a_quoted_name_that_is_not_on_disk(estate, monkeypatch):
    _, wt, _ = estate
    blob = git(wt, "rev-parse", "HEAD:source.txt")
    real = worktree_release._git

    def listing(repo, *args, **kwargs):
        if args[:3] == ("ls-files", "--stage", "-z"):
            return 0, f'100644 {blob} 0\t"source.txt"\0'
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(worktree_release, "_git", listing)
    with pytest.raises(worktree_release.Refused):
        worktree_release._check_tracked_content(wt)


@pytest.mark.skipif(os.name == "nt", reason="a Windows file name cannot hold a double quote")
def test_an_edit_to_a_file_whose_name_starts_with_a_quote_is_kept(estate):
    repo, wt, cfg = estate  # '"source.txt"' holds the same bytes as source.txt: the wrong file would hash the same
    _commit_exact(repo, wt, {'"source.txt"': b"original\n"}, "false")
    assert git(wt, "rev-parse", 'HEAD:"source.txt"') == git(wt, "rev-parse", "HEAD:source.txt")
    changed = _same_size_same_time_edit(wt, wt / '"source.txt"')
    code, text = run(cfg, wt)
    assert code != 0 and "content differs" in text and (wt / '"source.txt"').read_bytes() == changed, text


def test_a_hash_answer_that_does_not_match_the_names_sent_refuses(estate, monkeypatch):
    repo, wt, cfg = estate
    real = worktree_release._git

    def short(repo, *args, **kwargs):
        return (0, "") if "--stdin-paths" in args else real(repo, *args, **kwargs)

    monkeypatch.setattr(worktree_release, "_git", short)
    with pytest.raises(worktree_release.Refused, match="answer"):
        worktree_release._check_tracked_content(wt)


def test_git_untracked_cache_override_wins_over_repository_config(estate):
    from agenttalk import worktree_release as release_mod
    repo, wt, _ = estate
    git(repo, "config", "core.untrackedCache", "true")
    assert git(wt, "config", "--bool", "core.untrackedCache") == "true"
    # Ask Git for its effective setting through the same boundary used by
    # checks and removal. This pins the cache override independently of fsmonitor.
    assert release_mod._read_git(wt, "config", "--bool", "core.untrackedCache") == "false"
    assert git(wt, "config", "--bool", "core.untrackedCache") == "true", "do not edit user config"


def test_a_path_read_from_git_keeps_every_character_but_the_line_end(tmp_path):
    # Round 17, automated comment 4236579571: the fence check trimmed the spaces from the
    # address Git gave for origin. A path is now read as Git wrote it, line end removed only:
    # spaces at either end stay, and a carriage return is not turned into a line end.
    from agenttalk import worktree_release as release_mod
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    value = " a\rb "
    git(repo, "config", "test.value", value)
    assert release_mod._read_git_path(repo, "config", "--get", "test.value") == value


def _finished_lane(repo, wt, status):
    state = repo / ".agenttalk/state"
    state.mkdir(exist_ok=True)
    (state / "lanes.json").write_text(json.dumps({"lanes": {"one": {"status": status, "worktree_path": str(wt)}}}))


@contextlib.contextmanager
def _session_on_lane(repo):
    """The wrapper's side of the token, held for the block."""
    lane_occupancy.hold(Store(repo), "one")
    try:
        yield
    finally:
        lane_occupancy._close(lane_occupancy._HELD.pop("one"))


@pytest.mark.parametrize("status", ["delivered", "abandoned"])
def test_release_keeps_a_checkout_while_its_lane_token_is_held(estate, status):
    repo, wt, cfg = estate
    _finished_lane(repo, wt, status)

    with _session_on_lane(repo):
        code, text = run(cfg, wt)

    assert code == 1 and lane_occupancy.refusal("one", lane_occupancy.IN_USE) in text, text
    assert (wt / "source.txt").exists()
    code, text = run(cfg, wt)  # the session ended, and nothing of the refusal is left behind
    assert code == 0 and "REMOVED" in text, text
    assert not wt.exists()


@pytest.mark.parametrize("status", ["delivered", "abandoned"])
def test_release_keeps_a_checkout_whose_lane_token_cannot_be_checked(estate, status):
    repo, wt, cfg = estate
    _finished_lane(repo, wt, status)
    (repo / ".agenttalk/lane-occupancy/lane-one.token").mkdir(parents=True)  # not a plain file: never read as free

    code, text = run(cfg, wt)

    assert code == 1 and lane_occupancy.UNKNOWN in text, text
    assert (wt / "source.txt").exists()


def test_release_report_does_not_offer_a_checkout_whose_lane_is_in_use(estate):
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")

    with _session_on_lane(repo):
        code, text = run(cfg, None)

    assert code == 0 and f"WOULD RELEASE {wt}" not in text
    assert lane_occupancy.refusal("one", lane_occupancy.IN_USE) in text
    assert wt.exists()


def test_release_report_does_not_offer_a_checkout_whose_token_folder_cannot_be_checked(estate):
    """R5-01: a token folder that is not a plain folder makes the lane's state unknown. The report must say so, as the
    release does, and must not replace or add anything."""
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")
    damaged = repo / ".agenttalk/lane-occupancy"
    damaged.write_text("not a folder")

    code, text = run(cfg, None)

    assert code == 0 and f"WOULD RELEASE {wt}" not in text and lane_occupancy.UNKNOWN in text, text
    assert damaged.read_text() == "not a folder"
    code, text = run(cfg, wt)
    assert code == 1 and lane_occupancy.UNKNOWN in text, text
    assert (wt / "source.txt").exists()


@pytest.mark.parametrize("name", ["lane-occupancy", "lane-one.token"], ids=["the folder", "the token"])
def test_release_report_does_not_offer_a_checkout_whose_token_cannot_be_looked_at(estate, monkeypatch, name):
    """R6-01: a look the system refuses is not a missing token. The fake raises, as a real permission error would, and
    `os.path.lexists` hides that as "absent", as the standard library does."""
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")
    (repo / ".agenttalk/lane-occupancy").mkdir()
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if os.fspath(path).endswith(name):
            raise PermissionError(13, "denied", os.fspath(path))
        return real(path, *args, **kwargs)

    def lexists(path):
        try:
            lstat(path)
        except OSError:
            return False
        return True

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os.path, "lexists", lexists)

    code, text = run(cfg, None)

    assert code == 0 and f"WOULD RELEASE {wt}" not in text and lane_occupancy.UNKNOWN in text, text
    assert wt.exists()


def test_release_report_creates_no_token(estate):
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")

    code, text = run(cfg, None)

    assert code == 0 and f"WOULD RELEASE {wt}" in text, text
    assert not (repo / ".agenttalk/lane-occupancy").exists()


def test_a_session_that_starts_during_release_is_refused_until_the_checkout_is_gone(estate, monkeypatch):
    """Release first: the claim is taken before the last check and kept through the removal."""
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")
    store, real, refused = Store(repo), worktree_release._read_git, []

    def read_git(where, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            with pytest.raises(lane_occupancy.LaneOccupied):
                lane_occupancy.hold(store, "one")
            refused.append(True)
        return real(where, *args, **kwargs)

    monkeypatch.setattr(worktree_release, "_read_git", read_git)

    code, text = run(cfg, wt)

    assert code == 0 and refused == [True], text
    assert lane_occupancy.peek(store, "one") is None  # let go of when the removal is over


def test_release_refuses_when_the_lane_changes_after_its_last_full_check(estate, monkeypatch):
    """The token does not keep a lane writer out, so the lane is looked at once more under the config lock: a lane that
    recovery reactivated just before that lock keeps its checkout."""
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")
    real_lock, fired = Store._config_lock, []

    def lock(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            _finished_lane(repo, wt, "active")
        return real_lock(self, *args, **kwargs)

    monkeypatch.setattr(Store, "_config_lock", lock)

    code, text = run(cfg, wt)

    assert fired and code == 1 and "active lane" in text, text
    assert (wt / "source.txt").exists()


def test_a_lane_writer_that_arrives_while_release_removes_the_checkout_waits_until_it_is_gone(estate, monkeypatch):
    repo, wt, cfg = estate
    _finished_lane(repo, wt, "delivered")
    store, real, events = Store(repo), worktree_release._read_git, []

    def writer():
        with store._config_lock():
            events.append("saved")

    thread = threading.Thread(target=writer)

    def read_git(where, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            thread.start()
            thread.join(timeout=2.0)  # long enough for a writer that is not held back to get its save in
        result = real(where, *args, **kwargs)
        if args[:2] == ("worktree", "remove"):
            events.append("removed")
        return result

    monkeypatch.setattr(worktree_release, "_read_git", read_git)

    code, text = run(cfg, wt)
    thread.join(timeout=30)

    assert code == 0, text
    assert events == ["removed", "saved"]


@pytest.mark.parametrize("retained", [None, "bad", {}, [None], [7], [""]])
def test_release_refuses_damaged_retained_checkout_links(estate, retained):
    repo, wt, cfg = estate
    state = repo / ".agenttalk/state"
    state.mkdir()
    (state / "lanes.json").write_text(json.dumps({"lanes": {"reassigned": {
        "status": "active", "occupancy_worktrees": retained}}}))
    before = (wt / "source.txt").read_bytes()
    head = git(repo, "rev-parse", "finished")

    code, text = run(cfg, wt)

    assert code == 1 and "lanes.json lane reassigned: could not read retained occupancy worktrees" in text, text
    assert wt.is_dir() and (wt / "source.txt").read_bytes() == before
    assert git(repo, "rev-parse", "finished") == head


@pytest.mark.parametrize("field,damaged", [
    *(('occupancy_worktrees', value) for value in (None, "bad", {}, [None], [7], [""])),
    *((field, value) for field in ("worktree_path", "worktree_toplevel_canonical")
      for value in (7, {}, [], False)),
])
def test_assign_refuses_damaged_checkout_links(tmp_path, capsys, field, damaged):
    from test_lanes import _assign_worktree, _branch, _repo, _run_raw

    from agenttalk import lanes

    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "reassigned")
    checkout = Path(lane["worktree_path"])
    state = Store(root).state_dir / "lanes.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["lanes"]["reassigned"][field] = damaged
    state.write_text(json.dumps(data), encoding="utf-8")
    before = state.read_bytes()
    content = (checkout / "src/core/a.py").read_bytes()
    branch = git(root, "rev-parse", "lane/reassigned")
    capsys.readouterr()

    code = _run_raw(["lane", "assign", "--id", "reassigned", "--force", "--from", "lead",
                     "--assignee", "dev2", "--advisory", "--domain", "core", "--base", base,
                     "--target", _branch(root), "--path", "src/core", "--no-worktree",
                     "--worktree-waiver-reason", "second generation"], root)

    output = capsys.readouterr().err
    updated = lanes.load_lanes(Store(root))["lanes"]["reassigned"]
    reason = (
        "could not read retained occupancy worktrees"
        if field == "occupancy_worktrees"
        else f"lanes.json lane reassigned: could not read current checkout path {field}; "
        "to retire this lane without using its checkout, run "
        "`lane abandon --id reassigned --acknowledge-missing-worktree`"
    )
    assert code == 2 and output == f"agenttalk lane assign: {reason}\n", output
    assert state.read_bytes() == before
    assert updated["assignee"] == "dev"
    assert (checkout / "src/core/a.py").read_bytes() == content
    assert git(root, "rev-parse", "lane/reassigned") == branch


@pytest.mark.parametrize("canonical", [None, ""])
def test_assign_checks_fallback_current_checkout_path(tmp_path, capsys, canonical):
    from test_lanes import _assign_worktree, _branch, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "reassigned")
    checkout = Path(lane["worktree_path"])
    state = Store(root).state_dir / "lanes.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["lanes"]["reassigned"]["worktree_path"] = 7
    data["lanes"]["reassigned"]["worktree_toplevel_canonical"] = canonical
    state.write_text(json.dumps(data), encoding="utf-8")
    before = state.read_bytes()
    content = (checkout / "src/core/a.py").read_bytes()
    branch = git(root, "rev-parse", "lane/reassigned")
    capsys.readouterr()

    code = _run_raw(
        [
            "lane",
            "assign",
            "--id",
            "reassigned",
            "--force",
            "--from",
            "lead",
            "--assignee",
            "dev2",
            "--advisory",
            "--domain",
            "core",
            "--base",
            base,
            "--target",
            _branch(root),
            "--path",
            "src/core",
            "--no-worktree",
            "--worktree-waiver-reason",
            "second generation",
        ],
        root,
    )

    assert code == 2
    assert capsys.readouterr().err == (
        "agenttalk lane assign: lanes.json lane reassigned: could not read current checkout path worktree_path; "
        "to retire this lane without using its checkout, run "
        "`lane abandon --id reassigned --acknowledge-missing-worktree`\n"
    )
    assert state.read_bytes() == before
    assert (checkout / "src/core/a.py").read_bytes() == content
    assert git(root, "rev-parse", "lane/reassigned") == branch


@pytest.mark.parametrize("damaged", [False, 0, [], {}, "", None])
def test_assign_only_repairs_an_absent_fallback_checkout(tmp_path, capsys, damaged):
    from test_lanes import _assign_worktree, _branch, _repo, _run_raw

    from agenttalk import lanes

    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "reassigned")
    checkout = Path(lane["worktree_path"])
    state = Store(root).state_dir / "lanes.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["lanes"]["reassigned"]["worktree_path"] = damaged
    data["lanes"]["reassigned"]["worktree_toplevel_canonical"] = None
    state.write_text(json.dumps(data), encoding="utf-8")
    before = state.read_bytes()
    content = (checkout / "src/core/a.py").read_bytes()
    branch = git(root, "rev-parse", "lane/reassigned")
    capsys.readouterr()

    code = _run_raw(
        [
            "lane",
            "assign",
            "--id",
            "reassigned",
            "--force",
            "--from",
            "lead",
            "--assignee",
            "dev2",
            "--advisory",
            "--domain",
            "core",
            "--base",
            base,
            "--target",
            _branch(root),
            "--path",
            "src/core",
            "--no-worktree",
            "--worktree-waiver-reason",
            "repair empty checkout",
        ],
        root,
    )

    output = capsys.readouterr().err
    if damaged is not None:
        assert code == 2
        assert output == (
            "agenttalk lane assign: lanes.json lane reassigned: "
            "could not read current checkout path worktree_path; "
            "to retire this lane without using its checkout, run "
            "`lane abandon --id reassigned --acknowledge-missing-worktree`\n"
        )
        assert state.read_bytes() == before
        assert (checkout / "src/core/a.py").read_bytes() == content
        assert git(root, "rev-parse", "lane/reassigned") == branch
        return
    assert code == 0 and output == ""
    updated = lanes.load_lanes(Store(root))["lanes"]["reassigned"]
    assert updated["assignee"] == "dev2"
    assert "occupancy_worktrees" not in updated
    assert "worktree_path" not in updated and "worktree_toplevel_canonical" not in updated
    assert state.read_bytes() != before
    assert (checkout / "src/core/a.py").read_bytes() == content
    assert git(root, "rev-parse", "lane/reassigned") == branch


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("whitespace", ["   ", " \t\n "])
def test_assign_names_whitespace_checkout_path(tmp_path, capsys, field, whitespace):
    from test_lanes import _assign_worktree, _branch, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "reassigned")
    checkout = Path(lane["worktree_path"])
    state = Store(root).state_dir / "lanes.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["lanes"]["reassigned"]["worktree_toplevel_canonical"] = None
    data["lanes"]["reassigned"][field] = whitespace
    state.write_text(json.dumps(data), encoding="utf-8")
    before = state.read_bytes()
    content = (checkout / "src/core/a.py").read_bytes()
    branch = git(root, "rev-parse", "lane/reassigned")
    capsys.readouterr()

    code = _run_raw(
        [
            "lane",
            "assign",
            "--id",
            "reassigned",
            "--force",
            "--from",
            "lead",
            "--assignee",
            "dev2",
            "--advisory",
            "--domain",
            "core",
            "--base",
            base,
            "--target",
            _branch(root),
            "--path",
            "src/core",
            "--no-worktree",
            "--worktree-waiver-reason",
            "second generation",
        ],
        root,
    )

    assert code == 2
    assert capsys.readouterr().err == (
        f"agenttalk lane assign: lanes.json lane reassigned: could not read current checkout path {field}; "
        "to retire this lane without using its checkout, run "
        "`lane abandon --id reassigned --acknowledge-missing-worktree`\n"
    )
    assert state.read_bytes() == before
    assert (checkout / "src/core/a.py").read_bytes() == content
    assert git(root, "rev-parse", "lane/reassigned") == branch


# ------------------------------------------------- #737: the content check works in bounded groups
MIB = 1024 * 1024


def _names(count, prefix="f"):
    return [f"{prefix}{i:05d}.txt" for i in range(count)]


class _FakeGit:
    """Stands in for ``subprocess.run`` under the real ``_git``: it answers ls-files, check-attr and hash-object for
    synthetic tracked files, with a different hash for every file, and records every call."""

    def __init__(self, names, attrs=None, crlf=(), edited=(), override=None):
        self.names = list(names)
        self.blob = {n: f"{i + 1:040x}" for i, n in enumerate(self.names)}
        self.attrs, self.crlf, self.edited, self.override = attrs or {}, set(crlf), set(edited), override
        self.calls = []  # (command, names asked, flags, timeout, from stdin)

    def hash_of(self, name, raw):
        if name in self.edited or (name in self.crlf and not raw):
            return f"{int(self.blob[name], 16) + (1 << 100):040x}"  # the converted hash of a CRLF file, or any edit
        return self.blob[name]

    def __call__(self, cmd, **kwargs):
        args = cmd[11:]  # after `git -C <repo>` and the four -c options that _git adds
        command, data = args[0], kwargs.get("input")
        flags = tuple(a for a in args[1:] if a.startswith("--") and a not in ("--stdin-paths", "--stdin", "--"))
        asked = []
        if command == "ls-files":
            out = "".join(f"100644 {self.blob[n]} 0\t{n}\0" for n in self.names)
        elif command == "check-attr":
            asked = data.decode().split("\0")[:-1]
            out = "".join(f"{n}\0filter\0{self.attrs.get(n, 'unspecified')}\0" for n in asked)
        else:  # hash-object, from stdin or with one literal argument
            raw = "--no-filters" in flags
            asked = data.decode().split("\n")[:-1] if "--stdin-paths" in args else [args[-1]]
            out = "".join(self.hash_of(n, raw) + "\n" for n in asked)
        self.calls.append((command, asked, flags, kwargs.get("timeout"), "--stdin-paths" in args))
        result = types.SimpleNamespace(returncode=0, stdout=out if kwargs.get("text") else out.encode())
        return (self.override(self, len(self.calls), command, result) or result) if self.override else result

    def groups(self, raw):
        """The names of each stdin hash call of one pass."""
        return [c[1] for c in self.calls if c[0] == "hash-object" and c[4] and ("--no-filters" in c[2]) == raw]


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    """Install _FakeGit and the sizes of its files; returns (the fake, the names whose size was read)."""

    def install(names, sizes=None, lstat_error=None, **kwargs):
        fake, read, real = _FakeGit(names, **kwargs), [], Path.lstat
        known = {str(tmp_path / n): n for n in fake.names}

        def lstat(self, *args, **kw):
            name = known.get(str(self))
            if name is None:
                return real(self, *args, **kw)
            read.append(name)
            if lstat_error and name == lstat_error[0]:
                raise lstat_error[1]
            return types.SimpleNamespace(st_size=(sizes or {}).get(name, 1))

        monkeypatch.setattr(worktree_release.subprocess, "run", fake)
        monkeypatch.setattr(Path, "lstat", lstat)
        return fake, read

    return install


@pytest.mark.parametrize(
    "count,expected", [(0, []), (1, [1]), (1024, [1024]), (1025, [1024, 1]), (2049, [1024, 1024, 1])]
)
@pytest.mark.parametrize("pass_two", [False, True], ids=["converted-matches", "converted-differs-raw-matches"])
def test_the_count_limit_splits_the_hash_calls_in_both_passes(tmp_path, fake_git, count, expected, pass_two):
    names = _names(count)
    fake, _ = fake_git(names, crlf=names if pass_two else ())

    worktree_release._check_tracked_content(tmp_path)

    assert [len(g) for g in fake.groups(raw=False)] == expected
    assert [len(g) for g in fake.groups(raw=True)] == (expected if pass_two else [])
    assert [g for group in fake.groups(raw=False) for g in group] == names  # every name once, in order
    assert sum(1 for c in fake.calls if c[0] == "check-attr") == (
        1 if count else 0
    )  # one query per check, none if empty
    assert {c[3] for c in fake.calls} == {60}  # every call, the query too, keeps its own minute
    assert all(len(g) * 59 / 1024 <= 60 for g in fake.groups(raw=False))  # a group fits a minute at 59/1024 s per file


@pytest.mark.parametrize(
    "value,filtered",
    [("lfs", True), ("set", True), ("custom-driver", True), ("unspecified", False), ("unset", False)],
)
def test_a_filtered_file_is_hashed_alone_and_any_other_value_is_not_a_filter(tmp_path, fake_git, value, filtered):
    fake, _ = fake_git(["before.txt", "target.txt", "after.txt"], attrs={"target.txt": value})

    worktree_release._check_tracked_content(tmp_path)

    assert fake.groups(raw=False) == (
        [["before.txt"], ["target.txt"], ["after.txt"]] if filtered else [["before.txt", "target.txt", "after.txt"]]
    )


def test_nine_filtered_files_make_nine_converted_calls_and_one_raw_call_each_within_a_minute(tmp_path, fake_git):
    filtered = _names(9, "lfs")
    names = ["a.txt", *filtered, "z.txt"]
    fake, _ = fake_git(names, attrs=dict.fromkeys(filtered, "lfs"), crlf=filtered)

    worktree_release._check_tracked_content(tmp_path)

    assert fake.groups(raw=False) == [["a.txt"], *[[n] for n in filtered], ["z.txt"]]
    assert fake.groups(raw=True) == [filtered]  # filters do not run in the raw pass: the nine share one call
    cost = 40  # seconds a slow filter needs per file: one file fits a minute, two do not
    assert max(len(g) for g in fake.groups(raw=False)) * cost <= 60 < 2 * cost


@pytest.mark.parametrize(
    "sizes,expected",
    [
        ([128 * MIB, 128 * MIB, 1], [2, 1]),
        ([256 * MIB], [1]),
        ([1, 256 * MIB + 1, 1], [1, 1, 1]),
        ([0, 0, 0], [3]),
        ([256 * MIB, 0], [2]),
        ([256 * MIB + 1, 0], [1, 1]),
    ],
)
def test_the_byte_limit_splits_groups_exactly_in_both_passes(tmp_path, fake_git, sizes, expected):
    names = _names(len(sizes))
    fake, _ = fake_git(names, sizes=dict(zip(names, sizes, strict=True)), crlf=names)

    worktree_release._check_tracked_content(tmp_path)

    assert [len(g) for g in fake.groups(raw=False)] == expected
    assert [len(g) for g in fake.groups(raw=True)] == expected


def test_a_name_that_must_be_asked_alone_does_not_shift_the_group_boundaries(tmp_path, fake_git):
    ordinary = _names(1024)
    for special in ("cr\rname.txt", "lf\nname.txt", '"quoted.txt'):
        names = [*ordinary[:500], special, *ordinary[500:]]
        fake, _ = fake_git(names)

        worktree_release._check_tracked_content(tmp_path)

        assert [len(g) for g in fake.groups(raw=False)] == [1024]  # the special name is not in any group
        asked_alone = [c for c in fake.calls if c[0] == "hash-object" and not c[4]]
        assert [c[1] for c in asked_alone] == [[special]]
        assert all(special not in g for g in fake.groups(raw=False))


def test_sizes_are_read_once_per_file_and_per_check_never_the_content(tmp_path, fake_git):
    names = _names(5)
    fake, read = fake_git(names, crlf=names)  # the raw pass runs too, and must not read the sizes again

    worktree_release._check_tracked_content(tmp_path)
    assert read == names
    worktree_release._check_tracked_content(tmp_path)  # a second eligibility check measures again
    assert read == names + names


@pytest.mark.parametrize("error", [FileNotFoundError(2, "gone"), PermissionError(13, "denied")])
def test_a_failed_size_read_refuses_before_any_hashing(tmp_path, fake_git, error):
    names = _names(3)
    fake, _ = fake_git(names, lstat_error=(names[1], error))

    with pytest.raises(worktree_release.Refused, match="could not read the size"):
        worktree_release._check_tracked_content(tmp_path)

    assert not [c for c in fake.calls if c[0] == "hash-object"]  # never a size of 0, never a skipped file


def test_the_attribute_query_sends_every_name_with_a_nul_and_hash_calls_never_send_the_special_ones(tmp_path, fake_git):
    names = ["a.txt", "cr\rname.txt", "lf\nname.txt", "trail\r", '"quoted.txt', " spaced name ", "né.txt"]
    fake, _ = fake_git(names)

    worktree_release._check_tracked_content(tmp_path)

    asked = next(c for c in fake.calls if c[0] == "check-attr")
    assert asked[1] == names and asked[4] is False  # all of them, special names included, in index order, NUL-separated
    assert fake.groups(raw=False) == [["a.txt", " spaced name ", "né.txt"]]
    assert [c[1] for c in fake.calls if c[0] == "hash-object" and not c[4]] == [[n] for n in names[1:5]]


def _break_attr(how):
    def override(fake, index, command, result):
        if command != "check-attr":
            return None
        out = result.stdout.decode()
        if how == "wrong-count":
            result.stdout = out.split("\0", 3)[3].encode()  # the first triple is missing
        elif how == "wrong-path":
            result.stdout = out.replace("f00001.txt", "other.txt", 1).encode()
        elif how == "wrong-attribute":
            result.stdout = out.replace("filter", "text", 1).encode()
        elif how == "missing-final-nul":
            result.stdout = out[:-1].encode()
        elif how == "one-extra-record":  # all the expected records, then one more that is complete and valid
            result.stdout = (out + "extra.txt\0filter\0unspecified\0").encode()
        elif how == "nonzero-with-valid-output":
            result.returncode = 1
        elif how == "undecodable":
            result.stdout = b"\xff\xfe"
        elif how == "timeout":
            raise subprocess.TimeoutExpired("git", 60)
        return result

    return override


@pytest.mark.parametrize(
    "how",
    [
        "wrong-count",
        "wrong-path",
        "wrong-attribute",
        "missing-final-nul",
        "one-extra-record",
        "nonzero-with-valid-output",
        "undecodable",
        "timeout",
    ],
)
def test_a_failed_or_odd_attribute_answer_refuses_and_nothing_is_hashed(tmp_path, fake_git, how):
    fake, _ = fake_git(_names(3), override=_break_attr(how))

    with pytest.raises(worktree_release.Refused):
        worktree_release._check_tracked_content(tmp_path)

    assert not [c for c in fake.calls if c[0] == "hash-object"]  # never "no filters, hash everything"


@pytest.mark.parametrize("edit_at", [0, 1023, 1024, 2048])
def test_a_real_edit_in_any_group_is_found_by_name(tmp_path, fake_git, edit_at):
    names = _names(2049)
    fake, _ = fake_git(names, edited=[names[edit_at]], crlf=names[1000:1010])  # raw matches for the CRLF files only

    with pytest.raises(worktree_release.Refused, match=f"differs from the index: {names[edit_at]}$"):
        worktree_release._check_tracked_content(tmp_path)


def test_clean_crlf_files_in_every_group_are_accepted(tmp_path, fake_git):
    names = _names(2049)
    fake_git(names, crlf=names)  # every converted hash differs, every raw hash matches

    worktree_release._check_tracked_content(tmp_path)  # must not raise


@pytest.mark.parametrize("raw", [False, True], ids=["converted-pass", "raw-pass"])
@pytest.mark.parametrize("how", ["too-few-answers", "too-many-answers", "nonzero-with-answers", "timeout"])
def test_a_later_group_that_fails_refuses_in_each_pass(tmp_path, fake_git, raw, how):
    names = _names(2049)

    def override(fake, index, command, result):
        stdin_hash = command == "hash-object" and fake.calls[-1][4] and ("--no-filters" in fake.calls[-1][2]) == raw
        if not stdin_hash or len(fake.groups(raw)) != 2:  # the second group of that pass
            return None
        if how == "too-few-answers":
            result.stdout = result.stdout[: -len(result.stdout.splitlines()[-1]) - 1]
        elif how == "too-many-answers":
            result.stdout = result.stdout + result.stdout.splitlines()[0] + b"\n"
        elif how == "nonzero-with-answers":
            result.returncode = 1
        else:
            raise subprocess.TimeoutExpired("git", 60)
        return result

    fake_git(names, crlf=names, override=override)

    with pytest.raises(worktree_release.Refused):
        worktree_release._check_tracked_content(tmp_path)


@pytest.mark.parametrize("hook_at", ["converted", "raw"])
@pytest.mark.parametrize("check", ["first", "last"])
def test_ctrl_c_during_the_content_check_keeps_the_checkout_and_lets_go_of_the_lane(
    estate, monkeypatch, hook_at, check
):
    repo, wt, cfg = estate
    _commit_exact(repo, wt, {"crlf.txt": b"one\r\ntwo\r\n"}, "true")  # a CRLF file, so the raw pass runs too
    _finished_lane(repo, wt, "delivered")
    seen, wanted, entered, left, removed = {"converted": 0, "raw": 0}, 1 if check == "first" else 2, [], [], []
    real_run, real_claim = subprocess.run, lane_occupancy.cleanup_claim

    @contextlib.contextmanager
    def claim(store, lane):
        entered.append(lane)
        with real_claim(store, lane) as busy:
            try:
                yield busy
            finally:
                left.append(lane)

    def run_git(cmd, *args, **kwargs):
        if "--stdin-paths" in cmd:  # the n-th hash call of the pass named by hook_at is interrupted
            kind = "raw" if "--no-filters" in cmd else "converted"
            seen[kind] += 1
            if kind == hook_at and seen[kind] == wanted:
                raise KeyboardInterrupt
        if "worktree" in cmd and "remove" in cmd:
            removed.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_release.subprocess, "run", run_git)
    monkeypatch.setattr(lane_occupancy, "cleanup_claim", claim)

    with pytest.raises(KeyboardInterrupt):
        run(cfg, wt)

    assert wt.exists() and (wt / "crlf.txt").read_bytes() == b"one\r\ntwo\r\n" and not removed
    assert entered == left == ([] if check == "first" else ["one"])  # the final check holds the claim; it was let go of
    assert lane_occupancy.peek(Store(repo), "one") is None


def test_a_failed_attribute_query_keeps_the_checkout_in_targeted_and_report_mode(estate, monkeypatch):
    repo, wt, cfg = estate
    real_run = subprocess.run

    def run_git(cmd, *args, **kwargs):
        if "check-attr" in cmd:
            return subprocess.CompletedProcess(cmd, 1, b"", b"")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_release.subprocess, "run", run_git)

    code, text = run(cfg, wt)
    assert code == 1 and "KEPT" in text and "check-attr" in text and wt.exists(), text
    code, text = run(cfg, None)
    assert f"KEPT {wt}" in text and "check-attr" in text and wt.exists(), text
