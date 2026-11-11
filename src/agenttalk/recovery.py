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
  (`Store._message_publication_lock`) only around the clone, which is made
  of HARDLINKS (`os.link` per file - directory-entry creation, not byte
  copying, so that part depends on the file COUNT, not the aggregate size).
  The lock is held while the store is cloned and while the publication
  order is read and checked. Files that are copied byte by byte - launch
  requests and lane delivery records (always), any file whose link fails,
  and every file on a disk without hardlinks - take longer the more data
  there is, and that time is spent inside the lock too. The lock-free
  publication-order sequence is read once, inside the fence, immediately
  after the clone - not a substitute for the fence (the fence is what
  makes the clone itself a real point-in-time snapshot: this codebase's
  own write-temp-then-``os.replace`` convention, see ``_atomic.py``, never
  mutates the inode a hardlink already points to, so once cloned a file
  can't change under us even after the fence is released) - but a cheap,
  independently-verifiable number stamped into the manifest. Hashing and
  the manifest write happen AFTER the fence is released, against the now
  fully static clone.
- **Fallback**: a filesystem/mount that does not support hardlinks (cross-
  device, some network/container filesystems) falls back to a byte copy
  for the whole clone (the probe links one real store file into the backup
  folder; a file whose link still fails later is copied on its own, and the
  manifest's ``hardlink_used`` is true only if every file was linked) -
  still correct, just slower (and the fence
  duration becomes proportional to the amount of data again in that mode,
  which is the documented, unavoidable tradeoff of no hardlink support; the
  always-copied files and any file whose link fails add their copying time
  to the fence in the same way, even when the rest of the store is linked).

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
from . import store as _store_mod
from . import __version__

RECOVERY_MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

# store.py's O_EXCL/generation-guard locks validate their own guard file's
# hardlink count == 1 on every acquisition (_validate_lock_file_stat) -
# deliberately, to detect exactly this kind of aliasing. Hardlinking one of
# these would not just corrupt the SNAPSHOT's own view of the lock; it would
# make every FUTURE acquisition on the LIVE store fail with "unsafe lock
# path" for as long as the backup (which is never deleted automatically)
# keeps the alias alive. Worse on Windows: a guard this module itself holds
# for the fence duration (message-publication) is byte-locked, so even a
# plain copy of it fails with a sharing-violation PermissionError while held
# - confirmed empirically, not just reasoned about. Lock/guard files are
# pure runtime coordination state, never durable data a restore needs (a
# fresh one is created on next use), so they are SKIPPED from the snapshot
# entirely rather than hardlinked or copied.
#
# The name predicate itself is NOT hand-maintained here (reviewer-3's PR
# #177 finding: a hand list here missed several `_exclusive_lock`-class
# marker files - operation-publication.lock, per-agent waiting/awaiting/
# lead-loop-lease locks - none individually tested, one of them reproduced
# the live wedge in an executed proof). ``store.is_lock_or_guard_artifact``
# is the single source of truth store.py's own lock code is defined next
# to, covering every current lock by naming convention (suffix-based, so a
# FUTURE lock is covered by construction) rather than needing a matching
# edit here every time a new lock is added.
_is_lock_artifact = _store_mod.is_lock_or_guard_artifact


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
    hardlink_used: bool  # every included file was hard-linked; copy-only files (below) are always copied
    sequence_at_snapshot: int | None
    fence_seconds: float
    files: dict[str, str] = field(default_factory=dict)
    files_linked: int = 0
    files_copied: int = 0  # eligible for a link but copied without trying: the probe found no hard-link support
    files_link_failed: int = 0  # a link was tried and failed (a store can span mounts); copied instead
    files_copied_on_purpose: int = 0  # launch requests and lane delivery records: see store.is_copy_only_backup_path


def mode_summary(result: BackupResult) -> str:
    """The words for `mode:` in the command's output, true for every mix of linked and copied files."""
    on_purpose = (
        f"{result.files_copied_on_purpose} launch-request and lane-delivery files are always copied"
        if result.files_copied_on_purpose
        else ""
    )
    failed = f"{result.files_link_failed} copied because a link failed" if result.files_link_failed else ""
    if result.hardlink_used:
        return "hardlink"
    if result.files_linked:
        return f"hardlink ({', '.join(x for x in (on_purpose, failed) if x)})"
    if result.files_link_failed:  # links were tried, none worked: that is not "no hardlink support"
        return f"copy (every link failed: {', '.join(x for x in (failed, on_purpose) if x)})"
    if result.files_copied:
        return "copy (no hardlink support)" + (f"; {on_purpose}" if on_purpose else "")
    return "copy (no file was eligible for linking" + (f"; {on_purpose}" if on_purpose else "") + ")"


class BackupStoreChanged(ValueError):
    """The store kept changing while the backup copied it. A ValueError, which `agenttalk backup` and `main()` already
    turn into a message and exit code 2."""


class _Vanished(Exception):
    """A folder or file listed for this pass was gone when it was reached: the pass is thrown away."""


# One budget, started inside the lock before the first listing and never reset: at most this many passes, and no new
# pass starts once the time has gone by (throwing an attempt away counts). It limits when a new pass may START; it
# does not bound how long the lock is held, and a file operation already running can outlast it. Senders waiting for
# the same lock give up after 10 seconds.
_MAX_PASSES = 3
_PASS_BUDGET_SECONDS = 3.0
_monotonic = time.monotonic


def _iter_files(root: Path):
    """Every file under ``root``. Unlike ``Path.rglob`` with ``is_file`` (which read a folder or file it cannot open as
    "nothing there" and return a shorter list), a failure raises: FileNotFoundError or NotADirectoryError when it
    vanished, any other OSError when it cannot be read."""
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file():
                    yield Path(entry.path)


def _list_files(root: Path) -> list[Path]:
    return sorted(_iter_files(root))


def _probe_hardlink_support(store_dir: Path, staging: Path) -> bool:
    """Best-effort, cheap: hardlink one REAL store file into the staging dir
    and remove that link again. A hardlink cannot cross a filesystem, drive or
    mount boundary, so the probe has to run between the same two places the
    clone will use (#505: a probe that linked two files inside the backup
    folder only proved the destination could link, then the clone failed when
    the store was on another drive). A store with no real file yet is copied. It picks a file by the same
    rules as the clone: never a lock file or helper name, never a copy-only file."""
    try:
        src = next(
            (
                p
                for p in _iter_files(store_dir)
                if not _is_lock_artifact(p.name)
                and not _store_mod.is_copy_only_backup_path(p.relative_to(store_dir).as_posix())
            ),
            None,
        )
    except OSError:
        return False  # a guess that cannot be made means copying, which is always correct
    if src is None:
        return False
    probe = staging / f".hardlink-probe-{uuid.uuid4().hex}"
    try:
        os.link(src, probe)
        return True
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


@dataclass
class _Clone:
    included: list[str] = field(default_factory=list)  # relative paths of every file made, posix style
    linked: int = 0
    copied: int = 0
    link_failed: int = 0
    on_purpose: int = 0


def _clone_tree(store_dir: Path, staging: Path, *, use_hardlinks: bool) -> _Clone:
    """One pass: clone the store into `staging`.

    A file whose link fails (the probe tried one file, a store can span mounts) is copied instead, so the clone
    completes and the manifest does not claim hard links it did not use. Lock files and their helper names are left
    out; copy-only files (see store.is_copy_only_backup_path) are copied, never linked. A folder or file that vanishes
    raises _Vanished; any other error, a listing error included, propagates and refuses the backup."""
    clone = _Clone()
    try:
        files = _list_files(store_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _Vanished(str(exc)) from exc
    for src in files:
        if _is_lock_artifact(src.name):
            continue
        rel = src.relative_to(store_dir)
        posix = rel.as_posix()
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_only = _store_mod.is_copy_only_backup_path(posix)
        link_failed = False
        if use_hardlinks and not copy_only:
            try:
                os.link(src, dst)
                clone.linked += 1
                clone.included.append(posix)
                continue
            except OSError:
                link_failed = True
        try:
            shutil.copy2(src, dst)
        except FileNotFoundError as exc:
            if os.path.lexists(src):
                raise  # the source is there: the copy failed for another reason (a too-long path on Windows)
            raise _Vanished(posix) from exc
        clone.included.append(posix)
        if copy_only:
            clone.on_purpose += 1
        elif link_failed:
            clone.link_failed += 1
        else:
            clone.copied += 1
    return clone


def _clone_with_retries(store_dir: Path, staging: Path, *, use_hardlinks: bool) -> _Clone:
    """Run passes until one completes with nothing vanished, within the budget above. A pass that finds something gone
    is thrown away completely (if that fails, the error refuses the backup) and the store is listed again: skipping
    the file would publish a snapshot that holds neither the old name nor the one it was moved to."""
    started = _monotonic()
    passes = 0
    while passes < _MAX_PASSES and (passes == 0 or _monotonic() - started <= _PASS_BUDGET_SECONDS):
        passes += 1
        try:
            return _clone_tree(store_dir, staging, use_hardlinks=use_hardlinks)
        except _Vanished:
            shutil.rmtree(staging)
            staging.mkdir()
    raise BackupStoreChanged(
        f"the store kept changing while the backup copied it ({passes} passes, {_monotonic() - started:.1f} s); "
        "nothing was published. Run agenttalk backup again."
    )


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

    Under the store fence (``AGENTTALK_STORE_FENCE``) the destination must lie
    inside the fence, like the store; otherwise ``StoreFenceError`` is raised
    before any folder is made or file written.
    """
    if not store.initialized():
        raise FileNotFoundError(f"agenttalk not initialized at {store.root}; nothing to back up.")
    now = now or datetime.now(timezone.utc)
    project_id = store.project_id()
    project_dir = dest_root or recovery_project_dir(project_id)
    # A test or probe under the fence must not write its backup into the real per-user folder (#427).
    _store_mod.check_folder_fence(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    final_name = _timestamp_dirname(now)
    staging = project_dir / f".tmp-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        # A hard link gives each store file a second name in the snapshot, which the store
        # fence (tests and probes) refuses; under the fence the snapshot is a copy instead.
        use_hardlinks = (
            not os.environ.get(_store_mod.STORE_FENCE_ENV)
            and _probe_hardlink_support(store.dir, staging)
        )
        sequence_at_snapshot: int | None = None
        fence_start = time.monotonic()
        with store._message_publication_lock():
            clone = _clone_with_retries(store.dir, staging, use_hardlinks=use_hardlinks)
            order = store._read_message_publication_order()
            sequence_at_snapshot = order["append_sequence"] if order else None
        fence_seconds = time.monotonic() - fence_start
        hardlink_used = clone.linked > 0 and clone.copied + clone.link_failed + clone.on_purpose == 0

        files: dict[str, str] = {}
        total_bytes = 0
        for p in _list_files(staging):
            rel = p.relative_to(staging).as_posix()
            files[rel] = _hash_file(p)
            total_bytes += p.stat().st_size
        if sorted(files) != sorted(clone.included):  # what the store listing gave, not only what the folder now holds
            raise ValueError("backup self-check failed: the snapshot does not hold exactly the files that were copied")

        manifest_body = {
            "schema_version": RECOVERY_MANIFEST_SCHEMA_VERSION,
            "project_id": project_id,
            "project_root": str(store.root),
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "agenttalk_version": __version__,
            "hardlink_used": hardlink_used,
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
        hardlink_used=hardlink_used,
        sequence_at_snapshot=sequence_at_snapshot,
        fence_seconds=fence_seconds,
        files=files,
        files_linked=clone.linked,
        files_copied=clone.copied,
        files_link_failed=clone.link_failed,
        files_copied_on_purpose=clone.on_purpose,
    )
