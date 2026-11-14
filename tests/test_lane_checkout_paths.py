"""Damaged checkout paths are refused or reported without being used as folders."""

import json
import ntpath
import os
import re
import sys
from pathlib import Path

import pytest

from agenttalk import cli, ephemeral, lane_occupancy, lanes, supervisor
from agenttalk.store import Store
from agenttalk.wrapper import run as wrapper_run


def _committed_unpublished_delivery(tmp_path, monkeypatch, canonical=None, relative_worktree=False):
    """A delivery whose committed final exists but whose publication marker was never saved.

    `canonical` models a delivery made by an OLDER version, which did not look at the saved checkout paths: the row's
    `worktree_toplevel_canonical` is already that value (a string, or a function of the checkout folder) BEFORE the
    delivery, so the signed snapshot and the final's lane fingerprint both carry the damage, and the path rule is
    switched off for that one delivery."""
    from test_lanes import _assign_worktree, _commit, _committed_artifacts, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane_id = "committed-damage"
    lane = _assign_worktree(root, base, lane_id)
    worktree = Path(lane["worktree_path"])
    head = _commit(worktree, "src/core/a.py", "base\ncommitted delivery\n")
    store = Store(root)
    real_save = lanes.save_lanes
    if canonical is not None:
        data = lanes.load_lanes(store)
        data["lanes"][lane_id]["worktree_toplevel_canonical"] = (
            canonical(worktree) if callable(canonical) else canonical
        )
        real_save(store, data)

    if relative_worktree:  # a hand-written row: the checkout named relative to the folder the delivery runs from
        data = lanes.load_lanes(store)
        data["lanes"][lane_id]["worktree_path"] = os.path.relpath(worktree, os.getcwd())
        real_save(store, data)

    def fail_publication_marker(target_store, data):
        if data["lanes"][lane_id].get("publish_pending") is False:
            raise OSError("injected publication marker save failure")
        return real_save(target_store, data)

    with monkeypatch.context() as fault:
        fault.setattr(lanes, "save_lanes", fail_publication_marker)
        if canonical is not None:
            fault.setattr(lanes, "require_checkout_paths", lambda *args, **kwargs: None)  # the older version had none
        assert _run_raw(["lane", "deliver", "--id", lane_id, "--from", "dev"], root) == 2
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert isinstance(saved["publish_pending"], dict)
    assert saved["cleanup_pending"] is True
    finals = _committed_artifacts(store, lane_id)
    assert len(finals) == 1
    lanes.validate_delivery_artifact(finals[0], lane_id=lane_id, head_sha=head, store=store)
    with pytest.raises(lanes.LaneError, match="pending"):
        lanes.validate_delivery_artifact(
            finals[0], lane_id=lane_id, head_sha=head, store=store, require_live_marker=True
        )
    return store, lane_id, worktree, head, finals[0]


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("damage", ["", " \t ", 7])
def test_committed_delivery_with_damaged_path_publishes_without_cleanup(tmp_path, monkeypatch, capsys, field, damage):
    from test_lanes import _committed_artifacts, _git_rc

    store, lane_id, worktree, head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    original_artifact = artifact.read_bytes()
    original_file = (worktree / "src/core/a.py").read_bytes()
    data = lanes.load_lanes(store)
    data["lanes"][lane_id][field] = damage
    lanes.save_lanes(store, data)
    capsys.readouterr()
    real_verify = cli._verify_lane_worktree
    checked = []

    def verify_snapshot(target_store, lane, **kwargs):
        assert lanes.checkout_path_problem(lane) is None
        assert lane["worktree_path"] == str(worktree)
        checked.append(True)
        return real_verify(target_store, lane, **kwargs)

    def no_cleanup(*args, **kwargs):
        pytest.fail("publication recovery used the damaged checkout for cleanup")

    with monkeypatch.context() as guard:
        guard.setattr(cli, "_verify_lane_worktree", verify_snapshot)
        guard.setattr(cli, "_lane_deliver_cleanup", no_cleanup)
        guard.setattr(cli, "_lane_entry_present", no_cleanup)
        guard.setattr(cli, "_git_write", no_cleanup)
        assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0

    output = capsys.readouterr()
    assert "cleanup pending" in output.out
    assert field in output.err and lane_id in output.err
    assert checked  # The existing terminal checks still validate the persisted healthy snapshot.
    current = lanes.load_lanes(store)["lanes"][lane_id]
    assert current["publish_pending"] is False
    assert current["cleanup_pending"] is True
    assert current[field] == damage
    assert not lanes.delivery_recovery_required(current)
    assert artifact.read_bytes() == original_artifact
    assert _committed_artifacts(store, lane_id) == [artifact]
    assert (worktree / "src/core/a.py").read_bytes() == original_file
    lanes.validate_delivery_artifact(
        artifact, lane_id=lane_id, head_sha=head, store=store, require_isolation=True, require_live_marker=True
    )

    # Model the person removing the known original fixture checkout, never the damaged spelling.
    removed = _git_rc(store.root, "worktree", "remove", "--", str(worktree))
    assert removed.returncode == 0, removed.stderr
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", lane_id, "--acknowledge-missing-worktree"]) == 0
    )
    retired = lanes.load_lanes(store)["lanes"][lane_id]
    assert retired["status"] == lanes.STATUS_ABANDONED
    assert retired["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


def test_committed_delivery_with_healthy_path_resumes_cleanup(tmp_path, monkeypatch):
    store, lane_id, worktree, head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    original_artifact = artifact.read_bytes()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    current = lanes.load_lanes(store)["lanes"][lane_id]
    assert current["publish_pending"] is False
    assert current["cleanup_pending"] is False
    assert not worktree.exists()
    assert artifact.read_bytes() == original_artifact
    lanes.validate_delivery_artifact(
        artifact, lane_id=lane_id, head_sha=head, store=store, require_isolation=True, require_live_marker=True
    )


@pytest.mark.parametrize("evidence", ["missing", "invalid"])
def test_committed_delivery_with_damaged_path_requires_existing_valid_final(tmp_path, monkeypatch, evidence):
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    if evidence == "missing":
        artifact.unlink()
    else:
        artifact.write_text("{}")
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["worktree_path"] = "\t"
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 2
    assert lanes.lanes_path(store).read_bytes() == before
    if evidence == "missing":
        assert not artifact.exists()
    else:
        assert artifact.read_text() == "{}"


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
        ["restamp", "--from", "lead", "--reason", "repair"],
    ],
    ids=[
        "workspace",
        "workspace-json",
        "check",
        "deliver",
        "abandon",
        "restamp",
    ],
)
def test_blank_checkout_refuses_without_changing_lane(damaged_lane, capsys, field, blank, command):
    store = damaged_lane(field, blank)
    before = lanes.lanes_path(store).read_bytes()
    argv = command if command[0] == "status" else [*command, "--id", "damaged"]
    assert cli.main(["--root", str(store.root), "lane", *argv]) == 2
    output = capsys.readouterr()
    assert "damaged" in output.err and field in output.err
    assert "--acknowledge-missing-worktree" in output.err
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
def test_blank_checkout_workspace_map_keeps_healthy_lanes(damaged_lane, capsys, field, blank):
    store = damaged_lane(field, blank)
    data = lanes.load_lanes(store)
    healthy = dict(
        data["lanes"]["damaged"],
        lane_id="healthy",
        worktree_path=str(store.root / "healthy"),
        worktree_toplevel_canonical=str(store.root / "healthy"),
    )
    data["lanes"]["healthy"] = healthy
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    assert supervisor.active_ephemeral_lane_workspaces(store) == {"healthy": str(store.root / "healthy")}
    output = capsys.readouterr()
    assert "damaged" in output.err and field in output.err
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("blank", ["", "   ", "\t", 7])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("binding", ["lane_id", "scope"])
def test_queued_damaged_lane_is_denied_then_can_be_retired(damaged_lane, capsys, field, blank, binding):
    from test_ephemeral_reviewers import NOW, _cfg, _marker, _report

    store = damaged_lane(field, blank)
    marker = _marker("request")
    if binding == "lane_id":
        marker["lane_id"] = "damaged"
    else:
        marker["scope"]["lane_id"] = "damaged"
    store.write_launch_request(marker)
    config = _cfg()
    state = {}
    plan = supervisor.plan_actions(_report(marker), state, config, now_epoch=NOW)
    assert plan["launch_requests"]["request"]["action"] == ephemeral.ACTION_LAUNCH

    with pytest.raises(ephemeral.EphemeralError, match=f"damaged.*{field}"):
        supervisor.prepare_launch_request(store, state, config, "request", now_epoch=NOW)

    assert store.read_launch_request("request") is None
    archived_path = store.launch_requests_archive_dir / "request.json"
    archived = json.loads(archived_path.read_text())
    assert archived["terminal_state"] == ephemeral.STATE_DENIED
    assert "damaged" in archived["reason"] and field in archived["reason"]
    assert store.load_config()["agents"] == ["lead", "dev"]
    assert not state
    next_report = _report()
    next_report["launch_requests"] = store.list_launch_requests()
    assert supervisor.plan_actions(next_report, state, config, now_epoch=NOW + 1)["launch_requests"] == {}
    assert list(store.launch_requests_archive_dir.glob("*.json")) == [archived_path]
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 0
    )
    capsys.readouterr()
    lane = lanes.load_lanes(store)["lanes"]["damaged"]
    assert lane["status"] == lanes.STATUS_ABANDONED
    assert lane["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


def test_healthy_lane_launch_still_prepares_request(damaged_lane):
    from test_ephemeral_reviewers import NOW, _cfg, _marker

    store = damaged_lane("worktree_path", "unused")
    data = lanes.load_lanes(store)
    lane = data["lanes"]["damaged"]
    lane["worktree_path"] = str(store.root / "checkout")
    (store.root / "checkout").mkdir()
    lanes.save_lanes(store, data)
    shim = store.dir / "bin" / "agenttalk.cmd"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_bytes(supervisor.agenttalk_shim(sys.executable).encode("utf-8"))
    store.write_launch_request(_marker("healthy-request", lane_id="damaged"))
    state = {}

    spec = supervisor.prepare_launch_request(store, state, _cfg(), "healthy-request", now_epoch=NOW)

    assert spec["lane_id"] == "damaged"
    assert spec["workspace_path"] == str(store.root / "checkout")
    assert spec["cwd"] == str(store.root / "checkout")
    marker = store.read_launch_request("healthy-request")
    assert marker["state"] == ephemeral.STATE_REQUESTED
    agent = spec["agent"]
    assert agent in store.load_config()["agents"]
    assert store.messages_for(agent)[0].kind == "review-request"
    assert "healthy-request" in state["ephemeral_reviewers"]["active"]
    assert not list(store.launch_requests_archive_dir.glob("*.json"))


@pytest.mark.parametrize("blank", ["   ", "\t"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_blank_checkout_release_names_lane_and_field(damaged_lane, field, blank):
    from agenttalk import worktree_release

    store = damaged_lane(field, blank)
    row = json.loads(lanes.lanes_path(store).read_text())["lanes"]["damaged"]
    with pytest.raises(worktree_release.Refused, match=f"damaged.*{field}"):
        worktree_release._names_checkout(
            row, store.root / "other", store.root, lane_id="damaged", source="lanes.json lane damaged", lane=True
        )


@pytest.mark.parametrize(
    "value,reason",
    [
        ("", "is blank"),
        (" \t ", "contains only whitespace"),
        (7, "is not a string"),
    ],
)
def test_checkout_path_problem_explains_the_field(value, reason):
    assert lanes.checkout_path_problem({"worktree_path": value}) == ("worktree_path", reason)


@pytest.mark.parametrize("identifier", ["bad\nFORGED", "bad;command"])
def test_checkout_recovery_does_not_print_an_unusable_command_id(identifier):
    advice = lanes.checkout_path_recovery(lane_id=identifier)
    assert identifier not in advice
    assert len(advice.splitlines()) == 1
    assert "lane abandon --id <lane-id> --acknowledge-missing-worktree" in advice


def test_checkout_path_check_preserves_padded_absolute_values(tmp_path):
    row = {"worktree_path": str(tmp_path / "padded "), "worktree_toplevel_canonical": str(tmp_path / "canonical ")}
    before = dict(row)
    assert lanes.checkout_path_problem(row) is None
    assert row == before
    assert lanes.checkout_path_problem({}) is None
    assert lanes.checkout_path_problem({"worktree_path": None}) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t", 7])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("status", [lanes.STATUS_ACTIVE, lanes.STATUS_ABANDONED])
def test_status_marks_damage_and_lists_healthy_lanes(damaged_lane, capsys, field, blank, json_output, status):
    store = damaged_lane(field, blank)
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["status"] = status
    healthy = dict(
        data["lanes"]["damaged"],
        lane_id="healthy",
        worktree_path=str(store.root / "healthy"),
        worktree_toplevel_canonical=str(store.root / "healthy"),
    )
    data["lanes"]["healthy"] = healthy
    healthy["status"] = lanes.STATUS_ACTIVE
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    args = ["--json"] if json_output else []
    assert cli.main(["--root", str(store.root), "lane", "status", *args]) == 0
    output = capsys.readouterr()
    if json_output:
        rows = {row["lane_id"]: row for row in json.loads(output.out)}
        assert rows["healthy"] == healthy
        assert set(rows["damaged"]) == set(data["lanes"]["damaged"]) | {"checkout_path_error"}
        diagnostic = rows["damaged"]["checkout_path_error"]
    else:
        assert "healthy" in output.out
        diagnostic = output.out
    assert "damaged" in diagnostic and field in diagnostic
    assert "--acknowledge-missing-worktree" in diagnostic
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("blank", ["", "   ", "\t", 7])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_acknowledgement_checks_surviving_path_without_touching_content(damaged_lane, monkeypatch, field, blank):
    store = damaged_lane(field, blank)

    def folder_use(*args, **kwargs):
        pytest.fail("record-only acknowledgement used a checkout")

    checked = []

    def present(path):
        assert path == store.root / "checkout"
        checked.append(path)
        return False

    monkeypatch.setattr(cli, "_lane_entry_present", present)
    monkeypatch.setattr(cli, "_verify_lane_worktree", folder_use)
    monkeypatch.setattr(cli, "_git_write", folder_use)
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 0
    )
    row = lanes.load_lanes(store)["lanes"]["damaged"]
    assert row["status"] == lanes.STATUS_ABANDONED
    assert row["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED
    assert row[field] == blank
    assert checked == [store.root / "checkout"]
    assert not cli._lane_branch_gc_allowed(row)[0]


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("json_output", [False, True])
def test_acknowledged_damage_is_retired_from_status(damaged_lane, capsys, field, json_output):
    store = damaged_lane(field, "\t")
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 0
    )
    capsys.readouterr()
    before = lanes.lanes_path(store).read_bytes()
    options = ["--json"] if json_output else []
    assert cli.main(["--root", str(store.root), "lane", "status", *options]) == 0
    output = capsys.readouterr()
    if json_output:
        assert json.loads(output.out) == []
    else:
        assert output.out.strip() == "active lanes: none"
    assert not output.err
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("path_field", ["cwd", "launch_cwd", "workspace_path"])
@pytest.mark.parametrize("matching", [False, True])
def test_damaged_exit_compares_surviving_path_with_supervised_seats(damaged_lane, capsys, field, path_field, matching):
    store = damaged_lane(field, "\t")
    folder = store.root / ("checkout" if matching else "unrelated-seat")
    (store.dir / "supervisor-state.json").write_text(json.dumps({"agents": {"other-seat": {path_field: str(folder)}}}))
    before = lanes.lanes_path(store).read_bytes()
    assert cli.main(
        ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    ) == (2 if matching else 0)
    output = capsys.readouterr()
    if matching:
        assert "other-seat" in output.err
        assert lanes.lanes_path(store).read_bytes() == before
    else:
        assert not output.err
        assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ABANDONED


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("matching", [False, True])
def test_damaged_exit_compares_surviving_path_with_launch_markers(damaged_lane, monkeypatch, field, matching):
    store = damaged_lane(field, "\t")
    marker = {
        "request_id": "other",
        "lane_id": "other",
        "state": "launched",
        "workspace_path": str(store.root / ("checkout" if matching else "unrelated-seat")),
    }
    monkeypatch.setattr(cli, "_lane_launch_markers", lambda _: ([marker], None))
    before = lanes.lanes_path(store).read_bytes()
    assert cli.main(
        ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    ) == (2 if matching else 0)
    if matching:
        assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("embedded_id", ["otherid", "damaged"], ids=["different-embedded-id", "same-embedded-id"])
def test_damaged_exit_checks_launches_by_the_registry_lane_id(damaged_lane, capsys, embedded_id):
    """A queued launch request is bound to the lane's registry key ("damaged"). The `lane_id` written inside the
    lane's own record is not what the acknowledgement may trust: with a different one, the request was missed,
    the lane was retired (exit 0) and the request stayed queued."""
    store = damaged_lane("worktree_path", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["lane_id"] = embedded_id
    lanes.save_lanes(store, data)
    store.launch_requests_dir.mkdir(parents=True, exist_ok=True)
    request = store.launch_requests_dir / "lr-damaged.json"
    request.write_text(json.dumps({"request_id": "lr-damaged", "state": "queued", "lane_id": "damaged"}))
    before = lanes.lanes_path(store).read_bytes()
    assert not (store.root / "checkout").exists()  # the checkout really is gone
    code = cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "active or pending launch" in err and "lr-damaged" in err and "'queued'" in err
    assert "nothing was changed" in err
    assert lanes.lanes_path(store).read_bytes() == before
    assert json.loads(request.read_text())["state"] == "queued"


@pytest.mark.parametrize("source", ["supervisor", "marker"])
def test_no_surviving_path_explains_how_to_stop_supervised_seats(damaged_lane, capsys, monkeypatch, source):
    store = damaged_lane("worktree_path", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_toplevel_canonical"] = " "
    lanes.save_lanes(store, data)
    if source == "supervisor":
        (store.dir / "supervisor-state.json").write_text(
            json.dumps({"agents": {"other": {"cwd": str(store.root / "seat")}}})
        )
    else:
        marker = {
            "request_id": "other",
            "lane_id": "other",
            "state": "launched",
            "workspace_path": str(store.root / "seat"),
        }
        monkeypatch.setattr(cli, "_lane_launch_markers", lambda _: ([marker], None))
    before = lanes.lanes_path(store).read_bytes()
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 2
    )
    assert "stop the supervised seats first" in capsys.readouterr().err
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize(
    "obstacle", ["occupied", "publication", "launch", "unknown-folder", "supervisor-folder", "corrupt-state"]
)
def test_damaged_acknowledgement_keeps_other_safeguards(damaged_lane, monkeypatch, capsys, obstacle):
    import contextlib

    store = damaged_lane("worktree_path", "\t")
    if obstacle == "occupied":

        @contextlib.contextmanager
        def occupied(*args):
            yield lane_occupancy.IN_USE

        monkeypatch.setattr(lane_occupancy, "cleanup_claim", occupied)
    elif obstacle == "publication":
        monkeypatch.setattr(lanes, "delivery_recovery_required", lambda _: True)
    elif obstacle in {"launch", "unknown-folder"}:
        marker = {"request_id": "busy", "lane_id": "damaged", "state": "launched"}
        if obstacle == "unknown-folder":
            marker["lane_id"] = "other"
            marker["workspace_path"] = str(store.root / "checkout")
        monkeypatch.setattr(cli, "_lane_launch_markers", lambda _: ([marker], None))
    elif obstacle == "supervisor-folder":
        (store.dir / "supervisor-state.json").write_text(
            json.dumps({"agents": {"dev": {"cwd": str(store.root / "checkout")}}})
        )
    else:
        (store.dir / "supervisor-state.json").write_text("{")
    before = lanes.lanes_path(store).read_bytes()
    assert cli.main(
        ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    ) in {2, 3}
    assert capsys.readouterr().err
    assert lanes.lanes_path(store).read_bytes() == before


def test_supervisor_rechecks_damage_after_claim(damaged_lane, monkeypatch):
    store = damaged_lane("worktree_path", "usable-relative-path")
    marker = {"request_id": "request", "lane_id": "damaged", "scope": {}}
    monkeypatch.setattr(store, "read_launch_request", lambda _: marker)
    monkeypatch.setattr(ephemeral, "validate_launch_request", lambda *_: ([], {}))
    monkeypatch.setattr(ephemeral, "capacity_errors", lambda *_: [])
    archived = []
    monkeypatch.setattr(store, "archive_launch_request", lambda *args: archived.append(args))

    def claim(*args, **kwargs):
        data = lanes.load_lanes(store)
        data["lanes"]["damaged"]["worktree_path"] = "\t"
        lanes.save_lanes(store, data)
        return marker

    monkeypatch.setattr(store, "claim_launch_request", claim)
    with pytest.raises(ephemeral.EphemeralError, match="invalid lane launch:.*worktree_path"):
        supervisor.prepare_launch_request(store, {}, {}, "request", now_epoch=1)
    assert archived and archived[0][1]["terminal_state"] == ephemeral.STATE_DENIED


def test_acknowledgement_handles_two_damaged_fields_and_an_unrelated_marker(damaged_lane, monkeypatch):
    store = damaged_lane("worktree_path", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_toplevel_canonical"] = " "
    lanes.save_lanes(store, data)
    marker = {"request_id": "other", "lane_id": "other", "state": "launched"}
    monkeypatch.setattr(cli, "_lane_launch_markers", lambda _: ([marker], None))

    def folder_use(*args, **kwargs):
        pytest.fail("acknowledgement resolved a damaged path")

    monkeypatch.setattr(lanes, "canonical_host_path_checked", folder_use)
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 0
    )


ADVICE = "real absolute folder"
REPAIR = "Repair worktree_path in lanes.json lane"


@pytest.mark.parametrize("case", ["gone", "exists", "used", "unresolved"])
def test_a_relative_worktree_path_is_never_acknowledged_missing(damaged_lane, capsys, monkeypatch, case):
    """The folder a caller-relative path names depends on where the command that wrote it ran, and that cannot be
    recovered, so `--acknowledge-missing-worktree` refuses for every state of the folder (nothing is recorded) and
    says to repair `worktree_path` to the real absolute folder first."""
    store = damaged_lane("worktree_toplevel_canonical", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_path"] = ".worktrees/relative"
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    target = store.root / ".worktrees/relative"
    if case != "unresolved":
        target.parent.mkdir()
    elsewhere = store.root / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    if case == "exists":
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("keep")
    elif case == "used":
        (store.dir / "supervisor-state.json").write_text(json.dumps({"agents": {"dev": {"cwd": str(target)}}}))
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 2
    )
    output = capsys.readouterr()
    assert (
        "is not absolute" in output.err and ADVICE in output.err and REPAIR in output.err and "'damaged'" in output.err
    )
    assert lanes.lanes_path(store).read_bytes() == before  # nothing was recorded
    if case == "exists":
        assert (target / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific relative path spellings")
@pytest.mark.parametrize("value", ["C:relative", r"\rooted"])
def test_damaged_exit_keeps_ambiguous_windows_paths_refused(damaged_lane, capsys, value):
    store = damaged_lane("worktree_toplevel_canonical", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_path"] = value
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 2
    )
    assert "is not absolute, so whether" in capsys.readouterr().err
    assert lanes.lanes_path(store).read_bytes() == before


def test_real_relative_assignment_round_trip(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    from agenttalk import worktree_release
    from test_lanes import _branch, _repo

    root, base = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    prefix = ["--root", str(root), "lane"]
    assert (
        cli.main(
            [
                *prefix,
                "assign",
                "--id",
                "relative",
                "--from",
                "lead",
                "--assignee",
                "dev",
                "--domain",
                "core",
                "--base",
                base,
                "--target",
                _branch(root),
                "--path",
                "src/core",
                "--worktrees-root",
                ".worktrees",
            ]
        )
        == 0
    )
    store = Store(root)
    row = lanes.load_lanes(store)["lanes"]["relative"]
    assert not Path(row["worktree_path"]).is_absolute()
    capsys.readouterr()
    assert cli.main([*prefix, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [row]
    assert supervisor.active_ephemeral_lane_workspaces(store) == {}  # legacy map omits relative spellings
    assert cli.main([*prefix, "workspace", "--id", "relative"]) == 0
    assert capsys.readouterr().out.strip() == row["worktree_path"]
    assert cli.main([*prefix, "check", "--id", "relative"]) == 0
    assert "GO" in capsys.readouterr().out
    assert worktree_release._names_checkout(row, root / row["worktree_path"], root, lane_id="relative", lane=True)
    assert not worktree_release._names_checkout(row, root / "unrelated", root, lane_id="relative", lane=True)
    assert cli.main([*prefix, "abandon", "--id", "relative", "--acknowledge-missing-worktree"]) == 2
    assert "is not absolute, so whether" in capsys.readouterr().err
    assert cli.main([*prefix, "abandon", "--id", "relative"]) == 0
    assert lanes.load_lanes(store)["lanes"]["relative"]["status"] == lanes.STATUS_ABANDONED
    assert not (root / row["worktree_path"]).exists()


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


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_damaged_acknowledgement_refuses_a_surviving_real_folder(damaged_lane, capsys, field):
    store = damaged_lane(field, "\t")
    checkout = store.root / "checkout"
    checkout.mkdir()
    evidence = checkout / "keep.txt"
    evidence.write_text("unfinished work")
    before = lanes.lanes_path(store).read_bytes()

    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 2
    )
    assert "still exists" in capsys.readouterr().err
    assert lanes.lanes_path(store).read_bytes() == before
    assert evidence.read_text() == "unfinished work"


def test_retired_damaged_lane_assignment_advises_a_new_id(damaged_lane, capsys):
    store = damaged_lane("worktree_path", "\t")
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 0
    )
    capsys.readouterr()
    before = lanes.lanes_path(store).read_bytes()
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
    advice = capsys.readouterr().err
    assert "use a new lane id" in advice
    assert "--acknowledge-missing-worktree" not in advice
    assert lanes.lanes_path(store).read_bytes() == before


def test_acknowledgement_help_names_damaged_paths(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["lane", "abandon", "--help"])
    assert stopped.value.code == 0
    assert "damaged checkout path" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_wrap_rechecks_checkout_damage_after_occupancy(damaged_lane, monkeypatch, capsys, field):
    store = damaged_lane(field, "valid-relative-path")
    if field == "worktree_toplevel_canonical":  # only worktree_path may be relative; a canonical path is absolute
        data = lanes.load_lanes(store)
        data["lanes"]["damaged"][field] = str(store.root / "valid-canonical")
        lanes.save_lanes(store, data)
    held = []

    def hold(*args):
        held.append(True)
        data = lanes.load_lanes(store)
        data["lanes"]["damaged"][field] = "\t"
        lanes.save_lanes(store, data)

    def launch(*args, **kwargs):
        pytest.fail("wrap used the checkout damaged after occupancy")

    monkeypatch.setattr(lane_occupancy, "hold", hold)
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
    assert held == [True]
    refusal = capsys.readouterr().err
    assert field in refusal and "--acknowledge-missing-worktree" in refusal


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("value", ["\t", 7])
def test_release_checkout_damage_explains_recovery(damaged_lane, field, value):
    from agenttalk import worktree_release

    store = damaged_lane(field, value)
    row = lanes.load_lanes(store)["lanes"]["damaged"]
    with pytest.raises(worktree_release.Refused) as stopped:
        worktree_release._names_checkout(
            row, store.root / "other", store.root, lane_id="damaged", source="lanes.json lane damaged", lane=True
        )
    assert field in str(stopped.value)
    assert "lane abandon --id damaged --acknowledge-missing-worktree" in str(stopped.value)


def test_workspace_damage_warning_is_once_per_lane_and_store(damaged_lane, monkeypatch, capsys):
    store = damaged_lane("worktree_path", "\t")
    monkeypatch.setattr(supervisor, "_workspace_damage_warned", set(), raising=False)
    before = lanes.lanes_path(store).read_bytes()
    assert supervisor.active_ephemeral_lane_workspaces(store) == {}
    first = capsys.readouterr().err
    assert "damaged" in first and "worktree_path" in first
    assert supervisor.active_ephemeral_lane_workspaces(Store(store.root)) == {}
    assert capsys.readouterr().err == ""
    data = lanes.load_lanes(store)
    data["lanes"]["another"] = dict(data["lanes"]["damaged"], lane_id="another")
    lanes.save_lanes(store, data)
    assert supervisor.active_ephemeral_lane_workspaces(store) == {}
    second = capsys.readouterr().err
    assert "another" in second
    assert "lane 'damaged'" not in second
    data["lanes"].pop("another")
    lanes.save_lanes(store, data)
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
@pytest.mark.parametrize("acknowledge_first", [False, True])
def test_both_abandon_refusals_offer_repair_without_a_command_circle(damaged_lane, capsys, field, acknowledge_first):
    store = damaged_lane(field, " \t")
    checkout = store.root / "checkout"
    checkout.mkdir()
    evidence = checkout / "keep.txt"
    evidence.write_text("unfinished work")
    before = lanes.lanes_path(store).read_bytes()
    command = ["--root", str(store.root), "lane", "abandon", "--id", "damaged"]

    for acknowledge in (acknowledge_first, not acknowledge_first):
        assert cli.main(command + (["--acknowledge-missing-worktree"] if acknowledge else [])) == 2
        refusal = capsys.readouterr().err
        assert "damaged" in refusal and field in refusal
        assert "repair" in refusal and "lanes.json" in refusal
        assert "before retrying plain `lane abandon`" in refusal
        assert "without the option removes a worktree normally" not in refusal
        if acknowledge:
            assert repr(str(checkout)) in refusal and "still exists" in refusal
        assert lanes.lanes_path(store).read_bytes() == before
        assert evidence.read_text() == "unfinished work"


@pytest.mark.parametrize("json_output", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize("damaged_path", [False, True], ids=["valid-path", "damaged-path"])
@pytest.mark.parametrize("bucket", ["active", "recovery", "abandoned"])
def test_status_without_embedded_lane_id_is_read_only_and_lists_each_row_once(
    damaged_lane, capsys, json_output, damaged_path, bucket
):
    store = damaged_lane("worktree_path", "\t" if damaged_path else "relative-checkout")
    data = lanes.load_lanes(store)
    row = data["lanes"]["damaged"]
    healthy = dict(
        row,
        lane_id="healthy",
        worktree_path="healthy-checkout",
        worktree_toplevel_canonical=str(store.root / "healthy-checkout"),  # a canonical path is always absolute
    )
    normal_recovery = dict(
        healthy,
        lane_id="healthy-recovery",
        status=lanes.STATUS_DELIVERED,
        publish_pending=False,
        cleanup_pending=True,
    )
    data["lanes"]["healthy"] = healthy
    data["lanes"]["healthy-recovery"] = normal_recovery
    del row["lane_id"]
    row["assignee"] = "missing-id-seat"
    if bucket == "recovery":
        row.update(status=lanes.STATUS_DELIVERED, publish_pending=False, cleanup_pending=True)
    elif bucket == "abandoned":
        row["status"] = lanes.STATUS_ABANDONED
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()

    args = ["--json"] if json_output else []
    assert cli.main(["--root", str(store.root), "lane", "status", *args]) == 0
    output = capsys.readouterr()
    assert not output.err
    if json_output:
        rows = json.loads(output.out)
        assert next(r for r in rows if r.get("lane_id") == "healthy") == healthy
        assert next(r for r in rows if r.get("lane_id") == "healthy-recovery") == {
            **normal_recovery,
            "recovery_state": "cleanup_pending",
            "recovery_detail": "committed publication is waiting for worktree cleanup",
        }
        missing = [r for r in rows if r.get("assignee") == "missing-id-seat"]
        visible = bucket != "abandoned" or damaged_path
        assert len(missing) == int(visible)
        assert len(rows) == 2 + int(visible)
        if visible:
            shown = missing[0]
            assert "lane_id" not in shown
            if damaged_path:
                assert "damaged" in shown["checkout_path_error"]
                assert "worktree_path" in shown["checkout_path_error"]
            else:
                assert "checkout_path_error" not in shown
            if bucket == "recovery":
                assert shown["recovery_state"] == ("damaged checkout" if damaged_path else "cleanup_pending")
    else:
        assert output.out.count("damaged checkout:") == int(damaged_path)
        assert output.out.count("?: missing-id-seat @") == int(bucket == "active")
        if bucket == "recovery":
            diagnosis = "damaged checkout" if damaged_path else "cleanup_pending"
            assert output.out.count(f"?: {diagnosis} -") == 1
        assert output.out.count("  healthy:") == 1
        assert output.out.count("  healthy-recovery:") == 1
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("row", [[], ["not", "a", "row"], "text", 7], ids=["empty-list", "list", "string", "number"])
def test_restamp_refuses_a_lane_row_that_is_not_an_object_without_a_traceback(damaged_lane, capsys, row):
    """On master a row that is a list, string or number reached restamp's own shape check and was refused with
    exit 2; the checkout check that now runs first must not turn that into an AttributeError."""
    store = damaged_lane("worktree_path", "unused")
    path = lanes.lanes_path(store)
    path.write_text(json.dumps({"schema_version": 1, "lanes": {"damaged": row}}))
    before = path.read_bytes()
    code = cli.main(
        ["--root", str(store.root), "lane", "restamp", "--id", "damaged", "--from", "lead", "--reason", "repair"]
    )
    err = capsys.readouterr().err
    assert code == 2, err
    assert "lane record has an unknown shape (lane); nothing was changed" in err
    assert "Traceback" not in err and "AttributeError" not in err
    assert path.read_bytes() == before


@pytest.mark.parametrize("row", [[], "text", 7, None], ids=["list", "string", "number", "none"])
def test_checkout_checks_leave_a_row_that_is_not_an_object_to_the_callers_own_shape_check(row):
    assert lanes.checkout_path_problem(row) is None
    lanes.require_checkout_paths(row, lane_id="damaged", root=Path("."))  # raises nothing; no AttributeError


def test_an_empty_registry_key_gets_the_placeholder_not_another_lanes_id():
    """The embedded `lane_id` can name another valid lane. An explicitly given key, even an empty one, is never
    replaced by it, so the hint is never a runnable abandon command for a different lane."""
    row = {"lane_id": "other-lane", "worktree_path": "\t", "worktree_toplevel_canonical": "\t"}
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row, lane_id="", root=Path("."))
    text = str(caught.value)
    assert "lane abandon --id <lane-id> --acknowledge-missing-worktree" in text
    assert "other-lane" not in text


# ======================================================================================================================
# #771 systematic round: every recovery hint and checkout decision uses the registry key, and a saved canonical
# checkout path must be absolute. Row key "bad", the id written inside the row "other" (a separate healthy lane).
# ======================================================================================================================

BAD, OTHER = "bad", "other"
RELATIVE_CANONICALS = ["absent-relative-canonical", "./dot/relative", "sub/dir/checkout"]
if os.name == "nt":
    RELATIVE_CANONICALS += ["C:drive-relative", "\\rooted-without-drive"]
ACK_HINT = "--acknowledge-missing-worktree"


def _hint(key: str) -> str:
    return f"lane abandon --id {key} {ACK_HINT}"


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    """A store with a healthy lane "other" and a lane registered under `key` whose record says `embedded`."""
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    store.set_role("lead", "lead")

    def make(*, key=BAD, embedded=OTHER, **fields):
        row = lanes.new_lane(
            "seed",
            assignee="bad-seat",
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
        checkout = str(tmp_path / "bad-checkout")
        row.update(worktree_path=checkout, worktree_toplevel_canonical=checkout)
        row.update(fields)
        if embedded is _MISSING:
            del row["lane_id"]
        else:
            row["lane_id"] = embedded
        healthy = dict(
            row,
            lane_id=OTHER,
            assignee="other-seat",
            worktree_path=str(tmp_path / "other-checkout"),
            worktree_toplevel_canonical=str(tmp_path / "other-checkout"),
        )
        lanes.save_lanes(store, {"schema_version": 1, "lanes": {key: row, OTHER: healthy}})
        return store

    make.root = tmp_path
    return make


_MISSING = object()


def _run(store, *argv):
    return cli.main(["--root", str(store.root), *argv])


# ---------------------------------------------------------------------------------------------- shared API (H2, H3)


def test_every_checkout_helper_requires_the_registry_key_by_name(tmp_path):
    row = {"worktree_path": "\t"}
    with pytest.raises(TypeError):
        lanes.require_checkout_paths(row)  # no key, no root
    with pytest.raises(TypeError):
        lanes.require_checkout_paths(row, lane_id=BAD)  # no root
    with pytest.raises(TypeError):
        lanes.require_checkout_paths(row, BAD, tmp_path)  # not by name
    with pytest.raises(TypeError):
        lanes.checkout_path_recovery(BAD)  # not by name


@pytest.mark.parametrize("embedded", [OTHER, None, "", 5], ids=["other-lane", "none", "empty", "number"])
def test_the_hint_names_the_registry_key_whatever_the_row_says_about_itself(tmp_path, embedded):
    row = {"lane_id": embedded, "worktree_path": "\t"}
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row, lane_id=BAD, root=tmp_path)
    text = str(caught.value)
    assert _hint(BAD) in text and "lanes.json lane 'bad'" in text
    assert OTHER not in text


@pytest.mark.parametrize("key", ["", "not a valid key!", "x" * 200])
def test_an_unusable_registry_key_gets_the_placeholder_never_another_lanes_id(tmp_path, key):
    row = {"lane_id": OTHER, "worktree_path": "\t"}
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row, lane_id=key, root=tmp_path)
    assert f"lane abandon --id <lane-id> {ACK_HINT}" in str(caught.value)
    assert OTHER not in str(caught.value)


@pytest.mark.parametrize("canonical", RELATIVE_CANONICALS)
def test_a_relative_canonical_checkout_path_is_refused_with_repair_advice(tmp_path, canonical):
    row = {"lane_id": OTHER, "worktree_path": str(tmp_path / "w"), "worktree_toplevel_canonical": canonical}
    assert lanes.canonical_checkout_problem(row, root=tmp_path) is not None
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row, lane_id=BAD, root=tmp_path)
    text = str(caught.value)
    assert "worktree_toplevel_canonical" in text and "absolute" in text and "lanes.json lane 'bad'" in text
    assert ACK_HINT not in text  # repair the field; do not offer a retirement that release would still refuse
    assert OTHER not in text


def test_the_canonical_rule_runs_before_the_blank_field_advice(tmp_path):
    row = {"worktree_path": "\t", "worktree_toplevel_canonical": "relative/canonical"}
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row, lane_id=BAD, root=tmp_path)
    assert "worktree_toplevel_canonical" in str(caught.value) and ACK_HINT not in str(caught.value)


@pytest.mark.parametrize(
    "fields",
    [
        {"worktree_toplevel_canonical": None},
        {},
        {"worktree_toplevel_canonical": "{ROOT}/checkout"},
    ],
    ids=["none", "absent", "root-placeholder"],
)
def test_the_canonical_rule_leaves_absent_and_placeholder_spellings_alone(tmp_path, fields):
    row = {"worktree_path": str(tmp_path / "w"), **fields}
    assert lanes.canonical_checkout_problem(row, root=tmp_path) is None
    lanes.require_checkout_paths(row, lane_id=BAD, root=tmp_path)


def test_a_padded_relative_canonical_is_still_relative(tmp_path):
    """Nothing is stripped, resolved or joined to a base folder to make a relative spelling valid."""
    row = {"worktree_toplevel_canonical": "  relative-with-padding  "}
    assert lanes.canonical_checkout_problem(row, root=tmp_path) is not None


def test_a_relative_worktree_path_keeps_its_legacy_meaning(tmp_path):
    row = {"worktree_path": "relative-worktrees/w", "worktree_toplevel_canonical": str(tmp_path / "abs")}
    assert lanes.canonical_checkout_problem(row, root=tmp_path) is None
    lanes.require_checkout_paths(row, lane_id=BAD, root=tmp_path)


@pytest.mark.parametrize("row", [[], "text", 7, None], ids=["list", "string", "number", "none"])
def test_the_canonical_rule_leaves_a_row_that_is_not_an_object_to_the_callers(tmp_path, row):
    assert lanes.canonical_checkout_problem(row, root=tmp_path) is None


# ---------------------------------------------------------------------------- K1/K0/V0: lane check, candidate, verify


@pytest.mark.parametrize("embedded", [OTHER, _MISSING, ""], ids=["other-lane", "missing", "empty"])
def test_lane_check_names_the_selected_registry_key(keyed, capsys, embedded):
    store = keyed(embedded=embedded, worktree_path="\t")
    before = lanes.lanes_path(store).read_bytes()
    assert _run(store, "lane", "check", "--id", BAD) == 2
    err = capsys.readouterr().err
    assert _hint(BAD) in err and f"lane abandon --id {OTHER}" not in err
    assert lanes.lanes_path(store).read_bytes() == before


def test_the_candidate_and_the_verifier_name_the_selected_key(keyed):
    store = keyed(worktree_path="\t")
    row = lanes.load_lanes(store)["lanes"][BAD]
    for call in (
        lambda: cli._lane_candidate(store, row, None, lane_id=BAD, delivery=False),
        lambda: cli._lane_candidate(store, row, "HEAD", lane_id=BAD, delivery=True),
        lambda: cli._verify_lane_worktree(store, row, lane_id=BAD),
    ):
        with pytest.raises(lanes.LaneError) as caught:
            call()
        assert _hint(BAD) in str(caught.value) and f"--id {OTHER}" not in str(caught.value)


@pytest.mark.parametrize("canonical", RELATIVE_CANONICALS[:2])
def test_the_candidate_refuses_a_relative_canonical_before_any_git_or_folder_use(keyed, monkeypatch, canonical):
    store = keyed(worktree_toplevel_canonical=canonical)
    row = lanes.load_lanes(store)["lanes"][BAD]

    def git(*args, **kwargs):
        pytest.fail("a relative canonical field must be refused before Git or any folder is used")

    monkeypatch.setattr(cli, "_git_read_one", git)
    monkeypatch.setattr(cli, "_git", git)
    for call in (
        lambda: cli._lane_candidate(store, row, None, lane_id=BAD, delivery=False),
        lambda: cli._lane_candidate(store, row, "HEAD", lane_id=BAD, delivery=True),  # even when --head is given
        lambda: cli._verify_lane_worktree(store, row, lane_id=BAD),
    ):
        with pytest.raises(lanes.LaneError, match="worktree_toplevel_canonical.*absolute"):
            call()


# ---------------------------------------------------- command matrix, each field and kind of damage (W, T, B1, D1, K1)

COMMANDS = {
    "workspace": ["workspace"],
    "workspace-json": ["workspace", "--json"],
    "check": ["check"],
    "deliver": ["deliver", "--from", "dev"],
    "abandon": ["abandon"],
    "restamp": ["restamp", "--from", "lead", "--reason", "repair"],
}


@pytest.mark.parametrize("command", sorted(COMMANDS))
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_blank_field_hints_name_the_registry_key_in_every_command(keyed, capsys, command, field):
    store = keyed(**{field: "\t"})
    before = lanes.lanes_path(store).read_bytes()
    assert _run(store, "lane", *COMMANDS[command][:1], "--id", BAD, *COMMANDS[command][1:]) == 2
    err = capsys.readouterr().err
    assert field in err and _hint(BAD) in err and f"--id {OTHER}" not in err
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("command", sorted(COMMANDS))
@pytest.mark.parametrize("worktree_path", ["\t", "absent"], ids=["damaged-path", "healthy-path"])
def test_a_relative_canonical_is_refused_by_every_command_without_a_circular_hint(
    keyed, capsys, command, worktree_path
):
    store = keyed(
        worktree_path="\t" if worktree_path == "\t" else str(keyed.root / "absent-checkout"),
        worktree_toplevel_canonical="absent-relative-canonical",
    )
    before = lanes.lanes_path(store).read_bytes()
    assert _run(store, "lane", *COMMANDS[command][:1], "--id", BAD, *COMMANDS[command][1:]) == 2
    err = capsys.readouterr().err
    assert "worktree_toplevel_canonical" in err and "absolute" in err and "'bad'" in err
    assert ACK_HINT not in err and f"--id {OTHER}" not in err
    assert lanes.lanes_path(store).read_bytes() == before


# --------------------------------------------------------------------------------- B2/B3: the acknowledgement owner


@pytest.mark.parametrize("canonical", RELATIVE_CANONICALS)
@pytest.mark.parametrize("first", ["blank", "absent-absolute"])
def test_acknowledgement_refuses_a_relative_canonical_in_either_position(keyed, capsys, canonical, first):
    """bot 4562636881: it anchored the relative canonical against the repository, found nothing, and retired a lane
    that release would still refuse. Neither a blank first field nor an absent absolute one may hide it."""
    store = keyed(
        worktree_path="\t" if first == "blank" else str(keyed.root / "absent-first-field"),
        worktree_toplevel_canonical=canonical,
    )
    before = lanes.lanes_path(store).read_bytes()
    assert _run(store, "lane", "abandon", "--id", BAD, ACK_HINT) == 2
    err = capsys.readouterr().err
    assert "worktree_toplevel_canonical" in err and "absolute" in err and f"--id {OTHER}" not in err
    assert lanes.lanes_path(store).read_bytes() == before


def test_acknowledgement_still_retires_a_blank_path_with_an_absolute_absent_canonical(keyed, capsys):
    store = keyed(worktree_path="\t", worktree_toplevel_canonical=str(keyed.root / "gone-absolute"))
    assert _run(store, "lane", "abandon", "--id", BAD, ACK_HINT) == 0
    row = lanes.load_lanes(store)["lanes"][BAD]
    assert row["status"] == lanes.STATUS_ABANDONED and row["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED
    assert row["lane_id"] == OTHER  # the saved record is not rewritten


def test_the_pathless_launch_check_does_not_select_a_path_again(keyed, monkeypatch):
    """The acknowledgement owner picks the surviving path once; launch-use is told, and never picks for itself."""
    store = keyed(worktree_path="\t")
    row = lanes.load_lanes(store)["lanes"][BAD]

    def pick(*args, **kwargs):
        pytest.fail("launch-use must not select a checkout path by itself")

    monkeypatch.setattr(cli, "_lane_acknowledgement_path", pick)
    assert cli._lane_worktree_launch_use(store, {**row, "lane_id": BAD}, pathless=True) is None


# ------------------------------------------------------------------------------------------- S: status, plain and JSON


def _status_rows(store, capsys):
    assert _run(store, "lane", "status", "--json") == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("embedded", [OTHER, _MISSING, ""], ids=["other-lane", "missing", "empty"])
def test_status_hint_names_the_registry_key_and_never_fills_in_the_embedded_id(keyed, capsys, embedded):
    store = keyed(embedded=embedded, worktree_path="\t")
    before = lanes.lanes_path(store).read_bytes()
    rows = _status_rows(store, capsys)
    shown = next(r for r in rows if r.get("assignee") == "bad-seat")
    assert _hint(BAD) in shown["checkout_path_error"] and f"--id {OTHER}" not in shown["checkout_path_error"]
    assert ("lane_id" in shown) == (embedded is not _MISSING)
    assert lanes.lanes_path(store).read_bytes() == before


def test_status_with_an_empty_registry_key_prints_the_placeholder(keyed, capsys):
    store = keyed(key="", worktree_path="\t")
    rows = _status_rows(store, capsys)
    shown = next(r for r in rows if r.get("assignee") == "bad-seat")
    assert f"lane abandon --id <lane-id> {ACK_HINT}" in shown["checkout_path_error"]
    assert OTHER not in shown["checkout_path_error"]
    assert {r["lane_id"] for r in rows if r.get("assignee") != "bad-seat"} == {OTHER}  # the healthy lane is untouched


@pytest.mark.parametrize("acknowledged", [False, True], ids=["plain", "acknowledged"])
def test_status_shows_a_relative_canonical_even_on_an_acknowledged_lane(keyed, capsys, acknowledged):
    fields = {"worktree_toplevel_canonical": "absent-relative-canonical"}
    if acknowledged:
        fields.update(status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED)
    store = keyed(**fields)
    rows = _status_rows(store, capsys)
    shown = next(r for r in rows if r.get("assignee") == "bad-seat")
    assert "worktree_toplevel_canonical" in shown["checkout_path_error"]
    assert ACK_HINT not in shown["checkout_path_error"]


def test_status_still_hides_an_acknowledged_blank_path(keyed, capsys):
    store = keyed(worktree_path="\t", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED)
    assert all(r.get("assignee") != "bad-seat" for r in _status_rows(store, capsys))


# ------------------------------------------------------------------------------- V2: the fresh row at delivery cleanup


@pytest.mark.parametrize(
    "fields",
    [
        {"worktree_path": "\t"},
        {"worktree_path": ""},
        {"worktree_path": 7},
        {"worktree_toplevel_canonical": "\t"},
        {"worktree_toplevel_canonical": "absent-relative-canonical"},
    ],
    ids=["tab", "blank", "number", "blank-canonical", "relative-canonical"],
)
def test_delivery_cleanup_checks_the_fresh_row_before_it_looks_at_a_folder(keyed, monkeypatch, capsys, fields):
    """A damaged field that looked like "no folder" used to end the cleanup as complete."""
    store = keyed(**fields)
    row = dict(lanes.load_lanes(store)["lanes"][BAD])
    done = []
    monkeypatch.setattr(cli, "_lane_checkpoint_cleanup", lambda *a, **k: done.append((a, k)))
    monkeypatch.setattr(cli, "_lane_entry_present", lambda *a, **k: pytest.fail("a folder was looked at"))
    monkeypatch.setattr(cli, "_lane_worktree_launch_use", lambda *a, **k: pytest.fail("launch use was checked"))
    artifact, pending, head = cli._lane_deliver_cleanup(store, BAD, {"head_sha": "h"}, row, Path("artifact"), "h")
    assert pending is True and done == []  # cleanup stays pending; nothing is marked complete
    assert "cleanup is pending" in capsys.readouterr().err


# ------------------------------------------------------------------------------- L1/L2/L3: wrapper, map, queued launch


@pytest.mark.parametrize(
    "field,value",
    [
        ("worktree_path", "\t"),
        ("worktree_toplevel_canonical", "\t"),
        ("worktree_toplevel_canonical", "absent-relative"),
    ],
)
def test_wrapper_refuses_the_damaged_lane_with_the_selected_key(keyed, monkeypatch, capsys, field, value):
    store = keyed(**{field: value})
    monkeypatch.setattr(lane_occupancy, "hold", lambda *_: pytest.fail("occupancy was taken"))
    monkeypatch.setattr(wrapper_run, "preflight_launch_runtime", lambda *a, **k: pytest.fail("reached launch"))
    assert _run(store, "wrap", "--for", "dev", "--cli", "claude", "--lane-id", BAD, "--", sys.executable) == 2
    err = capsys.readouterr().err
    assert field in err and f"--id {OTHER}" not in err


@pytest.mark.parametrize(
    "field,value",
    [
        ("worktree_path", "\t"),
        ("worktree_toplevel_canonical", "absent-relative"),
    ],
)
def test_workspace_map_skips_the_damaged_lane_once_and_keeps_the_healthy_one(keyed, capsys, field, value):
    store = keyed(**{field: value}, status=lanes.STATUS_ACTIVE)
    data = lanes.load_lanes(store)
    data["lanes"][OTHER]["status"] = lanes.STATUS_ACTIVE
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    assert supervisor.active_ephemeral_lane_workspaces(store) == {OTHER: str(store.root / "other-checkout")}
    err = capsys.readouterr().err
    assert field in err and f"--id {OTHER}" not in err.replace(f"lane {OTHER!r}", "")
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("worktree_path", "\t"),
        ("worktree_toplevel_canonical", "absent-relative"),
    ],
)
def test_a_queued_launch_for_the_damaged_lane_is_denied_and_archived(keyed, field, value):
    from test_ephemeral_reviewers import NOW, _cfg, _marker

    store = keyed(**{field: value})
    marker = _marker("request")
    marker["lane_id"] = BAD
    store.write_launch_request(marker)
    state = {}
    with pytest.raises(ephemeral.EphemeralError, match=f"{BAD}.*{field}"):
        supervisor.prepare_launch_request(store, state, _cfg(), "request", now_epoch=NOW)
    assert store.read_launch_request("request") is None
    archived = json.loads((store.launch_requests_archive_dir / "request.json").read_text())
    assert archived["terminal_state"] == ephemeral.STATE_DENIED and f"--id {OTHER}" not in archived["reason"]
    assert not state


# ------------------------------------------------------------------------------------ D2: committed-delivery resume


def test_resuming_a_delivery_refuses_a_relative_canonical_when_no_committed_artifact_covers_it(keyed, capsys):
    """The path-free publication exception needs valid committed evidence; without it every damage kind refuses."""
    store = keyed(worktree_toplevel_canonical="absent-relative", status=lanes.STATUS_DELIVERED)
    before = lanes.lanes_path(store).read_bytes()
    assert _run(store, "lane", "deliver", "--id", BAD, "--from", "dev") == 2
    err = capsys.readouterr().err
    assert "worktree_toplevel_canonical" in err and "absolute" in err and f"--id {OTHER}" not in err
    assert lanes.lanes_path(store).read_bytes() == before


# -------------------------------------------------------------------------------------- A1: forced reassignment


@pytest.mark.parametrize(
    "field,value",
    [
        ("worktree_path", "\t"),
        ("worktree_toplevel_canonical", "\t"),
        ("worktree_toplevel_canonical", "absent-relative"),
    ],
)
def test_forced_assignment_refuses_a_damaged_previous_checkout_with_the_key(keyed, capsys, field, value):
    store = keyed(key=BAD, embedded=BAD, **{field: value})
    before = lanes.lanes_path(store).read_bytes()
    code = _run(
        store,
        "lane",
        "assign",
        "--id",
        BAD,
        "--assignee",
        "dev",
        "--from",
        "lead",
        "--domain",
        "core",
        "--base",
        "HEAD",
        "--target",
        "main",
        "--force",
    )
    err = capsys.readouterr().err
    assert code == 2 and field in err and f"'{OTHER}'" not in err
    assert lanes.lanes_path(store).read_bytes() == before


# ---------------------------------------------------------------------------------------- F1-F5: worktree release


def _release_row(**fields):
    return {"lane_id": OTHER, "status": lanes.STATUS_ACTIVE, **fields}


@pytest.mark.parametrize("order", ["worktree-first", "canonical-first"])
@pytest.mark.parametrize("canonical", RELATIVE_CANONICALS[:2])
def test_release_refuses_a_relative_canonical_before_an_earlier_matching_path(tmp_path, order, canonical):
    """The matching absolute worktree path used to return True before the invalid canonical field was visited, in
    one dictionary order only; the lexical guard runs first, so neither order decides."""
    from agenttalk import worktree_release

    target = tmp_path / "checkout"
    target.mkdir()
    pairs = [("worktree_path", str(target)), ("worktree_toplevel_canonical", canonical)]
    row = _release_row(**dict(pairs if order == "worktree-first" else pairs[::-1]))
    with pytest.raises(worktree_release.Refused, match="worktree_toplevel_canonical.*ambiguous"):
        worktree_release._names_checkout(row, target, tmp_path, lane_id=BAD, source="lanes.json lane bad", lane=True)


def test_release_still_refuses_an_acknowledged_relative_canonical_but_not_an_acknowledged_blank_path(tmp_path):
    from agenttalk import worktree_release

    ack = {"status": lanes.STATUS_ABANDONED, "worktree_state": lanes.STATUS_CLEANUP_ACKNOWLEDGED}
    with pytest.raises(worktree_release.Refused, match="worktree_toplevel_canonical"):
        worktree_release._names_checkout(
            _release_row(**ack, worktree_toplevel_canonical="relative"),
            tmp_path / "t",
            tmp_path,
            lane_id=BAD,
            source="lanes.json lane bad",
            lane=True,
        )
    assert not worktree_release._names_checkout(
        _release_row(**ack, worktree_path="\t"),
        tmp_path / "t",
        tmp_path,
        lane_id=BAD,
        source="lanes.json lane bad",
        lane=True,
    )


@pytest.mark.parametrize("lane_id", [BAD, "", None])
def test_release_hints_use_the_given_key_and_the_row_never_decides(tmp_path, lane_id):
    from agenttalk import worktree_release

    row = _release_row(worktree_path="\t")
    with pytest.raises(worktree_release.Refused) as stopped:
        worktree_release._names_checkout(row, tmp_path / "t", tmp_path, lane_id=lane_id, source="x", lane=True)
    text = str(stopped.value)
    assert (f"lane abandon --id {lane_id} " if lane_id else "lane abandon --id <lane-id> ") in text
    assert OTHER not in text


def test_release_keeps_a_relative_worktree_path_and_nested_lane_entries_keyed(tmp_path):
    from agenttalk import worktree_release

    target = tmp_path / "rel" / "co"
    target.mkdir(parents=True)
    row = _release_row(worktree_path="rel/co", worktree_toplevel_canonical=str(target))
    assert worktree_release._names_checkout(row, target, tmp_path, lane_id=BAD, source="s", lane=True)
    nested = {"lanes": [{"lane_id": OTHER, "worktree_path": "\t"}]}
    with pytest.raises(worktree_release.Refused, match="lane abandon --id bad "):
        worktree_release._names_checkout(nested, target, tmp_path, lane_id=BAD, source="s", lane=True)


# ------------------------------------------------------------------------------------------- the AST contract

import ast  # noqa: E402
import inspect  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "agenttalk"
KEYED = {
    "require_checkout_paths",
    "require_canonical_checkout",
    "checkout_issue_advice",
    "checkout_path_recovery",
    "_lane_candidate",
    "_verify_lane_worktree",
    "_lane_acknowledgement_path",
    "_names_checkout",
}


def _source_trees():
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _derived_from_the_row(expr: ast.expr) -> bool:
    """True when the expression reads an id out of a row: row.get("lane_id"), row["lane_id"], or any call."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Call):
            return True
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "lane_id":
            return True
    return False


def test_every_call_forwards_the_registry_key_by_name_and_never_reads_it_from_the_row():
    calls, problems = 0, []
    for path, tree in _source_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in KEYED:
                continue
            calls += 1
            where = f"{path.name}:{node.lineno} {_call_name(node)}"
            given = {kw.arg: kw.value for kw in node.keywords}
            if None in given or any(isinstance(arg, ast.Starred) for arg in node.args):
                problems.append(f"{where}: arguments are unpacked, so the key cannot be seen")
            elif "lane_id" not in given:
                problems.append(f"{where}: no lane_id=")
            elif _derived_from_the_row(given["lane_id"]):
                problems.append(f"{where}: lane_id is read from a row or computed")
            elif isinstance(given["lane_id"], ast.Constant) and given["lane_id"].value is not None:
                problems.append(f"{where}: lane_id is a literal")
            elif isinstance(given["lane_id"], ast.Constant) and _call_name(node) != "_names_checkout":
                problems.append(f"{where}: only a record that is not a lane row may pass lane_id=None")
    assert calls >= 40, f"the inventory found only {calls} calls: the scan is broken"
    assert not problems, "\n".join(problems)


def test_no_alias_or_dynamic_access_can_hide_a_call_from_the_contract():
    problems = []
    for path, tree in _source_trees():
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                problems += [
                    f"{where}: import alias {a.name} as {a.asname}"
                    for a in node.names
                    if a.name.split(".")[-1] in KEYED and a.asname
                ]
            elif isinstance(node, ast.Name) and node.id in KEYED and isinstance(node.ctx, ast.Load):
                if id(node) not in called:
                    problems.append(f"{where}: {node.id} is referenced without being called")
            elif isinstance(node, ast.Attribute) and node.attr in KEYED and id(node) not in called:
                problems.append(f"{where}: {node.attr} is referenced without being called")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in KEYED:
                problems.append(f"{where}: the name {node.value!r} is used as text (getattr/dispatch)")
    assert not problems, "\n".join(problems)


def test_the_keyed_helpers_have_no_default_for_the_key():
    from agenttalk import worktree_release

    for function in (
        lanes.require_checkout_paths,
        lanes.require_canonical_checkout,
        lanes.checkout_issue_advice,
        lanes.checkout_path_recovery,
        cli._lane_candidate,
        cli._verify_lane_worktree,
        cli._lane_acknowledgement_path,
        worktree_release._names_checkout,
    ):
        parameter = inspect.signature(function).parameters["lane_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, function.__name__
        assert parameter.default is inspect.Parameter.empty, function.__name__


# ------------------------------------------------------------------- R1-R7: reservations are keyed by the registry key


def _reservation_data():
    """Row "x" is registered under "x" but says it is "sel" (a different lane that merely carries that id); the
    row registered under "sel" says "zzz". The selected lane is the one stored under the key "sel"."""
    base = {"status": lanes.STATUS_ACTIVE, "domain_id": "core", "path_subset": ["src/a"]}
    return {
        "lanes": {
            "x": {**base, "lane_id": "sel", "assignee": "x-seat"},
            "sel": {**base, "lane_id": "zzz", "assignee": "sel-seat"},
            "gone": {**base, "status": lanes.STATUS_ABANDONED, "lane_id": "gone"},
            "bad-row": "not an object",
        }
    }


def test_the_keyed_reservation_iterator_excludes_by_the_registry_key_only():
    data = _reservation_data()
    assert [key for key, _row in lanes.reservation_lane_items(data)] == ["x", "sel"]
    kept = lanes.reservation_lane_items(data, exclude="sel")
    assert [(key, row["lane_id"]) for key, row in kept] == [("x", "sel")]  # the row that only SAYS "sel" stays
    assert [row for _key, row in lanes.reservation_lane_items(data)] == lanes.reservation_lanes(data)


def test_conflicts_exclude_the_selected_key_and_name_registry_keys():
    data = _reservation_data()
    selected = {"domain_id": "core", "path_subset": ["src/a"]}
    assert lanes.reservation_conflicts(data, selected, exclude="sel") == ["x"]
    assert lanes.reservation_conflicts(data, selected) == ["sel", "x"]  # labels are keys, not the ids inside the rows
    data["lanes"]["noid"] = {**data["lanes"]["x"]}
    del data["lanes"]["noid"]["lane_id"]
    assert "noid" in lanes.reservation_conflicts(data, selected, exclude="sel")  # never "<unknown>"


def test_active_lane_fingerprints_exclude_by_key_and_keep_their_payload():
    data = _reservation_data()
    entries = lanes.active_lane_fingerprints(data, exclude="sel")
    assert entries == [{"lane_id": "sel", "fingerprint": lanes.fingerprint(data["lanes"]["x"])}]
    # the raw rows are hashed as they are, whatever they say about themselves
    both = lanes.active_lane_fingerprints(data)
    assert {e["fingerprint"] for e in both} == {lanes.fingerprint(data["lanes"][k]) for k in ("x", "sel")}


def test_check_evaluates_against_the_row_that_only_names_the_selected_lane(keyed, monkeypatch):
    """R3: lane check --id sel must still count a row stored under another key that merely says "sel"."""
    store = keyed(key="sel", embedded="zzz", status=lanes.STATUS_ACTIVE)
    data = lanes.load_lanes(store)
    data["lanes"]["x"] = {**data["lanes"]["sel"], "lane_id": "sel", "assignee": "x-seat"}
    lanes.save_lanes(store, data)
    seen = []

    class Stop(Exception):
        pass

    def evaluate(_store, _lane, others, *args, **kwargs):
        seen.append(others)
        raise Stop

    monkeypatch.setattr(cli, "_lane_candidate", lambda *a, **k: ("h" * 40, None))
    monkeypatch.setattr(cli, "_lane_eval", evaluate)
    with pytest.raises(Stop):
        _run(store, "lane", "check", "--id", "sel")
    assert sorted(row["assignee"] for row in seen[0]) == ["other-seat", "x-seat"]  # the selected row is not there


def test_reservation_rows_are_never_filtered_by_the_id_inside_them():
    """R2-R7: identity for a reservation is the registry key, so nothing outside the iterator may walk the rows."""
    for path, tree in _source_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "reservation_lanes":
                inside = path.name == "lanes.py"
                assert inside, f"{path.name}:{node.lineno} uses reservation_lanes (rows without their keys)"
    lanes_tree = ast.parse((SRC / "lanes.py").read_text(encoding="utf-8"))
    bodies = {fn.name: fn for fn in ast.walk(lanes_tree) if isinstance(fn, ast.FunctionDef)}
    for name in ("reservation_conflicts", "active_lane_fingerprints"):
        used = {_call_name(n) for n in ast.walk(bodies[name]) if isinstance(n, ast.Call)}
        assert "reservation_lane_items" in used and "reservation_lanes" not in used, name


@pytest.mark.parametrize("canonical", RELATIVE_CANONICALS[:2])
@pytest.mark.parametrize("first", ["\t", None, "ABSENT"], ids=["blank", "missing", "absent-absolute"])
def test_the_acknowledgement_owner_judges_the_canonical_before_choosing_any_path(keyed, canonical, first):
    """Called on its own: a blank, missing or absent-absolute first field must not let it return a path while a relative
    canonical is still saved in the second."""
    store = keyed(worktree_toplevel_canonical=canonical)
    row = dict(lanes.load_lanes(store)["lanes"][BAD])
    if first == "ABSENT":
        row["worktree_path"] = str(store.root / "absent-first-field")
    elif first is None:
        del row["worktree_path"]
    else:
        row["worktree_path"] = first
    with pytest.raises(lanes.LaneError, match="worktree_toplevel_canonical.*absolute") as caught:
        cli._lane_acknowledgement_path(store, row, lane_id=BAD)
    assert f"--id {OTHER}" not in str(caught.value) and "'bad'" in str(caught.value)


def test_the_acknowledgement_owner_refuses_a_relative_worktree_path_and_still_expands_the_root_placeholder(keyed):
    store = keyed()
    row = dict(lanes.load_lanes(store)["lanes"][BAD])
    row.update(worktree_path="relative/worktree", worktree_toplevel_canonical=str(store.root / "canon"))
    with pytest.raises(lanes.LaneError, match=ADVICE):
        cli._lane_acknowledgement_path(store, row, lane_id=BAD)
    row.update(worktree_path="\t", worktree_toplevel_canonical="{ROOT}/canon")
    assert cli._lane_acknowledgement_path(store, row, lane_id=BAD) == Path(str(store.root) + "/canon")
    row.update(worktree_path="\t", worktree_toplevel_canonical="\t")
    assert cli._lane_acknowledgement_path(store, row, lane_id=BAD) is None


# --------------------------------------------------------------------------- rows an older agenttalk already saved

OLD_STYLE = {"worktree_path": ".worktrees/old-lane", "worktree_toplevel_canonical": ".worktrees/old-lane"}
OLD_ADVICE = "remove the worktree_toplevel_canonical field"


def _assert_old_row_refusal(err):
    assert "lanes.json lane 'bad'" in err and "worktree_toplevel_canonical is a relative path" in err
    assert "it must be an absolute path" in err and OLD_ADVICE in err
    assert "set it to the checkout's real absolute folder" in err
    assert "lane abandon --id" not in err and OTHER not in err  # no command at all, so none can name another lane


@pytest.mark.parametrize("embedded", [OTHER, _MISSING], ids=["names-another-lane", "no-id-inside"])
@pytest.mark.parametrize("command", sorted(COMMANDS) + ["abandon-acknowledge"])
def test_a_row_saved_with_a_relative_canonical_gets_exit_2_and_a_repair_that_names_no_command(
    keyed, capsys, embedded, command
):
    store = keyed(embedded=embedded, **OLD_STYLE)
    before = lanes.lanes_path(store).read_bytes()
    argv = ["abandon", ACK_HINT] if command == "abandon-acknowledge" else COMMANDS[command]
    assert _run(store, "lane", argv[0], "--id", BAD, *argv[1:]) == 2
    _assert_old_row_refusal(capsys.readouterr().err)
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("repair", ["remove-the-field", "set-the-real-absolute-folder"])
@pytest.mark.parametrize("acknowledged", [False, True], ids=["active", "already-retired"])
def test_the_stated_repair_of_an_old_row_works_and_status_shows_it_until_then(keyed, capsys, repair, acknowledged):
    fields = dict(OLD_STYLE)
    if acknowledged:
        fields.update(status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED)
    store = keyed(**fields)
    shown = next(r for r in _status_rows(store, capsys) if r.get("assignee") == "bad-seat")
    _assert_old_row_refusal(shown["checkout_path_error"])
    data = lanes.load_lanes(store)  # the repair the message describes, done by hand in lanes.json
    if repair == "remove-the-field":
        del data["lanes"][BAD]["worktree_toplevel_canonical"]
    else:
        data["lanes"][BAD]["worktree_toplevel_canonical"] = str(store.root / ".worktrees" / "old-lane")
    lanes.save_lanes(store, data)
    assert not any("checkout_path_error" in r for r in _status_rows(store, capsys))
    if not acknowledged:
        assert _run(store, "lane", "workspace", "--id", BAD) == 0
        assert capsys.readouterr().out.strip() == OLD_STYLE["worktree_path"]


# ------------------------------------------------------------------------------- UNC share roots are absolute paths

UNC_ABSOLUTE = [
    r"\\server\share",
    "//server/share",
    "\\\\server\\share\\",
    "//server/share/",
    r"\\server\share\checkout",
    "//server/share/checkout",
    r"\\server\share/mixed\separators",
]
WINDOWS_RELATIVE = [r"C:relative", r"\rooted-without-drive", r"relative\checkout", r"\\server", "server/share", "C:"]


class _NtHost:
    """What `os` looks like to `canonical_checkout_problem` on Windows, so the Windows branch runs on every host."""

    name = "nt"
    path = ntpath


@pytest.mark.parametrize("canonical", UNC_ABSOLUTE)
def test_an_absolute_unc_share_root_is_not_called_relative(monkeypatch, tmp_path, canonical):
    monkeypatch.setattr(lanes, "os", _NtHost)
    row = {"worktree_path": canonical, "worktree_toplevel_canonical": canonical}
    assert lanes.canonical_checkout_problem(row, root=tmp_path) is None
    lanes.require_checkout_paths(row, lane_id=BAD, root=tmp_path)  # raises nothing


@pytest.mark.parametrize("canonical", WINDOWS_RELATIVE + ["relative/checkout", "./dot"])
def test_windows_relative_spellings_stay_refused_next_to_the_unc_roots(monkeypatch, tmp_path, canonical):
    monkeypatch.setattr(lanes, "os", _NtHost)
    problem = lanes.canonical_checkout_problem({"worktree_toplevel_canonical": canonical}, root=tmp_path)
    assert problem == ("worktree_toplevel_canonical", "is a relative path")


@pytest.mark.skipif(os.name != "nt", reason="a UNC path is only absolute on Windows")
@pytest.mark.parametrize("canonical", UNC_ABSOLUTE[:4])
def test_a_saved_unc_share_root_does_not_stop_a_lane_command(keyed, capsys, canonical):
    """No folder on the share is ever looked at: the stored text is only classified and printed."""
    store = keyed(worktree_path=canonical, worktree_toplevel_canonical=canonical)
    before = lanes.lanes_path(store).read_bytes()
    assert _run(store, "lane", "workspace", "--id", BAD, "--json") == 0
    assert json.loads(capsys.readouterr().out)["worktree_path"] == canonical
    assert not any("checkout_path_error" in r for r in _status_rows(store, capsys))
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="a UNC path is only absolute on Windows")
def test_worktree_release_accepts_a_unc_share_root_as_absolute_too(tmp_path, monkeypatch):
    """Release must not call a share root ambiguous. Its physical matching (`Path.resolve`) is kept in production but
    replaced here by a stand-in that looks at nothing, so this test never touches a share or the network; it records
    what release asked to resolve, which shows that release got past the classification and reached the matching."""
    import pathlib

    from agenttalk import worktree_release

    asked = []

    def resolve_nothing(self, strict=False):
        asked.append(str(self))
        return self

    monkeypatch.setattr(pathlib.Path, "resolve", resolve_nothing)
    row = {"lane_id": OTHER, "worktree_toplevel_canonical": r"\\server\share"}
    assert not worktree_release._names_checkout(
        row, tmp_path / "elsewhere", tmp_path, lane_id=BAD, source="lanes.json lane bad", lane=True
    )
    assert any(path.rstrip("\\") == r"\\server\share" for path in asked)  # reached the matching, looked at nothing


# ---------------------------------------- the committed-publication exception is kept for a relative canonical too


def test_a_committed_delivery_still_publishes_when_the_canonical_path_is_relative(tmp_path, monkeypatch, capsys):
    """Valid committed evidence needs no checkout cleanup: publication finishes (exit 0) and cleanup stays pending,
    with the relative field left exactly as it is for the person to repair. (The delivery still reads the healthy
    saved snapshot's checkout with Git; it just never cleans it up.) Every other command refuses such a row."""
    from test_lanes import _committed_artifacts

    store, lane_id, worktree, head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    original_artifact = artifact.read_bytes()
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["worktree_toplevel_canonical"] = "relative/canonical"
    lanes.save_lanes(store, data)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    output = capsys.readouterr()
    assert "cleanup pending" in output.out and "worktree_toplevel_canonical" in output.err
    current = lanes.load_lanes(store)["lanes"][lane_id]
    assert current["publish_pending"] is False and current["cleanup_pending"] is True
    assert current["worktree_toplevel_canonical"] == "relative/canonical"  # not rewritten
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]
    assert worktree.exists()  # the checkout is not cleaned up
    assert cli.main(["--root", str(store.root), "lane", "workspace", "--id", lane_id]) == 2  # other commands refuse


# ----------- a delivery an older version prepared with the damage already in the row: committed, so it publishes


def _relative_to_cwd(worktree):
    return os.path.relpath(worktree, os.getcwd())


LEGACY_CANONICALS = [
    pytest.param("", id="blank"),
    pytest.param(_relative_to_cwd, id="relative"),
]


def _repair_the_row(store, lane_id, worktree):
    """What a person does to the registry row: the real absolute folder. The signed snapshot is not touched."""
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["worktree_toplevel_canonical"] = lanes.canonical_host_path(worktree)
    lanes.save_lanes(store, data)


RETRY_FOLDERS = ["original", "sibling", "child"]


def _retry_folder(tmp_path, kind):
    """The folder a retry runs from, with `tmp_path` as the original one: a sibling beside it, or a new child in it."""
    if kind == "original":
        return tmp_path
    folder = tmp_path.parent / (tmp_path.name + "-sibling") if kind == "sibling" else tmp_path / "child"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("cwd", RETRY_FOLDERS)
@pytest.mark.parametrize("row", ["damaged", "repaired"])
@pytest.mark.parametrize("canonical", LEGACY_CANONICALS)
def test_a_legacy_snapshot_with_a_damaged_canonical_path_publishes_its_valid_final(
    tmp_path, monkeypatch, capsys, canonical, row, cwd
):
    """The signed snapshot inside the pending delivery (and the final's lane fingerprint) carry the damage from before
    the delivery. The committed final is valid, so resuming publishes it: it is not quarantined and the damaged
    active lane is not put back. With the row still damaged, cleanup stays pending and the row is not rewritten;
    with the row repaired, the delivery also finishes its cleanup."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree, head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch, canonical)
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    damage = saved["worktree_toplevel_canonical"]
    assert saved["publish_pending"]["lane_snapshot"]["worktree_toplevel_canonical"] == damage  # damaged BEFORE delivery
    assert lanes.checkout_issue(saved["publish_pending"]["lane_snapshot"], root=store.root) is not None
    if row == "repaired":
        _repair_the_row(store, lane_id, worktree)
    kept = lanes.load_lanes(store)["lanes"][lane_id]["worktree_toplevel_canonical"]
    original_artifact = artifact.read_bytes()
    monkeypatch.chdir(_retry_folder(tmp_path, cwd))  # the retry may run from another folder than the delivery
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    output = capsys.readouterr()
    current = lanes.load_lanes(store)["lanes"][lane_id]
    assert current["publish_pending"] is False and current["status"] == lanes.STATUS_DELIVERED
    assert current["worktree_toplevel_canonical"] == kept  # not rewritten
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]
    if row == "damaged":  # cleanup would use the damaged field: it stays pending, for the person to repair
        assert "cleanup pending" in output.out and "worktree_toplevel_canonical" in output.err
        assert current["cleanup_pending"] is True and worktree.exists()
    else:  # the row was repaired, so the delivery also finishes its cleanup
        assert "cleanup pending" not in output.out and current["cleanup_pending"] is False
        assert not worktree.exists()
    lanes.validate_delivery_artifact(
        artifact, lane_id=lane_id, head_sha=head, store=store, require_isolation=True, require_live_marker=True
    )


@pytest.mark.parametrize("canonical", LEGACY_CANONICALS)
@pytest.mark.parametrize("evidence", ["missing", "invalid"])
def test_a_legacy_snapshot_still_refuses_without_a_valid_committed_final(tmp_path, monkeypatch, canonical, evidence):
    """The exception needs a valid committed final. Without one the damaged snapshot refuses as before and the
    registry is left byte for byte as it was."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch, canonical)
    if evidence == "missing":
        artifact.unlink()
    else:
        artifact.write_text("{}")
    before = lanes.lanes_path(store).read_bytes()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    assert lanes.lanes_path(store).read_bytes() == before
    assert lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]


def _real_lane_row(tmp_path):
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "verify-canonical")
    store = Store(root)
    return (
        store,
        "verify-canonical",
        dict(lanes.load_lanes(store)["lanes"]["verify-canonical"]),
        Path(lane["worktree_path"]),
    )


@pytest.mark.parametrize("cwd", RETRY_FOLDERS)
@pytest.mark.parametrize("canonical", LEGACY_CANONICALS)
def test_the_committed_snapshot_path_does_not_read_an_ambiguous_canonical_against_the_cwd(
    tmp_path, monkeypatch, canonical, cwd
):
    """With `check_paths=False` (a valid committed final exists) the worktree itself is still verified against Git, but
    a blank or relative `worktree_toplevel_canonical` is not turned into a folder by the retry process's cwd."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = canonical(worktree) if callable(canonical) else canonical
    monkeypatch.chdir(_retry_folder(tmp_path, cwd))
    found = cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert found["worktree_toplevel_canonical"] == lanes.canonical_host_path(worktree)


@pytest.mark.parametrize("canonical", ["", "relative/canonical"])
def test_the_normal_path_still_refuses_an_ambiguous_canonical(tmp_path, canonical):
    store, lane_id, row, _worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = canonical
    with pytest.raises(lanes.LaneError, match="worktree_toplevel_canonical"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id)


def test_an_absolute_canonical_that_really_differs_is_refused_on_the_committed_snapshot_path_too(tmp_path):
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = str(tmp_path / "some-other-folder")
    with pytest.raises(lanes.LaneError, match="no longer matches git"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    row["worktree_toplevel_canonical"] = lanes.canonical_host_path(worktree)  # the right absolute one still passes
    assert cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)["head"]


def test_the_committed_snapshot_path_still_verifies_the_worktree_path_against_git(tmp_path):
    store, lane_id, row, _worktree = _real_lane_row(tmp_path)
    row["worktree_path"] = str(tmp_path / "not-a-checkout")
    row["worktree_toplevel_canonical"] = ""
    with pytest.raises(lanes.LaneError) as caught:
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert not isinstance(caught.value, cli._LaneRetryFromDeliveryFolder)  # an absolute path is not a folder problem


def test_checkout_identity_and_cleanliness_stay_in_force_on_the_committed_snapshot_path(tmp_path):
    from test_lanes import _git_rc

    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = ""
    assert cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)["tracked_tree_clean"] is True
    primary = dict(row, worktree_path=str(store.root))  # the primary checkout is not a lane worktree
    with pytest.raises(lanes.LaneError, match="primary checkout"):
        cli._verify_lane_worktree(store, primary, lane_id=lane_id, check_paths=False)
    with pytest.raises(lanes.LaneError, match="does not match"):  # another lane's branch
        cli._verify_lane_worktree(store, dict(row, lane_id="some-other-lane"), lane_id=lane_id, check_paths=False)
    (worktree / "src/core/a.py").write_text("tracked change\n", encoding="utf-8")
    assert _git_rc(worktree, "status", "--porcelain").stdout.strip()
    with pytest.raises(lanes.LaneError, match="staged/unstaged tracked changes"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)


def test_only_the_two_committed_final_rebind_checks_skip_the_saved_path_rule():
    """Every other caller keeps the normal checks: a fresh delivery, cleanup, gc, assignment, abandon and the rest never
    pass `check_paths`. The only places that name it are the rebind (twice) and the candidate passing it on."""
    import inspect

    source = inspect.getsource(cli)
    uses = re.findall(r"(?<!`)check_paths=\w+", source)  # the names in quoted prose are in backticks
    assert sorted(uses) == ["check_paths=check_paths", "check_paths=paths_matter", "check_paths=paths_matter"], uses
    rebind = inspect.getsource(cli._lane_rebind_pending_delivery)
    assert rebind.count("check_paths=paths_matter") == 2 and "paths_matter = committed is None" in rebind


def _prepared_only_delivery(tmp_path, monkeypatch, canonical=None, relative_worktree=False):
    """A pending delivery an older version left BEFORE its final was committed (only the prepared evidence exists)."""
    from test_lanes import _assign_worktree, _commit, _committed_artifacts, _prepared_artifacts, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane_id = "prepared-damage"
    lane = _assign_worktree(root, base, lane_id)
    worktree = Path(lane["worktree_path"])
    _commit(worktree, "src/core/a.py", "base\nprepared delivery\n")
    store = Store(root)
    real_save = lanes.save_lanes
    data = lanes.load_lanes(store)
    if canonical is not None:
        data["lanes"][lane_id]["worktree_toplevel_canonical"] = (
            canonical(worktree) if callable(canonical) else canonical
        )
    if relative_worktree:
        data["lanes"][lane_id]["worktree_path"] = os.path.relpath(worktree, os.getcwd())
    real_save(store, data)
    failed = []

    def save_then_fail(target_store, data):
        real_save(target_store, data)
        if data["lanes"][lane_id].get("status") == lanes.STATUS_DELIVERED and not failed:
            failed.append(True)
            raise OSError("injected failure after the first delivery state save")

    with monkeypatch.context() as fault:
        fault.setattr(lanes, "save_lanes", save_then_fail)
        fault.setattr(lanes, "require_checkout_paths", lambda *args, **kwargs: None)  # the older version had none
        assert _run_raw(["lane", "deliver", "--id", lane_id, "--from", "dev"], root) == 2
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert isinstance(saved["publish_pending"], dict) and _committed_artifacts(store, lane_id) == []
    assert len(_prepared_artifacts(store, lane_id)) == 1
    return store, lane_id, worktree


def test_a_committed_final_is_kept_when_only_the_retry_folder_makes_a_relative_worktree_path_unreadable(
    tmp_path, monkeypatch, capsys
):
    """A row written by hand with a RELATIVE worktree path can only be checked from the folder the delivery was made in.
    A retry from another folder is refused for that reason alone, so it must not quarantine the valid final or undo
    the pending delivery: nothing changes, and the advice says to retry from the original folder, where it publishes."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree, head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, relative_worktree=True
    )
    assert not os.path.isabs(
        lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]["lane_snapshot"]["worktree_path"]
    )
    before = lanes.lanes_path(store).read_bytes()
    original_artifact = artifact.read_bytes()
    monkeypatch.chdir(_retry_folder(tmp_path, "sibling"))
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    err = capsys.readouterr().err.lower()
    assert "relative" in err and "retry" in err and "folder" in err
    assert lanes.lanes_path(store).read_bytes() == before  # the pending delivery and the row are untouched
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]
    assert not list((store.dir / "lane-deliveries").rglob(".recovery-quarantine*"))
    monkeypatch.chdir(tmp_path)  # the folder the delivery was made in
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    assert lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"] is False
    assert _committed_artifacts(store, lane_id) == [artifact]
    lanes.validate_delivery_artifact(
        artifact, lane_id=lane_id, head_sha=head, store=store, require_isolation=True, require_live_marker=True
    )


@pytest.mark.parametrize("path", ["relative", "absolute"])
def test_a_relative_worktree_path_with_another_kind_of_failure_takes_the_normal_terminal_rollback(
    tmp_path, monkeypatch, capsys, path
):
    """The keep-it-pending advice is only for a failure caused by reaching the relative checkout. Here the checkout is
    reachable (the retry runs from the delivery's folder) but the target ref no longer resolves: that is a real
    terminal failure, so the valid final is set aside and the lane is restored active, exactly as for any other row.
    The absolute-path row is the control: both exit 3 with the same outcome, and `lane recover` then has nothing to
    send the person back to `lane deliver` for."""
    from test_lanes import _committed_artifacts, _git_rc

    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, relative_worktree=path == "relative"
    )
    target = lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]["lane_snapshot"]["target_ref"]
    assert _git_rc(store.root, "branch", "-m", target, "renamed-" + target).returncode == 0  # the target is gone
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 3
    err = capsys.readouterr().err.lower()
    assert "no consumable evidence was published" in err and "restored active" in err
    assert "committed with a relative" not in err and "retry `lane deliver`" not in err
    assert _committed_artifacts(store, lane_id) == []  # set aside, as for any terminal failure
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ACTIVE
    assert artifact.exists() is False
    assert (
        cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "probe"]) != 3
    )  # nothing left to deliver again
    assert "authoritative committed final exists" not in capsys.readouterr().err


def test_a_dirty_tracked_file_retried_from_the_delivery_folder_gets_no_retry_from_that_folder_advice(
    tmp_path, monkeypatch, capsys
):
    """The advice must be honest: the checkout is reachable from the folder the delivery was made in, so a refusal there
    is about the checkout itself (a changed tracked file), not about the folder, and says nothing about retrying."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree, _head, _artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, relative_worktree=True
    )
    (worktree / "src/core/a.py").write_text("a tracked change that was never committed\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    err = capsys.readouterr().err.lower()
    assert "tracked changes" in err and "no consumable evidence was published" in err
    assert "relative worktree path" not in err and "retry `lane deliver`" not in err and "from that folder" not in err
    assert _committed_artifacts(store, lane_id) == []


def test_a_relative_worktree_path_without_a_committed_final_is_still_set_aside_as_before(tmp_path, monkeypatch, capsys):
    """The keep-it-pending rule is only for a delivery whose final is committed and valid. A delivery that never got
    that far still takes the normal terminal refusal: nothing is published and the lane is restored."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree = _prepared_only_delivery(tmp_path, monkeypatch, relative_worktree=True)
    monkeypatch.chdir(_retry_folder(tmp_path, "sibling"))
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    err = capsys.readouterr().err.lower()
    assert "no consumable evidence was published" in err and "committed with a relative" not in err
    assert _committed_artifacts(store, lane_id) == []


@pytest.mark.parametrize("canonical", LEGACY_CANONICALS)
def test_a_legacy_snapshot_without_a_committed_final_is_still_refused(tmp_path, monkeypatch, canonical):
    """No committed final yet, so there is nothing to vouch for the damaged snapshot: it is refused and nothing is
    published, even after a person has repaired the registry row."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree = _prepared_only_delivery(tmp_path, monkeypatch, canonical)
    _repair_the_row(store, lane_id, worktree)
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    assert _committed_artifacts(store, lane_id) == []


# ------------------------------------------------ status reads this run's findings, never a key stored in the row

RECOVERY_ROW = {"status": lanes.STATUS_DELIVERED, "publish_pending": False, "cleanup_pending": True}


def _plain_status(store, capsys):
    capsys.readouterr()
    assert _run(store, "lane", "status") == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("fields", [RECOVERY_ROW, {"status": lanes.STATUS_ACTIVE}], ids=["recovery", "active"])
def test_a_healthy_row_that_carries_a_stored_checkout_path_error_is_shown_healthy(keyed, capsys, fields):
    """`load_lanes` keeps extra fields, so a hand edit can leave this key in a healthy row. Only the errors found in
    this run mark a row damaged; the stored key is data, never an annotation, and it hides no real diagnosis."""
    clean = keyed(**fields)
    clean_rows = {r["assignee"]: r for r in _status_rows(clean, capsys)}
    clean_plain = _plain_status(clean, capsys)
    stored = "a hand-edited note that is not an error from this run"
    carrying = keyed(checkout_path_error=stored, **fields)
    rows = {r["assignee"]: r for r in _status_rows(carrying, capsys)}
    shown = rows["bad-seat"]
    assert shown["checkout_path_error"] == stored  # the stored field comes back as stored, untouched
    assert shown.get("recovery_state") == clean_rows["bad-seat"].get("recovery_state") != "damaged checkout"
    assert shown.get("recovery_detail") == clean_rows["bad-seat"].get("recovery_detail")
    plain = _plain_status(carrying, capsys)
    assert "damaged checkout" not in plain and stored not in plain
    assert plain == clean_plain  # the plain report is exactly the healthy one


@pytest.mark.parametrize("fields", [RECOVERY_ROW, {"status": lanes.STATUS_ACTIVE}], ids=["recovery", "active"])
def test_a_really_damaged_row_is_annotated_with_this_runs_error_not_the_stored_one(keyed, capsys, fields):
    store = keyed(worktree_path="\t", checkout_path_error="old stored text", **fields)
    before = lanes.lanes_path(store).read_bytes()
    shown = next(r for r in _status_rows(store, capsys) if r["assignee"] == "bad-seat")
    assert _hint(BAD) in shown["checkout_path_error"] and "old stored text" not in shown["checkout_path_error"]
    if fields is RECOVERY_ROW:
        assert (
            shown["recovery_state"] == "damaged checkout" and shown["recovery_detail"] == shown["checkout_path_error"]
        )
    plain = _plain_status(store, capsys)
    assert "damaged checkout: lanes.json lane 'bad': checkout path worktree_path" in plain
    assert "old stored text" not in plain
    assert lanes.lanes_path(store).read_bytes() == before  # nothing is written back


def test_status_does_not_let_one_rows_error_leak_to_another(keyed, capsys):
    """The annotation belongs to the row it was found for: the healthy lane next to a damaged one stays clean."""
    store = keyed(worktree_path="\t", status=lanes.STATUS_ACTIVE)
    data = lanes.load_lanes(store)
    data["lanes"][OTHER]["status"] = lanes.STATUS_ACTIVE
    lanes.save_lanes(store, data)
    rows = {r["assignee"]: r for r in _status_rows(store, capsys)}
    assert "checkout_path_error" in rows["bad-seat"] and "checkout_path_error" not in rows["other-seat"]


# ----------------------- a caller-relative worktree path whose real folder is somewhere else is not "gone"


def _git_repo_store(tmp_path, monkeypatch):
    """A store inside a real Git repository, with a lane row whose checkout field is damaged (so the acknowledgement
    owner picks the surviving path) and whose worktree path is relative."""
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, capture_output=True, text=True
        )

    assert git("init", "-q", "-b", "master").returncode == 0
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert git("add", "f.txt").returncode == 0 and git("commit", "-q", "-m", "base").returncode == 0
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    store.set_role("lead", "lead")
    lane = lanes.new_lane(
        "rel",
        assignee="dev",
        assigned_by="lead",
        assigned_at="t",
        domain_id="core",
        path_subset=[],
        base_sha="a" * 40,
        target_ref="master",
        target_head_at_assign="a" * 40,
        epoch_at_assign=None,
        registry_hash_at_assign="hash",
    )
    lane.update(worktree_path=".worktrees/relative", worktree_toplevel_canonical="\t")
    lanes.save_lanes(store, {"schema_version": 1, "lanes": {"rel": lane}})
    return store, git


def _abandon_acknowledging(store):
    return cli.main(["--root", str(store.root), "lane", "abandon", "--id", "rel", "--acknowledge-missing-worktree"])


@pytest.mark.parametrize(
    "where", ["registered-elsewhere", "registered-in-the-cwd", "plain-folder-in-the-cwd", "gone-everywhere"]
)
def test_a_relative_worktree_path_is_refused_with_repair_advice_wherever_the_real_folder_is(
    tmp_path, monkeypatch, capsys, where
):
    store, git = _git_repo_store(tmp_path, monkeypatch)
    (store.root / ".worktrees").mkdir()  # the repository-relative parent exists; its leaf does not
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = elsewhere / ".worktrees" / "relative"
    if where.startswith("registered"):  # a real Git worktree where the delivery was made, registered with Git
        (elsewhere / ".worktrees").mkdir()
        assert git("worktree", "add", "-q", "-b", "lane/rel", str(real)).returncode == 0
        monkeypatch.chdir(elsewhere if where == "registered-in-the-cwd" else tmp_path)
    elif where == "plain-folder-in-the-cwd":
        real.mkdir(parents=True)
        (real / "keep.txt").write_text("keep", encoding="utf-8")
        monkeypatch.chdir(elsewhere)
    else:
        monkeypatch.chdir(elsewhere)
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    assert _abandon_acknowledging(store) == 2
    err = capsys.readouterr().err
    assert "is not absolute" in err and ADVICE in err and REPAIR in err and "'rel'" in err
    assert lanes.lanes_path(store).read_bytes() == before  # nothing acknowledged
    assert real.exists() == (where != "gone-everywhere")


def test_a_relative_worktree_path_is_refused_when_git_cannot_list_worktrees(damaged_lane, monkeypatch, capsys):
    """The earlier search treated "Git could not say" as "no worktrees" and went on to acknowledge. Nothing is looked up
    now, so an unavailable listing cannot matter: the refusal does not depend on Git at all."""
    store = damaged_lane("worktree_toplevel_canonical", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_path"] = ".worktrees/relative"
    lanes.save_lanes(store, data)
    monkeypatch.setattr(cli, "_git", lambda *args, **kwargs: (128, ""))
    before = lanes.lanes_path(store).read_bytes()
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 2
    )
    assert REPAIR in capsys.readouterr().err and lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("spelling", ["../outside/checkout", "sub/../../outside/checkout", "./.worktrees/x"])
def test_a_dotted_relative_worktree_path_is_refused_the_same_way(damaged_lane, capsys, spelling):
    """A `..` spelling never matched Git's normalized absolute paths in the earlier search. There is no search now:
    every relative spelling gets the same refusal and the same advice."""
    store = damaged_lane("worktree_toplevel_canonical", "\t")
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_path"] = spelling
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    assert (
        cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
        == 2
    )
    err = capsys.readouterr().err
    assert repr(spelling) in err and ADVICE in err and REPAIR in err and lanes.lanes_path(store).read_bytes() == before


def test_another_lanes_checkout_with_the_same_tail_does_not_change_the_refusal(tmp_path, monkeypatch, capsys):
    """The earlier search let another lane's registered checkout with the same tail block the missing lane with a path
    that was not its own. The refusal now names this lane's own stored path and nothing else."""
    store, git = _git_repo_store(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere" / ".worktrees" / "relative"
    other.parent.mkdir(parents=True)
    assert git("worktree", "add", "-q", "-b", "lane/other", str(other)).returncode == 0
    capsys.readouterr()
    assert _abandon_acknowledging(store) == 2
    err = capsys.readouterr().err
    assert "'.worktrees/relative'" in err and ADVICE in err and REPAIR in err and str(other) not in err


def test_only_a_relative_worktree_path_on_the_committed_snapshot_path_asks_for_the_original_folder(tmp_path):
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = lanes.canonical_host_path(worktree)  # a healthy absolute canonical
    row["worktree_path"] = "no/such/relative/checkout"
    with pytest.raises(cli._LaneRetryFromDeliveryFolder, match="relative worktree path"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    with pytest.raises(lanes.LaneError) as caught:  # the normal path: the same row is simply refused
        cli._verify_lane_worktree(store, row, lane_id=lane_id)
    assert not isinstance(caught.value, cli._LaneRetryFromDeliveryFolder)


# ------------------------------------------------------ the advice says `lane recover` first for an unfinished delivery


def test_the_checkout_advice_depends_on_the_lane_state() -> None:
    plain = lanes.checkout_path_recovery(lane_id="x", lane={"status": lanes.STATUS_ACTIVE})
    assert "lane abandon --id x --acknowledge-missing-worktree" in plain and "lane recover" not in plain
    unfinished = {"status": lanes.STATUS_DELIVERED, "publish_pending": {"transaction_id": "t"}}
    advice = lanes.checkout_path_recovery(lane_id="x", lane=unfinished)
    assert "lane recover --id x --reason" in advice and "lane abandon --id x --acknowledge-missing-worktree" in advice
    assert advice.index("lane recover") < advice.index("lane abandon")  # recover first, then retire
    finished = {"status": lanes.STATUS_DELIVERED, "publish_pending": False}
    assert "lane recover" not in lanes.checkout_path_recovery(lane_id="x", lane=finished)
    assert "lane recover" not in lanes.checkout_path_recovery(lane_id="x")  # no row, no state to read


def test_a_delivered_lane_with_an_unfinished_delivery_and_a_blank_checkout_is_told_to_recover_first(
    tmp_path, monkeypatch, capsys
):
    """Prepared-only delivery (no committed final) and a blank checkout field: `lane abandon --acknowledge-missing-
    worktree` refuses in that state, so the advice must not lead with it. `lane recover` then works."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree = _prepared_only_delivery(tmp_path, monkeypatch, canonical="")
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert row["status"] == lanes.STATUS_DELIVERED and isinstance(row["publish_pending"], dict)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "workspace", "--id", lane_id]) == 2
    err = capsys.readouterr().err
    assert f"lane recover --id {lane_id} --reason" in err and err.index("lane recover") < err.index("lane abandon")
    assert cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "advice"]) == 0
