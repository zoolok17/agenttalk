"""#923 drift visibility: advisory warnings and a read-only report (criteria numbered as in the design)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agenttalk import cli, drift
from agenttalk.store import Message, Store

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=10)


@pytest.fixture(autouse=True)
def _activation(monkeypatch):
    monkeypatch.setattr(drift, "ACTIVATION", "2000-01-01T00:00:00Z")


def ts(hours):
    return (T0 + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


_n = [0]


def msg(hours, kind="task", sender="lead", to="worker", **meta):
    _n[0] += 1
    return Message(id=f"{_n[0]:06d}", ts=ts(hours), sender=sender, recipient=to, kind=kind, meta=meta)


def verdict(hours, rid, v="reshape"):
    return msg(hours, kind="message", sender="chal", to="lead", request_id=rid, challenge="true", verdict=v)


def order(hours=5, **meta):
    base = {"stage": "build", "work_item": "w", "request_id": f"tk-{_n[0]}"}
    return msg(hours, **{**base, **meta})


def warn(msgs, **meta):
    return drift.order_warnings(msgs, {"stage": "build", "work_item": "w", "allowance": "10h", **meta}, NOW)


def cls(msgs, meta, hours=5):
    return drift.classify(drift._by_rid(msgs), meta, T0 + timedelta(hours=hours))[0]


# --- criteria 2 and 3: evidence as of the order -----------------------------------------------------------
def test_the_evidence_states():
    ch = {"stage": "build", "challenge": "ch-1"}
    assert cls([verdict(1, "ch-1")], ch) == "ok"
    assert cls([], ch) == "unverifiable"
    assert cls([msg(1, kind="question", request_id="ch-1", challenge="true")], ch) == "pending"
    assert cls([verdict(1, "ch-1", "unassessed")], ch) == "unusable"
    assert cls([verdict(6, "ch-1")], ch) == "late"          # a verdict after the order never helps it
    assert cls([verdict(5, "ch-1")], ch) == "late"          # equal timestamp is not earlier
    assert cls([verdict(1, "ch-1", "bogus"), verdict(2, "ch-1")], ch) == "ok"   # invalid then valid
    assert cls([], {"stage": "build"}) == "absent"
    assert cls([], {"challenge": "ch-1"}) == "unclassified"


def test_a_prior_task_alone_is_not_evidence_but_a_prior_task_with_a_resolving_reference_is():
    prior_only = msg(1, request_id="tk-old", challenge_verdict="reshape", stage="build")
    assert cls([prior_only], {"stage": "build", "challenge": "tk-old"}) == "unverifiable"
    prior = msg(2, request_id="tk-old", stage="build", challenge="ch-1")
    assert cls([verdict(1, "ch-1"), prior], {"stage": "build", "challenge": "tk-old"}) == "reused-ok"
    two = msg(2, request_id="tk-two", stage="build", challenge="ch-1,ch-2")
    assert cls([verdict(1, "ch-1"), two], {"stage": "build", "challenge": "tk-two"}) == "unverifiable"
    assert cls([verdict(1, "ch-1"), verdict(1, "ch-2"), two], {"stage": "build", "challenge": "tk-two"}) == "reused-ok"


def test_two_ids_are_ok_only_if_both_are():
    meta = {"stage": "fix", "challenge": "ch-1,ch-2"}
    assert cls([verdict(1, "ch-1"), verdict(1, "ch-2")], meta) == "ok"
    kind, states = drift.classify(drift._by_rid([verdict(1, "ch-1")]), meta, T0 + timedelta(hours=5))
    assert kind == "unverifiable" and states == ["ok", "unverifiable"]


@pytest.mark.parametrize("ident", drift.EXEMPT_IDS)
def test_each_listed_exemption_is_accepted_with_and_without_a_note(ident):
    assert cls([], {"stage": "fix", "challenge": f"exempt:{ident}"}) == "exempt-listed"
    assert cls([], {"stage": "fix", "challenge": f"exempt:{ident}:because"}) == "exempt-listed"


@pytest.mark.parametrize("bad", ["exempt:", "exempt:whatever", "exempt:fix-it"])
def test_an_unlisted_or_empty_exemption_warns(bad):
    assert cls([], {"stage": "fix", "challenge": bad}) == "exempt-unlisted"


# --- criteria 5 and 6: allowance and elapsed time ---------------------------------------------------------
def test_the_first_valid_allowance_is_kept_and_later_values_are_attempts():
    tasks = [order(0, allowance="oops"), order(1, allowance="12h"), order(2, allowance="30h", work_cycle="2")]
    s = drift.work_item_state(tasks, "w", NOW)
    assert s["allowance"] == 12 and s["attempts"] == ["30h"]


def test_new_work_without_a_usable_allowance_warns_and_is_never_zero():
    out = drift.order_warnings([], {"stage": "design", "challenge": "exempt:docs-only", "work_item": "n"}, NOW)
    assert any("no usable allowance" in line for line in out)
    assert not any("allowance 0" in line for line in out)
    out = drift.order_warnings([], {"stage": "design", "challenge": "exempt:docs-only"}, NOW)
    assert any("no work_item" in line for line in out)


def reply(hours, rid, status, who="worker"):
    return msg(hours, kind="task-response", sender=who, to="lead", request_id=rid, status=status)


def hours_of(msgs):
    return drift.work_item_state(msgs, "w", NOW)


def test_elapsed_time_ends_at_done_not_at_accepted():
    t = order(0, request_id="a")
    s = hours_of([t, reply(1, "a", "accepted"), reply(4, "a", "done")])
    assert s["hours"] == pytest.approx(4) and s["open"] == 0


def test_open_work_runs_to_now_and_is_flagged_as_a_lower_bound():
    s = hours_of([order(0, request_id="a")])
    assert s["hours"] == pytest.approx(10) and s["open"] == 1
    assert "at least 10.0" in drift.growth_line(s, False)


def test_declined_superseded_requeued_and_parallel_work():
    assert hours_of([order(0, request_id="a"), reply(2, "a", "declined")])["hours"] == pytest.approx(2)
    sup = hours_of([order(0, request_id="a"), order(3, request_id="b", supersedes="a")])
    assert sup["hours"] >= 3                       # ended at the superseding order
    done_first = hours_of([order(0, request_id="a"), reply(2, "a", "done"), order(6, request_id="b", supersedes="a")])
    assert done_first["hours"] == pytest.approx(2 + 4)  # a ended at done (2h); b is open for 4h
    copy = hours_of([order(0, request_id="a"), order(1, request_id="a"), reply(3, "a", "done")])
    assert copy["hours"] == pytest.approx(3)       # a requeued copy counts once
    par = hours_of([order(0, request_id="a", to="x"), order(0, request_id="a", to="y"),
                    reply(2, "a", "done", "x"), reply(3, "a", "done", "y")])
    assert par["hours"] == pytest.approx(5)        # recipients add


def test_a_task_with_an_unreadable_time_gives_a_lower_bound():
    t = order(0, request_id="a")
    t.ts = "not a time"
    assert hours_of([t])["incomplete"] == 1


# --- criteria 7 and 8: rounds and the decision ------------------------------------------------------------
def rounds(msgs):
    return drift._rounds([m for m in msgs if m.kind == "task"])


def test_rounds_count_once_per_pair_across_tasks_stages_copies_and_cycles():
    build = order(0, stage="build")
    f1, f2 = order(1, stage="fix", work_round="1"), order(2, stage="fix", work_round="2")
    assert rounds([build, f1, f2]) == (2, 0)
    review = order(3, stage="read", work_round="2")           # a review of round 2 counts once
    split = order(3, stage="fix", work_round="2", to="other")  # a split / copy of round 2 counts once
    assert rounds([build, f1, f2, review, split]) == (2, 0)
    both = order(4, stage="fix", work_round="3")              # matches both predicates
    assert rounds([build, f1, f2, both]) == (3, 0)
    assert rounds([f1, order(5, stage="fix", work_round="1", work_cycle="2")]) == (2, 0)   # cumulative across cycles
    assert rounds([order(0, stage="fix"), order(1, stage="fix")]) == (0, 2)   # untagged: counted apart, never zero
    assert rounds([order(0, stage="build", work_round="1"), order(1, stage="build", work_round="1")]) == (0, 0)


def two_rounds(extra=()):
    return [order(0, stage="build", request_id="b"), reply(1, "b", "done"),
            order(2, stage="fix", work_round="1", request_id="f1"), reply(3, "f1", "done"),
            order(4, stage="fix", work_round="2", request_id="f2"), reply(5, "f2", "done"), *extra]


@pytest.mark.parametrize("with_allowance", [True, False])
def test_after_two_fix_rounds_the_next_order_needs_a_decision(with_allowance):
    meta = {"stage": "build", "work_item": "w", **({"allowance": "50h"} if with_allowance else {})}
    out = drift.order_warnings(two_rounds(), meta, NOW)
    assert any("needs decision=" in line for line in out)
    for value in drift.DECISIONS:
        ok = drift.order_warnings(two_rounds(), {**meta, "decision": value, "decision_reason": "why"}, NOW)
        assert not any("needs decision=" in line for line in ok)
    for bad in ({"decision": "narrow"}, {"decision": "narrow", "decision_reason": "  "},
                {"decision": "go", "decision_reason": "x"}):
        assert any("needs decision=" in line for line in drift.order_warnings(two_rounds(), {**meta, **bad}, NOW))


def test_one_fix_round_needs_no_decision():
    msgs = two_rounds()[:4]
    out = drift.order_warnings(msgs, {"stage": "build", "work_item": "w"}, NOW)
    assert not any("needs decision=" in x for x in out)


@pytest.mark.parametrize("spent,needs", [(9.9, False), (10.0, False), (10.1, True)])
def test_the_allowance_boundary(spent, needs):
    msgs = [order(0, request_id="a", allowance="10h"), reply(spent, "a", "done")]
    out = drift.order_warnings(msgs, {"stage": "build", "work_item": "w"}, NOW)
    assert any("needs decision=" in x for x in out) is needs


# --- criteria 1, 9, 10, 18: the command ---------------------------------------------------------------------
@pytest.fixture
def bus(tmp_path):
    store = Store(tmp_path)
    store.init(["lead", "worker", "chal"])
    store.set_role("lead", "lead")
    return store


def run(bus, *args):
    try:
        return cli.main(["--root", str(bus.root), *args])
    except SystemExit as exc:
        return exc.code


def send(bus, *meta):
    args = ["task", "--from", "lead", "--to", "worker", "--force", "-m", "work"]
    for m in meta:
        args += ["--meta", m]
    return run(bus, *args)


def test_a_staged_task_without_a_challenge_warns_but_is_delivered_with_exit_zero(bus, capsys):
    assert send(bus, "stage=build", "work_item=w1", "allowance=5h") == 0
    err = capsys.readouterr().err
    assert "drift: challenge evidence for this build order: absent" in err
    assert [m.kind for m in bus.valid_messages()] == ["task"]


def test_a_task_without_a_stage_gets_no_evidence_warning(bus, capsys):
    assert send(bus) == 0
    assert "drift" not in capsys.readouterr().err


def test_the_warning_is_printed_before_the_message_is_stored(bus, capsys, monkeypatch):
    seen = {}
    real = Store.send

    def spy(self, *a, **k):
        seen["err"] = capsys.readouterr().err
        return real(self, *a, **k)

    monkeypatch.setattr(Store, "send", spy)
    assert send(bus, "stage=build", "work_item=w1", "allowance=5h") == 0
    assert "drift:" in seen["err"]


def test_a_failing_drift_check_never_changes_delivery_or_the_exit_code(bus, capsys, monkeypatch):
    monkeypatch.setattr(drift, "order_warnings", lambda *a, **k: 1 / 0)
    assert send(bus, "stage=build") == 0
    assert "drift: check unavailable" in capsys.readouterr().err
    assert len(bus.valid_messages()) == 1


def test_malformed_optional_values_never_break_a_send(bus, capsys):
    assert send(bus, "stage=build", "work_item=w1", "allowance=banana", "decision=?", "challenge=,,") == 0
    assert len(bus.valid_messages()) == 1


def test_a_verdict_before_the_order_silences_the_warning(bus, capsys):
    bus.send(sender="chal", recipient="lead", kind="message", body="v",
             meta={"request_id": "ch-1", "challenge": "true", "verdict": "reshape"})
    assert send(bus, "stage=build", "work_item=w1", "allowance=5h", "challenge=ch-1") == 0
    err = capsys.readouterr().err
    assert "challenge evidence" not in err and "growth" not in err.replace("work item w1", "")


def test_one_vendor_team_behaves_the_same_because_nothing_reads_vendor_or_exposure(bus, capsys):
    import inspect
    source = inspect.getsource(drift).lower()
    assert "vendor" not in source and "exposed" not in source
    assert send(bus, "stage=fix", "work_item=w1", "allowance=5h") == 0
    assert "absent" in capsys.readouterr().err


def test_broadcast_task_is_not_hooked(bus, capsys):
    assert run(bus, "broadcast", "--from", "lead", "--all", "--kind", "task", "--force", "-m", "x",
               "--meta", "stage=build") == 0
    assert "drift:" not in capsys.readouterr().err


# --- criteria 11 and 12: unknown, and the read-only report ---------------------------------------------------
def test_pre_activation_orders_are_unknown_on_both_sides_of_the_instant(monkeypatch):
    monkeypatch.setattr(drift, "ACTIVATION", ts(5))
    before = order(4.99, stage="build")
    after = order(5.0, stage="build")
    rep = drift.report([before, after], now=NOW)
    assert rep["classes"] == {"unknown": 1, "absent": 1}
    assert rep["nothing_share"] == 1.0


def test_a_late_verdict_does_not_change_an_old_order_in_a_later_report():
    o = order(5, stage="build", challenge="ch-1")
    assert drift.report([o], now=NOW)["classes"] == {"unverifiable": 1}
    assert drift.report([o, verdict(8, "ch-1")], now=NOW)["classes"] == {"late": 1}


def test_the_report_lists_warned_orders_and_work_items_and_skips_other_stages():
    msgs = [order(0, stage="build", request_id="a", allowance="10h"), order(1, stage="read")]
    rep = drift.report(msgs, now=NOW)
    assert rep["classes"] == {"absent": 1, "unclassified": 1}
    assert len(rep["warned_orders"]) == 1 and rep["work_items"][0]["allowance"] == 10
    assert "elapsed task-hours" in drift.render_report(rep)


def test_overview_drift_is_read_only_and_json_is_valid(bus, capsys):
    send(bus, "stage=build", "work_item=w1", "allowance=5h")
    capsys.readouterr()
    before = sorted(p.name for p in bus.root.rglob("*") if p.is_file())
    assert run(bus, "overview", "--drift") == 0
    plain = capsys.readouterr().out
    assert run(bus, "overview", "--drift", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert "DRIFT" in plain and data["classes"]
    assert before == sorted(p.name for p in bus.root.rglob("*") if p.is_file())


def test_the_plain_overview_is_unchanged_without_the_flag(bus, capsys):
    assert run(bus, "overview") == 0
    assert "DRIFT" not in capsys.readouterr().out


# --- criterion 16: skill text ----------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["claude/agenttalk.challenge.md", "codex/agenttalk-challenge/SKILL.md"])
def test_both_challenge_skill_copies_carry_the_drift_rules(path):
    from agenttalk.install_skills import SKILLS_ROOT
    text = " ".join((SKILLS_ROOT / path).read_text(encoding="utf-8").split())
    for needle in (*drift.EXEMPT_IDS, "allowance=<N>h", "decision=narrow|stop|continue", "decision_reason",
                   "names what it leaves out", "two fix rounds"):
        assert needle in text, needle


# --- PR #924 review: the defects reviewer-1 reproduced, each pinned ----------------------------------------
def test_a_rescinded_task_stops_its_timer_at_the_requesters_rescind():
    t = order(0, request_id="a")
    s = hours_of([t, msg(2, kind="rescind", sender="lead", to="worker", request_id="a")])
    assert s["hours"] == pytest.approx(2) and s["open"] == 0


def test_read_and_unstaged_tasks_still_count_toward_the_work_items_elapsed_time():
    s = hours_of([order(0, stage="read", request_id="r"), msg(0, work_item="w", request_id="u")])
    assert s["tasks"] == 2 and s["hours"] == pytest.approx(20)


def test_parallel_recipients_are_labelled_as_added_and_incomplete_history_is_named():
    s = hours_of([order(0, request_id="a", to="x"), order(0, request_id="a", to="y")])
    assert "recipients added" in drift.growth_line(s, False)
    bad = order(0, request_id="b")
    bad.ts = "nonsense"
    assert "incomplete history, 1 tasks" in drift.growth_line(hours_of([bad]), False)


@pytest.mark.parametrize("ref", [",,", "ch-1,", ",ch-1", "ch-1,,ch-2", "bad id!", " , "])
def test_a_malformed_or_empty_reference_list_is_never_evidence(ref):
    msgs = [verdict(1, "ch-1"), verdict(1, "ch-2")]
    assert cls(msgs, {"stage": "build", "challenge": ref}) == "unusable"


def test_a_task_created_after_the_order_is_not_reuse():
    later = msg(9, request_id="tk-later", stage="build", challenge="ch-1")
    assert cls([verdict(1, "ch-1"), later], {"stage": "build", "challenge": "tk-later"}) == "unverifiable"


def test_reuse_goes_exactly_one_hop():
    t1 = msg(2, request_id="t1", stage="build", challenge="ch-1")
    t2 = msg(3, request_id="t2", stage="build", challenge="t1")
    assert cls([verdict(1, "ch-1"), t1, t2], {"stage": "build", "challenge": "t1"}) == "reused-ok"
    assert cls([verdict(1, "ch-1"), t1, t2], {"stage": "build", "challenge": "t2"}) == "unverifiable"


@pytest.mark.parametrize("allowance", ["0h", "banana", "-3h", ""])
def test_a_first_order_with_an_unusable_allowance_warns_and_still_shows_the_growth_line(allowance):
    meta = {"stage": "build", "work_item": "n", "allowance": allowance, "challenge": "exempt:docs-only"}
    out = drift.order_warnings([], meta, NOW)
    assert any("no usable allowance" in x for x in out)
    assert any(x.startswith("work item n: allowance none usable") for x in out)


def test_an_earlier_read_task_does_not_hide_the_missing_allowance_on_the_first_scoped_order():
    out = drift.order_warnings([order(0, stage="read")], {"stage": "build", "work_item": "w"}, NOW)
    assert any("no usable allowance" in x for x in out)


def test_older_work_keeps_an_unknown_allowance(monkeypatch):
    monkeypatch.setattr(drift, "ACTIVATION", ts(5))
    s = drift.work_item_state([order(1, allowance="9h")], "w", NOW)
    assert s["legacy"] and s["allowance"] is None
    assert "unknown (older work)" in drift.growth_line(s, False)


def test_the_growth_line_is_part_of_every_order_under_an_allowance():
    msgs = [order(0, allowance="10h", request_id="a"), reply(1, "a", "done")]
    out = drift.order_warnings(msgs, {"stage": "build", "work_item": "w", "challenge": "exempt:docs-only"}, NOW)
    assert len(out) == 1 and out[0].startswith("work item w: allowance 10h")


def test_untagged_fix_tasks_never_prove_the_decision_threshold():
    msgs = [order(0, stage="fix", request_id="u1"), order(1, stage="fix", request_id="u2")]
    out = drift.order_warnings(msgs, {"stage": "build", "work_item": "w", "allowance": "50h"}, NOW)
    assert not any("needs decision=" in x for x in out)
    assert any("decision possibly needed" in x and "count incomplete" in x for x in out)


def test_an_old_decision_does_not_cover_a_later_order():
    first = {"stage": "build", "work_item": "w", "decision": "continue", "decision_reason": "ok"}
    assert not any("needs decision=" in x for x in drift.order_warnings(two_rounds(), first, NOW))
    later = {"stage": "build", "work_item": "w"}
    assert any("needs decision=" in x for x in drift.order_warnings(two_rounds([order(6, **first)]), later, NOW))


def test_a_read_order_after_two_fix_rounds_also_needs_a_decision():
    out = drift.order_warnings(two_rounds(), {"stage": "read", "work_item": "w"}, NOW)
    assert any("needs decision=" in x for x in out)


def exempt_fix(hours, rid, rnd, **extra):
    return order(hours, stage="fix", work_round=rnd, challenge="exempt:review-ordered-fix", request_id=rid, **extra)


def test_the_report_rebuilds_decision_warnings_as_of_each_order_and_labels_them_reconstructed():
    head = [order(0, stage="build", challenge="exempt:docs-only", request_id="b", allowance="50h"),
            reply(1, "b", "done"),
            exempt_fix(2, "f1", "1"), reply(3, "f1", "done"), exempt_fix(4, "f2", "2"), reply(5, "f2", "done")]
    decided = order(6, stage="read", request_id="r", decision="stop", decision_reason="enough")
    with_decision = drift.report(head + [decided], now=NOW)
    assert with_decision["warned_orders"] == []
    quiet = drift.report(head + [order(6, stage="read", request_id="r")], now=NOW)
    assert len(quiet["warned_orders"]) == 1
    row = quiet["warned_orders"][0]
    assert row["reconstructed"] and "needs decision=" in row["reasons"][0]
    assert "reconstructed from messages" in drift.render_report(quiet)


def test_the_report_counts_exposure_as_of_the_order_not_as_of_today():
    msgs = [order(0, request_id="a", allowance="3h", challenge="exempt:docs-only"),
            order(2, request_id="b", challenge="exempt:docs-only"), reply(8, "a", "done")]
    rep = drift.report(msgs, now=NOW)
    reasons = [r for w in rep["warned_orders"] for r in w["reasons"]]
    assert not any("needs decision=" in r for r in reasons)   # at hour 2 only 2h were used


def test_exemptions_unknown_and_unclassified_stay_out_of_the_nothing_share():
    msgs = [order(1, request_id="a0")] + [order(1, challenge="exempt:docs-only", request_id=f"e{i}") for i in range(9)]
    msgs.append(msg(1, request_id="plain"))
    rep = drift.report(msgs, now=NOW)
    assert rep["classes"]["exempt-listed"] == 9 and rep["classes"]["unclassified"] == 1
    assert rep["nothing_share"] == 1.0


def test_with_no_activation_instant_everything_is_unknown_and_nothing_is_older_work(monkeypatch):
    monkeypatch.setattr(drift, "ACTIVATION", None)
    rep = drift.report([order(1)], now=NOW)
    assert rep["classes"] == {"unknown": 1} and rep["activation"] is None and rep["warned_orders"] == []
    assert "not set" in drift.render_report(rep)
    assert not drift.work_item_state([order(1, allowance="9h")], "w", NOW)["legacy"]


def test_overview_drift_leaves_every_file_byte_identical(bus, capsys):
    send(bus, "stage=build", "work_item=w1", "allowance=5h")
    capsys.readouterr()

    def snapshot():
        return {str(p.relative_to(bus.root)): p.read_bytes() for p in bus.root.rglob("*") if p.is_file()}

    before = snapshot()
    assert run(bus, "overview", "--drift") == 0 and run(bus, "overview", "--drift", "--json") == 0
    assert before == snapshot()


def test_the_task_command_prints_the_same_stdout_with_and_without_the_drift_check(bus, capsys, monkeypatch):
    import re

    def stdout_for(rid, patched):
        if patched:
            monkeypatch.setattr(drift, "order_warnings", lambda *a, **k: [])
        capsys.readouterr()
        assert send(bus, "stage=build", "work_item=w1", "allowance=5h", f"request_id={rid}") == 0
        out = capsys.readouterr().out.replace(rid, "RID")
        return re.sub(r"\d{8}-\d{6}-\d+-\w+|\d{4}-\d\d-\d\dT[\d:.]+Z?", "X", out)

    assert stdout_for("tk-new", False) == stdout_for("tk-old", True)


def test_both_challenge_skill_copies_carry_the_same_full_drift_paragraph():
    from agenttalk.install_skills import SKILLS_ROOT
    paragraphs = []
    for path in ("claude/agenttalk.challenge.md", "codex/agenttalk-challenge/SKILL.md"):
        text = " ".join((SKILLS_ROOT / path).read_text(encoding="utf-8").split())
        start = text.index("**Drift visibility")
        paragraphs.append(text[start:text.index("shows the picture.", start)])
    assert paragraphs[0] == paragraphs[1]
    for needle in ("stamped before the order", "A broadcast of kind task", "is not checked in this slice",
                   "overview command with `--drift`", "names what it leaves out"):
        assert needle in paragraphs[0], needle


# --- the 3-second ceiling (lead decision on criterion 14) -----------------------------------------------
def test_the_bounded_reader_returns_the_same_messages_as_the_trust_gate_when_it_finishes(bus):
    send(bus, "stage=build", "work_item=w1", "allowance=5h")
    msgs, complete = drift.read_messages(bus)
    assert complete and [m.id for m in msgs] == [m.id for m in bus.valid_messages()]


def test_a_slow_reader_stops_at_the_ceiling_says_history_incomplete_and_still_delivers(bus, capsys, monkeypatch):
    bus.send(sender="lead", recipient="worker", kind="note", body="one earlier message")
    real, ticks = drift.read_messages, iter(range(0, 1000, 5))      # each clock reading is 5 s later
    monkeypatch.setattr(drift, "read_messages", lambda store: real(store, ceiling=3.0, clock=lambda: next(ticks)))
    assert send(bus, "stage=build", "work_item=w1", "allowance=5h") == 0
    err = capsys.readouterr().err
    assert "drift: history incomplete" in err and "lower bounds" in err
    assert "no usable allowance" not in err                         # an unseen allowance is not reported as missing
    assert [m.kind for m in bus.valid_messages()] == ["note", "task"]   # delivered unchanged


# --- GitHub reviewer's five P2s on #924 (1 and 3 were already fixed; 2, 4 and 5 pinned here) ---------------
def test_the_report_derives_the_recorded_decision_instead_of_always_saying_needed():
    base = two_rounds()
    decided = order(6, stage="build", request_id="n", decision="narrow", decision_reason="smaller")
    rep = drift.report(base + [decided], now=NOW)
    assert rep["work_items"][0]["decided"] is True
    assert "decision recorded" in drift.render_report(rep)
    plain = two_rounds()
    assert drift.report(plain + [order(6, request_id="m")], now=NOW)["work_items"][0]["decided"] is False


@pytest.mark.parametrize("bad", ["garbage", "1.5", "-2", "٣"])
def test_a_malformed_historical_round_number_is_incomplete_accounting_not_a_crash(bad):
    assert drift._rounds([order(0, stage="fix", work_round=bad, request_id="x")]) == (0, 1)
    assert drift._rounds([order(0, stage="build", work_round=bad, request_id="y")]) == (0, 0)
    bad_fix = order(0, stage="fix", work_round=bad, request_id="x", challenge="exempt:review-ordered-fix")
    rep = drift.report([bad_fix], now=NOW)
    assert rep["work_items"][0]["untagged"] == 1
    assert drift.order_warnings([order(0, stage="fix", work_round=bad)], {"stage": "build", "work_item": "w"}, NOW)


def test_a_review_request_that_supersedes_a_task_stops_the_tasks_timer_but_is_not_charged():
    replace = msg(1, kind="review-request", to="reviewer", request_id="rr", supersedes="a", work_item="w")
    s = hours_of([order(0, request_id="a"), replace])
    assert s["hours"] == pytest.approx(1) and s["open"] == 0
