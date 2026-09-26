"""Closed increment-2 records and bounded staged inputs, without execution.

M1 only: these readers do not decide preflight success or enable schema-4 GO.
Distribution pins are never copied by the declarative-input reader (D1).
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import unicodedata

from agenttalk import acceptance as A

MAX_ENTRIES = 64
MAX_FILES = 256
MAX_EDGES = 1024
MAX_DEPTH = 24
MAX_NODES = 8192
MAX_ARGV = 64
MAX_PATH = 512
MAX_MARKER = 256
MAX_INPUT_BYTES = A.MAX_BYTES
MAX_TOTAL_BYTES = A.MAX_TOTAL_BYTES
ROLES = ("distribution", "manifest", "lockfile", "config", "proof-log", "banner",
         "provenance", "verification", "snapshot", "adapter")
PLACEHOLDERS = ("{artifact}", "{checkout}", "{scratch}", "{cache_overlay}")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³]) *?(?:\..*)?\Z", re.I)


def _list(value, label, limit=MAX_FILES):
    if not isinstance(value, list) or len(value) > limit:
        A._fail(f"{label}: expected bounded list")
    return value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        A._fail(f"{label}: integer out of bounds")


def _boolean(value, label):
    if type(value) is not bool:
        A._fail(f"{label}: expected boolean")


def _text(value, label):
    A._text(value, label)
    if any(ord(c) < 32 for c in value):
        A._fail(f"{label}: control character")
    try:
        value.encode("utf-8")
    except UnicodeError:
        A._fail(f"{label}: invalid Unicode")
    return value


def _index(values, label, limit):
    _list(values, label, limit)
    return A._indexed(values, label)


def _refs(values, label):
    _list(values, label)
    for index, value in enumerate(values):
        A._id(value, f"{label}[{index}]")
    if len(set(values)) != len(values):
        A._fail(f"{label}: duplicate reference")
    return values


def utc(value):
    """Canonical second-resolution UTC only; no implicit local time or grace."""
    _text(value, "UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        A._fail("expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ")
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        A._fail("expected canonical UTC timestamp")
    return parsed


def decode(data):
    """Reuse strict JSON decoding; bound nesting before allocating its tree."""
    if not isinstance(data, bytes) or len(data) > MAX_INPUT_BYTES:
        A._fail("registry input exceeds byte limit or is not bytes")
    depth = 0
    nodes = 0
    atom = False
    quoted = escaped = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
            atom = False
            nodes += 1
        elif byte in (91, 123):
            atom = False
            nodes += 1
            depth += 1
            if depth > MAX_DEPTH:
                A._fail("registry JSON nesting exceeds limit")
        elif byte in (93, 125):
            atom = False
            depth -= 1
        elif byte in b" \t\r\n,:":
            atom = False
        elif not atom:
            atom = True
            nodes += 1
        if nodes > MAX_NODES:
            A._fail("registry JSON node count exceeds limit")
    value = A.decode(data)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            # Empty strings are rejected by field validators where required.
            try:
                item.encode("utf-8")
            except UnicodeError:
                A._fail("invalid Unicode in registry JSON")
    return value


def relative_path(value):
    _text(value, "cache-relative path")
    if len(value) > MAX_PATH:
        A._fail("cache-relative path exceeds limit")
    parts = value.split("/")
    if (any(c in value for c in '\\:*?"<>|')
            or any(not p or p in (".", "..") or p.endswith((".", " ")) or _DEVICE.fullmatch(p)
                   for p in parts)):
        A._fail("cache-relative path must stay beneath its declared root")
    return value


def staged_path(root, relative):
    """Resolve a registry file beneath its operator-approved staging root."""
    relative_path(relative)
    try:
        root = Path(root).absolute()
        for parent in (*reversed(root.parents), root):
            if parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 1024:
                A._fail("staging root contains a link/reparse point")
        return A._path(root, relative)
    except OSError:
        # Never include an OS exception's private locator in a public hold.
        pass
    raise A.AcceptanceError("acceptance_preflight_unavailable", "staged input is unavailable") from None


def file_pin(pin):
    keys = "id role path sha256 size expires_at version provenance"
    if isinstance(pin, dict) and pin.get("role") == "snapshot":
        keys += " distribution"
    A._object(pin, keys, "staged file pin")
    A._id(pin["id"], "file pin.id")
    if pin["role"] not in ROLES:
        A._fail("unsupported staged file role")
    relative_path(pin["path"])
    A._digest(pin["sha256"])
    _integer(pin["size"], "staged file size", 0, 2**63 - 1)
    if pin["role"] != "distribution" and pin["size"] > MAX_INPUT_BYTES:
        A._fail("declarative input exceeds byte limit", "acceptance_record_missing")
    if pin["role"] in ("distribution", "snapshot"):
        _text(pin["version"], "exact file version")
    elif pin["version"] is not None or pin["provenance"] is not None:
        A._fail("only distributions/snapshots declare version and provenance")
    if pin["role"] == "snapshot":
        utc(pin["expires_at"])
        A._object(pin["distribution"], "id sha256", "snapshot distribution pin")
        A._id(pin["distribution"]["id"], "snapshot.distribution.id")
        A._digest(pin["distribution"]["sha256"])
    elif pin["expires_at"] is not None:
        A._fail("only snapshots declare expires_at")
    return pin


def _file_ref(files, ref, roles):
    A._id(ref, "file reference")
    if ref not in files or files[ref]["role"] not in roles:
        A._fail("unresolved file reference or wrong role")
    return ref


def _provenance(value, files):
    A._object(value, "source retrieved_at checksum_source independent_verification record verification", "provenance")
    for key in ("source", "checksum_source"):
        _text(value[key], key)
    utc(value["retrieved_at"])
    _boolean(value["independent_verification"], "independent_verification")
    used = {_file_ref(files, value["record"], ("provenance",))}
    if value["independent_verification"]:
        used.add(_file_ref(files, value["verification"], ("verification",)))
    elif value["verification"] is not None:
        A._fail("verification reference requires independent_verification")
    return used


def _template(value, files):
    A._object(value, "argv cwd inputs outputs", "command template")
    argv = _list(value["argv"], "argv", MAX_ARGV)
    if not argv or argv[0] != "{artifact}" or value["cwd"] != "{checkout}":
        A._fail("template requires pinned artifact and checkout placeholders")
    for arg in argv:
        _text(arg, "argv token")
        if arg in PLACEHOLDERS:
            continue
        prefix = next((token + "/" for token in PLACEHOLDERS[1:] if arg.startswith(token + "/")), None)
        if prefix is not None:
            relative_path(arg[len(prefix):])
        elif any(c in arg for c in "{}/\\:") or arg in (".", ".."):
            A._fail("unknown placeholder or unbounded argv path")
    inputs = _refs(value["inputs"], "template inputs")
    for ref in inputs:
        _file_ref(files, ref, ROLES)
    for output in _list(value["outputs"], "template outputs", MAX_ARGV):
        _text(output, "output")
        if not output.startswith("{scratch}/"):
            A._fail("template outputs require scratch-relative paths")
        relative_path(output[len("{scratch}/"):])
    if len(set(value["outputs"])) != len(value["outputs"]):
        A._fail("duplicate template output")
    return set(inputs)


def _offline_policy(value):
    A._object(value, "mode positive_control cache_hit real_fetch", "offline policy")
    if value["mode"] not in ("external-denial", "offline-recipe"):
        A._fail("unsupported offline mode")
    markers = [value["positive_control"], value["real_fetch"]]
    if value["mode"] == "offline-recipe":
        markers.append(value["cache_hit"])
    elif value["cache_hit"] is not None:
        A._fail("external denial does not declare a cache-hit marker")
    for marker in markers:
        if len(_text(marker, "literal proof marker")) > MAX_MARKER:
            A._fail("proof marker exceeds limit")
    if len(set(markers)) != len(markers):
        A._fail("proof markers must be distinct")


def _entry(entry, files):
    A._object(entry, "id kind version artifact dependencies inputs snapshots provenance command offline "
              "failure_policy measurement expected_banner", "registry entry")
    A._id(entry["id"], "entry.id")
    if entry["kind"] not in ("checker", "toolchain", "service"):
        A._fail("unsupported registry kind")
    if entry["kind"] in ("toolchain", "service"):
        _text(entry["expected_banner"], "expected banner")
    elif entry["expected_banner"] is not None:
        A._fail("only toolchain/service entries declare expected_banner")
    # Exact, opaque versions: no discovery, semver matching or normalization.
    _text(entry["version"], "exact version")
    used = {_file_ref(files, entry["artifact"], ("distribution",))}
    artifact = files[entry["artifact"]]
    if artifact["version"] != entry["version"] or artifact["provenance"] != entry["provenance"]:
        A._fail("entry version/provenance differs from its artifact pin")
    _refs(entry["dependencies"], "entry dependencies")
    for ref in _refs(entry["inputs"], "entry inputs"):
        used.add(_file_ref(files, ref, ROLES))
    for ref in _refs(entry["snapshots"], "snapshots"):
        used.add(_file_ref(files, ref, ("snapshot",)))
    used.update(_provenance(entry["provenance"], files))
    used.update(_template(entry["command"], files))
    _offline_policy(entry["offline"])
    A._object(entry["failure_policy"], "unavailable mismatch expired absent_proof attempted_fetch", "failure policy")
    if entry["failure_policy"] != {"unavailable": "not-run", "mismatch": "fail", "expired": "not-run",
                                   "absent_proof": "not-run", "attempted_fetch": "fail"}:
        A._fail("unsupported failure-policy mapping")
    measurement = entry["measurement"]
    if entry["kind"] == "checker":
        A._object(measurement, "comparator parser config normalizer", "measurement pins")
        for key, roles in (("comparator", ("adapter",)), ("parser", ("adapter",)),
                           ("config", ("config",)), ("normalizer", ("adapter",))):
            used.add(_file_ref(files, measurement[key], roles))
    elif measurement is not None:
        A._fail("measurement pins are conditional on checker kind")
    return used


def validate_registry(value):
    """Validate registry v2 only; legacy dispatch stays in acceptance.py for M1."""
    A._object(value, "schema_version entries files", "registry")
    A._version(value["schema_version"], (2,))
    entries = _index(value["entries"], "registry entries", MAX_ENTRIES)
    files = _index(value["files"], "registry files", MAX_FILES)
    paths = set()
    used = set()
    total = 0
    for pin in files.values():
        file_pin(pin)
        key = unicodedata.normalize("NFC", pin["path"]).casefold()
        if any(key == p or key.startswith(p + "/") or p.startswith(key + "/") for p in paths):
            A._fail("duplicate or conflicting portable staged path")
        paths.add(key)
        if pin["role"] != "distribution":
            total += pin["size"]
    if total > MAX_TOTAL_BYTES:
        A._fail("declarative inputs exceed aggregate limit", "acceptance_record_missing")
    # Validate all target shapes before dereferencing provenance records.
    for pin in files.values():
        if pin["role"] in ("distribution", "snapshot"):
            used.update(_provenance(pin["provenance"], files))
        if pin["role"] == "snapshot":
            ref = _file_ref(files, pin["distribution"]["id"], ("distribution",))
            if pin["distribution"]["sha256"] != files[ref]["sha256"]:
                A._fail("snapshot distribution digest differs from pin")
            used.add(ref)
    edges = 0
    for entry in entries.values():
        used.update(_entry(entry, files))
        edges += len(entry["dependencies"])
        if edges > MAX_EDGES:
            A._fail("dependency edge limit exceeded")
        if not set(entry["dependencies"]) <= entries.keys():
            A._fail("unresolved entry dependency")
    if used != files.keys():
        A._fail("unused staged file pin")
    depths = {}

    def visit(key, chain):
        if key in chain or len(chain) >= MAX_DEPTH:
            A._fail("dependency cycle or depth limit")
        if key not in depths:
            depths[key] = 1 + max((visit(dep, chain + (key,)) for dep in entries[key]["dependencies"]), default=0)
        if depths[key] + len(chain) > MAX_DEPTH:
            A._fail("dependency depth limit")
        return depths[key]

    for key in entries:
        visit(key, ())
    return value


def validate_environment(value, *, overrides=True):
    A._object(value, "schema_version runtime compiler package_manager services os locale timezone "
              "environment_digest config_digest scratch cache_overlay service_data time_limit_seconds "
              "memory_limit_bytes row_overrides", "environment")
    A._version(value["schema_version"])
    for key in ("runtime", "compiler", "package_manager", "services"):
        _list(value[key], key, MAX_ENTRIES)
        _refs(value[key], key)
    for key in ("os", "locale", "timezone"):
        _text(value[key], key)
    for key in ("environment_digest", "config_digest"):
        A._digest(value[key])
    if (value["scratch"] != "isolated" or value["cache_overlay"] != "fresh-writable"
            or value["service_data"] != "fresh"):
        A._fail("unsupported scratch/cache/service-data policy")
    _integer(value["time_limit_seconds"], "time limit", 1, 86400)
    _integer(value["memory_limit_bytes"], "memory limit", 1, 2**40)
    rows = _index(value["row_overrides"], "row overrides", MAX_FILES)
    if rows and not overrides:
        A._fail("nested environment overrides are unsupported")
    for row in rows.values():
        A._object(row, "id environment", "row environment override")
        # The digest refers to exact external environment bytes, not this object.
        evidence_ref(row["environment"])
    if sum(row["environment"]["size"] for row in rows.values()) > MAX_TOTAL_BYTES:
        A._fail("environment overrides exceed aggregate limit", "acceptance_record_missing")
    return value


def validate_plan(value, registry):
    """Validate a schema-4 policy projection, without installing a close route."""
    from copy import deepcopy

    validate_registry(registry)
    A._object(value, "schema_version plan_id project_id scope authors partitions rows registry_ref "
              "registry_digest trust_profile cold_policy environment", "preflight plan")
    A._version(value["schema_version"], (4,))
    validate_environment(value["environment"])
    relative_path(value["registry_ref"])
    legacy = deepcopy(value)
    del legacy["environment"]
    legacy["schema_version"] = 3
    entries = {entry["id"]: entry for entry in registry["entries"]}
    _service_refs(value["environment"], entries)
    used = set()
    for row in _list(legacy["rows"], "rows"):
        A._object(row, "id partition policy comparator expected artifact field registry_entries", "preflight row")
        refs = _refs(row.pop("registry_entries"), "row registry entries")
        if not set(refs) <= entries.keys():
            A._fail("unresolved row registry reference")
        used.update(refs)
    A.validate_plan(legacy)
    pending = list(used)
    while pending:
        for dep in entries[pending.pop()]["dependencies"]:
            if dep not in used:
                used.add(dep)
                pending.append(dep)
    if used != entries.keys():
        A._fail("unused registry entry")
    row_ids = {row["id"] for row in value["rows"]}
    if any(row["id"] not in row_ids for row in value["environment"]["row_overrides"]):
        A._fail("unresolved environment override row")
    return value


def policy(plan_bytes, registry_bytes):
    """Import exact policy bytes with their pin; no store writes or staged checks."""
    plan, registry = decode(plan_bytes), decode(registry_bytes)
    validate_plan(plan, registry)
    if plan["registry_digest"] != A._hash(registry_bytes):
        A._fail("registry differs from plan pin", "acceptance_plan_stale")
    return plan, registry


def _service_refs(environment, entries):
    seen = set()
    for role in ("runtime", "compiler", "package_manager", "services"):
        kind = "service" if role == "services" else "toolchain"
        for ref in environment[role]:
            if ref not in entries or entries[ref]["kind"] != kind or ref in seen:
                A._fail("unresolved, mistyped or repeated environment entry reference")
            seen.add(ref)
    if seen != {key for key, entry in entries.items() if entry["kind"] in ("toolchain", "service")}:
        A._fail("environment must reference every toolchain/service entry exactly once")


def evidence_ref(value):
    A._object(value, "path sha256 size", "declarative evidence reference")
    relative_path(value["path"])
    A._digest(value["sha256"])
    _integer(value["size"], "evidence size", 0, MAX_INPUT_BYTES)


def validate_observation(value, registry):
    """Validate supplied proof shape; false/mismatched observations remain data."""
    validate_registry(registry)
    A._object(value, "schema_version registry_hash observed_at environment entries", "preflight observation")
    A._version(value["schema_version"])
    A._digest(value["registry_hash"])
    utc(value["observed_at"])
    validate_environment(value["environment"])
    _service_refs(value["environment"], {entry["id"]: entry for entry in registry["entries"]})
    entries = _index(value["entries"], "observed entries", MAX_ENTRIES)
    if entries.keys() != {entry["id"] for entry in registry["entries"]}:
        A._fail("observed entry set differs from registry")
    total = sum(row["environment"]["size"] for row in value["environment"]["row_overrides"])
    for entry in entries.values():
        A._object(entry, "id version banner offline", "observed entry")
        _text(entry["version"], "observed version")
        evidence_ref(entry["banner"])
        proof = entry["offline"]
        A._object(proof, "mode egress_denied positive_control cache_hit attempted_fetch endpoints log", "offline proof")
        if proof["mode"] not in ("external-denial", "offline-recipe"):
            A._fail("unsupported offline proof mode")
        for key in ("egress_denied", "positive_control", "cache_hit", "attempted_fetch"):
            _boolean(proof[key], key)
        evidence_ref(proof["log"])
        total += entry["banner"]["size"] + proof["log"]["size"]
        for endpoint in _list(proof["endpoints"], "endpoints", MAX_ENTRIES):
            A._object(endpoint, "host port pid owned", "observed endpoint")
            _text(endpoint["host"], "endpoint host")
            _integer(endpoint["port"], "port", 1, 65535)
            _integer(endpoint["pid"], "PID", 1, 2**31 - 1)
            _boolean(endpoint["owned"], "endpoint ownership")
    if total > MAX_TOTAL_BYTES:
        A._fail("observation inputs exceed aggregate limit", "acceptance_record_missing")
    return value


def read_declarative_inputs(root, registry):
    """Read bounded declarative bytes only; hash comparison/retention belongs to M2/M3."""
    validate_registry(registry)
    inputs = {}
    total = 0
    for pin in registry["files"]:
        if pin["role"] == "distribution":
            continue
        path = staged_path(root, pin["path"])
        data = None
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                A._fail("staged declarative input must be a regular file")
            # NOFOLLOW refuses a substituted leaf link on POSIX. NONBLOCK keeps
            # a substituted FIFO from hanging before fstat can reject its type.
            flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                # Windows lacks O_NOFOLLOW: also reject a leaf replaced by a
                # link to the original inode, even when fstat identity matches.
                after = path.lstat()
                if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(after.st_mode)
                        or getattr(after, "st_file_attributes", 0) & 1024
                        or any(getattr(before, key, None) != getattr(current, key, None)
                               for current in (opened, after) for key in ("st_dev", "st_ino", "st_mode"))):
                    A._fail("staged declarative input changed while opening")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(min(MAX_INPUT_BYTES, MAX_TOTAL_BYTES - total) + 1)
            finally:
                os.close(fd)
        except OSError:
            data = None
        if data is None:
            raise A.AcceptanceError("acceptance_preflight_unavailable",
                                    "staged declarative input is unreadable") from None
        total += len(data)
        if len(data) > MAX_INPUT_BYTES or total > MAX_TOTAL_BYTES:
            A._fail("declarative input exceeds byte budget", "acceptance_record_missing")
        inputs[pin["id"]] = data
    return inputs
