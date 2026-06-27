"""The wrapper-owned listen loop (design C, Phase B): the long-running supervised
process. IDLE on the bus (write_waiting + write_heartbeat + adaptive backoff) until
an inbound message; drive the CLI for ONE turn; COMMIT (advance the cursor) AFTER
the turn (at-least-once - a crash mid-turn re-delivers on restart); return to idle.

The supervisor stays dumb (heartbeat/backoff/kill). The heartbeat stays fresh both
IDLE (here) and DURING a turn (the per-turn engine stamps on progress events), so a
healthy blocked-or-working wrapped agent never looks stale to the supervisor
(Codex flagged this as required).

Pure orchestration: the per-turn ``drive`` callback, the clock, and sleep are
injected, so the loop is unit-testable with a fixture Store and NO real CLI.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import recv_api


def is_terminal_control(record: dict) -> bool:
    """A scoped record on a rescinded/closed thread is TERMINAL control state
    (Codex carry-forward): consume it, NEVER drive a turn on it as ordinary work."""
    sc = record.get("scoped")
    return bool(sc and (sc.get("closed") or sc.get("superseded")))


LOOP_CONTROL_KINDS = ("release", "end")


def _meta_true(meta: dict, key: str) -> bool:
    v = meta.get(key)
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def classify_loop_control(store, record: dict) -> str:
    """The shared loop-exit classifier (stand-down authority, 0.39.0). REPLACES the
    old is_stop_signal so a casual/unmarked release/end can no longer stand a
    listener down. Returns one of:

      * ``"stop"``            - a VALID loop-exit: kind in {release, end}, sender is
        the authorized relay (operator_facing / sole lead), exactly one authority
        mode (``human``|``emergency``), and a non-empty ``authority_reason``.
        Commit + exit.
      * ``"invalid_control"`` - a release/end whose sender/marker/reason is missing
        or invalid (incl. an UNMARKED end, the old bypass). The caller COMMITS it
        (so it never redelivers and is never fed to the model) + reports it +
        KEEPS LISTENING.
      * ``"ordinary"``        - everything else (prose, notes, sign-offs, work).
        Handle normally; it NEVER exits the loop.

    Authority is an auditable trusted-team assertion (the lead relays a human's
    decision), NOT cryptographic proof a human spoke - see SECURITY.md."""
    kind = record.get("kind")
    if kind not in LOOP_CONTROL_KINDS:
        return "ordinary"
    if not store.loop_exit_relay_authorized(record.get("from")):
        return "invalid_control"
    meta = record.get("meta") or {}
    reason = meta.get("authority_reason")
    if not (isinstance(reason, str) and reason.strip()):
        return "invalid_control"
    # EXACTLY ONE authority mode, with the full marker set for that mode and NO
    # marker from the other mode - so a raw bus message with mixed/ambiguous markers
    # (which bypasses the CLI) is invalid_control, preserving the human-vs-emergency
    # audit distinction (reviewer findings).
    authority = meta.get("release_authority")
    human_ok = (authority == "human"
                and _meta_true(meta, "operator_decision")
                and not _meta_true(meta, "emergency"))
    emergency_ok = (authority == "emergency"
                    and _meta_true(meta, "emergency")
                    and _meta_true(meta, "operator_report_required")
                    and not _meta_true(meta, "operator_decision"))
    if human_ok != emergency_ok:   # XOR: exactly one valid mode
        return "stop"
    return "invalid_control"


def run_loop(store, agent: str, drive: Callable[[dict], bool], *,
             idle_interval: float = 0.3, max_idle_interval: float = 2.0,
             heartbeat_interval: float = 10.0,
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             max_turns: int | None = None, max_polls: int | None = None,
             only_request_id: str | None = None) -> int:
    """Run the wrapper listen loop. ``drive(record)`` handles ONE turn (injected).
    Returns the number of turns driven. ``max_turns`` / ``max_polls`` bound the
    loop for tests (both None = run forever)."""
    store.write_waiting(agent, {"agent": agent, "mode": "wrapper-loop"})
    turns = 0
    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    while True:
        if max_polls is not None and polls >= max_polls:
            return turns
        polls += 1
        record = recv_api.next_record(store, agent)
        now = clock()
        if record is None:
            # IDLE: keep the heartbeat fresh, then back off while the bus is quiet.
            if last_hb is None or (now - last_hb) >= heartbeat_interval:
                store.write_heartbeat(agent)
                last_hb = now
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        cur_sleep = idle_interval                       # reset backoff on activity
        if is_terminal_control(record):
            recv_api.commit(store, agent, record)       # consume + skip (control)
            continue
        control = classify_loop_control(store, record)
        if control == "stop":
            # a VALID, authorized, human/emergency-marked release/end: the wrapper
            # owns loop-exit (the model is a pure handler). Consume it + STAND DOWN.
            recv_api.commit(store, agent, record)
            return turns
        if control == "invalid_control":
            # an unauthorized / unmarked / reasonless release|end (incl. the old
            # unmarked-end bypass): COMMIT it so it never redelivers and is never
            # driven into the model, then KEEP LISTENING (idle stays listening).
            recv_api.commit(store, agent, record)
            continue
        if only_request_id and record.get("request_id") != only_request_id:
            # One-shot ephemeral reviewers are scoped to the launch request. Leave
            # unrelated content unread rather than spending the single turn on it.
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        # Drive ONE turn. Commit the inbound message ONLY when the turn SUCCEEDS.
        # drive() stamps the heartbeat itself on a clean completed turn (and NOT on
        # a failed turn), so the loop does not stamp here - a failed turn leaves no
        # fresh heartbeat (reviewer-1 gate).
        if drive(record):
            recv_api.commit(store, agent, record)
            last_hb = clock()                           # drive already stamped on success
            fail_sleep = idle_interval                  # reset failure backoff
            turns += 1
            if max_turns is not None and turns >= max_turns:
                return turns
        else:
            # FAILED turn: do NOT commit (re-delivers, at-least-once) and do NOT
            # stamp the heartbeat - so a persistent no-progress failure goes stale
            # and the supervisor restarts us. BACK OFF before retrying so we never
            # hammer the CLI in a hot spawn loop (a process storm) before that.
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
