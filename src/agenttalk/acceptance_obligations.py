"""One inheritance fold for substantive change obligations and their evidence.

The retained source records are immutable. Reviewed dispositions live in ordinary
counter records on the new attempt; raw integrity and LD2 coverage are never
waived through that disposition channel.
"""

from copy import deepcopy

from agenttalk import acceptance as A, acceptance_audit as audit, acceptance_coverage as coverage, close, gates


# These obligations have dedicated current-evidence resolvers. All other saved
# holds default to a reviewed counter, including future/unknown hold codes.
RECOMPUTED = frozenset({
    "acceptance_row_failed", "acceptance_category_moved_unreviewed",
    "acceptance_scope_reduction_unapproved", "acceptance_plan_stale",
    "acceptance_residual_open", "undecided_counter", "accepted_counter_missing_remediation",
    "open_blocker_remediation", "gate_hold",
    "acceptance_cold_missing", "acceptance_lens_not_independent",
    "acceptance_trust_unresolved", "missing_lens", "stale_lens_ack", "unauthorized_lens_ack",
})


def sources(store, route):
    """Read the bounded source-record graph, including previously recovered roots."""
    pending = list(A.decode(A._retained(store, route["obligations_hash"])))
    seen = set()
    result = []
    while pending:
        digest = pending.pop(0)
        A._digest(digest)
        if digest in seen:
            continue
        if len(seen) >= 256:
            A._fail("recovery evidence exceeds 256 source records", "acceptance_record_missing")
        seen.add(digest)
        prior = A.decode(A._retained(store, digest))
        if not close._is_wellformed(prior):
            A._fail("invalid retained recovery close", "acceptance_record_missing")
        prior_route, prior_plan = A._policy(store, prior)
        result.append((digest, prior, prior_route, prior_plan))
        if prior_route.get("parent_record_hash"):
            pending.append(prior_route["parent_record_hash"])
        if prior_route.get("obligations_hash"):
            pending.extend(A.decode(A._retained(store, prior_route["obligations_hash"])))
    return sorted(result, key=lambda s: (s[1]["opened_at"], s[1].get("generation", 0), s[0]))


def evidence(store, route):
    """Re-read source bytes, never trust a cached outcome as integrity evidence."""
    digests = {route["obligations_hash"]}
    for digest, prior, old_route, plan in sources(store, route):
        digests.add(digest)
        digests.update(v for k, v in old_route.items() if k.endswith("_hash") and v)
        if old_route.get("bundle_hash"):
            bundle = A.decode(A._retained(store, old_route["bundle_hash"]))
            rows, artifacts = A._bundle(bundle, prior, old_route, plan)
            digests.update(a["sha256"] for a in artifacts.values())
            for row in plan["rows"]:
                raw = A.decode(A._retained(store, artifacts[row["artifact"]]["sha256"]))
                A.validate_raw(raw, prior["revision"], rows[row["id"]]["run_id"])
                if row["field"] not in raw["values"]:
                    A._fail("inherited observed field missing", "acceptance_record_missing")
            for reproduction in bundle.get("reproductions", []):
                for row in reproduction["rows"]:
                    raw = A.decode(A._retained(store, artifacts[row["artifact"]]["sha256"]))
                    A.validate_raw(raw, prior["revision"], reproduction["id"])
            for approval in bundle.get("recovery_approvals", []):
                digests.update(approval["reduction"]["evidence"])
        if old_route.get("cold_commit_hash"):
            from agenttalk import acceptance_cold
            initial = A.decode(A._retained(store, old_route["cold_commit_hash"]))
            observations = acceptance_cold.validate_initial(initial, prior, plan)
            digests.update(a["sha256"] for a in initial["delivery_manifest"])
            if old_route.get("cold_reconcile_hash"):
                reconciliation = A.decode(A._retained(store, old_route["cold_reconcile_hash"]))
                acceptance_cold.validate_reconciliation(reconciliation, old_route, observations)
        if old_route.get("amendment_hash"):
            amendment = A.decode(A._retained(store, old_route["amendment_hash"]))
            if amendment.get("approval_hash"):
                digests.add(amendment["approval_hash"])
            if amendment.get("reduction"):
                digests.update(amendment["reduction"]["evidence"])
    for digest in sorted(digests):
        A._retained(store, digest)
    return sorted(digests)


def _counter(prior, kind, identity, finding, actor):
    origin = {"attempt_id": prior["acceptance_route"]["attempt_id"], "kind": kind, "id": identity}
    cid = "ob-" + A._hash(coverage.canonical(origin))[:32]
    return {"counter_id": cid, "lens": "acceptance-recovery", "raised_by": actor,
            "at": prior["opened_at"], "decision": close.COUNTER_PENDING,
            "remediation_id": None, "finding": finding, "obligation_source": origin}


def _review_obligations(store, prior, route):
    """Normalize every review channel into the existing reviewed-counter model."""
    result = []
    for cid, value in prior["counters"].items():
        counter = deepcopy(value)
        if "obligation_source" not in counter:
            normalized = _counter(prior, "counter", cid, counter["finding"], counter["raised_by"])
            counter.update(counter_id=normalized["counter_id"], obligation_source=normalized["obligation_source"])
        result.append(counter)
    if route.get("cold_commit_hash"):
        initial = A.decode(A._retained(store, route["cold_commit_hash"]))
        findings = {}
        if route.get("cold_reconcile_hash"):
            reconciliation = A.decode(A._retained(store, route["cold_reconcile_hash"]))
            findings = {f["id"]: f for f in reconciliation["findings"]}
        for observation in initial["observations"]:
            if observation["blocking"] and findings.get(observation["id"], {}).get("disposition") != "resolved":
                result.append(_counter(prior, "cold", observation["id"], observation["evidence"], initial["reviewer"]))
    if route.get("bundle_hash") and route["schema_version"] == 3:
        from agenttalk import acceptance_hygiene
        try:
            acceptance_hygiene.execution(store, A.decode(A._retained(store, route["bundle_hash"])))
        except A.AcceptanceError as exc:
            result.append(_counter(prior, "execution", exc.code + ":" + str(exc), str(exc), route["attached_by"]))
    final = prior.get("final") or {}
    for hold in final.get("close_result", {}).get("holds", []):
        code, detail = hold["code"], hold["detail"]
        if code not in RECOMPUTED or (code == "acceptance_residual_open" and not result):
            result.append(_counter(prior, "hold", code + ":" + detail, detail, final["by"]))
    return result


def inherit(store, record, plan, *, capture=False, bundle=None, snapshot=None):
    """Capture, carry and evaluate all recovery obligations through one fold."""
    route = record["acceptance_route"]
    if capture:
        retained = []
        for prior, _old_route, old_plan in audit.project_attempts(store, record):
            if (audit._time(prior["opened_at"]) < audit._time(record["opened_at"])
                    and audit.same_change(record, plan, prior, old_plan)):
                retained.append(A._retain(store, coverage.canonical(prior)))
        route["obligations_hash"] = A._retain(store, coverage.canonical(sorted(set(retained))))
    entries = sources(store, route)
    counters = {}
    remediation = {}
    remediation_sources = {}
    requirements = {}
    for _, prior, old_route, old_plan in entries:
        for counter in _review_obligations(store, prior, old_route):
            counters[counter["counter_id"]] = counter
            rid = counter.get("remediation_id")
            if rid and rid in prior["remediation_items"]:
                if (rid in remediation and remediation[rid] != prior["remediation_items"][rid]
                        and remediation_sources[rid] != counter["counter_id"]):
                    A._fail("conflicting inherited remediation ids", "acceptance_residual_open")
                remediation[rid] = prior["remediation_items"][rid]
                remediation_sources[rid] = counter["counter_id"]
        derived = {"acceptance-cold"} | {"acceptance-run-" + p["id"] for p in old_plan["partitions"]}
        if old_route.get("bundle_hash"):
            old_bundle = A.decode(A._retained(store, old_route["bundle_hash"]))
            derived.update("acceptance-repro-" + rep["id"] for rep in old_bundle.get("reproductions", []))
        for lens in prior["required_lenses"]:
            if lens["id"] not in derived and lens.get("required", True):
                if lens["id"] in requirements and requirements[lens["id"]] != lens:
                    A._fail("conflicting inherited review requirements", "acceptance_residual_open")
                requirements[lens["id"]] = lens
    if capture:
        for cid, counter in counters.items():
            origin = counter["obligation_source"]
            native = record["counters"].get(origin["id"])
            if origin["kind"] == "counter" and native and native.get("finding") == counter["finding"]:
                # A linked successor already cloned this native counter. Move it
                # to the same stable source identity used by an unlinked root.
                record["counters"].pop(origin["id"])
            record["counters"].setdefault(cid, deepcopy(counter))
        for rid, item in remediation.items():
            record["remediation_items"].setdefault(rid, deepcopy(item))
        existing = {lens["id"] for lens in record["required_lenses"]}
        record["required_lenses"].extend(deepcopy(lens) for lid, lens in requirements.items() if lid not in existing)
        return

    latest, verified = {}, {}
    repo = route["project"]["locator"]
    for _, prior, old_route, _ in entries:
        latest[old_route["attempt_id"]] = prior
        revision = prior["revision"]
        if revision not in verified:
            verified[revision] = A.verify_project(repo, revision, live=False)
        project = verified[revision]
        expected = dict(old_route["project"], locator=repo)
        if project != expected or A.project_id(project) != old_route["project_id"]:
            A._fail("inherited source project identity changed", "acceptance_project_unverified")
    for prior in latest.values():
        if prior["status"] != close.PUBLISHED or not isinstance(prior.get("final"), dict):
            snapshot["holds"].append(("acceptance_plan_stale",
                f"source close {prior['close_id']} is unfinished; publish its HOLD and create a fresh attempt"))

    frozen = {digest for digest, _, _, _ in entries}
    for prior, _, old_plan in audit.project_attempts(store, record):
        if (audit._time(prior["opened_at"]) < audit._time(record["opened_at"])
                and A._hash(coverage.canonical(prior)) not in frozen
                and audit.same_change(record, plan, prior, old_plan)):
            A._fail("related attempt changed after recovery freeze; create a fresh attempt to retain its obligations",
                    "acceptance_plan_stale")

    # This check runs before cold review and the final hygiene fold. Missing
    # source bytes cannot be excused by a current passing measurement/decision.
    manifest = evidence(store, route)
    snapshot["inherited_evidence"] = manifest
    for cid, original in counters.items():
        current = record["counters"].get(cid)
        immutable = ("counter_id", "finding", "raised_by", "obligation_source")
        if (not isinstance(current, dict) or any(current.get(k) != original.get(k) for k in immutable)
                or current.get("decision") not in {close.COUNTER_ACCEPTED, close.COUNTER_REJECTED}):
            snapshot["holds"].append(("acceptance_residual_open",
                                      f"inherited counter {cid} needs reviewed disposition"))
        elif not all(current.get(k) for k in ("decided_by", "decided_at", "decision_reason")):
            snapshot["holds"].append(("acceptance_residual_open", f"inherited counter {cid} lacks decision evidence"))
        elif any(current.get(k) != original.get(k) for k in
                 ("decision", "decided_by", "decided_at", "decision_reason", "remediation_id")):
            if not any(e.get("event") == "counter:" + current["decision"] and e.get("counter_id") == cid
                       and e.get("by") == current["decided_by"] and e.get("at") == current["decided_at"]
                       for e in record["events"]):
                snapshot["holds"].append(("acceptance_residual_open",
                                          f"inherited counter {cid} lacks a decision event"))
    lenses = {lens["id"]: lens for lens in record["required_lenses"]}
    for lid, lens in requirements.items():
        if lenses.get(lid) != lens:
            snapshot["holds"].append(("acceptance_residual_open", f"inherited review requirement {lid} changed"))
    for _, prior, _, _ in entries:
        green = close._green_gate_names(gates.check_gates(store.root, scope=prior["gate_scope"]))
        for name in (prior.get("final") or {}).get("blockers", []):
            if name not in green:
                snapshot["holds"].append(("acceptance_residual_open", f"inherited blocker gate {name} is unresolved"))
        for key in ("scope", "gate_scope"):
            if record[key] != prior[key]:
                snapshot["holds"].append(("acceptance_residual_open", f"inherited {key} changed"))
        for key in ("required_signoffs", "risk_inventory"):
            if prior.get(key) and any(item not in (record.get(key) or []) for item in prior[key]):
                snapshot["holds"].append(("acceptance_residual_open", f"inherited {key} must be restored"))

    from agenttalk import acceptance_history
    lineage_ids = {r["attempt_id"] for _, r, _ in audit.lineage(store, record)}
    approvals = {a["prior_attempt_id"]: a for a in bundle["recovery_approvals"]}
    artifacts = {a["id"]: a for a in bundle["artifacts"]}
    consumed, seen = set(), set()
    snapshot["related_obligations"] = []
    for digest, prior, old_route, _ in reversed(entries):
        identity = old_route["attempt_id"]
        if identity in lineage_ids or identity in seen:
            continue
        seen.add(identity)
        protected = coverage.history(store, prior)
        entry = approvals.get(identity)
        reduction = entry["reduction"] if entry else None
        approval_hash = artifacts[entry["approval_artifact"]]["sha256"] if entry else None
        if entry:
            consumed.add(identity)
        snapshot["related_obligations"].append({"close_id": prior["close_id"], "attempt_id": identity,
                                                "record_hash": digest, "protected": protected})
        acceptance_history.apply_coverage(store, record, plan, snapshot, prior, protected, reduction, approval_hash)
    if set(approvals) != consumed:
        A._fail("recovery approval references no prior related attempt", "acceptance_category_moved_unreviewed")
