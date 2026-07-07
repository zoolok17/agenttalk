"""Knowledge layer MVP (middle-tier Phase 2): durable, pointer-shaped project memory.

A *note* preserves the small piece of human/agent insight that is NOT in the
artifact (a seam, a gotcha, a decision + rationale); its ANCHOR points to the code/
thread that is. Consumers treat every note body as untrusted data and must reverify
the anchor before acting. Notes hang off the Phase-0 domain registry and go STALE
when their anchor changes (anchor-relative, not HEAD-relative).

Design (codex knowledge design, lead-gated; dev-2 + reviewer-1 consults folded in):
  * Store is ``.agenttalk/knowledge/notes.jsonl`` - append-only, one immutable event
    per line, preserved by reset (durable memory, not active bus state). The current
    view is the latest valid event per ``(domain_id, key)``.
  * CAPTURE is open (any active agent publishes an ``uncurated`` note); CURATION is
    gated (a domain owner/curator, or a lead override, verifies/retracts).
  * Staleness is ANCHOR-RELATIVE + PURE: :func:`compute_staleness` derives
    stale_reasons / caution_flags from already-resolved inputs (the CLI/git adapter
    does the I/O). A note is hard-stale only when its anchor actually changed; a
    moved HEAD with an unchanged anchor is a CAUTION, not stale (over-staling would
    empty the layer on every unrelated commit).
  * Pointer-not-mirror is a CONTENT rule (the body is the insight, not a copy of the
    anchor) backed by a byte cap; bodies are untrusted data, never instructions.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from agenttalk import domains as dom

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

_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_NOTE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SAFE_TAG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_AGENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
LESSON_TEXT_MAX_BYTES = 500
LESSON_TAG_LIMIT = 16
LESSON_SUPERSEDES_LIMIT = 16


class KnowledgeError(ValueError):
    """Invalid knowledge input / state (CLI maps to a usage exit)."""


def new_note_id() -> str:
    import uuid
    return "kn-" + uuid.uuid4().hex[:12]


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
                      domain_registry_hash: str, author: str, resolved_from: str,
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
                     at: str, reason: str | None) -> dict[str, Any]:
    """A CURATE (verify) or RETRACT event over an existing note (same key/domain).
    verify -> authority.state=verified (or lead_override); retract -> terminal
    tombstone for that key until a later publish supersedes it."""
    if action not in ("verify", "retract"):
        raise KnowledgeError("curate action must be verify or retract")
    if action == "retract" and not (reason and reason.strip()):
        raise KnowledgeError("a retract requires a reason")
    state = AUTH_RETRACTED if action == "retract" else (
        AUTH_LEAD_OVERRIDE if resolved_from == "lead" else AUTH_VERIFIED)
    evt = dict(base)
    evt["event"] = EVENT_RETRACT if action == "retract" else EVENT_CURATE
    evt["id"] = new_note_id()
    evt["authority"] = {"state": state, "resolved_from": resolved_from, "reason": reason}
    evt["curated_by"] = curated_by
    evt["curated_at"] = at
    evt["updated_at"] = at
    if evt.get("type") == TYPE_LESSON:
        lesson = validate_lesson(evt.get("lesson"))
        lesson["status"] = LESSON_STATUS_RETIRED if action == "retract" else LESSON_STATUS_ACCEPTED
        lesson["curator"] = curated_by
        evt["lesson"] = validate_lesson(lesson)
    if action == "retract":
        evt["retract_reason"] = reason
    return evt


# --------------------------------------------------------------- current view

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
    for key in ("id", "key", "domain_id"):
        if not isinstance(evt.get(key), str) or not evt.get(key):
            return f"{key} is required"
    if not isinstance(evt.get("domain_registry_hash"), str) or not evt.get("domain_registry_hash"):
        return "domain_registry_hash is required"
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
    if event == EVENT_RETRACT and not (
            isinstance(evt.get("retract_reason"), str) and evt.get("retract_reason").strip()):
        return "a retract event requires a non-empty retract_reason"
    try:
        validate_key(evt["key"])
        validate_note_id(evt["id"])
        note_type = validate_type(evt.get("type"))
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


def current_view(events: list[dict]) -> dict[tuple, dict]:
    """PURE: fold append-only events into the latest VALID event per (domain_id, key),
    in file order (later wins). Invalid events are skipped (fail-safe - a malformed
    latest line never hides a valid note). A retract is kept as the terminal event."""
    view: dict[tuple, dict] = {}
    for evt in events:
        if not _is_wellformed(evt):
            continue
        view[(evt["domain_id"], evt["key"])] = evt
    return view


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
    out: dict[tuple, dict] = {}
    for evt in events:
        if not _is_wellformed(evt):
            continue
        k = (evt["domain_id"], evt["key"])
        rec = out.setdefault(k, {"latest": None, "curated": None, "tombstoned": False})
        rec["latest"] = evt
        state = (evt.get("authority") or {}).get("state")
        if evt.get("event") == EVENT_RETRACT or state == AUTH_RETRACTED:
            rec["curated"] = evt
            rec["tombstoned"] = True
        elif state in (AUTH_VERIFIED, AUTH_LEAD_OVERRIDE):
            rec["curated"] = evt
            rec["tombstoned"] = False
        # an uncurated publish updates `latest` only (capture-open, curate-gated)
    return out


def is_retracted(note: dict) -> bool:
    return note.get("event") == EVENT_RETRACT or \
        (note.get("authority") or {}).get("state") == AUTH_RETRACTED


def is_curated(note: dict) -> bool:
    return (note.get("authority") or {}).get("state") in (AUTH_VERIFIED, AUTH_LEAD_OVERRIDE)


# --------------------------------------------------------------- pure staleness

def compute_staleness(note: dict, *, domain_exists: bool, current_registry_hash: str,
                      anchor_status: dict) -> dict[str, Any]:
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
    if not domain_exists:
        reasons.append(STALE_DOMAIN_GONE)
    if note.get("domain_registry_hash") != current_registry_hash:
        reasons.append(STALE_REGISTRY_CHANGED)

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
                         superseded_keys: set[str] | None = None) -> dict[str, Any]:
    """PURE lesson freshness. Lesson staleness is date/status/key based only.

    The optional lesson anchor is provenance and filtering context, never freshness
    authority, so callers do not pass anchor/git status here.
    """
    tnow = _coerce_now(now)
    lesson = validate_lesson(note.get("lesson"))
    reasons: list[str] = []
    cautions: list[str] = []
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
    ``store._config_lock()`` (curate reads-then-appends under one lock to avoid a nested
    lock; publish goes through :func:`append_event`). Append-only - never whole-file
    replace, so the reader's skip-invalid/torn-tail tolerance is preserved."""
    knowledge_dir(store).mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    path = notes_path(store)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    # Directory fsync is POSIX durability for the new dir entry; best-effort and guarded
    # because opening/fsyncing a directory fd is not portable to Windows (the primary
    # platform) - it raises there, so swallow OSError and rely on the file fsync.
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def append_event(store, event: dict) -> None:
    """Append exactly one event under the shared store lock (public publish path).
    Delegates the durable write to :func:`write_event_locked` so publish and curate share
    ONE write path (reuse the lane/gate file-lock primitive - no new locking scheme)."""
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
        if not _is_wellformed(evt):
            problems.append({"line": n, "error": "malformed event (missing required structure)"})
            continue
        valid.append(evt)
    return valid, problems
