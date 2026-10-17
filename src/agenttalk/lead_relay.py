"""Owed hand-offs of finished results: derived from the bus, closed by one atomic operation.

A lead that opened a request (task, review-request, proposal, question) gets a *result* when the peer's terminal
response arrives. Other seats may wait for the
lead's decision about that result. Whether a waiting seat has been answered is NOT stored in a file that can be lost: it
is **derived**, every time, from messages
that are already durable on the bus (see ``docs/DESIGN-lead-loop-default.md``, section 5).

* *waiter declaration*: a message from seat S with ``meta.waiting_on=<request id>[,<request id>]``, or
``meta.relay_to=<seat>[,<seat>]`` on the lead's own
  opening message of that request; ``meta.waiting_off=<request id>`` from S cancels S's declaration (the later message
  wins).
* *closing message*: a message from the lead L to S with ``meta.relay_of=<result id>`` (the decision text),
``meta.relay_outcome=not-needed`` (a reason), or
  ``meta.relay_of=<request id>`` with ``meta.relay_outcome=unresolved`` (the request is no longer in live history).
  It counts only as the FULL tuple: ``relay_of``, the ``relay_nonce`` that :func:`handoff_nonce` gives for this lead,
  target and seat, a supported outcome (none means forwarded) and, for not-needed, a reason in the body. A message
  that carries only part of it is ordinary chatter and closes nothing. A forwarded or not-needed closing also names
  the request (``meta.relay_request``), so the closed relation survives the archiving of the request itself.

The promise is deliberately small: best effort, for declared waiters only, and an undeclared waiter is invisible.
Integrity is the declared sender plus the store's
configured integrity policy (``Store.valid_messages()``); a malicious seat of the same OS user is outside this slice.

Everything here is a pure read of the validated message list (no cursor, no write); the one write is
:func:`publish_handoff`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from agenttalk import threads as _threads
from agenttalk.store import OPENER_KINDS, Message, Store

OWED = "owed"
UNRESOLVED = "unresolved"
CLOSED_FORWARDED = "closed-forwarded"
CLOSED_NOT_NEEDED = "closed-not-needed"
CLOSED_UNRESOLVED = "closed-unresolved"
CLOSED_RECIPIENT_RETIRED = "closed-recipient-retired"
CANCELLED = "cancelled"
OPEN_STATES = (OWED, UNRESOLVED)

OUTCOME_FORWARDED = "forwarded"
OUTCOME_NOT_NEEDED = "not-needed"
OUTCOME_UNRESOLVED = "unresolved"

RELAY_DEADLINE_SECONDS = 600.0     # ESTIMATE: twice the default sweep interval
RELAY_ESCALATE_BUCKET = 3          # the third reminder also goes to the operator
NOTICE_LIMIT = 10
UNRESOLVED_TEXT = "unresolved: result no longer in live history"


class RelayRefused(ValueError):
    """The hand-off cannot be published (no such result, wrong recipient, nothing to resolve).

    Raised inside the publication lock."""


@dataclass(frozen=True)
class Obligation:
    lead: str
    seat: str
    request_id: str
    result_id: str | None          # None while unresolved (no result in live history)
    state: str
    since: str                     # ISO time the obligation became owed (the later of the result and the declaration)
    declaration_id: str
    closing_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


def _epoch(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def age_seconds(ob: Obligation, now_epoch: float) -> float:
    return max(0.0, now_epoch - _epoch(ob.since))


def _csv(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _meta(m: Message) -> dict:
    return m.meta if isinstance(m.meta, dict) else {}


def _decisions(messages: list[Message], lead: str) -> tuple[dict, dict, set]:
    """Return (latest declaration by (seat, rid), latest cancel id by (seat, rid), rids opened by another seat)."""
    declared: dict[tuple[str, str], Message] = {}
    cancelled: dict[tuple[str, str], str] = {}
    foreign: set[str] = set()
    for m in messages:                                   # ``messages`` is sorted by id
        meta = _meta(m)
        for rid in _csv(meta.get("waiting_on")):
            if m.sender != lead:
                declared[(m.sender, rid)] = m
        for rid in _csv(meta.get("waiting_off")):
            cancelled[(m.sender, rid)] = m.id
        if m.sender == lead and m.kind in OPENER_KINDS:
            rid = meta.get("request_id")
            if isinstance(rid, str) and rid:
                for seat in _csv(meta.get("relay_to")):
                    if seat != lead:
                        declared[(seat, rid)] = m
        elif m.kind in OPENER_KINDS and m.sender != lead:
            rid = meta.get("request_id")
            if isinstance(rid, str) and rid:
                foreign.add(rid)
    return declared, cancelled, foreign


def is_closing(m: Message, lead: str) -> bool:
    """True when ``m`` carries the full closing tuple (see the module text); anything less closes nothing."""
    meta = _meta(m)
    ref = meta.get("relay_of")
    if m.sender != lead or not isinstance(ref, str) or not ref:
        return False
    outcome = meta.get("relay_outcome") or OUTCOME_FORWARDED
    if outcome not in (OUTCOME_FORWARDED, OUTCOME_NOT_NEEDED, OUTCOME_UNRESOLVED):
        return False
    if meta.get("relay_nonce") != handoff_nonce(lead, ref, m.recipient, outcome):
        return False
    return outcome != OUTCOME_NOT_NEEDED or bool((m.body or "").strip())


def _closings(messages: list[Message], lead: str) -> tuple[dict[tuple[str, str], Message],
                                                           dict[tuple[str, str], Message]]:
    """``({(relay_of, seat): closing}, {(request id, seat): closing})`` of the lead's valid closing messages.

    The second map holds forwarded and not-needed closings by the request they name, so a declaration whose
    request and result were archived first (compaction runs in batches) still finds its closing."""
    by_ref: dict[tuple[str, str], Message] = {}
    by_request: dict[tuple[str, str], Message] = {}
    for m in messages:
        if not is_closing(m, lead):
            continue
        meta = _meta(m)
        by_ref[(meta["relay_of"], m.recipient)] = m
        req = meta.get("relay_request")
        if isinstance(req, str) and req and meta.get("relay_outcome") != OUTCOME_UNRESOLVED:
            by_request[(req, m.recipient)] = m
    return by_ref, by_request


def derive(messages: list[Message], lead: str, *, retired: Iterable[str] = ()) -> list[Obligation]:
    """Every obligation of ``lead`` that the live messages show, oldest first. ``messages`` MUST be
    ``Store.valid_messages()`` (roster, schema and, when the
    store enforces signing, the signature gate). A declaration for a request that has no live opener of the lead's and
    is not another seat's request is
    ``unresolved`` (the bus cannot tell an archived request from a typo)."""
    msgs = sorted(messages, key=lambda m: m.id)
    retired_set = set(retired)
    declared, cancelled, foreign = _decisions(msgs, lead)
    results = _threads.terminal_responses(msgs, lead)
    closings, closings_by_request = _closings(msgs, lead)
    openers = {rid for rid, group in _threads.group_messages_by_request_id(msgs).items()
               if any(m.kind in OPENER_KINDS and m.sender == lead for m in group)}
    found: list[Obligation] = []
    for (seat, rid), decl in declared.items():
        cancel_id = cancelled.get((seat, rid))
        is_cancelled = bool(cancel_id and cancel_id > decl.id)
        if rid in results:
            result = results[rid][1]
            closing = closings.get((result.id, seat))
            since = decl.ts if _epoch(decl.ts) > _epoch(result.ts) else result.ts
            if closing is not None:
                outcome = _meta(closing).get("relay_outcome")
                state = CLOSED_NOT_NEEDED if outcome == OUTCOME_NOT_NEEDED else CLOSED_FORWARDED
                found.append(Obligation(lead, seat, rid, result.id, state, since, decl.id, closing.id))
            elif is_cancelled:
                found.append(Obligation(lead, seat, rid, result.id, CANCELLED, since, decl.id))
            elif seat in retired_set:
                found.append(Obligation(lead, seat, rid, result.id, CLOSED_RECIPIENT_RETIRED, since, decl.id))
            else:
                found.append(Obligation(lead, seat, rid, result.id, OWED, since, decl.id))
        elif rid in openers or rid in foreign:
            continue    # the result has not arrived yet (or the request is another seat's): nothing owed now
        else:
            closing = closings.get((rid, seat))
            kept = closings_by_request.get((rid, seat))
            if closing is not None and _meta(closing).get("relay_outcome") == OUTCOME_UNRESOLVED:
                found.append(Obligation(lead, seat, rid, None, CLOSED_UNRESOLVED, decl.ts, decl.id, closing.id))
            elif kept is not None and kept.id > decl.id and not is_cancelled:
                # the request and its result were archived, the declaration and the closing were not
                state = (CLOSED_NOT_NEEDED if _meta(kept).get("relay_outcome") == OUTCOME_NOT_NEEDED
                         else CLOSED_FORWARDED)
                found.append(Obligation(lead, seat, rid, _meta(kept).get("relay_of"), state, decl.ts, decl.id,
                                        kept.id))
            elif is_cancelled:
                found.append(Obligation(lead, seat, rid, None, CANCELLED, decl.ts, decl.id))
            elif seat in retired_set:
                found.append(Obligation(lead, seat, rid, None, CLOSED_RECIPIENT_RETIRED, decl.ts, decl.id))
            else:
                found.append(Obligation(lead, seat, rid, None, UNRESOLVED, decl.ts, decl.id))
    found.sort(key=lambda o: (_epoch(o.since), o.seat, o.request_id))
    return found


def derive_for_seat(messages: list[Message], seat: str, *, retired: Iterable[str] = ()) -> list[Obligation]:
    """What each lead's derivation says about the declarations of ``seat`` (the read-only ``relay waiting`` view)."""
    msgs = sorted(messages, key=lambda m: m.id)
    leads = {m.sender for m in msgs if m.kind in OPENER_KINDS}
    seen: dict[tuple[str, str, str], Obligation] = {}
    for lead in sorted(leads):
        for ob in derive(msgs, lead, retired=retired):
            if ob.seat == seat:
                seen[(ob.lead, ob.request_id, ob.seat)] = ob
    orphan_rids = {rid for m in msgs if m.sender == seat for rid in _csv(_meta(m).get("waiting_on"))}
    known = {ob.request_id for ob in seen.values()}
    groups = _threads.group_messages_by_request_id(msgs)
    live_rids = {rid for rid, group in groups.items() if any(m.kind in OPENER_KINDS for m in group)}
    for rid in sorted(orphan_rids - known - live_rids):
        decl = next(m for m in reversed(msgs) if m.sender == seat and rid in _csv(_meta(m).get("waiting_on")))
        cancel = [m for m in msgs if m.sender == seat and rid in _csv(_meta(m).get("waiting_off")) and m.id > decl.id]
        closing = next((m for m in reversed(msgs) if m.recipient == seat and _meta(m).get("relay_of") == rid
                        and is_closing(m, m.sender) and _meta(m).get("relay_outcome") == OUTCOME_UNRESOLVED), None)
        if closing is not None:
            seen[(closing.sender, rid, seat)] = Obligation(closing.sender, seat, rid, None, CLOSED_UNRESOLVED, decl.ts,
                                                           decl.id, closing.id)
        elif cancel:
            seen[("?", rid, seat)] = Obligation("?", seat, rid, None, CANCELLED, decl.ts, decl.id)
        else:
            seen[("?", rid, seat)] = Obligation("?", seat, rid, None, UNRESOLVED, decl.ts, decl.id)
    return sorted(seen.values(), key=lambda o: (_epoch(o.since), o.request_id))


def protected_floor(messages: list[Message], *, retired: Iterable[str] = ()) -> str | None:
    """The oldest message id that an open (owed or unresolved) obligation of any lead depends on, or None.

    Compaction must keep everything from this id on: the declaration, the request group and the result. Archiving
    them would end the derivation, which is the loss this design exists to prevent.
    """
    msgs = sorted(messages, key=lambda m: m.id)
    groups = _threads.group_messages_by_request_id(msgs)
    ids: list[str] = []
    leads = {m.sender for m in msgs if m.kind in OPENER_KINDS}
    leads |= {m.recipient for m in msgs if _csv(_meta(m).get("waiting_on"))}    # a lead with no live opener at all
    for lead in leads:
        for ob in derive(msgs, lead, retired=retired):
            if not ob.is_open:
                continue
            ids.append(ob.declaration_id)
            if ob.result_id:
                ids.append(ob.result_id)
            ids.extend(m.id for m in groups.get(ob.request_id, []))
    return min(ids) if ids else None


def open_obligations(obligations: Iterable[Obligation]) -> list[Obligation]:
    return [o for o in obligations if o.is_open]


def reminder_bucket(age: float, deadline: float = RELAY_DEADLINE_SECONDS) -> int:
    """0 before the deadline; 1, 2, 3, 4 at 1, 2, 4 and 8 times the deadline; then one more every 24 hours."""
    if age < deadline:
        return 0
    bucket = 1 + sum(1 for factor in (2, 4, 8) if age >= deadline * factor)
    if age >= deadline * 8:
        bucket += int((age - deadline * 8) // 86400)
    return bucket


def _human_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 90:
        return "%d min" % minutes
    hours = minutes / 60.0
    return "%.1f h" % hours if hours < 48 else "%.1f days" % (hours / 24.0)


def notice_lines(obligations: Iterable[Obligation], *, now_epoch: float, deadline: float = RELAY_DEADLINE_SECONDS,
                 limit: int = NOTICE_LIMIT) -> list[str]:
    """The owed-relay block (plain text, bounded); empty when nothing is open."""
    opened = open_obligations(obligations)
    if not opened:
        return []
    lines = ["== OWED RELAY (best effort; declared waiters only) =="]
    for ob in opened[:limit]:
        age = age_seconds(ob, now_epoch)
        flag = " [OVERDUE]" if age >= deadline else ""
        if ob.state == UNRESOLVED:
            lines.append("- UNRESOLVED: seat %s declared that it waits for request %s; %s (declared %s ago)%s" % (
                ob.seat, ob.request_id, UNRESOLVED_TEXT, _human_age(age), flag))
            lines.append('  close it: agenttalk relay result --request-id %s --to %s --unresolved'
                         % (ob.request_id, ob.seat))
        else:
            lines.append("- seat %s waits for the decision on request %s (result %s, owed for %s)%s" % (
                ob.seat, ob.request_id, ob.result_id, _human_age(age), flag))
            lines.append('  forward it: agenttalk relay result --result-id %s --to %s -m "<decision>"'
                         '   or   --none-needed --reason "<why>"' % (ob.result_id, ob.seat))
    if len(opened) > limit:
        lines.append("- ... and %d more (agenttalk lead-routine status)" % (len(opened) - limit))
    return lines


def is_lead_like(store: Store, agent: str) -> bool:
    """Only a lead (role lead, a managed lead loop or the operator-facing seat) pays for the derivation."""
    try:
        cfg = store.load_config()
        role = (cfg.get("roles") or {}).get(agent)
        is_role = isinstance(role, str) and role.casefold() == "lead"
        return is_role or store.is_managed_lead_loop(agent) or store.operator_facing() == agent
    except Exception:  # noqa: BLE001 - advisory, never fails a command
        return False


def obligations_for(store: Store, agent: str) -> list[Obligation]:
    return derive(store.valid_messages(), agent, retired=store.retired_agents())


def notice_for(store: Store, agent: str, *, now_epoch: float | None = None) -> str:
    """The notice text a lead's command or turn prompt carries, or "" (not a lead, nothing open, or any read problem:
    advisory only)."""
    import time
    try:
        if not is_lead_like(store, agent):
            return ""
        lines = notice_lines(obligations_for(store, agent), now_epoch=time.time() if now_epoch is None else now_epoch)
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(lines)


def handoff_nonce(lead: str, target: str, seat: str, outcome: str) -> str:
    """Stable identity of one hand-off: one nonce per lead, result (or request, if unresolved) and recipient."""
    kind = "unresolved" if outcome == OUTCOME_UNRESOLVED else "result"
    return hashlib.sha256(("relay\0%s\0%s\0%s\0%s" % (lead, kind, target, seat)).encode("utf-8")).hexdigest()[:32]


def publish_handoff(store: Store, lead: str, seat: str, *, result_id: str | None = None,
                    request_id: str | None = None,
                    body: str = "", outcome: str = OUTCOME_FORWARDED,
                    reason: str | None = None) -> tuple[Message, bool]:
    """Publish ONE closing message to ``seat`` exactly once: ``(message, created)``.

    A rerun, a second process and a crash between the send and the intent update all return the first
    message (``created`` False). A different payload under the same identity is refused (``ValueError``
    from the store). The result (or, for ``unresolved``, the absence of a live request) is re-derived
    INSIDE the publication lock (``state/operation-publication.lock``, rank 100; ``send`` takes the
    message-publication mutex, rank 160, inside it, as ``send_operation`` always does)."""
    if outcome not in (OUTCOME_FORWARDED, OUTCOME_NOT_NEEDED, OUTCOME_UNRESOLVED):
        raise RelayRefused("unknown outcome %r" % outcome)
    if seat == lead:
        raise RelayRefused("a lead does not hand a result to itself")
    if outcome == OUTCOME_UNRESOLVED:
        if not request_id:
            raise RelayRefused("--unresolved needs --request-id")
        target = request_id
        text = (body or "").strip() or UNRESOLVED_TEXT
    else:
        if not result_id:
            raise RelayRefused("--result-id is required")
        target = result_id
        text = (reason if outcome == OUTCOME_NOT_NEEDED else body) or ""
        text = text.strip()
        if not text:
            raise RelayRefused("a reason (--none-needed --reason) is required" if outcome == OUTCOME_NOT_NEEDED
                               else "the decision text is required (-m)")
    meta = {"relay_of": target, "relay_nonce": handoff_nonce(lead, target, seat, outcome)}
    if outcome != OUTCOME_FORWARDED:
        meta["relay_outcome"] = outcome
    if outcome != OUTCOME_UNRESOLVED:
        # name the request, so the closed relation outlives the archiving of the request and its result
        shown = next((m for m in store.valid_messages() if m.id == result_id), None)
        rid_of_result = _meta(shown).get("request_id") if shown is not None else None
        if isinstance(rid_of_result, str) and rid_of_result:
            meta["relay_request"] = rid_of_result
    nonce = meta["relay_nonce"]
    intent = Store._operation_intent_identity(operation="relay", kind="message", recipient=seat, meta=meta)
    digest = Store._operation_digest_for_intent(intent, text)

    def precheck() -> None:
        msgs = store.valid_messages()
        if seat not in store.load_config().get("agents", []):
            raise RelayRefused("%r is not on the roster" % seat)
        if outcome == OUTCOME_UNRESOLVED:
            live = [m for m in msgs if m.kind in OPENER_KINDS and (_meta(m).get("request_id") == request_id)]
            if live:
                raise RelayRefused("request %s is still in live history; use --result-id once its result arrives"
                                   % request_id)
            return
        found = {terminal.id: rid for rid, (_, terminal) in _threads.terminal_responses(msgs, lead).items()}
        if result_id not in found:
            raise RelayRefused("%s is not a finished result of a request opened by %s in live history"
                               % (result_id, lead))

    subject = "relay: %s" % outcome if outcome != OUTCOME_FORWARDED else "relay: decision"
    return store.send_operation(sender=lead, recipient=seat, body=text, kind="message", subject=subject, meta=meta,
                                operation_nonce=nonce, operation_digest=digest, operation="relay", precheck=precheck)
