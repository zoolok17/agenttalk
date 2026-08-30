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
        conflict_id=payload.get("conflict_id"), evidence=list(payload.get("evidence", [])),
        adapter_problem_reason=payload.get("adapter_problem_reason"),
        adapter_problem_reasons=list(payload.get("adapter_problem_reasons", [])),
        qualified_name=payload.get("qualified_name"),
    )


def _language_for_path(relative_path: str) -> str:
    basename = relative_path.rsplit("/", 1)[-1]
    if basename in _LANGUAGE_BY_BASENAME:
        return _LANGUAGE_BY_BASENAME[basename]
    for ext, lang in _LANGUAGE_BY_EXTENSION.items():
        if relative_path.endswith(ext):
            return lang
    return "unknown"


def _default_classification(relative_path: str) -> str:
    if _TEST_SOURCE_ROOT_SEGMENT.search(relative_path.replace("\\", "/")):
        return "test"
    return "production"


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


def _display_name(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1]


def _parent_qualified_name(qualified_name: str, known_names: set[str]) -> str | None:
    if "." not in qualified_name:
        return None
    candidate = qualified_name.rsplit(".", 1)[0]
    return candidate if candidate in known_names else None


def build_modules(
    discovery: DiscoveryResult, java_results: dict[str, java_adapter.JavaFileResult],
    *, worker_problem_reasons_by_path: dict[str, list[str]] | None = None,
    worker_problem_reasons_by_unit: dict[tuple[str, str], list[str]] | None = None,
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
    separately as ``adapter_problem_reasons``."""
    records: list[ModuleRecord] = []
    worker_problem_reasons_by_path = worker_problem_reasons_by_path or {}
    worker_problem_reasons_by_unit = worker_problem_reasons_by_unit or {}

    for file_entry in discovery.files:
        relative_path = file_entry.relative_path
        java_result = java_results.get(relative_path)

        if java_result is None or not java_result.units:
            reasons = worker_problem_reasons_by_path.get(relative_path, [])
            records.append(ModuleRecord(
                unit_id=digests.unit_id(kind="file", paths=[relative_path], qualified_name=None),
                kind="file",
                display_name=relative_path.rsplit("/", 1)[-1],
                language=_language_for_path(relative_path),
                paths=[relative_path],
                source_digests={relative_path: file_entry.content_digest},
                classification=[_default_classification(relative_path)],
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
                display_name=_display_name(unit_claim.qualified_name),
                language="java",
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
            display_name=relative_path.rsplit("/", 1)[-1],
            language="java",
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

    return _populate_duplicate_qualified_name_conflicts(records)


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
        replace(record, conflict_id=conflict_id_by_unit_id[record.unit_id])
        if record.unit_id in conflict_id_by_unit_id else record
        for record in records
    ]
