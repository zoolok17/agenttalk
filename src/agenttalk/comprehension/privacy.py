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

FIX ROUND 33 (twenty-ninth cold read, R1 REJECT on round 32's own F1,
reviewer-3's delta): the design's own "synthetic child path" wording
(quoted above) describes a PROOF BY PROXY — checking ONE stand-in path
and generalizing its answer to the whole store — and that proxy proof
was measured broken: any Git rule that discriminates by real filename or
real scan-id shape (rather than by a probe's own fixed literal spelling)
defeats it while leaving the probe itself "ignored". This module's own
:func:`run_privacy_preflight`/:func:`_check_ignore` remain a cheap, EARLY
fail-fast against an obviously-unignored store — refusing before any
scan work even starts is still worth doing — but they are NOT this
plane's actual privacy guarantee any more.

FIX ROUND 34 (reviewer-3's re-delta on round 33's own R1 fix — THE
HOLE): round 33's own ground-truth check (enumerate the real files
``rename_staging_to_run`` just wrote under ``runs/<scan_id>/``, verify
each directly) reached everything INSIDE that one directory, but
publication's OTHER step — ``publish.publish_index_cas``, which
CAS-writes ``index.json`` at the store root — was never in its
enumeration. THE GUARANTEE now belongs to :func:`verify_store_ignored`:
called by ``publish.py`` once, after BOTH publish steps fully complete,
asking git directly what is stageable across the WHOLE store
(``git ls-files --others --exclude-standard``) rather than enumerating
any directory by hand — this covers ``index.json``, every
``runs/<id>/`` artifact, and any file a future slice adds under this
same store, with no enumeration left to fall out of date.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - invokes the real git binary for the VCS ignore proof; no shell, no untrusted input
from dataclasses import dataclass, field
from pathlib import Path

from .digests import root_binding_digest
from .errors import VcsPrivacyRefused
from .paths import INDEX_FILENAME, RELATIVE_COMPREHENSION_DIR, RUNS_DIRNAME, STAGING_DIRNAME

GIT_TIMEOUT_SECONDS = 2.0

#: FIX ROUND 32 (twenty-eighth cold read, F1 BLOCKER, privacy boundary): a
#: single probe at one synthetic, writer-never-uses-it depth (the old
#: ``.privacy-probe/probe.json`` sentinel this replaces) generalizes its ONE
#: answer to the WHOLE comprehension store. A git re-inclusion idiom
#: (``.agenttalk/**`` then ``!.agenttalk/comprehension/runs/**``) makes that
#: one answer "ignored" while the REAL published artifacts under ``runs/**``
#: stay un-ignored and stageable by ``git add -A``; a rule scoped only to the
#: probe's own private subdirectory (``/.agenttalk/**/.privacy-probe/``)
#: defeats it the same way. Probing instead at every structural depth the
#: writer actually creates (``index.json`` directly under the comprehension
#: dir, a ``runs/<id>/`` entry, a ``.staging/<id>/`` entry — see paths.py's
#: own layout) and requiring ALL of them to independently prove ignored
#: closes both shapes: a broad ``.agenttalk/`` or ``.agenttalk/comprehension/``
#: rule still ignores every probe exactly as before; only a rule that
#: unignores one of the real shapes now fails to generalize past this
#: module. The probe names themselves are fixed, arbitrary literals — they
#: never correspond to a real scan_id/nonce a writer could produce, so a
#: probe can never collide with real content.
#:
#: FIX ROUND 33 (twenty-ninth cold read, R1 REJECT on round 32's own F1,
#: reviewer-3's delta): even THREE sentinels are still sentinels — the
#: proof this constant enables is still "these specific LITERAL paths are
#: ignored, therefore everything published is," which is false for any
#: rule that discriminates by real filename or real scan-id SHAPE rather
#: than by these probes' own fixed literal spelling. Confirmed defeated
#: three ways. This preflight — and the module-level probe below — is
#: DEMOTED to a cheap, EARLY, non-authoritative fail-fast: a real store
#: this obviously unignored is refused before any scan work even starts,
#: which is still worth doing, but it is NO LONGER the guarantee.
#:
#: FIX ROUND 34 (reviewer-3's re-delta — THE HOLE): note that the first
#: probe above already names the REAL ``index.json`` path (not a
#: synthetic id) — so a rule targeting it specifically WAS caught, but
#: only at THIS one-time, pre-scan moment. A ``.gitignore`` flip that
#: re-includes ``index.json`` AFTER this preflight ran (and specifically
#: before ``publish.publish_index_cas`` writes it) is invisible to a
#: check that only ever runs once, before the scan even starts. THE
#: guarantee is now ``verify_store_ignored``, called once by
#: ``publish.py`` after BOTH publish steps fully complete, asking git
#: directly what is stageable across the whole store rather than probing
#: (or enumerating) anything by hand.
_PRIVACY_PROBE_RELATIVE_PATHS = (
    f"{RELATIVE_COMPREHENSION_DIR}/{INDEX_FILENAME}",
    f"{RELATIVE_COMPREHENSION_DIR}/{RUNS_DIRNAME}/privacy-probe-run/scan.json",
    f"{RELATIVE_COMPREHENSION_DIR}/{STAGING_DIRNAME}/privacy-probe-run-deadbeef/owner.json",
)

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


def _check_ignore_one(root: Path, relative_path: str) -> tuple[bool, str | None] | None:
    """Proves ignore status of ONE concrete path via Git's OWN matcher
    (design: "use Git's ignore matcher on a synthetic child path to prove
    the directory is ignored" — never inferred by reading ``.gitignore``
    text). Returns ``(is_ignored, matched_rule)``, or ``None`` if the query
    itself could not be trusted. Used only by :func:`_check_ignore` (the
    demoted, early-refusal multi-probe preflight) — the round 34 ground-
    truth check (:func:`verify_store_ignored`) asks a different git
    question entirely (``ls-files --others``, not ``check-ignore``), so
    it does not share this helper.

    FIX ROUND 32 (F1 BLOCKER, discovered while building the multi-probe
    regression fixtures — a proper `!pattern` re-inclusion chain (unlike the
    single-level negation this module used to only ever probe past): ``git
    check-ignore -v``'s own EXIT CODE is 0 whenever ANY pattern matched the
    path — including a NEGATION pattern that actually un-ignores it. The
    exit code alone is therefore not the real verdict; the matched
    pattern's own text is — a pattern starting with ``!`` means "not
    ignored after all," regardless of the 0 exit code. Both this probe
    check and the pre-round-32 single-probe version it replaces were
    exposed to this, but the old probe's own synthetic subdirectory was
    never a realistic re-inclusion target, so it never actually surfaced."""
    result = _run_git(root, "check-ignore", "--no-index", "-v", relative_path)
    if result is None:
        return None
    if result.returncode == 0:
        # `-v` output: "<source>:<linenum>:<pattern>\t<pathname>"
        metadata = result.stdout.split("\t", 1)[0].strip()
        pattern = metadata.split(":", 2)[-1] if metadata else ""
        if pattern.startswith("!"):
            return False, None
        return True, metadata or None
    if result.returncode == 1:
        return False, None
    return None  # 128 or anything else: git itself could not answer


def _check_ignore(root: Path) -> tuple[bool, str | None] | None:
    """FIX ROUND 33 (twenty-ninth cold read, demoted): a cheap, EARLY,
    NON-AUTHORITATIVE fail-fast heuristic only — checks EVERY probe in
    :data:`_PRIVACY_PROBE_RELATIVE_PATHS` (see that constant's own
    docstring for why even three fixed, synthetic probes cannot prove the
    whole store ignored) and requires all of them to independently prove
    ignored. A store this obviously unignored is refused before any scan
    work even starts — still worth doing — but a ``True`` result here is
    NOT the guarantee; see :func:`verify_store_ignored` for the actual
    ground-truth check. Returns ``(True, matched_rule)`` (the first
    probe's own rule) only when every probe is ignored; ``(False, None)``
    as soon as any probe proves NOT ignored; ``None`` (untrustworthy) as
    soon as any probe's query itself could not be trusted."""
    first_matched_rule: str | None = None
    for probe_path in _PRIVACY_PROBE_RELATIVE_PATHS:
        probe_status = _check_ignore_one(root, probe_path)
        if probe_status is None:
            return None
        is_ignored, matched_rule = probe_status
        if not is_ignored:
            return False, None
        if first_matched_rule is None:
            first_matched_rule = matched_rule
    return True, first_matched_rule


def verify_store_ignored(root: Path, relative_store_dir: str) -> None:
    """FIX ROUND 34 (reviewer-3's re-delta on round 33's own R1 fix - THE
    HOLE): round 33 stopped proving by proxy for the ``runs/<scan_id>/``
    directory (enumerating its own real files and checking each one
    directly) — but publication is TWO steps: step 1 renames staging into
    ``runs/<scan_id>/``; step 2 CAS-writes ``index.json`` at the STORE
    ROOT, never inside the directory step 1's own enumeration ever
    walked. A mid-run ``.gitignore`` flip re-including ONLY ``index.json``
    left it unprotected by anything but the demoted, early preflight —
    the identical flip re-including ``runs/**`` was correctly refused,
    proving the mechanism works everywhere it REACHES; ``index.json`` sat
    outside its reach. Round 32's probes forgot filenames and id shapes;
    round 33's check remembers those perfectly and forgot the file
    written by the OTHER publication step — the same class of defect,
    a third time.

    RETIRES THE CLASS (reviewer-3's own prescription, taken): no
    enumeration at all, of anything. Asks git ONCE, after publication
    fully completes, what is actually stageable across the WHOLE store —
    ``git ls-files --others --exclude-standard`` lists every untracked
    path that is NOT excluded by ``.gitignore``/``.git/info/exclude``/
    global excludes, scoped to ``relative_store_dir``. Anything this
    returns is stageable BY DEFINITION — there is nothing left to
    generalize from, and nothing to fall out of date, because this
    covers ``index.json``, every ``runs/<id>/`` artifact, and any file a
    future slice adds under this same directory, with nobody needing to
    remember to enumerate it.

    Tracked files inside the store are a SEPARATE, already-covered
    concern: :func:`run_privacy_preflight`'s own ``_tracked_paths_under_
    comprehension_dir`` check already refuses those, at lock acquisition,
    before any scan work starts. ``--others`` deliberately never
    re-flags an already-tracked path (that is what "others" excludes) —
    this function's own job is only the untracked-and-unignored gap that
    earlier check cannot see.

    Raises :class:`VcsPrivacyRefused` (``vcs_kind="git"``) the moment
    anything is stageable, or the query itself could not be trusted
    (fail-closed, same as every other check in this module); the caller
    is responsible for rolling back the just-completed publication on
    this raise — this function only ever queries git, it never touches
    the filesystem itself.

    Callers must gate on the recorded disposition themselves, same as
    round 33's own check did: only meaningful for the automatic
    ``"ignored"`` disposition — an operator who explicitly ACKNOWLEDGED
    an unignored store already accepted this exact risk for this one
    run, and re-checking here would spuriously refuse a publish that
    operator already attended to."""
    result = _run_git(root, "ls-files", "--others", "--exclude-standard", "--", relative_store_dir)
    if result is None or result.returncode != 0:
        raise VcsPrivacyRefused(
            "git ls-files could not be trusted to answer whether "
            f"{relative_store_dir}/ has any stageable (untracked, unignored) content "
            "immediately after publication — refusing", vcs_kind="git",
        )
    stageable = sorted(line for line in result.stdout.splitlines() if line)
    if stageable:
        raise VcsPrivacyRefused(
            f"{len(stageable)} path(s) under {relative_store_dir}/ are stageable "
            f"(untracked and not ignored) immediately after publication (e.g. "
            f"{stageable[0]!r}) — refusing, rolling back the just-published run, and "
            "restoring the prior index.json", vcs_kind="git",
        )


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
