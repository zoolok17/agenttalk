"""Damaged checkout paths are refused or reported without being used as folders."""

import json
import ntpath
import os
import re
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from agenttalk import cli, ephemeral, lane_occupancy, lanes, supervisor
from agenttalk.store import Store
from agenttalk.wrapper import run as wrapper_run


@pytest.mark.parametrize("saved", ["/p/co//wt", "{ROOT}/co//wt"])
def test_old_doubled_slash_canonical_is_damage_with_repair_advice(tmp_path, monkeypatch, saved):
    import posixpath

    monkeypatch.setattr(lanes, "os", types.SimpleNamespace(name="posix", path=posixpath))
    row = {"worktree_toplevel_canonical": saved}
    problem = lanes.canonical_checkout_problem(row, root="/p")
    assert problem is not None and "older version" in problem[1]
    with pytest.raises(lanes.LaneError, match="real absolute folder"):
        lanes.require_checkout_paths(row, lane_id="test", root="/p")


@pytest.mark.parametrize(
    "raw,retained,expected",
    [
        ("/p/target", [], ("other", True)),
        ("/p/other", [], None),
        ("", [], ("other", False)),
        ("relative", [], ("other", False)),
        ("bad\x00name", [], ("other", False)),
        ("/p/other", ["/p/old//checkout"], ("other", False)),
        ("/p/target", ["/p/old//checkout"], ("other", True)),
        ("/p/other", ["/p/old//checkout", "/p/target"], ("other", True)),
        ("/p/other", ["/p/target"], ("other", True)),
        ("", ["/p/target"], ("other", True)),
    ],
)
def test_guarded_paths_do_not_hide_healthy_claims(monkeypatch, raw, retained, expected):
    import posixpath

    fake_os = types.SimpleNamespace(name="posix", path=posixpath)
    monkeypatch.setattr(lanes, "os", fake_os)
    monkeypatch.setattr(cli, "_strict_real", lambda path: str(path))
    monkeypatch.setattr(cli.os.path, "samefile", lambda a, b: False)
    row = {"worktree_path": raw, "worktree_toplevel_canonical": "/p/old//checkout",
           "occupancy_worktrees": retained}
    assert cli._lane_checkout_owned_by_another(types.SimpleNamespace(root="/p"), "mine", "/p/target",
                                                rows={"other": row}) == expected


@pytest.mark.parametrize("raw,canonical,claim,expected", [
    ("/p/other", "/p/other", "{ROOT}/old//checkout", ("other", False)),
    ("", "/p/other", "/p/old//checkout", ("other", False)),
    ("relative", "/p/other", "/p/old//checkout", ("other", False)),
    ("bad\x00name", "/p/other", "/p/old//checkout", ("other", False)),
    ("/p/other", "/p/target", "/p/old//checkout", ("other", True)),
])
def test_guarded_retained_claim_cannot_be_dismissed(monkeypatch, raw, canonical, claim, expected):
    import posixpath

    monkeypatch.setattr(lanes, "os", types.SimpleNamespace(name="posix", path=posixpath))
    monkeypatch.setattr(cli, "_strict_real", lambda path: str(path))
    monkeypatch.setattr(cli.os.path, "samefile", lambda a, b: False)
    row = {"worktree_path": raw, "worktree_toplevel_canonical": canonical, "occupancy_worktrees": [claim]}
    assert cli._lane_checkout_owned_by_another(types.SimpleNamespace(root="/p"), "mine", "/p/target",
                                                rows={"other": row}) == expected


@pytest.mark.parametrize("raw,expected", [("/p/real", "worktree_path"), ("/p/missing", None), ("", None)])
def test_surviving_entry_never_probes_a_guarded_canonical(monkeypatch, raw, expected):
    import posixpath

    def lstat(path):
        assert "//" not in path
        if path != "/p/real":
            raise FileNotFoundError(path)
        return types.SimpleNamespace(st_mode=stat.S_IFDIR)

    monkeypatch.setattr(lanes, "os", types.SimpleNamespace(name="posix", path=posixpath, lstat=lstat))
    row = {"worktree_path": raw, "worktree_toplevel_canonical": "/p/co//wt"}
    found = lanes.surviving_checkout_entry(row, root="/p")
    assert (found[0] if found else None) == expected


def _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical=None, relative_worktree=False, with_other_lane=False):
    """A delivery whose committed final exists but whose publication marker was never saved.

    `canonical` models a delivery made by an OLDER version, which did not look at the saved checkout paths: the row's
    `worktree_toplevel_canonical` is already that value (a string, or a function of the checkout folder) BEFORE the
    delivery, so the signed snapshot and the final's lane fingerprint both carry the damage, and the path rule is
    switched off for that one delivery. `with_other_lane` assigns a second, healthy lane ("other") through the public
    command first, so the lane being delivered is not the only one in the registry."""
    from test_lanes import _assign_worktree, _commit, _committed_artifacts, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane_id = "committed-damage"
    lane = _assign_worktree(root, base, lane_id)
    if with_other_lane:
        _assign_second_lane(root, base)
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
@pytest.mark.parametrize("damage", ["", 7])
def test_the_cleanup_warning_after_a_successful_committed_delivery_names_a_next_step_that_runs(
    tmp_path, monkeypatch, capsys, field, damage
):
    """The warning is built from the row as it is saved AFTER publication. Built from the row as it was before
    publication (delivery still unfinished) it sent the person to `lane recover`, which refuses a lane whose delivery
    is complete. The printed command is run as printed; `lane recover` is the control that cannot run here."""
    import shlex

    store, lane_id, worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id][field] = damage
    lanes.save_lanes(store, data)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    err = capsys.readouterr().err
    assert "worktree cleanup is pending" in err and field in err
    assert "lane recover" not in err
    printed = re.findall(r"`(lane abandon --id [^`]+)`", err)
    assert lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"] is False
    assert cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "probe"]) == 2
    assert "no incomplete delivered transaction to recover" in capsys.readouterr().err  # the old advice cannot run
    # The damage is in one of the two saved fields and the other still names the checkout, which is still there: the
    # acknowledgement is refused, so it is not printed. The person repairs the field, then the printed plain
    # `lane abandon` runs and removes the checkout.
    assert "names a folder that still exists" in err and worktree.exists()
    assert printed == [f"lane abandon --id {lane_id}"] and "--acknowledge-missing-worktree" not in err
    assert f"set {field} in lanes.json lane {lane_id!r} to its real absolute folder" in err
    data = lanes.load_lanes(store)
    data["lanes"][lane_id][field] = str(worktree) if field == "worktree_path" else lanes.canonical_host_path(worktree)
    lanes.save_lanes(store, data)
    assert cli.main(["--root", str(store.root), *shlex.split(printed[0])]) == 0  # the printed step runs
    assert not worktree.exists()
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ABANDONED


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
    _follow_the_whitespace_advice(store, capsys, field, damage, lane_id=lane_id)
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


def test_a_committed_final_is_kept_when_another_lanes_healthy_checkout_cannot_be_examined(
        tmp_path, monkeypatch, capsys):
    """Round 32 (review bot, P1): the retry of a delivery whose committed final is valid, while ANOTHER lane's healthy
    absolute checkout cannot be examined (access denied). Not being able to look is not the same as that lane owning
    the folder or its path being unreadable as a full path: the final must be kept and the delivery published, not set
    aside and the lane put back to ACTIVE. Two lanes assigned the ordinary way; the denial is injected into the
    resolution of the other lane's folder only (a real permission denial is not possible on every system)."""
    from test_lanes import _checkout_cannot_be_resolved

    store, lane_id, _worktree, head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, with_other_lane=True)
    other = Path(lanes.load_lanes(store)["lanes"]["other"]["worktree_path"])
    original_artifact = artifact.read_bytes()
    fired = _checkout_cannot_be_resolved(monkeypatch, other)
    capsys.readouterr()

    rc = cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"])

    err = capsys.readouterr().err
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert fired, "the other lane's folder was never examined: the set-up does not reach the fault"
    assert artifact.is_file() and artifact.read_bytes() == original_artifact, err   # kept, not quarantined
    assert row["status"] != lanes.STATUS_ACTIVE, err                                # not put back to ACTIVE
    assert rc == 0 and row["publish_pending"] is False, err                         # and the delivery goes through
    lanes.validate_delivery_artifact(artifact, lane_id=lane_id, head_sha=head, store=store, require_isolation=True)
    assert (other / ".git").exists()                                                # the other lane is untouched


def _two_ordinary_lanes(tmp_path):
    """Two lanes assigned the ordinary way, each with a healthy absolute checkout, and work committed in the first.
    Returns (root, store, first_checkout, other_checkout)."""
    from test_lanes import _assign_worktree, _commit, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    _commit(first, "src/core/a.py", "base\nfirst lane work\n")
    return root, Store(root), first, other


def test_a_fresh_delivery_says_it_could_not_look_when_another_lanes_checkout_cannot_be_examined(
        tmp_path, monkeypatch, capsys):
    """Nothing was published yet, so a fresh delivery stops. The reason is the true one (a look that could not be made,
    which a retry can fix), not that the other lane's saved path is "not a full path" (advice to edit lanes.json)."""
    from test_lanes import _checkout_cannot_be_resolved

    root, store, first, other = _two_ordinary_lanes(tmp_path)
    fired = _checkout_cannot_be_resolved(monkeypatch, other)
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "first", "--from", "dev"])

    err = capsys.readouterr().err
    assert fired and rc == 3
    assert "whether another lane owns this checkout could not be established" in err, err
    assert "not a full path" not in err, err
    _nothing_was_published(store, "first")
    _assert_untouched(first, other)


def test_an_abandon_says_it_could_not_look_when_another_lanes_checkout_cannot_be_examined(
        tmp_path, monkeypatch, capsys):
    """A removal never goes on without the look. The checkout is left alone, and the reason is the true one."""
    from test_lanes import _checkout_cannot_be_resolved

    root, _store, first, other = _two_ordinary_lanes(tmp_path)
    fired = _checkout_cannot_be_resolved(monkeypatch, other)
    capsys.readouterr()

    cli.main(["--root", str(root), "lane", "abandon", "--id", "first"])

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert fired
    assert "could not be established" in text, text
    assert "not a full path" not in text, text
    _assert_untouched(first, other)


def test_a_terminal_recheck_uses_its_proof_when_another_lanes_checkout_can_no_longer_be_examined(
        tmp_path, monkeypatch, capsys):
    """The fresh evaluation scanned the other lane's row and proved the checkout is this lane's. When that same
    command's terminal recheck can no longer look at the other lane's folder, its recorded proof stands and the
    delivery is published (the cleanup after it is a removal, so it keeps the checkout)."""
    from test_lanes import _checkout_cannot_be_resolved

    root, store, first, other = _two_ordinary_lanes(tmp_path)
    fired = _checkout_cannot_be_resolved(monkeypatch, other, only_during="_lane_finalize_delivery")
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "first", "--from", "dev"])

    err = capsys.readouterr().err
    row = lanes.load_lanes(store)["lanes"]["first"]
    assert fired, "the other lane's folder was never examined during the terminal recheck"
    assert rc == 0 and row["status"] == lanes.STATUS_DELIVERED and row["publish_pending"] is False, err
    assert Path(row["delivery_artifact"]).is_file()
    _assert_untouched(first, other)


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
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
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
    _follow_the_whitespace_advice(store, capsys, field, blank)
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
def test_acknowledgement_checks_surviving_path_without_touching_content(
    damaged_lane, monkeypatch, capsys, field, blank
):
    store = damaged_lane(field, blank)
    blank = _follow_the_whitespace_advice(store, capsys, field, blank)

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
    _follow_the_whitespace_advice(store, capsys, field, "\t")
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
    _follow_the_whitespace_advice(store, capsys, field, "\t")
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
def test_damaged_exit_compares_surviving_path_with_launch_markers(damaged_lane, monkeypatch, capsys, field, matching):
    store = damaged_lane(field, "\t")
    _follow_the_whitespace_advice(store, capsys, field, "\t")
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
    _follow_the_whitespace_advice(store, capsys, "worktree_path", "\t")
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
    _follow_the_whitespace_advice(store, capsys, "worktree_path", "\t")
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


def test_acknowledgement_handles_two_damaged_fields_and_an_unrelated_marker(damaged_lane, monkeypatch, capsys):
    store = damaged_lane("worktree_path", "\t")
    _follow_the_whitespace_advice(store, capsys, "worktree_path", "\t")
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
REPAIR = "Replace worktree_path in lanes.json lane"


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
    assigned = lanes.load_lanes(store)["lanes"]["relative"]
    # Since #802 a relative --worktrees-root is resolved to one absolute folder under the store root, so a fresh
    # assignment records an absolute path. The rest of the round trip is about how a RELATIVE row (the spelling an older
    # agenttalk recorded, relative to the store root) is read, so that row is rewritten to it, keeping the rest as is.
    assert Path(assigned["worktree_path"]).is_absolute()
    assert Path(assigned["worktree_path"]).resolve().is_relative_to(root.resolve())
    data = lanes.load_lanes(store)
    data["lanes"]["relative"]["worktree_path"] = os.path.relpath(assigned["worktree_path"], root)
    lanes.save_lanes(store, data)
    row = lanes.load_lanes(store)["lanes"]["relative"]
    assert not Path(row["worktree_path"]).is_absolute() and row["worktree_path"].startswith(".worktrees")
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
    _follow_the_whitespace_advice(store, capsys, field, "\t")
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
    _follow_the_whitespace_advice(store, capsys, "worktree_path", "\t")
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


def test_healthy_missing_checkout_acknowledgement_does_not_read_git_list(tmp_path, damaged_lane, monkeypatch):
    store = damaged_lane("worktree_path", str(tmp_path / "gone"))
    _update_row(store, "damaged", worktree_toplevel_canonical=str(store.root / "gone"))
    monkeypatch.setattr(cli, "_worktree_records_strict", lambda *_: pytest.fail("Git list must not be read"))
    real_git = cli._git
    monkeypatch.setattr(cli, "_git", lambda root, argv, **kw: pytest.fail("Git list must not be read")
                        if argv[:2] == ["worktree", "list"] else real_git(root, argv, **kw))
    assert cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged",
                     "--acknowledge-missing-worktree"]) == 0


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


def _unfinished_delivery_without_a_final(tmp_path, monkeypatch, canonical):
    """A delivery that stopped BEFORE its final was committed: the pending delivery holds a signed snapshot, but no
    committed final exists. `canonical` is the damage the older version saved before the delivery."""
    from test_lanes import _assign_worktree, _commit, _committed_artifacts, _repo, _run_raw

    root, base = _repo(tmp_path)
    lane_id = "no-final-damage"
    lane = _assign_worktree(root, base, lane_id)
    worktree = Path(lane["worktree_path"])
    _commit(worktree, "src/core/a.py", "base\nunpublished delivery\n")
    store = Store(root)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["worktree_toplevel_canonical"] = canonical
    lanes.save_lanes(store, data)
    with monkeypatch.context() as fault:
        fault.setattr(lanes, "publish_delivery_artifact", lambda *a, **k: (_ for _ in ()).throw(OSError("stop")))
        fault.setattr(lanes, "require_checkout_paths", lambda *args, **kwargs: None)  # the older version had none
        assert _run_raw(["lane", "deliver", "--id", lane_id, "--from", "dev"], root) == 2
    assert isinstance(lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"], dict)
    assert _committed_artifacts(store, lane_id) == []
    return store, lane_id, worktree


def _a_pending_delivery_with_a_blank_snapshot(tmp_path, monkeypatch, final=True):
    """A delivery made by an older version and left unfinished, whose signed snapshot inside the pending delivery holds
    a blank canonical path. `final`: its committed final exists (the publication marker was never saved), or it does
    not (it stopped before). The live row has since been repaired to an absolute path, so only the saved copy is
    damaged."""
    monkeypatch.chdir(tmp_path)
    if final:
        store, lane_id, worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch, "")
    else:
        store, lane_id, worktree = _unfinished_delivery_without_a_final(tmp_path, monkeypatch, "")
    _repair_the_row(store, lane_id, worktree)
    row = lanes.load_lanes(store)["lanes"][lane_id]
    snapshot = row["publish_pending"]["lane_snapshot"]
    assert row["status"] == lanes.STATUS_DELIVERED and lanes.delivery_recovery_required(row)
    assert lanes.checkout_issue(row, root=store.root) is None  # the live row is sound
    assert snapshot["status"] == lanes.STATUS_ACTIVE and snapshot["worktree_toplevel_canonical"] == ""
    return store, lane_id, worktree


def _release_check_refusal(store, target=None) -> str:
    """What the release check says about this store (it reads every lane row, so a damaged row refuses any release).
    `target` is the checkout the release is for (by default one no lane names)."""
    from agenttalk import janitor, worktree_release

    with pytest.raises(worktree_release.Refused) as refused:
        worktree_release._activity(janitor.JanitorConfig.load(store.root), target or store.root / "unrelated-checkout")
    return str(refused.value)


def _release_check_is_clean(store, target=None) -> None:
    from agenttalk import janitor, worktree_release

    worktree_release._activity(janitor.JanitorConfig.load(store.root), target or store.root / "unrelated-checkout")


@pytest.mark.parametrize("final", [True, False], ids=["committed-final", "no-final"])
def test_release_advice_for_a_pending_delivery_names_recover_first(tmp_path, monkeypatch, final):
    store, lane_id, _worktree = _a_pending_delivery_with_a_blank_snapshot(tmp_path, monkeypatch, final)
    advice = _release_check_refusal(store)
    assert f"run `lane recover --id {lane_id} --reason <why>` first" in advice
    assert "then repeat this command: it names what is left, if anything" in advice
    assert "names `lane deliver` when a committed final exists, or says which other lane overlaps it" in advice
    # no second step is promised: what is left depends on what recover did, and `lane abandon` with the
    # acknowledgement is refused when the checkout folder is still there (see the no-final test below)
    assert "lane abandon" not in advice and "to retire this lane without using its checkout" not in advice


def _printed_abandon_steps(advice: str) -> list[list[str]]:
    """Every `lane abandon ...` command the advice prints, as the arguments to run (so a step the advice promises is
    one the test takes)."""
    return [text.split()[1:] for text in re.findall(r"`([^`]+)`", advice) if text.startswith("lane abandon")]


def _step(store, capsys, *argv):
    capsys.readouterr()
    code = cli.main(["--root", str(store.root), "lane", *argv])
    return code, capsys.readouterr()


def test_release_advice_for_a_pending_delivery_with_a_committed_final_runs_step_by_step(tmp_path, monkeypatch, capsys):
    """Every printed step is taken in order, and each does what the advice says."""
    store, lane_id, worktree = _a_pending_delivery_with_a_blank_snapshot(tmp_path, monkeypatch, final=True)
    advice = _release_check_refusal(store)
    assert f"`lane recover --id {lane_id} --reason <why>` first" in advice
    # 1. recover: "names `lane deliver` when a committed final exists"
    code, out = _step(store, capsys, "recover", "--id", lane_id, "--reason", "follow the release advice")
    assert code == 3 and "run `lane deliver`" in out.err
    # 2. what it names: the delivery finishes, and its cleanup removes the checkout
    code, out = _step(store, capsys, "deliver", "--id", lane_id, "--from", "dev")
    assert code == 0 and f"delivered lane {lane_id}" in out.out
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert row["publish_pending"] is False and row["cleanup_pending"] is False and not worktree.exists()
    # 3. "repeat this command: it names what is left, if anything": nothing is left
    _release_check_is_clean(store)
    for argv in _printed_abandon_steps(advice):  # none is printed; a printed step would have to be accepted here
        assert _step(store, capsys, *argv)[0] == 0


def test_release_advice_for_a_pending_delivery_without_a_final_runs_step_by_step(tmp_path, monkeypatch, capsys):
    """The branch where recover restores the lane to ACTIVE with its checkout still on disk."""
    store, lane_id, worktree = _a_pending_delivery_with_a_blank_snapshot(tmp_path, monkeypatch, final=False)
    row_before = lanes.load_lanes(store)["lanes"][lane_id]
    advice = _release_check_refusal(store)
    assert worktree.exists() and f"`lane recover --id {lane_id} --reason <why>` first" in advice
    # 1. recover: "restores the lane to ACTIVE" (and names nothing else: there is no final to deliver)
    code, out = _step(store, capsys, "recover", "--id", lane_id, "--reason", "follow the release advice")
    assert code == 0 and f"recovered lane {lane_id} to ACTIVE" in out.out
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert row["status"] == lanes.STATUS_ACTIVE and "publish_pending" not in row and worktree.exists()
    assert row["worktree_toplevel_canonical"] == row_before["worktree_toplevel_canonical"]  # the repaired path is kept
    # 2. "repeat this command": the saved copy with the blank path is gone, so it refuses nothing for an unrelated
    # checkout. It is NOT clean for every target: the active lane's own checkout is in use, and the advice only says
    # the repeat "names what is left, if anything".
    _release_check_is_clean(store)
    assert _release_check_refusal(store, worktree)  # still refused, for the lane's own checkout
    for argv in _printed_abandon_steps(advice):  # none is printed; a printed step would have to be accepted here
        assert _step(store, capsys, *argv)[0] == 0, argv
    # Why the advice promises no second step: the acknowledgement is refused while the checkout is on disk, and a
    # plain `lane abandon` (which removes the checkout) is what retires this lane.
    code, out = _step(store, capsys, "abandon", "--id", lane_id, "--acknowledge-missing-worktree")
    assert code == 2 and "still exists, so there is nothing to acknowledge" in out.err
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ACTIVE
    code, out = _step(store, capsys, "abandon", "--id", lane_id)
    assert code == 0 and f"abandoned lane {lane_id}" in out.out and not worktree.exists()
    _release_check_is_clean(store)


def test_release_advice_when_another_lane_overlaps_the_recovery(tmp_path, monkeypatch, capsys):
    """Recover can hold: restoring the lane to ACTIVE would overlap another lane's paths. It says which lane; the same
    release check keeps saying "recover first" (true: nothing has changed) until the overlap is resolved."""
    from test_lanes import _lane

    store, lane_id, worktree = _a_pending_delivery_with_a_blank_snapshot(tmp_path, monkeypatch, final=False)
    data = lanes.load_lanes(store)
    data["lanes"]["rival"] = _lane(instance_id="2" * 32)  # an ACTIVE lane on the same paths (src/core), written by hand
    lanes.save_lanes(store, data)
    advice = _release_check_refusal(store)
    assert f"run `lane recover --id {lane_id} --reason <why>` first" in advice
    code, out = _step(store, capsys, "recover", "--id", lane_id, "--reason", "follow the release advice")
    assert code == 3 and "would overlap current path reservation(s)" in out.err and "rival" in out.err
    held = lanes.load_lanes(store)["lanes"][lane_id]
    assert held["status"] == lanes.STATUS_DELIVERED and isinstance(held["publish_pending"], dict) and worktree.exists()
    assert f"run `lane recover --id {lane_id} --reason <why>` first" in _release_check_refusal(store)  # still true
    # resolve the overlap, as recover says, and retry: now it restores the lane and the check is clean
    data = lanes.load_lanes(store)
    del data["lanes"]["rival"]
    lanes.save_lanes(store, data)
    code, out = _step(store, capsys, "recover", "--id", lane_id, "--reason", "overlap resolved")
    assert code == 0 and f"recovered lane {lane_id} to ACTIVE" in out.out
    _release_check_is_clean(store)


def _a_pending_live_row_with_one_damaged_checkout_field(tmp_path, monkeypatch, damaged):
    """The live row ITSELF is damaged in one checkout field (an older version saved it so), its delivery stopped before
    a final was committed, and the other field still names the real checkout folder that exists on disk."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree = _unfinished_delivery_without_a_final(tmp_path, monkeypatch, "")
    data = lanes.load_lanes(store)
    row = data["lanes"][lane_id]
    if damaged == "worktree_path":  # the other way round: the canonical field is the one that survives
        row["worktree_path"] = ""
        row["worktree_toplevel_canonical"] = lanes.canonical_host_path(worktree)
    lanes.save_lanes(store, data)
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert row["status"] == lanes.STATUS_DELIVERED and lanes.delivery_recovery_required(row)
    assert row[damaged] == "" and worktree.is_dir()
    assert lanes.checkout_issue(row, root=store.root) is not None
    return store, lane_id, worktree


@pytest.mark.parametrize("damaged", ["worktree_toplevel_canonical", "worktree_path"])
def test_release_advice_for_a_pending_live_row_with_a_surviving_checkout_runs_step_by_step(
    tmp_path, monkeypatch, capsys, damaged
):
    """The row's own checkout field is damaged and the other field names a folder that exists. Every step the advice
    prints is taken, in order, and each is accepted; the acknowledgement it must not promise is refused."""
    store, lane_id, worktree = _a_pending_live_row_with_one_damaged_checkout_field(tmp_path, monkeypatch, damaged)
    # 1. the first advice: recover first, then repeat; no abandon of any kind is promised
    advice = _release_check_refusal(store)
    assert f"run `lane recover --id {lane_id} --reason <why>` first" in advice
    assert "then repeat this command: it names what is left, if anything" in advice
    assert "lane abandon" not in advice and "acknowledge" not in advice
    code, out = _step(store, capsys, "recover", "--id", lane_id, "--reason", "follow the release advice")
    assert code == 0 and f"recovered lane {lane_id} to ACTIVE" in out.out
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert row["status"] == lanes.STATUS_ACTIVE and "publish_pending" not in row and worktree.is_dir()
    assert row[damaged] == ""  # recover does not write the damaged field
    # 2. "repeat this command": what is left depends on the row, and the checkout is still there
    advice = _release_check_refusal(store)
    other = "worktree_toplevel_canonical" if damaged == "worktree_path" else "worktree_path"
    assert f"other saved checkout path ({other}) names a folder that still exists" in advice
    assert f"set {damaged} in lanes.json lane {lane_id!r} to its real absolute folder" in advice
    assert "--acknowledge-missing-worktree" not in advice  # the step it would promise is refused (below)
    assert _printed_abandon_steps(advice) == [["abandon", "--id", lane_id]]
    # why: the acknowledgement is refused while the folder exists, and a plain abandon while a field is damaged
    code, out = _step(store, capsys, "abandon", "--id", lane_id, "--acknowledge-missing-worktree")
    assert code == 2 and "still exists" in out.err and worktree.is_dir()
    code, out = _step(store, capsys, "abandon", "--id", lane_id)
    assert code == 2 and "repair the damaged field" in out.err and worktree.is_dir()
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ACTIVE
    # 3. the person repairs the field by hand (no command writes it), then takes each printed step: accepted
    data = lanes.load_lanes(store)
    data["lanes"][lane_id][damaged] = (
        str(worktree) if damaged == "worktree_path" else lanes.canonical_host_path(worktree)
    )
    lanes.save_lanes(store, data)
    for argv in _printed_abandon_steps(advice):
        code, out = _step(store, capsys, *argv)
        assert code == 0 and f"abandoned lane {lane_id}" in out.out, (argv, out.err)
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ABANDONED and not worktree.exists()
    _release_check_is_clean(store)


def test_the_retirement_advice_acknowledges_only_when_no_saved_path_names_an_existing_folder(tmp_path):
    folder = tmp_path / "there"
    folder.mkdir()
    gone = tmp_path / "gone"
    blank_canonical = {"status": lanes.STATUS_ACTIVE, "worktree_path": str(folder), "worktree_toplevel_canonical": ""}
    # a path survives: no acknowledgement is promised; repair the damaged field, then a plain abandon
    survives = lanes.checkout_path_recovery(lane_id="a", lane=blank_canonical)
    assert "--acknowledge-missing-worktree" not in survives
    assert "set worktree_toplevel_canonical in lanes.json lane 'a' to its real absolute folder" in survives
    assert survives.endswith("then run `lane abandon --id a`")
    # no saved path names a folder that exists (the other one is gone, or both are damaged): the acknowledgement
    for row in (
        {**blank_canonical, "worktree_path": str(gone)},
        {**blank_canonical, "worktree_path": ""},
        {**blank_canonical, "worktree_path": 7},
    ):
        assert lanes.checkout_path_recovery(lane_id="a", lane=row) == (
            "to retire this lane without using its checkout, run `lane abandon --id a --acknowledge-missing-worktree`"
        )
    # the canonical field is judged the same way, with the {ROOT} placeholder expanded
    rooted = {"worktree_path": "", "worktree_toplevel_canonical": "{ROOT}/there"}
    assert lanes.surviving_checkout_field(rooted, root=tmp_path) == "worktree_toplevel_canonical"
    assert lanes.surviving_checkout_field(rooted, root=tmp_path / "elsewhere") is None
    assert lanes.surviving_checkout_field(rooted) is None  # no root, no way to know: not claimed to survive
    assert lanes.surviving_checkout_field("not a row") is None


def _the_advice(store, lane_id):
    """The advice the checkout checks print for this lane's damaged field (what every refusing command quotes)."""
    row = lanes.load_lanes(store)["lanes"][lane_id]
    return lanes.checkout_issue_advice(row, lane_id=lane_id, root=store.root)[2]


def _not_a_folder_advice(lane_id, what, field="worktree_path"):
    """The advice for a surviving path (by default the row's worktree_path) that exists but is not a folder. It never
    tells the reader to touch the entry: the record only names it, and may name something that is not the lane's."""
    return (
        f"the lane's other saved checkout path ({field}) names {what}, not a folder, so retiring it as missing is "
        "refused. The record only names that entry and it may not belong to this lane: leave it as it is and repair "
        f"the lane record in lanes.json lane {lane_id!r} instead: if the lane has a checkout, set both worktree_path "
        f"and worktree_toplevel_canonical to its real absolute folder and run `lane abandon --id {lane_id}`; if it "
        f'has none, set {field} to empty text ("") and repeat this command, which then names the retirement as missing'
    )


def _never_tells_the_reader_to_touch_the_entry(advice):
    assert not any(word in advice.lower() for word in ("move", "delete", "remove", "rename", "unlink"))
    assert "leave it as it is" in advice


def _unexamined_advice(lane_id, why):
    return (
        f"the lane's other saved checkout path (worktree_path) could not be examined ({why}), so it is not known "
        "whether anything is there, and retiring it as missing is refused: check that the entry can be read "
        f"(permissions, disk) and repair the lane record in lanes.json lane {lane_id!r} if it is wrong, then repeat "
        "this command: it names the step that then runs"
    )


def _the_missing_worktree_retirement(lane_id):
    return (
        f"to retire this lane without using its checkout, run `lane abandon --id {lane_id} "
        "--acknowledge-missing-worktree`"
    )


@pytest.mark.parametrize("damaged", ["worktree_toplevel_canonical", "worktree_path"])
def test_a_file_the_record_merely_names_is_left_alone_and_the_record_is_repaired_instead(damaged_lane, capsys, damaged):
    """A damaged record can name an unrelated file (another project's data). The advice must not tell the reader to
    move it; the lane has no checkout, and the way out is to repair the RECORD. Every printed step is accepted."""
    store = damaged_lane(damaged, "")
    named = "worktree_path" if damaged == "worktree_toplevel_canonical" else "worktree_toplevel_canonical"
    unrelated = store.root / "checkout"  # the record names this file; it is independent data, not this lane's
    unrelated.write_text("independent data")
    advice = _the_advice(store, "damaged")
    assert advice == _not_a_folder_advice("damaged", "a file", named)
    _never_tells_the_reader_to_touch_the_entry(advice)
    assert "acknowledge" not in advice and "names a folder" not in advice and "that folder" not in advice
    # what the commands really do with it: both refuse and change nothing
    code, out = _step(store, capsys, "abandon", "--id", "damaged", "--acknowledge-missing-worktree")
    assert code == 2 and "still exists" in out.err
    code, out = _step(store, capsys, "abandon", "--id", "damaged")
    assert code == 2 and "repair the damaged field" in out.err
    assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ACTIVE
    # "if it has none, set <the field that names it> to empty text and repeat this command" (the record is the lane's
    # own): the repeat names the retirement as missing, and it runs
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"][named] = ""
    lanes.save_lanes(store, data)
    advice = _the_advice(store, "damaged")
    assert advice == _the_missing_worktree_retirement("damaged")
    for argv in _printed_abandon_steps(advice):
        code, out = _step(store, capsys, *argv)
        assert code == 0 and "abandoned lane damaged" in out.out, (argv, out.err)
    assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ABANDONED
    # and the file the record named was never touched, by the advice or by the commands
    assert unrelated.is_file() and unrelated.read_text() == "independent data"


def test_a_file_at_one_path_and_a_real_checkout_elsewhere_end_in_the_plain_abandon(tmp_path, monkeypatch, capsys):
    """The other way out of the file case: the lane's real checkout folder exists, the record names a file."""
    store, lane_id, worktree = _unfinished_delivery_without_a_final(tmp_path, monkeypatch, "")
    code, out = _step(store, capsys, "recover", "--id", lane_id, "--reason", "follow the release advice")
    assert code == 0 and f"recovered lane {lane_id} to ACTIVE" in out.out
    stray = tmp_path / "stray.txt"
    stray.write_text("not a folder")
    data = lanes.load_lanes(store)
    data["lanes"][lane_id].update(worktree_path=str(stray), worktree_toplevel_canonical="")
    lanes.save_lanes(store, data)
    advice = _the_advice(store, lane_id)
    assert advice == _not_a_folder_advice(lane_id, "a file")
    _never_tells_the_reader_to_touch_the_entry(advice)
    assert _printed_abandon_steps(advice) == [["abandon", "--id", lane_id]]  # no acknowledgement is printed
    assert _step(store, capsys, "abandon", "--id", lane_id, "--acknowledge-missing-worktree")[0] == 2
    assert _step(store, capsys, "abandon", "--id", lane_id)[0] == 2
    assert worktree.is_dir() and stray.exists()
    # the person sets both fields to the real folder (no command writes them), then the printed step runs
    data = lanes.load_lanes(store)
    data["lanes"][lane_id].update(
        worktree_path=str(worktree), worktree_toplevel_canonical=lanes.canonical_host_path(worktree)
    )
    lanes.save_lanes(store, data)
    for argv in _printed_abandon_steps(advice):
        code, out = _step(store, capsys, *argv)
        assert code == 0 and f"abandoned lane {lane_id}" in out.out, (argv, out.err)
    assert not worktree.exists()
    assert stray.read_text() == "not a folder"  # the file the record named is still there, unchanged


def test_a_link_at_the_surviving_path_is_a_link_even_when_it_leads_to_a_folder(damaged_lane, capsys):
    """A link is never offered as the value to set: the record must name the real folder (see the helper)."""
    store = damaged_lane("worktree_toplevel_canonical", "")
    target = store.root / "the-real-folder"
    target.mkdir()
    link = store.root / "checkout"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow creating a symbolic link")
    advice = _the_advice(store, "damaged")
    assert advice == _not_a_folder_advice("damaged", "a link (the record must name the real folder, not a link to it)")
    _never_tells_the_reader_to_touch_the_entry(advice)
    assert "names a folder" not in advice and "acknowledge" not in advice
    code, out = _step(store, capsys, "abandon", "--id", "damaged", "--acknowledge-missing-worktree")
    assert code == 2 and "still exists" in out.err and target.is_dir()
    data = lanes.load_lanes(store)  # the record is repaired; the link and what it leads to are left alone
    data["lanes"]["damaged"]["worktree_path"] = ""
    lanes.save_lanes(store, data)
    assert _the_advice(store, "damaged") == _the_missing_worktree_retirement("damaged")
    assert link.is_symlink() and target.is_dir()


@pytest.mark.parametrize(
    ("mode", "what"),
    [
        (stat.S_IFSOCK, "an entry that is neither a file nor a folder"),
        (stat.S_IFLNK, "a link (the record must name the real folder, not a link to it)"),  # no privilege needed
    ],
    ids=["socket", "link"],
)
def test_an_entry_that_is_not_a_file_or_a_folder_is_not_called_a_folder(monkeypatch, tmp_path, mode, what):
    real_lstat = os.lstat
    odd = str(tmp_path / "odd")

    def lstat(path, *args, **kwargs):
        if str(path) == odd:
            return types.SimpleNamespace(st_mode=mode)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(lanes.os, "lstat", lstat)
    row = {"status": lanes.STATUS_ACTIVE, "worktree_path": odd, "worktree_toplevel_canonical": ""}
    assert lanes.checkout_path_recovery(lane_id="a", lane=row) == _not_a_folder_advice("a", what)


def test_an_entry_that_cannot_be_examined_is_not_claimed_to_exist(damaged_lane, monkeypatch, capsys):
    import errno

    store = damaged_lane("worktree_toplevel_canonical", "")
    target = str(store.root / "checkout")
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as broken:
        broken.setattr(lanes.os, "lstat", lstat)
        advice = _the_advice(store, "damaged")
        assert advice == _unexamined_advice("damaged", "Permission denied")
        assert "exists" not in advice and "folder that" not in advice and "acknowledge" not in advice
        # the commands agree: nothing is established, so the acknowledgement is refused and nothing changes
        code, out = _step(store, capsys, "abandon", "--id", "damaged", "--acknowledge-missing-worktree")
        assert code == 2 and "could not be established" in out.err
        assert _step(store, capsys, "abandon", "--id", "damaged")[0] == 2
        assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ACTIVE
    # "repeat this command" once the entry can be looked at: nothing is there, so the retirement is named and runs
    advice = _the_advice(store, "damaged")
    assert advice == _the_missing_worktree_retirement("damaged")
    for argv in _printed_abandon_steps(advice):
        code, out = _step(store, capsys, *argv)
        assert code == 0 and "abandoned lane damaged" in out.out, (argv, out.err)


def test_a_verified_folder_is_reported_before_a_file_and_a_file_before_an_entry_that_cannot_be_examined(
    tmp_path, monkeypatch
):
    import errno

    folder = tmp_path / "folder"
    folder.mkdir()
    file = tmp_path / "file"
    file.write_text("x")
    row = {"worktree_path": str(file), "worktree_toplevel_canonical": str(folder)}
    assert lanes.surviving_checkout_entry(row) == ("worktree_toplevel_canonical", "folder", "")
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == str(tmp_path / "broken"):
            raise OSError(errno.EIO, "Input/output error")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(lanes.os, "lstat", lstat)
    both = {"worktree_path": str(tmp_path / "broken"), "worktree_toplevel_canonical": str(file)}
    assert lanes.surviving_checkout_entry(both) == ("worktree_toplevel_canonical", "file", "")
    broken_only = {"worktree_path": str(tmp_path / "broken"), "worktree_toplevel_canonical": ""}
    assert lanes.surviving_checkout_entry(broken_only) == ("worktree_path", "unexamined", "Input/output error")
    assert lanes.surviving_checkout_entry({"worktree_path": str(tmp_path / "gone")}) is None  # only absence is None


def test_the_recovery_step_follows_the_live_row_when_given_one_and_the_row_itself_otherwise():
    pending = {"status": lanes.STATUS_DELIVERED, "publish_pending": {"lane_snapshot": {}}}
    active = {"status": lanes.STATUS_ACTIVE, "worktree_path": "", "worktree_toplevel_canonical": ""}
    # a saved copy (active, blank) inside a live row that is pending: recover first, and no second step is promised
    held = lanes.checkout_path_recovery(lane_id="a", lane=active, owner=pending)
    assert "this lane's delivery is not finished: run `lane recover --id a" in held
    assert "then repeat this command" in held and "lane abandon" not in held
    # the same copy inside a live row that is not pending: the plain retirement
    plain = lanes.checkout_path_recovery(lane_id="a", lane=active, owner={"status": lanes.STATUS_ACTIVE})
    assert plain.startswith("to retire this lane without using its checkout, run `lane abandon --id a")
    # no owner: the row is judged by itself, exactly as before this change (every other caller)
    assert lanes.checkout_path_recovery(lane_id="a", lane=active) == plain
    assert "this lane's delivery is not finished" in lanes.checkout_path_recovery(lane_id="a", lane=pending)
    # the owner is the row itself (its OWN checkout field is the damaged one): the same advice, recover first and then
    # repeat the command. No second step is promised: a damaged field does not mean no checkout survives, and the
    # acknowledgement is refused while one does.
    own = lanes.checkout_path_recovery(lane_id="a", lane=pending, owner=pending)
    assert own == lanes.checkout_path_recovery(lane_id="a", lane=pending) == held
    assert "then repeat this command" in own and "lane abandon" not in own and "acknowledge" not in own


def test_the_live_row_is_carried_through_a_list_inside_the_row_too(tmp_path):
    from agenttalk import worktree_release

    blank = {"status": lanes.STATUS_ACTIVE, "worktree_path": "", "worktree_toplevel_canonical": ""}
    row = {"status": lanes.STATUS_DELIVERED, "publish_pending": {"saved_copies": [blank]}}
    with pytest.raises(worktree_release.Refused) as stopped:
        worktree_release._names_checkout(
            row, tmp_path / "other", tmp_path, lane_id="a", source="lanes.json lane a", lane=True
        )
    assert "run `lane recover --id a --reason <why>` first" in str(stopped.value)


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
        if acknowledge and field == "worktree_path":
            # A path of only whitespace is non-absolute as stored: the acknowledgement refuses it before it looks at
            # any folder, and says how to go on (replace it, or empty it when the checkout is really gone).
            assert "Replace worktree_path in lanes.json lane 'damaged'" in refusal and 'empty text ("")' in refusal
            assert "nothing was changed" in refusal and "still exists" not in refusal
            assert lanes.lanes_path(store).read_bytes() == before
            assert evidence.read_text() == "unfinished work"
            continue
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


def test_the_verifier_refuses_a_row_whose_own_lane_id_is_another_lanes_before_any_folder_use(
    keyed, monkeypatch, tmp_path
):
    """The lane branch is picked by the id inside the row. A damaged row "bad" that holds the id "other" and names a
    real, clean checkout would otherwise be confirmed as lane/other's checkout, and abandon's cleanup would remove
    it. The row must name itself before any folder is read."""
    folder = tmp_path / "someone-elses-checkout"
    folder.mkdir()
    store = keyed(embedded=OTHER, worktree_path=str(folder), worktree_toplevel_canonical=str(folder))
    row = lanes.load_lanes(store)["lanes"][BAD]

    def git(*args, **kwargs):
        pytest.fail("a row that holds another lane's id must be refused before Git or any folder is used")

    monkeypatch.setattr(cli, "_git_read_one", git)
    monkeypatch.setattr(cli, "_git", git)
    with pytest.raises(lanes.LaneError) as caught:
        cli._verify_lane_worktree(store, row, lane_id=BAD)
    assert f"lane row {BAD} holds the lane id {OTHER}" in str(caught.value)
    assert "repair lane_id in that row of lanes.json" in str(caught.value)
    assert folder.is_dir()


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


def test_acknowledgement_still_retires_an_empty_path_with_an_absolute_absent_canonical(keyed, capsys):
    store = keyed(worktree_path="", worktree_toplevel_canonical=str(keyed.root / "gone-absolute"))
    subprocess.run(["git", "-C", str(store.root), "init", "-q"], check=True)
    assert _run(store, "lane", "abandon", "--id", BAD, ACK_HINT) == 0
    row = lanes.load_lanes(store)["lanes"][BAD]
    assert row["status"] == lanes.STATUS_ABANDONED and row["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED
    assert row["lane_id"] == OTHER  # the saved record is not rewritten


@pytest.mark.parametrize("case", ["registered", "prunable", "locked", "truncated", "duplicate", "nonzero",
                                 "empty", "relative", "bad-head", "empty-branch", "both", "bare-head",
                                 "bare-value", "bare-empty-value", "detached-empty-value", "duplicate-prunable",
                                 "unknown-first", "invalid-utf8"])
def test_damaged_acknowledgement_refuses_registered_or_unreliable_listing(keyed, monkeypatch, capsys, case):
    store = keyed(worktree_path="", worktree_toplevel_canonical=str(keyed.root / "absent"))
    before = lanes.lanes_path(store).read_bytes()
    real_git = cli._git
    marker = {"prunable": b"prunable missing\0", "locked": b"locked reason\0"}.get(case, b"")
    record = (b"worktree " + str(keyed.root / "checkout").encode() + b"\0HEAD " + b"a" * 40
              + b"\0branch refs/heads/lane/bad\0" + marker)
    if case == "duplicate":
        record += b"branch refs/heads/lane/other\0"
    if case == "relative":
        record = record.replace(str(keyed.root / "checkout").encode(), b"relative")
    if case == "bad-head":
        record = record.replace(b"a" * 40, b"xyz")
    if case == "empty-branch":
        record = record.replace(b"refs/heads/lane/bad", b"")
    if case == "both":
        record += b"detached\0"
    if case == "bare-head":
        record += b"bare\0"
    if case == "bare-value":
        record = b"worktree " + str(keyed.root).encode() + b"\0bare unexpected\0"
    if case == "bare-empty-value":
        record = b"worktree " + str(keyed.root).encode() + b"\0bare \0"
    if case == "detached-empty-value":
        record = record.replace(b"branch refs/heads/lane/bad", b"detached ")
    if case == "duplicate-prunable":
        record += b"prunable\0prunable\0"
    if case == "unknown-first":
        record = record.replace(b"\0HEAD ", b"\0unknown test\0HEAD ")
    if case == "invalid-utf8":
        record = record.replace(b"checkout", b"\xff")
    listing = record if case == "truncated" else record + b"\0"
    if case == "empty":
        listing = b""

    def git(root, argv, **kwargs):
        if argv == ["worktree", "list", "--porcelain", "-z"]:
            return (1, b"") if case == "nonzero" else (0, listing)
        return real_git(root, argv, **kwargs)

    monkeypatch.setattr(cli, "_git", git)
    monkeypatch.setattr(cli.janitormod, "_fence_registrations", lambda root: None)
    assert _run(store, "lane", "abandon", "--id", BAD, ACK_HINT) == 2
    error = capsys.readouterr().err.lower()
    assert "nothing was acknowledged" in error and "sweep" not in error
    assert lanes.lanes_path(store).read_bytes() == before


def test_strict_listing_keeps_unknown_fields_after_the_checkout_identity(tmp_path, monkeypatch):
    listing = (b"worktree " + str(tmp_path).encode() + b"\0HEAD " + b"a" * 40
               + b"\0branch refs/heads/main\0future extension\0\0")
    order = []
    monkeypatch.setattr(cli.janitormod, "_fence_registrations", lambda root: order.append("fence"))

    def git(root, argv, *, raw=False):
        order.append("git")
        assert raw and argv == ["worktree", "list", "--porcelain", "-z"]
        return 0, listing

    monkeypatch.setattr(cli, "_git", git)
    records = cli._worktree_records_strict(tmp_path)
    assert order == ["fence", "git"]
    assert records[0]["future"] == "extension"


@pytest.mark.parametrize("error", [cli.janitormod._UseCheckFailed("unreadable registration"),
                                   cli.store_mod.StoreFenceError("outside the fence")])
def test_acknowledgement_stops_before_git_when_registration_fence_fails(keyed, monkeypatch, capsys, error):
    store = keyed(worktree_path="", worktree_toplevel_canonical=str(keyed.root / "absent"))
    before = lanes.lanes_path(store).read_bytes()

    def refuse(root):
        raise error

    monkeypatch.setattr(cli.janitormod, "_fence_registrations", refuse)
    monkeypatch.setattr(cli, "_git", lambda *args, **kwargs: pytest.fail("Git must not be called"))
    assert _run(store, "lane", "abandon", "--id", BAD, ACK_HINT) == 2
    assert "nothing was acknowledged" in capsys.readouterr().err
    assert lanes.lanes_path(store).read_bytes() == before


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


def test_status_still_hides_an_acknowledged_empty_path(keyed, capsys):
    # (a whitespace-only path is not retired by the acknowledgement: status shows it; see the release test of round 13)
    store = keyed(worktree_path="", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED)
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


def test_release_still_refuses_an_acknowledged_relative_canonical_but_not_an_acknowledged_empty_path(tmp_path):
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
        _release_row(**ack, worktree_path=""),  # (a whitespace-only one is not retired: see round 12 below)
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


NON_ABSOLUTE = [
    "relative/worktree",
    pytest.param("C:relative", marks=pytest.mark.skipif(os.name != "nt", reason="a Windows drive-relative spelling")),
    pytest.param("\\rooted", marks=pytest.mark.skipif(os.name != "nt", reason="a Windows rooted spelling")),
]


@pytest.mark.parametrize("canonical", ["", "ABSOLUTE"])
@pytest.mark.parametrize("spelling", NON_ABSOLUTE)
def test_the_acknowledgement_owner_refuses_every_non_absolute_worktree_path(keyed, spelling, canonical):
    """No fallback to an absolute canonical and no skipping of a drive-relative or rooted spelling: whatever is not
    absolute is refused with the same advice, with a blank canonical field and with an absolute one."""
    store = keyed()
    row = dict(lanes.load_lanes(store)["lanes"][BAD])
    row.update(worktree_path=spelling, worktree_toplevel_canonical=str(store.root / "canon") if canonical else "")
    with pytest.raises(lanes.LaneError, match=REPAIR) as caught:
        cli._lane_acknowledgement_path(store, row, lane_id=BAD)
    assert ADVICE in str(caught.value) and "remove" not in str(caught.value).lower()


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


def _assert_old_row_refusal(err):
    assert "lanes.json lane 'bad'" in err and "worktree_toplevel_canonical is a relative path" in err
    assert "it must be an absolute path" in err
    assert "set worktree_toplevel_canonical to the checkout's real absolute folder" in err
    assert "remove" not in err.lower()  # removing the field can leave a lane with nothing to acknowledge
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


@pytest.mark.parametrize("acknowledged", [False, True], ids=["active", "already-retired"])
def test_the_stated_repair_of_an_old_row_works_and_status_shows_it_until_then(keyed, capsys, acknowledged):
    fields = dict(OLD_STYLE)
    if acknowledged:
        fields.update(status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED)
    store = keyed(**fields)
    shown = next(r for r in _status_rows(store, capsys) if r.get("assignee") == "bad-seat")
    _assert_old_row_refusal(shown["checkout_path_error"])
    data = lanes.load_lanes(store)  # the repair the message describes, done by hand in lanes.json
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
    """Release must not call a share root ambiguous. Its physical matching (`worktree_release._placed`) is kept in
    production but replaced here by a stand-in that looks at nothing, so this test never touches a share or the
    network; it records what release asked to place, which shows that release got past the classification and
    reached the matching. The suite's store fence is lifted for this test (the way `tests/test_probe_store.py` models
    an unfenced shell): under a fence a record's path outside it is refused first, as a string, which is another
    test's business (`tests/test_store_fence.py`); this one is about the classification and the matching."""
    from agenttalk import worktree_release

    monkeypatch.delenv("AGENTTALK_STORE_FENCE", raising=False)
    asked = []

    def place_nothing(path):
        asked.append(str(path))
        return "//server/share" if str(path).rstrip("\\") == r"\\server\share" else "c:/elsewhere"

    monkeypatch.setattr(worktree_release, "_placed", place_nothing)
    row = {"lane_id": OTHER, "worktree_toplevel_canonical": r"\\server\share"}
    assert not worktree_release._names_checkout(
        row, tmp_path / "elsewhere", tmp_path, lane_id=BAD, source="lanes.json lane bad", lane=True
    )
    assert any(path.rstrip("\\") == r"\\server\share" for path in asked)  # reached the matching, looked at nothing


@pytest.mark.skipif(os.name != "nt", reason="a UNC path is only absolute on Windows")
def test_under_the_store_fence_a_unc_record_path_keeps_the_checkout_and_nothing_looks_at_it(tmp_path, monkeypatch):
    """The fenced twin of the test above, with the suite's own store fence ON (not lifted): the same UNC record is
    refused (`Refused`: the checkout is kept, never released), and no `lstat`, `stat`, `realpath` or placing of that
    path happens first. Every look at it is answered with an error and noted, so the share is never contacted."""
    import errno

    from agenttalk import worktree_release

    assert os.environ.get("AGENTTALK_STORE_FENCE"), "this test is about the fence the suite sets for every test"
    unc = r"\\server\share"
    looked = []

    def is_unc(path) -> bool:
        try:
            return os.fspath(path).rstrip("\\").lower() == unc
        except TypeError:
            return False

    def watch(name, real):
        def spy(path, *args, **kwargs):
            if is_unc(path):
                looked.append(name)
                raise OSError(errno.EIO, "the share was looked at (test)")
            return real(path, *args, **kwargs)

        return spy

    for owner, name in ((os, "lstat"), (os, "stat"), (os.path, "realpath"), (os.path, "exists")):
        monkeypatch.setattr(owner, name, watch(f"{owner.__name__}.{name}", getattr(owner, name)))
    monkeypatch.setattr(worktree_release, "_placed", lambda path: looked.append("_placed") or "c:/elsewhere")
    row = {"lane_id": OTHER, "worktree_toplevel_canonical": unc}

    with pytest.raises(worktree_release.Refused, match="outside the fence") as refused:
        worktree_release._names_checkout(
            row, tmp_path / "elsewhere", tmp_path, lane_id=BAD, source="lanes.json lane bad", lane=True
        )

    assert "the checkout was kept" in str(refused.value)
    assert looked == [], f"the share was looked at: {looked}"


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


@pytest.mark.parametrize("state", ["ordinary", "locked", "prunable", "prunable-absent", "detached"])
def test_damaged_acknowledgement_checks_real_git_records(tmp_path, capsys, state):
    from test_lanes import _git_rc

    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    if state == "locked":
        assert _git_rc(store.root, "worktree", "lock", str(worktree)).returncode == 0
    elif state in {"prunable", "prunable-absent"}:
        (worktree / ".git").unlink()
        if state == "prunable-absent":
            import shutil

            assert worktree.resolve().is_relative_to(tmp_path.resolve())
            shutil.rmtree(worktree)
    elif state == "detached":
        assert _git_rc(worktree, "checkout", "--detach").returncode == 0
    rc, listing = cli._git(store.root, ["worktree", "list", "--porcelain", "-z"], raw=True)
    assert rc == 0 and worktree in cli.janitormod._parse_worktree_list_z(listing)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id].update(worktree_path="", worktree_toplevel_canonical="")
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    code = _run(store, "lane", "abandon", "--id", lane_id, ACK_HINT)
    output = capsys.readouterr()
    if state == "detached":  # the documented limit: a detached checkout names no branch
        assert code == 0, output.err
    else:
        marker = "prunable" if state == "prunable-absent" else state.replace("ordinary", "registered")
        assert code == 2 and worktree.as_posix() in output.err and marker in output.err
        assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"])
@pytest.mark.parametrize("nonempty", [True, False])
def test_damaged_acknowledgement_refuses_git_repository_overrides(tmp_path, monkeypatch, capsys, name, nonempty):
    store, lane_id, _row, worktree = _real_lane_row(tmp_path)
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id].update(worktree_path="", worktree_toplevel_canonical="")
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    assert worktree.is_dir()
    monkeypatch.setenv(name, str(other / ".git") if nonempty else "")
    real_git, calls = cli._git, []
    def git(root, argv, **kwargs):
        calls.append(argv)
        return real_git(root, argv, **kwargs)
    monkeypatch.setattr(cli, "_git", git)
    assert _run(store, "lane", "abandon", "--id", lane_id, ACK_HINT) == 2
    assert name in capsys.readouterr().err and not calls  # set or empty, refused before Git is asked (#942)
    assert lanes.lanes_path(store).read_bytes() == before
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ACTIVE


def test_janitor_listing_still_accepts_a_real_bare_main_repository(tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    rc, listing = cli._git(bare, ["worktree", "list", "--porcelain", "-z"], raw=True)
    assert rc == 0 and cli.janitormod._parse_worktree_list_z(listing) == [bare]
    assert cli._worktree_records_strict(bare)[0]["bare"] == ""


@pytest.mark.skipif(os.name == "nt", reason="literal backslash names need a POSIX file system")
def test_old_inside_backslash_canonical_is_refused_with_repair_advice(tmp_path):
    from test_lanes import _assign_worktree, _commit, _repo, _run_raw

    root = tmp_path / "a\\b" / "repo"
    root.mkdir(parents=True)
    root, base = _repo(root)
    row = _assign_worktree(root, base, "legacy")
    _commit(Path(row["worktree_path"]), "src/core/a.py", "base\nlegacy change\n")
    current = row["worktree_toplevel_canonical"]
    row["worktree_toplevel_canonical"] = current.replace("\\", "/")
    with pytest.raises(lanes.LaneError, match="worktree_toplevel_canonical.*real absolute folder"):
        cli._verify_lane_worktree(Store(root), row, lane_id="legacy")
    store = Store(root)
    data = lanes.load_lanes(store)
    data["lanes"]["legacy"]["worktree_toplevel_canonical"] = row["worktree_toplevel_canonical"]
    lanes.save_lanes(store, data)
    assert _run_raw(["lane", "deliver", "--id", "legacy", "--from", "dev"], root) == 3
    data["lanes"]["legacy"]["worktree_toplevel_canonical"] = current
    lanes.save_lanes(store, data)
    row["worktree_toplevel_canonical"] = current
    assert cli._verify_lane_worktree(Store(root), row, lane_id="legacy")["head"]
    assert _run_raw(["lane", "deliver", "--id", "legacy", "--from", "dev"], root) == 0


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
    with pytest.raises(lanes.LaneError, match="no longer matches git") as caught:
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert "worktree_toplevel_canonical" in str(caught.value) and "real absolute folder" in str(caught.value)
    assert "it must be an absolute path" not in str(caught.value)
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
    # another lane's id inside the row: refused before any folder is read (#834), not only by the branch check
    with pytest.raises(lanes.LaneError, match="holds the lane id some-other-lane; the two must match"):
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


RETRY_PLACES = [*RETRY_FOLDERS, "inside-another-checkout"]


def _retry_place(tmp_path, kind):
    """A retry folder. "inside-another-checkout" is a new Git repository in which the delivery's relative worktree
    spelling happens to be a folder, so a relative path resolved against it succeeds with the WRONG checkout."""
    if kind in RETRY_FOLDERS:
        return _retry_folder(tmp_path, kind)
    import subprocess

    place = tmp_path.parent / (tmp_path.name + "-other")
    place.mkdir()
    assert subprocess.run(["git", "init", "-q", str(place)], capture_output=True).returncode == 0
    return place


def _make_the_relative_spelling_exist_in(place, store, lane_id):
    spelling = lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]["lane_snapshot"]["worktree_path"]
    (place / spelling).mkdir(parents=True)


@pytest.mark.parametrize("place", RETRY_PLACES)
def test_a_committed_final_with_a_relative_worktree_path_is_verified_through_its_absolute_canonical_from_any_folder(
    tmp_path, monkeypatch, capsys, place
):
    """The signed snapshot has a relative worktree path and an ABSOLUTE canonical. The relative spelling is never read
    against the retry's folder (a retry from inside another checkout would read the wrong one): the checkout is
    verified through the canonical, with the usual identity, branch and cleanliness checks, so the valid final is
    published once from the original folder, a sibling, a child, or a folder inside some other Git checkout."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree, head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, relative_worktree=True
    )
    saved = lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]["lane_snapshot"]
    assert not os.path.isabs(saved["worktree_path"]) and os.path.isabs(saved["worktree_toplevel_canonical"])
    folder = _retry_place(tmp_path, place)
    if place == "inside-another-checkout":
        _make_the_relative_spelling_exist_in(folder, store, lane_id)
    original_artifact = artifact.read_bytes()
    monkeypatch.chdir(folder)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    assert lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"] is False
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]
    assert not list((store.dir / "lane-deliveries").rglob(".recovery-quarantine*"))
    lanes.validate_delivery_artifact(
        artifact, lane_id=lane_id, head_sha=head, store=store, require_isolation=True, require_live_marker=True
    )


@pytest.mark.parametrize("canonical", [""])
@pytest.mark.parametrize("place", RETRY_PLACES)
def test_a_committed_final_with_a_relative_worktree_path_and_no_absolute_canonical_is_kept_whole_from_any_folder(
    tmp_path, monkeypatch, capsys, place, canonical
):
    """With nothing absolute in the signed snapshot, nothing can be located from any folder (the original one
    included), so nothing is read: the final, the pending delivery and the row stay byte-identical, nothing is set
    aside, and the message says plainly that this version cannot complete the delivery, that the evidence is kept and
    that it should be reported. It does not promise that repairing the row helps (the signed copy is what is
    checked)."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical=canonical, relative_worktree=True
    )
    folder = tmp_path if place == "original" else _retry_place(tmp_path, place)
    if place == "inside-another-checkout":
        _make_the_relative_spelling_exist_in(folder, store, lane_id)
    before = lanes.lanes_path(store).read_bytes()
    original_artifact = artifact.read_bytes()
    monkeypatch.chdir(folder)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    err = capsys.readouterr().err.lower()
    assert "relative worktree path" in err and "nothing was changed" in err and "any folder" in err
    assert "this version cannot complete this delivery; the evidence is kept; report it" in err
    assert "does not help" in err and "saved copy of the lane" in err  # it says why a repaired row is not enough
    assert "replace worktree_path" not in err and "then retry" not in err and "remove" not in err
    assert "restored active" not in err and "from that folder" not in err
    assert lanes.lanes_path(store).read_bytes() == before  # the pending delivery and the row are untouched
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]
    assert not list((store.dir / "lane-deliveries").rglob(".recovery-quarantine*"))


SCENARIOS = [
    "publishes",
    "target-gone",
    "dirty-file",
    "checkout-removed",
    "barrier-changed",
    "overlapping-lane-added",
]
WORLD_CHANGES = ["barrier-changed", "overlapping-lane-added", "other-lane-added"]


def _change_the_world(store, lane_id, change):
    """What can happen between the first attempt and the retry: a new release barrier, or another ACTIVE lane row."""
    if change == "barrier-changed":
        store.send(
            sender="lead",
            recipient="lead",
            body="a release barrier sent after the delivery was prepared",
            meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
        )
        return
    data = lanes.load_lanes(store)
    row = data["lanes"][lane_id]
    other = lanes.new_lane(
        "other-lane",
        assignee="dev",
        assigned_by="lead",
        assigned_at="t",
        domain_id=row["domain_id"],
        path_subset=["src/other"] if change == "other-lane-added" else list(row["path_subset"]),
        base_sha=row["base_sha"],
        target_ref=row["target_ref"],
        target_head_at_assign=row["target_head_at_assign"],
        epoch_at_assign=row.get("epoch_at_assign"),
        registry_hash_at_assign=row["registry_hash_at_assign"],
    )
    data["lanes"]["other-lane"] = other
    lanes.save_lanes(store, data)


def _committed_outcome(base, monkeypatch, capsys, kind, scenario, place):
    """Everything a person or a later command can observe after one retry of a committed delivery (publication or
    rollback), with the machine's folder names and hashes blanked, so two deliveries made in different folders can be
    compared. The worktree CLEANUP that follows a publication is left out on purpose: it reads the lane row's own
    `worktree_path`, not the signed copy, and a relative row path there is still read against the retry's folder (an
    older, separate behavior reported to the lead, not changed here)."""
    from test_lanes import _committed_artifacts, _git_rc

    base.mkdir()
    monkeypatch.chdir(base)
    store, lane_id, worktree, _head, artifact = _committed_unpublished_delivery(
        base, monkeypatch, relative_worktree=kind == "relative"
    )
    if scenario == "target-gone":
        target = lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]["lane_snapshot"]["target_ref"]
        assert _git_rc(store.root, "branch", "-m", target, "renamed-" + target).returncode == 0
    elif scenario == "dirty-file":
        (worktree / "src/core/a.py").write_text("a tracked change that was never committed\n", encoding="utf-8")
    elif scenario == "checkout-removed":
        assert _git_rc(store.root, "worktree", "remove", "--force", "--", str(worktree)).returncode == 0
    elif scenario in WORLD_CHANGES:
        _change_the_world(store, lane_id, scenario)
    folder = base if place == "original" else _retry_place(base, place)
    if place == "inside-another-checkout" and kind == "relative":
        _make_the_relative_spelling_exist_in(folder, store, lane_id)
    monkeypatch.chdir(folder)
    capsys.readouterr()
    code = cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"])
    captured = capsys.readouterr()
    row = lanes.load_lanes(store)["lanes"][lane_id]
    shown = captured.err + captured.out
    for spelling in (str(base), base.as_posix(), str(base).replace("\\", "\\\\")):
        shown = shown.replace(spelling, "<base>")
    shown = re.sub(r"[0-9a-f]{12,64}", "<hash>", shown).replace("; cleanup pending", "")
    return {
        "exit": code,
        "shown": shown,
        "status": row["status"],
        "publish_pending": type(row.get("publish_pending")).__name__,
        "finals": len(_committed_artifacts(store, lane_id)),
        "quarantined": len(list((store.dir / "lane-deliveries").rglob(".recovery-quarantine*"))),
        "final_still_there": artifact.exists(),
    }


@pytest.mark.parametrize("place", RETRY_PLACES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_relative_worktree_path_with_an_absolute_canonical_ends_exactly_like_an_absolute_path_delivery(
    tmp_path, monkeypatch, capsys, scenario, place
):
    """Same checks, same rollback, same refusals, no new quarantine case, never the retry's folder: for every outcome
    (published; target branch gone; tracked file changed; checkout removed; a release barrier sent or an overlapping
    active lane added before the retry) and every retry folder, a delivery whose
    saved worktree path is relative (with an absolute canonical) ends exactly as the same delivery with an absolute
    worktree path: exit code, what is printed, the lane row, the committed final and the quarantine."""
    absolute = _committed_outcome(tmp_path / "absolute", monkeypatch, capsys, "absolute", scenario, place)
    relative = _committed_outcome(tmp_path / "relative", monkeypatch, capsys, "relative", scenario, place)
    assert relative == absolute
    expected = {"publishes": 0, "target-gone": 3}.get(scenario)
    if expected is not None:
        assert absolute["exit"] == expected
    assert (absolute["exit"] == 0) == (scenario == "publishes")
    if scenario in ("target-gone", "dirty-file", "checkout-removed"):
        assert absolute["status"] == lanes.STATUS_ACTIVE and absolute["final_still_there"] is False  # set aside


def test_an_absolute_worktree_path_with_a_real_terminal_failure_still_takes_the_normal_terminal_rollback(
    tmp_path, monkeypatch, capsys
):
    """The keep-it-pending refusal is only for a saved worktree path that is not absolute. With an absolute path the
    checkout is read as before: here the target ref no longer resolves, which is a real terminal failure, so the valid
    final is set aside and the lane is restored active (exit 3), and `lane recover` then has nothing to send the
    person back to `lane deliver` for. (A relative row is never read, so it cannot reach this failure: it is kept
    whole instead, see the kept-whole test.)"""
    from test_lanes import _committed_artifacts, _git_rc

    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
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


def test_a_dirty_tracked_file_in_an_absolute_path_checkout_gets_the_dirty_file_advice_and_nothing_about_paths(
    tmp_path, monkeypatch, capsys
):
    """The advice must be honest: with an absolute worktree path the checkout is read, so a refusal there is about the
    checkout itself (a changed tracked file), not about its path, and says nothing about replacing a path."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
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


@pytest.mark.parametrize("place", ["original", "sibling"])
@pytest.mark.parametrize("change", WORLD_CHANGES)
def test_a_committed_final_with_a_relative_path_and_no_absolute_canonical_is_kept_whole_even_if_the_world_moved(
    tmp_path, monkeypatch, capsys, change, place
):
    """If a release barrier is sent, or another active lane is added, between the first attempt and the retry, a valid
    committed final whose saved copy cannot be located is STILL kept: the preserve-and-report outcome comes before any
    terminal rollback, so the final is not set aside, the lane is not restored, and nothing is put on hold."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical="", relative_worktree=True
    )
    _change_the_world(store, lane_id, change)
    folder = tmp_path if place == "original" else _retry_folder(tmp_path, "sibling")
    before = lanes.lanes_path(store).read_bytes()
    original_artifact = artifact.read_bytes()
    monkeypatch.chdir(folder)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 2
    err = capsys.readouterr().err.lower()
    assert "this version cannot complete this delivery; the evidence is kept; report it" in err
    assert "restored active" not in err and "terminal" not in err.replace("terminal input recovery failed", "")
    assert lanes.lanes_path(store).read_bytes() == before  # the pending delivery and the row are untouched
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]
    assert not list((store.dir / "lane-deliveries").rglob(".recovery-quarantine*"))


def test_a_damaged_committed_final_is_still_refused_before_it_can_be_kept(tmp_path, monkeypatch, capsys):
    """Keeping the evidence is only for a final that passes its integrity check. A final that was altered after it was
    signed is refused as it always was, with no promise that the evidence is intact."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical="", relative_worktree=True
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["delivered_by"] = "tampered-after-signing"  # no longer what was signed
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    err = capsys.readouterr().err.lower()
    assert "the evidence is kept" not in err and err.strip()
    assert lanes.lanes_path(store).read_bytes() == before  # refused as before: nothing about the row changes


def test_a_saved_copy_the_final_does_not_vouch_for_is_not_kept_as_an_unlocatable_delivery(
    tmp_path, monkeypatch, capsys
):
    """Only a saved copy that the committed final vouches for counts as the kept class. One that was changed after
    signing is refused as before (its own message), and nothing about the row changes."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical="", relative_worktree=True
    )
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["publish_pending"]["lane_snapshot"]["assignee"] = "someone-else"
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    err = capsys.readouterr().err.lower()
    assert "differs from transaction evidence" in err and "the evidence is kept" not in err
    assert lanes.lanes_path(store).read_bytes() == before


def test_repairing_the_row_does_not_finish_a_committed_delivery_with_a_relative_saved_copy_and_no_canonical(
    tmp_path, monkeypatch, capsys
):
    """What the refusal says is true: the signed saved copy of the lane inside the pending delivery is what is
    checked, so putting the real absolute folder into the lane row changes nothing about the retry. The message must
    therefore not promise that a repair of the row finishes the delivery."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, worktree, _head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical="", relative_worktree=True
    )
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    first = capsys.readouterr().err
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["worktree_path"] = str(worktree)  # the repair of the row
    lanes.save_lanes(store, data)
    after_repair = lanes.lanes_path(store).read_bytes()
    original_artifact = artifact.read_bytes()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) != 0
    second = capsys.readouterr().err
    assert "cannot complete this delivery" in first and "cannot complete this delivery" in second
    assert lanes.lanes_path(store).read_bytes() == after_repair and artifact.read_bytes() == original_artifact
    assert isinstance(lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"], dict)  # still unfinished


@pytest.mark.parametrize("canonical", ["", "\t", "relative/canonical"])
@pytest.mark.parametrize("resolves_here", [False, True])
@pytest.mark.parametrize("spelling", NON_ABSOLUTE)
def test_a_saved_worktree_path_that_is_not_absolute_with_no_absolute_canonical_is_never_read(
    tmp_path, monkeypatch, spelling, resolves_here, canonical
):
    """Any non-absolute spelling (relative, and on Windows `C:x` and `\\x`) with no absolute canonical is refused before
    any Git read, whether or not the spelling happens to name a folder here."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = canonical
    if spelling == "relative/worktree":
        row["worktree_path"] = os.path.relpath(worktree, tmp_path) if resolves_here else "no/such/relative/checkout"
    else:
        row["worktree_path"] = spelling
    reads = []
    monkeypatch.setattr(cli, "_git_read_one", lambda *args, **kwargs: reads.append(args) or "")
    with pytest.raises(cli._LaneRetryFromDeliveryFolder) as caught:  # never read against the cwd
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert reads == []
    message = str(caught.value)
    assert "Nothing was changed" in message and "any folder" in message
    assert "This version cannot complete this delivery; the evidence is kept; report it" in message
    assert "replace worktree_path" not in message.lower() and "then retry" not in message
    assert "remove" not in message.lower()
    with pytest.raises(lanes.LaneError) as plain:  # the normal path: the same row is simply refused
        cli._verify_lane_worktree(store, row, lane_id=lane_id)
    assert not isinstance(plain.value, cli._LaneRetryFromDeliveryFolder)


@pytest.mark.parametrize("place", ["here", "sibling", "inside-another-checkout"])
@pytest.mark.parametrize("spelling", NON_ABSOLUTE)
def test_a_saved_worktree_path_that_is_not_absolute_with_an_absolute_canonical_is_verified_through_the_canonical(
    tmp_path, monkeypatch, spelling, place
):
    monkeypatch.chdir(tmp_path)
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_toplevel_canonical"] = str(worktree)
    row["worktree_path"] = os.path.relpath(worktree, tmp_path) if spelling == "relative/worktree" else spelling
    if place != "here":
        folder = _retry_place(tmp_path, "sibling" if place == "sibling" else place)
        if place == "inside-another-checkout" and spelling == "relative/worktree":
            (folder / row["worktree_path"]).mkdir(parents=True)
        monkeypatch.chdir(folder)
    found = cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert found["worktree_toplevel_canonical"] == lanes.canonical_host_path(worktree) and found["head"]


def test_the_identity_checks_run_through_the_canonical_and_a_failure_there_is_a_plain_refusal(tmp_path, monkeypatch):
    from test_lanes import _git_rc

    monkeypatch.chdir(tmp_path)
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_path"] = os.path.relpath(worktree, tmp_path)
    for canonical, match in (
        (str(store.root), "primary checkout"),  # the primary checkout is not a lane worktree
        (str(tmp_path / "not-a-checkout"), "worktree toplevel"),  # nothing there to read
    ):
        row["worktree_toplevel_canonical"] = canonical
        with pytest.raises(lanes.LaneError, match=match) as caught:
            cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
        assert not isinstance(caught.value, cli._LaneRetryFromDeliveryFolder)  # a real failure: the normal rollback
    row["worktree_toplevel_canonical"] = str(worktree)
    (worktree / "src/core/a.py").write_text("tracked change\n", encoding="utf-8")
    assert _git_rc(worktree, "status", "--porcelain").stdout.strip()
    with pytest.raises(lanes.LaneError, match="staged/unstaged tracked changes"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert _git_rc(worktree, "checkout", "--", "src/core/a.py").returncode == 0  # clean again
    row["lane_id"] = "some-other-lane"
    # another lane's id inside the row: refused before any folder is read (#834), not only by the branch check
    with pytest.raises(lanes.LaneError, match="holds the lane id some-other-lane; the two must match"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)


def test_the_identity_checks_still_run_on_the_committed_snapshot_path_for_an_absolute_worktree_path(
    tmp_path, monkeypatch
):
    from test_lanes import _git_rc

    monkeypatch.chdir(tmp_path)
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_path"] = str(worktree)
    row["worktree_toplevel_canonical"] = str(worktree)
    assert cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)["head"]
    row["worktree_toplevel_canonical"] = str(tmp_path / "somewhere-else")
    with pytest.raises(lanes.LaneError, match="no longer matches git") as caught:  # a real difference is refused
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert not isinstance(caught.value, cli._LaneRetryFromDeliveryFolder)  # a real failure: the normal rollback
    row["worktree_toplevel_canonical"] = str(worktree)
    (worktree / "src/core/a.py").write_text("tracked change\n", encoding="utf-8")
    assert _git_rc(worktree, "status", "--porcelain").stdout.strip()
    with pytest.raises(lanes.LaneError, match="staged/unstaged tracked changes"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert _git_rc(worktree, "checkout", "--", "src/core/a.py").returncode == 0  # clean again
    row["lane_id"] = "some-other-lane"
    # another lane's id inside the row: refused before any folder is read (#834), not only by the branch check
    with pytest.raises(lanes.LaneError, match="holds the lane id some-other-lane; the two must match"):
        cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)


# ------------------------------------------------------ the advice says `lane recover` first for an unfinished delivery


def test_the_checkout_advice_depends_on_the_lane_state() -> None:
    plain = lanes.checkout_path_recovery(lane_id="x", lane={"status": lanes.STATUS_ACTIVE})
    assert "lane abandon --id x --acknowledge-missing-worktree" in plain and "lane recover" not in plain
    unfinished = {"status": lanes.STATUS_DELIVERED, "publish_pending": {"transaction_id": "t"}}
    advice = lanes.checkout_path_recovery(lane_id="x", lane=unfinished)
    # recover first; no second step is promised (what is left depends on what recover did), the reader repeats the
    # command: the retirement is then judged for the state that is left (see the matrix test below)
    assert "lane recover --id x --reason" in advice and "then repeat this command" in advice
    assert "lane abandon" not in advice and "acknowledge" not in advice
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
    assert f"lane recover --id {lane_id} --reason" in err and "then repeat this command" in err
    assert "lane abandon" not in err  # the acknowledgement refuses in this state: it is not offered
    assert cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "advice"]) == 0


# ------------------------------------------------- the advice matrix: state x path fields


def _advice_row(state, path):
    row = {"worktree_toplevel_canonical": ""}
    if path == "absolute":
        row["worktree_path"] = os.path.abspath(os.path.join("somewhere", "checkout"))
    elif path == "relative":
        row["worktree_path"] = ".worktrees/relative"
    elif path == "blank":
        row["worktree_path"] = ""
    if state == "active":
        row["status"] = lanes.STATUS_ACTIVE
    elif state == "delivered-unfinished":
        row.update(status=lanes.STATUS_DELIVERED, publish_pending={"transaction_id": "t"})
    elif state == "delivered-finished":
        row.update(status=lanes.STATUS_DELIVERED, publish_pending=False, cleanup_pending=False)
    elif state == "retired":
        row.update(status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_ACKNOWLEDGED)
    return row


@pytest.mark.parametrize("path", ["absolute", "relative", "blank", "none"])
@pytest.mark.parametrize("state", ["active", "delivered-unfinished", "delivered-finished", "retired"])
def test_the_checkout_advice_is_actionable_for_every_state_and_path(state, path) -> None:
    advice = lanes.checkout_path_recovery(lane_id="x", lane=_advice_row(state, path))
    retire = "lane abandon --id x --acknowledge-missing-worktree"
    assert "remove" not in advice.lower()  # removing worktree_path leaves a lane with "no worktree to acknowledge"
    if state == "retired" and path == "relative":
        # The retirement does not clear a non-absolute worktree_path, and it still blocks releases: the advice says how
        # to clear that exact row (replace it, or empty it), not "use a new lane id".
        assert "replace worktree_path in lanes.json lane 'x' with the checkout's real absolute folder" in advice
        assert 'set it to empty text ("")' in advice and "use a new lane id" not in advice
        return
    if state == "retired":
        assert advice == "this lane was already retired; use a new lane id"
        return
    if state == "delivered-unfinished":
        # recover first, then repeat the command: it names what is left for the row recover leaves (the retirement, and
        # the replace-first step for a relative path, are printed by that repeat; the next case of this test)
        assert "lane recover --id x --reason" in advice and "then repeat this command" in advice
        assert retire not in advice and "replace worktree_path" not in advice
        return
    assert retire in advice
    steps = {
        "recover": advice.find("lane recover --id x --reason"),
        "replace": advice.find("replace worktree_path in lanes.json lane 'x' with the checkout's real absolute folder"),
        "retire": advice.find(retire),
    }
    assert steps["recover"] < 0
    assert (steps["replace"] >= 0) == (path == "relative")  # only a relative path needs repairing before retiring
    assert steps["retire"] == max(steps.values())  # retiring is always the last step


def test_the_relative_worktree_refusal_says_replace_never_remove() -> None:
    message = cli._lane_relative_worktree_refusal(".worktrees/relative", "x")
    assert "Replace worktree_path in lanes.json lane 'x' with the checkout's real absolute folder" in message
    assert "remove" not in message.lower()


# ------------------------------------------------------------------ an embedded NUL in a saved path (review bot)


def _nul_path(tmp_path):
    return str(tmp_path / "checkout") + chr(0) + "x"


@pytest.mark.parametrize("command", ["abandon-acknowledge", "workspace", "check", "deliver"])
def test_a_blank_worktree_path_and_a_canonical_with_a_nul_gets_the_normal_refusal_with_lane_field_and_advice(
    tmp_path, damaged_lane, capsys, command
):
    """`os.lstat` raises ValueError (not OSError) for a path with an embedded NUL. The saved value is refused up front
    as damage in the lane's own field, with the advice, instead of the bare "lstat: embedded null character"."""
    store = damaged_lane("worktree_toplevel_canonical", _nul_path(tmp_path))
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"]["worktree_path"] = ""
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    argv = {
        "abandon-acknowledge": ["abandon", "--acknowledge-missing-worktree"],
        "workspace": ["workspace"],
        "check": ["check"],
        "deliver": ["deliver", "--from", "dev"],
    }[command]
    assert cli.main(["--root", str(store.root), "lane", argv[0], "--id", "damaged", *argv[1:]]) == 2
    err = capsys.readouterr().err
    assert "lanes.json lane 'damaged'" in err and "worktree_toplevel_canonical" in err
    assert "embedded NUL" in err and "absolute folder" in err
    assert "lstat" not in err and "embedded null character" not in err
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_a_saved_path_with_a_nul_is_a_damaged_checkout_field(tmp_path, field):
    row = {"worktree_path": str(tmp_path / "a"), "worktree_toplevel_canonical": str(tmp_path / "a")}
    row[field] = _nul_path(tmp_path)
    issue = lanes.checkout_issue(row, root=tmp_path)
    assert issue is not None and issue[0] == field and "NUL" in issue[1]


def test_looking_for_a_folder_never_leaks_a_value_error(tmp_path):
    """The shared lookup turns the ValueError for a NUL into the OSError every caller already treats as "cannot be
    looked at", so a row that was not judged by the shared check still fails closed."""
    with pytest.raises(OSError):
        cli._lane_entry_present(Path(_nul_path(tmp_path)))


# --------------------------------------- status names a step that runs for a finished publication with cleanup pending


def test_status_names_only_a_step_that_runs_for_a_finished_publication_with_cleanup_pending(
    tmp_path, monkeypatch, capsys
):
    """`lane recover` refuses a lane whose publication is complete, so status must not send the person to it. The step
    it names (`lane deliver`) is run as printed."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["cleanup_pending"] = True  # a cleanup that could not finish
    lanes.save_lanes(store, data)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "status"]) == 0
    shown = capsys.readouterr().out
    line = next(item for item in shown.splitlines() if lane_id in item and "cleanup_pending" in item)
    assert "lane recover" not in line and "run `lane deliver` to retry the cleanup" in line
    assert cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "probe"]) == 2
    capsys.readouterr()  # the old advice cannot run
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    assert lanes.load_lanes(store)["lanes"][lane_id]["cleanup_pending"] is False


_ABSENT = object()


@pytest.mark.parametrize("repair", ["set-false", "set-true-then-deliver"])
@pytest.mark.parametrize("stored", [None, _ABSENT, 0, ""], ids=["null", "missing", "zero", "empty"])
def test_status_gives_repair_guidance_not_lane_deliver_for_a_corrupt_cleanup_marker(
    tmp_path, monkeypatch, capsys, stored, repair
):
    """A finished publication whose cleanup marker is not an explicit true or false (null, missing, 0, "") is labelled
    corrupt. `lane deliver` reads such a marker as "nothing pending", exits 0 and leaves it (and the warning) as it is,
    and `lane recover` refuses, so status must not name either. It says what to set the field to; doing that clears
    the warning (false: the cleanup is done) or turns it into the ordinary pending cleanup that `lane deliver` finishes
    (true: the checkout still needs removing)."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0

    def save_marker(value):
        data = lanes.load_lanes(store)
        if value is _ABSENT:
            data["lanes"][lane_id].pop("cleanup_pending", None)
        else:
            data["lanes"][lane_id]["cleanup_pending"] = value
        lanes.save_lanes(store, data)

    def listed():
        capsys.readouterr()
        assert cli.main(["--root", str(store.root), "lane", "status"]) == 0
        return [item for item in capsys.readouterr().out.splitlines() if lane_id in item and "cleanup" in item]

    save_marker(stored)
    (line,) = listed()
    assert "cleanup_marker_corrupt" in line
    assert "lane deliver" not in line and "lane recover" not in line  # neither command clears it
    assert f"lanes.json lane {lane_id!r}" in line and "cleanup_pending" in line
    assert "`true`" in line and "`false`" in line  # what to set it to, and when

    # The controls behind that advice: deliver exits 0 but changes nothing; recover refuses.
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    assert lanes.load_lanes(store)["lanes"][lane_id].get("cleanup_pending", _ABSENT) == stored
    assert len(listed()) == 1
    assert cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "probe"]) == 2
    capsys.readouterr()

    if repair == "set-false":
        save_marker(False)
        assert listed() == []
    else:
        save_marker(True)
        (line,) = listed()
        assert "run `lane deliver` to retry the cleanup" in line and "cleanup_pending - " in line
        assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
        capsys.readouterr()
        assert lanes.load_lanes(store)["lanes"][lane_id]["cleanup_pending"] is False
        assert listed() == []


def test_status_still_names_deliver_or_recover_for_an_unfinished_publication(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "status"]) == 0
    line = next(item for item in capsys.readouterr().out.splitlines() if lane_id in item and "final_pending" in item)
    assert "(run `lane deliver` or `lane recover`)" in line


# ------------------------------------------ round 9: one shared damage check before every printed step (the audit)


@pytest.mark.parametrize("canonical_folder", ["gone", "exists"])
def test_acknowledgement_skips_a_damaged_worktree_path_and_uses_the_healthy_canonical(
    tmp_path, damaged_lane, capsys, canonical_folder
):
    """An absolute `worktree_path` with an embedded NUL is damaged (the shared check says so), so the fallback selection
    skips it and looks at the healthy canonical folder instead of failing at the lookup: gone -> retired, still there
    -> the normal "still exists" refusal that names that folder."""
    store = damaged_lane("worktree_path", str(tmp_path / "checkout") + chr(0) + "x")
    canonical = tmp_path / "checkout"
    if canonical_folder == "exists":
        canonical.mkdir()
    chosen = cli._lane_acknowledgement_path(store, lanes.load_lanes(store)["lanes"]["damaged"], lane_id="damaged")
    assert chosen == canonical
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    code = cli.main(["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"])
    err = capsys.readouterr().err
    assert "cannot be looked at" not in err and chr(0) not in err
    if canonical_folder == "gone":
        assert code == 0
        assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ABANDONED
    else:
        assert code == 2 and repr(str(canonical)) in err and "still exists" in err
        assert lanes.lanes_path(store).read_bytes() == before


def _status_lines(store, capsys, needle):
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "status"]) == 0
    return [item for item in capsys.readouterr().out.splitlines() if needle in item]


def test_status_names_only_the_path_specific_step_for_a_damaged_row_with_a_pending_cleanup(
    tmp_path, monkeypatch, capsys
):
    """A finished publication, cleanup pending, and a damaged checkout field: `lane deliver` refuses there, so status
    keeps only the acknowledgement step already in the damage message. The printed step is followed."""
    import shlex

    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["cleanup_pending"] = True
    data["lanes"][lane_id]["worktree_path"] = ""  # a damaged field (the checkout itself is already gone)
    lanes.save_lanes(store, data)
    (line,) = _status_lines(store, capsys, f"{lane_id}: damaged checkout - ")
    assert "lane deliver" not in line and "lane recover" not in line
    printed = re.findall(r"`(lane abandon --id [^`]+)`", line)
    assert printed == [f"lane abandon --id {lane_id} --acknowledge-missing-worktree"]
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 2  # control
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), *shlex.split(printed[0])]) == 0  # the printed step runs
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ABANDONED


def test_status_offers_the_abandon_step_for_unfinished_abandoned_rows_only_when_the_row_is_not_damaged(
    tmp_path, damaged_lane, capsys
):
    store = damaged_lane("worktree_path", "")
    data = lanes.load_lanes(store)
    healthy = dict(data["lanes"]["damaged"])
    healthy.update(lane_id="healthy", worktree_path=str(tmp_path / "other"))
    healthy["worktree_toplevel_canonical"] = str(tmp_path / "other")
    for row in (data["lanes"]["damaged"], healthy):
        row.update(status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_FAILED)
    data["lanes"]["healthy"] = healthy
    lanes.save_lanes(store, data)
    (damaged_line,) = _status_lines(store, capsys, "  damaged: cleanup failed")
    (healthy_line,) = _status_lines(store, capsys, "  healthy: cleanup failed")
    assert "acknowledge-missing-worktree" not in damaged_line  # its own "damaged checkout:" line carries the step
    assert "lane abandon --id healthy --acknowledge-missing-worktree" in healthy_line
    shown = _status_lines(store, capsys, "damaged checkout:")
    assert shown and "lane abandon --id damaged --acknowledge-missing-worktree" in shown[0]


def test_recover_does_not_send_anyone_to_deliver_for_a_final_that_cannot_be_located(tmp_path, monkeypatch, capsys):
    """Audit: `lane recover` goes through the same terminal re-check, so for a committed final whose saved copy cannot
    be located it says so (nothing changed) instead of naming `lane deliver`, which cannot complete it either. The
    absolute-path control still names `lane deliver`, and that step runs."""
    (tmp_path / "a").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(
        tmp_path / "a", monkeypatch, canonical="", relative_worktree=True
    )
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "recover", "--id", lane_id, "--reason", "probe"]) == 3
    err = capsys.readouterr().err.lower()
    assert "this version cannot complete this delivery; the evidence is kept; report it" in err
    assert "run `lane deliver`" not in err
    assert lanes.lanes_path(store).read_bytes() == before
    (tmp_path / "b").mkdir()
    monkeypatch.chdir(tmp_path / "b")
    store2, lane_id2, *_rest = _committed_unpublished_delivery(tmp_path / "b", monkeypatch)
    capsys.readouterr()
    assert cli.main(["--root", str(store2.root), "lane", "recover", "--id", lane_id2, "--reason", "probe"]) == 3
    assert "run `lane deliver` to complete its publication marker" in capsys.readouterr().err
    assert cli.main(["--root", str(store2.root), "lane", "deliver", "--id", lane_id2, "--from", "dev"]) == 0


def test_reset_points_at_the_step_status_prints_not_at_commands_that_may_not_run(tmp_path, monkeypatch, capsys):
    """Audit: a lane that only has its cleanup pending is a recovery row too, and `lane recover` cannot run there, so
    `reset` no longer names the two commands: it points at `lane status`, which prints the step that runs."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, _worktree, _head, _artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["cleanup_pending"] = True
    lanes.save_lanes(store, data)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "reset"]) == 2
    err = capsys.readouterr().err
    assert lane_id in err and "`lane status`" in err and "follow the step it prints" in err
    assert "`lane recover`" not in err and "`lane deliver`" not in err


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_release_keeps_the_shared_reason_for_a_nul_in_a_saved_path(tmp_path, field):
    from agenttalk import worktree_release

    nul = str(tmp_path / "x") + chr(0) + "y"
    value = {"worktree_path": str(tmp_path / "a"), "worktree_toplevel_canonical": str(tmp_path / "a")}
    value[field] = nul
    with pytest.raises(worktree_release.Refused) as caught:
        worktree_release._names_checkout(value, tmp_path / "t", tmp_path, lane_id="x", source="lane row", lane=True)
    message = str(caught.value)
    assert field in message and "contains an embedded NUL character" in message
    assert "relative" not in message and "record an absolute path first" in message


@pytest.mark.parametrize("command", ["workspace", "check", "restamp", "deliver"])
@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_audit_every_lane_command_prints_a_step_that_runs_for_a_damaged_active_row(
    tmp_path, damaged_lane, capsys, command, field
):
    """Audit of the advice printed by status/workspace/check/restamp/deliver for one shared damage check: for a blank
    saved path the printed acknowledgement step is followed and retires the lane."""
    import shlex

    store = damaged_lane(field, "")
    argv = {
        "workspace": ["workspace"],
        "check": ["check"],
        "restamp": ["restamp", "--from", "lead", "--reason", "repair"],
        "deliver": ["deliver", "--from", "dev"],
    }[command]
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", argv[0], "--id", "damaged", *argv[1:]]) == 2
    err = capsys.readouterr().err
    printed = re.findall(r"`(lane abandon --id [^`]+)`", err)
    assert printed == ["lane abandon --id damaged --acknowledge-missing-worktree"]
    assert cli.main(["--root", str(store.root), *shlex.split(printed[0])]) == 0
    assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ABANDONED


# ---- round 10: a delivery this version cannot complete gets only the honest report ----


def test_deliver_prints_only_the_cannot_complete_report_for_a_final_that_cannot_be_located(
    tmp_path, monkeypatch, capsys
):
    """A first `lane deliver` that meets the cannot-complete state while finishing must not add the generic "retry the
    same command" (a retry hits the same wall): it prints the report line and nothing else is promised. There is no step
    to follow, so the evidence is checked instead: the row is unchanged by the report."""
    from test_lanes import _assign_worktree, _commit, _repo

    monkeypatch.chdir(tmp_path)
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "unlocatable")
    _commit(Path(lane["worktree_path"]), "src/core/a.py", "base\nmore\n")
    store = Store(root)
    with pytest.raises(cli._LaneRetryFromDeliveryFolder) as raised:
        cli._lane_committed_checkout_folder(store, {"worktree_path": "rel/checkout"}, "unlocatable")

    def cannot_complete(*_args, **_kwargs):
        raise raised.value

    monkeypatch.setattr(cli, "_lane_finalize_delivery", cannot_complete)
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", "unlocatable", "--from", "dev"]) == 2
    err = capsys.readouterr().err.lower()
    assert "this version cannot complete this delivery; the evidence is kept; report it" in err
    assert "retry" not in err and "pending recovery" not in err


def _recovery_line(store, capsys, lane_id):
    (line,) = _status_lines(store, capsys, f"  {lane_id}: ")
    return line


@pytest.mark.parametrize("canonical", ["", lambda _worktree: None], ids=["blank-canonical", "no-canonical"])
def test_status_names_no_deliver_or_recover_step_for_a_final_that_cannot_be_located(
    tmp_path, monkeypatch, capsys, canonical
):
    """Status asks the rule `lane deliver` and `lane recover` use. For a valid committed final whose saved copy has a
    relative worktree path and no absolute canonical, neither command can finish it, so the only line is the report. The
    controls (an absolute canonical, an absolute path) keep their step, and the step runs."""
    (tmp_path / "a").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    store, lane_id, *_rest = _committed_unpublished_delivery(
        tmp_path / "a", monkeypatch, canonical=canonical, relative_worktree=True
    )
    before = lanes.lanes_path(store).read_bytes()
    line = _recovery_line(store, capsys, lane_id)
    assert "this version cannot complete this delivery; the evidence is kept; report it" in line
    assert "lane deliver" not in line and "lane recover" not in line
    capsys.readouterr()
    assert cli.main(["--root", str(store.root), "lane", "status"]) == 0
    printed = capsys.readouterr().out  # no other line (a "damaged checkout:" one included) names either command
    assert "lane deliver" not in printed and "lane recover" not in printed and "damaged checkout" not in printed
    assert lanes.lanes_path(store).read_bytes() == before  # status only reads

    for name, kwargs in (("b", {"relative_worktree": True}), ("c", {})):  # absolute canonical / absolute path
        (tmp_path / name).mkdir()
        monkeypatch.chdir(tmp_path / name)
        control, control_id, *_rest = _committed_unpublished_delivery(tmp_path / name, monkeypatch, **kwargs)
        line = _recovery_line(control, capsys, control_id)
        assert "run `lane deliver` or `lane recover`" in line and "cannot complete" not in line
        assert cli.main(["--root", str(control.root), "lane", "deliver", "--id", control_id, "--from", "dev"]) == 0


@pytest.mark.parametrize("field", ["worktree_path", "worktree_toplevel_canonical"])
def test_an_unpaired_surrogate_in_a_saved_path_is_damage_with_a_named_reason(tmp_path, field):
    """A path that strict UTF-8 cannot encode (lanes.json is saved that way) names no folder; the shared check
    refuses it, so an acknowledgement never picks it ahead of a healthy path and a canonical like it is not an
    absolute canonical."""
    bad = str(tmp_path / "x") + "\ud800"
    reason = (
        "contains a character that cannot be written to lanes.json (an unpaired surrogate; "
        "the file is saved as strict UTF-8)"
    )
    assert lanes.checkout_path_problem({field: bad}) == (field, reason)
    assert lanes.checkout_issue({field: bad}, root=tmp_path) == (field, reason)
    assert cli._lane_absolute_canonical(Store(tmp_path), {"worktree_toplevel_canonical": bad}) is None
    healthy = str(tmp_path / "ok")
    row = {"worktree_path": healthy, "worktree_toplevel_canonical": healthy}
    row[field] = bad
    if field == "worktree_toplevel_canonical":  # judged first, like a relative or NUL canonical: repair advice
        with pytest.raises(lanes.LaneError, match="unpaired surrogate"):
            cli._lane_acknowledgement_path(Store(tmp_path), row, lane_id="x")
    else:  # a bad worktree_path is skipped, never chosen ahead of the healthy canonical
        assert cli._lane_acknowledgement_path(Store(tmp_path), row, lane_id="x") == Path(healthy)


# ---- round 11: a whitespace-only committed worktree path is not an absolute path ----

STORED_NON_ABSOLUTE = [" ", "\t \t", "relative/checkout", "C:x", "\\x"]


@pytest.mark.parametrize("cwd", RETRY_FOLDERS)
@pytest.mark.parametrize("spelling", STORED_NON_ABSOLUTE)
def test_a_committed_snapshot_path_is_judged_as_stored_so_a_whitespace_only_one_goes_through_the_canonical(
    tmp_path, monkeypatch, spelling, cwd
):
    """Git allows a checkout folder named with a space, so the committed snapshot's `worktree_path` is judged exactly as
    stored: a value that is not a full absolute path (whitespace only included) is never fed to Git relative to the
    retry's folder. The checkout is verified through the snapshot's ABSOLUTE canonical, from the original folder or any
    other, and with no absolute canonical the delivery is kept whole (the report error, nothing read)."""
    monkeypatch.chdir(tmp_path)
    store, lane_id, row, worktree = _real_lane_row(tmp_path)
    row["worktree_path"] = spelling
    row["worktree_toplevel_canonical"] = str(worktree)
    monkeypatch.chdir(_retry_folder(tmp_path, cwd))
    assert cli._lane_worktree_path_is_relative(row)
    found = cli._verify_lane_worktree(store, row, lane_id=lane_id, check_paths=False)
    assert found["worktree_toplevel_canonical"] == lanes.canonical_host_path(worktree)
    for none in ("", None):  # no absolute canonical: nothing is located from any folder
        with pytest.raises(cli._LaneRetryFromDeliveryFolder, match="cannot complete this delivery"):
            cli._verify_lane_worktree(
                store, dict(row, worktree_toplevel_canonical=none), lane_id=lane_id, check_paths=False
            )
    assert cli._lane_row_delivery_unlocatable(
        store, {"publish_pending": {"lane_snapshot": dict(row, worktree_toplevel_canonical="")}}
    )
    assert not cli._lane_row_delivery_unlocatable(store, {"publish_pending": {"lane_snapshot": row}})


def test_a_blank_committed_worktree_path_is_still_not_a_relative_spelling(tmp_path):
    """Only a non-empty value that is not absolute counts; an empty or absent one is the other kind of damage."""
    assert not cli._lane_worktree_path_is_relative({"worktree_path": ""})
    assert not cli._lane_worktree_path_is_relative({})
    assert not cli._lane_worktree_path_is_relative({"worktree_path": str(tmp_path)})


@pytest.mark.parametrize("field", ["worktree_toplevel_canonical", "worktree_path"])
def test_acknowledging_a_row_whose_saved_path_cannot_be_stored_leaves_lanes_json_unchanged(damaged_lane, capsys, field):
    """A JSON escape for an unpaired surrogate loads, but the registry is saved as strict UTF-8 and cannot be
    written with it. The acknowledgement would fail at its save: a saved canonical like that is refused first with
    its repair advice (exit 2), and in both cases lanes.json is untouched."""
    store = damaged_lane(field, "placeholder")
    path = lanes.lanes_path(store)
    path.write_bytes(path.read_bytes().replace(b'"placeholder"', json.dumps("C:\\gone\ud800").encode("ascii")))
    before = path.read_bytes()
    capsys.readouterr()
    argv = ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    if field == "worktree_toplevel_canonical":  # the canonical rule prints its repair advice, as for a NUL
        assert field in err and "unpaired surrogate" in err and "Edit this lane's record in lanes.json" in err
    assert path.read_bytes() == before  # (a bad worktree_path also fails without a change; its advice is #793)


# ---- round 12: one as-stored rule for a non-absolute worktree_path in every reader ----

WHITESPACE_PATHS = [" ", "\t", "\u2003"]


def test_the_shared_rule_judges_worktree_path_exactly_as_stored(tmp_path):
    """A non-empty text that is not a full absolute path is non-absolute, whitespace only included (Git allows a folder
    named with a space). Only an empty, absent or non-text value is left out: that is the other kind of damage."""
    for spelling in (*WHITESPACE_PATHS, "relative/x", "C:x", "\\x"):
        assert lanes.worktree_path_is_relative({"worktree_path": spelling}), spelling
        assert cli._lane_worktree_path_is_relative({"worktree_path": spelling}), spelling
    for other in ("", None, 7, str(tmp_path)):
        assert not lanes.worktree_path_is_relative({"worktree_path": other}), other
    assert not lanes.worktree_path_is_relative({})


def test_the_committed_path_check_is_the_shared_rule_not_a_copy():
    import inspect

    assert "worktree_path_is_relative" in inspect.getsource(cli._lane_worktree_path_is_relative)
    assert "strip" not in inspect.getsource(cli._lane_worktree_path_is_relative)


@pytest.mark.parametrize("value", WHITESPACE_PATHS)
def test_the_repair_advice_for_a_whitespace_only_path_names_the_replace_step(value):
    advice = lanes.checkout_path_recovery(lane_id="x", lane={"status": lanes.STATUS_ACTIVE, "worktree_path": value})
    assert "replace worktree_path in lanes.json lane 'x'" in advice and "real absolute folder" in advice


@pytest.mark.parametrize("value", WHITESPACE_PATHS)
def test_acknowledging_a_whitespace_only_worktree_path_is_refused_like_any_relative_path(
    damaged_lane, capsys, monkeypatch, tmp_path, value
):
    """The bot's reproduction: a lane whose saved worktree_path is only whitespace (and no usable canonical path) with a
    real folder of that name where the command runs must not be acknowledged as missing. It is refused like every other
    non-absolute path: exit 2, lanes.json byte-identical, and the advice names replacing worktree_path first."""
    store = damaged_lane("worktree_path", value)
    data = lanes.load_lanes(store)
    data["lanes"]["damaged"].pop("worktree_toplevel_canonical")  # no usable canonical path
    lanes.save_lanes(store, data)
    monkeypatch.chdir(tmp_path)
    try:
        (tmp_path / value).mkdir()  # a real folder of that name, where the platform allows one
    except OSError:
        pass
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    argv = ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "Replace worktree_path in lanes.json lane 'damaged'" in err and "nothing was changed" in err
    assert lanes.lanes_path(store).read_bytes() == before


@pytest.mark.parametrize("value", WHITESPACE_PATHS)
def test_release_never_drops_a_whitespace_only_worktree_path_even_on_an_acknowledged_row(tmp_path, value):
    """Retiring acknowledged damage must not hide a path that, as stored, may still name a checkout: the release reader
    refuses (fails closed) instead of dropping the field and answering "not this checkout"."""
    from agenttalk import worktree_release

    ack = {"status": lanes.STATUS_ABANDONED, "worktree_state": lanes.STATUS_CLEANUP_ACKNOWLEDGED}
    for row in (_release_row(**ack, worktree_path=value), _release_row(worktree_path=value)):
        with pytest.raises(worktree_release.Refused, match="worktree_path"):
            worktree_release._names_checkout(
                row, tmp_path / "t", tmp_path, lane_id=BAD, source="lanes.json lane bad", lane=True
            )


def _follow_the_whitespace_advice(store, capsys, field, value, *, lane_id="damaged"):
    """A saved worktree_path of only whitespace is non-absolute as stored, so it is never acknowledged as missing: the
    first attempt is refused (exit 2) with nothing changed (lanes.json byte-identical) and advice that names replacing
    it with the real absolute folder or, when the checkout is really gone, emptying it. This asserts all of that and
    then FOLLOWS the advice (empties the field), so the caller's own point (retiring it, or what the retirement checks)
    carries on with an empty value. Any other value is returned untouched."""
    if (
        field != "worktree_path"
        or not isinstance(value, str)
        or not lanes.worktree_path_is_relative({"worktree_path": value})
    ):
        return value
    before = lanes.lanes_path(store).read_bytes()
    capsys.readouterr()
    argv = ["--root", str(store.root), "lane", "abandon", "--id", lane_id, "--acknowledge-missing-worktree"]
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert f"Replace worktree_path in lanes.json lane {lane_id!r}" in err and "real absolute folder" in err
    assert 'set worktree_path to empty text ("")' in err and "nothing was changed" in err
    assert lanes.lanes_path(store).read_bytes() == before
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["worktree_path"] = ""  # the advice, followed
    lanes.save_lanes(store, data)
    return ""


# ---- round 12, part 2: status says the same honest thing in JSON and in text ----


@pytest.mark.parametrize("mode", ["text", "json"])
@pytest.mark.parametrize("case", ["blank-canonical", "relative-canonical", "row-repaired-since"])
def test_status_reports_a_final_that_cannot_be_completed_the_same_way_in_text_and_json(
    tmp_path, monkeypatch, capsys, case, mode
):
    """A valid committed final whose signed saved copy has a relative worktree path and no usable absolute canonical is
    reported as exactly that, decided on the record before text or JSON is chosen: the report and no recover, repair,
    deliver or abandon step (every one of them names something that cannot finish it). The same when the CURRENT row
    was repaired since (the signed copy stays what it was). The registry and the final are only read."""
    from test_lanes import _committed_artifacts

    monkeypatch.chdir(tmp_path)
    canonical = _relative_to_cwd if case == "relative-canonical" else ""
    store, lane_id, worktree, _head, artifact = _committed_unpublished_delivery(
        tmp_path, monkeypatch, canonical=canonical, relative_worktree=True
    )
    if case == "row-repaired-since":
        _repair_the_row(store, lane_id, worktree)
    before = lanes.lanes_path(store).read_bytes()
    original_artifact = artifact.read_bytes()
    capsys.readouterr()
    argv = ["--root", str(store.root), "lane", "status", *(["--json"] if mode == "json" else [])]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    if mode == "json":
        (row,) = [item for item in json.loads(out) if item.get("lane_id") == lane_id]
        assert row["recovery_state"] == "final_pending_marker"
        assert "this version cannot complete this delivery; the evidence is kept; report it" in row["recovery_detail"]
        assert "checkout_path_error" not in row  # its advice names the steps that cannot finish this delivery
    else:
        (line,) = [line for line in out.splitlines() if line.startswith(f"  {lane_id}: ")]
        assert "this version cannot complete this delivery; the evidence is kept; report it" in line
        assert "damaged checkout" not in out
    for step in ("lane recover", "lane deliver", "abandon", "repair"):
        assert step not in out, step
    assert lanes.lanes_path(store).read_bytes() == before
    assert artifact.read_bytes() == original_artifact and _committed_artifacts(store, lane_id) == [artifact]


@pytest.mark.parametrize("value", WHITESPACE_PATHS)
def test_emptying_a_whitespace_only_worktree_path_as_advised_lets_the_acknowledgement_retire_the_lane(
    damaged_lane, capsys, value
):
    """The way out for a checkout that is really gone: the refusal says to empty the field, and doing exactly that, then
    acknowledging again, retires the lane (the refusal itself changed nothing)."""
    store = damaged_lane("worktree_path", value)
    assert _follow_the_whitespace_advice(store, capsys, "worktree_path", value) == ""
    assert lanes.load_lanes(store)["lanes"]["damaged"]["worktree_path"] == ""
    argv = ["--root", str(store.root), "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]
    assert cli.main(argv) == 0
    row = lanes.load_lanes(store)["lanes"]["damaged"]
    assert row["status"] == lanes.STATUS_ABANDONED and row["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


# ---- round 13: a saved path the registry cannot be written with gets repair advice, never the acknowledgement ----


def _unwritable_worktree_path(damaged_lane, tmp_path):
    """An ABSOLUTE worktree_path holding an unpaired surrogate (a JSON escape stores it; the file is saved as strict
    UTF-8, so no command can save the registry with it)."""
    store = damaged_lane("worktree_path", "placeholder")
    path = lanes.lanes_path(store)
    escaped = json.dumps(str(tmp_path / "gone") + "\ud800").encode("ascii")
    path.write_bytes(path.read_bytes().replace(b'"placeholder"', escaped))
    return store, path


def test_a_worktree_path_the_registry_cannot_be_written_with_gets_repair_advice_not_the_acknowledgement(
    damaged_lane, capsys, tmp_path
):
    store, path = _unwritable_worktree_path(damaged_lane, tmp_path)
    before = path.read_bytes()
    root = ["--root", str(store.root)]
    capsys.readouterr()
    assert cli.main([*root, "lane", "status"]) == 0
    text = capsys.readouterr().out
    assert cli.main([*root, "lane", "status", "--json"]) == 0
    (item,) = [r for r in json.loads(capsys.readouterr().out) if r.get("lane_id") == "damaged"]
    for said in (text, item["checkout_path_error"]):
        assert "worktree_path" in said and "unpaired surrogate" in said and "strict UTF-8" in said
        assert "Edit this lane's record in lanes.json" in said and "--acknowledge-missing-worktree" not in said
    for argv in (
        ["lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"],
        ["lane", "abandon", "--id", "damaged"],
    ):
        assert cli.main([*root, *argv]) == 2
        err = capsys.readouterr().err
        assert "Edit this lane's record in lanes.json" in err and "strict UTF-8" in err
        assert "--acknowledge-missing-worktree" not in err and "codec" not in err
        assert path.read_bytes() == before
    assert lanes.load_lanes(store)["lanes"]["damaged"]["status"] == lanes.STATUS_ACTIVE
    data = lanes.load_lanes(store)  # the advice, followed: empty the field, and the usual retirement then works
    data["lanes"]["damaged"]["worktree_path"] = ""
    lanes.save_lanes(store, data)
    capsys.readouterr()
    assert cli.main([*root, "lane", "abandon", "--id", "damaged", "--acknowledge-missing-worktree"]) == 0


# ---------------------------------------------------------------------------------------------------------------------
# Removal safety (zteam's final read of PR #771): a checkout is removed only when that exact checkout is proven to be
# the selected lane's own: its own absolute path, its own registry key, its own branch. Never another lane's folder.
# ---------------------------------------------------------------------------------------------------------------------


def _assign_second_lane(root: Path, base: str, lane_id: str = "other") -> Path:
    """A second, non-overlapping lane (path src/other) through the public command; returns its checkout folder."""
    from test_lanes import _branch

    assert cli.main([
        "--root", str(root), "lane", "assign", "--id", lane_id, "--from", "lead", "--assignee", "dev2",
        "--domain", "core", "--base", base, "--target", _branch(root), "--path", "src/other",
        "--worktrees-root", str(root / ".worktrees"),
    ]) == 0
    return Path(lanes.load_lanes(Store(root))["lanes"][lane_id]["worktree_path"])


def _update_row(store: Store, lane_id: str, **fields) -> None:
    data = lanes.load_lanes(store)
    data["lanes"][lane_id].update(fields)
    lanes.save_lanes(store, data)


def _point_lane_at(store: Store, lane_id: str, folder: Path) -> None:
    """Make the lane's two saved checkout fields name `folder`, both written absolute."""
    canonical = lanes.canonical_host_path(folder)
    _update_row(store, lane_id, worktree_path=str(folder), worktree_toplevel_canonical=canonical)


def _move_other_to_the_name_of(store: Store, first_checkout: Path, other_id: str = "other") -> Path:
    """The relative-path set-up of the review: the other lane's checkout is moved to <root>/<N>, N being the first
    checkout's folder name, and the other lane's row follows it. Returns that new folder."""
    from test_lanes import _git_rc

    other_checkout = Path(lanes.load_lanes(store)["lanes"][other_id]["worktree_path"])
    moved = store.root / first_checkout.name
    assert _git_rc(store.root, "worktree", "move", str(other_checkout), str(moved)).returncode == 0
    _point_lane_at(store, other_id, moved)
    return moved


def _published_with_cleanup_postponed(tmp_path, monkeypatch):
    """A delivery that is published, whose checkout cleanup has not run: the retry that does it is the test."""
    store, lane_id, checkout, head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    with monkeypatch.context() as postponed:
        postponed.setattr(cli, "_lane_deliver_cleanup", lambda *args, **kwargs: (artifact, True, head))
        cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"])
    assert lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"] is False
    return store, lane_id, checkout, head, artifact


def _assert_untouched(*folders: Path) -> None:
    gone = [f"{folder.parent.name}/{folder.name}" for folder in folders if not (folder / ".git").exists()]
    assert not gone, f"these checkouts were removed: {gone}"


def test_abandon_with_a_relative_path_removes_the_checkout_it_verified_or_nothing(tmp_path, monkeypatch, capsys):
    """A relative worktree_path was verified from the caller's folder and removed from the store folder: the command
    verified one checkout (the first lane's) and removed another (the other lane's). One absolute target is resolved
    once and handed to the removal, so what is removed is exactly what was verified."""
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    _assign_second_lane(root, base)
    store = Store(root)
    moved = _move_other_to_the_name_of(store, first)
    _update_row(store, "first", worktree_path=first.name)  # relative; the saved canonical path still names `first`
    monkeypatch.chdir(first.parent)  # from here the relative name reads as the first lane's own checkout
    capsys.readouterr()

    cli.main(["--root", str(root), "lane", "abandon", "--id", "first"])

    _assert_untouched(moved)  # the other lane's checkout is never touched
    assert (moved / "src" / "other" / "b.py").exists()
    assert not first.exists()  # the checkout that was verified is the one that was removed


def test_delivery_cleanup_with_a_relative_path_removes_the_checkout_it_verified_or_nothing(
    tmp_path, monkeypatch, capsys
):
    """The same split in the cleanup that ends a delivery: verified in one folder, removed from another."""
    store, lane_id, first, _head, artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
    base = lanes.load_lanes(store)["lanes"][lane_id]["base_sha"]
    _assign_second_lane(store.root, base)
    moved = _move_other_to_the_name_of(store, first)
    _update_row(store, lane_id, worktree_path=first.name)
    monkeypatch.chdir(first.parent)
    signed = artifact.read_bytes()
    capsys.readouterr()

    cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"])

    _assert_untouched(moved)
    assert not first.exists()  # the delivered lane's own checkout is the one that went
    assert artifact.read_bytes() == signed


def test_abandon_does_not_remove_a_detached_checkout_that_another_lane_owns(tmp_path, capsys):
    """Two checkouts at one commit are not one lane. A detached checkout at the lane branch's tip passed the check
    for ANY row that named it; it is the first lane's only when nothing says it is somebody else's."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    store = Store(root)
    assert _git_rc(other, "checkout", "--detach", base).returncode == 0
    _point_lane_at(store, "first", other)  # the first row now names the other lane's checkout
    capsys.readouterr()

    cli.main(["--root", str(root), "lane", "abandon", "--id", "first"])

    _assert_untouched(first, other)
    assert (other / "src" / "other" / "b.py").exists()


@pytest.mark.skipif(os.name != "nt", reason="a junction is a Windows folder link")
def test_abandon_does_not_remove_a_checkout_reached_through_a_junction(tmp_path, capsys):
    import subprocess

    from test_lanes import _assign_worktree, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    store = Store(root)
    assert _git_rc(other, "checkout", "--detach", base).returncode == 0
    junction = tmp_path / "via-junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(other)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("this machine does not allow a junction here")
    _update_row(store, "first", worktree_path=str(junction), worktree_toplevel_canonical=str(junction))
    capsys.readouterr()

    cli.main(["--root", str(root), "lane", "abandon", "--id", "first"])

    _assert_untouched(first, other)


def test_delivery_cleanup_does_not_remove_a_detached_checkout_that_another_lane_owns(tmp_path, monkeypatch, capsys):
    from test_lanes import _git_rc

    store, lane_id, first, head, artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
    base = lanes.load_lanes(store)["lanes"][lane_id]["base_sha"]
    other = _assign_second_lane(store.root, base)
    assert _git_rc(other, "checkout", "--detach", head).returncode == 0
    _point_lane_at(store, lane_id, other)
    signed = artifact.read_bytes()
    capsys.readouterr()

    cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"])

    _assert_untouched(first, other)
    assert artifact.read_bytes() == signed


def test_gc_does_not_remove_an_active_lanes_checkout_found_in_a_retired_lanes_folder(tmp_path, capsys):
    """gc finds a retired lane's folder by its name. A detached checkout of ANOTHER lane moved into that folder is
    that other lane's: gc must neither call it safe to remove nor remove it."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    store = Store(root)
    assert _git_rc(root, "worktree", "remove", str(first)).returncode == 0
    assert _git_rc(other, "checkout", "--detach", base).returncode == 0
    assert _git_rc(root, "worktree", "move", str(other), str(first)).returncode == 0
    _point_lane_at(store, "other", first)
    _update_row(store, "first", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "gc", "--delete", "--target", "HEAD", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    item = next(row for row in report["items"] if row["lane_id"] == "first")
    assert item["worktree_remove_safe"] is False
    assert item.get("worktree_removed") is not True
    _assert_untouched(first)
    assert (first / "src" / "other" / "b.py").exists()


def test_a_checkout_that_only_its_own_lane_names_is_still_removed(tmp_path, capsys):
    """The control for the ownership rules: a lane's own, attached checkout, named by no other lane, goes."""
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "abandon", "--id", "first"]) == 0

    assert not first.exists()
    _assert_untouched(other)


def test_delivery_retry_does_not_delete_an_unrelated_file_named_by_a_damaged_pending_record(
    tmp_path, monkeypatch, capsys
):
    """On a retry the committed final already exists, so publication returned early and the saved prepared-copy path
    reached the unlink unchecked: a hand-damaged record deleted any file its path named."""
    store, lane_id, _checkout, _head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    unrelated = tmp_path.parent / f"{tmp_path.name}-unrelated.txt"
    unrelated.write_text("not part of any delivery\n", encoding="utf-8")
    signed = artifact.read_bytes()
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["publish_pending"]["prepared_artifact"] = str(unrelated)
    lanes.save_lanes(store, data)
    capsys.readouterr()

    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0

    assert unrelated.read_text(encoding="utf-8") == "not part of any delivery\n"
    assert artifact.read_bytes() == signed
    assert lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"] is False  # the delivery still finished


def test_the_prepared_copy_of_a_delivery_is_removed_only_at_its_own_path(tmp_path, capsys):
    """Every remover of a prepared copy asks `_lane_remove_prepared`: its own path goes, any other path stays."""
    from test_lanes import _repo

    root, _base = _repo(tmp_path)
    store = Store(root)
    transaction = "ab" * 16
    own = lanes.prepared_delivery_artifact_path(store, "lane-a", transaction)
    own.parent.mkdir(parents=True)
    own.write_text("{}", encoding="utf-8")
    stray = tmp_path / "stray.json"
    stray.write_text("{}", encoding="utf-8")
    pending = {"lane_id": "lane-a", "transaction_id": transaction}

    other_lanes_copy = own.with_name("lane-b-" + transaction + ".prepared.json")
    for raw in (str(stray), str(own.parent), "", None, 7, str(other_lanes_copy)):
        cli._lane_remove_prepared(store, {**pending, "prepared_artifact": raw})
        assert stray.exists() and own.exists() and own.parent.exists()
    cli._lane_remove_prepared(store, {**pending, "transaction_id": "not-hex", "prepared_artifact": str(own)})
    assert own.exists()
    cli._lane_remove_prepared(store, {**pending, "prepared_artifact": str(own)})
    assert not own.exists() and stray.exists()
    capsys.readouterr()


def test_abandon_does_not_remove_a_detached_checkout_while_the_lane_branch_is_checked_out_elsewhere(tmp_path, capsys):
    """The branch alone settles it where no other record does: the first lane's branch is attached in its own
    checkout, so a detached checkout at the same commit is not the first lane's, whatever the first row says."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    store = Store(root)
    assert _git_rc(other, "checkout", "--detach", base).returncode == 0
    _point_lane_at(store, "first", other)
    _point_lane_at(store, "other", tmp_path / "nowhere")  # no other record names the detached checkout now
    capsys.readouterr()

    cli.main(["--root", str(root), "lane", "abandon", "--id", "first"])

    _assert_untouched(first, other)


def _link_directory(link: Path, target: Path) -> None:
    """Make `link` a folder link to `target`: a junction on Windows, a symbolic link elsewhere."""
    import subprocess

    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if made.returncode != 0:
            pytest.skip("this machine does not allow a junction here")
    else:
        os.symlink(target, link, target_is_directory=True)


def _unplaced_owner_case(tmp_path, monkeypatch, command, spelling):
    """Another lane's checkout, claimed by the selected lane's row, whose OWN row cannot be placed: its saved path is
    not a full path and it has no saved canonical path. Returns (run, victim): `run()` does the command, `victim` is
    the other lane's checkout, which must still be there afterwards."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    if command == "deliver":
        store, selected, mine, head, _artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
        tip = head
        other = _assign_second_lane(store.root, lanes.load_lanes(store)["lanes"][selected]["base_sha"])
        assert _git_rc(mine, "checkout", "--detach", head).returncode == 0  # the lane branch is checked out nowhere
        assert _git_rc(other, "checkout", "--detach", tip).returncode == 0
        _point_lane_at(store, selected, other)
        victim = other
        argv = ["lane", "deliver", "--id", selected, "--from", "dev"]
    else:
        root, base = _repo(tmp_path)
        store, selected = Store(root), "first"
        mine = Path(_assign_worktree(root, base, selected)["worktree_path"])
        other = _assign_second_lane(root, base)
        if command == "abandon":
            assert _git_rc(mine, "checkout", "--detach", base).returncode == 0
            assert _git_rc(other, "checkout", "--detach", base).returncode == 0
            _point_lane_at(store, selected, other)
            victim = other
            argv = ["lane", "abandon", "--id", selected]
        else:  # gc finds the retired lane's folder by name; the other lane's checkout has been moved into it
            assert _git_rc(root, "worktree", "remove", str(mine)).returncode == 0
            assert _git_rc(other, "checkout", "--detach", base).returncode == 0
            assert _git_rc(root, "worktree", "move", str(other), str(mine)).returncode == 0
            _update_row(store, selected, status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED)
            victim = mine
            argv = ["lane", "gc", "--delete", "--target", "HEAD", "--json"]
    if spelling == "relative":
        unplaced = "not-the-folder-it-looks-like"
    else:  # a drive-relative name, read against that drive's current folder: here the other checkout's parent
        unplaced = victim.drive + victim.name
        monkeypatch.chdir(victim.parent)
    _update_row(store, "other", worktree_path=unplaced, worktree_toplevel_canonical=None)
    return (lambda: cli.main(["--root", str(store.root), *argv])), victim


@pytest.mark.parametrize("command", ["abandon", "deliver", "gc"])
@pytest.mark.parametrize(
    "spelling",
    [
        "relative",
        pytest.param("drive-relative", marks=pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows")),
    ],
)
def test_a_removal_is_blocked_by_another_lanes_record_whose_path_cannot_be_placed(
    tmp_path, monkeypatch, capsys, command, spelling
):
    """An other-lane record that cannot be read as one folder (not a full path, no saved full canonical path) could be
    naming the very checkout about to be removed. It was skipped, so abandon, delivery cleanup and gc removed that
    other lane's checkout. It now blocks the removal unless the record's own canonical path rules it out."""
    if command != "abandon" and spelling == "drive-relative":
        pytest.skip("the drive-relative reading is the abandon reproduction")
    run, victim = _unplaced_owner_case(tmp_path, monkeypatch, command, spelling)
    capsys.readouterr()

    run()

    _assert_untouched(victim)
    assert (victim / "src" / "other" / "b.py").exists()


def test_another_lanes_relative_path_with_a_full_canonical_path_does_not_block_a_removal(tmp_path, capsys):
    """The control: the other record's relative spelling is ruled out by its own saved full canonical path, which names
    the other lane's real checkout, so the first lane's own checkout is still removed."""
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    _update_row(Store(root), "other", worktree_path=os.path.relpath(other, root))
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "abandon", "--id", "first"]) == 0

    assert not first.exists()
    _assert_untouched(other)


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_the_prepared_copy_is_not_deleted_through_a_linked_prepared_folder(tmp_path, monkeypatch, capsys, kind):
    """The expected file name and a matching canonical path do not make the managed `.prepared` folder trustworthy: if
    it is a link or junction, the path resolves into some other folder, and the file there is not the delivery's.
    Windows has the junction case (codex-rev's reproduction); the symbolic-link case is for systems that make them
    without special rights."""
    if kind == "junction" and os.name != "nt":
        pytest.skip("a junction is a Windows folder link")
    if kind == "symlink" and os.name == "nt":
        pytest.skip("a symbolic link needs special rights on Windows; the junction case covers it there")
    store, lane_id, _checkout, _head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    pending = lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]
    own = lanes.prepared_delivery_artifact_path(store, pending["lane_id"], pending["transaction_id"])
    own.parent.rename(own.parent.with_name(".prepared-original"))
    independent = tmp_path.parent / f"{tmp_path.name}-independent"
    independent.mkdir()
    victim = independent / own.name
    victim.write_text("not part of any delivery\n", encoding="utf-8")
    _link_directory(own.parent, independent)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["publish_pending"]["prepared_artifact"] = str(victim)
    lanes.save_lanes(store, data)
    signed = artifact.read_bytes()
    capsys.readouterr()

    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0

    assert victim.read_text(encoding="utf-8") == "not part of any delivery\n"
    assert artifact.read_bytes() == signed
    assert "left in place" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["abandon", "deliver", "gc"])
@pytest.mark.parametrize("dirt", ["tracked", "untracked"])
def test_no_removal_takes_a_detached_checkout_that_no_record_shares_while_it_holds_unsaved_work(
    tmp_path, monkeypatch, capsys, command, dirt
):
    """The one case accepted on the commit match alone (a detached checkout at the branch tip that no other record
    names and whose branch is checked out nowhere) can only ever lose a clean checkout: a changed tracked file or an
    untracked file stops every removal path, and nothing forces it."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    if command == "deliver":
        store, selected, checkout, head, _artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
        assert _git_rc(checkout, "checkout", "--detach", head).returncode == 0
        argv = ["lane", "deliver", "--id", selected, "--from", "dev"]
    else:
        root, base = _repo(tmp_path)
        store, selected = Store(root), "first"
        checkout = Path(_assign_worktree(root, base, selected)["worktree_path"])
        assert _git_rc(checkout, "checkout", "--detach", base).returncode == 0
        if command == "abandon":
            argv = ["lane", "abandon", "--id", selected]
        else:
            _update_row(store, selected, status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED)
            argv = ["lane", "gc", "--delete", "--target", "HEAD", "--json"]
    work = checkout / ("src/core/a.py" if dirt == "tracked" else "notes-not-saved.txt")
    work.write_text("work that was never committed\n", encoding="utf-8")
    capsys.readouterr()

    cli.main(["--root", str(store.root), *argv])

    _assert_untouched(checkout)
    assert work.read_text(encoding="utf-8") == "work that was never committed\n"


# ---------------------------------------------------------------------------------------------------------------------
# Round 22 (zteam's read of ebe81643): a forced reassignment moves a lane's checkout into its retained claims
# (`occupancy_worktrees`); a removal for another lane must see that claim too.
# ---------------------------------------------------------------------------------------------------------------------


def _reassign_without_a_checkout(root: Path, base: str, lane_id: str = "other") -> None:
    """The ordinary forced reassignment of review: the lane keeps no current checkout, and the checkout it had moves
    into `occupancy_worktrees`."""
    from test_lanes import _branch

    assert cli.main([
        "--root", str(root), "lane", "assign", "--id", lane_id, "--from", "lead", "--assignee", "dev2",
        "--domain", "core", "--base", base, "--target", _branch(root), "--path", "src/other", "--force",
        "--advisory", "--no-worktree", "--worktree-waiver-reason", "review fixture retained generation",
    ]) == 0


def test_forced_reassignment_retains_the_original_absolute_raw_path(tmp_path):
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    row = _assign_worktree(root, base, "first")
    assert not row.get("occupancy_worktrees")
    store = Store(root)
    raw = str(root / "a-different-absolute-spelling")
    _update_row(store, "first", worktree_path=raw)
    _reassign_without_a_checkout(root, base, "first")
    assert lanes.load_lanes(store)["lanes"]["first"]["occupancy_worktrees"] == [raw]
    assert row["worktree_toplevel_canonical"] != raw


@pytest.mark.parametrize("case", ["relative-with-canonical", "relative-without-canonical", "no-checkout",
                                  "no-checkout-existing", "retired", "damaged", "retired-hidden-damage"])
def test_reassignment_preserves_or_refuses_the_previous_checkout(tmp_path, capsys, case):
    from test_lanes import _assign_worktree, _branch, _repo

    root, base = _repo(tmp_path)
    row = _assign_worktree(root, base, "first")
    store = Store(root)
    canonical = row["worktree_toplevel_canonical"]
    fields = {
        "relative-with-canonical": {"worktree_path": "relative", "worktree_toplevel_canonical": canonical},
        "relative-without-canonical": {"worktree_path": "relative", "worktree_toplevel_canonical": None},
        "no-checkout": {"worktree_path": None, "worktree_toplevel_canonical": None},
        "no-checkout-existing": {"worktree_path": None, "worktree_toplevel_canonical": None,
                                 "occupancy_worktrees": [str(root / "earlier")]},
        "retired": {"worktree_path": "", "worktree_toplevel_canonical": "", "status": lanes.STATUS_ABANDONED,
                    "worktree_state": lanes.STATUS_CLEANUP_ACKNOWLEDGED},
        "damaged": {"worktree_path": "bad\x00path", "worktree_toplevel_canonical": canonical},
        "retired-hidden-damage": {"worktree_path": "", "worktree_toplevel_canonical": 42,
                                  "status": lanes.STATUS_ABANDONED,
                                  "worktree_state": lanes.STATUS_CLEANUP_ACKNOWLEDGED},
    }[case]
    _update_row(store, "first", **fields)
    code = cli.main(["--root", str(root), "lane", "assign", "--id", "first", "--from", "lead", "--assignee", "dev2",
                     "--domain", "core", "--base", base, "--target", _branch(root), "--path", "src/other", "--force",
                     "--advisory", "--no-worktree", "--worktree-waiver-reason", "test"])
    text = capsys.readouterr().err
    if case in {"relative-without-canonical", "damaged", "retired", "retired-hidden-damage"}:
        assert code != 0 and ("real absolute folder" in text or "NUL" in text or "retired" in text), text
    else:
        assert code == 0, text
        claims = lanes.load_lanes(store)["lanes"]["first"].get("occupancy_worktrees", [])
        expected = ([canonical] if case == "relative-with-canonical" else
                    [str(root / "earlier")] if case == "no-checkout-existing" else [])
        assert claims == expected


@pytest.mark.parametrize("case", ["doubled_slash_canonical"])
def test_forced_reassignment_with_a_damaged_earlier_field_is_refused_as_before(tmp_path, monkeypatch, capsys, case):
    import posixpath
    from test_lanes import _assign_worktree, _branch, _repo

    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "first")
    _update_row(Store(root), "first", worktree_toplevel_canonical="/p/co//wt")
    monkeypatch.setattr(lanes, "os", types.SimpleNamespace(name="posix", path=posixpath))
    assert "older version" in lanes.canonical_checkout_problem({"worktree_toplevel_canonical": "/p/co//wt"}, root="")[1]
    code = cli.main(["--root", str(root), "lane", "assign", "--id", "first", "--from", "lead", "--assignee", "dev2",
                     "--domain", "core", "--base", base, "--target", _branch(root), "--path", "src/other", "--force",
                     "--advisory", "--no-worktree", "--worktree-waiver-reason", "test"])
    assert code == 2 and "worktree_toplevel_canonical" in capsys.readouterr().err


def test_a_raw_path_with_a_doubled_slash_is_not_kept_as_the_claim(tmp_path, monkeypatch):
    import posixpath
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "first")
    _update_row(Store(root), "first", worktree_path="/p/co//wt", worktree_toplevel_canonical="/p/canonical")
    monkeypatch.setattr(lanes, "os", types.SimpleNamespace(name="posix", path=posixpath))
    _reassign_without_a_checkout(root, base, "first")
    assert lanes.load_lanes(Store(root))["lanes"]["first"]["occupancy_worktrees"] == ["/p/canonical"]


def test_guarded_absolute_raw_without_canonical_is_not_called_relative(tmp_path, monkeypatch, capsys):
    import posixpath
    from test_lanes import _assign_worktree, _branch, _repo
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "first")
    _update_row(Store(root), "first", worktree_path="/p/co//wt", worktree_toplevel_canonical=None)
    monkeypatch.setattr(lanes, "os", types.SimpleNamespace(name="posix", path=posixpath))
    code = cli.main(["--root", str(root), "lane", "assign", "--id", "first", "--from", "lead", "--assignee", "dev2",
                     "--domain", "core", "--base", base, "--target", _branch(root), "--force", "--advisory",
                     "--no-worktree", "--worktree-waiver-reason", "test"])
    assert code == 2 and "relative path" not in capsys.readouterr().err


def _retained_claim_case(tmp_path, monkeypatch, command):
    """Another lane's checkout that only its retained claims still name. Returns (run, victim)."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    if command == "deliver":
        store, selected, mine, head, _artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
        other = _assign_second_lane(store.root, lanes.load_lanes(store)["lanes"][selected]["base_sha"])
        assert _git_rc(mine, "checkout", "--detach", head).returncode == 0
        assert _git_rc(other, "checkout", "--detach", head).returncode == 0
        victim = other
        base = lanes.load_lanes(store)["lanes"][selected]["base_sha"]
        argv = ["lane", "deliver", "--id", selected, "--from", "dev"]
    else:
        root, base = _repo(tmp_path)
        store, selected = Store(root), "first"
        mine = Path(_assign_worktree(root, base, selected)["worktree_path"])
        other = _assign_second_lane(root, base)
        if command == "abandon":
            assert _git_rc(mine, "checkout", "--detach", base).returncode == 0
            assert _git_rc(other, "checkout", "--detach", base).returncode == 0
            victim = other
            argv = ["lane", "abandon", "--id", selected]
        else:
            assert _git_rc(root, "worktree", "remove", str(mine)).returncode == 0
            assert _git_rc(other, "checkout", "--detach", base).returncode == 0
            assert _git_rc(root, "worktree", "move", str(other), str(mine)).returncode == 0
            _point_lane_at(store, "other", mine)
            _update_row(store, selected, status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED)
            victim = mine
            argv = ["lane", "gc", "--delete", "--target", "HEAD", "--json"]
    _reassign_without_a_checkout(store.root, base)
    assert lanes.load_lanes(store)["lanes"]["other"].get("worktree_path") in (None, "")
    assert any(str(victim.name) in claim for claim in lanes.load_lanes(store)["lanes"]["other"]["occupancy_worktrees"])
    if command != "gc":
        _point_lane_at(store, selected, victim)
    return (lambda: cli.main(["--root", str(store.root), *argv])), victim


@pytest.mark.parametrize("command", ["abandon", "deliver", "gc"])
def test_a_removal_never_takes_a_checkout_that_another_lane_still_claims_as_retained(
    tmp_path, monkeypatch, capsys, command
):
    run, victim = _retained_claim_case(tmp_path, monkeypatch, command)
    capsys.readouterr()

    run()

    _assert_untouched(victim)
    assert (victim / "src" / "other" / "b.py").exists()


def test_a_lanes_own_retained_claim_does_not_stop_its_own_checkout_from_being_removed(tmp_path, capsys):
    """The control: the lane's own earlier-checkout claim is its own, so it blocks nothing."""
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    _update_row(Store(root), "first", occupancy_worktrees=[str(tmp_path / "an-earlier-checkout")])
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "abandon", "--id", "first"]) == 0

    assert not first.exists()


def test_gc_removes_the_checkout_of_a_retired_lane_whose_row_names_no_folder(tmp_path, capsys):
    """The control for the lane's own managed folder: gc finds it through Git's list, inside this store's folder."""
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    _update_row(Store(root), "first", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED,
                worktree_path=None, worktree_toplevel_canonical=None)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "gc", "--delete", "--target", "HEAD", "--json"]) == 0

    assert not first.exists()


# ---------------------------------------------------------------------------------------------------------------------
# Round 23 (codex-rev's read of 806dcba9): a store made inside a lane checkout is store data, which Git ignores, so no
# clean check sees it, and removing the checkout deletes it. It may sit at the checkout's root or deeper.
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["abandon", "deliver", "gc"])
@pytest.mark.parametrize(
    ("where", "spelling"),
    [
        ("root", ".agenttalk"), ("deeper", ".agenttalk"), ("none", ".agenttalk"),
        # Round 24: on a case-insensitive disk (Windows, the macOS default) these are the very store `Store(...)`
        # opens as `.agenttalk`; the name is compared without regard to case on every system (refusing one on a
        # case-sensitive disk costs only the manual exit).
        ("root", ".AGENTTALK"), ("deeper", ".AGENTTALK"), ("root", ".AgentTalk"), ("deeper", ".AgentTalk"),
        # Round 27: a store inside a NESTED `.git` folder (a nested repository's metadata can hold anything). Only
        # the checkout's own `.git` is skipped by the scan.
        ("nested-git", ".agenttalk"), ("sub-git", ".agenttalk"),
    ],
)
def test_a_removal_never_deletes_a_store_made_inside_the_checkout(
    tmp_path, monkeypatch, capsys, command, where, spelling
):
    from test_lanes import _assign_worktree, _repo

    if command == "deliver":
        store, selected, checkout, _head, _artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
        argv = ["lane", "deliver", "--id", selected, "--from", "dev"]
    else:
        root, base = _repo(tmp_path)
        store, selected = Store(root), "first"
        checkout = Path(_assign_worktree(root, base, selected)["worktree_path"])
        if command == "abandon":
            argv = ["lane", "abandon", "--id", selected]
        else:
            _update_row(store, selected, status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED)
            argv = ["lane", "gc", "--delete", "--target", "HEAD", "--json"]
    kept = None
    if where != "none":
        inner = {
            "root": checkout,
            "deeper": checkout / "sub" / "deeper",
            "nested-git": checkout / ".worktrees" / "inner" / ".git",
            "sub-git": checkout / "sub" / ".git",
        }[where]
        inner.mkdir(parents=True, exist_ok=True)
        if spelling != ".agenttalk":
            (inner / spelling).mkdir()  # the store folder spelled in another case, made before the store is
        Store(inner).init(["lead"])
        kept = inner / ".agenttalk" / "independent-data.txt"
        kept.write_text("independent store data\n", encoding="utf-8")
    capsys.readouterr()

    cli.main(["--root", str(store.root), *argv])

    shown = capsys.readouterr()
    if where == "none":
        assert not checkout.exists()  # the control: an ordinary own checkout is still removed
    else:
        assert kept.read_text(encoding="utf-8") == "independent store data\n"
        _assert_untouched(checkout)
        said = shown.err + shown.out
        if command == "deliver":  # the delivery cleanup keeps its reason in the row (`lane status` shows it)
            said = lanes.load_lanes(store)["lanes"][selected].get("worktree_cleanup_error", "")
        assert "holds a store of its own" in said


@pytest.mark.parametrize("spelling", [".agenttalk", ".AGENTTALK", ".AgentTalk"])
def test_the_store_folder_name_is_recognised_in_any_case_by_both_walks(tmp_path, spelling):
    """The walk down into a checkout and the walk up between a checkout and its store use one name test."""
    inside = tmp_path / "checkout"
    (inside / "sub" / spelling).mkdir(parents=True)
    assert cli._lane_checkout_holds_a_store(str(inside)) == str(inside / "sub" / spelling)
    (tmp_path / "folder-with-a-file").mkdir()
    (tmp_path / "folder-with-a-file" / spelling).write_text("a file is not a store folder", encoding="utf-8")
    assert not cli._lane_dir_has_store_folder(str(tmp_path / "folder-with-a-file"))
    # a store of its own between this store's folder and a checkout, whatever its spelling
    (tmp_path / "mid" / spelling).mkdir(parents=True)
    (tmp_path / "mid" / "made-checkout").mkdir()
    assert cli._lane_dir_has_store_folder(str(tmp_path / "mid"))
    assert cli._lane_checkout_outside_store(Store(tmp_path), str(tmp_path / "mid" / "made-checkout")) is not None
    assert cli._lane_checkout_outside_store(Store(tmp_path), str(tmp_path / "checkout")) is None  # the control


# ---------------------------------------------------------------------------------------------------------------------
# Round 26 (zteam's Windows probes at afa826a4): a directory link inside a checkout can lead out of it, and a removal
# can follow it. Git 2.39.1 on Windows follows a junction while removing a worktree and deletes what it points at; an
# ignored junction is invisible to `git status`. The rollback of a failed `lane assign` ends in the same removal.
# ---------------------------------------------------------------------------------------------------------------------


def _checkout_and_command(tmp_path, monkeypatch, command):
    """A lane's own, clean checkout and the command that removes it: (store, selected, checkout, argv)."""
    from test_lanes import _assign_worktree, _repo

    if command == "deliver":
        store, selected, checkout, _head, _artifact = _published_with_cleanup_postponed(tmp_path, monkeypatch)
        return store, selected, checkout, ["lane", "deliver", "--id", selected, "--from", "dev"]
    root, base = _repo(tmp_path)
    store, selected = Store(root), "first"
    checkout = Path(_assign_worktree(root, base, selected)["worktree_path"])
    if command == "abandon":
        return store, selected, checkout, ["lane", "abandon", "--id", selected]
    _update_row(store, selected, status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_ABANDONED)
    return store, selected, checkout, ["lane", "gc", "--delete", "--target", "HEAD", "--json"]


@pytest.mark.parametrize("command", ["abandon", "deliver", "gc"])
@pytest.mark.parametrize(
    "kind",
    [
        "store-named-link",
        "store-named-link-other-case",
        pytest.param("link-out", marks=pytest.mark.skipif(os.name != "nt", reason="a followed link is a Windows case")),
        pytest.param(
            "link-inside", marks=pytest.mark.skipif(os.name != "nt", reason="a followed link is a Windows case")
        ),
        "none",
    ],
)
def test_a_removal_never_follows_a_directory_link_out_of_the_checkout(tmp_path, monkeypatch, capsys, command, kind):
    """A link or junction named like a store folder, and on Windows any directory link that leads out of the checkout,
    stops the removal (the checkout and what it leads to stay). The controls: a link that leads to a folder inside the
    checkout, and no link at all, are removed as before."""
    store, selected, checkout, argv = _checkout_and_command(tmp_path, monkeypatch, command)
    independent = tmp_path.parent / f"{tmp_path.name}-independent"
    Store(independent).init(["lead"])  # a real, separate store
    sentinel = independent / ".agenttalk" / "independent-data.txt"
    sentinel.write_text("independent store data\n", encoding="utf-8")
    (independent / "shared-data.txt").write_text("shared data\n", encoding="utf-8")
    if kind.startswith("store-named-link"):
        name = ".agenttalk" if kind == "store-named-link" else ".AgentTalk"
        _link_directory(checkout / name, independent / ".agenttalk")
    elif kind == "link-out":
        (checkout / ".worktrees").mkdir()  # a folder the repository's .gitignore ignores
        _link_directory(checkout / ".worktrees" / "out", independent)
    elif kind == "link-inside":
        (checkout / ".worktrees").mkdir()
        _link_directory(checkout / ".worktrees" / "self", checkout / "src")
    capsys.readouterr()

    cli.main(["--root", str(store.root), *argv])

    shown = capsys.readouterr()
    assert sentinel.read_text(encoding="utf-8") == "independent store data\n"
    assert (independent / "shared-data.txt").read_text(encoding="utf-8") == "shared data\n"
    said = shown.err + shown.out
    if command == "deliver":  # the delivery cleanup keeps its reason in the row (`lane status` shows it)
        said = lanes.load_lanes(store)["lanes"][selected].get("worktree_cleanup_error", "") or said
    if kind in ("none", "link-inside"):
        assert not (checkout / ".git").exists()  # removed, as before
        assert "directory link" not in said
    else:
        _assert_untouched(checkout)
        assert "holds a directory link" in said


def test_the_rollback_of_a_failed_assign_never_deletes_a_store_a_hook_made_in_the_new_checkout(
    tmp_path, monkeypatch, capsys
):
    """A post-checkout hook that makes a store in the new checkout and then fails: the rollback removes the checkout it
    made, and used to take the hook's store with it (Git ignores the store, so no clean check sees it)."""
    import sys

    from test_lanes import _branch, _repo

    root, base = _repo(tmp_path)
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).as_posix()
    hook.write_text(
        "#!/bin/sh\n"
        f'"{python}" -c "import sys; from pathlib import Path; from agenttalk.store import Store; '
        "s = Store(Path.cwd()); s.init(['lead']); (s.dir / 'independent-data.txt').write_text('data'); "
        'sys.exit(1)"\n',
        encoding="utf-8", newline="\n",
    )
    hook.chmod(0o755)
    capsys.readouterr()

    rc = cli.main([
        "--root", str(root), "lane", "assign", "--id", "hooked", "--from", "lead", "--assignee", "dev",
        "--domain", "core", "--base", base, "--target", _branch(root), "--path", "src/core",
        "--worktrees-root", str(root / ".worktrees"),
    ])

    shown = capsys.readouterr()
    assert rc == 2
    made = list((root / ".worktrees").glob("hooked-*/.agenttalk/independent-data.txt"))
    assert made, f"the store the hook made was deleted by the rollback: {shown.err!r}"
    assert made[0].read_text(encoding="utf-8") == "data"


def test_only_the_checkouts_own_git_entry_is_skipped_by_the_scan(tmp_path):
    """The root's `.git` (a file in a linked checkout) is Git's own metadata and is skipped; a `.git` folder anywhere
    else is walked like any folder, and a store in it is found."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert cli._lane_checkout_hazard(str(checkout)) is None
    (checkout / "sub" / ".git" / ".agenttalk").mkdir(parents=True)
    assert cli._lane_checkout_hazard(str(checkout)) == ("store", str(checkout / "sub" / ".git" / ".agenttalk"))
    other = tmp_path / "other"
    (other / ".git" / ".agenttalk").mkdir(parents=True)  # the root's own `.git` is never entered
    assert cli._lane_checkout_hazard(str(other)) is None
    (other / "deeper" / ".worktrees" / "inner" / ".git" / ".agenttalk").mkdir(parents=True)
    assert cli._lane_checkout_hazard(str(other)) is not None


# ---------------------------------------------------------------------------------------------------------------------
# Round 28 (zteam's read of d2b7d908): `os.path.realpath` without strict=True swallows some failures while following a
# junction (access denied, a sharing violation) and returns a partial path that still starts where the link is, so the
# "does the target lie inside the checkout?" check passed for a link it could not follow. A path that cannot be
# resolved completely is not proven inside; it is refused.
# ---------------------------------------------------------------------------------------------------------------------


def _realpath_cannot_follow(monkeypatch, link: Path, error: OSError) -> list[str]:
    """Make resolving `link` fail the way Windows does: the plain call hands back the unresolved path, the strict one
    raises `error`. Everything else resolves as usual. Returns the list of times the fault fired."""
    real = os.path.realpath
    wanted = os.path.normcase(os.path.normpath(str(link)))
    fired: list[str] = []

    def realpath(path, *args, strict=False, **kwargs):
        if os.path.normcase(os.path.normpath(os.fspath(path))) == wanted:
            fired.append("strict" if strict else "plain")
            if strict:
                raise error
            return os.fspath(path)
        return real(path, *args, strict=strict, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    return fired


@pytest.mark.skipif(os.name != "nt", reason="a followed junction is a Windows case")
@pytest.mark.parametrize("command", ["abandon", "deliver", "gc"])
@pytest.mark.parametrize("error", [PermissionError(5, "access denied"), OSError(32, "sharing violation")],
                         ids=["access-denied", "sharing-violation"])
def test_a_junction_that_cannot_be_resolved_is_refused_not_trusted(tmp_path, monkeypatch, capsys, command, error):
    store, selected, checkout, argv = _checkout_and_command(tmp_path, monkeypatch, command)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("outside data\n", encoding="utf-8")
    (checkout / ".worktrees").mkdir()  # a folder the repository's .gitignore ignores
    link = checkout / ".worktrees" / "out"
    _link_directory(link, outside)
    fired = _realpath_cannot_follow(monkeypatch, link, error)
    capsys.readouterr()

    cli.main(["--root", str(store.root), *argv])

    shown = capsys.readouterr()
    assert fired, "the injected failure never fired"
    assert sentinel.read_text(encoding="utf-8") == "outside data\n"
    _assert_untouched(checkout)
    said = shown.err + shown.out
    if command == "deliver":
        said = lanes.load_lanes(store)["lanes"][selected].get("worktree_cleanup_error", "") or said
    assert "holds a directory link" in said


@pytest.mark.skipif(os.name != "nt", reason="a followed junction is a Windows case")
def test_a_folder_that_cannot_be_resolved_is_not_proven_inside_the_store(tmp_path, monkeypatch):
    """The other place the same check is made: whether a checkout lies inside this store's folder."""
    store_root = tmp_path / "store"
    (store_root / ".worktrees").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = store_root / ".worktrees" / "via-link"
    _link_directory(link, outside)
    assert cli._lane_checkout_outside_store(Store(store_root), str(link)) is not None  # resolves out: not inside
    fired = _realpath_cannot_follow(monkeypatch, link, PermissionError(5, "access denied"))

    assert cli._lane_checkout_outside_store(Store(store_root), str(link)) is not None  # cannot resolve: not proven
    assert fired


def test_a_checkout_that_resolves_completely_is_still_inside_the_store(tmp_path):
    """The control: strict resolution changes nothing for an ordinary folder."""
    store_root = tmp_path / "store"
    (store_root / ".worktrees" / "x").mkdir(parents=True)
    assert cli._lane_checkout_outside_store(Store(store_root), str(store_root / ".worktrees" / "x")) is None
    assert cli._lane_checkout_hazard(str(store_root / ".worktrees" / "x")) is None


@pytest.mark.parametrize("command", ["abandon"])
def test_another_lanes_path_that_cannot_be_resolved_blocks_a_removal(tmp_path, monkeypatch, capsys, command):
    """The owner scan decides by resolved paths: another lane whose saved path cannot be resolved (here a link to the
    checkout, with the resolution and the file-identity comparison both failing) could be the owner, so it blocks."""
    from test_lanes import _assign_worktree, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    store = Store(root)
    assert _git_rc(first, "checkout", "--detach", base).returncode == 0
    assert _git_rc(other, "checkout", "--detach", base).returncode == 0
    _point_lane_at(store, "first", other)
    via = tmp_path / "via-link"
    _link_directory(via, other)
    _update_row(store, "other", worktree_path=str(via), worktree_toplevel_canonical=None)
    fired = _realpath_cannot_follow(monkeypatch, via, PermissionError(5, "access denied"))
    real_samefile = os.path.samefile

    def samefile(a, b):
        if os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(via))):
            raise PermissionError(5, "access denied")
        return real_samefile(a, b)

    monkeypatch.setattr(os.path, "samefile", samefile)
    capsys.readouterr()

    cli.main(["--root", str(root), "lane", "abandon", "--id", "first"])

    assert "strict" in fired
    _assert_untouched(first, other)


def test_a_checkout_that_is_not_there_is_judged_by_where_it_would_be(tmp_path):
    """A registered checkout whose folder was deleted is judged by its nearest existing ancestor, resolved strictly,
    plus the missing names; nothing that is not there can be a link."""
    store_root = tmp_path / "store"
    (store_root / ".worktrees").mkdir(parents=True)
    assert cli._lane_checkout_outside_store(Store(store_root), str(store_root / ".worktrees" / "gone-x")) is None
    assert cli._lane_checkout_outside_store(Store(store_root), str(tmp_path / "elsewhere" / "gone-x")) is not None


# ---------------------------------------------------------------------------------------------------------------------
# Round 30 (codex-rev's read of round 29): a checkout nobody could look at must never be what a delivery publishes.
# ---------------------------------------------------------------------------------------------------------------------


def _other_lanes_checkout_claimed_by_first(tmp_path):
    """Two lanes, work committed in the other lane's checkout, both checkouts detached at that commit, the lane branch
    of `first` moved to it (so its tip matches), and the row of `first` pointed at the OTHER checkout. Returns
    (root, store, first_checkout, other_checkout)."""
    from test_lanes import _assign_worktree, _commit, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    _commit(other, "src/core/a.py", "base\nanother lane work\n")
    head = _git_rc(other, "rev-parse", "HEAD").stdout.strip()
    assert _git_rc(first, "checkout", "--detach", base).returncode == 0
    assert _git_rc(other, "checkout", "--detach", head).returncode == 0
    assert _git_rc(root, "branch", "-f", "lane/first", head).returncode == 0
    store = Store(root)
    _point_lane_at(store, "first", other)
    return root, store, first, other


def _nothing_was_published(store: Store, lane_id: str) -> None:
    deliveries = store.dir / lanes.DELIVERIES_DIRNAME
    left = [p.name for p in deliveries.rglob("*") if p.is_file() and not p.name.startswith(".")]
    assert not left, left        # dot-files are bookkeeping that any attempt leaves; no evidence file may be left
    row = lanes.load_lanes(store)["lanes"][lane_id]
    assert row["status"] == lanes.STATUS_ACTIVE and "publish_pending" not in row and "delivery_artifact" not in row


def test_delivery_never_publishes_another_lanes_checkout_it_could_not_examine(tmp_path, capsys, monkeypatch):
    """codex-rev's second probe: the checkout the row of `first` names is the other lane's, and looking at it fails.
    Nothing is published; the other lane's checkout is untouched."""
    from test_lanes import _checkout_cannot_be_resolved

    root, store, _first, other = _other_lanes_checkout_claimed_by_first(tmp_path)
    fired = _checkout_cannot_be_resolved(monkeypatch, other)
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "first", "--from", "dev"])

    err = capsys.readouterr().err
    assert fired and rc == 3 and "could not be examined" in err
    _nothing_was_published(store, "first")
    _assert_untouched(other)


def test_delivery_is_refused_when_the_scan_for_another_lanes_claim_cannot_be_made(tmp_path, capsys, monkeypatch):
    """The same set-up, but only the other-owner scan fails (the checkout itself can be examined and is inside the
    store): an error in that scan is not set aside for a delivery. Without it nothing else in the set-up stops the
    delivery."""
    root, store, _first, other = _other_lanes_checkout_claimed_by_first(tmp_path)
    scans: list[str] = []

    def scan_fails(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        scans.append("scan")
        raise PermissionError(13, "injected: a saved path of another lane cannot be read")

    monkeypatch.setattr(cli, "_lane_checkout_owned_by_another", scan_fails)
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "first", "--from", "dev"])

    err = capsys.readouterr().err
    assert scans and rc == 3 and "whether another lane owns this checkout could not be established" in err
    _nothing_was_published(store, "first")
    _assert_untouched(other)


def test_a_terminal_recheck_with_no_earlier_proof_from_this_command_refuses(tmp_path, capsys, monkeypatch):
    """What a recovery run by another command looks like: nothing in this command proved the checkout is the lane's, and
    from the final steps on it cannot be examined. The delivery is rolled back, not published."""
    from test_lanes import _assign_worktree, _checkout_cannot_be_resolved, _commit, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "mine")["worktree_path"])
    _commit(mine, "src/core/a.py", "base\nmine\n")
    monkeypatch.setattr(cli, "_lane_record_proof", lambda *args, **kwargs: None)   # no proof was made in this command
    fired = _checkout_cannot_be_resolved(monkeypatch, mine, only_during="_lane_finalize_delivery")
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "mine", "--from", "dev"])

    err = capsys.readouterr().err
    assert fired and rc == 3 and "nothing earlier in this delivery proved" in err
    _nothing_was_published(Store(root), "mine")
    _assert_untouched(mine)


def test_a_checkout_swapped_in_after_the_fresh_evaluation_is_not_published(tmp_path, capsys, monkeypatch):
    """The folder of `first` is replaced, between the fresh evaluation and the terminal recheck, by a link to the other
    lane's checkout (same commit, both detached), and from then on `first`'s folder cannot be resolved. The recheck
    sees another checkout by Git's own account, so nothing is published."""
    from test_lanes import _assign_worktree, _checkout_cannot_be_resolved, _commit, _git_rc, _repo

    root, base = _repo(tmp_path)
    first = Path(_assign_worktree(root, base, "first")["worktree_path"])
    other = _assign_second_lane(root, base)
    _commit(first, "src/core/a.py", "base\nfirst\n")
    head = _git_rc(first, "rev-parse", "HEAD").stdout.strip()
    assert _git_rc(first, "checkout", "--detach", head).returncode == 0
    assert _git_rc(other, "checkout", "--detach", head).returncode == 0
    store = Store(root)
    real_prepare = cli._lane_prepare_publication

    def swap_then_prepare(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        first.rename(first.with_name(first.name + "-away"))
        _link_directory(first, other)
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(cli, "_lane_prepare_publication", swap_then_prepare)
    fired = _checkout_cannot_be_resolved(monkeypatch, first, only_during="_lane_prepare_publication")
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "first", "--from", "dev"])

    assert fired and rc == 3, capsys.readouterr().err
    _nothing_was_published(store, "first")
    _assert_untouched(other)


def test_the_proof_made_by_the_fresh_evaluation_holds_only_for_the_same_checkout(tmp_path):
    """The proof is bound to the lane instance, the folder, what Git says the checkout is (toplevel and its own
    administrative folder) and every other lane's row. Anything else does not match."""
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    mine = _assign_worktree(root, base, "mine")
    other = _assign_second_lane(root, base)
    store = Store(root)
    folder = mine["worktree_path"]
    top = lanes.canonical_host_path(folder)
    git_dir = cli._git_dir(folder)
    assert not cli._lane_proof_holds(store, "mine", mine, folder, top, git_dir)             # nothing recorded yet
    cli._lane_record_proof(store, "mine", mine, folder, top, git_dir, lanes.load_lanes(store).get("lanes") or {})

    assert cli._lane_proof_holds(store, "mine", mine, folder, top, git_dir)
    assert not cli._lane_proof_holds(store, "mine", mine, folder, top, cli._git_dir(other))   # another admin folder
    assert not cli._lane_proof_holds(store, "mine", mine, str(other), top, git_dir)           # another folder
    assert not cli._lane_proof_holds(store, "mine", mine, folder, lanes.canonical_host_path(other), git_dir)
    assert not cli._lane_proof_holds(store, "mine", dict(mine, generation=99), folder, top, git_dir)
    _update_row(store, "other", notes="changed")                                              # another lane's row
    assert not cli._lane_proof_holds(store, "mine", mine, folder, top, git_dir)


# ---------------------------------------------------------------------------------------------------------------------
# Round 31 (zteam's read of round 30): the proof vouches for exactly the rows that were scanned, and Git's own record
# of the checkout names its folder.
# ---------------------------------------------------------------------------------------------------------------------


def _retired_claim_on(store: Store, lane_id: str, checkout: Path) -> None:
    """A retired, cleanup-pending row of another lane that claims `checkout`."""
    data = lanes.load_lanes(store)
    row = dict(data["lanes"][lane_id])
    for pending in ("publish_pending", "prepared_artifact", "delivery_transaction_id", "terminal_hold"):
        row.pop(pending, None)
    # a delivered, finished lane whose checkout cleanup is still pending: it reserves no paths, so only the scan for
    # another lane's claim can see that it names the checkout
    row.update(lane_id="ghost", status=lanes.STATUS_DELIVERED, publish_pending=False, instance_id="2" * 32,
               cleanup_pending=True, worktree_path=str(checkout),
               worktree_toplevel_canonical=lanes.canonical_host_path(checkout))
    data["lanes"]["ghost"] = row
    lanes.save_lanes(store, data)


def _delivery_with_a_claim_inserted(tmp_path, monkeypatch, capsys, *, when: str):
    """A delivery of `mine`, a retired claim on its checkout inserted `when` ("between-scan-and-proof": right after the
    fresh evaluation's last scan; "after-the-proof": before the terminal steps), and looking at the checkout failing
    from the final steps on. Returns (rc, stderr, root, checkout)."""
    from test_lanes import _assign_worktree, _checkout_cannot_be_resolved, _commit, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "mine")["worktree_path"])
    _commit(mine, "src/core/a.py", "base\nmine\n")
    store = Store(root)
    if when == "between-scan-and-proof":
        real_scan, calls = cli._lane_checkout_owned_by_another, []

        def scan_then_insert(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            found = real_scan(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:                         # the fresh evaluation scans twice; this is after the last
                _retired_claim_on(store, "mine", mine)
            return found

        monkeypatch.setattr(cli, "_lane_checkout_owned_by_another", scan_then_insert)
    else:
        real_prepare = cli._lane_prepare_publication

        def insert_then_prepare(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            _retired_claim_on(store, "mine", mine)
            return real_prepare(*args, **kwargs)

        monkeypatch.setattr(cli, "_lane_prepare_publication", insert_then_prepare)
    for step in ("_lane_prepare_publication", "_lane_finalize_delivery"):   # every terminal step looks at a dead folder
        _checkout_cannot_be_resolved(monkeypatch, mine, only_during=step)
    capsys.readouterr()
    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "mine", "--from", "dev"])
    return rc, capsys.readouterr().err, root, mine


@pytest.mark.parametrize("when", ["between-scan-and-proof", "after-the-proof"])
def test_a_claim_inserted_after_the_scan_is_never_vouched_for(tmp_path, monkeypatch, capsys, when) -> None:
    """zteam's P2 and its control. A retired claim on the checkout that was not scanned must not end up inside the
    proof: when looking at the checkout then fails at the final steps, nothing is published, as it is when the same row
    comes in after the proof was made."""
    rc, err, root, mine = _delivery_with_a_claim_inserted(tmp_path, monkeypatch, capsys, when=when)

    assert rc == 3 and "nothing earlier in this delivery proved" in err, err
    _nothing_was_published(Store(root), "mine")
    _assert_untouched(mine)


def test_the_proof_is_made_from_the_rows_that_were_scanned() -> None:
    """The proof is given the scanned rows and does not read the registry itself: the fingerprint it keeps is that of
    the snapshot, whatever the saved registry says by then."""
    import inspect

    assert "rows" in inspect.signature(cli._lane_record_proof).parameters
    scanned = {"mine": {"lane_id": "mine"}, "other": {"lane_id": "other"}}
    store = types.SimpleNamespace(__dict__={})
    key, facts = cli._lane_proof_identity(store, "mine", {"instance_id": "i", "generation": 1}, "f", "t", "g", scanned)
    assert facts["other_rows"] == lanes.fingerprint({"other": {"lane_id": "other"}})


def test_a_checkout_moved_with_git_worktree_move_is_still_delivered(tmp_path, capsys) -> None:
    """What the registration check costs a legitimate checkout: nothing when Git itself moved it (it rewrites the
    record), as long as the lane's row names the new folder."""
    from test_lanes import _assign_worktree, _commit, _git_rc, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "mv")["worktree_path"])
    _commit(mine, "src/core/a.py", "base\nmoved\n")
    moved = mine.with_name("moved-" + mine.name)
    assert _git_rc(root, "worktree", "move", str(mine), str(moved)).returncode == 0
    _point_lane_at(Store(root), "mv", moved)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "deliver", "--id", "mv", "--from", "dev"]) == 0
    assert lanes.load_lanes(Store(root))["lanes"]["mv"]["status"] == lanes.STATUS_DELIVERED


def _printed_repair_step(err: str, repository: Path) -> tuple[list[str], Path]:
    """The repair step a refusal prints, read back from the message the way a person reads it: the command between
    the backticks, and the folder it is to be run in (the moved checkout the message names, or, when it names none,
    the repository). Nothing here knows the path the step needs."""
    step = re.search(r"`(git worktree repair[^`]*)`", err)
    assert step, err
    named = re.search(r"open the moved folder (.+?) and run `git worktree repair`", err)
    if named:
        return step.group(1).split(), Path(named.group(1))
    return step.group(1).split(), repository  # no other folder is named: the step is run where the repository is


def test_a_checkout_renamed_by_hand_is_refused_until_git_worktree_repair(tmp_path, capsys) -> None:
    """And what it costs one moved without Git: Git's record still names the old folder, so the delivery is refused
    with the way out; the step the refusal PRINTS, run as printed where it says, repairs the record and the delivery
    then goes through (zteam's P2: the earlier text said to run it from the repository, which repairs nothing
    without the checkout's path)."""
    import subprocess

    from test_lanes import _assign_worktree, _commit, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "hand")["worktree_path"])
    _commit(mine, "src/core/a.py", "base\nrenamed\n")
    renamed = mine.with_name("renamed-" + mine.name)
    mine.rename(renamed)
    _point_lane_at(Store(root), "hand", renamed)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "deliver", "--id", "hand", "--from", "dev"]) == 3
    err = capsys.readouterr().err
    assert "git worktree repair" in err and "Git's own record" in err
    _nothing_was_published(Store(root), "hand")

    argv, folder = _printed_repair_step(err, root)
    assert subprocess.run(argv, cwd=folder, capture_output=True, text=True, encoding="utf-8").returncode == 0
    capsys.readouterr()
    assert cli.main(["--root", str(root), "lane", "deliver", "--id", "hand", "--from", "dev"]) == 0, err
    assert argv == ["git", "worktree", "repair"] and folder == renamed, (argv, folder, err)


def test_the_printed_repair_step_is_a_plain_command_with_no_path_to_quote(tmp_path, capsys) -> None:
    """The step names no folder in the command, so a checkout whose path holds spaces or quotes needs no quoting: the
    folder is only shown, once, in the sentence that says where to run it."""
    import subprocess

    from test_lanes import _assign_worktree, _commit, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "odd")["worktree_path"])
    _commit(mine, "src/core/a.py", "base\nodd\n")
    renamed = mine.with_name("it's a renamed " + mine.name)
    mine.rename(renamed)
    _point_lane_at(Store(root), "odd", renamed)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "deliver", "--id", "odd", "--from", "dev"]) == 3
    err = capsys.readouterr().err
    argv, folder = _printed_repair_step(err, root)
    assert subprocess.run(argv, cwd=folder, capture_output=True, text=True, encoding="utf-8").returncode == 0
    capsys.readouterr()
    assert cli.main(["--root", str(root), "lane", "deliver", "--id", "odd", "--from", "dev"]) == 0, err
    assert argv == ["git", "worktree", "repair"] and folder == renamed, (argv, folder, err)


def test_the_repair_step_warns_that_a_copy_with_the_old_folder_still_there_is_not_to_be_repaired(
        tmp_path, capsys) -> None:
    """From inside a COPY the repair would take Git's record away from the original: the message says so."""
    moved = str(tmp_path / "new")
    text = cli._lane_checkout_registration_text(moved, ("mismatch", str(tmp_path / "old")))
    assert "if the old folder is still there" in text and "take Git's record away from the original" in text, text
    assert f"open the moved folder {moved} and run `git worktree repair` there" in text, text
    assert "from the repository" not in text, text
    unknown = cli._lane_checkout_registration_text(moved, ("unknown", "no such file"))
    assert f"open the moved folder {moved} and run `git worktree repair` there" in unknown, unknown
    assert "from the repository" not in unknown, unknown


def test_the_could_not_be_examined_text_carries_the_old_folder_condition_and_warning(tmp_path) -> None:
    """Round 31d (codex-rev): "could not be examined" does not prove the original folder is gone, so the repair is
    offered only if the old folder is really gone, with the warning for when it still exists or cannot be checked."""
    moved = str(tmp_path / "new")
    unknown = cli._lane_checkout_registration_text(moved, ("unknown", "PermissionError: access denied"))
    assert "and the old folder is really gone" in unknown, unknown
    assert ("if the old folder still exists or cannot be checked, leave this one alone: the repair would take "
            "Git's record away from the original") in unknown, unknown


def test_a_copy_whose_original_cannot_be_examined_is_refused_with_the_warning(tmp_path, monkeypatch, capsys) -> None:
    """codex-rev's case: the original folder is there but looking at it fails (PermissionError). The copy is refused,
    and the message does not offer the repair without the warning that it would take the original's record."""
    import shutil

    from test_lanes import _assign_worktree, _commit, _repo

    root, base = _repo(tmp_path)
    original = Path(_assign_worktree(root, base, "cp")["worktree_path"])
    _commit(original, "src/core/a.py", "base\ncopy\n")
    copy = original.with_name("copy-of-" + original.name)
    shutil.copytree(original, copy)
    _point_lane_at(Store(root), "cp", copy)
    real = cli._strict_real

    def unreadable_original(path, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(original)):
            raise PermissionError(13, "Access is denied", str(original))
        return real(path, **kwargs)

    monkeypatch.setattr(cli, "_strict_real", unreadable_original)
    capsys.readouterr()

    assert cli.main(["--root", str(root), "lane", "deliver", "--id", "cp", "--from", "dev"]) == 3
    err = capsys.readouterr().err
    assert "could not be examined" in err, err
    assert "if the old folder still exists or cannot be checked, leave this one alone" in err, err
    assert "take Git's record away from the original" in err, err
    _nothing_was_published(Store(root), "cp")
    _assert_untouched(original, copy)


def test_the_text_for_a_linked_git_entry_gives_a_step_that_works_as_printed() -> None:
    """Rounds 31d and 31e: a `.git` entry that is a link is replaced by this checkout's real `.git` file. No repair
    step is printed (`git worktree repair` from the repository without a path mends nothing), and no advice to remove
    the folder or assign the lane again: this refusal comes before the work checks, so the folder may hold the only
    copy of uncommitted or untracked work, and removing a folder does not remove the lane's row."""
    text = cli._lane_checkout_registration_text("C:\\x\\wt", ("link", "C:\\x\\wt\\.git"))
    assert ("Replace the link with this checkout's real `.git` file and try again; if you cannot, copy any work in "
            "this folder somewhere else first and ask for help: do not delete this folder") in text, text
    advice = text.lower().replace("do not delete this folder", "")
    for forbidden in ("remove", "delete", "assign", "repair", "from the repository", "discard", "rm "):
        assert forbidden not in advice, (forbidden, text)



# ---------------------------------------------------------------------------------------------------------------------
# Round 31b (codex-rev's P2): `os.path.realpath` keeps the Windows extended-path prefix when it was given one, so
# Git's record of a checkout, written with the prefix, compared unequal with the checkout's ordinary spelling and a
# legitimate delivery was refused. The prefix is removed from every `_strict_real` answer where it is the same folder.
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written, same",
    [
        ("\\\\?\\C:\\x\\y", "C:\\x\\y"),
        ("//?/c:/x/y", "c:/x/y"),
        ("\\\\?\\C:", "C:"),
        ("\\\\?\\c:\\", "c:\\"),
        ("\\\\?\\UNC\\srv\\share\\x", "\\\\srv\\share\\x"),
        ("\\\\?\\UNC\\srv\\share", "\\\\srv\\share"),
        ("//?/UNC/srv/share/x", "\\\\srv/share/x"),
    ],
)
def test_the_extended_prefix_is_removed_where_it_names_the_same_folder(written, same) -> None:
    assert cli._without_extended_prefix(written, windows=True) == same


@pytest.mark.parametrize(
    "written",
    ["\\\\?\\Volume{1234}\\x", "\\\\.\\pipe\\x", "\\\\?\\GLOBALROOT\\x", "\\\\?\\UNC\\\\x", "\\\\?\\C", "C:\\x",
     "\\\\srv\\share\\x", "\\x"],
)
def test_any_other_spelling_is_left_as_it_is(written) -> None:
    """A volume or device spelling stays different from every ordinary spelling: the safe side."""
    assert cli._without_extended_prefix(written, windows=True) == written


def test_nothing_is_changed_on_a_system_that_has_no_such_prefix() -> None:
    assert cli._without_extended_prefix("\\\\?\\C:\\x", windows=False) == "\\\\?\\C:\\x"


windows_only = pytest.mark.skipif(os.name != "nt", reason="the extended-path prefix is a Windows spelling")


@windows_only
def test_one_real_folder_written_with_and_without_the_prefix_compares_equal(tmp_path) -> None:
    folder = tmp_path / "there"
    folder.mkdir()
    ordinary = cli._strict_real(str(folder))
    assert cli._strict_real("\\\\?\\" + str(folder)) == ordinary
    assert cli._strict_real("//?/" + str(folder).replace("\\", "/")) == ordinary
    assert not ordinary.startswith("\\\\?\\")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert cli._strict_real("\\\\?\\" + str(other)) != ordinary          # a different folder stays different


def _record_the_checkout_as(checkout: Path, text: str) -> None:
    """Rewrite Git's record of where the checkout lives (the administrative folder's `gitdir` file)."""
    (Path(cli._git_dir(str(checkout))) / "gitdir").write_text(text + "\n", encoding="utf-8")


@windows_only
def test_a_record_spelled_with_the_extended_prefix_still_names_the_checkout(tmp_path) -> None:
    from test_lanes import _assign_worktree, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "mine")["worktree_path"])
    other = _assign_second_lane(root, base)
    admin = cli._git_dir(str(mine))
    assert cli._lane_checkout_registration(str(mine), admin) is None
    _record_the_checkout_as(mine, "\\\\?\\" + str(mine / ".git"))
    assert cli._lane_checkout_registration(str(mine), admin) is None           # the same file, spelled with the prefix
    _record_the_checkout_as(mine, "\\\\?\\" + str(other / ".git"))
    assert cli._lane_checkout_registration(str(mine), admin)[0] == "mismatch"    # a really different folder


@windows_only
@pytest.mark.parametrize("spelling", ["extended-drive", "ordinary"])
def test_a_delivery_is_not_refused_for_the_way_git_spelled_its_record(tmp_path, capsys, spelling) -> None:
    from test_lanes import _assign_worktree, _commit, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "mine")["worktree_path"])
    _commit(mine, "src/core/a.py", "base\nmine\n")
    if spelling == "extended-drive":
        _record_the_checkout_as(mine, "\\\\?\\" + str(mine / ".git"))
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "mine", "--from", "dev"])

    assert rc == 0, capsys.readouterr().err
    assert lanes.load_lanes(Store(root))["lanes"]["mine"]["status"] == lanes.STATUS_DELIVERED


@windows_only
def test_a_delivery_is_still_refused_when_the_prefixed_record_names_another_folder(tmp_path, capsys) -> None:
    from test_lanes import _assign_worktree, _commit, _repo

    root, base = _repo(tmp_path)
    mine = Path(_assign_worktree(root, base, "mine")["worktree_path"])
    other = _assign_second_lane(root, base)
    _commit(mine, "src/core/a.py", "base\nmine\n")
    _record_the_checkout_as(mine, "\\\\?\\" + str(other / ".git"))
    capsys.readouterr()

    rc = cli.main(["--root", str(root), "lane", "deliver", "--id", "mine", "--from", "dev"])

    assert rc == 3 and "Git's own record of this checkout says it lives at" in capsys.readouterr().err
    _nothing_was_published(Store(root), "mine")


# ---------------------------------------------------------------------------------------------------------------------
# #843: the prepared copy is deleted through the folders that were checked, not through their names again. The removal
# checked that `lane-deliveries` and `.prepared` were real folders and then deleted by name, so another program of the
# same user could swap `.prepared` for a link in between and the delete followed it. The seams below make the swap
# happen at an exact step instead of relying on timing.


def _hook_unlink(monkeypatch, wrapper):
    """Replace `os.unlink` by `wrapper`, which passes everything on (`dir_fd` included). The held-folder delete is only
    made when `os.unlink` is listed in `os.supports_dir_fd`, a set of the original function objects; a plain wrapper is
    not in it, the delete was refused before it was reached, and the seam never ran (Linux, Python 3.10 and 3.11, in
    CI). So the wrapper is listed too: it does take `dir_fd`."""
    monkeypatch.setattr(os, "unlink", wrapper)
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {wrapper})


def _prepared_copy_with_a_swap_ready(tmp_path, monkeypatch, seam):
    """A committed delivery whose own prepared copy exists, an unrelated file with the same name in another folder,
    and a `swap()` that moves the real `.prepared` folder aside and puts a link to the other folder in its place at
    `seam`: "after-the-last-folder-check" (right after the check of the `.prepared` folder says it is a real folder) or
    "inside-the-delete-call" (just before the real delete runs, whether by `os.unlink` or `Path.unlink`)."""
    store, lane_id, _checkout, _head, artifact = _committed_unpublished_delivery(tmp_path, monkeypatch)
    pending = lanes.load_lanes(store)["lanes"][lane_id]["publish_pending"]
    own = lanes.prepared_delivery_artifact_path(store, pending["lane_id"], pending["transaction_id"])
    assert own.exists()
    independent = tmp_path.parent / f"{tmp_path.name}-independent-843"
    independent.mkdir()
    victim = independent / own.name
    victim.write_text("not part of any delivery\n", encoding="utf-8")
    moved = own.parent.with_name(".prepared-original")
    state = {"swapped": False, "refused": None, "calls": 0}

    def swap():
        state["calls"] += 1
        if state["swapped"] or state["refused"] is not None:
            return
        try:
            own.parent.rename(moved)
        except OSError as exc:  # a held folder cannot be renamed on Windows
            state["refused"] = exc
            return
        _link_directory(own.parent, independent)
        state["swapped"] = True

    if seam == "inside-the-delete-call":
        real_unlink, real_path_unlink = os.unlink, Path.unlink

        def unlink(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == own.name:
                swap()
            return real_unlink(path, *args, **kwargs)

        def path_unlink(self, *args, **kwargs):
            if self.name == own.name:
                swap()
            return real_path_unlink(self, *args, **kwargs)

        _hook_unlink(monkeypatch, unlink)
        monkeypatch.setattr(Path, "unlink", path_unlink)
    else:
        real_is_link = lanes._is_link
        wanted = os.path.normcase(os.fspath(own.parent))

        def is_link(path):
            answer = real_is_link(path)
            if not answer and os.path.normcase(os.fspath(path)) == wanted:
                swap()
            return answer

        monkeypatch.setattr(lanes, "_is_link", is_link)
    return store, lane_id, pending, own, victim, moved, state, artifact


@pytest.mark.parametrize("seam", ["inside-the-delete-call", "after-the-last-folder-check"])
def test_a_swapped_prepared_folder_cannot_redirect_the_delete_of_a_committed_retry(tmp_path, monkeypatch, capsys, seam):
    store, lane_id, _pending, own, victim, moved, state, artifact = _prepared_copy_with_a_swap_ready(
        tmp_path, monkeypatch, seam
    )
    signed = artifact.read_bytes()
    capsys.readouterr()

    assert cli.main(["--root", str(store.root), "lane", "deliver", "--id", lane_id, "--from", "dev"]) == 0

    assert state["calls"] >= 1, "the swap seam never ran"
    assert victim.read_text(encoding="utf-8") == "not part of any delivery\n"
    assert artifact.read_bytes() == signed
    if seam == "after-the-last-folder-check":
        # the link is found when the folders are opened: nothing is deleted and the existing message says so
        assert state["swapped"] and (moved / own.name).exists()
        assert "left in place" in capsys.readouterr().err
    elif os.name == "nt":
        # the checked folders are held open: the swap itself is refused and the real copy goes
        assert state["refused"] is not None and not state["swapped"] and not own.exists()
    else:
        # the swap goes through, but the delete is made in the folder that was opened: the real copy goes
        assert state["swapped"] and not (moved / own.name).exists()


@pytest.mark.parametrize("seam", ["inside-the-delete-call", "after-the-last-folder-check"])
def test_a_swapped_prepared_folder_cannot_redirect_the_direct_removal(tmp_path, monkeypatch, capsys, seam):
    """The other callers (rollback, a failed save, a competing delivery, a refused final check) ask the same remover."""
    store, _lane_id, pending, own, victim, moved, state, _artifact = _prepared_copy_with_a_swap_ready(
        tmp_path, monkeypatch, seam
    )
    capsys.readouterr()

    cli._lane_remove_prepared(store, pending)

    assert state["calls"] >= 1
    assert victim.read_text(encoding="utf-8") == "not part of any delivery\n"
    if seam == "after-the-last-folder-check":
        assert "left in place" in capsys.readouterr().err
    elif os.name == "nt":
        assert state["refused"] is not None and not own.exists()
    else:
        assert state["swapped"] and not (moved / own.name).exists()


def _managed_folders(tmp_path):
    from test_lanes import _repo

    root, _base = _repo(tmp_path)
    store = Store(root)
    own = lanes.prepared_delivery_artifact_path(store, "lane-a", "cd" * 16)
    own.parent.mkdir(parents=True)
    own.write_text("{}", encoding="utf-8")
    return store, lanes.deliveries_dir(store), own


def test_the_helper_removes_the_copy_and_reports_what_it_did(tmp_path):
    _store, deliveries, own = _managed_folders(tmp_path)
    assert lanes._remove_in_managed_folders(deliveries, own) == "removed"
    assert not own.exists() and own.parent.is_dir()
    assert lanes._remove_in_managed_folders(deliveries, own) == "absent"  # the file is already gone
    own.parent.rmdir()
    assert lanes._remove_in_managed_folders(deliveries, own) == "absent"  # so is the folder


@pytest.mark.parametrize("which", ["prepared-folder", "deliveries-folder"])
def test_the_helper_refuses_a_linked_managed_folder(tmp_path, which):
    _store, deliveries, own = _managed_folders(tmp_path)
    independent = tmp_path.parent / f"{tmp_path.name}-linked-843"
    independent.mkdir()
    if which == "prepared-folder":
        (independent / own.name).write_text("not part of any delivery\n", encoding="utf-8")
        original = own.parent.rename(own.parent.with_name(".prepared-original"))
        _link_directory(own.parent, independent)
        victim = independent / own.name
    else:
        (independent / ".prepared").mkdir()
        victim = independent / ".prepared" / own.name
        victim.write_text("not part of any delivery\n", encoding="utf-8")
        original = deliveries.rename(deliveries.with_name("lane-deliveries-original"))
        _link_directory(deliveries, independent)

    assert lanes._remove_in_managed_folders(deliveries, own) == "refused"

    assert victim.read_text(encoding="utf-8") == "not part of any delivery\n"
    assert any(original.rglob(own.name))  # the real copy was not touched either


def test_the_helper_refuses_a_file_that_is_not_in_the_prepared_folder(tmp_path):
    _store, deliveries, own = _managed_folders(tmp_path)
    stray = deliveries / own.name
    stray.write_text("{}", encoding="utf-8")
    assert lanes._remove_in_managed_folders(deliveries, stray) == "refused"
    assert stray.exists() and own.exists()


def test_the_prepared_copy_is_left_in_place_when_the_held_folder_delete_is_not_available(
    tmp_path, monkeypatch, capsys
):
    """No folder-relative delete (POSIX) or no way to hold the folders (Windows): the copy stays and the existing
    message is printed. It is never deleted by name instead: a leftover prepared copy is never accepted as a
    delivery."""
    store, deliveries, own = _managed_folders(tmp_path)
    pending = {"lane_id": "lane-a", "transaction_id": "cd" * 16, "prepared_artifact": str(own)}
    if os.name == "nt":
        import ctypes

        def unavailable(*_args, **_kwargs):
            raise OSError("no kernel32 here")

        monkeypatch.setattr(ctypes, "WinDLL", unavailable)
    else:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    capsys.readouterr()

    cli._lane_remove_prepared(store, pending)

    assert own.exists()
    assert "left in place" in capsys.readouterr().err


def test_the_prepared_copy_remover_no_longer_deletes_by_name():
    import inspect

    source = inspect.getsource(cli._lane_remove_prepared)
    assert "own.unlink" not in source and ".unlink(" not in source
    assert "_remove_in_managed_folders" in source


@pytest.mark.parametrize("missing", ["prepared-folder", "file"])
def test_the_posix_helper_closes_every_descriptor_it_opened_on_an_early_return(tmp_path, monkeypatch, missing):
    """A missing `.prepared` folder (or a missing file in it) is "absent", and every descriptor opened by then is
    closed. The helper is exercised through stand-ins for the folder-relative calls, so this runs on every system."""
    opened, closed = [], []
    real_close = os.close

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and missing == "prepared-folder":
            raise FileNotFoundError(2, "no .prepared", path)
        descriptor = 1000 + len(opened)
        opened.append(descriptor)
        return descriptor

    def fake_close(descriptor):
        if descriptor in opened:
            closed.append(descriptor)
            return None
        return real_close(descriptor)

    def fake_unlink(name, *, dir_fd=None):
        raise FileNotFoundError(2, "no file", name)

    monkeypatch.setattr(os, "O_DIRECTORY", 0o200000, raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0o400000, raising=False)
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "unlink", fake_unlink)
    monkeypatch.setattr(os, "close", fake_close)
    monkeypatch.setattr(os, "supports_dir_fd", {fake_open, fake_unlink})

    answer = lanes._remove_held_posix(str(tmp_path / "lane-deliveries"), ".prepared", "x.prepared.json")

    assert answer == "absent"
    assert opened and sorted(closed) == sorted(opened), (opened, closed)


def _hold_stand_ins(monkeypatch):
    """Stand-ins for the folder-relative calls (so this runs on every host): returns the list of `(name, dir_fd)`
    that the held-folder delete made, and the list of folders it opened as `(path, dir_fd)`."""
    opened, deleted = [], []

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        opened.append((path, dir_fd))
        return 1000 + len(opened)

    def fake_unlink(name, *, dir_fd=None):
        deleted.append((name, dir_fd))

    monkeypatch.setattr(os, "O_DIRECTORY", 0o200000, raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0o400000, raising=False)
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "close", lambda descriptor: None)
    monkeypatch.setattr(os, "unlink", fake_unlink)
    monkeypatch.setattr(os, "supports_dir_fd", {fake_open, fake_unlink})
    return opened, deleted


def test_a_plain_replacement_of_os_unlink_is_refused_before_it_is_reached(tmp_path, monkeypatch):
    """The cause of the CI failure on Linux (Python 3.10 and 3.11): the held-folder delete only runs when `os.unlink` is
    one of the functions the platform lists in `os.supports_dir_fd` (that set holds the original function objects).
    A test that swaps in its own `os.unlink` without listing it is refused before the delete is made, so a hook inside
    the delete call is never reached, and the copy is left in place. That refusal is the product being careful, and
    stays."""
    opened, deleted = _hold_stand_ins(monkeypatch)
    reached = []
    monkeypatch.setattr(os, "unlink", lambda name, **kwargs: reached.append(name))  # not listed in supports_dir_fd

    assert lanes._remove_held_posix(str(tmp_path / "lane-deliveries"), ".prepared", "x.prepared.json") == "refused"
    assert reached == [] and deleted == []


def test_a_hook_in_the_delete_call_is_reached_and_the_delete_is_made_in_the_held_folder(tmp_path, monkeypatch):
    """With the hook listed (`_hook_unlink`, the one the swap seam uses), the held-folder delete reaches it, and the
    real delete behind it is made relative to the second folder that was opened (the `.prepared` folder), never by
    name: the swap inside the call cannot redirect it."""
    opened, deleted = _hold_stand_ins(monkeypatch)
    real_unlink, reached = os.unlink, []

    def hook(name, *args, **kwargs):
        reached.append(name)  # a swap of `.prepared` for a link would happen here
        return real_unlink(name, *args, **kwargs)

    _hook_unlink(monkeypatch, hook)

    answer = lanes._remove_held_posix(str(tmp_path / "lane-deliveries"), ".prepared", "x.prepared.json")

    assert answer == "removed" and reached == ["x.prepared.json"]
    prepared_descriptor = 1002  # the second folder opened: the first is `lane-deliveries`, the second `.prepared`
    assert opened[1][0] == ".prepared" and opened[1][1] == 1001
    assert deleted == [("x.prepared.json", prepared_descriptor)]


@pytest.mark.skipif(os.name == "nt", reason="the held-folder delete is the POSIX route; Windows holds the folders open")
def test_the_real_os_takes_the_held_folder_route_on_this_platform():
    """Recorded on every Linux and macOS run: the guard of the POSIX route, asked of the REAL `os` (no stand-in, no
    patch): both open flags exist and `os.open` and `os.unlink` are listed in `os.supports_dir_fd`. If this fails, the
    prepared copy is left in place on that platform and the seam tests above cannot reach the delete."""
    assert lanes._held_folder_delete_available(), (
        f"O_DIRECTORY={hasattr(os, 'O_DIRECTORY')} O_NOFOLLOW={hasattr(os, 'O_NOFOLLOW')} "
        f"open_in_supports_dir_fd={os.open in os.supports_dir_fd} "
        f"unlink_in_supports_dir_fd={os.unlink in os.supports_dir_fd}"
    )


def test_the_seam_s_change_to_os_is_undone_when_its_patch_is_undone():
    """`_hook_unlink` lists its wrapper in `os.supports_dir_fd` and replaces `os.unlink`; both go back, to the very same
    objects, when the monkeypatch is undone (the seam tests use the `monkeypatch` fixture, which undoes it after each
    test)."""
    original_unlink, original_set = os.unlink, os.supports_dir_fd
    listed_before = set(original_set)

    def wrapper(name, *args, **kwargs):
        return original_unlink(name, *args, **kwargs)

    patch = pytest.MonkeyPatch()
    try:
        _hook_unlink(patch, wrapper)
        assert os.unlink is wrapper and wrapper in os.supports_dir_fd and os.supports_dir_fd is not original_set
    finally:
        patch.undo()
    assert os.unlink is original_unlink and os.supports_dir_fd is original_set
    assert set(os.supports_dir_fd) == listed_before and wrapper not in os.supports_dir_fd


def test_the_posix_helper_closes_a_descriptor_when_the_second_open_is_refused(tmp_path, monkeypatch):
    """The refusal branch (a link, or not a folder) closes what was opened too."""
    opened, closed = [], []

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise OSError(40, "too many levels of symbolic links", path)
        opened.append(1000 + len(opened))
        return opened[-1]

    def fake_close(descriptor):
        closed.append(descriptor)

    def fake_unlink(name, *, dir_fd=None):
        raise AssertionError("nothing may be deleted")

    monkeypatch.setattr(os, "O_DIRECTORY", 0o200000, raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0o400000, raising=False)
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "unlink", fake_unlink)
    monkeypatch.setattr(os, "close", fake_close)
    monkeypatch.setattr(os, "supports_dir_fd", {fake_open, fake_unlink})

    assert lanes._remove_held_posix(str(tmp_path / "lane-deliveries"), ".prepared", "x.prepared.json") == "refused"
    assert opened and sorted(closed) == sorted(opened)
