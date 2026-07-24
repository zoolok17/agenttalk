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

import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import recv_api

# Failure taxonomy (dead-letter, design q-bfdb1bbc3638). A FAILED drive is
# classified so the loop can tell a POISON message (auto dead-letter at K_poison) from
# a transient INFRA outage (never auto-dead-letter - retry under backoff; escalate at
# K_escalate) from an AMBIGUOUS failure (escalate AND dead-letter at K_escalate, the
# misclassified-poison escape hatch) from CONFIG_BLOCKED (deterministic local exec/config
# denial: escalate once, park the head, never commit/dead-letter/retry-storm).
CLASS_POISON = "poison_eligible"
CLASS_INFRA = "known_global_infra"
CLASS_AMBIGUOUS = "ambiguous_or_unknown"
CLASS_CONFIG_BLOCKED = "config_blocked"
CLASS_INFRA_RETRY_EXHAUSTED = "infra_retry_exhausted"
# A TRANSIENT, operator-resolvable gateway HOLD surfaced at mint time (a held/unresolved
# OVH ledger, a clock rollback awaiting reconcile - any LedgerHold): no child spawned, no
# spend. Distinct from CONFIG_BLOCKED (a DURABLE local denial that sticky-parks and never
# re-drives) and from INFRA (which dead-letters at the infra-exhaustion backstop and withholds
# the heartbeat). A held head is parked WITHOUT persisting an attempt (no counter climbs -> no
# ceiling trips -> NEVER dead-lettered) yet re-drives every poll, so the worker self-heals the
# instant the operator clears the hold. See _hold_park + the failed-turn branch below (#62).
CLASS_GATEWAY_HELD = "gateway_held"


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
    child_output_tail: dict | None = None
    bus_action_attempted: bool = False
    bus_action_infra: bool = False
    bus_action_rejected: bool = False


def _as_outcome(ret: object) -> DriveOutcome:
    if isinstance(ret, DriveOutcome):
        return ret
    if ret:
        return DriveOutcome(ok=True)
    return DriveOutcome(ok=False, failure_class=CLASS_POISON, summary="drive returned False")


@dataclass(frozen=True)
class CadenceResult:
    """The result of consulting the proactive CADENCE hook on one IDLE poll (WP3).

    ``ran`` False  = the hook was a no-op this poll (no lead-loop, or a sweep was not
                     yet due) - the loop proceeds with its normal idle heartbeat.
    ``ran`` True   = a sweep ran (the interval elapsed). ``ok`` False = the sweep FAILED
                     (a controller-HEALTH failure, NOT message poison): the hook OWNS its
                     own backoff / escalation and the loop WITHHOLDS the idle heartbeat
                     this poll (so the controller goes stale and the supervisor notices).
                     ``ok`` True = the sweep completed (no-op or a driven turn).
    ``drove_turn`` = a synthetic model turn was actually driven (snapshot had actionable
                     items). Informational; the loop never advances a cursor for it."""
    ran: bool = False
    ok: bool = True
    drove_turn: bool = False


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


def _iso_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _infra_retry_exhausted(rec: dict, *, now_text: str,
                           after_seconds: float, min_attempts: int) -> bool:
    if _safe_int((rec or {}).get("attempts_started")) < max(1, int(min_attempts)):
        return False
    first = _iso_epoch((rec or {}).get("first_started_at"))
    now = _iso_epoch(now_text)
    if first is None or now is None:
        return False
    return (now - first) >= max(0.0, float(after_seconds))


def _noninfra_failure_count(rec: dict) -> int:
    return (_safe_int((rec or {}).get("poison_eligible_failures"))
            + _safe_int((rec or {}).get("ambiguous_failures")))


def _noninfra_failure_class(rec: dict) -> str:
    if _safe_int((rec or {}).get("ambiguous_failures")) >= _safe_int(
            (rec or {}).get("poison_eligible_failures")):
        return CLASS_AMBIGUOUS
    return CLASS_POISON


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
             infra_exhaust_after_seconds: float = 14400.0,
             infra_exhaust_min_attempts: int = 100,
             noninfra_sub_ceiling: int | None = None,
             on_dead_letter: Callable[[dict], None] | None = None,
             on_escalate: Callable[[dict], None] | None = None,
             heartbeat: Callable[[], None] | None = None,
             pre_commit: Callable[[], None] | None = None,
             manage_waiting: bool = True,
             cadence: Callable[[], CadenceResult] | None = None,
             on_health_idle: Callable[[], None] | None = None,
             on_health_parked: Callable[[dict, str], None] | None = None,
             on_runtime_idle: Callable[[], None] | None = None,
             on_runtime_dead_letter: Callable[[dict], None] | None = None,
             capacity_refresh: Callable[[], None] | None = None,
             capacity_interval_seconds: float = 60.0,
             wrapper_generation: str | None = None,
             commit_gate=None,
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

    The ``.waiting`` marker is generation-bound and cleared on exit (try/finally)
    only while it still carries this loop's token, so an older wrapper cannot erase
    a replacement marker. ``manage_waiting=False`` leaves the marker untouched (the
    managed lead-loop controller's LEASE owns that mirror, WP2).

    ``heartbeat`` overrides the per-idle stamp (default ``store.write_heartbeat``);
    the managed lead-loop passes a combined ``write_heartbeat`` + ``renew lease`` so
    the lease stays fresh on every idle stamp (WP2 condition 4).

    ``cadence`` (WP3, CONTINUOUS only): the proactive-sweep hook, consulted on each IDLE
    poll (no inbound message) BEFORE the heartbeat. It is a SYNTHETIC, wrapper-owned turn
    - it NEVER advances the cursor, records an attempt, or enters the dead-letter path
    (the loop's commit / attempt / dead-letter machinery is untouched on the idle path).
    The hook gates due-ness itself (returns ``ran=False`` when not due, so calling it every
    poll is cheap-by-contract). A FAILED sweep (``ran and not ok``) makes the loop withhold
    the idle heartbeat this poll (controller-health staleness).

    ``capacity_refresh`` (CONTINUOUS only): an advisory observability hook
    called only after an idle heartbeat stamp or successful turn boundary. It is
    interval-gated and failure-isolated; exceptions are swallowed so capacity
    cannot undo the just-completed liveness/cursor boundary."""
    stamp = heartbeat if heartbeat is not None else (lambda: store.write_heartbeat(agent))
    gate_generation = getattr(commit_gate, "fence", None)
    wait_token = (
        wrapper_generation or gate_generation or uuid.uuid4().hex
    ) if manage_waiting else None
    try:
        if wait_token is not None:
            store.write_waiting(agent, {
                "agent": agent,
                "pid": os.getpid(),
                "since": now_iso(),
                "mode": "wrapper-loop",
                "wait_token": wait_token,
                "wrapper_generation": wait_token,
            })
        if on_health_idle is not None:
            on_health_idle()
        if on_runtime_idle is not None:
            on_runtime_idle()
        if only_request_id is not None:
            return _run_one_shot(
                store, agent, drive, rid=only_request_id,
                idle_interval=idle_interval, max_idle_interval=max_idle_interval,
                heartbeat_interval=heartbeat_interval, clock=clock, sleep=sleep,
                max_turns=max_turns, max_polls=max_polls, max_wall=max_wall,
                stamp=stamp, on_health_idle=on_health_idle,
                on_runtime_idle=on_runtime_idle,
                commit_gate=commit_gate)
        return _run_continuous(
            store, agent, drive, idle_interval=idle_interval,
            max_idle_interval=max_idle_interval, heartbeat_interval=heartbeat_interval,
            clock=clock, sleep=sleep, max_turns=max_turns, max_polls=max_polls,
            max_wall=max_wall, k_poison=k_poison, k_escalate=k_escalate,
            infra_exhaust_after_seconds=infra_exhaust_after_seconds,
            infra_exhaust_min_attempts=infra_exhaust_min_attempts,
            noninfra_sub_ceiling=noninfra_sub_ceiling,
            on_dead_letter=on_dead_letter, on_escalate=on_escalate, stamp=stamp,
            pre_commit=pre_commit, cadence=cadence, on_health_idle=on_health_idle,
            on_health_parked=on_health_parked,
            on_runtime_idle=on_runtime_idle,
            on_runtime_dead_letter=on_runtime_dead_letter,
            capacity_refresh=capacity_refresh,
            capacity_interval_seconds=capacity_interval_seconds,
            commit_gate=commit_gate,
            now_iso=now_iso)
    finally:
        if wait_token is not None:
            store.clear_waiting_if_token(agent, wait_token)


def _run_continuous(store, agent: str, drive: Callable[[dict], object], *,
                    idle_interval: float, max_idle_interval: float,
                    heartbeat_interval: float, clock: Callable[[], float],
                    sleep: Callable[[float], None], max_turns: int | None,
                    max_polls: int | None, max_wall: float | None,
                    k_poison: int, k_escalate: int,
                    infra_exhaust_after_seconds: float,
                    infra_exhaust_min_attempts: int,
                    noninfra_sub_ceiling: int | None,
                    on_dead_letter: Callable[[dict], None] | None,
                    on_escalate: Callable[[dict], None] | None,
                    stamp: Callable[[], None],
                    pre_commit: Callable[[], None] | None,
                    cadence: Callable[[], CadenceResult] | None,
                    on_health_idle: Callable[[], None] | None,
                    on_health_parked: Callable[[dict, str], None] | None,
                    on_runtime_idle: Callable[[], None] | None,
                    on_runtime_dead_letter: Callable[[dict], None] | None,
                    capacity_refresh: Callable[[], None] | None,
                    capacity_interval_seconds: float,
                    commit_gate,
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

    def _runtime_idle() -> None:
        if on_runtime_idle is not None:
            on_runtime_idle()

    def _runtime_idle_if_terminal(resolution) -> None:
        if resolution is not None and (
            resolution.terminal or resolution.allows_legacy_commit
        ):
            _runtime_idle()

    def _commit(rec: dict, gate_resolution=None) -> bool:
        _guard_advance()
        if (
            commit_gate is not None
            and gate_resolution is not None
            and gate_resolution.ledger_revision is not None
        ):
            finalized = commit_gate.finalize(
                rec,
                gate_resolution,
                expected_revision=gate_resolution.ledger_revision,
            )
            committed = finalized.allows_legacy_commit or finalized.terminal
            if committed:
                _runtime_idle()
            return committed
        recv_api.commit(store, agent, rec)
        _runtime_idle()
        return True

    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    start = clock()
    last_capacity_refresh = start

    def _maybe_refresh_capacity(now: float) -> None:
        nonlocal last_capacity_refresh
        if capacity_refresh is None:
            return
        interval = max(0.0, float(capacity_interval_seconds))
        if (now - last_capacity_refresh) < interval:
            return
        last_capacity_refresh = now
        try:
            capacity_refresh()
        except Exception:  # noqa: BLE001 - advisory capacity must never break loop progress
            return

    def _info(record: dict, rec: dict, failure_class: str, *,
              infra_exhausted: bool = False, quarantined: bool = False) -> dict:
        attempts = _safe_int((rec or {}).get("attempts_started"))
        bucket = "below_backstop"
        if quarantined:
            bucket = "quarantined"
        elif infra_exhausted:
            bucket = "infra_exhausted"
        elif k_escalate > 0 and attempts >= k_escalate:
            bucket = "escalate_backstop"
        return {"agent": agent, "msg_id": record.get("id"), "from": record.get("from"),
                "subject": record.get("subject"), "kind": record.get("kind"),
                "request_id": record.get("request_id"),
                "attempts": attempts,  # degrade-low
                "attempts_bucket": bucket,
                "first_started_at": (rec or {}).get("first_started_at"),
                "requeue_generation": (rec or {}).get("requeue_generation"),
                "infra_exhausted": infra_exhausted,
                "quarantined": quarantined,
                "failure_class": failure_class,
                "summary": (rec or {}).get("last_failure_summary") or ""}

    def _dispose(record: dict, *, failure_class: str, reason: str | None,
                 infra_exhausted: bool = False,
                 child_output_tail: dict | None = None) -> None:
        """Dead-letter the head record (store advances the cursor past it), then stamp
        progress: DL is PROGRESS, not a failed turn, so the heartbeat goes FRESH and
        the failure backoff resets - the supervisor sees progress + never restarts."""
        nonlocal last_hb, fail_sleep
        rec = store.attempt_record(agent, record.get("id")) or {}
        _guard_advance()                      # dead-letter ADVANCES the cursor - verify
        #                                       lease ownership first (lead-loop), else a
        #                                       lost-lease controller could dispose unguarded
        if commit_gate is not None and legacy_gate_resolution is not None:
            authority = commit_gate.validate_no_admission_authority(
                record,
                legacy_gate_resolution,
                side_effect=lambda: store.dead_letter(
                    agent,
                    record,
                    reason=reason,
                    failure_class=failure_class,
                    at=now_iso(),
                    child_output_tail=child_output_tail,
                ),
            )
            if not authority.allows_legacy_commit:
                stamp()
                if on_health_idle is not None:
                    on_health_idle()
                last_hb = clock()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                return
        else:
            store.dead_letter(
                agent,
                record,
                reason=reason,
                failure_class=failure_class,
                at=now_iso(),
                child_output_tail=child_output_tail,
            )
        if on_runtime_dead_letter is not None:
            on_runtime_dead_letter(record)
        stamp()                               # DL is progress (bypasses drive's stamp)
        if on_health_idle is not None:
            on_health_idle()
        last_hb = clock()
        fail_sleep = idle_interval
        if on_dead_letter is not None:
            # The disposal already SUCCEEDED (bytes in the sink, cursor advanced) and progress
            # is stamped above; a raising notice callback must not crash the loop and leave it
            # stuck - doctor surfaces the sink regardless (no-silent-disposal). Swallow + return.
            try:
                on_dead_letter(_info(
                    record, rec, failure_class,
                    infra_exhausted=infra_exhausted, quarantined=True))
            except Exception:  # noqa: BLE001 - a notification must never crash the loop
                return

    def _escalate_once(record: dict, failure_class: str, *, retry_unrouted: bool = True) -> None:
        """Class-agnostic K_escalate backstop: emit an operator escalation, DEDUPED on
        successful ROUTING - not merely on having attempted. An UNROUTED notice (no
        liaison/lead resolved) is retried on subsequent polls so it fires once the operator
        configures a target (codex P2); meanwhile doctor surfaces it LOUD. A routed notice
        latches escalation_routed and never re-sends."""
        rec = store.attempt_record(agent, record.get("id")) or {}
        if rec.get("escalation_routed") or (rec.get("escalated") and not retry_unrouted):
            return                                    # already routed -> done (deduped)
        try:
            routed = bool(on_escalate(_info(record, rec, failure_class))) \
                if on_escalate is not None else False
        except Exception:  # noqa: BLE001 - a notification must never crash the loop
            routed = False                            # unrouted -> retried + doctor LOUD
        store.mark_attempt_escalated(agent, record.get("id"), routed=routed)

    config_blocked_ids: set[str] = set()
    gateway_held_escalated: set[str] = set()

    def _park_config_blocked(record: dict, *, reason_code: str = "config_blocked") -> None:
        """Hold a deterministic local config denial (e.g. a held gateway, an exec-denied bus
        write) at the head WITHOUT consuming it. Emits a distinct advisory HEALTH state (via
        ``on_health_parked``) so status/doctor show the worker as blocked-on-a-message rather
        than a frozen 'idle' - the health-freeze that hid this wedge (#58). The heartbeat still
        stamps (a blocked worker is not dead), so the supervisor does NOT restart it."""
        nonlocal last_hb, fail_sleep
        if on_health_parked is not None:
            # advisory health must never break loop progress
            try:
                on_health_parked(record, reason_code)
            except Exception:  # noqa: BLE001, S110  # nosec
                pass
        _escalate_once(record, CLASS_CONFIG_BLOCKED, retry_unrouted=False)
        stamp()                          # keeps wrapper heartbeat and lead-loop lease fresh
        last_hb = clock()
        sleep(fail_sleep)
        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)

    def _hold_park(record: dict) -> None:
        """Blocked-but-alive RE-DRIVING park for a TRANSIENT gateway HOLD (CLASS_GATEWAY_HELD,
        #62): the mint refused because the gateway is held/unresolved - NO child spawned, NO
        spend. CLEAR the write-ahead attempt (drive() already stamped it via record_attempt_start)
        so NO failure counter climbs and attempts_started never reaches a ceiling -> the held head
        is NEVER dead-lettered (operator ruling: park a held message indefinitely). The head is
        left UNCOMMITTED, so the next poll re-drives it and it self-heals the instant the hold
        clears. Distinct from _park_config_blocked, which persists last_failure_class=config_blocked
        and thereby sticky-latches at the entry re-park (never re-driving). The heartbeat is
        re-stamped (drive() cleared it on the failed turn) so a held worker reads as blocked, not
        dead -> the supervisor does NOT restart it (no restart-storm under a long hold). Health is
        surfaced by drive()'s health_writer.failure (STATE_RATE_LIMITED_OR_OUTAGE + 'gateway_held'),
        so this park does not also write health (avoiding a state flip-flop).

        Escalate ONCE per head on first entering the held state: an operator-placed hold is
        expected (park indefinitely, ruling), but a LedgerHold can also be AUTONOMOUS (an
        unresolved provider attempt, a clock anomaly) that the operator never heard about - a
        silent indefinite fleet stall. The one-shot routed notice restores the signal master
        emitted via config_blocked, without a per-poll storm. The dedup lives in the loop
        (gateway_held_escalated), NOT the attempt record, which clear_attempt wipes every poll.

        TRADE-OFF (bounded, fail-safe): clearing the attempt also forgets any PRIOR genuine
        failures on this head, so a message that is BOTH intermittently-held AND poison has its
        poison run reset each hold. On ovh-qwen this is bounded by the ledger's per-message caps
        (300s wall / 8 calls / EUR 0.50) -> ChildTurnCapExceeded -> config_blocked park, so it
        cannot retry a poison message forever. It errs toward retrying (never toward a wrongful
        dead-letter). It is unavoidable: preserving history via record_attempt_result would let the
        write-ahead attempts_started climb into the K_escalate dispose, dead-lettering the held
        head - exactly what park-indefinitely forbids."""
        nonlocal last_hb, fail_sleep
        head_id = record.get("id")
        if isinstance(head_id, str) and head_id not in gateway_held_escalated:
            gateway_held_escalated.add(head_id)
            try:                              # a notification must never crash the loop
                if on_escalate is not None:
                    on_escalate(_info(record, {}, CLASS_GATEWAY_HELD))
            except Exception:  # noqa: BLE001, S110  # nosec
                pass
        store.clear_attempt(agent, head_id)
        stamp()                          # blocked != dead: keep heartbeat + lead-loop lease fresh
        last_hb = clock()
        sleep(fail_sleep)
        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)

    while True:
        if max_polls is not None and polls >= max_polls:
            return turns
        if max_wall is not None and (clock() - start) >= max_wall:
            return turns
        polls += 1
        record = recv_api.next_record(store, agent)
        now = clock()
        if record is None:
            # IDLE. First consult the proactive CADENCE hook (WP3): it gates due-ness
            # itself and, when due, drives at most ONE synthetic sweep turn - WITHOUT
            # ever advancing the cursor / recording an attempt / dead-lettering (this
            # idle branch contains none of that machinery, by construction).
            res = cadence() if cadence is not None else None
            if res is not None and res.ran and not res.ok:
                # FAILED sweep (controller-HEALTH, not poison): the hook already updated its
                # own backoff + escalation. WITHHOLD the idle heartbeat this poll so a
                # persistently-failing controller goes stale and the supervisor notices; do
                # NOT advance any cursor. Back off and keep listening.
                sleep(cur_sleep)
                cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
                continue
            # No cadence, not due, a no-op sweep, or a SUCCESSFUL sweep: keep the heartbeat
            # fresh (+ renew the lease for the managed lead-loop, via the injected stamp),
            # then back off while the bus is quiet.
            if last_hb is None or (now - last_hb) >= heartbeat_interval:
                stamp()
                if on_health_idle is not None:
                    on_health_idle()
                last_hb = now
                _maybe_refresh_capacity(now)
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

        legacy_gate_resolution = None
        if commit_gate is not None:
            from .obligations import GateError, ResolverState

            resolution = commit_gate.admit_or_finalize(record)
            if resolution.allows_legacy_commit:
                legacy_gate_resolution = resolution
            if not resolution.allows_legacy_commit:
                if resolution.state in {
                    ResolverState.BLOCKED,
                    ResolverState.BLOCKED_POLICY,
                    ResolverState.BLOCKED_COMPLIANCE,
                    ResolverState.INDETERMINATE,
                    ResolverState.IN_PROGRESS,
                    ResolverState.DEFERRED,
                }:
                    stamp()
                    if on_health_idle is not None:
                        on_health_idle()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                if resolution.terminal:
                    _guard_advance()
                    finalized = commit_gate.finalize(
                        record,
                        resolution,
                        expected_revision=resolution.ledger_revision,
                    )
                    if finalized.state == ResolverState.INDETERMINATE:
                        if (
                            resolution.key is not None
                            and finalized.reason == "finalization CAS contention exhausted"
                        ):
                            commit_gate.settle_retry_exhaustion(
                                record,
                                resolution.key,
                                category="finalization",
                                reason="finalization CAS contention exhausted",
                            )
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                        continue
                    if resolution.key is not None and resolution.compliance_success:
                        commit_gate.mark_satisfied(resolution.key)
                    _runtime_idle_if_terminal(finalized)
                    stamp()
                    last_hb = clock()
                    fail_sleep = idle_interval
                    continue
                if resolution.state != ResolverState.OWED_UNSATISFIED:
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue

                key = resolution.key
                if key is None:
                    stamp()
                    sleep(fail_sleep)
                    continue
                captured = commit_gate.captured_operation(key)
                if captured is not None:
                    may_retry = commit_gate.record_retry_barrier(
                        key,
                        category="operation_infra",
                        expected_revision=resolution.scoped_revision,
                    )
                    if may_retry:
                        if commit_gate.retry_captured_operation(captured, record):
                            commit_gate.mark_captured_operation_succeeded(captured)
                        else:
                            commit_gate.complete_retry_barrier(
                                key, category="operation_infra")
                    else:
                        latest = commit_gate.resolve(record)
                        should_settle = latest.terminal or latest.state in {
                            ResolverState.BLOCKED,
                            ResolverState.BLOCKED_POLICY,
                            ResolverState.BLOCKED_COMPLIANCE,
                        } or commit_gate.retry_bound_exhausted(
                            key,
                            category="operation_infra",
                        )
                        if not should_settle:
                            stamp()
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                            continue
                        _guard_advance()
                        settled = commit_gate.settle_retry_exhaustion(
                            record,
                            key,
                            category="operation_infra",
                            reason="captured bus operation exhausted its durable retry bound",
                            permit=captured,
                        )
                        _runtime_idle_if_terminal(settled)
                        commit_gate.cleanup_permit(captured)
                        stamp()
                        continue
                    resolution = commit_gate.resolve(record)
                    if resolution.terminal:
                        _guard_advance()
                        finalized = commit_gate.finalize(record, resolution)
                        if finalized.state != ResolverState.INDETERMINATE:
                            if resolution.compliance_success:
                                commit_gate.mark_satisfied(key)
                            commit_gate.cleanup_permit(captured)
                            _runtime_idle_if_terminal(finalized)
                            stamp()
                            turns += 1
                            if max_turns is not None and turns >= max_turns:
                                return turns
                        else:
                            if finalized.reason == "finalization CAS contention exhausted":
                                commit_gate.settle_retry_exhaustion(
                                    record,
                                    key,
                                    category="finalization",
                                    reason="finalization CAS contention exhausted",
                                )
                    else:
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue

                purpose = commit_gate.next_dispatch_purpose(key)
                if purpose is None:
                    if commit_gate.dispatch_exhausted(key):
                        _guard_advance()
                        disposition = commit_gate.fail_delivery_or_block(
                            record,
                            key,
                            reason=(
                                "agent computed but did not emit the owed reply "
                                "(model/prompt-compliance gap)"
                            ),
                            expected_revision=resolution.scoped_revision,
                        )
                        _runtime_idle_if_terminal(disposition)
                        stamp()
                        fail_sleep = idle_interval
                    else:
                        stamp()
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                try:
                    permit = commit_gate.reserve_dispatch(resolution, purpose=purpose)
                except GateError:
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                try:
                    dispatch_record = commit_gate.dispatch_record(record, permit)
                except GateError:
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                outcome = _as_outcome(drive(dispatch_record))
                action_infra = (
                    outcome.bus_action_infra or outcome.failure_class == CLASS_INFRA
                )
                resolution = commit_gate.resolve(record)
                post_budget_composing = resolution.reason == "post_budget_composing"
                action_rejected = outcome.bus_action_rejected or (
                    resolution.state == ResolverState.OWED_UNSATISFIED
                    and outcome.bus_action_attempted
                    and not action_infra
                )
                commit_gate.mark_dispatch_result(
                    permit,
                    action_attempted=outcome.bus_action_attempted,
                    action_rejected=action_rejected,
                    action_infra=action_infra,
                )
                if post_budget_composing:
                    resolution = commit_gate.resolve(record)
                    action_infra = commit_gate.captured_operation(key) is not None
                if (
                    resolution.state == ResolverState.OWED_UNSATISFIED
                    and outcome.bus_action_attempted
                    and not action_infra
                ):
                    commit_gate.mark_unsatisfied_attempt(
                        permit,
                        reason="attempted action did not legally terminate this obligation",
                    )
                if resolution.terminal:
                    _guard_advance()
                    finalized = commit_gate.finalize(record, resolution)
                    if finalized.state != ResolverState.INDETERMINATE:
                        if resolution.compliance_success:
                            commit_gate.mark_satisfied(key)
                        commit_gate.cleanup_permit(permit)
                        _runtime_idle_if_terminal(finalized)
                        stamp()
                        last_hb = clock()
                        fail_sleep = idle_interval
                        turns += 1
                        if max_turns is not None and turns >= max_turns:
                            return turns
                        continue
                    if finalized.reason == "finalization CAS contention exhausted":
                        commit_gate.settle_retry_exhaustion(
                            record,
                            key,
                            category="finalization",
                            reason="finalization CAS contention exhausted",
                        )
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                if action_infra:
                    may_retry = commit_gate.record_retry_barrier(
                        key,
                        category="operation_infra",
                        expected_revision=resolution.scoped_revision,
                    )
                    if may_retry:
                        if commit_gate.retry_captured_operation(permit, record):
                            commit_gate.mark_captured_operation_succeeded(permit)
                            resolution = commit_gate.resolve(record)
                            if resolution.terminal:
                                _guard_advance()
                                finalized = commit_gate.finalize(record, resolution)
                                if finalized.state != ResolverState.INDETERMINATE:
                                    if resolution.compliance_success:
                                        commit_gate.mark_satisfied(key)
                                    commit_gate.cleanup_permit(permit)
                                    _runtime_idle_if_terminal(finalized)
                                    stamp()
                                    turns += 1
                                    if max_turns is not None and turns >= max_turns:
                                        return turns
                                    continue
                                if (
                                    finalized.reason
                                    == "finalization CAS contention exhausted"
                                ):
                                    settled = commit_gate.settle_retry_exhaustion(
                                        record,
                                        key,
                                        category="finalization",
                                        reason="finalization CAS contention exhausted",
                                    )
                                    if settled.terminal:
                                        commit_gate.cleanup_permit(permit)
                                        _runtime_idle()
                                sleep(fail_sleep)
                                fail_sleep = min(
                                    max_idle_interval,
                                    fail_sleep * 2.0,
                                )
                                continue
                        else:
                            commit_gate.complete_retry_barrier(
                                key, category="operation_infra")
                    else:
                        latest = commit_gate.resolve(record)
                        should_settle = latest.terminal or latest.state in {
                            ResolverState.BLOCKED,
                            ResolverState.BLOCKED_POLICY,
                            ResolverState.BLOCKED_COMPLIANCE,
                        } or commit_gate.retry_bound_exhausted(
                            key,
                            category="operation_infra",
                        )
                        if not should_settle:
                            stamp()
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                            continue
                        _guard_advance()
                        settled = commit_gate.settle_retry_exhaustion(
                            record,
                            key,
                            category="operation_infra",
                            reason="captured bus operation exhausted its durable retry bound",
                            permit=permit,
                        )
                        _runtime_idle_if_terminal(settled)
                        commit_gate.cleanup_permit(permit)
                        stamp()
                        continue
                if commit_gate.dispatch_exhausted(key) and not action_infra:
                    _guard_advance()
                    disposition = commit_gate.fail_delivery_or_block(
                        record,
                        key,
                        reason=(
                            "agent computed but did not emit the owed reply "
                            "(model/prompt-compliance gap)"
                        ),
                        expected_revision=resolution.scoped_revision,
                    )
                    _runtime_idle_if_terminal(disposition)
                    commit_gate.cleanup_permit(permit)
                    stamp()
                    fail_sleep = idle_interval
                    continue
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue

        if (
            commit_gate is not None
            and legacy_gate_resolution is not None
            and legacy_gate_resolution.reason in {
                "no_admission_finalization_pending",
                "no_admission_disposition_pending",
            }
        ):
            _guard_advance()
            finalized = commit_gate.finalize(
                record,
                legacy_gate_resolution,
                expected_revision=legacy_gate_resolution.ledger_revision,
            )
            if finalized.allows_legacy_commit:
                store.clear_attempt(agent, record.get("id"))
                store.gc_attempts_below(agent, store.cursor(agent))
                _runtime_idle()
                stamp()
                last_hb = clock()
                fail_sleep = idle_interval
                turns += 1
                if max_turns is not None and turns >= max_turns:
                    return turns
                continue
            stamp()
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
            continue

        # ---- dead-letter: bound the at-least-once retry of a POISON head message ----
        head_id = record.get("id")
        # A stale in_progress on this head means the process crashed mid-turn last run ->
        # reconcile it as an AMBIGUOUS crash_mid_turn failure (unobserved cause, NOT poison),
        # which also RESETS the consecutive poison run (codex ruling). The write-ahead
        # attempts_started already counted the crashed attempt toward the K_escalate ceiling.
        store.reconcile_crash_in_progress(agent, head_id, at=now_iso())
        rec = store.attempt_record(agent, head_id) or {}
        if rec.get("last_failure_class") == CLASS_CONFIG_BLOCKED:
            if isinstance(head_id, str):
                config_blocked_ids.add(head_id)
            _park_config_blocked(record)
            continue
        # AUTO-DISPOSE WITHOUT DRIVE if a cap is already reached on entry (covers the
        # relaunch/crash-accumulation path - test #3).
        if rec.get("last_failure_class") != CLASS_CONFIG_BLOCKED:
            cap_now = now_iso()
            if k_poison > 0 and _safe_int(rec.get("poison_eligible_failures")) >= k_poison:
                _dispose(record, failure_class=CLASS_POISON,
                         reason=rec.get("last_failure_summary"))
                continue
            if (noninfra_sub_ceiling is not None and noninfra_sub_ceiling > 0
                    and _noninfra_failure_count(rec) >= int(noninfra_sub_ceiling)):
                cls = _noninfra_failure_class(rec)
                _dispose(record, failure_class=cls,
                         reason=rec.get("last_failure_summary"))
                continue
            if (rec.get("last_failure_class") == CLASS_INFRA
                    and _infra_retry_exhausted(
                        rec, now_text=cap_now,
                        after_seconds=infra_exhaust_after_seconds,
                        min_attempts=infra_exhaust_min_attempts)):
                _dispose(record, failure_class=CLASS_INFRA_RETRY_EXHAUSTED,
                         reason=rec.get("last_failure_summary"),
                         infra_exhausted=True)
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

        if commit_gate is not None and legacy_gate_resolution is not None:
            authorized = commit_gate.authorize_no_admission_drive(
                record,
                legacy_gate_resolution,
            )
            if authorized.terminal:
                _guard_advance()
                finalized = commit_gate.finalize(
                    record,
                    authorized,
                    expected_revision=authorized.ledger_revision,
                )
                if finalized.terminal:
                    store.clear_attempt(agent, record.get("id"))
                    store.gc_attempts_below(agent, store.cursor(agent))
                    _runtime_idle()
                    stamp()
                    fail_sleep = idle_interval
                    continue
                stamp()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            if not authorized.allows_legacy_commit:
                stamp()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            legacy_gate_resolution = authorized

        # WRITE-AHEAD: count + mark in_progress BEFORE drive() so a crash mid-turn
        # still costs a durable attempt on relaunch. EXACTLY one attempt per drive().
        store.record_attempt_start(agent, record, attempt_id=uuid.uuid4().hex[:12],
                                   at=now_iso())
        outcome = _as_outcome(drive(record))
        if outcome.ok:
            finalization_resolution = legacy_gate_resolution
            retained_success = False
            if (
                commit_gate is not None
                and legacy_gate_resolution is not None
                and legacy_gate_resolution.ledger_revision is not None
            ):
                finalization_resolution = commit_gate.record_no_admission_success(
                    record,
                    legacy_gate_resolution,
                )
                retained_success = (
                    finalization_resolution.reason
                    == "no_admission_finalization_pending"
                )
                if not (
                    finalization_resolution.allows_legacy_commit
                    or finalization_resolution.terminal
                ):
                    store.record_attempt_result(
                        agent,
                        head_id,
                        failure_class=CLASS_AMBIGUOUS,
                        summary="commit-gate no-admission success retention miss",
                        at=now_iso(),
                    )
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
            if not _commit(record, finalization_resolution):
                if retained_success:
                    store.clear_attempt(agent, head_id)
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                store.record_attempt_result(
                    agent,
                    head_id,
                    failure_class=CLASS_AMBIGUOUS,
                    summary="commit-gate no-admission CAS miss",
                    at=now_iso(),
                )
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            store.clear_attempt(agent, head_id)
            store.gc_attempts_below(agent, store.cursor(agent))
            last_hb = clock()                           # drive already stamped on success
            _maybe_refresh_capacity(last_hb)
            fail_sleep = idle_interval                  # reset failure backoff
            turns += 1
            if max_turns is not None and turns >= max_turns:
                return turns
            continue
        # FAILED turn.
        if outcome.failure_class == CLASS_GATEWAY_HELD:
            # TRANSIENT, operator-resolvable gateway hold at mint time (#62): do NOT persist an
            # attempt result. A "blocked-but-alive re-driving park" - clears the write-ahead
            # attempt (no counter climbs -> never dead-lettered), keeps the heartbeat fresh, and
            # leaves the head UNCOMMITTED so it re-drives next poll and self-heals when the hold
            # clears. Must precede record_attempt_result so gateway_held is never written to the
            # ledger (and so it can never reach a dispose ceiling). See _hold_park.
            _hold_park(record)
            continue
        # record the classified failure (clears in_progress), then decide.
        store.record_attempt_result(agent, head_id, failure_class=outcome.failure_class,
                                    summary=outcome.summary, at=now_iso())
        rec = store.attempt_record(agent, head_id) or {}
        if outcome.failure_class == CLASS_CONFIG_BLOCKED:
            if isinstance(head_id, str):
                config_blocked_ids.add(head_id)
            _park_config_blocked(record)
            continue
        if (k_poison > 0 and outcome.failure_class == CLASS_POISON
                and _safe_int(rec.get("poison_eligible_failures")) >= k_poison):
            _dispose(record, failure_class=CLASS_POISON, reason=outcome.summary,
                     child_output_tail=outcome.child_output_tail)
            continue
        if (noninfra_sub_ceiling is not None and noninfra_sub_ceiling > 0
                and _noninfra_failure_count(rec) >= int(noninfra_sub_ceiling)):
            _dispose(record, failure_class=_noninfra_failure_class(rec),
                     reason=outcome.summary,
                     child_output_tail=outcome.child_output_tail)
            continue
        if (outcome.failure_class == CLASS_INFRA
                and _infra_retry_exhausted(
                    rec, now_text=now_iso(),
                    after_seconds=infra_exhaust_after_seconds,
                    min_attempts=infra_exhaust_min_attempts)):
            _dispose(record, failure_class=CLASS_INFRA_RETRY_EXHAUSTED,
                     reason=outcome.summary, infra_exhausted=True,
                     child_output_tail=outcome.child_output_tail)
            continue
        if k_escalate > 0 and _safe_int(rec.get("attempts_started")) >= k_escalate:
            _escalate_once(record, outcome.failure_class)
            # dispose at the ceiling ONLY when not infra AND the history is not dominantly-infra
            # (codex ruling: a dominantly-infra ledger keeps retrying through the outage). rec was
            # re-read AFTER record_attempt_result above, so the predicate counts the current result.
            if outcome.failure_class != CLASS_INFRA and not _infra_dominant(rec):
                _dispose(record, failure_class=outcome.failure_class,
                         reason=outcome.summary,
                         child_output_tail=outcome.child_output_tail)
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
                  stamp: Callable[[], None] | None = None,
                  on_health_idle: Callable[[], None] | None = None,
                  on_runtime_idle: Callable[[], None] | None = None,
                  commit_gate=None) -> int:
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
                if on_health_idle is not None:
                    on_health_idle()
                last_hb = now
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        cur_sleep = idle_interval
        if is_terminal_control(record) and commit_gate is None:
            # scoped record on a rescinded/closed thread: consume + stop (terminal).
            recv_api.commit(store, agent, record)
            return turns
        legacy_gate_resolution = None
        if commit_gate is not None:
            from .obligations import GateError, ResolverState

            resolution = commit_gate.admit_or_finalize(record)
            if resolution.allows_legacy_commit:
                legacy_gate_resolution = resolution
            if not resolution.allows_legacy_commit:
                if resolution.terminal:
                    finalized = commit_gate.finalize(
                        record,
                        resolution,
                        expected_revision=resolution.ledger_revision,
                    )
                    if finalized.state != ResolverState.INDETERMINATE:
                        if resolution.key is not None and resolution.compliance_success:
                            commit_gate.mark_satisfied(resolution.key)
                        return turns
                    if (
                        resolution.key is not None
                        and finalized.reason == "finalization CAS contention exhausted"
                    ):
                        commit_gate.settle_retry_exhaustion(
                            record,
                            resolution.key,
                            category="finalization",
                            reason="finalization CAS contention exhausted",
                        )
                if resolution.state != ResolverState.OWED_UNSATISFIED:
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                key = resolution.key
                if key is None:
                    _stamp()
                    sleep(fail_sleep)
                    continue
                captured = commit_gate.captured_operation(key)
                if captured is not None:
                    may_retry = commit_gate.record_retry_barrier(
                        key,
                        category="operation_infra",
                        expected_revision=resolution.scoped_revision,
                    )
                    if not may_retry:
                        latest = commit_gate.resolve(record)
                        should_settle = latest.terminal or latest.state in {
                            ResolverState.BLOCKED,
                            ResolverState.BLOCKED_POLICY,
                            ResolverState.BLOCKED_COMPLIANCE,
                        } or commit_gate.retry_bound_exhausted(
                            key,
                            category="operation_infra",
                        )
                        if not should_settle:
                            _stamp()
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                            continue
                        commit_gate.settle_retry_exhaustion(
                            record,
                            key,
                            category="operation_infra",
                            reason="captured bus operation exhausted its durable retry bound",
                            permit=captured,
                        )
                        commit_gate.cleanup_permit(captured)
                        return turns
                    if commit_gate.retry_captured_operation(captured, record):
                        commit_gate.mark_captured_operation_succeeded(captured)
                    else:
                        commit_gate.complete_retry_barrier(
                            key, category="operation_infra")
                    resolved = commit_gate.resolve(record)
                    if resolved.terminal:
                        finalized = commit_gate.finalize(record, resolved)
                        if finalized.state != ResolverState.INDETERMINATE:
                            if resolved.compliance_success:
                                commit_gate.mark_satisfied(key)
                            commit_gate.cleanup_permit(captured)
                            turns += 1
                            return turns
                        if finalized.reason == "finalization CAS contention exhausted":
                            commit_gate.settle_retry_exhaustion(
                                record,
                                key,
                                category="finalization",
                                reason="finalization CAS contention exhausted",
                            )
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                purpose = commit_gate.next_dispatch_purpose(key)
                if purpose is None:
                    if commit_gate.dispatch_exhausted(key):
                        commit_gate.fail_delivery_or_block(
                            record,
                            key,
                            reason=(
                                "agent computed but did not emit the owed reply "
                                "(model/prompt-compliance gap)"
                            ),
                            expected_revision=resolution.scoped_revision,
                        )
                        return turns
                    _stamp()
                    sleep(fail_sleep)
                    continue
                try:
                    permit = commit_gate.reserve_dispatch(resolution, purpose=purpose)
                except GateError:
                    _stamp()
                    sleep(fail_sleep)
                    continue
                try:
                    dispatch_record = commit_gate.dispatch_record(record, permit)
                except GateError:
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                outcome = _as_outcome(drive(dispatch_record))
                action_infra = (
                    outcome.bus_action_infra or outcome.failure_class == CLASS_INFRA
                )
                resolved = commit_gate.resolve(record)
                post_budget_composing = resolved.reason == "post_budget_composing"
                action_rejected = outcome.bus_action_rejected or (
                    resolved.state == ResolverState.OWED_UNSATISFIED
                    and outcome.bus_action_attempted
                    and not action_infra
                )
                commit_gate.mark_dispatch_result(
                    permit,
                    action_attempted=outcome.bus_action_attempted,
                    action_rejected=action_rejected,
                    action_infra=action_infra,
                )
                if post_budget_composing:
                    resolved = commit_gate.resolve(record)
                    action_infra = commit_gate.captured_operation(key) is not None
                if (
                    resolved.state == ResolverState.OWED_UNSATISFIED
                    and outcome.bus_action_attempted
                    and not action_infra
                ):
                    commit_gate.mark_unsatisfied_attempt(
                        permit,
                        reason="attempted action did not legally terminate this obligation",
                    )
                if resolved.terminal:
                    finalized = commit_gate.finalize(record, resolved)
                    if finalized.state != ResolverState.INDETERMINATE:
                        if resolved.compliance_success:
                            commit_gate.mark_satisfied(key)
                        commit_gate.cleanup_permit(permit)
                        turns += 1
                        return turns
                    if finalized.reason == "finalization CAS contention exhausted":
                        commit_gate.settle_retry_exhaustion(
                            record,
                            key,
                            category="finalization",
                            reason="finalization CAS contention exhausted",
                        )
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                if action_infra:
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                if commit_gate.dispatch_exhausted(key) and not action_infra:
                    commit_gate.fail_delivery_or_block(
                        record,
                        key,
                        reason=(
                            "agent computed but did not emit the owed reply "
                            "(model/prompt-compliance gap)"
                        ),
                        expected_revision=resolved.scoped_revision,
                    )
                    commit_gate.cleanup_permit(permit)
                    return turns
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
        if (
            commit_gate is not None
            and legacy_gate_resolution is not None
            and legacy_gate_resolution.reason in {
                "no_admission_finalization_pending",
                "no_admission_disposition_pending",
            }
        ):
            finalized = commit_gate.finalize(
                record,
                legacy_gate_resolution,
                expected_revision=legacy_gate_resolution.ledger_revision,
            )
            if finalized.allows_legacy_commit:
                turns += 1
                return turns
            _stamp()
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
            continue
        # A scoped record carries ``rid`` by construction - it is the work this ephemeral
        # reviewer was spawned for. Normalize ONCE, then commit/thread-seen/turns++ ONLY when
        # ok - drive() returns a DriveOutcome (frozen dataclass, no __bool__ -> always truthy),
        # so a bare ``if drive(record):`` would treat a FAILED turn as success and mark the
        # scoped request seen (lead 4th-verify P1 #1; mirrors _run_continuous's _as_outcome).
        if commit_gate is not None and legacy_gate_resolution is not None:
            authorized = commit_gate.authorize_no_admission_drive(
                record,
                legacy_gate_resolution,
            )
            if authorized.terminal:
                finalized = commit_gate.finalize(
                    record,
                    authorized,
                    expected_revision=authorized.ledger_revision,
                )
                if finalized.terminal:
                    return turns
                _stamp()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            if not authorized.allows_legacy_commit:
                _stamp()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            legacy_gate_resolution = authorized
        outcome = _as_outcome(drive(record))
        if outcome.ok:
            if legacy_gate_resolution is not None and legacy_gate_resolution.ledger_revision is not None:
                retained = commit_gate.record_no_admission_success(
                    record,
                    legacy_gate_resolution,
                )
                if not (retained.allows_legacy_commit or retained.terminal):
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
                finalized = commit_gate.finalize(
                    record,
                    retained,
                    expected_revision=retained.ledger_revision,
                )
                if not (finalized.allows_legacy_commit or finalized.terminal):
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
            else:
                recv_api.commit(store, agent, record)   # SCOPED: thread-seen only
            if on_runtime_idle is not None:
                on_runtime_idle()
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
