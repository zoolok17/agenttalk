"""Non-voting Windows experiment contract; the ordinary gate is never modified.

Integration interfaces (P7/P3 supply implementations, not CLI options):
Installer.prepare(request, mode, workdir) -> PreparedEnvironment; finish(prepared).
The installer owns frozen environment creation, packaging and non-pytest checks.
Recorder.inventory(journal_dir) -> {complete: bool, plugins: list[str]}.
P3's adapter must combine all process inventories and refuse missing inventories.
Until these real dependencies land, production_dependencies refuses every run.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

from agenttalk import dev_gate as gate
from agenttalk._atomic import write_text

TYPE = "agenttalk-windows-workers-preparation-v1"
PARALLEL = ["-p", "xdist.plugin", "-n", "2", "--dist", "loadgroup", "--max-worker-restart=0"]
PROBES = {
    "p8": {"arm": "serial", "args": ["-p", "experiment.audit_probe"]},
    "p8-gateway-repeat": {"arm": "parallel", "args": ["-p", "experiment.audit_probe"]},
    "p9": {"arm": "parallel", "args": ["-p", "experiment.death_probe"]},
    "p10": {"arm": "parallel", "args": ["-p", "experiment.collection_probe"]},
    "p11": {"arm": "serial", "args": ["-p", "experiment.native_trace"]},
}
MAX_JSON_BYTES = 2_000_000


def require(condition, message):
    if not condition:
        raise gate.GateBlock("experiment_blocked", message)


def read_plain(path, *, within=None):
    """Bounded nonblocking read; inspect the opened file, including its link count."""
    path = Path(path).resolve()
    if within is not None:
        require(path.is_relative_to(within.resolve()), "configuration escapes checkout")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "configuration must be a plain single-link file")
            raw = handle.read(MAX_JSON_BYTES + 1)
        require(len(raw) <= MAX_JSON_BYTES, "configuration exceeds size limit")
        return raw
    except OSError as exc:
        raise gate.GateBlock("experiment_blocked", f"configuration missing or unreadable: {path}") from exc


def read_json(path, *, within=None):
    try:
        raw = read_plain(path, within=within)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            require(depth <= 20, "configuration nesting exceeds limit")
            children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
            pending.extend((child, depth + 1) for child in children)
        require(isinstance(value, dict), "configuration must be a JSON object")
        return value
    except (OSError, ValueError, RecursionError) as exc:
        raise gate.GateBlock("experiment_blocked", f"configuration missing, unreadable or invalid: {path}") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate configuration key")
        result[key] = value
    return result


def contract_hash(contract):
    return gate.sha256_bytes(json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def validate_contract(contract):
    require(
        set(contract) == {"id", "version", "arms", "probes", "gateway_policy", "differences"}, "contract fields differ"
    )
    require(
        contract["id"] == TYPE and type(contract["version"]) is int and contract["version"] == 1,
        "contract identity differs",
    )
    require(contract["arms"] == {"serial": [], "parallel": PARALLEL}, "contract arms differ")
    require(
        contract["probes"] == PROBES and contract["gateway_policy"] == "error",
        "contract probe or gateway policy differs",
    )
    differences = contract["differences"]
    require(
        isinstance(differences, dict) and set(differences) == {"D1", "D2", "D3", "D4"}, "agreed differences missing"
    )
    require(
        all(isinstance(value, str) and value.strip() for value in differences.values()),
        "difference description missing",
    )


def installed_identity(checkout):
    """Tie a noneditable local install to the clean checkout and its source bytes."""
    try:
        checkout = Path(checkout).resolve()
        distribution = importlib.metadata.distribution("agenttalk")
        root = Path(distribution.locate_file("")).resolve()
        entries = {str(item).replace("\\", "/"): item for item in distribution.files or ()}
        record = next(item for name, item in entries.items() if name.endswith(".dist-info/RECORD"))
        direct = next(item for name, item in entries.items() if name.endswith(".dist-info/direct_url.json"))
        metadata = read_json(Path(distribution.locate_file(direct)), within=root)
        directory = metadata.get("dir_info")
        require(isinstance(directory, dict), "installed candidate dir_info must be an object")
        require(directory.get("editable", False) is False, "installed candidate must not be editable")
        if "vcs_info" in metadata:
            vcs = metadata["vcs_info"]
            require(isinstance(vcs, dict), "installed candidate vcs_info must be an object")
            require(isinstance(vcs.get("commit_id"), str), "installed candidate commit_id must be a string")
        require(metadata.get("url") == checkout.as_uri(), "installed candidate URL differs from checkout")
        binding = gate.capture_candidate_binding(checkout)
        require(binding.clean, "installed candidate checkout is dirty")
        record_path = Path(distribution.locate_file(record)).resolve()
        entry = Path(__file__).resolve()
        require(entry.is_relative_to(root), "entry point is not from the installed candidate")
        raw_record = read_plain(record_path, within=root)
        verified = set()
        for row in csv.reader(io.StringIO(raw_record.decode("utf-8"))):
            require(len(row) == 3, "installed RECORD row must have three fields")
            name, digest, size = row
            if not name.startswith("agenttalk/") or name.endswith(".pyc"):
                continue  # pip-generated metadata, scripts and bytecode are not checkout source
            relative = Path(name)
            require(not relative.is_absolute() and ".." not in relative.parts, "installed RECORD path escapes package")
            require(digest.startswith("sha256="), "installed package file has no SHA256 RECORD hash")
            installed = Path(distribution.locate_file(name)).resolve()
            actual = read_plain(installed, within=root)
            source = read_plain(checkout / "src" / relative, within=checkout)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(actual).digest()).decode().rstrip("=")
            require(digest == f"sha256={encoded}" and size == str(len(actual)), "installed RECORD hash or size differs")
            require(actual == source, "installed package file differs from checkout")
            verified.add(installed)
        require(entry in verified, "installed entry point has no verified RECORD hash")
        require(distribution.version == gate._committed_version(checkout), "installed candidate version differs")
        return {
            "commit": binding.candidate_sha,
            "version": distribution.version,
            "record_sha256": gate.sha256_bytes(raw_record),
        }
    except (importlib.metadata.PackageNotFoundError, StopIteration, OSError, ValueError, csv.Error) as exc:
        raise gate.GateBlock("experiment_blocked", "installed candidate identity unavailable") from exc


@dataclass
class Request:
    checkout: Path
    output: Path
    leg: str
    modes: tuple[str, ...]
    arm: str | None
    probe: str | None
    manifest: dict
    contract: dict
    frozen: dict
    identity: dict
    commit: str
    binding: object
    cases: tuple[str, ...] = ()


def validate_recorder(checkout):
    read_plain(checkout / "tests" / "experiment_record.py", within=checkout)
    read_plain(checkout / "tests" / "conftest.py", within=checkout)
    try:
        # Fixed pytest help argv in the validated checkout.
        help_result = subprocess.run(  # noqa: S603  # nosec B603
            [sys.executable, "-I", "-m", "pytest", "--help", "-p", "no:cacheprovider", "tests"],
            cwd=checkout,
            env=gate._base_env(checkout),
            text=True,
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise gate.GateBlock(
            "experiment_blocked", "tests/conftest.py --experiment-record preflight could not finish"
        ) from exc
    require(
        help_result.returncode == 0
        and re.search(r"^\s+--experiment-record(?:[=\s]|$)", help_result.stdout, re.MULTILINE) is not None,
        "tests/conftest.py must register --experiment-record in pytest --help with plugin autoload disabled",
    )


def prepare_request(*, checkout, record, output, leg, modes, arm=None, probe=None, cases=()):
    require("AGENTTALK_DEV_GATE_POSIX_PARALLEL" not in os.environ, "AGENTTALK_DEV_GATE_POSIX_PARALLEL must be absent")
    require(gate._platform_label() == "windows", "Windows experiment requires a Windows host")
    checkout = Path(checkout).resolve()
    manifest = gate.validate_manifest(read_json(checkout / "dev-gate.json", within=checkout))
    contract = read_json(checkout / "dev-gate-experiment.json", within=checkout)
    validate_contract(contract)
    frozen = read_json(record)
    require(frozen.get("contract_sha256") == contract_hash(contract), "frozen contract hash differs")
    require(re.fullmatch(r"windows/3\.(10|11|12|13)", leg) is not None, "invalid Windows leg")
    gate.parse_ci_leg(leg, manifest)
    require(modes and len(modes) == len(set(modes)) and set(modes) <= {"source", "wheel"}, "invalid modes")
    require((arm is None) != (probe is None), "choose an acceptance arm or a probe, never both")
    require(arm in {None, "serial", "parallel"} and probe in {None, *PROBES}, "unknown arm or probe")
    require(
        not cases or (probe == "p9" and len(cases) == len(set(cases)) and set(cases) <= {"worker", "controller"}),
        "cases must name worker or controller, for p9 only",
    )
    environments = frozen.get("environments")
    require(isinstance(environments, dict), "P7 environment plan missing")
    freeze = environments.get(leg.split("/")[1], {})
    require(isinstance(freeze, dict), "P7 environment plan invalid")
    require(re.fullmatch(r"[0-9a-f]{64}", str(freeze.get("freeze_sha256", ""))) is not None, "P7 freeze missing")
    binding = gate.capture_candidate_binding(checkout)
    require(
        binding.clean and frozen.get("commit") == binding.candidate_sha, "candidate is dirty or differs from freeze"
    )
    identity = installed_identity(checkout)
    require(identity.get("commit") == binding.candidate_sha, "installed candidate commit differs")
    output = Path(output).resolve()
    require(
        not output.exists() and not output.is_relative_to(checkout) and not checkout.is_relative_to(output),
        "output must be fresh and external",
    )
    require(
        not any((parent / ".agenttalk").is_dir() for parent in (output, *output.parents)),
        "output must be outside message stores",
    )
    validate_recorder(checkout)
    return Request(
        checkout,
        output,
        leg,
        tuple(modes),
        arm,
        probe,
        manifest,
        contract,
        frozen,
        identity,
        binding.candidate_sha,
        binding,
        tuple(cases),
    )


def partition(manifest, leg, mode):
    checks = gate.required_check_ids(manifest, execution_scope="ci-leg", ci_leg=leg)
    return [
        check
        for check in checks
        if check in {"git-binding", "final-binding"} or check.startswith("pytest-source-") == (mode == "source")
    ]


def pytest_call(request, mode, python, paths, env):
    for name, value in env.items():
        if name.casefold() in {"http_proxy", "https_proxy"}:
            require("@" not in value, "credentialed proxy settings cannot be recorded")
    spec = request.manifest["checks"]["pytest"]
    arm = request.arm if request.probe is None else request.contract["probes"][request.probe]["arm"]
    probe_args = [] if request.probe is None else request.contract["probes"][request.probe]["args"]
    if request.cases:
        probe_args = [*probe_args, "--cases", ",".join(request.cases)]
    argv = gate.isolated_tool_argv(
        python,
        "pytest",
        *spec["args"],
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(paths["bt"]),
        f"--experiment-record={paths['records']}",
        *request.contract["arms"][arm],
        *probe_args,
        *spec["paths"],
        candidate_import_root=paths["imports"] if mode == "source" else None,
    )
    return {
        "argv": argv,
        "env": dict(env),
        "cwd": str(paths["source"]),
        "timeout_seconds": spec["windows_timeout_seconds"],
        "checks": partition(request.manifest, request.leg, mode),
    }


def expected_call(request, mode, prepared, work):
    """Ordinary gate setup plus only the contract's declared instrumentation."""
    minor = request.leg.split("/")[1]
    base = gate._base_env(work)
    env = gate.source_environment(base, prepared.source_root) if mode == "source" else base
    spec = request.manifest["checks"]["pytest"]
    ordinary = gate.isolated_tool_argv(
        prepared.python,
        "pytest",
        *spec["args"],
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(work / f"pt-{'s' if mode == 'source' else 'w'}-{gate._python_suffix(minor)}"),
        *spec["paths"],
        candidate_import_root=prepared.import_root if mode == "source" else None,
    )
    arm = request.arm if request.probe is None else request.contract["probes"][request.probe]["arm"]
    extra = [f"--experiment-record={work / 'records'}", *request.contract["arms"][arm]]
    if request.probe is not None:
        extra += request.contract["probes"][request.probe]["args"]
    if request.cases:
        extra += ["--cases", ",".join(request.cases)]
    insert = len(ordinary) - len(spec["paths"])
    ordinary[insert:insert] = extra
    return {
        "argv": ordinary,
        "env": env,
        "cwd": str(prepared.source_root),
        "timeout_seconds": gate._pytest_timeout_seconds(spec),
        "checks": partition(request.manifest, request.leg, mode),
    }


def validate_call(actual, expected, *, actual_root=None, expected_root=None):
    def normalize(value, root):
        if isinstance(value, dict):
            return {key: normalize(child, root) for key, child in value.items()}
        if isinstance(value, list):
            return [normalize(child, root) for child in value]
        if isinstance(value, str) and root is not None:
            prefix = str(root)
            if value == prefix or value.startswith(prefix + os.sep) or value.startswith(prefix + "/"):
                return "<RUN>" + value[len(prefix) :]
        return value

    def comparable(call, root):
        call = normalize(call, root)
        env = call.get("env", {})
        require(len({key.casefold() for key in env}) == len(env), "ambiguous Windows environment keys")
        call["env"] = {key.casefold(): value for key, value in env.items()}
        return call

    require(comparable(actual, actual_root) == comparable(expected, expected_root), "undeclared call difference")


def validate_inventory(proof, *, acceptance):
    require(isinstance(proof, dict) and proof.get("complete") is True, "P3 inventory incomplete")
    plugins = proof.get("plugins")
    require(
        isinstance(plugins, list) and plugins and all(isinstance(p, str) for p in plugins),
        "P3 plugin inventory missing",
    )
    if acceptance:
        require(not any(p.casefold().startswith("experiment.") for p in plugins), "probe plugin in acceptance run")


def validate_install(proof, expected_hash, *, mode=None):
    require(
        isinstance(proof, dict) and proof.get("freeze_sha256") == expected_hash, "installed set differs from P7 freeze"
    )
    require(proof.get("probe_files") == [], "probe files present or installed-set check missing")
    require(proof.get("gateway_policy") == "error", "P1 held-port error policy missing")
    if mode == "wheel":
        require(
            re.fullmatch(r"[0-9a-f]{64}", str(proof.get("wheel_sha256", ""))) is not None,
            "tested wheel fingerprint missing",
        )


def artifact_header(request):
    return {
        "artifact_type": TYPE,
        "authoritative": False,
        "status": "blocked",
        "commit": request.commit,
        "contract_sha256": contract_hash(request.contract),
        "installed_candidate": request.identity,
        "freeze": request.frozen,
        "leg": request.leg,
        "modes": list(request.modes),
        "arm": request.arm,
        "probe": request.probe,
        "calls": [],
    }


@dataclass
class PreparedEnvironment:
    python: Path
    source_root: Path
    import_root: Path
    installed_set: dict
    setup_steps: list
    checks: list


def production_dependencies(request):
    """P7/P3 integration point. No fake, environment variable or CLI bypass exists."""
    raise gate.GateBlock("experiment_blocked", "real P7 installer and P3 recorder adapters are not available")


def execute(request, *, installer, recorder, runner=gate.run_command):
    """Run with explicit adapters. Only the production dependency factory feeds main."""
    request.output.mkdir(parents=True, exist_ok=False)
    artifact = artifact_header(request)
    try:
        for mode in request.modes:
            work = request.output / mode
            work.mkdir()
            phases = ("source",) if mode == "source" else ("source", "package", "wheel")
            exports = {phase: gate._export_phase(request.binding, work, phase) for phase in phases}
            prepared = installer.prepare(request, mode, work)
            paths = {
                "source": prepared.source_root,
                "imports": prepared.import_root,
                "bt": work / f"pt-{'s' if mode == 'source' else 'w'}-{gate._python_suffix(request.leg.split('/')[1])}",
                "logs": work / "logs",
                "records": work / "records",
            }
            failure = None
            try:
                freeze = request.frozen["environments"][request.leg.split("/")[1]]["freeze_sha256"]
                validate_install(prepared.installed_set, freeze, mode=mode)
                roles = ["host"] if mode == "source" else ["host", "build", "runtime", "wheel-test"]
                require(prepared.setup_steps == roles, "installer changed environment setup order")
                assigned = partition(request.manifest, request.leg, mode)
                supplied = [
                    c for c in assigned if c not in {"git-binding", "final-binding"} and not c.startswith("pytest-")
                ]
                require(
                    [check.get("id") for check in prepared.checks] == supplied
                    and all(check.get("status") == "pass" for check in prepared.checks),
                    "installer omitted, failed or reordered ordinary checks",
                )
                require(prepared.source_root.resolve() == exports[mode].resolve(), "installer changed committed export")
                require(prepared.python.is_absolute(), "installer interpreter must be absolute")
                if mode == "source":
                    require(
                        prepared.import_root.resolve() == (exports[mode] / "src").resolve(), "source imports differ"
                    )
                env = gate._base_env(work)
                if mode == "source":
                    env = gate.source_environment(env, prepared.source_root)
                call = pytest_call(request, mode, prepared.python, paths, env)
                expected = expected_call(request, mode, prepared, work)
                validate_call(call, expected, actual_root=work, expected_root=work)
                outcome = runner(
                    check_id=f"pytest-{mode}-py{request.leg.split('/')[1].replace('.', '')}",
                    argv=call["argv"],
                    cwd=prepared.source_root,
                    env=call["env"],
                    timeout_seconds=call["timeout_seconds"],
                    logs_dir=paths["logs"],
                    timeout_note=gate.PYTEST_DURATIONS_UNAVAILABLE,
                )
                call.update(
                    mode=mode,
                    status=outcome.status,
                    setup_steps=prepared.setup_steps,
                    installer_checks=prepared.checks,
                    installed_set=prepared.installed_set,
                    exports={phase: str(path) for phase, path in exports.items()},
                )
                artifact["calls"].append(call)
                require(outcome.status == "pass", "pytest workload did not pass")
                inventory = recorder.inventory(paths["records"])
                validate_inventory(inventory, acceptance=request.probe is None)
                call["inventory"] = inventory
                exports["static"] = gate._export_phase(request.binding, work, "static")
                call["exports"]["static"] = str(exports["static"])
            except Exception as exc:
                failure = exc
                raise
            finally:
                try:
                    bt = paths["bt"]
                    require(bt.resolve().is_relative_to(work.resolve()), "basetemp escaped run folder")
                    retained = bt.exists()
                    installer.finish(prepared)
                    require(bt.exists() == retained, "installer changed basetemp retention")
                    for kept in (bt, paths["logs"], paths["records"], *exports.values()):
                        require(kept.exists(), f"installer removed kept evidence: {kept}")
                    if artifact["calls"] and artifact["calls"][-1].get("mode") == mode:
                        artifact["calls"][-1]["cleanup"] = {
                            "basetemp_removed": False,
                            "basetemp_present": retained,
                            "kept": [bt.name, "logs", "records", "exports"],
                        }
                except Exception as exc:
                    artifact.setdefault("cleanup_errors", []).append(error_detail(exc))
                    if failure is None:
                        raise
        binding = gate.capture_candidate_binding(request.checkout)
        require(binding.clean and binding.candidate_sha == request.commit, "candidate changed during execution")
        artifact["status"] = "complete"
    except Exception as exc:
        detail = error_detail(exc)
        artifact["error"] = detail["message"]
        artifact["error_type"] = detail["type"]
    finally:
        write_text(request.output / "experiment.json", json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def error_detail(exc):
    return {"type": type(exc).__name__, "message": exc.detail if isinstance(exc, gate.GateBlock) else str(exc)}


def parse_cases(value):
    cases = tuple(value.split(","))
    if len(cases) != len(set(cases)) or not set(cases) <= {"worker", "controller"}:
        raise argparse.ArgumentTypeError("cases must be worker, controller or worker,controller")
    return cases


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "accept"):
        action = actions.add_parser(name)
        action.add_argument("--checkout", type=Path, required=True)
        action.add_argument("--leg", required=True)
        action.add_argument("--modes", required=True)
        action.add_argument("--record", type=Path, required=True)
        action.add_argument("--output", type=Path, required=True)
        action.add_argument(
            "--probe" if name == "run" else "--arm",
            required=True,
            choices=tuple(PROBES) if name == "run" else ("serial", "parallel"),
        )
        if name == "run":
            action.add_argument("--cases", type=parse_cases, default=())
    args = parser.parse_args(argv)
    if getattr(args, "cases", ()) and args.probe != "p9":
        parser.error("--cases is valid for p9 only")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        request = prepare_request(
            checkout=args.checkout,
            record=args.record,
            output=args.output,
            leg=args.leg,
            modes=tuple(args.modes.split(",")),
            arm=getattr(args, "arm", None),
            probe=getattr(args, "probe", None),
            cases=getattr(args, "cases", ()),
        )
        installer, recorder = production_dependencies(request)
    except gate.GateBlock as exc:
        print(f"experiment refused: {exc.detail}", file=sys.stderr)
        return 2
    try:
        result = execute(request, installer=installer, recorder=recorder)
        if result["status"] == "complete":
            return 0
        print(
            f"experiment failed: {result.get('error_type', 'GateBlock')}: {result.get('error', 'incomplete')}",
            file=sys.stderr,
        )
        for detail in result.get("cleanup_errors", []):
            print(f"cleanup failed: {detail['type']}: {detail['message']}", file=sys.stderr)
    except Exception as exc:
        print(f"experiment failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
