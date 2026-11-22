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


def test_a_checkout_registered_under_an_empty_candidate_before_the_delete_keeps_it(estate, monkeypatch):
    """A plain folder (no checkout at the first look) gains an active checkout just before the last comparison."""
    repo, wt, cfg = estate
    parent = repo / ".worktrees" / "group"
    parent.mkdir(parents=True)
    report = janitor.build_report(cfg)
    assert any(c.path == parent for c in report.candidates)
    real = janitor._checkouts_changed
    created = []

    def register(c, worktrees, cfg):
        if c.path == parent:
            lane = checkout(repo, "group", "new")
            _write_record(repo, "active-lane", lane)
            created.append(lane)
        return real(c, worktrees, cfg)

    monkeypatch.setattr(janitor, "_checkouts_changed", register)

    text = janitor.apply(cfg, report)

    assert created and created[0].exists() and parent.is_dir(), text
    assert "REFUSED (kept)" in text and "removed: 1" not in text, text


def test_a_plain_folder_with_no_new_checkout_is_still_removed(estate):
    repo, wt, cfg = estate
    parent = repo / ".worktrees" / "plain"
    parent.mkdir(parents=True)
    (parent / "note.txt").write_text("x")

    report, text = sweep(cfg)

    assert not parent.exists(), text


def test_a_lane_and_a_session_that_appear_at_the_last_look_keep_the_checkout(estate, monkeypatch):
    """Nothing named the checkout at the first look; a delivered lane and a running session appear after the list of
    checkouts was compared. The last read of the records and the occupancy claim come after that comparison."""
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    report = janitor.build_report(cfg)
    real = janitor._checkouts_changed
    sessions = []

    def start(c, worktrees, cfg):
        if c.path == lane:
            _finished_lane(repo, lane, "delivered")
            session = _session_on_lane(repo)
            session.__enter__()
            sessions.append(session)
        return real(c, worktrees, cfg)

    monkeypatch.setattr(janitor, "_checkouts_changed", start)
    try:
        text = janitor.apply(cfg, report)
        assert sessions and lane.exists(), text
        assert lane_occupancy.refusal("one", lane_occupancy.IN_USE) in text, text
    finally:
        for session in sessions:
            session.__exit__(None, None, None)


def test_a_lane_that_appears_at_the_last_look_is_claimed_through_the_delete(estate, monkeypatch):
    """A lane that names the checkout only after the first claims were taken gets a claim too, kept to the end."""
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    report = janitor.build_report(cfg)
    real_changed, real_remove = janitor._checkouts_changed, janitor.remove_plainly
    seen = {}

    def appears(c, worktrees, cfg):
        if c.path == lane:
            _finished_lane(repo, lane, "delivered")
        return real_changed(c, worktrees, cfg)

    def looking(path):
        seen["token"] = lane_occupancy.peek(Store(repo), "one")
        return real_remove(path)

    monkeypatch.setattr(janitor, "_checkouts_changed", appears)
    monkeypatch.setattr(janitor, "remove_plainly", looking)

    text = janitor.apply(cfg, report)

    assert not lane.exists(), text  # a finished lane with a free token does not keep a checkout, as in a release
    assert seen.get("token") is not None, "the new lane's token was free during the delete"


def test_the_lock_every_lane_writer_takes_is_held_through_the_delete_even_when_no_lane_named_it(estate, monkeypatch):
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    seen = {}
    real = janitor.remove_plainly

    def looking(path):
        with contextlib.suppress(TimeoutError):
            with Store(repo).config_lock(timeout=0.2):
                seen["lock"] = "free"
        return real(path)

    monkeypatch.setattr(janitor, "remove_plainly", looking)

    report, text = sweep(cfg)

    assert not lane.exists(), text
    assert "lock" not in seen, "a lane writer could have got in between the last look and the delete"


def test_a_repository_without_a_store_is_swept_without_making_one(estate):
    import shutil

    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    shutil.rmtree(repo / ".agenttalk")

    report, text = sweep(cfg)

    assert not lane.exists(), text
    assert not (repo / ".agenttalk").exists()


def test_the_preview_lists_every_kept_checkout_even_past_the_display_limit(estate):
    """Only the oldest 15 candidates are listed, but a checkout that is in use must never hide in `and N more`."""
    repo, wt, cfg = estate
    old = 1_000_000_000
    for number in range(16):
        folder = repo / ".worktrees" / f"plain-{number:02d}"
        folder.mkdir(parents=True)
        os.utime(folder, (old + number, old + number))
    busy = checkout(repo, "busy")  # the newest candidate: outside the 15 that are listed
    _write_record(repo, "active-lane", busy)

    report = janitor.build_report(cfg)
    preview = janitor.format_report(report, cfg, apply=False)

    assert len(report.candidates) == 17
    assert f"REFUSED (kept) {busy}" in preview, preview
    assert "and 1 more" in preview, preview  # 17 candidates: 15 listed by age, the kept one named, one left over


@pytest.mark.parametrize("failure", [(1, "", "git list failed"), (None, "", "git is not resolvable")])
@pytest.mark.parametrize("held_first", [False, True])
def test_a_failed_final_listing_of_checkouts_keeps_the_candidate_and_is_reported_as_failed(
    estate, monkeypatch, failure, held_first
):
    """Git's list of registered checkouts is compared at the delete. A failed listing is not an empty one: with an
    empty first list and a failed final one, an active checkout that appeared under the folder would not be seen."""
    repo, wt, cfg = estate
    parent = repo / ".worktrees" / "group"
    if held_first:
        checkout(repo, "group", "first")
    else:
        parent.mkdir(parents=True)
    report = janitor.build_report(cfg)
    assert any(c.path == parent for c in report.candidates)
    real_changed, real_git = janitor._checkouts_changed, janitor._run_git_checked
    created = []

    def changed(c, worktrees, cfg):
        if c.path == parent:
            lane = checkout(repo, "group", "new")
            _write_record(repo, "active-lane", lane)
            created.append(lane)

            def failed_git(repo, *args, **kwargs):
                if args[:3] == ("worktree", "list", "--porcelain"):
                    return failure
                return real_git(repo, *args, **kwargs)

            monkeypatch.setattr(janitor, "_run_git_checked", failed_git)
        return real_changed(c, worktrees, cfg)

    monkeypatch.setattr(janitor, "_checkouts_changed", changed)

    text = janitor.apply(cfg, report)

    assert created and created[0].exists() and parent.is_dir(), text
    assert report.apply_failed and "FAILED" in text and "could not list the registered checkouts" in text, text
    assert "then run the sweep again" in text, text
    assert "removed: 1" not in text


def test_a_successful_final_listing_still_lets_a_folder_with_a_checkout_go(estate):
    repo, wt, cfg = estate
    checkout(repo, "group", "only")

    report, text = sweep(cfg)

    assert not (repo / ".worktrees" / "group").exists(), text
    assert not report.apply_failed


@pytest.mark.parametrize("final_works", [True, False])
def test_a_checkout_that_appears_under_an_empty_candidate_is_kept_whatever_the_first_listing_did(
    estate, monkeypatch, final_works
):
    """The listing failed when the report was made (so the folder looked empty and plain), then an active checkout was
    registered under it. At the delete the listing works (the folder is kept: it changed) or fails again (it is kept
    and reported as FAILED): a failed listing is never read as "no checkout", whatever the first one did."""
    repo, wt, cfg = estate
    parent = repo / ".probe-parent"
    parent.mkdir()
    real_git = janitor._run_git_checked

    def failed_git(repo, *args, **kwargs):
        if args[:3] == ("worktree", "list", "--porcelain"):
            return 1, "", "git list failed"
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", failed_git)
    report = janitor.build_report(cfg)
    assert any(c.path == parent for c in report.candidates)
    real_changed = janitor._checkouts_changed
    created = []

    def changed(c, worktrees, cfg):
        if c.path == parent:
            lane = parent / "new"
            git(repo, "worktree", "add", "-q", "-b", "lane/new", str(lane))
            _write_record(repo, "active-lane", lane)
            created.append(lane)
            if final_works:
                monkeypatch.setattr(janitor, "_run_git_checked", real_git)
        return real_changed(c, worktrees, cfg)

    monkeypatch.setattr(janitor, "_checkouts_changed", changed)

    text = janitor.apply(cfg, report)

    assert created and created[0].exists() and parent.is_dir(), text
    assert "removed: 1" not in text
    assert report.apply_failed is (not final_works)


def test_every_candidate_is_kept_and_reported_as_failed_when_git_cannot_list_checkouts(estate, monkeypatch):
    """With no list of checkouts from Git, nothing can show that a candidate holds none at the delete, so none is
    removed."""
    repo, wt, cfg = estate
    folder = repo / ".probe-folder"
    folder.mkdir()
    (folder / "note.txt").write_text("x")
    real_git = janitor._run_git_checked

    def failed_git(repo, *args, **kwargs):
        if args[:3] == ("worktree", "list", "--porcelain"):
            return 1, "", "git list failed"
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", failed_git)

    report, text = sweep(cfg)

    assert folder.is_dir() and report.apply_failed
    assert "FAILED" in text and "could not list the registered checkouts" in text, text
    assert "Git could not list this repository's checkouts" in text and "then run the sweep again" in text, text


def listing_fails(monkeypatch):
    real_git = janitor._run_git_checked

    def failed_git(repo, *args, **kwargs):
        if args[:3] == ("worktree", "list", "--porcelain"):
            return 1, "", "git list failed"
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", failed_git)


def test_the_failed_list_never_tells_the_person_to_remove_a_folder_that_was_not_checked(estate, monkeypatch):
    """A folder kept because Git could not list its checkouts may hold a live one: the report must not say to remove it
    by hand, which would skip the check that could not be made. Each is named with what to do instead."""
    repo, wt, cfg = estate
    folders = [repo / ".probe-first", repo / ".probe-second"]
    for folder in folders:
        folder.mkdir()
    listing_fails(monkeypatch)
    report = janitor.build_report(cfg)

    text = janitor.apply(cfg, report)

    assert report.apply_failed and all(folder.exists() for folder in folders), text
    assert all(folder.name in text for folder in folders), text
    assert "remove it yourself" not in text and "Look at each one" not in text, text
    assert "do not remove them by hand" in text and "fix Git" in text and "run the sweep again" in text, text


def test_mixed_failures_advise_removal_only_for_the_candidates_that_were_checked(estate, monkeypatch):
    """One candidate could not be checked, another was checked and its delete failed: the old advice stays for the
    second one only, and the first one is never listed under it."""
    repo, wt, cfg = estate
    unchecked, failed_delete = repo / ".probe-unchecked", repo / ".probe-failed"
    for folder in (unchecked, failed_delete):
        folder.mkdir()
    report = janitor.build_report(cfg)
    real_changed, real_remove = janitor._checkouts_changed, janitor.remove_plainly

    def changed(c, worktrees, cfg):
        if c.path == unchecked:
            raise janitor._UseCheckFailed("could not list the registered checkouts to compare them (test)")
        return real_changed(c, worktrees, cfg)

    def remove(path):
        if path == failed_delete:
            return "FAILED", "injected delete failure"
        return real_remove(path)

    monkeypatch.setattr(janitor, "_checkouts_changed", changed)
    monkeypatch.setattr(janitor, "remove_plainly", remove)

    text = janitor.apply(cfg, report)

    lines = text.splitlines()
    old = next(n for n, line in enumerate(lines) if "Look at each one, then remove it yourself:" in line)
    new = next(n for n, line in enumerate(lines) if "do not remove them by hand" in line)
    section = {
        old: lines[old + 1 : new] if old < new else lines[old + 1 :],
        new: lines[new + 1 : old] if new < old else lines[new + 1 :],
    }
    assert ".probe-failed" in "\n".join(section[old]) and ".probe-unchecked" not in "\n".join(section[old]), text
    assert ".probe-unchecked" in "\n".join(section[new]) and ".probe-failed" not in "\n".join(section[new]), text
    assert unchecked.exists() and report.apply_failed


def sections(text):
    """The FAILED headings of a report and the lines under each: {heading start: [lines]}."""
    lines, found, current = text.splitlines(), {}, None
    for line in lines:
        if line.startswith("FAILED (") and line.endswith(":"):
            current = line
            found[current] = []
        elif current is not None and line.startswith("  "):
            found[current].append(line)
        elif current is not None:
            current = None
    return found


@pytest.mark.parametrize("when", ["after-discovery", "later-in-the-checks"])
def test_a_candidate_whose_identity_changed_is_never_advised_for_removal_by_hand(estate, monkeypatch, when):
    """The path changed after discovery (or between the checks and the delete): its checkout and use were not
    established for what is there now, so it is listed as not checked, never under "remove it yourself"."""
    repo, wt, cfg = estate
    folder = repo / ".probe-swapped"
    folder.mkdir()
    report = janitor.build_report(cfg)
    assert any(c.path == folder for c in report.candidates)
    if when == "after-discovery":
        folder.rmdir()
        directory_link(folder, wt)  # a link where the folder was: it may lead to a live checkout
    else:
        real, calls = janitor._path_change_reason, []

        def changed(c, cfg):
            if c.path != folder:
                return real(c, cfg)
            calls.append(1)
            return None if len(calls) < 2 else "the candidate changed since it was found (test)"

        monkeypatch.setattr(janitor, "_path_change_reason", changed)
    try:
        text = janitor.apply(cfg, report)
    finally:
        if when == "after-discovery" and os.path.lexists(folder):
            folder.rmdir() if os.name == "nt" else folder.unlink()

    found = sections(text)
    assert report.apply_failed, text
    assert "remove it yourself" not in text and "Look at each one" not in text, text
    (heading,) = [h for h in found if "do not remove them by hand" in h]
    assert any(".probe-swapped" in line for line in found[heading]), text


def test_a_candidate_whose_delete_failed_still_gets_the_old_advice(estate, monkeypatch):
    """The checks completed and only the delete failed: the folder was checked, so the old advice is right for it."""
    repo, wt, cfg = estate
    folder = repo / ".probe-failed"
    folder.mkdir()
    report = janitor.build_report(cfg)
    monkeypatch.setattr(janitor, "remove_plainly", lambda path: ("FAILED", "injected delete failure"))

    text = janitor.apply(cfg, report)

    found = sections(text)
    (heading,) = [h for h in found if "remove it yourself" in h]
    assert any(".probe-failed" in line for line in found[heading]), text
    assert not [h for h in found if "do not remove them by hand" in h], text


# ------------------------------------------------ the checkouts under a candidate are compared without lossy decoding


@pytest.mark.parametrize("name", ["café", "a b", "日本語"], ids=["accent", "line-separator", "cjk"])
def test_a_late_checkout_with_a_non_ascii_name_keeps_the_candidate_and_its_unsaved_file(estate, monkeypatch, name):
    """The listing was decoded with the console's encoding and split with splitlines(): a name such as `café` read as a
    path that does not exist, and one with U+2028 was cut in two, so the checkout registered under an empty allowed
    folder after the report was not seen and was deleted with its unsaved files (data loss). The listing is read as
    bytes, with NUL record boundaries, and compared as the paths Git wrote."""
    repo, wt, cfg = estate
    parent = repo / ".probe-parent"
    parent.mkdir()
    report = janitor.build_report(cfg)
    assert any(c.path == parent for c in report.candidates)
    real_changed = janitor._checkouts_changed
    created = []

    def late(c, worktrees, cfg):
        if c.path == parent:
            lane = parent / name
            git(repo, "worktree", "add", "-q", "-b", "lane/late", str(lane))
            (lane / "unsaved.txt").write_text("not saved anywhere else")
            _write_record(repo, "active-lane", lane)
            created.append(lane)
        return real_changed(c, worktrees, cfg)

    monkeypatch.setattr(janitor, "_checkouts_changed", late)

    text = janitor.apply(cfg, report)

    assert created and (created[0] / "unsaved.txt").read_text() == "not saved anywhere else", text
    assert parent.is_dir() and "removed: 1" not in text, text


@pytest.mark.parametrize(
    "answer",
    [
        (0, b"", b""),
        (0, b"no record here", b""),
        (0, b"HEAD abc\0\0", b""),
        (0, b"worktree \xff\xfe\0\0", b""),
        (0, b"worktree " + b"x" * 5, b""),
    ],
    ids=["empty", "no-record", "field-before-a-record", "undecodable-path", "record-not-terminated"],
)
def test_an_inventory_that_cannot_be_read_reliably_keeps_the_candidate_as_not_checked(estate, monkeypatch, answer):
    repo, wt, cfg = estate
    folder = repo / ".probe-unreadable"
    folder.mkdir()
    report = janitor.build_report(cfg)
    real_git = janitor._run_git_checked

    def odd_git(repo, *args, **kwargs):
        if args[:3] == ("worktree", "list", "--porcelain") and args[-1:] == ("-z",):
            return answer
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", odd_git)

    text = janitor.apply(cfg, report)

    assert folder.is_dir() and report.apply_failed, text
    found = sections(text)
    (heading,) = [h for h in found if "do not remove them by hand" in h]
    assert any(".probe-unreadable" in line for line in found[heading]), text
    assert "could not list the registered checkouts" in text and "then run the sweep again" in text, text


def test_the_listing_is_read_with_nul_boundaries_as_bytes(estate, monkeypatch):
    """The reader asks Git for `-z` and for bytes, so no decoding and no line splitting can change a path."""
    repo, wt, cfg = estate
    seen = []
    real = janitor._run_git_checked

    def watching(repo, *args, **kwargs):
        if args[:3] == ("worktree", "list", "--porcelain"):
            seen.append((args, kwargs))
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", watching)

    listed = janitor._registered_worktrees_listed(repo)

    assert any(janitor._same_worktree(path, repo) for path in listed)
    assert seen and seen[-1][0][-1] == "-z" and seen[-1][1].get("raw") is True, seen


# --------------------------------------------------------------- a kept checkout gets a next step


@pytest.mark.parametrize(
    "kind", ["active-lane", "pending-lane", "supervisor-state", "store-supervisor", "launch-request"]
)
def test_every_refusal_for_an_active_use_says_what_to_do_next(estate, kind):
    repo, wt, cfg = estate
    lane = checkout(repo, "l1")
    _write_record(repo, kind, lane)
    report = janitor.build_report(cfg)

    preview = janitor.format_report(report, cfg, apply=False)
    applied = janitor.apply(cfg, report)

    for text in (preview, applied):
        line = next(line for line in text.splitlines() if f"REFUSED (kept) {lane}" in line)
        assert "leave it until what uses it has finished" in line and "then run the sweep again" in line, line
    assert lane.exists()


def test_a_held_lane_session_refusal_says_what_to_do_next(estate):
    repo, wt, cfg = estate
    lane = checkout(repo, "one")
    _finished_lane(repo, lane, "delivered")
    with _session_on_lane(repo):
        report = janitor.build_report(cfg)
        preview = janitor.format_report(report, cfg, apply=False)
        applied = janitor.apply(cfg, report)
    for text in (preview, applied):
        line = next(line for line in text.splitlines() if f"REFUSED (kept) {lane}" in line)
        assert "then run the sweep again" in line, line
