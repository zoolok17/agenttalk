"""Protected measurement targets and conservative implication for built-in predicates."""

import hashlib
import json

from agenttalk import acceptance as A

MAX_LINKS = 32


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def target(row):
    return {key: row[key] for key in ("partition", "artifact", "field")}


def target_id(row):
    return "target-" + hashlib.sha256(canonical(target(row))).hexdigest()


def predicate(row):
    if row["comparator"] == "exact-failure-set":
        return {"kind": "failure-set", "expected": sorted(set(row["expected"]))}
    # Both exact-value and exit-code accept precisely this typed integer when
    # expected is an integer. Neither accepts true or a floating-point spelling.
    return {"kind": "exact-json", "expected": row["expected"]}


def implies(candidate, obligation):
    if canonical(candidate) == canonical(obligation):
        return True
    value = candidate["expected"]
    return (candidate["kind"] == "exact-json" and obligation["kind"] == "failure-set"
            and isinstance(value, list) and len(value) <= A.MAX_ITEMS
            and all(isinstance(item, str) and item.strip() and len(item) <= 4096 for item in value)
            and len(set(value)) == len(value)
            and sorted(set(value)) == obligation["expected"])


def strongest(predicates):
    unique = {canonical(p): p for p in predicates}
    return [unique[key] for key in sorted(unique)
            if not any(other != key and implies(value, unique[key]) for other, value in unique.items())]


def group(rows):
    targets = {}
    for row in rows:
        if row["policy"] == "gating":
            entry = targets.setdefault(target_id(row), {"target": target(row), "predicates": []})
            entry["predicates"].append(predicate(row))
    for entry in targets.values():
        entry["predicates"] = strongest(entry["predicates"])
    return targets


def history(store, record, *, max_links=MAX_LINKS):
    """Collect all retained obligations and their outcome sources, never row IDs as identity."""
    targets = {}
    for _ in range(max_links + 1):
        route, plan = A._policy(store, record)
        saved = (record.get("final") or {}).get("acceptance_snapshot") or {}
        if not isinstance(saved, dict):
            A._fail("ancestor outcome snapshot is malformed", "acceptance_record_missing")
        outcomes = {r["id"]: r for r in saved.get("outcomes", [])}
        for row in plan["rows"]:
            if row["policy"] != "gating":
                continue
            entry = targets.setdefault(target_id(row), {"target": target(row), "predicates": [], "sources": []})
            entry["predicates"].append(predicate(row))
            outcome = outcomes.get(row["id"], {})
            # The retained close remains the complete record; this projection
            # avoids recursively embedding prior final snapshots.
            entry["sources"].append({"close_id": record["close_id"], "attempt_id": route["attempt_id"],
                                     "plan_hash": route["plan_hash"], "row_id": row["id"],
                                     "outcome": {k: outcome[k] for k in
                                                 ("id", "policy", "passed", "comparison_passed", "error")
                                                 if k in outcome}})
        digest = route.get("parent_record_hash")
        if digest is None:
            for entry in targets.values():
                entry["predicates"] = strongest(entry["predicates"])
            return targets
        record = A.decode(A._retained(store, digest))
    A._fail("acceptance ancestry exceeds supported depth")


def changes(protected, plan):
    current = group(plan["rows"])
    changed = {}
    for key, old in sorted(protected.items()):
        new = current.get(key, {}).get("predicates", [])
        if not all(any(implies(p, obligation) for p in new) for obligation in old["predicates"]):
            changed[key] = {"target": old["target"], "old": old["predicates"], "new": new}
    return changed
