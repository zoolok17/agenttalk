"""Tests for WP-1: the `release` stand-down signal + listen-exit clarity.

`release` is a dedicated loop-control kind so a prose "done for now" can never
be misread as "stop listening". The exit DECISION lives in the listen skill, so
the code tests assert SURFACING + SEND + AUTHORIZATION semantics, and a
skill-text-contract test guards the behavioral rule against drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agenttalk
from agenttalk import cli
from agenttalk.store import CONTROL_KINDS, KNOWN_KINDS, OPENER_KINDS, Store


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _team(tmp_path: Path, agents: str = "lead,worker") -> Store:
    s = Store(tmp_path)
    s.init(agents.split(","))
    return s


# ------------------------------------------------ (a) known kind, not control

def test_release_is_known_non_control_non_opener() -> None:
    assert "release" in KNOWN_KINDS
    assert "release" not in CONTROL_KINDS   # must surface in wait/recv
    assert "release" not in OPENER_KINDS    # opens no thread


def test_release_message_validates(tmp_path: Path) -> None:
    s = _team(tmp_path)
    m = s.send(sender="lead", recipient="worker", body="stand down", kind="release")
    m.validate(["lead", "worker"])  # does not raise
    # clean: a release is not an opener, so send mints no thread correlation id
    assert "request_id" not in (m.meta or {})
    assert "broadcast_id" not in (m.meta or {})


# ------------------------------------------------ (b) wait surfaces release

def test_wait_returns_release_like_a_real_message(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    s.send(sender="lead", recipient="worker", body="stand down", kind="release")
    rc = _run(["wait", "--for", "worker", "--timeout", "5", "--quiet"], tmp_path)
    assert rc == 0  # surfaced (NOT filtered like a control kind)
    assert "stand down" in capsys.readouterr().out


def test_wait_returns_done_for_now_note_identically(tmp_path: Path, capsys) -> None:
    """Contrast: at the BUS layer a 'done for now' note ALSO just surfaces
    (exit 0). 'keep listening vs exit' is purely the skill's call on the KIND,
    never the body — so the bus treats release and a prose note identically."""
    s = _team(tmp_path)
    s.send(sender="lead", recipient="worker", body="great work, you're done for now",
           kind="note")
    rc = _run(["wait", "--for", "worker", "--timeout", "5", "--quiet"], tmp_path)
    assert rc == 0
    assert "done for now" in capsys.readouterr().out


# ------------------------------------------------ (c) recv shows release

def test_recv_shows_release_in_default_view(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path)
    s.send(sender="lead", recipient="worker", body="stand down", kind="release")
    rc = _run(["recv", "--for", "worker"], tmp_path)
    assert rc == 0
    assert "stand down" in capsys.readouterr().out  # not control-filtered


# ------------------------------------------------ (d) the release command

def _sessions_transcripts(s: Store) -> list[Path]:
    d = s.dir / "sessions"
    return list(d.glob("transcript-*")) if d.is_dir() else []


def test_release_single_carries_authority_meta_and_no_transcript(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")
    before = _sessions_transcripts(s)
    rc = _run(["release", "--from", "lead", "--to", "worker", "--relay-human",
               "-m", "operator says stand down", "--quiet"], tmp_path)
    assert rc == 0
    msgs = s.messages_for("worker")
    assert len(msgs) == 1 and msgs[0].kind == "release"
    meta = msgs[0].meta or {}
    assert meta.get("release_authority") == "human"      # authority envelope (0.39.0)
    assert meta.get("operator_decision") == "true"
    assert meta.get("authority_reason") == "operator says stand down"
    assert "request_id" not in meta and "broadcast_id" not in meta  # opens no thread
    assert _sessions_transcripts(s) == before            # NO transcript (unlike `end`)


def test_release_all_fans_out_to_every_other_active(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead,w1,w2,w3")
    s.set_operator_facing("lead")
    rc = _run(["release", "--from", "lead", "--all", "--relay-human", "-m", "wrap up",
               "--quiet"], tmp_path)
    assert rc == 0
    for w in ("w1", "w2", "w3"):
        got = s.messages_for(w)
        assert len(got) == 1 and got[0].kind == "release"
        assert (got[0].meta or {}).get("release_authority") == "human"
    assert s.messages_for("lead") == []  # sender excluded


def test_release_to_group(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead,w1,w2,w3")
    s.set_operator_facing("lead")
    s.set_group("pod", ["w1", "w2"])
    rc = _run(["release", "--from", "lead", "--to-group", "pod", "--emergency",
               "-m", "pod agents rogue", "--quiet"], tmp_path)
    assert rc == 0
    assert (s.messages_for("w1")[0].meta or {}).get("release_authority") == "emergency"
    assert len(s.messages_for("w2")) == 1
    assert s.messages_for("w3") == []  # not in the group


def test_release_requires_authority_mode_and_reason(tmp_path: Path) -> None:
    s = _team(tmp_path)
    s.set_operator_facing("lead")
    # bare release (no mode) -> exit 2, no message
    assert _run(["release", "--from", "lead", "--to", "worker", "--quiet"], tmp_path) == 2
    # mode but no reason -> exit 2
    assert _run(["release", "--from", "lead", "--to", "worker", "--relay-human",
                 "--quiet"], tmp_path) == 2
    assert s.messages_for("worker") == []   # nothing sent on refusal


def test_release_requires_exactly_one_target(tmp_path: Path) -> None:
    _team(tmp_path)
    assert _run(["release", "--from", "lead", "--quiet"], tmp_path) == 2
    assert _run(["release", "--from", "lead", "--to", "worker", "--all",
                 "--quiet"], tmp_path) == 2


# ------------------------------------------------ (f) authorization

def test_is_release_authorized_liaison(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead,worker,other")
    s.set_operator_facing("lead")
    assert s.is_release_authorized("lead") is True
    assert s.is_release_authorized("worker") is False
    assert s.is_release_authorized("other") is False


def test_is_release_authorized_sole_lead(tmp_path: Path) -> None:
    s = _team(tmp_path, "lead,worker")
    s.set_role("lead", "lead")
    assert s.is_release_authorized("lead") is True
    assert s.is_release_authorized("worker") is False


def test_is_release_authorized_plain_pair_fails_closed(tmp_path: Path) -> None:
    """0.40.0 unification: is_release_authorized now DELEGATES to the single
    loop-exit resolver, which has NO zero-lead any-active fallback. A plain pair
    with no liaison and no role=lead authorizes NO ONE (fail closed) - it must
    match loop_exit_relay_authorized exactly (no authority drift)."""
    s = _team(tmp_path, "alpha,beta")
    assert s.is_release_authorized("alpha") is False
    assert s.is_release_authorized("beta") is False
    assert s.is_release_authorized("ghost") is False
    # the two resolvers are now ONE: identical answers for every sender.
    for who in ("alpha", "beta", "ghost"):
        assert s.is_release_authorized(who) == s.loop_exit_relay_authorized(who)
    # designating a liaison restores a single authority
    s.set_operator_facing("alpha")
    assert s.is_release_authorized("alpha") is True
    assert s.is_release_authorized("beta") is False


def test_is_release_authorized_fails_closed_on_ambiguous_multilead(
    tmp_path: Path
) -> None:
    """Regression (codex MAJOR — broken access control): with 2+ role=lead and
    NO operator_facing, leadership is ambiguous => authorize NO ONE (fail
    closed). sole_lead() returns None for both zero and 2+ leads, so this must
    NOT fall through to the any-active-agent fallback."""
    s = _team(tmp_path, "lead1,lead2,worker")
    s.set_role("lead1", "lead")
    # set_role enforces at-most-one-lead, so force a 2-lead config directly.
    import json as _json
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["roles"] = {"lead1": "lead", "lead2": "lead"}
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    assert s.sole_lead() is None                  # ambiguous
    assert s.is_release_authorized("lead1") is False
    assert s.is_release_authorized("lead2") is False
    assert s.is_release_authorized("worker") is False  # NOT the fallback
    # repairing it (designate a liaison) restores a single authority
    s.set_operator_facing("lead1")
    assert s.is_release_authorized("lead1") is True
    assert s.is_release_authorized("worker") is False


def test_release_unauthorized_sender_fails_closed(tmp_path: Path, capsys) -> None:
    # 0.39.0: an unauthorized relay now FAILS CLOSED (exit 2, NO message), not a warn.
    s = _team(tmp_path, "lead,worker,other")
    s.set_operator_facing("lead")
    rc = _run(["release", "--from", "other", "--to", "worker", "--relay-human",
               "-m", "x", "--quiet"], tmp_path)
    assert rc == 2
    assert "not the authorized stand-down relay" in capsys.readouterr().err
    assert s.messages_for("worker") == []   # nothing sent


def test_release_authorized_relay_succeeds(tmp_path: Path, capsys) -> None:
    s = _team(tmp_path, "lead,worker")
    s.set_operator_facing("lead")
    rc = _run(["release", "--from", "lead", "--to", "worker", "--relay-human",
               "-m", "operator decision", "--quiet"], tmp_path)
    assert rc == 0
    assert len(s.messages_for("worker")) == 1


# ------------------------------------------------ (e) skill-text contract

def _skill(*parts: str) -> str:
    return (Path(agenttalk.__file__).parent / "skills" / Path(*parts)).read_text(
        encoding="utf-8")


@pytest.mark.parametrize("path", [
    ("claude", "agenttalk.listen.md"),
    ("codex", "agenttalk-listen", "SKILL.md"),
])
def test_listen_skills_state_release_end_only_exit_and_antipattern(path) -> None:
    text = _skill(*path)
    assert "exits ONLY on `kind=release`" in text  # the hard rule
    assert "KEEP LISTENING" in text
    assert "Anti-pattern" in text                  # the exact-trap example
    assert "done for now" in text                  # the prose that must NOT exit
    # stand-down authority (0.39.0): the marker contract + the lead-prose trap
    assert "release_authority" in text
    assert "authority_reason" in text
    assert "even from the lead" in text            # lead prose must NOT stop you
    # NEGATIVE regression: the stale unmarked-exit wording in the bottom Exiting
    # section must be GONE (the bypass codex+reviewer-1 found), not just shadowed.
    assert "The peer sends `kind=end` (graceful shutdown)" not in text
    assert "`kind=release` from an authorized sender (stand down" not in text
    # the EMERGENCY envelope must state operator_report_required EVERYWHERE it is
    # described (top authority block + bottom Exiting) - not just somewhere in the
    # file (reviewer-1: the top block omitted it). Every emergency mention pairs it.
    assert text.count("operator_report_required") >= text.count("release_authority=emergency")
    assert "release_authority=emergency`\n+ `emergency=true` + `operator_report_required=true`" in text \
        or "`emergency=true` + `operator_report_required=true`" in text


@pytest.mark.parametrize("path", [
    ("claude", "agenttalk.sk-loop.md"),
    ("codex", "agenttalk-sk-loop", "SKILL.md"),
])
def test_sk_loop_skills_require_authority_envelope_to_exit(path) -> None:
    text = _skill(*path)
    # the sk-loop must NOT say a bare kind=end exits; it must require the envelope.
    assert "Only exit on\nmission completion, `kind=end`" not in text
    assert "release_authority" in text and "authority_reason" in text
    assert "KEEP LISTENING" in text


@pytest.mark.parametrize("path", [
    ("claude", "agenttalk.lead.md"),
    ("codex", "agenttalk-lead", "SKILL.md"),
])
def test_lead_skills_state_release_to_stop_and_note_for_done(path) -> None:
    text = _skill(*path)
    assert "agenttalk release" in text             # the way to actually stop a member
    assert "done for now" in text
    assert "never stops anyone" in text            # a note doesn't stop a listener
    # stand-down authority (0.39.0): lead never originates a normal stand-down
    assert "--relay-human" in text
    assert "--emergency" in text
    assert "NEVER originate a" in text or "never originate a" in text.lower()
