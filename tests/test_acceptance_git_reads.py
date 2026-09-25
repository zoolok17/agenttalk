"""Git process budget and live-state safety at the real CLI boundary."""

import subprocess

import pytest
import test_acceptance as fixtures

from agenttalk import acceptance
from test_acceptance import command, git, open_attempt

case = fixtures.case


def test_command_reads_project_metadata_once_but_rechecks_live_state(case, monkeypatch):
    calls = []
    original = subprocess.run

    def counted(args, *a, **kw):
        if args[:3] == ["git", "-C", str(case["project"])]:
            calls.append(args[3:])
        return original(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", counted)
    assert open_attempt(case) == 0
    assert sum("--show-toplevel" in args for args in calls) == 1
    assert sum(args[0] == "status" for args in calls) == 2
    calls.clear()
    assert command(case, "check", "--id", "attempt", "--json") == 3
    assert sum("--show-toplevel" in args for args in calls) == 1


@pytest.mark.parametrize("change", ["dirty", "head"])
def test_command_cannot_reuse_live_success_after_prepare(case, monkeypatch, capsys, change):
    original = acceptance.prepare

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        (case["project"] / "source.txt").write_text("changed during command\n", encoding="utf-8")
        if change == "head":
            git(case["project"], "commit", "-qam", "moved during command")
        return result

    monkeypatch.setattr(acceptance, "prepare", prepare)
    assert open_attempt(case) != 0
    assert "acceptance_project_unverified" in capsys.readouterr().err


def test_direct_project_reads_are_fresh_without_a_command(case):
    acceptance.verify_project(case["project"], case["sha"])
    (case["project"] / "source.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(acceptance.AcceptanceError, match="dirty"):
        acceptance.verify_project(case["project"], case["sha"])


@pytest.mark.parametrize("revision", ["--help", "HEAD source.txt", "HEAD^{tree}", "f" * 40])
def test_project_revision_still_requires_one_verified_commit(case, revision):
    with pytest.raises(acceptance.AcceptanceError) as error:
        acceptance.verify_project(case["project"], revision)
    assert error.value.code == "acceptance_project_unverified"
