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
import re
import shutil
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

from .errors import bounded_os_error_detail
from .paths import RELATIVE_COMPREHENSION_DIR

PATH_NORMALIZATION_VERSION = 1

#: PROVISIONAL - see module docstring.
#:
#: N10 (fourth cold read, fix round 6, judged): the design's own wording
#: ("100,000 filesystem entries per scope") reads as EVERY entry walked -
#: directories, boundaries, and default-excluded files/dirs included.
#: This counts only non-excluded REGULAR FILES (the ``entry_count += 1``
#: below sits in the "regular file" branch, after every exclusion check
#: has already run) - a directory, a boundary, or an excluded file is
#: free against this cap. Declared here as a deliberate reading, not a
#: silent divergence: this cap's own PURPOSE is to bound the EXPENSIVE
#: work enumeration does (stat-ing, reading, hashing a regular file,
#: each backed by the separate per-file/total-byte caps below) - a
#: directory node or an already-excluded entry costs one cheap
#: ``iterdir()``/name-match and is never read or hashed, so counting it
#: toward the SAME limit would trip a monorepo's cap on directory or
#: dependency-cache SPRAWL alone, never on actual scan cost. Fail-open
#: relative to the design's literal wording, bounded anyway by the
#: byte caps; revisit if review wants the wording taken literally.
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
#: FIX ROUND 16 (twelfth cold read, B2 BLOCKER, wrong-data): the ANY-
#: DEPTH name exclusion silently deleted a real, standard hexagonal-
#: architecture package (``.../port/out/PaymentGateway.java``) from the
#: whole inventory - ``out`` is a routine Java package name at that
#: depth, never a build-output directory the way it is at repo/module
#: root level. A generated/vendor NAME match only fires OUTSIDE a
#: recognized source root - inside one (``src/main/java/...`` and its
#: siblings), the same name is a PACKAGE segment, not a build artifact.
#: PROVISIONAL, the same Maven/Gradle standard-layout convention this
#: whole package already keys test classification off (``modules_
#: artifact._TEST_SOURCE_ROOT_SEGMENT``). Explicit SEMANTICS CHANGE,
#: flagged for reviewer-3: a repo whose build tooling genuinely places
#: OUTPUT under ``src/main/java/.../build/`` (unusual, but not
#: impossible) would now walk into it instead of excluding it - judged
#: the correct trade given a real, standard package name silently
#: vanishing is the worse failure mode.
#:
#: FIX ROUND 18 (fourteenth cold read, F1 BLOCKER, wrong-data): the
#: prefix match above was anchored to REPO ROOT (``str.startswith``
#: from position 0) - it only ever recognized a source root that WAS
#: the whole repo. A multi-module reactor (``core/src/main/java/...``),
#: a Kotlin/Groovy/Scala root (``src/main/kotlin/...``), a webapp tree
#: (``src/main/webapp/...``), or any source root one directory deeper
#: than repo root defeated it silently: the SAME hexagonal ``port/out``
#: package this fix was originally written for goes right back to being
#: deleted, and worse, a tier-2 code-bearing file (``.kt``, ``.jsp``)
#: sitting under an excluded directory vanishes along with it, with the
#: run still reporting complete/zero problems. Recognition now keys on
#: the SOURCE-ROOT SEGMENT PATTERN appearing ANYWHERE in the ancestor
#: chain (``src/(main|test)/(java|kotlin|groovy|scala|resources|
#: webapp)``, at any depth), not a repo-root anchor - this still
#: requires the LITERAL Maven/Gradle segment sequence somewhere in the
#: path, so a genuine module-root output directory with no such
#: segment anywhere (``core/build/``) is unaffected and stays excluded.
#: JUDGED NOT to also recognize a bare Ant-style ``java/`` root with no
#: ``src/main`` or ``src/test`` prefix: unlike the Maven/Gradle segment
#: sequence, a lone ``java`` (or ``test``) directory name is common
#: enough to appear in unrelated contexts (a vendored dependency's own
#: internal layout, a documentation-sample directory, ...) that
#: recognizing it unconditionally would reopen a broader false-
#: exemption risk than this fix is closing - the qualifying segment
#: must include the ``src/main/`` or ``src/test/`` scaffolding that
#: makes the convention unambiguous.
_RECOGNIZED_SOURCE_ROOT_SEGMENT_RE = re.compile(
    r"(?:^|/)src/(?:main|test)/(?:java|kotlin|groovy|scala|resources|webapp)(?:/|$)",
)


def _is_inside_a_recognized_source_root(relative_path: str) -> bool:
    return bool(_RECOGNIZED_SOURCE_ROOT_SEGMENT_RE.search(relative_path))


#: FIX ROUND 19 (fifteenth cold read, F4 MAJOR, wrong-data): the round-18
#: Ant-layout residual (a bare ``src/`` root, never recognized by
#: ``_RECOGNIZED_SOURCE_ROOT_SEGMENT_RE`` above since it has no ``main``/
#: ``test`` scaffolding) is measured WIDER than accepted - a DOMAIN
#: package literally named ``vendor``/``out``/etc under such a root is
#: deleted from the inventory as ``generated_or_vendor`` on a COMPLETE
#: run, asserting a factually wrong category about real, hand-written
#: code. Generalizes round 18's own F6 standard (a binary-sniffed file
#: whose extension is code-bearing degrades the run) to the DIRECTORY
#: exclusion case: kept in sync by hand with worker.py's own
#: ``_ADAPTER_EXTENSIONS``/``_DEGRADING_CODE_EXTENSIONS`` (discovery.py
#: cannot import worker.py - worker.py already imports discovery.py,
#: and this module owns filesystem access exclusively) - see that
#: module's own criterion comment before adding a new one here.
_DEGRADABLE_EXCLUDED_EXTENSIONS = frozenset({
    ".java",
    ".jsp", ".jspx", ".jspf", ".tag", ".sql", ".groovy", ".kt", ".scala", ".xhtml", ".ftl", ".vm",
    ".cs", ".php", ".rb", ".go", ".pks", ".pkb", ".xsl",
})
#: A genuine build-output position (repo/module-root ``target``/
#: ``build``/``dist``/etc, the classic contents of which ARE generated
#: `.java`/`.class`/`.jar`) has NO ``src`` segment anywhere in its own
#: path - a blanket degrading rule there would degrade every ordinary
#: Maven/Gradle repo (the exact regression round 16b's own B4
#: calibration measurement already fixed once for the analogous tier-2
#: rule). An excluded region nested under a literal ``src`` segment that
#: was NOT carved out as a recognized source root (the Ant case) is a
#: different position entirely - real, hand-authored source trees are
#: the ONLY thing that legitimately lives under a bare ``src/``.
_MAX_EXCLUDED_DIRECTORY_PEEK_ENTRIES = 5_000


def _sits_under_an_uncarved_src_segment(relative_path: str) -> bool:
    return "src" in relative_path.split("/")


def _excluded_directory_contains_a_code_bearing_file(directory: Path) -> tuple[bool, bool]:
    """A BOUNDED peek inside a directory this run is about to exclude
    outright as ``generated_or_vendor`` - never added to ``files``,
    never hashed, just a cheap extension check so an excluded region
    that swallows real, hand-written code can be told apart from an
    ordinary build-output tree (a real ``target/``/``build/`` full of
    compiled ``.class``/``.jar`` output stays silent, unaffected).
    Never follows a symlink (this walk exists purely to answer a yes/no
    extension question, never to enumerate or hash content - skipping
    is the safe, conservative choice, not a correctness requirement).
    Short-circuits on the FIRST match; bounded by a hard entry-count cap
    (PROVISIONAL, like every other cap in this module) against a
    pathologically large excluded subtree.

    Returns ``(found, truncated)``. FIX ROUND 19b (reviewer-3's rejection
    of round 19, THE MAJOR, wrong-data): this used to return a bare
    ``bool``, with cap exhaustion silently folded into the SAME ``False``
    a genuinely fully-explored, code-free directory returns - the caller
    then read "exhausted" as "no code found", a confident negative this
    peek never actually earned. Measured: two repos differing ONLY in
    the FILENAME of the one ``.java`` among thousands of ``.class``
    files inside a bare ``src/build/`` (the normal Ant-era shape, easily
    past the entry cap) published DIFFERENT run status purely because
    ``os.scandir`` happened to visit one filename before the cap and the
    other after it - a published outcome depending on filesystem
    enumeration order, on a plane whose entire design is reproducible
    runs. ``truncated`` is ``True`` only when the cap was hit WITHOUT
    finding a match - the caller (discovery.py's own ``_walk``) now
    treats that as its own honest, distinct, still-degrading outcome
    (``excluded_region_peek_truncated``) rather than silently reusing
    the confident "no code" problem code - the same "record the
    truncation, never just stop looking" discipline every OTHER cap in
    this module already follows (``MAX_FILESYSTEM_ENTRIES``, the
    per-file/total-byte caps, ...).

    FIX ROUND 20 (sixteenth cold read, P6 MINOR, taken): a symlinked
    subdirectory used to be silently skipped as if it were simply
    absent - folding "we deliberately never look here" into the same
    ``False`` a genuinely explored, code-free directory returns, exactly
    the confident-negative-from-an-unexplored-region defect round 19b
    already fixed for cap exhaustion above. We genuinely do not know
    what a symlink points to; skipping it (still the right, safe choice
    - this peek never follows symlinks) now marks the peek
    ``truncated`` the same way running out of the entry cap does,
    rather than silently counting as "confidently no code".

    FIX ROUND 21 (seventeenth cold read, CR17-5 MAJOR, completeness -
    calibration, CRITICAL for the exit-gate measurement): a code-bearing
    file sitting under a RECOGNIZED GENERATED-OUTPUT position inside the
    excluded root (``target/generated-sources``, ``target/generated-
    test-sources``, ``build/generated`` - MapStruct/Lombok/JPA-
    metamodel/protobuf-generated ``.java``, ubiquitous in any ordinary
    compiled Maven/Gradle repo) is EXPECTED, build-tool-authored output,
    indistinguishable to a bare extension check from vendored first-
    party source - poisoning round 20's own POISON RULE on the single
    MOST COMMON repo state (any compiled repo at all) rather than the
    genuinely suspicious shapes it exists to catch. Exempted by POSITION
    specifically (never by excluded-root NAME, and never the root as a
    whole) - a ``.java`` sitting anywhere ELSE inside the same excluded
    root (a vendored reactor module under ``target/``, a stray hand-
    written file) still counts as evidence and still poisons, exactly
    as before this round."""
    visited = 0
    symlink_skipped = False
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            visited += 1
            if visited > _MAX_EXCLUDED_DIRECTORY_PEEK_ENTRIES:
                return False, True
            try:
                if entry.is_symlink():
                    symlink_skipped = True
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(
                    tuple(_DEGRADABLE_EXCLUDED_EXTENSIONS),
                ):
                    relative_to_excluded_root = Path(entry.path).relative_to(
                        directory).as_posix()
                    if _sits_under_a_recognized_generated_output_position(
                        relative_to_excluded_root,
                    ):
                        continue
                    return True, False
            except OSError:
                continue
    return False, symlink_skipped


#: FIX ROUND 21 (seventeenth cold read, CR17-5 MAJOR, completeness -
#: calibration): CLOSED, PROVISIONAL list of recognized generated-output
#: positions - documented like tier 2 (worker.py's own extension sets),
#: not chasing exhaustiveness. Anchored to the START of the path
#: relative to the excluded root itself (the excluded root IS ``target``/
#: ``build`` by construction - discovery only ever peeks inside a
#: ``generated_or_vendor``-category root), never a mid-path search - a
#: coincidentally-named ``generated`` directory living somewhere ELSE
#: inside an excluded tree (e.g. ``target/some-vendor-lib/generated/``)
#: is NOT this recognized position and still counts as evidence.
_RECOGNIZED_GENERATED_OUTPUT_POSITIONS = (
    "generated-sources/", "generated-test-sources/", "generated/",
)


def _sits_under_a_recognized_generated_output_position(relative_to_excluded_root: str) -> bool:
    return relative_to_excluded_root.startswith(_RECOGNIZED_GENERATED_OUTPUT_POSITIONS)


_SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.pfx", "*.p12", "*.ppk",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
)
_BINARY_SNIFF_BYTES = 8192
_PLATFORM_PROBE_RELATIVE_DIR = f"{RELATIVE_COMPREHENSION_DIR}/.platform-probe"


def effective_exclude_rule_digest() -> str:
    """SHA-256 over the CURRENT default-exclude rule sets (hard-excluded/
    VCS/dependency-cache/generated-vendor directory names, secret file
    patterns) - published in scan.json (N2, fourth cold read, fix round
    6) so a future change to any of these hardcoded lists is
    independently detectable even before ``config.json`` exists to make
    them caller-configurable. Without this, changing what these lists
    exclude silently changes what ``whole_scope_fingerprint`` means, with
    no recorded rule identity to explain why two scans of the identical
    tree, at different points in this codebase's own history, now
    disagree."""
    payload = {
        "hard_excluded_dirs": sorted(_HARD_EXCLUDE_DIR_NAMES),
        "vcs_dirs": sorted(_VCS_DIR_NAMES),
        "dependency_cache_dirs": sorted(_DEPENDENCY_CACHE_DIR_NAMES),
        "generated_vendor_dirs": sorted(_GENERATED_VENDOR_DIR_NAMES),
        "secret_file_patterns": sorted(_SECRET_FILE_PATTERNS),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
    non-excluded file remains an addressable `file` unit").

    N4 (fourth cold read, fix round 6): ``content_digest`` previously
    documented (and typed) a ``None`` case for "a resource cap prevented
    reading this specific file's bytes" - that case cannot occur HERE:
    every resource-cap/read-failure exit in ``_walk`` (below) returns or
    ``continue``s BEFORE constructing an ``EnumeratedFile`` at all (the
    problem is recorded via ``problems``/``exclusions`` instead, and no
    file candidate is ever created for it). The single construction site
    always supplies a real digest from bytes it just successfully read
    and hashed - documenting a null case nothing produces would have let
    a caller silently accept (and modules.json silently publish) a null
    digest, had one ever slipped through undetected."""

    relative_path: str
    byte_count: int
    content_digest: str


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
    #: FIX ROUND 16 (twelfth cold read, B2 BLOCKER, part 2): `exclusions`
    #: is a bare category -> count map - it names WHAT was excluded, never
    #: WHERE. The design's own scan.json fields name "excluded roots with
    #: an explicit boundary reason" - this is that list, one entry per
    #: excluded root path (bounded + omitted-count at publish time in
    #: scan_pipeline.py, the same discipline `boundaries` already follows).
    excluded_roots: list[dict[str, str]] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)
    whole_scope_fingerprint: str | None = None
    fingerprint_complete: bool = True
    degraded: bool = False
    #: FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR, wrong-data - the
    #: POISON RULE): True when ANY generated/vendor-named excluded
    #: directory this run swallowed either genuinely contains an
    #: adapter-handled/tier-2 code-bearing file, or the peek exceeded its
    #: own entry cap before it could confirm either way - regardless of
    #: whether that directory sits under an "uncarved src ancestor" (F4's
    #: OWN narrower degradation boundary, unchanged). A registry miss may
    #: publish a confident EXTERNAL claim only when this is False for the
    #: whole run - dependencies_artifact.py consults it (alongside its
    #: own reactor-rule finding) rather than trying to string-match an
    #: individual target's own qualified name against an excluded root's
    #: path, which round 19's own fix could never make sound for the
    #: mainstream Maven vendored-module-inside-an-excluded-tree shape
    #: (the excluded root is recorded as a bare directory name -
    #: ``vendor`` - while the unwalked source lives arbitrarily deeper -
    #: ``vendor/<module>/src/main/java/<pkg>/...`` - with no string
    #: relationship the qualified name alone could ever recover).
    excluded_region_may_contain_target: bool = False
    #: FIX ROUND 20b (seventeenth-round dispatch, THE MAJOR - poison-rule
    #: VISIBILITY): the same condition ``excluded_region_may_contain_
    #: target`` folds into one run-wide boolean, WITH per-root attribution
    #: - one entry per generated/vendor exclusion whose peek found code
    #: or was truncated before it could rule code out, each ``{"path":
    #: <root's own relative path>, "trigger": "peek_positive" or
    #: "peek_truncated"}``. Reviewer-3's own measured finding: the poison
    #: rule (round 20's M1+M2) fires SILENTLY - a run whose registry
    #: misses all resolve unresolved because of this had no record
    #: anywhere naming which root did it or why. scan_pipeline.py turns
    #: each entry here into its own ``externality_suppressed`` problem;
    #: never used for containment/matching itself (that question is the
    #: poison rule's own job) - purely an attribution list for visibility.
    poisoning_excluded_roots: list[dict[str, str]] = field(default_factory=list)


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

    N3 (seventh cold read, fix round 11, corrected same round after a
    CI-caught regression): a single ``lstat()`` call answers BOTH the
    symlink and the reparse-point question - calling ``entry.is_symlink()``
    separately, as the first cut of this fix did, hides an equally real
    fail-open gap: on this Python/platform combination ``is_symlink()``
    does not itself catch ``OSError``, so a stat failure there crashed
    this function outright rather than degrading anything. One stat
    call, one failure path, fails CLOSED for both checks alike: an entry
    that cannot even be verified might be a symlink or a junction
    pointing outside the root, so it is treated as a boundary - recorded,
    never entered - rather than a silent "must be fine" OR an unhandled
    crash.
    """
    try:
        st = entry.lstat()
    except OSError:
        return "unverifiable"
    if stat_module.S_ISLNK(st.st_mode):
        return "symlink"
    if os.name != "nt":
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


def _exclusion_category(name: str, relative_path: str, *, is_dir: bool) -> str | None:
    if name in _HARD_EXCLUDE_DIR_NAMES:
        # M2 (sixth cold read, fix round 10): `.git` as a REGULAR FILE (a
        # git worktree or submodule checkout stores a `gitdir: ...`
        # pointer there, not a directory) was neither excluded nor
        # counted - published as an addressable unit and folded into the
        # whole-scope fingerprint. That pointer names an absolute path
        # that differs per worktree/machine even for the exact same
        # commit, so two worktrees of the same commit could never
        # fingerprint equal - the exact field PR-C freshness gates on.
        # Hard-excluded by NAME regardless of `is_dir`, same as the
        # directory shape already was.
        return "hard_excluded"
    if is_dir:
        if name in _VCS_DIR_NAMES:
            return "vcs"
        if name in _DEPENDENCY_CACHE_DIR_NAMES:
            return "dependency_cache"
        if name in _GENERATED_VENDOR_DIR_NAMES and not _is_inside_a_recognized_source_root(
            relative_path,
        ):
            return "generated_or_vendor"
        return None
    if any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_FILE_PATTERNS):
        return "secret"
    return None


def _submodule_boundary_paths(root: Path) -> tuple[frozenset[str], dict[str, str] | None]:
    """A minimal ``.gitmodules`` parse: every ``path = ...`` line's value,
    POSIX-spelled. Full git-submodule semantics (nested configs, URL
    rewriting) are out of scope - this only needs the boundary PATHS
    themselves, to satisfy "submodules are external boundaries unless
    explicitly included" (design). No git subprocess is invoked; this is a
    plain text parse of a file already inside the resolved root.

    N2 (seventh cold read, fix round 11): an unreadable ``.gitmodules``
    used to silently return an EMPTY boundary set - indistinguishable
    from "no submodules at all" - so any real submodule then walked
    STRAIGHT INTO the fingerprint with ``fingerprint_complete: true``.
    Fail-open against this package's own choke-point discipline: you
    cannot know what you failed to exclude. Returns ``(paths, problem)``
    - ``problem`` is ``None`` on success (including the ordinary "no
    ``.gitmodules`` file" case, never a failure), or a problem dict the
    caller must record (marking the fingerprint incomplete) when the
    file exists but could not be read."""
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return frozenset(), None
    paths: set[str] = set()
    try:
        text = gitmodules.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return frozenset(), {
            "reason_code": "parse_failed",
            "path": ".gitmodules",
            "detail": bounded_os_error_detail(
                "could not read .gitmodules - submodule boundaries are unknown, "
                "so none could be excluded", exc),
        }
    for line in text.splitlines():
        stripped = line.strip()
        # N7 (fourth cold read, fix round 6): startswith("path") also
        # matched a DIFFERENT git-config key that merely starts with the
        # same letters (e.g. "pathspec = ..."), silently treating an
        # unrelated key's value as if it were a submodule path. The key
        # (everything before "=", trimmed) must equal "path" exactly.
        key, sep, value = stripped.partition("=")
        if sep and key.strip() == "path":
            value = value.strip().replace("\\", "/")
            if value:
                paths.add(value)
    return frozenset(paths), None


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
    submodule_boundaries, submodule_problem = _submodule_boundary_paths(root)

    files: list[EnumeratedFile] = []
    boundaries: list[BoundaryEntry] = []
    exclusions: dict[str, int] = {}
    excluded_roots: list[dict[str, str]] = []
    problems: list[dict[str, str]] = []
    degraded = False
    excluded_region_may_contain_target = False
    poisoning_excluded_roots: list[dict[str, str]] = []
    entry_count = 0
    hashed_total = 0
    entry_cap_hit = False

    if submodule_problem is not None:
        # N2 (seventh cold read, fix round 11): an unreadable .gitmodules
        # is an enumeration OMISSION, not a merely-informational note -
        # you cannot know what you failed to exclude, so the fingerprint
        # can never be trusted complete over whatever a submodule this
        # run could not identify may have folded into it.
        degraded = True
        problems.append(submodule_problem)

    def _record_exclusion(category: str, relative_path: str) -> None:
        exclusions[category] = exclusions.get(category, 0) + 1
        # FIX ROUND 16 (twelfth cold read, B2 BLOCKER, part 2): a bare
        # count-only record hid WHICH path was excluded and WHY - the
        # same "excluded roots with an explicit boundary reason" gap the
        # design's own scan.json fields already name, and the same
        # discipline `boundaries` already follows. Bounded at publish
        # time (scan_pipeline.py), the same way `boundaries` already is.
        excluded_roots.append({"path": relative_path, "category": category})

    def _walk(directory: Path, depth: int = 0) -> None:
        nonlocal entry_count, hashed_total, degraded, entry_cap_hit, excluded_region_may_contain_target
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
                # M-3 (third cold read, fix round 5): never str(exc) - an
                # OSError's own string embeds its ABSOLUTE filename.
                "detail": bounded_os_error_detail("directory could not be listed", exc),
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
                if boundary_kind == "unverifiable":
                    # N3 (seventh cold read, fix round 11): could not
                    # stat this entry to check whether it is a Windows
                    # directory junction - recorded as a boundary (never
                    # entered) AND a named problem, since an unknown
                    # entry is an enumeration omission the fingerprint
                    # can never be trusted complete over.
                    degraded = True
                    problems.append({
                        "reason_code": "parse_failed", "path": relative,
                        "detail": "could not stat this entry to check whether it is a "
                                  "directory junction - excluded rather than risk "
                                  "following one outside the root",
                    })
                boundaries.append(BoundaryEntry(relative_path=relative, boundary_kind=boundary_kind))
                continue
            if entry.is_dir():
                category = _exclusion_category(entry.name, relative, is_dir=True)
                if category is not None:
                    _record_exclusion(category, relative)
                    # FIX ROUND 19 (fifteenth cold read, F4 MAJOR, wrong-
                    # data): a generated/vendor-NAMED directory that
                    # sits under an uncarved bare `src/` root (the Ant
                    # residual round 18 accepted, then measured wider
                    # than accepted) can swallow real, hand-written
                    # code - never a real build-output tree, which has
                    # no `src` segment in its own path at all and stays
                    # silent, unaffected.
                    #
                    # FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR,
                    # wrong-data - the POISON RULE): the peek itself now
                    # runs for EVERY generated/vendor exclusion, not just
                    # ones under an uncarved src ancestor - the mainstream
                    # Maven vendored-module shape (``vendor/<module>/
                    # src/main/java/...``) has NO src segment in the
                    # excluded root's OWN path (only arbitrarily deeper,
                    # never recorded), so F4's own narrow boundary alone
                    # can never see it. Scoped to the generated_or_vendor
                    # CATEGORY specifically (never dependency-cache/vcs/
                    # hard-excluded, which structurally can never hold
                    # first-party code by the SAME reasoning discovery
                    # already excludes them by name without walking, and
                    # peeking a real node_modules/.m2 would be both
                    # expensive and pointless). F4's OWN degradation
                    # STAYS gated to the ratified src-ancestry boundary
                    # (unchanged, below) - only the run-wide POISON flag
                    # (consumed by dependencies_artifact.py to decide
                    # whether a registry miss may ever publish a
                    # confident external claim) widens to run-wide.
                    if category == "generated_or_vendor":
                        contains_code, peek_truncated = (
                            _excluded_directory_contains_a_code_bearing_file(entry)
                        )
                        if contains_code or peek_truncated:
                            excluded_region_may_contain_target = True
                            poisoning_excluded_roots.append({
                                "path": relative,
                                "trigger": "peek_positive" if contains_code else "peek_truncated",
                            })
                        if _sits_under_an_uncarved_src_segment(relative):
                            if contains_code:
                                degraded = True
                                problems.append({
                                    "reason_code": "excluded_region_contains_code",
                                    "path": relative,
                                    "detail": "a generated/vendor-named directory nested under "
                                              "an unrecognized bare src/ root contains at least "
                                              "one adapter-handled or tier-2 code-bearing file - "
                                              "excluded from the inventory as if it were build "
                                              "output, but this content is genuinely unread code",
                                })
                            elif peek_truncated:
                                # FIX ROUND 19b (reviewer-3's rejection of
                                # round 19, THE MAJOR, wrong-data): the peek
                                # hit its own entry cap before finding (or
                                # ruling out) a code-bearing file - an
                                # honestly UNKNOWN outcome, never silently
                                # folded into the same confident "no code"
                                # answer a fully-explored, genuinely code-
                                # free directory gets. Degrades, the same
                                # "record the truncation" discipline every
                                # other cap in this module already follows.
                                degraded = True
                                problems.append({
                                    "reason_code": "excluded_region_peek_truncated",
                                    "path": relative,
                                    "detail": "a generated/vendor-named directory nested under "
                                              "an unrecognized bare src/ root exceeded this "
                                              f"run's {_MAX_EXCLUDED_DIRECTORY_PEEK_ENTRIES}-"
                                              "entry peek cap before a code-bearing file could "
                                              "be confirmed present or absent - not confidently "
                                              "either",
                                })
                    continue
                if relative in submodule_boundaries:
                    boundaries.append(
                        BoundaryEntry(relative_path=relative, boundary_kind="submodule"))
                    continue
                _walk(entry, depth + 1)
                continue
            # A regular file.
            category = _exclusion_category(entry.name, relative, is_dir=False)
            if category is not None:
                _record_exclusion(category, relative)
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
                    "reason_code": "parse_failed", "path": relative,
                    "detail": bounded_os_error_detail("could not stat the file", exc),
                })
                continue
            # The per-file size cap is checked from stat() ALONE, before any
            # content is read - a large binary must trip this, never be
            # silently removed by a later binary-sniff exclusion first.
            if size > MAX_PER_FILE_BYTES:
                degraded = True
                _record_exclusion("resource_limit_oversized", relative)
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
                    "reason_code": "parse_failed", "path": relative,
                    "detail": bounded_os_error_detail("could not read the file's bytes", exc),
                })
                continue
            if _looks_binary(data):
                _record_exclusion("binary", relative)
                continue
            if hashed_total + len(data) > MAX_HASHED_TOTAL_BYTES:
                degraded = True
                _record_exclusion("resource_limit_total_bytes", relative)
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
        excluded_roots=excluded_roots,
        problems=problems,
        whole_scope_fingerprint=fingerprint,
        fingerprint_complete=fingerprint_complete,
        degraded=degraded,
        excluded_region_may_contain_target=excluded_region_may_contain_target,
        poisoning_excluded_roots=poisoning_excluded_roots,
    )
