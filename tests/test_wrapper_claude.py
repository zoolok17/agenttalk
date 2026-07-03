"""Progress-adapter wrapper Phase 2: the Claude stream-json adapter. Golden
fixtures are a redacted slice of a REAL live probe of
`claude -p ... --output-format stream-json --verbose --include-partial-messages`
(the parsed-dict stream IS the fixture). Covers the event mapping, the channel
distinction (thinking vs text), the no-stamp-on-text-delta + stamp-on-thinking
liveness ruling, the never-scan-thinking degraded guard, and run.py wiring.
"""

from __future__ import annotations

import json

from agenttalk.wrapper import claude_adapter, run
from agenttalk.wrapper.degraded import DegradedConfig, DegradedDetector
from agenttalk.wrapper.events import Event, EventType
from agenttalk.wrapper.framework import WrapperEngine

ET = EventType
LEAK_HIGH = '<invoke name="Read">\n<parameter name="file_path">x</parameter>\n</invoke>'

# ---- a redacted slice of the REAL probe (think-then-answer, adaptive thinking) ----
PROBE = [
    {"type": "system", "subtype": "init", "session_id": "s"},
    {"type": "system", "subtype": "status", "status": "requesting"},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    {"type": "stream_event", "event": {"type": "message_start", "message": {"role": "assistant"}}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "let me work it out"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
     "delta": {"type": "signature_delta", "signature": "ABC=="}}},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "let me work it out"}]}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
     "content_block": {"type": "text", "text": ""}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "17 x 23 = 391."}}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "17 x 23 = 391."}]}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}},
    {"type": "stream_event", "event": {"type": "message_stop"}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "17 x 23 = 391."},
]


def _map_all(objs):
    out = []
    for o in objs:
        out.extend(claude_adapter.map_event(o))
    return out


# --------------------------------------------------------- adapter mapping

def test_claude_adapter_probe_sequence() -> None:
    evs = _map_all(PROBE)
    assert [(e.type, e.channel) for e in evs] == [
        (ET.TURN_STARTED, None),
        (ET.MODEL_OUTPUT_DELTA, "thinking"),     # thinking_delta -> liveness, not scanned
        (ET.MODEL_OUTPUT, "thinking"),           # assembled thinking block
        (ET.MODEL_OUTPUT_DELTA, "text"),         # text_delta -> does NOT stamp
        (ET.MODEL_OUTPUT, "text"),               # assembled text -> SCAN target + clean stamp
        (ET.TURN_FINISHED, None),
    ]
    assert evs[4].text == "17 x 23 = 391."       # the assembled text we scan/stamp


def test_claude_adapter_tool_use_mapping() -> None:
    start = {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}}}
    delta = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"file":'}}}
    # the assistant snapshot's tool_use block must NOT re-emit tool_started
    asst = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file": "x"}}]}}
    user = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]}}
    evs = _map_all([start, delta, asst, user])
    assert [e.type for e in evs] == [ET.TOOL_STARTED, ET.TOOL_OUTPUT_DELTA, ET.TOOL_FINISHED]
    assert evs[0].tool == "Read"
    assert evs[2].text == "file body"


def test_claude_adapter_error_and_ratelimit() -> None:
    err = claude_adapter.map_event({"type": "result", "is_error": True, "result": "boom"})
    assert err[0].type == ET.ADAPTER_ERROR and err[0].retryable is False  # terminal
    rl = claude_adapter.map_event({"type": "rate_limit_event",
                                   "rate_limit_info": {"status": "blocked"}})
    assert rl[0].type == ET.ADAPTER_ERROR and rl[0].retryable is True     # transient
    assert claude_adapter.map_event({"type": "rate_limit_event",
                                     "rate_limit_info": {"status": "allowed"}}) == []
    assert claude_adapter.map_event({"type": "system", "subtype": "init"}) == []
    assert claude_adapter.map_event("not a dict") == []


# --------------------------------------------------------- is_progress channel rule

def test_text_delta_does_not_stamp_thinking_delta_does() -> None:
    # ruling: raw text_delta must NOT stamp liveness; thinking_delta MUST.
    assert Event(ET.MODEL_OUTPUT_DELTA, text="hi", channel="text").is_progress is False
    assert Event(ET.MODEL_OUTPUT_DELTA, text="reason", channel="thinking").is_progress is True
    # the assembled text model_output still stamps (when clean) + is the scan target
    assert Event(ET.MODEL_OUTPUT, text="done", channel="text").is_progress is True
    # reviewer-1 gate: the assembled THINKING snapshot is render-only (NOT progress)
    # so it cannot mask a degraded turn; the live thinking_delta above still stamps.
    assert Event(ET.MODEL_OUTPUT, text="reasoning", channel="thinking").is_progress is False
    # default channel (codex) unchanged
    assert Event(ET.MODEL_OUTPUT_DELTA, text="x").is_progress is True


def test_engine_claude_liveness_only_thinking_and_clean_text_stamp() -> None:
    stamps: list[str] = []
    eng = WrapperEngine(
        detector=DegradedDetector("claude", DegradedConfig(telemetry_only=False)),
        on_heartbeat=lambda e, now: stamps.append(e.channel or e.type.value),
        min_interval=0.0,
    )
    eng.process(Event(ET.MODEL_OUTPUT_DELTA, text="reasoning", channel="thinking"), 1.0)
    eng.process(Event(ET.MODEL_OUTPUT_DELTA, text="visible frag", channel="text"), 2.0)
    eng.process(Event(ET.MODEL_OUTPUT, text="clean assembled answer", channel="text"), 3.0)
    # thinking delta stamped; text delta did NOT; clean assembled text stamped.
    assert stamps == ["thinking", "text"]


def test_engine_degraded_turn_with_thinking_block_does_not_stamp() -> None:
    # reviewer-1 gate mask case: a degraded turn whose only model outputs are an
    # assembled THINKING snapshot and the degraded assembled TEXT must produce NO
    # heartbeat stamp - the thinking snapshot is render-only and the degraded text
    # is suppressed, so neither refreshes (masks) health.
    stamps: list[object] = []
    eng = WrapperEngine(
        detector=DegradedDetector("claude", DegradedConfig(telemetry_only=False)),
        on_heartbeat=lambda e, now: stamps.append((e.type.value, e.channel)),
        min_interval=0.0,
    )
    eng.process(Event(ET.MODEL_OUTPUT, text="internal reasoning", channel="thinking"), 1.0)
    eng.process(Event(ET.MODEL_OUTPUT, text=LEAK_HIGH, channel="text"), 2.0)
    assert stamps == [], f"a degraded turn with a thinking block must not stamp; got {stamps}"


# --------------------------------------------------------- never-scan-thinking guard

def test_degraded_never_scans_thinking_channel() -> None:
    d = DegradedDetector("claude", DegradedConfig(window_turns=1, telemetry_only=False))
    # leaked-looking markup INSIDE a thinking block must never be flagged/suppressed.
    r = d.feed(Event(ET.MODEL_OUTPUT, text=LEAK_HIGH, channel="thinking"), 0.0)
    assert r.signal is None and r.suppress_heartbeat is False


def test_degraded_scans_assembled_text_channel() -> None:
    d = DegradedDetector("claude", DegradedConfig(window_turns=1, telemetry_only=False))
    r = d.feed(Event(ET.MODEL_OUTPUT, text=LEAK_HIGH, channel="text"), 0.0)
    assert r.suppress_heartbeat is True   # leaked assembled text -> no stamp + degraded


# --------------------------------------------------------- run.py wiring (claude)

def test_run_wrapper_claude_escalates_on_leaked_assembled_text() -> None:
    restarts: list[object] = []
    objs = []
    for _ in range(2):  # two turns, each leaking in the assembled assistant text
        objs += [
            {"type": "stream_event", "event": {"type": "message_start", "message": {}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": LEAK_HIGH}]}},
            {"type": "stream_event", "event": {"type": "message_stop"}},
        ]
    lines = [json.dumps(o) for o in objs]
    run.run_wrapper(
        cli="claude", agent="claude-test", argv=["claude"], line_source=lines,
        render=False, clock=lambda: 0.0,
        heartbeat_fn=lambda e, now: None,
        restart_fn=lambda sig: restarts.append(sig),
    )
    assert len(restarts) == 1
    assert "degraded-output" in restarts[0].reason


def test_run_wrapper_claude_clean_run_no_escalation() -> None:
    restarts: list[object] = []
    stamps: list[object] = []
    lines = [json.dumps(o) for o in PROBE]
    rc = run.run_wrapper(
        cli="claude", agent="claude-test", argv=["claude"], line_source=lines,
        min_interval=0.0, render=False, clock=lambda: 0.0,
        heartbeat_fn=lambda e, now: stamps.append(e), restart_fn=lambda sig: restarts.append(sig),
    )
    assert rc == 0
    assert restarts == []          # clean probe -> no degraded escalation
    assert len(stamps) >= 1        # thinking/turn/clean-text progress stamped


def test_inject_claude_permission_mode():
    """Wrapped Claude write-grant fix: a wrapped Claude child must receive the
    resolved --permission-mode (its supervisor session_args is empty, so the
    {PERM_MODE} substitution never fires and it would launch read-only). No-op
    for codex, an empty mode, or when the operator already passed
    --permission-mode in the tail."""
    from agenttalk import cli
    # wrapped claude, no explicit mode in tail -> mode appended
    assert cli._inject_claude_permission_mode(
        ["claude", "-p"], "claude", "bypassPermissions"
    ) == ["claude", "-p", "--permission-mode", "bypassPermissions"]
    # operator already set --permission-mode -> unchanged (explicit tail wins)
    argv = ["claude", "--permission-mode", "acceptEdits", "-p"]
    assert cli._inject_claude_permission_mode(
        argv, "claude", "bypassPermissions") == argv
    # non-claude cli -> unchanged
    assert cli._inject_claude_permission_mode(
        ["codex", "exec"], "codex", "bypassPermissions") == ["codex", "exec"]
    # empty / None mode -> unchanged
    assert cli._inject_claude_permission_mode(["claude"], "claude", "") == ["claude"]
    assert cli._inject_claude_permission_mode(["claude"], "claude", None) == ["claude"]
    # original argv is not mutated
    orig = ["claude", "-p"]
    cli._inject_claude_permission_mode(orig, "claude", "bypassPermissions")
    assert orig == ["claude", "-p"]
