"""Operator attention queue (0.56.0): typed escalate fields, a derived ranked/deduped
read-only view over existing signals, and a durable operator disposition log.

This module is the PURE core + the disposition IO. It stays on existing rails: it defines
no new message kind, creates no work/task objects, and mutates nothing except the
disposition log (and, from the CLI layer, the dead-letter resolved sidecar). Dispositions
annotate OPERATOR ATTENTION only - never "the underlying work is done".

Layering:
- Pure functions (validate/parse typed meta, item_id, source_hash, rank_key, dedupe_key,
  disposition validate + snapshot-bound fold) - fully unit-testable with fakes.
- Disposition IO (append-only JSONL, fsync, skip-invalid reader) - mirrors knowledge.py's
  write_event_locked/read_events under store._config_lock().
- The source PROJECTION (reading needs_operator/config_blocked/dead-letter/gate/close/
  capacity/lead_unarmed) lives in the CLI-facing collector and feeds these pure helpers;
  each source read is independently fail-safe so one bad source never blanks the queue.

Load-bearing invariants (lead gate conditions):
- Snapshot-bound dispositions: a disposition applies ONLY while the source_hash it was made
  against still matches; a CHANGED source resurfaces the item (never hide a later different
  problem under the same identity key).
- Fail-safe reader: a needs_operator escalation is NEVER hidden because meta.attention is
  malformed, a disposition line is torn, or a source read failed.
- dismiss is forbidden for blocking sources; dead-letter uses resolve, not dismiss.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenttalk._jsonl import append_record, iter_lines
from agenttalk import ephemeral as eph
from agenttalk.store import validate_agent_name

SCHEMA_VERSION = 1

# --- typed meta.attention enums (exact) ---
PRIORITIES = ("low", "normal", "high", "urgent")
RISK_LEVELS = ("low", "medium", "high")
CONFIDENCE_LEVELS = ("low", "medium", "high")

# --- field caps (single-line typed strings; detail belongs in the body) ---
_CAP_DECISION = 500
_CAP_WHY = 500
_CAP_RECOMMENDATION = 500
_CAP_RISK_IF_IGNORED = 1000
_CAP_OPTION = 300
_MAX_OPTIONS = 10
_CAP_AFFECTED = 200
_MAX_AFFECTED = 20

# --- sources ---
SOURCE_NEEDS_OPERATOR = "needs_operator"
SOURCE_CONFIG_BLOCKED = "config_blocked"
SOURCE_PROCESS_TREE_HOLD = "process_tree_hold"
SOURCE_DEAD_LETTER = "dead_letter"
SOURCE_GATE_HOLD = "gate_hold"
SOURCE_CLOSE_HOLD = "close_hold"
SOURCE_LEAD_UNARMED = "lead_unarmed"
SOURCE_CAPACITY = "capacity"
SOURCE_COORDINATION_STALL = "coordination_stall"
SOURCE_ERROR = "source_error"

# rank weight per source (higher = more urgent to a human)
_SOURCE_WEIGHT = {
    SOURCE_NEEDS_OPERATOR: 100,
    SOURCE_PROCESS_TREE_HOLD: 95,
    SOURCE_CONFIG_BLOCKED: 90,
    SOURCE_COORDINATION_STALL: 85,
    SOURCE_DEAD_LETTER: 80,
    SOURCE_GATE_HOLD: 70,
    SOURCE_CLOSE_HOLD: 70,
    SOURCE_LEAD_UNARMED: 60,
    SOURCE_CAPACITY: 20,
    SOURCE_ERROR: 10,
}

# Blocking sources: a human must repair/answer/waive/defer - NEVER dismiss (dismiss would
# silently hide a real need). config/gate/close/lead_unarmed are dismissable ONLY when the
# collector classifies that specific item advisory (see item["advisory"]).
_ALWAYS_BLOCKING = frozenset({
    SOURCE_NEEDS_OPERATOR,
    SOURCE_PROCESS_TREE_HOLD,
    SOURCE_DEAD_LETTER,
})
_ADVISORY_CAPABLE = frozenset({
    SOURCE_CONFIG_BLOCKED,
    SOURCE_GATE_HOLD,
    SOURCE_CLOSE_HOLD,
    SOURCE_LEAD_UNARMED,
    SOURCE_COORDINATION_STALL,
})
# capacity is advisory by nature; source_error is defer-only (fix the reader).

# --- disposition actions ---
ACTION_DEFER = "defer"
ACTION_DISMISS = "dismiss"
ACTION_ANSWERED_ELSEWHERE = "answered_elsewhere"
ACTION_RESOLVE_DEAD_LETTER = "resolve_dead_letter"
ACTION_REQUEUED_AFTER_RESOLVE = "requeued_after_resolve"
_DISPOSITION_ACTIONS = frozenset({
    ACTION_DEFER, ACTION_DISMISS, ACTION_ANSWERED_ELSEWHERE,
    ACTION_RESOLVE_DEAD_LETTER, ACTION_REQUEUED_AFTER_RESOLVE,
})
# action family: the "latest valid wins" fold is per (item_id, family). defer and the
# terminal actions share a family so the newest operator decision on an item wins.
_ACTION_FAMILY = {
    ACTION_DEFER: "disposition",
    ACTION_DISMISS: "disposition",
    ACTION_ANSWERED_ELSEWHERE: "disposition",
    ACTION_RESOLVE_DEAD_LETTER: "dead_letter_resolution",
    ACTION_REQUEUED_AFTER_RESOLVE: "dead_letter_resolution",
}

NOTICE_DEAD_LETTER = "dead_letter"


# ----------------------------------------------------------- typed meta validation

def _is_single_line_str(v: Any) -> bool:
    return isinstance(v, str) and "\n" not in v and "\r" not in v


def validate_attention_meta(meta: dict | None) -> list[str]:
    """STRICT validation of a typed ``meta.attention`` block for the CLI-write path.
    Returns a list of error strings ([] == valid). An ABSENT block is valid (returns []);
    only a PRESENT block is validated. The reader side (:func:`parse_attention_meta`) is
    separately fail-safe - it never rejects, only downgrades.

    Requires the WRAPPED form (``meta`` with an ``attention`` key). A ``meta`` WITHOUT an
    ``attention`` key means 'no typed block' and validates clean - we never treat an arbitrary
    unwrapped dict AS the attention block, so a future caller passing a full message ``meta``
    with unrelated ``priority``/``options`` keys does not get spurious errors (fable-max #3)."""
    if not meta:
        return []
    att = meta.get("attention")
    if att is None:
        return []
    errs: list[str] = []
    if not isinstance(att, dict):
        return ["attention must be an object"]
    sv = att.get("schema_version", SCHEMA_VERSION)
    if sv != SCHEMA_VERSION:
        errs.append(f"unsupported schema_version {sv!r} (expected {SCHEMA_VERSION})")

    def _capped(key: str, cap: int) -> None:
        v = att.get(key)
        if v is None:
            return
        if not _is_single_line_str(v):
            errs.append(f"{key} must be a single-line string")
        elif len(v.encode("utf-8")) > cap:
            errs.append(f"{key} exceeds {cap} bytes")

    _capped("decision", _CAP_DECISION)
    _capped("why_it_matters", _CAP_WHY)
    _capped("recommendation", _CAP_RECOMMENDATION)
    _capped("risk_if_ignored", _CAP_RISK_IF_IGNORED)

    def _enum(key: str, allowed: tuple) -> None:
        v = att.get(key)
        if v is not None and v not in allowed:
            errs.append(f"{key} must be one of {allowed} (got {v!r})")

    _enum("priority", PRIORITIES)
    _enum("risk_severity", RISK_LEVELS)
    _enum("confidence", CONFIDENCE_LEVELS)

    opts = att.get("options")
    if opts is not None:
        if not isinstance(opts, list):
            errs.append("options must be a list")
        else:
            if len(opts) > _MAX_OPTIONS:
                errs.append(f"too many options (max {_MAX_OPTIONS})")
            for o in opts:
                if not _is_single_line_str(o) or len(str(o).encode("utf-8")) > _CAP_OPTION:
                    errs.append(f"each option must be a single-line string <= {_CAP_OPTION} bytes")
                    break

    aff = att.get("affected")
    if aff is not None:
        if not isinstance(aff, list):
            errs.append("affected must be a list")
        else:
            if len(aff) > _MAX_AFFECTED:
                errs.append(f"too many affected refs (max {_MAX_AFFECTED})")
            for a in aff:
                if not _is_single_line_str(a) or len(str(a).encode("utf-8")) > _CAP_AFFECTED:
                    errs.append(f"each affected ref must be a single-line string <= {_CAP_AFFECTED} bytes")
                    break

    nb = att.get("needed_by")
    if nb is not None and not _is_valid_needed_by(nb):
        errs.append("needed_by must be an ISO-8601 date or datetime "
                    "(a naive datetime is treated as UTC)")
    return errs


def parse_iso_dt(v: Any):
    """ISO-8601 date or datetime -> a tz-aware UTC ``datetime``, or ``None`` if unparseable.
    FAIL-SAFE (never raises). Handles a trailing ``Z``, a date-only value (midnight UTC),
    and Python 3.10's cap of 3/6-digit fractional seconds (trims extra digits). Shared by
    the ``--needed-by`` / ``--until`` validators and the defer-expiry read path so a
    malformed persisted timestamp is never string-compared into hiding a blocking item."""
    import re
    from datetime import datetime, timezone
    if not isinstance(v, str) or not v.strip():
        return None
    norm = v.strip().replace("Z", "+00:00")
    for candidate in (norm, re.sub(r"(\.\d{6})\d+", r"\1", norm)):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def _is_valid_needed_by(v: Any) -> bool:
    return parse_iso_dt(v) is not None


_ATT_FIELDS = ("decision", "why_it_matters", "options", "recommendation",
               "risk_if_ignored", "risk_severity", "confidence", "priority",
               "needed_by", "affected")


def parse_attention_meta(meta: dict | None) -> tuple[dict, list[str]]:
    """FAIL-SAFE reader-side parse. Returns (fields, warnings). NEVER raises and NEVER
    rejects: a malformed / future / absent block yields safe defaults + a warning so the
    escalation still surfaces. This is the reader analogue of the strict CLI validator."""
    out = {"decision": None, "why_it_matters": None, "options": [], "recommendation": None,
           "risk_if_ignored": None, "risk_severity": "unknown", "confidence": "unknown",
           "priority": "unknown", "needed_by": None, "affected": []}
    warnings: list[str] = []
    if not meta:
        return out, warnings
    att = meta.get("attention")
    if att is None:
        return out, warnings
    if not isinstance(att, dict):
        return out, ["typed_fields_warning"]
    if validate_attention_meta({"attention": att}):
        warnings.append("typed_fields_warning")
        # still salvage the enum/scalar fields that ARE well-typed, best-effort
    for k in ("decision", "why_it_matters", "recommendation", "risk_if_ignored", "needed_by"):
        v = att.get(k)
        if _is_single_line_str(v):
            out[k] = v
    if isinstance(att.get("options"), list):
        out["options"] = [o for o in att["options"] if isinstance(o, str)]
    if isinstance(att.get("affected"), list):
        out["affected"] = [a for a in att["affected"] if isinstance(a, str)]
    if att.get("priority") in PRIORITIES:
        out["priority"] = att["priority"]
    if att.get("risk_severity") in RISK_LEVELS:
        out["risk_severity"] = att["risk_severity"]
    if att.get("confidence") in CONFIDENCE_LEVELS:
        out["confidence"] = att["confidence"]
    return out, warnings


# ----------------------------------------------------------- item identity + hashing

def item_id(source: str, *parts: str) -> str:
    """Canonical source primary key, e.g. needs_operator:<request_id>,
    dead_letter:<agent>:<message_id>, config_blocked:<agent>, gate_hold:<scope>:<gate>,
    close_hold:<close_id>:<hold_code>, lead_unarmed:<agent>, capacity:<agent>:<kind>."""
    return ":".join([source, *[str(p) for p in parts]])


def source_hash(payload: Any) -> str:
    """Stable content hash over the source's IDENTIFYING CONTENT (NOT just the identity
    key). This is what binds a disposition to the state it was made against: a config_blocked
    hold's reason, a needs_operator decision, a gate's blocking set, etc. When this changes,
    a prior disposition is stale and the item resurfaces (gate condition 1)."""
    norm = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    # Persisted state may contain an escaped lone surrogate.  Source projectors
    # validate fields before exposing them, but hashing malformed evidence must
    # still isolate the bad record instead of erasing the whole attention source.
    return hashlib.sha256(norm.encode("utf-8", errors="surrogatepass")).hexdigest()


def _notice_log_path(store) -> Path:
    # Attention-owned notification memory, not a dead-letter sink sidecar.
    return attention_dir(store) / "notices.jsonl"


def dead_letter_notice_key(agent: str, message_id: str,
                           generation: str | None = None) -> str:
    base = item_id(SOURCE_DEAD_LETTER, agent, message_id)
    return f"{base}:gen:{generation or 'unknown'}"


def dead_letter_notice_state(info: dict, *, disposed: bool) -> dict:
    """Stable tuple used to dedupe wrapper dead-letter/backstop notices."""
    failure_class = info.get("failure_class") or info.get("class") or ""
    infra_exhausted = bool(info.get("infra_exhausted") or failure_class == "infra_retry_exhausted")
    return {
        "failure_class": str(failure_class),
        "attempts_bucket": str(info.get("attempts_bucket") or info.get("attempts") or ""),
        "disposed": bool(disposed),
        "infra_exhausted": infra_exhausted,
        "quarantined": bool(info.get("quarantined")),
    }


def dead_letter_entry_notice_state(entry: dict) -> dict:
    """State tuple for a dead-letter sink row, matching wrapper notice state."""
    info = dict(entry or {})
    info.setdefault("failure_class", info.get("class"))
    info.setdefault("attempts_bucket", "quarantined")
    info["quarantined"] = True
    return dead_letter_notice_state(info, disposed=True)


def dead_letter_entry_source_hash(entry: dict) -> str:
    return source_hash(dead_letter_entry_notice_state(entry))


def read_notice_events(store) -> tuple[list[dict], list[str]]:
    """Read attention notice events fail-safe.

    Torn/corrupt lines produce warnings but never hide a notice path. Valid prior lines
    are still returned so a corrupt append fails open to one replacement notice rather
    than crashing or suppressing all future notices.
    """
    p = _notice_log_path(store)
    if not p.exists():
        return [], []
    events: list[dict] = []
    warnings: list[str] = []
    try:
        for idx, line in iter_lines(p):
            if line is None:
                warnings.append(f"notice_log_utf8:{idx}")
                continue
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"notice_log_torn:{idx}")
                continue
            if not isinstance(obj, dict):
                warnings.append(f"notice_log_malformed:{idx}")
                continue
            events.append(obj)
    except (OSError, UnicodeError) as e:
        warnings.append(f"notice_log_unreadable:{e}")
    return events, warnings


def latest_dead_letter_notices(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in events:
        if e.get("kind") != NOTICE_DEAD_LETTER:
            continue
        key = e.get("notice_key")
        if isinstance(key, str) and key:
            out[key] = e
    return out


def append_notice_event(store, event: dict) -> None:
    with store._config_lock():
        append_record(_notice_log_path(store), event, default=str)


def dead_letter_resolved_for_state(store, *, agent: str, message_id: str,
                                   source_hash_value: str) -> bool:
    events, _ = read_dispositions(store)
    folded = fold_dispositions(events)
    iid = item_id(SOURCE_DEAD_LETTER, agent, message_id)
    dl = folded.get(iid, {}).get("dead_letter_resolution")
    return bool(
        dl
        and dl.get("action") == ACTION_RESOLVE_DEAD_LETTER
        and dl.get("source_snapshot", {}).get("source_hash") == source_hash_value
    )


def should_emit_dead_letter_notice(store, *, agent: str, message_id: str,
                                   generation: str | None,
                                   state: dict) -> tuple[bool, dict]:
    """Decide if the wrapper should send a needs-operator dead-letter notice.

    Only ``dead-letter resolve`` releases the latch. Closing or replying on the escalation
    thread is intentionally ignored.
    """
    key = dead_letter_notice_key(agent, message_id, generation)
    state_hash = source_hash(state)
    if dead_letter_resolved_for_state(
            store, agent=agent, message_id=message_id,
            source_hash_value=state_hash):
        return False, {"reason": "resolved"}
    events, warnings = read_notice_events(store)
    prior = latest_dead_letter_notices(events).get(key)
    if prior and prior.get("state_hash") == state_hash:
        return False, {"reason": "duplicate", "request_id": prior.get("request_id")}
    return True, {"notice_key": key, "state_hash": state_hash, "warnings": warnings}


def dedupe_key(source: str, *, identity: str, decision_hash: str | None = None) -> str:
    """Grouping key for DISPLAY only (distinct from item_id, which is the disposition key).
    Similar needs_operator escalations group by identity + a decision/content hash so two
    DISTINCT decisions with the same sender/subject never merge. Non-escalation sources
    group by their identity (config_blocked:<agent>, dead_letter:<agent>:<msg>, ...)."""
    if decision_hash:
        return f"{source}:{hashlib.sha256((identity + '|' + decision_hash).encode()).hexdigest()}"
    return f"{source}:{identity}"


# ----------------------------------------------------------- disposition events

@dataclass(frozen=True)
class Disposition:
    action: str
    item_id: str
    source: str
    actor: str
    reason: str
    at: str
    source_hash: str
    until: str | None = None
    evidence: str | None = None
    refs: list = field(default_factory=list)


def validate_disposition_event(evt: Any) -> bool:
    """Well-formedness for a persisted disposition line (reader-side, fail-safe)."""
    if not isinstance(evt, dict):
        return False
    if evt.get("action") not in _DISPOSITION_ACTIONS:
        return False
    for key in ("item_id", "actor", "at"):
        if not isinstance(evt.get(key), str) or not evt[key]:
            return False
    # reason is required for every operator disposition (audit)
    if not isinstance(evt.get("reason"), str) or not evt["reason"].strip():
        return False
    snap = evt.get("source_snapshot")
    if not isinstance(snap, dict) or not isinstance(snap.get("source_hash"), str):
        return False
    return True


def allowed_action_for_source(action: str, source: str, *, advisory: bool) -> bool:
    """dismiss is forbidden for blocking sources (gate condition 7). dead-letter uses
    resolve_dead_letter, not dismiss. source_error is defer-only."""
    if action == ACTION_RESOLVE_DEAD_LETTER or action == ACTION_REQUEUED_AFTER_RESOLVE:
        return source == SOURCE_DEAD_LETTER
    if action == ACTION_ANSWERED_ELSEWHERE:
        return source == SOURCE_NEEDS_OPERATOR
    if action == ACTION_DEFER:
        return True  # any source may be deferred
    if action == ACTION_DISMISS:
        if source == SOURCE_CAPACITY:
            return True
        if source in _ALWAYS_BLOCKING:
            return False
        if source in _ADVISORY_CAPABLE:
            return advisory  # only when this specific item is advisory-classified
        if source == SOURCE_ERROR:
            return False
        return False
    return False


def fold_dispositions(events: list[dict]) -> dict[str, dict]:
    """Latest-VALID event wins per (item_id, action family). Returns
    {item_id: {family: event}}. Order-preserving: later lines override earlier ones.
    Snapshot-match gating is applied at read/projection time (a folded disposition applies
    ONLY while the live source_hash matches its snapshot) - this fold just resolves the
    newest operator intent."""
    latest: dict[str, dict] = {}
    for evt in events:
        if not validate_disposition_event(evt):
            continue
        iid = evt["item_id"]
        fam = _ACTION_FAMILY.get(evt["action"], "disposition")
        latest.setdefault(iid, {})[fam] = evt
    return latest


def apply_disposition(item: dict, folded: dict[str, dict], *, now_iso: str) -> dict:
    """Given a live item (with source + source_hash + advisory) and the folded dispositions,
    compute the item's attention state. SNAPSHOT-BOUND: a disposition applies only while the
    live source_hash matches the snapshot it was recorded against; a changed source
    resurfaces the item as active with a prior_disposition_stale warning (gate condition 1).
    A deferred item resurfaces when ``until`` passes OR the source changed, whichever first.
    NEVER hides a needs_operator item on a torn/absent disposition (fail-safe)."""
    item = dict(item)
    item.setdefault("state", "active")
    item.setdefault("warnings", [])
    fams = folded.get(item["item_id"])
    if not fams:
        return item
    live_hash = item.get("source_hash")
    advisory = bool(item.get("advisory", False))
    src = item.get("source", "")
    # RE-ENFORCE the write-side legitimacy guard at READ time (gate 2 / reviewer-3 P3): the
    # dispositions.jsonl is untrusted input (a torn-then-rewritten, hand-edited, or forged
    # line is well-formed yet illegitimate). A folded action is applied ONLY if it is allowed
    # for THIS item's source; an illegitimate one is IGNORED (item stays ACTIVE) with a
    # warning, so e.g. a forged dismiss/resolve can never hide a needs_operator/dead_letter.
    # dead-letter resolution family
    dl = fams.get("dead_letter_resolution")
    if dl and dl["action"] == ACTION_RESOLVE_DEAD_LETTER:
        if not allowed_action_for_source(ACTION_RESOLVE_DEAD_LETTER, src, advisory=advisory):
            item["warnings"] = [*item["warnings"], "ignored_illegitimate_disposition"]
        elif dl.get("source_snapshot", {}).get("source_hash") == live_hash:
            item["state"] = "resolved"
            return item
        else:
            item["warnings"] = [*item["warnings"], "prior_disposition_stale"]
    disp = fams.get("disposition")
    if not disp:
        return item
    action = disp["action"]
    if not allowed_action_for_source(action, src, advisory=advisory):
        # illegitimate for this source -> IGNORE (keep active), never hide a blocking item.
        item["warnings"] = [*item["warnings"], "ignored_illegitimate_disposition"]
        return item
    snap_hash = disp.get("source_snapshot", {}).get("source_hash")
    if snap_hash != live_hash:
        # the source changed since the operator acted -> the disposition is stale; the
        # item resurfaces ACTIVE so a later, different problem is never hidden.
        item["warnings"] = [*item["warnings"], "prior_disposition_stale"]
        return item
    if action == ACTION_DEFER:
        until_raw = disp.get("until")
        until_dt = parse_iso_dt(until_raw)
        now_dt = parse_iso_dt(now_iso)
        # A malformed/unparseable persisted `until` (or an expired one) resurfaces the item
        # ACTIVE - a bad timestamp must NEVER string-compare a blocking item into hiding
        # indefinitely (codex F2). Only a valid, still-future `until` defers.
        if until_dt is None or now_dt is None or now_dt >= until_dt:
            if until_raw and until_dt is None:
                item["warnings"] = [*item["warnings"], "invalid_defer_until"]
            return item
        item["state"] = "deferred"
        item["deferred_until"] = until_raw
    elif action == ACTION_DISMISS:
        item["state"] = "dismissed"
    elif action == ACTION_ANSWERED_ELSEWHERE:
        item["state"] = "answered_elsewhere"
    item["disposition_reason"] = disp.get("reason")
    return item


# ----------------------------------------------------------- ranking

def rank_key(item: dict) -> tuple:
    """Deterministic rank tuple where HIGHER is more urgent, computed only from validated
    meta + observed state (no prose scoring, no model inference). :func:`sort_items` orders by
    the NEGATED tuple (so every weight sorts descending) then ``item_id`` ascending, giving a
    stable, fully deterministic order under tied inputs."""
    state = item.get("state", "active")
    active = state in ("active",)  # deferred/dismissed/resolved sink below active
    prio = {"urgent": 4, "high": 3, "normal": 2, "low": 1}.get(item.get("priority"), 1)
    risk = {"high": 3, "medium": 2, "low": 1}.get(item.get("risk_severity"), 0)
    src = _SOURCE_WEIGHT.get(item.get("source"), 0)
    needed = _needed_by_weight(item.get("needed_by"))
    age_bucket = min(int(item.get("age_seconds", 0) // 3600), 72)
    return (
        1 if active else 0,
        1 if item.get("human_can_unblock_now") else 0,
        prio, risk, src, needed, age_bucket,
    )


def _needed_by_weight(needed_by: Any) -> int:
    """Urgency bucket from a ``needed_by`` deadline vs now: 3 overdue, 2 due within 24h,
    1 later, 0 absent/unparseable. Reuses the shared :func:`parse_iso_dt` (a naive datetime
    is treated as UTC)."""
    nb = parse_iso_dt(needed_by)
    if nb is None:
        return 0
    from datetime import datetime, timezone
    delta_h = (nb - datetime.now(timezone.utc)).total_seconds() / 3600.0
    if delta_h < 0:
        return 3      # overdue
    if delta_h <= 24:
        return 2      # due soon
    return 1          # due later


def sort_items(items: list[dict]) -> list[dict]:
    """Rank descending by the weight tuple, then item_id ascending for a stable, fully
    deterministic order under tied inputs."""
    return sorted(items, key=lambda it: (tuple(-w for w in rank_key(it)), it.get("item_id", "")))


# ----------------------------------------------------------- disposition IO (durable)

def attention_dir(store) -> Path:
    """.agenttalk/attention/ - OUTSIDE messages/state/sessions, so it is preserved by both
    ``reset`` and ``reset --archive`` (operator memory survives a reset)."""
    return store.dir / "attention"


def dispositions_path(store) -> Path:
    return attention_dir(store) / "dispositions.jsonl"


def write_disposition_locked(store, event: dict) -> None:
    """Append ONE disposition event durably. Caller MUST hold store._config_lock().
    Append-only + fsync + best-effort dir-fsync - the exact knowledge.write_event_locked
    contract, so the reader's skip-invalid/torn-tail tolerance is preserved."""
    append_record(dispositions_path(store), event)


def append_disposition(store, event: dict) -> None:
    """Public append path: take the shared store lock, then durably write."""
    with store._config_lock():
        write_disposition_locked(store, event)


def _mk_item(source: str, iid: str, *, title: str, ident_content: Any,
             human_can_unblock_now: bool, advisory: bool = False,
             age_seconds: float = 0.0, source_refs: list | None = None,
             fields: dict | None = None, warnings: list | None = None) -> dict:
    """Assemble one queue item + its content-bound source_hash (gate 1)."""
    it = {"item_id": iid, "source": source, "title": title, "state": "active",
          "source_hash": source_hash(ident_content),
          "human_can_unblock_now": human_can_unblock_now, "advisory": advisory,
          "age_seconds": age_seconds, "source_refs": source_refs or [],
          "warnings": list(warnings or [])}
    it.update({"decision": None, "why_it_matters": None, "options": [], "recommendation": None,
               "risk_if_ignored": None, "risk_severity": "unknown", "confidence": "unknown",
               "priority": "unknown", "needed_by": None, "affected": []})
    if fields:
        it.update(fields)
    return it


def needs_operator_items(pending: list[dict]) -> list[dict]:
    """Each entry: {request_id, subject, sender, age_seconds, meta, prompt_excerpt?}.

    The typed fields come from the opener's meta.attention via the FAIL-SAFE parse
    (a malformed block still surfaces the escalation with a warning - gate 2).
    ``prompt_excerpt`` is optional and caller-sanitized; the core projection only
    carries it so UI surfaces with an answer box can show what is being answered.
    human_can_unblock_now=True (a worker is literally blocked on the operator's answer).
    """
    out = []
    for e in pending:
        fields, warns = parse_attention_meta(e.get("meta") or {})
        extra_fields = {**fields, "requester": e.get("sender", "")}
        if isinstance(e.get("prompt_excerpt"), str) and e.get("prompt_excerpt"):
            extra_fields["prompt_excerpt"] = e["prompt_excerpt"]
        rid = e.get("request_id", "")
        dh = hashlib.sha256((fields.get("decision") or e.get("subject") or "").encode()).hexdigest()
        it = _mk_item(SOURCE_NEEDS_OPERATOR, item_id(SOURCE_NEEDS_OPERATOR, rid),
                      title=fields.get("decision") or e.get("subject") or "operator input needed",
                      ident_content={"rid": rid, "decision": fields.get("decision"),
                                     "subject": e.get("subject")},
                      human_can_unblock_now=True, age_seconds=float(e.get("age_seconds") or 0),
                      source_refs=[{"kind": "message", "request_id": rid}],
                      fields=extra_fields,
                      warnings=warns)
        it["dedupe_key"] = dedupe_key(SOURCE_NEEDS_OPERATOR,
                                      identity=f"{e.get('sender', '')}|{e.get('subject', '')}",
                                      decision_hash=dh)
        out.append(it)
    return out


def config_blocked_items(holds: list[dict]) -> list[dict]:
    """Each hold: {agent, summary, at}. Content-bound on the summary, so a DIFFERENT
    config fault for the same agent resurfaces despite a prior disposition (gate 1)."""
    out = []
    for h in holds:
        ag = h.get("agent", "")
        it = _mk_item(SOURCE_CONFIG_BLOCKED, item_id(SOURCE_CONFIG_BLOCKED, ag),
                      title=f"config-blocked hold: {ag}",
                      ident_content={"agent": ag, "summary": h.get("summary")},
                      human_can_unblock_now=True,
                      fields={"why_it_matters": h.get("summary") or "",
                              "priority": "high", "risk_severity": "high"},
                      source_refs=[{"kind": "config_blocked", "agent": ag}])
        it["dedupe_key"] = dedupe_key(SOURCE_CONFIG_BLOCKED, identity=ag)
        out.append(it)
    return out


def configured_process_tree_hold_agents(state: dict) -> list[str]:
    """Return only validated configured agents with a current strict HOLD."""
    agents = state.get("agents") if isinstance(state, dict) else None
    if not isinstance(agents, dict):
        return []
    held: list[str] = []
    for raw_agent, row in agents.items():
        if not isinstance(row, dict):
            continue
        tree = row.get("owned_process_tree")
        if not (
            isinstance(tree, dict)
            and tree.get("status") in {"invalid", "truncated"}
        ):
            continue
        try:
            held.append(validate_agent_name(raw_agent))
        except (TypeError, ValueError):
            continue
    return sorted(held)


def _process_tree_identity_warning(tree: dict) -> str | None:
    """Describe identities the reset command cannot prove from its own list."""
    omitted = tree.get("omitted_count")
    omitted_count = (
        omitted
        if isinstance(omitted, int)
        and not isinstance(omitted, bool)
        and 0 < omitted <= 1_000_000
        else 0
    )
    omitted_over_display_cap = (
        isinstance(omitted, int)
        and not isinstance(omitted, bool)
        and omitted > 1_000_000
    )
    rejected = tree.get("rejected_count")
    rejected_count = (
        rejected
        if isinstance(rejected, int)
        and not isinstance(rejected, bool)
        and 0 < rejected <= 1_000_000
        else 0
    )
    rejected_over_display_cap = (
        isinstance(rejected, int)
        and not isinstance(rejected, bool)
        and rejected > 1_000_000
    )
    omitted_text = (
        f"omits {omitted_count} observed "
        f"{'identity' if omitted_count == 1 else 'identities'}"
        if omitted_count
        else (
            "omits >1,000,000 identities"
            if omitted_over_display_cap
            else ""
        )
    )
    rejected_text = (
        f"excludes {rejected_count} candidate "
        f"{'identity' if rejected_count == 1 else 'identities'}"
        if rejected_count
        else (
            "excludes >1,000,000 candidates"
            if rejected_over_display_cap
            else ""
        )
    )
    if omitted_text and rejected_text:
        warning = (
            f"Reset evidence {omitted_text} and {rejected_text} outside the "
            "reset command's identity list. Operator must confirm them gone "
            "separately."
        )
    elif rejected_text:
        pronoun = "it" if rejected_count == 1 else "them"
        warning = (
            f"Reset evidence {rejected_text} outside the reset command's "
            f"identity list. Operator must confirm {pronoun} gone separately."
        )
    elif omitted_text:
        warning = (
            f"Reset evidence {omitted_text}. Operator must confirm omitted "
            "identities gone separately."
        )
    else:
        warning = ""
    if "rejected_count" not in tree:
        omitted_clause = f" and {omitted_text}" if omitted_text else ""
        return (
            "Ownership record carries no rejected-candidate accounting "
            f"(UNKNOWN, not zero){omitted_clause}. Operator must confirm "
            "unlisted identities gone separately."
        )
    if (
        not isinstance(rejected, int)
        or isinstance(rejected, bool)
        or rejected < 0
    ):
        omitted_clause = f" and {omitted_text}" if omitted_text else ""
        return (
            "Ownership record has invalid rejected-candidate accounting "
            f"(UNKNOWN, not zero){omitted_clause}. Operator must confirm "
            "unlisted identities gone separately."
        )
    return warning or None


def process_tree_hold_items(
    state: dict,
    *,
    supervisor_config: dict | None = None,
    root: str | Path | None = None,
    restart_requests: dict[str, dict] | None = None,
    reset_admissions: dict | None = None,
) -> list[dict]:
    """Project strict supervisor process-tree HOLDs into global human attention.

    A truncated/invalid tree deliberately authorizes no automated teardown.
    This durable projection prevents that fail-closed decision from becoming
    silent immunity for a runaway process tree.  ``reset_admissions`` is the
    read-only result of the reset command's own current preconditions; when it
    is absent this projector names no scripted command and makes no claim that
    none exists.  The configured detached argv remains recovery information,
    never launch authority.
    """
    from agenttalk import supervisor as supervisor_mod

    agents = state.get("agents") if isinstance(state, dict) else None
    admissions = (
        reset_admissions.get("admissions")
        if isinstance(reset_admissions, dict)
        and isinstance(reset_admissions.get("admissions"), dict)
        else {}
    )
    blocked_admissions = (
        reset_admissions.get("blocked_admissions")
        if isinstance(reset_admissions, dict)
        and reset_admissions.get("evaluated") is True
        and isinstance(reset_admissions.get("blocked_admissions"), dict)
        else {}
    )
    admissions_evaluated = bool(
        isinstance(reset_admissions, dict)
        and reset_admissions.get("evaluated") is True
    )
    out: list[dict] = []

    def append_hold(
        *,
        identity: str,
        agent: str,
        row: dict,
        request_id: str | None = None,
    ) -> None:
        tree = row.get("owned_process_tree")
        tree_record = tree if isinstance(tree, dict) else {}
        status = tree_record.get("status")
        hold_reason = row.get("process_tree_hold_reason")
        if (
            status not in {"truncated", "invalid"}
            and request_id is not None
            and "process_tree_hold_reason" in row
        ):
            # Ephemeral identity/runtime validation can fail before a strict
            # tree can be constructed. The durable HOLD reason must still
            # become operator-visible rather than granting silent immunity.
            status = "invalid"
            if not eph.is_safe_reason(hold_reason, max_length=256):
                hold_reason = "process_tree_hold_reason_invalid"
        if status not in {"truncated", "invalid"}:
            return
        observed = tree_record.get("observed_count")
        limit = tree_record.get("limit")
        reason_code = tree_record.get("reason_code") or hold_reason
        if not eph.is_safe_reason(reason_code, max_length=256):
            reason_code = "process_tree_hold_reason_invalid"
        display_observed = (
            observed
            if isinstance(observed, int)
            and not isinstance(observed, bool)
            and 0 <= observed <= 1_000_000
            else None
        )
        display_limit = (
            limit
            if isinstance(limit, int)
            and not isinstance(limit, bool)
            and 0 <= limit <= 1_000_000
            else None
        )
        gap_status = (
            status
            if status != "truncated"
            or (display_observed is not None and display_limit is not None)
            else "invalid"
        )
        gap = supervisor_mod.process_tree_operator_gap(
            status=gap_status,
            reason_code=reason_code,
            observed=display_observed,
            limit=display_limit,
        )
        summary = (
            f"{gap} Automatic teardown and relaunch are refused because a "
            "partial action could strand descendants or start a duplicate agent."
        )
        launch, launch_problem = supervisor_mod.configured_detached_launch(
            supervisor_config,
            agent,
            root=root,
        )
        marker = (
            restart_requests.get(agent)
            if isinstance(restart_requests, dict)
            and isinstance(restart_requests.get(agent), dict)
            else None
        )
        restart = supervisor_mod.restart_request_progress(
            row,
            marker,
            decision_state=(
                "PROCESS_TREE_TRUNCATED"
                if status == "truncated"
                else "PROCESS_TREE_INVALID"
            ),
        )
        blocked_restart = (
            {
                "request_id": restart["request_id"],
                "state": restart["state"],
                "pending_progress": restart["pending"],
                **(
                    {"unavailable": True}
                    if restart.get("unavailable") is True
                    else {}
                ),
            }
            if restart["blocked"]
            and (
                restart.get("unavailable") is True
                or eph.is_safe_reason(restart["request_id"], max_length=256)
            )
            else None
        )
        remedy = admissions.get(identity)
        remedy_mode = None
        remedy_identity = None
        remedy_blocker = None
        if isinstance(remedy, dict):
            mode = remedy.get("mode")
            actor = remedy.get("actor")
            reason = remedy.get("reason")
            try:
                valid_actor = validate_agent_name(actor)
            except (TypeError, ValueError):
                valid_actor = None
            valid_reason = eph.is_safe_reason(reason)
            if (
                mode == "configured_reset"
                and remedy.get("agent") == agent
                and valid_actor is not None
                and valid_reason
                and isinstance(remedy.get("verified_launch_nonce"), str)
                and bool(remedy["verified_launch_nonce"])
            ):
                remedy_mode = mode
                remedy_identity = {
                    "mode": mode,
                    "agent": agent,
                    "actor": valid_actor,
                    "verified_launch_nonce": remedy["verified_launch_nonce"],
                    "reason": reason,
                }
            elif (
                mode == "ephemeral_archive"
                and request_id is not None
                and remedy.get("request_id") == request_id
                and eph.is_safe_id(request_id)
                and valid_actor is not None
                and valid_reason
                and remedy.get("verification_mode") in {
                    "strict_identity",
                    "operator_attested",
                }
                and (
                    (
                        remedy.get("verification_mode") == "strict_identity"
                        and isinstance(
                            remedy.get("verified_launch_nonce"), str
                        )
                        and bool(remedy["verified_launch_nonce"])
                    )
                    or (
                        remedy.get("verification_mode") == "operator_attested"
                        and remedy.get("verified_launch_nonce") is None
                    )
                )
            ):
                remedy_mode = mode
                remedy_identity = {
                    "mode": mode,
                    "request_id": request_id,
                    "actor": valid_actor,
                    "verification_mode": remedy["verification_mode"],
                    "verified_launch_nonce": remedy[
                        "verified_launch_nonce"
                    ],
                    "reason": reason,
                }
        blocker = blocked_admissions.get(identity)
        if remedy_mode is None and isinstance(blocker, dict):
            expected_common = {
                "mode",
                "missing_precondition",
            }
            if (
                blocker.get("missing_precondition")
                == "supervisor_kill_switch_absent"
                and blocker.get("mode") == "configured_reset"
                and frozenset(blocker) == expected_common | {"agent"}
                and blocker.get("agent") == agent
            ):
                remedy_blocker = {
                    "mode": "configured_reset",
                    "agent": agent,
                    "missing_precondition": "supervisor_kill_switch_absent",
                }
            elif (
                blocker.get("missing_precondition")
                == "supervisor_kill_switch_absent"
                and blocker.get("mode") == "ephemeral_archive"
                and request_id is not None
                and frozenset(blocker) == expected_common | {"request_id"}
                and blocker.get("request_id") == request_id
                and eph.is_safe_id(request_id)
            ):
                remedy_blocker = {
                    "mode": "ephemeral_archive",
                    "request_id": request_id,
                    "missing_precondition": "supervisor_kill_switch_absent",
                }
        if remedy_mode is not None:
            recommendation = (
                "The attended scripted remedy argv below is currently admitted; "
                "the command rechecks every precondition before it changes state."
            )
        elif remedy_blocker is not None:
            recommendation = (
                "no scripted remedy applies in this state: "
                ".agenttalk/supervisor.kill is absent. Create it while the "
                "supervisor remains stopped."
            )
        elif admissions_evaluated:
            recommendation = "no scripted remedy applies in this state."
        else:
            recommendation = (
                "Scripted remedy admission was not evaluated by this state-only "
                "projection; no scripted command is shown."
            )
        identity_warning = (
            _process_tree_identity_warning(tree_record)
            if isinstance(tree, dict)
            else None
        )
        if identity_warning:
            recommendation = f"{identity_warning} {recommendation}"
        if blocked_restart is not None:
            recommendation += (
                " A restart request is blocked by this refusal and is not "
                "pending progress."
            )
        if launch is not None:
            recommendation += (
                " After independently verifying the prior agent processes are "
                "stopped, reproduce the listed launch environment and run the "
                "configured argv below as a detached process."
            )
        else:
            recommendation += (
                " The configured detached launch could not be established from "
                f"supervisor.json: {launch_problem}."
            )
        it = _mk_item(
            SOURCE_PROCESS_TREE_HOLD,
            item_id(SOURCE_PROCESS_TREE_HOLD, identity),
            title=f"automatic recovery refused: {agent}",
            ident_content={
                "agent": agent,
                "request_id": request_id,
                "status": status,
                "reason_code": reason_code,
                "observed_count": display_observed,
                "limit": display_limit,
                "wrapper_generation": tree_record.get("wrapper_generation"),
                "launch_nonce": tree_record.get("launch_nonce"),
                "configured_launch": launch,
                "configured_launch_unavailable": (
                    launch_problem if launch is None else None
                ),
                "blocked_restart": blocked_restart,
                "scripted_remedy": remedy_identity,
                "scripted_remedy_blocker": remedy_blocker,
                "evidence_hash": source_hash({
                    "owned_process_tree": tree_record,
                    "legacy_process_evidence": row.get(
                        "legacy_process_evidence"
                    ),
                    "launcher_pid": row.get("launcher_pid"),
                    "launcher_start": row.get("launcher_start"),
                    "launcher_nonce": row.get("launcher_nonce"),
                    "runtime_wrapper_generation": row.get(
                        "runtime_wrapper_generation"
                    ),
                    "revoked_wrapper_runtime": row.get(
                        "revoked_wrapper_runtime"
                    ),
                    "brain_pid": row.get("brain_pid"),
                    "brain_start": row.get("brain_start"),
                    "managed_pids": row.get("managed_pids"),
                    "held_terminal": row.get("held_terminal"),
                }),
            },
            human_can_unblock_now=True,
            fields={
                "why_it_matters": summary,
                "recommendation": recommendation,
                "risk_if_ignored": (
                    "Automatic recovery remains blocked, and later work can be "
                    "starved behind a restart request that cannot progress."
                ),
                "priority": "high",
                "risk_severity": "high",
                "confidence": "high",
                "affected": [agent],
            },
            source_refs=[{
                "kind": "supervisor_state",
                "agent": agent,
                "reason_code": reason_code,
            }] if request_id is None else [{
                "kind": "supervisor_ephemeral_state",
                "agent": agent,
                "request_id": request_id,
                "reason_code": reason_code,
            }],
        )
        if launch is not None:
            it["configured_launch"] = launch
        else:
            it["configured_launch_unavailable"] = launch_problem
        if blocked_restart is not None:
            it["restart_request"] = blocked_restart
        if remedy_mode == "configured_reset":
            it["operator_argv"] = [
                "agenttalk",
                "supervise",
                "--reset-process-tree-ownership",
                "--for",
                remedy["agent"],
                "--hold-source-hash",
                it["source_hash"],
                "--verified-launch-nonce",
                remedy["verified_launch_nonce"],
                "--acknowledge-no-live-supervisor",
                "--acknowledge-owned-processes-stopped",
                "--reason",
                remedy["reason"],
                "--from",
                remedy["actor"],
            ]
        elif remedy_mode == "ephemeral_archive":
            operator_argv = [
                "agenttalk",
                "supervise",
                "--reset-process-tree-ownership",
                "--request-id",
                remedy["request_id"],
                "--hold-source-hash",
                it["source_hash"],
            ]
            if remedy["verification_mode"] == "strict_identity":
                operator_argv.extend([
                    "--verified-launch-nonce",
                    remedy["verified_launch_nonce"],
                ])
            operator_argv.extend([
                "--acknowledge-no-live-supervisor",
                "--acknowledge-owned-processes-stopped",
                "--reason",
                remedy["reason"],
                "--from",
                remedy["actor"],
            ])
            it["operator_argv"] = operator_argv
            # This is archive verification evidence, not remedy authority.
            # The CLI still independently rechecks every command precondition.
            it["attended_disposition_mode"] = remedy["verification_mode"]
        it["dedupe_key"] = dedupe_key(
            SOURCE_PROCESS_TREE_HOLD,
            identity=identity,
        )
        out.append(it)

    if isinstance(agents, dict):
        for agent, row in sorted(agents.items()):
            if isinstance(agent, str) and isinstance(row, dict):
                append_hold(identity=agent, agent=agent, row=row)
    eph_root = (
        state.get("ephemeral_reviewers")
        if isinstance(state, dict)
        else None
    )
    active = eph_root.get("active") if isinstance(eph_root, dict) else None
    if isinstance(active, dict):
        for raw_request_id, row in sorted(active.items(), key=lambda pair: str(pair[0])):
            if not isinstance(raw_request_id, str) or not isinstance(row, dict):
                continue
            request_id = raw_request_id
            if not eph.is_safe_id(request_id):
                request_id = (
                    "invalid-"
                    + hashlib.sha256(
                        request_id.encode("utf-8", errors="surrogatepass")
                    ).hexdigest()[:12]
                )
            fallback_agent = request_id
            try:
                fallback_agent = validate_agent_name(fallback_agent)
            except (TypeError, ValueError):
                fallback_agent = (
                    "ephemeral-"
                    + hashlib.sha256(
                        request_id.encode("utf-8", errors="surrogatepass")
                    ).hexdigest()[:12]
                )
            try:
                agent = validate_agent_name(row.get("agent"))
            except (TypeError, ValueError):
                agent = fallback_agent
            append_hold(
                identity=f"ephemeral:{request_id}",
                agent=agent,
                row=row,
                request_id=request_id,
            )
    return out


def dead_letter_items(entries: list[dict]) -> list[dict]:
    """Build items from canonical dead-letter entries.

    Resolution filtering is applied centrally by ``build_queue`` against each
    entry's source snapshot, so callers pass both resolved and unresolved rows.
    """
    out = []
    for e in entries:
        ag, mid = e.get("agent", ""), e.get("message_id", "")
        ident = dead_letter_entry_notice_state(e)
        it = _mk_item(SOURCE_DEAD_LETTER, item_id(SOURCE_DEAD_LETTER, ag, mid),
                      title=f"dead-letter: {ag}/{mid}",
                      ident_content=ident,
                      human_can_unblock_now=True,
                      fields={"why_it_matters": "a required message could not be delivered",
                              "priority": "high"},
                      source_refs=[{"kind": "dead_letter", "agent": ag, "message_id": mid}])
        it["dedupe_key"] = dedupe_key(SOURCE_DEAD_LETTER, identity=f"{ag}:{mid}")
        out.append(it)
    return out


def gate_hold_items(blockers: list[dict], *, scope: str = "release") -> list[dict]:
    """Each blocker: gates.check_gates()[...] item {name, reason, scope, ...}."""
    out = []
    for b in blockers:
        name = b.get("name", "")
        sc = b.get("scope", scope)
        it = _mk_item(SOURCE_GATE_HOLD, item_id(SOURCE_GATE_HOLD, sc, name),
                      title=f"gate HOLD: {name} ({sc})",
                      ident_content={"scope": sc, "name": name, "reason": b.get("reason")},
                      human_can_unblock_now=True,
                      fields={"why_it_matters": b.get("reason") or "", "priority": "high"},
                      source_refs=[{"kind": "gate", "scope": sc, "name": name}])
        it["dedupe_key"] = dedupe_key(SOURCE_GATE_HOLD, identity=f"{sc}:{name}")
        out.append(it)
    return out


def lead_unarmed_items(signals: list[dict]) -> list[dict]:
    """Each signal: {agent, reason}. From the PURE lead-loop-state helper (gate 8), not
    doctor text. Classified advisory (a lead-loop can self-recover / be re-armed)."""
    out = []
    for s in signals:
        ag = s.get("agent", "")
        it = _mk_item(SOURCE_LEAD_UNARMED, item_id(SOURCE_LEAD_UNARMED, ag),
                      title=f"lead-loop unarmed: {ag}",
                      ident_content={"agent": ag, "reason": s.get("reason")},
                      human_can_unblock_now=True, advisory=True,
                      fields={"why_it_matters": s.get("reason") or "", "priority": "normal"},
                      source_refs=[{"kind": "lead_unarmed", "agent": ag}])
        it["dedupe_key"] = dedupe_key(SOURCE_LEAD_UNARMED, identity=ag)
        out.append(it)
    return out


def coordination_stall_items(signals: list[dict]) -> list[dict]:
    """Project pure detector signals as stable, dismissable advisories."""
    out = []
    for signal in signals:
        stall_id = str(signal.get("stall_id") or "")
        if not stall_id:
            continue
        waiter = str(signal.get("waiter") or signal.get("agent") or "")
        request_id = str(signal.get("request_id") or "")
        reason = str(signal.get("reason") or "coordination is stalled")
        action = str(signal.get("action") or "Inspect and reassign the blocked work.")
        it = _mk_item(
            SOURCE_COORDINATION_STALL,
            item_id(SOURCE_COORDINATION_STALL, stall_id),
            title=f"team coordination stalled: {waiter}",
            ident_content={
                "identity_hash": signal.get("identity_hash"),
                "content_hash": signal.get("content_hash"),
            },
            human_can_unblock_now=True,
            advisory=True,
            age_seconds=float(signal.get("age_seconds") or 0),
            fields={
                "why_it_matters": reason,
                "recommendation": action,
                "priority": "high",
                "risk_severity": "medium",
                "confidence": "high",
            },
            source_refs=[{
                "kind": "coordination_stall",
                "agent": waiter,
                "request_id": request_id,
            }],
        )
        it["dedupe_key"] = dedupe_key(
            SOURCE_COORDINATION_STALL,
            identity=str(signal.get("identity_hash") or stall_id),
        )
        out.append(it)
    return out


def capacity_items(signals: list[dict]) -> list[dict]:
    """Each signal: {agent, kind, detail}. Threshold-tripped only (the caller filters).
    Advisory + passive (human_can_unblock_now=False)."""
    out = []
    for s in signals:
        ag, kind = s.get("agent", ""), s.get("kind", "")
        it = _mk_item(SOURCE_CAPACITY, item_id(SOURCE_CAPACITY, ag, kind),
                      title=f"capacity: {ag} {kind}",
                      ident_content={"agent": ag, "kind": kind, "detail": s.get("detail")},
                      human_can_unblock_now=False, advisory=True,
                      fields={"why_it_matters": s.get("detail") or "", "priority": "low"},
                      source_refs=[{"kind": "capacity", "agent": ag, "warning_kind": kind}])
        it["dedupe_key"] = dedupe_key(SOURCE_CAPACITY, identity=f"{ag}:{kind}")
        out.append(it)
    return out


def close_hold_items(holds: list[dict]) -> list[dict]:
    """Each hold: {close_id, scope, verdict, reason, revision}. Surfaced for a PUBLISHED
    close whose final verdict is HOLD (read cheaply from the persisted record's snapshot -
    NO gate recompute). Content-bound on (close_id, verdict, reason) so a re-published,
    differently-blocked close resurfaces despite a prior disposition (gate 1)."""
    out = []
    for h in holds:
        cid = h.get("close_id", "")
        it = _mk_item(SOURCE_CLOSE_HOLD, item_id(SOURCE_CLOSE_HOLD, cid),
                      title=f"close HOLD: {cid} ({h.get('scope') or 'release'})",
                      ident_content={"close_id": cid, "verdict": h.get("verdict"),
                                     "reason": h.get("reason"), "revision": h.get("revision")},
                      human_can_unblock_now=True,
                      fields={"why_it_matters": h.get("reason") or "close is published HOLD",
                              "priority": "high"},
                      source_refs=[{"kind": "close", "close_id": cid}])
        it["dedupe_key"] = dedupe_key(SOURCE_CLOSE_HOLD, identity=cid)
        out.append(it)
    return out


def source_error_item(source_name: str, error: str) -> dict:
    """A source read failed: surface a bounded warning item so the queue never blanks over
    one bad source (gate 8). defer-only (fix the reader)."""
    it = _mk_item(SOURCE_ERROR, item_id(SOURCE_ERROR, source_name),
                  title=f"attention source unavailable: {source_name}",
                  ident_content={"source": source_name, "error": error[:200]},
                  human_can_unblock_now=False, advisory=False,
                  fields={"why_it_matters": f"source {source_name} could not be read: {error[:200]}"},
                  warnings=["source_read_failed"])
    it["dedupe_key"] = dedupe_key(SOURCE_ERROR, identity=source_name)
    return it


def build_queue(items: list[dict], dispositions: list[dict], *, now_iso: str,
                include_deferred: bool = False, include_dismissed: bool = False,
                include_resolved: bool = False) -> dict:
    """PURE assembly: apply snapshot-bound dispositions, filter by state, group by
    dedupe_key for display, rank, and summarize. ``items`` are already-built source items
    (each source read fail-safe by the caller); ``dispositions`` are the raw disposition
    events. Never raises; a needs_operator item is never hidden by a torn disposition."""
    folded = fold_dispositions(dispositions)
    applied = [apply_disposition(it, folded, now_iso=now_iso) for it in items]
    shown_states = {"active"}
    if include_deferred:
        shown_states.add("deferred")
    if include_dismissed:
        shown_states.add("dismissed")
    if include_resolved:
        shown_states.update({"resolved", "answered_elsewhere"})
    visible = [it for it in applied if it.get("state") in shown_states]
    # dedupe for DISPLAY only: representative = highest-ranked per dedupe_key
    groups: dict[str, list[dict]] = {}
    for it in visible:
        groups.setdefault(it.get("dedupe_key", it["item_id"]), []).append(it)
    reps = []
    for members in groups.values():
        ranked = sort_items(members)
        rep = dict(ranked[0])
        rep["duplicates"] = [{"item_id": m["item_id"], "source_refs": m.get("source_refs", [])}
                             for m in ranked[1:]]
        reps.append(rep)
    ordered = sort_items(reps)
    summary = {
        "active_count": sum(1 for it in applied if it.get("state") == "active"),
        "deferred_count": sum(1 for it in applied if it.get("state") == "deferred"),
        "by_source": _count_by(applied, "source"),
        "by_priority": _count_by([it for it in applied if it.get("state") == "active"], "priority"),
        "oldest_active_age_seconds": max(
            [it.get("age_seconds", 0) for it in applied if it.get("state") == "active"],
            default=None),
    }
    return {"schema_version": SCHEMA_VERSION, "items": ordered, "summary": summary}


def compute_stats(items: list[dict], dispositions: list[dict], *, now_iso: str) -> dict:
    """NORTH-STAR instrumentation: derived counts of what the queue routes. Same inputs as
    build_queue - adds NO reads beyond the existing attention-queue collector, NO new state,
    NO writes, and does not inspect, print, or semantically use message-body content (the
    shared collector validates message envelopes; stats derive only from the collected item
    metadata). Counts the APPLIED source items across ALL states (raw signal volume, no
    display-dedup): what surfaced active (total + by source), what has been dispositioned
    (deferred/dismissed/resolved/answered_elsewhere), and dwell (oldest active age). Never
    raises."""
    folded = fold_dispositions(dispositions)
    applied = [apply_disposition(it, folded, now_iso=now_iso) for it in items]
    by_state: dict[str, int] = {}
    active_by_source: dict[str, int] = {}
    for it in applied:
        st = it.get("state", "active")
        by_state[st] = by_state.get(st, 0) + 1
        if st == "active":
            src = str(it.get("source"))
            active_by_source[src] = active_by_source.get(src, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "surfaced_active": by_state.get("active", 0),
        "active_by_source": dict(sorted(active_by_source.items())),
        "dispositioned": {
            "deferred": by_state.get("deferred", 0),
            "dismissed": by_state.get("dismissed", 0),
            "resolved": by_state.get("resolved", 0),
            "answered_elsewhere": by_state.get("answered_elsewhere", 0),
        },
        "oldest_active_age_seconds": max(
            [int(it.get("age_seconds") or 0) for it in applied if it.get("state") == "active"],
            default=0),
    }


def _count_by(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for it in items:
        out[str(it.get(key))] = out.get(str(it.get(key)), 0) + 1
    return out


def read_dispositions(store) -> tuple[list[dict], list[dict]]:
    """FAIL-SAFE read: scan in file order, skip invalid/torn lines, NEVER let an invalid
    line hide a prior valid disposition. Returns (valid_events, problems) where
    problems = [{line, error}] for doctor to surface torn lines."""
    path = dispositions_path(store)
    if not path.exists():
        return [], []
    valid: list[dict] = []
    problems: list[dict] = []
    try:
        for n, line in iter_lines(path):
            if line is None:
                problems.append({"line": n, "error": "invalid utf-8"})
                continue
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError as e:
                problems.append({"line": n, "error": f"invalid json: {e}"})
                continue
            if not validate_disposition_event(evt):
                problems.append({"line": n, "error": "malformed disposition event"})
                continue
            valid.append(evt)
    except (OSError, UnicodeError) as e:
        problems.append({"line": 0, "error": f"unreadable: {e}"})
    return valid, problems
