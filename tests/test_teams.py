"""Tests for the 0.11.0 multi-agent team features: groups/roles config,
roster management, audience resolution, and broadcast fan-out."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk import avatars
from agenttalk import cli
from agenttalk import health as hm
from agenttalk.store import (
    ACTIVE_WITHIN_SECONDS,
    Store,
    validate_agent_name,
    validate_group_name,
    validate_groups,
    validate_roles,
)


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _run_expect_exit(argv: list[str], root: Path, code: int) -> None:
    try:
        rc = cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        actual = 0 if e.code is None else int(e.code)
    else:
        actual = int(rc)
    assert actual == code, f"expected exit {code}, got {actual}"


def _team(tmp_path: Path, agents: list[str]) -> Path:
    Store(tmp_path).init(agents)
    return tmp_path


TEAM = ["claude-dev", "codex-dev", "claude-rev", "codex-rev"]


# --------------------------------------------------------- validators

def test_validate_group_name_rejects_reserved_all() -> None:
    for name in ("all", "All", "ALL"):
        with pytest.raises(ValueError):
            validate_group_name(name)
    assert validate_group_name("reviewers") == "reviewers"


def test_validate_groups_requires_members_in_roster() -> None:
    roster = ["a", "b"]
    validate_groups({"g": ["a", "b"]}, roster)  # ok
    with pytest.raises(ValueError):
        validate_groups({"g": ["a", "ghost"]}, roster)


def test_validate_roles_bounds() -> None:
    roster = ["a"]
    validate_roles({"a": "implementer"}, roster)  # ok
    with pytest.raises(ValueError):
        validate_roles({"ghost": "x"}, roster)  # key not in roster
    with pytest.raises(ValueError):
        validate_roles({"a": "x" * 65}, roster)  # too long


@pytest.mark.parametrize("name", sorted(avatars.RESERVED_PRINCIPALS))
def test_validate_agent_name_rejects_reserved_principals(name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        validate_agent_name(name)
    with pytest.raises(ValueError, match="reserved"):
        validate_agent_name(name.upper())


# --------------------------------------------------- resolve_audience

def test_resolve_audience_group_all_and_exclude(tmp_path: Path) -> None:
    root = _team(tmp_path, TEAM)
    store = Store(root)
    store.set_group("reviewers", ["claude-rev", "codex-rev"])
    assert store.resolve_audience("reviewers") == ["claude-rev", "codex-rev"]
    # exclude drops the sender
    assert store.resolve_audience("reviewers", exclude="claude-rev") == ["codex-rev"]
    # all = whole roster minus excluded
    assert store.resolve_audience("all", exclude="claude-dev") == [
        "codex-dev", "claude-rev", "codex-rev",
    ]


def test_resolve_audience_unknown_group_raises(tmp_path: Path) -> None:
    store = Store(_team(tmp_path, TEAM))
    with pytest.raises(ValueError):
        store.resolve_audience("nope")


# ------------------------------------------------ roster mutation API

def test_add_agent_creates_cursor_and_is_idempotent(tmp_path: Path) -> None:
    root = _team(tmp_path, ["a", "b"])
    store = Store(root)
    store.add_agent("c", role="reviewer", groups=["reviewers"])
    cfg = store.load_config()
    assert "c" in cfg["agents"]
    assert cfg["roles"]["c"] == "reviewer"
    assert cfg["groups"]["reviewers"] == ["c"]
    assert (root / ".agenttalk" / "state" / "c.cursor").exists()
    # idempotent: adding again doesn't duplicate
    store.add_agent("c")
    assert store.load_config()["agents"].count("c") == 1


def test_remove_agent_purges_role_and_groups(tmp_path: Path) -> None:
    store = Store(_team(tmp_path, TEAM))
    store.set_group("reviewers", ["claude-rev", "codex-rev"])
    store.set_role("claude-rev", "reviewer")
    store.remove_agent("claude-rev")
    cfg = store.load_config()
    assert "claude-rev" not in cfg["agents"]
    assert "claude-rev" not in cfg.get("roles", {})
    assert "claude-rev" not in cfg["groups"]["reviewers"]


def test_set_role_and_group_reject_non_roster(tmp_path: Path) -> None:
    store = Store(_team(tmp_path, TEAM))
    with pytest.raises(ValueError):
        store.set_role("ghost", "x")
    with pytest.raises(ValueError):
        store.set_group("devs", ["claude-dev", "ghost"])


def test_corrupt_groups_in_config_rejected(tmp_path: Path) -> None:
    root = _team(tmp_path, ["a", "b"])
    cfg_path = root / ".agenttalk" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["groups"] = {"g": ["a", "ghost"]}  # ghost not in roster
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError):
        Store(root).load_config()


# ----------------------------------------------------- roster command

def test_roster_show_lists_agents_groups(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, TEAM)
    Store(root).set_group("reviewers", ["claude-rev", "codex-rev"])
    capsys.readouterr()
    assert _run(["roster"], root) == 0
    out = capsys.readouterr().out
    assert "claude-dev" in out and "@reviewers" in out and "@all" in out


def test_roster_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, TEAM)
    Store(root).set_role("claude-dev", "implementer")
    capsys.readouterr()
    assert _run(["roster", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"] == TEAM
    assert payload["roles"]["claude-dev"] == "implementer"


def test_roster_add_remove_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, ["a", "b"])
    assert _run(["roster", "add", "c", "--role", "reviewer", "--group", "revs"], root) == 0
    cfg = Store(root).load_config()
    assert "c" in cfg["agents"] and cfg["roles"]["c"] == "reviewer"
    assert cfg["groups"]["revs"] == ["c"]
    # #19 FR-007: bare remove is refused with a retire hint; --force removes.
    assert _run(["roster", "remove", "c"], root) == 2
    assert "c" in Store(root).load_config()["agents"]
    assert _run(["roster", "remove", "c", "--force"], root) == 0
    assert "c" not in Store(root).load_config()["agents"]


def test_roster_add_reserved_operator_rejected(tmp_path: Path) -> None:
    root = _team(tmp_path, ["alpha", "beta"])

    _run_expect_exit(["roster", "add", avatars.OPERATOR_PRINCIPAL], root, 2)
    assert avatars.OPERATOR_PRINCIPAL not in Store(root).load_config()["agents"]


def test_roster_rename_to_reserved_operator_rejected(tmp_path: Path) -> None:
    root = _team(tmp_path, ["alpha", "beta"])

    _run_expect_exit(["roster", "rename", "alpha", avatars.OPERATOR_PRINCIPAL], root, 2)
    cfg = Store(root).load_config()
    assert cfg["agents"] == ["alpha", "beta"]
    assert "retired" not in cfg


def test_avatar_cli_list_and_self_set_clear(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, ["alpha", "beta"])

    assert _run(["avatar", "list", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {item["id"]: item for item in payload["avatars"]}
    assert {"codex-dev", avatars.OPERATOR_DEFAULT_ID, "hexagon-architect"} <= set(by_id)
    assert by_id["operator"]["shape"] == ""
    assert by_id["codex-dev"]["shape"] == ""
    assert by_id["hexagon-architect"]["shape"] == "hexagon"

    assert _run(["avatar", "list"], root) == 0
    human = capsys.readouterr().out
    assert "Originals:" in human
    assert "hexagon:" in human
    assert "rounded-square:" in human
    assert "rounded-square-accessibility" in human

    assert _run(["avatar", "set", "codex-dev", "--from", "beta"], root) == 0
    cfg = Store(root).load_config()
    assert cfg["avatars"] == {"beta": "codex-dev"}

    assert _run(["avatar", "set", "hexagon-architect", "--from", "beta"], root) == 0
    cfg = Store(root).load_config()
    assert cfg["avatars"] == {"beta": "hexagon-architect"}

    assert _run(["avatar", "clear", "--from", "beta"], root) == 0
    assert "avatars" not in Store(root).load_config()


def test_avatar_cli_self_only_and_off_roster_rejected(tmp_path: Path) -> None:
    root = _team(tmp_path, ["alpha", "beta"])

    _run_expect_exit(["avatar", "set", "codex-dev", "--from", "ghost"], root, 2)
    _run_expect_exit(["avatar", "set", "codex-dev", "--from", "beta", "--for", "alpha"], root, 2)
    assert "avatars" not in Store(root).load_config()


@pytest.mark.parametrize("bad_id", [
    "../console.js",
    "avatars/claude-dev.png",
    "claude-dev.png",
    "http://example.invalid/avatar.png",
    "nope",
])
def test_avatar_cli_rejects_unallowlisted_or_pathlike_ids(tmp_path: Path, bad_id: str) -> None:
    root = _team(tmp_path, ["alpha", "beta"])

    _run_expect_exit(["avatar", "set", bad_id, "--from", "beta"], root, 2)
    assert "avatars" not in Store(root).load_config()


def test_avatar_cli_operator_set_and_clear(tmp_path: Path) -> None:
    root = _team(tmp_path, ["alpha", "beta"])

    assert _run(["avatar", "set-operator", "claude-lead"], root) == 0
    assert Store(root).load_config()["avatars"][avatars.OPERATOR_PRINCIPAL] == "claude-lead"

    assert _run(["avatar", "clear-operator"], root) == 0
    assert "avatars" not in Store(root).load_config()


# ------------------------------------------------ unique-name self-join guard

def test_agent_active_heartbeat_or_live_waiter(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["codex", "lead"])
    assert s.agent_active("lead") is False              # no heartbeat, no waiter
    s.write_heartbeat("codex")
    hb = s.read_heartbeat("codex").timestamp()
    assert s.agent_active("codex", now=hb + 1) is True              # fresh
    assert s.agent_active("codex", now=hb + ACTIVE_WITHIN_SECONDS + 5) is False  # stale
    # a FRESH live waiting marker (alive pid, within deadline) is active even
    # with NO heartbeat
    s.write_waiting("lead", {"agent": "lead", "pid": os.getpid(),
                             "deadline_epoch": hb + 1800})
    assert s.agent_active("lead", now=hb + 100) is True
    # ...but a long-EXPIRED marker (past deadline + stale_after) does NOT count,
    # even if its pid happens to still be alive (codex-reviewer-1 r1 follow-up)
    assert s.agent_active("lead", now=hb + 1800 + 99999) is False
    # a dead pid is NOT active
    s.write_waiting("lead", {"agent": "lead", "pid": 2_000_000_000,
                             "deadline_epoch": hb + 1800})
    assert s.agent_active("lead", now=hb + 100) is False


def test_agent_active_invalid_name_is_false_not_path_probe(tmp_path: Path) -> None:
    # codex-reviewer-1 r1: an unsafe name must NOT be interpolated into a state
    # file path - agent_active validates first and returns False.
    s = Store(tmp_path)
    s.init(["codex"])
    assert s.agent_active("../evil") is False
    assert s.agent_active("a/b") is False


def test_agent_active_bounds_future_heartbeat_skew(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["codex"])
    store.write_heartbeat("codex")
    heartbeat_epoch = store.read_heartbeat("codex").timestamp()

    assert store.agent_active(
        "codex",
        now=heartbeat_epoch - hm.DEFAULT_HEARTBEAT_SKEW_SECONDS - 1.0,
    ) is False
    assert store.agent_active(
        "codex",
        now=heartbeat_epoch - hm.DEFAULT_HEARTBEAT_SKEW_SECONDS,
    ) is True


def test_roster_add_unique_rejects_unsafe_name_before_probe(tmp_path: Path) -> None:
    # the CLI validates the raw name BEFORE the active probe -> usage exit 2
    # (path-traversal class never reaches the filesystem read).
    root = _team(tmp_path, ["codex"])
    _run_expect_exit(["roster", "add", "../evil", "--unique"], root, 2)


def test_suggest_unique_name_stays_within_64_chars(tmp_path: Path) -> None:
    # codex-reviewer-1 r1: a long active base must still yield an ADOPTABLE
    # suggestion (<=64 chars, passes validate_agent_name).
    from agenttalk.store import validate_agent_name
    s = Store(tmp_path)
    base = "c" * 64                      # a maximal valid base
    s.init([base])
    s.write_heartbeat(base)              # make it active so a variant is suggested
    sug = s.suggest_unique_name(base)
    assert len(sug) <= 64
    assert validate_agent_name(sug) == sug   # adoptable (does not raise)


def test_suggest_unique_name_skips_roster_and_active(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["codex", "codex-2"])           # codex-2 already a roster member
    s.write_heartbeat("codex")             # codex itself active
    # codex active + codex-2 taken -> the first FREE variant is codex-3
    assert s.suggest_unique_name("codex") == "codex-3"


def test_roster_add_unique_refuses_active_and_suggests(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, ["codex", "codex-2"])
    Store(root).write_heartbeat("codex")            # codex is ACTIVE
    # plain text refusal: exit 3 + a suggestion on stderr
    rc = _run(["roster", "add", "codex", "--unique"], root)
    assert rc == 3
    err = capsys.readouterr().err
    assert "ACTIVE identity" in err and "codex-3" in err
    # --json refusal shape
    rc = _run(["roster", "add", "codex", "--unique", "--json"], root)
    assert rc == 3
    data = json.loads(capsys.readouterr().out)
    assert data == {"refused": True, "active_holder": "codex", "suggested": "codex-3"}
    # codex was NOT re-added / no second identity created
    assert Store(root).load_config()["agents"].count("codex") == 1


def test_roster_add_unique_allows_free_name(tmp_path: Path) -> None:
    root = _team(tmp_path, ["codex"])
    assert _run(["roster", "add", "claude-dev", "--unique"], root) == 0
    assert "claude-dev" in Store(root).load_config()["agents"]


def test_roster_add_plain_rebind_active_warns_but_succeeds(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, ["codex"])
    Store(root).write_heartbeat("codex")            # active
    # plain add (idempotent) of an ACTIVE existing name -> exit 0 + warning
    assert _run(["roster", "add", "codex"], root) == 0
    err = capsys.readouterr().err
    assert "LIVE owner" in err and "--unique" in err
    assert Store(root).load_config()["agents"].count("codex") == 1
    # a plain add of a NON-active (new) name -> no warning
    assert _run(["roster", "add", "fresh-name"], root) == 0
    assert "LIVE owner" not in capsys.readouterr().err


# -------------------------------------------------------- broadcast

def _messages(root: Path) -> list[dict]:
    md = root / ".agenttalk" / "messages"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(md.glob("*.json"))]


def test_broadcast_fans_out_to_group_excluding_sender(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    root = _team(tmp_path, TEAM)
    Store(root).set_group("reviewers", ["claude-rev", "codex-rev"])
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "claude-dev", "--to-group", "reviewers",
               "--kind", "question", "-m", "review please"], root)
    assert rc == 0
    msgs = _messages(root)
    assert len(msgs) == 2
    recips = sorted(m["to"] for m in msgs)
    assert recips == ["claude-rev", "codex-rev"]
    bids = {m["meta"]["broadcast_id"] for m in msgs}
    assert len(bids) == 1  # one shared broadcast_id
    for m in msgs:
        assert m["from"] == "claude-dev"
        assert m["kind"] == "question"
        assert m["meta"]["audience"] == "reviewers"
        assert m["meta"]["request_id"] == m["meta"]["broadcast_id"]


def test_broadcast_all_excludes_sender(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, TEAM)
    capsys.readouterr()
    _run(["broadcast", "--from", "claude-dev", "--all", "-m", "fyi"], root)
    msgs = _messages(root)
    assert sorted(m["to"] for m in msgs) == ["claude-rev", "codex-dev", "codex-rev"]
    assert all(m["meta"]["audience"] == "all" for m in msgs)


def test_broadcast_print_id_outputs_b_id(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, TEAM)
    capsys.readouterr()
    _run(["broadcast", "--from", "claude-dev", "--all", "-m", "x",
          "--print-id", "--quiet"], root)
    out = capsys.readouterr().out.strip()
    assert out.startswith("b-")


def test_broadcast_unknown_group_errors(tmp_path: Path) -> None:
    root = _team(tmp_path, TEAM)
    _run_expect_exit(["broadcast", "--from", "claude-dev", "--to-group", "ghosts",
                      "-m", "x"], root, 2)


def test_broadcast_requires_a_target(tmp_path: Path) -> None:
    # neither --to-group nor --all → argparse usage error
    _run_expect_exit(["broadcast", "--from", "claude-dev", "-m", "x"], _team(tmp_path, TEAM), 2)


def test_broadcast_both_targets_mutually_exclusive(tmp_path: Path) -> None:
    _run_expect_exit(["broadcast", "--from", "claude-dev", "--all",
                      "--to-group", "reviewers", "-m", "x"], _team(tmp_path, TEAM), 2)


def test_broadcast_empty_body_errors(tmp_path: Path) -> None:
    _run_expect_exit(["broadcast", "--from", "claude-dev", "--all"], _team(tmp_path, TEAM), 2)


def test_broadcast_supplied_request_id_stays_synced_with_broadcast_id(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Regression (Codex review): a user-supplied request_id must NOT desync
    from the printed broadcast_id — broadcast owns one correlation id."""
    root = _team(tmp_path, TEAM)
    capsys.readouterr()
    _run(["broadcast", "--from", "claude-dev", "--all",
          "--meta", "request_id=custom-1", "-m", "x"], root)
    for m in _messages(root):
        assert m["meta"]["request_id"] == "custom-1"
        assert m["meta"]["broadcast_id"] == "custom-1"  # both equal — synced


def test_broadcast_print_id_reflects_supplied_id(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    root = _team(tmp_path, TEAM)
    capsys.readouterr()
    _run(["broadcast", "--from", "claude-dev", "--all",
          "--meta", "broadcast_id=b-mine", "-m", "x", "--print-id", "--quiet"], root)
    assert capsys.readouterr().out.strip() == "b-mine"


def test_broadcast_conflicting_request_and_broadcast_id_errors(tmp_path: Path) -> None:
    _run_expect_exit(
        ["broadcast", "--from", "claude-dev", "--all",
         "--meta", "request_id=x", "--meta", "broadcast_id=y", "-m", "z"],
        _team(tmp_path, TEAM), 2,
    )


def test_roster_mutators_tolerate_null_groups_roles(tmp_path: Path) -> None:
    """Fresh-review finding: an explicit `groups:null`/`roles:null` config
    loads fine, but the mutators used to `setdefault` → None → TypeError."""
    root = _team(tmp_path, ["a", "b"])
    cfg_path = root / ".agenttalk" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["roles"] = None
    cfg["groups"] = None
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    store = Store(root)
    store.set_role("a", "lead")          # must not raise TypeError
    store.set_group("g", ["a", "b"])
    store.add_agent("c", role="reviewer", groups=["g"])
    out = store.load_config()
    assert out["roles"]["a"] == "lead" and out["roles"]["c"] == "reviewer"
    assert "a" in out["groups"]["g"] and "c" in out["groups"]["g"]


# ---------------------------------------------- review-fix regressions

def test_validators_fail_closed_on_empty_roster() -> None:
    # An empty roster has no valid members/keys — reject, don't fail open.
    with pytest.raises(ValueError):
        validate_groups({"g": ["x"]}, [])
    with pytest.raises(ValueError):
        validate_roles({"x": "lead"}, [])


def test_add_agent_validation_failure_leaves_no_orphan_cursor(tmp_path: Path) -> None:
    root = _team(tmp_path, ["a", "b"])
    store = Store(root)
    with pytest.raises(ValueError):
        store.add_agent("c", role="x" * 65)  # role too long → validation fails
    cfg = store.load_config()
    assert "c" not in cfg["agents"]
    assert not (root / ".agenttalk" / "state" / "c.cursor").exists()


def test_load_config_accepts_null_groups_and_roles(tmp_path: Path) -> None:
    root = _team(tmp_path, ["a", "b"])
    cfg_path = root / ".agenttalk" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["groups"] = None
    cfg["roles"] = None
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    store = Store(root)
    store.load_config()  # must not raise on explicit null
    assert store.groups() == {} and store.roles() == {}


def test_avatar_preferences_roundtrip_and_invalid_entries_are_ignored(tmp_path: Path) -> None:
    store = Store(_team(tmp_path, ["alpha", "beta"]))
    store.set_avatar("alpha", "codex-dev")
    store.set_operator_avatar("operator")

    cfg = store.load_config()
    assert cfg["avatars"] == {"alpha": "codex-dev", avatars.OPERATOR_PRINCIPAL: "operator"}
    assert store.avatar_preferences() == cfg["avatars"]

    cfg_path = tmp_path / ".agenttalk" / "config.json"
    cfg["avatars"] = {
        "alpha": "../console.js",
        "beta": "codex-rev",
        "ghost": "claude-dev",
        avatars.OPERATOR_PRINCIPAL: "http://example.invalid/avatar.png",
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    assert Store(tmp_path).load_config()["avatars"]["alpha"] == "../console.js"
    assert Store(tmp_path).avatar_preferences() == {"beta": "codex-rev"}


def test_roster_mutators_cleanup_avatar_preferences(tmp_path: Path) -> None:
    store = Store(_team(tmp_path, ["alpha", "beta", "gamma", "delta"]))
    store.set_avatar("alpha", "claude-dev")
    store.set_avatar("beta", "claude-rev")
    store.set_avatar("gamma", "codex-dev")

    store.remove_agent("alpha")
    store.retire_agent("beta")
    store.rename_agent("gamma", "gamma-2")

    prefs = store.load_config().get("avatars", {})
    assert "alpha" not in prefs
    assert "beta" not in prefs
    assert prefs["gamma-2"] == "codex-dev"
    assert "gamma" not in prefs


def test_status_json_includes_role(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _team(tmp_path, TEAM)
    Store(root).set_role("claude-dev", "implementer")
    capsys.readouterr()
    _run(["status", "--json"], root)
    payload = json.loads(capsys.readouterr().out)
    dev = next(a for a in payload["agents"] if a["name"] == "claude-dev")
    assert dev["role"] == "implementer"


# ======================================================================
# 0.15.0 role audiences (WP01, #15)
# ======================================================================

def test_resolve_role_audience_happy_order_dedupe(tmp_path: Path) -> None:
    root = _team(tmp_path, TEAM)  # claude-dev, codex-dev, claude-rev, codex-rev
    store = Store(root)
    store.set_role("claude-rev", "reviewer")
    store.set_role("codex-rev", "reviewer")
    store.set_role("claude-dev", "implementer")
    # roster order preserved
    assert store.resolve_role_audience("reviewer") == ["claude-rev", "codex-rev"]
    # sender excluded
    assert store.resolve_role_audience("reviewer", exclude="claude-rev") == ["codex-rev"]


def test_resolve_role_audience_unknown_role_names_known(tmp_path: Path) -> None:
    import pytest
    root = _team(tmp_path, TEAM)
    store = Store(root)
    store.set_role("claude-rev", "reviewer")
    with pytest.raises(ValueError) as ei:
        store.resolve_role_audience("ghost")
    assert "reviewer" in str(ei.value)  # known roles are named


def test_resolve_role_audience_empty_after_exclude(tmp_path: Path) -> None:
    import pytest
    root = _team(tmp_path, TEAM)
    store = Store(root)
    store.set_role("claude-rev", "reviewer")
    with pytest.raises(ValueError, match="no members besides"):
        store.resolve_role_audience("reviewer", exclude="claude-rev")


def test_resolve_role_audience_no_roles_at_all(tmp_path: Path) -> None:
    import pytest
    root = _team(tmp_path, TEAM)
    store = Store(root)
    with pytest.raises(ValueError, match="unknown role"):
        store.resolve_role_audience("reviewer")
