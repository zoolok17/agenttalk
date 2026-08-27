"""``scan.lock``: single-writer lock with provable stale recovery.

DESIGN-55-comprehension-plane.md, "Local storage model":

    scan.lock permits one writer per initialized project. The scanner
    creates it exclusively before enumerating source and records a random
    owner token, PID, process-start identity, host identity, acquisition
    time, and predecessor-index digest. A second scanner refuses while
    that owner is live. A lock is reclaimed automatically only when the
    recorded local process identity is definitely dead; an unverifiable or
    remote-looking owner requires the explicit attended
    `--recover-stale-lock` action. PID reuse cannot prove death because the
    process-start identity must also match.

The stale-death proof reuses ``lifecycle_lock.process_observation`` /
``process_identity`` instead of reimplementing cross-platform exact-process
identity (the lead-approved reuse decision for this PR). Unlike
``lifecycle_lock.CrossProcessLifecycleLock`` — a persistent 4096-byte
artifact held via an OS byte-range lock for short mutual exclusion — this
lock is a plain "create it exclusively" lockfile: a second acquire attempt
during a live hold hits ``FileExistsError`` and refuses IMMEDIATELY (no
poll/wait, matching the design's contention contract), and a clean release
DELETES the file so the next acquire's exclusive-create simply succeeds. A
crash leaves the file behind in ``state: "held"`` — that is the provable
evidence this module's stale-recovery path reasons about.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..lifecycle_lock import ProcessIdentity, process_identity, process_observation
from .digests import root_binding_digest
from .envelope import EnvelopeError, read_json_document, validate_rfc3339_utc
from .errors import PrivacyProofRootMismatch, ScanLockContended, ScanLockUnrecoverable
from .paths import lock_path as _lock_path
from .paths import project_root_from_comprehension_dir
from .privacy import PrivacyPreflightResult, _canonical_root_spelling

LOCK_SCHEMA_VERSION = 1
_VALID_VCS_PRIVACY = ("ignored", "acknowledged_unignored", "no_vcs_acknowledged")


def host_identity() -> str:
    """The current host's identity for the lock record. A function (not a
    module-level constant) so tests can monkeypatch it to simulate a
    remote-looking / different-host lock record without touching the real
    hostname.

    Deliberately does NOT import ``socket`` (reviewer-1 cold-read finding 3
    on PR-A, rq-6cc5560b62f6: the design's offline contract prohibits
    network-capable imports in this package's production modules,
    regardless of whether a specific call like ``gethostname()`` ever
    opens a connection — the allowlist gate is a static per-import check,
    not a per-call trust judgment). ``platform.node()`` queries the OS's
    local hostname API directly (``uname(2)`` nodename on POSIX,
    ``COMPUTERNAME`` on Windows) without this package importing a
    network-capable module itself, and is stable for the lifetime of the
    host between reboots — exactly the determinism scan.lock's cross-host
    detection needs (same host -> same value every time this process or
    any other reads it). The ``os.environ`` fallback only matters on the
    rare host where the OS API itself returns nothing.
    """
    node = platform.node()
    if node:
        return node
    env_fallback = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME")
    if env_fallback:
        return env_fallback
    raise ScanLockUnrecoverable(
        "this host's identity could not be determined (platform.node() and the "
        "COMPUTERNAME/HOSTNAME environment variables are all empty)")


@dataclass(frozen=True)
class ScanLockHandle:
    """The live handle returned by :func:`acquire_scan_lock`. Pass it to
    :func:`release_scan_lock` to release — a handle from a DIFFERENT
    acquisition (different ``owner_token``) can never release this one."""

    path: Path
    owner_token: str
    pid: int
    process_identity: ProcessIdentity
    host_identity: str
    acquired_at: str
    predecessor_index_digest: str | None
    vcs_privacy: str
    work_id: str | None


def _utc_now_iso(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _record_to_json(handle: ScanLockHandle) -> dict:
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "state": "held",
        "owner_token": handle.owner_token,
        "pid": handle.pid,
        "process_identity": {
            "scheme": handle.process_identity.scheme,
            "value": handle.process_identity.value,
        },
        "host_identity": handle.host_identity,
        "acquired_at": handle.acquired_at,
        "predecessor_index_digest": handle.predecessor_index_digest,
        "vcs_privacy": handle.vcs_privacy,
        "work_id": handle.work_id,
    }


def _validate_lock_record(doc: Any) -> dict:
    if not isinstance(doc, dict):
        raise EnvelopeError("scan.lock record must be a JSON object")
    if doc.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise EnvelopeError(
            f"scan.lock schema_version must be {LOCK_SCHEMA_VERSION}, got "
            f"{doc.get('schema_version')!r}")
    if doc.get("state") != "held":
        raise EnvelopeError(f"scan.lock state must be 'held', got {doc.get('state')!r}")
    if not isinstance(doc.get("owner_token"), str) or not doc["owner_token"]:
        raise EnvelopeError("scan.lock owner_token must be a non-empty string")
    pid = doc.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise EnvelopeError("scan.lock pid must be a positive integer")
    identity = doc.get("process_identity")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("scheme"), str)
        or not isinstance(identity.get("value"), str)
    ):
        raise EnvelopeError("scan.lock process_identity must be a {scheme, value} object")
    if not isinstance(doc.get("host_identity"), str) or not doc["host_identity"]:
        raise EnvelopeError("scan.lock host_identity must be a non-empty string")
    validate_rfc3339_utc(doc.get("acquired_at"), label="scan.lock acquired_at")
    predecessor = doc.get("predecessor_index_digest")
    if predecessor is not None and not isinstance(predecessor, str):
        raise EnvelopeError("scan.lock predecessor_index_digest must be a string or null")
    if doc.get("vcs_privacy") not in _VALID_VCS_PRIVACY:
        raise EnvelopeError(
            f"scan.lock vcs_privacy must be one of {_VALID_VCS_PRIVACY}, got "
            f"{doc.get('vcs_privacy')!r}")
    work_id = doc.get("work_id")
    if work_id is not None and not isinstance(work_id, str):
        raise EnvelopeError("scan.lock work_id must be a string or null")
    return doc


def _write_exclusive(path: Path, doc: dict) -> None:
    """Create ``path`` if and only if it does not already exist — the
    mutual-exclusion primitive itself. Raises ``FileExistsError`` (never
    wrapped) so the caller's contention/reclaim branch stays explicit."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(path)
        raise


def _read_lock_record(path: Path) -> dict:
    try:
        doc = read_json_document(path)
    except EnvelopeError as exc:
        raise ScanLockUnrecoverable(f"scan.lock record is malformed: {exc}") from exc
    try:
        return _validate_lock_record(doc)
    except EnvelopeError as exc:
        raise ScanLockUnrecoverable(f"scan.lock record is malformed: {exc}") from exc


def _classify_and_maybe_reclaim(path: Path) -> None:
    """The lock file already exists. Read it, decide whether the recorded
    owner is a live contender (refuse), definitely dead (reclaim: delete
    and let the caller retry the exclusive create once), or merely
    unverifiable (refuse, attended-recovery-only)."""
    record = _read_lock_record(path)
    if record["host_identity"] != host_identity():
        # A lock recorded on a different host can never be proven dead from
        # here (design: "an unverifiable or remote-looking owner").
        raise ScanLockUnrecoverable(
            f"scan.lock was recorded on a different host ({record['host_identity']!r})")
    status, observed = process_observation(record["pid"])
    if status == "dead":
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return  # reclaimed; caller retries the exclusive create
    if status == "alive":
        recorded = record["process_identity"]
        if (
            observed is not None
            and observed.scheme == recorded["scheme"]
            and observed.value == recorded["value"]
        ):
            raise ScanLockContended(record)
        # Same PID, but the exact process-start identity does not match —
        # PID reuse. This can NEVER prove death (design: "PID reuse cannot
        # prove death because the process-start identity must also match").
        raise ScanLockUnrecoverable(
            f"scan.lock pid {record['pid']} is alive but its process-start identity "
            "does not match the recorded owner (PID reuse) — this cannot prove the "
            "original owner is dead")
    raise ScanLockUnrecoverable(
        f"scan.lock pid {record['pid']}'s liveness could not be observed exactly "
        f"on this platform")


def acquire_scan_lock(
    comprehension_dir: Path,
    *,
    privacy: PrivacyPreflightResult,
    predecessor_index_digest: str | None,
    now: datetime | None = None,
) -> ScanLockHandle:
    """Acquire the single-writer lock, reclaiming a definitely-dead stale
    holder automatically. Raises :class:`ScanLockContended` for a live
    owner or :class:`ScanLockUnrecoverable` for anything that cannot be
    proven dead. Reclaim happens at most once per call — if a third party
    wins the race immediately after reclaim, that shows up as ordinary
    contention on the retry, never a loop.

    ``privacy`` is REQUIRED, with no default (reviewer-3 B-1 on PR-A,
    rq-5bd5427ad64d, reproduced: without this, the privacy preflight was a
    function a caller could simply forget to call, and three entry points
    created ``.agenttalk/comprehension/`` unconditionally — an untracked,
    unignored write reachable even when the preflight refuses). This is
    the FIRST thing that creates the directory, so requiring a proven
    :class:`~.privacy.PrivacyPreflightResult` here — obtainable only from
    :func:`privacy.run_privacy_preflight` or
    :func:`privacy.acknowledge_unignored_private_store`, never fabricated
    — makes the precondition structural rather than procedural. The
    disposition is recorded into the lock record itself (``vcs_privacy``,
    ``work_id``) so it is durable from the very first byte written.

    ``privacy`` must also be BOUND to ``comprehension_dir``'s own project
    root — a proof obtained for a different root is rejected with
    :class:`PrivacyProofRootMismatch` (reviewer-1 cold-read finding 1 on
    PR-A, rq-6cc5560b62f6, reproduced: a real proof from protected root A
    previously unlocked a write in unrelated root B, since ``isinstance``
    was the only check performed).
    """
    if not isinstance(privacy, PrivacyPreflightResult):
        raise TypeError(
            "acquire_scan_lock() requires privacy: PrivacyPreflightResult - obtain one "
            "from privacy.run_privacy_preflight() or "
            "privacy.acknowledge_unignored_private_store(), never fabricate one")
    project_root = project_root_from_comprehension_dir(comprehension_dir)
    expected_binding = root_binding_digest(_canonical_root_spelling(project_root))
    if privacy.root_binding != expected_binding:
        raise PrivacyProofRootMismatch(
            f"the privacy proof was issued for a different project root than "
            f"{project_root} - a proof is only valid at the exact root it was "
            "obtained for")
    self_identity = process_identity(os.getpid())
    if self_identity is None:
        raise ScanLockUnrecoverable(
            "this process's own identity could not be observed exactly")
    path = _lock_path(comprehension_dir)
    handle = ScanLockHandle(
        path=path,
        owner_token=uuid.uuid4().hex,
        pid=os.getpid(),
        process_identity=self_identity,
        host_identity=host_identity(),
        acquired_at=_utc_now_iso(now),
        predecessor_index_digest=predecessor_index_digest,
        vcs_privacy=privacy.vcs_privacy,
        work_id=privacy.work_id,
    )
    # Bounded retries: each FileExistsError is classified, which either
    # raises a typed error (contended/unrecoverable — the common case) or
    # reclaims a definitely-dead holder and falls through to retry. The
    # bound only guards against a pathological repeated-reclaim race (a
    # dead lock reappearing every attempt); an ordinary race against a
    # live writer is resolved by _classify_and_maybe_reclaim's own
    # ScanLockContended on the very next attempt, well inside the bound.
    for _attempt in range(5):
        try:
            _write_exclusive(path, _record_to_json(handle))
            return handle
        except FileExistsError:
            _classify_and_maybe_reclaim(path)
    raise ScanLockUnrecoverable(
        "scan.lock could not be acquired after repeated reclaim attempts — a stale "
        "lock keeps reappearing; investigate before retrying")


def release_scan_lock(handle: ScanLockHandle) -> None:
    """Release a lock this process holds. Verifies the on-disk record still
    names THIS handle's ``owner_token`` before deleting — a mismatch means
    something else already reclaimed or replaced it, which is a bug in the
    caller (double-release, or releasing after a reclaim), not a condition
    to paper over."""
    try:
        record = read_json_document(handle.path)
    except EnvelopeError as exc:
        raise ScanLockUnrecoverable(
            f"scan.lock could not be read at release time: {exc}") from exc
    if not isinstance(record, dict) or record.get("owner_token") != handle.owner_token:
        raise ScanLockUnrecoverable(
            "scan.lock no longer names this handle's owner_token — refusing to delete "
            "a lock this process does not own")
    os.remove(handle.path)


def recover_stale_lock(comprehension_dir: Path) -> None:
    """Attended-only override: unconditionally clear an existing scan.lock.

    This function performs no attendance check itself — proving an
    interactive terminal and explicit operator confirmation is the CLI's
    job (PR-B's ``--recover-stale-lock`` flag). By the time this is called,
    the operator has already confirmed the prior scan is gone; this is the
    single place that acts on that confirmation, so PR-B's CLI has exactly
    one internal call to make.
    """
    path = _lock_path(comprehension_dir)
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)
