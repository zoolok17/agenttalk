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


def edge_id(*, from_unit_id: str, relation: str, target: str, phase: str) -> str:
    """DESIGN-55-comprehension-plane.md, Artifact 2: "a deterministic
    edge_id and from_unit_id"."""
    return _domain_separated_id(
        _EDGE_ID_DOMAIN,
        {"from_unit_id": from_unit_id, "relation": relation, "target": target, "phase": phase},
    )


def entry_point_id(*, kind: str, owning_unit_id: str, name: str) -> str:
    return _domain_separated_id(
        _ENTRY_POINT_ID_DOMAIN,
        {"kind": kind, "owning_unit_id": owning_unit_id, "name": name},
    )


def feature_id(*, label: str, unit_ids: list[str]) -> str:
    return _domain_separated_id(
        _FEATURE_ID_DOMAIN, {"label": label, "unit_ids": sorted(unit_ids)},
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
    this is additive, not a reason to revert those."""
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
