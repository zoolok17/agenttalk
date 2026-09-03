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


#: M-3 (third cold read, fix round 5): a problem's persisted ``detail``
#: must be reason-coded and bounded, templated free text - never raw
#: parser/OS-exception output copied wholesale (design: "reason codes;
#: bounded templated free text; the projection exposes no raw source,
#: absolute paths, or parser logs"). PROVISIONAL, like this package's
#: other bound constants.
#:
#: MICRO-ROUND 40b (reviewer-3's own delta on round 40, one-word note):
#: this is the TRUNCATION point, not the published field's own maximum
#: length - ``bounded_detail`` (below) appends the ``"...(truncated)"``
#: marker AFTER slicing to this many characters (round 37's own F6
#: fix, so a truncation is never silently indistinguishable from a
#: detail that genuinely ends there), so a truncated ``detail`` can be
#: up to ``MAX_PROBLEM_DETAIL_LENGTH + len("...(truncated)")`` = 214
#: characters long. A consumer sizing a buffer/column on "bounded to
#: 200" undercounts by the marker's own length.
MAX_PROBLEM_DETAIL_LENGTH = 200


def bounded_os_error_detail(action: str, exc: OSError) -> str:
    """A templated, length-bounded problem detail for an ``OSError`` -
    never ``str(exc)`` verbatim. ``str(exc)`` on an ``OSError`` embeds the
    exception's OWN absolute filename (``exc.filename``) by construction
    (e.g. ``"[Errno 13] Permission denied: 'C:\\\\Users\\\\...\\\\blocked"``)
    - exactly the machine-local absolute-path leak M-3 found flowing into
    ``problems.json``. This uses only ``action`` (a fixed, named template
    the caller already knows is path-free) and the OS's own short
    ``strerror``, neither of which can carry a filesystem path - the
    caller supplies the file's already-project-relative path separately,
    in the problem record's own ``path`` field."""
    reason = exc.strerror or exc.__class__.__name__
    return f"{action}: {reason}"[:MAX_PROBLEM_DETAIL_LENGTH]


def bounded_detail(text: str) -> str:
    """Length-bounds an already path-free detail string (an adapter's own
    parse-failure message, or an :class:`EnvelopeError`'s message) before
    it is persisted - defense in depth against an unbounded exception
    message of any origin, current or future, ballooning a problem
    record.

    FIX ROUND 37 (thirty-first cold read, F6 LOW, wrong-data): this used
    to slice at exactly ``MAX_PROBLEM_DETAIL_LENGTH`` with NO marker -
    silently truncating mid-word, indistinguishable from a detail that
    genuinely ends there. The same visible-truncation marker
    ``adapters.java._bounded_route_target`` already establishes for the
    identical shape - a truncated detail now says so."""
    if len(text) <= MAX_PROBLEM_DETAIL_LENGTH:
        return text
    return text[:MAX_PROBLEM_DETAIL_LENGTH] + "...(truncated)"


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


class InvalidReadinessStateFilter(ComprehensionError):
    """FIX ROUND 12 (eighth cold read, F8): ``--readiness``/
    ``readiness_state`` used to silently match nothing at all for an
    unrecognized value - a typo or a stale value from a since-renamed
    state returned an empty, exit-0 result indistinguishable from "every
    unit was filtered out for real reasons". The closed vocabulary
    already exists (``readiness_artifact.ASSESSMENT_STATES``); an
    argument outside it is a caller mistake, refused the same way every
    other malformed input this package rejects is."""

    reason_code = "comprehension_invalid_readiness_state_filter"

    def __init__(self, value: str, allowed: tuple[str, ...]) -> None:
        self.detail = f"{value!r} is not a recognized readiness state (must be one of {allowed})"
        super().__init__(f"{self.reason_code}: {self.detail}")


class InvalidComprehensionDir(ComprehensionError):
    """A ``comprehension_dir`` argument does not have the exact
    ``<root>/.agenttalk/comprehension`` shape (reviewer-1 cold-read finding
    1 on the PR-A fix round, rq-6cc5560b62f6, round 2: deriving a project
    root as merely "two parents up" let ``acquire_scan_lock(root /
    "unignored" / "store", ...)`` pass a root-binding check proven for
    ``root`` while writing ``scan.lock`` OUTSIDE ``.agenttalk`` entirely —
    reproduced with ``under_agenttalk=False``). Every plane output must
    stay under ``.agenttalk`` (design: "every plane output is written
    under `.agenttalk/`") — this is checked BEFORE the root-binding
    comparison even runs, so a malformed path can never reach it."""

    reason_code = "comprehension_dir_invalid_shape"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.reason_code}: {detail}")


class StagingSourceEscapesRoot(ComprehensionError):
    """``staging_handle.path`` does not resolve under the lock's own
    ``.staging/`` directory, or its on-disk ``owner.json`` does not match
    the presented handle (reviewer-1 cold-read finding 2 on the PR-A fix
    round, rq-6cc5560b62f6, round 2: ``StagingHandle`` is a public,
    trivially-constructible dataclass, so a handle naming an external
    directory — with a copied, real ``owner_token`` — was accepted by the
    owner-token string comparison alone and its content was published
    under ``runs/``, reproduced with ``external_content_published=True``).
    Trust is re-derived from what is ACTUALLY on disk at the confined
    path, never from the handle's claimed fields alone."""

    reason_code = "comprehension_staging_source_escapes_root"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.reason_code}: {detail}")


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

    def __init__(self, detail: str, *, remedy: str | None = None) -> None:
        # FIX ROUND 26 (twenty-second cold read, F8, wrong-data): every
        # call site used to share ONE generic remedy sentence ("run
        # --recover-stale-lock after confirming the prior scan is really
        # gone") - accurate for a same-host dead/unverifiable/malformed-
        # record owner (the flag genuinely resolves those), but a caller
        # who supplied the flag for a DIFFERENT-HOST owner and hit this
        # again (that host keeps re-acquiring) was told the same generic
        # advice, as if simply running the flag they had already run
        # would help - it cannot, since this flag has no way to observe
        # a foreign host's process state at all. `remedy` lets a call
        # site override the generic sentence with the ACTUAL remedy for
        # its own case; unset, every existing call site is unchanged.
        self.detail = detail
        remedy_text = remedy if remedy is not None else (
            "an attended operator must run --recover-stale-lock after confirming the "
            "prior scan is really gone"
        )
        super().__init__(
            f"{self.reason_code}: {detail}; this cannot be reclaimed automatically - "
            f"{remedy_text}"
        )
