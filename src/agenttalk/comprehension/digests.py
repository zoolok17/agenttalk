"""Exact-byte and canonical-content SHA-256 digests (#55 slice-1 spine).

Two distinct digest families, per DESIGN-55-comprehension-plane.md's
"Common JSON envelope" section:

- an EXACT-BYTE digest authenticates the concrete generation (differs
  whenever a single byte differs, including ``scan_id``/``generated_at``);
- a CANONICAL CONTENT digest answers "are these two generations
  content-equivalent", by hashing a projection that removes generation
  identity (``scan_id``, ``generated_at``, capture times, lock/owner
  tokens) while retaining everything else, including ordering.

Both use the same canonical-JSON-bytes recipe already established elsewhere
in this codebase (``signing.canonical_payload``, ``attention.source_hash``):
``sort_keys=True``, compact separators, ``ensure_ascii=False``.

MICRO-ROUND 38b (reviewer-3 delta on ``740a856``): this module follows TWO
DIFFERENT conventions for an optional string component, stated once here
rather than left implicit per function. ``entry_point_id`` and
``problem_id`` both fold ``qualified_name is None`` to the empty string
before hashing (``qualified_name or ""``) - deliberate, since for BOTH,
``None`` (unattributed) and ``""`` (an empty but present value) are meant
to be indistinguishable identity components, and no emitter in this
package ever actually publishes a literal empty string for either field
today (a synthetic owner is always ``path#name`` shaped). ``unit_id``
keeps ``qualified_name`` un-folded - ``None`` and ``""`` hash DIFFERENTLY
there, because a file-kind unit's ``qualified_name`` really is ``None``
(no declared type), while a component-kind unit's is always a real,
non-empty dotted name - collapsing the two there would conflate two
genuinely different unit shapes. Latent today (no id family has a real
empty-string emitter - the micro-round 38b leaf-decode fix in
``adapters/java.py`` refuses to publish an empty identity component at
all, rather than ever testing this distinction), but worth one explicit
sentence before a future emitter's own choice has to be reverse-
engineered from which function it happens to call.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Keys stripped at ANY nesting depth before a canonical content digest is
#: computed — generation identity, never content (design: "removes
#: `scan_id`, `generated_at`, capture times, lock/owner tokens, and any
#: other generation identity"). A caller with additional generation-identity
#: fields for its own artifact shape passes a wider ``strip_keys`` set.
#: MAJOR 3 (fifth cold read, fix round 8): round 6's N2 fix added
#: "started_at"/"completed_at" to scan.json (the design's own "start and
#: completion times" field, distinct from generated_at) but never added
#: them here - two content-identical scans, run at different wall-clock
#: moments, produced DIFFERENT canonical_content_digest(scan_doc) values,
#: contradicting this module's own docstring ("two byte-identical scans
#: two seconds apart produce... the SAME canonical content digest") and
#: invariant 7's equivalence claim for the ONE document this now anchors
#: (scan_json_anchor_state's own content_digest check, round 7's MAJOR
#: 3) - a real determinism gap published under a name that promises it,
#: not merely a latent one (run_content_digest never included scan.json
#: itself, so this never affected the RUN-level digest - only scan.json's
#: own).
#: MAJOR 3 (sixth cold read, fix round 9): round 8's fix (started_at/
#: completed_at above) was NOT sufficient - field-diffing two REAL
#: scans isolated scan.json's own artifacts[].byte_sha256: each OTHER
#: artifact's byte digest is computed over that artifact's OWN on-disk
#: bytes, which embed ITS OWN envelope's scan_id/generated_at - so
#: byte_sha256 IS generation identity, one level removed, and hashing
#: it into scan.json's canonical content digest imported that variance
#: right back in. Round 8's own determinism test used a hand-built
#: scan.json-shaped fixture that omitted the "artifacts" key entirely -
#: the exact shape that would have caught this - so it passed while the
#: real bug remained (fixture-conceals-the-defect, instance four).
#: byte_sha256 has no consumer outside scan_pipeline.py's own scan.json
#: artifacts summary (no other artifact shape uses this field name), so
#: stripping it here is unconditional and safe.
GENERATION_IDENTITY_KEYS = frozenset({
    "scan_id", "generated_at", "capture_time", "lock_token", "owner_token",
    "started_at", "completed_at", "byte_sha256",
})

_ROOT_BINDING_DOMAIN = b"agenttalk.comprehension.root_binding.v1\x00"
_UNIT_ID_DOMAIN = b"agenttalk.comprehension.unit_id.v1\x00"
_EDGE_ID_DOMAIN = b"agenttalk.comprehension.edge_id.v1\x00"
_ENTRY_POINT_ID_DOMAIN = b"agenttalk.comprehension.entry_point_id.v1\x00"
_FEATURE_ID_DOMAIN = b"agenttalk.comprehension.feature_id.v1\x00"
_SIGNAL_ID_DOMAIN = b"agenttalk.comprehension.signal_id.v1\x00"
_CONFLICT_ID_DOMAIN = b"agenttalk.comprehension.conflict_id.v1\x00"
_PROBLEM_ID_DOMAIN = b"agenttalk.comprehension.problem_id.v1\x00"


def sha256_bytes(data: bytes) -> str:
    """Exact-byte SHA-256, hex-encoded."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Any) -> str:
    """Exact-byte SHA-256 of a file's on-disk content, streamed so an
    oversized file never loads whole into memory before the publish-time
    ceiling check gets a chance to refuse it."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """The one canonical-JSON recipe this package hashes: sorted keys,
    compact separators, non-ASCII preserved rather than ``\\uXXXX``-escaped
    (escaping is reversible but would make two semantically-identical
    documents from different encoders hash differently)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _strip_generation_identity(value: Any, *, strip_keys: frozenset) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_generation_identity(val, strip_keys=strip_keys)
            for key, val in value.items()
            if key not in strip_keys
        }
    if isinstance(value, list):
        return [_strip_generation_identity(item, strip_keys=strip_keys) for item in value]
    return value


def canonical_content_digest(
    doc: Any, *, strip_keys: frozenset = GENERATION_IDENTITY_KEYS,
) -> str:
    """SHA-256 of the canonical JSON projection of ``doc`` with every
    ``strip_keys`` member removed at any nesting depth. Two byte-identical
    source scans two seconds apart produce different ``scan_id``/
    ``generated_at``/``capture_time`` values and therefore different
    exact-byte digests, but the SAME canonical content digest — this is the
    acceptance fixture named in the PR-A dispatch."""
    projection = _strip_generation_identity(doc, strip_keys=strip_keys)
    return sha256_bytes(canonical_json_bytes(projection))


def run_content_digest(artifacts: list[dict]) -> str:
    """``scan.json``'s run-level ``content_digest``: the canonical digest of
    the ordered tuple of (artifact_type, schema_version, record_count,
    artifact content_digest) for every artifact in the run (design,
    "Common JSON envelope": "scan.json computes its run-level
    content_digest from the ordered tuple of artifact type, schema version,
    record count, and artifact content digest"). ``artifacts`` must already
    be in the run's canonical (stable) order — this function does not sort,
    since sort order is itself part of the identity the design fixes.
    """
    tuples = [
        [a["artifact_type"], a["schema_version"], a["record_count"], a["content_digest"]]
        for a in artifacts
    ]
    return sha256_bytes(canonical_json_bytes(tuples))


def _domain_separated_id(domain: bytes, payload: Any) -> str:
    hasher = hashlib.sha256()
    hasher.update(domain)
    hasher.update(canonical_json_bytes(payload))
    return hasher.hexdigest()


def unit_id(*, kind: str, paths: list[str], qualified_name: str | None) -> str:
    """DESIGN-55-comprehension-plane.md, Artifact 1: "Deterministic SHA-256
    ID over unit kind, normalized path, and qualified name." ``paths`` is
    sorted here so caller ordering never perturbs the ID."""
    return _domain_separated_id(
        _UNIT_ID_DOMAIN,
        {"kind": kind, "paths": sorted(paths), "qualified_name": qualified_name},
    )


def edge_id(
    *, from_unit_id: str, relation: str, target: str, phase: str,
    from_qualified_name: str | None = None,
) -> str:
    """DESIGN-55-comprehension-plane.md, Artifact 2: "a deterministic
    edge_id and from_unit_id".

    FIX ROUND 39 (thirty-third cold read, F1(c) - re-running the
    collision hunt against every family's own DEGENERATE/FALLBACK
    inputs, per reviewer-3's own standard): ``from_qualified_name`` was
    NOT a hash input - two OUT-OF-SCAN declaring classes (real,
    different classes this scan cannot see - `dependencies_artifact.
    build_dependencies`'s own ``by_qualified_name.get(edge.from_
    qualified_name) or file_unit_id_by_path[path]`` fallback) both
    resolve ``from_unit_id`` to the SAME synthetic file unit; when they
    also share ``relation``/``target``/``phase`` (two out-of-scan
    servlets mapped to the identical ``<url-pattern>``, an ordinary
    real-world shape already named by round 31's own ``duplicate_
    route_target`` problem) their edges collided BY CONSTRUCTION and
    were silently coalesced into ONE published record, even though they
    are genuinely two different declaring classes' own facts - measured
    directly (two adapter-level edges collapsing to one dependency
    record). ``from_qualified_name`` is exactly the datum this
    producer's own emission site already has and already differs
    between the two - threaded into the id now, the same fix shape
    round 38/39 already applied to ``entry_point_id``/``feature_id``.
    Deliberately does NOT change the coalescing this id family's own
    ``_coalesce_by_edge_id`` intentionally performs for a genuinely
    repeated fact (the same class's own identical call site emitted
    more than once) - ``from_qualified_name`` is identical for those,
    so they still collide (and coalesce) exactly as before. ``None``
    folds to ``""``, per this module's own stated convention."""
    return _domain_separated_id(
        _EDGE_ID_DOMAIN,
        {
            "from_unit_id": from_unit_id, "relation": relation, "target": target, "phase": phase,
            "from_qualified_name": from_qualified_name or "",
        },
    )


def entry_point_id(
    *, kind: str, owning_unit_id: str, name: str, qualified_name: str | None = None,
) -> str:
    """FIX ROUND 38 (thirty-second cold read, F1 BLOCKER, wrong-data):
    ``qualified_name`` was NOT a hash input - two declarations whose
    classes are OUT OF SCAN both fall back to the SAME synthetic file
    owner (``features_artifact.build_features``'s own file-fallback
    path), so when they also share ``kind``/``name`` (e.g. two
    ``<servlet-mapping>``s naming the same ``<url-pattern>``, each
    backed by a different out-of-scan class) their ids collided BY
    CONSTRUCTION even though the records genuinely differ - two entry-
    point records, one distinct id, two features cross-claiming it.
    ``build_features`` already computes and uses this exact
    distinguishing datum (the claim's own declared ``qualified_name``)
    to keep the two under SEPARATE feature groups (``group_key``) - it
    was simply never carried into the id itself, the identical gap
    ``problem_id`` had before round 37's own F1 fix, now closed the same
    way: ``None`` hashes as the empty string (see ``problem_id``'s own
    docstring for why ``None``/``""`` are deliberately equivalent),
    distinct from any real qualified name."""
    return _domain_separated_id(
        _ENTRY_POINT_ID_DOMAIN,
        {
            "kind": kind, "owning_unit_id": owning_unit_id, "name": name,
            "qualified_name": qualified_name or "",
        },
    )


def feature_id(
    *, label: str, unit_ids: list[str], qualified_name: str | None = None, kind: str | None = None,
) -> str:
    """FIX ROUND 39 (thirty-third cold read, F1 BLOCKER, wrong-data):
    neither ``qualified_name`` nor ``kind`` was a hash input - for an
    OUT-OF-SCAN class, ``label`` is the SIMPLE name (``_feature_label``)
    and ``unit_ids`` is the SAME synthetic file-fallback unit, so two
    jar-shipped classes sharing a simple name in different packages
    (two ``LoginServlet``s - utterly ordinary) produced two DIFFERENT
    features sharing ONE id - the identical by-construction collision
    class ``entry_point_id`` had before round 38's own F1 fix, missed
    by round 38's own collision sweep because that sweep hunted
    canonicalisation/list-join ambiguity, never this family's own
    DEGENERATE-INPUT shape (a fallback owner plus a simple-name label).
    ``features_artifact.build_features`` already computes and uses the
    exact distinguishing datum (the claim's own full ``qualified_name``)
    to keep the two under SEPARATE feature groups (``group_key``) -
    threaded into the id itself now too, the same fix shape as
    ``entry_point_id``'s own. ``kind`` is threaded too (a filter and a
    servlet sharing a simple name and a fallback owner collided
    identically, proving kind was not discriminated either) - both
    ``None``-fold to the empty string, per this module's own stated
    convention (see the module docstring)."""
    return _domain_separated_id(
        _FEATURE_ID_DOMAIN,
        {
            "label": label, "unit_ids": sorted(unit_ids),
            "qualified_name": qualified_name or "", "kind": kind or "",
        },
    )


def signal_id(*, unit_id: str, check: str, policy_version: int) -> str:
    """DESIGN-55-comprehension-plane.md, Artifact 4: "Stable ID for unit,
    check, and policy version.\""""
    return _domain_separated_id(
        _SIGNAL_ID_DOMAIN,
        {"unit_id": unit_id, "check": check, "policy_version": policy_version},
    )


def conflict_id(*, conflict_kind: str, anchor: str, claim_digests: list[str]) -> str:
    """DESIGN-55-comprehension-plane.md, "Fact provenance and canonical
    merge": "the SHA-256 of the conflict kind, normalized anchor, and
    sorted canonical claim digests, with generation identity removed.\""""
    return _domain_separated_id(
        _CONFLICT_ID_DOMAIN,
        {"conflict_kind": conflict_kind, "anchor": anchor, "claim_digests": sorted(claim_digests)},
    )


def problem_id(
    *, reason_code: str, path: str | None, detail: str, qualified_name: str | None = None,
) -> str:
    """N3 (third cold read, fix round 5): DESIGN-55-comprehension-plane.md's
    ``problems.json`` section: "Each record has a stable ID and reason
    code, severity, producers, optional relative path and line, and a
    generated message." A stable ID over the record's own identifying
    fields, the same domain-separated-hash pattern every other artifact's
    stable ID already uses here.

    FIX ROUND 37 (thirty-first cold read, F1 BLOCKER - availability,
    lead's own override of round 36's per-site preference, ratified or
    overturned by reviewer-3): ``qualified_name`` was NOT a hash input -
    round 36b fixed two JAX-RS class-closer sites whose own distinguishing
    datum (the class) sat BESIDE the detail rather than in it, but the
    reader's own AST sweep found NINETEEN more emitters carrying the same
    coupling, all keyed on nothing sharper than a source LINE - two
    same-kind declarations sharing one line (an ordinary, minified/
    one-line web.xml with two ``<listener>`` elements) collide and the
    round-36 collision detector correctly, but catastrophically, bricks
    the whole scan. Per-site detail edits are the enumeration antipattern
    this arc keeps re-learning (19 sites today, unknown emitters
    tomorrow); ``qualified_name`` closes the class structurally, at the
    one chokepoint every problem record already passes through to get an
    id. ``None`` hashes as the empty string, distinct from any real
    qualified name - a change from unattributed to attributed (or vice
    versa) on an otherwise-identical record is itself a real content
    change, correctly a new id, never a collision. Round 36b's own
    "details carry their own distinguishing datum" rule stays in force as
    a readability SHOULD for the reactor/closer sites already updated -
    this is additive, not a reason to revert those.

    THE INVARIANT (micro-round 40b, reviewer-3's own delta on round 40's
    S1 - restated as a checkable rule, not merely a measurement): this
    id also hashes ``detail`` - and since round 41's own F4, ``detail``
    IS now uniformly routed through ``errors.bounded_detail`` (truncated
    to ``MAX_PROBLEM_DETAIL_LENGTH`` characters) before it ever reaches
    here, so a per-trigger detail template that puts its OWN
    distinguishing datum (the fact that makes two same-reason_code/
    same-path/same-qualified_name problems genuinely different) PAST
    that bound would let two truly different problems collide on this
    id - the identical class of bug F1/F2 fixed for a route's own
    identity.

    CORRECTED (round 41, thirty-fifth cold read, F4 MAJOR): this
    docstring's own "uniformly routed" claim was FALSE when micro-round
    40b wrote it - only java.py's own emitters ever called
    ``bounded_detail`` directly; ``scan_pipeline._problem_record`` (the
    ONE chokepoint every problem this run publishes actually passes
    through) did not call it at all, so 12 of that function's own 15
    call sites published a raw, unbounded detail - one measured at 707
    characters, 3.3x this function's own declared 214-char ceiling.
    Fixed at the chokepoint itself (``_problem_record`` now calls
    ``bounded_detail`` unconditionally, closing every current AND
    future caller in one place, never a per-site sweep again) - the
    claim above is true again, and now true STRUCTURALLY, not by
    convention. THE RULE, stated so it is auditable at template-writing
    time rather than re-derived by a future reader: every problem
    detail template must place its own distinguishing datum within the
    first ``MAX_PROBLEM_DETAIL_LENGTH`` characters (``errors.py``) - the
    same discipline round 36's own invariant ("a problem detail may
    assert a cause only if the branch that emitted it proved that
    cause; any per-trigger detail must include the trigger's own
    distinguishing datum, which also keeps problem_id unique") already
    established, restated here to name WHERE that datum must sit now
    that the detail is length-bounded.

    FIX ROUND 37c (LATENT note, reviewer-3's own delta on round 37b): a
    literal empty string ``""`` for ``qualified_name`` hashes identically
    to ``None`` (both fold to ``""`` below) - DELIBERATELY equivalent, not
    an accidental collision. No emitter today ever publishes an empty
    string (a synthetic owner is always ``path#name`` shaped, never
    empty), so this is presently unreachable - named here so a future
    emitter is not written believing an empty string is distinct from
    absent."""
    return _domain_separated_id(
        _PROBLEM_ID_DOMAIN,
        {
            "reason_code": reason_code, "path": path, "detail": detail,
            "qualified_name": qualified_name or "",
        },
    )


def root_binding_digest(resolved_root_spelling: str) -> str:
    """SHA-256 over a domain-separation prefix plus the canonical resolved
    project-root spelling (design: "binds a run to the exact local root
    without persisting the absolute path in artifacts... a privacy
    minimization, not a cryptographic secrecy claim"). The caller supplies
    the already-canonicalized spelling (POSIX-separated, case-policy
    applied per the platform identity recorded alongside it) — this
    function only owns the domain-separated hashing, not path
    canonicalization, which depends on platform facts this module does not
    have.
    """
    hasher = hashlib.sha256()
    hasher.update(_ROOT_BINDING_DOMAIN)
    hasher.update(resolved_root_spelling.encode("utf-8"))
    return hasher.hexdigest()
