"""B3a/B3b: pure work-board reducer, one test per design section 3 precedence row,
plus the B3a cold-read regressions and adversarial orderings.

Envelope shapes copy live bus messages (2026-09-26): task openers carry
epoch_at_send/request_id/stage/work_item; task-responses carry
in_reply_to/request_id/status/verdict; review-results carry
in_reply_to/request_id/status; rescind carries request_id only. Every fixture
envelope passes the shared B2a publication normalizer (work_tags.normalize)
unless it models malformed history explicitly (raw=True).
"""

import random
from dataclasses import replace

from agenttalk import work_board as W
from agenttalk import work_tags
from agenttalk.store import Message

LEAD = "claude-agenttalk-lead"
LIAISON = "claude-agenttalk-liaison"
OLD_LEAD = "codex-agenttalk-lead"
BUILDER = "codex-agenttalk-dev-1"
REVIEWER = "claude-agenttalk-developer-2"
REVIEWER2 = "qwen-agenttalk-dev-1"
ITEM = "work-board-207"
HEAD, HEAD2 = "a" * 40, "b" * 40
POLICY = {"no_gates_reason": "CI tracked outside local gates"}
EXTERNAL = {"external_deliverable": "true", "work_repo": "agenttalk", "work_branch": "feature",
            "work_target": "master", "required_gates": "[]", **POLICY}


class Bus:
    """Publishes through the real B2a normalizer; also the store it consults."""

    def __init__(self):
        self.messages = []

    def valid_messages(self):
        return list(self.messages)

    def sole_lead(self):
        return LEAD

    def operator_facing(self):
        return LIAISON

    def add(self, sender, to, kind, meta, raw=False, publisher=None):
        if not raw:
            meta = work_tags.normalize(self, sender, to, kind, meta)
        n = len(self.messages) + 1
        message = Message.from_dict({
            "id": f"20260926-2225{n:02d}-{n:06d}-q{n:03d}", "ts": f"2026-09-26T22:25:{n:02d}.000000Z",
            "from": sender, "to": to, "kind": kind, "subject": "subject", "body": "body",
            "meta": dict(meta, **(publisher or {}))})
        self.messages.append(message)
        return message

    def task(self, rid, to, stage, sender=LEAD, kind="task", item=ITEM, raw=False, publisher=None, **extra):
        meta = {"epoch_at_send": None, "request_id": rid, "stage": stage, **extra}
        if item is not None:
            meta["work_item"] = item
        return self.add(sender, to, kind, meta, raw=raw, publisher=publisher)

    def reply(self, opener, status="done", verdict=None, raw=False):
        kind = "review-result" if opener.kind == "review-request" else "task-response"
        meta = {"in_reply_to": opener.id, "request_id": opener.meta["request_id"], "status": status}
        if verdict is not None:
            meta["verdict"] = verdict
        return self.add(opener.recipient, opener.sender, kind, meta, raw=raw)

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
    bypassed = Bus()
    reviewed(bypassed, reviewer=BUILDER)  # only the builder reviewed
    assert card(bypassed, integrated={(ITEM, HEAD): True})["reason"] == "integrated without independent GO"
    assert card(bus, integrated={(ITEM, HEAD): True})["reason"] == "integrated in configured target"


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
                          publisher={"assignee_model_vendors": {REVIEWER: "anthropic"}})
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


def test_external_deliverable_review_only_item_and_same_cycle_mixed_provenance():
    bus = Bus()
    read = bus.task("tk-read", REVIEWER, "read", work_head=HEAD, **EXTERNAL)
    bus.reply(read, verdict="GO")
    item = card(bus)
    assert item["workflow_column"] == "ready" and "external deliverable; authorship unverified" in item["issues"]
    mixed = Bus()
    mixed.reply(mixed.task("tk-read", REVIEWER, "read", work_head=HEAD, **EXTERNAL), verdict="GO")
    mixed.task("tk-build", BUILDER, "build", **POLICY)
    item = card(mixed)
    assert (item["workflow_column"], item["row"], item["reason"]) == (
        "unknown", 2, "external deliverable conflicts with recorded build dispatches")
    malformed = Bus()
    malformed.task("tk-read", REVIEWER, "read", work_head=HEAD, raw=True, **dict(EXTERNAL, external_deliverable="yes"))
    assert (card(malformed)["row"], card(malformed)["reason"]) == (2, "malformed work metadata")


def test_lead_ruling_m3_later_seat_fix_cycle_is_a_normal_build():
    bus = Bus()
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD, **EXTERNAL), verdict="GO")
    fix = bus.task("tk-build-2", BUILDER, "build", work_cycle="2", **POLICY)
    assert (card(bus)["cycle"], card(bus)["workflow_column"]) == (2, "queued")
    bus.reply(fix, verdict="done")
    bus.reply(bus.task("tk-read-2", REVIEWER2, "read", work_head=HEAD2, work_cycle="2"), verdict="GO")
    assert (card(bus)["workflow_column"], card(bus)["candidate"]) == ("ready", HEAD2)
    # The all-cycle builder set still governs independence: the cycle-2 builder cannot certify.
    self_review = Bus()
    self_review.reply(self_review.task("tk-read", REVIEWER, "read", work_head=HEAD, **EXTERNAL), verdict="GO")
    self_review.reply(self_review.task("tk-build-2", BUILDER, "build", work_cycle="2", **POLICY), verdict="done")
    self_review.reply(self_review.task("tk-read-2", BUILDER, "read", work_head=HEAD2, work_cycle="2"), verdict="GO")
    assert card(self_review)["reason"] == "no independent GO"


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


# ---- B3a cold read (codex, 2026-09-26): the reviewer's exact sequences ----

def built(bus, **policy):
    build = bus.task("tk-build", BUILDER, "build", **(policy or POLICY))
    bus.reply(build, verdict="done")
    return build


def test_f1_rescinded_needs_info_hold_is_not_erased_by_an_unrelated_go():
    bus = Bus()
    built(bus)
    bus.reply(bus.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="GO")
    r2 = bus.task("tk-r2", REVIEWER2, "read", kind="review-request", work_head=HEAD)
    hold = bus.reply(r2, status="needs-info", verdict="HOLD")
    item = card(bus)
    assert item["workflow_column"] == "independent_review"
    assert {"reviewer": REVIEWER2, "verdict": "HOLD", "reply": hold.id, "independent": True,
            "vendor": "unverified"} in item["verdicts"][HEAD]
    bus.rescind("tk-r2", sender=LEAD, to=REVIEWER2)
    item = card(bus)
    assert (item["workflow_column"], item["reason"]) == ("unknown", "non-operator HOLD")
    assert hold.id in item["evidence"]


def test_f2_linked_design_fix_keeps_the_design_author_out_of_independent_review():
    bus = Bus()
    design = bus.task("tk-design", BUILDER, "design", **POLICY)
    bus.reply(design, verdict="done")
    bus.reply(bus.task("tk-fix", REVIEWER2, "fix", supersedes="tk-design"), verdict="done")
    bus.reply(bus.task("tk-read", BUILDER, "read", work_head=HEAD), verdict="GO")
    item = card(bus)
    assert (item["workflow_column"], item["reason"]) == ("unknown", "no independent GO")
    assert item["verdicts"][HEAD][0]["independent"] is False


def test_f3_snapshot_missing_the_superseded_opener_is_unknown():
    bus = Bus()
    built(bus)
    r1 = bus.task("tk-r1", REVIEWER, "read", work_head=HEAD)
    fix = bus.reply(r1, verdict="FIX")
    r2 = bus.task("tk-r2", REVIEWER, "delta", work_head=HEAD2, supersedes="tk-r1")
    bus.reply(r2, verdict="GO")
    assert card(bus)["workflow_column"] == "ready"
    bus.messages = [m for m in bus.messages if m.id not in {r1.id, fix.id}]
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["reason"]) == ("unknown", 2, "missing referenced opener")
    assert r2.id in item["evidence"]


def test_f4_parallel_origins_with_different_repositories_conflict():
    bus = Bus()
    repo = {"work_branch": "feature", "work_target": "main", **POLICY}
    bus.reply(bus.task("tk-build-a", BUILDER, "build", work_repo="repo-a", **repo), verdict="done")
    bus.reply(bus.task("tk-build-b", REVIEWER2, "build", work_repo="repo-b", **repo), verdict="done")
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    item = card(bus)
    assert (item["workflow_column"], item["reason"]) == ("unknown", "conflicting repository/check policies")


def test_f5_superseded_success_does_not_satisfy_a_declined_replacement():
    bus = Bus()
    built(bus)
    bus.reply(bus.task("tk-build-2", REVIEWER2, "build", supersedes="tk-build"), status="declined")
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    item = card(bus)
    assert (item["workflow_column"], item["reason"]) == ("unknown", "no successful surviving deliverable")


def test_f6_replacement_chain_discharges_the_original_fix():
    bus = Bus()
    built(bus)
    bus.reply(bus.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="FIX")
    bus.reply(bus.task("tk-r2", REVIEWER2, "delta", work_head=HEAD2, supersedes="tk-r1"), status="declined")
    bus.reply(bus.task("tk-r3", REVIEWER, "delta", work_head=HEAD2, supersedes="tk-r2"), verdict="GO")
    item = card(bus)
    assert (item["workflow_column"], item["candidate"]) == ("ready", HEAD2)


def test_f7_pending_review_of_a_second_head_is_a_candidate_conflict_first():
    bus = Bus()
    built(bus)
    bus.reply(bus.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="GO")
    second = bus.task("tk-r2", REVIEWER2, "read", work_head=HEAD2)
    item = card(bus)
    assert (item["workflow_column"], item["row"], item["reason"]) == (
        "unknown", 2, "multiple candidates without supersession")
    assert second.id in item["evidence"]


def test_f8_malformed_historical_numbers_are_a_per_item_unknown():
    for key in ("work_cycle", "work_round"):
        bus = Bus()
        bad = bus.task("tk-old", BUILDER, "build", raw=True, **{key: "garbage"})
        bus.reply(bus.task("tk-other", BUILDER, "build", item="other-item", **POLICY), verdict="done")
        out = W.reduce(bus.messages, lead=LEAD)
        item = next(i for i in out["items"] if i["work_item"] == ITEM)
        assert (item["workflow_column"], item["row"], item["reason"], item["evidence"]) == (
            "unknown", 2, "malformed work metadata", [bad.id])
        assert next(i for i in out["items"] if i["work_item"] == "other-item")["reason"] == "no review dispatched"


# ---- B3b adversarial cases beyond the cold read ----

def scenarios():
    ready, fixed, chain, fan, rescinded = Bus(), Bus(), Bus(), Bus(), Bus()
    reviewed(ready)
    reviewed(fixed, verdict="FIX")
    built(chain)
    chain.reply(chain.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="FIX")
    chain.reply(chain.task("tk-r2", REVIEWER2, "delta", work_head=HEAD2, supersedes="tk-r1"), status="declined")
    chain.task("tk-r3", REVIEWER, "delta", work_head=HEAD2, supersedes="tk-r2")
    built(fan)
    group = [fan.task("tk-group", to, "read", work_head=HEAD) for to in (REVIEWER, REVIEWER2)]
    fan.reply(group[0], verdict="GO")
    built(rescinded)
    rescinded.reply(rescinded.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="GO")
    rescinded.reply(rescinded.task("tk-r2", REVIEWER2, "read", kind="review-request", work_head=HEAD),
                    status="needs-info", verdict="HOLD")
    rescinded.rescind("tk-r2", sender=LEAD, to=REVIEWER2)
    return [ready, fixed, chain, fan, rescinded]


def summary(messages):
    return [(i["work_item"], i["workflow_column"], i["reason"], i["candidate"])
            for i in W.reduce(messages, lead=LEAD)["items"]]


def test_every_ordering_and_writer_clock_skew_gives_the_same_placement():
    # Deterministic test shuffles, not security randomness.
    rng = random.Random(207)  # noqa: S311  # nosec B311
    for bus in scenarios():
        expected = W.reduce(bus.messages, lead=LEAD)
        for _ in range(40):
            shuffled = bus.messages[:]
            rng.shuffle(shuffled)
            assert W.reduce(shuffled, lead=LEAD) == expected
            # Skew: every envelope receives another envelope's ID; links follow the renaming.
            ids = [m.id for m in bus.messages]
            rng.shuffle(ids)
            rename = dict(zip([m.id for m in bus.messages], ids, strict=True))
            skewed = [replace(m, id=rename[m.id], meta=dict(m.meta, **(
                {"in_reply_to": rename[m.meta["in_reply_to"]]} if "in_reply_to" in m.meta else {})))
                for m in shuffled]
            assert summary(skewed) == summary(bus.messages)


def test_fan_out_review_needs_every_recipient():
    bus = scenarios()[3]
    assert card(bus)["workflow_column"] == "independent_review"
    group = [m for m in bus.messages if m.meta.get("request_id") == "tk-group" and m.kind == "task"]
    bus.reply(group[1], verdict="GO")
    assert card(bus)["workflow_column"] == "ready"
    split = Bus()
    built(split)
    pair = [split.task("tk-group", to, "read", work_head=HEAD) for to in (REVIEWER, REVIEWER2)]
    split.reply(pair[0], verdict="GO")
    split.reply(pair[1], verdict="FIX")
    assert card(split)["workflow_column"] == "fix_round"


def test_fan_out_build_needs_every_recipient():
    bus = Bus()
    pair = [bus.task("tk-build", to, "build", **POLICY) for to in (BUILDER, REVIEWER2)]
    bus.reply(pair[0], verdict="done")
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert (card(bus)["workflow_column"], card(bus)["reason"]) == ("queued", "start unconfirmed")


def test_rescind_and_supersede_interleavings():
    cancelled = Bus()
    cancelled.task("tk-build", BUILDER, "build", **POLICY)
    cancelled.reply(cancelled.task("tk-build-2", REVIEWER2, "build", supersedes="tk-build"), verdict="done")
    cancelled.reply(cancelled.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert card(cancelled)["workflow_column"] == "queued"  # superseding is not cancelling
    cancelled.rescind("tk-build", sender=LEAD)
    assert card(cancelled)["workflow_column"] == "ready"
    withdrawn = Bus()
    built(withdrawn)
    withdrawn.task("tk-build-2", REVIEWER2, "build", supersedes="tk-build")
    withdrawn.rescind("tk-build-2", sender=LEAD, to=REVIEWER2)
    withdrawn.reply(withdrawn.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert card(withdrawn)["reason"] == "no successful surviving deliverable"
    withdrawn_fix = Bus()
    built(withdrawn_fix)
    withdrawn_fix.reply(withdrawn_fix.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="FIX")
    withdrawn_fix.task("tk-r2", REVIEWER2, "delta", work_head=HEAD2, supersedes="tk-r1")
    assert card(withdrawn_fix)["reason"] == "awaiting replacement review"
    withdrawn_fix.rescind("tk-r2", sender=LEAD, to=REVIEWER2)
    assert (card(withdrawn_fix)["workflow_column"], card(withdrawn_fix)["reason"]) == (
        "fix_round", "unresolved FIX without replacement review")


def test_partial_snapshots_never_look_complete():
    bus = Bus()
    build = built(bus)
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert card(bus)["workflow_column"] == "ready"
    no_opener = [m for m in bus.messages if m.id != build.id]
    item = next(i for i in W.reduce(no_opener, lead=LEAD)["items"] if i["work_item"] == ITEM)
    assert (item["row"], item["reason"]) == (2, "reply has no available opener")
    no_reply = [m for m in bus.messages if m.meta.get("in_reply_to") != build.id]
    assert next(i for i in W.reduce(no_reply, lead=LEAD)["items"])["workflow_column"] == "queued"


def test_historical_branching_cyclic_or_cross_cycle_supersession_is_unknown():
    branching = Bus()
    built(branching)
    branching.reply(branching.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="FIX")
    for rid in ("tk-r2", "tk-r3"):
        branching.task(rid, REVIEWER2, "delta", work_head=HEAD2, supersedes="tk-r1", raw=True)
    assert card(branching)["reason"] == "ambiguous branching replacements"
    cyclic = Bus()
    built(cyclic)
    cyclic.task("tk-r1", REVIEWER, "read", work_head=HEAD, supersedes="tk-r2", raw=True)
    cyclic.task("tk-r2", REVIEWER, "read", work_head=HEAD, supersedes="tk-r1", raw=True)
    assert card(cyclic)["reason"] == "supersession cycle"
    crossing = Bus()
    built(crossing)
    crossing.task("tk-build-2", BUILDER, "build", work_cycle="2", supersedes="tk-build", raw=True, **POLICY)
    assert card(crossing)["reason"] == "supersedes crosses work cycles"


def test_hold_discharged_only_by_a_successful_replacement():
    bus = scenarios()[4]  # rescinded needs-info HOLD beside an unrelated GO
    assert card(bus)["reason"] == "non-operator HOLD"
    bus.reply(bus.task("tk-r3", REVIEWER2, "read", work_head=HEAD, supersedes="tk-r2"), verdict="GO")
    assert card(bus)["workflow_column"] == "ready"


def test_unlinked_fix_in_a_design_cycle_leaves_purpose_unknown():
    bus = Bus()
    bus.reply(bus.task("tk-design", BUILDER, "design", **POLICY), verdict="done")
    bus.reply(bus.task("tk-fix", REVIEWER2, "fix"), verdict="done")
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    item = card(bus)
    assert (item["workflow_column"], item["reason"]) == (
        "unknown", "fix purpose unknown: no supersedes ancestry to the design task")


def test_declined_parallel_execution_needs_its_own_replacement():
    bus = Bus()
    built(bus)
    bus.reply(bus.task("tk-build-b", REVIEWER2, "build", **POLICY), status="declined")
    bus.reply(bus.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert card(bus)["reason"] == "declined execution without replacement"
    bus.reply(bus.task("tk-build-c", BUILDER, "build", supersedes="tk-build-b"), verdict="done")
    assert card(bus)["workflow_column"] == "ready"


def test_rescinded_review_does_not_pin_a_competing_candidate():
    bus = Bus()
    built(bus)
    bus.reply(bus.task("tk-r1", REVIEWER, "read", work_head=HEAD), verdict="GO")
    bus.task("tk-r2", REVIEWER2, "read", work_head=HEAD2)
    assert card(bus)["reason"] == "multiple candidates without supersession"
    bus.rescind("tk-r2", sender=LEAD, to=REVIEWER2)
    assert (card(bus)["workflow_column"], card(bus)["candidate"]) == ("ready", HEAD)


def test_pre_b2a_history_without_verdict_or_with_text_false_is_not_green():
    # Live history holds task-responses with status=done and no verdict/verdict_issue (pre-B2a publication).
    legacy = Bus()
    legacy.reply(legacy.task("tk-build", BUILDER, "build", **POLICY), raw=True)
    legacy.reply(legacy.task("tk-read", REVIEWER, "read", work_head=HEAD), verdict="GO")
    assert (card(legacy)["workflow_column"], card(legacy)["reason"]) == ("unknown", "verdict missing")
    # A historical text "false" is a valid false declaration, never an external deliverable.
    text_false = Bus()
    declared = dict(EXTERNAL, external_deliverable="false")
    text_false.reply(text_false.task("tk-read", REVIEWER, "read", work_head=HEAD, raw=True, **declared),
                     verdict="GO", raw=True)
    assert card(text_false)["reason"] == "no successful surviving deliverable"
