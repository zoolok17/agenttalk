"""Bound cooperative execution and close-out evidence; never an execution witness."""

from agenttalk import acceptance as A
from agenttalk.acceptance_coverage import canonical


def binding(bundle):
    return {k: bundle[k] for k in
            ("instance_id", "attempt_id", "project_id", "revision", "plan_hash", "registry_hash")}


def execution_manifest(bundle):
    """Exclude the hygiene result itself, avoiding a content-address cycle."""
    return sorted({a["sha256"] for a in bundle["artifacts"] if a["id"] != bundle["hygiene"]}
                  | {bundle["plan_hash"], bundle["registry_hash"]})


def execution_digest(bundle):
    value = dict(bundle, artifacts=[a for a in bundle["artifacts"] if a["id"] != bundle["hygiene"]])
    return A._hash(canonical(value))


def final_manifest(store, route, reconciliation):
    """Seal revealed evidence and report content, excluding only close-out's result."""
    bundle = A.decode(A._retained(store, route["bundle_hash"]))
    initial = A.decode(A._retained(store, route["cold_commit_hash"]))
    report = {k: v for k, v in reconciliation.items() if k != "closeout"}
    digests = {a["sha256"] for a in bundle["artifacts"]}
    digests.update(a["sha256"] for a in initial["delivery_manifest"])
    digests.update(route[k] for k in ("plan_hash", "registry_hash", "bundle_hash", "cold_commit_hash"))
    # Parent/amendment records are part of the final evidence set as well.
    from agenttalk import acceptance_audit
    if route.get("parent_record_hash"):
        digests.update(route[k] for k in ("parent_record_hash", "amendment_hash"))
        parent = A.decode(A._retained(store, route["parent_record_hash"]))
        for _, old_route, _ in acceptance_audit.lineage(store, parent):
            digests.update(v for k, v in old_route.items() if k.endswith("_hash") and v)
            if old_route.get("bundle_hash"):
                old_bundle = A.decode(A._retained(store, old_route["bundle_hash"]))
                digests.update(a["sha256"] for a in old_bundle["artifacts"])
            if old_route.get("cold_commit_hash"):
                old_initial = A.decode(A._retained(store, old_route["cold_commit_hash"]))
                digests.update(a["sha256"] for a in old_initial["delivery_manifest"])
    return sorted(digests), A._hash(canonical(report))


def confidentiality(value):
    A._object(value, "positive_control matches evidence", "confidentiality sweep")
    if value["positive_control"] is not True or value["matches"] != []:
        A._fail("confidentiality sweep requires a successful positive control and zero matches",
                "acceptance_record_missing")
    A._text(value["evidence"], "confidentiality sweep evidence")


def evaluate(store, record, bundle, snapshot):
    artifacts = {a["id"]: a for a in bundle["artifacts"]}

    def read(artifact_id, fields):
        value = A.decode(A._retained(store, artifacts[artifact_id]["sha256"]))
        A._object(value, "schema_version binding " + fields, "hygiene evidence")
        A._version(value["schema_version"])
        if value["binding"] != binding(bundle):
            A._fail("hygiene evidence belongs to another attempt", "acceptance_row_unbound")
        return value

    for run in bundle["runs"] + bundle["reproductions"]:
        env = read(run["environment"], "run_id version_banners scratch cache_overlay service_data "
                   "scratch_isolated cache_overlay_fresh service_data_fresh "
                   "outputs_outside_checkout services")
        offline = read(run["offline_proof"], "run_id mode egress_denied owned_loopback_only positive_control "
                       "attempted_fetch evidence")
        if env["run_id"] != run["id"] or offline["run_id"] != run["id"]:
            A._fail("environment/offline proof belongs to another run", "acceptance_row_unbound")
        A._strings(env["version_banners"], "version banners", nonempty=True)
        for key in ("scratch", "cache_overlay", "service_data"):
            A._text(env[key], key)
        if any(env[k] is not True for k in ("scratch_isolated", "cache_overlay_fresh", "service_data_fresh")):
            A._fail("scratch/cache/service isolation evidence incomplete", "acceptance_record_missing")
        if env["outputs_outside_checkout"] is not True:
            A._fail("execution outputs must be outside the checkout", "acceptance_project_unverified")
        for service in A._items(env["services"], "owned services"):
            A._object(service, "pid ports owned stopped ports_released evidence", "owned service")
            if type(service["pid"]) is not int or service["pid"] <= 0:
                A._fail("service needs its started PID")
            for port in A._items(service["ports"], "owned ports"):
                if type(port) is not int or not 0 < port < 65536:
                    A._fail("invalid owned port")
            if any(service[k] is not True for k in ("owned", "stopped", "ports_released")):
                A._fail("owned service teardown incomplete", "acceptance_record_missing")
            A._text(service["evidence"], "service start/stop and socket evidence")
        if (offline["mode"] != "external-denial" or offline["egress_denied"] is not True
                or offline["owned_loopback_only"] is not True or offline["positive_control"] is not True
                or offline["attempted_fetch"] is not False):
            A._fail("supported external offline enforcement proof missing", "acceptance_record_missing")
        A._text(offline["evidence"], "offline enforcement evidence")
    hygiene = read(bundle["hygiene"], "sealed_manifest bundle_digest retained_readable scratch_removed "
                   "services_stopped ports_released confidentiality")
    if (hygiene["sealed_manifest"] != execution_manifest(bundle)
            or hygiene["bundle_digest"] != execution_digest(bundle)):
        A._fail("execution hygiene manifest differs from bound evidence", "acceptance_row_unbound")
    if any(hygiene[k] is not True for k in
           ("retained_readable", "scratch_removed", "services_stopped", "ports_released")):
        A._fail("execution cleanup/readability evidence incomplete", "acceptance_record_missing")
    confidentiality(hygiene["confidentiality"])
    route = record["acceptance_route"]
    reconciliation = A.decode(A._retained(store, route["cold_reconcile_hash"]))
    final = reconciliation["closeout"]
    A._object(final, "sealed_manifest report_digest confidentiality", "final close-out")
    manifest, report_digest = final_manifest(store, route, reconciliation)
    if final["sealed_manifest"] != manifest or final["report_digest"] != report_digest:
        A._fail("final hygiene manifest differs from revealed evidence", "acceptance_row_unbound")
    for digest in manifest:
        A._retained(store, digest)
    confidentiality(final["confidentiality"])
    snapshot["hygiene_checked"] = True
    snapshot["hygiene"] = {"execution": artifacts[bundle["hygiene"]]["sha256"], "sealed_manifest": manifest,
                           "report_digest": report_digest}
