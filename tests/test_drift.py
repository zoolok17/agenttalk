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
    assert rep["classes"] == {"absent": 1}
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


# --- the one-off baseline tool (not product code) -----------------------------------------------------------
def _baseline():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / "scripts" / "drift_baseline.py"
    spec = importlib.util.spec_from_file_location("drift_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_baseline_tool_applies_the_same_rules_over_the_thirty_days_before_activation(monkeypatch):
    msgs = [verdict(0, "ch-1"), order(1, challenge="ch-1"), order(2), order(3, challenge="ch-9"),
            order(4, stage="read"), order(800, challenge="ch-1")]
    out = _baseline().baseline(msgs, ts(24 * 29))
    assert out["classes"] == {"ok": 1, "absent": 1, "unverifiable": 1} and out["orders"] == 3


def test_the_baseline_tool_counts_an_order_it_cannot_classify_as_unverifiable_never_absent(monkeypatch):
    monkeypatch.setattr(drift, "classify", lambda *a, **k: 1 / 0)
    out = _baseline().baseline([order(1, challenge="ch-1")], ts(24 * 29))
    assert out["classes"] == {"unverifiable": 1}
