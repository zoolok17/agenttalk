"""``modules.json`` record assembly (DESIGN-55-comprehension-plane.md,
Artifact 1: module inventory).

This module assembles in-memory records only - the JSON envelope, ceiling
enforcement, and publish/staging wiring are a later step (PR-B item 9),
reusing PR-A's existing envelope/ceilings/publish machinery unchanged.

Scope simplification for this slice (Java-only adapter, flagged for
review, not a blocking fork): no package/module/service-level container
unit is synthesized above a file's own declared types - the design
permits an adapter to identify "a package, module, component, or service"
additionally containing file units, but the bundled Java adapter (item 3)
only identifies per-type ``component`` units this slice. A future adapter
or a Java package-grouping producer can add that container tier without
changing this module's shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from . import digests
from .adapters import java as java_adapter
from .discovery import DiscoveryResult

MODULES_ARTIFACT_TYPE = "agenttalk.comprehension.modules"
MODULES_SCHEMA_VERSION = 1

#: FIX ROUND 28 (twenty-fourth cold read, F3 JUDGE, declared - the SAME
#: ruling as F2 above, stated in-artifact): round 26b's own binary-
#: excluded ruling ("record but don't degrade, this run cannot know the
#: file's own tier") is UNCHANGED and correctly extends to a non-adapter
#: ``.xml`` file this run could not even DECODE - but nothing in-artifact
#: previously declared that a `complete` run status may still coexist
#: with at least one unreadable, non-code-bearing-classified XML file
#: whose own content this producer never actually saw. Declared here
#: rather than left implicit: `complete` means "no evidence of a
#: DEGRADING gap was found," never "every file this run touched was
#: positively confirmed benign" - the exact same "absence of evidence
#: is not evidence of absence" honesty `ASSESSMENT_STATE_CAVEAT`/
#: `PROVENANCE_CAVEAT` already establish for their own gaps.
CLASSIFICATION_CAVEAT = (
    "a `complete` run status does not warrant that every non-adapter-handled "
    "XML file this run encountered was positively confirmed benign - an "
    "encoding-undecodable XML file (this producer could not decode its "
    "content to sniff its own root element, the same 'cannot know the tier' "
    "epistemics a binary-excluded XML file already gets) records a real, "
    "visible problem (`encoding_undecodable`) and publishes NO decided "
    "classification (an empty list, never a guessed one) but does not "
    "degrade the run - `complete` here means 'no evidence of a degrading "
    "gap was found', not 'every file was confirmed'."
)

_LANGUAGE_BY_EXTENSION = {".java": "java"}
#: M-2 (second cold read, fix round 4): pom.xml/web.xml go THROUGH the
#: java adapter package (build_dependencies/build_features already
#: consume their edges/entry points) but named no language of their own -
#: _language_for_path fell through to "unknown" purely by extension,
#: making a file the adapter demonstrably understood indistinguishable
#: from one no adapter has ever touched. Named here, by basename (these
#: are fixed, well-known filenames, not a language-by-extension family).
_LANGUAGE_BY_BASENAME = {"pom.xml": "xml", "web.xml": "xml"}
#: FIX ROUND 15 (eleventh cold read, F3 MAJOR, wrong-data): the ORIGINAL
#: combined pattern classified a bare ``/test/`` package segment with NO
#: corroboration at all (the same bug class CR10-7 already fixed for the
#: adapter's own NAME heuristic) - a package literally named ``test``
#: (common in lab/QA-domain legacy code) published classification=[test]
#: with zero supporting evidence. This module has no per-file import
#: evidence available to corroborate a bare ``/test/`` segment the way
#: ``adapters.java._classify`` now can (a same-file test-framework
#: import) - so only the real build-convention root qualifies here;
#: a bare ``/test/`` segment with nothing else to corroborate it now
#: stays production, never a guess.
#:
#: FIX ROUND 15b (reviewer-3's MINOR 2, measured on an Ant layout): a
#: REPOSITORY-ROOT ``test/`` or ``tests/`` directory is a build
#: convention exactly like ``src/test`` (the classic pre-Maven Ant
#: layout) - sufficient alone. Anchored to the very START of the path
#: ONLY - the bug F3 fixed was a test segment INSIDE a package path,
#: never the repository root itself; root-anchoring does not reopen it.
_TEST_SOURCE_ROOT_SEGMENT = re.compile(r"(?:^|/)src/test/|^tests?/")


@dataclass(frozen=True)
class ModuleRecord:
    unit_id: str
    kind: str
    display_name: str
    language: str
    paths: list[str]
    source_digests: dict[str, str]
    classification: list[str]
    container_unit_id: str | None
    producers: list[dict[str, Any]]
    conflict_id: str | None = None
    #: FIX ROUND 29 (twenty-fifth cold read, F1 BLOCKER): names WHICH
    #: conflict produced ``conflict_id`` - ``"duplicate_qualified_name"``
    #: (two units declaring the identical fully-qualified name across
    #: files) or ``"duplicate_descriptor_name"`` (a web.xml servlet-name/
    #: filter-name declared twice with different class values, entirely
    #: local to one descriptor). Both conflict kinds route through the
    #: SAME generic readiness override (any unit carrying a conflict_id
    #: reports unknown on its dependent signals) - but that override's
    #: own published ``reason_code`` must name the REAL cause, never
    #: reuse ``duplicate_qualified_name`` for a conflict that is not
    #: actually an FQN collision. ``None`` whenever ``conflict_id`` is.
    conflict_kind: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: M-2 (third cold read, fix round 5): CLOSES THE CLASS - the THIRD
    #: instance of a file that no adapter ever actually analyzed
    #: publishing as source_understood=satisfied anyway (round 3: parse
    #: failures; round 4: no-adapter-for-language; round 5: adapter-work
    #: resource cap). Rather than adding a fourth manually-threaded
    #: negative flag for the next such case, this carries the worker's
    #: OWN reason_code (``"parse_failed"``, ``"resource_limit"``,
    #: ``"path_excluded"``, or any future one) whenever a "file" unit has
    #: NO positive adapter evidence (no entry in ``java_results``) AND the
    #: worker recorded a problem for it - readiness derives
    #: ``source_understood`` from the PRESENCE of positive evidence, never
    #: from the absence of a specific, named failure kind, so a future
    #: fifth reason the worker invents needs no readiness-side change at
    #: all to be seen as unknown.
    adapter_problem_reason: str | None = None
    #: MINOR 5 (sixth cold read, fix round 9): round 8's own N3 fix
    #: joined every distinct reason the worker recorded for one path
    #: into a SINGLE compound string ("no_types_extracted+resource_
    #: limit") and published it as ``adapter_problem_reason`` - a value
    #: OUTSIDE the closed, enumerated reason-code vocabulary every
    #: reader of that field expects (readiness's own
    #: ``f"adapter_{reason}"`` construction, and any future consumer
    #: matching against the known set). ``adapter_problem_reason`` stays
    #: a single enumerated value (the FIRST reason, sorted) again; the
    #: full sorted, deduplicated list - lossless, just as round 8
    #: intended - lives here instead, a separate list-valued field.
    adapter_problem_reasons: list[str] = field(default_factory=list)
    #: FIX ROUND 15 (eleventh cold read, N2 MINOR): a consumer previously
    #: had no way to recover a "component"-kind unit's own FULLY
    #: QUALIFIED name from this artifact - only `display_name` (the
    #: rightmost simple-name segment), which a same-named class in a
    #: different package makes ambiguous on its own. `None` for a
    #: "file"-kind unit (no qualified name of its own) and for any
    #: default-classified non-Java/no-adapter-evidence file.
    qualified_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "language": self.language,
            "paths": sorted(self.paths),
            "source_digests": dict(sorted(self.source_digests.items())),
            "classification": sorted(self.classification),
            "container_unit_id": self.container_unit_id,
            "producers": self.producers,
            "conflict_id": self.conflict_id,
            "conflict_kind": self.conflict_kind,
            "evidence": self.evidence,
            "adapter_problem_reason": self.adapter_problem_reason,
            "adapter_problem_reasons": self.adapter_problem_reasons,
            "qualified_name": self.qualified_name,
        }


def module_record_from_json(payload: dict[str, Any]) -> ModuleRecord:
    """The inverse of :meth:`ModuleRecord.to_json` - reconstructs a record
    from a persisted ``modules.json`` row, e.g. for ``report``/``status``
    to feed back into :func:`projector.project_comprehension`."""
    return ModuleRecord(
        unit_id=payload["unit_id"], kind=payload["kind"], display_name=payload["display_name"],
        language=payload["language"], paths=list(payload["paths"]),
        source_digests=dict(payload["source_digests"]),
        classification=list(payload["classification"]),
        container_unit_id=payload["container_unit_id"], producers=list(payload["producers"]),
        conflict_id=payload.get("conflict_id"), conflict_kind=payload.get("conflict_kind"),
        evidence=list(payload.get("evidence", [])),
        adapter_problem_reason=payload.get("adapter_problem_reason"),
        adapter_problem_reasons=list(payload.get("adapter_problem_reasons", [])),
        qualified_name=payload.get("qualified_name"),
    )


def _language_for_path(relative_path: str) -> str:
    """FIX ROUND 37 (thirty-first cold read, F4 MAJOR, wrong-data): this
    used to match case-SENSITIVELY (a plain ``endswith``/``in`` check on
    the path/basename verbatim) while worker.py's own dispatch (the
    thing that decides whether a file is actually parsed at all) matches
    case-INSENSITIVELY (``rel_lower``/``rel_name_lower``, lowercased
    before every comparison) - a ``.JAVA`` file was PARSED as Java (a
    real route extracted, a real unit built) but published
    ``language: "unknown"``, a contradiction between two facts about
    the identical file in the SAME run. One case policy now, aligned
    with the worker: lowercase before matching, exactly like worker.py
    already does."""
    lower = relative_path.lower()
    basename = lower.rsplit("/", 1)[-1]
    if basename in _LANGUAGE_BY_BASENAME:
        return _LANGUAGE_BY_BASENAME[basename]
    for ext, lang in _LANGUAGE_BY_EXTENSION.items():
        if lower.endswith(ext):
            return lang
    return "unknown"


#: FIX ROUND 32 (twenty-eighth cold read, F3 MAJOR, wrong-data): the OLD
#: discriminator for "infrastructure" classification among tier-3
#: (recorded, non-degrading ``unsupported_language``) paths was TIER
#: MEMBERSHIP ITSELF - every tier-3 path got "infrastructure", with no
#: further distinction. Tier 3 is a DEGRADATION calibration (worker.py's
#: own three-tier rule: "never-incidental application/database estate" vs
#: everything else), not a classification one - it deliberately includes
#: real, polyglot APPLICATION source this producer just has no adapter
#: for (an Express/Node service's own ``.js``, a Python ETL job's own
#: ``.py`` - round 17b's own measured "routinely incidental" exclusion
#: from tier 2) alongside genuine build/tooling/infra files (a
#: Dockerfile, a CI YAML, a build wrapper). Both landed in the identical
#: bucket as a `.gitignore` - a real service's own source file classified
#: the SAME as a project's own tooling plumbing.
#:
#: The discriminator instead is this closed, PROVISIONAL basename/
#: extension/well-known-CI-path allowlist - the SAME closed-set
#: convention every other calibration constant in this package already
#: follows (documented, expected to grow, safe direction to be wrong in
#: is UNDER-claiming). A tier-3 path matching NEITHER this list NOR the
#: benign-extension/basename allowlist (worker.py's own, which never even
#: reaches this branch - see ``_derive_classification`` below) gets an
#: EMPTY classification, never a guessed "infrastructure" - the same
#: "no decided value" discipline round 28's own encoding_undecodable fix
#: already established for a file this producer never even read.
_CONFIDENT_INFRASTRUCTURE_BASENAMES = frozenset({
    "dockerfile", "makefile", "jenkinsfile", "vagrantfile", "procfile",
    "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat",
    ".travis.yml", ".gitlab-ci.yml", ".drone.yml", "appveyor.yml", "azure-pipelines.yml",
})
#: ``.sh``/``.bash`` (round 23's own ratified ``release.sh`` shape,
#: reconfirmed here rather than regressed): a shell script is essentially
#: ALWAYS a build/release/tooling script in an ordinary repository, never
#: a genuine polyglot application SERVICE the way a ``.js``/``.py`` file
#: can be - unlike those two, it never needed round 17b's own "routinely
#: incidental" carve-out to begin with.
_CONFIDENT_INFRASTRUCTURE_EXTENSIONS = frozenset({".properties", ".sh", ".bash"})
#: A lowercase POSIX-spelled substring, checked anywhere in the path -
#: both are well-known, CI-specific directory conventions (arbitrary
#: basenames live inside them, e.g. ``.github/workflows/build.yml``),
#: never a claim about an arbitrary ``.yml``/``.yaml`` file elsewhere in
#: the tree (an ``application.yml`` is real application configuration,
#: not confidently infrastructure).
_CONFIDENT_INFRASTRUCTURE_PATH_SEGMENTS = (".github/workflows/", ".circleci/")


#: FIX ROUND 35 (twenty-ninth cold read, F8 LOW, JUDGE - argued, not
#: churned): the reader measured two apparent asymmetries - ``release.sh``
#: classifies ``infrastructure`` while a sibling ``release.py`` classifies
#: empty; a ``Dockerfile`` classifies ``infrastructure`` while a top-level
#: ``.github-ci.yml`` classifies empty. Both are the SAME one rule this
#: closed set has followed since round 32, stated explicitly here rather
#: than left implicit: a name/extension/path segment earns membership
#: only when it is MANDATED by one specific, real tool or platform
#: convention - a name whose mere presence, unlike ordinary source, is
#: already proof of a build/release/CI role, independent of this run ever
#: reading its content. ``.sh``/``.bash`` qualify under that rule (round
#: 23's own reconfirmed reasoning: a shell script is essentially always a
#: build/release/tooling script in an ordinary repository); ``.py``/``.js``
#: do NOT (round 17b's own "routinely incidental" carve-out - either can
#: just as easily be a genuine polyglot application SERVICE, so its mere
#: extension proves nothing about its role). A real GitHub Actions
#: workflow file already earns ``infrastructure`` through the PATH-SEGMENT
#: rule below (``.github/workflows/`` is itself the platform-mandated
#: convention - the file's own basename inside it is arbitrary); a
#: differently-shaped top-level ``.github-ci.yml`` is not a filename any
#: platform mandates, so it correctly stays OUT under this same rule, the
#: same way an arbitrary ``application.yml`` does. Neither asymmetry is a
#: gap in this set - both are this one rule applied consistently, and
#: BOTH is the important word: widening the extension rule to ``.py``/
#: ``.js`` (real service languages) or the basename rule to an arbitrary
#: CI-flavored name (no platform mandate) would break the rule, not fix an
#: inconsistency in it.
def _is_confident_infrastructure_path(relative_path: str) -> bool:
    posix_lower = relative_path.replace("\\", "/").lower()
    name_lower = posix_lower.rsplit("/", 1)[-1]
    return (
        name_lower in _CONFIDENT_INFRASTRUCTURE_BASENAMES
        or name_lower.endswith(tuple(_CONFIDENT_INFRASTRUCTURE_EXTENSIONS))
        or any(segment in posix_lower for segment in _CONFIDENT_INFRASTRUCTURE_PATH_SEGMENTS)
    )


def _default_classification(relative_path: str) -> str:
    if _TEST_SOURCE_ROOT_SEGMENT.search(relative_path.replace("\\", "/")):
        return "test"
    return "production"


def _derive_classification(
    relative_path: str, *, java_result_is_none: bool, reasons: list[str],
    non_degrading_unsupported_language_paths: frozenset[str],
) -> str | None:
    """FIX ROUND 23 (nineteenth cold read, F3 MAJOR, wrong-data): every
    non-test file published ``classification=production`` regardless
    of whether this SAME run recorded it as a genuine build/tooling/
    infra file this producer was never going to model at all (README,
    LICENSE, Dockerfile, mvnw, a CI YAML, a release script) - the
    design's own vocabulary names ``infrastructure`` as an alternative
    to production/test, and #208 groups by classification; a consumer
    scoping migration work by ``classification==production`` pulled in
    the README. The evidence already exists in-run: worker.py's own
    non-degrading ``unsupported_language`` problem (TIER 3 - "not on
    the recognized code-bearing list, so this run does not degrade
    over it"), or the complete absence of any worker problem at all for
    a file matching its own benign-extension/basename allowlist -
    classification simply never consulted either.

    FIX ROUND 27 (twenty-third cold read, F3 MAJOR, wrong-data):
    ``non_degrading_unsupported_language_paths`` (scan_pipeline.py, the
    parameter name unchanged) now ALSO includes the non-degrading half
    of ``encoding_undecodable`` for a non-adapter-handled ``.xml`` file
    (worker.py's own xml-root-sniff decode site) - a binary-excluded and
    an encoding-undecodable file are epistemically identical (neither
    can be root-sniffed to determine its own tier), so this same
    ``infrastructure`` derivation now applies to both, not just the
    binary-excluded twin. This function itself needed no change - it
    already trusts whatever this SET contains, generically.

    DECIDED (reviewer-3 ratifies): derives ``infrastructure`` for this
    non-degrading/benign-non-code case ONLY. A TIER-2 file (a JSP, a
    Kotlin source, a Spring-bean-XML config, ...) is genuinely UNMODELED
    APPLICATION code, not infrastructure - worker.py's own
    ``degrades_run=True`` on that identical reason code is the exact
    discriminator this run already computed, so it is deliberately
    excluded here and keeps its existing production/test classification
    unchanged. ``pom.xml``/``web.xml`` are unaffected either way - both
    are genuinely adapter-handled (``java_result`` is never ``None`` for
    them), never carrying ``unsupported_language`` at all.

    A file under a recognized TEST path (``_default_classification``
    already returns ``"test"``) is deliberately never overridden here -
    a test fixture (``test/fixtures/data.txt``) is genuinely part of
    the test estate, migration-relevant to test-coverage decisions the
    same way a real test source file is, not a build/tooling concern
    the way a root-level README or a CI YAML is - checked FIRST,
    unconditionally.

    MICRO-ROUND 23b (reviewer-3 delta, R3 note, ratified with residual):
    ``"infrastructure"`` for a README/LICENSE is the CLOSEST AVAILABLE
    value in this producer's closed classification vocabulary
    (production/test/infrastructure), not an exact one - a future
    ``"documentation"`` member is where prose-only files like these
    would actually belong. Named explicitly so the next reader does not
    conclude the vocabulary was examined and found sufficient; not
    extended this slice.

    FIX ROUND 28 (twenty-fourth cold read, F2 BLOCKER, round-27
    REGRESSION, wrong-data): round 27's own F3 fix widened
    ``non_degrading_unsupported_language_paths`` to ALSO include a non-
    adapter-handled ``.xml`` file this run could not even DECODE
    (``encoding_undecodable``) - correct for the DEGRADE question (this
    run cannot know the file's own tier, the same "cannot know" ruling
    round 26b already made for a binary-excluded twin), but WRONG for
    CLASSIFICATION: publishing ``["infrastructure"]`` is a CONFIDENT,
    DECIDED claim about a file this producer admits it never read at
    all - the one-byte reader repro is exact (the SAME 96-byte Spring
    bean XML, one byte flipped to make it undecodable, flips
    classification from `production` (still degrading, correctly
    unaffected) to `infrastructure` (a decided value with zero evidence
    behind it) purely because decoding failed). An `encoding_
    undecodable` reason is checked FIRST and unconditionally here -
    BEFORE the non-degrading-paths lookup below - returning an EMPTY
    classification list: the closed vocabulary
    (production/test/infrastructure) has NO ``unknown`` member and must
    not grow one (a frozen-vocabulary rule, the same discipline the
    readiness signal states already follow structurally), so "no
    decided value" is expressed as the absence of any classification at
    all, never a guessed or invented one.

    MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R2, wrong-data):
    this docstring previously claimed the binary-excluded root-sniffed-
    XML twin (``binary_excluded_root_sniffed_xml``, round 26b) "never
    reaches this function at all... honest BY OMISSION" - MEASURED
    FALSE: ``CLASSIFICATION_CAVEAT`` itself already claimed the two
    twins share "the same epistemics," which they did NOT while one
    published a real unit (empty classification, six visible unknown
    readiness rows) and the other published no unit at all. Fixed the
    unit-level HALF that was actually missing (scan_pipeline.py now
    synthesizes a file-kind unit for a binary-excluded root-sniffed-XML
    path too, from the content_digest discovery.py already had in hand
    at the exclusion site) - ``binary_excluded_root_sniffed_xml`` now
    reaches this exact same check, right alongside `encoding_
    undecodable`, both meaning the identical "this run cannot know the
    file's own tier" fact for the identical reason (neither was ever
    decoded/root-sniffed), so both get the identical empty-
    classification treatment. The caveat's own sentence is true now,
    not merely asserted.

    FIX ROUND 32 (twenty-eighth cold read, F3 MAJOR, wrong-data,
    CORRECTION): every paragraph above still describes the intent
    correctly, but the ``non_degrading_unsupported_language_paths``
    branch below used to derive ``"infrastructure"`` from TIER
    MEMBERSHIP ALONE - true for a Dockerfile/CI-YAML/build-wrapper, but
    ALSO true for a polyglot repo's own real, unmodeled application
    source (an Express service's ``.js``, a Python ETL job's ``.py`` -
    round 17b's own measured "routinely incidental" exclusion from tier
    2) - a real service source file classified the identical
    "infrastructure" as a `.gitignore`. See
    ``_is_confident_infrastructure_path`` for the closed allowlist that
    now discriminates within this branch; a tier-3 path matching neither
    it nor worker.py's own benign allowlist gets no classification at
    all.
    """
    default = _default_classification(relative_path)
    if default == "test":
        return default
    if "encoding_undecodable" in reasons or "binary_excluded_root_sniffed_xml" in reasons:
        return None
    if relative_path in non_degrading_unsupported_language_paths:
        # FIX ROUND 32 (F3 MAJOR): tier-3 membership alone is no longer
        # the discriminator - see _is_confident_infrastructure_path's own
        # docstring. A tier-3 path that does not match this closed
        # allowlist (a polyglot service's own real source this producer
        # merely has no adapter for) gets NO classification at all,
        # never a guessed "infrastructure".
        if _is_confident_infrastructure_path(relative_path):
            return "infrastructure"
        return None
    if java_result_is_none and not reasons:
        # No worker problem recorded AT ALL - worker.py's own inverted
        # allowlist (round 16's own fix) means an unenumerated,
        # non-adapter-handled extension ALWAYS gets "unsupported_
        # language" recorded; reaching here with nothing recorded can
        # only happen for a file matching its own BENIGN extension/
        # basename set (a README, a .gitignore, a lockfile, ...) -
        # genuinely never tier-2, since tier-2 always gets recorded.
        return "infrastructure"
    return default


def _producer(
    *, name: str, version: int, source_digest: str | None,
    basis: str, rule_version: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "producer": name, "producer_version": version, "basis": basis,
        "source_digest": source_digest,
    }
    if rule_version is not None:
        entry["rule_version"] = rule_version
    return entry


def _parent_qualified_name(qualified_name: str, known_names: set[str]) -> str | None:
    if "." not in qualified_name:
        return None
    candidate = qualified_name.rsplit(".", 1)[0]
    return candidate if candidate in known_names else None


def build_modules(
    discovery: DiscoveryResult, java_results: dict[str, java_adapter.JavaFileResult],
    *, worker_problem_reasons_by_path: dict[str, list[str]] | None = None,
    worker_problem_reasons_by_unit: dict[tuple[str, str], list[str]] | None = None,
    worker_problem_reasons_by_qualified_name: dict[str, list[str]] | None = None,
    worker_declaring_paths_by_qualified_name: dict[str, set[str]] | None = None,
    non_degrading_unsupported_language_paths: frozenset[str] | None = None,
    binary_excluded_root_sniffed_xml_digests: dict[str, str] | None = None,
    binary_excluded_code_bearing_digests: dict[str, str] | None = None,
    descriptor_name_conflicts: list[tuple[str, list[str]]] | None = None,
) -> list[ModuleRecord]:
    """``java_results`` maps a ``.java`` file's relative path to its
    already-parsed :class:`~.adapters.java.JavaFileResult` (item 3) -
    parsing happens once, upstream; this function only assembles records
    from what was already extracted.

    ``worker_problem_reasons_by_path`` names every relative path the worker
    recorded ANY problem for, with EVERY distinct reason_code it recorded
    for that path, sorted (M-2, third cold read, fix round 5 - closes the
    class B3/round-3's ``parse_failed_paths`` started but only covered one
    reason: a file absent from ``java_results`` is either "no adapter for
    this extension" (a confident negative, no problem recorded at all) or
    "the adapter was eligible but has no positive result" for SOME worker
    reason - parse failure, the per-file resource cap, or a re-confinement
    rejection - all genuinely unknown, never a confident understood
    merely because the extension maps to a known language. Broadcasts to
    EVERY unit declared in that path - correct for a genuinely file-wide
    problem (a parse failure has no single "owning" type), never for one
    an adapter can pin to a specific declared type.

    ``worker_problem_reasons_by_unit`` (FIX ROUND 13c, reviewer-3's part
    1 on round 13b) is the narrower counterpart: keyed by
    ``(relative_path, qualified_name)``, for a worker problem an adapter
    DID attribute to one specific declared type (e.g. an unrecognized
    cli_main-like method belongs to its own enclosing type) - applied
    ONLY to the one matching unit's own record, never its siblings in
    the same file, and never the file-kind record itself (the concept
    of "this exact type's own entry-point signature" does not apply to
    the file as a whole the way "was this file's content parseable" does).

    MINOR 5 (sixth cold read, fix round 9): a path can legitimately have
    MORE than one distinct reason recorded (round 8's N3) - the closed,
    single-value ``adapter_problem_reason`` vocabulary takes only the
    FIRST (sorted) of them; the full list is never discarded, published
    separately as ``adapter_problem_reasons``.

    ``worker_problem_reasons_by_qualified_name`` (FIX ROUND 21c,
    reviewer-3's re-delta, THE CARRY) is the CROSS-FILE counterpart to
    ``worker_problem_reasons_by_unit`` above: keyed by bare qualified
    name alone, for a worker problem recorded against a DIFFERENT file
    than the one declaring the type it names - a web.xml ``<listener>``
    element's own problem is correctly recorded against web.xml's own
    path, naming a class declared in some other ``.java`` file entirely,
    so the tuple-keyed map above can never match it. Applied via
    ``_attribute_cross_file_entry_point_reasons`` below, after every
    record is built - resolved the same "unambiguous or not resolved at
    all" way ``features_artifact.py``'s own owner resolution already
    treats a qualified name.

    FIX ROUND 37 (thirty-first cold read, F3 MAJOR, wrong-data,
    CORRECTION): the paragraph above previously claimed "web.xml has no
    unit of its own to broadcast to" - MEASURED FALSE: modules.json
    already publishes a real file-kind unit for web.xml, the same as
    for every other file this run enumerates. That false premise is
    exactly what let the zero-in-scan-claimant case (a jar-shipped
    listener class, the NORMAL real-world shape) fall through to
    nothing - see ``worker_declaring_paths_by_qualified_name`` and
    ``_attribute_cross_file_entry_point_reasons``'s own updated
    docstring for the fallback this round adds.

    ``worker_declaring_paths_by_qualified_name`` (FIX ROUND 37) tracks
    each qualified name's own DECLARING path(s) (web.xml's own path, for
    the ``<listener>`` shape) alongside the reasons above - needed
    because a qualified name resolving to ZERO in-scan claimants must
    still reach a real unit somewhere, and the only unit this run can
    honestly attribute it to is the file that declared the reference in
    the first place.

    ``binary_excluded_root_sniffed_xml_digests`` (MICRO-ROUND 28b,
    reviewer-3 delta on ``02c6b30``, R2, wrong-data) maps a binary-
    excluded, non-adapter-handled ``.xml`` file's own relative path to
    the content_digest discovery.py already computed at its own
    exclusion site - these paths are NOT in ``discovery.files`` (they
    were excluded before ever reaching that list), so they need their
    own separate, additive record-construction pass rather than routing
    through the main loop below. Each publishes a real ``ModuleRecord``
    (empty classification, ``binary_excluded_root_sniffed_xml`` in its
    own ``adapter_problem_reasons``) - the SAME visible, empty-
    classification form the encoding-undecodable twin already gets,
    closing the gap ``CLASSIFICATION_CAVEAT``'s own sentence had wrongly
    assumed was already closed.

    ``binary_excluded_code_bearing_digests`` (FIX ROUND 31, twenty-seventh
    cold read, F3 MINOR, completeness - WIDENED by FIX ROUND 32, twenty-
    eighth cold read, F8 LOW, JUDGE, taken) is the SAME additive shape as
    ``binary_excluded_root_sniffed_xml_digests`` above, for every binary/
    UTF-16-excluded file this run would otherwise have tried to understand
    as code (``worker.is_a_code_bearing_extension_worth_degrading_when_
    silently_excluded`` - adapter-handled ``.java``, ``pom.xml``/
    ``web.xml``, AND every tier-2 ``_DEGRADING_CODE_EXTENSIONS`` shape:
    ``.jsp``, ``.kt``, ...) - none of these ever reach ``worker.is_a_root_
    sniffed_xml_extension`` (that predicate is for the genuine TIER
    AMBIGUITY case, never for something already known to be code-bearing),
    so they previously got NO modules.json UNIT at all when excluded, even
    though round 18's own F6 fix already records a real, DEGRADING problem
    for them (``binary_excluded_code_bearing_file`` - these ARE code-
    bearing by definition, unlike the root-sniffed case's genuine tier
    ambiguity).

    ROUND 31 ITSELF only ever populated this for the two adapter-handled
    XML basenames (``pom.xml``/``web.xml``) - a binary-excluded ``.java``
    file (or a binary-excluded ``.jsp``/``.kt``/...) got the same real,
    visible, DEGRADING problem but still no synthesized unit, an
    inconsistency the reader's own F8 measured: two files in the identical
    epistemic state (this run never read either one), different
    visibility. Widened here to the FULL code-bearing predicate - the
    exact same one scan_pipeline.py's own ``binary_excluded_code_bearing_
    problems`` already uses, so the two can never independently drift on
    what counts as "code-bearing" for this purpose.

    Reuses that SAME existing reason code here rather than inventing a
    new one - the problem this unit's own ``adapter_problem_reason``
    names is the identical fact scan_pipeline.py's own ``binary_excluded_
    code_bearing_problems`` already publishes for the same path. This
    reason code was ALREADY added to readiness_artifact.py's own
    ``_READINESS_CHECKS_BY_REASON_CODE`` map in round 31 (the lesson that
    round learned the hard way - a reason reaching a SYNTHESIZED unit for
    the first time, rather than only ever a worker-attributed one, needs
    an entry there or it raises a bare ``KeyError``) - reusing the
    IDENTICAL reason code for this wider set of paths needs no second
    entry; the map is keyed on the reason code, not on which predicate
    populated it.

    ``descriptor_name_conflicts`` (FIX ROUND 29, twenty-fifth cold read,
    F1 BLOCKER, wrong-data) is the aggregate of every web.xml this run
    parsed own ``java.parse_web_xml``-produced conflicts (a servlet-name/
    filter-name declared twice with different class values) - see
    :func:`_populate_descriptor_name_conflicts`'s own docstring for the
    resolution mechanism."""
    records: list[ModuleRecord] = []
    worker_problem_reasons_by_path = worker_problem_reasons_by_path or {}
    worker_problem_reasons_by_unit = worker_problem_reasons_by_unit or {}
    non_degrading_unsupported_language_paths = non_degrading_unsupported_language_paths or frozenset()
    binary_excluded_root_sniffed_xml_digests = binary_excluded_root_sniffed_xml_digests or {}
    binary_excluded_code_bearing_digests = binary_excluded_code_bearing_digests or {}

    for relative_path, content_digest in sorted(binary_excluded_root_sniffed_xml_digests.items()):
        records.append(ModuleRecord(
            unit_id=digests.unit_id(kind="file", paths=[relative_path], qualified_name=None),
            kind="file",
            display_name=java_adapter.bounded_route_target(relative_path.rsplit("/", 1)[-1]),
            language=_language_for_path(relative_path),
            paths=[relative_path],
            source_digests={relative_path: content_digest},
            classification=[],
            container_unit_id=None,
            producers=[_producer(
                name="discovery", version=1, source_digest=content_digest, basis="extracted",
            )],
            adapter_problem_reason="binary_excluded_root_sniffed_xml",
            adapter_problem_reasons=["binary_excluded_root_sniffed_xml"],
        ))

    for relative_path, content_digest in sorted(binary_excluded_code_bearing_digests.items()):
        records.append(ModuleRecord(
            unit_id=digests.unit_id(kind="file", paths=[relative_path], qualified_name=None),
            kind="file",
            display_name=java_adapter.bounded_route_target(relative_path.rsplit("/", 1)[-1]),
            language=_language_for_path(relative_path),
            paths=[relative_path],
            source_digests={relative_path: content_digest},
            classification=[],
            container_unit_id=None,
            producers=[_producer(
                name="discovery", version=1, source_digest=content_digest, basis="extracted",
            )],
            adapter_problem_reason="binary_excluded_code_bearing_file",
            adapter_problem_reasons=["binary_excluded_code_bearing_file"],
        ))

    for file_entry in discovery.files:
        relative_path = file_entry.relative_path
        java_result = java_results.get(relative_path)

        if java_result is None or not java_result.units:
            reasons = worker_problem_reasons_by_path.get(relative_path, [])
            # FIX ROUND 28 (twenty-fourth cold read, F2 BLOCKER): `None`
            # means no decided classification (a file this run could not
            # even decode) - the closed classification vocabulary has no
            # "unknown" member, so this publishes as an EMPTY list, never
            # a guessed one. See `_derive_classification`'s own docstring.
            derived_classification = _derive_classification(
                relative_path, java_result_is_none=java_result is None, reasons=reasons,
                non_degrading_unsupported_language_paths=non_degrading_unsupported_language_paths,
            )
            records.append(ModuleRecord(
                unit_id=digests.unit_id(kind="file", paths=[relative_path], qualified_name=None),
                kind="file",
                display_name=java_adapter.bounded_route_target(relative_path.rsplit("/", 1)[-1]),
                language=_language_for_path(relative_path),
                paths=[relative_path],
                source_digests={relative_path: file_entry.content_digest},
                classification=(
                    [derived_classification] if derived_classification is not None else []
                ),
                container_unit_id=None,
                producers=[_producer(
                    name="discovery", version=1, source_digest=file_entry.content_digest,
                    basis="extracted",
                )],
                # BLOCKER 1b (fifth cold read, fix round 8): this used to
                # look up a worker-recorded problem ONLY when java_result
                # was None - a file whose parse SUCCEEDED but extracted
                # zero units (java_result is not None, java_result.units
                # is empty) always got None here regardless of whether
                # the worker had just recorded a real problem for it
                # (the worker's own new "no_types_extracted" check, or
                # any future one), silently discarding it and reporting
                # positive adapter evidence for a file never actually
                # understood. pom.xml/web.xml legitimately have zero
                # units with no problem recorded either way, so this
                # unconditional lookup is safe for them too - it simply
                # returns an empty list where nothing was ever recorded.
                adapter_problem_reason=reasons[0] if reasons else None,
                adapter_problem_reasons=list(reasons),
            ))
            continue

        # FIX ROUND 13b (reviewer-3's B1 class-closer on round 13): a
        # worker-recorded problem (e.g. an adapter under-claim fail-safe
        # like route_annotation_unassociated, route_value_unrecoverable,
        # or cli_main_unrecognized) used to reach a unit's own
        # adapter_problem_reason(s) ONLY through the "zero units
        # extracted" branch above - a file that DOES have real declared
        # types (the ordinary, common case for every one of those
        # problem kinds) silently dropped the reason here, so no
        # readiness check downstream could ever see it. Threaded through
        # both record shapes below the exact same way the zero-units
        # branch already does.
        reasons = worker_problem_reasons_by_path.get(relative_path, [])
        qualified_names_in_file = {u.qualified_name for u in java_result.units}
        unit_id_by_qualified_name: dict[str, str] = {}
        for unit_claim in java_result.units:
            unit_id_by_qualified_name[unit_claim.qualified_name] = digests.unit_id(
                kind="component", paths=[relative_path], qualified_name=unit_claim.qualified_name,
            )
        file_unit_id = digests.unit_id(kind="file", paths=[relative_path], qualified_name=None)

        for unit_claim in java_result.units:
            parent_name = _parent_qualified_name(unit_claim.qualified_name, qualified_names_in_file)
            # Note 3 (second cold read, fix round 4): a NESTED type is
            # contained by its outer type (unchanged); a TOP-LEVEL type
            # (parent_name is None) is contained by the FILE it is
            # declared in - previously it got container_unit_id=None here
            # while the FILE record below claimed the inverse (the file
            # "contained by" the first type declared inside it), backwards
            # containment that was never cyclic only because a file
            # doesn't point back to itself.
            container_unit_id = (
                unit_id_by_qualified_name[parent_name] if parent_name is not None else file_unit_id
            )
            # FIX ROUND 13c (reviewer-3's part 1 on round 13b): this
            # unit's OWN attributed reasons (if any) merge with the
            # file-wide broadcast ones - never the reverse (a sibling
            # type's own attributed problem must never appear here).
            unit_reasons = sorted({
                *reasons,
                *worker_problem_reasons_by_unit.get((relative_path, unit_claim.qualified_name), []),
            })
            records.append(ModuleRecord(
                unit_id=unit_id_by_qualified_name[unit_claim.qualified_name],
                kind="component",
                # FIX ROUND 17b (reviewer-3's rejection of round 17, MINOR
                # 4): this used to re-derive the display name from
                # qualified_name via _display_name (a bare rightmost DOT
                # segment) instead of publishing the producer's own
                # simple_name it already carries - correct for an
                # ordinary Java type (whose simple_name IS its qualified
                # name's own rightmost dot segment, so behavior does not
                # change there) but WRONG for a pom coordinate
                # ("com.acme:shop-web"), where the rightmost dot segment
                # lands INSIDE the colon-joined artifactId
                # ("acme:shop-web" - neither the groupId nor the
                # artifactId) - the CR13c simple_name carry becoming
                # visible on a second producer. Trusting the claim's own
                # simple_name generally is a no-op for Java types and the
                # actual fix for coordinates.
                # FIX ROUND 42 (thirty-sixth cold read, F3 MAJOR,
                # completeness - THE RECONCILIATION): `display_name` is
                # a LABEL, never re-hashed or re-looked-up (`unit_id`
                # above is keyed on `qualified_name`, not this field) -
                # bounded at display the same way `target_external`
                # now is (dependencies_artifact.py), closing Artifact-
                # 1's own "Bounded derived or declared label" promise.
                display_name=java_adapter.bounded_route_target(unit_claim.simple_name),
                # FIX ROUND 18 (fourteenth cold read, F4 MINOR, wrong-
                # data): this used to hardcode "java" regardless of the
                # producing file - true for an ordinary .java type, but
                # FALSE for a pom-coordinate component (a pom.xml is an
                # XML document, never Java source), which is how the
                # SAME pom's own published language flipped between
                # "java" and "xml" depending purely on whether it
                # happened to declare its own project-level groupId (a
                # component-kind unit) or stayed file-only. Every
                # producer's unit now gets ONE stable, truthful value
                # from the same path-based lookup the file-kind record
                # below already uses.
                language=_language_for_path(relative_path),
                paths=[relative_path],
                source_digests={relative_path: file_entry.content_digest},
                classification=[unit_claim.classification],
                container_unit_id=container_unit_id,
                producers=[_producer(
                    name=java_adapter.ADAPTER_NAME, version=java_adapter.ADAPTER_VERSION,
                    rule_version=java_adapter.RULE_VERSION,
                    source_digest=file_entry.content_digest, basis="extracted",
                )],
                adapter_problem_reason=unit_reasons[0] if unit_reasons else None,
                adapter_problem_reasons=unit_reasons,
                qualified_name=unit_claim.qualified_name,
            ))

        records.append(ModuleRecord(
            unit_id=file_unit_id,
            kind="file",
            display_name=java_adapter.bounded_route_target(relative_path.rsplit("/", 1)[-1]),
            language=_language_for_path(relative_path),
            paths=[relative_path],
            source_digests={relative_path: file_entry.content_digest},
            classification=[java_result.units[0].classification],
            container_unit_id=None,  # a file is the top of its own containment chain
            producers=[_producer(
                name="discovery", version=1, source_digest=file_entry.content_digest,
                basis="extracted",
            )],
            adapter_problem_reason=reasons[0] if reasons else None,
            adapter_problem_reasons=list(reasons),
        ))

    records = _attribute_cross_file_entry_point_reasons(
        records, worker_problem_reasons_by_qualified_name or {},
        worker_declaring_paths_by_qualified_name or {})
    records = _populate_duplicate_qualified_name_conflicts(records)
    records = _populate_descriptor_name_conflicts(records, descriptor_name_conflicts or [])
    # FIX ROUND 29 (twenty-fifth cold read, F6 polish, wrong-data):
    # MICRO-ROUND 28b's own binary-excluded-root-sniffed-XML synthesized
    # units are built in their OWN loop, BEFORE the main per-file loop
    # below - so they landed PREPENDED to every other record, never
    # interleaved in path order the way every other unit already is
    # (the design's own publish-validation step names "deterministic
    # ordering" as a real requirement, not merely an accident of
    # whichever loop happened to run first). Sorted here, once, at the
    # very end - by each record's own first path, then unit_id for a
    # stable tie-break between two records sharing the identical path
    # (a file-kind record and its own component-kind children).
    return sorted(records, key=lambda r: (r.paths[0] if r.paths else "", r.unit_id))


def _attribute_cross_file_entry_point_reasons(
    records: list[ModuleRecord],
    worker_problem_reasons_by_qualified_name: dict[str, list[str]],
    worker_declaring_paths_by_qualified_name: dict[str, set[str]],
) -> list[ModuleRecord]:
    """FIX ROUND 21c (reviewer-3's re-delta, THE CARRY, wrong-data): a
    worker problem recorded against a DIFFERENT file than the one
    declaring the type it names (web.xml's own ``<listener>`` element,
    naming a class declared in some other ``.java`` file entirely)
    could never reach that class's own unit via ``worker_problem_
    reasons_by_unit`` - that map is keyed by ``(relative_path,
    qualified_name)``, and the declaring file (web.xml) is never the
    SAME path as the named class's own file. Readiness then published
    the confident negative ``not_applicable``/``no_entry_point`` on a
    class this SAME run already knows carries an unmodeled listener.
    The annotation spelling (``@WebListener``) never has this problem -
    it is recorded directly against the class's own file, same-file,
    already correctly attributed above.

    Resolved via the exact same "unambiguous or not resolved at all"
    registry discipline ``features_artifact.py``'s own owner resolution
    already applies to a qualified name: a name with EXACTLY ONE
    "component"-kind claimant in THIS run gets the reason merged onto
    that one unit's own record.

    FIX ROUND 37 (thirty-first cold read, F3 MAJOR, wrong-data): the
    DECLARING file's own unit (web.xml's own file-kind record - modules.
    json already publishes one, the round-21c comment's own premise
    "web.xml genuinely has no unit of its own to broadcast to" was
    FALSE) now ALSO always gets ``unsupported_entry_point_shape``,
    regardless of whether the class resolves to zero, one, or more
    in-scan claimants. This is a fact about the DECLARING FILE - "this
    file names an unmodeled entry-point mechanism" - never contingent
    on whether this run happens to ALSO have the target class in scope;
    a listener/filter class that IS in scan (one claimant) still leaves
    web.xml itself carrying an unmodeled declaration, and a class NOT in
    scan at all (the NORMAL real-world case - a jar-shipped listener,
    e.g. Spring's own ContextLoaderListener) must not let the reason
    reach NOTHING just because there is no component to attribute it to
    either. The one-claimant component attribution is unchanged and
    additive - both units carry the reason when both apply. Restricted
    to ``unsupported_entry_point_shape`` specifically: the web.xml
    descriptor-conflict reason codes (``duplicate_descriptor_name``,
    ``undeclared_descriptor_name``, ``descriptor_name_without_class``,
    ...) ALSO carry a qualified_name here (a synthetic, file-anchored
    anchor - never a real class - see their own emission sites), but
    they already have their OWN dedicated, purpose-built resolution
    machinery (``_populate_descriptor_name_conflicts``) that decides
    which unit(s) they concern; broadcasting them onto the declaring
    file's own unit too would be a second, independently-maintained
    attribution path for a fact that mechanism already owns - out of
    scope for the narrower gap this round measured."""
    if not worker_problem_reasons_by_qualified_name:
        return records
    unit_ids_by_qualified_name: dict[str, list[str]] = {}
    file_unit_id_by_path: dict[str, str] = {}
    for record in records:
        if record.kind == "component" and record.qualified_name is not None:
            unit_ids_by_qualified_name.setdefault(record.qualified_name, []).append(record.unit_id)
        elif record.kind == "file" and len(record.paths) == 1:
            file_unit_id_by_path[record.paths[0]] = record.unit_id
    extra_reasons_by_unit_id: dict[str, list[str]] = {}
    for qualified_name, reasons in worker_problem_reasons_by_qualified_name.items():
        candidates = unit_ids_by_qualified_name.get(qualified_name, [])
        if len(candidates) == 1:
            extra_reasons_by_unit_id.setdefault(candidates[0], []).extend(reasons)
        # 2+ claimants: a genuine duplicate-qualified-name collision,
        # already its own separate, visible problem - the component
        # side is not this fallback's job either way.
        #
        # FIX ROUND 37 (F3 MAJOR): the declaring file's own unit ALWAYS
        # gets unsupported_entry_point_shape, independent of the
        # candidate count above - see this function's own docstring.
        fallback_reasons = [r for r in reasons if r == "unsupported_entry_point_shape"]
        if not fallback_reasons:
            continue
        for declaring_path in worker_declaring_paths_by_qualified_name.get(qualified_name, ()):
            file_unit_id = file_unit_id_by_path.get(declaring_path)
            if file_unit_id is not None:
                extra_reasons_by_unit_id.setdefault(file_unit_id, []).extend(fallback_reasons)
    if not extra_reasons_by_unit_id:
        return records
    updated: list[ModuleRecord] = []
    for record in records:
        extra = extra_reasons_by_unit_id.get(record.unit_id)
        if not extra:
            updated.append(record)
            continue
        merged = sorted({*record.adapter_problem_reasons, *extra})
        updated.append(replace(
            record, adapter_problem_reasons=merged,
            adapter_problem_reason=merged[0] if merged else None,
        ))
    return updated


def _populate_duplicate_qualified_name_conflicts(records: list[ModuleRecord]) -> list[ModuleRecord]:
    """FIX ROUND 16 (twelfth cold read, B1 BLOCKER, wrong-data): two
    in-scan classes declaring the IDENTICAL fully-qualified name (routine
    in legacy repos - multi-module copies, shaded relocations) is a real
    registry COLLISION (M12's own class) - both units used to carry
    ``conflict_id: null``, the design's own visible-grouping mechanism
    (``digests.conflict_id()``) existing with no caller anywhere in this
    artifact. Groups "component"-kind records by their own qualified_name
    and stamps every unit in a 2+ group with the SAME conflict_id (a
    stable hash over the conflict kind, the qualified name itself as the
    anchor, and the SORTED unit ids as the colliding claim digests - the
    design's own "sorted canonical claim digests" wording, this slice's
    stand-in for a full claim digest since a component's own unit_id is
    already a stable, deterministic identity). A qualified_name with
    exactly one claimant is never touched."""
    unit_ids_by_qualified_name: dict[str, list[str]] = {}
    for record in records:
        if record.kind == "component" and record.qualified_name is not None:
            unit_ids_by_qualified_name.setdefault(record.qualified_name, []).append(record.unit_id)

    conflict_id_by_unit_id: dict[str, str] = {}
    for qualified_name, unit_ids in unit_ids_by_qualified_name.items():
        if len(unit_ids) < 2:
            continue
        conflict_id = digests.conflict_id(
            conflict_kind="duplicate_qualified_name", anchor=qualified_name,
            claim_digests=sorted(unit_ids),
        )
        for unit_id in unit_ids:
            conflict_id_by_unit_id[unit_id] = conflict_id

    if not conflict_id_by_unit_id:
        return records
    return [
        replace(
            record, conflict_id=conflict_id_by_unit_id[record.unit_id],
            conflict_kind="duplicate_qualified_name")
        if record.unit_id in conflict_id_by_unit_id else record
        for record in records
    ]


def _populate_descriptor_name_conflicts(
    records: list[ModuleRecord],
    descriptor_name_conflicts: list[tuple[str, list[str]]],
) -> list[ModuleRecord]:
    """FIX ROUND 29 (twenty-fifth cold read, F1 BLOCKER, wrong-data): a
    web.xml declaring the SAME servlet-name/filter-name twice with
    DIFFERENT class values (``java.parse_web_xml``'s own ``descriptor_
    name_conflicts`` - see its docstring for the full mechanism) is a
    real conflict, entirely LOCAL to one descriptor file, never a
    cross-file fully-qualified-name collision - a DIFFERENT root cause
    from :func:`_populate_duplicate_qualified_name_conflicts` above, so
    it gets its OWN ``conflict_kind`` (never silently relabeled as
    ``"duplicate_qualified_name"``, which would misattribute the cause).
    Mirrors that function's own mechanism otherwise, with ONE
    deliberate difference (round 31, twenty-seventh cold read, F1
    BLOCKER, wrong-data - see below): only a candidate class that
    actually resolves to a real, in-scan COMPONENT unit gets a
    conflict_id at all (an unresolved candidate name has no unit to
    stamp) - the problem record java.py already published for the raw
    descriptor fact stays the only trace for a candidate this run
    cannot resolve in-scan.

    A class ALREADY carrying a conflict_id (e.g. its own independent
    duplicate-qualified-name collision) is left untouched - first
    conflict wins, never silently overwritten by a second, unrelated
    one; a vanishingly rare double-conflict shape, not worth a compound
    conflict_id/kind for.

    FIX ROUND 31 (twenty-seventh cold read, F1 BLOCKER, wrong-data):
    this used to require 2+ IN-SCAN candidates before stamping anything
    at all ("fewer than 2 in-scan candidates means there is nothing
    this run can actually see conflicting") - empirically FALSE. The
    COMMON real shape has exactly ONE in-scan claimant: the rival
    backing is a jar class (never in this scan), a ``<jsp-file>``, or
    an unrecoverable value - java.py's own ``duplicate_descriptor_name``
    problem is published either way, naming the in-scan class as one of
    the rival backings, but the gate above left that SAME class with NO
    conflict_id - readiness then gave it a CONFIDENT negative
    (``not_applicable``/``no_entry_point``, ``unsatisfied``/
    ``no_feature_link``), byte-identical to a POJO with zero descriptor
    involvement, on a run that both SAW and PUBLISHED the conflict. The
    gate now applies whenever there is AT LEAST ONE in-scan candidate -
    the conflict_id stamps every in-scan claimant (one or more); an
    out-of-scan/non-class rival is still represented by its own
    candidate label in java.py's own problem row (unchanged), never by
    a conflict_id here (there is no unit to stamp one on)."""
    if not descriptor_name_conflicts:
        return records
    unit_id_by_qualified_name: dict[str, str] = {
        record.qualified_name: record.unit_id
        for record in records
        if record.kind == "component" and record.qualified_name is not None
    }
    conflict_id_by_unit_id: dict[str, str] = {}
    for anchor, candidate_qualified_names in descriptor_name_conflicts:
        candidate_unit_ids = sorted({
            unit_id_by_qualified_name[name]
            for name in candidate_qualified_names
            if name in unit_id_by_qualified_name
        })
        if not candidate_unit_ids:
            continue
        conflict_id = digests.conflict_id(
            conflict_kind="duplicate_descriptor_name", anchor=anchor,
            claim_digests=candidate_unit_ids,
        )
        for unit_id in candidate_unit_ids:
            conflict_id_by_unit_id.setdefault(unit_id, conflict_id)

    if not conflict_id_by_unit_id:
        return records
    return [
        replace(
            record, conflict_id=conflict_id_by_unit_id[record.unit_id],
            conflict_kind="duplicate_descriptor_name")
        if record.unit_id in conflict_id_by_unit_id and record.conflict_id is None
        else record
        for record in records
    ]
