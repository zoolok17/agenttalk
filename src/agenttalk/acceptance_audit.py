"""Read retained attempt provenance for cold independence and delivery checks."""

from datetime import datetime

from agenttalk import acceptance as A, acceptance_coverage as coverage, close


def lineage(store, record):
    for _ in range(coverage.MAX_LINKS + 1):
        route, plan = A._policy(store, record)
        yield record, route, plan
        digest = route.get("parent_record_hash")
        if digest is None:
            return
        record = A.decode(A._retained(store, digest))
    A._fail("cold provenance ancestry exceeds supported depth")


def actors(value):
    """Interpret attribution keys, not event names or a list of excluded roles."""
    found = set()
    if isinstance(value, list):
        for child in value:
            found.update(actors(child))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"by", "from", "actor", "owner"} or key.endswith("_by"):
                if isinstance(child, str) and child:
                    found.add(child)
            elif key in {"authors", "agents"} and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            elif isinstance(child, (dict, list)):
                found.update(actors(child))
    return found


def provenance(store, record, bundle=None):
    excluded, withheld = set(), set()
    for depth, (attempt, route, plan) in enumerate(lineage(store, record)):
        # Passive lens/roster assignments and post-commit acknowledgments do not
        # establish pre-sweep participation. Actions are sourced from the ledger.
        lifecycle = {k: v for k, v in attempt.items() if k not in
                     {"required_lenses", "required_signoffs", "lens_acks", "events", "draft", "final",
                      "counters", "signoff_overrides"}}
        excluded.update(actors(lifecycle))
        excluded.update(actors({"authors": plan["authors"], "partitions": plan["partitions"]}))
        for event in attempt["events"]:
            if depth == 0 and event.get("event") == "acceptance:cold-commit":
                break
            excluded.update(actors(event))
        for key, digest in route.items():
            if key.endswith("_hash") and digest is not None:
                withheld.add(digest)
        if route.get("amendment_hash"):
            excluded.update(actors(A.decode(A._retained(store, route["amendment_hash"]))))
        execution = bundle if depth == 0 and bundle is not None else (
            A.decode(A._retained(store, route["bundle_hash"])) if route["bundle_hash"] else None)
        if execution:
            excluded.update(actors({"runs": execution["runs"], "reproductions": execution.get("reproductions", [])}))
            withheld.update(item["sha256"] for item in execution["artifacts"])
    return excluded, withheld


def check_delivery(store, record, delivered):
    _, withheld = provenance(store, record)
    if any(item["sha256"] in withheld for item in delivered):
        A._fail("cold delivery contains withheld acceptance evidence under a different label",
                "acceptance_cold_missing")


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        A._fail("cold audit timestamp lacks timezone")
    return result


def check_prior_exposure(store, record, plan, reviewer):
    """Exclude prior reveals for this source or overlapping targets, across roots."""
    route = record["acceptance_route"]
    commits = [e for e in record["events"] if e.get("event") == "acceptance:cold-commit"]
    if len(commits) != 1:
        A._fail("cold commitment event missing or ambiguous", "acceptance_cold_missing")
    committed = _time(commits[0]["at"])
    targets = {coverage.target_id(row) for row in plan["rows"]}
    for close_id in close.list_close_ids(store):
        try:
            candidate = close.load_close(store, close_id)
            if "acceptance_route" not in candidate:
                continue
            for prior, prior_route, prior_plan in lineage(store, candidate):
                if (prior_route["attempt_id"] == route["attempt_id"]
                        or prior_route["project_id"] != route["project_id"]
                        or not prior_route.get("cold_reconcile_hash")):
                    continue
                same_change = (prior["revision"] == record["revision"] or
                               bool(targets & {coverage.target_id(row) for row in prior_plan["rows"]}))
                if same_change and prior_plan["cold_policy"]["reviewer"] == reviewer:
                    reveals = [e for e in prior["events"] if e.get("event") == "acceptance:cold-reconcile"]
                    if not reveals or any(_time(e["at"]) < committed for e in reveals):
                        A._fail("reviewer was unblinded for this change, including another root",
                                "acceptance_cold_missing")
        except (close.CloseError, OSError, ValueError, TypeError, KeyError) as exc:
            A._fail(f"cold exposure audit unavailable for {close_id}: {exc}", "acceptance_cold_missing")
