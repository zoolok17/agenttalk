"""Publication: rename staging -> runs/<scan-id>/, then CAS-replace index.json.

DESIGN-55-comprehension-plane.md, "Local storage model":

    Publication uses this sequence:

    1. Rename the staging directory to a new, never-before-existing
       `runs/<scan-id>/` path on the same volume. It never replaces a run
       directory.
    2. Write the complete successor index to a unique sibling temporary
       file, flush and close it, recheck the predecessor digest, then
       replace `index.json`.
    3. Release the lock only after the index replacement or a reported
       failure.

    On Windows, all scanner handles are closed before either rename. A
    sharing violation receives bounded exponential retries for at most two
    seconds. If it persists, the operation fails and the old index remains
    current. [...] A crash after step 1 but before step 2 leaves a valid
    unindexed run, never a half-current run; v1 does not silently adopt it.

Each step below is a SEPARATE, independently callable function rather than
one monolithic sequence — that is what lets the test suite inject a "crash"
between any two steps by simply not calling the next one, instead of
needing a special test-only hook inside a combined function. ``publish_run``
is the ordinary orchestrator that calls all three in order and always
releases the lock (design step 3: "after the index replacement OR a
reported failure" — the only case the lock stays held is an unreported
crash, exactly the scenario scan.lock's stale recovery exists for).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .ceilings import enforce_artifact_ceilings, measure_staging_artifacts
from .digests import canonical_content_digest
from .envelope import read_json_document, validate_envelope
from .errors import ComprehensionError
from .lock import ScanLockHandle, release_scan_lock
from .paths import index_path as _index_path
from .paths import run_dir as _run_dir
from .staging import StagingHandle

INDEX_SCHEMA_VERSION = 1
INDEX_ARTIFACT_TYPE = "agenttalk.comprehension.index"
_INDEX_RUNS_MAX = 50
_RENAME_RETRY_TIMEOUT_SECONDS = 2.0


class RunDirectoryExists(ComprehensionError):
    """design: "It never replaces a run directory." — publishing a
    ``scan_id`` that already has a ``runs/<scan_id>/`` directory is a
    caller bug (scan IDs must be unique), not a retryable condition."""

    reason_code = "comprehension_run_directory_exists"


class RenamePublishFailed(ComprehensionError):
    """The staging-to-runs rename failed even after the bounded Windows
    sharing-violation retry window. The old index remains current; nothing
    under ``runs/`` or ``index.json`` was touched."""

    reason_code = "comprehension_rename_publish_failed"


class IndexCasConflict(ComprehensionError):
    """The live ``index.json`` no longer matches the digest captured when
    the writer lock was acquired — a concurrent or manual writer changed it
    out from under this publish. The prior index is left exactly as it was
    (design: "leaves the prior index current, and returns a command
    error")."""

    reason_code = "comprehension_index_cas_conflict"


def _utc_now_iso(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _is_windows() -> bool:
    """A function (not a direct ``os.name`` read) so tests can simulate
    Windows/POSIX by patching THIS — matches ``_atomic._is_windows``'s
    existing rationale exactly."""
    return os.name == "nt"


def rename_staging_to_run(
    comprehension_dir: Path, staging_handle: StagingHandle, *, scan_id: str,
) -> Path:
    """Publish step 1: rename the staging directory to
    ``runs/<scan_id>/``. Bounded exponential retry on a Windows sharing
    violation, for at most ~2 seconds total; a POSIX failure (or an
    exhausted Windows retry) raises immediately and touches nothing else.
    """
    dst = _run_dir(comprehension_dir, scan_id)
    if dst.exists():
        raise RunDirectoryExists(f"runs/{scan_id}/ already exists — scan IDs must be unique")
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _is_windows():
        os.rename(staging_handle.path, dst)
        return dst
    deadline = time.monotonic() + _RENAME_RETRY_TIMEOUT_SECONDS
    delay = 0.01
    while True:
        try:
            os.rename(staging_handle.path, dst)
            return dst
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise RenamePublishFailed(
                    f"publishing runs/{scan_id}/ failed after retrying a Windows "
                    f"sharing violation for {_RENAME_RETRY_TIMEOUT_SECONDS}s: {exc}"
                ) from exc
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.25)


def read_current_index(comprehension_dir: Path) -> tuple[dict | None, str | None]:
    """Returns ``(index_doc, content_digest)`` — ``(None, None)`` when no
    index has ever been published yet. A caller captures the digest at lock
    acquisition time as the CAS precondition for the publish that follows.
    """
    path = _index_path(comprehension_dir)
    if not path.exists():
        return None, None
    doc = read_json_document(path)
    validate_envelope(doc, artifact_type=INDEX_ARTIFACT_TYPE, schema_version=INDEX_SCHEMA_VERSION)
    return doc, canonical_content_digest(doc)


def _build_successor_index(
    prior_doc: dict | None, *, scan_id: str, run_summary: dict,
    predecessor_digest: str | None, now: datetime | None,
) -> dict:
    prior_runs = list(prior_doc["runs"]) if prior_doc else []
    runs = ([dict(run_summary)] + prior_runs)[:_INDEX_RUNS_MAX]
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "artifact_type": INDEX_ARTIFACT_TYPE,
        "scan_id": scan_id,
        "generated_at": _utc_now_iso(now),
        "latest_scan_id": scan_id,
        "predecessor_digest": predecessor_digest,
        "runs": runs,
    }


def publish_index_cas(
    comprehension_dir: Path,
    *,
    scan_id: str,
    run_summary: dict,
    predecessor_index_digest: str | None,
    now: datetime | None = None,
) -> dict:
    """Publish step 2: CAS-write the successor ``index.json``.

    Re-reads the LIVE index immediately before replacing it and compares
    against ``predecessor_index_digest`` (captured by the caller at lock
    acquisition) — this is the actual compare-and-set. On a mismatch,
    raises :class:`IndexCasConflict` and leaves the prior index file
    completely untouched.
    """
    if not isinstance(run_summary, dict) or not run_summary.get("scan_id"):
        raise ComprehensionError("run_summary must be a dict with a non-empty scan_id")
    live_doc, live_digest = read_current_index(comprehension_dir)
    if live_digest != predecessor_index_digest:
        raise IndexCasConflict(
            "index.json changed since the writer lock was acquired — a concurrent or "
            "manual writer conflict; the prior index remains current")
    successor = _build_successor_index(
        live_doc, scan_id=scan_id, run_summary=run_summary,
        predecessor_digest=predecessor_index_digest, now=now,
    )
    path = _index_path(comprehension_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(successor, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        # Recheck immediately before the replace (design: "flush and close
        # it, recheck the predecessor digest, then replace index.json") —
        # narrows, though cannot fully close, the TOCTOU window against a
        # writer that changed index.json between our first read and now.
        _recheck_doc, recheck_digest = read_current_index(comprehension_dir)
        if recheck_digest != predecessor_index_digest:
            raise IndexCasConflict(
                "index.json changed since the writer lock was acquired (caught on the "
                "pre-replace recheck) — the prior index remains current")
        _replace_with_windows_retry(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
    return successor


def _replace_with_windows_retry(src: Path, dst: Path) -> None:
    """``os.replace`` — same short (<200ms) bounded Windows retry as
    ``_atomic._replace_with_retry``; this call site does not need the
    longer 2-second window (that one is specific to the staging-to-runs
    directory rename step, per the design's own separate bound)."""
    if not _is_windows():
        os.replace(src, dst)
        return
    for delay in (0.01, 0.02, 0.04, 0.08):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)


def publish_run(
    comprehension_dir: Path,
    *,
    staging_handle: StagingHandle,
    lock_handle: ScanLockHandle,
    scan_id: str,
    run_summary: dict,
    predecessor_index_digest: str | None,
    now: datetime | None = None,
    record_counts: dict[str, int] | None = None,
) -> dict:
    """The ordinary end-to-end orchestrator: enforce the durable-artifact
    ceilings, rename, CAS-write the index, then always release the lock —
    on success AND on any REPORTED (caught) failure alike (design step 3).
    A real process crash mid-sequence never reaches the ``finally`` below
    at all, which is exactly what leaves the lock file behind for the next
    scanner's stale-recovery path to reason about; tests exercise that by
    calling the individual step functions directly instead of this
    orchestrator.

    ``record_counts`` maps each staged artifact filename to its record
    count for the ceiling check (design: "16 MiB and 100,000 records per
    artifact, and 64 MiB and 250,000 records for all durable artifacts in
    one run") — omit it (or a given filename) to measure that artifact's
    record count as 0, which only makes the record ceiling MORE permissive
    for a caller that has not measured it, never less; the byte ceiling is
    always measured directly from disk regardless.
    """
    try:
        measurements = measure_staging_artifacts(
            staging_handle.path, record_counts=record_counts or {})
        enforce_artifact_ceilings(measurements)
        rename_staging_to_run(comprehension_dir, staging_handle, scan_id=scan_id)
        return publish_index_cas(
            comprehension_dir, scan_id=scan_id, run_summary=run_summary,
            predecessor_index_digest=predecessor_index_digest, now=now,
        )
    finally:
        release_scan_lock(lock_handle)
