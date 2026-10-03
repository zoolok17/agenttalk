"""The park marker is a file other programs may read: it carries a version and a provider, the reader
refuses anything it does not know, and the README documents exactly the keys the writer emits.
Synthetic data; no model."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agenttalk.store import Store
from agenttalk.wrapper import usage_park as park

AGENT = "beta"
T0 = 1788900000
README = Path(__file__).resolve().parents[1] / "README.md"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path)
    s.init(["alpha", AGENT])
    return s


def publish(store, **over):
    args = {"provider": "claude", "window": "five_hour", "reset_epoch": T0 + 3600, "wake_epoch": T0 + 3630,
            "message_id": "m1", "parked_at": "2026-09-09T03:00:01Z", "wrapper_generation": "g1", "now_epoch": T0}
    args.update(over)
    store.write_usage_limit_park(AGENT, **args)


def raw(store):
    return json.loads(store.usage_limit_park_path(AGENT).read_text(encoding="utf-8"))


def rewrite(store, **changes):
    data = raw(store)
    for key, value in changes.items():
        if value is KeyError:
            data.pop(key)
        else:
            data[key] = value
    store.usage_limit_park_path(AGENT).write_text(json.dumps(data), encoding="utf-8")


def test_the_writer_emits_the_version_and_the_provider(store):
    publish(store)
    data = raw(store)
    assert data["schema_version"] == 1 and data["provider"] == "claude"
    marker = store.read_usage_limit_park(AGENT, now_epoch=T0)
    assert marker["schema_version"] == 1 and marker["provider"] == "claude"


@pytest.mark.parametrize("provider", [None, "", "codex", "Claude", "beta", ["claude"]])
def test_the_writer_refuses_an_unproven_provider_and_writes_nothing(store, provider):
    with pytest.raises(ValueError):
        publish(store, provider=provider)
    assert not store.usage_limit_park_path(AGENT).exists()


@pytest.mark.parametrize("changes", [
    {"schema_version": KeyError},          # missing
    {"schema_version": 2},                 # a version this reader does not know
    {"schema_version": 0},
    {"schema_version": True},              # a boolean is not an integer version
    {"schema_version": 1.0},
    {"schema_version": "1"},
    {"provider": KeyError},                # missing
    {"provider": "codex"},                 # not (yet) a known provider
    {"provider": "openai"},
    {"provider": None},
    {"provider": ["claude"]},
])
def test_the_reader_refuses_what_it_does_not_know(store, changes):
    publish(store)
    assert store.read_usage_limit_park(AGENT, now_epoch=T0) is not None
    rewrite(store, **changes)
    assert store.read_usage_limit_park(AGENT, now_epoch=T0) is None
    assert store.usage_limit_park_view(AGENT, now_epoch=T0) is None


def test_the_attempt_path_names_claude_for_a_proven_claude_limit():
    rec: dict = {}
    park.apply_limit_result(rec, at="2026-09-09T03:00:00Z", generation="g1", window="five_hour",
                            reset_epoch=None, provider=park.PROVIDER_CLAUDE)
    assert rec["limit_provider"] == "claude"
    unproven: dict = {}
    park.apply_limit_result(unproven, at="2026-09-09T03:00:00Z", generation="g1", window="five_hour",
                            reset_epoch=None)
    assert "limit_provider" not in unproven
    park.apply_park_close(rec, at_epoch=None)
    assert "limit_provider" not in rec


def refuse(rec, *, at, window, reset):
    park.apply_limit_result(rec, at=at, generation="g1", window=window, reset_epoch=reset,
                            provider=park.PROVIDER_CLAUDE)


def test_what_the_readme_says_about_a_later_refusal_is_what_the_code_does(store):
    rec: dict = {}
    refuse(rec, at="2026-09-09T03:00:00Z", window="five_hour", reset=T0 + 3600)
    refuse(rec, at="2026-09-09T04:00:00Z", window="seven_day", reset=None)         # unknown reset
    assert rec["limit_window"] == "seven_day" and rec["reset_epoch"] == T0 + 3600   # latest window, kept reset
    refuse(rec, at="2026-09-09T05:00:00Z", window="seven_day", reset=T0 + 1800)     # earlier reset
    assert rec["reset_epoch"] == T0 + 3600
    refuse(rec, at="2026-09-09T06:00:00Z", window="seven_day", reset=T0 + 7200)     # strictly later
    assert rec["reset_epoch"] == T0 + 7200
    assert rec["parked_at"] == "2026-09-09T03:00:00Z"                                # first refusal, never moved


def test_a_refresh_changes_the_update_time_but_not_the_park_time(store):
    publish(store, now_epoch=T0)
    first = raw(store)
    publish(store, now_epoch=T0 + 90)
    second = raw(store)
    assert second["updated_at_epoch"] == T0 + 90 and first["updated_at_epoch"] == T0
    assert second["parked_at"] == first["parked_at"]


# ----------------------------------------------------------------- the README tells the truth


def documented_fields():
    text = README.read_text(encoding="utf-8")
    start = text.index("#### Reading the park marker from another program")
    section = text[start:text.index("**Technical detail.** The park is recorded", start)]
    return section, set(re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.M))


def test_the_readme_lists_exactly_the_keys_the_writer_emits(store):
    publish(store)
    section, fields = documented_fields()
    assert fields == set(raw(store))
    assert "state/usage-limit-park/<agent>.json" in section


def test_the_readme_states_the_rules_a_reader_needs():
    section, _ = documented_fields()
    squeezed = " ".join(section.split())
    for needle in ("refuse a file whose `schema_version` it does not know", "updated_at_epoch",
                   "never from the file's modified time", "MARKER_STALE_SECONDS", "read again a little later",
                   "No message text and no text from the provider", "**strictly later** reset",
                   "**first** parked", "writing the file **in place**", "Only a fresh file means a live parked seat",
                   "not** a new observation from the provider", "can miss a short park"):
        assert needle in squeezed, needle
    assert str(int(park.MARKER_STALE_SECONDS)) in squeezed


def test_the_readme_section_is_generic_no_drive_letter_or_home_path():
    section, _ = documented_fields()
    assert not re.search(r"\b[A-Za-z]:[\\/]", section)                    # a drive-letter path
    assert not re.search(r"(?i)(/home/|/users/|\\users\\|~/)", section)   # a user-home path
