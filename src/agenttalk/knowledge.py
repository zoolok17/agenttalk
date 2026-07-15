"""Durable pointer notes and advisory process lessons.

A *note* preserves the small piece of human/agent insight that is NOT in the
artifact (a seam, a gotcha, a decision + rationale); its ANCHOR points to the code/
thread that is. Consumers treat every note body as untrusted data and must reverify
the anchor before acting. Notes hang off the Phase-0 domain registry and go STALE
when their anchor changes (anchor-relative, not HEAD-relative).

Design (codex knowledge design, lead-gated; dev-2 + reviewer-1 consults folded in):
  * Store is ``.agenttalk/knowledge/notes.jsonl`` - append-only, one immutable event
    per line, preserved by reset (durable memory, not active bus state). The current
    view is the latest structurally valid and causally foldable event per
    ``(domain_id, key)``.
  * CAPTURE is open (any active agent publishes an ``uncurated`` note); CURATION is
    gated (a domain owner/curator, or a lead override, verifies/retracts).
  * Registry freshness is scoped to the effective domain definition; unrelated
    registry edits are cautions, not hard stale. Pointer staleness remains
    ANCHOR-RELATIVE + PURE: :func:`compute_staleness` derives
    stale_reasons / caution_flags from already-resolved inputs (the CLI/git adapter
    does the I/O). A note is hard-stale only when its anchor actually changed; a
    moved HEAD with an unchanged anchor is a CAUTION, not stale (over-staling would
    empty the layer on every unrelated commit).
  * Pointer-not-mirror is a CONTENT rule (the body is the insight, not a copy of the
    anchor) backed by a byte cap; bodies are untrusted data, never instructions.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from agenttalk import domains as dom
from agenttalk._jsonl import append_record, iter_lines

SCHEMA_VERSION = 1
STORE_DIRNAME = "knowledge"
NOTES_FILENAME = "notes.jsonl"

EVENT_PUBLISH = "publish"
EVENT_CURATE = "curate"
EVENT_RETRACT = "retract"

TYPE_SEAM = "seam"
TYPE_GOTCHA = "gotcha"
TYPE_DECISION = "decision"
TYPE_POINTER = "pointer"
TYPE_LESSON = "lesson"
NOTE_TYPES = frozenset({TYPE_SEAM, TYPE_GOTCHA, TYPE_DECISION, TYPE_POINTER, TYPE_LESSON})

PROCESS_DOMAIN = "process"
LESSON_SCOPES = frozenset({
    "process", "craft", "review", "test", "release", "ops", "docs", "security",
})
LESSON_STATUS_PROPOSED = "proposed"
LESSON_STATUS_ACCEPTED = "accepted"
LESSON_STATUS_RETIRED = "retired"
LESSON_STATUSES = frozenset({
    LESSON_STATUS_PROPOSED, LESSON_STATUS_ACCEPTED, LESSON_STATUS_RETIRED,
})

ANCHOR_KINDS = frozenset({"path", "symbol", "request", "wp", "sha"})

AUTH_UNCURATED = "uncurated"
AUTH_VERIFIED = "verified"
AUTH_LEAD_OVERRIDE = "lead_override"
AUTH_RETRACTED = "retracted"

BODY_MAX_BYTES = 2000   # short insight (a paragraph), not a mirror of the artifact

# stable stale reasons (hard - excluded from pull by default)
STALE_SUPERSEDED = "superseded"
STALE_RETRACTED = "retracted"
STALE_DOMAIN_GONE = "domain_gone"
STALE_REGISTRY_CHANGED = "domain_registry_hash_changed"
STALE_DOMAIN_DEFINITION_CHANGED = "domain_definition_hash_changed"
STALE_SHA_UNREACHABLE = "verified_sha_unreachable"
STALE_ANCHOR_GONE = "anchor_disappeared"
STALE_ANCHOR_CHANGED = "anchor_path_changed"
STALE_SYMBOL_MISMATCH = "symbol_evidence_mismatch"
STALE_TARGET_UNRESOLVABLE = "anchor_target_unresolvable"
STALE_MISSING_BASELINE = "missing_verified_baseline"   # path/symbol anchor, null vsha (C4b)
STALE_UNSUPPORTED_WP = "unsupported_wp_anchor"          # pathless wp, no resolver in 0.40.1 (C4b)
STALE_EXPIRED = "expired"
# caution flags (shown, NOT excluded by default)
CAUTION_SHA_NOT_HEAD = "verified_sha_not_head"
CAUTION_UNCURATED = "uncurated"
CAUTION_WEAK_SYMBOL = "weak_symbol_evidence"
CAUTION_REVIEW_DUE = "review_due"
CAUTION_REGISTRY_CHANGED = "domain_registry_changed_elsewhere"
CAUTION_LEGACY_UNSCOPED = "legacy_unscoped_registry_freshness"

_VIRTUAL_PROCESS_POLICY = {
    "domain_id": PROCESS_DOMAIN,
    "kind": "virtual-process-lessons",
    "lesson_only": True,
    "curation_policy": "operator-facing-or-lead",
    "schema_version": 1,
}

_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_NOTE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SAFE_TAG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_AGENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
LESSON_TEXT_MAX_BYTES = 500
LESSON_TAG_LIMIT = 16
LESSON_SUPERSEDES_LIMIT = 16

_AUTHORITY_FIELDS = frozenset({"state", "resolved_from", "reason"})
_ANCHOR_FIELDS_BY_KIND = {
    "path": frozenset({"kind", "path", "anchor_evidence"}),
    "symbol": frozenset({"kind", "path", "symbol", "anchor_evidence"}),
    "request": frozenset({"kind", "request_id", "msg_id", "anchor_evidence"}),
    "wp": frozenset({"kind", "mission", "wp_id", "path", "anchor_evidence"}),
    "sha": frozenset({"kind", "sha", "anchor_evidence"}),
}
_LESSON_BASE_FIELDS = frozenset({
    "scope", "trigger", "evidence_ref", "applies_to", "owner", "status",
    "review_after", "expires_at", "supersedes", "anchor",
})
# Curation integrity boundary: top-level content is domain_id/key/type/body.
# Lesson content is EVERY validated base field except status: scope, trigger,
# evidence_ref, applies_to, owner, review_after, expires_at, supersedes, and the
# full normalized anchor. New base fields therefore bind by default. Status and
# curator are curation state; event ids, authority, timestamps, verification SHA,
# and registry/payload hashes are attestations. Pointer content binds the complete
# normalized anchor returned by validate_anchor(), including anchor_evidence.
_LESSON_CURATION_STATE_FIELDS = frozenset({"status"})
_LESSON_CONTENT_FIELDS = tuple(sorted(
    _LESSON_BASE_FIELDS - _LESSON_CURATION_STATE_FIELDS))
_EVENT_BASE_FIELDS = frozenset({
    "schema_version", "event", "id", "key", "type", "domain_id", "body",
    "verified_against_sha", "domain_registry_hash", "domain_definition_hash",
    "authority", "updated_at", "supersedes_id", "supersedes_key", "author",
    "created_at",
})
_EVENT_FIELDS_BY_KIND = {
    EVENT_PUBLISH: _EVENT_BASE_FIELDS,
    EVENT_CURATE: _EVENT_BASE_FIELDS | frozenset({
        "curated_by", "curated_at", "curates_id", "payload_hash",
    }),
    EVENT_RETRACT: _EVENT_BASE_FIELDS | frozenset({
        "curated_by", "curated_at", "curates_id", "payload_hash",
        "retract_reason",
    }),
}


class KnowledgeError(ValueError):
    """Invalid knowledge input / state (CLI maps to a usage exit)."""


def new_note_id() -> str:
    import uuid
    return "kn-" + uuid.uuid4().hex[:12]


def _canonical_hash(value: object) -> str:
    blob = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def domain_definition_hash(entry: dict[str, Any]) -> str:
    """Hash one normalized domain definition, independent of unrelated domains."""
    if not isinstance(entry, dict):
        raise KnowledgeError("domain definition must be an object")
    return _canonical_hash(entry)


VIRTUAL_PROCESS_DOMAIN_HASH = domain_definition_hash(_VIRTUAL_PROCESS_POLICY)


def effective_domain(domain_id: str, note_type: str,
                     domains: dict[str, Any]) -> dict[str, Any]:
    """Resolve the real domain, or the lesson-only virtual process policy.

    A real ``process`` entry always overrides the virtual policy. Callers use the
    returned subject hash for scoped freshness and curation restamping.
    """
    entry = (domains or {}).get(domain_id)
    if isinstance(entry, dict):
        return {
            "exists": True,
            "virtual": False,
            "entry": entry,
            "definition_hash": domain_definition_hash(entry),
        }
    if domain_id == PROCESS_DOMAIN and note_type == TYPE_LESSON:
        return {
            "exists": True,
            "virtual": True,
            "entry": None,
            "definition_hash": VIRTUAL_PROCESS_DOMAIN_HASH,
        }
    return {"exists": False, "virtual": False, "entry": None,
            "definition_hash": None}


def immutable_payload(note: dict[str, Any]) -> dict[str, Any]:
    """Return curation-bound content, excluding mutable verification metadata."""
    note_type = validate_type(note.get("type"))
    payload: dict[str, Any] = {
        "domain_id": note.get("domain_id"),
        "key": validate_key(note.get("key")),
        "type": note_type,
        "body": validate_body(note.get("body")),
    }
    if note_type == TYPE_LESSON:
        lesson = validate_lesson(note.get("lesson"))
        semantic_lesson = {
            key: lesson.get(key)
            for key in _LESSON_CONTENT_FIELDS
            if lesson.get(key) is not None
        }
        payload["lesson"] = semantic_lesson
    else:
        payload["anchor"] = validate_anchor(note.get("anchor"))
    return payload


def payload_hash(note: dict[str, Any]) -> str:
    return _canonical_hash(immutable_payload(note))


# --------------------------------------------------------------- validators

def validate_key(value: object) -> str:
    if not isinstance(value, str) or not _KEY_RE.match(value):
        raise KnowledgeError(
            f"key {value!r} is not a safe stable key (alphanumeric plus . _ : -, "
            "starts alphanumeric, max 128 chars)")
    return value


def validate_note_id(value: object) -> str:
    if not isinstance(value, str) or not _NOTE_ID_RE.match(value):
        raise KnowledgeError(f"note id {value!r} is not a safe identifier")
    return value


def validate_domain_definition_hash(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise KnowledgeError("domain_definition_hash must be a sha256 hex digest")
    return value


def validate_type(value: object) -> str:
    if value not in NOTE_TYPES:
        raise KnowledgeError(f"type must be one of {sorted(NOTE_TYPES)}, got {value!r}")
    return value


def validate_body(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError("body is required (the insight not already in the anchor)")
    n = len(value.encode("utf-8"))
    if n > BODY_MAX_BYTES:
        raise KnowledgeError(
            f"body is {n} bytes, above the {BODY_MAX_BYTES}-byte cap - notes are "
            "pointers to insight, not copies of code/docs (point with the anchor)")
    return value


def validate_lesson_tag(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_TAG_RE.match(value):
        raise KnowledgeError(
            f"lesson tag {value!r} is not a safe slug (alphanumeric plus . _ -, "
            "starts alphanumeric, max 64 chars)")
    return value


def _validate_agent(value: object, field: str) -> str:
    if not isinstance(value, str) or not _AGENT_RE.match(value):
        raise KnowledgeError(f"lesson.{field} must be a valid agent name")
    return value


def validate_lesson_owner(value: object) -> str:
    return _validate_agent(value, "owner")


def _validate_bounded_text(value: object, field: str,
                           *, max_bytes: int = LESSON_TEXT_MAX_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError(f"lesson.{field} is required")
    n = len(value.encode("utf-8"))
    if n > max_bytes:
        raise KnowledgeError(f"lesson.{field} is {n} bytes, above the {max_bytes}-byte cap")
    return value


def _parse_iso_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError(f"lesson.{field} must be an ISO date or datetime")
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise KnowledgeError(f"lesson.{field} must be an ISO date or datetime") from e
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_now(now: datetime | str | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return _parse_iso_datetime(now, "now")


def validate_lesson(raw: object, *, default_owner: str | None = None,
                    default_status: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise KnowledgeError("lesson must be an object")
    scope = raw.get("scope")
    if scope not in LESSON_SCOPES:
        raise KnowledgeError(
            f"lesson.scope must be one of {sorted(LESSON_SCOPES)}, got {scope!r}")
    status = raw.get("status", default_status)
    if status not in LESSON_STATUSES:
        raise KnowledgeError(
            f"lesson.status must be one of {sorted(LESSON_STATUSES)}, got {status!r}")
    owner = raw.get("owner", default_owner)
    out: dict[str, Any] = {
        "scope": scope,
        "trigger": _validate_bounded_text(raw.get("trigger"), "trigger"),
        "evidence_ref": _validate_bounded_text(raw.get("evidence_ref"), "evidence_ref"),
        "owner": _validate_agent(owner, "owner"),
        "status": status,
        "review_after": raw.get("review_after"),
        "expires_at": raw.get("expires_at"),
    }
    review_after = _parse_iso_datetime(out["review_after"], "review_after")
    expires_at = _parse_iso_datetime(out["expires_at"], "expires_at")
    if expires_at <= review_after:
        raise KnowledgeError("lesson.expires_at must be after lesson.review_after")

    tags = raw.get("applies_to", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise KnowledgeError("lesson.applies_to must be a list of safe slug tags")
    if len(tags) > LESSON_TAG_LIMIT:
        raise KnowledgeError(f"lesson.applies_to may contain at most {LESSON_TAG_LIMIT} tags")
    out["applies_to"] = [validate_lesson_tag(t) for t in tags]

    supersedes = raw.get("supersedes", [])
    if supersedes is None:
        supersedes = []
    if not isinstance(supersedes, list):
        raise KnowledgeError("lesson.supersedes must be a list of lesson keys")
    if len(supersedes) > LESSON_SUPERSEDES_LIMIT:
        raise KnowledgeError(
            f"lesson.supersedes may contain at most {LESSON_SUPERSEDES_LIMIT} keys")
    out["supersedes"] = [validate_key(k) for k in supersedes]

    if raw.get("curator") is not None:
        out["curator"] = _validate_agent(raw.get("curator"), "curator")
    if raw.get("anchor") is not None:
        out["anchor"] = validate_anchor(raw.get("anchor"))
    return out


def validate_anchor(raw: object) -> dict:
    """A pointer-shaped anchor. Validates shape only - the CLI/git adapter resolves
    existence/staleness. anchor_evidence (optional) must stay lightweight (ids/
    hashes), never copied code/docs (enforced by the body byte-cap + review)."""
    if not isinstance(raw, dict):
        raise KnowledgeError("anchor must be an object {kind, ...}")
    kind = raw.get("kind")
    if kind not in ANCHOR_KINDS:
        raise KnowledgeError(f"anchor.kind must be one of {sorted(ANCHOR_KINDS)}, got {kind!r}")
    out: dict[str, Any] = {"kind": kind}
    if kind == "path":
        out["path"] = _norm_path(_require_str(raw, "path"))
    elif kind == "symbol":
        out["path"] = _norm_path(_require_str(raw, "path"))
        out["symbol"] = _require_str(raw, "symbol")
    elif kind == "request":
        out["request_id"] = _require_str(raw, "request_id")
        if raw.get("msg_id") is not None:
            out["msg_id"] = _require_str(raw, "msg_id")
    elif kind == "wp":
        out["mission"] = _require_str(raw, "mission")
        out["wp_id"] = _require_str(raw, "wp_id")
        if raw.get("path") is not None:
            out["path"] = _norm_path(_require_str(raw, "path"))
    elif kind == "sha":
        sha = raw.get("sha")
        if not (isinstance(sha, str) and _FULL_SHA_RE.match(sha)):
            raise KnowledgeError("anchor.sha must be a full 40-char SHA")
        out["sha"] = sha
    ev = raw.get("anchor_evidence")
    if ev is not None:
        if not isinstance(ev, dict):
            raise KnowledgeError("anchor_evidence must be an object of lightweight ids/hashes")
        out["anchor_evidence"] = ev
    return out


def _require_str(raw: dict, field: str) -> str:
    v = raw.get(field)
    if not isinstance(v, str) or not v:
        raise KnowledgeError(f"anchor.{field} is required and must be a non-empty string")
    return v


def _norm_path(value: str) -> str:
    """Normalize a path-bearing anchor to a safe repo-relative path (reuse the
    domains normalizer so anchors classify like domain ownership and absolute/
    escaping paths are rejected)."""
    try:
        return dom.normalize_repo_path(value)
    except dom.DomainError as e:
        raise KnowledgeError(f"anchor path {value!r} is not a safe repo-relative path: {e}") from e


def anchor_path(anchor: dict) -> str | None:
    """The repo path an anchor is bound to (for anchor-relative staleness), or None
    for anchors that are not path-bound (request/wp-without-path/sha)."""
    if not isinstance(anchor, dict):
        return None
    if anchor.get("kind") in ("path", "symbol") or (anchor.get("kind") == "wp" and anchor.get("path")):
        return anchor.get("path")
    return None


# --------------------------------------------------------------- event builders

def new_publish_event(*, note_id: str, key: str, type: str, domain_id: str, body: str,
                      anchor: dict | None, verified_against_sha: str | None,
                      domain_registry_hash: str, domain_definition_hash: str,
                      author: str, resolved_from: str,
                      at: str, supersedes_id: str | None = None,
                      supersedes_key: str | None = None,
                      lesson: dict | None = None) -> dict[str, Any]:
    """A PUBLISH event (capture). Always ``uncurated`` - curation is a separate
    event. Pure; the CLI persists it under the lock."""
    if verified_against_sha is not None and not _FULL_SHA_RE.match(str(verified_against_sha)):
        raise KnowledgeError("verified_against_sha must be a full 40-char SHA when present")
    note_type = validate_type(type)
    evt = {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_PUBLISH,
        "id": validate_note_id(note_id),
        "key": validate_key(key),
        "type": note_type,
        "domain_id": domain_id,
        "body": validate_body(body),
        "verified_against_sha": verified_against_sha,
        "domain_registry_hash": domain_registry_hash,
        "domain_definition_hash": validate_domain_definition_hash(
            domain_definition_hash),
        "author": author,
        "authority": {"state": AUTH_UNCURATED, "resolved_from": resolved_from, "reason": None},
        "created_at": at,
        "updated_at": at,
        "supersedes_id": supersedes_id,
        "supersedes_key": supersedes_key,
    }
    if note_type == TYPE_LESSON:
        lesson_obj = dict(lesson or {})
        if anchor is not None and lesson_obj.get("anchor") is None:
            lesson_obj["anchor"] = anchor
        evt["lesson"] = validate_lesson(
            lesson_obj, default_owner=author, default_status=LESSON_STATUS_PROPOSED)
    else:
        evt["anchor"] = validate_anchor(anchor)
    return evt


def new_curate_event(*, base: dict, action: str, curated_by: str, resolved_from: str,
                     at: str, reason: str | None,
                     domain_registry_hash: str | None = None,
                     domain_definition_hash: str | None = None) -> dict[str, Any]:
    """A CURATE (verify) or RETRACT event over an existing note (same key/domain).
    verify -> authority.state=verified (or lead_override); retract -> terminal
    tombstone for that key until a later publish supersedes it."""
    if action not in ("verify", "retract"):
        raise KnowledgeError("curate action must be verify or retract")
    if action == "retract" and not (reason and reason.strip()):
        raise KnowledgeError("a retract requires a reason")
    state = AUTH_RETRACTED if action == "retract" else (
        AUTH_LEAD_OVERRIDE if resolved_from == "lead" else AUTH_VERIFIED)
    definition_hash = domain_definition_hash or base.get("domain_definition_hash")
    definition_hash = validate_domain_definition_hash(definition_hash)
    registry_hash = domain_registry_hash or base.get("domain_registry_hash")
    if not isinstance(registry_hash, str) or not registry_hash:
        raise KnowledgeError("domain_registry_hash is required")
    note_type = validate_type(base.get("type"))
    evt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_RETRACT if action == "retract" else EVENT_CURATE,
        "id": new_note_id(),
        "key": validate_key(base.get("key")),
        "type": note_type,
        "domain_id": base.get("domain_id"),
        "body": validate_body(base.get("body")),
        "verified_against_sha": base.get("verified_against_sha"),
        "domain_registry_hash": registry_hash,
        "domain_definition_hash": definition_hash,
        "authority": {"state": state, "resolved_from": resolved_from, "reason": reason},
        "curated_by": curated_by,
        "curated_at": at,
        "updated_at": at,
        "curates_id": validate_note_id(base.get("id")),
        "payload_hash": payload_hash(base),
    }
    for field in ("author", "created_at", "supersedes_id", "supersedes_key"):
        if field in base:
            evt[field] = base.get(field)
    if note_type == TYPE_LESSON:
        lesson = validate_lesson(base.get("lesson"))
        lesson["status"] = LESSON_STATUS_RETIRED if action == "retract" else LESSON_STATUS_ACCEPTED
        lesson["curator"] = curated_by
        evt["lesson"] = validate_lesson(lesson)
    else:
        evt["anchor"] = validate_anchor(base.get("anchor"))
    if action == "retract":
        evt["retract_reason"] = reason
    return evt


# --------------------------------------------------------------- current view

def _event_allowed_fields(event: str, note_type: str) -> frozenset[str]:
    note_field = "lesson" if note_type == TYPE_LESSON else "anchor"
    return _EVENT_FIELDS_BY_KIND[event] | frozenset({note_field})


def _unexpected_fields(raw: object, allowed: frozenset[str], label: str) -> str | None:
    if not isinstance(raw, dict):
        return None
    extras = sorted(set(raw) - allowed)
    if not extras:
        return None
    return f"unexpected {label} field(s): {', '.join(extras)}"


def _anchor_schema_problem(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    allowed = _ANCHOR_FIELDS_BY_KIND.get(
        raw.get("kind"), frozenset({"kind", "anchor_evidence"}))
    return _unexpected_fields(raw, allowed, "anchor")


def _event_schema_problem(evt: dict, event: str, note_type: str) -> str | None:
    problem = _unexpected_fields(
        evt, _event_allowed_fields(event, note_type), "top-level")
    if problem is not None:
        return problem
    problem = _unexpected_fields(evt.get("authority"), _AUTHORITY_FIELDS, "authority")
    if problem is not None:
        return problem
    if note_type == TYPE_LESSON:
        lesson = evt.get("lesson")
        lesson_fields = _LESSON_BASE_FIELDS | (
            frozenset({"curator"}) if event != EVENT_PUBLISH else frozenset())
        problem = _unexpected_fields(lesson, lesson_fields, "lesson")
        if problem is not None:
            return problem
        if isinstance(lesson, dict) and lesson.get("anchor") is not None:
            return _anchor_schema_problem(lesson.get("anchor"))
        return None
    return _anchor_schema_problem(evt.get("anchor"))


def _is_legacy_persisted_event(evt: object) -> bool:
    return isinstance(evt, dict) and all(
        evt.get(field) is None
        for field in ("domain_definition_hash", "curates_id", "payload_hash")
    )


def _canonical_anchor(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    allowed = _ANCHOR_FIELDS_BY_KIND.get(
        raw.get("kind"), frozenset({"kind", "anchor_evidence"}))
    return {key: value for key, value in raw.items() if key in allowed}


def _canonical_legacy_event(evt: object) -> object:
    """Drop unknown fields from historical rows before validation and output."""
    if not _is_legacy_persisted_event(evt):
        return evt
    event = evt.get("event")
    note_type = evt.get("type")
    if event not in _EVENT_FIELDS_BY_KIND or note_type not in NOTE_TYPES:
        return evt
    allowed = _event_allowed_fields(event, note_type)
    out = {key: value for key, value in evt.items() if key in allowed}
    authority = out.get("authority")
    if isinstance(authority, dict):
        out["authority"] = {
            key: value for key, value in authority.items()
            if key in _AUTHORITY_FIELDS
        }
    if note_type == TYPE_LESSON:
        lesson = out.get("lesson")
        if isinstance(lesson, dict):
            lesson_fields = _LESSON_BASE_FIELDS | (
                frozenset({"curator"}) if event != EVENT_PUBLISH else frozenset())
            clean_lesson = {
                key: value for key, value in lesson.items()
                if key in lesson_fields
            }
            if clean_lesson.get("anchor") is not None:
                clean_lesson["anchor"] = _canonical_anchor(clean_lesson["anchor"])
            out["lesson"] = clean_lesson
    elif out.get("anchor") is not None:
        out["anchor"] = _canonical_anchor(out["anchor"])
    return out


def event_problem(evt: object) -> str | None:
    """FULL structural validation of one persisted event AS AN EVENT - the reader/
    fold use this so a JSON-valid-but-malformed line can NEVER become current, hide a
    valid note, OR FORGE curation (codex + reviewer-1 blockers). Validates the note
    fields AND domain_registry_hash, verified_against_sha shape, the allowed authority
    states, the event-kind <-> authority-state matrix (publish=uncurated,
    curate=verified|lead_override, retract=retracted - so an open-capture publish can
    never self-declare verified), and the per-kind required lineage fields. Returns an
    error string, or None if the event is foldable."""
    if not isinstance(evt, dict):
        return "not a JSON object"
    if evt.get("schema_version") != SCHEMA_VERSION:
        return "schema_version must be 1"
    event = evt.get("event")
    if event not in (EVENT_PUBLISH, EVENT_CURATE, EVENT_RETRACT):
        return "event must be publish|curate|retract"
    try:
        note_type = validate_type(evt.get("type"))
    except KnowledgeError as e:
        return str(e)
    schema_problem = _event_schema_problem(evt, event, note_type)
    if schema_problem is not None:
        return schema_problem
    for key in ("id", "key", "domain_id"):
        if not isinstance(evt.get(key), str) or not evt.get(key):
            return f"{key} is required"
    if not isinstance(evt.get("domain_registry_hash"), str) or not evt.get("domain_registry_hash"):
        return "domain_registry_hash is required"
    definition_hash = evt.get("domain_definition_hash")
    if definition_hash is not None and not (
            isinstance(definition_hash, str) and _SHA256_RE.match(definition_hash)):
        return "domain_definition_hash must be a sha256 hex digest"
    vsha = evt.get("verified_against_sha")
    if vsha is not None and not (isinstance(vsha, str) and _FULL_SHA_RE.match(vsha)):
        return "verified_against_sha must be null or a full 40-char SHA"
    auth = evt.get("authority")
    if not isinstance(auth, dict):
        return "authority is required"
    state = auth.get("state")
    if state not in (AUTH_UNCURATED, AUTH_VERIFIED, AUTH_LEAD_OVERRIDE, AUTH_RETRACTED):
        return f"authority.state must be one of uncurated|verified|lead_override|retracted, got {state!r}"
    # event-kind <-> authority-state matrix: a publish (open capture) can NEVER be
    # verified/lead_override (that requires a separate curate event) - this is the
    # forged-curation guard.
    if event == EVENT_PUBLISH and state != AUTH_UNCURATED:
        return "a publish event must be uncurated (curation is a separate curate event)"
    if event == EVENT_CURATE and state not in (AUTH_VERIFIED, AUTH_LEAD_OVERRIDE):
        return "a curate event must be verified or lead_override"
    if event == EVENT_RETRACT and state != AUTH_RETRACTED:
        return "a retract event must be retracted"
    # per-kind lineage fields
    if event == EVENT_PUBLISH and not (isinstance(evt.get("author"), str) and evt.get("author")):
        return "a publish event requires author"
    if event in (EVENT_CURATE, EVENT_RETRACT) and not (
            isinstance(evt.get("curated_by"), str) and evt.get("curated_by")):
        return "a curate/retract event requires curated_by"
    curates_id = evt.get("curates_id")
    semantic_hash = evt.get("payload_hash")
    if event == EVENT_PUBLISH and (curates_id is not None or semantic_hash is not None):
        return "a publish event cannot carry curation lineage"
    if event in (EVENT_CURATE, EVENT_RETRACT):
        if (curates_id is None) != (semantic_hash is None):
            return "curates_id and payload_hash must be present together"
        if curates_id is not None:
            try:
                validate_note_id(curates_id)
            except KnowledgeError as e:
                return str(e)
            if not isinstance(semantic_hash, str) or not _SHA256_RE.match(semantic_hash):
                return "payload_hash must be a sha256 hex digest"
            if definition_hash is None:
                return "new curation events require domain_definition_hash"
    if event == EVENT_RETRACT and not (
            isinstance(evt.get("retract_reason"), str) and evt.get("retract_reason").strip()):
        return "a retract event requires a non-empty retract_reason"
    try:
        validate_key(evt["key"])
        validate_note_id(evt["id"])
        validate_body(evt.get("body"))
        if note_type == TYPE_LESSON:
            lesson = validate_lesson(evt.get("lesson"))
            expected = {
                EVENT_PUBLISH: LESSON_STATUS_PROPOSED,
                EVENT_CURATE: LESSON_STATUS_ACCEPTED,
                EVENT_RETRACT: LESSON_STATUS_RETIRED,
            }[event]
            if lesson["status"] != expected:
                return f"lesson.status must be {expected} for {event}"
            if event == EVENT_CURATE and not lesson.get("curator"):
                return "accepted lessons require lesson.curator"
        else:
            validate_anchor(evt.get("anchor"))
    except KnowledgeError as e:
        return str(e)
    return None


def _is_wellformed(evt: object) -> bool:
    return event_problem(evt) is None


def _curation_causal_problem(evt: dict, prior: list[dict]) -> str | None:
    if evt.get("event") == EVENT_PUBLISH:
        return None
    if not prior:
        return "curation has no prior valid same-key event"

    curates_id = evt.get("curates_id")
    semantic_hash = evt.get("payload_hash")
    if curates_id is None and semantic_hash is None:
        # Legacy rows copied their current base. Preserve them only when that
        # immutable payload still matches the current accepted same-key event.
        parent = prior[-1]
        if payload_hash(evt) != payload_hash(parent):
            return "legacy curation payload differs from its prior current event"
        return None

    parent = prior[-1]
    if curates_id != parent.get("id"):
        return "curates_id does not reference the current prior same-key event"
    parent_hash = payload_hash(parent)
    if semantic_hash != parent_hash:
        return "payload_hash does not match the referenced prior event"
    if payload_hash(evt) != semantic_hash:
        return "payload_hash does not match the curation event payload"
    return None


def resolve_views_with_problems(
        events: list[dict]) -> tuple[dict[tuple, dict], list[dict]]:
    """Causally fold valid events and report non-foldable semantic rows.

    Structurally or semantically invalid curation is skipped entirely, so it can
    neither become current nor hide a prior publish/curation.
    """
    out: dict[tuple, dict] = {}
    history: dict[tuple, list[dict]] = {}
    problems: list[dict] = []
    for line, raw_evt in enumerate(events, 1):
        evt = _canonical_legacy_event(raw_evt)
        structural = event_problem(evt)
        if structural is not None:
            problems.append({"line": line, "error": f"malformed event: {structural}"})
            continue
        k = (evt["domain_id"], evt["key"])
        causal = _curation_causal_problem(evt, history.get(k, []))
        if causal is not None:
            problems.append({"line": line, "error": causal})
            continue
        history.setdefault(k, []).append(evt)
        rec = out.setdefault(
            k, {"latest": None, "curated": None, "tombstoned": False})
        rec["latest"] = evt
        state = (evt.get("authority") or {}).get("state")
        if evt.get("event") == EVENT_RETRACT or state == AUTH_RETRACTED:
            rec["curated"] = evt
            rec["tombstoned"] = True
        elif state in (AUTH_VERIFIED, AUTH_LEAD_OVERRIDE):
            rec["curated"] = evt
            rec["tombstoned"] = False
    return out, problems


def current_view(events: list[dict]) -> dict[tuple, dict]:
    """PURE: fold append-only events into the latest VALID event per (domain_id, key),
    in file order (later wins). Invalid events are skipped (fail-safe - a malformed
    latest line never hides a valid note). A retract is kept as the terminal event."""
    views, _problems = resolve_views_with_problems(events)
    return {key: rec["latest"] for key, rec in views.items()}


def resolve_views(events: list[dict]) -> dict[tuple, dict]:
    """PURE: per (domain_id, key), the latest valid event PLUS the latest CURATED
    event - so open capture cannot mutate the authoritative (curated) visible set
    (codex blocker). Returns {key: {"latest", "curated", "tombstoned"}}:

      * latest:     the latest valid event (for --include-uncurated proposals)
      * curated:    the latest verify / lead_override / retract event (authoritative)
      * tombstoned: the latest curation was a retract

    Default pull shows ``curated`` (verified, non-tombstoned); a later uncurated
    publish updates ``latest`` only, so the verified note never silently disappears.
    Re-opening a retracted key requires a fresh publish AND re-curation."""
    out, _problems = resolve_views_with_problems(events)
    return out


def is_retracted(note: dict) -> bool:
    return note.get("event") == EVENT_RETRACT or \
        (note.get("authority") or {}).get("state") == AUTH_RETRACTED


def is_curated(note: dict) -> bool:
    return (note.get("authority") or {}).get("state") in (AUTH_VERIFIED, AUTH_LEAD_OVERRIDE)


# --------------------------------------------------------------- pure staleness

def compute_domain_freshness(
        note: dict, *, domain_exists: bool, current_registry_hash: str | None,
        current_domain_definition_hash: str | None) -> dict[str, list[str]]:
    """Evaluate only registry/domain freshness for pointer and lesson views."""
    reasons: list[str] = []
    cautions: list[str] = []
    stored_subject_hash = note.get("domain_definition_hash")
    if stored_subject_hash is None:
        cautions.append(CAUTION_LEGACY_UNSCOPED)
    if not domain_exists:
        reasons.append(STALE_DOMAIN_GONE)
        return {"stale_reasons": reasons, "caution_flags": cautions}

    global_changed = (
        current_registry_hash is not None
        and note.get("domain_registry_hash") != current_registry_hash
    )
    if stored_subject_hash is not None and current_domain_definition_hash is not None:
        if stored_subject_hash != current_domain_definition_hash:
            reasons.append(STALE_DOMAIN_DEFINITION_CHANGED)
        elif global_changed:
            cautions.append(CAUTION_REGISTRY_CHANGED)
    elif global_changed and stored_subject_hash is not None:
        cautions.append(CAUTION_REGISTRY_CHANGED)
    return {"stale_reasons": reasons, "caution_flags": cautions}


def compute_staleness(note: dict, *, domain_exists: bool, current_registry_hash: str,
                      anchor_status: dict,
                      current_domain_definition_hash: str | None = None) -> dict[str, Any]:
    """PURE: derive {stale_reasons, caution_flags, hard_stale} for a CURRENT note.
    All anchor/git resolution is done by the CLI and passed in ``anchor_status``:

      {
        "sha_reachable": bool|None,   # verified_against_sha reachable from HEAD (None=unknown->stale)
        "head_moved": bool,           # verified_against_sha != HEAD
        "anchor_changed": bool|None,  # anchor path changed verified_sha..HEAD (None=could-not-determine->stale)
        "anchor_exists": bool,        # the anchor path/target exists at HEAD
        "evidence_match": bool|None,  # symbol/anchor evidence still matches (None=weak/unknown)
        "target_resolvable": bool,    # request/wp/sha anchor resolves in live/archived store
      }

    Anchor-relative: a moved HEAD with an UNCHANGED anchor is a CAUTION, not stale."""
    reasons: list[str] = []
    cautions: list[str] = []

    if is_retracted(note):
        reasons.append(STALE_RETRACTED)
    domain_verdict = compute_domain_freshness(
        note,
        domain_exists=domain_exists,
        current_registry_hash=current_registry_hash,
        current_domain_definition_hash=current_domain_definition_hash,
    )
    reasons.extend(domain_verdict["stale_reasons"])
    cautions.extend(domain_verdict["caution_flags"])

    if not is_curated(note):
        cautions.append(CAUTION_UNCURATED)

    kind = (note.get("anchor") or {}).get("kind")
    path_bound = anchor_path(note.get("anchor") or {}) is not None

    if note.get("verified_against_sha"):
        if anchor_status.get("sha_reachable") is not True:
            reasons.append(STALE_SHA_UNREACHABLE)
        elif anchor_status.get("head_moved"):
            cautions.append(CAUTION_SHA_NOT_HEAD)

    if path_bound:
        if not note.get("verified_against_sha"):
            # C4b: a path/symbol anchor with NO verified baseline cannot be checked for
            # change -> fail closed HARD-STALE (was silently fresh). Distinct reason so
            # `--include-stale` shows WHY (no baseline, not "anchor changed").
            reasons.append(STALE_MISSING_BASELINE)
        if anchor_status.get("anchor_exists") is False:
            reasons.append(STALE_ANCHOR_GONE)
        elif note.get("verified_against_sha") and (
                anchor_status.get("anchor_changed") is True
                or anchor_status.get("anchor_changed") is None):
            # changed OR could-not-be-determined -> hard stale (never infer fresh). Only
            # meaningful WITH a baseline; null-baseline is already STALE_MISSING_BASELINE.
            reasons.append(STALE_ANCHOR_CHANGED)
        if kind == "symbol":
            ev = anchor_status.get("evidence_match")
            if ev is False:
                reasons.append(STALE_SYMBOL_MISMATCH)
            elif ev is None:
                cautions.append(CAUTION_WEAK_SYMBOL)
    elif kind == "wp":
        # C4b: a PATHLESS wp anchor has no resolver in 0.40.1 -> unsupported/unresolved,
        # hard-stale by default with a self-describing reason (a wp WITH a path is
        # path_bound above and uses the path-bound check). WP identity stays advisory
        # until a future durable WP resolver exists (documented limitation).
        reasons.append(STALE_UNSUPPORTED_WP)
    elif kind in ("request", "sha"):
        if anchor_status.get("target_resolvable") is False:
            reasons.append(STALE_TARGET_UNRESOLVABLE)

    return {"stale_reasons": sorted(set(reasons)),
            "caution_flags": sorted(set(cautions)),
            "hard_stale": bool(reasons)}


def lesson_superseded_keys(notes: list[dict]) -> set[str]:
    """Keys retired from active lesson digests by accepted superseding lessons."""
    out: set[str] = set()
    for note in notes:
        if note.get("type") != TYPE_LESSON or not is_curated(note) or is_retracted(note):
            continue
        lesson = note.get("lesson") or {}
        if lesson.get("status") != LESSON_STATUS_ACCEPTED:
            continue
        out.update(k for k in lesson.get("supersedes", []) if isinstance(k, str))
    return out


def lesson_updated_at(note: dict) -> datetime:
    for field in ("curated_at", "updated_at", "created_at"):
        try:
            return _parse_iso_datetime(note.get(field), field)
        except KnowledgeError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def compute_lesson_state(note: dict, *, now: datetime | str | None = None,
                         superseded_keys: set[str] | None = None,
                         domain_exists: bool = True,
                         current_registry_hash: str | None = None,
                         current_domain_definition_hash: str | None = None) -> dict[str, Any]:
    """PURE lesson freshness. Lesson staleness is date/status/key based only.

    The optional lesson anchor is provenance and filtering context, never freshness
    authority, so callers do not pass anchor/git status here.
    """
    tnow = _coerce_now(now)
    lesson = validate_lesson(note.get("lesson"))
    reasons: list[str] = []
    cautions: list[str] = []
    domain_verdict = compute_domain_freshness(
        note,
        domain_exists=domain_exists,
        current_registry_hash=(current_registry_hash
                               if current_registry_hash is not None
                               else note.get("domain_registry_hash")),
        current_domain_definition_hash=current_domain_definition_hash,
    )
    reasons.extend(domain_verdict["stale_reasons"])
    cautions.extend(domain_verdict["caution_flags"])
    review_at = _parse_iso_datetime(lesson.get("review_after"), "review_after")
    expires_at = _parse_iso_datetime(lesson.get("expires_at"), "expires_at")
    status = lesson.get("status")

    if is_retracted(note) or status == LESSON_STATUS_RETIRED:
        reasons.append(STALE_RETRACTED)
    if note.get("key") in (superseded_keys or set()):
        reasons.append(STALE_SUPERSEDED)
    if expires_at <= tnow:
        reasons.append(STALE_EXPIRED)
    if status != LESSON_STATUS_ACCEPTED or not is_curated(note):
        cautions.append(CAUTION_UNCURATED)
    if review_at <= tnow and status == LESSON_STATUS_ACCEPTED:
        cautions.append(CAUTION_REVIEW_DUE)

    active = (
        status == LESSON_STATUS_ACCEPTED
        and is_curated(note)
        and not reasons
    )
    return {
        "stale_reasons": sorted(set(reasons)),
        "caution_flags": sorted(set(cautions)),
        "hard_stale": bool(reasons),
        "review_due": CAUTION_REVIEW_DUE in cautions,
        "expired": STALE_EXPIRED in reasons,
        "active": active,
        "review_after": lesson.get("review_after"),
        "expires_at": lesson.get("expires_at"),
    }


def _knowledge_search_text(note: dict) -> str:
    if note.get("type") == TYPE_LESSON:
        lesson = note.get("lesson") or {}
        value = {
            "key": note.get("key"),
            "body": note.get("body"),
            "type": note.get("type"),
            "scope": lesson.get("scope"),
            "trigger": lesson.get("trigger"),
            "evidence_ref": lesson.get("evidence_ref"),
            "applies_to": lesson.get("applies_to") or [],
        }
    else:
        value = {
            "key": note.get("key"),
            "body": note.get("body"),
            "type": note.get("type"),
            "anchor": note.get("anchor") or {},
        }
    return json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()


def _lesson_rank_key(row: tuple[dict, dict], context_scope: str) -> tuple:
    note, verdict = row
    lesson = note.get("lesson") or {}
    return (
        0 if lesson.get("scope") == PROCESS_DOMAIN else 1,
        0 if lesson.get("scope") == context_scope else 1,
        0 if verdict.get("review_due") else 1,
        -lesson_updated_at(note).timestamp(),
        note.get("domain_id") or "",
        note.get("key") or "",
    )


def select_knowledge_view(
        views: dict[tuple, dict], *, domains: dict[str, Any],
        registry_hash: str, anchor_status_by_id: dict[str, dict],
        semantic_problems: list[dict] | None = None,
        domain_id: str | None = None, type_filter: str | None = None,
        scope: str | None = None, tags: list[str] | None = None,
        query: str | None = None, include_uncurated: bool = False,
        include_stale: bool = False, note_limit: int | None = None,
        lesson_limit: int | None = None, context_scope: str = PROCESS_DOMAIN,
        now: datetime | str | None = None,
        exclude_lessons: bool = False) -> dict[str, Any]:
    """Select a deterministic mixed knowledge view from one resolved snapshot."""
    for name, limit in (("note_limit", note_limit), ("lesson_limit", lesson_limit)):
        if limit is not None and limit < 0:
            raise KnowledgeError(f"{name} must be non-negative")

    curated_lessons = [
        rec.get("curated")
        for rec in views.values()
        if rec.get("curated") is not None
        and rec["curated"].get("type") == TYPE_LESSON
        and is_curated(rec["curated"])
        and not is_retracted(rec["curated"])
    ]
    superseded = lesson_superseded_keys(curated_lessons)
    wanted_tags = {str(tag).casefold() for tag in (tags or [])}
    needle = (query or "").casefold()
    note_rows: list[tuple[dict, dict]] = []
    lesson_rows: list[tuple[dict, dict]] = []

    for (candidate_domain, _key), rec in views.items():
        if domain_id and candidate_domain != domain_id:
            continue
        latest = rec.get("latest")
        curated = rec.get("curated")
        if (include_uncurated and latest is not None
                and not is_curated(latest) and not is_retracted(latest)):
            note = latest
            view_kind = "proposal"
        else:
            note = curated
            view_kind = "curated"
        if note is None:
            continue
        note_type = note.get("type")
        if exclude_lessons and note_type == TYPE_LESSON:
            continue
        if type_filter and note_type != type_filter:
            continue
        if (scope or wanted_tags) and note_type != TYPE_LESSON:
            continue
        if is_retracted(note) and not include_stale:
            continue

        effective = effective_domain(
            str(note.get("domain_id") or ""), str(note_type or ""), domains)
        if note_type == TYPE_LESSON:
            lesson = note.get("lesson") or {}
            if scope and lesson.get("scope") != scope:
                continue
            applies_to = {
                str(tag).casefold() for tag in lesson.get("applies_to", [])
            }
            if wanted_tags and not (wanted_tags & applies_to):
                continue
            verdict = compute_lesson_state(
                note,
                now=now,
                superseded_keys=superseded,
                domain_exists=effective["exists"],
                current_registry_hash=registry_hash,
                current_domain_definition_hash=effective["definition_hash"],
            )
        else:
            verdict = compute_staleness(
                note,
                domain_exists=effective["exists"],
                current_registry_hash=registry_hash,
                current_domain_definition_hash=effective["definition_hash"],
                anchor_status=anchor_status_by_id.get(str(note.get("id") or ""), {}),
            )
        if verdict.get("hard_stale") and not include_stale:
            continue
        if view_kind == "proposal" and not include_uncurated:
            continue
        if needle and needle not in _knowledge_search_text(note):
            continue

        row = dict(note)
        row["view"] = view_kind
        if note_type == TYPE_LESSON:
            lesson_rows.append((row, verdict))
        else:
            note_rows.append((row, verdict))

    note_rows.sort(key=lambda row: (
        row[0].get("domain_id") or "",
        row[0].get("type") or "",
        row[0].get("key") or "",
    ))
    lesson_rows.sort(key=lambda row: _lesson_rank_key(row, context_scope))
    totals = {"notes": len(note_rows), "lessons": len(lesson_rows)}
    totals["all"] = totals["notes"] + totals["lessons"]
    shown_notes = note_rows if note_limit is None else note_rows[:note_limit]
    shown_lessons = lesson_rows if lesson_limit is None else lesson_rows[:lesson_limit]
    truncation = {
        "notes": max(0, len(note_rows) - len(shown_notes)),
        "lessons": max(0, len(lesson_rows) - len(shown_lessons)),
    }
    truncation["all"] = truncation["notes"] + truncation["lessons"]
    return {
        "notes": shown_notes,
        "lessons": shown_lessons,
        "totals": totals,
        "truncation": truncation,
        "problems": list(semantic_problems or []),
    }


# --------------------------------------------------------------- authority

def resolve_curation_authority(actor: str, *, owner_agents: list[str],
                               curator_agents: list[str], is_lead: bool) -> str | None:
    """Who may CURATE (verify/retract) a domain's notes: an owner, a curator, or a
    lead (override). Returns 'owner'|'curator'|'lead' or None (not authorized).
    PUBLISH is open to any active agent and is NOT gated here."""
    if actor in (curator_agents or []):
        return "curator"
    if actor in (owner_agents or []):
        return "owner"
    if is_lead:
        return "lead"
    return None


# --------------------------------------------------------------- persistence

def knowledge_dir(store):
    return store.dir / STORE_DIRNAME


def notes_path(store):
    return knowledge_dir(store) / NOTES_FILENAME


def write_event_locked(store, event: dict) -> None:
    """The SINGLE durable append path for a knowledge event (C4c) - used by BOTH publish
    and curate. Encodes exactly one event line, appends, flushes, and fsyncs the file so
    a crash cannot lose a just-recorded note/curation. The CALLER must already hold
    ``store._config_lock()``. Append-only - never whole-file replace, so the reader's
    skip-invalid/torn-tail tolerance is preserved."""
    append_record(notes_path(store), event)


def append_event(store, event: dict) -> None:
    """Append one event under the shared lock for callers without a transaction lock."""
    with store._config_lock():
        write_event_locked(store, event)


def read_events(store) -> tuple[list[dict], list[dict]]:
    """Fail-safe read: scan in file order, skip invalid/torn lines, NEVER let an
    invalid line hide a previous valid note. Returns (valid_events, problems) where
    problems = [{line, error}] for doctor."""
    path = notes_path(store)
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
            evt = _canonical_legacy_event(evt)
            if not _is_wellformed(evt):
                problems.append({"line": n, "error": "malformed event (missing required structure)"})
                continue
            valid.append(evt)
    except (OSError, UnicodeError) as e:
        problems.append({"line": 0, "error": f"unreadable: {e}"})
    return valid, problems
