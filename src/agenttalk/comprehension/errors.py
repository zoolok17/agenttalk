"""Typed error hierarchy for the comprehension plane (#55 slice-1).

One base class per PR-A subsystem, mirroring the codebase's existing
typed-refusal convention (e.g. ``lifecycle_lock.LifecycleLockError``,
``domains.DomainError``): callers branch on type, not on message text, and
every message stays human-actionable.
"""

from __future__ import annotations


class ComprehensionError(RuntimeError):
    """Base class for every typed refusal raised by this package."""

    reason_code = "comprehension_error"


class EnvelopeError(ComprehensionError):
    """A JSON document failed strict envelope, schema, or path-safety checks."""

    reason_code = "comprehension_envelope_invalid"


class ScanLockError(ComprehensionError):
    """Base class for scan.lock refusals."""

    reason_code = "comprehension_lock_error"


class ScanLockContended(ScanLockError):
    """A live, provably-same-host, provably-same-process owner holds the
    lock. Refuse immediately - the design's single-writer contract has no
    wait/poll; a second scanner simply refuses (retry is the caller's
    business, not this module's)."""

    reason_code = "comprehension_lock_contended"

    def __init__(self, record: dict) -> None:
        self.holder_pid = record.get("pid")
        self.holder_acquired_at = record.get("acquired_at")
        super().__init__(
            f"{self.reason_code}: scan.lock is held by a live process (pid "
            f"{self.holder_pid}) since {self.holder_acquired_at}; wait for that scan "
            "to finish, or its process to end, then retry"
        )


class VcsPrivacyRefused(ComprehensionError):
    """The private-store VCS-ignore disposition could not be proven before
    any plane output would be written (design, "Privacy and offline
    enforcement"). Only the attended ``--acknowledge-unignored-private-
    store`` action may proceed from here — this refusal fires BEFORE any
    lock or staging file exists, so refusing here leaves nothing to clean
    up."""

    reason_code = "comprehension_vcs_privacy_refused"

    def __init__(self, detail: str, *, vcs_kind: str) -> None:
        self.detail = detail
        self.vcs_kind = vcs_kind
        super().__init__(
            f"{self.reason_code}: {detail}; only an attended operator running "
            "--acknowledge-unignored-private-store may proceed"
        )


class PrivacyProofRootMismatch(ComprehensionError):
    """A ``PrivacyPreflightResult`` proven for one project root was
    presented at a DIFFERENT root's lock acquisition (reviewer-1 cold-read
    finding 1 on PR-A, rq-6cc5560b62f6, reproduced: a real proof from
    protected root A unlocked writes in unrelated root B). A proof is only
    valid at the exact root it was issued for."""

    reason_code = "comprehension_privacy_proof_root_mismatch"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.reason_code}: {detail}")


class ScanLockUnrecoverable(ScanLockError):
    """The recorded owner cannot be PROVEN dead (unverifiable identity, a
    process-start mismatch/PID reuse, a host mismatch, an unsupported
    platform, or a malformed record). Only the attended
    ``--recover-stale-lock`` action may clear it (design: "An unverifiable
    or remote-looking owner requires the explicit attended
    `--recover-stale-lock` action")."""

    reason_code = "comprehension_lock_unrecoverable"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(
            f"{self.reason_code}: {detail}; this cannot be reclaimed automatically - "
            "an attended operator must run --recover-stale-lock after confirming the "
            "prior scan is really gone"
        )
