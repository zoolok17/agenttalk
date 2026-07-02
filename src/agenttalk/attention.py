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
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
SOURCE_DEAD_LETTER = "dead_letter"
SOURCE_GATE_HOLD = "gate_hold"
SOURCE_CLOSE_HOLD = "close_hold"
SOURCE_LEAD_UNARMED = "lead_unarmed"
SOURCE_CAPACITY = "capacity"
SOURCE_ERROR = "source_error"

# rank weight per source (higher = more urgent to a human)
_SOURCE_WEIGHT = {
    SOURCE_NEEDS_OPERATOR: 100,
    SOURCE_CONFIG_BLOCKED: 90,
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
_ALWAYS_BLOCKING = frozenset({SOURCE_NEEDS_OPERATOR, SOURCE_DEAD_LETTER})
_ADVISORY_CAPABLE = frozenset(
    {SOURCE_CONFIG_BLOCKED, SOURCE_GATE_HOLD, SOURCE_CLOSE_HOLD, SOURCE_LEAD_UNARMED})
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


# ----------------------------------------------------------- typed meta validation

def _is_single_line_str(v: Any) -> bool:
    return isinstance(v, str) and "\n" not in v and "\r" not in v


def validate_attention_meta(meta: dict | None) -> list[str]:
    """STRICT validation of a typed ``meta.attention`` block for the CLI-write path.
    Returns a list of error strings ([] == valid). An ABSENT block is valid (returns []);
    only a PRESENT block is validated. The reader side (:func:`parse_attention_meta`) is
    separately fail-safe - it never rejects, only downgrades."""
    if not meta:
        return []
    att = meta.get("attention") if "attention" in meta else meta
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
        errs.append("needed_by must be an ISO-8601 date or timezone-bearing datetime")
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
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


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
    """Deterministic sort key (descending on the weights, ascending on the final id
    tie-break). No prose scoring, no model inference - only validated meta + observed
    state. Callers sort with reverse=True on the weight tuple, then id ascending; we encode
    the id ascending by negating nothing and appending it as the last element with reverse
    handled by the caller. To keep it a single sortable tuple, id is inverted via a wrapper."""
    state = item.get("state", "active")
    active = state in ("active",)  # deferred/dismissed/resolved sink below active
    prio = {"urgent": 4, "high": 3, "normal": 2, "low": 1}.get(item.get("priority"), 1)
    risk = {"high": 3, "medium": 2, "low": 1}.get(item.get("risk_severity"), 0)
    src = _SOURCE_WEIGHT.get(item.get("source"), 0)
    needed = _needed_by_weight(item.get("needed_by"), item.get("_now_iso"))
    age_bucket = min(int(item.get("age_seconds", 0) // 3600), 72)
    return (
        1 if active else 0,
        1 if item.get("human_can_unblock_now") else 0,
        prio, risk, src, needed, age_bucket,
    )


def _needed_by_weight(needed_by: Any, now_iso: Any) -> int:
    if not isinstance(needed_by, str) or not needed_by:
        return 0
    from datetime import datetime, timezone
    try:
        nb = datetime.fromisoformat(needed_by.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if nb.tzinfo is None:
        nb = nb.replace(tzinfo=timezone.utc)
    try:
        now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")) if now_iso else datetime.now(timezone.utc)
    except (ValueError, TypeError):
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta_h = (nb - now).total_seconds() / 3600.0
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
    attention_dir(store).mkdir(parents=True, exist_ok=True)
    path = dispositions_path(store)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


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
    """Each entry: {request_id, subject, sender, age_seconds, meta}. The typed fields come
    from the opener's meta.attention via the FAIL-SAFE parse (a malformed block still
    surfaces the escalation with a warning - gate 2). human_can_unblock_now=True (a worker
    is literally blocked on the operator's answer)."""
    out = []
    for e in pending:
        fields, warns = parse_attention_meta(e.get("meta") or {})
        rid = e.get("request_id", "")
        dh = hashlib.sha256((fields.get("decision") or e.get("subject") or "").encode()).hexdigest()
        it = _mk_item(SOURCE_NEEDS_OPERATOR, item_id(SOURCE_NEEDS_OPERATOR, rid),
                      title=fields.get("decision") or e.get("subject") or "operator input needed",
                      ident_content={"rid": rid, "decision": fields.get("decision"),
                                     "subject": e.get("subject")},
                      human_can_unblock_now=True, age_seconds=float(e.get("age_seconds") or 0),
                      source_refs=[{"kind": "message", "request_id": rid}],
                      fields=fields, warnings=warns)
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


def dead_letter_items(entries: list[dict]) -> list[dict]:
    """Each entry: {agent, message_id, ...}. Unresolved dead letters (the caller excludes
    resolved). Content-bound on (agent, message_id)."""
    out = []
    for e in entries:
        ag, mid = e.get("agent", ""), e.get("message_id", "")
        it = _mk_item(SOURCE_DEAD_LETTER, item_id(SOURCE_DEAD_LETTER, ag, mid),
                      title=f"dead-letter: {ag}/{mid}",
                      ident_content={"agent": ag, "message_id": mid},
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
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return [], [{"line": 0, "error": f"unreadable: {e}"}]
    for n, line in enumerate(raw.splitlines(), start=1):
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
    return valid, problems
