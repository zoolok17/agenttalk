"""Real-Git witnesses for #794; run only in fenced throwaway repositories."""

import os
import shutil
from pathlib import Path
import time
import subprocess

import pytest

from agenttalk import acceptance, cli, close, janitor, lanes
from test_git_index_safety import safety as _safety
from agenttalk.store import Store
from test_lanes import (
    _abandon_with_the_worktree_kept,
    _assign_worktree,
    _commit as _lane_commit,
    _gc_item,
    _git,
    _lane_branch_present,
    _repo,
    _run_raw,
    _786_assign_args,
)


def _commit(root, relative, text):
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    return _lane_commit(root, relative, text)


def _flagged(wt, flag, edited=True):
    file = wt / "src/core/a.py"
    if flag in {"assume", "both"}:
        _git(wt, "update-index", "--assume-unchanged", "--", "src/core/a.py")
    if flag in {"skip", "both"}:
        _git(wt, "update-index", "--skip-worktree", "--", "src/core/a.py")
    tag = {"assume": "h", "skip": "S", "both": "s"}[flag]
    assert tag + " src/core/a.py\0" in _git(wt, "ls-files", "-v", "-z")
    if edited:
        file.write_bytes(b"precious hidden work\n")
    assert _git(wt, "status", "--porcelain", "--untracked-files=normal").strip() == ""
    return file, file.read_bytes()


@pytest.mark.parametrize("action", ["abandon", "gc", "deliver", "rollback"])
@pytest.mark.parametrize("flag", ["assume", "skip"])
def test_hidden_work_survives_all_lane_cleanup_routes(tmp_path, capsys, monkeypatch, action, flag):
    root, base = _repo(tmp_path)
    lane_id = "hidden-" + action
    if action == "rollback":
        verify = cli._verify_lane_worktree
        created = []

        def fail(store, lane, **kwargs):
            verify(store, lane, **kwargs)
            wt = Path(lane["worktree_path"])
            created.append((wt, *_flagged(wt, flag)))
            raise lanes.LaneError("deterministic provisioning failure")

        monkeypatch.setattr(cli, "_verify_lane_worktree", fail)
        assert _run_raw(_786_assign_args(root, base, lane_id), root) == 2
        assert "deterministic provisioning failure" in capsys.readouterr().err
        wt, file, data = created[0]
    else:
        lane = _assign_worktree(root, base, lane_id)
        wt = Path(lane["worktree_path"])
        if action == "deliver":
            _commit(wt, "src/core/a.py", "base\ndelivered\n")
        if action == "gc":
            _abandon_with_the_worktree_kept(root, lane_id)
        file, data = _flagged(wt, flag)
        if action == "gc":
            item = _gc_item(capsys, root, lane_id)
            assert item["worktree_removed"] is False and not item.get("branch_deleted")
        else:
            args = ["lane", action, "--id", lane_id] + (["--from", "dev"] if action == "deliver" else [])
            if action == "abandon":
                args += ["--delete-branch"]
            assert _run_raw(args, root) == 0
            saved = lanes.load_lanes(Store(root))["lanes"][lane_id]
            assert saved["worktree_state"] == lanes.STATUS_CLEANUP_FAILED
            assert _safety().HIDDEN in saved["worktree_cleanup_error"]
            if action == "deliver":
                assert saved["status"] == lanes.STATUS_DELIVERED and saved["cleanup_pending"] is True
            else:
                assert saved["status"] == lanes.STATUS_ABANDONED
    assert wt.is_dir() and file.read_bytes() == data
    assert _lane_branch_present(root, lane_id)
    assert _safety().check_hidden_index(wt).state == "hidden"
    assert str(wt).replace("\\", "/") in _git(root, "worktree", "list", "--porcelain").replace("\\", "/")


@pytest.mark.parametrize("flag", ["assume", "skip", "both"])
@pytest.mark.parametrize("edited", [False, True])
def test_linked_index_not_primary_index_controls_removal(tmp_path, flag, edited):
    root, base = _repo(tmp_path)
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    file, data = _flagged(wt, flag, edited)
    assert _safety().check_hidden_index(root).state == "safe"
    before = _git(wt, "ls-files", "-v", "-z")
    assert cli._git_write(root, ["worktree", "remove", "--", str(wt)])[0] != 0
    assert file.read_bytes() == data and _git(wt, "ls-files", "-v", "-z") == before


@pytest.mark.parametrize("action", ["open", "reopen"])
@pytest.mark.parametrize("flag", ["assume", "skip", "ignoreStat"])
def test_close_records_hidden_index_as_false_boolean(tmp_path, capsys, action, flag):
    root, base = _repo(tmp_path)
    if action == "reopen":
        assert (
            _run_raw(
                ["close", "open", "--id", "hidden", "--from", "lead", "--scope", "release", "--revision", base], root
            )
            == 0
        )
    if flag == "ignoreStat":
        _git(root, "config", "core.ignoreStat", "true")
        _git(root, "update-index", "--really-refresh")
        assert "h src/core/a.py\0" in _git(root, "ls-files", "-v", "-z")
    else:
        _flagged(root, flag)
    args = ["close", action, "--id", "hidden", "--from", "lead", "--revision", base]
    if action == "open":
        args += ["--scope", "release"]
    assert _run_raw(args, root) == 0
    assert close.load_close(Store(root), "hidden")["revision_clean"] is False
    assert "this checkout is not recorded as clean" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["cone", "sparse-index", "non-cone", "large"])
def test_real_sparse_absence_is_not_hidden_work(tmp_path, mode, record_property, monkeypatch):
    root, _ = _repo(tmp_path)
    _commit(root, "included/a", "included\n")
    count = 4096 if mode == "large" else 2
    omitted = root / "omitted"
    omitted.mkdir()
    for number in range(count):
        (omitted / str(number)).write_bytes(b"omitted\n")
    _git(root, "add", "omitted")
    _git(root, "commit", "-qm", "sparse fixture")
    base = _git(root, "rev-parse", "HEAD").strip()
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    if mode == "non-cone":
        _git(wt, "sparse-checkout", "init", "--no-cone")
        _git(wt, "sparse-checkout", "set", "/included/", "/.gitignore")
    else:
        _git(wt, "sparse-checkout", "init", "--cone", *(["--sparse-index"] if mode == "sparse-index" else []))
        _git(wt, "sparse-checkout", "set", "included")
    listing = _git(wt, "ls-files", "--cached", "-v", "-z", "--full-name")
    assert listing.count("S omitted/") == count and not (wt / "omitted").exists()
    reads, metadata = [], []
    real_run, real_lstat = subprocess.run, os.lstat

    def read(argv, **kwargs):
        reads.append(argv)
        return real_run(argv, **kwargs)

    def probe(path, *args, **kwargs):
        metadata.append(path)
        return real_lstat(path, *args, **kwargs)

    start = time.perf_counter()
    with monkeypatch.context() as measured:
        measured.setattr(subprocess, "run", read)
        measured.setattr(os, "lstat", probe)
        assert _safety().check_hidden_index(wt).state == "safe"
    record_property("omitted_entries", count)
    record_property("visibility_elapsed_seconds", time.perf_counter() - start)
    record_property("helper_git_commands", len(reads))
    record_property("metadata_probes", len(metadata))
    assert len(reads) == 2
    assert sum("--show-toplevel" in argv for argv in reads) == 1
    assert sum("ls-files" in argv for argv in reads) == 1
    assert cli._worktree_clean(wt) is True and janitor.is_dirty_worktree(wt) is False
    store = Store(wt)
    store.init(["lead"])
    store.set_role("lead", "lead")
    assert (
        cli.main(
            [
                "--root",
                str(wt),
                "close",
                "open",
                "--id",
                "sparse",
                "--from",
                "lead",
                "--scope",
                "release",
                "--revision",
                base,
                "--non-lane-isolation-not-asserted",
            ]
        )
        == 0
    )
    assert close.load_close(store, "sparse")["revision_clean"] is True
    # The store made above lives inside the checkout, and a removal now refuses a checkout that holds a store (Git
    # ignores it, so it would be deleted unseen). This test is about sparse absence, so the store goes first.
    assert cli._git_write(root, ["worktree", "remove", "--", str(wt)])[0] == 1
    shutil.rmtree(wt / ".agenttalk")
    assert cli._git_write(root, ["worktree", "remove", "--", str(wt)])[0] == 0


@pytest.mark.parametrize("before", ["none", "direct-wip"])
def test_unpopulated_gitlink_content_survives_abandon(tmp_path, capsys, monkeypatch, before):
    root, base = _repo(tmp_path)
    _git(root, "update-index", "--add", "--cacheinfo", "160000," + base + ",module")
    _git(root, "commit", "-qm", "unpopulated gitlink")
    base = _git(root, "rev-parse", "HEAD").strip()
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    module = wt / "module"
    module.mkdir(exist_ok=True)
    file = module / "precious"
    file.write_bytes(b"work without submodule metadata")
    assert not (module / ".git").exists()
    assert _git(wt, "status", "--porcelain", "--ignore-submodules=none", "--untracked-files=all").strip() == ""
    if before == "direct-wip":
        real_run, mutations = subprocess.run, []

        def run(argv, *args, **kwargs):
            if "add" in argv or "commit" in argv:
                mutations.append(argv)
            return real_run(argv, *args, **kwargs)

        with monkeypatch.context() as observed:
            observed.setattr(subprocess, "run", run)
            result = janitor.wip_commit_dirty_worktree(wt, default_branches=["main", "master"])
        assert result.refused and _safety().SUBMODULE in result.message and not mutations
        assert _git(wt, "rev-parse", "HEAD").strip() == base
    assert _run_raw(["lane", "abandon", "--id", "lwt", "--delete-branch"], root) == 0
    capsys.readouterr()
    assert file.read_bytes() == b"work without submodule metadata"
    saved = lanes.load_lanes(Store(root))["lanes"]["lwt"]
    assert _safety().SUBMODULE in saved["worktree_cleanup_error"] and _lane_branch_present(root, "lwt")


@pytest.mark.parametrize("consumer", ["close", "acceptance"])
@pytest.mark.parametrize(
    "case", ["clean", "tracked", "untracked", "head", "assume", "skip", "uninitialized", "missing"]
)
def test_close_submodule_cleanliness_real(tmp_path, case, consumer, capsys):
    root = tmp_path / "parent"
    root.mkdir()
    root, _ = _repo(root)
    source = tmp_path / "source"
    source.mkdir()
    _repo(source)

    def checked(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    checked(root, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module")
    checked(root, "commit", "-qam", "local submodule")
    revision = _git(root, "rev-parse", "HEAD").strip()
    module = root / "module"
    _git(root, "config", "submodule.module.ignore", "all")
    if case == "missing":
        shutil.rmtree(module)
    elif case == "untracked":
        (module / "new-file").write_text("untracked", encoding="utf-8")
    elif case == "head":
        checked(module, "config", "user.name", "test")
        checked(module, "config", "user.email", "test@example.invalid")
        _commit(module, "src/core/a.py", "new commit\n")
        assert checked(module, "rev-parse", "HEAD").strip() != checked(root, "rev-parse", "HEAD:module").strip()
    elif case == "uninitialized":
        (module / ".git").unlink()
    elif case != "clean":
        if case in {"assume", "skip"}:
            flag = "--assume-unchanged" if case == "assume" else "--skip-worktree"
            _git(module, "update-index", flag, "src/core/a.py")
        (module / "src/core/a.py").write_text("uncommitted", encoding="utf-8")
    if consumer == "acceptance":
        if case == "clean":
            assert acceptance.verify_project(root, revision)["revision"] == revision
        else:
            with pytest.raises(acceptance.AcceptanceError, match="clean|dirty"):
                acceptance.verify_project(root, revision)
    else:
        store = Store(root)
        assert (
            cli.main(
                [
                    "--root",
                    str(root),
                    "close",
                    "open",
                    "--id",
                    "sub",
                    "--from",
                    "lead",
                    "--scope",
                    "docs",
                    "--revision",
                    revision,
                ]
            )
            == 0
        )
        assert close.load_close(store, "sub")["revision_clean"] is (case == "clean")
        if case == "missing":
            output = capsys.readouterr()
            assert "HOLD" in output.err
            assert cli.main(["--root", str(root), "close", "check", "--id", "sub"]) == 3
            assert "HOLD" in capsys.readouterr().out
        if case == "assume":
            with close.close_transaction(store, "sub") as transaction:
                transaction.record["revision_clean"] = True
                transaction.commit()
            assert (
                cli.main(
                    ["--root", str(root), "close", "reopen", "--id", "sub", "--from", "lead", "--revision", revision]
                )
                == 0
            )
            assert close.load_close(store, "sub")["revision_clean"] is False


def test_nested_submodule_hidden_index_is_not_clean(tmp_path):
    parent = tmp_path / "parent"
    source = tmp_path / "source"
    nested_source = tmp_path / "nested-source"
    for path in (parent, source, nested_source):
        path.mkdir()
        _repo(path)

    def checked(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    checked(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module")
    module = parent / "module"
    checked(module, "config", "user.name", "test")
    checked(module, "config", "user.email", "test@example.invalid")
    checked(module, "-c", "protocol.file.allow=always", "submodule", "add", str(nested_source), "nested")
    checked(module, "commit", "-qam", "nested submodule")
    checked(parent, "add", "module")
    checked(parent, "commit", "-qm", "record nested submodule")
    revision = checked(parent, "rev-parse", "HEAD").strip()
    nested = module / "nested"
    checked(nested, "update-index", "--assume-unchanged", "src/core/a.py")
    (nested / "src/core/a.py").write_text("hidden nested work", encoding="utf-8")
    assert checked(parent, *_safety().STATUS_ARGS).strip() == ""
    assert cli._close_clean_detail(parent)[0] is False
    with pytest.raises(acceptance.AcceptanceError, match="clean"):
        acceptance.verify_project(parent, revision)


def test_submodule_status_failure_is_not_clean(tmp_path, monkeypatch):
    root = tmp_path / "parent"
    source = tmp_path / "source"
    root.mkdir()
    source.mkdir()
    _repo(root)
    _repo(source)
    subprocess.run(
        ["git", "-C", str(root), "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "submodule"], check=True, capture_output=True)
    revision = _git(root, "rev-parse", "HEAD").strip()
    original = subprocess.run
    calls = []

    def fail_child_status(argv, **kwargs):
        if argv[:3] == ["git", "-C", str(root / "module")] and argv[3:] == _safety().STATUS_ARGS:
            calls.append(True)
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_child_status)
    assert cli._close_clean_detail(root)[0] is False
    with pytest.raises(acceptance.AcceptanceError, match="clean"):
        acceptance.verify_project(root, revision)
    assert len(calls) == 2


def test_acceptance_submodule_checks_ignore_ambient_git_redirect(tmp_path, monkeypatch):
    root = tmp_path / "parent"
    source = tmp_path / "source"
    root.mkdir()
    source.mkdir()
    _repo(root)
    _repo(source)
    subprocess.run(
        ["git", "-C", str(root), "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "submodule"], check=True, capture_output=True)
    revision = _git(root, "rev-parse", "HEAD").strip()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-git-dir"))
    assert acceptance.verify_project(root, revision)["revision"] == revision


def test_acceptance_refuses_hidden_parent_index(tmp_path):
    root, revision = _repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(root), "update-index", "--assume-unchanged", "src/core/a.py"], check=True, capture_output=True
    )
    (root / "src/core/a.py").write_text("hidden parent work", encoding="utf-8")
    with pytest.raises(acceptance.AcceptanceError, match="index.*clean"):
        acceptance.verify_project(root, revision)


def test_close_submodule_malformed_index_listing_fails_closed(tmp_path, monkeypatch):
    from test_git_index_safety import git_reads

    git_reads(monkeypatch, stage=b"not a staged record\0")
    assert _safety().check_close_submodules(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("ignore", ["dirty", "all", "untracked"])
@pytest.mark.parametrize("location", ["config", "gitmodules"])
@pytest.mark.parametrize("consumer", ["janitor", "close"])
def test_submodule_status_override_and_preservation_gate(tmp_path, ignore, location, consumer):
    root = tmp_path / "parent"
    root.mkdir()
    root, _ = _repo(root)
    source = tmp_path / "source"
    source.mkdir()
    _repo(source)
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module")
    if location == "gitmodules":
        _git(root, "config", "-f", ".gitmodules", "submodule.module.ignore", ignore)
        _git(root, "add", ".gitmodules")
    _git(root, "commit", "-qam", "local submodule")
    base = _git(root, "rev-parse", "HEAD").strip()
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    _git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
    # Initialized submodules were already kept by janitor's metadata walk.
    assert (wt / "module/.git").is_file()
    _git(wt, "config", "diff.ignoreSubmodules", "all")
    if location == "config":
        _git(wt, "config", "submodule.module.ignore", ignore)
    file = wt / "module" / ("new-build-file" if ignore == "untracked" else "src/core/a.py")
    file.write_bytes(b"submodule work")
    assert _git(wt, "status", "--porcelain").strip() == ""
    if consumer == "janitor":
        assert janitor.is_dirty_worktree(wt) is True
    else:
        store = Store(wt)
        store.init(["lead"])
        store.set_role("lead", "lead")
        assert (
            cli.main(
                [
                    "--root",
                    str(wt),
                    "close",
                    "open",
                    "--id",
                    "submodule",
                    "--from",
                    "lead",
                    "--scope",
                    "release",
                    "--revision",
                    base,
                ]
            )
            == 0
        )
        assert close.load_close(store, "submodule")["revision_clean"] is False
    assert cli._worktree_clean(wt) is False
    tip = _git(wt, "rev-parse", "HEAD")
    assert janitor.wip_commit_dirty_worktree(wt, default_branches=["main", "master"]).refused
    assert file.read_bytes() == b"submodule work" and _git(wt, "rev-parse", "HEAD") == tip
    if ignore != "untracked":
        _git(wt / "module", "update-index", "--assume-unchanged", "src/core/a.py")
        assert _git(wt, *_safety().STATUS_ARGS).strip() == ""
        assert _safety().check_materialized_submodules(wt).state == "hidden"
        assert janitor.wip_commit_dirty_worktree(wt, default_branches=["main", "master"]).refused


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink metadata; Windows symlinks=false is not this control")
@pytest.mark.parametrize("where", ["final", "ancestor"])
def test_skipped_dangling_symlink_is_never_sparse_absence(tmp_path, where):
    root, _ = _repo(tmp_path)
    _git(root, "update-index", "--skip-worktree", "src/core/a.py")
    if where == "final":
        (root / "src/core/a.py").unlink()
        (root / "src/core/a.py").symlink_to(root / "missing")
        assert _safety().check_hidden_index(root).state == "hidden"
    else:
        (root / "src").rename(root / "saved-src")
        (root / "src").symlink_to(root / "missing", target_is_directory=True)
        assert _safety().check_hidden_index(root).state == "unverifiable"


@pytest.mark.parametrize("flag", ["assume", "skip"])
@pytest.mark.parametrize("edited", [False, True])
def test_janitor_report_and_apply_keep_flags_without_preservation(tmp_path, monkeypatch, flag, edited):
    root, base = _repo(tmp_path)
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    file, data = _flagged(wt, flag, edited)
    before = _git(wt, "ls-files", "-v", "-z")
    cfg = janitor.JanitorConfig(root, tmp_path / "scratch", 0, 0, tmp_path / "temp", [], [], [], [], ["master", "main"])
    report = janitor.build_report(cfg)
    assert wt in report.dirty_worktrees
    real_run, mutations = subprocess.run, []

    def run(argv, *args, **kwargs):
        if "add" in argv or "commit" in argv or "remove" in argv:
            mutations.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    text = janitor.apply(cfg, report)
    assert "REFUSED (kept)" in text and not mutations
    assert file.read_bytes() == data and _git(wt, "rev-parse", "HEAD").strip() == base
    assert _git(wt, "ls-files", "-v", "-z") == before
    assert _lane_branch_present(root, "lwt")


def test_real_conflict_records_are_not_malformed_visibility(tmp_path):
    root, base = _repo(tmp_path)
    _git(root, "checkout", "-qb", "conflict-left")
    _commit(root, "src/core/a.py", "left\n")
    _git(root, "checkout", "-qb", "conflict-right", base)
    _commit(root, "src/core/a.py", "right\n")
    _git(root, "merge", "--no-commit", "conflict-left")
    listing = _git(root, "ls-files", "--cached", "-v", "-z", "--full-name")
    assert listing.count("M src/core/a.py\0") == 3
    assert _safety().check_hidden_index(root).state == "safe"
    assert _safety().check_materialized_submodules(root).state == "safe"
    assert janitor.is_dirty_worktree(root) is True


def test_both_bits_absent_are_not_sparse_permission_to_remove(tmp_path):
    root, base = _repo(tmp_path)
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    file, _ = _flagged(wt, "both", edited=False)
    file.unlink()
    assert "s src/core/a.py\0" in _git(wt, "ls-files", "-v", "-z")
    assert _safety().check_hidden_index(wt).state == "hidden"
    assert cli._git_write(root, ["worktree", "remove", "--", str(wt)])[0] != 0
    assert wt.is_dir() and _lane_branch_present(root, "lwt")


def test_materialized_sparse_file_is_not_exempted_by_configuration(tmp_path):
    root, _ = _repo(tmp_path)
    _commit(root, "included/a", "included\n")
    _commit(root, "omitted/b", "original\n")
    base = _git(root, "rev-parse", "HEAD").strip()
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    _git(wt, "sparse-checkout", "init", "--cone")
    _git(wt, "sparse-checkout", "set", "included")
    # Keep the intended S entry when newer Git reads an out-of-pattern file.
    _git(wt, "config", "sparse.expectFilesOutsideOfPatterns", "true")
    file = wt / "omitted/b"
    file.parent.mkdir()
    file.write_bytes(b"materialized hidden sparse work")
    assert "S omitted/b\0" in _git(wt, "ls-files", "-v", "-z")
    assert _safety().check_hidden_index(wt).state == "hidden"
    assert cli._git_write(root, ["worktree", "remove", "--", str(wt)])[0] != 0
    assert file.read_bytes() == b"materialized hidden sparse work"


@pytest.mark.parametrize("name", ["with space.txt", "with\ttab.txt", "with\nnewline.txt"])
def test_index_path_framing_keeps_supported_filename_characters(tmp_path, name):
    if os.name == "nt" and any(character in name for character in "\t\n"):
        pytest.skip("Windows does not support these control characters in filenames; POSIX evidence required")
    root, _ = _repo(tmp_path)
    relative = "src/core/" + name
    _commit(root, relative, "original\n")
    _git(root, "update-index", "--assume-unchanged", "--", relative)
    (root / relative).write_bytes(b"precious oddly named work")
    assert "h " + relative + "\0" in _git(root, "ls-files", "-v", "-z")
    assert _safety().check_hidden_index(root).state == "hidden"


@pytest.mark.parametrize("flag", ["assume", "skip"])
def test_one_unsafe_registered_checkout_keeps_the_whole_janitor_candidate(tmp_path, monkeypatch, flag):
    root, base = _repo(tmp_path)
    candidate = root / ".worktrees/batch"
    candidate.mkdir(parents=True)
    worktrees = [candidate / name for name in ("first", "second")]
    for number, path in enumerate(worktrees):
        _git(root, "worktree", "add", "-b", f"feature-{number}", "--", str(path), base)
    first = worktrees[0] / "src/core/a.py"
    first.write_bytes(b"ordinary work must not be committed before the all-keep pass")
    second, data = _flagged(worktrees[1], flag)
    before = [_git(path, "rev-parse", "HEAD") for path in worktrees]
    cfg = janitor.JanitorConfig(root, tmp_path / "scratch", 0, 0, tmp_path / "temp", [], [], [], [], ["master", "main"])
    report = janitor.build_report(cfg)
    assert candidate in [item.path for item in report.candidates]
    real_run, mutations = subprocess.run, []

    def run(argv, *args, **kwargs):
        if "add" in argv or "commit" in argv:
            mutations.append(argv)
        return real_run(argv, *args, **kwargs)

    with monkeypatch.context() as observed:
        observed.setattr(subprocess, "run", run)
        text = janitor.apply(cfg, report)
    assert "REFUSED (kept)" in text and not mutations
    assert first.read_bytes() == b"ordinary work must not be committed before the all-keep pass"
    assert second.read_bytes() == data and candidate.is_dir()
    assert [_git(path, "rev-parse", "HEAD") for path in worktrees] == before


@pytest.mark.parametrize("prefix", ["missing", "ignored-file"])
def test_real_sparse_absence_requires_plain_directory_prefixes(tmp_path, prefix):
    root, _ = _repo(tmp_path)
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    base = _commit(root, ".gitignore", ignore + "\nsrc\n")
    wt = Path(_assign_worktree(root, base)["worktree_path"])
    _git(wt, "update-index", "--skip-worktree", "--", "src/core/a.py", "src/other/b.py")
    shutil.rmtree(wt / "src")
    if prefix == "ignored-file":
        (wt / "src").write_bytes(b"ignored prefix must not be mistaken for absent children")
    assert _git(wt, *_safety().STATUS_ARGS).strip() == ""
    assert _safety().check_hidden_index(wt).state == ("safe" if prefix == "missing" else "unverifiable")
    if prefix == "ignored-file":
        assert cli._git_write(root, ["worktree", "remove", "--", str(wt)])[0] != 0
        assert (wt / "src").read_bytes() == b"ignored prefix must not be mistaken for absent children"
