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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import recv_api

# Three-way failure taxonomy (dead-letter, design q-bfdb1bbc3638). A FAILED drive is
# classified so the loop can tell a POISON message (auto dead-letter at K_poison) from
# a transient INFRA outage (never auto-dead-letter - retry under backoff; escalate at
# K_escalate) from an AMBIGUOUS failure (escalate AND dead-letter at K_escalate, the
# misclassified-poison escape hatch).
CLASS_POISON = "poison_eligible"
CLASS_INFRA = "known_global_infra"
CLASS_AMBIGUOUS = "ambiguous_or_unknown"


@dataclass(frozen=True)
class DriveOutcome:
    """The normalized result of one ``drive(record)`` turn. ``ok`` True = the turn
    completed cleanly; otherwise ``failure_class`` is one of the CLASS_* constants and
    ``summary`` is a short diagnostic. A bare ``bool`` returned by a drive is normalized
    (:func:`_as_outcome`): True = ok, False = a poison-eligible failure (the default, so a
    deterministic always-False drive dead-letters at K_poison - the v0.30.0 regression)."""
    ok: bool
    failure_class: str | None = None
    summary: str = ""


def _as_outcome(ret: object) -> DriveOutcome:
    if isinstance(ret, DriveOutcome):
        return ret
    if ret:
        return DriveOutcome(ok=True)
    return DriveOutcome(ok=False, failure_class=CLASS_POISON, summary="drive returned False")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_int(value: object) -> int:
    """Coerce a ledger counter to int, degrading to 0 on null/non-numeric (a hand-edited
    or forward-incompatible ledger VALUE must err LOW, never crash the loop - mirrors the
    degrade-to-empty file read)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _infra_dominant(rec: dict) -> bool:
    """True if this message's failure history is DOMINANTLY infra (codex ruling, v1 predicate):
    infra_failures > poison_eligible_failures + ambiguous_failures. At the K_escalate ceiling a
    dominantly-infra record must escalate + keep retrying, NOT take the ambiguous last-resort
    disposal - else a healthy message stale-killed during a sustained OUTAGE (whose crash flips
    last_failure_class to ambiguous) would be silently dead-lettered, breaking infra-never-DL."""
    rec = rec or {}
    return (_safe_int(rec.get("infra_failures"))
            > _safe_int(rec.get("poison_eligible_failures"))
            + _safe_int(rec.get("ambiguous_failures")))


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


def run_loop(store, agent: str, drive: Callable[[dict], object], *,
             idle_interval: float = 0.3, max_idle_interval: float = 2.0,
             heartbeat_interval: float = 10.0,
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             max_turns: int | None = None, max_polls: int | None = None,
             only_request_id: str | None = None,
             max_wall: float | None = None,
             k_poison: int = 3, k_escalate: int = 20,
             on_dead_letter: Callable[[dict], None] | None = None,
             on_escalate: Callable[[dict], None] | None = None,
             heartbeat: Callable[[], None] | None = None,
             pre_commit: Callable[[], None] | None = None,
             manage_waiting: bool = True,
             now_iso: Callable[[], str] = _iso_now) -> int:
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
    stand-down / one-shot completion does not leave a stale waiting marker - UNLESS
    ``manage_waiting`` is False (the managed lead-loop controller: its LEASE owns the
    ``.waiting`` mirror, so the loop must neither write the generic wrapper-loop marker
    at start - which would clobber the lease mirror - nor clear it on exit; the
    controller's lease release handles the mirror, WP2).

    ``heartbeat`` overrides the per-idle stamp (default ``store.write_heartbeat``);
    the managed lead-loop passes a combined ``write_heartbeat`` + ``renew lease`` so
    the lease stays fresh on every idle stamp (WP2 condition 4)."""
    stamp = heartbeat if heartbeat is not None else (lambda: store.write_heartbeat(agent))
    if manage_waiting:
        store.write_waiting(agent, {"agent": agent, "mode": "wrapper-loop"})
    try:
        if only_request_id is not None:
            return _run_one_shot(
                store, agent, drive, rid=only_request_id,
                idle_interval=idle_interval, max_idle_interval=max_idle_interval,
                heartbeat_interval=heartbeat_interval, clock=clock, sleep=sleep,
                max_turns=max_turns, max_polls=max_polls, max_wall=max_wall,
                stamp=stamp)
        return _run_continuous(
            store, agent, drive, idle_interval=idle_interval,
            max_idle_interval=max_idle_interval, heartbeat_interval=heartbeat_interval,
            clock=clock, sleep=sleep, max_turns=max_turns, max_polls=max_polls,
            max_wall=max_wall, k_poison=k_poison, k_escalate=k_escalate,
            on_dead_letter=on_dead_letter, on_escalate=on_escalate, stamp=stamp,
            pre_commit=pre_commit, now_iso=now_iso)
    finally:
        if manage_waiting:
            store.clear_waiting(agent)


def _run_continuous(store, agent: str, drive: Callable[[dict], object], *,
                    idle_interval: float, max_idle_interval: float,
                    heartbeat_interval: float, clock: Callable[[], float],
                    sleep: Callable[[float], None], max_turns: int | None,
                    max_polls: int | None, max_wall: float | None,
                    k_poison: int, k_escalate: int,
                    on_dead_letter: Callable[[dict], None] | None,
                    on_escalate: Callable[[dict], None] | None,
                    stamp: Callable[[], None],
                    pre_commit: Callable[[], None] | None,
                    now_iso: Callable[[], str]) -> int:
    turns = 0

    def _guard_advance() -> None:
        # Called at EVERY cursor-advancing boundary (commit on success/control/invalid,
        # AND dead-letter dispose) BEFORE the advance. The managed lead-loop passes a
        # renew-or-raise ownership check here: a controller that has LOST the lease must
        # NOT advance the cursor / consume a record while unguarded (codex WP2 consume-
        # boundary blocker). A raise propagates out of run_loop and the controller exits
        # lease-lost (no marker -> relaunch). Default (no lead-loop): a no-op.
        if pre_commit is not None:
            pre_commit()

    def _commit(rec: dict) -> None:
        _guard_advance()
        recv_api.commit(store, agent, rec)
    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    start = clock()

    def _info(record: dict, rec: dict, failure_class: str) -> dict:
        return {"agent": agent, "msg_id": record.get("id"), "from": record.get("from"),
                "subject": record.get("subject"), "kind": record.get("kind"),
                "request_id": record.get("request_id"),
                "attempts": _safe_int((rec or {}).get("attempts_started")),  # degrade-low
                "failure_class": failure_class}

    def _dispose(record: dict, *, failure_class: str, reason: str | None) -> None:
        """Dead-letter the head record (store advances the cursor past it), then stamp
        progress: DL is PROGRESS, not a failed turn, so the heartbeat goes FRESH and
        the failure backoff resets - the supervisor sees progress + never restarts."""
        nonlocal last_hb, fail_sleep
        rec = store.attempt_record(agent, record.get("id")) or {}
        _guard_advance()                      # dead-letter ADVANCES the cursor - verify
        #                                       lease ownership first (lead-loop), else a
        #                                       lost-lease controller could dispose unguarded
        store.dead_letter(agent, record, reason=reason, failure_class=failure_class,
                          at=now_iso())
        stamp()                               # DL is progress (bypasses drive's stamp)
        last_hb = clock()
        fail_sleep = idle_interval
        if on_dead_letter is not None:
            # The disposal already SUCCEEDED (bytes in the sink, cursor advanced) and progress
            # is stamped above; a raising notice callback must not crash the loop and leave it
            # stuck - doctor surfaces the sink regardless (no-silent-disposal). Swallow + return.
            try:
                on_dead_letter(_info(record, rec, failure_class))
            except Exception:  # noqa: BLE001 - a notification must never crash the loop
                return

    def _escalate_once(record: dict, failure_class: str) -> None:
        """Class-agnostic K_escalate backstop: emit an operator escalation, DEDUPED on
        successful ROUTING - not merely on having attempted. An UNROUTED notice (no
        liaison/lead resolved) is retried on subsequent polls so it fires once the operator
        configures a target (codex P2); meanwhile doctor surfaces it LOUD. A routed notice
        latches escalation_routed and never re-sends."""
        rec = store.attempt_record(agent, record.get("id")) or {}
        if rec.get("escalation_routed"):
            return                                    # already routed -> done (deduped)
        try:
            routed = bool(on_escalate(_info(record, rec, failure_class))) \
                if on_escalate is not None else False
        except Exception:  # noqa: BLE001 - a notification must never crash the loop
            routed = False                            # unrouted -> retried + doctor LOUD
        store.mark_attempt_escalated(agent, record.get("id"), routed=routed)

    while True:
        if max_polls is not None and polls >= max_polls:
            return turns
        if max_wall is not None and (clock() - start) >= max_wall:
            return turns
        polls += 1
        record = recv_api.next_record(store, agent)
        now = clock()
        if record is None:
            # IDLE: keep the heartbeat fresh (+ renew the lease for the managed
            # lead-loop, via the injected stamp), then back off while the bus is quiet.
            if last_hb is None or (now - last_hb) >= heartbeat_interval:
                stamp()
                last_hb = now
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        cur_sleep = idle_interval                       # reset backoff on activity
        if is_terminal_control(record):
            _commit(record)                             # consume + skip (control)
            continue
        control = classify_loop_control(store, record)
        if control == "stop":
            # a VALID, authorized, human/emergency-marked release/end: the wrapper
            # owns loop-exit (the model is a pure handler). Consume it + STAND DOWN.
            _commit(record)
            return turns
        if control == "invalid_control":
            # an unauthorized / unmarked / reasonless release|end (incl. the old
            # unmarked-end bypass): COMMIT it so it never redelivers and is never
            # driven into the model, then KEEP LISTENING (idle stays listening).
            _commit(record)
            continue

        # ---- dead-letter: bound the at-least-once retry of a POISON head message ----
        head_id = record.get("id")
        # A stale in_progress on this head means the process crashed mid-turn last run ->
        # reconcile it as an AMBIGUOUS crash_mid_turn failure (unobserved cause, NOT poison),
        # which also RESETS the consecutive poison run (codex ruling). The write-ahead
        # attempts_started already counted the crashed attempt toward the K_escalate ceiling.
        store.reconcile_crash_in_progress(agent, head_id, at=now_iso())
        rec = store.attempt_record(agent, head_id) or {}
        # AUTO-DISPOSE WITHOUT DRIVE if a cap is already reached on entry (covers the
        # relaunch/crash-accumulation path - test #3).
        if k_poison > 0 and _safe_int(rec.get("poison_eligible_failures")) >= k_poison:
            _dispose(record, failure_class=CLASS_POISON,
                     reason=rec.get("last_failure_summary"))
            continue
        if k_escalate > 0 and _safe_int(rec.get("attempts_started")) >= k_escalate:
            last_class = rec.get("last_failure_class") or CLASS_AMBIGUOUS
            _escalate_once(record, last_class)
            if last_class != CLASS_INFRA and not _infra_dominant(rec):
                # ambiguous/unknown at the ceiling -> last-resort dispose (the
                # misclassified-poison escape hatch); a relaunch re-disposes here.
                _dispose(record, failure_class=last_class,
                         reason=rec.get("last_failure_summary"))
                continue
            # known infra OR a dominantly-infra history (codex ruling): escalated, but NEVER
            # auto-dead-letter and NEVER freeze - fall through to drive AGAIN (a real outage,
            # incl. a stale-kill mid-outage, must keep retrying until it clears).

        # WRITE-AHEAD: count + mark in_progress BEFORE drive() so a crash mid-turn
        # still costs a durable attempt on relaunch. EXACTLY one attempt per drive().
        store.record_attempt_start(agent, record, attempt_id=uuid.uuid4().hex[:12],
                                   at=now_iso())
        outcome = _as_outcome(drive(record))
        if outcome.ok:
            _commit(record)                             # guards lease ownership first
            store.clear_attempt(agent, head_id)
            store.gc_attempts_below(agent, store.cursor(agent))
            last_hb = clock()                           # drive already stamped on success
            fail_sleep = idle_interval                  # reset failure backoff
            turns += 1
            if max_turns is not None and turns >= max_turns:
                return turns
            continue
        # FAILED turn: record the classified failure (clears in_progress), then decide.
        store.record_attempt_result(agent, head_id, failure_class=outcome.failure_class,
                                    summary=outcome.summary, at=now_iso())
        rec = store.attempt_record(agent, head_id) or {}
        if (k_poison > 0 and outcome.failure_class == CLASS_POISON
                and _safe_int(rec.get("poison_eligible_failures")) >= k_poison):
            _dispose(record, failure_class=CLASS_POISON, reason=outcome.summary)
            continue
        if k_escalate > 0 and _safe_int(rec.get("attempts_started")) >= k_escalate:
            _escalate_once(record, outcome.failure_class)
            # dispose at the ceiling ONLY when not infra AND the history is not dominantly-infra
            # (codex ruling: a dominantly-infra ledger keeps retrying through the outage). rec was
            # re-read AFTER record_attempt_result above, so the predicate counts the current result.
            if outcome.failure_class != CLASS_INFRA and not _infra_dominant(rec):
                _dispose(record, failure_class=outcome.failure_class,
                         reason=outcome.summary)
                continue
        # Below the caps (or known-infra at the backstop): do NOT commit (re-delivers,
        # at-least-once) and do NOT stamp the heartbeat - so a persistent no-progress
        # failure goes stale and the supervisor restarts us. BACK OFF before retrying.
        sleep(fail_sleep)
        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)


def _run_one_shot(store, agent: str, drive: Callable[[dict], bool], *, rid: str,
                  idle_interval: float, max_idle_interval: float,
                  heartbeat_interval: float, clock: Callable[[], float],
                  sleep: Callable[[float], None], max_turns: int | None,
                  max_polls: int | None, max_wall: float | None,
                  stamp: Callable[[], None] | None = None) -> int:
    """SCOPED one-shot loop for an ephemeral reviewer (see run_loop). Receives only
    messages on ``rid`` so unrelated traffic neither starves it nor is consumed from
    the global inbox; bounded so it always terminates."""
    turns = 0
    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    start = clock()
    _stamp = stamp if stamp is not None else (lambda: store.write_heartbeat(agent))
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
                _stamp()
                last_hb = now
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        cur_sleep = idle_interval
        if is_terminal_control(record):
            # scoped record on a rescinded/closed thread: consume + stop (terminal).
            recv_api.commit(store, agent, record)
            return turns
        # A scoped record carries ``rid`` by construction - it is the work this ephemeral
        # reviewer was spawned for. Normalize ONCE, then commit/thread-seen/turns++ ONLY when
        # ok - drive() returns a DriveOutcome (frozen dataclass, no __bool__ -> always truthy),
        # so a bare ``if drive(record):`` would treat a FAILED turn as success and mark the
        # scoped request seen (lead 4th-verify P1 #1; mirrors _run_continuous's _as_outcome).
        outcome = _as_outcome(drive(record))
        if outcome.ok:
            recv_api.commit(store, agent, record)       # SCOPED: thread-seen only
            last_hb = clock()
            fail_sleep = idle_interval
            turns += 1
            if max_turns is not None and turns >= max_turns:
                return turns
        else:
            # FAILED: do NOT commit (the scoped request stays unseen -> redelivers/pending on
            # relaunch), back off, and let the bound exit nonzero at the caller.
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
