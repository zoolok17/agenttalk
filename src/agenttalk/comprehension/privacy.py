"""VCS privacy preflight: refuse before ANY plane output is written unless
the private store is provably VCS-ignored, or an attended operator has
explicitly accepted the risk for one run.

DESIGN-55-comprehension-plane.md, "Privacy and offline enforcement":

    Before creating `.agenttalk/comprehension/`, `scan` performs a VCS
    privacy preflight:

    1. For Git, reject if any path under `.agenttalk/comprehension/` is
       already tracked, then use Git's ignore matcher on a synthetic child
       path to prove the directory is ignored. Other supported VCSs
       require an equivalent native ignore query; guessing from file text
       is not proof.
    2. Record `vcs_privacy` as `ignored`, `acknowledged_unignored`, or
       `no_vcs_acknowledged` [...].
    3. If ignore status is unprovable or false, refuse before any plane
       output is written. The only override is the attended
       `--acknowledge-unignored-private-store` action [...] Scripts and
       wrappers cannot supply the acknowledgement.

v1 supports Git only (the only VCS this repository itself uses, and the
only one this codebase already shells out to elsewhere — see
``checkpoint._git_output``, whose subprocess pattern this mirrors). No
other supported VCS is claimed; its absence routes through the SAME
"no VCS" attended-acknowledgement path as a plain non-VCS directory,
exactly as the design requires for anything that cannot be proven.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - invokes the real git binary for the VCS ignore proof; no shell, no untrusted input
from dataclasses import dataclass, field
from pathlib import Path

from .digests import root_binding_digest
from .errors import VcsPrivacyRefused
from .paths import RELATIVE_COMPREHENSION_DIR

GIT_TIMEOUT_SECONDS = 2.0
_PROBE_RELATIVE_PATH = f"{RELATIVE_COMPREHENSION_DIR}/.privacy-probe/probe.json"

#: Sentinel identity only this module's own factory functions hold —
#: PrivacyPreflightResult.__post_init__ refuses any construction that
#: doesn't present it (reviewer-1 cold-read finding 1 on PR-A,
#: rq-6cc5560b62f6: "make the result non-trivially-fabricable"). This is
#: NOT cryptographic secrecy (nothing stops code that reads this source
#: from importing the sentinel too) - it closes the ACCIDENTAL/careless
#: fabrication path a public frozen dataclass otherwise invites, matching
#: the design's own trust model ("does not claim... cryptographically
#: true") while still raising the bar past "just construct one".
_ISSUED_BY_THIS_MODULE = object()


def _canonical_root_spelling(root: Path) -> str:
    """Canonicalize ``root`` for :func:`digests.root_binding_digest`:
    resolve symlinks/`.`/`..` to an absolute path, POSIX-separated, and
    case-folded on Windows (NTFS is case-insensitive-but-preserving, so
    two differently-cased spellings of the same directory must bind
    identically). Not a claim of cross-platform canonicalization beyond
    what this in-process comparison needs — see root_binding_digest's own
    docstring for the caveat this mirrors.
    """
    resolved = Path(root).resolve()
    spelling = resolved.as_posix()
    if os.name == "nt":
        spelling = spelling.casefold()
    return spelling


@dataclass(frozen=True)
class PrivacyPreflightResult:
    """The recorded ``vcs_privacy`` disposition (design: "ignored",
    "acknowledged_unignored", or "no_vcs_acknowledged"), plus the VCS kind,
    the matched ignore rule when one exists, and the work ID bound to
    either acknowledgement (``None`` for the automatic ``ignored``
    disposition, which needs no attendance).

    ``root_binding`` (reviewer-1 cold-read finding 1 on PR-A,
    rq-6cc5560b62f6, reproduced): a real proof obtained for one project
    root must never unlock a write in an unrelated root. Every consumer of
    this result (``lock.acquire_scan_lock``) MUST verify ``root_binding``
    matches the root it is about to act on before treating the proof as
    valid — the field is present specifically so that check is possible.

    Only :func:`run_privacy_preflight` and
    :func:`acknowledge_unignored_private_store` can construct a valid
    instance - direct construction raises ``TypeError``.
    """

    vcs_privacy: str
    vcs_kind: str
    matched_rule: str | None
    work_id: str | None
    root_binding: str
    _issued_by: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issued_by is not _ISSUED_BY_THIS_MODULE:
            raise TypeError(
                "PrivacyPreflightResult must be obtained from "
                "run_privacy_preflight() or acknowledge_unignored_private_store() "
                "- it cannot be constructed directly"
            )


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _is_git_worktree(root: Path) -> bool:
    result = _run_git(root, "rev-parse", "--is-inside-work-tree")
    return result is not None and result.returncode == 0 and result.stdout.strip() == "true"


def _tracked_paths_under_comprehension_dir(root: Path) -> list[str] | None:
    """Returns the list of already-tracked paths (empty if none), or
    ``None`` if the query itself could not be trusted (git errored) —
    ``None`` is NOT the same as "no tracked paths" and must refuse, not
    proceed."""
    result = _run_git(root, "ls-files", "--", RELATIVE_COMPREHENSION_DIR)
    if result is None or result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def _check_ignore(root: Path) -> tuple[bool, str | None] | None:
    """Proves ignore status via Git's OWN matcher against a synthetic
    child path (design: "use Git's ignore matcher on a synthetic child
    path to prove the directory is ignored" — never inferred by reading
    ``.gitignore`` text). Returns ``(is_ignored, matched_rule)``, or
    ``None`` if the query itself could not be trusted."""
    result = _run_git(root, "check-ignore", "--no-index", "-v", _PROBE_RELATIVE_PATH)
    if result is None:
        return None
    if result.returncode == 0:
        # `-v` output: "<source>:<linenum>:<pattern>\t<pathname>"
        matched_rule = result.stdout.split("\t", 1)[0].strip() or None
        return True, matched_rule
    if result.returncode == 1:
        return False, None
    return None  # 128 or anything else: git itself could not answer


def run_privacy_preflight(root: Path) -> PrivacyPreflightResult:
    """The automatic (non-attended) path only. Raises
    :class:`VcsPrivacyRefused` — carrying ``vcs_kind`` ("git" or "none")
    for the caller to route into :func:`acknowledge_unignored_private_store`
    — whenever ignore status cannot be proven true. Never creates
    ``.agenttalk/comprehension/`` or anything under it; this call happens
    strictly before that (design: "before creating
    `.agenttalk/comprehension/`")."""
    if not _is_git_worktree(root):
        # FIX ROUND 14 (tenth cold read, CR10-12 polish): this message
        # reaches the CLI's plain stderr output (`agenttalk: {exc}`) - the
        # one place in this whole plane that used to name the raw,
        # absolute local root next to a projection family that otherwise
        # never persists one (scan.json's root_binding is a one-way
        # digest specifically to avoid this). The basename is enough to
        # identify which directory failed; the full absolute path is not
        # needed here and every other VcsPrivacyRefused message already
        # avoids it (they name RELATIVE_COMPREHENSION_DIR, never root).
        raise VcsPrivacyRefused(
            f"{root.name!r} is not inside a Git worktree (or git is unavailable) — "
            "ignore status cannot be proven for any supported VCS",
            vcs_kind="none",
        )
    tracked = _tracked_paths_under_comprehension_dir(root)
    if tracked is None:
        raise VcsPrivacyRefused(
            "git ls-files could not be trusted to answer whether "
            f"{RELATIVE_COMPREHENSION_DIR}/ is tracked", vcs_kind="git",
        )
    if tracked:
        raise VcsPrivacyRefused(
            f"{len(tracked)} path(s) under {RELATIVE_COMPREHENSION_DIR}/ are already "
            f"tracked by Git (e.g. {tracked[0]!r})", vcs_kind="git",
        )
    ignore_status = _check_ignore(root)
    if ignore_status is None:
        raise VcsPrivacyRefused(
            "git check-ignore could not be trusted to answer whether "
            f"{RELATIVE_COMPREHENSION_DIR}/ is ignored", vcs_kind="git",
        )
    is_ignored, matched_rule = ignore_status
    if not is_ignored:
        raise VcsPrivacyRefused(
            f"{RELATIVE_COMPREHENSION_DIR}/ is not proven ignored by Git", vcs_kind="git",
        )
    return PrivacyPreflightResult(
        vcs_privacy="ignored", vcs_kind="git", matched_rule=matched_rule, work_id=None,
        root_binding=root_binding_digest(_canonical_root_spelling(root)),
        _issued_by=_ISSUED_BY_THIS_MODULE,
    )


def acknowledge_requires_work_id_message(
    *, acknowledge_unignored: bool, work_id: str | None,
) -> str | None:
    """The ``--acknowledge-unignored-private-store``/``--work-id``
    pairing predicate - MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`,
    R4, taken): shared by ``scan_pipeline.py``'s own ``_obtain_privacy``
    and ``cli.py``'s own ``cmd_comprehension``, TWO independent call
    sites deliberately kept independent rather than unified into one.

    FIX ROUND 21's own CR17-1 BLOCKER analysis (reaffirmed here by
    reviewer-3's own R4 ruling): ``cmd_comprehension``'s ``scan`` action
    calls ``scan_pipeline.run_scan`` TWICE under different
    circumstances - an UNACKNOWLEDGED first attempt (so an ordinary
    dead-owner lock is reclaimed automatically with no override needed,
    and a live-owner lock refusal is never masked by a flag the caller
    only meant for the PRIVACY question), then an ACKNOWLEDGED retry
    ONLY after a real ``VcsPrivacyRefused`` proves the override is
    actually needed. Collapsing the two call sites into one (always
    passing ``acknowledge_unignored`` through from the very start) would
    REOPEN that exact BLOCKER - the override would clear a live,
    legitimately-held lock before ``ScanLockContended``'s own refusal
    ever got a chance to fire. The two call sites - and the two
    genuinely distinct error-reporting surfaces around them (a typed
    :class:`~.errors.ScanRefused` here in ``scan_pipeline.py``, a plain
    stderr write + exit 2 in ``cli.py``) - are LOAD-BEARING duplication,
    not oversight; only the PREDICATE itself (the pairing rule, and its
    exact wording) is shared here, so the two sites can never
    independently drift on what "requires --work-id" actually means.
    Do not "simplify" this by unifying the two call sites.

    Returns the refusal message when the pairing is invalid
    (acknowledgement requested with no ``work_id``), ``None`` when the
    pairing is fine (or acknowledgement was never requested at all).

    FIX ROUND 30 (twenty-sixth cold read, F5 polish, wrong-data): a
    whitespace-only ``work_id`` (``"   "``) used to pass this guard - a
    bare truthiness check treats any non-empty string as fine, and a
    string of only whitespace IS non-empty. ``--run``'s own identical
    shape (``_resolve_run_id``, round 29's own F7) already refuses
    whitespace-only explicitly; mirrored here so the two guards cannot
    independently drift on what counts as "no real value was given.\""""
    if not acknowledge_unignored or (work_id is not None and work_id.strip()):
        return None
    return (
        "--acknowledge-unignored-private-store requires --work-id "
        "(design: \"applies to one run bound to an existing work item\")"
    )


def acknowledge_unignored_private_store(
    root: Path, *, vcs_kind: str, work_id: str, matched_rule: str | None = None,
) -> PrivacyPreflightResult:
    """Attended-only override for a :class:`VcsPrivacyRefused` refusal.

    Like :func:`lock.recover_stale_lock`, this function performs no
    attendance check itself — proving an interactive terminal and explicit
    operator confirmation (and displaying the resolved local target plus
    the ``git add -A`` risk) is PR-B's CLI job. By the time this is
    called, the operator has already confirmed the risk for exactly one
    run bound to ``work_id``; this is simply where that confirmation
    becomes the recorded disposition. ``work_id`` is REQUIRED and must be
    non-empty (design: "applies to one run bound to an existing work
    item... Scripts and wrappers cannot supply the acknowledgement" — an
    empty/missing work_id is exactly what an unattended caller would have).

    ``root`` binds this acknowledgement to the SAME project root the
    operator was shown and confirmed the risk for — an attended override
    for root A must never silently cover a write in unrelated root B
    (reviewer-1 cold-read finding 1 on PR-A, rq-6cc5560b62f6).

    FIX ROUND 29 (twenty-fifth cold read, F5 MAJOR, completeness):
    ``work_id`` is REQUIRED to be non-empty (above) but is NEVER verified
    against a real, existing work item this slice — no work-item
    subcommand or plane exists yet to check it against, so a fabricated,
    unrelated, or already-closed work_id is accepted with no validation
    beyond non-emptiness. The recorded ``PrivacyPreflightResult.work_id``
    (and ``scan.json``'s own ``privacy.work_id`` field, downstream) is
    exactly what the caller passed, never a confirmed binding — see
    ``readiness_artifact.PROVENANCE_CAVEAT``'s own declaration of this gap.
    """
    if not isinstance(work_id, str) or not work_id.strip():
        raise VcsPrivacyRefused(
            "acknowledge_unignored_private_store requires a non-empty work_id bound "
            "to an existing work item", vcs_kind=vcs_kind,
        )
    if vcs_kind not in ("git", "none"):
        raise VcsPrivacyRefused(f"unsupported vcs_kind {vcs_kind!r}", vcs_kind=vcs_kind)
    disposition = "no_vcs_acknowledged" if vcs_kind == "none" else "acknowledged_unignored"
    return PrivacyPreflightResult(
        vcs_privacy=disposition, vcs_kind=vcs_kind, matched_rule=matched_rule,
        work_id=work_id,
        root_binding=root_binding_digest(_canonical_root_spelling(root)),
        _issued_by=_ISSUED_BY_THIS_MODULE,
    )
