"""The ordinary sweep (`janitor --apply`) keeps a registered checkout that a lane, a launch or a supervisor record
still names, by the same activity and occupancy rules a release applies (#858).
Every removal here is inside tmp_path."""

# ruff: noqa: F811  (the imported `estate` fixture is named as an argument by each test)
import contextlib
import json
import os

import pytest

from agenttalk import janitor, lane_occupancy, worktree_release
from agenttalk.cli import main
from agenttalk.store import Store

from test_worktree_release import (  # noqa: F401  (estate is a fixture)
    _finished_lane,
    _resolver_cannot_follow,
    _session_on_lane,
    directory_link,
    _write_record,
    estate,
    git,
    run,
)


def checkout(repo, *parts, branch=None):
    """A registered checkout at `.worktrees/<parts>`, on its own branch."""
    where = repo.joinpath(".worktrees", *parts)
    git(repo, "worktree", "add", "-q", "-b", branch or "lane/" + "-".join(parts), str(where))
    return where


def sweep(cfg):
    report = janitor.build_report(cfg)
    return report, janitor.apply(cfg, report)


def untouched(repo, where, branch):
    """What a kept checkout must still be: there, registered, and its branch where it was."""
    assert (where / "source.txt").exists()
    assert str(where).replace("\\", "/") in git(repo, "worktree", "list", "--porcelain").replace("\\", "/")
    return git(repo, "rev-parse", branch)


@pytest.mark.parametrize(
    "kind", ["active-lane", "pending-lane", "supervisor-state", "store-supervisor", "launch-request"]
)
def test_the_sweep_keeps_a_clean_checkout_that_a_record_still_names(estate, kind):
    """The reproduction: release keeps the checkout, and the sweep used to remove it right afterwards."""
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    before = git(repo, "rev-parse", "lane/l1")
    _write_record(repo, kind, lane)

    code, text = run(cfg, lane)
    assert code == 1 and "KEPT" in text and lane.exists(), text

    report = janitor.build_report(cfg)
    preview = janitor.format_report(report, cfg, apply=False)
    assert f"REFUSED (kept) {lane}" in preview, preview
    applied = janitor.apply(cfg, report)

    assert f"REFUSED (kept) {lane}" in applied, applied
    assert "refused: 1" in applied and "FAILED" not in applied and not report.apply_failed
    assert git(repo, "rev-parse", "lane/l1") == before == untouched(repo, lane, "lane/l1")
    assert git(repo, "log", "-1", "--format=%s", "lane/l1") == "base"  # no work-in-progress commit


def test_the_sweep_still_removes_an_unrelated_clean_checkout(estate):
    """Both directions: an active lane keeps its own checkout, and another one in the same sweep still goes."""
    repo, wt, cfg = estate
    busy, free = checkout(repo, "busy"), checkout(repo, "free")
    _write_record(repo, "active-lane", busy)

    report, text = sweep(cfg)

    assert busy.exists() and not free.exists(), text
    assert f"REFUSED (kept) {busy}" in text and "removed: 1" in text and "refused: 1" in text, text
    assert git(repo, "rev-parse", "lane/free")  # its branch is kept, as always


def test_the_sweep_does_not_commit_the_unsaved_work_of_an_active_checkout(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    (lane / "source.txt").write_text("unsaved\n")
    _write_record(repo, "active-lane", lane)
    before = git(repo, "rev-parse", "lane/l1")

    report, text = sweep(cfg)

    assert "WIP committed" not in text and f"REFUSED (kept) {lane}" in text, text
    assert git(repo, "rev-parse", "lane/l1") == before
    assert (lane / "source.txt").read_text() == "unsaved\n"
    assert git(lane, "status", "--porcelain")  # still dirty: nothing was committed or cleaned


def test_a_parent_of_two_checkouts_is_kept_whole_when_one_is_active(estate):
    repo, wt, cfg = estate
    first, second = checkout(repo, "group", "a"), checkout(repo, "group", "b")
    (first / "source.txt").write_text("unsaved\n")  # the one that is not active must not get a commit either
    _write_record(repo, "active-lane", second)
    before = git(repo, "rev-parse", "lane/group-a", "lane/group-b")

    report, text = sweep(cfg)

    parent = repo / ".worktrees" / "group"
    assert parent.is_dir() and first.exists() and second.exists(), text
    assert f"REFUSED (kept) {parent}" in text and "WIP committed" not in text, text
    assert git(repo, "rev-parse", "lane/group-a", "lane/group-b") == before
    assert (first / "source.txt").read_text() == "unsaved\n"


def test_a_parent_of_two_checkouts_with_nothing_active_is_still_removed(estate):
    repo, wt, cfg = estate
    checkout(repo, "group", "a")
    checkout(repo, "group", "b")

    report, text = sweep(cfg)

    assert not (repo / ".worktrees" / "group").exists(), text
    assert git(repo, "rev-parse", "lane/group-a") and git(repo, "rev-parse", "lane/group-b")


def test_a_retained_lane_whose_session_is_running_keeps_the_checkout_and_a_stopped_one_does_not(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    _finished_lane(repo, lane, "delivered")

    with _session_on_lane(repo):
        report, text = sweep(cfg)
        assert lane.exists(), text
        assert lane_occupancy.refusal("one", lane_occupancy.IN_USE) in text, text
        assert "FAILED" not in text and not report.apply_failed

    report, text = sweep(cfg)  # the session ended: nothing of the refusal is left behind
    assert not lane.exists(), text


def test_a_lane_token_that_cannot_be_checked_keeps_the_checkout(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    _finished_lane(repo, lane, "abandoned")
    (repo / ".agenttalk/lane-occupancy/lane-one.token").mkdir(parents=True)  # not a plain file: never read as free

    report, text = sweep(cfg)

    assert lane.exists() and lane_occupancy.UNKNOWN in text, text


def test_a_damaged_record_keeps_every_checkout_and_says_it_could_not_be_read(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    state = repo / ".agenttalk" / "state"
    state.mkdir()
    (state / "lanes.json").write_text("{not json")

    report = janitor.build_report(cfg)
    preview = janitor.format_report(report, cfg, apply=False)
    applied = janitor.apply(cfg, report)

    assert f"REFUSED (kept) {lane}" in preview and "could not read" in preview, preview
    assert lane.exists() and f"REFUSED (kept) {lane}" in applied and "could not read" in applied, applied


@pytest.mark.parametrize("error", [PermissionError(13, "Access is denied"), OSError(32, "sharing violation")])
def test_a_recorded_path_that_cannot_be_followed_keeps_the_checkout(estate, monkeypatch, error):
    """#853's strict placement, reached through the sweep: an uncertain path is never read as an unrelated one."""
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    alias = repo.parent / "alias-to-the-checkout"
    directory_link(alias, lane)  # a real link, which the resolver is then made unable to follow
    _write_record(repo, "active-lane", alias)
    _resolver_cannot_follow(monkeypatch, alias, error)
    try:
        report, text = sweep(cfg)
        assert lane.exists() and "could not establish where activity path" in text, text
    finally:
        monkeypatch.undo()
        alias.rmdir() if os.name == "nt" else alias.unlink()


def test_a_record_naming_a_missing_unrelated_path_does_not_hold_a_checkout(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    _write_record(repo, "active-lane", repo.parent / "long-gone" / "deeper")

    report, text = sweep(cfg)

    assert not lane.exists(), text


def test_a_record_that_names_the_checkout_through_a_junction_keeps_it(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    alias = repo.parent / "alias-to-the-checkout"
    directory_link(alias, lane)
    try:
        _write_record(repo, "active-lane", alias)
        report, text = sweep(cfg)
        assert lane.exists() and f"REFUSED (kept) {lane}" in text, text
    finally:
        alias.rmdir() if os.name == "nt" else alias.unlink()


def test_a_record_that_appears_after_the_preview_is_found_by_apply(estate):
    """A preview is not an authorization: apply reads the records again."""
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    report = janitor.build_report(cfg)
    assert "REFUSED" not in janitor.format_report(report, cfg, apply=False)
    _write_record(repo, "active-lane", lane)

    text = janitor.apply(cfg, report)

    assert lane.exists() and f"REFUSED (kept) {lane}" in text, text


def test_a_lane_that_starts_while_the_work_is_committed_keeps_the_checkout(estate, monkeypatch):
    """The last look at the records is made at the removal, after the commit that preserved unsaved work."""
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    (lane / "source.txt").write_text("unsaved\n")
    real = janitor.wip_commit_dirty_worktree

    def commit_then_a_lane_starts(path, **kwargs):
        result = real(path, **kwargs)
        _write_record(repo, "active-lane", lane)
        return result

    monkeypatch.setattr(janitor, "wip_commit_dirty_worktree", commit_then_a_lane_starts)

    report, text = sweep(cfg)

    assert lane.exists() and f"REFUSED (kept) {lane}" in text, text
    assert "removed: 1" not in text


def test_a_checkout_registered_under_the_parent_after_the_checks_keeps_the_parent(estate, monkeypatch):
    repo, wt, cfg = estate
    first = checkout(repo, "group", "a")
    (first / "source.txt").write_text("unsaved\n")
    real = janitor.wip_commit_dirty_worktree
    added = []

    def commit_then_another_checkout_appears(path, **kwargs):
        result = real(path, **kwargs)
        added.append(checkout(repo, "group", "c"))
        return result

    monkeypatch.setattr(janitor, "wip_commit_dirty_worktree", commit_then_another_checkout_appears)

    report, text = sweep(cfg)

    assert added and (repo / ".worktrees" / "group").is_dir() and added[0].exists(), text
    assert "REFUSED (kept)" in text and "registered checkouts" in text, text


def test_the_claims_and_the_lock_are_held_through_the_removal(estate, monkeypatch):
    """A session cannot start on the lane, and no lane writer can get in, between the last look and the removal."""
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    _finished_lane(repo, lane, "delivered")
    seen = {}
    real = janitor.remove_plainly

    def looking(path):
        seen["token"] = lane_occupancy.peek(Store(repo), "one")
        with contextlib.suppress(TimeoutError):
            with Store(repo).config_lock(timeout=0.2):
                seen["lock"] = "free"
        return real(path)

    monkeypatch.setattr(janitor, "remove_plainly", looking)

    report, text = sweep(cfg)

    assert not lane.exists(), text
    assert seen.get("token") is not None, "the lane's token was free during the removal"
    assert "lock" not in seen, "the config lock was free during the removal"


def test_one_lane_that_names_two_checkouts_of_a_parent_does_not_refuse_itself(estate):
    repo, wt, cfg = estate
    first, second = checkout(repo, "group", "a"), checkout(repo, "group", "b")
    state = repo / ".agenttalk" / "state"
    state.mkdir()
    row = {"status": "delivered", "worktree_path": str(first), "occupancy_worktrees": [str(second)]}
    (state / "lanes.json").write_text(json.dumps({"lanes": {"one": row}}))

    report, text = sweep(cfg)

    assert not (repo / ".worktrees" / "group").exists(), text


def test_a_failed_check_is_a_failure_and_a_kept_checkout_is_not(estate, monkeypatch):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    monkeypatch.setattr(worktree_release, "_activity", lambda *args: (_ for _ in ()).throw(OSError(5, "unreadable")))
    report = janitor.build_report(cfg)

    text = janitor.apply(cfg, report)

    assert lane.exists() and report.apply_failed and "could not check whether" in text, text


def test_the_command_keeps_an_active_checkout_and_exits_clean(estate, capsys):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    _write_record(repo, "active-lane", lane)

    assert main(["--root", str(repo), "janitor"]) == 0
    preview = capsys.readouterr().out
    assert main(["--root", str(repo), "janitor", "--apply"]) == 0
    applied = capsys.readouterr().out

    assert f"REFUSED (kept) {lane}" in preview and f"REFUSED (kept) {lane}" in applied, (preview, applied)
    assert lane.exists()


def test_the_report_escapes_what_a_record_puts_in_the_reason(estate, capsys):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    state = repo / ".agenttalk" / "state"
    state.mkdir()
    row = {"status": "active", "worktree_path": str(lane)}
    (state / "lanes.json").write_text(json.dumps({"lanes": {"x\x1b[31m\nFAILED forged": row}}))

    assert main(["--root", str(repo), "janitor", "--apply"]) == 0
    out = capsys.readouterr().out

    assert "\x1b" not in out and "\nFAILED forged" not in out and f"REFUSED (kept) {lane}" in out, out
    assert lane.exists()
