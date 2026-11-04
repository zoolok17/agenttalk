"""P6: experimental calls remain bound, non-voting, and separate from the gate."""

from __future__ import annotations

import csv
import io
import zipfile
import copy
import base64
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import dev_gate as gate
from agenttalk import dev_gate_trial as trial
from agenttalk import dev_gate_experiment as frozen_installs
from test_dev_gate import _binding, _leg_artifact, _manifest


@pytest.fixture
def exp():
    return importlib.import_module("agenttalk.dev_gate_experiment")


@pytest.fixture
def identity_impl(exp):
    return exp.installed_identity


@pytest.fixture
def request_factory(exp, identity_impl, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    manifest = _manifest()
    (checkout / "dev-gate.json").write_text(json.dumps(manifest), encoding="utf-8")
    contract = json.loads((Path(__file__).parents[1] / "dev-gate-experiment.json").read_text())
    (checkout / "dev-gate-experiment.json").write_text(json.dumps(contract), encoding="utf-8")
    (checkout / "tests").mkdir()
    # P3 defines the option in its helper and delegates registration from conftest.
    (checkout / "tests" / "experiment_record.py").write_text(
        'OPTION = "--experiment-record"\n'
        "def add_option(parser):\n"
        '    parser.addoption(OPTION, dest="experiment_record", default=None, metavar="DIR")\n',
        encoding="utf-8",
    )
    (checkout / "tests" / "conftest.py").write_text(
        "import experiment_record\ndef pytest_addoption(parser): experiment_record.add_option(parser)\n",
        encoding="utf-8",
    )
    binding = SimpleNamespace(candidate_sha="a" * 40, clean=True)
    monkeypatch.setattr(gate, "capture_candidate_binding", lambda root: binding)
    # Model Windows' one value per case-insensitive name even on POSIX hosts.
    for name, value in list(os.environ.items()):
        if name != name.upper():
            monkeypatch.delenv(name)
        monkeypatch.setenv(name.upper(), value)
    monkeypatch.setattr(gate, "_platform_label", lambda: "windows")
    monkeypatch.setattr(
        exp,
        "installed_identity",
        lambda root: {
            "commit": "a" * 40,
            "version": "1.0",
            "record_sha256": "b" * 64,
        },
    )
    frozen = {
        "contract_sha256": exp.contract_hash(contract),
        "commit": "a" * 40,
        "environments": {"3.12": {"freeze_sha256": "c" * 64}},
    }
    record = tmp_path / "frozen.json"
    record.write_text(json.dumps(frozen), encoding="utf-8")
    counter = 0

    def make(**kwargs):
        nonlocal counter
        counter += 1
        options = {
            "checkout": checkout,
            "record": record,
            "output": tmp_path / f"out{counter}",
            "leg": "windows/3.12",
            "modes": ("source", "wheel"),
            "arm": "serial",
            "probe": None,
        }
        options.update(kwargs)
        return exp.prepare_request(**options)

    make.checkout, make.record, make.contract, make.manifest = checkout, record, contract, manifest
    return make


def capture_ordinary(monkeypatch, manifest, mode, paths, env):
    """Use the actual ordinary-gate argv/environment/timeout path as the oracle."""
    captured = {}
    monkeypatch.setattr(gate, "_probe_python_module", lambda *a, **k: "pytest")
    monkeypatch.setattr(gate, "import_probe", lambda **k: {})
    monkeypatch.setattr(gate, "_platform_label", lambda: "windows")

    def run(**kwargs):
        captured.update(kwargs)
        log = paths["logs"] / "ordinary.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("pass", encoding="utf-8")
        return gate.CommandOutcome(tuple(kwargs["argv"]), 0, 1, "pass", None, "", log)

    monkeypatch.setattr(gate, "run_command", run)
    gate._run_pytest_mode(
        mode=mode,
        interpreter=gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", platform.python_version()),
        source_root=paths["source"],
        import_root=paths["imports"],
        env=env,
        expected_version="1.0",
        manifest=manifest,
        basetemp=paths["bt"],
        logs_dir=paths["logs"],
    )
    return captured


@pytest.mark.parametrize("mode", ["source", "wheel"])
def test_01_serial_equivalence_and_parallel_has_only_d4(exp, request_factory, tmp_path, monkeypatch, mode):
    req = request_factory(modes=(mode,))
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "logs", "records")}
    env = gate._base_env(tmp_path)
    ordinary = capture_ordinary(monkeypatch, req.manifest, mode, paths, env)
    call = exp.pytest_call(req, mode, Path(sys.executable), paths, env)
    expected = ordinary["argv"][: -len(req.manifest["checks"]["pytest"]["paths"])]
    expected += [f"--experiment-record={paths['records']}"] + req.manifest["checks"]["pytest"]["paths"]
    assert call["argv"] == expected
    assert call["env"] == ordinary["env"] and call["timeout_seconds"] == ordinary["timeout_seconds"]
    assert call["checks"] == trial.partition(req.manifest, req.leg, mode)
    req.arm = "parallel"
    parallel = exp.pytest_call(req, mode, Path(sys.executable), paths, env)
    insert = len(expected) - len(req.manifest["checks"]["pytest"]["paths"])
    expected[insert:insert] = req.contract["arms"]["parallel"]
    assert parallel["argv"] == expected


@pytest.mark.parametrize("mode", ["source", "wheel"])
@pytest.mark.parametrize("arm", ["serial", "parallel"])
@pytest.mark.parametrize("builder", ["pytest_call", "expected_call"])
def test_fix_n3_record_option_is_one_argument(exp, request_factory, tmp_path, mode, arm, builder):
    request = request_factory(modes=(mode,), arm=arm)
    work = tmp_path / "work with spaces"
    records = work / "records"
    records.mkdir(parents=True)
    paths = {name: work / name for name in ("source", "imports", "bt", "logs", "records")}
    if builder == "pytest_call":
        call = exp.pytest_call(request, mode, Path(sys.executable), paths, gate._base_env(work))
    else:
        prepared = SimpleNamespace(
            python=Path(sys.executable), source_root=paths["source"], import_root=paths["imports"]
        )
        call = exp.expected_call(request, mode, prepared, work)
    argv = call["argv"]
    # A separate existing folder can influence pytest's rootdir before option parsing.
    assert "--experiment-record" not in argv
    assert [arg for arg in argv if arg.startswith("--experiment-record=")] == [f"--experiment-record={records}"]
    assert str(records) not in argv


def test_02_ordinary_windows_parallel_evidence_still_rejected():
    manifest = _manifest()
    artifact = _leg_artifact(manifest, "windows/3.12")
    check = next(c for c in artifact["checks"] if c["kind"] == "pytest")
    check["argv"][-1:-1] = manifest["checks"]["pytest"]["posix_parallel_args"]
    with pytest.raises(gate.GateBlock):
        gate.validate_run_artifact(artifact, manifest)


def test_03_posix_switch_refused_even_when_empty(exp, request_factory, monkeypatch):
    monkeypatch.setenv("AGENTTALK_DEV_GATE_POSIX_PARALLEL", "")
    with pytest.raises(gate.GateBlock, match="POSIX"):
        request_factory()


def test_04_experiment_artifact_never_votes(exp, request_factory, tmp_path, monkeypatch):
    artifact = exp.artifact_header(request_factory())
    assert artifact["artifact_type"] == "agenttalk-windows-workers-preparation-v1"
    assert artifact["authoritative"] is False
    manifest = gate.validate_manifest(_manifest())
    ordinary = _leg_artifact(manifest, "windows/3.12")
    gate.validate_run_artifact(ordinary, manifest)
    ordinary["artifact_type"] = exp.TYPE
    with pytest.raises(gate.GateBlock, match="artifact schema or type"):
        gate.validate_run_artifact(ordinary, manifest)
    legs = [_leg_artifact(manifest, leg) for leg in gate.expected_ci_legs(manifest, "release")]
    monkeypatch.setattr(gate, "_committed_version", lambda root: "0.78.1")
    aggregate = gate.aggregate_leg_artifacts(manifest, "release", legs, _binding(manifest, root=tmp_path))
    gate.validate_aggregate_artifact(aggregate, manifest)
    aggregate["artifact_type"] = exp.TYPE
    # The ordinary aggregate validator calls its type refusal an invalid header.
    with pytest.raises(gate.GateBlock, match="aggregate header is invalid"):
        gate.validate_aggregate_artifact(aggregate, manifest)
    (tmp_path / "trial.json").write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(gate.GateBlock):
        trial.validate_part(tmp_path, {}, _manifest(), "windows/3.12", "source")


@pytest.mark.parametrize("change", ["hash", "probe", "arm", "leg", "missing-gate", "missing-contract"])
def test_05_preflight_refuses_unbound_inputs(exp, request_factory, change):
    options = {}
    if change == "hash":
        data = json.loads(request_factory.record.read_text())
        data["contract_sha256"] = "0" * 64
        request_factory.record.write_text(json.dumps(data), encoding="utf-8")
    elif change in {"probe", "arm", "leg"}:
        options[change] = "linux/3.12" if change == "leg" else "unexpected"
        if change == "probe":
            options["arm"] = None
    else:
        name = "dev-gate.json" if change == "missing-gate" else "dev-gate-experiment.json"
        (request_factory.checkout / name).unlink()
    with pytest.raises(gate.GateBlock):
        request_factory(**options)


def test_05_cli_requires_checkout_and_rejects_extra_flags(exp):
    with pytest.raises(SystemExit):
        exp.parse_args(
            [
                "accept",
                "--arm",
                "serial",
                "--leg",
                "windows/3.12",
                "--modes",
                "source",
                "--record",
                "freeze.json",
                "--output",
                "out",
            ]
        )
    with pytest.raises(SystemExit):
        exp.parse_args(
            [
                "run",
                "--checkout",
                ".",
                "--probe",
                "p10",
                "--arm",
                "serial",
                "--leg",
                "windows/3.12",
                "--modes",
                "source",
                "--record",
                "freeze.json",
                "--output",
                "out",
            ]
        )


def test_06_windows_timeout_is_9000(exp, request_factory, tmp_path):
    req = request_factory()
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "logs", "records")}
    assert exp.pytest_call(req, "source", Path(sys.executable), paths, {})["timeout_seconds"] == 9000


def test_07_installed_commit_mismatch_refused(exp, request_factory, monkeypatch):
    monkeypatch.setattr(
        exp,
        "installed_identity",
        lambda root: {
            "commit": "d" * 40,
            "version": "1.0",
            "record_sha256": "b" * 64,
        },
    )
    with pytest.raises(gate.GateBlock, match="installed"):
        request_factory()


@pytest.mark.parametrize("proof", [None, {"complete": True}, {"complete": True, "plugins": ["experiment.audit_probe"]}])
def test_08_acceptance_requires_complete_clean_plugin_inventory(exp, proof):
    with pytest.raises(gate.GateBlock):
        exp.validate_inventory(proof, acceptance=True)


def test_08_probe_and_arm_cannot_mix(exp, request_factory):
    with pytest.raises(gate.GateBlock):
        request_factory(arm="serial", probe="p10")


def test_08_acceptance_rejects_probe_files(exp):
    with pytest.raises(gate.GateBlock):
        exp.validate_install(
            {"freeze_sha256": "c" * 64, "probe_files": ["observer.pth"], "gateway_policy": "error"}, "c" * 64
        )


def test_wheel_install_proof_requires_separate_wheel_identity(exp):
    proof = {"freeze_sha256": "c" * 64, "probe_files": [], "gateway_policy": "error"}
    with pytest.raises(gate.GateBlock, match="wheel fingerprint"):
        exp.validate_install(proof, "c" * 64, mode="wheel")


@pytest.mark.parametrize("field", ["argv", "env", "timeout_seconds"])
def test_09_undeclared_call_difference_rejected(exp, request_factory, tmp_path, field):
    req = request_factory()
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "logs", "records")}
    expected = exp.pytest_call(req, "source", Path(sys.executable), paths, {"Path": "safe"})
    actual = copy.deepcopy(expected)
    if field == "argv":
        actual[field].append("--unexpected")
    elif field == "env":
        actual[field]["EXTRA"] = "1"
    else:
        actual[field] -= 1
    with pytest.raises(gate.GateBlock):
        exp.validate_call(actual, expected)


def test_environment_case_and_only_own_root_are_normalised(exp, tmp_path):
    left = {"argv": [str(tmp_path / "a" / "file")], "env": {"Path": "safe"}, "timeout_seconds": 9000}
    right = {"argv": [str(tmp_path / "b" / "file")], "env": {"PATH": "safe"}, "timeout_seconds": 9000}
    exp.validate_call(right, left, actual_root=tmp_path / "b", expected_root=tmp_path / "a")


def test_contract_hash_is_canonical(exp, request_factory):
    data = request_factory.contract
    reversed_keys = dict(reversed(list(data.items())))
    assert exp.contract_hash(data) == exp.contract_hash(json.loads(json.dumps(reversed_keys, indent=4) + "\r\n"))


def test_production_dependencies_fail_closed(exp, request_factory):
    with pytest.raises(gate.GateBlock, match="P7|P3"):
        exp.production_dependencies(request_factory())


def test_credentialed_proxy_refused_without_echo(exp, request_factory, tmp_path):
    req = request_factory()
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "logs", "records")}
    supplied = "http://fixture:fixture@127.0.0.1:8080"
    with pytest.raises(gate.GateBlock) as caught:
        exp.pytest_call(req, "source", Path(sys.executable), paths, {"HTTPS_PROXY": supplied})
    assert supplied not in str(caught.value)


def observe_fixture_suite(suite, work, *, exp, request, experimental, mode):
    """Bounded pytest control: record collection marks separately from runtime reports."""
    work.mkdir()
    copy_root = work / "suite"
    shutil.copytree(suite, copy_root)
    (copy_root / "conftest.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "observations = {'collection': [], 'skips': []}\n"
        "def pytest_addoption(parser): parser.addoption('--experiment-record')\n"
        "def pytest_collection_finish(session):\n"
        "    observations['collection'] = [\n"
        "        (i.nodeid, sorted(m.name for m in i.iter_markers())) for i in session.items]\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.skipped:\n"
        "        reason = str(report.longrepr[-1]).removeprefix('Skipped: ')\n"
        "        observations['skips'].append({'id': report.nodeid, 'phase': report.when, 'reason': reason})\n"
        "def pytest_sessionfinish(session):\n"
        "    observations['lifecycle'] = Path('lifecycle').read_text().splitlines()\n"
        "    Path('observations.json').write_text(json.dumps(observations))\n",
        encoding="utf-8",
    )
    (copy_root / "tests").mkdir()
    (copy_root / "test_fixture.py").rename(copy_root / "tests" / "test_fixture.py")
    paths = {"source": copy_root, "imports": copy_root, "bt": work / "bt", "records": work / "journals"}
    env = gate._base_env(work)
    if experimental:
        argv = exp.pytest_call(request, mode, Path(sys.executable), paths, env)["argv"]
    else:
        spec = request.manifest["checks"]["pytest"]
        argv = gate.isolated_tool_argv(
            sys.executable,
            "pytest",
            *spec["args"],
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(paths["bt"]),
            *spec["paths"],
            candidate_import_root=copy_root if mode == "source" else None,
        )
    outcome = gate.run_command(
        check_id="bounded-control", argv=argv, cwd=copy_root, env=env, timeout_seconds=45, logs_dir=work / "logs"
    )
    assert outcome.status == "pass", outcome.diagnostic
    return json.loads((copy_root / "observations.json").read_text())


@pytest.mark.parametrize("mode", ["source", "wheel"])
def test_01_real_collection_and_runtime_skips_setup_cleanup_match(exp, request_factory, tmp_path, mode):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_fixture.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.fixture\ndef resource():\n"
        "    Path('lifecycle').open('a').write('setup\\n')\n"
        "    yield\n    Path('lifecycle').open('a').write('cleanup\\n')\n"
        "def test_pass(resource): pass\n"
        "@pytest.mark.skip(reason='collection-mark')\ndef test_mark(): pass\n"
        "def test_body_skip(): pytest.skip('body-skip')\n"
        "@pytest.fixture\ndef unavailable(): pytest.skip('setup-skip')\n"
        "def test_setup_skip(unavailable): pass\n",
        encoding="utf-8",
    )
    observations = [
        observe_fixture_suite(
            suite, tmp_path / arm, exp=exp, request=request_factory(), experimental=arm == "experimental", mode=mode
        )
        for arm in ("ordinary", "experimental")
    ]
    assert observations[0] == observations[1]
    assert len(observations[0]["collection"]) == 4
    assert {row["reason"] for row in observations[0]["skips"]} == {"collection-mark", "body-skip", "setup-skip"}
    assert observations[0]["lifecycle"] == ["setup", "cleanup"]


@pytest.mark.parametrize("mode", ["source", "wheel"])
def test_installer_interface_execution_records_setup_freeze_and_cleanup(exp, request_factory, mode, monkeypatch):
    request = request_factory(modes=(mode,))
    events = []

    def export(binding, destination, archive):
        events.append(destination.name)
        destination.mkdir()

    monkeypatch.setattr(gate, "export_candidate", export)

    class FakeInstaller:
        def prepare(self, request, mode, work):
            events.append("prepare")
            source = work / f"candidate-{mode}"
            checks = [
                c
                for c in exp.partition(request.manifest, request.leg, mode)
                if c not in {"git-binding", "final-binding"} and not c.startswith("pytest-")
            ]
            return exp.PreparedEnvironment(
                Path(sys.executable),
                source,
                source / "src" if mode == "source" else source,
                {"freeze_sha256": "c" * 64, "probe_files": [], "gateway_policy": "error", "wheel_sha256": "d" * 64},
                ["host"] if mode == "source" else ["host", "build", "runtime", "wheel-test"],
                [{"id": c, "status": "pass"} for c in checks],
            )

        def finish(self, prepared):
            events.append("finish")

    class FixtureRecorder:
        def inventory(self, directory):
            events.append("inventory")
            return {"complete": True, "plugins": ["pytest", "experiment_record"]}

    def runner(**kwargs):
        events.append("pytest")
        assert kwargs["check_id"] == f"pytest-{mode}-py312"
        r4_runner(**kwargs)
        return SimpleNamespace(status="pass")

    artifact = exp.execute(request, installer=FakeInstaller(), recorder=FixtureRecorder(), runner=runner)
    exports = ["candidate-source"] if mode == "source" else ["candidate-source", "candidate-package", "candidate-wheel"]
    assert events == [*exports, "prepare", "pytest", "inventory", "candidate-static", "finish"]
    assert artifact["status"] == "complete" and artifact["authoritative"] is False
    roles = ["host"] if mode == "source" else ["host", "build", "runtime", "wheel-test"]
    assert artifact["calls"][0]["setup_steps"] == roles
    assert artifact["calls"][0]["cleanup"]["basetemp_removed"] is False
    assert artifact["calls"][0]["cleanup"]["basetemp_present"] is True
    if mode == "wheel":
        assert artifact["calls"][0]["installed_set"]["wheel_sha256"] == "d" * 64
        assert artifact["installed_candidate"]["record_sha256"] == "b" * 64
    assert (request.output / "experiment.json").is_file()


@pytest.mark.parametrize("kind", ["large", "deep", "duplicate", "hard-link", "escape"])
def test_json_input_is_bounded_plain_unambiguous_and_contained(exp, tmp_path, kind):
    root = tmp_path / "config"
    root.mkdir()
    path = root / "input.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    if kind == "large":
        path.write_text(json.dumps({"payload": "a" * exp.MAX_JSON_BYTES}), encoding="utf-8")
    elif kind == "deep":
        path.write_text('{"a":' * 30 + "0" + "}" * 30, encoding="utf-8")
    elif kind == "duplicate":
        path.write_text('{"ok":true,"ok":false}', encoding="utf-8")
    elif kind == "hard-link":
        os.link(path, root / "second-name.json")
    else:
        path = tmp_path / "outside.json"
        path.write_text("{}", encoding="utf-8")
    with pytest.raises(gate.GateBlock):
        exp.read_json(path, within=root)


@pytest.mark.parametrize("change", [None, "missing-dir", "wrong-version", "source-entry", "record-link"])
def test_installed_distribution_provenance_and_record(exp, local_identity, tmp_path, monkeypatch, change):
    f = local_identity
    if change == "missing-dir":
        f.direct.write_text(json.dumps({"url": f.checkout.as_uri()}), encoding="utf-8")
    elif change == "wrong-version":
        f.distribution.version = "2.0"
    elif change == "source-entry":
        monkeypatch.setattr(exp, "__file__", str(tmp_path / "outside.py"))
    elif change == "record-link":
        os.link(f.record, f.record.with_name("linked-record"))
    if change is not None:
        with pytest.raises(gate.GateBlock):
            exp.installed_identity(f.checkout)
    else:
        identity = exp.installed_identity(f.checkout)
        assert identity == {
            "commit": "a" * 40,
            "version": "1.0",
            "record_sha256": gate.sha256_bytes(f.record.read_bytes()),
        }


@pytest.fixture
def local_identity(exp, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    source = checkout / "src" / "agenttalk" / "dev_gate_experiment.py"
    source.parent.mkdir(parents=True)
    source.write_text("# candidate fixture\n", encoding="utf-8")
    installed = tmp_path / "installed"
    entry = installed / "agenttalk" / source.name
    entry.parent.mkdir(parents=True)
    entry.write_bytes(source.read_bytes())
    info = installed / "agenttalk-1.0.dist-info"
    info.mkdir()
    direct = info / "direct_url.json"
    direct.write_text(json.dumps({"url": checkout.as_uri(), "dir_info": {}}), encoding="utf-8")
    record = info / "RECORD"
    digest = base64.urlsafe_b64encode(hashlib.sha256(source.read_bytes()).digest()).decode().rstrip("=")
    record.write_text(f"agenttalk/{source.name},sha256={digest},{source.stat().st_size}\n", encoding="utf-8")
    distribution = SimpleNamespace(
        version="1.0",
        files=[Path("agenttalk-1.0.dist-info/RECORD"), Path("agenttalk-1.0.dist-info/direct_url.json")],
        locate_file=lambda path: installed / path,
    )
    monkeypatch.setattr(exp.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(exp, "__file__", str(entry))
    monkeypatch.setattr(gate, "_committed_version", lambda root: "1.0")
    monkeypatch.setattr(
        gate, "capture_candidate_binding", lambda root: SimpleNamespace(clean=True, candidate_sha="a" * 40)
    )
    return SimpleNamespace(
        checkout=checkout, source=source, entry=entry, direct=direct, record=record, distribution=distribution
    )


def test_fix_cx1_real_local_pip_install_identity(exp, tmp_path_factory, monkeypatch):
    pytest.importorskip("hatchling")
    # Keep the node directory short too: pip adds its own nested build paths.
    tmp_path = tmp_path_factory.mktemp("i")
    # CX1/CL1: reproduce the approved local-directory route, without a VCS URL.
    checkout = tmp_path / "p"
    source = checkout / "src" / "agenttalk"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "dev_gate_experiment.py").write_text("# fixture entry point\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "agenttalk"\nversion = "1.0"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/agenttalk"]\n',
        encoding="utf-8",
    )
    target = tmp_path / "i"
    temporary = tmp_path / "t"
    temporary.mkdir()
    env = gate._base_env(temporary)
    done = subprocess.run(  # noqa: S603 - fixed local pip argv, offline, temporary target
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--no-cache-dir",
            "--target",
            str(target),
            str(checkout),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    distribution = next(iter(exp.importlib.metadata.distributions(path=[str(target)])))
    provenance = json.loads(distribution.read_text("direct_url.json"))
    assert provenance == {"dir_info": {}, "url": checkout.as_uri()}
    monkeypatch.setattr(exp.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(exp, "__file__", str(target / "agenttalk" / "dev_gate_experiment.py"))
    monkeypatch.setattr(gate, "_committed_version", lambda root: "1.0")
    monkeypatch.setattr(
        gate, "capture_candidate_binding", lambda root: SimpleNamespace(clean=True, candidate_sha="a" * 40)
    )
    assert exp.installed_identity(checkout)["commit"] == "a" * 40


@pytest.mark.parametrize(
    "damage", ["url", "editable", "installed-bytes", "source-bytes", "hash", "unhashed-entry", "dirty"]
)
def test_fix_cx1_local_identity_refuses_mismatched_copy(exp, local_identity, monkeypatch, damage):
    f = local_identity
    if damage in {"url", "editable"}:
        data = json.loads(f.direct.read_text())
        if damage == "url":
            data["url"] = f.checkout.parent.as_uri()
        else:
            data["dir_info"]["editable"] = True
        f.direct.write_text(json.dumps(data), encoding="utf-8")
    elif damage == "installed-bytes":
        f.entry.write_text("# altered installed copy\n", encoding="utf-8")
    elif damage == "source-bytes":
        f.source.write_text("# altered checkout\n", encoding="utf-8")
    elif damage == "hash":
        f.record.write_text(f.record.read_text().replace("sha256=", "sha512="), encoding="utf-8")
    elif damage == "unhashed-entry":
        f.record.write_text("agenttalk/dev_gate_experiment.py,,\n", encoding="utf-8")
    else:
        monkeypatch.setattr(
            gate, "capture_candidate_binding", lambda root: SimpleNamespace(clean=False, candidate_sha="a" * 40)
        )
    with pytest.raises(gate.GateBlock):
        exp.installed_identity(f.checkout)


@pytest.mark.parametrize("metadata", [{"dir_info": None}, {"dir_info": {}, "vcs_info": {"commit_id": 123}}])
def test_fix_cx2_malformed_provenance_refuses_through_main(
    exp, identity_impl, request_factory, local_identity, monkeypatch, capsys, metadata
):
    payload = {"url": local_identity.checkout.as_uri(), "dir_info": {}, **metadata}
    local_identity.direct.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(exp, "installed_identity", identity_impl)
    result = exp.main(
        [
            "accept",
            "--checkout",
            str(request_factory.checkout),
            "--arm",
            "serial",
            "--leg",
            "windows/3.12",
            "--modes",
            "source",
            "--record",
            str(request_factory.record),
            "--output",
            str(local_identity.checkout.parent / "out"),
        ]
    )
    assert result == 2
    err = capsys.readouterr().err
    assert "experiment refused:" in err and "Traceback" not in err
    assert ("dir_info" if metadata["dir_info"] is None else "commit_id") in err


@pytest.fixture
def execution_fixture(exp, request_factory, monkeypatch):
    monkeypatch.setattr(gate, "export_candidate", lambda binding, destination, archive: destination.mkdir())

    class Installer:
        def prepare(self, request, mode, work):
            checks = [
                c
                for c in exp.partition(request.manifest, request.leg, mode)
                if c not in {"git-binding", "final-binding"} and not c.startswith("pytest-")
            ]
            source = work / f"candidate-{mode}"
            return exp.PreparedEnvironment(
                Path(sys.executable),
                source,
                source / "src" if mode == "source" else source,
                {"freeze_sha256": "c" * 64, "probe_files": [], "gateway_policy": "error", "wheel_sha256": "d" * 64},
                ["host"] if mode == "source" else ["host", "build", "runtime", "wheel-test"],
                [{"id": check, "status": "pass"} for check in checks],
            )

        def finish(self, prepared):
            pass

    return SimpleNamespace(
        installer=Installer(),
        recorder=SimpleNamespace(
            inventory=lambda directory: {"complete": True, "plugins": ["pytest", "experiment_record"]}
        ),
    )


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_fix_cx3_runner_error_and_cleanup_error_remain_visible(
    exp, request_factory, execution_fixture, monkeypatch, capsys, cleanup_fails
):
    request = request_factory(modes=("source",))
    adapters = execution_fixture

    def runner(**kwargs):
        raise OSError("fixture runner disk failure")

    if cleanup_fails:

        def finish(prepared):
            raise gate.GateBlock("fixture_cleanup", "fixture cleanup failure")

        monkeypatch.setattr(adapters.installer, "finish", finish)
    original_execute = exp.execute
    monkeypatch.setattr(exp, "prepare_request", lambda **kwargs: request)
    monkeypatch.setattr(exp, "production_dependencies", lambda req: (adapters.installer, adapters.recorder))
    monkeypatch.setattr(exp, "execute", lambda req, **kwargs: original_execute(req, runner=runner, **kwargs))
    rc = exp.main(
        [
            "accept",
            "--checkout",
            str(request.checkout),
            "--arm",
            "serial",
            "--leg",
            "windows/3.12",
            "--modes",
            "source",
            "--record",
            "unused",
            "--output",
            str(request.output),
        ]
    )
    assert rc == 1
    artifact = json.loads((request.output / "experiment.json").read_text())
    assert artifact["status"] == "blocked"
    assert artifact["error_type"] == "OSError"
    assert artifact["error"] == "fixture runner disk failure"
    if cleanup_fails:
        assert artifact["cleanup_errors"] == [{"type": "GateBlock", "message": "fixture cleanup failure"}]
    err = capsys.readouterr().err
    assert "OSError" in err and "fixture runner disk failure" in err and "Traceback" not in err


@pytest.mark.parametrize(
    "probe,cases", [("p8-gateway-repeat", None), ("p9", "worker,controller"), ("p9", "worker"), ("p9", "controller")]
)
def test_fix_cl2_plan_commands_parse_and_cases_reach_pytest(exp, request_factory, tmp_path, probe, cases):
    argv = [
        "run",
        "--checkout",
        ".",
        "--probe",
        probe,
        "--leg",
        "windows/3.12",
        "--modes",
        "source,wheel",
        "--record",
        "frozen.json",
        "--output",
        "fresh-output",
    ]
    if cases is not None:
        argv += ["--cases", cases]
    args = exp.parse_args(argv)
    request = request_factory(arm=None, probe=probe, cases=getattr(args, "cases", ()))
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "records")}
    call = exp.pytest_call(request, "source", Path(sys.executable), paths, {})
    assert "--max-worker-restart=0" in call["argv"]
    if cases:
        assert call["argv"][call["argv"].index("--cases") + 1] == cases


@pytest.mark.parametrize(
    "probe,cases",
    [
        ("p8", "worker"),
        ("p10", "controller"),
        ("p11", "worker"),
        ("p9", "other"),
        ("p9", "worker,worker"),
        ("p9", "worker,"),
    ],
)
def test_fix_cl2_cases_are_closed_and_only_for_p9(exp, probe, cases):
    with pytest.raises(SystemExit):
        exp.parse_args(
            [
                "run",
                "--checkout",
                ".",
                "--probe",
                probe,
                "--cases",
                cases,
                "--leg",
                "windows/3.12",
                "--modes",
                "source",
                "--record",
                "frozen.json",
                "--output",
                "out",
            ]
        )


def test_fix_l1_p11_is_serial_in_contract_and_call(exp, request_factory, tmp_path):
    request = request_factory(arm=None, probe="p11")
    assert request.contract["probes"]["p11"]["arm"] == "serial"
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "records")}
    assert "-n" not in exp.pytest_call(request, "source", Path(sys.executable), paths, {})["argv"]


def test_fix_cl3_execute_rejects_extra_environment_before_running(exp, request_factory, execution_fixture, monkeypatch):
    request = request_factory(modes=("source",))
    original = exp.pytest_call
    ran = []

    def changed(*args, **kwargs):
        call = original(*args, **kwargs)
        call["env"]["P6_UNDECLARED"] = "1"
        return call

    monkeypatch.setattr(exp, "pytest_call", changed)
    artifact = exp.execute(
        request,
        installer=execution_fixture.installer,
        recorder=execution_fixture.recorder,
        runner=lambda **kwargs: ran.append(kwargs) or SimpleNamespace(status="pass"),
    )
    assert artifact["status"] == "blocked" and "undeclared call difference" in artifact["error"]
    assert ran == []


@pytest.mark.parametrize("mode", ["source", "wheel"])
def test_fix_cl3_execute_uses_ordinary_environment_and_basetemp(
    exp, request_factory, execution_fixture, monkeypatch, mode
):
    request = request_factory(modes=(mode,))
    captured = []
    artifact = exp.execute(
        request,
        installer=execution_fixture.installer,
        recorder=execution_fixture.recorder,
        runner=lambda **kwargs: captured.append(kwargs) or r4_runner(**kwargs),
    )
    assert artifact["status"] == "complete"
    work = request.output / mode
    paths = {
        "source": work / f"candidate-{mode}",
        "imports": work / f"candidate-{mode}" / "src",
        "bt": work / f"pt-{'s' if mode == 'source' else 'w'}-py312",
        "logs": work / "logs",
    }
    env = gate._base_env(work)
    if mode == "source":
        env = gate.source_environment(env, paths["source"])
    ordinary = capture_ordinary(monkeypatch, request.manifest, mode, paths, env)
    actual = captured[0]
    argv = list(actual["argv"])
    argv.remove(f"--experiment-record={work / 'records'}")
    assert argv == ordinary["argv"]
    assert actual["env"] == ordinary["env"]
    assert actual["timeout_seconds"] == ordinary["timeout_seconds"]


@pytest.mark.parametrize("missing", ["dev-gate.json", "dev-gate-experiment.json", "record"])
def test_fix_cl5_missing_input_names_file(exp, request_factory, capsys, missing):
    path = request_factory.record if missing == "record" else request_factory.checkout / missing
    path.unlink()
    rc = exp.main(
        [
            "accept",
            "--checkout",
            str(request_factory.checkout),
            "--arm",
            "serial",
            "--leg",
            "windows/3.12",
            "--modes",
            "source",
            "--record",
            str(request_factory.record),
            "--output",
            "unused",
        ]
    )
    assert rc == 2
    assert path.name in capsys.readouterr().err


def test_fix_cl5_switch_refusal_names_variable(exp, request_factory, monkeypatch, capsys):
    monkeypatch.setenv("AGENTTALK_DEV_GATE_POSIX_PARALLEL", "")
    rc = exp.main(
        [
            "accept",
            "--checkout",
            str(request_factory.checkout),
            "--arm",
            "serial",
            "--leg",
            "windows/3.12",
            "--modes",
            "source",
            "--record",
            str(request_factory.record),
            "--output",
            "unused",
        ]
    )
    assert rc == 2
    assert "AGENTTALK_DEV_GATE_POSIX_PARALLEL" in capsys.readouterr().err


def test_fix_docs_local_identity_and_temporary_d2_proof():
    text = Path("docs/DEV-GATE.md").read_text(encoding="utf-8")
    assert "have a VCS commit" not in text
    assert "dir_info" in text and "RECORD" in text
    assert "Until P1 lands, D2 is checked only by the installer's proof." in text


# Round 4: each negative case starts from a valid request/proof and changes one condition.
def test_r4_windows_fixture_canonicalizes_duplicate_environment(request, monkeypatch, exp, tmp_path):
    environment = dict(os.environ)
    environment.update(HTTP_PROXY="http://127.0.0.1:8080", http_proxy="http://127.0.0.1:8080")
    monkeypatch.setattr(os, "environ", environment)
    factory = request.getfixturevalue("request_factory")
    req = factory(modes=("source",))
    paths = {name: tmp_path / name for name in ("source", "imports", "bt", "logs", "records")}
    call = exp.pytest_call(req, "source", Path(sys.executable), paths, gate._base_env(tmp_path))
    exp.validate_call(call, copy.deepcopy(call))


@pytest.mark.parametrize("condition", ["dirty", "commit"])
def test_r4_candidate_binding_refusal(exp, request_factory, monkeypatch, condition):
    if condition == "dirty":
        monkeypatch.setattr(
            gate, "capture_candidate_binding", lambda root: SimpleNamespace(candidate_sha="a" * 40, clean=False)
        )
    else:
        frozen = json.loads(request_factory.record.read_text())
        frozen["commit"] = "d" * 40
        request_factory.record.write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(gate.GateBlock, match="candidate.*freeze|candidate.*dirty"):
        request_factory()


@pytest.mark.parametrize("condition", ["exists", "inside", "contains", "store"])
def test_r4_output_refusal(exp, request_factory, tmp_path, monkeypatch, condition):
    output = tmp_path / "fresh"
    if condition == "exists":
        output.mkdir()
    elif condition == "inside":
        output = request_factory.checkout / "fresh"
    elif condition == "contains":
        output = tmp_path
        # An ancestor of an existing checkout necessarily exists. Isolate containment
        # from freshness so deleting only the containment guard is observable.
        exists = Path.exists
        monkeypatch.setattr(Path, "exists", lambda path: False if path == output else exists(path))
    else:
        store = tmp_path / "store"
        (store / ".agenttalk").mkdir(parents=True)
        output = store / "fresh"
    with pytest.raises(
        gate.GateBlock, match="output.*" + ("message stores" if condition == "store" else "fresh and external")
    ):
        request_factory(output=output)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("freeze_sha256", "d" * 64, "P7 freeze"),
        ("gateway_policy", "skip", "held-port error policy"),
        ("probe_files", ["observer.pth"], "probe files"),
    ],
)
def test_r4_install_proof_refusal(exp, field, value, reason):
    proof = {"freeze_sha256": "c" * 64, "gateway_policy": "error", "probe_files": []}
    proof[field] = value
    with pytest.raises(gate.GateBlock, match=reason):
        exp.validate_install(proof, "c" * 64)


def r4_runner(**kwargs):
    argv = kwargs["argv"]
    Path(argv[argv.index("--basetemp") + 1]).mkdir()
    kwargs["logs_dir"].mkdir()
    records = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--experiment-record="))
    Path(records).mkdir()
    return SimpleNamespace(status="pass")


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("setup", "setup order"),
        ("checks-order", "reordered ordinary checks"),
        ("checks-failed", "failed.*ordinary checks"),
        ("export", "committed export"),
    ],
)
def test_r4_installer_refusal(exp, request_factory, execution_fixture, monkeypatch, condition, reason):
    req = request_factory(modes=("wheel",))
    original = execution_fixture.installer.prepare

    def prepare(*args):
        prepared = original(*args)
        if condition == "setup":
            prepared.setup_steps = list(reversed(prepared.setup_steps))
        elif condition == "checks-order":
            prepared.checks = list(reversed(prepared.checks))
        elif condition == "checks-failed":
            prepared.checks[0]["status"] = "fail"
        else:
            prepared.source_root = req.output / "different-export"
        return prepared

    monkeypatch.setattr(execution_fixture.installer, "prepare", prepare)
    artifact = exp.execute(
        req, installer=execution_fixture.installer, recorder=execution_fixture.recorder, runner=r4_runner
    )
    assert artifact["status"] == "blocked"
    assert re.search(reason, artifact["error"])


@pytest.mark.parametrize(
    "missing",
    ["records", "logs", "candidate-source", "candidate-package", "candidate-wheel", "candidate-static", "pt-w-py312"],
)
def test_r4_finish_must_keep_evidence(exp, request_factory, execution_fixture, monkeypatch, missing):
    req = request_factory(modes=("wheel",))

    def finish(prepared):
        shutil.rmtree(req.output / "wheel" / missing)

    monkeypatch.setattr(execution_fixture.installer, "finish", finish)
    artifact = exp.execute(
        req, installer=execution_fixture.installer, recorder=execution_fixture.recorder, runner=r4_runner
    )
    assert artifact["status"] == "blocked"
    assert ("basetemp retention" if missing.startswith("pt-") else missing) in artifact["error"]
    assert "cleanup" not in artifact["calls"][0]


@pytest.mark.parametrize("condition", ["dirty", "commit"])
def test_r4_candidate_changed_during_execution(exp, request_factory, execution_fixture, monkeypatch, condition):
    req = request_factory(modes=("source",))
    monkeypatch.setattr(
        gate,
        "capture_candidate_binding",
        lambda root: SimpleNamespace(
            clean=condition != "dirty", candidate_sha="d" * 40 if condition == "commit" else req.commit
        ),
    )
    artifact = exp.execute(
        req, installer=execution_fixture.installer, recorder=execution_fixture.recorder, runner=r4_runner
    )
    assert artifact["status"] == "blocked"
    assert "candidate changed during execution" in artifact["error"]


@pytest.mark.parametrize("complete", [False, 1])
def test_r4_incomplete_inventory_refused(exp, complete):
    with pytest.raises(gate.GateBlock, match="P3 inventory incomplete"):
        exp.validate_inventory({"complete": complete, "plugins": ["pytest", "experiment_record"]}, acceptance=True)


def test_r4_non_windows_host_refused(exp, request_factory, monkeypatch):
    monkeypatch.setattr(gate, "_platform_label", lambda: "linux")
    with pytest.raises(gate.GateBlock, match="Windows host"):
        request_factory()


@pytest.mark.parametrize("missing", ["experiment_record.py", "conftest.py", "option", "comment", "syntax"])
def test_r4_recorder_preflight_names_missing_piece(exp, request_factory, missing):
    tests = request_factory.checkout / "tests"
    if missing in {"option", "comment", "syntax"}:
        content = {
            "option": "def pytest_addoption(parser): pass\n",
            "comment": "# parser.addoption('--experiment-record')\n",
            "syntax": "def pytest_addoption(\n",
        }[missing]
        (tests / "conftest.py").write_text(content, encoding="utf-8")
    else:
        (tests / missing).unlink()
    reason = (
        "--experiment-record"
        if missing in {"option", "comment"}
        else "conftest.py"
        if missing == "syntax"
        else missing.replace(".", r"\.")
    )
    with pytest.raises(gate.GateBlock, match=reason):
        request_factory()


@pytest.mark.parametrize("extra", [0, 1])
def test_r4_read_plain_size_boundary(exp, tmp_path, extra):
    path = tmp_path / "plain"
    data = b"x" * (exp.MAX_JSON_BYTES + extra)
    path.write_bytes(data)
    if extra:
        with pytest.raises(gate.GateBlock, match="configuration exceeds size limit"):
            exp.read_plain(path)
    else:
        assert exp.read_plain(path) == data


def test_r4_finish_must_not_change_absent_basetemp(exp, request_factory, execution_fixture, monkeypatch):
    req = request_factory(modes=("wheel",))
    bt = req.output / "wheel" / "pt-w-py312"

    def runner(**kwargs):
        result = r4_runner(**kwargs)
        bt.rmdir()
        return result

    monkeypatch.setattr(execution_fixture.installer, "finish", lambda prepared: bt.mkdir())
    artifact = exp.execute(
        req, installer=execution_fixture.installer, recorder=execution_fixture.recorder, runner=runner
    )
    assert artifact["status"] == "blocked"
    assert "basetemp retention" in artifact["error"]
    assert "cleanup" not in artifact["calls"][0]


@pytest.mark.parametrize("shape", ["delegated", "unused", "missing"])
def test_r5_recorder_preflight_agrees_with_real_child(exp, request_factory, shape):
    checkout = request_factory.checkout
    if shape == "unused":
        (checkout / "tests" / "conftest.py").write_text(
            "def unused(parser): parser.addoption('--experiment-record')\n", encoding="utf-8"
        )
    elif shape == "missing":
        (checkout / "tests" / "experiment_record.py").unlink()
    control = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--help", "-p", "no:cacheprovider", "tests"],
        cwd=checkout,
        env=gate._base_env(checkout),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if shape == "delegated":
        assert control.returncode == 0, control.stderr
        assert "--experiment-record" in control.stdout
        assert request_factory().checkout == checkout
    else:
        if shape == "unused":
            assert control.returncode == 0, control.stderr
            assert "--experiment-record" not in control.stdout
        else:
            assert "--experiment-record" not in control.stdout
        with pytest.raises(gate.GateBlock, match="experiment_record" if shape == "missing" else "--experiment-record"):
            request_factory()


def test_r5_docs_name_recorder_option_owner():
    text = (Path(__file__).parents[1] / "docs" / "DEV-GATE.md").read_text(encoding="utf-8")
    assert "`tests/experiment_record.py` defines the `--experiment-record` option" in text
    assert "`tests/conftest.py` registers it through its `pytest_addoption` hook" in text


@pytest.mark.parametrize("failure", [OSError("child unavailable"), subprocess.TimeoutExpired("pytest", 30)])
def test_r5_recorder_child_failure_is_named(exp, request_factory, monkeypatch, failure):
    def run(*args, **kwargs):
        assert kwargs["timeout"] == 30
        assert kwargs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        raise failure

    monkeypatch.setattr(exp.subprocess, "run", run)
    with pytest.raises(gate.GateBlock, match="conftest.py --experiment-record preflight"):
        request_factory()


def p7_wheel(folder, name, version="1.0", dependencies=()):
    stem = name.replace("-", "_")
    info = f"{stem}-{version}.dist-info"
    files = {
        f"{stem}/__init__.py": b"",
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: p7-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in dependencies)
        ).encode(),
    }
    rows = [
        [path, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="), str(len(data))]
        for path, data in files.items()
    ]
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows([*rows, [f"{info}/RECORD", "", ""]])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    result = folder / f"{stem}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(result, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return result


@pytest.fixture(params=[False, True], ids=["native-bootstrap", "setuptools-bootstrap"])
def p7_bootstrap_wheels(request, tmp_path):
    import ensurepip

    wheels = sorted((Path(ensurepip.__file__).parent / "_bundled").glob("*.whl"))
    # Python 3.10/3.11 bundle setuptools too; exercise that baseline on newer hosts.
    if request.param and not any(wheel.name.startswith("setuptools-") for wheel in wheels):
        wheels.append(p7_wheel(tmp_path, "setuptools", "65.5.0"))
    return wheels


def p7_create_venv(folder, env, bootstrap_wheels):
    # Install complete wheel RECORDs rather than depending on venv's seeded metadata.
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(folder)], env=env, check=True, timeout=120)
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pip = next(wheel for wheel in bootstrap_wheels if wheel.name.startswith("pip-"))
    bootstrap = "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('pip', run_name='__main__')"
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            bootstrap,
            str(pip),
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            *map(str, bootstrap_wheels),
        ],
        env=env,
        check=True,
        timeout=120,
    )
    return python


def p7_lock_file(folder, packages):
    path = folder / "runtime.txt"
    path.write_text(
        "\n".join(
            f"{name}=={version} --hash=sha256:{'a' * 64} # wheel={name.replace('-', '_')}-{version}-py3-none-any.whl"
            for name, version in packages.items()
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def p7_inventory(packages):
    return {
        "python_version": "3.12.9",
        "pip_version": packages["pip"],
        "packages": [
            {"name": name, "version": version, "record_sha256": "b" * 64} for name, version in packages.items()
        ],
        "unowned_files": {},
        "damaged_files": [],
    }


def p7_role_packages(role):
    roots = {
        "host": ("hatchling",),
        "build": ("hatchling", "build"),
        "runtime": (),
        "wheel-test": ("pytest", "pytest-xdist", "pluggy", "execnet"),
    }
    return {"pip": "25.2", **dict.fromkeys(roots[role], "1.0")}


@pytest.mark.parametrize("role", ["host", "build", "wheel-test"])
def test_lock_role_requires_its_declared_roots(tmp_path, role):
    path = p7_lock_file(tmp_path, {"pip": "25.2"})
    with pytest.raises(gate.GateBlock):
        frozen_installs.read_lock(path, minor="3.12", role=role)


@pytest.mark.parametrize("change", ["transitive", "extra", "missing", "runtime-pytest", "duplicate"])
def test_lock_installed_set_refuses_distribution_drift(tmp_path, change):
    # Plan P7 tests 1,2,6: equality includes transitive pins, not only top-level inputs.
    packages = {"pip": "25.2", "parent": "1.0", "leaf": "2.0"}
    path = p7_lock_file(tmp_path, packages)
    observed = p7_inventory(packages)
    if change == "transitive":
        observed["packages"][-1]["version"] = "2.1"
    elif change == "missing":
        observed["packages"].pop()
    elif change == "duplicate":
        observed["packages"].append(dict(observed["packages"][0], name="PIP"))
    else:
        name = "pytest" if change == "runtime-pytest" else "extra"
        observed["packages"].append({"name": name, "version": "1", "record_sha256": "c" * 64})
    with pytest.raises(gate.GateBlock):
        frozen_installs.check_installed_set(path, observed, role="runtime", minor="3.12")


def test_lock_changed_wheel_refused_before_install(tmp_path):
    # Plan P7 test 3: candidate bytes are checked against the built wheel identity.
    candidate = p7_wheel(tmp_path, "agenttalk")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    candidate.write_bytes(candidate.read_bytes() + b"changed")
    with pytest.raises(gate.GateBlock):
        frozen_installs.candidate_install_argv(Path(sys.executable), candidate, digest)


@pytest.mark.parametrize("damage", ["changed", "unlisted", "acceptance", "owned", "escape"])
def test_lock_observer_file_identity_is_separate(tmp_path, damage):
    # Plan P7 test 4 and file-level ruling: metadata alone cannot see observer injection.
    path = p7_lock_file(tmp_path, {"pip": "25.2"})
    observed = p7_inventory({"pip": "25.2"})
    allowed = {"audit_probe.pth": "c" * 64}
    observed["unowned_files"] = dict(allowed)
    if damage == "changed":
        observed["unowned_files"]["audit_probe.pth"] = "d" * 64
    elif damage == "unlisted":
        observed["unowned_files"]["other.py"] = "d" * 64
    elif damage == "owned":
        observed["damaged_files"] = ["pip/__init__.py"]
    elif damage == "escape":
        allowed = {"../audit_probe.pth": "c" * 64}
        observed["unowned_files"] = dict(allowed)
    with pytest.raises(gate.GateBlock):
        frozen_installs.check_installed_set(
            path, observed, role="runtime", minor="3.12", observers=allowed, acceptance=damage == "acceptance"
        )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "# empty\n",
        "pip>=1",
        "pip==1 # wheel=pip-1-py3-none-any.whl",
        "pip==1 --hash=sha256:bad",
        f"pip==1 --hash=sha256:{'a' * 64} # wheel=pip-1.tar.gz",
        f"other==1 --hash=sha256:{'a' * 64} # wheel=other-1-py3-none-any.whl",
    ],
)
def test_lock_empty_unpinned_or_nonwheel_is_refused(tmp_path, content):
    # Empty requirements can succeed in pip: explicit lock/bootstrap validation is required.
    path = tmp_path / "runtime.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(gate.GateBlock):
        frozen_installs.read_lock(path, minor="3.12", role="runtime")


@pytest.mark.parametrize("role", ["host", "build", "runtime", "wheel-test"])
def test_lock_installer_argv_is_hashed_and_no_resolution(tmp_path, role):
    # Plan P7 test 7 and D02: every role uses one pinned lock; local candidate is hash checked separately.
    folder = tmp_path / "lock folder; literal"
    folder.mkdir()
    path = p7_lock_file(folder, {"pip": "25.2"})
    argv = frozen_installs.lock_install_argv(Path(sys.executable), path)
    assert argv[:5] == [sys.executable, "-I", "-m", "pip", "install"]
    for flag in ("--require-hashes", "--no-deps", "--no-cache-dir", "--only-binary=:all:"):
        assert flag in argv
    assert "--force-reinstall" in argv
    assert argv[argv.index("-r") + 1] == str(path)
    assert "--index-url" not in argv
    candidate = p7_wheel(folder, "agenttalk")
    call = frozen_installs.candidate_install_argv(
        Path(sys.executable), candidate, hashlib.sha256(candidate.read_bytes()).hexdigest()
    )
    assert "--no-deps" in call and "--no-index" in call
    assert call[-1] == str(candidate)


def test_lock_ordinary_install_argv_stays_unpinned(tmp_path, monkeypatch):
    # Plan P7 test 5: the ordinary gate continues to resolve its own inputs.
    calls = []
    log = tmp_path / "command.log"
    log.write_text("pip fixture\n", encoding="utf-8")

    def run(**kwargs):
        calls.append(kwargs["argv"])
        return gate.CommandOutcome(tuple(kwargs["argv"]), 0, 1, "pass", None, "", log)

    monkeypatch.setattr(gate, "run_command", run)
    gate._install_wheel(
        interpreter=gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", "3.12.9"),
        runtime_environment={},
        wheel=tmp_path / "candidate.whl",
        source_root=tmp_path,
        env={},
        manifest={
            "checks": {"wheel-contract": {"dependency_index": "https://example.invalid/simple", "timeout_seconds": 10}}
        },
        logs_dir=tmp_path,
    )
    assert "--require-hashes" not in calls[-1]
    assert "--index-url" in calls[-1]
    assert "-r" not in calls[-1]


def test_lock_undeclared_backend_fails_offline(tmp_path):
    # Plan P7 test 8: use the real installed build frontend, no isolation and no index.
    pytest.importorskip("build")
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires=["p7-backend-does-not-exist"]\nbuild-backend="p7_missing_backend"\n', encoding="utf-8"
    )
    argv, env = frozen_installs.pinned_build_call(Path(sys.executable), tmp_path / "dist", gate._base_env(tmp_path))
    assert "--no-isolation" in argv
    assert env["PIP_NO_INDEX"] == "1"
    result = subprocess.run(argv, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode != 0
    assert (
        "p7_missing_backend" in result.stdout + result.stderr
        or "p7-backend-does-not-exist" in result.stdout + result.stderr
    )


def test_lock_normalised_names_patch_version_and_record_proof(tmp_path):
    path = p7_lock_file(tmp_path, {"pip": "25.2", "example-package": "1.0"})
    observed = p7_inventory({"pip": "25.2", "EXAMPLE_package": "1.0"})
    proof = frozen_installs.check_installed_set(path, observed, role="runtime", minor="3.12", python_version="3.12.9")
    assert proof["packages"] == {"pip": "25.2", "example-package": "1.0"}
    assert proof["python_version"] == "3.12.9"
    assert proof["records"]["example-package"] == "b" * 64
    with pytest.raises(gate.GateBlock):
        frozen_installs.check_installed_set(path, observed, role="runtime", minor="3.12", python_version="3.12.10")


@pytest.mark.parametrize("minor", ["3.10", "3.11"])
def test_p7_legacy_bootstrap_requires_setuptools_pin_and_record(tmp_path, minor):
    packages = {"pip": "25.2", "setuptools": "65.5.0"}
    observed = p7_inventory(packages)
    observed["python_version"] = minor + ".9"
    lock = p7_lock_file(tmp_path, packages)
    proof = frozen_installs.check_installed_set(lock, observed, role="runtime", minor=minor)
    assert proof["packages"] == packages
    observed["packages"][1]["record_sha256"] = None
    with pytest.raises(gate.GateBlock, match="distribution RECORD missing: setuptools"):
        frozen_installs.check_installed_set(lock, observed, role="runtime", minor=minor)
    p7_lock_file(tmp_path, {"pip": "25.2"})
    with pytest.raises(gate.GateBlock, match="lock missing bootstrap pins: setuptools"):
        frozen_installs.read_lock(lock, role="runtime", minor=minor)


def test_lock_local_fixture_wheel_install_and_file_inventory(tmp_path, p7_bootstrap_wheels):
    # Real offline boundary: interpreter bootstrap, fixture wheel, metadata and a stray .pth.
    candidate = p7_wheel(tmp_path, "agenttalk")
    local = p7_wheel(tmp_path, "example-package", dependencies=("pip>=1",))
    lock = tmp_path / "runtime.txt"
    lock.write_text(
        "\n".join(
            f"{artifact.name.split('-')[0]}=={artifact.name.split('-')[1]} "
            f"--hash=sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()} "
            f"# wheel={artifact.name}"
            for artifact in [*p7_bootstrap_wheels, local]
        )
        + "\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PIP_NO_INDEX="1", PIP_FIND_LINKS=str(tmp_path))
    env["PIP_CONFIG_FILE"] = os.devnull
    for wheel in p7_bootstrap_wheels:
        if wheel.parent != tmp_path:
            (tmp_path / wheel.name).write_bytes(wheel.read_bytes())
    python = p7_create_venv(tmp_path / "venv", env, p7_bootstrap_wheels)
    subprocess.run(frozen_installs.lock_install_argv(python, lock), env=env, check=True, timeout=120)
    subprocess.run(
        frozen_installs.candidate_install_argv(python, candidate, hashlib.sha256(candidate.read_bytes()).hexdigest()),
        env=env,
        check=True,
        timeout=120,
    )
    observed = frozen_installs.environment_inventory(python, cwd=tmp_path, env=env)
    proof = frozen_installs.check_installed_set(
        lock,
        observed,
        role="runtime",
        minor=f"{sys.version_info.major}.{sys.version_info.minor}",
        candidate={"name": "agenttalk", "version": "1.0"},
    )
    assert proof["packages"]["example-package"] == "1.0"
    assert len(proof["records"]["agenttalk"]) == 64
    site = Path(observed["site_packages"][0])
    assert (
        proof["records"]["agenttalk"]
        == hashlib.sha256((site / "agenttalk-1.0.dist-info" / "RECORD").read_bytes()).hexdigest()
    )
    (site / "stray.pth").write_text("# passive sentinel\n", encoding="utf-8")
    with pytest.raises(gate.GateBlock):
        frozen_installs.check_installed_set(
            lock,
            frozen_installs.environment_inventory(python, cwd=tmp_path, env=env),
            role="runtime",
            minor=f"{sys.version_info.major}.{sys.version_info.minor}",
            candidate={"name": "agenttalk", "version": "1.0"},
        )


@pytest.mark.parametrize("damage", ["patch", "lock", "freeze"])
def test_lock_environment_plan_freezes_each_role_and_bootstrap(tmp_path, damage):
    checkout = tmp_path / "checkout"
    root = checkout / "locks" / "3.12"
    root.mkdir(parents=True)
    (checkout / "dev-gate-requirements.txt").write_text("hatchling>=1\n", encoding="utf-8")
    for role in ("host", "build", "runtime", "wheel-test"):
        path = p7_lock_file(root, p7_role_packages(role))
        if role != "runtime":
            (root / (role + ".txt")).write_bytes(path.read_bytes())
    p7_lock_file(root, {"pip": "25.2"})
    plan = frozen_installs.make_environment_plan(
        root,
        minor="3.12",
        python_version="3.12.9",
        bootstrap_pip_version="25.2",
        candidate_version="1.0",
        wheel_sha256="d" * 64,
    )
    installer = frozen_installs.PinnedInstaller(gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", "3.12.9"))
    installer.validate_plan(root, plan)
    if damage == "patch":
        installer.creator = gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", "3.12.10")
    elif damage == "lock":
        (root / "host.txt").write_text("# removed\n", encoding="utf-8")
    else:
        plan["bootstrap_pip_version"] = "24.0"
    with pytest.raises(gate.GateBlock):
        installer.validate_plan(root, plan)


@pytest.mark.parametrize("role", ["host", "build", "runtime", "wheel-test"])
def test_lock_role_installer_pins_then_checks_actual_environment(tmp_path, monkeypatch, role):
    packages = p7_role_packages(role)
    path = p7_lock_file(tmp_path, packages)
    plan = {
        "python_version": "3.12.9",
        "bootstrap_pip_version": "25.2",
        "candidate_version": "1.0",
        "wheel_sha256": "d" * 64,
        "observers": {},
    }
    installer = frozen_installs.PinnedInstaller(gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", "3.12.9"))
    calls = []
    monkeypatch.setattr(gate, "_create_isolated_venv", lambda **kwargs: (installer.creator, {"prefix": str(tmp_path)}))
    monkeypatch.setattr(installer, "run", lambda argv, **kwargs: calls.append(argv))
    snapshots = iter([p7_inventory({"pip": "25.2"}), p7_inventory(packages)])
    monkeypatch.setattr(frozen_installs, "environment_inventory", lambda *args, **kwargs: next(snapshots))
    interpreter, _, proof = installer.install_role(
        role, path, plan, tmp_path / role, tmp_path, candidate=None, acceptance=True
    )
    assert interpreter.path == installer.creator.path
    assert proof["role"] == role
    assert proof["bootstrap_pip_version"] == "25.2"
    assert "--require-hashes" in calls[0] and "--no-deps" in calls[0]
    assert calls[-1][-2:] == ["pip", "check"]


@pytest.mark.parametrize("mode", ["source", "wheel"])
def test_lock_prepare_covers_only_the_declared_environment_roles(tmp_path, monkeypatch, mode):
    installer = frozen_installs.PinnedInstaller(gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", "3.12.9"))
    plan = {"freeze_sha256": "a" * 64, "candidate_version": "1.0", "wheel_sha256": "b" * 64, "observers": {}}
    request = SimpleNamespace(
        leg="windows/3.12",
        checkout=tmp_path,
        probe=None,
        frozen={"environments": {"3.12": plan}},
        manifest={},
        contract={"gateway_policy": "error"},
    )
    monkeypatch.setattr(installer, "validate_plan", lambda *args: None)
    roles = []

    def install(role, *args, **kwargs):
        roles.append(role)
        return installer.creator, {"prefix": str(tmp_path)}, {"role": role}

    monkeypatch.setattr(installer, "install_role", install)
    monkeypatch.setattr(installer, "non_pytest_checks", lambda *args: [])
    monkeypatch.setattr(installer, "package", lambda *args: (tmp_path / "candidate.whl", {"id": "package-build"}))
    prepared = installer.prepare(request, mode, tmp_path / "work")
    assert roles == (["host"] if mode == "source" else ["host", "build", "runtime", "wheel-test"])
    assert prepared.setup_steps == roles
    assert set(prepared.installed_set["environments"]) == set(roles)
    assert prepared.installed_set["freeze_sha256"] == "a" * 64


@pytest.mark.parametrize("damage", ["dependency", "wheel-hash", "unused"])
def test_lock_wheel_graph_is_complete_and_hashes_real_wheels(tmp_path, damage):
    import ensurepip

    pip_wheel = next((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
    artifacts = {
        "pip": pip_wheel,
        "parent": p7_wheel(tmp_path, "parent", dependencies=("leaf>=2",)),
        "leaf": p7_wheel(tmp_path, "leaf", "2.0"),
    }
    (tmp_path / pip_wheel.name).write_bytes(pip_wheel.read_bytes())
    lock = tmp_path / "runtime.txt"

    def write():
        lock.write_text(
            "\n".join(
                f"{name}=={path.name.split('-')[1]} --hash=sha256:{hashlib.sha256(path.read_bytes()).hexdigest()} "
                f"# wheel={path.name}"
                for name, path in artifacts.items()
            )
            + "\n",
            encoding="utf-8",
        )

    write()
    frozen_installs.validate_lock_wheels(lock, tmp_path, minor="3.12", role="runtime", requirements=["pip", "parent"])
    if damage == "dependency":
        del artifacts["leaf"]
        write()
    elif damage == "wheel-hash":
        artifacts["parent"].write_bytes(artifacts["parent"].read_bytes() + b"changed")
    else:
        artifacts["unused"] = p7_wheel(tmp_path, "unused")
        write()
    with pytest.raises(gate.GateBlock):
        frozen_installs.validate_lock_wheels(
            lock, tmp_path, minor="3.12", role="runtime", requirements=["pip", "parent"]
        )


@pytest.mark.parametrize("damage", ["deleted", "changed", "unhashed-deleted"])
def test_p7_record_damage_names_file(tmp_path, damage, p7_bootstrap_wheels):
    # A real install is required: Distribution.files can hide deleted RECORD entries.
    wheel = p7_wheel(tmp_path, "example-package")
    env = dict(gate._base_env(tmp_path), PIP_NO_INDEX="1", PIP_CONFIG_FILE=os.devnull)
    python = p7_create_venv(tmp_path / "v", env, p7_bootstrap_wheels)
    subprocess.run(
        [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        env=env,
        check=True,
        timeout=120,
    )
    baseline = frozen_installs.environment_inventory(python, cwd=tmp_path, env=env)
    site = Path(baseline["site_packages"][0])
    target = site / "example_package" / "__init__.py"
    expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(b"").digest()).decode().rstrip("=")
    observed = "<missing>"
    if damage == "unhashed-deleted":
        target = site / "example_package" / "unhashed.bin"
        target.write_bytes(b"presence only")
        record = site / "example_package-1.0.dist-info" / "RECORD"
        with record.open("a", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow(["example_package/unhashed.bin", "", ""])
        expected = "present"
        baseline = frozen_installs.environment_inventory(python, cwd=tmp_path, env=env)
    lock = p7_lock_file(tmp_path, {row["name"]: row["version"] for row in baseline["packages"]})
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    frozen_installs.check_installed_set(lock, baseline, role="runtime", minor=minor)
    if damage == "changed":
        target.write_bytes(b"changed")
        observed = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(b"changed").digest()).decode().rstrip("=")
    else:
        target.unlink()
    inventory = frozen_installs.environment_inventory(python, cwd=tmp_path, env=env)
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(lock, inventory, role="runtime", minor=minor)
    message = str(failure.value)
    assert str(target) in message
    assert f"expected {expected}" in message and f"observed {observed}" in message


def test_p7_lock_refusal_names_line(tmp_path):
    lock = tmp_path / "runtime.txt"
    lock.write_text("# header\npip==1 # wheel=pip-1-py3-none-any.whl\n", encoding="utf-8")
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.read_lock(lock, minor="3.12", role="runtime")
    assert f"{lock}:2" in str(failure.value)


@pytest.mark.parametrize("change", ["extra", "missing", "drift"])
def test_p7_set_refusal_names_versions(tmp_path, change):
    lock = p7_lock_file(tmp_path, {"pip": "25.2", "example-package": "1.0"})
    inventory = p7_inventory({"pip": "25.2", "EXAMPLE_package": "1.0"})
    name, expected, observed = "example-package", "1.0", "<missing>"
    if change == "missing":
        inventory["packages"].pop()
    elif change == "drift":
        inventory["packages"][-1]["version"] = "2.0"
        observed = "2.0"
    else:
        inventory["packages"].append({"name": "OTHER_pkg", "version": "3", "record_sha256": "c" * 64})
        name, expected, observed = "other-pkg", "<missing>", "3"
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(lock, inventory, role="runtime", minor="3.12")
    message = str(failure.value)
    assert name in message and f"expected {expected}" in message and f"observed {observed}" in message


def test_p7_refusal_lists_are_bounded(tmp_path):
    lock = p7_lock_file(tmp_path, {"pip": "25.2"})
    inventory = p7_inventory({"pip": "25.2", **{f"extra-{i:02d}": "1" for i in range(25)}})
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(lock, inventory, role="runtime", minor="3.12")
    message = str(failure.value)
    assert "extra-00" in message and "extra-19" in message
    assert "extra-20" not in message and "5 more" in message


def test_p7_locked_wheel_refusal_names_hashes(tmp_path):
    wheel = p7_wheel(tmp_path, "pip")
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = p7_lock_file(tmp_path, {"pip": "1.0"})
    lock.write_text(lock.read_text().replace("a" * 64, expected))
    wheel.write_bytes(wheel.read_bytes() + b"changed")
    observed = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.validate_lock_wheels(lock, tmp_path, minor="3.12", role="runtime", requirements=["pip"])
    message = str(failure.value)
    assert str(wheel) in message and expected in message and observed in message


def test_p7_candidate_wheel_refusal_names_hashes(tmp_path):
    wheel = p7_wheel(tmp_path, "agenttalk")
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.write_bytes(wheel.read_bytes() + b"changed")
    observed = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.candidate_install_argv(Path(sys.executable), wheel, expected)
    message = str(failure.value)
    assert str(wheel) in message and expected in message and observed in message


def test_p7_observer_refusal_names_hashes(tmp_path):
    lock = p7_lock_file(tmp_path, {"pip": "25.2"})
    inventory = p7_inventory({"pip": "25.2"})
    inventory["unowned_files"] = {"audit_probe.pth": "d" * 64}
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(
            lock, inventory, role="runtime", minor="3.12", acceptance=False, observers={"audit_probe.pth": "c" * 64}
        )
    message = str(failure.value)
    assert "audit_probe.pth" in message and "c" * 64 in message and "d" * 64 in message


def test_p7_observer_source_refusal_names_hashes(tmp_path, monkeypatch):
    source = tmp_path / "audit_probe.pth"
    source.write_bytes(b"changed")
    expected, observed = "c" * 64, hashlib.sha256(b"changed").hexdigest()
    lock = p7_lock_file(tmp_path, {"pip": "25.2"})
    creator = gate.InterpreterInfo("3.12", Path(sys.executable), "CPython", "3.12.9")
    installer = frozen_installs.PinnedInstaller(creator, observer_sources={"runtime": {source.name: source}})
    plan = {
        "python_version": "3.12.9",
        "bootstrap_pip_version": "25.2",
        "observers": {"runtime": {source.name: expected}},
    }
    monkeypatch.setattr(gate, "_create_isolated_venv", lambda **kwargs: (creator, {}))
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: None)
    snapshot = dict(p7_inventory({"pip": "25.2"}), site_packages=[str(tmp_path / "site")])
    monkeypatch.setattr(frozen_installs, "environment_inventory", lambda *args, **kwargs: snapshot)
    with pytest.raises(gate.GateBlock) as failure:
        installer.install_role("runtime", lock, plan, tmp_path / "v", tmp_path, candidate=None, acceptance=False)
    message = str(failure.value)
    assert str(source) in message and expected in message and observed in message


def test_p7_docs_record_scope_and_runner_combo():
    text = Path("docs/DEV-GATE.md").read_text(encoding="utf-8")
    assert "raw RECORD rows" in text and "presence only" in text
    assert "pytest 9.2.0" in text and "xdist 3.8.0" in text and "grouped-ID repair" in text


@pytest.mark.parametrize("field", ["damaged_files", "unowned_files"])
def test_p7_file_refusal_lists_are_bounded(tmp_path, field):
    lock = p7_lock_file(tmp_path, {"pip": "25.2"})
    inventory = p7_inventory({"pip": "25.2"})
    paths = [f"file-{i:02d}.py" for i in range(25)]
    inventory[field] = paths if field == "damaged_files" else dict.fromkeys(paths, "c" * 64)
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(lock, inventory, role="runtime", minor="3.12")
    message = str(failure.value)
    assert paths[0] in message and paths[19] in message
    assert paths[20] not in message and "5 more" in message


@pytest.mark.parametrize("field", ["version", "record-path", "record-hash"])
def test_p7_refusal_escapes_real_metadata(tmp_path, field, p7_bootstrap_wheels):
    wheel = p7_wheel(tmp_path, "example-package")
    env = dict(gate._base_env(tmp_path), PIP_NO_INDEX="1", PIP_CONFIG_FILE=os.devnull)
    python = p7_create_venv(tmp_path / "v", env, p7_bootstrap_wheels)
    subprocess.run(
        [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        env=env,
        check=True,
        timeout=120,
    )
    baseline = frozen_installs.environment_inventory(python, cwd=tmp_path, env=env)
    lock = p7_lock_file(tmp_path, {row["name"]: row["version"] for row in baseline["packages"]})
    info = Path(baseline["site_packages"][0]) / "example_package-1.0.dist-info"
    forged = "experiment complete: all checks passed"
    if field == "version":
        metadata = info / "METADATA"
        metadata.write_text(
            metadata.read_text(encoding="utf-8").replace("Version: 1.0", "Version: 1.0\n " + forged), encoding="utf-8"
        )
    else:
        record = info / "RECORD"
        rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
        if field == "record-path":
            rows.append(["example_package/missing\n" + forged, "", ""])
        else:
            for row in rows:
                if row[0] == "example_package/__init__.py":
                    row[1] = "sha256=bad\n" + forged
        with record.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(rows)
    inventory = frozen_installs.environment_inventory(python, cwd=tmp_path, env=env)
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(
            lock, inventory, role="runtime", minor=f"{sys.version_info.major}.{sys.version_info.minor}"
        )
    message = str(failure.value)
    assert len(message.splitlines()) == 1
    assert "\\n" in message and forged in message
    if field != "version":
        assert all(len(detail.splitlines()) == 1 and "\\n" in detail for detail in inventory["damaged_files"])


@pytest.mark.parametrize("surface", ["duplicate", "record-digest", "pip-version", "unowned", "lock-path"])
def test_p7_refusal_escapes_other_echoed_values(tmp_path, surface):
    lock = p7_lock_file(tmp_path, {"pip": "25.2"})
    inventory = p7_inventory({"pip": "25.2"})
    poison = "unexpected\nexperiment complete: all checks passed\x1b[0m"
    if surface == "duplicate":
        inventory["packages"].append({"name": "pip", "version": poison, "record_sha256": "a" * 64})
    elif surface == "record-digest":
        inventory["packages"][0]["record_sha256"] = poison
    elif surface == "pip-version":
        inventory["pip_version"] = poison
    elif surface == "unowned":
        inventory["unowned_files"] = {poison: "a" * 64}
    else:

        class BadPath:
            def __fspath__(self):
                return str(lock)

            def __str__(self):
                return poison

        lock.write_text("pip>=1", encoding="utf-8")
        lock = BadPath()
    with pytest.raises(gate.GateBlock) as failure:
        frozen_installs.check_installed_set(lock, inventory, role="runtime", minor="3.12")
    message = str(failure.value)
    assert len(message.splitlines()) == 1 and "\x1b" not in message
    assert "\\n" in message and "\\u001b" in message


@pytest.mark.parametrize("mode", ["source", "wheel"])
@pytest.mark.parametrize("fault", ["exact", "missing", "extra", "changed", "list-missing", "empty"])
def test_p7_execute_checks_declared_observer_hashes(exp, request_factory, monkeypatch, mode, fault):
    req = request_factory(probe="p9", arm=None, cases=("worker",), modes=(mode,))
    declared = (
        {
            "host": {"experiment/death_probe.py": "a" * 64},
            "wheel-test": {"experiment/death_probe.py": "b" * 64},
        }
        if fault != "empty"
        else {}
    )
    req.frozen["environments"]["3.12"]["observers"] = declared
    installed = {role: dict(files) for role, files in declared.items() if mode == "wheel" or role == "host"}
    bad_name = "experiment/death_probe.py"
    if fault == "missing":
        installed["host"].pop(bad_name)
    elif fault == "changed":
        installed["host"][bad_name] = "d" * 64
    elif fault == "extra":
        bad_name = "experiment/injected\nforged.py"
        installed["host"][bad_name] = "e" * 64
    files = sorted({name for row in installed.values() for name in row})
    if fault == "list-missing":
        files = []
    proof = {
        "freeze_sha256": "c" * 64,
        "gateway_policy": "error",
        "probe_files": files,
        "wheel_sha256": "d" * 64,
    }
    if fault != "empty":
        proof["environments"] = {role: {"observer_hashes": values} for role, values in installed.items()}
    called = []

    def export(binding, destination, archive):
        destination.mkdir()

    monkeypatch.setattr(gate, "export_candidate", export)

    class Installer:
        def prepare(self, request, mode, work):
            checks = [
                name
                for name in exp.partition(request.manifest, request.leg, mode)
                if name not in {"git-binding", "final-binding"} and not name.startswith("pytest-")
            ]
            source = work / f"candidate-{mode}"
            return exp.PreparedEnvironment(
                Path(sys.executable),
                source,
                source / "src" if mode == "source" else source,
                proof,
                ["host"] if mode == "source" else ["host", "build", "runtime", "wheel-test"],
                [{"id": name, "status": "pass"} for name in checks],
            )

        def finish(self, prepared):
            pass

    class Recorder:
        def inventory(self, directory):
            return {"complete": True, "plugins": ["pytest", "experiment_record", "experiment.death_probe"]}

    def runner(**kwargs):
        called.append(True)
        r4_runner(**kwargs)
        return SimpleNamespace(status="pass")

    artifact = exp.execute(req, installer=Installer(), recorder=Recorder(), runner=runner)
    if fault in {"exact", "empty"}:
        assert artifact["status"] == "complete", artifact
        assert called == [True]
        recorded = json.loads((req.output / "experiment.json").read_text(encoding="utf-8"))
        assert recorded["calls"][0]["installed_set"] == proof
        if fault == "exact":
            assert {
                role: row["observer_hashes"]
                for role, row in recorded["calls"][0]["installed_set"]["environments"].items()
            } == installed
    else:
        assert artifact["status"] == "blocked", artifact
        assert called == []
        assert bad_name.replace("\n", "\\n") in artifact["error"]
        assert len(artifact["error"].splitlines()) == 1
        if fault == "changed":
            assert "a" * 64 in artifact["error"] and "d" * 64 in artifact["error"]


def test_p7_acceptance_rejects_hidden_observer_hashes(exp):
    proof = {
        "freeze_sha256": "c" * 64,
        "gateway_policy": "error",
        "probe_files": [],
        "environments": {"host": {"observer_hashes": {"experiment/hidden.py": "a" * 64}}},
    }
    with pytest.raises(gate.GateBlock) as failure:
        exp.validate_install(proof, "c" * 64)
    assert "experiment/hidden.py" in str(failure.value)


def test_p7_acceptance_refuses_even_declared_observers(exp):
    files = {"experiment/death_probe.py": "a" * 64}
    proof = {
        "freeze_sha256": "c" * 64,
        "gateway_policy": "error",
        "probe_files": list(files),
        "environments": {"host": {"observer_hashes": files}},
    }
    with pytest.raises(gate.GateBlock, match="probe files present in acceptance"):
        exp.validate_install(proof, "c" * 64, observers={"host": files})


@pytest.mark.parametrize("fault", ["bad-hash", "outside-path", "unknown-role", "missing-roles", "duplicate-list"])
def test_p7_observer_proof_is_validated(exp, fault):
    name = "experiment/death_probe.py"
    declared = {"host": {name: "a" * 64}}
    proof = {
        "freeze_sha256": "c" * 64,
        "gateway_policy": "error",
        "probe_files": [name],
        "environments": {"host": {"observer_hashes": {name: "a" * 64}}},
    }
    if fault == "bad-hash":
        declared["host"][name] = "invalid\nforged"
        proof["environments"]["host"]["observer_hashes"][name] = "invalid\nforged"
    elif fault == "outside-path":
        declared = {"host": {"../outside.py": "a" * 64}}
        proof["environments"] = {"host": {"observer_hashes": dict(declared["host"])}}
        proof["probe_files"] = ["../outside.py"]
    elif fault == "unknown-role":
        declared = {"unexpected": {name: "a" * 64}}
        proof["environments"] = {"unexpected": {"observer_hashes": dict(declared["unexpected"])}}
    elif fault == "missing-roles":
        del proof["environments"]
    else:
        proof["probe_files"].append(name)
    with pytest.raises(gate.GateBlock) as failure:
        exp.validate_install(proof, "c" * 64, acceptance=False, observers=declared)
    message = str(failure.value)
    assert len(message.splitlines()) == 1
    assert ("outside.py" if fault == "outside-path" else "unexpected" if fault == "unknown-role" else name) in message


def test_p7_observer_refusals_keep_twenty_name_limit(exp):
    declared = {"host": {f"experiment/file-{i:02d}.py": "a" * 64 for i in range(25)}}
    proof = {"freeze_sha256": "c" * 64, "gateway_policy": "error", "probe_files": []}
    with pytest.raises(gate.GateBlock) as failure:
        exp.validate_install(proof, "c" * 64, acceptance=False, observers=declared)
    message = str(failure.value)
    assert "file-00.py" in message and "file-19.py" in message
    assert "file-20.py" not in message and "5 more" in message
