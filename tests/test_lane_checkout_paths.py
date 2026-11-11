"""Damaged stored paths never become a cwd or permission to change a lane."""

import json
import sys

import pytest

from agenttalk import cli, ephemeral, lane_occupancy, lanes, supervisor
from agenttalk.store import Store
from agenttalk.wrapper import run as wrapper_run


@pytest.fixture
def damaged_lane(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    store.set_role("lead", "lead")
    lane = lanes.new_lane(
        "damaged",
        assignee="dev",
        assigned_by="lead",
        assigned_at="t",
        domain_id="core",
        path_subset=[],
        base_sha="a" * 40,
        target_ref="main",
        target_head_at_assign="a" * 40,
        epoch_at_assign=None,
        registry_hash_at_assign="hash",
    )
    lane.update(worktree_path=str(tmp_path / "checkout"), worktree_toplevel_canonical=str(tmp_path / "checkout"))

    def damage(field, value):
        lane[field] = value
        lanes.save_lanes(store, {"schema_version": 1, "lanes": {"damaged": lane}})
        return store

    return damage


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "\u2003"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize(
    "command",
    [
        ["workspace"],
        ["workspace", "--json"],
        ["check"],
        ["deliver", "--from", "dev"],
        ["abandon"],
        ["abandon", "--acknowledge-missing-worktree"],
        ["restamp", "--from", "lead", "--reason", "repair"],
        ["status"],
        ["status", "--json"],
    ],
    ids=[
        "workspace",
        "workspace-json",
        "check",
        "deliver",
        "abandon",
        "acknowledge",
        "restamp",
        "status",
        "status-json",
    ],
)
def test_blank_checkout_refuses_without_changing_lane(damaged_lane, capsys, field, blank, command):
    store = damaged_lane(field, blank)
    before = lanes.lanes_path(store).read_bytes()
    argv = command if command[0] == "status" else [*command, "--id", "damaged"]
    assert cli.main(["--root", str(store.root), "lane", *argv]) == 2
    output = capsys.readouterr()
    assert "damaged" in output.err and field in output.err
    assert not output.out
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("blank", ["   ", "\t"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_blank_checkout_never_reaches_wrapper(damaged_lane, monkeypatch, capsys, field, blank):
    store = damaged_lane(field, blank)
    used = []
    monkeypatch.setattr(lane_occupancy, "hold", lambda *_: used.append("occupancy"))

    def launch(*args, **kwargs):
        pytest.fail("damaged checkout reached wrapper launch")

    monkeypatch.setattr(wrapper_run, "preflight_launch_runtime", launch)
    assert (
        cli.main(
            [
                "--root",
                str(store.root),
                "wrap",
                "--for",
                "dev",
                "--cli",
                "claude",
                "--lane-id",
                "damaged",
                "--",
                sys.executable,
            ]
        )
        == 2
    )
    assert used == []
    output = capsys.readouterr()
    assert "damaged" in output.err and field in output.err


@pytest.mark.parametrize("blank", ["   ", "\t"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_blank_checkout_makes_workspace_map_unavailable(damaged_lane, capsys, field, blank):
    store = damaged_lane(field, blank)
    assert supervisor.active_ephemeral_lane_workspaces(store) is None
    output = capsys.readouterr()
    assert "damaged" in output.err and field in output.err


@pytest.mark.parametrize("blank", ["   ", "\t"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_blank_checkout_keeps_supervisor_request_queued(damaged_lane, monkeypatch, field, blank):
    store = damaged_lane(field, blank)
    marker = {"request_id": "request", "lane_id": "damaged", "scope": {}}
    # Request shape/capacity are independent of lane path validation. Record
    # whether the claim is attempted, before any roster or launch changes.
    monkeypatch.setattr(store, "read_launch_request", lambda _: marker)
    monkeypatch.setattr(ephemeral, "validate_launch_request", lambda *_: ([], {}))
    monkeypatch.setattr(ephemeral, "capacity_errors", lambda *_: [])
    claimed = []
    monkeypatch.setattr(store, "claim_launch_request", lambda *a, **k: claimed.append(True) or None)
    with pytest.raises(ephemeral.EphemeralError, match=f"damaged.*{field}"):
        supervisor.prepare_launch_request(store, {}, {}, "request", now_epoch=1)
    assert claimed == []


@pytest.mark.parametrize("blank", ["   ", "\t"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_blank_checkout_release_names_lane_and_field(damaged_lane, field, blank):
    from agenttalk import worktree_release

    store = damaged_lane(field, blank)
    row = json.loads(lanes.lanes_path(store).read_text())["lanes"]["damaged"]
    with pytest.raises(worktree_release.Refused, match=f"damaged.*{field}"):
        worktree_release._names_checkout(
            row, store.root / "other", store.root, source="lanes.json lane damaged", lane=True
        )


@pytest.mark.parametrize(
    "value,reason",
    [
        ("", "is blank"),
        (" \t ", "contains only whitespace"),
        ("relative", "is not absolute"),
        (7, "is not a string"),
    ],
)
def test_checkout_path_problem_explains_the_field(value, reason):
    assert lanes.checkout_path_problem({"worktree_path": value}) == ("worktree_path", reason)


def test_checkout_path_check_preserves_padded_absolute_values(tmp_path):
    row = {"worktree_path": str(tmp_path / "padded "), "worktree_toplevel_canonical": str(tmp_path / "canonical ")}
    before = dict(row)
    assert lanes.checkout_path_problem(row) is None
    assert row == before
    assert lanes.checkout_path_problem({}) is None
    assert lanes.checkout_path_problem({"worktree_path": None}) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_assignment_checks_before_preparation(damaged_lane, monkeypatch, capsys, field, blank):
    store = damaged_lane(field, blank)
    before = lanes.lanes_path(store).read_bytes()

    def prepare(*args, **kwargs):
        pytest.fail("damaged assignment reached preparation")

    monkeypatch.setattr(cli, "_load_domain_registry", prepare)
    assert (
        cli.main(
            [
                "--root",
                str(store.root),
                "lane",
                "assign",
                "--id",
                "damaged",
                "--force",
                "--from",
                "lead",
                "--assignee",
                "dev",
                "--domain",
                "core",
                "--base",
                "a" * 40,
                "--target",
                "main",
            ]
        )
        == 2
    )
    assert f"could not read current checkout path {field}" in capsys.readouterr().err
    assert lanes.lanes_path(store).read_bytes() == before
    assert not lanes.worktrees_root(store).exists()


def test_unreadable_registry_keeps_supervisor_denial_behavior(tmp_path):
    from test_ephemeral_reviewers import NOW, _cfg, _marker, _store

    store = _store(tmp_path)
    marker = _marker("unreadable", lane_id="damaged")
    marker["scope"]["lane_id"] = "damaged"
    store.write_launch_request(marker)
    path = lanes.lanes_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{")
    with pytest.raises(ephemeral.EphemeralError, match="invalid lane launch: lanes.json is unreadable/corrupt"):
        supervisor.prepare_launch_request(store, {}, _cfg(), "unreadable", now_epoch=NOW)
    assert store.read_launch_request("unreadable") is None
    archived = json.loads((store.launch_requests_archive_dir / "unreadable.json").read_text())
    assert archived["terminal_state"] == ephemeral.STATE_DENIED
