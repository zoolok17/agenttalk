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

FIX ROUND 29 (twenty-fifth cold read, F3 MAJOR, completeness): the design
distinguishes hard-excluded content (secret/VCS/cache - "not read or
copied into the fingerprint") from the wider set that still contributes to
whole-scope freshness tracking even when not adapter-addressable. This
module used to treat every default-exclude category uniformly (tallied by
category+count, never in the whole-scope fingerprint's per-entry inputs at
all) - a real gap, measured: a changed binary-excluded file left the
fingerprint unchanged while the same run published its own changed
content_digest in modules.json. Widened now: `binary`/`generated_or_
vendor`/`resource_limit_oversized`/`resource_limit_total_bytes` each join
the fingerprint via their own `excluded_roots` entry (path + category +
content_digest when one is already in hand, `None` when computing one
would require re-reading bytes this producer deliberately never reads at
all - see `enumerate_scope`'s own fingerprint-assembly comment for the
per-category detail). `secret`/`vcs`/`dependency_cache`/`hard_excluded`
remain OUT of the fingerprint entirely, unchanged - genuinely never read
or copied, for confidentiality (secret) and cost (VCS/dependency-cache
directories) reasons the design's own wording already names.

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
import subprocess  # nosec B404 - invokes the real git binary to parse .gitmodules; no shell, no untrusted input
from dataclasses import dataclass, field
from pathlib import Path

from .errors import bounded_detail, bounded_os_error_detail
from .paths import RELATIVE_COMPREHENSION_DIR

#: FIX ROUND 47 (forty-first cold read, B1 BLOCKER): the SAME timeout
#: value privacy.py's own ``_run_git`` already uses for every git
#: invocation - duplicated rather than imported (this module owns
#: filesystem/subprocess access for the discovery stage; privacy.py is
#: a later-stage enforcement concern that never imports discovery.py
#: either - the same accepted, hand-synced-duplication shape this
#: package already carries for ``_TEST_SOURCE_ROOT_SEGMENT``).
_GIT_CONFIG_TIMEOUT_SECONDS = 2.0

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
#: FIX ROUND 48 (forty-second cold read, F1 BLOCKER - THE STRUCTURAL
#: FIX): the round-20 poison gate used to be ``if category ==
#: "generated_or_vendor":`` - an enumerated equality naming exactly one
#: directory-exclusion category, silently blind to a category added
#: later (round 47's own "secret" directory-name matching). Inverted:
#: this is the small, CLOSED set of categories that structurally can
#: NEVER hold first-party code (excluded by directory NAME alone,
#: never walked or peeked, by construction of `_exclusion_category`
#: itself) - every other directory-exclusion category is poison-
#: eligible BY DEFAULT, so a future widened category never has to be
#: separately remembered here. `hard_excluded` (`.git`/`.agenttalk`)
#: and `vcs` (`.hg`/`.svn`) are VCS/tooling-internal state, never a
#: package a build could ever place first-party source under; `
#: dependency_cache` (`node_modules`/`.m2`/etc) is third-party-package-
#: manager state by construction of the directory NAME itself - peeking
#: any of the three would be both expensive and pointless.
_DIRECTORY_CATEGORIES_THAT_CANNOT_HIDE_FIRST_PARTY_CODE = frozenset({
    "hard_excluded", "vcs", "dependency_cache",
})
#: FIX ROUND 37 (thirty-first cold read, F9 LOW, carry - folded into F2's
#: own case policy): unlike F2's own secret-file PATTERN matching (now
#: deliberately case-insensitive, identically on every platform - see
#: discovery._matches_any_secret_pattern), this directory-NAME set (and
#: its siblings _HARD_EXCLUDE_DIR_NAMES/_VCS_DIR_NAMES/_DEPENDENCY_
#: CACHE_DIR_NAMES above) is matched via a plain `name in {...}` test -
#: case-SENSITIVE on every platform, by construction of Python's own
#: `in` operator. This is a REAL, DECLARED asymmetry with F2's own
#: choice, not an oversight left unexamined: a directory literally
#: named "Target" (capitalized) is NOT recognized as generated/vendor
#: output, on Windows or Linux alike - unification with F2's own case-
#: insensitive policy is a real behavior change (widening exclusion
#: coverage for every one of these four directory-name sets at once)
#: out of scope for this LOW item. Covered by a direct unit test on the
#: predicate itself (explicit strings, no real filesystem) rather than
#: an end-to-end fixture - a real "Target"-vs-"target" DIRECTORY PAIR
#: cannot even be constructed on this dev host's own case-INSENSITIVE
#: NTFS filesystem to prove anything about real per-OS enumeration
#: order either way, the identical constraint round 36's own F4 secret-
#: pattern caveat already names for this exact class of check.
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
#:
#: DECLARED (round 21b, reviewer-3's re-delta, item 4(a)): the
#: ``startswith`` match below exempts a code-bearing file at ANY DEPTH
#: under a recognized position, not merely a direct child - a real,
#: accepted trade: first-party source someone deliberately (or
#: accidentally) parked several directories deep under a genuine
#: ``generated-sources/`` root goes UNDETECTED by this peek specifically.
#: The reactor rule (round 20's own M1+M2) is the only backstop left for
#: that shape - it still fires independently whenever a pom's own
#: declared ``<module>`` path resolves into the excluded region,
#: regardless of what this peek alone concluded.
_RECOGNIZED_GENERATED_OUTPUT_POSITIONS = (
    "generated-sources/", "generated-test-sources/", "generated/",
)


def _sits_under_a_recognized_generated_output_position(relative_to_excluded_root: str) -> bool:
    return relative_to_excluded_root.startswith(_RECOGNIZED_GENERATED_OUTPUT_POSITIONS)


#: FIX ROUND 32 (twenty-eighth cold read, F5 MAJOR, completeness): widened
#: from a battery that measured seven real, secret-shaped paths (a
#: ``.netrc``, an EXTENSION-form ``.env`` file like ``conf/app.env``/
#: ``conf/production.env`` - the pre-existing ``.env``/``.env.*`` entries
#: only ever matched a DOTFILE-style name, never this equally common
#: extension spelling - a Java keystore, a bare ``credentials`` file, a
#: ``secrets.properties``, and a PEM-family private key under a ``.key``
#: extension) leaking their PATH and content DIGEST (never the secret
#: VALUE itself - this producer never reads excluded content) because
#: none of them matched this closed pattern set and so fell through as an
#: ordinary discovered file. ``*.env``/``*.key``/``*.jks``/``*.keystore``/
#: ``*.p8``/``.netrc``/``credentials`` are added as new entries;
#: ``secrets.properties`` is added as an EXACT LITERAL rather than a
#: glob (``*secret*`` or similar) deliberately - a wildcard would also
#: swallow a harmless, unrelated file that merely mentions "secret" in
#: its name (a ``docs/secrets-rotation-policy.md``), which is a real
#: completeness cost (the safe direction for an actual secret is to
#: under-model it, but there is no reason to pay that cost for a file
#: that was never actually secret-shaped to begin with) for no matching
#: benefit, since the reader's own reproduction named this one specific,
#: well-known basename. PROVISIONAL, like every other closed-set
#: constant in this package - a genuinely secret-shaped basename absent
#: from this set still leaks path+digest, the same under-claim trade
#: struck everywhere else in this module.
#: FIX ROUND 35 (twenty-ninth cold read, F4 MINOR, completeness): a
#: further measured battery of seven canonical credential files (a
#: Postgres ``.pgpass``, a git credential store, Docker's own legacy
#: ``.dockercfg`` (embeds base64-encoded registry auth), Apache's
#: ``.htpasswd``, an npm ``.npmrc`` (can carry a registry auth token), a
#: ``secrets.yaml``, and a Spring Boot ``application-secret.properties``)
#: leaked path+digest the same way round 32's own battery did. ``.pgpass``/
#: ``.git-credentials``/``.dockercfg``/``.htpasswd``/``.npmrc`` are added
#: as new literal entries; ``application-secret.properties`` is added as
#: an exact literal, the same conservative choice round 32 made for the
#: identical-shaped ``secrets.properties``.
#: FIX ROUND 37 (thirty-first cold read, F2 BLOCKER, wrong-data): round
#: 35's own ``secrets.*`` GLOB was measured matching FAR wider than its
#: own docstring claimed - ``fnmatch.fnmatch`` applies ``os.path.
#: normcase``, case-INSENSITIVE on Windows, so ``secrets.*`` (meant to
#: catch ``secrets.yaml``/``secrets.json``/``secrets.properties``) also
#: matched ``Secrets.java`` - a real, parseable, adapter-handled JAVA
#: SOURCE FILE, silently dropped as category "secret" (its own GET route
#: vanished, complete/0 problems - worse than the tier-2 standard, where
#: an unparseable code file at least degrades VISIBLY). Narrowed from an
#: open glob to a CLOSED, explicit extension list - every real-world
#: "secrets.<config-format>" shape this producer has actually measured,
#: never a bare ``*`` that can suffix-match an arbitrary code extension.
#: FIX ROUND 41 (thirty-fifth cold read, F7 POLISH, completeness): the
#: bare ``credentials`` literal (an AWS-CLI-style ``~/.aws/credentials``,
#: no extension) was already closed, but the equally common
#: ``credentials.json`` shape (a downloaded Google Cloud service-account
#: key or OAuth client-secret file) was not - the same class of gap
#: round 32/35 each closed for a different basename. Added as an EXACT
#: LITERAL, the SAME round-37 calibration already applied to every
#: other entry here (never a ``credentials.*`` glob, which would also
#: swallow an unrelated, real ``credentials.py``/``credentials.go``
#: source file the way ``secrets.*`` swallowed ``Secrets.java``).
_SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.pfx", "*.p12", "*.ppk",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "*.key", "*.jks", "*.keystore", "*.p8", ".netrc", "credentials", "credentials.json",
    "secrets.properties", "secrets.yaml", "secrets.yml", "secrets.json", "secrets.xml",
    ".pgpass", ".git-credentials", ".dockercfg", ".htpasswd", ".npmrc",
    "application-secret.properties",
)
#: FIX ROUND 37 (F2 BLOCKER, part 2 - the case policy): ``fnmatch.
#: fnmatch``'s own case sensitivity is a PLATFORM FACT
#: (``os.path.normcase``), not a decision this producer ever made - the
#: SAME pattern set matched case-insensitively on Windows and case-
#: sensitively on Linux/macOS, an inventory divergence across platforms
#: for the identical repository and the identical rule set (the reader's
#: own F9 note). Decided explicitly here, deliberately, the SAME on every
#: platform: secret-pattern matching is case-INSENSITIVE (a credential
#: file named ``ID_RSA`` or ``SECRETS.YAML`` is exactly as sensitive as
#: its lowercase spelling, and the safe direction for a security-relevant
#: exclusion is to exclude MORE, not fewer, case spellings) - via
#: ``fnmatch.fnmatchcase`` (never applies any platform normcase) over an
#: explicit ``casefold()`` of both the name and the pattern, so the
#: SAME two strings compare identically regardless of which OS is
#: running this code.
def _matches_any_secret_pattern(name: str) -> bool:
    folded = name.casefold()
    return any(
        fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in _SECRET_FILE_PATTERNS
    )


#: FIX ROUND 37 (F2 BLOCKER, part 3 - the calibration rule): kept in
#: sync by hand with worker.py's own ``_ADAPTER_EXTENSIONS`` (discovery.py
#: cannot import worker.py - see ``_DEGRADABLE_EXCLUDED_EXTENSIONS``'s own
#: comment for why). Deliberately NARROWER than that set: genuinely
#: adapter-PARSED extensions only (this producer actually reads and
#: extracts from these), not every recognized-but-unmodeled tier-2 shape
#: - a secret-pattern hit on one of THESE extensions discards real,
#: parseable inventory the run would otherwise have understood, the
#: exact "worse than tier-2" shape F2 measured: an unparseable code file
#: at least degrades visibly; a parseable one must not vanish silently
#: just because its own name happens to collide with a secret-shaped
#: pattern.
_ADAPTER_HANDLED_EXTENSIONS_FOR_SECRET_CALIBRATION = frozenset({".java"})
#: FIX ROUND 38 (thirty-second cold read, F4 MINOR, wrong-data): the
#: calibration above was ``.java``-only, but the closed secrets list's
#: OWN literal members reach past ``.java`` - ``secrets.xml`` is one of
#: its eleven exact-literal entries, and a Spring beans root (or any
#: other genuinely code-bearing XML/properties/YAML/JSON descriptor)
#: sharing that exact name is dropped category=secret with a
#: COMPLETE/0-problem run, while a byte-equivalent ``beans.xml`` (no
#: name collision) degrades via the established root-sniff/tier
#: machinery - the identical epistemic gap F2 already closed for
#: ``.java``, unclosed for the rest of the closed list's own
#: potentially-code-bearing members, so ``SECRET_PATTERNS_CAVEAT``'s own
#: "never silently" sentence was not actually true for them.
#:
#: Deliberately a SEPARATE, narrower-consequence set from the one
#: above: unlike ``.java`` (unambiguously a real compilation unit by
#: extension alone - this producer parses every one it can read), this
#: producer excludes a secret-pattern hit PRE-READ, so whether one of
#: these five extensions is genuinely code-bearing (a Spring beans XML
#: root, an actually-Java `application.properties`) or merely secret-
#: shaped config this run correctly never needed to see is UNKNOWABLE
#: without reading content this exclusion rule exists specifically to
#: never read (the round 26b precedent: an excluded file's own tier is
#: unknowable pre-read). Recorded (never silently), but NOT degrading -
#: "record, don't guess," the same disposition round 26b's own
#: ``binary_excluded_root_sniffed_xml`` established for the identical
#: "could be code, could be ordinary, cannot tell without reading"
#: shape.
_POTENTIALLY_CODE_BEARING_EXTENSIONS_FOR_SECRET_CALIBRATION = frozenset({
    ".xml", ".properties", ".yaml", ".yml", ".json",
})
#: FIX ROUND 35 (F4 MINOR, completeness): the provisional-set caveat
#: this closed list has always deserved but never published in-artifact
#: (declared only in the source comments above) - the same *_CAVEAT
#: discipline every other provisional set in this package already
#: follows (see FINGERPRINT_CAVEAT).
SECRET_PATTERNS_CAVEAT = (
    "the secret-file exclusion list is a closed, PROVISIONAL basename/"  # noqa: S105  # nosec B105 - a prose caveat string, not a credential
    "extension set (see discovery._SECRET_FILE_PATTERNS), grown battery-"
    "by-battery as real secret-shaped paths are measured leaking - it is "
    "not, and does not claim to be, an exhaustive catalogue of every "
    "credential-file convention. A genuinely secret-shaped path absent "
    "from this set is not excluded: its path and content digest (never "
    "its value - this producer never reads excluded content either way) "
    "are published as an ordinary discovered file, the same safe-"
    "direction-to-be-wrong-in trade every other provisional set in this "
    "package accepts. Matching is deliberately case-insensitive on every "
    "platform (a credential file named in any letter case is equally "
    "sensitive). The OTHER direction also exists and is declared here "
    "too (FIX ROUND 37, F2 BLOCKER): a real, parseable, adapter-handled "
    "source file whose OWN name happens to collide with one of these "
    "patterns is excluded the same as a genuine secret would be, but "
    "never silently - a secret_pattern_matched_code_bearing_file problem "
    "is recorded and the run degrades, naming exactly which adapter-"
    "handled file this exclusion list discarded. Widened (FIX ROUND 38, "
    "F4 MINOR): the identical visibility now also covers a secret-"
    "excluded file whose extension is one of this list's OWN other "
    "potentially-code-bearing members (.xml/.properties/.yaml/.yml/"
    ".json - secrets.xml among the literal entries above) - recorded the "
    "same way, but NOT degrading, since (unlike a .java file) this "
    "producer never reads excluded content and genuinely cannot tell "
    "whether one of these five extensions was real, code-bearing "
    "content or ordinary config without reading what this exclusion "
    "rule exists specifically to never read. Widened (FIX ROUND 47, "
    "completeness): secret-pattern matching also governs DIRECTORY "
    "names, not just files - a directory literally named .env or "
    "credentials is excluded, subtree and all, the same safe direction. "
    "A secret-shaped directory name sitting inside a recognized source "
    "root (src/main/java and its siblings) is exempted first (FIX ROUND "
    "48, the round-16 pattern: a plausible domain package coincidentally "
    "matching a secret pattern, e.g. com/ex/credentials, is real source, "
    "not a credentials store) - a directory genuinely excluded as "
    "secret is peeked the same way a generated/vendor exclusion already "
    "is: code-bearing content found there both poisons this run's own "
    "externality confidence run-wide and, when it sits under an "
    "unrecognized bare src/ root, degrades the run visibly "
    "(excluded_region_contains_code/excluded_region_peek_truncated) - "
    "never a silent, confident external claim for what is actually "
    "unscanned first-party code."
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
        # M (cold-read PR-B fix round 47 completeness): secret-pattern
        # matching used to apply to FILES only - a directory literally
        # named ``.env`` (a real, if less common, convention: a whole
        # directory of per-environment secret files, e.g. ``.env/
        # production``) walked its children uninhibited, since this
        # branch returned before ever consulting
        # _matches_any_secret_pattern. The SAME closed, casefolded
        # pattern set already governs files - reused here unchanged (no
        # second, parallel notion of "secret-shaped"), so a secret-shaped
        # directory name is excluded, subtree and all, the same safe
        # direction this producer already takes for a secret-shaped file.
        #
        # FIX ROUND 48 (forty-second cold read, F1 BLOCKER, wrong-data -
        # .cr42-secretdir): unlike the generated/vendor directory-name
        # check three lines above, this had NO ``_is_inside_a_recognized_
        # source_root`` guard - the round-16 pattern, applied here for
        # the first time. Several of `_SECRET_FILE_PATTERNS`'s own
        # entries (``credentials``, in particular) are also plausible
        # ordinary Java package segments (``com/ex/credentials``) - one
        # sitting inside an established ``src/main/java/...`` tree is
        # real, hand-written source, not a credentials store, the exact
        # "domain package coincidentally named like the exclusion
        # category" shape round 16 already fixed once for generated/
        # vendor names. Without this guard, that package silently
        # vanished from the inventory AND from externality-safety at
        # once: with no code-bearing/poison signal for the "secret"
        # category at all (see the poison-gate fix below), an import
        # resolving into it published a CONFIDENT third-party dependency
        # for what was actually unscanned first-party code.
        if _matches_any_secret_pattern(name) and not _is_inside_a_recognized_source_root(
            relative_path,
        ):
            return "secret"
        return None
    if _matches_any_secret_pattern(name):
        return "secret"
    return None


def _submodule_boundary_paths(root: Path) -> tuple[frozenset[str], dict[str, str] | None]:
    """Every declared ``submodule.<name>.path`` value from ``.gitmodules``,
    POSIX-spelled - the boundary PATHS needed to satisfy "submodules are
    external boundaries unless explicitly included" (design). Full git-
    submodule semantics (nested configs, URL rewriting) stay out of scope.

    FIX ROUND 47 (forty-first cold read, B1 BLOCKER, wrong-data - THE
    WORST FAILURE SHAPE): this used to be a hand-rolled plain-text parse
    (``key.strip() == "path"``) with no section scope and no value
    unquoting - genuinely a DIFFERENT, incompatible grammar from git's
    own config-file format, in both directions. (a) FABRICATION: a
    ``[core]`` block's own ``path = svc`` key (a real, common
    ``.gitmodules``-adjacent shape - nothing stops an operator from
    hand-editing stray config into this file) matched the same bare
    ``key == "path"`` check with NO section awareness, inventing a
    submodule boundary at ``svc/`` that git itself never recognizes at
    all (``git config -f --list`` reads it as ``core.path``, not
    ``submodule.*.path``) - the REAL module ``svc/`` was silently
    DELETED from the inventory on a complete/zero-problem run. (b)
    LEAKAGE (the mirror direction): a quoted path (``path = "libs/foo"``),
    a trailing slash, or a trailing inline comment are all read by git as
    the real submodule path, but the old hand-rolled parse's own naive
    ``strip()`` either left the surrounding quotes/comment IN the value
    (never matching the real on-disk directory, so the exclusion silently
    never applied) or split on the wrong boundary - a genuine external
    submodule's own source then published as first-party units.

    FIXED per the reader's own prescription: delegate to
    ``git config -f <file> --list`` - the real git config parser (the
    same binary privacy.py already shells out to, for the identical
    reason: guessing at git's own file format from first principles is
    exactly the class of defect this fix retires). Unquoting, comment
    stripping, and section scoping (restricting to ``submodule.*.path``
    keys specifically) all come free from git's own parser - there is no
    longer a second, hand-maintained grammar to keep in sync with git's
    real one.

    ``git`` ABSENT or ERRORING (a real, if rare, possibility - unlike the
    privacy preflight's own VCS-worktree check, this call needs only the
    ``git`` BINARY on PATH, never a real ``.git`` worktree at ``root``,
    since ``-f <file>`` reads the given file directly) is treated with
    the SAME fail-open discipline this function already established for
    an unreadable/undecodable file (N2/round 26b, below) - a declared,
    degrading ``parse_failed`` problem, never a silent empty boundary
    set. This is a DIFFERENT question from whether ``root`` itself is a
    git worktree (the privacy preflight's own ``no_vcs_acknowledged``
    disposition already covers that, upstream of this call, and does not
    require git to be resolvable there either) - reusing that path here
    would conflate two independent facts ("is this root under git" vs.
    "can this one config file be parsed"), so this function keeps its
    own narrow, already-established fail-open shape instead.

    Returns ``(paths, problem)`` - ``problem`` is ``None`` on success
    (including the ordinary "no ``.gitmodules`` file" case, never a
    failure), or a problem dict the caller must record (marking the
    fingerprint incomplete) otherwise."""
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return frozenset(), None
    try:
        result = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "config", "-f", str(gitmodules), "--list", "-z"],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_CONFIG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return frozenset(), {
            "reason_code": "parse_failed",
            "path": ".gitmodules",
            "detail": bounded_os_error_detail(
                "could not invoke git to parse .gitmodules - submodule boundaries are "
                "unknown, so none could be excluded", exc),
        }
    if result.returncode != 0:
        return frozenset(), {
            "reason_code": "parse_failed",
            "path": ".gitmodules",
            "detail": bounded_detail(
                "git config -f .gitmodules --list exited "
                f"{result.returncode} - submodule boundaries are unknown, so none could "
                "be excluded"),
        }
    paths: set[str] = set()
    # FIX ROUND 48 (forty-second cold read, N1, judged - taken): a plain
    # (non-``-z``) ``--list`` line is ``key=value``, split via
    # ``line.partition("=")`` - a real, legal git-config subsection name
    # containing its OWN literal ``=`` (``[submodule "a=b"]``) makes git
    # emit ``submodule.a=b.path=libs/foo`` as ONE line with TWO ``=``
    # characters; partitioning on the FIRST one splits it as
    # key=``submodule.a``, value=``b.path=libs/foo`` - the key no longer
    # ends with ``.path``, so this genuine submodule path is silently
    # NEVER recognized, reopening the exact LEAKAGE direction round 47's
    # own fix closed (a real external submodule's own source published
    # as first-party). ``-z`` (the same NUL-safe idiom round 47's own
    # `_check_ignore_one` fix already established for `git check-ignore`)
    # makes this unambiguous regardless of what the value OR the key
    # itself contains: each entry is NUL-terminated, and the key/value
    # split within an entry is on the first NEWLINE - a character that
    # can never appear in a git config KEY - never on `=`, which very
    # much can.
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        key, _sep, value = entry.partition("\n")
        if key.startswith("submodule.") and key.endswith(".path"):
            # A trailing slash (`libs/foo/`, a real, legal git-config
            # spelling) is otherwise never produced by the enumerator's
            # own `relative` paths this set is exact-matched against
            # below - stripped here so it still excludes the real
            # directory rather than silently leaking it.
            value = value.strip().replace("\\", "/").rstrip("/")
            if value:
                paths.add(value)
    return frozenset(paths), None


#: FIX ROUND 30 (twenty-sixth cold read, F3 MINOR, completeness): round
#: 29's own F3 fix (see ``enumerate_scope``'s own fingerprint-assembly
#: comment below) widened ``whole_scope_fingerprint`` to cover every
#: non-hard-excluded category via its own ``content_digest`` when one is
#: already in hand - but ``generated_or_vendor``/``resource_limit_
#: oversized`` deliberately carry ``None`` instead (reading a skipped
#: directory's own bytes, or a file excluded by size BEFORE any read at
#: all, would defeat the entire point of skipping it). The SEMANTICS
#: were already ratified (reviewer-3's round 29 R3 delta) - what was
#: missing is the PUBLISHED declaration: a caller reading `scan.json`
#: alone, with `fingerprint_complete: true`, had no way to discover
#: that a content change entirely inside an already-excluded
#: generated/vendor or dependency-cache tree leaves the fingerprint
#: byte-identical. Declared here, the same "declare the gap" idiom
#: ``ASSESSMENT_STATE_CAVEAT``/``PROVENANCE_CAVEAT``/``CLASSIFICATION_
#: CAVEAT``/``FEATURES_STRUCTURAL_CAVEAT`` already establish for their
#: own artifacts - published unconditionally in scan.json, never only
#: in this module's own comment.
#:
#: MICRO-ROUND 30b (reviewer-3 delta on round 30's own F3, R3, wrong-
#: data - correct before merge, a caveat exists to be trusted): the
#: FIRST version of this caveat claimed ``dependency_cache`` (alongside
#: secret/vcs/hard_excluded) has "NO fingerprint sensitivity at all -
#: not even entry-level... never selected into the fingerprint
#: computation" - MEASURED FALSE. `exclusions` (the category -> count
#: tally, below) feeds `fingerprint_input` unconditionally, for EVERY
#: category, including these four - a `node_modules` tree appearing or
#: disappearing changes its own category's count, which changes the
#: fingerprint (measured both directions). The MECHANISM claim (no
#: per-entry path/digest ever joins the fingerprint for these four
#: categories) was always right; only the BEHAVIOR claim (therefore no
#: sensitivity at all) was wrong - a category's own AGGREGATE COUNT is
#: not "no sensitivity", it is a coarser one than generated_or_vendor's
#: own per-entry path tracking. Corrected below.
FINGERPRINT_CAVEAT = (
    "whole_scope_fingerprint's own sensitivity to an excluded region is "
    "ENTRY-LEVEL, not CONTENT-LEVEL, for the generated_or_vendor category - "
    "a directory or file appearing, disappearing, or being excluded under a "
    "different category changes the fingerprint, but a content change (an "
    "added or modified file) entirely inside an already-excluded generated/"
    "vendor region does not, since no per-file bytes are ever read for it "
    "(reading them just to fingerprint them would defeat the entire point "
    "of skipping it). The dependency_cache category (alongside secret/vcs/"
    "hard-excluded) contributes no PER-ENTRY input (no individual path or "
    "digest joins the fingerprint) - but its own CATEGORY TALLY does: a "
    "whole region of one of these categories appearing or disappearing "
    "changes the count recorded under that category, which does change the "
    "fingerprint; only a content change (or a path change that leaves the "
    "count unchanged) entirely inside an already-excluded region of one of "
    "these categories never does. The binary category is NOT affected by "
    "either gap - it already reads the file's own bytes before excluding "
    "it, so its own content_digest already makes the fingerprint sensitive "
    "to a content change there.\n\n"
    "FIX ROUND 47 (forty-first cold read, M7 MAJOR, corrected): the "
    "resource_limit_oversized and resource_limit_total_bytes categories "
    "were PREVIOUSLY described here as having their own fingerprint-"
    "sensitivity granularity too (entry-level for the former, content-level "
    "for the latter) - both claims describe DEAD CODE that can never "
    "actually execute. Both categories ALWAYS record a genuinely degrading "
    "problem (`degrades_run: True`) at the exact same call site as their "
    "own exclusion - fingerprint_complete is False, and whole_scope_"
    "fingerprint is None, unconditionally, for the ENTIRE run whenever "
    "either category has even one entry; the fingerprint is never "
    "partially or coarsely computed for a run containing one, only ever "
    "entirely absent. A caller reading fingerprint_complete: false and "
    "whole_scope_fingerprint: null already sees this honestly - this "
    "caveat's own job (an ENTRY-LEVEL-vs-CONTENT-LEVEL granularity "
    "question) simply does not arise for either resource_limit category."
)


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

    def _record_exclusion(
        category: str, relative_path: str, *, content_digest: str | None = None,
    ) -> None:
        exclusions[category] = exclusions.get(category, 0) + 1
        # FIX ROUND 16 (twelfth cold read, B2 BLOCKER, part 2): a bare
        # count-only record hid WHICH path was excluded and WHY - the
        # same "excluded roots with an explicit boundary reason" gap the
        # design's own scan.json fields already name, and the same
        # discipline `boundaries` already follows. Bounded at publish
        # time (scan_pipeline.py), the same way `boundaries` already is.
        entry = {"path": relative_path, "category": category}
        # MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R2, wrong-data):
        # a binary-excluded file's own bytes are already in hand at the
        # ONE call site that passes this (the binary-sniff exclusion,
        # below) - `content_digest` is carried through here (never
        # recomputed) so scan_pipeline.py can synthesize a real
        # modules.json unit for a root-sniffed-XML binary exclusion the
        # same "record, don't vanish" way it already does for an
        # encoding-undecodable twin, without discovery.py needing to
        # know anything about XML/root-sniffing itself - discovery.py
        # cannot import worker.py (the reverse import already exists,
        # and discovery.py owns filesystem access exclusively), the
        # same constraint the existing `_DEGRADABLE_EXCLUDED_EXTENSIONS`
        # carry (Named decisions and residuals) already documents.
        # Absent (never null) for every OTHER exclusion category, the
        # same absent-not-null idiom this artifact family already
        # follows for an optional field.
        if content_digest is not None:
            entry["content_digest"] = content_digest
        excluded_roots.append(entry)

    def _walk(directory: Path, depth: int = 0) -> None:
        nonlocal entry_count, hashed_total, degraded, entry_cap_hit, excluded_region_may_contain_target
        if depth > MAX_NESTING_DEPTH:
            degraded = True
            problems.append({
                "reason_code": "resource_limit",
                "path": directory.relative_to(root).as_posix(),
                "detail": f"exceeded the {MAX_NESTING_DEPTH}-level nesting cap",
                "degrades_run": True,
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
                "degrades_run": True,
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
                    "degrades_run": True,
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
                        "degrades_run": True,
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
                    # can never see it. F4's OWN degradation STAYS gated
                    # to the ratified src-ancestry boundary (unchanged,
                    # below) - only the run-wide POISON flag (consumed by
                    # dependencies_artifact.py to decide whether a
                    # registry miss may ever publish a confident external
                    # claim) widens to run-wide.
                    #
                    # FIX ROUND 48 (forty-second cold read, F1 BLOCKER,
                    # wrong-data - THE STRUCTURAL FIX, .cr42-secretdir):
                    # this used to be ``if category ==
                    # "generated_or_vendor":`` - an ENUMERATED EQUALITY
                    # naming exactly one directory-exclusion category.
                    # Round 47 widened secret-pattern matching to
                    # directory names (a genuinely new directory-
                    # exclusion category) without ever touching this
                    # gate - a secret-shaped directory hiding real code
                    # got NEITHER the poison flag NOR a degrading
                    # problem, so an import resolving into it published
                    # a CONFIDENT third-party dependency for what was
                    # actually unscanned first-party code, directly
                    # contradicting SECRET_PATTERNS_CAVEAT's own promise
                    # ("never silently... recorded and the run
                    # degrades"). Inverted to a PROPERTY of the category
                    # instead: ``_DIRECTORY_CATEGORIES_THAT_CANNOT_HIDE_
                    # FIRST_PARTY_CODE`` names the SMALL, closed set that
                    # structurally never holds first-party code (vcs/
                    # dependency-cache/hard-excluded - excluded by NAME
                    # alone, never peeked, the reasoning this code's own
                    # comment already stated) - every OTHER directory-
                    # exclusion category (generated_or_vendor, secret,
                    # and any future one) is poison-eligible BY DEFAULT,
                    # so the NEXT widened category can never repeat this
                    # silently.
                    if category not in _DIRECTORY_CATEGORIES_THAT_CANNOT_HIDE_FIRST_PARTY_CODE:
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
                                    "detail": f"a {category!r}-category excluded directory "
                                              "nested under an unrecognized bare src/ root "
                                              "contains at least one adapter-handled or tier-2 "
                                              "code-bearing file - excluded from the inventory "
                                              "as if it could never hold first-party code, but "
                                              "this content is genuinely unread code",
                                    "degrades_run": True,
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
                                    "detail": f"a {category!r}-category excluded directory "
                                              "nested under an unrecognized bare src/ root "
                                              "exceeded this run's "
                                              f"{_MAX_EXCLUDED_DIRECTORY_PEEK_ENTRIES}-entry "
                                              "peek cap before a code-bearing file could be "
                                              "confirmed present or absent - not confidently "
                                              "either",
                                    "degrades_run": True,
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
                # FIX ROUND 37 (thirty-first cold read, F2 BLOCKER, part
                # 3 - THE CALIBRATION RULE): a secret-pattern hit on a
                # genuinely adapter-handled extension discards real,
                # parseable inventory - worse than the tier-2 standard
                # (an unparseable code file at least degrades visibly).
                # Still excluded (safe direction: never read a file this
                # producer cannot rule out is a real secret), but never
                # silently - recorded and degrading, naming exactly which
                # file this exclusion list discarded.
                if category == "secret" and entry.name.lower().endswith(
                    tuple(_ADAPTER_HANDLED_EXTENSIONS_FOR_SECRET_CALIBRATION),
                ):
                    degraded = True
                    problems.append({
                        "reason_code": "secret_pattern_matched_code_bearing_file",
                        "path": relative,
                        "detail": f"{entry.name!r} matches this producer's own secret-file "
                                  "exclusion pattern set, but its extension is genuinely "
                                  "adapter-handled - excluded the same as a real secret would "
                                  "be (never read), but never silently: a parseable file this "
                                  "run could otherwise have understood is missing from the "
                                  "inventory because of this",
                        "degrades_run": True,
                    })
                # FIX ROUND 38 (F4 MINOR, wrong-data): the SAME visibility
                # for the closed list's OTHER potentially-code-bearing
                # members (secrets.xml et al) - never degrading, since
                # this file's own tier is genuinely unknowable pre-read
                # (see _POTENTIALLY_CODE_BEARING_EXTENSIONS_FOR_SECRET_
                # CALIBRATION's own docstring for why this is "record,
                # don't guess," not the .java case's "known code-bearing,
                # so degrade" disposition).
                elif category == "secret" and entry.name.lower().endswith(
                    tuple(_POTENTIALLY_CODE_BEARING_EXTENSIONS_FOR_SECRET_CALIBRATION),
                ):
                    problems.append({
                        "reason_code": "secret_pattern_matched_code_bearing_file",
                        "path": relative,
                        "detail": f"{entry.name!r} matches this producer's own secret-file "
                                  "exclusion pattern set, and its extension is one this "
                                  "producer sometimes finds genuinely code-bearing (a Spring "
                                  "beans XML root, an actually-parsed properties/YAML/JSON "
                                  "descriptor) - excluded the same as a real secret would be "
                                  "(never read, so its own tier is unknowable), but never "
                                  "silently: recorded, not degrading, since this run cannot "
                                  "tell whether this specific file was ordinary config or real "
                                  "inventory without reading content this rule exists to never "
                                  "read",
                        # FIX ROUND 39 (thirty-third cold read, F2 MAJOR,
                        # wrong-data - THE SELF-CONTRADICTION): this
                        # detail's own "not degrading" claim was defeated
                        # one layer up in scan_pipeline.py, which computed
                        # status/degraded_by from the bare TRUTHINESS of
                        # `discovery_result.problems` (any problem at all,
                        # never checking degrades_run) - so this run
                        # published status=degraded + degraded_by
                        # containing this exact reason_code, contradicting
                        # the caveat and this very sentence in the SAME
                        # run. `degrades_run` is now a real, per-problem
                        # published (internally - never serialized into
                        # problems.json itself, same as WorkerProblem's
                        # own field) flag scan_pipeline.py actually reads,
                        # the round-30 F5 lesson (per-instance flags are
                        # authoritative) applied to discovery's own
                        # problems too.
                        "degrades_run": False,
                    })
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
                    "degrades_run": True,
                })
                return
            try:
                size = entry.stat().st_size
            except OSError as exc:
                problems.append({
                    "reason_code": "parse_failed", "path": relative,
                    "detail": bounded_os_error_detail("could not stat the file", exc),
                    "degrades_run": True,
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
                    "degrades_run": True,
                })
                continue
            try:
                data = entry.read_bytes()
            except OSError as exc:
                problems.append({
                    "reason_code": "parse_failed", "path": relative,
                    "detail": bounded_os_error_detail("could not read the file's bytes", exc),
                    "degrades_run": True,
                })
                continue
            if _looks_binary(data):
                _record_exclusion(
                    "binary", relative, content_digest=hashlib.sha256(data).hexdigest())
                continue
            if hashed_total + len(data) > MAX_HASHED_TOTAL_BYTES:
                degraded = True
                # FIX ROUND 29 (twenty-fifth cold read, F3 MAJOR, wrong-
                # data): the bytes are ALREADY in hand here (`data`, read
                # just above) - the same "carry the digest, never re-read"
                # discipline round 28b's own binary-category fix already
                # established, now applied to this category too.
                _record_exclusion(
                    "resource_limit_total_bytes", relative,
                    content_digest=hashlib.sha256(data).hexdigest())
                problems.append({
                    "reason_code": "resource_limit",
                    "path": relative,
                    "detail": f"whole-scope hashed bytes would exceed the "
                              f"{MAX_HASHED_TOTAL_BYTES}-byte cap",
                    "degrades_run": True,
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
    #
    # FIX ROUND 47 (forty-first cold read, M3 MAJOR, wrong-data - THE
    # BARE-TRUTHINESS SIBLING): this was `not problems` - round 39's own
    # F2 exact class (scan_pipeline.py's own status computation used to
    # make the identical mistake, fixed there, never swept here too).
    # `secret_pattern_matched_code_bearing_file` (.cr41-secretxml) is
    # explicitly recorded `degrades_run: False` - a genuinely non-
    # degrading, purely informational problem (this run cannot tell
    # whether the excluded file was ordinary config or real inventory,
    # but that not-knowing itself is not evidence anything was actually
    # missed from the fingerprint) - yet its mere PRESENCE permanently
    # nulled the fingerprint (`freshness` stays `not_evaluated` forever
    # for this run, disqualifying it from any future "is this still
    # current" dispatch), while every OTHER discovery-level problem
    # recorded here IS a genuine walked-content omission and correctly
    # marks it incomplete. Derived from each problem's own `degrades_run`
    # flag now, the same per-instance-authoritative discipline round 39
    # already established - swept for any OTHER bare-truthiness
    # aggregate in this module and found none (every other boolean here -
    # `degraded`, `entry_cap_hit`, `excluded_region_may_contain_target` -
    # is already set explicitly, per-condition, never derived from a
    # bare collection-truthiness check).
    fingerprint_complete = not any(p.get("degrades_run", True) for p in problems)

    fingerprint = None
    if fingerprint_complete:
        # FIX ROUND 29 (twenty-fifth cold read, F3 MAJOR, completeness):
        # this module's own docstring names the design's requirement (the
        # fingerprint covers every NON-hard-excluded entry) and declared,
        # as a scope simplification, that every default-exclude category
        # was folded uniformly into the OTHER extreme instead - never in
        # the fingerprint at all, even for the categories that are
        # genuinely fine to include. Measured: a changed binary-excluded
        # file left the fingerprint unchanged while modules.json published
        # its changed source_digest - a real, silent gap, not a merely
        # theoretical one. Widened here rather than left declared: a
        # NON-hard-excluded category's own excluded_roots entries now
        # join the fingerprint too, each carrying its own content_digest
        # when one is already in hand (never re-read for this alone) -
        # ``binary`` (round 28b) and ``resource_limit_total_bytes``
        # (above, this round) both already read the file's bytes before
        # excluding it; ``generated_or_vendor`` (a directory-level skip,
        # no per-file bytes ever read at all - reading its own contents
        # just to fingerprint them would defeat the entire point of
        # skipping it) and ``resource_limit_oversized`` (excluded from
        # ``stat().st_size`` alone, BEFORE any read, by design - the
        # identical reasoning) both carry ``None`` instead: the fingerprint
        # is still sensitive to that PATH's own presence/category (a
        # generated/vendor tree appearing, disappearing, or being excluded
        # under a different category all change the fingerprint), just not
        # to a content change happening entirely inside an already-
        # excluded region - a named, accepted residual, not silently
        # unlimited. ``secret``/``vcs``/``dependency_cache``/
        # ``hard_excluded`` stay OUT of the fingerprint entirely,
        # unchanged - the design's own "not read or copied into the
        # fingerprint" wording for exactly these categories (secret files
        # for confidentiality; VCS/dependency-cache directories because
        # walking them at all would be both expensive and pointless).
        non_hard_excluded_categories = frozenset({
            "binary", "generated_or_vendor", "resource_limit_oversized",
            "resource_limit_total_bytes",
        })
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
            "non_hard_excluded": sorted(
                (entry["path"], entry["category"], entry.get("content_digest"))
                for entry in excluded_roots
                if entry["category"] in non_hard_excluded_categories
            ),
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
