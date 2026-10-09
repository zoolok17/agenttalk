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


# --- a pinned replacement resolves a reviewer disagreement (PR #403 round 2) -----------------------------------


def _disagreement(bus):
    _built(bus)
    first, second = _review(bus, REVIEWER, REVIEWER2)
    _answer(bus, first, HEAD)
    _answer(bus, second, HEAD2)


def test_a_pinned_replacement_resolves_the_disagreement_and_both_old_verdicts_stay_under_their_commits():
    bus = Bus()
    _disagreement(bus)
    before = card(bus)
    assert before["workflow_column"] == "unknown" and before["candidate"] is None
    assert "different commits" in before["reason"]
    delta = bus.task("tk-delta", REVIEWER, "delta", work_head=HEAD2, supersedes="tk-read")
    waiting = card(bus)
    assert "different commits" not in waiting["reason"], "the superseded request's disagreement still blocks"
    assert waiting["candidate"] == HEAD2 and waiting["workflow_column"] != "ready"       # the replacement is awaited
    _answer(bus, delta, HEAD2)
    done = card(bus)
    assert (done["candidate"], done["workflow_column"]) == (HEAD2, "ready")
    assert set(done["verdicts"]) == {HEAD, HEAD2}, "both old verdicts stay under the commits they reviewed"
    assert {v["reviewer"] for v in done["verdicts"][HEAD]} == {REVIEWER}
    assert {v["reviewer"] for v in done["verdicts"][HEAD2]} == {REVIEWER, REVIEWER2}


def test_a_disagreement_on_the_surviving_request_still_blocks():
    bus = Bus()
    _disagreement(bus)
    item = card(bus)
    assert item["workflow_column"] == "unknown" and "different commits" in item["reason"]


def test_other_conflicts_on_a_superseded_request_stay_conservative():
    """Only the commit disagreement is scoped to the surviving request: a request with two terminal replies keeps
    the item unknown even after a replacement."""
    bus = Bus()
    _built(bus)
    (read,) = _review(bus, REVIEWER)
    _answer(bus, read, HEAD)
    _answer(bus, read, HEAD)                                  # a second terminal reply from the same reviewer
    bus.task("tk-delta", REVIEWER, "delta", work_head=HEAD, supersedes="tk-read")
    item = card(bus)
    assert item["workflow_column"] == "unknown" and "multiple terminal replies" in item["reason"]


# --- a review that asks for more information keeps its commit (PR #403 round 3) --------------------------------

from test_work_board_reducer import LEAD  # noqa: E402


def _native_review(bus, *recipients):
    return [bus.task("tk-hold", who, "read", kind="review-request") for who in recipients]


def _native_answer(bus, opener, head, status, verdict):
    meta = {"request_id": "tk-hold", "in_reply_to": opener.id, "status": status, "verdict": verdict}
    if head is not None:
        meta["work_head"] = head
    return bus.add(opener.recipient, LEAD, "review-result", meta)


def test_a_needs_info_reply_keeps_its_commit_and_approves_nothing():
    bus = Bus()
    _built(bus)
    (review,) = _native_review(bus, REVIEWER)
    hold = _native_answer(bus, review, HEAD, "needs-info", "HOLD")
    item = card(bus)
    assert [v["reply"] for v in item["verdicts"][HEAD]] == [hold.id] and None not in item["verdicts"]
    assert item["verdicts"][HEAD][0]["verdict"] == "HOLD"
    assert item["workflow_column"] != "ready", "a HOLD approves nothing"
    assert item["obligations"][-1]["state"] != "done" or item["obligations"][-1]["verdict"] != "GO"


def test_a_hold_at_one_commit_and_a_go_at_another_are_a_disagreement_with_no_candidate():
    bus = Bus()
    _built(bus)
    first, second = _native_review(bus, REVIEWER, REVIEWER2)
    hold = _native_answer(bus, first, HEAD, "needs-info", "HOLD")
    go = _native_answer(bus, second, HEAD2, "approved", "GO")
    item = card(bus)
    assert item["candidate"] is None and item["workflow_column"] == "unknown"
    assert "different commits" in item["reason"]
    assert [v["reply"] for v in item["verdicts"][HEAD]] == [hold.id], "the HOLD stays under the commit it named"
    assert [v["reply"] for v in item["verdicts"][HEAD2]] == [go.id], "the GO is not filed under the HOLD's commit"


def test_a_hold_and_a_go_at_the_same_commit_agree_and_the_hold_still_blocks():
    bus = Bus()
    _built(bus)
    first, second = _native_review(bus, REVIEWER, REVIEWER2)
    _native_answer(bus, first, HEAD, "needs-info", "HOLD")
    _native_answer(bus, second, HEAD, "approved", "GO")
    item = card(bus)
    assert item["candidate"] == HEAD and item["workflow_column"] != "ready"       # the open HOLD is unresolved
    assert {v["verdict"] for v in item["verdicts"][HEAD]} == {"HOLD", "GO"}


def test_a_hold_with_no_commit_next_to_a_go_does_not_change_the_goes_commit():
    bus = Bus()
    _built(bus)
    first, second = _native_review(bus, REVIEWER, REVIEWER2)
    _native_answer(bus, first, None, "needs-info", "HOLD")
    _native_answer(bus, second, HEAD2, "approved", "GO")
    item = card(bus)
    assert item["candidate"] == HEAD2 and item["workflow_column"] != "ready"
