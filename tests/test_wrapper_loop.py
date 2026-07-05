"""Phase B of the wrapper integration (design C): the wrapper-owned listen loop,
per-turn prompt assembly, and per-CLI session continuity. Driven entirely with a
fixture Store + injected drive/spawn - NO real CLI.
"""

from __future__ import annotations

import errno
import gc
import json
from pathlib import Path
import re
import sys

import pytest

from agenttalk import capacity as capmod
from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import loop, prompt, run, session


def _store(tmp_path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


# --------------------------------------------------------------- prompt

def test_prompt_assembly_includes_message_and_rules() -> None:
    rec = {"from": "alpha", "kind": "review-request", "subject": "s",
           "body": "please review X", "correlation_id": "rq-1",
           "request_id": "rq-1", "broadcast_id": None}
    p = prompt.assemble_turn_prompt(rec)
    assert "please review X" in p and "alpha" in p and "review-request" in p
    assert "rq-1" in p and "HOW TO HANDLE" in p
    assert "REJOIN CONTEXT" not in p          # no rejoin by default
    p2 = prompt.assemble_turn_prompt(rec, rejoin="roster: a,b", rules="custom rules")
    assert "REJOIN CONTEXT" in p2 and "roster: a,b" in p2 and "custom rules" in p2


def test_prompt_includes_meta_for_classification(tmp_path) -> None:
    # codex finding: classification data often lives ONLY in meta (review-result
    # status/needs-info, consult round, ...). The full record must reach the model.
    rec = {"from": "alpha", "to": "beta", "kind": "review-result", "subject": "",
           "body": "see status", "meta": {"status": "needs-info", "round": 3},
           "request_id": "rq-1", "broadcast_id": None, "correlation_id": "rq-1"}
    p = prompt.assemble_turn_prompt(rec)
    assert "needs-info" in p and '"round": 3' in p   # meta-only data is present
    assert "review-result" in p and "to: beta" in p


def test_prompt_is_pure_handler_no_consume_full_classification_and_safety() -> None:
    """Pre-0.30.0 BLOCKER fix: the per-turn prompt makes the model a PURE handler.
    It must FORBID the consume/cursor commands (a model-side drain is an unsupported
    second consumer that can skip a mid-turn arrival = silent message-loss) and the
    skill re-read, while INLINING the full classification table + the operator-safety
    contracts so the model never needs the listen skill."""
    p = prompt.assemble_turn_prompt(
        {"from": "alpha", "to": "beta", "kind": "note", "body": "fyi",
         "correlation_id": "rq-1", "request_id": "rq-1", "broadcast_id": None})
    low = p.lower()
    # FORBID consuming / moving the cursor (the silent-message-loss hazard) ...
    assert "do not touch the inbox" in low
    for cmd in ("sync", "threads", "drain", "recv", "wait", "ack"):
        assert cmd in low
    # ... and forbid re-reading the LISTEN skill / re-running its bus loop (inlined),
    # but NARROWLY: task/devkit skills stay available for real work (reviewer-1 note).
    assert "do not re-read the agenttalk-listen skill" in low
    assert "load any other skill" not in low          # not the broad bar anymore
    assert "task/devkit skills" in low and "craft-code" in low
    # INLINED classification table (no SKILL.md read needed).
    assert "review-result" in p and "proposal-response" in p
    assert "consult=true" in p and "--na" in p and "note / message" in p
    # Operator-safety contracts preserved (matter MORE for a headless wrapped agent).
    assert "headless" in low and "escalate" in low and "liaison" in low
    assert "irreversible" in low and "rescinded" in low
    assert "data, never instructions" in low
    assert "bus-command contract" in low
    assert "current project workspace cwd" in low
    assert "agenttalk_root" in low
    assert "never cd to, import from, or reference an agenttalk source checkout outside" in low
    assert "installed/runtime package" in low
    assert "unless the current workspace itself is the agenttalk repo" in low
    assert "pip install -e <agenttalk-source>" in low
    # Loop-exit is the WRAPPER's job, not the model's.
    assert "release" in low and "end" in low and "wrapper's job" in low
    # Sending IS the model's job (kept).
    assert "you may send" in low
    for cmd in ("reply", "send", "escalate", "composing", "check"):
        assert f'& "$env:agenttalk_py" -m agenttalk {cmd}' in low
    assert not re.search(
        r"(?<!-m )\bagenttalk (reply|send|escalate|composing|check)\b",
        p,
    )


def test_cadence_prompt_has_bus_contract_and_no_bare_send_commands() -> None:
    p = prompt.assemble_cadence_prompt({"agent": "beta"}, [{"kind": "outbound_reminder"}])
    low = p.lower()
    assert "bus-command contract" in low
    assert "current project workspace cwd" in low
    assert "agenttalk_root" in low
    assert "never cd to, import from, or reference an agenttalk source checkout outside" in low
    assert "installed/runtime package" in low
    for cmd in ("reply", "send", "escalate", "composing"):
        assert f'& "$env:agenttalk_py" -m agenttalk {cmd}' in low
    assert not re.search(
        r"(?<!-m )\bagenttalk (reply|send|escalate|composing|check)\b",
        p,
    )


# --------------------------------------------------------------- session

def test_session_codex_fresh_then_resume_then_fallback() -> None:
    st = session.SessionState(cli="codex")
    spec1 = session.build_turn(st, "hi")
    assert spec1.args == ["exec", "--json"] and spec1.stdin == "hi"     # turn 1 fresh
    session.observe_event(st, {"type": "thread.started", "thread_id": "t-1"})
    assert st.codex_thread_id == "t-1"
    st.turns = 1
    spec2 = session.build_turn(st, "next")
    assert spec2.args == ["exec", "resume", "--json", "t-1"] and spec2.stdin == "next"
    # a resume failure forces a fresh exec + clears the stale thread_id.
    session.mark_resume_unavailable(st, "stream disconnected")
    spec3 = session.build_turn(st, "again")
    assert spec3.args == ["exec", "--json"] and st.codex_thread_id is None


def test_session_claude_session_id_then_resume() -> None:
    st = session.SessionState(cli="claude", claude_session_id="sid-1")
    spec1 = session.build_turn(st, "hi")                                # turn 0
    assert spec1.args[:1] == ["-p"] and "--session-id" in spec1.args
    assert "sid-1" in spec1.args and "--include-partial-messages" in spec1.args
    st.turns = 1
    spec2 = session.build_turn(st, "next")
    assert "--resume" in spec2.args and "sid-1" in spec2.args
    with pytest.raises(ValueError):           # claude requires a minted session id
        session.build_turn(session.SessionState(cli="claude"), "x")


def test_session_unknown_cli_raises() -> None:
    with pytest.raises(ValueError):
        session.build_turn(session.SessionState(cli="gemini"), "x")


def test_session_observe_event_is_codex_only() -> None:
    st = session.SessionState(cli="claude", claude_session_id="sid-1")
    session.observe_event(st, {"type": "thread.started", "thread_id": "t-9"})
    assert st.codex_thread_id is None         # claude has no codex thread_id


def test_session_resume_ledger_torn_reads_degrade_low(tmp_path) -> None:
    s = _store(tmp_path)
    path = s.state_dir / "beta.wrapper-session.json"
    path.write_text(json.dumps({
        "cli": "codex",
        "codex_thread_id": "t-old",
        "resume_failure_count": "not-a-number",
        "resume_attempt_state": "in_progress",
        "resume_attempt_key": "beta:codex:t-old",
        "resume_attempt_msg_id": "m-1",
    }), encoding="utf-8")

    st = session.load_session(s, "beta", "codex")

    assert st.resume_failure_count == 0
    assert st.resume_attempt_state == "ambiguous"
    assert st.resume_attempt_msg_id == ""


def test_session_resume_attempt_start_records_auditable_metadata() -> None:
    st = session.SessionState(cli="codex", codex_thread_id="t-old")

    session.resume_attempt_start(st, agent="beta", msg_id="msg-1")

    assert st.resume_attempt_state == "in_progress"
    assert st.resume_attempt_key == "beta:codex:t-old"
    assert st.resume_attempt_msg_id == "msg-1"
    assert st.resume_attempt_id
    assert st.resume_attempt_started_at.endswith("Z")


# --------------------------------------------------------------- loop

def test_is_terminal_control() -> None:
    assert loop.is_terminal_control({"scoped": {"closed": True}}) is True
    assert loop.is_terminal_control({"scoped": {"superseded": True}}) is True
    assert loop.is_terminal_control({"scoped": {"closed": False, "superseded": False}}) is False
    assert loop.is_terminal_control({"scoped": None}) is False
    assert loop.is_terminal_control({}) is False


def _human_meta(reason="operator says wrap up"):
    return {"release_authority": "human", "operator_decision": "true",
            "authority_reason": reason}


def test_classify_loop_control(tmp_path) -> None:
    # stand-down authority (0.39.0): stop ONLY on an authorized relay + a valid
    # authority marker + a reason; everything else is invalid_control or ordinary.
    s = _store(tmp_path)
    s.set_operator_facing("alpha")                    # alpha is the authorized relay
    stop = {"kind": "release", "from": "alpha", "meta": _human_meta()}
    assert loop.classify_loop_control(s, stop) == "stop"
    emer = {"kind": "release", "from": "alpha",
            "meta": {"release_authority": "emergency", "emergency": "true",
                     "operator_report_required": "true",
                     "authority_reason": "alpha looks rogue"}}
    assert loop.classify_loop_control(s, emer) == "stop"
    # MIXED markers (raw bus message bypassing the CLI) -> invalid_control: the
    # human-vs-emergency audit distinction must be preserved (exactly one mode).
    mixed = {"kind": "release", "from": "alpha",
             "meta": {"release_authority": "human", "operator_decision": "true",
                      "emergency": "true", "authority_reason": "ambiguous"}}
    assert loop.classify_loop_control(s, mixed) == "invalid_control"
    # emergency missing the operator_report_required audit marker -> invalid_control
    emer_noreport = {"kind": "release", "from": "alpha",
                     "meta": {"release_authority": "emergency", "emergency": "true",
                              "authority_reason": "x"}}
    assert loop.classify_loop_control(s, emer_noreport) == "invalid_control"
    # unauthorized sender -> invalid_control
    assert loop.classify_loop_control(
        s, {"kind": "release", "from": "beta", "meta": _human_meta()}) == "invalid_control"
    # authorized but UNMARKED release -> invalid_control
    assert loop.classify_loop_control(
        s, {"kind": "release", "from": "alpha", "meta": {}}) == "invalid_control"
    # marker but NO reason -> invalid_control
    assert loop.classify_loop_control(
        s, {"kind": "release", "from": "alpha",
            "meta": {"release_authority": "human", "operator_decision": "true"}}) == "invalid_control"
    # UNMARKED end (the old bypass) -> invalid_control, never stop
    assert loop.classify_loop_control(s, {"kind": "end", "from": "alpha", "meta": {}}) == "invalid_control"
    # ordinary kinds never stop
    assert loop.classify_loop_control(s, {"kind": "message", "from": "alpha"}) == "ordinary"


def test_loop_stops_on_marked_authorized_release(tmp_path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("alpha")
    s.send(sender="alpha", recipient="beta", body="do work")
    rel = s.send(sender="alpha", recipient="beta", kind="release", body="stand down",
                 meta=_human_meta())
    s.send(sender="alpha", recipient="beta", body="after release")  # must NOT be driven
    seen: list[str] = []

    def drive(rec):
        seen.append(rec["body"])
        return True

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_polls=10)
    assert seen == ["do work"] and turns == 1
    assert s.cursor("beta") == rel.id                 # committed the release, stopped there


def test_loop_ignores_unmarked_release_and_keeps_listening(tmp_path) -> None:
    # the actual failure: an UNMARKED (or unauthorized) release must NOT stand the
    # listener down - it is committed (no redeliver, not driven) and the loop continues.
    s = _store(tmp_path)
    s.set_operator_facing("alpha")
    rel = s.send(sender="alpha", recipient="beta", kind="release", body="casual sign-off")
    seen: list[str] = []

    def drive(rec):
        seen.append(rec["body"])
        return True

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_polls=5)
    assert seen == [] and turns == 0                  # never driven, never stopped early
    assert s.cursor("beta") == rel.id                 # committed (won't redeliver)


def test_loop_ignores_unmarked_end(tmp_path) -> None:
    # a RECEIVED unmarked end no longer winds a peer down (the narrowing).
    s = _store(tmp_path)
    s.set_operator_facing("alpha")
    end = s.send(sender="alpha", recipient="beta", kind="end", body="bye")
    drove = {"n": 0}

    def drive(rec):
        drove["n"] += 1
        return True

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_polls=5)
    assert turns == 0 and drove["n"] == 0             # never driven
    assert s.cursor("beta") == end.id                 # committed + kept listening


def test_loop_drives_each_message_and_commits(tmp_path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="one")
    m2 = s.send(sender="alpha", recipient="beta", body="two")
    seen: list[str] = []

    def drive(rec):
        seen.append(rec["body"])
        return True                      # success -> commit

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_turns=2)
    assert turns == 2 and seen == ["one", "two"]
    # commit on SUCCESS advanced the global cursor to the newest handled msg.
    assert s.cursor("beta") == m2.id


def test_loop_does_not_commit_failed_turn(tmp_path) -> None:
    # a failed turn (drive -> falsy) is NOT committed: the message re-delivers.
    # dead-letter DISABLED here (k_poison=0) to test the underlying retry mechanics;
    # the dead-letter-on path is covered in test_dead_letter.py.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="one")
    attempts = {"n": 0}

    def failing_drive(rec):
        attempts["n"] += 1
        return False                     # turn failed

    loop.run_loop(s, "beta", failing_drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_polls=3, k_poison=0, k_escalate=0)
    assert attempts["n"] >= 1            # the same record was retried, never committed
    assert s.cursor("beta") == ""        # NOT committed -> re-delivered


def test_loop_idle_stamps_heartbeat(tmp_path) -> None:
    s = _store(tmp_path)            # no messages -> pure idle
    t = {"n": 0.0}

    def clock():
        t["n"] += 100.0            # advance well past heartbeat_interval each poll
        return t["n"]

    turns = loop.run_loop(s, "beta", lambda rec: None, clock=clock,
                          sleep=lambda d: None, max_polls=3, heartbeat_interval=10.0)
    assert turns == 0
    assert s.read_heartbeat("beta") is not None   # idle kept the heartbeat fresh


# --------------------------------------------- make_drive (run.py, injected spawn)

def _codex_turn_lines(thread_id: str = "t-1", text: str = "done") -> list[str]:
    return [json.dumps(o) for o in [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    ]]


def test_make_drive_codex_captures_thread_id_and_resumes(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    spawned: list[tuple[list[str], str | None]] = []

    def fake_spawn(argv, stdin):
        spawned.append((argv, stdin))
        return _codex_turn_lines()

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    drive({"from": "a", "kind": "message", "body": "hello",
           "correlation_id": None, "request_id": None, "broadcast_id": None})
    # turn 1: fresh exec, prompt on stdin; thread_id captured from thread.started.
    assert spawned[0][0] == ["codex", "exec", "--json"] and "hello" in spawned[0][1]
    assert st.codex_thread_id == "t-1" and st.turns == 1
    assert s.read_heartbeat("beta") is not None     # engine stamped on progress
    # turn 2: resume by the durable thread_id.
    drive({"from": "a", "kind": "message", "body": "again",
           "correlation_id": None, "request_id": None, "broadcast_id": None})
    assert spawned[1][0] == ["codex", "exec", "resume", "--json", "t-1"]
    assert st.turns == 2


def _failed_turn_lines(msg: str = "no session") -> list[str]:
    return [json.dumps({"type": "turn.failed", "error": {"message": msg}})]


def test_make_drive_resume_failure_gives_up_after_two_then_fresh_succeeds(tmp_path) -> None:
    # Slice 1 B4: a failed resume does not inline-spawn fresh. After two consecutive
    # session-attributable failures, resume is marked unavailable; the next attempt is fresh.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex", codex_thread_id="t-old", resume_available=True)
    spawned: list[list[str]] = []

    def fake_spawn(argv, stdin):
        spawned.append(argv)
        if "resume" in argv:
            return _failed_turn_lines()              # the resume turn FAILS (terminal)
        return _codex_turn_lines(thread_id="t-new")  # the fresh exec SUCCEEDS

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    rec = {"from": "a", "kind": "message", "body": "hi",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    one = drive(rec)
    assert one.ok is False and one.failure_class == loop.CLASS_AMBIGUOUS
    assert spawned == [["codex", "exec", "resume", "--json", "t-old"]]
    assert st.resume_available is True and st.resume_failure_count == 1
    two = drive(rec)
    assert two.ok is False and "resume_unavailable" in two.summary
    assert st.resume_available is False and st.codex_thread_id is None
    three = drive(rec)
    assert three.ok is True
    assert spawned[-1] == ["codex", "exec", "--json"]
    assert st.codex_thread_id == "t-new" and st.turns == 1


def test_make_drive_resume_first_failure_does_not_mark_unavailable(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex", codex_thread_id="t-old")

    def fake_spawn(argv, stdin):
        return _failed_turn_lines("no session")        # session-attributable resume failure

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    ok = drive({"from": "a", "kind": "message", "body": "hi",
                "correlation_id": None, "request_id": None, "broadcast_id": None})
    assert ok.ok is False                              # genuine failure -> no commit
    assert st.resume_available is True                 # first failure only records the ledger
    assert st.resume_failure_count == 1
    assert st.turns == 0                               # a failed turn does not advance


def test_make_drive_resume_infra_failures_do_not_consume_b4_ceiling(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex", codex_thread_id="t-old")
    spawned: list[list[str]] = []

    def fake_spawn(argv, stdin):
        spawned.append(argv)
        return _failed_turn_lines("HTTP 529 overloaded")

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    rec = {"id": "msg-1", "from": "a", "kind": "message", "body": "hi",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    one = drive(rec)
    two = drive(rec)

    assert one.ok is False and one.failure_class == loop.CLASS_INFRA
    assert two.ok is False and two.failure_class == loop.CLASS_INFRA
    assert st.resume_available is True
    assert st.resume_failure_count == 0
    assert st.codex_thread_id == "t-old"
    assert spawned == [
        ["codex", "exec", "resume", "--json", "t-old"],
        ["codex", "exec", "resume", "--json", "t-old"],
    ]


def test_make_drive_resume_ambiguous_non_session_failure_does_not_consume_b4_ceiling(
    tmp_path,
) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex", codex_thread_id="t-old")

    def fake_spawn(argv, stdin):
        return _failed_turn_lines("child killed by supervisor")

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    rec = {"id": "msg-1", "from": "a", "kind": "message", "body": "hi",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    out = drive(rec)

    assert out.ok is False and out.failure_class == loop.CLASS_AMBIGUOUS
    assert st.resume_available is True
    assert st.resume_failure_count == 0
    assert st.codex_thread_id == "t-old"


def test_make_drive_resume_give_up_sends_one_continuity_loss_signal(tmp_path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("alpha")
    st = session.SessionState(cli="codex", codex_thread_id="t-old", resume_available=True)

    def fake_spawn(argv, stdin):
        if "resume" in argv:
            return _failed_turn_lines("no session")
        return _codex_turn_lines(thread_id="t-new")

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    rec = {"id": "msg-1", "from": "a", "kind": "message", "body": "hi",
           "correlation_id": None, "request_id": None, "broadcast_id": None}

    assert drive(rec).ok is False
    assert not [m for m in s.valid_messages() if m.subject == "wrapper resume continuity loss"]
    assert drive(rec).ok is False
    notices = [m for m in s.valid_messages() if m.subject == "wrapper resume continuity loss"]
    assert len(notices) == 1
    assert notices[0].recipient == "alpha"
    assert notices[0].meta["resume_unavailable"] == "true"
    assert notices[0].meta["continuity_lost"] == "true"
    assert notices[0].meta["msg_id"] == "msg-1"

    assert drive(rec).ok is True
    assert len([m for m in s.valid_messages()
                if m.subject == "wrapper resume continuity loss"]) == 1
    assert st.codex_thread_id == "t-new"
    assert st.fresh_session_success_reason == "fresh_session_success"


class _Stream:
    """A spawner result with a controllable child exit code (codex r2)."""

    def __init__(self, lines, returncode=None):
        self._lines = lines
        self.returncode = returncode

    def __iter__(self):
        return iter(self._lines)


class _PipeTeardownStream:
    """A spawner result that raises a benign pipe error after its last line."""

    def __init__(self, lines, exc: OSError, returncode=0):
        self._lines = lines
        self._exc = exc
        self.returncode = returncode

    def __iter__(self):
        yield from self._lines
        raise self._exc


def test_make_drive_partial_stream_is_failure(tmp_path) -> None:
    # codex r2 MAJOR 1: a stream that started a turn but never reached a COMPLETED
    # boundary (no turn.completed / message_stop) is NOT success - the message must
    # not be committed for an incomplete turn.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    partial = [json.dumps(o) for o in [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},          # ... then the child dies; no turn.completed
    ]]
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda a, i: partial, clock=lambda: 0.0, render=False)
    # fresh exec has no resume to retry, so a partial first turn just fails.
    assert drive({"from": "a", "kind": "message", "body": "hi",
                  "correlation_id": None, "request_id": None, "broadcast_id": None}).ok is False
    assert st.turns == 0
    # reviewer-1 gate: a FAILED turn must NOT leave a fresh heartbeat (so a
    # persistently-crashing child goes stale -> supervisor restart).
    assert s.read_heartbeat("beta") is None


def test_make_drive_nonzero_exit_is_failure(tmp_path) -> None:
    # codex r2 MAJOR 1: even a stream that reached turn.completed is NOT success if
    # the child exited nonzero.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    drive = run.make_drive(
        s, "beta", "codex", st, ["codex"], clock=lambda: 0.0, render=False,
        spawn=lambda a, i: _Stream(_codex_turn_lines(), returncode=1),
    )
    assert drive({"from": "a", "kind": "message", "body": "hi",
                  "correlation_id": None, "request_id": None, "broadcast_id": None}).ok is False
    assert s.read_heartbeat("beta") is None     # reviewer-1 gate: no heartbeat on failure


def test_make_drive_ignores_benign_einval_after_completed_turn(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    stream = _PipeTeardownStream(
        _codex_turn_lines(), OSError(errno.EINVAL, "Invalid argument"), returncode=0)
    drive = run.make_drive(
        s, "beta", "codex", st, ["codex"], clock=lambda: 0.0, render=False,
        spawn=lambda a, i: stream,
    )

    out = drive({"from": "a", "kind": "message", "body": "hi",
                 "correlation_id": None, "request_id": None, "broadcast_id": None})

    assert out.ok is True
    assert st.turns == 1
    assert s.read_heartbeat("beta") is not None


def test_make_drive_benign_broken_pipe_is_not_classified_infra(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    partial = [json.dumps({"type": "thread.started", "thread_id": "t"}),
               json.dumps({"type": "turn.started"})]
    stream = _PipeTeardownStream(
        partial, OSError(errno.EPIPE, "Broken pipe"), returncode=0)
    drive = run.make_drive(
        s, "beta", "codex", st, ["codex"], clock=lambda: 0.0, render=False,
        spawn=lambda a, i: stream,
    )

    out = drive({"from": "a", "kind": "message", "body": "hi",
                 "correlation_id": None, "request_id": None, "broadcast_id": None})

    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "partial stream" in out.summary


def test_make_drive_classifies_failures_for_dead_letter(tmp_path) -> None:
    # dead-letter taxonomy. A terminal turn.failed is low-cap POISON ONLY when its error text
    # POSITIVELY matches a message-content signature (size / content-policy); a 529/rate-limit
    # or edge-block terminal is an OUTAGE -> INFRA (never false-DL at cap 3), and an
    # unrecognized terminal cause is AMBIGUOUS (lead-verify P1 + codex ruling). A partial
    # stream (started, no completion) is AMBIGUOUS; launch-spawn lookup errors and
    # deterministic spawn permission denials are CONFIG_BLOCKED.
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}

    def _drive(spawn):
        return run.make_drive(_store(tmp_path), "beta", "codex",
                              session.SessionState(cli="codex"), ["codex"],
                              spawn=spawn, clock=lambda: 0.0, render=False)

    # codex unified taxonomy - terminal failure split 3 ways by its error TEXT:
    # (a) infra signature -> INFRA (never auto-DL). Includes edge/WAF/reverse-proxy blocks
    #     (lead C4): a gateway/proxy/firewall block is an OUTAGE signature, not message-poison.
    for msg in ("HTTP 529 overloaded", "rate limit exceeded", "503 service unavailable",
                "401 unauthorized: invalid api key", "Request blocked by upstream proxy",
                "blocked by waf", "403 forbidden", "request rejected by firewall"):
        o = _drive(lambda a, i, m=msg: _failed_turn_lines(m))(rec)
        assert o.ok is False and o.failure_class == loop.CLASS_INFRA, msg
    # (b) EXPLICIT message-content-attributable failure -> POISON (the @K_poison fast-path):
    #     ONLY the size + content-policy families (this message is the deterministic cause).
    #     "blocked by content policy"/"by safety" stay POISON - the C4 edge tokens above do NOT
    #     include bare "blocked", so they never SHADOW a real content-policy block (lead C4).
    #     "violates"/"flagged by" are poison ONLY when QUALIFIED by content/policy/safety/
    #     filter (codex marker-conservatism ruling #1).
    for msg in ("context length exceeded", "prompt is too long",
                "content policy violation", "request blocked by safety policy",
                "blocked by content policy",
                "violates content policy", "flagged by content filter"):
        o = _drive(lambda a, i, m=msg: _failed_turn_lines(m))(rec)
        assert o.ok is False and o.failure_class == loop.CLASS_POISON, msg
    # (c) UNKNOWN / unrecognized terminal cause -> AMBIGUOUS, NOT poison (codex ruling:
    #     an unobserved cause must never false-DL at the low cap). Includes GENERIC
    #     request-level / GLOBAL CONFIG errors (model-not-found, unsupported-parameter,
    #     invalid request), bare "too long" (a timeout-ish outage), and UNQUALIFIED
    #     "violates" (a constraint/quota failure) - none are message-content poison
    #     (codex re-review P1 + marker-conservatism ruling #1).
    for msg in ("tool execution failed: bad arg", "",
                "invalid request: model not found",
                "invalid request: unsupported parameter 'temperature'",
                "400 bad request: malformed", "unprocessable entity",
                "server took too long to respond",          # bare "too long" -> NOT poison
                "request violates server constraints"):      # unqualified "violates" -> NOT poison
        o = _drive(lambda a, i, m=msg: _failed_turn_lines(m))(rec)
        assert o.ok is False and o.failure_class == loop.CLASS_AMBIGUOUS, msg
    # partial stream (started, never completed) -> AMBIGUOUS
    partial = [json.dumps({"type": "thread.started", "thread_id": "t"}),
               json.dumps({"type": "turn.started"})]
    o_part = _drive(lambda a, i: partial)(rec)
    assert o_part.ok is False and o_part.failure_class == loop.CLASS_AMBIGUOUS

    def _boom(a, i):
        raise FileNotFoundError("no codex binary")

    o_spawn = _drive(_boom)(rec)
    assert o_spawn.ok is False and o_spawn.failure_class == loop.CLASS_CONFIG_BLOCKED
    assert "subtype=launch_cli" in o_spawn.summary

    def _denied(a, i):
        raise PermissionError(13, "Access is denied")

    o_denied = _drive(_denied)(rec)
    assert o_denied.ok is False and o_denied.failure_class == loop.CLASS_CONFIG_BLOCKED


def test_agenttalk_runtime_path_preflight_classifies_install_and_source_paths(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    site_pkg = tmp_path / "venv" / "Lib" / "site-packages" / "agenttalk" / "__init__.py"
    site_pkg.parent.mkdir(parents=True)
    site_pkg.write_text("# installed\n", encoding="utf-8")
    assert run.agenttalk_runtime_config_blocked_summary(str(site_pkg), workspace) is None

    outside = tmp_path / "sibling-agenttalk" / "src" / "agenttalk" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("# source\n", encoding="utf-8")
    blocked = run.agenttalk_runtime_config_blocked_summary(str(outside), workspace)
    assert blocked is not None
    assert "out-of-workspace source checkout" in blocked
    assert "install agenttalk non-editable" in blocked

    in_workspace = workspace / "vendor" / "agenttalk-src" / "src" / "agenttalk" / "__init__.py"
    in_workspace.parent.mkdir(parents=True)
    in_workspace.write_text("# editable inside workspace\n", encoding="utf-8")
    assert run.agenttalk_runtime_config_blocked_summary(str(in_workspace), workspace) is None

    repo = tmp_path / "agenttalk"
    (repo / "src" / "agenttalk").mkdir(parents=True)
    (repo / "src" / "agenttalk" / "__init__.py").write_text("# source\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "agenttalk"\n', encoding="utf-8")
    assert run.agenttalk_runtime_config_blocked_summary(
        str(repo / "src" / "agenttalk" / "__init__.py"),
        repo,
    ) is None


def test_agenttalk_runtime_preflight_uses_workspace_cwd_and_reports_bad_source(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "sibling-agenttalk" / "src" / "agenttalk" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("# source\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def runner(argv, cwd, env, timeout):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["env"] = env
        seen["timeout"] = timeout
        return 0, json.dumps({"file": str(outside)})

    blocked = run.preflight_agenttalk_runtime(workspace_root=workspace, runner=runner)
    assert seen["argv"][0] == seen["env"]["AGENTTALK_PY"]
    assert seen["argv"][1] == "-c"
    assert seen["cwd"] == str(workspace.resolve())
    assert "AGENTTALK_LEAD_LOOP_LEASE" not in seen["env"]
    assert seen["env"]["AGENTTALK_ROOT"] == str(workspace.resolve())
    assert blocked is not None
    assert str(outside.resolve()) in blocked
    assert "install agenttalk non-editable" in blocked


def test_child_env_stamps_pin_root_and_strips_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTTALK_LEAD_LOOP_LEASE", "secret")
    env = run._child_env(tmp_path)
    assert env["AGENTTALK_PY"] == str(Path(sys.executable).resolve())
    assert env["AGENTTALK_ROOT"] == str(tmp_path.resolve())
    assert "AGENTTALK_LEAD_LOOP_LEASE" not in env


def test_child_creationflags_set_no_window_only_for_hidden_windows() -> None:
    flag = 0x08000000
    assert run._child_creationflags(
        {run._NO_CHILD_WINDOW_ENV: "1"},
        is_windows=True,
        create_no_window=flag,
    ) == flag
    assert run._child_creationflags(
        {run._NO_CHILD_WINDOW_ENV: "true"},
        is_windows=True,
        create_no_window=flag,
    ) == flag
    assert run._child_creationflags(
        {run._NO_CHILD_WINDOW_ENV: "0"},
        is_windows=True,
        create_no_window=flag,
    ) == 0
    assert run._child_creationflags(
        {run._NO_CHILD_WINDOW_ENV: "1"},
        is_windows=False,
        create_no_window=flag,
    ) == 0


def test_hidden_wrapper_spawn_paths_pass_creationflags(monkeypatch) -> None:
    monkeypatch.setenv(run._NO_CHILD_WINDOW_ENV, "1")
    calls: list[dict] = []

    class _Stdin:
        def write(self, text):
            _ = text

        def close(self):
            pass

    class _Stdout:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    class _Popen:
        def __init__(self, argv, **kwargs):
            calls.append({"argv": argv, **kwargs})
            self.stdin = _Stdin()
            self.stdout = _Stdout()
            self.pid = 123

        def poll(self):
            return None

        def wait(self):
            return 0

    def fake_window_kwargs(env):
        assert env[run._NO_CHILD_WINDOW_ENV] == "1"
        return {"creationflags": 99}

    monkeypatch.setattr(run, "_child_window_kwargs", fake_window_kwargs)
    monkeypatch.setattr(run.subprocess, "Popen", _Popen)

    run.run_wrapper(cli="codex", agent="beta", argv=["codex"], store=None, render=False)
    run._ProcStream(["codex"], "prompt")

    assert [c["creationflags"] for c in calls] == [99, 99]


def _patch_procstream_popen(monkeypatch, lines, *, close_exc: OSError | None = None) -> None:
    class _Stdin:
        def write(self, text):
            _ = text

        def close(self):
            pass

    class _Stdout:
        def __iter__(self):
            return iter(lines)

        def close(self):
            if close_exc is not None:
                raise close_exc

    class _Popen:
        def __init__(self, argv, **kwargs):
            _ = argv, kwargs
            self.stdin = _Stdin()
            self.stdout = _Stdout()
            self.pid = 123

        def poll(self):
            return None

        def wait(self):
            return 0

    monkeypatch.setattr(run.subprocess, "Popen", _Popen)


class _TrackedPipe:
    def __init__(self, lines: list[str] | None = None, write_exc: OSError | None = None) -> None:
        self._lines = lines or []
        self._write_exc = write_exc
        self.closed = False
        self.close_count = 0

    def write(self, text):
        _ = text
        if self._write_exc is not None:
            raise self._write_exc

    def close(self):
        self.close_count += 1
        self.closed = True

    def __iter__(self):
        return iter(self._lines)


class _TrackedPopen:
    def __init__(self, argv, **kwargs):
        _ = argv, kwargs
        self.stdin = _TrackedPipe(write_exc=OSError(errno.EINVAL, "Invalid argument"))
        self.stdout = _TrackedPipe()
        self.pid = 123
        self.returncode = None
        self.terminated = False
        self.wait_calls: list[float | None] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _patch_procstream_popen_write_error(monkeypatch) -> list[_TrackedPopen]:
    created: list[_TrackedPopen] = []

    class _Popen(_TrackedPopen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            created.append(self)

    monkeypatch.setattr(run.subprocess, "Popen", _Popen)
    return created


def test_procstream_stdout_close_einval_is_swallowed(monkeypatch) -> None:
    lines = _codex_turn_lines()
    _patch_procstream_popen(
        monkeypatch, lines, close_exc=OSError(errno.EINVAL, "Invalid argument"))

    stream = run._ProcStream(["codex"], "prompt")

    assert list(stream) == lines
    assert stream.returncode == 0


def test_procstream_generator_finalizer_einval_is_quiet(monkeypatch, capsys) -> None:
    lines = _codex_turn_lines()
    _patch_procstream_popen(
        monkeypatch, lines, close_exc=OSError(errno.EINVAL, "Invalid argument"))
    stream = run._ProcStream(["codex"], "prompt")
    gen = iter(stream)
    assert next(gen) == lines[0]

    del gen
    gc.collect()

    assert stream.returncode == 0
    assert "Exception ignored" not in capsys.readouterr().err


def test_procstream_constructor_write_error_closes_pipes_and_child(monkeypatch) -> None:
    created = _patch_procstream_popen_write_error(monkeypatch)

    with pytest.raises(OSError):
        run._ProcStream(["codex"], "prompt")

    proc = created[0]
    assert proc.stdin.closed is True
    assert proc.stdin.close_count == 1
    assert proc.stdout.closed is True
    assert proc.stdout.close_count == 1
    assert proc.terminated is True
    assert proc.wait_calls == [10.0]


def test_procstream_success_closes_stdout_once(monkeypatch) -> None:
    lines = _codex_turn_lines()
    created: list[_TrackedPopen] = []

    class _Popen(_TrackedPopen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            self.stdin = _TrackedPipe()
            self.stdout = _TrackedPipe(lines)
            created.append(self)

    monkeypatch.setattr(run.subprocess, "Popen", _Popen)

    stream = run._ProcStream(["codex"], "prompt")

    proc = created[0]
    assert proc.stdin.closed is True
    assert proc.stdin.close_count == 1
    assert proc.stdout.close_count == 0
    assert list(stream) == lines
    assert proc.stdout.closed is True
    assert proc.stdout.close_count == 1
    assert proc.terminated is False
    assert stream.returncode == 0
    assert proc.wait_calls == [None]


def test_make_drive_procstream_write_error_stays_infra(tmp_path, monkeypatch) -> None:
    created = _patch_procstream_popen_write_error(monkeypatch)
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    drive = run.make_drive(
        _store(tmp_path),
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        agenttalk_preflight=lambda: None,
        clock=lambda: 0.0,
        render=False,
    )

    out = drive(rec)

    assert out.ok is False
    assert out.failure_class == loop.CLASS_INFRA
    assert "spawn/exec error" in out.summary
    proc = created[0]
    assert proc.stdin.closed is True
    assert proc.stdout.closed is True
    assert proc.terminated is True


def test_launch_preflight_resolves_known_npm_codex_shim_and_blocks_unknown(
    tmp_path,
) -> None:
    shim = tmp_path / "node_modules" / ".bin" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\r\n", encoding="utf-8")
    native = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    seen: dict[str, object] = {}

    def ok_runner(argv, cwd, env, timeout):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["env"] = env
        seen["timeout"] = timeout
        return 0, "agenttalk 0.test"

    env = {"AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path)}
    res = run.preflight_launch_runtime(
        [str(shim), "exec"], "codex", tmp_path, env, runner=ok_runner)
    assert res.blocked is None
    assert res.argv == [str(native.resolve()), "exec"]
    assert seen["argv"] == [sys.executable, "-m", "agenttalk", "--version"]

    unknown = tmp_path / "other" / "codex.cmd"
    unknown.parent.mkdir()
    unknown.write_text("@echo off\r\n", encoding="utf-8")
    blocked = run.preflight_launch_runtime(
        [str(unknown)], "codex", tmp_path, env, runner=ok_runner)
    assert blocked.blocked is not None
    assert ".cmd/.bat/.ps1 shim" in blocked.blocked


def test_launch_preflight_rejects_planted_nested_vendor_codex(tmp_path) -> None:
    shim = tmp_path / "node_modules" / ".bin" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\r\n", encoding="utf-8")
    planted = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "OTHER"
        / "bin"
        / "codex.exe"
    )
    planted.parent.mkdir(parents=True)
    planted.write_text("", encoding="utf-8")

    def ok_runner(_argv, _cwd, _env, _timeout):
        return 0, "agenttalk 0.test"

    env = {"AGENTTALK_PY": sys.executable, "AGENTTALK_ROOT": str(tmp_path)}
    res = run.preflight_launch_runtime(
        [str(shim)], "codex", tmp_path, env, runner=ok_runner)
    assert res.blocked is not None
    assert ".cmd/.bat/.ps1 shim" in res.blocked


def test_launch_preflight_uses_agenttalk_codex_override(tmp_path) -> None:
    native = tmp_path / "codex.exe"
    native.write_text("", encoding="utf-8")
    env = {
        "AGENTTALK_PY": sys.executable,
        "AGENTTALK_ROOT": str(tmp_path),
        "AGENTTALK_CODEX": str(native),
    }

    def ok_runner(_argv, _cwd, _env, _timeout):
        return 0, "agenttalk 0.test"

    res = run.preflight_launch_runtime(
        ["codex", "exec"], "codex", tmp_path, env, runner=ok_runner)
    assert res.blocked is None
    assert res.argv == [str(native.resolve()), "exec"]


def test_agenttalk_runtime_preflight_ignores_ambiguous_probe_failures(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def os_error_runner(_argv, _cwd, _env, _timeout):
        raise OSError("temporary process launch failure")

    assert run.preflight_agenttalk_runtime(
        workspace_root=workspace,
        runner=os_error_runner,
    ) is None

    def transient_rc_runner(_argv, _cwd, _env, _timeout):
        return 1, "temporary import timeout"

    assert run.preflight_agenttalk_runtime(
        workspace_root=workspace,
        runner=transient_rc_runner,
    ) is None

    def malformed_runner(_argv, _cwd, _env, _timeout):
        return 0, "not json"

    assert run.preflight_agenttalk_runtime(
        workspace_root=workspace,
        runner=malformed_runner,
    ) is None


def test_agenttalk_runtime_preflight_parks_denied_source_probe(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    denied = tmp_path / "sibling-agenttalk" / "src" / "agenttalk" / "__init__.py"
    text = (
        "Traceback: import agenttalk failed: Access is denied: "
        f"{denied}"
    )

    def denied_runner(_argv, _cwd, _env, _timeout):
        return 1, text

    blocked = run.preflight_agenttalk_runtime(
        workspace_root=workspace,
        runner=denied_runner,
    )
    assert blocked is not None
    assert "Access is denied" in blocked
    assert "install agenttalk non-editable" in blocked


def test_agenttalk_runtime_preflight_parks_missing_module_probe(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def missing_runner(_argv, _cwd, _env, _timeout):
        return 1, "ModuleNotFoundError: No module named 'agenttalk'"

    blocked = run.preflight_agenttalk_runtime(
        workspace_root=workspace,
        runner=missing_runner,
    )
    assert blocked is not None
    assert "No module named" in blocked
    assert "install agenttalk non-editable" in blocked


def test_classify_bus_execution_contract_matrix() -> None:
    cases = [
        ("python -m agenttalk reply --to-request rq-1", "Access is denied", None,
         run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk reply --to-request rq-1",
         "ModuleNotFoundError: No module named 'agenttalk'", None,
         run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk reply --to-request rq-1",
         "python is not recognized as an internal or external command", None,
         run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk reply --to-request rq-1",
         "agenttalk runtime preflight failed: resolved_path=D:\\sibling\\agenttalk\\src\\"
         "agenttalk\\__init__.py is outside the workspace", None,
         run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk reply --to-request rq-1",
         "Traceback: SyntaxError in D:\\sibling\\agenttalk\\src\\agenttalk\\__init__.py",
         None, run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk reply --to-request rq-1",
         "CreateProcess error 2: python.exe could not be started", None,
         run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk reply --to-request rq-1", "novel durable write failure", 7,
         run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -magenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("py -3 -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("py -3.14 -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("py -V:3.14 -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -X utf8 -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -Xutf8 -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -X=utf8 -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -W ignore -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -Wignore -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -bb -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -OO -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -vvv -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -Es -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -P -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -EP -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -I -P -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("py -3.14 -P -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -m agenttalk --root D:\\repo reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -m agenttalk --root=D:\\repo send --to lead",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('python -m agenttalk --root="D:\\Repo Root" reply --to-request rq-1',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -m agenttalk --root 'D:\\Repo Root' reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        (["python", "-m", "agenttalk", "--root", "D:\\Repo Root", "reply",
          "--to-request", "rq-1"],
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("agenttalk.cmd --root D:\\repo reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ("cmd.exe /c python -m agenttalk --root D:\\repo escalate --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('cmd.exe /c "python -m agenttalk --root D:\\repo reply --to-request rq-1"',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('cmd.exe /c "python -m agenttalk --root=\'D:\\Repo Root\' reply --to-request rq-1"',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('cmd.exe /c "python -m agenttalk --root \\"D:\\Repo Root\\" reply --to-request rq-1"',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('cmd.exe /c "python -m agenttalk --root ""D:\\Repo Root"" reply --to-request rq-1"',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        (["cmd.exe", "/c", "python", "-m", "agenttalk", "--root", "D:\\Repo Root",
          "reply", "--to-request", "rq-1"],
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('pwsh.exe -Command "python -m agenttalk --root=\'D:\\Repo Root\' reply --to-request rq-1"',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('bash -c "python -m agenttalk --root=\'/tmp/Repo Root\' reply --to-request rq-1"',
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        (["powershell", "-Command", "& $env:AGENTTALK_PY -m agenttalk reply --to-request rq-1"],
         "novel durable write failure", 7, run.BUS_KIND_UNKNOWN_FAILURE),
        (["powershell", "-Command",
          "& $env:AGENTTALK_PY -m agenttalk --root D:\\repo reply --to-request rq-1"],
         "novel durable write failure", 17, run.BUS_KIND_UNKNOWN_FAILURE),
        ('& "$env:AGENTTALK_PY" -m "agenttalk" reply --to-request rq-1',
         "novel durable write failure", 7, run.BUS_KIND_UNKNOWN_FAILURE),
        ("python -m agenttalk reply --to-request rq-1", "novel durable write failure", None,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m agenttalk reply --bad-flag",
         "usage: agenttalk reply [-h]\nerror: unrecognized arguments: --bad-flag", 2,
         run.BUS_KIND_SEMANTIC_FAILURE),
        ("python -m agenttalk send --to ghost",
         "agenttalk validation failed: retired recipient", 2,
         run.BUS_KIND_SEMANTIC_FAILURE),
        ("python -m agenttalk check --for beta --to-request rq-1", "superseded", 3,
         run.BUS_KIND_SEMANTIC_FAILURE),
        ("python -m agenttalk composing --to-request rq-1", "novel composing failure", 9,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m agenttalk composing --to-request rq-1",
         "ModuleNotFoundError: No module named agenttalk", 1,
         run.BUS_KIND_CONFIG_BLOCKED),
        ("python -m agenttalk sync --for beta", "novel read failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m agenttalk sync --for beta -m 'please reply/send/escalate'",
         "novel read failure", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m agenttalk sync --for beta -m 'please reply/send/escalate'",
         "Access is denied", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("rg agenttalk reply src", "novel grep failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("grep agenttalk reply src", "novel grep failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m pytest -k agenttalk reply", "novel pytest failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python script.py -m agenttalk reply", "novel script failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ('python -c "print(1)" -m agenttalk reply', "novel command failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -c -m agenttalk reply", "novel command failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -cprint(1) -m agenttalk reply", "novel command failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -- -m agenttalk reply", "novel script failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python - -m agenttalk reply", "novel stdin failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python --help -m agenttalk reply", "novel help failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python --version -m agenttalk reply", "novel version failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -h -m agenttalk reply", "novel help failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -V -m agenttalk reply", "novel version failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -VV -m agenttalk reply", "novel version failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -V:3.14 -m agenttalk reply", "novel selector failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -Z -m agenttalk reply", "novel option failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python --frobnicate -m agenttalk reply", "novel option failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m other -m agenttalk --root D:\\repo reply", "novel module failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("echo agenttalk --root D:\\repo reply", "novel echo failure", 1,
         run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m other -m 'agenttalk --root D:\\repo reply --to-request rq-1'",
         "Access is denied", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m agenttalk --unknown D:\\repo reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -m agenttalk --root \"D:\\Repo Root reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -W -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python -X -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
        ("python --check-hash-based-pycs -m agenttalk reply --to-request rq-1",
         "novel durable write failure", 17, run.BUS_KIND_OK_OR_NO_SIGNAL),
    ]
    for command, output, exit_status, expected in cases:
        got = run.classify_bus_execution(command, output, exit_status)
        assert got["kind"] == expected, (command, output, got)


# THE classification CONTRACT (lead 4th-verify "structural cap"): a table of realistic
# provider error strings -> their REQUIRED dead-letter class. This is the authoritative
# anti-drift gate for the marker allowlists - the recurring marker-breadth bugs (bare
# "too long" / "violates" / "maximum number of tokens") were each an overbroad entry this
# matrix would have caught in ONE pass. Add a row when a new provider string is observed.
_CLASSIFICATION_MATRIX = [
    # --- INFRA: global outage / rate-limit / auth / edge - NEVER auto-DL ---
    ("rate limit exceeded", loop.CLASS_INFRA),
    ("429 too many requests", loop.CLASS_INFRA),
    ("HTTP 529 overloaded", loop.CLASS_INFRA),
    ("the model is overloaded", loop.CLASS_INFRA),
    ("request timed out", loop.CLASS_INFRA),
    ("502 bad gateway", loop.CLASS_INFRA),
    ("503 service unavailable", loop.CLASS_INFRA),
    ("504 gateway timeout", loop.CLASS_INFRA),
    ("connection reset by peer", loop.CLASS_INFRA),
    ("401 unauthorized", loop.CLASS_INFRA),
    ("403 forbidden", loop.CLASS_INFRA),
    ("invalid api key", loop.CLASS_INFRA),
    ("request blocked by upstream proxy", loop.CLASS_INFRA),
    ("blocked by waf", loop.CLASS_INFRA),
    # token/quota RATE windows (P1 #2 + codex expansion) - a TPM/daily/quota limit, NOT this
    # message being too big.
    ("maximum number of tokens per minute exceeded for this organization", loop.CLASS_INFRA),
    ("reached your maximum number of tokens per day", loop.CLASS_INFRA),
    ("maximum tokens per minute reached (TPM)", loop.CLASS_INFRA),
    ("daily token limit reached", loop.CLASS_INFRA),
    ("daily token limit exceeded", loop.CLASS_INFRA),       # codex probe: rate-window synonym
    # --- POISON: positive message-content attribution - DL at the low cap ---
    ("context length exceeded", loop.CLASS_POISON),
    ("context window exceeded", loop.CLASS_POISON),
    ("prompt is too long", loop.CLASS_POISON),
    ("input too large", loop.CLASS_POISON),
    ("content policy violation", loop.CLASS_POISON),
    ("flagged by content filter", loop.CLASS_POISON),
    ("blocked by safety", loop.CLASS_POISON),
    ("blocked by content policy", loop.CLASS_POISON),
    ("violates content policy", loop.CLASS_POISON),
    # 413 is QUALIFIED poison (HTTP/status/request-entity/payload context), never a bare "413"
    # substring (codex) - request-size-attributable, retry won't help.
    ("413 request entity too large", loop.CLASS_POISON),
    ("HTTP 413", loop.CLASS_POISON),
    ("status 413", loop.CLASS_POISON),
    ("payload too large", loop.CLASS_POISON),
    # --- PRECEDENCE: a terminal matching BOTH families is INFRA (fail-closed, human decides) ---
    ("context length exceeded (429 rate limit)", loop.CLASS_INFRA),
    ("payload too large behind upstream proxy", loop.CLASS_INFRA),
    # --- CONFIG-BLOCKED: deterministic bus exec/permission denial, not transient infra ---
    ("Bash(agenttalk reply --from beta --to-request rq-1 -m ok) failed: "
     "Access is denied (os error 5)", loop.CLASS_CONFIG_BLOCKED),
    ("python -m agenttalk send --from beta --to lead --kind message failed: "
     "Permission denied", loop.CLASS_CONFIG_BLOCKED),
    ("CreateProcess error 5 while running agenttalk composing --to-request rq-1",
     loop.CLASS_CONFIG_BLOCKED),
    ("agenttalk runtime preflight failed: resolved_path=D:\\Projects\\claude\\agenttalk\\src\\"
     "agenttalk\\__init__.py is outside the workspace", loop.CLASS_CONFIG_BLOCKED),
    ("import agenttalk failed: Access is denied: D:\\Projects\\claude\\agenttalk\\src\\"
     "agenttalk\\__init__.py", loop.CLASS_CONFIG_BLOCKED),
    ("ModuleNotFoundError: No module named 'agenttalk'", loop.CLASS_CONFIG_BLOCKED),
    ("python -m agenttalk reply failed: error 503 service unavailable", loop.CLASS_INFRA),
    # --- AMBIGUOUS: not-known-infra, not positive-content - high ceiling, never DL@3 ---
    ("invalid request: model not found", loop.CLASS_AMBIGUOUS),
    ("invalid request: unsupported parameter 'temperature'", loop.CLASS_AMBIGUOUS),
    ("400 bad request: malformed", loop.CLASS_AMBIGUOUS),
    ("unprocessable entity", loop.CLASS_AMBIGUOUS),
    ("server took too long to respond", loop.CLASS_AMBIGUOUS),     # bare "too long"
    ("request violates server constraints", loop.CLASS_AMBIGUOUS),  # unqualified "violates"
    ("flagged by the fraud detection system", loop.CLASS_AMBIGUOUS),  # "flagged by" unqualified
    ("exceeds the maximum retries", loop.CLASS_AMBIGUOUS),          # no content-size object
    # NEGATIVE: "413" must be a STANDALONE token in an HTTP/status/entity/payload context to be
    # poison - never a substring of a larger number (8413/4130), even when a qualifier word is
    # ALSO present elsewhere in the text (reviewer-1 blocker: \b413\b token boundary, not substring).
    ("the response body was 8413 bytes", loop.CLASS_AMBIGUOUS),
    ("HTTP response body was 8413 bytes", loop.CLASS_AMBIGUOUS),     # qualifier present, 8413 != 413
    ("status text included 8413 bytes", loop.CLASS_AMBIGUOUS),      # qualifier present, 8413 != 413
    ("request id req_413 failed", loop.CLASS_AMBIGUOUS),
    ("opaque error code 4130", loop.CLASS_AMBIGUOUS),               # 4130 != 413 token
    ("error 413", loop.CLASS_AMBIGUOUS),                            # 413 token but NO qualifier
    ("Access is denied while reading a user-provided document; no bus command was run",
     loop.CLASS_AMBIGUOUS),
    ("agenttalk roster failed with Access is denied; this is not a required bus write",
     loop.CLASS_AMBIGUOUS),
    ("tool execution failed: bad arg", loop.CLASS_AMBIGUOUS),
    ("some totally unknown provider error 9000", loop.CLASS_AMBIGUOUS),
]


def test_classification_contract_matrix(tmp_path) -> None:
    # the authoritative classification contract: each provider string maps to its REQUIRED
    # dead-letter class via the real make_drive _classify (terminal turn.failed). The gate
    # against future marker drift - INFRA-first precedence + the narrow positive-poison allowlist.
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}

    def _class_of(text):
        drive = run.make_drive(_store(tmp_path), "beta", "codex",
                               session.SessionState(cli="codex"), ["codex"],
                               spawn=lambda a, i, m=text: _failed_turn_lines(m),
                               clock=lambda: 0.0, render=False)
        return drive(rec)

    for text, expected in _CLASSIFICATION_MATRIX:
        o = _class_of(text)
        assert o.ok is False, text
        assert o.failure_class == expected, f"{text!r}: got {o.failure_class}, want {expected}"


def test_make_drive_tool_bus_denial_is_config_blocked_even_if_turn_completed(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "agenttalk reply --from beta --to-request rq-1 -m ok",
                                   "aggregated_output": "Access is denied"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_CONFIG_BLOCKED
    assert "$env:AGENTTALK_PY -m agenttalk" in out.summary


def test_make_drive_tool_bus_missing_module_is_config_blocked_even_if_turn_completed(
    tmp_path,
) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -m agenttalk reply --to-request rq-1",
                                   "aggregated_output": (
                                       "ModuleNotFoundError: No module named 'agenttalk'"
                                   )}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_CONFIG_BLOCKED
    assert "install agenttalk non-editable" in out.summary


def test_make_drive_tool_bus_usage_error_is_not_config_blocked(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -m agenttalk reply --bad-flag",
                                   "aggregated_output": (
                                       "usage: agenttalk reply [-h]; "
                                       "error: unrecognized arguments: --bad-flag"
                                   )}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is True


def test_make_drive_required_bus_unknown_without_exit_signal_is_success(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -m agenttalk reply --to-request rq-1",
                                   "aggregated_output": "novel durable write failure"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    assert drive(rec).ok is True


def test_make_drive_required_bus_semantic_failure_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -m agenttalk reply --bad-flag",
                                   "aggregated_output": (
                                       "usage: agenttalk reply [-h]; "
                                       "error: unrecognized arguments: --bad-flag"
                                   ),
                                   "exit_code": 2, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_semantic_failure" in out.summary


def test_make_drive_required_bus_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -m agenttalk reply --to-request rq-1",
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 19, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_required_bus_with_global_root_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": (
                                       "python -m agenttalk --root D:\\repo "
                                       "reply --to-request rq-1"
                                   ),
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 17, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_required_bus_with_spaced_global_root_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": ["python", "-m", "agenttalk", "--root",
                                               "D:\\Repo Root", "reply",
                                               "--to-request", "rq-1"],
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 17, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_required_bus_with_python_option_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": (
                                       "python -X utf8 -m agenttalk "
                                       "reply --to-request rq-1"
                                   ),
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 17, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_required_bus_with_attached_python_option_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": (
                                       "python -Xutf8 -m agenttalk "
                                       "reply --to-request rq-1"
                                   ),
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 17, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_required_bus_with_safe_path_flag_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": (
                                       "python -P -m agenttalk "
                                       "reply --to-request rq-1"
                                   ),
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 17, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_required_bus_with_escaped_launcher_root_unknown_nonzero_is_ambiguous(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": (
                                       'cmd.exe /c "python -m agenttalk --root '
                                       '\\"D:\\Repo Root\\" reply --to-request rq-1"'
                                   ),
                                   "aggregated_output": "novel durable write failure",
                                   "exit_code": 17, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_AMBIGUOUS
    assert "bus_write_failed_unknown" in out.summary


def test_make_drive_non_bus_command_mentioning_agenttalk_reply_stays_success(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "rg agenttalk reply src",
                                   "aggregated_output": "novel grep failure",
                                   "exit_code": 1, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    assert drive(rec).ok is True


def test_make_drive_python_script_argument_mentioning_agenttalk_reply_stays_success(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python script.py -m agenttalk reply",
                                   "aggregated_output": "novel script failure",
                                   "exit_code": 1, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    assert drive(rec).ok is True


def test_make_drive_python_attached_command_mentioning_agenttalk_reply_stays_success(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -cprint(1) -m agenttalk reply",
                                   "aggregated_output": "novel command failure",
                                   "exit_code": 1, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    assert drive(rec).ok is True


def test_make_drive_python_invalid_selector_mentioning_agenttalk_reply_stays_success(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "turn.started"}),
              json.dumps({"type": "item.completed",
                          "item": {"type": "command_execution",
                                   "command": "python -V:3.14 -m agenttalk reply",
                                   "aggregated_output": "novel selector failure",
                                   "exit_code": 2, "status": "completed"}}),
              json.dumps({"type": "turn.completed"})]
    drive = run.make_drive(_store(tmp_path), "beta", "codex",
                           session.SessionState(cli="codex"), ["codex"],
                           spawn=lambda a, i: stream, clock=lambda: 0.0, render=False)
    assert drive(rec).ok is True


def test_make_drive_preflight_bad_runtime_is_config_blocked_without_spawn(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}

    def spawn(_argv, _stdin):
        raise AssertionError("codex child should not spawn after a bad bus preflight")

    drive = run.make_drive(
        _store(tmp_path),
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=spawn,
        agenttalk_preflight=lambda: (
            "command=$env:AGENTTALK_PY -c import agenttalk; resolved_path=D:\\Projects\\claude\\"
            "agenttalk\\src\\agenttalk\\__init__.py; error=agenttalk runtime resolved "
            "to an out-of-workspace source checkout; remediation=install agenttalk "
            "non-editable into the runtime Python used by AGENTTALK_PY"
        ),
        clock=lambda: 0.0,
        render=False,
    )
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_CONFIG_BLOCKED
    assert "out-of-workspace source checkout" in out.summary


def test_make_drive_spawn_missing_executable_is_config_blocked(tmp_path) -> None:
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}

    def missing_spawn(_argv, _stdin):
        raise FileNotFoundError(2, "No such file or directory")

    drive = run.make_drive(
        _store(tmp_path),
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["missing-codex.exe"],
        spawn=missing_spawn,
        clock=lambda: 0.0,
        render=False,
    )
    out = drive(rec)
    assert out.ok is False
    assert out.failure_class == loop.CLASS_CONFIG_BLOCKED
    assert "subtype=launch_cli" in out.summary


def test_cmd_wrap_launch_preflight_blocks_before_consuming_message(tmp_path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("alpha")
    msg = s.send(sender="alpha", recipient="beta", body="do work")
    missing = tmp_path / "missing-codex.exe"

    rc = cli.main([
        "--root", str(tmp_path),
        "wrap", "--for", "beta", "--cli", "codex", "--loop",
        "--", str(missing),
    ])

    assert rc == 1
    assert s.cursor("beta") == ""
    assert s.messages_for("beta")[0].id == msg.id
    assert s.attempt_record("beta", msg.id) is None
    assert s.dead_lettered_count("beta") == 0
    health = s.read_health("beta", ttl_seconds=999999)
    assert health["reason_code"] == "config_blocked"
    hold = s.read_config_blocked_hold("beta")
    assert hold is not None
    assert hold["agent"] == "beta"
    assert "missing-codex.exe" in hold["summary"]
    notice = s.messages_for("alpha")[-1]
    assert notice.subject == "wrapper launch config-blocked"
    assert "before any message was consumed" in notice.body
    assert "missing-codex.exe" in notice.body


def test_cmd_wrap_preflight_success_clears_config_blocked_hold(
    tmp_path,
    monkeypatch,
) -> None:
    s = _store(tmp_path)
    s.write_config_blocked_hold("beta", summary="old launch failure")
    native = tmp_path / "codex.exe"
    native.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        run,
        "preflight_launch_runtime",
        lambda argv, _cli, _root, _env: run.LaunchPreflightResult(list(argv)),
    )

    seen: dict[str, object] = {}

    def fake_loop(store, agent, **kwargs):
        seen["store"] = store
        seen["agent"] = agent
        seen["base_argv"] = kwargs["base_argv"]
        return 0

    monkeypatch.setattr(cli, "_wrap_loop_mode", fake_loop)

    rc = cli.main([
        "--root", str(tmp_path),
        "wrap", "--for", "beta", "--cli", "codex", "--loop",
        "--", str(native), "exec",
    ])

    assert rc == 0
    assert seen["agent"] == "beta"
    assert seen["base_argv"] == [str(native), "exec"]
    assert s.read_config_blocked_hold("beta") is None


def test_make_drive_retryable_after_start_is_infra(tmp_path) -> None:
    # lead 6th-verify P2: a RECOGNIZED retryable transport error that arrives AFTER turn-start
    # (then drops with no terminal) must classify INFRA, NOT be shadowed as the started/partial-
    # stream AMBIGUOUS - else a homogeneous transport OUTAGE on one head accumulates ambiguous
    # and false-dead-letters a HEALTHY message at the K_escalate ceiling.
    rec = {"from": "a", "kind": "message", "body": "x",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    # codex: thread.started + turn.started + a top-level {type:error} (retryable), no completion
    codex_stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps({"type": "error", "message": "Reconnecting... 2/5"})]
    d_codex = run.make_drive(_store(tmp_path), "beta", "codex",
                             session.SessionState(cli="codex"), ["codex"],
                             spawn=lambda a, i: codex_stream, clock=lambda: 0.0, render=False)
    o = d_codex(rec)
    assert o.ok is False and o.failure_class == loop.CLASS_INFRA, o
    # claude: message_start + a throttled rate_limit_event (retryable), no message_stop
    claude_stream = [json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
                     json.dumps({"type": "rate_limit_event",
                                 "rate_limit_info": {"status": "throttled"}})]
    d_claude = run.make_drive(_store(tmp_path), "beta", "claude",
                              session.SessionState(cli="claude", claude_session_id="sess-1"),
                              ["claude"], spawn=lambda a, i: claude_stream,
                              clock=lambda: 0.0, render=False)
    o2 = d_claude(rec)
    assert o2.ok is False and o2.failure_class == loop.CLASS_INFRA, o2


def _claude_ok_lines():
    return [json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})]


def _claude_fail_lines(msg="session is full"):
    return [json.dumps({"type": "result", "is_error": True, "result": msg})]


def _claude_rec():
    return {"from": "a", "kind": "message", "body": "x",
            "correlation_id": None, "request_id": None, "broadcast_id": None}


def test_make_drive_claude_resume_gives_up_after_two_then_fresh_session(tmp_path) -> None:
    # Slice 1 B4: failed claude --resume is bounded by the session-attributable K=2
    # ledger, then the following attempt uses a fresh --session-id.
    calls = []

    def spawn(argv, stdin):
        calls.append(list(argv))
        if "--resume" in argv:
            return _claude_fail_lines("prompt is too long")     # stale, full session
        return _claude_ok_lines()                                # fresh session succeeds

    state = session.SessionState(cli="claude", claude_session_id="sess-1",
                                 turns=1, resume_available=True)
    drive = run.make_drive(_store(tmp_path), "beta", "claude", state, ["claude"],
                           spawn=spawn, clock=lambda: 0.0, render=False)
    out = drive(_claude_rec())
    assert out.ok is False and out.failure_class == loop.CLASS_AMBIGUOUS
    assert len(calls) == 1 and "--resume" in calls[0]
    out2 = drive(_claude_rec())
    assert out2.ok is False and "resume_unavailable" in out2.summary
    assert state.claude_session_id != "sess-1"                  # fresh id minted only at K
    out3 = drive(_claude_rec())
    assert out3.ok is True
    assert "--session-id" in calls[-1]
    assert state.resume_available is True
    assert state.continuity_lost_reason
    assert state.fresh_session_success_reason == "fresh_session_success"


def test_make_drive_claude_prompt_too_long_on_resume_is_not_message_poison(tmp_path) -> None:
    # Resume-scoped session pressure never classifies the message as poison on the
    # first failed resume; it must go through the B4 resume ledger.
    calls = []

    def spawn(argv, stdin):
        calls.append(list(argv))
        return _claude_fail_lines("prompt is too long")          # BOTH resume and fresh fail

    state = session.SessionState(cli="claude", claude_session_id="sess-1",
                                 turns=1, resume_available=True)
    drive = run.make_drive(_store(tmp_path), "beta", "claude", state, ["claude"],
                           spawn=spawn, clock=lambda: 0.0, render=False)
    out = drive(_claude_rec())
    assert out.ok is False and out.failure_class == loop.CLASS_AMBIGUOUS
    assert len(calls) == 1 and "--resume" in calls[0]
    assert state.resume_failure_count == 1


def test_make_drive_success_stamps_heartbeat(tmp_path) -> None:
    # the flip side: a CLEAN completed turn leaves a fresh heartbeat.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    drive = run.make_drive(s, "beta", "codex", st, ["codex"], clock=lambda: 0.0,
                           render=False, spawn=lambda a, i: _codex_turn_lines())
    assert drive({"from": "a", "kind": "message", "body": "hi",
                  "correlation_id": None, "request_id": None, "broadcast_id": None}).ok is True
    assert s.read_heartbeat("beta") is not None


def test_make_drive_stamps_liveness_during_successful_turn(tmp_path) -> None:
    # reviewer-1 gate r2: in-turn liveness must stay fresh for a long SUCCESSFUL
    # turn (stamp on streaming progress BEFORE completion), not only at the end.
    # The generator captures the heartbeat MID-stream (after turn.started, before
    # turn.completed) to prove it.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    mid: list[object] = []

    def gen_spawn(argv, stdin):
        yield json.dumps({"type": "turn.started"})
        mid.append(s.read_heartbeat("beta"))   # stamped DURING the turn, pre-completion
        yield json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x"}})
        yield json.dumps({"type": "turn.completed"})

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=gen_spawn,
                           clock=lambda: 0.0, render=False)
    assert drive({"from": "a", "kind": "message", "body": "hi",
                  "correlation_id": None, "request_id": None, "broadcast_id": None}).ok is True
    assert mid and mid[0] is not None          # liveness was fresh BEFORE completion
    assert s.read_heartbeat("beta") is not None


def test_make_drive_failed_then_successful_retry_keeps_liveness(tmp_path) -> None:
    # reviewer-1 gate r3: a failed turn clears the heartbeat AND resets the engine
    # throttle, so a SUCCESSFUL retry within min_interval still stamps mid-turn and
    # ends fresh - the throttle does not suppress the retry into leaving no heartbeat.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    mid: list[object] = []
    calls = {"n": 0}

    def spawn(argv, stdin):
        calls["n"] += 1
        if calls["n"] == 1:
            return [json.dumps({"type": "turn.started"})]   # partial -> stamps then FAILS

        def gen():
            yield json.dumps({"type": "turn.started"})
            mid.append(s.read_heartbeat("beta"))            # mid-turn of the RETRY
            yield json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x"}})
            yield json.dumps({"type": "turn.completed"})
        return gen()

    rec = {"from": "a", "kind": "message", "body": "hi",
           "correlation_id": None, "request_id": None, "broadcast_id": None}
    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=spawn,
                           clock=lambda: 0.0, min_interval=5.0, render=False)
    assert drive(rec).ok is False                  # first turn fails (partial)
    assert s.read_heartbeat("beta") is None     # failed turn cleared it
    assert drive(rec).ok is True                   # retry succeeds within min_interval
    assert mid and mid[0] is not None           # mid-turn liveness of the retry (throttle reset)
    assert s.read_heartbeat("beta") is not None  # final heartbeat present


def test_loop_failed_turn_backs_off_not_hot_spin(tmp_path) -> None:
    # codex r2 MAJOR 2: a persistent drive failure must NOT hot-loop spawning; the
    # loop backs off (sleeps) between failed attempts and never stamps heartbeat.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="poison")
    sleeps: list[float] = []
    # dead-letter DISABLED (k_poison=0): this test is about the backoff-not-hot-spin
    # behavior below/without the dead-letter cap (dead-letter dispatch is tested
    # separately in test_dead_letter.py).
    loop.run_loop(s, "beta", lambda rec: False, clock=lambda: 0.0,
                  sleep=lambda d: sleeps.append(d), max_polls=4, k_poison=0, k_escalate=0)
    assert len(sleeps) >= 3              # backed off on each failed retry (not a hot spin)
    assert s.cursor("beta") == ""        # never committed
    assert s.read_heartbeat("beta") is None  # a failed turn does NOT stamp heartbeat


def test_loop_config_blocked_escalates_once_parks_head_and_keeps_liveness(tmp_path) -> None:
    s = _store(tmp_path)
    msg = s.send(sender="alpha", recipient="beta", body="reply please")
    calls: list[str] = []
    escalations: list[dict] = []
    stamps: list[str] = []
    sleeps: list[float] = []

    def drive(rec):
        calls.append(rec["id"])
        return loop.DriveOutcome(
            ok=False,
            failure_class=loop.CLASS_CONFIG_BLOCKED,
            summary=("command=agenttalk reply --from beta --to-request rq-1; "
                     "error=Access is denied; remediation=use $env:AGENTTALK_PY -m agenttalk"),
        )

    def heartbeat() -> None:
        stamps.append("hb")
        s.write_heartbeat("beta")

    loop.run_loop(
        s,
        "beta",
        drive,
        clock=lambda: 0.0,
        sleep=lambda d: sleeps.append(d),
        max_polls=4,
        k_poison=1,
        k_escalate=1,
        on_escalate=lambda info: escalations.append(info) or True,
        heartbeat=heartbeat,
    )

    assert calls == [msg.id]                 # parked: no retry storm to K=20
    assert len(escalations) == 1
    assert escalations[0]["failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert "$env:AGENTTALK_PY -m agenttalk" in escalations[0]["summary"]
    assert s.cursor("beta") == ""            # cursor stays on the head
    assert s.dead_lettered_count("beta") == 0
    rec = s.attempt_record("beta", msg.id)
    assert rec is not None
    assert rec["attempts_started"] == 1
    assert rec["last_failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert rec["in_progress"] is False
    assert rec["escalated"] is True and rec["escalation_routed"] is True
    assert stamps and s.read_heartbeat("beta") is not None
    assert sleeps                              # parked branch backs off without retrying


def test_loop_resume_config_blocked_does_not_self_heal_and_commit(tmp_path) -> None:
    s = _store(tmp_path)
    head = s.send(sender="alpha", recipient="beta", body="needs bus reply")
    state = session.SessionState(cli="codex", codex_thread_id="t-old")
    calls: list[list[str]] = []

    def spawn(argv, _stdin):
        calls.append(argv)
        if "resume" in argv:
            return [
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.completed",
                            "item": {"type": "command_execution",
                                     "command": "agenttalk reply --from beta "
                                                "--to-request rq-1 -m ok",
                                     "aggregated_output": "Access is denied"}}),
                json.dumps({"type": "turn.completed"}),
            ]
        return [
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed"}),
        ]

    drive = run.make_drive(
        s,
        "beta",
        "codex",
        state,
        ["codex"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
    )
    escalations: list[dict] = []
    stamps: list[str] = []
    sleeps: list[float] = []

    turns = loop.run_loop(
        s,
        "beta",
        drive,
        clock=lambda: 100.0,
        sleep=lambda d: sleeps.append(d),
        heartbeat=lambda: (stamps.append("hb"), s.write_heartbeat("beta")),
        on_escalate=lambda info: escalations.append(info) or True,
        k_poison=1,
        k_escalate=1,
        max_polls=3,
    )

    assert turns == 0
    assert len(calls) == 1
    assert "resume" in calls[0]
    assert s.cursor("beta") == ""
    assert s.messages_for("beta")[0].id == head.id
    assert s.dead_lettered_count("beta") == 0
    rec = s.attempt_record("beta", head.id)
    assert rec is not None
    assert rec["attempts_started"] == 1
    assert rec["last_failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert escalations and escalations[0]["failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert stamps and s.read_heartbeat("beta") is not None
    assert sleeps


def test_loop_runtime_denied_sibling_agenttalk_import_parks_without_commit(tmp_path) -> None:
    s = _store(tmp_path)
    head = s.send(sender="alpha", recipient="beta", body="needs bus reply")
    sibling = tmp_path.parent / "sibling-agenttalk" / "src" / "agenttalk" / "__init__.py"
    error = (
        "Traceback (most recent call last): import agenttalk failed: "
        f"PermissionError: [Errno 13] Access is denied: '{sibling}'"
    )
    calls: list[list[str]] = []

    def spawn(argv, _stdin):
        calls.append(argv)
        return _failed_turn_lines(error)

    drive = run.make_drive(
        s,
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
    )
    escalations: list[dict] = []
    stamps: list[str] = []
    sleeps: list[float] = []

    turns = loop.run_loop(
        s,
        "beta",
        drive,
        clock=lambda: 100.0,
        sleep=lambda d: sleeps.append(d),
        heartbeat=lambda: (stamps.append("hb"), s.write_heartbeat("beta")),
        on_escalate=lambda info: escalations.append(info) or True,
        k_poison=1,
        k_escalate=1,
        max_polls=3,
    )

    assert turns == 0
    assert len(calls) == 1
    assert s.cursor("beta") == ""
    assert s.messages_for("beta")[0].id == head.id
    assert s.dead_lettered_count("beta") == 0
    rec = s.attempt_record("beta", head.id)
    assert rec is not None
    assert rec["attempts_started"] == 1
    assert rec["last_failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert escalations and escalations[0]["failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert "install agenttalk non-editable" in escalations[0]["summary"]
    assert stamps and s.read_heartbeat("beta") is not None
    assert sleeps


def test_loop_runtime_missing_agenttalk_module_parks_without_commit(tmp_path) -> None:
    s = _store(tmp_path)
    head = s.send(sender="alpha", recipient="beta", body="needs bus reply")
    calls: list[list[str]] = []

    def spawn(argv, _stdin):
        calls.append(argv)
        return [
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution",
                                 "command": "python -m agenttalk reply --to-request rq-1",
                                 "aggregated_output": (
                                     "ModuleNotFoundError: No module named agenttalk"
                                 )}}),
            json.dumps({"type": "turn.completed"}),
        ]

    drive = run.make_drive(
        s,
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
    )
    escalations: list[dict] = []
    stamps: list[str] = []
    sleeps: list[float] = []

    turns = loop.run_loop(
        s,
        "beta",
        drive,
        clock=lambda: 100.0,
        sleep=lambda d: sleeps.append(d),
        heartbeat=lambda: (stamps.append("hb"), s.write_heartbeat("beta")),
        on_escalate=lambda info: escalations.append(info) or True,
        k_poison=1,
        k_escalate=1,
        max_polls=3,
    )

    assert turns == 0
    assert len(calls) == 1
    assert s.cursor("beta") == ""
    assert s.messages_for("beta")[0].id == head.id
    assert s.dead_lettered_count("beta") == 0
    rec = s.attempt_record("beta", head.id)
    assert rec is not None
    assert rec["attempts_started"] == 1
    assert rec["last_failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert escalations and escalations[0]["failure_class"] == loop.CLASS_CONFIG_BLOCKED
    assert "install agenttalk non-editable" in escalations[0]["summary"]
    assert stamps and s.read_heartbeat("beta") is not None
    assert sleeps


def test_loop_required_bus_semantic_failure_does_not_commit(tmp_path) -> None:
    s = _store(tmp_path)
    head = s.send(sender="alpha", recipient="beta", body="needs bus reply")
    calls: list[list[str]] = []

    def spawn(argv, _stdin):
        calls.append(argv)
        return [
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution",
                                 "command": "python -m agenttalk reply --bad-flag",
                                 "aggregated_output": (
                                     "usage: agenttalk reply [-h]; "
                                     "error: unrecognized arguments: --bad-flag"
                                 ),
                                 "exit_code": 2, "status": "completed"}}),
            json.dumps({"type": "turn.completed"}),
        ]

    drive = run.make_drive(
        s,
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
    )
    turns = loop.run_loop(
        s,
        "beta",
        drive,
        clock=lambda: 100.0,
        sleep=lambda _d: None,
        k_poison=0,
        k_escalate=0,
        max_polls=1,
    )

    assert turns == 0
    assert len(calls) == 1
    assert s.cursor("beta") == ""
    assert s.messages_for("beta")[0].id == head.id
    rec = s.attempt_record("beta", head.id)
    assert rec is not None
    assert rec["last_failure_class"] == loop.CLASS_AMBIGUOUS
    assert "semantic" in rec["last_failure_summary"]


def test_loop_required_bus_unknown_nonzero_does_not_commit(tmp_path) -> None:
    s = _store(tmp_path)
    head = s.send(sender="alpha", recipient="beta", body="needs bus reply")
    calls: list[list[str]] = []

    def spawn(argv, _stdin):
        calls.append(argv)
        return [
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution",
                                 "command": "python -m agenttalk reply --to-request rq-1",
                                 "aggregated_output": "novel durable write failure",
                                 "exit_code": 17, "status": "completed"}}),
            json.dumps({"type": "turn.completed"}),
        ]

    drive = run.make_drive(
        s,
        "beta",
        "codex",
        session.SessionState(cli="codex"),
        ["codex"],
        spawn=spawn,
        clock=lambda: 0.0,
        render=False,
    )
    turns = loop.run_loop(
        s,
        "beta",
        drive,
        clock=lambda: 100.0,
        sleep=lambda _d: None,
        k_poison=0,
        k_escalate=0,
        max_polls=1,
    )

    assert turns == 0
    assert len(calls) == 1
    assert s.cursor("beta") == ""
    assert s.messages_for("beta")[0].id == head.id
    rec = s.attempt_record("beta", head.id)
    assert rec is not None
    assert rec["last_failure_class"] == loop.CLASS_AMBIGUOUS
    assert "unknown" in rec["last_failure_summary"]


# --------------------------------------------- C3 (0.40.0): one-shot scoped loop

def test_one_shot_scoped_does_not_starve_or_consume_unrelated(tmp_path) -> None:
    # AUDIT P1-c: an unrelated head-of-inbox message must NOT starve a scoped
    # one-shot, and the one-shot must NOT consume it from the GLOBAL inbox (it stays
    # unread for a later global sync). The one-shot drives ONLY its scoped request.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="unrelated head")   # no request_id
    target = s.send(sender="alpha", recipient="beta", body="the task",
                    meta={"request_id": "rq-1"})
    seen: list[str] = []

    def drive(rec):
        seen.append(rec["body"])
        return True

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_turns=1, only_request_id="rq-1")
    assert turns == 1 and seen == ["the task"]        # drove ONLY the scoped request
    assert s.cursor("beta") == ""                      # unrelated NOT globally consumed
    assert s.thread_seen("beta", "rq-1") == target.id  # scoped seen advanced only
    assert s.read_waiting("beta") is None              # waiting cleared on exit


def test_one_shot_times_out_nonzero_when_request_never_arrives(tmp_path) -> None:
    # AUDIT P1-c: a one-shot whose request never arrives must TERMINATE (bounded),
    # not spin forever. max_wall is the in-process bound; turns==0 -> nonzero exit.
    s = _store(tmp_path)
    t = {"n": 0.0}

    def clock():
        t["n"] += 100.0
        return t["n"]

    turns = loop.run_loop(s, "beta", lambda rec: True, clock=clock,
                          sleep=lambda d: None, only_request_id="rq-missing",
                          max_wall=50.0)
    assert turns == 0                                  # never arrived -> no turn driven
    assert s.read_waiting("beta") is None              # waiting cleared on exit


def test_one_shot_idle_stamps_heartbeat_while_waiting(tmp_path) -> None:
    # AUDIT P1-c: while waiting for its scoped request the one-shot keeps the
    # heartbeat FRESH (the old skip branch never stamped -> it went stale spinning).
    s = _store(tmp_path)
    t = {"n": 0.0}

    def clock():
        t["n"] += 100.0
        return t["n"]

    turns = loop.run_loop(s, "beta", lambda rec: True, clock=clock,
                          sleep=lambda d: None, only_request_id="rq-x",
                          max_polls=3, heartbeat_interval=10.0)
    assert turns == 0
    assert s.read_heartbeat("beta") is not None        # waiting one-shot is not stale
    assert s.read_waiting("beta") is None              # cleared on exit


def test_continuous_loop_clears_waiting_on_stop(tmp_path) -> None:
    # AUDIT residual: the wrapper must clear its .waiting marker on a NORMAL exit
    # (try/finally), not only when killed. A marked authorized release stands it down.
    s = _store(tmp_path)
    s.set_operator_facing("alpha")
    s.send(sender="alpha", recipient="beta", kind="release", body="down",
           meta=_human_meta())
    loop.run_loop(s, "beta", lambda rec: True, clock=lambda: 0.0,
                  sleep=lambda d: None, max_polls=5)
    assert s.read_waiting("beta") is None


def test_capacity_refresh_runs_after_idle_stamp_when_due(tmp_path) -> None:
    s = _store(tmp_path)
    seen_heartbeat: list[bool] = []
    times = iter([0.0, 61.0])

    def refresh() -> None:
        seen_heartbeat.append(s.read_heartbeat("beta") is not None)
        raise RuntimeError("capacity source unavailable")

    turns = loop.run_loop(
        s, "beta", lambda rec: True, clock=lambda: next(times),
        sleep=lambda d: None, max_polls=1, heartbeat_interval=10.0,
        capacity_refresh=refresh, capacity_interval_seconds=60.0)

    assert turns == 0
    assert seen_heartbeat == [True]
    assert s.read_heartbeat("beta") is not None


def test_capacity_refresh_not_called_before_interval_or_in_one_shot(tmp_path) -> None:
    s = _store(tmp_path)
    calls: list[str] = []
    times = iter([0.0, 10.0, 20.0])

    loop.run_loop(
        s, "beta", lambda rec: True, clock=lambda: next(times),
        sleep=lambda d: None, max_polls=2, heartbeat_interval=1.0,
        capacity_refresh=lambda: calls.append("continuous"),
        capacity_interval_seconds=60.0)

    loop.run_loop(
        s, "beta", lambda rec: True, clock=lambda: 1000.0,
        sleep=lambda d: None, max_polls=2, only_request_id="q-missing",
        heartbeat_interval=1.0, capacity_refresh=lambda: calls.append("one-shot"),
        capacity_interval_seconds=0.0)

    assert calls == []


def test_capacity_refresh_runs_after_successful_turn_commit_cleanup_when_due(tmp_path) -> None:
    s = _store(tmp_path)
    msg = s.send(sender="alpha", recipient="beta", body="work")
    seen: list[dict] = []
    times = iter([0.0, 1.0, 61.0])

    def drive(_record: dict) -> bool:
        assert seen == []
        return True

    def refresh() -> None:
        seen.append({
            "cursor": s.cursor("beta"),
            "attempt": s.attempt_record("beta", msg.id),
        })

    turns = loop.run_loop(
        s, "beta", drive, clock=lambda: next(times),
        sleep=lambda d: None, max_turns=1, capacity_refresh=refresh,
        capacity_interval_seconds=60.0)

    assert turns == 1
    assert s.cursor("beta") == msg.id
    assert seen == [{"cursor": msg.id, "attempt": None}]


def test_capacity_refresh_not_called_after_failed_turn(tmp_path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="work")
    calls: list[str] = []

    turns = loop.run_loop(
        s, "beta", lambda rec: False, clock=lambda: 100.0,
        sleep=lambda d: None, max_polls=1, capacity_refresh=lambda: calls.append("refresh"),
        capacity_interval_seconds=0.0)

    assert turns == 0
    assert calls == []
    assert s.cursor("beta") == ""


def test_loop_with_make_drive_end_to_end(tmp_path) -> None:
    # the full Phase-B pipeline, no real CLI: loop -> make_drive -> fake codex stream.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="one")
    m2 = s.send(sender="alpha", recipient="beta", body="two")
    st = session.SessionState(cli="codex")
    prompts: list[str] = []

    def fake_spawn(argv, stdin):
        prompts.append(stdin)
        return _codex_turn_lines()

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_turns=2)
    assert turns == 2 and st.turns == 2
    assert s.cursor("beta") == m2.id                 # both committed
    assert "one" in prompts[0] and "two" in prompts[1]


# --------------------------------------------- session persistence + cli wiring

def test_session_persist_round_trip(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.load_session(s, "beta", "claude")
    assert st.claude_session_id                       # minted on first load
    st.turns = 3
    session.save_session(s, "beta", st)
    st2 = session.load_session(s, "beta", "claude")
    assert st2.claude_session_id == st.claude_session_id and st2.turns == 3
    cst = session.load_session(s, "alpha", "codex")
    cst.codex_thread_id = "t-xyz"
    session.save_session(s, "alpha", cst)
    assert session.load_session(s, "alpha", "codex").codex_thread_id == "t-xyz"


def test_wrap_loop_mode_capacity_refresh_uses_codex_home_and_thread_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path)
    codex_home = tmp_path / "codex-home"
    session.save_session(
        s, "beta", session.SessionState(cli="codex", codex_thread_id="THREAD123"))
    seen: dict[str, object] = {}

    def fake_read_local(source_agent, **kwargs):
        seen["source_agent"] = source_agent
        seen.update(kwargs)
        return capmod.CapacitySnapshot.unknown(source_agent)

    def fake_run_loop(store, agent, drive, **kw):
        kw["capacity_refresh"]()
        return 0

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(capmod, "read_local", fake_read_local)
    monkeypatch.setattr(loop, "run_loop", fake_run_loop)
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: (lambda rec: True))

    rc = cli._wrap_loop_mode(
        s, "beta", cli="codex", base_argv=["codex"], sender="beta",
        min_interval=0.0, render=False)

    assert rc == 0
    assert seen["source_agent"] == "beta"
    assert seen["source"] == "codex"
    assert seen["sessions_dir"] == codex_home / "sessions"
    assert seen["thread_id"] == "THREAD123"
    assert s.read_capacity("beta")["source"] == "unknown"


def test_wrap_loop_mode_codex_missing_home_publishes_unknown_without_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path)

    def forbidden_read_local(*args, **kwargs):
        raise AssertionError("supervised codex must not fall back to operator sessions")

    def fake_run_loop(store, agent, drive, **kw):
        kw["capacity_refresh"]()
        return 0

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(capmod, "read_local", forbidden_read_local)
    monkeypatch.setattr(loop, "run_loop", fake_run_loop)
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: (lambda rec: True))

    rc = cli._wrap_loop_mode(
        s, "beta", cli="codex", base_argv=["codex"], sender="beta",
        min_interval=0.0, render=False)

    snap = s.read_capacity("beta")
    assert rc == 0
    assert snap["source"] == "unknown"
    assert snap["confidence"] == "unknown"
    assert snap["reason"] == "codex_home_missing"


def test_wrap_loop_mode_unknown_cli_returns_2(tmp_path) -> None:
    # exercises cmd_wrap -> _wrap_loop_mode -> make_drive(ValueError) -> rc 2,
    # WITHOUT entering the forever loop (make_drive fails before run_loop).
    _store(tmp_path)                                  # init the roster
    rc = cli.main(["--root", str(tmp_path), "wrap", "--for", "beta",
                   "--cli", "gemini", "--loop", "--", "gemini"])
    assert rc == 2
