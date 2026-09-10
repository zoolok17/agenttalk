"""Cross-platform scratch janitor (#148) - a Python port of the validated
`tools/cleanup-scratch.ps1` reference implementation.

Report mode (default) lists scratch candidates and dirty registered
worktrees without touching anything. Apply mode WIP-commits dirty
worktrees on their own branch (never the default branch), removes the
allow-listed candidates, and prunes stale worktree registrations. Never
touches `.agenttalk/`, tracked files, or a configured "foreign" folder
under the temp root - those are reported and kept.

Config (optional, `.agenttalk/config.json`, key `"scratch"`, long form):

    {
      "scratch": {
        "root": "...",                    # see scratch.py
        "keep_days": 3,
        "repo_dir_families": [...],       # fnmatch globs, repo root, directories
        "repo_file_families": [...],      # fnmatch globs, repo root, files
        "tmp_root": "...",                # default: the OS temp dir
        "tmp_families": [...],            # fnmatch globs under tmp_root
        "foreign": [...],                 # exact names under tmp_root: report, never remove
        "default_branches": ["master", "main"]
      }
    }

Every list has a built-in default (below) used when the key is absent, so
an unconfigured project still gets sane behaviour.
"""

from __future__ import annotations

import dataclasses
import datetime
import fnmatch
import platform
import shutil
import subprocess
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
DEFAULT_TMP_FAMILIES = [
    "pytest-*", "review*", "reply-q*", "rq-*", "qa-*", "q-*", "race-*",
    "adv-*", "run-*", "wt-*", "cold*", "mut*", "lane-*", "sub*", "probe-*",
    "gate-*", "wf-*",
]
# Never removed regardless of family match, even if a candidate happens to
# collide with one of these names.
_ALWAYS_EXCLUDED_NAMES = {".agenttalk", ".claude", "docs", "tools"}
_ALWAYS_EXCLUDED_PREFIXES = ("launch-", "MANUAL-", ".wt-")

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
        return cls(
            repo=repo,
            scratch_root=resolve_scratch_root(repo),
            keep_days=resolve_keep_days(repo),
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


def _is_excluded_name(name: str) -> bool:
    if name in _ALWAYS_EXCLUDED_NAMES:
        return True
    return any(name.startswith(p) for p in _ALWAYS_EXCLUDED_PREFIXES)


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    return result.stdout


def get_registered_worktrees(repo: Path) -> list[Path]:
    out = _run_git(repo, "worktree", "list", "--porcelain")
    worktrees = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            worktrees.append(Path(line[len("worktree "):]).resolve())
    return worktrees


def is_dirty_worktree(path: Path) -> bool:
    """True if the worktree has uncommitted changes to TRACKED files.
    Untracked files (`??`) don't count - only tracked changes get a WIP
    commit; an all-untracked worktree is not "dirty" in this sense."""
    if not (path / ".git").exists():
        return False
    out = _run_git(path, "status", "--porcelain")
    for line in out.splitlines():
        if line and not line.startswith("??"):
            return True
    return False


def worktree_branch(path: Path) -> str | None:
    out = _run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return out or None


def find_candidates(cfg: JanitorConfig) -> list[Candidate]:
    seen: set[Path] = set()
    out: list[Candidate] = []

    def add(path: Path, reason: str) -> None:
        resolved = path.resolve()
        if resolved in seen or _is_excluded_name(resolved.name):
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        seen.add(resolved)
        out.append(Candidate(path=resolved, mtime=mtime, reason=reason))

    if cfg.repo.is_dir():
        for entry in cfg.repo.iterdir():
            if entry.is_dir() and _matches_any(entry.name, cfg.repo_dir_families):
                add(entry, "repo-dir")
            elif entry.is_file() and _matches_any(entry.name, cfg.repo_file_families):
                add(entry, "repo-file")

    worktrees_dir = cfg.repo / ".worktrees"
    if worktrees_dir.is_dir():
        for entry in worktrees_dir.iterdir():
            if entry.is_dir():
                add(entry, "worktrees-dir")

    if cfg.tmp_root.is_dir():
        for entry in cfg.tmp_root.iterdir():
            if entry.name in cfg.foreign:
                continue
            if _matches_any(entry.name, cfg.tmp_families):
                add(entry, "tmp")

    if cfg.scratch_root.is_dir():
        cutoff = datetime.datetime.now().timestamp() - cfg.keep_days * 86400
        for agent_dir in cfg.scratch_root.iterdir():
            if not agent_dir.is_dir():
                continue
            for task_dir in agent_dir.iterdir():
                if task_dir.is_dir() and task_dir.stat().st_mtime < cutoff:
                    add(task_dir, "scratch-stale")

    return out


def find_foreign_kept(cfg: JanitorConfig) -> list[Path]:
    if not cfg.tmp_root.is_dir():
        return []
    return [
        (cfg.tmp_root / name).resolve()
        for name in cfg.foreign
        if (cfg.tmp_root / name).is_dir()
    ]


def build_report(cfg: JanitorConfig) -> JanitorReport:
    candidates = find_candidates(cfg)
    registered = get_registered_worktrees(cfg.repo)
    dirty = [
        w for w in registered
        if w != cfg.repo and w.exists() and is_dirty_worktree(w)
    ]
    foreign = find_foreign_kept(cfg)
    return JanitorReport(
        candidates=candidates, registered_worktrees=registered,
        dirty_worktrees=dirty, foreign_kept=foreign,
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
    refused: bool  # True: on a default branch - not committed AND not removed


def wip_commit_dirty_worktree(path: Path, *, default_branches: list[str]) -> WipCommitResult:
    """Commit tracked changes as a WIP commit on the worktree's own branch.
    Refuses (no commit, and the caller must not remove the directory either
    - uncommitted changes on a default branch are never destroyed) if the
    worktree is on a default branch."""
    branch = worktree_branch(path)
    if branch in default_branches:
        return WipCommitResult(
            message=f"  REFUSED dirty worktree on {branch} (never auto-commit or remove "
                     f"the default branch): {path}",
            refused=True,
        )
    _run_git(path, "add", "-u")
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m",
         f"wip: preserve uncommitted worktree state before scratch cleanup ({stamp})"],
        capture_output=True, text=True, check=False,
    )
    return WipCommitResult(message=f"  WIP committed on {branch}: {path}", refused=False)


def _rmtree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def remove_stubborn(path: Path) -> str:
    """Remove `path` (file or directory), escalating (Windows only) if a
    plain remove fails. Returns one of: 'absent', 'removed',
    'removed-after-acl', 'removed-after-robocopy', 'FAILED'. Never raises."""
    if not path.exists():
        return "absent"
    is_dir = path.is_dir() and not path.is_symlink()
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
    # listing (#147 class).
    takeown_args = ["takeown", "/F", str(path)] + (["/R", "/D", "Y"] if is_dir else [])
    subprocess.run(takeown_args, capture_output=True, check=False)
    icacls_args = ["icacls", str(path), "/grant", "*S-1-3-4:F" if not is_dir else "*S-1-3-4:(OI)(CI)F"]
    if is_dir:
        icacls_args += ["/T"]
    icacls_args += ["/C", "/Q"]
    subprocess.run(icacls_args, capture_output=True, check=False)
    try:
        _rmtree(path)
    except OSError:
        pass
    if not path.exists():
        return "removed-after-acl"
    if not is_dir:
        return "FAILED"  # the robocopy-mirror trick below only applies to directories
    empty = Path(tempfile.gettempdir()) / f"empty-{uuid.uuid4().hex}"
    empty.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["robocopy", str(empty), str(path), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
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
        # removed - uncommitted changes on a default branch must survive.
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
