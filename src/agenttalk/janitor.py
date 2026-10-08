"""Cross-platform scratch janitor (#148) - a Python port of the validated
`tools/cleanup-scratch.ps1` reference implementation.

Report mode (default) lists scratch candidates and dirty registered
worktrees without touching anything. Apply mode WIP-commits only dirty
worktrees selected for removal, on their own branch (never a default branch, never a detached
HEAD - both are refused outright, neither committed nor removed), removes
the allow-listed candidates, and prunes stale worktree registrations.
Never touches `.agenttalk/`, tracked files, a configured "foreign" folder
under the temp root, or the CONTENTS behind a symlink/junction (the link
itself is unlinked; its target is never touched). A path the ordinary,
link-safe delete cannot remove is kept and reported as FAILED, never
forced. Out of scope: a folder swapped for a link by another process
during the delete itself (single-user tool; see docs/ops/scratch-hygiene.md).

Config (optional, `.agenttalk/config.json`, key `"scratch"`, long form):

    {
      "scratch": {
        "root": "...",                    # see scratch.py
        "keep_days": 3,
        "tmp_keep_days": 1,
        "repo_dir_families": [...],       # fnmatch globs, repo root, directories
        "repo_file_families": [...],      # fnmatch globs, repo root, files
        "tmp_root": "...",                # default: the OS temp dir
        "tmp_families": [...],            # fnmatch globs, temp root, age-gated per entry
        "foreign": [...],                 # exact names under tmp_root: report, never remove
        "default_branches": ["master", "main"]
      }
    }

Every list has a built-in default (below) used when the key is absent, so
an unconfigured project still gets sane behaviour. Every family match is
CASE-SENSITIVE (fnmatch.fnmatch is case-insensitive on Windows, which
would over-match in the shared temp root - fnmatchcase is used instead).
"""

from __future__ import annotations

import dataclasses
import datetime
import fnmatch
import os
import shutil
import stat
import subprocess  # nosec B404 - every call site below uses a resolved-path argv list, shell disabled
import tempfile
from pathlib import Path

from .scratch import _read_scratch_config, default_scratch_root, resolve_keep_days

DEFAULT_REPO_DIR_FAMILIES = [
    ".review-*", ".pytest-*", ".pytest_cache", ".subreview-*", ".gw-*",
    ".probe-*", "probe-*", "gateway-*", ".qa-*", ".codex-tmp-*",
    ".codex-review-tmp", ".tmp_pytest*", ".task*-*",
]
DEFAULT_REPO_FILE_FAMILIES = [
    ".review-*.tar", ".review-*.zip", ".review-*.md", ".agenttalk-*.txt",
    ".agenttalk-*.md", ".coverage",
]
# Narrower than the repo-root families on purpose: the OS temp root is
# shared with every OTHER program on the machine, so a family here must be
# specific enough not to collide with something real (reviewer-3 measured
# 157 false-positive matches on the old, broader list, including a live
# pytest run), and every match - file or directory - is still gated by
# tmp_keep_days below (see find_candidates). No estate/project-specific
# name belongs here (#148 requires the shipped defaults stay
# project-agnostic) - a project's own real-boot/integration-test scratch
# families belong in ITS OWN .agenttalk/config.json under
# scratch.tmp_families, never in this list.
DEFAULT_TMP_FAMILIES = [
    "pytest-of-*", "agenttalk-review-*", "agenttalk-reply-*",
    "agenttalk-gate-*", "agenttalk-probe-*", "agenttalk-wf-*",
    "mockitoboot*", "surefire*",
]
DEFAULT_TMP_KEEP_DAYS = 1
# Never removed regardless of family match, even if a candidate happens to
# collide with one of these names.
_ALWAYS_EXCLUDED_NAMES = {".agenttalk", ".claude", "docs", "tools"}
_ALWAYS_EXCLUDED_PREFIXES = ("launch-", "MANUAL-", ".wt-")
# A worktree on one of these, or a detached HEAD (git prints "HEAD" for
# `rev-parse --abbrev-ref HEAD` when detached), is refused outright.
_NEVER_AUTO_COMMIT_BRANCHES_SENTINELS = {None, "HEAD"}
# Only these ignored directories are disposable in a registered worktree.
# Keep this list shared so other cleanup commands can use the same policy.
IGNORED_CACHE_DIR_PATTERNS = ("__pycache__", ".pytest_cache", ".ruff_cache", "*.egg-info")


@dataclasses.dataclass
class JanitorConfig:
    repo: Path
    scratch_root: Path
    keep_days: int
    tmp_keep_days: int
    tmp_root: Path
    repo_dir_families: list[str]
    repo_file_families: list[str]
    tmp_families: list[str]
    foreign: list[str]
    default_branches: list[str]

    @classmethod
    def load(cls, repo: Path) -> "JanitorConfig":
        # Keep links visible to the scan's checks; resolve() would hide them.
        repo = Path(repo).absolute()
        cfg = _read_scratch_config(repo)
        scratch_root = cfg.get("root")
        tmp_root = cfg.get("tmp_root")
        tmp_keep_days = cfg.get("tmp_keep_days")
        return cls(
            repo=repo,
            scratch_root=Path(scratch_root).absolute()
            if isinstance(scratch_root, str) and scratch_root.strip() else default_scratch_root(repo),
            keep_days=resolve_keep_days(repo),
            tmp_keep_days=tmp_keep_days if isinstance(tmp_keep_days, int) and tmp_keep_days >= 0
            else DEFAULT_TMP_KEEP_DAYS,
            tmp_root=Path(tmp_root).absolute() if tmp_root else Path(tempfile.gettempdir()).absolute(),
            repo_dir_families=list(cfg.get("repo_dir_families") or DEFAULT_REPO_DIR_FAMILIES),
            repo_file_families=list(cfg.get("repo_file_families") or DEFAULT_REPO_FILE_FAMILIES),
            tmp_families=list(cfg.get("tmp_families") or DEFAULT_TMP_FAMILIES),
            foreign=list(cfg.get("foreign") or []),
            default_branches=list(cfg.get("default_branches") or ["master", "main"]),
        )


@dataclasses.dataclass
class Candidate:
    path: Path
    mtime: float
    reason: str  # "repo-dir", "repo-file", "worktrees-dir", "tmp", "scratch-stale"
    link: bool = False  # a symlink/junction when found: removed as the link itself


@dataclasses.dataclass
class JanitorReport:
    candidates: list[Candidate]
    registered_worktrees: list[Path]
    dirty_worktrees: list[Path]
    foreign_kept: list[Path]
    # Discovery locations that could not even be LISTED or TRUSTED (an
    # ACL-denied directory, or a .worktrees/ pass that had to fail closed
    # because git itself was unresolvable or unreliable there) - #148's
    # acceptance is "reported as FAILED, never silently skipped", so
    # these surface exactly like a removal failure, not a crash and not
    # a swallowed exception. Each entry names WHY, not just WHERE.
    access_errors: list[tuple[Path, str]]
    ancestor_ids: dict[Path, tuple[int, int]] | None = None
    apply_failed: bool = False


def _is_excluded_name(name: str) -> bool:
    if name in _ALWAYS_EXCLUDED_NAMES:
        return True
    return any(name.startswith(p) for p in _ALWAYS_EXCLUDED_PREFIXES)


def _matches_any(name: str, patterns: list[str]) -> bool:
    # fnmatchcase, NOT fnmatch: fnmatch normalizes case on Windows, which
    # would over-match unrelated real files/dirs in a shared temp root.
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def is_link_like(path: Path) -> bool:
    """True for a symlink OR a Windows reparse point (junction/mount point).

    `os.path.isjunction` only exists on Python 3.12+, so junctions are
    detected the version-independent way: `os.stat(path, follow_symlinks=
    False)` never follows a reparse point, and on Windows its
    `st_file_attributes` carries `FILE_ATTRIBUTE_REPARSE_POINT` (0x400) for
    BOTH symlinks and junctions. Candidates must NEVER be resolved through
    this - a link is removed as the link itself, never followed into its
    target (reviewer-3 finding P6A/P6B: `Path.resolve()` follows symlinks
    on POSIX too).
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return _is_link_stat(st)


def _is_link_stat(st: os.stat_result) -> bool:
    """`is_link_like` for a result already read without following links."""
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(st, "st_file_attributes", 0) & reparse_point)


def _os_error_text(exc: OSError) -> str:
    """The system's own words for an OSError, without its filename fields."""
    return exc.strerror or str(exc) or type(exc).__name__


def _ancestor_ids(cfg: JanitorConfig) -> tuple[dict[Path, tuple[int, int]], list[tuple[Path, str]]]:
    """Record parents from the drive root down, before entering any scanned root.

    A link that predates the scan is refused too. Do not resolve these paths:
    that would replace the evidence with the link's destination (#399).
    """
    identities: dict[Path, tuple[int, int]] = {}
    for root in (cfg.repo, cfg.scratch_root, cfg.tmp_root):
        for parent in reversed(root.absolute().parents):
            if parent in identities:
                continue
            try:
                st = os.lstat(parent)
            except OSError as exc:
                return identities, [(parent, f"could not check ancestor: {_os_error_text(exc)}")]
            if _is_link_stat(st) or not stat.S_ISDIR(st.st_mode):
                return identities, [(parent, "ancestor is a link or not a plain folder; scan refused")]
            if not st.st_ino:
                return identities, [(parent, "ancestor has no usable file identity; scan refused")]
            identities[parent] = (st.st_dev, st.st_ino)
    return identities, []


def _ancestor_change_reason(identities: dict[Path, tuple[int, int]] | None) -> str | None:
    if not identities:
        return "the scan did not verify the ancestors; nothing was deleted"
    # Shallower parents must be checked before lstat can traverse them.
    for parent in sorted(identities, key=lambda p: len(p.parts)):
        try:
            st = os.lstat(parent)
        except OSError as exc:
            return f"could not check ancestor {parent}: {_os_error_text(exc)}; nothing was deleted"
        if _is_link_stat(st) or not stat.S_ISDIR(st.st_mode):
            return f"ancestor {parent} is a link or no longer a plain folder; nothing was deleted"
        if (st.st_dev, st.st_ino) != identities[parent]:
            return f"ancestor {parent} changed identity since the scan; nothing was deleted"
    return None


def _safe_iterdir(path: Path) -> tuple[list[Path], tuple[Path, str] | None]:
    """List `path`'s entries, or (on ANY OSError - permission denial,
    a vanished directory mid-scan, etc.) return an empty list plus the
    path and reason that failed, instead of raising out of discovery or
    silently returning nothing. The caller surfaces the failure as
    FAILED, with the reason quoted."""
    try:
        return sorted(path.iterdir()), None
    except OSError as exc:
        return [], (path, _os_error_text(exc))


def _run_git_checked(repo: Path, *args: str) -> tuple[int | None, str, str]:
    """Run git, returning (returncode, stdout, stderr). returncode is
    `None` if git itself is unresolvable - callers that need to
    distinguish "genuinely nothing to report" from "could not even run
    git" must check this, not just parse stdout. A prior version of this
    module returned "" for both cases from a single stdout-only helper,
    and every caller (worktree discovery, dirty-status checks, the
    post-commit clean confirmation) silently treated an unrunnable git
    the same as a successful, empty answer - the exact mechanism that let
    a live, dirty, untracked-file-holding worktree be removed with no
    refusal printed anywhere."""
    git = shutil.which("git")
    if git is None:
        return None, "", "git is not resolvable"
    result = subprocess.run(  # nosec B603 - resolved executable, argv list
        [git, "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _run_git(repo: Path, *args: str) -> str:
    """Best-effort stdout only - for call sites where a failure is
    genuinely harmless (e.g. the final `worktree prune`, or `git add`
    before a commit whose own result is checked separately)."""
    _rc, out, _err = _run_git_checked(repo, *args)
    return out


def get_registered_worktrees(repo: Path) -> list[Path]:
    """Best-effort: returns `[]` if git is unresolvable or the command
    fails, the same as "no worktrees registered" to THIS function's own
    callers (a dirty-worktree count and doctor's outside-scratch-root
    warning, neither of which removes anything on the strength of this
    result alone). Candidate discovery under `.worktrees/` does NOT use
    this function for that reason - see `_worktrees_discovery_ok`."""
    _rc, out, _err = _run_git_checked(repo, "worktree", "list", "--porcelain")
    worktrees = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            worktrees.append(Path(line[len("worktree "):]))
    return worktrees


def _worktrees_discovery_ok(repo: Path) -> tuple[bool, str]:
    """Whether candidate discovery under `.worktrees/` can trust git's
    account of what is registered there. False means git is unresolvable,
    or `worktree list` itself failed (a damaged `.git`, "detected dubious
    ownership", etc.) - in either case candidate discovery must leave
    EVERYTHING under `.worktrees/` alone rather than reading the failure
    as "zero worktrees registered" and removing every directory there as
    an ordinary, unregistered candidate with no WIP-commit-or-refuse gate
    ever applied to it."""
    rc, _out, err = _run_git_checked(repo, "worktree", "list", "--porcelain")
    if rc is None:
        return False, "git is not resolvable"
    if rc != 0:
        return False, f"git worktree list failed, rc={rc}: {err.strip()}"
    return True, ""


def _find_git_entries(root: Path) -> tuple[list[Path], bool]:
    """Every `.git` entry (a directory, for an ordinary clone, or a file,
    for a worktree's gitdir pointer) anywhere in `root`'s own tree, plus
    whether any nested subdirectory could not even be listed - an
    lstat-based walk, entirely independent of a runnable git. NEVER
    follows a symlink/junction - a link entry is checked for the name
    `.git` but never descended into, so whatever it points at is
    invisible to this walk either way. An unlistable subdirectory is
    reported via the second return value rather than raising or being
    silently read as "nothing here" (conservative: an unreadable
    subdirectory is exactly the kind of thing this walk exists to guard
    against, not a reason to assume it's safe)."""
    found: list[Path] = []
    entries, err = _safe_iterdir(root)
    if err is not None:
        return found, True
    unlistable = False
    for entry in entries:
        if entry.name == ".git":
            found.append(entry)
            continue
        if is_link_like(entry):
            continue
        if entry.is_dir():
            sub_found, sub_unlistable = _find_git_entries(entry)
            found.extend(sub_found)
            unlistable = unlistable or sub_unlistable
    return found, unlistable


def _tree_contains_git_entry(root: Path) -> bool:
    """True if `root`'s own tree contains a `.git` entry anywhere, OR a
    nested subdirectory could not be listed - see `_find_git_entries`.
    Used when `_worktrees_discovery_ok` is False: git itself cannot be
    trusted to say what is or isn't a worktree, so this is the fallback
    signal that a directory candidate might be (or contain) one, and must
    be refused rather than silently removed."""
    found, unlistable = _find_git_entries(root)
    return bool(found) or unlistable


def _git_entry_belongs_to_registered_worktree(git_entry: Path, registered: list[Path]) -> bool:
    """True if `git_entry` (a `.git` file or directory found somewhere
    inside a candidate's tree) is the `.git` of a worktree this janitor
    already knows is registered to `cfg.repo` - the owning worktree root
    is always `git_entry`'s parent, whether `.git` is a gitdir-pointer
    FILE (a linked worktree) or a DIRECTORY (an ordinary clone/the main
    worktree)."""
    owner = git_entry.parent
    return any(_same_worktree(owner, r) for r in registered)


def _same_worktree(left: Path, right: Path) -> bool:
    """Compare identity only; callers retain the original paths for link checks."""
    try:
        return left.samefile(right)
    except OSError:
        return False


def _contains_worktree(candidate: Path, worktree: Path) -> bool:
    # Git reports physical paths even when discovery used a link or '..'.
    # These resolved paths must never be used as deletion targets.
    physical_worktree = worktree.resolve()
    return candidate.resolve() in (physical_worktree, *physical_worktree.parents)


def _protected_worktree_content(root: Path) -> str | None:
    entries, error = _safe_iterdir(root)
    if error:
        return f"could not check worktree contents: {error[1]}"
    for entry in entries:
        if entry.name.casefold() in {".agenttalk", ".env"}:
            return f"protected worktree content: {entry.name}"
        if entry.name == ".git" or is_link_like(entry):
            continue
        if entry.is_dir():
            reason = _protected_worktree_content(entry)
            if reason:
                return reason
    return None


def ignored_worktree_keep_reason(worktree: Path) -> str | None:
    """Keep ignored work unless every entry belongs to a known cache directory."""
    protected = _protected_worktree_content(worktree)
    if protected:
        return f"ignored or unignored {protected}"
    rc, out, err = _run_git_checked(worktree, "ls-files", "--others", "--ignored",
                                     "--exclude-standard", "-z")
    if rc != 0:
        return f"could not check ignored files: {err.strip()}"
    for name in out.split("\0"):
        if not name:
            continue
        parts = Path(name).parts
        cache = any(_matches_any(p, list(IGNORED_CACHE_DIR_PATTERNS)) for p in parts[:-1])
        if not cache:
            return f"ignored content is not disposable: {name}"
    return None


def is_dirty_worktree(path: Path) -> bool:
    """True if the worktree has ANY uncommitted change - tracked or
    untracked - OR if `git status` itself could not be run/completed for
    it. An untracked file left behind by a `.worktrees/` removal is real,
    unrecovered work; it is no longer excluded from "dirty" the way a
    purely cosmetic ignored-file diff would be. Failing to determine the
    status at all is treated as dirty, not clean - it routes the worktree
    through the WIP-commit-or-refuse gate instead of silently letting an
    unconfirmed one through as "nothing to do here"."""
    if not (path / ".git").exists():
        return False
    rc, out, _err = _run_git_checked(path, "status", "--porcelain")
    if rc != 0:
        return True
    return bool(out.strip())


def worktree_branch(path: Path) -> str | None:
    out = _run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return out or None


def worktree_head_reachable(path: Path) -> bool:
    """True if the worktree's HEAD commit is reachable from an existing
    branch, tag, or remote-tracking ref (`git for-each-ref --contains
    HEAD refs/heads refs/tags refs/remotes` is non-empty). `refs/remotes`
    is included deliberately: Rule 2's own recommended review-worktree
    form (`git worktree add --detach <scratch>/wt-<sha> <sha>`) is
    routinely detached at a fetched PR head that lives ONLY under
    `refs/remotes/origin/*` until it lands on a local branch - without
    it, that ordinary, disposable checkout would be refused on every
    single run for as long as it exists (over-refusal, not data loss,
    but it defeats the point of automated cleanup for the exact case the
    docs recommend). False (unreachable) also if the check itself could
    not be completed - fail closed: a detached HEAD whose reachability
    this janitor cannot confirm is exactly the case that must be
    protected, not assumed safe. Only meaningful for a DETACHED worktree
    (`worktree_branch` returning `None` or the literal string `"HEAD"` -
    git's own `rev-parse --abbrev-ref HEAD` prints "HEAD" for a detached
    checkout, not an empty string) - a worktree checked out onto a real
    branch is trivially reachable via that branch itself."""
    rc, out, _err = _run_git_checked(
        path, "for-each-ref", "--contains", "HEAD",
        "refs/heads", "refs/tags", "refs/remotes",
    )
    if rc != 0:
        return False
    return bool(out.strip())


def find_candidates(cfg: JanitorConfig) -> tuple[list[Candidate], list[tuple[Path, str]]]:
    _, ancestor_errors = _ancestor_ids(cfg)
    if ancestor_errors:
        return [], ancestor_errors
    seen: set[str] = set()
    out: list[Candidate] = []
    access_errors: list[tuple[Path, str]] = []
    # Computed ONCE: whether git's account of registered worktrees can be
    # trusted at all, and (when it can) what IS registered to cfg.repo.
    # Every directory candidate outside .worktrees/ is checked against
    # this regardless of discovery - the earlier version only ran the
    # `.git`-in-tree guard when discovery was UNTRUSTED, which is
    # backwards for a worktree registered to some OTHER clone (every seat
    # shares one scratch root - Rule 1) or a standalone clone: git being
    # perfectly resolvable for cfg.repo says nothing about whether a
    # tree elsewhere under the shared root belongs to it.
    discovery_ok, discovery_reason = _worktrees_discovery_ok(cfg.repo)
    registered_worktrees = get_registered_worktrees(cfg.repo) if discovery_ok else []

    def add(path: Path, reason: str) -> None:
        # NEVER path.resolve() - that follows symlinks (POSIX) and would
        # silently retarget a link candidate onto whatever it points at
        # (P6A). normcase-of-the-given-path is dedup enough; identity, not
        # canonicalization.
        key = os.path.normcase(str(path))
        if key in seen or _is_excluded_name(path.name):
            return
        try:
            entry_stat = os.lstat(path)
        except OSError:
            return
        entry_mtime = entry_stat.st_mtime
        # No exemption for .worktrees/ entries: that pass adds EVERY
        # directory found there regardless of name (see below), not just
        # ones git actually knows about - an unregistered tree living
        # there (another clone's worktree dropped in by hand, a
        # standalone clone, or this repo's OWN worktree whose
        # `.git/worktrees/<name>` admin entry was separately lost) is
        # exactly as invisible to `get_registered_worktrees` as one
        # found anywhere else, and the dirty/refuse gate in apply() only
        # ever runs for entries git DOES still recognize as registered.
        if not is_link_like(path) and path.is_dir():
            git_entries, unlistable = _find_git_entries(path)
            if not discovery_ok:
                if git_entries or unlistable:
                    access_errors.append((
                        path,
                        "worktree discovery is not trusted (git unresolvable or "
                        "`worktree list` failed) and this directory's tree contains "
                        "a .git entry - refusing to remove",
                    ))
                    return
            else:
                foreign = [
                    g for g in git_entries
                    if not _git_entry_belongs_to_registered_worktree(g, registered_worktrees)
                ]
                if foreign or unlistable:
                    access_errors.append((
                        path,
                        "this directory's tree contains a .git entry that is not a "
                        "registered worktree of this repository (another clone's "
                        "worktree, a standalone clone, or an unlistable "
                        "subdirectory) - refusing to remove",
                    ))
                    return
        seen.add(key)
        out.append(Candidate(path=path, mtime=entry_mtime, reason=reason,
                             link=_is_link_stat(entry_stat)))

    if cfg.repo.is_dir():
        entries, err = _safe_iterdir(cfg.repo)
        if err is not None:
            access_errors.append(err)
        for entry in entries:
            # A link is a candidate in its own right (removed as a link,
            # never followed) - checked before is_dir()/is_file(), both of
            # which FOLLOW symlinks/junctions and would misclassify it.
            if is_link_like(entry):
                if _matches_any(entry.name, cfg.repo_dir_families + cfg.repo_file_families):
                    add(entry, "repo-dir")
                continue
            if entry.is_dir() and _matches_any(entry.name, cfg.repo_dir_families):
                add(entry, "repo-dir")
            elif entry.is_file() and _matches_any(entry.name, cfg.repo_file_families):
                add(entry, "repo-file")

    worktrees_dir = cfg.repo / ".worktrees"
    if worktrees_dir.is_dir():
        # FAIL CLOSED: candidate discovery here must be able to trust
        # git's own account of what is registered under .worktrees/ - a
        # directory this janitor cannot ask git about is NOT the same as
        # "not a worktree", and reading it that way is exactly how a
        # live, dirty, unregistered-in-its-own-eyes worktree used to be
        # removed as a plain candidate with no WIP-commit-or-refuse gate
        # ever applied to it (git unresolvable, or `worktree list` itself
        # failing - a damaged `.git`, "detected dubious ownership").
        # Nothing under .worktrees/ is touched until discovery is trusted.
        if not discovery_ok:
            access_errors.append((worktrees_dir, discovery_reason))
        else:
            entries, err = _safe_iterdir(worktrees_dir)
            if err is not None:
                access_errors.append(err)
            for entry in entries:
                # Every directory (or dir-like link) under .worktrees/ is a
                # candidate regardless of name - this location IS the
                # family; no separate name-family filter applies here.
                if is_link_like(entry) or entry.is_dir():
                    add(entry, "worktrees-dir")

    if cfg.tmp_root.is_dir():
        cutoff = datetime.datetime.now().timestamp() - cfg.tmp_keep_days * 86400
        entries, err = _safe_iterdir(cfg.tmp_root)
        if err is not None:
            access_errors.append(err)
        for entry in entries:
            if entry.name in cfg.foreign:
                continue
            if not _matches_any(entry.name, cfg.tmp_families):
                continue
            # A match here (file OR directory - some real families, like
            # a Mockito boot log or a surefire report, are files) is only
            # a candidate once past tmp_keep_days: this root is shared
            # machine-wide, not owned the way the repo root/scratch root
            # are, so age is the load-bearing safety check for EVERY
            # entry, not an extra filter on top of a type restriction.
            # A DIRECTORY entry is aged by the newest mtime anywhere in
            # its tree, not its own top-level mtime: a directory's own
            # mtime does not change when a file nested inside it is
            # written, so a live file three levels down inside an
            # apparently-old top-level directory would otherwise be
            # deleted. A link or a plain file has no nested tree, so
            # lstat is already correct/complete for those.
            if is_link_like(entry) or not entry.is_dir():
                try:
                    entry_mtime = os.lstat(entry).st_mtime
                except OSError:
                    continue
            else:
                entry_mtime, terr = _newest_mtime_in_tree(entry, at_least=cutoff)
                if terr is not None:
                    access_errors.append(terr)
                    continue
                if entry_mtime is None:
                    continue
            if entry_mtime >= cutoff:
                continue
            add(entry, "tmp")

    if cfg.scratch_root.is_dir():
        cutoff = datetime.datetime.now().timestamp() - cfg.keep_days * 86400
        agent_entries, err = _safe_iterdir(cfg.scratch_root)
        if err is not None:
            access_errors.append(err)
        for agent_dir in agent_entries:
            # A link ABOVE a candidate must not be walked through either -
            # a link-like agent/task entry is a candidate in its own
            # right (matching the repo-root pass), never recursed into.
            # It still needs its OWN age check against the same cutoff
            # first, though: a fresh junction (an operator's scratch
            # relocation, or a seat's live task link, age 0s) must not be
            # removed just because it happens to be link-like - only
            # once it is actually older than keep_days, exactly like
            # every other scratch-root candidate.
            if is_link_like(agent_dir):
                try:
                    link_mtime = os.lstat(agent_dir).st_mtime
                except OSError:
                    continue
                if link_mtime < cutoff:
                    add(agent_dir, "scratch-stale")
                continue
            if not agent_dir.is_dir():
                continue
            task_entries, terr = _safe_iterdir(agent_dir)
            if terr is not None:
                access_errors.append(terr)
            for task_dir in task_entries:
                if is_link_like(task_dir):
                    try:
                        link_mtime = os.lstat(task_dir).st_mtime
                    except OSError:
                        continue
                    if link_mtime < cutoff:
                        add(task_dir, "scratch-stale")
                    continue
                if not task_dir.is_dir():
                    continue
                # Staleness is the NEWEST mtime anywhere in the tree, not
                # the task directory's own mtime (P3): a directory's mtime
                # only changes when an entry is added/removed/renamed
                # directly inside it, NOT when a file three levels down is
                # edited - live work under an old task directory would
                # otherwise be misclassified as stale and deleted.
                newest, nerr = _newest_mtime_in_tree(task_dir, at_least=cutoff)
                if nerr:
                    access_errors.append(nerr)
                if newest is not None and newest < cutoff:
                    add(task_dir, "scratch-stale")

    return out, access_errors


def _newest_mtime_in_tree(
    root: Path, *, at_least: float | None = None
) -> tuple[float | None, tuple[Path, str] | None]:
    """The newest mtime of `root` itself or anything nested under it.
    Returns (None, (error_path, reason)) if any nested directory could
    not be listed, rather than silently under-counting staleness.

    `at_least`: if given, the caller only cares whether the tree is AT
    LEAST this fresh (e.g. "not stale" - past some cutoff), not the exact
    maximum - the walk returns as soon as it finds an entry that alone
    already clears the threshold, capping cost on a very large tree
    (this function runs once per scratch task on every `agenttalk
    doctor`/`janitor` invocation, so an unbounded walk scales with the
    total size of every task directory, not just the ones near the
    staleness boundary)."""
    try:
        newest = os.lstat(root).st_mtime
    except OSError as exc:
        return None, (root, _os_error_text(exc))
    if at_least is not None and newest >= at_least:
        return newest, None
    entries, err = _safe_iterdir(root)
    if err is not None:
        return None, err
    for entry in entries:
        if is_link_like(entry):
            try:
                newest = max(newest, os.lstat(entry).st_mtime)
            except OSError:
                pass
        elif entry.is_dir():
            sub_newest, sub_err = _newest_mtime_in_tree(entry, at_least=at_least)
            if sub_err is not None:
                return None, sub_err
            if sub_newest is not None:
                newest = max(newest, sub_newest)
        else:
            try:
                newest = max(newest, os.lstat(entry).st_mtime)
            except OSError:
                pass
        if at_least is not None and newest >= at_least:
            return newest, None
    return newest, None


def find_foreign_kept(cfg: JanitorConfig) -> list[Path]:
    if not cfg.tmp_root.is_dir():
        return []
    return [
        cfg.tmp_root / name
        for name in cfg.foreign
        if (cfg.tmp_root / name).is_dir() or is_link_like(cfg.tmp_root / name)
    ]


def build_report(cfg: JanitorConfig) -> JanitorReport:
    ancestor_ids, ancestor_errors = _ancestor_ids(cfg)
    if ancestor_errors:
        return JanitorReport([], [], [], [], ancestor_errors)
    candidates, access_errors = find_candidates(cfg)
    registered = get_registered_worktrees(cfg.repo)
    dirty = [
        w for w in registered
        if not _same_worktree(w, cfg.repo) and w.exists() and is_dirty_worktree(w)
    ]
    foreign = find_foreign_kept(cfg)
    return JanitorReport(
        candidates=candidates, registered_worktrees=registered,
        dirty_worktrees=dirty, foreign_kept=foreign, access_errors=access_errors,
        ancestor_ids=ancestor_ids,
    )


def format_report(report: JanitorReport, cfg: JanitorConfig, *, apply: bool) -> str:
    lines = [
        f"agenttalk janitor  repo={cfg.repo}  tmp={cfg.tmp_root}  "
        f"scratch={cfg.scratch_root}  mode={'APPLY' if apply else 'REPORT'}",
        f"candidates: {len(report.candidates)}   "
        f"registered worktrees: {len(report.registered_worktrees)}",
        f"dirty registered worktrees: {len(report.dirty_worktrees)}",
    ]
    for w in report.dirty_worktrees:
        lines.append(f"  DIRTY {w}")
    for f in report.foreign_kept:
        lines.append(f"  FOREIGN (kept) {f}")
    if report.access_errors:
        lines.append(f"FAILED to list ({len(report.access_errors)}) - not silently skipped:")
        for p, reason in report.access_errors:
            lines.append(f"  {_scanned_root_relative(p, cfg)}: {reason}")
    if not apply:
        oldest = sorted(report.candidates, key=lambda c: c.mtime)[:15]
        for c in oldest:
            when = datetime.datetime.fromtimestamp(c.mtime).strftime("%Y-%m-%d")
            lines.append(f"  {when} {c.path}")
        if len(report.candidates) > 15:
            lines.append(f"  ... and {len(report.candidates) - 15} more")
        lines.append("re-run with --apply to preserve dirty removal candidates as WIP commits, "
                      "delete, and prune.")
    return "\n".join(lines)


@dataclasses.dataclass
class WipCommitResult:
    message: str
    refused: bool  # True: default branch or detached HEAD - not committed AND not removed


def wip_commit_dirty_worktree(path: Path, *, default_branches: list[str]) -> WipCommitResult:
    """Commit ALL changes (tracked and untracked - P2) as a WIP commit on
    the worktree's own branch. Refuses (no commit, and the caller must not
    remove the directory either) if:
    - the worktree is on a default branch OR is a detached HEAD (P1): a WIP
      commit made on a detached HEAD is reachable from no ref and becomes
      an unreachable object the moment the directory is removed -
      recoverable only until the next `git gc`, i.e. not really preserved.
      `--detach` is the ops doc's OWN recommended form for a review
      worktree, so this is not a corner case.
    - `git` cannot be resolved, `git add -A` fails, `git commit` fails (a
      refusing pre-commit hook, `commit.gpgsign` without a key, no
      configured user identity), or the worktree is STILL dirty after
      the commit supposedly succeeded. Never `--no-verify`: a
      refusing hook should keep the work, not be bypassed. Every one of
      these previously fell through silently - `check=False` on both git
      calls, no return-code check, always reporting "WIP committed" while
      apply() went on to remove the (still uncommitted) directory."""
    branch = worktree_branch(path)
    if branch in default_branches or branch in _NEVER_AUTO_COMMIT_BRANCHES_SENTINELS:
        label = branch or "an unresolvable HEAD"
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {label} (never auto-commit or remove "
                     f"a default branch or a detached HEAD): {path}",
            refused=True,
        )
    git = shutil.which("git")
    if git is None:
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {branch} (git not resolvable, cannot "
                     f"commit): {path}",
            refused=True,
        )
    add_result = subprocess.run(  # nosec B603 - resolved executable, argv list
        [git, "-C", str(path), "add", "-A"], capture_output=True, text=True, check=False,
    )
    if add_result.returncode != 0:
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {branch} (git add -A failed, rc="
                     f"{add_result.returncode}: {add_result.stderr.strip()}): {path}",
            refused=True,
        )
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    commit_result = subprocess.run(  # nosec B603 - resolved executable, argv list
        [git, "-C", str(path), "commit", "-q", "-m",
         f"wip: preserve uncommitted worktree state before scratch cleanup ({stamp})"],
        capture_output=True, text=True, check=False,
    )
    if commit_result.returncode != 0:
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {branch} (git commit failed, rc="
                     f"{commit_result.returncode}: {commit_result.stderr.strip()}): {path}",
            refused=True,
        )
    status_rc, status_out, status_err = _run_git_checked(path, "status", "--porcelain")
    if status_rc != 0:
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {branch} (could not confirm clean after "
                     f"commit, git status rc={status_rc}: {status_err.strip()}): {path}",
            refused=True,
        )
    if status_out.strip():
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {branch} (still dirty after commit - "
                     f"refusing to remove): {path}",
            refused=True,
        )
    return WipCommitResult(message=f"  WIP committed on {branch}: {path}", refused=False)


def _make_tree_writable(path: Path) -> None:
    """Clear the read-only bit recursively before removal (Windows: git
    marks its own `.git/objects/**` blobs read-only, so a plain
    `shutil.rmtree` on a full clone hits PermissionError - reviewer-3's
    teardown observation). Best-effort; a failure here just means the
    remove attempt may also fail, and the path is then kept and reported.

    A link entry is skipped ENTIRELY, not just left unrecursed into:
    `os.chmod` follows symlinks by default on POSIX, so chmod-ing a
    symlink candidate would silently change its TARGET's
    mode - exactly the kind of reach-through-the-link this module exists
    to prevent everywhere else."""
    if is_link_like(path):
        return
    try:
        current = os.stat(path, follow_symlinks=False).st_mode
        os.chmod(path, current | stat.S_IWRITE)
    except OSError:
        pass
    if not path.is_dir():
        return
    entries, _err = _safe_iterdir(path)
    for entry in entries:
        _make_tree_writable(entry)


def _rmtree(path: Path) -> None:
    if is_link_like(path):
        # The link itself, never its target - os.remove works for a file
        # symlink; a directory symlink/junction needs rmdir on Windows.
        try:
            os.remove(path)
        except OSError:
            os.rmdir(path)
        return
    if path.is_dir():
        _make_tree_writable(path)
        shutil.rmtree(path)
    else:
        path.unlink()


def _failure_reason(exc: OSError, path: Path) -> str:
    """The OS's own words for why a delete failed, plus WHERE inside `path`
    when the error names an entry there - never an absolute path."""
    what = _os_error_text(exc)
    where = exc.filename
    if isinstance(where, str) and where:
        try:
            rel = os.path.relpath(where, path)
        except ValueError:  # on another drive
            rel = ""
        if rel and rel != os.curdir and not rel.startswith(os.pardir):
            return f"{what} (at {rel})"
    return what


def remove_plainly(path: Path) -> tuple[str, str | None]:
    """Remove `path` the ordinary, link-safe way and never try harder.

    `_rmtree` unlinks a link as the link itself, and `shutil.rmtree` unlinks
    a symlink or junction found nested in the tree without entering it (a
    link swapped in by another process during the delete is out of scope;
    see `_path_change_reason`). Returns ('absent', None), ('removed', None)
    or ('FAILED', reason); 'absent' and 'removed' need a confirmed not-found
    (`_exists`). On FAILED the path stays as the failed attempt left it, for
    the caller to keep and report - files removed before the failure stay
    removed, and read-only flags may have been cleared (`_make_tree_writable`).
    No ownership takeover, no permission grant and no mirror delete: each of
    those walks into a folder link nested in the tree and acts on whatever
    lies behind it (#342). Never raises."""
    try:
        if not _exists(path):
            return "absent", None
    except OSError as exc:
        return "FAILED", f"could not check whether it exists: {_os_error_text(exc)}"
    delete_reason = None
    try:
        _rmtree(path)
    except OSError as exc:
        delete_reason = _failure_reason(exc, path)
    try:
        gone = not _exists(path)
    except OSError as exc:
        return "FAILED", delete_reason or f"could not confirm it is gone: {_os_error_text(exc)}"
    if gone:
        return "removed", None
    return "FAILED", delete_reason or "still present after the delete"


def _exists(path: Path) -> bool:
    """False only for a CONFIRMED not-found. `os.path.lexists` reads any
    failed query (a denied metadata read, for one) as "not there", which
    would report a folder still on disk as absent or removed (#342 review);
    every other error is raised for the caller to report as FAILED."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _path_change_reason(c: Candidate, cfg: JanitorConfig) -> str | None:
    """Why `c` must be kept rather than deleted where it was found, or None.

    Checked immediately before the delete, with lstat (never following a
    link), in three cases:
    - The scanned root and every folder below it, down to the candidate's
      parent, must still be plain folders. A junction, symlink or other
      reparse point there keeps the candidate: a parent swapped for a
      junction after discovery would otherwise send the delete into the
      junction's target (#342 review, probe 1).
    - A candidate that was NOT a link when found and is one now is kept and
      reported.
    - A candidate that WAS already a link when found is removed as the link
      itself; what it points to is never touched. If it is no longer a link,
      it is kept.
    This compares link-or-not status with what discovery recorded
    (Candidate.link); it does not prove the object is the same one (no inode
    or file id). Any failed check keeps the candidate. What this cannot
    cover is a swap made by another process inside the delete itself; janitor
    is a single-user tool that runs while no other process rewrites the tree
    (see docs/ops/scratch-hygiene.md)."""
    found = _scanned_root_of(c.path, cfg)
    if found is None:
        return "it is not under a scanned root; nothing was deleted"
    label, root, rel = found
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        return f"could not check the scanned root [{label}]: {_os_error_text(exc)}; nothing was deleted"
    if _is_link_stat(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        return (f"the scanned root [{label}] is a link or no longer a plain folder; "
                "nothing was deleted")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        try:
            st = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return None  # gone, so the candidate is too: removal reports it absent
        except OSError as exc:
            return (f"could not check the folder {current.relative_to(root)} on its path: "
                    f"{_os_error_text(exc)}; nothing was deleted")
        if _is_link_stat(st):
            return (f"the folder {current.relative_to(root)} on its path is now a link "
                    "(junction, symbolic link or other reparse point); nothing was deleted")
        if not stat.S_ISDIR(st.st_mode):
            return (f"{current.relative_to(root)} on its path is no longer a folder; "
                    "nothing was deleted")
    try:
        st = os.lstat(c.path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        return f"could not check it before the delete: {_os_error_text(exc)}; nothing was deleted"
    if _is_link_stat(st) and not c.link:
        return ("it is now a link (junction, symbolic link or other reparse point), and it was "
                "not when it was found; nothing was deleted")
    if c.link and not _is_link_stat(st):
        return "it was a link when it was found and is not one now; nothing was deleted"
    return None


def _scanned_root_of(path: Path, cfg: JanitorConfig) -> tuple[str, Path, Path] | None:
    """(label, root, path relative to it) for the scanned root that holds
    `path` most closely: the scratch, tmp and repo roots of the report's
    first line. Never resolved: a link is named where it was found."""
    best: tuple[str, Path, Path] | None = None
    for label, root in (("scratch", cfg.scratch_root), ("tmp", cfg.tmp_root), ("repo", cfg.repo)):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if best is None or len(rel.parts) < len(best[2].parts):
            best = (label, root, rel)
    return best


def _scanned_root_relative(path: Path, cfg: JanitorConfig) -> str:
    """`path` as `[<root>] <relative path>` (see `_scanned_root_of`)."""
    found = _scanned_root_of(path, cfg)
    return f"[{found[0]}] {found[2]}" if found else str(path)


def apply(cfg: JanitorConfig, report: JanitorReport) -> str:
    """Run apply mode: WIP-commit dirty worktrees, remove candidates, prune.
    Returns the report text (same shape as report mode, plus the apply
    summary)."""
    report.apply_failed = bool(report.access_errors)
    lines = [format_report(report, cfg, apply=True)]

    def refuse_ancestry(reason: str) -> str:
        report.apply_failed = True
        return "\n".join(lines + [
            "removed summary:", f"  FAILED: {max(1, len(report.candidates))}",
            f"FAILED: {reason}; no worktree commit or prune was attempted after this failure",
            *[f"  {_scanned_root_relative(c.path, cfg)}: {reason}" for c in report.candidates],
        ])

    reason = _ancestor_change_reason(report.ancestor_ids)
    if reason is not None:
        return refuse_ancestry(reason)
    summary: dict[str, int] = {}
    failed: list[tuple[Path, str]] = []
    for c in report.candidates:
        reason = _ancestor_change_reason(report.ancestor_ids) or _path_change_reason(c, cfg)
        if reason is not None:
            result = "FAILED"
        else:
            # Complete all keep checks for this candidate before any WIP commit.
            # Link candidates are only unlinked, never entered or committed.
            worktrees = []
            try:
                if not c.link:
                    worktrees = [w for w in report.registered_worktrees
                                 if w.exists() and _contains_worktree(c.path, w)]
                keep = None
                for w in report.registered_worktrees:
                    if (not c.link and w.exists() and not _same_worktree(w, cfg.repo)
                            and not _same_worktree(w, c.path) and _contains_worktree(w, c.path)):
                        keep = "candidate is inside a registered worktree; keep it until the whole worktree is removed"
                        break
                for w in worktrees:
                    if keep:
                        break
                    if _same_worktree(w, cfg.repo):
                        keep = "the main repository is never committed or removed"
                        break
                    keep = ignored_worktree_keep_reason(w)
                    if keep:
                        break
                    branch = worktree_branch(w)
                    unsafe_branch = (branch in cfg.default_branches
                                     or branch in _NEVER_AUTO_COMMIT_BRANCHES_SENTINELS)
                    if unsafe_branch and is_dirty_worktree(w):
                        keep = "dirty worktree on a default branch or detached HEAD"
                        break
                    if (branch in _NEVER_AUTO_COMMIT_BRANCHES_SENTINELS
                            and not worktree_head_reachable(w)):
                        keep = "detached worktree with unreachable HEAD (would be lost on prune)"
                        break
                if keep:
                    lines.append(f"  REFUSED (kept) {c.path}: {keep}")
                    summary["refused"] = summary.get("refused", 0) + 1
                    continue
            except (OSError, RuntimeError) as exc:
                reason = f"could not check worktree identity: {exc}"

            refused = False
            for w in worktrees:
                if reason:
                    break
                reason = _ancestor_change_reason(report.ancestor_ids) or _path_change_reason(c, cfg)
                if reason:
                    break
                if is_dirty_worktree(w):
                    commit = wip_commit_dirty_worktree(w, default_branches=cfg.default_branches)
                    lines.append(commit.message)
                    if commit.refused:
                        refused = True
                        break
            if refused:
                summary["refused"] = summary.get("refused", 0) + 1
                continue
            if reason is None:
                reason = _ancestor_change_reason(report.ancestor_ids) or _path_change_reason(c, cfg)
            if reason is not None:
                result = "FAILED"
            else:
                result, reason = remove_plainly(c.path)
        summary[result] = summary.get(result, 0) + 1
        if result == "FAILED":
            report.apply_failed = True
            failed.append((c.path, reason or "unknown"))

    reason = _ancestor_change_reason(report.ancestor_ids)
    if reason is None:
        _run_git(cfg.repo, "worktree", "prune")
        lines.append(f"registered worktrees after prune: {len(get_registered_worktrees(cfg.repo))}")
    else:
        report.apply_failed = True
        lines.append(f"FAILED: worktree prune skipped: {reason}")

    lines.append("removed summary:")
    for key, count in summary.items():
        lines.append(f"  {key}: {count}")
    if failed:
        lines.append(f"FAILED ({len(failed)}) - kept, not removed: janitor never forces a delete. "
                     "Look at each one, then remove it yourself:")
        for path, reason in failed:
            lines.append(f"  {_scanned_root_relative(path, cfg)}: {reason}")
    return "\n".join(lines)
