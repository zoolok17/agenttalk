"""Immutable acceptance successors and operator-origin scope amendments.

All evidence remains private. Operator messages are checked against the existing
reserved principal; this is cooperative origin checking, not authentication.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from agenttalk import acceptance as A, close
from agenttalk import acceptance_coverage as coverage


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _now():
    return datetime.now(timezone.utc)


def successors(store, parent):
    """Read-only audit of every sibling, including open alternatives, by parent attempt."""
    result, errors = [], []
    identity = parent["acceptance_route"].get("attempt_id")
    try:
        close_ids = close.list_close_ids(store, strict=True)
    except OSError as exc:
        return result, [{"error": f"acceptance_audit_unavailable: {close.closes_dir(store)}: {exc}; "
                        "restore readable access to the complete closes directory and retry"}]
    for close_id in close_ids:
        try:
            child = close.load_close(store, close_id)
            digest = (child.get("acceptance_route") or {}).get("parent_record_hash")
            if digest is None:
                continue
            retained = A.decode(A._retained(store, digest))
            if retained["acceptance_route"]["attempt_id"] == identity:
                result.append({"close_id": close_id, "status": child["status"],
                               "verdict": (child.get("final") or {}).get("verdict")})
        except (close.CloseError, OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({"close_id": close_id, "error": str(exc)})
    return result, errors


def _reduction(value):
    fields = "rows reason alternatives impact owner expires_at cause evidence decision_ref"
    A._object(value, fields + " changes", "scope reduction / policy amendment")
    if not isinstance(value["changes"], dict):
        A._fail("amendment changes must map protected targets to old/new coverage")
    A._strings(value["rows"], "reduced rows", nonempty=True)
    A._strings(value["alternatives"], "alternatives", nonempty=True)
    A._strings(value["evidence"], "scope reduction evidence", nonempty=True)
    for digest in value["evidence"]:
        A._digest(digest)
    for key in ("reason", "impact", "owner", "expires_at", "decision_ref"):
        A._text(value[key], key)
    A._id(value["decision_ref"], "reduction.decision_ref")
    if value["cause"] not in {"unavailable-tool", "measured-variance", "policy-amendment"}:
        A._fail("unsupported approval cause: expected unavailable-tool, measured-variance or policy-amendment",
                "acceptance_scope_reduction_unapproved")
    try:
        expiry = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= _now():
            raise ValueError("expired")
    except (ValueError, TypeError) as exc:
        raise A.AcceptanceError("acceptance_scope_reduction_unapproved", "scope approval expired or invalid") from exc
    return value


def approval_payload(parent, successor_id, plan_hash, reduction):
    """Exact structured operator message body, excluding its own message ID."""
    return {"schema_version": 1, "parent_attempt_id": parent["acceptance_route"]["attempt_id"],
            "successor_close_id": successor_id, "plan_hash": plan_hash,
            "reduction": {key: value for key, value in reduction.items() if key != "decision_ref"}}


def _approval(store, data, parent, successor_id, plan_hash, reduction):
    _reduction(reduction)
    msg = A.decode(data)
    try:
        operator = store.operator_identity()
    except ValueError as exc:
        raise A.AcceptanceError("acceptance_scope_reduction_unapproved", "operator identity unavailable") from exc
    if (not isinstance(msg, dict) or msg.get("from") != operator or msg.get("id") != reduction["decision_ref"]
            or msg.get("kind") != "message" or not isinstance(msg.get("body"), str)):
        A._fail("approval must resolve to the actual operator message", "acceptance_scope_reduction_unapproved")
    body = A.decode(msg["body"].encode("utf-8"))
    A._object(body, "schema_version parent_attempt_id successor_close_id plan_hash reduction", "operator approval")
    A._version(body["schema_version"])
    if _bytes(body) != _bytes(approval_payload(parent, successor_id, plan_hash, reduction)):
        A._fail("operator approval does not bind this scope amendment", "acceptance_scope_reduction_unapproved")
    for digest in reduction["evidence"]:
        A._retained(store, digest)


def successor(store, *, parent_id, close_id, plan_file, project_repo, revision, by, at, reason,
              reduction_file=None, cache_root=None):
    """Preserve a terminal parent before exclusively creating a linked attempt."""
    A._id(close_id, "successor.close_id")
    if close_id == parent_id:
        A._fail("a successor needs a different close ID")
    A._text(reason, "amendment reason")
    before = close.load_close(store, parent_id)
    if before["status"] != close.PUBLISHED:
        A._fail("successor requires a published acceptance parent")
    # Import and staged hashing must precede the parent's shared writer lock.
    prepared = A.prepare(store, plan_file, project_repo, revision, before["scope"], cache_root=cache_root)
    with close.close_transaction(store, parent_id) as transaction:
        parent = deepcopy(transaction.record)
        if parent != before:
            A._fail("parent changed during successor preparation; retry", "acceptance_plan_stale")
        route, old_plan = A._policy(store, parent)
        if not A.schema(route["schema_version"]).modern or parent["status"] != close.PUBLISHED:
            A._fail("successor requires a published schema-2 acceptance parent")
        if not isinstance((parent.get("final") or {}).get("acceptance_snapshot"), dict):
            A._fail("parent lacks its published evidence snapshot", "acceptance_record_missing")
        # A child adds one link; refuse before creation rather than burning its ID.
        protected = coverage.history(store, parent, max_links=coverage.MAX_LINKS - 1)
        # Confirm retained evidence remains readable before preserving the parent.
        original = A.resolve(store, parent)
        if any(code in {"acceptance_record_missing", "acceptance_project_unverified"}
               for code, _ in original["holds"]):
            A._fail("parent evidence cannot be preserved", "acceptance_record_missing")
        if (A.schema(prepared["plan"]["schema_version"]).version < A.schema(route["schema_version"]).version
                or prepared["plan"]["project_id"] != old_plan["project_id"]):
            A._fail("successor must retain verified project identity", "acceptance_project_unverified")
        reduction = None
        approval_hash = None
        if reduction_file:
            path = Path(reduction_file).absolute()
            reduction = _reduction(A.decode(A._read(A._path(path.parent, path.name))))
            data = A._read(A._path(store.messages_dir, reduction["decision_ref"] + ".json"))
            _approval(store, data, parent, close_id, prepared["plan_hash"], reduction)
            approval_hash = A._retain(store, data)
        prepared["parent_record_hash"] = A._retain(store, _bytes(parent))
        cause = "source-change" if parent["revision"] != prepared["project"]["revision"] else "policy-change"
        amendment = {"schema_version": 3, "parent_close_id": parent_id,
                     "parent_attempt_id": route["attempt_id"], "successor_close_id": close_id,
                     "prev_plan_hash": route["plan_hash"], "new_plan_hash": prepared["plan_hash"],
                     "prev_registry_hash": route["registry_hash"], "new_registry_hash": prepared["registry_hash"],
                     "by": by, "at": at, "reason": reason, "cause": cause,
                     "observed_before": prepared["parent_record_hash"], "reduction": reduction,
                     "approval_hash": approval_hash,
                     "assertion_changes": coverage.changes(protected, prepared["plan"])}
        prepared["amendment_hash"] = A._retain(store, _bytes(amendment))
        record = deepcopy(parent)
        record.update(close_id=close_id, instance_id=None, generation=0, status=close.OPEN,
                      revision=prepared["project"]["revision"], revision_clean=True, dirty_artifact=None,
                      opened_by=by, opened_at=at, epoch_at_open=store.current_epoch(),
                      final=None, draft=None, lens_acks={}, events=[], signoff_overrides={},
                      acceptance_route={"pending": True})
        # Preserve non-acceptance requirements, counters and remediation; fresh
        # acks and unchanged frozen specialist routes cannot silently waive them.
        old_partition_ids = {"acceptance-run-" + p["id"] for p in old_plan["partitions"]}
        record["required_lenses"] = [lens for lens in record["required_lenses"]
                                      if lens["id"] not in old_partition_ids
                                      and lens["id"] != "acceptance-cold"
                                      and not lens["id"].startswith("acceptance-repro-")]
        A.partition_lenses(prepared["plan"], record["required_lenses"])
        close._event(record, "acceptance:successor", by, at, parent_record_hash=prepared["parent_record_hash"],
                     amendment_hash=prepared["amendment_hash"])
        close.create_close(store, record)
        return A.freeze(store, close_id, prepared, at)


def evaluate(store, record, plan, snapshot, *, depth=0):
    """Check amendments without mutating parent, child or operator messages."""
    route = record["acceptance_route"]
    if route["parent_record_hash"] is None:
        return
    if depth >= coverage.MAX_LINKS:
        A._fail("acceptance ancestry exceeds supported depth")
    parent = A.decode(A._retained(store, route["parent_record_hash"]))
    amendment = A.decode(A._retained(store, route["amendment_hash"]))
    version = amendment.get("schema_version") if isinstance(amendment, dict) else None
    A._object(amendment, "schema_version parent_close_id parent_attempt_id successor_close_id prev_plan_hash "
              "new_plan_hash prev_registry_hash new_registry_hash by at reason cause observed_before "
              "reduction approval_hash" + (" assertion_changes" if A.schema(version).modern else ""), "amendment")
    A._version(version, (1, 2, 3))
    old_route, old_plan = A._policy(store, parent)
    old_bundle = A.decode(A._retained(store, old_route["bundle_hash"]))
    _, old_artifacts = A._bundle(old_bundle, parent, old_route, old_plan)
    for artifact in old_artifacts.values():
        A._retained(store, artifact["sha256"])
    inherited = {"holds": [], "outcomes": deepcopy(parent["final"]["acceptance_snapshot"]["outcomes"])}
    evaluate(store, parent, old_plan, inherited, depth=depth + 1)
    snapshot["holds"].extend(h for h in inherited["holds"] if h[0] in {
        "acceptance_scope_reduction_unapproved", "acceptance_category_moved_unreviewed",
        "acceptance_lens_not_independent"})
    if parent["revision"] == record["revision"]:
        snapshot["holds"].extend(h for h in inherited["holds"] if h[0] == "acceptance_plan_stale")
    if (parent["status"] != close.PUBLISHED or amendment["parent_close_id"] != parent["close_id"]
            or amendment["parent_attempt_id"] != old_route["attempt_id"]
            or amendment["successor_close_id"] != record["close_id"]
            or amendment["prev_plan_hash"] != old_route["plan_hash"]
            or amendment["new_plan_hash"] != route["plan_hash"]
            or amendment["prev_registry_hash"] != old_route["registry_hash"]
            or amendment["new_registry_hash"] != route["registry_hash"]
            or amendment["observed_before"] != route["parent_record_hash"]
            or old_plan["project_id"] != plan["project_id"]):
        A._fail("successor amendment binding differs", "acceptance_plan_stale")
    A._text(amendment["reason"], "amendment reason")
    if amendment["cause"] not in {"source-change", "policy-change"}:
        A._fail("unsupported amendment cause")
    snapshot["parent"] = {"close_id": parent["close_id"], "record_hash": route["parent_record_hash"],
                          "verdict": parent["final"]["verdict"]}
    if not set(old_plan["authors"]).issubset(plan["authors"]):
        snapshot["holds"].append(("acceptance_lens_not_independent", "successor removed declared authors"))
    partitions = {p["id"]: set(p["agents"]) for p in plan["partitions"]}
    if any(not set(p["agents"]).issubset(partitions.get(p["id"], set())) for p in old_plan["partitions"]):
        snapshot["holds"].append(("acceptance_lens_not_independent", "successor reduced declared runner set"))
    for cid in parent["counters"] if not A.schema(route["schema_version"]).cold else ():
        current = record["counters"].get(cid)
        if not isinstance(current, dict) or current.get("decision") == close.COUNTER_PENDING:
            snapshot["holds"].append(("acceptance_residual_open", f"parent counter {cid} remains unresolved"))
    protected = coverage.history(store, parent)
    changes = coverage.changes(protected, plan)
    reduction = amendment["reduction"]
    if A.schema(version).cold and _bytes(amendment["assertion_changes"]) != _bytes(changes):
        A._fail("retained target coverage differs from plans", "acceptance_category_moved_unreviewed")
    apply_coverage(store, record, plan, snapshot, parent, protected, reduction, amendment["approval_hash"])


def apply_coverage(store, record, plan, snapshot, parent, protected, reduction, approval_hash):
    """The same exact LD2 approval and outcome rules apply inside and across roots."""
    route = record["acceptance_route"]
    changes = coverage.changes(protected, plan)
    approved = False
    if changes or reduction is not None:
        try:
            if (reduction is None or set(_reduction(reduction)["rows"]) != set(changes)
                    or _bytes(reduction["changes"]) != _bytes(changes)):
                A._fail("operator must approve the exact protected-target coverage changes")
            if reduction["cause"] != "policy-amendment" and any(c["new"] for c in changes.values()):
                A._fail("scope reduction cannot authorize replacement gating assertions")
            _approval(store, A._retained(store, approval_hash), parent,
                      record["close_id"], route["plan_hash"], reduction)
            approved = True
        except (A.AcceptanceError, KeyError, TypeError, ValueError) as exc:
            snapshot["holds"].append(("acceptance_category_moved_unreviewed", str(exc)))
            if any(not change["new"] for change in changes.values()):
                snapshot["holds"].append(("acceptance_scope_reduction_unapproved", str(exc)))
            if parent["revision"] == record["revision"]:
                snapshot["holds"].append(("acceptance_plan_stale", "same-SHA coverage loss is unapproved"))
    snapshot.setdefault("coverage_changes", {})
    definitions = {r["id"]: r for r in plan["rows"]}
    for key, change in changes.items():
        sources = protected[key]["sources"]
        originals = [s["outcome"] for s in sources if s["outcome"]]
        original = next((o for o in originals if o.get("passed") is False), originals[0] if originals else {})
        missing = not change["new"]
        label = "reduced scope" if missing else "policy amended"
        snapshot["report_label"] = "reduced scope" if missing else snapshot.get("report_label", label)
        snapshot["coverage_changes"][key] = dict(change, approved=approved, report_label=label, sources=sources)
        for outcome in snapshot["outcomes"]:
            if coverage.target_id(definitions[outcome["id"]]) != key:
                continue
            if outcome["policy"] == "gating" and outcome["passed"] is False:
                snapshot["holds"].append(("acceptance_row_failed", f"amended row {outcome['id']} failed"))
            observed = outcome["passed"] if outcome["passed"] is not None else outcome.get("comparison_passed")
            outcome.update(original_outcome=original, comparison_passed=observed, passed=None,
                           disposition="scope-narrowed" if missing else "policy-amended", report_label=label)


def related_obligations(store, record, plan, bundle, snapshot):
    from agenttalk import acceptance_obligations
    acceptance_obligations.inherit(store, record, plan, bundle=bundle, snapshot=snapshot)
