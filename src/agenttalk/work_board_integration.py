"""Local git ancestry facts for the work board's Done lane (B5), hardened to the
APPROVED design (docs/DESIGN-work-board.md section 4) after fix round 1 (dev-5 cold
read, M1-M4):

- `work_repo` selects an operator-approved LOCAL ALIAS, mapped in project config
  (`work_repos`) to a canonical checkout and its approved target refs - never a path
  taken from message metadata, never an implicit root default. An unmapped alias is
  UNPROVED, never "borrow the root's evidence" (M2).
- The observer runs ONLY `rev-parse`, `merge-base --is-ancestor` and `cat-file`:
  shell-free, validated arguments, a hardened environment (every inherited `GIT_*`
  dropped, then `GIT_NO_LAZY_FETCH=1`/`GIT_NO_REPLACE_OBJECTS=1` set, prompts
  disabled, `-c core.fsmonitor=false`, `--no-replace-objects`), reading canonical
  objects only - never worktree files, hooks, a shell, or the network (M3).
- Every ancestry result is bound to the resolved TARGET OID and the alias's own
  resolved checkout path, re-resolved at most every `TARGET_REFRESH_INTERVAL_SECONDS`;
  a moved branch, a repointed alias, or a missing/shallow object invalidates the
  cached fact rather than reusing a stale one. A diagnostic result is retried after
  the same freshness window, never cached forever (M1).
- Bounded to one probe at a time, `GIT_PROBE_TIMEOUT_SECONDS` per probe; the caller
  (envelope_snapshot.py) budgets the whole candidate set and checks for shutdown
  between items, so a slow or hung git can never block the shared snapshot's own
  publication (M4).
"""
import os
import re
import subprocess  # nosec B404
import time
from pathlib import Path

GIT_PROBE_TIMEOUT_SECONDS = 2.0
TARGET_REFRESH_INTERVAL_SECONDS = 10.0

_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,199}$")
_OID_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_ALLOWED_SUBCOMMANDS = {"rev-parse", "merge-base", "cat-file"}
_ALLOWED_FLAGS = {"--is-ancestor", "--verify", "--quiet", "--git-dir", "-e"}

#: reserved work_repos key selecting the checkout used for an item with no declared
#: work_repo - never active unless the operator configures it explicitly (design:
#: "Root repo defaults only when configured; otherwise unknown").
DEFAULT_ALIAS = ""


def _hardened_env():
    """Drop every inherited GIT_* variable - one could redirect the repo/object store
    or object database - then set only the design's own explicit safety flags."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _validate_arg(arg):
    if arg.startswith("-"):
        if arg not in _ALLOWED_FLAGS:
            raise ValueError(f"git flag not allowed: {arg!r}")
        return
    if not (_REF_PATTERN.fullmatch(arg) or _OID_PATTERN.fullmatch(arg)):
        raise ValueError(f"unsafe git argument: {arg!r}")


def _run_git(repo_path, subcommand, *args, timeout=GIT_PROBE_TIMEOUT_SECONDS):
    """Shell-free, allowlisted (rev-parse/merge-base/cat-file only), validated-argument
    git invocation with a hardened environment - see the module docstring (M3). Never
    raises: a rejected subcommand/argument is a programming error (ValueError, the
    caller's bug to fix), while any git/OS failure (missing binary, timeout, non-repo)
    returns None for the caller to treat as "not integrated" plus a diagnostic."""
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        raise ValueError(f"git subcommand not allowed: {subcommand!r}")
    for arg in args:
        _validate_arg(arg)
    argv = ["git", "--no-replace-objects", "-c", "core.fsmonitor=false",
            "-C", str(repo_path), subcommand, *args]
    try:
        return subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            argv, check=False, capture_output=True, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            env=_hardened_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _has_symlink_component(path):
    p = path
    while True:
        try:
            if p.is_symlink():
                return True
        except OSError:
            return True  # unreadable is not provably safe either
        parent = p.parent
        if parent == p:
            return False
        p = parent


def resolve_alias(cfg, alias, *, run_git=_run_git):
    """An operator-approved LOCAL alias, mapped in project config's `work_repos` to a
    canonical checkout and its approved target refs (design section 4). Returns
    `(resolved_path, [approved_targets])` on success, or `(None, diagnostic)` - an
    unmapped alias, an invalid path (relative, escaping, symlinked, missing, not a
    repo) or a malformed target list is UNPROVED, never a silent fallback to
    anything else. Never raises."""
    repos = cfg.get("work_repos")
    if not isinstance(repos, dict):
        return None, "no work_repos configured"
    entry = repos.get(alias)
    if alias == DEFAULT_ALIAS and entry is None:
        return None, "no default work_repo configured"
    if not isinstance(entry, dict):
        return None, f"alias {alias!r} is not an approved work_repos entry"
    raw_path, targets = entry.get("path"), entry.get("targets")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, f"alias {alias!r} has no configured path"
    if (not isinstance(targets, list) or not targets
            or not all(isinstance(t, str) and _REF_PATTERN.fullmatch(t) for t in targets)):
        return None, f"alias {alias!r} has no valid approved targets"
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts:
        return None, f"alias {alias!r} path must be absolute with no '..' segments"
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None, f"alias {alias!r} path does not exist"
    if not resolved.is_dir():
        return None, f"alias {alias!r} path is not a directory"
    if _has_symlink_component(path):
        return None, f"alias {alias!r} path contains a symlink"
    check = run_git(resolved, "rev-parse", "--git-dir")
    if check is None or check.returncode != 0:
        return None, f"alias {alias!r} is not a local git repository"
    return resolved, list(targets)


class GitAncestryCache:
    """Memoizes ancestry facts, bound to the resolved target OID and repo path - never
    just a (head, target-name) pair for the service's whole lifetime (M1). A target
    ref is re-resolved at most every TARGET_REFRESH_INTERVAL_SECONDS; a result carrying
    a diagnostic (a real failure, not an ordinary "not yet merged") is retried after
    the same window rather than cached forever."""

    def __init__(self, *, run_git=_run_git, clock=time.monotonic):
        self._run_git = run_git
        self._clock = clock
        self._targets = {}   # (repo_path, ref_name) -> (resolved_at, oid|None)
        self._results = {}   # (repo_path, head, target_oid) -> (checked_at, ok, diagnostic|None)

    def _resolve_target(self, repo_path, ref_name):
        key = (str(repo_path), ref_name)
        now = self._clock()
        cached = self._targets.get(key)
        if cached is not None and now - cached[0] < TARGET_REFRESH_INTERVAL_SECONDS:
            return cached[1]
        result = self._run_git(repo_path, "rev-parse", "--verify", "--quiet", ref_name)
        oid = result.stdout.strip() if result is not None and result.returncode == 0 else None
        self._targets[key] = (now, oid)
        return oid

    def is_ancestor(self, repo_path, head, ref_name):
        oid = self._resolve_target(repo_path, ref_name)
        if oid is None:
            return False, f"target ref {ref_name!r} did not resolve"
        key = (str(repo_path), head, oid)
        cached = self._results.get(key)
        now = self._clock()
        if cached is not None and (cached[2] is None or now - cached[0] < TARGET_REFRESH_INTERVAL_SECONDS):
            return cached[1], cached[2]
        ok, why = self._check(repo_path, head, oid)
        self._results[key] = (now, ok, why)
        return ok, why

    def _check(self, repo_path, head, target_oid):
        exists = self._run_git(repo_path, "cat-file", "-e", head)
        if exists is None:
            return False, "git unavailable or timed out"
        if exists.returncode != 0:
            return False, "candidate commit not found locally (missing or shallow object)"
        result = self._run_git(repo_path, "merge-base", "--is-ancestor", head, target_oid)
        if result is None:
            return False, "git unavailable or timed out"
        if result.returncode == 0:
            return True, None
        if result.returncode == 1:
            return False, None  # a clean, known "not an ancestor yet" - not a diagnostic
        return False, (result.stderr or f"git exited {result.returncode}").strip()[:200]


def integration_fact_for(item, cfg, cache, *, run_git=_run_git):
    """`((work_item, head), ok, diagnostic|None)` for one reduced item, or `None` when
    it has no candidate head at all (nothing to check). Resolves the item's declared
    work_repo alias (or the reserved default alias when none is declared) and its
    declared work_target (must be one of the alias's own approved targets - an
    unapproved or unmapped combination is UNPROVED, never silently accepted)."""
    head = item.get("candidate")
    if not head:
        return None
    key = (item["work_item"], head)
    alias = item.get("work_repo") or DEFAULT_ALIAS
    repo_path, targets_or_reason = resolve_alias(cfg, alias, run_git=run_git)
    if repo_path is None:
        return key, False, targets_or_reason
    declared_target = item.get("work_target")
    if declared_target is not None and declared_target not in targets_or_reason:
        return key, False, (f"declared work_target {declared_target!r} is not an approved "
                            f"target for alias {alias!r}")
    target = declared_target or targets_or_reason[0]
    ok, why = cache.is_ancestor(repo_path, head, target)
    return key, ok, why
