from __future__ import annotations

import json

import pytest

from agenttalk import ovh_gateway_reasoning as reasoning
from agenttalk.ovh_gateway import MODEL_ALIAS, render_litellm_config
from agenttalk.ovh_gateway_reasoning import (
    ReasoningParamError,
    ReasoningStats,
    ReasoningStripLog,
    SseReasoningStripper,
    parse_reasoning_param,
    parse_reasoning_params,
    strip_reasoning_json,
    validate_reasoning_params,
)

API_BASE = "https://oai.example.invalid/v1"


# ---------------------------------------------------------------- parameters


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("reasoning_effort=low", ("reasoning_effort", "low")),
        ("reasoning_effort = high", ("reasoning_effort", "high")),
        ("chat_template_kwargs.enable_thinking=false", ("chat_template_kwargs.enable_thinking", False)),
        ("enable_thinking=true", ("enable_thinking", True)),
        ("thinking_budget=2048", ("thinking_budget", 2048)),
        ("thinking_budget=-1", ("thinking_budget", -1)),
    ],
)
def test_parse_reasoning_param_accepts_the_documented_forms(text, expected) -> None:
    assert parse_reasoning_param(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "reasoning_effort",  # no value
        "=low",  # no name
        "Reasoning_Effort=low",  # upper case
        "reasoning-effort=low",  # dash in name
        "reasoning.effort=low",  # dot other than the nested prefix
        "chat_template_kwargs.a.b=true",  # only one nesting level
        "model=other",  # reserved
        "messages=x",
        "store=true",
        "stream=false",
        "max_tokens=1",
        "api_base=http://evil",
        "extra_body=x",
        "chat_template_kwargs=true",  # the container itself is reserved
        "reasoning_effort=low\nmodel: other",  # newline injection
        "reasoning_effort=low: other",  # YAML separator
        'reasoning_effort="low"',  # quotes
        "reasoning_effort=Low",  # upper-case value
        "reasoning_effort=low medium",
        "thinking_budget=12345678",  # more than seven digits
        "thinking_budget=1.5",
        "x" * 100 + "=low",
    ],
)
def test_parse_reasoning_param_rejects_reserved_malformed_and_injection(text) -> None:
    with pytest.raises(ReasoningParamError):
        parse_reasoning_param(text)


def test_parse_reasoning_params_rejects_duplicates_and_too_many_and_sorts() -> None:
    with pytest.raises(ReasoningParamError, match="twice"):
        parse_reasoning_params(["reasoning_effort=low", "reasoning_effort=high"])
    with pytest.raises(ReasoningParamError, match="at most eight"):
        parse_reasoning_params([f"p{i}=1" for i in range(9)])
    assert list(parse_reasoning_params(["zeta=1", "alpha=2"])) == ["alpha", "zeta"]
    assert parse_reasoning_params(None) == {}
    assert parse_reasoning_params([]) == {}


def test_validate_reasoning_params_rejects_unsafe_stored_values() -> None:
    # A tampered manifest is re-validated with the same rules as the CLI.
    for bad in (
        {"model": "x"},
        {"reasoning_effort": "Low"},
        {"reasoning_effort": ["low"]},
        {"reasoning_effort": None},
        {"reasoning_effort": 1.5},
        {"reasoning_effort": 10**9},
        {"a": "x\ny"},
        "reasoning_effort=low",
    ):
        with pytest.raises(ReasoningParamError):
            validate_reasoning_params(bad)
    assert validate_reasoning_params({"reasoning_effort": "low", "a": True}) == {
        "a": True,
        "reasoning_effort": "low",
    }


# -------------------------------------------------------------------- config

DEFAULT_CONFIG = (
    "model_list:\n"
    f"  - model_name: {MODEL_ALIAS}\n"
    "    litellm_params:\n"
    f"      model: openai/{MODEL_ALIAS}\n"
    f"      api_base: {API_BASE}\n"
    "      api_key: os.environ/OVH_KEY\n"
    "      store: false\n"
    "      extra_body:\n"
    "        store: false\n"
    "      max_retries: 0\n"
    "litellm_settings:\n"
    "  drop_params: true\n"
    "  num_retries: 0\n"
    "  telemetry: false\n"
    "  use_chat_completions_url_for_anthropic_messages: true\n"
    "router_settings:\n"
    "  num_retries: 0\n"
    "general_settings:\n"
    "  master_key: os.environ/LITELLM_MASTER_KEY\n"
)


def test_default_config_is_pinned_byte_for_byte_and_never_merges_reasoning() -> None:
    assert render_litellm_config(api_base=API_BASE) == DEFAULT_CONFIG
    assert render_litellm_config(api_base=API_BASE, reasoning_params={}) == DEFAULT_CONFIG
    assert "merge_reasoning_content_in_choices" not in DEFAULT_CONFIG


def test_reasoning_params_render_under_extra_body_sorted_quoted_and_nested() -> None:
    rendered = render_litellm_config(
        api_base=API_BASE,
        reasoning_params={
            "reasoning_effort": "low",
            "chat_template_kwargs.enable_thinking": False,
            "thinking_budget": 512,
        },
    )
    assert rendered == DEFAULT_CONFIG.replace(
        "        store: false\n",
        "        store: false\n"
        '        reasoning_effort: "low"\n'
        "        thinking_budget: 512\n"
        "        chat_template_kwargs:\n"
        "          enable_thinking: false\n",
    )
    # Never as a top-level litellm_params key: LiteLLM silently drops those
    # (drop_params: true) for a model it does not know supports them.
    top_level = rendered.split("      extra_body:\n", 1)[0]
    assert "reasoning_effort" not in top_level


def test_rendered_config_parses_as_yaml_with_the_expected_extra_body() -> None:
    yaml = pytest.importorskip("yaml")
    rendered = render_litellm_config(
        api_base=API_BASE,
        # Words that YAML would coerce if left unquoted stay strings.
        reasoning_params={"mode": "off", "other": "null", "third": "yes"},
    )
    parsed = yaml.safe_load(rendered)
    params = parsed["model_list"][0]["litellm_params"]
    assert params["extra_body"] == {"store": False, "mode": "off", "other": "null", "third": "yes"}
    assert "reasoning_effort" not in params


def test_render_rejects_invalid_reasoning_params() -> None:
    with pytest.raises(ReasoningParamError):
        render_litellm_config(api_base=API_BASE, reasoning_params={"model": "x"})
    with pytest.raises(ReasoningParamError):
        render_litellm_config(api_base=API_BASE, reasoning_params={"a": "x\nstore: true"})


# ------------------------------------------------------------ SSE stripping


def _sse(*events: dict, eol: str = "\n") -> bytes:
    out = []
    for event in events:
        out.append(f"event: {event['type']}{eol}data: {json.dumps(event)}{eol}{eol}")
    return "".join(out).encode("utf-8")


def _start(index: int, kind: str = "text", **extra) -> dict:
    block = {"type": kind, **extra}
    if kind == "text":
        block.setdefault("text", "")
    if kind == "thinking":
        block.setdefault("thinking", "")
        block.setdefault("signature", "")
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, kind: str, **fields) -> dict:
    return {"type": "content_block_delta", "index": index, "delta": {"type": kind, **fields}}


def _stop(index: int) -> dict:
    return {"type": "content_block_stop", "index": index}


MESSAGE_START = {
    "type": "message_start",
    "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [],
                "model": MODEL_ALIAS, "usage": {"input_tokens": 0, "output_tokens": 0}},
}
MESSAGE_DELTA = {
    "type": "message_delta",
    "delta": {"stop_reason": "tool_use"},
    "usage": {"input_tokens": 50, "output_tokens": 30},
}
MESSAGE_STOP = {"type": "message_stop"}

# Shape captured from the pinned LiteLLM (1.91.3) with reasoning_content
# streamed before a tool call and no reasoning merge configured.
TOOL_STREAM_EVENTS = [
    MESSAGE_START,
    _start(0), _delta(0, "text_delta", text=""), _stop(0),
    _start(1, "thinking"), _delta(1, "thinking_delta", thinking="I should read the file. "), _stop(1),
    _start(2, "tool_use", id="call_1", name="Read", input={}),
    _delta(2, "input_json_delta", partial_json='{"file_path": '),
    _delta(2, "input_json_delta", partial_json='"a.txt"}'),
    _stop(2),
    MESSAGE_DELTA, MESSAGE_STOP,
]


def _events(raw: bytes) -> list[dict]:
    out = []
    for block in raw.decode("utf-8").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:]))
    return out


def _strip(raw: bytes, *, splits: list[int] | None = None) -> tuple[bytes, ReasoningStats]:
    stripper = SseReasoningStripper()
    if splits is None:
        out = stripper.feed(raw)
    else:
        out = b""
        previous = 0
        for cut in splits + [len(raw)]:
            out += stripper.feed(raw[previous:cut])
            previous = cut
    return out + stripper.finish(), stripper.stats


def test_thinking_block_is_removed_tool_call_kept_and_indices_stay_contiguous() -> None:
    raw = _sse(*TOOL_STREAM_EVENTS)
    out, stats = _strip(raw)
    events = _events(out)
    kinds = [(e["type"], e.get("index")) for e in events]
    assert kinds == [
        ("message_start", None),
        ("content_block_start", 0), ("content_block_delta", 0), ("content_block_stop", 0),
        ("content_block_start", 1), ("content_block_delta", 1), ("content_block_delta", 1),
        ("content_block_stop", 1),
        ("message_delta", None), ("message_stop", None),
    ]
    tool_start = events[4]["content_block"]
    assert tool_start["type"] == "tool_use" and tool_start["name"] == "Read"
    assert "".join(e["delta"]["partial_json"] for e in events[5:7]) == '{"file_path": "a.txt"}'
    assert b"thinking" not in out
    assert b"I should read" not in out
    assert stats.blocks == 1 and stats.chars == len("I should read the file. ")


def test_usage_and_stop_events_pass_through_byte_for_byte() -> None:
    raw = _sse(*TOOL_STREAM_EVENTS)
    out, _ = _strip(raw)
    for event in (MESSAGE_START, MESSAGE_DELTA, MESSAGE_STOP):
        block = _sse(event)
        assert block in out
    assert _events(out)[-2]["usage"] == {"input_tokens": 50, "output_tokens": 30}


def test_interleaved_reasoning_between_text_blocks_is_removed_and_reindexed() -> None:
    raw = _sse(
        MESSAGE_START,
        _start(0), _delta(0, "text_delta", text="Part A. "), _stop(0),
        _start(1, "thinking"), _delta(1, "thinking_delta", thinking="hmm"), _stop(1),
        _start(2), _delta(2, "text_delta", text="Part B."), _stop(2),
        _start(3, "thinking"), _delta(3, "thinking_delta", thinking="more"), _stop(3),
        _start(4), _delta(4, "text_delta", text="Part C."), _stop(4),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "end_turn"}}, MESSAGE_STOP,
    )
    out, stats = _strip(raw)
    events = [e for e in _events(out) if "index" in e]
    assert sorted({e["index"] for e in events}) == [0, 1, 2]
    text = "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta")
    assert text == "Part A. Part B.Part C."
    assert stats.blocks == 2 and stats.chars == len("hmm") + len("more")


def test_thinking_delta_landing_on_a_text_block_is_dropped_without_dropping_the_text() -> None:
    # The historical failure: a thinking delta arriving while a text block is open.
    raw = _sse(
        MESSAGE_START,
        _start(0),
        _delta(0, "text_delta", text="visible "),
        _delta(0, "thinking_delta", thinking="stray reasoning"),
        _delta(0, "signature_delta", signature="abc"),
        _delta(0, "text_delta", text="answer"),
        _stop(0),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "end_turn"}}, MESSAGE_STOP,
    )
    out, stats = _strip(raw)
    deltas = [e["delta"] for e in _events(out) if e["type"] == "content_block_delta"]
    assert [d["type"] for d in deltas] == ["text_delta", "text_delta"]
    assert "".join(d["text"] for d in deltas) == "visible answer"
    assert stats.chars == len("stray reasoning")


def test_redacted_thinking_block_is_dropped() -> None:
    raw = _sse(
        MESSAGE_START,
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "redacted_thinking", "data": "xyz"}},
        _stop(0),
        _start(1), _delta(1, "text_delta", text="ok"), _stop(1),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "end_turn"}}, MESSAGE_STOP,
    )
    out, stats = _strip(raw)
    assert b"redacted_thinking" not in out and b"xyz" not in out
    assert [e["index"] for e in _events(out) if "index" in e] == [0, 0, 0]
    assert stats.blocks == 1


def test_stream_without_reasoning_is_forwarded_unchanged() -> None:
    raw = _sse(
        MESSAGE_START,
        _start(0), _delta(0, "text_delta", text="plain"), _stop(0),
        _start(1, "tool_use", id="c", name="Read", input={}),
        _delta(1, "input_json_delta", partial_json="{}"), _stop(1),
        MESSAGE_DELTA, MESSAGE_STOP,
    )
    out, stats = _strip(raw)
    assert out == raw
    assert not stats


def test_output_does_not_depend_on_how_the_bytes_are_chunked() -> None:
    raw = _sse(*TOOL_STREAM_EVENTS)
    whole, whole_stats = _strip(raw)
    for cut in range(1, len(raw), 37):
        assert _strip(raw, splits=[cut])[0] == whole
    assert _strip(raw, splits=list(range(1, len(raw))))[0] == whole  # one byte at a time
    for stride in (3, 11, 29, 53, 97, 211):
        cuts = list(range(stride, len(raw), stride))
        out, stats = _strip(raw, splits=cuts)
        assert out == whole
        assert stats == whole_stats


def test_crlf_framing_is_handled() -> None:
    raw = _sse(*TOOL_STREAM_EVENTS, eol="\r\n")
    out, stats = _strip(raw)
    assert b"thinking" not in out
    assert out.count(b"\r\n\r\n") == 10
    assert stats.blocks == 1


def test_non_json_comment_and_unknown_events_pass_through_untouched() -> None:
    raw = b": keep-alive\n\nevent: ping\ndata: {\"type\": \"ping\"}\n\ndata: not json\n\ndata: [1, 2]\n\n"
    out, stats = _strip(raw)
    assert out == raw
    assert not stats


def test_oversized_event_stops_filtering_and_forwards_raw(monkeypatch) -> None:
    monkeypatch.setattr(reasoning, "MAX_EVENT_BYTES", 64)
    stripper = SseReasoningStripper()
    huge = b"data: " + b"x" * 200  # no terminator yet
    out = stripper.feed(huge)
    assert out == huge  # forwarded, never held or dropped
    later = _sse(_start(0, "thinking"))
    assert stripper.feed(later) == later  # raw from here on: fail open
    assert stripper.finish() == b""


def test_unterminated_tail_is_flushed_on_finish() -> None:
    stripper = SseReasoningStripper()
    assert stripper.feed(b"data: {\"type\": \"message_st") == b""
    assert stripper.finish() == b"data: {\"type\": \"message_st"


# ------------------------------------------------------------ JSON stripping


def test_json_message_drops_thinking_blocks_only() -> None:
    body = json.dumps({
        "type": "message", "role": "assistant", "model": MODEL_ALIAS,
        "content": [
            {"type": "thinking", "thinking": "private", "signature": ""},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "c", "name": "Read", "input": {}},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }).encode()
    out, stats = strip_reasoning_json(body)
    parsed = json.loads(out)
    assert [b["type"] for b in parsed["content"]] == ["text", "tool_use"]
    assert parsed["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert stats.blocks == 1 and stats.chars == len("private")


def test_json_without_reasoning_or_unparseable_is_returned_untouched() -> None:
    plain = b'{"type": "message", "content": [{"type": "text", "text": "x"}]}'
    assert strip_reasoning_json(plain) == (plain, ReasoningStats())
    assert strip_reasoning_json(b"not json")[0] == b"not json"
    assert strip_reasoning_json(b'{"content": "str"}')[0] == b'{"content": "str"}'


# ------------------------------------------------------------------ strip log


def test_strip_log_appends_content_free_lines_and_rotates(tmp_path, monkeypatch) -> None:
    path = tmp_path / "gateway" / "reasoning-stripped.jsonl"
    log = ReasoningStripLog(path)
    log.record("a" * 32, ReasoningStats(blocks=2, chars=900))
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["attempt_id"] == "a" * 32 and row["blocks"] == 2 and row["chars"] == 900
    assert set(row) == {
        "ts", "attempt_id", "blocks", "chars", "empty", "stop_reason", "misplaced", "failed",
    }
    assert row["empty"] is False and row["failed"] is False
    monkeypatch.setattr(ReasoningStripLog, "MAX_BYTES", 10)
    log.record("b" * 32, ReasoningStats(blocks=1, chars=5))
    assert path.with_name(path.name + ".1").is_file()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_strip_log_never_raises_when_the_path_is_unusable(tmp_path) -> None:
    unusable = tmp_path / "is-a-directory"
    unusable.mkdir()
    ReasoningStripLog(unusable).record("c" * 32, ReasoningStats(blocks=1, chars=1))


# ------------------------------------------------------------ keep-alive pings


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pings(raw: bytes) -> int:
    return sum(1 for e in _events(raw) if e["type"] == "ping")


def test_long_silent_reasoning_gets_periodic_pings_instead_of_dead_air() -> None:
    clock = _Clock()
    stripper = SseReasoningStripper(ping_interval=15.0, clock=clock)
    out = stripper.feed(_sse(MESSAGE_START))
    assert _pings(out) == 0
    out = stripper.feed(_sse(_start(0, "thinking")))
    assert out == b""  # dropped, and not yet 15 s of silence
    forwarded = []
    for step in range(1, 9):  # eight thinking deltas, 10 s apart
        clock.now += 10.0
        forwarded.append(stripper.feed(_sse(_delta(0, "thinking_delta", thinking=f"t{step}"))))
    pinged = [i for i, chunk in enumerate(forwarded) if chunk]
    assert pinged == [1, 3, 5, 7]  # a ping whenever >= 15 s passed since the last output
    for chunk in forwarded:
        if chunk:
            assert chunk == b'event: ping\ndata: {"type": "ping"}\n\n'
    assert stripper.stats.chars == sum(len(f"t{i}") for i in range(1, 9))


def test_no_pings_while_real_events_are_flowing() -> None:
    clock = _Clock()
    stripper = SseReasoningStripper(ping_interval=15.0, clock=clock)
    total = b""
    for _ in range(6):
        clock.now += 10.0
        total += stripper.feed(_sse(_delta(0, "text_delta", text="x")))
        clock.now += 10.0
        total += stripper.feed(_sse(_delta(1, "thinking_delta", thinking="y")))
    assert _pings(total) == 0  # every 20 s window still had forwarded output


def test_ping_uses_the_streams_line_endings_and_never_carries_usage() -> None:
    clock = _Clock()
    stripper = SseReasoningStripper(ping_interval=5.0, clock=clock)
    stripper.feed(_sse(_start(0, "thinking"), eol="\r\n"))
    clock.now += 6.0
    out = stripper.feed(_sse(_delta(0, "thinking_delta", thinking="z"), eol="\r\n"))
    assert out == b'event: ping\r\ndata: {"type": "ping"}\r\n\r\n'
    tail = stripper.feed(_sse(_stop(0), MESSAGE_DELTA, MESSAGE_STOP, eol="\r\n"))
    assert _sse(MESSAGE_DELTA, eol="\r\n") in tail  # usage passes untouched


# ------------------------------------------- fail-open, surrogates, diagnostics


def _thinking_then_text(text: str) -> bytes:
    return _sse(
        MESSAGE_START,
        _start(0), _delta(0, "text_delta", text="a"), _stop(0),
        _start(1, "thinking"), _delta(1, "thinking_delta", thinking="hmm"), _stop(1),
        _start(2), _delta(2, "text_delta", text=text), _stop(2),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "end_turn"}}, MESSAGE_STOP,
    )


def test_a_lone_surrogate_after_a_thinking_block_is_forwarded_without_raising() -> None:
    # A lone UTF-16 surrogate decodes to a str that ensure_ascii=False could not
    # encode as UTF-8; the rewrite must stay byte-safe for any string.
    stripper = SseReasoningStripper()
    out = stripper.feed(_thinking_then_text("ok \ud83d")) + stripper.finish()
    assert not stripper.stats.failed
    text = [e["delta"]["text"] for e in _events(out) if e["type"] == "content_block_delta"]
    assert text == ["a", "ok \ud83d"]
    assert [e["index"] for e in _events(out) if e["type"] == "content_block_start"] == [0, 1]


def test_non_ascii_text_survives_renumbering_exactly() -> None:
    stripper = SseReasoningStripper()
    out = stripper.feed(_thinking_then_text("héllo ✓ \U0001f600")) + stripper.finish()
    text = [e["delta"]["text"] for e in _events(out) if e["type"] == "content_block_delta"]
    assert text[-1] == "héllo ✓ \U0001f600"
    out.decode("ascii")  # renumbered events are plain ASCII escapes


def test_json_reply_with_a_lone_surrogate_is_stripped_without_raising() -> None:
    body = json.dumps({
        "type": "message", "role": "assistant", "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "private", "signature": ""},
            {"type": "text", "text": "ok \ud83d"},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }).encode()
    out, stats = strip_reasoning_json(body)
    assert not stats.failed and stats.blocks == 1
    assert json.loads(out)["content"] == [{"type": "text", "text": "ok \ud83d"}]


def test_an_internal_stripper_error_switches_to_raw_passthrough_and_is_recorded(
    monkeypatch,
) -> None:
    raw = _sse(*TOOL_STREAM_EVENTS)
    stripper = SseReasoningStripper()

    def boom(self, block, terminator):
        raise RuntimeError("bug")

    monkeypatch.setattr(SseReasoningStripper, "_process", boom)
    first, rest = raw[:100], raw[100:]
    out = stripper.feed(first)
    out += stripper.feed(rest) + stripper.finish()
    assert out == raw  # nothing lost, nothing held: the whole reply is forwarded raw
    assert stripper.stats.failed and bool(stripper.stats)


def test_json_strip_error_returns_the_original_body_and_records_it(monkeypatch) -> None:
    body = json.dumps({
        "type": "message", "content": [{"type": "thinking", "thinking": "x"}],
    }).encode()

    def boom(*_args, **_kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(reasoning.json, "dumps", boom)
    out, stats = strip_reasoning_json(body)
    assert out == body and stats.failed


def test_a_reply_that_is_only_reasoning_is_flagged_empty_with_its_stop_reason() -> None:
    raw = _sse(
        MESSAGE_START,
        _start(0, "thinking"), _delta(0, "thinking_delta", thinking="endless"), _stop(0),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "max_tokens"}}, MESSAGE_STOP,
    )
    stripper = SseReasoningStripper()
    stripper.feed(raw)
    stripper.finish()
    assert stripper.stats.empty is True and stripper.stats.stop_reason == "max_tokens"

    with_text = SseReasoningStripper()
    with_text.feed(_thinking_then_text("real answer"))
    with_text.finish()
    assert with_text.stats.empty is False and with_text.stats.stop_reason == "end_turn"

    with_tool = SseReasoningStripper()
    with_tool.feed(_sse(*TOOL_STREAM_EVENTS))
    with_tool.finish()
    assert with_tool.stats.empty is False


def test_json_reply_that_is_only_reasoning_is_flagged_empty() -> None:
    body = json.dumps({
        "type": "message", "stop_reason": "max_tokens",
        "content": [{"type": "thinking", "thinking": "endless"}],
    }).encode()
    _, stats = strip_reasoning_json(body)
    assert stats.empty is True and stats.stop_reason == "max_tokens"


def test_content_delivered_on_a_dropped_thinking_block_is_counted_not_lost_silently() -> None:
    raw = _sse(
        MESSAGE_START,
        _start(0, "thinking"),
        _delta(0, "thinking_delta", thinking="t"),
        _delta(0, "text_delta", text="answer on the wrong block"),
        _delta(0, "input_json_delta", partial_json="{}"),
        _stop(0),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "end_turn"}}, MESSAGE_STOP,
    )
    stripper = SseReasoningStripper()
    stripper.feed(raw)
    assert stripper.stats.misplaced == 2 and bool(stripper.stats)
