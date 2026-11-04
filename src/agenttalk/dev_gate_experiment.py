"""Non-voting Windows experiment contract; the ordinary gate is never modified.

Integration interfaces (P7/P3 supply implementations, not CLI options):
Installer.prepare(request, mode, workdir) -> PreparedEnvironment; finish(prepared).
The installer owns frozen environment creation, packaging and non-pytest checks.
Recorder.inventory(journal_dir) -> {complete: bool, plugins: list[str]}.
P3's adapter must combine all process inventories and refuse missing inventories.
P7 public interfaces:
read_lock(path, *, minor, role) -> normalized-name -> {version, sha256, filename}.
lock_install_argv(python, lock); candidate_install_argv(python, wheel, expected_sha256).
validate_lock_wheels(lock, wheel_root, *, minor, python_version, role, requirements) verifies downloaded
wheel hashes/metadata, Windows target tags and exact transitive closure offline.
pinned_build_call(python, output, env) -> (argv, offline_env).
environment_inventory(python, *, cwd, env) -> {python_version, pip_version,
site_packages, packages: [{name, version, record_sha256}], unowned_files, damaged_files}.
check_installed_set(lock, inventory, *, role, minor, candidate=None, observers=None,
acceptance=True, python_version=None) -> role proof: packages, records (per-distribution
RECORD SHA256), observer_hashes (site-relative path -> SHA256), python_version,
pip_version and lock_sha256. candidate is {name, version}; observers is path -> SHA256.
make_environment_plan(lock_root, *, minor, python_version, bootstrap_pip_version,
candidate_version, wheel_sha256, observers=None) -> frozen environments[minor] value:
schema_version, minor, python_version, bootstrap_pip_version, candidate_version,
wheel_sha256, roles (each lock's sha256 and packages), observers (role -> path -> SHA256),
freeze_sha256 (canonical hash of the preceding fields).
PinnedInstaller(creator: gate.InterpreterInfo, *, observer_sources=None) implements
prepare/finish. observer_sources maps role -> site-relative path -> local source file.
install_role(role, lock, plan, root, cwd, *, candidate=None, acceptance=True) creates,
pins and checks a fresh venv, returning (InterpreterInfo, venv proof, role proof).
The P6 installed_set aggregate contains freeze_sha256, gateway_policy, probe_files,
environments (role proofs), and wheel_sha256 in wheel mode. Each role proof also records
bootstrap_pip_version before pinning; wheel roles retain the independently checked wheel hash.
The host's bound source export is installed offline with its locked backend; runtime
and wheel-test install only the separately hash-checked candidate wheel.
Production dependencies still refuse until the P3 recorder adapter is connected.
validate_install(..., acceptance=True, observers=None) compares installed observer hashes
to the frozen role -> relative filename -> SHA256 declaration, restricted to mode's roles.
"""

from __future__ import annotations

import argparse
import base64
import csv
import email
import hashlib
import importlib.metadata
import io
import inspect
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
import zipfile
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


def _display(value):
    """Keep diagnostics on one line; repeated rendering must not double-escape paths."""
    return "".join(char if char.isprintable() else json.dumps(char, ensure_ascii=True)[1:-1] for char in str(value))


def require(condition, message):
    if not condition:
        raise gate.GateBlock("experiment_blocked", _display(message))


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
        raise gate.GateBlock("experiment_blocked", f"configuration missing or unreadable: {_display(path)}") from exc


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
        raise gate.GateBlock(
            "experiment_blocked", f"configuration missing, unreadable or invalid: {_display(path)}"
        ) from exc


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


def _observer_hashes(roles):
    """Validate and flatten role-qualified observer identities for one bounded diagnostic."""
    require(isinstance(roles, dict), "observer roles must be an object")
    result = {}
    for role, files in roles.items():
        require(role in LOCK_ROLES, f"unknown observer role: {role}")
        require(isinstance(files, dict), f"observer files must be an object: {role}")
        for name, digest in files.items():
            require(isinstance(name, str), f"observer filename must be a string: {role}")
            parts = name.replace("\\", "/").split("/")
            require(
                all(parts) and not any(part in {".", ".."} or ":" in part for part in parts),
                f"observer path escapes site-packages: {role}:{name}",
            )
            require(
                isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
                f"invalid observer SHA256: {role}:{name}: observed {digest}",
            )
            result[f"{role}:{name}"] = digest
    return result


def validate_install(proof, expected_hash, *, mode=None, acceptance=True, observers=None):
    require(
        isinstance(proof, dict) and proof.get("freeze_sha256") == expected_hash, "installed set differs from P7 freeze"
    )
    require(isinstance(proof.get("probe_files"), list), "installed-set file check missing")
    require(not acceptance or proof["probe_files"] == [], "probe files present in acceptance")
    declared = {} if observers is None else observers
    # Source runs create only the host; wheel runs create all four isolated environments.
    expected = _observer_hashes(declared)
    if mode == "source":
        expected = {name: digest for name, digest in expected.items() if name.startswith("host:")}
    require(not acceptance or not expected, "probe observers declared in acceptance: " + _bounded_details(expected))
    environments = proof.get("environments", {})
    require(isinstance(environments, dict), "installed observer environments must be an object")
    observed_roles = {}
    for role, environment in environments.items():
        require(isinstance(environment, dict), f"installed observer environment must be an object: {role}")
        observed_roles[role] = environment.get("observer_hashes", {})
    observed = _observer_hashes(observed_roles)
    require(
        observed == expected,
        "installed observer files differ from declaration: " + _mapping_differences(expected, observed),
    )
    files = proof["probe_files"]
    require(all(isinstance(name, str) for name in files), "installed observer filenames must be strings")
    expected_files = sorted({name.partition(":")[2] for name in expected})
    listing_difference = _mapping_differences(dict.fromkeys(expected_files, "listed"), dict.fromkeys(files, "listed"))
    require(
        files == expected_files,
        "installed observer file list differs from declaration: " + (listing_difference or _bounded_details(files)),
    )
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


LOCK_ROLES = ("host", "build", "runtime", "wheel-test")
_LOCK_LINE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*) "
    r"--hash=sha256:([0-9a-f]{64}) # wheel=([A-Za-z0-9_.+!-]+\.whl)"
)


def normalise_distribution(name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name), "invalid package name")
    return re.sub(r"[-_.]+", "-", name).lower()


def _bounded_details(values):
    values = list(values)
    summary = "; ".join(_display(value) for value in values[:20])
    return summary + (f"; {len(values) - 20} more" if len(values) > 20 else "")


def _mapping_differences(expected, observed):
    return _bounded_details(
        f"{_display(name)}: expected {_display(expected.get(name, '<missing>'))}, "
        f"observed {_display(observed.get(name, '<missing>'))}"
        for name in sorted(expected.keys() | observed.keys())
        if expected.get(name) != observed.get(name)
    )


def read_lock(path, *, minor, role):
    """Read strict complete pins with wheel provenance; an empty pip requirements file is not a lock."""
    require(role in LOCK_ROLES, "unknown environment role")
    packages = {}
    for number, line in enumerate(read_plain(path).decode("utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        require(match is not None, f"{path}:{number}: lock requires exact version, SHA256 and wheel filename")
        name, version, digest, filename = match.groups()
        name = normalise_distribution(name)
        fields = filename[:-4].split("-")
        require(len(fields) in {5, 6} and all(fields), f"{path}:{number}: lock artifact must be a wheel: {filename}")
        require(
            normalise_distribution(fields[0]) == name and fields[1] == version,
            f"{path}:{number}: wheel identity differs from pin {name}=={version}: {filename}",
        )
        require(name not in packages, f"{path}:{number}: duplicate normalized lock package: {name}")
        packages[name] = {"version": version, "sha256": digest, "filename": filename}
    bootstrap = {"pip", *({"setuptools"} if minor in {"3.10", "3.11"} else set())}
    require(
        bootstrap <= packages.keys(),
        f"{path}: lock missing bootstrap pins: {_bounded_details(sorted(bootstrap - packages.keys()))}",
    )
    roots = {
        "host": {"hatchling"},
        "build": {"hatchling", "build"},
        "runtime": set(),
        "wheel-test": {"pytest", "pytest-xdist", "pluggy", "execnet"},
    }
    require(
        roots[role] <= packages.keys(),
        f"{path}: lock missing {role} requirements: {_bounded_details(sorted(roots[role] - packages.keys()))}",
    )
    if role == "runtime":
        require(
            not ({"pytest", "pytest-xdist", "pluggy", "execnet", "hatchling", "build"} & packages.keys()),
            f"{path}: test or build package in runtime lock: "
            + _bounded_details(
                sorted({"pytest", "pytest-xdist", "pluggy", "execnet", "hatchling", "build"} & packages.keys())
            ),
        )
    return packages


def lock_install_argv(python, lock):
    return [
        str(python),
        "-I",
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "--force-reinstall",
        "--no-deps",
        "--no-cache-dir",
        "--only-binary=:all:",
        "-r",
        str(lock),
    ]


def validate_lock_wheels(lock, wheel_root, *, minor, python_version, role, requirements):
    """Offline closure/provenance check against downloaded wheels; pip supplies its existing requirement parser."""
    from pip._vendor.packaging.markers import Variable
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
    from pip._vendor.packaging.tags import compatible_tags, cpython_tags
    from pip._vendor.packaging.utils import parse_wheel_filename
    from pip._vendor.packaging.version import Version

    pins = read_lock(lock, minor=minor, role=role)
    root = Path(wheel_root).resolve()
    version = tuple(map(int, minor.split(".")))
    patch = Version(python_version)
    require(patch.release[:2] == version and len(patch.release) == 3, "Python patch does not match minor")
    implementation = "cp" + minor.replace(".", "")
    tags = set(cpython_tags(version, abis=[implementation], platforms=["win_amd64"]))
    tags.update(compatible_tags(version, interpreter=implementation, platforms=["win_amd64"]))
    metadata = {}
    for name, pin in pins.items():
        found = list(root.rglob(pin["filename"]))
        require(
            bool(found),
            f"{lock}: locked wheel missing: {pin['filename']}; expected {pin['sha256']}, observed <missing>",
        )
        wheel = found[0].resolve()
        require(wheel.is_relative_to(root), f"locked wheel path outside {root}: {wheel}")
        observed_hash = gate._sha256_file(wheel)
        require(
            observed_hash == pin["sha256"],
            f"locked wheel hash differs: {wheel}: expected {pin['sha256']}, observed {observed_hash}",
        )
        wheel_name, wheel_version, _, wheel_tags = parse_wheel_filename(wheel.name)
        require(
            normalise_distribution(wheel_name) == name
            and wheel_version == Version(pin["version"])
            and bool(tags & wheel_tags),
            "wheel identity or Windows target differs",
        )
        with zipfile.ZipFile(wheel) as archive:
            paths = [info for info in archive.infolist() if info.filename.endswith(".dist-info/METADATA")]
            require(len(paths) == 1 and paths[0].file_size <= MAX_JSON_BYTES, "wheel metadata invalid or oversized")
            message = email.message_from_bytes(archive.read(paths[0]))
        require(
            normalise_distribution(message["Name"]) == name and Version(message["Version"]) == wheel_version,
            "wheel metadata identity differs",
        )
        requires_python = message.get_all("Requires-Python", [])
        require(len(requires_python) <= 1, "wheel Requires-Python must have one value")
        if requires_python:
            try:
                compatible = patch in SpecifierSet(requires_python[0])
            except InvalidSpecifier as exc:
                raise gate.GateBlock("experiment_blocked", "wheel Requires-Python invalid") from exc
            require(
                compatible, f"wheel Requires-Python excludes frozen Python: {_display(name)} {_display(python_version)}"
            )
        metadata[name] = message.get_all("Requires-Dist", [])
    target = {
        "python_version": minor,
        "python_full_version": python_version,
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
        "platform_machine": "AMD64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "implementation_version": python_version,
        "platform_release": "",
        "platform_version": "",
    }
    pending, seen, reached = [(text, ()) for text in requirements], set(), set()
    while pending:
        text, parent_extras = pending.pop()
        require(len(seen) < 10000, "dependency closure exceeds limit")
        requirement = Requirement(text)
        require(requirement.url is None, "dependency URLs are not lock pins")
        # These target values are absent from the freeze; never substitute the validator host.
        marker_parts = [requirement.marker._markers] if requirement.marker else []
        while marker_parts:
            part = marker_parts.pop()
            if isinstance(part, (list, tuple)):
                marker_parts.extend(part)
            elif isinstance(part, Variable):
                require(
                    part.value not in {"platform_release", "platform_version"},
                    f"unfrozen platform marker: {part.value}",
                )
        if requirement.marker and not any(
            requirement.marker.evaluate(dict(target, extra=extra)) for extra in ("", *parent_extras)
        ):
            continue
        identity = (str(requirement), tuple(sorted(parent_extras)))
        if identity in seen:
            continue
        seen.add(identity)
        name = normalise_distribution(requirement.name)
        require(
            name in pins and Version(pins[name]["version"]) in requirement.specifier,
            "lock missing or drifting transitive dependency",
        )
        reached.add(name)
        pending.extend((dependency, tuple(requirement.extras)) for dependency in metadata[name])
    require(reached == pins.keys(), "lock has packages outside the declared role dependency closure")
    return pins


def candidate_install_argv(python, wheel, expected_sha256):
    wheel = Path(wheel).resolve()
    require(wheel.suffix == ".whl" and zipfile.is_zipfile(wheel), "candidate must be a wheel")
    observed_hash = gate._sha256_file(wheel)
    require(
        re.fullmatch(r"[0-9a-f]{64}", expected_sha256) and observed_hash == expected_sha256,
        f"candidate wheel hash differs from build: {wheel}: expected {expected_sha256}, observed {observed_hash}",
    )
    return [str(python), "-I", "-m", "pip", "install", "--no-deps", "--no-index", "--no-cache-dir", str(wheel)]


def pinned_build_call(python, output, env):
    return (
        [str(python), "-I", "-m", "build", "--no-isolation", "--sdist", "--wheel", "--outdir", str(output)],
        dict(env, PIP_NO_INDEX="1"),
    )


_ENVIRONMENT_PROBE = (
    "import base64, csv, hashlib, importlib.metadata as md, io, json, os, pathlib, platform, sys, sysconfig\n"
    + inspect.getsource(_display)
    + r"""
sites = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
prefix = pathlib.Path(sys.prefix).resolve()
packages, owned, damaged = [], set(), []
for dist in md.distributions(path=sites):
    record_text = dist.read_text("RECORD")
    rows = list(csv.reader(io.StringIO(record_text))) if record_text is not None else []
    # Vendored RECORDs are owned files, not this distribution's top-level identity.
    record_paths = [pathlib.Path(dist.locate_file(row[0])) for row in rows
                    if len(row) == 3 and row[0].endswith(".dist-info/RECORD") and row[0].count("/") == 1]
    record = (record_paths[0].read_bytes() if len(record_paths) == 1
              and record_paths[0].resolve().is_relative_to(prefix) and record_paths[0].is_file() else None)
    packages.append({"name": dist.metadata["Name"], "version": dist.version,
                     "record_sha256": hashlib.sha256(record).hexdigest() if record is not None else None})
    for row in rows:
        if len(row) != 3:
            damaged.append(_display(f"{dist.metadata['Name']}/RECORD: expected 3 fields, observed {len(row)}"))
            continue
        entry, digest, _ = row
        entry_path = pathlib.Path(os.path.abspath(dist.locate_file(entry)))
        path = entry_path.resolve()
        expected = digest or "present"
        if not path.is_relative_to(prefix):
            damaged.append(_display(f"{path}: expected inside {prefix}, observed outside environment"))
            continue
        for site in sites:
            if path.is_relative_to(pathlib.Path(site).resolve()):
                owned.add(os.path.normcase(str(entry_path)))
        observed = "<missing>"
        if path.is_file():
            observed = ("sha256=" + base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest())
                        .decode().rstrip("=")) if digest else "present"
        if expected != observed:
            damaged.append(_display(f"{path}: expected {expected}, observed {observed}"))
unowned = {}
for index, site in enumerate(sites):
    for path in pathlib.Path(site).rglob("*"):
        entry_key = os.path.normcase(os.path.abspath(path))
        if (path.is_symlink() or getattr(path.lstat(), "st_reparse_tag", 0)) and entry_key not in owned:
            damaged.append(_display(f"{path}: expected recorded directory entry, observed unrecorded link"))
            continue
        if not path.resolve().is_relative_to(pathlib.Path(site).resolve()):
            damaged.append(_display(f"{path}: expected inside site-packages, observed outside"))
            continue
        if path.is_file() and entry_key not in owned:
            name = path.relative_to(site).as_posix()
            if len(sites) > 1:
                name = str(index) + "/" + name
            unowned[name] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({"python_version": platform.python_version(), "pip_version": md.version("pip"),
                  "site_packages": sites, "packages": packages, "unowned_files": unowned, "damaged_files": damaged}))
"""
)


def environment_inventory(python, *, cwd, env):
    """Inspect the target interpreter, including unowned files invisible to distribution metadata."""
    # Fixed argument list, interpreter under test, and no shell.
    result = subprocess.run(  # noqa: S603  # nosec B603
        [str(python), "-I", "-c", _ENVIRONMENT_PROBE],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    require(result.returncode == 0, "environment inventory failed")
    require(len(result.stdout) <= MAX_JSON_BYTES, "environment inventory exceeds limit")
    try:
        return json.loads(result.stdout, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise gate.GateBlock("experiment_blocked", "environment inventory invalid") from exc


def check_installed_set(
    lock, inventory, *, role, minor, candidate=None, observers=None, acceptance=True, python_version=None
):
    """Validate versions and RECORD identities separately from observer file identities."""
    pins = read_lock(lock, minor=minor, role=role)
    expected = {name: row["version"] for name, row in pins.items()}
    if candidate is not None:
        name = normalise_distribution(candidate["name"])
        require(name not in expected, "candidate must be separate from index lock")
        expected[name] = candidate["version"]
    observed, records = {}, {}
    for package in inventory["packages"]:
        name = normalise_distribution(package["name"])
        require(
            name not in observed,
            f"duplicate installed package: {_display(name)}, "
            f"expected one version {_display(expected.get(name, '<missing>'))}, "
            f"observed {_display(package['version'])} again",
        )
        observed[name] = package["version"]
        digest = package.get("record_sha256")
        require(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            f"distribution RECORD missing: {name}, expected SHA256, observed {digest}",
        )
        records[name] = digest
    require(
        observed == expected,
        f"{lock}: installed package set or transitive version differs from lock: "
        + _mapping_differences(expected, observed),
    )
    require(
        not inventory["damaged_files"],
        "installed file differs from RECORD: " + _bounded_details(inventory["damaged_files"]),
    )
    require(
        inventory["pip_version"] == expected["pip"],
        f"pip: expected {expected['pip']}, observed {inventory['pip_version']}",
    )
    require(inventory["python_version"].startswith(minor + "."), "interpreter minor differs")
    if python_version is not None:
        require(inventory["python_version"] == python_version, "interpreter patch differs from freeze")
    observers = {} if observers is None else dict(observers)
    for path, digest in observers.items():
        parts = path.replace("\\", "/").split("/")
        require(
            all(parts) and not any(p in {".", ".."} or ":" in p for p in parts), "observer path escapes site-packages"
        )
        require(re.fullmatch(r"[0-9a-f]{64}", digest), "observer hash invalid")
    require(not acceptance or not observers, "probe files in acceptance plan")
    require(
        inventory["unowned_files"] == observers,
        "added site-packages file differs from probe plan: "
        + _mapping_differences(observers, inventory["unowned_files"]),
    )
    return {
        "role": role,
        "packages": observed,
        "records": records,
        "observer_hashes": observers,
        "python_version": inventory["python_version"],
        "pip_version": inventory["pip_version"],
        "lock_sha256": hashlib.sha256(read_plain(lock)).hexdigest(),
    }


def make_environment_plan(
    lock_root, *, minor, python_version, bootstrap_pip_version, candidate_version, wheel_sha256, observers=None
):
    """Make the value frozen under environments[minor]; hash every role, not a combined loose set."""
    require(python_version.startswith(minor + "."), "Python patch does not match minor")
    require(re.fullmatch(r"[0-9a-f]{64}", wheel_sha256), "wheel hash missing")
    roles = {}
    for role in LOCK_ROLES:
        path = Path(lock_root) / f"{role}.txt"
        roles[role] = {
            "sha256": hashlib.sha256(read_plain(path)).hexdigest(),
            "packages": read_lock(path, minor=minor, role=role),
        }
    plan = {
        "schema_version": 1,
        "minor": minor,
        "python_version": python_version,
        "bootstrap_pip_version": bootstrap_pip_version,
        "candidate_version": candidate_version,
        "wheel_sha256": wheel_sha256,
        "roles": roles,
        "observers": observers or {},
    }
    return dict(plan, freeze_sha256=contract_hash(plan))


class PinnedInstaller:
    """P6 installer implementation; external recorder integration remains a separate dependency."""

    def __init__(self, creator, *, observer_sources=None):
        self.creator = creator
        self.observer_sources = observer_sources or {}

    def validate_plan(self, locks, plan):
        require(plan["python_version"] == self.creator.version, "Python patch differs from frozen creator")
        expected = make_environment_plan(
            locks,
            minor=self.creator.requested,
            python_version=plan["python_version"],
            bootstrap_pip_version=plan["bootstrap_pip_version"],
            candidate_version=plan["candidate_version"],
            wheel_sha256=plan["wheel_sha256"],
            observers=plan["observers"],
        )
        require(plan == expected, "environment plan or role lock differs from freeze")
        requirements = Path(locks).parents[1] / "dev-gate-requirements.txt"
        names = set()
        for line in read_plain(requirements).decode("utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                require(match is not None, "gate requirement invalid")
                names.add(normalise_distribution(match[1]))
        require(names <= plan["roles"]["host"]["packages"].keys(), "host lock omits a gate requirement")

    def run(self, argv, *, cwd, env, logs, label):
        result = gate.run_command(check_id=label, argv=argv, cwd=cwd, env=env, timeout_seconds=900, logs_dir=logs)
        require(result.status == "pass", f"pinned environment command failed: {label}")
        return result

    def install_role(self, role, lock, plan, root, cwd, *, candidate=None, acceptance=True):
        minor = self.creator.requested
        read_lock(lock, minor=minor, role=role)
        logs = root.parent / "logs" / role
        interpreter, venv = gate._create_isolated_venv(
            creator=self.creator, root=root, role="test" if role == "wheel-test" else "runtime", logs_dir=logs
        )
        env = gate._base_env(root.parent)
        bootstrap = environment_inventory(interpreter.path, cwd=cwd, env=env)
        require(bootstrap["python_version"] == plan["python_version"], "venv patch differs")
        require(bootstrap["pip_version"] == plan["bootstrap_pip_version"], "ensurepip bootstrap differs")
        self.run(lock_install_argv(interpreter.path, lock), cwd=cwd, env=env, logs=logs, label=f"lock-{role}")
        identity = None
        if candidate is not None:
            if candidate.get("source"):
                # The committed source export is supplied only by prepare, after P6's binding check.
                require(
                    role == "host" and Path(candidate["path"]).resolve() == Path(cwd).resolve(),
                    "source candidate is only the bound host export",
                )
                argv = [
                    str(interpreter.path),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-index",
                    "--no-cache-dir",
                    "--no-build-isolation",
                    str(candidate["path"]),
                ]
            else:
                argv = candidate_install_argv(interpreter.path, candidate["path"], plan["wheel_sha256"])
            self.run(argv, cwd=cwd, env=dict(env, PIP_NO_INDEX="1"), logs=logs, label=f"candidate-{role}")
            identity = {"name": "agenttalk", "version": plan["candidate_version"]}
        self.run(
            [str(interpreter.path), "-I", "-m", "pip", "check"],
            cwd=cwd,
            env=env,
            logs=logs,
            label=f"dependencies-{role}",
        )
        allowed = plan["observers"].get(role, {})
        require(not acceptance or not allowed, "observers forbidden in acceptance environment")
        observed = environment_inventory(interpreter.path, cwd=cwd, env=env)
        sources = self.observer_sources.get(role, {})
        require(set(sources) == set(allowed), "observer source set differs from freeze")
        if sources:
            site = Path(observed["site_packages"][0]).resolve()
            for name, source in sources.items():
                target = (site / name).resolve()
                require(target.is_relative_to(site) and target != site, "observer escapes site-packages")
                data = read_plain(source)
                observed_hash = hashlib.sha256(data).hexdigest()
                require(
                    observed_hash == allowed[name],
                    f"observer source hash differs: {source}: expected {allowed[name]}, observed {observed_hash}",
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(data)
            observed = environment_inventory(interpreter.path, cwd=cwd, env=env)
        proof = check_installed_set(
            lock,
            observed,
            role=role,
            minor=minor,
            candidate=identity,
            observers=allowed,
            acceptance=acceptance,
            python_version=plan["python_version"],
        )
        proof["bootstrap_pip_version"] = bootstrap["pip_version"]
        proof["install_argv"] = lock_install_argv(interpreter.path, lock)
        if candidate is not None:
            proof["candidate_install_argv"] = argv
        if candidate is not None and not candidate.get("source"):
            proof["wheel_sha256"] = plan["wheel_sha256"]
        return interpreter, venv, proof

    def package(self, request, work, interpreter):
        _, env = pinned_build_call(interpreter.path, work / "dist", gate._base_env(work))
        record, _, wheel = gate.run_package_build(
            interpreter=interpreter,
            package_root=work / "candidate-package",
            out_dir=work / "dist",
            env=env,
            manifest=request.manifest,
            logs_dir=work / "logs",
        )
        require(record["status"] == "pass" and wheel is not None, "offline package build failed")
        plan = request.frozen["environments"][self.creator.requested]
        candidate_install_argv(interpreter.path, wheel, plan["wheel_sha256"])
        return wheel, record

    def non_pytest_checks(self, request, work, host, runtime, runtime_proof, build_record):
        records = {"package-build": build_record}
        source = work / "candidate-wheel"
        env = gate._base_env(work)
        suffix = gate._python_suffix(self.creator.requested)
        install = {
            "id": f"wheel-install-{suffix}",
            "status": "pass",
            "kind": "wheel-install",
            "wheel_sha256": request.frozen["environments"][self.creator.requested]["wheel_sha256"],
        }
        records[install["id"]] = install
        records[f"wheel-dependency-check-{suffix}"] = gate._wheel_dependency_check(
            interpreter=runtime,
            runtime_environment=runtime_proof,
            source_root=source,
            env=env,
            install_record=install,
            timeout_seconds=900,
            logs_dir=work / "logs",
        )
        records[f"wheel-contract-{suffix}"] = gate._wheel_contract(
            interpreter=runtime,
            runtime_environment=runtime_proof,
            source_root=source,
            env=env,
            expected_version=request.frozen["environments"][self.creator.requested]["candidate_version"],
            manifest=request.manifest,
            install_record=install,
            logs_dir=work / "logs",
        )
        assigned = [
            c
            for c in partition(request.manifest, request.leg, "wheel")
            if c not in {"git-binding", "final-binding"} and not c.startswith("pytest-")
        ]
        static = set(assigned) & {"ruff", "bandit", "gitleaks", "pip-audit", "semgrep", "zizmor"}
        if static:
            exported = gate._export_phase(request.binding, work, "static")
            snapshot = gate._runtime_dependency_snapshot(
                interpreter=runtime,
                source_root=source,
                env=env,
                output=work / "audit-requirements.txt",
                timeout_seconds=900,
                logs_dir=work / "logs",
            )
            extra, _, _ = gate.run_static_checks(
                interpreter=host,
                source_root=exported,
                candidate_root=request.checkout,
                env=env,
                manifest=request.manifest,
                required_ids=static,
                audit_requirements=snapshot,
                logs_dir=work / "logs",
            )
            records.update(extra)
        require(
            all(name in records and records[name]["status"] == "pass" for name in assigned),
            "ordinary non-pytest check failed or missing",
        )
        return [records[name] for name in assigned]

    def prepare(self, request, mode, workdir):
        work = Path(workdir)
        minor = request.leg.split("/")[1]
        require(minor == self.creator.requested and mode in {"source", "wheel"}, "installer leg differs")
        plan = request.frozen["environments"][minor]
        locks = request.checkout / "locks" / minor
        require(locks.resolve().is_relative_to(request.checkout.resolve()), "locks escape checkout")
        self.validate_plan(locks, plan)
        acceptance = request.probe is None
        environments = {}

        def install(role, candidate=None):
            result = self.install_role(
                role,
                locks / f"{role}.txt",
                plan,
                work / f"env-{role}",
                work / f"candidate-{mode}",
                candidate=candidate,
                acceptance=acceptance,
            )
            environments[role] = result[2]
            return result

        source = work / f"candidate-{mode}"
        host, _, _ = install("host", {"path": source, "source": True})
        python, import_root, checks = host.path, source / "src", []
        if mode == "wheel":
            build, _, _ = install("build")
            wheel, build_record = self.package(request, work, build)
            runtime, runtime_proof, _ = install("runtime", {"path": wheel})
            test, _, _ = install("wheel-test", {"path": wheel})
            python = test.path
            import_root = work / "env-wheel-test"
            checks = self.non_pytest_checks(request, work, host, runtime, runtime_proof, build_record)
        probe_files = sorted({name for row in environments.values() for name in row.get("observer_hashes", {})})
        proof = {
            "freeze_sha256": plan["freeze_sha256"],
            "gateway_policy": request.contract["gateway_policy"],
            "probe_files": probe_files,
            "environments": environments,
        }
        if mode == "wheel":
            proof["wheel_sha256"] = plan["wheel_sha256"]
        return PreparedEnvironment(python, source, import_root, proof, list(environments), checks)

    def finish(self, prepared):
        # Retain isolated venvs and immutable evidence; no basetemp or export is removed.
        require(prepared.python.is_absolute(), "prepared interpreter must be absolute")


def production_dependencies(request):
    """P7/P3 integration point. No fake, environment variable or CLI bypass exists."""
    raise gate.GateBlock("experiment_blocked", "P7 pinned installer awaits the real P3 recorder adapter")


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
                validate_install(
                    prepared.installed_set,
                    freeze,
                    mode=mode,
                    acceptance=request.probe is None,
                    observers=request.frozen["environments"][request.leg.split("/")[1]].get("observers", {}),
                )
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
    return {
        "type": type(exc).__name__,
        "message": _display(exc.detail if isinstance(exc, gate.GateBlock) else str(exc)),
    }


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
        print(f"experiment refused: {_display(exc.detail)}", file=sys.stderr)
        return 2
    try:
        result = execute(request, installer=installer, recorder=recorder)
        if result["status"] == "complete":
            return 0
        print(
            f"experiment failed: {_display(result.get('error_type', 'GateBlock'))}: "
            f"{_display(result.get('error', 'incomplete'))}",
            file=sys.stderr,
        )
        for detail in result.get("cleanup_errors", []):
            print(f"cleanup failed: {_display(detail['type'])}: {_display(detail['message'])}", file=sys.stderr)
    except Exception as exc:
        print(f"experiment failed: {type(exc).__name__}: {_display(exc)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
