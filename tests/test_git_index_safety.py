"""Read-only index visibility and preservation boundaries (#794)."""

import errno
import importlib
import os
from pathlib import Path
import subprocess
import stat
import time
from types import SimpleNamespace

import pytest

from agenttalk import cli
from agenttalk import close
from agenttalk import janitor
from agenttalk.store import Store


def safety():
    return importlib.import_module("agenttalk.git_index_safety")


def git_reads(monkeypatch, listing=b"", *, rc=0, status="", stage=b""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(argv[2]) + b"\n", b"")
        if "ls-files" in argv:
            data = stage if "--stage" in argv else listing
            return subprocess.CompletedProcess(argv, rc, data, b"")
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, status, "")
        raise AssertionError(argv)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("tag", ["h", "s", "S", "m", "r", "c", "k"])
def test_clean_status_does_not_hide_materialized_flagged_files(tmp_path, monkeypatch, tag):
    (tmp_path / "kept.txt").write_bytes(b"precious edit")
    git_reads(monkeypatch, tag.encode() + b" kept.txt\0")
    assert cli._worktree_clean(tmp_path) is False
    assert (tmp_path / "kept.txt").read_bytes() == b"precious edit"


@pytest.mark.parametrize("tag,state", [("S", "safe"), ("s", "hidden"), ("h", "hidden")])
def test_absent_flagged_entry_policy(tmp_path, monkeypatch, tag, state):
    git_reads(monkeypatch, tag.encode() + b" absent/directory/file\0")
    assert safety().check_hidden_index(tmp_path).state == state


@pytest.mark.parametrize(
    "listing", [b"", b"H file\0", b"M file\0M file\0M file\0", b"H leading space \0", b"H with\ttab\0"]
)
def test_valid_listings(tmp_path, monkeypatch, listing):
    git_reads(monkeypatch, listing)
    assert safety().check_hidden_index(tmp_path).state == "safe"


@pytest.mark.parametrize(
    "listing",
    [
        b"H file",
        b"X file\0",
        b"H \0",
        b"H /absolute\0",
        b"S ../escape\0",
        b"S C:/outside\0",
        b"H file\0\0",
        b"HH file\0",
    ],
)
def test_malformed_or_escaping_records_are_unverifiable(tmp_path, monkeypatch, listing):
    git_reads(monkeypatch, listing)
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


def test_nonzero_plausible_output_is_not_safe(tmp_path, monkeypatch):
    git_reads(monkeypatch, b"H safe\0", rc=128)
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("git", 15)])
def test_read_failure_is_unverifiable(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("check", ["check_hidden_index", "check_materialized_submodules"])
@pytest.mark.parametrize("output", [b"", b"\n", b"relative\n", b"/missing-newline", b"/bad\0path\n", "not bytes"])
def test_invalid_top_level_output_refuses_before_index_read(tmp_path, monkeypatch, check, output):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", run)
    result = getattr(safety(), check)(tmp_path)
    assert result.state == "unverifiable"
    expected = safety().UNKNOWN if check == "check_hidden_index" else safety().SUBMODULE_UNKNOWN
    assert result.reason == expected
    assert len(calls) == 1 and "--show-toplevel" in calls[0]


def test_non_directory_prefix_is_not_sparse_absence(tmp_path, monkeypatch):
    (tmp_path / "plain").write_bytes(b"not a directory")
    git_reads(monkeypatch, b"S plain/child\0")
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "io")])
def test_metadata_errors_are_not_absence(tmp_path, monkeypatch, error):
    git_reads(monkeypatch, b"S child\0")
    real = os.lstat

    def fail(path, *args, **kwargs):
        if Path(path) == tmp_path / "child":
            raise error
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fail)
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("present", ["absent", "empty", "content", "file"])
def test_gitlink_content_gate(tmp_path, monkeypatch, present):
    link = tmp_path / "module"
    if present in {"empty", "content"}:
        link.mkdir()
    if present == "content":
        (link / "precious").write_bytes(b"unpopulated submodule work")
    if present == "file":
        link.write_bytes(b"replacement")
    git_reads(monkeypatch, stage=b"160000 " + b"a" * 40 + b" 0\tmodule\0")
    expected = "safe" if present in {"absent", "empty"} else "hidden"
    assert safety().check_materialized_submodules(tmp_path).state == expected


def test_gitlink_conflict_stages_are_valid(tmp_path, monkeypatch):
    git_reads(
        monkeypatch, stage=b"".join(b"160000 " + b"a" * 40 + f" {stage}\tmodule\0".encode() for stage in (1, 2, 3))
    )
    assert safety().check_materialized_submodules(tmp_path).state == "safe"


def test_gitlink_nonzero_exit_rejects_a_complete_plausible_listing(tmp_path, monkeypatch):
    git_reads(monkeypatch, rc=128, stage=b"160000 " + b"a" * 40 + b" 0\tmodule\0")
    result = safety().check_materialized_submodules(tmp_path)
    assert result.state == "unverifiable" and result.reason == safety().SUBMODULE_UNKNOWN


@pytest.mark.parametrize(
    "listing",
    [b"160000 bad 0\tmodule\0", b"160000 " + b"a" * 40 + b" 4\tmodule\0", b"160000 " + b"a" * 40 + b" 0\t../module\0"],
)
def test_invalid_stage_listing_is_unverifiable(tmp_path, monkeypatch, listing):
    git_reads(monkeypatch, stage=listing)
    assert safety().check_materialized_submodules(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("status_rc", [1, 128])
def test_failed_index_read_after_clean_status_is_false_not_unknown(tmp_path, monkeypatch, status_rc):
    git_reads(monkeypatch, b"H file\0", rc=status_rc)
    assert cli._worktree_clean(tmp_path) is False


def test_status_visibility_arguments(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    calls = git_reads(monkeypatch)
    assert cli._worktree_clean(tmp_path) is True
    for call in [argv for argv in calls if "status" in argv]:
        assert call[3:] == [
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--ignore-submodules=none",
        ]


def test_unsafe_recovery_values_are_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(safety(), "_checkout_root", lambda folder: folder)
    note = safety().inspection_guidance("folder\n; touch BAD")
    assert "not a ready command" in note and "<folder>" in note and "<file>" in note
    assert "\n;" not in note and "\n&" not in note
    assert "--no-assume-unchanged -- <file>" in note


@pytest.mark.parametrize("action", ["open", "reopen"])
@pytest.mark.parametrize("listing,rc", [(b"h file\0", 0), (b"H file", 0), (b"H file\0", 128)])
def test_close_index_refusal_never_enters_unknown_status_fallback(tmp_path, monkeypatch, capsys, action, listing, rc):
    store = Store(tmp_path)
    store.init(["lead"])
    store.set_role("lead", "lead")
    sha = "a" * 40
    monkeypatch.setattr(
        cli,
        "_git",
        lambda root, argv: (0, "true" if "--is-inside-work-tree" in argv else "" if "status" in argv else sha),
    )
    git_reads(monkeypatch, b"")
    if action == "reopen":
        assert (
            cli.main(
                [
                    "--root",
                    str(tmp_path),
                    "close",
                    "open",
                    "--id",
                    "index",
                    "--from",
                    "lead",
                    "--scope",
                    "release",
                    "--revision",
                    sha,
                ]
            )
            == 0
        )
    git_reads(monkeypatch, listing, rc=rc)
    args = ["--root", str(tmp_path), "close", action, "--id", "index", "--from", "lead", "--revision", sha]
    if action == "open":
        args += ["--scope", "release"]
    assert cli.main(args) == 0
    assert close.load_close(store, "index")["revision_clean"] is False
    error = capsys.readouterr().err
    assert "this checkout is not recorded as clean" in error
    assert "worktree is dirty and no" not in error


def test_materialized_gitlink_refuses_the_final_cli_removal(tmp_path, monkeypatch):
    (tmp_path / "module").mkdir()
    precious = tmp_path / "module/precious"
    precious.write_bytes(b"unpopulated work")
    git_reads(monkeypatch, stage=b"160000 " + b"a" * 40 + b" 0\tmodule\0")
    monkeypatch.setattr(cli, "_git", lambda root, argv: (0, str(tmp_path) if argv[0] == "rev-parse" else ""))
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, communicate=lambda timeout: ("", "")),
    )
    answer = cli._git_write(tmp_path.parent, ["worktree", "remove", "--", str(tmp_path)])
    assert answer[0] == 1 and "materialized submodule" in answer[2]
    assert precious.read_bytes() == b"unpopulated work"


def test_index_read_uses_this_checkout_not_its_parent(tmp_path, monkeypatch):
    calls = git_reads(monkeypatch, b"h file\0")
    assert safety().check_hidden_index(tmp_path).state == "hidden"
    assert len(calls) == 2 and all(call[2] == str(tmp_path) for call in calls)
    assert "--show-toplevel" in calls[0] and "ls-files" in calls[1]


@pytest.mark.parametrize("tag,state", [("S", "hidden"), ("h", "hidden")])
def test_final_dangling_link_counts_as_present(tmp_path, monkeypatch, tag, state):
    git_reads(monkeypatch, tag.encode() + b" link\0")
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == tmp_path / "link":
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    assert safety().check_hidden_index(tmp_path).state == state


def test_dangling_ancestor_is_not_sparse_absence(tmp_path, monkeypatch):
    git_reads(monkeypatch, b"S link/child\0")
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == tmp_path / "link":
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


def test_large_omitted_index_resolves_root_once_and_uses_one_index_read(tmp_path, monkeypatch, record_property):
    count = 4096
    calls = git_reads(monkeypatch, b"".join(f"S omitted/{number}\0".encode() for number in range(count)))
    lstat, probes = os.lstat, []

    def metadata(path, *args, **kwargs):
        probes.append(path)
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", metadata)
    started = time.perf_counter()
    assert safety().check_hidden_index(tmp_path).state == "safe"
    elapsed = time.perf_counter() - started
    assert len(calls) == 2
    assert sum("--show-toplevel" in call for call in calls) == 1
    assert sum("ls-files" in call for call in calls) == 1
    assert len(probes) == count * 2
    record_property("omitted_entries", count)
    record_property("git_commands", len(calls))
    record_property("metadata_probes", len(probes))
    record_property("observed_elapsed_seconds", elapsed)


@pytest.mark.skipif(os.name != "nt", reason="Windows requires a lossless Unicode pathname")
def test_invalid_windows_encoding_is_unverifiable(tmp_path, monkeypatch):
    git_reads(monkeypatch, b"S invalid-\xff\0")
    assert safety().check_hidden_index(tmp_path).state == "unverifiable"


def test_unreadable_gitlink_directory_is_unverifiable(tmp_path, monkeypatch):
    (tmp_path / "module").mkdir()
    git_reads(monkeypatch, stage=b"160000 " + b"a" * 40 + b" 0\tmodule\0")
    real = os.scandir

    def scandir(path):
        if Path(path) == tmp_path / "module":
            raise PermissionError("cannot inspect nested work")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert safety().check_materialized_submodules(tmp_path).state == "unverifiable"


@pytest.mark.parametrize("listing,clear_steps", [(b"h tracked\0", True), (b"H tracked", False)])
def test_recovery_commands_are_separate_from_stored_cleanup_reason(tmp_path, monkeypatch, capsys, listing, clear_steps):
    target = tmp_path / "folder & command"
    target.mkdir()
    git_reads(monkeypatch, listing)
    monkeypatch.setattr(cli, "_git", lambda root, argv: (0, str(target) if argv[0] == "rev-parse" else ""))
    rc, _, reason = cli._git_write(tmp_path, ["worktree", "remove", "--", str(target)])
    assert rc == 1 and "was not removed" in reason
    assert "git -C" not in reason and len(reason) < 500
    note = capsys.readouterr().err
    assert "`git -C <folder> ls-files -v`" in note and "not a ready command" in note
    assert ("--no-assume-unchanged -- <file>" in note) is clear_steps
    assert ("--no-skip-worktree -- <file>" in note) is clear_steps


@pytest.mark.parametrize("required", ["core.fsmonitor=false", "core.untrackedCache=false"])
@pytest.mark.parametrize("site", ["cli", "janitor", "post-commit"])
def test_status_bypasses_a_plausible_cached_clean_answer(tmp_path, monkeypatch, required, site):
    (tmp_path / ".git").touch()

    def run(argv, **kwargs):
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(argv[2]) + b"\n", b"")
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "status" in argv:
            output = " M tracked\n" if required in argv else ""
            return subprocess.CompletedProcess(argv, 0, output, "")
        assert "add" in argv or "commit" in argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(janitor.shutil, "which", lambda _: "git")
    monkeypatch.setattr(janitor, "worktree_branch", lambda _: "feature")
    if site == "cli":
        assert cli._worktree_clean(tmp_path) is False
    elif site == "janitor":
        assert janitor.is_dirty_worktree(tmp_path) is True
    else:
        result = janitor.wip_commit_dirty_worktree(tmp_path, default_branches=["main"])
        assert result.refused and "still dirty after commit" in result.message
