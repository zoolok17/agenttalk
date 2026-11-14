"""`agenttalk overview`: one read-only screen built only from the dashboard's own builders."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agenttalk import budget, cli, overview, web
from agenttalk.store import Store


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "claude-a", "codex-b"])
    s.send(sender="lead", recipient="claude-a", body="please look", kind="question",
           subject="check the thing", meta={"request_id": "q-ov1"})
    s.send(sender="lead", recipient="codex-b", body="and this", kind="question",
           subject="another thing", meta={"request_id": "q-ov2"})
    return s


def _run(tmp_path: Path, capsys, *argv: str) -> str:
    assert cli.main(["--root", str(tmp_path), "overview", *argv]) == 0
    return capsys.readouterr().out


def _files(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------- parity

def test_counts_match_the_dashboard_builders(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    desc = web.RootDescriptor(store=s, label=s.root.name)
    state = web.build_state([desc])["roots"][0]
    attention = web.build_attention(desc, state["agents"])
    gates = web.build_gates(desc)

    data = json.loads(_run(tmp_path, capsys, "--json"))
    assert data["state"]["counts"] == state["counts"]
    assert [a["name"] for a in data["state"]["agents"]] == [a["name"] for a in state["agents"]]
    assert data["attention"]["count"] == attention["count"]
    assert data["gates"]["verdict"] == gates["verdict"]
    assert [t["request_id"] for t in data["state"]["threads"]] == [t["request_id"] for t in state["threads"]]

    text = _run(tmp_path, capsys)
    assert f"{len(state['agents'])} agents" in text
    assert f"{state['counts']['open_threads']} open conversations" in text
    assert f"{attention['count']} items needing attention" in text
    assert gates["verdict"] == "GO" and gates["count"] == 0  # this store has no gates, so nothing was checked
    assert "release checks: nothing checked (0 applicable gates)" in " ".join(text.split())
    for agent in state["agents"]:
        assert agent["name"] in text


def test_json_has_exactly_the_three_builder_payloads(tmp_path: Path, capsys) -> None:
    _store(tmp_path)
    data = json.loads(_run(tmp_path, capsys, "--json"))
    assert set(data) == {"state", "attention", "gates"}
    assert {"counts", "agents", "threads", "label"} <= set(data["state"])
    assert {"items", "count"} <= set(data["attention"])
    assert {"verdict", "gates"} <= set(data["gates"])


# ------------------------------------------------------------------- read-only

def test_it_changes_nothing_and_moves_no_cursor(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    cursors = {a: s.cursor(a) for a in ("lead", "claude-a", "codex-b")}
    before = _files(s.dir)
    _run(tmp_path, capsys)
    _run(tmp_path, capsys, "--json")
    assert _files(s.dir) == before
    assert {a: s.cursor(a) for a in cursors} == cursors


# ----------------------------------------------------------------------- spend

class _Boom:
    def __init__(self, *a, **k):
        raise AssertionError("the spend ledger must not be touched without --enable-budget")


def test_spend_is_off_by_default(tmp_path: Path, capsys, monkeypatch) -> None:
    _store(tmp_path)
    monkeypatch.setattr(budget, "BudgetFeed", _Boom)
    text = _run(tmp_path, capsys)
    assert "SPEND" not in text
    assert "budget" not in json.loads(_run(tmp_path, capsys, "--json"))


def test_spend_appears_only_with_the_flag(tmp_path: Path, capsys, monkeypatch) -> None:
    _store(tmp_path)

    class Fake:
        def get(self):
            return {"status": "ok", "committed_micro_eur": 1_500_000,
                    "trial_cutoff_micro_eur": 10_000_000, "external_ceiling_micro_eur": 12_000_000,
                    "service_hold": False, "unresolved_attempts": 0}

    monkeypatch.setattr(budget, "BudgetFeed", Fake)
    text = _run(tmp_path, capsys, "--enable-budget")
    assert "SPEND" in text and "spent this month: 1.50 EUR" in text
    assert json.loads(_run(tmp_path, capsys, "--json", "--enable-budget"))["budget"]["status"] == "ok"


@pytest.mark.parametrize("status,words", [("not_set_up", "not set up"), ("busy", "busy"),
                                          ("unavailable", "could not be read")])
def test_spend_problems_are_said_in_words_not_shown_as_zero(status: str, words: str) -> None:
    lines = overview._budget_lines({"status": status})
    assert words in " ".join(lines) and "EUR" not in " ".join(lines)


# ---------------------------------------------------------------- rendering only

def test_the_screen_translates_builder_values_and_leaves_missing_fields_out() -> None:
    data = {
        "state": {"label": "proj", "counts": {"open_threads": 1}, "agents": [
            {"name": "a", "role": "lead", "health": {"state": "working_turn", "stale": True},
             "last_seen_age_seconds": 300, "capacity": {"rate_used_pct": 40.0, "context_used_pct": None},
             "usage_limit_park": {"present": True}, "conversation_size": {"warn": True}},
            {"name": "b"},
        ], "threads": [{"request_id": "q-1", "subject": "Fix it", "next_owner": "b", "age_seconds": 7200}]},
        "attention": {"count": 1, "items": [
            {"severity": "high", "title": "Reply owed", "agent": "b", "age_seconds": 120, "age_unknown": False}]},
        "gates": {"verdict": "HOLD", "gates": [{"name": "ci", "blocks": True, "reason": "red"}]},
    }
    text = " ".join(overview.render(data, width=400).split())
    assert "a (lead): working, (that status is old), parked on a usage limit until restarted, seen 5 min ago, " \
           "limit used 40%, conversation getting long" in text
    assert "b: status unknown" in text
    assert "memory used" not in text            # a null percentage is left out, not shown as 0
    assert "Fix it: next move is b's, open 2 h" in text
    assert "[high] Reply owed (b), 2 min" in text
    assert "RELEASE CHECKS HOLDING" in text and "ci: red" in text
    assert "1 agent," not in text and "2 agents" in text


def test_a_builder_error_is_said_plainly() -> None:
    data = {"state": {"label": "p", "errors": ["boom"], "agents": [], "threads": []},
            "attention": {"items": []}, "gates": {}}
    assert "Could not read the team: boom" in overview.render(data)


def test_long_lists_are_cut_with_a_pointer() -> None:
    items = [{"severity": "low", "title": f"t{i}", "age_unknown": True} for i in range(9)]
    threads = [{"request_id": f"q-{i}", "age_seconds": 1} for i in range(9)]
    text = overview.render({"state": {"label": "p", "agents": [], "threads": threads},
                            "attention": {"items": items, "count": 9}, "gates": {}})
    assert "... and 4 more (agenttalk attention)" in text
    assert "... and 3 more (agenttalk threads)" in text


def test_the_text_is_plain_ascii_and_fits_the_width() -> None:
    data = {"state": {"label": "café — " + "x" * 300, "agents": [], "threads": []},
            "attention": {"items": []}, "gates": {}}
    text = overview.render(data, width=80)
    assert text.isascii()
    assert max(len(line) for line in text.splitlines()) < 80


def test_the_terminal_text_uses_no_programmer_words_beyond_commands(tmp_path: Path, capsys) -> None:
    _store(tmp_path)
    text = _run(tmp_path, capsys)
    assert not re.search(r"\b(json|schema|epoch|cursor|nonce|hmac)\b", text, re.I)


# ------------------------------------------- unreadable data is not an empty team

def test_an_unreadable_team_is_not_reported_as_empty(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    s.config_path.write_text("{ not valid json", encoding="utf-8")
    text = _run(tmp_path, capsys)
    assert "Could not read the team" in text
    for claim in ("0 agents", "No agents.", "No open conversations.", "Nothing right now.",
                  "0 open conversations", "0 items needing attention"):
        assert claim not in text, claim
    assert text.count("Could not be read, so this may not be empty.") == 2  # team and conversations


def test_a_real_empty_team_still_says_so() -> None:
    text = overview.render({"state": {"label": "p", "agents": [], "threads": [], "counts": {"open_threads": 0}},
                            "attention": {"items": [], "count": 0}, "gates": {"verdict": "GO"}})
    assert "0 agents" in text and "No agents." in text and "No open conversations." in text
    assert "Nothing right now." in text


def test_a_source_that_reports_an_error_is_not_shown_as_none() -> None:
    text = overview.render({"state": {"label": "p", "agents": [], "threads": []},
                            "attention": {"items": [], "count": 0, "errors": ["boom"]}, "gates": {}})
    assert "Could not read attention items: boom" in text
    assert "Nothing right now." not in text and "0 items needing attention" not in text


# ------------------------------------------------ terminal control characters

_EVIL = "A\x1b[2J\x1b[H\x07\x08\x00\x7f\r‮é\x9b2J B"


def test_no_printed_field_can_carry_a_control_character() -> None:
    """Every field the screen prints, each filled with escape and other control characters."""
    e = _EVIL
    data = {
        "state": {"label": e, "errors": [e], "counts": {"open_threads": 1}, "agents": [
            {"name": e, "role": e, "health": {"state": "working_turn", "stale": True},
             "cli_child_verdict": {"state": e}, "usage_limit_park": {"present": True},
             "last_seen_age_seconds": 5, "capacity": {"rate_used_pct": 5, "context_used_pct": 6},
             "conversation_size": {"warn": True}}],
         "threads": [{"request_id": e, "subject": e, "next_owner": e, "age_seconds": 5}]},
        "attention": {"count": 1, "errors": [e], "items": [
            {"severity": e, "title": e, "agent": e, "age_seconds": 5}]},
        "gates": {"verdict": e, "errors": [e], "gates": [{"name": e, "blocks": True, "reason": e}]},
        "budget": {"status": e},
    }
    text = overview.render(data)
    assert "\x1b" not in text
    bad = [c for c in text if not (c == "\n" or 32 <= ord(c) < 127)]
    assert not bad, bad
    # and the same, field by field, for the ones that did not go through the old filter
    for part in ({"verdict": e}, ):
        assert overview.render({"state": {"label": "p", "agents": [], "threads": []},
                                "attention": {"items": []}, "gates": part}).isascii()


def test_a_real_message_subject_cannot_clear_the_screen(tmp_path: Path, capsys) -> None:
    s = Store(tmp_path)
    s.init(["lead", "claude-a"])
    s.send(sender="lead", recipient="claude-a", body="x", kind="question",
           subject="hi\x1b[2J\x1b[Hthere", meta={"request_id": "q-esc"})
    text = _run(tmp_path, capsys)
    assert "\x1b" not in text and "hi" in text and "there" in text


# ------------------------------- the same ranking and wording as status and the dashboard

def _seat(**agent) -> str:
    data = {"state": {"label": "p", "agents": [{"name": "a", **agent}], "threads": []},
            "attention": {"items": []}, "gates": {}}
    return " ".join(overview.render(data, width=400).split())


def test_an_adverse_supervisor_verdict_comes_first_not_the_wrapper_report() -> None:
    """The wrapper cannot see its own CLI child die, so it may still say it is working."""
    text = _seat(health={"state": "working_turn"}, cli_child_verdict={"state": "CLI_CHILD_MISSING"})
    assert "a: supervisor says cli child missing (the wrapper itself reports: working)" in text
    assert "a: working," not in text


def test_a_confirmed_healthy_verdict_keeps_the_wrapper_state_first() -> None:
    from agenttalk import supervisor
    healthy = sorted(supervisor.CLI_CHILD_HEALTHY_STATES)[0]
    text = _seat(health={"state": "working_turn"}, cli_child_verdict={"state": healthy})
    assert f"a: working, supervisor: {healthy.replace('_', ' ').lower()}" in text


@pytest.mark.parametrize("state", ["WRAPPER_MISSING", "TURN_FAILED", "STUCK_OR_DEAD", "SOMETHING_NEW"])
def test_every_verdict_other_than_the_healthy_ones_is_primary(state: str) -> None:
    assert f"supervisor says {state.replace('_', ' ').lower()}" in _seat(
        health={"state": "idle_waiting"}, cli_child_verdict={"state": state})


def test_a_provider_wait_is_not_called_a_usage_limit() -> None:
    park = {"present": True, "state": "parked", "kind": "overloaded", "next_try_epoch": None}
    text = _seat(health={"state": "idle_waiting"}, usage_limit_park=park)
    assert "waiting for an overloaded AI provider" in text and "usage limit" not in text
    throttled = dict(park, kind="throttled")
    assert "waiting on a possible usage limit" in _seat(usage_limit_park=throttled)


def test_a_stale_park_says_the_wrapper_is_not_responding() -> None:
    text = _seat(usage_limit_park={"present": True, "state": "stale", "kind": "usage_limit"})
    assert "parked on a usage limit, wrapper not responding" in text
    cooldown = {"present": True, "state": "stale", "kind": "overloaded"}
    assert "wrapper not responding" in _seat(usage_limit_park=cooldown)


def test_a_stale_report_normalised_to_unknown_keeps_what_it_last_said() -> None:
    text = _seat(health={"state": "unknown", "stale": True, "last_known_state": "working_silent"})
    assert "no recent report (it last said: working (quiet for a while))" in text
    assert "status unknown" not in text
    assert "a: status unknown" in _seat(health={"state": "unknown", "stale": True})


def test_the_attention_total_uses_the_active_count_not_the_rows_shown() -> None:
    data = {"state": {"label": "p", "agents": [], "threads": []},
            "attention": {"items": [{"severity": "low", "title": "x", "age_unknown": True}],
                          "count": 1, "active_count": 7}, "gates": {}}
    assert "7 items needing attention" in overview.render(data)
    data["attention"].pop("active_count")
    assert "1 item needing attention" in overview.render(data)


def test_a_broadcast_with_several_owners_names_them_all() -> None:
    threads = [{"request_id": "q-1", "subject": "All hands", "next_owner": ["alpha", "beta", "gamma"],
                "age_seconds": 5}]
    text = overview.render({"state": {"label": "p", "agents": [], "threads": threads},
                            "attention": {"items": []}, "gates": {}})
    assert "next move is alpha, beta and gamma (each)" in text
    one = overview.render({"state": {"label": "p", "agents": [], "threads": [dict(threads[0], next_owner="solo")]},
                           "attention": {"items": []}, "gates": {}})
    assert "next move is solo's" in one


# ------------------------------------ the supervisor's list of agenttalk commands (#483 / #491 CI)

def test_the_supervisor_knows_overview_so_a_word_inside_an_argument_is_not_taken_for_it() -> None:
    from agenttalk import supervisor as sup

    assert "overview" in sup._AGENTTALK_SUBCOMMANDS
    inv = sup._agenttalk_invocation("agenttalk --root D:/p overview --json", "D:/p")
    assert inv["subcommand"] == "overview" and inv["args"] == ["--json"]
    # "overview" as the value of another command's option stays that command's value
    inv = sup._agenttalk_invocation("agenttalk --root D:/p wait --for beta --to-request overview", "D:/p")
    assert inv["subcommand"] == "wait"


# ---------------------------------------------------- a check that checked nothing (#610)
NOTHING_CHECKED = "release checks: nothing checked (0 applicable gates)"


def test_610_an_empty_store_says_nothing_was_checked_and_keeps_its_json(tmp_path: Path, capsys) -> None:
    _store(tmp_path)

    text = " ".join(_run(tmp_path, capsys).split())
    assert NOTHING_CHECKED in text and "release checks: GO" not in text

    data = json.loads(_run(tmp_path, capsys, "--json"))
    assert set(data) == {"state", "attention", "gates"}
    assert data["gates"]["verdict"] == "GO" and data["gates"]["count"] == 0


@pytest.mark.parametrize(("severity", "verdict"), [("warn", "GO"), ("blocker", "HOLD")])
def test_610_a_real_gate_keeps_the_verdict(tmp_path: Path, capsys, severity, verdict) -> None:
    from agenttalk import gates

    _store(tmp_path)
    gates.set_gate(tmp_path, name="g", status="red", severity=severity, scope="global", actor="a",
                   evidence_source="local_command")

    text = " ".join(_run(tmp_path, capsys).split())

    assert f"release checks: {verdict}" in text and "nothing checked" not in text


def test_610_green_ci_blocker_stays_plain_go(tmp_path: Path, capsys) -> None:
    from agenttalk import gates

    _store(tmp_path)
    evidence = "https://ci.example.invalid/run/610"
    gates.set_gate(
        tmp_path,
        name="ci",
        status="green",
        severity="blocker",
        scope="global",
        actor="lead",
        evidence_source="automation_ci",
        evidence=[evidence],
        required=True,
    )

    data = json.loads(_run(tmp_path, capsys, "--json"))
    checked = data["gates"]
    assert checked["verdict"] == "GO" and checked["count"] == 1 == len(checked["gates"])
    (gate,) = checked["gates"]
    assert (gate["name"], gate["status"], gate["severity"], gate["blocks"]) == ("ci", "green", "blocker", False)
    assert gate["evidence"][-1]["source"] == "automation_ci" and gate["evidence"][-1]["refs"] == [evidence]

    text = " ".join(_run(tmp_path, capsys).split())
    assert "release checks: GO" in text and "nothing checked" not in text


def _gates_line(payload: dict) -> str:
    data = {"state": {"label": "p", "agents": [], "threads": []}, "attention": {"items": []}, "gates": payload}
    return " ".join(overview.render(data, width=400).split())


def test_610_an_explicit_zero_with_go_is_nothing_checked() -> None:
    assert NOTHING_CHECKED in _gates_line({"verdict": "GO", "count": 0})


@pytest.mark.parametrize("payload", [
    {"verdict": "GO", "count": 1},
    {"verdict": "GO"},
    {"verdict": "GO", "count": None},
    {"verdict": "GO", "count": False},
    {"verdict": "GO", "count": "0"},
    {"verdict": "GO", "count": 0.0},
], ids=["positive", "missing", "null", "boolean", "text", "float"])
def test_610_only_a_known_integer_zero_means_nothing_was_checked(payload) -> None:
    text = _gates_line(payload)

    assert "release checks: GO" in text and "nothing checked" not in text


def test_610_zero_never_overrides_hold() -> None:
    text = _gates_line({"verdict": "HOLD", "count": 0})

    assert "release checks: HOLD" in text and "nothing checked" not in text


def test_610_zero_never_overrides_a_read_error() -> None:
    text = _gates_line({"verdict": "GO", "count": 0, "errors": ["boom"]})

    assert "Could not read release checks: boom" in text and "nothing checked" not in text
