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

import contextlib
from decimal import Decimal
import hashlib
import json
import math
import re
import uuid
from typing import Any

from agenttalk.coverage_contract import COVERAGE_GATE_NAMES, coverage_profile_from_gate
from agenttalk.gates import (
    CORE_RISK_CLASSES,
    is_valid_risk_class,
    validate_review_result_evidence,
)

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
# P3 specialist sign-off hold codes (join the same stable set).
HOLD_MISSING_SIGNOFF = "missing_required_signoff"
HOLD_UNROUTABLE_SIGNOFF = "unroutable_required_signoff"
HOLD_INVALID_POLICY = "invalid_signoff_policy"
HOLD_UNMAPPED_RISK = "unmapped_required_risk"
HOLD_STALE_ROUTE = "stale_signoff_route"
HOLD_WORKTREE_ISOLATION = "worktree_isolation_unverified"
# DoD forcing-gate hold codes (#60) - the "red-by-default until typed evidence exists" dimensions.
HOLD_INVALID_DOD_POLICY = "invalid_dod_policy"
HOLD_MISSING_ASSURANCE = "missing_assurance_evidence"
HOLD_STALE_ASSURANCE = "stale_assurance_evidence"
HOLD_UNATTESTED_ASSURANCE = "unattested_assurance_evidence"
HOLD_MISSING_COVERAGE = "missing_coverage_evidence"
HOLD_LOW_COVERAGE = "low_coverage_evidence"
HOLD_STALE_COVERAGE = "stale_coverage_evidence"
HOLD_MISSING_KNOWLEDGE = "missing_knowledge_evidence"
HOLD_TRIVIAL_EVIDENCE = "trivial_knowledge_evidence"

RELEASE_CLASS_SCOPES = {"release", "milestone", "feature", "hotfix"}

SIGNOFF_DIRNAME = "signoffs.json"
DOD_DIRNAME = "dod.json"
# Dimensions the DoD evaluator can actually enforce. This set GROWS per increment; declaring a
# dimension the engine cannot enforce is a policy error (you must not be able to require what
# cannot be checked - a silent soft-pass is exactly the failure this gate exists to prevent).
# inc-1: assurance only. inc-2 adds knowledge; inc-3 adds coverage + a depth-signoff dimension.
_DOD_SUPPORTED_DIMENSIONS = frozenset({"assurance", "coverage", "knowledge"})
_KNOWLEDGE_NOTE_TYPES = frozenset({"decision", "gotcha", "lesson", "pointer", "seam"})
# The DoD knowledge dimension only counts DELIBERATE, human-authored knowledge (a lesson, a
# gotcha, a decision) as evidence that "what we learned was written down". seam/pointer are
# structural index notes, NOT that kind of evidence - a policy must not be able to satisfy the
# knowledge gate with them, so the CONFIGURABLE types are this trio, a strict subset of
# _KNOWLEDGE_NOTE_TYPES (a policy may narrow to a subset of the trio, never widen past it).
_DOD_KNOWLEDGE_ALLOWED_TYPES = frozenset({"decision", "gotcha", "lesson"})
_DOD_KNOWLEDGE_KEYS = frozenset({"when", "min_notes", "types", "min_body_chars"})
_DOD_ALLOWED_TOP_KEYS = frozenset({"schema_version", "scopes"})
_DOD_ASSURANCE_KEYS = frozenset({"gate", "max_age_days"})
_DOD_COVERAGE_KEYS = frozenset({"gate", "min_percent", "max_age_days"})
_DOD_SCHEMA_VERSION = 1
# Fail-closed bounds on the policy file itself: reject an oversized dod.json before decode, so a
# pathological deeply-nested document cannot exhaust the parser (RecursionError) or memory.
_DOD_MAX_BYTES = 64 * 1024
# Bounded clock-skew allowance for freshness: an attestation timestamp up to this far in the
# future is tolerated (clock jitter); beyond it is treated as an invalid future-dated attestation.
_DOD_CLOCK_SKEW_DAYS = 300.0 / 86400.0

_CLOSE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_INSTANCE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


class CloseError(ValueError):
    """Invalid close input / state (CLI maps to a usage exit)."""


class CloseConflict(CloseError):
    """A close changed after a caller loaded it; the mutation was not persisted."""


def empty_close(close_id: str, *, scope: str, revision: str, revision_kind: str,
                gate_scope: str, opened_by: str, opened_at: str,
                epoch_at_open: str | None, required_lenses: list[dict],
                revision_clean: bool, dirty_artifact: str | None,
                lane_delivery_artifact: str | None = None,
                non_lane_isolation_not_asserted: bool = False) -> dict[str, Any]:
    """A freshly OPENED, unpersisted close record."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": 0,
        "instance_id": None,
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
        "required_lenses": required_lenses,     # [{id, allowed_agents, allowed_roles, allowed_groups, required}]
        "lane_delivery_artifact": lane_delivery_artifact,
        "non_lane_isolation_not_asserted": bool(non_lane_isolation_not_asserted),
        "worktree_isolation": None,
        "lens_acks": {},                        # lens_id -> ack record
        "counters": {},                         # counter_id -> counter record
        "remediation_items": {},                # item_id -> remediation record
        # P3: explicit risk inventory + DERIVED required signoffs (set by `apply`).
        "risk_inventory": [],                   # [{risk_class, source, affected_paths, na_reason}]
        "required_signoffs": [],                # first-class derived signoff sets (see apply_signoffs)
        "signoff_overrides": {},                # set_id -> {by, reason, at} (the unroutable lead escape)
        "signoff_route": None,                  # {policy_hash, risk_inventory_hash, revision, derived_at, derived_by}
        "draft": None,                          # merged human draft (lead)
        "final": None,                          # snapshot recorded at publish
        "events": [],                           # append-only audit trail
    }


# --------------------------------------------------------------- pure verdict

def compute_verdict(record: dict, gate_check: dict,
                    signoff_eval: dict | None = None,
                    worktree_eval: dict | None = None,
                    dod_eval: dict | None = None) -> dict[str, Any]:
    """PURE: derive HOLD|GO + the stable hold codes from a close ``record`` and a
    ``gate_check`` result (:func:`gates.check_gates` output for the close's
    gate_scope). No I/O. GO requires, in order of the codes below: a well-formed
    record on a frozen, resolved, clean (or dirty-with-artifact) revision; the gate
    check GO; every REQUIRED lens terminally satisfied by an AUTHORIZED,
    non-stale ack (accept / na-with-reason / counter-that-was-decided); every
    counter decided; every ACCEPTED counter carrying a remediation item; every
    OPEN blocker remediation resolved by its named gate being green; and (P3) every
    derived required signoff met by enough DISTINCT qualifying acks.

    ``signoff_eval`` is the impure->pure bridge for P3: the CLI resolves the signoff
    policy + roster/group/role/domain refsets + git diff (all I/O) and passes the
    already-resolved evaluation in; this function only COUNTS, so it stays pure and
    unit-testable. It is None for a P2-only close; a close that HAS
    ``required_signoffs`` but is handed no ``signoff_eval`` fails closed (the CLI
    always supplies one for P3 closes).

    ``dod_eval`` is the same impure->pure bridge for the #60 Definition-of-Done
    forcing gate: the CLI resolves the DoD policy + the required evidence (e.g. the
    ``assurance:<scope>`` gate's fields) and passes the already-resolved bundle in;
    :func:`evaluate_dod` only COUNTS. It is ``None`` when the close's scope has no
    DoD requirements, in which case the fold is a no-op (byte-identical to today).
    Returns ``{"verdict", "holds": [{code, detail}], "ok"}``."""
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

    if str(record.get("scope") or "").lower() in RELEASE_CLASS_SCOPES:
        if record.get("non_lane_isolation_not_asserted"):
            pass
        elif not isinstance(worktree_eval, dict):
            hold(HOLD_WORKTREE_ISOLATION,
                 "release-class close lacks verified lane worktree isolation evidence")
        else:
            status = worktree_eval.get("status")
            if status in {"verified", "waived"}:
                if worktree_eval.get("delivered_head") != record.get("revision"):
                    hold(HOLD_WORKTREE_ISOLATION,
                         "lane delivery artifact head does not match close revision")
            else:
                detail = worktree_eval.get("reason") or "lane worktree isolation is unverified"
                hold(HOLD_WORKTREE_ISOLATION, str(detail))

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

    # P3: derived specialist sign-off counts (pure over the CLI-supplied eval).
    for code, detail in _evaluate_signoffs(record, signoff_eval):
        hold(code, detail)
    # DoD forcing gate (#60): red-by-default evidence dimensions, pure over the CLI-supplied
    # ``dod_eval`` bundle (same impure->pure bridge as signoff_eval). Purely ADDITIVE - it can
    # only ADD holds, never remove one, so it cannot loosen today's verdict. None ⇒ no DoD.
    for code, detail in evaluate_dod(record, dod_eval):
        hold(code, detail)
    return _verdict(holds)


def _evaluate_signoffs(record: dict, ev: dict | None) -> list[tuple[str, str]]:
    """PURE: given a close ``record`` and a CLI-resolved ``ev`` bundle, return
    (code, detail) signoff holds. ``ev`` carries everything that needed I/O to
    produce - resolved current candidate agents per set, the active roster, the
    current policy/risk-inventory hashes, and any policy/unmapped errors - so the
    counting here reads NO config.

    ev = {
      "policy_present": bool,
      "policy_error": str | None,          # malformed policy / bad refset
      "current_policy_hash": str,
      "current_risk_inventory_hash": str,
      "unmapped_risks": [risk_class, ...], # declared non-none risks with no mapping
      "resolved_candidates": {set_id: [agent, ...]},  # who may currently sign each set
      "active_agents": [agent, ...],       # current active roster (retired excluded)
    }
    """
    if not record.get("signoff_route"):
        return []                       # P2-only close (apply never run): nothing to do
    req = record.get("required_signoffs") or []
    if not isinstance(ev, dict):
        # fail closed: a P3 close (apply ran) MUST be evaluated with a resolved bundle.
        return [(HOLD_INVALID_POLICY,
                 "signoff route present but no signoff evaluation supplied")]
    out: list[tuple[str, str]] = []
    if ev.get("policy_error"):
        return [(HOLD_INVALID_POLICY, str(ev["policy_error"]))]
    for rc in ev.get("unmapped_risks") or []:
        out.append((HOLD_UNMAPPED_RISK,
                    f"declared risk {rc!r} has no signoff policy mapping (allow_unmapped is false)"))
    route = record.get("signoff_route") or {}
    # The route is stale if the policy, the risk inventory, OR the REVISION it was
    # derived for no longer matches - a reopen to new code can change which files
    # are touched and therefore which specialists are required (reviewer-1 blocker).
    if (route.get("policy_hash") != ev.get("current_policy_hash")
            or route.get("risk_inventory_hash") != ev.get("current_risk_inventory_hash")
            or route.get("revision") != record.get("revision")):
        out.append((HOLD_STALE_ROUTE,
                    "signoff route is stale (policy, risk inventory, or revision "
                    "changed since `close signoffs apply`); rerun apply"))
    acks = record.get("lens_acks", {})
    revision = record.get("revision")
    active = set(ev.get("active_agents") or [])
    resolved = ev.get("resolved_candidates") or {}
    overrides = record.get("signoff_overrides", {}) or {}
    for s in req:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if sid in overrides:
            continue                    # recorded close-lead escape (audited, not counted)
        required_count = int(s.get("required_count") or 0)
        if required_count <= 0:
            continue
        candidates = set(resolved.get(sid) or [])
        if not candidates:
            out.append((HOLD_UNROUTABLE_SIGNOFF,
                        f"signoff {sid!r} requires {required_count} but no candidate "
                        "agents resolve from its refsets (override to escape)"))
            continue
        countable = set(s.get("countable_statuses") or [ACCEPT])
        allow_na = bool(s.get("allow_na"))
        override_counts = bool(s.get("override_counts"))
        signers = _signoff_signers(
            record, s.get("generated_lens_ids") or [], acks, revision, candidates,
            active, countable, allow_na, override_counts)
        if len(signers) < required_count:
            out.append((HOLD_MISSING_SIGNOFF,
                        f"signoff {sid!r} needs {required_count} distinct qualifying "
                        f"acks, has {len(signers)}"))
    return out


def _signoff_signers(record, lens_ids, acks, revision, candidates, active,
                     countable, allow_na, override_counts) -> set[str]:
    """The set of DISTINCT agents who satisfy a signoff set: a qualifying
    (candidate AND active) agent with a non-stale countable ack on any of the set's
    generated lenses. na counts only with allow_na + a reason; an undecided counter
    never counts; an --override ack counts only if override_counts."""
    signers: set[str] = set()
    counters = record.get("counters", {})
    for lid in lens_ids:
        ack = acks.get(lid)
        if not isinstance(ack, dict):
            continue
        agent = ack.get("from")
        if agent not in candidates or agent not in active:
            continue
        if ack.get("revision") != revision:        # stale ack (reviewed other code)
            continue
        status = ack.get("status")
        if ack.get("override"):
            if override_counts and status in countable:
                signers.add(agent)
            continue
        if status == NA:
            if allow_na and ack.get("reason"):
                signers.add(agent)
            continue
        if status == COUNTER:
            cid = ack.get("counter_id")
            counter = counters.get(cid) if isinstance(cid, str) else None
            decided = isinstance(counter, dict) and counter.get("decision") != COUNTER_PENDING
            if COUNTER in countable and decided:
                signers.add(agent)
            continue
        if status in countable:                     # accept (default)
            signers.add(agent)
    return signers


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
    """PURE: an ack authorizes a lens via the SAME refset vocabulary as the roster
    and domains - agent / role / group. Group membership is resolved at ack time by
    the CLI and stored on the ack as ``from_groups`` (mirroring ``from_role``), so
    this stays pure (no roster read)."""
    if ack.get("override"):           # a recorded lead/operator override
        return True
    agent = ack.get("from")
    if agent in (lens.get("allowed_agents") or []):
        return True
    if ack.get("from_role") in (lens.get("allowed_roles") or []):
        return True
    allowed_groups = set(lens.get("allowed_groups") or [])
    return bool(allowed_groups & set(ack.get("from_groups") or []))


def _is_wellformed(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("schema_version") != SCHEMA_VERSION:
        return False
    generation = record.get("generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        return False
    instance_id = record.get("instance_id")
    if instance_id is not None and (
        not isinstance(instance_id, str) or not _INSTANCE_ID_RE.match(instance_id)
    ):
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
    for ack in record["lens_acks"].values():
        if not _is_wellformed_ack(ack):
            return False
    return True


def _is_wellformed_ack(ack: object) -> bool:
    if not isinstance(ack, dict):
        return False
    status = ack.get("status")
    if status not in ACK_STATUSES:
        return False
    if status == NA:
        reason = ack.get("reason")
        return isinstance(reason, str) and bool(reason.strip())
    if status == COUNTER:
        counter_id = ack.get("counter_id")
        return isinstance(counter_id, str) and bool(counter_id)
    evidence = ack.get("evidence")
    if not isinstance(evidence, dict):
        return False
    try:
        validate_review_result_evidence(
            "review-result", {**evidence, "status": "approved"}
        )
    except ValueError:
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
    spec = {
        "id": lid,
        "allowed_agents": _str_list(raw.get("allowed_agents")),
        "allowed_roles": _str_list(raw.get("allowed_roles")),
        "allowed_groups": _str_list(raw.get("allowed_groups")),
        "required": bool(raw.get("required", True)),
    }
    # P3: generated signoff lenses carry their set id + refset for audit/routing.
    if raw.get("signoff_set_id"):
        spec["signoff_set_id"] = str(raw["signoff_set_id"])
    return spec


def _str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CloseError("allowed_agents/allowed_roles/allowed_groups must be lists of strings")
    return list(value)


# ---------------------------------------------------- P3 signoff policy + derive

def validate_risk_class(value: object) -> str:
    """Core VALIDATES a risk_class string (envelope OR a namespaced extension); it
    never DECIDES risk. Raises CloseError on a bad string."""
    if not isinstance(value, str) or not value:
        raise CloseError("risk_class must be a non-empty string")
    if is_valid_risk_class(value):
        return value
    raise CloseError(
        f"risk_class {value!r} is not in the core envelope {sorted(CORE_RISK_CLASSES)} "
        "or a namespaced extension like project:name")


def _refset(raw: object) -> dict:
    """Normalize a candidate refset using the SAME vocabulary as roster/domains."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise CloseError("a candidate refset must be an object {agents, groups, roles}")
    return {"agents": _str_list(raw.get("agents")),
            "groups": _str_list(raw.get("groups")),
            "roles": _str_list(raw.get("roles"))}


def _json_bool(raw: dict, key: str, *, context: str, default: bool = False) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise CloseError(f"{context} {key!r} must be a JSON boolean")
    return value


def validate_signoff_policy(raw: object) -> dict:
    """Validate + normalize ``.agenttalk/signoffs.json``. A MISSING file is a valid
    EMPTY policy (handled by the loader); here ``raw`` is the parsed object. Raises
    CloseError (CLI -> invalid_signoff_policy) on a malformed policy."""
    if not isinstance(raw, dict):
        raise CloseError("signoff policy must be a JSON object")
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise CloseError("signoff policy 'defaults' must be an object")
    default_reviewers = _refset((defaults or {}).get("reviewers"))
    risk_policies_raw = raw.get("risk_policies") or {}
    if not isinstance(risk_policies_raw, dict):
        raise CloseError("signoff policy 'risk_policies' must be an object")
    risk_policies: dict[str, list[dict]] = {}
    seen_ids: set[str] = set()
    for risk_class, sets in risk_policies_raw.items():
        validate_risk_class(risk_class)
        if not isinstance(sets, list):
            raise CloseError(f"risk_policies[{risk_class!r}] must be a list of signoff sets")
        norm_sets = []
        for s in sets:
            if not isinstance(s, dict):
                raise CloseError(f"each signoff set under {risk_class!r} must be an object")
            sid = s.get("id")
            if not isinstance(sid, str) or not sid:
                raise CloseError(f"a signoff set under {risk_class!r} needs a non-empty id")
            count = s.get("required_count", 1)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise CloseError(f"signoff set {sid!r} required_count must be a non-negative int")
            statuses = _str_list(s.get("countable_statuses")) or [ACCEPT]
            for st in statuses:
                if st not in ACK_STATUSES:
                    raise CloseError(
                        f"signoff set {sid!r} countable_statuses has invalid {st!r}")
            rsid = f"{risk_class}:{sid}"
            if rsid in seen_ids:
                raise CloseError(f"duplicate signoff set {rsid!r}")
            seen_ids.add(rsid)
            norm_sets.append({
                "id": sid,
                "required_count": count,
                "candidates": _refset(s.get("candidates")),
                "use_default_reviewers": _json_bool(
                    s, "use_default_reviewers", context=f"signoff set {sid!r}"),
                "include_domain_reviewers": _json_bool(
                    s, "include_domain_reviewers", context=f"signoff set {sid!r}"),
                "allow_na": _json_bool(s, "allow_na", context=f"signoff set {sid!r}"),
                "countable_statuses": statuses,
                "override_counts": _json_bool(
                    s, "override_counts", context=f"signoff set {sid!r}"),
            })
        risk_policies[risk_class] = norm_sets
    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise CloseError("signoff policy schema_version must be an integer")
    return {
        "schema_version": schema_version,
        "defaults": {"reviewers": default_reviewers},
        "risk_policies": risk_policies,
        "allow_unmapped": _json_bool(
            raw, "allow_unmapped", context="signoff policy"),
    }


def _stable_hash(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
        .encode("utf-8")).hexdigest()


def policy_hash(policy: dict) -> str:
    return _stable_hash(policy)


def risk_inventory_hash(inventory: list) -> str:
    # order-independent: hash the sorted normalized entries
    norm = sorted(
        (str(e.get("risk_class")), str(e.get("na_reason") or ""),
         tuple(sorted(e.get("affected_paths") or [])))
        for e in inventory if isinstance(e, dict))
    return _stable_hash(norm)


def _signoff_set_id(risk_class: str, set_id: str) -> str:
    return f"{risk_class}:{set_id}"


def generated_lens_ids(risk_class: str, set_id: str, count: int) -> list[str]:
    base = f"so:{_signoff_set_id(risk_class, set_id)}"
    return [f"{base}#{k}" for k in range(1, max(count, 1) + 1)]


def derive_required_signoffs(policy: dict, risk_inventory: list) -> dict:
    """PURE: map a validated ``policy`` + a close's ``risk_inventory`` into derived
    required-signoff set records (refsets + generated lens ids), plus the list of
    declared non-none risks that have NO policy mapping. A risk entry carrying an
    ``na_reason`` is dispositioned N/A and requires no signoff. Returns
    ``{"signoffs": [...], "unmapped": [risk_class, ...]}``. Candidate REFSETS are
    stored, not concrete agents - the CLI resolves them at check time."""
    out: list[dict] = []
    unmapped: list[str] = []
    allow_unmapped = bool(policy.get("allow_unmapped"))
    risk_policies = policy.get("risk_policies") or {}
    seen = set()
    for entry in risk_inventory or []:
        if not isinstance(entry, dict):
            continue
        rc = entry.get("risk_class")
        if rc == "none" or not rc:
            continue
        if entry.get("na_reason"):                  # explicitly dispositioned N/A
            continue
        sets = risk_policies.get(rc)
        if not sets:
            if not allow_unmapped and rc not in unmapped:
                unmapped.append(rc)
            continue
        for s in sets:
            rsid = _signoff_set_id(rc, s["id"])
            if rsid in seen:
                continue
            seen.add(rsid)
            out.append({
                "id": rsid,
                "risk_class": rc,
                "set_id": s["id"],
                "required_count": s["required_count"],
                "candidate_refset": s["candidates"],
                "use_default_reviewers": s["use_default_reviewers"],
                "include_domain_reviewers": s["include_domain_reviewers"],
                "allow_na": s["allow_na"],
                "countable_statuses": s["countable_statuses"],
                "override_counts": s["override_counts"],
                "generated_lens_ids": generated_lens_ids(rc, s["id"], s["required_count"]),
            })
    return {"signoffs": out, "unmapped": unmapped}


# --------------------------------------------------------------- persistence

def signoffs_policy_path(store):
    return store.dir / SIGNOFF_DIRNAME


def load_signoff_policy(store) -> tuple[dict | None, str | None]:
    """Load + validate ``.agenttalk/signoffs.json``. Returns (policy, error):
    a MISSING file is a valid EMPTY policy -> (None, None) meaning "no policy,
    zero derived signoffs"; a present-but-malformed/unparseable file fails CLOSED
    -> (None, error) which the CLI surfaces as invalid_signoff_policy."""
    path = signoffs_policy_path(store)
    if not path.exists():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as e:
        return None, f"signoffs.json is unreadable/corrupt: {e}"
    try:
        return validate_signoff_policy(raw), None
    except CloseError as e:
        return None, str(e)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        # defense-in-depth: ANY malformed policy must fail closed to
        # invalid_signoff_policy, never crash `close check` (fail-closed contract).
        return None, f"malformed signoff policy: {type(e).__name__}: {e}"


# --------------------------------------------- #60 DoD forcing gate (red-by-default)

def dod_policy_path(store):
    return store.dir / DOD_DIRNAME


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """``object_pairs_hook`` that fails CLOSED on a duplicated key at ANY object level. Python's
    default decoder silently keeps the LAST value of a duplicated key, so a malformed policy like
    ``{"scopes":{...},"scopes":{}}`` (erases all requirements) or a duplicated ``max_age_days``
    (disables freshness) would decode to a valid-looking dict and produce a false GO *before*
    validate_dod_policy ever runs. Rejecting duplicates at decode time closes that class."""
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in dod.json (ambiguous policy; fail closed)")
        seen[key] = value
    return seen


def load_dod_policy(store) -> tuple[dict | None, str | None]:
    """Load + validate ``.agenttalk/dod.json``. Returns (policy, error): a MISSING file is a
    valid EMPTY policy -> (None, None) meaning "no DoD, zero required dimensions" (close behaves
    byte-identically to before #60); a present-but-malformed/unparseable file fails CLOSED ->
    (None, error), which the CLI surfaces as ``HOLD_INVALID_DOD_POLICY`` - never a crash."""
    path = dod_policy_path(store)
    if not path.exists():
        return None, None
    try:
        size = path.stat().st_size
        if size > _DOD_MAX_BYTES:
            # bound the file BEFORE decode so a pathological document cannot exhaust the parser.
            return None, f"dod.json exceeds max size ({size} > {_DOD_MAX_BYTES} bytes)"
        # object_pairs_hook rejects duplicate keys at every level (a duplicated key would otherwise
        # silently keep its last value and erase a requirement / disable freshness -> false GO).
        raw = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
            # ``min_percent`` is the only fractional DoD policy operand. Decode every
            # JSON fraction exactly so validation can keep its floor from rounding down
            # before a pass/fail comparison. Integer-only fields reject Decimal values.
            parse_float=Decimal,
        )
    except (ValueError, OSError, RecursionError) as e:
        # RecursionError: deeply-nested JSON. Must fail CLOSED, never propagate out of close check.
        return None, f"dod.json is unreadable/corrupt: {type(e).__name__}: {e}"
    try:
        return validate_dod_policy(raw), None
    except CloseError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - ANY malformed policy fails CLOSED, never crashes close check
        return None, f"malformed dod policy: {type(e).__name__}: {e}"


def validate_dod_policy(raw: object) -> dict:
    """Validate + normalize ``.agenttalk/dod.json``. Raises CloseError (CLI ->
    HOLD_INVALID_DOD_POLICY) on any malformed policy. A dimension the engine cannot enforce
    (not in ``_DOD_SUPPORTED_DIMENSIONS``) is REJECTED: you must not be able to declare a
    requirement that would silently soft-pass - that is the exact failure #60 prevents."""
    if not isinstance(raw, dict):
        raise CloseError("dod policy must be a JSON object")
    unknown_top = set(raw) - set(_DOD_ALLOWED_TOP_KEYS)
    if unknown_top:
        # unknown keys fail CLOSED - a typo like "scope"/"scopez" must never be silently ignored.
        raise CloseError(f"dod policy has unknown top-level key(s): {sorted(unknown_top)}")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise CloseError("dod policy schema_version must be an integer")
    if schema_version != _DOD_SCHEMA_VERSION:
        # reject unknown/future versions rather than best-effort interpreting them.
        raise CloseError(
            f"dod policy schema_version {schema_version} is unsupported "
            f"(this build enforces {_DOD_SCHEMA_VERSION})")
    # distinguish ABSENT (valid empty policy) from PRESENT-BUT-WRONG-TYPE (fail closed): `or {}`
    # would collapse a present [] / "" / 0 to an empty policy and silently drop all requirements.
    if "scopes" in raw:
        scopes_raw = raw["scopes"]
        if not isinstance(scopes_raw, dict):
            raise CloseError("dod policy 'scopes' must be an object")
    else:
        scopes_raw = {}
    scopes: dict[str, dict] = {}
    for scope_name, dims in scopes_raw.items():
        if not isinstance(dims, dict):
            raise CloseError(f"dod scope {scope_name!r} must map to an object of dimensions")
        norm: dict[str, dict] = {}
        for dim, spec in dims.items():
            if dim not in _DOD_SUPPORTED_DIMENSIONS:
                raise CloseError(
                    f"dod dimension {dim!r} is not supported in this version "
                    f"(supported: {sorted(_DOD_SUPPORTED_DIMENSIONS)})")
            if dim == "assurance":
                norm[dim] = _validate_dod_assurance_spec(spec, scope_name)
            elif dim == "coverage":
                norm[dim] = _validate_dod_coverage_spec(spec, scope_name)
            elif dim == "knowledge":
                norm[dim] = _validate_dod_knowledge_spec(spec, scope_name)
        key = str(scope_name).lower()
        if key in scopes:
            # two scope names that collide after lowercasing are ambiguous -> fail closed.
            raise CloseError(f"dod policy has case-insensitively colliding scope name {key!r}")
        scopes[key] = norm
    return {"schema_version": schema_version, "scopes": scopes}


def _validate_dod_assurance_spec(spec: object, scope_name: str) -> dict:
    if not isinstance(spec, dict):
        raise CloseError(f"dod scope {scope_name!r} assurance must be an object")
    unknown = set(spec) - set(_DOD_ASSURANCE_KEYS)
    if unknown:
        # e.g. a "max_age_day" typo must RAISE, not silently drop the freshness requirement.
        raise CloseError(
            f"dod scope {scope_name!r} assurance has unknown key(s): {sorted(unknown)}")
    gate = spec.get("gate")
    if not isinstance(gate, str) or not gate.strip():
        raise CloseError(f"dod scope {scope_name!r} assurance.gate must be a non-empty string")
    max_age = spec.get("max_age_days")
    if max_age is not None and (
        not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0
    ):
        raise CloseError(
            f"dod scope {scope_name!r} assurance.max_age_days must be a non-negative integer")
    return {"gate": gate, "max_age_days": max_age}


def _is_finite_number(value: object) -> bool:
    """True for finite builtin ints/floats without coercing arbitrarily large ints to float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isinstance(value, int) or math.isfinite(value)


def _is_percent(value: object) -> bool:
    return _is_finite_number(value) and 0 <= value <= 100


def _normalize_coverage_floor(value: object) -> int | float | None:
    """Return a JSON-compatible floor that never understates an exact JSON decimal.

    Direct callers retain the existing builtin int/float contract. ``load_dod_policy``
    supplies ``Decimal`` for JSON fractions; nearest-float conversion is accepted only
    when its canonical decimal is at least the configured value, otherwise the result is
    stepped upward. The conservative adjustment can make an unrepresentable floor
    marginally stricter, but it cannot move the policy toward passing.
    """
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0 or value > 100:
            return None
        normalized = float(value)
        if Decimal(str(normalized)) < value:
            normalized = math.nextafter(normalized, math.inf)
            if Decimal(str(normalized)) < value:
                return None
        return normalized if _is_percent(normalized) else None
    return value if _is_percent(value) else None


def _validate_dod_coverage_spec(spec: object, scope_name: str) -> dict:
    """Normalize a scope's coverage producer and floor without accepting impossible gates."""
    if not isinstance(spec, dict):
        raise CloseError(f"dod scope {scope_name!r} coverage must be an object")
    unknown = set(spec) - set(_DOD_COVERAGE_KEYS)
    if unknown:
        raise CloseError(
            f"dod scope {scope_name!r} coverage has unknown key(s): {sorted(unknown)}")
    gate = spec.get("gate")
    if not isinstance(gate, str) or gate not in COVERAGE_GATE_NAMES:
        raise CloseError(
            f"dod scope {scope_name!r} coverage.gate must be one of "
            f"{sorted(COVERAGE_GATE_NAMES)}"
        )
    min_percent = _normalize_coverage_floor(spec.get("min_percent"))
    if min_percent is None:
        raise CloseError(
            f"dod scope {scope_name!r} coverage.min_percent must be a finite number from 0 to 100")
    max_age = spec.get("max_age_days")
    if max_age is not None and (
        not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0
    ):
        raise CloseError(
            f"dod scope {scope_name!r} coverage.max_age_days must be a non-negative integer")
    return {"gate": gate, "min_percent": min_percent, "max_age_days": max_age}


def _validate_dod_knowledge_spec(spec: object, scope_name: str) -> dict:
    """Normalize a scope's ``knowledge`` requirement. ``when``: on_remediation (default; required
    only when the close carries an ACCEPTED COUNTER / remediation item - see the honesty note in
    :func:`_evaluate_dod_knowledge`, NOT "any fix") | always. ``min_notes``: how many CURATED,
    non-trivial, revision-bound notes clear it (default 1). ``types``: allowed note types, a
    subset of the lesson/gotcha/decision trio (default the whole trio). ``min_body_chars``:
    triviality floor - knowledge.validate_body only forbids empty, so a stub could otherwise
    clear the gate (default 40). Rejects UNKNOWN keys fail-closed (a ``min_note`` typo must RAISE,
    not silently apply the ``min_notes`` default and let one note through)."""
    if not isinstance(spec, dict):
        raise CloseError(f"dod scope {scope_name!r} knowledge must be an object")
    unknown = set(spec) - set(_DOD_KNOWLEDGE_KEYS)
    if unknown:
        # e.g. a "min_note"/"min_body_char" typo must RAISE, not silently drop the requirement.
        raise CloseError(
            f"dod scope {scope_name!r} knowledge has unknown key(s): {sorted(unknown)}")
    when = spec.get("when", "on_remediation")
    if when not in ("on_remediation", "always"):
        raise CloseError(
            f"dod scope {scope_name!r} knowledge.when must be 'on_remediation' or 'always'")
    min_notes = spec.get("min_notes", 1)
    if not isinstance(min_notes, int) or isinstance(min_notes, bool) or min_notes < 1:
        raise CloseError(
            f"dod scope {scope_name!r} knowledge.min_notes must be an integer >= 1")
    types = spec.get("types", ["lesson", "gotcha", "decision"])
    if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
        raise CloseError(
            f"dod scope {scope_name!r} knowledge.types must be a non-empty list of strings")
    bad = [t for t in types if t not in _DOD_KNOWLEDGE_ALLOWED_TYPES]
    if bad:
        raise CloseError(
            f"dod scope {scope_name!r} knowledge.types has disallowed type(s) {bad} - the "
            f"knowledge dimension only counts {sorted(_DOD_KNOWLEDGE_ALLOWED_TYPES)} "
            f"(seam/pointer are structural index notes, not captured-learning evidence)")
    min_body_chars = spec.get("min_body_chars", 40)
    if not isinstance(min_body_chars, int) or isinstance(min_body_chars, bool) or min_body_chars < 0:
        raise CloseError(
            f"dod scope {scope_name!r} knowledge.min_body_chars must be a non-negative integer")
    return {"when": when, "min_notes": min_notes,
            "types": sorted(set(types)), "min_body_chars": min_body_chars}


def dod_policy_hash(policy: dict) -> str:
    return _stable_hash(policy)


def derive_required_dod(policy: dict | None, scope: str) -> dict:
    """PURE: the DoD dimensions required for this close's ``scope``. ``{"dimensions": {}}`` when
    there is no policy or the scope is unmapped (unmapped ⇒ zero requirements ⇒ safe default,
    exactly like an absent policy). Live-derived at check time (no frozen apply/route), so the
    gate can never be silently skipped by forgetting an apply step."""
    if not policy:
        return {"dimensions": {}}
    scopes = policy.get("scopes") or {}
    return {"dimensions": dict(scopes.get(str(scope or "").lower()) or {})}


def evaluate_dod(record: dict, dod_eval: dict | None) -> list[tuple[str, str]]:
    """PURE: given a close ``record`` and a CLI-resolved ``dod_eval`` bundle, return (code,
    detail) DoD holds. All I/O (load the policy, read the assurance gate) happens in the CLI
    and is passed in already resolved - this only COUNTS, mirroring :func:`_evaluate_signoffs`.

    dod_eval = {
      "policy_present": bool,
      "policy_error": str | None,               # malformed dod.json -> HOLD_INVALID_DOD_POLICY
      "required_dimensions": {dim: spec, ...},  # derived for THIS close's scope
      "assurance": {                            # present iff "assurance" in required_dimensions
        "gate": str, "present": bool, "status": str|None, "severity": str|None,
        "evidence_source": str|None, "revision": str|None, "waiver_active": bool,
        "gate_scope": str|None,                 # the gate's OWN scope (None = global)
        "close_gate_scope": str|None,           # the scope this close's gate check applies to
        "age_days": float|None,                 # SIGNED age (negative = future); None if unparseable
        "max_age_days": int|None,
      } | None,
      "coverage": {                             # present iff "coverage" in required_dimensions
        "gate": str, "present": bool, "status": str|None, "severity": str|None,
        "evidence_source": str|None, "revision": str|None, "waiver_active": bool,
        "gate_scope": str|None,                 # must match the selected producer profile/global
        "coverage_percent": float|None, "min_percent": float,
        "age_days": float|None, "max_age_days": int|None,
      } | None,
    }
    ``None`` ⇒ [] (the scope has no DoD requirements)."""
    if dod_eval is None:
        return []
    if not isinstance(dod_eval, dict):
        return [(HOLD_INVALID_DOD_POLICY, "dod evaluation bundle is malformed")]
    if dod_eval.get("policy_error"):
        return [(HOLD_INVALID_DOD_POLICY, str(dod_eval["policy_error"]))]
    required = dod_eval.get("required_dimensions") or {}
    if not required:
        return []
    out: list[tuple[str, str]] = []
    if "assurance" in required:
        out.extend(_evaluate_dod_assurance(record, dod_eval.get("assurance")))
    if "coverage" in required:
        out.extend(
            _evaluate_dod_coverage(
                record,
                dod_eval.get("coverage"),
                required.get("coverage"),
            )
        )
    if "knowledge" in required:
        out.extend(_evaluate_dod_knowledge(record, dod_eval.get("knowledge")))
    return out


def _evaluate_dod_assurance(record: dict, a: dict | None) -> list[tuple[str, str]]:
    """PURE: the assurance dimension binds to the EXISTING ``assurance:<scope>`` gate's own
    fields - it never reads artifact.json. Cleared iff the gate is present, applies to this
    close's scope, is green from a CI attestation as a blocker, bound to THIS close's revision,
    and - when max_age_days is set - fresh. A gate WAIVER does NOT clear this dimension (see the
    waiver note below); the authenticated operator escape is task #65.

    RESIDUAL RISK (not closed by this dimension): the assurance guarantee is only as strong as
    the blocker-gate substrate it reads. ``evidence_source == "automation_ci"`` is a LABEL, not
    yet cryptographically authenticated CI provenance - gates.py forbids greening a blocker from
    other sources, but the label itself is not signed, so an actor who can write gates.json can
    still assert it. Authenticated CI attestation is a separate substrate hardening (task #64,
    sibling to the gate-waive authentication hardening); until then this is the existing blocker-gate
    mechanism (label-trust), NOT a cryptographic sole-certifier guarantee."""
    gate = (a or {}).get("gate") or "assurance"
    if not isinstance(a, dict) or not a.get("present"):
        return [(HOLD_MISSING_ASSURANCE, f"required assurance gate {gate!r} is not present")]
    # M2: the gate's OWN scope must apply to this close, mirroring gates.check_gates EXACTLY - a
    # gate applies to a scoped check iff its scope is that scope or "global". A gate scoped to
    # "feature" (or scope-less) therefore does NOT satisfy a "release" close. Only when the close
    # itself imposes no scope (close_scope falsy) is any gate applicable (gates.py skips the filter).
    gate_scope = a.get("gate_scope")
    close_scope = a.get("close_gate_scope")
    if close_scope and gate_scope not in (close_scope, "global"):
        return [(HOLD_MISSING_ASSURANCE,
                 f"assurance gate {gate!r} is recorded under scope {gate_scope!r}, not applicable "
                 f"to this close's scope {close_scope!r} (needs that scope or 'global')")]
    revision_bound = a.get("revision") == record.get("revision")
    status = a.get("status")
    # A gate WAIVER does NOT clear the DoD assurance dimension. `gate waive --operator <text>` is
    # UNAUTHENTICATED caller free text (docs/ASSURANCE.md) with no authenticated operator, so
    # honoring it would turn a single `gate waive` into a revision-independent bypass of the whole
    # forcing gate (Codex re-review of 848841a, BLOCKER). Only a green CI-attested, revision-bound
    # blocker gate clears assurance. The AUTHENTICATED operator escape (close dod-waive validating
    # a typed operator-answer reference) is task #65 (depends on authenticated gate-waive origin).
    if status != "green":
        detail = f"assurance gate {gate!r} is not green (status={status!r})"
        if status == "waived":
            detail = (f"assurance gate {gate!r} is waived, but an unauthenticated gate waiver does "
                      f"not clear a DoD assurance requirement - needs a CI-attested green gate (#65)")
        return [(HOLD_MISSING_ASSURANCE, detail)]
    # A green blocker greens ONLY from automation_ci (gates.py enforces this at write time; we
    # re-assert it - see the RESIDUAL RISK note above on why this is label-trust, not a proof).
    if a.get("severity") != "blocker" or a.get("evidence_source") != "automation_ci":
        return [(HOLD_UNATTESTED_ASSURANCE,
                 f"assurance gate {gate!r} is green but not a CI-attested blocker "
                 f"(severity={a.get('severity')!r}, source={a.get('evidence_source')!r})")]
    out: list[tuple[str, str]] = []
    if not revision_bound:
        out.append((HOLD_STALE_ASSURANCE,
                    f"assurance gate {gate!r} is attested for a different revision than the "
                    f"close ({a.get('revision')!r} != {record.get('revision')!r})"))
    # B3: when freshness is REQUIRED (max_age_days set) it must FAIL CLOSED if it cannot be
    # validated: a missing/unparseable timestamp (age is None) and a materially future-dated one
    # are both HOLDs, not passes. The CLI provides a SIGNED age (negative = future, not clamped).
    max_age = a.get("max_age_days")
    if isinstance(max_age, int) and not isinstance(max_age, bool):
        age = a.get("age_days")
        if age is None:
            out.append((HOLD_STALE_ASSURANCE,
                        f"assurance gate {gate!r} freshness is required (max_age_days={max_age}) "
                        f"but its timestamp is missing or unparseable"))
        elif isinstance(age, (int, float)) and not isinstance(age, bool):
            if age < -_DOD_CLOCK_SKEW_DAYS:
                out.append((HOLD_STALE_ASSURANCE,
                            f"assurance gate {gate!r} is future-dated (age_days={age:.4f}); "
                            f"refusing to treat a future attestation as fresh"))
            elif age > max_age:
                out.append((HOLD_STALE_ASSURANCE,
                            f"assurance gate {gate!r} attestation is older than "
                            f"max_age_days={max_age}"))
        else:
            out.append((HOLD_STALE_ASSURANCE,
                        f"assurance gate {gate!r} freshness is required but its age is invalid"))
    return out


def _evaluate_dod_coverage(
    record: dict,
    cov: dict | None,
    required: dict | None,
) -> list[tuple[str, str]]:
    """PURE: enforce the policy-selected, CI-attested, revision-bound coverage gate.

    Close scope and assurance scan profile are separate axes: policy explicitly selects a
    producible ``coverage:<profile>`` gate, whose own scope must match that producer profile (or
    be global). The gate is otherwise unusable unless it is present, green, blocker severity,
    produced by automation_ci, and bound to this close's revision. A waiver never clears the
    dimension. Freshness is checked before the numeric floor so stale evidence cannot be reported
    as merely low. Missing or malformed percentages fail closed as missing evidence.
    """
    gate = required.get("gate") if isinstance(required, dict) else None
    if not isinstance(gate, str):
        return [(
            HOLD_MISSING_COVERAGE,
            "required coverage policy gate identity is missing or malformed",
        )]
    try:
        producer_scope = coverage_profile_from_gate(gate)
    except ValueError:
        return [(
            HOLD_MISSING_COVERAGE,
            f"required coverage gate {gate!r} has no supported assurance producer",
        )]
    if not isinstance(cov, dict):
        return [(HOLD_MISSING_COVERAGE, f"required coverage gate {gate!r} is not present")]
    if cov.get("gate") != gate:
        return [(
            HOLD_MISSING_COVERAGE,
            f"resolved coverage gate {cov.get('gate')!r} does not match required gate {gate!r}",
        )]
    if cov.get("present") is not True:
        return [(HOLD_MISSING_COVERAGE, f"required coverage gate {gate!r} is not present")]

    gate_scope = cov.get("gate_scope")
    if gate_scope not in (producer_scope, "global"):
        return [(
            HOLD_MISSING_COVERAGE,
            f"coverage gate {gate!r} is recorded under scope {gate_scope!r}, not applicable "
            f"to the selected producer profile {producer_scope!r} "
            f"(needs that profile or 'global')",
        )]

    status = cov.get("status")
    if status != "green":
        detail = f"coverage gate {gate!r} is not green (status={status!r})"
        if status == "waived":
            detail = (
                f"coverage gate {gate!r} is waived, but a gate waiver does not clear a DoD "
                "coverage requirement - needs a CI-attested green gate"
            )
        return [(HOLD_MISSING_COVERAGE, detail)]

    if cov.get("severity") != "blocker" or cov.get("evidence_source") != "automation_ci":
        return [(
            HOLD_MISSING_COVERAGE,
            f"coverage gate {gate!r} is green but not a CI-attested blocker "
            f"(severity={cov.get('severity')!r}, source={cov.get('evidence_source')!r})",
        )]

    if cov.get("revision") != record.get("revision"):
        return [(
            HOLD_MISSING_COVERAGE,
            f"coverage gate {gate!r} is not bound to the close revision "
            f"({cov.get('revision')!r} != {record.get('revision')!r})",
        )]

    max_age = required.get("max_age_days")
    if max_age is not None:
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
            return [(
                HOLD_STALE_COVERAGE,
                f"coverage gate {gate!r} has an invalid max_age_days requirement",
            )]
        age = cov.get("age_days")
        if not _is_finite_number(age):
            return [(
                HOLD_STALE_COVERAGE,
                f"coverage gate {gate!r} freshness is required (max_age_days={max_age}) "
                "but its timestamp is missing or unparseable",
            )]
        if age < -_DOD_CLOCK_SKEW_DAYS:
            return [(
                HOLD_STALE_COVERAGE,
                f"coverage gate {gate!r} is future-dated (age_days={age:.4f}); "
                "refusing to treat future evidence as fresh",
            )]
        if age > max_age:
            return [(
                HOLD_STALE_COVERAGE,
                f"coverage gate {gate!r} evidence is older than max_age_days={max_age}",
            )]

    min_percent = required.get("min_percent")
    coverage_percent = cov.get("coverage_percent")
    if not _is_percent(min_percent) or not _is_percent(coverage_percent):
        return [(
            HOLD_MISSING_COVERAGE,
            f"coverage gate {gate!r} does not carry a valid coverage_percent and floor",
        )]
    if coverage_percent < min_percent:
        return [(
            HOLD_LOW_COVERAGE,
            f"coverage gate {gate!r} reports {coverage_percent!r}% below the "
            f"required {min_percent!r}%",
        )]
    return []


def _evaluate_dod_knowledge(record: dict, k: dict | None) -> list[tuple[str, str]]:
    """PURE: the knowledge dimension flips "write down what you learned" from opt-in to
    red-by-default (Papendal shipped a multi-day build with ZERO notes). HOLDs unless the close
    cites >= min_notes CURATED, non-trivial knowledge notes of an allowed type that are BOUND to
    the close. BINDING: a note is bound iff its ``sha`` anchor (or ``verified_against_sha``) equals
    the close ``revision`` - the close record has no request_id, so a sha match to the closed
    commit is the only clean, non-circular link (the CLI resolves the curated/typed/bound set;
    this only COUNTS). Distinguishes 'nothing captured' (HOLD_MISSING_KNOWLEDGE) from 'only stubs
    captured' (HOLD_TRIVIAL_EVIDENCE).

    ``when`` HONESTY (do not overclaim): ``when=always`` ALWAYS requires notes. ``when=on_remediation``
    (default) requires them only when ``has_remediation`` is set, and ``has_remediation`` is
    precisely ``bool(record["remediation_items"])`` - i.e. the close carries an ACCEPTED COUNTER /
    remediation item. It is NOT a general "a fix was produced" signal: a hotfix that lands without
    an accepted counter has no remediation_items and so is NOT triggered. There is no authoritative
    durable "fix produced" fact to bind to yet (that would be a separate task). So a scope that must
    capture knowledge on EVERY close (not only counter-bearing ones) MUST use ``when=always``;
    ``on_remediation`` is the narrower "when we formally accepted a counter" trigger. #64/#65-adjacent.

    #66 KNOWN RACE (documented, tracked): the CLI resolves ``bound_notes`` (an I/O read of the
    knowledge log) and then persists the close verdict in a separate step; a ``knowledge retract``
    landing in that window can leave a persisted GO citing a now-retracted note. The publish path
    resolves evidence immediately before the write to keep the exposure narrow (not a guaranteed
    closure - the write is not atomic with the evidence read across files); full closure + a
    stale-GO detector ride the #31 close-provenance envelope. This is a narrow known race tracked
    by #66/#31. This evaluator is pure and correct for the inputs it is given."""
    if not isinstance(k, dict):
        return [(HOLD_MISSING_KNOWLEDGE,
                 "knowledge evidence is required for this scope but was not resolved")]
    when = k.get("when", "on_remediation")
    required = when == "always" or (when == "on_remediation" and bool(k.get("has_remediation")))
    if not required:
        return []
    min_notes = k.get("min_notes", 1)
    min_body_chars = k.get("min_body_chars", 0)
    types = k.get("types") or []
    bound = k.get("bound_notes") or []
    qualifying = [n for n in bound
                  if isinstance(n, dict) and int(n.get("body_len") or 0) >= int(min_body_chars)]
    if len(qualifying) >= int(min_notes):
        return []
    if bound and not qualifying:
        return [(HOLD_TRIVIAL_EVIDENCE,
                 f"the {len(bound)} knowledge note(s) bound to this close revision are all below "
                 f"min_body_chars={min_body_chars} (stubs do not count as captured knowledge)")]
    return [(HOLD_MISSING_KNOWLEDGE,
             f"scope requires >= {min_notes} curated knowledge note(s) of {types} bound to the "
             f"close revision (by sha anchor / verified_against_sha); found {len(qualifying)}")]


def merge_candidate_refsets(*refsets: dict) -> dict:
    """Merge signoff candidate refsets without resolving roster membership."""
    merged = {"agents": [], "groups": [], "roles": []}
    for refset in refsets:
        for key in merged:
            merged[key].extend((refset or {}).get(key) or [])
    return merged


def signoff_domain_refset(
    store,
    cfg: dict,
    changed_paths: list[str] | None,
) -> dict:
    """Resolve matched domain reviewer refsets, or every possible one for a guard.

    ``changed_paths=None`` is the conservative roster-mutation mode: it returns
    every domain/shared-path reviewer that a future close could count. A concrete
    path list preserves close evaluation's path-matched behavior.
    """
    from agenttalk import domains as domain_mod

    empty = merge_candidate_refsets()
    if changed_paths == []:
        return empty
    try:
        registry = domain_mod.load_registry(store.dir / domain_mod.FILENAME, cfg)
    except Exception:  # noqa: BLE001 - preserve close evaluation's empty fallback
        return empty
    data = registry.data
    domains = data.get("domains", {})
    shared = data.get("shared_paths", [])
    if changed_paths is None:
        return merge_candidate_refsets(
            empty,
            *(entry.get("reviewers") or {} for entry in domains.values()),
            *(entry.get("default_reviewers") or {} for entry in shared),
        )
    verdicts = domain_mod.check_paths(data, changed_paths)
    matched_domains: set[str] = set()
    matched_globs: set[str] = set()
    for verdict in verdicts:
        matched_domains.update(verdict.get("domains", []))
        for match in verdict.get("shared_paths", []):
            matched_globs.add(match.get("glob"))
    return merge_candidate_refsets(
        empty,
        *(domains.get(domain_id, {}).get("reviewers") or {}
          for domain_id in matched_domains),
        *(entry.get("default_reviewers") or {}
          for entry in shared if entry.get("glob") in matched_globs),
    )


def resolve_signoff_candidates(
    cfg: dict,
    *,
    candidate_refset: dict,
    default_reviewers: dict,
    use_default_reviewers: bool,
    domain_refset: dict,
    include_domain_reviewers: bool,
) -> list[str]:
    """The single candidate merge+resolution contract used by close and roster."""
    from agenttalk import domains as domain_mod

    merged = merge_candidate_refsets(
        candidate_refset,
        default_reviewers if use_default_reviewers else {},
        domain_refset if include_domain_reviewers else {},
    )
    return domain_mod.resolve_refset(merged, cfg)


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
    data.setdefault("generation", 0)
    return data


def close_generation(record: dict) -> int:
    """Return a validated generation token, treating legacy records as generation 0."""
    generation = record.get("generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise CloseError("close generation must be a non-negative integer")
    return generation


def close_instance_id(record: dict) -> str | None:
    """Return the immutable instance id, or None for a legacy close record."""
    instance_id = record.get("instance_id")
    if instance_id is None:
        return None
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.match(instance_id):
        raise CloseError("close instance_id must be a 32-character lowercase hex id")
    return instance_id


def _close_update_lock(store, close_id: str, *, timeout: float):
    lock_path = closes_dir(store) / f".{validate_close_id(close_id)}.lock"
    return store._exclusive_lock(
        lock_path,
        timeout=timeout,
        what=f"close {close_id!r} update lock (another agent may be updating it)",
    )


def _write_close(path, record: dict) -> None:
    from agenttalk import _atomic

    _atomic.write_text(
        path,
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _validate_expected_tokens(expected_generation: int | None,
                              expected_instance_id: str | None) -> None:
    if expected_generation is None:
        raise CloseConflict("close update requires expected_generation")
    if (not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0):
        raise CloseError("expected close generation must be a non-negative integer")
    if expected_instance_id is None:
        raise CloseConflict("close update requires expected_instance_id")
    if (not isinstance(expected_instance_id, str)
            or not _INSTANCE_ID_RE.match(expected_instance_id)):
        raise CloseError("expected close instance_id must be a 32-character lowercase hex id")


def _require_current_tokens(current: dict, *, close_id: str,
                            expected_generation: int,
                            expected_instance_id: str) -> None:
    actual_generation = close_generation(current)
    actual_instance_id = close_instance_id(current)
    if actual_instance_id is None:
        raise CloseConflict(
            f"close {close_id!r} is a legacy record; upgrade it while locked first")
    if actual_instance_id != expected_instance_id:
        raise CloseConflict(
            f"close {close_id!r} instance changed from {expected_instance_id!r} "
            f"to {actual_instance_id!r}; reload before retrying")
    if actual_generation != expected_generation:
        raise CloseConflict(
            f"close {close_id!r} changed from generation {expected_generation} "
            f"to {actual_generation}; reload before retrying")


def _upgrade_legacy_locked(store, close_id: str, current: dict) -> dict:
    if close_instance_id(current) is not None:
        raise CloseConflict(f"close {close_id!r} is already versioned; reload it")
    upgraded = dict(current)
    upgraded["instance_id"] = uuid.uuid4().hex
    upgraded["generation"] = close_generation(current) + 1
    if not _is_wellformed(upgraded):
        raise CloseError(f"close {close_id!r} is malformed")
    _write_close(close_path(store, close_id), upgraded)
    return upgraded


class CloseTransaction:
    """A latest-record close mutation while the per-close lock is held."""

    def __init__(self, store, close_id: str, record: dict) -> None:
        self.store = store
        self.close_id = close_id
        self.record = record
        self._generation = close_generation(record)
        instance_id = close_instance_id(record)
        if instance_id is None:
            raise CloseConflict(f"close {close_id!r} is not versioned")
        self._instance_id = instance_id
        self._active = True

    def commit(self) -> int:
        """Persist the transaction record after rechecking its locked tokens."""
        if not self._active:
            raise CloseConflict("close transaction is no longer active")
        if validate_close_id(self.record.get("close_id")) != self.close_id:
            raise CloseConflict("close transaction close_id was modified")
        generation = close_generation(self.record)
        instance_id = close_instance_id(self.record)
        if generation != self._generation or instance_id != self._instance_id:
            raise CloseConflict(
                f"close {self.close_id!r} transaction tokens were modified")
        current = load_close(self.store, self.close_id)
        _require_current_tokens(
            current, close_id=self.close_id,
            expected_generation=self._generation,
            expected_instance_id=self._instance_id,
        )
        persisted = dict(self.record)
        next_generation = self._generation + 1
        persisted["generation"] = next_generation
        if not _is_wellformed(persisted):
            raise CloseError(f"close {self.close_id!r} is malformed")
        _write_close(close_path(self.store, self.close_id), persisted)
        self.record["generation"] = next_generation
        self._generation = next_generation
        return next_generation

    def _finish(self) -> None:
        self._active = False


@contextlib.contextmanager
def close_transaction(store, close_id: str, *, lock_timeout: float = 10.0):
    """Lock, reload, and yield the latest versioned close for one mutation.

    Legacy records are upgraded from the bytes read while holding the same lock.
    This is the CLI read-evaluate-write boundary; callers commit through the yielded
    transaction and must not retain it after the context exits.
    """
    close_id = validate_close_id(close_id)
    with _close_update_lock(store, close_id, timeout=lock_timeout):
        current = load_close(store, close_id)
        if close_instance_id(current) is None:
            current = _upgrade_legacy_locked(store, close_id, current)
        transaction = CloseTransaction(store, close_id, current)
        try:
            yield transaction
        finally:
            transaction._finish()


def create_close(store, record: dict, *, lock_timeout: float = 10.0) -> int:
    """Exclusively create a close and return its first persisted generation."""
    close_id = validate_close_id(record.get("close_id"))
    generation = close_generation(record)
    instance_id = close_instance_id(record)
    if generation != 0:
        raise CloseConflict(
            f"new close {close_id!r} must start at generation 0, got {generation}")
    if instance_id is not None:
        raise CloseConflict("new close must not reuse an existing instance_id")
    closes_dir(store).mkdir(parents=True, exist_ok=True)
    path = close_path(store, close_id)
    with _close_update_lock(store, close_id, timeout=lock_timeout):
        if path.exists():
            raise CloseConflict(f"close {close_id!r} already exists; use a checked update")
        persisted = dict(record)
        persisted["instance_id"] = uuid.uuid4().hex
        persisted["generation"] = 1
        if not _is_wellformed(persisted):
            raise CloseError(f"close {close_id!r} is malformed")
        _write_close(path, persisted)
    record["instance_id"] = persisted["instance_id"]
    record["generation"] = 1
    return 1


def upgrade_legacy_close(store, close_id: str, *, lock_timeout: float = 10.0) -> dict:
    """Add version identity to the latest legacy record under its close lock.

    The caller supplies no stale record to overwrite: this function reads the
    current bytes while locked, adds only identity metadata, persists, and returns
    the upgraded record. Callers must then use checked saves.
    """
    close_id = validate_close_id(close_id)
    path = close_path(store, close_id)
    with _close_update_lock(store, close_id, timeout=lock_timeout):
        if not path.exists():
            raise CloseError(f"no close {close_id!r} at {path}")
        current = load_close(store, close_id)
        upgraded = _upgrade_legacy_locked(store, close_id, current)
    return upgraded


def replace_close(store, record: dict, *, expected_generation: int | None,
                  expected_instance_id: str | None,
                  lock_timeout: float = 10.0) -> int:
    """Atomically replace an existing close with a new, independently identified one."""
    close_id = validate_close_id(record.get("close_id"))
    _validate_expected_tokens(expected_generation, expected_instance_id)
    if close_generation(record) != 0 or close_instance_id(record) is not None:
        raise CloseConflict("replacement close must be a fresh unpersisted record")
    path = close_path(store, close_id)
    with _close_update_lock(store, close_id, timeout=lock_timeout):
        if not path.exists():
            raise CloseConflict(f"close {close_id!r} no longer exists; reload before retrying")
        current = load_close(store, close_id)
        _require_current_tokens(
            current, close_id=close_id,
            expected_generation=expected_generation,
            expected_instance_id=expected_instance_id,
        )
        persisted = dict(record)
        persisted["instance_id"] = uuid.uuid4().hex
        persisted["generation"] = 1
        if not _is_wellformed(persisted):
            raise CloseError(f"close {close_id!r} is malformed")
        _write_close(path, persisted)
    record["instance_id"] = persisted["instance_id"]
    record["generation"] = 1
    return 1


def save_close(store, record: dict, *, expected_generation: int | None = None,
               expected_instance_id: str | None = None,
               lock_timeout: float = 10.0) -> int:
    """Checked update of an existing close; creation uses :func:`create_close`.

    Both preconditions are mandatory. The generation prevents lost updates and
    the immutable instance id prevents delete/recreate ABA from matching a stale
    updater that happens to carry the recreated close's generation.
    """
    close_id = validate_close_id(record.get("close_id"))
    generation = close_generation(record)
    instance_id = close_instance_id(record)
    if expected_generation is None:
        raise CloseConflict(
            f"close {close_id!r} update requires expected_generation; "
            "use create_close for creation")
    if (not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0):
        raise CloseError("expected close generation must be a non-negative integer")
    if instance_id is None:
        raise CloseConflict(
            f"close {close_id!r} is a legacy record; run upgrade_legacy_close first")
    if expected_instance_id is None:
        raise CloseConflict(f"close {close_id!r} update requires expected_instance_id")
    if (not isinstance(expected_instance_id, str)
            or not _INSTANCE_ID_RE.match(expected_instance_id)):
        raise CloseError("expected close instance_id must be a 32-character lowercase hex id")
    if generation != expected_generation:
        raise CloseConflict(
            f"close {close_id!r} record generation {generation} does not match "
            f"expected generation {expected_generation}")
    if instance_id != expected_instance_id:
        raise CloseConflict(
            f"close {close_id!r} record instance {instance_id!r} does not match "
            f"expected instance {expected_instance_id!r}")
    path = close_path(store, close_id)
    with _close_update_lock(store, close_id, timeout=lock_timeout):
        if not path.exists():
            raise CloseConflict(f"close {close_id!r} no longer exists; reload before retrying")
        current = load_close(store, close_id)
        _require_current_tokens(
            current, close_id=close_id,
            expected_generation=expected_generation,
            expected_instance_id=expected_instance_id,
        )
        next_generation = expected_generation + 1
        persisted = dict(record)
        persisted["generation"] = next_generation
        if not _is_wellformed(persisted):
            raise CloseError(f"close {close_id!r} is malformed")
        _write_close(path, persisted)
    record["generation"] = next_generation
    return next_generation


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
              override: bool = False, from_groups: list[str] | None = None) -> dict:
    """Record a lens ack. Refuses if the close is not accepting acks (must be open/
    reopened, not published). ACCEPT carries typed evidence (validated by the
    caller via gates.validate_review_result_evidence); NA needs a reason; COUNTER
    needs a counter_id (the caller also creates the counter record). ``from_groups``
    (resolved by the CLI from the roster) is stored so group-refset authorization
    stays pure (see :func:`_ack_authorized`)."""
    if record.get("status") == PUBLISHED:
        raise CloseError("close is published; reopen before acking (stale-proof)")
    if status not in ACK_STATUSES:
        raise CloseError(f"ack status must be one of {sorted(ACK_STATUSES)}")
    if status == NA and not (reason and reason.strip()):
        raise CloseError("an NA ack requires a reason")
    if status == COUNTER and not counter_id:
        raise CloseError("a COUNTER ack requires a counter_id")
    if status == COUNTER and counter_id in record.get("counters", {}):
        raise CloseError(f"duplicate counter id {counter_id!r} on this close")
    record["lens_acks"][lens_id] = {
        "lens": lens_id, "status": status, "from": agent, "from_role": from_role,
        "from_groups": list(from_groups or []),
        "revision": record.get("revision"), "at": at,
        "evidence": evidence or {}, "reason": reason, "counter_id": counter_id,
        "override": bool(override),
    }
    if status == COUNTER:
        record["counters"][counter_id] = {
            "counter_id": counter_id, "lens": lens_id, "raised_by": agent,
            "at": at, "decision": COUNTER_PENDING, "remediation_id": None,
            "finding": (evidence or {}).get("finding") or reason or "",
        }
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
        if revision != record.get("revision"):
            record["dirty_artifact"] = None
        record["revision"] = revision
    if revision_clean is not None:
        record["revision_clean"] = bool(revision_clean)
    _event(record, "reopen", by, at, revision=revision)
    return record


def apply_signoffs(record: dict, *, policy: dict, risk_inventory: list,
                   derived_by: str, at: str,
                   resolved_candidates_at_apply: dict | None = None) -> dict:
    """The ONLY mutating derivation step. Freeze the route INPUTS (policy hash, risk
    inventory hash, revision), derive first-class required_signoffs from
    (policy, risk_inventory), and (re)generate their signoff lens slots as
    ``required: false`` P2 lenses carrying the candidate refset (so the P2 ack
    machinery + refset auth apply, but P2's per-lens required-checks skip them - the
    count is enforced purely by _evaluate_signoffs). Concrete agents are NOT frozen:
    the refsets resolve against the current roster/groups/roles/domains at check.
    ``resolved_candidates_at_apply`` is recorded AUDIT-ONLY."""
    if record.get("status") == PUBLISHED:
        raise CloseError("close is published; reopen before re-deriving signoffs")
    inv = [_validate_risk_entry(e) for e in (risk_inventory or [])]
    derived = derive_required_signoffs(policy, inv)
    signoffs = derived["signoffs"]
    audit = resolved_candidates_at_apply or {}
    for s in signoffs:
        s["resolved_candidates_at_apply"] = list(audit.get(s["id"], []))
    record["risk_inventory"] = inv
    record["required_signoffs"] = signoffs
    # rebuild generated signoff lenses (drop any from a prior apply, keep manual ones)
    manual = [ln for ln in record.get("required_lenses", [])
              if isinstance(ln, dict) and not ln.get("signoff_set_id")]
    generated = []
    for s in signoffs:
        for lid in s["generated_lens_ids"]:
            generated.append(validate_lens_spec({
                "id": lid, "required": False, "signoff_set_id": s["id"],
                "allowed_agents": s["candidate_refset"].get("agents"),
                "allowed_roles": s["candidate_refset"].get("roles"),
                "allowed_groups": s["candidate_refset"].get("groups"),
            }))
    record["required_lenses"] = manual + generated
    record["signoff_route"] = {
        "policy_hash": policy_hash(policy),
        "risk_inventory_hash": risk_inventory_hash(inv),
        "revision": record.get("revision"),
        "derived_at": at, "derived_by": derived_by,
        "unmapped_risks": derived["unmapped"],
    }
    _event(record, "signoffs:apply", derived_by, at,
           required=len(signoffs), unmapped=len(derived["unmapped"]))
    return record


def _validate_risk_entry(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise CloseError("each risk_inventory entry must be an object")
    rc = validate_risk_class(raw.get("risk_class"))
    na_reason = raw.get("na_reason")
    if na_reason is not None and not (isinstance(na_reason, str) and na_reason.strip()):
        raise CloseError("risk_inventory na_reason, when present, must be a non-empty string")
    return {
        "risk_class": rc,
        "source": raw.get("source"),
        "affected_paths": _str_list(raw.get("affected_paths")),
        "na_reason": na_reason,
    }


def signoff_override(record: dict, *, set_id: str, by: str, at: str, reason: str) -> dict:
    """The SINGLE escape for an unroutable/blocked required signoff: a recorded
    close-lead override with a reason. It is audited as an override, NOT counted as a
    specialist signoff (the CLI gates this on close-lead authority)."""
    if record.get("status") == PUBLISHED:
        raise CloseError("close is published; reopen before overriding a signoff")
    if not (reason and reason.strip()):
        raise CloseError("a signoff override requires a reason")
    known = {s.get("id") for s in record.get("required_signoffs", []) if isinstance(s, dict)}
    if set_id not in known:
        raise CloseError(f"no required signoff {set_id!r} on this close (known: {sorted(known)})")
    record.setdefault("signoff_overrides", {})[set_id] = {
        "by": by, "reason": reason, "at": at}
    _event(record, "signoffs:override", by, at, set_id=set_id)
    return record
