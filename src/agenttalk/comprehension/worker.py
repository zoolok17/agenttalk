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
import subprocess  # nosec B404 - launches only the bundled worker module, argv fixed, shell disabled
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import java as java_adapter
from .envelope import EnvelopeError, resolve_under_root
from .errors import ComprehensionError, bounded_detail, bounded_os_error_detail

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
    stay exactly as silent as they are today."""
    lower = relative_path.lower()
    return lower.endswith(tuple(_ADAPTER_EXTENSIONS)) or lower.endswith(
        tuple(_DEGRADING_CODE_EXTENSIONS))


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
                text = data.decode("utf-8-sig", errors="replace")
                result = adapter.parse_java_source(rel, text)
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=bounded_detail(f"{adapter.ADAPTER_NAME} adapter failed: {exc}")))
            else:
                java_results[rel] = adapter.file_result_to_json(result)
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
                text = data.decode("utf-8-sig", errors="replace")
                pom_units, build_edges, profile_scoped_dependency_count = (
                    java_adapter.parse_maven_pom(rel, text))
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
                    java_adapter.JavaFileResult(units=pom_units, edges=build_edges))
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
        elif rel_name_lower == "web.xml":
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
                text = data.decode("utf-8-sig", errors="replace")
                web_entry_points = java_adapter.parse_web_xml(rel, text)
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=bounded_detail(f"{java_adapter.ADAPTER_NAME} adapter failed: {exc}")))
            else:
                java_results[rel] = java_adapter.file_result_to_json(
                    java_adapter.JavaFileResult(entry_points=web_entry_points))
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
                root_element = java_adapter.sniff_xml_root_element(
                    data.decode("utf-8-sig", errors="replace"))
                if root_element is None:
                    degrades_run = False
                    detail = (
                        "this XML file's root element could not be determined - failing "
                        "toward record-only rather than guessing a code-bearing shape"
                    )
                elif root_element == _SPRING_BEAN_XML_ROOT_ELEMENT:
                    degrades_run = True
                    detail = (
                        "no bundled adapter recognizes this file's language - Spring bean "
                        f"XML (root element <{root_element}>) is a code-bearing "
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

    return WorkerResult(
        schema_version=WORKER_SCHEMA_VERSION, file_claims=claims, problems=problems,
        java_results=java_results, exclusions=exclusions,
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
    except (KeyError, TypeError) as exc:
        raise WorkerError(f"worker output is malformed: {exc}") from exc
    return WorkerResult(
        schema_version=WORKER_SCHEMA_VERSION, file_claims=claims, problems=problems,
        java_results=java_results, exclusions=exclusions,
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
