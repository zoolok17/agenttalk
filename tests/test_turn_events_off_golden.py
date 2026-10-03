"""With the turn journal OFF, the loop does what it did before the journal existed.

The golden file ``tests/golden/off_loop_2112cec.json`` was made once from agenttalk
master 2112cec (the code before the journal) by ``tests/make_off_golden.py`` (see
``tests/golden_off_scenarios.py``). These tests run the same fixtures on the current
code, with no journal and no observer, and compare the durable outputs: the project
store's files (ledger, state, cursor, dead letters), the loop's result and sleeps,
the exception raised, and the lifecycle log lines, with volatile values replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import golden_off_scenarios as scenarios
import pytest

from agenttalk.store import Store
from agenttalk.wrapper import run

GOLDEN = Path(__file__).resolve().parent / "golden" / "off_loop_2112cec.json"


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


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


@pytest.fixture(autouse=True)
def _no_journal(monkeypatch, tmp_path):
    from agenttalk import turn_events as te

    monkeypatch.setenv(te.ENV_TURN_EVENTS_DIR, str(tmp_path / "journal-root"))
    monkeypatch.delenv(te.ENV_TURN_EVENTS, raising=False)


def test_the_golden_file_covers_the_required_paths():
    golden = _golden()
    assert {"success", "failure_then_success", "repeated_failure", "success_then_dead_letter", "gateway_hold",
            "e5_exception"} <= set(golden)
    assert golden["e5_exception"]["raised"] == "RuntimeError: boom-e5"
    assert any("dead-letter/" in name and name.endswith(".deadletter.json")
               for name in golden["success_then_dead_letter"]["files"])


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_off_matches_what_master_did(name, tmp_path):
    got = scenarios.capture(name, tmp_path / name)
    assert _differences(_golden()[name], got) == []
    assert got == _golden()[name]


def test_the_comparison_fails_when_a_durable_file_changes(tmp_path, monkeypatch):
    real = Store.dead_letter

    def with_a_stray_file(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        (Path(self.state_dir) / "stray.txt").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(Store, "dead_letter", with_a_stray_file)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "x")
    assert "file .agenttalk/state/stray.txt" in _differences(_golden()["success_then_dead_letter"], got)


def test_the_comparison_fails_when_the_exception_changes(tmp_path, monkeypatch):
    original = run._GatewayChildCapUnavailable

    def spawns(script):
        return scenarios.__dict__["_spawns"](script)

    monkeypatch.setitem(scenarios.SCENARIOS, "e5_exception", dict(
        scenarios.SCENARIOS["e5_exception"], script=[RuntimeError("another text")]))
    got = scenarios.capture("e5_exception", tmp_path / "x")
    assert "raised" in _differences(_golden()["e5_exception"], got)
    assert original is run._GatewayChildCapUnavailable and spawns


def test_the_comparison_fails_when_the_loop_waits_differently(tmp_path, monkeypatch):
    from agenttalk.wrapper import loop

    real = loop.run_loop

    def slower(*args, **kwargs):
        kwargs["idle_interval"] = 0.7
        return real(*args, **kwargs)

    monkeypatch.setattr(loop, "run_loop", slower)
    got = scenarios.capture("failure_then_success", tmp_path / "x")
    assert "sleeps" in _differences(_golden()["failure_then_success"], got)


def test_a_path_below_the_root_reads_the_same_on_every_platform(tmp_path):
    # The golden file is made on one platform and compared on all of them: Windows joins
    # the parts of a path with a backslash, Linux and macOS with "/".
    root = tmp_path / "store"
    windows = str(root) + "\\.agenttalk\\x.json"
    posix = str(root).replace("\\", "/") + "/.agenttalk/x.json"
    for text in (windows, posix):  # plain text, for example a log line
        assert scenarios.normalise(text, root, []) == "<ROOT>/.agenttalk/x.json", text
    stored = {scenarios.normalise_file(json.dumps({"payload_path": path}), root, []) for path in (windows, posix)}
    assert stored == {json.dumps({"payload_path": "<ROOT>/.agenttalk/x.json"}, indent=2)}


def test_a_changed_file_name_below_the_root_stays_visible(tmp_path):
    root = tmp_path / "store"
    first, second = (json.dumps({"payload_path": str(root) + "/.agenttalk/dead-letter/beta/%s.json" % name})
                     for name in ("first", "second"))
    assert scenarios.normalise_file(first, root, []) != scenarios.normalise_file(second, root, [])


def test_a_control_character_in_a_stored_path_is_not_hidden(tmp_path, monkeypatch):
    # The review's case: the separator before "beta" turned into a backspace escape
    # ("\b" + "eta"). Rewriting backslashes in serialized JSON would read it as a valid
    # path; decoding first keeps the control character, so the comparison must fail.
    original = Store.dead_letter
    corrupted = []

    def corrupt_payload_path(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        for stored in Path(self.root).rglob("*.deadletter.json"):
            data = json.loads(stored.read_text(encoding="utf-8"))
            good = data["payload_path"]
            bad = good.replace("\\beta", "\beta").replace("/beta", "\beta")
            assert good != bad
            data["payload_path"] = bad
            stored.write_text(json.dumps(data, indent=2), encoding="utf-8")
            corrupted.append(stored.name)
        return result

    monkeypatch.setattr(Store, "dead_letter", corrupt_payload_path)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "store")
    assert corrupted
    assert any(item.endswith(".deadletter.json") for item in
               _differences(_golden()["success_then_dead_letter"], got))


def test_a_new_agenttalk_version_does_not_change_the_comparison(tmp_path, monkeypatch):
    from agenttalk.wrapper import health

    monkeypatch.setattr(health, "__version__", "99.98.97")
    got = scenarios.capture("success", tmp_path / "x")
    assert "99.98.97" not in json.dumps(got)
    assert _differences(_golden()["success"], got) == []


def test_any_other_change_to_the_health_record_does_change_the_comparison(tmp_path):
    got = scenarios.capture("success", tmp_path / "x")
    name = ".agenttalk/state/beta.health.json"
    health = json.loads(got["files"][name])
    assert health["agenttalk_version"] == "<VERSION>"
    health["reason_code"] = "something_else"
    got["files"][name] = json.dumps(health, indent=2)
    assert _differences(_golden()["success"], got) == ["file " + name]
