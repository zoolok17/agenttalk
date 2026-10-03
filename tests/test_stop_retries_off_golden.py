"""With AGENTTALK_STOP_RETRIES_AT_LIMIT=0 the wrapper loop behaves byte for byte as it
did on master fc791ece, scenario by scenario (the golden file was made from that
master's code, never from this one; see tests/golden_stop_retries_scenarios.py).

Scenarios without a usage limit in them must ALSO match with the switch unset
(on by default): the park changes nothing for a history that never met a limit.
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
