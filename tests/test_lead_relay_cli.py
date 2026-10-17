"""The commands of the owed-relay slice: ``relay result``, ``relay waiting`` and the owed-relay notice after the
consuming commands.

Design: docs/DESIGN-lead-loop-default.md, sections 5 and 11 (cases 12c, 12e).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import lead_relay as relay
from agenttalk.store import Store


@pytest.fixture
def lead_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv("AGENTTALK_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    s = Store(tmp_path)
    s.init(["lead", "reviewer", "builder", "other"])
    s.set_role("lead", "lead")
    return s


def _scenario(s: Store) -> str:
    """lead asked reviewer (R1); builder waits for the decision; the verdict arrived. Returns the result id."""
    s.send(sender="lead", recipient="reviewer", kind="review-request", body="please", meta={"request_id": "R1"})
    s.send(sender="builder", recipient="lead", body="I wait for R1", meta={"waiting_on": "R1"})
    return s.send(sender="reviewer", recipient="lead", kind="review-result", body="GO",
                  meta={"request_id": "R1", "status": "approved"}).id


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


# ------------------------------------------------------------------------------------------- relay result
def test_relay_result_sends_once_and_a_rerun_sends_nothing(lead_store: Store, capsys) -> None:
    result = _scenario(lead_store)
    argv = ["relay", "result", "--from", "lead", "--to", "builder", "--result-id", result, "-m", "merge it"]
    rc, out, _ = _run(argv, capsys)
    assert rc == 0 and "sent" in out and "already" not in out
    rc, out, _ = _run(argv, capsys)
    assert rc == 0 and "already sent" in out and "nothing was sent again" in out
    sent = [m for m in lead_store.valid_messages() if (m.meta or {}).get("relay_of") == result]
    assert len(sent) == 1 and sent[0].recipient == "builder" and sent[0].body == "merge it"


def test_relay_result_none_needed_needs_a_reason_and_closes_only_that_seat(lead_store: Store, capsys) -> None:
    result = _scenario(lead_store)
    argv = ["relay", "result", "--from", "lead", "--to", "builder", "--result-id", result, "--none-needed"]
    rc, _, err = _run(argv, capsys)
    assert rc == 2 and "reason" in err
    rc, out, _ = _run(["relay", "result", "--from", "lead", "--to", "builder", "--result-id", result, "--none-needed",
                       "--reason", "not affected"], capsys)
    assert rc == 0
    assert relay.derive(lead_store.valid_messages(), "lead")[0].state == relay.CLOSED_NOT_NEEDED


def test_relay_result_refuses_what_is_not_a_result_and_conflicting_options(lead_store: Store, capsys) -> None:
    _scenario(lead_store)
    argv = ["relay", "result", "--from", "lead", "--to", "builder", "--result-id", "nope", "-m", "x"]
    rc, _, err = _run(argv, capsys)
    assert rc == 2 and "refused" in err
    rc, _, err = _run(["relay", "result", "--from", "lead", "--to", "builder", "--none-needed", "--unresolved"], capsys)
    assert rc == 2 and "exclude each other" in err
    assert not [m for m in lead_store.valid_messages() if (m.meta or {}).get("relay_of")]


def test_relay_result_refuses_a_different_payload_for_the_same_hand_off(lead_store: Store, capsys) -> None:
    result = _scenario(lead_store)
    base = ["relay", "result", "--from", "lead", "--to", "builder", "--result-id", result]
    assert _run(base + ["-m", "go"], capsys)[0] == 0
    rc, _, err = _run(base + ["-m", "stop"], capsys)
    assert rc == 2 and "different payload" in err


def test_relay_unresolved_after_the_request_left_live_history(lead_store: Store, capsys) -> None:
    lead_store.send(sender="builder", recipient="lead", body="I wait", meta={"waiting_on": "GONE"})
    rc, out, _ = _run(["relay", "waiting", "--for", "builder"], capsys)
    assert rc == 0 and "GONE: unresolved" in out and "no longer in live history" in out
    argv = ["relay", "result", "--from", "lead", "--to", "builder", "--request-id", "GONE", "--unresolved"]
    assert _run(argv, capsys)[0] == 0
    rc, out, _ = _run(["relay", "waiting", "--for", "builder"], capsys)
    assert "GONE: closed-unresolved" in out
    notes = [m for m in lead_store.valid_messages() if (m.meta or {}).get("relay_outcome") == "unresolved"]
    assert len(notes) == 1


def test_relay_waiting_json_and_an_empty_answer(lead_store: Store, capsys) -> None:
    result = _scenario(lead_store)
    rc, out, _ = _run(["relay", "waiting", "--for", "builder", "--json"], capsys)
    data = json.loads(out)
    assert rc == 0 and data[0]["state"] == "owed" and data[0]["result_id"] == result
    rc, out, _ = _run(["relay", "waiting", "--for", "other"], capsys)
    assert rc == 0 and "no declaration" in out


# ------------------------------------------------------------------------------------- the owed-relay notice
def test_the_global_commands_print_the_notice_and_move_the_cursor_as_before(lead_store: Store, capsys) -> None:
    _scenario(lead_store)
    rc, out, _ = _run(["drain", "--for", "lead"], capsys)
    assert rc == 0 and "== OWED RELAY" in out and "builder" in out
    assert lead_store.cursor("lead") != ""                           # drain consumed, as it always does
    rc, out, _ = _run(["recv", "--for", "lead", "--ack"], capsys)
    assert rc == 0 and "== OWED RELAY" in out                       # still owed: shown again, the cursor already past
    rc, out, _ = _run(["recv", "--for", "lead"], capsys)
    assert "== OWED RELAY" in out


def test_json_and_quiet_output_stay_free_of_the_notice(lead_store: Store, capsys) -> None:
    _scenario(lead_store)
    rc, out, _ = _run(["recv", "--for", "lead", "--json"], capsys)
    assert rc == 0 and "OWED RELAY" not in out
    json.loads(out.strip().splitlines()[0]) if out.strip().startswith("{") else None
    rc, out, _ = _run(["recv", "--for", "lead", "--quiet"], capsys)
    assert "OWED RELAY" not in out
    rc, out, _ = _run(["drain", "--for", "lead", "--quiet"], capsys)
    assert "OWED RELAY" not in out


def test_a_seat_that_is_not_a_lead_or_has_nothing_open_sees_no_notice(lead_store: Store, capsys) -> None:
    _scenario(lead_store)
    rc, out, _ = _run(["recv", "--for", "builder"], capsys)
    assert rc == 0 and "OWED RELAY" not in out
    relay.publish_handoff(lead_store, "lead", "builder", result_id=_find_result(lead_store), body="done")
    rc, out, _ = _run(["recv", "--for", "lead"], capsys)
    assert "OWED RELAY" not in out                                   # nothing open any more


def _find_result(s: Store) -> str:
    return next(m.id for m in s.valid_messages() if m.kind == "review-result")


def test_global_and_scoped_wait_print_the_notice_and_keep_their_own_cursor_rules(lead_store: Store, capsys) -> None:
    _scenario(lead_store)
    rc, out, _ = _run(["wait", "--for", "lead", "--to-request", "R1", "--timeout", "3"], capsys)
    assert rc == 0 and "== OWED RELAY" in out
    assert lead_store.thread_seen("lead", "R1") != ""                  # the scoped wait marks the thread seen ...
    assert lead_store.cursor("lead") == ""                             # ... and does NOT advance the global cursor
    rc, out, _ = _run(["wait", "--for", "lead", "--timeout", "3"], capsys)
    assert rc == 0 and "== OWED RELAY" in out
    assert lead_store.cursor("lead") != ""


def test_the_notice_itself_moves_no_cursor_and_no_thread_seen_pointer(lead_store: Store, capsys) -> None:
    _scenario(lead_store)
    lead_store.mark_thread_seen("lead", "R1", _find_result(lead_store))
    cursor, seen, state_dir = lead_store.cursor("lead"), lead_store.thread_seen("lead", "R1"), lead_store.state_dir
    before = {p.name: p.read_bytes() for p in state_dir.iterdir() if p.is_file() and p.name.startswith("lead.")}
    cli._emit_owed_notice(lead_store, "lead", 0)
    out = capsys.readouterr().out
    after = {p.name: p.read_bytes() for p in state_dir.iterdir() if p.is_file() and p.name.startswith("lead.")}
    assert "== OWED RELAY" in out
    assert (lead_store.cursor("lead"), lead_store.thread_seen("lead", "R1")) == (cursor, seen)
    assert before == after


def test_a_failing_notice_never_changes_the_command(lead_store: Store, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    _scenario(lead_store)

    def boom(*a, **k):
        raise RuntimeError("bus unreadable")

    monkeypatch.setattr(relay, "obligations_for", boom)
    rc, out, _ = _run(["drain", "--for", "lead"], capsys)
    assert rc == 0 and "OWED RELAY" not in out and "GO" in out


def test_the_invisible_waiter_has_no_obligation_no_notice_and_no_status_line(lead_store: Store, capsys) -> None:
    lead_store.send(sender="lead", recipient="reviewer", kind="review-request", body="please",
                    meta={"request_id": "R9"})
    lead_store.send(sender="reviewer", recipient="lead", kind="review-result", body="GO",
                    meta={"request_id": "R9", "status": "approved"})
    assert relay.derive(lead_store.valid_messages(), "lead") == []
    rc, out, _ = _run(["drain", "--for", "lead"], capsys)
    assert rc == 0 and "OWED RELAY" not in out
