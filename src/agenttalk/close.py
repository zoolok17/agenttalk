"""Assurance P2: milestone/release CLOSE protocol (generic, opt-in).

A `close` aggregates the 0.32.0 assurance signals - gate state (HOLD/GO), typed
review-result evidence, and named-gate-bound remediation - into ONE auditable
release-confidence verdict for a frozen revision, gathered from a declared set of
required review LENSES, published as a final HOLD|GO by a (advisory) lead.

Design (codex P2 design, lead-gated; dev-2 review folded in):
  * State is a per-close ATOMIC fail-closed JSON file, `.agenttalk/closes/<id>.json`
    - mirrors gates.json; the bus carries the evidence, the file carries pointers.
  * The verdict (:func:`compute_verdict`) is a PURE function over (close record,
    gate-check result) emitting STABLE hold codes, so it is unit-testable from
    synthetic inputs - the CLI is the thin I/O shell (mirrors build_report /
    plan_actions / check_gates).
  * The global epoch/barrier is NOT the close freeze (audit-only at open). A
    `publish --verdict go --bump-barrier` fires ONE explicit release barrier AFTER
    recording GO; a HOLD never bumps. Post-publish acks are refused unless a lead
    reopens (stale-proof WITHOUT a team-wide bump).
  * ADVISORY, not authz: authority/lens checks are mechanical (reuse the roster's
    sole-lead / operator-facing / configured close-lead). The published verdict is
    a strong signal + audit trail, never an enforced lock.
  * Reuses gates.check_gates / gate state / barrier; never reinvents gate semantics
    and never auto-creates or mutates gates. Coexists with spec-kitty (references
    mission/WP ids in evidence/remediation, never alters WP state).

P3 (HELD, not here): role->member routing, specialist discovery, skill rubrics,
severity policy, ephemeral adversarial reviewers.
"""

from __future__ import annotations

import json
import re
from typing import Any

SCHEMA_VERSION = 1
DIRNAME = "closes"

# A close moves through these statuses; `published` is terminal (HOLD or GO).
OPEN = "open"
PUBLISHED = "published"
REOPENED = "reopened"
STATUSES = frozenset({OPEN, PUBLISHED, REOPENED})

# A required lens reaches ONE terminal ack.
ACCEPT = "accept"
COUNTER = "counter"
NA = "na"
ACK_STATUSES = frozenset({ACCEPT, COUNTER, NA})

COUNTER_PENDING = "pending"
COUNTER_ACCEPTED = "accepted"
COUNTER_REJECTED = "rejected"

VERDICT_GO = "GO"
VERDICT_HOLD = "HOLD"

# A named blocker-remediation gate counts as resolved only when its check status
# is one of these (and it is non-blocking). gates.py guarantees a blocker goes
# `green` only from automation_ci, and `waived` only with a valid operator waiver.
RESOLVED_GATE_STATUSES = frozenset({"green", "waived"})

# STABLE hold codes (the public verdict contract - tests assert each one).
HOLD_MALFORMED = "malformed_state"
HOLD_REVISION = "revision_dirty_or_unresolved"
HOLD_GATE = "gate_hold"
HOLD_MISSING_LENS = "missing_lens"
HOLD_UNAUTHORIZED_ACK = "unauthorized_lens_ack"
HOLD_STALE_ACK = "stale_lens_ack"
HOLD_UNDECIDED_COUNTER = "undecided_counter"
HOLD_COUNTER_NO_REMEDIATION = "accepted_counter_missing_remediation"
HOLD_OPEN_BLOCKER = "open_blocker_remediation"
HOLD_PUBLISH_NOT_ALLOWED = "publish_not_allowed"

_CLOSE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class CloseError(ValueError):
    """Invalid close input / state (CLI maps to a usage exit)."""


def empty_close(close_id: str, *, scope: str, revision: str, revision_kind: str,
                gate_scope: str, opened_by: str, opened_at: str,
                epoch_at_open: str | None, required_lenses: list[dict],
                revision_clean: bool, dirty_artifact: str | None) -> dict[str, Any]:
    """A freshly OPENED close record (pure; the CLI persists it)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "close_id": close_id,
        "scope": scope,
        "revision": revision,
        "revision_kind": revision_kind,        # "sha" | "ref" (resolved) - audit
        "revision_clean": bool(revision_clean),
        "dirty_artifact": dirty_artifact,       # pointer when the worktree was dirty
        "gate_scope": gate_scope,
        "opened_by": opened_by,
        "opened_at": opened_at,
        "epoch_at_open": epoch_at_open,         # AUDIT ONLY - never the freeze
        "status": OPEN,
        "required_lenses": required_lenses,     # [{id, allowed_agents, allowed_roles, required}]
        "lens_acks": {},                        # lens_id -> ack record
        "counters": {},                         # counter_id -> counter record
        "remediation_items": {},                # item_id -> remediation record
        "draft": None,                          # merged human draft (lead)
        "final": None,                          # snapshot recorded at publish
        "events": [],                           # append-only audit trail
    }


# --------------------------------------------------------------- pure verdict

def compute_verdict(record: dict, gate_check: dict) -> dict[str, Any]:
    """PURE: derive HOLD|GO + the stable hold codes from a close ``record`` and a
    ``gate_check`` result (:func:`gates.check_gates` output for the close's
    gate_scope). No I/O. GO requires, in order of the codes below: a well-formed
    record on a frozen, resolved, clean (or dirty-with-artifact) revision; the gate
    check GO; every REQUIRED lens terminally satisfied by an AUTHORIZED,
    non-stale ack (accept / na-with-reason / counter-that-was-decided); every
    counter decided; every ACCEPTED counter carrying a remediation item; and every
    OPEN blocker remediation resolved by its named gate being green. Returns
    ``{"verdict", "holds": [{code, detail}], "ok"}``."""
    holds: list[dict] = []

    def hold(code: str, detail: str) -> None:
        holds.append({"code": code, "detail": detail})

    if not _is_wellformed(record):
        hold(HOLD_MALFORMED, "close record is missing required structure")
        return _verdict(holds)   # nothing else is trustworthy

    if record.get("status") == PUBLISHED:
        # already terminal: re-deriving is fine for `check`, but a GO cannot be
        # re-published over a published close (publish() guards this); surfaced so
        # `check` on a published close reports it rather than implying re-publish.
        hold(HOLD_PUBLISH_NOT_ALLOWED, "close is already published; reopen to change it")

    if not record.get("revision_clean") and not record.get("dirty_artifact"):
        hold(HOLD_REVISION, "revision worktree was dirty with no recorded diff artifact")
    if not _FULL_SHA_RE.match(str(record.get("revision", ""))):
        hold(HOLD_REVISION, "revision is not a resolved full 40-char SHA")

    if gate_check.get("verdict") != VERDICT_GO:
        names = ", ".join(b.get("name", "?") for b in gate_check.get("blockers", [])) or "?"
        hold(HOLD_GATE, f"gate check for scope is HOLD (blockers: {names})")

    revision = record.get("revision")
    acks = record.get("lens_acks", {})
    counters = record.get("counters", {})
    for lens in record.get("required_lenses", []):
        if not (isinstance(lens, dict) and lens.get("required", True)):
            continue
        lid = lens.get("id")
        ack = acks.get(lid)
        if not isinstance(ack, dict):
            hold(HOLD_MISSING_LENS, f"required lens {lid!r} has no ack")
            continue
        if not _ack_authorized(ack, lens):
            hold(HOLD_UNAUTHORIZED_ACK,
                 f"lens {lid!r} ack from {ack.get('from')!r} is not authorized")
        if ack.get("revision") != revision:
            hold(HOLD_STALE_ACK,
                 f"lens {lid!r} ack was given against a different revision (stale)")
        status = ack.get("status")
        if status == COUNTER:
            cid = ack.get("counter_id")
            counter = counters.get(cid) if isinstance(cid, str) else None
            if not isinstance(counter, dict) or counter.get("decision") == COUNTER_PENDING:
                hold(HOLD_UNDECIDED_COUNTER,
                     f"lens {lid!r} raised counter {cid!r} which is not decided")
        # accept / na are terminal here (na must carry a reason - validated at ack)

    # every ACCEPTED counter must carry a remediation item; a blocker remediation
    # must name a gate that is currently green (the SINGLE resolution authority).
    rem = record.get("remediation_items", {})
    green = _green_gate_names(gate_check)
    for cid, counter in counters.items():
        if not isinstance(counter, dict) or counter.get("decision") != COUNTER_ACCEPTED:
            continue
        item_id = counter.get("remediation_id")
        item = rem.get(item_id) if isinstance(item_id, str) else None
        if not isinstance(item, dict):
            hold(HOLD_COUNTER_NO_REMEDIATION,
                 f"accepted counter {cid!r} has no remediation item")
            continue
        if item.get("blocker") and item.get("gate") not in green:
            hold(HOLD_OPEN_BLOCKER,
                 f"blocker remediation {item_id!r} gate {item.get('gate')!r} is not green")
    return _verdict(holds)


def _verdict(holds: list[dict]) -> dict[str, Any]:
    return {"verdict": VERDICT_GO if not holds else VERDICT_HOLD,
            "holds": holds, "ok": not holds}


def _green_gate_names(gate_check: dict) -> set[str]:
    """Names of gates a blocker remediation may rely on as RESOLVED: the gate must
    be GREEN or actively WAIVED *and* non-blocking in this check. gates.py is the
    single authority that a blocker gate only goes green from automation_ci and
    that a waiver is valid/unexpired; here we additionally refuse a merely
    non-blocking gate (e.g. red/warn, skipped, unknown/info) so a blocker
    remediation is resolved only by a truly green or waived gate, never an
    unrelated low-severity/skipped one."""
    blocked = {b.get("name") for b in gate_check.get("blockers", [])}
    resolved: set[str] = set()
    for g in gate_check.get("gates", []):
        name = g.get("name")
        if name in blocked or g.get("blocks"):
            continue
        if str(g.get("status")) in RESOLVED_GATE_STATUSES:
            resolved.add(name)
    return resolved


def _ack_authorized(ack: dict, lens: dict) -> bool:
    if ack.get("override"):           # a recorded lead/operator override
        return True
    agent = ack.get("from")
    if agent in (lens.get("allowed_agents") or []):
        return True
    return ack.get("from_role") in (lens.get("allowed_roles") or [])


def _is_wellformed(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("schema_version") != SCHEMA_VERSION:
        return False
    for key in ("close_id", "scope", "revision", "gate_scope", "status"):
        if not isinstance(record.get(key), str) or not record.get(key):
            return False
    if record.get("status") not in STATUSES:
        return False
    for key in ("required_lenses",):
        if not isinstance(record.get(key), list):
            return False
    for key in ("lens_acks", "counters", "remediation_items"):
        if not isinstance(record.get(key), dict):
            return False
    return True


# --------------------------------------------------------------- validators

def validate_close_id(value: str) -> str:
    if not isinstance(value, str) or not _CLOSE_ID_RE.match(value):
        raise CloseError(
            f"close id {value!r} is not a safe identifier (alphanumeric plus . _ -, "
            "starts alphanumeric, max 64 chars)")
    return value


def validate_lens_spec(raw: dict) -> dict:
    lid = raw.get("id")
    if not isinstance(lid, str) or not lid:
        raise CloseError("each required lens needs a non-empty id")
    return {
        "id": lid,
        "allowed_agents": _str_list(raw.get("allowed_agents")),
        "allowed_roles": _str_list(raw.get("allowed_roles")),
        "required": bool(raw.get("required", True)),
    }


def _str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CloseError("allowed_agents/allowed_roles must be lists of strings")
    return list(value)


# --------------------------------------------------------------- persistence

def closes_dir(store):
    return store.dir / DIRNAME


def close_path(store, close_id: str):
    return closes_dir(store) / f"{validate_close_id(close_id)}.json"


def load_close(store, close_id: str) -> dict:
    """Load a close record; fail closed (CloseError) on missing / unreadable /
    malformed - the verdict treats a malformed record as HOLD, but the CLI surfaces
    a not-found / parse error directly."""
    path = close_path(store, close_id)
    if not path.exists():
        raise CloseError(f"no close {close_id!r} at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise CloseError(f"close {close_id!r} is unreadable/corrupt: {e}") from e
    if not _is_wellformed(data):
        raise CloseError(f"close {close_id!r} is malformed")
    return data


def save_close(store, record: dict) -> None:
    """Persist a close record atomically (sandbox-safe writer)."""
    from agenttalk import _atomic
    closes_dir(store).mkdir(parents=True, exist_ok=True)
    _atomic.write_text(close_path(store, record["close_id"]),
                       json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))


def list_close_ids(store) -> list[str]:
    d = closes_dir(store)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


# ----------------------------------------------- pure state transitions
#
# Each takes the record (+ inputs) and mutates it IN PLACE, appending an audit
# event; the CLI does the I/O (git SHA resolve, gate check, barrier, roster
# authority) around them and persists. Kept pure so the state machine is testable.

def _event(record: dict, kind: str, by: str, at: str, **fields: Any) -> None:
    record.setdefault("events", []).append(
        {"event": kind, "by": by, "at": at, **fields})


def apply_ack(record: dict, *, lens_id: str, status: str, agent: str,
              from_role: str | None, at: str, evidence: dict | None = None,
              reason: str | None = None, counter_id: str | None = None,
              override: bool = False) -> dict:
    """Record a lens ack. Refuses if the close is not accepting acks (must be open/
    reopened, not published). ACCEPT carries typed evidence (validated by the
    caller via gates.validate_review_result_evidence); NA needs a reason; COUNTER
    needs a counter_id (the caller also creates the counter record)."""
    if record.get("status") == PUBLISHED:
        raise CloseError("close is published; reopen before acking (stale-proof)")
    if status not in ACK_STATUSES:
        raise CloseError(f"ack status must be one of {sorted(ACK_STATUSES)}")
    if status == NA and not (reason and reason.strip()):
        raise CloseError("an NA ack requires a reason")
    if status == COUNTER and not counter_id:
        raise CloseError("a COUNTER ack requires a counter_id")
    record["lens_acks"][lens_id] = {
        "lens": lens_id, "status": status, "from": agent, "from_role": from_role,
        "revision": record.get("revision"), "at": at,
        "evidence": evidence or {}, "reason": reason, "counter_id": counter_id,
        "override": bool(override),
    }
    if status == COUNTER:
        record["counters"].setdefault(counter_id, {
            "counter_id": counter_id, "lens": lens_id, "raised_by": agent,
            "at": at, "decision": COUNTER_PENDING, "remediation_id": None,
            "finding": (evidence or {}).get("finding") or reason or "",
        })
    _event(record, f"ack:{status}", agent, at, lens=lens_id, counter_id=counter_id)
    return record


def decide_counter(record: dict, *, counter_id: str, decision: str, by: str,
                   at: str, reason: str, remediation: dict | None = None) -> dict:
    """Lead accept/reject a counter. ACCEPT requires a remediation item; a
    blocker remediation MUST name a gate (GO then needs that gate green)."""
    if record.get("status") == PUBLISHED:
        raise CloseError("close is published; reopen before deciding counters")
    counter = record.get("counters", {}).get(counter_id)
    if not isinstance(counter, dict):
        raise CloseError(f"no counter {counter_id!r} on this close")
    if decision not in (COUNTER_ACCEPTED, COUNTER_REJECTED):
        raise CloseError("counter decision must be accept or reject")
    if not (reason and reason.strip()):
        raise CloseError("a counter decision requires a reason")
    counter["decision"] = decision
    counter["decided_by"] = by
    counter["decided_at"] = at
    counter["decision_reason"] = reason
    if decision == COUNTER_ACCEPTED:
        item = _validate_remediation(remediation)
        item_id = item["id"]
        record["remediation_items"][item_id] = item
        counter["remediation_id"] = item_id
    _event(record, f"counter:{decision}", by, at, counter_id=counter_id)
    return record


def _validate_remediation(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise CloseError("accepting a counter requires a remediation item")
    rid = raw.get("id")
    if not isinstance(rid, str) or not rid:
        raise CloseError("remediation needs an id")
    blocker = bool(raw.get("blocker"))
    gate = raw.get("gate")
    if blocker and (not isinstance(gate, str) or not gate):
        raise CloseError("a blocker remediation MUST name a gate id "
                         "(GO requires that gate green from CI or a waiver)")
    for field in ("owner", "fix", "verification"):
        if not isinstance(raw.get(field), str) or not raw.get(field).strip():
            raise CloseError(f"remediation needs a non-empty {field}")
    return {
        "id": rid, "owner": raw["owner"], "severity": raw.get("severity", "unknown"),
        "affected": _str_list(raw.get("affected")), "blocker": blocker,
        "gate": gate, "fix": raw["fix"], "verification": raw["verification"],
        "regression_test": raw.get("regression_test"), "target": raw.get("target"),
    }


def set_draft(record: dict, *, body: str, by: str, at: str) -> dict:
    if record.get("status") == PUBLISHED:
        raise CloseError("close is published; reopen before redrafting")
    record["draft"] = {"body": body, "by": by, "at": at}
    _event(record, "draft", by, at)
    return record


def record_publish(record: dict, *, verdict: str, by: str, at: str, reason: str,
                   gate_check: dict, residual_risk: str | None,
                   barrier_epoch: str | None) -> dict:
    """Snapshot a publish into the record. The CALLER must have re-run
    compute_verdict and refused a GO that is not actually GO; this only records the
    terminal snapshot (and the release barrier id, when one was bumped for a GO)."""
    record["status"] = PUBLISHED
    record["final"] = {
        "verdict": verdict, "by": by, "at": at, "reason": reason,
        "gate_verdict": gate_check.get("verdict"),
        "blockers": [b.get("name") for b in gate_check.get("blockers", [])],
        "residual_risk": residual_risk,
        "remediation_ids": sorted(record.get("remediation_items", {})),
        "barrier_epoch": barrier_epoch,
    }
    _event(record, f"publish:{verdict}", by, at, barrier_epoch=barrier_epoch)
    return record


def reopen(record: dict, *, by: str, at: str, revision: str | None = None,
           revision_clean: bool | None = None) -> dict:
    """Reopen a published close (operator). If the revision changed, prior lens
    acks are STALE by construction (compute_verdict compares ack.revision to the
    record revision), so we just update the revision and let the verdict re-flag."""
    record["status"] = REOPENED
    record["final"] = None
    if revision is not None:
        record["revision"] = revision
    if revision_clean is not None:
        record["revision_clean"] = bool(revision_clean)
    _event(record, "reopen", by, at, revision=revision)
    return record
