"""Common JSON envelope, strict duplicate-key reading, and path safety.

Implements the "Common JSON envelope" and path-safety rules from
DESIGN-55-comprehension-plane.md: every persisted document uses UTF-8
without a BOM, stable key/record ordering, RFC 3339 UTC timestamps, and the
``schema_version``/``artifact_type``/``scan_id``/``generated_at`` identity.
Readers reject a duplicate object key at any nesting level before schema
validation, and reject a persisted path that is absolute, escapes the
project root, contains a NUL byte, or looks like a URL.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import EnvelopeError

_UTF8_BOM = b"\xef\xbb\xbf"
_RFC3339_UTC = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\Z"
)
_URL_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:")
_REQUIRED_ENVELOPE_FIELDS = ("schema_version", "artifact_type", "scan_id", "generated_at")


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` for :func:`json.loads` — mirrors the existing
    per-module convention (e.g. ``gates._reject_duplicate_members``): a
    document with two ``"key"`` members at the same nesting level is
    malformed input, not "last write wins" (design: "The strict JSON loader
    rejects duplicate object keys at every nesting level before schema
    validation")."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    """Parse ``text`` as JSON, rejecting a duplicate object key at any
    nesting level. Raises :class:`EnvelopeError` (never a bare
    ``json.JSONDecodeError``/``ValueError``) so every caller catches one
    type."""
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_members)
    except (json.JSONDecodeError, ValueError) as exc:
        raise EnvelopeError(f"malformed JSON: {exc}") from exc


def read_json_document(path: Path) -> Any:
    """Strictly read one JSON document from disk: UTF-8, no BOM, no
    duplicate keys. Raises :class:`EnvelopeError` on any I/O or format
    failure — a caller never needs to catch ``OSError`` separately."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EnvelopeError(f"could not read {path}: {exc}") from exc
    if raw.startswith(_UTF8_BOM):
        raise EnvelopeError(f"{path}: UTF-8 byte-order mark is not permitted")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvelopeError(f"{path}: not valid UTF-8: {exc}") from exc
    return strict_json_loads(text)


def validate_rfc3339_utc(value: Any, *, label: str) -> str:
    """Require an RFC 3339 UTC timestamp spelled with a trailing ``Z``
    (the design's ``generated_at`` example: ``2026-08-26T09:15:30Z``) — a
    numeric offset like ``+00:00`` is rejected even though it names the same
    instant, so every persisted timestamp has exactly one spelling."""
    if not isinstance(value, str) or not _RFC3339_UTC.match(value):
        raise EnvelopeError(f"{label} must be an RFC 3339 UTC timestamp ending in 'Z'")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise EnvelopeError(f"{label} is not a valid timestamp: {exc}") from exc
    return value


def validate_envelope(doc: Any, *, artifact_type: str, schema_version: int) -> dict:
    """Validate the common envelope identity of one already-parsed document.

    Readers accept the EXACT expected ``schema_version`` and reject a higher
    one outright (design: "reject missing required fields or a higher
    version"); accepting and migrating an older version is an explicit,
    separate reader capability that no PR-A caller needs yet, so it is not
    silently attempted here. Returns ``doc`` for chaining once validated.
    """
    if not isinstance(doc, dict):
        raise EnvelopeError("document must be a JSON object")
    for field in _REQUIRED_ENVELOPE_FIELDS:
        if field not in doc:
            raise EnvelopeError(f"document is missing required envelope field {field!r}")
    got_type = doc["artifact_type"]
    if got_type != artifact_type:
        raise EnvelopeError(
            f"artifact_type mismatch: expected {artifact_type!r}, got {got_type!r}")
    got_version = doc["schema_version"]
    if not isinstance(got_version, int) or isinstance(got_version, bool):
        raise EnvelopeError("schema_version must be an integer")
    if got_version > schema_version:
        raise EnvelopeError(
            f"schema_version {got_version} is newer than the {schema_version} this "
            f"reader understands")
    if got_version != schema_version:
        raise EnvelopeError(
            f"schema_version {got_version} is older than {schema_version}; no reader "
            f"migration is registered for {artifact_type!r}")
    scan_id = doc["scan_id"]
    if not isinstance(scan_id, str) or not scan_id:
        raise EnvelopeError("scan_id must be a non-empty string")
    validate_rfc3339_utc(doc["generated_at"], label="generated_at")
    return doc


def validate_relative_path(value: Any, *, label: str = "path") -> str:
    """Validate one persisted path as project-relative POSIX spelling.

    Rejects (design, "Local storage model" / "Common JSON envelope"):
    a non-string or empty value, a NUL byte, backslashes (not POSIX
    spelling — a raw Windows path must be normalized by its producer before
    it is ever persisted), an absolute path (POSIX ``/...`` or a Windows
    drive/UNC spelling), a URL-like value, and any ``..`` or empty segment
    (which would let a path climb above the root or hide a double-slash).
    Does not touch the filesystem — this is the syntactic half only; the
    caller resolves the result against a real root when it needs the
    stronger "does not escape the root on disk" guarantee.
    """
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise EnvelopeError(f"{label} must not contain a NUL byte")
    if "\\" in value:
        raise EnvelopeError(f"{label} must use POSIX spelling (no backslashes): {value!r}")
    if value.startswith("/") or _WINDOWS_DRIVE.match(value):
        raise EnvelopeError(f"{label} must be relative, not absolute: {value!r}")
    if _URL_SCHEME.match(value):
        raise EnvelopeError(f"{label} must not be URL-like: {value!r}")
    segments = value.split("/")
    for segment in segments:
        if segment in ("", "."):
            raise EnvelopeError(f"{label} has an empty or '.' segment: {value!r}")
        if segment == "..":
            raise EnvelopeError(f"{label} must not contain a '..' segment: {value!r}")
    return value


def resolve_under_root(value: Any, *, root: Path, label: str = "path") -> Path:
    """Syntactically validate, then prove the path resolves under ``root``
    on this filesystem (design: "paths resolving outside the project root
    are rejected"). Raises :class:`EnvelopeError` either way."""
    rel = validate_relative_path(value, label=label)
    resolved_root = root.resolve()
    resolved = (resolved_root / rel).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise EnvelopeError(f"{label} resolves outside the project root: {value!r}") from exc
    return resolved


def find_case_fold_collisions(paths: list[str]) -> list[tuple[str, str]]:
    """Return every distinct pair of ``paths`` that collide once
    case-folded — a scan problem (``case_collision``), never two silently
    merged units (design, "Common JSON envelope")."""
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for path in paths:
        key = path.casefold()
        prior = seen.get(key)
        if prior is not None and prior != path:
            collisions.append((prior, path))
        else:
            seen[key] = path
    return collisions
