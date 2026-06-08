"""HMAC-SHA256 message signing (optional, opt-in).

Threat model (see SECURITY.md): another local user on a shared
machine who can read/write the project's ``.agenttalk/`` directory
but who CANNOT read the OS account's private config dir
(``~/.config/agenttalk/keys/`` on POSIX,
``%LOCALAPPDATA%\\agenttalk\\keys\\`` on Windows). HMAC proves
which key wrote each message byte; it does NOT defend against
the same OS user, does NOT solve replay/deletion/reordering, and
does NOT validate timestamps for freshness.

Wire format (stable as of v0.6.0):

- ``meta.signature_version`` = ``"v1"``
- ``meta.signature_alg`` = ``"hmac-sha256"``
- ``meta.key_id`` = the path-derived project_id (sha256 of the
  absolute project root path; NOT a secret — identifies which key
  was used so a future rotation can match). See
  ``project_id_for_root`` for why this is path-derived rather than
  stored in attacker-writable ``.agenttalk/config.json``.
- ``meta.signed_at`` = ISO 8601 UTC timestamp the signer used
  (diagnostic only — NOT enforced as freshness, since clock skew
  would create avoidable failure modes)
- ``meta.signature`` = hex-encoded HMAC-SHA256 of the canonical
  payload

Canonical payload is the message dict with ``meta.signature``
removed (every other field including the rest of meta is signed)
serialized via::

    json.dumps(payload, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False).encode("utf-8")

A golden test in ``tests/test_signing.py`` locks this format in.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SIGNATURE_VERSION = "v1"
SIGNATURE_ALG = "hmac-sha256"
_KEY_BYTES = 32  # 256-bit HMAC key per RFC 2104 recommendation
_MIN_KEY_BYTES = 16  # RFC 2104 floor: keys shorter than the hash's block
# minimum are weak. sign/verify/load/inspect ALL enforce this so a
# degenerate (empty/short/garbage) key can never present as a working,
# "enforced" signing setup (review C*).


# --------------------------------------------------------------- canonicalization

def canonical_payload(msg_dict: dict) -> bytes:
    """Return the exact byte sequence that gets HMAC'd.

    Excludes only ``meta.signature``. Every other field — including
    the rest of ``meta`` (signature_version, signature_alg, key_id,
    signed_at) — is part of the signed payload.
    """
    if not isinstance(msg_dict, dict):
        raise TypeError("canonical_payload requires a dict")
    # Shallow copy so we don't mutate caller; deep-clone meta because
    # we strip a key from it.
    out = dict(msg_dict)
    meta = dict(out.get("meta") or {})
    meta.pop("signature", None)
    out["meta"] = meta
    return json.dumps(
        out, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


# ------------------------------------------------------------------- project id

def project_id_for_root(root: Path) -> str:
    """Derive a stable project ID from the absolute project root path.

    Used as the filename stem for the per-user HMAC key file. Plain
    SHA-256 of ``str(root.resolve())`` — not a secret (the project
    path is observable to anyone with read access to the parent
    directory). The security boundary is the OS ACL on the keys
    directory, not the unguessability of the ID.

    **Why this is path-derived in 0.6.0+** (vs. the v0.6.0-iter-1/2
    design that stored project_id in `.agenttalk/config.json`):
    config.json is inside the attacker-writable project directory.
    The iter-2 review proved that even with HMAC "enabled", an
    attacker could rewrite `project_id` to point at a UUID with no
    corresponding key file — which made `signing_enforced()` return
    false. Anchoring project_id to ``Store.root`` (the absolute path
    derived from CWD / ``--root`` flag) closes that bypass:
    ``find_root()`` returns the same value regardless of what's
    inside ``.agenttalk/``.

    **Tradeoff:** moving the project directory changes its
    ``project_id``, which means the existing key file is no longer
    found. Workaround: re-run ``agenttalk hmac-init`` at the new
    location. Documented in SECURITY.md.
    """
    canonical = str(Path(root).resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# -------------------------------------------------------------- key locations

def default_keys_dir() -> Path:
    """Return the per-user keys directory for this OS.

    POSIX: respects ``$XDG_CONFIG_HOME`` (defaults to
    ``~/.config``). Windows: ``%LOCALAPPDATA%\\agenttalk\\keys``.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "agenttalk" / "keys"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "agenttalk" / "keys"


def default_key_path(project_id: str) -> Path:
    return default_keys_dir() / f"{project_id}.key"


def resolve_key_path(project_id: str, *, override: str | os.PathLike | None = None) -> Path:
    """Return the file path the key should live at.

    Precedence: explicit ``override`` arg > ``AGENTTALK_HMAC_KEY_FILE``
    env var > ``default_key_path(project_id)``.
    """
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get("AGENTTALK_HMAC_KEY_FILE")
    if env:
        return Path(env).expanduser()
    return default_key_path(project_id)


# ------------------------------------------------------------- key file I/O

def _write_key_file(path: Path, key: bytes) -> None:
    """Atomically write the key with the tightest portable mode.

    Uses a temp-file-then-rename like the rest of the codebase. On
    POSIX, sets mode 0600 before the rename so the key is never
    world-readable. Windows: NTFS inherits the parent ACL — we
    document the user-account-private invariant in SECURITY.md.
    """
    from agenttalk._atomic import write_text as _atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # 0o700 (RWX owner, no access for group/other) is INTENTIONAL:
        # the HMAC defense rests on the keys dir being unreadable by
        # other local users. semgrep's heuristic suggests 0o644 which
        # would make the keys world-readable and defeat the defense.
        try:
            os.chmod(path.parent, 0o700)  # nosemgrep
        except OSError:
            pass
    # Write hex-encoded (one line, trailing newline) for human
    # inspection. The on-wire signature is over the *raw* bytes.
    _atomic_write_text(path, key.hex() + "\n")
    if os.name != "nt":
        # 0o600 — owner read/write only. Same reasoning as the parent
        # dir above: semgrep's "wider is better" suggestion would
        # break the security model.
        try:
            os.chmod(path, 0o600)  # nosemgrep
        except OSError:
            pass


def _read_key_file(path: Path) -> bytes:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError as e:
        raise ValueError(
            f"key file at {path} is not a valid hex string: {e}"
        ) from e
    if len(key) < _MIN_KEY_BYTES:
        # An empty/whitespace file decodes to b'' and a 2-hex-char file to
        # 1 byte; either would silently HMAC under a weak/public key while
        # signing_enforced() (existence-only) still reports green. Reject
        # on every read path so the failure is loud, not forgeable.
        raise ValueError(
            f"key file at {path} decodes to {len(key)} bytes; HMAC keys must "
            f"be at least {_MIN_KEY_BYTES} bytes (run `agenttalk hmac-init` to "
            f"generate a {_KEY_BYTES}-byte key)"
        )
    return key


def init_key(project_id: str, *, override: str | os.PathLike | None = None,
             force: bool = False) -> Path:
    """Generate a fresh 256-bit key and write it to the conventional
    location. Returns the path written.

    Refuses to overwrite an existing key unless ``force=True`` — the
    user has to opt in to "throw away the existing trust anchor."
    """
    path = resolve_key_path(project_id, override=override)
    if path.exists() and not force:
        raise FileExistsError(
            f"key already exists at {path}. Pass force=True to overwrite "
            f"(every message signed with the old key becomes unverifiable)."
        )
    _write_key_file(path, secrets.token_bytes(_KEY_BYTES))
    return path


def load_key(project_id: str, *,
             override: str | os.PathLike | None = None) -> bytes:
    """Read the key. Raises FileNotFoundError or ValueError on issues."""
    path = resolve_key_path(project_id, override=override)
    if not path.exists():
        raise FileNotFoundError(
            f"no key file at {path}. Run `agenttalk hmac-init` first."
        )
    return _read_key_file(path)


# ---------------------------------------------------------- key file health

@dataclass
class KeyHealth:
    path: Path
    exists: bool
    readable: bool
    posix_mode: int | None       # None on Windows or unreadable
    mode_warning: str | None     # set if mode is too permissive
    in_project_dir: bool         # True = configuration error
    project_root: Path
    valid: bool = False          # key file parses as hex AND meets length floor
    key_error: str | None = None  # why it's invalid (set iff readable & not valid)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "readable": self.readable,
            "posix_mode": (oct(self.posix_mode) if self.posix_mode is not None
                           else None),
            "mode_warning": self.mode_warning,
            "in_project_dir": self.in_project_dir,
            "valid": self.valid,
            "key_error": self.key_error,
        }


def inspect_key(project_id: str, project_root: Path, *,
                override: str | os.PathLike | None = None) -> KeyHealth:
    """Diagnostic-only inspection, consumed by ``status`` and ``doctor``."""
    path = resolve_key_path(project_id, override=override)
    health = KeyHealth(
        path=path, exists=False, readable=False, posix_mode=None,
        mode_warning=None,
        in_project_dir=_path_inside(path, project_root),
        project_root=project_root,
    )
    if not path.exists():
        return health
    health.exists = True
    try:
        path.read_bytes()
        health.readable = True
    except OSError:
        health.readable = False
    if health.readable:
        # Parse + length-check via the same gate load_key uses, so doctor
        # can never report a degenerate/garbage key as enabled-OK (review C*).
        try:
            _read_key_file(path)
            health.valid = True
        except ValueError as e:
            health.valid = False
            health.key_error = str(e)
    if os.name != "nt":
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = None
        health.posix_mode = mode
        if mode is not None and (mode & 0o077):
            health.mode_warning = (
                f"key file mode is {oct(mode)} but should be 0o600 "
                f"(group/other-readable keys defeat the HMAC defense). "
                f"Run: chmod 600 {path}"
            )
    return health


def _path_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------- sign / verify operations

def sign_message(msg_dict: dict, key: bytes, *,
                 key_id: str, signed_at: str | None = None) -> dict:
    """Return a copy of ``msg_dict`` with signature metadata attached.

    Mutates only the returned copy. Idempotent: signing an already-
    signed message replaces the old signature.
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) < _MIN_KEY_BYTES:
        raise ValueError(f"HMAC key must be at least {_MIN_KEY_BYTES} bytes")
    out = dict(msg_dict)
    meta = dict(out.get("meta") or {})
    meta.pop("signature", None)  # replace any existing
    meta["signature_version"] = SIGNATURE_VERSION
    meta["signature_alg"] = SIGNATURE_ALG
    meta["key_id"] = key_id
    meta["signed_at"] = signed_at or _now_iso()
    out["meta"] = meta
    sig = hmac.new(key, canonical_payload(out), hashlib.sha256).hexdigest()
    out["meta"]["signature"] = sig
    return out


def verify_message(msg_dict: dict, key: bytes, *,
                   expected_key_id: str | None = None) -> None:
    """Raise ValueError if the signature is missing / mismatched /
    using an unexpected algorithm. Returns None on success.

    Uses ``hmac.compare_digest`` for constant-time comparison.
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) < _MIN_KEY_BYTES:
        # Mirror sign_message: a too-short key must fail loudly here too,
        # so the weak-key invariant is local to verification and a future
        # in-process caller can't silently verify under a degenerate key.
        raise ValueError(f"HMAC key must be at least {_MIN_KEY_BYTES} bytes")
    meta = msg_dict.get("meta") or {}
    if "signature" not in meta:
        raise ValueError("missing signature")
    if meta.get("signature_version") != SIGNATURE_VERSION:
        raise ValueError(
            f"unsupported signature_version {meta.get('signature_version')!r}"
        )
    if meta.get("signature_alg") != SIGNATURE_ALG:
        raise ValueError(
            f"unsupported signature_alg {meta.get('signature_alg')!r}"
        )
    if expected_key_id is not None and meta.get("key_id") != expected_key_id:
        raise ValueError(
            f"key_id mismatch: message claims {meta.get('key_id')!r}, "
            f"this project's id is {expected_key_id!r}"
        )
    claimed = meta["signature"]
    # A non-string signature value (e.g. a JSON list smuggled into a
    # message file) would make hmac.compare_digest raise TypeError, which
    # escapes every read-path's `except ValueError` gate and crashes the
    # whole bus — including list_invalid_messages, so the poison file
    # could not even be quarantined (0.18.0 BLOCKER). Treat it as just
    # another invalid signature.
    if not isinstance(claimed, str):
        raise ValueError("signature is not a string")
    actual = hmac.new(key, canonical_payload(msg_dict), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(claimed, actual):
        raise ValueError("signature mismatch")


# ------------------------------------------------------------------ helper

def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"))
