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

from agenttalk import threads as th
from agenttalk.store import CONTROL_KINDS, Store, _new_id

EXECUTOR_MARKER = "dashboard_intent_v2"

INTENT_KINDS = (
    "send", "reply", "propose", "broadcast", "answer_escalation",
    "lead_chat_send",
)

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
    "needs_operator", "human_authorized", "operator_answer", "operator_origin",
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
    elif kind == "broadcast":
        allowed = {"audience", "subject", "body", "message_kind"}
    elif kind == "answer_escalation":
        allowed = {"to_request", "body"}
    else:  # lead_chat_send
        allowed = {"body"}
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
    if kind == "answer_escalation":
        to_request = _str_field(
            payload, "to_request", required=True, max_len=256, errors=errors)
        if to_request and not _safe_prefixed_id(to_request, "esc-"):
            errors.append("field 'to_request' must be a safe esc-* bus id")
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


def _resolve_lead_chat_identities(store: Store) -> tuple[str, str]:
    try:
        return store.lead_chat_identities()
    except ValueError as e:
        raise IntentDenied("lead_chat_identity_denied", str(e)) from e


def _lead_chat_unavailable(liveness: dict) -> IntentDenied:
    detail = (
        liveness.get("reason")
        or liveness.get("detail")
        or "lead is unavailable for lead chat"
    )
    return IntentDenied("lead_unavailable", str(detail))


def lead_chat_stable_meta(store: Store, *, operator: str, lead: str) -> dict:
    return {
        "request_id": store.lead_chat_request_id(operator=operator, lead=lead),
        "lead_chat": "true",
        "operator_identity": operator,
        "operator_facing_lead": lead,
    }


def _resolve_actor_for_record(store: Store, record: dict) -> str | None:
    return resolve_web_actor(store)


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
    elif kind == "lead_chat_send":
        operator, lead = _resolve_lead_chat_identities(store)
        liveness = store.lead_chat_liveness(lead=lead)
        if not liveness.get("available"):
            raise _lead_chat_unavailable(liveness)
        bus_kind = "message"
        subject = "lead chat"
        meta_shape = "lead_chat"
        recipients = [lead]
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
    elif kind == "answer_escalation":
        rid = payload.get("to_request") or ""
        resolved = th.resolve_operator_answer_target(store, actor, rid)
        if not resolved.ok:
            raise IntentDenied(
                resolved.denial_code or "operator_answer_denied",
                resolved.detail,
            )
        recipients = [resolved.recipient or ""]
        bus_kind = "message"
        subject = f"operator answer ({rid})"
        meta_shape = "operator_answer"
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
        "actor": operator if kind == "lead_chat_send" else actor,
        "operator_identity": operator if kind == "lead_chat_send" else None,
        "operator_facing_lead": lead if kind == "lead_chat_send" else None,
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
    if shape == "operator_answer":
        return {
            "request_id": payload.get("to_request") or "",
            "operator_answer": "true",
            "operator_origin": semantic.get("actor") or "",
        }
    if shape == "lead_chat":
        operator = semantic.get("operator_identity") or ""
        lead = semantic.get("operator_facing_lead") or ""
        return lead_chat_stable_meta(store, operator=operator, lead=lead)
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


def _stable_meta_error(store: Store | None, semantic: dict, meta: object) -> str | None:
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
    elif shape == "operator_answer":
        allowed = {"request_id", "operator_answer", "operator_origin"}
    elif shape == "lead_chat":
        allowed = {
            "request_id", "lead_chat", "operator_identity",
            "operator_facing_lead",
        }
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
    if shape == "operator_answer":
        if meta.get("request_id") != payload.get("to_request"):
            return "operator-answer request_id does not match payload.to_request"
        if meta.get("operator_answer") != "true":
            return "operator_answer must be the string 'true'"
        if meta.get("operator_origin") != semantic.get("actor"):
            return "operator_origin does not match the frozen actor"
        return None
    if shape == "lead_chat":
        if not _safe_prefixed_id(meta.get("request_id"), "lc-"):
            return "lead-chat request_id must be a safe lc-* id"
        if store is None:
            return "lead-chat stable meta requires store context"
        if meta.get("request_id") != store.lead_chat_request_id(
            operator=semantic.get("operator_identity") or "",
            lead=semantic.get("operator_facing_lead") or "",
        ):
            return "lead-chat request_id does not match current identities"
        if meta.get("lead_chat") != "true":
            return "lead_chat must be the string 'true'"
        if meta.get("operator_identity") != semantic.get("operator_identity"):
            return "operator_identity does not match current operator identity"
        if meta.get("operator_facing_lead") != semantic.get("operator_facing_lead"):
            return "operator_facing_lead does not match current lead"
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
        meta_error = _stable_meta_error(store, semantic, delivery.get("stable_meta"))
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


def _answer_static_semantic(actor: str, record: dict, delivery: dict) -> dict:
    payload = record.get("payload") or {}
    rid = payload.get("to_request") or ""
    return {
        "kind": "answer_escalation",
        "payload": payload,
        "recipients": [delivery.get("recipient")],
        "bus_kind": "message",
        "subject": f"operator answer ({rid})",
        "body": payload.get("body") or "",
        "meta_shape": "operator_answer",
        "actor": actor,
    }


def _validate_answer_escalation_static_plan(record: dict, plan: object) -> tuple[str, dict]:
    if not isinstance(plan, dict):
        raise _plan_revalidation_failed("plan_shape", "plan must be an object")
    actor = plan.get("actor")
    if not isinstance(actor, str) or not actor:
        raise _plan_revalidation_failed("actor_changed", "plan actor is missing")
    deliveries = plan.get("deliveries")
    if not isinstance(deliveries, list):
        raise _plan_revalidation_failed("plan_shape",
                                        "plan.deliveries must be a list")
    if len(deliveries) != 1:
        raise _plan_revalidation_failed(
            "recipient_drift", "operator answer plan must have exactly one delivery")
    delivery = deliveries[0]
    if not isinstance(delivery, dict):
        raise _plan_revalidation_failed("plan_shape", "delivery 0 must be an object")
    extras = set(delivery) - {"recipient", "bus_kind", "subject", "body", "stable_meta"}
    if extras:
        raise _plan_revalidation_failed(
            "plan_shape", f"delivery 0 has unexpected keys {sorted(extras)}")
    recipient = delivery.get("recipient")
    if not isinstance(recipient, str) or not recipient:
        raise _plan_revalidation_failed(
            "recipient_drift", "delivery 0 recipient must be a non-empty string")
    semantic = _answer_static_semantic(actor, record, delivery)
    if delivery.get("bus_kind") != "message":
        raise _plan_revalidation_failed(
            "bus_kind_drift", "operator answer delivery must be a message")
    subject = delivery.get("subject")
    body = delivery.get("body")
    if not isinstance(subject, str) or len(subject) > _MAX_SUBJECT_CHARS:
        raise _plan_revalidation_failed(
            "body_subject_drift", "delivery 0 subject has invalid shape")
    if not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
        raise _plan_revalidation_failed(
            "body_subject_drift", "delivery 0 body has invalid shape")
    if subject != semantic["subject"] or body != semantic["body"]:
        raise _plan_revalidation_failed(
            "body_subject_drift", "delivery 0 content does not match payload")
    meta_error = _stable_meta_error(None, semantic, delivery.get("stable_meta"))
    if meta_error is not None:
        raise _plan_revalidation_failed(
            "stable_meta_shape", f"delivery 0: {meta_error}")
    return actor, delivery


def _delivery_fp(intent_id: str, delivery_index: int, actor: str, delivery: dict) -> str:
    return delivery_fingerprint(
        intent_id=intent_id, delivery_index=delivery_index, actor=actor,
        recipient=delivery["recipient"], bus_kind=delivery["bus_kind"],
        subject=delivery.get("subject") or "", body=delivery.get("body") or "",
        stable_meta=delivery.get("stable_meta") or {},
    )


def _delivered_record_matches(store: Store, *, intent_id: str, delivery_index: int,
                              actor: str, delivery: dict, state: dict,
                              fingerprint: str) -> bool:
    if state.get("fingerprint") != fingerprint:
        return False
    message_id = state.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return False
    floor = state.get("attempt_floor")
    if isinstance(floor, str) and floor and message_id <= floor:
        return False
    for m in store.valid_messages():
        if m.id != message_id:
            continue
        if (
            m.sender != actor
            or m.recipient != delivery["recipient"]
            or m.kind != delivery["bus_kind"]
            or (m.subject or "") != (delivery.get("subject") or "")
            or (m.body or "") != (delivery.get("body") or "")
        ):
            return False
        meta = m.meta or {}
        stable_meta = delivery.get("stable_meta") or {}
        if any(meta.get(k) != v for k, v in stable_meta.items()):
            return False
        return (
            meta.get("web_intent_id") == intent_id
            and str(meta.get("web_intent_delivery_index")) == str(delivery_index)
            and meta.get("web_intent_fingerprint") == fingerprint
        )
    return False


def _deny_intent(store: Store, intent_id: str, code: str, error: str) -> str:
    store.mark_intent_terminal(
        intent_id, state=Store.INTENT_DENIED, code=code, error=error[:500])
    return Store.INTENT_DENIED


def _resolve_answer_actor(store: Store, request_id: str) -> str | None:
    """Use the operator principal for operator-addressed escalations.

    Existing dashboard attention answers still resolve through the historical
    web actor when the escalation is addressed to an agent liaison.
    """
    try:
        operator = store.operator_identity()
    except ValueError:
        operator = None
    if operator:
        resolved = th.resolve_operator_answer_target(store, operator, request_id)
        if resolved.ok or resolved.denial_code != "not_found":
            return operator
    return resolve_web_actor(store)


def _actor_is_operator(store: Store, actor: str) -> bool:
    try:
        return actor == store.operator_identity()
    except ValueError:
        return False


def _is_lead_chat_origin(record: dict) -> bool:
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    return origin.get("source") == "web-lead-chat"


def _drain_answer_escalation(store: Store, rec: dict) -> str:
    iid = rec["intent_id"]
    errors = validate_intent(rec.get("kind"), rec.get("payload"))
    if errors:
        return _deny_intent(store, iid, "invalid_payload", "; ".join(errors))

    request_id = (rec.get("payload") or {}).get("to_request") or ""
    lead_chat_origin = _is_lead_chat_origin(rec)
    if lead_chat_origin:
        try:
            actor, current_lead = _resolve_lead_chat_identities(store)
        except IntentDenied as e:
            return _deny_intent(store, iid, e.code, e.detail)
    else:
        actor = _resolve_answer_actor(store, request_id)
        current_lead = None
    if actor is not None and _actor_is_operator(store, actor):
        return _deny_intent(
            store, iid, "operator_answer_not_queue_authorized",
            "operator-principal answers are authorized only inside an "
            "authenticated /api/lead-chat request",
        )
    plan = rec.get("plan") if isinstance(rec.get("plan"), dict) else None
    if plan is None:
        if actor is None:
            code, detail = web_actor_denial(store)
            return _deny_intent(store, iid, code, detail)
        try:
            plan = build_plan(store, actor, rec)
        except IntentDenied as e:
            return _deny_intent(store, iid, e.code, e.detail)
        frozen = plan

        def _freeze(r: dict) -> None:
            r["plan"] = frozen

        rec = store.update_intent(iid, _freeze) or rec

    try:
        frozen_actor, delivery = _validate_answer_escalation_static_plan(rec, plan)
    except IntentDenied as e:
        return _deny_intent(store, iid, e.code, e.detail)

    states = list(rec.get("deliveries") or [])
    while len(states) < 1:
        states.append({})
    st = states[0] if isinstance(states[0], dict) else {}
    fp = _delivery_fp(iid, 0, frozen_actor, delivery)
    if st.get("state") == "delivered":
        if not _delivered_record_matches(
            store, intent_id=iid, delivery_index=0, actor=frozen_actor,
            delivery=delivery, state=st, fingerprint=fp,
        ):
            return _deny_intent(
                store, iid, "plan_revalidation_failed",
                "delivered_record_mismatch: stored delivery does not match log")
        store.mark_intent_terminal(iid, state=Store.INTENT_APPLIED)
        return Store.INTENT_APPLIED

    prior_floor = st.get("attempt_floor")
    if isinstance(prior_floor, str) and prior_floor:
        done = _find_completed_send(
            store, intent_id=iid, delivery_index=0, attempt_floor=prior_floor,
            actor=frozen_actor, delivery=delivery,
        )
        if done is not None:
            _record_delivery(store, iid, 0, state="delivered",
                             message_id=done, fingerprint=fp,
                             attempt_floor=prior_floor)
            store.mark_intent_terminal(iid, state=Store.INTENT_APPLIED)
            return Store.INTENT_APPLIED

    if actor is None:
        code, detail = web_actor_denial(store)
        return _deny_intent(store, iid, code, detail)
    if actor != frozen_actor:
        return _deny_intent(
            store, iid, "actor_changed",
            f"resolved actor {actor!r} differs from the frozen plan's "
            f"{frozen_actor!r}; requeue a fresh intent",
        )
    if lead_chat_origin:
        if current_lead is None:
            return _deny_intent(
                store, iid, "lead_chat_identity_denied",
                "lead-chat answer has no current lead identity")
        if frozen_actor != actor:
            return _deny_intent(
                store, iid, "actor_changed",
                f"resolved operator {actor!r} differs from the frozen plan's "
                f"{frozen_actor!r}; requeue a fresh intent",
            )
        if delivery["recipient"] != current_lead:
            return _deny_intent(
                store, iid, "plan_revalidation_failed",
                "recipient_drift: lead-chat answer recipient does not match "
                "the current lead",
            )
        liveness = store.lead_chat_liveness(lead=current_lead)
        if not liveness.get("available"):
            denied = _lead_chat_unavailable(liveness)
            return _deny_intent(store, iid, denied.code, denied.detail)
    try:
        semantic = _resolve_plan_semantics(store, actor, rec)
    except IntentDenied as e:
        return _deny_intent(store, iid, e.code, e.detail)
    if semantic["recipients"] != [delivery["recipient"]]:
        return _deny_intent(
            store, iid, "plan_revalidation_failed",
            "recipient_drift: live recipient does not match frozen plan")
    if _actor_is_operator(store, actor):
        try:
            _operator, resolved_lead = store.lead_chat_identities()
        except ValueError:
            resolved_lead = None
        if resolved_lead and delivery["recipient"] == resolved_lead:
            liveness = store.lead_chat_liveness(lead=resolved_lead)
            if not liveness.get("available"):
                denied = _lead_chat_unavailable(liveness)
                return _deny_intent(store, iid, denied.code, denied.detail)

    floor = _new_id()
    _record_delivery(store, iid, 0, state="attempting", fingerprint=fp,
                     attempt_floor=floor, recipient=delivery["recipient"],
                     bus_kind=delivery["bus_kind"])
    meta = dict(delivery.get("stable_meta") or {})
    meta.update({
        "web_intent_id": iid,
        "web_intent_delivery_index": "0",
        "web_intent_fingerprint": fp,
        "web_intent_attempt_floor": floor,
        "executor_marker": EXECUTOR_MARKER,
    })
    result = store.send_operator_answer_atomic(
        actor=actor,
        request_id=(rec.get("payload") or {}).get("to_request") or "",
        body=delivery.get("body") or "",
        subject=delivery.get("subject") or "",
        extra_meta=meta,
        expected_recipient=delivery["recipient"],
    )
    if not result.ok:
        code = result.denial_code or "operator_answer_denied"
        if result.failed:
            store.mark_intent_terminal(iid, state=Store.INTENT_FAILED,
                                       code=code, error=result.detail[:500])
            return Store.INTENT_FAILED
        store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                   code=code, error=result.detail[:500])
        return Store.INTENT_DENIED
    msg = result.message
    if msg is None:
        store.mark_intent_terminal(
            iid, state=Store.INTENT_FAILED,
            code="operator_answer_send_inconclusive",
            error="atomic operator-answer helper succeeded without a message",
        )
        return Store.INTENT_FAILED
    _record_delivery(store, iid, 0, state="delivered", message_id=msg.id,
                     fingerprint=fp, attempt_floor=floor)
    store.mark_intent_terminal(iid, state=Store.INTENT_APPLIED)
    return Store.INTENT_APPLIED


def _drain_one(store: Store, rec: dict) -> str:
    """Execute one CLAIMED intent to a terminal state. Returns the terminal
    state string. Every authority decision happens HERE, server-side."""
    if rec.get("kind") == "answer_escalation":
        return _drain_answer_escalation(store, rec)
    iid = rec["intent_id"]
    errors = validate_intent(rec.get("kind"), rec.get("payload"))
    if errors:
        store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                   code="invalid_payload",
                                   error="; ".join(errors)[:500])
        return Store.INTENT_DENIED
    if rec.get("kind") == "lead_chat_send":
        store.mark_intent_terminal(
            iid, state=Store.INTENT_DENIED,
            code="lead_chat_send_not_queue_authorized",
            error="lead_chat_send is authorized only inside an authenticated "
                  "/api/lead-chat request",
        )
        return Store.INTENT_DENIED
    try:
        actor = _resolve_actor_for_record(store, rec)
    except IntentDenied as e:
        store.mark_intent_terminal(iid, state=Store.INTENT_DENIED,
                                   code=e.code, error=e.detail)
        return Store.INTENT_DENIED
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
