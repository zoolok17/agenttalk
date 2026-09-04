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
import os
import re
import stat as stat_module
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import EnvelopeError, bounded_os_error_detail

_UTF8_BOM = b"\xef\xbb\xbf"
_RFC3339_UTC = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\Z"
)
_URL_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:")
_REQUIRED_ENVELOPE_FIELDS = ("schema_version", "artifact_type", "scan_id", "generated_at")
#: A closed, path-traversal-proof grammar: starts with an alphanumeric,
#: then any mix of alphanumerics/dash/underscore, 1-128 chars. No '.',
#: '/', '\\', whitespace, or control characters are POSSIBLE at all — this
#: is a path-traversal defense, not merely a format check (reviewer-1
#: cold-read finding 2 on PR-A, rq-6cc5560b62f6, reproduced:
#: scan_id="../../../../escaped" wrote a staging owner.json OUTSIDE the
#: protected root, because scan_id was interpolated into a path
#: unvalidated). The design's own example format
#: ("20260826T091530Z-a1b2c3d4") fits this grammar; it is intentionally
#: looser than that exact shape so it does not also encode a producer
#: policy decision that belongs to PR-B/C.
_SCAN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def require_field(doc: dict[str, Any], key: str, *, doc_name: str) -> Any:
    """MAJOR 2 (fifth cold read, fix round 8): the ONE typed-access
    helper for every field read off an already-loaded, previously-
    published document (scan.json, index.json, a per-artifact digest-
    summary entry inside scan.json, ...) - a missing key raises the
    same typed :class:`EnvelopeError` every other malformed-document
    shape this package already raises, never a bare ``KeyError``. Round
    6 closed this class for per-artifact RECORD conversion
    (``_records``); round 7 closed it for scan.json's own top-level
    scalar fields (``_scan_field``); round 8 closes it AS A CLASS: every
    remaining raw subscript into a loaded document - on both the READ
    path (``get_status``/``get_report``/``validate_run``,
    ``_verify_artifact_digests``'s per-artifact digest-summary entries)
    and the WRITE path (``publish._build_successor_index``'s own read of
    a prior index) - routes through this one helper instead of a
    fourth, fifth, and sixth hand-rolled guard."""
    try:
        return doc[key]
    except KeyError as exc:
        raise EnvelopeError(f"{doc_name} is missing required field {key!r}") from exc


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
    failure — a caller never needs to catch ``OSError`` separately.

    FIX ROUND 21 (seventeenth cold read, CR17-8 MINOR, the class-closer):
    all three failure messages here used to embed ``path`` - the FULL
    absolute local path - directly; an ``OSError`` embeds it a SECOND
    time via its own ``str(exc)`` (``exc.filename``). Every message now
    names only the file's own basename (enough to identify WHICH
    artifact failed) plus, for the OS-error case, ``bounded_os_error_
    detail`` (the same machine-local-path-leak-safe helper worker.py's
    own M-3 fix already established) rather than the raw exception
    text."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EnvelopeError(
            bounded_os_error_detail(f"could not read {path.name}", exc)) from exc
    if raw.startswith(_UTF8_BOM):
        raise EnvelopeError(f"{path.name}: UTF-8 byte-order mark is not permitted")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvelopeError(f"{path.name}: not valid UTF-8: {exc}") from exc
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
    validate_scan_id(doc["scan_id"], label="scan_id")
    validate_rfc3339_utc(doc["generated_at"], label="generated_at")
    return doc


def validate_scan_id(value: Any, *, label: str = "scan_id") -> str:
    """Validate ``value`` against the closed scan-ID grammar. MUST run
    BEFORE any filesystem path is constructed from a scan_id (design's
    ``runs/<scan-id>/`` and ``.staging/<scan-id>-<nonce>/`` paths) — see
    the ``_SCAN_ID`` module comment for why this is a security boundary,
    not just a format check.
    """
    if not isinstance(value, str) or not _SCAN_ID.match(value):
        raise EnvelopeError(
            f"{label} must match {_SCAN_ID.pattern} (alphanumeric, dash, or "
            f"underscore only) — got {value!r}")
    return value


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


def path_is_reparse_point_or_symlink(path: Path) -> bool:
    """``True`` iff ``path`` EXISTS and is itself a symlink or (Windows)
    directory reparse point (junction/mount point) — mirrors discovery.py's
    own ``_boundary_kind`` (same ``lstat`` + ``FILE_ATTRIBUTE_REPARSE_
    POINT`` technique, the attribute Windows Explorer/``dir`` use, so this
    also catches reparse tags this module has never heard of, not just
    symlinks and junctions by name).

    A path that does not exist yet is NOT a reparse point (``False``,
    never fail-closed here) — unlike a tree WALK over already-listed
    dirents (where a vanished entry is a genuine TOCTOU race worth
    treating conservatively), this function is also called on store paths
    (``runs/``, ``.staging/``) that legitimately do not exist yet on a
    project's very first scan; treating "not created yet" as "refuse"
    would brick every first publish. Any OTHER ``OSError`` (permission
    denied, etc.) IS treated as unverifiable and fails closed (``True``)
    — the same asymmetry ``_boundary_kind`` draws between "confirmed
    absent" and "could not be confirmed"."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat_module.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt" and st.st_file_attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return False


def resolve_under_root(value: Any, *, root: Path, label: str = "path") -> Path:
    """Syntactically validate, then prove the path resolves under ``root``
    on this filesystem (design: "paths resolving outside the project root
    are rejected"). Raises :class:`EnvelopeError` either way.

    MICRO-ROUND 50 (Cluster 0, B1 BLOCKER, the worst finding of the arc):
    the OLD body called ``root.resolve()`` first, then checked ``(root /
    rel).resolve()`` against THAT — a confinement proof that is vacuous
    by construction the moment ``root`` (or an already-existing directory
    between ``root`` and the target) is ITSELF a symlink or a Windows
    directory reparse point/junction: resolving ``root`` FIRST bakes the
    redirection into ``resolved_root`` itself, so the target's ``relative_
    to(resolved_root)`` trivially succeeds even though the real bytes
    land wherever the reparse point actually points, entirely outside the
    project the caller believes ``root`` denotes. Reproduced exactly:
    a reparse point placed AT ``.agenttalk/comprehension/runs`` (or
    ``.staging``) redirected an entire published run outside the pinned
    store root — six artifacts physically outside the repository, git
    genuinely unaware of them, while this producer's own privacy proof
    (asked about the NOMINAL store path, never the resolved destination —
    see ``privacy.verify_store_ignored``'s own round-50 fix) published
    ``vcs_privacy: "ignored"`` — a false proof about the exact property
    the whole design gates writing on.

    FIX, fail-closed: walk every path SEGMENT from ``root`` down to the
    target BEFORE ever calling ``.resolve()`` on anything, checking each
    already-existing one (``root`` itself included) with :func:`path_is_
    reparse_point_or_symlink`. A hit refuses immediately, naming the
    offending segment and a remedy — never silently followed, never
    resolved through. Only once every segment is proven reparse-point-
    free does the existing resolve-and-confine check run, now as a
    genuine (not vacuous) final sanity net."""
    rel = validate_relative_path(value, label=label)
    if path_is_reparse_point_or_symlink(root):
        raise EnvelopeError(
            f"{label} root {root} is a symlink or a directory reparse point/junction - "
            "refusing to trust any path built under it; remove the reparse point (or "
            "point the store at a real directory) and retry")
    walked = root
    for segment in rel.split("/")[:-1]:
        walked = walked / segment
        if path_is_reparse_point_or_symlink(walked):
            raise EnvelopeError(
                f"{label} {value!r} passes through {walked}, a symlink or a directory "
                "reparse point/junction, before reaching its own target - refusing to "
                "trust a path that crosses one; remove the reparse point and retry")
    resolved_root = root.resolve()
    resolved = (resolved_root / rel).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise EnvelopeError(f"{label} resolves outside the project root: {value!r}") from exc
    return resolved


def find_case_fold_collisions(paths: list[str]) -> list[tuple[str, str]]:
    """Return one ``(first, second)`` pair for every OTHER path that
    collides, once case-folded, with the FIRST path this scan sees at
    that key — a scan problem (``case_collision``), never two silently
    merged units (design, "Common JSON envelope").

    MICRO-ROUND 49 (forty-third cold read, polish): this docstring used
    to claim "every distinct pair" - false for a genuine 3+-way
    collision group (three paths sharing one case-folded key): ``seen[
    key]`` is set once and never updated (below), so the SECOND and
    THIRD paths both pair with the FIRST, never with each other - two
    pairs returned for three colliding paths, not the three a literal
    "every distinct pair" (full pairwise) reading would promise.
    Anchoring every pair to the group's first-seen representative is
    sufficient (collision is transitive - every member already shares
    one edge to the anchor) and is what every caller already assumes
    (see ``scan_pipeline.py``'s own round-42 comment on this exact
    shape) - restated here as fact, not changed.

    FIX ROUND 36 (thirtieth cold read, F4 MAJOR, completeness): the key
    used to be ``casefold()`` alone — two Unicode NFC/NFD canonical-
    equivalent spellings of the IDENTICAL visible name (a precomposed
    accented character, e.g. ``"é"`` U+00E9, versus the decomposed form
    ``"e"`` + a combining acute accent, U+0065 U+0301) casefold to
    DIFFERENT strings, since casefold never normalizes composition — so
    this detector missed them entirely even though ``platform_identity``
    already publishes ``unicode_normalizing: false`` (this producer's own
    admission that both forms can coexist on disk) and a consumer handed
    either path opens whichever form its own normalizer happens to emit,
    reading the other one's digest as stale. The key now NFC-normalizes
    before casefolding, so a normalization-variant pair collides here
    too — see :func:`is_pure_case_fold_collision` for the per-pair
    TRUTHFUL cause (never assert "case-folds identically" for a pair
    that only collides once normalized, the exact invariant this whole
    round adopts)."""
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for path in paths:
        key = unicodedata.normalize("NFC", path).casefold()
        prior = seen.get(key)
        if prior is not None and prior != path:
            collisions.append((prior, path))
        else:
            seen[key] = path
    return collisions


def is_pure_case_fold_collision(first: str, second: str) -> bool:
    """FIX ROUND 36 (F4 MAJOR): distinguishes, for one already-detected
    colliding pair, whether a BARE case-fold (no Unicode normalization)
    already proves the two identical - the only case
    :func:`find_case_fold_collisions`'s own ``case_collision`` detail
    ("case-folds identically to ...") is actually true for. ``False``
    means the pair collides only once NFC-normalized - a Unicode
    canonical-equivalence difference the caller must describe honestly
    instead, never as a bare case-fold claim."""
    return first.casefold() == second.casefold()
