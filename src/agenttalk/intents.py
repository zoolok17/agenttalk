"""Dashboard intent queue: typed schema + the EXECUTOR (v0.59.0 write spine).

Architecture C (docs/DASHBOARD-CONTROL-PLANE-ROADMAP.md): the web tier can only
APPEND a typed intent envelope (``store.write_intent``); this module is the
other half - the supervised executor (``agenttalk supervise --drain-intents``)
that claims queued intents, RE-RESOLVES ALL AUTHORITY SERVER-SIDE, and performs
the actual bus writes through the normal ``store.send`` validation/HMAC path.
Nothing the browser asserts (origin, ``from``, ``human_authorized``) is ever
authority - it is diagnostics at best; the acting identity always comes from
:func:`resolve_web_actor` and every recipient/audience/thread anchor is
re-derived here at drain time.

Dedup (the P0 attempt-floor reservation - consult-agreed over a reserved
message id, which would be silently LOST to cursor fast-skip when written
late): BEFORE each send the executor durably records the delivery attempt in
the intent file - a stable content FINGERPRINT plus ``attempt_floor``, a fresh
message id minted by the SAME process's monotonic generator. The sent message
id is therefore guaranteed ``> attempt_floor``, so crash recovery is an
airtight bounded scan: any message with ``id > attempt_floor`` carrying this
intent's ``web_intent_id`` + delivery index + fingerprint proves the send
completed -> mark delivered, never re-send. Concurrent-drainer double-sends
are excluded at the CLAIM: a claimed intent is reclaimed only when its owner
pid is CONFIRMED dead (never on age alone - live/unknown owners block), and
the supervisor instance lock is a second, process-lifetime layer.

Fixed bus-kind map (v0.59.0 Tier-1): send -> message|note|question, reply ->
message|proposal-response, propose -> proposal, broadcast ->
message|note|question. Control kinds (release/end/rescind/wake/composing) are
unrepresentable; reserved/control payload keys are REJECTED (400), never
silently stripped, at BOTH ``store.write_intent`` and the executor.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Callable

from agenttalk.store import CONTROL_KINDS, Store, _new_id

EXECUTOR_MARKER = "dashboard_intent_v2"

INTENT_KINDS = ("send", "reply", "propose", "broadcast")

# Message kinds a browser intent may produce, per intent kind. Everything else
# (release, end, rescind, wake, composing, review-result, gate, ...) is
# permanently browser-unreachable in v0.59.
_SEND_MESSAGE_KINDS = frozenset({"message", "note", "question"})
_REPLY_KINDS = frozenset({"message", "proposal-response"})
_PROPOSAL_STATUSES = frozenset({"accepted", "rejected", "countered"})
_AUDIENCE_KINDS = frozenset({"all", "group", "role"})
_SAFE_BUS_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")

# Reserved/control keys REJECTED anywhere in a payload (exact names).
RESERVED_KEYS = frozenset({
    "meta",                      # no free-form meta surface in v0.59 at all
    "from", "sender",            # authority is resolve_web_actor, never payload
    "request_id", "broadcast_id", "epoch_at_send",
    "needs_operator", "human_authorized",
    "release", "end", "rescind", "wake", "composing",
})
# Reserved prefixes (executor-computed namespaces).
RESERVED_PREFIXES = ("web_intent_", "executor_")

_MAX_BODY_CHARS = 65536
_MAX_SUBJECT_CHARS = 512


class IntentDenied(Exception):
    """A structured executor denial (terminal state=denied, typed code)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _plan_revalidation_failed(detail_code: str, detail: str) -> IntentDenied:
    return IntentDenied("plan_revalidation_failed",
                        f"{detail_code}: {detail}")


# ------------------------------------------------------------------ schema

def _reserved_key_errors(payload: dict) -> list[str]:
    out = []
    for k in payload:
        if not isinstance(k, str):
            out.append(f"payload key {k!r} is not a string")
            continue
        if k in RESERVED_KEYS or any(k.startswith(p) for p in RESERVED_PREFIXES):
            out.append(f"payload key {k!r} is reserved/control - rejected, "
                       f"never silently stripped")
    return out


def _str_field(payload: dict, key: str, *, required: bool, max_len: int,
               errors: list[str]) -> str:
    v = payload.get(key)
    if v is None:
        if required:
            errors.append(f"missing required field {key!r}")
        return ""
    if not isinstance(v, str):
        errors.append(f"field {key!r} must be a string")
        return ""
    if required and not v.strip():
        errors.append(f"field {key!r} must be non-empty")
    if len(v) > max_len:
        errors.append(f"field {key!r} exceeds {max_len} chars")
    return v


def validate_intent(kind: object, payload: object) -> list[str]:
    """PURE typed-schema validation, fail-closed. Returns human-readable
    errors ([] = valid). Applied at BOTH the web write (``store.write_intent``)
    and the executor (defense in depth against a co-resident dropping a file
    straight into ``state/intents/active``)."""
    if kind not in INTENT_KINDS:
        return [f"unknown intent kind {kind!r} (allowed: {', '.join(INTENT_KINDS)})"]
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    errors = _reserved_key_errors(payload)
    if errors:
        return errors
    allowed: set[str]
    if kind == "send":
        allowed = {"target", "subject", "body", "message_kind"}
    elif kind == "reply":
        allowed = {"to_request", "body", "reply_kind", "status"}
    elif kind == "propose":
        allowed = {"target", "subject", "body"}
    else:  # broadcast
        allowed = {"audience", "subject", "body", "message_kind"}
    for k in payload:
        if k not in allowed:
            errors.append(f"unknown field {k!r} for intent kind {kind!r}")
    _str_field(payload, "body", required=True, max_len=_MAX_BODY_CHARS, errors=errors)
    if "subject" in allowed:
        _str_field(payload, "subject", required=False,
                   max_len=_MAX_SUBJECT_CHARS, errors=errors)
    if kind in ("send", "propose"):
        _str_field(payload, "target", required=True, max_len=256, errors=errors)
    if kind == "send" or kind == "broadcast":
        mk = payload.get("message_kind", "message")
        if mk not in _SEND_MESSAGE_KINDS:
            errors.append(f"message_kind {mk!r} not allowed "
                          f"(allowed: {sorted(_SEND_MESSAGE_KINDS)})")
    if kind == "reply":
        _str_field(payload, "to_request", required=True, max_len=256, errors=errors)
        rk = payload.get("reply_kind", "message")
        if rk not in _REPLY_KINDS:
            errors.append(f"reply_kind {rk!r} not allowed "
                          f"(allowed: {sorted(_REPLY_KINDS)})")
        status = payload.get("status")
        if rk == "proposal-response":
            if status not in _PROPOSAL_STATUSES:
                errors.append("proposal-response requires status in "
                              f"{sorted(_PROPOSAL_STATUSES)}")
        elif status is not None:
            errors.append("field 'status' is only valid with "
                          "reply_kind='proposal-response'")
    if kind == "broadcast":
        aud = payload.get("audience")
        if not isinstance(aud, dict):
            errors.append("broadcast requires audience: "
                          "{kind: all|group|role, value?: str}")
        else:
            for k in aud:
                if k not in ("kind", "value"):
                    errors.append(f"unknown audience field {k!r}")
            akind = aud.get("kind")
            if akind not in _AUDIENCE_KINDS:
                errors.append(f"audience.kind {akind!r} not allowed "
                              f"(allowed: {sorted(_AUDIENCE_KINDS)})")
            aval = aud.get("value")
            if akind in ("group", "role"):
                if not (isinstance(aval, str) and aval.strip()):
                    errors.append(f"audience.kind {akind!r} requires a "
                                  f"non-empty audience.value")
            elif aval is not None:
                errors.append("audience.value is not valid with audience.kind='all'")
    return errors


# ------------------------------------------------------------------ authority

def resolve_web_actor(store: Store) -> str | None:
    """The ONLY identity a browser-originated intent can act as: the
    operator-facing liaison, else the unambiguous sole lead, else None
    (fail-closed). Takes NO identity argument on purpose - this is a DERIVER,
    not a validator: browser-supplied identity fields never reach it (nor
    ``_resolve_disposition_actor`` / ``loop_exit_relay_authorized``, which
    validate already-known identities)."""
    actor = store.operator_facing()
    if actor is not None:
        return actor
    return store.sole_lead()


def web_actor_denial(store: Store) -> tuple[str, str]:
    """The typed denial (code, remediation) for resolve_web_actor() == None.
    Distinct codes because the operator fix differs (consult condition)."""
    try:
        cfg = store.load_config()
    except (ValueError, OSError, FileNotFoundError):
        cfg = {}
    roster = cfg.get("agents", []) or []
    roles = cfg.get("roles") or {}
    leads = [a for a in roster
             if isinstance(roles.get(a), str) and roles[a].casefold() == "lead"]
    if len(leads) > 1:
        return ("multiple_leads_configured",
                "several role=lead agents exist and no operator-facing liaison "
                "is set; pick one: agenttalk roster set-operator-facing <agent>")
    return ("no_liaison_and_no_sole_lead",
            "no operator-facing liaison and no sole role=lead agent; run "
            "agenttalk roster set-operator-facing <agent> (or set exactly one "
            "role=lead)")


# ------------------------------------------------------------------ fingerprint

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def delivery_fingerprint(*, intent_id: str, delivery_index: int, actor: str,
                         recipient: str, bus_kind: str, subject: str,
                         body: str, stable_meta: dict) -> str:
    """Stable sha256 over a length-prefixed canonical tuple. Includes recipient
    + delivery_index (identical bodies to different recipients cannot collide);
    EXCLUDES the message id and signature fields, so it is identical across
    retry attempts of the same delivery."""
    parts = [
        intent_id, str(int(delivery_index)), actor, recipient, bus_kind,
        _sha256_text(subject), _sha256_text(body),
        _sha256_text(json.dumps(stable_meta, sort_keys=True, ensure_ascii=False)),
        EXECUTOR_MARKER,
    ]
    h = hashlib.sha256()
    for p in parts:
        pb = p.encode("utf-8")
        h.update(len(pb).to_bytes(4, "big"))
        h.update(pb)
    return h.hexdigest()


# ------------------------------------------------------------------ planning

def _roster(store: Store) -> tuple[list[str], dict, dict]:
    cfg = store.load_config()
    return (cfg.get("agents", []) or [], cfg.get("roles") or {},
            cfg.get("groups") or {})


def resolve_reply_anchor(store: Store, actor: str, request_id: str):
    """The latest validated non-control message addressed to ``actor`` in the
    ``request_id`` thread - the same anchor rule as ``agenttalk reply
    --to-request`` (cli._resolve_reply_anchor), narrowed to the executor's
    needs. Returns the Message or None."""
    matches = [m for m in store.messages_for(actor)
               if (m.meta or {}).get("request_id") == request_id
               and m.kind not in CONTROL_KINDS]
    return matches[-1] if matches else None


def _safe_prefixed_id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and _SAFE_BUS_ID_RE.fullmatch(value) is not None
    )


def _epoch_shape(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and _SAFE_BUS_ID_RE.fullmatch(value) is not None
    )


def _resolve_plan_semantics(store: Store, actor: str, record: dict) -> dict:
    """Side-effect-free authority + delivery semantic resolver.

    ``build_plan`` mints stable ids from this shape; ``validate_frozen_plan``
    checks an untrusted frozen plan against the same shape before any send.
    """
    kind = record.get("kind")
    payload = record.get("payload") or {}
    roster, roles, groups = _roster(store)
    body = payload.get("body") or ""
    subject = payload.get("subject") or ""

    def _check_target(target: str) -> None:
        if target not in roster:
            raise IntentDenied("target_not_in_roster",
                               f"recipient {target!r} is not in the roster")

    if kind == "send":
        target = payload.get("target") or ""
        _check_target(target)
        bus_kind = payload.get("message_kind") or "message"
        meta_shape = "send_question" if bus_kind == "question" else "empty"
        recipients = [target]
    elif kind == "propose":
        target = payload.get("target") or ""
        _check_target(target)
        bus_kind = "proposal"
        meta_shape = "propose"
        recipients = [target]
    elif kind == "reply":
        rid = payload.get("to_request") or ""
        anchor = resolve_reply_anchor(store, actor, rid)
        if anchor is None:
            raise IntentDenied(
                "reply_anchor_not_found",
                f"no validated message addressed to {actor!r} carries "
                f"request_id {rid!r}")
        bus_kind = payload.get("reply_kind") or "message"
        meta_shape = ("reply_proposal_response"
                      if bus_kind == "proposal-response" else "reply_message")
        recipients = [anchor.sender]
    elif kind == "broadcast":
        aud = payload.get("audience") or {}
        akind = aud.get("kind")
        aval = aud.get("value")
        if akind == "all":
            recipients = [a for a in roster if a != actor]
        elif akind == "group":
            members = groups.get(aval) or []
            recipients = [a for a in members if a in roster and a != actor]
        else:  # role
            recipients = [a for a in roster
                          if a != actor and isinstance(roles.get(a), str)
                          and roles[a].casefold() == str(aval).casefold()]
        if not recipients:
            raise IntentDenied("empty_audience",
                               f"audience {akind!r}={aval!r} resolves to no "
                               f"recipients (excluding the actor)")
        bus_kind = payload.get("message_kind") or "message"
        meta_shape = "broadcast_question" if bus_kind == "question" else "broadcast"
    else:
        raise IntentDenied("invalid_payload", f"unknown intent kind {kind!r}")

    return {
        "kind": kind,
        "payload": payload,
        "recipients": recipients,
        "bus_kind": bus_kind,
        "subject": subject,
        "body": body,
        "meta_shape": meta_shape,
    }


def _opener_meta(store: Store, meta: dict, bus_kind: str) -> dict:
    if bus_kind in {"question", "proposal"} and "epoch_at_send" not in meta:
        meta = dict(meta)
        meta["epoch_at_send"] = store.current_epoch()
    return meta


def _mint_stable_meta(store: Store, semantic: dict) -> dict:
    shape = semantic["meta_shape"]
    bus_kind = semantic["bus_kind"]
    payload = semantic["payload"]
    if shape == "empty":
        return {}
    if shape == "send_question":
        return _opener_meta(
            store, {"request_id": "q-" + secrets.token_hex(6)}, bus_kind)
    if shape == "propose":
        return _opener_meta(
            store, {"request_id": "pp-" + secrets.token_hex(6)}, bus_kind)
    if shape == "reply_message":
        return {"request_id": payload.get("to_request") or ""}
    if shape == "reply_proposal_response":
        return {
            "request_id": payload.get("to_request") or "",
            "status": payload.get("status") or "",
        }
    if shape in {"broadcast", "broadcast_question"}:
        bid = "b-" + secrets.token_hex(6)
        shared = {
            "broadcast_id": bid,
            "request_id": bid,
            "audience_kind": str((payload.get("audience") or {}).get("kind")),
            "audience_resolved": ",".join(semantic["recipients"]),
            "batch_total": str(len(semantic["recipients"])),
        }
        return _opener_meta(store, shared, bus_kind)
    raise IntentDenied("invalid_payload", f"unknown stable meta shape {shape!r}")


def _semantic_deliveries(semantic: dict, stable_meta: dict) -> list[dict]:
    return [
        {
            "recipient": recipient,
            "bus_kind": semantic["bus_kind"],
            "subject": semantic["subject"],
            "body": semantic["body"],
            "stable_meta": dict(stable_meta),
        }
        for recipient in semantic["recipients"]
    ]


def build_plan(store: Store, actor: str, record: dict) -> dict:
    """Freeze the delivery plan (recipients + bus kinds + minted thread ids)
    for an intent. Raises :class:`IntentDenied` on any resolution failure.
    Called ONCE per intent - the frozen plan makes retries deterministic (same
    minted request_id/broadcast_id, same audience, same fingerprints)."""
    semantic = _resolve_plan_semantics(store, actor, record)
    stable_meta = _mint_stable_meta(store, semantic)
    deliveries = _semantic_deliveries(semantic, stable_meta)
    return {"actor": actor, "deliveries": deliveries, "planned_at_epoch": time.time()}


def _stable_meta_error(semantic: dict, meta: object) -> str | None:
    if not isinstance(meta, dict):
        return "stable_meta must be an object"
    shape = semantic["meta_shape"]
    payload = semantic["payload"]
    recipients = semantic["recipients"]
    if shape == "empty":
        allowed: set[str] = set()
    elif shape in {"send_question", "propose"}:
        allowed = {"request_id", "epoch_at_send"}
    elif shape == "reply_message":
        allowed = {"request_id"}
    elif shape == "reply_proposal_response":
        allowed = {"request_id", "status"}
    elif shape == "broadcast":
        allowed = {
            "broadcast_id", "request_id", "audience_kind",
            "audience_resolved", "batch_total",
        }
    else:  # broadcast_question
        allowed = {
            "broadcast_id", "request_id", "audience_kind",
            "audience_resolved", "batch_total", "epoch_at_send",
        }
    keys = set(meta)
    if keys != allowed:
        extra = sorted(keys - allowed)
        missing = sorted(allowed - keys)
        parts = []
        if extra:
            parts.append(f"unexpected stable_meta keys {extra}")
        if missing:
            parts.append(f"missing stable_meta keys {missing}")
        return "; ".join(parts)
    if shape == "empty":
        return None
    if shape == "send_question":
        if not _safe_prefixed_id(meta.get("request_id"), "q-"):
            return "send question request_id must be a safe q-* id"
        if not _epoch_shape(meta.get("epoch_at_send")):
            return "send question epoch_at_send has invalid shape"
        return None
    if shape == "propose":
        if not _safe_prefixed_id(meta.get("request_id"), "pp-"):
            return "proposal request_id must be a safe pp-* id"
        if not _epoch_shape(meta.get("epoch_at_send")):
            return "proposal epoch_at_send has invalid shape"
        return None
    if shape == "reply_message":
        if meta.get("request_id") != payload.get("to_request"):
            return "reply request_id does not match payload.to_request"
        return None
    if shape == "reply_proposal_response":
        if meta.get("request_id") != payload.get("to_request"):
            return "proposal-response request_id does not match payload.to_request"
        if meta.get("status") != payload.get("status"):
            return "proposal-response status does not match payload.status"
        return None
    if not _safe_prefixed_id(meta.get("broadcast_id"), "b-"):
        return "broadcast_id must be a safe b-* id"
    if meta.get("request_id") != meta.get("broadcast_id"):
        return "broadcast request_id must equal broadcast_id"
    if meta.get("audience_kind") != str((payload.get("audience") or {}).get("kind")):
        return "broadcast audience_kind does not match payload"
    if meta.get("audience_resolved") != ",".join(recipients):
        return "broadcast audience_resolved does not match current audience"
    if meta.get("batch_total") != str(len(recipients)):
        return "broadcast batch_total does not match delivery count"
    if shape == "broadcast_question" and not _epoch_shape(meta.get("epoch_at_send")):
        return "broadcast question epoch_at_send has invalid shape"
    return None


def validate_frozen_plan(store: Store, actor: str, record: dict, plan: object) -> None:
    """Validate an untrusted frozen plan against current store semantics.

    The plan is never authority: this re-resolves the allowed recipients,
    content, bus kind, and stable-meta shape before reconciliation or send.
    """
    if not isinstance(plan, dict):
        raise _plan_revalidation_failed("plan_shape", "plan must be an object")
    if plan.get("actor") != actor:
        raise _plan_revalidation_failed("actor_changed", "plan actor changed")
    try:
        semantic = _resolve_plan_semantics(store, actor, record)
    except IntentDenied as e:
        raise _plan_revalidation_failed(e.code, e.detail) from e
    deliveries = plan.get("deliveries")
    if not isinstance(deliveries, list):
        raise _plan_revalidation_failed("plan_shape",
                                        "plan.deliveries must be a list")
    expected_recipients = semantic["recipients"]
    if len(deliveries) != len(expected_recipients):
        raise _plan_revalidation_failed(
            "recipient_drift",
            "delivery count does not match current semantics")
    actual_recipients: list[str] = []
    for i, delivery in enumerate(deliveries):
        if not isinstance(delivery, dict):
            raise _plan_revalidation_failed(
                "plan_shape", f"delivery {i} must be an object")
        recipient = delivery.get("recipient")
        if not isinstance(recipient, str):
            raise _plan_revalidation_failed(
                "recipient_drift", f"delivery {i} recipient must be a string")
        actual_recipients.append(recipient)
    if len(set(actual_recipients)) != len(actual_recipients):
        raise _plan_revalidation_failed(
            "recipient_drift", "duplicate recipient in delivery fan-out")
    if actual_recipients != expected_recipients:
        raise _plan_revalidation_failed(
            "recipient_drift",
            "delivery recipients do not match current semantics")
    for i, delivery in enumerate(deliveries):
        if delivery.get("bus_kind") != semantic["bus_kind"]:
            raise _plan_revalidation_failed(
                "bus_kind_drift",
                f"delivery {i} bus_kind does not match current semantics")
        subject = delivery.get("subject")
        body = delivery.get("body")
        if not isinstance(subject, str) or len(subject) > _MAX_SUBJECT_CHARS:
            raise _plan_revalidation_failed(
                "body_subject_drift", f"delivery {i} subject has invalid shape")
        if not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
            raise _plan_revalidation_failed(
                "body_subject_drift", f"delivery {i} body has invalid shape")
        if subject != semantic["subject"] or body != semantic["body"]:
            raise _plan_revalidation_failed(
                "body_subject_drift",
                f"delivery {i} content does not match payload")
        meta_error = _stable_meta_error(semantic, delivery.get("stable_meta"))
        if meta_error is not None:
            raise _plan_revalidation_failed(
                "stable_meta_shape", f"delivery {i}: {meta_error}")


# ------------------------------------------------------------------ drain

def _find_completed_send(store: Store, *, intent_id: str, delivery_index: int,
                         attempt_floor: str, actor: str, delivery: dict) -> str | None:
    """Crash recovery: the bounded idempotency scan. ``attempt_floor`` was
    minted by the sending process's own monotonic id generator BEFORE the send,
    so a completed send's id is strictly greater - the scan can never miss it,
    and it never scans the log before the floor."""
    for m in store.valid_messages():
        if m.id <= attempt_floor:
            continue
        if (m.sender != actor or m.recipient != delivery["recipient"]
                or m.kind != delivery["bus_kind"]):
            continue
        meta = m.meta or {}
        stable_meta = delivery.get("stable_meta") or {}
        if any(meta.get(k) != v for k, v in stable_meta.items()):
            continue
        fp = delivery_fingerprint(
            intent_id=intent_id, delivery_index=delivery_index, actor=actor,
            recipient=delivery["recipient"], bus_kind=delivery["bus_kind"],
            subject=m.subject or "", body=m.body or "",
            stable_meta=stable_meta)
        if (meta.get("web_intent_id") == intent_id
                and str(meta.get("web_intent_delivery_index")) == str(delivery_index)
                and meta.get("web_intent_fingerprint") == fp):
            return m.id
    return None


def _drain_one(store: Store, rec: dict) -> str:
    """Execute one CLAIMED intent to a terminal state. Returns the terminal
    state string. Every authority decision happens HERE, server-side."""
    iid = rec["intent_id"]
    errors = validate_intent(rec.get("kind"), rec.get("payload"))
    if errors:
        store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                   code="invalid_payload",
                                   error="; ".join(errors)[:500])
        return Store.INTENT_DENIED
    actor = resolve_web_actor(store)
    if actor is None:
        code, detail = web_actor_denial(store)
        store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                   code=code, error=detail)
        return Store.INTENT_DENIED
    plan = rec.get("plan") if isinstance(rec.get("plan"), dict) else None
    if plan is None:
        try:
            plan = build_plan(store, actor, rec)
        except IntentDenied as e:
            store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                       code=e.code, error=e.detail)
            return Store.INTENT_DENIED
        frozen = plan

        def _freeze(r: dict) -> None:
            r["plan"] = frozen

        rec = store.update_intent(iid, _freeze) or rec
    elif plan.get("actor") != actor:
        # Authority changed between attempts: fail closed, never silently
        # re-author a half-delivered fan-out as a different identity.
        store.mark_intent_terminal(
            iid, state=Store.INTENT_FAILED, code="actor_changed",
            error=f"resolved actor {actor!r} differs from the frozen plan's "
                  f"{plan.get('actor')!r}; requeue a fresh intent")
        return Store.INTENT_FAILED
    try:
        validate_frozen_plan(store, actor, rec, plan)
    except IntentDenied as e:
        store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                   code=e.code, error=e.detail[:500])
        return Store.INTENT_DENIED
    deliveries = plan.get("deliveries") or []
    states = list(rec.get("deliveries") or [])
    while len(states) < len(deliveries):
        states.append({})
    for i, d in enumerate(deliveries):
        st = states[i] if isinstance(states[i], dict) else {}
        if st.get("state") == "delivered":
            continue
        fp = delivery_fingerprint(
            intent_id=iid, delivery_index=i, actor=actor,
            recipient=d["recipient"], bus_kind=d["bus_kind"],
            subject=d.get("subject") or "", body=d.get("body") or "",
            stable_meta=d.get("stable_meta") or {})
        prior_floor = st.get("attempt_floor")
        if isinstance(prior_floor, str) and prior_floor:
            done = _find_completed_send(store, intent_id=iid, delivery_index=i,
                                        attempt_floor=prior_floor, actor=actor,
                                        delivery=d)
            if done is not None:
                _record_delivery(store, iid, i, state="delivered",
                                 message_id=done, fingerprint=fp,
                                 attempt_floor=prior_floor)
                continue
        # Durable pre-send reservation: floor minted NOW by THIS process's
        # monotonic generator, recorded BEFORE the send (P0 dedup).
        floor = _new_id()
        _record_delivery(store, iid, i, state="attempting", fingerprint=fp,
                         attempt_floor=floor, recipient=d["recipient"],
                         bus_kind=d["bus_kind"])
        meta = dict(d.get("stable_meta") or {})
        meta.update({
            "web_intent_id": iid,
            "web_intent_delivery_index": str(i),
            "web_intent_fingerprint": fp,
            "web_intent_attempt_floor": floor,
            "executor_marker": EXECUTOR_MARKER,
        })
        try:
            msg = store.send(sender=actor, recipient=d["recipient"],
                             body=d.get("body") or "", kind=d["bus_kind"],
                             subject=d.get("subject") or "", meta=meta)
        except ValueError as e:
            store.mark_intent_terminal(iid, state=Store.INTENT_FAILED,
                                       code="send_rejected", error=str(e)[:500])
            return Store.INTENT_FAILED
        _record_delivery(store, iid, i, state="delivered", message_id=msg.id,
                         fingerprint=fp, attempt_floor=floor)
    store.mark_intent_terminal(iid, state=Store.INTENT_APPLIED)
    return Store.INTENT_APPLIED


def _record_delivery(store: Store, intent_id: str, index: int, **fields) -> None:
    def _mut(rec: dict) -> None:
        dl = rec.setdefault("deliveries", [])
        while len(dl) <= index:
            dl.append({})
        if not isinstance(dl[index], dict):
            dl[index] = {}
        dl[index].update({"delivery_index": index, **fields})

    store.update_intent(intent_id, _mut)


def drain_intents(store: Store, *, pid: int, pid_start: object = None,
                  max_per_tick: int = 25, now_epoch: float | None = None,
                  on_note: Callable[[str], None] | None = None) -> dict:
    """One executor pass: claim up to ``max_per_tick`` oldest actionable
    intents (queued, or claimed by a CONFIRMED-DEAD owner), execute each to a
    terminal state, then rotate settled terminals into the control-audit sink.
    Per-intent failures are contained (terminal state=failed), never abort the
    pass. Returns a summary dict."""
    summary = {"examined": 0, "claimed": 0, "applied": 0, "denied": 0,
               "failed": 0, "skipped": 0, "quarantined_invalid": 0}
    kill = store.supervisor_kill_switch()
    if kill is not False:
        summary["disabled"] = True
        summary["disabled_reason"] = "kill_switch" if kill else "kill_switch_unreadable"
        summary["rotation"] = {"rotated": 0, "audit_dropped": 0,
                               "quarantined_invalid": 0}
        return summary
    now = now_epoch if now_epoch is not None else time.time()
    quarantine = store.quarantine_invalid_intents(now_epoch=now)
    summary["quarantined_invalid"] = int(quarantine.get("quarantined") or 0)
    candidates = [r for r in store.list_intents(limit=100000)
                  if r.get("state") not in Store.INTENT_TERMINAL_STATES]
    candidates.sort(key=lambda r: str(r.get("created_at") or ""))
    for rec in candidates[: max(0, int(max_per_tick))]:
        summary["examined"] += 1
        claimed = store.claim_intent(rec["intent_id"], pid=pid,
                                     pid_start=pid_start, now_epoch=now)
        if claimed is None:
            summary["skipped"] += 1
            continue
        summary["claimed"] += 1
        try:
            terminal = _drain_one(store, claimed)
        except Exception as e:  # noqa: BLE001 - one poisoned intent never kills the drain
            store.mark_intent_terminal(rec["intent_id"], state=Store.INTENT_FAILED,
                                       code="executor_error", error=str(e)[:500])
            terminal = Store.INTENT_FAILED
        key = {Store.INTENT_APPLIED: "applied", Store.INTENT_DENIED: "denied"}.get(
            terminal, "failed")
        summary[key] += 1
        if on_note is not None:
            on_note(f"intent {rec['intent_id']}: {terminal}")
    summary["rotation"] = store.rotate_intents(now_epoch=now)
    return summary
