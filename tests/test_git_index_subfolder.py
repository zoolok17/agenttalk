"""Index inspection covers the checkout when the store is in a subfolder (#807)."""

import os
import subprocess

import pytest

from agenttalk import cli, close, git_index_safety as safety, printed_commands as pc
from agenttalk.store import Store


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


@pytest.fixture
def subfolder_store(tmp_path):
    root = tmp_path / "checkout"
    sub = root / "sub"
    sub.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Synthetic Author")
    git(root, "config", "user.email", "synthetic@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    (sub / "a.txt").write_bytes(b"inside\n")
    (root / "outside.txt").write_bytes(b"outside\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    store = Store(sub)
    store.init(["lead"])
    store.set_role("lead", "lead")
    return root, store, git(root, "rev-parse", "HEAD").strip()


def run_close(store, revision, action):
    args = ["--root", str(store.root), "close", action, "--id", "nested", "--from", "lead", "--revision", revision]
    if action == "open":
        args += ["--scope", "release"]
    assert cli.main(args) == 0
    return close.load_close(store, "nested")


@pytest.mark.parametrize("action", ["open", "reopen"])
@pytest.mark.parametrize("case", ["assume-inside", "assume-outside", "skip-inside"])
def test_close_subfolder_refuses_hidden_work(subfolder_store, capsys, action, case):
    root, store, revision = subfolder_store
    if action == "reopen":
        assert run_close(store, revision, "open")["revision_clean"] is True
    capsys.readouterr()
    relative = "outside.txt" if case == "assume-outside" else "sub/a.txt"
    option = "--skip-worktree" if case == "skip-inside" else "--assume-unchanged"
    git(root, "update-index", option, "--", relative)
    if case.startswith("assume"):
        (root / relative).write_bytes(b"hidden edit\n")
    before = (root / relative).read_bytes()
    listing = git(root, "ls-files", "-v", "-z", "--full-name")
    assert ("S " if case == "skip-inside" else "h ") + relative + "\0" in listing
    assert git(store.root, *safety.STATUS_ARGS).strip() == ""

    record = run_close(store, revision, action)

    assert record["revision_clean"] is False
    error = capsys.readouterr().err
    assert safety.HIDDEN in error and "this checkout is not recorded as clean" in error
    assert "git -C <folder> ls-files -v" in error
    assert pc.shown(str(root), limit=pc.PATH_SHOWN_MAX_CHARACTERS) in error
    assert pc.shown(str(store.root), limit=pc.PATH_SHOWN_MAX_CHARACTERS) not in error
    assert (root / relative).read_bytes() == before
    assert git(root, "ls-files", "-v", "-z", "--full-name") == listing


@pytest.mark.parametrize("action", ["open", "reopen"])
def test_clean_subfolder_records_clean(subfolder_store, capsys, action):
    _, store, revision = subfolder_store
    if action == "reopen":
        assert run_close(store, revision, "open")["revision_clean"] is True
    capsys.readouterr()
    assert run_close(store, revision, action)["revision_clean"] is True
    assert "not recorded as clean" not in capsys.readouterr().err


@pytest.mark.parametrize("relative", ["sub/module", "outside-module"])
def test_subfolder_inspection_keeps_materialized_gitlinks(subfolder_store, relative):
    root, store, revision = subfolder_store
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{revision},{relative}")
    git(root, "commit", "-qm", "gitlink")
    folder = root / relative
    folder.mkdir()
    precious = folder / "precious.txt"
    precious.write_bytes(b"unpopulated submodule work\n")
    assert safety.check_materialized_submodules(store.root).state == "hidden"
    assert precious.read_bytes() == b"unpopulated submodule work\n"


@pytest.mark.parametrize("action", ["open", "reopen"])
def test_failed_top_level_resolution_records_false(subfolder_store, monkeypatch, capsys, action):
    root, store, revision = subfolder_store
    if action == "reopen":
        run_close(store, revision, "open")
    capsys.readouterr()
    real = subprocess.run

    def failed(args, **kwargs):
        if "--show-toplevel" in args:
            return subprocess.CompletedProcess(args, 128, os.fsencode(root) + b"\n", b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failed)
    assert run_close(store, revision, action)["revision_clean"] is False
    error = capsys.readouterr().err
    assert safety.UNKNOWN in error
    assert "--show-toplevel" in error
    assert "--no-assume-unchanged" not in error
