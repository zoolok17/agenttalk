"""Cross-platform scratch janitor (#148) - a Python port of the validated
`tools/cleanup-scratch.ps1` reference implementation.

Report mode (default) lists scratch candidates and dirty registered
worktrees without touching anything. Apply mode WIP-commits dirty
worktrees on their own branch (never a default branch, never a detached
HEAD - both are refused outright, neither committed nor removed), removes
the allow-listed candidates, and prunes stale worktree registrations.
Never touches `.agenttalk/`, tracked files, a configured "foreign" folder
under the temp root, or the CONTENTS behind a symlink/junction (the link
itself is unlinked; its target is never touched).

Config (optional, `.agenttalk/config.json`, key `"scratch"`, long form):

    {
      "scratch": {
        "root": "...",                    # see scratch.py
        "keep_days": 3,
        "tmp_keep_days": 1,
        "repo_dir_families": [...],       # fnmatch globs, repo root, directories
        "repo_file_families": [...],      # fnmatch globs, repo root, files
        "tmp_root": "...",                # default: the OS temp dir
        "tmp_families": [...],            # fnmatch globs, temp root, DIRECTORIES only
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
import platform
import shutil
import stat
import subprocess  # nosec B404 - every call site below uses a resolved-path argv list, shell disabled
import tempfile
import uuid
from pathlib import Path

from .scratch import _read_scratch_config, resolve_keep_days, resolve_scratch_root

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
# pytest run). Directories only - see find_candidates.
DEFAULT_TMP_FAMILIES = [
    "pytest-of-*", "agenttalk-review-*", "agenttalk-reply-*",
    "agenttalk-gate-*", "agenttalk-probe-*", "agenttalk-wf-*",
    "mockitoboot*", "backend-request-test-*", "surefire*",
]
DEFAULT_TMP_KEEP_DAYS = 1
# Never removed regardless of family match, even if a candidate happens to
# collide with one of these names.
_ALWAYS_EXCLUDED_NAMES = {".agenttalk", ".claude", "docs", "tools"}
_ALWAYS_EXCLUDED_PREFIXES = ("launch-", "MANUAL-", ".wt-")
# A worktree on one of these, or a detached HEAD (git prints "HEAD" for
# `rev-parse --abbrev-ref HEAD` when detached), is refused outright.
_NEVER_AUTO_COMMIT_BRANCHES_SENTINELS = {None, "HEAD"}

_ELEVATED_HINT_WINDOWS = (
    "re-run elevated: "
    "pwsh -Command \"Start-Process pwsh -Verb RunAs -Wait -ArgumentList "
    "'-NoProfile','-ExecutionPolicy','Bypass','-Command','agenttalk janitor --apply'\""
)


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
        repo = Path(repo).resolve()
        cfg = _read_scratch_config(repo)
        tmp_root = cfg.get("tmp_root")
        tmp_keep_days = cfg.get("tmp_keep_days")
        return cls(
            repo=repo,
            scratch_root=resolve_scratch_root(repo),
            keep_days=resolve_keep_days(repo),
            tmp_keep_days=tmp_keep_days if isinstance(tmp_keep_days, int) and tmp_keep_days >= 0
            else DEFAULT_TMP_KEEP_DAYS,
            tmp_root=Path(tmp_root).resolve() if tmp_root else Path(tempfile.gettempdir()),
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


@dataclasses.dataclass
class JanitorReport:
    candidates: list[Candidate]
    registered_worktrees: list[Path]
    dirty_worktrees: list[Path]
    foreign_kept: list[Path]
    # Discovery locations that could not even be LISTED (e.g. an ACL-denied
    # directory) - #148's acceptance is "reported as FAILED, never silently
    # skipped", so these surface exactly like a removal failure, not a
    # crash and not a swallowed exception.
    access_errors: list[Path]


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
    target (Codex xhigh finding P6A/P6B: `Path.resolve()` follows symlinks
    on POSIX too, and un-guarded escalation mirrors an empty dir INTO a
    junction's target instead of refusing it).
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(st, "st_file_attributes", 0) & reparse_point)


def _safe_iterdir(path: Path) -> tuple[list[Path], Path | None]:
    """List `path`'s entries, or (on ANY OSError - permission denial,
    a vanished directory mid-scan, etc.) return an empty list plus the
    path that failed, instead of raising out of discovery or silently
    returning nothing. The caller surfaces the failure path as FAILED."""
    try:
        return sorted(path.iterdir()), None
    except OSError:
        return [], path


def _resolve_system_tool(name: str) -> str | None:
    """Resolve an escalation tool to an absolute path (bandit B607 - never
    start a process with a partial/PATH-searched name). Falls back to the
    well-known System32 location since takeown/icacls/robocopy are core
    Windows components that may not be on PATH in a restricted shell."""
    found = shutil.which(name)
    if found:
        return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / f"{name}.exe"
    return str(candidate) if candidate.is_file() else None


def _run_git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    result = subprocess.run(  # nosec B603 - resolved executable, argv list
        [git, "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    return result.stdout


def get_registered_worktrees(repo: Path) -> list[Path]:
    out = _run_git(repo, "worktree", "list", "--porcelain")
    worktrees = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            worktrees.append(Path(line[len("worktree "):]))
    return worktrees


def is_dirty_worktree(path: Path) -> bool:
    """True if the worktree has ANY uncommitted change - tracked or
    untracked. An untracked file left behind by a `.worktrees/` removal is
    real, unrecovered work (Codex xhigh finding P2); it is no longer
    excluded from "dirty" the way a purely cosmetic ignored-file diff
    would be."""
    if not (path / ".git").exists():
        return False
    return bool(_run_git(path, "status", "--porcelain").strip())


def worktree_branch(path: Path) -> str | None:
    out = _run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return out or None


def find_candidates(cfg: JanitorConfig) -> tuple[list[Candidate], list[Path]]:
    seen: set[str] = set()
    out: list[Candidate] = []
    access_errors: list[Path] = []

    def add(path: Path, reason: str) -> None:
        # NEVER path.resolve() - that follows symlinks (POSIX) and would
        # silently retarget a link candidate onto whatever it points at
        # (P6A). normcase-of-the-given-path is dedup enough; identity, not
        # canonicalization.
        key = os.path.normcase(str(path))
        if key in seen or _is_excluded_name(path.name):
            return
        try:
            entry_mtime = os.lstat(path).st_mtime
        except OSError:
            return
        seen.add(key)
        out.append(Candidate(path=path, mtime=entry_mtime, reason=reason))

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
        entries, err = _safe_iterdir(worktrees_dir)
        if err is not None:
            access_errors.append(err)
        for entry in entries:
            # Every directory (or dir-like link) under .worktrees/ is a
            # candidate regardless of name - this location IS the family;
            # no separate name-family filter applies here.
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
            # Directories only (P4): a matching FILE in the shared temp
            # root (another program's log, e.g.) is not this janitor's to
            # remove - and age-gated, since this root is shared machine-
            # wide, not owned the way the repo root/scratch root are.
            link = is_link_like(entry)
            if not link and not entry.is_dir():
                continue
            try:
                entry_mtime = os.lstat(entry).st_mtime
            except OSError:
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
            if not agent_dir.is_dir():
                continue
            task_entries, terr = _safe_iterdir(agent_dir)
            if terr is not None:
                access_errors.append(terr)
            for task_dir in task_entries:
                if not task_dir.is_dir():
                    continue
                # Staleness is the NEWEST mtime anywhere in the tree, not
                # the task directory's own mtime (P3): a directory's mtime
                # only changes when an entry is added/removed/renamed
                # directly inside it, NOT when a file three levels down is
                # edited - live work under an old task directory would
                # otherwise be misclassified as stale and deleted.
                newest, nerr = _newest_mtime_in_tree(task_dir)
                if nerr:
                    access_errors.append(nerr)
                if newest is not None and newest < cutoff:
                    add(task_dir, "scratch-stale")

    return out, access_errors


def _newest_mtime_in_tree(root: Path) -> tuple[float | None, Path | None]:
    """The newest mtime of `root` itself or anything nested under it.
    Returns (None, error_path) if any nested directory could not be
    listed, rather than silently under-counting staleness."""
    try:
        newest = os.lstat(root).st_mtime
    except OSError:
        return None, root
    entries, err = _safe_iterdir(root)
    if err is not None:
        return None, err
    for entry in entries:
        if is_link_like(entry):
            try:
                newest = max(newest, os.lstat(entry).st_mtime)
            except OSError:
                pass
            continue
        if entry.is_dir():
            sub_newest, sub_err = _newest_mtime_in_tree(entry)
            if sub_err is not None:
                return None, sub_err
            if sub_newest is not None:
                newest = max(newest, sub_newest)
        else:
            try:
                newest = max(newest, os.lstat(entry).st_mtime)
            except OSError:
                pass
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
    candidates, access_errors = find_candidates(cfg)
    registered = get_registered_worktrees(cfg.repo)
    dirty = [
        w for w in registered
        if w != cfg.repo and w.exists() and is_dirty_worktree(w)
    ]
    foreign = find_foreign_kept(cfg)
    return JanitorReport(
        candidates=candidates, registered_worktrees=registered,
        dirty_worktrees=dirty, foreign_kept=foreign, access_errors=access_errors,
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
        lines.append(f"FAILED to list ({len(report.access_errors)}) - "
                      "not silently skipped, permissions likely need fixing:")
        for p in report.access_errors:
            lines.append(f"  {p}")
    if not apply:
        oldest = sorted(report.candidates, key=lambda c: c.mtime)[:15]
        for c in oldest:
            when = datetime.datetime.fromtimestamp(c.mtime).strftime("%Y-%m-%d")
            lines.append(f"  {when} {c.path}")
        if len(report.candidates) > 15:
            lines.append(f"  ... and {len(report.candidates) - 15} more")
        lines.append("re-run with --apply to preserve dirty worktrees as WIP commits, "
                      "delete, and prune.")
    return "\n".join(lines)


@dataclasses.dataclass
class WipCommitResult:
    message: str
    refused: bool  # True: default branch or detached HEAD - not committed AND not removed


def wip_commit_dirty_worktree(path: Path, *, default_branches: list[str]) -> WipCommitResult:
    """Commit ALL changes (tracked and untracked - P2) as a WIP commit on
    the worktree's own branch. Refuses (no commit, and the caller must not
    remove the directory either) if the worktree is on a default branch OR
    is a detached HEAD (P1): a WIP commit made on a detached HEAD is
    reachable from no ref and becomes an unreachable object the moment the
    directory is removed - recoverable only until the next `git gc`, i.e.
    not really preserved. `--detach` is the ops doc's OWN recommended form
    for a review worktree, so this is not a corner case."""
    branch = worktree_branch(path)
    if branch in default_branches or branch in _NEVER_AUTO_COMMIT_BRANCHES_SENTINELS:
        label = branch or "an unresolvable HEAD"
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {label} (never auto-commit or remove "
                     f"a default branch or a detached HEAD): {path}",
            refused=True,
        )
    _run_git(path, "add", "-A")
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    git = shutil.which("git")
    if git is not None:
        subprocess.run(  # nosec B603 - resolved executable, argv list
            [git, "-C", str(path), "commit", "-q", "-m",
             f"wip: preserve uncommitted worktree state before scratch cleanup ({stamp})"],
            capture_output=True, text=True, check=False,
        )
    return WipCommitResult(message=f"  WIP committed on {branch}: {path}", refused=False)


def _make_tree_writable(path: Path) -> None:
    """Clear the read-only bit recursively before removal (Windows: git
    marks its own `.git/objects/**` blobs read-only, so a plain
    `shutil.rmtree` on a full clone hits PermissionError before it ever
    reaches the escalation branch - reviewer-3's teardown observation).
    Best-effort; a failure here just means the subsequent remove attempt
    may also fail and fall through to escalation as before."""
    try:
        current = os.stat(path, follow_symlinks=False).st_mode
        os.chmod(path, current | stat.S_IWRITE)
    except OSError:
        pass
    if is_link_like(path) or not path.is_dir():
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


def remove_stubborn(path: Path) -> str:
    """Remove `path` (file, directory, or - as the link itself, never its
    target - a symlink/junction), escalating (Windows only) if a plain
    remove fails. Returns one of: 'absent', 'removed', 'removed-after-acl',
    'removed-after-robocopy', 'FAILED'. Never raises."""
    link = is_link_like(path)
    if not link and not path.exists():
        return "absent"
    if link:
        try:
            _rmtree(path)
        except OSError:
            pass
        # A link is never escalated into: the takeover/robocopy branches
        # below operate ON THE TARGET's contents, which is exactly the
        # P6A/P6B mistake (deleting through the link, or mirroring an
        # empty directory INTO the target). A link that resists a plain
        # unlink is reported FAILED, full stop.
        return "removed" if not path.exists() else "FAILED"

    is_dir = path.is_dir()
    try:
        _rmtree(path)
    except OSError:
        pass
    if not path.exists():
        return "removed"
    if platform.system() != "Windows":
        return "FAILED"
    # Windows-only escalation: the reference script's ACL-takeover path for
    # paths created inside a Codex sandbox whose session ACLs deny even a
    # listing (#147 class). Tools resolved to an absolute path (bandit
    # B607); a missing tool just means escalation can't proceed - FAILED,
    # not a crash.
    takeown = _resolve_system_tool("takeown")
    icacls = _resolve_system_tool("icacls")
    if takeown is not None:
        takeown_args = [takeown, "/F", str(path)] + (["/R", "/D", "Y"] if is_dir else [])
        subprocess.run(takeown_args, capture_output=True, check=False)  # nosec B603
    if icacls is not None:
        icacls_args = [icacls, str(path), "/grant",
                        "*S-1-3-4:(OI)(CI)F" if is_dir else "*S-1-3-4:F"]
        if is_dir:
            icacls_args += ["/T"]
        icacls_args += ["/C", "/Q"]
        subprocess.run(icacls_args, capture_output=True, check=False)  # nosec B603
    try:
        _rmtree(path)
    except OSError:
        pass
    if not path.exists():
        return "removed-after-acl"
    if not is_dir:
        return "FAILED"  # the robocopy-mirror trick below only applies to directories
    robocopy = _resolve_system_tool("robocopy")
    if robocopy is None:
        return "FAILED"
    empty = Path(tempfile.gettempdir()) / f"empty-{uuid.uuid4().hex}"
    empty.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # nosec B603 - resolved executable, argv list
        [robocopy, str(empty), str(path), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
         "/R:0", "/W:0"],
        capture_output=True, check=False,
    )
    shutil.rmtree(empty, ignore_errors=True)
    try:
        _rmtree(path)
    except OSError:
        pass
    return "removed-after-robocopy" if not path.exists() else "FAILED"


def apply(cfg: JanitorConfig, report: JanitorReport) -> str:
    """Run apply mode: WIP-commit dirty worktrees, remove candidates, prune.
    Returns the report text (same shape as report mode, plus the apply
    summary)."""
    lines = [format_report(report, cfg, apply=True)]
    refused: set[Path] = set()
    for w in report.dirty_worktrees:
        result = wip_commit_dirty_worktree(w, default_branches=cfg.default_branches)
        lines.append(result.message)
        if result.refused:
            refused.add(w)

    def is_refused(path: Path) -> bool:
        # A refused worktree's own directory, or anything under it, is never
        # removed - uncommitted changes on a default branch or a detached
        # HEAD must survive.
        return any(path == r or r in path.parents for r in refused)

    summary: dict[str, int] = {}
    failed: list[Path] = []
    for c in report.candidates:
        if is_refused(c.path):
            summary["refused"] = summary.get("refused", 0) + 1
            continue
        result = remove_stubborn(c.path)
        summary[result] = summary.get(result, 0) + 1
        if result == "FAILED":
            failed.append(c.path)

    _run_git(cfg.repo, "worktree", "prune")

    lines.append("removed summary:")
    for key, count in summary.items():
        lines.append(f"  {key}: {count}")
    lines.append(f"registered worktrees after prune: {len(get_registered_worktrees(cfg.repo))}")
    if failed:
        lines.append(f"FAILED ({len(failed)})" + (
            " - " + _ELEVATED_HINT_WINDOWS if platform.system() == "Windows" else ""
        ))
        for f in failed:
            lines.append(f"  {f}")
    return "\n".join(lines)
