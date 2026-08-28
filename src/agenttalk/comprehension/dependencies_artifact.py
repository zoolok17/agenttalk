"""``dependencies.json`` record assembly (DESIGN-55-comprehension-plane.md,
Artifact 2: dependency edges).

"An adapter may emit a relation only when its versioned extraction rule
names a producer for that relation. Unsupported relation types remain
coverage gaps; they are never coerced into `data` or another
healthy-looking generic edge." This module is also where cross-file
target resolution happens (design step 6: "Normalize records, resolve only
evidenced edges") - the Java adapter (item 3) deliberately emits LOCAL,
unresolved-target candidate claims; this is the later, global step that
reconciles them against every file's declared types in the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from . import digests
from .adapters import java as java_adapter
from .errors import ComprehensionError

DEPENDENCIES_ARTIFACT_TYPE = "agenttalk.comprehension.dependencies"
DEPENDENCIES_SCHEMA_VERSION = 1

#: DESIGN-55-comprehension-plane.md, Artifact 2: "a closed relation from
#: the coarse slice-1 vocabulary."
CLOSED_RELATIONS = frozenset({
    "import", "include", "inherit", "invoke", "route", "data", "configuration",
    "build", "test",
})


class UnsupportedRelationClaimed(ComprehensionError):
    """A producer emitted a relation outside the closed S1 vocabulary -
    an adapter bug, not a legitimate edge. Caught here so dependencies.json
    can never silently carry an out-of-contract relation."""

    reason_code = "comprehension_unsupported_relation_claimed"


@dataclass(frozen=True)
class DependencyRecord:
    edge_id: str
    from_unit_id: str
    relation: str
    phase: str
    optional: bool
    evidence_class: str
    resolution_state: str
    target_unit_id: str | None = None
    target_external: str | None = None
    target_unresolved: str | None = None
    confidence: str | None = None
    producers: list[dict[str, Any]] = field(default_factory=list)
    conflict_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_unit_id": self.from_unit_id,
            "target_unit_id": self.target_unit_id,
            "target_external": self.target_external,
            "target_unresolved": self.target_unresolved,
            "relation": self.relation,
            "phase": self.phase,
            "optional": self.optional,
            "evidence_class": self.evidence_class,
            "resolution_state": self.resolution_state,
            "confidence": self.confidence,
            "producers": self.producers,
            "conflict_id": self.conflict_id,
            "evidence": self.evidence,
        }


def dependency_record_from_json(payload: dict[str, Any]) -> DependencyRecord:
    """The inverse of :meth:`DependencyRecord.to_json`."""
    return DependencyRecord(
        edge_id=payload["edge_id"], from_unit_id=payload["from_unit_id"],
        relation=payload["relation"], phase=payload["phase"], optional=payload["optional"],
        evidence_class=payload["evidence_class"], resolution_state=payload["resolution_state"],
        target_unit_id=payload.get("target_unit_id"), target_external=payload.get("target_external"),
        target_unresolved=payload.get("target_unresolved"), confidence=payload.get("confidence"),
        producers=list(payload.get("producers", [])), conflict_id=payload.get("conflict_id"),
        evidence=list(payload.get("evidence", [])),
    )


def _java_component_unit_id(relative_path: str, qualified_name: str) -> str:
    return digests.unit_id(kind="component", paths=[relative_path], qualified_name=qualified_name)


def _java_file_unit_id(relative_path: str) -> str:
    return digests.unit_id(kind="file", paths=[relative_path], qualified_name=None)


def _build_registry(
    java_results: dict[str, java_adapter.JavaFileResult],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """M12 (cold-read, PR-B fix round 3): two units DECLARING the same
    fully-qualified name (a real collision, not merely a same-SIMPLE-name
    coincidence) used to last-wins into ``by_qualified_name`` with no
    collision handling - a later scan-order-dependent unit would silently
    replace an earlier one, and every edge resolving to that name would
    confidently pick whichever happened to win, never reporting the
    conflict. The full conflict/merge machinery (``conflict_id``) stays
    out of this slice (named PR-C entry item), but a duplicate qualified
    name must never resolve confidently either way: it is removed from
    ``by_qualified_name`` entirely once a second claimant is seen, so an
    exact-name lookup finds nothing there and any resolution instead falls
    through to the existing simple-name-ambiguity path below (which
    already reports ``ambiguous`` for 2+ same-simple-name candidates -
    true here by construction, since a duplicate qualified name shares its
    own simple name with itself)."""
    by_qualified_name: dict[str, str] = {}
    duplicate_qualified_names: set[str] = set()
    by_simple_name: dict[str, list[str]] = {}
    file_unit_id_by_path: dict[str, str] = {}
    for path, result in java_results.items():
        file_unit_id_by_path[path] = _java_file_unit_id(path)
        for unit_claim in result.units:
            uid = _java_component_unit_id(path, unit_claim.qualified_name)
            if unit_claim.qualified_name in by_qualified_name:
                duplicate_qualified_names.add(unit_claim.qualified_name)
            else:
                by_qualified_name[unit_claim.qualified_name] = uid
            by_simple_name.setdefault(unit_claim.simple_name, []).append(uid)
    for name in duplicate_qualified_names:
        by_qualified_name.pop(name, None)
    return by_qualified_name, by_simple_name, file_unit_id_by_path


def _exact_qualified_lookup(target: str, by_qualified_name: dict[str, str]) -> str | None:
    """The exact, non-fuzzy half of internal resolution: ``target`` names
    an in-scan type's fully-qualified name, verbatim - never a similarity
    guess. Shared verbatim by both :func:`_resolve_internal_candidate`
    (whose ``extends``/``implements``/test-pairing callers also need the
    simple-name fallback below, for unqualified references like a bare
    ``extends Base``) and the import path (D-1, reviewer-3 PR-B delta
    review round 2), whose target is already fully qualified and so never
    needs - and must never receive - that fallback: reusing it for imports
    would risk a genuinely-external import matching a same-named LOCAL
    type by coincidence, a false positive the import path must not
    produce."""
    return by_qualified_name.get(target)


def _resolve_internal_candidate(
    target: str, by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
) -> tuple[str, str | None, str | None, str | None]:
    """Returns ``(resolution_state, target_unit_id, target_unresolved,
    confidence)``. Never invents a match: an unqualified name matching
    more than one declared type across the scan is ``ambiguous``, not a
    guess at which one was meant (design: "The scanner never invents an
    internal target because names look similar. Ambiguous resolution
    creates an unresolved edge with candidates.")."""
    exact = _exact_qualified_lookup(target, by_qualified_name)
    if exact is not None:
        return "resolved", exact, None, "high"
    simple = target.rsplit(".", 1)[-1]
    candidates = by_simple_name.get(simple, [])
    if len(candidates) == 1:
        return "resolved", candidates[0], None, "medium"
    if len(candidates) > 1:
        return "ambiguous", None, target, None
    return "unresolved", None, target, None


def _producer(*, name: str, version: int, rule_version: int, source_digest: str | None) -> dict[str, Any]:
    return {
        "producer": name, "producer_version": version, "rule_version": rule_version,
        "basis": "extracted", "source_digest": source_digest,
    }


def build_dependencies(
    java_results: dict[str, java_adapter.JavaFileResult],
    *, file_digests: dict[str, str] | None = None,
) -> list[DependencyRecord]:
    """``java_results`` carries every producer's claims uniformly, keyed
    by relative path - including a ``pom.xml``'s ``build`` edges (B-3,
    reviewer-3 PR-B delta review round 1: routed through the sanitized
    worker's ``process_paths`` on the same already-read bytes, the same
    way every other adapter claim is). A pom.xml's ``JavaFileResult`` has
    empty ``units`` and just its ``edges`` populated - these are always
    ``target_kind: external`` and never need the cross-file registry
    below, but fall out of the same loop naturally since nothing here
    depends on the producing file being a ``.java`` source.

    (dead-parameter removal, reviewer-3 PR-B delta review round 2: this
    function previously took a second, separate ``build_edges_by_path``
    parameter for exactly this pom.xml case, from when scan_pipeline.py
    read pom.xml directly in the parent process; once B-3 routed it
    through the worker instead, that parameter had no production caller
    left.)

    ``file_digests`` maps a relative path to discovery's own content
    digest for that file (M7, cold-read PR-B fix round 3: ``source_digest``
    was set to ``None`` once per file and never actually assigned from it -
    easy to miss since modules_artifact.py populates its OWN producers'
    source_digest correctly, so only this module's own producers stayed
    silently unpopulated).
    """
    file_digests = file_digests or {}
    by_qualified_name, by_simple_name, file_unit_id_by_path = _build_registry(java_results)
    records: list[DependencyRecord] = []

    for path, result in java_results.items():
        source_digest = file_digests.get(path)
        for edge in result.edges:
            if edge.relation not in CLOSED_RELATIONS:
                raise UnsupportedRelationClaimed(
                    f"{java_adapter.ADAPTER_NAME} claimed unsupported relation "
                    f"{edge.relation!r} for {path}")
            from_unit_id = (
                by_qualified_name.get(edge.from_qualified_name)
                or file_unit_id_by_path[path]
            )
            record = _edge_claim_to_record(
                edge, from_unit_id=from_unit_id, source_digest=source_digest,
                by_qualified_name=by_qualified_name, by_simple_name=by_simple_name,
            )
            records.append(record)

    return _coalesce_by_edge_id(records)


def _coalesce_by_edge_id(records: list[DependencyRecord]) -> list[DependencyRecord]:
    """M6 (cold-read, PR-B fix round 3): "byte-identical claims must
    coalesce to one record per edge_id with merged producer lists" (design
    merge rule) - e.g. three identical calls to the same method at the
    same call site pattern (``Foo.bar(); Foo.bar(); Foo.bar();``) all
    produce the SAME ``edge_id`` (from_unit_id + relation + target +
    phase - the adapter records no per-call-site distinguishing evidence
    this slice), so without this step they inflated record_counts,
    ceilings, and every fan-in/fan-out count with pure duplicates.
    Preserves first-seen order (a plain dict is insertion-ordered) so
    coalescing never perturbs an otherwise-deterministic record order."""
    merged: dict[str, DependencyRecord] = {}
    for record in records:
        existing = merged.get(record.edge_id)
        if existing is None:
            merged[record.edge_id] = record
            continue
        combined_producers = list(existing.producers)
        for producer in record.producers:
            if producer not in combined_producers:
                combined_producers.append(producer)
        merged[record.edge_id] = replace(existing, producers=combined_producers)
    return list(merged.values())


def _edge_claim_to_record(
    edge: java_adapter.JavaEdgeClaim, *, from_unit_id: str, source_digest: str | None,
    by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
) -> DependencyRecord:
    target_unit_id = target_external = target_unresolved = confidence = None
    if edge.target_kind == "internal_candidate":
        resolution_state, target_unit_id, target_unresolved, confidence = (
            _resolve_internal_candidate(edge.target, by_qualified_name, by_simple_name)
        )
    elif edge.target_kind == "internal_exact_or_external":
        # D-1 (reviewer-3, PR-B delta review round 2): an import's target
        # is already fully qualified - an EXACT registry hit means it
        # names an in-scan type and resolves internally exactly like the
        # identical name would via `extends`; anything else is a genuinely
        # external dependency, never a simple-name guess.
        exact_unit_id = _exact_qualified_lookup(edge.target, by_qualified_name)
        if exact_unit_id is not None:
            resolution_state, target_unit_id, confidence = "resolved", exact_unit_id, "high"
        else:
            resolution_state, target_external = "resolved", edge.target
    elif edge.target_kind == "internal_static_import_exact_or_external":
        # N5 (fourth cold read, fix round 6): a static import's target is
        # a member path (Type.MEMBER) or a static-member wildcard
        # (Type.*) - the TYPE PREFIX (everything but the last segment) is
        # what might be in-scan, exact-matched the same way D-1 already
        # does for a plain import; the member itself is never resolved
        # (out of scope). The published target keeps the full original
        # spelling either way, for evidence - only the LOOKUP key strips
        # the trailing member/wildcard segment.
        type_prefix = edge.target[:-2] if edge.target.endswith(".*") else edge.target.rsplit(".", 1)[0]
        exact_unit_id = _exact_qualified_lookup(type_prefix, by_qualified_name)
        if exact_unit_id is not None:
            resolution_state, target_unit_id, confidence = "resolved", exact_unit_id, "high"
        else:
            resolution_state, target_external = "resolved", edge.target
    elif edge.target_kind == "internal_unqualified_call_candidate":
        # M12 (cold-read, PR-B fix round 3): an invoke call whose qualifier
        # is neither locally declared nor import-recognized - could be a
        # genuine same-package sibling (Java needs no import for that),
        # but the GLOBAL simple-name matcher would let an unrelated
        # same-named class ANYWHERE in the whole scan silently capture it
        # (a JDK-shadowing name, or a common test-helper name). Exact
        # qualified match only; otherwise unresolved, never a guess.
        exact_unit_id = _exact_qualified_lookup(edge.target, by_qualified_name)
        if exact_unit_id is not None:
            resolution_state, target_unit_id, confidence = "resolved", exact_unit_id, "high"
        else:
            resolution_state, target_unresolved = "unresolved", edge.target
    else:
        resolution_state = "resolved"
        target_external = edge.target

    return DependencyRecord(
        edge_id=digests.edge_id(
            from_unit_id=from_unit_id, relation=edge.relation, target=edge.target,
            phase=edge.phase,
        ),
        from_unit_id=from_unit_id,
        relation=edge.relation,
        phase=edge.phase,
        # M3 (fourth cold read, fix round 6): hardcoded False regardless
        # of what the adapter actually claimed - a Maven <dependency>'s
        # own <optional>true</optional> was read past and discarded,
        # publishing every pom edge as a positive "not optional" fact.
        optional=edge.optional,
        evidence_class=edge.evidence_class,
        resolution_state=resolution_state,
        target_unit_id=target_unit_id,
        target_external=target_external,
        target_unresolved=target_unresolved,
        confidence=confidence,
        producers=[_producer(
            name=java_adapter.ADAPTER_NAME, version=java_adapter.ADAPTER_VERSION,
            rule_version=java_adapter.RULE_VERSION, source_digest=source_digest,
        )],
    )
