"""C6 (0.40.0): ONE in-process lifecycle regression test over a REAL throwaway git
repo. It drives ``cli.main`` end-to-end and asserts EXIT CODES + JSON verdict SHAPE
plus the NEGATIVE regressions the 0.40.0 hardening fixes (skipped-blocker HOLDs;
broad/forged shared approval -> HOLD_SHARED_WRONG_APPROVAL; wrapper one-shot does not
starve on / consume an unrelated message; reset clears messages + state/lanes while
PRESERVING knowledge/closes/gates/domains/lane-deliveries).

This is a guard rail, NOT a substitute for the focused unit tests in test_gates.py /
test_lanes.py / test_wrapper_loop.py - it exercises the seams between them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import loop

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(root: Path, *args: str) -> str:
    # Deterministic author/committer so commits are reproducible across hosts.
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    import os
    full = {**os.environ, **env}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8", env=full).stdout


def _commit(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"change {rel}")
    return _git(root, "rev-parse", "HEAD").strip()


def _run(argv: list[str], root: Path) -> int:
    if argv[:2] == ["lane", "assign"] and "--no-worktree" not in argv and "--worktrees-root" not in argv:
        argv = [*argv, "--no-worktree", "--worktree-waiver-reason", "legacy e2e test"]
    return cli.main(["--root", str(root), *argv])


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_e2e_lifecycle(tmp_path: Path, capsys) -> None:
    root = tmp_path
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    base = _commit(root, "src/core/a.py", "base\n")
    br = _git(root, "branch", "--show-current").strip()

    s = Store(root)
    s.init(["lead", "dev", "dev2"])
    s.set_role("lead", "lead")
    # Two shared entries with DISTINCT approvers so an ambiguous --path is refused and
    # the over-grant cannot cross entries.
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1,
        "domains": {"core": {"title": "Core", "owners": {"agents": ["dev"]},
                             "owned_globs": ["src/core/**"]}},
        "shared_paths": [
            {"glob": "shared/**", "category": "schema", "requires": "lead-approval",
             "default_approvers": {"agents": ["dev2"]}},
            {"glob": "shared/secret.sql", "category": "schema", "requires": "lead-approval",
             "default_approvers": {"agents": ["dev"]}}]}), encoding="utf-8")

    # ---- GATES: skipped blocker HOLDs (not-run, not not-applicable) -> then green GO
    assert _run(["gate", "set", "--from", "lead", "--name", "ci", "--status", "skipped",
                 "--severity", "blocker", "--scope", "release", "--required",
                 "--evidence-source", "local_command"], root) == 0
    capsys.readouterr()
    assert _run(["gate", "check", "--release", "--json"], root) == 3   # HOLD exit code
    res = _json_out(capsys)
    assert res["verdict"] == "HOLD"
    ci = next(b for b in res["blockers"] if b["name"] == "ci")
    assert "skipped" in ci["reason"]
    # set it green from CI with evidence -> GO
    assert _run(["gate", "set", "--from", "lead", "--name", "ci", "--status", "green",
                 "--severity", "blocker", "--scope", "release",
                 "--evidence-source", "automation_ci", "--evidence", "http://ci/1"], root) == 0
    capsys.readouterr()
    assert _run(["gate", "check", "--release", "--json"], root) == 0   # GO
    assert _json_out(capsys)["verdict"] == "GO"

    # ---- LANE owned-path deliver: GO writes a durable artifact (preserved by reset)
    head1 = _commit(root, "src/core/a.py", "base\nwork\n")
    assert _run(["lane", "assign", "--id", "lwork", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root) == 0
    capsys.readouterr()
    # --gate-scope release: the required `ci` gate is release-scoped + now green, so the
    # lane's gate check is satisfied (a default lane:<id> scope would correctly HOLD on
    # the present-but-wrong-scope required gate - that fail-closed path is unit-tested).
    assert _run(["lane", "deliver", "--id", "lwork", "--from", "dev", "--head", head1,
                 "--gate-scope", "release"], root) == 0
    arts = list((s.dir / "lane-deliveries").glob("lwork-*.json"))
    assert len(arts) == 1 and json.loads(arts[0].read_text(encoding="utf-8"))["verdict"] == "GO"

    # ---- LANE shared approval (D-11: ALL matching entries must approve):
    # The touched path shared/secret.sql matches BOTH shared/** (dev2) and shared/secret.sql
    # (dev); under all-matching, BOTH approvers must sign off (no winner-picking bypass).
    head2 = _commit(root, "shared/secret.sql", "create table t;\n")
    assert _run(["lane", "assign", "--id", "lsh", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", head1, "--target", br, "--path", "shared"], root) == 0
    capsys.readouterr()
    # missing approval -> HOLD
    assert _run(["lane", "check", "--id", "lsh", "--head", head2, "--gate-scope", "release", "--json"], root) == 3
    assert "shared_path_missing_approval" in {h["code"] for h in _json_out(capsys)["holds"]}
    # dev2 approves -> records shared/** only (dev2 not authorized for shared/secret.sql);
    # the shared/secret.sql entry is STILL unapproved -> HOLD.
    assert _run(["lane", "approve-shared", "--id", "lsh", "--path", "shared/secret.sql",
                 "--from", "dev2", "--reason", "broad ok"], root) == 0
    capsys.readouterr()
    assert _run(["lane", "check", "--id", "lsh", "--head", head2, "--gate-scope", "release", "--json"], root) == 3
    assert "shared_path_missing_approval" in {h["code"] for h in _json_out(capsys)["holds"]}  # still HOLD
    # dev approves the narrower entry -> now BOTH matching entries approved -> GO.
    assert _run(["lane", "approve-shared", "--id", "lsh", "--path", "shared/secret.sql",
                 "--from", "dev", "--reason", "dba ok"], root) == 0
    capsys.readouterr()
    assert _run(["lane", "check", "--id", "lsh", "--head", head2, "--gate-scope", "release", "--json"], root) == 0
    assert _json_out(capsys)["verdict"] == "GO"
    # changing the registry invalidates the recorded approvals -> HOLD_SHARED_WRONG_APPROVAL
    reg = json.loads((s.dir / "domains.json").read_text(encoding="utf-8"))
    reg["domains"]["core"]["title"] = "Core (renamed)"   # registry hash changes
    (s.dir / "domains.json").write_text(json.dumps(reg), encoding="utf-8")
    assert _run(["lane", "check", "--id", "lsh", "--head", head2, "--gate-scope", "release", "--json"], root) == 3
    holds = {h["code"] for h in _json_out(capsys)["holds"]}
    assert "shared_path_wrong_approval" in holds

    # ---- WRAPPER one-shot: scoped receive does not starve on / consume an unrelated head
    s.send(sender="lead", recipient="dev", body="unrelated head")   # no request_id
    target = s.send(sender="lead", recipient="dev", body="the scoped task",
                    meta={"request_id": "rq-eph"})
    seen: list[str] = []
    turns = loop.run_loop(s, "dev", lambda rec: (seen.append(rec["body"]) or True),
                          clock=lambda: 0.0, sleep=lambda d: None, max_turns=1,
                          only_request_id="rq-eph")
    assert turns == 1 and seen == ["the scoped task"]   # only the scoped request
    assert s.cursor("dev") == ""                         # unrelated NOT globally consumed
    assert s.thread_seen("dev", "rq-eph") == target.id

    # ---- artifacts that reset must PRESERVE (besides gates/domains/lane-deliveries)
    (s.dir / "closes").mkdir(parents=True, exist_ok=True)
    (s.dir / "closes" / "c1.json").write_text(json.dumps({"close_id": "c1"}), encoding="utf-8")
    (s.dir / "knowledge").mkdir(parents=True, exist_ok=True)
    (s.dir / "knowledge" / "notes.jsonl").write_text('{"event":"publish"}\n', encoding="utf-8")
    assert s.messages_dir.exists() and any(s.messages_dir.iterdir())   # messages present
    assert lane_active_ids(s) == {"lsh"}                                # one active lane

    # ---- RESET: clears messages + state/lanes; preserves the durable assurance state
    assert _run(["reset"], root) == 0
    capsys.readouterr()
    assert not any(s.messages_dir.glob("*.json"))          # messages cleared
    assert lane_active_ids(s) == set()                      # state/lanes.json cleared
    assert s.cursor("dev") == ""                            # cursors reset
    # preserved (directly under .agenttalk/, not in messages/ or state/):
    assert (s.dir / "gates.json").exists()
    assert (s.dir / "domains.json").exists()
    assert (s.dir / "closes" / "c1.json").exists()
    assert (s.dir / "knowledge" / "notes.jsonl").exists()
    assert list((s.dir / "lane-deliveries").glob("lwork-*.json"))
    # the preserved gate state still reads back GO after reset
    assert _run(["gate", "check", "--release", "--json"], root) == 0
    assert _json_out(capsys)["verdict"] == "GO"


def lane_active_ids(store: Store) -> set[str]:
    from agenttalk import lanes
    return {ln.get("lane_id") for ln in lanes.active_lanes(lanes.load_lanes(store))}
