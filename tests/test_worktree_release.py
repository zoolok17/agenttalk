"""Real local remotes and checkouts; every possible deletion is inside tmp_path."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agenttalk import janitor


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

    def refused(repo, *args):
        if args[:2] == ("worktree", "remove"):
            removals.append(args)
            return 1, ""
        return original(repo, *args)

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

    def fake_remove(repo, *args):
        if args[:2] == ("worktree", "remove"):
            reached_remove.append(True)
            return 0, ""
        return original_git(repo, *args)

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


def test_git_untracked_cache_override_wins_over_repository_config(estate):
    from agenttalk import worktree_release as release_mod
    repo, wt, _ = estate
    git(repo, "config", "core.untrackedCache", "true")
    assert git(wt, "config", "--bool", "core.untrackedCache") == "true"
    # Ask Git for its effective setting through the same boundary used by
    # checks and removal. This pins the cache override independently of fsmonitor.
    assert release_mod._read_git(wt, "config", "--bool", "core.untrackedCache") == "false"
    assert git(wt, "config", "--bool", "core.untrackedCache") == "true", "do not edit user config"
