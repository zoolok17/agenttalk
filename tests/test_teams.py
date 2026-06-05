"""Tests for the 0.11.0 multi-agent team features: groups/roles config,
roster management, audience resolution, and broadcast fan-out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import (
    Store,
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
    assert _run(["roster", "remove", "c"], root) == 0
    assert "c" not in Store(root).load_config()["agents"]


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
