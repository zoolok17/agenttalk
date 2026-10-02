"""issue #279, build part 1: the `planned` record publish path and state machine.

docs/DESIGN-planned-stage.md is the contract this tests against: §1 (record
format), §2 (replaces-chain lifecycle/F2 state machine), §3 (publisher gate/
F6), §4 (malformed isolation/F4), and the activity exclusion (F7a). Part 2
(the feed projection, console lane) is explicitly out of scope here.
"""

from __future__ import annotations

import pytest

from agenttalk import cli, work_board, work_tags
from agenttalk.store import CONTROL_KINDS, KNOWN_KINDS, OPENER_KINDS, Store
from agenttalk.wrapper import recv_api


def _run(argv: list[str], root) -> int:
    return cli.main(["--root", str(root), *argv])


def _run_expect_exit(argv: list[str], root, code: int) -> None:
    try:
        rc = cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        rc = e.code
    assert rc == code


# ----------------------------------------------------------- kind registration

def test_planned_kind_registered_not_opener_not_stage() -> None:
    assert "planned" in KNOWN_KINDS
    assert "planned" not in OPENER_KINDS
    assert "planned" not in work_tags.OPENERS
    assert "planned" not in work_tags.STAGES
    assert "planned" in CONTROL_KINDS


# ----------------------------------------------------------- work_tags.validate_planned

def test_validate_planned_accepts_add_and_change_and_withdraw_shapes() -> None:
    add = work_tags.validate_planned({"work_item": "demo-item", "work_title": "Demo"})
    assert add == {"work_item": "demo-item", "work_title": "Demo"}
    change = work_tags.validate_planned(
        {"work_item": "demo-item", "work_title": "Demo 2", "replaces": "m1"})
    assert change["replaces"] == "m1"
    withdraw = work_tags.validate_planned(
        {"work_item": "demo-item", "withdrawn": True, "replaces": "m2"})
    assert withdraw == {"work_item": "demo-item", "withdrawn": True, "replaces": "m2"}


@pytest.mark.parametrize("bad_meta,fragment", [
    ({"work_title": "x"}, "requires work_item"),
    ({"work_item": "demo", "work_title": "   "}, "nonblank work_title"),
    ({"work_item": "demo", "work_title": ""}, "nonblank work_title"),
    ({"work_item": "demo"}, "requires work_title"),
    ({"work_item": "demo", "withdrawn": False}, "withdrawn must be true"),
    ({"work_item": "demo", "withdrawn": "true"}, "withdrawn must be true"),
    ({"work_item": "demo", "withdrawn": True, "work_title": "x"}, "must not carry work_title"),
    ({"work_item": "demo", "withdrawn": True}, "requires replaces"),
    ({"work_item": "demo", "work_title": "x", "replaces": ""}, "nonblank message id"),
    ({"work_item": "demo", "work_title": "x", "replaces": 5}, "nonblank message id"),
    ({"work_item": "demo", "work_title": "x", "attention": {}}, "unsupported metadata"),
    ({"work_item": "demo", "work_title": "x", "request_id": "r1"}, "unsupported metadata"),
    ({"work_item": "demo", "work_title": "x", "in_reply_to": "m1"}, "unsupported metadata"),
    ({"work_item": "demo", "work_title": "x", "stage": "build"}, "unsupported metadata"),
    ({"work_item": "demo", "work_title": "x", "supersedes": "r1"}, "unsupported metadata"),
    ({"work_item": "demo", "work_title": "x", "owner": "a"}, "unsupported metadata"),
    ("not a dict", "must be an object"),
    ({"work_item": "NOT A SLUG", "work_title": "x"}, "lowercase slug"),
])
def test_validate_planned_refuses(bad_meta, fragment) -> None:
    with pytest.raises(ValueError, match=fragment):
        work_tags.validate_planned(bad_meta)


# ----------------------------------------------------------- Store.send integration

def test_store_send_planned_runs_the_shared_validator(store: Store) -> None:
    store.set_role("alpha", "lead")
    msg = store.send(sender="alpha", recipient="alpha", kind="planned", body="",
                     meta={"work_item": "demo-item", "work_title": "Demo"})
    assert msg.sender == msg.recipient == "alpha"
    assert msg.meta == {"work_item": "demo-item", "work_title": "Demo"}
    with pytest.raises(ValueError, match="unsupported metadata"):
        store.send(sender="alpha", recipient="alpha", kind="planned", body="",
                   meta={"work_item": "demo-item", "work_title": "x", "request_id": "r1"})


# ----------------------------------------------------------- work_board.planned_state

def _planned(store: Store, *, work_item, title=None, withdrawn=None, replaces=None, sender="alpha"):
    meta = {"work_item": work_item}
    if withdrawn:
        meta["withdrawn"] = True
    else:
        meta["work_title"] = title
    if replaces:
        meta["replaces"] = replaces
    return store.send(sender=sender, recipient=sender, kind="planned", body="", meta=meta)


def test_planned_state_absent_for_unknown_item(store: Store) -> None:
    assert work_board.planned_state([], set()) == {}


def test_planned_state_active_then_change_then_withdraw(store: Store) -> None:
    m1 = _planned(store, work_item="demo", title="First")
    state = work_board.planned_state(store.valid_messages(), set())
    assert state["demo"] == {"state": "active", "title": "First", "current_id": m1.id, "reason": None}
    m2 = _planned(store, work_item="demo", title="Second", replaces=m1.id)
    state = work_board.planned_state(store.valid_messages(), set())
    assert state["demo"] == {"state": "active", "title": "Second", "current_id": m2.id, "reason": None}
    m3 = _planned(store, work_item="demo", withdrawn=True, replaces=m2.id)
    state = work_board.planned_state(store.valid_messages(), set())
    assert state["demo"]["state"] == "withdrawn"
    assert state["demo"]["current_id"] == m3.id


def test_planned_state_is_order_independent_shuffled_input(store: Store) -> None:
    m1 = _planned(store, work_item="demo", title="First")
    m2 = _planned(store, work_item="demo", title="Second", replaces=m1.id)
    msgs = store.valid_messages()
    forward = work_board.planned_state(msgs, set())
    backward = work_board.planned_state(list(reversed(msgs)), set())
    assert forward == backward == {
        "demo": {"state": "active", "title": "Second", "current_id": m2.id, "reason": None}}


def test_planned_state_missing_predecessor_is_unknown(store: Store) -> None:
    _planned(store, work_item="demo", title="Only", replaces="no-such-message-id")
    state = work_board.planned_state(store.valid_messages(), set())
    assert state["demo"]["state"] == "unknown"
    assert "does not resolve" in state["demo"]["reason"]


def test_planned_state_self_edge_is_unknown(store: Store) -> None:
    # A record cannot name itself before its own id exists; simulate via a
    # second record whose replaces is forged to equal its own id.
    m = _planned(store, work_item="demo", title="Loop")
    msgs = list(store.valid_messages())
    msgs[-1] = msgs[-1].__class__(**{**msgs[-1].__dict__, "meta": {**msgs[-1].meta, "replaces": m.id}})
    state = work_board.planned_state(msgs, set())
    assert state["demo"]["state"] == "unknown"
    assert "names itself" in state["demo"]["reason"]


def test_planned_state_cross_item_replaces_is_unknown(store: Store) -> None:
    m1 = _planned(store, work_item="alpha-item", title="A")
    _planned(store, work_item="beta-item", title="B", replaces=m1.id)
    state = work_board.planned_state(store.valid_messages(), set())
    # alpha-item's own chain is untouched (nothing replaces it FOR alpha-item);
    # beta-item's replaces points across items, so beta-item is unknown.
    assert state["alpha-item"]["state"] == "active"
    assert state["beta-item"]["state"] == "unknown"
    assert "does not resolve" in state["beta-item"]["reason"]


def test_planned_state_cycle_is_unknown(store: Store) -> None:
    m1 = _planned(store, work_item="demo", title="A")
    m2 = _planned(store, work_item="demo", title="B", replaces=m1.id)
    msgs = list(store.valid_messages())
    # Forge m1 to also replace m2, creating a 2-cycle (bypassing the CLI,
    # which could never construct this - exactly the "hand-edited log" case
    # replay must still handle).
    patched = []
    for m in msgs:
        if m.id == m1.id:
            m = m.__class__(**{**m.__dict__, "meta": {**m.meta, "replaces": m2.id}})
        patched.append(m)
    state = work_board.planned_state(patched, set())
    assert state["demo"]["state"] == "unknown"
    assert "cycle" in state["demo"]["reason"]


def test_planned_state_competing_tips_is_unknown(store: Store) -> None:
    m1 = _planned(store, work_item="demo", title="First")
    _planned(store, work_item="demo", title="Branch A", replaces=m1.id)
    _planned(store, work_item="demo", title="Branch B", replaces=m1.id)
    state = work_board.planned_state(store.valid_messages(), set())
    assert state["demo"]["state"] == "unknown"
    assert "competing" in state["demo"]["reason"]


def test_planned_state_broken_chain_reason_is_order_independent() -> None:
    # issue #279 F8: a self-edge fault and a missing-predecessor fault on two
    # DIFFERENT records for the same work_item must report the SAME reason
    # regardless of which record is scanned first - a stable priority, not
    # iteration order, decides.
    from dataclasses import replace
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    m = bus.add(LEAD, LEAD, "planned", {"work_item": "demo", "work_title": "Seed"})
    a = replace(m, id="a", meta={"work_item": "demo", "work_title": "A", "replaces": "a"})
    b = replace(m, id="b", meta={"work_item": "demo", "work_title": "B", "replaces": "missing"})
    forward = work_board.planned_state([a, b], set())
    reverse = work_board.planned_state([b, a], set())
    assert forward == reverse
    assert "names itself" in forward["demo"]["reason"]


def test_planned_state_refuses_a_non_self_addressed_record_on_replay() -> None:
    # issue #279 F9: self-addressing is a structural shape property, checked
    # on replay too, not just at publish time - a stored record from lead to
    # dev (e.g. a pre-F9 log entry, or a hand edit) must not be accepted as
    # an active plan.
    from dataclasses import replace
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    m = bus.add(LEAD, LEAD, "planned", {"work_item": "demo", "work_title": "Plan"})
    nonself = replace(m, recipient="dev")
    state = work_board.planned_state([nonself], set())
    assert state["demo"]["state"] == "unknown"
    assert "malformed" in state["demo"]["reason"]


def test_planned_state_malformed_record_in_chain_is_unknown() -> None:
    # A malformed planned record can only ever exist via a path that
    # bypasses today's publish-time validator (a hand edit, or a future
    # schema's "valid at the time" record) - `store.send` itself always
    # refuses this (tested separately above), so this uses the same `Bus`
    # `raw=True` escape hatch the _audit isolation test uses to simulate
    # "already in the log".
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    bus.add(LEAD, LEAD, "planned", {"work_item": "demo", "work_title": "Good"})
    bus.add(LEAD, LEAD, "planned", {"work_item": "demo"}, raw=True)  # missing work_title
    state = work_board.planned_state(bus.messages, set())
    assert state["demo"]["state"] == "unknown"
    assert "malformed" in state["demo"]["reason"]


def test_planned_state_promotion_wins_over_a_broken_chain(store: Store) -> None:
    m1 = _planned(store, work_item="demo", title="First")
    _planned(store, work_item="demo", title="Branch A", replaces=m1.id)
    _planned(store, work_item="demo", title="Branch B", replaces=m1.id)  # competing tips
    state = work_board.planned_state(store.valid_messages(), {"demo"})
    assert state["demo"] == {"state": "promoted", "title": None, "current_id": None, "reason": None}


def test_planned_state_withdrawal_without_replaces_is_not_accepted_as_terminal() -> None:
    # issue #279 F4: a withdrawal with nothing to withdraw must never be
    # accepted as a rootless terminal state - store.send itself now refuses
    # this at publish time (tested above), so this simulates a record that
    # bypassed that validator (a hand edit, or a pre-F4 log entry) the same
    # way test_planned_state_malformed_record_in_chain_is_unknown does.
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    bus.add(LEAD, LEAD, "planned", {"work_item": "demo", "withdrawn": True}, raw=True)
    state = work_board.planned_state(bus.messages, set())
    assert state["demo"]["state"] == "unknown"
    assert "malformed" in state["demo"]["reason"]


def test_planned_state_unattributable_record_is_silently_ignored() -> None:
    # A planned-kind message with no string work_item at all (hand-edited/
    # corrupt - store.send itself always refuses this, see above) cannot be
    # blamed on any item - it contributes nothing.
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    bus.add(LEAD, LEAD, "planned", {}, raw=True)
    assert work_board.planned_state(bus.messages, set()) == {}


# ----------------------------------------------------------- _audit isolation (F4/F7a)

# -------------------------------------- fix round 3: partition at the entry (F12-F14)

def test_reduce_and_feed_are_byte_identical_with_any_number_of_planned_records() -> None:
    """issue #279 fix round 3: work_board.reduce() and work_board_feed.build()
    each partition `real`/`planned` ONCE at their own entry - every real-work
    path (audit, by_id/parent, ancestry, closure, activity, attribution,
    coverage) sees ONLY `real`. This is the one structural regression test
    the lead asked fix round 3 to add in place of the per-site isolation
    tests it overlaps with: a rich mix of planned records - a valid
    add/change/withdraw chain, a malformed one (a forbidden field, added raw
    the way a hand-edited log entry would be), and one a real, unrelated
    message replies to via in_reply_to - must leave BOTH reduce()'s items
    and the full feed byte-identical to the same fixture with no planned
    records at all."""
    import json
    from dataclasses import replace
    from test_work_board_feed import project
    from test_work_board_reducer import BUILDER, HEAD, ITEM, LEAD, reviewed, Bus

    # The same note (anchored on an unresolved id either way) is present in
    # BOTH fixtures, so the only variable between them is the planned
    # records themselves - "before" anchors it on an ordinary nonexistent
    # id (master's own baseline for an unresolved anchor); "after" anchors
    # the identical note on a planned record's id instead. Both must
    # produce the identical "missing required correlation history" fate.
    def real_fixture(note_anchor):
        bus = Bus()
        reviewed(bus)
        bus.task("tk-other", BUILDER, "build", item="other-item")
        bus.add(LEAD, LEAD, "note", {"in_reply_to": note_anchor}, raw=True)
        return bus

    bus = real_fixture("nonexistent-id")
    before_reduce = work_board.reduce(bus.messages, lead=LEAD)
    before_feed = project(bus, integrated={(ITEM, HEAD): True})

    bad_plan_id = "20260926-222511-000099-qPLN"
    bus = real_fixture(bad_plan_id)
    m1 = bus.add(LEAD, LEAD, "planned", {"work_item": "demo-a", "work_title": "A"})
    bus.add(LEAD, LEAD, "planned", {"work_item": "demo-a", "work_title": "A2", "replaces": m1.id})
    m3 = bus.add(LEAD, LEAD, "planned", {"work_item": "demo-b", "work_title": "B"})
    bus.add(LEAD, LEAD, "planned", {"work_item": "demo-b", "withdrawn": True, "replaces": m3.id})
    bad_plan = replace(bus.add(LEAD, LEAD, "planned", {"work_item": "demo-c"}, raw=True), id=bad_plan_id)
    bus.messages[-1] = bad_plan
    after_reduce = work_board.reduce(bus.messages, lead=LEAD)
    after_feed = project(bus, integrated={(ITEM, HEAD): True})

    assert json.dumps(after_reduce["items"], sort_keys=True) == json.dumps(before_reduce["items"], sort_keys=True)
    assert json.dumps(after_reduce["unassigned"], sort_keys=True) == \
        json.dumps(before_reduce["unassigned"], sort_keys=True)
    assert json.dumps(after_feed["items"], sort_keys=True) == json.dumps(before_feed["items"], sort_keys=True)
    assert json.dumps(after_feed["coverage"], sort_keys=True) == json.dumps(before_feed["coverage"], sort_keys=True)


def test_f12_conflicting_duplicate_planned_id_is_unknown_not_last_write_wins() -> None:
    """issue #279 F12 (replay): the same planned id can legitimately appear
    twice (once in active storage, once in compacted) - an identical copy
    dedupes silently, but a CONFLICTING payload under the same id must make
    the item Unknown, never "whichever is scanned last"."""
    from dataclasses import replace
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    m = bus.add(LEAD, LEAD, "planned", {"work_item": "demo", "work_title": "A"})
    conflicting = replace(m, meta={"work_item": "demo", "work_title": "B"})
    state = work_board.planned_state([m, conflicting], set())["demo"]
    assert state["state"] == "unknown"
    assert "conflicting" in state["reason"]
    identical = replace(m)
    state2 = work_board.planned_state([m, identical], set())["demo"]
    assert state2["state"] == "active"  # an identical duplicate is not a conflict


def test_f12_cli_refuses_a_conflicting_duplicate_id_across_partitions(store: Store, store_root, capsys) -> None:
    """issue #279 F12 (publish): board plan reads active AND compacted
    storage; a conflicting duplicate id across the two partitions must
    refuse before publication, naming the id - never silently accepted as
    complete history. Asserts read_history()'s OWN message (not just the
    exit code), which would otherwise also be produced, for a different
    reason, by planned_state()'s independent replay-side conflict check."""
    import json
    store.set_role("alpha", "lead")
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T"], store_root) == 0
    m = store.valid_messages()[0]
    store.compacted_dir.mkdir(parents=True, exist_ok=True)
    conflicting = dict(m.to_dict())
    conflicting["meta"] = {**conflicting["meta"], "work_title": "Conflicting"}
    (store.compacted_dir / f"{m.id}.json").write_text(json.dumps(conflicting), encoding="utf-8")
    try:
        rc = cli.main(["--root", str(store_root), "board", "plan", "change", "--from", "alpha",
                      "--work-item", "demo", "--work-title", "T2"])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    err = capsys.readouterr().err
    assert m.id in err and "conflicting payloads across active/compacted storage" in err


def test_f13_plan_anchored_note_does_not_revive_a_pruned_done_card() -> None:
    """issue #279 F13: an ordinary note with in_reply_to=<plan id> must not
    revive a pruned Done card - the activity walker's by_id must never
    resolve a planned record as an ancestry anchor."""
    from dataclasses import replace
    from datetime import timedelta
    from test_work_board_feed import project, NOW
    from test_work_board_reducer import HEAD, ITEM, LEAD, reviewed, Bus
    bus = Bus()
    reviewed(bus)
    old = [replace(m, ts=(NOW - timedelta(days=8)).isoformat()) for m in bus.messages]
    plan = replace(Bus.add(bus, LEAD, LEAD, "planned", {"work_item": ITEM, "work_title": "Demo"}),
                   ts=(NOW - timedelta(days=8)).isoformat())
    note = replace(Bus.add(bus, LEAD, LEAD, "note", {"in_reply_to": plan.id}, raw=True), ts=NOW.isoformat())
    bus.messages = [*old, plan, note]
    feed = project(bus, integrated={(ITEM, HEAD): True})
    assert not any(i["work_item"] == ITEM for i in feed["items"])


def test_f14_reply_anchored_only_on_a_plan_does_not_complete_the_real_task() -> None:
    """issue #279 F14: a stored plan carrying a forbidden request_id (a hand
    edit/pre-fix log entry, bypassing validate_planned's own refusal of that
    field) must not let a task-response anchored on it via in_reply_to
    resolve to, and complete, the REAL task sharing that request_id."""
    from test_work_board_reducer import BUILDER, ITEM, LEAD, Bus
    bus = Bus()
    bus.task("tk-real", BUILDER, "build")
    raw_plan = bus.add(LEAD, LEAD, "planned",
                       {"work_item": ITEM, "work_title": "Demo", "request_id": "tk-real"}, raw=True)
    bus.add(BUILDER, LEAD, "task-response",
           {"in_reply_to": raw_plan.id, "status": "done", "verdict": "done"}, raw=True)
    result = work_board.reduce(bus.messages, lead=LEAD)
    item = next(i for i in result["items"] if i["work_item"] == ITEM)
    assert item["obligations"][0]["state"] == "outstanding"


def test_raw_planned_record_sharing_request_id_does_not_attribute_a_real_card() -> None:
    """issue #279 F3(c): a raw planned record that happens to share a
    request_id with a malformed/detached real opener must not be used to
    attribute (or thereby fabricate) a real card for the planned record's
    own work_item - the detached opener's own "ambiguous/malformed" fate is
    unchanged by the planned record's presence."""
    from dataclasses import replace
    from test_work_board_reducer import BUILDER, ITEM, LEAD, Bus
    bus = Bus()
    m = bus.task("tk-bad", BUILDER, "build")
    bad_task = replace(m, id="task", kind="task", recipient="dev",
                        meta={"request_id": "tk-bad", "work_item": "BAD SLUG", "stage": "build"})
    raw_plan = replace(m, id="plan", kind="planned", sender=LEAD, recipient=LEAD,
                        meta={"work_item": ITEM, "work_title": "Demo", "request_id": "tk-bad"})
    before = work_board.reduce([bad_task], lead=LEAD)
    after = work_board.reduce([bad_task, raw_plan], lead=LEAD)
    assert before["items"] == after["items"] == []


# ----------------------------------------------------------- CLI: transition table (F2)

def test_cli_plan_add_then_add_again_refuses(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T"], store_root) == 0
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T2"], store_root, 2)


# ----------------------------------------------------------- F1: archive-aware, coverage-checked history

def test_cli_plan_add_refuses_when_an_active_plan_was_archived(store: Store, store_root) -> None:
    # issue #279 F1: `store.valid_messages()` alone omits compacted/archived
    # history, so after the original plan is archived a second `add` used to
    # see "absent" and wrongly succeed, leaving two competing roots. The
    # archive-aware read must still see it and refuse.
    store.set_role("alpha", "lead")
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T"], store_root) == 0
    assert store.archive_messages_below("z")
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T2"], store_root, 2)


def test_cli_plan_add_refuses_against_an_archived_real_opener(store: Store, store_root) -> None:
    # issue #279 F1+F6: a real dispatch that has since been archived must
    # still count toward promotion - an invisible real opener must never
    # let `add` through.
    store.set_role("alpha", "lead")
    store.send(sender="alpha", recipient="beta", kind="task", body="work",
              meta={"work_item": "demo", "request_id": "tk-demo", "stage": "build"})
    assert store.archive_messages_below("z")
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T"], store_root, 2)


def test_cli_plan_add_refuses_when_an_active_envelope_is_corrupt(store: Store, store_root) -> None:
    # issue #279 F1: a corrupt/unreadable active envelope used to be
    # silently dropped by `store.valid_messages()`, letting `add` through
    # despite history completeness being impossible to establish.
    store.set_role("alpha", "lead")
    (store.messages_dir / "bad.json").write_text("{broken", encoding="utf-8")
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T"], store_root, 2)


# ----------------------------------------------------------- F2: atomic check-then-publish

def test_cli_plan_concurrent_adds_exactly_one_wins(store: Store, store_root, monkeypatch) -> None:
    # issue #279 F2: deterministic interleave at the pre-send boundary (the
    # reviewer's own technique) - a second, fully independent `add` command
    # completes AFTER the first has decided "absent" but BEFORE the first's
    # own write lands. The precheck re-validates atomically with the append:
    # exactly one command succeeds, the other refuses, and the replayed
    # state is a clean "active" (never two competing roots/"unknown").
    from agenttalk.store import Store as StoreClass
    store.set_role("alpha", "lead")
    original_send = StoreClass.send
    entered = {"value": False}

    def interleaved_send(self, **kwargs):
        if kwargs.get("kind") == "planned" and not entered["value"]:
            entered["value"] = True
            inner_rc = _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                            "--work-title", "Inner"], store_root)
            assert inner_rc == 0
        return original_send(self, **kwargs)

    monkeypatch.setattr(StoreClass, "send", interleaved_send)
    outer_rc = _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                    "--work-title", "Outer"], store_root)
    assert outer_rc == 2
    state = work_board.planned_state(store.valid_messages(), set())["demo"]
    assert state["state"] == "active"
    assert state["title"] == "Inner"


# ----------------------------------------------------------- F6: promotion is a real opener only

def test_cli_plan_add_not_blocked_by_an_orphan_note(store: Store, store_root) -> None:
    # issue #279 F6: an ordinary note with a dangling in_reply_to and a
    # matching work_item tag is NOT a real opener - it must not permanently
    # block `add` for a work_item nothing real was ever dispatched for.
    store.set_role("alpha", "lead")
    store.send(sender="alpha", recipient="alpha", kind="note", body="",
              meta={"work_item": "demo", "in_reply_to": "missing"})
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T"], store_root) == 0


def test_cli_plan_change_without_existing_refuses(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    _run_expect_exit(["board", "plan", "change", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T"], store_root, 2)


def test_cli_plan_withdraw_without_existing_refuses(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    _run_expect_exit(["board", "plan", "withdraw", "--from", "alpha", "--work-item", "demo"],
                     store_root, 2)


def test_cli_plan_full_lifecycle_and_terminal_states(
    store: Store, store_root, capsys: pytest.CaptureFixture,
) -> None:
    store.set_role("alpha", "lead")
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T1"], store_root) == 0
    capsys.readouterr()
    assert _run(["board", "plan", "change", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T2"], store_root) == 0
    assert _run(["board", "plan", "withdraw", "--from", "alpha", "--work-item", "demo"],
               store_root) == 0
    # withdrawn is terminal: add/change both refuse, naming it.
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T3"], store_root, 2)
    assert "terminal" in capsys.readouterr().err
    _run_expect_exit(["board", "plan", "change", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T3"], store_root, 2)
    assert "terminal" in capsys.readouterr().err
    _run_expect_exit(["board", "plan", "withdraw", "--from", "alpha", "--work-item", "demo"],
                     store_root, 2)
    assert "terminal" in capsys.readouterr().err


def test_cli_plan_refuses_against_promoted_work_item(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    store.send(sender="alpha", recipient="alpha", kind="task", subject="s", body="do it",
              meta={"work_item": "demo", "request_id": "tk-1", "stage": "build",
                    "work_title": "Real work"})
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T"], store_root, 2)


def test_cli_plan_resolves_replaces_itself_never_by_order(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo", "--work-title", "T1"],
        store_root)
    first_id = store.valid_messages()[-1].id
    _run(["board", "plan", "change", "--from", "alpha", "--work-item", "demo", "--work-title", "T2"],
        store_root)
    second = store.valid_messages()[-1]
    assert second.meta["replaces"] == first_id


def test_cli_plan_self_addressed(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo", "--work-title", "T"],
        store_root)
    msg = store.valid_messages()[-1]
    assert msg.sender == msg.recipient == "alpha"


# ----------------------------------------------------------- CLI: publisher gate (F6)

def test_cli_plan_refuses_non_lead_non_liaison(store: Store, store_root, capsys) -> None:
    store.set_role("alpha", "lead")
    _run_expect_exit(["board", "plan", "add", "--from", "beta", "--work-item", "demo",
                     "--work-title", "T"], store_root, 2)
    assert "is not the roster's lead" in capsys.readouterr().err


def test_cli_plan_allows_operator_facing_liaison_without_lead_role(store: Store, store_root) -> None:
    store.set_operator_facing("alpha")
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T"], store_root) == 0


# ----------------------------------------------------------- send/broadcast refusal

def test_send_kind_planned_rejected(store: Store, store_root, capsys) -> None:
    store.set_role("alpha", "lead")
    _run_expect_exit(["send", "--from", "alpha", "--to", "alpha", "--kind", "planned",
                     "-m", "x"], store_root, 2)
    assert "board plan add|change|withdraw" in capsys.readouterr().err


def test_broadcast_kind_planned_not_a_choice(store: Store, store_root) -> None:
    # argparse's own `choices=` already excludes "planned" the same way it
    # excludes "rescind"/"end" - no code path in cmd_broadcast ever sees it.
    _run_expect_exit(["broadcast", "--all", "--from", "alpha", "--kind", "planned",
                     "-m", "x"], store_root, 2)


# ----------------------------------------------------------- not deliverable to a turn (F1)

def test_recv_api_excludes_planned_from_default_records(store: Store) -> None:
    store.set_role("alpha", "lead")
    store.send(sender="alpha", recipient="alpha", body="real")
    store.send(sender="alpha", recipient="alpha", body="", kind="planned",
              meta={"work_item": "demo", "work_title": "T"})
    recs = recv_api.records(store, "alpha")
    assert all(r["kind"] != "planned" for r in recs)
    assert [r["body"] for r in recs] == ["real"]
    # still visible with explicit inspection, same as composing/progress today.
    all_recs = recv_api.records(store, "alpha", include_control=True)
    assert any(r["kind"] == "planned" for r in all_recs)


def test_last_received_for_skips_planned_as_default_reply_anchor(store: Store) -> None:
    store.set_role("alpha", "lead")
    real = store.send(sender="beta", recipient="alpha", body="real question")
    store.send(sender="alpha", recipient="alpha", body="", kind="planned",
              meta={"work_item": "demo", "work_title": "T"})
    assert store.last_received_for("alpha").id == real.id
