"""#162: the reply-refusals sink (module, CLI, doctor + status surfacing).

A SEPARATE sink from the poison-inbound `Store.dead_letter()` path - see
`reply_refusals.py`'s own module docstring. Covers the sink functions
directly, the `reply-refusal list/show/resolve` CLI verbs (incl. the
liaison/sole-lead authority gate on resolve, mirroring `dead-letter
resolve`), doctor's `_check_reply_refused`, and `status`'s summary line -
all resolved-aware (a resolved entry drops out of the default list/WARN/
summary, same convention as dead-letter's own resolved-awareness).
"""
from __future__ import annotations

from pathlib import Path

from agenttalk import cli, doctor, health as hm, reply_refusals
from agenttalk.store import Store


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def _run(root: Path, *argv: str) -> int:
    return cli.main(["--root", str(root), *argv])


def _seed(s: Store, *, agent: str = "beta", mid: str = "20990101-000000-000000-RFSD",
          reason: str = "OSError: publication lock timeout") -> str:
    reply_refusals.record_reply_refusal(
        s, agent=agent, original_message_id=mid, original_from="alpha",
        intended_kind="message", reason=reason,
        draft_path=str(s.dir / "reply-drafts" / agent / f"{mid}.refused.md"),
        correlation={"request_id": "q-162"}, at=hm.now_iso(),
    )
    return mid


# --------------------------------------------------------------- sink module

def test_record_list_read_round_trip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _seed(s)
    items = reply_refusals.list_reply_refusals(s)
    assert len(items) == 1
    assert items[0]["original_message_id"] == mid
    assert items[0]["_agent"] == "beta"
    record = reply_refusals.read_reply_refusal(s, "beta", mid)
    assert record is not None
    assert record["reason"] == "OSError: publication lock timeout"
    assert record["correlation"] == {"request_id": "q-162"}
    assert reply_refusals.reply_refusal_count(s) == 1
    assert not reply_refusals.is_resolved(s, "beta", mid)


def test_list_filters_by_agent(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.init(["alpha", "beta", "gamma"])
    _seed(s, agent="beta", mid="20990101-000000-000000-AAAA")
    _seed(s, agent="gamma", mid="20990101-000000-000000-BBBB")
    assert len(reply_refusals.list_reply_refusals(s, "beta")) == 1
    assert len(reply_refusals.list_reply_refusals(s, "gamma")) == 1
    assert len(reply_refusals.list_reply_refusals(s)) == 2


def test_record_collision_gets_a_distinct_timestamped_sibling(tmp_path: Path) -> None:
    # A second refusal on the SAME original_message_id (a retried draft) must
    # not silently overwrite the first - collision-safe, same convention
    # Store.dead_letter() uses for its own payload collisions.
    s = _store(tmp_path)
    mid = "20990101-000000-000000-RFSD"
    p1 = reply_refusals.record_reply_refusal(
        s, agent="beta", original_message_id=mid, original_from="alpha",
        intended_kind="message", reason="first", draft_path="d1",
        correlation={}, at="2026-09-16T00:00:00Z")
    p2 = reply_refusals.record_reply_refusal(
        s, agent="beta", original_message_id=mid, original_from="alpha",
        intended_kind="message", reason="second", draft_path="d2",
        correlation={}, at="2026-09-16T00:00:01Z")
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_resolve_requires_non_empty_reason(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _seed(s)
    try:
        reply_refusals.resolve_reply_refusal(
            s, "beta", mid, reason="   ", sender="alpha", at=hm.now_iso())
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert not reply_refusals.is_resolved(s, "beta", mid)


def test_resolve_unknown_record_raises(tmp_path: Path) -> None:
    s = _store(tmp_path)
    try:
        reply_refusals.resolve_reply_refusal(
            s, "beta", "20990101-000000-000000-NONE", reason="x",
            sender="alpha", at=hm.now_iso())
        raised = False
    except FileNotFoundError:
        raised = True
    assert raised


# --------------------------------------------------------------------- CLI

def test_cli_list_show_empty(tmp_path: Path) -> None:
    _store(tmp_path)
    assert _run(tmp_path, "reply-refusal", "list") == 0
    rc = _run(tmp_path, "reply-refusal", "show", "--agent", "beta", "--id", "nope")
    assert rc == 2


def test_cli_list_and_show_populated(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    mid = _seed(s)
    assert _run(tmp_path, "reply-refusal", "list") == 0
    out = capsys.readouterr().out
    assert "beta/" + mid in out
    assert _run(tmp_path, "reply-refusal", "show", "--agent", "beta", "--id", mid) == 0
    out = capsys.readouterr().out
    assert "publication lock timeout" in out
    # #162 review ask: show prints a ready-to-run re-publish recipe.
    assert f"agenttalk reply --from beta --to-id {mid}" in out


def test_cli_resolve_requires_liaison_authority(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    mid = _seed(s)
    rc = _run(tmp_path, "reply-refusal", "resolve", "--from", "beta",
              "--agent", "beta", "--id", mid, "--reason", "not authorized")
    assert rc == 2
    assert not reply_refusals.is_resolved(s, "beta", mid)


def test_cli_resolve_requires_reason(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    mid = _seed(s)
    rc = _run(tmp_path, "reply-refusal", "resolve", "--from", "alpha",
              "--agent", "beta", "--id", mid, "--reason", "   ")
    assert rc == 2
    assert not reply_refusals.is_resolved(s, "beta", mid)


def test_cli_resolve_hides_from_default_list(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    mid = _seed(s)
    rc = _run(tmp_path, "reply-refusal", "resolve", "--from", "alpha",
              "--agent", "beta", "--id", mid, "--reason", "requeued by hand")
    assert rc == 0
    assert reply_refusals.is_resolved(s, "beta", mid)
    _run(tmp_path, "reply-refusal", "list")
    assert "none" in capsys.readouterr().out
    _run(tmp_path, "reply-refusal", "list", "--all")
    assert ("beta/" + mid) in capsys.readouterr().out


# ------------------------------------------------------------ doctor + status

def test_doctor_check_absent_when_none(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert doctor._check_reply_refused(s) is None


def test_doctor_check_warns_when_routable(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    mid = _seed(s)
    check = doctor._check_reply_refused(s)
    assert check is not None
    assert check.status == "warn"
    assert mid in check.details


def test_doctor_check_errors_loud_when_unroutable(tmp_path: Path) -> None:
    # No operator_facing liaison and no sole lead configured at all.
    s = _store(tmp_path)
    _seed(s)
    check = doctor._check_reply_refused(s)
    assert check is not None
    assert check.status == "error"
    assert "NO escalation target resolves" in check.details


def test_doctor_check_errors_loud_when_only_target_is_the_refusing_seat(
    tmp_path: Path,
) -> None:
    s = _store(tmp_path)
    s.set_role("beta", "lead")  # beta is BOTH the refusing seat and the sole lead
    _seed(s, agent="beta")
    check = doctor._check_reply_refused(s)
    assert check is not None
    assert check.status == "error"
    assert "cannot escalate to itself" in check.details


def test_doctor_check_clears_once_resolved(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    mid = _seed(s)
    assert doctor._check_reply_refused(s) is not None
    reply_refusals.resolve_reply_refusal(
        s, "beta", mid, reason="handled", sender="alpha", at=hm.now_iso())
    assert doctor._check_reply_refused(s) is None


def test_status_summary_line_and_json(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    _seed(s)
    assert _run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "reply-refused: 1 refused reply draft(s)" in out
    assert _run(tmp_path, "status", "--json") == 0
    import json
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply_refused_count"] == 1


def test_status_summary_line_absent_when_resolved(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    s.set_role("alpha", "lead")
    mid = _seed(s)
    reply_refusals.resolve_reply_refusal(
        s, "beta", mid, reason="handled", sender="alpha", at=hm.now_iso())
    assert _run(tmp_path, "status") == 0
    assert "reply-refused" not in capsys.readouterr().out
