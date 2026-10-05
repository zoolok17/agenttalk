"""`agenttalk supervisor` and `agenttalk status` never show a seat's own "idle" as its health beside a
supervisor verdict that says it is stuck or dead. One rule (cli._health_column) serves both commands.
Synthetic data; no model."""

from __future__ import annotations

import time

import pytest

import test_usage_park_readers as rd
from agenttalk import cli, health as hm, supervisor as sup
from agenttalk.wrapper import usage_park as park


@pytest.fixture(autouse=True)
def current_clock(monkeypatch):
    """The readers' fixtures stamp the time the test module was imported; in a long run that is
    minutes old and health would read as stale. Use the time of each test."""
    monkeypatch.setattr(rd, "NOW", time.time())


def idle_beta(store):
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="idle_waiting", updated_at=park.epoch_iso(rd.NOW),
        since=park.epoch_iso(rd.NOW - 60), last_progress_at=park.epoch_iso(rd.NOW), source="wrapper"))


def supervisor_line(tmp_path, store, monkeypatch, capsys, plan):
    report = sup.build_report(store, now_epoch=rd.NOW)["agents"]["beta"]
    assessed = sup.supervisor_agent_assessment("beta", report, plan)
    monkeypatch.setattr(sup, "build_supervisor_observation", lambda *a, **k: {
        "root": str(store.root), "agents": [assessed], "event_ring": {"events": [], "warnings": []}})
    assert cli.main(["--root", str(tmp_path), "supervisor"]) == 0
    out = capsys.readouterr().out
    return next(x for x in out.splitlines() if x.strip().startswith("beta")), out


@pytest.mark.parametrize("state", ["STUCK_OR_DEAD", "TURN_FAILED", "WRAPPER_MISSING", "CLI_CHILD_UNKNOWN"])
def test_the_supervisor_command_shows_the_verdict_not_the_wrappers_idle(tmp_path, monkeypatch, capsys, state):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    idle_beta(store)
    line, out = supervisor_line(tmp_path, store, monkeypatch, capsys,
                                {"state": state, "action": "none", "reason": "confirmed dead child"})
    assert f"health={state} (wrapper self-reports idle_waiting)" in line
    assert "health=idle_waiting" not in out
    assert "usage_limit_parked" not in out


def test_the_status_command_is_the_control_and_uses_the_same_rule(tmp_path, monkeypatch, capsys):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    idle_beta(store)
    monkeypatch.setattr(sup, "build_supervisor_observation", lambda *a, **k: {
        "agents": [{"name": "beta", "decision": {"state": "STUCK_OR_DEAD", "action": "none"}}],
        "event_ring": {"warnings": []}})
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.strip().startswith("beta"))
    assert "health=STUCK_OR_DEAD (wrapper self-reports idle_waiting)" in line and "usage_limit_parked" not in line


@pytest.mark.parametrize("state", ["HEALTHY_IDLE", "HEALTHY_WORKING"])
def test_a_healthy_verdict_leaves_the_line_as_before(tmp_path, monkeypatch, capsys, state):
    store = rd.make_store(tmp_path)
    idle_beta(store)
    line, _ = supervisor_line(tmp_path, store, monkeypatch, capsys, {"state": state, "action": "none", "reason": "ok"})
    assert "health=idle_waiting heartbeat=" in line and "self-reports" not in line


def test_with_no_plan_the_line_is_unchanged(tmp_path, monkeypatch, capsys):
    store = rd.make_store(tmp_path)
    idle_beta(store)
    line, _ = supervisor_line(tmp_path, store, monkeypatch, capsys, None)
    assert "UNMANAGED" in line and "health=idle_waiting heartbeat=" in line


def test_the_helper_is_one_rule():
    adverse = cli._health_column("STUCK_OR_DEAD", "idle_waiting")
    assert adverse == "health=STUCK_OR_DEAD (wrapper self-reports idle_waiting)"
    assert cli._health_column("HEALTHY_IDLE", "idle_waiting", "5s ago") == "health=idle_waiting/5s ago"
    assert cli._health_column(None, "unknown") == "health=unknown"


def test_the_supervisor_projection_carries_the_rate_limit_detail():
    """Fix round 1, connector 4177637232: the supervisor's own intermediate conversion
    used to drop reason_detail, so status named the usage-limit window and supervisor
    did not, off the SAME record."""
    rpt = {"health": {"state": "rate_limited_or_outage", "reason_code": "usage_limit_rejected",
                      "reason_detail": "rate_limit_event.rejected.seven_day"}}
    assessed = sup._assessment_health(rpt, None)
    assert assessed["reason_detail"] == "rate_limit_event.rejected.seven_day"
    assert cli._rate_limit_reason_flag(assessed) == "rate_limited(usage_limit window=seven_day)"


def test_an_old_record_with_no_reason_detail_is_still_forwarded_as_none():
    rpt = {"health": {"state": "rate_limited_or_outage", "reason_code": "adapter_rate_limit"}}
    assessed = sup._assessment_health(rpt, None)
    assert assessed["reason_detail"] is None


@pytest.mark.parametrize("state", ["STUCK_OR_DEAD", "TURN_FAILED", "WRAPPER_MISSING", "CLI_CHILD_UNKNOWN"])
def test_the_rate_limited_flag_is_suppressed_when_the_supervisor_cannot_confirm_healthy(
        tmp_path, monkeypatch, capsys, state):
    """Fix round 1, connector 4177637230 (P3): when the supervisor says the seat is DEAD
    or cannot confirm it healthy, the `rate_limited(...)` flag must not repeat the
    wrapper's own overridden self-report as an unqualified claim - suppressed in both
    `status` and `supervisor`, under the SAME precedence rule `_health_column` already uses."""
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="rate_limited_or_outage",
        updated_at=park.epoch_iso(rd.NOW), since=park.epoch_iso(rd.NOW - 60),
        reason_code="usage_limit_rejected", reason_detail="rate_limit_event.rejected.seven_day",
        source="wrapper"))
    line, out = supervisor_line(tmp_path, store, monkeypatch, capsys,
                                {"state": state, "action": "none", "reason": "confirmed dead child"})
    assert "rate_limited(" not in out

    monkeypatch.setattr(sup, "build_supervisor_observation", lambda *a, **k: {
        "agents": [{"name": "beta", "decision": {"state": state, "action": "none"},
                   "health": {"state": "rate_limited_or_outage", "reason_code": "usage_limit_rejected",
                             "reason_detail": "rate_limit_event.rejected.seven_day"}}],
        "event_ring": {"warnings": []}})
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    assert "rate_limited(" not in capsys.readouterr().out


def test_the_rate_limited_flag_still_shows_when_the_supervisor_confirms_healthy(tmp_path, monkeypatch, capsys):
    store = rd.make_store(tmp_path)
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="rate_limited_or_outage",
        updated_at=park.epoch_iso(rd.NOW), since=park.epoch_iso(rd.NOW - 60),
        reason_code="throttled", source="wrapper"))
    monkeypatch.setattr(sup, "build_supervisor_observation", lambda *a, **k: {
        "agents": [{"name": "beta", "decision": {"state": "HEALTHY_WORKING", "action": "none"},
                   "health": {"state": "rate_limited_or_outage", "reason_code": "throttled"}}],
        "event_ring": {"warnings": []}})
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    assert "rate_limited(throttled)" in capsys.readouterr().out


def test_the_readme_names_the_screens_that_apply_the_verdict():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = " ".join(text[text.index("When several facts disagree"):].split()[:170])
    for needle in ("`agenttalk status`", "`agenttalk supervisor`", "both web consoles",
                   "health=STUCK_OR_DEAD (wrapper self-reports idle_waiting)", "(supervisor not consulted)"):
        assert needle in section, needle
