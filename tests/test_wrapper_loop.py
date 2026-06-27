"""Phase B of the wrapper integration (design C): the wrapper-owned listen loop,
per-turn prompt assembly, and per-CLI session continuity. Driven entirely with a
fixture Store + injected drive/spawn - NO real CLI.
"""

from __future__ import annotations

import json

import pytest

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
    # Loop-exit is the WRAPPER's job, not the model's.
    assert "release" in low and "end" in low and "wrapper's job" in low
    # Sending IS the model's job (kept).
    assert "you may send" in low


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
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="one")
    attempts = {"n": 0}

    def failing_drive(rec):
        attempts["n"] += 1
        return False                     # turn failed

    loop.run_loop(s, "beta", failing_drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_polls=3)
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


def test_make_drive_resume_failure_retries_fresh(tmp_path) -> None:
    # reviewer-1/codex: a persisted thread_id whose RESUME turn fails must clear the
    # stale id, retry FRESH (exec --json) for the same record, observe the new id,
    # and report success. The next invocation uses exec --json, not exec resume.
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
    ok = drive({"from": "a", "kind": "message", "body": "hi",
                "correlation_id": None, "request_id": None, "broadcast_id": None})
    assert ok is True                                  # fresh retry succeeded
    assert spawned[0] == ["codex", "exec", "resume", "--json", "t-old"]  # resume first
    assert spawned[1] == ["codex", "exec", "--json"]                      # then fresh
    assert st.codex_thread_id == "t-new"               # new id observed + persisted
    assert st.turns == 1


def test_make_drive_resume_and_fresh_both_fail_returns_false(tmp_path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex", codex_thread_id="t-old")

    def fake_spawn(argv, stdin):
        return _failed_turn_lines("broken")            # every attempt fails

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    ok = drive({"from": "a", "kind": "message", "body": "hi",
                "correlation_id": None, "request_id": None, "broadcast_id": None})
    assert ok is False                                 # genuine failure -> no commit
    assert st.resume_available is False                # marked unavailable
    assert st.turns == 0                               # a failed turn does not advance


class _Stream:
    """A spawner result with a controllable child exit code (codex r2)."""

    def __init__(self, lines, returncode=None):
        self._lines = lines
        self.returncode = returncode

    def __iter__(self):
        return iter(self._lines)


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
                  "correlation_id": None, "request_id": None, "broadcast_id": None}) is False
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
                  "correlation_id": None, "request_id": None, "broadcast_id": None}) is False
    assert s.read_heartbeat("beta") is None     # reviewer-1 gate: no heartbeat on failure


def test_make_drive_success_stamps_heartbeat(tmp_path) -> None:
    # the flip side: a CLEAN completed turn leaves a fresh heartbeat.
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    drive = run.make_drive(s, "beta", "codex", st, ["codex"], clock=lambda: 0.0,
                           render=False, spawn=lambda a, i: _codex_turn_lines())
    assert drive({"from": "a", "kind": "message", "body": "hi",
                  "correlation_id": None, "request_id": None, "broadcast_id": None}) is True
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
                  "correlation_id": None, "request_id": None, "broadcast_id": None}) is True
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
    assert drive(rec) is False                  # first turn fails (partial)
    assert s.read_heartbeat("beta") is None     # failed turn cleared it
    assert drive(rec) is True                   # retry succeeds within min_interval
    assert mid and mid[0] is not None           # mid-turn liveness of the retry (throttle reset)
    assert s.read_heartbeat("beta") is not None  # final heartbeat present


def test_loop_failed_turn_backs_off_not_hot_spin(tmp_path) -> None:
    # codex r2 MAJOR 2: a persistent drive failure must NOT hot-loop spawning; the
    # loop backs off (sleeps) between failed attempts and never stamps heartbeat.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="poison")
    sleeps: list[float] = []
    loop.run_loop(s, "beta", lambda rec: False, clock=lambda: 0.0,
                  sleep=lambda d: sleeps.append(d), max_polls=4)
    assert len(sleeps) >= 3              # backed off on each failed retry (not a hot spin)
    assert s.cursor("beta") == ""        # never committed
    assert s.read_heartbeat("beta") is None  # a failed turn does NOT stamp heartbeat


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


def test_wrap_loop_mode_unknown_cli_returns_2(tmp_path) -> None:
    # exercises cmd_wrap -> _wrap_loop_mode -> make_drive(ValueError) -> rc 2,
    # WITHOUT entering the forever loop (make_drive fails before run_loop).
    _store(tmp_path)                                  # init the roster
    rc = cli.main(["--root", str(tmp_path), "wrap", "--for", "beta",
                   "--cli", "gemini", "--loop", "--", "gemini"])
    assert rc == 2
