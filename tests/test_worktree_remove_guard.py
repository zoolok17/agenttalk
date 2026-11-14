"""Removal refuses unfinished work even when Git's default status would hide it."""

import subprocess
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli


@pytest.mark.parametrize("state", ["untracked", "tracked", "unavailable", "clean"])
def test_removal_requires_a_confirmed_clean_worktree(tmp_path, monkeypatch, state):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    precious = worktree / "precious.txt"
    precious.write_text("keep this work\n", encoding="utf-8")
    removals = []

    def status(cmd, **kwargs):
        assert cmd[:3] == ["git", "-C", str(worktree)]
        if cmd[3:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(cmd, 0, str(worktree), "")
        assert cmd[3:5] == ["status", "--porcelain"]
        if state == "unavailable":
            raise FileNotFoundError("Git could not be started")
        output = " M precious.txt\n" if state == "tracked" else ""
        if state == "untracked" and any(flag in cmd for flag in ("--untracked-files=all", "--untracked-files=normal")):
            output = "?? precious.txt\n"
        return subprocess.CompletedProcess(cmd, 0, output, "")

    class Removed:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def remove(cmd, **kwargs):
        removals.append(cmd)
        return Removed()

    monkeypatch.setattr(cli.subprocess, "run", status)
    monkeypatch.setattr(cli.subprocess, "Popen", remove)

    rc, _out, err = cli._git_write(tmp_path, ["worktree", "remove", "--", str(worktree)])

    if state == "clean":
        assert rc == 0 and len(removals) == 1
    else:
        assert rc != 0 and not removals
        assert "was not removed" in err
    assert precious.read_text(encoding="utf-8") == "keep this work\n"


UNKNOWN = (1, "", "could not verify worktree status; the worktree was not removed")
DIRTY = (1, "", "worktree has uncommitted changes or untracked files; the worktree was not removed")
SETTING = ["-c", "status.showUntrackedFiles=all"]


def _executor(monkeypatch, worktree, *, root_result=None, status_result=(0, ""), result=(0, "", "")):
    """Keep the real read adapter and status helper; replace only subprocesses."""
    reads, writes = [], []
    if root_result is None:
        root_result = (0, str(worktree))

    def read(cmd, **kwargs):
        assert cmd[:2] == ["git", "-C"]
        reads.append((Path(cmd[2]), cmd[3:]))
        if cmd[3:] == ["rev-parse", "--show-toplevel"]:
            response = root_result
        else:
            assert cmd[3:] == ["status", "--porcelain", "--untracked-files=all"]
            response = status_result
        if response == "unavailable":
            raise FileNotFoundError("Git could not be started")
        return subprocess.CompletedProcess(cmd, response[0], response[1], "")

    def write(cmd, **kwargs):
        writes.append(cmd)
        return SimpleNamespace(returncode=result[0], communicate=lambda timeout: result[1:])

    monkeypatch.setattr(cli.subprocess, "run", read)
    monkeypatch.setattr(cli.subprocess, "Popen", write)
    return reads, writes


@pytest.mark.parametrize("status", [(0, "?? precious\n"), (0, " M precious\n"), (128, ""), "unavailable", (0, "")])
def test_782_present_target_keeps_exact_status_results_and_fixed_execution(tmp_path, monkeypatch, status):
    target = tmp_path / "checkout"
    target.mkdir()
    reads, writes = _executor(monkeypatch, target, status_result=status)
    argv = ["worktree", "remove", "--", str(target)]
    answer = cli._git_write(tmp_path, argv)
    assert reads == [
        (target, ["rev-parse", "--show-toplevel"]),
        (target, ["status", "--porcelain", "--untracked-files=all"]),
    ]
    if status == (0, ""):
        assert answer == (0, "", "")
        assert writes == [["git", "-c", "core.editor=false", "-C", str(tmp_path), *SETTING, *argv]]
    else:
        assert answer == (UNKNOWN if status == "unavailable" or status[0] != 0 else DIRTY)
        assert writes == []
    assert argv == ["worktree", "remove", "--", str(target)]


@pytest.mark.parametrize("result", [(0, "removed", ""), (73, "output", "Git refused the missing registration")])
def test_782_absent_absolute_target_attempts_removal_and_preserves_gits_result(tmp_path, monkeypatch, result):
    target = tmp_path / "absent"
    reads, writes = _executor(monkeypatch, target, result=result)
    argv = ["worktree", "remove", "--", str(target)]
    assert cli._git_write(tmp_path, argv) == result
    assert reads == []
    assert writes == [["git", "-c", "core.editor=false", "-C", str(tmp_path), *SETTING, *argv]]
    assert argv == ["worktree", "remove", "--", str(target)]


@pytest.mark.parametrize("case", ["failed-matching", "empty", "relative", "mismatch", "unavailable"])
def test_782_root_must_succeed_and_identify_this_absolute_root(tmp_path, monkeypatch, case):
    target = tmp_path / "checkout"
    target.mkdir()
    responses = {
        "failed-matching": (128, str(target)),
        "empty": (0, "\n"),
        "relative": (0, "checkout"),
        "mismatch": (0, str(tmp_path)),
        "unavailable": "unavailable",
    }
    reads, writes = _executor(monkeypatch, target, root_result=responses[case])
    assert cli._git_write(tmp_path, ["worktree", "remove", "--", str(target)]) == UNKNOWN
    assert reads == [(target, ["rev-parse", "--show-toplevel"])] and writes == []


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError(errno.EACCES, "unreadable"),
        OSError(errno.EIO, "I/O failure"),
        NotADirectoryError(errno.ENOTDIR, "not a directory"),
    ],
)
def test_782_entry_errors_are_not_absence(tmp_path, monkeypatch, failure):
    target = tmp_path / "unreadable"
    real_lstat = cli.os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == str(target):
            raise failure
        return real_lstat(path, *args, **kwargs)

    reads, writes = _executor(monkeypatch, target)
    monkeypatch.setattr(cli.os, "lstat", lstat)
    assert cli._git_write(tmp_path, ["worktree", "remove", "--", str(target)]) == UNKNOWN
    assert reads == [] and writes == []


@pytest.mark.parametrize("target", ["", "absent-relative"])
def test_782_empty_and_missing_relative_inputs_remain_unverifiable(tmp_path, monkeypatch, target):
    reads, writes = _executor(monkeypatch, tmp_path / target)
    assert cli._git_write(tmp_path, ["worktree", "remove", "--", target]) == UNKNOWN
    assert reads == [] and writes == []


def test_782_present_relative_target_is_inspected_relative_to_git_root(tmp_path, monkeypatch):
    root, elsewhere = tmp_path / "repo", tmp_path / "cwd"
    target = root / "checkout"
    target.mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    reads, writes = _executor(monkeypatch, target)
    argv = ["worktree", "remove", "--", "checkout"]
    assert cli._git_write(root, argv) == (0, "", "")
    assert len(reads) == 2 and all(path == target for path, _command in reads)
    assert writes[0][5:7] == SETTING and writes[0][-4:] == argv


def test_782_dangling_entry_requires_root_verification(tmp_path, monkeypatch):
    target = tmp_path / "dangling"
    real_lstat = cli.os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == str(target):
            return SimpleNamespace()  # lstat succeeded; a dangling final link is still an entry
        return real_lstat(path, *args, **kwargs)

    reads, writes = _executor(monkeypatch, target, root_result=(128, ""))
    monkeypatch.setattr(cli.os, "lstat", lstat)
    assert cli._git_write(tmp_path, ["worktree", "remove", "--", str(target)]) == UNKNOWN
    assert reads == [(target, ["rev-parse", "--show-toplevel"])] and writes == []


def test_782_regular_file_is_not_an_absent_checkout(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("preserve", encoding="utf-8")
    reads, writes = _executor(monkeypatch, target, root_result=(128, ""))
    assert cli._git_write(tmp_path, ["worktree", "remove", "--", str(target)]) == UNKNOWN
    assert reads == [(target, ["rev-parse", "--show-toplevel"])] and writes == []
    assert target.read_text(encoding="utf-8") == "preserve"


def test_782_path_comparison_error_refuses_before_status(tmp_path, monkeypatch):
    target = tmp_path / "checkout"
    target.mkdir()
    reads, writes = _executor(monkeypatch, target)

    def compare(*args):
        raise ValueError("cannot compare paths")

    monkeypatch.setattr(cli, "_same_folder", compare)
    assert cli._git_write(tmp_path, ["worktree", "remove", "--", str(target)]) == UNKNOWN
    assert reads == [(target, ["rev-parse", "--show-toplevel"])] and writes == []


@pytest.mark.parametrize(
    "argv",
    [
        ["-c", "status.showUntrackedFiles=all", "worktree", "remove", "--", "x"],
        ["-c", "status.showUntrackedFiles=no", "worktree", "remove", "--", "x"],
        ["--config-env=status.showUntrackedFiles=SETTING", "worktree", "remove", "--", "x"],
        ["worktree", "remove", "--force", "--", "x"],
        ["worktree", "remove", "--", "x", "y"],
        ["worktree", "remove", "x"],
        ["-c", "status.showUntrackedFiles=all", "-c", "core.editor=true", "worktree", "remove", "--", "x"],
    ],
)
def test_782_callers_cannot_supply_configuration_force_or_extra_paths(tmp_path, monkeypatch, argv):
    reads, writes = _executor(monkeypatch, tmp_path)
    with pytest.raises(cli.GitWriteError, match="not allowlisted"):
        cli._git_write(tmp_path, argv)
    assert reads == [] and writes == []


@pytest.mark.parametrize(
    "argv",
    [
        ["worktree", "add", "-b", "lane/test", "--", "target", "a" * 40],
        ["branch", "-D", "--", "lane/test"],
        ["update-ref", "-d", "refs/heads/lane/test"],
    ],
)
def test_782_other_allowed_mutations_do_not_receive_the_setting(tmp_path, monkeypatch, argv):
    original = list(argv)
    reads, writes = _executor(monkeypatch, tmp_path)
    assert cli._git_write(tmp_path, argv) == (0, "", "")
    assert reads == []
    assert writes == [["git", "-c", "core.editor=false", "-C", str(tmp_path), *original]]
    assert argv == original


def test_782_real_nested_checkout_without_git_file_never_starts_removal(tmp_path, monkeypatch):
    from test_lanes import _assign_worktree, _git, _repo

    root, base = _repo(tmp_path)
    target = Path(_assign_worktree(root, base, "nested-root")["worktree_path"])
    (target / ".git").unlink()
    sentinel = target / "precious.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    assert cli._same_folder(_git(target, "rev-parse", "--show-toplevel").strip(), str(root))
    assert _git(root, "status", "--porcelain", "--untracked-files=all").strip() == ""
    real_popen, mutations = cli.subprocess.Popen, []

    def popen(cmd, **kwargs):
        if "worktree" in cmd and "remove" in cmd:
            mutations.append(cmd)
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    answer = cli._git_write(root, ["worktree", "remove", "--", str(target)])
    assert mutations == [], "parent cleanliness must not permit even an attempted removal"
    assert answer == UNKNOWN
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_782_real_missing_registered_checkout_leaves_the_result_to_git(tmp_path, monkeypatch):
    import shutil
    from test_lanes import _assign_worktree, _git, _repo

    root, base = _repo(tmp_path)
    target = Path(_assign_worktree(root, base, "registered-gone")["worktree_path"])
    assert target.is_relative_to(root / ".worktrees")
    shutil.rmtree(target)
    assert str(target).replace("\\", "/") in _git(root, "worktree", "list", "--porcelain")
    real_popen, attempts, results = cli.subprocess.Popen, [], []

    def popen(cmd, **kwargs):
        proc = real_popen(cmd, **kwargs)
        if "worktree" in cmd and "remove" in cmd:
            attempts.append(cmd)
            communicate = proc.communicate

            def observed(timeout=None):
                out, err = communicate(timeout=timeout)
                results.append((proc.returncode, out, err))
                return out, err

            proc.communicate = observed
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    answer = cli._git_write(root, ["worktree", "remove", "--", str(target)])
    assert len(attempts) == 1 and results == [answer]
    assert attempts[0][5:7] == SETTING
    if answer[0] == 0:
        assert str(target).replace("\\", "/") not in _git(root, "worktree", "list", "--porcelain")
    else:
        assert answer[2] and answer != UNKNOWN
