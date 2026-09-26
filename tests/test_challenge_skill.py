"""agenttalk.challenge: the skill's example commands EXECUTE, and a challenge survives the
real wrapper transport with its typed verdict meta.

A flag-inventory lint proves a flag exists; it cannot prove a command works (PR #205
cold read: `--await-reply` is refused outside a wrapper turn, and `escalate
--origin-request` without `--origin-id` exits 2). So these tests lift the exact commands
out of BOTH shipped twins, substitute their placeholders, and run them through
``cli.main`` against a scratch store - wrapped and unwrapped where the skill branches.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from agenttalk import cli, reply_transport
from agenttalk.install_skills import SKILLS_ROOT
from agenttalk.store import Store
from agenttalk.wrapper import loop, prompt
from agenttalk.wrapper import run as wrapper_run

TWINS = {
    "claude": SKILLS_ROOT / "claude" / "agenttalk.challenge.md",
    "codex": SKILLS_ROOT / "codex" / "agenttalk-challenge" / "SKILL.md",
}
VERDICT_META = ("challenge", "verdict", "confidence", "basis", "exposed", "minutes")


def _commands(twin: str) -> list[str]:
    """Every agenttalk command in the twin's fenced blocks, continuations joined."""
    text = TWINS[twin].read_text(encoding="utf-8").replace("\r\n", "\n")
    prefix = "agenttalk " if twin == "claude" else "python -m agenttalk "
    cont = "`" if twin == "claude" else "\\"
    commands, block, fenced = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            if not fenced:
                logical, pending = [], ""
                for raw in block:
                    part = raw.strip()
                    if part.endswith(cont):
                        pending += part[:-1] + " "
                        continue
                    logical.append(pending + part)
                    pending = ""
                commands += [c for c in logical if c.startswith(prefix)]
            block = []
            continue
        if fenced:
            block.append(line)
    return commands


def _argv(command: str, twin: str, values: dict) -> list[str]:
    prefix = "agenttalk " if twin == "claude" else "python -m agenttalk "
    command = command[len(prefix):]
    # choice placeholders (<a|b|c>) take their first option; named ones are mapped
    command = re.sub(r"<([^<>|]+)\|[^<>]*>", lambda m: m.group(1), command)
    for key, value in values.items():
        command = command.replace(key, value)
    return shlex.split(command)


def _pick(commands: list[str], sub: str, *, await_reply: bool | None = None,
          to: str | None = None) -> str:
    found = [c for c in commands
             if c.split("agenttalk ", 1)[1].startswith(sub + " ")
             and (await_reply is None or ("--await-reply" in c) == await_reply)
             and (to is None or f"--to {to} " in c)]
    assert len(found) == 1, (sub, await_reply, found)
    return found[0]


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.init(["alpha", "beta", "gamma", "liaison"])
    assert cli.main(["--root", str(tmp_path), "roster", "set-operator-facing", "liaison"]) == 0
    return store


def _values(tmp_path: Path, rid: str, sender: str) -> dict:
    brief = tmp_path / "brief.md"
    brief.write_text("## Outcome\nFACT: x\n", encoding="utf-8")
    verdict = tmp_path / "verdict.md"
    verdict.write_text("## Headline\nstop: already exists\n", encoding="utf-8")
    return {
        '"$SELF"': sender, "$SELF": sender,
        '"$REQ_ID"': rid, "$REQ_ID": rid, "$reqId": rid,
        '"$REQ_A"': rid + "-a", "$REQ_A": rid + "-a", "$reqA": rid + "-a",
        '"$REQ_B"': rid + "-b", "$REQ_B": rid + "-b", "$reqB": rid + "-b",
        "<challenger-a>": "beta", "<challenger-b>": "gamma",
        "<challenger>": "beta", "<brief.md>": brief.as_posix(), "<verdict.md>": verdict.as_posix(),
        "<RID>": rid, "<verdict>": "stop", "<n>": "3", "--timeout 900": "--timeout 5",
    }


def _run(tmp_path: Path, command: str, twin: str, values: dict) -> int:
    return cli.main(["--root", str(tmp_path), *_argv(command, twin, values)])


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_unwrapped_examples_execute_end_to_end(tmp_path, monkeypatch, twin) -> None:
    monkeypatch.delenv(wrapper_run.WRAPPER_GENERATION_ENV, raising=False)
    store = _store(tmp_path)
    commands = _commands(twin)
    rid = "ch-unwrapped-1"

    # The wrapped branch is REFUSED outside a wrapper turn - which is exactly why the
    # skill must branch (cold-read MAJOR 1: the old always-await send sent nothing).
    assert _run(tmp_path, _pick(commands, "send", await_reply=True, to="<challenger>"), twin,
                _values(tmp_path, rid, "alpha")) == 2
    assert not [m for m in store.valid_messages() if (m.meta or {}).get("request_id") == rid]

    assert _run(tmp_path, _pick(commands, "send", await_reply=False), twin,
                _values(tmp_path, rid, "alpha")) == 0
    (question,) = [m for m in store.valid_messages() if (m.meta or {}).get("request_id") == rid]
    assert question.kind == "question" and question.recipient == "beta"
    assert question.meta["challenge"] == "true" and question.meta["round"] == "1"

    # The challenger's typed verdict lands with every required meta key.
    assert _run(tmp_path, _pick(commands, "reply"), twin, _values(tmp_path, rid, "beta")) == 0
    (verdict,) = [m for m in store.messages_for("alpha") if m.sender == "beta"]
    assert verdict.kind == "message" and verdict.meta["request_id"] == rid
    assert all(verdict.meta.get(key) for key in VERDICT_META)
    assert verdict.meta["verdict"] == "proceed" and verdict.meta["exposed"] == "yes"

    # The unwrapped scoped wait returns on that verdict.
    assert _run(tmp_path, _pick(commands, "wait"), twin, _values(tmp_path, rid, "alpha")) == 0

    # Cold-read MAJOR 2: the stop-class escalation example must reach the liaison.
    assert _run(tmp_path, _pick(commands, "escalate"), twin, _values(tmp_path, rid, "alpha")) == 0
    (esc,) = [m for m in store.messages_for("liaison") if m.sender == "alpha"]
    assert esc.kind == "question" and esc.meta["needs_operator"] == "true"
    assert esc.meta["challenge"] == rid and esc.meta["challenge_verdict"] == "stop"
    assert "reply GO, DROP or ALT" in esc.body


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_wrapped_send_example_records_an_await_reply(tmp_path, monkeypatch, twin) -> None:
    store = _store(tmp_path)
    generation = "challenge-generation"
    store.write_waiting("alpha", {"agent": "alpha", "mode": "wrapper-loop",
                                  "wait_token": generation, "wrapper_generation": generation})
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, generation)
    monkeypatch.delenv(wrapper_run.INBOUND_REQUEST_ID_ENV, raising=False)
    rid = "ch-wrapped-1"

    assert _run(tmp_path, _pick(_commands(twin), "send", await_reply=True, to="<challenger>"), twin,
                _values(tmp_path, rid, "alpha")) == 0
    records, problems = store.list_awaiting("alpha")
    assert problems == [] and [r["request_id"] for r in records] == [rid]
    (question,) = [m for m in store.valid_messages() if (m.meta or {}).get("request_id") == rid]
    assert question.meta["challenge"] == "true"


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_wrapped_two_challenger_example_sends_both_before_yielding(
    tmp_path, monkeypatch, twin,
) -> None:
    # Fix round 2: the two-challenger sequence is its own block - BOTH sends, with
    # distinct ids, then `return` - so both correlations are awaited across turns.
    text = TWINS[twin].read_text(encoding="utf-8").replace("\r\n", "\n")
    block = text[text.index("--to <challenger-a>"):]
    block = block[:block.index("```")]
    assert block.index("--to <challenger-b>") < block.index("return")

    store = _store(tmp_path)
    generation = "two-challenger-generation"
    store.write_waiting("alpha", {"agent": "alpha", "mode": "wrapper-loop",
                                  "wait_token": generation, "wrapper_generation": generation})
    monkeypatch.setenv(wrapper_run.WRAPPER_GENERATION_ENV, generation)
    monkeypatch.delenv(wrapper_run.INBOUND_REQUEST_ID_ENV, raising=False)
    commands = _commands(twin)
    values = _values(tmp_path, "ch-two", "alpha")
    for target in ("<challenger-a>", "<challenger-b>"):
        assert _run(tmp_path, _pick(commands, "send", await_reply=True, to=target), twin, values) == 0
    records, problems = store.list_awaiting("alpha")
    assert problems == []
    assert sorted(r["request_id"] for r in records) == ["ch-two-a", "ch-two-b"]
    sent = {m.recipient: m.meta["request_id"] for m in store.valid_messages()
            if (m.meta or {}).get("challenge") == "true"}
    assert sent == {"beta": "ch-two-a", "gamma": "ch-two-b"}


@pytest.mark.parametrize("twin", sorted(TWINS))
def test_escalate_example_carries_no_origin_flags(twin) -> None:
    escalate = _pick(_commands(twin), "escalate")
    assert "--origin-request" not in escalate and "--origin-id" not in escalate


# ------------------------------------------------ the real wrapper transport

def _challenge_question(store: Store, rid: str = "ch-1"):
    return store.send(sender="alpha", recipient="beta", kind="question", subject="challenge: x",
                      body="## Outcome\nFACT: x\n",
                      meta={"request_id": rid, "challenge": "true", "round": "1"})


def test_wrapped_challenger_gets_no_body_only_draft_channel(tmp_path) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    q = _challenge_question(store)
    record = loop._with_reply_draft(
        store, "beta", {"id": q.id, "from": "alpha", "kind": "question", "meta": dict(q.meta)})
    assert "reply_draft" not in record
    # Even a body-only draft left at the deterministic path is never published for it:
    # it would reach the requester without the verdict meta and count as unassessed.
    path = reply_transport.reply_draft_path(store, "beta", q.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Headline\nproceed\n", encoding="utf-8")
    loop._deliver_reply_draft(store, "beta", record)
    assert not [m for m in store.messages_for("alpha") if m.sender == "beta"]


def test_typed_verdict_lands_through_the_wrapper_loop(tmp_path) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    q = _challenge_question(store, "ch-loop")
    verdict = tmp_path / "verdict.md"
    verdict.write_text("## Headline\nreshape: smaller\n", encoding="utf-8")
    seen: list[dict] = []

    def drive(rec):
        seen.append(rec)
        assert "reply_draft" not in rec
        return cli.main([
            "--root", str(tmp_path), "reply", "--from", "beta", "--to-request", "ch-loop",
            "--kind", "message", "--meta", "challenge=true", "--meta", "verdict=reshape",
            "--meta", "confidence=medium", "--meta", "basis=verified", "--meta", "exposed=no",
            "--meta", "minutes=7", "--file", str(verdict),
        ]) == 0

    assert loop.run_loop(store, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                         max_turns=1) == 1
    assert seen and seen[0]["id"] == q.id
    (reply,) = [m for m in store.messages_for("alpha") if m.sender == "beta"]
    assert reply.meta["in_reply_to"] == q.id and reply.meta["verdict"] == "reshape"
    assert all(reply.meta.get(key) for key in VERDICT_META)
    assert store.cursor("beta") == q.id


def test_a_dispatch_referencing_a_challenge_keeps_its_draft_channel(tmp_path) -> None:
    # The dispatch reuses the `challenge` key for a request-id reference; only the exact
    # challenge=true marker on a QUESTION loses the draft channel.
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    store.set_role("alpha", "lead")
    t = store.send(sender="alpha", recipient="beta", kind="task", body="do X",
                   meta={"request_id": "tk-1", "challenge": "ch-9",
                         "challenge_verdict": "proceed", "challenge_disposition": "accepted"})
    record = loop._with_reply_draft(
        store, "beta", {"id": t.id, "from": "alpha", "kind": "task", "meta": dict(t.meta)})
    assert isinstance(record.get("reply_draft"), dict)


def test_a_question_assignment_referencing_a_challenge_keeps_its_draft_channel(tmp_path) -> None:
    # The lead skill dispatches assignments as `send --kind question` carrying
    # `--meta challenge=<request_id>`: a QUESTION whose challenge value is a reference,
    # not the marker. It must keep the draft channel and the ordinary reply prompt.
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    meta = {"request_id": "q-assign", "challenge": "ch-9",
            "challenge_verdict": "proceed", "challenge_disposition": "accepted"}
    q = store.send(sender="alpha", recipient="beta", kind="question", body="do X", meta=meta)
    record = loop._with_reply_draft(
        store, "beta", {"id": q.id, "from": "alpha", "kind": "question", "meta": dict(q.meta)})
    assert isinstance(record.get("reply_draft"), dict)
    rec = {"from": "alpha", "to": "beta", "kind": "question", "body": "do X",
           "correlation_id": "q-assign", "request_id": "q-assign", "broadcast_id": None,
           "id": "m-2", "meta": meta}
    assert "This is a CHALLENGE question" not in prompt.assemble_turn_prompt(rec)


def test_wrapped_prompt_routes_the_challenger_to_the_typed_cli_verdict() -> None:
    rec = {"from": "alpha", "to": "beta", "kind": "question", "body": "brief",
           "correlation_id": "ch-1", "request_id": "ch-1", "broadcast_id": None,
           "id": "m-1", "meta": {"request_id": "ch-1", "challenge": "true", "round": "1"}}
    p = prompt.assemble_turn_prompt(rec)
    assert "PREFERRED DRAFT CHANNEL" not in p
    assert "This is a CHALLENGE question" in p
    assert "--to-request ch-1 --kind message --meta challenge=true" in p
    for key in VERDICT_META:
        assert f"--meta {key}=" in p
    assert "question with meta.challenge=true: you are the CHALLENGER" in p
