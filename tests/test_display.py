"""Tests for display.render — the human-readable message block."""

from __future__ import annotations

from agenttalk.display import render
from agenttalk.store import Message


def _make_msg(**overrides) -> Message:
    base = {
        "id": "20260521-120000-000000-AAAA",
        "ts": "2026-05-21T12:00:00.000000Z",
        "sender": "alpha",
        "recipient": "beta",
        "kind": "message",
        "subject": "",
        "body": "hello",
        "meta": {},
    }
    base.update(overrides)
    return Message(**base)


def test_render_basic_message_has_all_expected_lines() -> None:
    out = render(_make_msg())
    lines = out.split("\n")
    # Top + bottom bars
    assert lines[0] == "=" * 72
    assert lines[-1] == "=" * 72
    # Required fields
    assert any("id      " in ln for ln in lines)
    assert any("at      " in ln for ln in lines)
    assert "hello" in out


def test_render_uses_custom_header_when_provided() -> None:
    out = render(_make_msg(), header="AGENTTALK :: CUSTOM HEADER")
    assert "AGENTTALK :: CUSTOM HEADER" in out


def test_render_omits_kind_line_for_default_message_kind() -> None:
    out = render(_make_msg(kind="message"))
    # Default kind shouldn't add a meta line
    assert "kind=" not in out


def test_render_includes_kind_when_non_default() -> None:
    out = render(_make_msg(kind="review-request"))
    assert "kind=review-request" in out


def test_render_subject_line_only_when_present() -> None:
    with_subject = render(_make_msg(subject="WP01 ready"))
    assert "subject WP01 ready" in with_subject
    without_subject = render(_make_msg(subject=""))
    assert "subject " not in without_subject


def test_render_meta_line_renders_all_key_value_pairs() -> None:
    out = render(_make_msg(meta={"wp_id": "WP01", "request_id": "abc"}))
    assert "wp_id=WP01" in out
    assert "request_id=abc" in out


def test_render_empty_body_shows_placeholder() -> None:
    out = render(_make_msg(body=""))
    assert "(empty body)" in out


def test_render_strips_trailing_whitespace_from_body() -> None:
    out = render(_make_msg(body="hello\n\n\n"))
    # The body+bottom-bar transition has exactly one newline,
    # not multiple blank lines
    assert "hello\n" + "=" * 72 == out.rsplit("\n" + "-" * 72 + "\n", 1)[1]


def test_render_preserves_unicode_in_body() -> None:
    out = render(_make_msg(body="approved → ship · 我们 🎉"))
    assert "approved → ship · 我们 🎉" in out


def test_render_handles_multiline_body() -> None:
    body = "line 1\nline 2\nline 3"
    out = render(_make_msg(body=body))
    assert "line 1\nline 2\nline 3" in out
