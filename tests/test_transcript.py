"""Tests for transcript.to_markdown / to_jsonl / export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk.store import Message, Store
from agenttalk import transcript as tx


def _make_msg(**overrides) -> Message:
    base = dict(
        id="20260521-120000-000000-AAAA",
        ts="2026-05-21T12:00:00.000000Z",
        sender="alpha",
        recipient="beta",
        kind="message",
        subject="",
        body="hello",
        meta={},
    )
    base.update(overrides)
    return Message(**base)


# ----------------------------------------------------------- to_markdown

def test_markdown_empty_list_renders_placeholder() -> None:
    out = tx.to_markdown([])
    assert "# agenttalk transcript" in out
    assert "(no messages)" in out


def test_markdown_renders_basic_message() -> None:
    out = tx.to_markdown([_make_msg(body="hi there")])
    assert "alpha → beta" in out
    assert "hi there" in out


def test_markdown_includes_kind_when_not_default() -> None:
    out = tx.to_markdown([_make_msg(kind="review-request")])
    assert "*(review-request)*" in out
    # And does NOT label plain `message` (it's the default)
    out_default = tx.to_markdown([_make_msg(kind="message")])
    assert "*(message)*" not in out_default


def test_markdown_includes_subject_and_meta_when_present() -> None:
    out = tx.to_markdown([_make_msg(
        subject="WP01 ready",
        meta={"wp_id": "WP01", "request_id": "abc-123"},
    )])
    assert "**Subject:** WP01 ready" in out
    assert "wp_id=WP01" in out
    assert "request_id=abc-123" in out


def test_markdown_handles_empty_body() -> None:
    out = tx.to_markdown([_make_msg(body="")])
    assert "(empty body)" in out


def test_markdown_preserves_unicode_in_body() -> None:
    """Arrows, em-dashes, non-Latin — must survive without escape garbage."""
    out = tx.to_markdown([_make_msg(body="approved → ship · 我们 🎉")])
    assert "approved → ship · 我们 🎉" in out


def test_markdown_separates_messages_with_horizontal_rule() -> None:
    out = tx.to_markdown([_make_msg(id="m1"), _make_msg(id="m2", body="second")])
    assert out.count("---") >= 2  # one per message


# ------------------------------------------------------------- to_jsonl

def test_jsonl_empty_list_renders_empty_string() -> None:
    assert tx.to_jsonl([]) == ""


def test_jsonl_one_line_per_message() -> None:
    msgs = [_make_msg(id="m1"), _make_msg(id="m2", body="second")]
    out = tx.to_jsonl(msgs)
    lines = out.strip().split("\n")
    assert len(lines) == 2


def test_jsonl_each_line_is_valid_json() -> None:
    msgs = [_make_msg(id="m1"), _make_msg(id="m2",
                                          body="line\nwith\nnewlines",
                                          meta={"k": "v"})]
    out = tx.to_jsonl(msgs)
    for line in out.strip().split("\n"):
        data = json.loads(line)
        assert "id" in data
        assert "from" in data
        assert "to" in data


def test_jsonl_preserves_unicode_without_escaping() -> None:
    """ensure_ascii=False so the on-disk file is human-readable."""
    out = tx.to_jsonl([_make_msg(body="approved →")])
    assert "approved →" in out
    assert "\\u2192" not in out


# -------------------------------------------------------------- export

def test_export_markdown_writes_to_default_session_path(
    store_root: Path,
) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="one")
    path = tx.export(s, fmt="md")
    assert path.exists()
    assert path.parent == s.sessions_dir
    assert path.suffix == ".md"
    content = path.read_text(encoding="utf-8")
    assert "alpha → beta" in content


def test_export_jsonl_writes_to_default_session_path(
    store_root: Path,
) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="one")
    path = tx.export(s, fmt="jsonl")
    assert path.exists()
    assert path.suffix == ".jsonl"
    lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l]
    assert len(lines) == 1
    assert json.loads(lines[0])["body"] == "one"


def test_export_to_explicit_out_path(store_root: Path, tmp_path: Path) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="one")
    custom = tmp_path / "custom-transcript.md"
    returned = tx.export(s, fmt="md", out=custom)
    assert returned == custom
    assert custom.exists()


def test_export_rejects_unknown_format(store_root: Path) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="one")
    with pytest.raises(ValueError, match="unknown format"):
        tx.export(s, fmt="xml")  # type: ignore[arg-type]
