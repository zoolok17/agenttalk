"""B5: local git ancestry facts for the work board's Done lane.

work_board.reduce(..., integrated=...) only places an item in "done" when
facts["integrated"][(slug, candidate)] is true - nothing ever computed that
fact until now. Read-only, no network: `git merge-base --is-ancestor <head>
<target>`, bounded by a timeout, one subprocess per distinct head via
GitAncestryCache. An unknown commit, a missing git binary or a timeout all
degrade to "not integrated" plus a diagnostic - never an exception.

Uses a REAL throwaway git repo (skipped if git is unavailable), matching the
project's existing convention (see tests/conftest.py's comprehension_privacy_root).
"""
import shutil
import subprocess

import pytest

from agenttalk import work_board as W
from agenttalk.store import Message
from agenttalk.work_board_integration import (
    GitAncestryCache,
    compute_integrated,
    resolve_default_target,
)

LEAD = "claude-agenttalk-lead"
BUILDER = "codex-agenttalk-dev-1"
REVIEWER = "claude-agenttalk-developer-2"
ITEM = "work-board-done-lane"
POLICY = {"no_gates_reason": "CI tracked outside local gates"}


def _git(root, *args):
    return subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _init(root, *, branch="master"):
    if shutil.which("git") is None:
        pytest.skip("git is required for these tests")
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")


def _commit(root, name):
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", name)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _init(tmp_path)
    return tmp_path


# --------------------------------------------------------------- GitAncestryCache

def test_merged_head_is_reported_as_an_ancestor(repo):
    base = _commit(repo, "a")
    _commit(repo, "b")
    cache = GitAncestryCache(repo)
    assert cache.is_ancestor(base, "master") == (True, None)


def test_unmerged_head_on_a_topic_branch_is_not_an_ancestor(repo):
    _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic_tip = _commit(repo, "b")
    _git(repo, "checkout", "-q", "master")
    cache = GitAncestryCache(repo)
    assert cache.is_ancestor(topic_tip, "master") == (False, None)  # a clean "not yet", no diagnostic


def test_unknown_commit_is_not_an_ancestor_with_a_diagnostic(repo):
    _commit(repo, "a")
    cache = GitAncestryCache(repo)
    ok, why = cache.is_ancestor("f" * 40, "master")
    assert ok is False
    assert why  # git's own "unknown revision" stderr, surfaced as a diagnostic


def test_git_missing_or_timeout_degrades_cleanly_never_raises(repo):
    _commit(repo, "a")
    cache = GitAncestryCache(repo, run_git=lambda *a, **k: None)  # simulates OSError/TimeoutExpired
    assert cache.is_ancestor("a" * 40, "master") == (False, "git unavailable or timed out")


def test_cache_runs_at_most_one_subprocess_per_distinct_head(repo):
    head = _commit(repo, "a")
    calls = []

    def counting_run_git(root, *args, timeout=None):
        calls.append(args)
        return subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "-C", str(root), *args], check=False, capture_output=True, text=True)

    cache = GitAncestryCache(repo, run_git=counting_run_git)
    for _ in range(5):
        cache.is_ancestor(head, "master")
    assert len([c for c in calls if c[0] == "merge-base"]) == 1


# --------------------------------------------------------------- resolve_default_target

def test_resolve_default_target_finds_master(repo):
    _commit(repo, "a")
    assert resolve_default_target(repo) == "master"


def test_resolve_default_target_falls_back_to_main_when_master_is_absent(tmp_path):
    _init(tmp_path, branch="main")
    _commit(tmp_path, "a")
    assert resolve_default_target(tmp_path) == "main"


def test_resolve_default_target_is_none_when_neither_exists(tmp_path):
    _init(tmp_path, branch="trunk")
    _commit(tmp_path, "a")
    assert resolve_default_target(tmp_path) is None


# --------------------------------------------------------------- compute_integrated

def test_compute_integrated_skips_items_with_no_candidate_head(repo):
    cache = GitAncestryCache(repo)
    items = [{"work_item": "x", "candidate": None, "work_target": None}]
    integrated, diagnostics = compute_integrated(items, cache=cache, default_target="master")
    assert integrated == {} and diagnostics == {}


def test_compute_integrated_true_for_a_merged_candidate(repo):
    base = _commit(repo, "a")
    cache = GitAncestryCache(repo)
    items = [{"work_item": ITEM, "candidate": base, "work_target": None}]
    integrated, diagnostics = compute_integrated(items, cache=cache, default_target="master")
    assert integrated == {(ITEM, base): True} and diagnostics == {}


def test_compute_integrated_false_for_an_unmerged_candidate(repo):
    _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic_tip = _commit(repo, "b")
    _git(repo, "checkout", "-q", "master")
    cache = GitAncestryCache(repo)
    items = [{"work_item": ITEM, "candidate": topic_tip, "work_target": None}]
    integrated, diagnostics = compute_integrated(items, cache=cache, default_target="master")
    assert integrated == {(ITEM, topic_tip): False} and diagnostics == {}


def test_compute_integrated_uses_the_items_own_declared_target_over_the_default(repo):
    _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "release")
    release_tip = _commit(repo, "b")  # ahead of master, only reachable via "release"
    _git(repo, "checkout", "-q", "master")
    cache = GitAncestryCache(repo)
    items = [{"work_item": ITEM, "candidate": release_tip, "work_target": "release"}]
    integrated, _ = compute_integrated(items, cache=cache, default_target="master")
    assert integrated == {(ITEM, release_tip): True}
    # the SAME head against the default ("master") would have been false - proves the
    # item's own declared target actually won, not the default.
    assert cache.is_ancestor(release_tip, "master") == (False, None)


def test_compute_integrated_degrades_with_a_diagnostic_when_no_target_is_known(repo):
    cache = GitAncestryCache(repo)
    items = [{"work_item": ITEM, "candidate": "a" * 40, "work_target": None}]
    integrated, diagnostics = compute_integrated(items, cache=cache, default_target=None)
    assert integrated == {(ITEM, "a" * 40): False}
    assert diagnostics == {(ITEM, "a" * 40): "no configured or default target branch found"}


# ----------------------------------------------- end-to-end: reducer placement

class Bus:
    """Minimal work-item bus fixture, matching test_work_board_reducer.py's own Bus shape."""

    def __init__(self):
        self.messages = []

    def add(self, sender, to, kind, meta):
        n = len(self.messages) + 1
        message = Message.from_dict({
            "id": f"20260930-1000{n:02d}-{n:06d}-q{n:03d}", "ts": f"2026-09-30T10:00:{n:02d}.000000Z",
            "from": sender, "to": to, "kind": kind, "subject": "s", "body": "b", "meta": meta})
        self.messages.append(message)
        return message

    def task(self, rid, to, stage, sender=LEAD, kind="task", **extra):
        meta = {"request_id": rid, "stage": stage, "work_item": ITEM, **extra}
        return self.add(sender, to, kind, meta)

    def reply(self, opener, status="done", verdict=None):
        kind = "review-result" if opener.kind == "review-request" else "task-response"
        meta = {"in_reply_to": opener.id, "request_id": opener.meta["request_id"], "status": status}
        if verdict is not None:
            meta["verdict"] = verdict
        return self.add(opener.recipient, opener.sender, kind, meta)


def _reviewed_ready_item(bus, head=None):
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    bus.reply(build, verdict="done")
    read = bus.task("tk-read", REVIEWER, "read", **({"work_head": head} if head else {}))
    bus.reply(read, verdict="GO")


def _placed(bus, integrated):
    return next(i for i in W.reduce(bus.messages, lead=LEAD, integrated=integrated)["items"]
                if i["work_item"] == ITEM)


def test_end_to_end_a_merged_candidate_lands_in_done(repo):
    head = _commit(repo, "a")
    bus = Bus()
    _reviewed_ready_item(bus, head)
    cache = GitAncestryCache(repo)
    preliminary = W.reduce(bus.messages, lead=LEAD)
    integrated, _ = compute_integrated(preliminary["items"], cache=cache, default_target="master")
    item = _placed(bus, integrated)
    assert (item["workflow_column"], item["row"]) == ("done", 3)


def test_end_to_end_an_unmerged_candidate_stays_off_done(repo):
    _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic_tip = _commit(repo, "b")
    _git(repo, "checkout", "-q", "master")
    bus = Bus()
    _reviewed_ready_item(bus, topic_tip)
    cache = GitAncestryCache(repo)
    preliminary = W.reduce(bus.messages, lead=LEAD)
    integrated, _ = compute_integrated(preliminary["items"], cache=cache, default_target="master")
    item = _placed(bus, integrated)
    assert item["workflow_column"] != "done"
    assert item["candidate"] == topic_tip


def test_end_to_end_a_missing_head_keeps_the_existing_reason(repo):
    """No work_head declared at all: candidate stays None, and the reducer's own EXISTING
    placement reasoning ("candidate missing" etc.) is unaffected by integration - the lead's
    dispatch practice gap this fix is explicitly NOT responsible for closing."""
    bus = Bus()
    _reviewed_ready_item(bus, head=None)
    cache = GitAncestryCache(repo)
    preliminary = W.reduce(bus.messages, lead=LEAD)
    integrated, diagnostics = compute_integrated(preliminary["items"], cache=cache, default_target="master")
    assert integrated == {} and diagnostics == {}  # no candidate head -> nothing to check
    without_integration = _placed(bus, None)
    with_integration = _placed(bus, integrated)
    assert with_integration["candidate"] is None
    assert with_integration["workflow_column"] != "done"
    # identical reason either way - supplying (empty) integration facts changed nothing here.
    assert with_integration["reason"] == without_integration["reason"]


def test_git_missing_end_to_end_never_raises_and_keeps_the_item_off_done(repo):
    head = _commit(repo, "a")
    bus = Bus()
    _reviewed_ready_item(bus, head)
    cache = GitAncestryCache(repo, run_git=lambda *a, **k: None)  # simulated git-missing/timeout
    preliminary = W.reduce(bus.messages, lead=LEAD)
    integrated, diagnostics = compute_integrated(preliminary["items"], cache=cache, default_target="master")
    assert integrated == {(ITEM, head): False}
    assert diagnostics == {(ITEM, head): "git unavailable or timed out"}
    item = _placed(bus, integrated)
    assert item["workflow_column"] != "done"
