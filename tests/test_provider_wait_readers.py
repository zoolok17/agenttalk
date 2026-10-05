"""Every reader of seat health shows the truth for a message that is cooling down (``overloaded`` or ``throttled``).

The records are written by the real store functions the wrapper loop calls (``record_attempt_start`` and
``record_attempt_result``), then each reader is asked what it shows. Nothing is hand-made for the attention queue: its
items come from the real shared view (``Store.usage_limit_park_view``) through the real collector. No model, no network.

Readers covered (each also with the new keys ABSENT, an older record):
    the shared view, ``park_view`` / ``park_text``, the attention item (title, why, advice, identity), the CLI
    collector, ``status --json`` and the status flag, ``doctor`` (line, data, fix), the supervisor row, the web agent
    payload and the web attention / risk register. The two consoles are covered by the Node tests beside this file.
"""

from __future__ import annotations

import json
import time

import pytest

from agenttalk import attention as A
from agenttalk import cli, doctor, web
from agenttalk import supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper import usage_park as park

AGENT = "beta"
ROSTER = ["alpha", "beta", "lead"]


def now():
    return time.time()


def make_store(tmp_path):
    store = Store(tmp_path)
    store.init(ROSTER)
    store.set_operator_facing("lead")
    store.send(sender="alpha", recipient=AGENT, body="one")
    return store, store.messages_for(AGENT)[0].id


def _iso(at=None):
    return park.epoch_iso(at if at is not None else now())


def start(store, mid, *, probe_of=None):
    """The write-ahead of an attempt; a probe of a parked head consumes its due wake."""
    kwargs = {}
    if probe_of is not None:
        rec = store.attempt_record(AGENT, mid) or {}
        kwargs["usage_probe"] = {"generation": "g1", "consumed_wake": park.whole_seconds(rec.get("wake_epoch"))}
    store.record_attempt_start(AGENT, {"id": mid, "request_id": None}, attempt_id="c" * 12, at=_iso(), **kwargs)


def cool(store, mid, kind, *, detail=None, at=None):
    """A failed attempt (or probe) that the wrapper parks in a cool-down of ``kind``."""
    start(store, mid, probe_of=True if (store.attempt_record(AGENT, mid) or {}).get("park_state") else None)
    store.record_attempt_result(
        AGENT, mid, failure_class="known_global_infra", summary="x", at=_iso(at),
        cooldown={"generation": "g1", "kind": kind, "detail": detail, "now_epoch": at if at is not None else now()})


def usage_limit(store, mid, *, window="five_hour", reset=None):
    start(store, mid, probe_of=True if (store.attempt_record(AGENT, mid) or {}).get("park_state") else None)
    store.record_attempt_result(
        AGENT, mid, failure_class="usage_limit", summary="x", at=_iso(),
        usage_limit={"generation": "g1", "window": window, "provider": "claude",
                     "reset_epoch": reset if reset is not None else int(now()) + 3600})


def alive(store):
    store.write_heartbeat(AGENT)


def view(store):
    alive(store)
    return store.usage_limit_park_view(AGENT, now_epoch=now())


def items(store):
    alive(store)
    return A.usage_limit_park_items(cli._usage_limit_park_views(store, ROSTER))


def only(items_):
    assert len(items_) == 1, items_
    return items_[0]


def strip_new_keys(rec):
    """An older record: none of the keys the cool-down adds."""
    for key in ("park_kind", "cooldown_step", "park_detail", "park_rev", "soft_run"):
        rec.pop(key, None)
    return rec


def rewrite(store, mid, change):
    data = store.dead_letter_attempts(AGENT)
    change(data["messages"][mid])
    store._write_attempts(AGENT, data)


# --------------------------------------------------------------------------- the shared view and its words


@pytest.mark.parametrize("kind,word,sentence", [
    ("overloaded", "an overloaded AI provider", "waiting for an overloaded AI provider; tries again at "),
    ("throttled", "a possible usage limit", "waiting on a possible usage limit; tries again at ")])
def test_the_view_names_the_kind_and_the_parks_own_next_try(tmp_path, kind, word, sentence):
    store, mid = make_store(tmp_path)
    cool(store, mid, kind)
    v = view(store)
    wake = store.attempt_record(AGENT, mid)["wake_epoch"]
    assert (v["kind"], v["next_try_epoch"], v["state"], v["fresh"]) == (kind, wake, "parked", True)
    assert (v["window"], v["reset_epoch"], v["wake_epoch"], v["park_rev"]) == (None, None, None, 1)
    assert park.park_text(v) == sentence + park.format_epoch(wake)
    # never "usage limit" for an overload
    assert (kind == "overloaded") == ("usage limit" not in park.park_text(v))


def test_a_wrapper_that_stopped_answering_reads_as_not_responding_for_every_kind(tmp_path):
    for kind, expected in (("overloaded", "waiting for an overloaded AI provider, wrapper not responding"),
                           ("throttled", "waiting on a possible usage limit, wrapper not responding")):
        store, mid = make_store(tmp_path / kind)
        cool(store, mid, kind)
        v = store.usage_limit_park_view(AGENT, now_epoch=now() + 7200)               # the heartbeat is two hours old
        assert (v["state"], v["fresh"]) == ("stale", False)
        assert park.park_text(v) == expected


def test_while_a_probe_runs_the_time_is_absent_and_the_text_says_shortly(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    start(store, mid, probe_of=True)                                                  # the wake is consumed
    v = view(store)
    assert v["next_try_epoch"] is None
    assert park.park_text(v) == "waiting for an overloaded AI provider; tries again shortly"


def test_a_marker_left_by_an_earlier_usage_limit_is_never_used_for_a_cooldown(tmp_path):
    store, mid = make_store(tmp_path)
    store.write_usage_limit_park(AGENT, provider="claude", window="seven_day", reset_epoch=int(now()) + 9000,
                                 wake_epoch=int(now()) + 9030, message_id=mid, parked_at=_iso(),
                                 wrapper_generation="g1", now_epoch=now())
    cool(store, mid, "throttled")
    v = view(store)
    assert (v["kind"], v["window"], v["reset_epoch"], v["wake_epoch"]) == ("throttled", None, None, None)
    assert v["next_try_epoch"] == store.attempt_record(AGENT, mid)["wake_epoch"]
    assert "usage limit until" not in park.park_text(v)


def test_the_marker_path_for_a_usage_limit_is_unchanged_and_the_marker_file_is_untouched(tmp_path):
    store, mid = make_store(tmp_path)
    reset = int(now()) + 3600
    store.write_usage_limit_park(AGENT, provider="claude", window="five_hour", reset_epoch=reset, wake_epoch=reset + 30,
                                 message_id=mid, parked_at=_iso(), wrapper_generation="g1", now_epoch=now())
    rewrite_rec = {"park_state": "parked", "limit_window": "five_hour", "parked_at": _iso()}
    data = store.dead_letter_attempts(AGENT)
    data["messages"][mid] = dict(rewrite_rec)
    store._write_attempts(AGENT, data)
    path = store.usage_limit_park_path(AGENT)
    before = path.read_bytes()
    v = view(store)
    assert path.read_bytes() == before                                                 # byte for byte
    assert (v["kind"], v["next_try_epoch"], v["park_rev"], v["reset_epoch"], v["wake_epoch"]) == (
        "usage_limit", None, None, reset, reset + 30)
    assert park.park_text(v) == "parked on a usage limit until " + park.format_epoch(reset)


def test_an_older_record_without_any_new_key_reads_as_a_usage_limit(tmp_path):
    store, mid = make_store(tmp_path)
    usage_limit(store, mid)
    rewrite(store, mid, strip_new_keys)
    v = view(store)
    assert (v["kind"], v["next_try_epoch"], v["park_rev"]) == ("usage_limit", None, None)
    assert park.park_text(v).startswith("parked on a usage limit")


def test_a_damaged_kind_or_next_try_degrades_to_a_usage_limit_and_no_time(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    rewrite(store, mid, lambda rec: rec.update(park_kind=["overloaded"]))
    assert view(store)["kind"] == "usage_limit"
    rewrite(store, mid, lambda rec: rec.update(park_kind="overloaded", wake_epoch=10 ** 30))
    v = view(store)
    assert (v["kind"], v["next_try_epoch"]) == ("overloaded", None)


def test_park_rev_is_projected_through_both_the_marker_path_and_the_fallback_path(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "throttled")                                                       # rev 1
    usage_limit(store, mid)                                                             # a return: rev 2
    rec = store.attempt_record(AGENT, mid)
    assert rec["park_rev"] == 2
    assert view(store)["park_rev"] == 2                                                 # fallback path: no marker yet
    store.write_usage_limit_park(AGENT, provider="claude", window="five_hour", reset_epoch=rec["reset_epoch"],
                                 wake_epoch=rec["wake_epoch"], message_id=mid, parked_at=rec["parked_at"],
                                 wrapper_generation="g1", now_epoch=now())
    v = view(store)
    assert v["park_rev"] == 2 and v["reset_epoch"] == rec["reset_epoch"]                # the matching-marker path


# --------------------------------------------------------------------------- attention: words, advice, identity


def test_the_attention_item_says_what_is_true_and_never_promises_a_restart(tmp_path):
    for kind, cause in (("overloaded", "its AI provider looks overloaded"),
                        ("throttled", "looks like a usage limit but could not be confirmed")):
        store, mid = make_store(tmp_path / kind)
        cool(store, mid, kind)
        item = only(items(store))
        assert item["source"] == "usage_limit_park" and item["title"].startswith("beta: waiting ")
        assert cause in item["why_it_matters"] and "allowance" not in item["why_it_matters"]
        advice = item["recommendation"]
        assert "agenttalk ack --for beta --id " + mid in advice
        for forbidden in ("request-restart", "start it again now", "force-protected"):
            assert forbidden not in advice
        assert (kind == "overloaded") == ("usage limit" not in item["title"])


def test_the_usage_limit_item_is_byte_for_byte_what_it_was(tmp_path):
    store, mid = make_store(tmp_path)
    usage_limit(store, mid)
    item = only(items(store))
    assert item["recommendation"] == park.recovery_text("beta", mid)
    assert "request-restart --for beta" in item["recommendation"]
    assert "allowance" in item["why_it_matters"]
    v = view(store)
    merged_identity = {"agent": "beta", "window": "five_hour", "reset_epoch": v["reset_epoch"], "stale": False,
                       "message_id": mid, "parked_at": v["parked_at"], "wake_epoch": v["wake_epoch"]}
    assert A.source_hash(merged_identity) == item["source_hash"]      # no park_rev in a usage-limit-only history


def _later(item, store_hash=None):
    """A person's "Later" on ``item``, as the attention store records it."""
    until = park.epoch_iso(now() + 7200)
    return {item["item_id"]: {"disposition": {"action": A.ACTION_DEFER, "until": until,
                                                "source_snapshot": {"source_hash": item["source_hash"]}}}}


def _state_after(folded, item_now):
    return A.apply_disposition(item_now, folded, now_iso=_iso())["state"]


def test_a_later_survives_the_next_try_of_the_same_kind_but_not_a_real_transition(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    first = only(items(store))
    folded = _later(first)
    assert _state_after(folded, first) == "deferred"
    cool(store, mid, "overloaded", at=now() + 900)                  # a failed probe: the next step, a moving wake
    same = only(items(store))
    assert store.attempt_record(AGENT, mid)["cooldown_step"] == 1 and same["source_hash"] == first["source_hash"]
    assert _state_after(folded, same) == "deferred"
    cool(store, mid, "throttled", at=now() + 2000)                         # B: a change of kind
    other = only(items(store))
    assert other["source_hash"] != first["source_hash"] and _state_after(folded, other) == "active"


def test_a_then_b_then_a_is_three_identities_and_a_later_on_the_first_a_does_not_hide_the_second(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    a1 = only(items(store))
    folded = _later(a1)
    cool(store, mid, "throttled", at=now() + 1000)
    b = only(items(store))
    cool(store, mid, "overloaded", at=now() + 2000)
    a2 = only(items(store))
    assert len({a1["source_hash"], b["source_hash"], a2["source_hash"]}) == 3
    assert [store.attempt_record(AGENT, mid)["park_rev"]] == [3]
    assert _state_after(folded, a2) == "active"                              # the old Later is stale for the second A
    # parked_at is kept, so the long-park warning measures the whole wait
    assert store.attempt_record(AGENT, mid)["parked_at"] == view(store)["parked_at"]


def test_usage_limit_then_a_new_kind_then_usage_limit_each_change_is_a_new_identity(tmp_path):
    store, mid = make_store(tmp_path)
    usage_limit(store, mid)
    u1 = only(items(store))
    cool(store, mid, "overloaded")
    c = only(items(store))
    usage_limit(store, mid)
    u2 = only(items(store))
    assert len({u1["source_hash"], c["source_hash"], u2["source_hash"]}) == 3
    assert _state_after(_later(u1), u2) == "active"
    assert store.attempt_record(AGENT, mid)["park_rev"] == 2


def test_an_already_routed_or_exhausted_notice_never_silences_the_next_kind(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    store.mark_usage_notice(AGENT, mid, routed=True, next_at_epoch=None)
    for _ in range(park.NOTICE_MAX_TRIES):
        store.mark_usage_notice(AGENT, mid, routed=False, next_at_epoch=1.0)
    cool(store, mid, "throttled", at=now() + 1000)
    rec = store.attempt_record(AGENT, mid)
    assert (rec["notice_key"], rec["notice_routed"], rec["notice_tries"], rec["notice_next_at"]) == (
        "rev:2", False, 0, None)
    assert store.list_unrouted_usage_notices() == [{"agent": AGENT, "message_id": mid, "tries": 0}]


# --------------------------------------------------------------------------- the other readers


def test_status_json_and_the_status_flag(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    v = view(store)
    flag = cli._usage_limit_park_flag(v)
    assert flag == f"provider_wait_parked(kind=overloaded,retry={park.format_epoch(v['next_try_epoch'])})"
    stale = cli._usage_limit_park_flag({**v, "state": "stale"})
    assert stale == "provider_wait_parked(kind=overloaded,wrapper_not_responding)"
    soon = cli._usage_limit_park_flag({**v, "next_try_epoch": None})
    assert soon == "provider_wait_parked(kind=overloaded,retry=soon)"
    assert "usage_limit_parked" not in flag
    older = {k: v[k] for k in v if k not in ("kind", "next_try_epoch", "park_rev")}      # keys absent: an older view
    assert cli._usage_limit_park_flag(older).startswith("usage_limit_parked(")


def test_status_json_carries_the_additive_keys(tmp_path, capsys):
    store, mid = make_store(tmp_path)
    cool(store, mid, "throttled")
    alive(store)
    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = [a for a in payload["agents"] if a["name"] == AGENT][0]["usage_limit_park"]
    assert (row["kind"], row["park_rev"]) == ("throttled", 1) and isinstance(row["next_try_epoch"], int)
    assert (row["window"], row["reset_epoch"]) == (None, None)


def test_the_supervisor_row_accepts_the_new_keys_and_still_drops_an_unknown_kind(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    v = view(store)
    row = sup._usage_limit_park_row(v)
    assert (row["kind"], row["next_try_epoch"], row["window"], row["state"]) == (
        "overloaded", v["next_try_epoch"], None, "parked")
    assert set(row) == {"present", "state", "window", "reset_epoch", "wake_epoch", "kind", "next_try_epoch",
                        "supervisor_consulted"}
    assert sup._usage_limit_park_row({**v, "kind": "something_else"})["kind"] == "usage_limit"
    assert sup._usage_limit_park_row({**v, "next_try_epoch": "later"})["next_try_epoch"] is None
    assert sup._usage_limit_park_row({**v, "next_try_epoch": True})["next_try_epoch"] is None
    legacy = {k: v[k] for k in v if k not in ("kind", "next_try_epoch", "park_rev")}
    assert sup._usage_limit_park_row(legacy)["kind"] == "usage_limit"
    assert sup._usage_limit_park_row(legacy)["next_try_epoch"] is None
    assert sup._usage_limit_park_row(None) == {"present": False}


def test_the_supervisor_report_row_comes_from_the_shared_view(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "throttled")
    alive(store)
    report = sup.build_report(store, now_epoch=now())
    assert report["agents"][AGENT]["usage_limit_park"]["kind"] == "throttled"
    assessed = sup.supervisor_agent_assessment(AGENT, report["agents"][AGENT], None)
    assert assessed["usage_limit_park"]["kind"] == "throttled"


def test_doctor_line_data_and_fix_are_kind_aware_and_the_whole_wait_is_measured(tmp_path, monkeypatch):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    long_ago = _iso(now() - 30 * 3600)
    rewrite(store, mid, lambda rec: rec.update(parked_at=long_ago))                      # parked for 30 hours
    cool(store, mid, "throttled", at=now())                                              # the kind changed since
    assert store.attempt_record(AGENT, mid)["parked_at"] == long_ago
    alive(store)
    check = doctor._check_usage_limit_parks(store, now_epoch=now())
    assert check.status == "warn" and "waiting on a possible usage limit" in check.details
    assert "parked for over 24 h" in check.details
    row = check.data["parked"][0]
    assert (row["kind"], row["long_park"]) == ("throttled", True) and isinstance(row["next_try_epoch"], int)
    assert "request-restart" not in check.fix and "saved retry time" in check.fix
    assert "agenttalk ack --for <agent> --id <message id>" in check.fix


def test_doctor_for_a_usage_limit_is_byte_for_byte_what_it_was(tmp_path):
    store, mid = make_store(tmp_path)
    usage_limit(store, mid)
    alive(store)
    check = doctor._check_usage_limit_parks(store, now_epoch=now())
    assert check.fix == ("It tries again by itself at the stated reset time and each time it is started. "
                         + park.recovery_text("<agent>", "<message id>")
                         + " If a notice never routed, set a liaison "
                         "(`agenttalk roster --set-operator-facing <agent>`).")
    assert check.data["parked"][0]["kind"] == "usage_limit" and check.data["parked"][0]["next_try_epoch"] is None


def test_the_web_payload_and_the_web_attention_and_the_risk_register(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded")
    alive(store)
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    row = {a["name"]: a for a in root["agents"]}[AGENT]["usage_limit_park"]
    assert (row["kind"], row["park_rev"], row["window"]) == ("overloaded", 1, None)
    assert isinstance(row["next_try_epoch"], int)
    risks = web.build_risk_register(web.RootDescriptor(store=store, label="root"))["items"]
    mine = [r for r in risks if r["owner"] == AGENT and r["category"] == "usage_limit_park"]
    assert len(mine) == 1 and mine[0]["category_label"] == "Parked" and mine[0]["severity"] == "med"


def test_no_reader_payload_carries_provider_text(tmp_path):
    store, mid = make_store(tmp_path)
    cool(store, mid, "overloaded", detail="status_529")
    rewrite(store, mid, lambda rec: rec.update(last_failure_summary="SECRET-PROVIDER-WORDS"))
    alive(store)
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    blob = json.dumps(root["agents"]) + json.dumps(list(items(store))) + json.dumps(view(store))
    assert "SECRET-PROVIDER-WORDS" not in blob


# ---------------------------------------------------------- the new keys ABSENT: an older record or view


def test_every_reader_still_works_on_an_older_record_without_the_new_keys(tmp_path, capsys):
    store, mid = make_store(tmp_path)
    usage_limit(store, mid)
    rewrite(store, mid, strip_new_keys)
    alive(store)
    older_view = {k: v for k, v in view(store).items() if k not in ("kind", "next_try_epoch", "park_rev")}
    # the pure view/text and the attention item on a view that has none of the three keys
    assert park.park_text(older_view).startswith("parked on a usage limit")
    item = only(A.usage_limit_park_items([{"agent": AGENT, **older_view}]))
    assert item["title"].startswith("beta: parked on a usage limit")
    assert "allowance" in item["why_it_matters"]
    assert item["recommendation"] == park.recovery_text("beta", mid)
    # the CLI collector, status --json and the status flag
    assert only(items(store))["source"] == "usage_limit_park"
    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    row = [a for a in json.loads(capsys.readouterr().out)["agents"] if a["name"] == AGENT][0]["usage_limit_park"]
    assert (row["kind"], row["next_try_epoch"], row["park_rev"]) == ("usage_limit", None, None)
    assert cli._usage_limit_park_flag(older_view).startswith("usage_limit_parked(")
    # doctor, the supervisor row and the web payloads
    check = doctor._check_usage_limit_parks(store, now_epoch=now())
    assert check.data["parked"][0]["kind"] == "usage_limit"
    assert "usage limit" in check.details
    assert sup._usage_limit_park_row(older_view)["kind"] == "usage_limit"
    root = web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]
    assert {a["name"]: a for a in root["agents"]}[AGENT]["usage_limit_park"]["kind"] == "usage_limit"
    risks = web.build_risk_register(web.RootDescriptor(store=store, label="root"))["items"]
    assert [r["category_label"] for r in risks if r["category"] == "usage_limit_park"] == ["Parked"]


def test_a_marker_without_the_new_keys_still_gives_a_usage_limit_view():
    marker = {"fresh": True, "window": "five_hour", "reset_epoch": 1790000000, "wake_epoch": 1790000030,
              "message_id": "m", "parked_at": "2026-10-05T00:00:00Z", "age_seconds": 3.0}
    v = park.park_view(marker, None, heartbeat_age=1.0)
    assert (v["kind"], v["next_try_epoch"], v["park_rev"]) == ("usage_limit", None, None)
    assert park.park_text(v) == "parked on a usage limit until " + park.format_epoch(1790000000)


def test_the_view_keys_are_validated_not_trusted():
    base = {"fresh": True, "window": None, "reset_epoch": None, "wake_epoch": None, "message_id": "m",
            "parked_at": None, "age_seconds": None}
    odd = {**base, "kind": "overloaded", "next_try_epoch": 10 ** 30, "park_rev": True}
    v = park.park_view(odd, None, heartbeat_age=1.0)
    assert (v["kind"], v["next_try_epoch"], v["park_rev"]) == ("overloaded", None, None)
    v = park.park_view({**base, "kind": "bogus", "next_try_epoch": 1790000000, "park_rev": -3}, None, heartbeat_age=1.0)
    assert (v["kind"], v["next_try_epoch"], v["park_rev"]) == ("usage_limit", None, None)
