"""#299 / PR #403 round 1: a review task that pinned no commit takes its candidate from the commit the reviewers named.

Publication accepts a reply's work_head when the task named none; the board must then use it, not report
"candidate missing". It stays conservative: heads that are absent or disagree never yield a guessed candidate.
"""

from test_work_board_reducer import BUILDER, HEAD, HEAD2, ITEM, POLICY, REVIEWER, REVIEWER2, Bus, card


def _built(bus):
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    bus.reply(build, verdict="done")


def _review(bus, *recipients):
    return [bus.task("tk-read", who, "read") for who in recipients]          # one request, no work_head on it


def _answer(bus, opener, head=None, verdict="GO"):
    meta = {"in_reply_to": opener.id, "request_id": opener.meta["request_id"], "status": "done", "verdict": verdict}
    if head is not None:
        meta["work_head"] = head
    return bus.add(opener.recipient, opener.sender, "task-response", meta)


def test_one_concrete_head_on_the_reply_becomes_the_candidate_and_indexes_the_verdict():
    bus = Bus()
    _built(bus)
    (read,) = _review(bus, REVIEWER)
    reply = _answer(bus, read, HEAD)
    item = card(bus)
    assert item["candidate"] == HEAD
    assert item["workflow_column"] == "ready"
    assert [v["reply"] for v in item["verdicts"][HEAD]] == [reply.id] and None not in item["verdicts"]


def test_the_unpinned_review_reduces_exactly_like_the_same_review_pinned_on_that_head():
    unpinned = Bus()
    _built(unpinned)
    (read,) = _review(unpinned, REVIEWER)
    _answer(unpinned, read, HEAD)
    pinned = Bus()
    _built(pinned)
    pinned.reply(pinned.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    keys = ("candidate", "workflow_column", "column", "reason", "verdicts", "issues")
    a, b = card(unpinned), card(pinned)
    assert {k: a[k] for k in keys} == {k: b[k] for k in keys}
    assert a["workflow_column"] == "ready"


def test_fan_out_reviewers_naming_the_same_head_agree_on_one_candidate():
    bus = Bus()
    _built(bus)
    first, second = _review(bus, REVIEWER, REVIEWER2)
    _answer(bus, first, HEAD)
    _answer(bus, second, HEAD)
    item = card(bus)
    assert item["candidate"] == HEAD and item["workflow_column"] == "ready"
    assert {v["reviewer"] for v in item["verdicts"][HEAD]} == {REVIEWER, REVIEWER2}


def test_a_reply_with_no_head_leaves_the_candidate_missing_as_before():
    bus = Bus()
    _built(bus)
    (read,) = _review(bus, REVIEWER)
    _answer(bus, read)
    item = card(bus)
    assert item["candidate"] is None and item["workflow_column"] == "unknown" and "candidate missing" in item["reason"]


def test_one_reviewer_naming_a_head_and_another_naming_none_picks_no_candidate():
    bus = Bus()
    _built(bus)
    first, second = _review(bus, REVIEWER, REVIEWER2)
    _answer(bus, first, HEAD)
    _answer(bus, second)
    item = card(bus)
    assert item["candidate"] is None and item["workflow_column"] != "ready"


def test_reviewers_naming_different_heads_are_a_conflict_and_never_an_arbitrary_pick():
    bus = Bus()
    _built(bus)
    first, second = _review(bus, REVIEWER, REVIEWER2)
    _answer(bus, first, HEAD)
    _answer(bus, second, HEAD2)
    item = card(bus)
    assert item["candidate"] is None and item["workflow_column"] == "unknown"
    assert "different commits" in item["reason"]


def test_a_head_named_by_one_reviewer_while_another_is_still_outstanding_is_the_candidate():
    bus = Bus()
    _built(bus)
    first, _second = _review(bus, REVIEWER, REVIEWER2)
    _answer(bus, first, HEAD)
    item = card(bus)
    assert item["candidate"] == HEAD and item["workflow_column"] != "ready"       # the other review is awaited


def test_a_pinned_task_is_untouched():
    bus = Bus()
    _built(bus)
    read = bus.task("tk-read", REVIEWER, "read", work_head=HEAD)
    _answer(bus, read, HEAD)
    assert card(bus)["candidate"] == HEAD and ITEM
