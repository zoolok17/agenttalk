"""`agenttalk session fresh` (#475): a start-fresh REQUEST the wrapper applies itself between turns.

The wrapper loads its session record once and saves its own copy after turns, so the command
only writes a request marker; the wrapper applies it (new Claude session id / cleared Codex
thread), saves, records the outcome and clears the marker. Fakes and throwaway stores only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import wrapper_runtime as wr
from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api, run, session

OLD_ID = "11111111-1111-4111-8111-111111111111"


def _485_existing(s, cli_name):
    state = (_claude_state() if cli_name == "claude" else session.SessionState(
        cli="codex", codex_thread_id="thread-old", turns=5))
    session.save_session(s, "beta", state)
    _request(s)
    return state


def _485_double_fault(s, state, monkeypatch):
    from agenttalk import _atomic
    real_fsync, real_read = _atomic.os.fsync, Path.read_text
    faults = {"write": False, "read": False}

    def fsync(fd):
        if not faults["write"]:
            faults["write"] = True
            raise OSError("after write and flush")
        return real_fsync(fd)

    def read(path, *a, **kw):
        if path == _session_file(s) and faults["write"] and not faults["read"]:
            faults["read"] = True
            raise PermissionError("readback unavailable once")
        return real_read(path, *a, **kw)

    monkeypatch.setattr(_atomic, "_sandbox_direct_write", True)
    monkeypatch.setattr(_atomic.os, "fsync", fsync)
    monkeypatch.setattr(Path, "read_text", read)
    run._apply_fresh_request(s, "beta", state, _saved(s))
    assert faults == {"write": True, "read": True}


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_485_double_fault_retry_preserves_saved_reset(tmp_path, monkeypatch, cli_name):
    """Loss regression: deliberately does not assert the interim memory policy."""
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    _485_double_fault(s, state, monkeypatch)
    assert s.read_fresh_request("beta") is not None
    saved = session.load_session(s, "beta", cli_name)
    assert saved.fresh_request_id == "fr-aaaaaaaaaaaa"
    run._apply_fresh_request(s, "beta", state, _saved(s))
    assert s.read_fresh_request("beta") is None
    assert session.build_turn(state, "hello").args == session.build_turn(saved, "hello").args
    assert OLD_ID not in session.build_turn(state, "hello").args
    assert "thread-old" not in session.build_turn(state, "hello").args
    session.save_session(s, "beta", state)
    assert session.load_session(s, "beta", cli_name) == saved


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_485_unknown_readback_keeps_memory_fresh(tmp_path, monkeypatch, cli_name):
    """Availability policy is separate from the saved-reset loss regression."""
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    _485_double_fault(s, state, monkeypatch)
    assert s.read_fresh_request("beta") is not None
    args = session.build_turn(state, "hello").args
    assert "resume" not in args and "--resume" not in args
    assert OLD_ID not in args and "thread-old" not in args


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_485_takeover_uses_loader_defaults_and_reconciles_in_live_object(tmp_path, cli_name):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    alias = state
    state.conversation_context_tokens = 999
    state.conversation_turns = 12
    saved = {"cli": cli_name, "fresh_request_id": "fr-aaaaaaaaaaaa", "resume_available": False,
             "claude_session_id": "22222222-2222-4222-8222-222222222222",
             "resume_failure_count": "broken", "resume_attempt_state": "in_progress",
             "resume_attempt_msg_id": "old-attempt", "future_field": "ignored"}
    _session_file(s).write_text(json.dumps(saved), encoding="utf-8")
    expected = session.load_session(s, "beta", cli_name)
    session.apply_fresh_request(s, "beta", state, _saved(s))
    assert alias == expected
    assert alias.resume_failure_count == 0 and alias.resume_attempt_state == "ambiguous"
    assert alias.conversation_context_tokens is None and alias.conversation_turns == 0
    assert s.read_fresh_request("beta") is None


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("restart", [False, True])
def test_485_retry_preserves_original_backup_after_partial_write(tmp_path, cli_name, restart):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    original = _session_file(s).read_bytes()

    def partial_write(_state):
        _session_file(s).write_text("{", encoding="utf-8")
        raise OSError("partial direct write")

    run._apply_fresh_request(s, "beta", state, partial_write)
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    assert backup.read_bytes() == original and s.read_fresh_request("beta") is not None
    if restart:
        state = session.load_session(s, "beta", cli_name)
    run._apply_fresh_request(s, "beta", state, _saved(s))
    assert backup.read_bytes() == original
    assert s.read_fresh_request("beta") is None
    args = session.build_turn(state, "hello").args
    assert OLD_ID not in args and "thread-old" not in args


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_485_real_drive_uses_fresh_session_after_unknown_write(tmp_path, monkeypatch, cli_name):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    _485_double_fault(s, state, monkeypatch)
    _send_one(s)
    calls = []
    drive = run.make_drive(s, "beta", cli_name, state, [cli_name], render=False, clock=lambda: 0.0,
                           persist=_saved(s), spawn=lambda argv, stdin: calls.append(argv) or _ok_lines())
    loop.run_loop(s, "beta", drive, max_polls=2, max_turns=1, clock=lambda: 0.0, sleep=lambda _: None)
    assert calls and OLD_ID not in calls[0] and "thread-old" not in calls[0]
    assert "resume" not in calls[0] and "--resume" not in calls[0]


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "beta", "gamma"])
    s.set_operator_facing("lead")
    return s


def _run(root: Path, *argv: str) -> int:
    return cli.main(["--root", str(root), *argv])


def _session_file(s: Store) -> Path:
    return s.state_dir / "beta.wrapper-session.json"


def _claude_state(**kw) -> session.SessionState:
    return session.SessionState(cli="claude", claude_session_id=OLD_ID, turns=5,
                                resume_available=True, **kw)


def _saved(s: Store):
    return lambda st: session.save_session(s, "beta", st)


def _request(s: Store, rid: str = "fr-aaaaaaaaaaaa", reason: str = "conversation too big") -> None:
    assert s.write_fresh_request("beta", {"agent": "beta", "request_id": rid, "reason": reason,
                                          "requested_by": "lead", "at": "2026-10-13T12:00:00Z"})


def _ok_lines() -> list[str]:
    return [json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})]


# ------------------------------------------------------------------ the command


def test_the_command_writes_a_request_and_never_touches_the_session_record(tmp_path, capsys) -> None:
    s = _store(tmp_path)
    session.save_session(s, "beta", _claude_state())
    before = _session_file(s).read_bytes()

    assert _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "lead",
                "--reason", "  too   big  ") == 0

    marker = s.read_fresh_request("beta")
    assert marker["agent"] == "beta" and marker["requested_by"] == "lead"
    assert marker["reason"] == "too big" and marker["request_id"].startswith("fr-")
    assert _session_file(s).read_bytes() == before  # a request, not an edit
    assert "applies it between turns" in capsys.readouterr().out


def test_the_command_requires_a_reason_a_roster_agent_and_an_authorised_requester(tmp_path) -> None:
    s = _store(tmp_path)
    with pytest.raises(SystemExit) as no_reason:
        _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "lead")
    assert no_reason.value.code == 2
    assert _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "lead", "--reason", " ") == 2
    assert _run(tmp_path, "session", "fresh", "--for", "ghost", "--from", "lead", "--reason", "x") == 2
    # another worker may not reset beta; beta itself and the operator-facing lead may
    assert _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "gamma", "--reason", "x") == 2
    assert s.read_fresh_request("beta") is None
    assert _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "beta", "--reason", "x") == 0


def test_a_second_request_while_one_is_pending_is_refused_not_duplicated(tmp_path) -> None:
    s = _store(tmp_path)
    assert _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "lead", "--reason", "one") == 0
    first = s.read_fresh_request("beta")

    assert _run(tmp_path, "session", "fresh", "--for", "beta", "--from", "lead", "--reason", "two") == 1

    assert s.read_fresh_request("beta") == first


# ------------------------------------------------------- the wrapper applies it


def test_claude_gets_a_new_session_id_and_never_resumes_the_old_conversation(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _request(s)

    outcome = session.apply_fresh_request(s, "beta", state, _saved(s))

    assert outcome["outcome"] == "applied"
    assert state.claude_session_id != OLD_ID
    spec = session.build_turn(state, "hello")
    assert "--resume" not in spec.args and OLD_ID not in spec.args
    assert "--session-id" in spec.args and state.claude_session_id in spec.args
    saved = json.loads(_session_file(s).read_text(encoding="utf-8"))
    assert saved["claude_session_id"] == state.claude_session_id
    assert saved["fresh_request_id"] == "fr-aaaaaaaaaaaa" and saved["fresh_request_outcome"] == "applied"
    assert saved["fresh_request_reason"] == "conversation too big" and saved["fresh_request_turn"] == 5
    assert s.read_fresh_request("beta") is None  # the request is consumed
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    assert json.loads(backup.read_text(encoding="utf-8"))["claude_session_id"] == OLD_ID


def test_codex_gets_its_thread_cleared_and_starts_a_fresh_exec(tmp_path) -> None:
    s = _store(tmp_path)
    state = session.SessionState(cli="codex", codex_thread_id="thread-old", turns=9, resume_available=True)
    _request(s)

    session.apply_fresh_request(s, "beta", state, _saved(s))

    assert state.codex_thread_id is None
    spec = session.build_turn(state, "hello")
    assert "resume" not in spec.args and "thread-old" not in spec.args


def test_a_request_is_applied_exactly_once_even_when_the_clearing_was_lost(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    _request(s)
    session.apply_fresh_request(s, "beta", state, _saved(s))
    applied_id = state.claude_session_id

    _request(s)  # the same marker survives, as after a crash between "saved" and "cleared"
    assert session.apply_fresh_request(s, "beta", state, _saved(s)) is None

    assert state.claude_session_id == applied_id  # not reset a second time
    assert s.read_fresh_request("beta") is None
    _request(s, rid="fr-bbbbbbbbbbbb")  # a genuinely new request applies again
    assert session.apply_fresh_request(s, "beta", state, _saved(s))["outcome"] == "applied"
    assert state.claude_session_id != applied_id


def test_a_failed_save_keeps_the_request_until_the_reset_is_saved_to_disk(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)  # the initial record is on disk
    _request(s)
    failures = {"left": 1}

    def flaky(st) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("disk full")
        session.save_session(s, "beta", st)

    run._apply_fresh_request(s, "beta", state, flaky)  # never raises into the wrapper

    assert s.read_fresh_request("beta") is not None  # still pending: nothing durable yet
    assert session.load_session(s, "beta", "claude").claude_session_id == OLD_ID
    assert state.claude_session_id == OLD_ID  # nothing runs on a reset that is not saved
    assert state.fresh_request_id == "" and state.resume_available
    run._apply_fresh_request(s, "beta", state, flaky)  # the same-process retry applies and saves it
    assert s.read_fresh_request("beta") is None
    reloaded = session.load_session(s, "beta", "claude")
    assert reloaded.claude_session_id == state.claude_session_id != OLD_ID
    assert reloaded.fresh_request_id == "fr-aaaaaaaaaaaa"  # a restart would not resume the old one


def test_a_restart_after_a_failed_save_still_finds_the_request(tmp_path) -> None:
    for cli_name, old in (("claude", OLD_ID), ("codex", "thread-old")):
        root = tmp_path / cli_name
        s = _store(root)
        state = (_claude_state() if cli_name == "claude"
                 else session.SessionState(cli="codex", codex_thread_id=old, turns=4))
        session.save_session(s, "beta", state)
        _request(s)

        def broken(_st) -> None:
            raise OSError("disk full")

        run._apply_fresh_request(s, "beta", state, broken)
        restarted = session.load_session(s, "beta", cli_name)  # the old record, as on disk
        assert s.read_fresh_request("beta") is not None
        session.apply_fresh_request(s, "beta", restarted, _saved(s))
        spec = session.build_turn(restarted, "hello")
        assert "--resume" not in spec.args and "resume" not in spec.args and old not in spec.args


def test_no_backup_means_no_reset_and_the_request_stays_pending(tmp_path, monkeypatch) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _request(s)

    def no_copy(*_a, **_k):
        raise OSError("read-only folder")

    monkeypatch.setattr(session.shutil, "copyfile", no_copy)
    run._apply_fresh_request(s, "beta", state, _saved(s))

    assert state.claude_session_id == OLD_ID and state.fresh_request_id == ""
    assert s.read_fresh_request("beta") is not None
    assert json.loads(_session_file(s).read_text(encoding="utf-8"))["claude_session_id"] == OLD_ID
    monkeypatch.undo()
    run._apply_fresh_request(s, "beta", state, _saved(s))  # the folder is writable again
    assert state.claude_session_id != OLD_ID and s.read_fresh_request("beta") is None


def test_the_requester_is_kept_in_the_applied_record_and_status(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    _request(s)
    assert s.read_fresh_status("beta")["pending"]["requested_by"] == "lead"

    session.apply_fresh_request(s, "beta", state, _saved(s))

    assert json.loads(_session_file(s).read_text(encoding="utf-8"))["fresh_request_by"] == "lead"
    assert s.read_fresh_status("beta")["applied"]["requested_by"] == "lead"


def test_a_late_status_write_by_a_superseded_wrapper_cannot_take_the_request(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    old_runtime = wr.WrapperRuntimeWriter(s.state_dir, "beta", "generation-old", wrapper_pid=1111,
                                          wrapper_start="a")
    run.claim_wrapper_ownership(s, "beta", "generation-old")
    old_runtime.starting(message_id="20261013-000000-000000-aaaa")  # the old wrapper's turn is running
    assert run.is_active_wrapper(s, "beta", "generation-old")

    # the replacement starts: it claims the agent and publishes its own idle runtime
    new_runtime = wr.WrapperRuntimeWriter(s.state_dir, "beta", "generation-new", wrapper_pid=2222,
                                          wrapper_start="b")
    run.claim_wrapper_ownership(s, "beta", "generation-new")
    new_runtime.idle()

    old_runtime.idle()  # the old turn finishes and writes late: the runtime record names it again
    assert wr.read_runtime(s.state_dir, "beta")["record"]["wrapper_generation"] == "generation-old"
    _request(s)  # a request arrives

    old_owner = lambda: run.is_active_wrapper(s, "beta", "generation-old")  # noqa: E731
    run._apply_fresh_request(s, "beta", state, _saved(s), owner_check=old_owner)
    assert s.read_fresh_request("beta") is not None and state.claude_session_id == OLD_ID

    new_owner = lambda: run.is_active_wrapper(s, "beta", "generation-new")  # noqa: E731
    run._apply_fresh_request(s, "beta", state, _saved(s), owner_check=new_owner)
    assert s.read_fresh_request("beta") is None and state.claude_session_id != OLD_ID


def test_no_ownership_claim_yet_is_not_the_active_wrapper(tmp_path) -> None:
    assert not run.is_active_wrapper(_store(tmp_path), "beta", "generation-new")


def test_an_unreadable_request_is_removed_and_recorded_without_a_reset(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    (s.state_dir / "beta.fresh-request").write_text("{not json", encoding="utf-8")

    outcome = session.apply_fresh_request(s, "beta", state, _saved(s))

    assert outcome["outcome"] == "unreadable_request_removed"
    assert state.claude_session_id == OLD_ID and state.resume_available
    assert s.read_fresh_request("beta") is None


def test_a_cli_without_support_records_it_and_resets_nothing(tmp_path) -> None:
    s = _store(tmp_path)
    state = session.SessionState(cli="other", turns=3)
    _request(s)

    outcome = session.apply_fresh_request(s, "beta", state, _saved(s))

    assert outcome["outcome"] == "unsupported_cli"
    assert state.resume_available and s.read_fresh_request("beta") is None


# ------------------------------------------------------------ between turns only


def test_a_request_that_arrives_during_a_turn_waits_for_the_turn_to_end(tmp_path) -> None:
    s = _store(tmp_path)
    for body in ("first", "second"):
        s.send(sender="lead", recipient="beta", body=body, kind="message", meta={})
    state = _claude_state()
    calls: list[list[str]] = []

    def spawn(argv, _stdin):
        calls.append(list(argv))
        if len(calls) == 1:  # the request lands while turn one is running
            _request(s)
        return _ok_lines()

    drive = run.make_drive(s, "beta", "claude", state, ["claude"], spawn=spawn,
                           clock=lambda: 0.0, render=False, persist=_saved(s))
    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=2)

    assert "--resume" in calls[0] and OLD_ID in calls[0]  # turn one ran on the old conversation
    assert "--session-id" in calls[1] and OLD_ID not in calls[1]  # turn two on a fresh one
    assert "--resume" not in calls[1]
    assert state.fresh_request_outcome == "applied" and s.read_fresh_request("beta") is None
    assert recv_api.next_record(s, "beta") is None


def test_an_ordinary_empty_idle_poll_applies_a_request_written_while_idle(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    _request(s)

    loop.run_loop(s, "beta", lambda rec: True, clock=lambda: 0.0, sleep=lambda d: None, max_polls=2,
                  on_idle_poll=lambda: run._apply_fresh_request(s, "beta", state, _saved(s)))

    assert state.claude_session_id != OLD_ID and s.read_fresh_request("beta") is None


# ------------------------------------------------------------------ status


def test_status_shows_pending_then_applied_with_the_reason(tmp_path, capsys) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _request(s)

    assert _run(tmp_path, "status", "--json") == 0
    rows = {a["name"]: a for a in json.loads(capsys.readouterr().out)["agents"]}
    assert rows["beta"]["fresh_request"]["pending"]["reason"] == "conversation too big"
    assert "fresh_request" not in rows["gamma"]

    session.apply_fresh_request(s, "beta", state, _saved(s))
    assert _run(tmp_path, "status", "--json") == 0
    rows = {a["name"]: a for a in json.loads(capsys.readouterr().out)["agents"]}
    applied = rows["beta"]["fresh_request"]["applied"]
    assert applied["outcome"] == "applied" and applied["reason"] == "conversation too big"
    assert applied["turns_before"] == 5 and applied["request_id"] == "fr-aaaaaaaaaaaa"
    assert "session_id" not in json.dumps(rows["beta"]["fresh_request"])  # no session identities

    assert _run(tmp_path, "status") == 0
    assert "fresh=applied" in capsys.readouterr().out


def test_the_dashboard_state_shows_a_pending_then_applied_request(tmp_path) -> None:
    from agenttalk import web

    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _request(s)

    def beta_entry() -> dict:
        root = web.build_state([web.RootDescriptor(store=s, label="root")])["roots"][0]
        return next(a for a in root["agents"] if a["name"] == "beta")

    assert beta_entry()["fresh_request"] == {"state": "pending", "at": "2026-10-13T12:00:00Z",
                                             "requested_by": "lead"}
    session.apply_fresh_request(s, "beta", state, _saved(s))
    entry = beta_entry()
    assert entry["fresh_request"]["state"] == "applied" and entry["fresh_request"]["requested_by"] == "lead"
    assert "conversation too big" not in json.dumps(entry)  # no free text on the dashboard
    assert OLD_ID not in json.dumps(entry) and state.claude_session_id not in json.dumps(entry)


def test_the_dashboard_projection_knows_the_new_reset_reason(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    _request(s)
    session.apply_fresh_request(s, "beta", state, _saved(s))

    assert s.read_wrapper_runtime("beta")["reset_reason"] == "fresh_requested"


# ------------------------------------------------ turn path: the fence and a failed save


def _drive_claude(s: Store, state, calls: list, *, generation: str | None, persist=None):
    def spawn(argv, _stdin):
        calls.append(list(argv))
        return _ok_lines()

    return run.make_drive(s, "beta", "claude", state, ["claude"], spawn=spawn, clock=lambda: 0.0,
                          render=False, persist=persist or _saved(s), wrapper_generation=generation)


def _send_one(s: Store) -> None:
    s.send(sender="lead", recipient="beta", body="work", kind="message", meta={})


def test_a_superseded_wrapper_finishing_a_turn_cannot_take_the_request(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    run.claim_wrapper_ownership(s, "beta", "generation-new")  # the replacement owns the agent
    _request(s)
    _send_one(s)
    calls: list = []
    drive = _drive_claude(s, state, calls, generation="generation-old")

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1)

    assert s.read_fresh_request("beta") is not None  # left for the replacement
    assert "--resume" in calls[0] and OLD_ID in calls[0]


def test_the_active_wrapper_applies_the_request_at_the_start_of_a_turn(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    run.claim_wrapper_ownership(s, "beta", "generation-new")
    _request(s)
    _send_one(s)
    calls: list = []
    drive = _drive_claude(s, state, calls, generation="generation-new")

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1)

    assert s.read_fresh_request("beta") is None
    assert "--session-id" in calls[0] and OLD_ID not in calls[0]


def test_a_superseded_wrapper_cannot_take_the_request_on_a_cadence_turn(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    run.claim_wrapper_ownership(s, "beta", "generation-new")
    _request(s)
    calls: list = []
    cadence = run.make_cadence_drive(
        s, "beta", "claude", state, ["claude"], clock=lambda: 0.0, render=False, persist=_saved(s),
        spawn=lambda argv, _stdin: calls.append(list(argv)) or _ok_lines(),
        wrapper_generation="generation-old")

    cadence({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}])

    assert s.read_fresh_request("beta") is not None
    assert "--resume" in calls[0] and OLD_ID in calls[0]


def test_a_failed_save_at_turn_start_runs_the_turn_on_the_saved_conversation(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _request(s)
    _send_one(s)
    calls: list = []
    failures = {"left": 1}

    def flaky(st) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("disk full")
        session.save_session(s, "beta", st)

    drive = _drive_claude(s, state, calls, generation=None, persist=flaky)

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1)

    # the reset could not be saved, so the turn did NOT run on an unsaved fresh conversation
    assert "--resume" in calls[0] and OLD_ID in calls[0]
    assert s.read_fresh_request("beta") is not None  # still pending: a later process applies it once
    on_disk = session.load_session(s, "beta", "claude")
    assert on_disk.claude_session_id == OLD_ID and on_disk.fresh_request_id == ""


# ------------------------------------------------------------ parked head records


def test_a_parked_head_does_not_make_a_request_wait(tmp_path) -> None:
    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _send_one(s)
    polls = {"with_head": 0}

    def parked_drive(_rec):
        return loop.DriveOutcome(ok=False, failure_class=loop.CLASS_CONFIG_BLOCKED,
                                 summary="gateway is held")

    def hook() -> None:
        if recv_api.next_record(s, "beta") is not None:
            polls["with_head"] += 1
            if polls["with_head"] == 2:  # the request is written while the head is parked
                _request(s)
        run._apply_fresh_request(s, "beta", state, _saved(s))

    loop.run_loop(s, "beta", parked_drive, clock=lambda: 0.0, sleep=lambda d: None, max_polls=8,
                  k_poison=0, k_escalate=20, on_idle_poll=hook)

    assert recv_api.next_record(s, "beta") is not None  # the head is still parked, never consumed
    assert s.read_fresh_request("beta") is None  # yet the request was applied
    assert state.claude_session_id != OLD_ID


# -------------------------------------------- a save that landed but then raised (#483)


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_a_save_that_landed_before_fsync_raised_is_adopted_not_undone(tmp_path, monkeypatch, cli_name) -> None:
    from agenttalk import _atomic

    s = _store(tmp_path)
    state = (_claude_state() if cli_name == "claude"
             else session.SessionState(cli="codex", codex_thread_id="thread-old", turns=7,
                                       resume_available=True))
    session.save_session(s, "beta", state)
    _request(s)
    real_fsync = _atomic.os.fsync
    calls = {"n": 0, "failed": False}

    def fsync_fails_once(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            calls["failed"] = True
            raise OSError("fsync failed after the data was written and flushed")
        return real_fsync(fd)

    monkeypatch.setattr(_atomic, "_sandbox_direct_write", True)  # the direct final-path write
    monkeypatch.setattr(_atomic.os, "fsync", fsync_fails_once)

    outcome = session.apply_fresh_request(s, "beta", state, lambda st: session.save_session(s, "beta", st))

    assert calls["failed"]  # the injected failure really happened, after the write
    assert outcome["outcome"] == "applied" and s.read_fresh_request("beta") is None
    on_disk = session.load_session(s, "beta", cli_name)
    assert on_disk.fresh_request_id == "fr-aaaaaaaaaaaa"
    # memory and disk agree on the reset, so the next turn starts fresh and saving it keeps the reset
    if cli_name == "claude":
        assert state.claude_session_id == on_disk.claude_session_id != OLD_ID
    else:
        assert state.codex_thread_id is None and on_disk.codex_thread_id is None
    spec = session.build_turn(state, "hello")
    assert "--resume" not in spec.args and "resume" not in spec.args
    session.save_session(s, "beta", state)
    assert session.load_session(s, "beta", cli_name).fresh_request_id == "fr-aaaaaaaaaaaa"


def test_a_failed_save_that_did_not_land_still_restores_the_old_session(tmp_path, monkeypatch) -> None:
    from agenttalk import _atomic

    s = _store(tmp_path)
    state = _claude_state()
    session.save_session(s, "beta", state)
    _request(s)

    def write_fails(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(_atomic, "write_text", write_fails)
    with pytest.raises(OSError):
        session.apply_fresh_request(s, "beta", state, lambda st: session.save_session(s, "beta", st))

    assert state.claude_session_id == OLD_ID and s.read_fresh_request("beta") is not None


# ------------------------------------------- the supervisor's list and the store's lock order (#483 CI)


def test_the_supervisor_knows_session_so_a_reason_word_is_not_taken_for_the_command() -> None:
    from agenttalk import supervisor as sup

    # "reset" is a real agenttalk command; inside --reason it must not become the subcommand
    line = "agenttalk --root D:/p session fresh --for beta --reason reset"
    inv = sup._agenttalk_invocation(line, "D:/p")
    assert inv["subcommand"] == "session" and inv["args"][:2] == ["fresh", "--for"]
    assert "session" in sup._AGENTTALK_SUBCOMMANDS


def test_the_request_marker_locks_keep_the_store_lock_order(tmp_path) -> None:
    from agenttalk import lock_order

    s = _store(tmp_path)
    marker = {"agent": "beta", "request_id": "fr-cccccccccccc", "reason": "x", "requested_by": "lead"}
    assert s.write_fresh_request("beta", marker)  # nothing held: allowed
    assert not s.write_fresh_request("beta", marker)
    # a lock ranked above the config lock may not be held when the marker lock is taken: the order is enforced
    with lock_order.hold(s.dir, s.dir / "message-publication"):
        with pytest.raises(TimeoutError, match="lock order inversion"):
            s.clear_fresh_request("beta", "fr-cccccccccccc")
        with pytest.raises(TimeoutError, match="lock order inversion"):
            s.write_fresh_request("gamma", marker)
    assert s.read_fresh_request("beta") is not None  # the refused clear changed nothing
    assert s.clear_fresh_request("beta", "fr-cccccccccccc")
    assert not getattr(lock_order._local, "held", None)  # nothing is left held


def test_the_wrapper_applies_a_request_with_no_store_lock_held(tmp_path) -> None:
    from agenttalk import lock_order

    s = _store(tmp_path)
    state = _claude_state()
    held_at_save: list = []

    def persist(st) -> None:
        held_at_save.append(dict(getattr(lock_order._local, "held", None) or {}))
        session.save_session(s, "beta", st)

    _request(s)
    loop.run_loop(s, "beta", lambda rec: True, clock=lambda: 0.0, sleep=lambda d: None, max_polls=2,
                  on_idle_poll=lambda: run._apply_fresh_request(s, "beta", state, persist))
    _request(s, rid="fr-dddddddddddd")
    s.send(sender="lead", recipient="beta", body="work", kind="message", meta={})
    spawn_calls: list = []
    drive = run.make_drive(s, "beta", "claude", state, ["claude"], clock=lambda: 0.0, render=False,
                           persist=persist, spawn=lambda a, i: spawn_calls.append(a) or _ok_lines())
    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1)

    assert held_at_save and all(not h for h in held_at_save)  # idle path and turn-start path
    assert s.read_fresh_request("beta") is None


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("how", ["copy_stops_halfway", "copy_differs"])
def test_485_failed_backup_is_not_accepted_on_retry(tmp_path, monkeypatch, cli_name, how):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    original = _session_file(s).read_bytes()

    def bad_copy(src, dst):
        Path(dst).write_bytes(Path(src).read_bytes()[:10])
        if how == "copy_stops_halfway":
            raise OSError("disk full")

    monkeypatch.setattr(session.shutil, "copyfile", bad_copy)
    run._apply_fresh_request(s, "beta", state, _saved(s))
    assert s.read_fresh_request("beta") is not None and state.fresh_request_id == ""  # first attempt: no reset
    monkeypatch.undo()
    run._apply_fresh_request(s, "beta", state, _saved(s))  # storage is healthy again
    assert s.read_fresh_request("beta") is None  # the reset went ahead
    assert (s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json").read_bytes() == original


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("crash", [False, True])
def test_485_nothing_written_and_readback_unknown(tmp_path, monkeypatch, cli_name, crash):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    old_bytes = _session_file(s).read_bytes()
    real_read, armed, fired = Path.read_text, [False], [False]

    def persist(_st):
        armed[0] = True
        raise OSError("failed before writing")

    def read(path, *a, **kw):
        if path == _session_file(s) and armed[0] and not fired[0]:
            fired[0] = True
            raise PermissionError("unreadable once")
        return real_read(path, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    run._apply_fresh_request(s, "beta", state, persist)
    assert fired[0] and s.read_fresh_request("beta") is not None
    assert _session_file(s).read_bytes() == old_bytes
    args = session.build_turn(state, "hello").args
    assert OLD_ID not in args and "thread-old" not in args and "resume" not in args and "--resume" not in args
    first = state.claude_session_id
    if crash:
        state = session.load_session(s, "beta", cli_name)
        assert state.claude_session_id == (OLD_ID if cli_name == "claude" else None)
    run._apply_fresh_request(s, "beta", state, _saved(s))
    assert s.read_fresh_request("beta") is None
    assert (s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json").read_bytes() == old_bytes
    args = session.build_turn(state, "hello").args
    assert OLD_ID not in args and "thread-old" not in args
    assert session.load_session(s, "beta", cli_name) == state
    if cli_name == "claude":
        assert (state.claude_session_id != first) is crash  # a crash resets again; a plain retry keeps the same id


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize(
    "damage",
    ["torn", "list", "null", "text", "number", "boolean", "bad_utf8"],
)
def test_485_rebuilds_unusable_old_version_backup(tmp_path, cli_name, damage):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    original = _session_file(s).read_bytes()
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    payloads = {
        "torn": b'{"cli":',
        "list": b"[]",
        "null": b"null",
        "text": b'"old"',
        "number": b"0",
        "boolean": b"false",
        "bad_utf8": b"\xff",
    }
    backup.write_bytes(payloads[damage])  # an older version published this final name before finishing
    run._apply_fresh_request(s, "beta", state, _saved(s))

    assert s.read_fresh_request("beta") is None
    assert session.load_session(s, "beta", cli_name) == state
    assert backup.read_bytes() == original
    assert not backup.with_name(backup.name + ".part").exists()


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_485_keeps_existing_empty_object_backup(tmp_path, cli_name):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    backup.write_bytes(b"{}")

    run._apply_fresh_request(s, "beta", state, _saved(s))

    assert s.read_fresh_request("beta") is None
    assert backup.read_bytes() == b"{}"


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("torn_record", [False, True])
def test_485_backup_read_error_keeps_request_pending(tmp_path, monkeypatch, cli_name, torn_record):
    from dataclasses import replace

    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    original = _session_file(s).read_bytes()
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    if torn_record:

        def partial_write(_state):
            _session_file(s).write_text("{", encoding="utf-8")
            raise OSError("partial session write")

        run._apply_fresh_request(s, "beta", state, partial_write)
        assert backup.read_bytes() == original
        state = session.load_session(s, "beta", cli_name)
    else:
        backup.write_bytes(original)
    before = replace(state)
    record = _session_file(s).read_bytes()
    real_read, fired = Path.read_text, []

    def read(path, *args, **kwargs):
        if path == backup and not fired:
            fired.append(path)
            raise PermissionError("backup held by scanner once")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    run._apply_fresh_request(s, "beta", state, _saved(s))

    assert fired
    assert s.read_fresh_request("beta") is not None
    assert state == before
    assert backup.read_bytes() == original
    assert _session_file(s).read_bytes() == record
    assert not backup.with_name(backup.name + ".part").exists()

    run._apply_fresh_request(s, "beta", state, _saved(s))

    assert s.read_fresh_request("beta") is None
    assert backup.read_bytes() == original
    assert session.load_session(s, "beta", cli_name) == state
    args = session.build_turn(state, "hello").args
    assert OLD_ID not in args and "thread-old" not in args


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_485_rebuilds_every_incomplete_backup_prefix(tmp_path, cli_name):
    s = _store(tmp_path)
    _485_existing(s, cli_name)
    original = _session_file(s).read_bytes().rstrip()
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    for cut in range(len(original)):
        backup.write_bytes(original[:cut])
        session._backup_session_record(s, "beta", "fr-aaaaaaaaaaaa")
        assert json.loads(backup.read_text(encoding="utf-8")) == json.loads(original), cut


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("existing", [False, True])
def test_485_backup_succeeds_without_rename(tmp_path, monkeypatch, cli_name, existing):
    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    original = _session_file(s).read_bytes()
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    if existing:
        backup.write_bytes(b'{"cli":')
    attempted = []
    real_replace, real_rename = Path.replace, Path.rename
    real_os_replace, real_os_rename = os.replace, os.rename

    def guarded(operation):
        def rename(path, target):
            if Path(target) == backup:
                attempted.append(path)
                raise PermissionError("backup rename blocked by sandbox")
            return operation(path, target)

        return rename

    monkeypatch.setattr(Path, "replace", guarded(real_replace))
    monkeypatch.setattr(Path, "rename", guarded(real_rename))
    monkeypatch.setattr(os, "replace", guarded(real_os_replace))
    monkeypatch.setattr(os, "rename", guarded(real_os_rename))
    run._apply_fresh_request(s, "beta", state, _saved(s))

    assert s.read_fresh_request("beta") is None
    assert backup.read_bytes() == original
    assert session.load_session(s, "beta", cli_name) == state
    assert attempted == []
    assert not backup.with_name(backup.name + ".part").exists()
    args = session.build_turn(state, "hello").args
    assert OLD_ID not in args and "thread-old" not in args


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("refuse_delete", [False, True])
def test_707_mismatched_backup_is_removed_before_refusal(tmp_path, monkeypatch, cli_name, refuse_delete):
    from dataclasses import replace

    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    before = replace(state)
    original = _session_file(s).read_bytes()
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    real_unlink = Path.unlink
    attempts = []

    def bad_copy(src, dst):
        Path(dst).write_bytes(b'{"different":true}')

    def unlink(path, *args, **kwargs):
        if path == backup:
            attempts.append(path)
            if refuse_delete:
                raise PermissionError("delete blocked by sandbox")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(session.shutil, "copyfile", bad_copy)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="backup does not match"):
        session.apply_fresh_request(s, "beta", state, _saved(s))

    assert attempts == [backup]
    assert backup.exists() is refuse_delete
    assert state == before
    assert s.read_fresh_request("beta") is not None
    assert _session_file(s).read_bytes() == original
    if not refuse_delete:
        monkeypatch.undo()
        session.apply_fresh_request(s, "beta", state, _saved(s))
        assert backup.read_bytes() == original
        assert s.read_fresh_request("beta") is None


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
@pytest.mark.parametrize("damage", ["directory", "permission"])
def test_707_unreadable_backup_logs_why_request_stays_pending(tmp_path, monkeypatch, cli_name, damage):
    import io
    from dataclasses import replace
    from agenttalk.wrapper_logs import WrapperLifecycleLog

    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    before = replace(state)
    original = _session_file(s).read_bytes()
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    real_read = Path.read_text
    if damage == "directory":
        backup.mkdir()
    else:
        backup.write_bytes(original)

        def read(path, *args, **kwargs):
            if path == backup:
                raise PermissionError("old conversation backup cannot be read")
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    stream = io.StringIO()
    lifecycle = WrapperLifecycleLog("beta", stream=stream, clock=lambda: 0)
    for _ in range(2):
        run._apply_fresh_request(s, "beta", state, _saved(s), lifecycle_log=lifecycle)
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(rows) == 1
    assert all(row["event"] == "fresh_request_pending" and row["agent"] == "beta" for row in rows)
    assert all(row["exception_type"] in {"PermissionError", "IsADirectoryError"} and row["reason"] for row in rows)
    assert all("backup" in row["reason"] if damage == "permission" else backup.name in row["reason"] for row in rows)
    assert state == before
    assert s.read_fresh_request("beta") is not None
    assert _session_file(s).read_bytes() == original
    if damage == "directory":
        backup.rmdir()
    else:
        assert backup.read_bytes() == original
        monkeypatch.undo()
    run._apply_fresh_request(s, "beta", state, _saved(s), lifecycle_log=lifecycle)
    assert s.read_fresh_request("beta") is None
    assert len(stream.getvalue().splitlines()) == 1


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_707_idle_caller_logs_unreadable_backup(tmp_path, monkeypatch, cli_name):
    import io
    from agenttalk.wrapper_logs import WrapperLifecycleLog

    s = _store(tmp_path)
    _485_existing(s, cli_name)
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    backup.mkdir()
    stream = io.StringIO()
    lifecycle = WrapperLifecycleLog("beta", stream=stream, clock=lambda: 0)
    polls = []
    records = []

    def idle_loop(store, agent, drive, **kwargs):
        assert store is s and agent == "beta"
        polls.append(agent)
        records.append(_session_file(s).read_bytes())
        kwargs["on_idle_poll"]()
        records.append(_session_file(s).read_bytes())
        return 0

    monkeypatch.setattr(loop, "run_loop", idle_loop)
    assert (
        cli._wrap_loop_mode(
            s,
            "beta",
            cli=cli_name,
            base_argv=[cli_name],
            sender="beta",
            min_interval=0.0,
            render=False,
            lifecycle_log=lifecycle,
        )
        == 0
    )

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    pending = [row for row in rows if row["event"] == "fresh_request_pending"]
    assert polls == ["beta"] and len(pending) == 1
    assert backup.name in pending[0]["reason"]
    assert s.read_fresh_request("beta") is not None
    assert records[0] == records[1]
    saved = session.load_session(s, "beta", cli_name)
    assert saved.fresh_request_id == ""
    assert saved.claude_session_id == OLD_ID if cli_name == "claude" else saved.codex_thread_id == "thread-old"
    assert backup.is_dir()


@pytest.mark.parametrize("cli_name", ["claude", "codex"])
def test_707_recovery_ends_the_failure_episode(tmp_path, monkeypatch, cli_name):
    import io
    from dataclasses import replace
    from agenttalk.wrapper_logs import WrapperLifecycleLog

    s = _store(tmp_path)
    state = _485_existing(s, cli_name)
    stream = io.StringIO()
    lifecycle = WrapperLifecycleLog("beta", stream=stream, clock=lambda: 0)

    def bad_copy(src, dst):
        Path(dst).write_bytes(b"{}")

    def fail_twice():
        before = replace(state)
        record = _session_file(s).read_bytes()
        with monkeypatch.context() as fault:
            fault.setattr(session.shutil, "copyfile", bad_copy)
            for _ in range(2):
                run._apply_fresh_request(s, "beta", state, _saved(s), lifecycle_log=lifecycle)
        assert s.read_fresh_request("beta") is not None
        assert state == before and _session_file(s).read_bytes() == record

    fail_twice()
    assert len(stream.getvalue().splitlines()) == 1
    run._apply_fresh_request(s, "beta", state, _saved(s), lifecycle_log=lifecycle)
    assert s.read_fresh_request("beta") is None
    assert state.fresh_request_id == "fr-aaaaaaaaaaaa"
    assert len(stream.getvalue().splitlines()) == 1

    _request(s, rid="fr-bbbbbbbbbbbb")
    fail_twice()
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(rows) == 2
    assert all(row["event"] == "fresh_request_pending" for row in rows)
    assert rows[0]["exception_type"] == rows[1]["exception_type"] == "OSError"
    assert rows[0]["reason"] == rows[1]["reason"] == "the session record backup does not match the record"
    assert s.read_fresh_request("beta")["request_id"] == "fr-bbbbbbbbbbbb"


@pytest.mark.parametrize("path", ["turn", "cadence"])
def test_707_turn_paths_log_pending_request(tmp_path, path):
    import io
    from agenttalk.wrapper_logs import WrapperLifecycleLog

    s = _store(tmp_path)
    state = _485_existing(s, "claude")
    backup = s.state_dir / "beta.wrapper-session.before-fresh-fr-aaaaaaaaaaaa.json"
    backup.mkdir()
    stream = io.StringIO()
    lifecycle = WrapperLifecycleLog("beta", stream=stream, clock=lambda: 0)
    calls = []
    options = {
        "clock": lambda: 0.0,
        "render": False,
        "persist": _saved(s),
        "spawn": lambda argv, stdin: calls.append(list(argv)) or _ok_lines(),
        "lifecycle_log": lifecycle,
    }
    if path == "turn":
        _send_one(s)
        drive = run.make_drive(s, "beta", "claude", state, ["claude"], **options)
        loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda _: None, max_turns=1)
    else:
        drive = run.make_cadence_drive(s, "beta", "claude", state, ["claude"], **options)
        drive({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}])

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    pending = [row for row in rows if row["event"] == "fresh_request_pending"]
    assert len(pending) == 1
    assert backup.name in pending[0]["reason"]
    assert pending[0]["exception_type"] in {"PermissionError", "IsADirectoryError"}
    assert s.read_fresh_request("beta") is not None
    assert calls and "--resume" in calls[0] and OLD_ID in calls[0]
    assert session.load_session(s, "beta", "claude").fresh_request_id == ""
