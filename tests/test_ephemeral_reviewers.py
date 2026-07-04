from __future__ import annotations

import json
from pathlib import Path

from agenttalk import ephemeral as eph
from agenttalk import cli
from agenttalk import supervisor as sup
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

    s.send(sender=agent, recipient="lead", kind="review-result", body="reject",
           meta={"request_id": "lr-prep", "status": "rejected"})
    report = sup.build_report(s, now_epoch=NOW + 1, state=state, supervisor_config=_cfg())
    plan = sup.plan_actions(report, state, _cfg(), now_epoch=NOW + 1, snapshot=[])
    done = plan["ephemeral_reviewers"]["lr-prep"]
    assert done["action"] == eph.ACTION_COMPLETE
    assert done["completion"]["counter"] is True

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


def test_cli_archive_launch_request_preserves_completion_evidence(tmp_path: Path) -> None:
    s = _store(tmp_path)
    agent = "adversary-lr-cli-archive"
    marker = _marker("lr-cli-archive", state=eph.STATE_REQUESTED, agent=agent)
    s.write_launch_request(marker)
    s.add_agent(agent, role="reviewer", groups=["ephemeral-reviewers"])
    state_path = tmp_path / "supervisor-state.json"
    state_path.write_text(json.dumps({
        "ephemeral_reviewers": {
            "active": {
                "lr-cli-archive": {
                    "request_id": "lr-cli-archive",
                    "agent": agent,
                    "requested_by": "lead",
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


def test_retired_name_is_not_reused(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_agent("adversary-lr-repeat", role="reviewer", groups=["ephemeral-reviewers"])
    s.retire_agent("adversary-lr-repeat", reason="done")
    name = eph.choose_agent_name("lr-repeat", s.load_config()["agents"], s.retired_agents())
    assert name != "adversary-lr-repeat"
    assert name.startswith("adversary-lr-repeat-")


def test_launched_exit_without_result_fails_without_restart() -> None:
    state = {"ephemeral_reviewers": {"active": {
        "lr-dead": {"agent": "adversary-lr-dead", "requested_by": "lead",
                    "phase": eph.STATE_LAUNCHED, "launcher_pid": 10,
                    "launcher_start": "t-start", "deadline_epoch": NOW + 100}
    }}}
    report = _report(active={"lr-dead": {
        "completion": {"status": eph.COMPLETION_NONE, "terminal": False, "hold": True}
    }})
    plan = sup.plan_actions(report, state, _cfg(), now_epoch=NOW, snapshot=[])
    failed = plan["ephemeral_reviewers"]["lr-dead"]
    assert failed["action"] == eph.ACTION_FAILED
    assert failed["kill_targets"] == []
    assert failed["reason"].endswith("no auto-restart")


def test_timeout_plans_process_tree_kill_targets() -> None:
    launcher_start = "2026-07-04T07:20:31.1000000+00:00"
    child_start = "2026-07-04T07:20:31.2000000+00:00"
    state = {"ephemeral_reviewers": {"active": {
        "lr-timeout": {"agent": "adversary-lr-timeout", "requested_by": "lead",
                       "phase": eph.STATE_LAUNCHED, "launcher_pid": 10,
                       "launcher_start": launcher_start, "launched_epoch": NOW - 100,
                       "deadline_epoch": NOW - 1,
                       "launcher_nonce": SUPERVISOR_NONCE,
                       "launcher_nonce_injected": True,
                       "launcher_nonce_source": "agenttalk_global_arg"}
    }}}
    report = _report(active={"lr-timeout": {
        "completion": {"status": eph.COMPLETION_NONE, "terminal": False, "hold": True}
    }})
    report["root_key"] = sup._root_key(TEST_ROOT)
    snap = [
        {"pid": 10, "parent_pid": 1, "name": "python.exe",
         "command_line": (
             "python -m agenttalk "
             f"--supervisor-launch-nonce {SUPERVISOR_NONCE} "
             f"--root {TEST_ROOT} wrap --for adversary-lr-timeout --loop"
         ),
         "start_time": launcher_start},
        {"pid": 11, "parent_pid": 10, "name": "codex.exe", "command_line": "codex exec",
         "start_time": child_start},
    ]
    plan = sup.plan_actions(report, state, _cfg(), now_epoch=NOW, snapshot=snap)
    timeout = plan["ephemeral_reviewers"]["lr-timeout"]
    assert timeout["action"] == eph.ACTION_TIMEOUT
    assert {t["pid"] for t in timeout["kill_targets"]} == {10, 11}


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
