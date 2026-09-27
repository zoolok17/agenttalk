"""Cooperative final cold observations, ordered reveal and actor attestations."""

from pathlib import Path

from agenttalk import acceptance as A, close
from agenttalk import acceptance_audit as audit


def policy(value):
    A._object(value, "reviewer roster absence_disclosure change_base", "cold policy")
    if not isinstance(value["change_base"], str) or not A._SHA.fullmatch(value["change_base"]):
        A._fail("cold policy change_base must be a full commit SHA")
    A._text(value["reviewer"], "cold reviewer")
    roster = {}
    for entry in A._items(value["roster"], "available vendor roster", nonempty=True):
        A._object(entry, "actor vendor", "vendor assignment")
        for key in ("actor", "vendor"):
            A._text(entry[key], key)
        if entry["actor"] in roster:
            A._fail("duplicate roster actor")
        roster[entry["actor"]] = entry["vendor"]
    if value["reviewer"] not in roster or not isinstance(value["absence_disclosure"], str):
        A._fail("cold policy lacks reviewer vendor or disclosure field")
    return roster


def binding(record):
    route = record["acceptance_route"]
    return {key: route[key] for key in
            ("instance_id", "attempt_id", "revision", "project_id", "plan_hash", "registry_hash", "obligations_hash")}


def validate_initial(value, record, plan):
    A._object(value, "schema_version binding reviewer context_id claims_exposed authored_in_scope "
              "prior_exposure leakage_reviewed access_id access_evidence delivery_manifest observations blind_spots",
              "initial cold report")
    A._version(value["schema_version"])
    if value["binding"] != binding(record) or value["reviewer"] != plan["cold_policy"]["reviewer"]:
        A._fail("cold report belongs to another assignment/attempt", "acceptance_cold_missing")
    A._text(value["context_id"], "cold context identity")
    if (value["claims_exposed"] is not False or value["authored_in_scope"] is not False
            or value["prior_exposure"] is not False or value["leakage_reviewed"] is not True):
        A._fail("final cold reviewer authored scope or read author claims", "acceptance_cold_missing")
    delivered = A._items(value["delivery_manifest"], "cold delivery", nonempty=True)
    kinds = set()
    for item in delivered:
        A._object(item, "kind path sha256", "cold delivered resource")
        if item["kind"] not in {"source", "safety", "toolchain", "access"}:
            A._fail("cold delivery must withhold plan, expectations and author claims", "acceptance_cold_missing")
        kinds.add(item["kind"])
        A._text(item["path"], "cold delivery path")
        A._digest(item["sha256"])
    A._text(value["access_id"], "cold access identity")
    A._digest(value["access_evidence"])
    if "source" not in kinds or not any(item["kind"] == "access" and item["sha256"] == value["access_evidence"]
                                        for item in delivered):
        A._fail("cold delivery lacks source or separate-access evidence", "acceptance_cold_missing")
    A._strings(value["blind_spots"], "blind spots")
    observations = A._indexed(value["observations"], "cold observations")
    for observation in observations.values():
        A._object(observation, "id blocking evidence", "cold observation")
        if type(observation["blocking"]) is not bool:
            A._fail("cold blocking must be boolean")
        A._text(observation["evidence"], "cold observation evidence")
    return observations


def validate_reconciliation(value, route, observations):
    A._object(value, "schema_version commit_hash bundle_hash revealed findings closeout", "cold reconciliation")
    A._version(value["schema_version"])
    if (value["commit_hash"] != route["cold_commit_hash"] or value["bundle_hash"] != route["bundle_hash"]
            or value["revealed"] is not True):
        A._fail("reconciliation must bind committed report and revealed bundle", "acceptance_cold_missing")
    findings = A._indexed(value["findings"], "reconciled findings")
    if set(findings) != set(observations):
        A._fail("reconciliation must disposition every cold observation", "acceptance_cold_missing")
    for finding in findings.values():
        A._object(finding, "id disposition evidence", "reconciled finding")
        if finding["disposition"] not in {"open", "resolved"}:
            A._fail("unsupported cold finding disposition")
        A._text(finding["evidence"], "reconciliation evidence")
    return findings


def submit(store, close_id, report_file, *, phase, by, at):
    path = Path(report_file).absolute()
    data = A._read(A._path(path.parent, path.name))
    value = A.decode(data)
    with close.close_transaction(store, close_id) as tx:
        route, plan = A._policy(store, tx.record)
        if not A.schema(route["schema_version"]).cold or tx.record["status"] == close.PUBLISHED:
            A._fail("cold submission requires an open schema-3 attempt")
        if by != plan["cold_policy"]["reviewer"]:
            A._fail("only assigned reviewer may commit/reconcile", "acceptance_lens_not_independent")
        key = "cold_" + phase + "_hash"
        if route[key] is not None:
            A._fail("cold phase is immutable")
        if phase == "commit":
            if route["bundle_hash"] is not None:
                A._fail("initial cold observations must precede bundle attachment", "acceptance_cold_missing")
            validate_initial(value, tx.record, plan)
            audit.check_delivery(store, tx.record, value["delivery_manifest"])
            total = len(data)
            for item in value["delivery_manifest"]:
                resource = A._read(A._path(path.parent, item["path"]))
                total += len(resource)
                if total > A.MAX_TOTAL_BYTES or A._hash(resource) != item["sha256"]:
                    A._fail("cold delivered bytes exceed limit or differ from digest", "acceptance_record_missing")
                A._retain(store, resource)
        else:
            if route["cold_commit_hash"] is None or route["bundle_hash"] is None:
                A._fail("reconciliation requires initial commitment and attachment", "acceptance_cold_missing")
            initial = A.decode(A._retained(store, route["cold_commit_hash"]))
            validate_reconciliation(value, route, validate_initial(initial, tx.record, plan))
        route[key] = A._retain(store, data)
        close._event(tx.record, "acceptance:cold-" + phase, by, at, report_hash=route[key])
        tx.commit()
    return route[key]


def bound_accept(record, lens, actor):
    ack = record["lens_acks"].get(lens, {})
    return (ack.get("from") == actor and ack.get("status") == close.ACCEPT and not ack.get("override")
            and ack.get("acceptance_binding") == A.ack_binding(record))


def reproduction_lenses(record, bundle):
    for rep in bundle["reproductions"]:
        lens = close.validate_lens_spec({"id": "acceptance-repro-" + rep["id"], "allowed_agents": [rep["actor"]]})
        if any(item["id"] == lens["id"] for item in record["required_lenses"]):
            A._fail("reproduction lens conflicts with an existing lens")
        record["required_lenses"].append(lens)


def evaluate(store, record, plan, bundle, snapshot):
    route = record["acceptance_route"]
    holds = snapshot["holds"]
    for rep in bundle["reproductions"]:
        if not bound_accept(record, "acceptance-repro-" + rep["id"], rep["actor"]):
            holds.append(("acceptance_lens_not_independent", "reproducer must attest its own bound evidence"))
    if route["cold_commit_hash"] is None or route["cold_reconcile_hash"] is None:
        A._fail("final cold commitment/reconciliation missing", "acceptance_cold_missing")
    initial = A.decode(A._retained(store, route["cold_commit_hash"]))
    observations = validate_initial(initial, record, plan)
    audit.check_delivery(store, record, initial["delivery_manifest"])
    total = 0
    for item in initial["delivery_manifest"]:
        resource = A._retained(store, item["sha256"])
        total += len(resource)
        if total > A.MAX_TOTAL_BYTES or not resource.strip():
            A._fail("cold delivery is empty or exceeds retained limit", "acceptance_record_missing")
    reconciliation = A.decode(A._retained(store, route["cold_reconcile_hash"]))
    findings = validate_reconciliation(reconciliation, route, observations)
    reviewer = plan["cold_policy"]["reviewer"]
    change = audit.check_prior_exposure(store, record, plan, reviewer)
    excluded, _ = audit.provenance(store, record, bundle)
    if reviewer in excluded:
        A._fail("final cold actor is not independent", "acceptance_lens_not_independent")
    access_ids = {bundle["verifier_access"]["id"]} | {r["access_id"] for r in bundle["runs"]}
    access_ids.update(r["access_id"] for r in bundle["reproductions"])
    if initial["access_id"] in access_ids:
        A._fail("final cold access is shared with execution/verifier", "acceptance_lens_not_independent")
    vendors = policy(plan["cold_policy"])
    participants = audit.actors({"authors": plan["authors"], "partitions": plan["partitions"],
                                 "runs": bundle["runs"], "reproductions": bundle["reproductions"],
                                 "attached_by": route["attached_by"]}) | {reviewer}
    if not participants.issubset(vendors):
        A._fail("available-vendor snapshot omits a participant", "acceptance_lens_not_independent")
    diverse = len(set(vendors.values())) > 1
    original_actors = set(plan["authors"]) | {run["actor"] for run in bundle["runs"]}
    if diverse:
        original_vendors = {vendors[actor] for actor in original_actors}
        if vendors[reviewer] in original_vendors:
            A._fail("available second vendor required for final cold review", "acceptance_lens_not_independent")
        runs = {run["id"]: run for run in bundle["runs"]}
        if any(vendors[rep["actor"]] == vendors[runs[rep["source_run"]]["actor"]]
               for rep in bundle["reproductions"]):
            A._fail("available second vendor required for reproduction", "acceptance_lens_not_independent")
    elif not plan["cold_policy"]["absence_disclosure"].strip():
        A._fail("second-vendor absence must be disclosed", "acceptance_lens_not_independent")
    events = record["events"]
    expected = [("acceptance:cold-commit", reviewer, "report_hash", route["cold_commit_hash"]),
                ("acceptance:attach", route["attached_by"], "bundle_hash", route["bundle_hash"]),
                ("acceptance:cold-reconcile", reviewer, "report_hash", route["cold_reconcile_hash"])]
    positions = []
    for action, actor, key, digest in expected:
        matches = [i for i, e in enumerate(events) if e.get("event") == action
                   and e.get("by") == actor and e.get(key) == digest]
        if len(matches) != 1:
            A._fail("cold phase commitment event missing or ambiguous", "acceptance_cold_missing")
        positions.append(matches[0])
    if positions != sorted(set(positions)):
        A._fail("cold commitment/reveal order invalid", "acceptance_cold_missing")
    if not bound_accept(record, "acceptance-cold", reviewer):
        A._fail("final reviewer must accept bound reconciliation", "acceptance_cold_missing")
    for key, observation in observations.items():
        if observation["blocking"] and findings[key]["disposition"] != "resolved":
            holds.append(("acceptance_residual_open", f"cold finding {key} remains open"))
    snapshot["cold_checked"] = True
    snapshot["cold"] = {"reviewer": reviewer, "context_id": initial["context_id"],
                        "change_identity": change,
                        "commit_hash": route["cold_commit_hash"], "reconcile_hash": route["cold_reconcile_hash"],
                        "absence_disclosure": plan["cold_policy"]["absence_disclosure"], "findings": findings}
