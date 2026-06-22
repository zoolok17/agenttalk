"""Progress-adapter wrapper (0.30.0, Phase 1): event contract, Codex adapter
mapping (golden fixtures from the empirical `codex exec --json` 0.141.0 catalog),
the engine's throttled-heartbeat / render / no-stamp-on-error behavior, the
degraded-output confirmation-window state machine, and the run.py wiring driven
by an injected line source (no real subprocess).
"""

from __future__ import annotations

import json

from agenttalk.wrapper import codex_adapter, run
from agenttalk.wrapper.degraded import DegradedConfig, DegradedDetector, classify_text
from agenttalk.wrapper.events import PROGRESS_EVENTS, Event, EventType
from agenttalk.wrapper.framework import WrapperEngine

ET = EventType

# ---- the EXACT redacted catalog samples Codex pasted on a-progress-adapter ----
CATALOG_TOOL = [
    {"type": "thread.started", "thread_id": "<uuid>"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_0", "type": "command_execution",
     "command": "<powershell python --version>", "aggregated_output": "",
     "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
     "command": "<powershell python --version>",
     "aggregated_output": "Python 3.10.11\r\n", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
     "text": "catalog complete."}},
    {"type": "turn.completed", "usage": {"input_tokens": 58017}},
]
CATALOG_NOTOOL = [
    {"type": "thread.started", "thread_id": "<uuid>"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
     "text": "alpha\nbeta\ngamma"}},
    {"type": "turn.completed", "usage": {"input_tokens": 29704}},
]
CATALOG_ERROR = [
    {"type": "thread.started", "thread_id": "<uuid>"},
    {"type": "turn.started"},
    {"type": "error", "message": "Reconnecting... 2/5 (...)"},
    {"type": "error", "message": "Reconnecting... 3/5 (...)"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "error",
     "message": "Falling back from WebSockets to HTTPS transport. ..."}},
    {"type": "error", "message": "stream disconnected before completion: ..."},
    {"type": "turn.failed", "error": {"message": "stream disconnected before completion: ..."}},
]


def _map_all(objs):
    out = []
    for o in objs:
        out.extend(codex_adapter.map_event(o))
    return out


# --------------------------------------------------------- event contract

def test_progress_events_membership() -> None:
    # the two non-progress types must NEVER stamp the heartbeat.
    assert ET.ADAPTER_ERROR not in PROGRESS_EVENTS
    assert ET.DEGRADED_OUTPUT not in PROGRESS_EVENTS
    for t in (ET.TURN_STARTED, ET.MODEL_OUTPUT, ET.MODEL_OUTPUT_DELTA, ET.TOOL_STARTED,
              ET.TOOL_OUTPUT, ET.TOOL_OUTPUT_DELTA, ET.TOOL_FINISHED, ET.TURN_FINISHED):
        assert t in PROGRESS_EVENTS
    assert Event(ET.TURN_STARTED).is_progress is True
    assert Event(ET.ADAPTER_ERROR).is_progress is False
    assert Event(ET.DEGRADED_OUTPUT).is_progress is False


# --------------------------------------------------------- codex adapter

def test_codex_adapter_tool_sample() -> None:
    evs = _map_all(CATALOG_TOOL)
    assert [e.type for e in evs] == [
        ET.TURN_STARTED, ET.TOOL_STARTED, ET.TOOL_FINISHED, ET.MODEL_OUTPUT, ET.TURN_FINISHED]
    # thread.started maps to nothing; tool carries the command + finished output.
    assert evs[1].tool == "<powershell python --version>"
    assert evs[2].text == "Python 3.10.11\r\n"
    assert evs[3].text == "catalog complete."


def test_codex_adapter_notool_sample() -> None:
    evs = _map_all(CATALOG_NOTOOL)
    assert [e.type for e in evs] == [ET.TURN_STARTED, ET.MODEL_OUTPUT, ET.TURN_FINISHED]
    assert evs[1].text == "alpha\nbeta\ngamma"


def test_codex_adapter_error_tiering() -> None:
    evs = _map_all(CATALOG_ERROR)
    assert [e.type for e in evs] == [
        ET.TURN_STARTED, ET.ADAPTER_ERROR, ET.ADAPTER_ERROR, ET.ADAPTER_ERROR,
        ET.ADAPTER_ERROR, ET.ADAPTER_ERROR]
    errs = [e for e in evs if e.type == ET.ADAPTER_ERROR]
    # the transient transport errors (top-level "error" + the item-level error)
    # are retryable; the terminal turn.failed is NOT.
    assert all(e.retryable for e in errs[:-1]), "transport errors must be retryable"
    assert errs[-1].retryable is False, "turn.failed is terminal, not retryable"
    # no error is a progress event
    assert all(not e.is_progress for e in errs)


def test_codex_adapter_ignores_unknown_and_nondict() -> None:
    assert codex_adapter.map_event({"type": "thread.started"}) == []
    assert codex_adapter.map_event({"type": "mystery.future_event"}) == []
    assert codex_adapter.map_event("not a dict") == []
    assert codex_adapter.map_event({"type": "item.completed", "item": {"type": "weird"}}) == []


# --------------------------------------------------------- framework engine

def test_engine_heartbeat_throttle() -> None:
    stamps: list[float] = []
    eng = WrapperEngine(detector=DegradedDetector("codex"),
                        on_heartbeat=lambda e, now: stamps.append(now),
                        min_interval=5.0)
    eng.process(Event(ET.TURN_STARTED), now=0.0)    # first -> stamp
    eng.process(Event(ET.TOOL_STARTED), now=2.0)    # within 5s -> skip
    eng.process(Event(ET.MODEL_OUTPUT, text="hi"), now=6.0)  # >=5s -> stamp
    assert stamps == [0.0, 6.0]


def test_engine_degraded_model_output_does_not_stamp_heartbeat() -> None:
    # reviewer-1/codex r1: a high-confidence degraded MODEL_OUTPUT must be
    # classified BEFORE the heartbeat decision and must NOT stamp - leaked
    # tool-call markup proves the agent broken; it cannot also keep it healthy.
    # A clean MODEL_OUTPUT still stamps.
    stamps: list[float] = []
    infos: list[object] = []
    eng = WrapperEngine(
        detector=DegradedDetector("claude", DegradedConfig(window_turns=2, telemetry_only=False)),
        on_heartbeat=lambda e, now: stamps.append(now),
        on_info=lambda s: infos.append(s),
        min_interval=0.0,
    )
    eng.process(Event(ET.TURN_STARTED), now=0.0)                   # progress -> stamps 0.0
    eng.process(Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), now=1.0)   # degraded -> NO stamp at 1.0
    assert 1.0 not in stamps, "leaked model output must NOT stamp the heartbeat"
    eng.process(Event(ET.TURN_FINISHED), now=2.0)                 # detector still flags it
    assert any(s.level == "informational" for s in infos), "detector still reports degraded"
    eng.process(Event(ET.MODEL_OUTPUT, text="all good now"), now=3.0)  # clean -> stamps
    assert 3.0 in stamps, "clean model output still stamps"


def test_engine_error_and_degraded_never_stamp() -> None:
    stamps: list[float] = []
    rendered: list[EventType] = []
    eng = WrapperEngine(detector=DegradedDetector("codex"),
                        on_heartbeat=lambda e, now: stamps.append(now),
                        on_render=lambda e: rendered.append(e.type),
                        min_interval=0.0)
    eng.process(Event(ET.TURN_STARTED), now=0.0)              # stamps
    eng.process(Event(ET.ADAPTER_ERROR, text="boom"), now=1.0)   # NOT progress
    eng.process(Event(ET.DEGRADED_OUTPUT, text="x"), now=2.0)    # NOT progress
    assert stamps == [0.0]                       # only the progress event stamped
    # render still sees every event, including the non-progress ones
    assert rendered == [ET.TURN_STARTED, ET.ADAPTER_ERROR, ET.DEGRADED_OUTPUT]


# --------------------------------------------------------- degraded detector

# leaked tool-call markup as assistant TEXT (the Claude high-confidence signature)
LEAK_HIGH = '<invoke name="Read">\n<parameter name="file_path">x</parameter>\n</invoke>'
LEAK_LONE = 'I will now <invoke name="Read"> the file'   # open tag only -> candidate
FENCED = "Here is the syntax:\n```\n" + LEAK_HIGH + "\n```\nthat is how it looks."


def test_classify_text_tiers() -> None:
    assert classify_text(LEAK_HIGH) == "high"
    assert classify_text("<function_calls>") == "high"
    assert classify_text(LEAK_LONE) == "candidate"
    assert classify_text("a normal sentence with no markup") is None
    assert classify_text(None) is None
    # fenced / inline code is stripped before scanning -> no false positive
    assert classify_text(FENCED) is None
    assert classify_text("use `<invoke name=...>` inline") is None


def _feed(detector, events_with_now):
    # feed() returns a FeedResult; the window tests assert on the .signal it carries.
    return [detector.feed(ev, now).signal for ev, now in events_with_now]


def test_degraded_self_heals_via_tool_started() -> None:
    # turn 1 leaks; turn 2 the agent retries with a REAL tool -> window clears.
    d = DegradedDetector("claude", DegradedConfig(window_turns=2, telemetry_only=False))
    sigs = _feed(d, [
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 1.0),            # informational (1st degraded)
        (Event(ET.TURN_STARTED), 2.0),
        (Event(ET.TOOL_STARTED, tool="Read"), 2.0),  # clean progress -> clears
        (Event(ET.TURN_FINISHED), 3.0),
    ])
    levels = [s.level for s in sigs if s]
    assert "escalate" not in levels
    assert levels.count("informational") == 1


def test_degraded_escalates_after_two_degraded_turns() -> None:
    d = DegradedDetector("claude", DegradedConfig(window_turns=2, telemetry_only=False))
    sigs = _feed(d, [
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),            # degraded turn 1 -> informational
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),            # degraded turn 2 -> ESCALATE
    ])
    out = [s for s in sigs if s]
    assert out[0].level == "informational"
    esc = [s for s in out if s.level == "escalate"]
    assert len(esc) == 1
    assert "degraded-output" in esc[0].reason and esc[0].confidence == "high"


def test_degraded_time_backstop() -> None:
    # turn-count high so only the TIME backstop (180s) can fire.
    d = DegradedDetector("claude", DegradedConfig(window_turns=99, window_seconds=180.0,
                                                  telemetry_only=False))
    sigs = _feed(d, [
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),            # window opens at t=0
        (Event(ET.TURN_STARTED), 200.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 200.0),
        (Event(ET.TURN_FINISHED), 200.0),          # 200s >= 180s -> ESCALATE
    ])
    assert [s.level for s in sigs if s][-1] == "escalate"


def test_degraded_codex_telemetry_only_never_escalates() -> None:
    d = DegradedDetector("codex", DegradedConfig(window_turns=2, telemetry_only=True))
    sigs = _feed(d, [
        (Event(ET.TURN_STARTED), 0.0), (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),
        (Event(ET.TURN_STARTED), 0.0), (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),
        (Event(ET.TURN_STARTED), 0.0), (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),
    ])
    assert all(s.level != "escalate" for s in sigs if s)


def test_degraded_candidate_does_not_escalate() -> None:
    # a lone tag is candidate-only -> informational, never fuels escalation.
    d = DegradedDetector("claude", DegradedConfig(window_turns=1, telemetry_only=False))
    sigs = _feed(d, [
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_LONE), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_LONE), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),
    ])
    out = [s for s in sigs if s]
    assert all(s.level != "escalate" for s in out)
    assert all(s.confidence == "candidate" for s in out)


def test_degraded_real_tool_this_turn_downgrades_high_to_candidate() -> None:
    # if a structured tool fired this turn, leaked-looking text is likely
    # commentary -> downgrade high to candidate (no escalation fuel).
    d = DegradedDetector("claude", DegradedConfig(window_turns=1, telemetry_only=False))
    sigs = _feed(d, [
        (Event(ET.TURN_STARTED), 0.0),
        (Event(ET.TOOL_STARTED, tool="Read"), 0.0),
        (Event(ET.MODEL_OUTPUT, text=LEAK_HIGH), 0.0),
        (Event(ET.TURN_FINISHED), 0.0),
    ])
    out = [s for s in sigs if s]
    assert all(s.level != "escalate" for s in out)


# --------------------------------------------------------- run.py wiring

def test_parse_lines_skips_blank_and_nonjson() -> None:
    lines = ["", "   ", "not json at all", json.dumps({"type": "turn.started"}), "{bad"]
    evs = list(run.parse_lines(lines, codex_adapter.map_event))
    assert [e.type for e in evs] == [ET.TURN_STARTED]


def test_run_wrapper_injected_stream_stamps_heartbeat() -> None:
    stamps: list[float] = []
    restarts: list[object] = []
    lines = [json.dumps(o) for o in CATALOG_TOOL]
    rc = run.run_wrapper(
        cli="codex", agent="worker", argv=["codex"], line_source=lines,
        min_interval=0.0, render=False, clock=lambda: 0.0,
        heartbeat_fn=lambda e, now: stamps.append(now),
        restart_fn=lambda sig: restarts.append(sig),
    )
    assert rc == 0
    assert len(stamps) >= 1            # progress events stamped the heartbeat
    assert restarts == []              # a clean run never escalates


def test_run_wrapper_degraded_codex_message_escalates_when_enabled() -> None:
    # a synthetic codex stream whose agent_message TEXT leaks markup, across two
    # turns; with escalation enabled (telemetry_only off) the wrapper requests
    # exactly one restart with the redacted reason - exercising the full pipeline
    # lines -> parse -> codex map -> engine -> detector -> escalate sink.
    restarts: list[object] = []
    objs = []
    for _ in range(2):
        objs += [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": LEAK_HIGH}},
            {"type": "turn.completed"},
        ]
    lines = [json.dumps(o) for o in objs]
    run.run_wrapper(
        cli="codex", agent="worker", argv=["codex"], line_source=lines,
        render=False, clock=lambda: 0.0,
        degraded_config=DegradedConfig(window_turns=2, telemetry_only=False),
        heartbeat_fn=lambda e, now: None,
        restart_fn=lambda sig: restarts.append(sig),
    )
    assert len(restarts) == 1
    assert "degraded-output" in restarts[0].reason


def test_run_wrapper_unknown_cli_raises() -> None:
    import pytest
    with pytest.raises(ValueError):
        run.run_wrapper(cli="claude", agent="w", argv=["claude"], line_source=[])
