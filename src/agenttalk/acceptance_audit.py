"""Read retained attempt provenance for cold independence and delivery checks."""

from datetime import datetime
import os
import subprocess  # nosec B404 - fixed Git argv lists; shell is never used

from agenttalk import acceptance as A, acceptance_coverage as coverage, acceptance_git, close, gates


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
    history = list(lineage(store, record))
    if record["acceptance_route"].get("obligations_hash"):
        from agenttalk import acceptance_obligations
        retained = acceptance_obligations.sources(store, record["acceptance_route"])
        withheld.update(digest for digest, _, _, _ in retained)
        history.extend((prior, route, plan) for _, prior, route, plan in retained)
    for depth, (attempt, route, plan) in enumerate(history):
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


def _git(project, *args, data=None):
    """Read real objects, with identical Git environment for ancestry and content."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    def read():
        return subprocess.run(["git", "-C", project["locator"], *args], capture_output=True,  # noqa: S603,S607  # nosec B603 B607
                              input=data, timeout=10, env=env)

    try:
        # These fixed, read-only probes have bounded output. Do not retain the
        # potentially large diff/patch input in the command cache.
        if args[0] in {"rev-parse", "merge-base"}:
            key = ("audit", project["locator"], args, tuple(sorted(env.items())))
            return acceptance_git.read_once(key, read)
        return read()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise A.AcceptanceError("acceptance_cold_missing", "source identity Git unavailable") from exc


def _complete_history(project, *revisions):
    if not all(isinstance(sha, str) and A._SHA.fullmatch(sha) for sha in revisions):
        A._fail("source ancestry requires verified commit identities", "acceptance_cold_missing")
    shallow = _git(project, "rev-parse", "--is-shallow-repository")
    if shallow.returncode or shallow.stdout.strip() != b"false":
        A._fail("source ancestry requires complete project history", "acceptance_cold_missing")


def related_revisions(project, earlier, later):
    """Only Git can establish ancestry; absence of evidence is not independence."""
    _complete_history(project, earlier, later)
    for first, second in ((earlier, later), (later, earlier)):
        result = _git(project, "merge-base", "--is-ancestor", first, second)
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            A._fail("source ancestry cannot be verified; restore the project's commit objects",
                    "acceptance_cold_missing")
    return False


def change_identity(project, plan):
    """Stable patch-id of the complete frozen base-to-candidate diff."""
    base = plan["cold_policy"]["change_base"]
    key = ("change", project["locator"], project["revision"], base, tuple(sorted(os.environ.items())))
    return acceptance_git.read_once(key, lambda: _change_identity(project, base))


def _change_identity(project, base):
    revision = project["revision"]
    _complete_history(project, base, revision)
    for sha in (base, revision):
        verified = _git(project, "rev-parse", "--verify", "--end-of-options", sha + "^{commit}")
        if verified.returncode or verified.stdout.strip() != sha.encode("ascii"):
            A._fail("change base/candidate commit cannot be verified", "acceptance_cold_missing")
    ancestor = _git(project, "merge-base", "--is-ancestor", base, revision)
    if ancestor.returncode:
        A._fail("change_base must be a verified ancestor of the candidate", "acceptance_cold_missing")
    diff = _git(project, "-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false",
                "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv",
                "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--line-prefix=", "--unified=3",
                "--submodule=short", "--ignore-submodules=none", base, revision, "--")
    if diff.returncode or not diff.stdout or len(diff.stdout) > A.MAX_TOTAL_BYTES:
        A._fail("whole change diff is unavailable, empty or exceeds the acceptance byte limit",
                "acceptance_cold_missing")
    result = _git(project, "-c", "patchid.verbatim=false", "patch-id", "--stable", data=diff.stdout)
    fields = result.stdout.split()
    if (result.returncode or len(fields) != 2 or
            any(len(field) != 40 or any(c not in b"0123456789abcdef" for c in field) for field in fields)):
        A._fail("whole change patch-id cannot be verified", "acceptance_cold_missing")
    return {"base": base, "revision": revision, "patch_id": fields[0].decode("ascii")}


def project_attempts(store, record):
    """Read project history once per audit, failing closed on unknown identities."""
    route = record["acceptance_route"]
    seen = set()
    try:
        close_ids = close.list_close_ids(store, strict=True)
    except OSError as exc:
        A._fail(f"acceptance audit unavailable at {close.closes_dir(store)}: {exc}; "
                "restore readable access to the complete closes directory and retry; "
                "do not replace unavailable history with an empty directory",
                "acceptance_audit_unavailable")
    for close_id in close_ids:
        if close_id == record["close_id"]:
            continue
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
            identity = prior_route["attempt_id"]
            if identity not in seen and prior_route["project_id"] == route["project_id"]:
                seen.add(identity)
                yield prior, prior_route, prior_plan


def same_change(record, plan, prior, prior_plan):
    """Labels only add to the shared ancestry/content identity rule."""
    project = record["acceptance_route"]["project"]
    if related_revisions(project, prior["revision"], record["revision"]):
        return True
    if {coverage.target_id(r) for r in plan["rows"]} & {coverage.target_id(r) for r in prior_plan["rows"]}:
        return True
    # Older HOLD-only plans cannot prove a disjoint whole-change boundary.
    if "cold_policy" not in prior_plan:
        A._fail("prior change boundary unavailable", "acceptance_cold_missing")
    return change_identity(project, plan)["patch_id"] == change_identity(
        dict(project, revision=prior["revision"]), prior_plan)["patch_id"]


def check_prior_exposure(store, record, plan, reviewer):
    """Exclude prior reveals for related source history or targets, across roots."""
    route = record["acceptance_route"]
    commits = [e for e in record["events"] if e.get("event") == "acceptance:cold-commit"]
    if len(commits) != 1:
        A._fail("cold commitment event missing or ambiguous", "acceptance_cold_missing")
    committed = _time(commits[0]["at"])
    current_change = change_identity(route["project"], plan)
    for prior, prior_route, prior_plan in project_attempts(store, record):
        if (prior_route["attempt_id"] == route["attempt_id"]
                or prior_route["project_id"] != route["project_id"]
                or not prior_route.get("cold_reconcile_hash")):
            continue
        if prior_plan["cold_policy"]["reviewer"] != reviewer:
            continue
        if same_change(record, plan, prior, prior_plan):
            reveals = [e for e in prior["events"] if e.get("event") == "acceptance:cold-reconcile"]
            if not reveals or any(_time(e["at"]) < committed for e in reveals):
                A._fail("reviewer was unblinded for this change, including another root",
                        "acceptance_cold_missing")
    return current_change
