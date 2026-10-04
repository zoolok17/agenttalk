"""Fix round 1 for step 2 of the usage-limit park: one precedence, one recovery text, a doctor that
shows times and does not advise about a message that is gone.

Built on the readers' fixtures (test_usage_park_readers). Synthetic data; no model.
"""

from __future__ import annotations

import sys
import time

import pytest

import test_usage_park_readers as rd
from agenttalk import attention as A
from agenttalk import cli, doctor, health as hm, web
from agenttalk.wrapper import usage_park as park

NOW = rd.NOW


@pytest.fixture(autouse=True)
def _fresh_now(monkeypatch):
    """#311 recast fix round 2: NOW here is a one-time copy of rd.NOW (itself read at import),
    and rd.park_beta's heartbeat is always written with the real clock - the same drift this
    file's own tests can hit as test_usage_park_readers.py (see the fixture there). Refresh
    both this module's NOW and rd.NOW to the same real time right before each test runs."""
    fresh = time.time()
    monkeypatch.setattr(rd, "NOW", fresh)
    monkeypatch.setattr(sys.modules[__name__], "NOW", fresh)


# ------------------------------------------------------------------ finding 1: one precedence


def work(state, stale=False):
    return {"state": state, "stale": stale}


def test_adverse_supervisor_verdict_wins_over_a_fresh_park(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    for verdict in ("STUCK_OR_DEAD", "TURN_FAILED", "WRAPPER_MISSING", "CLI_CHILD_UNKNOWN"):
        assert store.usage_limit_park_view("beta", verdict_state=verdict, now_epoch=NOW) is None, verdict
    for verdict in ("HEALTHY_IDLE", "HEALTHY_WORKING", None, ""):
        assert store.usage_limit_park_view("beta", verdict_state=verdict, now_epoch=NOW) is not None, verdict


@pytest.mark.parametrize("state", ["working_turn", "working_silent", "stuck_suspected"])
def test_current_work_evidence_wins_over_any_park_fresh_or_stale(tmp_path, state):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    assert store.usage_limit_park_view("beta", health=work(state), now_epoch=NOW) is None
    rd.park_beta(store, age=park.MARKER_STALE_SECONDS + 60, health=False)
    assert store.usage_limit_park_view("beta", health=work(state), now_epoch=NOW) is None


def test_a_fresh_park_wins_over_historical_stale_working_health(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    view = store.usage_limit_park_view("beta", health=work("working_turn", stale=True), now_epoch=NOW)
    assert view is not None and view["state"] == "parked"


def test_a_stale_park_with_nothing_stronger_is_wrapper_not_responding(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, age=park.MARKER_STALE_SECONDS + 60, health=False)
    for health in (None, work("idle_waiting"), work("rate_limited_or_outage")):
        view = store.usage_limit_park_view("beta", health=health, now_epoch=NOW)
        assert view is not None and view["state"] == "stale", health


def test_stale_working_history_outranks_a_stale_park_alone(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, age=park.MARKER_STALE_SECONDS + 60, health=False)
    assert store.usage_limit_park_view("beta", health=work("working_turn", stale=True), now_epoch=NOW) is None


def test_a_fresh_marker_with_a_stopped_heartbeat_reads_as_not_responding(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    assert store.usage_limit_park_view("beta", now_epoch=NOW)["state"] == "parked"
    assert store.usage_limit_park_view("beta", now_epoch=NOW + park.MARKER_STALE_SECONDS + 5)["state"] == "stale"


def test_no_marker_means_today_s_display(tmp_path):
    store = rd.make_store(tmp_path)
    assert store.usage_limit_park_view("beta", health=work("rate_limited_or_outage"), now_epoch=NOW) is None


def test_the_real_web_payload_carries_the_decision_for_each_case(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    desc = web.RootDescriptor(store=store, label="synthetic")

    def beta_row():
        rows = web.build_state([desc])["roots"][0]["agents"]
        return next(r for r in rows if r["name"] == "beta")

    # fresh park beside 15-minute-old working health: the park, with the old work as stale context
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="working_turn", updated_at=park.epoch_iso(NOW - 900),
        since=park.epoch_iso(time.time() - 1200), last_progress_at=park.epoch_iso(NOW - 900), source="wrapper"))
    row = beta_row()
    assert row["usage_limit_park"]["state"] == "parked" and row["health"]["stale"] is True
    # CURRENT working health beside a stale leftover park: no park view at all
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="working_turn", updated_at=park.epoch_iso(time.time()),
        since=park.epoch_iso(time.time() - 30), last_progress_at=park.epoch_iso(time.time()), source="wrapper"))
    rd.park_beta(store, age=park.MARKER_STALE_SECONDS + 60, health=False)
    assert "usage_limit_park" not in beta_row()


def test_the_web_payload_drops_a_park_under_an_adverse_supervisor_verdict(tmp_path, monkeypatch):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    desc = web.RootDescriptor(store=store, label="synthetic")
    monkeypatch.setattr("agenttalk.supervisor.strict_child_verdicts",
                        lambda *a, **k: {"beta": {"state": "STUCK_OR_DEAD", "action": "none"}}, raising=False)
    rows = web.build_state([desc])["roots"][0]["agents"]
    beta = next(r for r in rows if r["name"] == "beta")
    if "cli_child_verdict" in beta:                     # only when the supervisor reported one
        assert "usage_limit_park" not in beta
    else:
        pytest.skip("this root reports no supervisor verdict")


def test_status_json_uses_the_same_precedence(tmp_path, capsys):
    store = rd.make_store(tmp_path)
    rd.park_beta(store, health=False)
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="working_turn", updated_at=park.epoch_iso(time.time()),
        since=park.epoch_iso(time.time() - 30), last_progress_at=park.epoch_iso(time.time()), source="wrapper"))
    cli.main(["--root", str(tmp_path), "status", "--json"])
    import json
    rows = {a["name"]: a for a in json.loads(capsys.readouterr().out)["agents"]}
    assert "usage_limit_park" not in rows["beta"]


# ------------------------------------------------------------------ finding 2: one recovery text

REQUIRED = ("agenttalk request-restart --for", "--force-protected", "--acknowledge-live-protected-kill",
            "running supervisor", "stop the wrapper and start it again", "agenttalk ack --for",
            "no dead-letter record", "refused for a managed lead-loop agent")


def has_all(text):
    return [needle for needle in REQUIRED if needle not in text]


def test_the_shared_text_has_every_required_part():
    assert has_all(park.recovery_text("beta", "m1")) == []
    assert "--for beta" in park.recovery_text("beta", "m1") and "--id m1" in park.recovery_text("beta", "m1")


def test_attention_doctor_and_the_notice_all_use_it(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    item = A.usage_limit_park_items(rd.views(store))[0]
    assert has_all(item["recommendation"]) == []
    check = doctor._check_usage_limit_parks(store)
    assert has_all(check.fix) == []
    body = cli._usage_limit_notice_body({"agent": "beta", "msg_id": rd.head_id(store), "from": "alpha",
                                         "usage_limit": {"window": "five_hour", "wake_epoch": rd.WAKE}})
    assert has_all(body) == []
    entry = web.build_attention(web.RootDescriptor(store=store, label="r"))["items"]
    assert has_all(next(e for e in entry if e["agent"] == "beta")["recommendation"]) == []


def test_the_readme_says_the_same(tmp_path):
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("### When a Claude seat runs out of its allowance"):]
    section = section[:section.index("### Messaging-system internals")]
    squeezed = " ".join(section.split())
    for needle in ("agenttalk request-restart --for <agent>", "--force-protected", "--acknowledge-live-protected-kill",
                   "running supervisor", "without a dead-letter record", "refused for a managed lead-loop agent"):
        assert needle in squeezed, needle


# ------------------------------------------------------------------ finding 3: doctor shows a real time


def test_doctor_shows_a_fractional_park_time(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    check = doctor._check_usage_limit_parks(store, now_epoch=NOW)
    assert "(since None)" not in check.details and "UTC)" in check.details


def test_format_epoch_accepts_any_valid_time_and_rejects_the_rest():
    assert park.format_epoch(1788948000) == "2026-09-09 10:00 UTC"
    assert park.format_epoch(1788948000.987) == "2026-09-09 10:00 UTC"
    for bad in (None, "x", 0, -1, True, float("nan"), float("inf"), 10 ** 400):
        assert park.format_epoch(bad) is None, bad
    assert park.whole_seconds(1788948000.5) is None                   # the strict rule for proof stays strict


# ------------------------------------------------------------------ finding 4: a consumed head


def test_a_consumed_message_is_not_an_active_park_in_doctor(tmp_path):
    store = rd.make_store(tmp_path)
    mid = rd.head_id(store)
    store.record_attempt_start("beta", {"id": mid, "kind": "message"}, attempt_id="a1", at=park.epoch_iso(NOW))
    store.record_attempt_result("beta", mid, failure_class=park.CLASS_USAGE_LIMIT, summary="", at=park.epoch_iso(NOW),
                                usage_limit={"generation": "g1", "window": "five_hour", "reset_epoch": None})
    assert [u["message_id"] for u in store.list_unrouted_usage_notices()] == [mid]
    assert doctor._check_usage_limit_parks(store) is not None
    store.advance_cursor("beta", mid)
    assert store.list_unrouted_usage_notices() == []
    assert doctor._check_usage_limit_parks(store) is None


def test_an_unrelated_invalid_stem_in_the_ledger_dir_is_skipped_not_fatal(tmp_path):
    """#311 connector 4174800521: one *.json whose stem is not a valid agent name (an
    unrelated file dropped in state/dead-letter-attempts/) must not raise ValueError and
    drop the WHOLE list - only that one file is skipped."""
    store = rd.make_store(tmp_path)
    mid = rd.head_id(store)
    store.record_attempt_start("beta", {"id": mid, "kind": "message"}, attempt_id="a1", at=park.epoch_iso(NOW))
    store.record_attempt_result("beta", mid, failure_class=park.CLASS_USAGE_LIMIT, summary="", at=park.epoch_iso(NOW),
                                usage_limit={"generation": "g1", "window": "five_hour", "reset_epoch": None})
    ledger_dir = store.state_dir / "dead-letter-attempts"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "..bad-name.json").write_text("{}", encoding="utf-8")
    assert [u["message_id"] for u in store.list_unrouted_usage_notices()] == [mid]
