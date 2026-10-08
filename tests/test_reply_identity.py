"""Reply identity (#354, #178): a wrapped seat that follows the printed reply instructions replies as itself.

The instructions the wrapper prints must carry ``--from <seat>``, and the wrapper must set AGENTTALK_SELF in the
seat's child environment (Claude and Codex), so the reply cannot fail with "no agent identity" or be sent under the
name of a different seat. An identity the supervisor configuration sets on purpose is kept.
"""

from __future__ import annotations

import json
import shlex

import pytest

from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import run, session

SEAT = "beta"


class _Capture:
    """Stands in for the real child process: records what the wrapper would have started."""

    started: list[dict] = []

    def __init__(self, argv, stdin_text=None, *, child_env=None, **_kwargs):
        self.returncode = 0
        _Capture.started.append({"argv": list(argv), "stdin": stdin_text or "", "env": dict(child_env or {})})

    def __iter__(self):
        yield json.dumps({"type": "stream_event", "event": {"type": "message_start"}})
        yield json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})


def _turn(tmp_path, monkeypatch, cli_name: str, kind: str = "task") -> dict:
    """Drive ONE wrapped turn through the real ``make_drive``, child process replaced; return what it started."""
    store = Store(tmp_path)
    store.init(["alpha", SEAT])
    _Capture.started = []
    monkeypatch.setattr(run, "_ProcStream", _Capture)
    state = (session.SessionState(cli="claude", claude_session_id="session-1") if cli_name == "claude"
             else session.SessionState(cli="codex"))
    drive = run.make_drive(store, SEAT, cli_name, state, [cli_name], render=False)
    drive({"id": "m-1", "from": "alpha", "to": SEAT, "kind": kind, "body": "do work",
           "meta": {"request_id": "tk-1"}})
    assert _Capture.started, "the wrapper started no child"
    return _Capture.started[0]


def _reply_commands(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "-m agenttalk reply" in line and line.strip().startswith("&")]


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_every_printed_reply_command_names_the_seat(tmp_path, monkeypatch, cli_name) -> None:
    started = _turn(tmp_path, monkeypatch, cli_name)
    prompt = started["stdin"] + "\n".join(started["argv"])
    section = prompt[prompt.index("== HOW TO REPLY TO THIS MESSAGE"):prompt.index("== HOW TO HANDLE ==")]
    commands = _reply_commands(section)
    assert len(commands) >= 2, section
    assert all(f"reply --from {SEAT} " in command for command in commands), commands


@pytest.mark.parametrize("kind", ["task", "question", "message"])
def test_the_printed_reply_works_with_no_identity_anywhere_in_the_environment(tmp_path, monkeypatch, kind) -> None:
    """The issue's repro: a seat whose supervisor.json has no env entry follows its instructions to the letter."""
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    started = _turn(tmp_path, monkeypatch, "claude", kind)
    prompt = started["stdin"] + "\n".join(started["argv"])
    section = prompt[prompt.index("== HOW TO REPLY TO THIS MESSAGE"):prompt.index("== HOW TO HANDLE ==")]
    command = next(c for c in _reply_commands(section) if "'your answer here'" in c)
    argv = shlex.split(command.split("-m agenttalk ", 1)[1])
    assert argv[0] == "reply"
    Store(tmp_path).send(sender="alpha", recipient=SEAT, kind="task", body="x", meta={"request_id": "tk-1"})
    assert cli.main(["--root", str(tmp_path), *argv]) == 0
    answers = [m for m in Store(tmp_path).messages_for("alpha") if m.sender == SEAT]
    assert len(answers) == 1 and answers[0].body == "your answer here"


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_the_wrapper_sets_the_seat_identity_for_the_child(tmp_path, monkeypatch, cli_name) -> None:
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    started = _turn(tmp_path, monkeypatch, cli_name)
    assert started["env"].get("AGENTTALK_SELF") == SEAT


def test_an_identity_set_on_purpose_by_the_supervisor_config_is_kept(tmp_path, monkeypatch) -> None:
    """Which wins: the value the supervisor configured. The printed ``--from`` still names the seat, and an explicit
    ``--from`` wins over the environment at the command line, so a reply never goes out under the wrong name."""
    monkeypatch.setenv("AGENTTALK_SELF", "configured-on-purpose")
    started = _turn(tmp_path, monkeypatch, "claude")
    assert started["env"]["AGENTTALK_SELF"] == "configured-on-purpose"


def test_child_env_sets_identity_in_the_gateway_profile_too(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    env = run._child_env(
        tmp_path, agent=SEAT, backend_profile="ovh-qwen",
        profile_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000", "ANTHROPIC_AUTH_TOKEN": "front-token"})
    assert env["AGENTTALK_SELF"] == SEAT


def test_child_env_without_a_seat_adds_no_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    assert "AGENTTALK_SELF" not in run._child_env(tmp_path)


def test_the_codex_reply_that_lost_its_identity_in_178_now_resolves_the_seat(tmp_path, monkeypatch) -> None:
    """#178 (a): a codex seat whose first reply stopped with "no agent identity". With the wrapper's child environment
    and the printed ``--from``, both routes to the identity are present, and the reply publishes under the seat."""
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    started = _turn(tmp_path, monkeypatch, "codex")
    child_environment = started["env"]
    assert child_environment["AGENTTALK_SELF"] == SEAT
    monkeypatch.setenv("AGENTTALK_SELF", child_environment["AGENTTALK_SELF"])
    Store(tmp_path).send(sender="alpha", recipient=SEAT, kind="task", body="x", meta={"request_id": "tk-9"})
    assert cli.main(["--root", str(tmp_path), "reply", "--to-request", "tk-9", "--kind", "task-response",
                     "--meta", "status=done", "-m", "done"]) == 0
    assert [m.sender for m in Store(tmp_path).messages_for("alpha")] == [SEAT]
