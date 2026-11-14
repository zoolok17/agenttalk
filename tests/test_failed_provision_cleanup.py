"""Failed worktree removal must retain the branch that checkout still uses."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli


@pytest.mark.parametrize("failure", ["dirty", "unreadable", "exception"])
def test_failed_removal_keeps_branch_and_explains_why(tmp_path, monkeypatch, failure):
    writes, reads = [], []
    (tmp_path / "checkout").mkdir()

    def write(root, argv):
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            if failure == "exception":
                raise cli.GitWriteError("could not start removal")
            return 1, "", failure
        return 0, "", ""

    def read(root, argv):
        reads.append(argv)
        return 0, "a" * 40

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", read)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout",
        cleanup_branch=True,
    )
    assert writes == [["worktree", "remove", "--", str(tmp_path / "checkout")]]
    assert reads == []
    reason = "could not start removal" if failure == "exception" else failure
    assert note.startswith(f"worktree cleanup failed: {reason}; branch kept: the worktree was not removed;")
    assert str(tmp_path / "checkout") in note and "save or move any work" in note
    assert "git -C <repo> worktree remove -- <folder>" in note
    assert "git -C <repo> branch -D -- <branch>" in note and "lane/retained" in note
    assert "not a ready command" in note and "or assign a new lane id" in note


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("tip", ["base", "changed"])
def test_successful_or_unneeded_removal_keeps_existing_branch_tip_rule(tmp_path, monkeypatch, created, tip):
    writes = []

    def write(root, argv):
        writes.append(argv)
        return 0, "", ""

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", lambda root, argv: (0, ("a" if tip == "base" else "b") * 40))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout" if created else None,
        cleanup_branch=True,
    )
    expected = [["worktree", "remove", "--", str(tmp_path / "checkout")]] if created else []
    if tip == "base":
        expected.append(["update-ref", "-d", "refs/heads/lane/retained"])
        assert note is None
    else:
        assert note == "branch cleanup skipped: lane branch tip changed after failed provision"
    assert writes == expected


def test_unowned_branch_is_not_changed_or_reported_as_ours(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_git_write", lambda root, argv: (1, "", "refused"))

    def unexpected(*args):
        pytest.fail("branch inspection must remain disabled")

    monkeypatch.setattr(cli, "_git", unexpected)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=Path(tmp_path / "checkout"),
        cleanup_branch=False,
    )
    assert note == "worktree cleanup failed: refused"


@pytest.mark.parametrize("failure", ["refused", "exception"])
def test_no_entry_after_failed_removal_allows_base_branch_cleanup(tmp_path, monkeypatch, failure):
    target = tmp_path / "checkout"
    target.mkdir()
    writes = []

    def write(root, argv):
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            target.rmdir()  # absence must be checked after the attempt, not before it
            if failure == "exception":
                raise cli.GitWriteError("removal exception")
            return 1, "", "removal refused"
        return 0, "", ""

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", lambda root, argv: (0, "a" * 40))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
    )
    assert writes == [
        ["worktree", "remove", "--", str(target)],
        ["update-ref", "-d", "refs/heads/lane/retained"],
    ]
    reason = "removal exception" if failure == "exception" else "removal refused"
    assert note == f"worktree cleanup failed: {reason}"


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O failure"), NotADirectoryError("blocked")])
def test_uncertain_entry_after_failed_removal_keeps_branch(tmp_path, monkeypatch, error):
    writes = []

    def write(root, argv):
        writes.append(argv)
        return 1, "", "removal refused"

    def entry(path):
        raise error

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_lane_entry_present", entry)
    monkeypatch.setattr(cli, "_git", lambda *args: pytest.fail("uncertain path must keep its branch"))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout",
        cleanup_branch=True,
    )
    assert writes == [["worktree", "remove", "--", str(tmp_path / "checkout")]]
    assert note.startswith("worktree cleanup failed: removal refused; branch kept: the worktree was not removed;")
    assert str(tmp_path / "checkout") in note and "lane/retained" in note
    assert "save or move any work" in note and "git -C <repo> worktree remove -- <folder>" in note


def test_kept_branch_recovery_commands_do_not_embed_shell_metacharacters(tmp_path, monkeypatch):
    target = tmp_path / "kept folder; ' & $(x)\nnext"
    monkeypatch.setattr(cli, "_git_write", lambda *args: (1, "", "refused"))
    monkeypatch.setattr(cli, "_lane_entry_present", lambda path: True)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="recover-me",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
    )
    assert "\n" not in note
    assert cli.pc.line(str(target)) in note
    assert cli.pc.shown(str(target), cli.pc.PATH_SHOWN_MAX_CHARACTERS) in note
    assert '"lane/recover-me"' in note
    commands = [piece for index, piece in enumerate(note.split("`")) if index % 2]
    assert commands == ["git -C <repo> worktree remove -- <folder>", "git -C <repo> branch -D -- <branch>"]
    assert "not a ready command" in note
