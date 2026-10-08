"""Class A: publication never borrows another account or the caller's session (#301).

The seeded-fault tests run the same assertions healthy, broken, then restored.
Only model execution and loop scheduling are replaced; readers and stores are real.
"""

import json
import os
from pathlib import Path

import pytest

from agenttalk import capacity as cap, checkpoint, cli
from agenttalk.wrapper import loop, run, session
from capacity_class_helpers import (
    AT, OTHER, OWN, directory_link, isolate, remove_directory_link, rollout, statusline, stream,
)

CASES = (
    "wrapper-claude-stream", "wrapper-claude-file", "wrapper-provider-change",
    "manual-self", "manual-self-mismatch", "manual-self-profile", "manual-self-other-session",
    "manual-other-saved", "manual-other-provider", "manual-other-unbound",
    "manual-explicit-match", "manual-explicit-mismatch", "manual-explicit-unbound",
    "wrapper-codex-thread", "wrapper-codex-no-thread",
    "manual-codex-thread", "manual-codex-no-thread", "manual-codex-private",
    "codex-linked-shared", "codex-linked-private", "relocated-claude", "relocated-codex",
    "recorded-events", "providers-same-home", "direct-foreign-stream",
)


@pytest.fixture
def world(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_load_supervisor_config", lambda _: {"agents": {"alpha": {}}})
    monkeypatch.setattr(run, "make_drive", lambda *a, **kw: lambda record: True)

    def refresh_once(store, agent, drive, **kwargs):
        kwargs["capacity_refresh"]()
        return 0

    monkeypatch.setattr(loop, "run_loop", refresh_once)
    return tmp_path, monkeypatch, store


def _publish(store, source, *, wrapper=False, profile=None, extra=()):
    if wrapper:
        result = cli._wrap_loop_mode(
            store, "alpha", cli=source, base_argv=[source], sender="alpha",
            min_interval=0, render=False, backend_profile=profile)
    else:
        result = cli.main(["--root", str(store.root), "capacity", "refresh", "--for", "alpha",
                           "--source", source, *extra])
    assert result == 0, "publication command failed"
    snap = store.read_capacity("alpha")
    assert snap is not None, "publication missing"
    return snap


def _assert_reading(snap, expected, account, foreign, context=None):
    assert snap["source_agent"] == "alpha", "reading isolation: wrong seat"
    assert snap["primary_used_percent"] == expected, "reading isolation: wrong percentage"
    assert snap["primary_used_percent"] != OTHER, "reading isolation: foreign budget"
    assert snap["context_used_percent"] != OTHER, "reading isolation: foreign conversation"
    assert snap["context_used_percent"] == context, "reading isolation: wrong conversation fill"
    if expected is None:
        assert snap["source"] == "unknown", "reading isolation: unsupported source"
        assert snap["context_used_percent"] is None, "reading isolation: unexpected conversation"
    else:
        assert snap["confidence"] == "observed", "reading isolation: valid reading lost"
        assert snap["account"] == account, "account isolation: wrong binding"
        assert snap["account"] != foreign, "account isolation: accounts merged"


def _check_claude(case, world):
    root, mp, store = world
    caller = Path.home() / ".claude"
    own = root / "target-claude"
    profile = "ovh-qwen" if case in ("manual-self-profile", "wrapper-provider-change") else None
    if profile:
        own = Path(run.child_claude_config_dir(store.root, profile))
    own_path = statusline(own, OWN)
    statusline(caller, OTHER, "other-session")
    provider = "ovh-qwen" if case == "manual-other-provider" else profile or "claude"
    binding = cap.account_key(provider, own)
    foreign = cap.account_key("claude", caller)
    saved = stream(binding)
    expected = OWN
    extra = ()
    file_cases = ("wrapper-claude-file", "manual-self", "manual-self-profile", "manual-explicit-match",
                  "manual-explicit-unbound", "wrapper-provider-change", "manual-self-other-session")
    if case in file_cases:
        saved = {"binding": binding}
    if case == "manual-self-other-session":
        payload = json.loads(own_path.read_text(encoding="utf-8"))
        payload["session_id"] = "other-session"
        payload["context_window"]["used_percentage"] = OTHER
        own_path.write_text(json.dumps(payload), encoding="utf-8")
        # Keep the file's observation time fixed after replacing its content.
        os.utime(own_path, (AT.timestamp(), AT.timestamp()))
    if case in ("manual-other-unbound", "manual-explicit-unbound"):
        saved = {}
    if case in ("manual-self-mismatch", "manual-explicit-mismatch", "wrapper-provider-change"):
        saved = stream(foreign, OTHER)
    if case in ("manual-other-unbound", "manual-self-mismatch", "manual-explicit-mismatch"):
        expected = None
    if case.startswith("manual-explicit"):
        extra = ("--statusline-path", str(own_path))
    if case.startswith("manual-self") or case.startswith("wrapper"):
        mp.setenv("AGENTTALK_SELF", "alpha")
        mp.setenv("CLAUDE_CONFIG_DIR", str(own if not profile else caller))
    else:
        mp.setenv("AGENTTALK_SELF", "beta")
        mp.setenv("CLAUDE_CONFIG_DIR", str(caller))
    if profile:
        mp.setattr(cli, "_load_supervisor_config", lambda _: {
            "agents": {"alpha": {"backend_profile": profile}}})
    session.save_session(store, "alpha", session.SessionState(
        cli="claude", claude_session_id="target-session", claude_rate_limit=saved))
    snap = _publish(store, "claude", wrapper=case.startswith("wrapper"), profile=profile, extra=extra)
    context = OWN if case in file_cases and case != "manual-self-other-session" else None
    _assert_reading(snap, expected, binding, foreign, context)


def _check_codex(case, world):
    root, mp, store = world
    shared = Path.home() / ".codex"
    private = root / "target-codex"
    # A shared provider home holds multiple conversations. The private home holds only its owner's.
    rollout(shared, OWN, "target-thread")
    rollout(shared, OTHER, "caller-thread")
    rollout(private, OWN, "target-thread")
    mp.setenv("CODEX_THREAD_ID", "caller-thread")
    no_thread = case.endswith("no-thread") or case == "codex-linked-shared"
    use_private = case in ("manual-codex-private", "codex-linked-private")
    tid = None if no_thread or use_private else "target-thread"
    session.save_session(store, "alpha", session.SessionState(cli="codex", codex_thread_id=tid))
    home = private if use_private else shared
    link = root / "home-link"
    if case.startswith("codex-linked"):
        directory_link(link, home)
        mp.setenv("CODEX_HOME", str(link))
    elif use_private:
        mp.setenv("CODEX_HOME", str(private))
    try:
        snap = _publish(store, "codex", wrapper=case.startswith("wrapper"))
        expected = None if no_thread else OWN
        _assert_reading(snap, expected, cap.account_key("codex", home),
                        cap.account_key("codex", shared if use_private else private), expected)
        if no_thread:
            assert snap["reason"] == "codex_no_thread_yet", "reading isolation: wrong unknown reason"
    finally:
        if case.startswith("codex-linked"):
            remove_directory_link(link)


def _check_relocated(case, world):
    root, mp, store = world
    source = case.removeprefix("relocated-")
    snapshots = []
    for name, pct in (("first", OWN), ("second", OTHER)):
        user_home = root / name
        mp.setenv("HOME", str(user_home))
        mp.setenv("USERPROFILE", str(user_home))
        mp.setenv("AGENTTALK_SELF", "alpha")
        if source == "claude":
            statusline(user_home / ".claude", pct)
            state = session.SessionState(cli=source)
        else:
            rollout(user_home / ".codex", pct, "target-thread")
            state = session.SessionState(cli=source, codex_thread_id="target-thread")
        session.save_session(store, "alpha", state)
        snapshots.append(_publish(store, source))
    assert [s["primary_used_percent"] for s in snapshots] == [OWN, OTHER], "reading isolation: wrong home"
    assert snapshots[0]["account"] != snapshots[1]["account"], "account isolation: relocated homes merged"
    assert snapshots[0]["primary_used_percent"] != snapshots[1]["primary_used_percent"]


def _check(case, world):
    root, mp, store = world
    if case == "providers-same-home":
        home = root / "shared-home"
        assert cap.account_key("claude", home) != cap.account_key("codex", home), (
            "account isolation: providers merged")
    elif case == "direct-foreign-stream":
        home = root / "own-home"
        statusline(home, OWN)
        binding = cap.account_key("claude", home)
        foreign = cap.account_key("claude", root / "other-home")
        snap = cap.read_local("alpha", source="claude", claude_home=home,
                              stream=stream(foreign, OTHER), session_id="target-session")
        _assert_reading(snap.to_dict(), OWN, binding, foreign, OWN)
    elif case == "recorded-events":
        home = root / "recorded-home"
        binding = cap.account_key("claude", home)
        state = session.SessionState(cli="claude", claude_rate_limit={"binding": binding})
        for window, percent in (("five_hour", OWN), ("seven_day", 34)):
            session.observe_event(state, {"type": "rate_limit_event", "rate_limit_info": {
                "rateLimitType": window, "status": "allowed", "utilization": percent / 100,
            }})
        session.save_session(store, "alpha", state)
        # Publishing for another seat cannot repair a lost binding from the caller's home.
        mp.setenv("AGENTTALK_SELF", "beta")
        snap = _publish(store, "claude")
        _assert_reading(snap, OWN, binding, cap.account_key("claude", Path.home() / ".claude"))
        assert snap["secondary_used_percent"] == 34, "reading isolation: second event lost"
    elif case.startswith("relocated"):
        _check_relocated(case, world)
    elif "codex" in case:
        _check_codex(case, world)
    else:
        _check_claude(case, world)


@pytest.mark.parametrize("case", CASES)
def test_account_isolation(case, world):
    _check(case, world)


@pytest.mark.xfail(strict=True, reason="#408: checkpoint borrows the caller's status line",
                   raises=AssertionError)
def test_checkpoint_for_another_seat_never_borrows_callers_context(world):
    root, mp, _ = world
    home = root / "caller-claude"
    statusline(home, OTHER, "caller-session")
    mp.setenv("CLAUDE_CONFIG_DIR", str(home))
    mp.setenv("AGENTTALK_SELF", "beta")
    context = checkpoint.collect_context("alpha", source="claude")
    assert context == {"pct": None, "limit": None, "used": None, "source": None}


FAULTS = (
    ("omit-home-from-account", "relocated-claude", "account isolation: relocated homes merged"),
    ("read-callers-statusline", "manual-other-saved", "reading isolation: wrong percentage"),
    ("borrow-callers-thread", "manual-codex-no-thread", "reading isolation: wrong percentage"),
    ("linked-shared-is-private", "codex-linked-shared", "reading isolation: wrong percentage"),
    ("ignore-explicit-binding", "manual-explicit-mismatch", "reading isolation: wrong percentage"),
    ("recorder-forgets-binding", "recorded-events", "reading isolation: wrong percentage"),
    ("omit-provider-from-account", "providers-same-home", "account isolation: providers merged"),
    ("ignore-read-local-binding", "direct-foreign-stream", "reading isolation: wrong percentage"),
)


def _plant(mp, fault):
    if fault == "omit-home-from-account":
        mp.setattr(cap, "account_key", lambda provider, home: f"{provider}:synthetic")
    elif fault in ("read-callers-statusline", "ignore-explicit-binding"):
        def wrong_source(store, agent, saved, statusline_path):
            return cap.for_publication(cap.read_local(
                agent, source="claude", statusline_path=statusline_path,
                session_id=saved.claude_session_id))
        mp.setattr(cli, "_manual_claude_snapshot", wrong_source)
    elif fault == "borrow-callers-thread":
        original = cap.read_local

        def wrong_thread(*args, **kwargs):
            kwargs["thread_id"] = os.environ["CODEX_THREAD_ID"]
            return original(*args, **kwargs)
        mp.setattr(cap, "read_local", wrong_thread)
    elif fault == "linked-shared-is-private":
        mp.setattr(cap, "_same_path", lambda a, b: os.path.abspath(a) == os.path.abspath(b))
    elif fault == "recorder-forgets-binding":
        original = session.observe_event

        def forget_binding(state, raw):
            state.claude_rate_limit = None
            original(state, raw)
        mp.setattr(session, "observe_event", forget_binding)
    elif fault == "omit-provider-from-account":
        original = cap.account_key
        mp.setattr(cap, "account_key", lambda provider, home: original("claude", home))
    elif fault == "ignore-read-local-binding":
        def trust_stream(agent, **kwargs):
            snap = cap.read_claude_stream(agent, kwargs["stream"])
            snap.account = cap.account_key("claude", kwargs["claude_home"])
            return snap
        mp.setattr(cap, "read_local", trust_stream)
    else:
        raise ValueError(fault)


@pytest.mark.parametrize("fault,case,reason", FAULTS, ids=[f[0] for f in FAULTS])
def test_seeded_fault_is_detected(fault, case, reason, world):
    _check(case, world)
    with pytest.MonkeyPatch.context() as mutation:
        _plant(mutation, fault)
        with pytest.raises(AssertionError, match=reason):
            _check(case, world)
    _check(case, world)
