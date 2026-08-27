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
GENERATION_IDENTITY_KEYS = frozenset({
    "scan_id", "generated_at", "capture_time", "lock_token", "owner_token",
})

_ROOT_BINDING_DOMAIN = b"agenttalk.comprehension.root_binding.v1\x00"


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
