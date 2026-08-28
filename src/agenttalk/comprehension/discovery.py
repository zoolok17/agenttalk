"""File enumeration, platform/path policy capture, and the three
pre-freshness resource caps.

DESIGN-55-comprehension-plane.md, "Scan behavior" step 4:

    Enumerate allowed files without following links outside the root.
    Exclude `.git/`, `.agenttalk/`, binaries, known secret files,
    dependency caches, and generated/vendor trees by default; record every
    category and count. Compute the whole-scope fingerprint before adapter
    relevance filtering.

and "Resource caps":

    Resource caps apply to file count, individual file bytes, total bytes,
    nesting, and adapter work. [...] 100,000 filesystem entries per scope
    -- PROVISIONAL; 64 MiB per file -- PROVISIONAL; 2 GiB of hashed source
    bytes per scope -- PROVISIONAL [...] Hitting a scan cap yields a
    degraded scan and explicit problem; the scanner does not silently
    sample.

All three cap constants are PROVISIONAL pending the PR-B exit-gate
measurement against a representative corpus (task #55 slice-1 dispatch,
C-6) - this module enforces whatever the constants currently say, not a
claim that these exact numbers are final.

Scope simplification for THIS commit (flagged, not silently decided): the
design distinguishes hard-excluded content (secret/VCS/cache - "not read or
copied into the fingerprint") from the wider set that still contributes to
whole-scope freshness tracking even when not adapter-addressable. This
module currently treats every default-exclude category uniformly: tallied
by category+count, never read for hashing, never in the whole-scope
fingerprint's per-entry inputs. Revisit if review wants the wider
freshness-tracking behavior for generated/vendor/binary content
specifically.

CRITICAL ORDERING (per the lead's explicit round-4 confirmation on the
approved PR-B plan): the per-file byte-size cap is checked from
``stat().st_size`` alone, BEFORE any file content is read for binary/text
sniffing. A "binaries excluded" heuristic must never pre-empt the cap, or
a large vendored binary (the exit-gate's named JDK RPM case) could never
reach it.
"""

from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import json
import os
import shutil
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

from .paths import RELATIVE_COMPREHENSION_DIR

PATH_NORMALIZATION_VERSION = 1

#: PROVISIONAL - see module docstring.
MAX_FILESYSTEM_ENTRIES = 100_000
MAX_PER_FILE_BYTES = 64 * 1024 * 1024
MAX_HASHED_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
#: N6-nesting (cold-read, PR-B fix round 3): the design lists nesting
#: among the resource caps, but _walk recursed with no depth limit at
#: all, and only caught OSError - a pathologically deep directory tree
#: could raise a bare RecursionError that propagated uncaught, crashing
#: the whole scan rather than degrading it with a bounded problem. Well
#: short of Python's own default recursion limit (~1000), leaving ample
#: headroom for this function's own call-stack frames above it.
MAX_NESTING_DEPTH = 200

_HARD_EXCLUDE_DIR_NAMES = frozenset({".git", ".agenttalk"})
_VCS_DIR_NAMES = frozenset({".hg", ".svn"})
_DEPENDENCY_CACHE_DIR_NAMES = frozenset({
    "node_modules", ".m2", ".gradle", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache",
})
_GENERATED_VENDOR_DIR_NAMES = frozenset({"target", "build", "dist", "vendor", "out", ".next"})
_SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.pfx", "*.p12", "*.ppk",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
)
_BINARY_SNIFF_BYTES = 8192
_PLATFORM_PROBE_RELATIVE_DIR = f"{RELATIVE_COMPREHENSION_DIR}/.platform-probe"


@dataclass(frozen=True)
class PlatformIdentity:
    """DESIGN-55-comprehension-plane.md, "Scan manifest": "platform
    identity: OS family, architecture, path-normalization version,
    case-sensitivity result, and filesystem Unicode-normalization policy."
    ``case_sensitive``/``unicode_normalizing`` are EMPIRICALLY probed
    against the actual scanned filesystem (never assumed from OS family
    alone) - a per-directory case-sensitivity opt-in on Windows, or an
    unusual POSIX filesystem, would otherwise be silently misreported."""

    os_family: str
    architecture: str
    path_normalization_version: int
    case_sensitive: bool
    unicode_normalizing: bool


@dataclass(frozen=True)
class EnumeratedFile:
    """One addressable ``file`` unit candidate (design, Artifact 1: "Every
    non-excluded file remains an addressable `file` unit"). ``content_digest``
    is ``None`` only when a resource cap prevented reading this specific
    file's bytes (see ``problems``) - never silently defaulted."""

    relative_path: str
    byte_count: int
    content_digest: str | None


@dataclass(frozen=True)
class BoundaryEntry:
    """A symlink or submodule boundary: recorded, never followed/entered
    (design: "Symlinks are recorded as boundaries and not followed by
    default"; "Submodules are external boundaries unless explicitly
    included")."""

    relative_path: str
    boundary_kind: str


@dataclass(frozen=True)
class DiscoveryResult:
    platform_identity: PlatformIdentity
    files: list[EnumeratedFile] = field(default_factory=list)
    boundaries: list[BoundaryEntry] = field(default_factory=list)
    exclusions: dict[str, int] = field(default_factory=dict)
    problems: list[dict[str, str]] = field(default_factory=list)
    whole_scope_fingerprint: str | None = None
    fingerprint_complete: bool = True
    degraded: bool = False


def _windows_architecture() -> str:
    """Queries ``GetNativeSystemInfo`` directly - deliberately NOT the
    ``platform`` module. Empirically verified (this same investigation that
    fixed ``lock.host_identity``): ``platform.machine()``/``platform.
    system()`` BOTH transitively import and use ``socket`` on Windows
    (CPython's ``platform.uname()`` builds the whole tuple, node/hostname
    included, as one cached unit even when only ``.machine`` is read).
    ``ctypes`` calling the Win32 API directly needs no such fallback."""

    class _SystemInfo(ctypes.Structure):
        _fields_ = [
            ("wProcessorArchitecture", ctypes.c_ushort),
            ("wReserved", ctypes.c_ushort),
            ("dwPageSize", ctypes.c_ulong),
            ("lpMinimumApplicationAddress", ctypes.c_void_p),
            ("lpMaximumApplicationAddress", ctypes.c_void_p),
            ("dwActiveProcessorMask", ctypes.c_void_p),
            ("dwNumberOfProcessors", ctypes.c_ulong),
            ("dwProcessorType", ctypes.c_ulong),
            ("dwAllocationGranularity", ctypes.c_ulong),
            ("wProcessorLevel", ctypes.c_ushort),
            ("wProcessorRevision", ctypes.c_ushort),
        ]

    names = {0: "x86", 5: "arm", 6: "ia64", 9: "x64", 12: "arm64"}
    info = _SystemInfo()
    ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(info))  # type: ignore[attr-defined]
    return names.get(info.wProcessorArchitecture, "unknown")


def _architecture() -> str:
    if os.name == "nt":
        return _windows_architecture()
    if hasattr(os, "uname"):
        return os.uname().machine
    return "unknown"


def _probe_case_sensitivity(probe_dir: Path) -> bool:
    lower = probe_dir / "CaseProbe.tmp"
    upper = probe_dir / "caseprobe.tmp"
    lower.write_bytes(b"x")
    try:
        return not upper.exists()
    finally:
        lower.unlink(missing_ok=True)


def _probe_unicode_normalization(probe_dir: Path) -> bool:
    nfc_name = "éprobe.tmp"  # 'e' with acute accent, precomposed
    nfd_name = "éprobe.tmp"  # 'e' + combining acute accent
    nfc_path = probe_dir / nfc_name
    nfd_path = probe_dir / nfd_name
    nfc_path.write_bytes(b"x")
    try:
        return nfd_path.exists()
    finally:
        nfc_path.unlink(missing_ok=True)
        if nfd_path.exists():
            nfd_path.unlink(missing_ok=True)


def detect_platform_identity(comprehension_dir: Path) -> PlatformIdentity:
    """``comprehension_dir`` must already exist (true from lock acquisition
    onward - see the design's scan pipeline: the writer lock is acquired
    before enumeration runs). The probe directory sits under it
    specifically because ``.agenttalk/`` is always hard-excluded from
    enumeration itself, so probing there can never pollute or affect a
    real scan result (mirrors ``privacy.py``'s own probe-path convention).
    """
    probe_dir = comprehension_dir / ".platform-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    try:
        case_sensitive = _probe_case_sensitivity(probe_dir)
        unicode_normalizing = _probe_unicode_normalization(probe_dir)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    return PlatformIdentity(
        os_family=os.name,
        architecture=_architecture(),
        path_normalization_version=PATH_NORMALIZATION_VERSION,
        case_sensitive=case_sensitive,
        unicode_normalizing=unicode_normalizing,
    )


def _boundary_kind(entry: Path) -> str | None:
    """Returns a boundary kind (``"symlink"`` or ``"reparse_point"``) if
    ``entry`` must be recorded and never followed; ``None`` for an
    ordinary entry.

    Cold-read B1 (reviewer, PR-B fix round 3): ``entry.is_symlink()``
    alone is not the whole boundary surface on Windows. A directory
    JUNCTION (mount point) is a reparse point but NOT a symlink proper -
    ``is_symlink()`` returns ``False`` for one, so the walker would
    otherwise descend into it, hash content living outside the project
    root, and fold it into the whole-scope fingerprint before the
    worker's own re-confinement ever gets a chance to catch it. Detected
    via ``st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`` - the same
    attribute Windows Explorer and ``dir`` use to identify a reparse
    point regardless of its specific reparse tag (symlink, junction, or
    otherwise unrecognized), so this also catches reparse tags this
    module has never heard of, not just the ones named here.
    """
    if entry.is_symlink():
        return "symlink"
    if os.name != "nt":
        return None
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    if st.st_file_attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT:
        return "reparse_point"
    return None


def _non_utf8_path_problem_detail(relative: str) -> dict[str, str] | None:
    """Returns a bounded problem's ``path``/``detail`` pair if ``relative``
    cannot be represented as UTF-8 (the encoding every published artifact
    uses), else ``None``. Factored out of ``_walk`` (an inner closure, not
    independently callable) specifically so this exact check is directly
    unit-testable against a manually-constructed surrogate string, without
    needing a real non-UTF-8 filename on disk (POSIX-only, and not
    reliably constructible from every dev/CI platform).

    The returned ``path`` is a backslash-escaped, ASCII-safe
    representation of the same bytes - never the raw un-encodable string
    itself, which would trip the identical failure the moment this
    problem record's OWN containing document gets serialized.
    """
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return {
            "path": relative.encode("utf-8", errors="backslashreplace").decode("ascii"),
            "detail": "path contains bytes that are not valid UTF-8 and cannot be "
                      "represented in this artifact format",
        }
    return None


def _looks_binary(data: bytes) -> bool:
    """A NUL byte anywhere in the sniffed prefix is the same heuristic
    Git itself uses to classify a blob as binary - simple, well
    understood, and cheap. Never applied before the size cap (see module
    docstring)."""
    return b"\x00" in data[:_BINARY_SNIFF_BYTES]


def _exclusion_category(name: str, *, is_dir: bool) -> str | None:
    if is_dir:
        if name in _HARD_EXCLUDE_DIR_NAMES:
            return "hard_excluded"
        if name in _VCS_DIR_NAMES:
            return "vcs"
        if name in _DEPENDENCY_CACHE_DIR_NAMES:
            return "dependency_cache"
        if name in _GENERATED_VENDOR_DIR_NAMES:
            return "generated_or_vendor"
        return None
    if any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_FILE_PATTERNS):
        return "secret"
    return None


def _submodule_boundary_paths(root: Path) -> frozenset[str]:
    """A minimal ``.gitmodules`` parse: every ``path = ...`` line's value,
    POSIX-spelled. Full git-submodule semantics (nested configs, URL
    rewriting) are out of scope - this only needs the boundary PATHS
    themselves, to satisfy "submodules are external boundaries unless
    explicitly included" (design). No git subprocess is invoked; this is a
    plain text parse of a file already inside the resolved root."""
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return frozenset()
    paths: set[str] = set()
    try:
        text = gitmodules.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("path") and "=" in stripped:
            _, _, value = stripped.partition("=")
            value = value.strip().replace("\\", "/")
            if value:
                paths.add(value)
    return frozenset(paths)


def enumerate_scope(root: Path, comprehension_dir: Path) -> DiscoveryResult:
    """Walk ``root``, apply default excludes, record symlink/submodule
    boundaries, enforce the three pre-freshness resource caps in the
    mandated order (name-based exclude -> size cap from ``stat()`` alone
    -> content read for binary-sniff + hash), and compute the whole-scope
    fingerprint from what was actually hashed. Never follows a symlink;
    never descends into a hard/default-excluded directory; never raises
    for an oversized or binary file - both are bounded, counted outcomes.
    """
    platform_identity = detect_platform_identity(comprehension_dir)
    submodule_boundaries = _submodule_boundary_paths(root)

    files: list[EnumeratedFile] = []
    boundaries: list[BoundaryEntry] = []
    exclusions: dict[str, int] = {}
    problems: list[dict[str, str]] = []
    degraded = False
    entry_count = 0
    hashed_total = 0
    entry_cap_hit = False

    def _record_exclusion(category: str) -> None:
        exclusions[category] = exclusions.get(category, 0) + 1

    def _walk(directory: Path, depth: int = 0) -> None:
        nonlocal entry_count, hashed_total, degraded, entry_cap_hit
        if depth > MAX_NESTING_DEPTH:
            degraded = True
            problems.append({
                "reason_code": "resource_limit",
                "path": directory.relative_to(root).as_posix(),
                "detail": f"exceeded the {MAX_NESTING_DEPTH}-level nesting cap",
            })
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            problems.append({
                "reason_code": "parse_failed",
                "path": str(directory.relative_to(root).as_posix()),
                "detail": str(exc),
            })
            return
        for entry in entries:
            if entry_cap_hit:
                return
            relative = entry.relative_to(root).as_posix()
            # Note 5 (second cold read, fix round 4): on POSIX, a filename
            # with bytes that are not valid UTF-8 decodes (via Python's
            # surrogateescape handling) to a string containing lone
            # surrogates - canonical_json_bytes' `.encode("utf-8")` later
            # raises UnicodeEncodeError on that string, an UNHANDLED
            # traceback surfacing late, at artifact-write time, well after
            # the lock and all the scan's own work. Caught here instead, at
            # the EARLIEST possible point (this is also where every other
            # bounded, reported exclusion in this walk already happens) -
            # this one entry is excluded with a named problem; the scan
            # degrades, it never crashes wholesale.
            non_utf8_detail = _non_utf8_path_problem_detail(relative)
            if non_utf8_detail is not None:
                problems.append({
                    "reason_code": "non_utf8_path", "path": non_utf8_detail["path"],
                    "detail": non_utf8_detail["detail"],
                })
                continue
            boundary_kind = _boundary_kind(entry)
            if boundary_kind is not None:
                boundaries.append(BoundaryEntry(relative_path=relative, boundary_kind=boundary_kind))
                continue
            if entry.is_dir():
                category = _exclusion_category(entry.name, is_dir=True)
                if category is not None:
                    _record_exclusion(category)
                    continue
                if relative in submodule_boundaries:
                    boundaries.append(
                        BoundaryEntry(relative_path=relative, boundary_kind="submodule"))
                    continue
                _walk(entry, depth + 1)
                continue
            # A regular file.
            category = _exclusion_category(entry.name, is_dir=False)
            if category is not None:
                _record_exclusion(category)
                continue
            entry_count += 1
            if entry_count > MAX_FILESYSTEM_ENTRIES:
                entry_cap_hit = True
                degraded = True
                problems.append({
                    "reason_code": "resource_limit",
                    "path": relative,
                    "detail": f"exceeded the {MAX_FILESYSTEM_ENTRIES}-entry filesystem cap",
                })
                return
            try:
                size = entry.stat().st_size
            except OSError as exc:
                problems.append({
                    "reason_code": "parse_failed", "path": relative, "detail": str(exc)})
                continue
            # The per-file size cap is checked from stat() ALONE, before any
            # content is read - a large binary must trip this, never be
            # silently removed by a later binary-sniff exclusion first.
            if size > MAX_PER_FILE_BYTES:
                degraded = True
                _record_exclusion("resource_limit_oversized")
                problems.append({
                    "reason_code": "resource_limit",
                    "path": relative,
                    "detail": f"{size} bytes exceeds the {MAX_PER_FILE_BYTES}-byte per-file cap",
                })
                continue
            try:
                data = entry.read_bytes()
            except OSError as exc:
                problems.append({
                    "reason_code": "parse_failed", "path": relative, "detail": str(exc)})
                continue
            if _looks_binary(data):
                _record_exclusion("binary")
                continue
            if hashed_total + len(data) > MAX_HASHED_TOTAL_BYTES:
                degraded = True
                _record_exclusion("resource_limit_total_bytes")
                problems.append({
                    "reason_code": "resource_limit",
                    "path": relative,
                    "detail": f"whole-scope hashed bytes would exceed the "
                              f"{MAX_HASHED_TOTAL_BYTES}-byte cap",
                })
                continue
            hashed_total += len(data)
            files.append(EnumeratedFile(
                relative_path=relative,
                byte_count=len(data),
                content_digest=hashlib.sha256(data).hexdigest(),
            ))

    _walk(root)

    # B-1 (third cold read, fix round 5): every problem this walk records -
    # an unlistable directory, a stat/read failure, an unrepresentable
    # filename, or a resource cap - means one entry that would otherwise
    # have contributed to `files`/`boundaries` was OMITTED from what got
    # walked and hashed. A single choke point here (rather than a manual
    # `fingerprint_complete = False` at each of the eight problem-raising
    # sites above) makes a ninth future exit incomplete-by-construction
    # instead of relying on every future editor to remember the flag.
    fingerprint_complete = not problems

    fingerprint = None
    if fingerprint_complete:
        fingerprint_input = {
            "platform_identity": {
                "os_family": platform_identity.os_family,
                "architecture": platform_identity.architecture,
                "path_normalization_version": platform_identity.path_normalization_version,
                "case_sensitive": platform_identity.case_sensitive,
                "unicode_normalizing": platform_identity.unicode_normalizing,
            },
            "files": sorted(
                (f.relative_path, "file", f.content_digest) for f in files
            ),
            "boundaries": sorted(
                (b.relative_path, b.boundary_kind) for b in boundaries
            ),
            "exclusions": dict(sorted(exclusions.items())),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    return DiscoveryResult(
        platform_identity=platform_identity,
        files=files,
        boundaries=boundaries,
        exclusions=exclusions,
        problems=problems,
        whole_scope_fingerprint=fingerprint,
        fingerprint_complete=fingerprint_complete,
        degraded=degraded,
    )
