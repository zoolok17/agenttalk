"""Acceptance increment 1a: bounded evidence inputs, never a standalone verdict.

The resolver performs I/O; evaluate() only consumes its validated snapshot.
Cooperative reproduction and final cold eligibility are not shipped in 1a, so
even otherwise complete evidence has a direct acceptance_trust_unresolved hold.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import uuid

from agenttalk import close

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


def _id(value):
    try:
        return close.validate_close_id(value)
    except close.CloseError as exc:
        raise AcceptanceError("acceptance_policy_invalid", str(exc)) from exc


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("expected lowercase SHA-256 digest")
    return value


def _version(value):
    if type(value) is not int or value != 1:
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
        key = _id(value.get("id"))
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
    finally:
        temporary.unlink(missing_ok=True)
    if _read(path) != data:
        _fail(f"retained bytes differ from their digest at {path}; quarantine this blob before retrying",
              "acceptance_record_missing")
    return digest


def _retained(store, digest):
    data = _read(_path(store.dir, f"acceptance/sha256/{_digest(digest)}"))
    if _hash(data) != digest:
        _fail("retained evidence digest mismatch", "acceptance_record_missing")
    return data


def validate_plan(plan):
    _object(plan, "schema_version plan_id project_id scope authors partitions rows registry_ref "
            "registry_digest trust_profile", "plan")
    _version(plan["schema_version"])
    for key in ("plan_id", "project_id", "scope"):
        _id(plan[key])
    _digest(plan["registry_digest"])
    _text(plan["registry_ref"], "registry_ref")
    if plan["trust_profile"] != "cooperative":
        _fail("unsupported acceptance trust profile")
    _strings(plan["authors"], "authors")
    partitions = _indexed(plan["partitions"], "partitions")
    if not partitions:
        _fail("plan requires partitions")
    for partition in partitions.values():
        _object(partition, "id agents", "partition")
        _strings(partition["agents"], "agents", nonempty=True)
        _id("acceptance-run-" + partition["id"])
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
        _id(row["artifact"])
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


def verify_project(repo, revision):
    """No fallback to a caller's unverifiable SHA; git must verify this checkout."""
    repo = Path(repo).resolve()
    # Ambient Git redirection must not let the bus checkout answer for the project.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}

    def git(*args):
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcceptanceError("acceptance_project_unverified", "project Git unavailable") from exc
        if result.returncode:
            _fail("project Git verification failed", "acceptance_project_unverified")
        return result.stdout.strip()

    _text(revision, "revision")
    if Path(git("rev-parse", "--show-toplevel")).resolve() != repo:
        _fail("project locator must name the checkout root", "acceptance_project_unverified")
    sha = git("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    if not _SHA.fullmatch(sha) or git("rev-parse", "HEAD") != sha:
        _fail("project HEAD must equal the verified SHA", "acceptance_project_unverified")
    dirty = git("status", "--porcelain", "--untracked-files=all")
    if dirty:
        lines = dirty.splitlines()
        details = "\n".join(lines[:20])
        if len(lines) > 20:
            details += f"\n... {len(lines) - 20} more entries"
        _fail(f"acceptance project checkout is dirty:\n{details}", "acceptance_project_unverified")
    return {"locator": str(repo), "revision": sha, "tree": git("rev-parse", f"{sha}^{{tree}}"),
            "roots": sorted(git("rev-list", "--max-parents=0", sha).splitlines())}


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
    return {"plan_hash": _retain(store, plan_bytes), "registry_hash": _retain(store, registry_bytes),
            "project": project, "plan": plan}


def partition_lenses(plan, lenses):
    """Check explicit assignments before creation and again when freezing."""
    result = list(lenses)
    existing = {lens["id"]: lens for lens in result}
    if len(existing) != len(result):
        _fail("duplicate close lens id")
    for partition in plan["partitions"]:
        lens = close.validate_lens_spec({"id": "acceptance-run-" + partition["id"],
                                        "allowed_agents": partition["agents"]})
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
        record["required_lenses"] = partition_lenses(plan, record["required_lenses"])
        record["acceptance_route"] = route
        transaction.commit()
    return record


def _route(record):
    route = record.get("acceptance_route")
    if not isinstance(route, dict) or route.get("pending"):
        _fail("acceptance route is absent or pending")
    _object(route, "schema_version attempt_id instance_id project_id revision project plan_hash "
            "registry_hash bundle_hash frozen_by frozen_at attached_by attached_at", "acceptance route")
    _version(route["schema_version"])
    _id(route["attempt_id"])
    _id(route["project_id"])
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
            or plan["scope"] != record["scope"]):
        _fail("frozen policy binding mismatch", "acceptance_plan_stale")
    return route, plan


def _bundle(bundle, record, route, plan):
    _object(bundle, "schema_version close_id instance_id attempt_id project_id revision plan_hash "
            "registry_hash runs rows artifacts", "bundle")
    _version(bundle["schema_version"])
    for key in ("instance_id", "attempt_id", "project_id", "revision", "plan_hash", "registry_hash"):
        if bundle[key] != route[key]:
            _fail(f"bundle {key} mismatch", "acceptance_row_unbound")
    if bundle["close_id"] != record["close_id"]:
        _fail("bundle close mismatch", "acceptance_row_unbound")
    partitions = {p["id"]: p for p in plan["partitions"]}
    runs = _indexed(bundle["runs"], "runs")
    for run in runs.values():
        _object(run, "id partition actor revision head_before head_after status_before status_after", "run")
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
    if set(artifacts) != {r["artifact"] for r in definitions.values()}:
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
        if route["bundle_hash"] is not None:
            _fail("attempt already has an immutable bundle")
        _, artifacts = _bundle(bundle, record, route, plan)
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


def resolve(store, record):
    """Read and verify immutable inputs; return a snapshot for the pure DoD fold."""
    try:
        route, plan = _policy(store, record)
        project = verify_project(route["project"]["locator"], record["revision"])
        if project != route["project"]:
            _fail("project identity changed", "acceptance_project_unverified")
        if route["bundle_hash"] is None:
            _fail("acceptance bundle missing", "acceptance_record_missing")
        bundle = decode(_retained(store, route["bundle_hash"]))
        rows, artifacts = _bundle(bundle, record, route, plan)
        raw_results = {}
        total = 0
        for artifact in artifacts.values():
            try:
                data = _retained(store, artifact["sha256"])
                total += len(data)
                raw_results[artifact["id"]] = decode(data)
            except (OSError, AcceptanceError) as exc:
                raw_results[artifact["id"]] = exc
        if total > MAX_TOTAL_BYTES:
            _fail("retained bundle exceeds total byte limit")
        outcomes = []
        holds = []
        for row in plan["rows"]:
            outcome = {"id": row["id"], "policy": row["policy"], "passed": None}
            try:
                raw = raw_results[row["artifact"]]
                if isinstance(raw, Exception):
                    raise raw
                _object(raw, "schema_version run_id revision values", "raw result")
                _version(raw["schema_version"])
                if raw["revision"] != record["revision"] or raw["run_id"] != rows[row["id"]]["run_id"]:
                    _fail("raw result belongs to another run/revision", "acceptance_row_unbound")
                if not isinstance(raw["values"], dict):
                    _fail("raw result values must be an object")
                outcome["passed"] = _compare(row, raw)
            except (AcceptanceError, OSError) as exc:
                code = getattr(exc, "code", "acceptance_record_missing")
                outcome["error"] = {"code": code, "detail": str(exc)}
                if row["policy"] == "gating":
                    holds.append((code, f"row {row['id']}: {exc}"))
            outcomes.append(outcome)
        return {"holds": holds, "outcomes": outcomes}
    except AcceptanceError as exc:
        return {"holds": [(exc.code, str(exc))], "outcomes": []}
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return {"holds": [("acceptance_record_missing", f"unreadable acceptance evidence: {type(exc).__name__}")],
                "outcomes": []}


def evaluate(snapshot):
    """Pure; caller-provided ack/gate labels never resolve acceptance evidence."""
    holds = [("acceptance_trust_unresolved", "increment 1a does not yet enforce reproduction/final cold review")]
    if not isinstance(snapshot, dict):
        return holds + [("acceptance_record_missing", "acceptance evaluation missing")]
    holds.extend(snapshot.get("holds", []))
    for row in snapshot.get("outcomes", []):
        if row["policy"] == "gating" and not row["passed"]:
            holds.append(("acceptance_row_failed", f"gating row {row['id']} failed recomputation"))
    return holds
