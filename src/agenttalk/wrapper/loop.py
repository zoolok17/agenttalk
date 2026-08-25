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

import math
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import reply_transport

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

# #205: the classifier summary prefix for a child that produced NO turn-start and NO
# model output before exiting (run._classify_drive_failure's last-resort branch, e.g. a
# codex child refusing a non-git/untrusted workspace before it emits a single stream
# event). ONE such result stays AMBIGUOUS (a transient host hiccup must not park a
# healthy seat). A SECOND CONSECUTIVE one on the same head, at least
# NEVER_STARTED_PROMOTION_MIN_ELAPSED_SECONDS after the first (cold-review FIX 2: two
# 0.3-2.0s-apart fail_sleep samples are NOT evidence of determinism - a transient auth
# refresh / CLI update banner / AV lock must not permanently park a healthy seat), is
# deterministic: the loop promotes it to CLASS_CONFIG_BLOCKED and escalates once via
# _park_config_blocked. Unlike CLASS_GATEWAY_HELD/_hold_park's #62 self-healing
# re-probe (which clears its attempt and re-drives every poll), a config_blocked park
# is STICKY - the entry-mirror check below re-parks WITHOUT ever driving again, so
# there is no automatic re-probe here; an operator must clear the underlying denial
# (and the attempt ledger) before this head can proceed. This avoids burning the
# K_escalate ceiling on 20 ambiguous retries for a deterministic denial.
NEVER_STARTED_SUMMARY_PREFIX = "turn never started"
NEVER_STARTED_REMEDY = (
    "child CLI never started; check: workspace is a git repo / trusted directory, "
    "CLI auth, executable path")
# #205 (cold-review FIX 2): the minimum time that must have elapsed since the FIRST
# never-started failure before a second consecutive one is trusted as deterministic
# rather than a transient hiccup landing ~seconds apart under fail_sleep (0.3-2.0s).
NEVER_STARTED_PROMOTION_MIN_ELAPSED_SECONDS = 60.0


def _is_never_started_failure(failure_class: object, summary: object) -> bool:
    """True iff a failure result carries the never-started AMBIGUOUS signature."""
    return (failure_class == CLASS_AMBIGUOUS and isinstance(summary, str)
            and NEVER_STARTED_SUMMARY_PREFIX in summary)


# The continuous loop's idle-heartbeat cadence AND the #202 D2 backoff chunk size.
# Named so the launch validation (cli) can enforce the load-bearing invariant
# heartbeat_interval < stuck_after: the chunked backoff stamps once per chunk, so
# a wrapper deliberately backing off can never look stale to the supervisor.
HEARTBEAT_INTERVAL_SECONDS = 10.0

# #202 D3: dead-letter reason for a head whose turns were repeatedly killed by the
# turn watchdog (k_interrupted consecutive turn_watchdog interruptions).
INTERRUPTION_BUDGET_EXHAUSTED = "interruption_budget_exhausted"

# #202 D2 (cold-review FIX 1): hard cap on the ANY-KIND interruption backoff
# (base x 2^(n-1)). n = interrupted_consecutive is bounded by k_interrupted ONLY
# for turn_watchdog kills; crash_mid_turn's cause is unobserved, so its disposal
# ceiling stays k_escalate (default 20) - and an infra-dominant crash history
# evades even THAT ceiling (_infra_dominant keeps retrying through a sustained
# outage instead of disposing). Left uncapped, n=10 demands an 8.5h sleep and
# n=19 demands ~182 DAYS, all while the chunked stamp reads the wrapper as
# healthy - a single interrupted head would head-of-line block the whole seat's
# queue for that long. Capped, a runaway counter degrades to "very slow"
# instead of "effectively never".
INTERRUPTION_BACKOFF_CAP_SECONDS = 900.0


def _interruption_remedy(agent: str, head_id: object, *, k: int, kind: str,
                         budget_seconds: float | None,
                         preserved_draft: Path | None = None) -> str:
    """The operator remedy carried by the k_interrupted escalation (#202 D3).

    #202 cold-review P2-3: when a preserved ``<id>.interrupted.md`` sibling exists
    for this head (the last thing the child managed to write before the FINAL kill
    that exhausted the budget), name its path here - the dead-letter dispose that
    follows now KEEPS that file (see ``_dispose``) specifically so the operator has
    something to inspect/requeue with context, and the remedy must say where."""
    budget = (f" after {budget_seconds:.0f}s each"
              if isinstance(budget_seconds, (int, float)) else "")
    remedy = (f"this message's turns were killed {k} times by {kind}{budget}; "
              f"raise turn_watchdog.turn_elapsed_seconds for {agent}, split the task, "
              f"or requeue as-is: `agenttalk dead-letter requeue --agent {agent} "
              f"--id {head_id}`")
    if preserved_draft is not None:
        remedy += f"; the last preserved partial progress is at: {preserved_draft}"
    return remedy


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
    # #202 D1: a SELF-INFLICTED interruption (the per-turn watchdog killed the
    # child's tree) is a first-class fact - set structurally at every make_drive
    # failure-return site under sig["watchdog"], never sniffed from the summary
    # (the resume branches rewrite summaries). Kind: "turn_watchdog".
    interrupted: bool = False
    interruption_kind: str | None = None


def _as_outcome(ret: object) -> DriveOutcome:
    if isinstance(ret, DriveOutcome):
        return ret
    if ret:
        return DriveOutcome(ok=True)
    return DriveOutcome(ok=False, failure_class=CLASS_POISON, summary="drive returned False")


# #201 wrapper-owned reply delivery (freeform path). Kinds whose natural
# reply is a plain message get a wrapper-declared draft file the child can
# answer through with nothing but its structured Write tool — the fix for
# seats whose harness statically rejects or approval-gates shell commands.
# Typed-response threads (review-request/proposal) are excluded: their
# closure requires a typed kind the draft channel does not carry yet.
# `wake` is included: it is an ordinary driven kind whose wk- request id is
# minted precisely so a plain message reply can correlate.
_REPLY_DRAFT_KINDS = frozenset({"question", "message", "wake"})


def _interrupted_draft_path(store, agent: str, msg_id: object) -> Path | None:
    """The preserved-progress sibling #202 D5 writes for an interrupted attempt's
    draft, or None when ``msg_id`` cannot name one. Shared by the write side
    (_with_reply_draft), the GC-on-deliver side (_deliver_reply_draft), and the
    GC-on-dispose side (_run_continuous._dispose) so a stale preserved copy never
    outlives the head it was recovered for (cold-review FIX 6)."""
    if not isinstance(msg_id, str) or not msg_id:
        return None
    return reply_transport.reply_draft_path(store, agent, msg_id).with_suffix(".interrupted.md")


def _with_reply_draft(store, agent: str, record: dict) -> dict:
    """Decorate a freeform (no-admission) record with its declared draft path."""
    if record.get("kind") not in _REPLY_DRAFT_KINDS:
        return record
    inbound_id = record.get("id")
    if not isinstance(inbound_id, str) or not inbound_id:
        return record
    if record.get("from") == agent:
        return record
    # A consult reply must echo consult=true + round meta the draft channel
    # cannot carry yet — offering the channel would give the child two
    # contradictory instructions and silently break consult round tracking.
    record_meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    if record_meta.get("consult"):
        return record
    # A kind=message arriving on a review-request/proposal thread (e.g. the
    # author's answer to a needs-info review-result) owes a TYPED response
    # next — publishing a draft as kind=message would commit the turn while
    # the typed response stays owed (PR #127 connector P2).
    thread_rid = record_meta.get("request_id")
    if record.get("kind") == "message" and isinstance(thread_rid, str) and thread_rid:
        opener = reply_transport.thread_opener_kind(
            store, agent=agent, request_id=thread_rid,
        )
        if opener in ("review-request", "proposal"):
            return record
    path = reply_transport.reply_draft_path(store, agent, inbound_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bind the draft to THIS attempt: a failed prior attempt may have left
        # a partial draft at the same deterministic path, and a later clean
        # turn that never overwrites it must not publish those stale bytes as
        # the authoritative answer (PR #127 connector P1).
        # #202 D5: when the PREVIOUS attempt was interrupted, that leftover is the
        # child's recoverable progress - preserve it as <id>.interrupted.md (the
        # rejoin names it; a failed rename falls through to today's delete). The
        # live path is cleared either way, so the preserved copy never publishes.
        # cold-review FIX 5: _run_continuous has a persisted ledger (last_interrupted
        # on the attempt record), but _run_one_shot is LEDGER-LESS - its previous
        # interruption rides ONLY the in-memory ``interrupted_redelivery`` decoration
        # the one-shot loop places on the record before calling here. Without this
        # second gate the ledger read is always empty for one-shot and the draft is
        # unconditionally deleted, even while the rejoin still says "prefer resuming".
        prev = store.attempt_record(agent, inbound_id) or {}
        previously_interrupted = bool(prev.get("last_interrupted")) or isinstance(
            record.get("interrupted_redelivery"), dict)
        if previously_interrupted and path.is_file():
            reply_transport.preserve_interrupted_draft(path)
        path.unlink(missing_ok=True)
    except OSError:
        return record
    decorated = dict(record)
    decorated["reply_draft"] = {"path": str(path)}
    return decorated


def _deliver_reply_draft(store, agent: str, record: dict) -> None:
    """Publish a child-written freeform draft after a CLEAN turn.

    Refusals are silent by contract: freeform replies are not obligatory, so
    a missing/invalid draft must leave the turn disposition byte-identical
    to today. The landed-check first makes the two channels race-free — a
    capable child that ran `agenttalk reply` itself published strictly
    before this end-of-turn call, and then the draft is only residue.
    """
    declared = record.get("reply_draft")
    if not isinstance(declared, dict) or not declared.get("path"):
        return
    draft = Path(str(declared["path"]))
    try:
        if not draft.is_file():
            # P2-6: no LIVE draft this turn - the child may have answered directly
            # via the CLI/bus channel instead. Most turns have no preserved
            # <id>.interrupted.md sibling either, so check that CHEAP is_file()
            # first (no store scan paid on a plain clean turn with no interruption
            # history); only when one exists is the landed-reply scan worth its
            # cost, to GC it - otherwise a successful direct reply left it on disk
            # FOREVER (nothing else ever revisits a head once it commits clean).
            preserved = _interrupted_draft_path(store, agent, record.get("id"))
            if (preserved is not None and preserved.is_file()
                    and reply_transport.landed_reply_exists(
                        store, agent=agent, record=record)):
                try:
                    preserved.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        if reply_transport.landed_reply_exists(store, agent=agent, record=record):
            try:
                draft.unlink(missing_ok=True)
            except OSError:
                pass
            # P2-6: the reply already landed via the direct channel - GC the
            # <id>.interrupted.md sibling here too (previously only done on the
            # deliver_draft_reply success path below), or it survives forever.
            preserved = _interrupted_draft_path(store, agent, record.get("id"))
            if preserved is not None:
                try:
                    preserved.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        published = reply_transport.deliver_draft_reply(
            store, agent=agent, record=record, draft_path=draft,
        )
        if published is None and draft.exists():
            # The child wrote an answer the wrapper refused (oversize, bad
            # encoding, publish failure). The turn still commits, so without
            # a trace the answer would vanish exactly like the dead-letter
            # dotfiles #201 exists to fix. Preserve the bytes observably.
            reply_transport.preserve_refused_draft(draft)
        elif published is not None:
            # cold-review FIX 6: a real reply just landed for this head, so any
            # earlier <id>.interrupted.md leftover (recovered progress from a
            # PRIOR interrupted attempt on the same id) is now moot - GC it so it
            # cannot linger on disk / be misread as still-pending by a later probe.
            preserved = _interrupted_draft_path(store, agent, record.get("id"))
            if preserved is not None:
                try:
                    preserved.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001, S110 - must never change disposition  # nosec B110
        return


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
             heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             max_turns: int | None = None, max_polls: int | None = None,
             only_request_id: str | None = None,
             max_wall: float | None = None,
             k_poison: int = 3, k_escalate: int = 20,
             k_interrupted: int = 3,
             interruption_redrive_seconds: float = 60.0,
             interruption_budget_seconds: float | None = None,
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
    Returns the number of completed inbound turns, including a redelivered turn whose
    exact terminal work was already durable and therefore was not re-driven.
    ``max_turns`` / ``max_polls`` / ``max_wall`` bound the loop (all None = run
    forever; tests inject clock/sleep).

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
            k_interrupted=k_interrupted,
            # #205 cold-review P1-A: the SAME token this run wrote to .waiting (the
            # explicit param, else the commit-gate fence, else a fresh uuid) - the
            # entry-check park's "did the operator restart the wrapper" signal.
            wrapper_generation=wait_token,
            interruption_redrive_seconds=interruption_redrive_seconds,
            interruption_budget_seconds=interruption_budget_seconds,
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
                    k_interrupted: int,
                    interruption_redrive_seconds: float,
                    interruption_budget_seconds: float | None,
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
                    now_iso: Callable[[], str],
                    wrapper_generation: str | None = None) -> int:
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

    def _runtime_idle_if_consumed(record: dict) -> bool:
        consumed = recv_api.consume_boundary_complete(store, agent, record)
        if consumed:
            _runtime_idle()
        return consumed

    def _settle_retry_exhaustion(record: dict, *args, **kwargs):
        settled = commit_gate.settle_retry_exhaustion(record, *args, **kwargs)
        _runtime_idle_if_consumed(record)
        return settled

    def _commit(rec: dict, gate_resolution=None) -> bool:
        _guard_advance()
        if (
            commit_gate is not None
            and gate_resolution is not None
            and gate_resolution.ledger_revision is not None
        ):
            commit_gate.finalize(
                rec,
                gate_resolution,
                expected_revision=gate_resolution.ledger_revision,
            )
            committed = recv_api.consume_boundary_complete(store, agent, rec)
            if committed:
                _runtime_idle()
            return committed
        recv_api.commit(store, agent, rec)
        _runtime_idle()
        return True

    def _commit_landed(
        rec: dict,
        gate_resolution,
        landed,
        *,
        outcome: DriveOutcome | None = None,
    ) -> bool:
        proof = landed.proof
        if proof is None:
            return False
        finalization_resolution = gate_resolution
        if gate_resolution.ledger_revision is not None:
            finalization_resolution = commit_gate.retain_landed_response(
                rec,
                gate_resolution,
                proof,
            )
            if not (
                finalization_resolution.allows_legacy_commit
                or finalization_resolution.terminal
            ):
                return False
        if not _commit(rec, finalization_resolution):
            return False
        store.clear_attempt(agent, rec.get("id"))
        store.gc_attempts_below(agent, store.cursor(agent))
        if outcome is not None and not outcome.ok:
            try:
                commit_gate.record_landed_work_override(
                    rec,
                    proof,
                    failure_class=outcome.failure_class,
                    summary=outcome.summary,
                )
            except Exception:  # noqa: BLE001, S110 - advisory telemetry  # nosec B110
                # Progress is already durable; telemetry cannot revoke it.
                pass
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
              infra_exhausted: bool = False, quarantined: bool = False,
              summary: str | None = None) -> dict:
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
                # #202 D3 (review finding 9): an explicit summary carries the REAL
                # reason/remedy to the operator instead of only the ledger's last line.
                "summary": (summary if summary is not None
                            else (rec or {}).get("last_failure_summary") or "")}

    def _dispose(record: dict, *, failure_class: str, reason: str | None,
                 infra_exhausted: bool = False,
                 child_output_tail: dict | None = None,
                 summary: str | None = None,
                 preserve_interrupted_draft: bool = False) -> None:
        """Dead-letter the head record (store advances the cursor past it), then stamp
        progress: DL is PROGRESS, not a failed turn, so the heartbeat goes FRESH and
        the failure backoff resets - the supervisor sees progress + never restarts.

        ``summary`` (cold-review FIX 3) overrides the operator-facing notice text
        (default: the ledger's ``last_failure_summary``, via ``_info``). Needed
        because ``_escalate_once``'s remedy no-ops when escalation is already
        latched (a prior, unrelated escalation on this head routed already) - the
        dead-letter notification must still carry the REAL remedy (e.g. the #202 D3
        interruption-budget-exhausted remedy) regardless of that dedupe.

        ``preserve_interrupted_draft`` (cold-review P2-3): the interruption-budget-
        exhausted dispose is the ONE case where the ``<id>.interrupted.md`` sibling
        IS the final progress evidence for the very interruptions that exhausted the
        budget - GC'ing it here (as every other dispose does) destroys it exactly
        when the operator most needs it to inspect/requeue with context. The remedy
        (built by the caller) already names its path."""
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
        if not recv_api.consume_boundary_complete(store, agent, record):
            stamp()
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
            return
        # cold-review FIX 6: the head is durably disposed - GC any <id>.interrupted.md
        # preserved-progress sibling now, so it cannot outlive the head it was
        # recovered for (nothing else ever removes it once a head is dead-lettered).
        # P2-3: EXCEPT the interruption-budget-exhausted dispose, which keeps it
        # (see the docstring above) - ordinary disposes still unlink as today.
        if not preserve_interrupted_draft:
            preserved = _interrupted_draft_path(store, agent, record.get("id"))
            if preserved is not None:
                try:
                    preserved.unlink(missing_ok=True)
                except OSError:
                    pass
        if on_runtime_dead_letter is not None:
            on_runtime_dead_letter(record)
        _runtime_idle()
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
                    infra_exhausted=infra_exhausted, quarantined=True,
                    summary=summary))
            except Exception:  # noqa: BLE001 - a notification must never crash the loop
                return

    def _escalate_once(record: dict, failure_class: str, *, retry_unrouted: bool = True,
                       summary: str | None = None) -> None:
        """Class-agnostic K_escalate backstop: emit an operator escalation, DEDUPED on
        successful ROUTING - not merely on having attempted. An UNROUTED notice (no
        liaison/lead resolved) is retried on subsequent polls so it fires once the operator
        configures a target (codex P2); meanwhile doctor surfaces it LOUD. A routed notice
        latches escalation_routed and never re-sends."""
        rec = store.attempt_record(agent, record.get("id")) or {}
        if rec.get("escalation_routed") or (rec.get("escalated") and not retry_unrouted):
            return                                    # already routed -> done (deduped)
        try:
            routed = bool(on_escalate(_info(record, rec, failure_class, summary=summary))) \
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
            if res is not None and res.ran:
                _runtime_idle()
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
                            _settle_retry_exhaustion(
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
                    consumed = _runtime_idle_if_consumed(record)
                    landed = (
                        finalized.terminal
                        and finalized.landed_evidence_id is not None
                        and finalized.landed_evidence_id == finalized.evidence_id
                    )
                    if consumed and landed:
                        store.clear_attempt(agent, record.get("id"))
                        store.gc_attempts_below(agent, store.cursor(agent))
                        if on_health_idle is not None:
                            on_health_idle()
                        turns += 1
                    stamp()
                    if consumed:
                        last_hb = clock()
                        fail_sleep = idle_interval
                        if max_turns is not None and turns >= max_turns:
                            return turns
                    else:
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
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
                        _settle_retry_exhaustion(
                            record,
                            key,
                            category="operation_infra",
                            reason="captured bus operation exhausted its durable retry bound",
                            permit=captured,
                        )
                        commit_gate.cleanup_permit(captured)
                        stamp()
                        if not recv_api.consume_boundary_complete(store, agent, record):
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                        continue
                    resolution = commit_gate.resolve(record)
                    if resolution.terminal:
                        _guard_advance()
                        finalized = commit_gate.finalize(record, resolution)
                        if finalized.state != ResolverState.INDETERMINATE:
                            if resolution.compliance_success:
                                commit_gate.mark_satisfied(key)
                            commit_gate.cleanup_permit(captured)
                            consumed = _runtime_idle_if_consumed(record)
                            stamp()
                            if consumed:
                                turns += 1
                            else:
                                sleep(fail_sleep)
                                fail_sleep = min(
                                    max_idle_interval,
                                    fail_sleep * 2.0,
                                )
                            if consumed and max_turns is not None and turns >= max_turns:
                                return turns
                        else:
                            if finalized.reason == "finalization CAS contention exhausted":
                                _settle_retry_exhaustion(
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
                        commit_gate.fail_delivery_or_block(
                            record,
                            key,
                            reason=(
                                "agent computed but did not emit the owed reply "
                                "(model/prompt-compliance gap)"
                            ),
                            expected_revision=resolution.scoped_revision,
                        )
                        consumed = _runtime_idle_if_consumed(record)
                        stamp()
                        if consumed:
                            fail_sleep = idle_interval
                        else:
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
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
                # interruption accounting deliberately not wired for admitted
                # dispatches - commit-gate integration is the #201 PR-2 work; see
                # connector P1 2026-08-25
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
                        consumed = _runtime_idle_if_consumed(record)
                        stamp()
                        if consumed:
                            last_hb = clock()
                            fail_sleep = idle_interval
                            turns += 1
                        else:
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                        if consumed and max_turns is not None and turns >= max_turns:
                            return turns
                        continue
                    if finalized.reason == "finalization CAS contention exhausted":
                        _settle_retry_exhaustion(
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
                                    consumed = _runtime_idle_if_consumed(record)
                                    stamp()
                                    if consumed:
                                        turns += 1
                                    else:
                                        sleep(fail_sleep)
                                        fail_sleep = min(
                                            max_idle_interval,
                                            fail_sleep * 2.0,
                                        )
                                    if (
                                        consumed
                                        and max_turns is not None
                                        and turns >= max_turns
                                    ):
                                        return turns
                                    continue
                                if (
                                    finalized.reason
                                    == "finalization CAS contention exhausted"
                                ):
                                    settled = _settle_retry_exhaustion(
                                        record,
                                        key,
                                        category="finalization",
                                        reason="finalization CAS contention exhausted",
                                    )
                                    if settled.terminal:
                                        commit_gate.cleanup_permit(permit)
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
                        _settle_retry_exhaustion(
                            record,
                            key,
                            category="operation_infra",
                            reason="captured bus operation exhausted its durable retry bound",
                            permit=permit,
                        )
                        commit_gate.cleanup_permit(permit)
                        stamp()
                        if not recv_api.consume_boundary_complete(store, agent, record):
                            sleep(fail_sleep)
                            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                        continue
                if commit_gate.dispatch_exhausted(key) and not action_infra:
                    _guard_advance()
                    commit_gate.fail_delivery_or_block(
                        record,
                        key,
                        reason=(
                            "agent computed but did not emit the owed reply "
                            "(model/prompt-compliance gap)"
                        ),
                        expected_revision=resolution.scoped_revision,
                    )
                    consumed = _runtime_idle_if_consumed(record)
                    commit_gate.cleanup_permit(permit)
                    stamp()
                    if consumed:
                        fail_sleep = idle_interval
                    else:
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
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
            commit_gate.finalize(
                record,
                legacy_gate_resolution,
                expected_revision=legacy_gate_resolution.ledger_revision,
            )
            if _runtime_idle_if_consumed(record):
                store.clear_attempt(agent, record.get("id"))
                store.gc_attempts_below(agent, store.cursor(agent))
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

        if commit_gate is not None and legacy_gate_resolution is not None:
            landed = commit_gate.resolve_landed_response(record)
            if landed.unavailable_reason is not None:
                stamp()
                if on_health_idle is not None:
                    on_health_idle()
                last_hb = clock()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            if landed.proof is not None:
                if _commit_landed(record, legacy_gate_resolution, landed):
                    stamp()
                    if on_health_idle is not None:
                        on_health_idle()
                    last_hb = clock()
                    fail_sleep = idle_interval
                    turns += 1
                    if max_turns is not None and turns >= max_turns:
                        return turns
                else:
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue

        # ---- dead-letter: bound the at-least-once retry of a POISON head message ----
        # #7-fix-5: set below (non-None) only when this turn's drive() is the ONE
        # re-probe allowed by the generation-mismatch fall-through further down -
        # threaded through to the result-recording block so a re-probe that fails
        # (whether via direct config_blocked or via re-promotion) re-parks with THE
        # CURRENT generation, not the stale one that granted the re-probe. Without
        # this, a re-probe failing DIRECTLY as config_blocked (never_started False,
        # so the promotion path below never runs) would leave the old generation on
        # the record forever, granting every later generation a further free
        # re-probe.
        reprobe_generation_consumed: str | None = None
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
            # #205 cold-review P1-A: a sticky config_blocked park never re-fires under
            # an unchanged class - but the operator CAN fix the underlying denial
            # (workspace trust, git, auth) and RESTART the wrapper. A restart mints a
            # NEW wrapper_generation (cli.py, once per process); when it differs from
            # the generation that PROMOTED this head, that is exactly "operator
            # intervention implies restart" - allow ONE re-probe drive instead of
            # re-parking blind. Fall through (no continue) WITHOUT calling
            # _park_config_blocked: the entry cap block below is gated on
            # last_failure_class != CLASS_CONFIG_BLOCKED, so it stays skipped too -
            # the re-probe reaches drive() untouched by the other ceilings. If the
            # workspace is still broken, the never-started counting (below) re-
            # promotes and re-parks recording the NEW generation, so a same-generation
            # entry after that re-parks immediately again (no re-probe storm).
            #
            # Scoped to #205's never-started promotion ONLY: a config_blocked
            # classification reached DIRECTLY from drive() (e.g. #58's sandbox/
            # workspace denials) never writes ``promoted_by_generation`` - it stays
            # sticky-forever exactly as before (that mechanism's own contract, not
            # changed here). Only a present, non-empty recorded generation that
            # DIFFERS from the current one triggers the re-probe.
            promoted_gen = rec.get("promoted_by_generation")
            if (wrapper_generation is not None
                    and isinstance(promoted_gen, str) and promoted_gen
                    and promoted_gen != wrapper_generation):
                reprobe_generation_consumed = wrapper_generation
            else:
                _park_config_blocked(record)
                continue
        # AUTO-DISPOSE WITHOUT DRIVE if a cap is already reached on entry (covers the
        # relaunch/crash-accumulation path - test #3).
        if rec.get("last_failure_class") != CLASS_CONFIG_BLOCKED:
            cap_now = now_iso()
            # #202 D3 (rev 3 NEW-3, entry mirror): a head whose persisted
            # turn_watchdog-only counter is already AT the k_interrupted ceiling
            # (the process died between the result-write and the dispose, then
            # relaunched) is disposed WITHOUT burning another watchdog budget.
            # Strictly more specific than the caps below, so it is checked first.
            # Only turn_watchdog counts reach this counter - crash_mid_turn's
            # reconcile never increments it (the store.py ruling stands).
            if (k_interrupted > 0 and _safe_int(
                    rec.get("interrupted_watchdog_consecutive")) >= k_interrupted):
                # P2-3: name the preserved draft (if any) in the remedy, then keep it
                # through the dispose below instead of GC'ing it.
                preserved_for_remedy = _interrupted_draft_path(store, agent, head_id)
                if preserved_for_remedy is not None and not preserved_for_remedy.is_file():
                    preserved_for_remedy = None
                remedy = _interruption_remedy(
                    agent, head_id, k=k_interrupted, kind="turn_watchdog",
                    budget_seconds=interruption_budget_seconds,
                    preserved_draft=preserved_for_remedy)
                _escalate_once(record, CLASS_AMBIGUOUS, summary=remedy)
                _dispose(record, failure_class=CLASS_AMBIGUOUS,
                         reason=INTERRUPTION_BUDGET_EXHAUSTED, summary=remedy,
                         preserve_interrupted_draft=True)
                continue
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
                if _runtime_idle_if_consumed(record):
                    store.clear_attempt(agent, record.get("id"))
                    store.gc_attempts_below(agent, store.cursor(agent))
                    if (
                        finalized.landed_evidence_id is not None
                        and finalized.landed_evidence_id == finalized.evidence_id
                    ):
                        if on_health_idle is not None:
                            on_health_idle()
                        turns += 1
                    stamp()
                    fail_sleep = idle_interval
                    if max_turns is not None and turns >= max_turns:
                        return turns
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

        # #202 D2: supervisor-safe interruption backoff, computed HERE (head entry)
        # from the PERSISTED record - never from in-memory fail_sleep, which relaunch
        # amnesia resets. This single site therefore also covers crash_mid_turn's
        # immediate-redrive-at-relaunch path (the reconcile above just incremented
        # the counter). base x 2^(n-1), n = interrupted_consecutive (ANY kind -
        # turn_watchdog AND crash_mid_turn), HARD-CAPPED at
        # INTERRUPTION_BACKOFF_CAP_SECONDS (cold-review FIX 1: the earlier "no separate
        # cap needed" claim was wrong - n is bounded by k_interrupted ONLY for
        # turn_watchdog; crash_mid_turn's counter is bounded only by k_escalate
        # (default 20, so n=19 is reachable), and an infra-dominant crash history
        # evades even that ceiling (_infra_dominant keeps retrying through a sustained
        # outage instead of disposing) - so an uncapped exponent reaches hours (n=10)
        # to months (n=19) of head-of-line blocking while heartbeat-stamped healthy).
        # The sleep is CHUNKED at heartbeat_interval with a stamp per chunk
        # (blocked-but-alive, the same posture as the holds), so the supervisor can
        # never STUCK_RECOVER a wrapper that is deliberately backing off. Control
        # kinds are delayed for the duration - documented cost, bounded by the launch
        # validation (cli).
        interrupted_n = _safe_int(rec.get("interrupted_consecutive"))
        if interrupted_n > 0 and interruption_redrive_seconds > 0:
            # #7-fix-1: cap the EXPONENT before evaluating 2.0 ** x. A huge
            # persisted counter (repeated reconciled crashes in an
            # infra-dominant history) can push interrupted_n past ~1025,
            # where CPython's 2.0 ** 1025 raises OverflowError instead of
            # returning inf - crashing the loop before the cap below ever
            # applies. 2**32 seconds is already many orders past the 900s
            # cap, so clamping the exponent there changes no real behavior.
            required = min(
                float(interruption_redrive_seconds)
                * (2.0 ** min(interrupted_n - 1, 32)),
                INTERRUPTION_BACKOFF_CAP_SECONDS,
            )
            # #202 cold-review P2-4 belt: the cap above already bounds ordinary
            # cases, but a NaN/inf value (a corrupt knob past the resolver, a
            # bypassing run_loop() caller, or the CAP constant itself corrupted
            # by a future refactor) must never reach `sleep()` as a non-finite
            # duration. Not an assert (stripped under -O; bandit B101), and a
            # LITERAL fallback on purpose: falling back to the module constant
            # would re-feed the corruption when the constant is the bad value.
            if not math.isfinite(required):
                required = 900.0
            last_at = _iso_epoch(rec.get("last_failure_at"))
            now_at = _iso_epoch(now_iso())
            remaining = (required if last_at is None or now_at is None
                         else required - (now_at - last_at))
            # #7-fix-2: a backwards wall clock (last_failure_at recorded in
            # the future, or a clock step-back) can make elapsed negative,
            # pushing remaining above required. Clamp into [0, required] so
            # a clock anomaly can neither skip the backoff nor extend it.
            remaining = min(max(remaining, 0.0), required)
            while remaining > 0:
                stamp()
                last_hb = clock()
                chunk = min(max(0.1, float(heartbeat_interval)), remaining)
                sleep(chunk)
                remaining -= chunk
        # WRITE-AHEAD: count + mark in_progress BEFORE drive() so a crash mid-turn
        # still costs a durable attempt on relaunch. EXACTLY one attempt per drive().
        record = _with_reply_draft(store, agent, record)
        store.record_attempt_start(agent, record, attempt_id=uuid.uuid4().hex[:12],
                                   at=now_iso())
        outcome = _as_outcome(drive(record))
        # #201: a CLEAN turn's child-written draft is published by the wrapper
        # itself BEFORE the landed-check below, which then finds and commits it
        # through the same proof machinery as a child-delivered reply. A dirty
        # outcome (watchdog kill, nonzero exit) never publishes — a truncated
        # draft must not become the agent's authoritative answer.
        if outcome.ok:
            _deliver_reply_draft(store, agent, record)
        if commit_gate is not None and legacy_gate_resolution is not None:
            landed = commit_gate.resolve_landed_response(record)
            if landed.proof is not None:
                if _commit_landed(
                    record,
                    legacy_gate_resolution,
                    landed,
                    outcome=outcome,
                ):
                    stamp()
                    if on_health_idle is not None:
                        on_health_idle()
                    last_hb = clock()
                    _maybe_refresh_capacity(last_hb)
                    fail_sleep = idle_interval
                    turns += 1
                    if max_turns is not None and turns >= max_turns:
                        return turns
                else:
                    stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
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
            # #7-fix-7: a clean retry that writes no live draft and sends no
            # direct reply (so neither _deliver_reply_draft branch above GC'd
            # it) would otherwise leave an earlier attempt's preserved
            # <id>.interrupted.md on disk FOREVER - nothing else ever revisits
            # a head once it commits clean. GC it here, unconditionally, once
            # the record has actually committed.
            preserved = _interrupted_draft_path(store, agent, head_id)
            if preserved is not None:
                try:
                    preserved.unlink(missing_ok=True)
                except OSError:
                    pass
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
        # #205: a run of never-started results on the same head is a deterministic
        # launch/config denial, not an ambiguous hiccup - PROVIDED it is actually
        # separated in time from the FIRST one. cold-review P1-B: comparing against
        # the immediately-PRECEDING failure (as this used to) is wrong - fail_sleep
        # retries land 0.3-2.0s apart, so a child that ALWAYS fails never-started
        # (#205's motivating case: codex in a non-git dir) never satisfies a 60s
        # PAIRWISE gap and burns k_escalate=20 ambiguous retries again. Persist
        # ``never_started_first_at`` (set once, on the FIRST never-started result)
        # and ``never_started_consecutive``; promote once count >= 2 AND the window
        # has elapsed since the FIRST failure - so a fast, ALWAYS-failing loop
        # promotes at the first failure after ~60s (not never), while a genuine
        # transient (one or two, then success) never promotes (the fields are reset
        # to None/0 below whenever a non-never-started result lands). Missing/
        # unparseable timestamps fail CLOSED (keep retrying ambiguous) rather than
        # promote on unproven elapsed time.
        failure_now = now_iso()
        never_started = _is_never_started_failure(outcome.failure_class, outcome.summary)
        never_started_first_at: str | None = None
        never_started_consecutive = 0
        promoted_by_generation: str | None = None
        if never_started:
            prev = store.attempt_record(agent, head_id) or {}
            prev_first_at = prev.get("never_started_first_at")
            prev_count = _safe_int(prev.get("never_started_consecutive"))
            never_started_first_at = (
                prev_first_at
                if prev_count > 0 and isinstance(prev_first_at, str) and prev_first_at
                else failure_now
            )
            never_started_consecutive = prev_count + 1
            first_at_epoch = _iso_epoch(never_started_first_at)
            now_at_epoch = _iso_epoch(failure_now)
            elapsed_ok = (
                first_at_epoch is not None and now_at_epoch is not None
                and (now_at_epoch - first_at_epoch)
                    >= NEVER_STARTED_PROMOTION_MIN_ELAPSED_SECONDS
            )
            # connector P1 (final head): fail_sleep retries land 0.3-2.0s apart,
            # so a FAST deterministic refusal reaches the k_escalate attempt
            # ceiling (default 20, disposed below as ambiguous) in well under
            # the 60s window - the promotion would never fire exactly for the
            # fastest, most clearly deterministic refusals. An UNBROKEN run of
            # never-started results as long as the ceiling is deterministic
            # evidence regardless of wall time: promote it here, preempting the
            # generic ceiling dispose this same iteration. A mixed history
            # (count resets on any non-never-started result) still disposes
            # ambiguous at the ceiling - that run is not deterministic.
            ceiling_run_ok = (
                k_escalate > 0 and never_started_consecutive >= k_escalate
            )
            if never_started_consecutive >= 2 and (elapsed_ok or ceiling_run_ok):
                outcome = replace(outcome, failure_class=CLASS_CONFIG_BLOCKED,
                                  summary=f"{NEVER_STARTED_REMEDY} ({outcome.summary})")
                # #205 cold-review P1-A: name the wrapper generation active at THIS
                # promotion, so the entry-check park (above) can allow exactly one
                # re-probe drive after a restart following operator intervention.
                promoted_by_generation = wrapper_generation
            # else: window not yet elapsed (or timestamps unavailable) - keep
            # retrying ambiguous instead of permanently parking the head.
        if (outcome.failure_class == CLASS_CONFIG_BLOCKED
                and promoted_by_generation is None
                and reprobe_generation_consumed is not None):
            # #7-fix-5: this turn consumed the one allowed re-probe (entry-check
            # above), and it failed directly as config_blocked (not through the
            # never-started promotion above, so promoted_by_generation is still
            # unset). Record the CURRENT generation now, so the next generation's
            # entry-check sees a matching (not stale) value and re-parks instead
            # of granting yet another free re-probe.
            promoted_by_generation = reprobe_generation_consumed
        # record the classified failure (clears in_progress), then decide.
        store.record_attempt_result(agent, head_id, failure_class=outcome.failure_class,
                                    summary=outcome.summary, at=failure_now,
                                    interrupted=outcome.interrupted,
                                    interruption_kind=outcome.interruption_kind,
                                    never_started_first_at=never_started_first_at,
                                    never_started_consecutive=never_started_consecutive,
                                    promoted_by_generation=promoted_by_generation)
        rec = store.attempt_record(agent, head_id) or {}
        if outcome.failure_class == CLASS_CONFIG_BLOCKED:
            if isinstance(head_id, str):
                config_blocked_ids.add(head_id)
            _park_config_blocked(record)
            continue
        # #202 D3: the interruption budget. Counts ONLY kind=turn_watchdog results
        # (crash_mid_turn's cause is unobserved - its disposal ceiling stays
        # k_escalate, per the codified store.py ruling). Checked BEFORE
        # k_poison/noninfra/k_escalate: strictly more specific. At the ceiling the
        # head DEAD-LETTERS (self-clearing: cursor advances, the seat keeps serving
        # the queue; `dead-letter requeue` is the operator's raised-budget retry)
        # and the escalation carries the concrete remedy - NOT a park, which would
        # wedge the seat with no un-park path (rev 1 F1/F2).
        if (k_interrupted > 0 and _safe_int(
                rec.get("interrupted_watchdog_consecutive")) >= k_interrupted):
            # #7-fix-6: THIS turn's drive() just ran (and was interrupted, so
            # _deliver_reply_draft above never published it) - if it left a LIVE
            # draft (<id>.md), that is the child's NEWEST recoverable progress,
            # newer than whatever older <id>.interrupted.md an earlier interrupted
            # attempt may have preserved. Move it over that older copy (pre-unlink
            # for Windows, via preserve_interrupted_draft) BEFORE the dispose below
            # keeps the interrupted.md sibling - so the remedy names the freshest
            # partial work, not a stale one, and the live copy is never orphaned.
            if isinstance(head_id, str) and head_id:
                live_draft = reply_transport.reply_draft_path(store, agent, head_id)
                if live_draft.is_file():
                    reply_transport.preserve_interrupted_draft(live_draft)
            # P2-3: name the preserved draft (if any) in the remedy, then keep it
            # through the dispose below instead of GC'ing it.
            preserved_for_remedy = _interrupted_draft_path(store, agent, head_id)
            if preserved_for_remedy is not None and not preserved_for_remedy.is_file():
                preserved_for_remedy = None
            remedy = _interruption_remedy(
                agent, head_id, k=k_interrupted, kind="turn_watchdog",
                budget_seconds=interruption_budget_seconds,
                preserved_draft=preserved_for_remedy)
            _escalate_once(record, outcome.failure_class or CLASS_AMBIGUOUS,
                           summary=remedy)
            _dispose(record, failure_class=CLASS_AMBIGUOUS,
                     reason=INTERRUPTION_BUDGET_EXHAUSTED,
                     child_output_tail=outcome.child_output_tail,
                     summary=remedy,
                     preserve_interrupted_draft=True)
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

    # #202 D4 (one-shot): this scoped loop has NO attempt ledger, so the previous
    # attempt's interruption is carried IN-MEMORY (single head by construction - a
    # local run suffices) and handed to the drive as a record decoration the cli's
    # rejoin_for consumes. D2's persisted-entry backoff deliberately does not apply
    # here (the one-shot is bounded by max_wall anyway).
    interrupted_run: dict = {"count": 0, "kind": None, "at": None}

    def _with_interruption_context(rec: dict) -> dict:
        if interrupted_run["count"] <= 0:
            return rec
        decorated = dict(rec)
        decorated["interrupted_redelivery"] = {
            "kind": interrupted_run["kind"],
            "consecutive": interrupted_run["count"],
            "last_failure_at": interrupted_run["at"],
        }
        return decorated

    def _note_interruption(outcome: DriveOutcome) -> None:
        if outcome.interrupted:
            interrupted_run["count"] += 1
            interrupted_run["kind"] = outcome.interruption_kind
            interrupted_run["at"] = _iso_now()
        else:
            interrupted_run.update({"count": 0, "kind": None, "at": None})

    def _runtime_idle() -> None:
        if on_runtime_idle is not None:
            on_runtime_idle()

    def _runtime_idle_if_consumed(record: dict) -> bool:
        consumed = recv_api.consume_boundary_complete(store, agent, record)
        if consumed:
            _runtime_idle()
        return consumed

    def _settle_retry_exhaustion(record: dict, *args, **kwargs):
        settled = commit_gate.settle_retry_exhaustion(record, *args, **kwargs)
        _runtime_idle_if_consumed(record)
        return settled

    def _commit_scoped_landed(
        rec: dict,
        gate_resolution,
        landed,
        *,
        outcome: DriveOutcome | None = None,
    ) -> bool:
        proof = landed.proof
        if proof is None:
            return False
        if gate_resolution.ledger_revision is not None:
            retained = commit_gate.retain_landed_response(
                rec,
                gate_resolution,
                proof,
            )
            if not (retained.allows_legacy_commit or retained.terminal):
                return False
            finalized = commit_gate.finalize(
                rec,
                retained,
                expected_revision=retained.ledger_revision,
            )
            if not (finalized.allows_legacy_commit or finalized.terminal):
                return False
        else:
            recv_api.commit(store, agent, rec)
        if not _runtime_idle_if_consumed(rec):
            return False
        if outcome is not None and not outcome.ok:
            try:
                commit_gate.record_landed_work_override(
                    rec,
                    proof,
                    failure_class=outcome.failure_class,
                    summary=outcome.summary,
                )
            except Exception:  # noqa: BLE001, S110 - advisory telemetry  # nosec B110
                # Progress is already durable; telemetry cannot revoke it.
                pass
        return True

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
            _runtime_idle()
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
                        consumed = _runtime_idle_if_consumed(record)
                        landed = (
                            finalized.terminal
                            and finalized.landed_evidence_id is not None
                            and finalized.landed_evidence_id == finalized.evidence_id
                        )
                        if consumed and landed:
                            if on_health_idle is not None:
                                on_health_idle()
                            turns += 1
                        if consumed:
                            return turns
                    if (
                        resolution.key is not None
                        and finalized.reason == "finalization CAS contention exhausted"
                    ):
                        _settle_retry_exhaustion(
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
                        _settle_retry_exhaustion(
                            record,
                            key,
                            category="operation_infra",
                            reason="captured bus operation exhausted its durable retry bound",
                            permit=captured,
                        )
                        commit_gate.cleanup_permit(captured)
                        if recv_api.consume_boundary_complete(store, agent, record):
                            return turns
                        _stamp()
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                        continue
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
                            if _runtime_idle_if_consumed(record):
                                turns += 1
                                return turns
                        if finalized.reason == "finalization CAS contention exhausted":
                            _settle_retry_exhaustion(
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
                        if _runtime_idle_if_consumed(record):
                            return turns
                        _stamp()
                        sleep(fail_sleep)
                        fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                        continue
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
                outcome = _as_outcome(
                    drive(_with_interruption_context(dispatch_record)))
                _note_interruption(outcome)
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
                        if _runtime_idle_if_consumed(record):
                            turns += 1
                            return turns
                    if finalized.reason == "finalization CAS contention exhausted":
                        _settle_retry_exhaustion(
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
                    if _runtime_idle_if_consumed(record):
                        return turns
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
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
            commit_gate.finalize(
                record,
                legacy_gate_resolution,
                expected_revision=legacy_gate_resolution.ledger_revision,
            )
            if _runtime_idle_if_consumed(record):
                turns += 1
                return turns
            _stamp()
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
            continue

        if commit_gate is not None and legacy_gate_resolution is not None:
            landed = commit_gate.resolve_landed_response(record)
            if landed.unavailable_reason is not None:
                _stamp()
                if on_health_idle is not None:
                    on_health_idle()
                last_hb = clock()
                sleep(fail_sleep)
                fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
            if landed.proof is not None:
                if _commit_scoped_landed(
                    record,
                    legacy_gate_resolution,
                    landed,
                ):
                    _stamp()
                    if on_health_idle is not None:
                        on_health_idle()
                    last_hb = clock()
                    fail_sleep = idle_interval
                    turns += 1
                    if max_turns is not None and turns >= max_turns:
                        return turns
                else:
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
                consumed = _runtime_idle_if_consumed(record)
                if consumed:
                    landed = (
                        finalized.terminal
                        and finalized.landed_evidence_id is not None
                        and finalized.landed_evidence_id == finalized.evidence_id
                    )
                    if landed:
                        _stamp()
                        if on_health_idle is not None:
                            on_health_idle()
                        turns += 1
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
        # cold-review FIX 5: decorate with the in-memory interruption context BEFORE
        # _with_reply_draft, so its ledger-less preservation gate (see there) can see
        # THIS attempt's "previously interrupted" fact on the record itself.
        record = _with_reply_draft(store, agent, _with_interruption_context(record))
        outcome = _as_outcome(drive(record))
        _note_interruption(outcome)
        # connector P2 (final head): one-shot is LEDGER-LESS, so the FIX 5
        # decoration above is the only carrier of "previously interrupted" -
        # and it only helps if THIS invocation gets another iteration. If the
        # bound (max_wall/max_polls) expires first, the killed attempt's
        # partial draft stays at the LIVE path, and a LATER invocation (fresh
        # memory, empty ledger) deletes it as ordinary stale residue. Preserve
        # it NOW, at the interrupted outcome itself; the next attempt's
        # preservation gate then finds no live file (a no-op) and the
        # preserved copy survives the exit either way.
        if outcome.interrupted:
            inbound_id = record.get("id")
            if isinstance(inbound_id, str) and inbound_id:
                live_draft = reply_transport.reply_draft_path(
                    store, agent, inbound_id)
                if live_draft.is_file():
                    reply_transport.preserve_interrupted_draft(live_draft)
        # #201: same wrapper-owned draft delivery as _run_continuous — the
        # one-shot path must not strand a sandbox-blocked child's answer.
        if outcome.ok:
            _deliver_reply_draft(store, agent, record)
        if commit_gate is not None and legacy_gate_resolution is not None:
            landed = commit_gate.resolve_landed_response(record)
            if landed.proof is not None:
                if _commit_scoped_landed(
                    record,
                    legacy_gate_resolution,
                    landed,
                    outcome=outcome,
                ):
                    _stamp()
                    if on_health_idle is not None:
                        on_health_idle()
                    last_hb = clock()
                    fail_sleep = idle_interval
                    turns += 1
                    if max_turns is not None and turns >= max_turns:
                        return turns
                else:
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                continue
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
                commit_gate.finalize(
                    record,
                    retained,
                    expected_revision=retained.ledger_revision,
                )
                if not recv_api.consume_boundary_complete(store, agent, record):
                    _stamp()
                    sleep(fail_sleep)
                    fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
                    continue
            else:
                recv_api.commit(store, agent, record)   # SCOPED: thread-seen only
            _runtime_idle()
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
