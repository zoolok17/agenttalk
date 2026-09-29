"""Local git ancestry facts for the work board's Done lane (B5).

work_board.reduce(..., integrated=...) only places an item in "done" when
facts["integrated"][(slug, candidate)] is true (design row 3: "a clean Done is
exactly an integrated Ready") - but nothing has ever computed that fact. This
module supplies it with a LOCAL, read-only git ancestry check:
`git merge-base --is-ancestor <head> <target>`, never a fetch, never the
network. An unknown commit, a missing git binary or a timeout all mean "not
integrated" plus a diagnostic - never an exception; the board must still
render.

Bounded and cached: one subprocess per distinct candidate head, memoized for
the life of a GitAncestryCache (the envelope worker's own long-lived object,
so every refresh cycle after the first pays nothing for a head it has already
resolved). The target branch is resolved ONCE per cache (not per head): an
item's own declared `work_target` (work_board's policy field) wins when every
item agrees on one; otherwise the first local branch found among
DEFAULT_TARGET_CANDIDATES ("master", then "main").

Keep the envelope worker the only place that runs this IO (not a polling
handler) - see envelope_snapshot.SnapshotService.refresh().
"""
import subprocess  # nosec B404

GIT_ANCESTRY_TIMEOUT_SECONDS = 3.0
DEFAULT_TARGET_CANDIDATES = ("master", "main")


def _run_git(repo_root, *args, timeout=GIT_ANCESTRY_TIMEOUT_SECONDS):
    try:
        return subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_default_target(repo_root, *, run_git=_run_git, candidates=DEFAULT_TARGET_CANDIDATES):
    """The first candidate branch that actually resolves locally, else None."""
    for name in candidates:
        result = run_git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        if result is not None and result.returncode == 0:
            return name
    return None


class GitAncestryCache:
    """Memoizes is_ancestor(head, target) -> (bool, diagnostic|None) per repo, for the life of
    this object. Construct one per project repo and reuse it across refresh cycles."""

    def __init__(self, repo_root, *, run_git=_run_git):
        self.repo_root = repo_root
        self._run_git = run_git
        self._cache = {}

    def is_ancestor(self, head, target):
        key = (head, target)
        if key not in self._cache:
            self._cache[key] = self._check(head, target)
        return self._cache[key]

    def _check(self, head, target):
        result = self._run_git(self.repo_root, "merge-base", "--is-ancestor", head, target)
        if result is None:
            return False, "git unavailable or timed out"
        if result.returncode == 0:
            return True, None
        if result.returncode == 1:
            return False, None  # a clean, known "not an ancestor yet" - not a diagnostic
        return False, (result.stderr or f"git exited {result.returncode}").strip()[:200]


def compute_integrated(items, *, cache, default_target=None):
    """{(work_item, candidate): is_ancestor} for every item with a candidate head, plus a
    {(work_item, candidate): reason} diagnostics map for anything that came back False for a
    real git problem (never for an ordinary, correctly-not-yet-merged candidate)."""
    integrated, diagnostics = {}, {}
    for item in items:
        head = item.get("candidate")
        if not head:
            continue
        target = item.get("work_target") or default_target
        key = (item["work_item"], head)
        if target is None:
            integrated[key] = False
            diagnostics[key] = "no configured or default target branch found"
            continue
        ok, why = cache.is_ancestor(head, target)
        integrated[key] = ok
        if why:
            diagnostics[key] = why
    return integrated, diagnostics
