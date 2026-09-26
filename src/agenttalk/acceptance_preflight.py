"""Read-only staged acceptance preflight; never acquire or execute tools.

Reports contain public IDs, hashes and fixed diagnostics, never input locators or
submitted banner/log content. Current staged pins are always read afresh.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
from pathlib import Path
import re

from agenttalk import acceptance as A, acceptance_registry as R

MAX_DISTRIBUTION_BYTES = 2 * 1024**3
MAX_SCAN_BYTES = 8 * 1024**3
CHUNK_BYTES = 1024**2
UNAVAILABLE = "acceptance_preflight_unavailable"
MISMATCH = "acceptance_preflight_mismatch"
EXPIRED = "acceptance_snapshot_expired"
UNPROVEN = "acceptance_offline_unproven"
VIOLATION = "acceptance_offline_violation"
INTEGRITY = "acceptance_record_missing"
ROOT_ADVICE = "staged input unavailable; pass a fully resolved root without links or reparse ancestors"


def decision_time():
    return datetime.now(timezone.utc).replace(microsecond=0)


def hold(code, detail):
    return {"code": code, "detail": detail}


def _status(holds):
    if not holds:
        return "pass"
    return "fail" if any(h["code"] in (MISMATCH, VIOLATION, INTEGRITY, "acceptance_plan_stale")
                         for h in holds) else "not-run"


def _unique(holds):
    return [{"code": code, "detail": detail} for code, detail in
            sorted({(h["code"], h["detail"]) for h in holds})]


def read_input(path):
    """Bounded policy/observation import. Unsafe paths keep the policy refusal code."""
    path = Path(path).absolute()
    with R.staged_stream(path.parent, path.name) as stream:
        data = stream.read(R.MAX_INPUT_BYTES + 1)
    if len(data) > R.MAX_INPUT_BYTES:
        A._fail("declarative input exceeds byte limit", INTEGRITY)
    return data


def _read_pin(root, ref, budget, *, distribution=False, evidence=False):
    limit = MAX_DISTRIBUTION_BYTES if distribution else R.MAX_INPUT_BYTES
    if ref["size"] > limit or ref["size"] > budget[0]:
        return None, [hold(UNAVAILABLE if distribution else INTEGRITY, "staged input exceeds read budget")]
    digest, size, chunks = hashlib.sha256(), 0, []
    try:
        with R.staged_stream(root, ref["path"]) as stream:
            while True:
                data = stream.read(min(CHUNK_BYTES, limit - size + 1, budget[0] + 1))
                if not data:
                    break
                size += len(data)
                budget[0] -= len(data)
                if size > limit or budget[0] < 0:
                    return None, [hold(UNAVAILABLE if distribution else INTEGRITY, "staged input exceeds read budget")]
                digest.update(data)
                if not distribution:
                    chunks.append(data)
    except (A.AcceptanceError, OSError, ValueError):
        # Import-time policy refusals remain strict; an evaluation race is retryable.
        return None, [hold(UNAVAILABLE, ROOT_ADVICE)]
    if size != ref["size"] or digest.hexdigest() != ref["sha256"]:
        return None, [hold(INTEGRITY if evidence else MISMATCH, "staged size or digest differs from pin")]
    return None if distribution else b"".join(chunks), []


def _lines(data):
    try:
        # Unlike str.splitlines, only CR/LF are separators; no trimming or BOM removal.
        return set(re.split(r"\r\n|\r|\n", data.decode("utf-8")))
    except UnicodeError:
        A._fail("banner/proof capture is not strict UTF-8", INTEGRITY)


def _offline(entry, observed, lines):
    policy, proof = entry["offline"], observed["offline"]
    errors = []
    if proof["mode"] != policy["mode"]:
        errors.append(hold(UNPROVEN, "offline proof mode differs from policy"))
    if not proof["positive_control"] or policy["positive_control"] not in lines:
        errors.append(hold(UNPROVEN, "offline positive control is absent"))
    if not proof["egress_denied"] or proof["attempted_fetch"] or policy["real_fetch"] in lines:
        errors.append(hold(VIOLATION, "external egress or attempted fetch is recorded"))
    if policy["mode"] == "offline-recipe" and (not proof["cache_hit"] or policy["cache_hit"] not in lines):
        errors.append(hold(UNPROVEN, "offline recipe cache-hit proof is absent"))
    for endpoint in proof["endpoints"]:
        try:
            address = ipaddress.ip_address(endpoint["host"])
            loopback = address.is_loopback and "%" not in endpoint["host"]
        except ValueError:
            loopback = False
        if not loopback or not endpoint["owned"]:
            errors.append(hold(VIOLATION, "endpoint is external, unresolved or not owned"))
    return errors


def _environment(environment, root, registry, budget):
    errors = []
    for row in environment["row_overrides"]:
        data, issues = _read_pin(root, row["environment"], budget, evidence=True)
        errors.extend(issues)
        if data is not None:
            try:
                override = R.decode(data)
                R.validate_environment(override, overrides=False)
                R._service_refs(override, {e["id"]: e for e in registry["entries"]})
            except A.AcceptanceError:
                errors.append(hold(INTEGRITY, "row environment override is invalid"))
    return errors


def evaluate(plan_bytes, registry_bytes, cache_root, *, observation_bytes=None, proof_root=None, decision_at=None):
    """Evaluate current pins and supplied proof using one injected UTC decision time.

    The caller freezes/retains results in M3. This function performs no writes and
    does not produce an acceptance GO; every required entry must pass preflight.
    """
    plan, registry = R.policy(plan_bytes, registry_bytes)
    now = decision_at if decision_at is not None else decision_time()
    if not isinstance(now, datetime) or now.utcoffset() != timedelta(0) or now.microsecond:
        A._fail("decision clock must be UTC at whole-second precision")
    proof_root = cache_root if proof_root is None else proof_root
    observed, common = None, []
    if observation_bytes is not None:
        try:
            observed = R.validate_observation(R.decode(observation_bytes), registry)
        except A.AcceptanceError:
            common.append(hold(INTEGRITY, "preflight observation is invalid"))
        if observed is not None:
            if observed["registry_hash"] != A._hash(registry_bytes):
                common.append(hold("acceptance_plan_stale", "observation registry binding differs"))
            if R.utc(observed["observed_at"]) > now:
                common.append(hold(MISMATCH, "observation time is after decision time"))
            if observed["environment"] != plan["environment"]:
                common.append(hold(MISMATCH, "observed environment differs from plan"))
    elif registry["entries"]:
        common.append(hold(UNPROVEN, "preflight observation and offline proof are absent"))
    proof_budget = [R.MAX_TOTAL_BYTES]
    common.extend(_environment(plan["environment"], cache_root, registry, [R.MAX_TOTAL_BYTES]))
    if observed is not None:
        common.extend(_environment(observed["environment"], proof_root, registry, proof_budget))
    observation_time = R.utc(observed["observed_at"]) if observed is not None else now
    files, file_errors, manifests = {}, {}, []
    distribution_budget, declarative_budget = [MAX_SCAN_BYTES], [R.MAX_TOTAL_BYTES]
    for pin in registry["files"]:
        distribution = pin["role"] == "distribution"
        _, issues = _read_pin(cache_root, pin, distribution_budget if distribution else declarative_budget,
                              distribution=distribution)
        if pin["provenance"] is not None and R.utc(pin["provenance"]["retrieved_at"]) > observation_time:
            issues.append(hold(MISMATCH, "provenance retrieval is after observation time"))
        if pin["role"] == "snapshot":
            if now >= R.utc(pin["expires_at"]):
                issues.append(hold(EXPIRED, "snapshot is expired at decision time"))
            manifests.append(pin)
        file_errors[pin["id"]] = issues
        files[pin["id"]] = {"id": pin["id"], "sha256": pin["sha256"], "size": pin["size"],
                            "status": _status(issues), "retention": "pinned-only" if distribution else "declarative"}
    inputs = R.entry_inputs(registry)
    # Every consumer inherits all manifests for a distribution, not only its own list.
    for required in inputs.values():
        for manifest in manifests:
            if manifest["distribution"]["id"] in required:
                required.add(manifest["id"])
                required.update(R._provenance(manifest["provenance"],
                                             {pin["id"]: pin for pin in registry["files"]}))
    observations = {entry["id"]: entry for entry in observed["entries"]} if observed is not None else {}
    results = {}
    for entry in registry["entries"]:
        issues = list(common)
        for ref in sorted(inputs[entry["id"]]):
            issues.extend(file_errors[ref])
        actual = observations.get(entry["id"])
        if actual is not None:
            if actual["version"] != entry["version"]:
                issues.append(hold(MISMATCH, "observed entry version differs from pin"))
            banner, errors = _read_pin(proof_root, actual["banner"], proof_budget, evidence=True)
            issues.extend(errors)
            log, errors = _read_pin(proof_root, actual["offline"]["log"], proof_budget, evidence=True)
            issues.extend(errors)
            try:
                if banner is not None:
                    banner_lines = _lines(banner)
                    if entry["expected_banner"] is not None and entry["expected_banner"] not in banner_lines:
                        issues.append(hold(MISMATCH, "expected banner is absent as a whole line"))
                issues.extend(_offline(entry, actual, _lines(log) if log is not None else set()))
            except A.AcceptanceError:
                issues.append(hold(INTEGRITY, "banner/proof capture is not strict UTF-8"))
        results[entry["id"]] = {"id": entry["id"], "holds": _unique(issues)}
    # Dependent entries also require their tools' execution/offline prerequisites.
    definitions = {entry["id"]: entry for entry in registry["entries"]}
    resolved = {}

    def inherited(key):
        if key in resolved:
            return resolved[key]
        errors = list(results[key]["holds"])
        for dep in definitions[key]["dependencies"]:
            errors.extend(inherited(dep))
        resolved[key] = _unique(errors)
        return resolved[key]

    for key in results:
        results[key]["holds"] = inherited(key)
        results[key]["status"] = _status(results[key]["holds"])
    rows = []
    for row in plan["rows"]:
        issues = _unique(common + [h for key in row["registry_entries"] for h in results[key]["holds"]])
        rows.append({"id": row["id"], "policy": row["policy"], "status": _status(issues), "holds": issues})
    all_holds = _unique(common + [h for result in results.values() for h in result["holds"]]
                        + [h for issues in file_errors.values() for h in issues])
    return {"schema_version": 1, "plan_hash": A._hash(plan_bytes), "registry_hash": A._hash(registry_bytes),
            "observation_hash": A._hash(observation_bytes) if observation_bytes is not None else None,
            "decision_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": _status(all_holds),
            "entries": list(results.values()), "rows": rows, "files": list(files.values()), "holds": all_holds}
