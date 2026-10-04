"""Every reader of seat health shows a seat parked on a provider usage limit.

The park itself is built in the wrapper loop (see test_usage_park_loop). Here the published
marker and the health file a parked wrapper writes are set up directly, and each reader is
asked what it shows: the attention queue (CLI collector and web), `status`, the supervisor
report, `doctor`, the web payloads the consoles read. Nothing here starts a model.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from agenttalk import attention as A
from agenttalk import cli, doctor, threads, web
from agenttalk import supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper import loop, run
from agenttalk.wrapper import usage_park as park
from agenttalk.wrapper.health import WrapperHealthWriter

#: Proof knob for #311 recast fix round 2, not read in production: back-dates the IMPORT-TIME
#: NOW below by this many seconds, simulating a test run where collection happened that long
#: before this file's tests actually execute (the exact shape of the CI drift this round
#: fixes). The autouse fixture further down always resets NOW to the real clock before each
#: test body runs, so with the fix in place this knob should change nothing; it exists to
#: prove that, not to be read by any production code. Deliberately NOT an AGENTTALK_* name -
#: conftest.py's autouse _clear_agenttalk_env fixture strips that whole prefix between tests.
_NOW_SHIFT_ENV = "PARK_TEST_NOW_SHIFT_SECONDS"

NOW = time.time() - float(os.environ.get(_NOW_SHIFT_ENV) or "0")
RESET = int(NOW) + 3600
WAKE = RESET + 30


@pytest.fixture(autouse=True)
def _fresh_now(monkeypatch):
    """#311 recast fix round 2: NOW is read once, at import, but park_beta's heartbeat is
    always written with the real clock (store.write_heartbeat takes no override). In a long
    combined run those two can be minutes apart by the time a given test actually executes,
    and the reader's bounded heartbeat-skew check (correctly) calls that gap impossible. Reset
    NOW to the real time right before each test runs so both sides stay within a fraction of a
    second of each other, regardless of how long the run has been going."""
    monkeypatch.setattr(sys.modules[__name__], "NOW", time.time())


def make_store(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta", "lead"])
    store.set_operator_facing("lead")
    store.send(sender="alpha", recipient="beta", body="one")
    return store


def head_id(store):
    return store.messages_for("beta")[0].id


def durable_park(store, agent, mid, *, window="five_hour", parked_at=None):
    """The durable attempt-record half of a park, for a test that writes the marker directly
    (#311 round 2: the record decides whether a park exists; a marker with nothing durable
    behind it is no longer a park at all)."""
    attempts = store.dead_letter_attempts(agent)
    rec = dict(attempts["messages"].get(mid) or {})
    rec.update(park_state="parked", limit_window=window, parked_at=parked_at)
    attempts["messages"][mid] = rec
    store._write_attempts(agent, attempts)


def park_beta(store, *, age=0.0, reset=RESET, wake=WAKE, window="five_hour", generation="g1", health=True):
    """What a parked wrapper leaves behind: the marker, its health state, a heartbeat, and the
    DURABLE attempt record the marker is only an optional view of (#311 round 2: the record
    decides whether a park exists at all; the marker is reconciled against it)."""
    mid = head_id(store)
    parked_at = park.epoch_iso(NOW - 120 - age)
    store.write_usage_limit_park(
        "beta", provider="claude", window=window, reset_epoch=reset, wake_epoch=wake, message_id=mid,
        parked_at=parked_at, wrapper_generation=generation, now_epoch=NOW - age)
    attempts = store.dead_letter_attempts("beta")
    rec = dict(attempts["messages"].get(mid) or {})
    rec.update(park_state="parked", limit_window=window, parked_at=parked_at)
    attempts["messages"][mid] = rec
    store._write_attempts("beta", attempts)
    if health:
        writer = WrapperHealthWriter(store, "beta", "claude", mode="wrapper-loop", min_interval=0.0)
        writer.parked({"id": mid}, park.REASON_PARKED)
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


def test_a_generation_mismatched_marker_loses_its_details_not_the_durable_park(tmp_path):
    """#311 park reader recast: a marker whose wrapper_generation no longer matches the
    live wrapper is a replaced instance's STALE VIEW of the park, not proof the park itself
    ended - the durable attempt record (written by ``park_beta`` regardless of generation)
    still says parked. Its reset/wake detail is dropped (a NEW wrapper instance has not
    reconfirmed them), but the row stays, with freshness decided by the heartbeat
    ``park_beta`` just wrote - the one real liveness signal still available."""
    store = make_store(tmp_path)
    park_beta(store, generation="old-generation")
    store.write_waiting("beta", {"agent": "beta", "pid": 1, "mode": "wrapper-loop", "wait_token": "new",
                                 "wrapper_generation": "new", "since": park.epoch_iso(NOW)})
    view = store.usage_limit_park_view("beta", now_epoch=NOW)
    assert view is not None and view["fresh"] is True and view["state"] == "parked"
    assert view["reset_epoch"] is None and view["wake_epoch"] is None, (
        "the mismatched marker's reset/wake detail must not be trusted")
    park_beta(store, generation="new")
    view = store.usage_limit_park_view("beta", now_epoch=NOW)
    assert view is not None and view["reset_epoch"] == RESET, "a matching marker's detail is trusted again"


def test_a_reader_never_breaks_on_a_damaged_marker(tmp_path):
    store = make_store(tmp_path)
    path = store.usage_limit_park_path("beta")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is None


# #311 blocker 2 / connector 4174800511, 4174800514, 4174800515: marker times that pass the
# reader can still break the programs that display them. The shared reader (store.py) is where
# this is fixed - a field no known reader (Python's datetime, JS's Date) could ever show is
# dropped (never the whole marker, except the one field everything else is computed from), and
# a file missing a documented key outright is refused, not silently read as null.


def _raw_marker(store):
    path = store.usage_limit_park_path("beta")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_raw_marker(store, data):
    path = store.usage_limit_park_path("beta")
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_reset_or_wake_beyond_every_readers_range_is_dropped_not_the_whole_marker(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    data = _raw_marker(store)
    data["reset_epoch"] = 10 ** 14          # the reviewer's exact repro: breaks a JS Date
    data["wake_epoch"] = 10 ** 14
    _write_raw_marker(store, data)
    marker = store.read_usage_limit_park("beta", now_epoch=NOW)
    assert marker is not None                           # the rest of the marker still reads
    assert marker["reset_epoch"] is None and marker["wake_epoch"] is None


def test_a_parked_at_that_parses_but_no_reader_can_show_is_dropped(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    data = _raw_marker(store)
    data["parked_at"] = "0001-01-01"         # parses; .timestamp() raises OSError on Windows
    _write_raw_marker(store, data)
    marker = store.read_usage_limit_park("beta", now_epoch=NOW)
    assert marker is not None and marker["parked_at"] is None


def test_an_undisplayable_updated_at_epoch_makes_the_whole_marker_unreadable(tmp_path):
    store = make_store(tmp_path)
    park_beta(store)
    data = _raw_marker(store)
    data["updated_at_epoch"] = 10 ** 14      # everything else (age, freshness) derives from this
    _write_raw_marker(store, data)
    assert store.read_usage_limit_park("beta", now_epoch=NOW) is None


def test_a_future_updated_at_epoch_beyond_clock_skew_is_not_fresh(tmp_path):
    """Round-1 regression: ``max(0.0, age)`` alone clamped a future updated_at_epoch to age
    zero and called it fresh. The same bounded-future-skew rule the health/heartbeat readers
    use now applies here too (#311 connector 4174800515)."""
    store = make_store(tmp_path)
    park_beta(store)
    data = _raw_marker(store)
    data["updated_at_epoch"] = int(NOW) + 3600        # an hour in the future: not ordinary skew
    _write_raw_marker(store, data)
    marker = store.read_usage_limit_park("beta", now_epoch=NOW)
    assert marker is not None and marker["fresh"] is False


@pytest.mark.parametrize("missing", ["wake_epoch", "reset_epoch", "parked_at", "wrapper_generation"])
def test_a_marker_missing_a_documented_nullable_key_is_refused_not_read_as_null(tmp_path, missing):
    """The documented version-1 contract is eleven keys, always present - a file missing one
    (even a nullable one) is a damaged write the reader must retry, not treat as if the field
    were simply null (#311 connector 4174800511)."""
    store = make_store(tmp_path)
    park_beta(store)
    data = _raw_marker(store)
    del data[missing]
    _write_raw_marker(store, data)
    assert store.read_usage_limit_park("beta", now_epoch=NOW) is None


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


def test_the_attention_items_age_is_the_parks_own_age_not_the_markers_refresh_age(tmp_path):
    """#311 connector 4174800507: the marker republishes at most once a minute while parked, so
    its OWN age_seconds stays small for a park that has lasted days - using it for the item's
    age made a days-old park look seconds old. Derive the item's age from parked_at instead;
    the marker's own age stays used for freshness only (park_text/park_view, unchanged)."""
    store = make_store(tmp_path)
    mid = head_id(store)
    three_days_ago = park.epoch_iso(NOW - 3 * 86400)
    store.write_usage_limit_park(
        "beta", provider="claude", window="five_hour", reset_epoch=RESET, wake_epoch=WAKE,
        message_id=mid, parked_at=three_days_ago, wrapper_generation="g1", now_epoch=NOW)
    durable_park(store, "beta", mid, parked_at=three_days_ago)
    item = item_for(store)
    assert item["age_seconds"] > 2 * 86400


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


def test_a_defer_renews_when_the_park_restarts_or_its_automatic_wake_disappears(tmp_path):
    """#311 recast fix round 1, finding 2: the item's content-bound hash omitted
    `parked_at` and `wake_epoch` - a NEW park for the SAME message (same window, same
    reset) kept the old hash, and so did a park whose automatic wake disappeared
    (consumed or no longer stated) with everything else unchanged. A hash that never
    changes is exactly how a card stays silently deferred through a real incident
    change (``attention.py``'s own queue filters a deferred card out by hash match, not
    by re-deriving "is this truly the same incident" itself) - checking the hash
    directly here is deterministic, where going through a hand-built disposition event
    and ``apply_disposition`` is not (the existing round-2 test already accepts either
    "deferred" or "active" for its own unchanged case, for the same reason)."""
    store = make_store(tmp_path)
    park_beta(store)
    mid = head_id(store)
    first_hash = item_for(store)["source_hash"]

    same_again_hash = item_for(store)["source_hash"]
    assert same_again_hash == first_hash, "the hash is not even stable across an unchanged read"

    # A new park on the SAME message: same window, same reset/wake - only parked_at moves.
    new_parked_at = park.epoch_iso(NOW + 500)
    store.write_usage_limit_park(
        "beta", provider="claude", window="five_hour", reset_epoch=RESET, wake_epoch=WAKE,
        message_id=mid, parked_at=new_parked_at, wrapper_generation="g1", now_epoch=NOW)
    durable_park(store, "beta", mid, parked_at=new_parked_at)
    reparked_hash = item_for(store)["source_hash"]
    assert reparked_hash != first_hash, "a new park on the same message kept the old hash"

    # The automatic wake disappears (consumed/cleared) - window/reset/parked_at unchanged
    # from the ORIGINAL park (not the reparked one above).
    park_beta(store, wake=None)
    wake_gone_hash = item_for(store)["source_hash"]
    assert wake_gone_hash != first_hash, "a park whose wake disappeared kept the old hash"


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


def test_a_routed_park_notice_is_never_a_second_card_next_to_the_canonical_row(tmp_path):
    """A routed park notice used to also create a pending needs_operator card next to the
    canonical usage_limit_park attention item - two cards for one park (#311 connector
    4174800513). Round 1 fixed this by coalescing the notice; round 2 replaced that with a
    simpler fix at the source - the notice is sent informationally (kind "message", no
    needs_operator meta), so it is never collected as a pending operator item at all."""
    store = make_store(tmp_path)
    park_beta(store)
    mid = head_id(store)
    info = {"agent": "beta", "msg_id": mid, "from": "alpha", "kind": "message",
            "failure_class": "usage_limit", "attempts": 1,
            "usage_limit": {"notice_key": "park:1", "window": "five_hour", "reset_epoch": RESET,
                            "wake_epoch": WAKE, "again": False}}
    assert cli._dead_letter_notifier(store, "beta")(info, disposed=False) is True
    items = cli._collect_attention_items(store, for_agent="lead", roster=["alpha", "beta", "lead"])
    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    assert len(by_source.get("usage_limit_park", [])) == 1
    assert "needs_operator" not in by_source


def test_a_recovered_park_leaves_no_operator_obligation(tmp_path):
    """#311 round 2, finding 1 (lead decision): recovering from a park used to still leave
    the liaison's notice thread "owed-inbound"/operator_state=pending forever, because
    filtering a notice from the attention feed (round 1's fix) never retired its question in
    the shared thread reducer. Fixed at the source - see test_usage_park_wiring's notice
    tests - verified here end to end through the real notifier and derive_threads."""
    from test_usage_park_wiring import info as wiring_info

    store = make_store(tmp_path)
    park_beta(store)
    mid = head_id(store)
    assert cli._dead_letter_notifier(store, "beta")(wiring_info(msg_id=mid), disposed=False) is True
    notice = list(store.messages_for("lead"))[-1]

    # Model recovery: the message is consumed, the marker and attempt record both go, and the
    # liaison has read the notice.
    store.advance_cursor("beta", mid)
    store.clear_usage_limit_park("beta")
    store.clear_attempt("beta", mid)
    store.advance_cursor("lead", notice.id)

    assert cli._collect_attention_items(store, for_agent="lead", roster=["alpha", "beta", "lead"]) == []
    # "message" (the notice's kind) is not one of threads.OPENER_KINDS, so derive_threads
    # tracks no thread at all for it - never "pending", because never tracked as owed at all.
    rows = threads.derive_threads(store.valid_messages(), agent="lead", cursor=notice.id)
    assert not any(t.request_id == notice.meta["request_id"] for t in rows)


def test_an_active_park_without_a_marker_still_shows_its_canonical_card(tmp_path):
    """#311 park reader recast (finding 3): the shared reader itself - not only the CLI
    collector - must show a durable park that has no marker. Marker publication is advisory
    and can fail while the notice sends successfully and the park itself is very much still
    active; the canonical card must not depend on the marker having been written. With no
    heartbeat at all here, there is no liveness evidence either, so the row reads as
    "wrapper not responding", never silently as healthy."""
    store = make_store(tmp_path)
    mid = head_id(store)
    at = park.epoch_iso(NOW)
    store.record_attempt_start("beta", {"id": mid}, attempt_id="synthetic", at=at)
    store.record_attempt_result(
        "beta", mid, failure_class="usage_limit", summary="", at=at,
        usage_limit={"generation": "g1", "window": "five_hour", "provider": "claude", "reset_epoch": None})
    view = store.usage_limit_park_view("beta", now_epoch=NOW)
    assert view is not None and view["fresh"] is False, "no marker and no heartbeat is not fresh evidence"
    items = cli._collect_attention_items(store, for_agent="lead", roster=["alpha", "beta", "lead"])
    assert any(i["source"] == "usage_limit_park" and i["title"].startswith("beta:") for i in items), items


def test_a_failed_marker_delete_does_not_outlive_the_durable_park_state(tmp_path):
    """#311 round 2, finding 4 (connector 4175000403): usage_limit_park_view reconciled the
    marker against the cursor and the wrapper generation, but not against the message's own
    durable park_state - a marker that survives a failed deletion kept showing a park the
    attempt record itself already says is over."""
    store = make_store(tmp_path)
    park_beta(store)
    mid = head_id(store)
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is not None
    store.clear_attempt("beta", mid)          # the durable record no longer says parked...
    # ...but the marker file itself is still sitting there (as it would after a failed unlink).
    assert store.usage_limit_park_path("beta").exists()
    assert store.usage_limit_park_view("beta", now_epoch=NOW) is None


def test_a_damaged_window_value_does_not_discard_other_agents_park_cards(tmp_path):
    """#311 recast fix round 1, finding 3: `limit_window` copied straight from the durable
    record (the no-marker fallback branch) was never validated the way the marker path's
    own `window` is - a damaged record (a list, from hand-editing or a bug) reached a
    dict lookup in attention.py as an unhashable key and raised, losing EVERY park card
    on both attention collectors, not just the damaged agent's own row."""
    store = make_store(tmp_path)
    # alpha: a second, separately-parked agent, cleanly set up via the marker path.
    alpha_msg = store.send(sender="lead", recipient="alpha", body="for alpha")
    alpha_parked_at = park.epoch_iso(NOW - 60)
    store.write_usage_limit_park(
        "alpha", provider="claude", window="five_hour", reset_epoch=RESET, wake_epoch=WAKE,
        message_id=alpha_msg.id, parked_at=alpha_parked_at, wrapper_generation="g1", now_epoch=NOW)
    durable_park(store, "alpha", alpha_msg.id, parked_at=alpha_parked_at)
    store.write_heartbeat("alpha")

    # beta: no marker at all (the fallback branch), with a DAMAGED limit_window (a list).
    mid = head_id(store)
    durable_park(store, "beta", mid, window=["five_hour"], parked_at=park.epoch_iso(NOW - 60))
    store.write_heartbeat("beta")

    for collect in (
        lambda: cli._collect_attention_items(store, for_agent=None, roster=["alpha", "beta", "lead"]),
        lambda: web.build_attention(web.RootDescriptor(store=store, label="root"))["items"],
    ):
        items = collect()
        by_source: dict[str, list] = {}
        for it in items:
            by_source.setdefault(it.get("source"), []).append(it)
        parks = by_source.get("usage_limit_park", [])
        assert len(parks) == 2, f"expected both agents' park cards, got {by_source}"
        assert A.SOURCE_ERROR not in by_source, by_source


def test_usage_limit_park_items_isolates_one_malformed_view_from_the_rest():
    """#311 recast fix round 1, finding 3 (direct unit test, no store involved): even
    with the source-level normalisation in place, `A.usage_limit_park_items` must
    isolate each view's OWN rendering failure - a damaged field reaching it any other
    way (a future caller, a bug elsewhere) must still degrade to one `source_error`
    item for that agent alone, never lose every other agent's valid park card."""
    good = {"agent": "alpha", "present": True, "state": "parked", "fresh": True,
            "window": "five_hour", "reset_epoch": RESET, "wake_epoch": WAKE,
            "message_id": "m-alpha", "parked_at": park.epoch_iso(NOW - 60), "age_seconds": None}
    bad = {"agent": "beta", "present": True, "state": "parked", "fresh": True,
           "window": ["five_hour"], "reset_epoch": None, "wake_epoch": None,
           "message_id": "m-beta", "parked_at": park.epoch_iso(NOW - 60), "age_seconds": None}
    items = A.usage_limit_park_items([good, bad])
    by_source: dict[str, list] = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    assert [i["item_id"] for i in by_source.get("usage_limit_park", [])] == ["usage_limit_park:alpha"]
    assert len(by_source.get(A.SOURCE_ERROR, [])) == 1


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


def test_a_parked_at_that_parses_but_overflows_timestamp_does_not_crash_doctor(tmp_path):
    """#311 blocker 2's exact probe repro: a date that PARSES (datetime.fromisoformat succeeds)
    but whose .timestamp() raises OSError on Windows for an early enough date. Fixed at the
    shared reader (parked_at is sanitized before doctor ever sees it) and at iso_epoch itself
    (now also catches OSError/OverflowError, not only ValueError)."""
    store = make_store(tmp_path)
    park_beta(store)
    path = store.usage_limit_park_path("beta")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["parked_at"] = "0001-01-01"
    path.write_text(json.dumps(data), encoding="utf-8")
    check = doctor._check_usage_limit_parks(store, now_epoch=NOW)
    assert check is not None


def test_doctor_never_crashes_on_one_seats_broken_view_and_still_lists_the_rest(tmp_path, monkeypatch):
    """The path lookup, the view and every field derived from it are ONE failure-isolated
    step PER SEAT (doctor.py's per-agent loop) - one seat's failure must never blank the
    whole check (#311 blocker 2)."""
    store = make_store(tmp_path)
    park_beta(store)
    # #311 park reader recast: the shared reader resolves the current eligible UNREAD
    # message for alpha too, so its park marker/record need a real message behind them -
    # a synthetic id with no corresponding message would never be found.
    alpha_msg = store.send(sender="lead", recipient="alpha", body="for alpha")
    alpha_parked_at = park.epoch_iso(NOW - 60)
    store.write_usage_limit_park(
        "alpha", provider="claude", window="five_hour", reset_epoch=RESET, wake_epoch=WAKE,
        message_id=alpha_msg.id, parked_at=alpha_parked_at, wrapper_generation="g1", now_epoch=NOW)
    durable_park(store, "alpha", alpha_msg.id, parked_at=alpha_parked_at)
    real_view = store.usage_limit_park_view

    def flaky(agent, **kw):
        if agent == "beta":
            raise OSError("simulated per-seat failure")
        return real_view(agent, **kw)

    monkeypatch.setattr(store, "usage_limit_park_view", flaky)
    check = doctor._check_usage_limit_parks(store, now_epoch=NOW)
    assert check is not None and "alpha" in check.details and "beta" not in check.details


def test_doctor_says_when_a_seat_has_been_parked_for_a_day_and_the_age_is_configurable(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    mid = head_id(store)
    thirty_hours_ago = park.epoch_iso(NOW - 30 * 3600)
    store.write_usage_limit_park(
        "beta", provider="claude", window="seven_day", reset_epoch=None, wake_epoch=None, message_id=mid,
        parked_at=thirty_hours_ago, wrapper_generation="g1", now_epoch=NOW)
    durable_park(store, "beta", mid, window="seven_day", parked_at=thirty_hours_ago)
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
    # #311 park reader recast: the durable record still says parked (no marker was ever
    # written here, and routing a NOTICE about the park is orthogonal to the park itself) -
    # the check now keeps showing the seat's own row; only the unrouted-notice half clears.
    check = doctor._check_usage_limit_parks(store)
    assert check is not None and "never reached anyone" not in check.details
    assert check.data["unrouted_notices"] == []


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
