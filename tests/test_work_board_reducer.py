"""B3a: pure work-board reducer, one test per design section 3 precedence row.

Envelope shapes copy live bus messages (2026-09-26): task openers carry
epoch_at_send/request_id/stage/work_item; task-responses carry
in_reply_to/request_id/status/verdict; review-results carry
in_reply_to/request_id/status; rescind carries request_id only.
"""

from dataclasses import replace

from agenttalk import work_board as W
from agenttalk.store import Message

LEAD = "claude-agenttalk-lead"
OLD_LEAD = "codex-agenttalk-lead"
BUILDER = "codex-agenttalk-dev-1"
REVIEWER = "claude-agenttalk-developer-2"
REVIEWER2 = "qwen-agenttalk-dev-1"
ITEM = "work-board-207"
HEAD, HEAD2 = "a" * 40, "b" * 40
POLICY = {"no_gates_reason": "CI tracked outside local gates"}


class Bus:
    def __init__(self):
        self.messages = []

    def add(self, sender, to, kind, meta):
        n = len(self.messages) + 1
        message = Message.from_dict({
            "id": f"20260926-2225{n:02d}-{n:06d}-q{n:03d}", "ts": f"2026-09-26T22:25:{n:02d}.000000Z",
            "from": sender, "to": to, "kind": kind, "subject": "subject", "body": "body", "meta": meta})
        self.messages.append(message)
        return message

    def task(self, rid, to, stage, sender=LEAD, kind="task", item=ITEM, **extra):
        meta = {"epoch_at_send": None, "request_id": rid, "stage": stage, **extra}
        if item is not None:
            meta["work_item"] = item
        return self.add(sender, to, kind, meta)

    def reply(self, opener, status="done", verdict=None):
        kind = "review-result" if opener.kind == "review-request" else "task-response"
        meta = {"in_reply_to": opener.id, "request_id": opener.meta["request_id"], "status": status}
        if verdict is not None:
            meta["verdict"] = verdict
        return self.add(opener.recipient, opener.sender, kind, meta)

    def rescind(self, rid, sender, to=BUILDER):
        return self.add(sender, to, "rescind", {"request_id": rid})


def card(bus, **facts):
    return next(i for i in W.reduce(bus.messages, lead=LEAD, **facts)["items"] if i["work_item"] == ITEM)


def reviewed(bus, verdict="GO", reviewer=REVIEWER):
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    bus.reply(build, verdict="done")
    read = bus.task("tk-read", reviewer, "read", work_head=HEAD)
    return build, read, bus.reply(read, verdict=verdict)


def test_row1_linked_incident_overlays_needs_you_and_keeps_placement():
    bus = Bus()
    reviewed(bus)
    item = card(bus, incidents=[{"id": "esc-1", "work_item": ITEM, "work_cycle": "1"},
                                {"id": "esc-2", "work_item": "other-item", "work_cycle": "1"}])
    assert (item["column"], item["workflow_column"], item["incidents"]) == ("needs_you", "ready", ["esc-1"])
    assert card(bus)["column"] == "ready"


def test_row2_conflicting_terminal_replies_are_unknown():
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    done, declined = bus.reply(build, verdict="done"), bus.reply(build, status="declined")
    item = card(bus)
    assert (item["workflow_column"], item["row"]) == ("unknown", 2)
    assert "multiple terminal replies" in item["reason"] and {done.id, declined.id} <= set(item["evidence"])


def test_row3_integrated_candidate_is_done_and_keeps_open_fix_visible():
    bus = Bus()
    reviewed(bus)
    item = card(bus, integrated={(ITEM, HEAD): True})
    assert (item["workflow_column"], item["row"]) == ("done", 3)
    fixed = Bus()
    _, _, fix = reviewed(fixed, verdict="FIX")
    item = card(fixed, integrated={(ITEM, HEAD): True})
    assert item["workflow_column"] == "done" and item["reason"] == "merged with open FIX/HOLD"
    assert fix.id in item["evidence"]


def test_row4_unresolved_fix_or_active_fix_task_is_fix_round():
    bus = Bus()
    _, _, fix = reviewed(bus, verdict="FIX")
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["evidence"]) == ("fix_round", 4, [fix.id])
    fix_task = bus.task("tk-fix", BUILDER, "fix")
    item = card(bus)
    assert item["workflow_column"] == "fix_round" and item["reason"] == "fix dispatched"
    bus.reply(fix_task, status="accepted")
    assert card(bus)["reason"] == "fix accepted"


def test_row5_accepted_or_running_build_is_building():
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    bus.reply(build, status="accepted")
    assert (card(bus)["workflow_column"], card(bus)["row"]) == ("building", 5)
    design = Bus()
    design.task("tk-design", BUILDER, "design", **POLICY)
    item = card(design, running={(BUILDER, "tk-design")})
    assert (item["workflow_column"], item["reason"]) == ("building", "design running")


def test_row6_outstanding_review_labels_unproved_independence():
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    bus.reply(build, verdict="done")
    read = bus.task("tk-read", REVIEWER, "read", work_head=HEAD)
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["reason"], item["evidence"]) == (
        "independent_review", 6, "independent review", [read.id])
    bus.task("tk-self", BUILDER, "delta", work_head=HEAD)
    assert card(bus)["reason"] == "unverified review"


def test_row7_ready_needs_deliverable_independent_go_and_check_policy():
    bus = Bus()
    build, read, go = reviewed(bus)
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["candidate"]) == ("ready", 7, HEAD)
    assert item["reason"] == "reviewed; independent GO; local checks not tracked"
    assert {go.id} | {m.id for m in bus.messages if m.meta.get("in_reply_to") == build.id} <= set(item["evidence"])
    assert item["verdicts"][HEAD] == [{"reviewer": REVIEWER, "verdict": "GO", "reply": go.id,
                                       "independent": True, "vendor": "unverified"}]
    native = Bus()
    native_build = native.task("tk-build", BUILDER, "build", **POLICY)
    native.reply(native_build, verdict="done")
    request = native.task("tk-read", REVIEWER, "read", kind="review-request", work_head=HEAD,
                          assignee_model_vendors={REVIEWER: "anthropic"})
    native.reply(request, status="approved", verdict="GO")
    item = card(native)
    assert item["workflow_column"] == "ready" and item["verdicts"][HEAD][0]["vendor"] == "anthropic"


def test_row8_dispatched_build_without_evidence_is_queued():
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["reason"], item["evidence"]) == (
        "queued", 8, "start unconfirmed", [build.id])


def test_row9_unknown_carries_the_exact_reason():
    missing = Bus()
    missing.reply(missing.task("tk-build", BUILDER, "build", **POLICY))
    assert (card(missing)["workflow_column"], card(missing)["row"], card(missing)["reason"]) == (
        "unknown", 9, "verdict missing")
    self_review = Bus()
    reviewed(self_review, reviewer=BUILDER)
    assert card(self_review)["reason"] == "no independent GO"
    no_policy = Bus()
    no_policy.reply(no_policy.task("tk-build", BUILDER, "build"), verdict="done")
    no_policy.reply(no_policy.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert card(no_policy)["reason"] == "check policy missing"
    held = Bus()
    reviewed(held, verdict="HOLD")
    assert card(held)["reason"] == "non-operator HOLD"
    later = Bus()
    reviewed(later)  # cycle 1 build defines policy; cycle 2 only has a fix and its review
    later.reply(later.task("tk-fix", BUILDER, "fix", work_cycle="2"), verdict="done")
    later.reply(later.task("tk-read-2", REVIEWER, "read", work_head=HEAD2, work_cycle="2"), verdict="GO")
    assert (card(later)["cycle"], card(later)["workflow_column"]) == (2, "ready")


def test_later_go_never_hides_fix_whatever_the_id_order():
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", **POLICY)
    bus.reply(build, verdict="done")
    bus.reply(bus.task("tk-read-1", REVIEWER, "read", work_head=HEAD), verdict="FIX")
    bus.reply(bus.task("tk-read-2", REVIEWER2, "read", work_head=HEAD), verdict="GO")
    assert card(bus)["workflow_column"] == "fix_round"
    # Reverse the writer clocks: every later message now sorts first. Explicit links still decide.
    ids = [m.id for m in bus.messages][::-1]
    skewed = Bus()
    remap = {m.id: new for m, new in zip(bus.messages, ids, strict=True)}
    for m in bus.messages:
        meta = dict(m.meta, **({"in_reply_to": remap[m.meta["in_reply_to"]]} if "in_reply_to" in m.meta else {}))
        skewed.messages.append(replace(m, id=remap[m.id], meta=meta))
    assert card(skewed)["workflow_column"] == "fix_round"


def test_rescind_is_requester_only_and_earlier_requester_task_blocks_ready():
    bus = Bus()
    bus.task("tk-old", BUILDER, "build", sender=OLD_LEAD, **POLICY)
    replacement = bus.task("tk-new", REVIEWER2, "build", supersedes="tk-old", **POLICY)
    bus.reply(replacement, verdict="done")
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert (card(bus)["workflow_column"], card(bus)["reason"]) == ("unknown", W.EARLIER_REQUESTER)
    bus.rescind("tk-old", sender=LEAD)
    assert card(bus)["reason"] == W.EARLIER_REQUESTER
    bus.rescind("tk-old", sender=OLD_LEAD)
    assert card(bus)["workflow_column"] == "ready"


def test_superseded_fix_awaits_replacement_review_then_ready():
    bus = Bus()
    reviewed(bus, verdict="FIX")
    replacement = bus.task("tk-read-2", REVIEWER, "delta", work_head=HEAD2, supersedes="tk-read")
    item = card(bus)
    assert (item["workflow_column"], item["reason"]) == ("independent_review", "awaiting replacement review")
    bus.reply(replacement, verdict="GO")
    item = card(bus)
    assert (item["workflow_column"], item["candidate"]) == ("ready", HEAD2)


def test_external_deliverable_review_only_item_and_mixed_provenance():
    bus = Bus()
    read = bus.task("tk-read", REVIEWER, "read", work_head=HEAD, external_deliverable="true", **POLICY)
    bus.reply(read, verdict="GO")
    item = card(bus)
    assert item["workflow_column"] == "ready" and "external deliverable; authorship unverified" in item["issues"]
    bus.task("tk-build", BUILDER, "build", work_cycle="2", **POLICY)
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["reason"]) == (
        "unknown", 2, "external deliverable conflicts with recorded build dispatches")
    malformed = Bus()
    malformed.task("tk-read", REVIEWER, "read", work_head=HEAD, external_deliverable="yes", **POLICY)
    assert (card(malformed)["row"], card(malformed)["reason"]) == (2, "malformed work metadata")


def test_legacy_untagged_openers_form_one_counted_group():
    bus = Bus()
    fan = [bus.task("tk-legacy", to, "build", item=None) for to in (BUILDER, REVIEWER)]
    bus.reply(fan[0], verdict="done")
    bus.reply(bus.task("tk-closed", BUILDER, "build", item=None), verdict="done")
    bus.add(LEAD, BUILDER, "question", {"request_id": "q-1"})
    reviewed(bus)
    out = W.reduce(bus.messages, lead=LEAD)
    assert out["legacy"] == {"open_request_count": 1, "known_lower_bound": 1, "counts_by_kind": {"task": 1},
                             "examples": [fan[0].id], "truncated": False}
    assert [i["work_item"] for i in out["items"]] == [ITEM]


def test_reducer_is_pure_order_free_and_never_reads_bodies_or_subjects():
    bus = Bus()
    reviewed(bus, verdict="FIX")
    bus.task("tk-legacy", BUILDER, "build", item=None)
    before = [m.to_dict() for m in bus.messages]
    expected = W.reduce(bus.messages, lead=LEAD)
    scrubbed = [replace(m, body="GO GO GO", subject="READY merged") for m in reversed(bus.messages)]
    assert W.reduce(scrubbed, lead=LEAD) == expected
    assert [m.to_dict() for m in bus.messages] == before
