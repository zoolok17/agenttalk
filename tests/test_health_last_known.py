"""Additive ``last_known_*`` health fields: nothing that already existed may change.

``health.normalize`` used to return a bare ``unknown`` for a snapshot that validated but was too old
(older than the TTL, or older than the heartbeat by more than the skew). The console v2 needs to know
what that snapshot said, so those two branches now ALSO carry ``last_known_state``, ``last_known_since``,
``last_known_updated_at`` and, when present, ``last_known_progress_at``. The contract tested here:

* every pre-existing key and value of the stale read is byte-for-byte what it was (state stays
  ``unknown``), and no other branch (missing, invalid, future-dated, fresh) gains a field;
* every existing consumer (status, supervisor report and plan, attention, lead chat, ``/api/state``,
  doctor) produces the same output as with the legacy reader, apart from the four new keys where a
  health dict is passed through verbatim.

The "legacy reader" is the exact pre-change behaviour: ``unknown(agent, warning)`` for those branches.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import doctor, web
from agenttalk import health as hm
from agenttalk import supervisor as sup
from agenttalk.store import Store

E = 1_800_000_000.0                      # the frozen "now" for every consumer below
NEW_KEYS = ("last_known_state", "last_known_since", "last_known_updated_at", "last_known_progress_at")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _raw(state: str = hm.STATE_WORKING_SILENT, *, updated: float, since: float | None = None,
         progress: float | None = None, agent: str = "beta") -> dict:
    return hm.build_snapshot(
        agent=agent, cli="claude", mode="wrapper", state=state,
        updated_at=_iso(updated), since=_iso(since if since is not None else updated),
        last_progress_at=_iso(progress) if progress is not None else None,
        reason_code="turn_spawned", source="wrapper", warnings=[],
    )


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in NEW_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _legacy(agent: str, warning: str, raw: dict, state: str) -> dict:
    return hm.unknown(agent, warning)


# ----------------------------------------------------------- the reader itself

@pytest.mark.parametrize("branch", ["ttl", "older_than_heartbeat"])
def test_stale_read_keeps_every_existing_key_and_value(branch: str) -> None:
    raw = _raw(updated=E - 900, since=E - 1200, progress=E - 1000)
    if branch == "ttl":
        got = hm.normalize(raw, agent="beta", now_epoch=E)
        warning = "health_stale_ttl"
    else:
        raw = _raw(updated=E - 100, since=E - 400, progress=E - 300)
        heartbeat = datetime.fromtimestamp(E, timezone.utc)
        got = hm.normalize(raw, agent="beta", now_epoch=E, heartbeat=heartbeat)
        warning = "health_older_than_heartbeat"
    legacy = hm.unknown("beta", warning)
    assert {k: v for k, v in got.items() if k not in NEW_KEYS} == legacy
    assert list(got)[: len(legacy)] == list(legacy), "existing keys keep their order; new ones come last"
    assert got["state"] == hm.STATE_UNKNOWN and got["stale"] is True and got["updated_at"] is None
    assert got["last_known_state"] == raw["state"]
    assert got["last_known_since"] == raw["since"]
    assert got["last_known_updated_at"] == raw["updated_at"]
    assert got["last_known_progress_at"] == raw["last_progress_at"]
    assert set(got) - set(legacy) == set(NEW_KEYS)


def test_progress_field_is_absent_when_the_snapshot_had_none() -> None:
    got = hm.normalize(_raw(updated=E - 900), agent="beta", now_epoch=E)
    assert "last_known_progress_at" not in got
    assert set(got) - set(hm.unknown("beta", "health_stale_ttl")) == {
        "last_known_state", "last_known_since", "last_known_updated_at"}


def test_no_other_branch_gains_a_field() -> None:
    fresh = hm.normalize(_raw(updated=E - 10), agent="beta", now_epoch=E)
    assert not any(k in fresh for k in NEW_KEYS)
    cases = {
        "missing": None,
        "not_a_dict": "x",
        "wrong_agent": _raw(updated=E - 10, agent="alpha"),
        "bad_state": {**_raw(updated=E - 900), "state": "bogus"},
        "bad_time": {**_raw(updated=E - 900), "updated_at": "nope"},
        "bad_progress": {**_raw(updated=E - 900), "last_progress_at": "nope"},
        "future": _raw(updated=E + 86400),
    }
    for name, raw in cases.items():
        got = hm.normalize(raw, agent="beta", now_epoch=E)
        assert not any(k in got for k in NEW_KEYS), name
        assert got["state"] == hm.STATE_UNKNOWN, name


def test_last_known_state_is_only_ever_a_validated_health_state() -> None:
    for state in hm.HEALTH_STATES:
        got = hm.normalize(_raw(state, updated=E - 900), agent="beta", now_epoch=E)
        assert got["last_known_state"] == state
    got = hm.normalize({**_raw(updated=E - 900), "state": "<img src=x onerror=alert(1)>"}, agent="beta", now_epoch=E)
    assert "last_known_state" not in got


def test_ttl_disabled_never_reads_stale() -> None:
    got = hm.normalize(_raw(updated=E - 99999), agent="beta", now_epoch=E, ttl_seconds=-1)
    assert got["state"] == hm.STATE_WORKING_SILENT and not any(k in got for k in NEW_KEYS)


# ---------------------------------------------------------------- the consumers

class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(E, tz)


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every reader below sees the same instant."""
    for module in (web, doctor):
        monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(time, "time", lambda: E)


def _wedged_store(root: Path, flavour: str) -> Store:
    s = Store(root)
    s.init(["alpha", "beta"])
    (s.state_dir / "beta.heartbeat").write_text(_iso(E - 10), encoding="utf-8")
    (s.state_dir / "alpha.heartbeat").write_text(_iso(E - 10), encoding="utf-8")
    if flavour == "ttl":
        s.write_health("beta", _raw(updated=E - 900, since=E - 900))
    else:   # health written 100 s ago, heartbeat newer by more than the 30 s skew
        (s.state_dir / "beta.heartbeat").write_text(_iso(E), encoding="utf-8")
        s.write_health("beta", _raw(updated=E - 100, since=E - 400, progress=E - 300))
    s.write_health("alpha", _raw(hm.STATE_IDLE_WAITING, updated=E - 5, agent="alpha"))
    return s


def _consumers(s: Store) -> dict:
    """Run every existing reader once, at the frozen time, and return their JSON-able outputs."""
    (desc,) = web.make_descriptors([s.root])
    cfg = {
        "agents": {"beta": {"auto_restart": True, "cli": "claude", "wrapped": True}},
        "backoff": {"base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180},
        "suspect_warn_interval_seconds": 300, "launch_grace_seconds": 120,
        "health": {"ttl_seconds": 300, "heartbeat_skew_seconds": 30},
    }
    report = sup.build_report(s, now_epoch=E, supervisor_config=cfg)
    state = {"agents": {"beta": {"readiness_seen": True, "resume_available": True, "launching": False}}}
    plan = sup.plan_actions(report, state, cfg, now_epoch=E, snapshot=[])
    return json.loads(json.dumps({
        "state": web.build_state([desc]),
        "status": web.status_payload(s),
        "attention": web.build_attention(desc),
        "lead_chat": web.build_lead_chat(desc),
        "supervisor_report": report,
        "supervisor_plan": plan,
        "doctor": doctor.run(s.root).to_dict(),
        "read_health": s.read_health("beta", now_epoch=E, heartbeat=s.read_heartbeat("beta")),
    }, default=str))


# Consumers that hand the health dict through verbatim may carry the new keys; everything else
# (derived output: attention items, lead-chat liveness, supervisor plan, doctor) must be identical.
VERBATIM = ("state", "status", "supervisor_report", "read_health")
DERIVED = ("attention", "lead_chat", "supervisor_plan", "doctor")


@pytest.mark.parametrize("flavour", ["ttl", "heartbeat"])
def test_existing_consumers_produce_the_same_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flavour: str,
) -> None:
    _freeze(monkeypatch)
    store = _wedged_store(tmp_path / "s", flavour)

    monkeypatch.setattr(hm, "_stale_unknown", _legacy)          # the pre-change reader
    before = _consumers(store)
    monkeypatch.undo()
    _freeze(monkeypatch)
    after = _consumers(store)

    # the wedged agent really is read as stale in both flavours (the scenario is not vacuous)
    assert before["read_health"]["state"] == "unknown" and after["read_health"]["state"] == "unknown"
    assert "last_known_state" in after["read_health"] and "last_known_state" not in before["read_health"]
    for name in VERBATIM:
        assert _strip(after[name]) == before[name], name
    for name in DERIVED:
        assert after[name] == before[name], name
    # and the only difference anywhere is the new keys, on the wedged agent
    assert json.dumps(_strip(after), sort_keys=True) == json.dumps(before, sort_keys=True)


def test_the_classic_console_does_not_read_the_new_fields() -> None:
    web_static = Path(web.__file__).with_name("web_static")
    assert "last_known" not in (web_static / "console.js").read_text(encoding="utf-8")


def test_the_new_fields_reach_state_json_for_the_v2_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch)
    store = _wedged_store(tmp_path / "s", "ttl")
    (desc,) = web.make_descriptors([store.root])
    beta = next(a for a in web.build_state([desc])["roots"][0]["agents"] if a["name"] == "beta")
    assert beta["health"]["state"] == "unknown"
    assert beta["health"]["last_known_state"] == "working_silent"
    assert beta["health"]["last_known_updated_at"] == _iso(E - 900)
