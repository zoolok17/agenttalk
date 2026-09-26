"""Command-local verified bytes and publication-time metadata checks.

No verdict survives between commands. Large artifacts are hashed outside the
writer lock; the final evaluator consumes those bytes only after identity,
size and modification metadata are rechecked under that lock. Cooperative
writers must not mutate a file while preserving all reported metadata.
"""

from copy import deepcopy

from agenttalk import acceptance as A, acceptance_preflight as P, acceptance_registry as R
from agenttalk import acceptance_staging as S, close


def identity(value):
    return tuple(getattr(value, key, None) for key in
                 ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _inputs(store, record):
    route, _ = A._policy(store, record)
    S.evidence(store, record)
    capsule = S.pending_snapshot(store, record)
    observation = None
    if capsule["observation_hash"]:
        envelope = R.decode(A._retained(store, capsule["observation_hash"]))
        A._object(envelope, "schema_version binding observation", "preflight observation envelope")
        A._version(envelope["schema_version"])
        if envelope["binding"] != S.binding(record):
            A._fail("preflight observation binding differs", "acceptance_row_unbound")
        observation = S.canonical(envelope["observation"])
    return route, capsule, observation


def prepare(store, record):
    """Read staged pins before acquiring publication/successor locks."""
    route = record.get("acceptance_route")
    if (not isinstance(route, dict) or route.get("pending")
            or not A.schema(route.get("schema_version")).preflight):
        return None
    scan = {"route": deepcopy(route), "reads": {}, "identities": {}}
    try:
        route, capsule, observation = _inputs(store, record)
        retained = {item["ref"]: item for item in capsule["inputs"]}

        def reader(root, pin, budget, **kwargs):
            role = kwargs.get("capture_role", "pin")
            ref = kwargs.get("public_ref") or pin.get("id", "preflight")
            key = ref + ":" + role
            if role in {"banner", "log", "observed"}:
                item = retained.get(key)
                data = A._retained(store, item["sha256"]) if item else None
                errors = []
                if (data is None or len(data) != pin["size"] or A._hash(data) != pin["sha256"]
                        or len(data) > budget[0]):
                    data = None
                    errors = [P.hold(P.INTEGRITY, "bound preflight evidence is missing or differs", ref)]
                else:
                    budget[0] -= len(data)
                result = data, errors
            else:
                try:
                    path = R.staged_path(root, pin["path"])
                    before = identity(path.lstat())
                    result = P._read_pin(root, pin, budget, **kwargs)
                    after = identity(R.staged_path(root, pin["path"]).lstat())
                    if before != after:
                        raise A.AcceptanceError(P.UNAVAILABLE, "staged input changed during hashing")
                    scan["identities"][pin["path"]] = after
                except (A.AcceptanceError, OSError, ValueError) as exc:
                    result = None, [P.hold(P.UNAVAILABLE, P.UNAVAILABLE_DETAIL, ref,
                                          mandatory=isinstance(exc, A.LinkedPathError))]
                # A later appearance/change of declarative evidence needs a fresh
                # attachment and seal. Never append bytes after cold reconciliation.
                data = result[0]
                if data is not None and (key not in retained or A._hash(data) != retained[key]["sha256"]):
                    result = None, [P.hold(P.INTEGRITY, "staged evidence is absent from the sealed capture", ref)]
            scan["reads"][key] = (deepcopy(pin), deepcopy(result))
            return result

        P.evaluate(A._retained(store, route["plan_hash"]), A._retained(store, route["registry_hash"]),
                   route["cache_root"], observation_bytes=observation, reader=reader)
    except (A.AcceptanceError, OSError, ValueError):
        scan["failure"] = True
    return scan


def recheck(record, scan):
    """Metadata only; safe inside the shared writer lock, including on Windows."""
    if scan is None or scan.get("failure") or scan["route"] != record["acceptance_route"]:
        A._fail("preflight input set changed or is unavailable; retry", P.UNAVAILABLE)
    try:
        for relative, expected in scan["identities"].items():
            path = R.staged_path(scan["route"]["cache_root"], relative)
            if identity(path.lstat()) != expected:
                A._fail("staged input changed after hashing; retry", P.UNAVAILABLE)
    except (OSError, ValueError, A.LinkedPathError):
        raise A.AcceptanceError(P.UNAVAILABLE, "staged input changed after hashing; retry") from None


def evaluate(store, record, *, scan=None, decision_at=None):
    route, _, observation = _inputs(store, record)
    if record["status"] == close.PUBLISHED:
        # Historical artifacts were pinned, not retained. Never reclassify the
        # original verdict using today's cache or fabricate a historical pass.
        registry = R.decode(A._retained(store, route["registry_hash"]))
        return {"historical": [{"id": pin["id"], "sha256": pin["sha256"],
                                "status": "artifact not retained, pinned by digest"}
                               for pin in registry["files"] if pin["role"] == "distribution"], "holds": []}
    if scan is not None and scan.get("not_requested"):
        # Explicit HOLD publication does not assess staging or invent a failure
        # that a successor would then have to dispose. No pass is asserted.
        return {"evaluation": "not requested for HOLD publication", "holds": []}
    scan = prepare(store, record) if scan is None else scan
    recheck(record, scan)

    def reader(root, pin, budget, **kwargs):
        ref = kwargs.get("public_ref") or pin.get("id", "preflight")
        key = ref + ":" + kwargs.get("capture_role", "pin")
        saved = scan["reads"].get(key)
        if saved is None or saved[0] != pin:
            return None, [P.hold(P.UNAVAILABLE, "preflight input was not verified in this command", ref)]
        return deepcopy(saved[1])

    report = P.evaluate(A._retained(store, route["plan_hash"]), A._retained(store, route["registry_hash"]),
                        route["cache_root"], observation_bytes=observation, reader=reader,
                        decision_at=decision_at)
    # Overall status is a prerequisite summary, not the close verdict. The
    # public snapshot exposes per-entry outcomes and the pure-fold hold set.
    return {key: value for key, value in report.items() if key != "status"}
