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
             only_request_id: str | None = None,
             max_wall: float | None = None) -> int:
    """Run the wrapper listen loop. ``drive(record)`` handles ONE turn (injected).
    Returns the number of turns driven. ``max_turns`` / ``max_polls`` / ``max_wall``
    bound the loop (all None = run forever; tests inject clock/sleep).

    Two modes:
      * CONTINUOUS (``only_request_id is None``): the long-running supervised loop.
        Reads the GLOBAL inbox, honors loop-control (release/end via
        classify_loop_control), drives one turn per inbound message.
      * ONE-SHOT (``only_request_id`` set): an ephemeral reviewer scoped to ONE
        launch request. Uses SCOPED receive so an unrelated head-of-inbox message
        cannot starve it (it stays unread for a later global sync); keeps the
        heartbeat fresh while waiting; exits on the scoped thread going
        closed/superseded; and is bounded by ``max_wall``/``max_polls`` so it exits
        (turns==0 -> the caller maps a nonzero process exit) instead of spinning
        forever if its request never arrives.

    The ``.waiting`` marker is ALWAYS cleared on exit (try/finally) so a normal
    stand-down / one-shot completion does not leave a stale waiting marker."""
    store.write_waiting(agent, {"agent": agent, "mode": "wrapper-loop"})
    try:
        if only_request_id is not None:
            return _run_one_shot(
                store, agent, drive, rid=only_request_id,
                idle_interval=idle_interval, max_idle_interval=max_idle_interval,
                heartbeat_interval=heartbeat_interval, clock=clock, sleep=sleep,
                max_turns=max_turns, max_polls=max_polls, max_wall=max_wall)
        return _run_continuous(
            store, agent, drive, idle_interval=idle_interval,
            max_idle_interval=max_idle_interval, heartbeat_interval=heartbeat_interval,
            clock=clock, sleep=sleep, max_turns=max_turns, max_polls=max_polls,
            max_wall=max_wall)
    finally:
        store.clear_waiting(agent)


def _run_continuous(store, agent: str, drive: Callable[[dict], bool], *,
                    idle_interval: float, max_idle_interval: float,
                    heartbeat_interval: float, clock: Callable[[], float],
                    sleep: Callable[[float], None], max_turns: int | None,
                    max_polls: int | None, max_wall: float | None) -> int:
    turns = 0
    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    start = clock()
    while True:
        if max_polls is not None and polls >= max_polls:
            return turns
        if max_wall is not None and (clock() - start) >= max_wall:
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


def _run_one_shot(store, agent: str, drive: Callable[[dict], bool], *, rid: str,
                  idle_interval: float, max_idle_interval: float,
                  heartbeat_interval: float, clock: Callable[[], float],
                  sleep: Callable[[float], None], max_turns: int | None,
                  max_polls: int | None, max_wall: float | None) -> int:
    """SCOPED one-shot loop for an ephemeral reviewer (see run_loop). Receives only
    messages on ``rid`` so unrelated traffic neither starves it nor is consumed from
    the global inbox; bounded so it always terminates."""
    turns = 0
    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    start = clock()
    while True:
        if max_polls is not None and polls >= max_polls:
            return turns                # bound hit: caller maps turns==0 -> nonzero
        if max_wall is not None and (clock() - start) >= max_wall:
            return turns                # wall timeout: never spin forever
        polls += 1
        env = recv_api.poll(store, agent, scoped_request_id=rid)
        record = env.get("record")
        scoped = env.get("scoped") or {}
        now = clock()
        if record is None:
            # No scoped message pending. If the thread is terminal (rescinded/closed
            # or superseded) the request is dead -> stop waiting (turns==0 -> nonzero).
            if scoped.get("closed") or scoped.get("superseded"):
                return turns
            # Otherwise keep the heartbeat fresh so a waiting one-shot never looks
            # stale to the supervisor, then back off. An unrelated head-of-inbox
            # message is NOT returned here (scoped floor) and is NOT committed, so it
            # cannot starve us and stays unread for a later global sync.
            if last_hb is None or (now - last_hb) >= heartbeat_interval:
                store.write_heartbeat(agent)
                last_hb = now
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        cur_sleep = idle_interval
        if is_terminal_control(record):
            # scoped record on a rescinded/closed thread: consume + stop (terminal).
            recv_api.commit(store, agent, record)
            return turns
        # A scoped record carries ``rid`` by construction - it is the work this
        # ephemeral reviewer was spawned for. Commit only on a successful turn.
        if drive(record):
            recv_api.commit(store, agent, record)       # SCOPED: thread-seen only
            last_hb = clock()
            fail_sleep = idle_interval
            turns += 1
            if max_turns is not None and turns >= max_turns:
                return turns
        else:
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
