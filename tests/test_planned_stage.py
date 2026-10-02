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


def test_planned_state_unattributable_record_is_silently_ignored() -> None:
    # A planned-kind message with no string work_item at all (hand-edited/
    # corrupt - store.send itself always refuses this, see above) cannot be
    # blamed on any item - it contributes nothing.
    from test_work_board_reducer import LEAD, Bus
    bus = Bus()
    bus.add(LEAD, LEAD, "planned", {}, raw=True)
    assert work_board.planned_state(bus.messages, set()) == {}


# ----------------------------------------------------------- _audit isolation (F4/F7a)

def test_audit_one_never_attributes_a_planned_record_to_real_work() -> None:
    from test_work_board_reducer import BUILDER, ITEM, LEAD, Bus
    bus = Bus()
    build = bus.task("tk-1", BUILDER, "build")
    bus.reply(build, verdict="done")
    reduced_before = work_board.reduce(bus.messages, lead=LEAD)
    # A planned record with a FORBIDDEN in_reply_to (the replay validator
    # already refuses this as unsupported metadata) must not reach _audit's
    # attribution machinery via owners() at all - added RAW (bypassing
    # normalize(), the same way a hand-edited/legacy log entry would).
    bus.add(LEAD, LEAD, "planned", {"work_item": ITEM, "in_reply_to": "nonexistent-id"}, raw=True)
    reduced_after = work_board.reduce(bus.messages, lead=LEAD)
    assert reduced_after["items"] == reduced_before["items"]
    assert reduced_after["unassigned"] == reduced_before["unassigned"]


def test_work_board_feed_activity_excludes_planned_records() -> None:
    from test_work_board_feed import project
    from test_work_board_reducer import BUILDER, ITEM, LEAD, Bus
    bus = Bus()
    build = bus.task("tk-1", BUILDER, "build")
    bus.reply(build, verdict="done")
    before = project(bus)
    before_event = next(i for i in before["items"] if i["work_item"] == ITEM)["last_work_event_at"]
    bus.add(LEAD, LEAD, "planned", {"work_item": ITEM, "work_title": "Still planned?"})
    after = project(bus)
    after_event = next(i for i in after["items"] if i["work_item"] == ITEM)["last_work_event_at"]
    assert after_event == before_event


# ----------------------------------------------------------- CLI: transition table (F2)

def test_cli_plan_add_then_add_again_refuses(store: Store, store_root) -> None:
    store.set_role("alpha", "lead")
    assert _run(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                "--work-title", "T"], store_root) == 0
    _run_expect_exit(["board", "plan", "add", "--from", "alpha", "--work-item", "demo",
                     "--work-title", "T2"], store_root, 2)


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
