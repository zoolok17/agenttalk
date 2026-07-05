"""Middle-tier Phase 1: lane DELIVER-GATE (generic, opt-in).

A *lane* is an active, scoped assignment: one assignee works a subset of a domain
(repo-relative path prefixes) from a base SHA toward a target ref. `lane check` /
`lane deliver` aggregate the coordination signals — segment-aware path bounds vs the
domain registry, overlap with other active lanes, a `git merge-tree` clean check
against the current target head, epoch/registry staleness, and the assurance gate
verdict — into ONE advisory HOLD/GO for delivering that work.

Design (codex lane design, lead-gated; dev-2 review folded in):
  * State is ``.agenttalk/state/lanes.json`` — ACTIVE coordination state, CLEARED by
    `reset` (with a warning when active lanes exist). Missing = no lanes; a malformed
    file fails closed for LANE COMMANDS ONLY (it must never brick send/wait/status).
  * The verdict (:func:`compute_verdict`) is PURE over already-RESOLVED data (the
    lane record, the domain classification of each changed path, the changed-path
    evidence, the active-lane snapshot, the current epoch + registry hash, the merge
    evidence, and the gate verdict). The CLI/git adapter does ALL the I/O — so the
    verdict is unit-testable from synthetic inputs (mirrors gates/close compute_verdict).
  * Path bounds + disjointness are SEGMENT-AWARE and reuse the domains normalizer, so
    a lane classifies a path IDENTICALLY to domain ownership (``src/foo`` includes
    ``src/foo/x`` but NOT ``src/foobar``).
  * ADVISORY, point-in-time COORDINATION — not a file lock, not Git/OS authz, not a
    merge guarantee (a clean merge-tree is only clean as of the target head it was
    run against), and not a parallel gate (lanes CONSUME gate state, never set it).
  * A delivery writes a durable artifact OUTSIDE lanes.json (which reset clears);
    the artifact is the stable pointer for close/gate evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Any

from agenttalk import domains as dom

SCHEMA_VERSION = 1
STATE_FILENAME = "lanes.json"          # under .agenttalk/state/
DELIVERIES_DIRNAME = "lane-deliveries"  # under .agenttalk/

STATUS_ACTIVE = "active"
STATUS_DELIVERED = "delivered"
STATUS_ABANDONED = "abandoned"
STATUS_CLEANUP_FAILED = "cleanup_failed"
STATUS_CLEANUP_PENDING = "cleanup_pending"

WORKTREE_MARKER_FILENAME = ".agenttalk-worktrees-root"
WORKTREE_DEFAULT_DIRNAME = ".worktrees"
WORKTREE_VERIFIER_VERSION = 1
INTEGRITY_KEY_FILENAME = ".worktree-integrity-secret"

VERDICT_GO = "GO"
VERDICT_HOLD = "HOLD"

# STABLE hold codes (the public verdict contract; tests assert each one).
HOLD_STALE_EPOCH = "stale_epoch"
HOLD_STALE_REGISTRY = "stale_registry"
HOLD_DIFF_UNAVAILABLE = "diff_unavailable"
HOLD_DIFF_PARSE_ERROR = "diff_parse_error"
HOLD_CASEFOLD_COLLISION = "casefold_collision"
HOLD_OUT_OF_BOUNDS = "out_of_bounds_path"
HOLD_UNOWNED = "unowned_path"
HOLD_DOMAIN_OVERLAP = "domain_overlap_path"
HOLD_SHARED_MISSING_APPROVAL = "shared_path_missing_approval"
HOLD_SHARED_WRONG_APPROVAL = "shared_path_wrong_approval"
HOLD_ACTIVE_LANE_OVERLAP = "active_lane_overlap"
HOLD_MERGE_CONFLICT = "merge_conflict"
HOLD_MERGE_UNKNOWN = "merge_unknown_degraded"
HOLD_GATE = "gate_hold"
HOLD_MALFORMED = "malformed_lane"

_LANE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9_-]\Z|\A[A-Za-z0-9]\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class LaneError(ValueError):
    """Invalid lane input / state (CLI maps to a usage exit)."""


# --------------------------------------------------------------- segment bounds

def _segments(path: str, *, casefold: bool) -> list[str]:
    """Normalized repo-relative path segments (reuses the domains normalizer so lane
    bounds and domain ownership split paths identically)."""
    norm = dom.normalize_repo_path(path, casefold=casefold)
    return [s for s in norm.split("/") if s]


def path_under_prefix(path: str, prefix: str, *, casefold: bool) -> bool:
    """SEGMENT-aware containment: ``path`` is under ``prefix`` iff it equals the
    prefix or extends it at a segment boundary. ``src/foo`` covers ``src/foo`` and
    ``src/foo/x`` but NOT ``src/foobar`` (the string-prefix bug). An empty prefix
    covers everything (a whole-repo lane)."""
    if not (prefix and prefix.strip()):
        return True            # empty prefix = whole repo, covers any path
    p = _segments(prefix, casefold=casefold)
    t = _segments(path, casefold=casefold)
    if not p:
        return True
    return len(t) >= len(p) and t[:len(p)] == p


def path_in_subset(path: str, prefixes: list[str], *, casefold: bool) -> bool:
    """An EMPTY prefix list means 'no path-prefix narrowing' — the whole domain —
    so every path is in subset and the per-path domain classification does the
    bounding. A non-empty list narrows to those segment-aware prefixes."""
    if not prefixes:
        return True
    return any(path_under_prefix(path, pre, casefold=casefold) for pre in prefixes)


def prefixes_disjoint(a: list[str], b: list[str], *, casefold: bool) -> bool:
    """True iff NO prefix of one set is under (or equal to / contains) a prefix of the
    other — segment-aware. Used to validate that two lanes do not claim overlapping
    scope. An empty prefix list (whole repo) is never disjoint from a non-empty set."""
    a = a or []
    b = b or []
    if not a or not b:
        # an empty subset means "the whole domain" — it overlaps any other set, so
        # the two are NOT disjoint.
        return False
    for pa in a:
        for pb in b:
            if (path_under_prefix(pa, pb, casefold=casefold)
                    or path_under_prefix(pb, pa, casefold=casefold)):
                return False
    return True


# --------------------------------------------------------------- lane record

def validate_lane_id(value: str) -> str:
    bad = (
        not isinstance(value, str)
        or not _LANE_ID_RE.match(value)
        or value in {".", ".."}
        or ".." in value
        or value.endswith(".")
        or value.lower().endswith(".lock")
        or any(ch in value for ch in "/\\: \t\r\n~^?*[")
        or bool(_CONTROL_RE.search(value))
        or value.startswith("-")
    )
    if bad:
        raise LaneError(
            f"lane id {value!r} is not a safe identifier (ASCII alphanumeric plus . _ -, "
            "starts alphanumeric, ends alphanumeric/_/-, max 64 chars, no path/ref metacharacters)")
    return value


def lane_branch(lane_id: str) -> str:
    return f"lane/{validate_lane_id(lane_id)}"


def lane_ref(lane_id: str) -> str:
    return f"refs/heads/{lane_branch(lane_id)}"


def canonical_host_path(value: str | os.PathLike[str]) -> str:
    """Canonical host path token for persisted worktree provenance.

    The compare key is deliberately host-local: absolute + realpath + Windows
    normcase + normalized separators. It is not a URL or a repo-relative path.
    """
    raw = os.fspath(value)
    path = os.path.abspath(os.path.realpath(raw))
    if os.name == "nt":
        path = os.path.normcase(path)
    return path.replace("\\", "/")


def normalize_prefixes(raw: object, *, casefold: bool = False) -> list[str]:
    """Normalize a path_subset to repo-relative prefixes (display form, not
    casefolded — casefolding is applied at COMPARE time). [] means the whole domain."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise LaneError("path_subset must be a list of repo-relative path prefixes")
    out = []
    for p in raw:
        if not p.strip():
            raise LaneError("a path prefix must be non-empty")
        out.append(dom.normalize_repo_path(p))   # validates + normalizes; raises on unsafe
    return out


def new_lane(lane_id: str, *, assignee: str, assigned_by: str, assigned_at: str,
             domain_id: str, path_subset: list[str], base_sha: str, target_ref: str,
             target_head_at_assign: str, epoch_at_assign: str | None,
             registry_hash_at_assign: str, notes: str | None = None,
             worktree: dict | None = None, waiver: dict | None = None,
             release_class: bool = True) -> dict[str, Any]:
    """A freshly assigned, ACTIVE lane record (pure; the CLI persists it under lock)."""
    lane = {
        "schema_version": SCHEMA_VERSION,
        "lane_id": lane_id,
        "status": STATUS_ACTIVE,
        "release_class": bool(release_class),
        "assignee": assignee,
        "assigned_by": assigned_by,
        "assigned_at": assigned_at,
        "domain_id": domain_id,
        "path_subset": list(path_subset),
        "base_sha": base_sha,
        "target_ref": target_ref,
        "target_head_at_assign": target_head_at_assign,
        "epoch_at_assign": epoch_at_assign,             # AUDIT + staleness
        "registry_hash_at_assign": registry_hash_at_assign,
        "shared_approvals": [],                         # [{path/glob, approved_by, reason, at, epoch, registry_hash}]
        "notes": notes,
    }
    if worktree:
        lane.update({
            "worktree_path": worktree.get("path"),
            "worktree_branch": worktree.get("branch"),
            "worktree_base_sha": worktree.get("base_sha"),
            "worktree_created_at": worktree.get("created_at"),
            "worktree_root": worktree.get("root"),
            "worktree_state": worktree.get("state") or STATUS_ACTIVE,
            "worktree_toplevel_canonical": worktree.get("toplevel_canonical"),
            "worktree_common_git_dir_canonical": worktree.get("common_git_dir_canonical"),
        })
    if waiver:
        lane.update({
            "worktree_waived": True,
            "worktree_waiver_reason": waiver.get("reason"),
            "worktree_waived_by": waiver.get("by"),
            "worktree_waived_at": waiver.get("at"),
        })
    else:
        lane["worktree_waived"] = False
    return lane


def add_shared_approval(lane: dict, *, path_or_glob: str, approved_by: str, reason: str,
                        at: str, epoch: str | None, registry_hash: str) -> dict:
    if not (reason and reason.strip()):
        raise LaneError("a shared-path approval requires a reason")
    lane.setdefault("shared_approvals", []).append({
        "path_or_glob": dom.normalize_repo_path(path_or_glob),
        "approved_by": approved_by, "reason": reason, "at": at,
        "epoch": epoch, "registry_hash": registry_hash,
    })
    return lane


def _is_wellformed(lane: object) -> bool:
    if not isinstance(lane, dict):
        return False
    if lane.get("schema_version") != SCHEMA_VERSION:
        return False
    for key in ("lane_id", "assignee", "domain_id", "base_sha", "target_ref",
                "target_head_at_assign", "registry_hash_at_assign", "status"):
        if not isinstance(lane.get(key), str) or not lane.get(key):
            return False
    if not isinstance(lane.get("path_subset"), list):
        return False
    if not isinstance(lane.get("shared_approvals"), list):
        return False
    return True


# --------------------------------------------------------------- pure verdict

def compute_verdict(lane: dict, *, changed: dict, classifications: dict,
                    active_lanes: list[dict], current_epoch: str | None,
                    current_registry_hash: str, merge: dict, gate_check: dict,
                    casefold: bool) -> dict[str, Any]:
    """PURE: derive HOLD|GO + stable hold codes for delivering ``lane``. All inputs
    are already RESOLVED by the CLI/git adapter:

      changed:          {"error": <None|"unavailable"|"parse_error">, "paths": [
                          {"path", "old_path"(rename), "status", "touched": bool}]}
                        — `touched` paths are the ones a delivery WRITES (M/A/D/T,
                        rename old+new, copy DEST; a copy SOURCE is evidence-only).
      classifications:  {display_path: domains.check_path(...) result} for touched paths.
      active_lanes:     other ACTIVE lane records (this lane excluded) for real overlap.
      current_epoch / current_registry_hash: for staleness vs the lane's stamps.
      merge:            {"status": "clean"|"conflict"|"unknown", "detail": ...}.
      gate_check:       gates.check_gates(...) result for the lane's gate scope.

    No I/O. Returns ``{"verdict", "holds": [{code, detail}], "ok"}``."""
    holds: list[dict] = []

    def hold(code: str, detail: str) -> None:
        holds.append({"code": code, "detail": detail})

    if not _is_wellformed(lane):
        hold(HOLD_MALFORMED, "lane record is missing required structure")
        return _verdict(holds)

    # staleness — independent of the diff
    if current_epoch != lane.get("epoch_at_assign"):
        hold(HOLD_STALE_EPOCH, "a barrier moved the epoch since assign; re-stamp the lane")
    if current_registry_hash != lane.get("registry_hash_at_assign"):
        hold(HOLD_STALE_REGISTRY, "the domain registry changed since assign; re-stamp the lane")

    # diff availability / integrity
    err = changed.get("error")
    if err == "unavailable":
        hold(HOLD_DIFF_UNAVAILABLE, "could not compute the diff (git unavailable)")
        return _verdict(holds)
    if err == "parse_error":
        hold(HOLD_DIFF_PARSE_ERROR, "could not parse the diff output")
        return _verdict(holds)

    touched = [p for p in changed.get("paths", []) if p.get("touched")]

    # casefold collisions among touched paths (two display paths, one casefold key)
    seen_keys: dict[str, str] = {}
    for entry in touched:
        disp = entry.get("path", "")
        key = dom.normalize_repo_path(disp, casefold=True)
        if key in seen_keys and seen_keys[key] != disp:
            hold(HOLD_CASEFOLD_COLLISION,
                 f"paths {seen_keys[key]!r} and {disp!r} collide under casefold")
        seen_keys.setdefault(key, disp)

    subset = lane.get("path_subset") or []
    domain_id = lane.get("domain_id")
    for entry in touched:
        disp = entry.get("path", "")
        # 1) must be inside the lane's declared subset
        if not path_in_subset(disp, subset, casefold=casefold):
            hold(HOLD_OUT_OF_BOUNDS, f"{disp!r} is outside the lane path subset {subset}")
            continue
        cls = classifications.get(disp) or {}
        shared = cls.get("shared_paths") or []
        domains = cls.get("domains") or []
        if shared:
            status = _shared_approval_status(
                lane, disp, cls, current_epoch=current_epoch,
                current_registry_hash=current_registry_hash, casefold=casefold)
            if status == "wrong":
                # Some matching shared entry HAS an approval but it is stale (epoch/
                # registry moved) or was recorded by someone not authorized for THAT
                # entry. Distinct from "missing" so a stale/forged approval is never
                # mistaken for a real one.
                hold(HOLD_SHARED_WRONG_APPROVAL,
                     f"shared path {disp!r} has an approval that is stale, forged, or "
                     "not authorized for a matching shared entry")
            elif status != "ok":
                # ALL-matching rule: every shared entry that matches the path must be
                # approved; at least one matching entry has no satisfying approval.
                hold(HOLD_SHARED_MISSING_APPROVAL,
                     f"shared path {disp!r} is missing a required approval - EVERY "
                     "matching shared entry must be approved by an authorized approver")
            continue
        if not domains:
            hold(HOLD_UNOWNED, f"{disp!r} is unowned (no domain) and not a shared path")
            continue
        if len(domains) > 1:
            hold(HOLD_DOMAIN_OVERLAP,
                 f"{disp!r} matches multiple domains {sorted(domains)} (never silently chosen)")
            continue
        if domains[0] != domain_id:
            hold(HOLD_OUT_OF_BOUNDS,
                 f"{disp!r} is owned by domain {domains[0]!r}, not this lane's {domain_id!r}")
            continue

    # real overlap with OTHER active lanes (recomputed from THIS lane's touched paths).
    # DOMAIN-AWARE: only a same-domain lane can legitimately claim a path this lane's
    # domain owns; a different-domain lane touching the same file would show up as
    # domain_overlap_path (path matches >1 domain) instead. Without this filter a
    # whole-domain lane (empty subset, which path_in_subset treats as "all paths")
    # would false-overlap every other lane regardless of domain.
    for other in active_lanes:
        if not isinstance(other, dict) or other.get("lane_id") == lane.get("lane_id"):
            continue
        if other.get("domain_id") != lane.get("domain_id"):
            continue
        other_subset = other.get("path_subset") or []
        for entry in touched:
            disp = entry.get("path", "")
            if path_in_subset(disp, other_subset, casefold=casefold):
                hold(HOLD_ACTIVE_LANE_OVERLAP,
                     f"{disp!r} also falls in active lane {other.get('lane_id')!r}")
                break

    # merge evidence (the conflict authority)
    mstatus = merge.get("status")
    if mstatus == "conflict":
        hold(HOLD_MERGE_CONFLICT, f"merge-tree reports a conflict: {merge.get('detail', '')}")
    elif mstatus != "clean":
        hold(HOLD_MERGE_UNKNOWN,
             f"merge evidence is degraded/unknown ({merge.get('detail', mstatus)}); "
             "not inferring clean")

    # assurance gate
    if gate_check.get("verdict") != VERDICT_GO:
        names = ", ".join(b.get("name", "?") for b in gate_check.get("blockers", [])) or "?"
        hold(HOLD_GATE, f"gate check is HOLD (blockers: {names})")

    return _verdict(holds)


def _verdict(holds: list[dict]) -> dict[str, Any]:
    return {"verdict": VERDICT_GO if not holds else VERDICT_HOLD,
            "holds": holds, "ok": not holds}


def _shared_approval_status(lane: dict, path: str, cls: dict, *,
                            current_epoch: str | None, current_registry_hash: str,
                            casefold: bool) -> str:
    """ALL-MATCHING-ENTRIES-MUST-APPROVE (lead decision D-11, 0.40.0): a touched shared
    ``path`` is approved only when EVERY shared entry that matches it has a valid
    approval - fresh (epoch + registry hash current) AND recorded against THAT entry by
    an approver authorized for it (its default_approvers, resolved by the CLI into
    ``cls['shared_entry_approvers']``, or a close lead).

    This is the ONLY provably fail-closed rule for overlapping shared entries: there is
    NO winner-picking, so there is no ordering/containment to get wrong - which is the
    unsound class that bit us twice (a string-prefix match, then a TOTAL-order tuple
    over what is really a PARTIAL order; both let one approver bypass another for
    cross-cutting / non-comparable globs). Stricter-is-safer: a path governed by two
    shared policies must satisfy BOTH; overlap is a deliberate registry choice.

    Returns ok|wrong|missing: "wrong" if some matching entry has an approval that is
    stale/forged/unauthorized (more actionable than a bare "missing"); "missing" if some
    matching entry has no approval at all; "ok" only when every matching entry is
    satisfied. The common case (exactly one matching entry) is unchanged.
    """
    per_glob = cls.get("shared_entry_approvers") or {}
    leads = set(cls.get("close_leads") or [])
    if not per_glob:
        return "missing"   # shared but no resolved entries -> fail closed
    approvals = [a for a in (lane.get("shared_approvals") or []) if isinstance(a, dict)]
    any_wrong = False
    any_missing = False
    for glob, entry_approvers in per_glob.items():
        try:
            glob_norm = dom.normalize_glob(glob, casefold=casefold)
        except (dom.DomainError, ValueError):
            any_wrong = True       # a registry glob we cannot normalize -> fail closed
            continue
        authorized = set(entry_approvers or []) | leads
        satisfied = False
        recorded_for_entry = False
        for appr in approvals:
            pg = appr.get("path_or_glob") or ""
            try:
                if dom.normalize_glob(pg, casefold=casefold) != glob_norm:
                    continue
            except (dom.DomainError, ValueError):
                continue           # a malformed stored token never matches (fail safe)
            recorded_for_entry = True
            if appr.get("epoch") != current_epoch:
                continue
            if appr.get("registry_hash") != current_registry_hash:
                continue
            if appr.get("approved_by") in authorized:
                satisfied = True
                break
        if satisfied:
            continue
        if recorded_for_entry:
            any_wrong = True       # an approval exists for this entry but is stale/unauth
        else:
            any_missing = True     # this matching entry has no approval at all
    if any_wrong:
        return "wrong"
    if any_missing:
        return "missing"
    return "ok"


# --------------------------------------------------------------- state I/O
#
# lanes.json lives under .agenttalk/state/ (reset clears it). The CLI wraps the
# read-validate-write of assign/deliver in store._config_lock() so two concurrent
# assigners cannot each validate a stale snapshot and create overlapping lanes.

def lanes_path(store):
    return store.dir / "state" / STATE_FILENAME


def deliveries_dir(store):
    return store.dir / DELIVERIES_DIRNAME


def worktrees_root(store, configured: str | None = None):
    from pathlib import Path
    return Path(configured) if configured else store.root / WORKTREE_DEFAULT_DIRNAME


def _integrity_secret_path(store):
    return deliveries_dir(store) / INTEGRITY_KEY_FILENAME


def _integrity_secret(store) -> bytes:
    from agenttalk import _atomic

    path = _integrity_secret_path(store)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if len(raw) >= 32:
            return raw.encode("ascii", errors="ignore")
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_hex(32)
    _atomic.write_text(path, raw)
    return raw.encode("ascii")


_INTEGRITY_FIELDS = (
    "delivery_id", "lane_id", "worktree_branch", "delivered_head", "base_sha",
    "worktree_toplevel_canonical", "common_git_dir_canonical", "tracked_tree_clean",
    "verifier_version", "delivered_at", "detached_at_lane_tip", "worktree_waived",
    "isolation_status", "worktree_waiver_reason", "worktree_waived_by",
    "worktree_waived_at",
)


def _integrity_payload(artifact: dict) -> dict[str, Any]:
    return {k: artifact.get(k) for k in _INTEGRITY_FIELDS if k in artifact}


def compute_integrity_token(store, artifact: dict) -> str:
    payload = json.dumps(
        _integrity_payload(artifact), ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_integrity_secret(store), payload, hashlib.sha256).hexdigest()


def verify_integrity_token(store, artifact: dict) -> bool:
    token = artifact.get("integrity_token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        return False
    try:
        expected = compute_integrity_token(store, artifact)
    except OSError:
        return False
    return hmac.compare_digest(token, expected)


def load_lanes(store) -> dict:
    """Load lanes.json. Missing = empty. A malformed file FAILS CLOSED for lane
    commands (LaneError) but the caller must ensure this never bricks unrelated bus
    commands — only lane commands call this."""
    import json
    path = lanes_path(store)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "lanes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise LaneError(f"lanes.json is unreadable/corrupt: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        raise LaneError("lanes.json is malformed (expected {schema_version, lanes})")
    return data


def save_lanes(store, data: dict) -> None:
    import json

    from agenttalk import _atomic
    path = lanes_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic.write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def active_lanes(data: dict) -> list[dict]:
    return [ln for ln in (data.get("lanes") or {}).values()
            if isinstance(ln, dict) and ln.get("status") == STATUS_ACTIVE]


def fingerprint(lane: dict) -> tuple:
    """A stable identity for the EVALUATED lane. deliver re-checks this under the lock
    before clearing, so a concurrent `assign --force` of the same id between eval and
    clear cannot make deliver delete a DIFFERENT (newly assigned) lane."""
    return (
        lane.get("lane_id"), lane.get("assigned_at"), lane.get("base_sha"),
        lane.get("target_ref"), lane.get("target_head_at_assign"),
        lane.get("registry_hash_at_assign"),
        tuple(lane.get("path_subset") or []),
        lane.get("worktree_path"), lane.get("worktree_branch"),
        lane.get("worktree_base_sha"), lane.get("worktree_waived"),
    )


def delivery_artifact_path(store, lane_id: str, head_sha: str):
    safe_head = head_sha[:12] if _FULL_SHA_RE.match(str(head_sha)) else "nohead"
    return deliveries_dir(store) / f"{validate_lane_id(lane_id)}-{safe_head}.json"


def write_delivery_artifact(store, *, lane: dict, head_sha: str, verdict: dict,
                            changed: dict, merge: dict, gate_check: dict,
                            delivered_by: str, at: str,
                            worktree_provenance: dict | None = None):
    """Durable delivery evidence OUTSIDE lanes.json (reset clears lanes.json). This is
    the stable pointer close/gate evidence can reference. Written BEFORE the lane is
    cleared; if this write fails the CLI must NOT clear the lane."""
    import json

    from agenttalk import _atomic
    d = deliveries_dir(store)
    d.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "delivery_id": f"{lane['lane_id']}-{head_sha[:12]}-{at}",
        "lane_id": lane["lane_id"],
        "domain_id": lane.get("domain_id"),
        "assignee": lane.get("assignee"),
        "base_sha": lane.get("base_sha"),
        "target_ref": lane.get("target_ref"),
        "worktree_branch": lane_branch(lane["lane_id"]),
        "delivered_head": head_sha,
        "delivered_by": delivered_by,
        "delivered_at": at,
        "verdict": verdict["verdict"],
        "holds": verdict["holds"],
        "changed_paths": [p.get("path") for p in changed.get("paths", []) if p.get("touched")],
        "merge": merge,
        "gate_verdict": gate_check.get("verdict"),
        "path_subset": lane.get("path_subset"),
        "epoch_at_assign": lane.get("epoch_at_assign"),
        "registry_hash_at_assign": lane.get("registry_hash_at_assign"),
    }
    if lane.get("worktree_waived"):
        artifact.update({
            "worktree_waived": True,
            "worktree_waiver_reason": lane.get("worktree_waiver_reason"),
            "worktree_waived_by": lane.get("worktree_waived_by"),
            "worktree_waived_at": lane.get("worktree_waived_at"),
            "isolation_status": "waived",
        })
        artifact["integrity_token"] = compute_integrity_token(store, artifact)
    elif worktree_provenance:
        artifact.update({
            "worktree_waived": False,
            "isolation_status": "verified",
            "worktree_toplevel_canonical": worktree_provenance.get("worktree_toplevel_canonical"),
            "common_git_dir_canonical": worktree_provenance.get("common_git_dir_canonical"),
            "tracked_tree_clean": bool(worktree_provenance.get("tracked_tree_clean")),
            "detached_at_lane_tip": bool(worktree_provenance.get("detached_at_lane_tip")),
            "verifier_version": WORKTREE_VERIFIER_VERSION,
        })
        artifact["integrity_token"] = compute_integrity_token(store, artifact)
    else:
        artifact.update({"worktree_waived": False, "isolation_status": "unverified"})
    path = delivery_artifact_path(store, lane["lane_id"], head_sha)
    _atomic.write_text(path, json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return path


def validate_delivery_artifact(path, *, lane_id: str, head_sha: str,
                               store=None, require_isolation: bool = False) -> dict:
    """Read back + SEMANTICALLY validate the just-written delivery artifact (C5a) before
    the lane is cleared. Raises :class:`LaneError` on ANY read/parse/shape/semantic
    mismatch so the caller leaves the lane ACTIVE - a truncated/corrupt/tampered OR
    valid-JSON-but-wrong artifact must never read as a successful delivery (reviewer-1
    P1: structural-only checks let a HOLD/unsupported-schema artifact clear the lane).
    Covers both the atomic and sandbox direct-write paths (we read the exact path
    returned). Returns the parsed artifact on success."""
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LaneError(f"delivery artifact readback failed ({e})") from e
    if not isinstance(data, dict):
        raise LaneError("delivery artifact is not a JSON object")
    for k in ("schema_version", "lane_id", "delivered_head", "verdict", "holds"):
        if k not in data:
            raise LaneError(f"delivery artifact is missing required field {k!r}")
    # Semantic, not just structural: the artifact must be the CURRENT schema, prove a GO
    # delivery (verdict==GO with NO holds), and describe THIS lane/head. Anything else is
    # corrupt/tampered/unsupported -> fail closed (lane stays active).
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LaneError(
            f"delivery artifact schema_version {data.get('schema_version')!r} != "
            f"{SCHEMA_VERSION} (unsupported)")
    if data.get("verdict") != VERDICT_GO:
        raise LaneError(
            f"delivery artifact verdict is {data.get('verdict')!r}, not {VERDICT_GO} - "
            "refusing to clear the lane on non-GO evidence")
    holds = data.get("holds")
    if not isinstance(holds, list) or holds:
        raise LaneError("delivery artifact holds must be an empty list for a GO delivery")
    if data.get("lane_id") != lane_id or data.get("delivered_head") != head_sha:
        raise LaneError(
            "delivery artifact lane_id/delivered_head does not match the delivered lane "
            f"(got {data.get('lane_id')!r}@{str(data.get('delivered_head'))[:12]})")
    if require_isolation:
        if store is None:
            raise LaneError("delivery artifact isolation validation needs a store secret")
        status = data.get("isolation_status")
        if status == "waived":
            if data.get("worktree_waived") is not True:
                raise LaneError("waived delivery artifact does not record worktree_waived=true")
            for k in ("worktree_waiver_reason", "worktree_waived_by", "worktree_waived_at"):
                if not isinstance(data.get(k), str) or not data[k].strip():
                    raise LaneError(f"waived delivery artifact is missing {k}")
        elif status == "verified":
            if data.get("worktree_waived") is not False:
                raise LaneError("verified delivery artifact does not record worktree_waived=false")
            if data.get("worktree_branch") != lane_branch(lane_id):
                raise LaneError("delivery artifact branch does not match lane id")
            if data.get("base_sha") and not _FULL_SHA_RE.match(str(data.get("base_sha"))):
                raise LaneError("delivery artifact base_sha is not a full SHA")
            if data.get("tracked_tree_clean") is not True:
                raise LaneError("delivery artifact did not record a clean tracked tree")
            for k in ("worktree_toplevel_canonical", "common_git_dir_canonical"):
                if not isinstance(data.get(k), str) or not data[k].strip():
                    raise LaneError(f"delivery artifact is missing {k}")
            if data.get("verifier_version") != WORKTREE_VERIFIER_VERSION:
                raise LaneError("delivery artifact verifier_version is unsupported")
        else:
            raise LaneError("delivery artifact has no verified or waived worktree isolation")
        if not verify_integrity_token(store, data):
            raise LaneError("delivery artifact integrity token is missing or invalid")
    return data
