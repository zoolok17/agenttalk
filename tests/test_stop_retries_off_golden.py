"""With AGENTTALK_STOP_RETRIES_AT_LIMIT=0 the wrapper loop behaves byte for byte as it
did on master fc791ece, scenario by scenario (the golden file was made from that
master's code, never from this one; see tests/golden_stop_retries_scenarios.py).

Scenarios without a usage limit in them must ALSO match with the switch unset
(on by default): the park changes nothing for a history that never met a limit.

#329 deliberately updated the golden ".agenttalk/state/beta.health.json" entry for
three scenarios (old_infra_history_exhausts, real_five_hour_retries,
real_seven_day_retries): "reason_code" changed from "retryable_transport_error" to
"usage_limit_rejected", with a new "reason_detail" naming the window. This is #329's
own intended change (the proven usage limit now survives the terminal health write,
where master's code used to drop it) - no other captured file in any scenario (attempt
records, dead letters, cursors, messages) moved by even one byte; confirmed by diffing
every file this golden covers before and after. The switch-off retry count and
dead-letter behaviour this golden exists to pin are unchanged - only the health label
master would have shown is corrected here.

Three scenarios were ADDED for the cool-down kinds (``made_up_529_retries``, ``made_up_429_retries``,
``unknown_status_retries``): made-up streams that the park rules now treat as an overload or a throttle. Their
records were captured from agenttalk 93988c06, the code before the cool-down existed (the same file run there, the
existing entries merged in untouched), and with the switch at 0 the loop must still retry them exactly as it did.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import golden_stop_retries_scenarios as scenarios

GOLDEN = json.loads((Path(__file__).parent / "golden" / "stop_retries_off_fc791ece.json").read_text(encoding="utf-8"))

# Scenarios whose stream holds a rejected usage-limit event followed by an error: only
# the switch at 0 keeps today's retry behaviour for them.
LIMIT_SCENARIOS = {
    "real_five_hour_retries", "real_seven_day_retries", "limit_then_success",
    "old_infra_history_exhausts",
    "made_up_529_retries", "made_up_429_retries", "unknown_status_retries",
}


def test_every_scenario_has_a_golden_record():
    assert sorted(GOLDEN) == sorted(scenarios.SCENARIOS)


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_switch_off_matches_master_exactly(name, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTALK_STOP_RETRIES_AT_LIMIT", "0")
    assert scenarios.capture(name, tmp_path) == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(set(scenarios.SCENARIOS) - LIMIT_SCENARIOS))
def test_switch_on_changes_nothing_without_a_limit(name, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTTALK_STOP_RETRIES_AT_LIMIT", raising=False)
    assert scenarios.capture(name, tmp_path) == GOLDEN[name]


# ----------------------------------------------------------------- the comparison is honest


def _differences(expected: dict, got: dict) -> list[str]:
    found: list[str] = []
    for key in sorted(set(expected) | set(got)):
        if key == "files":
            for name in sorted(set(expected[key]) | set(got[key])):
                if expected[key].get(name) != got[key].get(name):
                    found.append("file " + name)
        elif expected.get(key) != got.get(key):
            found.append(key)
    return found


def test_a_control_character_in_a_stored_path_fails_the_comparison(tmp_path, monkeypatch):
    from agenttalk.store import Store

    original = Store.dead_letter

    def corrupt_payload_path(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        for stored in Path(self.root).rglob("*.deadletter.json"):
            data = json.loads(stored.read_text(encoding="utf-8"))
            good = data["payload_path"]
            bad = good.replace("\\beta", "\beta").replace("/beta", "\beta")     # a backspace + "eta"
            assert good != bad
            data["payload_path"] = bad
            stored.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return result

    monkeypatch.setattr(Store, "dead_letter", corrupt_payload_path)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "store")
    assert any(item.endswith(".deadletter.json") for item in _differences(GOLDEN["success_then_dead_letter"], got))


def test_a_changed_dead_letter_file_name_fails_the_comparison(tmp_path, monkeypatch):
    from agenttalk.store import Store

    original = Store.dead_letter

    def rename(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        for stored in Path(self.root).rglob("*.deadletter.json"):
            data = json.loads(stored.read_text(encoding="utf-8"))
            data["payload_path"] = str(Path(data["payload_path"]).with_name("other.json"))
            stored.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return result

    monkeypatch.setattr(Store, "dead_letter", rename)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "store")
    assert any(item.endswith(".deadletter.json") for item in _differences(GOLDEN["success_then_dead_letter"], got))


def test_a_new_agenttalk_version_does_not_change_the_comparison(tmp_path, monkeypatch):
    from agenttalk.wrapper import health

    monkeypatch.setattr(health, "__version__", "99.98.97")
    got = scenarios.capture("success", tmp_path / "x")
    assert "99.98.97" not in json.dumps(got)
    assert _differences(GOLDEN["success"], got) == []


def test_any_other_change_to_the_health_record_does_change_the_comparison(tmp_path):
    got = scenarios.capture("success", tmp_path / "x")
    name = ".agenttalk/state/beta.health.json"
    health = json.loads(got["files"][name])
    assert health["agenttalk_version"] == "<VERSION>"
    health["reason_code"] = "something_else"
    got["files"][name] = json.dumps(health, indent=2)
    assert _differences(GOLDEN["success"], got) == ["file " + name]


@pytest.mark.parametrize("attempt_id", ["abcdef012345", "123456789012", "000000000000", "ffffffffffff"])
@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_every_valid_attempt_id_matches_the_golden(name, attempt_id, tmp_path, monkeypatch):
    # The wrapper mints a random 12-hex-digit attempt id: all digits, all letters or a mix are all
    # valid, and the comparison must not depend on which one a run happened to get.
    from agenttalk.store import Store

    original = Store.record_attempt_start

    def start(self, agent, record, **kwargs):
        kwargs["attempt_id"] = attempt_id
        return original(self, agent, record, **kwargs)

    monkeypatch.setattr(Store, "record_attempt_start", start)
    monkeypatch.setenv("AGENTTALK_STOP_RETRIES_AT_LIMIT", "0")
    assert scenarios.capture(name, tmp_path) == GOLDEN[name]
