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

import re
from typing import Any

from agenttalk import domains as dom

SCHEMA_VERSION = 1
STATE_FILENAME = "lanes.json"          # under .agenttalk/state/
DELIVERIES_DIRNAME = "lane-deliveries"  # under .agenttalk/

STATUS_ACTIVE = "active"

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

_LANE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


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
    return any(path_under_prefix(path, pre, casefold=casefold) for pre in (prefixes or []))


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
    if not isinstance(value, str) or not _LANE_ID_RE.match(value):
        raise LaneError(
            f"lane id {value!r} is not a safe identifier (alphanumeric plus . _ -, "
            "starts alphanumeric, max 64 chars)")
    return value


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
             registry_hash_at_assign: str, notes: str | None = None) -> dict[str, Any]:
    """A freshly assigned, ACTIVE lane record (pure; the CLI persists it under lock)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "lane_id": lane_id,
        "status": STATUS_ACTIVE,
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
            if not _shared_approved(lane, disp, casefold=casefold):
                hold(HOLD_SHARED_MISSING_APPROVAL,
                     f"shared path {disp!r} has no recorded approval")
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

    # real overlap with OTHER active lanes (recomputed from THIS lane's touched paths)
    for other in active_lanes:
        if not isinstance(other, dict) or other.get("lane_id") == lane.get("lane_id"):
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


def _shared_approved(lane: dict, path: str, *, casefold: bool) -> bool:
    for appr in lane.get("shared_approvals", []) or []:
        if not isinstance(appr, dict):
            continue
        pg = appr.get("path_or_glob", "")
        if dom.glob_matches(pg, path, casefold=casefold) or path_under_prefix(
                path, pg, casefold=casefold):
            return True
    return False


# --------------------------------------------------------------- state I/O
#
# lanes.json lives under .agenttalk/state/ (reset clears it). The CLI wraps the
# read-validate-write of assign/deliver in store._config_lock() so two concurrent
# assigners cannot each validate a stale snapshot and create overlapping lanes.

def lanes_path(store):
    return store.dir / "state" / STATE_FILENAME


def deliveries_dir(store):
    return store.dir / DELIVERIES_DIRNAME


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


def delivery_artifact_path(store, lane_id: str, head_sha: str):
    safe_head = head_sha[:12] if _FULL_SHA_RE.match(str(head_sha)) else "nohead"
    return deliveries_dir(store) / f"{validate_lane_id(lane_id)}-{safe_head}.json"


def write_delivery_artifact(store, *, lane: dict, head_sha: str, verdict: dict,
                            changed: dict, merge: dict, gate_check: dict,
                            delivered_by: str, at: str):
    """Durable delivery evidence OUTSIDE lanes.json (reset clears lanes.json). This is
    the stable pointer close/gate evidence can reference. Written BEFORE the lane is
    cleared; if this write fails the CLI must NOT clear the lane."""
    import json

    from agenttalk import _atomic
    d = deliveries_dir(store)
    d.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "lane_id": lane["lane_id"],
        "domain_id": lane.get("domain_id"),
        "assignee": lane.get("assignee"),
        "base_sha": lane.get("base_sha"),
        "target_ref": lane.get("target_ref"),
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
    path = delivery_artifact_path(store, lane["lane_id"], head_sha)
    _atomic.write_text(path, json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return path
