"""B5 fix round 1 (dev-5 cold read M1-M3; M4 lives in test_envelope_snapshot.py):
local git ancestry facts for the work board's Done lane, hardened to
docs/DESIGN-work-board.md section 4.

Uses REAL throwaway git repos (skipped if git is unavailable), matching the
project's existing convention (see tests/conftest.py's comprehension_privacy_root).
"""
import shutil
import subprocess

import pytest

from agenttalk.work_board_integration import (
    DEFAULT_ALIAS,
    GitAncestryCache,
    TARGET_REFRESH_INTERVAL_SECONDS,
    _run_git,
    integration_fact_for,
    resolve_alias,
)

ITEM = "work-board-done-lane"


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


def _cfg(**work_repos):
    return {"work_repos": work_repos}


# ------------------------------------------------------------------- M3: hardened git runner

def test_disallowed_subcommand_is_rejected(repo):
    with pytest.raises(ValueError, match="subcommand not allowed"):
        _run_git(repo, "fetch", "origin")


def test_disallowed_flag_is_rejected(repo):
    with pytest.raises(ValueError, match="flag not allowed"):
        _run_git(repo, "rev-parse", "--upload-pack=evil")


def test_unsafe_positional_argument_is_rejected(repo):
    with pytest.raises(ValueError, match="unsafe git argument"):
        _run_git(repo, "cat-file", "-e", "; rm -rf /")


def test_hardened_env_drops_inherited_git_env_and_disables_prompts(repo, monkeypatch):
    _commit(repo, "a")
    monkeypatch.setenv("GIT_DIR", "/somewhere/hostile")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/somewhere/else")
    # rev-parse --git-dir would resolve to the hostile GIT_DIR if it leaked through -
    # it must report THIS repo's own .git instead.
    result = _run_git(repo, "rev-parse", "--git-dir")
    assert result is not None and result.returncode == 0
    assert "somewhere" not in result.stdout


def test_replacement_objects_do_not_fabricate_integration(repo):
    """M3 (reproduced by dev-5): replacing an object that master's history actually
    walks through - substituting a FAKE commit that claims the unmerged topic tip as
    its own parent - makes plain git report the topic as merged (verified below: exit
    0 with plain git, exit 1 with --no-replace-objects/GIT_NO_REPLACE_OBJECTS=1). A
    replace targeting the topic tip itself would NOT do this: merge-base compares OIDs
    reached while walking master, never reads the topic tip's own (replaceable)
    content to perform that comparison, so the substitution has to sit somewhere on
    master's real walked path instead - here, master's own tip."""
    base = _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic_tip = _commit(repo, "b")
    _git(repo, "checkout", "-q", "master")
    tree = _git(repo, "rev-parse", f"{base}^{{tree}}").stdout.strip()
    fake = _git(repo, "commit-tree", tree, "-p", topic_tip, "-m", "fake parent").stdout.strip()
    _git(repo, "replace", base, fake)  # master's real tip now (falsely) has topic_tip as a parent
    cache = GitAncestryCache()
    ok, why = cache.is_ancestor(repo, topic_tip, "master")
    assert ok is False, "the replacement object must not fabricate integration"


def test_no_network_or_helper_subcommand_can_ever_be_invoked(repo):
    """Structural guarantee, not just a spot check: the allowlist rejects EVERY
    subcommand except rev-parse/merge-base/cat-file outright, so a caller can never
    reach fetch/pull/clone/remote or a credential helper."""
    for banned in ("fetch", "pull", "clone", "remote", "push", "ls-remote", "submodule"):
        with pytest.raises(ValueError, match="subcommand not allowed"):
            _run_git(repo, banned)


# --------------------------------------------------------------------- M2: alias binding

def test_resolve_alias_unmapped_is_unproved(repo):
    resolved, reason = resolve_alias(_cfg(), "agenttalk")
    assert resolved is None and "not an approved" in reason


def test_resolve_alias_valid_entry_resolves(repo):
    _commit(repo, "a")
    cfg = _cfg(agenttalk={"path": str(repo), "targets": ["master"]})
    resolved, targets = resolve_alias(cfg, "agenttalk")
    assert resolved == repo.resolve() and targets == ["master"]


def test_resolve_alias_rejects_a_relative_path(repo):
    cfg = _cfg(agenttalk={"path": "relative/path", "targets": ["master"]})
    resolved, reason = resolve_alias(cfg, "agenttalk")
    assert resolved is None and "absolute" in reason


def test_resolve_alias_rejects_dotdot_escape(repo):
    cfg = _cfg(agenttalk={"path": str(repo / ".." / "escaped"), "targets": ["master"]})
    resolved, reason = resolve_alias(cfg, "agenttalk")
    assert resolved is None and "absolute" in reason


def test_resolve_alias_rejects_a_symlinked_path(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _init(real)
    _commit(real, "a")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires elevated privileges on this platform")
    cfg = _cfg(agenttalk={"path": str(link), "targets": ["master"]})
    resolved, reason = resolve_alias(cfg, "agenttalk")
    assert resolved is None and "symlink" in reason


def test_resolve_alias_rejects_a_missing_path(tmp_path):
    cfg = _cfg(agenttalk={"path": str(tmp_path / "nope"), "targets": ["master"]})
    resolved, reason = resolve_alias(cfg, "agenttalk")
    assert resolved is None and "does not exist" in reason


def test_resolve_alias_rejects_a_non_repo_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = _cfg(agenttalk={"path": str(plain), "targets": ["master"]})
    resolved, reason = resolve_alias(cfg, "agenttalk")
    assert resolved is None and "not a local git repository" in reason


def test_resolve_alias_rejects_a_malformed_targets_list(repo):
    cfg = _cfg(agenttalk={"path": str(repo), "targets": []})
    resolved, reason = resolve_alias(cfg, "agenttalk")
    assert resolved is None and "approved targets" in reason
    cfg2 = _cfg(agenttalk={"path": str(repo), "targets": ["-oops"]})
    resolved2, reason2 = resolve_alias(cfg2, "agenttalk")
    assert resolved2 is None and "approved targets" in reason2


def test_default_alias_only_applies_when_explicitly_configured(repo):
    resolved, reason = resolve_alias(_cfg(), DEFAULT_ALIAS)
    assert resolved is None and "no default work_repo configured" in reason
    _commit(repo, "a")
    cfg = _cfg(**{DEFAULT_ALIAS: {"path": str(repo), "targets": ["master"]}})
    resolved2, targets2 = resolve_alias(cfg, DEFAULT_ALIAS)
    assert resolved2 == repo.resolve() and targets2 == ["master"]


# ----------------------------------------------------------- M1: cache invalidation/recovery

def test_merge_rewind_and_recovery_in_the_same_cache(repo):
    """M1: bind results to the resolved target OID, not the (head, target-name) pair for the
    service's whole lifetime - a merge, a rewind and a re-merge must each be observed, never
    stuck on the first answer. TARGET_REFRESH_INTERVAL_SECONDS gates re-resolution, so the
    clock is advanced past it before each check that must see new history."""
    now = [0.0]
    cache = GitAncestryCache(clock=lambda: now[0])
    base = _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic_tip = _commit(repo, "b")
    _git(repo, "checkout", "-q", "master")

    assert cache.is_ancestor(repo, topic_tip, "master") == (False, None)  # before the merge

    now[0] += TARGET_REFRESH_INTERVAL_SECONDS + 1
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "topic")
    assert cache.is_ancestor(repo, topic_tip, "master")[0] is True  # recovers after the merge

    now[0] += TARGET_REFRESH_INTERVAL_SECONDS + 1
    _git(repo, "update-ref", "refs/heads/master", base)  # a rewind past the merge
    assert cache.is_ancestor(repo, topic_tip, "master") == (False, None)  # false again, not stuck True

    now[0] += TARGET_REFRESH_INTERVAL_SECONDS + 1
    _git(repo, "update-ref", "refs/heads/master", "master@{1}")  # move forward again (redo the merge)
    assert cache.is_ancestor(repo, topic_tip, "master")[0] is True  # recovers a second time


def test_target_resolution_is_not_refreshed_within_the_freshness_window(repo):
    now = [0.0]
    cache = GitAncestryCache(clock=lambda: now[0])
    _commit(repo, "a")
    _git(repo, "checkout", "-q", "-b", "topic")
    topic_tip = _commit(repo, "b")
    _git(repo, "checkout", "-q", "master")
    assert cache.is_ancestor(repo, topic_tip, "master") == (False, None)
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "topic")
    now[0] += 1.0  # well inside TARGET_REFRESH_INTERVAL_SECONDS
    # the target OID is still the pre-merge one from cache's point of view - correctly
    # stale-but-bounded, not an immediate (unbounded-git-call) recheck.
    assert cache.is_ancestor(repo, topic_tip, "master") == (False, None)


def test_a_diagnostic_result_is_retried_after_the_freshness_window_not_cached_forever(repo):
    now = [0.0]
    calls = []

    def flaky_run_git(repo_path, subcommand, *args, timeout=None):
        calls.append(subcommand)
        if subcommand == "cat-file":
            return None  # simulated transient git failure
        return _run_git(repo_path, subcommand, *args, timeout=timeout)

    cache = GitAncestryCache(run_git=flaky_run_git, clock=lambda: now[0])
    head = _commit(repo, "a")
    ok, why = cache.is_ancestor(repo, head, "master")
    assert (ok, why) == (False, "git unavailable or timed out")
    first_cat_file_calls = calls.count("cat-file")
    ok2, why2 = cache.is_ancestor(repo, head, "master")
    assert (ok2, why2) == (False, "git unavailable or timed out")
    assert calls.count("cat-file") == first_cat_file_calls  # NOT retried yet - still within the window
    now[0] += TARGET_REFRESH_INTERVAL_SECONDS + 1
    cache.is_ancestor(repo, head, "master")
    assert calls.count("cat-file") > first_cat_file_calls  # retried once the window elapsed


def test_missing_object_is_a_diagnostic_not_an_exception(repo):
    _commit(repo, "a")
    cache = GitAncestryCache()
    ok, why = cache.is_ancestor(repo, "f" * 40, "master")
    assert ok is False
    assert "missing or shallow" in why


def test_git_missing_or_timeout_degrades_cleanly_never_raises(repo):
    _commit(repo, "a")
    cache = GitAncestryCache(run_git=lambda *a, **k: None)
    # git is unavailable even for resolving the target ref itself - a clean diagnostic,
    # never an exception, and never treated as integrated.
    ok, why = cache.is_ancestor(repo, "a" * 40, "master")
    assert ok is False and why


# --------------------------------------------------------------- integration_fact_for / M2 e2e

def test_integration_fact_for_skips_items_with_no_candidate():
    assert integration_fact_for({"work_item": "x", "candidate": None}, _cfg(), GitAncestryCache()) is None


def test_integration_fact_for_true_for_a_merged_candidate_via_its_declared_alias(repo):
    head = _commit(repo, "a")
    cfg = _cfg(agenttalk={"path": str(repo), "targets": ["master"]})
    item = {"work_item": ITEM, "candidate": head, "work_repo": "agenttalk", "work_target": None}
    key, ok, why = integration_fact_for(item, cfg, GitAncestryCache())
    assert (key, ok, why) == ((ITEM, head), True, None)


def test_integration_fact_for_unmapped_alias_is_unproved_never_falls_back_to_root(repo, tmp_path):
    """M2: the item declares an alias that ISN'T configured - it must be unproved, never
    silently checked against some other (e.g. the process's own) root repo."""
    head = _commit(repo, "a")
    cfg = _cfg(**{})  # nothing configured at all
    item = {"work_item": ITEM, "candidate": head, "work_repo": "some-other-alias", "work_target": None}
    key, ok, why = integration_fact_for(item, cfg, GitAncestryCache())
    assert key == (ITEM, head) and ok is False and "not an approved" in why


def test_integration_fact_for_two_checkouts_the_root_would_wrongly_say_done(tmp_path):
    """M2's own required test: TWO real checkouts. `checked_in` genuinely has the candidate
    merged; `wrong_root` (standing in for store.root, which the pre-fix code always used
    regardless of the item's declared work_repo) does NOT. An item declaring the alias that
    maps to `checked_in` must say integrated; the SAME head checked against `wrong_root`
    (what the old bypass would have done) must not - proving the alias binding, not the
    root, decides the answer."""
    checked_in = tmp_path / "checked-in"
    checked_in.mkdir()
    _init(checked_in)
    head = _commit(checked_in, "a")

    wrong_root = tmp_path / "wrong-root"
    wrong_root.mkdir()
    _init(wrong_root)
    _commit(wrong_root, "unrelated")  # never contains `head` at all

    cfg = _cfg(agenttalk={"path": str(checked_in), "targets": ["master"]})
    item = {"work_item": ITEM, "candidate": head, "work_repo": "agenttalk", "work_target": None}
    key, ok, why = integration_fact_for(item, cfg, GitAncestryCache())
    assert (ok, why) == (True, None)

    # What the OLD (pre-fix) code did: check the candidate against store.root regardless of
    # work_repo. Reproduced directly here against `wrong_root` to show it would have said
    # "not integrated" for the WRONG reason (or, with a coincidentally-shared history, could
    # just as easily have wrongly said Done) - the alias binding is what makes the answer
    # depend on the DECLARED repo, not whichever root happens to be running the server.
    ok_wrong, _ = GitAncestryCache().is_ancestor(wrong_root, head, "master")
    assert ok_wrong is False


def test_declared_target_must_be_one_of_the_alias_own_approved_targets(repo):
    """M2 'ambiguous binding': a work_target the alias never approved must not be honored,
    even if it happens to resolve as a real ref in that repo."""
    head = _commit(repo, "a")
    _git(repo, "branch", "unapproved")
    cfg = _cfg(agenttalk={"path": str(repo), "targets": ["master"]})
    item = {"work_item": ITEM, "candidate": head, "work_repo": "agenttalk", "work_target": "unapproved"}
    key, ok, why = integration_fact_for(item, cfg, GitAncestryCache())
    assert ok is False and "not an approved target" in why


def test_default_alias_used_only_when_no_work_repo_declared_and_configured(repo):
    head = _commit(repo, "a")
    cfg = _cfg(**{DEFAULT_ALIAS: {"path": str(repo), "targets": ["master"]}})
    item = {"work_item": ITEM, "candidate": head, "work_repo": None, "work_target": None}
    key, ok, why = integration_fact_for(item, cfg, GitAncestryCache())
    assert (ok, why) == (True, None)


def test_no_default_alias_configured_means_unproved_not_root_fallback(repo):
    head = _commit(repo, "a")
    item = {"work_item": ITEM, "candidate": head, "work_repo": None, "work_target": None}
    key, ok, why = integration_fact_for(item, _cfg(), GitAncestryCache())
    assert ok is False and "no default work_repo configured" in why
