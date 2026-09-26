"""Schema-4 open/attach capture. Publication integration is a separate milestone."""

import json
from pathlib import Path

from agenttalk import acceptance as A, acceptance_preflight as P, acceptance_registry as R, close

ROUTE_FIELDS = "cache_root environment_hash preflight_open_hash preflight_attach_hash"
BINDING_FIELDS = "instance_id attempt_id project_id revision plan_hash registry_hash"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def binding(record):
    route = record["acceptance_route"]
    return {"close_id": record["close_id"], **{key: route[key] for key in BINDING_FIELDS.split()}}


def locator(value):
    if value is None:
        A._fail("--cache-root is required for a schema-4 plan")
    R._text(value, "cache root")
    if not Path(value).is_absolute():
        A._fail("--cache-root must be an absolute, fully resolved path")
    path = Path(value).absolute()
    if ".." in path.parts:
        A._fail("cache root must be a fully resolved locator")
    try:
        R.staged_path(path, "preflight-probe")
    except A.LinkedPathError:
        raise
    except A.AcceptanceError as exc:
        if exc.code != P.UNAVAILABLE:
            raise
        # Valid absent staging is an unavailable prerequisite, not malformed policy.
    return str(path)


def prepare(store, plan_file, cache_root):
    path = Path(plan_file).absolute()
    plan_bytes = P.read_input(path)
    raw = R.decode(plan_bytes)
    if not isinstance(raw, dict):
        A._fail("preflight plan must be an object")
    registry_bytes = P.read_input(path.parent / R.relative_path(raw.get("registry_ref")))
    plan, _ = R.policy(plan_bytes, registry_bytes)
    root = locator(cache_root)
    captured = []
    report = P.evaluate(plan_bytes, registry_bytes, root, capture=captured)
    return {"plan": plan, "plan_hash": A._retain(store, plan_bytes),
            "registry_hash": A._retain(store, registry_bytes), "cache_root": root,
            "environment_hash": A._retain(store, canonical(plan["environment"])),
            "preflight_report": report, "preflight_inputs": captured}


def retain_report(store, record, report, inputs, observation_hash=None):
    retained = []
    for ref, data in inputs:
        if ref in {item["ref"] for item in retained}:
            A._fail("duplicate retained input reference")
        retained.append({"ref": ref, "sha256": A._retain(store, data), "size": len(data)})
    capsule = {"schema_version": 1, "binding": binding(record), "report": report,
               "observation_hash": observation_hash, "inputs": retained}
    return A._retain(store, canonical(capsule))


def freeze(store, record, prepared):
    route = record["acceptance_route"]
    route.update(cache_root=prepared["cache_root"], environment_hash=prepared["environment_hash"],
                 preflight_open_hash=None, preflight_attach_hash=None)
    route["preflight_open_hash"] = retain_report(
        store, record, prepared["preflight_report"], prepared["preflight_inputs"])


def validate_route(route):
    R._text(route["cache_root"], "private cache locator")
    path = Path(route["cache_root"])
    # Syntax only: historical cache absence must not invalidate the original route.
    if not path.is_absolute() or ".." in path.parts:
        A._fail("private cache locator must be absolute without parent traversal")
    for key in ("environment_hash", "preflight_open_hash"):
        A._digest(route[key])
    if route["preflight_attach_hash"] is not None:
        A._digest(route["preflight_attach_hash"])
    if (route["bundle_hash"] is None) != (route["preflight_attach_hash"] is None):
        A._fail("preflight attachment and bundle must be paired", "acceptance_row_unbound")


def policy(store, route, plan_bytes, registry_bytes):
    plan, _ = R.policy(plan_bytes, registry_bytes)
    if A._retained(store, route["environment_hash"]) != canonical(plan["environment"]):
        A._fail("frozen environment differs from plan", "acceptance_plan_stale")
    return plan


def prepare_attachment(store, close_id, path, bundle):
    record = close.load_close(store, close_id)
    route, _ = A._policy(store, record)
    if not A.schema(route["schema_version"]).preflight:
        A._fail("preflight attachment requires a schema-4 route")
    if record["status"] == close.PUBLISHED:
        A._fail("cannot attach to a published close")
    if route["cold_commit_hash"] is None:
        A._fail("commit initial cold observations before attachment", "acceptance_cold_missing")
    if route["bundle_hash"] is not None:
        A._fail("attempt already has an immutable bundle")
    ref = bundle.get("preflight_observation")
    R.evidence_ref(ref)
    data = P.read_input(R.staged_path(path.parent, ref["path"]))
    if len(data) != ref["size"] or A._hash(data) != ref["sha256"]:
        A._fail("observation envelope differs from pin", P.INTEGRITY)
    envelope = R.decode(data)
    A._object(envelope, "schema_version binding observation", "preflight observation envelope")
    A._version(envelope["schema_version"])
    if envelope["binding"] != binding(record):
        A._fail("preflight observation binding differs", "acceptance_row_unbound")
    captured = []
    report = P.evaluate(A._retained(store, route["plan_hash"]), A._retained(store, route["registry_hash"]),
                        route["cache_root"], observation_bytes=canonical(envelope["observation"]),
                        proof_root=path.parent, capture=captured)
    return {"route": dict(route), "report": report, "inputs": captured, "envelope": data}


def attach(store, record, prepared):
    if record["acceptance_route"] != prepared["route"]:
        A._fail("route changed during preflight capture", "acceptance_row_unbound")
    return retain_report(store, record, prepared["report"], prepared["inputs"],
                         A._retain(store, prepared["envelope"]))


def pending_snapshot(store, record):
    """Expose bound staging results, while refusing GO until M3b is implemented."""
    route = record["acceptance_route"]
    digest = route["preflight_attach_hash"] or route["preflight_open_hash"]
    capsule = A.decode(A._retained(store, digest))
    A._object(capsule, "schema_version binding report observation_hash inputs", "preflight capture")
    A._version(capsule["schema_version"])
    if capsule["binding"] != binding(record):
        A._fail("preflight capture binding differs", "acceptance_row_unbound")
    if capsule["observation_hash"] is not None:
        A._digest(capsule["observation_hash"])
    seen = set()
    for item in R._list(capsule["inputs"], "retained inputs", 3 * R.MAX_FILES + 2 * R.MAX_ENTRIES):
        A._object(item, "ref sha256 size", "retained preflight input")
        ref = item["ref"]
        if not isinstance(ref, str) or ref.count(":") != 1 or ref in seen:
            A._fail("retained input.ref must be a unique role-qualified ID")
        public_id, role = ref.split(":")
        A._id(public_id, "retained input.ref")
        if role not in {"pin", "planned", "observed", "banner", "log"}:
            A._fail("invalid retained input role")
        seen.add(ref)
        R._integer(item["size"], "retained input size", 0, R.MAX_INPUT_BYTES)
        if len(A._retained(store, item["sha256"])) != item["size"]:
            A._fail("retained preflight input size differs", P.INTEGRITY)
    if capsule["observation_hash"]:
        A._retained(store, capsule["observation_hash"])
    return {"outcomes": [], "preflight": capsule["report"],
            "holds": [(h["code"], h["detail"]) for h in capsule["report"]["holds"]]
            + [(P.UNAVAILABLE, "schema-4 publication verification is not enabled in this milestone")]}
