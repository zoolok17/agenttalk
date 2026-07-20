"""Enforcement canary (task #34): a deterministic, NO-MODEL, NO-NETWORK end-to-end
test of the wrapped-agent loop through the REAL spawn path.

Unlike ``tests/test_wrapper_loop.py`` (which injects a fake python spawner and so
bypasses the real subprocess + reply transport), this module drives real turns via
:func:`agenttalk.wrapper.run.make_drive` with the DEFAULT spawner and a real
stub-CLI subprocess (``tests/support/stub_cli.py``). It validates the full chain:
per-turn prompt -> real child process -> claude stream-json -> bus reply transport
-> owed-action (commit-gate) enforcement.

These tests spawn real Python subprocesses; they are marked ``subprocess`` so they
can be scoped/deselected.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api, run, session
from agenttalk.wrapper.obligations import (
    DETECTION_GRADE,
    DetectionCommitGate,
    PolicySnapshot,
    ResolverState,
)

pytestmark = pytest.mark.subprocess

STUB_CLI = Path(__file__).parent / "support" / "stub_cli.py"


# --------------------------------------------------------------------------- helpers


def _store(tmp_path: Path, agents: list[str] | None = None) -> Store:
    store = Store(tmp_path)
    roster = agents or ["alpha", "beta", "lead"]
    store.init(roster)
    if "lead" in roster:
        store.set_operator_facing("lead")
    return store


def _policy(agent: str = "beta") -> PolicySnapshot:
    return PolicySnapshot.from_mapping(
        {"schema_version": 1, "agents": {agent: {"grade": DETECTION_GRADE}}},
        agent,
    )


def _gate(store: Store, *, agent: str = "beta", fence: str = "wrapper-1") -> DetectionCommitGate:
    store.write_waiting(agent, {
        "mode": "wrapper-loop",
        "wrapper_generation": fence,
        "wait_token": fence,
        "pid": os.getpid(),
    })
    return DetectionCommitGate(store, agent, _policy(agent), fence=fence)


def _question(store: Store, rid: str, *, recipient: str = "beta") -> object:
    return store.send(
        sender="alpha",
        recipient=recipient,
        kind="question",
        body="What is 19 * 21?",
        meta={"request_id": rid},
    )


def _base_argv() -> list[str]:
    return [sys.executable, str(STUB_CLI)]


def _claude_session(store: Store, agent: str = "beta") -> session.SessionState:
    return session.load_session(store, agent, "claude")


def _reply_from(store: Store, agent: str, inbound_id: str) -> list[object]:
    """Every validated bus message ``agent`` sent in reply to ``inbound_id``."""
    return [
        m for m in store.valid_messages()
        if m.sender == agent and (m.meta or {}).get("in_reply_to") == inbound_id
    ]


# --------------------------------------------------------------------------- reply_ok


def test_reply_ok_satisfies_owed_action_and_lands_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compliant turn performs the owed-action transport: the reply lands on the
    bus with the dispatched operation-nonce and the commit gate finalizes SATISFIED
    (no dead-letter)."""
    monkeypatch.setenv("AGENTTALK_STUB_SCENARIO", "reply_ok")
    store = _store(tmp_path)
    inbound = _question(store, "q-ok")
    gate = _gate(store)
    record = recv_api.next_record(store, "beta")
    assert record is not None

    drive = run.make_drive(
        store, "beta", "claude", _claude_session(store), _base_argv(),
        render=False,
    )
    turns = loop.run_loop(
        store, "beta", drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _d: None,
        max_turns=1,
        max_polls=6,
    )

    assert turns == 1
    # The obligation is satisfied and the reply actually landed on the bus.
    assert gate.resolve(record).state == ResolverState.SATISFIED
    replies = _reply_from(store, "beta", inbound.id)
    assert len(replies) == 1
    reply = replies[0]
    assert "399" in reply.body
    assert (reply.meta or {}).get("operation_nonce")  # dispatched transport, not a plain reply
    # No dead-letter / delivery-failure sink for this agent.
    assert store.dead_lettered_count("beta") == 0


# ----------------------------------------------------------------- compute_no_reply


def test_compute_no_reply_fires_owed_action_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that computes but never emits the owed reply is NOT silently marked
    done: the commit gate re-dispatches up to the paid cap, then records a
    DELIVERY_FAILED / owed-action enforcement outcome. No reply ever lands."""
    monkeypatch.setenv("AGENTTALK_STUB_SCENARIO", "compute_no_reply")
    store = _store(tmp_path)
    inbound = _question(store, "q-noreply")
    gate = _gate(store)
    record = recv_api.next_record(store, "beta")
    assert record is not None

    drive = run.make_drive(
        store, "beta", "claude", _claude_session(store), _base_argv(),
        render=False,
    )
    turns = loop.run_loop(
        store, "beta", drive,
        commit_gate=gate,
        clock=lambda: 0.0,
        sleep=lambda _d: None,
        max_turns=1,
        max_polls=10,
    )

    # The obligation never succeeded and was never satisfied.
    assert turns == 0
    assert _reply_from(store, "beta", inbound.id) == []
    # Enforcement fired: the obligation terminalized as delivery-exhausted (the
    # "agent computed but did not emit the owed reply" path), NOT as SATISFIED.
    resolution = gate.resolve(record)
    assert resolution.state == ResolverState.DELIVERY_EXHAUSTED
    # The ledger recorded the paid dispatches + the delivery failure.
    ledger = json.loads(gate.path.read_text(encoding="utf-8"))
    transitions = [row["transition"] for row in ledger["transitions"]]
    assert "DELIVERY_FAILED" in transitions
    assert transitions.count("DISPATCH_ATTEMPT_STARTED") == 2  # MAX_PAID_DISPATCHES_TOTAL


# ------------------------------------------------------------------- resume_missing


def test_resume_missing_self_heals_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX (task #34): a claude ``--resume`` that hits 'No conversation found with
    session ID: ...'. The diagnostic arrives on a NON-JSON line the stream-json
    adapter discards, but the wrapper's captured raw child-output tail retains it and
    now feeds it into the resume-failure attributability decision -> the failure is
    session-attributable, so the EXISTING fresh-session self-heal fires: after two
    broken --resume attempts the session id is re-minted, the next attempt starts a
    FRESH ``--session-id`` turn, and the in-flight message is RECOVERED (its reply
    lands) instead of being retried to the dead-letter ceiling."""
    monkeypatch.setenv("AGENTTALK_STUB_SCENARIO", "resume_missing")
    store = _store(tmp_path, ["alpha", "beta"])
    first = _question(store, "q-first")
    second = _question(store, "q-second")

    state = _claude_session(store)
    original_session_id = state.claude_session_id
    drive = run.make_drive(
        store, "beta", "claude", state, _base_argv(),
        render=False,
        persist=lambda st: session.save_session(store, "beta", st),
    )

    calls: list[str] = []

    def counting_drive(rec: dict) -> object:
        calls.append(str(rec.get("id")))
        return drive(rec)

    dead: list[dict] = []
    k_escalate = 3
    turns = loop.run_loop(
        store, "beta", counting_drive,
        clock=lambda: 0.0,
        sleep=lambda _d: None,
        k_escalate=k_escalate,
        on_dead_letter=dead.append,
        max_polls=16,
    )

    # Turn 1 was a FRESH --session-id turn: it succeeded, its reply landed, committed.
    assert len(_reply_from(store, "beta", first.id)) == 1
    assert store.cursor("beta") >= first.id

    # The second message hit --resume and failed, but the wrapper SELF-HEALED: two
    # broken --resume attempts re-minted the session, then a fresh --session-id turn
    # recovered the message. Its reply landed; it was NOT dead-lettered.
    assert dead == []
    assert store.dead_lettered_count("beta") == 0
    assert len(_reply_from(store, "beta", second.id)) == 1
    assert store.cursor("beta") >= second.id
    # BOTH messages committed as successful turns (fresh-first + self-healed-second).
    assert turns == 2

    # Real subprocess spawns: 1 (fresh, first msg) + k_escalate (2 broken --resume for
    # the second, then 1 fresh --session-id that recovers it).
    assert calls == [first.id] + [second.id] * k_escalate

    # SELF-HEAL evidence: the session id was re-minted (a NEW uuid), the fresh session
    # succeeded so resume is re-armed, and the continuity-loss audit fields were set.
    assert state.claude_session_id != original_session_id
    assert state.resume_available is True
    assert state.resume_unavailable_reason == ""
    assert "resume_unavailable" in state.continuity_lost_reason
    assert state.fresh_session_success_reason == "fresh_session_success"


def test_raw_tail_scan_excludes_json_wrapped_model_content_no_spoof() -> None:
    """PR #51 codex-agenttalk-reviewer-1 finding: model output must not be able to
    spoof a session-failure. In stream-json mode model text is JSON-wrapped, so
    _child_output_tail_text scans NON-JSON lines only; a bare CLI diagnostic line is
    still captured. Proven both ways here."""
    # (1) model merely QUOTES the diagnostic inside a JSON assistant event -> excluded,
    # so an otherwise-ambiguous failure is NOT spuriously session-attributable.
    spoof_tail = {"lines": [
        {"stream": "stdout", "text": json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text",
                        "text": "No conversation found with session ID: abc123 — here is what that error means"}]},
        })},
        {"stream": "stdout", "text": json.dumps({"type": "result", "subtype": "success", "is_error": False})},
    ]}
    spoof_scanned = run._child_output_tail_text(spoof_tail)
    assert "no conversation found" not in spoof_scanned.casefold(), spoof_scanned
    assert not session.resume_failure_is_session_attributable(
        "ambiguous_or_unknown", "some ambiguous summary", raw_tail=spoof_scanned)

    # (2) a REAL bare (non-JSON) CLI diagnostic line IS captured and attributable.
    err_result = json.dumps(
        {"type": "result", "subtype": "error_during_execution", "is_error": True})
    real_tail = {"lines": [
        {"stream": "stdout", "text": "No conversation found with session ID: 26c40e8a-c8ef-4c9b"},
        {"stream": "stdout", "text": err_result},
    ]}
    real_scanned = run._child_output_tail_text(real_tail)
    assert "no conversation found with session id" in real_scanned.casefold()
    assert session.resume_failure_is_session_attributable(
        "ambiguous_or_unknown", "error_during_execution", raw_tail=real_scanned)


def test_truncation_spoof_blocked_by_no_model_output_signal() -> None:
    """codex-agenttalk-reviewer-1 re-review (PR #51): the non-JSON filter is not
    spoof-proof on its own because the child-output tail is byte/line-BOUNDED. A model
    JSON assistant line TRUNCATED at the tail boundary becomes invalid JSON, slips past
    the non-JSON filter, and - if it quotes the diagnostic - spoofs a session failure on
    a turn where the model ACTUALLY RAN. The content-independent ``produced_model_output``
    guard closes it structurally: a turn that produced model output is never
    session-attributable from tail text."""
    from agenttalk.redaction import normalize_child_output_tail

    # A long model assistant line (valid JSON on the live stream) whose stored tail copy
    # gets LEFT-TRIMMED past the tail byte budget -> an invalid-JSON fragment that still
    # carries "no conversation found with session ID" in its surviving suffix.
    payload = ("x" * 5000) + " No conversation found with session ID: 26c40e8a-c8ef"
    model_line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": payload}]}})
    tail = normalize_child_output_tail({"lines": [{"stream": "stdout", "text": model_line}]})
    assert tail["truncated"] is True
    scanned = run._child_output_tail_text(tail)
    # The fragment DID slip past the non-JSON filter (this is the raw vulnerability) ...
    assert "no conversation found with session id" in scanned.casefold()

    # ... but a turn that PRODUCED MODEL OUTPUT (num_turns>=1) is NOT session-attributable,
    # regardless of the spoofing tail -> no spurious fresh-session reset.
    assert not session.resume_failure_is_session_attributable(
        "ambiguous_or_unknown", "partial stream: started, never completed",
        raw_tail=scanned, produced_model_output=True)

    # A REAL missing-session failure (num_turns==0, no model output, bare diagnostic) still
    # self-heals: the same tail text IS attributable when no model output was produced.
    assert session.resume_failure_is_session_attributable(
        "ambiguous_or_unknown", "error_during_execution",
        raw_tail=scanned, produced_model_output=False)
