"""Cleanliness checks include new files even when Git hides them by default."""

from __future__ import annotations

import shutil
import subprocess
import os

import pytest

from agenttalk import cli, close, janitor
from agenttalk.store import Store


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git is required for the real-repository cleanliness tests")
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        # Surface a sandbox refusal even if Git only prints a warning and exits zero.
        assert "permission denied" not in result.stderr.lower(), result.stderr
        return result.stdout.strip()

    git("init", "-q", "-b", "feature-clean-check")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "status.showUntrackedFiles", "no")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    git("add", "tracked.txt", ".gitignore")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "test baseline")
    return root, git


@pytest.mark.parametrize(
    "state, clean",
    [("clean", True), ("tracked", False), ("untracked-top", False), ("untracked-directory", False), ("ignored", True)],
)
def test_worktree_clean_with_untracked_files_hidden(repo, state, clean):
    root, _git = repo
    if state == "tracked":
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif state in {"untracked-top", "untracked-directory", "ignored"}:
        relative = {
            "untracked-top": "untracked.txt",
            "untracked-directory": "new-folder/untracked.txt",
            "ignored": ".agenttalk/ignored.txt",
        }[state]
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("new\n", encoding="utf-8")

    assert cli._worktree_clean(root) is clean


def test_worktree_clean_without_git_is_unknown(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("Git is unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert cli._worktree_clean(tmp_path) is None


@pytest.mark.parametrize("extra", [[], ["--allow-dirty"], ["--dirty-artifact", "diff:one"]])
def test_close_open_unknown_status_is_not_clean(repo, monkeypatch, capsys, extra):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = git("rev-parse", "HEAD")
    original = cli._git
    status_calls = []

    def git_status_fails(path, args):
        if args == cli.index_safety.STATUS_ARGS:
            status_calls.append(True)
            return 1, ""
        return original(path, args)

    monkeypatch.setattr(cli, "_git", git_status_fails)
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "close",
                "open",
                "--id",
                "c",
                "--from",
                "lead",
                "--scope",
                "docs",
                "--revision",
                revision,
                *extra,
            ]
        )
        == 0
    )
    assert status_calls == [True]
    record = close.load_close(store, "c")
    assert record["revision_clean"] is False
    assert record["revision_clean_state"] == "unknown"
    assert record["dirty_artifact"] == ("diff:one" if "--dirty-artifact" in extra else None)
    err = capsys.readouterr().err
    assert "Git could not report whether this checkout is clean" in err
    assert "worktree is dirty" not in err
    assert ("close check will HOLD" in err) is (not record["dirty_artifact"])
    result = close.compute_verdict(record, {"verdict": "GO", "blockers": []})
    assert any(hold["code"] == close.HOLD_REVISION for hold in result["holds"]) is (not record["dirty_artifact"])


def test_close_open_outside_git_is_not_clean(tmp_path, capsys):
    store = Store(tmp_path)
    store.init(["lead"])
    store.set_role("lead", "lead")
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--root",
                str(tmp_path),
                "close",
                "open",
                "--id",
                "c",
                "--from",
                "lead",
                "--scope",
                "docs",
                "--revision",
                "a" * 40,
            ]
        )
        == 0
    )
    assert close.load_close(store, "c")["revision_clean"] is False
    err = capsys.readouterr().err
    assert "Git could not report whether this checkout is clean" in err
    assert "close check will HOLD" in err
    assert "worktree is dirty" not in err


@pytest.mark.parametrize("dirty", [False, True])
def test_close_open_reports_known_checkout_state(repo, capsys, dirty):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = git("rev-parse", "HEAD")
    if dirty:
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "close",
                "open",
                "--id",
                "c",
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
    assert close.load_close(store, "c")["revision_clean"] is (not dirty)
    err = capsys.readouterr().err
    assert ("worktree is dirty" in err) is dirty
    assert "Git could not report whether this checkout is clean" not in err


def test_close_reopen_does_not_record_clean_when_git_status_fails(repo, monkeypatch, capsys):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = git("rev-parse", "HEAD")
    common = ["--root", str(root), "close"]
    assert (
        cli.main(
            common
            + [
                "open",
                "--id",
                "check",
                "--from",
                "lead",
                "--scope",
                "release",
                "--revision",
                revision,
                "--non-lane-isolation-not-asserted",
            ]
        )
        == 0
    )
    assert close.load_close(store, "check")["revision_clean"] is True
    original_git = cli._git
    status_calls = []

    def git_status_fails(path, args):
        if args == cli.index_safety.STATUS_ARGS:
            status_calls.append(True)
            return 1, ""
        return original_git(path, args)

    monkeypatch.setattr(cli, "_git", git_status_fails)
    capsys.readouterr()
    assert cli.main(common + ["reopen", "--id", "check", "--from", "lead", "--revision", revision]) == 0
    assert status_calls == [True]
    assert close.load_close(store, "check")["revision_clean"] is False
    assert close.load_close(store, "check")["revision_clean_state"] == "unknown"
    assert (
        "Git could not report whether this checkout is clean; it is not recorded as clean." in capsys.readouterr().err
    )


@pytest.mark.parametrize("action", ["open", "reopen"])
@pytest.mark.parametrize("relative", ["untracked.txt", "new-folder/untracked.txt"])
def test_close_records_untracked_files_as_dirty(repo, action, relative):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = git("rev-parse", "HEAD")
    if action == "reopen":
        assert (
            cli.main(
                [
                    "--root",
                    str(root),
                    "close",
                    "open",
                    "--id",
                    "clean-check",
                    "--from",
                    "lead",
                    "--scope",
                    "release",
                    "--revision",
                    revision,
                    "--non-lane-isolation-not-asserted",
                ]
            )
            == 0
        )
        assert close.load_close(store, "clean-check")["revision_clean"] is True
    untracked = root / relative
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("uncommitted work\n", encoding="utf-8")

    args = ["--root", str(root), "close", action, "--id", "clean-check", "--from", "lead", "--revision", revision]
    if action == "open":
        args.extend(["--scope", "release", "--non-lane-isolation-not-asserted"])
    assert cli.main(args) == 0
    assert close.load_close(store, "clean-check")["revision_clean"] is False
    assert close.load_close(store, "clean-check")["revision_clean_state"] == "not_clean"


@pytest.mark.parametrize("relative", ["untracked.txt", "new-folder/untracked.txt"])
def test_janitor_sees_untracked_files_when_git_hides_them(repo, relative):
    root, _git = repo
    untracked = root / relative
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("work to preserve\n", encoding="utf-8")

    assert janitor.is_dirty_worktree(root) is True


@pytest.mark.parametrize(
    "relative, ignored",
    [("after-commit.txt", False), ("new-folder/after-commit.txt", False), (".agenttalk/ignored.txt", True)],
)
def test_janitor_checks_new_files_left_after_the_wip_commit(repo, monkeypatch, relative, ignored):
    root, _git = repo
    (root / "before-commit.txt").write_text("preserve in WIP\n", encoding="utf-8")
    real_run = subprocess.run

    def leave_new_work(argv, *args, **kwargs):
        result = real_run(argv, *args, **kwargs)
        if "commit" in argv and result.returncode == 0:
            new_work = root / relative
            new_work.parent.mkdir(parents=True, exist_ok=True)
            new_work.write_text("still needs preserving\n", encoding="utf-8")
        return result

    monkeypatch.setattr(subprocess, "run", leave_new_work)
    result = janitor.wip_commit_dirty_worktree(root, default_branches=["main", "master"])

    assert result.refused is not ignored
    assert (root / relative).read_text(encoding="utf-8") == "still needs preserving\n"


def test_janitor_ignored_files_do_not_make_a_worktree_dirty(repo):
    root, _git = repo
    ignored = root / ".agenttalk" / "ignored.txt"
    ignored.parent.mkdir(exist_ok=True)
    ignored.write_text("ignored store data\n", encoding="utf-8")

    assert janitor.is_dirty_worktree(root) is False
    assert ignored.read_text(encoding="utf-8") == "ignored store data\n"


@pytest.mark.parametrize("site", ["cli", "janitor", "post-commit"])
def test_status_mode_normal_for_yes_no_checks(tmp_path, monkeypatch, site):
    calls = []
    (tmp_path / ".git").touch()

    def read(root, argv):
        calls.append(argv)
        return 0, ""

    def checked(root, *argv):
        calls.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(cli, "_git", read)
    monkeypatch.setattr(janitor, "_run_git_checked", checked)
    index_calls = []

    def run(argv, **kwargs):
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 0, os.fsencode(argv[2]) + b"\n", b"")
        if "ls-files" in argv:
            index_calls.append(argv[3:])
            assert argv[3:] in [["ls-files", "--cached", opt, "-z", "--full-name"] for opt in ("-v", "--stage")]
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert "add" in argv or "commit" in argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    if site == "cli":
        assert cli._worktree_clean(tmp_path) is True
    elif site == "janitor":
        assert janitor.is_dirty_worktree(tmp_path) is False
    else:
        monkeypatch.setattr(janitor, "worktree_branch", lambda path: "feature")
        monkeypatch.setattr(janitor.shutil, "which", lambda name: "git")
        assert janitor.wip_commit_dirty_worktree(tmp_path, default_branches=["main"]).refused is False
    assert calls == [
        [
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--ignore-submodules=none",
        ]
    ]
    assert len(index_calls) == (4 if site == "post-commit" else 1)


@pytest.mark.parametrize("revision_mode", ["different", "missing", "omitted"])
def test_reopen_refuses_unverified_or_mismatched_revision(repo, capsys, revision_mode):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    original = git("rev-parse", "HEAD")
    common = ["--root", str(root), "close"]
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", original]) == 0
    (root / "tracked.txt").write_text("next\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "next")
    before = close.load_close(store, "c")
    args = [] if revision_mode == "omitted" else ["--revision", original if revision_mode == "different" else "a" * 40]
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead"] + args) == 2
    assert close.load_close(store, "c") == before
    assert "revision" in capsys.readouterr().err.lower()


def test_reopen_refuses_when_git_unavailable(repo, monkeypatch, capsys):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = git("rev-parse", "HEAD")
    common = ["--root", str(root), "close"]
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", revision]) == 0
    before = close.load_close(store, "c")
    monkeypatch.setattr(cli, "_git", lambda *args: (-1, ""))
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead", "--revision", revision]) == 2
    assert close.load_close(store, "c") == before
    assert "Git or the checkout is unavailable" in capsys.readouterr().err


def test_reopen_outside_repository_refuses_but_open_still_accepts_pin(tmp_path, capsys):
    store = Store(tmp_path)
    store.init(["lead"])
    store.set_role("lead", "lead")
    common = ["--root", str(tmp_path), "close"]
    revision = "a" * 40
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", revision]) == 0
    before = close.load_close(store, "c")
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead"]) == 2
    assert close.load_close(store, "c") == before
    assert "Git or the checkout is unavailable" in capsys.readouterr().err


def test_other_revision_callers_keep_full_pin_fallback(repo):
    root, _ = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    revision = "a" * 40
    assert cli._lane_resolve(store, revision) == revision
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "request-launch",
                "--from",
                "lead",
                "--profile",
                "review",
                "--skill",
                "review-code",
                "--revision",
                revision,
                "--request-id",
                "lr-test",
                "-m",
                "review",
            ]
        )
        == 0
    )
    assert store.read_launch_request("lr-test")["scope"]["revision"] == revision


def test_reopen_accepts_short_revision_detached_in_subfolder(repo):
    root, git = repo
    revision = git("rev-parse", "HEAD")
    git("checkout", "--detach", "-q", revision)
    sub = root / "sub"
    sub.mkdir()
    (root / ".gitignore").write_text(".agenttalk/\nsub/\n", encoding="utf-8")
    git("add", ".gitignore")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "ignore store")
    revision = git("rev-parse", "HEAD")
    store = Store(sub)
    store.init(["lead"])
    store.set_role("lead", "lead")
    common = ["--root", str(sub), "close"]
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", revision]) == 0
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead", "--revision", revision[:12]]) == 0
    assert close.load_close(store, "c")["revision"] == revision
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead"]) == 0


@pytest.mark.parametrize("bad", [None, "short", 123, "missing"])
def test_reopen_requires_recorded_full_revision(repo, capsys, monkeypatch, bad):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    common = ["--root", str(root), "close"]
    revision = git("rev-parse", "HEAD")
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", revision]) == 0
    real_load = close.load_close

    def damaged_load(*args):
        record = real_load(*args)
        if bad == "missing":
            record.pop("revision")
        else:
            record["revision"] = bad
        return record

    monkeypatch.setattr(close, "load_close", damaged_load)
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead"]) == 2
    assert "this close has no recorded full revision; pass --revision" in capsys.readouterr().err


def test_reopen_reports_stale_acks_only_when_revision_changes(repo, capsys):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    common = ["--root", str(root), "close"]
    revision = git("rev-parse", "HEAD")
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", revision]) == 0
    capsys.readouterr()
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead"]) == 0
    assert "prior lens acks now stale" not in capsys.readouterr().out
    (root / "tracked.txt").write_text("next\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "next")
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead", "--revision", git("rev-parse", "HEAD")]) == 0
    assert "prior lens acks now stale" in capsys.readouterr().out


def test_reopen_slow_git_does_not_lock_other_close(repo, monkeypatch):
    import threading

    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    common = ["--root", str(root), "close"]
    revision = git("rev-parse", "HEAD")
    for cid in ("a", "b"):
        args = ["open", "--id", cid, "--from", "lead", "--scope", "docs", "--revision", revision]
        assert cli.main(common + args) == 0
    entered = threading.Event()
    release = threading.Event()
    real_git = cli._git

    def slow_git(*args):
        if "rev-parse" in args[1]:
            entered.set()
            assert release.wait(10)
        return real_git(*args)

    monkeypatch.setattr(cli, "_git", slow_git)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(cli.main(common + ["reopen", "--id", "a", "--from", "lead"]))
    )
    worker.start()
    try:
        assert entered.wait(10)
        with close.close_transaction(store, "b", lock_timeout=1):
            pass
    finally:
        release.set()
        worker.join(20)
    assert result == [0]


def test_reopen_refuses_if_recorded_revision_changes_during_git_check(repo, monkeypatch, capsys):
    root, git = repo
    store = Store(root)
    store.init(["lead"])
    store.set_role("lead", "lead")
    common = ["--root", str(root), "close"]
    revision = git("rev-parse", "HEAD")
    assert cli.main(common + ["open", "--id", "c", "--from", "lead", "--scope", "docs", "--revision", revision]) == 0
    real_check = cli._reopen_revision

    def change_revision(root, ref):
        result = real_check(root, ref)
        with close.close_transaction(store, "c") as transaction:
            transaction.record["revision"] = "a" * 40
            transaction.commit()
        return result

    monkeypatch.setattr(cli, "_reopen_revision", change_revision)
    assert cli.main(common + ["reopen", "--id", "c", "--from", "lead"]) == 3
    assert close.load_close(store, "c")["revision"] == "a" * 40
    assert "revision changed" in capsys.readouterr().err
