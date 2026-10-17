"""The lead routine's record and the best-effort reminders of the owed hand-offs (lead loop, slice 1).

Design: docs/DESIGN-lead-loop-default.md, sections 3, 4, 6, 7 (what slice 1 writes) and 11 (cases 12b, 12c, 12e).
"""
from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path

import pytest

from _lead_loop_barrier import barrier_code, run_released_together
from agenttalk import cli
from agenttalk import lead_loop_cadence as cad
from agenttalk import lead_relay as relay
from agenttalk import lead_routine as routine
from agenttalk.store import Store
from agenttalk.wrapper import loop, prompt, run, session

ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "reviewer", "builder"])
    s.set_role("lead", "lead")
    return s


def _owed(s: Store) -> str:
    s.send(sender="lead", recipient="reviewer", kind="review-request", body="please", meta={"request_id": "R1"})
    s.send(sender="builder", recipient="lead", body="I wait", meta={"waiting_on": "R1"})
    return s.send(sender="reviewer", recipient="lead", kind="review-result", body="GO",
                  meta={"request_id": "R1", "status": "approved"}).id


# ---------------------------------------------------------------------------------------------- the record
ALL_SECTIONS = ["threads", "dead_letters", "unrouted_escalations", "relay"]


def _judged(rec: dict, part: str) -> dict:
    return rec["parts"][part]["judgement"]


def _machine(rec: dict, part: str) -> dict:
    return rec["parts"][part]["machine"]


def test_complete_changes_only_its_own_part_and_names_who_and_in_which_mode(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.complete(s, "lead", "relay", kind="self-reported", note="forwarded R1")
    routine.complete(s, "lead", "gates")
    rec = routine.read(s, "lead")
    assert set(rec["parts"]) == {"relay", "gates"}
    entry = _judged(rec, "relay")
    assert entry["kind"] == "self-reported" and entry["note"] == "forwarded R1"
    assert entry["by"] == "lead" and entry["mode"] == "interactive"
    assert rec["generation"] == 2 and "machine" not in rec["parts"]["relay"]
    with pytest.raises(ValueError, match="unknown part"):
        routine.complete(s, "lead", "nonsense")


def test_a_managed_leads_completion_says_so(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("lead")
    assert routine.complete(s, "lead", "gates")["mode"] == "managed"


def test_a_sweep_writes_only_the_machine_record_of_the_machine_parts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.complete(s, "lead", "gates", now_epoch=1.0)
    assert routine.record_sweep(s, "lead", controller_start="2026-10-17T10:00:00Z", lease_id="L1",
                                attempted=ALL_SECTIONS, failed=[], now_epoch=100.0)
    rec = routine.read(s, "lead")
    assert _judged(rec, "gates")["at"].startswith("1970-01-01T00:00:01")             # untouched by the sweep
    assert set(rec["parts"]) == {"gates", "bus", "relay"} and not set(rec["parts"]) & set(routine.MANUAL_PARTS)
    machine = _machine(rec, "bus")
    assert machine["kind"] == "sweep" and machine["mode"] == "managed"
    assert machine["controller_start"] == "2026-10-17T10:00:00Z" and machine["lease_id"] == "L1"


def test_a_sweep_keeps_the_leads_own_completion_of_the_same_part(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.complete(s, "lead", "relay", now_epoch=10, note="decision made")
    routine.record_sweep(s, "lead", controller_start="2026-10-17T10:00:00Z", attempted=ALL_SECTIONS, failed=[],
                         now_epoch=20)
    rec = routine.read(s, "lead")
    assert _judged(rec, "relay")["kind"] == "self-reported" and _judged(rec, "relay")["note"] == "decision made"
    assert _judged(rec, "relay")["at"].endswith("00:00:10Z") and _machine(rec, "relay")["kind"] == "sweep"


def test_the_leads_completion_keeps_the_sweeps_record_of_the_same_part(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.record_sweep(s, "lead", controller_start="2026-10-17T10:00:00Z", attempted=ALL_SECTIONS, failed=[])
    before = _machine(routine.read(s, "lead"), "relay")
    routine.complete(s, "lead", "relay", now_epoch=20)
    rec = routine.read(s, "lead")
    assert _machine(rec, "relay") == before and _judged(rec, "relay")["kind"] == "self-reported"


def test_a_failed_section_is_named_and_does_not_renew_its_part(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.record_sweep(s, "lead", controller_start="c1", attempted=ALL_SECTIONS, failed=[], now_epoch=10.0)
    first_bus = _machine(routine.read(s, "lead"), "bus")["at"]
    routine.record_sweep(s, "lead", controller_start="c1", attempted=["threads", "dead_letters", "relay"],
                         failed=["unrouted_escalations"], now_epoch=500.0)
    rec = routine.read(s, "lead")
    assert _machine(rec, "bus")["at"] == first_bus                              # NOT renewed
    assert _machine(rec, "bus")["failed"] == ["unrouted_escalations"]
    assert _machine(rec, "relay")["at"] != first_bus                           # the relay section was read fine
    assert rec["sweep"]["failed"] == ["unrouted_escalations"]


def test_a_section_that_was_not_attempted_is_not_completed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.record_sweep(s, "lead", controller_start="c1", attempted=[], failed=[])
    rec = routine.read(s, "lead")
    assert all("at" not in slots["machine"] for slots in rec["parts"].values())
    assert _machine(rec, "relay")["unattempted"] == ["relay"]
    routine.record_sweep(s, "lead", controller_start="c1", attempted=["threads", "relay"], failed=[])
    rec = routine.read(s, "lead")
    assert "at" in _machine(rec, "relay") and "at" not in _machine(rec, "bus")      # relay yes, bus still missing
    assert _machine(rec, "bus")["unattempted"] == ["dead_letters", "unrouted_escalations"]


def test_an_old_controllers_write_is_refused_and_leaves_the_newer_record_unchanged(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert routine.record_sweep(s, "lead", controller_start="2026-10-17T12:00:00Z", attempted=ALL_SECTIONS, failed=[])
    before = routine.record_path(s, "lead").read_bytes()
    assert not routine.record_sweep(s, "lead", controller_start="2026-10-17T11:00:00Z", attempted=ALL_SECTIONS,
                                    failed=[])
    assert routine.record_path(s, "lead").read_bytes() == before
    assert routine.record_sweep(s, "lead", controller_start="2026-10-17T12:00:00Z", attempted=ALL_SECTIONS, failed=[])


def test_a_controller_whose_lease_has_moved_on_is_refused_inside_the_lock(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert not routine.record_sweep(s, "lead", controller_start="2026-10-17T12:00:00Z", attempted=ALL_SECTIONS,
                                    failed=[], lease_id="old", still_owner=lambda: False)
    assert not routine.record_path(s, "lead").exists()
    assert routine.record_sweep(s, "lead", controller_start="2026-10-17T12:00:00Z", attempted=ALL_SECTIONS,
                                failed=[], lease_id="live", still_owner=lambda: True)


def test_a_torn_or_missing_record_reads_as_nothing_recorded(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert routine.read(s, "lead")["parts"] == {}
    routine.record_path(s, "lead").write_text("{not json", encoding="utf-8")
    assert routine.read(s, "lead")["parts"] == {}
    routine.complete(s, "lead", "bus")                                         # and the next write repairs it
    assert "bus" in routine.read(s, "lead")["parts"]


def _complete_script(root: Path, barrier_dir: Path, part: str) -> str:
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from pathlib import Path
        from agenttalk import lead_routine as routine
        from agenttalk.store import Store
        s = Store(Path({str(root)!r}))
    """) + barrier_code(barrier_dir, part) + 'routine.complete(s, "lead", %r)\n' % part


def test_two_processes_completing_different_parts_keep_both(tmp_path: Path) -> None:
    s = _store(tmp_path)
    barrier = tmp_path.parent / (tmp_path.name + "-barrier")
    outs = run_released_together(barrier, [_complete_script(tmp_path, barrier, part)
                                           for part in ("handover", "resources")])
    assert [o[0] for o in outs] == [0, 0], outs
    rec = routine.read(s, "lead")
    assert set(rec["parts"]) == {"handover", "resources"} and rec["generation"] == 2


def test_manual_parts_are_listed_apart_and_never_in_the_parts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    routine.complete(s, "lead", "handover")
    routine.complete(s, "lead", "gates")
    field = routine.status_field(s, "lead")
    assert set(field["manual_parts"]) == {"handover"} and set(field["parts"]) == {"gates"}


def test_nothing_recorded_and_nothing_owed_shows_no_field(tmp_path: Path) -> None:
    assert routine.status_field(_store(tmp_path), "lead") is None


# ---------------------------------------------------------------------------------------- the commands
def test_lead_routine_complete_and_status_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    s = _store(tmp_path)
    monkeypatch.setenv("AGENTTALK_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    _owed(s)
    assert cli.main(["lead-routine", "complete", "--for", "lead", "--part", "handover", "--note", "written"]) == 0
    with pytest.raises(SystemExit) as exc:
        cli.main(["lead-routine", "complete", "--for", "lead", "--part", "nonsense"])
    assert exc.value.code == 2
    capsys.readouterr()
    assert cli.main(["lead-routine", "status", "--for", "lead"]) == 0
    out = capsys.readouterr().out
    assert "open hand-offs 1" in out and "manual part handover" in out and "not covered by automatic health" in out
    assert "== OWED RELAY" in out
    assert cli.main(["lead-routine", "status", "--for", "lead", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["lead_routine"]["open_relays"] == 1


def test_the_status_row_of_a_lead_gains_the_field_and_other_rows_do_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                       capsys) -> None:
    s = _store(tmp_path)
    monkeypatch.setenv("AGENTTALK_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    _owed(s)
    assert cli.main(["status", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)["agents"]}
    assert rows["lead"]["lead_routine"]["open_relays"] == 1
    assert "lead_routine" not in rows["builder"] and "lead_routine" not in rows["reviewer"]


# ---------------------------------------------------------------------- the prompt block on every turn
def _claude_ok_lines() -> list[str]:
    return [json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})]


def _drive_and_capture(s: Store, scoped: bool) -> str:
    seen: list[str] = []

    def spawn(argv, stdin):
        seen.append(" ".join(argv) + "\n" + (stdin or ""))
        return _claude_ok_lines()

    state = session.SessionState(cli="claude", claude_session_id="sess-1", turns=0, resume_available=False)
    drive = run.make_drive(s, "lead", "claude", state, ["claude"], spawn=spawn, clock=lambda: 0.0, render=False)
    loop.run_loop(s, "lead", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1, max_polls=6,
                  only_request_id="R1" if scoped else None)
    return "\n".join(seen)


def test_the_owed_relay_block_rides_on_a_global_turn_of_the_lead(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _owed(s)
    text = _drive_and_capture(s, scoped=False)
    assert "== OWED RELAY" in text and "seat builder waits for the decision on request R1" in text


def test_the_owed_relay_block_rides_on_a_scoped_turn_too_and_not_on_another_seats_turn(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _owed(s)
    assert "== OWED RELAY" in _drive_and_capture(s, scoped=True)
    s2 = Store(tmp_path / "other")
    s2.init(["lead", "reviewer", "builder"])
    s2.send(sender="reviewer", recipient="builder", body="plain message")
    seen: list[str] = []

    def spawn(argv, stdin):
        seen.append(" ".join(argv) + (stdin or ""))
        return _claude_ok_lines()

    state = session.SessionState(cli="claude", claude_session_id="sess-2", turns=0, resume_available=False)
    drive = run.make_drive(s2, "builder", "claude", state, ["claude"], spawn=spawn, clock=lambda: 0.0, render=False)
    loop.run_loop(s2, "builder", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1, max_polls=6)
    assert seen and "OWED RELAY" not in seen[0]


def test_assemble_turn_prompt_carries_the_notice_only_when_given() -> None:
    record = {"id": "20261017-000000-000000-AAAA", "from": "reviewer", "to": "lead", "kind": "message", "body": "x",
              "meta": {}, "subject": None}
    assert "== OWED RELAY" not in prompt.assemble_turn_prompt(record)
    assert "== OWED RELAY" in prompt.assemble_turn_prompt(record, relay_notice="== OWED RELAY (test) ==\n- x")


# ------------------------------------------------------------------- the sweep: reminders that do not give up
def _snapshot_at(s: Store, age_seconds: float) -> dict:
    return cad.build_cadence_snapshot(s, "lead", now_epoch=time.time() + age_seconds)


def _due(s: Store, cstate: dict, age: float) -> list:
    return cad.cadence_actionable(_snapshot_at(s, age), cstate, now_epoch=time.time(), reminder_after_seconds=1800)


def _ticked(cstate: dict, item: dict, now: float | None = None) -> dict:
    return cad.apply_tick_success(cstate, now_epoch=time.time() if now is None else now, reminded_keys=[],
                                  escalation_keys=[], relay_reminded={item["key"]: item["bucket"]})


def test_the_snapshot_names_the_sections_it_attempted_and_the_ones_that_failed(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path)
    snap = _snapshot_at(s, 0)
    assert "relay" in snap["attempted_sections"] and snap["failed_sections"] == []

    def boom(*a, **k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(s, "list_dead_letters", boom)
    monkeypatch.setattr(relay, "obligations_for", boom)
    snap = _snapshot_at(s, 0)
    assert "dead_letters" in snap["failed_sections"] and "relay" in snap["failed_sections"]
    assert snap["relay"] == [] and snap["dead_letters"] == {"count": 0, "items": []}      # still degrades to empty


def test_a_relay_item_appears_after_the_deadline_with_a_growing_bucket_and_escalates_at_the_third(
        tmp_path: Path) -> None:
    s = _store(tmp_path)
    _owed(s)
    d = relay.RELAY_DEADLINE_SECONDS
    cstate = s.read_lead_loop_cadence("lead")
    assert _due(s, cstate, d / 2) == []
    (item,) = [i for i in _due(s, cstate, d + 5) if i["type"] == "relay_overdue"]
    assert item["bucket"] == 1 and item["escalate"] is False and item["seat"] == "builder"
    # a successful reminder turn is recorded per bucket, NOT as a permanent latch
    cstate = _ticked(cstate, item)
    assert _due(s, cstate, d + 6) == []
    (again,) = _due(s, cstate, 2 * d + 5)
    assert again["bucket"] == 2 and not again["escalate"]
    (third,) = _due(s, cstate, 4 * d + 5)
    assert third["bucket"] == 3 and third["escalate"] is True


def test_a_reminder_turn_that_ignores_the_hand_off_does_not_stop_the_reminders_and_closing_does(
        tmp_path: Path) -> None:
    s = _store(tmp_path)
    result = _owed(s)
    d = relay.RELAY_DEADLINE_SECONDS
    cstate = s.read_lead_loop_cadence("lead")
    for age, bucket in ((d + 1, 1), (2 * d + 1, 2), (4 * d + 1, 3), (8 * d + 1, 4)):
        (item,) = [i for i in _due(s, cstate, age) if i["type"] == "relay_overdue"]
        assert item["bucket"] == bucket        # the turn "succeeded" and ignored it: the next bucket comes
        cstate = _ticked(cstate, item)
    relay.publish_handoff(s, "lead", "builder", result_id=result, body="merge it")
    assert not [i for i in _due(s, cstate, 20 * d) if i["type"] == "relay_overdue"]


def test_the_cadence_state_keeps_the_relay_buckets_across_a_failed_sweep_and_a_reread(tmp_path: Path) -> None:
    s = _store(tmp_path)
    cstate = _ticked(s.read_lead_loop_cadence("lead"), {"key": "relay:R1:M:builder", "bucket": 2}, now=5.0)
    s.write_lead_loop_cadence("lead", cstate)
    failed, _ = cad.apply_tick_failure(s.read_lead_loop_cadence("lead"), now_epoch=6.0, base=60.0, max_backoff=1800.0,
                                       health_threshold=5)
    assert failed["relay_reminded"] == {"relay:R1:M:builder": 2}
    assert s.read_lead_loop_cadence("lead")["relay_reminded"] == {"relay:R1:M:builder": 2}


# ------------------------------------------------ fix round 1: the controller carries its OWN lease identity (P2-4)
def test_the_real_cadence_cannot_borrow_a_replacement_controllers_identity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from agenttalk import store as store_mod
    s = Store(tmp_path)
    s.init(["lead", "reviewer", "builder"])
    s.set_managed_lead_loop("lead")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: store_mod.PROC_ALIVE)
    real_snapshot = cad.build_cadence_snapshot
    seen: dict = {}

    def replace_owner_after_snapshot(*args, **kwargs):
        snap = real_snapshot(*args, **kwargs)
        seen["old"] = dict(s.read_lead_loop_lease("lead"))
        newer = dict(seen["old"], lease_id="replacement-lease", acquired_at="2099-01-01T00:00:00Z")
        s._write_lead_loop_lease("lead", newer)             # a replacement takes over while this controller computes
        routine.record_sweep(s, "lead", controller_start=newer["acquired_at"], lease_id="replacement-lease",
                             attempted=["replacement"], failed=[], now_epoch=200)
        seen["record"] = routine.record_path(s, "lead").read_bytes()
        return snap

    def capture_loop(store, agent, drive, **kwargs):
        kwargs["cadence"]()
        return 0

    monkeypatch.setattr(cad, "build_cadence_snapshot", replace_owner_after_snapshot)
    monkeypatch.setattr(loop, "run_loop", capture_loop)
    cli._wrap_loop_mode(s, "lead", cli="codex", base_argv=["python", "-c", "pass"], sender="lead",
                        min_interval=5.0, render=False, lead_loop=True)
    assert routine.record_path(s, "lead").read_bytes() == seen["record"]       # the newer record is untouched
    assert "stale controller write refused" in capsys.readouterr().err         # and the refusal is logged


def test_the_real_cadence_records_its_own_lease_identity_when_it_still_owns_the_lease(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agenttalk import store as store_mod
    s = Store(tmp_path)
    s.init(["lead", "reviewer", "builder"])
    s.set_managed_lead_loop("lead")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: store_mod.PROC_ALIVE)
    seen: dict = {}

    def capture_loop(store, agent, drive, **kwargs):
        kwargs["cadence"]()
        seen["lease"] = dict(store.read_lead_loop_lease(agent))
        return 0

    monkeypatch.setattr(loop, "run_loop", capture_loop)
    cli._wrap_loop_mode(s, "lead", cli="codex", base_argv=["python", "-c", "pass"], sender="lead",
                        min_interval=5.0, render=False, lead_loop=True)
    machine = routine.read(s, "lead")["parts"]["relay"]["machine"]
    assert machine["lease_id"] == seen["lease"]["lease_id"]
    assert machine["controller_start"] == seen["lease"]["acquired_at"]
