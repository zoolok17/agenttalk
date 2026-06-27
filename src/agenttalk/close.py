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

import hashlib
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
# P3 specialist sign-off hold codes (join the same stable set).
HOLD_MISSING_SIGNOFF = "missing_required_signoff"
HOLD_UNROUTABLE_SIGNOFF = "unroutable_required_signoff"
HOLD_INVALID_POLICY = "invalid_signoff_policy"
HOLD_UNMAPPED_RISK = "unmapped_required_risk"
HOLD_STALE_ROUTE = "stale_signoff_route"

# Core-neutral risk envelope (project: extensions allowed, like gates). Core
# VALIDATES the string; it never DECIDES a change's risk - the project supplies
# the risk inventory and the risk -> signoff policy mapping.
CORE_RISK_CLASSES = frozenset({
    "none", "unknown", "release", "security", "performance", "persistence",
    "docs-contract", "quality"})
_RISK_EXTENSION_RE = re.compile(r"\Aproject:[a-z0-9][a-z0-9_.-]{0,63}\Z")

SIGNOFF_DIRNAME = "signoffs.json"

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
        "required_lenses": required_lenses,     # [{id, allowed_agents, allowed_roles, allowed_groups, required}]
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
                    signoff_eval: dict | None = None) -> dict[str, Any]:
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
    always supplies one for P3 closes). Returns
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

    # P3: derived specialist sign-off counts (pure over the CLI-supplied eval).
    for code, detail in _evaluate_signoffs(record, signoff_eval):
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
    if (route.get("policy_hash") != ev.get("current_policy_hash")
            or route.get("risk_inventory_hash") != ev.get("current_risk_inventory_hash")):
        out.append((HOLD_STALE_ROUTE,
                    "signoff route is stale (policy or risk inventory changed since "
                    "`close signoffs apply`); rerun apply"))
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
    """Core VALIDATES a risk_class string (envelope OR a project: extension); it
    never DECIDES risk. Raises CloseError on a bad string."""
    if not isinstance(value, str) or not value:
        raise CloseError("risk_class must be a non-empty string")
    if value in CORE_RISK_CLASSES or _RISK_EXTENSION_RE.match(value):
        return value
    raise CloseError(
        f"risk_class {value!r} is not in the core envelope {sorted(CORE_RISK_CLASSES)} "
        "or a project:extension")


def _refset(raw: object) -> dict:
    """Normalize a candidate refset using the SAME vocabulary as roster/domains."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise CloseError("a candidate refset must be an object {agents, groups, roles}")
    return {"agents": _str_list(raw.get("agents")),
            "groups": _str_list(raw.get("groups")),
            "roles": _str_list(raw.get("roles"))}


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
                "use_default_reviewers": bool(s.get("use_default_reviewers", False)),
                "include_domain_reviewers": bool(s.get("include_domain_reviewers", False)),
                "allow_na": bool(s.get("allow_na", False)),
                "countable_statuses": statuses,
                "override_counts": bool(s.get("override_counts", False)),
            })
        risk_policies[risk_class] = norm_sets
    return {
        "schema_version": int(raw.get("schema_version", SCHEMA_VERSION)),
        "defaults": {"reviewers": default_reviewers},
        "risk_policies": risk_policies,
        "allow_unmapped": bool(raw.get("allow_unmapped", False)),
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
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return None, f"signoffs.json is unreadable/corrupt: {e}"
    try:
        return validate_signoff_policy(raw), None
    except CloseError as e:
        return None, str(e)


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
    record["lens_acks"][lens_id] = {
        "lens": lens_id, "status": status, "from": agent, "from_role": from_role,
        "from_groups": list(from_groups or []),
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
