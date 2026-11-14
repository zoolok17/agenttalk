"""Removal refuses unfinished work even when Git's default status would hide it."""

import subprocess

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
