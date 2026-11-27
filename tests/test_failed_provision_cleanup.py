"""Failed worktree removal must retain the branch that checkout still uses."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli


def _no_registration(root, argv):
    if argv == ["worktree", "list", "--porcelain", "-z"]:
        return 0, f"worktree {root}\0HEAD {'a' * 40}\0\0"
    return 0, "a" * 40


def _after_owned_checkout(root, target, lane_id, after):
    """Supply ownership before the existing removal-outcome scenario."""
    first = True

    def read(repo, argv):
        nonlocal first
        if first and argv == ["worktree", "list", "--porcelain", "-z"]:
            first = False
            return 0, f"worktree {target.as_posix()}\0branch {cli.lane_mod.lane_ref(lane_id)}\0\0"
        return after(repo, argv)

    return read


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
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, tmp_path / "checkout", "retained", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout",
        cleanup_branch=True,
    )
    assert writes == [["worktree", "remove", "--", str(tmp_path / "checkout")]]
    assert reads == [["worktree", "list", "--porcelain", "-z"]]
    reason = "could not start removal" if failure == "exception" else failure
    assert note.startswith(f"worktree cleanup failed: {reason}; branch kept: the worktree was not removed;")
    assert str(tmp_path / "checkout") in note and "save or move any work" in note
    assert "git -C <repo> -c status.showUntrackedFiles=all worktree remove -- <folder>" in note
    assert "git -C <repo> branch -d -- <branch>" in note and "lane/retained" in note
    assert "not a ready command" in note and "or assign a new lane id" in note


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("tip", ["base", "changed"])
def test_successful_or_unneeded_removal_keeps_existing_branch_tip_rule(tmp_path, monkeypatch, created, tip):
    writes = []
    if created:
        (tmp_path / "checkout").mkdir()

    def write(root, argv):
        writes.append(argv)
        return 0, "", ""

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(
        cli,
        "_git",
        lambda root, argv: (
            _no_registration(root, argv) if argv[0] == "worktree" else (0, ("a" if tip == "base" else "b") * 40)
        ),
    )
    if created:
        monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, tmp_path / "checkout", "retained", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout" if created else None,
        cleanup_branch=True,
    )
    expected = [["worktree", "remove", "--", str(tmp_path / "checkout")]] if created else []
    if tip == "base":
        expected.append(["branch", "-D", "--", "lane/retained"])
        assert note is None
    else:
        assert note == "branch cleanup skipped: lane branch tip changed after failed provision"
    assert writes == expected


def test_unowned_branch_is_not_changed_or_reported_as_ours(tmp_path, monkeypatch):
    (tmp_path / "checkout").mkdir()
    monkeypatch.setattr(cli, "_git_write", lambda root, argv: (1, "", "refused"))

    def unexpected(root, argv):
        assert argv == ["worktree", "list", "--porcelain", "-z"]
        return -1, ""

    monkeypatch.setattr(cli, "_git", unexpected)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=Path(tmp_path / "checkout"),
        cleanup_branch=False,
    )
    assert note is None  # unreadable ownership prevents even attempting removal


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
    monkeypatch.setattr(cli, "_git", _no_registration)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "retained", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
    )
    assert writes == [
        ["worktree", "remove", "--", str(target)],
        ["branch", "-D", "--", "lane/retained"],
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
    monkeypatch.setattr(cli, "_git", _no_registration)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, tmp_path / "checkout", "retained", cli._git))
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
    assert (
        "save or move any work" in note
        and "git -C <repo> -c status.showUntrackedFiles=all worktree remove -- <folder>" in note
    )


def test_kept_branch_recovery_commands_do_not_embed_shell_metacharacters(tmp_path, monkeypatch):
    target = tmp_path / "kept folder; ' & $(x)\nnext"
    monkeypatch.setattr(cli, "_git_write", lambda *args: (1, "", "refused"))
    monkeypatch.setattr(cli, "_lane_entry_present", lambda path: True)
    monkeypatch.setattr(cli, "_git", _no_registration)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "recover-me", cli._git))
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
    assert commands == [
        "git -C <repo> -c status.showUntrackedFiles=all worktree remove -- <folder>",
        "git -C <repo> rev-parse --verify <branch>",
        "git -C <repo> branch -D -- <branch>",
        "git -C <repo> branch -d -- <branch>",
    ]
    assert "not a ready command" in note


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("result", ["removed", "not-registered", "other-refusal"])
def test_absent_checkout_reaches_git_and_only_expected_refusal_is_quiet(tmp_path, monkeypatch, relative, result):
    root = tmp_path / "repo"
    root.mkdir()
    cwd = root / "subfolder"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    writes = []

    def write(path, argv):
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            if result == "not-registered":
                return 128, "", f"fatal: '{root / 'checkout'}' is not a working tree\n"
            if result == "other-refusal":
                return 128, "", "fatal: permission to remove was refused"
        return 0, "", ""

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", _no_registration)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, root / "checkout", "absent", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=root),
        lane_id="absent",
        base_sha="a" * 40,
        created_path=Path("checkout") if relative else root / "checkout",
        cleanup_branch=True,
    )
    assert writes == [
        ["worktree", "remove", "--", str(root / "checkout")],
        ["branch", "-D", "--", "lane/absent"],
    ]
    assert note == (
        "worktree cleanup failed: fatal: permission to remove was refused" if result == "other-refusal" else None
    )


@pytest.mark.parametrize("location", ["subfolder", "elsewhere"])
def test_relative_checkout_is_inspected_and_removed_against_store_root(tmp_path, monkeypatch, location):
    root = tmp_path / "repo"
    target = root / "checkout"
    target.mkdir(parents=True)
    cwd = root / "subfolder" if location == "subfolder" else tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    writes, reads = [], []

    def write(path, argv):
        writes.append(argv)
        return (1, "", "dirty checkout") if argv[:2] == ["worktree", "remove"] else (0, "", "")

    def read(*args):
        reads.append(args)
        return 0, "a" * 40

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "relative", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=root),
        lane_id="relative",
        base_sha="a" * 40,
        created_path=Path("checkout"),
        cleanup_branch=True,
    )
    assert writes == [["worktree", "remove", "--", str(target)]]
    assert reads == [(root, ["worktree", "list", "--porcelain", "-z"])]
    assert "branch kept:" in note and str(target) in note


@pytest.mark.parametrize(
    "case",
    ["native-path", "git-path", "other-path", "extra-error", "extra-output", "empty-error", "present", "unknown"],
)
def test_not_registered_note_requires_confirmed_absence_and_only_that_git_error(tmp_path, monkeypatch, case):
    target = tmp_path / "checkout"
    if case == "present":
        target.mkdir()
    if case == "unknown":

        def unknown(path):
            raise PermissionError("cannot inspect target")

        monkeypatch.setattr(cli, "_lane_entry_present", unknown)
    shown_path = target.as_posix() if case == "git-path" else str(target)
    if case == "other-path":
        shown_path = str(tmp_path / "unrelated")
    err = f"fatal: '{shown_path}' is not a working tree"
    if case == "extra-error":
        err += "\nfatal: another error"
    if case == "empty-error":
        err = ""
    writes = []

    def write(root, argv):
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            return 128, "extra output" if case == "extra-output" else "", err
        return 0, "", ""

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", _no_registration)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "quiet", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="quiet",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
    )
    assert writes[0] == ["worktree", "remove", "--", str(target)]
    if case in {"native-path", "git-path"}:
        assert note is None
    else:
        assert note.startswith(f"worktree cleanup failed: {cli.pc.line(err or str(128))}")
    if case in {"present", "unknown"}:
        assert len(writes) == 1 and "branch kept:" in note
    else:
        assert writes[-1] == ["branch", "-D", "--", "lane/quiet"]


@pytest.mark.parametrize(
    "case",
    [
        "locked",
        "locked-no-reason",
        "registered",
        "failed",
        "empty",
        "truncated",
        "malformed",
        "relative",
        "replacement",
        "gone",
        "branch-elsewhere",
        "different-branch",
    ],
)
@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("entry", ["missing", "present", "unknown"])
def test_checkout_lock_detection_and_deregistration_requirements(tmp_path, monkeypatch, case, relative, entry):
    root = tmp_path / "repo"
    cwd = root / "subfolder"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    target = root / "checkout"
    if entry == "present":
        target.mkdir()
    elif entry == "unknown":

        def unavailable(path):
            raise PermissionError("cannot inspect checkout")

        monkeypatch.setattr(cli, "_lane_entry_present", unavailable)
    writes, reads = [], []
    listing = f"worktree {root}\0HEAD {'a' * 40}\0\0"
    if case in {"locked", "locked-no-reason", "registered"}:
        # Slash spelling and a dot component must still identify the same folder.
        listing += f"worktree {target.as_posix()}/.\0HEAD {'a' * 40}\0"
        if case.startswith("locked"):
            listing += "locked initializing\0" if case == "locked" else "locked\0"
        listing += "\0"
    moved = root / "moved folder; value\nnext"
    if case in {"branch-elsewhere", "different-branch"}:
        ref = cli.lane_mod.lane_ref("locked" if case == "branch-elsewhere" else "different")
        listing += f"worktree {moved.as_posix()}\0HEAD {'a' * 40}\0branch {ref}\0\0"
    if case == "empty":
        listing = ""
    if case == "truncated":
        listing = listing[:-1]
    if case == "malformed":
        listing = "HEAD abc\0\0"
    if case == "relative":
        listing = "worktree checkout\0\0"
    if case == "replacement":
        listing = f"worktree {root.as_posix()}/\ufffd\0\0"

    def write(repo, argv):
        writes.append(argv)
        return (0, "", "") if argv[:2] == ["branch", "-D"] else (128, "", "fatal: removal refused")

    def read(repo, argv):
        reads.append(argv)
        if argv == ["worktree", "list", "--porcelain", "-z"]:
            return (-1 if case == "failed" else 0), listing
        assert argv == ["rev-parse", "--verify", "refs/heads/lane/locked^{commit}"]
        return 0, "a" * 40

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "locked", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=root),
        lane_id="locked",
        base_sha="a" * 40,
        created_path=Path("checkout") if relative else target,
        cleanup_branch=True,
    )
    assert reads[0] == ["worktree", "list", "--porcelain", "-z"]
    assert note.startswith("worktree cleanup failed: fatal: removal refused")
    if case in {"gone", "different-branch"} and entry == "missing":
        assert writes[-1] == ["branch", "-D", "--", "lane/locked"]
        assert "branch kept:" not in note
    else:
        assert writes == [["worktree", "remove", "--", str(target)]]
        assert "branch kept:" in note
    if case.startswith("locked"):
        assert "Git still has a locked record" in note
        assert "git -C <repo> worktree unlock -- <folder>" in note
        assert note.index("worktree unlock") < note.index("worktree remove -- <folder>")
    else:
        assert "worktree unlock" not in note

    if case == "branch-elsewhere":
        assert "the lane branch is checked out at " + cli.pc.line(str(moved)) in note
        assert "\n" not in note
        assert "worktree remove" not in note and "branch -d" not in note


@pytest.mark.parametrize("stage", ["removal", "removal-exception", "branch", "branch-exception"])
@pytest.mark.parametrize("separator", ["\n", "\r\n", "\u2028"])
def test_git_cleanup_diagnostics_stay_on_one_line_with_recovery_steps(tmp_path, monkeypatch, stage, separator):
    target = tmp_path / "checkout"
    target.mkdir()
    error = f"fatal: checkout is locked{separator}hint: unlock it before removing"
    writes = []

    def write(root, argv):
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            if stage == "removal-exception":
                raise cli.GitWriteError(error)
            return (128, "", error) if stage == "removal" else (0, "", "")
        if stage == "branch-exception":
            raise cli.GitWriteError(error)
        return 128, "", error

    def read(root, argv):
        if argv == ["worktree", "list", "--porcelain", "-z"]:
            if stage.startswith("branch"):
                return _no_registration(root, argv)
            return 0, f"worktree {target}\0HEAD {'a' * 40}\0branch refs/heads/lane/diagnostic\0locked initializing\0\0"
        assert argv == ["rev-parse", "--verify", "refs/heads/lane/diagnostic^{commit}"]
        return 0, "a" * 40

    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "diagnostic", cli._git))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="diagnostic",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
    )
    assert note and len(note.splitlines()) == 1
    assert cli.pc.line(error) in note
    if stage.startswith("removal"):
        assert "worktree cleanup failed:" in note and "branch kept:" in note
        assert "git -C <repo> worktree unlock -- <folder>" in note
        assert "git -C <repo> -c status.showUntrackedFiles=all worktree remove -- <folder>" in note
        assert "git -C <repo> branch -d -- <branch>" in note
        assert writes == [["worktree", "remove", "--", str(target)]]
    else:
        assert note.startswith("branch kept: Git refused branch cleanup:")
        assert writes[-1] == ["branch", "-D", "--", "lane/diagnostic"]


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("after", ["second-lane", "different", "unreadable", "replacement"])
def test_branch_users_are_checked_even_after_successful_removal(tmp_path, monkeypatch, created, after):
    target = tmp_path / "checkout"
    other = tmp_path / "second checkout"
    if created:
        target.mkdir()
    writes = []
    removed = False

    def listing(root, argv):
        if argv != ["worktree", "list", "--porcelain", "-z"]:
            assert argv == ["rev-parse", "--verify", "refs/heads/lane/ours^{commit}"]
            return 0, "a" * 40
        records = f"worktree {root.as_posix()}\0HEAD {'a' * 40}\0\0"
        if created and not removed:
            return 0, records + f"worktree {target.as_posix()}\0branch refs/heads/lane/ours\0\0"
        if after == "unreadable":
            return -1, ""
        if after == "replacement":
            return 0, f"worktree {root.as_posix()}/\ufffd\0\0"
        ref = "lane/ours" if after == "second-lane" else "other"
        return 0, records + f"worktree {other.as_posix()}\0branch refs/heads/{ref}\0\0"

    def write(root, argv):
        nonlocal removed
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            target.rmdir()
            removed = True
        return 0, "", ""

    monkeypatch.setattr(cli, "_git", listing)
    monkeypatch.setattr(cli, "_git_write", write)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="ours",
        base_sha="a" * 40,
        created_path=target if created else None,
        cleanup_branch=True,
    )
    deleted = [argv for argv in writes if argv[:2] == ["branch", "-D"]]
    assert bool(deleted) == (after == "different")
    if after == "second-lane":
        assert "the lane branch is checked out at " + cli.pc.line(str(other)) in note
    elif after != "different":
        assert "branch kept:" in note


@pytest.mark.parametrize("owner", ["ours", "near-miss", "other", "detached", "unlisted", "unreadable"])
@pytest.mark.parametrize("relative", [False, True])
def test_only_our_registered_checkout_is_removed(tmp_path, monkeypatch, owner, relative):
    target = tmp_path / "checkout"
    target.mkdir()
    precious = target / "precious.txt"
    precious.write_bytes(b"keep these bytes")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    writes = []
    removed = False

    def read(root, argv):
        if argv != ["worktree", "list", "--porcelain", "-z"]:
            return 0, "a" * 40
        records = f"worktree {root.as_posix()}\0HEAD {'a' * 40}\0\0"
        if owner == "unreadable":
            return -1, ""
        if removed or owner == "unlisted":
            return 0, records
        field = (
            "detached"
            if owner == "detached"
            else "branch refs/heads/" + ({"ours": "lane/ours", "near-miss": "lane/ours-2"}.get(owner, "other"))
        )
        return 0, records + f"worktree {target.as_posix()}\0HEAD {'a' * 40}\0{field}\0\0"

    def write(root, argv):
        nonlocal removed
        writes.append(argv)
        if argv[:2] == ["worktree", "remove"]:
            precious.unlink()
            target.rmdir()
            removed = True
        return 0, "", ""

    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(cli, "_git_write", write)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="ours",
        base_sha="a" * 40,
        created_path=Path("checkout") if relative else target,
        cleanup_branch=True,
    )
    if owner == "ours":
        assert removed and not target.exists() and note is None
    else:
        assert writes == [] and precious.read_bytes() == b"keep these bytes"
        assert "branch kept:" in note and "ownership" in note and cli.pc.line(str(target)) in note
        assert "worktree remove" not in note


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("shape", ["other", "detached", "unlisted", "unreadable"])
def test_v3_unowned_checkout_is_named_and_never_removed(tmp_path, monkeypatch, present, shape):
    target = tmp_path / "checkout"
    if present:
        target.mkdir()
    records = f"worktree {tmp_path.as_posix()}\0HEAD {'a' * 40}\0\0"
    if shape in {"other", "detached"}:
        field = "branch refs/heads/operator-other" if shape == "other" else "detached"
        records += f"worktree {target.as_posix()}\0HEAD {'a' * 40}\0{field}\0\0"
    writes = []
    monkeypatch.setattr(
        cli,
        "_git",
        lambda root, argv: (
            (-1, "") if shape == "unreadable" else (0, records) if argv[0] == "worktree" else (0, "a" * 40)
        ),
    )
    monkeypatch.setattr(cli, "_git_write", lambda root, argv: writes.append(argv) or (0, "", ""))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path), lane_id="ours", base_sha="a" * 40, created_path=target, cleanup_branch=True
    )
    if not present and shape == "unlisted":
        assert writes == [["branch", "-D", "--", "lane/ours"]] and note is None
        return
    assert writes == []
    expected = {
        "other": "refs/heads/operator-other",
        "detached": "detached checkout",
        "unlisted": "not a registered checkout",
        "unreadable": "could not read Git's checkout records",
    }[shape]
    assert "branch kept:" in note and expected in note and cli.pc.line(str(target)) in note
    assert "worktree remove" not in note
    if present:
        assert "the folder was left in place" in note and "nothing is at the planned folder" not in note
    else:
        assert "nothing is at the planned folder" in note and "the folder was left in place" not in note


def test_v3_branch_delete_uses_git_refusal_after_last_listing(tmp_path, monkeypatch):
    writes = []
    branch_present = True
    monkeypatch.setattr(cli, "_git", _no_registration)

    def write(root, argv):
        nonlocal branch_present
        writes.append(argv)
        # Simulate a checkout appearing after the read. Only Git's branch delete refuses it.
        if argv == ["branch", "-D", "--", "lane/ours"]:
            return 1, "", "error: branch is checked out\nhint: keep it"
        assert argv == ["update-ref", "-d", "refs/heads/lane/ours"]
        branch_present = False
        return 0, "", ""

    monkeypatch.setattr(cli, "_git_write", write)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="ours",
        base_sha="a" * 40,
        created_path=None,
        cleanup_branch=True,
    )
    assert branch_present
    assert writes == [["branch", "-D", "--", "lane/ours"]]
    assert "branch kept:" in note and "checked out" in note and len(note.splitlines()) == 1


@pytest.mark.parametrize("tip", ["base", "changed"])
@pytest.mark.parametrize("listing", ["none", "detached-elsewhere", "lane-elsewhere", "unreadable-after"])
def test_absent_unregistered_path_allows_only_unused_base_branch_cleanup(tmp_path, monkeypatch, tip, listing):
    target, other = tmp_path / "never-created", tmp_path / "moved"
    reads, writes = [], []

    def read(root, argv):
        reads.append(argv)
        if argv[0] != "worktree":
            return 0, ("a" if tip == "base" else "b") * 40
        if listing == "unreadable-after" and len(reads) == 2:
            return -1, ""
        records = f"worktree {root.as_posix()}\0HEAD {'a' * 40}\0\0"
        if listing in {"detached-elsewhere", "lane-elsewhere"}:
            field = "detached" if listing == "detached-elsewhere" else "branch refs/heads/lane/ours"
            records += f"worktree {other.as_posix()}\0HEAD {'a' * 40}\0{field}\0\0"
        return 0, records

    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(cli, "_git_write", lambda root, argv: writes.append(argv) or (0, "", ""))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path), lane_id="ours", base_sha="a" * 40, created_path=target, cleanup_branch=True
    )
    assert not target.exists()
    assert reads[:2] == [["worktree", "list", "--porcelain", "-z"]] * 2
    deleted = tip == "base" and listing in {"none", "detached-elsewhere"}
    assert writes == ([["branch", "-D", "--", "lane/ours"]] if deleted else [])
    if deleted:
        assert note is None
    else:
        assert note and "folder was left in place" not in note


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O failure"), NotADirectoryError("blocked")])
def test_unregistered_path_with_lstat_error_keeps_branch(tmp_path, monkeypatch, error):
    target = tmp_path / "uncertain"
    writes = []

    def entry(path):
        raise error

    monkeypatch.setattr(cli, "_lane_entry_present", entry)
    monkeypatch.setattr(cli, "_git", _no_registration)
    monkeypatch.setattr(cli, "_git_write", lambda root, argv: writes.append(argv) or (0, "", ""))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path), lane_id="ours", base_sha="a" * 40, created_path=target, cleanup_branch=True
    )
    assert writes == [] and "branch kept:" in note and "ownership" in note


@pytest.mark.parametrize(
    "failure",
    [
        "timeout", "unreaped-timeout", "reap-oserror", "wait-oserror", "count-fails", "fast", "timeout-text",
        "verify-timeout",
    ],
)
def test_assignment_add_timeout_keeps_checkout_and_branch(tmp_path, monkeypatch, capsys, failure):
    import contextlib
    import subprocess

    target = tmp_path / "checkout ; & $"
    store = SimpleNamespace(
        root=tmp_path,
        load_config=lambda: {"agents": ["lead", "dev"]},
        current_epoch=lambda: 0,
        _config_lock=contextlib.nullcontext,
    )
    state = {"branch": False, "registered": False}
    writes = []
    monkeypatch.setattr(cli, "_get_store", lambda args: store)
    monkeypatch.setattr(
        cli,
        "_load_domain_registry",
        lambda store: SimpleNamespace(
            data={"domains": {"core": {}}},
            registry_hash="a" * 64,
        ),
    )
    monkeypatch.setattr(cli, "_lane_resolve", lambda store, ref: "a" * 40)
    monkeypatch.setattr(cli, "_lane_assignment_fingerprint", lambda store: ())
    monkeypatch.setattr(cli, "_prepare_worktrees_root", lambda store, configured: tmp_path)
    monkeypatch.setattr(cli, "_mint_worktree_path", lambda *args: target)
    monkeypatch.setattr(cli.lane_mod, "load_lanes", lambda store: {"lanes": {}})
    saved = []
    monkeypatch.setattr(cli.lane_mod, "save_lanes", lambda *args: saved.append(args))

    def read(root, argv):
        if argv == ["worktree", "list", "--porcelain", "-z"]:
            listing = f"worktree {tmp_path.as_posix()}\0HEAD {'a' * 40}\0\0"
            if state["registered"]:
                listing += f"worktree {target.as_posix()}\0branch refs/heads/lane/slow\0\0"
            return 0, listing
        assert argv == ["rev-parse", "--verify", "refs/heads/lane/slow^{commit}"]
        return (0, "a" * 40) if state["branch"] else (1, "")

    class TimedOutGit:
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1 or failure in {"unreaped-timeout", "wait-oserror"}:
                raise subprocess.TimeoutExpired("git", timeout)
            if failure == "reap-oserror":
                raise OSError("simulated pipe failure")
            return "", ""

        def kill(self):
            pass

        def wait(self, timeout=None):
            if failure == "wait-oserror":
                raise OSError("simulated wait failure")
            return -9

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: TimedOutGit())
    real_write = cli._git_write

    def write(root, argv):
        writes.append(argv)
        if argv[:2] == ["worktree", "add"]:
            state["branch"] = True
            if failure == "fast":
                raise cli.GitWriteError("fast add failure")
            target.mkdir()
            (target / "tracked.txt").write_bytes(b"base\n")
            state["registered"] = True
            if failure == "timeout-text":
                raise cli.GitWriteError("mutating git timed out in unrelated diagnostic text")
            if failure == "verify-timeout":
                return 0, "", ""
            if failure == "count-fails":  # #920: Git's Windows job, whose live count cannot be read after the time-out
                from _git_job_fake import FakeWorld

                from agenttalk import git_job

                with pytest.MonkeyPatch.context() as patch:
                    FakeWorld().install(patch, (("timeout",), ("return", "", "")))
                    patch.setattr(git_job.GitJob, "count_after_stop", lambda job, deadline: 1 / 0)
                    return real_write(root, argv, timeout=0.01)
            return real_write(root, argv, timeout=0.01)
        if argv[:2] == ["worktree", "remove"]:
            (target / "tracked.txt").unlink()
            target.rmdir()
            state["registered"] = False
        else:
            assert argv == ["branch", "-D", "--", "lane/slow"]
            state["branch"] = False
        return 0, "", ""

    def verify(*args, **kwargs):
        try:
            raise subprocess.TimeoutExpired("verification", 1)
        except subprocess.TimeoutExpired as exc:
            raise cli.GitWriteError("verification failed") from exc

    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(cli, "_git_write", write)
    monkeypatch.setattr(cli, "_verify_lane_worktree", verify)
    args = SimpleNamespace(
        lane_cmd="assign",
        id="slow",
        actor="lead",
        assignee="dev",
        domain="core",
        base="main",
        target="main",
        path=["src/"],
        force=False,
        notes=None,
    )
    assert cli.cmd_lane(args) == 2
    error = capsys.readouterr().err
    assert saved == []
    if failure in {"timeout", "unreaped-timeout", "reap-oserror", "wait-oserror", "count-fails"}:
        assert len(writes) == 1
        assert state == {"branch": True, "registered": True}
        assert (target / "tracked.txt").read_bytes() == b"base\n"
        assert "setup timed out" in error and "hook or checkout may still be running" in error
        assert "wait until nothing is running in " + cli.pc.line(str(target)) in error
        assert "save or move any work" in error
        assert "git -C <repo> -c status.showUntrackedFiles=all worktree remove -- <folder>" in error
        assert "git -C <repo> branch -d -- <branch>" in error
        assert "not a ready command" in error
        assert "if Git still lists the checkout as locked once nothing is running, first run" in error
        assert "git -C <repo> worktree unlock -- <folder>" in error
    else:
        assert not target.exists() and state == {"branch": False, "registered": False}
        assert writes[-1] == ["branch", "-D", "--", "lane/slow"]
        assert "hook or checkout may still be running" not in error


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("record", ["locked", "unlocked", "unreadable"])
def test_timeout_always_prints_conditional_unlock_and_never_mutates(tmp_path, monkeypatch, present, record):
    target = tmp_path / "checkout ; & $"
    if present:
        target.mkdir()
        (target / "precious.txt").write_bytes(b"keep these bytes")
    listing = f"worktree {target.as_posix()}\0branch refs/heads/lane/ours\0"
    listing += "locked initializing\0\0" if record == "locked" else "\0"
    writes, captured = [], []
    monkeypatch.setattr(cli, "_git", lambda *args: (-1, "") if record == "unreadable" else (0, listing))
    monkeypatch.setattr(cli, "_git_write", lambda root, argv: writes.append(argv) or (0, "", ""))
    real_cmds = cli.pc.cmds

    def capture(templates, **values):
        result = real_cmds(templates, **values)
        captured.append((templates, values, result))
        return result

    monkeypatch.setattr(cli.pc, "cmds", capture)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="ours",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
        add_timed_out=True,
    )
    assert writes == []
    assert target.exists() == present
    if present:
        assert (target / "precious.txt").read_bytes() == b"keep these bytes"
    assert len(captured) == 1
    templates, values, (commands, _) = captured[0]
    assert templates == [
        "git -C {repo} worktree list --porcelain",
        "git -C {repo} worktree unlock -- {folder}",
        "git -C {folder} status --short --untracked-files=all",
        "git -C {folder} reset --mixed HEAD",
        "git -C {folder} checkout-index -a",
        "git -C {repo} -c status.showUntrackedFiles=all worktree remove -- {folder}",
        "git -C {repo} -c status.showUntrackedFiles=all worktree remove --force -- {folder}",
        "git -C {repo} rev-parse --verify {branch}",
        "git -C {repo} branch -D -- {branch}",
        "git -C {repo} branch -d -- {branch}",
    ]
    assert values["folder"] == str(target) and values["branch"] == "lane/ours"
    assert "wait until nothing is running in " + cli.pc.line(str(target)) in note
    assert "if Git still lists the checkout as locked once nothing is running, first run" in note
    assert all(f"`{command}`" in note for command in commands)
    assert note.index(f"`{commands[0]}`") < note.index(f"`{commands[1]}`") < note.index(f"`{commands[2]}`")
    assert len(note.splitlines()) == 1


def test_timeout_index_repair_is_conditional_and_content_safe(tmp_path, monkeypatch):
    captured = []
    real_cmds = cli.pc.cmds

    def capture(templates, **values):
        result = real_cmds(templates, **values)
        captured.append((templates, result[0]))
        return result

    monkeypatch.setattr(cli.pc, "cmds", capture)
    monkeypatch.setattr(cli, "_git_write", lambda *args: pytest.fail("timeout must not run any repair"))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="ours",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout ; & $",
        cleanup_branch=True,
        add_timed_out=True,
    )
    templates, commands = captured[0]
    assert "git -C {folder} reset --mixed HEAD" in templates
    assert all(
        "--hard" not in step and " checkout " not in step and "checkout-index -f" not in step and " clean" not in step
        for step in templates
    )
    reset = commands[templates.index("git -C {folder} reset --mixed HEAD")]
    condition = "if it shows staged deletions of files that are present"
    assert condition in note and note.index(condition) < note.index(f"`{reset}`")
    assert "confirmed this setup left an incomplete index" in note
    assert "working files are unchanged" in note and "mixed reset does not recreate missing files" in note
    force = commands[
        templates.index("git -C {repo} -c status.showUntrackedFiles=all worktree remove --force -- {folder}")
    ]
    confirmation = "only after you explicitly confirm all work is saved and this folder may be deleted"
    assert confirmation in note and note.index(confirmation) < note.index(f"`{force}`")
    assert "if changes or untracked files remain" in note
    assert "if the folder is gone or status shows no changes or untracked files" in note
    assert "after removal succeeds" in note and "no checkout uses the lane branch" in note


def test_timeout_note_explains_how_to_check_recovery_conditions(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_git_write", lambda *args: pytest.fail("timeout advice must not mutate anything"))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="ours",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout ; & $",
        cleanup_branch=True,
        add_timed_out=True,
    )
    assert "a line beginning with locked in that checkout's record means it is locked" in note
    assert 'if unlock answers "is not locked", go on with the next step' in note
    assert "D in the first column means a staged deletion" in note
    assert "D in the second column means a missing tracked file" in note
    assert "?? means an untracked file" in note
    assert "only a successful status command with no output counts as clean" in note
    assert "inspect the planned folder directly to confirm whether it is gone" in note
    assert "verify the saved copies contain everything you want to keep" in note
    assert "no record may contain " + cli.pc.line("branch refs/heads/lane/ours") in note
    assert len(note.splitlines()) == 1


@pytest.mark.parametrize("timed_out", [False, True])
def test_recovery_deletes_unmerged_base_only_after_the_printed_tip_check(tmp_path, monkeypatch, timed_out):
    target = tmp_path / "checkout"
    target.mkdir()
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "retained", lambda *a: (-1, "")))
    monkeypatch.setattr(cli, "_git_write", lambda *a: (1, "", "kept"))
    captured = []
    real = cli.pc.cmds

    def capture(steps, **values):
        captured.extend(steps)
        return real(steps, **values)

    monkeypatch.setattr(cli.pc, "cmds", capture)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=target,
        cleanup_branch=True,
        add_timed_out=timed_out,
    )
    assert captured[-3:] == [
        "git -C {repo} rev-parse --verify {branch}",
        "git -C {repo} branch -D -- {branch}",
        "git -C {repo} branch -d -- {branch}",
    ]
    assert "only if it succeeds and prints exactly " + "a" * 40 in note
    assert "otherwise use" in note and "remains reachable by its id" in note


@pytest.mark.parametrize("failure", ["timeout", "failed", "undecodable"])
def test_successful_remove_with_unreadable_listing_reports_removal_truthfully(tmp_path, monkeypatch, failure):
    target = tmp_path / "checkout"
    target.mkdir()
    writes = []

    def write(root, argv):
        writes.append(argv)
        target.rmdir()
        return 0, "", ""

    after = (-1, "") if failure != "undecodable" else (0, "worktree /bad\ufffd\0\0")
    monkeypatch.setattr(cli, "_git", _after_owned_checkout(tmp_path, target, "retained", lambda *a: after))
    monkeypatch.setattr(cli, "_git_write", write)
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path), lane_id="retained", base_sha="a" * 40, created_path=target, cleanup_branch=True
    )
    assert "the worktree was removed" in note and "could not read Git's checkout records" in note
    assert "worktree was not removed" not in note and "worktree remove" not in note
    assert "worktree list --porcelain" in note and "no checkout uses the lane branch" in note
    assert writes == [["worktree", "remove", "--", str(target)]]


def test_timeout_offers_only_missing_file_checkout_and_conditional_index_lock_advice(tmp_path, monkeypatch):
    captured = []
    real = cli.pc.cmds

    def capture(steps, **values):
        captured.extend(steps)
        return real(steps, **values)

    monkeypatch.setattr(cli.pc, "cmds", capture)
    monkeypatch.setattr(cli, "_git_write", lambda *a: pytest.fail("timeout advice must never mutate Git"))
    note = cli._cleanup_failed_provision(
        SimpleNamespace(root=tmp_path),
        lane_id="retained",
        base_sha="a" * 40,
        created_path=tmp_path / "checkout",
        cleanup_branch=True,
        add_timed_out=True,
    )
    restore = [step for step in captured if "checkout-index" in step]
    assert restore == ["git -C {folder} checkout-index -a"]
    assert "if status shows only ' D' lines" in note
    assert "refuses existing files and leaves untracked files alone" in note
    assert 'if reset reports "Unable to create' in note and "index.lock" in note
    assert "while status works" in note and "remove only that index.lock file" in note


@pytest.mark.parametrize("relative", [True, False], ids=["relative-root", "absolute-control"])
def test_timeout_recovery_uses_one_store_root_folder_from_a_subfolder(tmp_path, monkeypatch, relative):
    root = tmp_path / "repo"
    cwd = root / "subfolder"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    store = SimpleNamespace(root=root)
    # Older or hand-made records may still contain a relative checkout path.
    # Assignment now makes it absolute before minting, so build that input directly.
    raw = Path("managed") / "relative-timeout-aaaaaaaaaaaa-legacy"
    assert not raw.is_absolute()
    actual, decoy = root / raw, cwd / raw
    assert actual != decoy
    for folder, content in [(actual, b"actual unfinished work"), (decoy, b"decoy unfinished work")]:
        folder.mkdir(parents=True)
        (folder / "precious.txt").write_bytes(content)
    captured = []
    real = cli.pc.cmds

    def capture(templates, **values):
        result = real(templates, **values)
        captured.append((templates, values, result[0]))
        return result

    def no_git(*args, **kwargs):
        pytest.fail("timeout advice must not read or mutate Git")

    monkeypatch.setattr(cli.pc, "cmds", capture)
    monkeypatch.setattr(cli, "_git", no_git)
    monkeypatch.setattr(cli, "_git_write", no_git)
    note = cli._cleanup_failed_provision(
        store,
        lane_id="relative-timeout",
        base_sha="a" * 40,
        created_path=raw if relative else actual,
        cleanup_branch=True,
        add_timed_out=True,
    )
    assert len(captured) == 1
    templates, values, commands = captured[0]
    assert Path(values["folder"]).is_absolute() and values["folder"] == str(actual)
    assert "wait until nothing is running in " + cli.pc.line(str(actual)) in note
    assert "save or move any work" in note and "index.lock" in note
    assert cli.pc.line(str(decoy)) not in note and str(decoy) not in values.values()
    folder_steps = [template for template in templates if "{folder}" in template]
    assert len(folder_steps) == 6
    assert any("remove --force" in template for template in folder_steps)
    for template, command in zip(templates, commands, strict=True):
        argv = [
            values[token[1:-1]] if token.startswith("<") and token.endswith(">") else token for token in command.split()
        ]
        if "{folder}" in template:
            assert str(actual) in argv
            assert str(decoy) not in argv and str(raw) not in argv
        assert f"`{command}`" in note
    assert (actual / "precious.txt").read_bytes() == b"actual unfinished work"
    assert (decoy / "precious.txt").read_bytes() == b"decoy unfinished work"
