"""With the turn journal OFF, the loop does what it did before the journal existed.

The golden file ``tests/golden/off_loop_2112cec.json`` was made once from agenttalk
master 2112cec (the code before the journal) by ``tests/make_off_golden.py`` (see
``tests/golden_off_scenarios.py``). These tests run the same fixtures on the current
code, with no journal and no observer, and compare the durable outputs: the project
store's files (ledger, state, cursor, dead letters), the loop's result and sleeps,
the exception raised, and the lifecycle log lines, with volatile values replaced.
"""

from __future__ import annotations

import hashlib
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
    """#313 fix round 1 (tk-1bc26603ee28): a file entry names whether it is missing
    (in the golden record but never captured), unexpected (captured but not in the
    golden record) or present-with-different-contents - the three are NOT the same
    finding, and the bare "file <name>" this used to report for all three looked
    exactly like "missing" regardless of which one it actually was, which is what
    let an unrelated normalisation gap masquerade as a visibility lag during #313's
    first investigation."""
    found: list[str] = []
    for key in sorted(set(expected) | set(got)):
        if key == "files":
            exp_files, got_files = expected[key], got[key]
            for name in sorted(set(exp_files) | set(got_files)):
                if name not in got_files:
                    found.append(f"file {name} (missing)")
                elif name not in exp_files:
                    found.append(f"file {name} (unexpected)")
                elif exp_files[name] != got_files[name]:
                    found.append(f"file {name} (contents differ)")
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


_ATTEMPTS_FILE = ".agenttalk/state/dead-letter-attempts/beta.json"


@pytest.mark.parametrize("attempt_id", ["123456789012", "abcdef123456"])
def test_every_attempt_id_shape_still_matches_the_golden_record(tmp_path, monkeypatch, attempt_id):
    """#313 fix round 1 (tk-1bc26603ee28, codex-agenttalk-reviewer-1's cold read): the
    volatile attempt id (Store.record_attempt_start's attempt_id, uuid.uuid4().hex[:12])
    is masked by field name now, not by a regex that requires a letter - so an all-digit
    id (roughly 1 real run in 280) matches the golden record exactly the same as a mixed
    hex one, instead of silently failing to normalise and reporting the attempt-record
    file as different for a reason that had nothing to do with when it was written."""
    real_record = Store.record_attempt_start
    token = attempt_id  # the real caller always passes attempt_id=... by keyword - force it

    def fixed_id(self, agent, record, *, attempt_id, **kwargs):
        return real_record(self, agent, record, attempt_id=token, **kwargs)

    monkeypatch.setattr(Store, "record_attempt_start", fixed_id)
    got = scenarios.capture("e5_exception", tmp_path / attempt_id)
    assert _ATTEMPTS_FILE in got["files"]
    stored = json.loads(got["files"][_ATTEMPTS_FILE])
    item = next(iter(stored["messages"].values()))
    assert item["last_attempt_id"] == "<HEX12>"
    assert _differences(_golden()["e5_exception"], got) == []


def test_a_permanently_missing_attempt_record_is_still_detected(tmp_path, monkeypatch):
    """The #313 fix must not widen into masking a genuinely missing write - only the
    one volatile field is normalised; an attempt record that never reaches disk at all
    is still reported, by name, as missing."""
    monkeypatch.setattr(Store, "_write_attempts", lambda *a, **k: None)
    got = scenarios.capture("e5_exception", tmp_path / "missing-write")
    assert _ATTEMPTS_FILE not in got["files"]
    assert f"file {_ATTEMPTS_FILE} (missing)" in _differences(_golden()["e5_exception"], got)


def test_an_altered_retry_count_is_still_detected(tmp_path):
    """A real behavioural difference in the attempt record (not just its volatile id)
    must still fail the comparison - proves the fix narrows to the one masked field
    without widening to hide an unrelated, genuine change."""
    got = scenarios.capture("e5_exception", tmp_path / "x")
    stored = json.loads(got["files"][_ATTEMPTS_FILE])
    item = next(iter(stored["messages"].values()))
    item["attempts_started"] += 1
    got["files"][_ATTEMPTS_FILE] = json.dumps(stored, indent=2)
    assert f"file {_ATTEMPTS_FILE} (contents differ)" in _differences(_golden()["e5_exception"], got)


def test_the_comparison_fails_when_a_durable_file_changes(tmp_path, monkeypatch):
    real = Store.dead_letter

    def with_a_stray_file(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        (Path(self.state_dir) / "stray.txt").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(Store, "dead_letter", with_a_stray_file)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "x")
    assert "file .agenttalk/state/stray.txt (unexpected)" in _differences(
        _golden()["success_then_dead_letter"], got)


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
    assert any(".deadletter.json (contents differ)" in item for item in
               _differences(_golden()["success_then_dead_letter"], got))


_DEAD_LETTER = ".agenttalk/dead-letter/beta/<MSG2>.deadletter.json"


def test_a_wrong_recorded_payload_size_is_detected(tmp_path, monkeypatch):
    # The review's case: only the dead letter's recorded size changes, not its payload.
    real = Store.dead_letter
    changed = []

    def wrong_size(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        for stored in Path(self.root).rglob("*.deadletter.json"):
            data = json.loads(stored.read_text(encoding="utf-8"))
            assert data["size_bytes"] == Path(data["payload_path"]).stat().st_size
            data["size_bytes"] += 10000
            stored.write_text(json.dumps(data, indent=2), encoding="utf-8")
            changed.append(stored.name)
        return result

    monkeypatch.setattr(Store, "dead_letter", wrong_size)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "store")
    assert changed
    assert _differences(_golden()["success_then_dead_letter"], got) == [
        "file " + _DEAD_LETTER + " (contents differ)"]
    assert "<SIZE MISMATCH" in got["files"][_DEAD_LETTER]


def test_a_payload_with_the_other_platforms_line_endings_still_matches(tmp_path, monkeypatch):
    # Windows writes the payload with CRLF line endings and Linux with LF, so its size on
    # disk differs; a record that states the payload's true size must still match.
    real = Store.dead_letter
    flipped = []

    def other_line_endings(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        for stored in Path(self.root).rglob("*.deadletter.json"):
            data = json.loads(stored.read_text(encoding="utf-8"))
            payload = Path(data["payload_path"])
            body = payload.read_bytes()
            other = body.replace(b"\r\n", b"\n") if b"\r\n" in body else body.replace(b"\n", b"\r\n")
            assert other != body
            payload.write_bytes(other)
            data["size_bytes"], data["sha256"] = len(other), hashlib.sha256(other).hexdigest()
            stored.write_text(json.dumps(data, indent=2), encoding="utf-8")
            flipped.append(payload.name)
        return result

    monkeypatch.setattr(Store, "dead_letter", other_line_endings)
    got = scenarios.capture("success_then_dead_letter", tmp_path / "store")
    assert flipped
    assert _differences(_golden()["success_then_dead_letter"], got) == []


def test_only_the_dead_letter_records_own_size_is_masked(tmp_path):
    root = tmp_path / "store"
    payload = ".agenttalk/dead-letter/beta/x.json"

    def size_fields(name, recorded, actual):
        text = json.dumps({"size_bytes": recorded, "payload_path": str(root / payload), "tail": {"size_bytes": 7}})
        stored = json.loads(scenarios.normalise_file(text, root, [], name=name, sizes={payload: actual}))
        return stored["size_bytes"], stored["tail"]["size_bytes"]

    record = ".agenttalk/dead-letter/beta/x.deadletter.json"
    assert size_fields(record, 7, 7) == (0, 7)  # checked and masked; a nested key of that name is not
    assert size_fields(record, 7, 8) == ("<SIZE MISMATCH 7 != 8>", 7)
    assert size_fields(record, 7.0, 7) == (7.0, 7)  # only a whole number of bytes is a size
    assert size_fields(".agenttalk/state/x.json", 7, 7) == (7, 7)  # not a dead letter's record


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
    assert _differences(_golden()["success"], got) == ["file " + name + " (contents differ)"]
