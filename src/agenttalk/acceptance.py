"""Acceptance evidence resolver and additive pure verdict checks.

The resolver performs I/O; evaluate() only consumes its validated snapshot.
Schema 3 adds a committed final cold sweep and cooperative actor attestations.
Older schemas remain HOLD-only; no cryptographic execution provenance is claimed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess  # nosec B404 - fixed Git argv lists; shell is never used
import uuid

from agenttalk import acceptance_git, close

MAX_BYTES = 1024 * 1024
MAX_ITEMS = 256
MAX_TOTAL_BYTES = 16 * MAX_BYTES
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
COMPARATORS = frozenset({"exit-code", "exact-value", "exact-failure-set"})


class AcceptanceError(close.CloseError):
    """A refused input or a stable evidence HOLD."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _fail(detail, code="acceptance_policy_invalid"):
    raise AcceptanceError(code, detail)


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        _fail(f"{label}: expected fields {keys}")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        _fail(f"{label}: expected nonempty bounded text")
    return value


def _id(value, label="id"):
    try:
        return close.validate_close_id(value)
    except close.CloseError:
        pass
    # IDs can be hostile or private. Do not echo them, including in a chain.
    raise AcceptanceError("acceptance_policy_invalid",
                          f"{label}: alphanumerics plus . _ -; at most 64 characters; "
                          "start with alphanumeric") from None


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("expected lowercase SHA-256 digest")
    return value


def _version(value, supported=(1,)):
    if type(value) is not int or value not in supported:
        _fail("unsupported acceptance schema_version")


def _items(value, label, *, nonempty=False):
    if not isinstance(value, list) or len(value) > MAX_ITEMS or (nonempty and not value):
        _fail(f"{label}: expected bounded list")
    return value


def _strings(value, label, *, nonempty=False):
    values = _items(value, label, nonempty=nonempty)
    for item in values:
        _text(item, label)
    if len(set(values)) != len(values):
        _fail(f"{label}: duplicates")
    return values


def _indexed(values, label):
    result = {}
    for value in _items(values, label):
        if not isinstance(value, dict):
            _fail(f"{label}: expected objects")
        key = _id(value.get("id"), f"{label}[{len(result)}].id")
        if key in result:
            _fail(f"{label}: duplicate id")
        result[key] = value
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def decode(data):
    """Strict, size-bounded JSON, including nested duplicate/non-finite rejection."""
    if len(data) > MAX_BYTES:
        _fail("acceptance input exceeds byte limit")
    def finite_number(text):
        value = float(text)
        if not math.isfinite(value):
            _fail("non-finite JSON number")
        return value

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_float=finite_number,
                          parse_constant=lambda _: _fail("non-finite JSON number"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AcceptanceError("acceptance_policy_invalid", "invalid acceptance JSON") from exc


def _path(root, relative):
    """Resolve portable relative paths without following links or junctions."""
    _text(relative, "relative path")
    parts = PurePosixPath(relative).parts
    if (not parts or PurePosixPath(relative).is_absolute() or "\\" in relative
            or ":" in relative or any(p in {".", ".."} for p in relative.split("/"))):
        _fail("artifact path must stay beneath its declared root")
    root = Path(root).absolute()
    path = root
    for part in (None, *parts):
        if part is not None:
            path = path / part
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 1024):
            _fail("links/reparse points are not acceptance artifacts")
    if not path.resolve().is_relative_to(root.resolve()):
        _fail("artifact path escaped its root")
    return path


def _read(path):
    if not stat.S_ISREG(Path(path).stat().st_mode):
        _fail("acceptance evidence must be a regular file")
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        _fail("acceptance input exceeds byte limit")
    return data


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _retain(store, data):
    """Publish complete bytes exclusively; interrupted writes never occupy a digest."""
    digest = _hash(data)
    path = _path(store.dir, f"acceptance/sha256/{digest}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".pending-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            # Digest identity makes complete replacement idempotent on filesystems
            # without hard links. Keep the existing-corruption refusal when visible.
            if path.exists() and _read(path) != data:
                _fail(f"retained bytes differ from their digest at {path}; quarantine this blob before retrying",
                      "acceptance_record_missing")
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if _read(path) != data:
        _fail(f"retained bytes differ from their digest at {path}; quarantine this blob before retrying",
              "acceptance_record_missing")
    return digest


def _retained(store, digest):
    try:
        data = _read(_path(store.dir, f"acceptance/sha256/{_digest(digest)}"))
    except OSError as exc:
        raise AcceptanceError("acceptance_record_missing", f"retained evidence {digest} unreadable") from exc
    if _hash(data) != digest:
        _fail("retained evidence digest mismatch", "acceptance_record_missing")
    return data


def validate_plan(plan):
    cold = isinstance(plan, dict) and plan.get("schema_version") == 3
    _object(plan, "schema_version plan_id project_id scope authors partitions rows registry_ref "
            "registry_digest trust_profile" + (" cold_policy" if cold else ""), "plan")
    _version(plan["schema_version"], (1, 2, 3))
    if plan["schema_version"] == 3:
        from agenttalk.acceptance_cold import policy
        policy(plan["cold_policy"])
    for key in ("plan_id", "project_id", "scope"):
        _id(plan[key], f"plan.{key}")
    _digest(plan["registry_digest"])
    _text(plan["registry_ref"], "registry_ref")
    if plan["trust_profile"] != "cooperative":
        _fail("unsupported acceptance trust profile")
    _strings(plan["authors"], "authors", nonempty=plan["schema_version"] >= 2)
    partitions = _indexed(plan["partitions"], "partitions")
    if not partitions:
        _fail("plan requires partitions")
    for partition in partitions.values():
        _object(partition, "id agents", "partition")
        _strings(partition["agents"], "agents", nonempty=True)
        _id("acceptance-run-" + partition["id"], "partition lens id")
    rows = _indexed(plan["rows"], "rows")
    if not rows:
        _fail("plan requires rows")
    for row in rows.values():
        _object(row, "id partition policy comparator expected artifact field", "row")
        if row["partition"] not in partitions:
            _fail("row partition does not exist")
        if row["policy"] not in ("gating", "informational"):
            _fail("unsupported row policy")
        if row["comparator"] not in COMPARATORS:
            _fail("unsupported acceptance comparator")
        _id(row["artifact"], "row.artifact")
        _text(row["field"], "field")
        if row["comparator"] == "exit-code":
            if row["field"] != "exit_code" or type(row["expected"]) is not int:
                _fail("exit-code requires integer expected and exit_code field")
        elif row["comparator"] == "exact-failure-set":
            _strings(row["expected"], "expected failure IDs")
    if {r["partition"] for r in rows.values()} != set(partitions):
        _fail("each partition must own a row")
    return plan


def validate_registry(registry):
    _object(registry, "schema_version entries", "registry")
    _version(registry["schema_version"])
    for entry in _indexed(registry["entries"], "registry entries").values():
        _object(entry, "id kind version sha256", "registry entry")
        if entry["kind"] not in ("toolchain", "checker", "service"):
            _fail("unsupported registry kind")
        _text(entry["version"], "version")
        _digest(entry["sha256"])
    return registry


def verify_project(repo, revision, *, live=True):
    """No fallback to a caller's unverifiable SHA; git must verify this checkout."""
    repo = Path(repo).resolve()
    # Ambient Git redirection must not let the bus checkout answer for the project.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}

    def git(*args):
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,  # noqa: S603,S607  # nosec B603 B607
                                    text=True, encoding="utf-8", errors="replace", timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcceptanceError("acceptance_project_unverified", "project Git unavailable") from exc
        if result.returncode:
            _fail("project Git verification failed", "acceptance_project_unverified")
        return result.stdout.strip()

    _text(revision, "revision")
    head = None

    def metadata():
        nonlocal head
        # --verify still receives exactly one revision and --end-of-options
        # still precedes caller input. Split from the right for roots containing
        # newlines; --show-toplevel is emitted before the verified object ID.
        root, _, sha = git("rev-parse", "--show-toplevel", "--verify", "--end-of-options",
                           f"{revision}^{{commit}}").rpartition("\n")
        if Path(root.strip()).resolve() != repo:
            _fail("project locator must name the checkout root", "acceptance_project_unverified")
        if not _SHA.fullmatch(sha):
            _fail("project HEAD must equal the verified SHA", "acceptance_project_unverified")
        if live:
            head, _, tree = git("rev-parse", "HEAD", f"{sha}^{{tree}}").partition("\n")
        else:
            tree = git("rev-parse", f"{sha}^{{tree}}")
        return {"locator": str(repo), "revision": sha, "tree": tree,
                "roots": sorted(git("rev-list", "--max-parents=0", sha).splitlines())}

    # Only full object IDs are stable cache keys. Symbolic refs must resolve
    # afresh; HEAD and dirty state are checked at EVERY original live boundary.
    key = ("project", str(repo), revision, tuple(sorted(env.items())))
    project = acceptance_git.read_once(key, metadata) if _SHA.fullmatch(revision) else metadata()
    if live and (head if head is not None else git("rev-parse", "HEAD")) != project["revision"]:
        _fail("project HEAD must equal the verified SHA", "acceptance_project_unverified")
    dirty = git("status", "--porcelain", "--untracked-files=all") if live else ""
    if dirty:
        lines = dirty.splitlines()
        details = "\n".join(lines[:20])
        if len(lines) > 20:
            details += f"\n... {len(lines) - 20} more entries"
        _fail(f"acceptance project checkout is dirty:\n{details}", "acceptance_project_unverified")
    return project


def project_id(project):
    """Cooperative stable identity for the supported SHA-1 object graph."""
    payload = json.dumps({"object_format": "sha1", "roots": project["roots"]}, sort_keys=True).encode()
    return "git-" + _hash(payload)[:60]


def prepare(store, plan_file, project_repo, revision, scope, lenses=()):
    """Validate before opening; immutable copies cannot clear a close on their own."""
    path = Path(plan_file).absolute()
    plan_bytes = _read(_path(path.parent, path.name))
    plan = validate_plan(decode(plan_bytes))
    if plan["scope"] != scope:
        _fail("plan scope differs from close scope")
    partition_lenses(plan, lenses)
    registry_bytes = _read(_path(path.parent, plan["registry_ref"]))
    validate_registry(decode(registry_bytes))
    if _hash(registry_bytes) != plan["registry_digest"]:
        _fail("registry differs from plan pin", "acceptance_plan_stale")
    project = verify_project(project_repo, revision)
    if plan["schema_version"] >= 2 and plan["project_id"] != project_id(project):
        _fail(f"project_id must be {project_id(project)} for this Git root set", "acceptance_project_unverified")
    if plan["schema_version"] == 3:
        from agenttalk.acceptance_audit import change_identity
        change_identity(project, plan)
    return {"plan_hash": _retain(store, plan_bytes), "registry_hash": _retain(store, registry_bytes),
            "project": project, "plan": plan}


def partition_lenses(plan, lenses):
    """Check explicit assignments before creation and again when freezing."""
    result = list(lenses)
    existing = {lens["id"]: lens for lens in result}
    if len(existing) != len(result):
        _fail("duplicate close lens id")
    assignments = [("acceptance-run-" + p["id"], p["agents"]) for p in plan["partitions"]]
    if plan["schema_version"] == 3:
        assignments.append(("acceptance-cold", [plan["cold_policy"]["reviewer"]]))
    for lens_id, actors in assignments:
        lens = close.validate_lens_spec({"id": lens_id, "allowed_agents": actors})
        if lens["id"] in existing:
            actual = existing[lens["id"]]
            if (not actual.get("required") or actual.get("allowed_roles") or actual.get("allowed_groups")
                    or actual.get("signoff_set_id")
                    or set(actual.get("allowed_agents", [])) != set(lens["allowed_agents"])):
                _fail("declared partition lens differs from the frozen plan")
        else:
            result.append(lens)
    return result


def freeze(store, close_id, prepared, at):
    """Complete the pending route under the existing instance/generation transaction."""
    with close.close_transaction(store, close_id) as transaction:
        record = transaction.record
        if record.get("acceptance_route") != {"pending": True}:
            _fail("acceptance route already frozen")
        if record["revision"] != prepared["project"]["revision"]:
            _fail("project revision changed", "acceptance_project_unverified")
        if verify_project(prepared["project"]["locator"], record["revision"]) != prepared["project"]:
            _fail("project changed before freeze", "acceptance_project_unverified")
        plan = prepared["plan"]
        route = {"schema_version": 1, "attempt_id": uuid.uuid4().hex,
                 "instance_id": record["instance_id"], "project_id": plan["project_id"],
                 "revision": record["revision"], "project": prepared["project"],
                 "plan_hash": prepared["plan_hash"], "registry_hash": prepared["registry_hash"],
                 "bundle_hash": None, "frozen_by": record["opened_by"], "frozen_at": at,
                 "attached_by": None, "attached_at": None}
        if plan["schema_version"] >= 2:
            route.update(schema_version=plan["schema_version"], parent_record_hash=prepared.get("parent_record_hash"),
                         amendment_hash=prepared.get("amendment_hash"))
        if plan["schema_version"] == 3:
            route.update(cold_commit_hash=None, cold_reconcile_hash=None, obligations_hash=None)
        record["required_lenses"] = partition_lenses(plan, record["required_lenses"])
        record["acceptance_route"] = route
        if plan["schema_version"] == 3:
            from agenttalk import acceptance_obligations
            acceptance_obligations.inherit(store, record, plan, capture=True)
        transaction.commit()
    return record


def _route(record):
    route = record.get("acceptance_route")
    if not isinstance(route, dict) or route.get("pending"):
        _fail("acceptance route is absent or pending")
    extra = " parent_record_hash amendment_hash" if route.get("schema_version") in (2, 3) else ""
    if route.get("schema_version") == 3:
        extra += " cold_commit_hash cold_reconcile_hash obligations_hash"
        _digest(route.get("obligations_hash"))
        for key in ("cold_commit_hash", "cold_reconcile_hash"):
            if route.get(key) is not None:
                _digest(route[key])
    _object(route, "schema_version attempt_id instance_id project_id revision project plan_hash "
            "registry_hash bundle_hash frozen_by frozen_at attached_by attached_at" + extra, "acceptance route")
    _version(route["schema_version"], (1, 2, 3))
    if route["schema_version"] >= 2:
        if (route["parent_record_hash"] is None) != (route["amendment_hash"] is None):
            _fail("successor requires both parent record and amendment")
        if route["parent_record_hash"] is not None:
            _digest(route["parent_record_hash"])
            _digest(route["amendment_hash"])
    _id(route["attempt_id"], "route.attempt_id")
    _id(route["project_id"], "route.project_id")
    _digest(route["plan_hash"])
    _digest(route["registry_hash"])
    if route["bundle_hash"] is not None:
        _digest(route["bundle_hash"])
        _text(route["attached_by"], "attached_by")
        _text(route["attached_at"], "attached_at")
    elif route["attached_by"] is not None or route["attached_at"] is not None:
        _fail("attachment attribution without a bundle")
    _text(route["frozen_by"], "frozen_by")
    _text(route["frozen_at"], "frozen_at")
    if route["instance_id"] != record.get("instance_id") or route["revision"] != record.get("revision"):
        _fail("route instance/revision is stale", "acceptance_row_unbound")
    _object(route["project"], "locator revision tree roots", "project")
    return route


def _policy(store, record):
    route = _route(record)
    try:
        plan = validate_plan(decode(_retained(store, route["plan_hash"])))
        validate_registry(decode(_retained(store, route["registry_hash"])))
    except (OSError, AcceptanceError) as exc:
        raise AcceptanceError("acceptance_plan_stale", "frozen plan/registry invalid or missing") from exc
    if (plan["registry_digest"] != route["registry_hash"] or plan["project_id"] != route["project_id"]
            or plan["scope"] != record["scope"] or plan["schema_version"] != route["schema_version"]):
        _fail("frozen policy binding mismatch", "acceptance_plan_stale")
    return route, plan


def _bundle(bundle, record, route, plan):
    modern = route["schema_version"] >= 2
    final = route["schema_version"] == 3
    _object(bundle, "schema_version close_id instance_id attempt_id project_id revision plan_hash "
            "registry_hash runs rows artifacts" + (" verifier_access reproductions" if modern else "")
            + (" recovery_approvals hygiene" if final else ""), "bundle")
    _version(bundle["schema_version"], (route["schema_version"],))
    for key in ("instance_id", "attempt_id", "project_id", "revision", "plan_hash", "registry_hash"):
        if bundle[key] != route[key]:
            _fail(f"bundle {key} mismatch", "acceptance_row_unbound")
    if bundle["close_id"] != record["close_id"]:
        _fail("bundle close mismatch", "acceptance_row_unbound")
    partitions = {p["id"]: p for p in plan["partitions"]}
    runs = _indexed(bundle["runs"], "runs")
    for run in runs.values():
        _object(run, "id partition actor revision head_before head_after status_before status_after"
                + (" access_id" if modern else "") + (" environment offline_proof" if final else ""), "run")
        if modern:
            _text(run["access_id"], "runner access_id")
        if run["partition"] not in partitions or run["actor"] not in partitions[run["partition"]]["agents"]:
            _fail("runner not assigned to partition", "acceptance_lens_not_independent")
        if any(run[k] != record["revision"] for k in ("revision", "head_before", "head_after")):
            _fail("run not bound to revision", "acceptance_row_unbound")
        if run["status_before"] != "" or run["status_after"] != "":
            _fail("runner reports dirty checkout", "acceptance_project_unverified")
    rows = _indexed(bundle["rows"], "row observations")
    definitions = {r["id"]: r for r in plan["rows"]}
    if set(rows) != set(definitions):
        _fail("bundle must contain every planned row exactly once", "acceptance_record_missing")
    for row in rows.values():
        _object(row, "id run_id", "row observation")
        if row["run_id"] not in runs or runs[row["run_id"]]["partition"] != definitions[row["id"]]["partition"]:
            _fail("row refers to wrong run/partition", "acceptance_row_unbound")
    artifacts = _indexed(bundle["artifacts"], "artifacts")
    expected_artifacts = {r["artifact"] for r in definitions.values()}
    if modern:
        verifier = _object(bundle["verifier_access"], "id evidence", "verifier access")
        _text(verifier["id"], "verifier access id")
        expected_artifacts.add(_id(verifier["evidence"], "verifier.evidence"))
        for rep in _indexed(bundle["reproductions"], "reproductions").values():
            _object(rep, "id source_run actor access_id access_evidence revision head_before head_after "
                    "status_before status_after rows" + (" environment offline_proof" if final else ""), "reproduction")
            for key in ("actor", "access_id"):
                _text(rep[key], key)
            if rep["id"] in runs or rep["source_run"] not in runs:
                _fail("reproduction run reference is invalid", "acceptance_row_unbound")
            expected_artifacts.add(_id(rep["access_evidence"], "reproduction.access_evidence"))
            for obs in _indexed(rep["rows"], "reproduced rows").values():
                _object(obs, "id artifact", "reproduced row")
                if obs["id"] not in rows or rows[obs["id"]]["run_id"] != rep["source_run"]:
                    _fail("reproduction row belongs to another run", "acceptance_row_unbound")
                expected_artifacts.add(_id(obs["artifact"], "reproduction row.artifact"))
    if final:
        expected_artifacts.add(_id(bundle["hygiene"], "bundle.hygiene"))
        for run in bundle["runs"] + bundle["reproductions"]:
            expected_artifacts.update(_id(run[k], f"run.{k}") for k in ("environment", "offline_proof"))
        seen = set()
        for item in _items(bundle["recovery_approvals"], "recovery approvals"):
            _object(item, "prior_attempt_id reduction approval_artifact", "recovery approval")
            _text(item["prior_attempt_id"], "prior attempt")
            if item["prior_attempt_id"] in seen:
                _fail("duplicate recovery approval")
            seen.add(item["prior_attempt_id"])
            expected_artifacts.add(_id(item["approval_artifact"], "recovery.approval_artifact"))
    if set(artifacts) != expected_artifacts:
        _fail("artifact manifest differs from plan", "acceptance_record_missing")
    for artifact in artifacts.values():
        _object(artifact, "id path sha256", "artifact")
        _text(artifact["path"], "artifact path")
        _digest(artifact["sha256"])
    return rows, artifacts


def attach(store, close_id, bundle_file, *, by, at):
    _text(by, "attached_by")
    _text(at, "attached_at")
    path = Path(bundle_file).absolute()
    data = _read(_path(path.parent, path.name))
    bundle = decode(data)
    with close.close_transaction(store, close_id) as transaction:
        record = transaction.record
        if record["status"] == close.PUBLISHED:
            _fail("cannot attach to a published close")
        route, plan = _policy(store, record)
        if route["schema_version"] == 3 and route["cold_commit_hash"] is None:
            _fail("commit initial cold observations before attachment", "acceptance_cold_missing")
        if verify_project(route["project"]["locator"], record["revision"]) != route["project"]:
            _fail("project changed before attachment", "acceptance_project_unverified")
        if route["bundle_hash"] is not None:
            _fail("attempt already has an immutable bundle")
        _, artifacts = _bundle(bundle, record, route, plan)
        if route["schema_version"] == 3:
            from agenttalk.acceptance_cold import reproduction_lenses
            reproduction_lenses(record, bundle)
        captured = []
        total = len(data)
        for artifact in artifacts.values():
            raw = _read(_path(path.parent, artifact["path"]))
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                _fail("bundle exceeds total byte limit")
            if _hash(raw) != artifact["sha256"]:
                _fail("source artifact digest mismatch", "acceptance_record_missing")
            captured.append(raw)
        # A failure leaves only unreferenced blobs; no partial bundle can become current.
        for raw in captured:
            _retain(store, raw)
        if route["schema_version"] == 3:
            # Approval evidence must come from the reserved operator's actual
            # bus record, not a caller-authored file with a claimed sender.
            for item in bundle["recovery_approvals"]:
                from agenttalk.acceptance_history import _reduction
                reduction = _reduction(item["reduction"])
                actual = _read(_path(store.messages_dir, reduction["decision_ref"] + ".json"))
                if _hash(actual) != artifacts[item["approval_artifact"]]["sha256"]:
                    _fail("recovery approval differs from operator record", "acceptance_scope_reduction_unapproved")
        route["bundle_hash"] = _retain(store, data)
        route.update(attached_by=by, attached_at=at)
        close._event(record, "acceptance:attach", by, at, bundle_hash=route["bundle_hash"])
        transaction.commit()
    return route["bundle_hash"]


def _compare(row, raw):
    field = row["field"]
    if field not in raw["values"]:
        _fail("required observed field missing", "acceptance_record_missing")
    observed, expected = raw["values"][field], row["expected"]
    if row["comparator"] == "exit-code":
        if type(observed) is not int:
            _fail("observed exit code is not an integer")
        return observed == expected
    if row["comparator"] == "exact-failure-set":
        # A bounded expected set cannot equal an observed set beyond that bound.
        if isinstance(observed, list) and len(observed) > MAX_ITEMS:
            return False
        return set(_strings(observed, "observed failure IDs")) == set(expected)
    # JSON canonical comparison distinguishes true from 1, including nested values.
    return json.dumps(observed, sort_keys=True) == json.dumps(expected, sort_keys=True)


def validate_raw(raw, revision, run_id):
    _object(raw, "schema_version run_id revision values", "raw result")
    _version(raw["schema_version"])
    if raw["revision"] != revision or raw["run_id"] != run_id:
        _fail("raw result belongs to another run/revision", "acceptance_row_unbound")
    if not isinstance(raw["values"], dict):
        _fail("raw result values must be an object")


@acceptance_git.operation
def resolve(store, record, *, live=False):
    """Read and verify immutable inputs; return a snapshot for the pure DoD fold."""
    snapshot = {"holds": [], "outcomes": []}
    try:
        route, plan = _policy(store, record)
        # Schema-1 records preserve their original strict-live contract. Schema 2
        # reads historical objects; open, attach and GO publish check live state.
        project = verify_project(route["project"]["locator"], record["revision"],
                                 live=live or route["schema_version"] == 1)
        if project != route["project"]:
            _fail("project identity changed", "acceptance_project_unverified")
        if route["schema_version"] >= 2 and route["project_id"] != project_id(project):
            _fail("project ID differs from verified root identity", "acceptance_project_unverified")
        if route["bundle_hash"] is None:
            _fail("acceptance bundle missing", "acceptance_record_missing")
        bundle = decode(_retained(store, route["bundle_hash"]))
        rows, artifacts = _bundle(bundle, record, route, plan)
        raw_results = {}
        integrity_holds = []
        total = 0
        for artifact in artifacts.values():
            try:
                data = _retained(store, artifact["sha256"])
                total += len(data)
                # Non-measurement evidence may be plain text. Retention is
                # mandatory for every artifact; JSON shape belongs to its reader.
                try:
                    raw_results[artifact["id"]] = decode(data)
                except AcceptanceError as exc:
                    raw_results[artifact["id"]] = exc
            except (OSError, AcceptanceError) as exc:
                raw_results[artifact["id"]] = exc
                integrity_holds.append(("acceptance_record_missing", f"artifact {artifact['id']}: {exc}"))
        if total > MAX_TOTAL_BYTES:
            _fail("retained bundle exceeds total byte limit")
        outcomes = []
        holds = integrity_holds
        for row in plan["rows"]:
            outcome = {"id": row["id"], "policy": row["policy"], "passed": None}
            comparing = False
            try:
                raw = raw_results[row["artifact"]]
                if isinstance(raw, Exception):
                    raise raw
                validate_raw(raw, record["revision"], rows[row["id"]]["run_id"])
                comparing = True
                outcome["passed"] = _compare(row, raw)
            except (AcceptanceError, OSError) as exc:
                code = getattr(exc, "code", "acceptance_record_missing")
                outcome["error"] = {"code": code, "detail": str(exc)}
                if (not comparing or row["policy"] == "gating"
                        or code in {"acceptance_record_missing", "acceptance_row_unbound"}):
                    holds.append((code, f"row {row['id']}: {exc}"))
            outcomes.append(outcome)
        snapshot = {"holds": holds, "outcomes": outcomes}
        if route["schema_version"] >= 2:
            from agenttalk import acceptance_history
            snapshot["trust_checked"] = True
            snapshot["reproductions"] = _reproduce(store, record, plan, bundle, rows, artifacts, holds)
            _ack_bindings(record, plan, holds)
            acceptance_history.evaluate(store, record, plan, snapshot)
            if route["schema_version"] == 3:
                from agenttalk import acceptance_cold, acceptance_hygiene
                acceptance_history.related_obligations(store, record, plan, bundle, snapshot)
                acceptance_cold.evaluate(store, record, plan, bundle, snapshot)
                acceptance_hygiene.evaluate(store, record, bundle, snapshot)
        return snapshot
    except AcceptanceError as exc:
        snapshot["holds"].append((exc.code, str(exc)))
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        snapshot["holds"].append(("acceptance_record_missing",
                                  f"unreadable acceptance evidence: {type(exc).__name__}"))
        return snapshot


def evaluate(snapshot):
    """Pure; caller-provided ack/gate labels never resolve acceptance evidence."""
    holds = []
    if not isinstance(snapshot, dict) or not snapshot.get("trust_checked"):
        holds.append(("acceptance_trust_unresolved", "cooperative verification/reproduction evidence missing"))
    elif not snapshot.get("cold_checked"):
        holds.append(("acceptance_cold_missing", "eligible final cold review missing"))
    elif not snapshot.get("hygiene_checked"):
        holds.append(("acceptance_record_missing", "bound execution and final hygiene evidence missing"))
    if not isinstance(snapshot, dict):
        return holds + [("acceptance_record_missing", "acceptance evaluation missing")]
    holds.extend(snapshot.get("holds", []))
    for row in snapshot.get("outcomes", []):
        if row["policy"] == "gating" and row["passed"] is False:
            holds.append(("acceptance_row_failed", f"gating row {row['id']} failed recomputation"))
    return holds


def ack_binding(record):
    route = record.get("acceptance_route") or {}
    keys = ["instance_id", "attempt_id", "revision", "plan_hash", "registry_hash", "bundle_hash"]
    if route.get("schema_version") == 3:
        keys.extend(["cold_commit_hash", "cold_reconcile_hash", "obligations_hash"])
    return {key: route.get(key) for key in keys}


def _ack_bindings(record, plan, holds):
    for partition in plan["partitions"]:
        ack = record["lens_acks"].get("acceptance-run-" + partition["id"], {})
        if (ack.get("acceptance_binding") != ack_binding(record) or ack.get("status") != close.ACCEPT
                or ack.get("override") or ack.get("from") not in partition["agents"]):
            holds.append(("acceptance_lens_not_independent", f"partition {partition['id']} needs a fresh bound accept"))


def _reproduce(store, record, plan, bundle, rows, artifacts, holds):
    """Recompute original and reproduced assertions; never trust submitted verdicts."""
    route = record["acceptance_route"]
    runs = {run["id"]: run for run in bundle["runs"]}
    runners = {run["actor"] for run in runs.values()}
    authors = set(plan["authors"])
    verifier = route["attached_by"]
    authorities = {store.sole_lead(), store.operator_facing()}
    try:
        authorities.add(store.operator_identity())
    except ValueError:
        pass
    if verifier not in authorities:
        holds.append(("acceptance_trust_unresolved", "verifier is not the configured lead/operator"))
    access = bundle["verifier_access"]
    runner_access = {run["access_id"] for run in runs.values()}
    if verifier in runners | authors or access["id"] in runner_access:
        holds.append(("acceptance_trust_unresolved", "verifier must be separate from runners and authors"))
    if not _retained(store, artifacts[access["evidence"]]["sha256"]).strip():
        _fail("verifier separate-access evidence is empty", "acceptance_trust_unresolved")
    results = []
    gating = [row for row in plan["rows"] if row["policy"] == "gating"]
    for run_id in sorted({rows[row["id"]]["run_id"] for row in gating}):
        required = [row for row in gating if rows[row["id"]]["run_id"] == run_id]
        matches = [rep for rep in bundle["reproductions"] if rep["source_run"] == run_id]
        if len(matches) != 1:
            holds.append(("acceptance_trust_unresolved", f"run {run_id} requires exactly one reproduction"))
            continue
        rep = matches[0]
        try:
            if (rep["actor"] in runners | authors | {verifier}
                    or rep["access_id"] in runner_access | {access["id"]}):
                _fail("reproduction actor/access is not separate", "acceptance_trust_unresolved")
            if any(rep[k] != record["revision"] for k in ("revision", "head_before", "head_after")):
                _fail("reproduction revision differs", "acceptance_row_unbound")
            if rep["status_before"] != "" or rep["status_after"] != "":
                _fail("reproduction checkout is dirty", "acceptance_project_unverified")
            if not _retained(store, artifacts[rep["access_evidence"]]["sha256"]).strip():
                _fail("reproduction access evidence missing", "acceptance_trust_unresolved")
            observations = {obs["id"]: obs for obs in rep["rows"]}
            for row in required:
                if row["id"] not in observations:
                    _fail("gating comparison not reproduced", "acceptance_trust_unresolved")
                artifact = artifacts[observations[row["id"]]["artifact"]]
                raw = decode(_retained(store, artifact["sha256"]))
                _object(raw, "schema_version run_id revision values", "reproduction raw result")
                _version(raw["schema_version"])
                if raw["run_id"] != rep["id"] or raw["revision"] != record["revision"]:
                    _fail("reproduced raw result is unbound", "acceptance_row_unbound")
                if not isinstance(raw["values"], dict):
                    _fail("reproduction values must be an object")
                passed = _compare(row, raw)
                results.append({"run_id": run_id, "row_id": row["id"], "actor": rep["actor"], "passed": passed})
                if not passed:
                    holds.append(("acceptance_row_failed", f"reproduction of {row['id']} failed recomputation"))
        except AcceptanceError as exc:
            holds.append((exc.code, f"reproduction {rep['id']}: {exc}"))
    return results
