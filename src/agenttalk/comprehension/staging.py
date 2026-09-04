"""``.staging/<scan-id>-<nonce>/`` + ``owner.json``: reclaim and pruning.

DESIGN-55-comprehension-plane.md, "Local storage model":

    A scan writes to a uniquely named directory under `.staging/`;
    `owner.json` there repeats the lock token. [...] At lock acquisition,
    the scanner reclaims only unpublished staging directories whose
    contained `owner.json` has the expected schema, whose resolved path
    stays under `.staging/`, and whose owner is definitely dead. Anything
    ambiguous is reported and retained. `comprehension prune --staging`
    performs the same check as an attended v1 maintenance action. This
    cleanup is not deletion of published project memory.

The owner-death classification mirrors ``lock.py``'s exactly (same
provably-dead-or-leave-it-alone contract) — a staging directory is never
guessed away, only ever removed when its owner.json names a definitely
dead local process on this same host.

Note 10 (third cold read, PR-B fix round 5): a scan that fails at or
after staging creation, while its own process is still alive (has not
yet crashed or exited), leaves a staging directory that prune correctly
RETAINS — this is DESIGNED, not a leak: the directory is bounded (one
per failed attempt) and self-clearing the moment the creating process
actually ends (the next reclaim, automatic or via ``prune --staging``,
sees a dead owner and removes it then). The design's own dead-or-leave-
it-alone contract forbids removing a live owner's directory even when
that owner's scan has already failed internally; there is no unbounded
accumulation risk this doesn't already resolve on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..lifecycle_lock import process_observation
from .envelope import (
    EnvelopeError,
    path_is_reparse_point_or_symlink,
    read_json_document,
    resolve_under_root,
    validate_rfc3339_utc,
    validate_scan_id,
)
from .errors import ComprehensionError, bounded_os_error_detail
from .lock import ScanLockHandle, host_identity
from .paths import staging_dir as _staging_dir

OWNER_SCHEMA_VERSION = 1
_OWNER_FILENAME = "owner.json"


class StagingReclaimFailed(ComprehensionError):
    """MICRO-ROUND 50 (Cluster 5, the staging brick): a definitely-dead
    owner's own staging directory was selected for reclaim (per this
    module's own dead-or-leave-it-alone contract) but could not actually
    be REMOVED - a file inside it still held open by another process, a
    permissions/ACL issue, or any other OS-level deletion failure
    (measured: a raw ``PermissionError [WinError 5]`` on Windows,
    completely unhandled). This is not an ordinary transient I/O error:
    since this SAME directory is reclaimed again, identically, on EVERY
    future lock acquisition (this module's own automatic call site), an
    undeletable entry here is a PERMANENT BRICK - every future scan
    attempt hits the identical failure, forever, until an operator
    investigates and clears it by hand. A named, typed refusal with a
    concrete remedy, never a raw, unhandled ``OSError`` traceback.

    NAMED RESIDUAL, not silently dropped: this round closes the two
    IMMEDIATE harms (a raw, unhandled traceback naming an absolute local
    path; the leaked ``scan.lock`` compounding the brick on every future
    attempt - see ``scan_pipeline.run_scan``'s own round-50 fix) but does
    NOT add an operator escape hatch (e.g. a ``--force-staging`` flag to
    skip or force-remove a specific stuck entry) - genuinely OUT of a
    "judge fix-vs-issue, cheap fix only" scope this round: it needs new
    CLI surface (argparse plumbing, a new attended-action shape) and a
    real design decision about what "force" is even allowed to do to
    content whose owner cannot be proven dead by deletion (only by
    process-liveness, already proven here). Left as a real, undeletable-
    on-this-host directory an operator must clear by hand (the SAME
    manual-intervention shape a permanently unrecoverable scan.lock
    already has via ``--recover-stale-lock``) until a future round
    measures this as a common-enough gap to justify the new surface."""

    reason_code = "comprehension_staging_reclaim_failed"


#: Sentinel identity only this module's own factory function holds — mirrors
#: ``privacy._ISSUED_BY_THIS_MODULE`` exactly (same non-fabricability
#: rationale, same non-cryptographic caveat). Added per the lead's PR-A
#: round-4 dispatch, on top of round-4's resolve-and-confine fix in
#: ``publish.rename_staging_to_run``: reviewer-1 reproduced a publicly
#: constructed ``StagingHandle`` naming an external directory being
#: accepted by an owner-token string comparison alone. Confinement closes
#: that specific path; this closes the broader class by making
#: construction itself unreachable outside :func:`create_staging_dir`.
_ISSUED_BY_THIS_MODULE = object()


def _utc_now_iso(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class StagingHandle:
    """Returned only by :func:`create_staging_dir` — direct construction
    raises ``TypeError`` (same module-private-construction pattern as
    ``privacy.PrivacyPreflightResult``, applied here per the lead's PR-A
    round-4 dispatch: a publicly constructible handle let a caller name an
    arbitrary directory and have it accepted by publish-time checks that
    trusted the handle's own claimed fields)."""

    path: Path
    scan_id: str
    owner_token: str
    _issued_by: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issued_by is not _ISSUED_BY_THIS_MODULE:
            raise TypeError(
                "StagingHandle must be obtained from create_staging_dir() - it cannot "
                "be constructed directly"
            )


@dataclass(frozen=True)
class StagingReclaimReport:
    """Result of one reclaim/prune pass. ``reclaimed`` directories were
    deleted (their owner was provably dead); ``retained`` directories were
    left untouched, each with the reason it was NOT safe to remove."""

    reclaimed: list[str] = field(default_factory=list)
    retained: list[tuple[str, str]] = field(default_factory=list)


def create_staging_dir(
    *,
    scan_id: str,
    lock_handle: ScanLockHandle,
    now: datetime | None = None,
) -> StagingHandle:
    """Create ``.staging/<scan_id>-<nonce>/`` and its ``owner.json``,
    repeating ``lock_handle.owner_token`` (per the design) so a later
    reclaim pass can prove which lock holder — dead or alive — created it.

    Takes the FULL ``lock_handle`` rather than a bare ``owner_token: str``
    (closing the same door reviewer-3's B-1 finding on PR-A, rq-5bd5427ad64d,
    opened for ``acquire_scan_lock``) — since :func:`lock.acquire_scan_lock`
    cannot be called without a proven privacy disposition, requiring one of
    its handles here means staging creation is unreachable without that
    same proof, with no separate parameter for a caller to forget. The
    comprehension directory is DERIVED from ``lock_handle.path`` rather
    than accepted as an independent parameter for the same reason — there
    is then no second value that could ever disagree with the lock's own,
    already-root-bound, root.

    ``scan_id`` is validated against the closed scan-ID grammar BEFORE any
    path is built from it, and the resulting directory is resolved and
    confined to stay under ``.staging/`` (reviewer-1 cold-read finding 2 on
    PR-A, rq-6cc5560b62f6, reproduced: an unvalidated
    ``scan_id="../../../../escaped"`` previously wrote ``owner.json``
    outside the protected project root).
    """
    validated_scan_id = validate_scan_id(scan_id)
    comprehension_dir = lock_handle.path.parent
    nonce = uuid.uuid4().hex[:12]
    staging_root = _staging_dir(comprehension_dir)
    path = resolve_under_root(
        f"{validated_scan_id}-{nonce}", root=staging_root, label="staging directory")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    owner_doc = {
        "schema_version": OWNER_SCHEMA_VERSION,
        "scan_id": validated_scan_id,
        "owner_token": lock_handle.owner_token,
        "pid": os.getpid(),
        "host_identity": host_identity(),
        "created_at": _utc_now_iso(now),
    }
    (path / _OWNER_FILENAME).write_text(
        json.dumps(owner_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return StagingHandle(
        path=path, scan_id=validated_scan_id, owner_token=lock_handle.owner_token,
        _issued_by=_ISSUED_BY_THIS_MODULE)


def _validate_owner_doc(doc: Any) -> dict:
    if not isinstance(doc, dict):
        raise EnvelopeError("owner.json must be a JSON object")
    if doc.get("schema_version") != OWNER_SCHEMA_VERSION:
        raise EnvelopeError(
            f"owner.json schema_version must be {OWNER_SCHEMA_VERSION}, got "
            f"{doc.get('schema_version')!r}")
    if not isinstance(doc.get("scan_id"), str) or not doc["scan_id"]:
        raise EnvelopeError("owner.json scan_id must be a non-empty string")
    if not isinstance(doc.get("owner_token"), str) or not doc["owner_token"]:
        raise EnvelopeError("owner.json owner_token must be a non-empty string")
    pid = doc.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise EnvelopeError("owner.json pid must be a positive integer")
    if not isinstance(doc.get("host_identity"), str) or not doc["host_identity"]:
        raise EnvelopeError("owner.json host_identity must be a non-empty string")
    validate_rfc3339_utc(doc.get("created_at"), label="owner.json created_at")
    return doc


def _classify_one_staging_dir(
    entry: Path, *, staging_root: Path,
) -> tuple[str, str | None]:
    """Returns ``(disposition, retain_reason)``: disposition is
    ``"reclaim"`` or ``"retain"``; ``retain_reason`` is ``None`` only for
    ``"reclaim"``.

    MICRO-ROUND 50 (Cluster 0, B1-adjacent): the OLD check resolved
    ``staging_root`` FIRST, the same vacuous-by-construction pattern
    fixed in ``envelope.resolve_under_root`` - a reparse point AT
    ``.staging/`` itself would be baked into the comparison root before
    ``relative_to`` ever ran, so a dead-owner match found through it
    could reach the ``shutil.rmtree`` below on content entirely outside
    the pinned store. RETAINS (never raises - this runs at every lock
    acquisition, before this module's own reclaim loop, with no
    surrounding rollback for a raised exception here) whenever
    ``staging_root`` or ``entry`` itself is a symlink/reparse point,
    checked BEFORE any ``.resolve()`` call, mirroring every other
    fail-closed check in this fix."""
    if path_is_reparse_point_or_symlink(staging_root):
        return "retain", "staging root is a symlink or a directory reparse point/junction"
    if path_is_reparse_point_or_symlink(entry):
        return "retain", "entry is itself a symlink or a directory reparse point/junction"
    resolved = entry.resolve()
    try:
        resolved.relative_to(staging_root.resolve())
    except ValueError:
        return "retain", "resolved path escapes .staging/"
    if not entry.is_dir():
        return "retain", "not a directory"
    owner_path = entry / _OWNER_FILENAME
    try:
        doc = read_json_document(owner_path)
        owner = _validate_owner_doc(doc)
    except EnvelopeError as exc:
        # NAMED RESIDUAL, not silently dropped (MICRO-ROUND 50, Cluster 5,
        # judged - fix vs issue): a PRIOR failed shutil.rmtree attempt on
        # THIS same directory (a StagingReclaimFailed, above) may already
        # have deleted owner.json itself before hitting whatever file it
        # could not remove - rmtree's own deletion order is
        # implementation-defined, so owner.json (a single file directly
        # in the directory root) can vanish before the failure surfaces.
        # A directory in exactly this state reads identically to a
        # GENUINELY malformed/never-written owner.json - this branch
        # cannot (and does not try to) tell "debris from my own prior
        # failed reclaim, whose owner WAS already proven dead" apart from
        # "an unrelated, ambiguous anomaly," and RETAINS both the same
        # way, forever - a directory in the first state can never
        # automatically re-prove its own dead owner without owner.json's
        # own pid to check, so it silently accumulates rather than ever
        # being retried. Closing this needs a real design decision
        # (rename-then-delete so a partial rmtree failure can never leave
        # owner.json gone while other content remains, or a secondary
        # marker recorded before the delete begins) - genuinely out of a
        # "judge fix-vs-issue, cheap fix only" scope this round. Left for
        # a future round if measured as a common-enough gap; an operator
        # can always clear a directory in this state manually today, the
        # same "retained, not silently deleted" contract every other
        # ambiguous case here already has.
        return "retain", f"owner.json is missing or malformed: {exc}"
    if owner["host_identity"] != host_identity():
        return "retain", f"owner.json names a different host ({owner['host_identity']!r})"
    status, _observed = process_observation(owner["pid"])
    if status == "dead":
        return "reclaim", None
    if status == "alive":
        return "retain", f"owner pid {owner['pid']} is alive"
    return "retain", f"owner pid {owner['pid']}'s liveness could not be observed exactly"


def reclaim_abandoned_staging(comprehension_dir: Path) -> StagingReclaimReport:
    """Reclaim (delete) every unpublished ``.staging/`` directory whose
    owner is provably dead on this host; report and RETAIN everything else
    untouched. Called automatically at lock acquisition, and directly by
    the explicit ``comprehension prune --staging`` maintenance command
    (design: "performs the same check") — both share this one function so
    the two call sites can never drift apart.

    MICRO-ROUND 50 (Cluster 5, the staging brick BLOCKER): ``shutil.
    rmtree(..., ignore_errors=False)`` used to let a raw ``OSError``
    (measured: ``PermissionError [WinError 5]``, a file inside still
    held open) propagate completely unhandled - see
    :class:`StagingReclaimFailed`'s own docstring for why this is a
    PERMANENT brick, not a transient failure, if left as a raw
    traceback. Caught here and converted to that named, typed refusal
    with a concrete remedy.

    MICRO-ROUND 50 (Cluster 5, concurrent prune): a ``FileNotFoundError``
    specifically (never folded into the same-``OSError`` refusal above)
    is a genuinely BENIGN race, not a failure at all - this SAME
    function runs at every lock acquisition AND from the explicit
    ``prune --staging`` command, so two overlapping calls (an automatic
    reclaim racing an operator's manual prune, or two manual prunes)
    can both select the identical dead-owner directory for reclaim; by
    the time this call's own ``shutil.rmtree`` runs, the other caller
    already removed it. The outcome this call was trying to reach ("this
    directory no longer exists") is already true - counted as reclaimed,
    never raised as a failure the caller has to investigate.
    """
    staging_root = _staging_dir(comprehension_dir)
    report = StagingReclaimReport()
    if not staging_root.is_dir():
        return report
    for entry in sorted(staging_root.iterdir()):
        disposition, reason = _classify_one_staging_dir(entry, staging_root=staging_root)
        if disposition == "reclaim":
            try:
                shutil.rmtree(entry, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise StagingReclaimFailed(
                    bounded_os_error_detail(
                        f"{entry.name!r}'s own dead-owner staging directory could not be "
                        "deleted", exc,
                    ) + " - this directory is reclaimed again, identically, on EVERY "
                    "future scan attempt (this is a permanent brick, not a transient "
                    "failure); an operator must investigate what still holds it open "
                    "(a file handle, a permissions/ACL issue, ...) and remove it "
                    "manually before retrying"
                ) from exc
            report.reclaimed.append(entry.name)
        else:
            report.retained.append((entry.name, reason or "unknown"))
    return report


def prune_staging(comprehension_dir: Path) -> StagingReclaimReport:
    """``agenttalk comprehension prune --staging``'s internal entry point.
    Identical safety contract to the automatic at-lock-acquisition reclaim
    — never removes an ambiguous or live-owned directory, never touches
    ``runs/`` or ``packs/`` (design: "This cleanup is not deletion of
    published project memory")."""
    return reclaim_abandoned_staging(comprehension_dir)
