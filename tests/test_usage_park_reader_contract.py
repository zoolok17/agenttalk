"""#311 park reader recast: ONE table-driven proof that every consumer of a park view -
CLI attention, status, supervisor, doctor, the web agent row and web attention (both
consoles read the same server payload, so proving the payload is enough) - shows exactly
what the ONE shared reader (``Store.usage_limit_park_view``) says, for the whole matrix the
recast was asked to cover:

  marker {matching, missing, mismatched}
  x evidence {fresh, stale}
  x message/cursor {head parked, a record behind the cursor, a second message parked after
    the first was consumed}
  x switch {ON, OFF with state retained from an earlier ON run}

Point tests for the shared reader's own field-level detail (reset/wake dropped, freshness
math) already live in test_usage_park_readers.py; this file's job is the CROSS-CONSUMER
agreement property the prior round's missing-marker test could not catch, because it
exercised only one collector (see the final review, finding 3).
"""

from __future__ import annotations

import re
import time

import pytest

from agenttalk import cli, doctor, web
from agenttalk import supervisor as sup
from agenttalk.wrapper import usage_park as park
from test_usage_park_readers import durable_park, head_id, make_store

AGENT = "beta"
ROSTER = ["alpha", "beta", "lead"]


# --------------------------------------------------------------- per-reader adapters
#
# Each returns (present, state, message_id) normalized the same way, calling the
# consumer's OWN public entry point - never the shared reader directly - so a
# regression anywhere in a consumer's own path (not only in the shared reader) is caught.

def _attention_item(items):
    for it in items:
        if {"kind": "usage_limit_park", "agent": AGENT} in it.get("source_refs", []):
            state = "stale" if "wrapper not responding" in it["title"] else "parked"
            # The message id is not a top-level field on the item (only ``ident_content``'s
            # source-hash input sees it, and that is not retained) - it is embedded in the
            # recovery recommendation text (``usage_park.recovery_text``), the same place
            # the existing per-reader tests already read it from.
            m = re.search(r"--id (\S+)", it.get("recommendation") or "")
            return True, state, m.group(1) if m else None
    return False, None, None


def _cli_attention(store):
    items = cli._collect_attention_items(store, for_agent=None, roster=ROSTER)
    return _attention_item(items)


def _status(store):
    rows = cli._usage_limit_park_views(store, [AGENT])
    if not rows:
        return False, None, None
    row = rows[0]
    return True, row.get("state"), row.get("message_id")


def _supervisor(store):
    report = sup.build_report(store, now_epoch=time.time())
    view = report["agents"][AGENT].get("usage_limit_park")
    if not view:
        return False, None, None
    return True, view.get("state"), view.get("message_id")


def _doctor(store):
    check = doctor._check_usage_limit_parks(store)
    if check is None:
        return False, None, None
    for row in check.data.get("parked", []):
        if row["agent"] == AGENT:
            # doctor's row does not carry message_id (see doctor._check_usage_limit_parks) -
            # the message-identity assertions below use the readers that do expose it.
            return True, row.get("state"), None
    return False, None, None


def _web_state(store):
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    row = next((a for a in root["agents"] if a["name"] == AGENT), None)
    view = row.get("usage_limit_park") if row else None
    if not view:
        return False, None, None
    return True, view.get("state"), view.get("message_id")


def _web_attention(store):
    # web's own payload shape differs from the CLI/attention.py item shape (``agent``
    # directly, no ``source_refs``) - see web.build_attention's own projection.
    payload = web.build_attention(web.RootDescriptor(store=store, label="root"))
    for it in payload["items"]:
        if it.get("source") == "usage_limit_park" and it.get("agent") == AGENT:
            state = "stale" if "wrapper not responding" in it["title"] else "parked"
            m = re.search(r"--id (\S+)", it.get("recommendation") or "")
            return True, state, m.group(1) if m else None
    return False, None, None


READERS = {
    "cli_attention": _cli_attention,
    "status": _status,
    "supervisor": _supervisor,
    "doctor": _doctor,
    "web_state": _web_state,
    "web_attention": _web_attention,
}


def _oracle(store):
    health = store.read_health(AGENT, heartbeat=store.read_heartbeat(AGENT))
    view = store.usage_limit_park_view(AGENT, health=health)
    if view is None:
        return False, None, None
    return True, view.get("state"), view.get("message_id")


# --------------------------------------------------------------- scenario setup

def _set_fresh_heartbeat(store):
    store.write_heartbeat(AGENT)


def _set_stale_heartbeat(store):
    old = park.epoch_iso(time.time() - park.MARKER_STALE_SECONDS - 60)
    (store.state_dir / f"{AGENT}.heartbeat").write_text(old, encoding="utf-8")


def _park_head(store, *, evidence):
    """Row A/C: the only unread message is parked."""
    mid = head_id(store)
    durable_park(store, AGENT, mid, parked_at=park.epoch_iso(time.time() - 60))
    (_set_fresh_heartbeat if evidence == "fresh" else _set_stale_heartbeat)(store)
    return mid


def _matching_marker(store, mid, *, generation="g1"):
    store.write_usage_limit_park(
        AGENT, provider="claude", window="five_hour", reset_epoch=int(time.time()) + 3600,
        wake_epoch=int(time.time()) + 3630, message_id=mid, parked_at=park.epoch_iso(time.time() - 60),
        wrapper_generation=generation, now_epoch=time.time())
    store.write_waiting(AGENT, {"agent": AGENT, "pid": 1, "mode": "wrapper-loop",
                                "wait_token": "t", "wrapper_generation": generation,
                                "since": park.epoch_iso(time.time())})


def _mismatched_marker(store, mid):
    """A marker for a DIFFERENT message than the current eligible head."""
    store.write_usage_limit_park(
        AGENT, provider="claude", window="five_hour", reset_epoch=int(time.time()) + 3600,
        wake_epoch=int(time.time()) + 3630, message_id="not-" + mid, parked_at=park.epoch_iso(time.time() - 60),
        wrapper_generation="g1", now_epoch=time.time())


def _row_marker_matching_fresh_head_switch_on(store, monkeypatch):
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    mid = _park_head(store, evidence="fresh")
    _matching_marker(store, mid)
    return True, "parked", mid


def _row_marker_matching_stale_switch_off_retained(store, monkeypatch):
    """Switch OFF now, but the park marker/record were left by an earlier ON run - a
    reader must show it the same as it would with the switch ON (the switch gates only
    writing new parks, never reading retained state)."""
    monkeypatch.setenv(park.SWITCH_ENV, "0")
    mid = _park_head(store, evidence="stale")
    _matching_marker(store, mid)
    return True, "stale", mid


def _row_marker_missing_fresh_head_switch_on(store, monkeypatch):
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    mid = _park_head(store, evidence="fresh")
    return True, "parked", mid


def _row_marker_missing_stale_switch_on(store, monkeypatch):
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    mid = _park_head(store, evidence="stale")
    return True, "stale", mid


def _row_marker_missing_no_heartbeat_at_all_switch_on(store, monkeypatch):
    """Distinct from the "stale but present" row above: a MISSING heartbeat (never
    written, not merely old) must not be treated as freshness by omission - this is the
    exact shape of the final review's P1 HOLD finding (a stopped wrapper's heartbeat file
    does not exist at all, not just age out)."""
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    mid = head_id(store)
    durable_park(store, AGENT, mid, parked_at=park.epoch_iso(time.time() - 60))
    return True, "stale", mid


def _row_marker_mismatched_fresh_switch_on(store, monkeypatch):
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    mid = _park_head(store, evidence="fresh")
    _mismatched_marker(store, mid)
    return True, "parked", mid


def _row_record_behind_cursor_switch_on(store, monkeypatch):
    """A parked record for a message the cursor has already passed must never show,
    marker or not."""
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    mid = head_id(store)
    durable_park(store, AGENT, mid, parked_at=park.epoch_iso(time.time() - 60))
    _matching_marker(store, mid)
    _set_fresh_heartbeat(store)
    store.advance_cursor(AGENT, mid)
    return False, None, None


def _row_second_message_parked_after_first_consumed_switch_on(store, monkeypatch):
    """#311 finding 2's exact shape: the first message is read and consumed; a SECOND
    message then parks with no marker at all. Every reader must show the SECOND
    message's park, never stay silent and never resurrect the first."""
    monkeypatch.setenv(park.SWITCH_ENV, "1")
    first = head_id(store)
    store.advance_cursor(AGENT, first)
    second = store.send(sender="alpha", recipient=AGENT, kind="message", body="second")
    durable_park(store, AGENT, second.id, parked_at=park.epoch_iso(time.time() - 60))
    _set_fresh_heartbeat(store)
    return True, "parked", second.id


def _row_switch_off_retained_record_behind_cursor(store, monkeypatch):
    """The combination the final review's HOLD finding turned on: switch OFF, but a
    park record a since-advanced-cursor has already passed over, retained from an
    earlier ON run. Must still read as no park - the switch changes nothing here."""
    monkeypatch.setenv(park.SWITCH_ENV, "0")
    mid = head_id(store)
    durable_park(store, AGENT, mid, parked_at=park.epoch_iso(time.time() - 60))
    _matching_marker(store, mid)
    _set_fresh_heartbeat(store)
    store.advance_cursor(AGENT, mid)
    return False, None, None


SCENARIOS = {
    "marker_matching_fresh_head_switch_on": _row_marker_matching_fresh_head_switch_on,
    "marker_matching_stale_switch_off_retained": _row_marker_matching_stale_switch_off_retained,
    "marker_missing_fresh_head_switch_on": _row_marker_missing_fresh_head_switch_on,
    "marker_missing_stale_switch_on": _row_marker_missing_stale_switch_on,
    "marker_missing_no_heartbeat_at_all_switch_on": _row_marker_missing_no_heartbeat_at_all_switch_on,
    "marker_mismatched_fresh_switch_on": _row_marker_mismatched_fresh_switch_on,
    "record_behind_cursor_switch_on": _row_record_behind_cursor_switch_on,
    "second_message_parked_after_first_consumed_switch_on": (
        _row_second_message_parked_after_first_consumed_switch_on),
    "switch_off_retained_record_behind_cursor": _row_switch_off_retained_record_behind_cursor,
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_reader_agrees_with_the_shared_reader(tmp_path, monkeypatch, scenario):
    store = make_store(tmp_path)
    expected_present, expected_state, expected_mid = SCENARIOS[scenario](store, monkeypatch)

    # The oracle (the shared reader itself) must match what the scenario set out to prove -
    # this also pins the scenario's OWN meaning independently of any one consumer's code.
    assert _oracle(store) == (expected_present, expected_state, expected_mid), (
        f"{scenario}: the shared reader itself disagrees with the scenario setup")

    for name, read in READERS.items():
        present, state, mid = read(store)
        assert present == expected_present, f"{scenario}/{name}: present {present} != {expected_present}"
        assert state == expected_state, f"{scenario}/{name}: state {state!r} != {expected_state!r}"
        if mid is not None and expected_mid is not None:
            assert mid == expected_mid, f"{scenario}/{name}: message_id {mid!r} != {expected_mid!r}"
