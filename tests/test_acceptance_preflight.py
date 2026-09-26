"""Synthetic staging and operator preflight; no tools or network required."""

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import tracemalloc
# Execution is prohibited by an explicit test double, not used to stage tools.
import subprocess  # nosec B404

import pytest

from agenttalk import acceptance as A, acceptance_preflight as P, cli
import test_acceptance_registry as records

# Reuse the synthetic M1 fixtures without repositories or additional staging work.
environment, observation, plan, registry = records.environment, records.observation, records.plan, records.registry
pin = records.pin

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def encoded(value):
    return json.dumps(value).encode()


def bind(stage):
    digest = A._hash(encoded(stage["registry"]))
    stage["plan"]["registry_digest"] = digest
    stage["observation"]["registry_hash"] = digest


def evidence(stage, ref, content):
    path = stage["root"] / ref["path"]
    path.write_bytes(content)
    ref.update(sha256=A._hash(content), size=len(content))


@pytest.fixture
def staged(tmp_path, registry, plan, observation, monkeypatch):
    monkeypatch.setattr(P, "decision_time", lambda: NOW)
    stage = {"root": tmp_path, "registry": registry, "plan": plan, "observation": observation}
    registry["files"].extend([pin("advisory-data"), pin("advisory-manifest", "snapshot")])
    registry["files"][-1]["distribution"]["id"] = "advisory-data"
    registry["entries"][0].update(inputs=["advisory-data"], snapshots=["advisory-manifest"])
    for item in registry["files"]:
        evidence(stage, item, b"synthetic staged bytes")
    registry["files"][-1]["distribution"]["sha256"] = registry["files"][-2]["sha256"]
    evidence(stage, observation["entries"][0]["banner"], b'Runtime information\r\njava 21.0.1\r\nVM build 1\n')
    evidence(stage, observation["entries"][0]["offline"]["log"], b"DENIAL_OK\n")
    bind(stage)
    return stage


def run(stage, **kwargs):
    return P.evaluate(encoded(stage["plan"]), encoded(stage["registry"]), stage["root"],
                      observation_bytes=encoded(stage["observation"]), decision_at=NOW, **kwargs)


def codes(result):
    return {hold["code"] for hold in result["holds"]}


@pytest.mark.parametrize("fault", ["changed", "missing"])
def test_preflight_changed_byte_or_missing_dependency_fails(staged, fault):
    path = staged["root"] / "advisory-data.dat"
    if fault == "changed":
        path.write_bytes(b"synthetic staged BYTES")
    else:
        path.unlink()
    result = run(staged)
    expected = "acceptance_preflight_mismatch" if fault == "changed" else "acceptance_preflight_unavailable"
    assert expected in codes(result)
    assert result["status"] == ("fail" if fault == "changed" else "not-run")


def test_preflight_expired_snapshot_holds(staged):
    staged["registry"]["files"][-1]["expires_at"] = "2026-06-01T00:00:00Z"
    bind(staged)
    assert "acceptance_snapshot_expired" in codes(run(staged))


def test_preflight_offline_positive_control_required(staged):
    proof = staged["observation"]["entries"][0]["offline"]
    proof["positive_control"] = False
    assert "acceptance_offline_unproven" in codes(run(staged))


def test_preflight_loopback_allowed_egress_denied(staged):
    proof = staged["observation"]["entries"][0]["offline"]
    proof["endpoints"] = [{"host": "127.0.0.1", "port": 1234, "pid": 42, "owned": True}]
    assert run(staged)["status"] == "pass"
    proof["endpoints"][0]["host"] = "192.0.2.1"
    assert "acceptance_offline_violation" in codes(run(staged))


def test_preflight_toolchain_drift_holds(staged):
    assert run(staged)["status"] == "pass"
    evidence(staged, staged["observation"]["entries"][0]["banner"], b"VM information\n java 21.0.1\n")
    assert "acceptance_preflight_mismatch" in codes(run(staged))


@pytest.mark.parametrize("line,matched", [(b"java 21.0.1", True), (b"java 21.0.1 ", False),
                                         (b"java 21.0.10", False), (b"prefix java 21.0.1", False)])
def test_banner_matching_is_whole_line_any_position(staged, line, matched):
    evidence(staged, staged["observation"]["entries"][0]["banner"], b"Runtime\r\n" + line + b"\r\nVM\n")
    assert (run(staged)["status"] == "pass") is matched


@pytest.mark.parametrize("change,expected", [
    ({"positive_control": False}, P.UNPROVEN), ({"egress_denied": False}, P.VIOLATION),
    ({"attempted_fetch": True}, P.VIOLATION), ({"mode": "offline-recipe"}, P.UNPROVEN),
])
def test_proof_flags_are_enforced(staged, change, expected):
    staged["observation"]["entries"][0]["offline"].update(change)
    assert expected in codes(run(staged))


@pytest.mark.parametrize("log,expected", [(b"DENIAL_OK\rFETCH\r", P.VIOLATION),
                                        (b"prefix DENIAL_OK\n", P.UNPROVEN),
                                        (b"DENIAL_OK \n", P.UNPROVEN),
                                        ("prefix\u2028DENIAL_OK\n".encode(), P.UNPROVEN),
                                        (b"\xff", P.INTEGRITY)])
def test_offline_log_requires_literal_lines(staged, log, expected):
    evidence(staged, staged["observation"]["entries"][0]["offline"]["log"], log)
    assert expected in codes(run(staged))


def test_offline_recipe_requires_marker_and_flag(staged):
    staged["registry"]["entries"][0]["offline"].update(mode="offline-recipe", cache_hit="CACHE_HIT")
    proof = staged["observation"]["entries"][0]["offline"]
    proof.update(mode="offline-recipe", cache_hit=True)
    bind(staged)
    assert P.UNPROVEN in codes(run(staged))
    evidence(staged, proof["log"], b"DENIAL_OK\nCACHE_HIT\n")
    assert run(staged)["status"] == "pass"
    proof["cache_hit"] = False
    assert P.UNPROVEN in codes(run(staged))


@pytest.mark.parametrize("host,owned,passes", [("::1", True, True), ("127.1.2.3", True, True),
    ("localhost", True, False), ("::", True, False), ("127.0.0.1", False, False), ("::1%1", True, False)])
def test_endpoint_literals_and_ownership(staged, host, owned, passes):
    staged["observation"]["entries"][0]["offline"]["endpoints"] = [
        {"host": host, "port": 5000, "pid": 2, "owned": owned}]
    assert (run(staged)["status"] == "pass") is passes


def test_snapshot_requires_a_real_consumer(staged):
    staged["registry"]["entries"][0]["inputs"] = []
    bind(staged)
    with pytest.raises(A.AcceptanceError, match="consuming its distribution"):
        run(staged)


def test_declared_freshness_propagates_to_other_consumers(staged):
    registry = staged["registry"]
    other = dict(deepcopy(registry["entries"][0]), id="node", snapshots=[])
    registry["entries"].append(other)
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["runtime"] = ["java", "node"]
    staged["plan"]["rows"][0]["registry_entries"] = ["java", "node"]
    staged["observation"]["entries"].append(dict(deepcopy(staged["observation"]["entries"][0]), id="node"))
    registry["files"][-1]["expires_at"] = "2026-06-01T00:00:00Z"
    bind(staged)
    assert all(P.EXPIRED in codes(entry) for entry in run(staged)["entries"])


def test_dependency_proof_failure_propagates(staged):
    registry = staged["registry"]
    other = dict(deepcopy(registry["entries"][0]), id="node")
    registry["entries"].append(other)
    registry["entries"][0]["dependencies"] = ["node"]
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["runtime"] = ["java", "node"]
    staged["observation"]["entries"].append(dict(deepcopy(staged["observation"]["entries"][0]), id="node"))
    staged["observation"]["entries"][1]["offline"]["positive_control"] = False
    bind(staged)
    result = run(staged)
    assert all(P.UNPROVEN in codes(entry) for entry in result["entries"])
    assert P.UNPROVEN in codes(result["rows"][0])


@pytest.mark.parametrize("fault", ["future-observation", "future-retrieval", "environment", "version", "registry"])
def test_binding_environment_and_chronology(staged, fault):
    observation = staged["observation"]
    if fault == "future-observation":
        observation["observed_at"] = "2027-01-01T00:00:00Z"
    elif fault == "future-retrieval":
        for obj in (staged["registry"]["files"][0], staged["registry"]["entries"][0]):
            obj["provenance"]["retrieved_at"] = "2026-02-01T00:00:00Z"
        bind(staged)
    elif fault == "environment":
        observation["environment"] = dict(observation["environment"], locale="different")
    elif fault == "version":
        observation["entries"][0]["version"] = "changed"
    else:
        observation["registry_hash"] = "b" * 64
    assert ("acceptance_plan_stale" if fault == "registry" else P.MISMATCH) in codes(run(staged))


@pytest.mark.parametrize("fault", ["missing", "corrupt", "malformed", "absent"])
def test_required_proof_integrity_holds_informational_rows(staged, fault):
    staged["plan"]["rows"][0]["policy"] = "informational"
    if fault == "absent":
        result = P.evaluate(encoded(staged["plan"]), encoded(staged["registry"]), staged["root"], decision_at=NOW)
    elif fault == "malformed":
        staged["observation"]["extra"] = True
        result = run(staged)
    else:
        path = staged["root"] / "offline.log"
        if fault == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
        result = run(staged)
    assert result["status"] != "pass" and result["holds"]
    expected = {"missing": P.UNAVAILABLE, "corrupt": P.INTEGRITY,
                "malformed": P.INTEGRITY, "absent": P.UNPROVEN}[fault]
    assert expected in codes(result)


@pytest.mark.parametrize("limit", ["MAX_DISTRIBUTION_BYTES", "MAX_SCAN_BYTES"])
def test_streaming_distribution_budgets(staged, monkeypatch, limit):
    monkeypatch.setattr(P, limit, 1)
    result = run(staged)
    assert P.UNAVAILABLE in codes(result)
    assert all("data" not in pin for pin in result["files"])


def test_staged_open_race_is_retryable_and_import_remains_strict(staged, monkeypatch):
    original = os.fstat

    def raced(fd):
        fields = list(original(fd))
        fields[1] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", raced)
    assert P.UNAVAILABLE in codes(run(staged))
    with pytest.raises(A.AcceptanceError) as error:
        P.read_input(staged["root"] / "source.dat")
    assert error.value.code == "acceptance_policy_invalid"


@pytest.mark.parametrize("json_output", [True, False])
def test_operator_preflight_is_read_only_and_private(staged, monkeypatch, capsys, json_output):
    root = staged["root"]
    (root / "plan.json").write_bytes(encoded(staged["plan"]))
    (root / "registry.json").write_bytes(encoded(staged["registry"]))
    (root / "observation.json").write_bytes(encoded(staged["observation"]))

    def forbidden(*args, **kwargs):
        pytest.fail("preflight attempted store access, tool launch or network")

    monkeypatch.setattr(cli, "_get_store", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    args = ["close", "acceptance", "preflight", "--plan", str(root / "plan.json"), "--cache-root", str(root),
            "--observation", str(root / "observation.json")]
    if json_output:
        args.append("--json")
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert str(root) not in output.out + output.err and root.as_posix() not in output.out + output.err
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert not (root / ".agenttalk").exists()
    (root / "jdk.dat").unlink()
    assert cli.main(args) == 3
    assert P.UNAVAILABLE in capsys.readouterr().out


def test_operator_linked_root_requests_fully_resolved_path(staged, monkeypatch, capsys):
    root = staged["root"]
    (root / "plan.json").write_bytes(encoded(staged["plan"]))
    (root / "registry.json").write_bytes(encoded(staged["registry"]))
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root or original(path))
    assert cli.main(["close", "acceptance", "preflight", "--plan", str(root / "plan.json"),
                     "--cache-root", str(root), "--json"]) == 3
    output = capsys.readouterr().out
    assert "fully resolved" in output
    assert str(root) not in output and root.as_posix() not in output


def test_operator_absent_proof_is_not_run(staged, capsys):
    root = staged["root"]
    (root / "plan.json").write_bytes(encoded(staged["plan"]))
    (root / "registry.json").write_bytes(encoded(staged["registry"]))
    assert cli.main(["close", "acceptance", "preflight", "--plan", str(root / "plan.json"),
                     "--cache-root", str(root), "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "not-run" and P.UNPROVEN in codes(result)
    assert result["entries"][0]["status"] == "not-run"


def test_operator_malformed_policy_has_no_private_locator(staged, capsys):
    root = staged["root"]
    path = root / "policy.json"
    path.write_bytes(b'{"duplicate":1,"duplicate":2}')
    assert cli.main(["close", "acceptance", "preflight", "--plan", str(path),
                     "--cache-root", str(root), "--json"]) == 3
    output = capsys.readouterr()
    assert "acceptance_policy_invalid" in output.out
    assert str(root) not in output.out + output.err


def test_distribution_without_declared_freshness_is_allowed(staged):
    staged["registry"]["files"].pop()  # Explicitly no freshness declaration.
    staged["registry"]["entries"][0]["snapshots"] = []
    bind(staged)
    assert run(staged)["status"] == "pass"


@pytest.mark.parametrize("fault", ["nested", "corrupt", "drift", "valid"])
def test_environment_override_bytes_are_verified(staged, fault):
    environment = deepcopy(staged["plan"]["environment"])
    ref = {"path": "override.json", "sha256": "a" * 64, "size": 0}
    if fault == "nested":
        environment["row_overrides"] = [{"id": "build", "environment": deepcopy(ref)}]
    evidence(staged, ref, encoded(environment))
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["row_overrides"] = [{"id": "build", "environment": deepcopy(ref)}]
    if fault == "corrupt":
        (staged["root"] / "override.json").write_bytes(b"tampered")
    elif fault == "drift":
        staged["observation"]["environment"] = deepcopy(staged["plan"]["environment"])
        staged["observation"]["environment"]["row_overrides"][0]["environment"]["sha256"] = "b" * 64
    result = run(staged)
    assert (result["status"] == "pass") is (fault == "valid")
    if fault == "nested":
        assert P.INTEGRITY in codes(result)


@pytest.mark.parametrize("clock", [datetime(2026, 6, 1), NOW.replace(microsecond=1)])
def test_decision_clock_is_strict(staged, clock):
    with pytest.raises(A.AcceptanceError, match="decision clock"):
        P.evaluate(encoded(staged["plan"]), encoded(staged["registry"]), staged["root"], decision_at=clock)


def test_each_evaluation_reads_fresh_bytes_and_one_clock(staged, monkeypatch):
    clocks = []

    def tick():
        clocks.append(1)
        return NOW

    monkeypatch.setattr(P, "decision_time", tick)
    args = (encoded(staged["plan"]), encoded(staged["registry"]), staged["root"])
    assert P.evaluate(*args, observation_bytes=encoded(staged["observation"]))["status"] == "pass"
    (staged["root"] / "jdk.dat").write_bytes(b"changed")
    assert P.MISMATCH in codes(P.evaluate(*args, observation_bytes=encoded(staged["observation"])))
    assert len(clocks) == 2


def test_private_capture_content_never_appears_in_summary(staged):
    secret = str(staged["root"])
    evidence(staged, staged["observation"]["entries"][0]["banner"], secret.encode())
    summary = json.dumps(run(staged))
    assert secret not in summary and staged["root"].as_posix() not in summary
    assert "expected banner is absent" in summary


@pytest.mark.parametrize("field", ["partition", "comparator"])
def test_policy_wrong_field_type_is_a_structured_refusal(staged, field):
    staged["plan"]["rows"][0][field] = []
    with pytest.raises(A.AcceptanceError):
        run(staged)


@pytest.mark.parametrize("host,passes", [
    ("::ffff:127.0.0.1", True), ("::ffff:7f00:1", True), ("127.0.0.2", True),
    ("0:0:0:0:0:0:0:1", True), ("localhost", False),
    # Negative address data only; the test opens no socket.
    ("0.0.0.0", False),  # noqa: S104  # nosec B104
    ("::", False), ("::1%1", False), ("127.1", False), ("0x7f000001", False),
    ("0177.0.0.1", False), ("127.00.0.1", False), ("2130706433", False),
    ("[::1]", False), ("::ffff:192.0.2.1", False),
    ("127.0.0.1", True), ("127.255.255.254", True), ("::1", True),
    ("127.0.0.1%lo", False), ("10.0.0.1", False), ("169.254.1.1", False),
    ("::ffff:8.8.8.8", False), ("0x7f.0.0.1", False), ("127.000.000.001", False),
    ("::ffff:0:127.0.0.1", False),
])
def test_loopback_verdict_is_interpreter_independent(staged, host, passes):
    staged["observation"]["entries"][0]["offline"]["endpoints"] = [
        {"host": host, "port": 1234, "pid": 42, "owned": True}]
    assert run(staged)["status"] == ("pass" if passes else "fail")


@pytest.mark.parametrize("mode", ["external-denial", "offline-recipe"])
@pytest.mark.parametrize("fetch", ["none", "flag", "line"])
def test_offline_modes_have_distinct_egress_requirements(staged, mode, fetch):
    staged["registry"]["entries"][0]["offline"].update(
        mode=mode, cache_hit="CACHE_HIT" if mode == "offline-recipe" else None)
    proof = staged["observation"]["entries"][0]["offline"]
    proof.update(mode=mode, egress_denied=False, cache_hit=True, attempted_fetch=fetch == "flag")
    evidence(staged, proof["log"], b"DENIAL_OK\nCACHE_HIT\n" + (b"FETCH\n" if fetch == "line" else b""))
    bind(staged)
    assert run(staged)["status"] == ("pass" if mode == "offline-recipe" and fetch == "none" else "fail")


@pytest.mark.parametrize("code,status", [(P.VIOLATION, "fail"), (P.INTEGRITY, "fail"),
    ("acceptance_plan_stale", "fail"), ("acceptance_policy_invalid", "refusal")])
def test_operator_import_status_matches_hold(staged, monkeypatch, capsys, code, status):
    def refuse(path):
        raise A.AcceptanceError(code, "fixed diagnostic")
    monkeypatch.setattr(P, "read_input", refuse)
    assert cli.main(["close", "acceptance", "preflight", "--plan", "plan.json",
                     "--cache-root", str(staged["root"]), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize("fault,code", [("endpoint", P.VIOLATION), ("proof", P.INTEGRITY),
                                       ("binding", "acceptance_plan_stale")])
def test_evaluation_failure_status_membership(staged, fault, code):
    actual = staged["observation"]["entries"][0]
    if fault == "endpoint":
        actual["offline"]["attempted_fetch"] = True
    elif fault == "proof":
        (staged["root"] / actual["banner"]["path"]).write_bytes(b"corrupted")
    else:
        staged["observation"]["registry_hash"] = "b" * 64
    result = run(staged)
    assert code in codes(result) and result["status"] == "fail"


@pytest.mark.parametrize("detail,advice", [("staged declarative input changed while opening", False),
    ("links/reparse points are not acceptance artifacts", True)])
def test_evaluation_path_refusal_is_retryable(staged, monkeypatch, detail, advice):
    @contextmanager
    def refuse(*args):
        raise A.AcceptanceError("acceptance_policy_invalid", detail)
        yield  # pragma: no cover
    monkeypatch.setattr(P.R, "staged_stream", refuse)
    result = run(staged)
    assert result["status"] == "not-run" and P.UNAVAILABLE in codes(result)
    assert ("fully resolved" in json.dumps(result)) is advice


def test_distribution_read_stops_at_declared_size_plus_one(staged, monkeypatch):
    reads = []
    class Stream:
        def read(self, size):
            reads.append(size)
            assert sum(reads) <= 3
            return b"x" * size
    @contextmanager
    def opened(*args):
        yield Stream()
    monkeypatch.setattr(P.R, "staged_stream", opened)
    _, holds = P._read_pin(staged["root"], {"id": "tiny", "path": "tiny", "size": 2,
                                         "sha256": "a" * 64}, [P.MAX_SCAN_BYTES], distribution=True)
    assert sum(reads) == 3 and P.MISMATCH in codes({"holds": holds})


def test_distribution_chunks_are_not_accumulated(staged, monkeypatch):
    total = 8 * 1024**2
    class Stream:
        remaining = total
        def read(self, size):
            size = min(size, self.remaining, 65536)
            self.remaining -= size
            return b"x" * size
    @contextmanager
    def opened(*args):
        yield Stream()
    monkeypatch.setattr(P.R, "staged_stream", opened)
    tracemalloc.start()
    try:
        data, _ = P._read_pin(staged["root"], {"id": "large", "path": "large", "size": total,
                                              "sha256": "a" * 64}, [total], distribution=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert data is None and peak < 1024**2


def separate_proof_root(staged):
    root = staged["root"] / "evidence"
    root.mkdir()
    actual = staged["observation"]["entries"][0]
    for ref in (actual["banner"], actual["offline"]["log"]):
        (staged["root"] / ref["path"]).rename(root / ref["path"])
    return root


@pytest.mark.parametrize("missing", ["planned", "observed", "neither"])
def test_environment_overrides_use_separate_roots(staged, missing):
    proof_root = separate_proof_root(staged)
    ref = {"path": "override.json", "size": 0, "sha256": "a" * 64}
    evidence(staged, ref, encoded(staged["plan"]["environment"]))
    (proof_root / ref["path"]).write_bytes((staged["root"] / ref["path"]).read_bytes())
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["row_overrides"] = [{"id": "build", "environment": deepcopy(ref)}]
    if missing != "neither":
        ((staged["root"] if missing == "planned" else proof_root) / ref["path"]).unlink()
    result = run(staged, proof_root=proof_root)
    assert result["status"] == ("pass" if missing == "neither" else "not-run")


def test_operator_separate_evidence_directory(staged, capsys):
    root = staged["root"]
    proof_root = separate_proof_root(staged)
    for filename, record in (("plan.json", "plan"), ("registry.json", "registry")):
        (root / filename).write_bytes(encoded(staged[record]))
    (proof_root / "observation.json").write_bytes(encoded(staged["observation"]))
    assert cli.main(["close", "acceptance", "preflight", "--plan", str(root / "plan.json"),
                     "--cache-root", str(root), "--observation", str(proof_root / "observation.json"),
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_checker_null_expected_banner_is_valid(staged):
    entry = staged["registry"]["entries"][0]
    entry.update(kind="checker", expected_banner=None,
                 measurement={"comparator": "adapter", "parser": "adapter",
                              "config": "config", "normalizer": "adapter"})
    for item in (pin("adapter", "adapter"), pin("config", "config")):
        evidence(staged, item, b"{}")
        staged["registry"]["files"].append(item)
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["runtime"] = []
    bind(staged)
    assert run(staged)["status"] == "pass"


def test_snapshot_must_be_listed_by_a_consumer(staged):
    staged["registry"]["entries"][0].update(snapshots=[], inputs=["advisory-data", "advisory-manifest"])
    bind(staged)
    with pytest.raises(A.AcceptanceError, match="listed"):
        run(staged)


def test_snapshot_consumer_may_use_transitive_dependency(staged):
    entry = staged["registry"]["entries"][0]
    dep = dict(deepcopy(entry), id="node", snapshots=[])
    entry.update(inputs=[], dependencies=["node"])
    staged["registry"]["entries"].append(dep)
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["runtime"] = ["java", "node"]
    staged["observation"]["entries"].append(dict(deepcopy(staged["observation"]["entries"][0]), id="node"))
    bind(staged)
    assert run(staged)["status"] == "pass"


@pytest.mark.parametrize("role", ["distribution", "snapshot"])
def test_pin_provenance_is_in_entry_input_closure(staged, role):
    item = pin("dedicated-source", "provenance")
    evidence(staged, item, b"provenance")
    staged["registry"]["files"].append(item)
    target = next(p for p in staged["registry"]["files"] if p["id"] ==
                  ("advisory-data" if role == "distribution" else "advisory-manifest"))
    target["provenance"]["record"] = item["id"]
    bind(staged)
    (staged["root"] / item["path"]).unlink()
    result = run(staged)
    assert P.UNAVAILABLE in codes(result["entries"][0])


def test_inherited_manifest_provenance_reaches_other_consumer(staged):
    registry = staged["registry"]
    registry["entries"].append(dict(deepcopy(registry["entries"][0]), id="node", snapshots=[]))
    source = pin("manifest-source", "provenance")
    evidence(staged, source, b"provenance")
    registry["files"][-1]["provenance"]["record"] = source["id"]
    registry["files"].append(source)
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["runtime"] = ["java", "node"]
    staged["plan"]["rows"][0]["registry_entries"] = ["java", "node"]
    staged["observation"]["entries"].append(dict(deepcopy(staged["observation"]["entries"][0]), id="node"))
    bind(staged)
    (staged["root"] / source["path"]).unlink()
    result = run(staged)
    assert all(P.UNAVAILABLE in codes(entry) for entry in result["entries"])


@pytest.mark.parametrize("fault", ["roles", "digest"])
def test_override_role_and_integrity_failures(staged, fault):
    override = deepcopy(staged["plan"]["environment"])
    if fault == "roles":
        override["runtime"] = ["unknown"]
    ref = {"path": "override.json", "size": 0, "sha256": "a" * 64}
    evidence(staged, ref, encoded(override))
    for record in (staged["plan"], staged["observation"]):
        record["environment"]["row_overrides"] = [{"id": "build", "environment": deepcopy(ref)}]
    if fault == "digest":
        (staged["root"] / ref["path"]).write_bytes(b"wrong")
    assert P.INTEGRITY in codes(run(staged))


def test_clock_wrong_type_is_structured(staged):
    with pytest.raises(A.AcceptanceError, match="decision clock"):
        P.evaluate(encoded(staged["plan"]), encoded(staged["registry"]), staged["root"], decision_at="tomorrow")


@pytest.mark.parametrize("fault", ["oserror", "long", "non-object"])
def test_operator_import_error_surface(staged, monkeypatch, capsys, fault):
    def read(path):
        if fault == "oserror":
            raise OSError(str(staged["root"]))
        if fault == "long":
            raise A.AcceptanceError("acceptance_policy_invalid", "x" * 2000)
        return b"[]"
    monkeypatch.setattr(P, "read_input", read)
    assert cli.main(["close", "acceptance", "preflight", "--plan", "plan.json",
                     "--cache-root", str(staged["root"]), "--json"]) == 3
    output = capsys.readouterr().out
    result = json.loads(output)
    assert str(staged["root"]) not in output and len(result["holds"][0]["detail"]) <= 256
    assert (P.UNAVAILABLE if fault == "oserror" else "acceptance_policy_invalid") in codes(result)


def test_legacy_projection_keyerror_is_structured(staged, monkeypatch):
    def invalid(value):
        raise KeyError("missing")
    monkeypatch.setattr(A, "validate_plan", invalid)
    with pytest.raises(A.AcceptanceError, match="projected plan"):
        run(staged)


def test_pin_hold_ref_and_missing_file_advice(staged, capsys):
    (staged["root"] / "jdk.dat").unlink()
    result = run(staged)
    issue = next(h for h in result["holds"] if h["code"] == P.UNAVAILABLE)
    assert issue["ref"] == "jdk" and "fully resolved" not in issue["detail"]
    root = staged["root"]
    for key in ("plan", "registry", "observation"):
        (root / (key + ".json")).write_bytes(encoded(staged[key]))
    assert cli.main(["close", "acceptance", "preflight", "--plan", str(root / "plan.json"),
                     "--cache-root", str(root), "--observation", str(root / "observation.json")]) == 3
    assert "[jdk]" in capsys.readouterr().out


def test_import_read_cap_rejects_instead_of_truncating(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_bytes(b"x" * 17)
    monkeypatch.setattr(P.R, "MAX_INPUT_BYTES", 16)
    with pytest.raises(A.AcceptanceError) as error:
        P.read_input(path)
    assert error.value.code == P.INTEGRITY


@pytest.mark.parametrize("size,budget", [(2 * 1024**3 + 1, P.MAX_SCAN_BYTES), (21, 20)])
def test_distribution_limit_refuses_before_open(staged, monkeypatch, size, budget):
    def forbidden(*args):
        pytest.fail("out-of-budget distribution was opened")
    monkeypatch.setattr(P.R, "staged_stream", forbidden)
    _, issues = P._read_pin(staged["root"], {"id": "large", "path": "large", "size": size,
                                           "sha256": "a" * 64}, [budget], distribution=True)
    assert P.UNAVAILABLE in codes({"holds": issues})


@pytest.mark.parametrize("budget,length", [(100, 17), (8, 9)])
def test_declarative_read_limits_hold_before_digest(staged, monkeypatch, budget, length):
    monkeypatch.setattr(P.R, "MAX_INPUT_BYTES", 16)
    (staged["root"] / "oversized").write_bytes(b"x" * length)
    _, issues = P._read_pin(staged["root"], {"id": "large", "path": "oversized", "size": 2,
                                           "sha256": "a" * 64}, [budget])
    assert codes({"holds": issues}) == {P.INTEGRITY}


def test_matching_digest_does_not_excuse_incorrect_size(staged):
    ref = next(pin for pin in staged["registry"]["files"] if pin["id"] == "source")
    ref["size"] += 1
    bind(staged)
    assert P.MISMATCH in codes(run(staged))


def test_pin_read_oserror_is_structured(staged, monkeypatch):
    @contextmanager
    def denied(*args):
        raise PermissionError(str(staged["root"]))
        yield  # pragma: no cover
    monkeypatch.setattr(P.R, "staged_stream", denied)
    result = run(staged)
    assert result["status"] == "not-run" and codes(result) == {P.UNAVAILABLE, P.UNPROVEN}
    assert str(staged["root"]) not in json.dumps(result)


@pytest.mark.parametrize("locator", ["../outside.json", "/outside.json"])
def test_cli_refuses_escaping_registry_before_read(staged, monkeypatch, capsys, locator):
    staged["plan"]["registry_ref"] = locator
    reads = []
    def read(path):
        reads.append(path)
        assert len(reads) == 1, "escaped registry was read before policy refusal"
        return encoded(staged["plan"])
    monkeypatch.setattr(P, "read_input", read)
    assert cli.main(["close", "acceptance", "preflight", "--plan", "plan.json",
                     "--cache-root", str(staged["root"]), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "refusal"


def test_distinct_missing_pins_keep_distinct_public_refs(staged):
    for name in ("jdk", "advisory-data"):
        (staged["root"] / (name + ".dat")).unlink()
    issues = [h for h in run(staged)["holds"] if h["code"] == P.UNAVAILABLE]
    assert {h["ref"] for h in issues} == {"jdk", "advisory-data"}
