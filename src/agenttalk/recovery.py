"""Out-of-tree store backup (#156 increment 1).

The bus store (``.agenttalk/``) lives entirely inside the project tree, so
``rm -rf .agenttalk`` / ``Remove-Item -Recurse`` / a routine ``git clean
-fdx`` (which deletes ignored directories) destroys every message, the
roster, and the archived cold storage in one shot - see issue #156. This
module writes a verified, out-of-tree snapshot a project can be rebuilt
from.

Design (per the #156 design-review comment's own quiescence requirement,
and the reviewer-3 GO-WITH-CHANGES read of the first draft):

- **Identity**: ``signing.project_id_for_root`` - a pure path hash, already
  audited (see that function's own docstring for why it is anchored to the
  resolved root path rather than anything stored inside the attacker/
  incident-writable ``.agenttalk/``). Delete-and-recreate at the same path
  recomputes the identical id, which is exactly the property restore needs.
- **Location**: mirrors ``signing.default_keys_dir()`` - a per-user
  directory genuinely outside any git working tree (``%LOCALAPPDATA%\\
  agenttalk\\recovery`` / ``$XDG_CONFIG_HOME/agenttalk/recovery``), so it
  survives both ``git clean -fdx`` and an ``rm -rf`` of the project root.
- **Consistency without an O(store size) fence**: issue #154 (still open)
  documents wrapper deaths from a generation-guard critical section that
  scales with store size (`_reserve_message_publication_sequence` calling
  `valid_messages()` while a lock is held). A full recursive copy of the
  store is strictly more work than that - holding the SAME class of lock
  across one would reproduce #154 at snapshot scale. Instead: fence
  (`Store._message_publication_lock`) only around a HARDLINK-based clone
  (`os.link` per file - directory-entry creation, not byte copying), whose
  duration is bounded by file COUNT, not aggregate size. The lock-free
  publication-order sequence is read once, inside the fence, immediately
  after the clone - not a substitute for the fence (the fence is what
  makes the clone itself a real point-in-time snapshot: this codebase's
  own write-temp-then-``os.replace`` convention, see ``_atomic.py``, never
  mutates the inode a hardlink already points to, so once cloned a file
  can't change under us even after the fence is released) - but a cheap,
  independently-verifiable number stamped into the manifest. Hashing and
  the manifest write happen AFTER the fence is released, against the now
  fully static hardlinked clone.
- **Fallback**: a filesystem/mount that does not support hardlinks (cross-
  device, some network/container filesystems) falls back to a byte copy
  for the whole clone - still correct, just slower (and the fence
  duration becomes proportional to store size again in that mode, which
  is the documented, unavoidable tradeoff of no hardlink support).

**Writer-atomicity audit** (the caveat reviewer-3 flagged as worth
verifying before relying on hardlink safety): every write inside
``store.py`` goes through ``_atomic.write_text``; ``supervisor.py``'s own
state file uses an equivalent local temp+fsync+``os.replace`` writer
(``_atomic_write_supervisor_json``). Three exceptions were found, all
self-documented as best-effort/advisory in their own module comments, none
of them core durable state: the dead-letter resolution sidecar
(``cli.py``'s ``cmd_attention_resolve``, "central log already durable +
authoritative"), the reply-refusal reason sidecar (``reply_transport.
write_refused_reason``, "never raises... strictly better than nothing"),
and the ``tail`` command's own follow-cursor (``wrapper_logs.py``,
re-derived as "start fresh" if stale/torn). A backup taken mid-write to one
of those three could contain a stale or missing sidecar; it can never
contain corrupted core message/config/cursor/health state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import _atomic
from . import __version__

RECOVERY_MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

# store.py's O_EXCL/generation-guard locks validate their own guard file's
# hardlink count == 1 on every acquisition (_validate_lock_file_stat) -
# deliberately, to detect exactly this kind of aliasing. Hardlinking one of
# these would not just corrupt the SNAPSHOT's own view of the lock; it would
# make every FUTURE acquisition on the LIVE store fail with "unsafe lock
# path" for as long as the backup (which is never deleted automatically)
# keeps the alias alive. Worse on Windows: the guard this module itself
# holds for the fence duration (message-publication) is byte-locked, so even
# a plain copy of it fails with a sharing-violation PermissionError while
# held - confirmed empirically, not just reasoned about. Lock/guard files
# are pure runtime coordination state, never durable data a restore needs
# (a fresh one is created on next use), so they are SKIPPED from the
# snapshot entirely rather than hardlinked or copied. Bare marker names are
# the known `_lock_generation_guard` callers (store.py: config.lock,
# supervisor-lifecycle.lock, powershell-host.lock, retirement,
# message-publication) plus supervisor.instance.lock (a separate OS-lock
# primitive, excluded out of the same caution). Every guard's own hidden
# file is named ``.<lock-name>.generation`` (store.py:1362) - matched by
# suffix so a future new lock is covered without another edit here.
_LOCK_ARTIFACT_NAMES = frozenset({
    "config.lock",
    "supervisor-lifecycle.lock",
    "powershell-host.lock",
    "supervisor.instance.lock",
    "retirement",
    "message-publication",
})


def _is_lock_artifact(name: str) -> bool:
    return name in _LOCK_ARTIFACT_NAMES or name.endswith(".generation")


def default_recovery_dir() -> Path:
    """Return the per-user recovery directory for this OS.

    Precedence: ``AGENTTALK_RECOVERY_DIR`` env var (mirrors
    ``signing.resolve_key_path``'s ``AGENTTALK_HMAC_KEY_FILE`` override
    precedence - explicit env always wins, both for tests that must never
    touch the real per-user location and for an operator who wants
    recovery storage on a different volume) > the default per-OS location.
    Otherwise mirrors ``signing.default_keys_dir()`` exactly (same base-dir
    resolution, different leaf) so recovery snapshots live beside the HMAC
    keys - both are per-user, both are genuinely outside any project tree.
    POSIX default: respects ``$XDG_CONFIG_HOME`` (defaults to
    ``~/.config``). Windows default: ``%LOCALAPPDATA%\\agenttalk\\recovery``.
    """
    env = os.environ.get("AGENTTALK_RECOVERY_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "agenttalk" / "recovery"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "agenttalk" / "recovery"


def recovery_project_dir(project_id: str) -> Path:
    """Per-project recovery root; one subdirectory per backup lives under
    here, named by UTC timestamp, each with its own self-contained
    manifest (increment-2-ready: ``restore`` picks a specific one, or the
    latest, without this module changing shape)."""
    return default_recovery_dir() / project_id


def _timestamp_dirname(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%S-%f")[:-3] + "Z"


@dataclass
class BackupResult:
    project_id: str
    destination: Path
    manifest_path: Path
    manifest_hash: str
    file_count: int
    total_bytes: int
    hardlink_used: bool
    sequence_at_snapshot: int | None
    fence_seconds: float
    files: dict[str, str] = field(default_factory=dict)


def _relative_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _probe_hardlink_support(store_dir: Path, staging: Path) -> bool:
    """Best-effort, cheap: try to hardlink one real file if one exists,
    else a throwaway probe file in the SAME parent as the destination
    staging dir (hardlinks cannot cross filesystem/mount boundaries, so
    the probe must live on the destination's own filesystem, not the
    source's)."""
    probe_src = staging.parent / f".hardlink-probe-{uuid.uuid4().hex}"
    probe_dst = staging.parent / f".hardlink-probe-{uuid.uuid4().hex}.link"
    try:
        probe_src.write_bytes(b"probe")
        os.link(probe_src, probe_dst)
        return True
    except OSError:
        return False
    finally:
        probe_src.unlink(missing_ok=True)
        probe_dst.unlink(missing_ok=True)


def _clone_tree(store_dir: Path, staging: Path, *, use_hardlinks: bool) -> None:
    for src in _relative_files(store_dir):
        if _is_lock_artifact(src.name):
            continue
        rel = src.relative_to(store_dir)
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if use_hardlinks:
            os.link(src, dst)
        else:
            shutil.copy2(src, dst)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(store, *, dest_root: Path | None = None,
                  now: datetime | None = None) -> BackupResult:
    """Write a verified out-of-tree snapshot of ``store`` and return its
    manifest info. Raises on any failure - a caller printing a success
    message only sees one once this has actually returned.

    Staged under a ``.tmp-<uuid>`` sibling first and only renamed into its
    final timestamped name once the manifest is fully written and
    self-verified, so a killed backup never leaves a directory that looks
    like a complete one (the same discipline #156's design comment asks
    of ``restore``, applied here to the write side too).
    """
    if not store.initialized():
        raise FileNotFoundError(f"agenttalk not initialized at {store.root}; nothing to back up.")
    now = now or datetime.now(timezone.utc)
    project_id = store.project_id()
    project_dir = dest_root or recovery_project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    final_name = _timestamp_dirname(now)
    staging = project_dir / f".tmp-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        use_hardlinks = _probe_hardlink_support(store.dir, staging)
        sequence_at_snapshot: int | None = None
        fence_start = time.monotonic()
        with store._message_publication_lock():
            _clone_tree(store.dir, staging, use_hardlinks=use_hardlinks)
            order = store._read_message_publication_order()
            sequence_at_snapshot = order["append_sequence"] if order else None
        fence_seconds = time.monotonic() - fence_start

        files: dict[str, str] = {}
        total_bytes = 0
        for p in _relative_files(staging):
            rel = p.relative_to(staging).as_posix()
            files[rel] = _hash_file(p)
            total_bytes += p.stat().st_size

        manifest_body = {
            "schema_version": RECOVERY_MANIFEST_SCHEMA_VERSION,
            "project_id": project_id,
            "project_root": str(store.root),
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "agenttalk_version": __version__,
            "hardlink_used": use_hardlinks,
            "sequence_at_snapshot": sequence_at_snapshot,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
        }
        manifest_hash = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        manifest_body["manifest_hash"] = manifest_hash
        manifest_path = staging / MANIFEST_FILENAME
        _atomic.write_text(
            manifest_path,
            json.dumps(manifest_body, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        )

        # Self-verify before publishing: re-hash and compare, so a
        # transcription bug can never produce a manifest that lies about
        # its own clone.
        for rel, expected in files.items():
            actual = _hash_file(staging / rel)
            if actual != expected:
                raise ValueError(
                    f"backup self-check failed: {rel} hash changed between "
                    f"snapshot and manifest write ({expected} -> {actual})"
                )

        final_dir = project_dir / final_name
        if final_dir.exists():
            # Timestamp collision (sub-millisecond, extremely unlikely) -
            # never silently overwrite a prior backup.
            final_dir = project_dir / f"{final_name}-{uuid.uuid4().hex[:8]}"
        os.replace(staging, final_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return BackupResult(
        project_id=project_id,
        destination=final_dir,
        manifest_path=final_dir / MANIFEST_FILENAME,
        manifest_hash=manifest_hash,
        file_count=len(files),
        total_bytes=total_bytes,
        hardlink_used=use_hardlinks,
        sequence_at_snapshot=sequence_at_snapshot,
        fence_seconds=fence_seconds,
        files=files,
    )
