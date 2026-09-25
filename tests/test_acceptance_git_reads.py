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


def test_project_read_scopes_reset_on_nested_calls_and_exceptions(case, monkeypatch):
    from agenttalk.acceptance_git import command_scope
    calls = []
    original = subprocess.run

    def counted(args, *a, **kw):
        if "--show-toplevel" in args:
            calls.append(args)
        return original(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", counted)
    with pytest.raises(RuntimeError, match="abort"):
        with command_scope():
            first = acceptance.verify_project(case["project"], case["sha"], live=False)
            first["roots"].clear()
            with command_scope():
                acceptance.verify_project(case["project"], case["sha"], live=False)
            again = acceptance.verify_project(case["project"], case["sha"], live=False)
            assert again["roots"] == [case["sha"]]
            assert len(calls) == 2
            raise RuntimeError("abort")
    acceptance.verify_project(case["project"], case["sha"], live=False)
    assert len(calls) == 3


def test_template_copy_keeps_objects_index_and_config_independent(case, acceptance_project_template):
    template, sha = acceptance_project_template
    git(case["project"], "config", "user.name", "Changed test author")
    (case["project"] / "source.txt").write_text("test-local change\n", encoding="utf-8")
    git(case["project"], "commit", "-qam", "test-local commit")
    obj = case["project"] / ".git" / "objects" / sha[:2] / sha[2:]
    obj.chmod(0o600)
    obj.write_bytes(b"test-local corruption")
    assert git(template, "rev-parse", "HEAD") == sha
    assert git(template, "status", "--porcelain") == ""
    assert git(template, "config", "user.name") == "Synthetic Author"
    assert git(template, "cat-file", "-t", sha) == "commit"


def test_symbolic_revision_is_resolved_again_within_a_command(case):
    from agenttalk.acceptance_git import command_scope
    with command_scope():
        before = acceptance.verify_project(case["project"], "HEAD", live=False)
        (case["project"] / "source.txt").write_text("new revision\n", encoding="utf-8")
        git(case["project"], "commit", "-qam", "move symbolic ref")
        after = acceptance.verify_project(case["project"], "HEAD", live=False)
    assert before["revision"] == case["sha"]
    assert after["revision"] != before["revision"]


@pytest.mark.parametrize("fault", [OSError("unavailable"), subprocess.TimeoutExpired("git", 10)])
def test_project_query_failure_remains_unverified(case, monkeypatch, fault):
    def unavailable(*args, **kwargs):
        raise fault

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(acceptance.AcceptanceError) as error:
        acceptance.verify_project(case["project"], case["sha"])
    assert error.value.code == "acceptance_project_unverified"
