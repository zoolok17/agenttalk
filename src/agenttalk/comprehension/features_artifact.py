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
from .dependencies_artifact import _build_registry, _java_file_unit_id


@dataclass(frozen=True)
class EntryPointRecord:
    entry_point_id: str
    kind: str
    name: str
    owning_unit_id: str
    feature_ids: list[str]
    evidence_class: str
    producers: list[dict[str, Any]] = field(default_factory=list)
    conflict_id: str | None = None
    confidence: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

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
        }


def entry_point_record_from_json(payload: dict[str, Any]) -> EntryPointRecord:
    return EntryPointRecord(
        entry_point_id=payload["entry_point_id"], kind=payload["kind"], name=payload["name"],
        owning_unit_id=payload["owning_unit_id"], feature_ids=list(payload["feature_ids"]),
        evidence_class=payload["evidence_class"], producers=list(payload.get("producers", [])),
        conflict_id=payload.get("conflict_id"), confidence=payload.get("confidence"),
        evidence=list(payload.get("evidence", [])),
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


def _producer(source_digest: str | None) -> dict[str, Any]:
    return {
        "producer": java_adapter.ADAPTER_NAME, "producer_version": java_adapter.ADAPTER_VERSION,
        "rule_version": java_adapter.RULE_VERSION, "basis": "extracted",
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


def build_features(
    java_results: dict[str, java_adapter.JavaFileResult],
    *, confirmed_labels: frozenset[str] = frozenset(),
    file_digests: dict[str, str] | None = None,
) -> tuple[list[EntryPointRecord], list[FeatureRecord]]:
    """Returns ``(entry_points, features)``. ``confirmed_labels`` names
    which candidate feature labels a ``config.json`` declaration confirms
    (state -> ``confirmed``) - an empty set (the default) means every
    feature stays ``candidate``, matching "a detector may create only a
    candidate."

    ``file_digests`` maps a relative path to discovery's own content
    digest for that file (M7, cold-read PR-B fix round 3: every producer
    here carried ``source_digest=None`` unconditionally - the design's
    per-fact producer identity, source content digest included, was never
    actually populated even though the digest was already computed and
    available upstream)."""
    file_digests = file_digests or {}
    (
        by_qualified_name, _by_simple_name, _file_unit_ids, _duplicate_names,
        _unit_ids_by_qname, _in_scan_packages,
    ) = _build_registry(java_results)

    owning_unit_by_qualified_name = by_qualified_name
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
            resolved_unit_id = owning_unit_by_qualified_name.get(claim.qualified_name)
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
        feature_id = digests.feature_id(label=label, unit_ids=[owning_unit_id])

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
            entry_point_id = digests.entry_point_id(
                kind=claim.kind, owning_unit_id=owning_unit_id, name=claim.name,
            )
            producer = _producer(file_digests.get(path))
            existing = entry_points_by_id.get(entry_point_id)
            if existing is None:
                entry_points_by_id[entry_point_id] = EntryPointRecord(
                    entry_point_id=entry_point_id, kind=claim.kind, name=claim.name,
                    owning_unit_id=owning_unit_id, feature_ids=[feature_id],
                    evidence_class=claim.evidence_class, producers=[producer],
                )
            elif producer not in existing.producers:
                entry_points_by_id[entry_point_id] = replace(
                    existing, producers=[*existing.producers, producer])

        entry_point_records.extend(entry_points_by_id.values())

        state = "confirmed" if label in confirmed_labels else "candidate"
        features.append(FeatureRecord(
            feature_id=feature_id, label=label, state=state, origin="detected",
            unit_ids=[owning_unit_id], entry_point_ids=list(entry_points_by_id),
            producers=[_producer(file_digests.get(owner_path))],
        ))

    return entry_point_records, features


def feature_record_from_json(payload: dict[str, Any]) -> FeatureRecord:
    return FeatureRecord(
        feature_id=payload["feature_id"], label=payload["label"], state=payload["state"],
        origin=payload["origin"], unit_ids=list(payload["unit_ids"]),
        entry_point_ids=list(payload["entry_point_ids"]), producers=list(payload.get("producers", [])),
        conflict_id=payload.get("conflict_id"), confidence=payload.get("confidence"),
        evidence=list(payload.get("evidence", [])),
    )
