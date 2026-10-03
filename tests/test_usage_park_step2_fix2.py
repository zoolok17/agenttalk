"""Fix round 2 for step 2 of the usage-limit park: the supervisor verdict wins on every surface that
has one, console 2 keeps the whole recovery text, and stale working history outranks a stale park.

Built on the readers' fixtures (test_usage_park_readers). Synthetic data; no model.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import test_usage_park_readers as rd
from agenttalk import cli, doctor, health as hm, supervisor as sup, web
from agenttalk.wrapper import usage_park as park

NOW = rd.NOW
REPO_ROOT = Path(__file__).resolve().parents[1]
ADVERSE = {"beta": {"state": "STUCK_OR_DEAD", "action": "none"}}


def stale_work_health():
    return {"state": "unknown", "stale": True, "last_known_state": "working_turn",
            "last_known_updated_at": park.epoch_iso(NOW - 900), "last_known_since": park.epoch_iso(NOW - 1200)}


# ------------------------------------------------------- the whole ranking, every combination

HEALTH = {
    "none": None,
    "current_work": {"state": "working_turn", "stale": False},
    "stale_work": stale_work_health(),
}


@pytest.mark.parametrize("verdict,health,marker", list(itertools.product(
    [None, "STUCK_OR_DEAD"], ["none", "current_work", "stale_work"], ["none", "fresh", "stale"])))
def test_the_ranking_verdict_current_work_fresh_park_stale_work_stale_park_none(tmp_path, verdict, health, marker):
    store = rd.make_store(tmp_path)
    if marker != "none":
        rd.park_beta(store, age=0 if marker == "fresh" else park.MARKER_STALE_SECONDS + 60, health=False)
    got = store.usage_limit_park_view("beta", health=HEALTH[health], verdict_state=verdict, now_epoch=NOW)
    if verdict or health == "current_work" or marker == "none":
        expected = None
    elif marker == "fresh":
        expected = "parked"                         # a fresh park beats historical work
    else:
        expected = None if health == "stale_work" else "stale"   # stale work outranks a stale park
    assert (got or {}).get("state") == expected


# ------------------------------------------------------- finding 1: the verdict wins everywhere


def beta_row(desc):
    return next(r for r in web.build_state([desc])["roots"][0]["agents"] if r["name"] == "beta")


def park_cards(items):
    return [i for i in items if i.get("agent") == "beta" and i.get("source") == "usage_limit_park"]


def test_web_row_and_attention_card_agree_under_an_adverse_verdict(tmp_path, monkeypatch):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    desc = web.RootDescriptor(store=store, label="synthetic")
    assert park_cards(web.build_attention(desc)["items"])           # no verdict: a park card
    monkeypatch.setattr(sup, "strict_child_verdicts", lambda *a, **k: ADVERSE)
    row = beta_row(desc)
    assert row["cli_child_verdict"]["state"] == "STUCK_OR_DEAD" and "usage_limit_park" not in row
    assert park_cards(web.build_attention(desc)["items"]) == []


def test_a_healthy_verdict_keeps_the_park_on_the_row_and_the_card(tmp_path, monkeypatch):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    desc = web.RootDescriptor(store=store, label="synthetic")
    monkeypatch.setattr(sup, "strict_child_verdicts",
                        lambda *a, **k: {"beta": {"state": "HEALTHY_IDLE", "action": "none"}})
    assert beta_row(desc)["usage_limit_park"]["state"] == "parked"
    assert park_cards(web.build_attention(desc)["items"])


def test_the_final_assessment_applies_its_own_adverse_plan(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    report = sup.build_report(store, now_epoch=NOW)["agents"]["beta"]
    assert report["usage_limit_park"]["present"]                    # the raw report is a limited observation
    adverse = sup.supervisor_agent_assessment(
        "beta", report, {"state": "STUCK_OR_DEAD", "action": "none", "reason": "x"})
    assert adverse["decision"]["state"] == "STUCK_OR_DEAD" and adverse["usage_limit_park"] == {"present": False}
    healthy = sup.supervisor_agent_assessment(
        "beta", report, {"state": "HEALTHY_IDLE", "action": "none", "reason": "x"})
    assert healthy["usage_limit_park"]["present"] and healthy["usage_limit_park"]["supervisor_consulted"] is True
    no_plan = sup.supervisor_agent_assessment("beta", report, None)["usage_limit_park"]
    assert no_plan["present"] and no_plan["supervisor_consulted"] is False


def test_the_raw_report_row_says_the_supervisor_was_not_consulted(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    row = sup.build_report(store, now_epoch=NOW)["agents"]["beta"]["usage_limit_park"]
    assert row["present"] and row["supervisor_consulted"] is False


def test_the_cli_flag_follows_the_assessment(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    report = sup.build_report(store, now_epoch=NOW)["agents"]["beta"]
    row = sup.supervisor_agent_assessment(
        "beta", report, {"state": "STUCK_OR_DEAD", "action": "none", "reason": "x"})
    assert cli._usage_limit_park_flag(row["usage_limit_park"]) is None


def test_where_no_verdict_exists_the_text_is_a_labelled_limited_observation(tmp_path):
    store = rd.make_store(tmp_path)
    rd.park_beta(store)
    cli_items = cli._collect_attention_items(store, for_agent=None, roster=["alpha", "beta", "lead"])
    assert "supervisor not consulted" in next(i for i in cli_items if i.get("source") == "usage_limit_park")["title"]
    assert "supervisor not consulted" in doctor._check_usage_limit_parks(store).details
    # a surface that does hold the verdict does not carry the label
    web_card = park_cards(web.build_attention(web.RootDescriptor(store=store, label="s"))["items"])[0]
    assert "not consulted" not in web_card["title"]


# ------------------------------------------------------- console 2 on the REAL payload

NODE = shutil.which("node")


def run_console2(tmp_path, row, items):
    path = tmp_path / "real-payload.json"
    path.write_text(json.dumps({"row": row, "items": items}), encoding="utf-8")
    result = subprocess.run([NODE, str(REPO_ROOT / "tests" / "console2_real_payload.mjs"), str(path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_console2_shows_the_adverse_state_for_the_real_payload(tmp_path, monkeypatch):
    store = rd.make_store(tmp_path / "root")
    rd.park_beta(store, health=False)
    store.write_health("beta", hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state="idle_waiting", updated_at=park.epoch_iso(time.time()),
        since=park.epoch_iso(time.time() - 60), last_progress_at=park.epoch_iso(time.time()), source="wrapper"))
    desc = web.RootDescriptor(store=store, label="synthetic")
    monkeypatch.setattr(sup, "strict_child_verdicts", lambda *a, **k: ADVERSE)
    row = beta_row(desc)
    assert row["health"]["state"] == "idle_waiting" and row["cli_child_verdict"]["state"] == "STUCK_OR_DEAD"
    shown = run_console2(tmp_path, row, web.build_attention(desc)["items"])
    assert shown["state"] == "down", shown
    assert "stuck or dead" in shown["line"] and "Idle" not in shown["line"] and "Parked" not in shown["line"]
    assert not [c for c in shown["cards"] if c["kind"] == "PARKED"]


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_console2_card_keeps_the_whole_recovery_text(tmp_path):
    store = rd.make_store(tmp_path / "root")
    rd.park_beta(store)
    desc = web.RootDescriptor(store=store, label="synthetic")
    item = next(i for i in web.build_attention(desc)["items"] if i.get("source") == "usage_limit_park")
    assert len(item["recommendation"]) > 400                        # the real, unshortened text
    shown = run_console2(tmp_path, beta_row(desc), [item])
    card = next(c for c in shown["cards"] if c["kind"] == "PARKED")
    for needle in ("agenttalk ack --for beta", "no dead-letter record",
                   "refused for a managed lead-loop agent"):
        assert needle in card["evidence"], needle
    assert item["recommendation"] in card["evidence"]
