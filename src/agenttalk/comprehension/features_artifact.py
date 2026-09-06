"""``features.json`` record assembly (DESIGN-55-comprehension-plane.md,
Artifact 3: feature and entry-point map).

"A detector may create only a candidate. Confirmation requires an explicit
local declaration in `config.json` or a supported pointer to confirmed
onboarding evidence; confidence alone never promotes a feature." Per the
lead's decided plan disposition #2 for this slice: v1 confirmation is a
versioned ``config.json`` declaration only (onboarding-record confirmation
is deferred). ``config.json`` parsing itself lands with CLI wiring (item
9); this module accepts an already-parsed set of confirmed labels so it
does not need to know that format yet.

Grouping heuristic for this slice (Java-only, flagged for review, not a
blocking fork): one CANDIDATE feature per owning unit that has at least
one detected entry point, labeled with that unit's own display name. This
keeps every entry point linked to exactly one feature by construction (the
design's other reportable gap - "an entry point with no feature link" -
cannot occur under this heuristic) without guessing at a broader,
semantically-grouped feature boundary a detector cannot actually evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from . import digests
from .adapters import java as java_adapter
from .dependencies_artifact import (
    DescriptorRegistrabilityVerdict,
    _build_registry,
    _java_file_unit_id,
    resolve_descriptor_qualified_name,
)

#: FIX ROUND 22 (eighteenth cold read, F7 MINOR): the SAME "declare the
#: structurally unreachable, don't leave it to be independently
#: rediscovered" discipline readiness_artifact.ASSESSMENT_STATE_CAVEAT
#: already established (round 16's own N2), for this artifact's own
#: two structural constants: (1) `unmapped_entry_points` (projector.py)
#: is ALWAYS empty this slice - build_features below groups every
#: entry-point claim into a feature by construction (one CANDIDATE
#: feature per owning unit with at least one entry point), so every
#: EntryPointRecord this producer ever emits carries a non-empty
#: feature_ids unconditionally; the field exists for a FUTURE producer
#: shape that could leave one unlinked, not because this slice's own
#: producer ever does. (2) FeatureRecord.state never reports
#: "confirmed" this slice either - `confirmed_labels` comes from
#: config.json parsing (a named, deferred decision - "No config.json
#: parsing yet" - elsewhere in this PR), so `state in confirmed_labels`
#: can never be true yet; every feature this slice publishes is
#: "candidate" by construction, not because no repo happens to declare
#: a confirmation.
FEATURES_STRUCTURAL_CAVEAT = (
    "unmapped_entry_points (the projection's own field) is always empty this "
    "slice - build_features groups every entry-point claim into a feature by "
    "construction, so every entry point already carries a non-empty feature_ids; "
    "the field exists for a future producer shape that could leave one unlinked, "
    "not because this slice's own producer ever does. Every feature's own state "
    "is 'candidate' for the identical structural reason - 'confirmed' requires a "
    "config.json declaration, and config.json parsing is not implemented this "
    "slice."
)


@dataclass(frozen=True)
class EntryPointRecord:
    entry_point_id: str
    kind: str
    name: str
    #: NAMED LIMIT, declared (PR-B round 44, N1, thirty-eighth cold
    #: read): this field's own unit KIND is heterogeneous depending on
    #: how its claim's own ``qualified_name`` resolved - the clean case
    #: (a real, unambiguous declared type) resolves to a COMPONENT
    #: unit_id (`_build_registry`'s own `by_qualified_name`); a
    #: `duplicate_qualified_name` conflict (two units declaring the
    #: identical fully-qualified name) removes that name from `by_
    #: qualified_name` ENTIRELY (see that function's own docstring), so
    #: the SAME claim instead falls back to the FILE unit_id
    #: (`_java_file_unit_id`) here, even though a real declared type
    #: exists - just an ambiguous one. Judged defensible, not a bug:
    #: with two-plus duplicate claimants, there is no single correct
    #: component owner to point at without arbitrarily favoring one -
    #: the file-unit fallback is the SAME "ambiguous, never confidently
    #: pick one" honesty this producer already applies to a registry-
    #: miss resolution elsewhere, extended here rather than invented
    #: fresh. A consumer grouping entry points by owner-unit KIND alone
    #: (never resolving the id itself) sees this as two different
    #: shapes for what is, from THIS field's own contract, one
    #: consistent "the owner" concept - named here so that surprise is
    #: looked up, not independently rediscovered.
    owning_unit_id: str
    feature_ids: list[str]
    evidence_class: str
    producers: list[dict[str, Any]] = field(default_factory=list)
    conflict_id: str | None = None
    confidence: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: FIX ROUND 27 (twenty-third cold read, F1 BLOCKER, wrong-data): the
    #: FILE unit whose own adapter claim DECLARED this entry point - set
    #: unconditionally, regardless of where ``owning_unit_id`` resolved.
    #:
    #: CORRECTION (round 28's own F6, twenty-fourth cold read): this
    #: comment used to claim that for an annotation-based route "the two
    #: are usually the same unit (the class's own file)" - FALSE. When
    #: the class resolves in-scan, ``owning_unit_id`` is the COMPONENT
    #: unit (the declared type itself), never the FILE unit ``declared_
    #: in_unit_id`` always is - the two are different KINDS of unit_id
    #: even in the common annotation case. Readiness never shows a
    #: divergence there anyway, but for an UNRELATED reason: round 22's
    #: own containment rollup (``_aggregate_file_signal_from_
    #: components``) already derives the file's own signal by mirroring
    #: its single contained component's signal, independent of and
    #: prior to this field's own existence. Round 27's ``build_
    #: readiness`` fix (its own ``declared_in_unit_id`` credit) is a
    #: NO-OP for the annotation case for that reason - not because the
    #: two ids coincide - and matters ONLY for a producer like web.xml
    #: that has no component-kind unit of its own to roll up through.
    #: For a web.xml-declared route whose <servlet-class> resolves
    #: in-scan, the file/owner split is real and this field is what
    #: closes the gap: ``owning_unit_id``
    #: moves to the implementing class (CR13-2, round 17), but nothing
    #: previously remembered that web.xml itself was the file that
    #: DECLARED the route - readiness_artifact.py's own entry_points_
    #: mapped/feature_linked checks then found no evidence at all for
    #: the declaring file and published the confident negative on a
    #: complete/0-problem run, for essentially every real JEE repo (a
    #: web.xml naming an in-repo servlet class is the normal case, not
    #: an edge case). Readiness now credits a file as having real entry-
    #: point/feature evidence when it EITHER owns an entry point OR
    #: declared one whose ownership resolved elsewhere - this field is
    #: what makes the second half possible.
    declared_in_unit_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "entry_point_id": self.entry_point_id,
            "kind": self.kind,
            "name": self.name,
            "owning_unit_id": self.owning_unit_id,
            "feature_ids": sorted(self.feature_ids),
            "evidence_class": self.evidence_class,
            "producers": self.producers,
            "conflict_id": self.conflict_id,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "declared_in_unit_id": self.declared_in_unit_id,
        }


def entry_point_record_from_json(payload: dict[str, Any]) -> EntryPointRecord:
    return EntryPointRecord(
        entry_point_id=payload["entry_point_id"], kind=payload["kind"], name=payload["name"],
        owning_unit_id=payload["owning_unit_id"], feature_ids=list(payload["feature_ids"]),
        evidence_class=payload["evidence_class"], producers=list(payload.get("producers", [])),
        conflict_id=payload.get("conflict_id"), confidence=payload.get("confidence"),
        evidence=list(payload.get("evidence", [])),
        declared_in_unit_id=payload.get("declared_in_unit_id", payload["owning_unit_id"]),
    )


@dataclass(frozen=True)
class FeatureRecord:
    feature_id: str
    label: str
    state: str
    origin: str
    unit_ids: list[str]
    entry_point_ids: list[str]
    producers: list[dict[str, Any]] = field(default_factory=list)
    conflict_id: str | None = None
    confidence: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "label": self.label,
            "state": self.state,
            "origin": self.origin,
            "unit_ids": sorted(self.unit_ids),
            "entry_point_ids": sorted(self.entry_point_ids),
            "producers": self.producers,
            "conflict_id": self.conflict_id,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


def _producer(source_digest: str | None, *, basis: str) -> dict[str, Any]:
    """FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data):
    ``basis`` used to be the hardcoded literal ``"extracted"`` here,
    regardless of the SAME record's own ``evidence_class`` field - see
    dependencies_artifact.py's own identical fix and docstring for the
    measured contradiction this closes. The caller passes its own
    claim's ``evidence_class`` straight through."""
    return {
        "producer": java_adapter.ADAPTER_NAME, "producer_version": java_adapter.ADAPTER_VERSION,
        "rule_version": java_adapter.RULE_VERSION, "basis": basis,
        "source_digest": source_digest,
    }


def _feature_label(qualified_name: str) -> str:
    """Note 4 (second cold read, fix round 4): a Java-style dotted
    qualified name (``p.App``) uses its simple (rightmost) segment as the
    label - but a non-Java producer's SYNTHETIC qualified name (web.xml's
    servlet-mapping entry points: ``f"{relative_path}#{servlet_name}"``,
    e.g. ``WEB-INF/web.xml#dispatcher``) is not a dotted type name at all;
    splitting on "." there lands in the middle of the FILE PATH's own
    extension, producing garbage like "xml#legacy" instead of the actual
    servlet name. Splits on "#" first when present."""
    if "#" in qualified_name:
        return qualified_name.rsplit("#", 1)[-1]
    return qualified_name.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class DescriptorRegistrabilityProblem:
    """FIX ROUND 45 (thirty-ninth cold read, F2 MAJOR - THE MATRIX'S OWN
    MISSING COLUMN): one web.xml ``<servlet-class>``/``<filter-class>``
    that resolved, cross-file, to a real in-scan class this run already
    knows (via ``JavaUnitClaim.is_interface``/``is_abstract``/
    ``is_enum``, threaded through this round) can never actually be
    instantiated/dispatched to - a descriptor names ONE specific class
    with no implementor-may-serve escape (unlike Spring/JAX-RS's own
    weaker class-level-annotation claim), so this is the STRONGEST of
    the three registrability claims this producer models. ``relative_
    path`` is the DECLARING file (the web.xml itself, never the
    resolved class's own file) - the same attribution
    ``_uninstantiable_class_problem`` uses is not reusable here
    verbatim (its own wording assumes an annotation, never a
    descriptor), so this is a small, dedicated sibling.

    FIX ROUND 47 (forty-first cold read, B2+B3 - THE DESCRIPTOR FAMILY):
    ``detail`` is now taken VERBATIM from the shared, upstream
    ``DescriptorRegistrabilityVerdict`` (``dependencies_artifact.
    compute_descriptor_registrability_verdicts``) rather than re-derived
    here per-claim - the wording (including the duplicate-qualified-
    name disposition) is decided ONCE, in one place, for every
    consumer."""

    relative_path: str
    qualified_name: str
    detail: str


def build_features(
    java_results: dict[str, java_adapter.JavaFileResult],
    *, confirmed_labels: frozenset[str] = frozenset(),
    file_digests: dict[str, str] | None = None,
    descriptor_registrability_verdicts: dict[str, DescriptorRegistrabilityVerdict] | None = None,
) -> tuple[list[EntryPointRecord], list[FeatureRecord], list[DescriptorRegistrabilityProblem]]:
    """Returns ``(entry_points, features, descriptor_registrability_problems)``.
    ``confirmed_labels`` names which candidate feature labels a
    ``config.json`` declaration confirms (state -> ``confirmed``) - an
    empty set (the default) means every feature stays ``candidate``,
    matching "a detector may create only a candidate."

    ``file_digests`` maps a relative path to discovery's own content
    digest for that file (M7, cold-read PR-B fix round 3: every producer
    here carried ``source_digest=None`` unconditionally - the design's
    per-fact producer identity, source content digest included, was never
    actually populated even though the digest was already computed and
    available upstream).

    FIX ROUND 45 (thirty-ninth cold read, F2 MAJOR): return arity WIDENED
    (unlike ``EntryPointRecord``'s own frozen shape) to carry
    ``descriptor_registrability_problems`` - a web.xml ``<servlet-class>``/
    ``<filter-class>`` claim whose OWN qualified name resolves,
    unambiguously, to a real declared class this run ALSO knows is
    interface/abstract/enum. See ``DescriptorRegistrabilityProblem``'s
    own docstring for why this can only be decided HERE, cross-file, and
    ``java.py``'s own ``UNSUPPORTED_ENTRY_POINT_SHAPES`` docstring for
    why the reason_code is the SAME ``unsupported_entry_point_shape``
    every sibling registrability shape already uses (never a new reason
    code - only a new shape name embedded in the detail, the established
    convention every family member since round 44 already follows).
    Suppressed the same way an uninstantiable annotation target already
    is - never published as a served route/filter, only as this problem;
    ``scan_pipeline.py`` is responsible for feeding this into
    ``problems.json``/``degraded_by`` and re-attributing the reason onto
    the resolved unit's own ``modules.json`` record (``modules_artifact.
    _attribute_cross_file_entry_point_reasons``, reused verbatim - the
    exact same cross-file attribution mechanism the web.xml ``<listener>``
    case already established).

    FIX ROUND 47 (forty-first cold read, B2 BLOCKER, wrong-data - THE
    DESCRIPTOR FAMILY, the reader's own recommended structural shape):
    the registrability check used to be rebuilt HERE, independently,
    from ``JavaUnitClaim``s directly - resolved against ``by_qualified_
    name``, which EMPTIES ITSELF entirely for a duplicate qualified
    name (see ``_build_registry``'s own docstring), so a duplicate FQN
    (a ``src/test/java`` copy of the same class, a real, common shape)
    made ``resolved_unit_id`` ``None`` and silently skipped the WHOLE
    registrability check, publishing a confident served route for a
    class this run independently knew was uninstantiable. Moved
    upstream: ``descriptor_registrability_verdicts`` is now computed
    ONCE, before this function (and ``dependencies_artifact.
    build_dependencies``'s own route-edge builder) ever run - see
    ``dependencies_artifact.compute_descriptor_registrability_
    verdicts``'s own docstring for the full mechanism and the duplicate
    disposition - and consulted here via its OWN resolution, entirely
    independent of ``by_qualified_name``'s own emptying behavior."""
    file_digests = file_digests or {}
    descriptor_registrability_verdicts = descriptor_registrability_verdicts or {}
    (
        by_qualified_name, _by_simple_name, _file_unit_ids, _duplicate_names,
        _unit_ids_by_qname, _in_scan_packages,
    ) = _build_registry(java_results)

    owning_unit_by_qualified_name = by_qualified_name
    descriptor_registrability_problems: list[DescriptorRegistrabilityProblem] = []
    # FIX ROUND 14 (tenth cold read, CR10-8 MINOR, wrong-data): grouped
    # by owning_unit_id ALONE - a claim with no real declared-type owner
    # (web.xml's servlet-mapping entry points; parse_web_xml's synthetic
    # qualified_name never matches an actual declared type) falls back
    # to the FILE unit, but a web.xml with TWO servlet mappings owns
    # BOTH claims under that SAME file fallback, collapsing them into
    # ONE feature labelled after whichever claim happened to be first -
    # the second servlet's own identity silently folded under the
    # wrong name. Grouped by (owning_unit_id, a distinguisher) instead:
    # a REAL declared-type owner still groups every claim it owns into
    # one feature (multiple @GetMapping routes on the SAME controller
    # ARE one feature, unchanged); a FILE-fallback owner groups by the
    # claim's OWN qualified_name too, so each independent claim under
    # that same fallback gets its own feature.
    entry_points_by_owner: dict[
        tuple[str, str | None], list[tuple[str, java_adapter.JavaEntryPointClaim, str]]
    ] = {}

    for path, result in java_results.items():
        for claim in result.entry_points:
            # FIX ROUND 46 (fortieth cold read, F2 MAJOR, wrong-data - THE
            # DESCRIPTOR GATE IS BLIND TO THE BINARY SPELLING): a real
            # container requires the JVM's own binary name
            # (`Host$NestedAbs`) in a descriptor naming a nested class -
            # never the source-dotted spelling this adapter's own
            # `qualified_name` always publishes - so an exact string
            # match against `by_qualified_name` never fired for ANY
            # nested class named this way. Resolved via the shared
            # exact-then-translated boundary - see its own docstring for
            # the full mechanism and the collision disposition.
            resolved_qualified_name = resolve_descriptor_qualified_name(
                claim.qualified_name, owning_unit_by_qualified_name)
            resolved_unit_id = owning_unit_by_qualified_name.get(resolved_qualified_name)
            # FIX ROUND 47 (forty-first cold read, B2 BLOCKER, wrong-
            # data): only ``http_route``/``http_filter`` claims name a
            # class the container must actually instantiate -
            # ``cli_main`` has no such class-instantiation contract at
            # all. Resolved against the SHARED upstream verdict map
            # DIRECTLY - never gated on `resolved_unit_id is not None`
            # (that check is `by_qualified_name`-based, which empties on
            # a duplicate qualified name; the verdict map handles a
            # duplicate honestly instead of silently skipping it - see
            # `compute_descriptor_registrability_verdicts`'s own
            # docstring). Every annotation-sourced route on an
            # uninstantiable class already self-suppresses, same-file,
            # before it ever reaches `result.entry_points` (see java.py's
            # own registrability checks) - so any SURVIVING claim with a
            # suppressed verdict can only be a web.xml descriptor claim.
            if claim.kind in ("http_route", "http_filter"):
                verdict_key = resolve_descriptor_qualified_name(
                    claim.qualified_name, descriptor_registrability_verdicts)
                verdict = descriptor_registrability_verdicts.get(verdict_key)
                if verdict is not None and verdict.suppress:
                    descriptor_registrability_problems.append(DescriptorRegistrabilityProblem(
                        relative_path=path, qualified_name=verdict_key, detail=verdict.detail or "",
                    ))
                    continue
            if resolved_unit_id is not None:
                owning_unit_id, group_key = resolved_unit_id, (resolved_unit_id, None)
            else:
                owning_unit_id = _java_file_unit_id(path)
                group_key = (owning_unit_id, claim.qualified_name)
            entry_points_by_owner.setdefault(group_key, []).append((path, claim, owning_unit_id))

    entry_point_records: list[EntryPointRecord] = []
    features: list[FeatureRecord] = []

    for (owning_unit_id, _distinguisher), claims in entry_points_by_owner.items():
        owner_path, first_claim, _owner = claims[0]
        label = _feature_label(first_claim.qualified_name)
        # FIX ROUND 39 (F1 BLOCKER): `first_claim.qualified_name`/`kind`
        # are the exact distinguishing data `group_key` above already
        # uses to keep two file-fallback-owned claims apart - threaded
        # into feature_id itself now too, the same fix shape round 38
        # applied to entry_point_id (see digests.feature_id's own
        # docstring).
        feature_id = digests.feature_id(
            label=label, unit_ids=[owning_unit_id],
            qualified_name=first_claim.qualified_name, kind=first_claim.kind,
        )

        # M-5 (third cold read, fix round 5): two claims that normalize to
        # the SAME entry_point_id (kind+owning_unit_id+name) - e.g. a
        # duplicate declaration, or a raw claim shape this adapter has not
        # been taught to distinguish yet - used to publish as two SEPARATE
        # EntryPointRecords sharing one entry_point_id (a duplicate
        # "primary key" in a published artifact), and the owning feature's
        # entry_point_ids listed that ID twice (not the set the pack
        # contract's exact sorted sets assume). Coalesced by ID here, with
        # merged producer lists - the SAME rule dependencies_artifact.py's
        # _coalesce_by_edge_id already applies to edges (M6, round 3).
        entry_points_by_id: dict[str, EntryPointRecord] = {}
        for path, claim, _owner in claims:
            # FIX ROUND 38 (F1 BLOCKER): `claim.qualified_name` is the
            # exact distinguishing datum `group_key` above already uses
            # to keep two file-fallback-owned claims apart - threaded
            # into the id itself now too, closing the by-construction
            # collision reachable across two DIFFERENT groups that
            # nonetheless share kind/owning_unit_id/name (see digests.
            # entry_point_id's own docstring).
            #
            # FIX ROUND 41 (thirty-fifth cold read, F1+F2, THE
            # STRUCTURAL CURE): `claim.name` is now always the RAW,
            # unbounded/unescaped value (java.py no longer bounds
            # anything at extraction) - hashed here directly, never
            # through a display-projection that could make two
            # genuinely different names collide. The PUBLISHED record's
            # own `name` field is bounded separately, below, at the one
            # point it is actually serialized for display - see
            # java_adapter.bounded_route_target's own docstring for the
            # full architecture.
            entry_point_id = digests.entry_point_id(
                kind=claim.kind, owning_unit_id=owning_unit_id,
                name=claim.name, qualified_name=claim.qualified_name,
            )
            producer = _producer(file_digests.get(path), basis=claim.evidence_class)
            existing = entry_points_by_id.get(entry_point_id)
            if existing is None:
                entry_points_by_id[entry_point_id] = EntryPointRecord(
                    entry_point_id=entry_point_id, kind=claim.kind,
                    name=java_adapter.bounded_route_target(claim.name),
                    owning_unit_id=owning_unit_id, feature_ids=[feature_id],
                    evidence_class=claim.evidence_class, producers=[producer],
                    # FIX ROUND 27 (F1 BLOCKER): the FILE that declared
                    # THIS claim - independent of owning_unit_id, which
                    # may have resolved to a different (implementing)
                    # unit entirely. A rare coalesced-claim shape (M-5)
                    # takes the first claim's own declaring file, the
                    # same "first claim is representative" convention
                    # this function already uses for the feature label.
                    declared_in_unit_id=_java_file_unit_id(path),
                )
            elif producer not in existing.producers:
                entry_points_by_id[entry_point_id] = replace(
                    existing, producers=[*existing.producers, producer])

        entry_point_records.extend(entry_points_by_id.values())

        state = "confirmed" if label in confirmed_labels else "candidate"
        features.append(FeatureRecord(
            feature_id=feature_id, label=label, state=state, origin="detected",
            unit_ids=[owning_unit_id], entry_point_ids=list(entry_points_by_id),
            # FIX ROUND 37 (F5 MAJOR): a feature can aggregate more than
            # one entry-point claim (M-5's own coalesce case) with
            # potentially different evidence classes - the SAME "first
            # claim is representative" convention this function already
            # uses for the feature's own label is reused here too, never
            # a second, independently-decided representative.
            producers=[_producer(file_digests.get(owner_path), basis=first_claim.evidence_class)],
        ))

    return entry_point_records, features, descriptor_registrability_problems


def feature_record_from_json(payload: dict[str, Any]) -> FeatureRecord:
    return FeatureRecord(
        feature_id=payload["feature_id"], label=payload["label"], state=payload["state"],
        origin=payload["origin"], unit_ids=list(payload["unit_ids"]),
        entry_point_ids=list(payload["entry_point_ids"]), producers=list(payload.get("producers", [])),
        conflict_id=payload.get("conflict_id"), confidence=payload.get("confidence"),
        evidence=list(payload.get("evidence", [])),
    )
