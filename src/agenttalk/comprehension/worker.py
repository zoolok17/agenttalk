"""The sanitized bundled scanner worker (DESIGN-55-comprehension-plane.md,
"System boundary" / "Privacy and offline enforcement").

    The CLI starts one bundled scanner worker with a sanitized environment.
    Adapters run in-process inside that worker and receive only an
    allowlisted input object. [...] the CLI launches the bundled worker
    without provider credentials, proxy variables, remote endpoints, or
    inherited configuration that names a network service; sanitizing the
    child environment does not mutate the parent agenttalk process.

The worker runs as a genuinely separate OS process (``python -m
agenttalk.comprehension.worker``), launched with an ALLOWLISTED environment
built by :func:`sanitized_worker_env` - never the parent's own
``os.environ`` filtered down, an allowlist, matching this codebase's
existing convention (``dev_gate._base_env``) for the same reason: an
allowlist can only omit what nobody thought to add; a blocklist can only
omit what somebody remembered to name. Input (the resolved project root and
the already-enumerated, already-confined relative paths to process) travels
over stdin as one JSON object; output (each file's default addressable-unit
claim, and one problem record per file this worker could not process)
travels over stdout as one JSON object. Neither uses an environment
variable, a shared temp file outside the caller's own control, or any IPC
mechanism that could carry ambient configuration.

Adapters dispatch INSIDE this worker (item 9), on the same bytes already
read for the default file claim - never a second, separate read, and
never outside this sanitized process boundary. The Java adapter itself
(``adapters.java``) has no knowledge of the worker/subprocess boundary at
all; it is a pure function from (path, text) to claims, so it works
identically whether called here or directly in a unit test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - launches only the bundled worker module, argv fixed, shell disabled
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import java as java_adapter
from .envelope import EnvelopeError, resolve_under_root
from .errors import ComprehensionError, bounded_detail, bounded_os_error_detail

#: FIX ROUND 44 (thirty-eighth cold read, F2 MAJOR): a THIRD copy of the
#: same test-source-root predicate java.py's own module docstring
#: (near `_TEST_SOURCE_ROOT_SEGMENT`) and modules_artifact.py's own
#: `_default_classification` already carry - worker.py cannot import
#: either (this module IS the sanitized subprocess boundary those two
#: run as regular in-process code; discovery.py's own docstring already
#: names the identical constraint for the reverse direction). Accepted
#: the same way the existing two-copy duplication already is (see
#: "Named decisions and residuals") - a shared, lower-level module all
#: three could import from would close this properly, wider than this
#: fix.
_TEST_SOURCE_ROOT_SEGMENT = re.compile(r"(?:^|/)src/(?:test|it)/|^tests?/")


def _rel_is_under_deployment_root(rel: str, deployment_root: str) -> bool:
    """MICRO-ROUND 50 (Cluster 1, B2 BLOCKER): ``True`` iff ``rel`` (a
    ``.java`` file's own repo-relative path) sits inside the deployment
    ``deployment_root`` names - the directory directly containing that
    deployment's own ``WEB-INF/`` (see the metadata-complete pre-scan's
    own docstring). ``"."`` (``Path(...).as_posix()``'s own spelling for
    "no parent segment at all" - a top-level ``WEB-INF/web.xml``) means
    the WHOLE repo tree IS the one deployment, matching every path.
    Assumes deployment roots are DISJOINT sibling directories (the
    realistic multi-module-reactor shape - independent WAR modules never
    nest one inside another) rather than resolving a nested-root
    conflict; a future round can revisit if that assumption is ever
    measured false."""
    if deployment_root == ".":
        return True
    rel_posix = rel.replace("\\", "/")
    return rel_posix == deployment_root or rel_posix.startswith(f"{deployment_root}/")


#: MICRO-NOD 50c (F1): the WorkerProblem attribution for an annotation-
#: only descriptor-name conflict (see java_adapter.annotation_only_
#: descriptor_conflicts) - there is no single web.xml (or any other
#: file) to blame for a conflict between two ANNOTATIONS, the identical
#: "two real owners involved, no single one to anchor to" reasoning the
#: XML-anchored twin's own synthetic qualified_name already uses, one
#: level up (WorkerProblem.relative_path is a required, non-Optional
#: field, unlike JavaAdapterProblem.qualified_name - this sentinel is
#: never a real, reachable path on any platform).
_NO_DESCRIPTOR_SENTINEL_PATH = "<no descriptor>"
_ADAPTER_EXTENSIONS = {".java": java_adapter}
_ADAPTER_HANDLED_XML_BASENAMES = frozenset({"pom.xml", "web.xml"})
#: FIX ROUND 16 (twelfth cold read, B4 BLOCKER, wrong-data): INVERTED
#: from a closed CODE-extension allowlist (the previous
#: ``_UNSUPPORTED_LANGUAGE_EXTENSIONS`` - recognizing only
#: .jsp/.jspx/.properties/.sql, plus non-adapter-handled .xml, as
#: "unsupported but worth naming") to a closed BENIGN-extension
#: allowlist. The old direction meant ANY extension this producer had
#: never explicitly enumerated - `.xhtml`, `.groovy`, `.tag`, `.jspf`
#: (reviewer-3's own ``.cr12-jsf`` fixture), or literally any other
#: language - fell through with NO java_results entry and NO
#: WorkerProblem at all: not even addressed as a coverage gap, the
#: silent-vanish FIX ROUND 14's own CR10-5 JUDGE was supposed to close
#: as a class. The four categories below (docs, plain text, lockfiles,
#: images) are what an ordinary repository's real non-code surface
#: needs; PROVISIONAL, like every other closed-set constant in this
#: module - a genuinely benign extension absent from this set is the
#: SAFE direction to be wrong in (one more recorded, non-degrading
#: problem), never the silent-vanish the old direction risked.
_BENIGN_NON_CODE_EXTENSIONS = frozenset({
    ".md", ".markdown", ".rst", ".adoc", ".txt",
    ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".bmp", ".webp",
})
_BENIGN_NON_CODE_BASENAMES = frozenset({
    ".gitignore", ".gitattributes", ".gitmodules", ".editorconfig", ".dockerignore",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock", "poetry.lock",
    # FIX ROUND 25 (twenty-first cold read, F9, take-it): a closed,
    # well-known set of EXTENSIONLESS project-metadata files - the same
    # class of benign, non-code documentation _BENIGN_NON_CODE_
    # EXTENSIONS already covers for ".md"/".txt" variants, but these
    # five routinely ship with NO extension at all (a bare "LICENSE",
    # never "LICENSE.txt") - README.md already benign, an extensionless
    # LICENSE recorded unsupported_language, an asymmetry between two
    # equally inert project-root files. PROVISIONAL, like every other
    # closed-set constant in this module - documented the same way tier
    # 2's own criterion is.
    "license", "notice", "copying", "authors", "changelog",
})
#: FIX ROUND 16b (reviewer-3's rejection of round 16, BLOCKER 1 - the B4
#: CALIBRATION): round 16's own inversion is RATIFIED (an unenumerated
#: extension must never silently vanish with no problem recorded at
#: all) - but "recorded" and "degrades this run" were left as the SAME
#: claim, and that half is OVERTURNED, measured on a 38-repo battery: an
#: ordinary healthy Spring Boot repo scanned DEGRADED with 10 recorded
#: problems - `mvnw`, `mvnw.cmd`, `Dockerfile`, `LICENSE`, CI YAMLs, and
#: `application.yml` all flipped a clean run to degraded. Sharpest case:
#: `application.properties` stayed record-only (its own pre-existing
#: carve-out) while `application.yml` - the IDENTICAL configuration,
#: merely a different serialization - degraded, an incoherent claim
#: about the same information under two different reasonable spellings.
#:
#: THREE TIERS now, not two: (1) adapter-handled languages are parsed
#: normally, unchanged; (2) this CLOSED, PROVISIONAL list of recognized
#: CODE-BEARING shapes (plus Spring bean XML, still via the existing
#: root-element sniff below) is recorded AND degrades - a real,
#: application-level source file this producer simply has no adapter
#: for yet; (3) EVERYTHING ELSE non-benign (any extension neither
#: adapter-handled, benign, nor on this list - `Dockerfile`, `mvnw`, a
#: CI YAML, `application.yml`, ...) is still recorded (round 16's own
#: win - the cr12-jsf silent-vanish class stays closed, every one of
#: these still gets a WorkerProblem) but no longer flips status - a
#: build/tooling/infra file is not "missed application code" the way a
#: JSP or a Kotlin source is, even though this producer cannot parse it
#: either.
_DEGRADING_CODE_EXTENSIONS = frozenset({
    ".jsp", ".jspx", ".jspf", ".tag", ".sql", ".groovy", ".kt", ".scala", ".xhtml", ".ftl", ".vm",
    # FIX ROUND 17 (thirteenth cold read, CR13-1 MAJOR, wrong-data, part
    # (b) - GROW TIER 2): the three-tier rule's own inversion is
    # RATIFIED, but this list stayed too narrow - the reader's own
    # polyglot fixture (.pks/.pkb Oracle PL/SQL package bodies, real
    # migration estate) scanned COMPLETE, not degraded, over genuine
    # application code this producer simply has no adapter for. Added
    # the UNAMBIGUOUS application-code extensions the reader named that
    # are NEVER incidental in an ordinary Java repository: .cs/.php/.rb/
    # .go (a real application-language source file, never tooling),
    # .pks/.pkb (Oracle PL/SQL package spec/body - real database-tier
    # migration estate), .xsl (JUDGE - a transform is code-bearing;
    # included per the reviewer's own lean).
    #
    # FIX ROUND 17b (reviewer-3's rejection of round 17, TIER-2 PARTIAL
    # OVERTURN, measured): round 17 also added .js/.ts/.py - OVERTURNED.
    # Unlike .cs/.php/.rb/.go/.pks/.pkb/.xsl, these three are ROUTINELY
    # INCIDENTAL in an ordinary Java repository (a `scripts/release.py`
    # helper, a webapp's own static `app.js`/`app.ts` asset) - measured
    # against the round-16b composite Spring Boot repo (this producer's
    # own acceptance fixture for the three-tier rule), whose own
    # completeness was the condition tier 2 was accepted under: adding
    # these three silently re-degraded that same repo and reversed two
    # of its own ratified battery rows. The criterion that actually
    # separates tier 2 from tier 3 (see the module-level PROVISIONAL
    # note below): NEVER-incidental application/database estate, not
    # merely "a real programming language." A genuine Python/Node
    # service living in a polyglot monorepo still gets tier 3's own
    # guarantee (recorded, never silently vanished) - the SAME
    # under-claim trade this whole three-tier rule already accepts
    # everywhere else, applied consistently rather than carved out for
    # these three specifically.
    ".cs", ".php", ".rb", ".go", ".pks", ".pkb", ".xsl",
})
#: FIX ROUND 17 (thirteenth cold read, CR13-1 MAJOR): this whole list is
#: PROVISIONAL and expected to GROW as this producer meets more real
#: languages - closed and narrow by construction (round 16's own
#: inversion means an absent entry here is still recorded, never
#: silently vanished; it just under-claims degradation for a language
#: this list has not caught up to yet). Reviewer-3 ratifies additions.
#:
#: FIX ROUND 17b (measured criterion, replacing "any real programming
#: language" after that reading proved too wide): an extension belongs
#: here only when it is NEVER-INCIDENTAL application/database estate in
#: an ordinary Java repository - a file this extension names is ALWAYS
#: real migration-relevant source, never a routine helper/tooling/asset
#: script that merely happens to share the same language. `.js`/`.ts`/
#: `.py` failed this exact test (a `scripts/release.py` helper, a
#: webapp's own static `app.js` asset are BOTH routine and common) and
#: were removed; `.cs`/`.php`/`.rb`/`.go`/`.pks`/`.pkb`/`.xsl` pass it
#: (never merely incidental tooling in a Java repo). Consult THIS
#: criterion, not the list's own current membership, before adding a
#: new extension.
#: Spring bean XML's own root element - the ONE xml root name this
#: producer recognizes as code-bearing (a bean declaration is
#: application wiring, not tooling configuration). PROVISIONAL, like
#: every other closed-set constant in this package. FIX ROUND 14c: XML
#: element names are case-sensitive - ``sniff_xml_root_element`` returns
#: the root's name EXACTLY as spelled, so this comparison below is
#: exact-case by construction; ``<BEANS>`` never matches this lowercase
#: literal and correctly falls through to record-only.
_SPRING_BEAN_XML_ROOT_ELEMENT = "beans"
#: FIX ROUND 23 (nineteenth cold read, F12, JUDGE - taken): Struts 1.x's
#: own `struts-config.xml` is the SAME class of gap CR13-1 already
#: measured for Spring bean XML - a closed, recognized, code-bearing
#: action-mapping configuration shape (form beans, action mappings,
#: forwards - real routing estate, not tooling) this producer has no
#: adapter for, previously falling into tier 3's silent-unless-recorded
#: default alongside genuinely inert config XML. Same exact-case,
#: root-element-name recognition as `_SPRING_BEAN_XML_ROOT_ELEMENT`.
_STRUTS_CONFIG_XML_ROOT_ELEMENT = "struts-config"
#: The closed set of XML root elements this producer recognizes as
#: code-bearing (tier 2), each paired with the human-readable language
#: name its own detail message names - grown alongside the root-element
#: set itself rather than duplicating a parallel if/elif chain.
_TIER_2_XML_ROOT_ELEMENT_LABELS = {
    _SPRING_BEAN_XML_ROOT_ELEMENT: "Spring bean XML",
    _STRUTS_CONFIG_XML_ROOT_ELEMENT: "Struts action-mapping XML",
}
#: MAJOR 2 (sixth cold read, fix round 9): both are real, common
#: declarations this adapter's class/interface/enum/record extractor
#: does not recognize at all (package-info.java carries only a package
#: statement, possibly with a package-level annotation; module-info.java
#: declares a `module ... { ... }` block) - both ALWAYS legitimately
#: yield zero units, never a header shape this adapter merely failed to
#: recognize.
_LEGITIMATELY_TYPELESS_BASENAMES = frozenset({"package-info.java", "module-info.java"})


def is_a_code_bearing_extension_worth_degrading_when_silently_excluded(
    relative_path: str,
) -> bool:
    """FIX ROUND 18 (fourteenth cold read, F6 JUDGE, taken): true for an
    extension this run would otherwise have tried to understand as code
    - adapter-handled (``_ADAPTER_EXTENSIONS``) or tier-2 degrading
    (``_DEGRADING_CODE_EXTENSIONS``, see its own criterion comment).
    Used by scan_pipeline.py to decide whether a file discovery excluded
    outright as binary content (a NUL byte in its sniffed prefix - see
    discovery.py's own ``_looks_binary``) is genuinely silent (a real
    binary blob, unaffected) or a real code file this run failed to
    read at all - a UTF-16-encoded ``.java`` file is a legal ``javac``
    input, genuinely present in legacy Windows-authored codebases, that
    trips this heuristic; under this producer's own just-built tier
    calibration, silently dropping it is QUIETER (less visible) than
    dropping a ``.jsp`` file, an inconsistency in how seriously
    different silent-drop shapes are treated. Genuinely binary
    extensions (``.png``, ``.bin``, ...) are absent from BOTH sets and
    stay exactly as silent as they are today.

    FIX ROUND 26 (twenty-second cold read, F4 MAJOR, wrong-data): this
    predicate consulted only EXTENSIONS - a UTF-16-encoded ``pom.xml``/
    ``web.xml`` (a legal input to the same legacy Windows tooling that
    produces a UTF-16 ``.java`` file) tripped the identical binary-sniff
    heuristic but was never recognized as code-bearing here (a
    basename-matched producer has no extension of its own to check), so
    it was silently dropped - complete, 0 problems, no unit, no edges,
    no entry points - while a UTF-16 ``.java`` correctly degrades. An
    unreadable BUILD/ROUTING descriptor is at least as material as an
    unreadable ``.java`` file (it defines the estate's own structure) -
    now also consults ``_ADAPTER_HANDLED_XML_BASENAMES``, the same
    treatment (a degrading problem, plus externality suppression via
    scan_pipeline.py's own poison rule, which keys off this predicate)."""
    lower = relative_path.lower()
    name_lower = Path(relative_path).name.lower()
    return (
        lower.endswith(tuple(_ADAPTER_EXTENSIONS))
        or lower.endswith(tuple(_DEGRADING_CODE_EXTENSIONS))
        or name_lower in _ADAPTER_HANDLED_XML_BASENAMES
    )


def is_a_root_sniffed_xml_extension(relative_path: str) -> bool:
    """FIX ROUND 26b (reviewer-3 delta on `38a21f3`, item 2, R4 carry
    OVERTURNED - closed, wrong-data): a binary-excluded ``.xml`` file
    that is NOT one of the two adapter-handled basenames (``pom.xml``/
    ``web.xml``) is exactly the file this worker's own root-sniff branch
    (``sniff_xml_root_element``) would otherwise have decoded and
    inspected to decide its tier - a UTF-16-encoded Spring bean XML/
    Struts config XML (tier 2, code-bearing) or a UTF-16 ``logback.xml``
    (tier 3, not code-bearing) both trip discovery's own binary sniff
    identically, and neither ever reaches the root-element sniff at all,
    so this run genuinely has no evidence to tell them apart - unlike
    ``is_a_code_bearing_extension_worth_degrading_when_silently_
    excluded`` above (which answers "definitely code-bearing"), this
    answers only "would have been root-sniffed, tier unknown." Used by
    scan_pipeline.py to RECORD (never silently vanish) this shape
    without guessing a degrading verdict it has no evidence for -
    degrading every repo with an unreadable ``logback.xml`` would be the
    exact round-16 dilution this producer's own tier calibration already
    refuses to reopen."""
    lower = relative_path.lower()
    name_lower = Path(relative_path).name.lower()
    return lower.endswith(".xml") and name_lower not in _ADAPTER_HANDLED_XML_BASENAMES


WORKER_SCHEMA_VERSION = 1
_WORKER_TIMEOUT_SECONDS = 300.0

#: M11 (cold-read, PR-B fix round 3): the design lists "adapter work"
#: among the resource caps (alongside file count/bytes/nesting), but none
#: existed - only the whole-worker _WORKER_TIMEOUT_SECONDS, which aborts
#: the ENTIRE scan (no published run at all) rather than degrading just
#: the one problematic file. PROVISIONAL, like the other caps in
#: discovery.py, pending the PR-B exit-gate measurement.
_MAX_ADAPTER_INPUT_BYTES = 8 * 1024 * 1024

#: The worker gets ONLY what a pure, offline, file-reading Python process
#: needs to start - never provider credentials, proxy variables, bus/session
#: state, or any other ambient configuration a network-capable tool could
#: use (design: "without provider credentials, proxy variables, remote
#: endpoints, or inherited configuration that names a network service").
#: Deliberately an ALLOWLIST, not a blocklist that tries to name every
#: dangerous variable - the same rationale as ``dev_gate._base_env``.
_ALLOWED_ENV_VARS = frozenset({
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "WINDIR",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "TEMP",
    "TMP",
    "TMPDIR",
})


class WorkerError(ComprehensionError):
    """The sanitized worker process could not be started, timed out, exited
    with an error, or returned output this caller cannot trust. Never
    raised for an individual file's own parse/read failure - that is a
    bounded problem record in the result instead (design: a parser failure
    yields a bounded problem, it does not erase the rest of the scan)."""

    reason_code = "comprehension_worker_error"


def sanitized_worker_env(source_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build the worker child process's environment from ``source_env``
    (the CALLER's own environment by default - never mutated in place;
    this returns a new, filtered dict) using the fixed allowlist above.
    Exposed as its own function so a test can assert exactly what the
    worker process would see without actually spawning one.

    Note 7 (second cold read, fix round 4): matching is deliberately
    CASE-INSENSITIVE (``key.upper() in _ALLOWED_ENV_VARS``), never a
    strict, exact-case match - Windows itself treats environment variable
    names case-insensitively and does not guarantee any one casing
    convention for what ``os.environ`` reports (``Path`` and ``PATH`` are
    the same variable there), so a strict-case allowlist would risk
    silently dropping an allowed variable on some Windows hosts. This can
    only ever ADMIT a source key whose UPPERCASED form is already in the
    fixed allowlist - it never widens what family of variables can pass,
    only how their casing is spelled.
    """
    source = source_env if source_env is not None else os.environ
    return {key: value for key, value in source.items() if key.upper() in _ALLOWED_ENV_VARS}


@dataclass(frozen=True)
class WorkerFileClaim:
    """The default, adapter-independent claim for one enumerated file: it
    exists, and this is its size. Every non-excluded file gets exactly
    one of these regardless of whether any adapter understands it
    (design, Artifact 1: "Every non-excluded file remains an addressable
    `file` unit").

    N3 (fourth cold read, fix round 6): this used to also carry a
    ``content_digest`` - a SECOND hash of every file's bytes, on top of
    the one discovery.py already computes for the whole-scope
    fingerprint - with zero consumers outside this module: modules.json's
    ``source_digests`` publish discovery's own digest, never this one.
    Dropped, along with the hashing that produced it (``_hash_bytes``);
    reading the bytes themselves stays, since the adapter dispatch below
    still needs them."""

    relative_path: str
    byte_count: int


@dataclass(frozen=True)
class WorkerProblem:
    """One file this worker could not process. A preliminary, internal
    shape - the full ``problems.json`` record schema (stable ID, severity,
    producers, generated message) is formalized where that artifact is
    built; this only carries enough to construct that record later without
    losing information here.

    FIX ROUND 13c (reviewer-3's part 1 on round 13b): ``qualified_name``
    carries an adapter-attributed problem's own owning type through to
    ``scan_pipeline.py`` unchanged - ``None`` for the ordinary file-wide
    problem shapes (parse failures, resource caps), a real qualified
    name for the narrower few an adapter can pin to one declared type.

    FIX ROUND 14b (reviewer-3's ratified split on CR10-5): ``degrades_run``
    defaults ``True`` (every pre-existing reason code's own behavior,
    unchanged - a parse failure, a resource cap, an unassociated route
    all still flip the run to ``degraded``). ``unsupported_language`` is
    the first reason code where visibility and degradation are DIFFERENT
    claims about the SAME file: reviewer-3's own reader test ("degrade
    when a reader would say the inventory missed something they NEEDED")
    means a properties file or a tooling-XML file (checkstyle, logback)
    is always worth RECORDING (an operator should still see it was not
    read) but never worth degrading a healthy run over - unlike a JSP/SQL
    file or Spring bean XML, which really is missed application code.
    Set ``False`` per-instance only for that one reason code; every other
    call site in this module leaves the default alone."""

    reason_code: str
    relative_path: str
    detail: str
    qualified_name: str | None = None
    degrades_run: bool = True


@dataclass(frozen=True)
class WorkerResult:
    schema_version: int
    file_claims: list[WorkerFileClaim] = field(default_factory=list)
    problems: list[WorkerProblem] = field(default_factory=list)
    #: relative_path -> adapters.java.file_result_to_json(...) payload, for
    #: every recognized-extension file an adapter successfully parsed.
    java_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Round 11c (reviewer-3 delta on round 11b, vehicle change):
    #: adapter-level exclusion counts, whole-run-aggregated, category ->
    #: count - the SAME idiom discovery.py's own ``exclusions`` map
    #: already uses for enumeration-level categories, extended here to a
    #: DECLARED, deliberate scope limitation (a pom's profile-scoped
    #: dependency) that must be visible without driving the run to
    #: degraded the way a real problem does.
    exclusions: dict[str, int] = field(default_factory=dict)
    #: MICRO-NOD 50c (F1, completeness): descriptor-name conflicts found
    #: PURELY over the annotation registry when this run's own scan found
    #: NO web.xml at all (java_adapter.annotation_only_descriptor_
    #: conflicts's own return value, aggregated across servlet+filter) -
    #: the SAME shape/consumer as each web.xml JavaFileResult's own
    #: per-file ``descriptor_name_conflicts`` field, just not anchored to
    #: any one file, so it lives at the WorkerResult level instead.
    descriptor_name_conflicts: list[tuple[str, list[str]]] = field(default_factory=list)


def _decode_text_or_flag_undecodable(
    data: bytes, rel: str, problems: list[WorkerProblem], *, degrades_run: bool = True,
) -> str | None:
    """Decodes ``data`` as UTF-8 (BOM-tolerant), guarding against a
    silent, WRONG decode - never raises.

    FIX ROUND 26 (twenty-second cold read, F3 BLOCKER, wrong-data,
    client-corpus-critical): round 21's own CR17-4 U+FFFD ``encoding_
    undecodable`` guard existed ONLY on the ``.java`` decode site - the
    ``pom.xml``/``web.xml``/xml-root-sniff decode sites all decoded
    ``errors="replace"`` with NO guard at all, so a Latin-1/CP1252 pom
    (the default encoding of many pre-Maven-3 European estates -
    the target client's own estate among them) published a FABRICATED coordinate
    (``com.example:caf�-core``) on a complete/zero-problem run, and
    a UTF-8 sibling's edge to the REAL coordinate published a confident
    ``resolved``/``external`` claim (the registry never saw the
    fabricated identity, so the miss looked genuine) - the exact
    CR13-4/round-18-F3 over-claim class, reintroduced through an
    unguarded decode site. Fixed AS THE CLASS: one shared decode helper,
    used at every one of this worker's own decode sites, so a future new
    decode site inherits the guard automatically rather than needing its
    own copy remembered.

    FIX ROUND 27 (twenty-third cold read, F3 MAJOR, wrong-data): a
    binary-excluded and an encoding-undecodable, non-adapter-handled
    ``.xml`` file are EPISTEMICALLY IDENTICAL - neither can be root-
    sniffed to determine its own tier, since decoding is exactly what
    failed. Round 26b's own binary ruling already refused to degrade the
    binary-excluded twin ("degrading every repo carrying an unreadable
    logback.xml"); this call's ``degrades_run`` lets the xml-root-sniff
    call site (worker.py) pass ``False`` for the identical reason,
    without changing anything for the OTHER three decode sites (.java/
    pom.xml/web.xml), which stay degrading unconditionally - they are
    code-bearing by definition, never merely possibly so.

    Returns the decoded text, or ``None`` (having already appended an
    ``encoding_undecodable`` WorkerProblem) when the decoded text
    contains U+FFFD - the caller must skip adapter analysis entirely in
    that case, the same "cannot trust what would be extracted" treatment
    the ``.java`` branch already established."""
    text = data.decode("utf-8-sig", errors="replace")
    if "�" in text:
        problems.append(WorkerProblem(
            reason_code="encoding_undecodable", relative_path=rel,
            detail="this file's bytes could not be decoded as UTF-8 (the "
                   "decoded text contains the U+FFFD replacement character) - "
                   "likely Latin-1/CP1252 or another non-UTF-8 encoding; "
                   "adapter analysis skipped rather than risk a corrupted or "
                   "fabricated qualified name",
            degrades_run=degrades_run))
        return None
    return text


def process_paths(root: Path, relative_paths: list[str]) -> WorkerResult:
    """The worker's unit of work: read each of ``relative_paths`` under
    ``root``, produce its default file-unit claim, AND - for a recognized
    extension - dispatch the SAME already-read bytes to that language's
    bundled adapter, in-process, right here (design: "Adapters run
    in-process inside that worker"). Re-confines every path under ``root``
    itself (defense in depth - the caller has already validated and
    confined these paths during enumeration, but this process boundary
    never trusts an upstream check blindly for a security-relevant
    operation). An adapter parse failure is a bounded problem, exactly
    like an unreadable file - it never aborts the rest of the scan."""
    claims: list[WorkerFileClaim] = []
    problems: list[WorkerProblem] = []
    java_results: dict[str, dict[str, Any]] = {}
    exclusions: dict[str, int] = {}

    # MICRO-ROUND 49 (M3 MAJOR, wrong-data): every @WebServlet(name=...)/
    # @WebFilter(name=...) declared name this run's own .java files
    # carry, accumulated as the main loop below encounters them - a
    # web.xml's own <servlet-mapping>/<filter-mapping> needs the FULL
    # set to resolve a name-space lookup correctly (Servlet spec
    # s8.2.3), but web.xml may be processed BEFORE some of those .java
    # files in `relative_paths`' own order. Rather than a second full
    # read+parse pass over every .java file (correct, but doubles real
    # adapter work for every file, not just one small web.xml the way
    # the metadata-complete pre-scan above can afford to), web.xml's
    # OWN processing is deferred to a second, short pass after the main
    # loop finishes (below) - by then this dict is fully populated
    # regardless of the original iteration order, and every .java file
    # is still read and parsed exactly once.
    # MICRO-ROUND 50 (Cluster 2, M1 BLOCKER, wrong-data): widened from
    # name -> ONE qualified_name (a plain ``dict.update()`` below used to
    # silently pick whichever .java file this run's own walk happened to
    # visit LAST as the sole owner of a duplicated @WebServlet/@WebFilter
    # name) to name -> a LIST of every qualified_name declared for it -
    # see java.py's own ``_servlet_class_by_name`` docstring for why this
    # is what lets its conflict machinery see (and correctly conflict) a
    # name TWO DIFFERENT classes both declare via annotations alone, the
    # same "no declaration is authoritative by execution order" outcome
    # its XML <servlet>/<filter> twin already gets for the identical
    # shape.
    annotation_declared_servlet_names: dict[str, list[str]] = {}
    annotation_declared_filter_names: dict[str, list[str]] = {}
    deferred_web_xml: list[tuple[str, bytes]] = []

    # MICRO-ROUND 49 (M2 MAJOR, wrong-data): a small, cheap pre-scan for
    # web.xml's own metadata-complete declaration - Servlet 3.0 s8.1
    # makes this fact govern EVERY .java file's own @WebServlet/
    # @WebFilter dispatch below, but it is only knowable once web.xml
    # itself is read, which may come before OR after any given .java
    # file in `relative_paths` - this run's own iteration order is never
    # something the fact may depend on. Reads web.xml a second time here
    # (the main loop below reads it again, at its own normal turn, where
    # it is decoded for real and any genuine read/decode problem is
    # reported) - a small, one-time cost for a small file, traded for
    # not restructuring this loop's own single-pass shape. Silent on any
    # failure here (missing, unreadable, wrong encoding) - this pre-
    # scan's own job is only "is metadata-complete declared, and where,"
    # never "report a problem with web.xml," which the main loop's own
    # normal pass already owns exclusively.
    #
    # MICRO-ROUND 50 (Cluster 1, B2 BLOCKER, wrong-data): this used to be
    # ONE GLOBAL bool, set the moment ANY file named web.xml ANYWHERE in
    # the scan declared metadata-complete=true, with no notion of WHICH
    # deployment that fact belongs to. Reviewer-3 measured a two-module
    # reactor where only mod-a/WEB-INF/web.xml declares metadata-
    # complete=true publishing ZERO entry points repo-wide, including
    # mod-b's own (a wholly separate, independent deployment whose own
    # web.xml never made that declaration) - and a
    # src/test/resources/WEB-INF/web.xml test fixture silencing
    # PRODUCTION annotations elsewhere in the same repo, since this scan
    # never checked WHERE a web.xml sat or whether it was even a real,
    # loadable deployment descriptor before trusting its declaration.
    #
    # Fixed two ways at once: (1) scoped from a single bool to a SET of
    # DEPLOYMENT ROOTS that declared metadata-complete=true - a
    # deployment root is the directory that directly CONTAINS its own
    # WEB-INF/ (the Servlet spec's own webapp-root convention; per the
    # reactor's own layout, ``mod-a/WEB-INF/web.xml``'s deployment root
    # is ``mod-a``, never the whole repo) - a .java file's own dispatch
    # below now checks whether IT sits under one of these roots, never a
    # single repo-wide flag; (2) reuses the EXACT SAME "is this even a
    # real, loadable deployment descriptor" gate the main loop's own
    # dispatch branch already applies (round 43/44's own stray-web.xml
    # exclusion, `_TEST_SOURCE_ROOT_SEGMENT` included) - a web.xml
    # sitting anywhere other than directly inside a WEB-INF/ directory,
    # or one under a recognized test source root, can never set this
    # flag for ANY deployment, closing the test-fixture gap at the same
    # time as the multi-module one.
    metadata_complete_deployment_roots: set[str] = set()
    for rel in relative_paths:
        if Path(rel).name.lower() != "web.xml":
            continue
        rel_posix = rel.replace("\\", "/")
        if (
            Path(rel).parent.name.lower() != "web-inf"
            or _TEST_SOURCE_ROOT_SEGMENT.search(rel_posix.lower())
        ):
            continue
        try:
            resolved = resolve_under_root(rel, root=root, label="worker input path")
            data = resolved.read_bytes()
        except (EnvelopeError, OSError):
            continue
        text = _decode_text_or_flag_undecodable(data, rel, [])
        if text is not None and java_adapter.web_app_declares_metadata_complete(text):
            metadata_complete_deployment_roots.add(Path(rel).parent.parent.as_posix())
    for rel in relative_paths:
        try:
            resolved = resolve_under_root(rel, root=root, label="worker input path")
        except EnvelopeError as exc:
            # M-3 (third cold read, fix round 5): resolve_under_root's own
            # message already names only the RELATIVE value, never the
            # resolved absolute path - bounded_detail here is defense in
            # depth (length only), not a path scrub.
            problems.append(WorkerProblem(
                reason_code="path_excluded", relative_path=rel, detail=bounded_detail(str(exc))))
            continue
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            # M-3: never str(exc) - it embeds resolved's ABSOLUTE path.
            problems.append(WorkerProblem(
                reason_code="parse_failed", relative_path=rel,
                detail=bounded_os_error_detail("could not read the file's bytes", exc),
            ))
            continue
        claims.append(WorkerFileClaim(relative_path=rel, byte_count=len(data)))

        # Note 10 (second cold read, fix round 4): dispatch must not be
        # extension-case-sensitive - Windows and default macOS
        # filesystems are case-insensitive/case-preserving, so `Foo.JAVA`
        # or `POM.XML` are perfectly reachable real files there, and a
        # case-sensitive check would silently skip adapter dispatch for
        # them.
        rel_lower = rel.lower()
        rel_name_lower = Path(rel).name.lower()
        adapter = next(
            (mod for ext, mod in _ADAPTER_EXTENSIONS.items() if rel_lower.endswith(ext)), None)
        adapter_eligible = adapter is not None or rel_name_lower in ("pom.xml", "web.xml")
        if adapter_eligible and len(data) > _MAX_ADAPTER_INPUT_BYTES:
            # M11 (cold-read, PR-B fix round 3): the design lists adapter
            # work among the resource caps, but none existed - a
            # pathologically large file could only be caught by the
            # WHOLE-WORKER 300s timeout, which aborts the ENTIRE scan with
            # no published run at all, not a bounded, degraded one. This
            # file still gets its base WorkerFileClaim above (still
            # addressable); only adapter analysis is skipped, as a named,
            # bounded resource_limit problem - a scan cap degrades the
            # run, it never silently samples or aborts wholesale.
            problems.append(WorkerProblem(
                reason_code="resource_limit", relative_path=rel,
                detail=f"{len(data)} bytes exceeds the {_MAX_ADAPTER_INPUT_BYTES}-byte "
                       "per-file adapter-work cap - adapter analysis skipped"))
            continue
        if adapter is not None:
            try:
                # FIX ROUND 20 (sixteenth cold read, B1 BLOCKER, wrong-
                # data): a UTF-8 BOM on a .java file (ordinary Windows-
                # tooling output, a legal javac input, pervasive in
                # legacy estates) is not whitespace - plain "utf-8"
                # decoding left it as the file's own leading character,
                # defeating _PACKAGE_RE's own `^\s*package` anchor. The
                # unit then published a WRONG qualified name (the bare
                # simple name, package lost entirely), a wrong unit_id,
                # and every importer of the real type published a
                # confident EXTERNAL claim for genuine in-repo source -
                # a complete, zero-problem run. "utf-8-sig" strips a
                # leading BOM when present and is byte-identical to
                # plain "utf-8" when absent - applied at every one of
                # this worker's own decode sites (.java/pom.xml/web.xml/
                # the xml-root sniff) so every consumer inherits the fix
                # from one mechanism, never a second copy to drift.
                # FIX ROUND 21 (seventeenth cold read, CR17-4 MAJOR,
                # wrong-data): Latin-1/CP1252 source (the DEFAULT
                # encoding of many pre-Maven-3 European estates) decoded
                # with errors="replace" silently substitutes U+FFFD for
                # every byte sequence "utf-8-sig" cannot decode - and
                # U+FFFD is outside \w, so _PACKAGE_RE/the type-name
                # anchor regexes simply skip over it, producing a
                # TRUNCATED, FABRICATED qualified name (e.g. a class
                # named "Café" decodes to "Caf�" and extracts as
                # "Caf") - wrong on its own, and every importer of the
                # REAL type then publishes a confident EXTERNAL claim
                # for what is actually in-repo source, on a complete/
                # zero-problem run. Never attempt charset detection this
                # slice (declared, not a silent gap) - skip adapter
                # analysis entirely instead, the same "cannot trust what
                # would be extracted" treatment the per-file resource
                # cap above already gets. Feeds the existing
                # degraded_paths suppression (scan_pipeline.py) via the
                # SAME mechanism the resource cap already relies on -
                # any WorkerProblem for this path already makes it
                # ineligible for a confident external claim.
                #
                # DECLARED (round 21b, reviewer-3's re-delta, item 4(b)):
                # this check false-positives on a file that is genuinely
                # valid UTF-8 but happens to contain a LEGITIMATE literal
                # U+FFFD character (in a string constant, a comment, ...)
                # - a real if rare shape. Accepted: the cost is a one-
                # sided OVER-report (this file's own adapter analysis is
                # skipped and its importers under-claim, resolving
                # unresolved instead of a resolved internal edge, never a
                # WRONG claim in the other direction), and the recorded
                # detail already states this as evidence ("likely Latin-
                # 1/CP1252 ... ") rather than a certain conclusion.
                #
                # FIX ROUND 26 (twenty-second cold read, F3 BLOCKER):
                # routed through the shared `_decode_text_or_flag_
                # undecodable` helper - see its own docstring for why
                # this guard is no longer a copy living only here.
                text = _decode_text_or_flag_undecodable(data, rel, problems)
                if text is None:
                    continue
                # MICRO-ROUND 50 (Cluster 1, B2): scoped to THIS file's
                # own deployment - see the pre-scan's own docstring.
                metadata_complete = any(
                    _rel_is_under_deployment_root(rel, deployment_root)
                    for deployment_root in metadata_complete_deployment_roots
                )
                result = adapter.parse_java_source(rel, text, metadata_complete=metadata_complete)
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=bounded_detail(f"{adapter.ADAPTER_NAME} adapter failed: {exc}")))
            else:
                java_results[rel] = adapter.file_result_to_json(result)
                # MICRO-ROUND 49 (M3 MAJOR, wrong-data): accumulated
                # across every .java file this run scans - see this
                # function's own docstring for why web.xml's own
                # processing is deferred to see the FULL set.
                #
                # MICRO-ROUND 50 (Cluster 2, M1 BLOCKER): appends rather
                # than a plain dict.update() - see the accumulator's own
                # docstring above for why a walk-order-dependent single
                # winner must never be picked here.
                for name, qualified_name in result.web_servlet_declared_names.items():
                    annotation_declared_servlet_names.setdefault(name, []).append(qualified_name)
                for name, qualified_name in result.web_filter_declared_names.items():
                    annotation_declared_filter_names.setdefault(name, []).append(qualified_name)
                # BLOCKER 1b (fifth cold read, fix round 8): a parse that
                # SUCCEEDS but extracts ZERO units used to count as
                # positive adapter evidence with no problem recorded at
                # all - readiness then reported source_understood
                # satisfied for a file this adapter never actually
                # understood (a header shape its coarse pattern-based
                # extractor could not recognize is indistinguishable,
                # from here, from a legitimately typeless file). The two
                # legitimate typeless shapes - package-info.java and
                # module-info.java by name (MAJOR 2, sixth cold read,
                # fix round 9: module-info.java flipped an otherwise-
                # clean run to degraded with a factually wrong problem
                # detail - it declares a `module ... { ... }` block, a
                # keyword this adapter's class/interface/enum/record
                # extractor does not recognize at all, so it ALWAYS
                # yields zero units, the same legitimately-typeless
                # shape package-info.java already is), and a genuinely
                # blank/comment-only file by content - are recognized
                # explicitly and exempted; anything else with zero
                # units is a real, named problem.
                if (
                    not result.units
                    and rel_name_lower not in _LEGITIMATELY_TYPELESS_BASENAMES
                    and not adapter.is_effectively_empty_java_source(text)
                ):
                    problems.append(WorkerProblem(
                        reason_code="no_types_extracted", relative_path=rel,
                        detail="the java adapter parsed this file but extracted no declared "
                               "types - an unrecognized header shape, not a legitimate "
                               "empty/typeless file",
                    ))
                # Fix round 10 (structural order, fail-safe direction):
                # a route annotation the adapter could not confidently
                # associate with a class or a method - never silently
                # dropped, never silently published as a guessed route.
                # Fix round 11: a distinct reason_code (carried on the
                # adapter's own JavaAdapterProblem, not hardcoded here
                # any more) for a route annotation whose VALUE could not
                # be recovered as a literal - a different failure family
                # from association, never coalesced into one bucket.
                for problem in result.problems:
                    problems.append(WorkerProblem(
                        reason_code=problem.reason_code, relative_path=rel,
                        detail=problem.detail, qualified_name=problem.qualified_name,
                        # MICRO-ROUND 48b (F2): deployment_base_path_declared is
                        # informational, never degrading - the SAME non-degrading
                        # exception duplicate_route_target already has at its own
                        # web.xml conversion site below, needed here for the first
                        # time since no other .java-parse-path reason code has ever
                        # been non-degrading.
                        degrades_run=problem.reason_code != "deployment_base_path_declared",
                    ))
        elif rel_name_lower == "pom.xml":
            # reviewer-3 B-3 (PR-B delta review): a pom.xml's build-relation
            # extraction used to happen in the PARENT process, reading the
            # file directly and bypassing this worker entirely - the one
            # content path where the design's "adapters run inside the
            # sanitized worker, stdin/stdout is the sole channel" boundary
            # statement did not hold. Route it through the SAME already-read
            # bytes and the SAME java_results channel every other adapter
            # claim uses, wrapped as a JavaFileResult with no units/entry
            # points of its own.
            try:
                # FIX ROUND 26 (twenty-second cold read, F3 BLOCKER,
                # wrong-data, client-corpus-critical): this decode site never
                # had the U+FFFD guard the .java branch carries - see
                # `_decode_text_or_flag_undecodable`'s own docstring.
                text = _decode_text_or_flag_undecodable(data, rel, problems)
                if text is None:
                    continue
                pom_units, build_edges, profile_scoped_dependency_count = (
                    java_adapter.parse_maven_pom(rel, text))
                # FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR - THE
                # REACTOR RULE): a separate, additional call (not baked
                # into parse_maven_pom's own return arity, which 28+
                # existing call sites already unpack positionally) - see
                # declared_reactor_module_paths's own docstring for why
                # this producer resolves nothing itself; scan_pipeline.py
                # does the excluded-region cross-reference after the
                # worker returns.
                declared_module_paths = java_adapter.declared_reactor_module_paths(text)
                # FIX ROUND 24 (twentieth cold read, F4 MINOR, wrong-
                # data): same reasoning - a module-own dependency's own
                # groupId/artifactId that is present but undecodable
                # (CDATA/entity constructs) silently vanishes from
                # parse_maven_pom's own return with no problem recorded.
                undecodable_dependency_lines = (
                    java_adapter.pom_dependency_decode_problems(text))
                # FIX ROUND 35 (twenty-ninth cold read, F1 BLOCKER, wrong-
                # data): the same split for the pom's OWN coordinate - see
                # pom_own_coordinate_decode_problems's own docstring.
                undecodable_own_coordinate_lines = (
                    java_adapter.pom_own_coordinate_decode_problems(text))
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=bounded_detail(f"{java_adapter.ADAPTER_NAME} adapter failed: {exc}")))
            else:
                # FIX ROUND 17 (thirteenth cold read, CR13-4 MAJOR): this
                # pom's own groupId:artifactId coordinate (when declared
                # at this level - see parse_maven_pom's own named limit)
                # is now a real unit claim, not just build edges - the
                # SAME registry every other producer's units already
                # build through, so a sibling pom's dependency on it can
                # resolve internal instead of a hardcoded external guess.
                java_results[rel] = java_adapter.file_result_to_json(
                    java_adapter.JavaFileResult(
                        units=pom_units, edges=build_edges,
                        declared_module_paths=declared_module_paths,
                    ))
                # Round 11c (reviewer-3 delta on round 11b, vehicle
                # change): a profile-scoped dependency this adapter
                # excludes (a profile may be active by default) is a
                # DECLARED scope limitation, not a run-degrading problem
                # - an exclusion count, never added to `problems`.
                if profile_scoped_dependency_count:
                    exclusions["profile_scoped_dependencies"] = (
                        exclusions.get("profile_scoped_dependencies", 0)
                        + profile_scoped_dependency_count
                    )
                # FIX ROUND 24 (twentieth cold read, F1b, wrong-data): a
                # pom parse that SUCCEEDS but registers no own coordinate,
                # no dependency edge, and no reactor module - the SAME
                # "positive evidence, not merely absence of a negative"
                # gap BLOCKER 1b (round 8) already closed for a .java
                # file's own zero-types case, never extended to pom.xml -
                # a namespace-prefixed pom this adapter's own coordinate/
                # dependency/reactor gates all silently failed to
                # recognize (before this round's own tag-stack fix) read
                # as a COMPLETE, zero-problem run, `source_understood`
                # confidently satisfied over a file the adapter understood
                # nothing of. A REAL pom always carries SOME identity
                # (own or `<parent>`-inherited) per Maven's own model, so
                # "genuinely nothing at all" is never a legitimate minimal
                # shape the way an empty .java file can be - a profile-
                # scoped-only pom (real facts, merely excluded by policy)
                # is exempted via the SAME count checked just above.
                if (
                    not pom_units and not build_edges and not declared_module_paths
                    and not profile_scoped_dependency_count
                ):
                    problems.append(WorkerProblem(
                        reason_code="no_pom_facts_extracted", relative_path=rel,
                        detail="the pom adapter parsed this file but extracted no "
                               "coordinate, dependency, or reactor-module facts at all - "
                               "an unrecognized or unmodeled pom shape, not a "
                               "legitimately minimal one",
                    ))
                # FIX ROUND 24 (twentieth cold read, F4 MINOR, wrong-
                # data): a real, present groupId/artifactId this adapter
                # could not decode - the dependency edge silently
                # vanished with no problem recorded; visible now.
                for line in undecodable_dependency_lines:
                    problems.append(WorkerProblem(
                        reason_code="dependency_value_unrecoverable", relative_path=rel,
                        detail=f"a <dependency> declared at line {line} has a groupId or "
                               "artifactId containing XML constructs this producer does "
                               "not decode - suppressed rather than published with a "
                               "guessed coordinate",
                    ))
                # FIX ROUND 35 (twenty-ninth cold read, F1 BLOCKER, wrong-
                # data): a real, present project-level groupId/artifactId
                # (or <parent> groupId) this adapter could not decode -
                # this pom's own coordinate silently vanished (treated as
                # absent) with no problem recorded; visible now.
                for line in undecodable_own_coordinate_lines:
                    problems.append(WorkerProblem(
                        reason_code="coordinate_value_unrecoverable", relative_path=rel,
                        detail=f"this pom's own project-level groupId/artifactId (or its "
                               f"<parent> block's own groupId), declared at line {line}, "
                               "contains XML constructs this producer does not decode - "
                               "this pom's own coordinate is treated as absent rather than "
                               "published with a guessed value",
                    ))
        elif rel_name_lower == "web.xml" and (
            Path(rel).parent.name.lower() != "web-inf"
            # FIX ROUND 45 (thirty-ninth cold read, F1 MAJOR, wrong-
            # data): this used to match case-SENSITIVELY against a
            # lowercase-only pattern, one clause after the `WEB-INF`
            # parent-name check right above it ALREADY lowercases its
            # own operand - the inconsistency was within this one
            # expression. `src/Test/...` and `src/test/...` name the
            # SAME directory on a case-insensitive platform (the
            # common case); round 37's own F4 policy applies here too.
            or _TEST_SOURCE_ROOT_SEGMENT.search(rel.replace("\\", "/").lower())
        ):
            # FIX ROUND 43 (thirty-seventh cold read, F4 MAJOR, wrong-
            # data - declared gap): per the Servlet spec, a REAL
            # deployable descriptor always lives directly inside a
            # `WEB-INF/` directory (never nested any deeper, never
            # anywhere else) - `WEB-INF/web.xml` is already this
            # producer's OWN assumed convention elsewhere (see
            # features_artifact.py's own worked example,
            # "WEB-INF/web.xml#dispatcher"). A file merely NAMED
            # "web.xml" outside that one real location - a docs/
            # examples copy, a tutorial snippet, a test-resources
            # fixture - is not a shape any servlet container will ever
            # load as a deployment descriptor, and publishing its
            # <servlet-mapping>/<filter-mapping> entries as genuinely
            # SERVED routes over-claims this scan's own confidence
            # about what the target application actually exposes.
            #
            # FIX ROUND 44 (thirty-eighth cold read, F2 MAJOR, wrong-
            # data - the gap this round's own comment named but the
            # code never checked): a test-resources copy - the THIRD
            # of the three cases named just above - can genuinely sit
            # directly inside a real `WEB-INF/` directory
            # (`src/test/resources/WEB-INF/web.xml`, a real, common
            # shape for a servlet-container integration test fixture)
            # and still passed this gate, since the gate only ever
            # checked location, never classification. This producer
            # already computes, elsewhere in the SAME pipeline, exactly
            # the evidence that would catch this (a unit under a
            # recognized test source root classifies `test` -
            # modules_artifact.py's own `_default_classification`) but
            # never consulted it here. `_TEST_SOURCE_ROOT_SEGMENT` is
            # the SAME predicate re-applied directly to this file's own
            # path (worker.py cannot import modules_artifact.py - see
            # that constant's own docstring above) - test classification
            # always wins first in the real pipeline too (`_default_
            # classification` checks it before any infrastructure
            # heuristic), so this direct re-check can never disagree
            # with what the eventual unit record would say.
            #
            # Judged: the SAME exclusion bucket as the other two cases,
            # not a separate one - all three are the identical
            # underlying judgment ("not a genuine deployment
            # descriptor"), and the round-43 comment above already
            # named all three together as one class.
            #
            # Judged: an EXCLUSION count (the same `profile_scoped_
            # dependencies` idiom above, and scan.json's own discovery-
            # level `exclusions` map), never a silently-vanished file
            # and never a `parse_failed`/degrading problem either - a
            # stray web.xml copy is a real, deliberate, named scope
            # limitation, not a defect this run failed to handle.
            exclusions["stray_web_xml_ignored"] = (
                exclusions.get("stray_web_xml_ignored", 0) + 1
            )
        elif rel_name_lower == "web.xml":
            # MICRO-ROUND 49 (M3 MAJOR, wrong-data): deferred to a second
            # pass after this loop (below) - see this function's own
            # docstring for why. The claim, resource-cap, and dispatch
            # logic above already ran identically for this path; only
            # the actual adapter call moves.
            deferred_web_xml.append((rel, data))
        elif not (
            rel_lower.endswith(tuple(_BENIGN_NON_CODE_EXTENSIONS))
            or rel_name_lower in _BENIGN_NON_CODE_BASENAMES
        ):
            # FIX ROUND 14 (tenth cold read, CR10-5 JUDGE, completeness):
            # this file is still addressable (the WorkerFileClaim above
            # already covers that). FIX ROUND 14b (reviewer-3's ratified
            # split): recording is unconditional here - only whether THIS
            # instance also degrades the run varies by kind, decided
            # below. FIX ROUND 16 (B4 BLOCKER): this branch is reached by
            # anything NOT adapter-handled and NOT benign now - the
            # inverted allowlist's whole point.
            if rel_lower.endswith(".xml") and rel_name_lower not in _ADAPTER_HANDLED_XML_BASENAMES:
                # FIX ROUND 26 (twenty-second cold read, F3 BLOCKER,
                # wrong-data): the same missing guard swept to this
                # decode site too - see `_decode_text_or_flag_
                # undecodable`'s own docstring. An undecodable tooling/
                # config XML now records `encoding_undecodable` and
                # skips the root-element guess entirely, rather than
                # risking a tier verdict computed from a corrupted root
                # element name.
                #
                # FIX ROUND 27 (twenty-third cold read, F3 MAJOR, wrong-
                # data): degrades_run=False here - this run cannot root-
                # sniff an undecodable file's own tier any more than it
                # can root-sniff a BINARY-excluded one (round 26b's own
                # ruling already refuses to degrade that twin), so
                # guessing toward degrading here would be the identical
                # round-16 dilution. scan_pipeline.py's own classification
                # override also now treats this path the same as a non-
                # degrading unsupported_language file (infrastructure,
                # not production) - see its own comment.
                xml_text = _decode_text_or_flag_undecodable(
                    data, rel, problems, degrades_run=False)
                if xml_text is None:
                    continue
                root_element = java_adapter.sniff_xml_root_element(xml_text)
                if root_element is None:
                    degrades_run = False
                    detail = (
                        "this XML file's root element could not be determined - failing "
                        "toward record-only rather than guessing a code-bearing shape"
                    )
                elif root_element in _TIER_2_XML_ROOT_ELEMENT_LABELS:
                    degrades_run = True
                    detail = (
                        "no bundled adapter recognizes this file's language - "
                        f"{_TIER_2_XML_ROOT_ELEMENT_LABELS[root_element]} "
                        f"(root element <{root_element}>) is a code-bearing "
                        "configuration shape, so this run degrades"
                    )
                else:
                    degrades_run = False
                    detail = (
                        f"no bundled adapter recognizes this file's language - its root "
                        f"element <{root_element}> is tooling/config XML, not code-bearing, "
                        "so this run does not degrade over it"
                    )
            elif rel_lower.endswith(tuple(_DEGRADING_CODE_EXTENSIONS)):
                # FIX ROUND 16b (BLOCKER 1, the B4 CALIBRATION): TIER 2 -
                # a closed, recognized CODE-BEARING shape this producer
                # simply has no adapter for yet (JSP/JSF-family, SQL,
                # Groovy/Kotlin/Scala on the JVM) - real application
                # source, genuinely "missed application code".
                degrades_run = True
                detail = (
                    "no bundled adapter recognizes this file's language - a recognized "
                    "code-bearing shape (not benign, not tooling/infra), so this run degrades"
                )
            else:
                # FIX ROUND 16b (BLOCKER 1, the B4 CALIBRATION): TIER 3 -
                # every OTHER non-benign, non-adapter-handled extension
                # (`Dockerfile`, `mvnw`/`mvnw.cmd`, a CI YAML,
                # `application.yml`, `.properties`, ...) is still
                # RECORDED (round 16's own win over the old closed list's
                # silent vanish stays closed - every one of these still
                # gets a WorkerProblem) but never degrades: a build/
                # tooling/infra/config file this producer cannot parse is
                # not "missed application code" the way a JSP or a Kotlin
                # source is, even though the registry has no adapter for
                # it either. Presumed BENIGN-OF-STATUS by default now -
                # the inverted allowlist's own win (never silently
                # un-recorded) stands regardless of this tier.
                degrades_run = False
                detail = (
                    "no bundled adapter recognizes this file's language - not on the "
                    "recognized code-bearing list, so this run does not degrade over it"
                )
            problems.append(WorkerProblem(
                reason_code="unsupported_language", relative_path=rel, detail=detail,
                degrades_run=degrades_run,
            ))

    # MICRO-ROUND 49 (M3 MAJOR, wrong-data): every web.xml this run
    # found, processed HERE - after the main loop above, so
    # `annotation_declared_servlet_names`/`annotation_declared_filter_
    # names` are fully populated from every .java file this run scans,
    # regardless of web.xml's own position in `relative_paths`. Same
    # decode/dispatch/problem-recording logic the main loop's own
    # web.xml branch always had - only the ordering moved.
    for rel, data in deferred_web_xml:
        # M9 (cold-read, PR-B fix round 3): parse_web_xml existed as a
        # producer with its own passing unit tests but no dispatch
        # anywhere in the pipeline - the suite reported a capability
        # (servlet-mapping routes) the pipeline did not actually have.
        # The approved item-3 relation scope already names this exact
        # case ("plain-XML web.xml servlet-mapping declarations when
        # trivially present") as in-scope; wiring it in, not deleting
        # it, is what that decision calls for. Same already-read-bytes
        # / same java_results channel as pom.xml's build edges.
        try:
            # FIX ROUND 26 (twenty-second cold read, F3 BLOCKER,
            # wrong-data): the same missing guard as pom.xml's own
            # decode site above - see `_decode_text_or_flag_
            # undecodable`'s own docstring.
            text = _decode_text_or_flag_undecodable(data, rel, problems)
            if text is None:
                continue
            # FIX ROUND 27 (twenty-third cold read, F4, mechanism
            # confirmed): parse_web_xml now also returns the paired
            # route-relation edges every web.xml-declared route/
            # filter publishes - threaded into JavaFileResult below,
            # the identical channel every annotation-based route's
            # own edge already flows through.
            web_entry_points, web_problems, web_edges, web_descriptor_name_conflicts = (
                java_adapter.parse_web_xml(
                    rel, text,
                    annotation_declared_servlet_names=annotation_declared_servlet_names,
                    annotation_declared_filter_names=annotation_declared_filter_names))
        except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
            problems.append(WorkerProblem(
                reason_code="parse_failed", relative_path=rel,
                detail=bounded_detail(f"{java_adapter.ADAPTER_NAME} adapter failed: {exc}")))
        else:
            # FIX ROUND 21 (seventeenth cold read, CR17-3 MAJOR):
            # <filter>/<listener> elements now record their own
            # unsupported_entry_point_shape problems - surfaced the
            # same way every other adapter problem already is.
            for adapter_problem in web_problems:
                problems.append(WorkerProblem(
                    reason_code=adapter_problem.reason_code, relative_path=rel,
                    detail=adapter_problem.detail,
                    qualified_name=adapter_problem.qualified_name,
                    # FIX ROUND 32 (twenty-eighth cold read, F7 LOW,
                    # JUDGE - taken): the SAME non-degrading exception
                    # as the .java-file conversion loop above - see
                    # that site's own comment for the reasoning.
                    # duplicate_route_target is only ever emitted from
                    # THIS web.xml parsing path, never from the .java
                    # annotation-route path, so this is the one real
                    # call site that needed it.
                    degrades_run=adapter_problem.reason_code != "duplicate_route_target",
                ))
            java_results[rel] = java_adapter.file_result_to_json(
                java_adapter.JavaFileResult(
                    entry_points=web_entry_points, problems=web_problems,
                    edges=web_edges,
                    descriptor_name_conflicts=web_descriptor_name_conflicts))
            # FIX ROUND 24 (micro-round 24b, item 1, wrong-data): the
            # SAME positive-evidence gate F1b already gives pom.xml -
            # a parse that succeeds but yields ZERO entry points AND
            # zero problems reads as source_understood satisfied
            # purely from absence. Honest for a genuinely EMPTY
            # <web-app/> (nothing declared at all - the Empty.java
            # treatment, a positive finding); dishonest for a web.xml
            # with real content that simply matches none of this
            # adapter's five modeled element families - exactly the
            # shape that would mask the NEXT web.xml parser
            # blindness the same way the pom.xml one did.
            if (
                not web_entry_points and not web_problems
                and not java_adapter.is_effectively_empty_web_xml(text)
            ):
                problems.append(WorkerProblem(
                    reason_code="no_web_xml_facts_extracted", relative_path=rel,
                    detail="the web.xml adapter parsed this file but extracted no "
                           "entry points and recorded no problems, over a root that "
                           "is not genuinely empty - an unrecognized or unmodeled "
                           "web.xml shape, not a legitimately empty descriptor",
                ))

    # MICRO-NOD 50c (F1 BLOCKER, completeness): a descriptor-independent
    # trigger for the SAME duplicate-annotation-name conflict detection
    # `deferred_web_xml`'s own loop above already runs whenever a
    # web.xml exists - see java_adapter.annotation_only_descriptor_
    # conflicts's own docstring for why this population (annotation-
    # only, no web.xml at all) was previously silent. Only when NO
    # web.xml was found at all: when one exists, parse_web_xml already
    # sees the FULL registry (worker.py always passes the complete
    # accumulated dict regardless of file order) and already correctly
    # detects the identical conflict - running this too would publish
    # the SAME conflict twice, under two different synthetic anchors.
    descriptor_name_conflicts: list[tuple[str, list[str]]] = []
    if not deferred_web_xml:
        for element, registry in (
            ("servlet", annotation_declared_servlet_names),
            ("filter", annotation_declared_filter_names),
        ):
            annotation_problems, annotation_conflicts = (
                java_adapter.annotation_only_descriptor_conflicts(registry, element=element)
            )
            for adapter_problem in annotation_problems:
                problems.append(WorkerProblem(
                    reason_code=adapter_problem.reason_code,
                    relative_path=_NO_DESCRIPTOR_SENTINEL_PATH,
                    detail=adapter_problem.detail,
                    qualified_name=adapter_problem.qualified_name,
                ))
            descriptor_name_conflicts.extend(annotation_conflicts)

    return WorkerResult(
        schema_version=WORKER_SCHEMA_VERSION, file_claims=claims, problems=problems,
        java_results=java_results, exclusions=exclusions,
        descriptor_name_conflicts=descriptor_name_conflicts,
    )


def _result_to_json(result: WorkerResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "exclusions": dict(result.exclusions),
        "file_claims": [
            {
                "relative_path": claim.relative_path,
                "byte_count": claim.byte_count,
            }
            for claim in result.file_claims
        ],
        "problems": [
            {
                "reason_code": problem.reason_code,
                "relative_path": problem.relative_path,
                "detail": problem.detail,
                "qualified_name": problem.qualified_name,
                "degrades_run": problem.degrades_run,
            }
            for problem in result.problems
        ],
        # reviewer-3 B-1 (PR-B delta review): this field was previously
        # missing here entirely - the worker computed adapter claims
        # correctly, held them on its in-memory result, but never emitted
        # them across the stdout channel, so the reader always
        # reconstructed an empty dict regardless of what was actually
        # parsed. A scan run through the REAL worker therefore reported
        # `complete` while silently carrying no adapter-derived units,
        # edges, or entry points at all. Every WorkerResult field must
        # round-trip - see test_worker_result_json_round_trip_preserves_
        # every_field, the reviewer's repro made permanent.
        "java_results": result.java_results,
        # MICRO-NOD 50c (F1): every WorkerResult field must round-trip
        # across the real sanitized-worker subprocess boundary - see
        # reviewer-3's own B-1 finding right above (java_results) for
        # why this is not merely a style nit; a field silently dropped
        # here means the REAL worker path loses it while every in-
        # process test calling process_paths() directly never would.
        "descriptor_name_conflicts": [
            [anchor, list(candidates)] for anchor, candidates in result.descriptor_name_conflicts
        ],
    }


def _result_from_json(payload: Any) -> WorkerResult:
    if not isinstance(payload, dict):
        raise WorkerError("worker output must be a JSON object")
    if payload.get("schema_version") != WORKER_SCHEMA_VERSION:
        raise WorkerError(
            f"worker output schema_version must be {WORKER_SCHEMA_VERSION}, got "
            f"{payload.get('schema_version')!r}")
    try:
        claims = [
            WorkerFileClaim(
                relative_path=item["relative_path"],
                byte_count=item["byte_count"],
            )
            for item in payload["file_claims"]
        ]
        problems = [
            WorkerProblem(
                reason_code=item["reason_code"],
                relative_path=item["relative_path"],
                detail=item["detail"],
                qualified_name=item.get("qualified_name"),
                degrades_run=item.get("degrades_run", True),
            )
            for item in payload["problems"]
        ]
        java_results = dict(payload.get("java_results", {}))
        exclusions = dict(payload.get("exclusions", {}))
        descriptor_name_conflicts = [
            (anchor, list(candidates))
            for anchor, candidates in payload.get("descriptor_name_conflicts", [])
        ]
    except (KeyError, TypeError) as exc:
        raise WorkerError(f"worker output is malformed: {exc}") from exc
    return WorkerResult(
        schema_version=WORKER_SCHEMA_VERSION, file_claims=claims, problems=problems,
        java_results=java_results, exclusions=exclusions,
        descriptor_name_conflicts=descriptor_name_conflicts,
    )


def _derive_child_import_root() -> str:
    """Derive the path the CHILD needs on its module search path to import
    ``agenttalk`` exactly as THIS process resolved it - computed from this
    interpreter's OWN already-resolved package location, never inherited
    from an environment variable.

    B-2 (reviewer-3, PR-B delta review): the sanitized environment
    deliberately never allowlists ``PYTHONPATH`` - both the reviewer and
    the lead flagged it as an injection vector, since a compromised or
    merely unexpected parent environment could point it anywhere. But
    without it, the child cannot import ``agenttalk`` at all when this
    process only resolves the package via a source-tree ``PYTHONPATH``
    (not a real install), so the worker could never start from a source
    checkout. The fix is a derived, validated route instead of ambient
    inheritance: this process already knows exactly where its OWN
    ``agenttalk`` package lives (``agenttalk.__file__``); the directory
    ABOVE that package is the root the child needs. Validated with the
    same resolve-under-known-root machinery every other confined path in
    this package uses, and re-confirmed to actually contain THIS worker
    module, before it is ever handed to the child - not merely trusted
    because it was computed here.
    """
    import agenttalk

    import_root = Path(agenttalk.__file__).resolve().parent.parent
    try:
        worker_module = resolve_under_root(
            "agenttalk/comprehension/worker.py", root=import_root,
            label="derived child import root",
        )
    except EnvelopeError as exc:
        raise WorkerError(
            f"could not derive a valid child import root from this process's own "
            f"agenttalk package location ({import_root}): {exc}") from exc
    if not worker_module.is_file():
        raise WorkerError(
            f"derived child import root {import_root} does not contain "
            f"agenttalk.comprehension.worker - refusing to launch the worker from an "
            f"unverified location")
    return str(import_root)


def _worker_subprocess_argv() -> list[str]:
    """The exact argv :func:`run_sanitized_worker` launches the child
    with. Factored out to its own function (N6, third cold read, fix
    round 5) so ``test_comprehension_network_deny.py``'s own hand-built
    replica of this launch - it cannot call ``run_sanitized_worker``
    itself, since that has no sudo/unshare wrapping point - imports and
    calls this SAME function instead of maintaining its own separate
    copy of the flag list. Round 4's ``-s``/``-S`` addition here silently
    drifted out of sync with that test's copy until a cold read caught
    it; a shared function makes that class of drift structurally
    impossible instead of merely reviewable.

    -s (skip the user site-packages directory) and -S (skip the `site`
    module's own startup entirely - site-packages/.pth processing AND
    sitecustomize.py/usercustomize.py, which would otherwise execute
    inside this sanitized process) close exactly the channel the
    module's own claim of a closed input boundary is about. Never the
    fuller -I (isolated mode): -I also strips PYTHONPATH, which this
    launch sets EXPLICITLY and deliberately (_derive_child_import_root,
    above) so the child can resolve `agenttalk` from a source checkout -
    -I would undo the one thing this whole function exists to arrange.
    """
    return [sys.executable, "-s", "-S", "-m", "agenttalk.comprehension.worker"]


def run_sanitized_worker(
    root: Path, relative_paths: list[str], *, timeout_seconds: float = _WORKER_TIMEOUT_SECONDS,
) -> WorkerResult:
    """Launch the worker as a child process with :func:`sanitized_worker_env`
    - never the parent's own, unfiltered ``os.environ`` - and feed it
    ``root``/``relative_paths`` over stdin/stdout only. Raises
    :class:`WorkerError` if the process cannot be started, times out, exits
    non-zero, or returns output that does not parse as a valid result;
    never raised for one file's own read/parse failure (see
    :class:`WorkerProblem`).

    The child's ``PYTHONPATH`` is set explicitly here, to a value THIS
    function derives and validates itself (:func:`_derive_child_import_root`)
    - never inherited from the caller's own environment, which
    :func:`sanitized_worker_env`'s allowlist deliberately excludes it from
    (B-2, reviewer-3, PR-B delta review)."""
    env = sanitized_worker_env()
    env["PYTHONPATH"] = _derive_child_import_root()
    payload = json.dumps({"root": str(root), "relative_paths": list(relative_paths)})
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            _worker_subprocess_argv(),
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError(f"the sanitized worker could not be started or timed out: {exc}") from exc
    if completed.returncode != 0:
        raise WorkerError(
            f"the sanitized worker exited with status {completed.returncode}: "
            f"{completed.stderr.strip()}")
    try:
        payload_out = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WorkerError(f"the sanitized worker returned malformed output: {exc}") from exc
    return _result_from_json(payload_out)


def _main(argv: list[str]) -> int:
    """The worker process's own entrypoint (``python -m
    agenttalk.comprehension.worker``). Reads one JSON object from stdin
    (``{"root": str, "relative_paths": [str, ...]}``), writes one JSON
    result object to stdout, and takes no other input - no argv beyond
    nothing, no environment variable, no network."""
    del argv
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"malformed worker input: {exc}"}), file=sys.stderr)
        return 2
    if not isinstance(payload, dict) or "root" not in payload or "relative_paths" not in payload:
        print(json.dumps({"error": "worker input must have root and relative_paths"}), file=sys.stderr)
        return 2
    result = process_paths(Path(payload["root"]), list(payload["relative_paths"]))
    sys.stdout.write(json.dumps(
        _result_to_json(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
