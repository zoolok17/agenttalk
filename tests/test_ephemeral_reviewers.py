from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agenttalk import attention as att
from agenttalk import ephemeral as eph
from agenttalk import cli
from agenttalk import lanes
from agenttalk import store as store_mod
from agenttalk import supervisor as sup
from agenttalk import wrapper_runtime as wrt
from agenttalk.store import Store
from agenttalk.wrapper import loop


SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = 1000.0
TEST_ROOT = r"D:\agenttalk-test-root"
SUPERVISOR_NONCE = "A" * 32


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    s.set_role("lead", "lead")
    return s


def _cfg(**overrides) -> dict:
    base = {
        "agents": {},
        "ephemeral_reviewers": {
            "enabled": True,
            "max_concurrent": 1,
            "max_per_hour": 4,
            "max_per_day": 16,
            "default_timeout_seconds": 60,
            "max_prompt_bytes": 2000,
            "require_authorized_lead": True,
            "allowed_skills": ["review-code"],
            "allowed_roles": ["reviewer"],
            "allowed_groups": ["ephemeral-reviewers"],
            "allowed_profiles": {
                "codex-evidence-reviewer": {
                    "cli": "codex",
                    "role": "reviewer",
                    "groups": ["ephemeral-reviewers"],
                    "launch": {
                        "windows_file": "python.exe",
                        "windows_args": [
                            "-m", "agenttalk", "--root", "{ROOT}", "wrap", "--for", "{AGENT}",
                            "--cli", "codex", "--loop", "--one-shot",
                            "--to-request", "{REQUEST_ID}", "--", "codex",
                        ],
                    },
                },
            },
        },
    }
    base["ephemeral_reviewers"].update(overrides)
    return base


def _marker(rid: str = "lr-1", **overrides) -> dict:
    base = {
        "schema_version": eph.SCHEMA_VERSION,
        "kind": eph.REQUEST_KIND,
        "request_id": rid,
        "state": eph.STATE_QUEUED,
        "requested_by": "lead",
        "profile": "codex-evidence-reviewer",
        "skill": "review-code",
        "prompt": "review the diff adversarially",
        "scope": {"revision": SHA, "paths": ["src/agenttalk/supervisor.py"]},
    }
    base.update(overrides)
    return base


def _report(marker: dict | None = None, *, orphan: bool = False, active: dict | None = None) -> dict:
    roster = ["lead", "dev"]
    if orphan:
        roster.append("adversary-orphan")
    return {
        "roster": roster,
        "operator_facing": None,
        "agents": {
            "lead": {"role": "lead"},
            "dev": {"role": None},
            **({"adversary-orphan": {"role": "reviewer"}} if orphan else {}),
        },
        "launch_requests": [marker] if marker is not None else [],
        "ephemeral_reviewers": {"active": active or {}, "orphan_agents": ["adversary-orphan"] if orphan else []},
    }


def _runtime_view(
    agent: str,
    *,
    wrapper_pid: int = 10,
    wrapper_start: str = "1970-01-01T00:10:00Z",
    wrapper_generation: str = "wrapper-1",
    phase: str = "idle",
    launcher_pid: int | None = None,
    launcher_start: str | None = None,
) -> dict:
    active = phase == "active"
    return {
        "status": "valid",
        "record": {
            "schema_version": 1,
            "agent": agent,
            "wrapper_pid": wrapper_pid,
            "wrapper_start": wrapper_start,
            "wrapper_generation": wrapper_generation,
            "phase": phase,
            "turn_generation": 1,
            "turn_id": "turn-1" if active else None,
            "message_id": "msg-1" if active else None,
            "cli_launcher_pid": launcher_pid if active else None,
            "cli_launcher_start": launcher_start if active else None,
            "progress_sequence": 1 if active else 0,
            "last_progress_at": "1970-01-01T00:16:38Z" if active else None,
            "last_outcome": None,
            "updated_at": "1970-01-01T00:16:39Z",
        },
        "error": None,
    }


def _wrapper_row(
    agent: str,
    *,
    pid: int = 10,
    start: str = "1970-01-01T00:10:00Z",
    parent_pid: int = 1,
) -> dict:
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "name": "python.exe",
        "command_line": (
            "python -m agenttalk "
            f"--supervisor-launch-nonce {SUPERVISOR_NONCE} "
            f"--root {TEST_ROOT} wrap --for {agent} --loop"
        ),
        "start_time": start,
        "start_filetime": None,
    }


def test_marker_schema_rejects_counted_signoff_and_short_revision() -> None:
    bad = _marker(scope={"revision": "deadbeef"}, close_feed={"mode": "counted_signoff"})
    errors = eph.validate_marker(bad)
    assert "scope.revision must be a resolved full 40-char SHA" in errors
    assert any("counted_signoff" in e for e in errors)


def test_config_validation_fails_closed_for_prompt_unauthorized_and_stale(tmp_path: Path) -> None:
    s = _store(tmp_path)
    marker = _marker(requested_by="dev", prompt="x" * 30, scope={"revision": SHA})
    cfg = _cfg(max_prompt_bytes=10, current_revision=OTHER_SHA)
    errors, profile = eph.validate_launch_request(marker, s.load_config(), cfg)
    assert profile is not None
    assert any("sole lead requester required" in e for e in errors)
    assert any("above max_prompt_bytes=10" in e for e in errors)
    assert any("stale" in e for e in errors)


def test_validate_launch_request_rejects_module_args_from_the_resolver_would_reject(
    tmp_path: Path,
) -> None:
    """Round 17 connector finding, the fifth instance of one rule: the
    validator must accept exactly what the runtime resolver accepts.
    launch_spec() (round 13) and the wholesale entry persistence (round
    14) both make module_args_from survive the launch pipeline faithfully
    - including when it is wrong, which is exactly what a faithful
    pipeline does. The missing half was validation at the entry point:
    a malformed or wrong module_args_from used to sail through
    validate_launch_request unchecked, launch a Python process that
    silently gets no nonce injection or bounded logging, and then get
    PERSISTED that way - leaving the reviewer permanently stuck in
    process_tree_hold with no teardown authority, because
    _wrapped_liveness can never confirm the launcher's identity either.
    Calls supervisor._resolve_module_flag_index - the SAME resolver
    bootstrap_check delegates to - not a parallel reimplementation."""
    s = _store(tmp_path)

    wrong_token_profile = {
        "codex-evidence-reviewer": {
            "cli": "codex",
            "role": "reviewer",
            "groups": ["ephemeral-reviewers"],
            "launch": {
                "windows_file": "python.exe",
                "windows_args": [
                    "-u", "-Xutf8", "-m", "agenttalk", "--root", "{ROOT}",
                    "wrap", "--for", "{AGENT}", "--", "codex",
                ],
                "module_args_from": 1,
            },
        },
    }
    marker = _marker()
    cfg = _cfg(allowed_profiles=wrong_token_profile)
    errors, profile = eph.validate_launch_request(marker, s.load_config(), cfg)
    assert profile is not None
    assert any(
        "does not resolve against launch.windows_args" in e for e in errors
    ), errors

    malformed_profile = {
        "codex-evidence-reviewer": {
            **wrong_token_profile["codex-evidence-reviewer"],
            "launch": {
                **wrong_token_profile["codex-evidence-reviewer"]["launch"],
                "module_args_from": "1x",
            },
        },
    }
    errors2, _ = eph.validate_launch_request(
        marker, s.load_config(), _cfg(allowed_profiles=malformed_profile),
    )
    assert any("must be an integer" in e for e in errors2), errors2

    # The companion positive: a genuinely valid declared prefix, and the
    # plain undeclared form, must both stay clean - this must not turn
    # every correctly configured profile into a rejection.
    valid_profile = {
        "codex-evidence-reviewer": {
            **wrong_token_profile["codex-evidence-reviewer"],
            "launch": {
                **wrong_token_profile["codex-evidence-reviewer"]["launch"],
                "windows_args": [
                    "-Xutf8", "-m", "agenttalk", "--root", "{ROOT}",
                    "wrap", "--for", "{AGENT}", "--", "codex",
                ],
            },
        },
    }
    errors3, profile3 = eph.validate_launch_request(
        marker, s.load_config(), _cfg(allowed_profiles=valid_profile),
    )
    assert profile3 is not None
    assert not any("module_args_from" in e for e in errors3), errors3

    errors4, profile4 = eph.validate_launch_request(marker, s.load_config(), _cfg())
    assert profile4 is not None
    assert not any("module_args_from" in e for e in errors4), errors4


def test_plan_denies_when_disabled_or_capacity_exceeded() -> None:
    disabled = _cfg(enabled=False)
    plan = sup.plan_actions(_report(_marker()), {}, disabled, now_epoch=NOW, snapshot=[])
    assert plan["launch_requests"]["lr-1"]["action"] == eph.ACTION_DENY
    assert "enabled is false" in plan["launch_requests"]["lr-1"]["reason"]

    state = {"ephemeral_reviewers": {"active": {"lr-old": {"phase": eph.STATE_LAUNCHED}}}}
    plan2 = sup.plan_actions(_report(_marker()), state, _cfg(max_concurrent=1),
                             now_epoch=NOW, snapshot=[])
    assert plan2["launch_requests"]["lr-1"]["action"] == eph.ACTION_DENY
    assert "max_concurrent" in plan2["launch_requests"]["lr-1"]["reason"]


def test_claim_no_double_launch_and_archive_is_request_id_checked(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.write_launch_request(_marker("lr-claim"))
    first = s.claim_launch_request("lr-claim", claimed_by="supervisor", at_epoch=NOW)
    second = s.claim_launch_request("lr-claim", claimed_by="supervisor-2", at_epoch=NOW)
    assert first is not None and first["state"] == eph.STATE_CLAIMED
    assert second is None
    assert s.archive_launch_request("lr-other", {"request_id": "lr-other"}) is False
    assert s.read_launch_request("lr-claim") is not None
    assert s.archive_launch_request("lr-claim", {"request_id": "lr-claim"}) is True
    assert s.read_launch_request("lr-claim") is None


def test_launch_request_creation_is_exclusive_under_concurrency(tmp_path: Path) -> None:
    s = _store(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(prompt: str) -> None:
        barrier.wait()
        try:
            s.write_launch_request(_marker("lr-exclusive", prompt=prompt))
        except ValueError:
            outcomes.append("duplicate")
        else:
            outcomes.append("created")

    first = threading.Thread(target=create, args=("first",))
    second = threading.Thread(target=create, args=("second",))
    first.start()
    second.start()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(outcomes) == ["created", "duplicate"]
    assert s.read_launch_request("lr-exclusive")["prompt"] in {"first", "second"}


@pytest.mark.parametrize("failure_phase", ["write", "fsync", "close"])
def test_partial_launch_request_create_cleans_creator_owned_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    store = _store(tmp_path)
    request_id = f"lr-partial-{failure_phase}"
    path = store._launch_request_path(request_id)
    real_close = store_mod.os.close
    real_fsync = store_mod.os.fsync
    real_open = store_mod.os.open
    real_write = store_mod._write_all
    target_fds: set[int] = set()

    def tracked_open(raw_path, flags, *args):
        fd = real_open(raw_path, flags, *args)
        opened = Path(raw_path)
        if (
            opened.parent == path.parent
            and opened.name.startswith(f".{path.name}.")
            and opened.name.endswith(".prepare")
        ):
            target_fds.add(fd)
        return fd

    monkeypatch.setattr(store_mod.os, "open", tracked_open)

    if failure_phase == "write":
        def fail_write(fd: int, raw: bytes) -> None:
            if fd in target_fds:
                target_fds.remove(fd)
                raise OSError("injected launch write failure")
            real_write(fd, raw)

        monkeypatch.setattr(store_mod, "_write_all", fail_write)
    elif failure_phase == "fsync":
        def fail_fsync(fd: int) -> None:
            if fd in target_fds:
                target_fds.remove(fd)
                raise OSError("injected launch fsync failure")
            real_fsync(fd)

        monkeypatch.setattr(store_mod.os, "fsync", fail_fsync)
    else:
        def fail_close(fd: int) -> None:
            if fd in target_fds:
                target_fds.remove(fd)
                real_close(fd)
                raise OSError("injected launch close failure")
            real_close(fd)

        monkeypatch.setattr(store_mod.os, "close", fail_close)

    with pytest.raises(OSError, match=failure_phase):
        store.write_launch_request(_marker(request_id))

    assert not path.exists()
    assert list(path.parent.glob(f".{path.name}.*.prepare")) == []


@pytest.mark.parametrize(
    ("crash_phase", "public_expected"),
    [("before-link", False), ("after-link", True)],
)
def test_launch_request_abrupt_publish_death_is_absent_or_complete(
    tmp_path: Path,
    crash_phase: str,
    public_expected: bool,
) -> None:
    store = _store(tmp_path)
    request_id = f"lr-crash-{crash_phase}"
    payload = _marker(request_id)
    path = store._launch_request_path(request_id)
    crash_code = r"""
import json, os, pathlib, sys
from agenttalk import store as store_mod
from agenttalk.store import Store

root = pathlib.Path(sys.argv[1])
request_path = pathlib.Path(sys.argv[2])
phase = sys.argv[3]
payload = json.loads(sys.argv[4])
real_link = store_mod.os.link

def crash_link(source, destination, *, follow_symlinks=True):
    if pathlib.Path(destination) == request_path:
        if phase == 'after-link':
            real_link(source, destination, follow_symlinks=follow_symlinks)
        os._exit(91)
    return real_link(source, destination, follow_symlinks=follow_symlinks)

store_mod.os.link = crash_link
Store(root).write_launch_request(payload)
"""

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_code,
            str(tmp_path),
            str(path),
            crash_phase,
            json.dumps(payload),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 91, crashed.stderr
    assert path.exists() is public_expected

    if public_expected:
        assert json.loads(path.read_text(encoding="utf-8"))["request_id"] == request_id
    else:
        assert store.read_launch_request(request_id) is None
        store.write_launch_request(payload)

    assert [marker["request_id"] for marker in store.list_launch_requests()] == [
        request_id
    ]
    claimed = store.claim_launch_request(
        request_id,
        claimed_by="supervisor",
        at_epoch=NOW,
    )
    assert claimed is not None and claimed["state"] == eph.STATE_CLAIMED


def test_launch_request_create_rejects_hardlink_without_overwriting_target(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id = "lr-hardlink"
    path = store._launch_request_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "launch-target.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    os.link(target, path)

    with pytest.raises(ValueError, match="already exists"):
        store.write_launch_request(_marker(request_id))

    assert target.read_text(encoding="utf-8") == "do not overwrite"
    assert os.path.samefile(target, path)
    assert list(path.parent.glob(f".{path.name}.*.prepare")) == []


def test_launch_request_state_updates_cannot_regress(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.write_launch_request(_marker("lr-monotonic"))
    assert s.claim_launch_request(
        "lr-monotonic", claimed_by="supervisor", at_epoch=NOW,
    )["state"] == eph.STATE_CLAIMED
    assert s.update_launch_request(
        "lr-monotonic", {"state": eph.STATE_REQUESTED},
    )["state"] == eph.STATE_REQUESTED
    assert s.update_launch_request(
        "lr-monotonic", {"state": eph.STATE_LAUNCHED},
    )["state"] == eph.STATE_LAUNCHED

    with pytest.raises(ValueError, match="cannot transition launch request"):
        s.update_launch_request("lr-monotonic", {"state": eph.STATE_CLAIMED})

    assert s.read_launch_request("lr-monotonic")["state"] == eph.STATE_LAUNCHED


def test_prepare_rosters_unique_identity_sends_request_and_completion_retires(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.write_launch_request(_marker("lr-prep"))
    state: dict = {}
    spec = sup.prepare_launch_request(s, state, _cfg(), "lr-prep", now_epoch=NOW)
    agent = spec["agent"]
    assert agent.startswith("adversary-lr-prep")
    assert agent in s.load_config()["agents"]
    assert "--one-shot" in spec["launch"]["windows_args"]
    req = s.messages_for(agent)[0]
    assert req.kind == "review-request"
    assert req.meta["evidence_only"] == "true"
    assert req.meta["counted_signoff"] == "false"
    assert state["ephemeral_reviewers"]["active"]["lr-prep"]["cli"] == "codex"

    wrapper_start = (
        "linux:12345678-1234-1234-1234-123456789abc:100"
    )
    sup.record_ephemeral_launch(
        state,
        "lr-prep",
        pid=10,
        pid_start=wrapper_start,
        now_epoch=NOW,
        launcher_nonce=SUPERVISOR_NONCE,
        launcher_nonce_injected=True,
        launcher_nonce_source="agenttalk_global_arg",
    )

    s.send(sender=agent, recipient="lead", kind="review-result", body="reject",
           meta={"request_id": "lr-prep", "status": "rejected"})
    report = sup.build_report(s, now_epoch=NOW + 1, state=state, supervisor_config=_cfg())
    report["root_key"] = sup._root_key(TEST_ROOT)
    report["ephemeral_reviewers"]["active"]["lr-prep"]["wrapper_runtime"] = (
        _runtime_view(agent, wrapper_start=wrapper_start)
    )
    snapshot = [_wrapper_row(agent, start=wrapper_start)]
    adoption = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW + 1,
        snapshot=snapshot,
    )["ephemeral_reviewers"]["lr-prep"]
    assert adoption["action"] == eph.ACTION_NONE
    assert adoption["state"] == "process_tree_hold"
    state["ephemeral_reviewers"]["active"]["lr-prep"] = adoption["next_entry"]
    plan = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW + 2,
        snapshot=snapshot,
    )
    done = plan["ephemeral_reviewers"]["lr-prep"]
    assert done["action"] == eph.ACTION_COMPLETE
    assert done["completion"]["counter"] is True
    assert [target["pid"] for target in done["kill_targets"]] == [10]

    sup.archive_ephemeral_request(
        s, state, "lr-prep", terminal_state=eph.STATE_COMPLETED,
        reason="typed review-result status=rejected", completion=done["completion"],
        now_epoch=NOW + 1,
    )
    assert "lr-prep" not in state["ephemeral_reviewers"]["active"]
    assert agent not in s.load_config()["agents"]
    assert agent in s.retired_agents()


def test_prepare_launch_request_resolves_ephemeral_window_style(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.write_launch_request(_marker("lr-style"))
    cfg = _cfg()
    cfg["window_style"] = "normal"
    cfg["ephemeral_reviewers"]["allowed_profiles"]["codex-evidence-reviewer"]["window_style"] = "minimized"

    spec = sup.prepare_launch_request(s, {}, cfg, "lr-style", now_epoch=NOW)

    assert spec["window_style"] == "Minimized"
    assert spec["window_style_warning"] is None


def test_prepare_lane_without_worktree_archives_denied_after_claim(tmp_path: Path) -> None:
    s = _store(tmp_path)
    lane = lanes.new_lane(
        "nowt", assignee="dev", assigned_by="lead", assigned_at="t0",
        domain_id="core", path_subset=[], base_sha=SHA, target_ref="main",
        target_head_at_assign=SHA, epoch_at_assign=None,
        registry_hash_at_assign="hash")
    lanes.save_lanes(s, {"schema_version": lanes.SCHEMA_VERSION, "lanes": {"nowt": lane}})
    marker = _marker("lr-nowt", lane_id="nowt")
    marker["scope"]["lane_id"] = "nowt"
    s.write_launch_request(marker)

    with pytest.raises(eph.EphemeralError, match="no provisioned worktree"):
        sup.prepare_launch_request(s, {}, _cfg(), "lr-nowt", now_epoch=NOW)

    assert s.read_launch_request("lr-nowt") is None
    archived = json.loads(
        (s.launch_requests_archive_dir / "lr-nowt.json").read_text(encoding="utf-8"))
    assert archived["terminal_state"] == eph.STATE_DENIED
    assert "no provisioned worktree" in archived["reason"]


def test_prepare_inactive_lane_worktree_archives_denied_after_claim(tmp_path: Path) -> None:
    s = _store(tmp_path)
    lane = lanes.new_lane(
        "done", assignee="dev", assigned_by="lead", assigned_at="t0",
        domain_id="core", path_subset=[], base_sha=SHA, target_ref="main",
        target_head_at_assign=SHA, epoch_at_assign=None,
        registry_hash_at_assign="hash",
        worktree={"path": str(tmp_path / ".worktrees" / "done"), "branch": "lane/done",
                  "base_sha": SHA, "created_at": "t0", "root": str(tmp_path / ".worktrees")})
    lane["status"] = lanes.STATUS_DELIVERED
    lanes.save_lanes(s, {"schema_version": lanes.SCHEMA_VERSION, "lanes": {"done": lane}})
    marker = _marker("lr-done", lane_id="done")
    marker["scope"]["lane_id"] = "done"
    s.write_launch_request(marker)

    with pytest.raises(eph.EphemeralError, match="not active"):
        sup.prepare_launch_request(s, {}, _cfg(), "lr-done", now_epoch=NOW)

    assert s.read_launch_request("lr-done") is None
    archived = json.loads(
        (s.launch_requests_archive_dir / "lr-done.json").read_text(encoding="utf-8"))
    assert archived["terminal_state"] == eph.STATE_DENIED
    assert "not active" in archived["reason"]


def test_cli_archive_launch_request_preserves_completion_evidence(tmp_path: Path) -> None:
    s = _store(tmp_path)
    agent = "adversary-lr-cli-archive"
    s.add_agent(agent, role="reviewer", groups=["ephemeral-reviewers"])
    review_request = s.send(
        sender="lead",
        recipient=agent,
        kind="review-request",
        body="review",
        meta={
            "request_id": "lr-cli-archive",
            "ephemeral_request_id": "lr-cli-archive",
            "evidence_only": "true",
            "counted_signoff": "false",
        },
    )
    marker = _marker(
        "lr-cli-archive",
        state=eph.STATE_REQUESTED,
        agent=agent,
        review_request_msg_id=review_request.id,
    )
    s.write_launch_request(marker)
    state_path = tmp_path / "supervisor-state.json"
    state_path.write_text(json.dumps({
        "ephemeral_reviewers": {
            "launch_history": [{
                "request_id": "lr-cli-archive",
                "agent": agent,
                "at_epoch": NOW,
            }],
            "active": {
                "lr-cli-archive": {
                    "request_id": "lr-cli-archive",
                    "agent": agent,
                    "requested_by": "lead",
                    "review_request_id": review_request.id,
                    "phase": eph.STATE_LAUNCHED,
                }
            }
        }
    }), encoding="utf-8")
    completion = {
        "status": eph.COMPLETION_REJECTED,
        "terminal": True,
        "hold": False,
        "counter": True,
        "message_id": "msg-1",
        "evidence_only": True,
    }

    rc = cli.main([
        "--root", str(tmp_path),
        "supervise",
        "--archive-launch-request",
        "--request-id", "lr-cli-archive",
        "--terminal-state", eph.STATE_COMPLETED,
        "--reason", "typed review-result status=rejected",
        "--completion-json", json.dumps(completion),
        "--state-file", str(state_path),
        "--now", str(NOW + 1),
    ])

    assert rc == 0
    archived = json.loads(
        (s.launch_requests_archive_dir / "lr-cli-archive.json").read_text(encoding="utf-8"))
    assert archived["completion"] == completion
    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "lr-cli-archive" not in saved_state["ephemeral_reviewers"]["active"]
    assert agent in s.retired_agents()


def test_archive_failure_retains_ephemeral_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    request_id = "lr-archive-failure"
    state = {
        "ephemeral_reviewers": {
            "active": {
                request_id: {
                    "request_id": request_id,
                    "agent": "adversary-lr-archive-failure",
                },
            },
        },
    }
    monkeypatch.setattr(store, "archive_launch_request", lambda *_args: False)

    with pytest.raises(eph.EphemeralError, match="active state retained"):
        sup.archive_ephemeral_request(
            store,
            state,
            request_id,
            terminal_state=eph.STATE_FAILED,
            reason="archive write failed",
            retire=False,
        )

    assert request_id in state["ephemeral_reviewers"]["active"]


def test_retired_name_is_not_reused(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_agent("adversary-lr-repeat", role="reviewer", groups=["ephemeral-reviewers"])
    s.retire_agent("adversary-lr-repeat", reason="done")
    name = eph.choose_agent_name("lr-repeat", s.load_config()["agents"], s.retired_agents())
    assert name != "adversary-lr-repeat"
    assert name.startswith("adversary-lr-repeat-")
    assert eph.agent_name_matches_request("lr-repeat", name)
    assert not eph.agent_name_matches_request("lr-other", name)


def test_launched_exit_without_fresh_tree_holds_without_restart_or_archive() -> None:
    state = {"ephemeral_reviewers": {"active": {
        "lr-dead": {"agent": "adversary-lr-dead", "requested_by": "lead",
                    "phase": eph.STATE_LAUNCHED, "launcher_pid": 10,
                    "launcher_start": "t-start", "deadline_epoch": NOW + 100}
    }}}
    report = _report(active={"lr-dead": {
        "completion": {"status": eph.COMPLETION_NONE, "terminal": False, "hold": True}
    }})
    plan = sup.plan_actions(report, state, _cfg(), now_epoch=NOW, snapshot=[])
    held = plan["ephemeral_reviewers"]["lr-dead"]
    assert held["action"] == eph.ACTION_NONE
    assert held["state"] == "process_tree_hold"
    assert held["kill_targets"] == []
    assert held["retire"] is False
    assert held["archive"] is False
    assert held["auto_restart"] is False
    attention_items = att.process_tree_hold_items({
        "ephemeral_reviewers": {
            "active": {"lr-dead": held["next_entry"]},
        },
    })
    assert [item["item_id"] for item in attention_items] == [
        "process_tree_hold:ephemeral:lr-dead",
    ]
    assert "complete current ownership" in attention_items[0]["why_it_matters"]


@pytest.mark.parametrize("agent", [None, {}, "", "bad/agent"])
def test_invalid_ephemeral_agent_identity_holds_and_surfaces_attention(
    agent: object,
) -> None:
    state = {
        "ephemeral_reviewers": {
            "active": {
                "lr-invalid-agent": {
                    "request_id": "lr-invalid-agent",
                    "agent": agent,
                    "requested_by": "lead",
                    "phase": eph.STATE_LAUNCHED,
                    "launcher_pid": 10,
                    "launcher_start": "t-start",
                    "deadline_epoch": NOW - 1,
                },
            },
        },
    }
    report = _report(active={"lr-invalid-agent": {
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
    }})

    held = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW,
        snapshot=[],
    )["ephemeral_reviewers"]["lr-invalid-agent"]

    assert held["action"] == eph.ACTION_NONE
    assert held["state"] == "process_tree_hold"
    assert held["kill_targets"] == []
    assert held["archive"] is False
    assert "owned_process_tree" not in held["next_entry"]
    assert held["next_entry"]["process_tree_hold_reason"] == (
        "ephemeral_agent_identity_missing"
    )

    items = att.process_tree_hold_items({
        "ephemeral_reviewers": {
            "active": {"lr-invalid-agent": held["next_entry"]},
        },
    })
    assert len(items) == 1
    assert items[0]["item_id"] == (
        "process_tree_hold:ephemeral:lr-invalid-agent"
    )
    assert items[0]["affected"] == ["lr-invalid-agent"]
    assert items[0]["source_refs"][0]["agent"] == "lr-invalid-agent"
    assert items[0]["advisory"] is False


def test_timeout_plans_process_tree_kill_targets() -> None:
    boot_id = "12345678-1234-1234-1234-123456789abc"
    wrapper_start = f"linux:{boot_id}:100"
    launcher_start = f"linux:{boot_id}:200"
    state = {"ephemeral_reviewers": {"active": {
        "lr-timeout": {"agent": "adversary-lr-timeout", "requested_by": "lead",
                       "phase": eph.STATE_LAUNCHED, "launcher_pid": 10,
                       "launcher_start": wrapper_start, "launched_epoch": NOW - 100,
                       "deadline_epoch": NOW - 1,
                       "cli": "codex",
                       "launcher_nonce": SUPERVISOR_NONCE,
                       "launcher_nonce_injected": True,
                       "launcher_nonce_source": "agenttalk_global_arg"}
    }}}
    report = _report(active={"lr-timeout": {
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
        "wrapper_runtime": _runtime_view(
            "adversary-lr-timeout",
            wrapper_start=wrapper_start,
            phase="active",
            launcher_pid=11,
            launcher_start=launcher_start,
        ),
    }})
    report["root_key"] = sup._root_key(TEST_ROOT)
    snap = [
        _wrapper_row("adversary-lr-timeout", start=wrapper_start),
        {
            "pid": 11,
            "parent_pid": 10,
            "name": "codex.exe",
            "command_line": "codex exec",
            "start_time": launcher_start,
            "start_filetime": None,
        },
        {
            "pid": 12,
            "parent_pid": 11,
            "name": "codex.exe",
            "command_line": "codex tui",
            "start_time": f"linux:{boot_id}:300",
            "start_filetime": None,
        },
        {
            "pid": 13,
            "parent_pid": 12,
            "name": "pwsh.exe",
            "command_line": "pwsh -File tool.ps1",
            "start_time": f"linux:{boot_id}:400",
            "start_filetime": None,
        },
        {
            "pid": 14,
            "parent_pid": 13,
            "name": "node.exe",
            "command_line": "node build.js",
            "start_time": f"linux:{boot_id}:500",
            "start_filetime": None,
        },
    ]
    adoption = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW,
        snapshot=snap,
    )["ephemeral_reviewers"]["lr-timeout"]
    assert adoption["action"] == eph.ACTION_NONE
    assert adoption["state"] == "process_tree_hold"
    state["ephemeral_reviewers"]["active"]["lr-timeout"] = adoption["next_entry"]

    timeout = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW + 1,
        snapshot=snap,
    )["ephemeral_reviewers"]["lr-timeout"]
    assert timeout["action"] == eph.ACTION_TIMEOUT
    assert [t["pid"] for t in timeout["kill_targets"]] == [10, 11, 12, 13, 14]
    assert timeout["next_entry"]["held_terminal"]["terminal_state"] == (
        eph.STATE_TIMED_OUT
    )
    assert [
        entry["role"]
        for entry in timeout["next_entry"]["owned_process_tree"]["entries"]
    ] == [
        "wrapper",
        "cli_launcher",
        "cli_brain",
        "tool_descendant",
        "tool_descendant",
    ]


def test_ephemeral_cap_exceeded_holds_archive_and_escalates_attention() -> None:
    agent = "adversary-lr-cap"
    boot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    wrapper_start = f"linux:{boot}:100"
    launcher_start = f"linux:{boot}:200"
    state = {"ephemeral_reviewers": {"active": {
        "lr-cap": {
            "request_id": "lr-cap",
            "agent": agent,
            "requested_by": "lead",
            "phase": eph.STATE_LAUNCHED,
            "launcher_pid": 10,
            "launcher_start": wrapper_start,
            "deadline_epoch": NOW - 1,
            "cli": "codex",
            "launcher_nonce": SUPERVISOR_NONCE,
            "launcher_nonce_injected": True,
            "launcher_nonce_source": "agenttalk_global_arg",
        }
    }}}
    report = _report(active={"lr-cap": {
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
        "wrapper_runtime": _runtime_view(
            agent,
            wrapper_start=wrapper_start,
            phase="active",
            launcher_pid=11,
            launcher_start=launcher_start,
        ),
    }})
    report["root_key"] = sup._root_key(TEST_ROOT)
    snapshot = [_wrapper_row(agent, start=wrapper_start)]
    parent = 10
    for pid in range(11, 75):
        snapshot.append({
            "pid": pid,
            "parent_pid": parent,
            "name": "codex.exe" if pid in {11, 12} else "node.exe",
            "command_line": "codex exec" if pid == 11 else f"tool {pid}",
            "start_time": f"linux:{boot}:{(pid - 9) * 100}",
            "start_filetime": None,
        })
        parent = pid

    adoption = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW,
        snapshot=snapshot,
    )["ephemeral_reviewers"]["lr-cap"]
    state["ephemeral_reviewers"]["active"]["lr-cap"] = adoption["next_entry"]
    held = sup.plan_actions(
        report,
        state,
        _cfg(),
        now_epoch=NOW + 1,
        snapshot=snapshot,
    )["ephemeral_reviewers"]["lr-cap"]

    assert held["action"] == eph.ACTION_NONE
    assert held["state"] == "process_tree_hold"
    assert held["archive"] is False
    assert held["retire"] is False
    assert held["auto_restart"] is False
    assert held["kill_targets"] == []
    assert held["next_entry"]["owned_process_tree"]["status"] == "truncated"
    assert held["next_entry"]["owned_process_tree"]["observed_count"] == 65
    assert held["next_entry"]["held_terminal"] == {
        "terminal_state": eph.STATE_TIMED_OUT,
        "reason": (
            "ephemeral reviewer timed out without a typed terminal "
            "review-result"
        ),
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
    }

    attention_state = {
        "ephemeral_reviewers": {"active": {"lr-cap": held["next_entry"]}}
    }
    items = att.process_tree_hold_items(
        attention_state,
        reset_admissions={"evaluated": True, "admissions": {}},
    )
    assert [item["item_id"] for item in items] == [
        "process_tree_hold:ephemeral:lr-cap"
    ]
    assert "observed 65 identities over the safe cap 64" in items[0]["why_it_matters"]
    assert "operator_command" not in items[0]
    assert "no scripted remedy applies in this state" in items[0]["recommendation"]
    changed = json.loads(json.dumps(attention_state))
    changed["ephemeral_reviewers"]["active"]["lr-cap"]["held_terminal"][
        "reason"
    ] = "a different bounded terminal reason"
    assert att.process_tree_hold_items(changed)[0]["source_hash"] != (
        items[0]["source_hash"]
    )

    later_report = json.loads(json.dumps(report))
    later_report["ephemeral_reviewers"]["active"]["lr-cap"]["completion"] = {
        "status": eph.COMPLETION_APPROVED,
        "terminal": True,
        "hold": False,
        "counter": False,
        "message_id": "msg-late",
        "evidence_only": True,
    }
    later_state = {
        "ephemeral_reviewers": {"active": {"lr-cap": held["next_entry"]}}
    }
    held_again = sup.plan_actions(
        later_report,
        later_state,
        _cfg(),
        now_epoch=NOW + 2,
        snapshot=snapshot,
    )["ephemeral_reviewers"]["lr-cap"]
    assert held_again["next_entry"]["held_terminal"] == (
        held["next_entry"]["held_terminal"]
    )


def _write_attended_ephemeral_hold_fixture(
    store: Store,
    *,
    request_id: str = "lr-attended",
    agent: str | None = None,
) -> tuple[str, str, str]:
    agent = agent or f"adversary-{request_id}"
    wrapper_start = "1970-01-01T00:10:00Z"
    marker = _marker(
        request_id,
        state=eph.STATE_LAUNCHED,
        agent=agent,
    )
    store.add_agent(agent, role="reviewer", groups=["ephemeral-reviewers"])
    review_request = store.send(
        sender="lead",
        recipient=agent,
        kind="review-request",
        subject=f"ephemeral review {request_id}",
        body=eph.review_request_body(marker, agent),
        meta={
            "request_id": request_id,
            "ephemeral_request_id": request_id,
            "evidence_only": "true",
            "counted_signoff": "false",
            "profile": marker["profile"],
            "skill": marker["skill"],
            "revision": marker["scope"]["revision"],
        },
    )
    marker["review_request_msg_id"] = review_request.id
    store.write_launch_request(marker)
    held_terminal = {
        "terminal_state": eph.STATE_TIMED_OUT,
        "reason": (
            "ephemeral reviewer timed out without a typed terminal "
            "review-result"
        ),
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
    }
    entry = {
        "request_id": request_id,
        "agent": agent,
        "requested_by": "lead",
        "review_request_id": review_request.id,
        "phase": eph.STATE_LAUNCHED,
        "launcher_pid": 10,
        "launcher_start": wrapper_start,
        "launcher_nonce": SUPERVISOR_NONCE,
        "launcher_nonce_injected": True,
        "launcher_nonce_source": "agenttalk_global_arg",
        "runtime_wrapper_generation": "wrapper-1",
        "managed_pids": [],
        "process_tree_hold_reason": "process_tree_truncated",
        "held_terminal": held_terminal,
        "owned_process_tree": {
            "schema_version": 2,
            "attribution_model": "owned_process_tree_v2",
            "agent": agent,
            "root_key": sup._root_key(str(store.root.resolve())),
            "status": "truncated",
            "reason_code": "process_tree_truncated",
            "limit": 64,
            "observed_count": 2,
            "recorded_count": 1,
            "omitted_count": 1,
            "truncated": True,
            "refreshed_at": "1970-01-01T00:16:40Z",
            "wrapper_generation": "wrapper-1",
            "launch_nonce": SUPERVISOR_NONCE,
            "entries": [{
                "pid": 10,
                "start": wrapper_start,
                "start_filetime": None,
                "role": "wrapper",
                "parent_pid": 1,
                "discovered_at": "1970-01-01T00:16:40Z",
            }],
        },
    }
    state = {
        "ephemeral_reviewers": {
            "active": {request_id: entry},
            "launch_history": [{
                "request_id": request_id,
                "agent": agent,
                "at_epoch": NOW,
            }],
        },
    }
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    writer = wrt.WrapperRuntimeWriter(
        store.state_dir,
        agent,
        "wrapper-1",
        wrapper_pid=10,
        wrapper_start=wrapper_start,
        clock=lambda: NOW,
    )
    writer.idle()
    item = att.process_tree_hold_items(state)[0]
    return request_id, agent, item["source_hash"]


def _make_preupgrade_pruned_allocation_fixture(
    store: Store,
    state: dict,
    request_id: str,
) -> None:
    """Model an active request whose old 24h retention pruned its row."""
    marker = store.read_launch_request(request_id)
    assert marker is not None
    updated = store.update_launch_request(request_id, {
        "claimed_by": "supervisor",
        "claimed_at_epoch": NOW,
        "requested_at_epoch": NOW,
    })
    assert updated is not None
    entry = state["ephemeral_reviewers"]["active"][request_id]
    entry["profile"] = marker["profile"]
    entry["prepared_epoch"] = NOW
    state["ephemeral_reviewers"]["launch_history"] = []
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)


def _write_misbound_agent_ephemeral_hold_fixture(
    store: Store,
    *,
    request_id: str = "lr-misbound",
    agent: str = "dev",
    evidence_request_id: str | None = None,
    allocation_request_id: str | None = None,
    write_review_request: bool = True,
) -> tuple[str, dict, bytes]:
    if agent not in store.load_config()["agents"]:
        store.add_agent(agent, role="reviewer", groups=["ephemeral-reviewers"])
    evidence_request_id = evidence_request_id or request_id
    allocation_request_id = allocation_request_id or request_id
    review_request_id = "20260101-000000-000000-Ab12"
    if write_review_request:
        review_request_id = store.send(
            sender="lead",
            recipient=agent,
            kind="review-request",
            subject=f"ephemeral review {request_id}",
            body="review the change adversarially",
            meta={
                "request_id": evidence_request_id,
                "ephemeral_request_id": evidence_request_id,
                "evidence_only": "true",
                "counted_signoff": "false",
            },
        ).id
    marker = _marker(
        request_id,
        state=eph.STATE_LAUNCHED,
        agent=agent,
        review_request_msg_id=review_request_id,
    )
    store.write_launch_request(marker)
    held_terminal = {
        "terminal_state": eph.STATE_TIMED_OUT,
        "reason": "ephemeral reviewer timed out without a result",
        "completion": {
            "status": eph.COMPLETION_NONE,
            "terminal": False,
            "hold": True,
        },
    }
    state = {
        "ephemeral_reviewers": {
            "launch_history": [{
                "request_id": allocation_request_id,
                "agent": agent,
                "at_epoch": NOW,
            }],
            "active": {
                request_id: {
                    "request_id": request_id,
                    "agent": agent,
                    "requested_by": "lead",
                    "review_request_id": review_request_id,
                    "phase": eph.STATE_LAUNCHED,
                    "process_tree_hold_reason": "process_tree_truncated",
                    "held_terminal": held_terminal,
                },
            },
        },
    }
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    marker_bytes = (
        store.launch_requests_dir / f"{request_id}.json"
    ).read_bytes()
    return request_id, state, marker_bytes


def test_normal_agent_misbound_to_ephemeral_hold_has_no_archive_remedy(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, state, _marker_bytes = _write_misbound_agent_ephemeral_hold_fixture(
        store
    )
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")

    item = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


def test_finish_ephemeral_archive_refuses_normal_agent_before_effects(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, state, marker_bytes = _write_misbound_agent_ephemeral_hold_fixture(
        store
    )
    entry = state["ephemeral_reviewers"]["active"][request_id]
    marker = store.read_launch_request(request_id)
    assert marker is not None
    source_hash = att.process_tree_hold_items(state)[0]["source_hash"]
    sup.stage_attended_ephemeral_archive(
        state,
        request_id,
        agent="dev",
        launch_marker=marker,
        held_terminal=entry["held_terminal"],
        hold_source_hash=source_hash,
        acknowledged_by="lead",
        verification_mode="operator_attested",
        verified_launch_nonce=None,
        verified_identity_count=0,
        reason="operator verified the terminal request can be archived",
        now_epoch=NOW,
    )

    with pytest.raises(eph.EphemeralError, match="ephemeral identity does not match"):
        sup.finish_attended_ephemeral_archive(store, state, request_id)

    assert (store.launch_requests_dir / f"{request_id}.json").read_bytes() == marker_bytes
    assert not (store.launch_requests_archive_dir / f"{request_id}.json").exists()
    assert "dev" in store.load_config()["agents"]
    assert "dev" not in store.retired_agents()


def test_terminal_ephemeral_archive_refuses_normal_agent_before_effects(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, state, marker_bytes = _write_misbound_agent_ephemeral_hold_fixture(
        store
    )

    with pytest.raises(eph.EphemeralError, match="ephemeral identity does not match"):
        sup.archive_ephemeral_request(
            store,
            state,
            request_id,
            terminal_state=eph.STATE_TIMED_OUT,
            reason="timed out",
            now_epoch=NOW,
        )

    assert (store.launch_requests_dir / f"{request_id}.json").read_bytes() == marker_bytes
    assert not (store.launch_requests_archive_dir / f"{request_id}.json").exists()
    assert "dev" in store.load_config()["agents"]
    assert "dev" not in store.retired_agents()


def test_ephemeral_archive_requires_exact_durable_request_binding(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    prefix = "a" * 48
    original_request = f"{prefix}x"
    copied_request = f"{prefix}y"
    agent = eph.choose_agent_name(
        original_request,
        store.load_config()["agents"],
        store.retired_agents(),
    )
    request_id, state, marker_bytes = _write_misbound_agent_ephemeral_hold_fixture(
        store,
        request_id=copied_request,
        agent=agent,
        allocation_request_id=original_request,
    )
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    assert eph.agent_name_matches_request(original_request, agent)
    assert eph.agent_name_matches_request(copied_request, agent)

    item = _current_ephemeral_reset_item(store, state, request_id)
    assert "operator_argv" not in item
    with pytest.raises(eph.EphemeralError, match="ephemeral identity does not match"):
        sup.archive_ephemeral_request(
            store,
            state,
            request_id,
            terminal_state=eph.STATE_TIMED_OUT,
            reason="timed out",
            now_epoch=NOW,
        )

    assert (store.launch_requests_dir / f"{request_id}.json").read_bytes() == marker_bytes
    assert agent in store.load_config()["agents"]
    assert agent not in store.retired_agents()


def test_ephemeral_archive_requires_validated_review_request_evidence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id = "lr-missing-evidence"
    agent = eph.choose_agent_name(
        request_id,
        store.load_config()["agents"],
        store.retired_agents(),
    )
    request_id, state, marker_bytes = _write_misbound_agent_ephemeral_hold_fixture(
        store,
        request_id=request_id,
        agent=agent,
        write_review_request=False,
    )
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")

    item = _current_ephemeral_reset_item(store, state, request_id)
    assert "operator_argv" not in item
    with pytest.raises(eph.EphemeralError, match="ephemeral identity does not match"):
        sup.archive_ephemeral_request(
            store,
            state,
            request_id,
            terminal_state=eph.STATE_TIMED_OUT,
            reason="timed out",
            now_epoch=NOW,
        )

    assert (store.launch_requests_dir / f"{request_id}.json").read_bytes() == marker_bytes
    assert agent in store.load_config()["agents"]
    assert agent not in store.retired_agents()


@pytest.mark.parametrize(
    ("request_id", "agent"),
    [
        ("lr-0123456789ab", "adversary-lr-0123456789ab"),
        ("operator-review", "adversary-operator-review"),
        ("lr-abcdef012345", "adversary-lr-abcdef012345-2"),
    ],
    ids=["generated-id", "explicit-id", "later-agent-ordinal"],
)
def test_preupgrade_pruned_allocation_can_finish_terminal_archive(
    tmp_path: Path,
    request_id: str,
    agent: str,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(
        store,
        request_id=request_id,
        agent=agent,
    )
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _make_preupgrade_pruned_allocation_fixture(store, state, request_id)

    sup.archive_ephemeral_request(
        store,
        state,
        request_id,
        terminal_state=eph.STATE_TIMED_OUT,
        reason="timed out",
        now_epoch=NOW,
    )

    assert request_id not in state["ephemeral_reviewers"]["active"]
    assert store.read_launch_request(request_id) is None
    assert agent not in store.load_config()["agents"]
    assert agent in store.retired_agents()


def test_preupgrade_pruned_allocation_can_finish_attended_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(
        store,
        request_id="lr-0123456789ab",
    )
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _make_preupgrade_pruned_allocation_fixture(store, state, request_id)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    monkeypatch.setattr(cli, "_owner_identity_gone", lambda _pid, _start: True)
    monkeypatch.setattr(cli.time, "time", lambda: NOW)

    item = _current_ephemeral_reset_item(store, state, request_id)
    rc = cli.main(item["operator_argv"][1:])

    assert rc == 0
    persisted = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    assert request_id not in persisted["ephemeral_reviewers"]["active"]
    assert store.read_launch_request(request_id) is None
    assert agent not in store.load_config()["agents"]
    assert agent in store.retired_agents()


@pytest.mark.parametrize(
    "break_evidence",
    [
        lambda _store, state, rid: state["ephemeral_reviewers"].update({
            "launch_history": [{"request_id": rid}],
        }),
        lambda _store, state, rid: state["ephemeral_reviewers"]["active"][rid].update({
            "prepared_epoch": NOW + 1,
        }),
        lambda _store, state, rid: state["ephemeral_reviewers"]["active"][rid].update({
            "prepared_epoch": 10 ** 400,
        }),
        lambda store, _state, rid: store.update_launch_request(
            rid, {"claimed_by": "other"},
        ),
        lambda store, _state, rid: store.update_launch_request(
            rid, {"profile": "other-profile"},
        ),
        lambda _store, state, rid: state["ephemeral_reviewers"]["active"][rid].update({
            "identity_binding_version": 1,
        }),
        lambda _store, state, rid: state["ephemeral_reviewers"]["active"][rid].update({
            "identity_binding_version": None,
        }),
        lambda _store, state, rid: state["ephemeral_reviewers"].update({
            "launch_history": [{
                "request_id": "lr-other",
                "agent": state["ephemeral_reviewers"]["active"][rid]["agent"],
                "at_epoch": NOW,
            }],
        }),
    ],
    ids=[
        "malformed-history",
        "epoch-mismatch",
        "unbounded-epoch",
        "wrong-claimer",
        "profile-mismatch",
        "new-binding-version",
        "malformed-binding-version",
        "conflicting-allocation",
    ],
)
def test_preupgrade_pruned_allocation_fallback_requires_closed_exact_evidence(
    tmp_path: Path,
    break_evidence,
) -> None:
    store = _store(tmp_path)
    request_id, _agent, _source_hash = _write_attended_ephemeral_hold_fixture(
        store,
        request_id="lr-0123456789ab",
    )
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _make_preupgrade_pruned_allocation_fixture(store, state, request_id)
    break_evidence(store, state, request_id)

    marker = store.read_launch_request(request_id)
    entry = state["ephemeral_reviewers"]["active"][request_id]
    assert not sup._ephemeral_request_identity_matches(  # noqa: SLF001
        store,
        state,
        entry,
        marker,
        request_id,
    )


@pytest.mark.parametrize("binding_version", [None, False, True, 1.0, 2, "1"])
def test_ephemeral_archive_rejects_unknown_identity_binding_version(
    tmp_path: Path,
    binding_version: object,
) -> None:
    store = _store(tmp_path)
    request_id, _agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    entry = state["ephemeral_reviewers"]["active"][request_id]
    entry["identity_binding_version"] = binding_version

    assert not sup._ephemeral_request_identity_matches(  # noqa: SLF001
        store,
        state,
        entry,
        store.read_launch_request(request_id),
        request_id,
    )


def test_attention_marker_loader_distinguishes_corrupt_from_archived(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, agent, source_hash = _write_attended_ephemeral_hold_fixture(store)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _stage_ephemeral_archive_retry(
        store,
        state,
        request_id,
        agent,
        source_hash,
        verification_mode="operator_attested",
        verified_launch_nonce=None,
    )
    marker_path = store.launch_requests_dir / f"{request_id}.json"
    original = store.read_launch_request(request_id)
    assert original is not None

    marker_path.write_text("{corrupt", encoding="utf-8")
    assert request_id not in sup.active_ephemeral_launch_markers(store, state)

    marker_path.unlink()
    assert sup.active_ephemeral_launch_markers(store, state)[request_id] == original


def _current_ephemeral_reset_item(
    store: Store,
    state: dict,
    request_id: str,
    *,
    actor: str = "lead",
) -> dict:
    admissions = sup.evaluate_process_tree_reset_admissions(
        store,
        state,
        actor=actor,
        now_epoch=NOW,
    )
    return next(
        item
        for item in att.process_tree_hold_items(
            state,
            supervisor_config=cli._load_supervisor_config(store),  # noqa: SLF001
            root=store.root,
            launch_requests=sup.active_ephemeral_launch_markers(store, state),
            lane_workspaces=sup.active_ephemeral_lane_workspaces(store),
            reset_admissions=admissions,
        )
        if item["item_id"] == f"process_tree_hold:ephemeral:{request_id}"
    )


def _stage_ephemeral_archive_retry(
    store: Store,
    state: dict,
    request_id: str,
    agent: str,
    source_hash: str,
    *,
    verification_mode: str,
    verified_launch_nonce: str | None,
) -> None:
    marker = store.read_launch_request(request_id)
    assert marker is not None
    entry = state["ephemeral_reviewers"]["active"][request_id]
    sup.stage_attended_ephemeral_archive(
        state,
        request_id,
        agent=agent,
        launch_marker=marker,
        held_terminal=entry["held_terminal"],
        hold_source_hash=source_hash,
        acknowledged_by="lead",
        verification_mode=verification_mode,
        verified_launch_nonce=verified_launch_nonce,
        verified_identity_count=(1 if verification_mode == "strict_identity" else 0),
        reason="operator verified the terminal request can be archived",
        now_epoch=NOW,
    )
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)


def _drift_ephemeral_terminal(state: dict, request_id: str) -> None:
    entry = state["ephemeral_reviewers"]["active"][request_id]
    original = entry["held_terminal"]
    entry["held_terminal"] = {
        "terminal_state": original["terminal_state"],
        "reason": "a different valid terminal fact replaced the staged one",
        "completion": dict(original["completion"]),
    }


def test_rendered_strict_identity_ephemeral_retry_argv_executes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, agent, source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _stage_ephemeral_archive_retry(
        store,
        state,
        request_id,
        agent,
        source_hash,
        verification_mode="strict_identity",
        verified_launch_nonce=SUPERVISOR_NONCE,
    )

    item = _current_ephemeral_reset_item(store, state, request_id)
    argv = item["operator_argv"]

    assert item["attended_disposition_mode"] == "strict_identity"
    assert argv[:4] == ["agenttalk", "--root", str(tmp_path), "supervise"]
    assert argv[argv.index("--verified-launch-nonce") + 1] == SUPERVISOR_NONCE
    assert cli.main([*argv[1:], "--now", str(NOW)]) == 0


def test_staged_ephemeral_terminal_drift_hides_rendered_remedy(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, agent, source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _stage_ephemeral_archive_retry(
        store,
        state,
        request_id,
        agent,
        source_hash,
        verification_mode="operator_attested",
        verified_launch_nonce=None,
    )
    _drift_ephemeral_terminal(state, request_id)
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")

    item = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


def test_staged_ephemeral_terminal_drift_does_not_name_kill_switch(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, agent, source_hash = _write_attended_ephemeral_hold_fixture(store)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    original_terminal = dict(
        state["ephemeral_reviewers"]["active"][request_id]["held_terminal"]
    )
    _stage_ephemeral_archive_retry(
        store,
        state,
        request_id,
        agent,
        source_hash,
        verification_mode="operator_attested",
        verified_launch_nonce=None,
    )
    _drift_ephemeral_terminal(state, request_id)
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")

    drifted = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_argv" not in drifted
    assert ".agenttalk/supervisor.kill" not in drifted["recommendation"]

    state["ephemeral_reviewers"]["active"][request_id][
        "held_terminal"
    ] = original_terminal
    sup.save_supervisor_state(store.dir / "supervisor-state.json", state)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    matching = _current_ephemeral_reset_item(store, state, request_id)
    assert ".agenttalk/supervisor.kill" in matching["recommendation"]


def test_finish_attended_ephemeral_archive_rejects_drifted_terminal_before_effects(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, agent, source_hash = _write_attended_ephemeral_hold_fixture(store)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    _stage_ephemeral_archive_retry(
        store,
        state,
        request_id,
        agent,
        source_hash,
        verification_mode="operator_attested",
        verified_launch_nonce=None,
    )
    _drift_ephemeral_terminal(state, request_id)
    active_before = json.loads(json.dumps(
        state["ephemeral_reviewers"]["active"][request_id]
    ))
    pending_before = json.loads(json.dumps(
        state["ephemeral_reviewers"]["attended_archive_pending"][request_id]
    ))

    with pytest.raises(
        ValueError,
        match="active ephemeral HOLD changed after its attended archive was staged",
    ):
        sup.finish_attended_ephemeral_archive(store, state, request_id)

    assert state["ephemeral_reviewers"]["active"][request_id] == active_before
    assert (
        state["ephemeral_reviewers"]["attended_archive_pending"][request_id]
        == pending_before
    )
    assert store.read_launch_request(request_id) is not None
    assert not (store.launch_requests_archive_dir / f"{request_id}.json").exists()
    assert agent in store.load_config()["agents"]
    assert agent not in store.retired_agents()


def test_cli_attended_ephemeral_tree_hold_archives_exact_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    monkeypatch.setattr(cli, "_owner_identity_gone", lambda _pid, _start: True)
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    item = _current_ephemeral_reset_item(store, state, request_id)
    source_hash = item["source_hash"]

    assert item["operator_argv"][5:7] == ["--request-id", request_id]
    rc = cli.main(item["operator_argv"][1:])

    assert rc == 0
    persisted = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    assert request_id not in persisted["ephemeral_reviewers"]["active"]
    archived = json.loads(
        (store.launch_requests_archive_dir / f"{request_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert archived["terminal_state"] == eph.STATE_TIMED_OUT
    assert archived["completion"]["status"] == eph.COMPLETION_NONE
    assert archived["attended_teardown"] == {
        "schema_version": 1,
        "agent": agent,
        "request_id": request_id,
        "hold_source_hash": source_hash,
        "acknowledged_by": "lead",
        "verification_mode": "operator_attested",
        "verified_launch_nonce": None,
        "verified_identity_count": 0,
        "acknowledged_at": "1970-01-01T00:16:40Z",
        "reason": "operator verified the terminal request can be archived",
    }
    assert agent not in store.load_config()["agents"]
    assert agent in store.retired_agents()
    assert "dev" in store.load_config()["agents"]


def test_cli_attended_ephemeral_hold_archives_without_strict_tree_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state_path = store.dir / "supervisor-state.json"
    state = sup.load_supervisor_state(state_path)
    wrt.runtime_path(store.state_dir, agent).unlink()
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    item = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_command" not in item
    assert "no scripted remedy applies in this state" not in item["recommendation"]
    assert item["operator_argv"][5:7] == ["--request-id", request_id]

    rc = cli.main(item["operator_argv"][1:])

    assert rc == 0
    persisted = sup.load_supervisor_state(state_path)
    assert request_id not in persisted["ephemeral_reviewers"]["active"]
    archived = json.loads(
        (store.launch_requests_archive_dir / f"{request_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert archived["attended_teardown"]["verification_mode"] == (
        "operator_attested"
    )
    assert archived["attended_teardown"]["verified_launch_nonce"] is None
    assert agent in store.retired_agents()


@pytest.mark.parametrize(
    "inadmissible_context",
    ["journal_cap", "identity_missing", "archive_conflict"],
)
def test_ephemeral_archive_command_hidden_when_finish_precondition_fails(
    tmp_path: Path,
    inadmissible_context: str,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    if inadmissible_context == "journal_cap":
        state["ephemeral_reviewers"]["attended_archive_pending"] = {
            f"lr-other-{index}": {}
            for index in range(64)
        }
    elif inadmissible_context == "identity_missing":
        store.remove_agent(agent)
    else:
        store.launch_requests_archive_dir.mkdir(parents=True, exist_ok=True)
        (store.launch_requests_archive_dir / f"{request_id}.json").write_text(
            "{}",
            encoding="utf-8",
        )

    item = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


def test_corrupt_archive_journal_reason_cannot_erase_refusal_attention(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request_id, agent, source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    entry = state["ephemeral_reviewers"]["active"][request_id]
    marker = store.read_launch_request(request_id)
    assert marker is not None
    sup.stage_attended_ephemeral_archive(
        state,
        request_id,
        agent=agent,
        launch_marker=marker,
        held_terminal=entry["held_terminal"],
        hold_source_hash=source_hash,
        acknowledged_by="lead",
        verification_mode="operator_attested",
        verified_launch_nonce=None,
        verified_identity_count=0,
        reason="operator verified the terminal request can be archived",
        now_epoch=NOW,
    )
    state["ephemeral_reviewers"]["attended_archive_pending"][request_id][
        "reason"
    ] = "malformed-\ud800-reason"

    item = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


@pytest.mark.parametrize(
    "marker_poison",
    ["escaped-\ud800-surrogate", float("nan")],
    ids=["unpaired-surrogate", "non-finite-number"],
)
def test_unpersistable_launch_marker_hides_ephemeral_archive_remedy(
    tmp_path: Path,
    marker_poison: object,
) -> None:
    store = _store(tmp_path)
    request_id, _agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    marker = store.read_launch_request(request_id)
    assert marker is not None
    marker["unrelated_persistence_poison"] = marker_poison
    (store.launch_requests_dir / f"{request_id}.json").write_text(
        json.dumps(marker, ensure_ascii=True),
        encoding="utf-8",
    )
    assert eph.validate_marker(store.read_launch_request(request_id)) == []
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")

    item = _current_ephemeral_reset_item(store, state, request_id)

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


def test_cli_attended_ephemeral_archive_recovers_after_final_state_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    monkeypatch.setattr(cli, "_owner_identity_gone", lambda _pid, _start: True)
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    item = _current_ephemeral_reset_item(store, state, request_id)
    args = [
        *item["operator_argv"][1:],
        "--now", str(NOW),
    ]
    real_save = sup.save_supervisor_state
    save_calls = 0

    def fail_final_save(path: Path, state: dict) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise sup.SupervisorPersistenceError("injected final save failure")
        real_save(path, state)

    monkeypatch.setattr(sup, "save_supervisor_state", fail_final_save)

    assert cli.main(args) == 3
    staged = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    assert request_id in staged["ephemeral_reviewers"]["active"]
    assert request_id in staged["ephemeral_reviewers"][
        "attended_archive_pending"
    ]
    assert store.read_launch_request(request_id) is None
    assert agent in store.retired_agents()

    monkeypatch.setattr(sup, "save_supervisor_state", real_save)
    retry_args = list(args)
    retry_args[-1] = str(NOW + 1)
    assert cli.main(retry_args) == 0
    recovered = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    assert request_id not in recovered["ephemeral_reviewers"]["active"]
    assert request_id not in recovered["ephemeral_reviewers"].get(
        "attended_archive_pending", {}
    )
    assert recovered["ephemeral_reviewers"]["attended_archive_history"][-1][
        "request_id"
    ] == request_id


def test_cli_attended_ephemeral_archive_recovery_allows_liaison_turnover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    request_id, agent, _source_hash = _write_attended_ephemeral_hold_fixture(store)
    (store.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    state = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    item = _current_ephemeral_reset_item(store, state, request_id)
    args = [
        *item["operator_argv"][1:],
        "--now", str(NOW),
    ]
    real_save = sup.save_supervisor_state
    save_calls = 0

    def fail_final_save(path: Path, state: dict) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise sup.SupervisorPersistenceError("injected final save failure")
        real_save(path, state)

    monkeypatch.setattr(sup, "save_supervisor_state", fail_final_save)

    assert cli.main(args) == 3
    assert agent in store.retired_agents()

    monkeypatch.setattr(sup, "save_supervisor_state", real_save)
    store.add_agent("newlead")
    store.set_operator_facing("newlead")
    staged = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    retry_item = _current_ephemeral_reset_item(
        store,
        staged,
        request_id,
        actor="newlead",
    )
    assert retry_item["source_hash"] != item["source_hash"]
    retry_args = [
        *retry_item["operator_argv"][1:],
        "--now", str(NOW + 1),
    ]

    assert cli.main(retry_args) == 0
    recovered = sup.load_supervisor_state(store.dir / "supervisor-state.json")
    history = recovered["ephemeral_reviewers"]["attended_archive_history"]
    assert history[-1]["acknowledged_by"] == "lead"
    assert request_id not in recovered["ephemeral_reviewers"].get(
        "attended_archive_pending", {}
    )


def test_invalid_review_result_stays_hold_and_janitor_retires_orphan(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_agent("adversary-lr-hold", role="reviewer", groups=["ephemeral-reviewers"])
    s.send(sender="lead", recipient="adversary-lr-hold", kind="review-request",
           body="review", meta={"request_id": "lr-hold"})
    s.send(sender="adversary-lr-hold", recipient="lead", kind="review-result",
           body="missing status", meta={"request_id": "lr-hold"})
    result = eph.classify_review_result(
        s.valid_messages(), request_id="lr-hold",
        agent="adversary-lr-hold", requester="lead")
    assert result["status"] == eph.COMPLETION_MALFORMED
    assert result["hold"] is True and result["terminal"] is False

    s.add_agent("adversary-orphan", role="reviewer", groups=["ephemeral-reviewers"])
    plan = sup.plan_actions(_report(orphan=True), {}, _cfg(), now_epoch=NOW, snapshot=[])
    assert plan["ephemeral_reviewers"]["janitor:adversary-orphan"]["action"] == eph.ACTION_JANITOR
    assert sup.janitor_retire_ephemeral_orphan(s, "adversary-orphan") is True
    assert "adversary-orphan" in s.retired_agents()


def test_wrapper_one_shot_leaves_unrelated_message_unread(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_agent("adversary-lr-scope", role="reviewer")
    s.send(sender="lead", recipient="adversary-lr-scope", kind="review-request",
           body="other", meta={"request_id": "lr-other"})
    driven: list[str] = []

    turns = loop.run_loop(
        s,
        "adversary-lr-scope",
        lambda rec: driven.append(rec["request_id"]) or True,
        only_request_id="lr-target",
        max_polls=3,
        sleep=lambda _d: None,
        clock=lambda: 0.0,
    )

    assert turns == 0
    assert driven == []
    assert s.cursor("adversary-lr-scope") == ""
