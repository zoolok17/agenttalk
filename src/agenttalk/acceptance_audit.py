"""Read retained attempt provenance for cold independence and delivery checks."""

from datetime import datetime
import os
import subprocess

from agenttalk import acceptance as A, acceptance_coverage as coverage, close, gates


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
    gate_state = gates.load_gate_state(store.root)
    if gate_state.get("load_error"):
        A._fail("cold gate attribution unavailable; repair .agenttalk/gates.json", "acceptance_cold_missing")
    commits = [e for e in record["events"] if e.get("event") == "acceptance:cold-commit"]
    cutoff = _time(commits[0]["at"]) if commits else None
    for depth, (attempt, route, plan) in enumerate(lineage(store, record)):
        # Passive lens/roster assignments and post-commit acknowledgments do not
        # establish pre-sweep participation. Actions are sourced from the ledger.
        lifecycle = {k: v for k, v in attempt.items() if k not in
                     {"required_lenses", "required_signoffs", "lens_acks", "events", "draft", "final",
                      "counters", "signoff_overrides"}}
        excluded.update(actors(lifecycle))
        named_gates = {item.get("gate") for item in attempt["remediation_items"].values()}
        for name, gate in gate_state["gates"].items():
            if gate.get("scope") not in (attempt["gate_scope"], "global") and name not in named_gates:
                continue
            # Latest attribution and retained evidence may have different actors.
            # Later gate activity must not retroactively taint a committed sweep.
            if cutoff is None or not gate.get("updated_at") or _time(gate["updated_at"]) <= cutoff:
                excluded.update(actors({k: v for k, v in gate.items() if k != "evidence"}))
            for evidence in gate.get("evidence", []):
                if cutoff is None or not evidence.get("at") or _time(evidence["at"]) <= cutoff:
                    excluded.update(actors(evidence))
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


def related_revisions(project, earlier, later):
    """Only Git can establish ancestry; absence of evidence is not independence."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"

    def git(*args):
        try:
            return subprocess.run(["git", "-C", project["locator"], *args], capture_output=True,
                                  text=True, timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise A.AcceptanceError("acceptance_cold_missing", "source ancestry Git unavailable") from exc

    if not all(isinstance(sha, str) and A._SHA.fullmatch(sha) for sha in (earlier, later)):
        A._fail("source ancestry requires verified commit identities", "acceptance_cold_missing")
    shallow = git("rev-parse", "--is-shallow-repository")
    if shallow.returncode or shallow.stdout.strip() != "false":
        A._fail("source ancestry requires complete project history", "acceptance_cold_missing")
    for first, second in ((earlier, later), (later, earlier)):
        result = git("merge-base", "--is-ancestor", first, second)
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            A._fail("source ancestry cannot be verified; restore the project's commit objects",
                    "acceptance_cold_missing")
    return False


def check_prior_exposure(store, record, plan, reviewer):
    """Exclude prior reveals for related source history or targets, across roots."""
    route = record["acceptance_route"]
    commits = [e for e in record["events"] if e.get("event") == "acceptance:cold-commit"]
    if len(commits) != 1:
        A._fail("cold commitment event missing or ambiguous", "acceptance_cold_missing")
    committed = _time(commits[0]["at"])
    targets = {coverage.target_id(row) for row in plan["rows"]}
    for close_id in close.list_close_ids(store):
        try:
            # Read identity before traversing policy/artifacts of another project.
            raw = A.decode(A._read(close.close_path(store, close_id)))
            raw_route = raw.get("acceptance_route") if isinstance(raw, dict) else None
            identity = raw_route.get("project_id") if isinstance(raw_route, dict) else None
            if isinstance(identity, str) and identity and identity != route["project_id"]:
                continue
            candidate = close.load_close(store, close_id)
            if "acceptance_route" not in candidate:
                continue
            history = list(lineage(store, candidate))
        except (close.CloseError, OSError, ValueError, TypeError, KeyError) as exc:
            A._fail(f"cold exposure audit unavailable for close {close_id!r} at {close.close_path(store, close_id)}: "
                    f"{exc}; restore this close and its retained blobs from a trusted backup, or quarantine "
                    "the unreadable close file outside .agenttalk/closes after preserving it for operator review "
                    "(quarantine removes its exposure evidence)", "acceptance_cold_missing")
        for prior, prior_route, prior_plan in history:
            if (prior_route["attempt_id"] == route["attempt_id"]
                    or prior_route["project_id"] != route["project_id"]
                    or not prior_route.get("cold_reconcile_hash")):
                continue
            if prior_plan["cold_policy"]["reviewer"] != reviewer:
                continue
            same_change = (related_revisions(route["project"], prior["revision"], record["revision"]) or
                           bool(targets & {coverage.target_id(row) for row in prior_plan["rows"]}))
            if same_change:
                reveals = [e for e in prior["events"] if e.get("event") == "acceptance:cold-reconcile"]
                if not reveals or any(_time(e["at"]) < committed for e in reveals):
                    A._fail("reviewer was unblinded for this change, including another root",
                            "acceptance_cold_missing")
