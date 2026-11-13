"""Damaged checkout paths are refused or reported without being used as folders."""

import json
import sys
from pathlib import Path

import pytest

from agenttalk import cli, ephemeral, lane_occupancy, lanes, supervisor
from agenttalk.store import Store
from agenttalk.wrapper import run as wrapper_run


def _committed_unpublished_delivery(tmp_path, monkeypatch):
    from test_lanes import _assign_worktree, _commit, _committed_artifacts, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane_id = "committed-damage"
    lane = _assign_worktree(root, base, lane_id)
    worktree = Path(lane["worktree_path"])
    head = _commit(worktree, "src/core/a.py", "base\ncommitted delivery\n")
    store = Store(root)
    real_save = lanes.save_lanes

    def fail_publication_marker(target_store, data):
        if data["lanes"][lane_id].get("publish_pending") is False:
            raise OSError("injected publication marker save failure")
        return real_save(target_store, data)

    with monkeypatch.context() as fault:
        fault.setattr(lanes, "save_lanes", fail_publication_marker)
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
            row, store.root / "other", store.root, source="lanes.json lane damaged", lane=True
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
    advice = lanes.checkout_path_recovery(identifier)
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


@pytest.mark.parametrize("case", ["gone", "exists", "used", "unresolved"])
def test_damaged_relative_exit_uses_the_repository_root(damaged_lane, capsys, monkeypatch, case):
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
    assert cli.main(
        ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    ) == (0 if case == "gone" else 2)
    output = capsys.readouterr()
    if case == "gone":
        assert lanes.load_lanes(store)["lanes"]["damaged"]["worktree_path"] == ".worktrees/relative"
        assert not output.err
    else:
        reason = {
            "exists": "still exists",
            "used": "active or pending launch",
            "unresolved": "could not be established",
        }[case]
        assert reason in output.err
        assert lanes.lanes_path(store).read_bytes() == before
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
    assert worktree_release._names_checkout(row, root / row["worktree_path"], root, lane=True)
    assert not worktree_release._names_checkout(row, root / "unrelated", root, lane=True)
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
            row, store.root / "other", store.root, source="lanes.json lane damaged", lane=True
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
        worktree_toplevel_canonical="healthy-checkout",
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
    lanes.require_checkout_paths(row, "damaged")  # raises nothing; no AttributeError for any caller


def test_an_empty_registry_key_gets_the_placeholder_not_another_lanes_id():
    """The embedded `lane_id` can name another valid lane. An explicitly given key, even an empty one, is never
    replaced by it, so the hint is never a runnable abandon command for a different lane."""
    row = {"lane_id": "other-lane", "worktree_path": "\t", "worktree_toplevel_canonical": "\t"}
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row, "")
    text = str(caught.value)
    assert "lane abandon --id <lane-id> --acknowledge-missing-worktree" in text
    assert "other-lane" not in text


def test_a_missing_registry_key_still_falls_back_to_the_embedded_id():
    row = {"lane_id": "named-inside", "worktree_path": "\t"}
    with pytest.raises(lanes.LaneError) as caught:
        lanes.require_checkout_paths(row)
    assert "lane abandon --id named-inside --acknowledge-missing-worktree" in str(caught.value)
