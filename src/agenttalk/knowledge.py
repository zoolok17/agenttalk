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
import re
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
NOTE_TYPES = frozenset({TYPE_SEAM, TYPE_GOTCHA, TYPE_DECISION, TYPE_POINTER})

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
# caution flags (shown, NOT excluded by default)
CAUTION_SHA_NOT_HEAD = "verified_sha_not_head"
CAUTION_UNCURATED = "uncurated"
CAUTION_WEAK_SYMBOL = "weak_symbol_evidence"

_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_NOTE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


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
                      anchor: dict, verified_against_sha: str | None,
                      domain_registry_hash: str, author: str, resolved_from: str,
                      at: str, supersedes_id: str | None = None,
                      supersedes_key: str | None = None) -> dict[str, Any]:
    """A PUBLISH event (capture). Always ``uncurated`` - curation is a separate
    event. Pure; the CLI persists it under the lock."""
    if verified_against_sha is not None and not _FULL_SHA_RE.match(str(verified_against_sha)):
        raise KnowledgeError("verified_against_sha must be a full 40-char SHA when present")
    return {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_PUBLISH,
        "id": validate_note_id(note_id),
        "key": validate_key(key),
        "type": validate_type(type),
        "domain_id": domain_id,
        "body": validate_body(body),
        "anchor": validate_anchor(anchor),
        "verified_against_sha": verified_against_sha,
        "domain_registry_hash": domain_registry_hash,
        "author": author,
        "authority": {"state": AUTH_UNCURATED, "resolved_from": resolved_from, "reason": None},
        "created_at": at,
        "updated_at": at,
        "supersedes_id": supersedes_id,
        "supersedes_key": supersedes_key,
    }


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
        validate_type(evt.get("type"))
        validate_body(evt.get("body"))
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
        if anchor_status.get("anchor_exists") is False:
            reasons.append(STALE_ANCHOR_GONE)
        elif anchor_status.get("anchor_changed") is True or anchor_status.get("anchor_changed") is None:
            # changed OR could-not-be-determined -> hard stale (never infer fresh)
            reasons.append(STALE_ANCHOR_CHANGED)
        if kind == "symbol":
            ev = anchor_status.get("evidence_match")
            if ev is False:
                reasons.append(STALE_SYMBOL_MISMATCH)
            elif ev is None:
                cautions.append(CAUTION_WEAK_SYMBOL)
    elif kind in ("request", "wp", "sha"):
        if anchor_status.get("target_resolvable") is False:
            reasons.append(STALE_TARGET_UNRESOLVABLE)

    return {"stale_reasons": sorted(set(reasons)),
            "caution_flags": sorted(set(cautions)),
            "hard_stale": bool(reasons)}


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


def append_event(store, event: dict) -> None:
    """Append exactly one newline-terminated JSON event under the shared store lock
    (reuse the lane/gate file-lock primitive - no new locking scheme)."""
    knowledge_dir(store).mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with store._config_lock():
        with open(notes_path(store), "a", encoding="utf-8") as fh:
            fh.write(line)


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
