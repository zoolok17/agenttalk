"""Synthetic record and filesystem tests; no repositories or staged tools run."""

from pathlib import Path
from copy import deepcopy
import json
import os
import socket
import stat
import traceback
# Imported only to deny execution in the preflight-reader boundary test.
import subprocess  # nosec B404

import pytest

from agenttalk import acceptance as A, acceptance_registry as R

HASH = "a" * 64


def test_m1b_two_runtime_pins_require_exact_banners_and_environment_use(registry, plan):
    java = registry["entries"][0]
    java["expected_banner"] = "java 21.0.1"
    node = dict(deepcopy(java), id="node", expected_banner="node 22.0.0")
    registry["entries"].append(node)
    java["dependencies"] = ["node"]
    plan["environment"].update(runtime=["java", "node"], compiler=[], package_manager=[])
    assert R.validate_plan(plan, registry) == plan
    for value in (["java"], ["java", "python 3.1"], ["java", "java", "node"]):
        broken = deepcopy(plan)
        broken["environment"]["runtime"] = value
        with pytest.raises(A.AcceptanceError):
            R.validate_plan(broken, registry)
    for banner in (None, "", 21):
        node["expected_banner"] = banner
        with pytest.raises(A.AcceptanceError):
            R.validate_registry(registry)


@pytest.mark.parametrize("path", ["COM0", "LPT0.txt", "COM\u00b9.dat", "LPT\u00b2", "COM\u00b3",
                                  "CONIN$", "CONOUT$.txt", "nul .txt"])
def test_m1b_reserved_devices(path):
    with pytest.raises(A.AcceptanceError):
        R.relative_path(path)


@pytest.mark.parametrize("paths", [("a.dat", "a.dat/child"), ("A.DAT/child", "a.dat"),
                                   ("\u00e9.dat", "e\u0301.dat")])
def test_m1b_portable_path_collisions(registry, paths):
    registry["files"][0]["path"], registry["files"][1]["path"] = paths
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


def test_m1b_long_id_diagnostic_is_bounded():
    with pytest.raises(A.AcceptanceError) as error:
        A._id("x" * 200_000)
    assert len(str(error.value)) < 200
    assert len("".join(traceback.format_exception(error.type, error.value, error.tb))) < 3000


@pytest.mark.parametrize("field,replacement", [(1, None), (2, None), (0, stat.S_IFIFO | 0o600)])
def test_m1b_opened_file_identity_must_match_lstat(tmp_path, registry, monkeypatch, field, replacement):
    (tmp_path / "source.dat").write_bytes(b"{}")
    original = os.fstat

    def swapped(fd):
        before = original(fd)
        fields = list(before)
        fields[field] = replacement if replacement is not None else fields[field] + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", swapped)
    with pytest.raises(A.AcceptanceError, match="changed"):
        R.read_declarative_inputs(tmp_path, registry)


def test_m1b_os_error_traceback_hides_private_root(tmp_path, registry, monkeypatch):
    (tmp_path / "source.dat").write_bytes(b"{}")

    def denied(*args, **kwargs):
        raise PermissionError("private-cache-locator")

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(A.AcceptanceError) as error:
        R.read_declarative_inputs(tmp_path, registry)
    rendered = "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert "PermissionError: private-cache-locator" not in rendered
    assert error.value.__context__ is None


def test_m1b_large_distribution_has_small_expiring_manifest(registry):
    registry["files"].append(pin("advisory-content"))
    registry["files"][-1]["size"] = R.MAX_INPUT_BYTES * 100
    manifest = pin("advisory-manifest", "snapshot")
    manifest["distribution"] = {"id": "advisory-content", "sha256": HASH}
    registry["files"].append(manifest)
    registry["entries"][0]["snapshots"] = ["advisory-manifest"]
    registry["entries"][0]["inputs"] = ["advisory-content"]
    assert R.validate_registry(registry) == registry
    for key, value in (("id", "missing"), ("id", "source"), ("sha256", "b" * 64)):
        broken = deepcopy(registry)
        broken["files"][-1]["distribution"][key] = value
        with pytest.raises(A.AcceptanceError):
            R.validate_registry(broken)


def provenance():
    return {"source": "vendor:java:21.0.1", "retrieved_at": "2026-01-01T00:00:00Z",
            "checksum_source": "vendor:checksums", "independent_verification": False,
            "record": "source", "verification": None}


def pin(fid, role="distribution"):
    result = {"id": fid, "role": role, "path": fid + ".dat", "sha256": HASH, "size": 2,
            "expires_at": "2027-01-01T00:00:00Z" if role == "snapshot" else None,
            "version": "21.0.1" if role in ("distribution", "snapshot") else None,
            "provenance": provenance() if role in ("distribution", "snapshot") else None}
    if role == "snapshot":
        result["distribution"] = {"id": "jdk", "sha256": HASH}
    return result


@pytest.fixture
def registry():
    return {"schema_version": 2, "files": [pin("jdk"), pin("source", "provenance")], "entries": [{
        "id": "java", "kind": "toolchain", "version": "21.0.1", "artifact": "jdk", "dependencies": [],
        "expected_banner": "java 21.0.1",
        "inputs": [], "snapshots": [],
        "provenance": provenance(),
        "command": {"argv": ["{artifact}", "--version"], "cwd": "{checkout}", "inputs": [], "outputs": []},
        "offline": {"mode": "external-denial", "positive_control": "DENIAL_OK", "cache_hit": None,
                    "real_fetch": "FETCH"},
        "failure_policy": {"unavailable": "not-run", "mismatch": "fail", "expired": "not-run",
                           "absent_proof": "not-run", "attempted_fetch": "fail"}, "measurement": None}]}


@pytest.fixture
def environment():
    return {"schema_version": 1, "runtime": ["java"], "compiler": [],
            "package_manager": [], "services": [], "os": "synthetic", "locale": "C", "timezone": "UTC",
            "environment_digest": HASH, "config_digest": HASH, "scratch": "isolated",
            "cache_overlay": "fresh-writable", "service_data": "fresh", "time_limit_seconds": 60,
            "memory_limit_bytes": 1024, "row_overrides": []}


@pytest.fixture
def plan(environment):
    return {"schema_version": 4, "plan_id": "plan", "project_id": "project", "scope": "milestone",
            "authors": ["author"], "partitions": [{"id": "checks", "agents": ["runner"]}],
            "rows": [{"id": "build", "partition": "checks", "policy": "gating", "comparator": "exit-code",
                      "expected": 0, "artifact": "raw", "field": "exit_code", "registry_entries": ["java"]}],
            "registry_ref": "registry.json", "registry_digest": HASH, "trust_profile": "cooperative",
            "cold_policy": {"reviewer": "cold", "roster": [{"actor": "cold", "vendor": "vendor"}],
                            "absence_disclosure": "synthetic", "change_base": "b" * 40}, "environment": environment}


@pytest.fixture
def observation(environment):
    return {"schema_version": 1, "registry_hash": HASH, "observed_at": "2026-01-02T00:00:00Z",
            "environment": environment, "entries": [{"id": "java", "version": "21.0.1",
                "banner": {"path": "banner.txt", "sha256": HASH, "size": 2},
                "offline": {"mode": "external-denial", "egress_denied": True, "positive_control": True,
                            "cache_hit": False, "attempted_fetch": False, "endpoints": [],
                            "log": {"path": "offline.log", "sha256": HASH, "size": 2}}}]}


def change(value, path, replacement):
    parts = path.split(".")
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    value[int(parts[-1]) if isinstance(value, list) else parts[-1]] = replacement


def test_preflight_path_escape_rejected(tmp_path, monkeypatch):
    root = tmp_path / "redirect" / "cache"
    root.mkdir(parents=True)
    (root / "manifest.json").write_bytes(b"{}")
    original = Path.is_symlink

    def linked(path):
        # Model an ancestor junction even on hosts without symlink privileges.
        return path == root.parent or original(path)

    monkeypatch.setattr(Path, "is_symlink", linked)
    with pytest.raises(A.AcceptanceError) as error:
        R.staged_path(root, "manifest.json")
    assert error.value.code == "acceptance_policy_invalid"


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "a\\b", "a/../b", "a/./b",
                                  "a//b", "a/", "a.", ".. /secret", "NUL", "COM1.txt", "a:stream",
                                  "a?", "a*", "a\nfile", "", "x" * 513])
def test_nonportable_paths_refused(tmp_path, path):
    with pytest.raises(A.AcceptanceError):
        R.staged_path(tmp_path, path)


def test_real_staged_link_refused(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"private")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not allow symlink creation")
    with pytest.raises(A.AcceptanceError):
        R.staged_path(tmp_path, "link")


def test_registry_environment_plan_and_observation_roundtrip(registry, environment, plan, observation):
    assert R.validate_registry(R.decode(json.dumps(registry).encode())) == registry
    assert R.validate_environment(environment) == environment
    before = deepcopy(plan)
    assert R.validate_plan(plan, registry) == before
    assert R.validate_observation(observation, registry) == observation


@pytest.mark.parametrize("data", [b'\xff', b'\xef\xbb\xbf{}', b'{"a":1,"a":2}',
                                  b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":Infinity}',
                                  b'{"x":1e999}', b'"\\ud800"', b'{} garbage', b'['])
def test_strict_json_refuses_ambiguous_bytes(data):
    with pytest.raises(A.AcceptanceError):
        R.decode(data)


def test_decoder_limits_and_quoted_brackets(monkeypatch):
    assert R.decode(b'{"x":"[\\\"{"}') == {"x": '["{'}
    assert R.decode(b"[" * R.MAX_DEPTH + b"0" + b"]" * R.MAX_DEPTH)
    with pytest.raises(A.AcceptanceError):
        R.decode(b"[" * (R.MAX_DEPTH + 1) + b"0" + b"]" * (R.MAX_DEPTH + 1))
    monkeypatch.setattr(R, "MAX_INPUT_BYTES", 2)
    assert R.decode(b"{}") == {}
    with pytest.raises(A.AcceptanceError):
        R.decode(b"{} ")
    monkeypatch.setattr(R, "MAX_INPUT_BYTES", 1024)
    monkeypatch.setattr(R, "MAX_NODES", 3)
    assert R.decode(b"[0,1]") == [0, 1]
    with pytest.raises(A.AcceptanceError):
        R.decode(b"[0,1,2]")


@pytest.mark.parametrize("path,value", [
    ("schema_version", True), ("schema_version", 2.0), ("schema_version", 3), ("extra", 1),
    ("entries.0.extra", None), ("files.0.extra", None), ("files.0.size", True), ("files.0.size", -1),
    ("files.0.sha256", "A" * 64), ("files.0.path", "../outside"), ("files.0.role", "directory"),
    ("files.0.expires_at", "2027-01-01T00:00:00Z"), ("entries.0.kind", "package"),
    ("entries.0.artifact", "missing"), ("entries.0.version", ""), ("entries.0.version", 21),
    ("entries.0.dependencies", ["missing"]), ("entries.0.dependencies", ["java"]),
    ("entries.0.inputs", ["source", "source"]), ("entries.0.inputs", ["absent"]),
    ("entries.0.snapshots", ["jdk"]), ("entries.0.provenance.extra", "ignored"),
    ("entries.0.provenance.independent_verification", 1),
    ("entries.0.provenance.independent_verification", True),
    ("entries.0.provenance.retrieved_at", "2026-1-1T00:00:00Z"),
    ("entries.0.command.argv", "tool --flag"), ("entries.0.command.argv", []),
    ("entries.0.command.argv", ["java"]), ("entries.0.command.argv", ["{artifact}", "{unknown}"]),
    ("entries.0.command.argv", ["{artifact}", "{checkout}/../secret"]),
    ("entries.0.command.argv", ["{artifact}", "/etc/config"]),
    ("entries.0.command.cwd", "elsewhere"), ("entries.0.command.outputs", ["outside"]),
    ("entries.0.offline.mode", "assumed"), ("entries.0.offline.real_fetch", "DENIAL_OK"),
    ("entries.0.offline.cache_hit", "HIT"), ("entries.0.offline.mode", "offline-recipe"),
    ("entries.0.failure_policy.mismatch", "pass"), ("entries.0.measurement", {}),
])
def test_registry_closed_fields_and_references(registry, path, value):
    change(registry, path, value)
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


def test_checker_pins_snapshots_and_independent_provenance(registry):
    entry = registry["entries"][0]
    entry["kind"] = "checker"
    entry["expected_banner"] = None
    registry["files"].extend([pin("adapter", "adapter"), pin("config", "config"),
                              pin("advisories", "snapshot"), pin("verified", "verification")])
    entry["measurement"] = {"comparator": "adapter", "parser": "adapter", "config": "config", "normalizer": "adapter"}
    entry["snapshots"] = ["advisories"]
    entry["provenance"].update(independent_verification=True, verification="verified")
    registry["files"][0]["provenance"] = deepcopy(entry["provenance"])
    entry["offline"].update(mode="offline-recipe", cache_hit="CACHE_HIT")
    entry["command"]["outputs"] = ["{scratch}/output.json"]
    assert R.validate_registry(registry) == registry
    entry["measurement"]["normalizer"] = "missing"
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


@pytest.mark.parametrize("fault", ["duplicate-id", "case-path", "unused", "size", "total"])
def test_file_manifest_integrity(registry, monkeypatch, fault):
    if fault == "duplicate-id":
        registry["files"].append(deepcopy(registry["files"][0]))
    elif fault == "case-path":
        registry["files"][1]["path"] = registry["files"][0]["path"].upper()
    elif fault == "unused":
        registry["files"].append(pin("unused"))
    elif fault == "size":
        registry["files"][1]["size"] = R.MAX_INPUT_BYTES + 1
    else:
        monkeypatch.setattr(R, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


@pytest.mark.parametrize("field", ["role", "path", "sha256", "size", "expires_at", "version", "provenance"])
def test_forward_referenced_file_requires_complete_shape(registry, field):
    del registry["files"][1][field]
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


def test_plan_one_direction_mapping_and_transitive_use(registry, plan):
    dependency = deepcopy(registry["entries"][0])
    dependency["id"] = "dependency"
    registry["entries"].append(dependency)
    registry["entries"][0]["dependencies"] = ["dependency"]
    plan["environment"]["runtime"].append("dependency")
    assert R.validate_plan(plan, registry) == plan
    registry["entries"][0]["dependencies"] = []
    with pytest.raises(A.AcceptanceError, match="unused registry entry"):
        R.validate_plan(plan, registry)


@pytest.mark.parametrize("refs", [None, ["absent"], ["java", "java"], []])
def test_plan_missing_dangling_duplicate_or_unused_refs_hold(plan, registry, refs):
    if refs is None:
        del plan["rows"][0]["registry_entries"]
    else:
        plan["rows"][0]["registry_entries"] = refs
    with pytest.raises(A.AcceptanceError):
        R.validate_plan(plan, registry)


def test_explicit_tool_free_plan_and_legacy_refusal(plan):
    registry = {"schema_version": 2, "files": [], "entries": []}
    plan["rows"][0]["registry_entries"] = []
    plan["environment"]["runtime"] = []
    assert R.validate_plan(plan, registry) == plan
    with pytest.raises(A.AcceptanceError):
        A.validate_plan(plan)
    with pytest.raises(A.AcceptanceError):
        A.validate_registry(registry)
    assert A.validate_registry({"schema_version": 1, "entries": []})["schema_version"] == 1


def test_dependency_depth_limit_in_both_orders(registry, monkeypatch):
    entry = registry["entries"][0]
    registry["entries"] = [dict(deepcopy(entry), id=f"e{i}", dependencies=[f"e{i-1}"] if i else [])
                           for i in range(4)]
    monkeypatch.setattr(R, "MAX_DEPTH", 4)
    R.validate_registry(registry)
    monkeypatch.setattr(R, "MAX_DEPTH", 3)
    for _ in range(2):
        with pytest.raises(A.AcceptanceError):
            R.validate_registry(registry)
        registry["entries"].reverse()


@pytest.mark.parametrize("path,value", [
    ("schema_version", True), ("extra", 1), ("runtime", 21), ("environment_digest", "bad"),
    ("scratch", "shared"), ("cache_overlay", "immutable"), ("service_data", "reused"),
    ("time_limit_seconds", 0), ("time_limit_seconds", True), ("time_limit_seconds", 86401),
    ("memory_limit_bytes", 2**40 + 1), ("services", [{"id": "svc", "banner": ""}]),
    ("row_overrides", [{"id": "r", "environment": {"path": "../x", "sha256": HASH, "size": 1}}]),
])
def test_environment_is_closed_and_bounded(environment, path, value):
    change(environment, path, value)
    with pytest.raises(A.AcceptanceError):
        R.validate_environment(environment)


@pytest.mark.parametrize("timestamp", ["2026-01-01", "2026-01-01T00:00:00+00:00", "2026-02-30T00:00:00Z",
                                       "2026-01-01T00:00:00.1Z", "2026-01-01T00:00:60Z"])
def test_timestamps_require_exact_utc(timestamp):
    with pytest.raises(A.AcceptanceError):
        R.utc(timestamp)


@pytest.mark.parametrize("path,value", [
    ("schema_version", 1.0), ("registry_hash", "bad"), ("entries", []),
    ("entries.0.offline.extra", True), ("entries.0.offline.egress_denied", 1),
    ("entries.0.banner.path", "../banner"), ("entries.0.banner.size", R.MAX_INPUT_BYTES + 1),
    ("entries.0.offline.endpoints", [{"host": "127.0.0.1", "port": 0, "pid": 1, "owned": True}]),
])
def test_observation_shape_rejected(registry, observation, path, value):
    change(observation, path, value)
    with pytest.raises(A.AcceptanceError):
        R.validate_observation(observation, registry)


def test_negative_proof_observations_are_data_not_schema_success(registry, observation):
    proof = observation["entries"][0]["offline"]
    proof.update(positive_control=False, attempted_fetch=True, egress_denied=False)
    proof["endpoints"] = [{"host": "external.invalid", "port": 443, "pid": 1, "owned": False}]
    assert R.validate_observation(observation, registry) == observation
    # M2 must map these observations to HOLD; M1 makes no verdict claim.


def test_declarative_reader_never_reads_distribution_or_launches_tools(tmp_path, registry, monkeypatch):
    (tmp_path / "source.dat").write_bytes(b"{}")
    original = os.open

    def checked(path, *args, **kwargs):
        assert Path(path).name != "jdk.dat", "distribution was opened by declarative reader"
        return original(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("preflight record reader attempted process/network execution")

    monkeypatch.setattr(os, "open", checked)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert R.read_declarative_inputs(tmp_path, registry) == {"source": b"{}"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.dat"]


@pytest.mark.parametrize("fault", ["missing", "directory", "oversize", "aggregate"])
def test_declarative_io_is_bounded_and_diagnostic_hides_root(tmp_path, registry, monkeypatch, fault):
    source = tmp_path / "source.dat"
    if fault == "directory":
        source.mkdir()
    elif fault != "missing":
        source.write_bytes(b"12345")
        if fault == "oversize":
            monkeypatch.setattr(R, "MAX_INPUT_BYTES", 4)
        else:
            monkeypatch.setattr(R, "MAX_TOTAL_BYTES", 4)
    with pytest.raises(A.AcceptanceError) as error:
        R.read_declarative_inputs(tmp_path, registry)
    assert str(tmp_path) not in str(error.value)
    assert tmp_path.as_posix() not in str(error.value)


def test_policy_import_binds_exact_registry_bytes(plan, registry):
    data = json.dumps(registry).encode()
    plan["registry_digest"] = A._hash(data)
    plan_bytes = json.dumps(plan).encode()
    assert R.policy(plan_bytes, data) == (plan, registry)
    with pytest.raises(A.AcceptanceError) as error:
        R.policy(plan_bytes, data + b"\n")
    assert error.value.code == "acceptance_plan_stale"


@pytest.mark.parametrize("limit, accepted", [("MAX_ENTRIES", 1), ("MAX_FILES", 2),
                                            ("MAX_ARGV", 2), ("MAX_EDGES", 0)])
def test_registry_count_limits_at_boundary(registry, monkeypatch, limit, accepted):
    if limit == "MAX_EDGES":
        dependency = deepcopy(registry["entries"][0])
        dependency["id"] = "dependency"
        registry["entries"].append(dependency)
        registry["entries"][0]["dependencies"] = ["dependency"]
        accepted = 1
    monkeypatch.setattr(R, limit, accepted)
    R.validate_registry(registry)
    monkeypatch.setattr(R, limit, accepted - 1)
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


def test_aggregate_read_counts_actual_bytes_not_declared_size(tmp_path, registry, monkeypatch):
    registry["files"].append(pin("config", "config"))
    registry["entries"][0]["inputs"] = ["config"]
    (tmp_path / "source.dat").write_bytes(b"123")
    (tmp_path / "config.dat").write_bytes(b"456")
    monkeypatch.setattr(R, "MAX_TOTAL_BYTES", 6)
    assert R.read_declarative_inputs(tmp_path, registry) == {"source": b"123", "config": b"456"}
    monkeypatch.setattr(R, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(A.AcceptanceError, match="byte budget"):
        R.read_declarative_inputs(tmp_path, registry)


def test_missing_root_and_permission_errors_do_not_expose_locator(tmp_path, registry, monkeypatch):
    with pytest.raises(A.AcceptanceError) as error:
        R.read_declarative_inputs(tmp_path / "private-missing", registry)
    assert error.value.code == "acceptance_preflight_unavailable"
    (tmp_path / "source.dat").write_bytes(b"{}")

    def denied(*args, **kwargs):
        raise PermissionError(str(tmp_path / "sensitive"))

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(A.AcceptanceError) as error:
        R.read_declarative_inputs(tmp_path, registry)
    assert str(tmp_path) not in str(error.value)
    assert "sensitive" not in str(error.value)


def test_row_overrides_bind_rows_and_external_bytes(plan, registry):
    plan["environment"]["row_overrides"] = [{"id": "build", "environment": {
        "path": "override.json", "sha256": HASH, "size": 32}}]
    R.validate_plan(plan, registry)
    plan["environment"]["row_overrides"][0]["id"] = "absent"
    with pytest.raises(A.AcceptanceError):
        R.validate_plan(plan, registry)


def test_service_environment_references_are_typed(plan, registry, observation):
    plan["environment"].update(services=["java"], runtime=[])
    with pytest.raises(A.AcceptanceError):
        R.validate_plan(plan, registry)
    with pytest.raises(A.AcceptanceError):
        R.validate_observation(observation, registry)
    registry["entries"][0]["kind"] = "service"
    R.validate_plan(plan, registry)
    R.validate_observation(observation, registry)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_guard_m20_real_junction_above_staging_root(tmp_path):
    target = tmp_path / "real"
    (target / "cache").mkdir(parents=True)
    (target / "cache" / "proof").write_bytes(b"ok")
    junction = tmp_path / "junction"
    # Fixed OS helper; both paths are isolated fixture directories, no staged tool.
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                            capture_output=True, check=False)  # nosec B603 B607
    assert result.returncode == 0, result.stderr
    with pytest.raises(A.AcceptanceError, match="link/reparse"):
        R.staged_path(junction / "cache", "proof")


@pytest.mark.parametrize("version", [True, 4.0, 3, 5])
def test_guard_m69_plan_version(plan, registry, version):
    plan["schema_version"] = version
    with pytest.raises(A.AcceptanceError, match="schema_version"):
        R.validate_plan(plan, registry)


def test_guard_m55_snapshot_provenance(registry):
    snapshot = pin("advisories", "snapshot")
    snapshot["provenance"]["retrieved_at"] = "yesterday"
    registry["files"].append(snapshot)
    registry["entries"][0]["snapshots"] = ["advisories"]
    with pytest.raises(A.AcceptanceError, match="UTC"):
        R.validate_registry(registry)


def test_guard_m29_unrequested_verification(registry):
    for obj in (registry["entries"][0], registry["files"][0]):
        obj["provenance"]["verification"] = "source"
    with pytest.raises(A.AcceptanceError, match="independent_verification"):
        R.validate_registry(registry)


@pytest.mark.parametrize("field,value", [("version", "v1"), ("provenance", provenance())])
def test_guard_m25_non_distribution_metadata(registry, field, value):
    registry["files"][1][field] = value
    with pytest.raises(A.AcceptanceError, match="only distributions/snapshots"):
        R.validate_registry(registry)


@pytest.mark.parametrize("outputs", [["xxxxxxxxxxsafe"], ["{scratch}/../escape"],
                                     ["{scratch}/a", "{scratch}/a"]], ids=["M37", "M38", "M39"])
def test_guard_template_outputs(registry, outputs):
    registry["entries"][0]["command"]["outputs"] = outputs
    with pytest.raises(A.AcceptanceError):
        R.validate_registry(registry)


def test_guard_m05_utc_requires_zero_padding():
    with pytest.raises(A.AcceptanceError, match="canonical UTC"):
        R.utc("2026-1-1T00:00:00Z")


@pytest.mark.parametrize("field,value", [("version", "99"), ("provenance.source", "another-vendor")],
                         ids=["M47", "M46"])
def test_guard_artifact_entry_exact_agreement(registry, field, value):
    change(registry["files"][0], field, value)
    with pytest.raises(A.AcceptanceError, match="differs from its artifact"):
        R.validate_registry(registry)


def test_guard_m08_escaped_quote_does_not_hide_nesting(monkeypatch):
    monkeypatch.setattr(R, "MAX_DEPTH", 1)
    with pytest.raises(A.AcceptanceError, match="nesting"):
        R.decode(b'["\\\"",[]]')


def test_guard_m09_json_keys_count_towards_node_budget(monkeypatch):
    monkeypatch.setattr(R, "MAX_NODES", 2)
    with pytest.raises(A.AcceptanceError, match="node count"):
        R.decode(b'{"key":0}')


def test_guard_m12_decoder_requires_bytes():
    with pytest.raises(A.AcceptanceError):
        R.decode(bytearray(b"{}"))


def test_guard_m14_pipe_in_relative_path():
    with pytest.raises(A.AcceptanceError):
        R.relative_path("a|b")


def test_guard_m43_proof_marker_limit(registry):
    registry["entries"][0]["offline"]["positive_control"] = "x" * (R.MAX_MARKER + 1)
    with pytest.raises(A.AcceptanceError, match="marker exceeds"):
        R.validate_registry(registry)


def test_guard_m51_checker_config_role(registry):
    entry = registry["entries"][0]
    entry.update(kind="checker", expected_banner=None)
    registry["files"].append(pin("adapter", "adapter"))
    entry["measurement"] = dict.fromkeys(("comparator", "parser", "config", "normalizer"), "adapter")
    with pytest.raises(A.AcceptanceError, match="wrong role"):
        R.validate_registry(registry)


@pytest.mark.parametrize("fault", ["M66", "M67"])
def test_guard_override_limits(environment, monkeypatch, fault):
    environment["row_overrides"] = [{"id": "build", "environment": {
        "path": "override.json", "sha256": HASH, "size": 2}}]
    if fault == "M67":
        monkeypatch.setattr(R, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(A.AcceptanceError):
        R.validate_environment(environment, overrides=fault != "M66")


def test_guard_m70_registry_ref_portability(plan, registry):
    plan["registry_ref"] = "../registry.json"
    with pytest.raises(A.AcceptanceError):
        R.validate_plan(plan, registry)


@pytest.mark.parametrize("fault", ["M82", "M85", "M86", "M87"])
def test_guard_observation_proof_contract(observation, registry, monkeypatch, fault):
    proof = observation["entries"][0]["offline"]
    if fault == "M82":
        proof["mode"] = "assumed"
    elif fault == "M87":
        monkeypatch.setattr(R, "MAX_TOTAL_BYTES", 3)
    else:
        proof["endpoints"] = [{"host": "127.0.0.1", "port": 1,
                               "pid": 0 if fault == "M85" else 1,
                               "owned": 1 if fault == "M86" else True}]
    with pytest.raises(A.AcceptanceError):
        R.validate_observation(observation, registry)


def test_guard_m90_non_regular_file_rejected_before_open(tmp_path, registry, monkeypatch):
    source = tmp_path / "source.dat"
    source.write_bytes(b"{}")
    original = Path.lstat

    def nonregular(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == source:
            fields = list(result)
            fields[0] = stat.S_IFIFO | 0o600
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", nonregular)
    # Isolate the regular-file guard from the separately tested path resolver.
    monkeypatch.setattr(R, "staged_path", lambda root, relative: source)
    with pytest.raises(A.AcceptanceError, match="must be a regular file"):
        R.read_declarative_inputs(tmp_path, registry)


def test_guard_m94_unreadable_file_has_unavailable_code(tmp_path, registry, monkeypatch):
    (tmp_path / "source.dat").write_bytes(b"{}")

    def denied(*args, **kwargs):
        raise PermissionError("private-cache-locator")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(A.AcceptanceError) as error:
        R.read_declarative_inputs(tmp_path, registry)
    assert error.value.code == "acceptance_preflight_unavailable"
    assert error.value.__context__ is None


def test_guard_m97_json_key_unicode():
    with pytest.raises(A.AcceptanceError, match="invalid Unicode"):
        R.decode(b'{"\\ud800":0}')


def test_registry_role_and_kind_refuse_otherwise_valid_shape(registry):
    unknown = pin("unknown", "provenance")
    unknown["role"] = "unknown"
    with pytest.raises(A.AcceptanceError, match="unsupported staged file role"):
        R.file_pin(unknown)
    registry["entries"][0].update(kind="unknown", expected_banner=None)
    with pytest.raises(A.AcceptanceError, match="unsupported registry kind"):
        R.validate_registry(registry)


def test_whole_line_markers_may_contain_each_other(registry):
    registry["entries"][0]["offline"].update(mode="offline-recipe", cache_hit="ok",
                                             positive_control="cache ok", real_fetch="fetch ok")
    assert R.validate_registry(registry) == registry


@pytest.mark.parametrize("role", ["runtime", "compiler", "package_manager", "services"])
def test_environment_role_count_boundary(environment, monkeypatch, role):
    environment[role] = ["java", "node"]
    monkeypatch.setattr(R, "MAX_ENTRIES", 2)
    R.validate_environment(environment)
    monkeypatch.setattr(R, "MAX_ENTRIES", 1)
    with pytest.raises(A.AcceptanceError, match="bounded list"):
        R.validate_environment(environment)


def test_file_replaced_between_lstat_and_open_is_refused(tmp_path, registry, monkeypatch):
    source = tmp_path / "source.dat"
    source.write_bytes(b"{}")
    original = os.open

    def swapped(path, flags, *args, **kwargs):
        assert Path(path) == source
        source.rename(tmp_path / "previous.dat")
        source.write_bytes(b"different")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped)
    with pytest.raises(A.AcceptanceError, match="changed"):
        R.read_declarative_inputs(tmp_path, registry)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO/no-follow flags")
@pytest.mark.parametrize("replacement", ["fifo", "link"])
def test_posix_leaf_swap_never_blocks_or_follows(tmp_path, registry, monkeypatch, replacement):
    source = tmp_path / "source.dat"
    source.write_bytes(b"{}")
    original = os.open

    def swapped(path, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        source.rename(tmp_path / "previous.dat")
        if replacement == "fifo":
            os.mkfifo(source)
        else:
            source.symlink_to(tmp_path / "previous.dat")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped)
    with pytest.raises(A.AcceptanceError):
        R.read_declarative_inputs(tmp_path, registry)


@pytest.mark.parametrize("reference", [None, {"id": "jdk", "sha256": HASH, "extra": 0}])
def test_snapshot_reference_closed_shape(registry, reference):
    manifest = pin("manifest", "snapshot")
    manifest["distribution"] = reference
    registry["files"].append(manifest)
    registry["entries"][0]["snapshots"] = ["manifest"]
    with pytest.raises(A.AcceptanceError, match="snapshot distribution pin"):
        R.validate_registry(registry)


def test_snapshot_reference_requires_distribution_role(registry):
    manifest = pin("manifest", "snapshot")
    manifest["distribution"] = {"id": "source", "sha256": HASH}
    registry["files"].append(manifest)
    registry["entries"][0]["snapshots"] = ["manifest"]
    with pytest.raises(A.AcceptanceError, match="wrong role"):
        R.validate_registry(registry)


@pytest.mark.parametrize("fault", ["duplicate-role", "unknown-entry"])
def test_environment_role_references_refuse_structurally(plan, registry, fault):
    plan["environment"]["compiler"] = ["java" if fault == "duplicate-role" else "unknown"]
    with pytest.raises(A.AcceptanceError, match="environment entry reference"):
        R.validate_plan(plan, registry)


def test_checker_forbids_banner_even_with_valid_measurement(registry):
    registry["files"].extend([pin("adapter", "adapter"), pin("config", "config")])
    registry["entries"][0].update(kind="checker", measurement={
        "comparator": "adapter", "parser": "adapter", "normalizer": "adapter", "config": "config"})
    with pytest.raises(A.AcceptanceError, match="expected_banner"):
        R.validate_registry(registry)


def test_post_open_lstat_identity_and_descriptor_close(tmp_path, registry, monkeypatch):
    source = tmp_path / "source.dat"
    source.write_bytes(b"{}")
    original_open, original_stat = os.open, Path.lstat
    descriptors = []

    def opened(path, flags):
        fd = original_open(path, flags)
        descriptors.append(fd)
        return fd

    def replaced(path):
        result = original_stat(path)
        if path == source and descriptors:
            fields = list(result)
            fields[1] += 1
            # Preserve Windows-only attributes used by the reader.
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=result.st_mode, st_dev=result.st_dev,
                                   st_ino=fields[1], st_file_attributes=0)
        return result

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(Path, "lstat", replaced)
    with pytest.raises(A.AcceptanceError, match="changed"):
        R.read_declarative_inputs(tmp_path, registry)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_invalid_id_reports_safe_position_and_rule(registry):
    registry["entries"][0]["dependencies"] = ["x" * 200_000]
    with pytest.raises(A.AcceptanceError) as error:
        R.validate_registry(registry)
    assert "entry dependencies[0]" in str(error.value)
    assert "alphanumerics plus . _ -; at most 64 characters" in str(error.value)
    assert len(str(error.value)) < 200
