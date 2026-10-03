"""Every reader of seat health shows a seat parked on a provider usage limit.

The park itself is built in the wrapper loop (see test_usage_park_loop). Here the published
marker and the health file a parked wrapper writes are set up directly, and each reader is
asked what it shows: the attention queue (CLI collector and web), `status`, the supervisor
report, `doctor`, the web payloads the consoles read. Nothing here starts a model.
"""

from __future__ import annotations

import json
import time


from agenttalk import attention as A
from agenttalk import cli, doctor, web
from agenttalk import supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper import loop, run
from agenttalk.wrapper import usage_park as park
from agenttalk.wrapper.health import WrapperHealthWriter

NOW = time.time()
RESET = int(NOW) + 3600
WAKE = RESET + 30


def make_store(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta", "lead"])
    store.set_operator_facing("lead")
    store.send(sender="alpha", recipient="beta", body="one")
    return store


def head_id(store):
    return store.messages_for("beta")[0].id


def park_beta(store, *, age=0.0, reset=RESET, wake=WAKE, window="five_hour", generation="g1", health=True):
    """What a parked wrapper leaves behind: the marker, its health state and a heartbeat."""
    store.write_usage_limit_park(
        "beta", window=window, reset_epoch=reset, wake_epoch=wake, message_id=head_id(store),
        parked_at=park.epoch_iso(NOW - 120 - age), wrapper_generation=generation, now_epoch=NOW - age)
    if health:
        writer = WrapperHealthWriter(store, "beta", "claude", mode="wrapper-loop", min_interval=0.0)
        writer.parked({"id": head_id(store)}, park.REASON_PARKED)
    store.write_heartbeat("beta")


def views(store):
    return cli._usage_limit_park_views(store, ["alpha", "beta", "lead"])


# ------------------------------------------------------------------ the shared view


def test_a_fresh_marker_is_a_parked_view_with_its_time(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    view = store.usage_limit_park_view("beta", now_epoch=NOW)
    assert view["state"] == "parked" and view["fresh"] is True and view["wake_epoch"] == WAKE
    assert park.park_text(view) == "parked on a usage limit until " + park.format_epoch(RESET)


def test_no_time_reads_until_restarted(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, reset=None, wake=None)
    assert park.park_text(store.usage_limit_park_view("beta", now_epoch=NOW)) == (
        "parked on a usage limit until restarted")


def test_a_consumed_wake_reads_until_restarted_even_with_a_known_reset(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, wake=None)
    assert park.park_text(store.usage_limit_park_view("beta", now_epoch=NOW)).endswith("until restarted")


def test_a_stale_marker_stays_visible_as_wrapper_not_responding(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, age=park.MARKER_STALE_SECONDS + 60)
    view = store.usage_limit_park_view("beta", now_epoch=NOW)
    assert view is not None and view["state"] == "stale" and view["fresh"] is False
    assert park.park_text(view) == "parked on a usage limit, wrapper not responding"


def test_a_fresh_marker_never_overrides_current_working_or_stuck_evidence(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, health=False)
    for state in ("working_turn", "working_silent", "stuck_suspected"):
        health = {"state": state, "stale": False}
        assert store.usage_limit_park_view("beta", health=health, now_epoch=NOW) is None, state
    # but a STALE health read is not current evidence, and other states do not contradict it
    assert store.usage_limit_park_view("beta", health={"state": "working_turn", "stale": True},
                                       now_epoch=NOW) is not None
    assert store.usage_limit_park_view("beta", health={"state": "rate_limited_or_outage", "stale": False},
                                       now_epoch=NOW) is not None


def test_an_obsolete_marker_is_reconciled_when_its_head_was_consumed(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    store.advance_cursor("beta", head_id(store))
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is None


def test_an_obsolete_marker_is_reconciled_when_the_wrapper_was_replaced(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, generation="old-generation")
    store.write_waiting("beta", {"agent": "beta", "pid": 1, "mode": "wrapper-loop", "wait_token": "new",
                                 "wrapper_generation": "new", "since": park.epoch_iso(NOW)})
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is None
    park_beta(store, generation="new")
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is not None


def test_a_reader_never_breaks_on_a_damaged_marker(tmp_path):
    store = make_store(tmp_path)
    path = store.usage_limit_park_path("beta")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is None


# ------------------------------------------------------------------ attention


def item_for(store):
    items = A.usage_limit_park_items(views(store))
    assert len(items) == 1
    return items[0]


def test_the_attention_item_says_parked_until_the_time_never_config_blocked(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    item = item_for(store)
    assert item["source"] == "usage_limit_park" and item["title"].startswith("beta: parked on a usage limit until ")
    assert park.format_epoch(RESET) in item["title"]
    assert "config" not in (item["title"] + item["why_it_matters"]).lower()
    assert item["human_can_unblock_now"] is True and item["source_refs"] == [
        {"kind": "usage_limit_park", "agent": "beta"}]
    assert "agenttalk request-restart --for beta" in item["recommendation"]
    assert f"agenttalk ack --for beta --id {head_id(store)}" in item["recommendation"]


def test_the_attention_item_without_a_time_says_until_restarted(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, reset=None, wake=None)
    assert item_for(store)["title"] == "beta: parked on a usage limit until restarted (supervisor not consulted)"


def test_the_attention_item_for_a_stale_marker_says_wrapper_not_responding(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, age=park.MARKER_STALE_SECONDS + 60)
    item = item_for(store)
    assert item["title"] == "beta: parked on a usage limit, wrapper not responding (supervisor not consulted)"
    assert "has not refreshed" in item["why_it_matters"]


def test_a_new_window_or_a_stale_marker_resurfaces_after_a_defer(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    first = item_for(store)
    park_beta(store, reset=RESET + 18000, wake=WAKE + 18000, window="seven_day")
    second = item_for(store)
    park_beta(store, age=park.MARKER_STALE_SECONDS + 60)
    third = item_for(store)
    assert len({first["source_hash"], second["source_hash"], third["source_hash"]}) == 3
    assert first["item_id"] == second["item_id"] == third["item_id"]       # one item per seat


def test_a_defer_applies_to_the_same_park_but_not_a_changed_one(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    first = item_for(store)
    event = {"action": A.ACTION_DEFER, "item_id": first["item_id"], "source_hash": first["source_hash"],
             "snapshot": {"source": first["source"]}, "at": park.epoch_iso(NOW), "by": "lead",
             "until": park.epoch_iso(NOW + 86400)}
    same = A.apply_disposition(item_for(store), A.fold_dispositions([event]), now_iso=park.epoch_iso(NOW))
    park_beta(store, reset=RESET + 18000, wake=WAKE + 18000)
    changed = A.apply_disposition(item_for(store), A.fold_dispositions([event]), now_iso=park.epoch_iso(NOW))
    assert same["state"] in ("deferred", "active") and changed["state"] == "active"


def test_the_cli_collector_surfaces_the_park_and_keeps_config_blocked_separate(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    store.write_config_blocked_hold("alpha", summary="exec denied")
    items = cli._collect_attention_items(store, for_agent=None, roster=["alpha", "beta", "lead"])
    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    assert [i["title"] for i in by_source["usage_limit_park"]][0].startswith("beta: parked on a usage limit")
    assert [i["title"] for i in by_source["config_blocked"]] == ["config-blocked hold: alpha"]
    assert all("usage limit" not in i["title"] for i in by_source["config_blocked"])


def test_the_cli_attention_command_lists_it(tmp_path, capsys):
    store = make_store(tmp_path)
    park_beta(store)
    rc = cli.main(["--root", str(tmp_path), "attention"])
    out = capsys.readouterr().out
    assert rc == 0 and "parked on a usage limit until" in out and "beta" in out


def test_the_web_attention_map_labels_it_parked_with_medium_severity(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    payload = web.build_attention(web.RootDescriptor(store=store, label="root"))
    entries = [e for e in payload["items"] if e["agent"] == "beta"]
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["source"], entry["source_label"], entry["severity"]) == ("usage_limit_park", "PARKED", "med")
    assert entry["title"].startswith("beta: parked on a usage limit until ")
    assert "agenttalk request-restart --for beta" in entry["recommendation"]
    assert entry["human_can_unblock_now"] is True and "config" not in entry["title"].lower()


def test_the_web_risk_register_carries_it_as_a_usage_limit_risk(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    risks = web.build_risk_register(web.RootDescriptor(store=store, label="root"))["items"]
    mine = [r for r in risks if r["owner"] == "beta" and r["category"] == "usage_limit_park"]
    assert len(mine) == 1 and mine[0]["category_label"] == "Usage limit" and mine[0]["severity"] == "med"


# ------------------------------------------------------------------ status and the supervisor report


def test_status_flags_a_parked_seat_with_its_time(tmp_path, capsys):
    store = make_store(tmp_path)
    park_beta(store)
    rc = cli.main(["--root", str(tmp_path), "status"])
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("beta"))
    assert rc == 0 and f"usage_limit_parked(until={park.format_epoch(RESET)})" in line
    assert "config_blocked" not in line


def test_status_flags_no_time_and_a_stale_marker(tmp_path, capsys):
    store = make_store(tmp_path)
    park_beta(store, reset=None, wake=None)
    cli.main(["--root", str(tmp_path), "status"])
    assert "usage_limit_parked(until=restarted)" in capsys.readouterr().out
    park_beta(store, age=park.MARKER_STALE_SECONDS + 60)
    cli.main(["--root", str(tmp_path), "status"])
    assert "usage_limit_parked(wrapper_not_responding)" in capsys.readouterr().out


def test_status_json_carries_the_view_only_for_the_parked_seat(tmp_path, capsys):
    store = make_store(tmp_path)
    park_beta(store)
    cli.main(["--root", str(tmp_path), "status", "--json"])
    rows = {a["name"]: a for a in json.loads(capsys.readouterr().out)["agents"]}
    assert rows["beta"]["usage_limit_park"]["state"] == "parked" and "usage_limit_park" not in rows["alpha"]


def test_status_shows_nothing_for_an_obsolete_marker(tmp_path, capsys):
    store = make_store(tmp_path)
    park_beta(store)
    store.advance_cursor("beta", head_id(store))
    cli.main(["--root", str(tmp_path), "status"])
    assert "usage_limit_parked" not in capsys.readouterr().out


def test_the_supervisor_report_row_and_flag(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    report = sup.build_report(store, now_epoch=NOW)
    row = report["agents"]["beta"]["usage_limit_park"]
    assert row["state"] == "parked"
    assert sup.supervisor_agent_assessment("beta", report["agents"]["beta"], None)["usage_limit_park"] == {
        "present": True, "state": "parked", "window": "five_hour", "reset_epoch": RESET, "wake_epoch": WAKE,
            "supervisor_consulted": False}
    assert sup.supervisor_agent_assessment("alpha", report["agents"]["alpha"], None)["usage_limit_park"] == {
        "present": False}


def test_the_supervisor_flag_text():
    row = {"present": True, "state": "parked", "reset_epoch": RESET, "wake_epoch": WAKE}
    assert cli._usage_limit_park_flag(row) == f"usage_limit_parked(until={park.format_epoch(RESET)})"
    assert cli._usage_limit_park_flag({"present": True, "state": "parked"}) == "usage_limit_parked(until=restarted)"
    assert cli._usage_limit_park_flag({"present": True, "state": "stale"}) == (
        "usage_limit_parked(wrapper_not_responding)")
    assert cli._usage_limit_park_flag({"present": False}) is None and cli._usage_limit_park_flag(None) is None


# ------------------------------------------------------------------ doctor


def test_doctor_warns_with_the_time_and_never_errors(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    check = doctor._check_usage_limit_parks(store)
    assert check.name == "usage_limit_park" and check.status == "warn"
    assert "beta: parked on a usage limit until " + park.format_epoch(RESET) in check.details
    assert "config" not in check.details.lower()
    assert check.data["parked"][0]["long_park"] is False


def test_doctor_says_when_a_seat_has_been_parked_for_a_day_and_the_age_is_configurable(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.write_usage_limit_park(
        "beta", window="seven_day", reset_epoch=None, wake_epoch=None, message_id=head_id(store),
        parked_at=park.epoch_iso(NOW - 30 * 3600), wrapper_generation="g1", now_epoch=NOW)
    check = doctor._check_usage_limit_parks(store)
    assert "parked for over 24 h, check it" in check.details and check.data["parked"][0]["long_park"] is True
    monkeypatch.setenv(park.PARK_WARN_ENV, "48")
    assert "check it" not in doctor._check_usage_limit_parks(store).details
    monkeypatch.setenv(park.PARK_WARN_ENV, "not a number")
    assert "parked for over 24 h" in doctor._check_usage_limit_parks(store).details


def test_doctor_names_a_stale_marker_and_is_absent_without_a_park(tmp_path):
    store = make_store(tmp_path)
    assert doctor._check_usage_limit_parks(store) is None
    park_beta(store, age=park.MARKER_STALE_SECONDS + 60)
    assert "wrapper not responding" in doctor._check_usage_limit_parks(store).details


def test_doctor_lists_a_park_notice_that_never_routed(tmp_path):
    store = make_store(tmp_path)
    record = {"id": head_id(store), "kind": "message", "from": "alpha", "to": "beta"}
    store.record_attempt_start("beta", record, attempt_id="a1", at="2026-09-09T03:00:00Z")
    store.record_attempt_result(
        "beta", head_id(store), failure_class=park.CLASS_USAGE_LIMIT, summary="x", at="2026-09-09T03:00:01Z",
        usage_limit={"generation": "g1", "window": "five_hour", "reset_epoch": None})
    check = doctor._check_usage_limit_parks(store)
    assert check is not None and "never reached anyone" in check.details
    assert check.data["unrouted_notices"][0]["agent"] == "beta"
    store.mark_usage_notice("beta", head_id(store), routed=True, next_at_epoch=None)
    assert doctor._check_usage_limit_parks(store) is None


def test_doctor_run_includes_the_check(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    assert "usage_limit_park" in [c.name for c in doctor.run(tmp_path).checks]


# ------------------------------------------------------------------ the web payloads the consoles read


def test_the_web_state_agent_row_carries_the_park_view(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    rows = {a["name"]: a for a in root["agents"]}
    assert rows["beta"]["usage_limit_park"]["state"] == "parked"
    assert rows["beta"]["usage_limit_park"]["wake_epoch"] == WAKE
    assert "usage_limit_park" not in rows["alpha"] and "usage_limit_park" not in rows["lead"]
    assert rows["beta"]["health"]["reason_code"] == "usage_limit_parked"


def test_the_web_state_row_for_a_stale_marker_is_still_there(tmp_path):
    store = make_store(tmp_path)
    park_beta(store, age=park.MARKER_STALE_SECONDS + 60)
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    assert {a["name"]: a for a in root["agents"]}["beta"]["usage_limit_park"]["state"] == "stale"


def test_the_web_payloads_carry_no_text_from_the_park(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    row = {a["name"]: a for a in root["agents"]}["beta"]["usage_limit_park"]
    assert set(row) == {"present", "state", "fresh", "window", "reset_epoch", "wake_epoch", "message_id",
                        "parked_at", "age_seconds"}


# ------------------------------------------------------------------ the qwen gateway path stays apart


def test_a_gateway_child_turn_expiry_is_config_blocked_never_a_usage_park():
    sig = {"error": "atgw_child_turn_cap_exceeded", "terminal_text": "", "usage_stream": {
        "rejected": {"window": "five_hour", "reset_epoch": RESET}, "result_is_error": True}}
    cls, _ = run._classify_drive_failure(sig, backend_profile="ovh-qwen")
    assert cls == loop.CLASS_CONFIG_BLOCKED
    assert run._usage_limit_fact(sig, cls) is None


def test_a_usage_park_and_a_config_block_never_share_a_state_word(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    store.write_config_blocked_hold("alpha", summary="gateway held")
    parked_text = park.park_text(store.usage_limit_park_view("beta", now_epoch=NOW))
    assert "config" not in parked_text
    assert store.read_config_blocked_hold("beta") is None and store.read_config_blocked_hold("alpha") is not None
    assert store.usage_limit_park_view("alpha", now_epoch=NOW) is None
